"""Travel quote providers with explicit live, aggregate and mock labelling."""

from __future__ import annotations

import json
import math
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib import error, parse, request

from .http_client import TRUSTED_SSL_CONTEXT


_CACHE: dict[str, tuple[datetime, dict]] = {}
_CACHE_LOCK = threading.Lock()
_USAGE_LOCK = threading.Lock()
_CHINA_COUNTRY_NAMES = {"中国", "中华人民共和国", "china", "cn", "prc"}
_DOMESTIC_HOTEL_HOSTS = {
    "ctrip.com": "携程", "qunar.com": "去哪儿", "fliggy.com": "飞猪",
    "meituan.com": "美团", "elong.com": "艺龙", "ly.com": "同程旅行",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _date_value(value: object, field: str) -> str:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field}格式无效") from exc


def _positive_int(value: object, default: int = 2, maximum: int = 9) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _safe_url(value: object) -> str:
    text = str(value or "")[:1000]
    return text if parse.urlparse(text).scheme in {"http", "https"} else ""


def _is_china_country(value: object) -> bool:
    return str(value or "").strip().lower() in _CHINA_COUNTRY_NAMES


def _domestic_hotel_link(prices: list[object]) -> tuple[str, str] | None:
    for price in prices:
        if not isinstance(price, dict):
            continue
        url = _safe_url(price.get("link"))
        host = (parse.urlparse(url).hostname or "").lower()
        for allowed_host, label in _DOMESTIC_HOTEL_HOSTS.items():
            if host == allowed_host or host.endswith(f".{allowed_host}"):
                return url, label
    return None


def _coordinate(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    radius = 6_371_000
    first, second = math.radians(lat1), math.radians(lat2)
    latitude_delta = math.radians(lat2 - lat1)
    longitude_delta = math.radians(lon2 - lon1)
    value = math.sin(latitude_delta / 2) ** 2 + math.cos(first) * math.cos(second) * math.sin(longitude_delta / 2) ** 2
    return round(radius * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value))))


class QuoteProvider:
    name = "unknown"
    is_mock = False

    def search(self, query: dict) -> list[dict]:
        raise NotImplementedError

    def attach_request_guard(self, before_request: Callable[[str], None]) -> None:
        """Attach the application's persistent allowance before a provider is used."""
        self.before_request = before_request


class MockQuoteProvider(QuoteProvider):
    name = "mock"
    is_mock = True

    def search(self, query: dict) -> list[dict]:
        quote_type = str(query.get("type", "hotel"))
        destination = str(query.get("destination", "目的地"))[:80]
        base = max(10000, min(int(query.get("budget_cents", 60000) or 60000), 2_000_000))
        now = _now()
        labels = [
            ("演示平台 A", .72, "可免费取消"),
            ("演示平台 B", .82, "部分可退"),
            ("演示平台 C", .90, "含基础服务"),
            ("演示平台 D", .95, "退改以平台为准"),
            ("手工比价参考", 1.0, "请到平台确认"),
        ]
        noun = {"hotel": "住宿", "flight": "航班", "train": "车次"}.get(quote_type, "交通")
        return [{
            "quote_type": quote_type, "provider": name, "source_mode": "mock",
            "title": f"{destination}{noun}示例 {index}", "subtitle": "模拟报价，不代表真实库存",
            "amount_cents": int(base * ratio), "currency": "CNY", "terms": terms,
            "includes_tax": True, "booking_url": "", "fetched_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=30)).isoformat(), "mock": True,
        } for index, (name, ratio, terms) in enumerate(labels, 1)]


