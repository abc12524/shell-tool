#!/usr/bin/env python3
"""shell-tool 流式对话客户端：消费 /chat/stream 的语义事件流并渲染。

服务端只发送“变化的数据”，展示格式（emoji/颜色/表格）全部由本客户端构建——
这里就是 Android 等客户端的参考实现。

事件类型（与服务端约定，详见 core/events.py）：
  start                                  会话开始
  content   {content}                    正文增量（Markdown）
  reasoning {content}                    思考过程增量（暗色斜体）
  question  {text}                       用户问题回显
  db        {online}                     数据库模式
  session   {action,id}                  new/resume/saved
  memory    {phase,found}                profile/recall
  config    {keys}                       -k/-m 更新的 .env 项
  reasoning_header {}                    思考过程分隔线
  tool_round{index,local,search}         工具轮次
  continuing{index}                      继续推理
  tool_call {name,arguments,id,parse_error}
  tool_result{name,id,output,truncated,error}
  search    {state,id,first}             服务端网页搜索
  warning   {message,code}
  usage     {model,peak,hit,miss,out,cost_hit,cost_miss,cost_out,total,balance,currency}
  log       {text}                       未结构化的兜底文本
  error     {error,code}                 出错，客户端非零退出
  done                                   正常结束
  以 ':' 开头的行（含 ": heartbeat"）为注释/心跳，忽略

用法：
  dp.py [-H 主机] [-P 端口] [-n] [-t 超时秒] [-k KEY] [-m MODEL] <问题...>
环境变量 DP_HOST / DP_PORT / DP_TIMEOUT 可覆盖默认值。
"""
import argparse
import json
import os
import re
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme


def _force_utf8(stream):
    """强制标准流使用 UTF-8，避免 Windows 管道下 Rich 退化为 GBK/ASCII 导致乱码。

    管道下同时关闭 \\n -> \\r\\n 翻译，防止下游再次消费时把 CR 当行内字符。
    """
    try:
        if stream.isatty():
            stream.reconfigure(encoding="utf-8")
        else:
            stream.reconfigure(encoding="utf-8", newline="\n")
    except (AttributeError, ValueError, OSError):
        pass


_force_utf8(sys.stdout)
_force_utf8(sys.stderr)

# ── 终端视觉令牌 ────────────────────────────────────────────────────────────
# 与 core/console.py 的 THEME 保持一致（客户端不依赖 core，这里保留一份副本）。
# 颜色一律用固定灰阶（greyN）而非 "dim"：dim 是降亮度（SGR 2），在浅色终端
# 里会把中灰压到接近白色而不可见；greyN 是固定索引色，浅底/深底都可读。
THEME = Theme({
    "accent":       "bold cyan",     # 主强调：标题、提示符
    "title":        "bold",          # 区块标题
    "muted":        "grey58",        # 次要文字（分隔符、状态副词）
    "hint":         "grey54",        # 操作提示（怎么用）
    "ok":           "green",
    "warn":         "yellow",
    "err":          "bold red",
    "kv.key":       "grey62",        # 清单左列（键/标识）
    "kv.val":       "default",       # 清单右列（跟随终端前景色）
    "kv.num":       "bold",          # 数字列（靠右对齐 + 加粗，不靠颜色区分）
    "panel.border": "grey54",        # 工具框描边
    "panel.title":  "bold cyan",     # 工具框标题
    "section.label": "grey58",       # 框内分区标题
    "tbl.head":     "grey54",        # 表头
    # 行内代码：rich 默认「bold cyan on black」，浅色终端里那块黑底很扎眼，去掉底色
    "markdown.code": "cyan",
})

# 思考过程的终端样式（淡色斜体，和正文区分）
_THINK_STYLE = "grey58 italic"

console = Console(highlight=False, theme=THEME)

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def flatten_headings(text: str) -> str:
    """把 Markdown 标题降级成「加粗独立行」。

    rich 的标题是居中大字（h1/h2 还带下划线），在连续的会话输出里像一条装饰横幅；
    左对齐加粗更接近正文重点，也不额外占空行。代码块里的 # 注释保持原样。
    """
    if "#" not in text:
        return text
    lines, in_code = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            lines.append(line)
            continue
        match = None if in_code else _HEADING.match(line)
        lines.append(f"**{match.group(2)}**" if match else line)
    return "\n".join(lines)


