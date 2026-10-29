#!/usr/bin/env python3
"""语义事件层：服务端只输出“变化的数据”，展示格式由各客户端自行构建。

- API 模式（DP_API_MODE=1）：每个事件写成一行 JSON（见 core/console.emit），
  由 server/api.py 原样转成 SSE，客户端按类型渲染。
- 终端模式：这里用 rich/print 直接渲染。终端只保留正文流式输出、工具调用明细与
  末尾的峰谷/计费统计；开头提示（存储后端、用户问题、记忆检索）与分割线一律不打，
  以免打断阅读。
- 工具调用与结果共用同一个框：调用到达时先缓存，结果到达后再一次性输出。
- 结果先剥掉 {status, result} 信封（外壳字段压成一行 meta），再按 result 自身内容
  渲染：JSON → 高亮；Markdown → 渲染；其余 → 纯文本。

新增事件只需在这里加一个 ev_xxx，终端渲染与 API 数据一次写好。
"""
import json
import re

from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text

from .console import console, emit, render_table, _API_MODE

# 终端显示阈值（折叠 = 超出部分不显示，末尾用一行说明还有多少行）：
#   调用参数：缩进后 ≤5 行压成一行；5~15 行展开；>15 行折叠。
#   工具结果：>20 行折叠；script 编辑（带 diff）放宽到 >30 行才折叠。
#   再按字符兜底，防止单行超长撑爆屏幕。
_CALL_INLINE_LINES = 5
_CALL_MAX_LINES = 15
_CALL_JSON_LIMIT = 1200
_CALL_MAX_CHARS = 4000
_CALL_TEXT_LIMIT = 300
_RESULT_MAX_LINES = 20
_RESULT_DIFF_MAX_LINES = 30
_RESULT_MAX_CHARS = 4000

# 工具框统一的浅灰描边（不再按调用/结果/错误区分颜色）
_PANEL_BORDER = "grey50"

# 调用框缓存：call_id -> (工具名, 已渲染的调用部分)。
# 调用事件只入缓存不输出，等结果事件到达后合并成一个框打印；并发工具各自独立。
_PENDING_CALLS = {}


def _call_key(call_id, name):
    """调用/结果关联键：优先 call_id，缺失时退化为工具名"""
    return call_id or f"::{name}::"


def _clip_lines(text, limit=_RESULT_MAX_LINES, what=""):
    """按行折叠，返回 (正文, 说明)。折叠时留一行给说明，保证框内不超过 limit 行。"""
    lines = (text or "").splitlines()
    if len(lines) <= limit:
        return text or "", None
    keep = max(0, limit - 1)
    return "\n".join(lines[:keep]), f"… {what}还有 {len(lines) - keep} 行已折叠（共 {len(lines)} 行）"


def _clip_chars(text, limit, what=""):
    """按字符兜底截断（单行超长时用），返回 (正文, 说明)"""
    text = text or ""
    if len(text) <= limit:
        return text, None
    return text[:limit], f"… {what}已截断（共 {len(text)} 字符）"


def _clipped(text, char_limit, max_lines=None, what=""):
    """通用截断：先按行，再按字符；返回 (正文, 说明)"""
    body, note = text or "", None
    if max_lines:
        body, note = _clip_lines(body, max_lines, what)
    body, note2 = _clip_chars(body, char_limit, what)
    return body, note or note2


def _syntax_view(text):
    """JSON 高亮；pygments 缺失时退化为纯文本"""
    try:
        return Syntax(text, "json", theme="ansi_dark", background_color="default", word_wrap=True)
    except Exception:
        return Text(text, style="cyan")


_JSON_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|.)", re.S)


def _escape_repl(match):
    """反转义表中的换行相关项，其余（\\" \\\\ \\uXXXX）原样保留"""
    ch = match.group(1)
    return {"n": "\n", "r": "", "t": "\t"}.get(ch, match.group(0))


