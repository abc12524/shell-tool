#!/usr/bin/env python3
"""skill 注册表：扫描 skill/ 目录，按脚本头部的标准结构自动注册为可执行 skill。

脚本头部规范（文件最前面连续的 # 注释行）：
    # skill: <名称>
    # description: <一句话说明>
    # usage:
    #   <用法 / 参数 / 示例，可多行>
头部之后的普通脚本体需定义 `run(arguments: dict) -> str`，返回统一信封
（{"status":"ok","result":{...}} / {"status":"error",...}）。
未声明 "# skill:" 的脚本（如共享助手模块）不会被注册。

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
_CACHE_VERSION = 1

_SKILL_RE = re.compile(r'^#\s*skill\s*:\s*(\S+)\s*$', re.I)
_DESC_RE = re.compile(r'^#\s*description\s*:\s*(.*)$', re.I)
_USAGE_RE = re.compile(r'^#\s*usage\s*:\s*(.*)$', re.I)
_HASH_RE = re.compile(r'^#\s?')


def _parse_skill_header(path):
    """静态解析脚本头部（不导入执行），返回 meta 或 None。"""
    name = None
    description = ""
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
        "usage": entry_meta.get("usage", ""),
        "path": path,
        "module": module,
    }


def _meta_to_cache(meta):
    """取 meta 中可持久化的最小字段集；无 skill 头返回 None（一并缓存，避免重复读取）。"""
    if not meta:
        return None
    return {"name": meta["name"], "description": meta["description"], "usage": meta["usage"]}


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
    """name -> 说明"""
    return {name: meta["description"] for name, meta in REGISTRY.items()}


def get_usage(name):
    """返回某个 skill 的用法"""
    meta = REGISTRY.get(name)
    if not meta:
        return error(f"未知 skill '{name}'，可用: {', '.join(REGISTRY) or '(空)'}", code="unknown_skill")
    return ok({"skill": name, "description": meta["description"], "usage": meta["usage"]})


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
