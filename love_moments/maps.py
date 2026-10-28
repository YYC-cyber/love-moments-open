"""AMap, Geoapify, and deterministic offline map adapters."""

from __future__ import annotations

import hashlib
import json
import math
import os
import ssl
import threading
import time
from typing import Callable
from urllib import error, parse, request

try:
    import certifi
except ImportError:  # pragma: no cover - dependency is present in production installs
    certifi = None


class MapProvider:
    name = "unknown"
    is_mock = False

    def geocode(self, address: str, city: str = "") -> dict:
        raise NotImplementedError

    def route(self, origin: str, destination: str, mode: str = "walking", city: str = "") -> dict:
        raise NotImplementedError

    def route_between(self, origin: dict, destination: dict, mode: str = "walking", city: str = "") -> dict:
        """Route between already confirmed coordinates without geocoding again."""
        raise NotImplementedError

    def search_food(self, keyword: str, city: str = "", limit: int = 5, near: str = "") -> list[dict]:
        raise NotImplementedError

    def search_food_nearby(
        self, keyword: str, latitude: float, longitude: float, city: str = "", limit: int = 5,
    ) -> list[dict]:
        """Search restaurants around already supplied coordinates."""
        raise NotImplementedError

    def search_places(self, keyword: str, region_hint: str = "", limit: int = 8) -> list[dict]:
        raise NotImplementedError


class MockMapProvider(MapProvider):
    name = "mock"
    is_mock = True

    @staticmethod
    def _coordinate(label: str) -> tuple[float, float]:
        digest = hashlib.sha256(label.encode()).digest()
        return round(18 + int.from_bytes(digest[:2], "big") / 65535 * 28, 6), round(100 + int.from_bytes(digest[2:4], "big") / 65535 * 24, 6)

    def geocode(self, address: str, city: str = "") -> dict:
        lat, lng = self._coordinate(f"{city}:{address}")
        return {"name": address, "city": city, "latitude": lat, "longitude": lng, "source": "mock", "mock": True}

    def route(self, origin: str, destination: str, mode: str = "walking", city: str = "") -> dict:
        start, end = self.geocode(origin, city), self.geocode(destination, city)
        return self.route_between(start, end, mode, city)

    def route_between(self, origin: dict, destination: dict, mode: str = "walking", city: str = "") -> dict:
        start, end = dict(origin), dict(destination)
        distance = self._distance(start["latitude"], start["longitude"], end["latitude"], end["longitude"])
        speed = {"walking": 4.5, "bicycling": 14, "driving": 30, "transit": 22}.get(mode, 4.5)
        return {"origin": start, "destination": end, "mode": mode, "distance_meters": round(distance), "duration_seconds": round(distance / 1000 / speed * 3600), "steps": [{"instruction": f"从{start.get('name', '起点')}前往{end.get('name', '终点')}", "distance_meters": round(distance)}], "provider": "mock", "mock": True}

    def search_food(self, keyword: str, city: str = "", limit: int = 5, near: str = "") -> list[dict]:
        del near
        # Never invent a shop name. The UI can still show local dish ideas, but
        # exact restaurants only appear after a real places provider responds.
        return []

    def search_food_nearby(
        self, keyword: str, latitude: float, longitude: float, city: str = "", limit: int = 5,
    ) -> list[dict]:
        del keyword, latitude, longitude, city, limit
        return []

    def search_places(self, keyword: str, region_hint: str = "", limit: int = 8) -> list[dict]:
        del keyword, region_hint, limit
        return []

    @staticmethod
    def _distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        radius = 6_371_000
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
        value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


