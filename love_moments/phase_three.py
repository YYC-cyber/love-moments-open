"""Phase-three domain operations shared by the HTTP and native clients."""

from __future__ import annotations

import io
import hashlib
import json
import math
import re
import sqlite3
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .intelligent_ui import ConflictError
from .maps import MockMapProvider, geoapify_provider_from_env, map_provider_from_env
from .ocr import ManualTextProvider, ocr_provider_from_env
from .places import google_place_search_from_env, normalize_place_candidate
from .providers import physical_place_identity
from .quotes import quote_provider_from_env
from .quota import QuotaExceededError
from .restaurant_reviews import restaurant_review_search_from_env
from .schema import (
    ACCOUNT_EXPORT_POLICIES,
    ACCOUNT_EXPORT_POLICY_VERSION,
    account_export_policy_status,
)


FOOD_SPECIALTY_CATALOG = {
    "望仙谷": {
        "breakfast": ["铅山烫粉", "上饶米粉", "江西清汤"],
        "lunch": ["上饶土菜", "弋阳国道鱼", "余干辣椒炒肉"],
        "snack": ["灯盏粿", "清明粿", "江西米糕"],
        "dinner": ["广丰羊肉", "上饶土菜", "江西小炒"],
    },
    "上饶": {
        "breakfast": ["铅山烫粉", "上饶米粉", "江西清汤"],
        "lunch": ["上饶土菜", "弋阳国道鱼", "余干辣椒炒肉"],
        "snack": ["灯盏粿", "清明粿", "江西米糕"],
        "dinner": ["广丰羊肉", "上饶土菜", "江西小炒"],
    },
    "江西": {
        "breakfast": ["江西拌粉", "瓦罐汤", "清汤"],
        "lunch": ["江西小炒", "藜蒿炒腊肉", "余干辣椒炒肉"],
        "snack": ["灯盏粿", "清明粿", "江西米糕"],
        "dinner": ["江西菜", "瓦罐煨汤", "赣味烧烤"],
    },
    "云南": {
        "breakfast": ["过桥米线", "饵丝", "饵块"],
        "lunch": ["汽锅鸡", "野生菌", "云南菜"],
        "snack": ["鲜花饼", "乳扇", "云南甜品"],
        "dinner": ["野生菌火锅", "傣味", "云南烧烤"],
    },
    "成都": {
        "breakfast": ["肥肠粉", "钟水饺", "红油抄手"],
        "lunch": ["麻婆豆腐", "回锅肉", "川菜"],
        "snack": ["冰粉", "蛋烘糕", "糖油果子"],
        "dinner": ["成都火锅", "串串香", "川味烧烤"],
    },
    "四川": {
        "breakfast": ["肥肠粉", "红油抄手", "担担面"],
        "lunch": ["川菜", "麻婆豆腐", "回锅肉"],
        "snack": ["冰粉", "蛋烘糕", "糖油果子"],
        "dinner": ["四川火锅", "串串香", "川味烧烤"],
    },
    "杭州": {
        "breakfast": ["片儿川", "葱包桧", "杭州小笼"],
        "lunch": ["龙井虾仁", "东坡肉", "杭帮菜"],
        "snack": ["定胜糕", "藕粉", "桂花糕"],
        "dinner": ["杭帮菜", "西湖醋鱼", "宋嫂鱼羹"],
    },
    "三亚": {
        "breakfast": ["抱罗粉", "海南粉", "后安粉"],
        "lunch": ["文昌鸡", "海南菜", "糟粕醋"],
        "snack": ["清补凉", "椰子冻", "水果炒冰"],
        "dinner": ["三亚海鲜", "椰子鸡", "糟粕醋火锅"],
    },
    "海南": {
        "breakfast": ["海南粉", "抱罗粉", "后安粉"],
        "lunch": ["文昌鸡", "海南菜", "加积鸭"],
        "snack": ["清补凉", "椰子冻", "水果炒冰"],
        "dinner": ["海南海鲜", "椰子鸡", "糟粕醋火锅"],
    },
}

FOOD_PARENT_CITIES = {
    "望仙谷": "上饶市", "上饶": "上饶市", "昆明": "昆明市", "大理": "大理市",
    "丽江": "丽江市", "西双版纳": "西双版纳傣族自治州", "成都": "成都市",
    "杭州": "杭州市", "三亚": "三亚市", "海口": "海口市",
}

BROAD_DESTINATIONS = {
    "云南", "海南", "四川", "广东", "广西", "西藏", "新疆", "青海", "甘肃", "陕西",
    "贵州", "河北", "河南", "山东", "山西", "江苏", "浙江", "福建", "湖南", "湖北",
    "安徽", "江西", "辽宁", "吉林", "黑龙江",
}

REQUIRED_MEALS = (("breakfast", "08:00"), ("lunch", "12:00"), ("dinner", "18:30"))
CHINA_COUNTRY_NAMES = {"中国", "中华人民共和国", "china", "cn", "prc"}


def _meal_type(start_time: str) -> str:
    try:
        hour = int(str(start_time).split(":", 1)[0])
    except (TypeError, ValueError):
        hour = 12
    if hour < 10:
        return "breakfast"
    if hour < 14:
        return "lunch"
    if hour < 17:
        return "snack"
    return "dinner"


def _is_china_place(place: dict[str, Any] | None) -> bool:
    return bool(place) and str(place.get("country") or "").strip().lower() in CHINA_COUNTRY_NAMES


def _food_specialties(destination: str, meal_type: str) -> list[str]:
    for region, meals in FOOD_SPECIALTY_CATALOG.items():
        if region in destination or destination in region:
            return meals[meal_type]
    fallback = {
        "breakfast": [f"{destination}特色早餐", "当地早点"],
        "lunch": [f"{destination}本地菜", "当地特色午餐"],
        "snack": [f"{destination}传统小吃", "当地甜品"],
        "dinner": [f"{destination}本地菜", "当地特色晚餐"],
    }
    return fallback[meal_type]


def _food_search_city(destination: str, area: str = "") -> str:
    label = f"{area} {destination}"
    for keyword, city in FOOD_PARENT_CITIES.items():
        if keyword in label:
            return city
    return destination if destination not in BROAD_DESTINATIONS else ""


def _is_broad_destination(destination: str) -> bool:
    return destination in BROAD_DESTINATIONS or destination.endswith(("省", "自治区"))


def _confirmed_place_city(place: dict) -> str:
    return str(place.get("city") or place.get("district") or place.get("province") or "").strip()


def _confirmed_hotel_scope(place: dict, requested: object = "") -> str:
    """Build an unambiguous hotel query from the user-confirmed map entity."""
    target = str(requested or "").strip() or str(place.get("name") or "").strip()
    # The form normally contains the confirmed place name. A user may replace
    # it with a nearby district or hotel name; keep that intent while pinning it
    # to the confirmed country/city/district so a same-name place cannot jump
    # to another region.
    parts: list[str] = []
    for value in (place.get("country"), place.get("province"), place.get("city"), place.get("district")):
        text = str(value or "").strip()
        if text and text not in target and text not in parts:
            parts.append(text)
    if target:
        parts.append(target)
    return " ".join(parts)[:240]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def _iso(value: object, field: str, *, date_only: bool = False) -> str:
    text = str(value or "").strip()
    try:
        if date_only:
            return date.fromisoformat(text).isoformat()
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field}格式无效") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


