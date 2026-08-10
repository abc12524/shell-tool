#!/usr/bin/env python3
"""搜索类工具：baidu_search（调用 qianfan.py 子进程）"""
import os
import subprocess
import sys

from ..config import PROJECT_ROOT


def baidu_search(mode: str, query: str) -> str:
    """百度千帆搜索：调用 scripts/qianfan.py 获取搜索结果"""
    qianfan_script = os.path.join(PROJECT_ROOT, "scripts", "qianfan.py")
    if not os.path.exists(qianfan_script):
        return '{"error": "qianfan.py 不存在"}'
    try:
        # 强制子进程使用 UTF-8 输出，避免 Windows 终端编码干扰
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, qianfan_script, mode, query],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=60, env=env
        )
        if result.stdout:
            return result.stdout.strip()
        elif result.stderr:
            return f'{{"error": "{result.stderr.strip()}"}}'
        else:
            return '{"error": "搜索无返回"}'
    except subprocess.TimeoutExpired:
        return '{"error": "搜索超时（60秒）"}'
    except Exception as e:
        return f'{{"error": "搜索失败 - {str(e)}"}}'
