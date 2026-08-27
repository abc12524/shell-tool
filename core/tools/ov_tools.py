#!/usr/bin/env python3
"""OpenViking 外置记忆工具：搜索 / 读写 / 记住 / Session 管理

所有工具统一返回 DSH/OpenViking 规范信封：
  成功 → {"status": "ok",  "result": {...}}
  失败 → {"status": "error", "error": "...", "code": "..."}
底层 OpenViking 后端自身也采用同一信封，故 read/list_dir/write/session 等
直接透传其后端响应；remember/search 在透传基础上补全语义字段。
"""
import json
import os

import requests

from .. import config
from .envelope import ok, error, is_error, passthrough


# ============= 基础请求 =============
def _ov_base():
    """延迟读取 OpenViking URL，避免模块级别求值导致 .env 未加载的问题"""
    return os.environ.get('OPENVIKING_URL', '')


def _ov_headers():
    """获取 OpenViking API 请求头"""
    key = os.environ.get('OPENVIKING_KEY', '')
    if not key:
        raise ValueError("OPENVIKING_KEY 未设置")
    user = os.environ.get('OPENVIKING_USER', '')
    agent = os.environ.get('OPENVIKING_AGENT', 'default')
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-OpenViking-Account": "default",
        "X-OpenViking-User": f"{user}",
        "X-OpenViking-Peer": agent,
    }


