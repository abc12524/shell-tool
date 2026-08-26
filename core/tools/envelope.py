#!/usr/bin/env python3
"""统一的工具结果信封：对齐 OpenViking / DSH 规范输出。

所有工具成功返回  {"status": "ok",  "result": {...}}
所有工具失败返回  {"status": "error", "error": "...", "code": "..."}  (code 可选)

这样工具结果在服务端（OpenViking 后端）、DSH 会话（session.jsonl）与本地
shell-tool 三处保持同一信封结构，模型与上层编排无需针对不同来源做分支。
"""
import json


def ok(result=None):
    """构造成功信封。result 应为 dict（可空）。"""
    if result is None:
        result = {}
    return json.dumps({"status": "ok", "result": result}, ensure_ascii=False)


def error(message, code=None):
    """构造失败信封。code 为可选的稳定错误类别（如 timeout/http/internal）。"""
    payload = {"status": "error", "error": str(message)}
    if code is not None:
        payload["code"] = code
    return json.dumps(payload, ensure_ascii=False)


def is_error(value):
    """判断一个已解析的响应 dict 是否为失败信封（供底层透传判定）。"""
    return isinstance(value, dict) and value.get("status") == "error"


def passthrough(result):
    """透传 OpenViking 后端返回的规范信封（其本身已是 {status,result}）。

    若后端返回非预期格式，则降级为错误信封。
    """
    if isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False)
    return error("OpenViking 返回了非预期的数据格式", code="bad_response")