class AuthorizedHttpQuoteProvider(QuoteProvider):
    name = "authorized-http"

    def __init__(self, endpoint: str, api_key: str, timeout: int = 20, before_request: Callable[[str], None] | None = None):
        self.endpoint, self.api_key, self.timeout = endpoint, api_key, timeout
        self.before_request = before_request

    def search(self, query: dict) -> list[dict]:
        req = request.Request(self.endpoint, data=json.dumps(query, ensure_ascii=False).encode(), headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, method="POST")
        try:
            if self.before_request:
                self.before_request("authorized quote search")
            with request.urlopen(req, timeout=self.timeout, context=TRUSTED_SSL_CONTEXT) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (error.URLError, json.JSONDecodeError, UnicodeError) as exc:
            raise RuntimeError("报价服务暂时不可用") from exc
        rows = result.get("quotes")
        if not isinstance(rows, list):
            raise RuntimeError("报价服务返回格式无效")
        normalized = []
        for row in rows[:50]:
            if not isinstance(row, dict) or not row.get("provider") or not row.get("title"):
                continue
            try:
                amount_cents = max(0, min(int(row.get("amount_cents", 0)), 100_000_000))
            except (TypeError, ValueError):
                continue
            normalized.append({
                "quote_type": str(row.get("quote_type", query.get("type", "hotel")))[:20], "provider": str(row["provider"])[:80],
                "source_mode": "live", "title": str(row["title"])[:200], "subtitle": str(row.get("subtitle", ""))[:300],
                "amount_cents": amount_cents, "currency": str(row.get("currency", "CNY"))[:8], "terms": str(row.get("terms", ""))[:500],
                "includes_tax": bool(row.get("includes_tax", True)), "booking_url": _safe_url(row.get("booking_url")),
                "fetched_at": str(row.get("fetched_at") or _now().isoformat()), "expires_at": row.get("expires_at"), "mock": False,
            })
        if not normalized:
            raise RuntimeError("报价服务没有返回可用结果")
        return normalized


