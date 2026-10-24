#!/usr/bin/env python3
"""OpenViking 记忆召回 / 抓取（内置子系统）

本模块负责「自动」记忆能力（注入与抓取），自包含实现，不依赖 skill 侧代码：

- 召回：openviking_load_context（question / step 注入）、openviking_load_profile
- 抓取：openviking_capture / openviking_ensure_session / openviking_commit_session

并自带独立的 HTTP 请求实现（_ov_post / _ov_get / _ov_delete），记忆搜索走单独的请求。

开关见 .env（core/config.py）：OV_INJECT / OV_PROFILE / OV_CAPTURE /
OV_RECALL_DEDUP_TURNS / OV_FIND_THRESHOLD / OV_FIND_LIMIT / OV_MIN_QUERY_LENGTH 等。
"""
import hashlib
import json
import os
import re

import requests

from . import config
from .db import INSERT_USER_ROLE


# ============= 基础请求（本模块独立实现，仅供召回/抓取使用）=============
def _ov_base():
    """延迟读取 OpenViking URL，避免模块级别求值导致 .env 未加载的问题"""
    return os.environ.get('OPENVIKING_URL', '')


def openviking_peer_id():
    """解析当前 actor peer：显式 OV_PEER_ID > 按工作目录派生(ws-*) > 回退 OPENVIKING_AGENT。"""
    explicit = os.environ.get('OV_PEER_ID') or config.OV_PEER_ID
    if explicit:
        return explicit
    if config.OV_WORKSPACE_PEER:
        digest = hashlib.md5(config.PROJECT_ROOT.encode('utf-8')).hexdigest()[:12]
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


def _err(message, code=None):
    payload = {"status": "error", "error": str(message)}
    if code is not None:
        payload["code"] = code
    return payload


def _is_error(value):
    return isinstance(value, dict) and value.get("status") == "error"


def _http_failure(exc):
    """把 requests.HTTPError 映射为语义化错误 dict（隐藏 URL/IP，尽量透出后端 message）。"""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
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
    return _err(msg, code=_HTTP_STATUS_CODES.get(status, "http"))


