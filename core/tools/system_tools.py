#!/usr/bin/env python3
"""系统类工具：get_system_info / execute_system_command"""
import base64
import locale
import os
import platform
import shutil
import signal
import subprocess
import time

from .envelope import ok, error
from .output_trim import trim_text
from .. import config

# 截断预算里头部所占比例，其余留给尾部。tail -n / journalctl / grep -A 这类命令
# 的关键信息在末尾，纯头部截断会把最新的报错砍掉
_OUTPUT_HEAD_RATIO = 0.6


def get_system_info():
    """获取系统信息"""
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python_version": platform.python_version()
    }


def _ps_encode_command(command: str) -> list:
    """PowerShell 命令编码：含 $ 符号时用 Base64 避免转义丢失"""
    cmd = command.strip()

    # AI 经常在命令中写 powershell -Command "...script with $...".
    # 检测并剥离外层 wrapper，提取真正的 PowerShell 脚本
    stripped = cmd
    lower = cmd.lower()
    for wrapper in ('powershell.exe ', 'powershell '):
        if lower.startswith(wrapper):
            rest = cmd[len(wrapper):].strip()
            # 跳过 -NoProfile, -ExecutionPolicy Bypass 等 flag
            while rest and not rest.startswith('-Command') and not rest.startswith('-command'):
                parts = rest.split(None, 1)
                if len(parts) == 2:
                    rest = parts[1].strip()
                else:
                    rest = ''
                    break
            if rest:
                # 去掉 -Command 前缀
                rest = rest[8:].strip()  # len('-Command') = 8
                # 去掉外层引号
                if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in ('"', "'"):
                    rest = rest[1:-1]
                if rest:
                    stripped = rest
            break

    if '$' in stripped or '`' in stripped or '@(' in stripped:
        encoded = base64.b64encode(stripped.encode('utf-16-le')).decode('ascii')
        return ['powershell.exe', '-EncodedCommand', encoded]
    return ['powershell.exe', '-Command', stripped]


_IS_WINDOWS = (os.name == 'nt')
# Windows 专用标志：taskkill 是控制台程序，无窗口启动避免在 GUI 宿主里闪黑框
_CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _kill_process_tree(proc):
    """强制结束子进程及其全部子孙进程（跨平台）。

    POSIX：子进程由 start_new_session 成为独立会话/进程组 leader，killpg 连带子孙，
           再循环 waitpid 回收整个进程组，避免被 PID 1 收养后变僵尸。
    Windows：没有进程组信号——os.getpgid / os.killpg / os.WNOHANG / signal.SIGKILL
           这些接口在 Windows 上根本不存在（调用即 AttributeError），
           改用 taskkill /F /T 按父子链杀整棵树，失败再退回 proc.kill()。
    """
    if _IS_WINDOWS:
        try:
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        return

    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        return
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            pid, _ = os.waitpid(-pgid, os.WNOHANG)
            if pid == 0:
                time.sleep(0.05)
                continue
        except ChildProcessError:
            break


