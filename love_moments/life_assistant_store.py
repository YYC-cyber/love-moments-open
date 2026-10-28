"""Persistence and atomic execution for shared couple life plans."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .intelligent_ui import ConflictError
from .life_assistant import LifeIntent
from .recipes import build_shopping_items, normalize_name, recipe_snapshot


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


class LifeAssistantMixin:
    """Mixin relying on store authentication, meals, calendar and notification helpers."""

    def _fail_life_assistant_job(self, job_id: str, error: Exception) -> None:
        """Persist a terminal failure even when plan construction rolled back."""
        with self.connection() as db:
            db.execute(
                """
                UPDATE life_assistant_jobs
                SET status='failed',error_code=?,completed_at=?
                WHERE id=? AND status='running'
                """,
                (type(error).__name__[:80], _now(), job_id),
            )

    def _recover_life_assistant_jobs_after_restart(self) -> int:
        """A restarted process cannot resume prompts because raw prompts are not stored."""
        now = _now()
        with self.connection() as db:
            return int(
                db.execute(
                    """
                    UPDATE life_assistant_jobs
                    SET status='failed',error_code='server_restarted',completed_at=?
                    WHERE status IN ('queued','running')
                    """,
                    (now,),
                ).rowcount
            )

    def _assistant_settings_in_connection(
        self, db: sqlite3.Connection, couple_id: str
    ) -> dict:
        row = db.execute(
            "SELECT * FROM couple_assistant_settings WHERE couple_id=?",
            (couple_id,),
        ).fetchone()
        if row:
            value = dict(row)
            value["daily_summary_enabled"] = bool(value["daily_summary_enabled"])
            return value
        return {
            "couple_id": couple_id,
            "timezone": "Asia/Shanghai",
            "daily_summary_enabled": True,
            "daily_summary_time": "21:30",
            "updated_by": None,
            "updated_at": None,
        }

    def get_life_assistant_settings(self, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            return self._assistant_settings_in_connection(db, couple["id"])

    def update_life_assistant_settings(self, payload: dict, user_id: str) -> dict:
        timezone_name = str(payload.get("timezone") or "Asia/Shanghai").strip()
        try:
            ZoneInfo(timezone_name)
        except Exception as error:
            raise ValueError("时区无效") from error
        summary_time = str(payload.get("daily_summary_time") or "21:30").strip()
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", summary_time):
            raise ValueError("每日摘要时间无效")
        enabled = int(bool(payload.get("daily_summary_enabled", True)))
        now = _now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            db.execute(
                """
                INSERT INTO couple_assistant_settings(
                    couple_id,timezone,daily_summary_enabled,daily_summary_time,
                    updated_by,updated_at
                ) VALUES (?,?,?,?,?,?)
                ON CONFLICT(couple_id) DO UPDATE SET
                    timezone=excluded.timezone,
                    daily_summary_enabled=excluded.daily_summary_enabled,
                    daily_summary_time=excluded.daily_summary_time,
                    updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at
                """,
                (couple["id"], timezone_name, enabled, summary_time, user_id, now),
            )
            self._event(
                db,
                couple["id"],
                user_id,
                "assistant.settings_updated",
                couple["id"],
                {"dailySummaryEnabled": bool(enabled)},
            )
            return self._assistant_settings_in_connection(db, couple["id"])

    @staticmethod
    def _plan_item_dict(row: sqlite3.Row) -> dict:
        value = dict(row)
        value["payload"] = _parse(value.pop("payload_json", "{}"), {})
        value["preview"] = _parse(value.pop("preview_json", "{}"), {})
        return value

    def _plan_in_connection(
        self, db: sqlite3.Connection, row: sqlite3.Row, user_id: str
    ) -> dict:
        value = dict(row)
        expected = _parse(value.pop("expected_versions_json", "{}"), {})
        value["warnings"] = list(expected.pop("warnings", []))
        value["candidate_recipes"] = list(expected.pop("candidate_recipes", []))
        value["expected_versions"] = expected
        value["result"] = _parse(value.pop("result_json", None), None)
        value["items"] = [
            self._plan_item_dict(item)
            for item in db.execute(
                """
                SELECT * FROM life_assistant_plan_items
                WHERE plan_id=? ORDER BY position,id
                """,
                (row["id"],),
            ).fetchall()
        ]
        confirmations = [
            dict(item)
            for item in db.execute(
                """
                SELECT c.*,u.nickname,u.avatar_label
                FROM life_assistant_confirmations c
                JOIN users u ON u.id=c.user_id
                WHERE c.plan_id=? AND c.plan_version=?
                ORDER BY c.confirmed_at,c.user_id
                """,
                (row["id"], row["version"]),
            ).fetchall()
        ]
        members = [
            dict(item)
            for item in db.execute(
                """
                SELECT u.id,u.nickname,u.avatar_label
                FROM users u JOIN couples c
                  ON u.id IN (c.member_a,c.member_b)
                WHERE c.id=? AND c.status='active'
                ORDER BY CASE WHEN u.id=c.member_a THEN 0 ELSE 1 END
                """,
                (row["couple_id"],),
            ).fetchall()
        ]
        confirmed_ids = {item["user_id"] for item in confirmations}
        value["confirmations"] = confirmations
        value["members"] = [
            {**member, "confirmed": member["id"] in confirmed_ids}
            for member in members
        ]
        value["confirmation_count"] = len(confirmations)
        value["confirmation_required"] = len(members)
        value["my_confirmed"] = user_id in confirmed_ids
        return value

    def get_life_assistant_plan(self, plan_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT * FROM life_assistant_plans WHERE id=? AND couple_id=?",
                (plan_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("共同方案不存在")
            if row["status"] == "pending_confirmation" and row["expires_at"] <= _now():
                db.execute(
                    "UPDATE life_assistant_plans SET status='expired',updated_at=? WHERE id=?",
                    (_now(), plan_id),
                )
                row = db.execute(
                    "SELECT * FROM life_assistant_plans WHERE id=?", (plan_id,)
                ).fetchone()
            return self._plan_in_connection(db, row, user_id)

    def list_life_assistant_plans(
        self, user_id: str, *, status: str = "", limit: int = 30
    ) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        allowed = {
            "",
            "generating",
            "pending_confirmation",
            "applied",
            "cancelled",
            "failed",
            "expired",
        }
        if status not in allowed:
            raise ValueError("共同方案状态无效")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            db.execute(
                """
                UPDATE life_assistant_plans SET status='expired',updated_at=?
                WHERE couple_id=? AND status='pending_confirmation' AND expires_at<=?
                """,
                (_now(), couple["id"], _now()),
            )
            clause = " AND status=?" if status else ""
            parameters: list[object] = [couple["id"]]
            if status:
                parameters.append(status)
            parameters.append(limit)
            rows = db.execute(
                """
                SELECT * FROM life_assistant_plans
                WHERE couple_id=?
                """
                + clause
                + " ORDER BY updated_at DESC,id DESC LIMIT ?",
                tuple(parameters),
            ).fetchall()
            return [self._plan_in_connection(db, row, user_id) for row in rows]

    def _visible_recipe_candidates(
        self,
        db: sqlite3.Connection,
        *,
        couple_id: str,
        dish_query: str,
        limit: int = 8,
    ) -> list[sqlite3.Row]:
        normalized = normalize_name(dish_query)
        if not normalized:
            return []
        return db.execute(
            """
            SELECT * FROM recipes
            WHERE deleted_at IS NULL
              AND (visibility='system' OR (couple_id=? AND visibility='couple'))
              AND (normalized_name=? OR normalized_name LIKE ? OR name LIKE ?)
            ORDER BY
              CASE WHEN normalized_name=? THEN 0 ELSE 1 END,
              CASE source_type WHEN 'built_in' THEN 0 ELSE 1 END,
              updated_at DESC,id
            LIMIT ?
            """,
            (
                couple_id,
                normalized,
                f"%{normalized}%",
                f"%{dish_query}%",
                normalized,
                limit,
            ),
        ).fetchall()

    def _build_life_plan(
        self,
        db: sqlite3.Connection,
        *,
        couple: sqlite3.Row,
        creator_id: str,
        intent_text: str,
        intent: LifeIntent,
        source: str,
    ) -> dict:
        settings = self._assistant_settings_in_connection(db, couple["id"])
        local_now = datetime.now(ZoneInfo(settings["timezone"]))
        if intent.requested_date < local_now.date():
            raise ValueError("不能创建过去日期的共同安排，请选择今天或未来日期")
        if (
            intent.add_calendar
            and intent.requested_date == local_now.date()
            and datetime.strptime(intent.planned_time, "%H:%M").time()
            <= local_now.time().replace(second=0, microsecond=0)
        ):
            raise ValueError("这个时间已经过去，请选择稍后的时间")
        date_text = intent.requested_date.isoformat()
        meal_labels = {
            "breakfast": "早餐",
            "lunch": "午餐",
            "dinner": "晚餐",
            "snack": "加餐",
        }
        label = meal_labels[intent.meal_slot]
        dish = intent.dish_query or "共同安排"
        subject = intent.dish_query or intent.task_query or "共同安排"
        title = f"{date_text} · {subject}"
        if intent.dish_query:
            summary = (
                f"计划在 {date_text} {intent.planned_time} 安排{label}：{dish}。"
            )
        elif intent.add_calendar:
            summary = f"计划在 {date_text} {intent.planned_time} 安排：{subject}。"
        else:
            summary = f"计划在 {date_text} 完成共同待办：{subject}。"
        warnings: list[str] = []
        candidates = self._visible_recipe_candidates(
            db, couple_id=couple["id"], dish_query=intent.dish_query
        )
        exact = [
            row
            for row in candidates
            if row["normalized_name"] == normalize_name(intent.dish_query)
        ]
        selected = exact[0] if len(exact) == 1 else None
        candidate_values = [
            {
                "id": row["id"],
                "name": row["name"],
                "variant_name": row["variant_name"],
                "version": row["version"],
            }
            for row in candidates
        ]
        if len(exact) > 1:
            warnings.append("找到多个同名做法，请选择具体菜谱后再确认采购。")
        elif not selected and intent.dish_query:
            warnings.append("没有找到完全匹配的共享菜谱，暂不生成食材用量。")
        if not intent.dish_query and not intent.task_query:
            warnings.append("还没有识别到具体菜品，可以编辑方案标题后重新提出安排。")

        items: list[dict] = []
        if intent.add_calendar:
            local_start = datetime.combine(
                intent.requested_date,
                datetime.strptime(intent.planned_time, "%H:%M").time(),
                tzinfo=ZoneInfo(settings["timezone"]),
            )
            starts_at = local_start.isoformat(timespec="seconds")
            calendar_title = f"{label} · {dish}" if intent.dish_query else subject
            items.append(
                {
                    "item_type": "calendar.create",
                    "payload": {
                        "entry_type": "event",
                        "title": calendar_title,
                        "starts_at": starts_at,
                        "all_day": False,
                        "recurrence": "none",
                        "location": "",
                        "note": "由双人生活助理生成，双方确认后写入。",
                    },
                    "preview": {
                        "label": "加入共同日历",
                        "before": None,
                        "after": {
                            "date": date_text,
                            "time": intent.planned_time,
                            "title": calendar_title,
                        },
                    },
                }
            )
        elif intent.add_todo:
            items.append(
                {
                    "item_type": "todo.create",
                    "payload": {
                        "entry_type": "todo",
                        "title": (
                            intent.task_query
                            or (f"准备{dish}" if intent.dish_query else subject)
                        ),
                        "starts_at": date_text,
                        "all_day": True,
                        "recurrence": "none",
                        "location": "",
                        "note": "由双人生活助理生成，双方确认后写入。",
                    },
                    "preview": {
                        "label": "加入共同待办",
                        "before": None,
                        "after": {
                            "date": date_text,
                            "title": (
                                intent.task_query
                                or (
                                    f"准备{dish}"
                                    if intent.dish_query
                                    else subject
                                )
                            ),
                        },
                    },
                }
            )
        if selected:
            items.append(
                {
                    "item_type": "meal.schedule",
                    "payload": {
                        "recipe_id": selected["id"],
                        "recipe_version": int(selected["version"]),
                        "planned_date": date_text,
                        "meal_slot": intent.meal_slot,
                        "planned_time": intent.planned_time,
                        "servings": 2,
                        "course_type": "main",
                        "note": "双方确认的生活助理安排",
                    },
                    "preview": {
                        "label": "加入用餐日历",
                        "before": None,
                        "after": {
                            "date": date_text,
                            "meal": label,
                            "recipe": selected["name"],
                            "servings": 2,
                        },
                    },
                }
            )
            if intent.add_shopping:
                ingredients = db.execute(
                    """
                    SELECT name,quantity_milli,quantity_text,canonical_unit,
                           specification,optional
                    FROM recipe_ingredients WHERE recipe_id=?
                    ORDER BY position
                    """,
                    (selected["id"],),
                ).fetchall()
                if ingredients and all(
                    row["quantity_milli"] or row["quantity_text"] for row in ingredients
                ):
                    items.append(
                        {
                            "item_type": "shopping.merge",
                            "payload": {
                                "planned_date": date_text,
                                "recipe_id": selected["id"],
                            },
                            "preview": {
                                "label": "合并采购清单",
                                "before": "保留已勾选和人工锁定项",
                                "after": {
                                    "ingredient_count": len(ingredients),
                                    "recipe": selected["name"],
                                },
                            },
                        }
                    )
                else:
                    warnings.append("菜谱食材不完整，未生成采购清单。")
        elif intent.dish_query:
            items.append(
                {
                    "item_type": "meal.wish",
                    "payload": {
                        "title": intent.dish_query,
                        "note": "选择具体菜谱后才能计算采购食材。",
                    },
                    "preview": {
                        "label": "加入想吃清单",
                        "before": None,
                        "after": {"title": intent.dish_query},
                    },
                }
            )
        if not items:
            items.append(
                {
                    "item_type": "todo.create",
                    "payload": {
                        "entry_type": "todo",
                        "title": intent_text[:80],
                        "starts_at": date_text,
                        "all_day": True,
                        "recurrence": "none",
                        "location": "",
                        "note": "请确认后加入共同待办。",
                    },
                    "preview": {
                        "label": "加入共同待办",
                        "before": None,
                        "after": {"date": date_text, "title": intent_text[:80]},
                    },
                }
            )

        cart = db.execute(
            "SELECT version FROM meal_carts WHERE couple_id=? AND status='active'",
            (couple["id"],),
        ).fetchone()
        shopping = db.execute(
            "SELECT version FROM shopping_lists WHERE couple_id=? AND status='active'",
            (couple["id"],),
        ).fetchone()
        expected: dict[str, Any] = {
            "couple": int(couple["version"]),
            "meal_cart": int(cart["version"]) if cart else 0,
            "shopping_list": int(shopping["version"]) if shopping else 0,
            "warnings": warnings,
            "candidate_recipes": candidate_values,
        }
        if selected:
            expected[f"recipe:{selected['id']}"] = int(selected["version"])
        return {
            "title": title[:120],
            "summary": summary[:500],
            "items": items,
            "expected": expected,
            "source": source,
        }

    def create_life_assistant_job(self, payload: dict, user_id: str) -> dict:
        intent = str(payload.get("intent") or "").strip()
        client_request_id = str(payload.get("client_request_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9:._-]{8,160}", client_request_id):
            raise ValueError("客户端请求标识无效")
        if not intent or len(intent) > 1000:
            raise ValueError("一句话安排长度需为 1–1000 个字符")
        now = _now()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=7)
        ).replace(microsecond=0).isoformat()
        input_hash = hashlib.sha256(intent.encode("utf-8")).hexdigest()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            previous = db.execute(
                """
                SELECT * FROM life_assistant_jobs
                WHERE couple_id=? AND creator_id=? AND client_request_id=?
                """,
                (couple["id"], user_id, client_request_id),
            ).fetchone()
            if previous:
                return self._job_in_connection(db, previous, user_id)
            job_id = _id("assistant_job")
            inserted = db.execute(
                """
                INSERT OR IGNORE INTO life_assistant_jobs(
                    id,couple_id,creator_id,client_request_id,input_hash,status,created_at
                ) VALUES (?,?,?,?,?,'running',?)
                """,
                (
                    job_id,
                    couple["id"],
                    user_id,
                    client_request_id,
                    input_hash,
                    now,
                ),
            )
            if inserted.rowcount != 1:
                previous = db.execute(
                    """
                    SELECT * FROM life_assistant_jobs
                    WHERE couple_id=? AND creator_id=? AND client_request_id=?
                    """,
                    (couple["id"], user_id, client_request_id),
                ).fetchone()
                return self._job_in_connection(db, previous, user_id)
            timezone_name = self._assistant_settings_in_connection(
                db, couple["id"]
            )["timezone"]
            couple_id = couple["id"]

        # A remote model can take seconds. Never keep SQLite's write lock while
        # waiting for it, otherwise the partner cannot chat or confirm plans.
        try:
            parsed_intent = self.life_assistant_provider.parse(
                intent, timezone_name=timezone_name
            )
        except Exception as error:
            self._fail_life_assistant_job(job_id, error)
            raise

        try:
            with self.connection() as db:
                couple, _ = self._couple_context(db, user_id)
                if couple["id"] != couple_id:
                    raise PermissionError("情侣关系已经变化，请重新提交安排")
                existing = db.execute(
                    "SELECT * FROM life_assistant_jobs WHERE id=? AND couple_id=?",
                    (job_id, couple_id),
                ).fetchone()
                if not existing:
                    raise KeyError("生活助理任务不存在")
                if existing["status"] != "running":
                    return self._job_in_connection(db, existing, user_id)
                resolved = self._build_life_plan(
                    db,
                    couple=couple,
                    creator_id=user_id,
                    intent_text=intent,
                    intent=parsed_intent,
                    source=parsed_intent.source,
                )
                plan_id = _id("assistant_plan")
                db.execute(
                    """
                    INSERT INTO life_assistant_plans(
                        id,couple_id,creator_id,title,summary,status,version,source,
                        request_hash,expected_versions_json,expires_at,created_at,updated_at
                    ) VALUES (?,?,?,?,?,'pending_confirmation',1,?,?,?,?,?,?)
                    """,
                    (
                        plan_id,
                        couple["id"],
                        user_id,
                        resolved["title"],
                        resolved["summary"],
                        resolved["source"],
                        input_hash,
                        _json(resolved["expected"]),
                        expires_at,
                        now,
                        now,
                    ),
                )
                for position, item in enumerate(resolved["items"], 1):
                    db.execute(
                        """
                        INSERT INTO life_assistant_plan_items(
                            id,plan_id,item_type,payload_json,preview_json,position
                        ) VALUES (?,?,?,?,?,?)
                        """,
                        (
                            _id("assistant_item"),
                            plan_id,
                            item["item_type"],
                            _json(item["payload"]),
                            _json(item["preview"]),
                            position,
                        ),
                    )
                db.execute(
                    """
                    UPDATE life_assistant_jobs
                    SET status='completed',result_plan_id=?,completed_at=?
                    WHERE id=?
                    """,
                    (plan_id, _now(), job_id),
                )
                actor = db.execute(
                    "SELECT nickname FROM users WHERE id=?", (user_id,)
                ).fetchone()
                for recipient_id in (couple["member_a"], couple["member_b"]):
                    self._insert_in_app_notification(
                        db,
                        couple_id=couple["id"],
                        recipient_id=recipient_id,
                        actor_id=user_id,
                        event_type="assistant.plan_created",
                        resource_type="assistant_plan",
                        resource_id=plan_id,
                        title="新的共同方案",
                        body=f"{actor['nickname']}创建了一份待确认方案",
                        url=f"/?screen=home&assistant-plan={quote(plan_id)}",
                        dedupe_key=f"assistant-plan-created:{plan_id}:v1",
                    )
                self._event(
                    db,
                    couple["id"],
                    user_id,
                    "assistant.plan_updated",
                    plan_id,
                    {"status": "pending_confirmation", "version": 1},
                )
                job = db.execute(
                    "SELECT * FROM life_assistant_jobs WHERE id=?", (job_id,)
                ).fetchone()
                return self._job_in_connection(db, job, user_id)
        except Exception as error:
            self._fail_life_assistant_job(job_id, error)
            raise

    def _job_in_connection(
        self, db: sqlite3.Connection, row: sqlite3.Row, user_id: str
    ) -> dict:
        value = dict(row)
        value["plan"] = None
        if row["result_plan_id"]:
            plan = db.execute(
                "SELECT * FROM life_assistant_plans WHERE id=?",
                (row["result_plan_id"],),
            ).fetchone()
            value["plan"] = self._plan_in_connection(db, plan, user_id)
        return value

    def get_life_assistant_job(self, job_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                """
                SELECT * FROM life_assistant_jobs
                WHERE id=? AND couple_id=? AND creator_id=?
                """,
                (job_id, couple["id"], user_id),
            ).fetchone()
            if not row:
                raise KeyError("生活助理任务不存在")
            return self._job_in_connection(db, row, user_id)

    def update_life_assistant_plan(
        self, plan_id: str, payload: dict, user_id: str
    ) -> dict:
        try:
            expected_version = int(payload.get("expected_version"))
        except (TypeError, ValueError) as error:
            raise ValueError("缺少有效的方案版本") from error
        title = str(payload.get("title") or "").strip()
        summary = str(payload.get("summary") or "").strip()
        if title and len(title) > 120:
            raise ValueError("方案标题过长")
        if summary and len(summary) > 500:
            raise ValueError("方案说明过长")
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT * FROM life_assistant_plans WHERE id=? AND couple_id=?",
                (plan_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("共同方案不存在")
            if row["status"] != "pending_confirmation":
                raise ConflictError("只有待确认方案可以修改")
            if int(row["version"]) != expected_version:
                raise ConflictError("方案已被另一方修改，请刷新后重试")
            if not title and not summary:
                raise ValueError("没有可保存的方案修改")
            updated_title = title or row["title"]
            updated_summary = summary or row["summary"]
            db.execute(
                """
                UPDATE life_assistant_plans
                SET title=?,summary=?,version=version+1,updated_at=?
                WHERE id=? AND version=?
                """,
                (updated_title, updated_summary, now, plan_id, expected_version),
            )
            current = db.execute(
                "SELECT * FROM life_assistant_plans WHERE id=?", (plan_id,)
            ).fetchone()
            actor = db.execute(
                "SELECT nickname FROM users WHERE id=?", (user_id,)
            ).fetchone()
            for recipient_id in (couple["member_a"], couple["member_b"]):
                self._insert_in_app_notification(
                    db,
                    couple_id=couple["id"],
                    recipient_id=recipient_id,
                    actor_id=user_id,
                    event_type="assistant.plan_updated",
                    resource_type="assistant_plan",
                    resource_id=plan_id,
                    title="共同方案有更新",
                    body=f"{actor['nickname']}调整了待确认方案，请重新确认",
                    url=f"/?screen=home&assistant-plan={quote(plan_id)}",
                    dedupe_key=(
                        f"assistant-plan-updated:{plan_id}:v{current['version']}"
                    ),
                )
            self._event(
                db,
                couple["id"],
                user_id,
                "assistant.plan_updated",
                plan_id,
                {
                    "status": current["status"],
                    "version": int(current["version"]),
                },
            )
            return self._plan_in_connection(db, current, user_id)

    def _validate_plan_dependencies(
        self,
        db: sqlite3.Connection,
        *,
        couple: sqlite3.Row,
        expected: dict,
    ) -> None:
        if int(expected.get("couple", 0)) != int(couple["version"]):
            raise ConflictError("情侣空间状态已经变化，请重新生成方案")
        cart = db.execute(
            "SELECT version FROM meal_carts WHERE couple_id=? AND status='active'",
            (couple["id"],),
        ).fetchone()
        if int(expected.get("meal_cart", 0)) != (int(cart["version"]) if cart else 0):
            raise ConflictError("用餐计划已经变化，请重新生成方案")
        shopping = db.execute(
            "SELECT version FROM shopping_lists WHERE couple_id=? AND status='active'",
            (couple["id"],),
        ).fetchone()
        if int(expected.get("shopping_list", 0)) != (
            int(shopping["version"]) if shopping else 0
        ):
            raise ConflictError("采购清单已经变化，请重新生成方案")
        for key, version in expected.items():
            if not key.startswith("recipe:"):
                continue
            recipe = db.execute(
                "SELECT version FROM recipes WHERE id=? AND deleted_at IS NULL",
                (key.split(":", 1)[1],),
            ).fetchone()
            if not recipe or int(recipe["version"]) != int(version):
                raise ConflictError("菜谱已经更新，请重新生成方案")

    def _insert_calendar_plan_item(
        self,
        db: sqlite3.Connection,
        *,
        couple_id: str,
        creator_id: str,
        payload: dict,
    ) -> dict:
        values = self._calendar_values(payload)
        entry_id, now = _id("calendar"), _now()
        db.execute(
            """
            INSERT INTO calendar_entries(
                id,couple_id,creator_id,entry_type,title,starts_at,ends_at,
                all_day,recurrence,location,note,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                entry_id,
                couple_id,
                creator_id,
                *values.values(),
                now,
                now,
            ),
        )
        return dict(
            db.execute(
                "SELECT * FROM calendar_entries WHERE id=?", (entry_id,)
            ).fetchone()
        )

    def _insert_meal_plan_item(
        self,
        db: sqlite3.Connection,
        *,
        couple_id: str,
        creator_id: str,
        payload: dict,
    ) -> dict:
        values = self._validate_meal_values(payload)
        recipe_row = self._get_visible_recipe_row(
            db, payload["recipe_id"], couple_id, creator_id
        )
        if recipe_row["visibility"] == "private":
            raise PermissionError("仅自己可见的菜谱不能加入共同方案")
        if int(recipe_row["version"]) != int(payload["recipe_version"]):
            raise ConflictError("菜谱已经更新，请重新生成方案")
        cart = self._active_cart(db, couple_id, create=True)
        duplicate = db.execute(
            """
            SELECT id FROM meal_cart_items
            WHERE cart_id=? AND recipe_id=? AND planned_date=? AND meal_slot=?
              AND status!='cancelled' LIMIT 1
            """,
            (
                cart["id"],
                recipe_row["id"],
                values["planned_date"],
                values["meal_slot"],
            ),
        ).fetchone()
        if duplicate:
            raise ConflictError("这道菜已经安排在同一餐次，请重新生成方案")
        recipe = self._recipe_row_to_dict(db, recipe_row)
        item_id, now = _id("meal"), _now()
        db.execute(
            """
            INSERT INTO meal_cart_items(
                id,cart_id,couple_id,recipe_id,recipe_snapshot_json,planned_date,
                meal_slot,planned_time,servings,ordered_by,cook_user_id,status,
                course_type,note,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'accepted',?,?,?,?)
            """,
            (
                item_id,
                cart["id"],
                couple_id,
                recipe_row["id"],
                _json(recipe_snapshot(recipe)),
                values["planned_date"],
                values["meal_slot"],
                values["planned_time"],
                values["servings"],
                creator_id,
                creator_id,
                values["course_type"],
                values["note"],
                now,
                now,
            ),
        )
        db.execute(
            "UPDATE meal_carts SET version=version+1 WHERE id=?", (cart["id"],)
        )
        return self._meal_item_dict(
            db.execute(
                "SELECT * FROM meal_cart_items WHERE id=?", (item_id,)
            ).fetchone()
        )

    def _merge_plan_shopping(
        self,
        db: sqlite3.Connection,
        *,
        couple_id: str,
        meal_items: list[dict],
    ) -> dict:
        if not meal_items:
            raise ConflictError("没有可用于采购清单的已确认菜品")
        generated = build_shopping_items(meal_items)
        if not generated:
            raise ConflictError("菜谱缺少可靠食材，不能生成采购清单")
        dates = sorted({item["planned_date"] for item in meal_items})
        shopping = db.execute(
            "SELECT * FROM shopping_lists WHERE couple_id=? AND status='active'",
            (couple_id,),
        ).fetchone()
        now = _now()
        if not shopping:
            cart = self._active_cart(db, couple_id, create=True)
            list_id = _id("shopping")
            db.execute(
                """
                INSERT INTO shopping_lists(
                    id,couple_id,cart_id,title,range_start,range_end,status,created_at
                ) VALUES (?,?,?,?,?,?,'active',?)
                """,
                (
                    list_id,
                    couple_id,
                    cart["id"],
                    "采购清单",
                    dates[0],
                    dates[-1],
                    now,
                ),
            )
            shopping = db.execute(
                "SELECT * FROM shopping_lists WHERE id=?", (list_id,)
            ).fetchone()
        else:
            range_start = min(shopping["range_start"], dates[0])
            range_end = max(shopping["range_end"], dates[-1])
            db.execute(
                """
                UPDATE shopping_lists
                SET range_start=?,range_end=?,version=version+1
                WHERE id=?
                """,
                (range_start, range_end, shopping["id"]),
            )
        existing_rows = db.execute(
            "SELECT * FROM shopping_list_items WHERE shopping_list_id=?",
            (shopping["id"],),
        ).fetchall()
        existing = {
            (row["canonical_name"], row["unit_family"], row["specification"]): row
            for row in existing_rows
        }
        position = max([int(row["position"]) for row in existing_rows] or [0])
        for value in generated:
            key = (
                value["canonical_name"],
                value["unit_family"],
                value["specification"],
            )
            old = existing.get(key)
            if old:
                item_id = old["id"]
                if not old["manual_locked"]:
                    current_quantity = old["quantity_milli"]
                    incoming = value["quantity_milli"]
                    combined = (
                        int(current_quantity) + int(incoming)
                        if current_quantity is not None and incoming is not None
                        else current_quantity or incoming
                    )
                    db.execute(
                        """
                        UPDATE shopping_list_items
                        SET quantity_milli=?,quantity_text=?,version=version+1,
                            updated_at=?
                        WHERE id=?
                        """,
                        (
                            combined,
                            value["quantity_text"],
                            now,
                            item_id,
                        ),
                    )
            else:
                position += 1
                item_id = _id("shopping_item")
                db.execute(
                    """
                    INSERT INTO shopping_list_items(
                        id,shopping_list_id,canonical_name,display_name,
                        quantity_milli,quantity_text,canonical_unit,unit_family,
                        category,specification,position,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        item_id,
                        shopping["id"],
                        value["canonical_name"],
                        value["display_name"],
                        value["quantity_milli"],
                        value["quantity_text"],
                        value["canonical_unit"],
                        value["unit_family"],
                        value["category"],
                        value["specification"],
                        position,
                        now,
                    ),
                )
            for source in value["sources"]:
                db.execute(
                    """
                    INSERT OR IGNORE INTO shopping_item_sources(
                        shopping_item_id,meal_item_id,recipe_ingredient_id,
                        contributed_quantity_milli
                    ) VALUES (?,?,?,?)
                    """,
                    (
                        item_id,
                        source["meal_item_id"],
                        source["recipe_ingredient_id"],
                        source["contributed_quantity_milli"],
                    ),
                )
        for meal in meal_items:
            db.execute(
                """
                UPDATE meal_cart_items
                SET status='shopping',version=version+1,updated_at=?
                WHERE id=? AND status='accepted'
                """,
                (now, meal["id"]),
            )
        return self._shopping_list_in_connection(db, shopping["id"])

    def _execute_life_plan(
        self,
        db: sqlite3.Connection,
        *,
        plan: sqlite3.Row,
        actor_id: str,
    ) -> dict:
        rows = db.execute(
            """
            SELECT * FROM life_assistant_plan_items
            WHERE plan_id=? ORDER BY position,id
            """,
            (plan["id"],),
        ).fetchall()
        created: dict[str, list[dict] | dict | None] = {
            "calendar": [],
            "meals": [],
            "wishes": [],
            "shopping": None,
        }
        for row in rows:
            payload = _parse(row["payload_json"], {})
            if row["item_type"] in {"calendar.create", "todo.create"}:
                created["calendar"].append(
                    self._insert_calendar_plan_item(
                        db,
                        couple_id=plan["couple_id"],
                        creator_id=actor_id,
                        payload=payload,
                    )
                )
            elif row["item_type"] == "meal.wish":
                wish_id = _id("meal_wish")
                db.execute(
                    """
                    INSERT INTO meal_wishes(
                        id,couple_id,creator_id,title,note,created_at
                    ) VALUES (?,?,?,?,?,?)
                    """,
                    (
                        wish_id,
                        plan["couple_id"],
                        actor_id,
                        str(payload["title"])[:100],
                        str(payload.get("note") or "")[:1000],
                        _now(),
                    ),
                )
                created["wishes"].append(
                    dict(
                        db.execute(
                            "SELECT * FROM meal_wishes WHERE id=?", (wish_id,)
                        ).fetchone()
                    )
                )
            elif row["item_type"] == "meal.schedule":
                created["meals"].append(
                    self._insert_meal_plan_item(
                        db,
                        couple_id=plan["couple_id"],
                        creator_id=actor_id,
                        payload=payload,
                    )
                )
            elif row["item_type"] == "shopping.merge":
                created["shopping"] = self._merge_plan_shopping(
                    db,
                    couple_id=plan["couple_id"],
                    meal_items=list(created["meals"]),
                )
            else:
                raise ValueError("共同方案包含不支持的动作")
        return created

    def confirm_life_assistant_plan(
        self, plan_id: str, payload: dict, user_id: str
    ) -> dict:
        try:
            expected_version = int(payload.get("expected_version"))
        except (TypeError, ValueError) as error:
            raise ValueError("缺少有效的方案版本") from error
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9:._-]{8,160}", idempotency_key):
            raise ValueError("幂等键格式无效")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, partner_id = self._couple_context(db, user_id)
            previous = self._idempotent_result(
                db,
                couple["id"],
                user_id,
                f"assistant.plan.confirm:{plan_id}",
                idempotency_key,
            )
            if previous:
                previous["idempotent"] = True
                return previous
            plan = db.execute(
                "SELECT * FROM life_assistant_plans WHERE id=? AND couple_id=?",
                (plan_id, couple["id"]),
            ).fetchone()
            if not plan:
                raise KeyError("共同方案不存在")
            if int(plan["version"]) != expected_version:
                raise ConflictError("方案已被更新，请确认最新版本")
            if plan["status"] == "applied":
                raise ConflictError("方案已经执行")
            if plan["status"] != "pending_confirmation":
                raise ConflictError("方案当前不能确认")
            if plan["expires_at"] <= _now():
                db.execute(
                    "UPDATE life_assistant_plans SET status='expired',updated_at=? WHERE id=?",
                    (_now(), plan_id),
                )
                raise ConflictError("方案已过期，请重新生成")
            expected = _parse(plan["expected_versions_json"], {})
            self._validate_plan_dependencies(
                db, couple=couple, expected=expected
            )
            db.execute(
                """
                INSERT OR IGNORE INTO life_assistant_confirmations(
                    plan_id,plan_version,user_id,confirmed_at
                ) VALUES (?,?,?,?)
                """,
                (plan_id, expected_version, user_id, _now()),
            )
            count = int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM life_assistant_confirmations
                    WHERE plan_id=? AND plan_version=?
                    """,
                    (plan_id, expected_version),
                ).fetchone()[0]
            )
            required = int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM users
                    WHERE id IN (?,?)
                    """,
                    (couple["member_a"], couple["member_b"]),
                ).fetchone()[0]
            )
            if required != 2:
                raise ConflictError("情侣关系成员状态异常，请先修复绑定关系")
            applied = count >= required
            execution_result = None
            if applied:
                execution_result = self._execute_life_plan(
                    db, plan=plan, actor_id=user_id
                )
                now = _now()
                updated = db.execute(
                    """
                    UPDATE life_assistant_plans
                    SET status='applied',result_json=?,applied_at=?,updated_at=?
                    WHERE id=? AND status='pending_confirmation' AND version=?
                    """,
                    (_json(execution_result), now, now, plan_id, expected_version),
                ).rowcount
                if updated != 1:
                    raise ConflictError("方案已被另一方处理，请刷新")
                title, body = "共同方案已生效", "双方已确认，安排已写入"
                event_status = "applied"
            else:
                actor = db.execute(
                    "SELECT nickname FROM users WHERE id=?", (user_id,)
                ).fetchone()
                title, body = "共同方案等待确认", f"{actor['nickname']}已确认，等待你确认"
                event_status = "pending_confirmation"
            recipient_ids = (
                (couple["member_a"], couple["member_b"])
                if applied
                else (partner_id,)
            )
            for recipient_id in recipient_ids:
                self._insert_in_app_notification(
                    db,
                    couple_id=couple["id"],
                    recipient_id=recipient_id,
                    actor_id=user_id,
                    event_type=(
                        "assistant.plan_applied"
                        if applied
                        else "assistant.plan_confirmed"
                    ),
                    resource_type="assistant_plan",
                    resource_id=plan_id,
                    title=title,
                    body=body,
                    url=f"/?screen=home&assistant-plan={quote(plan_id)}",
                    dedupe_key=(
                        f"assistant-plan-{event_status}:{plan_id}:"
                        f"v{expected_version}:{recipient_id}"
                    ),
                )
            self._event(
                db,
                couple["id"],
                user_id,
                "assistant.plan_updated",
                plan_id,
                {"status": event_status, "version": expected_version},
            )
            current = db.execute(
                "SELECT * FROM life_assistant_plans WHERE id=?", (plan_id,)
            ).fetchone()
            result = {
                "applied": applied,
                "confirmation_count": count,
                "confirmation_required": required,
                "plan": self._plan_in_connection(db, current, user_id),
                "result": execution_result,
                "idempotent": False,
            }
            self._save_idempotent_result(
                db,
                couple["id"],
                user_id,
                f"assistant.plan.confirm:{plan_id}",
                idempotency_key,
                result,
            )
            return result

    def cancel_life_assistant_plan(self, plan_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            updated = db.execute(
                """
                UPDATE life_assistant_plans SET status='cancelled',updated_at=?
                WHERE id=? AND couple_id=? AND status='pending_confirmation'
                """,
                (_now(), plan_id, couple["id"]),
            ).rowcount
            if not updated:
                raise ConflictError("方案不存在或当前不能取消")
            self._event(
                db,
                couple["id"],
                user_id,
                "assistant.plan_updated",
                plan_id,
                {"status": "cancelled"},
            )
            row = db.execute(
                "SELECT * FROM life_assistant_plans WHERE id=?", (plan_id,)
            ).fetchone()
            return self._plan_in_connection(db, row, user_id)

    def get_life_assistant_today(self, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            counts = {
                "unread_messages": int(
                    db.execute(
                        """
                        SELECT COUNT(*) FROM messages
                        WHERE couple_id=? AND sender_id<>? AND read_at IS NULL
                        """,
                        (couple["id"], user_id),
                    ).fetchone()[0]
                ),
                "unread_notifications": int(
                    db.execute(
                        """
                        SELECT COUNT(*) FROM in_app_notifications
                        WHERE couple_id=? AND recipient_id=? AND read_at IS NULL
                          AND archived_at IS NULL
                        """,
                        (couple["id"], user_id),
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
                "pending_trips": int(
                    db.execute(
                        """
                        SELECT COUNT(*) FROM trips
                        WHERE couple_id=? AND deleted_at IS NULL AND status!='confirmed'
                        """,
                        (couple["id"],),
                    ).fetchone()[0]
                ),
                "meal_wishes": int(
                    db.execute(
                        """
                        SELECT COUNT(*) FROM meal_wishes
                        WHERE couple_id=? AND deleted_at IS NULL
                        """,
                        (couple["id"],),
                    ).fetchone()[0]
                ),
                "shopping_items": int(
                    db.execute(
                        """
                        SELECT COUNT(*) FROM shopping_list_items i
                        JOIN shopping_lists l ON l.id=i.shopping_list_id
                        WHERE l.couple_id=? AND l.status='active' AND i.checked=0
                        """,
                        (couple["id"],),
                    ).fetchone()[0]
                ),
                "anniversaries": int(
                    db.execute(
                        """
                        SELECT COUNT(*) FROM calendar_entries
                        WHERE couple_id=? AND entry_type='anniversary'
                          AND deleted_at IS NULL
                        """,
                        (couple["id"],),
                    ).fetchone()[0]
                ),
                "todos": int(
                    db.execute(
                        """
                        SELECT COUNT(*) FROM calendar_entries
                        WHERE couple_id=? AND entry_type='todo'
                          AND deleted_at IS NULL AND completed_at IS NULL
                        """,
                        (couple["id"],),
                    ).fetchone()[0]
                ),
            }
            priorities: list[dict] = []
            pending_plan = db.execute(
                """
                SELECT id,title,summary,version,updated_at
                FROM life_assistant_plans
                WHERE couple_id=? AND status='pending_confirmation' AND expires_at>?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (couple["id"], _now()),
            ).fetchone()
            if pending_plan:
                priorities.append(
                    {
                        "type": "assistant_plan",
                        "resource_id": pending_plan["id"],
                        "title": pending_plan["title"],
                        "subtitle": "待双方确认",
                        "url": (
                            f"/?screen=home&assistant-plan="
                            f"{quote(pending_plan['id'])}"
                        ),
                    }
                )
            notification = db.execute(
                """
                SELECT id,title,event_type,resource_type,resource_id,url
                FROM in_app_notifications
                WHERE couple_id=? AND recipient_id=? AND read_at IS NULL
                  AND archived_at IS NULL
                ORDER BY created_at DESC,id DESC LIMIT 1
                """,
                (couple["id"], user_id),
            ).fetchone()
            if notification and len(priorities) < 3:
                priorities.append(
                    {
                        "type": "notification",
                        "resource_id": notification["id"],
                        "title": notification["title"],
                        "subtitle": "未读通知",
                        "url": notification["url"],
                    }
                )
            todo = db.execute(
                """
                SELECT id,title,starts_at FROM calendar_entries
                WHERE couple_id=? AND entry_type='todo' AND deleted_at IS NULL
                  AND completed_at IS NULL
                ORDER BY starts_at,id LIMIT 1
                """,
                (couple["id"],),
            ).fetchone()
            if todo and len(priorities) < 3:
                priorities.append(
                    {
                        "type": "todo",
                        "resource_id": todo["id"],
                        "title": todo["title"],
                        "subtitle": f"计划时间 {todo['starts_at'][:16]}",
                        "url": "/?screen=life&section=todo",
                    }
                )
        return {
            "counts": counts,
            "priorities": priorities,
            "generated_at": _now(),
        }
