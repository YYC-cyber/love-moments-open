"""Private drafts and race-safe delivery for letters to the future."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .intelligent_ui import ConflictError


_KEY_RE = re.compile(r"^[A-Za-z0-9:._-]{8,160}$")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _now() -> str:
    return _now_dt().isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _version(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("版本无效")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("版本无效") from exc
    if parsed < 1 or str(value).strip() != str(parsed):
        raise ValueError("版本无效")
    return parsed


def _utc_datetime(value: object, field: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field}格式无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field}必须包含时区")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


class FutureLetterMixin:
    @staticmethod
    def _future_pair_active(db: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]) -> bool:
        return bool(db.execute(
            """SELECT 1 FROM couples c
               JOIN users a ON a.id=c.member_a AND a.deleted_at IS NULL
               JOIN users b ON b.id=c.member_b AND b.deleted_at IS NULL
               WHERE c.id=? AND c.status='active'
                 AND ((c.member_a=? AND c.member_b=?) OR (c.member_a=? AND c.member_b=?))""",
            (
                row["couple_id"], row["author_id"], row["recipient_id"],
                row["recipient_id"], row["author_id"],
            ),
        ).fetchone())

    @staticmethod
    def _future_public(row: sqlite3.Row | dict[str, Any], user_id: str) -> dict[str, Any]:
        status = str(row["status"])
        body: str | None = row["body"]
        if status == "sealed" and row["author_id"] == user_id:
            body = None
        return {
            "id": row["id"],
            "title": row["title"],
            "body": body,
            "status": status,
            "deliver_at": row["deliver_at"],
            "sealed_at": row["sealed_at"],
            "delivered_at": row["delivered_at"],
            "converted": bool(row["delivered_moment_id"]),
            "version": int(row["version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "is_author": row["author_id"] == user_id,
        }

    def _future_row_for_viewer(
        self, db: sqlite3.Connection, letter_id: str, user_id: str
    ) -> sqlite3.Row:
        row = db.execute("SELECT * FROM future_letters WHERE id=?", (letter_id,)).fetchone()
        if not row or row["status"] == "deleted":
            raise KeyError("未来信不存在")
        if row["author_id"] == user_id and row["status"] in {"draft", "sealed"}:
            return row
        if (
            user_id in {row["author_id"], row["recipient_id"]}
            and row["status"] == "delivered"
            and self._future_pair_active(db, row)
        ):
            return row
        raise KeyError("未来信不存在")

    @staticmethod
    def _letter_values(payload: dict[str, Any]) -> tuple[str, str]:
        if not isinstance(payload, dict) or set(payload) - {"title", "body", "expected_version"}:
            raise ValueError("未来信内容无效")
        title = str(payload.get("title") or "").strip()
        body = str(payload.get("body") or "")
        if not title or len(title) > 120:
            raise ValueError("未来信标题需为 1–120 个字符")
        if not body.strip() or len(body) > 20_000:
            raise ValueError("未来信正文需为 1–20000 个字符")
        return title, body

    def create_future_letter(
        self, payload: dict[str, Any], user_id: str, idempotency_key: str
    ) -> dict[str, Any]:
        title, body = self._letter_values(payload)
        key = str(idempotency_key or "").strip()
        if not _KEY_RE.fullmatch(key):
            raise ValueError("幂等键格式无效")
        canonical = {"title": title, "body": body}
        request_hash = hashlib.sha256(_canonical(canonical).encode()).hexdigest()
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, partner_id = self._couple_context(db, user_id)
            replay = self._idempotent_result(db, couple["id"], user_id, "future.create", key)
            if replay:
                if replay.get("request_hash") != request_hash:
                    raise ConflictError("幂等键已用于另一封未来信")
                return dict(replay["response"])
            letter_id = _id("letter")
            db.execute(
                """INSERT INTO future_letters(
                       id,couple_id,author_id,recipient_id,title,body,status,
                       client_request_id,version,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,'draft',?,1,?,?)""",
                (letter_id, couple["id"], user_id, partner_id, title, body, key, now, now),
            )
            row = db.execute("SELECT * FROM future_letters WHERE id=?", (letter_id,)).fetchone()
            response = self._future_public(row, user_id)
            self._audit(db, "future_letter.created", "future_letter", letter_id, {"status": "draft"}, actor_id=user_id)
            self._save_idempotent_result(
                db, couple["id"], user_id, "future.create", key,
                {"request_hash": request_hash, "response": response},
            )
            return response

    def list_future_letters(self, user_id: str, *, scope: str = "mine") -> dict[str, Any]:
        if scope not in {"mine", "delivered"}:
            raise ValueError("未来信列表范围无效")
        with self.connection() as db:
            if scope == "mine":
                rows = db.execute(
                    """SELECT * FROM future_letters
                       WHERE author_id=? AND status<>'deleted'
                       ORDER BY updated_at DESC,id DESC""",
                    (user_id,),
                ).fetchall()
                rows = [
                    row for row in rows
                    if row["status"] in {"draft", "sealed"}
                    or (row["status"] == "delivered" and self._future_pair_active(db, row))
                ]
            else:
                rows = db.execute(
                    """SELECT f.* FROM future_letters f JOIN couples c ON c.id=f.couple_id
                       JOIN users a ON a.id=f.author_id AND a.deleted_at IS NULL
                       JOIN users r ON r.id=f.recipient_id AND r.deleted_at IS NULL
                       WHERE f.status='delivered' AND c.status='active'
                         AND ? IN (f.author_id,f.recipient_id)
                         AND ((c.member_a=f.author_id AND c.member_b=f.recipient_id)
                           OR (c.member_a=f.recipient_id AND c.member_b=f.author_id))
                       ORDER BY f.delivered_at DESC,f.id DESC""",
                    (user_id,),
                ).fetchall()
            items = [self._future_public(row, user_id) for row in rows]
            return {"items": items, "total": len(items)}

    def get_future_letter(self, letter_id: str, user_id: str) -> dict[str, Any]:
        with self.connection() as db:
            return self._future_public(self._future_row_for_viewer(db, letter_id, user_id), user_id)

    def update_future_letter(
        self, letter_id: str, payload: dict[str, Any], user_id: str
    ) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) - {"title", "body", "expected_version"}:
            raise ValueError("未来信修改内容无效")
        expected = _version(payload.get("expected_version"))
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._future_row_for_viewer(db, letter_id, user_id)
            if row["author_id"] != user_id:
                raise PermissionError("只能修改自己的未来信")
            if row["status"] != "draft":
                raise ConflictError("只有草稿可以编辑")
            if int(row["version"]) != expected:
                raise ConflictError("未来信已经变化，请刷新后重试")
            title, body = self._letter_values({
                "title": payload.get("title", row["title"]),
                "body": payload.get("body", row["body"]),
            })
            changed = db.execute(
                """UPDATE future_letters SET title=?,body=?,version=version+1,updated_at=?
                   WHERE id=? AND author_id=? AND status='draft' AND version=?""",
                (title, body, _now(), letter_id, user_id, expected),
            ).rowcount
            if changed != 1:
                raise ConflictError("未来信已经变化，请刷新后重试")
            updated = db.execute("SELECT * FROM future_letters WHERE id=?", (letter_id,)).fetchone()
            self._audit(db, "future_letter.updated", "future_letter", letter_id, {"status": "draft"}, actor_id=user_id)
            return self._future_public(updated, user_id)

    def seal_future_letter(
        self, letter_id: str, deliver_at: str, expected_version: int, user_id: str
    ) -> dict[str, Any]:
        expected = _version(expected_version)
        deliver = _utc_datetime(deliver_at, "投递时间")
        now_dt = _now_dt()
        if deliver <= now_dt:
            raise ValueError("投递时间必须晚于当前时间")
        if deliver > now_dt + timedelta(days=366 * 20):
            raise ValueError("投递时间不能超过 20 年")
        now = now_dt.isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._future_row_for_viewer(db, letter_id, user_id)
            if row["author_id"] != user_id:
                raise PermissionError("只能封存自己的未来信")
            if row["status"] != "draft" or int(row["version"]) != expected:
                raise ConflictError("未来信已经变化，请刷新后重试")
            if not self._future_pair_active(db, row):
                raise PermissionError("只能投递给当前伴侣")
            changed = db.execute(
                """UPDATE future_letters SET status='sealed',deliver_at=?,sealed_at=?,
                           version=version+1,updated_at=?
                   WHERE id=? AND author_id=? AND status='draft' AND version=?""",
                (deliver.isoformat(), now, now, letter_id, user_id, expected),
            ).rowcount
            if changed != 1:
                raise ConflictError("未来信已经变化，请刷新后重试")
            updated = db.execute("SELECT * FROM future_letters WHERE id=?", (letter_id,)).fetchone()
            self._audit(db, "future_letter.sealed", "future_letter", letter_id, {"status": "sealed"}, actor_id=user_id)
            return self._future_public(updated, user_id)

    def cancel_future_letter(
        self, letter_id: str, expected_version: int, user_id: str
    ) -> dict[str, Any]:
        expected = _version(expected_version)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._future_row_for_viewer(db, letter_id, user_id)
            if row["author_id"] != user_id:
                raise PermissionError("只能取消自己的未来信")
            if row["status"] != "sealed" or int(row["version"]) != expected:
                raise ConflictError("未来信已经变化，请刷新后重试")
            changed = db.execute(
                """UPDATE future_letters SET status='draft',deliver_at=NULL,sealed_at=NULL,
                           version=version+1,updated_at=?
                   WHERE id=? AND author_id=? AND status='sealed' AND version=?""",
                (_now(), letter_id, user_id, expected),
            ).rowcount
            if changed != 1:
                raise ConflictError("未来信已经变化，请刷新后重试")
            updated = db.execute("SELECT * FROM future_letters WHERE id=?", (letter_id,)).fetchone()
            self._audit(db, "future_letter.cancelled", "future_letter", letter_id, {"status": "draft"}, actor_id=user_id)
            return self._future_public(updated, user_id)

    def delete_future_letter(
        self, letter_id: str, expected_version: int, user_id: str
    ) -> dict[str, Any]:
        expected = _version(expected_version)
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._future_row_for_viewer(db, letter_id, user_id)
            if row["author_id"] != user_id:
                raise PermissionError("只能删除自己的未来信")
            if row["status"] != "draft" or int(row["version"]) != expected:
                raise ConflictError("只有当前版本草稿可以删除")
            changed = db.execute(
                """UPDATE future_letters SET title='',body='',status='deleted',deleted_at=?,
                           version=version+1,updated_at=?
                   WHERE id=? AND author_id=? AND status='draft' AND version=?""",
                (now, now, letter_id, user_id, expected),
            ).rowcount
            if changed != 1:
                raise ConflictError("未来信已经变化，请刷新后重试")
            self._audit(db, "future_letter.deleted", "future_letter", letter_id, {"status": "deleted"}, actor_id=user_id)
            return {"id": letter_id, "deleted": True}

    def deliver_due_future_letters(
        self, *, now: datetime | None = None, limit: int = 50
    ) -> dict[str, Any]:
        effective = (now or _now_dt()).astimezone(timezone.utc).replace(microsecond=0)
        bounded = max(1, min(int(limit), 200))
        with self.connection() as db:
            due_ids = [row["id"] for row in db.execute(
                """SELECT id FROM future_letters
                   WHERE status='sealed' AND deliver_at<=?
                   ORDER BY deliver_at,id LIMIT ?""",
                (effective.isoformat(), bounded),
            ).fetchall()]
        delivered = skipped = 0
        push_recipients: list[dict[str, str]] = []
        for letter_id in due_ids:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM future_letters WHERE id=?", (letter_id,)).fetchone()
                if not row or row["status"] != "sealed" or row["deliver_at"] > effective.isoformat():
                    skipped += 1
                    continue
                expected = int(row["version"])
                if not self._future_pair_active(db, row):
                    author_exists = db.execute(
                        "SELECT 1 FROM users WHERE id=? AND deleted_at IS NULL", (row["author_id"],)
                    ).fetchone()
                    if author_exists:
                        db.execute(
                            """UPDATE future_letters SET status='draft',deliver_at=NULL,sealed_at=NULL,
                                      version=version+1,updated_at=?
                               WHERE id=? AND status='sealed' AND version=?""",
                            (effective.isoformat(), letter_id, expected),
                        )
                    else:
                        db.execute(
                            """UPDATE future_letters SET title='',body='',status='deleted',deleted_at=?,
                                      deliver_at=NULL,sealed_at=NULL,version=version+1,updated_at=?
                               WHERE id=? AND status='sealed' AND version=?""",
                            (effective.isoformat(), effective.isoformat(), letter_id, expected),
                        )
                    skipped += 1
                    continue
                changed = db.execute(
                    """UPDATE future_letters SET status='delivered',delivered_at=?,
                               version=version+1,updated_at=?
                       WHERE id=? AND status='sealed' AND version=?""",
                    (effective.isoformat(), effective.isoformat(), letter_id, expected),
                ).rowcount
                if changed != 1:
                    skipped += 1
                    continue
                self._insert_in_app_notification(
                    db,
                    couple_id=row["couple_id"], recipient_id=row["recipient_id"], actor_id=row["author_id"],
                    event_type="future_letter.delivered", resource_type="future_letter", resource_id=letter_id,
                    title="未来信已送达", body="一封定时信件已经可以打开。",
                    url=f"/?screen=timeline&future_letter={letter_id}",
                    dedupe_key=f"future-letter-delivered:{letter_id}:{row['recipient_id']}",
                )
                self._audit(db, "future_letter.delivered", "future_letter", letter_id, {"status": "delivered"}, actor_id=row["author_id"])
                self._event(db, row["couple_id"], row["author_id"], "future_letter.delivered", letter_id, {"resource": "future_letter"})
                delivered += 1
                push_recipients.append({
                    "user_id": row["recipient_id"], "letter_id": letter_id,
                    "tag": f"future-letter-delivered:{letter_id}:{row['recipient_id']}",
                    "url": f"/?screen=timeline&future_letter={letter_id}",
                })
        return {"delivered": delivered, "skipped": skipped, "push_recipients": push_recipients}

    def convert_future_letter_to_moment(
        self,
        letter_id: str,
        expected_version: int,
        user_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        expected = _version(expected_version)
        key = str(idempotency_key or "").strip()
        if not _KEY_RE.fullmatch(key):
            raise ValueError("幂等键格式无效")
        request = {"letter_id": letter_id, "expected_version": expected}
        request_hash = hashlib.sha256(_canonical(request).encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._future_row_for_viewer(db, letter_id, user_id)
            couple, _ = self._couple_context(db, user_id)
            replay = self._idempotent_result(db, couple["id"], user_id, "future.convert", key)
            if replay:
                if replay.get("request_hash") != request_hash:
                    raise ConflictError("幂等键已用于另一封未来信")
                return dict(replay["response"])
            if row["status"] != "delivered" or not self._future_pair_active(db, row):
                raise ConflictError("未来信尚未送达")
            if int(row["version"]) != expected:
                raise ConflictError("未来信已经变化，请刷新后重试")
            if row["delivered_moment_id"]:
                raise ConflictError("这封未来信已经保存为回忆")
            moment_id = _id("moment")
            db.execute(
                """INSERT INTO moments(
                       id,couple_id,creator_id,type,title,body,occurred_at,visibility
                   ) VALUES (?,?,?,?,?,?,?,'shared')""",
                (
                    moment_id, row["couple_id"], row["author_id"], "text", row["title"],
                    row["body"], row["delivered_at"],
                ),
            )
            changed = db.execute(
                """UPDATE future_letters SET delivered_moment_id=?,version=version+1,updated_at=?
                   WHERE id=? AND status='delivered' AND delivered_moment_id IS NULL AND version=?""",
                (moment_id, _now(), letter_id, expected),
            ).rowcount
            if changed != 1:
                raise ConflictError("未来信已经变化，请刷新后重试")
            self._upsert_memory_provenance_in_connection(
                db, "moment", moment_id, row["couple_id"], row["author_id"],
                source_type="human", date_source="user_confirmed", resource_version=1,
            )
            response = {
                "letter_id": letter_id,
                "moment_id": moment_id,
                "letter_version_before": expected,
                "letter_version": expected + 1,
            }
            self._audit(db, "future_letter.converted", "future_letter", letter_id, {"moment_id": moment_id}, actor_id=user_id)
            self._event(db, row["couple_id"], user_id, "moment.created", moment_id, {"type": "text", "visibility": "shared"})
            self._save_idempotent_result(
                db, couple["id"], user_id, "future.convert", key,
                {"request_hash": request_hash, "response": response},
            )
            return response
