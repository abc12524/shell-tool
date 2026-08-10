#!/usr/bin/env python3
from flask import Flask, request, jsonify, Response, stream_with_context
import subprocess
import sys
import os
import json

app = Flask(__name__)
# server/ 的上一级即项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEEPSEEK_PATH = os.path.join(PROJECT_ROOT, "dp.py")
# 使用当前解释器（原先硬编码 ~/.venv/bin/python 是 Linux 路径）
PYTHON = sys.executable


def build_args(data):
    """根据请求参数构建传给 deepseek.py 的命令行参数"""
    args = []
    if data.get("new"):
        args.append("-n")
    args += data.get("args", [])
    return args


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    if not data or "question" not in data:
        return jsonify({"error": "请提供 question 参数"}), 400

    question = data["question"]
    if not question.strip():
        return jsonify({"error": "question 不能为空"}), 400

    # 获取额外参数（new=True 时传 -n）
    args = build_args(data)

    # 构建完整命令：python deepseek.py [额外参数] question
    cmd = [PYTHON, DEEPSEEK_PATH] + args + [question]

    try:
        result = subprocess.run(
            cmd,  # 使用完整的命令
            capture_output=True, text=True, timeout=120,
            cwd=os.path.dirname(DEEPSEEK_PATH)
        )
        return jsonify({
            "question": question,
            "reply": result.stdout,
            "error": result.stderr if result.stderr else None
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "请求超时"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/chat/stream", methods=["POST"])
def chat_stream():
    data = request.get_json()
    if not data or "question" not in data:
        return jsonify({"error": "请提供 question 参数"}), 400

    question = data["question"]
    if not question.strip():
        return jsonify({"error": "question 不能为空"}), 400

    # 获取额外参数（new=True 时传 -n）
    args = build_args(data)

    def generate():
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        # 构建完整命令
        cmd = [PYTHON, DEEPSEEK_PATH] + args + [question]

        process = subprocess.Popen(
            cmd,  # 使用完整的命令
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.path.dirname(DEEPSEEK_PATH),
            env=env,
            bufsize=0,
        )

        # 流式处理输出...
        buf = b""
        while True:
            byte = process.stdout.read(1)
            if not byte:
                if buf:
                    yield f"data: {json.dumps({'type': 'content', 'content': buf.decode('utf-8', errors='replace')}, ensure_ascii=False)}\n\n"
                break
            buf += byte
            try:
                char = buf.decode("utf-8")
                yield f"data: {json.dumps({'type': 'content', 'content': char}, ensure_ascii=False)}\n\n"
                buf = b""
            except UnicodeDecodeError:
                pass

        process.stdout.close()
        process.wait()

        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "deepseek-api"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
