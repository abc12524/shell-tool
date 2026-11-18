#!/usr/bin/env python3
"""命令输出简化（trim）：实现放在技能包里，程序侧只做调用与降级。

**这不是技能调用**：不走 skill_tool.execute()（信封 / arguments 校验 / run 分发），
而是直接取已安装技能模块里导出的扩展函数：

    trim(text, **options) -> str     # 纯文本进、纯文本出；失败抛异常
    options: format / mode / timeout，实现可忽略

宿主拿到的就是压缩后的字符串，不用再解一层 JSON，也不依赖技能约定。
服务地址不在这里配 —— 归技能自己的 config.json（RTK_WEB_HOST）。
技能未安装、未导出 trim、抛异常、或压缩后不更短 → 静默用原文，简化不是单点故障；
抛异常时按 OUTPUT_TRIM_RETRY_SECONDS 熔断，避免服务挂了每条命令都等超时。
"""
import re
import time

from .. import config

# 单次压缩的输入上限；超过就跳过（大文件压缩没性价比，也免得打爆对端）
MAX_BYTES = 9 * 1024 * 1024
# 连续失败后的熔断截止时间（monotonic）
_DOWN_UNTIL = 0.0

# 压缩结果里出现这些标记，说明有条目没进输出：
#   ls 过滤器  -> "... (12 more)" / "... (3 filtered)"
#   cap 省略   -> "[… 33 lines elided …]"
#   budget 裁剪-> "[… 33 lines elided …]" / "[+120 chars]"
# 这不代表结果不能用 —— 宿主带了 max_chars，服务端就是按预算裁的，
# 报错行、首行、尾段都已保住。所以只标注 lossy，不再回退原文：
# 回退等于把「按预算选过的 6000 字符」换成「从头截断的 6000 字符」，更差。
_LOSS_HINT = re.compile(
    r'lines elided|\[\+\d+ chars\]|\d+ chars truncated|^\s*\.\.\. \(\d+ (?:more|filtered)',
    re.MULTILINE,
)


def _impl():
    """取已安装技能导出的 trim 扩展函数；未安装 / 未导出返回 None。"""
    from .skill_tool import get_module

    module = get_module(config.OUTPUT_TRIM_SKILL)
    fn = getattr(module, "trim", None) if module is not None else None
    return fn if callable(fn) else None


def trim_text(text):
    """压缩一段命令输出；成功返回 (trimmed, stats)，跳过/失败返回 None。

    stats = {saved_percent, bytes_before, bytes_after, via}
    """
    global _DOWN_UNTIL
    if not config.OUTPUT_TRIM_ENABLED or not text:
        return None
    if time.monotonic() < _DOWN_UNTIL:
        return None
    if len(text.encode('utf-8')) > MAX_BYTES:
        return None

    impl = _impl()
    if impl is None:
        # 没装技能 = 不裁剪；这是配置缺失而非故障，不触发熔断
        return None

    options = {
        'format': config.OUTPUT_TRIM_FORMAT,
        'mode': config.OUTPUT_TRIM_MODE,
        'timeout': config.OUTPUT_TRIM_TIMEOUT,
        # 让服务端知道宿主的截断口径：它在预算内按重要性裁，
        # 比我们拿到结果后砍尾巴更省也更准。
        'max_chars': config.OUTPUT_TRIM_MAX_CHARS,
    }

    try:
        trimmed = impl(text, **options)
    except Exception:
        if config.OUTPUT_TRIM_RETRY_SECONDS > 0:
            _DOWN_UNTIL = time.monotonic() + config.OUTPUT_TRIM_RETRY_SECONDS
        return None

    _DOWN_UNTIL = 0.0
    if not isinstance(trimmed, str) or not trimmed or len(trimmed) >= len(text):
        return None
    before = len(text.encode('utf-8'))
    after = len(trimmed.encode('utf-8'))
    return trimmed, {
        'saved_percent': round((before - after) / before * 100, 1),
        'bytes_before': before,
        'bytes_after': after,
        # 结果被裁过（省略了中间/单行截断），调用方可据此提示模型"要看全就精确化命令"
        'lossy': bool(_LOSS_HINT.search(trimmed)),
        'via': 'skill:' + config.OUTPUT_TRIM_SKILL,
    }
