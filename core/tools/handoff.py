#!/usr/bin/env python3
"""上下文压缩 / 交接文档（原生工具 compress_context）。

上下文接近上限时，模型把当前工作整理成一份交接文档交给这个工具：文档落库为一条
role=insert_user 的消息（内容包在 <history> 内）。下一次对话（无论用户有没有加 -n）
检测到未生效的交接文档，都会强制新开会话，并把这段 <history> 注入首轮上下文，
用「压缩后的历史」继续工作；没有交接文档的会话一切照旧。
"""
import os

from .envelope import ok, error
from .. import db

# 交接文档正文上限（字符）。超限截断并在末尾留标记，避免把超大文件整份塞进新会话。
MAX_HANDOFF_CHARS = 60000


def _resolve_path(path):
    """展开 ~ 与引号，转成绝对路径（相对路径按当前工作目录解析）。"""
    p = (path or '').strip().strip('"').strip("'")
    if not p:
        return ''
    return os.path.abspath(os.path.expanduser(p))


def _read_text(path):
    """读取文本文件：容忍编码差异，二进制/不可读文件由调用方转成错误信封。"""
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def compress_context(content='', path='', session_id=None):
    """保存交接文档：content（正文）与 path（本地文件）至少提供一个。

    - content：模型写好的交接文档正文（Markdown 即可）；
    - path：可选，读取该文本文件并接在正文之后（适合把已写好的文档落盘后再提交）；
    - session_id：由分发器注入的当前会话 id，交接文档记在该会话名下。
    """
    text = (content or '').strip()
    file_text = ''
    if (path or '').strip():
        resolved = _resolve_path(path)
        if not os.path.isfile(resolved):
            return error(f"文件不存在或不是普通文件：{resolved}", code="not_found")
        try:
            file_text = _read_text(resolved).strip()
        except OSError as e:
            return error(f"读取文件失败：{e}", code="read_failed")
    if file_text:
        text = f"{text}\n\n{file_text}" if text else file_text
    elif (path or '').strip() and not text:
        return error("文件内容为空，且未提供 content", code="empty")
    if not text:
        return error("content 与 path 至少提供一个（交接文档正文或本地文件路径）", code="empty")

    truncated = len(text) > MAX_HANDOFF_CHARS
    if truncated:
        text = text[:MAX_HANDOFF_CHARS].rstrip() + "\n…（超出上限，已截断）"
    if not session_id:
        return error("无法确定当前会话，交接文档未保存", code="no_session")

    try:
        uid, chars = db.save_handoff(session_id, text)
    except (OSError, ValueError) as e:
        return error(f"保存交接文档失败：{e}", code="save_failed")

    return ok({
        "saved": True,
        "uid": uid,
        "session": session_id,
        "chars": chars,
        "truncated": truncated,
        "source_file": os.path.basename(_resolve_path(path)) if file_text else "",
        "note": ("交接文档已保存。下一轮对话（无论是否加 -n）会自动开启新会话，"
                 "并把 <history> 注入首轮上下文，按文档继续工作。"),
    })
