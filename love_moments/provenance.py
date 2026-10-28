"""Source evidence, actual AI usage, reversible receipts and safe home priorities."""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .intelligent_ui import ConflictError
from .schema import FOUNDATION_LIFECYCLE_REGISTRY


SOURCE_TYPES = frozenset(
    {"human", "real_api", "rules", "ai", "mock", "cache", "legacy_unknown"}
)
DATE_SOURCES = frozenset(
    {"legacy_unknown", "user_confirmed", "media_capture", "trip_date", "system_provisional"}
)
EVIDENCE_KEYS = frozenset(
    {"provider", "provider_ref", "evidence_at", "external_ref", "resource_version", "confidence"}
)
TERMINAL_AI_STATUSES = frozenset({"succeeded", "failed", "timeout", "rejected", "unknown"})
SOURCE_RELIABILITY = {
    "legacy_unknown": 0, "mock": 1, "cache": 1, "rules": 2,
    "ai": 3, "real_api": 4, "human": 5,
}
DATE_RELIABILITY = {
    "legacy_unknown": 0, "system_provisional": 1, "trip_date": 2,
    "media_capture": 3, "user_confirmed": 4,
}

def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ProvenanceMixin:
    _PROVENANCE_TARGETS = {
        "moment": ("moments", "moment_id", "creator_id"),
        "media": ("media_assets", "media_id", "owner_id"),
        "trip": ("trips", "trip_id", None),
        "recipe": ("recipes", "recipe_id", "owner_id"),
        "calendar_entry": ("calendar_entries", "calendar_entry_id", "creator_id"),
        "shared_list_item": ("shared_list_items", "shared_list_item_id", "creator_id"),
    }
    _RECEIPT_OPERATIONS = {
        "calendar.complete": ("calendar_entry", "calendar_entries", {"completed_at"}),
        "calendar.soft_delete": ("calendar_entry", "calendar_entries", {"deleted_at"}),
        "trip.soft_delete": ("trip", "trips", {"deleted_at", "deleted_by"}),
        "media.trash": ("media", "media_assets", {"status", "deleted_at"}),
        "wishlist.complete": ("shared_list_item", "shared_list_items", {"completed_at"}),
        "wishlist.soft_delete": ("shared_list_item", "shared_list_items", {"deleted_at"}),
        "pantry.set_state": ("pantry_item", "pantry_items", {"state"}),
    }

    def record_provenance(
        self,
        resource_type: str,
        resource_id: str,
        *,
        user_id: str,
        source_type: str = "legacy_unknown",
        date_source: str = "legacy_unknown",
        evidence: dict[str, object] | None = None,
        trusted_producer: bool = False,
    ) -> dict[str, object]:
        target = self._PROVENANCE_TARGETS.get(str(resource_type))
        if target is None:
            raise ValueError("不支持的来源资源类型")
        if source_type not in SOURCE_TYPES or date_source not in DATE_SOURCES:
            raise ValueError("来源标记无效")
        if source_type not in {"human", "legacy_unknown"} and not trusted_producer:
            raise PermissionError("自动来源只能由可信服务写入")
        table, target_column, owner_column = target
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            if resource_type == "shared_list_item":
                row = db.execute(
                    """
                    SELECT l.couple_id,i.creator_id AS owner_id,i.version,'shared' AS visibility
                    FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id
                    WHERE i.id=? AND l.couple_id=?
                    """,
                    (resource_id, couple["id"]),
                ).fetchone()
            else:
                owner_select = f",{owner_column} AS owner_id" if owner_column else ",NULL AS owner_id"
                visibility_select = (
                    ",visibility" if resource_type in {"moment", "media", "recipe"}
                    else ",'shared' AS visibility"
                )
                row = db.execute(
                    f"SELECT couple_id{owner_select}{visibility_select},version FROM {table} WHERE id=? AND couple_id=?",
                    (resource_id, couple["id"]),
                ).fetchone()
            if not row:
                raise KeyError("来源资源不存在")
            if row["visibility"] not in {"shared", "couple"} and row["owner_id"] != user_id:
                raise PermissionError("不能修改伴侣的私密内容来源")
            supplied = evidence or {}
            resource_version = supplied.get("resource_version", row["version"])
            if not isinstance(resource_version, int) or isinstance(resource_version, bool) or resource_version != int(row["version"]):
                raise ConflictError("来源证据对应的资源版本已经变化")
            confidence = supplied.get("confidence", 1.0 if source_type == "human" else 0.5)
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
                raise ValueError("来源置信度无效")
            safe_evidence: dict[str, object] = {
                "resource_version": resource_version,
                "confidence": float(confidence),
            }
            for key, value in supplied.items():
                if key not in EVIDENCE_KEYS or key in {"resource_version", "confidence"} or value is None:
                    continue
                safe_evidence[key] = str(value)[:200]
            existing = db.execute(
                f"SELECT * FROM memory_provenance WHERE {target_column}=?",
                (resource_id,),
            ).fetchone()
            now = _now()
            if existing:
                previous_evidence = json.loads(existing["evidence_json"] or "{}")
                previous_version = previous_evidence.get("resource_version", 0)
                previous_confidence = previous_evidence.get("confidence", 0.0)
                if (
                    SOURCE_RELIABILITY[source_type] < SOURCE_RELIABILITY[existing["source_type"]]
                    or DATE_RELIABILITY[date_source] < DATE_RELIABILITY[existing["date_source"]]
                    or resource_version < previous_version
                    or (resource_version == previous_version and float(confidence) < float(previous_confidence))
                ):
                    raise ConflictError("可靠来源证据不能被较弱证据覆盖")
                db.execute(
                    f"""
                    UPDATE memory_provenance
                    SET source_type=?,date_source=?,evidence_json=?,version=version+1,updated_at=?
                    WHERE id=?
                    """,
                    (source_type, date_source, _json(safe_evidence), now, existing["id"]),
                )
                provenance_id = existing["id"]
            else:
                provenance_id = _id("provenance")
                db.execute(
                    f"""
                    INSERT INTO memory_provenance(
                        id,couple_id,owner_id,{target_column},source_type,date_source,
                        evidence_json,version,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,1,?,?)
                    """,
                    (
                        provenance_id, couple["id"], row["owner_id"], resource_id,
                        source_type, date_source, _json(safe_evidence), now, now,
                    ),
                )
            result = dict(db.execute("SELECT * FROM memory_provenance WHERE id=?", (provenance_id,)).fetchone())
            result["evidence"] = json.loads(result.pop("evidence_json"))
            return result

    @staticmethod
    def _validate_iso_datetime_or_none(value: object, field: str) -> None:
        if value is None:
            return
        if not isinstance(value, str) or not value or ("T" not in value and " " not in value):
            raise ValueError(f"{field} 必须是 ISO 日期时间或 null")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} 必须是 ISO 日期时间或 null") from exc

    def _validate_receipt_inverse(
        self,
        operation: str,
        inverse: dict[str, object],
        couple: sqlite3.Row,
    ) -> None:
        datetime_fields = {
            "calendar.complete": {"completed_at"},
            "calendar.soft_delete": {"deleted_at"},
            "trip.soft_delete": {"deleted_at"},
            "media.trash": {"deleted_at"},
            "wishlist.complete": {"completed_at"},
            "wishlist.soft_delete": {"deleted_at"},
        }
        for field in datetime_fields.get(operation, set()):
            self._validate_iso_datetime_or_none(inverse[field], field)
        if operation == "pantry.set_state":
            restores_state = set(inverse) == {"state"}
            deletes_created = set(inverse) == {"delete_if_created"}
            if restores_state and (
                not isinstance(inverse["state"], str)
                or inverse["state"] not in {"have", "low", "out"}
            ):
                raise ValueError("库存撤销状态无效")
            if deletes_created and inverse["delete_if_created"] is not True:
                raise ValueError("新建库存撤销标记无效")
            if not restores_state and not deletes_created:
                raise ValueError("库存撤销内容无效")
        if operation == "media.trash" and (
            not isinstance(inverse["status"], str)
            or inverse["status"] not in {"ready", "pending_private"}
        ):
            raise ValueError("媒体撤销状态无效")
        if operation == "trip.soft_delete":
            deleted_by = inverse["deleted_by"]
            if deleted_by is not None and (
                not isinstance(deleted_by, str)
                or deleted_by not in {couple["member_a"], couple["member_b"]}
            ):
                raise ValueError("旅行撤销操作者无效")

    def _receipt_target_row(
        self,
        db: sqlite3.Connection,
        *,
        couple: sqlite3.Row,
        actor_id: str,
        resource_type: str,
        resource_id: str,
        table: str,
    ) -> sqlite3.Row:
        if resource_type == "shared_list_item":
            row = db.execute(
                """SELECT i.version,'shared' AS visibility,i.creator_id AS owner_id
                   FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id
                   WHERE i.id=? AND l.couple_id=?""",
                (resource_id, couple["id"]),
            ).fetchone()
        elif resource_type == "media":
            row = db.execute(
                "SELECT version,visibility,owner_id FROM media_assets WHERE id=? AND couple_id=? AND purged_at IS NULL",
                (resource_id, couple["id"]),
            ).fetchone()
        else:
            row = db.execute(
                f"SELECT version,'shared' AS visibility,NULL AS owner_id FROM {table} WHERE id=? AND couple_id=?",
                (resource_id, couple["id"]),
            ).fetchone()
        if not row:
            raise KeyError("撤销目标不存在")
        if resource_type == "media" and row["visibility"] != "shared" and row["owner_id"] != actor_id:
            raise PermissionError("只能操作自己的私密媒体")
        return row

    def record_ai_usage_event(
        self,
        *,
        actor_kind: str,
        feature: str,
        provider: str,
        model: str,
        request_id: str,
        status: str,
        request_sent: bool,
        user_id: str | None = None,
        couple_id: str | None = None,
        attempt: int = 1,
        latency_ms: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        estimated_cost: float | None = None,
        cost_currency: str | None = None,
        pricing_version: str | None = None,
        failure_code: str | None = None,
    ) -> dict[str, object]:
        if actor_kind not in {"user", "system"}:
            raise ValueError("AI 调用主体无效")
        if status not in {"started", *TERMINAL_AI_STATUSES}:
            raise ValueError("AI 调用状态无效")
        if not feature or len(feature) > 100 or not provider or not model or not request_id:
            raise ValueError("AI 调用标识无效")
        numeric = (latency_ms, input_tokens, output_tokens, total_tokens)
        if any(value is not None and (not isinstance(value, int) or value < 0) for value in numeric):
            raise ValueError("AI 用量不能为负数")
        if estimated_cost is not None:
            if not math.isfinite(float(estimated_cost)) or estimated_cost < 0:
                raise ValueError("AI 费用无效")
            if not cost_currency or not pricing_version:
                raise ValueError("AI 费用缺少币种或计价版本")
        if status == "rejected" and request_sent:
            raise ValueError("未发出的请求不能标记为已发送")
        if status in {"succeeded", "timeout"} and not request_sent:
            raise ValueError("终态与发送状态不一致")
        now = _now()
        with self.connection() as db:
            if actor_kind == "user":
                if not user_id:
                    raise ValueError("用户 AI 调用缺少账号")
                couple, _ = self._couple_context(db, user_id)
                resolved_couple_id = couple["id"]
                if couple_id and couple_id != resolved_couple_id:
                    raise PermissionError("AI 调用情侣空间无效")
                couple_id = resolved_couple_id
            elif user_id is not None:
                raise ValueError("系统 AI 调用不能绑定用户")
            existing = db.execute(
                "SELECT * FROM ai_usage_events WHERE request_id=? AND attempt=? AND provider=? AND model=?",
                (request_id, attempt, provider, model),
            ).fetchone()
            finished_at = None if status == "started" else now
            if existing:
                if existing["status"] != "started":
                    return dict(existing)
                if status == "started":
                    return dict(existing)
                db.execute(
                    """
                    UPDATE ai_usage_events SET status=?,request_sent=?,finished_at=?,latency_ms=?,
                        input_tokens=?,output_tokens=?,total_tokens=?,estimated_cost=?,cost_currency=?,
                        pricing_version=?,failure_code=?
                    WHERE id=? AND status='started'
                    """,
                    (
                        status, int(request_sent), finished_at, latency_ms, input_tokens,
                        output_tokens, total_tokens, estimated_cost, cost_currency,
                        pricing_version, failure_code, existing["id"],
                    ),
                )
                event_id = existing["id"]
            else:
                event_id = _id("ai_usage")
                db.execute(
                    """
                    INSERT INTO ai_usage_events(
                        id,couple_id,user_id,actor_kind,feature,provider,model,request_id,attempt,
                        status,request_sent,started_at,finished_at,latency_ms,input_tokens,output_tokens,
                        total_tokens,estimated_cost,cost_currency,pricing_version,failure_code
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        event_id, couple_id, user_id, actor_kind, feature, provider, model,
                        request_id, attempt, status, int(request_sent), now, finished_at,
                        latency_ms, input_tokens, output_tokens, total_tokens, estimated_cost,
                        cost_currency, pricing_version, failure_code,
                    ),
                )
            return dict(db.execute("SELECT * FROM ai_usage_events WHERE id=?", (event_id,)).fetchone())

    @staticmethod
    def _public_ai_context_grant(row: sqlite3.Row, *, now: str) -> dict[str, object]:
        try:
            expires_at = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
            now_at = datetime.fromisoformat(now.replace("Z", "+00:00"))
            if expires_at.tzinfo is None or now_at.tzinfo is None:
                raise ValueError("AI grant timestamps must be timezone-aware")
            expired = expires_at.astimezone(timezone.utc) <= now_at.astimezone(timezone.utc)
        except (TypeError, ValueError):
            # An invalid legacy expiry can never safely remain active.
            expired = True
        if row["revoked_at"]:
            status = "revoked"
        elif expired:
            status = "expired"
        else:
            status = "active"
        return {
            "id": row["id"],
            "surface": row["surface"],
            "resource_type": row["resource_type"],
            "resource_id": row["resource_id"],
            "purpose": row["purpose"],
            "usage_scope": row["usage_scope"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
            "revoked_at": row["revoked_at"],
            "version": int(row["version"]),
            "status": status,
        }

    def get_privacy_center(self, user_id: str) -> dict[str, object]:
        """Return personal AI usage and grant metadata without prompt content.

        The privacy center deliberately aggregates usage rows and allowlists grant
        metadata.  Prompt text, selected resource bodies, model responses and
        request identifiers never leave this method.
        """
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            user = db.execute("SELECT timezone FROM users WHERE id=?", (user_id,)).fetchone()
            timezone_name = str(user["timezone"] or "UTC") if user else "UTC"
            try:
                local_zone = ZoneInfo(timezone_name)
            except (ZoneInfoNotFoundError, ValueError):
                timezone_name = "UTC"
                local_zone = timezone.utc
            local_now = now_dt.astimezone(local_zone)
            local_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if local_start.month == 12:
                local_end = local_start.replace(year=local_start.year + 1, month=1)
            else:
                local_end = local_start.replace(month=local_start.month + 1)
            start_at = local_start.astimezone(timezone.utc).isoformat()
            end_at = local_end.astimezone(timezone.utc).isoformat()
            usage_rows = db.execute(
                """
                SELECT feature,status,request_sent,input_tokens,output_tokens,total_tokens,
                       estimated_cost,cost_currency
                FROM ai_usage_events
                WHERE couple_id=? AND user_id=? AND started_at>=? AND started_at<?
                ORDER BY started_at,id
                """,
                (couple["id"], user_id, start_at, end_at),
            ).fetchall()
            feature_counts: dict[str, int] = {}
            status_counts: dict[str, int] = {}
            costs: dict[str, float] = {}
            sent_attempts = 0
            unknown_cost_attempts = 0
            input_tokens = output_tokens = total_tokens = 0
            for row in usage_rows:
                feature = str(row["feature"])
                status = str(row["status"])
                feature_counts[feature] = feature_counts.get(feature, 0) + 1
                status_counts[status] = status_counts.get(status, 0) + 1
                if bool(row["request_sent"]):
                    sent_attempts += 1
                    if row["estimated_cost"] is None:
                        unknown_cost_attempts += 1
                input_tokens += int(row["input_tokens"] or 0)
                output_tokens += int(row["output_tokens"] or 0)
                total_tokens += int(row["total_tokens"] or 0)
                if row["estimated_cost"] is not None:
                    currency = str(row["cost_currency"] or "UNKNOWN")
                    costs[currency] = costs.get(currency, 0.0) + float(row["estimated_cost"])
            grant_rows = db.execute(
                """
                SELECT id,surface,resource_type,resource_id,purpose,usage_scope,expires_at,
                       created_at,last_used_at,revoked_at,version
                FROM ai_context_grants
                WHERE couple_id=? AND user_id=?
                ORDER BY created_at DESC,id DESC LIMIT 100
                """,
                (couple["id"], user_id),
            ).fetchall()
        grants = [self._public_ai_context_grant(row, now=now) for row in grant_rows]
        return {
            "generated_at": now,
            "month": local_start.strftime("%Y-%m"),
            "timezone": timezone_name,
            "usage": {
                "attempts": len(usage_rows),
                "sent_attempts": sent_attempts,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "unknown_cost_attempts": unknown_cost_attempts,
                "status_counts": status_counts,
                "feature_counts": feature_counts,
                "costs": [
                    {"currency": currency, "amount": round(amount, 8)}
                    for currency, amount in sorted(costs.items())
                ],
            },
            "grants": grants,
            "notice": "撤回会阻止后续 AI 调用；已经发送给模型的数据无法收回。",
        }

    def revoke_ai_context_grant(
        self, grant_id: str, expected_version: object, user_id: str
    ) -> dict[str, object]:
        if not grant_id:
            raise ValueError("AI 授权标识不能为空")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("AI 授权版本无效")
        now = _now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                """
                SELECT id,couple_id,user_id,surface,resource_type,resource_id,purpose,usage_scope,
                       expires_at,created_at,last_used_at,revoked_at,version
                FROM ai_context_grants WHERE id=? AND couple_id=?
                """,
                (grant_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("AI 授权不存在")
            if row["user_id"] != user_id:
                raise PermissionError("只能撤回自己的 AI 授权")
            if row["revoked_at"]:
                result = self._public_ai_context_grant(row, now=now)
                result["idempotent"] = True
                result["notice"] = "已阻止后续 AI 调用；已经发送给模型的数据无法收回。"
                return result
            if int(row["version"]) != expected_version:
                raise ConflictError("AI 授权已经变化，请刷新后再试")
            updated = db.execute(
                """
                UPDATE ai_context_grants
                SET revoked_at=?,version=version+1
                WHERE id=? AND couple_id=? AND user_id=? AND version=? AND revoked_at IS NULL
                """,
                (now, grant_id, couple["id"], user_id, expected_version),
            ).rowcount
            if updated != 1:
                raise ConflictError("AI 授权已经变化，请刷新后再试")
            result_row = db.execute(
                """
                SELECT id,couple_id,user_id,surface,resource_type,resource_id,purpose,usage_scope,
                       expires_at,created_at,last_used_at,revoked_at,version
                FROM ai_context_grants WHERE id=?
                """,
                (grant_id,),
            ).fetchone()
        result = self._public_ai_context_grant(result_row, now=now)
        result["idempotent"] = False
        result["notice"] = "已阻止后续 AI 调用；已经发送给模型的数据无法收回。"
        return result

    def create_mutation_receipt(
        self,
        db: sqlite3.Connection,
        *,
        actor_id: str,
        resource_type: str,
        resource_id: str,
        operation: str,
        inverse: dict[str, object],
        expected_post_version: int,
        client_request_id: str,
        ttl_seconds: int = 600,
    ) -> dict[str, str]:
        contract = self._RECEIPT_OPERATIONS.get(operation)
        if contract is None or contract[0] != resource_type:
            raise ValueError("此操作不可撤销")
        pantry_created_inverse = operation == "pantry.set_state" and set(inverse) == {"delete_if_created"}
        if set(inverse) != contract[2] and not pantry_created_inverse:
            raise ValueError("撤销内容不符合操作契约")
        if expected_post_version < 1 or not client_request_id:
            raise ValueError("撤销版本或幂等标识无效")
        couple, _ = self._couple_context(db, actor_id)
        _, target_table, _ = contract
        self._validate_receipt_inverse(operation, inverse, couple)
        canonical_inverse = _json(inverse)
        existing = db.execute(
            "SELECT * FROM mutation_receipts WHERE couple_id=? AND actor_id=? AND operation=? AND client_request_id=?",
            (couple["id"], actor_id, operation, client_request_id),
        ).fetchone()
        if existing:
            if not (
                existing["resource_type"] == resource_type
                and existing["resource_id"] == resource_id
                and int(existing["expected_post_version"]) == int(expected_post_version)
                and _json(json.loads(existing["inverse_json"])) == canonical_inverse
            ):
                raise ConflictError("幂等标识已绑定到另一项撤销操作")
            self._receipt_target_row(
                db, couple=couple, actor_id=actor_id, resource_type=resource_type,
                resource_id=resource_id, table=target_table,
            )
            return {"receipt_id": existing["id"], "expires_at": existing["expires_at"]}
        target_row = self._receipt_target_row(
            db, couple=couple, actor_id=actor_id, resource_type=resource_type,
            resource_id=resource_id, table=target_table,
        )
        if int(target_row["version"]) != int(expected_post_version):
            raise ConflictError("撤销目标版本已经变化")
        now_value = datetime.now(timezone.utc).replace(microsecond=0)
        expires = now_value + timedelta(seconds=max(30, min(int(ttl_seconds), 3600)))
        receipt_id = _id("receipt")
        db.execute(
            """
            INSERT INTO mutation_receipts(
                id,couple_id,actor_id,resource_type,resource_id,operation,inverse_json,
                expected_post_version,client_request_id,created_at,expires_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                receipt_id, couple["id"], actor_id, resource_type, resource_id,
                operation, canonical_inverse, expected_post_version, client_request_id,
                now_value.isoformat(), expires.isoformat(),
            ),
        )
        return {"receipt_id": receipt_id, "expires_at": expires.isoformat()}

    def undo_mutation(self, receipt_id: str, user_id: str) -> dict[str, object]:
        now = _now()
        deferred_conflict: str | None = None
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            receipt = db.execute(
                "SELECT * FROM mutation_receipts WHERE id=? AND couple_id=?",
                (receipt_id, couple["id"]),
            ).fetchone()
            if not receipt:
                raise KeyError("撤销记录不存在")
            if receipt["actor_id"] != user_id:
                raise PermissionError("只能撤销自己的操作")
            if receipt["status"] == "undone":
                return {
                    "receipt_id": receipt_id,
                    "undone": True,
                    "idempotent": True,
                    "resource_id": receipt["resource_id"],
                    "version": receipt["resulting_version"],
                }
            if receipt["status"] != "available":
                raise ConflictError("该操作当前不能撤销")
            if receipt["expires_at"] <= now:
                db.execute(
                    "UPDATE mutation_receipts SET status='expired',inverse_json='{}' WHERE id=? AND status='available'",
                    (receipt_id,),
                )
                deferred_conflict = "撤销时间已过"
            contract = self._RECEIPT_OPERATIONS.get(receipt["operation"])
            inverse: dict[str, object] | None = None
            if deferred_conflict is None and (
                contract is None or contract[0] != receipt["resource_type"]
            ):
                db.execute(
                    "UPDATE mutation_receipts SET status='invalidated',inverse_json='{}' WHERE id=?",
                    (receipt_id,),
                )
                deferred_conflict = "撤销记录已失效"
            if deferred_conflict is None:
                assert contract is not None
                try:
                    decoded = json.loads(receipt["inverse_json"])
                    pantry_created_inverse = (
                        receipt["operation"] == "pantry.set_state"
                        and isinstance(decoded, dict)
                        and set(decoded) == {"delete_if_created"}
                    )
                    if not isinstance(decoded, dict) or (
                        set(decoded) != contract[2] and not pantry_created_inverse
                    ):
                        raise ValueError("撤销字段不符合持久化契约")
                    self._validate_receipt_inverse(receipt["operation"], decoded, couple)
                    inverse = decoded
                except (KeyError, TypeError, ValueError):
                    db.execute(
                        "UPDATE mutation_receipts SET status='invalidated',inverse_json='{}' WHERE id=?",
                        (receipt_id,),
                    )
                    deferred_conflict = "撤销记录已失效"
            if deferred_conflict is not None:
                # Leave the context normally so invalidation and secret
                # scrubbing commit before the client receives HTTP 409.
                pass
            else:
                assert contract is not None and inverse is not None
                _, table, fields = contract
                target_row = self._receipt_target_row(
                    db, couple=couple, actor_id=user_id, resource_type=receipt["resource_type"],
                    resource_id=receipt["resource_id"], table=table,
                )
                if int(target_row["version"]) != int(receipt["expected_post_version"]):
                    raise ConflictError("内容已经变化，请刷新后再试")
                delete_created_pantry = (
                    receipt["operation"] == "pantry.set_state"
                    and inverse.get("delete_if_created") is True
                )
                if receipt["resource_type"] == "shared_list_item":
                    scope = "id=? AND list_id IN (SELECT id FROM shared_lists WHERE couple_id=?)"
                else:
                    scope = "id=? AND couple_id=?"
                if delete_created_pantry:
                    updated = db.execute(
                        "DELETE FROM pantry_items WHERE id=? AND couple_id=? AND version=?",
                        (receipt["resource_id"], couple["id"], receipt["expected_post_version"]),
                    ).rowcount
                else:
                    assignments = ",".join(f"{field}=?" for field in sorted(fields))
                    params = [inverse[field] for field in sorted(fields)]
                    params.extend([receipt["resource_id"], couple["id"], receipt["expected_post_version"]])
                    if receipt["resource_type"] == "media":
                        scope += " AND purged_at IS NULL"
                    updated = db.execute(
                        f"UPDATE {table} SET {assignments},version=version+1 WHERE {scope} AND version=?",
                        params,
                    ).rowcount
                if updated != 1:
                    raise ConflictError("内容已经变化，请刷新后再试")
                resulting_version = int(receipt["expected_post_version"]) + 1
                consumed = db.execute(
                    """
                    UPDATE mutation_receipts SET status='undone',resulting_version=?,consumed_at=?,inverse_json='{}'
                    WHERE id=? AND status='available'
                    """,
                    (resulting_version, now, receipt_id),
                ).rowcount
                if consumed != 1:
                    raise ConflictError("撤销状态已经变化")
                return {
                    "receipt_id": receipt_id,
                    "undone": True,
                    "idempotent": False,
                    "resource_id": receipt["resource_id"],
                    "version": resulting_version,
                }
        assert deferred_conflict is not None
        raise ConflictError(deferred_conflict)

    def get_today_priorities(self, user_id: str, *, limit: int = 3) -> list[dict[str, str]]:
        limit = max(1, min(int(limit), 3))
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            now = _now()
            buckets: list[list[dict[str, str]]] = [[], [], []]
            for row in db.execute(
                """
                SELECT id,title,summary,updated_at FROM life_assistant_plans
                WHERE couple_id=? AND status='pending_confirmation' AND expires_at>?
                ORDER BY updated_at DESC LIMIT 3
                """,
                (couple["id"], now),
            ).fetchall():
                buckets[0].append({
                    "type": "assistant_plan", "resource_id": row["id"], "title": row["title"],
                    "subtitle": "等待双方确认", "url": f"/?screen=home&assistant-plan={row['id']}",
                })
            for row in db.execute(
                """
                SELECT id,title,url FROM in_app_notifications
                WHERE couple_id=? AND recipient_id=? AND read_at IS NULL AND archived_at IS NULL
                ORDER BY created_at DESC LIMIT 3
                """,
                (couple["id"], user_id),
            ).fetchall():
                buckets[1].append({
                    "type": "notification", "resource_id": row["id"], "title": row["title"],
                    "subtitle": "有一条未读通知", "url": row["url"] or "/?screen=home&section=notifications",
                })
            for row in db.execute(
                """
                SELECT id,title,starts_at FROM calendar_entries
                WHERE couple_id=? AND entry_type='todo' AND completed_at IS NULL AND deleted_at IS NULL
                ORDER BY starts_at,id LIMIT 3
                """,
                (couple["id"],),
            ).fetchall():
                buckets[2].append({
                    "type": "todo", "resource_id": row["id"], "title": row["title"],
                    "subtitle": "共同待办", "url": f"/?screen=life&section=calendar&entry={row['id']}",
                })
            priorities = [bucket.pop(0) for bucket in buckets if bucket][:limit]
            while len(priorities) < limit and any(buckets):
                for bucket in buckets:
                    if bucket and len(priorities) < limit:
                        priorities.append(bucket.pop(0))
            return priorities

    def recover_stale_ai_usage_events(self, *, max_age_seconds: int = 3600) -> int:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max(60, int(max_age_seconds)))
        ).replace(microsecond=0).isoformat()
        now = _now()
        with self.connection() as db:
            return db.execute(
                """UPDATE ai_usage_events
                   SET status='unknown',finished_at=?,failure_code='process_interrupted'
                   WHERE status='started' AND started_at<?""",
                (now, cutoff),
            ).rowcount

    def record_ai_telemetry_failure(self) -> None:
        now = _now()
        detail = _json({"feature": "life_assistant", "reason": "persistence_error"})
        try:
            with self.connection() as db:
                db.execute(
                    """INSERT INTO system_events(
                        id,event_key,severity,category,summary,detail_json,occurrences,
                        first_seen_at,last_seen_at,resolved_at,resolved_by
                    ) VALUES (?,'ai-telemetry:persistence-failed','warning','ai',?, ?,1,?,?,NULL,NULL)
                    ON CONFLICT(event_key) DO UPDATE SET
                        occurrences=system_events.occurrences+1,last_seen_at=excluded.last_seen_at,
                        resolved_at=NULL,resolved_by=NULL""",
                    (_id("system"), "AI 用量记录写入失败", detail, now, now),
                )
        except Exception:
            return

    def export_foundation_lifecycle_rows(
        self, db: sqlite3.Connection, *, couple_id: str, user_id: str
    ) -> dict[str, list[dict[str, object]]]:
        exported: dict[str, list[dict[str, object]]] = {}
        for table, policy in FOUNDATION_LIFECYCLE_REGISTRY.items():
            exported[table] = getattr(self, str(policy["export"]))(
                db, couple_id=couple_id, user_id=user_id
            )
        return exported

    def _export_trip_emergency_rows(self, db, *, couple_id: str, user_id: str) -> list[dict[str, object]]:
        rows = db.execute(
            """SELECT e.* FROM trip_emergency_entries e JOIN trips t ON t.id=e.trip_id
               WHERE t.couple_id=? AND e.deleted_at IS NULL
                 AND (e.visibility='shared' OR e.creator_id=?)
               ORDER BY e.trip_id,e.position,e.id""",
            (couple_id, user_id),
        ).fetchall()
        return [self._public_emergency_entry(row, user_id) for row in rows]

    def _export_voice_transcript_rows(
        self, db, *, couple_id: str, user_id: str
    ) -> list[dict[str, object]]:
        columns = (
            "t.id,t.owner_id,t.media_id,t.moment_id,t.body,t.language,t.method,"
            "t.source_type,t.status,t.visibility,t.reveal_at,t.version,t.created_at,t.updated_at"
        )
        return [dict(row) for row in db.execute(
            f"""SELECT {columns} FROM voice_transcripts t
                JOIN moments m ON m.id=t.moment_id
                JOIN media_assets media ON media.id=t.media_id
                WHERE t.couple_id=? AND t.media_id=m.media_id
                  AND media.couple_id=m.couple_id AND media.status='ready'
                  AND media.deleted_at IS NULL AND media.purged_at IS NULL
                  AND {self._visible_moment_sql('m')}
                  AND (t.owner_id=? OR (
                       t.status='ready' AND (t.visibility='shared'
                            OR (t.visibility='delayed' AND t.reveal_at<=?))
                       AND media.visibility='shared'))""",
            (couple_id, couple_id, user_id, _now(), user_id, _now()),
        ).fetchall()]

    @staticmethod
    def _export_future_letter_rows(db, *, couple_id: str, user_id: str) -> list[dict[str, object]]:
        rows = [dict(row) for row in db.execute(
            """SELECT id,author_id,recipient_id,title,body,status,deliver_at,sealed_at,
                      delivered_at,delivered_moment_id,version,created_at,updated_at,
                      CASE WHEN status IN ('draft','sealed') THEN 1 ELSE 0 END AS unpublished
               FROM future_letters WHERE couple_id=? AND status<>'deleted'
                 AND (author_id=? OR (status='delivered' AND recipient_id=?))""",
            (couple_id, user_id, user_id),
        ).fetchall()]
        for row in rows:
            row["unpublished"] = bool(row["unpublished"])
        return rows

    def _export_memory_description_rows(
        self, db: sqlite3.Connection, *, couple_id: str, user_id: str
    ) -> list[dict[str, object]]:
        columns = "d.id,d.author_id,d.moment_id,d.trip_id,d.body,d.visibility,d.source_type,d.version,d.created_at,d.updated_at"
        moment_rows = db.execute(
            f"""SELECT {columns} FROM memory_descriptions d
                JOIN moments m ON m.id=d.moment_id
                WHERE d.couple_id=? AND {self._visible_description_sql('d')}
                  AND {self._visible_moment_sql('m')}""",
            (couple_id, user_id, couple_id, user_id, _now()),
        ).fetchall()
        trip_rows = db.execute(
            f"""SELECT {columns} FROM memory_descriptions d
                JOIN trips t ON t.id=d.trip_id
                WHERE d.couple_id=? AND {self._visible_description_sql('d')}
                  AND {self._visible_trip_sql('t')}""",
            (couple_id, user_id, couple_id),
        ).fetchall()
        return [dict(row) for row in (*moment_rows, *trip_rows)]

    @staticmethod
    def _export_album_export_rows(
        db: sqlite3.Connection, *, couple_id: str, user_id: str
    ) -> list[dict[str, object]]:
        rows = db.execute(
            """SELECT id,creator_id,template,year,trip_id,audience,status,failure_code,
                      size_bytes,created_at,completed_at,expires_at,updated_at,version
               FROM album_export_jobs WHERE couple_id=? AND creator_id=?
               ORDER BY created_at,id""",
            (couple_id, user_id),
        ).fetchall()
        exported: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            if item["trip_id"] and not db.execute(
                "SELECT 1 FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL",
                (item["trip_id"], couple_id),
            ).fetchone():
                item["trip_id"] = None
            if item["failure_code"] not in {
                None, "selection_changed", "permission_changed", "media_missing",
                "render_failed", "expired", "cancelled",
            }:
                item["failure_code"] = "render_failed"
            item["temporary_artifact_included"] = False
            exported.append(item)
        return exported

    def apply_foundation_lifecycle(
        self,
        db: sqlite3.Connection,
        *,
        event: str,
        couple_id: str,
        user_id: str | None = None,
    ) -> None:
        now = _now()
        if event not in {"unlink", "account_delete"} or (event == "account_delete" and not user_id):
            raise ValueError("未知的敏感数据生命周期事件")
        for policy in FOUNDATION_LIFECYCLE_REGISTRY.values():
            handler_name = policy[event]
            if handler_name:
                getattr(self, str(handler_name))(
                    db, couple_id=couple_id, user_id=user_id, now=now
                )

    @staticmethod
    def _unlink_future_letter_rows(db, *, couple_id: str, user_id: str | None, now: str) -> None:
        del user_id
        db.execute(
            """UPDATE future_letters SET status='draft',deliver_at=NULL,sealed_at=NULL,
                      version=version+1,updated_at=?
               WHERE couple_id=? AND status='sealed'""",
            (now, couple_id),
        )

    @staticmethod
    def _revoke_album_export_rows(db, *, couple_id: str, user_id: str | None, now: str) -> None:
        del user_id
        db.execute(
            """UPDATE album_export_jobs SET status='expired',failure_code='permission_changed',
                      artifact_relative_path=NULL,artifact_sha256=NULL,size_bytes=NULL,
                      version=version+1,updated_at=?
               WHERE couple_id=? AND status IN ('queued','running','completed')""",
            (now, couple_id),
        )

    @staticmethod
    def _delete_trip_emergency_rows(db, *, couple_id: str, user_id: str, now: str) -> None:
        db.execute(
            """UPDATE trip_emergency_entries
               SET title='已删除的应急信息',details_json='{}',
                   deleted_at=COALESCE(deleted_at,?),
                   version=version+1,updated_at=?
               WHERE creator_id=? AND visibility='private'
                 AND trip_id IN (SELECT id FROM trips WHERE couple_id=?)""",
            (now, now, user_id, couple_id),
        )
        ProvenanceMixin._downgrade_trip_packing_rows(
            db, couple_id=couple_id, user_id=None, now=now,
        )

    @staticmethod
    def _downgrade_trip_packing_rows(
        db, *, couple_id: str, user_id: str | None, now: str,
    ) -> None:
        parameters: tuple[object, ...]
        member_clause = ""
        if user_id:
            member_clause = " AND assigned_user_id=?"
            parameters = (now, couple_id, user_id)
        else:
            parameters = (now, couple_id)
        db.execute(
            """UPDATE trip_packing_items
               SET assignment='shared',assigned_user_id=NULL,version=version+1,updated_at=?
               WHERE deleted_at IS NULL
                 AND assignment='person' AND assigned_user_id IS NOT NULL
                 AND trip_id IN (SELECT id FROM trips WHERE couple_id=?)"""
            + member_clause,
            parameters,
        )

    @staticmethod
    def _delete_voice_transcript_rows(db, *, couple_id: str, user_id: str, now: str) -> None:
        db.execute(
            """UPDATE voice_transcripts SET body='',status='failed',version=version+1,updated_at=?
               WHERE couple_id=? AND owner_id=? AND visibility<>'shared'""",
            (now, couple_id, user_id),
        )

    @staticmethod
    def _delete_future_letter_rows(db, *, couple_id: str, user_id: str, now: str) -> None:
        db.execute(
            """UPDATE future_letters SET title='',body='',status='deleted',deleted_at=?,
                      deliver_at=NULL,sealed_at=NULL,version=version+1,updated_at=?
               WHERE couple_id=? AND author_id=? AND status<>'delivered'""",
            (now, now, couple_id, user_id),
        )
        db.execute(
            """UPDATE future_letters SET status='draft',deliver_at=NULL,sealed_at=NULL,
                      version=version+1,updated_at=?
               WHERE couple_id=? AND recipient_id=? AND status='sealed'""",
            (now, couple_id, user_id),
        )
        # A create-idempotency receipt contains the original draft DTO so the
        # same retry can return exactly the same result.  Once the author has
        # deleted their account that unpublished plaintext must be scrubbed as
        # well; the deleted account can no longer legitimately replay it.
        db.execute(
            """DELETE FROM mutation_idempotency
               WHERE couple_id=? AND user_id=? AND operation='future.create'""",
            (couple_id, user_id),
        )

    @staticmethod
    def _delete_memory_description_rows(db, *, couple_id: str, user_id: str, now: str) -> None:
        db.execute(
            """UPDATE memory_descriptions SET body='',deleted_at=COALESCE(deleted_at,?),
                      version=version+1,updated_at=?
               WHERE couple_id=? AND author_id=? AND visibility='private'""",
            (now, now, couple_id, user_id),
        )

    def invalidate_foundation_scope(self, db: sqlite3.Connection, couple_id: str, user_id: str | None = None) -> None:
        now = _now()
        user_clause = " AND user_id=?" if user_id else ""
        parameters: tuple[object, ...] = (now, couple_id, user_id) if user_id else (now, couple_id)
        db.execute(
            f"UPDATE ai_context_grants SET revoked_at=COALESCE(revoked_at,?),version=version+1 WHERE couple_id=? AND revoked_at IS NULL{user_clause}",
            parameters,
        )
        receipt_parameters: tuple[object, ...] = (couple_id, user_id) if user_id else (couple_id,)
        actor_clause = " AND actor_id=?" if user_id else ""
        db.execute(
            f"UPDATE mutation_receipts SET status='invalidated',inverse_json='{{}}' WHERE couple_id=? AND status='available'{actor_clause}",
            receipt_parameters,
        )
