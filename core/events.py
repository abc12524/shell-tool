#!/usr/bin/env python3
"""语义事件层：服务端只输出“变化的数据”，展示格式由各客户端自行构建。

- API 模式（DP_API_MODE=1）：每个事件写成一行 JSON（见 core/console.emit），
  由 server/api.py 原样转成 SSE，客户端按类型渲染。
- 终端模式：这里用 rich/print 直接渲染。终端只保留正文流式输出、工具调用明细与
  末尾的峰谷/计费统计；开头提示（存储后端、用户问题、记忆检索）与分割线一律不打，
  以免打断阅读。

新增事件只需在这里加一个 ev_xxx，终端渲染与 API 数据一次写好。
"""
import json

from rich.console import Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from .console import console, emit, render_table, _API_MODE

# 终端预览上限：结果框最多 15 行（再按字符兜底，防单行超长）；
# 调用框通常本来就短，只按字符限制。
_RESULT_MAX_LINES = 15
_RESULT_MAX_CHARS = 1500
_CALL_JSON_LIMIT = 1200
_CALL_TEXT_LIMIT = 300

# 工具框统一的浅灰描边（不再按调用/结果/错误区分颜色）
_PANEL_BORDER = "grey50"


def _clip_lines(text, limit=_RESULT_MAX_LINES, what=""):
    """按行截断，返回 (正文, 说明)。截断时留一行给说明，保证框内不超过 limit 行。"""
    lines = (text or "").splitlines()
    if len(lines) <= limit:
        return text or "", None
    keep = max(0, limit - 1)
    return "\n".join(lines[:keep]), f"… {what}已截断（共 {len(lines)} 行）"


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


def _json_view(value, char_limit, max_lines=None):
    """dict/list → 缩进 2 的彩色 JSON（含截断说明）"""
    try:
        pretty = json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        pretty = str(value)
    body, note = _clipped(pretty, char_limit, max_lines)
    parts = [_syntax_view(body)]
    if note:
        parts.append(Text(note, style="dim"))
    return parts


def _text_view(text, char_limit, max_lines=None):
    """纯文本正文（含截断说明）"""
    body, note = _clipped(text, char_limit, max_lines)
    parts = [Text(body)]
    if note:
        parts.append(Text(note, style="dim"))
    return parts


def _payload_parts(text):
    """结果正文：能解析成 JSON 就格式化高亮，否则纯文本；最多 15 行"""
    text = (text or "").strip()
    if not text:
        return [Text("（空）", style="dim")]
    try:
        return _json_view(json.loads(text), _RESULT_MAX_CHARS, _RESULT_MAX_LINES)
    except (ValueError, TypeError):
        return _text_view(text, _RESULT_MAX_CHARS, _RESULT_MAX_LINES)


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


def _diff_parts(result):
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
    # 摘要占一行，正文与截断说明合计不超过 _RESULT_MAX_LINES 行
    body, note = _clipped(diff, _RESULT_MAX_CHARS, _RESULT_MAX_LINES - 1, what="diff ")
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
            parts.extend(_json_view(arguments, _CALL_JSON_LIMIT))
        elif raw:
            parts.extend(_text_view(raw, _CALL_TEXT_LIMIT))
        else:
            parts.append(Text("（无参数）", style="dim"))
        console.print(_tool_panel(parts, f"🔧 执行工具 · {name}"))
    _ev("tool_call", payload, t)


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
        # script 类写动作：结果里带 diff，就展示摘要 + 彩色 diff，而不是整段 JSON
        if not error and isinstance(result, dict) and isinstance(result.get("diff"), str):
            console.print(_tool_panel(_diff_parts(result), f"📝 变更 · {name}"))
            return
        console.print(_tool_panel(
            _payload_parts(full),
            ("❌ 结果 · " if error else "📤 结果 · ") + name,
        ))
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
