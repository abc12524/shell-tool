#!/usr/bin/env python3
"""API 调用层：流式请求 + 工具调用循环（支持两种接入协议）

- responses：DeepSeek 官方 Responses API（默认）
- chat：OpenAI 兼容 Chat Completions API（OpenCode Zen / OpenCode Go 等网关）

按 config.DEEPSEEK_API_TYPE 由 stream_llm 分发；两种协议返回签名一致，
上层工具循环无差别复用。

内部消息序列统一保持"聊天格式"（role=user/assistant/tool + tool_calls）：
- chat 协议直接规整后发送（to_chat_messages / to_chat_tools）
- responses 协议仅在发送时转换为 Responses API 的 input item 列表（to_responses_input），
  因此 DB 存储、会话重建、工具分发等下游逻辑无需改动。
"""
import hashlib
import json
from types import SimpleNamespace

from . import db
from . import config
from . import events
from .console import LiveMarkdown, LiveReasoning
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


def to_chat_messages(messages):
    """将内部聊天格式消息规整为 Chat Completions 请求消息。

    - 丢弃 Responses 专属字段（output_items），以及 reasoning_content
      （DeepSeek 等 OpenAI 兼容接口不接受回传思考内容）
    - 保留 user/system/assistant(含 tool_calls)/tool 的聊天结构，无需转换协议
    """
    out = []
    for m in messages:
        role = m.get('role')
        if role in ('system', 'user'):
            out.append({"role": role, "content": m.get('content') or ''})
        elif role == 'assistant':
            msg = {"role": "assistant", "content": m.get('content') or ''}
            tool_calls = m.get('tool_calls') or []
            if tool_calls:
                serialized = []
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        fn = tc.get('function', {})
                        serialized.append({
                            "id": tc.get('id'),
                            "type": "function",
                            "function": {"name": fn.get('name'), "arguments": fn.get('arguments')},
                        })
                    else:
                        serialized.append({
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        })
                msg['tool_calls'] = serialized
            out.append(msg)
        elif role == 'tool':
            out.append({
                "role": "tool",
                "tool_call_id": m.get('tool_call_id'),
                "content": m.get('content') or '',
            })
    return out


def to_chat_tools(tools):
    """将聊天格式 tool schema 转换为 Chat Completions 格式。

    聊天格式与 OpenAI function 格式一致，原样返回 function 工具；
    web_search 为 Responses API 服务端内置工具，Chat Completions 不支持，直接丢弃。
    """
    return [t for t in (tools or []) if t.get('type') == 'function']


def _session_headers(session_id):
    """按配置生成会话标识请求头（OpenCode Go 路由/缓存优化用）；未配置会话头时返回 None"""
    if config.LLM_SESSION_HEADER and session_id:
        return {config.LLM_SESSION_HEADER: str(session_id)}
    return None


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


def _normalize_chat_usage(usage):
    """把 Chat Completions 的 usage 归一化为聊天格式属性对象（兼容 print_usage_stats）

    兼容两种缓存字段表达：
    - DeepSeek 直出 prompt_cache_hit_tokens / prompt_cache_miss_tokens
    - OpenAI 标准 prompt_tokens_details.cached_tokens（miss = input - cached）
    """
    if usage is None:
        return None
    input_tokens = getattr(usage, 'prompt_tokens', 0) or 0
    output_tokens = getattr(usage, 'completion_tokens', 0) or 0

    hit = getattr(usage, 'prompt_cache_hit_tokens', None)
    miss = getattr(usage, 'prompt_cache_miss_tokens', None)
    if hit is None:
        cached = 0
        input_details = getattr(usage, 'prompt_tokens_details', None)
        if input_details is not None:
            cached = getattr(input_details, 'cached_tokens', 0) or 0
        hit = cached
        miss = input_tokens - cached
    else:
        hit = hit or 0
        if miss is None:
            miss = input_tokens - hit

    reasoning_tokens = 0
    output_details = getattr(usage, 'completion_tokens_details', None)
    if output_details is not None:
        reasoning_tokens = getattr(output_details, 'reasoning_tokens', 0) or 0

    return SimpleNamespace(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        prompt_cache_hit_tokens=hit,
        prompt_cache_miss_tokens=miss,
        total_tokens=getattr(usage, 'total_tokens', None) or (input_tokens + output_tokens),
        reasoning_tokens=reasoning_tokens,
    )


