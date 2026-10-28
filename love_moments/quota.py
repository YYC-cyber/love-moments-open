"""Persistent, conservative guards for metered external services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class QuotaPolicy:
    provider: str
    label: str
    period: str
    limit: int | None


class QuotaExceededError(RuntimeError):
    """Raised before a remote request when the configured hard cap is full."""

    def __init__(self, *, provider: str, label: str, used: int, limit: int, period_label: str, resets_at: str | None):
        self.provider = provider
        self.label = label
        self.used = used
        self.limit = limit
        self.period_label = period_label
        self.resets_at = resets_at
        reset_text = f"，将在 {resets_at} 自动恢复" if resets_at else ""
        super().__init__(f"{label}{period_label}的本地安全额度已用完（{used}/{limit}）{reset_text}；已停止真实请求，不会继续消耗次数")


def _integer(name: str, default: int, *aliases: str) -> int:
    for key in (name, *aliases):
        raw = os.getenv(key)
        if raw is None or not raw.strip():
            continue
        try:
            return max(0, int(raw))
        except ValueError:
            return default
    return default


def _optional_limit(name: str, default: int, *aliases: str) -> int | None:
    """Return None for an explicitly unlimited local counter.

    Provider-side balances and rate limits still apply; this only disables the
    additional Love Moments hard stop while retaining usage accounting.
    """
    for key in (name, *aliases):
        raw = os.getenv(key)
        if raw is None or not raw.strip():
            continue
        value = raw.strip().lower()
        if value in {"-1", "none", "unlimited", "infinite"}:
            return None
        try:
            return max(0, int(value))
        except ValueError:
            return default
    return default


def quota_policies() -> dict[str, QuotaPolicy]:
    """Read hard limits on every request so safe .env changes need no migration.

    A value of 0 blocks that real provider. The defaults intentionally leave
    headroom below typical introductory allowances; the provider console is
    still the source of truth and users can lower these values further.
    """

    return {
        "amap": QuotaPolicy("amap", "高德地图", "day", _integer("AMAP_DAILY_REQUEST_LIMIT", 500)),
        "geoapify": QuotaPolicy("geoapify", "Geoapify 全球地点与路线", "day", _integer("GEOAPIFY_DAILY_REQUEST_LIMIT", 1000)),
        "serpapi": QuotaPolicy(
            "serpapi", "SerpApi 旅行与口碑检索", "month",
            _integer("SERPAPI_MONTHLY_REQUEST_LIMIT", 230, "SERPAPI_MONTHLY_LIMIT"),
        ),
        "juhe_train": QuotaPolicy("juhe_train", "聚合数据火车票", "lifetime", _integer("JUHE_TRAIN_TOTAL_REQUEST_LIMIT", 90)),
        "aliyun_ocr": QuotaPolicy("aliyun_ocr", "阿里云 OCR", "month", _integer("ALIYUN_OCR_MONTHLY_REQUEST_LIMIT", 100)),
        "ocr_http": QuotaPolicy("ocr_http", "备用 OCR", "month", _integer("OCR_HTTP_MONTHLY_REQUEST_LIMIT", 100)),
        "llm": QuotaPolicy("llm", "大模型", "day", _optional_limit("LLM_DAILY_REQUEST_LIMIT", 30)),
        "recipe_llm": QuotaPolicy("recipe_llm", "AI 菜谱", "day", _optional_limit("RECIPE_LLM_DAILY_REQUEST_LIMIT", 8)),
        "tianapi_recipe": QuotaPolicy("tianapi_recipe", "天聚数行中文菜谱", "day", _integer("TIANAPI_RECIPE_DAILY_REQUEST_LIMIT", 80)),
        "themealdb": QuotaPolicy("themealdb", "TheMealDB 菜谱", "day", _integer("THEMEALDB_DAILY_REQUEST_LIMIT", 100)),
        "spoonacular": QuotaPolicy("spoonacular", "Spoonacular 菜谱", "day", _integer("SPOONACULAR_DAILY_REQUEST_LIMIT", 40)),
        "booking": QuotaPolicy("booking", "授权报价接口", "day", _integer("BOOKING_DAILY_REQUEST_LIMIT", 50)),
    }


def period_values(period: str, now: datetime | None = None) -> tuple[str, str, str | None]:
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if period == "day":
        next_day = (current + timedelta(days=1)).date()
        reset = datetime(next_day.year, next_day.month, next_day.day, tzinfo=SHANGHAI)
        return current.strftime("%Y-%m-%d"), "今日", reset.strftime("%m月%d日 %H:%M")
    if period == "month":
        year, month = current.year, current.month + 1
        if month == 13:
            year, month = year + 1, 1
        reset = datetime(year, month, 1, tzinfo=SHANGHAI)
        return current.strftime("%Y-%m"), "本月", reset.strftime("%m月%d日 %H:%M")
    return "all", "累计", None
