# skill: ov
# description: OpenViking 外置记忆统一入口（语义搜索 / 写入 / 读取 / 目录 / 遗忘 / Session）
# usage:
#   arguments.action 指定操作：
#     search          上下文感知语义搜索   {query, score_threshold?, limit?}
#     find            纯向量语义搜索       {query, score_threshold?, limit?, target_uri?}
#     remember        保存记忆             {category: preferences|entities|events|experiences, name, content}
#     read            读取记忆文件         {uri}（uri 可为字符串或数组）
#     list_dir        列出目录             {uri, recursive?}
#     write_file      写入记忆文件         {uri, content, mode?: replace|create|append}
#     forget          删除文件/目录        {uri, recursive?}
#     create_session  创建 Session         {session_id?}
#     add_message     追加单条消息         {session_id, role, content, peer_id?}
#     add_messages_batch 批量追加消息      {session_id, messages: [{role, content}]}
#     commit_session  提交/归档 Session    {session_id, keep_recent_count?}
#     get_session     获取 Session 详情    {session_id}
#     list_sessions   列出所有 Session     {}
#   示例: {"action": "search", "query": "用户偏好", "limit": 3}
from core.tools.ov_tools import (
    openviking_search,
    openviking_find,
    openviking_remember,
    openviking_read,
    openviking_list_dir,
    openviking_write_file,
    openviking_forget,
    openviking_create_session,
    openviking_add_message,
    openviking_add_messages_batch,
    openviking_commit_session,
    openviking_get_session,
    openviking_list_sessions,
)
from core.tools.envelope import error

_ACTIONS = {
    "search": lambda a: openviking_search(a.get("query", ""), a.get("score_threshold"), a.get("limit")),
    "find": lambda a: openviking_find(a.get("query", ""), a.get("score_threshold"), a.get("limit"), a.get("target_uri", "")),
    "remember": lambda a: openviking_remember(a.get("category", "entities"), a.get("name", "untitled"), a.get("content", "")),
    "read": lambda a: openviking_read(a.get("uri", "")),
    "list_dir": lambda a: openviking_list_dir(a.get("uri", ""), a.get("recursive", False)),
    "write_file": lambda a: openviking_write_file(a.get("uri", ""), a.get("content", ""), a.get("mode", "replace")),
    "forget": lambda a: openviking_forget(a.get("uri", ""), a.get("recursive", False)),
    "create_session": lambda a: openviking_create_session(a.get("session_id", "")),
    "add_message": lambda a: openviking_add_message(a.get("session_id", ""), a.get("role", "user"), a.get("content", ""), a.get("peer_id", "")),
    "add_messages_batch": lambda a: openviking_add_messages_batch(a.get("session_id", ""), a.get("messages", [])),
    "commit_session": lambda a: openviking_commit_session(a.get("session_id", ""), a.get("keep_recent_count", 0)),
    "get_session": lambda a: openviking_get_session(a.get("session_id", "")),
    "list_sessions": lambda a: openviking_list_sessions(),
}


def run(arguments):
    a = arguments or {}
    action = (a.get("action") or "").strip()
    fn = _ACTIONS.get(action)
    if not fn:
        return error(f"未知 action '{action}'，可用: {', '.join(_ACTIONS)}", code="unknown_action")
    return fn(a)
