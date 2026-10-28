"""Persistent, per-user in-app notifications.

The inbox is the durable source of truth. Realtime events and Web Push may
announce a new item, but losing either delivery channel never loses the item.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _id() -> str:
    return f"notification_{uuid.uuid4().hex}"


class NotificationMixin:
    @staticmethod
    def _notification_dict(row: sqlite3.Row) -> dict:
        value = dict(row)
        value["read"] = bool(value["read_at"])
        value["archived"] = bool(value["archived_at"])
        return value

    @staticmethod
    def _validate_notification_values(
        *,
        event_type: object,
        resource_type: object,
        resource_id: object,
        title: object,
        body: object,
        url: object,
        dedupe_key: object,
    ) -> dict:
        values = {
            "event_type": str(event_type or "").strip(),
            "resource_type": str(resource_type or "").strip(),
            "resource_id": str(resource_id or "").strip(),
            "title": str(title or "").strip(),
            "body": str(body or "").strip(),
            "url": str(url or "").strip(),
            "dedupe_key": str(dedupe_key or "").strip(),
        }
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,79}", values["event_type"]):
            raise ValueError("通知事件类型无效")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,49}", values["resource_type"]):
            raise ValueError("通知资源类型无效")
        if not values["resource_id"] or len(values["resource_id"]) > 160:
            raise ValueError("通知资源标识无效")
        if not values["title"] or len(values["title"]) > 80:
            raise ValueError("通知标题无效")
        if len(values["body"]) > 180:
            raise ValueError("通知摘要不能超过 180 个字符")
        if (
            not values["url"].startswith("/")
            or values["url"].startswith("//")
            or len(values["url"]) > 500
        ):
            raise ValueError("通知跳转地址无效")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{3,180}", values["dedupe_key"]):
            raise ValueError("通知去重标识无效")
        return values

    def _insert_in_app_notification(
        self,
        db: sqlite3.Connection,
        *,
        couple_id: str,
        recipient_id: str,
        actor_id: str | None,
        event_type: object,
        resource_type: object,
        resource_id: object,
        title: object,
        body: object,
        url: object,
        dedupe_key: object,
    ) -> dict:
        values = self._validate_notification_values(
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            title=title,
            body=body,
            url=url,
            dedupe_key=dedupe_key,
        )
        couple = db.execute(
            """
            SELECT 1 FROM couples
            WHERE id=? AND status='active' AND ? IN (member_a, member_b)
            """,
            (couple_id, recipient_id),
        ).fetchone()
        if not couple:
            raise PermissionError("通知接收人不属于当前情侣空间")
        if actor_id:
            actor = db.execute(
                """
                SELECT 1 FROM couples
                WHERE id=? AND status='active' AND ? IN (member_a, member_b)
                """,
                (couple_id, actor_id),
            ).fetchone()
            if not actor:
                raise PermissionError("通知发起人不属于当前情侣空间")
        notification_id = _id()
        db.execute(
            """
            INSERT OR IGNORE INTO in_app_notifications(
                id,recipient_id,actor_id,couple_id,event_type,resource_type,
                resource_id,title,body,url,dedupe_key,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                notification_id,
                recipient_id,
                actor_id,
                couple_id,
                values["event_type"],
                values["resource_type"],
                values["resource_id"],
                values["title"],
                values["body"],
                values["url"],
                values["dedupe_key"],
                _now(),
            ),
        )
        row = db.execute(
            "SELECT * FROM in_app_notifications WHERE recipient_id=? AND dedupe_key=?",
            (recipient_id, values["dedupe_key"]),
        ).fetchone()
        return self._notification_dict(row)

    def _insert_realtime_partner_notification(
        self,
        db: sqlite3.Connection,
        *,
        couple_id: str,
        actor_id: str,
        event_type: str,
        resource_id: str,
        payload: dict,
        sequence: int,
    ) -> dict | None:
        """Persist selected shared events without copying domain content."""
        couple = db.execute(
            """
            SELECT member_a,member_b FROM couples
            WHERE id=? AND status='active'
            """,
            (couple_id,),
        ).fetchone()
        if not couple or actor_id not in (couple["member_a"], couple["member_b"]):
            return None
        recipient_id = (
            couple["member_b"] if actor_id == couple["member_a"] else couple["member_a"]
        )
        values: dict | None = None
        if event_type == "call.invited":
            recipient_id = str(payload.get("toUserId") or recipient_id)
            values = {
                "resource_type": "call",
                "title": "对方正在呼叫",
                "body": "打开恋屿查看这通来电",
                "url": "/?screen=chat",
            }
        elif event_type == "date_plan.created":
            values = {
                "resource_type": "date_plan",
                "title": "新的约会邀请",
                "body": "对方创建了一份共同约会方案",
                "url": (
                    f"/?screen=life&shared_type=date_plan"
                    f"&shared_id={resource_id}"
                ),
            }
        elif event_type == "life.updated" and payload.get("resource") == "trip":
            values = {
                "resource_type": "trip",
                "title": "共同旅行有更新",
                "body": "对方调整了共同旅行，请查看最新版本",
                "url": (
                    f"/?screen=trip&shared_type=trip"
                    f"&shared_id={resource_id}"
                ),
            }
        if not values or recipient_id == actor_id:
            return None
        return self._insert_in_app_notification(
            db,
            couple_id=couple_id,
            recipient_id=recipient_id,
            actor_id=actor_id,
            event_type=event_type,
            resource_type=values["resource_type"],
            resource_id=resource_id,
            title=values["title"],
            body=values["body"],
            url=values["url"],
            dedupe_key=f"realtime:{sequence}:{recipient_id}",
        )

    def create_in_app_notification(
        self,
        *,
        recipient_id: str,
        actor_id: str | None,
        event_type: object,
        resource_type: object,
        resource_id: object,
        title: object,
        body: object,
        url: object,
        dedupe_key: object,
    ) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, recipient_id)
            item = self._insert_in_app_notification(
                db,
                couple_id=couple["id"],
                recipient_id=recipient_id,
                actor_id=actor_id,
                event_type=event_type,
                resource_type=resource_type,
                resource_id=resource_id,
                title=title,
                body=body,
                url=url,
                dedupe_key=dedupe_key,
            )
            self._event(
                db,
                couple["id"],
                actor_id or recipient_id,
                "notification.created",
                item["id"],
                {"recipientId": recipient_id, "eventType": item["event_type"]},
            )
        return item

    def list_in_app_notifications(
        self,
        user_id: str,
        *,
        limit: int = 50,
        unread_only: bool = False,
    ) -> dict:
        limit = max(1, min(int(limit), 100))
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            unread_clause = " AND read_at IS NULL" if unread_only else ""
            rows = db.execute(
                """
                SELECT * FROM in_app_notifications
                WHERE recipient_id=? AND couple_id=? AND archived_at IS NULL
                """
                + unread_clause
                + " ORDER BY created_at DESC,id DESC LIMIT ?",
                (user_id, couple["id"], limit),
            ).fetchall()
            unread_count = db.execute(
                """
                SELECT COUNT(*) FROM in_app_notifications
                WHERE recipient_id=? AND couple_id=? AND archived_at IS NULL
                  AND read_at IS NULL
                """,
                (user_id, couple["id"]),
            ).fetchone()[0]
        return {
            "items": [self._notification_dict(row) for row in rows],
            "unread_count": int(unread_count),
        }

    def mark_in_app_notification_read(
        self, notification_id: str, user_id: str
    ) -> dict:
        now = _now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                """
                SELECT * FROM in_app_notifications
                WHERE id=? AND recipient_id=? AND couple_id=? AND archived_at IS NULL
                """,
                (notification_id, user_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("通知不存在")
            db.execute(
                "UPDATE in_app_notifications SET read_at=COALESCE(read_at,?) WHERE id=?",
                (now, notification_id),
            )
            updated = db.execute(
                "SELECT * FROM in_app_notifications WHERE id=?", (notification_id,)
            ).fetchone()
        return self._notification_dict(updated)

    def mark_all_in_app_notifications_read(self, user_id: str) -> dict:
        now = _now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            updated = db.execute(
                """
                UPDATE in_app_notifications SET read_at=?
                WHERE recipient_id=? AND couple_id=? AND archived_at IS NULL
                  AND read_at IS NULL
                """,
                (now, user_id, couple["id"]),
            ).rowcount
        return {"updated": int(updated), "read_at": now}

    def archive_in_app_notification(
        self, notification_id: str, user_id: str
    ) -> dict:
        now = _now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            updated = db.execute(
                """
                UPDATE in_app_notifications
                SET archived_at=?,read_at=COALESCE(read_at,?)
                WHERE id=? AND recipient_id=? AND couple_id=? AND archived_at IS NULL
                """,
                (now, now, notification_id, user_id, couple["id"]),
            ).rowcount
            if not updated:
                raise KeyError("通知不存在")
            row = db.execute(
                "SELECT * FROM in_app_notifications WHERE id=?", (notification_id,)
            ).fetchone()
        return self._notification_dict(row)
