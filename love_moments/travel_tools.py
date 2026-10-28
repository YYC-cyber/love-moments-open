"""Deterministic trip negotiation and schedule realism tools.

This module deliberately does not call maps or language models.  It only reads
the active itinerary, saved provider cache and server-owned timing rules.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .intelligent_ui import ConflictError
from .places import normalize_place_candidate


PREFERENCES = frozenset({"must", "okay", "avoid"})
FINDING_SEVERITIES = frozenset({"info", "warning", "conflict"})
SOURCE_TYPES = frozenset({"human", "real_api", "rules", "ai", "mock", "cache", "legacy_unknown"})
TRUSTED_ROUTE_CACHE_PROVIDERS = frozenset({"amap", "geoapify", "google_maps"})
TRUSTED_ROUTE_SOURCE_TYPES = frozenset({"real_api", "cache"})
EMERGENCY_ENTRY_TYPES = frozenset({"hotel", "booking", "contact", "insurance", "hospital", "return", "other"})
EMERGENCY_VISIBILITIES = frozenset({"private", "shared"})
TRUSTED_PLACE_PROVIDERS = frozenset({"amap", "google_maps", "geoapify"})
HEALTHCARE_MARKERS = ("医院", "医疗", "诊所", "急救", "hospital", "clinic", "healthcare")
EMERGENCY_FIELDS = {
    "hotel": {"address", "phone", "check_in_at", "check_out_at", "front_desk_note"},
    "booking": {"provider", "booking_type", "booking_reference", "support_phone", "status_note"},
    "contact": {"name", "relationship", "phone", "note"},
    "insurance": {"provider", "policy_number", "assistance_phone", "coverage_summary", "valid_from", "valid_to"},
    "hospital": {"provider", "provider_place_id", "name", "address", "phone", "latitude", "longitude", "verified_at"},
    "return": {"transport_type", "carrier", "service_number", "departure_place", "departure_at", "arrival_place", "arrival_at", "booking_reference"},
    "other": {"note"},
}
PROHIBITED_EMERGENCY_KEY_PARTS = (
    "password", "token", "secret", "passport", "identity", "id_card", "idcard",
    "bank", "card_number", "cvv", "payment", "支付密码", "银行卡", "身份证", "护照",
)
EXPENSE_CATEGORIES = frozenset({"transport", "lodging", "food", "tickets", "shopping", "other"})
EXPENSE_SPLIT_MODES = frozenset({"equal", "personal"})
MAX_SAFE_MINOR = 9_007_199_254_740_991


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: object, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label}必须是正整数")
    return value


def _minute_of_day(value: object) -> int | None:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        return None
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _aware_iso_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


class TravelToolsMixin:
    @staticmethod
    def _travel_request_key(value: object) -> str:
        key = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9:._-]{8,160}", key):
            raise ValueError("幂等键格式无效")
        return key

    @staticmethod
    def _travel_fingerprint(value: dict) -> str:
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _travel_replay(
        self, db: sqlite3.Connection, couple_id: str, user_id: str,
        operation: str, key: str, fingerprint: str,
    ) -> dict | None:
        saved = self._idempotent_result(db, couple_id, user_id, operation, key)
        if saved is None:
            return None
        if saved.get("_request_hash") != fingerprint:
            raise ConflictError("幂等标识已绑定到另一项旅行操作")
        response = saved.get("_response")
        if not isinstance(response, dict):
            raise ConflictError("旅行操作回执已经失效")
        return response

    def _trip_tools_context(
        self, db: sqlite3.Connection, trip_id: str, user_id: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row, str]:
        couple, partner_id = self._couple_context(db, user_id)
        trip = db.execute(
            "SELECT * FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL",
            (trip_id, couple["id"]),
        ).fetchone()
        if not trip:
            raise KeyError("旅行不存在")
        return couple, trip, partner_id

    @staticmethod
    def _candidate_public(item: dict) -> dict:
        return {
            "id": item["id"], "day_id": item["day_id"], "day_number": item["day_number"],
            "date": item["date"], "type": item["type"], "title": item["title"],
            "start_time": item["start_time"], "duration_minutes": item["duration_minutes"],
            "locked": item["locked"], "version": item["version"],
            "place_name": item.get("place_name"),
        }

    def _active_itinerary_snapshot(self, db: sqlite3.Connection, trip: sqlite3.Row) -> list[dict]:
        if not str(trip["active_variant"] or "").strip():
            raise ValueError("请先选择一套旅行方案")
        rows = db.execute(
            """SELECT i.id,i.day_id,i.type,i.title,i.start_time,i.duration_minutes,i.details_json,
                      i.locked,i.position,i.version,d.day_number,d.date,d.position AS day_position,
                      p.name AS place_name,p.provider AS place_provider,p.provider_place_id,
                      p.latitude,p.longitude,p.confirmed_at
               FROM itinerary_items i
               JOIN trip_days d ON d.id=i.day_id
               JOIN plan_variants v ON v.id=d.variant_id
               LEFT JOIN itinerary_item_places p ON p.itinerary_item_id=i.id
               WHERE v.trip_id=? AND v.style=?
               ORDER BY d.position,i.position,i.id LIMIT 101""",
            (trip["id"], trip["active_variant"]),
        ).fetchall()
        if len(rows) > 100:
            raise ValueError("当前方案最多支持 100 个协商项目")
        items: list[dict] = []
        for row in rows:
            value = dict(row)
            if value.get("latitude") is None or value.get("longitude") is None:
                restaurant = db.execute(
                    """SELECT name,amap_poi_id AS provider_place_id,latitude,longitude,selected_at
                       FROM trip_food_recommendations
                       WHERE trip_id=? AND itinerary_item_id=? AND selected_at IS NOT NULL
                         AND latitude IS NOT NULL AND longitude IS NOT NULL
                       ORDER BY selected_at DESC LIMIT 1""",
                    (trip["id"], value["id"]),
                ).fetchone()
                if restaurant:
                    value.update({
                        "place_name": restaurant["name"], "place_provider": "amap",
                        "provider_place_id": restaurant["provider_place_id"],
                        "latitude": restaurant["latitude"], "longitude": restaurant["longitude"],
                        "confirmed_at": restaurant["selected_at"],
                    })
            value["locked"] = bool(value["locked"])
            value["version"] = int(value["version"])
            value["duration_minutes"] = int(value["duration_minutes"])
            value["details"] = _json(value.pop("details_json", "{}"), {})
            items.append(value)
        return items

    @staticmethod
    def _negotiation_receipt_snapshots(
        db: sqlite3.Connection, couple_id: str, trip_id: str,
    ) -> list[dict]:
        snapshots: list[dict] = []
        rows = db.execute(
            "SELECT result_json FROM mutation_idempotency WHERE couple_id=? AND operation=?",
            (couple_id, f"trip.negotiation:{trip_id}"),
        ).fetchall()
        for row in rows:
            receipt = _json(row["result_json"], {})
            response = receipt.get("_response") if isinstance(receipt, dict) else None
            if not isinstance(response, dict):
                continue
            round_number = response.get("round")
            if isinstance(round_number, bool) or not isinstance(round_number, int) or round_number < 1:
                continue
            snapshots.append(response)
        return snapshots

    @classmethod
    def _current_negotiation_round(
        cls, db: sqlite3.Connection, trip_id: str, couple_id: str,
    ) -> int:
        row = db.execute(
            "SELECT COALESCE(MAX(negotiation_round),1) AS value FROM trip_item_preferences WHERE trip_id=?",
            (trip_id,),
        ).fetchone()
        receipt_rounds = [
            int(response["round"])
            for response in cls._negotiation_receipt_snapshots(db, couple_id, trip_id)
        ]
        return max([int(row["value"]), *receipt_rounds])

    @staticmethod
    def _complete_ballot(rows: list[sqlite3.Row], candidates: list[dict]) -> bool:
        candidate_versions = {item["id"]: int(item["version"]) for item in candidates}
        if {str(row["itinerary_item_id"]) for row in rows} != set(candidate_versions):
            return False
        if any(not row["submitted_at"] for row in rows):
            return False
        if len({str(row["submitted_at"]) for row in rows}) != 1:
            return False
        return all(
            int(row["expected_item_version"]) == candidate_versions.get(str(row["itinerary_item_id"]))
            for row in rows
        )

    @staticmethod
    def _negotiation_receipt_trip_versions(
        db: sqlite3.Connection, couple_id: str, trip_id: str, round_number: int,
    ) -> list[int]:
        latest_by_actor_round: dict[str, int] = {}
        # user_id is intentionally selected separately so the generic receipt
        # stays actor-private while still providing version evidence.
        rows = db.execute(
            """SELECT user_id,result_json FROM mutation_idempotency
               WHERE couple_id=? AND operation=? ORDER BY created_at DESC,rowid DESC""",
            (couple_id, f"trip.negotiation:{trip_id}"),
        ).fetchall()
        for row in rows:
            if str(row["user_id"]) in latest_by_actor_round:
                continue
            receipt = _json(row["result_json"], {})
            response = receipt.get("_response") if isinstance(receipt, dict) else None
            if isinstance(response, dict) and int(response.get("round") or 0) == round_number:
                latest_by_actor_round[str(row["user_id"])] = int(response.get("trip_version") or 0)
        return list(latest_by_actor_round.values())

    @staticmethod
    def _negotiation_outcome(candidates: list[dict], rows_by_user: dict[str, list[sqlite3.Row]], members: list[str]) -> dict:
        ballots = {
            member: {str(row["itinerary_item_id"]): row for row in rows_by_user[member]}
            for member in members
        }
        vetoed: list[str] = []
        overridden: list[str] = []
        must_by_both: list[str] = []
        proposal: list[dict] = []
        for item in candidates:
            item_id = str(item["id"])
            rows = [ballots[member][item_id] for member in members]
            values = [str(row["preference"]) for row in rows]
            if "avoid" in values:
                vetoed.append(item_id)
                if not all(row["override_confirmed_at"] for row in rows):
                    continue
                overridden.append(item_id)
                reason = "explicit_avoid_override"
            elif all(value == "must" for value in values):
                must_by_both.append(item_id)
                reason = "must_by_both"
            elif "must" in values:
                reason = "must_and_okay"
            else:
                reason = "okay_by_both"
            proposal.append({
                "item_id": item_id, "day_id": item["day_id"], "day_number": item["day_number"],
                "date": item["date"], "title": item["title"], "reason_code": reason,
            })
        return {
            "must_by_both_item_ids": must_by_both,
            "vetoed_item_ids": vetoed,
            "overridden_veto_item_ids": overridden,
            "proposal": proposal,
        }

    def _serialize_trip_negotiation_in_connection(
        self, db: sqlite3.Connection, couple: sqlite3.Row, trip: sqlite3.Row,
        user_id: str, partner_id: str,
    ) -> dict:
        candidates = self._active_itinerary_snapshot(db, trip)
        round_number = self._current_negotiation_round(db, str(trip["id"]), str(couple["id"]))
        receipt_snapshots = [
            response for response in self._negotiation_receipt_snapshots(
                db, str(couple["id"]), str(trip["id"]),
            )
            if int(response["round"]) == round_number
        ]
        rows = db.execute(
            "SELECT * FROM trip_item_preferences WHERE trip_id=? AND negotiation_round=? ORDER BY itinerary_item_id",
            (trip["id"], round_number),
        ).fetchall()
        members = [str(couple["member_a"]), str(couple["member_b"])]
        rows_by_user = {member: [row for row in rows if row["user_id"] == member] for member in members}
        complete = {member: self._complete_ballot(rows_by_user[member], candidates) for member in members}
        receipt_versions = self._negotiation_receipt_trip_versions(
            db, str(couple["id"]), str(trip["id"]), round_number,
        )
        if not rows:
            # Preference rows can disappear through the itinerary FK cascade
            # during regeneration.  The durable mutation receipt preserves the
            # fact that this round existed, so it must not silently become a
            # brand-new round 1 or reuse new items by position.
            state = "stale" if receipt_snapshots else "open"
        elif any(version != int(trip["version"]) for version in receipt_versions):
            state = "stale"
        elif any(rows_by_user[member] and not complete[member] for member in members):
            state = "stale"
        elif sum(1 for value in complete.values() if value) == 1:
            state = "waiting_for_partner"
        elif all(complete.values()):
            state = "revealed"
        else:
            state = "stale"
        result: dict[str, Any] = {
            "trip_id": str(trip["id"]), "trip_version": int(trip["version"]),
            "active_variant": str(trip["active_variant"]), "round": round_number,
            "state": state, "submitted_by_me": bool(complete.get(user_id)),
            "partner_submitted": bool(complete.get(partner_id)),
            "candidates": [self._candidate_public(item) for item in candidates],
        }
        if state == "waiting_for_partner" and complete.get(user_id):
            own = {str(row["itinerary_item_id"]): row for row in rows_by_user[user_id]}
            result["my_preferences"] = [{
                "item_id": item["id"], "preference": str(own[item["id"]]["preference"]),
                "version": int(own[item["id"]]["version"]),
                "override_confirmed": bool(own[item["id"]]["override_confirmed_at"]),
            } for item in candidates]
        elif state == "revealed":
            identities: dict[str, dict] = {}
            for row in db.execute(
                "SELECT id,username,nickname,avatar_label FROM users WHERE id IN (?,?)",
                tuple(members),
            ).fetchall():
                identities[str(row["id"])] = {
                    "id": str(row["id"]), "username": str(row["username"]),
                    "nickname": str(row["nickname"]), "avatar_label": str(row["avatar_label"]),
                }
            ballots = []
            for member in members:
                own = {str(row["itinerary_item_id"]): row for row in rows_by_user[member]}
                ballots.append({
                    "user": identities.get(member, {"id": member}),
                    "preferences": [{
                        "item_id": item["id"], "preference": str(own[item["id"]]["preference"]),
                        "version": int(own[item["id"]]["version"]),
                        "override_confirmed": bool(own[item["id"]]["override_confirmed_at"]),
                    } for item in candidates],
                })
            result["ballots"] = ballots
            result.update(self._negotiation_outcome(candidates, rows_by_user, members))
            # Revealed state is shared; keep the complete response byte-for-byte
            # identical for both partners.
            result["submitted_by_me"] = True
            result["partner_submitted"] = True
        return result

    def get_trip_negotiation(self, trip_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, trip, partner_id = self._trip_tools_context(db, trip_id, user_id)
            return self._serialize_trip_negotiation_in_connection(db, couple, trip, user_id, partner_id)

    def submit_trip_item_preferences(self, trip_id: str, payload: object, user_id: str) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是对象")
        action = str(payload.get("action") or "submit_preferences")
        if action == "confirm_avoid_override":
            return self._confirm_trip_avoid_override(trip_id, payload, user_id)
        if action != "submit_preferences":
            raise ValueError("不支持的旅行协商操作")
        allowed = {
            "action", "expected_trip_version", "expected_round", "start_new_round", "preferences",
            "expected_preference_versions", "client_request_id", "idempotency_key",
        }
        if set(payload) - allowed:
            raise ValueError("旅行协商请求包含不支持的字段")
        expected_trip_version = _positive_int(payload.get("expected_trip_version"), "旅行版本")
        expected_round = _positive_int(payload.get("expected_round"), "协商轮次")
        start_new_round = payload.get("start_new_round", False)
        if type(start_new_round) is not bool:
            raise ValueError("是否开始新一轮必须是布尔值")
        raw_preferences = payload.get("preferences")
        if not isinstance(raw_preferences, list) or not raw_preferences or len(raw_preferences) > 100:
            raise ValueError("偏好必须是 1–100 项完整列表")
        normalized: list[dict] = []
        for raw in raw_preferences:
            if not isinstance(raw, dict) or set(raw) != {"item_id", "preference", "expected_item_version"}:
                raise ValueError("每个偏好必须包含项目、选择和项目版本")
            item_id = str(raw.get("item_id") or "").strip()
            preference = str(raw.get("preference") or "").strip()
            if not item_id or preference not in PREFERENCES:
                raise ValueError("旅行项目或偏好无效")
            normalized.append({
                "item_id": item_id, "preference": preference,
                "expected_item_version": _positive_int(raw.get("expected_item_version"), "行程项目版本"),
            })
        if len({item["item_id"] for item in normalized}) != len(normalized):
            raise ValueError("同一行程项目不能重复提交")
        expected_preference_versions = payload.get("expected_preference_versions")
        if expected_preference_versions is not None:
            if not isinstance(expected_preference_versions, dict):
                raise ValueError("旧偏好版本必须是对象")
            expected_preference_versions = {
                str(item_id): _positive_int(version, "偏好版本")
                for item_id, version in expected_preference_versions.items()
            }
        key = self._travel_request_key(payload.get("client_request_id") or payload.get("idempotency_key"))
        fingerprint = self._travel_fingerprint({
            "trip_id": trip_id, "action": action, "expected_trip_version": expected_trip_version,
            "expected_round": expected_round, "start_new_round": start_new_round,
            "preferences": normalized, "expected_preference_versions": expected_preference_versions,
        })
        operation = f"trip.negotiation:{trip_id}"
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, trip, partner_id = self._trip_tools_context(db, trip_id, user_id)
            replay = self._travel_replay(db, str(couple["id"]), user_id, operation, key, fingerprint)
            if replay is not None:
                return replay
            if int(trip["version"]) != expected_trip_version:
                raise ConflictError("旅行已经发生变化，请刷新后重新选择")
            candidates = self._active_itinerary_snapshot(db, trip)
            candidate_versions = {str(item["id"]): int(item["version"]) for item in candidates}
            supplied_versions = {item["item_id"]: item["expected_item_version"] for item in normalized}
            if set(supplied_versions) != set(candidate_versions):
                raise ValueError("必须为当前方案的全部项目提交选择")
            if supplied_versions != candidate_versions:
                raise ConflictError("行程项目已经发生变化，请刷新后重新选择")
            state = self._serialize_trip_negotiation_in_connection(db, couple, trip, user_id, partner_id)
            current_round = int(state["round"])
            if expected_round != current_round:
                raise ConflictError("旅行协商轮次已经变化")
            if start_new_round:
                if state["state"] not in {"revealed", "stale"}:
                    raise ConflictError("当前轮次尚未完成，不能开启新一轮")
                target_round = current_round + 1
                own_rows: list[sqlite3.Row] = []
            else:
                if state["state"] == "revealed":
                    raise ConflictError("本轮已经揭晓，如需修改请明确开启新一轮")
                if state["state"] == "stale":
                    raise ConflictError("旧协商已经失效，请明确开启新一轮")
                target_round = current_round
                own_rows = db.execute(
                    "SELECT * FROM trip_item_preferences WHERE trip_id=? AND negotiation_round=? AND user_id=?",
                    (trip_id, target_round, user_id),
                ).fetchall()
            existing = {str(row["itinerary_item_id"]): row for row in own_rows}
            if existing:
                if expected_preference_versions is None or set(expected_preference_versions) != set(candidate_versions):
                    raise ConflictError("修改已提交选择时必须提供全部当前偏好版本")
                if any(int(existing[item_id]["version"]) != expected_preference_versions[item_id] for item_id in candidate_versions):
                    raise ConflictError("旅行选择已在其他设备修改，请刷新")
            elif expected_preference_versions not in (None, {}):
                raise ValueError("首次提交不应包含旧偏好版本")
            for item in normalized:
                row = existing.get(item["item_id"])
                if row:
                    db.execute(
                        """UPDATE trip_item_preferences SET preference=?,expected_item_version=?,submitted_at=?,
                                  override_confirmed_at=NULL,version=version+1,updated_at=? WHERE id=? AND version=?""",
                        (item["preference"], item["expected_item_version"], now, now, row["id"], row["version"]),
                    )
                else:
                    db.execute(
                        """INSERT INTO trip_item_preferences(
                               id,trip_id,itinerary_item_id,user_id,negotiation_round,preference,
                               expected_item_version,submitted_at,version,updated_at
                           ) VALUES (?,?,?,?,?,?,?,?,1,?)""",
                        (_id("trip_pref"), trip_id, item["item_id"], user_id, target_round,
                         item["preference"], item["expected_item_version"], now, now),
                    )
            result = self._serialize_trip_negotiation_in_connection(db, couple, trip, user_id, partner_id)
            safe_meta = {"resource": "trip_negotiation", "round": target_round, "state": result["state"]}
            self._audit(db, "trip.negotiation.submitted", "trip", trip_id, safe_meta, actor_id=user_id)
            self._event(db, str(couple["id"]), user_id, "trip.negotiation.updated", trip_id, safe_meta)
            self._save_idempotent_result(db, str(couple["id"]), user_id, operation, key, {
                "_request_hash": fingerprint, "_response": result,
            })
            return result

    def _confirm_trip_avoid_override(self, trip_id: str, payload: dict, user_id: str) -> dict:
        allowed = {
            "action", "expected_round", "item_ids", "expected_preference_versions",
            "client_request_id", "idempotency_key",
        }
        if set(payload) - allowed:
            raise ValueError("否决确认请求包含不支持的字段")
        expected_round = _positive_int(payload.get("expected_round"), "协商轮次")
        item_ids = payload.get("item_ids")
        if not isinstance(item_ids, list) or not item_ids or len(item_ids) > 100 or not all(isinstance(value, str) and value.strip() for value in item_ids):
            raise ValueError("请选择需要共同确认的否决项目")
        item_ids = [value.strip() for value in item_ids]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("否决项目不能重复")
        versions = payload.get("expected_preference_versions")
        if not isinstance(versions, dict) or set(versions) != set(item_ids):
            raise ValueError("必须提供每项本人偏好的当前版本")
        versions = {item_id: _positive_int(versions[item_id], "偏好版本") for item_id in item_ids}
        key = self._travel_request_key(payload.get("client_request_id") or payload.get("idempotency_key"))
        fingerprint = self._travel_fingerprint({
            "trip_id": trip_id, "action": "confirm_avoid_override", "expected_round": expected_round,
            "item_ids": item_ids, "expected_preference_versions": versions,
        })
        operation = f"trip.negotiation.override:{trip_id}"
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, trip, partner_id = self._trip_tools_context(db, trip_id, user_id)
            replay = self._travel_replay(db, str(couple["id"]), user_id, operation, key, fingerprint)
            if replay is not None:
                return replay
            current = self._serialize_trip_negotiation_in_connection(db, couple, trip, user_id, partner_id)
            if current["state"] != "revealed" or int(current["round"]) != expected_round:
                raise ConflictError("只有当前已揭晓轮次可以共同确认否决项目")
            vetoed = set(current["vetoed_item_ids"])
            if not set(item_ids).issubset(vetoed):
                raise ValueError("只能确认本轮确实被否决的项目")
            rows = db.execute(
                "SELECT * FROM trip_item_preferences WHERE trip_id=? AND negotiation_round=? AND user_id=?",
                (trip_id, expected_round, user_id),
            ).fetchall()
            own = {str(row["itinerary_item_id"]): row for row in rows}
            for item_id in item_ids:
                row = own.get(item_id)
                if not row or int(row["version"]) != versions[item_id]:
                    raise ConflictError("旅行选择已经发生变化，请刷新")
                if row["override_confirmed_at"]:
                    raise ConflictError("这项否决已经由你确认")
                db.execute(
                    "UPDATE trip_item_preferences SET override_confirmed_at=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                    (now, now, row["id"], row["version"]),
                )
            result = self._serialize_trip_negotiation_in_connection(db, couple, trip, user_id, partner_id)
            safe_meta = {"resource": "trip_negotiation", "round": expected_round, "state": "revealed"}
            self._audit(db, "trip.negotiation.override_confirmed", "trip", trip_id, safe_meta, actor_id=user_id)
            self._event(db, str(couple["id"]), user_id, "trip.negotiation.updated", trip_id, safe_meta)
            self._save_idempotent_result(db, str(couple["id"]), user_id, operation, key, {
                "_request_hash": fingerprint, "_response": result,
            })
            return result

    @staticmethod
    def _finding(item_id: str, code: str, severity: str, buffer_minutes: int, suggestion: str,
                 source_type: str = "rules", evidence_at: str | None = None) -> dict:
        if severity not in FINDING_SEVERITIES or source_type not in SOURCE_TYPES:
            raise ValueError("真实性检查结果类型无效")
        return {
            "item_id": item_id, "code": code, "severity": severity,
            "buffer_minutes": max(0, int(buffer_minutes)), "source_type": source_type,
            "evidence_at": evidence_at, "suggestion": suggestion[:500],
        }

    @staticmethod
    def _is_kind(item: dict, kind: str) -> bool:
        item_type = str(item.get("type") or "").casefold()
        title = str(item.get("title") or "").casefold()
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        structured_values = " ".join(
            str(details.get(key) or "").casefold()
            for key in (
                "kind", "type", "mode", "transport_type", "transport_mode",
                "activity_type", "category", "meal_type",
            )
        )
        keywords = {
            "meal": ("meal", "breakfast", "lunch", "dinner", "早餐", "午餐", "晚餐", "用餐", "餐厅"),
            "luggage": ("luggage", "storage", "行李", "寄存"),
            "hotel": ("check-in", "check-out", "办理入住", "办理退房", "入住手续", "退房手续"),
            "train": ("train departure", "rail departure", "火车出发", "高铁出发", "动车出发", "列车出发"),
            "flight": ("flight departure", "航班出发", "航班起飞", "登机"),
            "queue": ("theme park", "amusement", "主题乐园", "游乐园", "迪士尼", "环球影城"),
        }
        structured_keys = {
            "train": ("train_no", "train_number", "departure_station"),
            "flight": ("flight_no", "flight_number", "departure_airport"),
        }
        structured_aliases = {
            "meal": {"meal", "breakfast", "lunch", "dinner"},
            "luggage": {"luggage", "storage"},
            "hotel": {"hotel_check_in", "hotel_check_out", "check_in", "check_out"},
            "train": {"train", "rail", "high_speed_rail"},
            "flight": {"flight", "airplane", "air"},
            "queue": {"theme_park", "amusement_park"},
        }
        structured_tokens = set(structured_values.replace("-", "_").split())
        return (
            item_type == kind
            or any(value in item_type or value in title or value in structured_values for value in keywords[kind])
            or bool(structured_tokens & structured_aliases[kind])
            or any(details.get(key) for key in structured_keys.get(kind, ()))
        )

    @staticmethod
    def _straight_distance(origin: dict, destination: dict) -> int:
        radius = 6_371_000
        lat1, lat2 = math.radians(float(origin["latitude"])), math.radians(float(destination["latitude"]))
        dlat = math.radians(float(destination["latitude"]) - float(origin["latitude"]))
        dlng = math.radians(float(destination["longitude"]) - float(origin["longitude"]))
        value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
        return int(round(radius * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))))

    @classmethod
    def _cached_route(cls, db: sqlite3.Connection, origin: dict, destination: dict, now: str) -> dict | None:
        rows = db.execute(
            """SELECT provider,mode,result_json,fetched_at,expires_at FROM route_cache
               WHERE ABS(origin_latitude-?)<0.000001 AND ABS(origin_longitude-?)<0.000001
                 AND ABS(destination_latitude-?)<0.000001 AND ABS(destination_longitude-?)<0.000001
               ORDER BY fetched_at DESC""",
            (origin["latitude"], origin["longitude"], destination["latitude"], destination["longitude"]),
        ).fetchall()
        now_at = _aware_iso_timestamp(now)
        if now_at is None:
            return None
        distance = cls._straight_distance(origin, destination)
        if distance <= 1500:
            mode_order = ("walking", "bicycling", "cycling")
        else:
            mode_order = ("transit", "driving")
        mode_rank = {mode: index for index, mode in enumerate(mode_order)}
        trusted: list[tuple[int, sqlite3.Row, dict]] = []
        for row in rows:
            value = _json(row["result_json"], {})
            provider = str(row["provider"] or "").strip().casefold()
            declared_provider = value.get("provider")
            embedded_provider = str(declared_provider or provider).strip().casefold()
            declared_source_type = value.get("source_type")
            source_type = str(declared_source_type or "real_api").strip().casefold()
            if (
                provider not in TRUSTED_ROUTE_CACHE_PROVIDERS
                or embedded_provider not in TRUSTED_ROUTE_CACHE_PROVIDERS
                or (declared_provider is not None and embedded_provider != provider)
                or source_type not in TRUSTED_ROUTE_SOURCE_TYPES
                or value.get("mock") or value.get("unavailable")
            ):
                continue
            fetched_at = _aware_iso_timestamp(row["fetched_at"])
            expires_at = _aware_iso_timestamp(row["expires_at"])
            if (
                fetched_at is None or expires_at is None
                or not (fetched_at <= now_at < expires_at)
                or not fetched_at < expires_at
            ):
                continue
            seconds = value.get("duration_seconds")
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
                minutes = value.get("duration_minutes")
                if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes <= 0:
                    continue
                seconds = float(minutes) * 60
            rank = mode_rank.get(str(row["mode"]))
            if rank is None:
                continue
            trusted.append((rank, row, {"minutes": max(1, math.ceil(float(seconds) / 60))}))
        if trusted:
            _, row, parsed = min(trusted, key=lambda entry: entry[0])
            return {
                **parsed, "fetched_at": row["fetched_at"], "mode": row["mode"],
                "provider": row["provider"], "distance_meters": distance,
            }
        return None

    def _realism_findings(self, db: sqlite3.Connection, items: list[dict]) -> list[dict]:
        findings: list[dict] = []
        by_day: dict[str, list[dict]] = {}
        for item in items:
            by_day.setdefault(str(item["day_id"]), []).append(item)
            duration = int(item["duration_minutes"])
            if self._is_kind(item, "meal") and duration < 60:
                findings.append(self._finding(item["id"], "meal_duration", "warning", 60 - duration, "用餐至少预留 60 分钟。"))
            if self._is_kind(item, "luggage") and duration < 30:
                findings.append(self._finding(item["id"], "luggage_storage", "warning", 30 - duration, "行李寄存或领取至少预留 30 分钟。"))
            if self._is_kind(item, "hotel") and duration < 30:
                findings.append(self._finding(item["id"], "hotel_procedure", "warning", 30 - duration, "酒店入住或退房手续至少预留 30 分钟。"))
            if self._is_kind(item, "queue"):
                findings.append(self._finding(item["id"], "queue_estimate", "warning", 60, "热门项目建议额外预留 60 分钟；这是经验估计，请在出发当天核对。"))
        now = _now()
        for day_items in by_day.values():
            for index, item in enumerate(day_items):
                start = _minute_of_day(item.get("start_time"))
                if start is None:
                    findings.append(self._finding(item["id"], "schedule_invalid", "conflict", 0, "开始时间无效，请手工检查。"))
                    continue
                if index == 0 and self._is_kind(item, "train"):
                    findings.append(self._finding(
                        item["id"], "train_early_arrival", "warning", 45,
                        "当天首项是火车出发，但尚未安排到站时间；请至少提前 45 分钟到站。",
                    ))
                if index == 0 and self._is_kind(item, "flight"):
                    findings.append(self._finding(
                        item["id"], "flight_early_arrival", "warning", 120,
                        "当天首项是航班出发，但尚未安排到达机场时间；请至少提前 120 分钟到达。",
                    ))
                if index > 0:
                    previous = day_items[index - 1]
                    previous_start = _minute_of_day(previous.get("start_time"))
                    if previous_start is not None:
                        previous_end = previous_start + int(previous["duration_minutes"])
                        gap = start - previous_end
                        origin_has_place = previous.get("latitude") is not None and previous.get("longitude") is not None
                        destination_has_place = item.get("latitude") is not None and item.get("longitude") is not None
                        route = self._cached_route(db, previous, item, now) if origin_has_place and destination_has_place else None
                        route_minutes = int(route["minutes"]) if route else 0
                        if self._is_kind(item, "train"):
                            required = route_minutes + 45
                            if gap < required:
                                route_note = f"（其中路线约 {route_minutes} 分钟）" if route else ""
                                findings.append(self._finding(
                                    item["id"], "train_early_arrival", "conflict", max(0, required - gap),
                                    f"从上一站出发后还需至少提前 45 分钟到站{route_note}。",
                                ))
                        if self._is_kind(item, "flight"):
                            required = route_minutes + 120
                            if gap < required:
                                route_note = f"（其中路线约 {route_minutes} 分钟）" if route else ""
                                findings.append(self._finding(
                                    item["id"], "flight_early_arrival", "conflict", max(0, required - gap),
                                    f"从上一站出发后还需至少提前 120 分钟到达机场{route_note}。",
                                ))
                        if route:
                            required = route_minutes + 15
                            severity = "conflict" if gap < required else "info"
                            findings.append(self._finding(
                                item["id"], "transfer_buffer", severity, 15,
                                f"已保存路线约 {route['minutes']} 分钟，另需预留 15 分钟换乘缓冲。",
                                "cache", str(route["fetched_at"]),
                            ))
                        else:
                            findings.append(self._finding(
                                item["id"], "route_missing", "warning", 15,
                                "没有可用的近期真实路线，交通时间仍未知；请确认地点后查询路线。",
                            ))
                            if gap < 15:
                                findings.append(self._finding(
                                    item["id"], "transfer_buffer", "conflict", 15 - max(0, gap),
                                    "相邻活动之间仍需至少预留 15 分钟换乘缓冲；实际交通时间尚未确认。",
                                ))
                            if origin_has_place and destination_has_place:
                                distance = self._straight_distance(previous, item)
                                if distance <= 1500:
                                    estimated = math.ceil(distance / 80)
                                elif distance <= 8000:
                                    estimated = math.ceil(distance / 350) + 10
                                else:
                                    estimated = math.ceil(distance / 500) + 10
                                if gap < estimated + 15:
                                    findings.append(self._finding(
                                        item["id"], "transfer_buffer", "conflict", 15,
                                        f"按直线距离本地估算交通约 {estimated} 分钟，另需预留 15 分钟；这不是地图路线。",
                                    ))
        return findings[:200]

    @staticmethod
    def _serialize_realism_check(row: sqlite3.Row | dict, items: list[dict], effective_status: str) -> dict:
        raw = dict(row)
        findings = _json(raw.get("findings_json", "[]"), [])
        locked = {str(item["id"]): bool(item["locked"]) for item in items}
        return {
            "id": str(raw["id"]), "trip_id": str(raw["trip_id"]),
            "creator_id": str(raw["creator_id"]), "trip_version": int(raw["trip_version"]),
            "expected_item_versions": _json(raw.get("expected_item_versions_json", "{}"), {}),
            "findings": [
                {**finding, "apply_allowed": not locked.get(str(finding.get("item_id")), True)}
                for finding in findings
            ],
            "source_type": str(raw["source_type"]), "status": str(raw["status"]),
            "version": int(raw["version"]), "checked_at": str(raw["checked_at"]),
            "expires_at": raw.get("expires_at"), "effective_status": effective_status,
            "actionable": effective_status == "current",
        }

    def run_trip_realism_check(self, trip_id: str, payload: object, user_id: str) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是对象")
        allowed = {"expected_trip_version", "expected_item_versions", "client_request_id", "idempotency_key"}
        if set(payload) - allowed:
            raise ValueError("真实性检查请求包含不支持的字段")
        expected_trip_version = _positive_int(payload.get("expected_trip_version"), "旅行版本")
        expected_items = payload.get("expected_item_versions")
        if expected_items is not None:
            if not isinstance(expected_items, dict):
                raise ValueError("行程项目版本必须是对象")
            expected_items = {str(item_id): _positive_int(version, "行程项目版本") for item_id, version in expected_items.items()}
        key = self._travel_request_key(payload.get("client_request_id") or payload.get("idempotency_key"))
        fingerprint = self._travel_fingerprint({
            "trip_id": trip_id, "expected_trip_version": expected_trip_version,
            "expected_item_versions": expected_items,
        })
        operation = f"trip.realism:{trip_id}"
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, trip, _ = self._trip_tools_context(db, trip_id, user_id)
            replay = self._travel_replay(db, str(couple["id"]), user_id, operation, key, fingerprint)
            if replay is not None:
                return replay
            if int(trip["version"]) != expected_trip_version:
                raise ConflictError("旅行已经发生变化，请刷新后重新检查")
            items = self._active_itinerary_snapshot(db, trip)
            versions = {str(item["id"]): int(item["version"]) for item in items}
            if expected_items is not None and expected_items != versions:
                raise ConflictError("行程项目已经发生变化，请刷新后重新检查")
            findings = self._realism_findings(db, items)
            check_id = _id("realism")
            expires_at = (now + timedelta(hours=24)).isoformat()
            db.execute("UPDATE trip_realism_checks SET status='stale',version=version+1 WHERE trip_id=? AND status='current'", (trip_id,))
            db.execute(
                """INSERT INTO trip_realism_checks(
                       id,trip_id,creator_id,trip_version,expected_item_versions_json,findings_json,
                       source_type,status,client_request_id,version,checked_at,expires_at
                   ) VALUES (?,?,?,?,?,?,'rules','current',?,1,?,?)""",
                (check_id, trip_id, user_id, int(trip["version"]), json.dumps(versions, ensure_ascii=False),
                 json.dumps(findings, ensure_ascii=False), key, now.isoformat(), expires_at),
            )
            row = db.execute("SELECT * FROM trip_realism_checks WHERE id=?", (check_id,)).fetchone()
            result = self._serialize_realism_check(row, items, "current")
            safe_meta = {"resource": "trip_realism_check", "status": "current", "finding_count": len(findings)}
            self._audit(db, "trip.realism.checked", "trip", trip_id, safe_meta, actor_id=user_id)
            self._event(db, str(couple["id"]), user_id, "trip.realism.updated", trip_id, safe_meta)
            self._save_idempotent_result(db, str(couple["id"]), user_id, operation, key, {
                "_request_hash": fingerprint, "_response": result,
            })
            return result

    def get_trip_realism_check(self, trip_id: str, user_id: str) -> dict:
        with self.connection() as db:
            _, trip, _ = self._trip_tools_context(db, trip_id, user_id)
            items = self._active_itinerary_snapshot(db, trip)
            row = db.execute(
                "SELECT * FROM trip_realism_checks WHERE trip_id=? ORDER BY checked_at DESC,rowid DESC LIMIT 1",
                (trip_id,),
            ).fetchone()
            if not row:
                return {"trip_id": trip_id, "effective_status": "missing", "actionable": False, "findings": []}
            versions = {str(item["id"]): int(item["version"]) for item in items}
            expected = _json(row["expected_item_versions_json"], {})
            now = _now()
            effective = str(row["status"])
            if (
                effective != "current" or int(row["trip_version"]) != int(trip["version"])
                or expected != versions or (row["expires_at"] and str(row["expires_at"]) <= now)
            ):
                effective = "stale"
            return self._serialize_realism_check(row, items, effective)

    def _cleanup_incomplete_trip_negotiations(self, db: sqlite3.Connection, couple_id: str) -> None:
        couple = db.execute("SELECT member_a,member_b FROM couples WHERE id=?", (couple_id,)).fetchone()
        if not couple:
            return
        members = [str(couple["member_a"]), str(couple["member_b"])]
        trips = db.execute("SELECT * FROM trips WHERE couple_id=? AND deleted_at IS NULL", (couple_id,)).fetchall()
        for trip in trips:
            round_number = self._current_negotiation_round(db, str(trip["id"]), couple_id)
            rows = db.execute(
                "SELECT * FROM trip_item_preferences WHERE trip_id=? AND negotiation_round=?",
                (trip["id"], round_number),
            ).fetchall()
            if not rows:
                continue
            try:
                items = self._active_itinerary_snapshot(db, trip)
            except ValueError:
                db.execute(
                    "DELETE FROM trip_item_preferences WHERE trip_id=? AND negotiation_round=?",
                    (trip["id"], round_number),
                )
                continue
            by_user = {member: [row for row in rows if row["user_id"] == member] for member in members}
            if not all(self._complete_ballot(by_user[member], items) for member in members):
                db.execute(
                    "DELETE FROM trip_item_preferences WHERE trip_id=? AND negotiation_round=?",
                    (trip["id"], round_number),
                )
        db.execute(
            "DELETE FROM mutation_idempotency WHERE couple_id=? AND operation LIKE 'trip.negotiation%'",
            (couple_id,),
        )

    # Task 12: packing, emergency cards, and a deliberately bounded offline
    # projection.  These methods share the trip authorization and concurrency
    # primitives above but never expose their raw SQL rows.

    @staticmethod
    def _packing_assignment(db: sqlite3.Connection, couple: sqlite3.Row, value: object) -> tuple[str, str | None]:
        assigned_to = str(value or "shared").strip()
        if assigned_to == "shared":
            return "shared", None
        if assigned_to == "none":
            return "not_needed", None
        member_ids = {str(couple["member_a"]), str(couple["member_b"])}
        if assigned_to not in member_ids:
            raise ValueError("行李只能分配给当前情侣成员、共同携带或不需要")
        active = db.execute(
            "SELECT 1 FROM users WHERE id=? AND deleted_at IS NULL", (assigned_to,),
        ).fetchone()
        if not active:
            raise ValueError("被分配的成员已经不可用")
        return "person", assigned_to

    @staticmethod
    def _public_packing_item(row: sqlite3.Row | dict, names: dict[str, str]) -> dict:
        value = dict(row)
        assignment = str(value["assignment"])
        assigned_to = "shared" if assignment == "shared" else "none"
        if assignment == "person":
            assigned_to = str(value["assigned_user_id"])
        return {
            "id": str(value["id"]),
            "label": str(value["label"]),
            "assigned_to": assigned_to,
            "assigned_name": names.get(assigned_to, "共同携带" if assigned_to == "shared" else "不需要"),
            "checked": bool(value["checked"]),
            "position": int(value["position"]),
            "source_type": str(value["source_type"]),
            "version": int(value["version"]),
            "created_at": str(value["created_at"]),
            "updated_at": str(value["updated_at"]),
        }

    @staticmethod
    def _trip_member_names(db: sqlite3.Connection, couple: sqlite3.Row) -> dict[str, str]:
        ids = [str(couple["member_a"]), str(couple["member_b"])]
        rows = db.execute(
            "SELECT id,nickname FROM users WHERE id IN (?,?) AND deleted_at IS NULL", ids,
        ).fetchall()
        return {str(row["id"]): str(row["nickname"]) for row in rows}

    def _list_trip_packing_db(self, db: sqlite3.Connection, trip_id: str, couple: sqlite3.Row) -> list[dict]:
        names = self._trip_member_names(db, couple)
        rows = db.execute(
            "SELECT * FROM trip_packing_items WHERE trip_id=? AND deleted_at IS NULL ORDER BY position,id",
            (trip_id,),
        ).fetchall()
        return [self._public_packing_item(row, names) for row in rows]

    def list_trip_packing(self, trip_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, trip, _ = self._trip_tools_context(db, trip_id, user_id)
            return {
                "trip_id": trip_id,
                "trip_version": int(trip["version"]),
                "items": self._list_trip_packing_db(db, trip_id, couple),
            }

    @staticmethod
    def _packing_rule_labels(trip: dict, itinerary: list[dict], forecast: dict | None) -> list[str]:
        labels = ["身份证件", "常用药", "手机充电器", "洗漱用品", "换洗衣物"]
        try:
            days = max(1, (date.fromisoformat(str(trip["end_date"])) - date.fromisoformat(str(trip["start_date"]))).days + 1)
        except ValueError:
            days = 1
        if days >= 3:
            labels.append("便携洗衣袋")
        text = " ".join(
            f"{item.get('title', '')} {item.get('note', '')} {json.dumps(item.get('details') or {}, ensure_ascii=False)}"
            for item in itinerary
        ).casefold()
        rules = (
            (("泳", "海滩", "水上"), "泳衣与防水袋"),
            (("徒步", "登山", "远足"), "防滑徒步鞋"),
            (("乐园", "迪士尼", "主题公园"), "轻便双肩包"),
            (("摄影", "拍照", "相机"), "相机与备用电池"),
        )
        for keywords, label in rules:
            if any(keyword in text for keyword in keywords):
                labels.append(label)
        for item in (forecast or {}).get("items", []):
            probability = item.get("precipitation_probability")
            low, high = item.get("temperature_min"), item.get("temperature_max")
            if isinstance(probability, (int, float)) and probability >= 40:
                labels.append("雨伞或轻便雨衣")
            if isinstance(high, (int, float)) and high >= 30:
                labels.extend(["防晒霜", "遮阳帽"])
            if isinstance(low, (int, float)) and low <= 10:
                labels.append("保暖外套")
        return list(dict.fromkeys(label.strip() for label in labels if label.strip()))

    def generate_trip_packing(
        self, trip_id: str, payload: dict, user_id: str, weather_provider: object,
    ) -> dict:
        key = self._travel_request_key(payload.get("client_request_id") or payload.get("idempotency_key"))
        expected_trip_version = _positive_int(payload.get("expected_trip_version"), "旅行版本")
        fingerprint = self._travel_fingerprint({"trip_id": trip_id, "expected_trip_version": expected_trip_version})

        # Provider I/O is deliberately outside the write transaction.
        with self.connection() as db:
            couple, trip_row, _ = self._trip_tools_context(db, trip_id, user_id)
            replay = self._travel_replay(db, str(couple["id"]), user_id, f"trip.packing.generate:{trip_id}", key, fingerprint)
            if replay is not None:
                return replay
            trip = dict(trip_row)
            destination = db.execute("SELECT * FROM trip_destination_places WHERE trip_id=?", (trip_id,)).fetchone()
            itinerary = self._active_itinerary_snapshot(db, trip_row)
        forecast = None
        fallback_reason = "天气未能核对"
        if destination and not getattr(weather_provider, "enabled", True) is False:
            try:
                forecast = weather_provider.forecast(
                    destination["latitude"], destination["longitude"], trip["start_date"], trip["end_date"], "auto",
                )
            except (RuntimeError, ValueError, TypeError):
                forecast = None
        if forecast:
            evidence_at = _aware_iso_timestamp(forecast.get("evidence_at")) if isinstance(forecast, dict) else None
            if (
                not isinstance(forecast, dict)
                or forecast.get("source_type") != "real_api"
                or not isinstance(forecast.get("items"), list)
                or evidence_at is None
                or evidence_at > datetime.now(timezone.utc)
            ):
                forecast = None
        labels = self._packing_rule_labels(trip, itinerary, forecast)
        evidence = {
            "weather_checked": bool(forecast),
            "label": "已核对天气" if forecast else fallback_reason,
            "source": str((forecast or {}).get("source") or "本地规则"),
            "source_type": str((forecast or {}).get("source_type") or "rules"),
            "evidence_at": (forecast or {}).get("evidence_at"),
        }
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, current, _ = self._trip_tools_context(db, trip_id, user_id)
            replay = self._travel_replay(db, str(couple["id"]), user_id, f"trip.packing.generate:{trip_id}", key, fingerprint)
            if replay is not None:
                return replay
            if int(current["version"]) != expected_trip_version:
                raise ConflictError("旅行已经发生变化，请刷新后重新生成行李")
            existing = {
                re.sub(r"\s+", "", str(row["label"])).casefold()
                for row in db.execute(
                    "SELECT label FROM trip_packing_items WHERE trip_id=? AND deleted_at IS NULL", (trip_id,),
                ).fetchall()
            }
            position = int(db.execute(
                "SELECT COALESCE(MAX(position),0) FROM trip_packing_items WHERE trip_id=? AND deleted_at IS NULL",
                (trip_id,),
            ).fetchone()[0])
            for label in labels:
                normalized = re.sub(r"\s+", "", label).casefold()
                if normalized in existing:
                    continue
                position += 1
                db.execute(
                    """INSERT INTO trip_packing_items(
                           id,trip_id,creator_id,label,assigned_user_id,assignment,checked,position,
                           source_type,client_request_id,version,deleted_at,created_at,updated_at
                       ) VALUES (?,?,?, ?,NULL,'shared',0,?,'rules',NULL,1,NULL,?,?)""",
                    (_id("packing"), trip_id, user_id, label[:200], position, now, now),
                )
                existing.add(normalized)
            result = {
                "trip_id": trip_id,
                "trip_version": int(current["version"]),
                "evidence": evidence,
                "items": self._list_trip_packing_db(db, trip_id, couple),
            }
            safe = {"resource": "trip_packing", "action": "generated", "item_count": len(result["items"])}
            self._audit(db, "trip.packing.generated", "trip", trip_id, safe, actor_id=user_id)
            self._event(db, str(couple["id"]), user_id, "trip.tools_updated", trip_id, safe)
            self._save_idempotent_result(db, str(couple["id"]), user_id, f"trip.packing.generate:{trip_id}", key, {
                "_request_hash": fingerprint, "_response": result,
            })
            return result

    def create_trip_packing_item(self, trip_id: str, payload: dict, idempotency_key: object, user_id: str) -> dict:
        key = self._travel_request_key(idempotency_key)
        label = str(payload.get("label") or "").strip()
        if not 1 <= len(label) <= 200:
            raise ValueError("行李名称需为 1–200 个字符")
        expected_trip_version = _positive_int(payload.get("expected_trip_version"), "旅行版本")
        fingerprint = self._travel_fingerprint({
            "trip_id": trip_id, "label": label, "assigned_to": payload.get("assigned_to", "shared"),
            "expected_trip_version": expected_trip_version,
        })
        operation = f"trip.packing.create:{trip_id}"
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, trip, _ = self._trip_tools_context(db, trip_id, user_id)
            replay = self._travel_replay(db, str(couple["id"]), user_id, operation, key, fingerprint)
            if replay is not None:
                return replay
            if int(trip["version"]) != expected_trip_version:
                raise ConflictError("旅行已经发生变化，请刷新后重试")
            assignment, assigned_user_id = self._packing_assignment(db, couple, payload.get("assigned_to", "shared"))
            position = int(db.execute(
                "SELECT COALESCE(MAX(position),0)+1 FROM trip_packing_items WHERE trip_id=? AND deleted_at IS NULL",
                (trip_id,),
            ).fetchone()[0])
            item_id = _id("packing")
            db.execute(
                """INSERT INTO trip_packing_items(
                       id,trip_id,creator_id,label,assigned_user_id,assignment,checked,position,source_type,
                       client_request_id,version,deleted_at,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,0,?,'human',?,1,NULL,?,?)""",
                (item_id, trip_id, user_id, label, assigned_user_id, assignment, position, key, now, now),
            )
            row = db.execute("SELECT * FROM trip_packing_items WHERE id=?", (item_id,)).fetchone()
            result = self._public_packing_item(row, self._trip_member_names(db, couple))
            self._audit(db, "trip.packing.created", "trip", trip_id, {"resource": "trip_packing", "action": "created"}, actor_id=user_id)
            self._event(db, str(couple["id"]), user_id, "trip.tools_updated", trip_id, {"resource": "trip_packing", "action": "changed"})
            self._save_idempotent_result(db, str(couple["id"]), user_id, operation, key, {"_request_hash": fingerprint, "_response": result})
            return result

    def update_trip_packing_item(self, trip_id: str, item_id: str, payload: dict, user_id: str) -> dict:
        expected = _positive_int(payload.get("expected_version"), "行李版本")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _, _ = self._trip_tools_context(db, trip_id, user_id)
            row = db.execute(
                "SELECT * FROM trip_packing_items WHERE id=? AND trip_id=? AND deleted_at IS NULL", (item_id, trip_id),
            ).fetchone()
            if not row:
                raise KeyError("行李项目不存在")
            if int(row["version"]) != expected:
                raise ConflictError("行李项目已被另一方修改")
            label = str(payload.get("label", row["label"])).strip()
            if not 1 <= len(label) <= 200:
                raise ValueError("行李名称需为 1–200 个字符")
            assignment, assigned_user_id = self._packing_assignment(
                db, couple, payload.get("assigned_to", self._public_packing_item(row, {})["assigned_to"]),
            )
            checked_value = payload.get("checked", bool(row["checked"]))
            if not isinstance(checked_value, bool):
                raise ValueError("勾选状态必须为布尔值")
            now = _now()
            db.execute(
                """UPDATE trip_packing_items SET label=?,assigned_user_id=?,assignment=?,checked=?,
                          version=version+1,updated_at=? WHERE id=? AND version=?""",
                (label, assigned_user_id, assignment, int(checked_value), now, item_id, expected),
            )
            updated = db.execute("SELECT * FROM trip_packing_items WHERE id=?", (item_id,)).fetchone()
            result = self._public_packing_item(updated, self._trip_member_names(db, couple))
            self._audit(db, "trip.packing.updated", "trip", trip_id, {"resource": "trip_packing", "action": "changed"}, actor_id=user_id)
            self._event(db, str(couple["id"]), user_id, "trip.tools_updated", trip_id, {"resource": "trip_packing", "action": "changed"})
            return result

    def delete_trip_packing_item(self, trip_id: str, item_id: str, expected_version: object, user_id: str) -> dict:
        expected = _positive_int(expected_version, "行李版本")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _, _ = self._trip_tools_context(db, trip_id, user_id)
            row = db.execute(
                "SELECT * FROM trip_packing_items WHERE id=? AND trip_id=? AND deleted_at IS NULL", (item_id, trip_id),
            ).fetchone()
            if not row:
                raise KeyError("行李项目不存在")
            if int(row["version"]) != expected:
                raise ConflictError("行李项目已被另一方修改")
            now = _now()
            db.execute("UPDATE trip_packing_items SET deleted_at=?,version=version+1,updated_at=? WHERE id=?", (now, now, item_id))
            self._audit(db, "trip.packing.deleted", "trip", trip_id, {"resource": "trip_packing", "action": "deleted"}, actor_id=user_id)
            self._event(db, str(couple["id"]), user_id, "trip.tools_updated", trip_id, {"resource": "trip_packing", "action": "changed"})
            return {"id": item_id, "deleted": True, "version": expected + 1}

    @staticmethod
    def _bounded_text(value: object, limit: int, label: str) -> str:
        text = str(value or "").strip()
        if len(text) > limit:
            raise ValueError(f"{label}过长")
        return text

    @classmethod
    def _normalize_emergency_details(cls, entry_type: str, details: object, *, server_hospital: bool = False) -> dict:
        if entry_type not in EMERGENCY_ENTRY_TYPES:
            raise ValueError("未知的应急卡类型")
        if not isinstance(details, dict):
            raise ValueError("应急卡详情必须是对象")
        if entry_type == "hospital" and not server_hospital:
            raise ValueError("医院必须从真实地图候选中选择")
        allowed = EMERGENCY_FIELDS[entry_type]
        unknown = set(details) - allowed
        if unknown:
            raise ValueError(f"应急卡包含不支持字段：{sorted(unknown)[0]}")
        for key in details:
            folded = str(key).casefold().replace("-", "_")
            if any(part in folded for part in PROHIBITED_EMERGENCY_KEY_PARTS):
                raise ValueError("应急卡不能保存密码、证件或支付信息")
        result: dict[str, object] = {}
        limits = {
            "front_desk_note": 500, "status_note": 500, "note": 1000,
            "coverage_summary": 1000, "booking_reference": 120, "policy_number": 120,
        }
        for key, raw in details.items():
            if raw is None or raw == "":
                continue
            if key in {"latitude", "longitude"}:
                try:
                    number = float(raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError("医院坐标无效") from exc
                if key == "latitude" and not -90 <= number <= 90:
                    raise ValueError("医院纬度无效")
                if key == "longitude" and not -180 <= number <= 180:
                    raise ValueError("医院经度无效")
                result[key] = number
                continue
            limit = limits.get(key, 300)
            text = cls._bounded_text(raw, limit, key)
            if key in {"check_in_at", "check_out_at", "departure_at", "arrival_at", "verified_at"}:
                if _aware_iso_timestamp(text) is None:
                    raise ValueError(f"{key} 必须是带时区的 ISO 时间")
            elif key in {"valid_from", "valid_to"}:
                try:
                    date.fromisoformat(text)
                except ValueError as exc:
                    raise ValueError(f"{key} 必须是 ISO 日期") from exc
            elif key == "transport_type" and text not in {"flight", "train", "bus", "ship", "car", "other"}:
                raise ValueError("返程交通类型无效")
            result[key] = text
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 8192:
            raise ValueError("应急卡详情不能超过 8 KiB")
        return result

    @classmethod
    def _public_emergency_entry(cls, row: sqlite3.Row | dict, user_id: str) -> dict:
        value = dict(row)
        raw_details = _json(value.get("details_json"), {})
        details: dict[str, object] = {}
        entry_type = str(value["entry_type"])
        if isinstance(raw_details, dict):
            # Existing databases can contain pre-v34 or manually repaired rows.
            # Re-validate every public field independently so an invalid legacy
            # key/value is omitted instead of leaking or making the whole list
            # unreadable.
            for key in EMERGENCY_FIELDS.get(entry_type, set()):
                if key not in raw_details:
                    continue
                try:
                    details.update(cls._normalize_emergency_details(
                        entry_type,
                        {key: raw_details[key]},
                        server_hospital=entry_type == "hospital",
                    ))
                except (TypeError, ValueError):
                    continue
        return {
            "id": str(value["id"]),
            "entry_type": entry_type,
            "title": str(value["title"]),
            "details": details,
            "visibility": str(value["visibility"]),
            "visibility_label": "双方可见" if value["visibility"] == "shared" else "仅自己可见",
            "source_type": str(value["source_type"]),
            "version": int(value["version"]),
            "created_at": str(value["created_at"]),
            "updated_at": str(value["updated_at"]),
            "editable_by_me": str(value["creator_id"]) == user_id,
        }

    @staticmethod
    def _emergency_row_visible_to_user(row: sqlite3.Row | dict, user_id: str) -> bool:
        value = dict(row)
        return str(value["creator_id"]) == user_id or str(value["visibility"]) == "shared"

    @staticmethod
    def _advance_trip_permission_version(db: sqlite3.Connection, couple_id: str) -> int:
        db.execute("UPDATE couples SET version=version+1 WHERE id=?", (couple_id,))
        return int(db.execute("SELECT version FROM couples WHERE id=?", (couple_id,)).fetchone()[0])

    def _emit_emergency_invalidation(
        self,
        db: sqlite3.Connection,
        couple_id: str,
        user_id: str,
        trip_id: str,
        entry_id: str,
        *,
        shared_scope: bool,
        permission_version: int,
    ) -> None:
        """Invalidate clients without putting emergency metadata in realtime.

        A shared row that becomes private or deleted no longer passes resource
        visibility checks, so its revocation event must be trip-scoped.  Private
        changes remain owner-only and never reveal even an entry identifier to
        the partner.
        """
        if shared_scope:
            self._event(
                db,
                couple_id,
                user_id,
                "trip.tools_updated",
                trip_id,
                {
                    "resource": "trip",
                    "action": "tools_changed",
                    "permission_version": permission_version,
                },
            )
            return
        self._event(
            db,
            couple_id,
            user_id,
            "trip.tools_updated",
            entry_id,
            {"resource": "trip_emergency", "action": "changed", "owner_only": True},
        )

    def list_trip_emergency(self, trip_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, trip, _ = self._trip_tools_context(db, trip_id, user_id)
            rows = db.execute(
                """SELECT * FROM trip_emergency_entries WHERE trip_id=? AND deleted_at IS NULL
                   AND (creator_id=? OR visibility='shared') ORDER BY position,id""",
                (trip_id, user_id),
            ).fetchall()
            items = [self._public_emergency_entry(row, user_id) for row in rows]
            return {
                "trip_id": trip_id, "trip_version": int(trip["version"]),
                "permission_version": int(couple["version"]), "items": items, "count": len(items),
            }

    @staticmethod
    def _same_destination_region(candidate: dict, destination: sqlite3.Row | dict) -> bool:
        expected_country = str(destination["country"] or "").strip().casefold()
        actual_country = str(candidate.get("country") or "").strip().casefold()
        if expected_country and actual_country and expected_country != actual_country:
            return False
        normalize_region = lambda value: re.sub(
            r"(?:特别行政区|自治区|自治州|地区|province|prefecture|city|省|市|州)$",
            "",
            re.sub(r"\s+", "", str(value or "").strip().casefold()),
        )
        expected_city = normalize_region(destination["city"])
        actual_city = normalize_region(candidate.get("city"))
        # Coordinates alone cannot disambiguate neighbouring cities.  A
        # hospital is safety-critical, so both the confirmed destination and
        # the provider candidate must carry the same verifiable city.
        if not expected_city or not actual_city or expected_city != actual_city:
            return False
        try:
            lat1, lon1 = float(destination["latitude"]), float(destination["longitude"])
            lat2, lon2 = float(candidate["latitude"]), float(candidate["longitude"])
        except (TypeError, ValueError):
            return False
        radius = 6371.0
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
        value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value)) <= 120

    @staticmethod
    def _healthcare_candidate(raw: dict) -> bool:
        text = f"{raw.get('name', '')} {raw.get('category', '')}".casefold()
        return any(marker in text for marker in HEALTHCARE_MARKERS)

    def search_trip_hospitals(self, trip_id: str, payload: dict, user_id: str) -> dict:
        with self.connection() as db:
            _, trip, _ = self._trip_tools_context(db, trip_id, user_id)
            destination = db.execute("SELECT * FROM trip_destination_places WHERE trip_id=?", (trip_id,)).fetchone()
            if not destination:
                raise ValueError("请先确认旅行目的地")
            destination_value = dict(destination)
            region = str(destination["city"] or destination["province"] or trip["destination"])
        provider = self._map_provider_for_places(destination_value)
        if getattr(provider, "is_mock", True):
            return {"items": [], "configured": False, "notice": "真实地图未配置，无法核验医院"}
        keyword = str(payload.get("query") or "医院 急救").strip()[:80]
        raw_items = provider.search_places(keyword, region, 12)
        now = _now()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0).isoformat()
        stored: list[dict] = []
        with self.connection() as db:
            self._trip_tools_context(db, trip_id, user_id)
            db.execute("DELETE FROM place_search_candidates WHERE expires_at<?", (now,))
            for raw in raw_items:
                item = normalize_place_candidate(raw)
                item.update({
                    "source_type": "real_api", "evidence_at": now,
                    "telephone": str(raw.get("telephone") or raw.get("phone") or "")[:100],
                })
                if (
                    str(item.get("provider") or provider.name) not in TRUSTED_PLACE_PROVIDERS
                    or not item.get("provider_place_id")
                    or item.get("latitude") is None or item.get("longitude") is None
                    or not self._healthcare_candidate(item)
                    or not self._same_destination_region(item, destination_value)
                ):
                    continue
                item["provider"] = str(item.get("provider") or provider.name)
                candidate_id = _id("place")
                db.execute(
                    "INSERT INTO place_search_candidates(id,user_id,query,region_hint,payload_json,expires_at,created_at) VALUES (?,?,?,?,?,?,?)",
                    (candidate_id, user_id, keyword, region, json.dumps(item, ensure_ascii=False), expires_at, now),
                )
                stored.append({"candidate_id": candidate_id, **item})
        return {"items": stored, "configured": True, "notice": "", "expires_at": expires_at}

    def _hospital_details_from_candidate(
        self, db: sqlite3.Connection, trip_id: str, candidate_id: object, user_id: str,
    ) -> tuple[dict, str]:
        candidate_key = str(candidate_id or "").strip()
        if not candidate_key:
            raise ValueError("请选择经过地图核验的医院")
        row = db.execute(
            "SELECT * FROM place_search_candidates WHERE id=? AND user_id=?", (candidate_key, user_id),
        ).fetchone()
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        if not row:
            raise ValueError("医院候选不存在或不属于当前账号")
        created = _aware_iso_timestamp(row["created_at"])
        expires = _aware_iso_timestamp(row["expires_at"])
        if (
            created is None
            or expires is None
            or created > now_dt
            or expires <= now_dt
            or expires <= created
            or expires - created > timedelta(minutes=31)
        ):
            raise ValueError("医院候选已失效，请重新搜索")
        raw = _json(row["payload_json"], {})
        if not isinstance(raw, dict):
            raise ValueError("医院候选数据无效")
        provider = str(raw.get("provider") or "")
        source_type = str(raw.get("source_type") or "")
        if provider not in TRUSTED_PLACE_PROVIDERS or source_type not in {"real_api", "cache"}:
            raise ValueError("医院必须来自真实地图服务")
        if source_type == "cache" and str(raw.get("original_source_type") or "") != "real_api":
            raise ValueError("医院缓存缺少可验证的真实来源")
        if not raw.get("provider_place_id") or raw.get("latitude") is None or raw.get("longitude") is None:
            raise ValueError("医院缺少真实地点标识或坐标")
        if not self._healthcare_candidate(raw):
            raise ValueError("所选地点不是经过核验的医疗机构")
        destination = db.execute("SELECT * FROM trip_destination_places WHERE trip_id=?", (trip_id,)).fetchone()
        if not destination or not self._same_destination_region(raw, destination):
            raise ValueError("医院不在当前旅行目的地区域")
        details = self._normalize_emergency_details("hospital", {
            "provider": provider,
            "provider_place_id": raw["provider_place_id"],
            "name": raw.get("name"),
            "address": raw.get("formatted_address") or raw.get("address"),
            "phone": raw.get("telephone") or raw.get("phone"),
            "latitude": raw["latitude"],
            "longitude": raw["longitude"],
            "verified_at": created.isoformat(),
        }, server_hospital=True)
        return details, source_type

    def create_trip_emergency(self, trip_id: str, payload: dict, idempotency_key: object, user_id: str) -> dict:
        key = self._travel_request_key(idempotency_key)
        entry_type = str(payload.get("entry_type") or "").strip()
        if entry_type not in EMERGENCY_ENTRY_TYPES:
            raise ValueError("未知的应急卡类型")
        visibility = str(payload.get("visibility") or "private").strip()
        if visibility not in EMERGENCY_VISIBILITIES:
            raise ValueError("应急卡可见范围无效")
        expected_trip_version = _positive_int(payload.get("expected_trip_version"), "旅行版本")
        title = str(payload.get("title") or ("附近医院" if entry_type == "hospital" else "")).strip()
        if not 1 <= len(title) <= 200:
            raise ValueError("应急卡标题需为 1–200 个字符")
        request_value = {
            "trip_id": trip_id, "entry_type": entry_type, "title": title, "visibility": visibility,
            "details": payload.get("details"), "candidate_id": payload.get("candidate_id"),
            "expected_trip_version": expected_trip_version,
        }
        fingerprint = self._travel_fingerprint(request_value)
        operation = f"trip.emergency.create:{trip_id}"
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, trip, _ = self._trip_tools_context(db, trip_id, user_id)
            replay = self._travel_replay(db, str(couple["id"]), user_id, operation, key, fingerprint)
            if replay is not None:
                return replay
            if int(trip["version"]) != expected_trip_version:
                raise ConflictError("旅行已经发生变化，请刷新后重试")
            if entry_type == "hospital":
                details, source_type = self._hospital_details_from_candidate(db, trip_id, payload.get("candidate_id"), user_id)
            else:
                if "source_type" in payload:
                    raise ValueError("应急卡来源由服务器判定")
                details = self._normalize_emergency_details(entry_type, payload.get("details") or {})
                source_type = "human"
            position = int(db.execute(
                "SELECT COALESCE(MAX(position),0)+1 FROM trip_emergency_entries WHERE trip_id=? AND deleted_at IS NULL",
                (trip_id,),
            ).fetchone()[0])
            entry_id = _id("emergency")
            db.execute(
                """INSERT INTO trip_emergency_entries(
                       id,trip_id,creator_id,entry_type,title,details_json,visibility,source_type,position,
                       client_request_id,version,deleted_at,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,1,NULL,?,?)""",
                (entry_id, trip_id, user_id, entry_type, title, json.dumps(details, ensure_ascii=False), visibility,
                 source_type, position, key, now, now),
            )
            permission_version = int(couple["version"])
            if visibility == "shared":
                permission_version = self._advance_trip_permission_version(db, str(couple["id"]))
            row = db.execute("SELECT * FROM trip_emergency_entries WHERE id=?", (entry_id,)).fetchone()
            result = {**self._public_emergency_entry(row, user_id), "permission_version": permission_version}
            self._audit(db, "trip.emergency.created", "trip_emergency", entry_id, {"action": "created"}, actor_id=user_id)
            self._emit_emergency_invalidation(
                db, str(couple["id"]), user_id, trip_id, entry_id,
                shared_scope=visibility == "shared", permission_version=permission_version,
            )
            self._save_idempotent_result(db, str(couple["id"]), user_id, operation, key, {"_request_hash": fingerprint, "_response": result})
            return result

    def update_trip_emergency(self, trip_id: str, entry_id: str, payload: dict, user_id: str) -> dict:
        expected = _positive_int(payload.get("expected_version"), "应急卡版本")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _, _ = self._trip_tools_context(db, trip_id, user_id)
            row = db.execute(
                "SELECT * FROM trip_emergency_entries WHERE id=? AND trip_id=? AND deleted_at IS NULL", (entry_id, trip_id),
            ).fetchone()
            if not row or str(row["creator_id"]) != user_id:
                raise KeyError("应急卡不存在")
            if int(row["version"]) != expected:
                raise ConflictError("应急卡已被修改")
            entry_type = str(row["entry_type"])
            if entry_type == "hospital" and "details" in payload:
                raise ValueError("医院事实只能通过重新选择真实候选更新")
            if "source_type" in payload or "entry_type" in payload:
                raise ValueError("应急卡类型和来源不可直接修改")
            title = str(payload.get("title", row["title"])).strip()
            if not 1 <= len(title) <= 200:
                raise ValueError("应急卡标题需为 1–200 个字符")
            visibility = str(payload.get("visibility", row["visibility"])).strip()
            if visibility not in EMERGENCY_VISIBILITIES:
                raise ValueError("应急卡可见范围无效")
            details = _json(row["details_json"], {})
            if "details" in payload:
                details = self._normalize_emergency_details(entry_type, payload["details"])
            now = _now()
            db.execute(
                """UPDATE trip_emergency_entries SET title=?,details_json=?,visibility=?,version=version+1,updated_at=?
                   WHERE id=? AND version=?""",
                (title, json.dumps(details, ensure_ascii=False), visibility, now, entry_id, expected),
            )
            permission_version = int(couple["version"])
            if row["visibility"] == "shared" or visibility == "shared":
                permission_version = self._advance_trip_permission_version(db, str(couple["id"]))
            updated = db.execute("SELECT * FROM trip_emergency_entries WHERE id=?", (entry_id,)).fetchone()
            result = {**self._public_emergency_entry(updated, user_id), "permission_version": permission_version}
            self._audit(db, "trip.emergency.updated", "trip_emergency", entry_id, {"action": "updated"}, actor_id=user_id)
            self._emit_emergency_invalidation(
                db, str(couple["id"]), user_id, trip_id, entry_id,
                shared_scope=row["visibility"] == "shared" or visibility == "shared",
                permission_version=permission_version,
            )
            return result

    def delete_trip_emergency(self, trip_id: str, entry_id: str, expected_version: object, user_id: str) -> dict:
        expected = _positive_int(expected_version, "应急卡版本")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _, _ = self._trip_tools_context(db, trip_id, user_id)
            row = db.execute(
                "SELECT * FROM trip_emergency_entries WHERE id=? AND trip_id=? AND deleted_at IS NULL", (entry_id, trip_id),
            ).fetchone()
            if not row or str(row["creator_id"]) != user_id:
                raise KeyError("应急卡不存在")
            if int(row["version"]) != expected:
                raise ConflictError("应急卡已被修改")
            now = _now()
            db.execute("UPDATE trip_emergency_entries SET deleted_at=?,version=version+1,updated_at=? WHERE id=?", (now, now, entry_id))
            permission_version = int(couple["version"])
            if row["visibility"] == "shared":
                permission_version = self._advance_trip_permission_version(db, str(couple["id"]))
            self._audit(db, "trip.emergency.deleted", "trip_emergency", entry_id, {"action": "deleted"}, actor_id=user_id)
            self._emit_emergency_invalidation(
                db, str(couple["id"]), user_id, trip_id, entry_id,
                shared_scope=row["visibility"] == "shared", permission_version=permission_version,
            )
            return {"id": entry_id, "deleted": True, "version": expected + 1, "permission_version": permission_version}

    @staticmethod
    def _current_revision_approved(db: sqlite3.Connection, trip: sqlite3.Row, couple: sqlite3.Row) -> bool:
        members = {str(couple["member_a"]), str(couple["member_b"])}
        rows = db.execute(
            """SELECT user_id FROM approvals
               WHERE trip_id=? AND revision=? AND status='approved' AND user_id IN (?,?)""",
            (trip["id"], trip["current_revision"], couple["member_a"], couple["member_b"]),
        ).fetchall()
        return {str(row["user_id"]) for row in rows} == members

    def _confirmed_itinerary_projection(self, db: sqlite3.Connection, trip: sqlite3.Row) -> list[dict]:
        revision = db.execute(
            "SELECT diff_json FROM plan_revisions WHERE trip_id=? AND revision=?", (trip["id"], trip["current_revision"]),
        ).fetchone()
        diff = _json(revision["diff_json"], {}) if revision else {}
        selected = diff.get("selected_items") if isinstance(diff, dict) else None
        selected_ids = []
        if isinstance(selected, list):
            selected_ids = list(dict.fromkeys(
                str(item.get("id") if isinstance(item, dict) else item)
                for item in selected if str(item.get("id") if isinstance(item, dict) else item).strip()
            ))
        params: list[object] = [trip["id"]]
        selection_sql = "v.style=?"
        params.append(trip["active_variant"])
        if selected_ids:
            selection_sql = f"i.id IN ({','.join('?' for _ in selected_ids)})"
            params = [trip["id"], *selected_ids]
        rows = db.execute(
            f"""SELECT i.id,i.type,i.title,i.start_time,i.duration_minutes,i.note,i.position,
                       d.day_number,d.date,d.title AS day_title,d.position AS day_position,
                       p.provider,p.name AS place_name,p.formatted_address,p.latitude,p.longitude
                FROM itinerary_items i JOIN trip_days d ON d.id=i.day_id
                JOIN plan_variants v ON v.id=d.variant_id
                LEFT JOIN itinerary_item_places p ON p.itinerary_item_id=i.id
                WHERE v.trip_id=? AND {selection_sql}
                ORDER BY d.position,i.position,i.id LIMIT 101""",
            params,
        ).fetchall()
        if len(rows) > 100:
            raise ValueError("已确认行程项目过多，无法生成离线副本")
        days: dict[tuple[int, str], dict] = {}
        for row in rows:
            key = (int(row["day_number"]), str(row["date"]))
            day_value = days.setdefault(key, {
                "day_number": key[0], "date": key[1], "title": str(row["day_title"]), "items": [],
            })
            place = None
            if row["place_name"]:
                place = {
                    "name": str(row["place_name"]), "address": str(row["formatted_address"] or ""),
                    "latitude": float(row["latitude"]), "longitude": float(row["longitude"]),
                    "provider": str(row["provider"]),
                }
            day_value["items"].append({
                "id": str(row["id"]), "type": str(row["type"]), "title": str(row["title"]),
                "start_time": str(row["start_time"]), "duration_minutes": int(row["duration_minutes"]),
                "note": str(row["note"] or "")[:500], "place": place,
            })
        return list(days.values())

    def get_trip_offline_snapshot(self, trip_id: str, user_id: str) -> dict:
        generated = datetime.now(timezone.utc).replace(microsecond=0)
        with self.connection() as db:
            couple, trip, _ = self._trip_tools_context(db, trip_id, user_id)
            if not self._current_revision_approved(db, trip, couple):
                raise ValueError("当前旅行版本尚未由双方确认，不能生成离线副本")
            members = [
                {"id": str(row["id"]), "nickname": str(row["nickname"]), "avatar_label": str(row["avatar_label"])}
                for row in db.execute(
                    "SELECT id,nickname,avatar_label FROM users WHERE id IN (?,?) AND deleted_at IS NULL ORDER BY id",
                    (couple["member_a"], couple["member_b"]),
                ).fetchall()
            ]
            emergency_rows = db.execute(
                """SELECT * FROM trip_emergency_entries WHERE trip_id=? AND deleted_at IS NULL
                   AND (creator_id=? OR visibility='shared') ORDER BY position,id""",
                (trip_id, user_id),
            ).fetchall()
            return {
                "schema_version": 1,
                "identity": {
                    "user_id": user_id, "couple_id": str(couple["id"]),
                    "permission_version": int(couple["version"]),
                },
                "trip": {
                    "id": trip_id, "title": str(trip["title"]), "destination": str(trip["destination"]),
                    "start_date": str(trip["start_date"]), "end_date": str(trip["end_date"]),
                    "currency": str(trip["currency"]), "confirmed_revision": int(trip["current_revision"]),
                    "version": int(trip["version"]),
                },
                "members": members,
                "itinerary": self._confirmed_itinerary_projection(db, trip),
                "packing": {"items": self._list_trip_packing_db(db, trip_id, couple)},
                "emergency": {"items": [self._public_emergency_entry(row, user_id) for row in emergency_rows]},
                "generated_at": generated.isoformat(),
                "expires_at": (generated + timedelta(hours=24)).isoformat(),
            }

    # Travel expenses intentionally stay online-only and deterministic.  They
    # do not enter the itinerary revision, offline snapshot or AI context.
    @staticmethod
    def _expense_member_names(db: sqlite3.Connection, couple: sqlite3.Row) -> dict[str, str]:
        member_ids = (str(couple["member_a"]), str(couple["member_b"]))
        rows = db.execute(
            "SELECT id,nickname FROM users WHERE id IN (?,?) AND deleted_at IS NULL",
            member_ids,
        ).fetchall()
        names = {str(row["id"]): str(row["nickname"]) for row in rows}
        if set(names) != set(member_ids):
            raise ValueError("当前情侣成员信息不完整")
        return names

    @staticmethod
    def _checked_minor_add(total: int, amount: int) -> int:
        result = total + amount
        if result > MAX_SAFE_MINOR:
            raise ValueError("旅行有效花费总额超过安全上限")
        return result

    @classmethod
    def _active_expense_total(
        cls, db: sqlite3.Connection, trip_id: str, *, exclude_id: str | None = None,
    ) -> int:
        params: list[object] = [trip_id]
        exclude_sql = ""
        if exclude_id:
            exclude_sql = " AND id<>?"
            params.append(exclude_id)
        rows = db.execute(
            "SELECT amount_minor FROM trip_expenses WHERE trip_id=? AND deleted_at IS NULL" + exclude_sql,
            params,
        ).fetchall()
        total = 0
        for row in rows:
            amount = row["amount_minor"]
            if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= MAX_SAFE_MINOR:
                raise ValueError("旅行花费数据需要修复后才能继续")
            total = cls._checked_minor_add(total, amount)
        return total

    @staticmethod
    def _expense_transport_fields(payload: dict) -> dict:
        return {key: value for key, value in payload.items() if key not in {"idempotency_key", "client_request_id"}}

    @classmethod
    def _normalize_expense_input(
        cls,
        payload: dict,
        *,
        member_ids: set[str],
        trip_currency: str,
        base: sqlite3.Row | dict | None = None,
    ) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("旅行花费必须是对象")
        business = cls._expense_transport_fields(payload)
        allowed = {
            "amount_cents", "payer_id", "currency", "category", "split_mode",
            "beneficiary_id", "note", "occurred_on",
        }
        unknown = set(business) - allowed
        if unknown:
            raise ValueError(f"旅行花费包含不支持字段：{sorted(unknown)[0]}")
        source = dict(base or {})

        amount = business.get("amount_cents", source.get("amount_minor"))
        if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= MAX_SAFE_MINOR:
            raise ValueError("花费金额必须是安全范围内的整数分")

        payer_id = business.get("payer_id", source.get("payer_id"))
        if not isinstance(payer_id, str) or payer_id not in member_ids:
            raise ValueError("付款人必须是当前情侣成员")

        currency = business.get("currency", source.get("currency", trip_currency))
        if not isinstance(currency, str) or currency != trip_currency:
            raise ValueError("旅行花费币种必须与旅行一致，不支持自动换汇")

        category = business.get("category", source.get("category"))
        if not isinstance(category, str) or category not in EXPENSE_CATEGORIES:
            raise ValueError("旅行花费分类无效")

        split_mode = business.get("split_mode", source.get("split_mode"))
        if not isinstance(split_mode, str) or split_mode not in EXPENSE_SPLIT_MODES:
            raise ValueError("旅行花费分摊方式无效")
        beneficiary_id = business.get("beneficiary_id", source.get("beneficiary_id"))
        if split_mode == "equal":
            # Changing a previously personal row to equal intentionally clears
            # its stored beneficiary.  A caller-supplied beneficiary remains a
            # contradictory request and is rejected rather than ignored.
            if "beneficiary_id" in business and beneficiary_id not in {None, ""}:
                raise ValueError("平均分摊不能指定个人承担者")
            beneficiary_id = None
        elif not isinstance(beneficiary_id, str) or beneficiary_id not in member_ids:
            raise ValueError("个人承担必须选择当前情侣成员")

        note = business.get("note", source.get("note", ""))
        if not isinstance(note, str) or len(note) > 500:
            raise ValueError("花费备注最多 500 个字符")
        note = note.strip()

        occurred_on = business.get("occurred_on", source.get("occurred_on"))
        if not isinstance(occurred_on, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", occurred_on):
            raise ValueError("花费日期必须是 YYYY-MM-DD")
        try:
            date.fromisoformat(occurred_on)
        except ValueError as exc:
            raise ValueError("花费日期无效") from exc
        return {
            "amount_cents": amount,
            "payer_id": payer_id,
            "currency": currency,
            "category": category,
            "split_mode": split_mode,
            "beneficiary_id": beneficiary_id,
            "note": note,
            "occurred_on": occurred_on,
        }

    @staticmethod
    def _expense_mutation_shape(payload: dict, allowed: set[str]) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("旅行花费操作必须是对象")
        value = {key: item for key, item in payload.items() if key not in {"idempotency_key", "client_request_id"}}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"旅行花费操作包含不支持字段：{sorted(unknown)[0]}")
        return value

    @staticmethod
    def _public_trip_expense(row: sqlite3.Row | dict, names: dict[str, str]) -> dict:
        value = dict(row)
        payer_id = str(value["payer_id"])
        beneficiary_id = str(value["beneficiary_id"]) if value.get("beneficiary_id") else None
        settled_by = str(value["settled_by"]) if value.get("settled_by") else None
        return {
            "id": str(value["id"]),
            "trip_id": str(value["trip_id"]),
            "amount_cents": int(value["amount_minor"]),
            "currency": str(value["currency"]),
            "category": str(value["category"]),
            "split_mode": str(value["split_mode"]),
            "payer_id": payer_id,
            "payer_name": names.get(payer_id, "已注销用户"),
            "beneficiary_id": beneficiary_id,
            "beneficiary_name": names.get(beneficiary_id, "已注销用户") if beneficiary_id else None,
            "note": str(value["note"]),
            "occurred_on": str(value["occurred_on"]),
            "settled_at": str(value["settled_at"]) if value.get("settled_at") else None,
            "settled_by_name": names.get(settled_by, "已注销用户") if settled_by else None,
            "source_type": "human",
            "editable": value.get("deleted_at") is None,
            "deleted": value.get("deleted_at") is not None,
            "deleted_at": str(value["deleted_at"]) if value.get("deleted_at") else None,
            "version": int(value["version"]),
            "created_at": str(value["created_at"]),
            "updated_at": str(value["updated_at"]),
        }

    @classmethod
    def _trip_expense_balance(
        cls,
        rows: list[sqlite3.Row],
        couple: sqlite3.Row,
        names: dict[str, str],
        user_id: str,
    ) -> dict:
        member_a, member_b = str(couple["member_a"]), str(couple["member_b"])
        paid = {member_a: 0, member_b: 0}
        owed = {member_a: 0, member_b: 0}
        total_active = 0
        total_unsettled = 0
        unsettled_count = 0
        for row in rows:
            amount = int(row["amount_minor"])
            total_active = cls._checked_minor_add(total_active, amount)
            if row["settled_at"] is not None:
                continue
            total_unsettled = cls._checked_minor_add(total_unsettled, amount)
            unsettled_count += 1
            payer_id = str(row["payer_id"])
            if payer_id not in paid:
                raise ValueError("旅行花费付款人不再属于当前情侣")
            paid[payer_id] = cls._checked_minor_add(paid[payer_id], amount)
            if row["split_mode"] == "equal":
                half, remainder = divmod(amount, 2)
                owed[member_a] = cls._checked_minor_add(owed[member_a], half + remainder)
                owed[member_b] = cls._checked_minor_add(owed[member_b], half)
            else:
                beneficiary = str(row["beneficiary_id"])
                if beneficiary not in owed:
                    raise ValueError("旅行花费承担人不再属于当前情侣")
                owed[beneficiary] = cls._checked_minor_add(owed[beneficiary], amount)
        by_member = [
            {
                "user_id": member_id,
                "nickname": names[member_id],
                "paid_cents": paid[member_id],
                "owed_cents": owed[member_id],
                "net_cents": paid[member_id] - owed[member_id],
            }
            for member_id in (member_a, member_b)
        ]
        current = next(item for item in by_member if item["user_id"] == user_id)
        partner_id = member_b if user_id == member_a else member_a
        net = int(current["net_cents"])
        return {
            "current_user_id": user_id,
            "net_cents": net,
            "direction": "partner_owes_me" if net > 0 else "i_owe_partner" if net < 0 else "balanced",
            "partner_name": names[partner_id],
            "unsettled_count": unsettled_count,
            "total_active_cents": total_active,
            "total_unsettled_cents": total_unsettled,
            "by_member": by_member,
        }

    @staticmethod
    def _expense_safe_channel_payload(action: str, trip_id: str) -> dict:
        return {"resource": "trip_expense", "action": action, "trip_id": trip_id}

    def _expense_list_db(
        self, db: sqlite3.Connection, couple: sqlite3.Row, trip: sqlite3.Row, user_id: str,
    ) -> dict:
        names = self._expense_member_names(db, couple)
        active_rows = db.execute(
            "SELECT * FROM trip_expenses WHERE trip_id=? AND deleted_at IS NULL "
            "ORDER BY occurred_on DESC,created_at DESC,id",
            (trip["id"],),
        ).fetchall()
        deleted_rows = db.execute(
            "SELECT * FROM trip_expenses WHERE trip_id=? AND deleted_at IS NOT NULL "
            "ORDER BY deleted_at DESC,id LIMIT 20",
            (trip["id"],),
        ).fetchall()
        member_ids = (str(couple["member_a"]), str(couple["member_b"]))
        return {
            "trip": {"id": str(trip["id"]), "title": str(trip["title"]), "currency": str(trip["currency"])},
            "members": [
                {"id": member_id, "nickname": names[member_id], "avatar_label": names[member_id][:1]}
                for member_id in member_ids
            ],
            "items": [self._public_trip_expense(row, names) for row in active_rows],
            "deleted_items": [self._public_trip_expense(row, names) for row in deleted_rows],
            "balance": self._trip_expense_balance(active_rows, couple, names, user_id),
        }

    def list_trip_expenses(self, trip_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, trip, _ = self._trip_tools_context(db, trip_id, user_id)
            return self._expense_list_db(db, couple, trip, user_id)

    def create_trip_expense(
        self, trip_id: str, payload: dict, idempotency_key: object, user_id: str,
    ) -> dict:
        key = self._travel_request_key(idempotency_key)
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, trip, _ = self._trip_tools_context(db, trip_id, user_id)
            names = self._expense_member_names(db, couple)
            normalized = self._normalize_expense_input(
                payload, member_ids=set(names), trip_currency=str(trip["currency"]),
            )
            fingerprint = self._travel_fingerprint({"trip_id": trip_id, **normalized})
            replay = self._travel_replay(db, str(couple["id"]), user_id, "trip.expense.create", key, fingerprint)
            if replay is not None:
                return replay
            self._checked_minor_add(self._active_expense_total(db, trip_id), normalized["amount_cents"])
            expense_id = _id("expense")
            db.execute(
                """INSERT INTO trip_expenses(
                       id,trip_id,creator_id,payer_id,amount_minor,currency,category,split_mode,
                       beneficiary_id,note,occurred_on,settled_at,settled_by,client_request_id,
                       version,deleted_at,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,1,NULL,?,?)""",
                (
                    expense_id, trip_id, user_id, normalized["payer_id"], normalized["amount_cents"],
                    normalized["currency"], normalized["category"], normalized["split_mode"],
                    normalized["beneficiary_id"], normalized["note"], normalized["occurred_on"],
                    key, now, now,
                ),
            )
            row = db.execute("SELECT * FROM trip_expenses WHERE id=?", (expense_id,)).fetchone()
            result = self._public_trip_expense(row, names)
            self._audit(
                db, "trip.expense_created", "trip_expense", expense_id,
                {"category": normalized["category"], "version": 1}, actor_id=user_id,
            )
            self._event(
                db, str(couple["id"]), user_id, "trip.tools_updated", trip_id,
                self._expense_safe_channel_payload("created", trip_id),
            )
            self._save_idempotent_result(
                db, str(couple["id"]), user_id, "trip.expense.create", key,
                {"_request_hash": fingerprint, "_response": result},
            )
            return result

    def update_trip_expense(
        self, trip_id: str, expense_id: str, payload: dict,
        idempotency_key: object, user_id: str,
    ) -> dict:
        key = self._travel_request_key(idempotency_key)
        business = self._expense_mutation_shape(
            payload,
            {
                "amount_cents", "payer_id", "currency", "category", "split_mode",
                "beneficiary_id", "note", "occurred_on", "expected_version", "unset_settlement",
            },
        )
        expected = _positive_int(business.get("expected_version"), "花费版本")
        unset_settlement = business.get("unset_settlement", False)
        if not isinstance(unset_settlement, bool):
            raise ValueError("重新计入未结清状态必须是布尔值")
        update_values = {key: value for key, value in business.items() if key not in {"expected_version", "unset_settlement"}}
        if not update_values:
            raise ValueError("请至少修改一项花费内容")
        fingerprint = self._travel_fingerprint({
            "trip_id": trip_id, "expense_id": expense_id, "expected_version": expected,
            "unset_settlement": unset_settlement, "changes": update_values,
        })
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, trip, _ = self._trip_tools_context(db, trip_id, user_id)
            replay = self._travel_replay(db, str(couple["id"]), user_id, "trip.expense.update", key, fingerprint)
            if replay is not None:
                return replay
            row = db.execute(
                "SELECT * FROM trip_expenses WHERE id=? AND trip_id=? AND deleted_at IS NULL",
                (expense_id, trip_id),
            ).fetchone()
            if not row:
                raise KeyError("旅行花费不存在")
            if int(row["version"]) != expected:
                raise ConflictError("旅行花费已经被另一方修改")
            if row["settled_at"] is not None and not unset_settlement:
                raise ConflictError("已线下结清的花费必须明确重新计入后才能编辑")
            names = self._expense_member_names(db, couple)
            normalized = self._normalize_expense_input(
                update_values, member_ids=set(names), trip_currency=str(trip["currency"]), base=row,
            )
            self._checked_minor_add(
                self._active_expense_total(db, trip_id, exclude_id=expense_id), normalized["amount_cents"],
            )
            settled_at = None if unset_settlement else row["settled_at"]
            settled_by = None if unset_settlement else row["settled_by"]
            cursor = db.execute(
                """UPDATE trip_expenses SET payer_id=?,amount_minor=?,currency=?,category=?,split_mode=?,
                          beneficiary_id=?,note=?,occurred_on=?,settled_at=?,settled_by=?,version=version+1,updated_at=?
                   WHERE id=? AND trip_id=? AND deleted_at IS NULL AND version=?""",
                (
                    normalized["payer_id"], normalized["amount_cents"], normalized["currency"],
                    normalized["category"], normalized["split_mode"], normalized["beneficiary_id"],
                    normalized["note"], normalized["occurred_on"], settled_at, settled_by,
                    now, expense_id, trip_id, expected,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("旅行花费已经被另一方修改")
            updated = db.execute("SELECT * FROM trip_expenses WHERE id=?", (expense_id,)).fetchone()
            result = self._public_trip_expense(updated, names)
            self._audit(
                db, "trip.expense_updated", "trip_expense", expense_id,
                {"category": normalized["category"], "version": result["version"]}, actor_id=user_id,
            )
            self._event(
                db, str(couple["id"]), user_id, "trip.tools_updated", trip_id,
                self._expense_safe_channel_payload("updated", trip_id),
            )
            self._save_idempotent_result(
                db, str(couple["id"]), user_id, "trip.expense.update", key,
                {"_request_hash": fingerprint, "_response": result},
            )
            return result

    def delete_trip_expense(
        self, trip_id: str, expense_id: str, payload: dict,
        idempotency_key: object, user_id: str,
    ) -> dict:
        key = self._travel_request_key(idempotency_key)
        business = self._expense_mutation_shape(payload, {"expected_version"})
        expected = _positive_int(business.get("expected_version"), "花费版本")
        fingerprint = self._travel_fingerprint({
            "trip_id": trip_id, "expense_id": expense_id, "expected_version": expected,
        })
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _, _ = self._trip_tools_context(db, trip_id, user_id)
            replay = self._travel_replay(db, str(couple["id"]), user_id, "trip.expense.delete", key, fingerprint)
            if replay is not None:
                return replay
            row = db.execute(
                "SELECT * FROM trip_expenses WHERE id=? AND trip_id=? AND deleted_at IS NULL",
                (expense_id, trip_id),
            ).fetchone()
            if not row:
                raise KeyError("旅行花费不存在或已经删除")
            if int(row["version"]) != expected:
                raise ConflictError("旅行花费已经被另一方修改")
            cursor = db.execute(
                "UPDATE trip_expenses SET deleted_at=?,version=version+1,updated_at=? "
                "WHERE id=? AND trip_id=? AND deleted_at IS NULL AND version=?",
                (now, now, expense_id, trip_id, expected),
            )
            if cursor.rowcount != 1:
                raise ConflictError("旅行花费已经被另一方修改")
            updated = db.execute("SELECT * FROM trip_expenses WHERE id=?", (expense_id,)).fetchone()
            names = self._expense_member_names(db, couple)
            result = self._public_trip_expense(updated, names)
            self._audit(
                db, "trip.expense_deleted", "trip_expense", expense_id,
                {"category": str(row["category"]), "version": result["version"]}, actor_id=user_id,
            )
            self._event(
                db, str(couple["id"]), user_id, "trip.tools_updated", trip_id,
                self._expense_safe_channel_payload("deleted", trip_id),
            )
            self._save_idempotent_result(
                db, str(couple["id"]), user_id, "trip.expense.delete", key,
                {"_request_hash": fingerprint, "_response": result},
            )
            return result

    def restore_trip_expense(
        self, trip_id: str, expense_id: str, payload: dict,
        idempotency_key: object, user_id: str,
    ) -> dict:
        key = self._travel_request_key(idempotency_key)
        business = self._expense_mutation_shape(payload, {"expected_version"})
        expected = _positive_int(business.get("expected_version"), "花费版本")
        fingerprint = self._travel_fingerprint({
            "trip_id": trip_id, "expense_id": expense_id, "expected_version": expected,
        })
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _, _ = self._trip_tools_context(db, trip_id, user_id)
            replay = self._travel_replay(db, str(couple["id"]), user_id, "trip.expense.restore", key, fingerprint)
            if replay is not None:
                return replay
            row = db.execute(
                "SELECT * FROM trip_expenses WHERE id=? AND trip_id=? AND deleted_at IS NOT NULL",
                (expense_id, trip_id),
            ).fetchone()
            if not row:
                raise KeyError("已删除的旅行花费不存在")
            if int(row["version"]) != expected:
                raise ConflictError("旅行花费删除状态已经变化")
            self._checked_minor_add(self._active_expense_total(db, trip_id), int(row["amount_minor"]))
            cursor = db.execute(
                "UPDATE trip_expenses SET deleted_at=NULL,version=version+1,updated_at=? "
                "WHERE id=? AND trip_id=? AND deleted_at IS NOT NULL AND version=?",
                (now, expense_id, trip_id, expected),
            )
            if cursor.rowcount != 1:
                raise ConflictError("旅行花费删除状态已经变化")
            updated = db.execute("SELECT * FROM trip_expenses WHERE id=?", (expense_id,)).fetchone()
            names = self._expense_member_names(db, couple)
            result = self._public_trip_expense(updated, names)
            self._audit(
                db, "trip.expense_restored", "trip_expense", expense_id,
                {"category": str(row["category"]), "version": result["version"]}, actor_id=user_id,
            )
            self._event(
                db, str(couple["id"]), user_id, "trip.tools_updated", trip_id,
                self._expense_safe_channel_payload("restored", trip_id),
            )
            self._save_idempotent_result(
                db, str(couple["id"]), user_id, "trip.expense.restore", key,
                {"_request_hash": fingerprint, "_response": result},
            )
            return result

    def settle_trip_expenses(
        self, trip_id: str, payload: dict, idempotency_key: object, user_id: str,
    ) -> dict:
        key = self._travel_request_key(idempotency_key)
        business = self._expense_mutation_shape(payload, {"expenses"})
        raw_items = business.get("expenses")
        if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 200:
            raise ValueError("请选择 1–200 条待线下结清的花费")
        requested: list[dict[str, object]] = []
        seen: set[str] = set()
        for item in raw_items:
            if not isinstance(item, dict) or set(item) != {"id", "expected_version"}:
                raise ValueError("结清清单格式无效")
            expense_id = item.get("id")
            expected = item.get("expected_version")
            if not isinstance(expense_id, str) or not expense_id.strip() or expense_id in seen:
                raise ValueError("结清清单包含重复或无效花费")
            seen.add(expense_id)
            requested.append({"id": expense_id, "expected_version": _positive_int(expected, "花费版本")})
        requested.sort(key=lambda item: str(item["id"]))
        fingerprint = self._travel_fingerprint({"trip_id": trip_id, "expenses": requested})
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _, _ = self._trip_tools_context(db, trip_id, user_id)
            replay = self._travel_replay(db, str(couple["id"]), user_id, "trip.expense.settle", key, fingerprint)
            if replay is not None:
                return replay
            rows: list[sqlite3.Row] = []
            for item in requested:
                row = db.execute(
                    "SELECT * FROM trip_expenses WHERE id=? AND trip_id=?",
                    (item["id"], trip_id),
                ).fetchone()
                if (
                    not row or row["deleted_at"] is not None or row["settled_at"] is not None
                    or int(row["version"]) != int(item["expected_version"])
                ):
                    raise ConflictError("待结清花费清单已经变化，请刷新后重试")
                rows.append(row)
            for row in rows:
                cursor = db.execute(
                    "UPDATE trip_expenses SET settled_at=?,settled_by=?,version=version+1,updated_at=? "
                    "WHERE id=? AND trip_id=? AND deleted_at IS NULL AND settled_at IS NULL AND version=?",
                    (now, user_id, now, row["id"], trip_id, row["version"]),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("待结清花费清单已经变化，请刷新后重试")
            names = self._expense_member_names(db, couple)
            updated = [
                self._public_trip_expense(
                    db.execute("SELECT * FROM trip_expenses WHERE id=?", (row["id"],)).fetchone(), names,
                )
                for row in rows
            ]
            result = {
                "trip_id": trip_id,
                "settled_count": len(updated),
                "settled_at": now,
                "settled_by_name": names[user_id],
                "items": updated,
                "notice": "已记录为线下结清；恋屿不会发起付款或转账",
            }
            self._audit(
                db, "trip.expenses_settled", "trip", trip_id,
                {"count": len(updated), "version": max(item["version"] for item in updated)}, actor_id=user_id,
            )
            self._event(
                db, str(couple["id"]), user_id, "trip.tools_updated", trip_id,
                self._expense_safe_channel_payload("settled", trip_id),
            )
            self._save_idempotent_result(
                db, str(couple["id"]), user_id, "trip.expense.settle", key,
                {"_request_hash": fingerprint, "_response": result},
            )
            return result
