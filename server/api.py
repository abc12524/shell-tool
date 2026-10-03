#!/usr/bin/env python3
"""Flask HTTP API：同步 /chat + SSE 流式 /chat/stream + /health

SSE 规范事件（data: <json>\n\n）：
  {"type":"start"}                 会话开始
  {"type":"content","id":N,"content":"..."}    正文增量（Markdown）
  {"type":"reasoning","id":N,"content":"..."}  思考过程增量
  {"type":"status","id":N,"content":"..."}     诊断信息（状态行/工具调用/用量）
  {"type":"error","error":"...","code":"..."} 出错（客户端应非零退出）
  {"type":"done"}                  正常结束
  以 ':' 开头的行（如 ": heartbeat"）为注释/心跳，客户端忽略

子进程（DP_API_MODE=1）stdout 为一行一个 JSON 事件：{"t":"content|reasoning|note","d":"..."}，
本模块按行解析后映射为上面的 SSE 事件；非 JSON 行按正文兜底。

注：Windows 上 select 仅支持 socket，不支持管道，故用线程 + 队列读取子进程
stdout；同时单独 drain stderr，避免双管道缓冲区打满造成死锁。
"""
import codecs
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

from flask import Flask, request, jsonify, Response, stream_with_context

app = Flask(__name__)
# server/ 的上一级即项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEEPSEEK_PATH = os.path.join(PROJECT_ROOT, "dp.py")
# 使用当前解释器（原先硬编码 ~/.venv/bin/python 是 Linux 路径）
PYTHON = sys.executable

HEARTBEAT_INTERVAL = 15  # 秒：超过该时间无输出则发心跳注释保活


def build_args(data):
    """根据请求参数构建传给 dp.py 的命令行参数"""
    args = []
    if data.get("new"):
        args.append("-n")
    if data.get("key"):
        args += ["-k", str(data["key"])]
    if data.get("model"):
        args += ["-m", str(data["model"])]
    args += data.get("args", [])
    return args


def _sse(event_type, **fields):
    """构造一个 SSE 事件块（data: <json>\\n\\n）"""
    payload = {"type": event_type}
    payload.update(fields)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# 子进程事件类型 → SSE 事件类型
_CHILD_EVENT_MAP = {"content": "content", "reasoning": "reasoning", "note": "status"}


def _parse_child_line(line):
    """解析子进程 stdout 的一行：结构化事件返回 (kind, data)，否则原样当正文"""
    try:
        evt = json.loads(line)
    except (ValueError, TypeError):
        return "content", line
    if isinstance(evt, dict) and "t" in evt:
        return evt.get("t", "content"), evt.get("d", "")
    return "content", line


def _child_event_to_sse(line, event_id):
    """把子进程一行结构化事件转成对应的 SSE 事件块"""
    kind, data = _parse_child_line(line)
    return _sse(_CHILD_EVENT_MAP.get(kind, "content"), id=event_id, content=data)


def _child_stdout_to_text(raw):
    """把子进程结构化 stdout 还原成纯文本（供同步 /chat 接口）"""
    parts = []
    for line in raw.splitlines():
        _, data = _parse_child_line(line)
        parts.append(data)
    return "".join(parts)


def _reap(process):
    """杀掉整个进程组并回收，杜绝僵尸/孤儿（客户端断开或异常时调用）"""
    try:
        pgid = os.getpgid(process.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, Exception):
        pass
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            pid, _ = os.waitpid(-process.pid, os.WNOHANG)
            if pid == 0:
                time.sleep(0.05)
                continue
        except ChildProcessError:
            break
    try:
        process.wait(timeout=2)
    except Exception:
        pass
    for stream in (process.stdout, process.stderr):
        try:
            stream.close()
        except Exception:
            pass


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True)
    if not data or "question" not in data:
        return jsonify({"status": "error", "error": "请提供 question 参数", "code": "bad_request"}), 400

    question = data["question"]
    if not question.strip():
        return jsonify({"status": "error", "error": "question 不能为空", "code": "bad_request"}), 400

    args = build_args(data)
    cmd = [PYTHON, DEEPSEEK_PATH] + args + [question]

    try:
        # 强制子进程以 UTF-8 输出：Windows 下管道默认 locale 编码(GBK)，
        # 会让 Rich 边框/中文乱码；这里统一按 UTF-8 编码与解码。
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["DP_API_MODE"] = "1"
        result = subprocess.run(
            cmd, capture_output=True, timeout=120,
            cwd=os.path.dirname(DEEPSEEK_PATH),
            env=env, encoding="utf-8", errors="replace",
        )
        # DP_API_MODE 下 stderr 是诊断日志（非错误）；仅在进程失败时当作错误返回
        error = (result.stderr or "").strip() if result.returncode != 0 else None
        return jsonify({
            "status": "ok",
            "result": {
                "question": question,
                "reply": _child_stdout_to_text(result.stdout),
                "error": error,
            },
        })
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "error": "请求超时", "code": "timeout"}), 504
    except Exception as e:
        return jsonify({"status": "error", "error": str(e), "code": "internal"}), 500


