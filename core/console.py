#!/usr/bin/env python3
"""全局 Rich Console 实例，供各模块统一使用"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# API 模式：由 server/api.py 注入 DP_API_MODE=1。此时 stdout 不再直接输出终端
# 文本，而是输出「一行一个 JSON 事件」的结构化流：
#   {"t":"content",  "d":"..."}  模型正文增量
#   {"t":"reasoning","d":"..."}  思考过程增量
#   {"t":"note",     "d":"..."}  诊断信息（状态行/工具调用/token 统计等）
# server/api.py 把每种事件映射成对应 SSE 事件，客户端按类型分别渲染，
# 从而既有干净的 Markdown 正文，又保留工具调用、用量、思考过程等信息。
_API_MODE = os.environ.get("DP_API_MODE") == "1"
# 保存真正的 stdout 作为结构化事件通道
_CONTENT_STREAM = sys.stdout


def _force_utf8(stream):
    """强制标准流使用 UTF-8。

    Windows 下当 stdout/stderr 被重定向到管道时，Python 默认用 locale 编码
    （中文系统为 cp936/GBK），Rich 会据此输出 GBK 字节并退化为 ASCII 边框；
    而 Server API 按 UTF-8 读取该管道，导致中文/边框部分乱码（格式化部分失效）。
    统一改回 UTF-8，保证重定向与终端下渲染一致。

    管道下同时关闭 \\n -> \\r\\n 翻译：CR 不是行分隔符，下游按 Markdown
    渲染时会被当普通字符吞掉，造成多行内容挤在一行、格式错乱。
    """
    try:
        if stream.isatty():
            stream.reconfigure(encoding="utf-8")
        else:
            stream.reconfigure(encoding="utf-8", newline="\n")
    except (AttributeError, ValueError, OSError):
        pass


# 工具在子线程执行时也可能打印，需保证一行事件原子写入，避免 JSON 被截断
_EMIT_LOCK = threading.Lock()


def emit(kind: str, **fields):
    """API 模式：把一条结构化事件写成一行 JSON；终端/非 API 模式：不输出。

    事件只携带“变化的数据”，展示格式（emoji/颜色/表格）由客户端自行构建。
    """
    if not _API_MODE:
        return
    payload = {"t": kind}
    payload.update(fields)
    try:
        line = json.dumps(payload, ensure_ascii=False)
        with _EMIT_LOCK:
            _CONTENT_STREAM.write(line + "\n")
            _CONTENT_STREAM.flush()
    except Exception:
        pass


def _output(kind: str, text: str):
    """正文/思考增量：API 模式发事件，否则原样写文本（重定向场景）"""
    if _API_MODE:
        emit(kind, content=text)
    elif text:
        with _EMIT_LOCK:
            _CONTENT_STREAM.write(text)
            _CONTENT_STREAM.flush()


class _DiagStream:
    """API 模式下的兜底诊断通道。

    任何未被显式结构化的 print()/rich 输出都会经此转成 log 事件，
    保证既不会混进正文，也不会丢失。
    """

    encoding = "utf-8"
    errors = "replace"

    def write(self, text):
        if text:
            emit("log", text=text)
        return len(text) if text else 0

    def flush(self):
        pass

    def isatty(self):
        return False


_force_utf8(_CONTENT_STREAM)
_force_utf8(sys.stderr)

# API 模式下把 sys.stdout 换成兜底诊断通道（未结构化输出 -> log 事件；正文走 _CONTENT_STREAM）
if _API_MODE:
    sys.stdout = _DiagStream()

# ── 终端视觉令牌 ────────────────────────────────────────────────────────────
# 统一在这里定义语义色与组件样式，events.py / main.py 只引用样式名而不写死颜色，
# 保证工具框、统计表、提示语共用同一套视觉语言（改一处即全局生效）。
# 颜色一律用固定灰阶（greyN）而非 "dim"：dim 是降亮度（SGR 2），在浅色终端
# 里会把中灰压到接近白色而不可见；greyN 是固定索引色，浅底/深底都可读。
THEME = Theme({
    # 灰阶取中间段（grey54~grey62）：更亮的灰在白底浅色终端里会消失，更暗的在深底看不见
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


# 终端下 Live 每秒最多重绘次数。cmd/conhost 对「上移光标 + 清行 + 重画」较慢，
# 逐 token 重绘（可达数十次/秒）会明显闪烁；限制到 ~6 次/秒并只重绘变化内容即可。
_LIVE_FPS = 6

# 内容超过终端高度时的滚动策略：
#   "visible"  → 不裁剪，超出部分让终端自然滚屏（跨屏内容连贯，推荐）
#   "ellipsis" → 裁剪到屏幕高，底部以 … 占位（Rich 默认，不滚动）
_LIVE_OVERFLOW = "visible"


class _LiveStream:
    """流式渲染基类：终端下节流刷新 Live，非终端下把增量原样转发给下游。

    - 终端路径：关闭 Rich 自带定时刷新线程（auto_refresh=False），改由 feed 按
      时间节流触发 refresh——既限制重绘频率（避免每个 token 全量重绘造成闪烁），
      又保证 Markdown/Text 只在内容真正变化时重建（无增量则完全不重绘）。
    - 非终端路径（被 Server API 捕获为管道时）：把增量原样交给下游渲染，
      避免服务端先渲染一遍、客户端再渲染一遍导致的格式错乱。
    """

    _kind = "content"   # 子类覆盖：passthrough 时的事件类型
    _fps = _LIVE_FPS    # 每秒最多重绘次数

    def __init__(self):
        self._buf = []
        self._live = None
        self._last = 0.0
        self._dirty = False
        self._passthrough = not console.is_terminal

    def _render(self, text):
        """子类覆盖：把累积文本转成 Rich 可渲染对象"""
        raise NotImplementedError

    def start(self):
        self._buf.clear()
        self._last = 0.0
        self._dirty = False
        if self._passthrough:
            self._live = None
        else:
            self._live = Live(
                console=console,
                refresh_per_second=self._fps,
                auto_refresh=False,   # 关闭定时重绘，改为 feed 节流触发
                transient=False,
                vertical_overflow=_LIVE_OVERFLOW,
            )
            self._live.start(refresh=True)
        return self

    def feed(self, delta: str):
        if not delta:
            return
        self._buf.append(delta)
        self._dirty = True
        if self._passthrough:
            _output(self._kind, delta)
            return
        # 节流：距上次重绘不足一个刷新周期则先攒着，下个 token 再决定
        if time.monotonic() - self._last >= 1.0 / self._fps:
            self._flush()

    def _flush(self):
        """把当前累积内容重绘一次（无变化则跳过）"""
        if self._live is None or not self._dirty:
            return
        self._live.update(self._render("".join(self._buf)))
        self._live.refresh()
        self._last = time.monotonic()
        self._dirty = False

    def finish(self):
        if self._live is not None:
            self._flush()   # 补一次，保证最后一个周期的增量也显示出来
            self._live.stop()
            self._live = None
        return "".join(self._buf)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.finish()


class LiveMarkdown(_LiveStream):
    """流式 Markdown 渲染器"""

    _kind = "content"

    def _render(self, text):
        return Markdown(flatten_headings(text), justify="left")


class LiveReasoning(_LiveStream):
    """流式思考过程渲染器（非终端下同样原样转发，交客户端渲染）"""

    _kind = "reasoning"

    def _render(self, text):
        return Text(text, style=_THINK_STYLE)


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


def render_markdown(text: str):
    """一次性渲染 Markdown

    justify="left"：rich 默认两端对齐，中文段落会被硬撑出大段空隙；
    终端里左对齐更好读，避免长句被拉成「稀疏的一行」。
    """
    console.print(Markdown(flatten_headings(text), justify="left"))


def _block(title, body, note=None):
    """统一的区块排版：空行 + 标题 + 内容 +（可选）操作提示。

    所有清单类输出共用这一个外壳，保证标题、缩进、与后续提示的间距一致。
    note 可含多行，每行都按同一缩进对齐（提示语能按「键 + 说明」竖排）。
    内容整体左缩进 2 格：与标题对齐，同时让表格右端不带多余空白（pad_edge=False）。
    """
    console.print()
    console.print(Text(f"  {title}", style="title"))
    console.print(Padding(body, (0, 0, 0, 2), expand=False))
    if note:
        for line in note.splitlines():
            console.print(Text(f"  {line}", style="hint"))


def render_table(title: str, rows: list[tuple[str, str]], style: str = "kv.key",
                 note: str | None = None):
    """两列清单：左列（标识/名称，可强调）左对齐，右列（描述）自动换行。

    无边框、无竖线——终端里表格线会和中文字符争宽度，反而显脏；
    用缩进 + 列对齐 + 灰阶层次来分区，浅色/深色终端都干净。
    """
    table = Table(
        box=None, show_header=False, show_edge=False,
        padding=(0, 2), pad_edge=False, expand=False,
    )
    table.add_column(justify="left", no_wrap=True)
    table.add_column(justify="left", overflow="fold", style="kv.val")
    for k, v in rows:
        # 键/值都允许传 Text（需要局部着色，如 enable/disable、当前项标记），其余按字符串处理
        table.add_row(k if isinstance(k, Text) else Text(str(k), style=style),
                      v if isinstance(v, Text) else str(v))
    _block(title, table, note)


def render_grid(title: str, header: list[str], rows: list[tuple], aligns: list[str] | None = None,
                note: str | None = None, foot: int = 0):
    """多列数字清单（统计/报表用）：表头灰阶小字，数字列右对齐便于纵向比对。

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
    _block(title, table, note)


def say(message: str, level: str = "info"):
    """单行提示：统一缩进、图标与配色（ok / warn / err / info）。

    原文案里「⚠️ 两个空格」和「✅ 一个空格」混用，一列提示排下来就是错位的；
    统一成「图标 + 一个空格」，并交给主题着色，成功/警告/失败一眼可辨。
    """
    icon, style = {
        "ok": ("✅", "ok"),
        "warn": ("⚠", "warn"),
        "err": ("❌", "err"),
        "info": ("·", "muted"),
    }.get(level, ("·", "muted"))
    console.print(Text(f"  {icon} {message}", style=style))


def render_reasoning(text: str):
    """一次性渲染思考过程"""
    console.print(Text(text, style=_THINK_STYLE))
