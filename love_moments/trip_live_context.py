"""Privacy-safe helpers for one-shot travel context requests."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any


MAX_LOCATION_AGE_SECONDS = 15 * 60
MAX_ACCURACY_METERS = 2_000.0
MAX_LOCAL_CLOCK_SKEW_SECONDS = 2 * 60


@dataclass(frozen=True)
class LiveContext:
    latitude: float
    longitude: float
    accuracy_meters: float
    observed_at: datetime
    local_time: datetime


def _datetime(value: Any, field: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field}格式无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field}必须包含时区")
    return parsed


def validate_live_context(payload: Any, now: datetime | None = None) -> LiveContext:
    if not isinstance(payload, dict):
        raise ValueError("现场位置参数无效")
    try:
        latitude = float(payload.get("latitude"))
        longitude = float(payload.get("longitude"))
        accuracy = float(payload.get("accuracy_meters"))
    except (TypeError, ValueError) as exc:
        raise ValueError("经纬度或定位精度无效") from exc
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("经纬度超出有效范围")
    if not math.isfinite(accuracy) or accuracy <= 0 or accuracy > MAX_ACCURACY_METERS:
        raise ValueError("定位精度不足，请到开阔位置后重试")
    observed = _datetime(payload.get("observed_at"), "定位时间")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=timezone.utc)
    if abs((current.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()) > MAX_LOCATION_AGE_SECONDS:
        raise ValueError("位置已过期，请重新获取当前位置")
    local = _datetime(payload.get("local_time") or payload.get("observed_at"), "本地时间")
    if abs((local.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()) > MAX_LOCAL_CLOCK_SKEW_SECONDS:
        raise ValueError("本地时间与定位时间不一致，请校准设备时间后重试")
    return LiveContext(latitude, longitude, accuracy, observed, local)


def _outside_china(latitude: float, longitude: float) -> bool:
    return not (0.8293 <= latitude <= 55.8271 and 72.004 <= longitude <= 137.8347)


def _transform_latitude(x: float, y: float) -> float:
    value = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    value += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    value += (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3
    return value + (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3


def _transform_longitude(x: float, y: float) -> float:
    value = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    value += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    value += (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3
    return value + (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3


def wgs84_to_gcj02(latitude: float, longitude: float) -> tuple[float, float]:
    """Convert browser WGS-84 coordinates for AMap inside mainland China."""
    latitude, longitude = float(latitude), float(longitude)
    if _outside_china(latitude, longitude):
        return latitude, longitude
    a, eccentricity = 6_378_245.0, 0.006693421622965943
    delta_lat = _transform_latitude(longitude - 105.0, latitude - 35.0)
    delta_lng = _transform_longitude(longitude - 105.0, latitude - 35.0)
    rad_lat = latitude / 180.0 * math.pi
    magic = 1 - eccentricity * math.sin(rad_lat) ** 2
    sqrt_magic = math.sqrt(magic)
    delta_lat = delta_lat * 180.0 / ((a * (1 - eccentricity)) / (magic * sqrt_magic) * math.pi)
    delta_lng = delta_lng * 180.0 / (a / sqrt_magic * math.cos(rad_lat) * math.pi)
    return latitude + delta_lat, longitude + delta_lng


def haversine_meters(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> int:
    radius = 6_371_000.0
    lat_a, lat_b = math.radians(float(latitude_a)), math.radians(float(latitude_b))
    delta_lat = math.radians(float(latitude_b) - float(latitude_a))
    delta_lng = math.radians(float(longitude_b) - float(longitude_a))
    value = math.sin(delta_lat / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lng / 2) ** 2
    return round(radius * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value))))


def _minutes(value: str) -> int:
    parsed = time.fromisoformat(value)
    return parsed.hour * 60 + parsed.minute


def classify_opening_hours(value: Any, local_time: datetime) -> str:
    """Return open/closed/unknown without guessing unstructured schedules."""
    text = str(value or "").strip().replace("：", ":")
    ranges = re.findall(r"((?:[01]\d|2[0-3]):[0-5]\d)\s*[-–—至]\s*((?:[01]\d|2[0-3]):[0-5]\d)", text)
    if not ranges:
        return "unknown"
    current = local_time.hour * 60 + local_time.minute
    for start_text, end_text in ranges:
        start, end = _minutes(start_text), _minutes(end_text)
        if start <= end and start <= current <= end:
            return "open"
        if start > end and (current >= start or current <= end):
            return "open"
    return "closed"
