#!/usr/bin/env python3
"""skill_hub：从远程技能仓库（GitHub）查看 / 下载 / 卸载 skill。

远程仓库结构（默认 abc12524/shell-skills@main，可用 SKILL_HUB_REPO / SKILL_HUB_BRANCH 覆盖）：
    index.json                    根索引：{"platforms":[...],"skills":[{name,description,platform,path,entry,requires,config}]}
    <skill>/skill.py              入口脚本（头部含 # skill: <name>）
    <skill>/config.example.json   配置模板（缺失则装后手动创建 config.json）

安装：下载整仓 tarball（1 次请求），仅解压 `<path>/` 子树到本地 `skill/<name>/`；
已有本地 config.json / *.conf 会在重装时保留，其余情况复制 config.example.json 为待填 config；
随后热刷新注册表。

安全：下载即等价于信任该仓库的代码（会在本进程内 import 执行）。仅从可信来源安装。
"""
import io
import json
import os
import re
import shutil
import tarfile

import requests

from .envelope import ok, error
from .skill_tool import SKILL_DIR, reload_registry
from .. import config

DEFAULT_REPO = "abc12524/shell-skills"
DEFAULT_BRANCH = "main"
TIMEOUT = 30

_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _repo():
    return (getattr(config, "SKILL_HUB_REPO", "") or DEFAULT_REPO).strip().strip("/") or DEFAULT_REPO


def _branch():
    return (getattr(config, "SKILL_HUB_BRANCH", "") or DEFAULT_BRANCH).strip() or DEFAULT_BRANCH


def _index_url():
    return f"https://raw.githubusercontent.com/{_repo()}/{_branch()}/index.json"


def _tarball_url():
    return f"https://codeload.github.com/{_repo()}/tar.gz/refs/heads/{_branch()}"


def fetch_index():
    """拉取根索引，返回 skills 列表（每项 dict）。失败抛 RuntimeError。"""
    try:
        resp = requests.get(_index_url(), timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"拉取索引失败 - {e}")
    try:
        data = resp.json()
    except ValueError as e:
        raise RuntimeError(f"索引不是合法 JSON - {e}")
    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, list):
        raise RuntimeError("索引缺少 skills 列表")
    return skills


def _local_installed(name):
    """本地是否已安装（文件夹或扁平文件）。"""
    return (os.path.isfile(os.path.join(SKILL_DIR, name + ".py"))
            or os.path.isdir(os.path.join(SKILL_DIR, name)))


def remote_skills():
    """远程技能列表，每项补充 installed 标记。"""
    out = []
    for s in fetch_index():
        name = str(s.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "description": s.get("description", ""),
            "version": s.get("version", ""),
            "platform": s.get("platform", []),
            "requires": s.get("requires", []),
            "config": s.get("config", []),
            "installed": _local_installed(name),
        })
    return out


def _safe_relpath(rel):
    """校验相对路径安全：非绝对、无 .. 分量。"""
    rel = rel.replace("\\", "/").strip("/")
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return None
    return rel


def _find_skill(index, name):
    for s in index:
        if str(s.get("name") or "").strip() == name:
            return s
    return None


def install(name):
    """下载并安装指定 skill，返回结果 dict；失败抛 RuntimeError。"""
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise RuntimeError(f"非法 skill 名 '{name}'")

    entry = _find_skill(fetch_index(), name)
    if not entry:
        raise RuntimeError(f"远程仓库没有 skill '{name}'")

    path = _safe_relpath(str(entry.get("path") or name))
    if not path:
        raise RuntimeError(f"skill '{name}' 的 path 非法")

    try:
        resp = requests.get(_tarball_url(), timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"下载技能包失败 - {e}")

    try:
        tar = tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz")
    except tarfile.TarError as e:
        raise RuntimeError(f"技能包不是合法压缩包 - {e}")

    members = tar.getmembers()
    if not members:
        raise RuntimeError("技能包为空")
    root = members[0].name.split("/", 1)[0] + "/"
    prefix = root + path + "/"

    dest_dir = os.path.join(SKILL_DIR, name)
    tmp_dir = dest_dir + ".tmp"
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)

    written, total = [], 0
    try:
        for m in members:
            if not m.isfile() or not m.name.startswith(prefix):
                continue
            rel = _safe_relpath(m.name[len(prefix):])
            if not rel:
                continue
            src = tar.extractfile(m)
            if src is None:
                continue
            data = src.read()
            target = os.path.join(tmp_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(data)
            written.append(rel)
            total += len(data)
    finally:
        tar.close()

    if not written:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"技能包中未找到 '{path}/' 目录内容")

    # 保留本地配置文件（config.json / *.conf），重装不丢配置
    preserved = {}
    if os.path.isdir(dest_dir):
        for base, _dirs, files in os.walk(dest_dir):
            for fn in files:
                if fn == "config.json" or fn.endswith(".conf"):
                    full = os.path.join(base, fn)
                    rel = os.path.relpath(full, dest_dir).replace("\\", "/")
                    if rel not in written:
                        try:
                            with open(full, "rb") as f:
                                preserved[rel] = f.read()
                        except OSError:
                            pass

    # 原子替换目标目录
    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir, ignore_errors=True)
    os.replace(tmp_dir, dest_dir)

    # 还原保留的本地配置，否则用配置模板生成待填的 config.json
    for rel, data in preserved.items():
        target = os.path.join(dest_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)

    config_created = False
    example = os.path.join(dest_dir, "config.example.json")
    real = os.path.join(dest_dir, "config.json")
    if os.path.isfile(example) and not os.path.isfile(real):
        shutil.copyfile(example, real)
        config_created = True

    reload_registry()
    return {
        "name": name,
        "path": dest_dir,
        "files": written,
        "bytes": total,
        "config_created": config_created,
        "config_preserved": sorted(preserved),
        "requires": entry.get("requires", []),
    }


def remove(name):
    """卸载文件夹式 skill（不删除扁平 .py），返回结果 dict。"""
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise RuntimeError(f"非法 skill 名 '{name}'")
    dest_dir = os.path.join(SKILL_DIR, name)
    if not os.path.isdir(dest_dir):
        raise RuntimeError(f"未安装文件夹式 skill '{name}'")
    shutil.rmtree(dest_dir)
    reload_registry()
    return {"name": name, "removed": True}


def skill_hub(action: str = "list", name: str = ""):
    """skill_hub 入口：查看远程技能 / 安装 / 卸载，返回规范信封字符串。

    - action="list"           → 列出远程仓库全部技能（标注是否已安装）
    - action="install", name  → 下载并安装指定技能到本地 skill/
    - action="remove",  name  → 卸载本地文件夹式技能
    """
    act = (action or "list").strip().lower()
    try:
        if act == "list":
            skills = remote_skills()
            return ok({"repo": _repo(), "branch": _branch(),
                       "count": len(skills), "skills": skills})
        if act == "install":
            if not name.strip():
                return error("install 需要提供 name", code="missing_argument")
            return ok(install(name))
        if act == "remove":
            if not name.strip():
                return error("remove 需要提供 name", code="missing_argument")
            return ok(remove(name))
        return error(f"未知 action '{action}'（应为 list/install/remove）", code="unknown_action")
    except RuntimeError as e:
        return error(str(e), code="hub_failed")