class AmapProvider(MapProvider):
    name = "amap"
    _rate_lock = threading.Lock()
    _last_request_at = 0.0

    def __init__(self, key: str, timeout: int = 15, before_request: Callable[[str], None] | None = None):
        self.key, self.timeout = key, timeout
        self.before_request = before_request
        self.ssl_context = ssl.create_default_context(cafile=certifi.where()) if certifi else ssl.create_default_context()
        self._geocode_cache: dict[tuple[str, str], dict] = {}

    def _get(self, endpoint: str, values: dict) -> dict:
        url = f"https://restapi.amap.com{endpoint}?{parse.urlencode({**values, 'key': self.key})}"
        for attempt in range(4):
            try:
                if self.before_request:
                    self.before_request(f"{endpoint} attempt {attempt + 1}")
                with self._rate_lock:
                    delay = 0.25 - (time.monotonic() - self.__class__._last_request_at)
                    if delay > 0:
                        time.sleep(delay)
                    with request.urlopen(url, timeout=self.timeout, context=self.ssl_context) as response:
                        result = json.loads(response.read().decode("utf-8"))
                    self.__class__._last_request_at = time.monotonic()
            except (error.URLError, json.JSONDecodeError, UnicodeError) as exc:
                raise RuntimeError("高德地图暂时无法连接") from exc
            if result.get("status") == "1":
                return result
            info = str(result.get("info", "未知错误"))
            if info == "CUQPS_HAS_EXCEEDED_THE_LIMIT" and attempt < 3:
                time.sleep(0.75 * (attempt + 1))
                continue
            raise RuntimeError(f"高德地图请求失败：{info}")
        raise RuntimeError("高德地图请求失败：请求过于频繁")

    def geocode(self, address: str, city: str = "") -> dict:
        cache_key = (address.strip(), city.strip())
        if cache_key in self._geocode_cache:
            return dict(self._geocode_cache[cache_key])
        result = self._get("/v3/geocode/geo", {"address": address, "city": city})
        rows = result.get("geocodes") or []
        if not rows or "," not in str(rows[0].get("location", "")):
            raise ValueError("没有找到该地点")
        lng, lat = map(float, rows[0]["location"].split(","))
        normalized = {"name": rows[0].get("formatted_address", address), "city": city, "latitude": lat, "longitude": lng, "source": "amap", "mock": False}
        self._geocode_cache[cache_key] = normalized
        return dict(normalized)

    def route(self, origin: str, destination: str, mode: str = "walking", city: str = "") -> dict:
        start, end = self.geocode(origin, city), self.geocode(destination, city)
        return self.route_between(start, end, mode, city)

    def route_between(self, origin: dict, destination: dict, mode: str = "walking", city: str = "") -> dict:
        start, end = dict(origin), dict(destination)
        if mode not in {"walking", "driving", "bicycling", "transit"}:
            raise ValueError("不支持的出行方式")
        path = {"walking": "walking", "driving": "driving", "bicycling": "bicycling", "transit": "transit/integrated"}.get(mode, "walking")
        values = {"origin": f"{start['longitude']},{start['latitude']}", "destination": f"{end['longitude']},{end['latitude']}"}
        if mode == "transit":
            values["city"] = city
        # The legacy v3 cycling endpoint now returns SERVICE_NOT_AVAILABLE for
        # valid keys. AMap Route Planning 2.0 exposes cycling through v5 while
        # the already verified walking/transit/driving calls remain compatible.
        endpoint = "/v5/direction/bicycling" if mode == "bicycling" else f"/v3/direction/{path}"
        result = self._get(endpoint, values)
        route = result.get("route", {})
        paths = route.get("paths") or route.get("transits") or []
        if not paths:
            raise ValueError("没有找到可用路线")
        first = paths[0]
        steps = first.get("steps", [])
        return {"origin": start, "destination": end, "mode": mode, "distance_meters": int(float(first.get("distance", 0))), "duration_seconds": int(float(first.get("duration", 0))), "steps": [{"instruction": step.get("instruction", step.get("walking", {}).get("origin", "继续前行")), "distance_meters": int(float(step.get("distance", 0) or 0))} for step in steps[:30]], "provider": "amap", "mock": False}

    @staticmethod
    def _text(value: object) -> str:
        if isinstance(value, list):
            return "、".join(str(item) for item in value if item)
        return str(value or "")

    def _food_results(self, rows: list[dict], safe_limit: int) -> list[dict]:
        results = []
        for row in rows[:safe_limit]:
            location = self._text(row.get("location"))
            if "," not in location:
                continue
            try:
                longitude, latitude = (float(value) for value in location.split(",", 1))
            except ValueError:
                continue
            biz = row.get("biz_ext") if isinstance(row.get("biz_ext"), dict) else {}
            rating_text, cost_text = self._text(biz.get("rating")), self._text(biz.get("cost"))
            try:
                rating = float(rating_text) if rating_text else None
            except ValueError:
                rating = None
            try:
                average_cost_cents = int(round(float(cost_text) * 100)) if cost_text else None
            except ValueError:
                average_cost_cents = None
            name = self._text(row.get("name"))[:120]
            address = self._text(row.get("address"))[:300]
            results.append({
                "amap_poi_id": self._text(row.get("id"))[:80],
                "name": name,
                "address": address,
                "district": self._text(row.get("adname") or row.get("business_area"))[:80],
                "telephone": self._text(row.get("tel"))[:100],
                "category": self._text(row.get("type"))[:160],
                "rating": rating,
                "average_cost_cents": average_cost_cents,
                "opening_hours": self._text(biz.get("open_time") or biz.get("opentime2"))[:160],
                "latitude": latitude,
                "longitude": longitude,
                "distance_meters": int(float(self._text(row.get("distance")) or 0)) or None,
                "navigation_url": "https://uri.amap.com/marker?" + parse.urlencode({"position": f"{longitude},{latitude}", "name": name, "src": "love_moments", "coordinate": "gaode", "callnative": "0"}),
                "source": "amap",
                "mock": False,
            })
        return results

    def search_food(self, keyword: str, city: str = "", limit: int = 5, near: str = "") -> list[dict]:
        safe_limit = max(1, min(int(limit), 10))
        values = {
            "keywords": keyword,
            "types": "050000",
            "city": city,
            "citylimit": "true" if city else "false",
            "extensions": "all",
            "offset": safe_limit,
            "page": 1,
        }
        endpoint = "/v3/place/text"
        if near:
            try:
                center = self.geocode(near, city)
            except ValueError:
                # Scenic spots and old towns are often not valid AMap city
                # values. Retry globally while keeping the exact anchor name.
                center = self.geocode(near, "")
            endpoint = "/v3/place/around"
            values = {
                "location": f"{center['longitude']},{center['latitude']}", "keywords": keyword,
                "types": "050000", "radius": 5000, "sortrule": "distance", "extensions": "all",
                "offset": safe_limit, "page": 1,
            }
        rows = self._get(endpoint, values).get("pois") or []
        if not rows and city and not near:
            values.update({"keywords": f"{city} {keyword}", "city": "", "citylimit": "false"})
            rows = self._get("/v3/place/text", values).get("pois") or []
        return self._food_results(rows, safe_limit)

    def search_food_nearby(
        self, keyword: str, latitude: float, longitude: float, city: str = "", limit: int = 5,
    ) -> list[dict]:
        del city
        safe_limit = max(1, min(int(limit), 10))
        rows = self._get("/v3/place/around", {
            "location": f"{float(longitude)},{float(latitude)}", "keywords": str(keyword)[:120],
            "types": "050000", "radius": 5000, "sortrule": "distance", "extensions": "all",
            "offset": safe_limit, "page": 1,
        }).get("pois") or []
        return self._food_results(rows, safe_limit)

    def search_places(self, keyword: str, region_hint: str = "", limit: int = 8) -> list[dict]:
        """Search real Chinese POIs without choosing the first match for users."""
        safe_limit = max(1, min(int(limit), 12))
        keyword, region_hint = str(keyword).strip()[:120], str(region_hint).strip()[:80]
        if not keyword:
            return []
        values = {
            "keywords": keyword, "city": region_hint,
            "citylimit": "true" if region_hint else "false",
            "extensions": "all", "offset": safe_limit, "page": 1,
        }
        rows = self._get("/v3/place/text", values).get("pois") or []
        results = []
        for row in rows[:safe_limit]:
            location = self._text(row.get("location"))
            if "," not in location:
                continue
            try:
                longitude, latitude = (float(value) for value in location.split(",", 1))
            except ValueError:
                continue
            name = self._text(row.get("name"))[:160]
            if not name:
                continue
            results.append({
                "provider": "amap",
                "provider_place_id": self._text(row.get("id"))[:180],
                "name": name,
                "formatted_address": self._text(row.get("address"))[:360],
                "country": "中国",
                "province": self._text(row.get("pname"))[:80],
                "city": self._text(row.get("cityname"))[:80],
                "district": self._text(row.get("adname"))[:80],
                "latitude": latitude,
                "longitude": longitude,
                "category": self._text(row.get("type"))[:120],
            })
        return results