def _normalize_newlines(text: str) -> str:
    """统一换行：CRLF / CR -> LF。

    Rich 的 Markdown 只认 LF 作块分隔，直接喂入 CRLF 会把多行挤成一行。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ---- 工具框渲染（与 core/events.py 的终端样式保持一致）----
# 浅灰描边、宽度自适应内容、调用与结果同一个框、JSON 缩进高亮、写动作展示彩色 diff。
#   调用参数：缩进后 ≤5 行压成一行；5~15 行展开；>15 行折叠。
#   工具结果：>20 行折叠；script 编辑（带 diff）放宽到 >30 行才折叠。
#   结果先剥掉 {status,result} 信封，再按 result 内容渲染 JSON / Markdown / 纯文本。
_CALL_INLINE_LINES = 5
_CALL_MAX_LINES = 15
_CALL_JSON_LIMIT = 1200
_CALL_MAX_CHARS = 4000
_CALL_TEXT_LIMIT = 300
_RESULT_MAX_LINES = 20
_RESULT_DIFF_MAX_LINES = 30
_RESULT_MAX_CHARS = 4000

# 工具框描边：默认静灰；出错时只换标题徽标与描边色，不改整体结构
_PANEL_BORDER = "panel.border"
_PANEL_MAX_WIDTH = 88   # 宽终端下不无限拉伸，保证多个框右边界对齐、行宽可控

_PLAIN_SCALAR = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff./:@~^+-]*$")

# 紧凑参数行里单个值的最长显示长度（文件路径、命令可能上百字符，
# 不截断会把面板撑到满屏并折行，反而更难读；完整值在结果框里仍可见）
_COMPACT_VALUE_MAX = 56

# 紧凑参数行整体不超过这个长度才压成一行；超出改为逐行 key=value
_COMPACT_INLINE_MAX = 84


def _call_key(call_id, name):
    """调用/结果关联键：优先 id，缺失时退化为工具名"""
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

    命令输出、脚本内容通常塞在字符串里，按 JSON 字面显示会变成一条超长单行，
    既看不清也无法按行折叠；展开后按真实行统计与折叠。
    """
    if "\\" not in text:
        return text
    return _JSON_ESCAPE.sub(_escape_repl, text)


def _scalar(value):
    """紧凑行里的标量写法：裸值够清楚就不加引号，有空格/特殊字符才用 JSON 引号。"""
    if isinstance(value, str):
        text = value if _PLAIN_SCALAR.match(value) else json.dumps(value, ensure_ascii=False)
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif value is None:
        text = "null"
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    if len(text) > _COMPACT_VALUE_MAX:
        text = text[:_COMPACT_VALUE_MAX - 1] + "…"
    return text


def _compact_view(value, char_limit, what=""):
    """扁平 dict → 一行 `k=v · k=v`（键灰、值亮）；一行放不下就每个参数一行、键左对齐。

    比 JSON 少一半字符：短参数不会把框撑成竖排，扫一眼就知道调了什么、带了什么；
    参数多或值长时改用逐行形式，避免交给 Rich 在空格处乱折行（那会切断 `key=` 与值）。
    """
    pairs = [(str(k), _scalar(v)) for k, v in value.items()]
    one_line = "  ·  ".join(f"{k}={v}" for k, v in pairs)
    if len(one_line) <= min(_COMPACT_INLINE_MAX, char_limit):
        line = Text()
        for i, (key, val) in enumerate(pairs):
            if i:
                line.append("  ·  ", style="panel.border")
            line.append(key, style="kv.key")
            line.append("=", style="panel.border")
            line.append(val)
        return [line]

    width = max(len(k) for k, _ in pairs)
    block = Text()
    for i, (key, val) in enumerate(pairs):
        if i:
            block.append("\n")
        block.append(key.ljust(width), style="kv.key")
        block.append(" = ", style="panel.border")
        block.append(val)
    return [block]


def _json_view(value, char_limit, max_lines=None, inline=0, what="", inline_chars=None):
    """dict/list → 缩进 2 的彩色 JSON（含折叠说明）。

    inline>0 且是「扁平短参数字典」时压成一行 k=v（见 _compact_view）；
    其余情况按缩进 JSON 展示，再按行/字符折叠。
    """
    if (inline and isinstance(value, dict) and value
            and all(not isinstance(v, (dict, list)) for v in value.values())):
        return _compact_view(value, inline_chars or char_limit, what)
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
        parts.append(Text(note, style="muted"))
    return parts


