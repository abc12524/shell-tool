#!/usr/bin/env python3
"""shell-tool 流式对话客户端：消费 /chat/stream 的 SSE 规范事件流。

事件类型（与服务端约定）：
  start                 会话开始
  content {id,content}  正文增量（Markdown 渲染）
  reasoning {id,content} 思考过程增量（暗色斜体，与正文区分）
  status {id,content}   诊断信息（状态行/工具调用日志/token 用量，暗色输出）
  error  {error,code}   出错，客户端以此非零退出
  done                  正常结束
  以 ':' 开头的注释行（含 ": heartbeat"）忽略

用法：
  dp.py [-H 主机] [-P 端口] [-n] [-t 超时秒] [-k KEY] [-m MODEL] <问题...>
  # 例：dp.py -H 192.168.30.181 "今天北京天气怎么样？"
  #      dp.py -n "帮我写个脚本"
  #      dp.py -m deepseek-v4-pro -k sk-xxx "问题"   # 覆盖服务端 .env 的模型/密钥
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
    """事件渲染器：按类型分别呈现

    - reasoning → 暗色斜体（思考过程，与正文区分）
    - content   → Markdown 正文
    - status    → 诊断信息（状态行 / 工具调用日志 / token 用量表）
    思考与正文各用独立 Live 区，切换时旧区定格保留，因此两者视觉上可区分。
    """

    def __init__(self):
        self._live = None
        self._kind = None
        self._buf = []

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

    def feed(self, kind: str, delta: str):
        delta = _normalize_newlines(delta)
        if not delta:
            return
        self._ensure(kind)
        self._buf.append(delta)
        if kind == "reasoning":
            self._live.update(Text("".join(self._buf), style="dim italic"))
        else:
            self._live.update(Markdown("".join(self._buf)))

    def status(self, text: str):
        # 诊断信息：先定格当前 Live（保留已显示的思考/正文），再按其下方输出，
        # 避免诊断内容与正文重叠；后续若再有正文会另起一段重新渲染。
        text = _normalize_newlines(text)
        had_live = self._live is not None
        self._stop_live()
        if had_live:
            console.print()  # 定格 Live 后换行，避免诊断信息贴在正文末行
        if text:
            console.print(Text(text, style="dim"), end="")

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
    if etype == "content":
        renderer.feed("content", evt.get("content", ""))
    elif etype == "reasoning":
        renderer.feed("reasoning", evt.get("content", ""))
    elif etype == "status":
        renderer.status(evt.get("content", ""))
    elif etype == "error":
        renderer.finish()
        console.print(f"\n[bold red][错误][/bold red] {evt.get('error', '')}")
        return 2
    elif etype == "done":
        return 1
    # start 等其它类型忽略
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
