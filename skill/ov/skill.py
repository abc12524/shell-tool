# skill: ov
# description: OpenViking 外置记忆统一入口（语义搜索 / 写入 / 读取 / 目录 / 遗忘 / Session）
# status: enable
# usage:
#   arguments.action 指定操作：
#     search          上下文感知语义搜索   {query, score_threshold?, limit?}
#     find            纯向量语义搜索       {query, score_threshold?, limit?, target_uri?}
#     resolve         按文件名解析完整 uri  {name, base_uri?, limit?}
#     remember        保存记忆             {category: preferences|entities|events|experiences, name, content}
#     read            读取记忆文件         {uri 或 name, base_uri?}（uri 可为字符串或数组；只给 name 时自动解析）
#     list_dir        列出目录             {uri, recursive?}
#     write_file      写入记忆文件         {uri 或 name, base_uri?, content, mode?: replace|create|append}
#     forget          删除文件/目录        {uri 或 name, base_uri?, recursive?}
#     create_session  创建 Session         {session_id?}
#     add_message     追加单条消息         {session_id, role, content, peer_id?}
#     add_messages_batch 批量追加消息      {session_id, messages: [{role, content}]}
#     commit_session  提交/归档 Session    {session_id, keep_recent_count?}
#     get_session     获取 Session 详情    {session_id}
#     list_sessions   列出所有 Session     {}
#   默认: search/find 的 score_threshold 与 limit 省略时读配置（env 或同目录 config.json）：
#         OV_SEARCH_THRESHOLD(0.4) / OV_SEARCH_LIMIT(3) / OV_FIND_THRESHOLD(0.4) / OV_FIND_LIMIT(3)；
#         调用时可显式传参覆盖。
#   说明: read/write_file/forget 支持只传 name（文件名或片段），程序用服务端
#         /api/v1/search/glob 解析出完整 viking:// uri，无需手写完整路径。
#         解析默认依次探测 peer 记忆 → 用户级记忆 → 公共资源库(resources)；
#         唯一命中才执行，多命中返回候选，write_file(mode=create) 未命中则新建
#         （落点取“有内容的记忆根”，与读取默认根一致；不会建进 resources）。
#         解析根可用 base_uri 显式指定；resolve 会回传 searched_bases 便于排错。
#   注意: 写入后语义/向量索引是异步排队（semantic skipped / vector queued），
#         刚写完立即 search 可能检索不到，稍候再查。
#   示例: {"action": "search", "query": "用户偏好", "limit": 3}
#         {"action": "write_file", "name": "ov删除工具", "content": "...", "mode": "create"}
try:  # 作为包导入（shell-tool: skill.ov.skill / 移动端 runner）
    from . import _ov_impl as _impl
except ImportError:  # 直接以脚本运行（脱离 shell-tool）：同目录导入
    import _ov_impl as _impl

_ACTIONS = {
    "search": lambda a: _impl.openviking_search(a.get("query", ""), a.get("score_threshold"), a.get("limit")),
    "find": lambda a: _impl.openviking_find(a.get("query", ""), a.get("score_threshold"), a.get("limit"), a.get("target_uri", "")),
    "resolve": lambda a: _impl.openviking_resolve_name(a.get("name", ""), a.get("base_uri", ""), a.get("limit", 20)),
    "remember": lambda a: _impl.openviking_remember(a.get("category", "entities"), a.get("name", "untitled"), a.get("content", "")),
    "read": lambda a: _impl.openviking_read(a.get("uri", ""), a.get("name", ""), a.get("base_uri", "")),
    "list_dir": lambda a: _impl.openviking_list_dir(a.get("uri", ""), a.get("recursive", False)),
    "write_file": lambda a: _impl.openviking_write_file(a.get("uri", ""), a.get("content", ""), a.get("mode", "replace"), a.get("name", ""), a.get("base_uri", "")),
    "forget": lambda a: _impl.openviking_forget(a.get("uri", ""), a.get("recursive", False), a.get("name", ""), a.get("base_uri", "")),
    "create_session": lambda a: _impl.openviking_create_session(a.get("session_id", "")),
    "add_message": lambda a: _impl.openviking_add_message(a.get("session_id", ""), a.get("role", "user"), a.get("content", ""), a.get("peer_id", "")),
    "add_messages_batch": lambda a: _impl.openviking_add_messages_batch(a.get("session_id", ""), a.get("messages", [])),
    "commit_session": lambda a: _impl.openviking_commit_session(a.get("session_id", ""), a.get("keep_recent_count", 0)),
    "get_session": lambda a: _impl.openviking_get_session(a.get("session_id", "")),
    "list_sessions": lambda a: _impl.openviking_list_sessions(),
}


def run(arguments):
    a = arguments or {}
    action = (a.get("action") or "").strip()
    fn = _ACTIONS.get(action)
    if not fn:
        return _impl._emit(_impl._error(f"未知 action '{action}'，可用: {', '.join(_ACTIONS)}", code="unknown_action"))
    return _impl._emit(fn(a))


if __name__ == "__main__":  # 独立运行：python skill.py '{"action": "search", "query": "x"}'
    import json
    import sys

    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
