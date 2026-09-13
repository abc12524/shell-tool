#!/usr/bin/env python3
"""全局 Rich Console 实例，供各模块统一使用"""
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)


class LiveMarkdown:
    """流式 Markdown 渲染器：每收到 delta 就刷新终端显示"""

    def __init__(self):
        self._buf = []
        self._live = None

    def start(self):
        self._buf.clear()
        self._live = Live(console=console, refresh_per_second=12, transient=False)
        self._live.start()
        return self

    def feed(self, delta: str):
        if delta:
            self._buf.append(delta)
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
    """流式思考过程渲染器"""

    def __init__(self):
        self._buf = []
        self._live = None

    def start(self):
        self._buf.clear()
        self._live = Live(console=console, refresh_per_second=12, transient=False)
        self._live.start()
        return self

    def feed(self, delta: str):
        if delta:
            self._buf.append(delta)
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