def run_cmd_reap(cmd_args: list, timeout: int):
    """运行子进程并在超时/异常时杀掉整棵进程树并回收，避免僵尸/孤儿。

    回收机制因平台而异（见 _kill_process_tree），这里只统一"超时 → 杀树 → 回收 → 抛出"
    的时序：超时后必须再 communicate 一次，否则管道残留输出与句柄不会释放。
    Windows 上不建独立进程组，让 Ctrl+C 仍能正常传给子进程（杀树靠 taskkill /T）。
    """
    kwargs = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE}
    if not _IS_WINDOWS:
        # POSIX：让子进程成为独立会话/进程组 leader，超时时可 killpg 连带子孙
        kwargs['start_new_session'] = True

    proc = subprocess.Popen(cmd_args, **kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            proc.communicate(timeout=10)
        except Exception:
            pass
        raise


def _decode_output(data: bytes) -> str:
    """尝试用 UTF-8 → GBK → 系统编码 逐级解码"""
    if not data:
        return ""
    for enc in ['utf-8', 'gbk', 'cp936']:
        try:
            return data.decode(enc).strip()
        except (UnicodeDecodeError, LookupError):
            continue
    # 兜底：用系统编码 + 替换
    sys_enc = locale.getpreferredencoding() or 'utf-8'
    return data.decode(sys_enc, errors='replace').strip()


def _truncate_keep_ends(text: str, limit: int):
    """超长输出保留头尾、省略中间，返回 (新文本, 是否截断)。

    纯 text[:limit] 截断会丢掉尾部，而 tail -n / journalctl / grep -A 这类命令的
    关键信息恰在末尾——线上真实案例：tail -n 150 xxx.log 的最新报错被砍掉。
    """
    if limit <= 0 or len(text) <= limit:
        return text, False

    # 上限太小时保头保尾没有意义，退化为头部截断 + 极短标记，避免预算变成负数
    if limit < 200:
        tag = f"...[{len(text) - limit}字省略]"
        return text[:max(0, limit - len(tag))] + tag, True

    budget = limit - 80  # 给省略标记留位置
    head_len = int(budget * _OUTPUT_HEAD_RATIO)
    tail_len = budget - head_len
    head, tail = text[:head_len], text[-tail_len:]

    # 向行边界收口，避免把一行切成两半
    cut = head.rfind('\n')
    if cut > head_len * 0.7:
        head = head[:cut]
    cut = tail.find('\n')
    if 0 <= cut < tail_len * 0.3:
        tail = tail[cut + 1:]

    marker = f"\n... [中间省略 {len(text) - len(head) - len(tail)} 字符 / 原长度 {len(text)} 字符] ...\n"
    result = head + marker + tail

    # 标记长度估算偏差时兜底，确保不超过 limit
    if len(result) > limit:
        excess = len(result) - limit
        head = head[:max(0, len(head) - excess)]
        result = head + marker + tail
    return result, True


def execute_system_command(command: str) -> str:
    """执行系统命令并返回结果（跨平台支持）"""
    os_type = platform.system().lower()

    try:
        # Windows 优先使用 PowerShell
        if os_type == 'windows':
            if shutil.which('powershell.exe'):
                cmd_args = _ps_encode_command(command)
            else:
                cmd_args = ['cmd.exe', '/c', command]
        else:
            # Linux/macOS 使用 bash
            if shutil.which('bash'):
                cmd_args = ['/bin/bash', '-c', command]
            else:
                cmd_args = ['/bin/sh', '-c', command]

        # 使用带进程组回收的封装：超时会杀掉整个进程组并回收，杜绝僵尸
        returncode, stdout, stderr = run_cmd_reap(cmd_args, timeout=3600)

        if stdout:
            output = _decode_output(stdout)
        elif stderr:
            output = _decode_output(stderr)
        else:
            output = "命令执行成功（无输出）"

        # 输出简化交给工具侧（默认 rtk_web 技能 → rtk-web），失败静默用原文。
        # 传 command 是为了分流：diff/JSON/源码这类内容走 chain 保真，其余 auto。
        trim_info = None
        if stdout or stderr:
            trimmed = trim_text(output, command)
            if trimmed:
                output, trim_info = trimmed

        # 兜底：正常路径由 trim 服务按 OUTPUT_TRIM_MAX_CHARS 裁好（≤ 预算且保住
        # 报错行），走不到这里；服务不可用 / 跳过修剪时才靠它防止爆上下文。
        # 这里必须保头尾 —— 纯头部截断会把 tail -n 的最新报错砍掉。
        output, truncated = _truncate_keep_ends(output, config.OUTPUT_TRIM_MAX_CHARS)

        result = {
            "output": output,
            "exit_code": returncode,
            "truncated": truncated,
        }
        if trim_info:
            result["trim"] = trim_info
        return ok(result)
    except subprocess.TimeoutExpired:
        return error("命令执行超时（1小时）", code="timeout")
    except Exception as e:
        return error(f"执行失败 - {str(e)}", code="internal")
