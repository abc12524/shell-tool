#!/usr/bin/env python3
"""OpenViking 外置记忆工具：搜索 / 读写 / 记住 / Session 管理"""
import json
import os

import requests

# ============= 基础请求 =============
def _ov_base():
    """延迟读取 OpenViking URL，避免模块级别求值导致 .env 未加载的问题"""
    return os.environ.get('OPENVIKING_URL', '')


def _ov_headers():
    """获取 OpenViking API 请求头"""
    key = os.environ.get('OPENVIKING_KEY', '')
    user = os.environ.get('OPENVIKING_USER', '')
    if not key:
        raise ValueError("OPENVIKING_KEY 未设置")
    agent = os.environ.get('OPENVIKING_AGENT', 'default')
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-OpenViking-Account": "default",
        "X-OpenViking-User": f"{user}",
        "X-OpenViking-Peer": agent,
    }


def _ov_get(path, params=None, timeout=15):
    """OpenViking GET 请求"""
    try:
        r = requests.get(f"{_ov_base()}{path}", headers=_ov_headers(),
                         params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        return {"error": "OpenViking 请求超时"}
    except Exception as e:
        return {"error": f"OpenViking 请求失败 - {str(e)}"}


def _ov_post(path, payload, timeout=15):
    """OpenViking POST 请求"""
    try:
        r = requests.post(f"{_ov_base()}{path}", headers=_ov_headers(),
                          json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        return {"error": "OpenViking 请求超时"}
    except Exception as e:
        return {"error": f"OpenViking 请求失败 - {str(e)}"}


# ============= 记忆 =============
def openviking_search(query: str) -> str:
    """在 OpenViking 记忆中语义搜索"""
    try:
        result = _ov_post("/api/v1/search/search", {
            "query": query, "score_threshold": 0.30, "limit": 5
        })
        if "error" in result:
            return json.dumps(result, ensure_ascii=False)
        # 响应在 result.memories 中
        mems = result.get("result", {}).get("memories", [])
        hits = mems[:8]
        if not hits:
            return json.dumps({"success": True, "results": [], "message": "未找到相关记忆"}, ensure_ascii=False)
        out = {"success": True, "results": []}
        for h in hits:
            out["results"].append({
                "uri": h.get("uri", ""),
                "score": h.get("score", 0),
                "snippet": h.get("abstract", "")[:500],
                "category": h.get("category", "")
            })
        return json.dumps(out, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"搜索记忆失败 - {str(e)}"}, ensure_ascii=False)


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
        return json.dumps({"error": f"未知分类: {category}，可选: preferences/entities/events/experiences"}, ensure_ascii=False)

    try:
        # 递归创建父目录（逐级 mkdir，忽略已存在的错误）
        parts = uri.split("/")
        for i in range(6, len(parts)):
            parent = "/".join(parts[:i]) + "/"
            _ov_post("/api/v1/fs/mkdir", {"uri": parent}, timeout=5)
        # 写入内容：先试 replace（文件已存在），失败再试 create（新建）
        write_result = _ov_post("/api/v1/content/write", {"uri": uri, "content": content, "mode": "replace", "wait": True}, timeout=30)
        err = write_result.get("error", "")
        if "NOT_FOUND" in err or "not found" in err.lower():
            write_result = _ov_post("/api/v1/content/write", {"uri": uri, "content": content, "mode": "create", "wait": False}, timeout=30)
        if "error" in write_result:
            return json.dumps({"error": write_result["error"]}, ensure_ascii=False)
        return json.dumps({"success": True, "uri": uri}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"保存记忆失败 - {str(e)}"}, ensure_ascii=False)


def openviking_read(uri: str) -> str:
    """读取单个 OpenViking 文件内容"""
    try:
        result = _ov_get("/api/v1/content/read", params={"uri": uri})
        if "error" in result:
            return json.dumps(result, ensure_ascii=False)
        if "content" in result:
            return result["content"]
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"读取失败 - {str(e)}"}, ensure_ascii=False)


def openviking_multi_read(uris: list) -> str:
    """批量读取多个 OpenViking 文件"""
    try:
        result = _ov_post("/api/v1/multi_read", {"uris": uris}, timeout=30)
        if "error" in result:
            return json.dumps(result, ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"批量读取失败 - {str(e)}"}, ensure_ascii=False)


def openviking_list_dir(uri: str, recursive: bool = False) -> str:
    """列出 OpenViking 目录内容"""
    try:
        params = {"uri": uri}
        if recursive:
            params["recursive"] = "true"
        result = _ov_get("/api/v1/fs/tree", params=params)
        if "error" in result:
            return json.dumps(result, ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"列出目录失败 - {str(e)}"}, ensure_ascii=False)


