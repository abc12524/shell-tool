#!/usr/bin/env python3
"""部署脚本：克隆仓库后运行一次，把 shell-tool 装成命令行命令 `ai`。

做的事（可重复运行，只补缺失、不覆盖已有配置）：
  1. 检查 Python 环境：优先用系统 python3 在 ~/.venv 创建虚拟环境，并按 requirements.txt
     安装依赖；已有且依赖齐全则直接复用（--no-venv / --venv 可调整）。
  2. 生成启动器：POSIX 下 ~/.local/bin/ai，Windows 下 ~/.local/bin/ai.cmd；
     并把该目录加进 PATH（--no-path 可跳过）。启动器固定使用上面的 Python。
  3. Windows 下在 PowerShell profile 里写入/刷新 `ai` 函数（带完整选项与交互模式）；
     用 $args 透传参数，避免 -I/-D 被 PowerShell 当成公共参数吃掉（--no-ps-profile 可跳过）。
  4. 给 dp.py 加执行权限（POSIX）。
  5. 用 .env.example 在 ~/.config/shell-tool/ 下创建 .env
     （仓库里已有 .env 时优先沿用其内容，避免丢 Key）。
  6. data/ 与 skill/ 放到 ~/.config/shell-tool/ 下，并把仓库里已有的数据 / 技能补拷过去。

用法：
    python deploy.py                     # 默认安装
    python deploy.py --no-path           # 不改 PATH，只生成启动器
    python deploy.py --no-venv           # 不建虚拟环境，直接用系统 python3
    python deploy.py --venv ~/venvs/ai   # 自定义虚拟环境目录
    python deploy.py --no-ps-profile     # 不写入 PowerShell ai 函数
    python deploy.py --home ~/my-conf    # 自定义配置目录
    python deploy.py --bin ~/.local/bin  # 自定义启动器目录
    python deploy.py --ps-profile <路径> # 自定义 PowerShell profile（测试/特殊布局）

运行时 core/config.py 按 SHELL_TOOL_HOME > ~/.config/shell-tool（存在 .env 时）> 仓库目录
的顺序定位配置，因此启动器无需再传路径。
"""
import argparse
import os
import shutil
import stat
import subprocess
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = sys.platform == "win32"
LAUNCHER_NAME = "ai.cmd" if IS_WINDOWS else "ai"
DEFAULT_HOME = os.path.join(os.path.expanduser("~"), ".config", "shell-tool")
DEFAULT_BIN = os.path.join(os.path.expanduser("~"), ".local", "bin")
DEFAULT_VENV = os.path.join(os.path.expanduser("~"), ".venv")


def say(msg):
    print(msg, flush=True)


def warn(msg):
    print(msg, file=sys.stderr, flush=True)


def same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# ---------- Python 环境 ----------

def system_python():
    """系统 Python：优先 python3，其次 python / py -3 / 当前解释器。

    只返回真正能执行 `-c` 的解释器，避开 Windows 商店的 python3 占位程序。
    """
    candidates = []
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    if IS_WINDOWS:
        py = shutil.which("py")
        if py:
            candidates.append(py)  # 下面用 -3 参数探测
    candidates.append(sys.executable)
    for candidate in candidates:
        if not candidate:
            continue
        args = [candidate, "-3", "-c", "import sys; sys.exit(0)"] \
            if os.path.basename(candidate).lower() == "py.exe" else \
            [candidate, "-c", "import sys; sys.exit(0)"]
        try:
            if subprocess.run(args, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=15).returncode == 0:
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    return sys.executable


def venv_python(venv_dir):
    return os.path.join(venv_dir, "Scripts" if IS_WINDOWS else "bin",
                        "python.exe" if IS_WINDOWS else "python3")


