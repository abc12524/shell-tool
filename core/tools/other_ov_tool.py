#!/usr/bin/env python3
"""other_ov_tool：OpenViking 其他工具合集（除 search/remember/read 外）

合并了 8 个子工具（list_dir / write_file / session 系列），
通过参数在 查询 与 执行 之间切换：
  - all=true                        → 列出合集内所有工具及说明（不含使用方式）
  - tool='openviking_xxx'           → 返回该工具的使用方式（不执行）
  - tool='openviking_xxx' + arguments={...} → 实际执行该子工具并返回结果
"""
import json

from .envelope import ok, error, is_error
from .ov_tools import (
    openviking_list_dir,
    openviking_write_file,
    openviking_create_session,
    openviking_add_message,
    openviking_add_messages_batch,
    openviking_commit_session,
    openviking_get_session,
    openviking_list_sessions,
)

# 合集内子工具：工具名 → 简短说明（供 all=true 列出）
OTHER_OV_TOOLS = {
    "openviking_list_dir": "列出 OpenViking 指定目录下的所有文件和子目录，支持递归",
    "openviking_write_file": "写入/追加内容到 OpenViking 记忆文件（create/replace/append 三种模式）",
    "openviking_create_session": "创建新的对话 Session，用于保存一段完整对话历史，返回 session_id",
    "openviking_add_message": "向 Session 添加单条消息（user 或 assistant）",
    "openviking_add_messages_batch": "批量向 Session 添加多条消息（一次最多 100 条）",
    "openviking_commit_session": "提交/归档 Session，触发 LLM 提取结构化长期记忆",
    "openviking_get_session": "获取 Session 详情，包括会话中的消息列表",
    "openviking_list_sessions": "列出所有已创建的 Session",
}

# 合集内子工具：工具名 → 使用方式（参数、必填、示例，供仅 tool 时返回）
OTHER_OV_USAGES = {
    "openviking_list_dir": (
        "列出 OpenViking 指定目录下的所有文件和子目录。\n"
        "必填参数:\n"
        "  uri (string) - 目录 URI，如 viking://user/{user}/peers/default/memories/entities/\n"
        "可选参数:\n"
        "  recursive (boolean) - 是否递归列出子目录内容，默认 false"
    ),
    "openviking_write_file": (
        "写入内容到 OpenViking 记忆文件。\n"
        "必填参数:\n"
        "  uri (string) - 文件 URI，如 viking://user/{user}/peers/default/memories/preferences/language.md\n"
        "  content (string) - 要写入的内容（Markdown 格式）\n"
        "  mode (string) - create=创建新文件, replace=覆盖已有, append=追加到末尾\n"
        "示例: {\"uri\": \".../memories/preferences/language.md\", \"content\": \"# 语言\", \"mode\": \"create\"}"
    ),
    "openviking_create_session": (
        "创建新的对话 Session，返回 session_id。\n"
        "可选参数:\n"
        "  session_id (string) - 自定义 session_id（UUID 格式），不传则自动生成"
    ),
    "openviking_add_message": (
        "向 Session 添加单条消息。\n"
        "必填参数:\n"
        "  session_id (string) - Session ID\n"
        "  role (string) - user 或 assistant\n"
        "  content (string) - 消息内容\n"
        "可选参数:\n"
        "  peer_id (string) - assistant 角色时的角色名/peer_id，如 vikingbot"
    ),
    "openviking_add_messages_batch": (
        "批量向 Session 添加多条消息（一次最多 100 条），比逐条添加效率高。\n"
        "必填参数:\n"
        "  session_id (string) - Session ID\n"
        "  messages (array of object) - 消息列表，每条含 role(user/assistant) 和 content\n"
        "示例: {\"session_id\": \"xxx\", \"messages\": [{\"role\": \"user\", \"content\": \"你好\"}]}"
    ),
    "openviking_commit_session": (
        "提交/归档 Session，触发 LLM 从会话内容中提取结构化长期记忆。commit 之后不要再次 add_message。\n"
        "必填参数:\n"
        "  session_id (string) - Session ID\n"
        "可选参数:\n"
        "  keep_recent_count (integer) - 保留最近 N 条消息在活跃 session 中，0=归档所有消息（默认）"
    ),
    "openviking_get_session": (
        "获取 Session 详情，包括会话中的消息列表。\n"
        "必填参数:\n"
        "  session_id (string) - Session ID"
    ),
    "openviking_list_sessions": (
        "列出所有已创建的 Session。\n"
        "无需参数。"
    ),
}

