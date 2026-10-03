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
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text


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

console = Console(highlight=False)


def _normalize_newlines(text: str) -> str:
    """统一换行：CRLF / CR -> LF。

    Rich 的 Markdown 只认 LF 作块分隔，直接喂入 CRLF 会把多行挤成一行。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


class Renderer:
    """语义事件渲染器。

    正文(content)/思考(reasoning) 各用独立 Live 区流式刷新；其余事件定位格后
    按固定格式打印。切换时旧 Live 定格保留，因此思考与正文视觉上可区分。
    """

    def __init__(self):
        self._live = None
        self._kind = None
        self._buf = []

    # ---- Live 管理 ----
    def _stop_live(self):
        if self._live:
            self._live.stop()
            self._live = None
            self._kind = None

    def _ensure(self, kind):
        if self._kind == kind and self._live:
            return
        self._stop_live()
        self._kind = kind
        self._buf = []
        self._live = Live(console=console, refresh_per_second=12, transient=False)
        self._live.start()

    def _feed(self, kind, delta):
        delta = _normalize_newlines(delta)
        if not delta:
            return
        self._ensure(kind)
        self._buf.append(delta)
        if kind == "reasoning":
            self._live.update(Text("".join(self._buf), style="dim italic"))
        else:
            self._live.update(Markdown("".join(self._buf)))

    # ---- 普通输出 ----
    def _print(self, renderable, end="\n"):
        had_live = self._live is not None
        self._stop_live()
        if had_live:
            console.print()
        console.print(renderable, end=end)

    def _line(self, text, style=None):
        self._print(Text(text, style=style) if style else Text(text))

    # ---- 各类事件 ----
    def handle(self, etype, evt):
        if etype == "content":
            self._feed("content", evt.get("content", ""))
        elif etype == "reasoning":
            self._feed("reasoning", evt.get("content", ""))
        elif etype == "question":
            self._line(f"\n👤 用户问题: {evt.get('text', '')}")
        elif etype == "db":
            self._line("🗄️  本地 SQLite + 在线 MySQL 同步" if evt.get("online")
                       else "🗄️  使用本地 SQLite 数据库")
        elif etype == "session":
            action, sid = evt.get("action"), evt.get("id", "")
            if action == "new":
                self._line(f"\n🆕 新会话: {sid}")
            elif action == "resume":
                self._line(f"\n↩️  继续会话: {sid}")
            elif action == "saved":
                self._line(f"\n✅ 对话已保存到会话: {sid}")
        elif etype == "memory":
            if evt.get("phase") == "profile":
                self._line("🔍 加载记忆索引... " + ("完成" if evt.get("found") else "无"))
            else:
                self._line("🔍 搜索相关记忆... " + ("找到相关记忆，注入上下文" if evt.get("found") else "无相关记忆。"))
        elif etype == "config":
            self._line(f"⚙️  已更新 .env：{'、'.join(evt.get('keys', []))}")
        elif etype == "reasoning_header":
            self._print(Text("🤔 思考过程", style="dim"), end="\n")
            self._print(Text("─" * 60, style="dim"), end="\n")
        elif etype == "tool_round":
            self._print(Text("=" * 30, style="dim"), end="\n")
            self._line(f"🔧 执行工具 (第{evt.get('index')}轮): {evt.get('local')} 个本地调用 / {evt.get('search')} 个服务端搜索",
                       style="bold yellow")
        elif etype == "continuing":
            self._print(Text("=" * 30, style="dim"), end="\n")
            self._line("🤔 继续推理...", style="dim italic")
        elif etype == "tool_call":
            if evt.get("parse_error"):
                self._line(f"⚠️ 工具 {evt.get('name')} 参数解析失败: {evt.get('parse_error')}", style="bold red")
            else:
                self._line(f"🔧 执行工具: {evt.get('name')}")
                if evt.get("arguments") is not None:
                    self._line(f"📥 参数: {json.dumps(evt.get('arguments'), ensure_ascii=False)}")
        elif etype == "tool_result":
            out = evt.get("output", "") or ""
            preview = out[:200] + ("..." if len(out) > 200 else "")
            self._line(f"📤 结果: {preview}")
        elif etype == "search":
            if evt.get("state") == "call":
                if evt.get("first"):
                    self._print(Text("\n🔎 服务端网页搜索：", style="bold cyan"), end="\n")
                self._line(f"  - 搜索调用 {evt.get('id', '')} 已发起", style="cyan")
            else:
                self._line(f"  - 搜索状态: {evt.get('state')}", style="cyan")
        elif etype == "warning":
            self._line(f"⚠️  {evt.get('message', '')}", style="bold red")
        elif etype == "usage":
            self._usage(evt)
        elif etype == "log":
            self._print(Text(_normalize_newlines(evt.get("text", ""))), end="")
        elif etype == "error":
            self._print(Text(f"\n[错误] {evt.get('error', '')}", style="bold red"))

    def _usage(self, evt):
        period = "高峰时段" if evt.get("peak") else "空闲时段"
        self._print(Text(f"\n⏰ {period}（北京时间 周一至周五 9:00-12:00、14:00-18:00）", style="bold"))
        table = Table(show_header=False, border_style="dim")
        table.add_column("Key", style="bold")
        table.add_column("Value", style="cyan")
        table.add_row("输入(缓存命中)", f"{evt.get('hit', 0):,} tokens  ¥{evt.get('cost_hit', 0):.4f}")
        table.add_row("输入(缓存未命中)", f"{evt.get('miss', 0):,} tokens  ¥{evt.get('cost_miss', 0):.4f}")
        table.add_row("输出", f"{evt.get('out', 0):,} tokens  ¥{evt.get('cost_out', 0):.4f}")
        table.title = f"📊 Token 消耗统计 · {evt.get('model', '')} · 合计 ¥{evt.get('total', 0):.4f}"
        self._print(table)
        balance = evt.get("balance")
        if balance is not None:
            currency = evt.get("currency", "")
            symbol = "¥" if currency == "CNY" else ""
            self._print(Text(f"💰 账户余额：{symbol}{balance} {currency}".rstrip(), style="bold green"))
        else:
            self._line("💰 账户余额：查询失败", style="dim")

    def finish(self):
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
        console.print(f"[bold red]HTTP 错误 {e.code}:[/bold red] {body}")
        sys.exit(1)
    except URLError as e:
        renderer.finish()
        console.print(f"[bold red]连接失败:[/bold red] {e.reason}")
        sys.exit(1)
    except Exception as e:
        renderer.finish()
        console.print(f"[bold red]请求异常:[/bold red] {str(e)}")
        sys.exit(1)

    renderer.finish()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