def _text_view(text, char_limit, max_lines=None, what=""):
    """纯文本正文（含折叠说明）"""
    body, note = _clipped(text, char_limit, max_lines, what)
    parts = [Text(body)]
    if note:
        parts.append(Text(note, style="muted"))
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
_TEXT_KEYS = ("output", "stdout", "stderr", "content", "text", "message", "body", "detail", "result")


def _is_markdown(text):
    """粗判一段文本是否是 Markdown（宁可漏判成纯文本，也不要把纯文本渲染乱）"""
    return sum(weight for pattern, weight in _MD_PATTERNS if pattern.search(text)) >= 3


def _auto_parts(text, max_lines, char_limit, what="结果 "):
    """按内容自动选渲染方式：JSON → 高亮；Markdown → 渲染；其他 → 纯文本。"""
    raw = (text or "").strip()
    if not raw:
        return [Text("（空）", style="muted")]
    if raw[:1] in "{[":
        try:
            return _json_view(json.loads(raw), char_limit, max_lines, what=what)
        except (ValueError, TypeError):
            pass
    if _is_markdown(raw):
        body, note = _clipped(raw, char_limit, max_lines, what=what)
        parts = [Markdown(flatten_headings(body), justify="left")]
        if note:
            parts.append(Text(note, style="muted"))
        return parts
    return _text_view(raw, char_limit, max_lines, what=what)


def _dumps(value):
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _meta_line(items):
    """信封/结果里的标量字段压成一行 dim 文本（truncated 之类的告警标黄）"""
    line = Text()
    for i, (key, value) in enumerate(items):
        if i:
            line.append(" · ", style="muted")
        warn = key in ("truncated", "diff_truncated") and bool(value)
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        line.append(f"{key}={shown}", style="yellow" if warn else "dim")
    return line


def _labeled(label, parts):
    """多个正文段时给每段加一行 dim 小标题"""
    return [Text(label, style="muted")] + list(parts)


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
        # 常见的错误体是 {message: "..."}：直接把消息当正文，别让用户读一层 JSON
        message = None
        if isinstance(result, dict):
            message = next((result[k] for k in ("message", "error", "detail")
                            if isinstance(result.get(k), str) and result[k]), None)
        body = message if message else (result if isinstance(result, str) else _dumps(result))
        if body not in ("", "null"):
            parts.extend(_auto_parts(body, max_lines - len(parts), char_limit))
        if not parts:
            parts.extend(_json_view(env, char_limit, max_lines, what="结果 "))
        return parts

    # 成功的 result：字符串 / 字典 / 列表分别处理
    meta = list(extras)
    bodies = []      # [(label|None, text)]
    if result is None or result == "" or result == {} or result == []:
        parts = [Text("（空）", style="muted")]
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


def _payload_parts(text):
    """结果正文：{status,result} 信封剥壳后按 result 内容渲染；其余按 JSON/Markdown/文本。"""
    text = (text or "").strip()
    if not text:
        return [Text("（空）", style="muted")]
    try:
        env = json.loads(text)
    except (ValueError, TypeError):
        return _auto_parts(text, _RESULT_MAX_LINES, _RESULT_MAX_CHARS)
    if isinstance(env, dict) and "status" in env:
        return _envelope_parts(env, _RESULT_MAX_LINES, _RESULT_MAX_CHARS)
    return _auto_parts(text, _RESULT_MAX_LINES, _RESULT_MAX_CHARS)


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
            body.append(line, style="section.label")
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
        summary.append("   " + " · ".join(stats), style="muted")

    diff = result.get("diff") or ""
    # 摘要占一行，diff 正文与折叠说明合计不超过 max_lines 行（script 编辑放宽到 30）
    body, note = _clipped(diff, _RESULT_MAX_CHARS, max_lines - 1, what="diff ")
    parts = [summary]
    if body:
        parts.append(_diff_text(body))
    if note:
        parts.append(Text(note, style="muted"))
    return parts


