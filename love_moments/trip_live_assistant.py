"""Deterministic live-trip recommendations built only from verified facts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .trip_live_context import LiveContext, classify_opening_hours, haversine_meters, wgs84_to_gcj02


ADVERSE_WEATHER_CODES = {
    45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77,
    80, 81, 82, 85, 86, 95, 96, 99,
}
INDOOR_KEYWORDS = (
    "博物馆", "美术馆", "展览", "剧院", "影院", "商场", "室内", "科技馆", "水族馆",
    "餐厅", "咖啡", "书店", "购物", "museum", "gallery", "theatre", "theater", "indoor",
)


def _active_variant(trip: dict) -> dict:
    variants = list(trip.get("variants") or [])
    active = str(trip.get("active_variant") or "")
    return next((variant for variant in variants if variant.get("style") == active), variants[0] if variants else {})


def _target_day(trip: dict, day_number: int) -> dict | None:
    return next(
        (day for day in _active_variant(trip).get("days", []) if int(day.get("day_number") or 0) == int(day_number)),
        None,
    )


def _day_number_for_date(trip: dict, value: date) -> int | None:
    for day in _active_variant(trip).get("days", []):
        if str(day.get("date") or "")[:10] == value.isoformat():
            return int(day.get("day_number") or 0)
    try:
        offset = (value - date.fromisoformat(str(trip.get("start_date"))[:10])).days + 1
        end = date.fromisoformat(str(trip.get("end_date"))[:10])
    except ValueError:
        return None
    return offset if 1 <= offset <= (end - date.fromisoformat(str(trip.get("start_date"))[:10])).days + 1 else None


def _minutes(value: str) -> int:
    hour, minute = str(value or "00:00")[:5].split(":")
    return int(hour) * 60 + int(minute)


def _clock(value: int) -> str:
    value = max(0, min(int(value), 1439))
    return f"{value // 60:02d}:{value % 60:02d}"


def _is_indoor(item: dict) -> bool:
    place = item.get("place") if isinstance(item.get("place"), dict) else {}
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    text = " ".join((str(item.get("title") or ""), str(item.get("type") or ""), str(place.get("category") or ""), str(details.get("environment") or ""))).lower()
    return any(keyword.lower() in text for keyword in INDOOR_KEYWORDS)


def _opening_hours(item: dict) -> str:
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    if details.get("opening_hours"):
        return str(details["opening_hours"])
    selected = next((row for row in item.get("food_recommendations", []) if row.get("selected_at")), None)
    return str((selected or {}).get("opening_hours") or "")


def _mode(distance: int) -> str:
    if distance <= 1_500:
        return "walking"
    if distance <= 8_000:
        return "transit"
    return "driving"


def build_live_recommendations(trip: dict, live_context: LiveContext, weather: dict | None) -> dict:
    day_number = _day_number_for_date(trip, live_context.local_time.date())
    if day_number is None:
        return {
            "current_day_number": None, "weather": weather, "weather_available": bool(weather),
            "candidates": [], "notice": "当前位置时间不在这次旅行日期内，可以仍在对话中指定第几天。",
        }
    day = _target_day(trip, day_number) or {}
    current_minutes = live_context.local_time.hour * 60 + live_context.local_time.minute
    adverse = int((weather or {}).get("weather_code", -1)) in ADVERSE_WEATHER_CODES
    candidates = []
    for item in day.get("items", []):
        if _minutes(str(item.get("start_time") or "00:00")) <= current_minutes:
            continue
        place = item.get("place") if isinstance(item.get("place"), dict) else None
        if not place or place.get("latitude") is None or place.get("longitude") is None:
            continue
        origin_latitude, origin_longitude = live_context.latitude, live_context.longitude
        if str(place.get("provider") or "").lower() == "amap":
            origin_latitude, origin_longitude = wgs84_to_gcj02(origin_latitude, origin_longitude)
        distance = haversine_meters(
            origin_latitude, origin_longitude,
            float(place["latitude"]), float(place["longitude"]),
        )
        indoor = _is_indoor(item)
        open_status = classify_opening_hours(_opening_hours(item), live_context.local_time)
        reasons = [f"距当前位置约 {distance} 米", "室内项目，雨天更稳妥" if adverse and indoor else "符合原行程顺序"]
        if open_status == "open":
            reasons.append("当前营业时间内")
        elif open_status == "unknown":
            reasons.append("营业时间待确认")
        else:
            reasons.append("按已知营业时间可能已打烊")
        candidates.append({
            "type": "next_stop", "item_id": item["id"], "title": item["title"],
            "start_time": item.get("start_time"), "distance_meters": distance,
            "recommended_mode": _mode(distance), "open_status": open_status,
            "indoor": indoor, "reason": "；".join(reasons),
            "place": {key: place.get(key) for key in ("provider", "provider_place_id", "name", "formatted_address", "latitude", "longitude", "category")},
        })
    candidates.sort(key=lambda row: (
        row["open_status"] == "closed",
        0 if adverse and row["indoor"] else 1,
        row["distance_meters"], row["start_time"],
    ))
    return {
        "current_day_number": day_number, "weather": weather, "weather_available": bool(weather),
        "candidates": candidates[:3],
        "notice": "只使用本次请求的位置；直线距离在本机计算，展开路线时才调用地图服务。",
    }


def build_day_replan_preview(
    trip: dict,
    day_number: int,
    local_time: datetime,
    delay_minutes: int = 0,
    weather: dict | None = None,
) -> dict:
    if not 1 <= int(day_number) <= 61:
        raise ValueError("旅行日期无效")
    if not 0 <= int(delay_minutes) <= 12 * 60:
        raise ValueError("延误时间需为 0–720 分钟")
    day = _target_day(trip, int(day_number))
    if not day:
        raise ValueError("没有找到要调整的旅行日期")
    current_day = str(day.get("date") or "")[:10] == local_time.date().isoformat()
    current_minutes = local_time.hour * 60 + local_time.minute
    editable = [
        item for item in day.get("items", [])
        if not bool(item.get("locked")) and (not current_day or _minutes(item.get("start_time")) > current_minutes)
    ]
    adverse = int((weather or {}).get("weather_code", -1)) in ADVERSE_WEATHER_CODES
    weather_affected = [item["id"] for item in editable if adverse and not _is_indoor(item)]
    ordered = sorted(editable, key=lambda item: (0 if adverse and _is_indoor(item) else 1, _minutes(item.get("start_time")), int(item.get("position") or 0)))
    available_slots = sorted((_minutes(item.get("start_time")) + int(delay_minutes), int(item.get("position") or 0)) for item in editable)
    proposals = []
    warnings: list[str] = []
    for item, (slot, position) in zip(ordered, available_slots):
        overflow = slot >= 24 * 60 or slot + int(item.get("duration_minutes") or 0) > 24 * 60
        if overflow:
            warnings.append(f"{item['title']} 调整后可能跨到次日，需要手动确认结束时间。")
        new_time = _clock(slot)
        before = {"start_time": item.get("start_time"), "position": int(item.get("position") or 0)}
        after = {"start_time": new_time, "position": position}
        proposals.append({
            "item_id": item["id"], "title": item["title"], "item_version": int(item.get("version") or 1),
            "duration_minutes": int(item.get("duration_minutes") or 0),
            "before": before, "after": after, "overflow": overflow,
        })

    # A replan must not silently move an activity on top of a locked booking,
    # an already-started item, or another proposed activity. Revert conflicting
    # moves to their original slot and report them for explicit user handling.
    proposal_by_id = {row["item_id"]: row for row in proposals}
    conflict_ids: set[str] = set()
    for _ in range(len(proposals) + 1):
        scheduled = []
        for item in day.get("items", []):
            proposal = proposal_by_id.get(item["id"])
            use_original = item["id"] in conflict_ids or proposal is None
            start = _minutes(item.get("start_time")) if use_original else _minutes(proposal["after"]["start_time"])
            scheduled.append({
                "item_id": item["id"], "start": start,
                "end": start + int(item.get("duration_minutes") or 0),
                "changed": bool(proposal and proposal["before"] != proposal["after"] and item["id"] not in conflict_ids),
            })
        scheduled.sort(key=lambda row: (row["start"], row["end"], row["item_id"]))
        newly_conflicted: set[str] = set()
        for index, left in enumerate(scheduled):
            for right in scheduled[index + 1:]:
                if right["start"] >= left["end"]:
                    break
                if left["changed"]:
                    newly_conflicted.add(left["item_id"])
                if right["changed"]:
                    newly_conflicted.add(right["item_id"])
        newly_conflicted -= conflict_ids
        if not newly_conflicted:
            break
        conflict_ids.update(newly_conflicted)
    conflicts = [
        {"item_id": row["item_id"], "title": row["title"], "reason": "调整后与已锁定、已开始或其他行程时间冲突"}
        for row in proposals if row["item_id"] in conflict_ids
    ]
    changes = [
        {key: value for key, value in row.items() if key != "duration_minutes"}
        for row in proposals if row["before"] != row["after"] and row["item_id"] not in conflict_ids
    ]
    if conflicts:
        warnings.append("部分项目调整后与预约或其他行程冲突，已保持原时间，请手动选择新的时段。")
    if weather_affected:
        warnings.insert(0, "雨雪天气下已把可编辑的室内项目提前；室外项目仍需结合真实营业与预约情况确认。")
    if not editable:
        warnings.append("当天没有尚未开始且可编辑的项目。")
    return {
        "trip_id": trip.get("id"), "trip_version": int(trip.get("version") or 0),
        "day_number": int(day_number), "day_id": day.get("id"), "delay_minutes": int(delay_minutes),
        "weather": weather, "weather_affected_item_ids": weather_affected,
        "changes": changes, "conflicts": conflicts, "warnings": list(dict.fromkeys(warnings)),
    }
