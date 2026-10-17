#!/usr/bin/env python3
"""OpenViking 外置记忆工具实现：search/find 语义搜索 / 读写 / 记住 / Session 管理

由 shell-tool 的 core/tools/ov_tools.py 抽取并去掉对 core.config 的依赖，改为直接读
环境变量（或同目录 config.json 注入的环境变量），使其可作为独立可下载 skill 运行。

内部统一使用 dict 信封：{"status":"ok","result":...} / {"status":"error","error":...,"code":...}
对外由入口 skill.py 调 _emit() 转成运行时原生信封（shell-tool=JSON 字符串 / 移动端=dict）。
底层 OpenViking 后端自身也采用同一信封，故 read/list_dir/write/session 等直接透传。
"""
import hashlib
import json
import os
import re

import requests


# ============================ 信封兼容 ============================
# 优先移动端 _envelope（返回 dict），其次 shell-tool core.tools.envelope（返回 JSON 字符串），
# 两者都不存在时用内置实现 —— 这样本技能可完全脱离 shell-tool 独立运行。
def _builtin_ok(result=None):
    return json.dumps({"status": "ok", "result": {} if result is None else result},
                      ensure_ascii=False)


def _builtin_error(message, code=None, **extra):
    payload = {"status": "error", "error": str(message)}
    if code is not None:
        payload["code"] = code
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return json.dumps(payload, ensure_ascii=False)


try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py（ok/error 返回 dict）
    import _envelope as _native

    def _native_ok(result=None):
        return _native.ok(result if result is not None else {})

    def _native_error(message, code=None, **extra):
        return _native.error(message, code=code)  # 移动端信封不接受额外字段
except ImportError:
    try:  # shell-tool：core.tools.envelope（ok/error 返回 JSON 字符串）
        from core.tools.envelope import ok as _native_ok, error as _native_error
    except ImportError:  # 独立运行：内置 DSH/OpenViking 规范信封
        _native_ok, _native_error = _builtin_ok, _builtin_error