def _expand_newlines(text):
    """把 JSON 里转义的 \\n / \\r / \\t 还原成真实字符。

    命令输出、脚本内容、skill 结果通常是塞在字符串里的多行文本，直接按 JSON 字面
    显示会变成一条超长单行，既看不清也无法按行折叠。展开后按真实行统计与折叠。
    """
    if "\\" not in text:
        return text
    return _JSON_ESCAPE.sub(_escape_repl, text)


def _json_view(value, char_limit, max_lines=None, inline=0, what="", inline_chars=None):
    """dict/list → 缩进 2 的彩色 JSON（含折叠说明）。

    inline>0 时：展开换行后行数不超过 inline 就压成单行显示（短参数不占屏幕）。
    """
    try:
        pretty = json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        pretty = str(value)
    pretty = _expand_newlines(pretty)
    if inline and len(pretty.splitlines()) <= inline:
        try:
            pretty = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
        except (TypeError, ValueError):
            pretty = pretty.replace("\n", " ")
        body, note = _clip_chars(pretty, inline_chars or char_limit, what)
    else:
        body, note = _clipped(pretty, char_limit, max_lines, what)
    parts = [_syntax_view(body)]
    if note:
        parts.append(Text(note, style="dim"))
    return parts



def _text_view(text, char_limit, max_lines=None, what=""):
    """纯文本正文（含折叠说明）"""
    body, note = _clipped(text, char_limit, max_lines, what)
    parts = [Text(body)]
    if note:
        parts.append(Text(note, style="dim"))
    return parts


# Markdown 识别：按「信号加权」判断，单一信号重复出现只计一次分，
# 避免把带 # 注释的脚本输出、列表状日志误判成 Markdown。
_MD_PATTERNS = (
    (re.compile(r"^\s*```", re.M), 3),                       # 代码块
    (re.compile(r"^\s*\|\s*:?-{3,}", re.M), 3),              # 表格分隔行
    (re.compile(r"^\s*#{2,6}\s", re.M), 2),                  # ## 及更深标题
    (re.compile(r"^\s*#\s", re.M), 1),                       # # 一级标题
    (re.compile(r"\[[^\]\n]+\]\([^)\n]+\)", re.M), 2),       # 链接
    (re.compile(r"\*\*[^*\n]+\*\*", re.M), 1),               # 加粗
    (re.compile(r"^\s*[-*+]\s+\S", re.M), 1),                # 无序列表
    (re.compile(r"^\s*\d+\.\s+\S", re.M), 1),                # 有序列表
    (re.compile(r"^\s*>\s+\S", re.M), 1),                    # 引用
)

# 结果 dict 里常见的「正文」字段：优先按文本/Markdown 展示，其余字段压成一行 meta
_TEXT_KEYS = ("output", "stdout", "stderr", "content", "text", "message", "body", "detail")


def _is_markdown(text):
    """粗判一段文本是否是 Markdown（宁可漏判成纯文本，也不要把纯文本渲染乱）"""
    return sum(weight for pattern, weight in _MD_PATTERNS if pattern.search(text)) >= 3


def _auto_parts(text, max_lines, char_limit, what="结果 "):
    """按内容自动选渲染方式：JSON → 高亮；Markdown → 渲染；其他 → 纯文本。"""
    raw = (text or "").strip()
    if not raw:
        return [Text("（空）", style="dim")]
    if raw[:1] in "{[":
        try:
            return _json_view(json.loads(raw), char_limit, max_lines, what=what)
        except (ValueError, TypeError):
            pass
    if _is_markdown(raw):
        body, note = _clipped(raw, char_limit, max_lines, what=what)
        parts = [Markdown(body)]
        if note:
            parts.append(Text(note, style="dim"))
        return parts
    return _text_view(raw, char_limit, max_lines, what=what)


def _meta_line(items):
    """信封/结果里的标量字段压成一行 dim 文本（truncated 之类的告警标黄）"""
    line = Text()
    for i, (key, value) in enumerate(items):
        if i:
            line.append(" · ", style="dim")
        warn = key in ("truncated", "diff_truncated") and bool(value)
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        line.append(f"{key}={shown}", style="yellow" if warn else "dim")
    return line