class PhaseThreeMixin:
    """Methods mixed into ``LoveMomentsStore`` to keep the main store readable."""

    # ---- Trips, routes and quotes -------------------------------------

    def _map_provider_for_places(self, *places: dict | None):
        """Use AMap for China and Geoapify for confirmed overseas places."""
        countries = {str((place or {}).get("country") or "").strip().casefold() for place in places}
        sources = {str((place or {}).get("provider") or "").strip().casefold() for place in places}
        china_names = {"中国", "中国大陆", "china", "cn", "prc", "people's republic of china"}
        explicit_countries = {country for country in countries if country}
        overseas = any(country not in china_names for country in explicit_countries) or (
            not explicit_countries and bool(sources & {"geoapify", "google_maps"})
        )
        if overseas:
            provider = geoapify_provider_from_env(self.external_request_guard("geoapify"))
            if not provider.is_mock:
                return provider
        provider = map_provider_from_env(self.external_request_guard("amap"))
        if not provider.is_mock:
            return provider
        global_provider = geoapify_provider_from_env(self.external_request_guard("geoapify"))
        return global_provider if not global_provider.is_mock else provider

    def list_trips(self, user_id: str) -> list[dict]:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            rows = db.execute("SELECT id FROM trips WHERE couple_id=? AND deleted_at IS NULL ORDER BY start_date DESC, created_at DESC", (couple["id"],)).fetchall()
        return [self.get_trip(row["id"], user_id=user_id) for row in rows]

    def create_trip_generation_job(self, payload: dict, user_id: str, idempotency_key: object) -> dict:
        """Queue slow AI planning without holding the browser request open."""
        key = str(idempotency_key or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{16,120}", key):
            raise ValueError("旅行生成请求标识无效，请刷新后重试")
        if not isinstance(payload, dict):
            raise ValueError("旅行参数必须是对象")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 32_768:
            raise ValueError("旅行参数过长")
        payload_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        now = _now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            existing = db.execute(
                "SELECT * FROM trip_generation_jobs WHERE user_id=? AND idempotency_key=?",
                (user_id, key),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise ConflictError("这次提交标识已经用于另一份旅行，请重新打开创建窗口")
                return self._public_trip_generation_job(existing, user_id)
            job_id = _id("trip_job")
            db.execute(
                """INSERT INTO trip_generation_jobs(
                    id,couple_id,user_id,idempotency_key,payload_hash,payload_json,status,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,'queued',?,?)""",
                (job_id, couple["id"], user_id, key, payload_hash, encoded, now, now),
            )
            row = db.execute("SELECT * FROM trip_generation_jobs WHERE id=?", (job_id,)).fetchone()
        return self._public_trip_generation_job(row, user_id)

    def _public_trip_generation_job(self, row: sqlite3.Row | dict, user_id: str) -> dict:
        value = dict(row)
        result = {key: value.get(key) for key in (
            "id", "status", "trip_id", "error_message", "created_at", "started_at", "completed_at", "updated_at",
        )}
        if result["status"] == "completed" and result["trip_id"]:
            try:
                result["trip"] = self.get_trip(str(result["trip_id"]), user_id=user_id)
            except KeyError:
                result["status"] = "failed"
                result["error_message"] = "旅行结果已经不存在"
        return result

    def get_trip_generation_job(self, job_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT * FROM trip_generation_jobs WHERE id=? AND couple_id=? AND user_id=?",
                (job_id, couple["id"], user_id),
            ).fetchone()
        if not row:
            raise KeyError("旅行生成任务不存在")
        return self._public_trip_generation_job(row, user_id)

    def recover_trip_generation_jobs(self) -> dict:
        """Requeue a job interrupted by a server restart; creation remains idempotent."""
        now = _now()
        with self.connection() as db:
            recovered = db.execute(
                "UPDATE trip_generation_jobs SET status='queued',started_at=NULL,updated_at=? WHERE status='running'",
                (now,),
            ).rowcount
        return {"recovered": recovered}

    def claim_trip_generation_job(self) -> dict | None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM trip_generation_jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            now = _now()
            changed = db.execute(
                "UPDATE trip_generation_jobs SET status='running',started_at=?,updated_at=? WHERE id=? AND status='queued'",
                (now, now, row["id"]),
            ).rowcount
            if not changed:
                return None
            value = dict(row)
            value["status"] = "running"
            value["started_at"] = now
        value["payload"] = _json(value.pop("payload_json"), {})
        return value

    def run_trip_generation_job(self, job: dict) -> dict:
        job_id = str(job["id"])
        try:
            trip = self.create_trip(job.get("payload") or {}, str(job["user_id"]), creation_job_id=job_id)
            now = _now()
            with self.connection() as db:
                db.execute(
                    "UPDATE trip_generation_jobs SET status='completed',trip_id=?,error_message=NULL,completed_at=?,updated_at=? WHERE id=?",
                    (trip["id"], now, now, job_id),
                )
            return {"id": job_id, "status": "completed", "trip_id": trip["id"]}
        except Exception as error:
            message = str(error).strip()[:500] or "旅行生成失败"
            now = _now()
            with self.connection() as db:
                db.execute(
                    "UPDATE trip_generation_jobs SET status='failed',error_message=?,completed_at=?,updated_at=? WHERE id=?",
                    (message, now, now, job_id),
                )
            return {"id": job_id, "status": "failed", "error_message": message}

    def search_destination_places(self, payload: dict, user_id: str) -> dict:
        """Return explicit candidates; never silently choose a same-name place."""
        query = str(payload.get("query", "")).strip()[:120]
        region_hint = str(payload.get("region_hint", "")).strip()[:120]
        if len(query) < 2:
            raise ValueError("请至少输入两个字的目的地名称")
        include_global = bool(payload.get("include_global", True))
        with self.connection() as db:
            self._couple_context(db, user_id)

        candidates: list[dict] = []
        notices: list[str] = []
        amap = map_provider_from_env(self.external_request_guard("amap"))
        if not amap.is_mock:
            try:
                candidates.extend(normalize_place_candidate(row) for row in amap.search_places(query, region_hint, 8))
            except (RuntimeError, QuotaExceededError) as exc:
                notices.append(str(exc))
        if include_global:
            geoapify = geoapify_provider_from_env(self.external_request_guard("geoapify"))
            if not geoapify.is_mock:
                try:
                    candidates.extend(normalize_place_candidate(row) for row in geoapify.search_places(query, region_hint, 8))
                except (RuntimeError, QuotaExceededError) as exc:
                    notices.append(str(exc))
            else:
                global_search = google_place_search_from_env(self.external_request_guard("serpapi"))
                if global_search.is_configured:
                    try:
                        candidates.extend(normalize_place_candidate(row) for row in global_search.search(query, region_hint, 8))
                    except (RuntimeError, QuotaExceededError) as exc:
                        notices.append(str(exc))

        # A manual scope is honest only when the user supplied a separate
        # country/city hint. It carries no invented coordinate or map ID.
        if region_hint:
            manual_candidate = normalize_place_candidate({
                "provider": "manual", "name": query,
                "formatted_address": f"用户确认范围：{region_hint}",
                "city": region_hint, "category": "用户确认的旅行范围",
            })
            foreign_markers = ("法国", "日本", "美国", "英国", "德国", "意大利", "澳大利亚", "加拿大", "新加坡", "韩国", "泰国")
            if re.search(r"(?:省|市|自治区|特别行政区|区|县)$", region_hint) and not any(marker in region_hint for marker in foreign_markers):
                manual_candidate["timezone"] = "Asia/Shanghai"
            candidates.append(manual_candidate)

        now = _now()
        unique, seen, semantic_seen = [], set(), set()
        for candidate in candidates:
            manual = candidate.get("provider") == "manual"
            candidate["source_type"] = str(candidate.get("source_type") or ("human" if manual else "real_api"))
            candidate["scope_confirmed"] = bool(candidate.get("scope_confirmed", not manual))
            candidate["scope_status"] = "confirmed" if candidate["scope_confirmed"] else "unconfirmed"
            candidate["evidence_at"] = str(candidate.get("evidence_at") or now)
            key = candidate["provider_place_id"] or (
                f"{candidate['provider']}:{candidate['name']}:{candidate['formatted_address']}:"
                f"{candidate['latitude']}:{candidate['longitude']}"
            )
            semantic_key = ":".join(str(candidate.get(field) or "").strip().casefold() for field in (
                "provider", "name", "city", "district", "formatted_address",
            ))
            if key in seen or semantic_key in semantic_seen:
                continue
            seen.add(key)
            semantic_seen.add(semantic_key)
            unique.append(candidate)

        expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0).isoformat()
        items = []
        with self.connection() as db:
            db.execute("DELETE FROM place_search_candidates WHERE expires_at<?", (now,))
            for candidate in unique[:16]:
                candidate_id = _id("place")
                db.execute(
                    "INSERT INTO place_search_candidates(id,user_id,query,region_hint,payload_json,expires_at,created_at) VALUES (?,?,?,?,?,?,?)",
                    (candidate_id, user_id, query, region_hint, json.dumps(candidate, ensure_ascii=False), expires, now),
                )
                items.append({
                    "candidate_id": candidate_id,
                    "created_at": now,
                    "cache_hit": candidate.get("source_type") == "cache",
                    **candidate,
                })
        configured = not amap.is_mock or (
            include_global and (
                not geoapify_provider_from_env().is_mock
                or google_place_search_from_env().is_configured
            )
        )
        notice = "；".join(notices[:2])
        if not items:
            notice = notice or "没有查到可确认地点，请补充国家、城市或区县后重试"
        elif not configured:
            notice = "地图接口未启用，当前仅可保存你亲自填写并确认的城市范围"
        return {"items": items, "query": query, "region_hint": region_hint, "configured": configured, "notice": notice, "expires_at": expires}

    def _destination_candidate(self, candidate_id: object, user_id: str) -> dict | None:
        value = str(candidate_id or "").strip()
        if not value:
            return None
        with self.connection() as db:
            row = db.execute(
                "SELECT payload_json,expires_at,created_at FROM place_search_candidates WHERE id=? AND user_id=?",
                (value, user_id),
            ).fetchone()
        if not row or row["expires_at"] < _now():
            raise ValueError("地点候选已失效，请重新搜索并确认")
        try:
            raw = json.loads(row["payload_json"])
            value = normalize_place_candidate(raw)
            source_type = str(raw.get("source_type") or ("human" if value.get("provider") == "manual" else "real_api"))
            value.update({
                "source_type": source_type,
                "original_source_type": str(raw.get("original_source_type") or source_type),
                "evidence_at": str(raw.get("evidence_at") or row["created_at"]),
                "created_at": str(row["created_at"]),
                "cache_hit": source_type == "cache",
                "scope_confirmed": bool(raw.get("scope_confirmed", value.get("provider") != "manual")),
                "scope_status": str(raw.get("scope_status") or ("unconfirmed" if value.get("provider") == "manual" else "confirmed")),
            })
            if raw.get("timezone"):
                value["timezone"] = str(raw["timezone"])
            if isinstance(raw.get("activity_tags"), list):
                value["activity_tags"] = [
                    str(tag)[:40] for tag in raw["activity_tags"] if str(tag).strip()
                ][:4]
            if source_type == "cache" and raw.get("cache_age_seconds") is not None:
                value["cache_age_seconds"] = max(0, int(raw["cache_age_seconds"]))
            return value
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("地点候选数据无效，请重新搜索") from exc

    def _destination_candidate_pool(
        self,
        candidate_id: object,
        user_id: str,
        limit: int = 6,
        interest_tags: list[str] | None = None,
    ) -> list[dict]:
        """Return a bounded, still-valid pool scoped to the confirmed admin area.

        This first reuses candidates already fetched for the user's explicit
        search.  When the only compatible row is the confirmed anchor, it may
        make one bounded category search so a plan is not padded with the same
        place.  It never chooses a same-name destination result on the user's
        behalf.
        """
        anchor = self._destination_candidate(candidate_id, user_id)
        if anchor is None:
            return []
        anchor_id = str(candidate_id)
        anchor_city = str(anchor.get("city") or "").strip().casefold()
        anchor_province = str(anchor.get("province") or "").strip().casefold()
        anchor_country = str(anchor.get("country") or "").strip().casefold()
        anchor_category = str(anchor.get("category") or "").strip().casefold()
        activity_specs = {
            "美食": ("餐厅", ("餐饮", "餐厅", "美食", "咖啡", "甜品")),
            "展览": ("博物馆 美术馆", ("文化", "博物", "美术", "展览", "艺术")),
            "自然": ("公园 景区", ("风景", "公园", "自然", "景区")),
            "散步": ("公园 步行街", ("公园", "步行", "街区", "风景")),
            "摄影": ("摄影 景点", ("风景", "景区", "摄影", "公园")),
            "电影": ("电影院", ("电影院", "电影")),
            "运动": ("运动场馆", ("体育", "运动", "健身")),
            "手作": ("手作 文化体验", ("文化", "手作", "工艺", "艺术")),
        }
        requested_specs = [
            (tag, *activity_specs[tag]) for tag in (interest_tags or []) if tag in activity_specs
        ][:2]
        with self.connection() as db:
            rows = db.execute(
                "SELECT id FROM place_search_candidates WHERE user_id=? AND expires_at>=? ORDER BY created_at DESC LIMIT 48",
                (user_id, _now()),
            ).fetchall()
        def eligible(item: dict) -> bool:
            return (
                item.get("latitude") is not None and item.get("longitude") is not None
                and str(item.get("source_type") or "") in {"real_api", "cache"}
                and item.get("provider") != "manual"
            )

        def same_area(item: dict) -> bool:
            city = str(item.get("city") or "").strip().casefold()
            province = str(item.get("province") or "").strip().casefold()
            country = str(item.get("country") or "").strip().casefold()
            return (
                bool(anchor_city and city and city == anchor_city)
                or bool(not anchor_city and anchor_province and province == anchor_province)
                or bool(not anchor_city and not anchor_province and anchor_country and country == anchor_country)
            )

        def matches_activity(item: dict) -> bool:
            category = str(item.get("category") or "").strip().casefold()
            activity_tags = {str(tag).strip() for tag in item.get("activity_tags", []) if str(tag).strip()}
            if requested_specs:
                return any(
                    tag in activity_tags or any(term.casefold() in category for term in terms)
                    for tag, _query, terms in requested_specs
                )
            return bool(anchor_category and category and category == anchor_category)

        result = [{"candidate_id": anchor_id, **anchor}] if eligible(anchor) and matches_activity(anchor) else []
        seen_places = {physical_place_identity(item) for item in result}
        for row in rows:
            value_id = str(row["id"])
            if value_id == anchor_id:
                continue
            try:
                value = self._destination_candidate(value_id, user_id)
            except ValueError:
                continue
            if value is None or not eligible(value):
                continue
            if not same_area(value) or not matches_activity(value):
                continue
            identity = physical_place_identity(value)
            if identity in seen_places:
                continue
            result.append({"candidate_id": value_id, **value})
            seen_places.add(identity)
            if len(result) >= max(1, min(limit, 8)):
                break

        # A category query is performed at most once per requested activity
        # (and at most twice overall), never once per generated variant. The
        # results remain server-owned candidates and are filtered back to the
        # confirmed administrative scope before the model can reference them.
        max_items = max(1, min(limit, 8))
        only_anchor = len(result) == 1 and str(result[0].get("candidate_id") or "") == anchor_id
        can_search = (
            (not result or only_anchor) and requested_specs and anchor.get("provider") != "manual"
            and anchor.get("scope_confirmed", True)
        )
        if can_search:
            provider_name = str(anchor.get("provider") or "")
            search_provider: object | None = None
            search_method = "search_places"
            if provider_name == "amap":
                value = map_provider_from_env(self.external_request_guard("amap"))
                search_provider = None if value.is_mock else value
            elif provider_name == "geoapify":
                value = geoapify_provider_from_env(self.external_request_guard("geoapify"))
                search_provider = None if value.is_mock else value
            elif provider_name == "google_maps":
                value = google_place_search_from_env(self.external_request_guard("serpapi"))
                search_provider = value if value.is_configured else None
                search_method = "search"
            region_hint = str(anchor.get("city") or anchor.get("province") or anchor.get("country") or "")
            fetched: list[dict] = []
            if search_provider is not None and region_hint:
                for tag, query, _terms in requested_specs:
                    try:
                        method = getattr(search_provider, search_method)
                        rows_found = method(query, region_hint, max_items)
                    except (RuntimeError, QuotaExceededError, TimeoutError, ValueError):
                        continue
                    for raw in rows_found:
                        item = normalize_place_candidate(raw)
                        item.update({
                            "source_type": "real_api", "scope_confirmed": True,
                            "scope_status": "confirmed", "activity_tags": [tag],
                            "evidence_at": _now(),
                        })
                        key = physical_place_identity(item)
                        if key in seen_places or not eligible(item) or not same_area(item) or not matches_activity(item):
                            continue
                        seen_places.add(key)
                        fetched.append(item)
                        if len(result) + len(fetched) >= max_items:
                            break
                    if len(result) + len(fetched) >= max_items:
                        break
            if fetched:
                created_at = _now()
                expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0).isoformat()
                with self.connection() as db:
                    for item in fetched:
                        value_id = _id("place")
                        db.execute(
                            "INSERT INTO place_search_candidates(id,user_id,query,region_hint,payload_json,expires_at,created_at) VALUES (?,?,?,?,?,?,?)",
                            (value_id, user_id, "约会活动", region_hint, json.dumps(item, ensure_ascii=False), expires_at, created_at),
                        )
                        result.append({"candidate_id": value_id, "created_at": created_at, "cache_hit": False, **item})
        return result

    @staticmethod
    def _save_destination_place(db: sqlite3.Connection, trip_id: str, place: dict, user_id: str, now: str) -> None:
        db.execute(
            """INSERT INTO trip_destination_places(
                trip_id,provider,provider_place_id,name,formatted_address,country,province,city,district,
                latitude,longitude,category,confirmed_by,confirmed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trip_id) DO UPDATE SET
                provider=excluded.provider,provider_place_id=excluded.provider_place_id,name=excluded.name,
                formatted_address=excluded.formatted_address,country=excluded.country,province=excluded.province,
                city=excluded.city,district=excluded.district,latitude=excluded.latitude,longitude=excluded.longitude,
                category=excluded.category,confirmed_by=excluded.confirmed_by,confirmed_at=excluded.confirmed_at""",
            (trip_id, place["provider"], place["provider_place_id"], place["name"], place["formatted_address"],
             place["country"], place["province"], place["city"], place["district"], place["latitude"],
             place["longitude"], place["category"], user_id, now),
        )

    def create_trip(self, payload: dict, user_id: str, creation_job_id: str | None = None) -> dict:
        destination = str(payload.get("destination", "")).strip()
        if not destination or len(destination) > 120:
            raise ValueError("目的地长度需为 1–120 位")
        start_date = _iso(payload.get("start_date"), "出发日期", date_only=True)
        end_date = _iso(payload.get("end_date"), "返程日期", date_only=True)
        if end_date < start_date:
            raise ValueError("返程日期不能早于出发日期")
        if (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days > 60:
            raise ValueError("单次旅行最长为 61 天")
        try:
            budget_cents = int(round(float(payload.get("budget", 0)) * 100)) if "budget_cents" not in payload else int(payload["budget_cents"])
        except (TypeError, ValueError) as exc:
            raise ValueError("预算必须是数字") from exc
        if not 0 <= budget_cents <= 100_000_000:
            raise ValueError("旅行预算超出范围")
        destination_place = self._destination_candidate(payload.get("destination_candidate_id"), user_id)
        if destination_place:
            destination = destination_place["name"]
        title = str(payload.get("title") or f"{destination} · {max(1, (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1)}日")[:120]
        departure_city = str(payload.get("departure_city", "")).strip()[:80]
        arrival_city = str(payload.get("arrival_city", "")).strip()[:80]
        if destination_place:
            arrival_city = str(destination_place.get("city") or destination_place.get("district") or destination_place.get("province") or arrival_city).strip()[:80]
        # A province-level destination is valid as an early inspiration, but it
        # is not precise enough for tickets, routes or footprints. Keep the
        # arrival city empty so later screens can ask for a real hub instead of
        # silently treating a whole province as a station or airport.
        if not arrival_city and not _is_broad_destination(destination):
            arrival_city = destination
        try:
            travelers = int(payload.get("travelers", 2))
            rooms = int(payload.get("rooms", 1))
            hotel_budget_cents = int(round(float(payload.get("hotel_budget", 0)) * 100))
        except (TypeError, ValueError) as exc:
            raise ValueError("人数、房间数和酒店预算必须是数字") from exc
        if not 1 <= travelers <= 20 or not 1 <= rooms <= 10:
            raise ValueError("出行人数需为 1–20，房间数需为 1–10")
        if not 0 <= hotel_budget_cents <= budget_cents:
            raise ValueError("酒店预算不能超过旅行总预算")
        outbound_time = _iso(payload.get("outbound_time"), "去程时间") if payload.get("outbound_time") else None
        return_time = _iso(payload.get("return_time"), "返程时间") if payload.get("return_time") else None
        if outbound_time and not outbound_time.startswith(start_date):
            raise ValueError("去程时间必须在出发日期当天")
        if return_time and not return_time.startswith(end_date):
            raise ValueError("返程时间必须在返程日期当天")
        preference = payload.get("preferences") or {}
        if not isinstance(preference, dict):
            raise ValueError("旅行偏好必须是对象")
        # Validate membership before any potentially paid model request. The
        # network call remains outside the write transaction so it cannot lock
        # the shared SQLite database while waiting for a provider.
        with self.connection() as db:
            initial_couple, initial_partner_id = self._couple_context(db, user_id)
            if creation_job_id:
                existing = db.execute(
                    "SELECT id FROM trips WHERE creation_job_id=? AND couple_id=?",
                    (creation_job_id, initial_couple["id"]),
                ).fetchone()
                if existing:
                    return self.get_trip(existing["id"], user_id=user_id)
        planning_context = {
            "start_date": start_date,
            "end_date": end_date,
            "departure_city": departure_city,
            "arrival_city": arrival_city,
            "outbound_time": outbound_time,
            "return_time": return_time,
            "travelers": travelers,
            "rooms": rooms,
            "hotel_budget_cents": hotel_budget_cents,
            "preferences": preference,
            "partner_preferences": payload.get("partner_preferences") if isinstance(payload.get("partner_preferences"), dict) else {},
            "destination_place": destination_place or {},
        }
        plans = self.planner.generate_variants(destination, budget_cents, planning_context)
        if len(plans) < 5:
            raise ValueError("旅行规划器必须生成至少五套方案")
        trip_id, now = _id("trip"), _now()
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            if couple["id"] != initial_couple["id"] or partner_id != initial_partner_id:
                raise ConflictError("情侣空间已发生变化，请重新创建旅行")
            db.execute(
                "INSERT INTO trips(id,couple_id,title,destination,start_date,end_date,budget_cents,currency,status,active_variant,departure_city,arrival_city,outbound_time,return_time,travelers,rooms,hotel_budget_cents,creation_job_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'CNY','planning','slow',?,?,?,?,?,?,?,?, ?,?)",
                (trip_id, couple["id"], title, destination, start_date, end_date, budget_cents, departure_city, arrival_city, outbound_time, return_time, travelers, rooms, hotel_budget_cents, creation_job_id, now, now),
            )
            if destination_place:
                self._save_destination_place(db, trip_id, destination_place, user_id, now)
            values = (
                str(preference.get("pace", "适中"))[:40],
                str(preference.get("interests", ""))[:500],
                str(preference.get("transport", "公共交通"))[:200],
                str(preference.get("diet", ""))[:500],
                str(preference.get("hotel", ""))[:500],
                str(preference.get("must_visits", ""))[:1000],
            )
            db.execute("INSERT INTO trip_preferences(trip_id,user_id,pace,interests,transport,diet,hotel,must_visits) VALUES (?,?,?,?,?,?,?,?)", (trip_id, user_id, *values))
            partner_pref = payload.get("partner_preferences")
            if isinstance(partner_pref, dict):
                partner_values = tuple(str(partner_pref.get(key, ""))[:limit] for key, limit in (("pace", 40), ("interests", 500), ("transport", 200), ("diet", 500), ("hotel", 500), ("must_visits", 1000)))
                db.execute("INSERT INTO trip_preferences(trip_id,user_id,pace,interests,transport,diet,hotel,must_visits) VALUES (?,?,?,?,?,?,?,?)", (trip_id, partner_id, *partner_values))
            self._insert_generated_plans(db, trip_id, destination, budget_cents, plans)
            self._audit(db, "trip.created", "trip", trip_id, {"destination": destination}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "trip.created", trip_id, {"destination": destination})
        return self.get_trip(trip_id, user_id=user_id)

    def delete_trip(self, trip_id: str, expected_version: object, user_id: str) -> dict:
        """Hide a shared trip for both partners without destroying its history."""
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT id,title,version FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL",
                (trip_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("旅行不存在或已经删除")
            if expected_version is None:
                raise ValueError("删除旅行需要当前版本，请刷新后重试")
            if int(expected_version) != int(row["version"]):
                raise ConflictError("旅行已经被另一方修改，请刷新后再删除")
            now = _now()
            db.execute(
                "UPDATE trips SET deleted_at=?,deleted_by=?,version=version+1,updated_at=? WHERE id=?",
                (now, user_id, now, trip_id),
            )
            db.execute("UPDATE couples SET version=version+1 WHERE id=?", (couple["id"],))
            permission_version = int(db.execute(
                "SELECT version FROM couples WHERE id=?", (couple["id"],),
            ).fetchone()[0])
            db.execute("UPDATE ui_action_drafts SET status='cancelled' WHERE couple_id=? AND status='draft' AND payload_json LIKE ?", (couple["id"], f'%"trip_id": "{trip_id}"%'))
            self._audit(db, "trip.deleted", "trip", trip_id, {"title": row["title"]}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", trip_id, {"resource": "trip", "operation": "deleted"})
        return {
            "id": trip_id, "title": row["title"], "deleted": True, "deleted_at": now,
            "version": int(row["version"]) + 1, "permission_version": permission_version,
        }

    def restore_trip(self, trip_id: str, expected_version: object, user_id: str) -> dict:
        """Restore a shared trip that either partner deleted by mistake."""
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT id,title,version FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NOT NULL",
                (trip_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("旅行不存在或已经恢复")
            if expected_version is None:
                raise ValueError("恢复旅行需要删除后的版本，请刷新后重试")
            if int(expected_version) != int(row["version"]):
                raise ConflictError("旅行删除状态已经变化，请刷新后重试")
            now = _now()
            db.execute(
                "UPDATE trips SET deleted_at=NULL,deleted_by=NULL,version=version+1,updated_at=? WHERE id=?",
                (now, trip_id),
            )
            db.execute("UPDATE couples SET version=version+1 WHERE id=?", (couple["id"],))
            permission_version = int(db.execute(
                "SELECT version FROM couples WHERE id=?", (couple["id"],),
            ).fetchone()[0])
            self._audit(db, "trip.restored", "trip", trip_id, {"title": row["title"]}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", trip_id, {"resource": "trip", "operation": "restored"})
        result = self.get_trip(trip_id, user_id)
        result["permission_version"] = permission_version
        return result

    def update_trip_logistics(self, trip_id: str, payload: dict, user_id: str) -> dict:
        departure_city = str(payload.get("departure_city", "")).strip()[:80]
        arrival_city = str(payload.get("arrival_city", "")).strip()[:80]
        destination_place = self._destination_candidate(payload.get("destination_candidate_id"), user_id)
        if destination_place:
            arrival_city = str(destination_place.get("city") or destination_place.get("district") or destination_place.get("province") or arrival_city).strip()[:80]
        try:
            travelers = int(payload.get("travelers", 2))
            rooms = int(payload.get("rooms", 1))
            hotel_budget_cents = int(round(float(payload.get("hotel_budget", 0)) * 100))
        except (TypeError, ValueError) as exc:
            raise ValueError("人数、房间数和酒店预算必须是数字") from exc
        if not 1 <= travelers <= 20 or not 1 <= rooms <= 10:
            raise ValueError("出行人数需为 1–20，房间数需为 1–10")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL", (trip_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("旅行不存在")
            if payload.get("expected_version") is None or int(payload["expected_version"]) != int(row["version"]):
                raise ConflictError("旅行已经被另一方修改，请刷新后重试")
            if not departure_city or not arrival_city:
                raise ValueError("出发城市和到达城市都不能为空")
            if _is_broad_destination(arrival_city):
                raise ValueError("到达地请填写具体城市、景区或交通枢纽，不能填写整个省份")
            if not 0 <= hotel_budget_cents <= int(row["budget_cents"]):
                raise ValueError("酒店预算不能超过旅行总预算")
            now = _now()
            destination = destination_place["name"] if destination_place else str(row["destination"])
            title = str(row["title"])
            if destination_place and title.startswith(f"{row['destination']} ·"):
                title = destination + title[len(str(row["destination"])):]
            db.execute(
                "UPDATE trips SET title=?,destination=?,departure_city=?,arrival_city=?,travelers=?,rooms=?,hotel_budget_cents=?,version=version+1,updated_at=? WHERE id=?",
                (title, destination, departure_city, arrival_city, travelers, rooms, hotel_budget_cents, now, trip_id),
            )
            if destination_place:
                self._save_destination_place(db, trip_id, destination_place, user_id, now)
            self._audit(db, "trip.logistics_updated", "trip", trip_id, {"destination": destination, "departure_city": departure_city, "arrival_city": arrival_city, "destination_resolved": bool(destination_place)}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", trip_id, {"resource": "trip", "operation": "updated"})
        return self.get_trip(trip_id, user_id)

    def trip_routes(self, trip_id: str, payload: dict, user_id: str) -> dict:
        trip = self.get_trip(trip_id, user_id=user_id)
        mode = str(payload.get("mode", "walking"))
        if mode not in {"walking", "driving", "bicycling", "transit"}:
            raise ValueError("不支持的出行方式")
        place = trip.get("destination_place")
        if not place:
            raise ValueError("请先从地图候选中确认具体目的地，再查询路线")
        origin = str(payload.get("origin", "")).strip()
        destination = str(payload.get("destination", "")).strip()
        if not origin or not destination:
            active = next((item for item in trip["variants"] if item["style"] == trip.get("active_variant")), trip["variants"][0] if trip["variants"] else None)
            items = [item for day in (active.get("days", []) if active else []) for item in day.get("items", [])]
            if len(items) < 2:
                raise ValueError("行程中至少需要两个地点")
            origin, destination = items[0]["title"], items[1]["title"]
        city_scope = _confirmed_place_city(place)
        provider = self._map_provider_for_places(place)
        try:
            route = provider.route(origin, destination, mode, city_scope)
            degraded = False
        except QuotaExceededError:
            raise
        except RuntimeError:
            if provider.is_mock:
                raise
            route = MockMapProvider().route(origin, destination, mode, trip["destination"])
            degraded = True
            route["notice"] = "地图路线暂时不可用，当前显示模拟路线"
        return {
            "trip_id": trip_id, **route, "configured": not provider.is_mock, "degraded": degraded,
            "search_scope": _confirmed_hotel_scope(place, place.get("name")),
        }

    @staticmethod
    def _route_distance(origin: dict, destination: dict) -> int:
        radius = 6_371_000
        lat1, lat2 = math.radians(float(origin["latitude"])), math.radians(float(destination["latitude"]))
        delta_lat = math.radians(float(destination["latitude"]) - float(origin["latitude"]))
        delta_lng = math.radians(float(destination["longitude"]) - float(origin["longitude"]))
        value = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
        return int(round(radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))))

    @staticmethod
    def _route_recommendations(distance_meters: int) -> list[str]:
        if distance_meters <= 1_500:
            return ["walking", "bicycling"]
        if distance_meters <= 8_000:
            return ["transit", "taxi"]
        return ["transit", "driving"]

    @staticmethod
    def _route_cache_key(provider: str, origin: dict, destination: dict, mode: str) -> str:
        raw = ":".join((
            provider, mode,
            str(origin.get("provider_place_id") or ""), str(destination.get("provider_place_id") or ""),
            f"{float(origin['latitude']):.6f}", f"{float(origin['longitude']):.6f}",
            f"{float(destination['latitude']):.6f}", f"{float(destination['longitude']):.6f}",
        ))
        return "route_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _public_route_place(row: sqlite3.Row | dict | None) -> dict | None:
        if not row:
            return None
        value = dict(row)
        return {key: value.get(key) for key in (
            "provider", "provider_place_id", "name", "formatted_address", "country", "province",
            "city", "district", "latitude", "longitude", "category",
        )}

    def _itinerary_place(self, db: sqlite3.Connection, item_id: str) -> dict | None:
        row = db.execute("SELECT * FROM itinerary_item_places WHERE itinerary_item_id=?", (item_id,)).fetchone()
        if row:
            return self._public_route_place(row)
        # Restaurant choices already contain a real AMap POI and coordinates.
        # Treat them as route-ready without spending another place-search call.
        restaurant = db.execute(
            "SELECT name,address AS formatted_address,district,latitude,longitude,category,amap_poi_id AS provider_place_id "
            "FROM trip_food_recommendations WHERE itinerary_item_id=? AND selected_at IS NOT NULL "
            "AND latitude IS NOT NULL AND longitude IS NOT NULL ORDER BY selected_at DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        if not restaurant:
            return None
        value = dict(restaurant)
        return {
            "provider": "amap", "provider_place_id": value["provider_place_id"], "name": value["name"],
            "formatted_address": value["formatted_address"], "country": "中国", "province": "", "city": "",
            "district": value["district"], "latitude": value["latitude"], "longitude": value["longitude"],
            "category": value["category"],
        }

    def _build_route_legs(self, db: sqlite3.Connection, trip: sqlite3.Row) -> list[dict]:
        rows = db.execute(
            "SELECT i.id,i.title,i.type,i.start_time,i.position,d.id AS day_id,d.day_number,d.date,d.title AS day_title "
            "FROM itinerary_items i JOIN trip_days d ON d.id=i.day_id "
            "JOIN plan_variants v ON v.id=d.variant_id WHERE v.trip_id=? AND v.style=? "
            "ORDER BY d.position,i.position",
            (trip["id"], trip["active_variant"]),
        ).fetchall()
        by_day: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_day.setdefault(row["day_id"], []).append(row)
        hotel_place = None
        hotel_title = "已选住宿"
        hotel_quote = db.execute(
            "SELECT title,details_json FROM quote_snapshots WHERE trip_id=? AND quote_type='hotel' "
            "AND selected_at IS NOT NULL ORDER BY selected_at DESC LIMIT 1",
            (trip["id"],),
        ).fetchone()
        if hotel_quote:
            details = _json(hotel_quote["details_json"], {})
            latitude, longitude = details.get("latitude"), details.get("longitude")
            if latitude is not None and longitude is not None:
                destination = db.execute("SELECT * FROM trip_destination_places WHERE trip_id=?", (trip["id"],)).fetchone()
                destination_value = dict(destination) if destination else {}
                hotel_title = str(hotel_quote["title"])
                hotel_place = {
                    "provider": "amap" if str(destination_value.get("country") or "中国") in {"", "中国", "中国大陆", "CN", "China"} else "geoapify",
                    "provider_place_id": f"hotel:{hashlib.sha256(hotel_title.encode('utf-8')).hexdigest()[:20]}",
                    "name": hotel_title, "formatted_address": "已选酒店，地址以预订页面为准",
                    "country": destination_value.get("country", ""), "province": destination_value.get("province", ""),
                    "city": destination_value.get("city", ""), "district": destination_value.get("district", ""),
                    "latitude": float(latitude), "longitude": float(longitude), "category": "住宿服务",
                }
        now = _now()
        today = date.today()
        start_date = date.fromisoformat(str(trip["start_date"])[:10])
        end_date = date.fromisoformat(str(trip["end_date"])[:10])
        driving_refresh_available = today <= end_date and start_date <= today + timedelta(days=3)
        legs: list[dict] = []
        for day_rows in by_day.values():
            nodes: list[tuple[dict | sqlite3.Row, dict | None]] = [
                (item, self._itinerary_place(db, item["id"])) for item in day_rows
            ]
            if hotel_place and day_rows:
                first = day_rows[0]
                start_hotel = {
                    "id": f"hotel-start:{first['day_id']}", "title": hotel_title, "type": "hotel",
                    "day_id": first["day_id"], "day_number": first["day_number"], "date": first["date"],
                    "day_title": first["day_title"],
                }
                end_hotel = {**start_hotel, "id": f"hotel-end:{first['day_id']}"}
                nodes = [(start_hotel, hotel_place), *nodes, (end_hotel, hotel_place)]
            for (origin_item, origin), (destination_item, destination) in zip(nodes, nodes[1:]):
                leg_seed = f"{trip['id']}:{trip['active_variant']}:{origin_item['day_id']}:{origin_item['id']}:{destination_item['id']}"
                leg = {
                    "id": "leg_" + hashlib.sha256(leg_seed.encode("utf-8")).hexdigest()[:24],
                    "day_id": origin_item["day_id"], "day_number": origin_item["day_number"],
                    "date": origin_item["date"], "day_title": origin_item["day_title"],
                    "origin_item": {"id": origin_item["id"], "title": origin_item["title"], "type": origin_item["type"], "place": origin},
                    "destination_item": {"id": destination_item["id"], "title": destination_item["title"], "type": destination_item["type"], "place": destination},
                    "resolved": bool(origin and destination), "straight_distance_meters": None,
                    "recommended_modes": [], "cached_routes": {},
                    "driving_refresh_available": driving_refresh_available,
                }
                if origin and destination:
                    provider_name = self._map_provider_for_places(origin, destination).name
                    distance = self._route_distance(origin, destination)
                    leg["straight_distance_meters"] = distance
                    leg["recommended_modes"] = self._route_recommendations(distance)
                    for mode in ("walking", "transit", "driving", "bicycling"):
                        cache_key = self._route_cache_key(provider_name, origin, destination, mode)
                        cached = db.execute(
                            "SELECT result_json,fetched_at,expires_at FROM route_cache WHERE cache_key=? AND expires_at>?",
                            (cache_key, now),
                        ).fetchone()
                        if cached:
                            route = _json(cached["result_json"], {})
                            route.update({"cached": True, "fetched_at": cached["fetched_at"], "expires_at": cached["expires_at"]})
                            leg["cached_routes"][mode] = route
                legs.append(leg)
        return legs

    def trip_route_legs(self, trip_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            trip = db.execute(
                "SELECT * FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL", (trip_id, couple["id"]),
            ).fetchone()
            if not trip:
                raise KeyError("旅行不存在")
            destination_place = db.execute("SELECT * FROM trip_destination_places WHERE trip_id=?", (trip_id,)).fetchone()
            if not destination_place:
                raise ValueError("请先从地图候选中确认具体目的地，再查看逐段路线")
            legs = self._build_route_legs(db, trip)
        provider = self._map_provider_for_places(dict(destination_place))
        return {
            "trip_id": trip_id, "active_variant": trip["active_variant"], "legs": legs,
            "configured": not provider.is_mock,
            "notice": "这里只读取已保存坐标并在本地计算直线距离；展开某一路段后才会查询对应地区的真实路线。",
            "cache_hours": 24,
        }

    def confirm_itinerary_place(
        self, trip_id: str, item_id: str, candidate_id: object, user_id: str,
        expected_version: object = None,
    ) -> dict:
        place = self._destination_candidate(candidate_id, user_id)
        if not place or place.get("latitude") is None or place.get("longitude") is None:
            raise ValueError("请选择带有地图坐标的高德或全球地点")
        now = _now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            item = db.execute(
                "SELECT i.*,v.trip_id,v.style FROM itinerary_items i JOIN trip_days d ON d.id=i.day_id "
                "JOIN plan_variants v ON v.id=d.variant_id JOIN trips t ON t.id=v.trip_id "
                "WHERE i.id=? AND v.trip_id=? AND t.couple_id=? AND t.deleted_at IS NULL",
                (item_id, trip_id, couple["id"]),
            ).fetchone()
            trip = db.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
            if not item or not trip:
                raise KeyError("行程地点不存在")
            if expected_version is not None and int(expected_version) != int(trip["version"]):
                raise ConflictError("旅行已经被另一方修改，请刷新路线后再确认地点")
            db.execute(
                """INSERT INTO itinerary_item_places(
                    itinerary_item_id,trip_id,provider,provider_place_id,name,formatted_address,country,province,city,
                    district,latitude,longitude,category,confirmed_by,confirmed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(itinerary_item_id) DO UPDATE SET provider=excluded.provider,
                    provider_place_id=excluded.provider_place_id,name=excluded.name,formatted_address=excluded.formatted_address,
                    country=excluded.country,province=excluded.province,city=excluded.city,district=excluded.district,
                    latitude=excluded.latitude,longitude=excluded.longitude,category=excluded.category,
                    confirmed_by=excluded.confirmed_by,confirmed_at=excluded.confirmed_at""",
                (item_id, trip_id, place["provider"], place["provider_place_id"], place["name"], place["formatted_address"],
                 place["country"], place["province"], place["city"], place["district"], place["latitude"],
                 place["longitude"], place["category"], user_id, now),
            )
            revision = int(trip["current_revision"]) + 1
            diff = {"itinerary_item_id": item_id, "confirmed_place": place["name"], "provider_place_id": place["provider_place_id"]}
            db.execute(
                "UPDATE trips SET current_revision=?,status='pending_approval',version=version+1,updated_at=? WHERE id=?",
                (revision, now, trip_id),
            )
            db.execute("DELETE FROM approvals WHERE trip_id=?", (trip_id,))
            db.execute(
                "INSERT INTO plan_revisions(id,trip_id,revision,parent_revision,author_type,diff_json,reason,created_at) "
                "VALUES (?,?,?,?, 'user',?,'确认行程地点',?)",
                (_id("revision"), trip_id, revision, trip["current_revision"], json.dumps(diff, ensure_ascii=False), now),
            )
            self._audit(db, "trip.item_place_confirmed", "itinerary_item", item_id, diff, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", trip_id, {"resource": "trip", "revision": revision})
        return {"trip": self.get_trip(trip_id, user_id), "place": place, "route_legs": self.trip_route_legs(trip_id, user_id)}

    def query_trip_route_leg(self, trip_id: str, leg_id: str, payload: dict, user_id: str) -> dict:
        raw_modes = payload.get("modes", ["walking", "transit", "driving"])
        if isinstance(raw_modes, str):
            raw_modes = [raw_modes]
        if not isinstance(raw_modes, list) or not raw_modes:
            raise ValueError("至少选择一种路线方式")
        modes = list(dict.fromkeys(str(mode) for mode in raw_modes))
        if len(modes) > 4 or any(mode not in {"walking", "transit", "driving", "bicycling"} for mode in modes):
            raise ValueError("路线方式仅支持步行、公交、驾车和骑行")
        force_refresh = bool(payload.get("force_refresh", False))
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            trip = db.execute(
                "SELECT * FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL", (trip_id, couple["id"]),
            ).fetchone()
            if not trip:
                raise KeyError("旅行不存在")
            leg = next((item for item in self._build_route_legs(db, trip) if item["id"] == leg_id), None)
        if not leg:
            raise KeyError("路线段不存在或行程顺序已经变化")
        if not leg["resolved"]:
            raise ValueError("请先确认这一路段两端的真实地图地点")
        if force_refresh and (modes != ["driving"] or not leg["driving_refresh_available"]):
            raise ValueError("强制刷新只用于临近出发日期的驾车路线")
        origin = leg["origin_item"]["place"]
        destination = leg["destination_item"]["place"]
        provider = self._map_provider_for_places(origin, destination)
        now = _now()
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).replace(microsecond=0).isoformat()
        routes, errors = {}, {}
        cache_hits = api_requests = 0
        for mode in modes:
            cache_key = self._route_cache_key(provider.name, origin, destination, mode)
            cached = None
            if not force_refresh:
                with self.connection() as db:
                    cached = db.execute(
                        "SELECT result_json,fetched_at,expires_at FROM route_cache WHERE cache_key=? AND expires_at>?",
                        (cache_key, now),
                    ).fetchone()
            if cached:
                route = _json(cached["result_json"], {})
                route.update({"cached": True, "fetched_at": cached["fetched_at"], "expires_at": cached["expires_at"]})
                routes[mode] = route
                if route.get("unavailable"):
                    errors[mode] = str(route.get("error") or "没有找到可用路线")
                cache_hits += 1
                continue
            if not provider.is_mock:
                # A provider request consumes quota even when it returns no
                # usable path, so count attempts rather than only successes.
                api_requests += 1
            try:
                route = provider.route_between(origin, destination, mode, _confirmed_place_city(origin) or _confirmed_place_city(destination))
            except ValueError as exc:
                errors[mode] = str(exc)
                route = {
                    "origin": origin, "destination": destination, "mode": mode, "provider": provider.name,
                    "mock": provider.is_mock, "unavailable": True, "error": str(exc), "distance_meters": 0,
                    "duration_seconds": 0, "steps": [], "cached": False, "fetched_at": now, "expires_at": expires_at,
                }
                with self.connection() as db:
                    db.execute(
                        """INSERT INTO route_cache(
                            cache_key,provider,mode,origin_place_id,destination_place_id,origin_latitude,origin_longitude,
                            destination_latitude,destination_longitude,result_json,fetched_at,expires_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(cache_key) DO UPDATE SET result_json=excluded.result_json,
                            fetched_at=excluded.fetched_at,expires_at=excluded.expires_at""",
                        (cache_key, provider.name, mode, str(origin.get("provider_place_id") or ""),
                         str(destination.get("provider_place_id") or ""), origin["latitude"], origin["longitude"],
                         destination["latitude"], destination["longitude"], json.dumps(route, ensure_ascii=False), now, expires_at),
                    )
                routes[mode] = route
                continue
            route.update({"cached": False, "fetched_at": now, "expires_at": expires_at})
            with self.connection() as db:
                db.execute("DELETE FROM route_cache WHERE expires_at<=?", (now,))
                db.execute(
                    """INSERT INTO route_cache(
                        cache_key,provider,mode,origin_place_id,destination_place_id,origin_latitude,origin_longitude,
                        destination_latitude,destination_longitude,result_json,fetched_at,expires_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(cache_key) DO UPDATE SET result_json=excluded.result_json,
                        fetched_at=excluded.fetched_at,expires_at=excluded.expires_at""",
                    (cache_key, provider.name, mode, str(origin.get("provider_place_id") or ""),
                     str(destination.get("provider_place_id") or ""), origin["latitude"], origin["longitude"],
                     destination["latitude"], destination["longitude"], json.dumps(route, ensure_ascii=False), now, expires_at),
                )
            routes[mode] = route
        return {
            "trip_id": trip_id, "leg_id": leg_id, "origin": origin, "destination": destination,
            "straight_distance_meters": leg["straight_distance_meters"], "recommended_modes": leg["recommended_modes"],
            "routes": routes, "errors": errors, "cache_hits": cache_hits, "api_requests": api_requests,
            "configured": not provider.is_mock, "cache_hours": 24,
            "notice": f"已复用 24 小时缓存。只有未命中的方式才会调用 {provider.name}；骑行只在你单独点击时查询。",
        }

    @staticmethod
    def _ensure_daily_meals(db: sqlite3.Connection, trip: sqlite3.Row) -> None:
        """Backfill breakfast/lunch/dinner without overwriting manual items."""
        days = db.execute(
            "SELECT d.* FROM trip_days d JOIN plan_variants v ON v.id=d.variant_id "
            "WHERE v.trip_id=? AND v.style='food' ORDER BY d.position",
            (trip["id"],),
        ).fetchall()
        for day in days:
            rows = db.execute("SELECT * FROM itinerary_items WHERE day_id=? ORDER BY position", (day["id"],)).fetchall()
            # Older builds created one generic snack/place per day. Remove an
            # unchosen afternoon-snack placeholder so the upgraded plan has an
            # unambiguous breakfast/lunch/dinner structure. A selected or locked
            # item is always preserved.
            for row in rows:
                selected = db.execute(
                    "SELECT 1 FROM trip_food_recommendations WHERE itinerary_item_id=? AND selected_at IS NOT NULL LIMIT 1",
                    (row["id"],),
                ).fetchone()
                if _meal_type(row["start_time"]) == "snack" and not row["locked"] and not selected:
                    db.execute("DELETE FROM trip_food_recommendations WHERE itinerary_item_id=?", (row["id"],))
                    db.execute("DELETE FROM itinerary_items WHERE id=?", (row["id"],))
            rows = db.execute("SELECT * FROM itinerary_items WHERE day_id=? ORDER BY position", (day["id"],)).fetchall()
            existing = {_meal_type(row["start_time"]) for row in rows}
            position = max((int(row["position"]) for row in rows), default=0)
            for meal, start_time in REQUIRED_MEALS:
                if meal in existing:
                    continue
                position += 1
                specialties = _food_specialties(trip["destination"], meal)
                meal_name = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐"}[meal]
                db.execute(
                    "INSERT INTO itinerary_items(id,day_id,type,title,start_time,duration_minutes,cost_cents,note,position) VALUES (?,?,?,?,?,?,0,?,?)",
                    (_id("item"), day["id"], "meal", f"{meal_name} · {' / '.join(specialties[:2])}", start_time, 60 if meal == "breakfast" else 90, f"推荐尝试：{'、'.join(specialties)}。请先选择本日游览或住宿区域。", position),
                )
            ordered = db.execute("SELECT id FROM itinerary_items WHERE day_id=? ORDER BY start_time, position", (day["id"],)).fetchall()
            for new_position, row in enumerate(ordered, 1):
                db.execute("UPDATE itinerary_items SET position=? WHERE id=?", (new_position, row["id"]))

    def refresh_food_recommendations(
        self,
        trip_id: str,
        user_id: str,
        search_area: str = "",
        day_areas: dict | None = None,
        day_number: object = None,
        item_id: str = "",
        preview_only: bool = False,
    ) -> dict:
        item_id = str(item_id or "").strip()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            trip = db.execute("SELECT * FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL", (trip_id, couple["id"])).fetchone()
            if not trip:
                raise KeyError("旅行不存在")
            if item_id:
                items = db.execute(
                    "SELECT i.*, d.day_number, d.id AS trip_day_id, v.style AS variant_style FROM itinerary_items i "
                    "JOIN trip_days d ON d.id=i.day_id JOIN plan_variants v ON v.id=d.variant_id "
                    "WHERE v.trip_id=? AND i.id=?",
                    (trip_id, item_id),
                ).fetchall()
                if not items:
                    raise KeyError("餐饮行程不存在")
                item = items[0]
                if item["type"] != "meal" and not re.search(r"早餐|早饭|午餐|午饭|晚餐|晚饭|餐厅|餐馆|饭店|小吃|美食|用餐|厨房|restaurant|breakfast|lunch|dinner", item["title"], re.I):
                    raise ValueError("只有餐饮行程可以查询附近餐厅")
            else:
                self._ensure_daily_meals(db, trip)
                items = db.execute(
                    "SELECT i.*, d.day_number, d.id AS trip_day_id, v.style AS variant_style FROM itinerary_items i JOIN trip_days d ON d.id=i.day_id "
                    "JOIN plan_variants v ON v.id=d.variant_id WHERE v.trip_id=? AND v.style='food' ORDER BY d.position, i.position",
                    (trip_id,),
                ).fetchall()
        if not items:
            raise ValueError("美食方案还没有可推荐餐馆的时段")
        search_area = str(search_area or "").strip()[:120]
        if day_areas is None:
            day_areas = {}
        if not isinstance(day_areas, dict):
            raise ValueError("每日餐馆区域格式无效")
        normalized_areas = {str(key): str(value or "").strip()[:120] for key, value in day_areas.items()}
        requested_day = None
        if day_number not in (None, ""):
            try:
                requested_day = int(day_number)
            except (TypeError, ValueError) as exc:
                raise ValueError("旅行日期无效") from exc
            if requested_day < 1:
                raise ValueError("旅行日期无效")
            items = [item for item in items if int(item["day_number"]) == requested_day]
            if not items:
                raise ValueError("没有找到这一天的餐饮安排")
        unique_days = sorted({int(item["day_number"]) for item in items})
        resolved_areas: dict[int, str] = {}
        missing_days = []
        for number in unique_days:
            first = next(item for item in items if int(item["day_number"]) == number)
            area = normalized_areas.get(str(number)) or normalized_areas.get(str(first["trip_day_id"])) or search_area
            if not area and not _is_broad_destination(trip["destination"]):
                area = trip["destination"]
            if not area:
                missing_days.append(number)
            resolved_areas[number] = area
        if missing_days:
            labels = "、".join(f"第 {number} 天" for number in missing_days)
            raise ValueError(f"{labels}需要填写具体城市、景区、古城或酒店区域，避免推荐到很远的店")
        provider = map_provider_from_env(self.external_request_guard("amap"))
        prepared: list[tuple[sqlite3.Row, str, list[str], str, list[dict]]] = []
        for item in items:
            meal = _meal_type(item["start_time"])
            specialties = _food_specialties(trip["destination"], meal)
            area = resolved_areas[int(item["day_number"])]
            city = _food_search_city(trip["destination"], area)
            rows, seen = [], set()
            # AMap accepts `|` as an OR separator. One nearby-place request per
            # meal keeps the self-hosted/free-quota version practical while
            # still covering several local specialties.
            for row in provider.search_food("|".join(specialties), city, 5, area):
                poi_id = str(row.get("amap_poi_id", ""))
                if not poi_id or poi_id in seen:
                    continue
                distance = row.get("distance_meters")
                if distance is not None and int(distance) > 5_000:
                    continue
                seen.add(poi_id)
                rows.append(row)
            # The search radius already guarantees proximity. Within that
            # radius, prefer stronger public ratings and use distance to break
            # ties so “nearby” does not accidentally mean “lowest rated”.
            rows.sort(key=lambda row: (row.get("rating") is None, -(row.get("rating") or 0), row.get("distance_meters") is None, row.get("distance_meters") or 99_999))
            prepared.append((item, meal, specialties, area, rows[:5]))
        review_rows: list[dict] = []
        review_notice = ""
        review_configured = False
        # A single item refresh is an explicit request for details. Use one
        # metered search for concrete public result pages; refreshing a whole
        # food plan must not silently spend one search per meal.
        if item_id and prepared:
            review_provider = restaurant_review_search_from_env(self.external_request_guard("serpapi"))
            review_configured = review_provider.is_configured
            if review_configured:
                _, _, _, review_area, nearby_rows = prepared[0]
                try:
                    review_rows = review_provider.search(
                        review_area,
                        [str(row.get("name", "")) for row in nearby_rows],
                    )
                    review_notice = "已检索大众点评、美团和小红书的具体公开结果；只展示搜索服务实际返回的来源。" if review_rows else "未检索到可验证的公开口碑页面，不会用通用平台入口或模型补写。"
                except (RuntimeError, QuotaExceededError) as exc:
                    review_notice = str(exc)
        fetched_at = _now()
        saved_count = 0
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            current = db.execute("SELECT * FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL", (trip_id, couple["id"])).fetchone()
            if not current:
                raise KeyError("旅行不存在")
            refreshed_item_ids = [item["id"] for item, _, _, _, _ in prepared]
            placeholders = ",".join("?" for _ in refreshed_item_ids)
            db.execute(
                f"DELETE FROM trip_food_recommendations WHERE trip_id=? AND selected_at IS NULL AND itinerary_item_id IN ({placeholders})",
                (trip_id, *refreshed_item_ids),
            )
            if item_id:
                db.execute(
                    f"DELETE FROM trip_food_review_evidence WHERE trip_id=? AND itinerary_item_id IN ({placeholders})",
                    (trip_id, *refreshed_item_ids),
                )
            changed_items = []
            for item, meal, specialties, area, rows in prepared:
                meal_name = {"breakfast": "早餐", "lunch": "午餐", "snack": "下午茶", "dinner": "晚餐"}[meal]
                title = f"{meal_name} · {' / '.join(specialties[:2])}"
                note = f"推荐尝试：{'、'.join(specialties)}。可在下方选择真实店铺并用高德导航。"
                if not item_id:
                    db.execute("UPDATE itinerary_items SET title=?, note=?, version=version+1 WHERE id=?", (title[:100], note[:500], item["id"]))
                changed_items.append({"item_id": item["id"], "meal_type": meal, "specialties": specialties})
                for row in rows:
                    poi_id = str(row.get("amap_poi_id", ""))
                    if not poi_id or not row.get("name"):
                        continue
                    db.execute(
                        "INSERT OR IGNORE INTO trip_food_recommendations(id,trip_id,itinerary_item_id,meal_type,specialties,name,address,district,telephone,category,rating,average_cost_cents,opening_hours,latitude,longitude,distance_meters,search_area,amap_poi_id,navigation_url,source,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (_id("food"), trip_id, item["id"], meal, "、".join(specialties), str(row["name"])[:120], str(row.get("address", ""))[:300], str(row.get("district", ""))[:80], str(row.get("telephone", ""))[:100], str(row.get("category", ""))[:160], row.get("rating"), row.get("average_cost_cents"), str(row.get("opening_hours", ""))[:160], row.get("latitude"), row.get("longitude"), row.get("distance_meters"), area, poi_id[:80], str(row.get("navigation_url", ""))[:1000], str(row.get("source", provider.name))[:40], fetched_at),
                    )
                    saved_count += 1
            for evidence in review_rows:
                db.execute(
                    "INSERT OR IGNORE INTO trip_food_review_evidence(id,trip_id,itinerary_item_id,platform,title,snippet,url,source_query,fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        _id("review"), trip_id, refreshed_item_ids[0], str(evidence["platform"])[:40],
                        str(evidence["title"])[:240], str(evidence.get("snippet", ""))[:600],
                        str(evidence["url"])[:1200], str(evidence.get("source_query", ""))[:600], fetched_at,
                    ),
                )
            revision = int(current["current_revision"])
            touches_active_variant = not preview_only and any(item["variant_style"] == current["active_variant"] for item, _, _, _, _ in prepared)
            if touches_active_variant:
                revision += 1
                diff = {"food_recommendations": changed_items, "restaurant_count": saved_count, "day_areas": resolved_areas}
                db.execute("UPDATE trips SET current_revision=?, status='pending_approval', version=version+1, updated_at=? WHERE id=?", (revision, fetched_at, trip_id))
                db.execute("DELETE FROM approvals WHERE trip_id=?", (trip_id,))
                db.execute(
                    "INSERT INTO plan_revisions(id,trip_id,revision,parent_revision,author_type,diff_json,reason,created_at) VALUES (?,?,?,?, 'user',?,'补充真实餐馆推荐',?)",
                    (_id("revision"), trip_id, revision, current["current_revision"], json.dumps(diff, ensure_ascii=False), fetched_at),
                )
            elif not preview_only:
                db.execute("UPDATE trips SET version=version+1, updated_at=? WHERE id=?", (fetched_at, trip_id))
            self._audit(db, "trip.food_refreshed", "trip", trip_id, {"restaurant_count": saved_count, "provider": provider.name}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", trip_id, {"resource": "trip", "food_recommendations": saved_count, "revision": revision})
        trip_result = self.get_trip(trip_id, user_id=user_id)
        notice = "已从高德地点服务读取 5 公里内真实餐馆并优先显示高评分结果。" if not provider.is_mock else "未配置真实地点服务，已补充当地特色菜品，但没有虚构具体店名。"
        if review_notice:
            notice = f"{notice}{review_notice}"
        return {
            "trip": trip_result, "count": saved_count, "provider": provider.name,
            "configured": not provider.is_mock, "day_areas": resolved_areas, "notice": notice,
            "review_count": len(review_rows), "review_configured": review_configured,
        }

    def select_food_recommendation(self, trip_id: str, recommendation_id: str, user_id: str) -> dict:
        now = _now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            trip = db.execute("SELECT * FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL", (trip_id, couple["id"])).fetchone()
            row = db.execute("SELECT * FROM trip_food_recommendations WHERE id=? AND trip_id=?", (recommendation_id, trip_id)).fetchone()
            if not trip or not row:
                raise KeyError("餐馆推荐不存在")
            db.execute("UPDATE trip_food_recommendations SET selected_at=NULL, selected_by=NULL WHERE trip_id=? AND itinerary_item_id=?", (trip_id, row["itinerary_item_id"]))
            db.execute("UPDATE trip_food_recommendations SET selected_at=?, selected_by=? WHERE id=?", (now, user_id, recommendation_id))
            if row["latitude"] is not None and row["longitude"] is not None:
                db.execute(
                    """INSERT INTO itinerary_item_places(
                        itinerary_item_id,trip_id,provider,provider_place_id,name,formatted_address,country,province,
                        city,district,latitude,longitude,category,confirmed_by,confirmed_at
                    ) VALUES (?,?, 'amap',?,?,?,'中国','','',?,?,?,?,?,?)
                    ON CONFLICT(itinerary_item_id) DO UPDATE SET provider='amap',
                        provider_place_id=excluded.provider_place_id,name=excluded.name,
                        formatted_address=excluded.formatted_address,district=excluded.district,
                        latitude=excluded.latitude,longitude=excluded.longitude,category=excluded.category,
                        confirmed_by=excluded.confirmed_by,confirmed_at=excluded.confirmed_at""",
                    (row["itinerary_item_id"], trip_id, row["amap_poi_id"], row["name"], row["address"],
                     row["district"], row["latitude"], row["longitude"], row["category"], user_id, now),
                )
            revision = int(trip["current_revision"]) + 1
            diff = {"selected_restaurant": row["name"], "meal_type": row["meal_type"], "itinerary_item_id": row["itinerary_item_id"]}
            db.execute("UPDATE trips SET current_revision=?, status='pending_approval', version=version+1, updated_at=? WHERE id=?", (revision, now, trip_id))
            db.execute("DELETE FROM approvals WHERE trip_id=?", (trip_id,))
            db.execute(
                "INSERT INTO plan_revisions(id,trip_id,revision,parent_revision,author_type,diff_json,reason,created_at) VALUES (?,?,?,?, 'user',?,'选择特色餐馆',?)",
                (_id("revision"), trip_id, revision, trip["current_revision"], json.dumps(diff, ensure_ascii=False), now),
            )
            self._audit(db, "trip.food_selected", "trip_food_recommendation", recommendation_id, diff, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", trip_id, {"resource": "trip", "selected_restaurant": recommendation_id, "revision": revision})
        return self.get_trip(trip_id, user_id=user_id)

    def search_quotes(self, payload: dict, user_id: str) -> dict:
        quote_type = str(payload.get("type", "hotel"))
        if quote_type not in {"hotel", "flight", "train", "transport"}:
            raise ValueError("报价类型无效")
        trip_id = str(payload.get("trip_id", "")).strip() or None
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            trip = None
            destination_place = None
            if trip_id:
                trip_row = db.execute("SELECT * FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL", (trip_id, couple["id"])).fetchone()
                trip = dict(trip_row) if trip_row else None
                if not trip:
                    raise KeyError("旅行不存在")
                place_row = db.execute(
                    "SELECT provider,provider_place_id,name,formatted_address,country,province,city,district,latitude,longitude,category FROM trip_destination_places WHERE trip_id=?",
                    (trip_id,),
                ).fetchone()
                destination_place = dict(place_row) if place_row else None
        query = dict(payload)
        search_scope = str(query.get("destination") or "").strip()
        if trip:
            if not destination_place:
                raise ValueError("请先从高德候选中确认具体目的地，再查询路线、酒店或交通")
            query.setdefault("destination", trip["destination"])
            query.setdefault("budget_cents", trip["budget_cents"])
            query.setdefault("start_date", trip["start_date"])
            query.setdefault("end_date", trip["end_date"])
            if quote_type == "hotel":
                query["destination"] = _confirmed_hotel_scope(destination_place, query.get("destination"))
                search_scope = query["destination"]
                query["destination_latitude"] = destination_place.get("latitude")
                query["destination_longitude"] = destination_place.get("longitude")
                query["destination_country"] = destination_place.get("country")
            elif not str(query.get("destination") or "").strip():
                query["destination"] = _confirmed_place_city(destination_place) or destination_place["name"]
        provider = quote_provider_from_env(quote_type)
        quota_provider = {
            "serpapi-google-travel": "serpapi",
            "juhe-train": "juhe_train",
            "authorized-http": "booking",
        }.get(provider.name)
        if quota_provider:
            provider.attach_request_guard(self.external_request_guard(quota_provider))
        if quote_type == "hotel" and provider.name == "serpapi-google-travel":
            raw_destination = str(query.get("destination") or "").strip()
            if raw_destination and not raw_destination.isascii():
                place_context = destination_place or {"name": raw_destination}
                cache_key = (str(place_context.get("provider_place_id") or place_context.get("name") or "")[:160], raw_destination[:240])
                with self._hotel_destination_query_cache_lock:
                    resolved_destination = self._hotel_destination_query_cache.get(cache_key, "")
                if not resolved_destination:
                    resolver = getattr(self.travel_assistant, "resolve_hotel_destination", None)
                    try:
                        resolved_destination = str(resolver(place_context, raw_destination) if resolver else "").strip()
                    except Exception:
                        resolved_destination = ""
                    if resolved_destination:
                        with self._hotel_destination_query_cache_lock:
                            self._hotel_destination_query_cache[cache_key] = resolved_destination
                if resolved_destination:
                    query["destination"] = resolved_destination
        try:
            rows = provider.search(query)
        except RuntimeError as error:
            if not trip_id:
                raise
            with self.connection() as db:
                cached_rows = db.execute(
                    "SELECT * FROM quote_snapshots WHERE couple_id=? AND trip_id=? AND quote_type=? "
                    "ORDER BY fetched_at DESC LIMIT 20",
                    (couple["id"], trip_id, quote_type),
                ).fetchall()
            if not cached_rows:
                raise
            cached = []
            for cached_row in cached_rows:
                item = dict(cached_row)
                item["details"] = _json(item.pop("details_json", "{}"), {})
                item["stale"] = True
                cached.append({**item, **self._quote_source_metadata(item)})
            return {
                "items": cached, "provider": provider.name, "source_mode": "cached",
                "mock": False, "degraded": True, "count": len(cached),
                "notice": f"实时查询暂时失败（{str(error)[:120]}），正在显示这次旅行上一次保存的结果；价格和库存可能已经变化，请跳转平台再次确认。",
                "search_scope": search_scope,
            }
        if quote_type == "hotel" and _is_china_place(destination_place):
            place_key = str(destination_place.get("provider_place_id") or destination_place.get("name") or "")[:160]
            english_names = [
                str(row.get("title") or "").strip() for row in rows
                if re.search(r"[A-Za-z]", str(row.get("title") or "").strip())
            ]
            localized: dict[str, str] = {}
            missing: list[str] = []
            with self._hotel_name_localization_cache_lock:
                for name in english_names:
                    cached_name = self._hotel_name_localization_cache.get((place_key, name))
                    if cached_name:
                        localized[name] = cached_name
                    else:
                        missing.append(name)
            if missing:
                localizer = getattr(self.travel_assistant, "localize_hotel_names", None)
                try:
                    translated = localizer(missing, destination_place) if localizer else {}
                except Exception:
                    translated = {}
                if isinstance(translated, dict):
                    for original in missing:
                        chinese = str(translated.get(original) or "").strip()
                        if chinese and re.search(r"[\u3400-\u9fff]", chinese):
                            localized[original] = chinese[:120]
                    with self._hotel_name_localization_cache_lock:
                        for original, chinese in localized.items():
                            self._hotel_name_localization_cache[(place_key, original)] = chinese
            for row in rows:
                original_title = str(row.get("title") or "").strip()
                details = row.get("details") if isinstance(row.get("details"), dict) else {}
                details["original_title"] = original_title
                if original_title in localized:
                    row["title"] = localized[original_title]
                details["booking_copy_text"] = str(row.get("title") or original_title)[:120]
                row["details"] = details
        saved = []
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            for row in rows:
                quote_id = _id("quote")
                details_json = json.dumps(row.get("details") if isinstance(row.get("details"), dict) else {}, ensure_ascii=False, separators=(",", ":"))
                db.execute("INSERT INTO quote_snapshots(id,couple_id,trip_id,quote_type,provider,source_mode,title,subtitle,amount_cents,currency,terms,includes_tax,booking_url,details_json,fetched_at,expires_at,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (quote_id, couple["id"], trip_id, row["quote_type"], row["provider"], row["source_mode"], row["title"], row["subtitle"], row["amount_cents"], row["currency"], row["terms"], int(row["includes_tax"]), row["booking_url"], details_json, row["fetched_at"], row.get("expires_at"), user_id))
                saved_row = {"id": quote_id, **row}
                saved.append({**saved_row, **self._quote_source_metadata(saved_row)})
        if provider.is_mock:
            notice = "当前为模拟报价，不代表真实库存；配置对应查询服务后才会显示实时内容。"
        elif provider.name == "serpapi-google-travel":
            notice = "以下为 Google Travel 聚合参考结果，不是平台直连库存；最终价格、税费和退改规则以跳转后的预订平台为准。"
        elif provider.name == "juhe-train":
            notice = "以下为聚合数据提供的车次、票价和余票参考；余票变化较快，购票前请在铁路 12306 再次确认。"
        else:
            notice = "最终价格、库存和退改规则以预订平台为准。"
        return {
            "items": saved, "provider": provider.name,
            "source_mode": saved[0]["source_mode"] if saved else "unknown",
            "mock": provider.is_mock, "count": len(saved), "notice": notice,
            "search_scope": search_scope,
        }

    def create_manual_quote(self, payload: dict, user_id: str) -> dict:
        title = str(payload.get("title", "")).strip()
        provider = str(payload.get("provider", "手工录入")).strip()
        if not title or not provider:
            raise ValueError("报价标题和来源不能为空")
        try:
            amount = int(round(float(payload.get("amount", 0)) * 100)) if "amount_cents" not in payload else int(payload["amount_cents"])
        except (TypeError, ValueError) as exc:
            raise ValueError("报价金额必须是数字") from exc
        if not 0 <= amount <= 100_000_000:
            raise ValueError("报价金额超出范围")
        trip_id = str(payload.get("trip_id", "")).strip() or None
        now, quote_id = _now(), _id("quote")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            if trip_id and not db.execute("SELECT 1 FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL", (trip_id, couple["id"])).fetchone():
                raise KeyError("旅行不存在")
            db.execute("INSERT INTO quote_snapshots(id,couple_id,trip_id,quote_type,provider,source_mode,title,subtitle,amount_cents,currency,terms,includes_tax,booking_url,fetched_at,created_by) VALUES (?,?,?,?,?,'manual',?,?,?,?,?,?,?,?,?)", (quote_id, couple["id"], trip_id, str(payload.get("type", "hotel"))[:20], provider[:80], title[:200], str(payload.get("subtitle", ""))[:300], amount, str(payload.get("currency", "CNY"))[:8], str(payload.get("terms", ""))[:500], int(bool(payload.get("includes_tax", True))), str(payload.get("booking_url", ""))[:1000], now, user_id))
            row = db.execute("SELECT * FROM quote_snapshots WHERE id=?", (quote_id,)).fetchone()
        value = dict(row)
        return {**value, **self._quote_source_metadata(value)}

    def select_quote(self, quote_id: str, user_id: str, expected_version: object = None) -> dict:
        """Select one shared quote per trip and category without purchasing it."""
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM quote_snapshots WHERE id=? AND couple_id=?", (quote_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("报价不存在")
            if not row["trip_id"]:
                raise ValueError("只有关联旅行的报价才能加入共同选择")
            trip = db.execute("SELECT * FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL", (row["trip_id"], couple["id"])).fetchone()
            if not trip:
                raise KeyError("旅行不存在")
            if expected_version is not None and int(expected_version) != int(trip["version"]):
                raise ConflictError("旅行已经被另一方修改，请刷新报价后再选择")
            now = _now()
            db.execute("UPDATE quote_snapshots SET selected_at=NULL, selected_by=NULL WHERE couple_id=? AND trip_id=? AND quote_type=?", (couple["id"], row["trip_id"], row["quote_type"]))
            db.execute("UPDATE quote_snapshots SET selected_at=?, selected_by=? WHERE id=?", (now, user_id, quote_id))
            revision = int(trip["current_revision"]) + 1
            diff = {
                "selected_quote_id": quote_id, "quote_type": row["quote_type"],
                "title": row["title"], "amount_cents": row["amount_cents"],
            }
            db.execute(
                "UPDATE trips SET current_revision=?,status='pending_approval',version=version+1,updated_at=? WHERE id=?",
                (revision, now, row["trip_id"]),
            )
            db.execute("DELETE FROM approvals WHERE trip_id=?", (row["trip_id"],))
            db.execute(
                "INSERT INTO plan_revisions(id,trip_id,revision,parent_revision,author_type,diff_json,reason,created_at) "
                "VALUES (?,?,?,?, 'user',?,?,?)",
                (_id("revision"), row["trip_id"], revision, trip["current_revision"], json.dumps(diff, ensure_ascii=False), "选择旅行报价", now),
            )
            self._audit(db, "quote.selected", "quote", quote_id, {"trip_id": row["trip_id"], "quote_type": row["quote_type"]}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "trip.updated", row["trip_id"], {"resource": "quote", "quote_id": quote_id})
            selected = db.execute("SELECT * FROM quote_snapshots WHERE id=?", (quote_id,)).fetchone()
        result = dict(selected)
        result["details"] = _json(result.pop("details_json", "{}"), {})
        result["trip_version"] = int(trip["version"]) + 1
        result["revision"] = revision
        return result

    # ---- OCR import and message-to-moment -----------------------------

    def create_chat_import(self, payload: dict, user_id: str) -> dict:
        raw_media_id = payload.get("media_id")
        media_id = str(raw_media_id).strip() or None if raw_media_id is not None else None
        hint_text = str(payload.get("text", ""))[:100_000]
        image, mime = b"", "image/png"
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            if media_id:
                media = db.execute("SELECT * FROM media_assets WHERE id=? AND couple_id=? AND owner_id=? AND kind='image' AND status='ready'", (media_id, couple["id"], user_id)).fetchone()
                if not media:
                    raise ValueError("OCR 图片不存在或无权使用")
                image, mime = self.get_media_content(media_id, user_id=user_id)[:2]
        if not image and not hint_text:
            raise ValueError("请上传聊天截图或粘贴聊天文字")
        # Pasted text is parsed locally and must not be labelled as a real OCR
        # result. Only uploaded image bytes are sent to the configured service.
        provider = ocr_provider_from_env() if image else ManualTextProvider()
        if image and hasattr(provider, "before_request"):
            quota_provider = "aliyun_ocr" if provider.name == "aliyun" else "ocr_http"
            provider.before_request = self.external_request_guard(quota_provider)
        raw, items = provider.extract(image, mime, hint_text)
        if not items:
            raise ValueError("没有识别到可编辑的聊天内容")
        import_id, now = _id("import"), _now()
        base_date = str(payload.get("base_date", date.today().isoformat()))
        try:
            base = date.fromisoformat(base_date)
        except ValueError as exc:
            raise ValueError("聊天日期格式无效") from exc
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            source_label = "粘贴文本" if provider.name == "manual" else "演示识别" if provider.is_mock else "阿里云 OCR" if provider.name == "aliyun" else "真实 OCR"
            db.execute("INSERT INTO chat_imports(id,couple_id,creator_id,media_id,provider,source_label,status,raw_text,created_at) VALUES (?,?,?,?,?,?, 'draft',?,?)", (import_id, couple["id"], user_id, media_id, provider.name, source_label, raw, now))
            for position, item in enumerate(items, 1):
                occurred_at = None
                if item.get("time"):
                    try:
                        hour, minute = map(int, item["time"].split(":"))
                        occurred_at = datetime(base.year, base.month, base.day, hour, minute, tzinfo=timezone.utc).isoformat()
                    except (ValueError, TypeError):
                        occurred_at = None
                db.execute("INSERT INTO chat_import_items(id,import_id,speaker,content,occurred_at,confidence,selected,position) VALUES (?,?,?,?,?,?,1,?)", (_id("import_item"), import_id, item["speaker"], item["content"], occurred_at, item.get("confidence", 0), position))
        return self.get_chat_import(import_id, user_id)

    def get_chat_import(self, import_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM chat_imports WHERE id=? AND couple_id=? AND creator_id=?", (import_id, couple["id"], user_id)).fetchone()
            if not row:
                raise KeyError("聊天导入草稿不存在")
            result = dict(row)
            result["items"] = [dict(item) for item in db.execute("SELECT * FROM chat_import_items WHERE import_id=? ORDER BY position", (import_id,)).fetchall()]
            result["mock"] = row["provider"] == "mock"
        return result

    def update_chat_import_item(self, import_id: str, item_id: str, payload: dict, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            draft = db.execute("SELECT * FROM chat_imports WHERE id=? AND couple_id=? AND creator_id=?", (import_id, couple["id"], user_id)).fetchone()
            item = db.execute("SELECT * FROM chat_import_items WHERE id=? AND import_id=?", (item_id, import_id)).fetchone()
            if not draft or not item:
                raise KeyError("聊天导入项目不存在")
            if draft["status"] != "draft":
                raise ValueError("该导入草稿已结束")
            expected = payload.get("expected_version")
            if expected is not None and int(expected) != item["version"]:
                raise ConflictError("该聊天条目已被修改，请刷新")
            speaker = str(payload.get("speaker", item["speaker"]))
            content = str(payload.get("content", item["content"])).strip()[:4000]
            if speaker not in {"me", "partner"} or not content:
                raise ValueError("聊天条目内容无效")
            selected = int(bool(payload.get("selected", item["selected"])))
            occurred = payload.get("occurred_at", item["occurred_at"])
            occurred = _iso(occurred, "聊天时间") if occurred else None
            db.execute("UPDATE chat_import_items SET speaker=?,content=?,occurred_at=?,selected=?,version=version+1 WHERE id=?", (speaker, content, occurred, selected, item_id))
        return self.get_chat_import(import_id, user_id)

    def finish_chat_import(self, import_id: str, payload: dict, user_id: str) -> dict:
        as_moments = bool(payload.get("as_moments", False))
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            draft = db.execute("SELECT * FROM chat_imports WHERE id=? AND couple_id=? AND creator_id=?", (import_id, couple["id"], user_id)).fetchone()
            if not draft:
                raise KeyError("聊天导入草稿不存在")
            if draft["status"] == "confirmed":
                return {"id": import_id, "status": "confirmed", "idempotent": True}
            if draft["status"] != "draft":
                raise ValueError("该导入草稿不能确认")
            items = db.execute("SELECT * FROM chat_import_items WHERE import_id=? AND selected=1 ORDER BY position", (import_id,)).fetchall()
            if not items:
                raise ValueError("至少选择一条聊天内容")
            created_ids = []
            for position, item in enumerate(items):
                message_id = _id("msg")
                sender = user_id if item["speaker"] == "me" else partner_id
                sent_at = item["occurred_at"] or (datetime.now(timezone.utc) + timedelta(seconds=position)).replace(microsecond=0).isoformat()
                metadata = {"importId": import_id, "ocrProvider": draft["provider"], "confirmedBy": user_id}
                db.execute("INSERT INTO messages(id,couple_id,sender_id,type,content,metadata,sent_at,read_at) VALUES (?,?,?,'text',?,?,?,?)", (message_id, couple["id"], sender, item["content"], json.dumps(metadata, ensure_ascii=False), sent_at, _now()))
                created_ids.append(message_id)
                if as_moments:
                    db.execute("INSERT INTO moments(id,couple_id,creator_id,type,title,body,occurred_at,visibility,source_message_id) VALUES (?,?,?,'chat','聊天收藏',?,?,'shared',?)", (_id("moment"), couple["id"], user_id, item["content"], sent_at, message_id))
            db.execute("UPDATE chat_imports SET status='confirmed',confirmed_at=? WHERE id=?", (_now(), import_id))
            self._audit(db, "chat_import.confirmed", "chat_import", import_id, {"count": len(created_ids), "as_moments": as_moments}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "message.created", created_ids[-1], {"imported": len(created_ids)})
        return {"id": import_id, "status": "confirmed", "created_message_ids": created_ids, "count": len(created_ids)}

    def cancel_chat_import(self, import_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            changed = db.execute("UPDATE chat_imports SET status='cancelled',cancelled_at=? WHERE id=? AND couple_id=? AND creator_id=? AND status='draft'", (_now(), import_id, couple["id"], user_id)).rowcount
            if not changed:
                raise KeyError("可取消的聊天导入草稿不存在")
        return {"id": import_id, "status": "cancelled"}

    def message_to_moment(self, message_id: str, payload: dict, user_id: str) -> dict:
        visibility = str(payload.get("visibility", "shared"))
        if visibility not in {"shared", "private"}:
            raise ValueError("收藏可见范围无效")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            message = db.execute("SELECT * FROM messages WHERE id=? AND couple_id=?", (message_id, couple["id"])).fetchone()
            if not message:
                raise KeyError("消息不存在")
            existing = db.execute("SELECT * FROM moments WHERE source_message_id=? AND creator_id=? AND purged_at IS NULL", (message_id, user_id)).fetchone()
            if existing:
                return dict(existing)
            moment_id = _id("moment")
            db.execute("INSERT INTO moments(id,couple_id,creator_id,type,title,body,occurred_at,visibility,source_message_id) VALUES (?,?,?,'chat',?,?,?,?,?)", (moment_id, couple["id"], user_id, str(payload.get("title") or "一段想记住的聊天")[:200], message["content"][:5000], message["sent_at"], visibility, message_id))
            self._audit(db, "message.saved_as_moment", "message", message_id, {"moment_id": moment_id}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "moment.created", moment_id, {"type": "chat"})
            row = db.execute("SELECT * FROM moments WHERE id=?", (moment_id,)).fetchone()
        return dict(row)

    # ---- Moment trash and reports ------------------------------------

    def list_moment_trash(self, user_id: str) -> list[dict]:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0).isoformat()
            expired_media = [row["media_id"] for row in db.execute("SELECT media_id FROM moments WHERE couple_id=? AND deleted_at<? AND purged_at IS NULL AND media_id IS NOT NULL", (couple["id"], cutoff)).fetchall()]
            db.execute(
                "UPDATE moments SET title='已永久删除',body='',location=NULL,mood=NULL,media_url=NULL,purged_at=?,version=version+1 WHERE couple_id=? AND deleted_at<? AND purged_at IS NULL",
                (_now(), couple["id"], cutoff),
            )
            for media_id in expired_media:
                self._sync_media_visibility(db, media_id)
            rows = db.execute("SELECT * FROM moments WHERE couple_id=? AND deleted_at IS NOT NULL AND purged_at IS NULL AND (visibility='shared' OR creator_id=?) ORDER BY deleted_at DESC", (couple["id"], user_id)).fetchall()
        return [dict(row) for row in rows]

    def trash_moment(self, moment_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM moments WHERE id=? AND couple_id=? AND purged_at IS NULL", (moment_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("瞬间不存在")
            if row["creator_id"] != user_id:
                raise PermissionError("只能移入自己创建的瞬间")
            db.execute("UPDATE moments SET deleted_at=?,version=version+1 WHERE id=?", (_now(), moment_id))
            if row["media_id"]:
                self._sync_media_visibility(db, row["media_id"])
            self._event(db, couple["id"], user_id, "moment.trashed", moment_id, {})
        return {"id": moment_id, "deleted": True}

    def restore_moment(self, moment_id: str, expected_version: object, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM moments WHERE id=? AND couple_id=? AND deleted_at IS NOT NULL AND purged_at IS NULL", (moment_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("回收站中没有该瞬间")
            if row["visibility"] != "shared" and row["creator_id"] != user_id:
                raise PermissionError("无权恢复这项私密内容")
            if expected_version is not None and int(expected_version) != row["version"]:
                raise ConflictError("瞬间已被另一方修改，请刷新")
            db.execute("UPDATE moments SET deleted_at=NULL,version=version+1 WHERE id=?", (moment_id,))
            if row["media_id"]:
                self._sync_media_visibility(db, row["media_id"])
            self._event(db, couple["id"], user_id, "moment.restored", moment_id, {})
            updated = db.execute("SELECT * FROM moments WHERE id=?", (moment_id,)).fetchone()
        return dict(updated)

    def purge_moment(self, moment_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM moments WHERE id=? AND couple_id=? AND deleted_at IS NOT NULL AND purged_at IS NULL", (moment_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("回收站中没有该瞬间")
            if row["creator_id"] != user_id:
                raise PermissionError("只能永久删除自己创建的瞬间")
            db.execute("UPDATE moments SET title='已永久删除',body='',location=NULL,mood=NULL,media_url=NULL,purged_at=?,version=version+1 WHERE id=?", (_now(), moment_id))
            if row["media_id"]:
                self._sync_media_visibility(db, row["media_id"])
            self._audit(db, "moment.purged", "moment", moment_id, actor_id=user_id)
        return {"id": moment_id, "purged": True}

    def timeline_months(self, user_id: str) -> list[dict]:
        moments = self.list_moments(limit=2000, user_id=user_id)
        groups: dict[str, dict] = {}
        for moment in moments:
            month = str(moment["occurred_at"])[:7]
            group = groups.setdefault(month, {"month": month, "count": 0, "media_count": 0, "items": []})
            group["count"] += 1
            group["media_count"] += int(bool(moment.get("media_url")))
            group["items"].append(moment)
        return [groups[key] for key in sorted(groups, reverse=True)]

    def timeline_footprints(self, user_id: str) -> dict:
        provider = map_provider_from_env(self.external_request_guard("amap"))
        degraded = False
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            rows = db.execute("SELECT id,'moment' AS resource_type,location AS name,occurred_at AS visited_at FROM moments WHERE couple_id=? AND deleted_at IS NULL AND purged_at IS NULL AND location IS NOT NULL AND location<>'' AND (visibility='shared' OR creator_id=?) UNION ALL SELECT id,'trip',arrival_city,start_date FROM trips WHERE couple_id=? AND deleted_at IS NULL AND arrival_city<>''", (couple["id"], user_id, couple["id"])).fetchall()
            couple_id = couple["id"]
            existing = {
                (place["resource_type"], place["resource_id"], place["name"]): dict(place)
                for place in db.execute("SELECT * FROM place_snapshots WHERE couple_id=?", (couple_id,)).fetchall()
            }
        # Never keep a SQLite write transaction open during a metered network
        # request: the quota reservation uses its own immediate transaction so
        # two accounts cannot race past the cap.
        items = []
        for row in rows:
            key = (row["resource_type"], row["id"], row["name"])
            place = existing.get(key)
            city_hint = _food_search_city(row["name"], row["name"]) or row["name"]
            place_city = str((place or {}).get("city") or "")
            needs_refresh = bool(place and row["resource_type"] == "trip" and city_hint and city_hint not in place_city and place_city not in city_hint)
            if not place or needs_refresh:
                try:
                    geo = provider.geocode(row["name"], city_hint)
                except QuotaExceededError:
                    raise
                except RuntimeError:
                    if provider.is_mock:
                        raise
                    geo = MockMapProvider().geocode(row["name"], row["name"])
                    degraded = True
                place_id = _id("place")
                with self.connection() as db:
                    if needs_refresh:
                        db.execute(
                            "UPDATE place_snapshots SET city=?,latitude=?,longitude=?,coordinate_source=?,visited_at=? WHERE id=?",
                            (geo.get("city", city_hint), geo["latitude"], geo["longitude"], geo["source"], row["visited_at"], place["id"]),
                        )
                    else:
                        db.execute("INSERT OR IGNORE INTO place_snapshots(id,couple_id,resource_type,resource_id,name,city,latitude,longitude,coordinate_source,visited_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (place_id, couple_id, row["resource_type"], row["id"], row["name"], geo.get("city", city_hint), geo["latitude"], geo["longitude"], geo["source"], row["visited_at"], _now()))
                    saved = db.execute("SELECT * FROM place_snapshots WHERE couple_id=? AND resource_type=? AND resource_id=? AND name=?", (couple_id, row["resource_type"], row["id"], row["name"])).fetchone()
                    place = dict(saved)
                existing[key] = place
            items.append(place)
        is_mock = provider.is_mock or degraded
        return {"items": items, "provider": "mock" if degraded else provider.name, "mock": is_mock, "degraded": degraded, "notice": "高德暂时不可用，显示模拟坐标" if degraded else "模拟坐标，仅用于界面测试" if provider.is_mock else "坐标来自高德地图"}

    def annual_report(self, year: int, user_id: str) -> dict:
        if not 2000 <= int(year) <= 2100:
            raise ValueError("年份超出范围")
        prefix = f"{int(year):04d}"
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            moments = db.execute("SELECT COUNT(*) n,COUNT(DISTINCT location) cities FROM moments WHERE couple_id=? AND substr(occurred_at,1,4)=? AND deleted_at IS NULL AND purged_at IS NULL AND (visibility='shared' OR creator_id=?)", (couple["id"], prefix, user_id)).fetchone()
            messages = db.execute("SELECT COUNT(*) n FROM messages WHERE couple_id=? AND substr(sent_at,1,4)=?", (couple["id"], prefix)).fetchone()["n"]
            calls = db.execute("SELECT COUNT(*) n,COALESCE(SUM(duration_seconds),0) seconds FROM call_records WHERE couple_id=? AND substr(started_at,1,4)=? AND state='ended'", (couple["id"], prefix)).fetchone()
            trips = db.execute(
                "SELECT COUNT(*) n,GROUP_CONCAT(DISTINCT COALESCE(NULLIF(arrival_city,''),destination)) destinations FROM trips WHERE couple_id=? AND deleted_at IS NULL AND substr(end_date,1,4)=? AND end_date<?",
                (couple["id"], prefix, date.today().isoformat()),
            ).fetchone()
            planned_trips = db.execute(
                "SELECT COUNT(*) n FROM trips WHERE couple_id=? AND deleted_at IS NULL AND substr(start_date,1,4)=? AND end_date>=?",
                (couple["id"], prefix, date.today().isoformat()),
            ).fetchone()["n"]
            completed = db.execute("SELECT COUNT(*) n FROM calendar_entries WHERE couple_id=? AND entry_type='todo' AND completed_at IS NOT NULL AND substr(completed_at,1,4)=?", (couple["id"], prefix)).fetchone()["n"]
        return {"year": int(year), "moments": moments["n"], "cities": moments["cities"], "messages": messages, "calls": calls["n"], "call_seconds": calls["seconds"], "trips": trips["n"], "planned_trips": planned_trips, "destinations": [value for value in str(trips["destinations"] or "").split(",") if value], "completed_todos": completed, "privacy": "旅行只统计已经结束的记录；未来或进行中的计划单独列出。私密内容只计入本人报告。"}

    # ---- Shared calendar and preference CRUD -------------------------

    def list_calendar_entries(self, user_id: str, entry_type: str | None = None) -> list[dict]:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            params: list[Any] = [couple["id"]]
            sql = "SELECT * FROM calendar_entries WHERE couple_id=? AND deleted_at IS NULL"
            if entry_type:
                if entry_type not in {"event", "anniversary", "todo"}:
                    raise ValueError("日历类型无效")
                sql += " AND entry_type=?"
                params.append(entry_type)
            sql += " ORDER BY completed_at IS NOT NULL, starts_at, created_at"
            rows = db.execute(sql, params).fetchall()
        return [self._public_calendar_entry(row) for row in rows]

    @staticmethod
    def _public_calendar_entry(row: sqlite3.Row | dict) -> dict:
        value = dict(row)
        participants = [
            str(value.get(key)) for key in ("participant_a_id", "participant_b_id")
            if value.get(key)
        ]
        value["participant_ids"] = participants
        value["blocks_time"] = bool(value.get("blocks_time", 0))
        value["all_day"] = bool(value.get("all_day", 0))
        value.pop("participant_a_id", None)
        value.pop("participant_b_id", None)
        return value

    @staticmethod
    def _calendar_values(
        payload: dict,
        current: sqlite3.Row | None = None,
        member_ids: set[str] | None = None,
        *,
        server_source_type: str | None = None,
    ) -> dict:
        def value(name: str, default: Any = "") -> Any:
            return payload[name] if name in payload else (current[name] if current else default)
        entry_type = str(value("entry_type", "event"))
        if entry_type not in {"event", "anniversary", "todo"}:
            raise ValueError("日历类型无效")
        title = str(value("title")).strip()[:160]
        if not title:
            raise ValueError("标题不能为空")
        raw_all_day = value("all_day", False)
        if isinstance(raw_all_day, bool):
            all_day = int(raw_all_day)
        elif isinstance(raw_all_day, int) and raw_all_day in {0, 1}:
            all_day = raw_all_day
        else:
            raise ValueError("全天标记必须是布尔值")
        starts_at_raw = value("starts_at")
        if all_day:
            try:
                start_date = date.fromisoformat(str(starts_at_raw)[:10])
            except ValueError as exc:
                raise ValueError("开始时间格式无效") from exc
            starts_at = start_date.isoformat()
        else:
            starts_at = _iso(starts_at_raw, "开始时间")
        ends_raw = value("ends_at", None)
        if all_day and ends_raw:
            try:
                end_date = date.fromisoformat(str(ends_raw)[:10])
            except ValueError as exc:
                raise ValueError("结束时间格式无效") from exc
            if end_date < start_date:
                raise ValueError("结束时间不能早于开始时间")
            # All-day ranges are persisted as [start,end). A same-day end is
            # the common UI expression for one full day, not an empty range.
            if end_date == start_date:
                end_date += timedelta(days=1)
            ends_at = end_date.isoformat()
        else:
            ends_at = _iso(ends_raw, "结束时间") if ends_raw else None
            if ends_at and ends_at < starts_at:
                raise ValueError("结束时间不能早于开始时间")
        recurrence = str(value("recurrence", "none"))
        if recurrence not in {"none", "daily", "weekly", "monthly", "yearly"}:
            raise ValueError("重复规则无效")
        if "participant_ids" in payload:
            raw_participants = payload.get("participant_ids")
        elif current is not None:
            raw_participants = [current["participant_a_id"], current["participant_b_id"]]
        else:
            raw_participants = []
        if not isinstance(raw_participants, list) or any(item is not None and not isinstance(item, str) for item in raw_participants):
            raise ValueError("参与人格式无效")
        participants = [item.strip() for item in raw_participants if item and item.strip()]
        if len(participants) > 2 or len(set(participants)) != len(participants):
            raise ValueError("参与人不能重复且最多两人")
        if member_ids is not None and not set(participants).issubset(member_ids):
            raise ValueError("参与人必须是当前情侣双方")
        raw_blocks_time = value("blocks_time", False)
        if isinstance(raw_blocks_time, bool):
            blocks_time = raw_blocks_time
        elif isinstance(raw_blocks_time, int) and raw_blocks_time in {0, 1}:
            blocks_time = bool(raw_blocks_time)
        else:
            raise ValueError("占用时间标记必须是布尔值")
        if blocks_time and not participants:
            raise ValueError("占用时间必须选择至少一位参与人")
        if blocks_time and not all_day and not ends_at:
            raise ValueError("占用时间的日程必须填写结束时间")
        current_source = str(current["source_type"] or "legacy_unknown") if current is not None else "human"
        requested_source = str(payload.get("source_type") or current_source)
        if requested_source != current_source:
            raise ValueError("日历来源由服务器维护，不能手动修改")
        if server_source_type is not None and current is not None and server_source_type != current_source:
            raise ValueError("已有日历来源不能被覆盖")
        source_type = str(server_source_type or current_source)
        if source_type not in {"human", "rules", "ai", "real_api", "cache", "mock", "legacy_unknown"}:
            raise ValueError("日历来源无效")
        return {
            "entry_type": entry_type, "title": title, "starts_at": starts_at,
            "ends_at": ends_at, "all_day": all_day, "recurrence": recurrence,
            "location": str(value("location", ""))[:200], "note": str(value("note", ""))[:2000],
            "participant_a_id": participants[0] if participants else None,
            "participant_b_id": participants[1] if len(participants) > 1 else None,
            "blocks_time": int(blocks_time), "source_type": source_type,
        }

    def create_calendar_entry(self, payload: dict, user_id: str) -> dict:
        entry_id, now = _id("calendar"), _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, partner_id = self._couple_context(db, user_id)
            previous = self._idempotent_result(db, couple["id"], user_id, "calendar.create", payload.get("idempotency_key"))
            if previous:
                return previous
            values = self._calendar_values(payload, member_ids={user_id, partner_id})
            db.execute("""INSERT INTO calendar_entries(
                id,couple_id,creator_id,entry_type,title,starts_at,ends_at,all_day,recurrence,
                location,note,participant_a_id,participant_b_id,blocks_time,source_type,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (entry_id, couple["id"], user_id, *values.values(), now, now))
            self._event(db, couple["id"], user_id, "calendar.created", entry_id, {"type": values["entry_type"]})
            row = db.execute("SELECT * FROM calendar_entries WHERE id=?", (entry_id,)).fetchone()
            result = self._public_calendar_entry(row)
            self._save_idempotent_result(db, couple["id"], user_id, "calendar.create", payload.get("idempotency_key"), result)
        return result

    def update_calendar_entry(self, entry_id: str, payload: dict, user_id: str) -> dict:
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM calendar_entries WHERE id=? AND couple_id=? AND deleted_at IS NULL", (entry_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("日历事项不存在")
            if payload.get("expected_version") is not None and int(payload["expected_version"]) != row["version"]:
                raise ConflictError("日历事项已被另一方修改，请刷新")
            values = self._calendar_values(payload, row, {user_id, partner_id})
            db.execute("""UPDATE calendar_entries SET
                entry_type=?,title=?,starts_at=?,ends_at=?,all_day=?,recurrence=?,location=?,note=?,
                participant_a_id=?,participant_b_id=?,blocks_time=?,source_type=?,
                version=version+1,updated_at=? WHERE id=?""", (*values.values(), _now(), entry_id))
            self._event(db, couple["id"], user_id, "calendar.updated", entry_id, {})
            updated = db.execute("SELECT * FROM calendar_entries WHERE id=?", (entry_id,)).fetchone()
        return self._public_calendar_entry(updated)

    def complete_calendar_entry(self, entry_id: str, completed: bool, expected_version: object, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM calendar_entries WHERE id=? AND couple_id=? AND deleted_at IS NULL", (entry_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("日历事项不存在")
            if row["entry_type"] != "todo":
                raise ValueError("只有待办可以标记完成")
            if expected_version is not None and int(expected_version) != row["version"]:
                raise ConflictError("待办已被另一方修改，请刷新")
            db.execute("UPDATE calendar_entries SET completed_at=?,version=version+1,updated_at=? WHERE id=?", (_now() if completed else None, _now(), entry_id))
            self._event(db, couple["id"], user_id, "calendar.updated", entry_id, {"completed": completed})
            updated = db.execute("SELECT * FROM calendar_entries WHERE id=?", (entry_id,)).fetchone()
        return self._public_calendar_entry(updated)

    def delete_calendar_entry(self, entry_id: str, expected_version: object, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM calendar_entries WHERE id=? AND couple_id=? AND deleted_at IS NULL", (entry_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("日历事项不存在")
            if expected_version is not None and int(expected_version) != row["version"]:
                raise ConflictError("日历事项已被另一方修改，请刷新")
            db.execute("UPDATE calendar_entries SET deleted_at=?,version=version+1,updated_at=? WHERE id=?", (_now(), _now(), entry_id))
            self._event(db, couple["id"], user_id, "calendar.deleted", entry_id, {})
        return {"id": entry_id, "deleted": True}

    def list_preferences(self, user_id: str) -> list[dict]:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            rows = db.execute("SELECT p.*,u.nickname AS subject_name FROM partner_preferences p JOIN users u ON u.id=p.subject_user_id WHERE p.couple_id=? AND p.deleted_at IS NULL ORDER BY p.category,p.updated_at DESC", (couple["id"],)).fetchall()
        return [dict(row) for row in rows]

    def create_preference(self, payload: dict, user_id: str) -> dict:
        category, label, value = (str(payload.get(key, "")).strip() for key in ("category", "label", "value"))
        if not category or not label or not value:
            raise ValueError("偏好分类、名称和值不能为空")
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            subject = str(payload.get("subject_user_id") or partner_id)
            if subject not in {user_id, partner_id}:
                raise PermissionError("偏好对象必须是情侣双方")
            pref_id, now = _id("preference"), _now()
            db.execute("INSERT INTO partner_preferences(id,couple_id,creator_id,subject_user_id,category,label,value,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (pref_id, couple["id"], user_id, subject, category[:60], label[:100], value[:500], str(payload.get("note", ""))[:1000], now, now))
            self._event(db, couple["id"], user_id, "preference.created", pref_id, {})
            row = db.execute("SELECT * FROM partner_preferences WHERE id=?", (pref_id,)).fetchone()
        return dict(row)

    def update_preference(self, pref_id: str, payload: dict, user_id: str) -> dict:
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM partner_preferences WHERE id=? AND couple_id=? AND deleted_at IS NULL", (pref_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("偏好不存在")
            if payload.get("expected_version") is not None and int(payload["expected_version"]) != row["version"]:
                raise ConflictError("偏好已被另一方修改，请刷新")
            subject = str(payload.get("subject_user_id", row["subject_user_id"]))
            if subject not in {user_id, partner_id}:
                raise PermissionError("偏好对象必须是情侣双方")
            values = [str(payload.get(key, row[key])).strip()[:limit] for key, limit in (("category", 60), ("label", 100), ("value", 500), ("note", 1000))]
            if not all(values[:3]):
                raise ValueError("偏好分类、名称和值不能为空")
            db.execute("UPDATE partner_preferences SET subject_user_id=?,category=?,label=?,value=?,note=?,version=version+1,updated_at=? WHERE id=?", (subject, *values, _now(), pref_id))
            self._event(db, couple["id"], user_id, "preference.updated", pref_id, {})
            updated = db.execute("SELECT * FROM partner_preferences WHERE id=?", (pref_id,)).fetchone()
        return dict(updated)

    def delete_preference(self, pref_id: str, expected_version: object, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM partner_preferences WHERE id=? AND couple_id=? AND deleted_at IS NULL", (pref_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("偏好不存在")
            if expected_version is not None and int(expected_version) != row["version"]:
                raise ConflictError("偏好已被另一方修改，请刷新")
            db.execute("UPDATE partner_preferences SET deleted_at=?,version=version+1,updated_at=? WHERE id=?", (_now(), _now(), pref_id))
            self._event(db, couple["id"], user_id, "preference.deleted", pref_id, {})
        return {"id": pref_id, "deleted": True}

    # ---- Account, devices and export ---------------------------------

    def list_devices(self, user_id: str, current_token: str | None = None) -> list[dict]:
        import hashlib
        current_hash = hashlib.sha256(current_token.encode()).hexdigest() if current_token else ""
        with self.connection() as db:
            rows = db.execute("SELECT id,token_hash,device_name,user_agent,ip_address,created_at,last_seen_at,expires_at,revoked_at FROM sessions WHERE user_id=? ORDER BY last_seen_at DESC", (user_id,)).fetchall()
        return [{key: value for key, value in dict(row).items() if key != "token_hash"} | {"current": row["token_hash"] == current_hash, "active": row["revoked_at"] is None and row["expires_at"] > _now()} for row in rows]

    def revoke_device(self, session_id: str, user_id: str, current_token: str | None = None) -> dict:
        import hashlib
        current_hash = hashlib.sha256(current_token.encode()).hexdigest() if current_token else ""
        with self.connection() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=? AND user_id=?", (session_id, user_id)).fetchone()
            if not row:
                raise KeyError("设备会话不存在")
            if row["token_hash"] == current_hash:
                raise ValueError("请使用退出登录结束当前设备，不要在这里撤销")
            db.execute("UPDATE sessions SET revoked_at=? WHERE id=?", (_now(), session_id))
            db.execute("UPDATE push_subscriptions SET active=0,updated_at=? WHERE session_id=?", (_now(), session_id))
        return {"id": session_id, "revoked": True}

    @staticmethod
    def _invalidate_expression_requests(db: sqlite3.Connection, couple_id: str, now: str) -> None:
        """Close in-flight private expression usage before invalidating its receipt."""
        rows = db.execute(
            """SELECT result_json FROM mutation_idempotency
            WHERE couple_id=? AND operation='expression.suggest'
              AND json_extract(result_json,'$._state')='pending'""",
            (couple_id,),
        ).fetchall()
        request_ids: set[str] = set()
        for row in rows:
            try:
                receipt = json.loads(str(row["result_json"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                continue
            request_id = receipt.get("_request_id") if isinstance(receipt, dict) else None
            if isinstance(request_id, str) and request_id:
                request_ids.add(request_id)
        for request_id in request_ids:
            db.execute(
                """UPDATE ai_usage_events SET status='failed',finished_at=?,
                failure_code='scope_changed'
                WHERE couple_id=? AND request_id=? AND status='started'""",
                (now, couple_id, request_id),
            )
        db.execute(
            "DELETE FROM mutation_idempotency WHERE couple_id=? AND operation='expression.suggest'",
            (couple_id,),
        )

    def unlink_couple(self, password: str, confirmation: str, user_id: str) -> dict:
        if confirmation != "解除情侣":
            raise ValueError("请输入“解除情侣”确认")
        with self.connection() as db:
            user = db.execute("SELECT username FROM users WHERE id=? AND deleted_at IS NULL", (user_id,)).fetchone()
        if not user:
            raise KeyError("用户不存在")
        try:
            self.authenticate(user["username"], password)
        except ValueError as exc:
            raise ValueError("当前密码不正确") from exc
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            now = _now()
            self._cleanup_incomplete_trip_negotiations(db, str(couple["id"]))
            db.execute("UPDATE couples SET status='unlinked',version=version+1 WHERE id=?", (couple["id"],))
            db.execute("UPDATE invites SET consumed_at=COALESCE(consumed_at,?) WHERE creator_id IN (?,?)", (now, couple["member_a"], couple["member_b"]))
            db.execute(
                "UPDATE push_subscriptions SET active=0,updated_at=? WHERE user_id IN (?,?)",
                (_now(), couple["member_a"], couple["member_b"]),
            )
            db.execute(
                "UPDATE push_outbox SET status='cancelled',updated_at=? WHERE user_id IN (?,?) AND status IN ('pending','processing')",
                (_now(), couple["member_a"], couple["member_b"]),
            )
            self.apply_foundation_lifecycle(db, event="unlink", couple_id=couple["id"])
            self.invalidate_foundation_scope(db, couple["id"])
            active_cook_ids = [row["id"] for row in db.execute(
                "SELECT id FROM cook_sessions WHERE couple_id=? AND status='active'", (couple["id"],),
            ).fetchall()]
            if active_cook_ids:
                placeholders = ",".join("?" for _ in active_cook_ids)
                db.execute(
                    f"UPDATE cook_timers SET status='cancelled',version=version+1,updated_at=? WHERE session_id IN ({placeholders}) AND status='running'",
                    (now, *active_cook_ids),
                )
                db.execute(
                    "UPDATE cook_sessions SET status='cancelled',version=version+1,updated_at=?,ended_at=? WHERE couple_id=? AND status='active'",
                    (now, now, couple["id"]),
                )
            db.execute(
                """UPDATE wishlist_claims SET status=CASE WHEN status='active' THEN 'cancelled' ELSE status END,
                    private_note='',version=version+1,updated_at=?,ended_at=COALESCE(ended_at,?)
                WHERE item_id IN (SELECT i.id FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id WHERE l.couple_id=?)""",
                (now, now, couple["id"]),
            )
            db.execute("DELETE FROM couple_statuses WHERE couple_id=?", (couple["id"],))
            db.execute(
                "DELETE FROM mutation_idempotency WHERE couple_id=? AND operation LIKE 'wishlist.claim%'",
                (couple["id"],),
            )
            self._invalidate_expression_requests(db, str(couple["id"]), now)
            self._audit(db, "couple.unlinked", "couple", couple["id"], actor_id=user_id)
        return {"unlinked": True, "couple_id": couple["id"]}

    def delete_account(self, password: str, confirmation: str, user_id: str) -> dict:
        if confirmation != "注销账号":
            raise ValueError("请输入“注销账号”确认")
        with self.connection() as db:
            user = db.execute("SELECT username FROM users WHERE id=? AND deleted_at IS NULL", (user_id,)).fetchone()
        if not user:
            raise KeyError("用户不存在")
        try:
            self.authenticate(user["username"], password)
        except ValueError as exc:
            raise ValueError("当前密码不正确") from exc
        now = _now()
        with self.connection() as db:
            couples = db.execute(
                "SELECT * FROM couples WHERE member_a=? OR member_b=?",
                (user_id, user_id),
            ).fetchall()
            for couple in couples:
                self._cleanup_incomplete_trip_negotiations(db, str(couple["id"]))
                if couple["status"] == "active":
                    db.execute(
                        "UPDATE couples SET status='account_deleted',version=version+1 WHERE id=?",
                        (couple["id"],),
                    )
                self.apply_foundation_lifecycle(
                    db, event="account_delete", couple_id=couple["id"], user_id=user_id
                )
                self.invalidate_foundation_scope(db, couple["id"])
                active_cook_ids = [row["id"] for row in db.execute(
                    "SELECT id FROM cook_sessions WHERE couple_id=? AND status='active'", (couple["id"],),
                ).fetchall()]
                if active_cook_ids:
                    placeholders = ",".join("?" for _ in active_cook_ids)
                    db.execute(
                        f"UPDATE cook_timers SET status='cancelled',version=version+1,updated_at=? WHERE session_id IN ({placeholders}) AND status='running'",
                        (now, *active_cook_ids),
                    )
                    db.execute(
                        "UPDATE cook_sessions SET status='cancelled',version=version+1,updated_at=?,ended_at=? WHERE couple_id=? AND status='active'",
                        (now, now, couple["id"]),
                    )
                db.execute(
                    """UPDATE wishlist_claims SET status=CASE WHEN status='active' THEN 'cancelled' ELSE status END,
                        private_note='',version=version+1,updated_at=?,ended_at=COALESCE(ended_at,?)
                    WHERE item_id IN (
                        SELECT i.id FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id WHERE l.couple_id=?
                    )""",
                    (now, now, couple["id"]),
                )
                db.execute("DELETE FROM couple_statuses WHERE couple_id=?", (couple["id"],))
                db.execute(
                    "DELETE FROM mutation_idempotency WHERE couple_id=? AND operation LIKE 'wishlist.claim%'",
                    (couple["id"],),
                )
                self._invalidate_expression_requests(db, str(couple["id"]), now)
            db.execute("UPDATE moments SET title='已删除的私密内容',body='',location=NULL,mood=NULL,media_url=NULL,purged_at=?,deleted_at=COALESCE(deleted_at,?) WHERE creator_id=? AND visibility!='shared'", (now, now, user_id))
            db.execute("UPDATE recipes SET name='已删除的私密菜谱',deleted_at=COALESCE(deleted_at,?),updated_at=? WHERE owner_id=? AND visibility='private'", (now, now, user_id))
            db.execute("UPDATE recipe_drafts SET status='expired' WHERE creator_id=? AND status='pending'", (user_id,))
            db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=?", (now, user_id))
            db.execute("UPDATE push_subscriptions SET active=0,updated_at=? WHERE user_id=?", (now, user_id))
            db.execute(
                "UPDATE push_outbox SET status='cancelled',updated_at=? WHERE user_id=? AND status IN ('pending','processing')",
                (now, user_id),
            )
            db.execute("UPDATE invites SET consumed_at=COALESCE(consumed_at,?) WHERE creator_id=?", (now, user_id))
            db.execute("UPDATE users SET username=?,phone=NULL,password_hash=NULL,nickname='已注销用户',avatar_label='已',deleted_at=? WHERE id=?", (f"deleted_{uuid.uuid4().hex}", now, user_id))
        return {"deleted": True}

    def account_export(self, user_id: str) -> tuple[bytes, str]:
        """Return a ZIP containing JSON data and all media the user may access."""
        policy_status = account_export_policy_status()
        if not policy_status["ok"]:
            raise RuntimeError("账户导出策略清单不完整，已停止导出")
        with self.connection() as db:
            user = db.execute("SELECT id,username,nickname,locale,timezone,created_at FROM users WHERE id=?", (user_id,)).fetchone()
            if not user:
                raise KeyError("用户不存在")
            couple, partner_id = self._memory_couple_context(db, user_id)
            self._reconcile_delayed_moments(db)
            partner = db.execute("SELECT id,username,nickname,created_at FROM users WHERE id=?", (partner_id,)).fetchone()
            data: dict[str, Any] = {
                "exported_at": _now(),
                "user": dict(user),
                "partner": dict(partner),
                "couple": dict(couple),
            }
            direct_tables = [
                "messages", "call_records", "trips", "trip_assistant_messages",
                "trip_assistant_action_drafts", "trip_memory_drafts", "shared_lists",
                "calendar_entries", "partner_preferences", "place_snapshots",
                "quote_snapshots", "couple_statuses", "weekly_reviews", "date_plans",
                "meal_wishes", "meal_carts", "meal_cart_items", "shopping_lists",
                "daily_couple_summaries",
            ]
            for table in direct_tables:
                if table == "trips":
                    rows = db.execute(
                        f"SELECT t.* FROM trips t WHERE {self._visible_trip_sql('t')}",
                        (couple["id"],),
                    ).fetchall()
                elif table == "trip_assistant_messages":
                    rows = db.execute(
                        f"""SELECT m.* FROM trip_assistant_messages m
                            JOIN trips t ON t.id=m.trip_id
                            WHERE {self._visible_trip_sql('t')}""",
                        (couple["id"],),
                    ).fetchall()
                elif table == "trip_assistant_action_drafts":
                    rows = db.execute(
                        f"""SELECT d.* FROM trip_assistant_action_drafts d
                            JOIN trips t ON t.id=d.trip_id
                            WHERE {self._visible_trip_sql('t')}
                              AND (d.creator_id=? OR d.status='confirmed')""",
                        (couple["id"], user_id),
                    ).fetchall()
                elif table == "trip_memory_drafts":
                    rows = db.execute(
                        f"""SELECT d.* FROM trip_memory_drafts d
                            JOIN trips t ON t.id=d.trip_id
                            WHERE {self._visible_trip_sql('t')}
                              AND (d.creator_id=? OR d.status='confirmed')""",
                        (couple["id"], user_id),
                    ).fetchall()
                elif table == "calendar_entries":
                    rows = db.execute(
                        "SELECT * FROM calendar_entries WHERE couple_id=? AND deleted_at IS NULL",
                        (couple["id"],),
                    ).fetchall()
                elif table == "meal_wishes":
                    rows = db.execute(
                        "SELECT * FROM meal_wishes WHERE couple_id=? AND deleted_at IS NULL",
                        (couple["id"],),
                    ).fetchall()
                else:
                    rows = db.execute(f"SELECT * FROM {table} WHERE couple_id=?", (couple["id"],)).fetchall()
                if table == "trip_assistant_messages":
                    allowed = (
                        "id", "trip_id", "author_id", "role", "content", "intent_type",
                        "cards_json", "reply_to", "created_at",
                    )
                    data[table] = [{key: row[key] for key in allowed} for row in rows]
                elif table == "trip_assistant_action_drafts":
                    allowed = (
                        "id", "trip_id", "creator_id", "action_type", "payload_json",
                        "preview_json", "expected_trip_version", "expected_item_version",
                        "expected_item_versions_json", "status", "result_json", "expires_at",
                        "created_at", "confirmed_at",
                    )
                    data[table] = [{key: row[key] for key in allowed} for row in rows]
                elif table == "trip_memory_drafts":
                    allowed = (
                        "id", "trip_id", "creator_id", "title", "body", "location",
                        "source_summary_json", "is_early", "expected_trip_version", "version",
                        "status", "result_json", "moment_id", "expires_at", "created_at",
                        "updated_at", "confirmed_at",
                    )
                    data[table] = [{key: row[key] for key in allowed} for row in rows]
                else:
                    data[table] = [dict(row) for row in rows]
            data["shared_comments"] = []
            for comment in db.execute(
                "SELECT * FROM shared_comments WHERE couple_id=? AND deleted_at IS NULL ORDER BY created_at,rowid",
                (couple["id"],),
            ).fetchall():
                try:
                    self._shared_resource(db, couple["id"], comment["resource_type"], comment["resource_id"])
                except KeyError:
                    continue
                data["shared_comments"].append(dict(comment))
            data["user_locations"] = [dict(row) for row in db.execute(
                "SELECT * FROM user_locations WHERE user_id=?", (user_id,),
            ).fetchall()]
            data["devices"] = [dict(row) for row in db.execute("SELECT id,device_name,user_agent,ip_address,expires_at,created_at,last_seen_at,revoked_at FROM sessions WHERE user_id=?", (user_id,)).fetchall()]
            data["invites"] = [dict(row) for row in db.execute("SELECT id,creator_id,expires_at,consumed_at FROM invites WHERE creator_id=?", (user_id,)).fetchall()]
            data["moments"] = [dict(row) for row in db.execute(
                f"SELECT m.* FROM moments m WHERE {self._visible_moment_sql('m')}",
                (couple["id"], user_id, _now()),
            ).fetchall()]
            visible_moment_ids = [str(row["id"]) for row in data["moments"]]
            data["moment_reactions"] = []
            if visible_moment_ids:
                placeholders = ",".join("?" for _ in visible_moment_ids)
                data["moment_reactions"] = [dict(row) for row in db.execute(
                    f"SELECT * FROM moment_reactions WHERE moment_id IN ({placeholders}) ORDER BY created_at,moment_id,user_id",
                    visible_moment_ids,
                ).fetchall()]
            settings = db.execute(
                "SELECT * FROM couple_assistant_settings WHERE couple_id=?",
                (couple["id"],),
            ).fetchone()
            data["couple_assistant_settings"] = [dict(settings)] if settings else []
            data["in_app_notifications"] = [dict(row) for row in db.execute(
                """SELECT id,actor_id,event_type,resource_type,resource_id,title,body,url,
                          read_at,archived_at,created_at
                   FROM in_app_notifications WHERE couple_id=? AND recipient_id=?
                   ORDER BY created_at,id""",
                (couple["id"], user_id),
            ).fetchall()]
            data["place_search_candidates"] = [dict(row) for row in db.execute(
                """SELECT id,query,region_hint,payload_json,expires_at,created_at
                   FROM place_search_candidates WHERE user_id=? ORDER BY created_at,id""",
                (user_id,),
            ).fetchall()]
            data["chat_imports"] = [dict(row) for row in db.execute("SELECT * FROM chat_imports WHERE couple_id=? AND creator_id=?", (couple["id"], user_id)).fetchall()]
            import_ids = [row["id"] for row in data["chat_imports"]]
            data["chat_import_items"] = []
            if import_ids:
                placeholders = ",".join("?" for _ in import_ids)
                data["chat_import_items"] = [dict(row) for row in db.execute(f"SELECT * FROM chat_import_items WHERE import_id IN ({placeholders})", import_ids).fetchall()]
            data["daily_answers"] = [dict(row) for row in db.execute(
                "SELECT * FROM daily_answers WHERE couple_id=? AND (user_id=? OR question_date IN (SELECT question_date FROM daily_answers WHERE couple_id=? GROUP BY question_date HAVING COUNT(DISTINCT user_id)>=2))",
                (couple["id"], user_id, couple["id"]),
            ).fetchall()]
            review_ids = [row["id"] for row in data["weekly_reviews"]]
            data["weekly_review_answers"] = []
            if review_ids:
                placeholders = ",".join("?" for _ in review_ids)
                data["weekly_review_answers"] = [dict(row) for row in db.execute(
                    f"SELECT a.* FROM weekly_review_answers a WHERE a.review_id IN ({placeholders}) AND (a.user_id=? OR a.review_id IN (SELECT review_id FROM weekly_review_answers GROUP BY review_id HAVING COUNT(DISTINCT user_id)>=2))",
                    (*review_ids, user_id),
                ).fetchall()]
            media_rows = []
            for media_row in db.execute("SELECT * FROM media_assets WHERE couple_id=? AND status='ready' AND purged_at IS NULL", (couple["id"],)).fetchall():
                try:
                    self._assert_media_visible(media_row, user_id)
                except PermissionError:
                    continue
                media_rows.append(media_row)
            data["media_assets"] = [self._media_public(row) for row in media_rows]
            trip_ids = [row["id"] for row in data["trips"]]
            variant_rows: list[sqlite3.Row] = []
            data["trip_expenses"] = []
            data["trip_generation_jobs"] = [dict(row) for row in db.execute(
                """SELECT id,user_id,status,trip_id,created_at,started_at,
                          completed_at,updated_at
                   FROM trip_generation_jobs
                   WHERE couple_id=? AND user_id=? ORDER BY created_at,id""",
                (couple["id"], user_id),
            ).fetchall()]
            data["trip_item_preferences"] = []
            data["trip_realism_checks"] = []
            data["trip_packing_items"] = []
            if trip_ids:
                placeholders = ",".join("?" for _ in trip_ids)
                for table in ("trip_preferences", "trip_destination_places", "plan_variants", "hotel_options", "plan_revisions", "approvals"):
                    key = "trip_id"
                    data[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE {key} IN ({placeholders})", trip_ids).fetchall()]
                for table in ("trip_food_recommendations", "trip_food_review_evidence"):
                    data[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE trip_id IN ({placeholders})", trip_ids).fetchall()]
                expense_names = self._expense_member_names(db, couple)
                for row in db.execute(
                    f"SELECT * FROM trip_expenses WHERE trip_id IN ({placeholders}) ORDER BY trip_id,occurred_on,id",
                    trip_ids,
                ).fetchall():
                    expense = self._public_trip_expense(row, expense_names)
                    expense["creator_id"] = str(row["creator_id"])
                    data["trip_expenses"].append(expense)
                variant_rows = db.execute(f"SELECT * FROM plan_variants WHERE trip_id IN ({placeholders})", trip_ids).fetchall()
                for trip_id in trip_ids:
                    trip_row = db.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
                    if not trip_row:
                        continue
                    itinerary: list[dict] = []
                    if trip_row["active_variant"]:
                        negotiation = self._serialize_trip_negotiation_in_connection(
                            db, couple, trip_row, user_id, partner_id,
                        )
                        if negotiation["state"] != "open":
                            data["trip_item_preferences"].append(negotiation)
                        itinerary = self._active_itinerary_snapshot(db, trip_row)
                    for check in db.execute(
                        "SELECT * FROM trip_realism_checks WHERE trip_id=? ORDER BY checked_at,id",
                        (trip_id,),
                    ).fetchall():
                        effective_status = str(check["status"])
                        if int(check["trip_version"]) != int(trip_row["version"]):
                            effective_status = "stale"
                        data["trip_realism_checks"].append(
                            self._serialize_realism_check(check, itinerary, effective_status)
                        )
                    data["trip_packing_items"].extend(
                        self._list_trip_packing_db(db, trip_id, couple)
                    )
            variant_ids = [row["id"] for row in variant_rows]
            data["trip_days"], data["itinerary_items"], data["itinerary_item_places"] = [], [], []
            if variant_ids:
                placeholders = ",".join("?" for _ in variant_ids)
                days = db.execute(f"SELECT * FROM trip_days WHERE variant_id IN ({placeholders})", variant_ids).fetchall()
                data["trip_days"] = [dict(row) for row in days]
                day_ids = [row["id"] for row in days]
                if day_ids:
                    day_placeholders = ",".join("?" for _ in day_ids)
                    data["itinerary_items"] = [dict(row) for row in db.execute(f"SELECT * FROM itinerary_items WHERE day_id IN ({day_placeholders})", day_ids).fetchall()]
                    item_ids = [row["id"] for row in data["itinerary_items"]]
                    if item_ids:
                        item_placeholders = ",".join("?" for _ in item_ids)
                        data["itinerary_item_places"] = [dict(row) for row in db.execute(
                            f"SELECT * FROM itinerary_item_places WHERE itinerary_item_id IN ({item_placeholders})", item_ids,
                        ).fetchall()]
            booking_targets = [row["id"] for row in data.get("hotel_options", [])] + [row["id"] for row in data["itinerary_items"]]
            data["booking_links"] = []
            if booking_targets:
                placeholders = ",".join("?" for _ in booking_targets)
                data["booking_links"] = [dict(row) for row in db.execute(f"SELECT * FROM booking_links WHERE target_id IN ({placeholders})", booking_targets).fetchall()]
            list_ids = [row["id"] for row in data["shared_lists"]]
            data["shared_list_items"] = []
            if list_ids:
                placeholders = ",".join("?" for _ in list_ids)
                data["shared_list_items"] = [dict(row) for row in db.execute(f"SELECT * FROM shared_list_items WHERE list_id IN ({placeholders}) AND deleted_at IS NULL", list_ids).fetchall()]
                data["wishlist_claims"] = [dict(row) for row in db.execute(
                    f"""SELECT c.id,c.item_id,c.claimant_id,c.status,c.reveal_claimant,c.version,
                               c.created_at,c.updated_at,c.ended_at
                    FROM wishlist_claims c JOIN shared_list_items i ON i.id=c.item_id
                    WHERE i.list_id IN ({placeholders}) AND c.claimant_id=?""",
                    (*list_ids, user_id),
                ).fetchall()]
            else:
                data["wishlist_claims"] = []
            date_plan_ids = [row["id"] for row in data["date_plans"]]
            for table in ("date_plan_variants", "date_plan_confirmations"):
                data[table] = []
            data["date_plan_votes"] = []
            if date_plan_ids:
                placeholders = ",".join("?" for _ in date_plan_ids)
                data["date_plan_variants"] = [dict(row) for row in db.execute(f"SELECT * FROM date_plan_variants WHERE date_plan_id IN ({placeholders})", date_plan_ids).fetchall()]
                data["date_plan_confirmations"] = [dict(row) for row in db.execute(f"SELECT * FROM date_plan_confirmations WHERE date_plan_id IN ({placeholders})", date_plan_ids).fetchall()]
                data["date_plan_votes"] = [dict(row) for row in db.execute(f"SELECT * FROM date_plan_votes WHERE date_plan_id IN ({placeholders}) AND (user_id=? OR revealed=1)", (*date_plan_ids, user_id)).fetchall()]

            decision_rows = db.execute(
                "SELECT * FROM decisions WHERE couple_id=? AND deleted_at IS NULL ORDER BY created_at,id",
                (couple["id"],),
            ).fetchall()
            data["decisions"] = self._serialize_decision_rows(
                db, decision_rows, user_id, couple=couple, partner_id=partner_id,
            )
            decision_ids = [str(row["id"]) for row in decision_rows]
            data["decision_options"], data["decision_votes"], data["decision_confirmations"] = [], [], []
            if decision_ids:
                placeholders = ",".join("?" for _ in decision_ids)
                data["decision_options"] = [dict(row) for row in db.execute(
                    f"SELECT decision_id,id,label,position FROM decision_options WHERE decision_id IN ({placeholders}) ORDER BY decision_id,position",
                    decision_ids,
                ).fetchall()]
                revealed_ids = {
                    str(row["id"]) for row in decision_rows
                    if self._effective_decision_status(row) in {
                        "revealed", "pending_confirmation", "confirmed", "converted",
                    }
                }
                vote_rows = db.execute(
                    f"""SELECT v.decision_id,v.user_id,v.option_id,v.voting_round,
                                v.submitted_at,v.version
                         FROM decision_votes v JOIN decisions d ON d.id=v.decision_id
                         WHERE v.decision_id IN ({placeholders}) AND v.voting_round=d.voting_round
                         ORDER BY v.decision_id,v.user_id,v.option_id""",
                    decision_ids,
                ).fetchall()
                data["decision_votes"] = [
                    dict(row) for row in vote_rows
                    if str(row["decision_id"]) in revealed_ids or str(row["user_id"]) == user_id
                ]
                data["decision_confirmations"] = [dict(row) for row in db.execute(
                    f"SELECT * FROM decision_confirmations WHERE decision_id IN ({placeholders}) ORDER BY decision_id,user_id",
                    decision_ids,
                ).fetchall()]

            data["food_profiles"] = self._food_profiles_in_connection(db, couple["id"])
            data["pantry_items"] = self._pantry_snapshot_in_connection(db, couple["id"])
            data["shopping_category_preferences"] = [dict(row) for row in db.execute(
                "SELECT * FROM shopping_category_preferences WHERE couple_id=? ORDER BY position,category",
                (couple["id"],),
            ).fetchall()]
            data["weekly_meal_plan_drafts"] = [
                self._weekly_draft_dict(row) for row in db.execute(
                    "SELECT * FROM weekly_meal_plan_drafts WHERE couple_id=? ORDER BY created_at,id",
                    (couple["id"],),
                ).fetchall()
            ]
            substitution_rows = db.execute(
                """SELECT * FROM ingredient_substitutions
                   WHERE couple_id=? AND creator_id=? ORDER BY created_at,id""",
                (couple["id"], user_id),
            ).fetchall()
            data["ingredient_substitutions"] = [
                self._substitution_public_item(db, row) for row in substitution_rows
            ]

            plan_rows = db.execute(
                """SELECT * FROM life_assistant_plans
                   WHERE couple_id=? AND (creator_id=? OR status='applied')
                   ORDER BY created_at,id""",
                (couple["id"], user_id),
            ).fetchall()
            data["life_assistant_plans"] = [{
                "id": row["id"], "creator_id": row["creator_id"], "title": row["title"],
                "summary": row["summary"], "status": row["status"], "version": int(row["version"]),
                "source": row["source"], "expires_at": row["expires_at"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "applied_at": row["applied_at"],
            } for row in plan_rows]
            plan_ids = [str(row["id"]) for row in plan_rows]
            data["life_assistant_plan_items"], data["life_assistant_confirmations"] = [], []
            if plan_ids:
                placeholders = ",".join("?" for _ in plan_ids)
                data["life_assistant_plan_items"] = [dict(row) for row in db.execute(
                    f"SELECT id,plan_id,item_type,preview_json,position FROM life_assistant_plan_items WHERE plan_id IN ({placeholders}) ORDER BY plan_id,position",
                    plan_ids,
                ).fetchall()]
                data["life_assistant_confirmations"] = [dict(row) for row in db.execute(
                    f"SELECT * FROM life_assistant_confirmations WHERE plan_id IN ({placeholders}) ORDER BY plan_id,user_id",
                    plan_ids,
                ).fetchall()]
            data["life_assistant_jobs"] = [dict(row) for row in db.execute(
                """SELECT id,creator_id,status,result_plan_id,error_code,created_at,completed_at
                   FROM life_assistant_jobs WHERE couple_id=? AND creator_id=? ORDER BY created_at,id""",
                (couple["id"], user_id),
            ).fetchall()]
            data["ui_manifests"] = [dict(row) for row in db.execute(
                """SELECT id,user_id,surface,context_version,schema_version,manifest_json,
                          source,expires_at,created_at
                   FROM ui_manifests WHERE couple_id=? AND (user_id IS NULL OR user_id=?)""",
                (couple["id"], user_id),
            ).fetchall()]
            data["ui_generation_jobs"] = [dict(row) for row in db.execute(
                """SELECT id,surface,status,result_manifest_id,error_code,retry_count,
                          created_at,completed_at
                   FROM ui_generation_jobs WHERE couple_id=? AND user_id=?""",
                (couple["id"], user_id),
            ).fetchall()]
            data["ui_action_drafts"] = [dict(row) for row in db.execute(
                """SELECT id,action_type,payload_json,preview_json,expected_versions_json,
                          status,result_json,expires_at,created_at,confirmed_at
                   FROM ui_action_drafts WHERE couple_id=? AND creator_id=?""",
                (couple["id"], user_id),
            ).fetchall()]
            data["ai_context_grants"] = [dict(row) for row in db.execute(
                """
                SELECT id,surface,resource_type,resource_id,purpose,expires_at,created_at,
                       revoked_at,last_used_at,usage_scope,version
                FROM ai_context_grants WHERE couple_id=? AND user_id=?
                """,
                (couple["id"], user_id),
            ).fetchall()]
            data["ai_usage_summary"] = [dict(row) for row in db.execute(
                """
                SELECT feature,status,COUNT(*) AS calls,
                       SUM(input_tokens) AS known_input_tokens,
                       SUM(output_tokens) AS known_output_tokens,
                       SUM(CASE WHEN estimated_cost IS NOT NULL THEN estimated_cost ELSE 0 END) AS known_cost,
                       SUM(CASE WHEN estimated_cost IS NULL THEN 1 ELSE 0 END) AS unknown_cost_calls
                FROM ai_usage_events WHERE couple_id=? AND user_id=?
                GROUP BY feature,status ORDER BY feature,status
                """,
                (couple["id"], user_id),
            ).fetchall()]
            data.update(
                self.export_foundation_lifecycle_rows(
                    db, couple_id=couple["id"], user_id=user_id
                )
            )
            data["export_exclusions"] = {
                table: str(policy["reason"])
                for table, policy in ACCOUNT_EXPORT_POLICIES.items()
                if policy["strategy"] == "operational_excluded"
            }
            data["export_exclusions"].update({
                "ai_usage_events": "仅导出本人聚合用量，不导出逐次运营记录。",
                "external_api_usage": "外部接口额度属于整套服务的运营数据，不是任何单个账号的个人数据。",
                "route_cache": "路线缓存可能由双方或系统复用，仅导出已确认地点与行程，不导出原始缓存响应。",
                "recipe_provider_cache": "第三方菜谱缓存不属于账号内容，已导出的菜谱仍包含可携带字段。",
                "schema_migrations": "数据库迁移历史属于服务实现；导出格式版本记录在清单中。",
            })
            data["widget_snapshots"] = [dict(row) for row in db.execute("SELECT * FROM widget_snapshots WHERE user_id=?", (user_id,)).fetchall()]
            data["recipes"] = [dict(row) for row in db.execute(
                "SELECT * FROM recipes WHERE deleted_at IS NULL AND ((couple_id=? AND visibility='couple') OR (owner_id=? AND visibility='private'))",
                (couple["id"], user_id),
            ).fetchall()]
            recipe_ids = [row["id"] for row in data["recipes"]]
            data["recipe_dishes"], data["recipe_ingredients"], data["recipe_steps"] = [], [], []
            data["recipe_media"], data["recipe_media_checks"] = [], []
            if recipe_ids:
                placeholders = ",".join("?" for _ in recipe_ids)
                dish_ids = [str(row["dish_id"]) for row in data["recipes"] if row.get("dish_id")]
                if dish_ids:
                    dish_placeholders = ",".join("?" for _ in dish_ids)
                    data["recipe_dishes"] = [dict(row) for row in db.execute(
                        f"""SELECT * FROM recipe_dishes WHERE id IN ({dish_placeholders})
                             AND (visibility='system' OR couple_id=? OR owner_id=?)""",
                        (*dish_ids, couple["id"], user_id),
                    ).fetchall()]
                data["recipe_ingredients"] = [dict(row) for row in db.execute(f"SELECT * FROM recipe_ingredients WHERE recipe_id IN ({placeholders})", recipe_ids).fetchall()]
                data["recipe_steps"] = [dict(row) for row in db.execute(f"SELECT * FROM recipe_steps WHERE recipe_id IN ({placeholders})", recipe_ids).fetchall()]
                media = db.execute(
                    f"SELECT * FROM recipe_media WHERE recipe_id IN ({placeholders}) ORDER BY recipe_id,created_at,id",
                    recipe_ids,
                ).fetchall()
                data["recipe_media"] = [dict(row) for row in media]
                media_ids = [str(row["id"]) for row in media]
                if media_ids:
                    media_placeholders = ",".join("?" for _ in media_ids)
                    data["recipe_media_checks"] = [dict(row) for row in db.execute(
                        f"""SELECT id,media_id,checked_at,ok,status_code
                             FROM recipe_media_checks WHERE media_id IN ({media_placeholders})
                             ORDER BY media_id,checked_at,id""",
                        media_ids,
                    ).fetchall()]
            shopping_ids = [row["id"] for row in data["shopping_lists"]]
            data["shopping_list_items"], data["shopping_item_sources"] = [], []
            if shopping_ids:
                placeholders = ",".join("?" for _ in shopping_ids)
                data["shopping_list_items"] = [dict(row) for row in db.execute(f"SELECT * FROM shopping_list_items WHERE shopping_list_id IN ({placeholders})", shopping_ids).fetchall()]
                shopping_item_ids = [row["id"] for row in data["shopping_list_items"]]
                if shopping_item_ids:
                    item_placeholders = ",".join("?" for _ in shopping_item_ids)
                    data["shopping_item_sources"] = [dict(row) for row in db.execute(f"SELECT * FROM shopping_item_sources WHERE shopping_item_id IN ({item_placeholders})", shopping_item_ids).fetchall()]
            data["recipe_drafts"] = [dict(row) for row in db.execute("SELECT * FROM recipe_drafts WHERE couple_id=? AND creator_id=?", (couple["id"], user_id)).fetchall()]
            data["recipe_generation_jobs"] = [dict(row) for row in db.execute(
                """SELECT id,job_type,provider,status,result_draft_id,error_code,
                          created_at,completed_at
                   FROM recipe_generation_jobs WHERE couple_id=? AND user_id=?""",
                (couple["id"], user_id),
            ).fetchall()]
            data["cook_sessions"] = [dict(row) for row in db.execute(
                "SELECT * FROM cook_sessions WHERE couple_id=? AND (visibility='shared' OR creator_id=?) ORDER BY started_at,id",
                (couple["id"], user_id),
            ).fetchall()]
            cook_ids = [row["id"] for row in data["cook_sessions"]]
            data["cook_timers"] = []
            if cook_ids:
                placeholders = ",".join("?" for _ in cook_ids)
                data["cook_timers"] = [dict(row) for row in db.execute(
                    f"SELECT * FROM cook_timers WHERE session_id IN ({placeholders}) ORDER BY started_at,id", cook_ids,
                ).fetchall()]

            data["memory_completion_suggestions"] = []
            for row in db.execute(
                """SELECT * FROM memory_completion_suggestions
                   WHERE couple_id=? AND user_id=? ORDER BY created_at,id""",
                (couple["id"], user_id),
            ).fetchall():
                resource_type = "moment" if row["moment_id"] else "trip"
                resource_id = str(row["moment_id"] or row["trip_id"])
                try:
                    self._visible_memory_parent(db, resource_type, resource_id, user_id)
                except KeyError:
                    continue
                data["memory_completion_suggestions"].append(
                    self._public_completion_suggestion(row, resource_type, resource_id)
                )

            visible_media_ids = {str(row["id"]) for row in media_rows}
            visible_recipe_ids = {str(row["id"]) for row in data["recipes"]}
            visible_calendar_ids = {str(row["id"]) for row in data["calendar_entries"]}
            visible_list_item_ids = {str(row["id"]) for row in data["shared_list_items"]}
            visible_trip_ids = {str(row["id"]) for row in data["trips"]}
            visible_moment_id_set = set(visible_moment_ids)
            data["memory_provenance"] = []
            for row in db.execute(
                "SELECT * FROM memory_provenance WHERE couple_id=? ORDER BY created_at,id",
                (couple["id"],),
            ).fetchall():
                refs = {
                    "moment_id": visible_moment_id_set,
                    "media_id": visible_media_ids,
                    "trip_id": visible_trip_ids,
                    "recipe_id": visible_recipe_ids,
                    "calendar_entry_id": visible_calendar_ids,
                    "shared_list_item_id": visible_list_item_ids,
                }
                if not any(row[column] and str(row[column]) in allowed for column, allowed in refs.items()):
                    continue
                data["memory_provenance"].append({
                    "id": row["id"], "owner_id": row["owner_id"],
                    "moment_id": row["moment_id"], "media_id": row["media_id"],
                    "trip_id": row["trip_id"], "recipe_id": row["recipe_id"],
                    "calendar_entry_id": row["calendar_entry_id"],
                    "shared_list_item_id": row["shared_list_item_id"],
                    "source_type": row["source_type"], "date_source": row["date_source"],
                    "version": int(row["version"]), "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                })

            ingredient_ids = {
                str(item["ingredient_id"]) for item in data["pantry_items"] if item.get("ingredient_id")
            }
            for profile in data["food_profiles"]:
                ingredient_ids.update(
                    str(item["ingredient_id"]) for item in profile.get("avoidances", [])
                    if item.get("ingredient_id")
                )
            for row in substitution_rows:
                ingredient_ids.update((str(row["from_ingredient_id"]), str(row["to_ingredient_id"])))
            couple_ingredients = db.execute(
                "SELECT * FROM ingredient_catalog WHERE couple_id=? ORDER BY canonical_name,id",
                (couple["id"],),
            ).fetchall()
            ingredient_ids.update(str(row["id"]) for row in couple_ingredients)
            data["ingredient_catalog"] = []
            if ingredient_ids:
                placeholders = ",".join("?" for _ in ingredient_ids)
                data["ingredient_catalog"] = [dict(row) for row in db.execute(
                    f"""SELECT * FROM ingredient_catalog WHERE id IN ({placeholders})
                         AND (couple_id IS NULL OR couple_id=?) ORDER BY canonical_name,id""",
                    (*sorted(ingredient_ids), couple["id"]),
                ).fetchall()]
        dataset_counts = {
            key: len(value)
            for key, value in sorted(data.items())
            if isinstance(value, list)
        }
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("README.txt", "恋屿账号数据导出\n包含当前账号有权访问的共同数据、本人私密数据和媒体文件。\n不包含密码、会话令牌或伴侣的私密内容。\n")
            archive.writestr("data.json", json.dumps(data, ensure_ascii=False, indent=2))
            missing = []
            for row in media_rows:
                try:
                    provider = row["storage_provider"]
                    storage = self.local_storage if provider == "local" or (provider == "qiniu" and self.storage.name != "qiniu") else self.storage
                    if storage.name not in {provider, "local" if provider == "qiniu" else provider}:
                        raise RuntimeError("媒体存储未配置")
                    content = storage.get(row["storage_key"] or row["relative_path"])
                    safe_name = re.sub(r"[\x00-\x1f\x7f]", "_", Path(row["original_name"]).name).strip() or row["id"]
                    archive.writestr(f"media/{row['id']}-{safe_name}", content)
                except (KeyError, RuntimeError, OSError):
                    missing.append(row["id"])
            included_media_count = len(media_rows) - len(missing)
            archive.writestr("media-status.json", json.dumps({
                "authorized_media_count": len(media_rows),
                "included_media_count": included_media_count,
                "missing_media_count": len(missing),
                "missing": missing,
            }, ensure_ascii=False, indent=2))
            archive.writestr("manifest.json", json.dumps({
                "schema_version": 2,
                "export_policy_version": ACCOUNT_EXPORT_POLICY_VERSION,
                "export_policy_coverage": policy_status,
                "exported_at": data["exported_at"],
                "data_file": "data.json",
                "dataset_counts": dataset_counts,
                "media_count": len(media_rows),
                "included_media_count": included_media_count,
                "missing_media_count": len(missing),
                "missing_media": missing,
                "privacy": "包含双方共享内容、本人私密内容，以及按产品解锁规则当前可见的双人回答。",
            }, ensure_ascii=False, indent=2))
        return stream.getvalue(), f"love-moments-export-{date.today().isoformat()}.zip"