@app.route("/chat/stream", methods=["POST"])
def chat_stream():
    data = request.get_json(silent=True)
    if not data or "question" not in data:
        return jsonify({"status": "error", "error": "请提供 question 参数", "code": "bad_request"}), 400

    question = data["question"]
    if not question.strip():
        return jsonify({"status": "error", "error": "question 不能为空", "code": "bad_request"}), 400

    args = build_args(data)

    def generate():
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        # Windows 管道默认 locale(GBK) 编码会使 Rich 中文/边框乱码，统一 UTF-8
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # 告知子进程：stdout 会被当作模型正文转发，诊断输出请走 stderr
        env["DP_API_MODE"] = "1"
        cmd = [PYTHON, DEEPSEEK_PATH] + args + [question]

        # start_new_session：让子进程成为独立进程组 leader，断开时能干净回收
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.path.dirname(DEEPSEEK_PATH),
            env=env,
            bufsize=0,
            start_new_session=True,
        )

        q = queue.Queue()
        stderr_buf = []

        def pump_stdout():
            # 逐字节读取：管道 read(n) 在 Windows 上会一直阻塞到凑满 n 字节，
            # 用 read(1) 才能让每个事件立刻上屏，避免 4KB 缓冲造成的流式卡顿。
            # 子进程在 API 模式下一行一个 JSON 事件，按行切分后 put 到队列。
            fd = process.stdout.fileno()
            dec = codecs.getincrementaldecoder("utf-8")("replace")
            buf = ""
            try:
                while True:
                    chunk = os.read(fd, 1)
                    if not chunk:
                        break
                    text = dec.decode(chunk)
                    if not text:
                        continue
                    buf += text
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        q.put(line)
                rest = dec.decode(b"", final=True)
                buf += rest
                if buf:
                    q.put(buf)
            except Exception:
                pass
            finally:
                q.put(None)  # EOF 哨兵
                try:
                    process.stdout.close()
                except Exception:
                    pass

        def pump_stderr():
            # 持续 drain stderr，避免子进程 stderr 缓冲区打满导致死锁
            fd = process.stderr.fileno()
            try:
                while True:
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    stderr_buf.append(chunk.decode("utf-8", errors="replace"))
            except Exception:
                pass
            try:
                process.stderr.close()
            except Exception:
                pass

        t_out = threading.Thread(target=pump_stdout, daemon=True)
        t_err = threading.Thread(target=pump_stderr, daemon=True)
        t_out.start()
        t_err.start()

        event_id = 0
        yield _sse("start")

        while True:
            try:
                item = q.get(timeout=HEARTBEAT_INTERVAL)
            except queue.Empty:
                # 心跳保活；读取线程已结束时（且无更多数据）即可停止
                if not t_out.is_alive():
                    break
                yield ": heartbeat\n\n"
                continue
            if item is None:  # EOF
                break
            if item:
                event_id += 1
                yield _child_event_to_sse(item, event_id)

        t_out.join(timeout=2)
        t_err.join(timeout=2)
        rc = process.poll()
        err_text = "".join(stderr_buf).strip()
        if rc and rc != 0:
            err = err_text or f"进程退出码 {rc}"
            yield _sse("error", error=err, code="process_error")
        else:
            yield _sse("done")
        _reap(process)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "shell-tool-api"})


def _reap_orphans(*_args):
    """PID 1 兜底回收：容器内本进程是 1 号进程，任何被遗弃的子进程
    都会收养到本进程；若不回收就会变成永久僵尸。
    subprocess 内部已能处理 ChildProcessError，故此处回收不会与之冲突。"""
    try:
        while True:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
    except ChildProcessError:
        pass


if __name__ == "__main__":
    # 仅在作为 1 号进程（容器入口）时注册兜底回收
    if os.getpid() == 1:
        signal.signal(signal.SIGCHLD, _reap_orphans)
    app.run(host="0.0.0.0", port=8000, debug=False)