class SerpApiQuoteProvider(QuoteProvider):
    """Google Flights/Hotels aggregate results exposed by SerpApi."""

    name = "serpapi-google-travel"
    endpoint = "https://serpapi.com/search.json"
    AIRPORTS = {
        "北京": "PEK", "北京首都": "PEK", "北京大兴": "PKX", "上海": "SHA", "上海浦东": "PVG",
        "广州": "CAN", "深圳": "SZX", "杭州": "HGH", "成都": "CTU", "成都天府": "TFU",
        "重庆": "CKG", "南京": "NKG", "武汉": "WUH", "西安": "XIY", "长沙": "CSX", "昆明": "KMG",
        "三亚": "SYX", "海口": "HAK", "厦门": "XMN", "青岛": "TAO", "香港": "HKG", "澳门": "MFM",
        "台北": "TPE", "东京": "TYO", "大阪": "OSA", "首尔": "SEL", "新加坡": "SIN", "曼谷": "BKK",
        "巴黎": "PAR", "伦敦": "LON", "纽约": "NYC", "洛杉矶": "LAX", "悉尼": "SYD", "迪拜": "DXB",
    }

    def __init__(self, api_key: str, timeout: int = 25, cache_ttl: int = 21600, monthly_limit: int = 0, usage_path: Path | None = None, before_request: Callable[[str], None] | None = None):
        self.api_key, self.timeout = api_key, max(5, min(timeout, 60))
        self.cache_ttl, self.monthly_limit, self.usage_path = max(0, min(cache_ttl, 86400)), max(0, monthly_limit), usage_path
        self.before_request = before_request

    def attach_request_guard(self, before_request: Callable[[str], None]) -> None:
        # The application guard stores usage atomically in SQLite. Keeping the
        # legacy JSON counter as well would double-count and fails in the
        # production read-only container before the real request can run.
        self.before_request = before_request
        self.monthly_limit = 0

    def search(self, query: dict) -> list[dict]:
        quote_type = str(query.get("type", "hotel"))
        if quote_type == "hotel":
            return self._hotels(query)
        if quote_type == "flight":
            return self._flights(query)
        raise ValueError("SerpApi 只处理酒店和机票查询")

    def _fetch(self, params: dict[str, object]) -> dict:
        safe_params = {key: str(value) for key, value in params.items() if value not in {None, ""}}
        cache_key = json.dumps(safe_params, ensure_ascii=False, sort_keys=True)
        now = _now()
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached and (now - cached[0]).total_seconds() <= self.cache_ttl:
                value = json.loads(json.dumps(cached[1], ensure_ascii=False))
                value["__love_moments_cache_hit"] = True
                value["__love_moments_fetched_at"] = cached[0].isoformat()
                return value
        self._reserve_usage()
        if self.before_request:
            self.before_request(str(safe_params.get("engine", "serpapi search")))
        url = f"{self.endpoint}?{parse.urlencode({**safe_params, 'api_key': self.api_key})}"
        try:
            with request.urlopen(request.Request(url, headers={"Accept": "application/json"}), timeout=self.timeout, context=TRUSTED_SSL_CONTEXT) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError) as exc:
            raise RuntimeError("机票/酒店聚合查询暂时不可用") from exc
        if not isinstance(result, dict):
            raise RuntimeError("机票/酒店聚合结果格式无效")
        if result.get("error"):
            raise RuntimeError(f"机票/酒店聚合查询失败：{str(result['error'])[:180]}")
        with _CACHE_LOCK:
            _CACHE[cache_key] = (now, result)
        value = json.loads(json.dumps(result, ensure_ascii=False))
        value["__love_moments_cache_hit"] = False
        value["__love_moments_fetched_at"] = now.isoformat()
        return value

    def _reserve_usage(self) -> None:
        if not self.monthly_limit or not self.usage_path:
            return
        month = _now().strftime("%Y-%m")
        with _USAGE_LOCK:
            value = {"month": month, "count": 0}
            try:
                loaded = json.loads(self.usage_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("month") == month:
                    value = {"month": month, "count": max(0, int(loaded.get("count", 0)))}
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
            if value["count"] >= self.monthly_limit:
                raise RuntimeError("本月机票/酒店免费查询保护额度已用完，请下月再试或调整额度")
            value["count"] += 1
            try:
                self.usage_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.usage_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                temporary.replace(self.usage_path)
            except OSError as exc:
                raise RuntimeError("无法记录查询额度，为避免超额已停止本次查询") from exc

    def _airport(self, value: object, field: str) -> str:
        text = str(value or "").strip()
        if re.fullmatch(r"[A-Za-z]{3}", text):
            return text.upper()
        code = self.AIRPORTS.get(text)
        if code:
            return code
        if not text or len(text) > 80:
            raise ValueError(f"{field}名称无效")
        result = self._fetch({"engine": "google_flights_autocomplete", "q": text, "hl": "zh-cn", "gl": "cn"})
        suggestions = result.get("suggestions")
        if not isinstance(suggestions, list):
            raise ValueError(f"没有识别到{field}对应的机场或城市")
        # SerpApi may return a city id (Google kgmid), an IATA id, or a city
        # group with child airports. Google Flights accepts both city and
        # airport identifiers, so prefer the top-ranked exact provider result.
        for suggestion in suggestions:
            if not isinstance(suggestion, dict):
                continue
            suggestion_id = str(suggestion.get("id", "")).strip()
            if suggestion_id:
                return suggestion_id
            airports = suggestion.get("airports")
            if isinstance(airports, list):
                for airport in airports:
                    if isinstance(airport, dict) and str(airport.get("id", "")).strip():
                        return str(airport["id"]).strip()
        raise ValueError(f"没有识别到{field}对应的机场或城市")

    def _hotels(self, query: dict) -> list[dict]:
        destination = str(query.get("destination", "")).strip()
        if not destination:
            raise ValueError("请填写酒店目的地")
        check_in = _date_value(query.get("check_in") or query.get("start_date"), "入住日期")
        check_out = _date_value(query.get("check_out") or query.get("end_date"), "离店日期")
        if check_out <= check_in:
            raise ValueError("离店日期必须晚于入住日期")
        result = self._fetch({"engine": "google_hotels", "q": destination, "check_in_date": check_in, "check_out_date": check_out, "adults": _positive_int(query.get("adults")), "currency": "CNY", "gl": "cn", "hl": "zh-cn"})
        anchor_latitude = _coordinate(query.get("destination_latitude"))
        anchor_longitude = _coordinate(query.get("destination_longitude"))
        now, rows = _now(), []
        fetched_at = str(result.get("__love_moments_fetched_at") or now.isoformat())
        try:
            fetched_time = datetime.fromisoformat(fetched_at)
        except ValueError:
            fetched_time, fetched_at = now, now.isoformat()
        source_mode = "cached" if bool(result.get("__love_moments_cache_hit")) else "aggregate"
        domestic_destination = _is_china_country(query.get("destination_country"))
        for item in result.get("properties", [])[:20]:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            rate = item.get("rate_per_night") if isinstance(item.get("rate_per_night"), dict) else {}
            amount = rate.get("extracted_lowest")
            try:
                amount_cents = max(0, int(round(float(amount) * 100)))
            except (TypeError, ValueError):
                continue
            prices = item.get("prices") if isinstance(item.get("prices"), list) else []
            source = next((str(price.get("source")) for price in prices if isinstance(price, dict) and price.get("source")), "Google Hotels 聚合")
            domestic_booking = _domestic_hotel_link(prices) if domestic_destination else None
            if domestic_booking:
                booking_url, domestic_platform = domestic_booking
                booking_mode, booking_label = "domestic_direct", f"去{domestic_platform}查看 / 购买"
                source = next((
                    str(price.get("source")) for price in prices
                    if isinstance(price, dict) and _safe_url(price.get("link")) == booking_url and price.get("source")
                ), source)
            elif domestic_destination:
                booking_url = "https://hotels.ctrip.com/list"
                booking_mode, booking_label = "domestic_search", "复制酒店名 · 去携程查询"
            else:
                booking_url = _safe_url(item.get("link")) or f"https://www.google.com/travel/search?q={parse.quote(destination + ' ' + str(item['name']))}"
                booking_mode, booking_label = "international", "跳转查看 / 购买"
            rating = item.get("overall_rating")
            hotel_class = item.get("hotel_class") or (f"{item.get('extracted_hotel_class')} 星" if item.get("extracted_hotel_class") else "")
            coordinates = item.get("gps_coordinates") if isinstance(item.get("gps_coordinates"), dict) else {}
            latitude = _coordinate(coordinates.get("latitude"))
            longitude = _coordinate(coordinates.get("longitude"))
            distance = _distance_meters(anchor_latitude, anchor_longitude, latitude, longitude) if None not in (anchor_latitude, anchor_longitude, latitude, longitude) else None
            distance_text = f"距已确认地点约 {distance / 1000:.1f} 公里" if distance is not None else ""
            terms = " · ".join(value for value in [str(hotel_class), f"评分 {rating}" if rating is not None else "", f"{item.get('reviews')} 条评价" if item.get("reviews") else "", distance_text, "每晚参考价"] if value)
            rows.append({
                "quote_type": "hotel", "provider": source[:80], "source_mode": source_mode, "title": str(item["name"])[:200],
                "subtitle": str(item.get("description", ""))[:300], "amount_cents": amount_cents, "currency": "CNY", "terms": terms[:500],
                "includes_tax": rate.get("extracted_before_taxes_fees") != amount,
                "booking_url": booking_url,
                "fetched_at": fetched_at, "expires_at": (fetched_time + timedelta(hours=6)).isoformat(), "mock": False,
                "details": {
                    "rating": rating, "reviews": item.get("reviews"), "hotel_class": hotel_class,
                    "check_in_time": item.get("check_in_time"), "check_out_time": item.get("check_out_time"),
                    "latitude": latitude, "longitude": longitude, "distance_meters": distance,
                    "original_title": str(item["name"])[:200], "domestic_destination": domestic_destination,
                    "booking_mode": booking_mode, "booking_label": booking_label,
                },
            })
        if not rows:
            raise RuntimeError("没有查到带价格的酒店，请更换目的地或日期")
        return sorted(rows, key=lambda row: row["amount_cents"])

    def _flights(self, query: dict) -> list[dict]:
        departure_id = self._airport(query.get("origin") or query.get("departure"), "出发地")
        arrival_id = self._airport(query.get("destination") or query.get("arrival"), "目的地")
        outbound = _date_value(query.get("outbound_date") or query.get("start_date") or query.get("date"), "出发日期")
        raw_return = query.get("return_date") or query.get("end_date")
        params: dict[str, object] = {"engine": "google_flights", "departure_id": departure_id, "arrival_id": arrival_id, "outbound_date": outbound, "adults": _positive_int(query.get("adults")), "currency": "CNY", "gl": "cn", "hl": "zh-cn", "travel_class": 1}
        if raw_return and str(raw_return) != outbound:
            return_date = _date_value(raw_return, "返程日期")
            if return_date <= outbound:
                raise ValueError("返程日期必须晚于出发日期")
            params.update({"return_date": return_date, "type": 1})
        else:
            params["type"] = 2
        result = self._fetch(params)
        now, rows = _now(), []
        fetched_at = str(result.get("__love_moments_fetched_at") or now.isoformat())
        try:
            fetched_time = datetime.fromisoformat(fetched_at)
        except ValueError:
            fetched_time, fetched_at = now, now.isoformat()
        source_mode = "cached" if bool(result.get("__love_moments_cache_hit")) else "aggregate"
        options = [*result.get("best_flights", []), *result.get("other_flights", [])]
        for option in options[:25]:
            if not isinstance(option, dict) or not isinstance(option.get("flights"), list) or not option["flights"]:
                continue
            legs = [leg for leg in option["flights"] if isinstance(leg, dict)]
            first, last = legs[0], legs[-1]
            dep = first.get("departure_airport") if isinstance(first.get("departure_airport"), dict) else {}
            arr = last.get("arrival_airport") if isinstance(last.get("arrival_airport"), dict) else {}
            try:
                amount_cents = max(0, int(round(float(option.get("price")) * 100)))
            except (TypeError, ValueError):
                continue
            airlines = list(dict.fromkeys(str(leg.get("airline")) for leg in legs if leg.get("airline")))
            numbers = [str(leg.get("flight_number")) for leg in legs if leg.get("flight_number")]
            duration = int(option.get("total_duration", 0) or 0)
            terms = f"{'直飞' if len(legs) == 1 else f'中转 {len(legs) - 1} 次'} · 总时长 {duration // 60}小时{duration % 60}分 · 经济舱"
            rows.append({
                "quote_type": "flight", "provider": " / ".join(airlines)[:80] or "Google Flights 聚合", "source_mode": source_mode,
                "title": " + ".join(numbers)[:200] or f"{departure_id} → {arrival_id}",
                "subtitle": f"{dep.get('time', outbound)} {dep.get('id', departure_id)} → {arr.get('time', '')} {arr.get('id', arrival_id)}",
                "amount_cents": amount_cents, "currency": "CNY", "terms": terms[:500], "includes_tax": True,
                "booking_url": f"https://www.google.com/travel/flights?q={parse.quote(f'{departure_id} to {arrival_id} flights {outbound}')}",
                "fetched_at": fetched_at, "expires_at": (fetched_time + timedelta(hours=3)).isoformat(), "mock": False,
                "details": {"segments": len(legs), "duration_minutes": duration, "airlines": airlines, "flight_numbers": numbers},
            })
        if not rows:
            raise RuntimeError("没有查到带价格的航班，请检查机场代码或日期")
        return sorted(rows, key=lambda row: row["amount_cents"])


class JuheTrainQuoteProvider(QuoteProvider):
    name = "juhe-train"
    endpoint = "https://apis.juhe.cn/fapigw/train/query"

    def __init__(self, api_key: str, timeout: int = 20, before_request: Callable[[str], None] | None = None):
        self.api_key, self.timeout = api_key, max(5, min(timeout, 60))
        self.before_request = before_request

    def search(self, query: dict) -> list[dict]:
        departure = str(query.get("origin") or query.get("departure") or "").strip()
        arrival = str(query.get("destination") or query.get("arrival") or "").strip()
        if not departure or not arrival:
            raise ValueError("请填写火车出发站和到达站")
        travel_date = _date_value(query.get("date") or query.get("start_date"), "乘车日期")
        query_day, today = date.fromisoformat(travel_date), datetime.now().astimezone().date()
        if query_day < today or query_day > today + timedelta(days=15):
            raise ValueError("火车票只能查询今天起 15 天内的日期")
        params = {"key": self.api_key, "search_type": "1", "departure_station": departure, "arrival_station": arrival, "date": travel_date, "filter": str(query.get("train_filter", ""))[:12], "enable_booking": "1", "departure_time_range": str(query.get("time_range", ""))[:8]}
        try:
            url = f"{self.endpoint}?{parse.urlencode(params)}"
            if self.before_request:
                self.before_request("train query")
            with request.urlopen(request.Request(url, headers={"Accept": "application/json"}), timeout=self.timeout, context=TRUSTED_SSL_CONTEXT) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError) as exc:
            raise RuntimeError("火车票查询暂时不可用") from exc
        try:
            error_code = int(result.get("error_code", -1)) if isinstance(result, dict) else -1
        except (TypeError, ValueError):
            error_code = -1
        if not isinstance(result, dict) or error_code != 0:
            reason = str(result.get("reason", "服务返回异常")) if isinstance(result, dict) else "服务返回异常"
            raise RuntimeError(f"火车票查询失败：{reason[:180]}")
        now, rows = _now(), []
        for train in result.get("result", [])[:50]:
            if not isinstance(train, dict) or not train.get("train_no"):
                continue
            seats = []
            for seat in train.get("prices", []):
                if not isinstance(seat, dict):
                    continue
                try:
                    price_cents = max(0, int(round(float(seat.get("price")) * 100)))
                except (TypeError, ValueError):
                    continue
                remaining = str(seat.get("num", "")).strip()[:20] or "未知"
                seats.append({"name": str(seat.get("seat_name", "席位"))[:40], "price_cents": price_cents, "remaining": remaining, "discount": seat.get("discount")})
            unavailable = {"无", "0", "--", "未知"}
            available = [seat for seat in seats if seat["remaining"] not in unavailable]
            priced = available or seats
            if not priced:
                continue
            lowest = min(priced, key=lambda seat: seat["price_cents"])
            seat_text = "；".join(f"{seat['name']} ¥{seat['price_cents'] / 100:g} 余{seat['remaining']}" for seat in seats[:8])
            flags = " / ".join(str(flag) for flag in train.get("train_flags", [])[:4])
            rows.append({
                "quote_type": "train", "provider": "聚合数据 · 12306 余票", "source_mode": "live",
                "title": f"{train['train_no']} · {train.get('departure_station', departure)} → {train.get('arrival_station', arrival)}",
                "subtitle": f"{train.get('departure_time', '--:--')} 出发 · {train.get('arrival_time', '--:--')} 到达 · 历时 {train.get('duration', '--')}",
                "amount_cents": lowest["price_cents"], "currency": "CNY", "terms": (seat_text + (f" · {flags}" if flags else ""))[:500],
                "includes_tax": True, "booking_url": "https://kyfw.12306.cn/otn/leftTicket/init", "fetched_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(), "mock": False,
                "details": {"train_no": train["train_no"], "bookable": train.get("enable_booking") == "Y", "seats": seats, "flags": train.get("train_flags", [])},
            })
        if not rows:
            raise RuntimeError("没有查到可展示的车次或票价")
        return rows