def _tool_panel(parts, title, status=None):
    """工具调用/结果统一用一个圆角框包起来。

    - 宽度取 min(终端宽, _PANEL_MAX_WIDTH)：同一轮里的多个框宽度一致，右边界成一条线；
    - 标题左侧是「▸ 工具名」，右侧是状态徽标（✓ 完成 / ✗ 失败），一眼能扫出成败；
    - 框前留一空行，与正文、上一个框之间保持呼吸感。
    """
    body = parts[0] if len(parts) == 1 else Group(*parts)
    width = max(24, min(console.width, _PANEL_MAX_WIDTH))
    if status == "error":
        subtitle, border = Text("✗ 失败", style="err"), "red"
    elif status == "ok":
        subtitle, border = Text("✓ 完成", style="ok"), _PANEL_BORDER
    else:
        subtitle, border = None, _PANEL_BORDER
    panel = Panel(
        body,
        title=Text(title, style="panel.title"),
        title_align="left",
        subtitle=subtitle,
        subtitle_align="right",
        border_style=border,
        box=box.ROUNDED,
        padding=(0, 1),
        width=width,
        # 注意：rich 的 Panel 在 expand=False 时会收缩到内容宽度、忽略 width，
        # 想让同轮多个框右边界对齐只能 expand=True + 固定 width。
        expand=True,
    )
    return Group(Text(""), panel)


def _section(label, label_style="section.label"):
    """框内分区标题：单独一行小字。

    原来用 Rule（横线）当分隔，但 Rule 在 Panel 里渲染出的宽度比内容行少 2 列，
    右边框会跟着错位；改成一行淡色小标题，既没有宽度问题，也比横线安静。
    """
    return Text(label, style=label_style)


def _unwrap_arguments(arguments):
    """拆掉 `{skill: x, arguments: {...}}` 这层信封，直接展示真正的参数。

    工具调用普遍是「工具名 + 参数包」两层，外层名字已经在框标题上，
    再留一层缩进会让每个调用多出 3~4 行；摊平后内层参数能压成一行。
    """
    if not isinstance(arguments, dict):
        return arguments
    if len(arguments) > 2:
        return arguments
    for key in ("arguments", "args", "parameters", "params"):
        inner = arguments.get(key)
        if isinstance(inner, dict) and inner:
            rest = {k: v for k, v in arguments.items()
                    if k not in (key, "skill", "tool", "name")}
            return {**rest, **inner}
    return arguments


def _call_label(name, arguments):
    """框标题：`工具名` 或 `工具名 · 目标`（skill/ssh 这类按子项调用的工具，光有工具名看不出在干什么）"""
    if isinstance(arguments, dict):
        for key in ("skill", "tool", "host"):
            value = arguments.get(key)
            if isinstance(value, str) and value and value != name:
                return f"{name} · {value}"
    return name


def _result_parts(evt):
    """工具结果 → 框内正文：diff / 错误 / 普通结果统一在这里分组"""
    out = evt.get("output", "") or ""
    error = bool(evt.get("error"))
    try:
        env = json.loads(out)
    except (ValueError, TypeError):
        env = None
    result = env.get("result") if isinstance(env, dict) else None
    # script 类写动作：结果里带 diff，就展示摘要 + 彩色 diff，而不是整段 JSON
    if not error and isinstance(result, dict) and isinstance(result.get("diff"), str):
        return [_section("变更")] + _diff_parts(result)
    if error:
        return [_section("错误", label_style="bold red")] + _payload_parts(out)
    return [_section("结果")] + _payload_parts(out)


