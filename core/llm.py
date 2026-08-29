#!/usr/bin/env python3
"""API 调用层：Responses API 流式请求 + 工具调用循环

内部消息序列统一保持"聊天格式"（role=user/assistant/tool + tool_calls），
仅在发送给 API 时转换为 Responses API 的 input item 列表（to_responses_input），
因此 DB 存储、会话重建、工具分发等下游逻辑无需改动。
"""
import hashlib
import json
from types import SimpleNamespace

from . import db
from . import config
from .tools import TOOLS, process_tool_calls
from .tools.ov_tools import openviking_load_context, wrap_recall_block, openviking_capture


def to_responses_input(messages):
    """将聊天格式消息序列转换为 Responses API 的 input item 列表。

    - system/user/assistant 消息 → message item（丢弃 reasoning_content，
      Responses API 不接受回传，thinking 会由模型自动重新生成）
    - assistant 的 tool_calls   → function_call item（拆分到相邻 message 之后）
    - tool 结果                → function_call_output item
    """
    items = []
    for m in messages:
        role = m.get('role')
        if role in ('system', 'user'):
            items.append({"role": role, "content": m.get('content') or ''})
        elif role == 'assistant':
            tool_calls = m.get('tool_calls') or []
            output_items = m.get('output_items')
            if output_items:
                # 服务端原始输出序列，原样透传（message/reasoning/web_search_call/function_call 保序）
                for oi in output_items:
                    items.append(oi)
            else:
                # 旧格式（无 output_items）fallback：拆分 message + function_call
                if m.get('content'):
                    items.append({"role": "assistant", "content": m['content']})
                if m.get('reasoning_content'):
                    items.append({"type": "reasoning", "content": [{"type": "reasoning_text", "text": m['reasoning_content']}]})
                for tc in tool_calls:
                    fn = tc.get('function', {}) if isinstance(tc, dict) else tc.function
                    items.append({
                        "type": "function_call",
                        "call_id": tc.get('id') if isinstance(tc, dict) else tc.id,
                        "name": fn.get('name') if isinstance(fn, dict) else fn.name,
                        "arguments": fn.get('arguments', '') if isinstance(fn, dict) else fn.arguments,
                    })
        elif role == 'tool':
            items.append({
                "type": "function_call_output",
                "call_id": m.get('tool_call_id'),
                "output": m.get('content') or '',
            })
    return items


def to_responses_tools(tools):
    """将聊天格式 tool schema 转换为 Responses API 格式。

    - function：name 嵌套在 function 下，Responses API 要求提升到工具顶层：
      {"type": "function", "function": {"name", "description", "parameters"}}
      → {"type": "function", "name", "description", "parameters"}
    - web_search：服务端内置工具，原样透传
    """
    out = []
    for t in tools or []:
        if t.get('type') == 'function':
            fn = t.get('function', {})
            item = {"type": "function", "name": fn.get('name')}
            if fn.get('description'):
                item['description'] = fn['description']
            if fn.get('parameters'):
                item['parameters'] = fn['parameters']
            out.append(item)
        elif t.get('type') == 'web_search':
            out.append({"type": "web_search"})
    return out


def _normalize_usage(usage):
    """把 Responses API 的 usage 归一化为聊天格式属性对象（兼容 print_usage_stats）"""
    if usage is None:
        return None
    input_tokens = getattr(usage, 'input_tokens', 0) or 0
    output_tokens = getattr(usage, 'output_tokens', 0) or 0

    cached = 0
    input_details = getattr(usage, 'input_tokens_details', None)
    if input_details is not None:
        cached = getattr(input_details, 'cached_tokens', 0) or 0

    reasoning_tokens = 0
    output_details = getattr(usage, 'output_tokens_details', None)
    if output_details is not None:
        reasoning_tokens = getattr(output_details, 'reasoning_tokens', 0) or 0

    return SimpleNamespace(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        prompt_cache_hit_tokens=cached,
        prompt_cache_miss_tokens=input_tokens - cached,
        total_tokens=input_tokens + output_tokens,
        reasoning_tokens=reasoning_tokens,
    )