def _ov_get(path, params=None, timeout=15):
    """OpenViking GET 请求；失败返回错误 dict（不抛异常）"""
    try:
        r = requests.get(f"{_ov_base()}{path}", headers=_ov_headers(),
                         params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        return _err("OpenViking 请求超时", code="timeout")
    except requests.HTTPError as e:
        return _http_failure(e)
    except Exception as e:
        return _err(f"OpenViking 请求失败 - {_sanitize(e)}", code="transport")


def _ov_post(path, payload, timeout=15):
    """OpenViking POST 请求；失败返回错误 dict（不抛异常）"""
    try:
        r = requests.post(f"{_ov_base()}{path}", headers=_ov_headers(),
                          json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        return _err("OpenViking 请求超时", code="timeout")
    except requests.HTTPError as e:
        return _http_failure(e)
    except Exception as e:
        return _err(f"OpenViking 请求失败 - {_sanitize(e)}", code="transport")


# ============= 召回：检索 + 注入辅助 =============
RECALL_MARKER = "## 相关记忆"
PROFILE_MARKER = '<openviking-context source="profile">'
# 所有上下文注入统一收在 <system-reminder> 内；OpenViking 内容再包一层 available_openviking
REMINDER_OPEN = "<system-reminder>"
REMINDER_CLOSE = "</system-reminder>"
OV_BLOCK_OPEN = "<available_openviking>"
OV_BLOCK_CLOSE = "</available_openviking>"


def _msg_query_text(m):
    """从单条消息提取用于检索的纯文本；已注入的召回/ profile 块会被跳过避免自反馈。"""
    if not isinstance(m, dict):
        return ''
    role = m.get('role')
    content = m.get('content') or ''
    if role == INSERT_USER_ROLE:
        # 合成注入消息：不参与检索 query，避免自反馈
        return ''
    if role == 'user':
        if RECALL_MARKER in content or PROFILE_MARKER in content:
            return ''
        return content
    if role == 'tool':
        return content
    if role == 'assistant':
        parts = [content or '']
        for tc in (m.get('tool_calls') or []):
            fn = tc.get('function', {}) if isinstance(tc, dict) else getattr(tc, 'function', {})
            name = fn.get('name', '') if isinstance(fn, dict) else getattr(fn, 'name', '')
            if name:
                parts.append(name)
        return '\n'.join(p for p in parts if p)
    return ''


def build_recall_query(messages):
    """由完整消息批次构造检索 query（对齐官方 recallMessage 的 promptText）。"""
    return '\n'.join(t for t in (_msg_query_text(m) for m in (messages or [])) if t).strip()


def openviking_block(content: str) -> str:
    """只包 <available_openviking>，供合并进统一 system-reminder 时使用。"""
    if not content:
        return ""
    return f"{OV_BLOCK_OPEN}\n{content}\n{OV_BLOCK_CLOSE}"


def wrap_openviking_block(content: str) -> str:
    """把 OpenViking 注入内容统一包进 <system-reminder><available_openviking>。"""
    if not content:
        return ""
    return f"{REMINDER_OPEN}\n{openviking_block(content)}\n{REMINDER_CLOSE}"


def wrap_recall_block(block: str) -> str:
    """把召回块包进 <system-reminder><available_openviking>（边界由标签承担）。"""
    return wrap_openviking_block(block)


def _find_payload(query, score_threshold=None, limit=None):
    return {
        "query": query,
        "score_threshold": score_threshold if score_threshold is not None else config.OV_FIND_THRESHOLD,
        "limit": limit if limit is not None else config.OV_FIND_LIMIT,
    }


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


# 客户端跨步/跨轮去重：本 session 已注入过的 URI 不再重复注入。
_RECALL_SEEN = {}


def _recall_dedup_filter(mems, session_id):
    """过滤本 session 已注入的 URI；返回 (新列表, 本次将注入的 uri 集合)。"""
    if not session_id or config.OV_RECALL_DEDUP_TURNS <= 0:
        return mems, set()
    seen = _RECALL_SEEN.setdefault(session_id, set())
    out, injected = [], set()
    for m in mems:
        uri = m.get("uri", "")
        if uri and uri in seen:
            continue
        out.append(m)
        if uri:
            injected.add(uri)
    seen.update(injected)
    if len(seen) > 500:  # 限制集合规模，防止长会话无限增长
        _RECALL_SEEN[session_id] = set(list(seen)[-500:])
    return out, injected


def search_memories(query):
    """召回用的记忆语义搜索：只走 find 接口的**单独请求**（与 skill 的搜索互不影响）。

    返回归一化后的记忆列表（list）；请求失败返回 []。
    query 过短或 OpenViking 未配置时返回 []。
    """
    if not query or len(query) < config.OV_MIN_QUERY_LENGTH:
        return []
    result = _ov_post("/api/v1/search/find", _find_payload(query, config.OV_FIND_THRESHOLD))
    if not isinstance(result, dict) or _is_error(result):
        return []
    return _extract_memories(result)


def openviking_load_context(messages, session_id=None) -> str:
    """基于完整消息批次检索相关记忆，返回可注入上下文的块（含 RECALL_MARKER）。

    对齐官方 DSH 插件：recall 发生在每一步（pre-step），query 取整批消息
    （用户输入 + 工具结果 + 工具调用名），而非仅首轮问题。
    是否调用由调用方按 config.OV_INJECT 决定（off 时本函数直接返回空）。
    """
    if not config.OV_INJECT_ENABLED:
        return ""
    try:
        query = build_recall_query(messages)
        mems = search_memories(query)
        mems, _ = _recall_dedup_filter(mems, session_id)
        hits = mems[:config.OV_FIND_LIMIT]
        if not hits:
            return ""
        ctx_parts = [RECALL_MARKER]
        for h in hits:
            uri = h.get("uri", "")
            abstract = h.get("abstract", "")
            score = h.get("score", 0)
            ctype = h.get("context_type", "")
            if abstract:
                ctx_parts.append(f"- [{uri}] (score={score:.2f}, {ctype})\n  {abstract[:300]}")
        return "\n".join(ctx_parts)
    except Exception:
        return ""


def _extract_tree_entries(tree):
    """从 fs/tree 响应中尽量宽容地提取条目名列表（不同版本结构不同）。"""
    if not isinstance(tree, dict):
        return []
    candidates = []
    raw = tree.get("result")
    if isinstance(raw, dict):
        candidates.append(raw)
    if isinstance(raw, list):
        candidates.append({"entries": raw})
    for key in ("entries", "list", "children", "files", "nodes"):
        v = tree.get(key)
        if isinstance(v, list):
            candidates.append({key: v})
    names = []
    for c in candidates:
        items = (c.get("entries") or c.get("list") or c.get("children")
                 or c.get("files") or c.get("nodes"))
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    names.append(it.get("name") or it.get("title")
                                 or it.get("uri", "").rstrip("/").split("/")[-1])
                elif isinstance(it, str):
                    names.append(it)
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _memory_base(category: str = "") -> str:
    """当前 peer 的隔离记忆目录根：viking://user/<user>/peers/<agent>/memories/[_<category>/]"""
    user = os.environ.get('OPENVIKING_USER', '')
    agent = os.environ.get('OPENVIKING_AGENT', 'default')
    base = f"viking://user/{user}/peers/{agent}/memories/"
    return base + (f"{category}/" if category else "")


def _active_memory_base():
    """默认记忆根，供 profile 索引使用（取第一个有内容的记忆根）。"""
    if config.OV_PEER_ID or config.OV_WORKSPACE_PEER:
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


def openviking_load_profile() -> str:
    """会话开始时拉取可用记忆索引，返回 <openviking-context source="profile"> 块。

    对齐官方 DSH 插件的 session-start profile 注入：让模型每轮都知道记忆库里
    大致有哪些主题，而不是盲搜。仅新建会话时调用一次。开关见 config.OV_PROFILE。
    """
    if not config.OV_PROFILE:
        return ""
    try:
        root = _active_memory_base()
        tree = _ov_get("/api/v1/fs/tree", params={"uri": root})
        if _is_error(tree):
            return ""
        entries = _extract_tree_entries(tree)
        if not entries:
            return ""
        text = "\n".join(f"- {e}" for e in entries[:40])
        cap = config.OV_PROFILE_TOKEN_BUDGET * 2
        if len(text) > cap:
            text = text[:cap]
        return f'{PROFILE_MARKER}\n可用记忆索引（主题概览）：\n{text}\n</openviking-context>'
    except Exception:
        return ""


# ============= 抓取：对话 → OV session（对齐官方 buildCapturePayload）=============
_ACK_RE = re.compile(
    r'^(?:ok|okay|k|yes|yep|no|nope|thanks|thank you|thx|done|收到|好的|好|嗯|可以|继续|'
    r'不用|不需要|没了|好了)[.!?。！？\s]*$', re.I)
_SLASH_RE = re.compile(r'^/[a-z0-9_-]{1,64}\b', re.I)


def _has_enough_signal(text):
    cjk = len(re.findall(r'[\u3400-\u9fff]', text))
    alnum = len(re.findall(r'[a-z0-9]', text, re.I))
    return cjk >= 4 or alnum >= 6 or len(text) >= 12


def _is_punctuation_only(text):
    return not re.search(r'[a-z0-9\u3400-\u9fff]', text, re.I)


def _should_capture(text, role):
    """对齐官方 capture-utils.shouldCaptureText 的轻量噪音过滤。"""
    text = text.strip()
    if not text:
        return False
    if role == 'user' and _SLASH_RE.match(text):
        return False
    if _ACK_RE.match(text):
        return False
    if _is_punctuation_only(text):
        return False
    if not _has_enough_signal(text):
        return False
    return True


def _tool_call_fields(tc):
    """从 tool_call（dict 或 SDK 对象）提取 (id, name, arguments)。"""
    if isinstance(tc, dict):
        fn = tc.get('function') or {}
        return tc.get('id'), fn.get('name'), fn.get('arguments')
    fn = getattr(tc, 'function', None)
    return (getattr(tc, 'id', None),
            getattr(fn, 'name', None),
            getattr(fn, 'arguments', None))


def _tool_input_value(raw):
    """把工具参数规范成官方 tool_input：对象透传，JSON 串解析，其余包成 {value}。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {"value": raw}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = str(raw)
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed}


def _drop_none(part):
    """丢掉值为 None 的键，对齐官方 `field || undefined` 的序列化结果。"""
    cleaned = {k: v for k, v in part.items() if v is not None}
    return cleaned or None


def _tool_call_part(tc):
    """构造官方 tool 部件（调用侧）：type=tool, tool_status=running, tool_input。"""
    tid, name, args = _tool_call_fields(tc)
    part = {
        "type": "tool",
        "tool_id": tid or None,
        "tool_name": name or None,
        "tool_status": "running",
    }
    value = _tool_input_value(args)
    if value is not None:
        part["tool_input"] = value
    return _drop_none(part)


def _tool_result_status(content):
    return "error" if content.lstrip().startswith("Error:") else "completed"


def _tool_result_part(content, tool_id, tool_name):
    """构造官方 tool 部件（结果侧）：type=tool, tool_status, tool_output。"""
    return _drop_none({
        "type": "tool",
        "tool_id": tool_id or None,
        "tool_name": tool_name or None,
        "tool_status": _tool_result_status(content),
        "tool_output": content,
    })


def _to_ov_messages(messages):
    """把聊天消息转成 OV session 批次消息（对齐官方 opencode 插件 upload 结构）。"""
    messages = [m for m in (messages or []) if isinstance(m, dict)]
    tool_names = {}
    for m in messages:
        for tc in (m.get('tool_calls') or []):
            tid, name, _ = _tool_call_fields(tc)
            if tid and name:
                tool_names[tid] = name

    out = []            # 已产出的 OV 消息（按对话顺序）
    index = {}          # tool_id → 已产出的 tool 部件，用于合并结果输出
    for m in messages:
        role = m.get('role')
        content = m.get('content') or ''
        if isinstance(content, list):
            content = "\n".join(str(c) for c in content)
        content = content or ""

        # 合成注入消息（skill 名单 / OV 块）不写回 OV，避免污染记忆
        if role == INSERT_USER_ROLE:
            continue

        # 跳过注入块（召回记忆 / 会话索引），防止把 OV 检索结果当对话回灌
        if RECALL_MARKER in content or PROFILE_MARKER in content:
            continue

        if role == 'assistant':
            if not config.OV_CAPTURE_ASSISTANT_TURNS:
                continue
            text = content[:config.OV_CAPTURE_MAX_LENGTH]
            keep_text = bool(text.strip()) and _should_capture(text, 'assistant')
            tool_parts = []
            for tc in (m.get('tool_calls') or []):
                part = _tool_call_part(tc)
                if not part:
                    continue
                tool_parts.append(part)
                tid = part.get('tool_id')
                if tid:
                    index[tid] = part
            if tool_parts:
                parts = ([{"type": "text", "text": text}] if keep_text else []) + tool_parts
                out.append({"role": "assistant", "parts": parts})
            elif keep_text:
                out.append({"role": "assistant", "content": text})
            continue

        if role == 'tool':
            if not config.OV_CAPTURE_ASSISTANT_TURNS:
                continue
            content = content[:config.OV_CAPTURE_TOOL_MAX_CHARS]
            if not content.strip():
                continue
            tid = m.get('tool_call_id') or ''
            part = index.get(tid) if tid else None
            if part is not None:
                part['tool_status'] = _tool_result_status(content)
                part['tool_output'] = content
                if not part.get('tool_name') and (tool_names.get(tid) or m.get('name')):
                    part['tool_name'] = tool_names.get(tid) or m.get('name')
            else:
                part = _tool_result_part(content, tid, m.get('name') or tool_names.get(tid) or '')
                out.append({"role": "assistant", "parts": [part]})
            continue

        if role == 'user':
            content = content[:config.OV_CAPTURE_MAX_LENGTH]
            if not _should_capture(content, role):
                continue
            out.append({"role": "user", "content": content})
    return out


def openviking_ensure_session(session_id):
    """为 shell-tool 会话在 OV 中建立对应 session，返回 OV session_id；失败返回空串。"""
    if not session_id:
        return ''
    try:
        result = _ov_post("/api/v1/sessions", {}, timeout=15)
        if _is_error(result):
            return ''
        res = result.get('result') if isinstance(result, dict) else None
        if not isinstance(res, dict):
            return ''
        oid = res.get('session_id') or res.get('id') or (res.get('session') or {}).get('id')
        return oid or ''
    except Exception:
        return ''


def openviking_capture(session_id, messages):
    """向 OV session 批量追加消息（config.OV_CAPTURE 开启时调用），失败静默跳过。"""
    if not session_id:
        return
    ov_msgs = _to_ov_messages(messages)
    if not ov_msgs:
        return
    try:
        _ov_post(f"/api/v1/sessions/{session_id}/messages/batch", {"messages": ov_msgs}, timeout=15)
    except Exception:
        pass


def openviking_commit_session(session_id, keep_recent_count=0):
    """提交/归档 OV session，触发记忆提取。失败静默跳过。"""
    if not session_id:
        return
    try:
        _ov_post(f"/api/v1/sessions/{session_id}/commit",
                 {"keep_recent_count": keep_recent_count}, timeout=30)
    except Exception:
        pass
