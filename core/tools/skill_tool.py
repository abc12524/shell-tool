#!/usr/bin/env python3
"""skill 注册表：扫描 skill/ 目录，按脚本头部的标准结构自动注册为可执行 skill。

脚本头部规范（文件最前面连续的 # 注释行）：
    # skill: <名称>
    # description: <一句话说明>
    # status: enable | disable   （可选，默认 enable；disable 不向 LLM 披露该工具）
    # usage:
    #   <用法 / 参数 / 示例，可多行>
头部之后的普通脚本体需定义 `run(arguments: dict) -> str`，返回统一信封
（{"status":"ok","result":{...}} / {"status":"error",...}）。
未声明 "# skill:" 的脚本（如共享助手模块）不会被注册。

status 只影响“是否对 LLM 披露”（skill 工具描述 + 系统提示词）；执行逻辑不变，
被 disable 的 skill 仍可按名称查看用法 / 执行。

启动时注册表为空，全部由 skill/ 目录下的脚本自动添加。
"""
import importlib
import json
import os
import re
import sys

from .envelope import ok, error
from .. import config

SKILL_DIR = os.path.join(config.PROJECT_ROOT, "skill")
# 注册表缓存：按 (mtime, size) 指纹复用，文件未变化时不再打开脚本解析头部
CACHE_PATH = os.path.join(config.PROJECT_ROOT, "data", "skill_registry.json")
_CACHE_VERSION = 2

_SKILL_RE = re.compile(r'^#\s*skill\s*:\s*(\S+)\s*$', re.I)
_DESC_RE = re.compile(r'^#\s*description\s*:\s*(.*)$', re.I)
_STATUS_RE = re.compile(r'^#\s*status\s*:\s*(\S+)\s*$', re.I)
_USAGE_RE = re.compile(r'^#\s*usage\s*:\s*(.*)$', re.I)
_HASH_RE = re.compile(r'^#\s?')

# 视为“不披露”的 status 取值；其余（含缺省）一律按 enable 处理
_DISABLED_VALUES = {"disable", "disabled", "off", "false", "0"}


def _is_enabled(meta):
    """status 是否为启用（非 disable）；缺省视为启用。"""
    value = str(meta.get("status") or "enable").strip().lower()
    return value not in _DISABLED_VALUES


def _parse_skill_header(path):
    """静态解析脚本头部（不导入执行），返回 meta 或 None。"""
    name = None
    description = ""
    status = "enable"
    usage_lines = []
    in_usage = False
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    for raw in lines[:300]:
        line = raw.strip()
        if not line:
            if name is not None and in_usage:
                usage_lines.append("")
            continue
        if not line.startswith("#"):
            break
        m = _SKILL_RE.match(line)
        if m:
            name = m.group(1)
            continue
        if name is None:
            continue
        m = _DESC_RE.match(line)
        if m and not in_usage:
            description = m.group(1).strip()
            continue
        m = _STATUS_RE.match(line)
        if m and not in_usage:
            status = m.group(1).strip().lower()
            continue
        m = _USAGE_RE.match(line)
        if m:
            in_usage = True
            rest = m.group(1).strip()
            if rest:
                usage_lines.append(rest)
            continue
        if in_usage:
            usage_lines.append(_HASH_RE.sub("", line))
    if not name:
        return None
    return {
        "name": name,
        "description": description,
        "status": status,
        "usage": "\n".join(usage_lines).strip(),
        "path": path,
        "module": "skill." + os.path.splitext(os.path.basename(path))[0],
    }