_USAGE_FIELDS = (
    'prompt_tokens',
    'completion_tokens',
    'prompt_cache_hit_tokens',
    'prompt_cache_miss_tokens',
    'total_tokens',
    'reasoning_tokens',
)


def _add_usage(total, usage):
    """把单次请求的 usage 逐项累加到总计，返回覆盖整轮对话的总用量。

    工具调用循环中每次请求都会返回各自独立的 usage，首次调用创建累计对象，
    之后每轮相加，避免只保留最后一次请求、导致统计与官方账单对不上。
    """
    if usage is None:
        return total
    if total is None:
        total = SimpleNamespace(**{f: 0 for f in _USAGE_FIELDS})
    for f in _USAGE_FIELDS:
        setattr(total, f, getattr(total, f, 0) + (getattr(usage, f, 0) or 0))
    return total


async def stream_responses_api(client, messages, session_id=None):
    """异步调用 Responses API 流式接口，返回 (content, reasoning, tool_calls, output_items, usage)

    tool_calls 为聊天格式 dict 列表（兼容 DB 存储与 process_tool_calls），
    output_items 为服务端 output 中可回传 items 的原始顺序序列
    （message/reasoning/web_search_call/function_call，按原文回传），
    usage 为聊天格式属性对象（prompt_tokens/completion_tokens/...）。
    会话标识通过 extra_headers 携带（如 OpenCode 系 /responses 模型）。
    """
    instructions, input_items = _split_instructions(messages)

    if config.DEBUG_SEND_SEQ:
        _dbg = []
        for _m in input_items:
            _role = _m.get('role', _m.get('type', '?'))
            _blob = json.dumps(_m, ensure_ascii=False, sort_keys=True)
            _dbg.append(f"{_role}:{hashlib.md5(_blob.encode()).hexdigest()[:6]}")
        print("DBGSEQ> " + " | ".join(_dbg))

    create_kwargs = {
        "model": config.DEEPSEEK_MODEL,
        "input": input_items,
        "tools": to_responses_tools(TOOLS),
        "tool_choice": "auto",
        "stream": True,
    }
    # system 消息需通过 instructions 参数传回（Responses API 会插入为首条 system 消息）
    if instructions:
        create_kwargs["instructions"] = instructions
    headers = _session_headers(session_id)
    if headers:
        create_kwargs["extra_headers"] = headers
    stream = await client.responses.create(**create_kwargs)

    content = ""
    reasoning = ""
    usage = None
    output_items = []
    search_header_shown = False
    # output_index → 累积中的 function_call
    fc_by_idx = {}

    live_reasoning = LiveReasoning().start()
    live_md = None  # 思考结束后再启动

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
                events.ev_search("call", getattr(item, 'id', ''), first=not search_header_shown)
                search_header_shown = True

        elif ctype.startswith('response.web_search_call.'):
            events.ev_search(ctype.split('.')[-1])

        elif ctype == 'response.function_call_arguments.delta':
            idx = getattr(chunk, 'output_index', None)
            if idx in fc_by_idx:
                fc_by_idx[idx]['arguments'] += getattr(chunk, 'delta', '') or ''

        elif ctype == 'response.reasoning_text.delta':
            delta = getattr(chunk, 'delta', '') or ''
            if delta:
                reasoning += delta
                live_reasoning.feed(delta)

        elif ctype == 'response.output_text.delta':
            delta = getattr(chunk, 'delta', '') or ''
            if delta:
                content += delta
                if live_md is None:
                    # 思考结束，切换到 Markdown 渲染
                    live_reasoning.finish()
                    if reasoning:
                        events.ev_reasoning_header()
                    live_md = LiveMarkdown().start()
                live_md.feed(delta)

        elif ctype == 'response.completed':
            resp = getattr(chunk, 'response', None)
            if resp is not None:
                usage = _normalize_usage(getattr(resp, 'usage', None))
                output_items = _extract_output_items(getattr(resp, 'output', None))

        elif ctype == 'response.failed':
            resp = getattr(chunk, 'response', None)
            err = ''
            if resp is not None:
                e = getattr(resp, 'error', None)
                if e is not None:
                    err = getattr(e, 'message', '') or str(e)
            raise RuntimeError(f"Responses API 请求失败: {err}")

    # 流式结束，关闭 Live 渲染
    if live_md is not None:
        live_md.finish()
    else:
        live_reasoning.finish()

    tool_calls = []
    for idx in sorted(k for k in fc_by_idx if k is not None):
        fc = fc_by_idx[idx]
        tool_calls.append({
            "id": fc['id'],
            "type": "function",
            "function": {"name": fc['name'], "arguments": fc['arguments']},
        })

    return content, reasoning, tool_calls, output_items, usage


