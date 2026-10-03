#!/usr/bin/env python3
"""全局 Rich Console 实例，供各模块统一使用"""
import os
import sys

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

# API 模式：由 server/api.py 注入 DP_API_MODE=1，表示本进程 stdout 会被 SSE
# 客户端当作"模型正文"消费。此时所有诊断/状态输出改走 stderr，只把模型正文
# 留在 stdout，避免状态日志被当作回答内容渲染导致格式错乱。
_API_MODE = os.environ.get("DP_API_MODE") == "1"
# 在重定向前保存真正的 stdout 作为正文通道
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


_force_utf8(_CONTENT_STREAM)
_force_utf8(sys.stderr)

# API 模式下把 sys.stdout 指向 stderr：此后所有 print()/rich console.print()
# 等诊断输出都进 stderr，只有 LiveMarkdown/LiveReasoning 的正文写入 _CONTENT_STREAM
if _API_MODE:
    sys.stdout = sys.stderr

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
            _CONTENT_STREAM.write(delta)
            _CONTENT_STREAM.flush()
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
            _CONTENT_STREAM.write(delta)
            _CONTENT_STREAM.flush()
        else:
            self._live.update(Text("".join(self._buf), style="dim italic"))

    def finish(self):
        if self._live:
            self._live.stop()
            self._live = None
        if self._passthrough and self._buf:
            # 思考过程与正文之间补一个空行，避免两段文字在 Markdown 里粘成一行
            _CONTENT_STREAM.write("\n\n")
            _CONTENT_STREAM.flush()
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
