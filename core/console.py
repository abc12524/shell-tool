#!/usr/bin/env python3
"""全局 Rich Console 实例，供各模块统一使用"""
import json
import os
import sys
import threading

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


def _emit(kind: str, data: str):
    """API 模式下向事件通道写一行 JSON；非 API 模式原样写文本"""
    if _API_MODE:
        try:
            payload = json.dumps({"t": kind, "d": data}, ensure_ascii=False)
            with _EMIT_LOCK:
                _CONTENT_STREAM.write(payload + "\n")
                _CONTENT_STREAM.flush()
        except Exception:
            pass
    elif data:
        with _EMIT_LOCK:
            _CONTENT_STREAM.write(data)
            _CONTENT_STREAM.flush()


class _DiagStream:
    """API 模式下的诊断输出通道。

    print() / rich console.print() 都写到这里，转成 note 事件发给客户端，
    使其仍能看到状态行、工具调用日志、token 用量表等信息。
    """

    encoding = "utf-8"
    errors = "replace"

    def write(self, text):
        if text:
            _emit("note", text)
        return len(text) if text else 0

    def flush(self):
        pass

    def isatty(self):
        return False


_force_utf8(_CONTENT_STREAM)
_force_utf8(sys.stderr)

# API 模式下把 sys.stdout 换成诊断通道（诊断 -> note 事件；正文走 _CONTENT_STREAM）
if _API_MODE:
    sys.stdout = _DiagStream()

console = Console(highlight=False)


class LiveMarkdown:
    """流式 Markdown 渲染器。

    终端下用 Live 就地刷新显示；stdout 非终端（如被 Server API 捕获为管道）时，
    改为原样转发模型增量文本——由下游 SSE 客户端负责渲染，避免服务端先渲染一遍
    Markdown、客户端再渲染一遍导致的格式错乱。
    """

    def __init__(self):
        self._buf = []
        self._live = None
        self._passthrough = not console.is_terminal

    def start(self):
        self._buf.clear()
        if self._passthrough:
            self._live = None
        else:
            self._live = Live(console=console, refresh_per_second=12, transient=False)
            self._live.start()
        return self

    def feed(self, delta: str):
        if not delta:
            return
        self._buf.append(delta)
        if self._passthrough:
            _emit("content", delta)
        else:
            self._live.update(Markdown("".join(self._buf)))

    def finish(self):
        if self._live:
            self._live.stop()
            self._live = None
        return "".join(self._buf)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.finish()


class LiveReasoning:
    """流式思考过程渲染器（非终端下同样原样转发，交客户端渲染）"""

    def __init__(self):
        self._buf = []
        self._live = None
        self._passthrough = not console.is_terminal

    def start(self):
        self._buf.clear()
        if self._passthrough:
            self._live = None
        else:
            self._live = Live(console=console, refresh_per_second=12, transient=False)
            self._live.start()
        return self

    def feed(self, delta: str):
        if not delta:
            return
        self._buf.append(delta)
        if self._passthrough:
            _emit("reasoning", delta)
        else:
            self._live.update(Text("".join(self._buf), style="dim italic"))

    def finish(self):
        if self._live:
            self._live.stop()
            self._live = None
        return "".join(self._buf)


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
