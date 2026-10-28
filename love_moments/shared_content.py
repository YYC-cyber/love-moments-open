"""Shared detail and comment threads for the couple's real domain records."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .intelligent_ui import ConflictError


RESOURCE_TYPES = frozenset({
    "moment", "trip", "itinerary_item", "meal_wish", "meal_item",
    "wishlist_item", "calendar_entry", "preference", "date_plan",
})


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def _field(label: str, value: object) -> dict[str, str] | None:
    text = str(value or "").strip()
    return {"label": label, "value": text} if text else None


class SharedContentMixin:
    """Resolve only records that genuinely belong to the current couple."""

    @staticmethod
    def _public_comment(row: sqlite3.Row, current_user_id: str) -> dict:
        value = dict(row)
        value["mine"] = value["author_id"] == current_user_id
        return value

    def _shared_resource(
        self,
        db: sqlite3.Connection,
        couple_id: str,
        resource_type: str,
        resource_id: str,
    ) -> dict:
        if resource_type not in RESOURCE_TYPES:
            raise ValueError("不支持查看这种共同内容")

        if resource_type == "moment":
            row = db.execute(
                """SELECT m.*,u.nickname AS creator_name FROM moments m
                JOIN users u ON u.id=m.creator_id
                WHERE m.id=? AND m.couple_id=? AND m.deleted_at IS NULL AND m.purged_at IS NULL
                  AND (m.visibility='shared' OR (m.visibility='delayed' AND m.reveal_at<=?))""",
                (resource_id, couple_id, _now()),
            ).fetchone()
            if row:
                fields = [
                    _field("记录人", row["creator_name"]), _field("发生时间", row["occurred_at"]),
                    _field("地点", row["location"]), _field("心情", row["mood"]),
                ]
                return {
                    "resource_type": resource_type, "resource_id": resource_id,
                    "kind_label": "恋爱瞬间", "title": row["title"], "summary": row["body"],
                    "fields": [item for item in fields if item], "version": row["version"],
                    "media_url": row["media_url"] or "",
                }

        elif resource_type == "trip":
            row = db.execute(
                "SELECT * FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL",
                (resource_id, couple_id),
            ).fetchone()
            if row:
                status = {"planning": "规划中", "pending_approval": "等待双方确认", "confirmed": "双方已确认"}.get(row["status"], row["status"])
                fields = [
                    _field("目的地", row["destination"]), _field("日期", f"{row['start_date']} 至 {row['end_date']}"),
                    _field("预算", f"¥{int(row['budget_cents']) / 100:,.0f}"), _field("状态", status),
                ]
                return {
                    "resource_type": resource_type, "resource_id": resource_id, "kind_label": "共同旅行",
                    "title": row["title"], "summary": f"从 {row['departure_city'] or '待确认'} 出发，{row['travelers']} 人同行。",
                    "fields": [item for item in fields if item], "version": row["version"],
                }

        elif resource_type == "itinerary_item":
            row = db.execute(
                """SELECT i.*,d.day_number,d.date,d.title AS day_title,v.name AS variant_name,t.title AS trip_title
                FROM itinerary_items i JOIN trip_days d ON d.id=i.day_id
                JOIN plan_variants v ON v.id=d.variant_id JOIN trips t ON t.id=v.trip_id
                WHERE i.id=? AND t.couple_id=? AND t.deleted_at IS NULL""",
                (resource_id, couple_id),
            ).fetchone()
            if row:
                details = _json(row["details_json"], {})
                reason = str(details.get("recommendation_reason") or "").strip()
                fields = [
                    _field("旅行", row["trip_title"]), _field("方案", row["variant_name"]),
                    _field("日期", f"第 {row['day_number']} 天 · {row['date']}"),
                    _field("时间", f"{row['start_time']} · 建议 {row['duration_minutes']} 分钟"),
                    _field("推荐原因", reason),
                ]
                return {
                    "resource_type": resource_type, "resource_id": resource_id, "kind_label": "共同旅行行程",
                    "title": row["title"], "summary": row["note"],
                    "fields": [item for item in fields if item], "version": row["version"],
                }

        elif resource_type == "meal_wish":
            row = db.execute(
                """SELECT w.*,u.nickname AS creator_name,r.name AS recipe_name FROM meal_wishes w
                JOIN users u ON u.id=w.creator_id LEFT JOIN recipes r ON r.id=w.recipe_id
                WHERE w.id=? AND w.couple_id=? AND w.deleted_at IS NULL""",
                (resource_id, couple_id),
            ).fetchone()
            if row:
                fields = [
                    _field("点菜人", row["creator_name"]), _field("具体做法", row["recipe_name"] or "尚未选择菜谱"),
                    _field("加入时间", row["created_at"]),
                ]
                return {
                    "resource_type": resource_type, "resource_id": resource_id, "kind_label": "想吃清单",
                    "title": row["title"], "summary": row["note"],
                    "fields": [item for item in fields if item], "version": row["version"],
                    "recipe_id": row["recipe_id"] or "",
                }

        elif resource_type == "meal_item":
            row = db.execute(
                """SELECT m.*,u.nickname AS ordered_by_name FROM meal_cart_items m
                JOIN users u ON u.id=m.ordered_by WHERE m.id=? AND m.couple_id=?""",
                (resource_id, couple_id),
            ).fetchone()
            if row:
                recipe = _json(row["recipe_snapshot_json"], {})
                name = str(recipe.get("name") or "共同菜单")
                fields = [
                    _field("安排人", row["ordered_by_name"]), _field("日期", row["planned_date"]),
                    _field("餐次", row["meal_slot"]), _field("时间", row["planned_time"]),
                    _field("人数", f"{row['servings']} 人份"), _field("状态", row["status"]),
                ]
                return {
                    "resource_type": resource_type, "resource_id": resource_id, "kind_label": "共同菜单",
                    "title": name, "summary": row["note"], "fields": [item for item in fields if item],
                    "version": row["version"], "recipe_id": row["recipe_id"] or "",
                }

        elif resource_type == "wishlist_item":
            row = db.execute(
                """SELECT i.*,u.nickname AS creator_name,l.title AS list_title FROM shared_list_items i
                JOIN shared_lists l ON l.id=i.list_id JOIN users u ON u.id=i.creator_id
                WHERE i.id=? AND l.couple_id=?""",
                (resource_id, couple_id),
            ).fetchone()
            if row:
                fields = [
                    _field("记录人", row["creator_name"]),
                    _field("状态", "已完成" if row["completed_at"] else "待完成"),
                    _field("完成时间", row["completed_at"]),
                ]
                return {
                    "resource_type": resource_type, "resource_id": resource_id, "kind_label": row["list_title"],
                    "title": row["text"], "summary": row["note"], "fields": [item for item in fields if item],
                    "version": row["version"],
                }

        elif resource_type == "calendar_entry":
            row = db.execute(
                """SELECT c.*,u.nickname AS creator_name FROM calendar_entries c JOIN users u ON u.id=c.creator_id
                WHERE c.id=? AND c.couple_id=? AND c.deleted_at IS NULL""",
                (resource_id, couple_id),
            ).fetchone()
            if row:
                label = {"event": "共同日历", "anniversary": "纪念日", "todo": "双人待办"}.get(row["entry_type"], "共同事项")
                fields = [
                    _field("记录人", row["creator_name"]), _field("日期", row["starts_at"]),
                    _field("结束", row["ends_at"]), _field("地点", row["location"]),
                    _field("重复", row["recurrence"] if row["recurrence"] != "none" else "不重复"),
                    _field("状态", "已完成" if row["completed_at"] else "待完成" if row["entry_type"] == "todo" else "已安排"),
                ]
                return {
                    "resource_type": resource_type, "resource_id": resource_id, "kind_label": label,
                    "title": row["title"], "summary": row["note"], "fields": [item for item in fields if item],
                    "version": row["version"],
                }

        elif resource_type == "preference":
            row = db.execute(
                """SELECT p.*,owner.nickname AS creator_name,subject.nickname AS subject_name
                FROM partner_preferences p JOIN users owner ON owner.id=p.creator_id
                JOIN users subject ON subject.id=p.subject_user_id
                WHERE p.id=? AND p.couple_id=? AND p.deleted_at IS NULL""",
                (resource_id, couple_id),
            ).fetchone()
            if row:
                fields = [
                    _field("关于", row["subject_name"]), _field("分类", row["category"]),
                    _field("内容", row["value"]), _field("记录人", row["creator_name"]),
                ]
                return {
                    "resource_type": resource_type, "resource_id": resource_id, "kind_label": "对方偏好",
                    "title": row["label"], "summary": row["note"], "fields": [item for item in fields if item],
                    "version": row["version"],
                }

        elif resource_type == "date_plan":
            row = db.execute(
                "SELECT * FROM date_plans WHERE id=? AND couple_id=?",
                (resource_id, couple_id),
            ).fetchone()
            if row:
                fields = [
                    _field("日期", row["plan_date"]), _field("城市", row["city"]),
                    _field("预算", f"¥{int(row['budget_cents']) / 100:,.0f}"), _field("状态", row["status"]),
                ]
                return {
                    "resource_type": resource_type, "resource_id": resource_id, "kind_label": "共同约会",
                    "title": row["title"], "summary": "双方可以在这里讨论方案和当天安排。",
                    "fields": [item for item in fields if item], "version": row["version"],
                }

        raise KeyError("这条共同内容不存在或当前不可见")

    def get_shared_content(self, resource_type: str, resource_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            resource = self._shared_resource(db, couple["id"], resource_type, resource_id)
            rows = db.execute(
                """SELECT c.*,u.nickname AS author_name,u.avatar_label FROM shared_comments c
                JOIN users u ON u.id=c.author_id
                WHERE c.couple_id=? AND c.resource_type=? AND c.resource_id=? AND c.deleted_at IS NULL
                ORDER BY c.created_at,c.rowid""",
                (couple["id"], resource_type, resource_id),
            ).fetchall()
            resource["comments"] = [self._public_comment(row, user_id) for row in rows]
            resource["comment_count"] = len(rows)
            return resource

    def create_shared_comment(self, resource_type: str, resource_id: str, body: object, user_id: str) -> dict:
        text = str(body or "").strip()
        if not text:
            raise ValueError("评论不能为空")
        if len(text) > 1000:
            raise ValueError("评论不能超过 1000 个字")
        now = _now()
        comment_id = _identifier("comment")
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            self._shared_resource(db, couple["id"], resource_type, resource_id)
            db.execute(
                """INSERT INTO shared_comments(
                    id,couple_id,resource_type,resource_id,author_id,body,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?)""",
                (comment_id, couple["id"], resource_type, resource_id, user_id, text, now, now),
            )
            self._audit(db, "shared_comment.created", resource_type, resource_id, {"comment_id": comment_id}, actor_id=user_id)
            actor = db.execute(
                "SELECT nickname FROM users WHERE id=?", (user_id,)
            ).fetchone()
            self._insert_in_app_notification(
                db,
                couple_id=couple["id"],
                recipient_id=partner_id,
                actor_id=user_id,
                event_type="shared_comment.created",
                resource_type="shared_comment",
                resource_id=comment_id,
                title="共同内容有新留言",
                body=f"{actor['nickname']}在共同内容中留下了评论",
                url=(
                    f"/?screen=life&shared_type={resource_type}"
                    f"&shared_id={resource_id}"
                ),
                dedupe_key=f"shared-comment:{comment_id}",
            )
            self._event(db, couple["id"], user_id, "shared_comment.created", resource_id, {"resource_type": resource_type, "comment_id": comment_id})
            row = db.execute(
                """SELECT c.*,u.nickname AS author_name,u.avatar_label FROM shared_comments c
                JOIN users u ON u.id=c.author_id WHERE c.id=?""",
                (comment_id,),
            ).fetchone()
            return self._public_comment(row, user_id)

    def delete_shared_comment(self, comment_id: str, expected_version: object, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT * FROM shared_comments WHERE id=? AND couple_id=? AND deleted_at IS NULL",
                (comment_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("评论不存在")
            if row["author_id"] != user_id:
                raise PermissionError("只能删除自己写的评论")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("评论已更新，请刷新后重试")
            db.execute(
                "UPDATE shared_comments SET deleted_at=?,updated_at=?,version=version+1 WHERE id=?",
                (_now(), _now(), comment_id),
            )
            self._audit(db, "shared_comment.deleted", row["resource_type"], row["resource_id"], {"comment_id": comment_id}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "shared_comment.deleted", row["resource_id"], {"resource_type": row["resource_type"], "comment_id": comment_id})
            return {"id": comment_id, "deleted": True}