def _delta_reasoning(delta):
    """读取思考增量：DeepSeek 返回 reasoning_content；兼容 SDK 放入 model_extra 的情况"""
    value = getattr(delta, 'reasoning_content', None)
    if value is None:
        extra = getattr(delta, 'model_extra', None) or {}
        value = extra.get('reasoning_content')
    return value or ''


async def stream_chat_completions(client, messages, session_id=None):
    """异步调用 OpenAI 兼容 Chat Completions 流式接口（OpenCode Zen / OpenCode Go 等）。

    返回签名与 stream_responses_api 完全一致，便于上层工具循环无差别复用：
    - tool_calls 为聊天格式 dict 列表（跨 delta 按 index 聚合）
    - output_items 恒为空列表（Chat Completions 无服务端 output items 概念）
    - usage 已归一化为聊天格式属性对象
    会话标识通过 extra_headers 携带（config.LLM_SESSION_HEADER，如 x-opencode-session）。
    """
    chat_messages = to_chat_messages(messages)

    kwargs = {
        "model": config.DEEPSEEK_MODEL,
        "messages": chat_messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    tools = to_chat_tools(TOOLS)
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    headers = _session_headers(session_id)
    if headers:
        kwargs["extra_headers"] = headers

    if config.DEBUG_SEND_SEQ:
        _dbg = []
        for _m in chat_messages:
            _blob = json.dumps(_m, ensure_ascii=False, sort_keys=True)
            _dbg.append(f"{_m.get('role', '?')}:{hashlib.md5(_blob.encode()).hexdigest()[:6]}")
        print("DBGSEQ> " + " | ".join(_dbg))

    try:
        stream = await client.chat.completions.create(**kwargs)
    except Exception as e:
        # 部分网关不支持 stream_options.include_usage → 去掉后重试一次
        if 'stream_options' not in str(e).lower():
            raise
        kwargs.pop('stream_options', None)
        stream = await client.chat.completions.create(**kwargs)

    content = ""
    reasoning = ""
    usage = None
    tool_acc = {}  # index → {'id', 'name', 'arguments'}

    live_reasoning = LiveReasoning().start()
    live_md = None  # 思考结束后再启动

    async for chunk in stream:
        chunk_usage = getattr(chunk, 'usage', None)
        if chunk_usage is not None:
            usage = _normalize_chat_usage(chunk_usage)

        choices = getattr(chunk, 'choices', None) or []
        if not choices:
            continue
        delta = getattr(choices[0], 'delta', None)
        if delta is None:
            continue

        r = _delta_reasoning(delta)
        if r:
            reasoning += r
            live_reasoning.feed(r)

        dcontent = getattr(delta, 'content', None)
        if dcontent:
            content += dcontent
            if live_md is None:
                # 思考结束，切换到 Markdown 渲染
                live_reasoning.finish()
                if reasoning:
                    events.ev_reasoning_header()
                live_md = LiveMarkdown().start()
            live_md.feed(dcontent)

        for tc in (getattr(delta, 'tool_calls', None) or []):
            idx = getattr(tc, 'index', 0)
            slot = tool_acc.setdefault(idx, {'id': None, 'name': '', 'arguments': ''})
            if getattr(tc, 'id', None):
                slot['id'] = tc.id
            fn = getattr(tc, 'function', None)
            if fn is not None:
                if getattr(fn, 'name', None):
                    slot['name'] = fn.name
                if getattr(fn, 'arguments', None):
                    slot['arguments'] += fn.arguments

    # 流式结束，关闭 Live 渲染
    if live_md is not None:
        live_md.finish()
    else:
        live_reasoning.finish()

    tool_calls = []
    for idx in sorted(k for k in tool_acc if k is not None):
        fc = tool_acc[idx]
        tool_calls.append({
            "id": fc['id'],
            "type": "function",
            "function": {"name": fc['name'], "arguments": fc['arguments']},
        })

    return content, reasoning, tool_calls, [], usage


async def stream_llm(client, messages, session_id=None):
    """按 config.DEEPSEEK_API_TYPE 分发到对应协议的流式实现：

    responses = DeepSeek 官方 Responses API；chat = OpenAI 兼容 Chat Completions。
    两者返回签名一致，上层工具调用循环无需感知协议差异。
    """
    if config.DEEPSEEK_API_TYPE == 'chat':
        return await stream_chat_completions(client, messages, session_id=session_id)
    return await stream_responses_api(client, messages, session_id=session_id)


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


def _persist(session_id, ov_session_id, msgs):
    """统一持久化出口：写本地库 + 同步 OV 会话，保证每条消息只落一次。"""
    if session_id:
        db.append_messages(session_id, msgs)
    if ov_session_id:
        openviking_capture(ov_session_id, msgs)


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

      返回: (content, reasoning, total_usage, assistant_msg, all_tool_results, new_history_messages)
      total_usage 为整轮对话内所有请求 usage 的累计值，用于准确的 token/费用统计。
    """
    # 累计整轮对话（含所有工具轮）的 token 用量
    total_usage = None

    # ---- 请求 1.1：工具 + 问题 → 思维链 + 工具调用 ----
    content, reasoning, tool_calls, output_items, usage = await stream_llm(client, messages, session_id=session_id)
    total_usage = _add_usage(total_usage, usage)
    web_search_calls = [oi for oi in output_items if oi.get('type') == 'web_search_call']
    assistant_msg = build_assistant_msg(content, reasoning, tool_calls, output_items)

    # 无工具调用 → 直接返回最终回答（先落库，避免简单问答的回复丢失）
    if not tool_calls and not web_search_calls:
        _persist(session_id, ov_session_id, [assistant_msg])
        return content, reasoning, total_usage, assistant_msg, [], [assistant_msg]

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

        events.ev_tool_round(tool_rounds, len(tool_calls), len(web_search_calls))

        # 一次并发执行本轮全部 function 调用（异步无同步屏障），结果一次性回传；
        # web_search_call 由服务端自动执行，仅随 assistant 消息原样回传供恢复结果
        tool_results = await process_tool_calls(tool_calls)
        all_tool_results.extend(tool_results)

        new_history_messages.append(assistant_msg)
        new_history_messages.extend(tool_results)
        messages.append(assistant_msg)
        messages.extend(tool_results)
        _persist(session_id, ov_session_id, [assistant_msg] + tool_results)

        # ---- 每步召回：工具结果回来后，基于完整批次重新检索相关记忆并注入 ----
        # 对齐官方 pre-step recall：query 含工具结果，下一次模型调用即带上新线索
        step_recall = openviking_load_context(messages, session_id=session_id)
        if step_recall:
            recall_msg = {"role": "user", "content": wrap_recall_block(step_recall)}
            messages.append(recall_msg)
            _persist(session_id, ov_session_id, [recall_msg])

        # ---- 请求 1.N+1：思维链 + 工具调用 + 调用结果 → 回答或继续 ----
        events.ev_continuing(tool_rounds)
        content, reasoning, tool_calls, output_items, usage = await stream_llm(client, messages, session_id=session_id)
        total_usage = _add_usage(total_usage, usage)
        web_search_calls = [oi for oi in output_items if oi.get('type') == 'web_search_call']
        assistant_msg = build_assistant_msg(content, reasoning, tool_calls, output_items)

    # ---- 预算耗尽但仍想调工具 → 强制给出最终回答 ----
    if forced_final:
        force_msg = {
            "role": "user",
            "content": "已达到工具调用次数上限，请不要再调用工具，直接基于已有信息给出最终回答。"
        }
        events.ev_warning("工具调用次数已达上限，强制基于已有结果给出最终回答", code="max_tool_rounds")
        messages.append(force_msg)
        step_recall = openviking_load_context(messages, session_id=session_id)
        if step_recall:
            recall_msg = {"role": "user", "content": wrap_recall_block(step_recall)}
            messages.append(recall_msg)
            _persist(session_id, ov_session_id, [recall_msg])
        content, reasoning, tool_calls, output_items, usage = await stream_llm(client, messages, session_id=session_id)
        total_usage = _add_usage(total_usage, usage)
        web_search_calls = [oi for oi in output_items if oi.get('type') == 'web_search_call']
        assistant_msg = build_assistant_msg(content, reasoning, tool_calls, output_items)
        new_history_messages.append(force_msg)

    new_history_messages.append(assistant_msg)
    _persist(session_id, ov_session_id, [assistant_msg])

    return content, reasoning, total_usage, assistant_msg, all_tool_results, new_history_messages
