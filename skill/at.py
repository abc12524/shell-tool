# skill: at
# description: 定时任务管理（创建 / 查看 / 删除到点执行一次的命令）
# status: enable
# usage:
#   arguments.action = schedule | list | remove | show
#     schedule  创建一个定时任务（需 time + command）
#     list      列出待执行的定时任务
#     remove    按编号删除任务（需 id）
#     show      查看任务详情（需 id）
#   schedule 参数：
#     time     时间表达式，如 "now + 2 minutes" / "15:30" / "2026-10-12 15:30" / "midnight"
#     command  到点执行的命令（字符串，可含换行）
#     name     可选，任务名（默认自动生成）
#     queue    可选，队列字母（默认 a）
#     mail     可选，是否允许发邮件
#   remove/show 参数：
#     id       任务标识（list / schedule 返回值）
#   示例: {"action": "schedule", "time": "now + 2 minutes", "command": "touch /tmp/at_demo"}
#         {"action": "list"}
#         {"action": "remove", "id": "3"}
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

from core.tools.envelope import ok, error

_IS_WIN = os.name == "nt"
_JOB_ID_RE = re.compile(r"job\s+(\d+)", re.IGNORECASE)
_ATQ_RE = re.compile(r"^(\d+)\s+(.*?)\s+([A-Za-z])\s+(\S+)\s*$")
_REL_RE = re.compile(
    r"(\d+)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\b",
    re.IGNORECASE,
)
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")
_TASK_PREFIX = "shelltool_at_"


# ---------------------------------------------------------------- Unix (at) ---
def _at_bin():
    return os.environ.get("AT_BIN") or shutil.which("at")


def _require_at():
    binary = _at_bin()
    if not binary:
        return None, error("未找到 at 命令（请安装 at 并确保在 PATH 中）", code="not_found")
    return binary, None


