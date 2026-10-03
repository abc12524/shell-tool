#!/usr/bin/env python3
"""工具包：原生 function 工具（系统信息 / 命令执行 / skill 合集）+ 工具调用分发器

tool 层只保留与运行环境强相关的少量原生工具；ov（OpenViking 记忆）、
script（脚本/代码编辑）、baidu_search（百度搜索）等一律下沉为 skill/ 目录下的脚本，
由 skill_tool 扫描脚本头部自动注册并在进程内执行。
"""
import asyncio
import json
import re

from .envelope import ok
from .system_tools import get_system_info, execute_system_command
from .skill_tool import skill_tool, list_skills
from .. import config

__all__ = [
    "TOOLS",
    "process_tool_calls",
    "get_system_info",
    "execute_system_command",
    "skill_tool",
]


# 工具定义列表（符合 OpenAI/DeepSeek 的 tool 格式）
# 原生 tool：系统信息 / 命令执行 / skill 统一入口（skill 列表由 skill/ 目录自动注册）。
# skill 名称与说明摘要由注册表运行时生成，新增脚本即自动出现在工具描述中。
_SKILL_SUMMARY = "、".join(f"{name}={desc}" for name, desc in list_skills().items()) or "(空)"
_SKILL_NAMES = " / ".join(list_skills()) or "(空)"

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
                f"skill 合集（由 skill/ 目录脚本自动注册）：{_SKILL_SUMMARY}。"
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
                        "description": f"要查询或执行的 skill 名称，如 {_SKILL_NAMES}"
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
]


# DeepSeek Responses API 内置 web 搜索工具（服务端自动执行，区别于 skill 里的 baidu_search）。
# 由 .env 的 LLM_WEB_SEARCH 控制开关；关闭后模型不再被提供该能力。
if config.LLM_WEB_SEARCH:
    TOOLS.append({"type": "web_search"})


# 工具名 → 执行函数 映射
TOOL_FUNCTIONS = {
    "get_system_info": lambda args: ok(get_system_info()),
    "execute_system_command": lambda args: execute_system_command(args.get('command', '')),
    "skill": lambda args: skill_tool(args.get('all', False), args.get('skill', ''), args.get('arguments')),
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