def _has_deps(py):
    """检查运行 shell-tool 所需的依赖是否齐全。"""
    try:
        return subprocess.run(
            [py, "-c", "import requests, dotenv, rich, flask, pymysql"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run(cmd, what):
    """执行命令，失败时给出末几行输出而不是整段 traceback。"""
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        warn(f"  ! {what}失败：{e}")
        return False
    if result.returncode == 0:
        return True
    tail = "\n".join((result.stdout or "").strip().splitlines()[-6:])
    warn(f"  ! {what}失败（exit {result.returncode}）")
    if tail:
        warn("    " + tail.replace("\n", "\n    "))
    return False


def ensure_python_env(venv_dir, use_venv=True):
    """确保有可用的 Python 及依赖，返回要写入启动器的解释器路径。"""
    system = system_python()
    say(f"  = 系统 Python：{system}")
    if not use_venv:
        return system

    py = venv_python(venv_dir)
    if os.path.isfile(py) and _has_deps(py):
        say(f"  = 复用已有虚拟环境 {venv_dir}")
        return py
    if not os.path.isfile(py):
        say(f"  + 用 {system} 创建虚拟环境 {venv_dir}")
        if not _run([system, "-m", "venv", venv_dir], "创建虚拟环境"):
            warn("  ! 回退使用系统 Python")
            return system
    req = os.path.join(REPO, "requirements.txt")
    if not os.path.isfile(req):
        warn(f"  ! 缺少 {req}，跳过依赖安装")
        return py
    say("  + 安装依赖 requirements.txt")
    if not _run([py, "-m", "pip", "install", "-r", req], "安装依赖"):
        warn("  ! 依赖未装齐，首次运行 ai 可能报 ModuleNotFoundError")
    return py


# ---------- .env / data / skill ----------

def setup_home(home):
    """建好配置目录，并从仓库补 .env、data/、skill/。"""
    os.makedirs(home, exist_ok=True)

    for sub in ("data", "skill"):
        path = os.path.join(home, sub)
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)

    # skill 包需要 __init__.py：CONFIG_HOME 下的 skill 若是命名空间包，
    # 会被仓库自带的普通包 skill（含 __init__.py）抢先命中，导致技能导入失败。
    init_py = os.path.join(home, "skill", "__init__.py")
    if not os.path.exists(init_py):
        with open(init_py, "w", encoding="utf-8"):
            pass

    env_dst = os.path.join(home, ".env")
    if os.path.exists(env_dst):
        say(f"  = 保留已有配置 {env_dst}")
    elif os.path.exists(os.path.join(REPO, ".env")):
        shutil.copyfile(os.path.join(REPO, ".env"), env_dst)
        say(f"  + 已沿用仓库 .env → {env_dst}")
    elif os.path.exists(os.path.join(REPO, ".env.example")):
        shutil.copyfile(os.path.join(REPO, ".env.example"), env_dst)
        say(f"  + 已用 .env.example 创建 {env_dst}")
    else:
        warn(f"  ! 缺少 .env.example，请手动创建 {env_dst}")

    if same_path(home, REPO):
        return

    for sub in ("data", "skill"):
        n = copy_missing(os.path.join(REPO, sub), os.path.join(home, sub))
        if n:
            say(f"  + 已从仓库迁移 {sub}/（{n} 项）")


def copy_missing(src_dir, dst_dir):
    """把 src_dir 里的条目补拷到 dst_dir，已存在的不覆盖；返回拷贝条数。"""
    if not os.path.isdir(src_dir):
        return 0
    copied = 0
    for name in sorted(os.listdir(src_dir)):
        if name in ("__pycache__", ".DS_Store"):
            continue
        src = os.path.join(src_dir, name)
        dst = os.path.join(dst_dir, name)
        if os.path.exists(dst):
            continue
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(src, dst)
            copied += 1
        except OSError as e:
            warn(f"  ! 迁移 {src} 失败：{e}")
    return copied


# ---------- 启动器 ----------

def unix_launcher(python_path):
    repo = REPO.replace("'", "'\\''")
    py = python_path.replace("'", "'\\''")
    return f"""#!/bin/sh
# shell-tool 启动器（deploy.py 生成；重新运行 deploy.py 可刷新）
DEFAULT_REPO='{repo}'
DEFAULT_PY='{py}'
REPO="${{SHELL_TOOL_REPO:-$DEFAULT_REPO}}"
PY="${{SHELL_TOOL_PYTHON:-}}"
if [ -z "$PY" ] && [ -x "$DEFAULT_PY" ]; then PY="$DEFAULT_PY"; fi
if [ -z "$PY" ]; then
    if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
fi
if [ ! -f "$REPO/dp.py" ]; then
    echo "ai: 找不到 shell-tool 仓库: $REPO（可设 SHELL_TOOL_REPO 覆盖）" >&2
    exit 1
fi
PYTHONIOENCODING="${{PYTHONIOENCODING:-utf-8}}" PYTHONUTF8=1 exec "$PY" "$REPO/dp.py" "$@"
"""


def windows_launcher(python_path):
    # .cmd 里只用 ASCII：避免 cmd.exe 代码页把中文提示变成乱码
    return f"""@echo off
rem shell-tool launcher (generated by deploy.py; re-run deploy.py to refresh)
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not defined SHELL_TOOL_REPO set "SHELL_TOOL_REPO={REPO}"
if not exist "%SHELL_TOOL_REPO%\\dp.py" (
  echo ai: shell-tool repo not found: %SHELL_TOOL_REPO% ^(set SHELL_TOOL_REPO to override^) 1>&2
  exit /b 1
)
if not defined SHELL_TOOL_PYTHON set "SHELL_TOOL_PYTHON={python_path}"
if defined SHELL_TOOL_PYTHON (
  "%SHELL_TOOL_PYTHON%" "%SHELL_TOOL_REPO%\\dp.py" %*
  exit /b %ERRORLEVEL%
)
where python3 >nul 2>nul
if %ERRORLEVEL%==0 (
  python3 "%SHELL_TOOL_REPO%\\dp.py" %*
  exit /b %ERRORLEVEL%
)
py -3 "%SHELL_TOOL_REPO%\\dp.py" %*
exit /b %ERRORLEVEL%
"""


def write_launcher(bin_dir, python_path):
    os.makedirs(bin_dir, exist_ok=True)
    path = os.path.join(bin_dir, LAUNCHER_NAME)
    content = (windows_launcher(python_path) if IS_WINDOWS
               else unix_launcher(python_path))
    with open(path, "w", encoding="utf-8", newline="\r\n" if IS_WINDOWS else "\n") as f:
        f.write(content)
    if not IS_WINDOWS:
        os.chmod(path, 0o755)
    say(f"  + 已生成启动器 {path}")
    return path


# ---------- PowerShell ai 函数 ----------

PS_PROFILE_MARK_BEGIN = "# >>> shell-tool ai function >>>"
PS_PROFILE_MARK_END = "# <<< shell-tool ai function <<<"

# 占位符 __HOME__ / __REPO__ 会被替换成 PowerShell 单引号字符串。
# 用 $args 而非 param(ValueFromRemainingArguments)：后者会让 PowerShell 把
# -I 当成 -InformationAction/-InformationVariable、-D 当成 -Debug 吃掉。
_PS_AI_TEMPLATE = r"""# >>> shell-tool ai function >>>
# shell-tool ai 函数（deploy.py 生成；重新运行 deploy.py 可刷新）
$env:SHELL_TOOL_HOME = __HOME__
$env:PYTHONIOENCODING = 'utf-8'

$AI_HELP = @'
shell-tool AI 命令

用法：
  ai "你的问题"                 单次提问（默认复用最近会话）
  ai                            进入交互模式
  ai -n "你的问题"              新开会话后提问
  ai -n                         只新开会话，不提问
  ai -s [会话ID]                列出最近 5 条会话 / 继续指定会话
  ai -S [名称 enable|disable]   列出 / 开关本地 skill
  ai -I [名称...]               列出远程可装 skill / 安装（名称不完整按前缀搜索）
  ai -D <名称...>               删除本地已装 skill
  ai -k <key>                   覆盖 DeepSeek API Key 并写回 .env
  ai -m <model>                 覆盖模型名并写回 .env
  ai -h / --help                显示本帮助

交互模式命令（仅 ai 不带参数时）：
  :new / :n        新开会话       :sessions / :s   会话列表
  :skills / :sk    本地 skill     :install / :i    远程 skill 列表/安装
  :delete / :d     删除本地 skill :key / :k        覆盖 API Key
  :model / :m      覆盖模型名     :help / :?       显示本帮助
  :q / :quit / exit  退出
'@

function ai {
    $Rest = @($args | ForEach-Object { [string]$_ })

    if ($Rest.Count -gt 0 -and ($Rest[0] -in @('-h', '--help', '?'))) {
        Write-Host $AI_HELP
        return
    }

    $entry = Join-Path __REPO__ 'dp.py'
    if (-not (Test-Path $entry)) {
        Write-Host "[ai] 未找到入口: $entry" -ForegroundColor Red
        return
    }

    $py = if ($env:SHELL_TOOL_PYTHON) { $env:SHELL_TOOL_PYTHON } else { __PYTHON__ }
    if (-not (Test-Path $py)) {
        Write-Host "[ai] 未找到 Python: $py（可设 SHELL_TOOL_PYTHON 覆盖）" -ForegroundColor Red
        return
    }

    $oldEnc = [Console]::OutputEncoding
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
    try {
        # 带参数：按 dp.py 原生参数执行一次（含 -I / -D / -S / -s / -k / -m）
        if ($Rest.Count -gt 0) {
            & $py $entry @Rest
            return
        }

        # 不带参数：交互模式（每轮复用最近会话，保证上下文连续）
        Write-Host 'shell-tool 交互模式 —— 直接输入问题回车发送' -ForegroundColor Cyan
        Write-Host '  :help 查看全部命令；:q 退出' -ForegroundColor DarkGray

        while ($true) {
            $raw = Read-Host '你'
            if ($null -eq $raw) { break }
            $line = $raw.Trim()
            if ($line -eq '') { continue }

            $parts = @($line -split '\s+')
            $cmd = $parts[0].ToLowerInvariant()
            $tail = @($parts | Select-Object -Skip 1)

            if ($cmd -in @(':q', ':quit', 'exit')) { break }
            elseif ($cmd -in @(':new', ':n'))        { & $py $entry -n @tail }
            elseif ($cmd -in @(':sessions', ':s'))   { & $py $entry -s @tail }
            elseif ($cmd -in @(':skills', ':sk'))    { & $py $entry -S @tail }
            elseif ($cmd -in @(':install', ':i'))    { & $py $entry -I @tail }
            elseif ($cmd -in @(':delete', ':d'))     { & $py $entry -D @tail }
            elseif ($cmd -in @(':key', ':k'))        { & $py $entry -k @tail }
            elseif ($cmd -in @(':model', ':m'))      { & $py $entry -m @tail }
            elseif ($cmd -in @(':help', ':?', ':h')) { Write-Host $AI_HELP }
            else                                     { & $py $entry $line }
        }
        Write-Host '已退出 shell-tool 交互模式' -ForegroundColor DarkGray
    }
    finally {
        [Console]::OutputEncoding = $oldEnc
    }
}
# <<< shell-tool ai function <<<
"""


def _ps_quote(value):
    """转 PowerShell 单引号字符串（内部单引号加倍）。"""
    return "'" + str(value).replace("'", "''") + "'"


def powershell_profile_paths():
    """PowerShell 7 与 Windows PowerShell 5.1 的当前用户 profile 路径。

    SHELL_TOOL_PS_PROFILE 可用分号分隔覆盖，便于测试与特殊布局。
    """
    custom = os.environ.get("SHELL_TOOL_PS_PROFILE", "").strip()
    if custom:
        return [os.path.abspath(os.path.expanduser(p.strip()))
                for p in custom.split(";") if p.strip()]
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return [os.path.join(docs, "PowerShell", "Microsoft.PowerShell_profile.ps1"),
            os.path.join(docs, "WindowsPowerShell", "Microsoft.PowerShell_profile.ps1")]


def _read_text_smart(path):
    """兼容 UTF-8(BOM) / GBK 的已有 profile，避免改写时损坏原有中文。"""
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def write_powershell_profile(repo, home, python_path):
    """在 PowerShell profile 中写入/刷新受管理的 ai 函数块（幂等）。"""
    block = (_PS_AI_TEMPLATE.replace("__HOME__", _ps_quote(home))
             .replace("__REPO__", _ps_quote(repo))
             .replace("__PYTHON__", _ps_quote(python_path)))
    written = []
    for path in powershell_profile_paths():
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            continue
        old = _read_text_smart(path) if os.path.exists(path) else ""
        if PS_PROFILE_MARK_BEGIN in old and PS_PROFILE_MARK_END in old:
            pre = old.split(PS_PROFILE_MARK_BEGIN, 1)[0]
            post = old.split(PS_PROFILE_MARK_END, 1)[1].lstrip("\r\n")
            text = pre + block + post
            action = "已刷新"
        else:
            if "function ai" in old.lower():
                warn(f"  ! {path} 已有自定义 ai 函数；新块追加在末尾会覆盖它")
            sep = "\r\n\r\n" if old.strip() else ""
            text = old.rstrip("\r\n") + sep + block + "\r\n"
            action = "已写入"
        try:
            # BOM 保证 Windows PowerShell 5.1 正确识别中文
            with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
                f.write(text)
            say(f"  + {action} PowerShell ai 函数 {path}")
            written.append(path)
        except OSError as e:
            warn(f"  ! 写入 {path} 失败：{e}")
    return written


# ---------- PATH ----------

def path_has(paths, target):
    t = os.path.normcase(os.path.abspath(os.path.expanduser(target)))
    for p in paths:
        if not p:
            continue
        expanded = os.path.normcase(os.path.abspath(os.path.expanduser(os.path.expandvars(p))))
        if expanded == t:
            return True
    return False


PATH_MARK_BEGIN = "# >>> shell-tool ai >>>"
PATH_MARK_END = "# <<< shell-tool ai <<<"


def add_path_unix(bin_dir):
    rc_files = [os.path.join(os.path.expanduser("~"), n)
                for n in (".bashrc", ".zshrc", ".profile")]
    rc_files = [p for p in rc_files if os.path.exists(p)]
    if not rc_files:
        rc_files = [os.path.join(os.path.expanduser("~"), ".profile")]

    block = (f"{PATH_MARK_BEGIN}\n"
             f'case ":$PATH:" in *":{bin_dir}:"*) ;; *) export PATH="{bin_dir}:$PATH" ;; esac\n'
             f"{PATH_MARK_END}\n")
    changed = False
    for rc in rc_files:
        try:
            with open(rc, encoding="utf-8", errors="replace") as f:
                text = f.read()
            if PATH_MARK_BEGIN in text or bin_dir in text:
                continue
            with open(rc, "a", encoding="utf-8") as f:
                if text and not text.endswith("\n"):
                    f.write("\n")
                f.write(block)
            say(f"  + 已在 {rc} 追加 PATH（新开终端生效）")
            changed = True
        except OSError as e:
            warn(f"  ! 写入 {rc} 失败：{e}")
    if not changed:
        say(f"  = shell 配置中已有 {bin_dir}")


def add_path_windows(bin_dir):
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                             winreg.KEY_READ | winreg.KEY_WRITE)
        try:
            raw, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            raw, kind = "", winreg.REG_EXPAND_SZ
        parts = [p for p in (raw or "").split(";") if p.strip()]
        if path_has(parts, bin_dir):
            say(f"  = 用户 PATH 中已有 {bin_dir}")
        else:
            parts.append(bin_dir)
            if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                kind = winreg.REG_EXPAND_SZ
            winreg.SetValueEx(key, "Path", 0, kind, ";".join(parts))
            say("  + 已写入用户 PATH（新开终端生效）")
            broadcast_env_change()
        winreg.CloseKey(key)
    except OSError as e:
        warn(f"  ! 写入用户 PATH 失败：{e}")
        warn(f"    请手动把 {bin_dir} 加入 PATH")