def _u_run(args, input_text=None, timeout=20):
    return subprocess.run(
        args,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def _u_schedule(a):
    binary, err = _require_at()
    if err:
        return err
    when = (a.get("time") or "").strip()
    command = a.get("command")
    if not when:
        return error("缺少 time（如 'now + 2 minutes' / '15:30'）", code="missing_argument")
    if command is None or not str(command).strip():
        return error("缺少 command（到点执行的命令）", code="missing_argument")

    argv = [binary]
    queue = (a.get("queue") or "").strip()
    if queue:
        if not re.fullmatch(r"[A-Za-z]", queue):
            return error("queue 必须是单个字母（at 队列标识）", code="bad_request")
        argv += ["-q", queue]
    if not a.get("mail", False) and sys.platform.startswith("linux"):
        argv.append("-M")
    argv.append(when)

    script = str(command)
    if not script.endswith("\n"):
        script += "\n"
    try:
        r = _u_run(argv, input_text=script)
    except subprocess.TimeoutExpired:
        return error("at 执行超时", code="timeout")
    if r.returncode != 0:
        return error((r.stderr or r.stdout or "at 创建任务失败").strip(), code="at_failed")

    text = ((r.stdout or "") + (r.stderr or "")).strip()
    m = _JOB_ID_RE.search(text)
    return ok({
        "platform": "unix",
        "id": m.group(1) if m else None,
        "time": when,
        "queue": queue or "a",
        "message": text,
    })


def _u_list(a):
    _, err = _require_at()
    if err:
        return err
    atq = os.environ.get("ATQ_BIN") or shutil.which("atq")
    if not atq:
        return error("未找到 atq 命令", code="not_found")
    try:
        r = _u_run([atq])
    except subprocess.TimeoutExpired:
        return error("atq 执行超时", code="timeout")
    if r.returncode != 0:
        return error((r.stderr or "atq 执行失败").strip(), code="at_failed")

    jobs = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _ATQ_RE.match(line)
        if m:
            jobs.append({"id": m.group(1), "when": m.group(2), "queue": m.group(3), "user": m.group(4)})
        else:
            jobs.append({"raw": line})
    return ok({"platform": "unix", "count": len(jobs), "jobs": jobs})


def _u_remove(a):
    _, err = _require_at()
    if err:
        return err
    job_id = str(a.get("id") or "").strip()
    if not job_id:
        return error("缺少 id（atq 列表中的任务编号）", code="missing_argument")
    if not job_id.isdigit():
        return error("id 必须是数字", code="bad_request")
    atrm = os.environ.get("ATRM_BIN") or shutil.which("atrm")
    if not atrm:
        return error("未找到 atrm 命令", code="not_found")
    try:
        r = _u_run([atrm, job_id])
    except subprocess.TimeoutExpired:
        return error("atrm 执行超时", code="timeout")
    if r.returncode != 0:
        return error((r.stderr or f"删除任务 {job_id} 失败").strip(), code="at_failed")
    return ok({"platform": "unix", "id": job_id, "removed": True})


def _u_show(a):
    binary, err = _require_at()
    if err:
        return err
    job_id = str(a.get("id") or "").strip()
    if not job_id:
        return error("缺少 id（atq 列表中的任务编号）", code="missing_argument")
    if not job_id.isdigit():
        return error("id 必须是数字", code="bad_request")
    try:
        r = _u_run([binary, "-c", job_id])
    except subprocess.TimeoutExpired:
        return error("at -c 执行超时", code="timeout")
    if r.returncode != 0:
        return error((r.stderr or f"查看任务 {job_id} 失败").strip(), code="at_failed")
    return ok({"platform": "unix", "id": job_id, "script": r.stdout})


# ----------------------------------------------------- Windows (Task Scheduler) ---
def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def _decode(data):
    for enc in ("utf-8", sys.getfilesystemencoding() or "utf-8", "gbk"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _powershell(script, timeout=60):
    exe = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
    try:
        r = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "", "PowerShell 执行超时"
    return r.returncode, _decode(r.stdout or b""), _decode(r.stderr or b"")


def _parse_when(text):
    t = (text or "").strip()
    low = t.lower()
    if not low:
        return None
    now = datetime.now()
    if low == "midnight":
        dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return dt if dt > now else dt + timedelta(days=1)
    if low == "noon":
        dt = now.replace(hour=12, minute=0, second=0, microsecond=0)
        return dt if dt > now else dt + timedelta(days=1)
    if low.startswith("now"):
        rest = low[3:].strip()
        if not rest:
            return now
        if not rest.startswith("+"):
            return None
        total = timedelta()
        matched = False
        for num, unit in _REL_RE.findall(rest):
            matched = True
            n = int(num)
            u = unit[0].lower()
            if u == "s":
                total += timedelta(seconds=n)
            elif u == "m":
                total += timedelta(minutes=n)
            elif u == "h":
                total += timedelta(hours=n)
            elif u == "d":
                total += timedelta(days=n)
        return now + total if matched else None
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
    ):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            tm = datetime.strptime(t, fmt)
        except ValueError:
            continue
        dt = now.replace(hour=tm.hour, minute=tm.minute, second=tm.second, microsecond=0)
        return dt if dt > now else dt + timedelta(days=1)
    return None


def _w_schedule(a):
    when = (a.get("time") or "").strip()
    command = a.get("command")
    if not when:
        return error("缺少 time（Windows 支持 'now + 2 minutes' / '15:30' / '2026-10-12 15:30'）", code="missing_argument")
    if command is None or not str(command).strip():
        return error("缺少 command（到点执行的命令）", code="missing_argument")
    dt = _parse_when(when)
    if dt is None:
        return error(
            f"无法解析 time '{when}'（Windows 支持 'now + N minutes|hours|days' / 'HH:MM' / "
            "'YYYY-MM-DD HH:MM[:SS]' / 'midnight' / 'noon'）",
            code="bad_request",
        )
    name = (a.get("name") or "").strip() or (_TASK_PREFIX + dt.strftime("%Y%m%d%H%M%S"))
    if not _NAME_RE.match(name):
        return error("name 仅允许字母/数字/下划线/中划线/点", code="bad_request")

    arg = "/c " + str(command)
    at_str = dt.strftime("%Y-%m-%dT%H:%M:%S")
    script = (
        "$ErrorActionPreference='Stop';"
        f"$a=New-ScheduledTaskAction -Execute 'cmd.exe' -Argument {_ps_quote(arg)};"
        f"$t=New-ScheduledTaskTrigger -Once -At (Get-Date {_ps_quote(at_str)});"
        "$s=New-ScheduledTaskSettingsSet -StartWhenAvailable;"
        f"Register-ScheduledTask -TaskName {_ps_quote(name)} -Action $a -Trigger $t -Settings $s -Force | Out-Null;"
        "Write-Output 'OK'"
    )
    rc, out, err = _powershell(script)
    if rc != 0:
        return error((err or out or "注册计划任务失败").strip(), code="task_failed")
    return ok({
        "platform": "windows",
        "id": name,
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "message": (out or "").strip(),
    })


def _w_list(a):
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"$items=@(Get-ScheduledTask -TaskName '{_TASK_PREFIX}*' | ForEach-Object {{"
        "  $i=$_ | Get-ScheduledTaskInfo;"
        "  $nr=if($i.NextRunTime){$i.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss')}else{''};"
        "  [pscustomobject]@{Name=$_.TaskName;State=[string]$_.State;NextRun=$nr}"
        "});"
        "if($items.Count -eq 0){'[]'}else{$items | ConvertTo-Json -Compress}"
    )
    rc, out, err = _powershell(script)
    if rc != 0:
        return error((err or out or "查询计划任务失败").strip(), code="task_failed")
    out = out.strip()
    try:
        data = json.loads(out) if out else []
    except json.JSONDecodeError:
        return error("解析计划任务列表失败", code="internal")
    if isinstance(data, dict):
        data = [data]
    jobs = [
        {"id": x.get("Name"), "when": str(x.get("NextRun") or ""), "state": x.get("State")}
        for x in data
    ]
    return ok({"platform": "windows", "count": len(jobs), "jobs": jobs})


