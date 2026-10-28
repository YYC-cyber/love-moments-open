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
            if card.get("type") in {"itinerary_change", "day_replan"} and card.get("draft_id")
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
            return [{"type": "clarification", "message": "请告诉我想调整到几点或停留多久，也可以直接写出行程名称。"}]
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
            return [{"type": "clarification", "message": "没有找到要调整的行程，请先在每日行程中选择一项。"}]
        hint = str(getattr(intent, "item_hint", "") or "").strip()
        exact = [
            pair for pair in available
            if pair[1]["title"] in message
            or (hint and (hint in pair[1]["title"] or pair[1]["title"] in hint))
        ]
        if len(exact) > 1:
            days = "、".join(f"第{pair[0]['day_number']}天" for pair in exact[:6])
            return [{"type": "clarification", "message": f"这段旅行里有多条同名行程（{days}），请补充第几天后再调整。"}]
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
                choices = "；".join(f"第{pair[0]['day_number']}天 {pair[1]['start_time']} {pair[1]['title']}" for pair in time_candidates[:4])
                return [{"type": "clarification", "message": f"还不能确定要修改哪一项，请写出行程名称或更具体的时段。可选：{choices}"}]
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
            return [{"type": "clarification", "message": "请告诉我是第几天，或先点击“更新当前位置”再调整今天。"}]
        day = next(
            (
                day
                for variant in trip.get("variants", []) if variant.get("style") == (trip.get("active_variant") or "slow")
                for day in variant.get("days", []) if int(day.get("day_number") or 0) == int(day_number)
            ),
            None,
        )
        if not day:
            return [{"type": "clarification", "message": "这次旅行没有对应日期，请检查第几天。"}]
        target_date = date.fromisoformat(str(day.get("date") or trip.get("start_date"))[:10])
        reference_date = context.local_time.date() if context else date.today()
        if target_date < reference_date:
            return [{"type": "clarification", "message": "这一天已经过去，不能重新安排已发生的行程。"}]
        if target_date == reference_date and context is None:
            return [{"type": "clarification", "message": "调整今天需要先点击“更新当前位置”，用于判断哪些项目已经开始；位置不会保存。"}]
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
            return [{"type": "clarification", "message": message_text}]
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
            quote_result = self.search_quotes({"trip_id": trip_id, "type": "hotel", "max_price_cents": intent.max_price_cents}, user_id)
            max_total = intent.max_price_cents
            if max_total:
                try:
                    nights = max(1, (date.fromisoformat(trip_row["end_date"]) - date.fromisoformat(trip_row["start_date"])).days)
                except ValueError:
                    nights = 1
                max_total *= nights
            cards = self._hotel_cards(quote_result, max_total)
            notice = str(quote_result.get("notice") or "")
            content = "已按这次旅行确认的地点查询酒店。" if cards else "没有找到符合当前预算的酒店，可以提高预算或换一个条件。"
        elif intent.kind == "restaurant_search":
            current = self.get_trip(trip_id, user_id)
            meal_item = None
            active = current.get("active_variant") or "slow"
            variants = sorted(current.get("variants", []), key=lambda value: (value["style"] != active, value["style"] != "food"))
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
                        meal_item = item
                        break
                    if meal_item:
                        break
                if meal_item:
                    break
            if meal_item:
                result = self.refresh_food_recommendations(trip_id, user_id, item_id=meal_item["id"], preview_only=True)
                notice = result.get("notice", "")
                current = result["trip"]
                cards = self._restaurant_cards(current, intent)
                content = "已按行程所在区域查找附近餐厅，评分和距离来自地点服务。" if cards else "当前范围没有找到符合条件的真实餐厅，可以放宽评分或距离。"
            else:
                content = "请先告诉我是第几天的早餐、午餐或晚餐，或者先在每日行程中添加餐次。"
                cards = [{"type": "clarification", "message": content}]
        elif intent.kind == "itinerary_change":
            current = self.get_trip(trip_id, user_id)
            cards = self._assistant_itinerary_draft(trip_id, current, text, intent, user_id, couple["id"])
            content = "我先生成了修改前后对比；确认后才会写入共同旅行。" if cards and cards[0]["type"] == "itinerary_change" else cards[0]["message"]
        elif intent.kind == "day_replan":
            current = self.get_trip(trip_id, user_id)
            cards = self._assistant_day_replan_draft(
                trip_id, current, intent, text, user_id, couple["id"], live_context, weather_provider,
            )
            content = "我只整理了指定日期尚未开始且未锁定的内容；确认后才会一起写入。" if cards[0]["type"] == "day_replan" else cards[0]["message"]
        elif intent.kind == "next_stop":
            if not live_context:
                content = "请先点击旅行中的“更新当前位置”，我不会在后台持续获取位置。"
                cards = [{"type": "clarification", "message": content}]
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
            content = "我可以比较五套方案、查询酒店、找附近餐厅、推荐下一站、重排当天行程或生成旅行回忆草稿。"
            cards = [{"type": "clarification", "message": content}]
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
