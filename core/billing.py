#!/usr/bin/env python3
"""DeepSeek 计费：工作日峰谷单价、费用计算与账户余额实时查询"""
from datetime import datetime, timedelta, timezone

import requests

from . import config

# 北京时间（UTC+8），DeepSeek 官方峰谷时段以北京时间计
BEIJING_TZ = timezone(timedelta(hours=8))

# 工作日高峰时段（北京时间）：周一至周五 09:00-12:00、14:00-18:00
PEAK_RANGES = ((9, 0, 12, 0), (14, 0, 18, 0))

# OpenCode Go 峰谷时段（UTC）：周一至周五 01:00-04:00、06:00-10:00
OPENCODE_GO_PEAK_RANGES = ((1, 0, 4, 0), (6, 0, 10, 0))

# DeepSeek 官方单价：元 / 百万 tokens
PRICING = {
    "flash": {
        "peak": {"hit": 0.04, "miss": 2.0, "out": 8.0},
        "off": {"hit": 0.02, "miss": 1.0, "out": 4.0},
    },
    "pro": {
        "peak": {"hit": 0.30, "miss": 9.0, "out": 27.0},
        "off": {"hit": 0.15, "miss": 4.5, "out": 13.5},
    },
}

# OpenCode Go 单价：美元 / 百万 tokens（来源：https://opencode.ai/docs/go）
OPENCODE_GO_PRICING = {
    "flash": {
        "peak": {"hit": 0.006, "miss": 0.30, "out": 1.20},
        "off": {"hit": 0.003, "miss": 0.15, "out": 0.60},
    },
    "pro": {
        "peak": {"hit": 0.044, "miss": 1.32, "out": 3.96},
        "off": {"hit": 0.022, "miss": 0.66, "out": 1.98},
    },
}


def provider():
    """当前服务商标识（deepseek / opencode-go）"""
    return config.LLM_PROVIDER


def is_opencode_go():
    return provider() == 'opencode-go'


def _pricing_table():
    return OPENCODE_GO_PRICING if is_opencode_go() else PRICING


def currency():
    return "USD" if is_opencode_go() else "CNY"


def currency_symbol():
    return "$" if currency() == "USD" else "¥"


def period_label(peak):
    """峰谷时段文案（DeepSeek 按北京时间，OpenCode Go 按 UTC）"""
    span = ("UTC 周一至周五 01:00-04:00、06:00-10:00" if is_opencode_go()
            else "北京时间 周一至周五 9:00-12:00、14:00-18:00")
    return f"{'高峰时段' if peak else '空闲时段'}（{span}）"


def model_family(model=None):
    """按模型名归族：含 pro 记为 pro，其余（flash / v4-flash 等）按 flash 计费"""
    name = (model or config.DEEPSEEK_MODEL or "").lower()
    return "pro" if "pro" in name else "flash"


def is_peak(when=None):
    """按服务商时区判断是否为工作日高峰时段"""
    dt = when or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    if is_opencode_go():
        d = dt.astimezone(timezone.utc)
        ranges = OPENCODE_GO_PEAK_RANGES
    else:
        d = dt.astimezone(BEIJING_TZ)
        ranges = PEAK_RANGES
    if d.weekday() >= 5:
        return False
    minutes = d.hour * 60 + d.minute
    return any(sh * 60 + sm <= minutes < eh * 60 + em for sh, sm, eh, em in ranges)


def compute_cost(usage, model=None, when=None):
    """计算本次用量费用，返回 (总费用, 是否高峰, 分项费用 dict)"""
    price = _pricing_table()[model_family(model)]["peak" if is_peak(when) else "off"]
    hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
    miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
    out = getattr(usage, "completion_tokens", 0) or 0
    costs = {
        "hit": hit / 1_000_000 * price["hit"],
        "miss": miss / 1_000_000 * price["miss"],
        "out": out / 1_000_000 * price["out"],
    }
    return sum(costs.values()), is_peak(when), costs


def fetch_balance():
    """实时查询账户余额，返回 (金额字符串, 币种) 或 (None, None)。

    OpenCode Go 无公开余额接口，直接返回 (None, None)。
    """
    if is_opencode_go():
        return None, None
    try:
        r = requests.get(
            f"{config.DEEPSEEK_BASE_URL.rstrip('/')}/user/balance",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
            },
            timeout=10,
        )
        r.raise_for_status()
        info = (r.json().get("balance_infos") or [{}])[0]
        return info.get("total_balance"), info.get("currency")
    except Exception:
        return None, None
