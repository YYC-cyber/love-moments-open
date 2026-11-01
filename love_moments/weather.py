"""Small Open-Meteo adapter with a no-network fallback for the self-hosted app."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib import error, parse, request

from .http_client import TRUSTED_SSL_CONTEXT


WEATHER_LABELS = {
    0: "晴",
    1: "大致晴朗",
    2: "局部多云",
    3: "阴",
    45: "有雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "较强毛毛雨",
    56: "冻毛毛雨",
    57: "较强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "较强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "阵雨",
    81: "较强阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷雨",
    96: "雷雨伴小冰雹",
    99: "雷雨伴强冰雹",
}


class OpenMeteoWeather:
    def __init__(self, enabled: bool | None = None, timeout: int = 4):
        if enabled is None:
            enabled = os.getenv("OPEN_METEO_ENABLED", "true").strip().lower() == "true"
        self.enabled = enabled
        self.timeout = max(1, min(timeout, 10))
        self._cache: dict[str, tuple[float, float, dict]] = {}
        self._lock = threading.Lock()

    def current(self, latitude: object, longitude: object, timezone_name: str = "auto") -> dict | None:
        if not self.enabled or latitude is None or longitude is None:
            return None
        key = f"{float(latitude):.4f},{float(longitude):.4f},{timezone_name}"
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                value = dict(cached[2])
                value.update({
                    "source_type": "cache",
                    "original_source_type": str(value.get("source_type") or "real_api"),
                    "cache_age_seconds": max(0, int(time.monotonic() - cached[1])),
                })
                return value
        query = parse.urlencode({
            "latitude": float(latitude),
            "longitude": float(longitude),
            "current": "temperature_2m,apparent_temperature,weather_code,is_day",
            "timezone": timezone_name or "auto",
        })
        try:
            req = request.Request(
                f"https://api.open-meteo.com/v1/forecast?{query}",
                headers={"User-Agent": "LoveMoments/0.3"},
            )
            with request.urlopen(req, timeout=self.timeout, context=TRUSTED_SSL_CONTEXT) as response:
                current = json.loads(response.read().decode("utf-8"))["current"]
            code = int(current.get("weather_code", -1))
            value = {
                "temperature": round(float(current["temperature_2m"]), 1),
                "apparent_temperature": round(float(current.get("apparent_temperature", current["temperature_2m"])), 1),
                "weather_code": code,
                "label": WEATHER_LABELS.get(code, "天气变化中"),
                "is_day": bool(current.get("is_day", 1)),
                "source": "Open-Meteo",
                "provider": "Open-Meteo",
                "source_type": "real_api",
                "evidence_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "updated_at": str(current.get("time", "")),
            }
        except (error.URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        with self._lock:
            cached_at = time.monotonic()
            self._cache[key] = (cached_at + 1800, cached_at, value)
        return dict(value)

    def forecast(
        self,
        latitude: object,
        longitude: object,
        start_date: str,
        end_date: str,
        timezone_name: str = "auto",
        force_refresh: bool = False,
    ) -> dict | None:
        if not self.enabled or latitude is None or longitude is None:
            return None
        try:
            start, end = date.fromisoformat(str(start_date)), date.fromisoformat(str(end_date))
        except ValueError as exc:
            raise ValueError("天气预报日期无效") from exc
        if end < start or (end - start).days > 15:
            raise ValueError("天气预报范围需为连续 1–16 天")
        key = f"forecast:{float(latitude):.3f},{float(longitude):.3f},{start},{end},{timezone_name}"
        with self._lock:
            cached = self._cache.get(key)
            if not force_refresh and cached and cached[0] > time.monotonic():
                value = dict(cached[2])
                value.update({
                    "source_type": "cache",
                    "original_source_type": str(value.get("source_type") or "real_api"),
                    "cache_age_seconds": max(0, int(time.monotonic() - cached[1])),
                })
                return value
        query = parse.urlencode({
            "latitude": float(latitude), "longitude": float(longitude),
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "daily": "weather_code,precipitation_probability_max,temperature_2m_max,temperature_2m_min,wind_speed_10m_max,uv_index_max,sunrise,sunset",
            "timezone": timezone_name or "auto",
        })
        try:
            req = request.Request(
                f"https://api.open-meteo.com/v1/forecast?{query}",
                headers={"User-Agent": "LoveMoments/0.3"},
            )
            with request.urlopen(req, timeout=self.timeout, context=TRUSTED_SSL_CONTEXT) as response:
                payload = json.loads(response.read().decode("utf-8"))
            daily = payload["daily"]
            dates = list(daily["time"])
            codes = list(daily["weather_code"])
            probabilities = list(daily.get("precipitation_probability_max") or [None] * len(dates))
            highs = list(daily.get("temperature_2m_max") or [None] * len(dates))
            lows = list(daily.get("temperature_2m_min") or [None] * len(dates))
            winds = list(daily.get("wind_speed_10m_max") or [None] * len(dates))
            uv_values = list(daily.get("uv_index_max") or [None] * len(dates))
            sunrises = list(daily.get("sunrise") or [None] * len(dates))
            sunsets = list(daily.get("sunset") or [None] * len(dates))
            items = []
            for index, value_date in enumerate(dates[:16]):
                code = int(codes[index])
                items.append({
                    "date": str(value_date), "weather_code": code,
                    "label": WEATHER_LABELS.get(code, "天气变化中"),
                    "precipitation_probability": int(probabilities[index]) if probabilities[index] is not None else None,
                    "temperature_max": round(float(highs[index]), 1) if highs[index] is not None else None,
                    "temperature_min": round(float(lows[index]), 1) if lows[index] is not None else None,
                    "wind_speed_max": round(float(winds[index]), 1) if winds[index] is not None else None,
                    "uv_index_max": round(float(uv_values[index]), 1) if uv_values[index] is not None else None,
                    "sunrise": str(sunrises[index]) if sunrises[index] else None,
                    "sunset": str(sunsets[index]) if sunsets[index] else None,
                })
            value = {
                "items": items,
                "source": "Open-Meteo",
                "provider": "Open-Meteo",
                "source_type": "real_api",
                "evidence_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "timezone": str(payload.get("timezone") or timezone_name),
            }
        except (error.URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            return None
        with self._lock:
            cached_at = time.monotonic()
            self._cache[key] = (cached_at + 1800, cached_at, value)
        return dict(value)

    @staticmethod
    def _daily_advice(items: list[dict]) -> list[str]:
        advice: list[str] = []
        if any((item.get("precipitation_probability") or 0) >= 50 or int(item.get("weather_code", -1)) in {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99} for item in items):
            advice.append("带伞和防滑鞋；把至少一个室外项目准备成可替换的室内方案。")
        if any((item.get("temperature_max") or -99) >= 30 for item in items):
            advice.append("午后注意防晒和补水，长时间户外活动尽量避开最热时段。")
        if any((item.get("temperature_min") or 99) <= 8 for item in items):
            advice.append("早晚温差可能较大，建议携带方便增减的外套。")
        if any((item.get("wind_speed_max") or 0) >= 30 for item in items):
            advice.append("可能有较强阵风，高空、游船和缆车项目出发前需再次确认运行状态。")
        if any((item.get("uv_index_max") or 0) >= 6 for item in items):
            advice.append("紫外线较强，建议准备防晒霜、帽子或遮阳伞。")
        return advice or ["当前预报没有明显高风险天气，临出发前仍建议再刷新一次。"]

    @staticmethod
    def _attach_itinerary_recommendations(trip: dict, weather_items: list[dict]) -> None:
        variants = list(trip.get("variants") or [])
        active_style = str(trip.get("active_variant") or "")
        variant = next((value for value in variants if value.get("style") == active_style), variants[0] if variants else {})
        days = {str(day.get("date") or "")[:10]: day for day in variant.get("days", [])}
        indoor_words = ("博物馆", "美术馆", "展览", "剧院", "影院", "商场", "室内", "科技馆", "水族馆", "书店", "购物")
        adverse_codes = {45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99}
        for weather in weather_items:
            adverse = int(weather.get("weather_code", -1)) in adverse_codes or int(weather.get("precipitation_probability") or 0) >= 50
            choices = []
            for item in (days.get(str(weather.get("date") or "")) or {}).get("items", []):
                place = item.get("place") if isinstance(item.get("place"), dict) else {}
                if not place or not (place.get("provider_place_id") or place.get("formatted_address")):
                    continue
                details = item.get("details") if isinstance(item.get("details"), dict) else {}
                text = " ".join((str(item.get("title") or ""), str(item.get("type") or ""), str(place.get("category") or ""), str(details.get("environment") or ""))).lower()
                indoor = any(word.lower() in text for word in indoor_words)
                weather_match = indoor if adverse else not indoor
                choices.append({
                    "item_id": str(item.get("id") or ""), "title": str(item.get("title") or place.get("name") or "已确认地点"),
                    "indoor": indoor, "weather_match": weather_match, "source_type": "confirmed_itinerary",
                    "reason": ("雨雪天气优先室内活动" if adverse and indoor else "天气较适合户外活动" if not adverse and not indoor else "已在当前行程中，可作为备选"),
                })
            choices.sort(key=lambda value: (not value["weather_match"], value["title"]))
            weather["suggested_places"] = choices[:3]

    def trip_forecast(
        self, trip: dict, *, force_refresh: bool = False, today: date | None = None,
        range_days: int | None = None,
    ) -> dict:
        """Return truthful weather coverage for a trip without inventing long-range forecasts."""
        place = trip.get("destination_place") if isinstance(trip.get("destination_place"), dict) else {}
        location = {
            "name": str(place.get("name") or trip.get("destination") or "旅行目的地"),
            "address": str(place.get("formatted_address") or trip.get("arrival_city") or ""),
            "latitude": place.get("latitude"), "longitude": place.get("longitude"),
        }
        base = {
            "trip_id": str(trip.get("id") or ""), "location": location, "items": [], "advice": [],
            "source": "Open-Meteo", "provider": "Open-Meteo", "source_type": "unavailable",
            "evidence_at": None, "forecast_window_days": 16,
            "range_mode": "rolling" if range_days else "trip", "range_days": range_days,
        }
        if not self.enabled:
            return {**base, "status": "disabled", "message": "天气服务当前未启用。"}
        if place.get("latitude") is None or place.get("longitude") is None:
            return {**base, "status": "location_required", "message": "请先在旅行设置中确认唯一目的地，才能查询对应地区天气。"}
        current = today or date.today()
        if range_days is not None:
            if range_days not in {7, 10}:
                raise ValueError("天气范围只支持未来 7 天或 10 天")
            start, end = current, current + timedelta(days=range_days - 1)
        else:
            try:
                start = date.fromisoformat(str(trip.get("start_date") or "")[:10])
                end = date.fromisoformat(str(trip.get("end_date") or "")[:10])
            except ValueError:
                return {**base, "status": "dates_required", "message": "请先填写有效的旅行开始和结束日期。"}
        if end < start:
            return {**base, "status": "dates_required", "message": "旅行结束日期不能早于开始日期。"}
        horizon = current + timedelta(days=15)
        if end < current:
            return {**base, "status": "past_trip", "message": "这段旅行已经结束，天气卡不再请求实时预报。", "trip_start": start.isoformat(), "trip_end": end.isoformat()}
        if start > horizon:
            check_again = max(current, start - timedelta(days=15))
            return {
                **base, "status": "not_yet_available", "trip_start": start.isoformat(), "trip_end": end.isoformat(),
                "check_again_on": check_again.isoformat(),
                "message": f"真实逐日预报通常只覆盖未来 16 天，请在 {check_again.isoformat()} 后再查看；当前不会用历史平均值冒充预报。",
            }
        forecast_start, forecast_end = max(start, current), min(end, horizon)
        result = self.forecast(
            place["latitude"], place["longitude"], forecast_start.isoformat(), forecast_end.isoformat(),
            "auto", force_refresh=force_refresh,
        )
        if not result or not result.get("items"):
            return {**base, "status": "unavailable", "trip_start": start.isoformat(), "trip_end": end.isoformat(), "message": "天气接口暂时没有返回有效预报，请稍后重试。"}
        items = list(result["items"])
        self._attach_itinerary_recommendations(trip, items)
        partial = forecast_start != start or forecast_end != end
        return {
            **base, "status": "partial" if partial else "available", "trip_start": start.isoformat(), "trip_end": end.isoformat(),
            "covered_start": forecast_start.isoformat(), "covered_end": forecast_end.isoformat(), "items": items,
            "advice": self._daily_advice(items), "source": result.get("source") or "Open-Meteo",
            "provider": result.get("provider") or "Open-Meteo", "source_type": result.get("source_type") or "real_api",
            "original_source_type": result.get("original_source_type") or result.get("source_type") or "real_api",
            "evidence_at": result.get("evidence_at"), "cache_age_seconds": result.get("cache_age_seconds"),
            "timezone": result.get("timezone"),
            "message": (f"已查询未来 {range_days} 天。" if range_days else "已覆盖全部旅行日期。") if not partial else f"目前只可核对 {forecast_start.isoformat()} 至 {forecast_end.isoformat()}；其余日期进入 16 天预报范围后再刷新。",
        }

    def enrich(self, context: dict) -> dict:
        result = dict(context)
        result["members"] = []
        for member in context.get("members", []):
            value = dict(member)
            value["weather"] = self.current(value.get("latitude"), value.get("longitude"), value.get("timezone", "auto"))
            result["members"].append(value)
        result["weather_enabled"] = self.enabled
        return result
