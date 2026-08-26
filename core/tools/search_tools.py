#!/usr/bin/env python3
"""搜索类工具：baidu_search（调用 qianfan.py 子进程）"""
import json
import os
import sys
import subprocess

from ..config import PROJECT_ROOT
from .system_tools import run_cmd_reap
from .envelope import ok, error


def baidu_search(mode: str, query: str) -> str:
    """百度千帆搜索：调用 scripts/qianfan.py 获取搜索结果"""
    qianfan_script = os.path.join(PROJECT_ROOT, "scripts", "qianfan.py")
    if not os.path.exists(qianfan_script):
        return error("qianfan.py 不存在", code="missing_script")
    try:
        # 强制子进程使用 UTF-8 输出，避免 Windows 终端编码干扰
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        # 使用带进程组回收的封装：超时会杀掉整个进程组并回收，杜绝僵尸
        _, stdout, stderr = run_cmd_reap(
            [sys.executable, qianfan_script, mode, query],
            timeout=3600,
        )
        if stdout:
            text = stdout.decode('utf-8', errors='replace').strip()
            # qianfan.py 本身已输出结构化 JSON，原样作为 result 透传
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = {"raw": text}
            return ok(payload)
        elif stderr:
            return error(stderr.decode('utf-8', errors='replace').strip(), code="search_failed")
        else:
            return error("搜索无返回", code="empty")
    except subprocess.TimeoutExpired:
        return error("搜索超时（1小时）", code="timeout")
    except Exception as e:
        return error(f"搜索失败 - {str(e)}", code="internal")