class Renderer:
    """语义事件渲染器。

    正文(content)/思考(reasoning) 各用独立 Live 区流式刷新；其余事件定位格后
    按固定格式打印。切换时旧 Live 定格保留，因此思考与正文视觉上可区分。

    Live 关闭自带定时刷新（auto_refresh=False），改由 _feed 按 _LIVE_FPS 节流触发：
    避免每个 token 全量重绘造成 cmd 下闪烁，同时保证只在内容变化时重绘。
    """

    _LIVE_FPS = 8
    _LIVE_OVERFLOW = "visible"   # 超出屏幕自然滚屏、跨屏连贯（改 "ellipsis" 可还原裁剪占位）

    def __init__(self):
        self._live = None
        self._kind = None
        self._buf = []
        self._last = 0.0
        self._dirty = False
        self._pending_calls = {}   # id -> (工具名, 调用框正文)，等结果到达后合并输出

    # ---- Live 管理 ----
    def _stop_live(self):
        if self._live:
            self._flush_live()   # 先补绘最后一个周期的增量，避免丢字
            self._live.stop()
            self._live = None
            self._kind = None

    def _ensure(self, kind):
        if self._kind == kind and self._live:
            return
        self._stop_live()
        self._kind = kind
        self._buf = []
        self._last = 0.0
        self._dirty = False
        self._live = Live(console=console, refresh_per_second=self._LIVE_FPS,
                          auto_refresh=False, transient=False,
                          vertical_overflow=self._LIVE_OVERFLOW)
        self._live.start(refresh=True)

    def _feed(self, kind, delta):
        delta = _normalize_newlines(delta)
        if not delta:
            return
        self._ensure(kind)
        self._buf.append(delta)
        self._dirty = True
        if time.monotonic() - self._last >= 1.0 / self._LIVE_FPS:
            self._flush_live()

    def _flush_live(self):
        """把当前累积内容重绘一次（无变化则跳过）"""
        if not self._live or not self._dirty:
            return
        text = "".join(self._buf)
        if self._kind == "reasoning":
            self._live.update(Text(text, style=_THINK_STYLE))
        else:
            self._live.update(Markdown(flatten_headings(text), justify="left"))
        self._live.refresh()
        self._last = time.monotonic()
        self._dirty = False

    # ---- 普通输出 ----
    def _print(self, renderable, end="\n"):
        had_live = self._live is not None
        self._stop_live()
        if had_live:
            console.print()
        console.print(renderable, end=end)

    def _line(self, text, style=None):
        self._print(Text(text, style=style) if style else Text(text))

    def _block(self, title, body, note=None):
        """统一的区块排版：空行 + 标题 + 缩进内容 +（可选）操作提示。

        与 core/console.py 的 _block 一致；内容整体左缩进 2 格与标题对齐，
        表格用 pad_edge=False 以免右端拖出一串空白。
        """
        had_live = self._live is not None
        self._stop_live()
        if had_live:
            console.print()
        console.print(Text(f"  {title}", style="title"))
        console.print(Padding(body, (0, 0, 0, 2), expand=False))
        if note:
            for line in note.splitlines():
                console.print(Text(f"  {line}", style="hint"))

    def _render_grid(self, title, header, rows, aligns=None, note=None, foot=0):
        """多列数字清单（统计用）：表头灰阶小字，数字列右对齐便于纵向比对。

        foot=n：最后 n 行按「合计行」加粗（与明细拉开层次，不需要再画分隔线）。
        """
        aligns = aligns or ["left"] * len(header)
        table = Table(box=None, show_header=True, show_edge=False,
                      padding=(0, 2), pad_edge=False, expand=False, header_style="tbl.head")
        for name, align in zip(header, aligns):
            table.add_column(name, justify=align, no_wrap=True,
                             style="kv.num" if align == "right" else "kv.val")
        for i, row in enumerate(rows):
            bold = foot and i >= len(rows) - foot
            table.add_row(*[Text(str(c), style="title") if bold else str(c) for c in row])
        self._block(title, table, note)

    def _say(self, message, level="info"):
        """单行提示：统一缩进、图标与配色（ok / warn / err / info）"""
        icon, style = {
            "ok": ("✅", "ok"),
            "warn": ("⚠", "warn"),
            "err": ("❌", "err"),
            "info": ("·", "muted"),
        }.get(level, ("·", "muted"))
        self._print(Text(f"  {icon} {message}", style=style))

    # ---- 各类事件 ----
    def handle(self, etype, evt):
        if etype == "content":
            self._feed("content", evt.get("content", ""))
        elif etype == "reasoning":
            self._feed("reasoning", evt.get("content", ""))
        elif etype == "question":
            pass   # 终端不再回显问题（与 CLI 一致）
        elif etype == "db":
            pass
        elif etype == "session":
            pass
        elif etype == "memory":
            pass
        elif etype == "config":
            self._print(Text(f"  ▸ 已更新 .env：{'、'.join(evt.get('keys', []))}", style="ok"))
        elif etype == "reasoning_header":
            pass   # 不再打印「思考过程」分隔线
        elif etype == "tool_round":
            self.flush_pending_calls()   # 上一轮若有调用没等到结果，先兜底打出来
        elif etype == "continuing":
            pass
        elif etype == "tool_call":
            parts = []
            if evt.get("parse_error"):
                parts.append(Text(f"参数解析失败：{evt.get('parse_error')}", style="err"))
            if evt.get("arguments") is not None:
                parts.extend(_json_view(
                    _unwrap_arguments(evt.get("arguments")), _CALL_MAX_CHARS, _CALL_MAX_LINES,
                    inline=_CALL_INLINE_LINES, what="参数 ", inline_chars=_CALL_JSON_LIMIT,
                ))
            elif evt.get("raw"):
                parts.extend(_text_view(evt.get("raw"), _CALL_TEXT_LIMIT, _CALL_MAX_LINES, what="参数 "))
            else:
                parts.append(Text("（无参数）", style="muted"))
            # 先缓存，等结果到达时合并到同一个框里输出
            self._pending_calls[_call_key(evt.get("id"), evt.get("name"))] = (
                _call_label(evt.get("name"), evt.get("arguments")), parts)
        elif etype == "tool_result":
            self._tool_result(evt)
        elif etype == "search":
            had_live = self._live is not None
            self._stop_live()
            if had_live:
                console.print()
            if evt.get("state") == "call":
                if evt.get("first"):
                    console.print()
                    console.print(Text("  🔎 服务端搜索", style="accent"))
                line = Text("    · ", style="panel.border")
                line.append("发起 ", style="muted")
                line.append(str(evt.get("id", "")), style="kv.key")
                console.print(line)
            else:
                line = Text("    · ", style="panel.border")
                line.append("状态 ", style="muted")
                line.append(str(evt.get("state")), style="muted")
                console.print(line)
        elif etype == "warning":
            # 警告不是错误：黄色 + 缩进，和红色的失败区分开
            self._print(Text(f"  ⚠ {evt.get('message', '')}", style="warn"))
        elif etype == "usage":
            self._usage(evt)
        elif etype == "log":
            self._print(Text(_normalize_newlines(evt.get("text", ""))), end="")
        elif etype == "error":
            self.flush_pending_calls()
            self._stop_live()
            console.print()
            self._print(Text(f"  ❌ {evt.get('error', '')}", style="err"))

    def flush_pending_calls(self):
        """结果一直没到（执行被中断）时兜底输出，避免调用信息丢失。"""
        while self._pending_calls:
            name, parts = self._pending_calls.popitem()[1]
            self._print(_tool_panel(list(parts), f"▸ {name}", status="pending"))

    def _tool_result(self, evt):
        """工具结果：与对应的调用合并成一个框（diff / 错误 / 普通结果各自分组）"""
        name = evt.get("name")
        pending = self._pending_calls.pop(_call_key(evt.get("id"), name), None)
        if pending:
            tool_name, parts = pending[0], list(pending[1])
        else:
            tool_name, parts = name, []
        parts.extend(_result_parts(evt))
        self._print(_tool_panel(parts, f"▸ {tool_name}",
                                status="error" if evt.get("error") else "ok"))

    def _usage(self, evt):
        self.flush_pending_calls()   # 收尾：异常中断时残留的调用框先补输出
        default_period = "高峰时段（北京时间 周一至周五 9:00-12:00、14:00-18:00）" if evt.get("peak") \
            else "空闲时段（北京时间 周一至周五 9:00-12:00、14:00-18:00）"
        period = evt.get("period") or default_period
        symbol = evt.get("symbol")
        if symbol is None:
            symbol = "¥" if evt.get("currency") == "CNY" else ""

        def money(value):
            return f"{symbol}{value:.4f}"

        # period 形如「空闲时段（北京时间 …）」：括号里是规则说明，放标题会折行，
        # 标题只留时段名，规则细节用户可在 .env / 文档里查
        short_period = period.split("（")[0]
        rows = [
            ("输入 · 缓存命中", f"{evt.get('hit', 0):,}", money(evt.get("cost_hit", 0))),
            ("输入 · 未命中", f"{evt.get('miss', 0):,}", money(evt.get("cost_miss", 0))),
            ("输出", f"{evt.get('out', 0):,}", money(evt.get("cost_out", 0))),
            ("合计", "", money(evt.get("total", 0))),
        ]
        self._render_grid(f"用量 · {evt.get('model', '')} · {short_period}",
                          ["项目", "tokens", "费用"], rows,
                          aligns=["left", "right", "right"], foot=1)
        # 服务商与余额压成一行：表格只放数字，说明性信息不占额外的行
        balance = evt.get("balance")
        tail = Text("  ")
        provider = evt.get("provider")
        if provider:
            tail.append(str(provider), style="kv.key")
        if balance is not None:
            tail.append("   💰 余额 ", style="muted")
            tail.append(f"{symbol}{balance}", style="ok")
        else:
            tail.append("   💰 余额 查询失败", style="muted")
        console.print(tail)

    def finish(self):
        self.flush_pending_calls()
        self._stop_live()
        self._buf = []


