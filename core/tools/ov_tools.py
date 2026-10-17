#!/usr/bin/env python3
"""OpenViking 记忆工具（skill 面）：search/find 语义搜索 / 读写 / 记住 / Session 管理

面向模型的 ov skill 实现，与「自动召回/抓取」子系统分离（见 core/recall.py）：
本模块只提供模型按需调用的记忆操作，不含自动注入 / 对话抓取；recall.py 自带独立的
HTTP 请求，记忆召回走单独请求，两条链路互不依赖。

所有工具统一返回 DSH/OpenViking 规范信封：
  成功 → {"status": "ok",  "result": {...}}
  失败 → {"status": "error", "error": "...", "code": "..."}
底层 OpenViking 后端自身也采用同一信封，故 read/list_dir/write/session 等
直接透传其后端响应；remember/search/find 在透传基础上补全语义字段。
"""
import hashlib
import json
import os
import re

import requests

from .. import config
from .envelope import ok, error, is_error, passthrough


# ============= 基础请求 =============
def _ov_base():
    """延迟读取 OpenViking URL，避免模块级别求值导致 .env 未加载的问题"""
    return os.environ.get('OPENVIKING_URL', '')


def openviking_peer_id():
    """解析当前 actor peer：显式 OV_PEER_ID > 按工作目录派生(ws-*) > 回退 OPENVIKING_AGENT。

    与官方 DSH 插件一致：默认可配置按 workspace 派生 peer，使不同项目的自动
    捕获/召回与记忆写入彼此隔离，避免跨项目串记忆。
    """
    explicit = os.environ.get('OV_PEER_ID') or config.OV_PEER_ID
    if explicit:
        return explicit
    if config.OV_WORKSPACE_PEER:
        digest = hashlib.md5(config.PROJECT_ROOT.encode('utf-8')).hexdigest()[:12]
        return f"ws-{digest}"
    return os.environ.get('OPENVIKING_AGENT', 'default')


def _hdr(v):
    """HTTP 头值必须是 latin-1 可编码；非 ASCII（如中文）做 RFC2047 编码，避免 requests 按 latin-1 编码抛错。"""
    v = "" if v is None else str(v)
    try:
        v.encode("latin-1")
        return v
    except UnicodeEncodeError:
        from email.header import Header
        return str(Header(v, "utf-8"))


def _ov_headers():
    """获取 OpenViking API 请求头"""
    key = os.environ.get('OPENVIKING_KEY', '')
    if not key:
        raise ValueError("OPENVIKING_KEY 未设置")
    user = os.environ.get('OPENVIKING_USER', '')
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-OpenViking-Account": "default",
        "X-OpenViking-User": _hdr(user),
        "X-OpenViking-Peer": _hdr(openviking_peer_id()),
    }


# HTTP 状态码 → 语义化错误 code（与 name 解析的 not_found/ambiguous 风格统一）
_HTTP_STATUS_CODES = {
    400: "invalid_argument",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "already_exists",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "invalid_argument",
    429: "rate_limited",
    500: "server_error",
    502: "bad_gateway",
    503: "unavailable",
    504: "gateway_timeout",
}
# 错误信息里可能带内网地址/URL，统一脱敏后再回传给模型
_URL_RE = re.compile(r'https?://[^\s"\')]+', re.I)
_IP_RE = re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b')


def _sanitize(text):
    """脱敏错误文本中的 URL / IP，避免把内网地址泄露给上层。"""
    text = _URL_RE.sub("<ov>", str(text or ""))
    return _IP_RE.sub("<ov>", text)


def _http_failure(exc):
    """把 requests.HTTPError 映射为语义化错误信封（隐藏 URL/IP，尽量透出后端 message）。"""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    code = _HTTP_STATUS_CODES.get(status, "http")
    detail = ""
    if resp is not None:
        try:
            data = resp.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                detail = err.get("message") or err.get("code") or ""
            elif isinstance(err, str):
                detail = err
            elif data.get("detail"):
                detail = str(data.get("detail"))
            elif data.get("message"):
                detail = str(data.get("message"))
    detail = _sanitize(detail).strip()
    msg = f"OpenViking 请求失败 (HTTP {status})"
    if detail:
        msg += f" - {detail}"
    return json.loads(error(msg, code=code))


