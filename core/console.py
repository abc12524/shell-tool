#!/usr/bin/env python3
"""全局 Rich Console 实例，供各模块统一使用"""
import json
import os
import sys
import threading
import time

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

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

console = Console(highlight=False)


# 终端下 Live 每秒最多重绘次数。cmd/conhost 对「上移光标 + 清行 + 重画」较慢，
# 逐 token 重绘（可达数十次/秒）会明显闪烁；限制到 ~8 次/秒并只重绘变化内容即可。
_LIVE_FPS = 8

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
        return Markdown(text)


class LiveReasoning(_LiveStream):
    """流式思考过程渲染器（非终端下同样原样转发，交客户端渲染）"""

    _kind = "reasoning"

    def _render(self, text):
        return Text(text, style="dim italic")


def render_markdown(text: str):
    """一次性渲染 Markdown"""
    console.print(Markdown(text))


def render_table(title: str, rows: list[tuple[str, str]], style: str = "cyan"):
    """渲染简易表格"""
    table = Table(title=title, show_header=False, border_style="dim")
    table.add_column("Key", style="bold")
    table.add_column("Value", style=style)
    for k, v in rows:
        table.add_row(k, str(v))
    console.print(table)


def render_reasoning(text: str):
    """一次性渲染思考过程"""
    console.print(Text(text, style="dim italic"))
