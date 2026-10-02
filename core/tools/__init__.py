#!/usr/bin/env python3
"""工具包：工具实现 + 工具定义（TOOLS schema）+ 工具调用分发器"""
import asyncio
import json
import re

from .envelope import ok
from .system_tools import get_system_info, execute_system_command
from .search_tools import baidu_search
from .ov_tools import (
    openviking_search,
    openviking_find,
    openviking_remember,
    openviking_read,
    openviking_load_context,
    openviking_load_profile,
)
from .other_ov_tool import other_ov_tool
from .script_tools import script_editor
from .. import config

__all__ = [
    "TOOLS",
    "process_tool_calls",
    "get_system_info",
    "execute_system_command",
    "baidu_search",
    "script_editor",
    "openviking_search",
    "openviking_find",
    "openviking_remember",
    "openviking_read",
    "openviking_load_context",
    "openviking_load_profile",
    "other_ov_tool",
]


# 工具定义列表（符合 OpenAI/DeepSeek 的 tool 格式）
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_system_info",
            "description": "获取当前操作系统的详细信息，包括系统类型、版本、架构等。适用于了解运行环境。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_system_command",
            "description": "执行系统命令（支持 Windows PowerShell/CMD 和 Linux/macOS bash）。注意：命令需要是当前操作系统支持的格式。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的系统命令，例如：'ls -la' (Linux/macOS) 或 'dir' (Windows)"
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "baidu_search",
            "description": "百度搜索 / 百科查询。通过百度千帆引擎搜索互联网信息或查询百科词条。适用于：搜索最新资讯、查百科、查询知识类问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["raw", "summary", "baike", "baikelist"],
                        "description": "搜索模式：raw=原始搜索结果, summary=网页摘要(AI总结+来源), baike=百科词条详情, baikelist=百科搜索列表"
                    },
                    "query": {
                        "type": "string",
                        "description": "搜索关键词或百科词条名"
                    }
                },
                "required": ["mode", "query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "script_editor",
            "description": (
                "对脚本做增删改查（CRUD）并按需读取片段/结构。数据来源为 file_path（本地文件，可写）"
                "或 url（http/https，只读），工具自行读取并自动探测编码（utf-8/BOM/gb18030/big5 等），输出统一 UTF-8。"
                "read=symbol（单个或数组）/pattern（正则）批量取符号，或 start_line~end_line 行范围，"
                "都不给则返回结构骨架；输出均带行号，超 limit 截断并给续读提示。"
                "outline=结构骨架（每个符号带起止行号，便于随后按范围精确读）。"
                "edit=用 new_code 精确替换 old_code；delete=删除 old_code；"
                "add=在 old_code 之后插入 new_code（省略 old_code 则追加末尾）。"
                "编辑采用精确字符串替换（对齐 edit 工具）：old_code 需逐字符一致，唯一匹配才执行，"
                "匹配多处需显式 replace_all=true；本地文件修改会写回并先生成 .bak 备份。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "outline", "add", "edit", "delete"],
                        "description": "read=读片段/骨架; outline=读结构骨架; add=在 old_code 后插入 new_code; edit=用 new_code 替换 old_code; delete=删除 old_code"
                    },
                    "file_path": {
                        "type": "string",
                        "description": "本地脚本文件路径（可读写；修改写回并备份）。与 url 二选一"
                    },
                    "url": {
                        "type": "string",
                        "description": "http/https 脚本 URL（只读，不能修改）。与 file_path 二选一"
                    },
                    "language": {
                        "type": "string",
                        "enum": ["python", "java", "kotlin", "c", "cpp", "csharp", "javascript", "typescript", "shell"],
                        "description": "脚本语言（read symbol/pattern、outline 时需要；省略则按文件名/URL 扩展名推断）"
                    },
                    "symbol": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}}
                        ],
                        "description": "read 时按符号名返回源码（可传数组批量），如 'ClassName.method'"
                    },
                    "pattern": {
                        "type": "string",
                        "description": "read 时按正则匹配符号名批量返回（与 symbol 可同时使用）"
                    },
                    "start_line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "read 时起始行号（1-based，含），省略则从第 1 行开始"
                    },
                    "end_line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "read 时结束行号（1-based，含），省略则到文件末尾"
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "read 输出最大行数，默认 400，超出截断并返回续读提示"
                    },
                    "old_code": {
                        "type": "string",
                        "description": "要精确匹配的原文片段（add/edit/delete 用；add 省略则追加末尾）"
                    },
                    "new_code": {
                        "type": "string",
                        "description": "edit=替换后的新片段；add=要插入的新片段；delete 不用"
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "old_code 匹配到多处时是否全部替换/插入，默认 false（多处则报错）"
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "openviking_search",
            "description": "在 OpenViking 外置记忆中做上下文感知语义搜索（search 接口：结合会话语境提升召回），查找之前保存的知识、偏好、项目信息等。当用户的问题涉及已知信息时先查记忆。可由你自行判断相似度阈值与返回条数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，描述要查找什么内容"
                    },
                    "score_threshold": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "相似度阈值（0~1），默认 0.4。阈值越高要求记忆与问题越相关"
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 10,
                        "description": "返回条数上限（0~10），默认 3"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "openviking_find",
            "description": "在 OpenViking 外置记忆中做语义搜索（find 接口：纯向量相似度、无会话上下文、低延迟），查找之前保存的知识、偏好、项目信息等。当用户的问题涉及已知信息时先查记忆。可由你自行判断相似度阈值、返回条数与检索范围。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，描述要查找什么内容"
                    },
                    "score_threshold": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "相似度阈值（0~1），默认 0.4。阈值越高要求记忆与问题越相关"
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 10,
                        "description": "返回条数上限（0~10），默认 3"
                    },
                    "target_uri": {
                        "type": "string",
                        "description": "可选，限定检索范围的 Viking URI 前缀，如 viking://user/memories/（仅用户记忆）、viking://resources/my-project/（指定项目）。留空则在全部范围检索"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "openviking_remember",
            "description": "将重要信息保存到 OpenViking 外置记忆中，以便后续对话回忆。适合保存：用户偏好、项目配置、关键决策、有用的操作经验。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["preferences", "entities", "events", "experiences"],
                        "description": "记忆分类：preferences=用户偏好, entities=项目/概念/人物, events=决策/里程碑, experiences=操作经验"
                    },
                    "name": {
                        "type": "string",
                        "description": "记忆名称/主题，如 'search_preference', 'project_hermes', 'deploy_decision'"
                    },
                    "content": {
                        "type": "string",
                        "description": "要保存的内容，用 Markdown 格式"
                    }
                },
                "required": ["category", "name", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "openviking_read",
            "description": "通过 URI 读取 OpenViking 记忆中的 .md 文件内容。uri 支持单个文件 URI 或 URI 数组（同时读取多个文件）。URI 格式: viking://user/{user}/...",
            "parameters": {
                "type": "object",
                "properties": {
                    "uri": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}}
                        ],
                        "description": "单个文件 URI，或文件 URI 数组，如 viking://user/p30/peers/default/memories/entities/home_snmp_ap_info.md"
                    }
                },
                "required": ["uri"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "other_ov_tool",
            "description": "OpenViking 其他工具合集（除 search/remember/read 外），含 9 个子工具：list_dir/write_file/forget/session 系列。all=true 列出所有工具及说明；tool=子工具名 查看使用方式；tool+arguments 实际执行子工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "all": {
                        "type": "boolean",
                        "description": "设为 true 时列出合集内所有工具的说明（不含使用方式）"
                    },
                    "tool": {
                        "type": "string",
                        "description": "要查询或执行的子工具名，如 openviking_write_file / openviking_create_session"
                    },
                    "arguments": {
                        "type": "object",
                        "description": "子工具的执行参数字典，配合 tool 使用（如 {\"uri\": \"...\", \"content\": \"...\", \"mode\": \"create\"}）"
                    }
                },
                "required": []
            }
        }
    },
]