def _ov_get(path, params=None, timeout=15):
    """OpenViking GET 请求；失败返回错误信封 dict（不抛异常）"""
    try:
        r = requests.get(f"{_ov_base()}{path}", headers=_ov_headers(),
                         params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        return json.loads(error("OpenViking 请求超时", code="timeout"))
    except requests.HTTPError as e:
        return _http_failure(e)
    except Exception as e:
        return json.loads(error(f"OpenViking 请求失败 - {_sanitize(e)}", code="transport"))


def _ov_post(path, payload, timeout=15):
    """OpenViking POST 请求；失败返回错误信封 dict（不抛异常）"""
    try:
        r = requests.post(f"{_ov_base()}{path}", headers=_ov_headers(),
                           json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        return json.loads(error("OpenViking 请求超时", code="timeout"))
    except requests.HTTPError as e:
        return _http_failure(e)
    except Exception as e:
        return json.loads(error(f"OpenViking 请求失败 - {_sanitize(e)}", code="transport"))


def _ov_delete(path, params=None, timeout=15):
    """OpenViking DELETE 请求；失败返回错误信封 dict（不抛异常）"""
    try:
        r = requests.delete(f"{_ov_base()}{path}", headers=_ov_headers(),
                            params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        return json.loads(error("OpenViking 请求超时", code="timeout"))
    except requests.HTTPError as e:
        return _http_failure(e)
    except Exception as e:
        return json.loads(error(f"OpenViking 请求失败 - {_sanitize(e)}", code="transport"))


# ============= 搜索请求体（skill: find / search）=============
def _find_payload(query, score_threshold=None, limit=None, target_uri=""):
    payload = {
        "query": query,
        "score_threshold": score_threshold if score_threshold is not None else config.OV_FIND_THRESHOLD,
        "limit": limit if limit is not None else config.OV_FIND_LIMIT,
    }
    # 同/跨项目隔离走 X-OpenViking-Peer 请求头（见 _ov_headers），find 请求体无需再带 peer_id
    if target_uri:
        payload["target_uri"] = target_uri
    return payload


def _search_payload(query, score_threshold=None, limit=None):
    """上下文感知搜索（/api/v1/search/search）的请求体构造。

    与 find 完全对齐：peer 隔离只走 X-OpenViking-Peer 请求头（见 _ov_headers），
    请求体不携带任何 peer 字段。服务端 SearchRequest 无 peer_id 字段（旧实现误塞
    会被请求校验拒绝 → HTTP 400）；peer_scope 又要求 mode='context'，而本工具用
    默认 list 模式，故同样不带。peer 身份仅由请求头决定。
    """
    return {
        "query": query,
        "score_threshold": score_threshold if score_threshold is not None else config.OV_SEARCH_THRESHOLD,
        "limit": limit if limit is not None else config.OV_SEARCH_LIMIT,
    }


# ============= 按文件名解析 URI =============
def _memory_base(category: str = "") -> str:
    """当前 peer 的隔离记忆目录根。

    viking://user/<user>/peers/<agent>/memories/[_<category>/]
    是否作为读写落点取决于是否启用隔离（见 _active_memory_base）。
    """
    user = os.environ.get('OPENVIKING_USER', '')
    agent = os.environ.get('OPENVIKING_AGENT', 'default')
    base = f"viking://user/{user}/peers/{agent}/memories/"
    return base + (f"{category}/" if category else "")


def _ensure_parent_dirs(uri: str) -> None:
    """逐级 mkdir 创建 uri 的父目录（忽略已存在的错误）。"""
    parts = uri.split("/")
    for i in range(6, len(parts)):
        parent = "/".join(parts[:i]) + "/"
        _ov_post("/api/v1/fs/mkdir", {"uri": parent}, timeout=5)


def _memory_search_bases():
    """按名检索时依次尝试的根目录。

    顺序：当前 peer 记忆目录（与 openviking_remember 写入位置一致）→ 用户级记忆
    目录（官方默认布局 viking://user/<user>/memories/）→ 公共资源库
    viking://resources/。不同部署的记忆可能只存在于其中一个，顺序探测让“只传文件名”
    在这些布局下都能命中；resources 放最后，避免抢在记忆目录之前命中同名资源。
    """
    user = os.environ.get('OPENVIKING_USER', '')
    peer_base = _memory_base()
    user_base = f"viking://user/{user}/memories/"
    bases = []
    for b in (peer_base, user_base, "viking://resources/"):
        if b and b not in bases:
            bases.append(b)
    return bases


def _extract_glob_matches(result):
    """从 /api/v1/search/glob 响应里宽容地取出 URI 列表。"""
    raw = result.get("result") if isinstance(result, dict) else None
    matches = []
    if isinstance(raw, dict):
        for key in ("matches", "files", "results", "hits", "items"):
            if isinstance(raw.get(key), list):
                matches = raw[key]
                break
    elif isinstance(raw, list):
        matches = raw
    normalized, seen = [], set()
    for it in matches:
        uri = it if isinstance(it, str) else (it.get("uri") or it.get("path") if isinstance(it, dict) else None)
        if uri and uri not in seen:
            seen.add(uri)
            normalized.append(uri)
    return normalized


def openviking_resolve_name(name: str, base_uri: str = "", limit: int = 20) -> str:
    """按文件名/片段解析出记忆库中的完整 viking:// URI。

    服务端没有“只传文件名即可读写”的接口（content/read、content/write 的 uri 均必填），
    但提供 /api/v1/search/glob 做文件名模式匹配，这里用它完成解析。
    - name: 文件名或片段，如 "ov删除工具" / "ov删除工具.md"
    - base_uri: 检索根目录；留空时依次探测 peer 记忆 → 用户级记忆 → 公共资源库（见 _memory_search_bases）
    - limit: 最多返回的候选数（1~200）
    返回 result.base_uri 为“实际命中的根”，result.searched_bases 为本次搜索过的全部根
    （未命中时 base_uri 回退为第一个根）。命中 0 / 多命中时交由调用方决定如何处理。
    """
    name = (name or "").strip()
    if not name:
        return error("缺少文件名 name", code="bad_request")
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 20
    n = max(1, min(200, n))
    pattern = f"**/*{name}*"
    bases = [base_uri] if base_uri else _memory_search_bases()

    matched_base, matches = bases[0], []
    for base in bases:
        try:
            result = _ov_post(
                "/api/v1/search/glob",
                {"pattern": pattern, "uri": base, "node_limit": n},
                timeout=15,
            )
        except Exception as e:
            return error(f"解析文件名失败 - {_sanitize(e)}", code="internal")
        if is_error(result):
            return json.dumps(result, ensure_ascii=False)
        found = _extract_glob_matches(result)
        if found:
            matched_base, matches = base, found
            break
    return ok({
        "name": name,
        "base_uri": matched_base,
        "searched_bases": bases,
        "pattern": pattern,
        "count": len(matches),
        "matches": matches,
    })


def _resolve_single(name: str, base_uri: str = ""):
    """把 name 解析为唯一 URI。返回 (uri, response)。

    唯一命中 → (uri, None)；未命中/多命中 → ("", 已序列化信封字符串)。
    """
    result = openviking_resolve_name(name, base_uri)
    data = json.loads(result) if isinstance(result, str) else result
    if not isinstance(data, dict) or is_error(data):
        resp = result if isinstance(result, str) else json.dumps(data, ensure_ascii=False)
        return "", resp
    res = data.get("result") or {}
    matches = res.get("matches") or []
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return "", error(f"未找到匹配文件: {name}", code="not_found",
                         name=name, base_uri=res.get("base_uri", ""),
                         searched_bases=res.get("searched_bases"))
    return "", error(
        f"文件名 '{name}' 匹配到 {len(matches)} 个文件，请改用完整 uri 或更精确的名字",
        code="ambiguous", name=name, matches=matches)


def _active_memory_base():
    """默认记忆根（读/写/profile 统一落点），使各入口落在同一处。

    - 隔离模式（OV_PEER_ID / OV_WORKSPACE_PEER 开启）：固定用 peer 目录，维持项目隔离；
    - 非隔离：在 peer 记忆 → 用户级记忆 中取第一个**有内容**(count>0)的根，都空则取第一个
      存在的，都不存在则回落主 peer 根（由 _ensure_parent_dirs 逐级建目录）。
    只用记忆目录、不含 resources，避免把新记忆误建进公共资源库。
    """
    if config.OV_PEER_ID or config.OV_WORKSPACE_PEER:
        return _memory_base()
    user = os.environ.get('OPENVIKING_USER', '')
    candidates = [_memory_base(), f"viking://user/{user}/memories/"]
    first_existing = None
    for base in candidates:
        stat = _ov_get("/api/v1/fs/stat", params={"uri": base}, timeout=10)
        if is_error(stat):
            continue
        res = stat.get("result") if isinstance(stat, dict) else None
        if not isinstance(res, dict):
            continue
        if first_existing is None:
            first_existing = base
        try:
            count = int(res.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            return base
    return first_existing or _memory_base()


def _resolve_for_write(name: str, mode: str, base_uri: str = ""):
    """写入场景的文件名解析：唯一命中直接用；未命中且 mode=create 时按记忆根新建。

    返回 (uri, response)；response 非 None 表示解析失败/歧义，调用方应直接返回它。
    """
    uri, resp = _resolve_single(name, base_uri)
    if resp is None:
        return uri, None
    data = json.loads(resp) if isinstance(resp, str) else resp
    if isinstance(data, dict) and data.get("code") == "not_found" and mode == "create":
        rel = name.strip().lstrip("/")
        if not rel:
            return "", resp
        if "." not in rel.split("/")[-1]:
            rel += ".md"
        new_uri = (base_uri or _active_memory_base()) + rel
        _ensure_parent_dirs(new_uri)
        return new_uri, None
    return "", resp


# ============= 记忆 =============
def _extract_memories(result):
    """从 OpenViking 搜索响应中提取并归一化记忆列表。

    兼容多种后端返回结构（不同版本/部署可能字段不同）：
      - {"result": {"memories": [...]}} / {"result": {"resources": [...], "skills": [...]}}
      - {"result": {"hits": [...]}} / {"result": {"data": {"memories": [...]}}}
      - {"result": [...]}（result 直接是列表）
      - {"memories": [...]}
    语义检索结果分散在 memories / resources / skills 三段，需合并、去重后按相关度排序。
    """
    if not isinstance(result, dict):
        return []

    def _merge(raw):
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = []
            # 优先合并 memories / resources / skills 三段，补全 context_type
            for ctype, key in (("memory", "memories"), ("resource", "resources"), ("skill", "skills")):
                seg = raw.get(key)
                if isinstance(seg, list):
                    for h in seg:
                        if isinstance(h, dict) and not h.get("context_type"):
                            h = dict(h)
                            h["context_type"] = h.get("context_type") or ctype
                        items.append(h)
            # 若无三段，退回其它常见列表字段
            if not items:
                for key in ("hits", "items", "results", "memories"):
                    v = raw.get(key)
                    if isinstance(v, list):
                        items = v
                        break
        else:
            return []
        # 按 uri 去重 + 按分数降序
        seen = set()
        merged = []
        for h in items:
            if not isinstance(h, dict):
                continue
            uri = h.get("uri", "")
            if uri:
                if uri in seen:
                    continue
                seen.add(uri)
            merged.append(h)
        merged.sort(key=lambda x: x.get("score", 0), reverse=True)
        return merged

    raw = result.get("result")
    if isinstance(raw, list):
        return _merge(raw)
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, dict) and any(isinstance(data.get(k), list)
                                          for k in ("memories", "resources", "skills", "hits", "items", "results")):
            return _merge(data)
        return _merge(raw)
    if isinstance(result.get("memories"), list) or isinstance(result.get("hits"), list):
        return _merge(result)
    return []


def openviking_find(query: str, score_threshold: float = None, limit: int = None, target_uri: str = "") -> str:
    """在 OpenViking 记忆中语义搜索（find 接口：纯向量相似度、无会话上下文、低延迟）。

    阈值/条数/范围由 LLM 调用时自行判断传入：
    - score_threshold: 0~1，默认 0.4（阈值越高要求越相关）
    - limit: 0~10，默认 3（返回条数）
    - target_uri: 可选，限定检索范围（如 viking://user/memories/、viking://resources/my-project/）
    超出允许范围会自动收敛。
    """
    threshold = float(score_threshold) if score_threshold is not None else config.OV_FIND_THRESHOLD
    threshold = max(0.0, min(1.0, threshold))
    n = int(limit) if limit is not None else config.OV_FIND_LIMIT
    n = max(0, min(10, n))
    try:
        result = _ov_post("/api/v1/search/find", _find_payload(query, threshold, n, target_uri))
        if not isinstance(result, dict):
            return error(f"搜索记忆失败 - 响应格式异常: {str(result)[:300]}", code="internal")
        raw = result.get("result")
        if is_error(result):
            return json.dumps(result, ensure_ascii=False)

        mems = _extract_memories(result)
        # 兜底：阈值过高会吞掉相关记忆。若按给定阈值命中过少（0 条，或阈值偏高 ≥0.3 却仅 1 条），
        # 放宽阈值到 0 再试一次，取结果更多的一次。
        if threshold > 0 and (len(mems) == 0 or (len(mems) <= 1 and threshold >= 0.3)):
            fallback = _ov_post("/api/v1/search/find", _find_payload(query, 0.0, n, target_uri))
            if not isinstance(fallback, dict) or is_error(fallback):
                fb = _extract_memories(fallback)
                if len(fb) > len(mems):
                    mems = fb
        hits = mems[:n]
        out = {
            "query": query,
            "target_uri": target_uri,
            "score_threshold": threshold,
            "limit": n,
            "count": len(hits),
            "total": raw.get("total", len(hits)) if isinstance(raw, dict) else len(hits),
            "results": hits,
        }
        if not hits:
            out["message"] = "未找到相关记忆"
            # 诊断：后端有响应但未能解析出记忆时，回传原始结构以便排查
            out["debug_raw"] = result
        return ok(out)
    except Exception as e:
        return error(f"搜索记忆失败 - {str(e)}", code="internal")


def openviking_search(query: str, score_threshold: float = None, limit: int = None) -> str:
    """在 OpenViking 记忆中语义搜索（search 接口：上下文感知，结合会话语境提升召回）。

    阈值/条数由 LLM 调用时自行判断传入：
    - score_threshold: 0~1，默认 0.4（阈值越高要求越相关）
    - limit: 0~10，默认 3（返回条数）
    超出允许范围会自动收敛。
    """
    threshold = float(score_threshold) if score_threshold is not None else config.OV_SEARCH_THRESHOLD
    threshold = max(0.0, min(1.0, threshold))
    n = int(limit) if limit is not None else config.OV_SEARCH_LIMIT
    n = max(0, min(10, n))
    try:
        result = _ov_post("/api/v1/search/search", _search_payload(query, threshold, n))
        if not isinstance(result, dict):
            return error(f"搜索记忆失败 - 响应格式异常: {str(result)[:300]}", code="internal")
        raw = result.get("result")
        if is_error(result):
            return json.dumps(result, ensure_ascii=False)

        mems = _extract_memories(result)
        # 兜底：阈值过高会吞掉相关记忆。若按给定阈值命中过少（0 条，或阈值偏高 ≥0.3 却仅 1 条），
        # 放宽阈值到 0 再试一次，取结果更多的一次。
        if threshold > 0 and (len(mems) == 0 or (len(mems) <= 1 and threshold >= 0.3)):
            fallback = _ov_post("/api/v1/search/search", _search_payload(query, 0.0, n))
            if not isinstance(fallback, dict) or is_error(fallback):
                fb = _extract_memories(fallback)
                if len(fb) > len(mems):
                    mems = fb
        hits = mems[:n]
        out = {
            "query": query,
            "score_threshold": threshold,
            "limit": n,
            "count": len(hits),
            "total": raw.get("total", len(hits)) if isinstance(raw, dict) else len(hits),
            "results": hits,
        }
        if not hits:
            out["message"] = "未找到相关记忆"
            # 诊断：后端有响应但未能解析出记忆时，回传原始结构以便排查
            out["debug_raw"] = result
        return ok(out)
    except Exception as e:
        return error(f"搜索记忆失败 - {str(e)}", code="internal")


def openviking_remember(category: str, name: str, content: str) -> str:
    """将信息保存到 OpenViking 记忆"""
    if category not in ("preferences", "entities", "events", "experiences"):
        return error(
            f"未知分类: {category}，可选: preferences/entities/events/experiences",
            code="bad_category",
        )
    uri = _active_memory_base() + f"{category}/{name}.md"

    try:
        # 递归创建父目录（逐级 mkdir，忽略已存在的错误）
        _ensure_parent_dirs(uri)

        # 写入内容：先试 replace（文件已存在），失败再试 create（新建）
        write_result = _ov_post(
            "/api/v1/content/write",
            {"uri": uri, "content": content, "mode": "replace", "wait": True},
            timeout=30,
        )
        if is_error(write_result):
            err_text = str(write_result.get("error", ""))
            if write_result.get("code") == "not_found" or "NOT_FOUND" in err_text or "not found" in err_text.lower():
                write_result = _ov_post(
                    "/api/v1/content/write",
                    {"uri": uri, "content": content, "mode": "create", "wait": True},
                    timeout=30,
                )
        if is_error(write_result):
            return json.dumps(write_result, ensure_ascii=False)

        # 透传 OpenViking 规范信封，并补全我们已知的分类/名称
        if isinstance(write_result, dict):
            res = write_result.get("result")
            if isinstance(res, dict):
                res.setdefault("category", category)
                res.setdefault("name", name)
            return json.dumps(write_result, ensure_ascii=False)
        return ok({"uri": uri, "category": category, "name": name})
    except Exception as e:
        return error(f"保存记忆失败 - {str(e)}", code="internal")


def openviking_read(uri: str, name: str = "", base_uri: str = "") -> str:
    """读取 OpenViking 文件内容

    支持三种调用方式：
    - 单个 URI 字符串 → 直接返回文件内容
    - URI 列表（数组）   → 逐个读取并聚合返回（多文件读取）
    - 仅给文件名 name    → 先按名解析出唯一 URI（见 openviking_resolve_name）再读取；
                         可用 base_uri 指定解析根目录（默认探测 peer / 用户级记忆目录）
    """
    if isinstance(uri, list):
        return _aggregate_read(uri)
    if not uri and name:
        uri, resp = _resolve_single(name, base_uri)
        if resp is not None:
            return resp
    try:
        result = _ov_get("/api/v1/content/read", params={"uri": uri})
        if is_error(result):
            return json.dumps(result, ensure_ascii=False)
        # OpenViking 读返回 {"status":"ok","result":"<内容>"}，原样透传
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return error(f"读取失败 - {str(e)}", code="internal")


def _aggregate_read(uris: list) -> str:
    """批量读取多个 OpenViking 文件：逐个调用单文件接口聚合返回"""
    if not uris:
        return error("uris 列表为空", code="bad_request")
    results = []
    for uri in uris:
        try:
            r = _ov_get("/api/v1/content/read", params={"uri": uri})
            if is_error(r):
                results.append({"uri": uri, "success": False, "error": r.get("error", "未知错误")})
            elif isinstance(r, dict) and "result" in r:
                results.append({"uri": uri, "success": True, "content": r.get("result", "")})
            else:
                results.append({"uri": uri, "success": False, "error": "未知响应结构"})
        except Exception as e:
            results.append({"uri": uri, "success": False, "error": f"读取失败 - {str(e)}"})
    return ok({"count": len(results), "results": results})


def openviking_list_dir(uri: str, recursive: bool = False) -> str:
    """列出 OpenViking 目录内容"""
    try:
        params = {"uri": uri}
        if recursive:
            params["recursive"] = "true"
        result = _ov_get("/api/v1/fs/tree", params=params)
        if is_error(result):
            return json.dumps(result, ensure_ascii=False)
        return passthrough(result)
    except Exception as e:
        return error(f"列出目录失败 - {str(e)}", code="internal")


def openviking_write_file(uri: str, content: str, mode: str = "replace", name: str = "", base_uri: str = "") -> str:
    """写入内容到 OpenViking 文件（create/replace/append）

    仅给文件名 name（不给 uri）时，先按名解析：
    - 唯一命中 → 写该文件；
    - mode=create 且未命中 → 在 base_uri（默认记忆根目录）下按 name 新建（自动补 .md、建父目录）；
    - 其余未命中/多命中 → 返回 not_found / ambiguous 信封，不落盘。
    base_uri 可指定解析（及新建）的根目录，默认探测 peer / 用户级记忆目录。
    """
    if not uri and name:
        uri, resp = _resolve_for_write(name, mode, base_uri)
        if resp is not None:
            return resp
    try:
        payload = {"uri": uri, "content": content, "mode": mode}
        if mode == "create":
            payload["wait"] = False
        result = _ov_post("/api/v1/content/write", payload, timeout=30)
        if is_error(result):
            return json.dumps(result, ensure_ascii=False)
        return passthrough(result)
    except Exception as e:
        return error(f"写入失败 - {str(e)}", code="internal")


def openviking_forget(uri: str, recursive: bool = False, name: str = "", base_uri: str = "") -> str:
    """从 OpenViking 删除（遗忘）文件或目录。

    对齐 MCP forget：recursive=True 时递归删除目录及其所有子项。
    仅给文件名 name（不给 uri）时先按名解析，且要求唯一命中（删除不接受歧义）；
    base_uri 可指定解析根目录（默认探测 peer / 用户级记忆目录）。
    注意：此操作不可撤销。
    """
    if not uri and name:
        uri, resp = _resolve_single(name, base_uri)
        if resp is not None:
            return resp
    if not uri or not uri.strip():
        return error("缺少 uri 参数，请提供要删除的文件/目录 URI", code="bad_request")
    try:
        params = {"uri": uri}
        if recursive:
            params["recursive"] = "true"
        result = _ov_delete("/api/v1/fs", params=params)
        if is_error(result):
            return json.dumps(result, ensure_ascii=False)
        return passthrough(result)
    except Exception as e:
        return error(f"删除失败 - {str(e)}", code="internal")


# ============= Session 管理 =============
def openviking_create_session(session_id: str = "") -> str:
    """创建 OpenViking Session"""
    payload = {}
    if session_id:
        payload["session_id"] = session_id
    try:
        result = _ov_post("/api/v1/sessions", payload, timeout=15)
        if is_error(result):
            return json.dumps(result, ensure_ascii=False)
        return passthrough(result)
    except Exception as e:
        return error(f"创建 session 失败 - {str(e)}", code="internal")


def openviking_add_message(session_id: str, role: str, content: str, peer_id: str = "") -> str:
    """向 OpenViking Session 添加单条消息"""
    if not session_id:
        return error("缺少 session_id 参数，请先创建 session 再添加消息", code="bad_request")
    payload = {"role": role, "content": content}
    if peer_id:
        payload["peer_id"] = peer_id
    try:
        result = _ov_post(f"/api/v1/sessions/{session_id}/messages", payload, timeout=15)
        if is_error(result):
            return json.dumps(result, ensure_ascii=False)
        return passthrough(result)
    except Exception as e:
        return error(f"添加消息失败 - {str(e)}", code="internal")


def openviking_add_messages_batch(session_id: str, messages: list) -> str:
    """批量向 OpenViking Session 添加消息（最多 100 条）"""
    if not session_id:
        return error("缺少 session_id 参数，请先创建 session 再批量添加消息", code="bad_request")
    if not messages:
        return error("messages 列表为空，请提供要添加的消息", code="bad_request")
    try:
        result = _ov_post(f"/api/v1/sessions/{session_id}/messages/batch", {"messages": messages}, timeout=15)
        if is_error(result):
            return json.dumps(result, ensure_ascii=False)
        return passthrough(result)
    except Exception as e:
        return error(f"批量添加消息失败 - {str(e)}", code="internal")


def openviking_commit_session(session_id: str, keep_recent_count: int = 0) -> str:
    """提交/归档 OpenViking Session，触发记忆提取"""
    if not session_id:
        return error("缺少 session_id 参数，请先创建 session 再提交", code="bad_request")
    try:
        result = _ov_post(f"/api/v1/sessions/{session_id}/commit", {"keep_recent_count": keep_recent_count}, timeout=30)
        if is_error(result):
            return json.dumps(result, ensure_ascii=False)
        return passthrough(result)
    except Exception as e:
        return error(f"提交 session 失败 - {str(e)}", code="internal")


def openviking_get_session(session_id: str) -> str:
    """获取 OpenViking Session 详情"""
    if not session_id:
        return error("缺少 session_id 参数，请先创建 session 再查询", code="bad_request")
    try:
        result = _ov_get(f"/api/v1/sessions/{session_id}", timeout=15)
        if is_error(result):
            return json.dumps(result, ensure_ascii=False)
        return passthrough(result)
    except Exception as e:
        return error(f"获取 session 失败 - {str(e)}", code="internal")


def openviking_list_sessions() -> str:
    """列出 OpenViking 所有 Session"""
    try:
        result = _ov_get("/api/v1/sessions", timeout=15)
        if is_error(result):
            return json.dumps(result, ensure_ascii=False)
        return passthrough(result)
    except Exception as e:
        return error(f"列出 session 失败 - {str(e)}", code="internal")