def quote_provider_from_env(quote_type: str | None = None, before_request: Callable[[str], None] | None = None) -> QuoteProvider:
    quote_type = str(quote_type or "hotel")
    endpoint, key = os.getenv("BOOKING_API_URL", "").strip(), os.getenv("BOOKING_API_KEY", "").strip()
    if endpoint and key:
        return AuthorizedHttpQuoteProvider(endpoint, key, before_request=before_request)
    if quote_type in {"hotel", "flight"} and os.getenv("SERPAPI_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}:
        key = os.getenv("SERPAPI_API_KEY", "").strip()
        if key:
            try:
                timeout, cache_ttl, monthly_limit = int(os.getenv("QUOTE_TIMEOUT_SECONDS", "25")), int(os.getenv("SERPAPI_CACHE_TTL_SECONDS", "21600")), int(os.getenv("SERPAPI_MONTHLY_LIMIT", "230"))
            except ValueError:
                timeout, cache_ttl, monthly_limit = 25, 21600, 230
            usage_path = Path(os.getenv("SERPAPI_USAGE_PATH", "data/serpapi-usage.json"))
            if not usage_path.is_absolute():
                usage_path = Path(__file__).resolve().parent.parent / usage_path
            # The store owns the persistent, concurrency-safe allowance. Keep
            # the legacy file counter only for directly constructed providers.
            return SerpApiQuoteProvider(key, timeout, cache_ttl, 0 if before_request else monthly_limit, usage_path, before_request)
    if quote_type == "train" and os.getenv("JUHE_TRAIN_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}:
        key = os.getenv("JUHE_TRAIN_API_KEY", "").strip()
        if key:
            try:
                timeout = int(os.getenv("QUOTE_TIMEOUT_SECONDS", "20"))
            except ValueError:
                timeout = 20
            return JuheTrainQuoteProvider(key, timeout, before_request)
    return MockQuoteProvider()