def broadcast_env_change():
    """通知资源管理器环境变量已变，新开的终端能立刻拿到新 PATH。"""
    try:
        import ctypes
        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(result))
    except Exception:
        pass


# ---------- 权限 ----------

def make_executable():
    target = os.path.join(REPO, "dp.py")
    if IS_WINDOWS:
        say("  = Windows 不需要执行权限（由启动器调用 python）")
        return
    mode = os.stat(target).st_mode
    if mode & stat.S_IXUSR:
        say(f"  = dp.py 已有执行权限")
        return
    os.chmod(target, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    say("  + 已给 dp.py 加执行权限")


# ---------- 主流程 ----------

def main():
    parser = argparse.ArgumentParser(description="部署 shell-tool：生成 ai 命令并把配置放到用户目录")
    parser.add_argument("--home", default=os.environ.get("SHELL_TOOL_HOME") or DEFAULT_HOME,
                        help=f"配置目录（.env/data/skill），默认 {DEFAULT_HOME}")
    parser.add_argument("--bin", default=DEFAULT_BIN,
                        help=f"启动器目录，默认 {DEFAULT_BIN}")
    parser.add_argument("--no-path", action="store_true", help="不修改 PATH")
    parser.add_argument("--venv", default=os.environ.get("SHELL_TOOL_VENV") or DEFAULT_VENV,
                        help=f"虚拟环境目录，默认 {DEFAULT_VENV}")
    parser.add_argument("--no-venv", action="store_true", help="不建虚拟环境，直接用系统 python3")
    parser.add_argument("--no-ps-profile", action="store_true", help="不写入 PowerShell ai 函数")
    parser.add_argument("--ps-profile", help="自定义 PowerShell profile 路径（; 分隔多个）")
    args = parser.parse_args()

    if args.ps_profile:
        os.environ["SHELL_TOOL_PS_PROFILE"] = args.ps_profile

    home = os.path.abspath(os.path.expanduser(args.home))
    bin_dir = os.path.abspath(os.path.expanduser(args.bin))
    venv_dir = os.path.abspath(os.path.expanduser(args.venv))

    say("shell-tool 部署")
    say("[1/6] 检查 Python 环境")
    python_path = ensure_python_env(venv_dir, use_venv=not args.no_venv)
    say(f"  = ai 将使用 {python_path}")
    say("[2/6] 创建配置目录与配置文件")
    setup_home(home)
    say("[3/6] 生成 ai 启动器")
    launcher = write_launcher(bin_dir, python_path)
    say("[4/6] 配置 PowerShell ai 函数")
    if not IS_WINDOWS:
        say("  - 非 Windows，跳过")
    elif args.no_ps_profile:
        say("  - 已用 --no-ps-profile 跳过")
    else:
        write_powershell_profile(REPO, home, python_path)
    say("[5/6] 配置 PATH")
    if args.no_path:
        say(f"  - 跳过；请自行确保 {bin_dir} 在 PATH 中")
    elif IS_WINDOWS:
        add_path_windows(bin_dir)
    else:
        add_path_unix(bin_dir)
    say("[6/6] 检查执行权限")
    make_executable()

    env_path = os.path.join(home, ".env")
    say("")
    say("完成：")
    say(f"  配置目录  {home}")
    say(f"    .env    {env_path}")
    say(f"    data/   {os.path.join(home, 'data')}")
    say(f"    skill/  {os.path.join(home, 'skill')}")
    say(f"  启动器    {launcher}")
    say(f"  Python    {python_path}")
    if IS_WINDOWS and not args.no_ps_profile:
        say(f"  PowerShell profile  {'; '.join(powershell_profile_paths())}")
    say("")
    say("下一步：")
    say(f"  1. 编辑 {env_path}，至少填 DEEPSEEK_API_KEY")
    say("  2. 新开一个终端，运行： ai \"你好\"（ai -h 查看全部选项）")
    say("  可用 SHELL_TOOL_HOME / SHELL_TOOL_REPO / SHELL_TOOL_PYTHON / SHELL_TOOL_VENV / "
        "SHELL_TOOL_PS_PROFILE 覆盖默认路径。")


if __name__ == "__main__":
    main()
