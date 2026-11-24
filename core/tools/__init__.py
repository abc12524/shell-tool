#!/usr/bin/env python3
"""工具包：原生 function 工具（系统信息 / 命令执行 / skill 合集）+ 工具调用分发器

tool 层只保留与运行环境强相关的少量原生工具；其他能力一律下沉为 skill/ 目录下的脚本，
由 skill_tool 扫描脚本头部自动注册并在进程内执行。
"""
import asyncio
import json
import re

from .envelope import ok
from .system_tools import get_system_info, execute_system_command
from .skill_tool import skill_tool
from .skill_hub import skill_hub, source_label as _hub_source
from .handoff import compress_context
from .. import config
from .. import events

__all__ = [
    "TOOLS",
    "process_tool_calls",
    "get_system_info",
    "execute_system_command",
    "skill_tool",
    "skill_hub",
    "compress_context",
]


# 工具定义列表（符合 OpenAI/DeepSeek 的 tool 格式）
# 原生 tool：系统信息 / 命令执行 / skill 统一入口。
# schema 保持完全静态：skill 名称与说明属于会变化的内容，放在会话首条上下文消息里，
# 避免安装 / 卸载 / 改名导致 tools 段变化、击穿前缀缓存。运行时列表一律走 skill(all=true)。
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
            "name": "skill",
            "description": (
                "skill 合集（由 skill/ 目录脚本自动注册）。"
                "all=true 列出所有 skill 及说明；skill='名称' 查看用法；"
                "skill='名称' 并传 arguments={参数} 执行。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "all": {
                        "type": "boolean",
                        "description": "设为 true 时列出所有已注册 skill 及说明"
                    },
                    "skill": {
                        "type": "string",
                        "description": "要查询或执行的 skill 名称；完整列表见会话中的 skill 上下文或用 all=true 获取"
                    },
                    "arguments": {
                        "type": "object",
                        "description": "执行参数对象（如 {\"action\": \"search\", \"query\": \"...\"}）；不传则返回该 skill 用法"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill_hub",
            "description": (
                "从远程技能仓库（GitHub / Gitea）查看 / 下载 / 卸载 skill，"
                f"默认仓库 {_hub_source()}。"
                "action='list' 列出远程可装技能（含平台/依赖）；"
                "action='install' 并传 name 下载安装到本地 skill/；"
                "action='remove' 并传 name 卸载。下载即信任其代码（会在进程内执行）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "操作：list（默认，列出远程技能）/ install（安装）/ remove（卸载）"
                    },
                    "name": {
                        "type": "string",
                        "description": "技能名，action=install/remove 时必填"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "compress_context",
            "description": (
                "上下文压缩（交接文档）。上下文过长、或当前工作要留给后续继续时调用："
                "把已完成的工作、关键结论与文件路径、待办与下一步、注意事项整理成一份交接文档提交。"
                "文档保存为本地库中的 insert_user 记录并包在 <history> 内；"
                "下一轮对话（无论用户是否加 -n）会自动开启新会话，并注入该 <history> 继续工作。"
                "content 与 path 至少提供一个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "交接文档正文（Markdown 即可）：目标、已完成、关键结论/文件路径、待办、注意事项"
                    },
                    "path": {
                        "type": "string",
                        "description": "可选：本地文本文件路径，读取其内容一并写入交接文档"
                    }
                },
                "required": []
            }
        }
    },
]


# DeepSeek Responses API 内置 web 搜索工具（服务端自动执行）。
# 由 .env 的 LLM_WEB_SEARCH 控制开关；关闭后模型不再被提供该能力。
if config.LLM_WEB_SEARCH:
    TOOLS.append({"type": "web_search"})


# 工具名 → 执行函数 映射
TOOL_FUNCTIONS = {
    "get_system_info": lambda args: ok(get_system_info()),
    "execute_system_command": lambda args: execute_system_command(args.get('command', '')),
    "skill": lambda args: skill_tool(args.get('all', False), args.get('skill', ''), args.get('arguments')),
    "skill_hub": lambda args: skill_hub(args.get('action', 'list'), args.get('name', '')),
}

# 需要会话上下文的工具：handler(arguments, ctx)，ctx = {"session_id": ...}
# 与 TOOL_FUNCTIONS 分开，避免所有处理器都被迫接受未使用的 ctx 参数。
TOOL_FUNCTIONS_CTX = {
    "compress_context": lambda args, ctx: compress_context(
        args.get('content', ''), args.get('path', ''), ctx.get('session_id')),
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


async def _execute_single_tool(tool_call, ctx=None):
    """异步执行单个工具调用，返回 tool 结果消息（供并发执行）。

    参数 JSON 解析失败不再中断整轮执行，而是以 tool 错误信息回传模型自纠。
    ctx 为会话上下文（如 {"session_id": ...}），只有 TOOL_FUNCTIONS_CTX 里的工具会用到。
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
        events.ev_tool_call(function_name, raw=raw_args, call_id=call_id, parse_error=err)
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": f"Error: 工具 {function_name} 参数不是合法 JSON，无法执行 - {err}",
        }

    events.ev_tool_call(function_name, arguments=arguments, call_id=call_id)

    # 执行对应函数（同步阻塞函数放到线程池，不阻塞事件循环）
    # 用 run_in_executor 而非 asyncio.to_thread（后者 3.9+），保证 Python 3.8 可用
    ctx_tool = TOOL_FUNCTIONS_CTX.get(function_name)
    handler = TOOL_FUNCTIONS.get(function_name)
    if ctx_tool:
        try:
            loop = asyncio.get_running_loop()
            result_str = await loop.run_in_executor(None, ctx_tool, arguments, ctx or {})
        except Exception as e:
            result_str = f"Error: 工具 {function_name} 执行失败 - {str(e)}"
    elif handler:
        try:
            loop = asyncio.get_running_loop()
            result_str = await loop.run_in_executor(None, handler, arguments)
        except Exception as e:
            result_str = f"Error: 工具 {function_name} 执行失败 - {str(e)}"
    else:
        result_str = f"Error: 未知工具 {function_name}"

    events.ev_tool_result(function_name, call_id, result_str)

    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": result_str
    }


async def process_tool_calls(tool_calls, session_id=None):
    """异步并发执行工具调用，返回 tool 结果消息列表（role=tool）

    无同步屏障：任务全部并发发起，谁先完成谁先返回（as_completed 逐个交付），
    不等待最慢者；返回列表按输入顺序排列（各结果自带 tool_call_id 关联），
    但执行/交付过程是流式的——快任务完成即输出，慢任务稍后跟上。
    session_id 透传给需要会话上下文的工具（如 compress_context 把交接文档记到当前会话）。
    """
    if not tool_calls:
        return []

    # 并发创建全部任务，无屏障逐个收集完成结果（FIRST_COMPLETED 循环）
    ctx = {"session_id": session_id}
    tasks = [asyncio.create_task(_execute_single_tool(tc, ctx)) for tc in tool_calls]
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