async def stream_responses_api(client, messages):
    """异步调用 Responses API 流式接口，返回 (content, reasoning, tool_calls, output_items, usage)

    tool_calls 为聊天格式 dict 列表（兼容 DB 存储与 process_tool_calls），
    output_items 为服务端 output 中可回传 items 的原始顺序序列
    （message/reasoning/web_search_call/function_call，按原文回传），
    usage 为聊天格式属性对象（prompt_tokens/completion_tokens/...）。
    """
    instructions, input_items = _split_instructions(messages)

    if config.DEBUG_SEND_SEQ:
        _dbg = []
        for _m in input_items:
            _role = _m.get('role', _m.get('type', '?'))
            _blob = json.dumps(_m, ensure_ascii=False, sort_keys=True)
            _dbg.append(f"{_role}:{hashlib.md5(_blob.encode()).hexdigest()[:6]}")
        print("DBGSEQ> " + " | ".join(_dbg))

    stream = await client.responses.create(
        model=config.DEEPSEEK_MODEL,
        input=input_items,
        tools=to_responses_tools(TOOLS),
        tool_choice="auto",
        stream=True,
    )

    content = ""
    reasoning = ""
    usage = None
    output_items = []
    search_header_shown = False
    # output_index → 累积中的 function_call
    fc_by_idx = {}

    async for chunk in stream:
        ctype = getattr(chunk, 'type', '')

        if ctype == 'response.output_item.added':
            item = getattr(chunk, 'item', None)
            if item is None:
                continue
            if getattr(item, 'type', None) == 'function_call':
                idx = getattr(chunk, 'output_index', None)
                fc_by_idx[idx] = {
                    'id': getattr(item, 'call_id', None),
                    'name': getattr(item, 'name', None),
                    'arguments': '',
                }
            elif getattr(item, 'type', None) == 'web_search_call':
                if not search_header_shown:
                    print("\n🔎 服务端网页搜索：")
                    search_header_shown = True
                print(f"  - 搜索调用 {getattr(item, 'id', '')} 已发起")

        elif ctype.startswith('response.web_search_call.'):
            print(f"  - 搜索状态: {ctype.split('.')[-1]}")

        elif ctype == 'response.function_call_arguments.delta':
            idx = getattr(chunk, 'output_index', None)
            if idx in fc_by_idx:
                fc_by_idx[idx]['arguments'] += getattr(chunk, 'delta', '') or ''

        elif ctype == 'response.reasoning_text.delta':
            delta = getattr(chunk, 'delta', '') or ''
            if delta:
                if not reasoning:
                    print("\n🤔 思考过程：")
                print(delta, end="", flush=True)
                reasoning += delta

        elif ctype == 'response.output_text.delta':
            delta = getattr(chunk, 'delta', '') or ''
            if delta:
                if reasoning and not content:
                    print("\n" + "=" * 30)
                    print("💬 最终回答：")
                print(delta, end="", flush=True)
                content += delta

        elif ctype == 'response.completed':
            resp = getattr(chunk, 'response', None)
            if resp is not None:
                usage = _normalize_usage(getattr(resp, 'usage', None))
                # 服务端完整 output 序列仅在最终 response.completed 中给出，
                # 从这里按原始顺序提取，供下一轮原样回传（reasoning/web_search_call 必须保持顺序）
                output_items = _extract_output_items(getattr(resp, 'output', None))

        elif ctype == 'response.failed':
            resp = getattr(chunk, 'response', None)
            err = ''
            if resp is not None:
                e = getattr(resp, 'error', None)
                if e is not None:
                    err = getattr(e, 'message', '') or str(e)
            raise RuntimeError(f"Responses API 请求失败: {err}")

    print()

    tool_calls = []
    for idx in sorted(k for k in fc_by_idx if k is not None):
        fc = fc_by_idx[idx]
        tool_calls.append({
            "id": fc['id'],
            "type": "function",
            "function": {"name": fc['name'], "arguments": fc['arguments']},
        })

    return content, reasoning, tool_calls, output_items, usage


def _extract_output_items(output):
    """从最终 response.output 提取全部可回传 items（message/reasoning/web_search_call/function_call），
    保持原始交错顺序。

    DeepSeek thinking 模式要求 reasoning_text 与 web_search_call 按原始顺序原样回传
    （校验按原文与顺序比对），因此回传段必须是服务端 output 的忠实子序列。
    """
    out = []
    for item in output or []:
        if getattr(item, 'type', None) not in ('message', 'reasoning', 'web_search_call', 'function_call'):
            continue
        d = {}
        for k, v in item.model_dump().items():
            if v is not None:
                d[k] = v
        out.append(d)
    return out


def _split_instructions(messages):
    """把第一条 system 消息提取为 instructions 参数，其余转为 input items"""
    input_items = to_responses_input(messages)
    instructions = None
    if input_items and input_items[0].get('role') == 'system':
        instructions = input_items.pop(0)['content']
    return instructions, input_items


def build_assistant_msg(content, reasoning, tool_calls_list, output_items=None):
    """构建可序列化的 assistant 消息（兼容 SDK 对象与 dict 两种 tool_calls）

    output_items 为服务端原始输出序列，下一轮回传时优先原样透传（保证顺序与原文）。
    """
    msg = {"role": "assistant", "content": content if content else ""}
    if reasoning:
        msg["reasoning_content"] = reasoning
    if output_items:
        msg["output_items"] = list(output_items)
    if tool_calls_list:
        serialized = []
        for tc in tool_calls_list:
            if isinstance(tc, dict):
                fn = tc.get('function', {})
                serialized.append({
                    "id": tc.get("id"),
                    "type": "function",
                    "function": {
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments"),
                    }
                })
            else:
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


