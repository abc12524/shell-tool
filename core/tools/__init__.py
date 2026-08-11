#!/usr/bin/env python3
"""工具包：工具实现 + 工具定义（TOOLS schema）+ 工具调用分发器"""
import asyncio
import json

from .system_tools import get_system_info, execute_system_command
from .search_tools import baidu_search
from .ov_tools import (
    openviking_search,
    openviking_remember,
    openviking_read,
    openviking_load_context,
)
from .other_ov_tool import other_ov_tool

__all__ = [
    "TOOLS",
    "process_tool_calls",
    "get_system_info",
    "execute_system_command",
    "baidu_search",
    "openviking_search",
    "openviking_remember",
    "openviking_read",
    "openviking_load_context",
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
            "name": "openviking_search",
            "description": "在 OpenViking 外置记忆中语义搜索，查找之前保存的知识、偏好、项目信息等。当用户的问题涉及已知信息时先查记忆。可由你自行判断相似度阈值与返回条数。",
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
                        "description": "相似度阈值（0~1），默认 0.35。阈值越高要求记忆与问题越相关"
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
            "name": "openviking_remember",
            "description": "将重要信息保存到 OpenViking 外置记忆中，以便后续对话回忆。适合保存：用户偏好、项目配置、关键决策、有用的操作经验。使用英文记录，中文有无法索引的bug",
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
            "description": "OpenViking 其他工具合集（除 search/remember/read 外），含 8 个子工具：list_dir/write_file/session 系列。all=true 列出所有工具及说明；tool=子工具名 查看使用方式；tool+arguments 实际执行子工具。",
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


# 工具名 → 执行函数 映射
TOOL_FUNCTIONS = {
    "get_system_info": lambda args: json.dumps(get_system_info(), ensure_ascii=False, indent=2),
    "execute_system_command": lambda args: execute_system_command(args.get('command', '')),
    "baidu_search": lambda args: baidu_search(args.get('mode', 'raw'), args.get('query', '')),
    "openviking_search": lambda args: openviking_search(args.get('query', ''), args.get('score_threshold'), args.get('limit')),
    "openviking_read": lambda args: openviking_read(args.get('uri', '')),
    "openviking_remember": lambda args: openviking_remember(args.get('category', 'entities'), args.get('name', 'untitled'), args.get('content', '')),
    "other_ov_tool": lambda args: other_ov_tool(args.get('all', False), args.get('tool', ''), args.get('arguments')),
}


async def _execute_single_tool(tool_call):
    """异步执行单个工具调用，返回 tool 结果消息（供并发执行）"""
    if hasattr(tool_call, 'function'):
        function_name = tool_call.function.name
        arguments = json.loads(tool_call.function.arguments)
        call_id = tool_call.id
    else:
        # 处理 dict 格式
        function_name = tool_call['function']['name']
        arguments = json.loads(tool_call['function']['arguments'])
        call_id = tool_call['id']

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

    本轮全部工具调用通过 asyncio 并发调度执行，无同步屏障
    （不等待最慢者才继续，各自完成各自返回）；
    返回顺序与输入 tool_calls 保持一致（各结果自带 tool_call_id 关联）。
    """
    if not tool_calls:
        return []

    # 并发创建全部任务，统一收集（asyncio 原生并发，非线程池阻塞等待）
    tasks = [asyncio.create_task(_execute_single_tool(tc)) for tc in tool_calls]
    results = await asyncio.gather(*tasks)

    return results
