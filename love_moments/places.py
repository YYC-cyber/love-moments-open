"""Destination candidate lookup and normalization.

Free text is never a reliable location identifier: names such as ``Disney``,
``People's Park`` and ``West Lake`` can refer to several places.  This module
normalizes structured candidates from AMap (China) and Google Maps results via
the already configured SerpApi account.  The caller still has to present the
candidates to the user; the first result is never selected automatically.
"""

from __future__ import annotations

import json
import os
from typing import Callable
from urllib import error, parse, request

from .http_client import TRUSTED_SSL_CONTEXT


def _text(value: object, maximum: int = 240) -> str:
    if isinstance(value, list):
        value = "、".join(str(item) for item in value if item)
    return str(value or "").strip()[:maximum]


def normalize_place_candidate(value: dict) -> dict:
    """Return the small, provider-neutral location record persisted by trips."""
    if not isinstance(value, dict):
        raise ValueError("地点候选格式无效")
    provider = _text(value.get("provider"), 40)
    if provider not in {"amap", "geoapify", "google_maps", "manual"}:
        raise ValueError("地点来源无效")
    name = _text(value.get("name"), 160)
    if not name:
        raise ValueError("地点名称不能为空")
    try:
        latitude = float(value["latitude"]) if value.get("latitude") not in {None, ""} else None
        longitude = float(value["longitude"]) if value.get("longitude") not in {None, ""} else None
    except (TypeError, ValueError) as exc:
        raise ValueError("地点坐标无效") from exc
    if latitude is not None and not -90 <= latitude <= 90:
        raise ValueError("地点纬度无效")
    if longitude is not None and not -180 <= longitude <= 180:
        raise ValueError("地点经度无效")
    if provider != "manual" and (latitude is None or longitude is None):
        raise ValueError("地图地点缺少坐标")
    return {
        "provider": provider,
        "provider_place_id": _text(value.get("provider_place_id"), 180),
        "name": name,
        "formatted_address": _text(value.get("formatted_address"), 360),
        "country": _text(value.get("country"), 80),
        "province": _text(value.get("province"), 80),
        "city": _text(value.get("city"), 80),
        "district": _text(value.get("district"), 80),
        "latitude": latitude,
        "longitude": longitude,
        "category": _text(value.get("category"), 120),
    }


class GoogleMapsPlaceSearch:
    """Global place candidates returned by SerpApi's Google Maps engine."""

    endpoint = "https://serpapi.com/search.json"

    def __init__(self, api_key: str, timeout: int = 20, before_request: Callable[[str], None] | None = None):
        self.api_key = api_key.strip()
        self.timeout = max(5, min(int(timeout), 45))
        self.before_request = before_request
        self.is_configured = bool(self.api_key)

    def search(self, query: str, region_hint: str = "", limit: int = 8) -> list[dict]:
        query = _text(query, 120)
        region_hint = _text(region_hint, 120)
        if not self.is_configured or not query:
            return []
        scoped_query = " ".join(value for value in (region_hint, query) if value)
        if self.before_request:
            self.before_request("global destination candidate search")
        params = {
            "engine": "google_maps", "type": "search", "q": scoped_query,
            "hl": "zh-cn", "api_key": self.api_key,
        }
        try:
            with request.urlopen(
                request.Request(f"{self.endpoint}?{parse.urlencode(params)}", headers={"Accept": "application/json", "User-Agent": "LoveMoments/1.0"}),
                timeout=self.timeout,
                context=TRUSTED_SSL_CONTEXT,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError) as exc:
            raise RuntimeError("全球地点检索暂时不可用") from exc
        if not isinstance(result, dict):
            raise RuntimeError("全球地点检索返回格式无效")
        if result.get("error"):
            raise RuntimeError(f"全球地点检索失败：{_text(result['error'], 160)}")
        rows = result.get("local_results") or result.get("place_results") or []
        if isinstance(rows, dict):
            rows = [rows]
        candidates, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            coordinates = row.get("gps_coordinates") if isinstance(row.get("gps_coordinates"), dict) else {}
            place_id = _text(row.get("place_id") or row.get("data_id") or row.get("data_cid"), 180)
            name = _text(row.get("title") or row.get("name"), 160)
            address = _text(row.get("address"), 360)
            try:
                latitude = float(coordinates.get("latitude"))
                longitude = float(coordinates.get("longitude"))
            except (TypeError, ValueError):
                continue
            key = place_id or f"{name}:{latitude:.5f}:{longitude:.5f}"
            if not name or key in seen:
                continue
            seen.add(key)
            candidates.append(normalize_place_candidate({
                "provider": "google_maps", "provider_place_id": place_id,
                "name": name, "formatted_address": address,
                "country": _text(row.get("country"), 80),
                "province": _text(row.get("state"), 80),
                "city": _text(row.get("city"), 80),
                "district": _text(row.get("district"), 80),
                "latitude": latitude, "longitude": longitude,
                "category": _text(row.get("type"), 120),
            }))
            if len(candidates) >= max(1, min(int(limit), 12)):
                break
        return candidates


def google_place_search_from_env(before_request: Callable[[str], None] | None = None) -> GoogleMapsPlaceSearch:
    enabled = os.getenv("SERPAPI_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    key = os.getenv("SERPAPI_API_KEY", "").strip() if enabled else ""
    try:
        timeout = int(os.getenv("QUOTE_TIMEOUT_SECONDS", "20"))
    except ValueError:
        timeout = 20
    return GoogleMapsPlaceSearch(key, timeout=timeout, before_request=before_request)
