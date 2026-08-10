#!/usr/bin/env python3
"""API 调用层：流式请求 + 按 think.txt 官方推荐流程的工具调用循环"""
from . import db

from . import config
from .tools import TOOLS, process_tool_calls


def clean_messages_for_api(messages):
    """保留 assistant 消息的 reasoning_content（thinking 模式模型要求原样回传）"""
    return messages


def stream_api_call(client, messages):
    """调用 API 流式接口，返回 (content, reasoning, tool_calls, usage)"""
    cleaned = clean_messages_for_api(messages)
    if config.DEBUG_SEND_SEQ:
        import hashlib, json
        _dbg = []
        for _m in cleaned:
            _blob = json.dumps(_m, ensure_ascii=False, sort_keys=True)
            _dbg.append(f"{_m['role']}:{hashlib.md5(_blob.encode()).hexdigest()[:6]}")
        print("DBGSEQ> " + " | ".join(_dbg))
    stream = client.chat.completions.create(
        model=config.DEEPSEEK_MODEL,
        messages=cleaned,
        tools=TOOLS,
        tool_choice="auto",
        stream=True,
        stream_options={"include_usage": True}
    )

    content = ""
    reasoning = ""
    usage = None
    tool_calls = []

    for chunk in stream:
        if chunk.choices and len(chunk.choices) > 0:
            delta = chunk.choices[0].delta

            if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                if not reasoning:
                    print("\n🤔 思考过程：")
                print(delta.reasoning_content, end="", flush=True)
                reasoning += delta.reasoning_content

            if delta.content:
                if reasoning and not content:
                    print("\n" + "=" * 30)
                    print("💬 最终回答：")
                print(delta.content, end="", flush=True)
                content += delta.content

            if hasattr(delta, 'tool_calls') and delta.tool_calls:
                for tc in delta.tool_calls:
                    existing = next((t for t in tool_calls if getattr(t, 'index', None) == getattr(tc, 'index', None)), None)
                    if existing:
                        if hasattr(tc, 'function') and hasattr(tc.function, 'arguments'):
                            existing.function.arguments += tc.function.arguments
                    else:
                        tool_calls.append(tc)

        if hasattr(chunk, 'usage') and chunk.usage:
            usage = chunk.usage

    print()
    return content, reasoning, tool_calls, usage


def build_assistant_msg(content, reasoning, tool_calls_list):
    """构建可序列化的 assistant 消息"""
    msg = {"role": "assistant", "content": content if content else ""}
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls_list:
        serialized = []
        for tc in tool_calls_list:
            serialized.append({
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments
                }
            })
        msg["tool_calls"] = serialized
    return msg


def chat_completion_with_tools(client, messages, session_id=None):
    """按 think.txt 官方推荐流程优化工具调用：

      请求 1.1  输入[工具, 问题1]      → 输出 思维链1.1 + 工具调用1.1
      请求 1.2  输入[工具, 问题1, 思维链1.1, 工具调用1.1, 调用结果1.1]
                                   → 输出 思维链1.2 + 回答1

      工具只在开始时批量调用一次（MAX_TOOL_ROUNDS），
      一次拿到的所有工具调用并行执行，结果一次性回传，之后直接输出最终回答。
      若模型在最终回答轮仍请求调用工具（依赖链场景），
      在 MAX_TOOL_ROUNDS 预算内可再执行，超出则强制基于已有结果作答。

      返回: (content, reasoning, usage, assistant_msg, all_tool_results, new_history_messages)
    """
    # ---- 请求 1.1：工具 + 问题 → 思维链 + 工具调用 ----
    content, reasoning, tool_calls, usage = stream_api_call(client, messages)
    assistant_msg = build_assistant_msg(content, reasoning, tool_calls)

    # 无工具调用 → 直接返回最终回答
    if not tool_calls:
        return content, reasoning, usage, assistant_msg, [], [assistant_msg]

    all_tool_results = []
    new_history_messages = []
    tool_rounds = 0
    forced_final = False

    while tool_calls:
        tool_rounds += 1
        if tool_rounds > config.MAX_TOOL_ROUNDS:
            # 工具预算耗尽 → 强制转入最终回答
            forced_final = True
            break

        print("\n" + "=" * 30)
        print(f"🔧 执行工具 (第{tool_rounds}轮): {len(tool_calls)} 个调用")

        # 一次并发执行本轮全部工具调用，结果一次性回传
        tool_results = process_tool_calls(tool_calls)
        all_tool_results.extend(tool_results)

        new_history_messages.append(assistant_msg)
        new_history_messages.extend(tool_results)
        messages.append(assistant_msg)
        messages.extend(tool_results)
        if session_id:
            db.append_messages(session_id, [assistant_msg] + tool_results)

        # ---- 请求 1.N+1：思维链 + 工具调用 + 调用结果 → 回答或继续 ----
        print("\n" + "=" * 30)
        print("🤔 继续推理...")
        content, reasoning, tool_calls, usage = stream_api_call(client, messages)
        assistant_msg = build_assistant_msg(content, reasoning, tool_calls)

    # ---- 预算耗尽但仍想调工具 → 强制给出最终回答 ----
    if forced_final:
        force_msg = {
            "role": "user",
            "content": "已达到工具调用次数上限，请不要再调用工具，直接基于已有信息给出最终回答。"
        }
        print("\n⚠️ 工具调用次数已达上限，强制基于已有结果给出最终回答")
        messages.append(force_msg)
        content, reasoning, tool_calls, usage = stream_api_call(client, messages)
        assistant_msg = build_assistant_msg(content, reasoning, tool_calls)
        new_history_messages.append(force_msg)
        if session_id:
            db.append_messages(session_id, [force_msg, assistant_msg])

    new_history_messages.append(assistant_msg)
    if session_id:
        db.append_messages(session_id, [assistant_msg])

    return content, reasoning, usage, assistant_msg, all_tool_results, new_history_messages
