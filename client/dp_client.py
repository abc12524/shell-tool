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
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
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


# ---- 工具框渲染（与 core/events.py 的终端样式保持一致）----
# 浅灰描边、宽度自适应内容、JSON 缩进高亮、结果最多 15 行、写动作展示彩色 diff。
_RESULT_MAX_LINES = 15
_RESULT_MAX_CHARS = 1500
_CALL_JSON_LIMIT = 1200
_CALL_TEXT_LIMIT = 300
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
            self._live.update(Text(text, style="dim italic"))
        else:
            self._live.update(Markdown(text))
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
            self._line(f"⚙️  已更新 .env：{'、'.join(evt.get('keys', []))}")
        elif etype == "reasoning_header":
            pass   # 不再打印「思考过程」分隔线
        elif etype == "tool_round":
            pass
        elif etype == "continuing":
            pass
        elif etype == "tool_call":
            parts = []
            if evt.get("parse_error"):
                parts.append(Text(f"⚠️ 参数解析失败: {evt.get('parse_error')}", style="bold red"))
            if evt.get("arguments") is not None:
                parts.extend(_json_view(evt.get("arguments"), _CALL_JSON_LIMIT))
            elif evt.get("raw"):
                parts.extend(_text_view(evt.get("raw"), _CALL_TEXT_LIMIT))
            else:
                parts.append(Text("（无参数）", style="dim"))
            self._print(_tool_panel(parts, f"🔧 执行工具 · {evt.get('name')}"))
        elif etype == "tool_result":
            self._tool_result(evt)
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

    def _tool_result(self, evt):
        """工具结果：带 diff 的写动作展示彩色 diff，其余走 JSON/文本框（最多 15 行）"""
        name = evt.get("name")
        out = evt.get("output", "") or ""
        error = bool(evt.get("error"))
        try:
            env = json.loads(out)
        except (ValueError, TypeError):
            env = None
        result = env.get("result") if isinstance(env, dict) else None
        if not error and isinstance(result, dict) and isinstance(result.get("diff"), str):
            self._print(_tool_panel(_diff_parts(result), f"📝 变更 · {name}"))
            return
        self._print(_tool_panel(
            _payload_parts(out),
            ("❌ 结果 · " if error else "📤 结果 · ") + str(name),
        ))

    def _usage(self, evt):
        default_period = "高峰时段（北京时间 周一至周五 9:00-12:00、14:00-18:00）" if evt.get("peak") \
            else "空闲时段（北京时间 周一至周五 9:00-12:00、14:00-18:00）"
        period = evt.get("period") or default_period
        symbol = evt.get("symbol")
        if symbol is None:
            symbol = "¥" if evt.get("currency") == "CNY" else ""
        self._print(Text(f"\n⏰ {period}", style="bold"))
        table = Table(show_header=False, border_style="dim")
        table.add_column("Key", style="bold")
        table.add_column("Value", style="cyan")
        table.add_row("输入(缓存命中)", f"{evt.get('hit', 0):,} tokens  {symbol}{evt.get('cost_hit', 0):.4f}")
        table.add_row("输入(缓存未命中)", f"{evt.get('miss', 0):,} tokens  {symbol}{evt.get('cost_miss', 0):.4f}")
        table.add_row("输出", f"{evt.get('out', 0):,} tokens  {symbol}{evt.get('cost_out', 0):.4f}")
        table.title = f"📊 Token 消耗统计 · {evt.get('model', '')} · 合计 {symbol}{evt.get('total', 0):.4f}"
        self._print(table)
        balance = evt.get("balance")
        if balance is not None:
            currency = evt.get("currency", "")
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