class GeoapifyProvider(MapProvider):
    """Global geocoding and routing using Geoapify's no-card free plan."""

    name = "geoapify"
    base_url = "https://api.geoapify.com"

    def __init__(self, api_key: str, timeout: int = 20, before_request: Callable[[str], None] | None = None):
        self.api_key = api_key.strip()
        self.timeout = max(5, min(int(timeout), 45))
        self.before_request = before_request
        self.ssl_context = ssl.create_default_context(cafile=certifi.where()) if certifi else ssl.create_default_context()
        self._geocode_cache: dict[tuple[str, str], dict] = {}

    def _get(self, endpoint: str, values: dict, operation: str) -> dict:
        if not self.api_key:
            raise RuntimeError("Geoapify 未配置")
        if self.before_request:
            # The bounded requests used below cost one Geoapify credit each.
            self.before_request(operation)
        url = f"{self.base_url}{endpoint}?{parse.urlencode({**values, 'apiKey': self.api_key})}"
        try:
            with request.urlopen(
                request.Request(url, headers={"Accept": "application/json", "User-Agent": "LoveMoments/1.0"}),
                timeout=self.timeout,
                context=self.ssl_context,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError) as exc:
            raise RuntimeError("Geoapify 暂时无法连接") from exc
        if not isinstance(result, dict):
            raise RuntimeError("Geoapify 返回格式无效")
        if result.get("error"):
            message = result.get("message") or result.get("error")
            raise RuntimeError(f"Geoapify 请求失败：{message}")
        return result

    @staticmethod
    def _result_rows(result: dict) -> list[dict]:
        rows = result.get("results")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        features = result.get("features")
        if isinstance(features, list):
            return [feature.get("properties", {}) for feature in features if isinstance(feature, dict)]
        return []

    @classmethod
    def _place(cls, row: dict, fallback_name: str = "") -> dict | None:
        try:
            latitude, longitude = float(row.get("lat")), float(row.get("lon"))
        except (TypeError, ValueError):
            return None
        name = str(row.get("name") or row.get("address_line1") or fallback_name or "").strip()[:160]
        if not name:
            return None
        return {
            "provider": "geoapify",
            "provider_place_id": str(row.get("place_id") or "")[:180],
            "name": name,
            "formatted_address": str(row.get("formatted") or row.get("address_line2") or "")[:360],
            "country": str(row.get("country") or "")[:80],
            "province": str(row.get("state") or row.get("region") or "")[:80],
            "city": str(row.get("city") or row.get("town") or row.get("village") or "")[:80],
            "district": str(row.get("district") or row.get("county") or row.get("suburb") or "")[:80],
            "latitude": latitude,
            "longitude": longitude,
            "category": str(row.get("result_type") or row.get("category") or "")[:120],
            "source": "geoapify",
            "mock": False,
        }

    def geocode(self, address: str, city: str = "") -> dict:
        address, city = str(address).strip()[:160], str(city).strip()[:120]
        cache_key = (address, city)
        if cache_key in self._geocode_cache:
            return dict(self._geocode_cache[cache_key])
        text = " ".join(value for value in (city, address) if value)
        result = self._get(
            "/v1/geocode/search",
            {"text": text, "format": "json", "lang": "zh", "limit": 1},
            "global geocode",
        )
        row = next(iter(self._result_rows(result)), None)
        normalized = self._place(row or {}, address)
        if not normalized:
            raise ValueError("没有找到该地点")
        self._geocode_cache[cache_key] = normalized
        return dict(normalized)

    def search_places(self, keyword: str, region_hint: str = "", limit: int = 8) -> list[dict]:
        keyword, region_hint = str(keyword).strip()[:120], str(region_hint).strip()[:120]
        if not keyword:
            return []
        safe_limit = max(1, min(int(limit), 12))
        text = " ".join(value for value in (region_hint, keyword) if value)
        result = self._get(
            "/v1/geocode/autocomplete",
            {"text": text, "format": "json", "lang": "zh", "limit": safe_limit},
            "global place autocomplete",
        )
        candidates, seen = [], set()
        for row in self._result_rows(result):
            place = self._place(row, keyword)
            if not place:
                continue
            unique = place["provider_place_id"] or f"{place['latitude']}:{place['longitude']}:{place['name']}"
            if unique in seen:
                continue
            seen.add(unique)
            candidates.append(place)
        return candidates

    def route(self, origin: str, destination: str, mode: str = "walking", city: str = "") -> dict:
        return self.route_between(self.geocode(origin, city), self.geocode(destination, city), mode, city)

    def route_between(self, origin: dict, destination: dict, mode: str = "walking", city: str = "") -> dict:
        del city
        mode_map = {"walking": "walk", "bicycling": "bicycle", "driving": "drive", "transit": "transit"}
        if mode not in mode_map:
            raise ValueError("不支持的出行方式")
        start, end = dict(origin), dict(destination)
        waypoints = f"{float(start['latitude'])},{float(start['longitude'])}|{float(end['latitude'])},{float(end['longitude'])}"
        result = self._get(
            "/v1/routing",
            {"waypoints": waypoints, "mode": mode_map[mode], "details": "instruction_details"},
            f"global route {mode}",
        )
        features = result.get("features") if isinstance(result.get("features"), list) else []
        if not features or not isinstance(features[0], dict):
            raise ValueError("没有找到可用路线")
        properties = features[0].get("properties") if isinstance(features[0].get("properties"), dict) else {}
        steps = []
        for leg in properties.get("legs") or []:
            if not isinstance(leg, dict):
                continue
            for step in leg.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                instruction = step.get("instruction")
                if isinstance(instruction, dict):
                    instruction = instruction.get("text") or instruction.get("transition_instruction")
                steps.append({
                    "instruction": str(instruction or step.get("text") or "继续前行")[:300],
                    "distance_meters": int(round(float(step.get("distance") or 0))),
                })
                if len(steps) >= 30:
                    break
        return {
            "origin": start,
            "destination": end,
            "mode": mode,
            "distance_meters": int(round(float(properties.get("distance") or 0))),
            "duration_seconds": int(round(float(properties.get("time") or properties.get("duration") or 0))),
            "steps": steps,
            "provider": "geoapify",
            "mock": False,
        }

    def search_food(self, keyword: str, city: str = "", limit: int = 5, near: str = "") -> list[dict]:
        # Restaurant enrichment uses a different persisted record shape. Keep
        # it separate so a global place is never mislabeled as an AMap POI.
        del keyword, city, limit, near
        return []

    def search_food_nearby(
        self, keyword: str, latitude: float, longitude: float, city: str = "", limit: int = 5,
    ) -> list[dict]:
        del keyword, city
        safe_limit = max(1, min(int(limit), 10))
        result = self._get(
            "/v2/places",
            {
                "categories": "catering.restaurant,catering.fast_food,catering.cafe",
                "filter": f"circle:{float(longitude)},{float(latitude)},5000",
                "bias": f"proximity:{float(longitude)},{float(latitude)}",
                "limit": safe_limit, "lang": "zh",
            },
            "nearby restaurants",
        )
        features = result.get("features") if isinstance(result.get("features"), list) else []
        rows = []
        for feature in features[:safe_limit]:
            properties = feature.get("properties") if isinstance(feature, dict) and isinstance(feature.get("properties"), dict) else {}
            place = self._place(properties)
            if not place:
                continue
            distance = properties.get("distance")
            try:
                distance_meters = int(round(float(distance))) if distance is not None else None
            except (TypeError, ValueError):
                distance_meters = None
            rows.append({
                "name": place["name"], "address": place["formatted_address"],
                "district": place["district"], "telephone": str(properties.get("contact", {}).get("phone", ""))[:100] if isinstance(properties.get("contact"), dict) else "",
                "category": place["category"], "rating": None, "average_cost_cents": None,
                "opening_hours": str(properties.get("opening_hours") or "")[:160],
                "latitude": place["latitude"], "longitude": place["longitude"],
                "distance_meters": distance_meters, "navigation_url": "", "source": "geoapify", "mock": False,
                "provider_place_id": place["provider_place_id"],
            })
        return rows


def map_provider_from_env(before_request: Callable[[str], None] | None = None) -> MapProvider:
    key = os.getenv("AMAP_WEB_KEY", "").strip()
    return AmapProvider(key, before_request=before_request) if key else MockMapProvider()


def geoapify_provider_from_env(before_request: Callable[[str], None] | None = None) -> MapProvider:
    enabled = os.getenv("GEOAPIFY_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    key = os.getenv("GEOAPIFY_API_KEY", "").strip() if enabled else ""
    try:
        timeout = int(os.getenv("GEOAPIFY_TIMEOUT_SECONDS", "20"))
    except ValueError:
        timeout = 20
    return GeoapifyProvider(key, timeout=timeout, before_request=before_request) if key else MockMapProvider()