# ============================ 配置 ============================
def _load_config():
    """把同目录 config.json 注入环境变量（不覆盖已有 env），照常 os.environ 读取。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return
    for k, v in cfg.items():
        if v and not os.environ.get(k):
            os.environ[k] = str(v)


_load_config()


def _conf_f(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _conf_i(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return int(default)
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(default)


def _conf_b(name, default):
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    return v.strip().lower() in ("1", "true", "yes", "on")


# ============================ 信封 ============================
def _ok(result=None):
    return {"status": "ok", "result": {} if result is None else result}


def _error(message, code=None, **extra):
    payload = {"status": "error", "error": str(message)}
    if code is not None:
        payload["code"] = code
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return payload


def _is_error(value):
    return isinstance(value, dict) and value.get("status") == "error"


def _passthrough(result):
    if isinstance(result, dict):
        return result
    return _error("OpenViking 返回了非预期的数据格式", code="bad_response")


def _emit(env):
    """把内部 dict 信封转成运行时信封：shell-tool/独立 → JSON 字符串，移动端 → dict。"""
    if not isinstance(env, dict):
        env = _ok(env)
    if env.get("status") == "ok":
        return _native_ok(env.get("result", {}))
    extra = {k: v for k, v in env.items() if k not in ("status", "error", "code")}
    return _native_error(env.get("error", ""), code=env.get("code"), **extra)


# ============================ 基础请求 ============================
def _ov_base():
    """延迟读取 OpenViking URL，避免模块级别求值导致 .env 未加载的问题"""
    return os.environ.get('OPENVIKING_URL', '')


def openviking_peer_id():
    """解析当前 actor peer：显式 OV_PEER_ID > 按工作目录派生(ws-*) > 回退 OPENVIKING_AGENT。"""
    explicit = os.environ.get('OV_PEER_ID') or ''
    if explicit:
        return explicit
    if _conf_b('OV_WORKSPACE_PEER', False):
        root = os.environ.get('OV_PROJECT_ROOT') or os.getcwd()
        digest = hashlib.md5(root.encode('utf-8')).hexdigest()[:12]
        return f"ws-{digest}"
    return os.environ.get('OPENVIKING_AGENT', 'default')


def _hdr(v):
    """HTTP 头值必须是 latin-1 可编码；非 ASCII（如中文）做 RFC2047 编码。"""
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
    return _error(msg, code=code)


def _ov_get(path, params=None, timeout=15):
    """OpenViking GET 请求；失败返回错误信封 dict（不抛异常）"""
    try:
        r = requests.get(f"{_ov_base()}{path}", headers=_ov_headers(),
                         params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        return _error("OpenViking 请求超时", code="timeout")
    except requests.HTTPError as e:
        return _http_failure(e)
    except Exception as e:
        return _error(f"OpenViking 请求失败 - {_sanitize(e)}", code="transport")


def _ov_post(path, payload, timeout=15):
    """OpenViking POST 请求；失败返回错误信封 dict（不抛异常）"""
    try:
        r = requests.post(f"{_ov_base()}{path}", headers=_ov_headers(),
                          json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        return _error("OpenViking 请求超时", code="timeout")
    except requests.HTTPError as e:
        return _http_failure(e)
    except Exception as e:
        return _error(f"OpenViking 请求失败 - {_sanitize(e)}", code="transport")


def _ov_delete(path, params=None, timeout=15):
    """OpenViking DELETE 请求；失败返回错误信封 dict（不抛异常）"""
    try:
        r = requests.delete(f"{_ov_base()}{path}", headers=_ov_headers(),
                            params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        return _error("OpenViking 请求超时", code="timeout")
    except requests.HTTPError as e:
        return _http_failure(e)
    except Exception as e:
        return _error(f"OpenViking 请求失败 - {_sanitize(e)}", code="transport")


def _find_payload(query, score_threshold=None, limit=None, target_uri=""):
    payload = {
        "query": query,
        "score_threshold": score_threshold if score_threshold is not None else _conf_f("OV_FIND_THRESHOLD", 0.4),
        "limit": limit if limit is not None else _conf_i("OV_FIND_LIMIT", 3),
    }
    if target_uri:
        payload["target_uri"] = target_uri
    return payload


def _search_payload(query, score_threshold=None, limit=None):
    """上下文感知搜索（/api/v1/search/search）的请求体构造（peer 隔离只走请求头）。"""
    return {
        "query": query,
        "score_threshold": score_threshold if score_threshold is not None else _conf_f("OV_SEARCH_THRESHOLD", 0.4),
        "limit": limit if limit is not None else _conf_i("OV_SEARCH_LIMIT", 3),
    }


# ============================ 按文件名解析 URI ============================
def _memory_base(category: str = "") -> str:
    """当前 peer 的隔离记忆目录根：viking://user/<user>/peers/<agent>/memories/[_<category>/]"""
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
    """按名检索时依次尝试的根目录：peer 记忆 → 用户级记忆 → 公共资源库。"""
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


def openviking_resolve_name(name: str, base_uri: str = "", limit: int = 20):
    """按文件名/片段解析出记忆库中的完整 viking:// URI（dict 信封）。"""
    name = (name or "").strip()
    if not name:
        return _error("缺少文件名 name", code="bad_request")
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
            return _error(f"解析文件名失败 - {_sanitize(e)}", code="internal")
        if _is_error(result):
            return result
        found = _extract_glob_matches(result)
        if found:
            matched_base, matches = base, found
            break
    return _ok({
        "name": name,
        "base_uri": matched_base,
        "searched_bases": bases,
        "pattern": pattern,
        "count": len(matches),
        "matches": matches,
    })


def _resolve_single(name: str, base_uri: str = ""):
    """把 name 解析为唯一 URI。返回 (uri, response)。

    唯一命中 → (uri, None)；未命中/多命中 → ("", 错误信封 dict)。
    """
    data = openviking_resolve_name(name, base_uri)
    if not isinstance(data, dict) or _is_error(data):
        return "", data
    res = data.get("result") or {}
    matches = res.get("matches") or []
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return "", _error(f"未找到匹配文件: {name}", code="not_found",
                          name=name, base_uri=res.get("base_uri", ""),
                          searched_bases=res.get("searched_bases"))
    return "", _error(
        f"文件名 '{name}' 匹配到 {len(matches)} 个文件，请改用完整 uri 或更精确的名字",
        code="ambiguous", name=name, matches=matches)


