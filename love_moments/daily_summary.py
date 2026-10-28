"""Privacy-safe nightly summaries for a couple."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DailySummaryMixin:
    @staticmethod
    def _summary_dict(row: sqlite3.Row) -> dict:
        value = dict(row)
        try:
            value["metrics"] = json.loads(value.pop("metrics_json", "{}"))
        except json.JSONDecodeError:
            value["metrics"] = {}
        return value

    @staticmethod
    def _summary_utc_window(local_date: date, timezone_name: str) -> tuple[str, str]:
        zone = ZoneInfo(timezone_name)
        start = datetime.combine(local_date, time.min, tzinfo=zone)
        end = start + timedelta(days=1)
        return (
            start.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            end.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        )

    def _generate_daily_summary_in_connection(
        self,
        db: sqlite3.Connection,
        *,
        couple: sqlite3.Row,
        local_date: date,
        timezone_name: str,
    ) -> tuple[dict, bool]:
        existing = db.execute(
            """
            SELECT * FROM daily_couple_summaries
            WHERE couple_id=? AND local_date=?
            """,
            (couple["id"], local_date.isoformat()),
        ).fetchone()
        if existing:
            return self._summary_dict(existing), False
        start_utc, end_utc = self._summary_utc_window(local_date, timezone_name)
        metrics = {
            "messages": int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM messages
                    WHERE couple_id=? AND sent_at>=? AND sent_at<?
                    """,
                    (couple["id"], start_utc, end_utc),
                ).fetchone()[0]
            ),
            "plans_applied": int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM life_assistant_plans
                    WHERE couple_id=? AND applied_at>=? AND applied_at<?
                    """,
                    (couple["id"], start_utc, end_utc),
                ).fetchone()[0]
            ),
            "todos_completed": int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM calendar_entries
                    WHERE couple_id=? AND entry_type='todo'
                      AND completed_at>=? AND completed_at<?
                    """,
                    (couple["id"], start_utc, end_utc),
                ).fetchone()[0]
            ),
            "meals_planned": int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM meal_cart_items
                    WHERE couple_id=? AND planned_date=? AND status!='cancelled'
                    """,
                    (couple["id"], local_date.isoformat()),
                ).fetchone()[0]
            ),
            "pending_plans": int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM life_assistant_plans
                    WHERE couple_id=? AND status='pending_confirmation'
                      AND expires_at>?
                    """,
                    (couple["id"], _now()),
                ).fetchone()[0]
            ),
            "shopping_remaining": int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM shopping_list_items i
                    JOIN shopping_lists l ON l.id=i.shopping_list_id
                    WHERE l.couple_id=? AND l.status='active' AND i.checked=0
                    """,
                    (couple["id"],),
                ).fetchone()[0]
            ),
        }
        parts = []
        if metrics["messages"]:
            parts.append(f"{metrics['messages']} 条新消息")
        if metrics["plans_applied"]:
            parts.append(f"{metrics['plans_applied']} 份共同方案已生效")
        if metrics["todos_completed"]:
            parts.append(f"完成 {metrics['todos_completed']} 项共同待办")
        if metrics["meals_planned"]:
            parts.append(f"安排了 {metrics['meals_planned']} 道菜")
        if metrics["pending_plans"]:
            parts.append(f"还有 {metrics['pending_plans']} 份方案待确认")
        if metrics["shopping_remaining"]:
            parts.append(f"采购清单剩余 {metrics['shopping_remaining']} 项")
        body = "；".join(parts) if parts else "今天没有必须处理的共同事项，轻松过好今晚吧。"
        summary_id = f"couple_summary_{uuid.uuid4().hex}"
        created_at = _now()
        db.execute(
            """
            INSERT INTO daily_couple_summaries(
                id,couple_id,local_date,title,body,metrics_json,source,created_at
            ) VALUES (?,?,?,?,?,?,'rules',?)
            """,
            (
                summary_id,
                couple["id"],
                local_date.isoformat(),
                "今晚的双人摘要",
                body,
                json.dumps(metrics, ensure_ascii=False, separators=(",", ":")),
                created_at,
            ),
        )
        for recipient_id in (couple["member_a"], couple["member_b"]):
            self._insert_in_app_notification(
                db,
                couple_id=couple["id"],
                recipient_id=recipient_id,
                actor_id=couple["member_a"],
                event_type="assistant.daily_summary",
                resource_type="daily_summary",
                resource_id=summary_id,
                title="今晚的双人摘要",
                body="今天的共同事项已经整理好",
                url=f"/?screen=home&daily-summary={local_date.isoformat()}",
                dedupe_key=(
                    f"daily-summary:{couple['id']}:{local_date.isoformat()}"
                ),
            )
        self._event(
            db,
            couple["id"],
            couple["member_a"],
            "assistant.daily_summary_created",
            summary_id,
            {"localDate": local_date.isoformat()},
        )
        row = db.execute(
            "SELECT * FROM daily_couple_summaries WHERE id=?", (summary_id,)
        ).fetchone()
        return self._summary_dict(row), True

    def generate_daily_couple_summary(
        self, user_id: str, *, local_date: str | None = None
    ) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            settings = self._assistant_settings_in_connection(db, couple["id"])
            day = (
                date.fromisoformat(local_date)
                if local_date
                else datetime.now(ZoneInfo(settings["timezone"])).date()
            )
            summary, _ = self._generate_daily_summary_in_connection(
                db,
                couple=couple,
                local_date=day,
                timezone_name=settings["timezone"],
            )
            return summary

    def get_daily_couple_summary(
        self, user_id: str, *, local_date: str | None = None
    ) -> dict | None:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            settings = self._assistant_settings_in_connection(db, couple["id"])
            day = (
                date.fromisoformat(local_date)
                if local_date
                else datetime.now(ZoneInfo(settings["timezone"])).date()
            )
            row = db.execute(
                """
                SELECT * FROM daily_couple_summaries
                WHERE couple_id=? AND local_date=?
                """,
                (couple["id"], day.isoformat()),
            ).fetchone()
            return self._summary_dict(row) if row else None

    def run_due_daily_summaries(self, *, now: datetime | None = None) -> dict:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        generated = 0
        checked = 0
        push_recipients: list[dict[str, str]] = []
        with self.connection() as db:
            couples = db.execute(
                "SELECT * FROM couples WHERE status='active' ORDER BY id"
            ).fetchall()
            for couple in couples:
                settings = self._assistant_settings_in_connection(db, couple["id"])
                if not settings["daily_summary_enabled"]:
                    continue
                checked += 1
                local_now = current.astimezone(ZoneInfo(settings["timezone"]))
                due_hour, due_minute = (
                    int(value) for value in settings["daily_summary_time"].split(":")
                )
                if (local_now.hour, local_now.minute) < (due_hour, due_minute):
                    continue
                _, created = self._generate_daily_summary_in_connection(
                    db,
                    couple=couple,
                    local_date=local_now.date(),
                    timezone_name=settings["timezone"],
                )
                generated += int(created)
                if created:
                    push_recipients.extend(
                        [
                            {
                                "user_id": couple["member_a"],
                                "local_date": local_now.date().isoformat(),
                            },
                            {
                                "user_id": couple["member_b"],
                                "local_date": local_now.date().isoformat(),
                            },
                        ]
                    )
        return {
            "checked": checked,
            "generated": generated,
            "push_recipients": push_recipients,
            "ran_at": current.isoformat(),
        }
