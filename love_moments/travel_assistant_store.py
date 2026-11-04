"""Shared trip conversation backed by controlled domain tools."""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from time import monotonic
from typing import Any

from .intelligent_ui import ConflictError
from .trip_live_assistant import build_day_replan_preview, build_live_recommendations
from .trip_live_context import validate_live_context, wgs84_to_gcj02


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


class TravelAssistantMixin:
    @staticmethod
    def _assistant_clarification(message: str, options: list[dict] | None = None) -> list[dict]:
        """Return a compact, clickable follow-up instead of a text-only dead end."""
        return [{
            "type": "clarification",
            "message": str(message)[:500],
            "options": list(options or [])[:8],
        }]

    @staticmethod
    def _quote_source_metadata(row: dict) -> dict:
        mode = str(row.get("source_mode") or "").strip().lower()
        if bool(row.get("mock")) or mode == "mock":
            data_kind, label = "mock", "模拟数据"
        elif mode == "manual":
            data_kind, label = "manual", "手工录入"
        elif bool(row.get("stale")) or mode in {"cached", "cache"}:
            data_kind, label = "cache", "缓存结果"
        elif mode in {"live", "aggregate", "real_time"}:
            data_kind, label = "real_time", "实时查询"
        else:
            data_kind, label = "unknown", "来源待确认"
        return {"data_kind": data_kind, "source_label": label, "fetched_at": str(row.get("fetched_at") or "")}

    def _assistant_trip_context(self, trip_id: str, user_id: str) -> tuple[dict, dict]:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            trip = db.execute("SELECT * FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL", (trip_id, couple["id"])).fetchone()
            if not trip:
                raise KeyError("旅行不存在")
            place = db.execute("SELECT * FROM trip_destination_places WHERE trip_id=?", (trip_id,)).fetchone()
            items = db.execute(
                "SELECT i.id,i.title,i.start_time,i.duration_minutes,i.position,i.version,d.day_number,v.style "
                "FROM itinerary_items i JOIN trip_days d ON d.id=i.day_id JOIN plan_variants v ON v.id=d.variant_id "
                "WHERE v.trip_id=? ORDER BY CASE WHEN v.style=? THEN 0 ELSE 1 END,d.day_number,i.position",
                (trip_id, trip["active_variant"] or "slow"),
            ).fetchall()
        context = {
            "destination": trip["destination"],
            "confirmed_place": dict(place) if place else {},
            "itinerary_items": [dict(row) for row in items[:100]],
        }
        return dict(couple), {**dict(trip), "assistant_context": context}

    @staticmethod
    def _assistant_message(row: Any) -> dict:
        result = dict(row)
        result["cards"] = _json(result.pop("cards_json", "[]"), [])
        return result

    def list_trip_assistant(self, trip_id: str, user_id: str, limit: object = 80) -> dict:
        couple, trip = self._assistant_trip_context(trip_id, user_id)
        try:
            safe_limit = max(1, min(int(limit), 120))
        except (TypeError, ValueError):
            safe_limit = 80
        with self.connection() as db:
            rows = db.execute(
                "SELECT m.*,u.nickname AS author_name FROM trip_assistant_messages m "
                "LEFT JOIN users u ON u.id=m.author_id WHERE m.trip_id=? AND m.couple_id=? "
                "ORDER BY m.created_at DESC,CASE m.role WHEN 'assistant' THEN 0 ELSE 1 END,m.id DESC LIMIT ?",
                (trip_id, couple["id"], safe_limit),
            ).fetchall()
        items = [self._assistant_message(row) for row in reversed(rows)]
        draft_ids = {
            str(card.get("draft_id"))
            for item in items
            for card in item.get("cards", [])
            if card.get("type") in {
                "itinerary_change", "day_replan", "indoor_place_option", "place_replacement_option",
            } and card.get("draft_id")
        }
        draft_statuses: dict[str, str] = {}
        if draft_ids:
            placeholders = ",".join("?" for _ in draft_ids)
            with self.connection() as db:
                draft_statuses = {
                    row["id"]: row["status"]
                    for row in db.execute(
                        f"SELECT id,status FROM trip_assistant_action_drafts WHERE couple_id=? AND id IN ({placeholders})",  # noqa: S608
                        (couple["id"], *sorted(draft_ids)),
                    ).fetchall()
                }
        for item in items:
            for card in item.get("cards", []):
                if card.get("draft_id") in draft_statuses:
                    card["status"] = draft_statuses[card["draft_id"]]
        recap_ids = {
            str(card.get("id"))
            for item in items
            for card in item.get("cards", [])
            if card.get("type") == "trip_recap" and card.get("id")
        }
        recap_drafts: dict[str, dict] = {}
        if recap_ids:
            placeholders = ",".join("?" for _ in recap_ids)
            with self.connection() as db:
                recap_drafts = {
                    row["id"]: self._memory_draft(row)
                    for row in db.execute(
                        f"SELECT * FROM trip_memory_drafts WHERE couple_id=? AND id IN ({placeholders})",  # noqa: S608
                        (couple["id"], *sorted(recap_ids)),
                    ).fetchall()
                }
        for item in items:
            item["cards"] = [
                {"type": "trip_recap", **recap_drafts[card["id"]]}
                if card.get("type") == "trip_recap" and card.get("id") in recap_drafts else card
                for card in item.get("cards", [])
            ]
        return {"trip_id": trip_id, "trip_version": trip["version"], "items": items, "provider": self.travel_assistant.name}

    def _assistant_plan_cards(self, trip_id: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT v.style,v.name,v.summary,v.estimated_budget_cents,v.intensity,v.transport_minutes,"
                "COUNT(DISTINCT d.id) AS day_count,COUNT(i.id) AS item_count "
                "FROM plan_variants v LEFT JOIN trip_days d ON d.variant_id=v.id LEFT JOIN itinerary_items i ON i.day_id=d.id "
                "WHERE v.trip_id=? GROUP BY v.id ORDER BY CASE v.style WHEN 'slow' THEN 1 WHEN 'classic' THEN 2 WHEN 'value' THEN 3 WHEN 'food' THEN 4 WHEN 'nature' THEN 5 ELSE 6 END",
                (trip_id,),
            ).fetchall()
        return [{"type": "plan_variant", **dict(row)} for row in rows]

    @staticmethod
    def _memory_draft(row: Any) -> dict:
        result = dict(row)
        result["source_summary"] = _json(result.pop("source_summary_json", "{}"), {})
        result["result"] = _json(result.pop("result_json", None), None)
        result["is_early"] = bool(result.get("is_early"))
        result["notice"] = (
            "旅行尚未结束，这是提前草稿；请在旅行结束后补充真实经历再确认。"
            if result["is_early"] else "草稿只整理共同旅行数据，确认后才会进入双方时光轴。"
        )
        return result

    def list_trip_recap_drafts(self, trip_id: str, user_id: str) -> dict:
        couple, _ = self._assistant_trip_context(trip_id, user_id)
        now = _now()
        with self.connection() as db:
            db.execute(
                "UPDATE trip_memory_drafts SET status='expired',updated_at=? "
                "WHERE trip_id=? AND couple_id=? AND status='draft' AND expires_at<=?",
                (now, trip_id, couple["id"], now),
            )
            rows = db.execute(
                "SELECT * FROM trip_memory_drafts WHERE trip_id=? AND couple_id=? ORDER BY created_at DESC",
                (trip_id, couple["id"]),
            ).fetchall()
        return {"trip_id": trip_id, "items": [self._memory_draft(row) for row in rows]}

    def create_trip_recap_draft(self, trip_id: str, user_id: str) -> dict:
        couple, trip = self._assistant_trip_context(trip_id, user_id)
        now = _now()
        with self.connection() as db:
            existing = db.execute(
                "SELECT * FROM trip_memory_drafts WHERE trip_id=? AND couple_id=? AND status='draft' "
                "AND expires_at>? AND expected_trip_version=? ORDER BY created_at DESC LIMIT 1",
                (trip_id, couple["id"], now, trip["version"]),
            ).fetchone()
            if existing:
                return self._memory_draft(existing)
            db.execute(
                "UPDATE trip_memory_drafts SET status='expired',updated_at=? "
                "WHERE trip_id=? AND couple_id=? AND status='draft'",
                (now, trip_id, couple["id"]),
            )
            items = db.execute(
                "SELECT d.day_number,d.date,i.id,i.start_time,i.title,p.name AS place_name "
                "FROM plan_variants v JOIN trip_days d ON d.variant_id=v.id "
                "JOIN itinerary_items i ON i.day_id=d.id "
                "LEFT JOIN itinerary_item_places p ON p.itinerary_item_id=i.id "
                "WHERE v.trip_id=? AND v.style=? ORDER BY d.day_number,i.position LIMIT 40",
                (trip_id, trip.get("active_variant") or "slow"),
            ).fetchall()
            item_ids = [row["id"] for row in items]
            item_placeholders = ",".join("?" for _ in item_ids) or "''"
            comments = db.execute(
                "SELECT body FROM shared_comments WHERE couple_id=? AND deleted_at IS NULL AND "
                "((resource_type='trip' AND resource_id=?) OR (resource_type='itinerary_item' AND resource_id IN "
                f"({item_placeholders}))) ORDER BY created_at LIMIT 8",  # noqa: S608
                (couple["id"], trip_id, *item_ids),
            ).fetchall()
            destination_place = db.execute(
                "SELECT name,formatted_address FROM trip_destination_places WHERE trip_id=?", (trip_id,),
            ).fetchone()
            title = f"{trip['title']} · 共同回忆"
            lines = [
                f"{trip['start_date']} 至 {trip['end_date']}，我们一起去了{trip['destination']}。",
                "", "这次旅程：",
            ]
            for row in items[:16]:
                place_text = f" · {row['place_name']}" if row["place_name"] else ""
                lines.append(f"- 第{row['day_number']}天 {row['start_time']} {row['title']}{place_text}")
            if comments:
                lines.extend(["", "共同留下的话："])
                lines.extend(f"- {str(row['body'])[:240]}" for row in comments)
            body = "\n".join(lines)[:5000]
            place_names = list(dict.fromkeys(str(row["place_name"]) for row in items if row["place_name"]))
            location = str(destination_place["name"] if destination_place else trip["destination"])
            is_early = date.today() <= date.fromisoformat(str(trip["end_date"])[:10])
            source_summary = {
                "trip_id": trip_id, "active_variant": trip.get("active_variant") or "slow",
                "itinerary_item_count": len(items), "confirmed_places": place_names[:20],
                "shared_comment_count": len(comments), "private_moments_read": False,
            }
            draft_id = _id("trip_memory")
            expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0).isoformat()
            db.execute(
                "INSERT INTO trip_memory_drafts(id,trip_id,couple_id,creator_id,title,body,location,"
                "source_summary_json,is_early,expected_trip_version,status,expires_at,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,'draft',?,?,?)",
                (
                    draft_id, trip_id, couple["id"], user_id, title[:200], body, location[:200],
                    json.dumps(source_summary, ensure_ascii=False), int(is_early), trip["version"],
                    expires_at, now, now,
                ),
            )
            self._audit(db, "trip.recap_draft_created", "trip_memory_draft", draft_id, {
                "trip_id": trip_id, "is_early": is_early, "private_moments_read": False,
            }, actor_id=user_id)
            self._event(db, couple["id"], user_id, "trip.recap_updated", draft_id, {
                "resource": "trip_memory_draft", "trip_id": trip_id,
            })
            row = db.execute("SELECT * FROM trip_memory_drafts WHERE id=?", (draft_id,)).fetchone()
        return self._memory_draft(row)

    def update_trip_recap_draft(self, trip_id: str, draft_id: str, payload: dict, user_id: str) -> dict:
        couple, _ = self._assistant_trip_context(trip_id, user_id)
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM trip_memory_drafts WHERE id=? AND trip_id=? AND couple_id=?",
                (draft_id, trip_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("旅行回忆草稿不存在")
            if row["status"] != "draft" or row["expires_at"] <= _now():
                raise ConflictError("旅行回忆草稿已经失效")
            try:
                expected = int(payload.get("expected_version"))
            except (TypeError, ValueError) as exc:
                raise ValueError("草稿版本无效") from exc
            if expected != int(row["version"]):
                raise ConflictError("草稿已经被另一方修改，请刷新后重试")
            title = str(payload.get("title", row["title"])).strip()[:200]
            body = str(payload.get("body", row["body"])).strip()[:5000]
            if not title or not body:
                raise ValueError("回忆标题和正文不能为空")
            db.execute(
                "UPDATE trip_memory_drafts SET title=?,body=?,version=version+1,updated_at=? WHERE id=?",
                (title, body, _now(), draft_id),
            )
            updated = db.execute("SELECT * FROM trip_memory_drafts WHERE id=?", (draft_id,)).fetchone()
        return self._memory_draft(updated)

    def confirm_trip_recap_draft(
        self, trip_id: str, draft_id: str, idempotency_key: object, user_id: str,
    ) -> dict:
        key = str(idempotency_key or "").strip()
        if not 8 <= len(key) <= 128:
            raise ValueError("幂等键长度需为 8–128 个字符")
        couple, _ = self._assistant_trip_context(trip_id, user_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT result_json FROM trip_memory_drafts WHERE couple_id=? AND idempotency_key=? AND status='confirmed'",
                (couple["id"], key),
            ).fetchone()
            if previous:
                return _json(previous["result_json"], {})
            draft = db.execute(
                "SELECT * FROM trip_memory_drafts WHERE id=? AND trip_id=? AND couple_id=?",
                (draft_id, trip_id, couple["id"]),
            ).fetchone()
            if not draft:
                raise KeyError("旅行回忆草稿不存在")
            if draft["status"] == "confirmed":
                return _json(draft["result_json"], {})
            if draft["status"] != "draft" or draft["expires_at"] <= _now():
                raise ConflictError("旅行回忆草稿已经失效")
            trip = db.execute(
                "SELECT version,end_date FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL",
                (trip_id, couple["id"]),
            ).fetchone()
            if not trip or int(trip["version"]) != int(draft["expected_trip_version"]):
                raise ConflictError("旅行内容已经变化，请重新生成回忆草稿")
            if date.today() <= date.fromisoformat(str(trip["end_date"])[:10]):
                raise ConflictError("请在旅行结束后确认进入时光轴；现在可以继续编辑提前草稿")
            moment_id = _id("moment")
            occurred_at = f"{trip['end_date']}T20:00:00+08:00"
            db.execute(
                "INSERT INTO moments(id,couple_id,creator_id,type,title,body,occurred_at,location,visibility) "
                "VALUES (?,?,?,'trip',?,?,?,?,'shared')",
                (moment_id, couple["id"], user_id, draft["title"], draft["body"], occurred_at, draft["location"]),
            )
            moment = dict(db.execute("SELECT * FROM moments WHERE id=?", (moment_id,)).fetchone())
            result = {"draft_id": draft_id, "moment": moment}
            now = _now()
            cursor = db.execute(
                "UPDATE trip_memory_drafts SET status='confirmed',idempotency_key=?,result_json=?,moment_id=?,"
                "confirmed_at=?,updated_at=? WHERE id=? AND status='draft'",
                (key, json.dumps(result, ensure_ascii=False), moment_id, now, now, draft_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("旅行回忆草稿已被取消或确认")
            self._audit(db, "trip.recap_confirmed", "moment", moment_id, {"trip_id": trip_id, "draft_id": draft_id}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "moment.created", moment_id, {"type": "trip", "trip_id": trip_id})
        return result

    def cancel_trip_recap_draft(self, trip_id: str, draft_id: str, user_id: str) -> dict:
        couple, _ = self._assistant_trip_context(trip_id, user_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM trip_memory_drafts WHERE id=? AND trip_id=? AND couple_id=?",
                (draft_id, trip_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("旅行回忆草稿不存在")
            if row["status"] == "confirmed":
                raise ConflictError("已经进入时光轴的回忆不能取消草稿")
            if row["status"] == "draft":
                db.execute("UPDATE trip_memory_drafts SET status='cancelled',updated_at=? WHERE id=?", (_now(), draft_id))
            updated = db.execute("SELECT * FROM trip_memory_drafts WHERE id=?", (draft_id,)).fetchone()
        return self._memory_draft(updated)

    @staticmethod
    def _hotel_cards(result: dict, max_price: int | None) -> list[dict]:
        rows = list(result.get("items") or [])
        if max_price:
            rows = [row for row in rows if int(row.get("amount_cents") or 0) <= max_price]
        cards = []
        for row in rows[:8]:
            cards.append({
                "type": "hotel_quote", "quote_id": row["id"], "title": row["title"],
                "subtitle": row.get("subtitle", ""), "amount_cents": row.get("amount_cents", 0),
                "currency": row.get("currency", "CNY"), "provider": row.get("provider", ""),
                "source_mode": row.get("source_mode", ""), "terms": row.get("terms", ""),
                "includes_tax": bool(row.get("includes_tax")), "booking_url": row.get("booking_url", ""),
                "fetched_at": row.get("fetched_at", ""), "stale": bool(row.get("stale")),
                "original_title": (row.get("details") or {}).get("original_title", ""),
                "booking_mode": (row.get("details") or {}).get("booking_mode", ""),
                "booking_label": (row.get("details") or {}).get("booking_label", ""),
                "booking_copy_text": (row.get("details") or {}).get("booking_copy_text", ""),
                **TravelAssistantMixin._quote_source_metadata(row),
            })
        return cards

    def live_trip_status(
        self, trip_id: str, payload: dict, user_id: str, weather_provider: Any = None,
    ) -> dict:
        context = validate_live_context(payload.get("live_context"))
        trip = self.get_trip(trip_id, user_id=user_id)
        weather = weather_provider.current(context.latitude, context.longitude) if weather_provider else None
        live = build_live_recommendations(trip, context, weather)
        anchor = trip.get("destination_place")
        if not anchor and live["candidates"]:
            anchor = live["candidates"][0].get("place")
        provider = self._map_provider_for_places(anchor)
        restaurants: list[dict] = []
        restaurant_notice = "未请求当前位置附近餐厅。"
        if bool(payload.get("include_restaurants")):
            if provider.is_mock:
                restaurant_notice = "未配置真实地点服务，因此没有虚构当前位置附近的餐厅。"
            else:
                latitude, longitude = context.latitude, context.longitude
                if provider.name == "amap":
                    latitude, longitude = wgs84_to_gcj02(latitude, longitude)
                keyword = str(payload.get("restaurant_keyword") or "当地特色|高评分餐厅")[:120]
                cache_key = (provider.name, round(latitude, 3), round(longitude, 3), keyword)
                now_tick = monotonic()
                with self._live_restaurant_cache_lock:
                    cached = self._live_restaurant_cache.get(cache_key)
                    if cached and float(cached["expires_at"]) > now_tick:
                        rows = json.loads(json.dumps(cached["rows"], ensure_ascii=False))
                        restaurant_cache_hit = True
                        restaurant_fetched_at = str(cached["fetched_at"])
                    else:
                        if cached:
                            self._live_restaurant_cache.pop(cache_key, None)
                        rows = []
                        restaurant_cache_hit = False
                        restaurant_fetched_at = ""
                if not restaurant_cache_hit:
                    rows = provider.search_food_nearby(keyword, latitude, longitude, limit=8)
                    restaurant_fetched_at = _now()
                    with self._live_restaurant_cache_lock:
                        self._live_restaurant_cache[cache_key] = {
                            "expires_at": monotonic() + 6 * 60 * 60,
                            "fetched_at": restaurant_fetched_at,
                            "rows": json.loads(json.dumps(rows, ensure_ascii=False)),
                        }
                        if len(self._live_restaurant_cache) > 128:
                            expired = [key for key, value in self._live_restaurant_cache.items() if float(value["expires_at"]) <= monotonic()]
                            for key in expired:
                                self._live_restaurant_cache.pop(key, None)
                base_label = "高德地点" if provider.name == "amap" else "Geoapify 地点"
                source_label = f"{base_label}缓存" if restaurant_cache_hit else f"{base_label}实时查询"
                restaurants = [
                    {
                        "type": "nearby_restaurant", **row, "source_label": source_label,
                        "data_kind": "cache" if restaurant_cache_hit else "real_time",
                        "fetched_at": restaurant_fetched_at,
                    }
                    for row in rows if not bool(row.get("mock"))
                ]
                restaurant_notice = (
                    "餐厅来自真实地点服务；相同位置 6 小时内复用缓存，评分、人均和营业时间缺失时会显示待确认。"
                    if restaurants else "真实地点服务在当前位置附近没有返回餐厅。"
                )
        return {
            **live, "restaurants": restaurants, "restaurant_notice": restaurant_notice,
            "map_provider": provider.name, "map_configured": not provider.is_mock,
            "location_persisted": False,
        }

    def live_trip_navigation(self, trip_id: str, payload: dict, user_id: str) -> dict:
        context = validate_live_context(payload.get("live_context"))
        item_id = str(payload.get("item_id") or "").strip()
        mode = str(payload.get("mode") or "walking").strip()
        if mode not in {"walking", "transit", "driving", "bicycling"}:
            raise ValueError("不支持的出行方式")
        trip = self.get_trip(trip_id, user_id=user_id)
        active = trip.get("active_variant") or "slow"
        item = next((
            item for variant in trip.get("variants", []) if variant.get("style") == active
            for day in variant.get("days", []) for item in day.get("items", []) if item.get("id") == item_id
        ), None)
        place = item.get("place") if isinstance(item, dict) and isinstance(item.get("place"), dict) else None
        if not item or not place or place.get("latitude") is None or place.get("longitude") is None:
            raise ValueError("请先为该行程确认具体地点")
        provider = self._map_provider_for_places(place)
        if provider.is_mock:
            return {
                "route": None, "map_provider": provider.name, "map_configured": False,
                "location_persisted": False, "notice": "未配置真实路线服务，无法生成当前位置到目的地的路线。",
            }
        latitude, longitude = context.latitude, context.longitude
        if provider.name == "amap":
            latitude, longitude = wgs84_to_gcj02(latitude, longitude)
        origin = {"name": "当前位置", "latitude": latitude, "longitude": longitude}
        route = provider.route_between(origin, place, mode, str(place.get("city") or trip.get("arrival_city") or ""))
        return {
            "route": route, "map_provider": provider.name, "map_configured": True,
            "location_persisted": False, "notice": "路线仅在本次点击时查询，当前位置未保存。",
        }

    @staticmethod
    def _restaurant_cards(trip: dict, intent: Any) -> list[dict]:
        candidates = []
        for variant in trip.get("variants", []):
            for day in variant.get("days", []):
                if intent.day_number and int(day["day_number"]) != intent.day_number:
                    continue
                for item in day.get("items", []):
                    for row in item.get("food_recommendations", []):
                        if intent.meal_type and row.get("meal_type") != intent.meal_type:
                            continue
                        if intent.min_rating is not None and (row.get("rating") is None or float(row["rating"]) < intent.min_rating):
                            continue
                        if intent.max_distance_meters is not None and (row.get("distance_meters") is None or int(row["distance_meters"]) > intent.max_distance_meters):
                            continue
                        candidates.append({
                            "type": "restaurant", "recommendation_id": row["id"], "itinerary_item_id": item["id"],
                            "day_number": day["day_number"], "meal_type": row["meal_type"], "name": row["name"],
                            "address": row.get("address", ""), "rating": row.get("rating"),
                            "average_cost_cents": row.get("average_cost_cents"), "distance_meters": row.get("distance_meters"),
                            "category": row.get("category", ""), "source": row.get("source", ""),
                            "navigation_url": row.get("navigation_url", ""), "fetched_at": row.get("fetched_at", ""),
                        })
        candidates.sort(key=lambda row: (row["rating"] is None, -(row["rating"] or 0), row["distance_meters"] is None, row["distance_meters"] or 999999))
        return candidates[:8]

    def _transport_comparison_cards(self, trip_id: str, trip: dict, user_id: str) -> tuple[list[dict], list[str]]:
        common = {
            "trip_id": trip_id, "origin": trip.get("departure_city") or "",
            "destination": trip.get("arrival_city") or trip.get("destination") or "",
            "start_date": trip.get("start_date"), "end_date": trip.get("end_date"),
            "adults": int(trip.get("travelers") or 2),
        }
        cards: list[dict] = []
        notices: list[str] = []
        for quote_type in ("hotel", "flight", "train"):
            try:
                result = self.search_quotes({**common, "type": quote_type}, user_id)
            except (KeyError, RuntimeError, ValueError) as error:
                cards.append({
                    "type": "quote_group", "quote_type": quote_type, "status": "unavailable",
                    "items": [], "message": str(error)[:300], "data_kind": "unavailable",
                    "source_label": "当前不可用", "fetched_at": "",
                })
                continue
            rows = []
            for row in list(result.get("items") or [])[:8]:
                rows.append({**row, **self._quote_source_metadata(row)})
            metadata = self._quote_source_metadata(rows[0]) if rows else {
                "data_kind": "unavailable", "source_label": "暂无结果", "fetched_at": "",
            }
            cards.append({
                "type": "quote_group", "quote_type": quote_type,
                "status": "ready" if rows else "empty", "items": rows,
                "message": str(result.get("notice") or ""), **metadata,
            })
            if result.get("notice"):
                notices.append(str(result["notice"]))
        return cards, notices

    def _assistant_itinerary_draft(self, trip_id: str, trip: dict, message: str, intent: Any, user_id: str, couple_id: str) -> list[dict]:
        if not intent.changes:
            return self._assistant_clarification(
                "我还不知道你想改时间、停留时长还是地点，请点选一项。",
                [
                    {"label": "调整时间", "prompt": "我要调整一项行程的开始时间"},
                    {"label": "调整停留时长", "prompt": "我要调整一项行程的停留时长"},
                    {"label": "更换景点或活动", "prompt": "我要换掉一个景点或活动"},
                ],
            )
        active = trip.get("active_variant") or "slow"
        available = []
        for variant in trip.get("variants", []):
            if variant["style"] != active:
                continue
            for day in variant.get("days", []):
                if intent.day_number and int(day["day_number"]) != intent.day_number:
                    continue
                for item in day.get("items", []):
                    available.append((day, item))
        if not available:
            return self._assistant_clarification("没有找到可调整的行程，请先在每日行程中添加或解锁一项。")
        hint = str(getattr(intent, "item_hint", "") or "").strip()
        exact = [
            pair for pair in available
            if pair[1]["title"] in message
            or (hint and (hint in pair[1]["title"] or pair[1]["title"] in hint))
        ]
        if len(exact) > 1:
            days = "、".join(f"第{pair[0]['day_number']}天" for pair in exact[:6])
            return self._assistant_clarification(
                f"这段旅行里有多条同名行程（{days}），请补充第几天，或直接点选具体日期。",
                [
                    {"label": f"第{day['day_number']}天 · {item['title']}", "prompt": f"第{day['day_number']}天，{message}"}
                    for day, item in exact[:8]
                ],
            )
        if len(exact) == 1:
            day, item = exact[0]
        else:
            time_candidates = available
            if "上午" in message or "早上" in message:
                time_candidates = [pair for pair in available if pair[1]["start_time"] < "12:00"]
            elif "下午" in message:
                time_candidates = [pair for pair in available if "12:00" <= pair[1]["start_time"] < "18:00"]
            elif "晚上" in message or "夜间" in message:
                time_candidates = [pair for pair in available if pair[1]["start_time"] >= "18:00"]
            if len(time_candidates) != 1:
                return self._assistant_clarification(
                    "还不能确定要修改哪一项，请点选原行程。",
                    [
                        {
                            "label": f"第{day['day_number']}天 {item['start_time']} · {item['title']}",
                            "prompt": f"把第{day['day_number']}天的{item['title']}{message}",
                        }
                        for day, item in time_candidates[:8]
                    ],
                )
            day, item = time_candidates[0]
        impact = self.impact_analysis(item["id"], intent.changes, user_id=user_id)
        before = {key: item.get(key) for key in intent.changes}
        preview = {"item_id": item["id"], "title": item["title"], "day_number": day["day_number"], "before": before, "after": intent.changes, "impact": impact}
        draft_id = _id("travel_draft")
        now = _now()
        with self.connection() as db:
            db.execute(
                "INSERT INTO trip_assistant_action_drafts(id,trip_id,couple_id,creator_id,action_type,payload_json,preview_json,expected_trip_version,expected_item_version,status,expires_at,created_at) "
                "VALUES (?,?,?,?, 'itinerary_change',?,?,?,?, 'draft',?,?)",
                (draft_id, trip_id, couple_id, user_id, json.dumps({"item_id": item["id"], "changes": intent.changes}, ensure_ascii=False), json.dumps(preview, ensure_ascii=False), trip["version"], item["version"], (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0).isoformat(), now),
            )
        return [{"type": "itinerary_change", "draft_id": draft_id, "status": "draft", **preview, "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0).isoformat()}]

    def _assistant_day_replan_draft(
        self,
        trip_id: str,
        trip: dict,
        intent: Any,
        message: str,
        user_id: str,
        couple_id: str,
        live_context: object = None,
        weather_provider: Any = None,
    ) -> list[dict]:
        context = validate_live_context(live_context) if live_context else None
        day_number = intent.day_number
        if day_number is None and context:
            try:
                day_number = (context.local_time.date() - date.fromisoformat(str(trip["start_date"])[:10])).days + 1
            except ValueError:
                day_number = None
        if day_number is None:
            active = trip.get("active_variant") or "slow"
            days = next((variant.get("days", []) for variant in trip.get("variants", []) if variant.get("style") == active), [])
            return self._assistant_clarification(
                "请先选择要重新安排哪一天。",
                [
                    {"label": f"第{day['day_number']}天 · {day.get('title') or day.get('date') or ''}", "prompt": f"第{day['day_number']}天，{message}"}
                    for day in days[:8]
                ],
            )
        day = next(
            (
                day
                for variant in trip.get("variants", []) if variant.get("style") == (trip.get("active_variant") or "slow")
                for day in variant.get("days", []) if int(day.get("day_number") or 0) == int(day_number)
            ),
            None,
        )
        if not day:
            return self._assistant_clarification("这次旅行没有对应日期，请重新选择有效日期。")
        target_date = date.fromisoformat(str(day.get("date") or trip.get("start_date"))[:10])
        reference_date = context.local_time.date() if context else date.today()
        if target_date < reference_date:
            return self._assistant_clarification("这一天已经过去，不能重新安排已发生的行程。")
        if target_date == reference_date and context is None:
            return self._assistant_clarification(
                "调整今天需要先点击“更新当前位置”，用于判断哪些项目已经开始；位置不会保存。",
                [{"label": "更新当前位置", "action": "trip-live-refresh"}],
            )
        if context:
            local_time = context.local_time
        else:
            local_time = datetime.combine(target_date, time.min, tzinfo=timezone(timedelta(hours=8)))
        lowered = message.lower()
        weather = None
        if any(word in lowered for word in ("下雨", "雨天", "有雨", "阵雨")):
            weather = {"weather_code": 61, "label": "用户说明可能下雨", "source": "user"}
        elif any(word in lowered for word in ("下雪", "雪天", "有雪")):
            weather = {"weather_code": 71, "label": "用户说明可能下雪", "source": "user"}
        elif context and weather_provider:
            weather = weather_provider.current(context.latitude, context.longitude)
        preview = build_day_replan_preview(
            trip, int(day_number), local_time,
            delay_minutes=int(intent.delay_minutes or 0), weather=weather,
        )
        if not preview["changes"]:
            message_text = preview["warnings"][0] if preview["warnings"] else "当前条件下没有需要调整的项目。"
            return self._assistant_clarification(message_text)
        item_versions = {row["item_id"]: row["item_version"] for row in preview["changes"]}
        payload = {
            "day_number": int(day_number),
            "changes": [{"item_id": row["item_id"], "changes": row["after"]} for row in preview["changes"]],
        }
        draft_id, created = _id("travel_draft"), _now()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0).isoformat()
        with self.connection() as db:
            db.execute(
                "INSERT INTO trip_assistant_action_drafts("
                "id,trip_id,couple_id,creator_id,action_type,payload_json,preview_json,expected_trip_version,"
                "expected_item_version,expected_item_versions_json,status,expires_at,created_at"
                ") VALUES (?,?,?,?, 'day_replan',?,?,?,?,?,'draft',?,?)",
                (
                    draft_id, trip_id, couple_id, user_id,
                    json.dumps(payload, ensure_ascii=False), json.dumps(preview, ensure_ascii=False),
                    trip["version"], 0, json.dumps(item_versions, ensure_ascii=False), expires_at, created,
                ),
            )
        return [{"type": "day_replan", "draft_id": draft_id, "status": "draft", **preview, "expires_at": expires_at}]

    def _assistant_place_replacement_cards(
        self,
        trip_id: str,
        trip: dict,
        day_number: int,
        user_id: str,
        couple_id: str,
        *,
        query: str,
        message: str,
        indoor_only: bool = False,
        target_item_id: str = "",
    ) -> tuple[list[dict], str]:
        """Build real, confirm-before-write place choices for one itinerary item."""
        active_style = trip.get("active_variant") or "slow"
        day = next(
            (
                value
                for variant in trip.get("variants", []) if variant.get("style") == active_style
                for value in variant.get("days", []) if int(value.get("day_number") or 0) == int(day_number)
            ),
            None,
        )
        if not day:
            return [], ""
        indoor_words = ("博物馆", "美术馆", "科技馆", "水族馆", "展览", "剧院", "影院", "书店", "商场", "室内")
        outdoor_words = ("公园", "古镇", "景区", "徒步", "露营", "观景", "漫步", "山", "湖", "海", "街", "广场", "植物园", "动物园", "乐园")
        meal_words = ("早餐", "午餐", "晚餐", "餐厅", "用餐", "下午茶", "咖啡")
        editable = [item for item in day.get("items", []) if not item.get("locked")]
        exact_targets = [
            item for item in editable
            if str(item.get("title") or "") in message
            or any(
                token in str(item.get("title") or "")
                for token in re.findall(r"[\u3400-\u9fffA-Za-z0-9]{2,}", message)
                if token not in {"帮我", "换成", "替换", "一个", "景点", "地点", "活动", "项目", "附近", "推荐"}
            )
        ]
        target = next((item for item in editable if item.get("id") == target_item_id), None)
        if target is None and len(exact_targets) == 1:
            target = exact_targets[0]
        if target is None:
            target = next(
            (
                item for item in editable
                if indoor_only and any(word in str(item.get("title") or "") for word in outdoor_words)
                and not any(word in str(item.get("title") or "") for word in indoor_words + meal_words)
            ),
            None,
            )
        if target is None:
            target = next(
                (
                    item for item in editable
                    if not any(word in str(item.get("title") or "") for word in meal_words)
                ),
                None,
            )
        if target is None:
            return [], "当天没有可替换的未锁定室外项目；已有安排会继续保留。"

        with self.connection() as db:
            destination = db.execute(
                "SELECT * FROM trip_destination_places WHERE trip_id=?", (trip_id,),
            ).fetchone()
        destination = dict(destination) if destination else {}
        region_hint = str(
            destination.get("city") or destination.get("district")
            or destination.get("province") or trip.get("destination") or ""
        ).strip()
        search = self.search_destination_places(
            {"query": ("博物馆" if indoor_only else query)[:120], "region_hint": region_hint[:120], "include_global": True},
            user_id,
        )
        candidates = [
            item for item in search.get("items", [])
            if item.get("provider") != "manual"
            and item.get("latitude") is not None and item.get("longitude") is not None
            and str(item.get("name") or "").strip()
        ][:6]
        cards: list[dict] = []
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0).isoformat()
        for candidate in candidates:
            candidate_name = str(candidate.get("name") or "").strip()[:100]
            note = (
                "雨天室内备选；开放时间、预约要求和临时闭馆信息以地点官方最新说明为准。"
                if indoor_only else
                "旅行助手地点备选；开放时间、预约要求和临时调整以地点官方最新说明为准。"
            )
            preview = {
                "title": candidate_name,
                "day_number": int(day_number),
                "replace_item_id": target["id"],
                "replace_title": target["title"],
                "before": {"title": target["title"], "note": target.get("note") or ""},
                "after": {"title": candidate_name, "note": note},
                "candidate_id": candidate.get("candidate_id"),
                "name": candidate_name,
                "address": candidate.get("formatted_address") or "",
                "category": candidate.get("category") or ("室内地点" if indoor_only else "景点与活动"),
                "provider": candidate.get("provider") or "地点服务",
                "latitude": candidate.get("latitude"),
                "longitude": candidate.get("longitude"),
            }
            draft_id, created = _id("travel_draft"), _now()
            payload = {
                "item_id": target["id"],
                "changes": {"title": candidate_name, "note": note},
                "candidate_id": candidate.get("candidate_id"),
            }
            with self.connection() as db:
                db.execute(
                    "INSERT INTO trip_assistant_action_drafts("
                    "id,trip_id,couple_id,creator_id,action_type,payload_json,preview_json,expected_trip_version,"
                    "expected_item_version,status,expires_at,created_at) "
                    "VALUES (?,?,?,?, 'itinerary_change',?,?,?,?, 'draft',?,?)",
                    (
                        draft_id, trip_id, couple_id, user_id,
                        json.dumps(payload, ensure_ascii=False), json.dumps(preview, ensure_ascii=False),
                        trip["version"], target["version"], expires_at, created,
                    ),
                )
            cards.append({
                "type": "indoor_place_option" if indoor_only else "place_replacement_option",
                "draft_id": draft_id, "status": "draft",
                "replacement_kind": "indoor" if indoor_only else "place",
                **preview, "expires_at": expires_at,
            })
        return cards, str(search.get("notice") or "")

    def _assistant_indoor_place_cards(
        self, trip_id: str, trip: dict, day_number: int, user_id: str, couple_id: str,
    ) -> tuple[list[dict], str]:
        return self._assistant_place_replacement_cards(
            trip_id, trip, day_number, user_id, couple_id,
            query="博物馆", message="", indoor_only=True,
        )

    def _assistant_itinerary_add_draft(
        self, trip_id: str, trip: dict, intent: Any, user_id: str, couple_id: str,
    ) -> list[dict]:
        title = str(intent.item_hint or intent.query or "").strip()[:100]
        title = re.sub(r"^(?:把|请把|帮我把)\s*", "", title)
        title = re.sub(r"\s*(?:加入|添加|放到|放进).*$", "", title).strip()
        if not title:
            return self._assistant_clarification(
                "我还不知道要加入哪个地点。你可以先搜索真实地点，再从结果卡片选择。",
                [{"label": "搜索附近景点", "prompt": "搜索当前目的地附近好玩的景点和活动"}],
            )
        active_style = trip.get("active_variant") or "slow"
        active_days = next(
            (variant.get("days", []) for variant in trip.get("variants", []) if variant.get("style") == active_style),
            [],
        )
        if intent.day_number is None:
            return self._assistant_clarification(
                f"请先选择把“{title}”加入哪一天。",
                [
                    {"label": f"第{day['day_number']}天 · {day.get('title') or day.get('date') or ''}", "prompt": f"把{title}加入第{day['day_number']}天行程"}
                    for day in active_days[:8]
                ],
            )
        day_number = int(intent.day_number)
        day = next(
            (
                day
                for day in active_days if int(day.get("day_number") or 0) == day_number
            ),
            None,
        )
        if not day:
            return self._assistant_clarification(f"当前方案没有第 {day_number} 天，请重新选择有效日期。")
        time_match = re.search(r"(?<!\d)((?:[01]?\d|2[0-3])[:：][0-5]\d)", intent.query)
        start_time = time_match.group(1).replace("：", ":") if time_match else "15:00"
        if len(start_time) == 4:
            start_time = f"0{start_time}"
        position = max((int(item.get("position") or 0) for item in day.get("items", [])), default=0) + 1
        payload = {
            "day_id": day["id"], "day_number": day_number, "title": title,
            "start_time": start_time, "duration_minutes": 90, "position": position,
        }
        preview = {
            "title": title, "day_number": day_number, "before": None,
            "after": {"start_time": start_time, "duration_minutes": 90, "position": position},
            "impact": {"warnings": ["地点和营业时间仍以外部平台最新信息为准。"]},
        }
        draft_id, created = _id("travel_draft"), _now()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0).isoformat()
        with self.connection() as db:
            db.execute(
                "INSERT INTO trip_assistant_action_drafts("
                "id,trip_id,couple_id,creator_id,action_type,payload_json,preview_json,expected_trip_version,"
                "expected_item_version,status,expires_at,created_at) "
                "VALUES (?,?,?,?, 'itinerary_add',?,?,?,?, 'draft',?,?)",
                (
                    draft_id, trip_id, couple_id, user_id,
                    json.dumps(payload, ensure_ascii=False), json.dumps(preview, ensure_ascii=False),
                    trip["version"], 0, expires_at, created,
                ),
            )
        return [{"type": "itinerary_add", "draft_id": draft_id, "status": "draft", **preview, "expires_at": expires_at}]

    def ask_trip_assistant(
        self, trip_id: str, message: object, user_id: str, idempotency_key: object,
        live_context: object = None, weather_provider: Any = None,
    ) -> dict:
        text = str(message or "").strip()
        key = str(idempotency_key or "").strip()
        if not 1 <= len(text) <= 1000:
            raise ValueError("问题长度需为 1–1000 个字符")
        if not 8 <= len(key) <= 128:
            raise ValueError("幂等键长度需为 8–128 个字符")
        couple, trip_row = self._assistant_trip_context(trip_id, user_id)
        with self.connection() as db:
            previous = db.execute("SELECT id FROM trip_assistant_messages WHERE couple_id=? AND idempotency_key=?", (couple["id"], key)).fetchone()
            if previous:
                assistant = db.execute("SELECT * FROM trip_assistant_messages WHERE reply_to=? AND role='assistant'", (previous["id"],)).fetchone()
                return {"assistant": self._assistant_message(assistant), "duplicate": True, "trip_version": trip_row["version"]}
            history = [self._assistant_message(row) for row in db.execute("SELECT * FROM trip_assistant_messages WHERE trip_id=? ORDER BY created_at DESC,id DESC LIMIT 12", (trip_id,)).fetchall()]
        intent = self.travel_assistant.interpret(text, trip_row["assistant_context"], list(reversed(history)))
        user_message_id = _id("travel_msg")
        with self.connection() as db:
            db.execute("INSERT INTO trip_assistant_messages(id,trip_id,couple_id,author_id,role,content,intent_type,cards_json,idempotency_key,created_at) VALUES (?,?,?,?, 'user',?,?, '[]',?,?)", (user_message_id, trip_id, couple["id"], user_id, text, intent.kind, key, _now()))
        cards: list[dict] = []
        notice = ""
        if intent.kind == "plan_compare":
            cards = self._assistant_plan_cards(trip_id)
            content = "我按预算、强度和交通时间整理了五套方案，可以先查看，再决定采用哪一套。"
        elif intent.kind == "hotel_search":
            replacing = bool(re.search(r"换|替换|改成|换成|不要这", text))
            quote_result = self.search_quotes({"trip_id": trip_id, "type": "hotel", "max_price_cents": intent.max_price_cents}, user_id)
            max_total = intent.max_price_cents
            if max_total:
                try:
                    nights = max(1, (date.fromisoformat(trip_row["end_date"]) - date.fromisoformat(trip_row["start_date"])).days)
                except ValueError:
                    nights = 1
                max_total *= nights
            cards = self._hotel_cards(quote_result, max_total)
            if replacing:
                for card in cards:
                    card["selection_mode"] = "replace"
            notice = str(quote_result.get("notice") or "")
            content = (
                "已查询可替换的酒店；点选后会替换当前酒店选择，并生成双方可见的新版本。"
                if cards and replacing else
                "已按这次旅行确认的地点查询酒店。" if cards else
                "没有找到符合当前预算的酒店，可以提高预算或换一个条件。"
            )
        elif intent.kind == "restaurant_search":
            replacing = bool(re.search(r"换|替换|改成|换成|不要这", text))
            current = self.get_trip(trip_id, user_id)
            meal_item = None
            active = current.get("active_variant") or "slow"
            def collect_meals(variants: list[dict]) -> list[tuple[dict, dict, str]]:
                found: list[tuple[dict, dict, str]] = []
                for variant in variants:
                    for day in variant.get("days", []):
                        if intent.day_number and int(day["day_number"]) != intent.day_number:
                            continue
                        for item in day.get("items", []):
                            if item["type"] != "meal" and not any(word in item["title"] for word in ("早餐", "早饭", "午餐", "午饭", "晚餐", "晚饭", "美食", "小吃")):
                                continue
                            title = item["title"]
                            if "早" in title:
                                item_meal = "breakfast"
                            elif "午" in title:
                                item_meal = "lunch"
                            elif "晚" in title or "夜" in title:
                                item_meal = "dinner"
                            elif item["start_time"] < "10:30":
                                item_meal = "breakfast"
                            elif item["start_time"] < "14:00":
                                item_meal = "lunch"
                            elif item["start_time"] < "17:00":
                                item_meal = "snack"
                            else:
                                item_meal = "dinner"
                            if intent.meal_type and item_meal != intent.meal_type:
                                continue
                            found.append((day, item, item_meal))
                return found

            active_variants = [variant for variant in current.get("variants", []) if variant.get("style") == active]
            meal_candidates = collect_meals(active_variants)
            if not meal_candidates and active != "food":
                meal_candidates = collect_meals([
                    variant for variant in current.get("variants", []) if variant.get("style") == "food"
                ])
            if len(meal_candidates) == 1 or (intent.day_number is not None and meal_candidates):
                meal_item = meal_candidates[0][1]
            elif len(meal_candidates) > 1:
                meal_labels = {"breakfast": "早餐", "lunch": "午餐", "snack": "下午茶", "dinner": "晚餐"}
                cards = self._assistant_clarification(
                    "我找到多餐安排，请先点选要查询或替换的具体餐次。",
                    [
                        {
                            "label": f"第{day['day_number']}天 · {meal_labels.get(meal_type, '用餐')}",
                            "prompt": f"第{day['day_number']}天的{meal_labels.get(meal_type, '用餐')}{'换一家餐厅' if replacing else '推荐附近高分餐厅'}",
                        }
                        for day, _item, meal_type in meal_candidates[:8]
                    ],
                )
                content = cards[0]["message"]
            if meal_item:
                result = self.refresh_food_recommendations(trip_id, user_id, item_id=meal_item["id"], preview_only=True)
                notice = result.get("notice", "")
                current = result["trip"]
                cards = self._restaurant_cards(current, intent)
                if replacing:
                    for card in cards:
                        card["selection_mode"] = "replace"
                content = (
                    "已查询这一餐可替换的真实餐厅；点选后会替换原选择，其他行程不变。"
                    if cards and replacing else
                    "已按行程所在区域查找附近餐厅，评分和距离来自地点服务。" if cards else
                    "当前范围没有找到符合条件的真实餐厅，可以放宽评分或距离。"
                )
            elif not cards:
                content = "请先告诉我是第几天的早餐、午餐或晚餐，或者先在每日行程中添加餐次。"
                cards = self._assistant_clarification(
                    content,
                    [
                        {"label": "第1天早餐", "prompt": "找第1天早餐附近高分餐厅"},
                        {"label": "第1天午餐", "prompt": "找第1天午餐附近高分餐厅"},
                        {"label": "第1天晚餐", "prompt": "找第1天晚餐附近高分餐厅"},
                    ],
                )
        elif intent.kind == "place_search":
            replacing = bool(re.search(r"换|替换|改成|换成|不要这", text))
            query = re.sub(
                r"(?:帮我|请|查找|搜索|推荐|附近|有什么|好玩的|地方|景点|活动)",
                " ",
                intent.query,
            )
            query = re.sub(r"\s+", " ", query).strip() or "景点"
            if replacing:
                current = self.get_trip(trip_id, user_id)
                active = current.get("active_variant") or "slow"
                active_items = [
                    (day, item)
                    for variant in current.get("variants", []) if variant.get("style") == active
                    for day in variant.get("days", [])
                    for item in day.get("items", [])
                    if not item.get("locked") and item.get("type") != "meal"
                ]
                replacement_day = intent.day_number
                if replacement_day is None:
                    matches = [
                        (day, item) for day, item in active_items
                        if str(item.get("title") or "") in text
                        or any(token in str(item.get("title") or "") for token in re.findall(r"[\u3400-\u9fffA-Za-z0-9]{2,}", text))
                    ]
                    if len(matches) == 1:
                        replacement_day = int(matches[0][0]["day_number"])
                if replacement_day is None:
                    cards = [
                        {
                            "type": "itinerary_item_choice", "item_id": item["id"],
                            "day_number": int(day["day_number"]), "start_time": item.get("start_time"),
                            "title": item.get("title"),
                            "prompt": f"把第{int(day['day_number'])}天的{item.get('title')}换成其他附近景点",
                        }
                        for day, item in active_items[:8]
                    ]
                    content = "请先点选要替换的原行程，我再给出同一地区的真实地点卡片。"
                else:
                    search_keyword = "博物馆" if re.search(r"室内|雨|博物馆|展览", text) else "景点"
                    day_items = [
                        item for day, item in active_items if int(day["day_number"]) == int(replacement_day)
                    ]
                    explicit = [item for item in day_items if str(item.get("title") or "") in text]
                    period_items = day_items
                    if not explicit and ("上午" in text or "早上" in text):
                        period_items = [item for item in day_items if str(item.get("start_time") or "") < "12:00"]
                    elif not explicit and "下午" in text:
                        period_items = [item for item in day_items if "12:00" <= str(item.get("start_time") or "") < "18:00"]
                    elif not explicit and ("晚上" in text or "夜间" in text):
                        period_items = [item for item in day_items if str(item.get("start_time") or "") >= "18:00"]
                    target_options = explicit or period_items
                    if len(target_options) != 1:
                        cards = [
                            {
                                "type": "itinerary_item_choice", "item_id": item["id"],
                                "day_number": int(replacement_day), "start_time": item.get("start_time"),
                                "title": item.get("title"),
                                "prompt": f"把第{int(replacement_day)}天的{item.get('title')}换成其他附近景点",
                            }
                            for item in target_options[:8]
                        ]
                        content = "这一天有多项安排，请先点选要替换的原行程。"
                    else:
                        cards, notice = self._assistant_place_replacement_cards(
                            trip_id, current, int(replacement_day), user_id, couple["id"],
                            query=search_keyword, message=text, target_item_id=str(target_options[0]["id"]),
                        )
                        content = (
                            "已查询同一旅行地区的真实地点；点选后只替换对应行程，确认前不会写入。"
                            if cards else "没有找到可安全替换的地点，请补充第几天或原行程名称。"
                        )
            else:
                current = self.get_trip(trip_id, user_id)
                active = current.get("active_variant") or "slow"
                day_numbers = [
                    int(day["day_number"])
                    for variant in current.get("variants", []) if variant.get("style") == active
                    for day in variant.get("days", [])
                ][:8]
                result = self.search_destination_places(
                    {
                        "query": query[:120],
                        "region_hint": intent.scope[:120],
                        "include_global": True,
                    },
                    user_id,
                )
                cards = [
                    {
                        "type": "place_result",
                        "candidate_id": item.get("candidate_id"),
                        "name": item.get("name"),
                        "address": item.get("formatted_address"),
                        "category": item.get("category"),
                        "provider": item.get("provider"),
                        "latitude": item.get("latitude"),
                        "longitude": item.get("longitude"),
                        "day_options": day_numbers,
                    }
                    for item in result.get("items", [])[:8]
                ]
                notice = str(result.get("notice") or "")
                content = (
                    "我按已经确认的旅行地区查询了真实地点，可以直接查看位置；需要加入行程时再告诉我要放到第几天。"
                    if cards
                    else "当前地点服务没有返回合适结果，可以换一个更具体的活动关键词。"
                )
        elif intent.kind == "itinerary_add":
            current = self.get_trip(trip_id, user_id)
            cards = self._assistant_itinerary_add_draft(
                trip_id, current, intent, user_id, couple["id"],
            )
            content = (
                "我已经把新增项目整理成修改预览，确认后才会写入共同旅行。"
                if cards and cards[0]["type"] == "itinerary_add"
                else cards[0]["message"]
            )
        elif intent.kind == "itinerary_change":
            current = self.get_trip(trip_id, user_id)
            cards = self._assistant_itinerary_draft(trip_id, current, text, intent, user_id, couple["id"])
            content = "我先生成了修改前后对比；确认后才会写入共同旅行。" if cards and cards[0]["type"] == "itinerary_change" else cards[0]["message"]
        elif intent.kind == "day_replan":
            current = self.get_trip(trip_id, user_id)
            cards = self._assistant_day_replan_draft(
                trip_id, current, intent, text, user_id, couple["id"], live_context, weather_provider,
            )
            rainy = any(word in text for word in ("下雨", "雨天", "有雨", "阵雨", "下雪", "雪天", "有雪"))
            indoor_cards: list[dict] = []
            if rainy and intent.day_number is not None:
                indoor_cards, indoor_notice = self._assistant_indoor_place_cards(
                    trip_id, current, int(intent.day_number), user_id, couple["id"],
                )
                notice = "；".join(value for value in (notice, indoor_notice) if value)
                cards.extend(indoor_cards)
            if indoor_cards:
                content = "已按雨雪天气整理当天安排，并查询了可直接选择的室内地点。每张卡都会先显示替换内容，确认后才修改共同旅行。"
            else:
                content = "我只整理了指定日期尚未开始且未锁定的内容；确认后才会一起写入。" if cards[0]["type"] == "day_replan" else cards[0]["message"]
        elif intent.kind == "next_stop":
            if not live_context:
                content = "请先点击旅行中的“更新当前位置”，我不会在后台持续获取位置。"
                cards = self._assistant_clarification(
                    content,
                    [{"label": "更新当前位置并推荐", "action": "trip-live-refresh"}],
                )
            else:
                current = self.get_trip(trip_id, user_id)
                context = validate_live_context(live_context)
                weather = weather_provider.current(context.latitude, context.longitude) if weather_provider else None
                cards = build_live_recommendations(current, context, weather)["candidates"]
                content = "已根据本次主动提供的位置和当前行程整理下一站。" if cards else "当前没有已确认坐标且尚未开始的下一站。"
        elif intent.kind == "transport_compare":
            current = self.get_trip(trip_id, user_id)
            cards, notices = self._transport_comparison_cards(trip_id, current, user_id)
            available = sum(bool(card.get("items")) for card in cards)
            content = (
                f"已分别查询酒店、航班和火车票，其中 {available} 类有可展示结果。"
                "每条结果都标明实时、缓存、手工或模拟来源，购买仍跳转第三方完成。"
            )
            notice = " ".join(dict.fromkeys(notices))[:600]
        elif intent.kind == "trip_recap":
            draft = self.create_trip_recap_draft(trip_id, user_id)
            content = "已生成双方可见的旅行回忆草稿；确认前不会进入时光轴。"
            cards = [{"type": "trip_recap", **draft}]
        else:
            content = "你可以直接点一个选项继续；查询会立即展示，修改仍会先给你预览。"
            cards = [{
                "type": "suggestion_set",
                "message": "这次想让我帮你做什么？",
                "options": [
                    {"label": "比较五套方案", "prompt": "比较五套方案的预算、强度和交通时间"},
                    {"label": "找附近好玩的", "prompt": "搜索当前目的地附近好玩的景点和活动"},
                    {"label": "找附近高分餐厅", "prompt": "找第一天晚餐，4.5分以上，3公里内"},
                    {"label": "查询酒店", "prompt": "查询目的地附近每晚800元以内的酒店"},
                    {"label": "按天气调整", "prompt": "根据天气重新安排当天后续行程"},
                    {"label": "生成旅行回忆", "prompt": "旅行结束后生成共同回忆草稿"},
                ],
            }]
        if notice:
            content = f"{content} {notice}"[:1000]
        assistant_id = _id("travel_msg")
        created = _now()
        with self.connection() as db:
            db.execute("INSERT INTO trip_assistant_messages(id,trip_id,couple_id,role,content,intent_type,cards_json,reply_to,created_at) VALUES (?,?,?,'assistant',?,?,?,?,?)", (assistant_id, trip_id, couple["id"], content, intent.kind, json.dumps(cards, ensure_ascii=False, separators=(",", ":")), user_message_id, created))
            row = db.execute("SELECT * FROM trip_assistant_messages WHERE id=?", (assistant_id,)).fetchone()
            self._audit(db, "trip.assistant_asked", "trip", trip_id, {"intent": intent.kind, "card_count": len(cards)}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "trip.assistant_updated", trip_id, {"resource": "trip_assistant", "trip_id": trip_id})
        return {"assistant": self._assistant_message(row), "duplicate": False, "trip_version": trip_row["version"]}

    def confirm_trip_assistant_action(self, trip_id: str, draft_id: str, idempotency_key: object, user_id: str) -> dict:
        key = str(idempotency_key or "").strip()
        if not 8 <= len(key) <= 128:
            raise ValueError("幂等键长度需为 8–128 个字符")
        couple, _ = self._assistant_trip_context(trip_id, user_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT result_json FROM trip_assistant_action_drafts WHERE couple_id=? AND idempotency_key=? AND status='confirmed'", (couple["id"], key)).fetchone()
            if previous:
                return _json(previous["result_json"], {})
            row = db.execute("SELECT * FROM trip_assistant_action_drafts WHERE id=? AND trip_id=? AND couple_id=?", (draft_id, trip_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("旅行助手动作草稿不存在")
            if row["status"] == "confirmed":
                return _json(row["result_json"], {})
            if row["status"] != "draft" or row["expires_at"] <= _now():
                raise ConflictError("旅行助手动作草稿已失效")
            payload = _json(row["payload_json"], {})
            trip = db.execute("SELECT version FROM trips WHERE id=?", (trip_id,)).fetchone()
            if not trip or int(trip["version"]) != int(row["expected_trip_version"]):
                raise ConflictError("旅行已经被另一方修改，请重新生成调整预览")
            if row["action_type"] == "itinerary_change":
                result = self._confirm_itinerary_change(db, row, payload, user_id, couple["id"])
            elif row["action_type"] == "itinerary_add":
                result = self._confirm_itinerary_add(db, row, payload, user_id, couple["id"])
            elif row["action_type"] == "day_replan":
                result = self._confirm_day_replan(db, row, payload, user_id, couple["id"])
            else:
                raise ValueError("不支持的旅行助手动作")
            cursor = db.execute(
                "UPDATE trip_assistant_action_drafts SET status='confirmed',idempotency_key=?,result_json=?,confirmed_at=? "
                "WHERE id=? AND status='draft'",
                (key, json.dumps(result, ensure_ascii=False), _now(), draft_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("旅行助手动作草稿已被取消或确认")
        return {**result, "trip": self.get_trip(trip_id, user_id=user_id)}

    def _confirm_itinerary_add(
        self, db: Any, draft: Any, payload: dict, user_id: str, couple_id: str,
    ) -> dict:
        trip = db.execute(
            "SELECT current_revision,version FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL",
            (draft["trip_id"], couple_id),
        ).fetchone()
        if not trip or int(trip["version"]) != int(draft["expected_trip_version"]):
            raise ConflictError("旅行已经被另一方修改，请重新生成新增预览")
        day = db.execute(
            "SELECT d.id,v.trip_id FROM trip_days d JOIN plan_variants v ON v.id=d.variant_id "
            "WHERE d.id=? AND v.trip_id=?",
            (payload.get("day_id"), draft["trip_id"]),
        ).fetchone()
        if not day:
            raise ConflictError("旅行日期已经变化，请重新生成新增预览")
        title = str(payload.get("title") or "").strip()[:100]
        start_time = str(payload.get("start_time") or "")
        duration = int(payload.get("duration_minutes") or 90)
        position = int(payload.get("position") or 1)
        if not title or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start_time):
            raise ValueError("新增行程内容或时间无效")
        if not 15 <= duration <= 1440 or position < 1:
            raise ValueError("新增行程时长或顺序无效")
        item_id = _id("item")
        db.execute(
            "INSERT INTO itinerary_items(id,day_id,type,title,start_time,duration_minutes,cost_cents,note,details_json,locked,position,version) "
            "VALUES (?,?,'activity',?,?,?,0,?,'{}',1,?,1)",
            (item_id, day["id"], title, start_time, duration, "由旅行助手建议，经用户确认加入。", position),
        )
        revision = int(trip["current_revision"]) + 1
        now, revision_id = _now(), _id("revision")
        diff = {"operation": "add", "item_id": item_id, "day_number": int(payload.get("day_number") or 1), "after": {"title": title, "start_time": start_time, "duration_minutes": duration}}
        db.execute("UPDATE trips SET current_revision=?,version=version+1,updated_at=? WHERE id=?", (revision, now, draft["trip_id"]))
        db.execute("DELETE FROM approvals WHERE trip_id=?", (draft["trip_id"],))
        db.execute(
            "INSERT INTO plan_revisions(id,trip_id,revision,parent_revision,author_type,diff_json,reason,created_at) VALUES (?,?,?,?, 'user',?,?,?)",
            (revision_id, draft["trip_id"], revision, revision - 1, json.dumps(diff, ensure_ascii=False), "旅行助手新增建议，经用户确认", now),
        )
        self._audit(db, "trip.item_added", "itinerary_item", item_id, diff, actor_id=user_id)
        self._event(db, couple_id, user_id, "life.updated", draft["trip_id"], {"resource": "trip", "revision": revision})
        return {"revision_id": revision_id, "revision": revision, "diff": diff, "item_id": item_id}

    def _confirm_itinerary_change(
        self, db: Any, draft: Any, payload: dict, user_id: str, couple_id: str,
    ) -> dict:
        item_id = str(payload.get("item_id") or "")
        changes = payload.get("changes")
        if not item_id or not isinstance(changes, dict):
            raise ValueError("行程调整草稿无效")
        allowed = {"title", "start_time", "duration_minutes", "note", "position"}
        updates = {key: value for key, value in changes.items() if key in allowed}
        if not updates:
            raise ValueError("行程调整草稿没有可执行修改")
        if "title" in updates:
            updates["title"] = str(updates["title"]).strip()[:100]
            if not updates["title"]:
                raise ValueError("行程标题不能为空")
        if "start_time" in updates and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(updates["start_time"])):
            raise ValueError("开始时间格式必须为 HH:MM")
        if "duration_minutes" in updates:
            updates["duration_minutes"] = int(updates["duration_minutes"])
            if not 15 <= updates["duration_minutes"] <= 1440:
                raise ValueError("行程时长需为 15–1440 分钟")
        if "position" in updates:
            updates["position"] = int(updates["position"])
            if updates["position"] < 1:
                raise ValueError("行程顺序必须大于 0")
        if "note" in updates:
            updates["note"] = str(updates["note"])[:500]
        item = db.execute(
            "SELECT i.*,v.trip_id FROM itinerary_items i JOIN trip_days d ON d.id=i.day_id "
            "JOIN plan_variants v ON v.id=d.variant_id JOIN trips t ON t.id=v.trip_id "
            "WHERE i.id=? AND t.couple_id=? AND t.deleted_at IS NULL",
            (item_id, couple_id),
        ).fetchone()
        if not item or item["trip_id"] != draft["trip_id"] or int(item["version"]) != int(draft["expected_item_version"]):
            raise ConflictError("旅行已经被另一方修改，请重新生成调整预览")
        trip = db.execute(
            "SELECT current_revision,version FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL",
            (draft["trip_id"], couple_id),
        ).fetchone()
        if not trip or int(trip["version"]) != int(draft["expected_trip_version"]):
            raise ConflictError("旅行已经被另一方修改，请重新生成调整预览")
        diff = {key: {"before": item[key], "after": value} for key, value in updates.items()}
        if "title" in updates and updates["title"] != item["title"]:
            updates["details_json"] = "{}"
            db.execute("DELETE FROM itinerary_item_places WHERE itinerary_item_id=?", (item_id,))
        set_sql = ", ".join(f"{key}=?" for key in updates)
        db.execute(f"UPDATE itinerary_items SET {set_sql},locked=1,version=version+1 WHERE id=?", (*updates.values(), item_id))
        candidate_id = str(payload.get("candidate_id") or "").strip()
        if candidate_id:
            candidate_row = db.execute(
                "SELECT payload_json,expires_at FROM place_search_candidates WHERE id=? AND user_id=?",
                (candidate_id, draft["creator_id"]),
            ).fetchone()
            if not candidate_row or candidate_row["expires_at"] < _now():
                raise ConflictError("地点候选已过期，请重新查询后选择")
            place = _json(candidate_row["payload_json"], {})
            provider = str(place.get("provider") or "")
            latitude, longitude = place.get("latitude"), place.get("longitude")
            if provider not in {"amap", "google_maps", "manual"} or latitude is None or longitude is None:
                raise ValueError("地点候选缺少可确认的地图坐标")
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
                (
                    item_id, draft["trip_id"], provider, str(place.get("provider_place_id") or ""),
                    str(place.get("name") or updates.get("title") or "")[:100], str(place.get("formatted_address") or "")[:500],
                    str(place.get("country") or "")[:100], str(place.get("province") or "")[:100],
                    str(place.get("city") or "")[:100], str(place.get("district") or "")[:100],
                    float(latitude), float(longitude), str(place.get("category") or "")[:100], user_id, _now(),
                ),
            )
            diff["confirmed_place"] = {
                "name": str(place.get("name") or ""), "provider": provider,
                "provider_place_id": str(place.get("provider_place_id") or ""),
            }
        revision = int(trip["current_revision"]) + 1
        revision_id = _id("revision")
        now = _now()
        db.execute("UPDATE trips SET current_revision=?,version=version+1,updated_at=? WHERE id=?", (revision, now, draft["trip_id"]))
        db.execute("DELETE FROM approvals WHERE trip_id=?", (draft["trip_id"],))
        db.execute(
            "INSERT INTO plan_revisions(id,trip_id,revision,parent_revision,author_type,diff_json,reason,created_at) "
            "VALUES (?,?,?,?, 'user',?,?,?)",
            (revision_id, draft["trip_id"], revision, revision - 1, json.dumps(diff, ensure_ascii=False), "旅行助手建议，经用户确认", now),
        )
        self._audit(db, "trip.item_revised", "itinerary_item", item_id, diff, actor_id=user_id)
        self._event(db, couple_id, user_id, "life.updated", draft["trip_id"], {"resource": "trip", "revision": revision})
        return {"revision_id": revision_id, "revision": revision, "diff": diff}

    def _confirm_day_replan(
        self, db: Any, draft: Any, payload: dict, user_id: str, couple_id: str,
    ) -> dict:
        changes = payload.get("changes")
        if not isinstance(changes, list) or not changes:
            raise ValueError("当天重排草稿没有可执行修改")
        expected_versions = _json(draft["expected_item_versions_json"], {})
        item_ids = [str(row.get("item_id") or "") for row in changes if isinstance(row, dict)]
        if len(item_ids) != len(changes) or len(set(item_ids)) != len(item_ids):
            raise ValueError("当天重排行程项目无效")
        placeholders = ",".join("?" for _ in item_ids)
        trip = db.execute(
            "SELECT current_revision,version FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL",
            (draft["trip_id"], couple_id),
        ).fetchone()
        if not trip or int(trip["version"]) != int(draft["expected_trip_version"]):
            raise ConflictError("旅行已经被另一方修改，请重新生成调整预览")
        rows = db.execute(
            f"SELECT i.*,v.trip_id FROM itinerary_items i JOIN trip_days d ON d.id=i.day_id "
            f"JOIN plan_variants v ON v.id=d.variant_id WHERE i.id IN ({placeholders})",  # noqa: S608
            item_ids,
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        if set(by_id) != set(item_ids) or any(row["trip_id"] != draft["trip_id"] for row in rows):
            raise ConflictError("当天行程已经变化，请重新生成调整预览")
        if any(int(by_id[item_id]["version"]) != int(expected_versions.get(item_id, -1)) for item_id in item_ids):
            raise ConflictError("当天有行程被另一方修改，请重新生成调整预览")
        diff_rows = []
        for change in changes:
            item_id, values = change["item_id"], change.get("changes")
            if not isinstance(values, dict) or not values or not set(values).issubset({"start_time", "position"}):
                raise ValueError("当天重排字段无效")
            start_time = str(values.get("start_time", by_id[item_id]["start_time"]))
            position = int(values.get("position", by_id[item_id]["position"]))
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start_time) or position < 1:
                raise ValueError("当天重排时间或顺序无效")
            diff_rows.append({
                "item_id": item_id, "title": by_id[item_id]["title"],
                "before": {"start_time": by_id[item_id]["start_time"], "position": by_id[item_id]["position"]},
                "after": {"start_time": start_time, "position": position},
            })
        revision = int(trip["current_revision"]) + 1
        revision_id = _id("revision")
        for value in diff_rows:
            db.execute(
                "UPDATE itinerary_items SET start_time=?,position=?,version=version+1 WHERE id=?",
                (value["after"]["start_time"], value["after"]["position"], value["item_id"]),
            )
        diff = {"day_replan": diff_rows, "day_number": payload.get("day_number")}
        db.execute(
            "UPDATE trips SET current_revision=?,version=version+1,updated_at=? WHERE id=?",
            (revision, _now(), draft["trip_id"]),
        )
        db.execute("DELETE FROM approvals WHERE trip_id=?", (draft["trip_id"],))
        db.execute(
            "INSERT INTO plan_revisions(id,trip_id,revision,parent_revision,author_type,diff_json,reason,created_at) "
            "VALUES (?,?,?,?, 'user',?,?,?)",
            (revision_id, draft["trip_id"], revision, revision - 1, json.dumps(diff, ensure_ascii=False), "旅行助手当天重排，经用户确认", _now()),
        )
        minimal = {"revision_id": revision_id, "revision": revision, "diff": diff}
        self._audit(db, "trip.day_replanned", "trip", draft["trip_id"], diff, actor_id=user_id)
        self._event(db, couple_id, user_id, "life.updated", draft["trip_id"], {"resource": "trip", "revision": revision})
        return minimal

    def cancel_trip_assistant_action(self, trip_id: str, draft_id: str, user_id: str) -> dict:
        couple, _ = self._assistant_trip_context(trip_id, user_id)
        with self.connection() as db:
            cursor = db.execute("UPDATE trip_assistant_action_drafts SET status='cancelled' WHERE id=? AND trip_id=? AND couple_id=? AND status='draft'", (draft_id, trip_id, couple["id"]))
            if cursor.rowcount != 1:
                raise KeyError("可取消的旅行助手动作草稿不存在")
        return {"id": draft_id, "status": "cancelled"}