def _active_memory_base():
    """默认记忆根（读/写统一落点），使各入口落在同一处。"""
    if os.environ.get('OV_PEER_ID') or _conf_b('OV_WORKSPACE_PEER', False):
        return _memory_base()
    user = os.environ.get('OPENVIKING_USER', '')
    candidates = [_memory_base(), f"viking://user/{user}/memories/"]
    first_existing = None
    for base in candidates:
        stat = _ov_get("/api/v1/fs/stat", params={"uri": base}, timeout=10)
        if _is_error(stat):
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

    返回 (uri, response)；response 非 None 表示解析失败/歧义（dict 信封）。
    """
    uri, resp = _resolve_single(name, base_uri)
    if resp is None:
        return uri, None
    if isinstance(resp, dict) and resp.get("code") == "not_found" and mode == "create":
        rel = name.strip().lstrip("/")
        if not rel:
            return "", resp
        if "." not in rel.split("/")[-1]:
            rel += ".md"
        new_uri = (base_uri or _active_memory_base()) + rel
        _ensure_parent_dirs(new_uri)
        return new_uri, None
    return "", resp


# ============================ 记忆 ============================
def _extract_memories(result):
    """从 OpenViking 搜索响应中提取并归一化记忆列表（合并 memories/resources/skills 三段）。"""
    if not isinstance(result, dict):
        return []

    def _merge(raw):
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = []
            for ctype, key in (("memory", "memories"), ("resource", "resources"), ("skill", "skills")):
                seg = raw.get(key)
                if isinstance(seg, list):
                    for h in seg:
                        if isinstance(h, dict) and not h.get("context_type"):
                            h = dict(h)
                            h["context_type"] = h.get("context_type") or ctype
                        items.append(h)
            if not items:
                for key in ("hits", "items", "results", "memories"):
                    v = raw.get(key)
                    if isinstance(v, list):
                        items = v
                        break
        else:
            return []
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


def openviking_find(query: str, score_threshold: float = None, limit: int = None, target_uri: str = ""):
    """在 OpenViking 记忆中语义搜索（find 接口：纯向量、无会话上下文、低延迟）。"""
    threshold = float(score_threshold) if score_threshold is not None else _conf_f("OV_FIND_THRESHOLD", 0.4)
    threshold = max(0.0, min(1.0, threshold))
    n = int(limit) if limit is not None else _conf_i("OV_FIND_LIMIT", 3)
    n = max(0, min(10, n))
    try:
        result = _ov_post("/api/v1/search/find", _find_payload(query, threshold, n, target_uri))
        if not isinstance(result, dict):
            return _error(f"搜索记忆失败 - 响应格式异常: {str(result)[:300]}", code="internal")
        raw = result.get("result")
        if _is_error(result):
            return result

        mems = _extract_memories(result)
        if threshold > 0 and (len(mems) == 0 or (len(mems) <= 1 and threshold >= 0.3)):
            fallback = _ov_post("/api/v1/search/find", _find_payload(query, 0.0, n, target_uri))
            if not isinstance(fallback, dict) or _is_error(fallback):
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
            out["debug_raw"] = result
        return _ok(out)
    except Exception as e:
        return _error(f"搜索记忆失败 - {str(e)}", code="internal")


def openviking_search(query: str, score_threshold: float = None, limit: int = None):
    """在 OpenViking 记忆中语义搜索（search 接口：上下文感知，结合会话语境提升召回）。"""
    threshold = float(score_threshold) if score_threshold is not None else _conf_f("OV_SEARCH_THRESHOLD", 0.4)
    threshold = max(0.0, min(1.0, threshold))
    n = int(limit) if limit is not None else _conf_i("OV_SEARCH_LIMIT", 3)
    n = max(0, min(10, n))
    try:
        result = _ov_post("/api/v1/search/search", _search_payload(query, threshold, n))
        if not isinstance(result, dict):
            return _error(f"搜索记忆失败 - 响应格式异常: {str(result)[:300]}", code="internal")
        raw = result.get("result")
        if _is_error(result):
            return result

        mems = _extract_memories(result)
        if threshold > 0 and (len(mems) == 0 or (len(mems) <= 1 and threshold >= 0.3)):
            fallback = _ov_post("/api/v1/search/search", _search_payload(query, 0.0, n))
            if not isinstance(fallback, dict) or _is_error(fallback):
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
            out["debug_raw"] = result
        return _ok(out)
    except Exception as e:
        return _error(f"搜索记忆失败 - {str(e)}", code="internal")


def openviking_remember(category: str, name: str, content: str):
    """将信息保存到 OpenViking 记忆"""
    if category not in ("preferences", "entities", "events", "experiences"):
        return _error(
            f"未知分类: {category}，可选: preferences/entities/events/experiences",
            code="bad_category",
        )
    uri = _active_memory_base() + f"{category}/{name}.md"

    try:
        _ensure_parent_dirs(uri)

        write_result = _ov_post(
            "/api/v1/content/write",
            {"uri": uri, "content": content, "mode": "replace", "wait": True},
            timeout=30,
        )
        if _is_error(write_result):
            err_text = str(write_result.get("error", ""))
            if write_result.get("code") == "not_found" or "NOT_FOUND" in err_text or "not found" in err_text.lower():
                write_result = _ov_post(
                    "/api/v1/content/write",
                    {"uri": uri, "content": content, "mode": "create", "wait": True},
                    timeout=30,
                )
        if _is_error(write_result):
            return write_result

        if isinstance(write_result, dict):
            res = write_result.get("result")
            if isinstance(res, dict):
                res.setdefault("category", category)
                res.setdefault("name", name)
            return write_result
        return _ok({"uri": uri, "category": category, "name": name})
    except Exception as e:
        return _error(f"保存记忆失败 - {str(e)}", code="internal")


def openviking_read(uri, name: str = "", base_uri: str = ""):
    """读取 OpenViking 文件内容（支持单 uri / uri 列表 / 仅给 name 解析）。"""
    if isinstance(uri, list):
        return _aggregate_read(uri)
    if not uri and name:
        uri, resp = _resolve_single(name, base_uri)
        if resp is not None:
            return resp
    try:
        result = _ov_get("/api/v1/content/read", params={"uri": uri})
        if _is_error(result):
            return result
        return result
    except Exception as e:
        return _error(f"读取失败 - {str(e)}", code="internal")


def _aggregate_read(uris: list):
    """批量读取多个 OpenViking 文件：逐个调用单文件接口聚合返回"""
    if not uris:
        return _error("uris 列表为空", code="bad_request")
    results = []
    for uri in uris:
        try:
            r = _ov_get("/api/v1/content/read", params={"uri": uri})
            if _is_error(r):
                results.append({"uri": uri, "success": False, "error": r.get("error", "未知错误")})
            elif isinstance(r, dict) and "result" in r:
                results.append({"uri": uri, "success": True, "content": r.get("result", "")})
            else:
                results.append({"uri": uri, "success": False, "error": "未知响应结构"})
        except Exception as e:
            results.append({"uri": uri, "success": False, "error": f"读取失败 - {str(e)}"})
    return _ok({"count": len(results), "results": results})


def openviking_list_dir(uri: str, recursive: bool = False):
    """列出 OpenViking 目录内容"""
    try:
        params = {"uri": uri}
        if recursive:
            params["recursive"] = "true"
        result = _ov_get("/api/v1/fs/tree", params=params)
        if _is_error(result):
            return result
        return _passthrough(result)
    except Exception as e:
        return _error(f"列出目录失败 - {str(e)}", code="internal")


def openviking_write_file(uri: str, content: str, mode: str = "replace", name: str = "", base_uri: str = ""):
    """写入内容到 OpenViking 文件（create/replace/append）。"""
    if not uri and name:
        uri, resp = _resolve_for_write(name, mode, base_uri)
        if resp is not None:
            return resp
    try:
        payload = {"uri": uri, "content": content, "mode": mode}
        if mode == "create":
            payload["wait"] = False
        result = _ov_post("/api/v1/content/write", payload, timeout=30)
        if _is_error(result):
            return result
        return _passthrough(result)
    except Exception as e:
        return _error(f"写入失败 - {str(e)}", code="internal")


def openviking_forget(uri: str, recursive: bool = False, name: str = "", base_uri: str = ""):
    """从 OpenViking 删除（遗忘）文件或目录（不可撤销）。"""
    if not uri and name:
        uri, resp = _resolve_single(name, base_uri)
        if resp is not None:
            return resp
    if not uri or not uri.strip():
        return _error("缺少 uri 参数，请提供要删除的文件/目录 URI", code="bad_request")
    try:
        params = {"uri": uri}
        if recursive:
            params["recursive"] = "true"
        result = _ov_delete("/api/v1/fs", params=params)
        if _is_error(result):
            return result
        return _passthrough(result)
    except Exception as e:
        return _error(f"删除失败 - {str(e)}", code="internal")


# ============================ Session 管理 ============================
def openviking_create_session(session_id: str = ""):
    """创建 OpenViking Session"""
    payload = {}
    if session_id:
        payload["session_id"] = session_id
    try:
        result = _ov_post("/api/v1/sessions", payload, timeout=15)
        if _is_error(result):
            return result
        return _passthrough(result)
    except Exception as e:
        return _error(f"创建 session 失败 - {str(e)}", code="internal")


def openviking_add_message(session_id: str, role: str, content: str, peer_id: str = ""):
    """向 OpenViking Session 添加单条消息"""
    if not session_id:
        return _error("缺少 session_id 参数，请先创建 session 再添加消息", code="bad_request")
    payload = {"role": role, "content": content}
    if peer_id:
        payload["peer_id"] = peer_id
    try:
        result = _ov_post(f"/api/v1/sessions/{session_id}/messages", payload, timeout=15)
        if _is_error(result):
            return result
        return _passthrough(result)
    except Exception as e:
        return _error(f"添加消息失败 - {str(e)}", code="internal")


def openviking_add_messages_batch(session_id: str, messages: list):
    """批量向 OpenViking Session 添加消息（最多 100 条）"""
    if not session_id:
        return _error("缺少 session_id 参数，请先创建 session 再批量添加消息", code="bad_request")
    if not messages:
        return _error("messages 列表为空，请提供要添加的消息", code="bad_request")
    try:
        result = _ov_post(f"/api/v1/sessions/{session_id}/messages/batch", {"messages": messages}, timeout=15)
        if _is_error(result):
            return result
        return _passthrough(result)
    except Exception as e:
        return _error(f"批量添加消息失败 - {str(e)}", code="internal")


def openviking_commit_session(session_id: str, keep_recent_count: int = 0):
    """提交/归档 OpenViking Session，触发记忆提取"""
    if not session_id:
        return _error("缺少 session_id 参数，请先创建 session 再提交", code="bad_request")
    try:
        result = _ov_post(f"/api/v1/sessions/{session_id}/commit", {"keep_recent_count": keep_recent_count}, timeout=30)
        if _is_error(result):
            return result
        return _passthrough(result)
    except Exception as e:
        return _error(f"提交 session 失败 - {str(e)}", code="internal")


def openviking_get_session(session_id: str):
    """获取 OpenViking Session 详情"""
    if not session_id:
        return _error("缺少 session_id 参数，请先创建 session 再查询", code="bad_request")
    try:
        result = _ov_get(f"/api/v1/sessions/{session_id}", timeout=15)
        if _is_error(result):
            return result
        return _passthrough(result)
    except Exception as e:
        return _error(f"获取 session 失败 - {str(e)}", code="internal")


def openviking_list_sessions():
    """列出 OpenViking 所有 Session"""
    try:
        result = _ov_get("/api/v1/sessions", timeout=15)
        if _is_error(result):
            return result
        return _passthrough(result)
    except Exception as e:
        return _error(f"列出 session 失败 - {str(e)}", code="internal")
