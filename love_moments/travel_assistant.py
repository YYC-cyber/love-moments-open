"""Controlled intent parsing for the shared trip conversation.

The model may classify a request and extract constraints.  It may not return
hotel, restaurant, price, rating, URL or database facts; those always come
from the application's existing providers and store.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from urllib import request

from .http_client import TRUSTED_SSL_CONTEXT


INTENT_KINDS = {
    "hotel_search", "restaurant_search", "plan_compare", "itinerary_change",
    "day_replan", "next_stop", "transport_compare", "trip_recap", "general",
}
MEAL_TYPES = {"breakfast", "lunch", "snack", "dinner"}
ALLOWED_MODEL_FIELDS = {
    "kind", "day_number", "meal_type", "max_price_cents", "min_rating",
    "max_distance_meters", "item_hint", "changes", "preference", "query", "delay_minutes",
}


def _scope(context: dict[str, Any]) -> str:
    place = context.get("confirmed_place") if isinstance(context.get("confirmed_place"), dict) else {}
    parts = [str(place.get(key) or "").strip() for key in ("country", "province", "city", "district", "name")]
    unique: list[str] = []
    for part in parts:
        if part and part not in unique:
            unique.append(part)
    return "·".join(unique) or str(context.get("destination") or "").strip()[:160]


def _number(text: str) -> int | None:
    match = re.search(r"第\s*(\d{1,2})\s*天", text)
    if match:
        return int(match.group(1))
    values = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    match = re.search(r"第\s*([一二两三四五六七八九十])\s*天", text)
    return values.get(match.group(1)) if match else None


@dataclass(frozen=True)
class TravelAssistantIntent:
    kind: str
    scope: str = ""
    day_number: int | None = None
    meal_type: str | None = None
    max_price_cents: int | None = None
    min_rating: float | None = None
    max_distance_meters: int | None = None
    item_hint: str = ""
    changes: dict[str, Any] = field(default_factory=dict)
    preference: str = ""
    query: str = ""
    delay_minutes: int | None = None

    @classmethod
    def from_dict(cls, raw: Any, context: dict[str, Any]) -> "TravelAssistantIntent":
        if not isinstance(raw, dict) or not set(raw).issubset(ALLOWED_MODEL_FIELDS):
            raise ValueError("旅行助手意图字段无效")
        kind = str(raw.get("kind") or "")
        if kind not in INTENT_KINDS:
            raise ValueError("旅行助手意图类型无效")
        day = raw.get("day_number")
        day = int(day) if day not in (None, "") else None
        if day is not None and not 1 <= day <= 61:
            raise ValueError("旅行日期无效")
        meal = str(raw.get("meal_type") or "") or None
        if meal is not None and meal not in MEAL_TYPES:
            raise ValueError("餐次无效")
        changes = raw.get("changes") or {}
        if not isinstance(changes, dict) or not set(changes).issubset({"start_time", "duration_minutes", "position", "note"}):
            raise ValueError("行程修改字段无效")
        if "start_time" in changes and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(changes["start_time"])):
            raise ValueError("行程时间无效")
        return cls(
            kind=kind,
            scope=_scope(context),
            day_number=day,
            meal_type=meal,
            max_price_cents=_bounded_int(raw.get("max_price_cents"), 0, 100_000_000),
            min_rating=_bounded_float(raw.get("min_rating"), 0, 5),
            max_distance_meters=_bounded_int(raw.get("max_distance_meters"), 100, 50_000),
            item_hint=str(raw.get("item_hint") or "")[:120],
            changes={key: value for key, value in changes.items()},
            preference=str(raw.get("preference") or "")[:160],
            query=str(raw.get("query") or "")[:160],
            delay_minutes=_bounded_int(raw.get("delay_minutes"), 0, 720),
        )


def _bounded_int(value: Any, low: int, high: int) -> int | None:
    if value in (None, ""):
        return None
    number = int(value)
    if not low <= number <= high:
        raise ValueError("数值超出范围")
    return number


def _bounded_float(value: Any, low: float, high: float) -> float | None:
    if value in (None, ""):
        return None
    number = float(value)
    if not low <= number <= high:
        raise ValueError("数值超出范围")
    return number


class TravelAssistantProvider(Protocol):
    name: str

    def interpret(self, message: str, context: dict[str, Any], history: list[dict[str, Any]]) -> TravelAssistantIntent: ...

    def resolve_hotel_destination(self, place: dict[str, Any], requested: str) -> str: ...

    def localize_hotel_names(self, names: list[str], place: dict[str, Any]) -> dict[str, str]: ...


@dataclass
class RuleBasedTravelAssistant:
    name: str = "rules"

    def resolve_hotel_destination(self, place: dict[str, Any], requested: str) -> str:
        del place, requested
        return ""

    def localize_hotel_names(self, names: list[str], place: dict[str, Any]) -> dict[str, str]:
        del names, place
        return {}

    def interpret(self, message: str, context: dict[str, Any], history: list[dict[str, Any]]) -> TravelAssistantIntent:
        text = str(message or "").strip()[:1000]
        lowered = text.lower()
        if re.search(r"回忆|回顾|游记|旅行总结|旅程总结", lowered):
            kind = "trip_recap"
        elif re.search(r"下一站|接下来.*(去|玩)|现在.*(去哪|去哪里)|当前位置.*推荐", lowered):
            kind = "next_stop"
        elif re.search(r"(统一|一起|综合).*(酒店|住宿).*(机票|航班).*(火车|高铁|车票)|交通.*住宿.*比较", lowered):
            kind = "transport_compare"
        elif re.search(r"下雨|雨天|下雪|雪天|延误|迟到|晚了|体力不支|走不动|换成室内|重新安排.*(后面|当天)", lowered):
            kind = "day_replan"
        elif re.search(r"酒店|住宿|民宿|hotel", lowered):
            kind = "hotel_search"
        elif re.search(r"餐厅|餐馆|饭店|早餐|早饭|午餐|午饭|晚餐|晚饭|小吃|美食|吃", lowered):
            kind = "restaurant_search"
        elif re.search(r"比较|哪套|哪个方案|五套|方案.*(好|适合|轻松|便宜)", lowered):
            kind = "plan_compare"
        elif re.search(r"改到|调整|提前|推迟|挪到|停留|时长", lowered):
            kind = "itinerary_change"
        else:
            kind = "general"
        meal = None
        if re.search(r"早餐|早饭|早点", text): meal = "breakfast"
        elif re.search(r"午餐|午饭|中饭", text): meal = "lunch"
        elif re.search(r"下午茶|甜品", text): meal = "snack"
        elif re.search(r"晚餐|晚饭|夜宵", text): meal = "dinner"
        price = re.search(r"(\d{2,6})\s*元?(?:以内|以下|左右|每晚)", text)
        rating = re.search(r"([0-5](?:\.\d)?)\s*分(?:以上|起)?", text)
        distance = re.search(r"(\d+(?:\.\d+)?)\s*(公里|千米|km|米)(?:内|以内|附近)?", lowered)
        start_time = re.search(r"(?:改到|挪到|调整到|推迟到|提前到)?\s*((?:[01]?\d|2[0-3])[:：][0-5]\d)", text)
        changes: dict[str, Any] = {}
        if start_time:
            hour, minute = start_time.group(1).replace("：", ":").split(":")
            changes["start_time"] = f"{int(hour):02d}:{minute}"
        duration = re.search(r"(?:停留|玩|时长).*?(\d+(?:\.\d+)?)\s*(小时|分钟)", text)
        if duration:
            changes["duration_minutes"] = int(float(duration.group(1)) * (60 if duration.group(2) == "小时" else 1))
        delay = re.search(r"(?:延误|迟到|晚了|推迟)\s*(\d+(?:\.\d+)?)\s*(小时|分钟)", text)
        delay_minutes = int(float(delay.group(1)) * (60 if delay and delay.group(2) == "小时" else 1)) if delay else None
        distance_meters = None
        if distance:
            distance_meters = int(float(distance.group(1)) * (1000 if distance.group(2) in {"公里", "千米", "km"} else 1))
        preference = ""
        for word in ("轻松", "松弛", "便宜", "性价比", "美食", "自然", "经典", "充实"):
            if word in text:
                preference = word
                break
        return TravelAssistantIntent.from_dict({
            "kind": kind,
            "day_number": _number(text),
            "meal_type": meal,
            "max_price_cents": int(price.group(1)) * 100 if price else None,
            "min_rating": float(rating.group(1)) if rating else None,
            "max_distance_meters": distance_meters,
            "changes": changes,
            "preference": preference,
            "query": text,
            "delay_minutes": delay_minutes,
        }, context)


class OpenAICompatibleTravelAssistant:
    name = "ai"

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 30, before_request: Callable[[str], None] | None = None):
        self.api_key, self.base_url, self.model, self.timeout = api_key, base_url.rstrip("/"), model, timeout
        self.before_request = before_request
        self.fallback = RuleBasedTravelAssistant()

    def resolve_hotel_destination(self, place: dict[str, Any], requested: str) -> str:
        """Resolve a confirmed place to the concise English query Google Hotels expects."""
        safe_place = {
            key: str(place.get(key) or "").strip()[:120]
            for key in ("country", "province", "city", "district", "name")
        }
        payload = {
            "model": self.model, "temperature": 0, "max_tokens": 120,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是酒店地点名称解析器。根据已确认的地图地点，把用户查询转换为 Google Hotels 可识别的简短英文地理名称。"
                        "优先使用国际上常用的景点或城市名称，并省略 Scenic Area、Tourist Area、District 等不影响定位的通用后缀。"
                        "只返回严格 JSON：{\"query\":\"Place, City\"}。不得添加酒店、价格、网址或未提供的城市国家；不要解释。"
                    ),
                },
                {"role": "user", "content": json.dumps({"confirmed_place": safe_place, "requested": str(requested)[:240]}, ensure_ascii=False)},
            ],
        }
        if "api.deepseek.com" in self.base_url.lower():
            payload["thinking"] = {"type": "disabled"}
        try:
            if self.before_request:
                self.before_request("hotel destination resolution")
            req = request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(req, timeout=self.timeout, context=TRUSTED_SSL_CONTEXT) as response:  # noqa: S310
                raw = json.loads(response.read().decode("utf-8"))
            query = str(json.loads(raw["choices"][0]["message"]["content"]).get("query") or "").strip()
            if not 2 <= len(query) <= 120 or not query.isascii() or not re.fullmatch(r"[A-Za-z0-9 .,'’&()/\-]+", query):
                return ""
            if re.search(r"https?://|www\.", query, re.IGNORECASE):
                return ""
            return query
        except Exception:
            return ""

    def localize_hotel_names(self, names: list[str], place: dict[str, Any]) -> dict[str, str]:
        safe_names = list(dict.fromkeys(str(name or "").strip()[:200] for name in names if str(name or "").strip()))[:20]
        if not safe_names:
            return {}
        safe_place = {
            key: str(place.get(key) or "").strip()[:120]
            for key in ("country", "province", "city", "district", "name")
        }
        payload = {
            "model": self.model, "temperature": 0, "max_tokens": 900,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是酒店名称本地化助手。把英文酒店正式名称转换成中国用户常用的简体中文名称，保留品牌、地点和分店信息，"
                        "不能编造酒店或改变酒店身份。只返回严格 JSON："
                        "{\"names\":[{\"original\":\"输入原名\",\"zh_cn\":\"中文名\"}]}。original 必须逐字来自输入；不要网址或解释。"
                    ),
                },
                {"role": "user", "content": json.dumps({"confirmed_place": safe_place, "hotel_names": safe_names}, ensure_ascii=False)},
            ],
        }
        if "api.deepseek.com" in self.base_url.lower():
            payload["thinking"] = {"type": "disabled"}
        try:
            if self.before_request:
                self.before_request("hotel name localization")
            req = request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(req, timeout=self.timeout, context=TRUSTED_SSL_CONTEXT) as response:  # noqa: S310
                raw = json.loads(response.read().decode("utf-8"))
            rows = json.loads(raw["choices"][0]["message"]["content"]).get("names")
            if not isinstance(rows, list):
                return {}
            allowed = set(safe_names)
            localized: dict[str, str] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                original = str(row.get("original") or "").strip()
                chinese = str(row.get("zh_cn") or "").strip()
                if original not in allowed or not 2 <= len(chinese) <= 120:
                    continue
                if not re.search(r"[\u3400-\u9fff]", chinese) or re.search(r"https?://|www\.", chinese, re.IGNORECASE):
                    continue
                localized[original] = chinese
            return localized
        except Exception:
            return {}

    def interpret(self, message: str, context: dict[str, Any], history: list[dict[str, Any]]) -> TravelAssistantIntent:
        safe_context = {"destination": context.get("destination"), "confirmed_place": context.get("confirmed_place"), "itinerary_items": context.get("itinerary_items", [])[:80]}
        payload = {
            "model": self.model, "temperature": 0.1, "max_tokens": 500,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你是旅行意图解析器。只返回JSON。kind只能是hotel_search、restaurant_search、plan_compare、itinerary_change、day_replan、next_stop、transport_compare、trip_recap、general。只提取约束，不得返回店名、价格结果、评分结果、URL或数据库ID。changes只能含start_time、duration_minutes、position、note；delay_minutes只能是0到720的整数。"},
                {"role": "user", "content": json.dumps({"context": safe_context, "recent": history[-6:], "message": str(message)[:1000]}, ensure_ascii=False)},
            ],
        }
        if "api.deepseek.com" in self.base_url.lower():
            payload["thinking"] = {"type": "disabled"}
        try:
            if self.before_request:
                self.before_request("travel assistant intent")
            req = request.Request(f"{self.base_url}/chat/completions", data=json.dumps(payload, ensure_ascii=False).encode(), headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, method="POST")
            with request.urlopen(req, timeout=self.timeout, context=TRUSTED_SSL_CONTEXT) as response:  # noqa: S310
                raw = json.loads(response.read().decode())
            return TravelAssistantIntent.from_dict(json.loads(raw["choices"][0]["message"]["content"]), context)
        except Exception:
            return self.fallback.interpret(message, context, history)


def travel_assistant_from_env(before_request: Callable[[str], None] | None = None) -> TravelAssistantProvider:
    key = (os.getenv("TRAVEL_ASSISTANT_LLM_API_KEY") or os.getenv("LLM_API_KEY") or "").strip()
    if not key:
        return RuleBasedTravelAssistant()
    try:
        timeout = int(os.getenv("TRAVEL_ASSISTANT_LLM_TIMEOUT_SECONDS") or os.getenv("LLM_TIMEOUT_SECONDS", "30"))
    except ValueError:
        timeout = 30
    return OpenAICompatibleTravelAssistant(
        key,
        os.getenv("TRAVEL_ASSISTANT_LLM_BASE_URL") or os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
        os.getenv("TRAVEL_ASSISTANT_LLM_MODEL") or os.getenv("LLM_MODEL", "deepseek-chat"),
        timeout,
        before_request,
    )