def _labeled(label, parts):
    """多个正文段时给每段加一行 dim 小标题"""
    return [Text(label, style="dim")] + list(parts)


def _envelope_parts(env, max_lines, char_limit):
    """{status, result} 信封：外壳压成一行 meta，result 内部按内容类型渲染。"""
    status = env.get("status")
    result = env.get("result")
    extras = [
        (k, v) for k, v in env.items()
        if k not in ("status", "result", "error")
        and not isinstance(v, (dict, list)) and v is not False
    ]

    if status == "error":
        parts = []
        if env.get("error"):
            parts.append(Text(str(env["error"]), style="bold red"))
        if extras:
            parts.append(_meta_line(extras))
        body = result if isinstance(result, str) else _dumps(result)
        if body not in ("", "null"):
            parts.extend(_auto_parts(body, max_lines - len(parts), char_limit))
        if not parts:
            parts.extend(_json_view(env, char_limit, max_lines, what="结果 "))
        return parts

    # 成功的 result：字符串 / 字典 / 列表分别处理
    meta = list(extras)
    bodies = []      # [(label|None, text)]
    if result in (None, "", {}, []):
        parts = [Text("（空）", style="dim")]
        if meta:
            parts.insert(0, _meta_line(meta))
        return parts
    if isinstance(result, str):
        bodies.append((None, result))
    elif isinstance(result, dict):
        texts, scalars = [], []
        for key, value in result.items():
            if key in _TEXT_KEYS and isinstance(value, str) and value:
                texts.append((key, value))
            elif isinstance(value, (dict, list)):
                pass
            elif value is not False:
                scalars.append((key, value))
        if texts:
            bodies = texts
            meta.extend(scalars)
        else:
            # 没有正文字段：整体按 JSON 高亮，不再重复罗列同一批字段
            parts = []
            if meta:
                parts.append(_meta_line(meta))
            parts.extend(_json_view(result, char_limit, max_lines - len(parts), what="结果 "))
            return parts
    else:
        bodies.append((None, _dumps(result)))

    parts = [_meta_line(meta)] if meta else []
    remaining = max(2, max_lines - len(parts))
    for i, (label, text) in enumerate(bodies):
        left = len(bodies) - i
        budget = max(2, remaining // left)
        body_parts = _auto_parts(text, budget, char_limit)
        if label and len(bodies) > 1:
            parts.extend(_labeled(label, body_parts))
        else:
            parts.extend(body_parts)
        remaining = max(2, remaining - budget)
    return parts


def _dumps(value):
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _payload_parts(text, max_lines=_RESULT_MAX_LINES, char_limit=_RESULT_MAX_CHARS):
    """结果正文：{status,result} 信封剥壳后按 result 内容渲染；其余按 JSON/Markdown/文本。"""
    text = (text or "").strip()
    if not text:
        return [Text("（空）", style="dim")]
    try:
        env = json.loads(text)
    except (ValueError, TypeError):
        return _auto_parts(text, max_lines, char_limit)
    if isinstance(env, dict) and "status" in env:
        return _envelope_parts(env, max_lines, char_limit)
    return _auto_parts(text, max_lines, char_limit)


def _tool_panel(parts, title, border=_PANEL_BORDER):
    """工具调用/结果统一用浅灰框包起来：宽度自适应内容，前面留一空行"""
    body = parts[0] if len(parts) == 1 else Group(*parts)
    panel = Panel(
        body,
        title=Text(title, style="bold"),
        title_align="left",
        border_style=border,
        padding=(0, 1),
        expand=False,   # 不顶右边，按内容宽度收缩（超过终端宽度时自动换行）
    )
    return Group(Text(""), panel)


def _section(label, style="grey50", label_style="dim"):
    """框内分区线：既是分隔线，也是结果/错误的标签（不额外占一行）"""
    return Rule(Text(label, style=label_style), align="left", style=style)


def _diff_text(text):
    """unified diff 着色：文件头加粗 / @@ 青色 / + 绿 / - 红 / 上下文灰"""
    body = Text()
    for i, line in enumerate(text.splitlines()):
        if i:
            body.append("\n")
        if line.startswith(("+++", "---", "diff ")):
            body.append(line, style="bold")
        elif line.startswith("@@"):
            body.append(line, style="cyan")
        elif line.startswith("+"):
            body.append(line, style="green")
        elif line.startswith("-"):
            body.append(line, style="red")
        else:
            body.append(line, style="dim")
    return body


def _diff_parts(result, max_lines=_RESULT_DIFF_MAX_LINES):
    """带 diff 的结果（script 的写动作）：一行摘要 + 彩色 diff，不再回显整段 JSON。"""
    head = " · ".join(str(result[k]) for k in ("action", "file") if result.get(k))
    stats = []
    lines = result.get("lines")
    if isinstance(lines, list) and len(lines) == 2:
        stats.append(f"行数 {lines[0]}→{lines[1]}")
    for key, value in result.items():
        if key in ("action", "file", "diff", "lines", "diff_truncated"):
            continue
        if isinstance(value, (dict, list)):
            stats.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
        else:
            stats.append(f"{key}={value}")
    if result.get("diff_truncated"):
        stats.append("diff 已折叠")
    summary = Text()
    summary.append(head or "diff", style="bold")
    if stats:
        summary.append("   " + " · ".join(stats), style="dim")

    diff = result.get("diff") or ""
    # 摘要占一行，diff 正文与折叠说明合计不超过 max_lines 行（script 编辑放宽到 30）
    body, note = _clipped(diff, _RESULT_MAX_CHARS, max_lines - 1, what="diff ")
    parts = [summary]
    if body:
        parts.append(_diff_text(body))
    if note:
        parts.append(Text(note, style="dim"))
    return parts



def _ev(kind, payload, terminal=None):
    """统一出口：API 模式发事件；终端模式调用 terminal() 渲染"""
    if _API_MODE:
        emit(kind, **payload)
    elif terminal is not None:
        terminal()


def ev_config(keys):
    """-k/-m 覆盖并写回 .env"""
    def t():
        print(f"⚙️  已更新 .env：{'、'.join(keys)}")
    _ev("config", {"keys": list(keys)}, t)


def ev_db(online):
    _ev("db", {"online": bool(online)}, None)


def ev_session(action, session_id):
    """action: new | resume | saved（终端静默，仅 API 事件）"""
    _ev("session", {"action": action, "id": session_id}, None)


def ev_memory(phase, found):
    """phase: profile(加载记忆索引) | recall(搜索相关记忆)（终端静默，仅 API 事件）"""
    _ev("memory", {"phase": phase, "found": bool(found)}, None)


def ev_question(text):
    _ev("question", {"text": text}, None)


def ev_reasoning_header():
    _ev("reasoning_header", {}, None)


def ev_tool_round(index, local, search):
    # 上一轮若有调用没等到结果（被中断），先兜底打出来，进入新一轮
    if not _API_MODE:
        flush_pending_calls()
    _ev("tool_round", {"index": index, "local": local, "search": search}, None)


def ev_continuing(index):
    _ev("continuing", {"index": index}, None)


def ev_tool_call(name, arguments=None, raw=None, call_id=None, parse_error=None):
    payload = {"name": name, "id": call_id}
    if arguments is not None:
        payload["arguments"] = arguments
    if raw is not None:
        payload["raw"] = raw
    if parse_error:
        payload["parse_error"] = parse_error

    def t():
        parts = []
        if parse_error:
            parts.append(Text(f"⚠️ 参数解析失败: {parse_error}", style="bold red"))
        if arguments is not None:
            parts.extend(_json_view(
                arguments, _CALL_MAX_CHARS, _CALL_MAX_LINES,
                inline=_CALL_INLINE_LINES, what="参数 ", inline_chars=_CALL_JSON_LIMIT,
            ))
        elif raw:
            parts.extend(_text_view(raw, _CALL_TEXT_LIMIT, _CALL_MAX_LINES, what="参数 "))
        else:
            parts.append(Text("（无参数）", style="dim"))
        # 先缓存，等结果到达时合并到同一个框里输出
        _PENDING_CALLS[_call_key(call_id, name)] = (name, parts)
    _ev("tool_call", payload, t)


def flush_pending_calls():
    """结果一直没到（执行被中断）时兜底输出，避免调用信息丢失。"""
    while _PENDING_CALLS:
        name, parts = _PENDING_CALLS.popitem()[1]
        console.print(_tool_panel(list(parts), f"🔧 {name}"))


def ev_tool_result(name, call_id, output, truncated=False, error=False):
    max_len = 2000
    full = output or ""
    payload = {
        "name": name, "id": call_id,
        "output": full[:max_len],
        "truncated": bool(truncated or len(full) > max_len),
        "error": bool(error),
    }

    def t():
        env = None
        try:
            env = json.loads(full)
        except (ValueError, TypeError):
            pass
        result = env.get("result") if isinstance(env, dict) else None
        pending = _PENDING_CALLS.pop(_call_key(call_id, name), None)
        if pending:
            tool_name, parts = pending[0], list(pending[1])
        else:
            tool_name, parts = name, []
        # script 类写动作：结果里带 diff，就展示摘要 + 彩色 diff，而不是整段 JSON
        if not error and isinstance(result, dict) and isinstance(result.get("diff"), str):
            parts.append(_section("📝 变更"))
            parts.extend(_diff_parts(result))
        elif error:
            parts.append(_section("❌ 错误", style="red", label_style="bold red"))
            parts.extend(_payload_parts(full))
        else:
            parts.append(_section("📤 结果"))
            parts.extend(_payload_parts(full))
        console.print(_tool_panel(parts, f"🔧 {tool_name}"))
    _ev("tool_result", payload, t)


def ev_search(state, call_id=None, first=False):
    """state: call(发起一次调用) | searching/completed/... (服务端状态)"""
    def t():
        if state == "call":
            if first:
                console.print("\n🔎 服务端网页搜索：", style="bold cyan")
            console.print(f"  - 搜索调用 {call_id} 已发起", style="cyan")
        else:
            console.print(f"  - 搜索状态: {state}", style="cyan")
    _ev("search", {"state": state, "id": call_id, "first": bool(first)}, t)


def ev_warning(message, code=None, silent=False):
    """silent=True 时仅在 API 模式发事件，终端不打印。"""
    payload = {"message": message}
    if code:
        payload["code"] = code

    def t():
        console.print(f"⚠️  {message}", style="bold red")
    _ev("warning", payload, None if silent else t)


def ev_usage(model, peak, hit, miss, out, costs, total, balance, currency):
    from . import billing
    symbol = billing.currency_symbol()
    period = billing.period_label(bool(peak))
    payload = {
        "model": model, "peak": bool(peak),
        "hit": hit, "miss": miss, "out": out,
        "cost_hit": costs["hit"], "cost_miss": costs["miss"], "cost_out": costs["out"],
        "total": total, "balance": balance, "currency": currency or billing.currency(),
        "symbol": symbol, "period": period,
    }

    def t():
        # 收尾：异常中断时残留的调用框先补输出
        flush_pending_calls()
        console.print(f"\n⏰ {period}", style="bold")
        rows = [
            ("输入(缓存命中)", f"{hit:,} tokens  {symbol}{costs['hit']:.4f}"),
            ("输入(缓存未命中)", f"{miss:,} tokens  {symbol}{costs['miss']:.4f}"),
            ("输出", f"{out:,} tokens  {symbol}{costs['out']:.4f}"),
        ]
        render_table(f"📊 Token 消耗统计 · {model} · 合计 {symbol}{total:.4f}", rows)
        if balance is not None:
            console.print(f"💰 账户余额：{symbol}{balance} {currency or billing.currency()}", style="bold green")
        else:
            console.print("💰 账户余额：查询失败", style="dim")
    _ev("usage", payload, t)
