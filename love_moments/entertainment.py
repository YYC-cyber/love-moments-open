"""Shared, consent-aware entertainment tools for a couple."""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone

from .intelligent_ui import ConflictError


WHEEL_PRESETS = {
    "normal": (
        "一起选今晚吃什么", "由你选一部电影", "出门散步二十分钟", "一起拍一张合照",
        "给对方一个认真拥抱", "一起准备一份小零食", "分享今天最开心的事", "计划下一次约会",
    ),
    "intimate": (
        "拥抱对方三十秒", "亲亲对方的额头", "轮流说一句心动的话", "给对方做五分钟肩颈按摩",
        "一起跳一支慢舞", "靠在一起听一首歌", "说出最喜欢对方的一个瞬间", "由对方选择今晚的浪漫活动",
    ),
    "wild": (
        "对视一分钟，先笑的人说一句情话", "模仿第一次约会时的自己", "蒙眼猜出对方递来的三样小物",
        "互换身份演五分钟日常", "认真回答一个一直不敢问的问题", "让对方设计一张大胆但舒服的约会任务卡",
        "用三个动作演出最心动的回忆", "在双方都舒服的范围内完成一次亲密挑战",
    ),
}

MODE_LABELS = {"normal": "普通版", "intimate": "情趣版", "wild": "变态版"}
UNSAFE_PHRASES = (
    "强迫", "不能拒绝", "不许拒绝", "必须服从", "灌醉", "偷拍", "偷录", "窒息", "伤害", "未成年",
)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class EntertainmentMixin:
    @staticmethod
    def _wheel_mode(value: object) -> str:
        mode = str(value or "normal").strip().lower()
        if mode not in WHEEL_PRESETS:
            raise ValueError("转盘模式无效")
        return mode

    @staticmethod
    def _wheel_label(value: object) -> str:
        label = " ".join(str(value or "").split())
        if not 1 <= len(label) <= 40:
            raise ValueError("转盘选项需为 1–40 个字符")
        if any(phrase in label for phrase in UNSAFE_PHRASES):
            raise ValueError("亲密互动必须允许双方随时拒绝或跳过")
        return label

    @staticmethod
    def _preset_options(mode: str) -> list[dict]:
        return [
            {"id": f"preset:{mode}:{index}", "label": label, "mode": mode, "preset": True, "version": 1}
            for index, label in enumerate(WHEEL_PRESETS[mode], 1)
        ]

    def _wheel_custom_options(self, db, couple_id: str, mode: str) -> list[dict]:
        rows = db.execute(
            "SELECT i.* FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id "
            "WHERE l.couple_id=? AND l.type=? AND i.deleted_at IS NULL ORDER BY i.position,i.created_at",
            (couple_id, f"wheel_{mode}"),
        ).fetchall()
        return [{
            "id": row["id"], "label": row["text"], "mode": mode, "preset": False,
            "creator_id": row["creator_id"], "version": int(row["version"]),
        } for row in rows]

    def get_couple_wheel(self, mode: object = "normal", user_id: str = "") -> dict:
        mode_value = self._wheel_mode(mode)
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            options = self._preset_options(mode_value) + self._wheel_custom_options(db, couple["id"], mode_value)
            rows = db.execute(
                "SELECT a.id,a.target_id,a.actor_id,a.detail,a.created_at,u.nickname FROM audit_events a "
                "LEFT JOIN users u ON u.id=a.actor_id WHERE a.couple_id=? AND a.action='wheel.spun' "
                "AND a.target_type='couple_wheel' ORDER BY a.created_at DESC LIMIT 20",
                (couple["id"],),
            ).fetchall()
            history = []
            for row in rows:
                try:
                    detail = json.loads(row["detail"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                if detail.get("mode") != mode_value:
                    continue
                history.append({
                    "id": row["target_id"], "mode": mode_value, "label": str(detail.get("label") or ""),
                    "option_id": str(detail.get("option_id") or ""), "spun_by": row["actor_id"],
                    "spun_by_name": row["nickname"] or "伴侣", "spun_at": row["created_at"],
                })
                if len(history) >= 8:
                    break
        return {
            "mode": mode_value, "mode_label": MODE_LABELS[mode_value], "options": options, "history": history,
            "consent_notice": (
                "仅限双方成年、自愿参与；任何一方都可以随时跳过或停止。"
                if mode_value in {"intimate", "wild"}
                else "转到不合适的结果时可以直接跳过，再转一次。"
            ),
        }

    def create_wheel_option(self, payload: dict, user_id: str) -> dict:
        mode = self._wheel_mode(payload.get("mode")); label = self._wheel_label(payload.get("label"))
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            list_type = f"wheel_{mode}"
            shared = db.execute("SELECT id FROM shared_lists WHERE couple_id=? AND type=?", (couple["id"], list_type)).fetchone()
            list_id = shared["id"] if shared else _id("list")
            if not shared:
                db.execute("INSERT INTO shared_lists(id,couple_id,type,title) VALUES (?,?,?,?)", (list_id, couple["id"], list_type, f"双人转盘·{MODE_LABELS[mode]}"))
            duplicate = db.execute(
                "SELECT 1 FROM shared_list_items WHERE list_id=? AND deleted_at IS NULL AND text=?",
                (list_id, label),
            ).fetchone()
            if duplicate or label in WHEEL_PRESETS[mode]:
                raise ValueError("这个转盘选项已经存在")
            position = int(db.execute("SELECT COALESCE(MAX(position),0)+1 FROM shared_list_items WHERE list_id=?", (list_id,)).fetchone()[0])
            option_id = _id("wheel_option")
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            db.execute(
                "INSERT INTO shared_list_items(id,list_id,creator_id,text,note,position,item_kind,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (option_id, list_id, user_id, label, mode, position, "general", now, now),
            )
            self._audit(db, "wheel.option_created", "couple_wheel", option_id, {"mode": mode}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "play.updated", option_id, {"resource": "wheel", "mode": mode})
        return {"id": option_id, "label": label, "mode": mode, "preset": False, "creator_id": user_id, "version": 1}

    def update_wheel_option(self, option_id: str, payload: dict, user_id: str) -> dict:
        label = self._wheel_label(payload.get("label")); expected = int(payload.get("expected_version") or 0)
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT i.*,l.type FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id "
                "WHERE i.id=? AND l.couple_id=? AND l.type IN ('wheel_normal','wheel_intimate','wheel_wild') AND i.deleted_at IS NULL",
                (option_id, couple["id"]),
            ).fetchone()
            if not row: raise KeyError("转盘选项不存在")
            if int(row["version"]) != expected:
                raise ConflictError("转盘选项已经变化，请刷新后重试")
            db.execute("UPDATE shared_list_items SET text=?,version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=?", (label, option_id))
            mode = str(row["type"]).removeprefix("wheel_")
            self._audit(db, "wheel.option_updated", "couple_wheel", option_id, {"mode": mode}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "play.updated", option_id, {"resource": "wheel", "mode": mode})
            updated = db.execute("SELECT * FROM shared_list_items WHERE id=?", (option_id,)).fetchone()
        return {"id": option_id, "label": updated["text"], "mode": mode, "preset": False, "creator_id": updated["creator_id"], "version": int(updated["version"])}

    def delete_wheel_option(self, option_id: str, expected_version: object, user_id: str) -> dict:
        expected = int(expected_version or 0)
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT i.*,l.type FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id "
                "WHERE i.id=? AND l.couple_id=? AND l.type IN ('wheel_normal','wheel_intimate','wheel_wild') AND i.deleted_at IS NULL",
                (option_id, couple["id"]),
            ).fetchone()
            if not row: raise KeyError("转盘选项不存在")
            if int(row["version"]) != expected:
                raise ConflictError("转盘选项已经变化，请刷新后重试")
            mode = str(row["type"]).removeprefix("wheel_")
            db.execute("UPDATE shared_list_items SET deleted_at=CURRENT_TIMESTAMP,version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=?", (option_id,))
            self._audit(db, "wheel.option_deleted", "couple_wheel", option_id, {"mode": mode}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "play.updated", option_id, {"resource": "wheel", "mode": mode})
        return {"id": option_id, "deleted": True, "mode": mode}

    def spin_couple_wheel(self, payload: dict, user_id: str) -> dict:
        mode = self._wheel_mode(payload.get("mode")); requested = [str(value) for value in payload.get("option_ids", []) if str(value)]
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            options = self._preset_options(mode) + self._wheel_custom_options(db, couple["id"], mode)
            available = {item["id"]: item for item in options}
            selected = [available[value] for value in dict.fromkeys(requested) if value in available] if requested else options
            if len(selected) < 2: raise ValueError("至少选择两个转盘选项")
            result = secrets.choice(selected); result_id = _id("wheel_result")
            self._audit(db, "wheel.spun", "couple_wheel", result_id, {"mode": mode, "option_id": result["id"], "label": result["label"]}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "play.updated", result_id, {"resource": "wheel", "mode": mode, "result": result["label"]})
            actor = db.execute("SELECT nickname FROM users WHERE id=?", (user_id,)).fetchone()
            row = db.execute("SELECT created_at FROM audit_events WHERE target_id=?", (result_id,)).fetchone()
        return {"id": result_id, "mode": mode, "option_id": result["id"], "label": result["label"], "spun_by": user_id, "spun_by_name": actor["nickname"] if actor else "伴侣", "spun_at": row["created_at"]}