def _w_remove(a):
    name = str(a.get("id") or a.get("name") or "").strip()
    if not name:
        return error("缺少 id（计划任务名）", code="missing_argument")
    script = (
        "$ErrorActionPreference='Stop';"
        f"Unregister-ScheduledTask -TaskName {_ps_quote(name)} -Confirm:$false;"
        "Write-Output 'OK'"
    )
    rc, out, err = _powershell(script)
    if rc != 0:
        return error((err or out or f"删除任务 {name} 失败").strip(), code="task_failed")
    return ok({"platform": "windows", "id": name, "removed": True})


def _w_show(a):
    name = str(a.get("id") or a.get("name") or "").strip()
    if not name:
        return error("缺少 id（计划任务名）", code="missing_argument")
    script = (
        "$ErrorActionPreference='Stop';"
        f"$t=Get-ScheduledTask -TaskName {_ps_quote(name)};"
        "$i=$t|Get-ScheduledTaskInfo;"
        "$nr=if($i.NextRunTime){$i.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss')}else{''};"
        "[pscustomobject]@{Name=$t.TaskName;Execute=$t.Actions.Execute;"
        "Arguments=$t.Actions.Arguments;State=[string]$t.State;NextRun=$nr}"
        "|ConvertTo-Json -Compress"
    )
    rc, out, err = _powershell(script)
    if rc != 0:
        return error((err or out or f"查看任务 {name} 失败").strip(), code="task_failed")
    try:
        d = json.loads(out.strip())
    except json.JSONDecodeError:
        return error("解析任务详情失败", code="internal")
    return ok({
        "platform": "windows",
        "id": d.get("Name"),
        "execute": d.get("Execute"),
        "arguments": d.get("Arguments"),
        "state": d.get("State"),
        "when": str(d.get("NextRun") or ""),
    })


_ACTIONS = {
    "schedule": _w_schedule if _IS_WIN else _u_schedule,
    "list": _w_list if _IS_WIN else _u_list,
    "remove": _w_remove if _IS_WIN else _u_remove,
    "show": _w_show if _IS_WIN else _u_show,
}


def run(arguments):
    a = arguments or {}
    action = (a.get("action") or "").strip()
    fn = _ACTIONS.get(action)
    if not fn:
        return error(
            f"未知 action '{action}'，可用: {', '.join(_ACTIONS)}",
            code="unknown_action",
        )
    return fn(a)