# DeepSeek Responses API 内置 web 搜索工具（服务端自动执行，区别于本地的 baidu_search 等）。
# 由 .env 的 LLM_WEB_SEARCH 控制开关（默认开启）；关闭后模型不再被提供该能力。
if config.LLM_WEB_SEARCH:
    TOOLS.append({"type": "web_search"})


# 工具名 → 执行函数 映射
TOOL_FUNCTIONS = {
    "get_system_info": lambda args: ok(get_system_info()),
    "execute_system_command": lambda args: execute_system_command(args.get('command', '')),
    "baidu_search": lambda args: baidu_search(args.get('mode', 'raw'), args.get('query', '')),
    "script_editor": lambda args: script_editor(args.get('action', ''), args.get('file_path'), args.get('url'), args.get('language'), args.get('symbol'), args.get('pattern'), args.get('start_line'), args.get('end_line'), args.get('limit', 400), args.get('old_code'), args.get('new_code'), args.get('replace_all', False)),
    "openviking_search": lambda args: openviking_search(args.get('query', ''), args.get('score_threshold'), args.get('limit')),
    "openviking_find": lambda args: openviking_find(args.get('query', ''), args.get('score_threshold'), args.get('limit'), args.get('target_uri', '')),
    "openviking_read": lambda args: openviking_read(args.get('uri', '')),
    "openviking_remember": lambda args: openviking_remember(args.get('category', 'entities'), args.get('name', 'untitled'), args.get('content', '')),
    "other_ov_tool": lambda args: other_ov_tool(args.get('all', False), args.get('tool', ''), args.get('arguments')),
}


