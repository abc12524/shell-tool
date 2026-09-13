#!/usr/bin/env python3
"""DeepSeek 计费：工作日峰谷单价、费用计算与账户余额实时查询"""
from datetime import datetime, timedelta, timezone

import requests

from . import config

# 北京时间（UTC+8），官方峰谷时段均以北京时间计
BEIJING_TZ = timezone(timedelta(hours=8))

# 工作日高峰时段（北京时间）：周一至周五 09:00-12:00、14:00-18:00
PEAK_RANGES = ((9, 0, 12, 0), (14, 0, 18, 0))

# 单价：元 / 百万 tokens
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


def model_family(model=None):
    """按模型名归族：含 pro 记为 pro，其余（flash / v4-flash 等）按 flash 计费"""
    name = (model or config.DEEPSEEK_MODEL or "").lower()
    return "pro" if "pro" in name else "flash"


def is_peak(when=None):
    """按北京时间判断是否为工作日高峰时段"""
    dt = when or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    dt = dt.astimezone(BEIJING_TZ)
    if dt.weekday() >= 5:
        return False
    minutes = dt.hour * 60 + dt.minute
    return any(sh * 60 + sm <= minutes < eh * 60 + em for sh, sm, eh, em in PEAK_RANGES)


def compute_cost(usage, model=None, when=None):
    """计算本次用量费用，返回 (总费用元, 是否高峰, 分项费用 dict)"""
    price = PRICING[model_family(model)]["peak" if is_peak(when) else "off"]
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
    """实时查询账户余额，返回 (金额字符串, 币种) 或 (None, None)"""
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
