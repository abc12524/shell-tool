#!/usr/bin/env python3
"""shell-tool 流式对话客户端：消费 /chat/stream 的 SSE 规范事件流。

事件类型（与服务端约定）：
  start                 会话开始
  content {id,content}  正文增量（思考/回答统一为文本流）
  reasoning {id,content} 思考过程增量（如需区分可在此加前缀，默认同样输出）
  error  {error,code}   出错，客户端以此非零退出
  done                  正常结束
  以 ':' 开头的注释行（含 ": heartbeat"）忽略

用法：
  dp.py [-H 主机] [-P 端口] [-n] [-t 超时秒] <问题...>
  # 例：dp.py -H 192.168.30.181 "今天北京天气怎么样？"
  #      dp.py -n "帮我写个脚本"
环境变量 DP_HOST / DP_PORT / DP_TIMEOUT 可覆盖默认值。
"""
import argparse
import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

DEFAULT_HOST = os.environ.get("DP_HOST", "192.168.30.181")
DEFAULT_PORT = os.environ.get("DP_PORT", "8000")
DEFAULT_TIMEOUT = int(os.environ.get("DP_TIMEOUT", "300"))


def _handle_event(event_text):
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
    if etype == "content" or etype == "reasoning":
        sys.stdout.write(evt.get("content", ""))
        sys.stdout.flush()
    elif etype == "error":
        sys.stderr.write(f"\n[错误] {evt.get('error', '')}\n")
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
    parser.add_argument("question", nargs="+", help="要问的问题")
    args = parser.parse_args()

    question = " ".join(args.question)
    url = f"http://{args.host}:{args.port}/chat/stream"
    payload = {"question": question}
    if args.new:
        payload["new"] = True

    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    print(f"连接到: {url}", file=sys.stderr)
    exit_code = 0
    event_lines = []  # 当前事件已收集的行（SSE 以空行分隔事件）

    try:
        with urlopen(req, timeout=args.timeout) as resp:
            # 逐行读取：服务端每个事件都是单行 data: {...} 紧跟一个空行，
            # 这样无需等待 4096 字节即可立即拿到增量，避免客户端侧流式卡顿。
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                if line == "":
                    # 空行 = 事件边界
                    if event_lines:
                        rc = _handle_event("\n".join(event_lines))
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
                rc = _handle_event("\n".join(event_lines))
                if rc == 2:
                    exit_code = 1
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        sys.stderr.write(f"HTTP 错误 {e.code}: {body}\n")
        sys.exit(1)
    except URLError as e:
        sys.stderr.write(f"连接失败: {e.reason}\n")
        sys.exit(1)
    except Exception as e:
        sys.stderr.write(f"请求异常: {str(e)}\n")
        sys.exit(1)

    print()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