async def chat_completion_with_tools(client, messages, session_id=None, ov_session_id=None):
    """工具调用循环（协议无关，底层基于 Responses API 流式请求）：

      请求 1.1  输入[工具, 问题1]      → 输出 思维链1.1 + 工具调用1.1
      请求 1.2  输入[工具, 问题1, 思维链1.1, 工具调用1.1, 调用结果1.1]
                                   → 输出 思维链1.2 + 回答1

      工具只在开始时批量调用一次（MAX_TOOL_ROUNDS），
      一次拿到的所有工具调用并行执行（asyncio 并发，无同步屏障），
      结果一次性回传，之后直接输出最终回答。
      若模型在最终回答轮仍请求调用工具（依赖链场景），
      在 MAX_TOOL_ROUNDS 预算内可再执行，超出则强制基于已有结果作答。

      返回: (content, reasoning, usage, assistant_msg, all_tool_results, new_history_messages)
    """
    # ---- 请求 1.1：工具 + 问题 → 思维链 + 工具调用 ----
    content, reasoning, tool_calls, output_items, usage = await stream_responses_api(client, messages)
    web_search_calls = [oi for oi in output_items if oi.get('type') == 'web_search_call']
    assistant_msg = build_assistant_msg(content, reasoning, tool_calls, output_items)

    # 无工具调用 → 直接返回最终回答
    if not tool_calls and not web_search_calls:
        return content, reasoning, usage, assistant_msg, [], [assistant_msg]

    all_tool_results = []
    new_history_messages = []
    tool_rounds = 0
    forced_final = False

    while tool_calls or web_search_calls:
        tool_rounds += 1
        if tool_rounds > config.MAX_TOOL_ROUNDS:
            # 工具预算耗尽 → 强制转入最终回答
            forced_final = True
            break

        print("\n" + "=" * 30)
        print(f"🔧 执行工具 (第{tool_rounds}轮): {len(tool_calls)} 个本地调用 / {len(web_search_calls)} 个服务端搜索")

        # 一次并发执行本轮全部 function 调用（异步无同步屏障），结果一次性回传；
        # web_search_call 由服务端自动执行，仅随 assistant 消息原样回传供恢复结果
        tool_results = await process_tool_calls(tool_calls)
        all_tool_results.extend(tool_results)

        new_history_messages.append(assistant_msg)
        new_history_messages.extend(tool_results)
        messages.append(assistant_msg)
        messages.extend(tool_results)
        if session_id:
            db.append_messages(session_id, [assistant_msg] + tool_results)
        if ov_session_id:
            openviking_capture(ov_session_id, [assistant_msg] + tool_results)

        # ---- 每步召回：工具结果回来后，基于完整批次重新检索相关记忆并注入 ----
        # 对齐官方 pre-step recall：query 含工具结果，下一次模型调用即带上新线索
        step_recall = openviking_load_context(messages, session_id=session_id)
        if step_recall:
            recall_msg = {"role": "user", "content": wrap_recall_block(step_recall)}
            messages.append(recall_msg)
            if session_id:
                db.append_messages(session_id, [recall_msg])
            if ov_session_id:
                openviking_capture(ov_session_id, [recall_msg])

        # ---- 请求 1.N+1：思维链 + 工具调用 + 调用结果 → 回答或继续 ----
        print("\n" + "=" * 30)
        print("🤔 继续推理...")
        content, reasoning, tool_calls, output_items, usage = await stream_responses_api(client, messages)
        web_search_calls = [oi for oi in output_items if oi.get('type') == 'web_search_call']
        assistant_msg = build_assistant_msg(content, reasoning, tool_calls, output_items)

    # ---- 预算耗尽但仍想调工具 → 强制给出最终回答 ----
    if forced_final:
        force_msg = {
            "role": "user",
            "content": "已达到工具调用次数上限，请不要再调用工具，直接基于已有信息给出最终回答。"
        }
        print("\n⚠️ 工具调用次数已达上限，强制基于已有结果给出最终回答")
        messages.append(force_msg)
        step_recall = openviking_load_context(messages, session_id=session_id)
        if step_recall:
            recall_msg = {"role": "user", "content": wrap_recall_block(step_recall)}
            messages.append(recall_msg)
            if session_id:
                db.append_messages(session_id, [recall_msg])
            if ov_session_id:
                openviking_capture(ov_session_id, [recall_msg])
        content, reasoning, tool_calls, output_items, usage = await stream_responses_api(client, messages)
        web_search_calls = [oi for oi in output_items if oi.get('type') == 'web_search_call']
        assistant_msg = build_assistant_msg(content, reasoning, tool_calls, output_items)
        new_history_messages.append(force_msg)
        if session_id:
            db.append_messages(session_id, [assistant_msg])
        if ov_session_id:
            openviking_capture(ov_session_id, [assistant_msg])

    new_history_messages.append(assistant_msg)
    if session_id:
        db.append_messages(session_id, [assistant_msg])
    if ov_session_id:
        openviking_capture(ov_session_id, [assistant_msg])

    return content, reasoning, usage, assistant_msg, all_tool_results, new_history_messages