DEFAULT_HOST = os.environ.get("DP_HOST", "192.168.30.181")
DEFAULT_PORT = os.environ.get("DP_PORT", "8000")
DEFAULT_TIMEOUT = int(os.environ.get("DP_TIMEOUT", "300"))


def _handle_event(event_text, renderer):
    """处理单个 SSE event 块（已去掉末尾空行）。

    返回:
      0 → 继续读取
      1 → 正常结束（done），停止
      2 → 出错（error），停止且客户端应非零退出
    """
    # 一个 event 可能含多行；只取 data: 行，忽略 ':' 注释行
    data_lines = []
    for line in event_text.split("\n"):
        if line.startswith(":"):
            continue  # 注释 / 心跳
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    if not data_lines:
        return 0

    data = "\n".join(data_lines)
    try:
        evt = json.loads(data)
    except json.JSONDecodeError:
        return 0  # 无法解析的片段忽略，继续

    etype = evt.get("type")
    if etype == "done":
        return 1
    if etype == "error":
        renderer.handle("error", evt)
        return 2
    if etype and etype != "start":
        renderer.handle(etype, evt)
    return 0


def main():
    parser = argparse.ArgumentParser(description="shell-tool 流式对话客户端")
    parser.add_argument("-H", "--host", default=DEFAULT_HOST, help="服务端主机")
    parser.add_argument("-P", "--port", default=DEFAULT_PORT, help="服务端端口")
    parser.add_argument("-n", "--new", action="store_true", help="新开对话")
    parser.add_argument("-t", "--timeout", type=int, default=DEFAULT_TIMEOUT, help="请求超时（秒）")
    parser.add_argument("-k", "--key", help="覆盖服务端 .env 的 DeepSeek API Key")
    parser.add_argument("-m", "--model", help="覆盖服务端 .env 的模型名")
    parser.add_argument("question", nargs="+", help="要问的问题")
    args = parser.parse_args()

    question = " ".join(args.question)
    url = f"http://{args.host}:{args.port}/chat/stream"
    payload = {"question": question}
    if args.new:
        payload["new"] = True
    if args.key:
        payload["key"] = args.key
    if args.model:
        payload["model"] = args.model

    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    print(f"连接到: {url}", file=sys.stderr)
    exit_code = 0
    event_lines = []  # 当前事件已收集的行（SSE 以空行分隔事件）
    renderer = Renderer()

    try:
        with urlopen(req, timeout=args.timeout) as resp:
            # 逐行读取：服务端每个事件都是单行 data: {...} 紧跟一个空行，
            # 这样无需等待 4096 字节即可立即拿到增量，避免客户端侧流式卡顿。
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                if line == "":
                    # 空行 = 事件边界
                    if event_lines:
                        rc = _handle_event("\n".join(event_lines), renderer)
                        event_lines = []
                        if rc == 2:
                            exit_code = 1
                            break
                        if rc == 1:
                            break
                    continue
                if line.startswith(":"):
                    # 注释 / 心跳，忽略
                    continue
                event_lines.append(line)
            # 连接关闭：处理可能残留的最后一个事件
            if event_lines:
                rc = _handle_event("\n".join(event_lines), renderer)
                if rc == 2:
                    exit_code = 1
    except HTTPError as e:
        renderer.finish()
        body = e.read().decode("utf-8", errors="replace")
        console.print(Text(f"  ❌ HTTP {e.code}：{body}", style="err"))
        sys.exit(1)
    except URLError as e:
        renderer.finish()
        console.print(Text(f"  ❌ 连接失败：{e.reason}", style="err"))
        sys.exit(1)
    except Exception as e:
        renderer.finish()
        console.print(Text(f"  ❌ 请求异常：{e}", style="err"))
        sys.exit(1)

    renderer.finish()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