def _ov_get(path, params=None, timeout=15):
    """OpenViking GET 请求；失败返回错误信封 dict（不抛异常）"""
    try:
        r = requests.get(f"{_ov_base()}{path}", headers=_ov_headers(),
                         params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        return error("OpenViking 请求超时", code="timeout")
    except requests.HTTPError as e:
        return error(f"OpenViking HTTP 错误 - {e}", code="http")
    except Exception as e:
        return error(f"OpenViking 请求失败 - {str(e)}", code="transport")


def _ov_post(path, payload, timeout=15):
    """OpenViking POST 请求；失败返回错误信封 dict（不抛异常）"""
    try:
        r = requests.post(f"{_ov_base()}{path}", headers=_ov_headers(),
                           json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        return error("OpenViking 请求超时", code="timeout")
    except requests.HTTPError as e:
        return error(f"OpenViking HTTP 错误 - {e}", code="http")
    except Exception as e:
        return error(f"OpenViking 请求失败 - {str(e)}", code="transport")


# ============= 记忆 =============
def openviking_search(query: str, score_threshold: float = None, limit: int = None) -> str:
    """在 OpenViking 记忆中语义搜索。

    阈值/条数由 LLM 调用时自行判断传入：
    - score_threshold: 0~1，默认 0.35（阈值越高要求越相关）
    - limit: 0~10，默认 3（返回条数）
    超出允许范围会自动收敛。
    """
    threshold = float(score_threshold) if score_threshold is not None else config.OV_SCORE_THRESHOLD
    threshold = max(0.0, min(1.0, threshold))
    n = int(limit) if limit is not None else config.OV_SEARCH_LIMIT
    n = max(0, min(10, n))
    try:
        result = _ov_post("/api/v1/search/search", {
            "query": query,
            "score_threshold": threshold,
            "limit": n,
        })
        if is_error(result):
            return json.dumps(result, ensure_ascii=False)

        raw = result.get("result") if isinstance(result, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        # 语义检索结果分散在 memories / resources / skills 三段，需合并后按相关度排序
        groups = [
            ("memory", raw.get("memories")),
            ("resource", raw.get("resources")),
            ("skill", raw.get("skills")),
        ]
        hits = []
        seen = set()
        for default_ctype, items in groups:
            for h in items or []:
                if not isinstance(h, dict):
                    continue
                uri = h.get("uri", "")
                if uri:
                    if uri in seen:
                        continue
                    seen.add(uri)
                hits.append({
                    "uri": uri,
                    "score": h.get("score", 0),
                    "abstract": h.get("abstract", ""),
                    "category": h.get("category", ""),
                    "context_type": h.get("context_type") or default_ctype,
                    "level": h.get("level", ""),
                })
        hits.sort(key=lambda x: x["score"], reverse=True)
        hits = hits[:n]
        out = {
            "query": query,
            "score_threshold": threshold,
            "limit": n,
            "count": len(hits),
            "total": raw.get("total", len(hits)),
            "results": hits,
        }
        if not hits:
            out["message"] = "未找到相关记忆"
        return ok(out)
    except Exception as e:
        return error(f"搜索记忆失败 - {str(e)}", code="internal")


def openviking_remember(category: str, name: str, content: str) -> str:
    """将信息保存到 OpenViking 记忆"""
    user = os.environ.get('OPENVIKING_USER', '')
    agent = os.environ.get('OPENVIKING_AGENT', 'default')
    path_map = {
        "preferences": f"viking://user/{user}/peers/{agent}/memories/preferences/{name}.md",
        "entities":    f"viking://user/{user}/peers/{agent}/memories/entities/{name}.md",
        "events":      f"viking://user/{user}/peers/{agent}/memories/events/{name}.md",
        "experiences": f"viking://user/{user}/peers/{agent}/memories/experiences/{name}.md",
    }
    uri = path_map.get(category)
    if not uri:
        return error(
            f"未知分类: {category}，可选: preferences/entities/events/experiences",
            code="bad_category",
        )

    try:
        # 递归创建父目录（逐级 mkdir，忽略已存在的错误）
        parts = uri.split("/")
        for i in range(6, len(parts)):
            parent = "/".join(parts[:i]) + "/"
            _ov_post("/api/v1/fs/mkdir", {"uri": parent}, timeout=5)

        # 写入内容：先试 replace（文件已存在），失败再试 create（新建）
        write_result = _ov_post(
            "/api/v1/content/write",
            {"uri": uri, "content": content, "mode": "replace", "wait": True},
            timeout=30,
        )
        if is_error(write_result):
            err_text = write_result.get("error", "")
            if "NOT_FOUND" in err_text or "not found" in err_text.lower():
                write_result = _ov_post(
                    "/api/v1/content/write",
                    {"uri": uri, "content": content, "mode": "create", "wait": False},
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


def openviking_read(uri: str) -> str:
    """读取 OpenViking 文件内容

    支持单个 URI 字符串或 URI 列表（数组）：
    - 单个字符串 → 直接返回文件内容
    - 列表 → 逐个读取并聚合返回（多文件读取）
    """
    if isinstance(uri, list):
        return _aggregate_read(uri)
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


def openviking_write_file(uri: str, content: str, mode: str = "replace") -> str:
    """写入内容到 OpenViking 文件（create/replace/append）"""
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


def openviking_load_context(query: str) -> str:
    """在对话前自动搜索相关记忆，返回可注入 prompt 的上下文字符串"""
    try:
        result = _ov_post("/api/v1/search/search", {
            "query": query,
            "score_threshold": config.OV_SCORE_THRESHOLD,
            "limit": config.OV_INJECT_LIMIT,
        })
        raw = result.get("result", {}) if isinstance(result, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        # memories / resources / skills 三段的检索结果合并（同类去重），保留相关度
        merged = []
        seen = set()
        for default_ctype, items in (
            ("memory", raw.get("memories")),
            ("resource", raw.get("resources")),
            ("skill", raw.get("skills")),
        ):
            for h in items or []:
                if not isinstance(h, dict):
                    continue
                uri = h.get("uri", "")
                if uri:
                    if uri in seen:
                        continue
                    seen.add(uri)
                merged.append((h.get("score", 0), uri,
                               h.get("abstract", ""),
                               h.get("context_type") or default_ctype))
        merged.sort(key=lambda x: x[0], reverse=True)
        hits = merged[:config.OV_INJECT_LIMIT]
        if not hits:
            return ""
        ctx_parts = ["## 📖 相关记忆"]
        for score, uri, abstract, ctype in hits:
            if abstract:
                ctx_parts.append(f"- [{uri}] (score={score:.2f}, {ctype})\n  {abstract[:300]}")
        return "\n".join(ctx_parts)
    except Exception:
        return ""


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