def _parse_tool_arguments(raw):
    """解析工具调用参数 JSON，对 LLM 常见输出瑕疵做容错。

    返回 (dict, None) 或 (None, error_msg)。解析失败时不再向上抛异常，
    交由调用方以 tool 错误信息回传给模型自纠。
    """
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str):
        raw = raw or "{}"
    text = raw.strip()
    # 1) 直接解析
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        pass
    # 2) 去除尾随逗号 / 多余逗号
    cleaned = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        return json.loads(cleaned), None
    except json.JSONDecodeError:
        pass
    # 3) 截断场景：补全未闭合的 { [ 括号
    opens = sum(1 for c in cleaned if c in "{[")
    closes = sum(1 for c in cleaned if c in "}]")
    if opens > closes:
        try:
            return json.loads(cleaned + "}" * (opens - closes)), None
        except json.JSONDecodeError:
            pass
    return None, f"参数不是合法 JSON（已尝试容错修复仍失败）: {raw[:200]}"


async def _execute_single_tool(tool_call):
    """异步执行单个工具调用，返回 tool 结果消息（供并发执行）。

    参数 JSON 解析失败不再中断整轮执行，而是以 tool 错误信息回传模型自纠。
    """
    # 先取工具名与调用 id（不依赖参数解析，确保总能产出关联结果）
    if hasattr(tool_call, 'function'):
        function_name = tool_call.function.name
        raw_args = tool_call.function.arguments
        call_id = tool_call.id
    else:
        function_name = tool_call['function']['name']
        raw_args = tool_call['function']['arguments']
        call_id = tool_call['id']

    arguments, err = _parse_tool_arguments(raw_args)
    if err:
        print(f"⚠️ 工具 {function_name} 参数解析失败: {err}")
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": f"Error: 工具 {function_name} 参数不是合法 JSON，无法执行 - {err}",
        }

    print(f"🔧 执行工具: {function_name}")
    print(f"📥 参数: {json.dumps(arguments, ensure_ascii=False)}")

    # 执行对应函数（同步阻塞函数放到线程池，不阻塞事件循环）
    handler = TOOL_FUNCTIONS.get(function_name)
    if handler:
        try:
            result_str = await asyncio.to_thread(handler, arguments)
        except Exception as e:
            result_str = f"Error: 工具 {function_name} 执行失败 - {str(e)}"
    else:
        result_str = f"Error: 未知工具 {function_name}"

    print(f"📤 结果: {result_str[:200]}{'...' if len(result_str) > 200 else ''}")

    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": result_str
    }


async def process_tool_calls(tool_calls):
    """异步并发执行工具调用，返回 tool 结果消息列表（role=tool）

    无同步屏障：任务全部并发发起，谁先完成谁先返回（as_completed 逐个交付），
    不等待最慢者；返回列表按输入顺序排列（各结果自带 tool_call_id 关联），
    但执行/交付过程是流式的——快任务完成即输出，慢任务稍后跟上。
    """
    if not tool_calls:
        return []

    # 并发创建全部任务，无屏障逐个收集完成结果（FIRST_COMPLETED 循环）
    tasks = [asyncio.create_task(_execute_single_tool(tc)) for tc in tool_calls]
    idx_map = {id(t): i for i, t in enumerate(tasks)}
    results = [None] * len(tool_calls)
    done_count = 0
    pending = set(tasks)
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            idx = idx_map[id(t)]
            results[idx] = t.result()
            done_count += 1
            # print(f"⚡ 工具完成 ({done_count}/{len(tool_calls)}): {results[idx]['tool_call_id']}")

    return results
