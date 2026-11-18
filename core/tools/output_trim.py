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

# rtk 的过滤器丢了内容时只留一行计数，条目本身不进输出：
#   ls 超过 50 项        -> "... (12 more)"
#   ls 命中噪音目录/解析失败 -> "... (3 filtered)"
#   "... (2 more, 1 filtered)"
# 这类结果是少了东西的，压缩收益再大也不能用 —— 宁可退回原文。
# 判定只认行首的独立提示行，避免误伤正文里恰好出现的括号。
_LOSS_HINT = re.compile(r'^\s*\.\.\. \(\d+ (?:more|filtered)', re.MULTILINE)


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
    if _LOSS_HINT.search(trimmed):
        # 压缩结果带折叠计数 = 有条目被吞掉（且 rtk-web 里 tee 恢复不可用），
        # 这条信息比省下的 token 值钱，直接用原文
        return None
    before = len(text.encode('utf-8'))
    after = len(trimmed.encode('utf-8'))
    return trimmed, {
        'saved_percent': round((before - after) / before * 100, 1),
        'bytes_before': before,
        'bytes_after': after,
        'via': 'skill:' + config.OUTPUT_TRIM_SKILL,
    }
