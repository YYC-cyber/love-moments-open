"""Private two-person decision state machine.

Candidate options are shared so both partners can vote.  Submitted choices stay
inside this module until both current members have submitted in the same round.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .intelligent_ui import ConflictError


DECISION_STATUSES = frozenset({
    "open", "awaiting_extension", "revealed", "pending_confirmation",
    "confirmed", "cancelled", "converted",
})


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _parse_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label}必须是有效的 ISO 日期时间") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


class CoupleInteractionMixin:
    _DECISION_CONVERSION_TARGETS = frozenset({
        "date_plan", "meal_wish", "trip", "hotel_search",
        "restaurant_search", "calendar_todo",
    })

    @staticmethod
    def _claim_key(value: object) -> str:
        key = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9:._-]{8,160}", key):
            raise ValueError("幂等键格式无效")
        return key

    @staticmethod
    def _claim_fingerprint(value: dict) -> str:
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _claim_replay(
        self, db: sqlite3.Connection, couple_id: str, user_id: str,
        operation: str, key: str, fingerprint: str,
    ) -> dict | None:
        replay = self._idempotent_result(db, couple_id, user_id, operation, key)
        if replay is None:
            return None
        if replay.get("_request_hash") != fingerprint:
            raise ConflictError("幂等标识已绑定到另一项心愿操作")
        claim_id = str(replay.get("_claim_id") or replay.get("id") or "")
        claim = db.execute(
            """SELECT c.* FROM wishlist_claims c
            JOIN shared_list_items i ON i.id=c.item_id
            JOIN shared_lists l ON l.id=i.list_id
            WHERE c.id=? AND c.claimant_id=? AND l.couple_id=?""",
            (claim_id, user_id, couple_id),
        ).fetchone()
        if not claim:
            raise PermissionError("认领回执已经失效")
        saved = replay.get("_response")
        if isinstance(saved, dict):
            result = dict(saved)
            if operation == "wishlist.claim":
                result["private_note"] = str(claim["private_note"] or "")
            return result
        # Safe compatibility for receipts written before snapshots were added.
        if operation == "wishlist.claim.complete":
            return {
                "id": claim_id, "item_id": str(claim["item_id"]),
                "completed": str(claim["status"]) == "completed",
                "revealed": bool(claim["reveal_claimant"]),
                "moment_id": replay.get("moment_id"),
            }
        if operation == "wishlist.claim":
            return self._claim_private(claim, include_note=True)
        return self._claim_private(claim, include_note=False)

    @staticmethod
    def _claim_receipt(claim_id: str, fingerprint: str, response: dict) -> dict:
        return {
            "_request_hash": fingerprint,
            "_claim_id": claim_id,
            "_response": response,
        }

    @staticmethod
    def _claim_private(row: sqlite3.Row | dict, *, include_note: bool = True) -> dict:
        value = {
            "id": str(row["id"]), "item_id": str(row["item_id"]),
            "status": str(row["status"]),
            "version": int(row["version"]), "reveal_claimant": bool(row["reveal_claimant"]),
            "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"]),
            "ended_at": row["ended_at"],
        }
        if include_note:
            value["private_note"] = str(row["private_note"] or "")
        return value

    def claim_wishlist_item(
        self, item_id: str, private_note: object, reveal_claimant: object,
        expected_item_version: object, idempotency_key: object, user_id: str,
    ) -> dict:
        key = self._claim_key(idempotency_key)
        if isinstance(expected_item_version, bool) or not isinstance(expected_item_version, int) or expected_item_version < 1:
            raise ValueError("心愿版本必须是正整数")
        if type(reveal_claimant) is not bool:
            raise ValueError("公开认领人必须是布尔值")
        if not isinstance(private_note, str) or len(private_note.strip()) > 1000:
            raise ValueError("私密备注不能超过 1000 个字符")
        note = private_note.strip()
        fingerprint = self._claim_fingerprint({
            "item_id": item_id, "private_note": note, "reveal_claimant": reveal_claimant,
            "expected_item_version": expected_item_version,
        })
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._claim_replay(db, str(couple["id"]), user_id, "wishlist.claim", key, fingerprint)
            if replay is not None:
                return replay
            item = db.execute(
                """SELECT i.* FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id
                WHERE i.id=? AND l.couple_id=? AND i.deleted_at IS NULL""",
                (item_id, couple["id"]),
            ).fetchone()
            if not item:
                raise KeyError("心愿不存在")
            if item["creator_id"] == user_id:
                raise PermissionError("不能认领自己创建的心愿")
            if item["item_kind"] != "gift" or item["completed_at"]:
                raise ValueError("只有未完成的礼物心愿可以认领")
            if int(item["version"]) != expected_item_version:
                raise ConflictError("心愿已发生变化，请刷新")
            if db.execute("SELECT 1 FROM wishlist_claims WHERE item_id=? AND status='active'", (item_id,)).fetchone():
                raise ConflictError("这份礼物心愿已经有人准备")
            claim_id = _id("claim")
            try:
                db.execute(
                    """INSERT INTO wishlist_claims(
                        id,item_id,claimant_id,status,private_note,reveal_claimant,
                        item_version_at_claim,client_request_id,version,created_at,updated_at
                    ) VALUES (?,?,?,'active',?,?,?,?,1,?,?)""",
                    (claim_id, item_id, user_id, note, int(reveal_claimant), int(item["version"]), key, now, now),
                )
            except sqlite3.IntegrityError as error:
                raise ConflictError("这份礼物心愿已经有人准备") from error
            claim = db.execute("SELECT * FROM wishlist_claims WHERE id=?", (claim_id,)).fetchone()
            safe_result = self._claim_private(claim, include_note=False)
            result = {**safe_result, "private_note": str(claim["private_note"] or "")}
            self._save_idempotent_result(
                db, str(couple["id"]), user_id, "wishlist.claim", key,
                self._claim_receipt(str(claim["id"]), fingerprint, safe_result),
            )
            return result

    def release_wishlist_claim(
        self, item_id: str, expected_claim_version: object,
        idempotency_key: object, user_id: str,
    ) -> dict:
        key = self._claim_key(idempotency_key)
        if isinstance(expected_claim_version, bool) or not isinstance(expected_claim_version, int) or expected_claim_version < 1:
            raise ValueError("认领版本必须是正整数")
        fingerprint = self._claim_fingerprint({"item_id": item_id, "version": expected_claim_version})
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._claim_replay(db, str(couple["id"]), user_id, "wishlist.claim.release", key, fingerprint)
            if replay is not None:
                return replay
            claim = db.execute(
                """SELECT c.* FROM wishlist_claims c JOIN shared_list_items i ON i.id=c.item_id
                JOIN shared_lists l ON l.id=i.list_id
                WHERE c.item_id=? AND c.claimant_id=? AND c.status='active'
                  AND l.couple_id=? AND i.deleted_at IS NULL""",
                (item_id, user_id, couple["id"]),
            ).fetchone()
            if not claim:
                raise KeyError("有效认领不存在")
            if int(claim["version"]) != expected_claim_version:
                raise ConflictError("认领状态已发生变化，请刷新")
            db.execute(
                "UPDATE wishlist_claims SET status='released',version=version+1,updated_at=?,ended_at=? WHERE id=?",
                (now, now, claim["id"]),
            )
            updated = db.execute("SELECT * FROM wishlist_claims WHERE id=?", (claim["id"],)).fetchone()
            result = self._claim_private(updated, include_note=False)
            self._save_idempotent_result(
                db, str(couple["id"]), user_id, "wishlist.claim.release", key,
                self._claim_receipt(str(updated["id"]), fingerprint, result),
            )
            return result

    def complete_wishlist_claim(
        self, item_id: str, expected_claim_version: object, reveal_claimant: object,
        create_moment: object, idempotency_key: object, user_id: str,
    ) -> dict:
        key = self._claim_key(idempotency_key)
        if isinstance(expected_claim_version, bool) or not isinstance(expected_claim_version, int) or expected_claim_version < 1:
            raise ValueError("认领版本必须是正整数")
        if type(reveal_claimant) is not bool or type(create_moment) is not bool:
            raise ValueError("完成选项必须是布尔值")
        if create_moment and not reveal_claimant:
            raise ValueError("生成共同瞬间时需要公开认领人")
        fingerprint = self._claim_fingerprint({
            "item_id": item_id, "version": expected_claim_version,
            "reveal_claimant": reveal_claimant, "create_moment": create_moment,
        })
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._claim_replay(db, str(couple["id"]), user_id, "wishlist.claim.complete", key, fingerprint)
            if replay is not None:
                return replay
            row = db.execute(
                """SELECT c.*,i.creator_id,i.text,i.note,i.completed_at,i.deleted_at,i.version AS item_version
                FROM wishlist_claims c JOIN shared_list_items i ON i.id=c.item_id
                JOIN shared_lists l ON l.id=i.list_id
                WHERE c.item_id=? AND c.claimant_id=? AND c.status='active' AND l.couple_id=?""",
                (item_id, user_id, couple["id"]),
            ).fetchone()
            if not row or row["deleted_at"] or row["completed_at"]:
                raise KeyError("有效认领不存在")
            if int(row["version"]) != expected_claim_version:
                raise ConflictError("认领状态已发生变化，请刷新")
            db.execute(
                """UPDATE wishlist_claims SET status='completed',reveal_claimant=?,version=version+1,
                updated_at=?,ended_at=? WHERE id=?""",
                (int(reveal_claimant), now, now, row["id"]),
            )
            db.execute(
                "UPDATE shared_list_items SET completed_at=?,version=version+1,updated_at=? WHERE id=?",
                (now, now, item_id),
            )
            moment_id = None
            if create_moment:
                moment_id = _id("moment")
                db.execute(
                    """INSERT INTO moments(
                        id,couple_id,creator_id,type,title,body,occurred_at,visibility
                    ) VALUES (?,?,?,'wishlist',?,?,?,'shared')""",
                    (moment_id, couple["id"], user_id, str(row["text"])[:200], str(row["note"] or "")[:5000], now),
                )
                self._audit(db, "moment.created", "moment", moment_id, {"visibility": "shared"}, actor_id=user_id)
                self._event(db, str(couple["id"]), user_id, "moment.created", moment_id, {"type": "wishlist", "visibility": "shared"})
            public_actor = user_id if reveal_claimant else str(row["creator_id"])
            self._audit(db, "wishlist.toggled", "shared_list_item", item_id, {"completed": True}, actor_id=public_actor)
            self._event(db, str(couple["id"]), public_actor, "life.updated", item_id, {"resource": "wishlist", "completed": True})
            if reveal_claimant:
                self._insert_in_app_notification(
                    db, couple_id=str(couple["id"]), recipient_id=str(row["creator_id"]), actor_id=user_id,
                    event_type="wishlist.gift_completed", resource_type="shared_list_item", resource_id=item_id,
                    title="礼物心愿已经完成", body="对方已经实现了这份礼物心愿，点击查看。",
                    url=f"/?screen=life&shared_type=wishlist_item&shared_id={item_id}",
                    dedupe_key=f"wishlist-gift-completed:{item_id}",
                )
            completed_claim = db.execute(
                "SELECT * FROM wishlist_claims WHERE id=?", (row["id"],),
            ).fetchone()
            result = {
                **self._claim_private(completed_claim, include_note=False),
                "completed": True, "revealed": reveal_claimant, "moment_id": moment_id,
            }
            self._save_idempotent_result(
                db, str(couple["id"]), user_id, "wishlist.claim.complete", key,
                self._claim_receipt(str(row["id"]), fingerprint, result),
            )
            return result
    @staticmethod
    def _expected_decision_version(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("expected_version 必须是正整数")
        return value

    @staticmethod
    def _decision_request_fingerprint(value: dict) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_decision_options(value: object) -> list[str]:
        if not isinstance(value, list) or not 2 <= len(value) <= 6:
            raise ValueError("决策选项需要 2–6 个")
        labels: list[str] = []
        for raw in value:
            if not isinstance(raw, str):
                raise ValueError("决策选项必须是文字")
            label = raw.strip()
            if not label or len(label) > 300:
                raise ValueError("决策选项长度需为 1–300 个字符")
            labels.append(label)
        if len({label.casefold() for label in labels}) != len(labels):
            raise ValueError("决策选项不能重复")
        return labels

    @staticmethod
    def _validate_decision_title(value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("请填写决策标题")
        title = value.strip()
        if not title or len(title) > 200:
            raise ValueError("决策标题长度需为 1–200 个字符")
        return title

    @staticmethod
    def _effective_decision_status(row: sqlite3.Row, now: datetime | None = None) -> str:
        status = str(row["status"])
        if status == "open" and _parse_datetime(row["closes_at"], "截止时间") <= (now or datetime.now(timezone.utc)):
            return "awaiting_extension"
        return status

    def _decision_in_connection(
        self,
        db: sqlite3.Connection,
        decision_id: str,
        user_id: str,
        *,
        couple: sqlite3.Row | None = None,
        partner_id: str | None = None,
    ) -> dict:
        if couple is None or partner_id is None:
            couple, partner_id = self._couple_context(db, user_id)
        row = db.execute(
            "SELECT * FROM decisions WHERE id=? AND couple_id=? AND deleted_at IS NULL",
            (decision_id, couple["id"]),
        ).fetchone()
        if not row:
            raise KeyError("双人决策不存在")
        return self._serialize_decision_rows(
            db, [row], user_id, couple=couple, partner_id=partner_id,
        )[0]

    def _serialize_decision_rows(
        self,
        db: sqlite3.Connection,
        rows: list[sqlite3.Row],
        user_id: str,
        *,
        couple: sqlite3.Row,
        partner_id: str,
    ) -> list[dict]:
        if not rows:
            return []
        decision_ids = [str(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in decision_ids)
        options_by_decision: dict[str, list[dict]] = {decision_id: [] for decision_id in decision_ids}
        for option in db.execute(
            f"""SELECT decision_id,id,label,position FROM decision_options
            WHERE decision_id IN ({placeholders}) ORDER BY decision_id,position""",
            decision_ids,
        ).fetchall():
            options_by_decision[str(option["decision_id"])].append({
                "id": str(option["id"]),
                "label": str(option["label"]),
                "position": int(option["position"]),
            })

        members = {str(couple["member_a"]), str(couple["member_b"])}
        submitters_by_decision: dict[str, set[str]] = {
            decision_id: set() for decision_id in decision_ids
        }
        votes_by_decision: dict[str, dict[str, list[dict]]] = {
            decision_id: {} for decision_id in decision_ids
        }
        for vote in db.execute(
            f"""SELECT v.decision_id,v.user_id,v.option_id,o.label,o.position
            FROM decision_votes v
            JOIN decisions d ON d.id=v.decision_id AND d.voting_round=v.voting_round
            JOIN decision_options o ON o.decision_id=v.decision_id AND o.id=v.option_id
            WHERE v.decision_id IN ({placeholders})
            ORDER BY v.decision_id,v.user_id,o.position""",
            decision_ids,
        ).fetchall():
            member_id = str(vote["user_id"])
            if member_id not in members:
                continue
            decision_id = str(vote["decision_id"])
            submitters_by_decision[decision_id].add(member_id)
            votes_by_decision[decision_id].setdefault(member_id, []).append({
                "id": str(vote["option_id"]),
                "label": str(vote["label"]),
            })

        results: list[dict] = []
        member_order = (str(couple["member_a"]), str(couple["member_b"]))
        for row in rows:
            decision_id = str(row["id"])
            submitters = submitters_by_decision[decision_id]
            status = self._effective_decision_status(row)
            result = {
                "id": decision_id,
                "title": str(row["title"]),
                "selection_mode": str(row["selection_mode"]),
                "status": status,
                "voting_round": int(row["voting_round"]),
                "closes_at": str(row["closes_at"]),
                "options_locked": bool(row["options_locked_at"]),
                "options": options_by_decision[decision_id],
                "submitted_by_me": user_id in submitters,
                "partner_submitted": str(partner_id) in submitters,
                "creator_id": str(row["creator_id"]),
                "version": int(row["version"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            if status in {"revealed", "pending_confirmation", "confirmed", "converted"}:
                grouped = votes_by_decision[decision_id]
                result["votes"] = [
                    {
                        "user_id": member,
                        "option_ids": [item["id"] for item in grouped.get(member, [])],
                        "options": grouped.get(member, []),
                    }
                    for member in member_order
                ]
                result["resolution"] = _json(row["resolution_json"], {})
            results.append(result)
        return results

    def create_decision(self, payload: dict, user_id: str) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("决策内容必须是对象")
        if set(payload) - {"title", "selection_mode", "options", "closes_at"}:
            raise ValueError("决策内容包含不支持的字段")
        title = self._validate_decision_title(payload.get("title"))
        mode = payload.get("selection_mode")
        if mode not in {"single", "multiple"}:
            raise ValueError("决策模式只能是单选或多选")
        labels = self._validate_decision_options(payload.get("options"))
        closes = _parse_datetime(payload.get("closes_at"), "截止时间")
        now_dt = datetime.now(timezone.utc)
        if closes <= now_dt:
            raise ValueError("截止时间必须晚于当前时间")
        decision_id, now = _id("decision"), _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, partner_id = self._couple_context(db, user_id)
            db.execute(
                """INSERT INTO decisions(
                    id,couple_id,creator_id,title,selection_mode,status,voting_round,
                    closes_at,version,created_at,updated_at
                ) VALUES (?,?,?,?,?,'open',1,?,1,?,?)""",
                (decision_id, couple["id"], user_id, title, mode, closes.isoformat(), now, now),
            )
            for position, label in enumerate(labels):
                db.execute(
                    "INSERT INTO decision_options(id,decision_id,label,position,version) VALUES (?,?,?,?,1)",
                    (_id("decision_option"), decision_id, label, position),
                )
            result = self._decision_in_connection(
                db, decision_id, user_id, couple=couple, partner_id=partner_id,
            )
            self._audit(
                db, "decision.created", "decision", decision_id,
                {"status": "open", "version": 1, "voting_round": 1}, actor_id=user_id,
            )
            self._event(
                db, couple["id"], user_id, "decision.created", decision_id,
                {"status": "open", "version": 1, "votingRound": 1},
            )
            self._insert_in_app_notification(
                db,
                couple_id=str(couple["id"]), recipient_id=partner_id, actor_id=user_id,
                event_type="decision.created", resource_type="decision",
                resource_id=decision_id, title="新的双人决策",
                body="对方发起了一项双人决策，提交后才会同时揭晓选择",
                url=f"/?screen=life&decision={decision_id}",
                dedupe_key=f"decision:{decision_id}:created:{partner_id}",
            )
        return result

    def get_decision(self, decision_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            return self._decision_in_connection(
                db, decision_id, user_id, couple=couple, partner_id=partner_id,
            )

    def list_decisions(self, user_id: str, status: str | None = None) -> list[dict]:
        if status is not None and status not in DECISION_STATUSES:
            raise ValueError("决策状态筛选无效")
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            where = ["couple_id=?", "deleted_at IS NULL"]
            params: list[object] = [couple["id"]]
            if status == "awaiting_extension":
                where.append("(status='awaiting_extension' OR (status='open' AND closes_at<=?))")
                params.append(_now())
            elif status == "open":
                where.append("status='open' AND closes_at>?")
                params.append(_now())
            elif status is not None:
                where.append("status=?")
                params.append(status)
            rows = db.execute(
                f"""SELECT * FROM decisions WHERE {' AND '.join(where)}
                ORDER BY updated_at DESC,id DESC LIMIT 100""",
                params,
            ).fetchall()
            return self._serialize_decision_rows(
                db, rows, user_id, couple=couple, partner_id=partner_id,
            )

    def prepare_decision_conversion(
        self,
        decision_id: str,
        target: object,
        option_id: object,
        user_id: str,
    ) -> dict:
        """Build a server-owned editor prefill without changing any data.

        This deliberately is not a mutation.  A normal target editor and its
        existing validated endpoint perform the eventual write after the user
        reviews the missing fields.
        """
        target_name = str(target or "").strip()
        if target_name not in self._DECISION_CONVERSION_TARGETS:
            raise ValueError("决策转换目标无效")
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT * FROM decisions WHERE id=? AND couple_id=? AND deleted_at IS NULL",
                (decision_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("双人决策不存在")
            status = self._effective_decision_status(row)
            if status != "confirmed":
                raise ValueError("只有双方确认后的决策才能打开转换预览")
            resolution = _json(row["resolution_json"], {})
            resolved_ids = resolution.get("option_ids")
            if not isinstance(resolved_ids, list) or not resolved_ids or any(not isinstance(item, str) for item in resolved_ids):
                raise ValueError("确认结果不完整，请重新确认")
            option_rows = db.execute(
                "SELECT id,label FROM decision_options WHERE decision_id=? ORDER BY position",
                (decision_id,),
            ).fetchall()
            labels = {str(item["id"]): str(item["label"]) for item in option_rows}
            if not set(resolved_ids).issubset(labels):
                raise ValueError("确认结果已失效，请重新投票")

        requested_option = str(option_id or "").strip()
        if len(resolved_ids) > 1 and not requested_option:
            raise ValueError("这个结果包含多项，请明确选择要转换的一项")
        selected_id = requested_option or resolved_ids[0]
        if selected_id not in resolved_ids:
            raise ValueError("所选内容不属于双方确认结果")
        label = labels[selected_id]
        target_contracts: dict[str, tuple[str, dict, list[str], str]] = {
            "date_plan": (
                "date_plan", {"title": f"约会 · {label}", "preference": label},
                ["plan_date", "start_time", "end_time", "budget", "place_candidate_id"], "life:dates",
            ),
            "meal_wish": (
                "meal_wish", {"title": label, "note": "来自双方确认的决定"},
                [], "life:meals:wishes",
            ),
            "trip": (
                "trip", {"destination_query": label},
                ["destination_candidate_id", "start_date", "end_date", "budget"], "trip:new",
            ),
            "hotel_search": (
                "hotel_search", {"search_term": label},
                ["trip_id", "check_in", "check_out", "adults"], "trip:stays",
            ),
            "restaurant_search": (
                "restaurant_search", {"search_term": label},
                ["scope_candidate_id"], "trip:itinerary",
            ),
            "calendar_todo": (
                "calendar", {
                    "title": label, "entry_type": "todo", "blocks_time": False,
                    "participant_ids": [],
                },
                ["starts_at"], "life:todo",
            ),
        }
        editor, values, missing_fields, next_screen = target_contracts[target_name]
        return {
            "decision_id": str(decision_id),
            "decision_version": int(row["version"]),
            "target": target_name,
            "source_type": "human",
            "requires_confirmation": True,
            "saved": False,
            "notice": "尚未保存，请检查并补全后再提交",
            "editor": editor,
            "values": values,
            "missing_fields": missing_fields,
            "next_screen": next_screen,
        }

    @staticmethod
    def _validate_vote_option_ids(
        value: object, *, selection_mode: str, available_ids: set[str],
    ) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("请选择至少一个选项")
        if any(not isinstance(item, str) or not item for item in value):
            raise ValueError("选择内容格式无效")
        option_ids = list(dict.fromkeys(value))
        if len(option_ids) != len(value):
            raise ValueError("不能重复选择同一选项")
        if selection_mode == "single" and len(option_ids) != 1:
            raise ValueError("单选决策只能选择一个选项")
        if not set(option_ids).issubset(available_ids):
            raise ValueError("选择内容不属于当前决策")
        return option_ids

    @staticmethod
    def _decision_resolution(
        selection_mode: str,
        selections: list[list[str]],
        option_order: list[str],
    ) -> dict:
        selected_sets = [set(items) for items in selections]
        if selection_mode == "single":
            if selections[0] == selections[1]:
                return {"kind": "agreement", "option_ids": list(selections[0])}
            return {"kind": "disagreement"}
        shared = selected_sets[0] & selected_sets[1]
        if not shared:
            return {"kind": "empty_intersection"}
        return {
            "kind": "intersection",
            "option_ids": [option_id for option_id in option_order if option_id in shared],
        }

    def submit_decision_vote(
        self,
        decision_id: str,
        option_ids: object,
        expected_version: object,
        idempotency_key: object,
        user_id: str,
    ) -> dict:
        expected = self._expected_decision_version(expected_version)
        if not isinstance(idempotency_key, str) or not re.fullmatch(
            r"[A-Za-z0-9:._-]{8,160}", idempotency_key.strip()
        ):
            raise ValueError("投票必须提供有效的幂等键")
        idempotency_key = idempotency_key.strip()
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, partner_id = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT * FROM decisions WHERE id=? AND couple_id=? AND deleted_at IS NULL",
                (decision_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("双人决策不存在")
            option_rows = db.execute(
                "SELECT id FROM decision_options WHERE decision_id=? ORDER BY position",
                (decision_id,),
            ).fetchall()
            available = [str(item["id"]) for item in option_rows]
            choices = self._validate_vote_option_ids(
                option_ids, selection_mode=str(row["selection_mode"]),
                available_ids=set(available),
            )
            fingerprint = self._decision_request_fingerprint({
                "decision_id": decision_id,
                "option_ids": choices,
                "expected_version": expected,
                "voting_round": int(row["voting_round"]),
            })
            operation = f"decision.vote:{decision_id}"
            replay = self._idempotent_result(
                db, couple["id"], user_id, operation, idempotency_key,
            )
            if replay is not None:
                if replay.get("request_fingerprint") != fingerprint:
                    raise ConflictError("这个投票请求标识已经用于不同内容")
                saved = replay.get("response")
                if not isinstance(saved, dict):
                    raise ConflictError("投票重试记录已失效，请刷新后重试")
                return saved

            status = self._effective_decision_status(row)
            if status == "awaiting_extension":
                raise ValueError("决策已到截止时间，请先延长或取消")
            if status != "open":
                raise ValueError("当前决策已经不能投票")
            if int(row["version"]) != expected:
                raise ConflictError("决策已被另一方修改，请刷新后重试")

            round_number = int(row["voting_round"])
            db.execute(
                "DELETE FROM decision_votes WHERE decision_id=? AND voting_round=? AND user_id=?",
                (decision_id, round_number, user_id),
            )
            for option_id in choices:
                db.execute(
                    """INSERT INTO decision_votes(
                        decision_id,voting_round,user_id,option_id,submitted_at,version
                    ) VALUES (?,?,?,?,?,1)""",
                    (decision_id, round_number, user_id, option_id, now),
                )

            partner_choices = [
                str(item["option_id"])
                for item in db.execute(
                    """SELECT option_id FROM decision_votes
                    WHERE decision_id=? AND voting_round=? AND user_id=?""",
                    (decision_id, round_number, partner_id),
                ).fetchall()
            ]
            if partner_choices:
                new_version = expected + 1
                member_selections = []
                for member_id in (str(couple["member_a"]), str(couple["member_b"])):
                    member_selections.append([
                        str(item["option_id"])
                        for item in db.execute(
                            """SELECT v.option_id FROM decision_votes v
                            JOIN decision_options o ON o.decision_id=v.decision_id AND o.id=v.option_id
                            WHERE v.decision_id=? AND v.voting_round=? AND v.user_id=?
                            ORDER BY o.position""",
                            (decision_id, round_number, member_id),
                        ).fetchall()
                    ])
                resolution = self._decision_resolution(
                    str(row["selection_mode"]), member_selections, available,
                )
                db.execute(
                    """UPDATE decisions SET status='revealed',revealed_at=?,
                    resolution_json=?,options_locked_at=COALESCE(options_locked_at,?),
                    version=?,updated_at=? WHERE id=?""",
                    (now, json.dumps(resolution, ensure_ascii=False), now, new_version, now, decision_id),
                )
                public_status = "revealed"
            else:
                db.execute(
                    "UPDATE decisions SET options_locked_at=COALESCE(options_locked_at,?) WHERE id=?",
                    (now, decision_id),
                )
                new_version = expected
                public_status = None

            result = self._decision_in_connection(
                db, decision_id, user_id, couple=couple, partner_id=partner_id,
            )
            if public_status == "revealed":
                self._audit(
                    db, "decision.revealed", "decision", decision_id,
                    {"status": public_status, "version": new_version, "voting_round": round_number},
                    actor_id=user_id,
                )
                self._event(
                    db, couple["id"], user_id, "decision.revealed", decision_id,
                    {"status": public_status, "version": new_version, "votingRound": round_number},
                )
                self._insert_in_app_notification(
                    db,
                    couple_id=str(couple["id"]), recipient_id=partner_id, actor_id=user_id,
                    event_type="decision.revealed", resource_type="decision",
                    resource_id=decision_id, title="双人决策已揭晓",
                    body="打开恋屿查看共同结果",
                    url=f"/?screen=life&decision={decision_id}",
                    dedupe_key=f"decision:{decision_id}:revealed:v{new_version}:{partner_id}",
                )
            self._save_idempotent_result(
                db, str(couple["id"]), user_id, operation, idempotency_key,
                {"request_fingerprint": fingerprint, "response": result},
            )
            return result

    def _record_decision_change(
        self,
        db: sqlite3.Connection,
        *,
        couple: sqlite3.Row,
        partner_id: str,
        user_id: str,
        decision_id: str,
        action: str,
        status: str,
        version: int,
        voting_round: int,
    ) -> None:
        safe_detail = {"status": status, "version": version, "voting_round": voting_round}
        self._audit(
            db, f"decision.{action}", "decision", decision_id, safe_detail,
            actor_id=user_id,
        )
        self._event(
            db, str(couple["id"]), user_id, f"decision.{action}", decision_id,
            {"status": status, "version": version, "votingRound": voting_round},
        )
        titles = {
            "updated": ("双人决策有更新", "对方更新了一项双人决策"),
            "extended": ("双人决策已延长", "对方延长了决策截止时间"),
            "cancelled": ("双人决策已取消", "对方取消了一项双人决策"),
            "revote": ("双人决策重新投票", "双方可以重新提交选择"),
            "confirmation_submitted": ("对方提交了共同确认", "打开恋屿完成双人确认"),
            "confirmed": ("双人决策已确认", "双方已经确认共同结果"),
            "random_drawn": ("双人决策已有结果", "随机抽取已经完成，打开恋屿查看"),
        }
        title, body = titles[action]
        self._insert_in_app_notification(
            db,
            couple_id=str(couple["id"]), recipient_id=partner_id, actor_id=user_id,
            event_type=f"decision.{action}", resource_type="decision",
            resource_id=decision_id, title=title, body=body,
            url=f"/?screen=life&decision={decision_id}",
            dedupe_key=f"decision:{decision_id}:{action}:v{version}:{partner_id}",
        )

    def update_decision(self, decision_id: str, payload: dict, user_id: str) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("决策修改内容必须是对象")
        extra = set(payload) - {"title", "selection_mode", "options", "closes_at", "expected_version"}
        if extra:
            raise ValueError("决策修改包含不支持的字段")
        expected = self._expected_decision_version(payload.get("expected_version"))
        if not set(payload) - {"expected_version"}:
            raise ValueError("没有需要修改的内容")
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, partner_id = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT * FROM decisions WHERE id=? AND couple_id=? AND deleted_at IS NULL",
                (decision_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("双人决策不存在")
            if row["creator_id"] != user_id:
                raise PermissionError("只有创建人可以在投票前修改决策")
            if int(row["version"]) != expected:
                raise ConflictError("决策已被另一方修改，请刷新后重试")
            if self._effective_decision_status(row) != "open":
                raise ValueError("当前决策已经不能修改")
            has_current_votes = db.execute(
                "SELECT 1 FROM decision_votes WHERE decision_id=? AND voting_round=? LIMIT 1",
                (decision_id, row["voting_round"]),
            ).fetchone()
            if has_current_votes:
                raise ValueError("已有成员提交选择，决策内容已经锁定")
            candidate_fields = {"options", "selection_mode"}.intersection(payload)
            if candidate_fields:
                has_historical_votes = db.execute(
                    "SELECT 1 FROM decision_votes WHERE decision_id=? LIMIT 1",
                    (decision_id,),
                ).fetchone()
                if row["options_locked_at"] or has_historical_votes:
                    raise ValueError("首次投票后不能修改候选或选择模式")

            title = self._validate_decision_title(payload.get("title", row["title"]))
            mode = payload.get("selection_mode", row["selection_mode"])
            if mode not in {"single", "multiple"}:
                raise ValueError("决策模式只能是单选或多选")
            closes = _parse_datetime(payload.get("closes_at", row["closes_at"]), "截止时间")
            if closes <= datetime.now(timezone.utc):
                raise ValueError("截止时间必须晚于当前时间")
            labels = None
            if "options" in payload:
                labels = self._validate_decision_options(payload["options"])
            new_version = expected + 1
            db.execute(
                """UPDATE decisions SET title=?,selection_mode=?,closes_at=?,
                version=?,updated_at=? WHERE id=?""",
                (title, mode, closes.isoformat(), new_version, now, decision_id),
            )
            if labels is not None:
                db.execute("DELETE FROM decision_options WHERE decision_id=?", (decision_id,))
                for position, label in enumerate(labels):
                    db.execute(
                        "INSERT INTO decision_options(id,decision_id,label,position,version) VALUES (?,?,?,?,1)",
                        (_id("decision_option"), decision_id, label, position),
                    )
            self._record_decision_change(
                db, couple=couple, partner_id=partner_id, user_id=user_id,
                decision_id=decision_id, action="updated", status="open",
                version=new_version, voting_round=int(row["voting_round"]),
            )
            return self._decision_in_connection(
                db, decision_id, user_id, couple=couple, partner_id=partner_id,
            )

    def extend_decision(
        self,
        decision_id: str,
        deadline_at: object,
        expected_version: object,
        user_id: str,
    ) -> dict:
        expected = self._expected_decision_version(expected_version)
        deadline = _parse_datetime(deadline_at, "新截止时间")
        if deadline <= datetime.now(timezone.utc):
            raise ValueError("新截止时间必须晚于当前时间")
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, partner_id = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT * FROM decisions WHERE id=? AND couple_id=? AND deleted_at IS NULL",
                (decision_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("双人决策不存在")
            if int(row["version"]) != expected:
                raise ConflictError("决策已被另一方修改，请刷新后重试")
            if self._effective_decision_status(row) not in {"open", "awaiting_extension"}:
                raise ValueError("当前决策不能延长")
            if deadline <= _parse_datetime(row["closes_at"], "截止时间"):
                raise ValueError("新截止时间必须晚于原截止时间")
            new_version = expected + 1
            db.execute(
                "UPDATE decisions SET status='open',closes_at=?,version=?,updated_at=? WHERE id=?",
                (deadline.isoformat(), new_version, now, decision_id),
            )
            self._record_decision_change(
                db, couple=couple, partner_id=partner_id, user_id=user_id,
                decision_id=decision_id, action="extended", status="open",
                version=new_version, voting_round=int(row["voting_round"]),
            )
            return self._decision_in_connection(
                db, decision_id, user_id, couple=couple, partner_id=partner_id,
            )

    def cancel_decision(
        self, decision_id: str, expected_version: object, user_id: str,
    ) -> dict:
        expected = self._expected_decision_version(expected_version)
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, partner_id = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT * FROM decisions WHERE id=? AND couple_id=? AND deleted_at IS NULL",
                (decision_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("双人决策不存在")
            if int(row["version"]) != expected:
                raise ConflictError("决策已被另一方修改，请刷新后重试")
            if self._effective_decision_status(row) not in {"open", "awaiting_extension"}:
                raise ValueError("当前决策不能取消")
            new_version = expected + 1
            db.execute(
                """UPDATE decisions SET status='cancelled',resolution_json=NULL,
                version=?,updated_at=? WHERE id=?""",
                (new_version, now, decision_id),
            )
            self._record_decision_change(
                db, couple=couple, partner_id=partner_id, user_id=user_id,
                decision_id=decision_id, action="cancelled", status="cancelled",
                version=new_version, voting_round=int(row["voting_round"]),
            )
            return self._decision_in_connection(
                db, decision_id, user_id, couple=couple, partner_id=partner_id,
            )

    def resolve_decision(
        self,
        decision_id: str,
        action: object,
        option_id: object,
        expected_version: object,
        user_id: str,
    ) -> dict:
        expected = self._expected_decision_version(expected_version)
        action_text = str(action or "").strip()
        if action_text not in {"revote", "confirm", "random_draw"}:
            raise ValueError("决策处理动作无效")
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, partner_id = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT * FROM decisions WHERE id=? AND couple_id=? AND deleted_at IS NULL",
                (decision_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("双人决策不存在")
            if int(row["version"]) != expected:
                raise ConflictError("决策已被另一方修改，请刷新后重试")
            status = str(row["status"])
            if status not in {"revealed", "pending_confirmation"}:
                raise ValueError("当前决策没有可处理的揭晓结果")
            round_number = int(row["voting_round"])

            if action_text == "revote":
                new_version = expected + 1
                db.execute(
                    """UPDATE decisions SET status='open',voting_round=voting_round+1,
                    revealed_at=NULL,resolution_json=NULL,
                    version=?,updated_at=? WHERE id=?""",
                    (new_version, now, decision_id),
                )
                self._record_decision_change(
                    db, couple=couple, partner_id=partner_id, user_id=user_id,
                    decision_id=decision_id, action="revote", status="open",
                    version=new_version, voting_round=round_number + 1,
                )
                return self._decision_in_connection(
                    db, decision_id, user_id, couple=couple, partner_id=partner_id,
                )

            option_rows = db.execute(
                "SELECT id FROM decision_options WHERE decision_id=? ORDER BY position",
                (decision_id,),
            ).fetchall()
            available = {str(item["id"]) for item in option_rows}
            current_resolution = _json(row["resolution_json"], {})

            if action_text == "random_draw":
                if status != "revealed" or current_resolution.get("kind") not in {"disagreement", "empty_intersection"}:
                    raise ValueError("只有分歧结果可以随机抽取")
                pool = sorted({
                    str(item["option_id"])
                    for item in db.execute(
                        "SELECT option_id FROM decision_votes WHERE decision_id=? AND voting_round=?",
                        (decision_id, round_number),
                    ).fetchall()
                })
                if not pool:
                    raise ValueError("没有可随机抽取的已选候选")
                chosen = secrets.choice(pool)
                resolution = {"kind": "random_draw", "option_ids": [chosen]}
                new_version = expected + 1
                db.execute(
                    "UPDATE decisions SET status='confirmed',resolution_json=?,version=?,updated_at=? WHERE id=?",
                    (json.dumps(resolution, ensure_ascii=False), new_version, now, decision_id),
                )
                self._record_decision_change(
                    db, couple=couple, partner_id=partner_id, user_id=user_id,
                    decision_id=decision_id, action="random_drawn", status="confirmed",
                    version=new_version, voting_round=round_number,
                )
                return self._decision_in_connection(
                    db, decision_id, user_id, couple=couple, partner_id=partner_id,
                )

            candidate = str(option_id or "").strip()
            if candidate not in available:
                raise ValueError("共同确认的选项不属于当前决策")
            selected_pool = {
                str(item["option_id"])
                for item in db.execute(
                    "SELECT option_id FROM decision_votes WHERE decision_id=? AND voting_round=?",
                    (decision_id, round_number),
                ).fetchall()
            }
            suggested = current_resolution.get("option_ids")
            allowed_confirmations = (
                {str(item) for item in suggested}
                if current_resolution.get("kind") in {"agreement", "intersection"}
                and isinstance(suggested, list)
                else selected_pool
            )
            if candidate not in allowed_confirmations:
                raise ValueError("只能共同确认双方本轮选择过的候选")
            pending_candidate = None
            if status == "pending_confirmation":
                pending_ids = current_resolution.get("option_ids")
                if isinstance(pending_ids, list) and len(pending_ids) == 1:
                    pending_candidate = pending_ids[0]

            if pending_candidate == candidate:
                existing = db.execute(
                    """SELECT 1 FROM decision_confirmations
                    WHERE decision_id=? AND decision_version=? AND user_id=?""",
                    (decision_id, expected, user_id),
                ).fetchone()
                if existing:
                    return self._decision_in_connection(
                        db, decision_id, user_id, couple=couple, partner_id=partner_id,
                    )
                db.execute(
                    "INSERT INTO decision_confirmations(decision_id,decision_version,user_id,confirmed_at) VALUES (?,?,?,?)",
                    (decision_id, expected, user_id, now),
                )
                confirmation_count = db.execute(
                    """SELECT COUNT(DISTINCT user_id) FROM decision_confirmations
                    WHERE decision_id=? AND decision_version=?""",
                    (decision_id, expected),
                ).fetchone()[0]
                if int(confirmation_count) == 2:
                    new_version = expected + 1
                    resolution = {"kind": "confirmed", "option_ids": [candidate]}
                    db.execute(
                        "UPDATE decisions SET status='confirmed',resolution_json=?,version=?,updated_at=? WHERE id=?",
                        (json.dumps(resolution, ensure_ascii=False), new_version, now, decision_id),
                    )
                    change_action, public_status = "confirmed", "confirmed"
                else:
                    new_version = expected
                    change_action, public_status = "confirmation_submitted", "pending_confirmation"
            else:
                new_version = expected
                resolution = {"kind": "confirmation_pending", "option_ids": [candidate]}
                db.execute(
                    "DELETE FROM decision_confirmations WHERE decision_id=? AND decision_version=?",
                    (decision_id, expected),
                )
                db.execute(
                    "UPDATE decisions SET status='pending_confirmation',resolution_json=?,updated_at=? WHERE id=?",
                    (json.dumps(resolution, ensure_ascii=False), now, decision_id),
                )
                db.execute(
                    "INSERT INTO decision_confirmations(decision_id,decision_version,user_id,confirmed_at) VALUES (?,?,?,?)",
                    (decision_id, expected, user_id, now),
                )
                change_action, public_status = "confirmation_submitted", "pending_confirmation"

            self._record_decision_change(
                db, couple=couple, partner_id=partner_id, user_id=user_id,
                decision_id=decision_id, action=change_action, status=public_status,
                version=new_version, voting_round=round_number,
            )
            return self._decision_in_connection(
                db, decision_id, user_id, couple=couple, partner_id=partner_id,
            )
