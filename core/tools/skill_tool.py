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
import os
import re
import sys

from .envelope import ok, error
from .. import config

SKILL_DIR = os.path.join(config.PROJECT_ROOT, "skill")

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


def _scan():
    """扫描 skill/ 目录，返回 name -> meta；目录不存在或为空时返回空注册表。"""
    registry = {}
    if not os.path.isdir(SKILL_DIR):
        return registry
    for fn in sorted(os.listdir(SKILL_DIR)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        meta = _parse_skill_header(os.path.join(SKILL_DIR, fn))
        if meta:
            registry[meta["name"]] = meta
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