def _fingerprint(path):
    """返回文件的 (mtime, size) 指纹；读取失败返回 None。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {"mtime": st.st_mtime, "size": st.st_size}


def _load_cache():
    """读取缓存文件；版本不符或缺损时返回空 dict（触发全量扫描）。"""
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
        return {}
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def _save_cache(files):
    """原子写回缓存；失败静默（下次仍可全量扫描，不影响功能）。"""
    try:
        os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": _CACHE_VERSION, "files": files}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass


def _meta_from_cache(entry_meta, path, module):
    """由缓存条目重建 meta（补齐不入缓存的 path/module）。"""
    if not entry_meta:
        return None
    return {
        "name": entry_meta.get("name"),
        "description": entry_meta.get("description", ""),
        "status": entry_meta.get("status", "enable"),
        "usage": entry_meta.get("usage", ""),
        "path": path,
        "module": module,
    }


def _meta_to_cache(meta):
    """取 meta 中可持久化的最小字段集；无 skill 头返回 None（一并缓存，避免重复读取）。"""
    if not meta:
        return None
    return {"name": meta["name"], "description": meta["description"],
            "status": meta.get("status", "enable"), "usage": meta["usage"]}


def _scan():
    """扫描 skill/ 目录，返回 name -> meta。

    结果按文件 (mtime, size) 指纹缓存到 data/skill_registry.json：文件未变化时
    直接复用缓存、不再打开脚本解析头部；新增/修改/删除都会只重解析受影响文件并
    原子写回缓存。缓存缺失/损坏时自动退化为全量扫描。
    """
    registry = {}
    if not os.path.isdir(SKILL_DIR):
        return registry

    cached = _load_cache()
    files = {}
    changed = False

    for fn in sorted(os.listdir(SKILL_DIR)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        path = os.path.join(SKILL_DIR, fn)
        module = "skill." + os.path.splitext(fn)[0]
        fp = _fingerprint(path)
        if fp is None:
            continue

        entry = cached.get(fn)
        if entry and entry.get("mtime") == fp["mtime"] and entry.get("size") == fp["size"]:
            # 指纹命中：复用缓存，不打开文件
            cached_meta = entry.get("meta")
            meta = _meta_from_cache(cached_meta, path, module)
            files[fn] = {"mtime": fp["mtime"], "size": fp["size"], "meta": cached_meta}
        else:
            meta = _parse_skill_header(path)
            files[fn] = {"mtime": fp["mtime"], "size": fp["size"], "meta": _meta_to_cache(meta)}
            changed = True

        if meta:
            registry[meta["name"]] = meta

    if changed or files != cached:
        _save_cache(files)

    return registry


# 启动时为空注册表，全部由 skill/ 目录脚本自动添加
REGISTRY = _scan()


def list_skills():
    """全部已注册 skill：name -> 说明（含 status=disable 者）。"""
    return {name: meta["description"] for name, meta in REGISTRY.items()}


def disclosed_skills():
    """对 LLM 披露的 skill：name -> 说明（仅 status 非 disable）。

    用于原生 skill 工具描述与系统提示词；disable 的 skill 仍留在 REGISTRY 中
    可按名称查看用法 / 执行，只是不再出现在这里，即执行逻辑不受影响。
    """
    return {name: meta["description"] for name, meta in REGISTRY.items() if _is_enabled(meta)}


def skills_overview():
    """列出全部已注册 skill：name -> (status, description)，供 CLI 展示。"""
    return {name: (meta.get("status", "enable"), meta["description"])
            for name, meta in REGISTRY.items()}


def get_usage(name):
    """返回某个 skill 的用法"""
    meta = REGISTRY.get(name)
    if not meta:
        return error(f"未知 skill '{name}'，可用: {', '.join(REGISTRY) or '(空)'}", code="unknown_skill")
    return ok({"skill": name, "description": meta["description"], "usage": meta["usage"]})


def _rewrite_status_header(text, value):
    """把脚本头部里的 `# status:` 改写为 value（无则在 suitable 位置插入一行）。"""
    lines = text.split("\n")
    end = 0
    while end < len(lines):
        stripped = lines[end].strip()
        if stripped and not stripped.startswith("#"):
            break
        end += 1
    status_re = re.compile(r'^#\s*status\s*:', re.I)
    insert_at = None
    for i in range(end):
        if status_re.match(lines[i].strip()):
            lines[i] = f"# status: {value}"
            return "\n".join(lines)
        if insert_at is None and _DESC_RE.match(lines[i].strip()):
            insert_at = i + 1
    if insert_at is None:
        for i in range(end):
            if _SKILL_RE.match(lines[i].strip()):
                insert_at = i + 1
                break
    if insert_at is None:
        insert_at = end
    lines.insert(insert_at, f"# status: {value}")
    return "\n".join(lines)


def set_status(name, status):
    """改写 skill 脚本头部的 `# status:`（enable / disable），返回 (ok, message)。

    只改脚本头部一行，不触碰执行逻辑；下次进程扫描到新指纹时自动刷新注册表。
    """
    value = (status or "").strip().lower()
    if value not in ("enable", "disable"):
        return False, f"status 只能是 enable 或 disable（收到 '{status}'）"
    meta = REGISTRY.get(name)
    if not meta:
        return False, f"未知 skill '{name}'，可用: {', '.join(REGISTRY) or '(空)'}"
    path = meta["path"]
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return False, f"读取失败 - {e}"
    # 保留原文件 BOM 与换行风格（Git 检出到 Windows 时可能是 CRLF）
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    newline = "\r\n" if b"\r\n" in raw else ("\r" if b"\r" in raw else "\n")
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as e:
        return False, f"读取失败（非 UTF-8）- {e}"
    fixed = _rewrite_status_header(text, value)
    if newline != "\n":
        fixed = fixed.replace("\n", newline)
    try:
        with open(path, "wb") as f:
            f.write(fixed.encode("utf-8-sig" if has_bom else "utf-8"))
    except OSError as e:
        return False, f"写入失败 - {e}"
    return True, f"skill '{name}' 的 status 已设为 {value}"


def _load_run(meta):
    """按需导入 skill 脚本模块，返回其中的 run 函数。"""
    if config.PROJECT_ROOT not in sys.path:
        sys.path.insert(0, config.PROJECT_ROOT)
    module = importlib.import_module(meta["module"])
    run = getattr(module, "run", None)
    if not callable(run):
        raise AttributeError(f"skill 脚本缺少 run(arguments) 函数: {meta['path']}")
    return run


def execute(name, arguments):
    """执行指定 skill，返回规范信封字符串。"""
    meta = REGISTRY.get(name)
    if not meta:
        return error(f"未知 skill '{name}'，可用: {', '.join(REGISTRY) or '(空)'}", code="unknown_skill")
    try:
        result = _load_run(meta)(arguments or {})
    except Exception as e:
        return error(f"skill {name} 执行失败 - {e}", code="internal")
    if isinstance(result, str):
        return result
    return ok(result)


_HELP = (
    "skill 合集（由 skill/ 目录脚本自动注册）：\n"
    "  - 列出全部 skill：传 all=true\n"
    "  - 查看某个 skill 用法：传 skill='名称'（不传 arguments）\n"
    "  - 执行某个 skill：传 skill='名称' 并传 arguments={参数}\n"
    "已注册 skill：" + (", ".join(sorted(REGISTRY)) or "(空)")
)


def skill_tool(all: bool = False, skill: str = "", arguments: dict = None):
    """skill 合集入口：查询或执行。

    - all=true                      → 列出所有 skill 及说明
    - skill 指定且不传 arguments     → 返回该 skill 用法
    - skill 指定且传 arguments       → 执行该 skill
    - 均未传                         → 返回本工具用法提示
    """
    if all:
        return ok({"count": len(REGISTRY), "skills": list_skills()})
    if skill:
        if arguments is not None:
            return execute(skill, arguments)
        return get_usage(skill)
    return _HELP
