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
import urllib.parse
import urllib.request

from .envelope import ok, error
from .. import config


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


# rtk-web 请求体上限（服务端 RTK_WEB_MAX_BODY 默认 10 MiB）；过大时直接跳过修剪
_RTK_WEB_MAX_BYTES = 9 * 1024 * 1024
# 服务失败后的熔断截止时间（monotonic），避免服务宕机时每条命令都卡超时
_RTK_WEB_DOWN_UNTIL = 0.0


def _rtk_web_base_url() -> str:
    """按 RTK_WEB_HOST / RTK_WEB_PORT 拼服务地址，兼容 host 自带 scheme/port。

    未配置 host / port 时返回空字符串，调用方跳过修剪。
    """
    host = config.RTK_WEB_HOST.strip()
    if not host:
        return ''
    if '://' not in host:
        host = f'http://{host}'
    parts = urllib.parse.urlsplit(host)
    netloc = parts.netloc
    if parts.port is None:
        if not config.RTK_WEB_PORT:
            return ''
        netloc = f'{netloc}:{config.RTK_WEB_PORT}'
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path.rstrip('/'), '', ''))


def _header_number(headers, name: str, default):
    """读取 X-Rtk-* 数字头，缺失或非法时返回默认值。"""
    value = headers.get(name)
    if value is None:
        return default
    try:
        return type(default)(value)
    except (TypeError, ValueError):
        return default


def _trim_with_rtk_web(text: str):
    """把命令输出交给 rtk-web 压缩。

    成功返回 (压缩后文本, 统计)；服务不可达、超时、非 2xx、解码失败或
    压缩后不更短时返回 None，调用方直接使用原始输出。
    """
    global _RTK_WEB_DOWN_UNTIL
    if not config.RTK_WEB_ENABLED or not text:
        return None
    if time.monotonic() < _RTK_WEB_DOWN_UNTIL:
        return None

    base_url = _rtk_web_base_url()
    if not base_url:
        return None

    payload = text.encode('utf-8')
    if len(payload) > _RTK_WEB_MAX_BYTES:
        return None

    try:
        query = urllib.parse.urlencode({
            'format': config.RTK_WEB_FORMAT,
            'mode': config.RTK_WEB_MODE,
            'lossless': 'false',
            'charset': 'utf-8',
        })
        request = urllib.request.Request(
            f'{base_url}/api/compress?{query}',
            data=payload,
            headers={'Content-Type': 'text/plain; charset=utf-8'},
            method='POST',
        )
        with urllib.request.urlopen(request, timeout=config.RTK_WEB_TIMEOUT) as response:
            body = response.read()
            headers = response.headers
    except Exception:
        if config.RTK_WEB_RETRY_SECONDS > 0:
            _RTK_WEB_DOWN_UNTIL = time.monotonic() + config.RTK_WEB_RETRY_SECONDS
        return None

    _RTK_WEB_DOWN_UNTIL = 0.0
    try:
        trimmed = body.decode('utf-8')
    except UnicodeDecodeError:
        return None
    if not trimmed or len(trimmed) >= len(text):
        return None

    stats = {
        'saved_percent': _header_number(
            headers,
            'X-Rtk-Saved-Percent',
            round((len(text) - len(trimmed)) / len(text) * 100, 1),
        ),
        'bytes_before': _header_number(headers, 'X-Rtk-Bytes-Before', len(payload)),
        'bytes_after': _header_number(headers, 'X-Rtk-Bytes-After', len(body)),
    }
    return trimmed, stats


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

        # 先过 rtk-web 修剪，再按字符上限截断；服务异常时保留原始输出
        compression = None
        if stdout or stderr:
            try:
                trimmed = _trim_with_rtk_web(output)
            except Exception:
                trimmed = None
            if trimmed:
                output, compression = trimmed

        # 限制输出长度，避免 token 过大
        max_length = 6000
        truncated = False
        if len(output) > max_length:
            output = output[:max_length] + f"\n... (输出被截断，原长度 {len(output)} 字符)"
            truncated = True

        result = {
            "output": output,
            "exit_code": returncode,
            "truncated": truncated,
        }
        if compression:
            result["compression"] = compression
        return ok(result)
    except subprocess.TimeoutExpired:
        return error("命令执行超时（1小时）", code="timeout")
    except Exception as e:
        return error(f"执行失败 - {str(e)}", code="internal")