def openviking_write_file(uri: str, content: str, mode: str = "replace") -> str:
    """写入内容到 OpenViking 文件（create/replace/append）"""
    try:
        payload = {"uri": uri, "content": content, "mode": mode}
        if mode == "create":
            payload["wait"] = False
        result = _ov_post("/api/v1/content/write", payload, timeout=30)
        if "error" in result:
            return json.dumps(result, ensure_ascii=False)
        return json.dumps({"success": True, "uri": uri, "mode": mode}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"写入失败 - {str(e)}"}, ensure_ascii=False)


def openviking_load_context(query: str) -> str:
    """在对话前自动搜索相关记忆，返回可注入 prompt 的上下文字符串"""
    try:
        result = _ov_post("/api/v1/search/search", {
            "query": query, "score_threshold": 0.30, "limit": 5
        })
        mems = result.get("result", {}).get("memories", [])
        hits = mems[:5]
        if not hits:
            return ""
        ctx_parts = ["## 📖 相关记忆"]
        for h in hits:
            uri = h.get("uri", "")
            abstract = h.get("abstract", "")
            score = h.get("score", 0)
            if abstract:
                ctx_parts.append(f"- [{uri}] (score={score:.2f})\n  {abstract[:300]}")
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
        if "error" in result:
            return json.dumps(result, ensure_ascii=False)
        return json.dumps(result.get("result", result), ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"创建 session 失败 - {str(e)}"}, ensure_ascii=False)


def openviking_add_message(session_id: str, role: str, content: str, peer_id: str = "") -> str:
    """向 OpenViking Session 添加单条消息"""
    if not session_id:
        return json.dumps({"error": "缺少 session_id 参数，请先创建 session 再添加消息"}, ensure_ascii=False)
    payload = {"role": role, "content": content}
    if peer_id:
        payload["peer_id"] = peer_id
    try:
        result = _ov_post(f"/api/v1/sessions/{session_id}/messages", payload, timeout=15)
        if "error" in result:
            return json.dumps(result, ensure_ascii=False)
        return json.dumps(result.get("result", result), ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"添加消息失败 - {str(e)}"}, ensure_ascii=False)


def openviking_add_messages_batch(session_id: str, messages: list) -> str:
    """批量向 OpenViking Session 添加消息（最多 100 条）"""
    if not session_id:
        return json.dumps({"error": "缺少 session_id 参数，请先创建 session 再批量添加消息"}, ensure_ascii=False)
    if not messages:
        return json.dumps({"error": "messages 列表为空，请提供要添加的消息"}, ensure_ascii=False)
    try:
        result = _ov_post(f"/api/v1/sessions/{session_id}/messages/batch", {"messages": messages}, timeout=15)
        if "error" in result:
            return json.dumps(result, ensure_ascii=False)
        return json.dumps(result.get("result", result), ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"批量添加消息失败 - {str(e)}"}, ensure_ascii=False)


def openviking_commit_session(session_id: str, keep_recent_count: int = 0) -> str:
    """提交/归档 OpenViking Session，触发记忆提取"""
    if not session_id:
        return json.dumps({"error": "缺少 session_id 参数，请先创建 session 再提交"}, ensure_ascii=False)
    try:
        result = _ov_post(f"/api/v1/sessions/{session_id}/commit", {"keep_recent_count": keep_recent_count}, timeout=30)
        if "error" in result:
            return json.dumps(result, ensure_ascii=False)
        return json.dumps(result.get("result", result), ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"提交 session 失败 - {str(e)}"}, ensure_ascii=False)


def openviking_get_session(session_id: str) -> str:
    """获取 OpenViking Session 详情"""
    if not session_id:
        return json.dumps({"error": "缺少 session_id 参数，请先创建 session 再查询"}, ensure_ascii=False)
    try:
        result = _ov_get(f"/api/v1/sessions/{session_id}", timeout=15)
        if "error" in result:
            return json.dumps(result, ensure_ascii=False)
        return json.dumps(result.get("result", result), ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"获取 session 失败 - {str(e)}"}, ensure_ascii=False)


def openviking_list_sessions() -> str:
    """列出 OpenViking 所有 Session"""
    try:
        result = _ov_get("/api/v1/sessions", timeout=15)
        if "error" in result:
            return json.dumps(result, ensure_ascii=False)
        return json.dumps(result.get("result", result), ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"列出 session 失败 - {str(e)}"}, ensure_ascii=False)