# 子工具名 → 实际执行函数 映射
TOOL_HANDLERS = {
    "openviking_list_dir": lambda a: openviking_list_dir(a.get('uri', ''), a.get('recursive', False)),
    "openviking_write_file": lambda a: openviking_write_file(a.get('uri', ''), a.get('content', ''), a.get('mode', 'replace')),
    "openviking_create_session": lambda a: openviking_create_session(a.get('session_id', '')),
    "openviking_add_message": lambda a: openviking_add_message(a.get('session_id', ''), a.get('role', 'user'), a.get('content', ''), a.get('peer_id', '')),
    "openviking_add_messages_batch": lambda a: openviking_add_messages_batch(a.get('session_id', ''), a.get('messages', [])),
    "openviking_commit_session": lambda a: openviking_commit_session(a.get('session_id', ''), a.get('keep_recent_count', 0)),
    "openviking_get_session": lambda a: openviking_get_session(a.get('session_id', '')),
    "openviking_list_sessions": lambda a: openviking_list_sessions(),
}

HELP_TEXT = (
    "other_ov_tool 是 OpenViking 其他工具合集（除 search/remember/read 外），含 9 个子工具：\n"
    "  - 列出所有工具及说明：传 all=true\n"
    "  - 查看某个工具的使用方式：传 tool='工具名'（不传 arguments）\n"
    "  - 执行某个子工具：传 tool='工具名' 并传 arguments={子工具参数}\n"
    "可用的子工具名：" + ", ".join(sorted(OTHER_OV_TOOLS.keys()))
)


def _list_tools() -> str:
    """列出合集内所有工具的说明（不含使用方式）"""
    return ok({"count": len(OTHER_OV_TOOLS), "tools": OTHER_OV_TOOLS})


def _get_usage(tool: str) -> str:
    """返回指定工具的使用方式"""
    usage = OTHER_OV_USAGES.get(tool)
    if not usage:
        return error(f"未知工具 {tool}", code="unknown_tool")
    return ok({"tool": tool, "usage": usage})


def _execute(tool: str, arguments: dict) -> str:
    """执行指定子工具并返回结果（子工具已返回规范信封，原样透传）"""
    handler = TOOL_HANDLERS.get(tool)
    if not handler:
        return error(f"未知工具 {tool}", code="unknown_tool")
    try:
        result = handler(arguments or {})
        # 子工具（list_dir/write_file/session 系列）已返回规范信封字符串
        if isinstance(result, str):
            return result
        return ok(result)
    except Exception as e:
        return error(f"工具 {tool} 执行失败 - {str(e)}", code="internal")


def other_ov_tool(all: bool = False, tool: str = "", arguments: dict = None) -> str:
    """OpenViking 其他工具合集：查询或执行子工具

    - all=true        → 列出所有子工具及说明（不含使用方式）
    - tool 指定无 arguments → 返回该工具使用方式
    - tool 指定 + arguments  → 执行该子工具
    - 均未传           → 返回本工具用法提示
    """
    if all:
        return _list_tools()
    if tool:
        # arguments 未传（None）→ 查用法；传了（即使是空字典 {}）→ 执行
        if arguments is not None:
            return _execute(tool, arguments)
        return _get_usage(tool)
    return HELP_TEXT
