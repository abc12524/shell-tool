# skill: at
# description: 控制 Unix at 定时任务（创建 / 查看 / 删除到点执行一次的命令）
# status: enable
# usage:
#   arguments.action = schedule | list | remove | show
#     schedule  创建一个定时任务（需 time + command）
#     list      列出待执行任务（atq）
#     remove    按编号删除任务（atrm，需 id）
#     show      查看任务的执行脚本（at -c，需 id）
#   schedule 参数：
#     time     时间表达式，如 "now + 2 minutes" / "15:30" / "2026-10-12 15:30" / "midnight"
#     command  到点执行的 shell 命令（字符串，可含换行）
#     queue    可选队列字母（at -q，默认 a）
#     mail     可选，true 时允许发邮件（Linux 默认加 -M 抑制邮件）
#   remove/show 参数：
#     id       任务编号（atq 列表 / schedule 返回的 id）
#   示例: {"action": "schedule", "time": "now + 2 minutes", "command": "touch /tmp/at_demo"}
#         {"action": "list"}
#         {"action": "remove", "id": "3"}
import os
import re
import shutil
import subprocess
import sys

from core.tools.envelope import ok, error

_JOB_ID_RE = re.compile(r"job\s+(\d+)", re.IGNORECASE)
_ATQ_RE = re.compile(r"^(\d+)\s+(.*?)\s+([A-Za-z])\s+(\S+)\s*$")


def _at_bin():
    return os.environ.get("AT_BIN") or shutil.which("at")


def _run(args, input_text=None, timeout=20):
    return subprocess.run(
        args,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def _require_at():
    if os.name == "nt":
        return None, error(
            "Windows 上的 at 与 Unix at 语义不同，本 skill 仅支持 Unix at 环境",
            code="unsupported_platform",
        )
    binary = _at_bin()
    if not binary:
        return None, error("未找到 at 命令（请安装 at 并确保在 PATH 中）", code="not_found")
    return binary, None


def _schedule(a):
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
        r = _run(argv, input_text=script)
    except subprocess.TimeoutExpired:
        return error("at 执行超时", code="timeout")
    if r.returncode != 0:
        return error((r.stderr or r.stdout or "at 创建任务失败").strip(), code="at_failed")

    text = ((r.stdout or "") + (r.stderr or "")).strip()
    m = _JOB_ID_RE.search(text)
    return ok({
        "id": m.group(1) if m else None,
        "time": when,
        "queue": queue or "a",
        "message": text,
    })


def _list(a):
    _, err = _require_at()
    if err:
        return err
    atq = os.environ.get("ATQ_BIN") or shutil.which("atq")
    if not atq:
        return error("未找到 atq 命令", code="not_found")
    try:
        r = _run([atq])
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
    return ok({"count": len(jobs), "jobs": jobs})


def _remove(a):
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
        r = _run([atrm, job_id])
    except subprocess.TimeoutExpired:
        return error("atrm 执行超时", code="timeout")
    if r.returncode != 0:
        return error((r.stderr or f"删除任务 {job_id} 失败").strip(), code="at_failed")
    return ok({"id": job_id, "removed": True})


def _show(a):
    binary, err = _require_at()
    if err:
        return err
    job_id = str(a.get("id") or "").strip()
    if not job_id:
        return error("缺少 id（atq 列表中的任务编号）", code="missing_argument")
    if not job_id.isdigit():
        return error("id 必须是数字", code="bad_request")
    try:
        r = _run([binary, "-c", job_id])
    except subprocess.TimeoutExpired:
        return error("at -c 执行超时", code="timeout")
    if r.returncode != 0:
        return error((r.stderr or f"查看任务 {job_id} 失败").strip(), code="at_failed")
    return ok({"id": job_id, "script": r.stdout})


_ACTIONS = {
    "schedule": _schedule,
    "list": _list,
    "remove": _remove,
    "show": _show,
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
