"""Small Open-Meteo adapter with a no-network fallback for the self-hosted app."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date
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
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "阵雨",
    81: "较强阵雨",
    82: "强阵雨",
    95: "雷雨",
}


class OpenMeteoWeather:
    def __init__(self, enabled: bool | None = None, timeout: int = 4):
        if enabled is None:
            enabled = os.getenv("OPEN_METEO_ENABLED", "true").strip().lower() == "true"
        self.enabled = enabled
        self.timeout = max(1, min(timeout, 10))
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def current(self, latitude: object, longitude: object, timezone_name: str = "auto") -> dict | None:
        if not self.enabled or latitude is None or longitude is None:
            return None
        key = f"{float(latitude):.4f},{float(longitude):.4f},{timezone_name}"
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                return dict(cached[1])
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
                "updated_at": str(current.get("time", "")),
            }
        except (error.URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        with self._lock:
            self._cache[key] = (time.monotonic() + 1800, value)
        return dict(value)

    def forecast(
        self,
        latitude: object,
        longitude: object,
        start_date: str,
        end_date: str,
        timezone_name: str = "auto",
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
            if cached and cached[0] > time.monotonic():
                return dict(cached[1])
        query = parse.urlencode({
            "latitude": float(latitude), "longitude": float(longitude),
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "daily": "weather_code,precipitation_probability_max,temperature_2m_max,temperature_2m_min",
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
            items = []
            for index, value_date in enumerate(dates[:16]):
                code = int(codes[index])
                items.append({
                    "date": str(value_date), "weather_code": code,
                    "label": WEATHER_LABELS.get(code, "天气变化中"),
                    "precipitation_probability": int(probabilities[index]) if probabilities[index] is not None else None,
                    "temperature_max": round(float(highs[index]), 1) if highs[index] is not None else None,
                    "temperature_min": round(float(lows[index]), 1) if lows[index] is not None else None,
                })
            value = {"items": items, "source": "Open-Meteo", "timezone": str(payload.get("timezone") or timezone_name)}
        except (error.URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            return None
        with self._lock:
            self._cache[key] = (time.monotonic() + 1800, value)
        return dict(value)

    def enrich(self, context: dict) -> dict:
        result = dict(context)
        result["members"] = []
        for member in context.get("members", []):
            value = dict(member)
            value["weather"] = self.current(value.get("latitude"), value.get("longitude"), value.get("timezone", "auto"))
            result["members"].append(value)
        result["weather_enabled"] = self.enabled
        return result
