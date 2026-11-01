"""Shared, consent-aware entertainment tools for a couple."""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .intelligent_ui import ConflictError
from .couple_card_pdf import render_couple_card_pdf


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


CARD_TEMPLATES = (
    ("wish", "愿望卡", "写下一个想和对方一起实现的小愿望", "把小小心愿交给我们，一起慢慢实现。"),
    ("reconcile", "和好卡", "邀请对方找一个合适的时间好好聊聊", "我更想抱抱你，也想好好听你说。"),
    ("kiss", "亲亲卡", "送出一次温柔的亲亲邀请", "把今天最甜的一颗糖，换成你的亲亲。"),
    ("hug", "抱抱卡", "兑换一个认真又温暖的拥抱", "烦恼先放在门外，来我的怀里歇一会儿。"),
    ("date", "约会卡", "约定一次只属于两个人的约会", "这一天不赶时间，只和喜欢的人浪费浪漫。"),
    ("company", "陪伴卡", "在需要的时候留一段专心陪伴的时间", "不用做什么大事，安静待在一起就很好。"),
    ("massage", "按摩卡", "兑换一次放松的肩颈或手部按摩", "辛苦啦，今天让我照顾一下疲惫的你。"),
    ("chore_swap", "家务交换卡", "交换或认领一次家务", "今天把一件麻烦事交给我，你去轻松一下。"),
    ("cooking", "做饭卡", "为对方认真做一顿饭", "想把锅里的香气，也变成爱你的证据。"),
    ("breakfast", "早餐卡", "准备一次喜欢的早餐", "从第一口早餐开始，把今天过得甜甜的。"),
    ("movie", "电影卡", "由持卡人选择一部一起看的电影", "灯光暗下来，手要偷偷牵在一起。"),
    ("walk", "散步卡", "一起散步并聊聊最近的生活", "陪你慢慢走，把最近的心事都说给晚风听。"),
    ("travel", "旅行心愿卡", "收藏一个以后想一起到达的地方", "地图上的下一颗星，等我们一起点亮。"),
    ("surprise", "惊喜卡", "准备一份尊重边界的小惊喜", "藏一点可爱心思，只为看见你亮起来的眼睛。"),
    ("compliment", "夸夸卡", "认真说出三个欣赏对方的地方", "喜欢你的理由很多，今天认真说三个给你听。"),
    ("listen", "倾听卡", "留出一段不打断、不急着评判的倾听时间", "你慢慢说，我会把每一句都放在心上。"),
    ("comfort", "情绪照顾卡", "先询问对方此刻更需要陪伴、空间还是帮助", "今天不开心也没关系，我会温柔地陪着你。"),
    ("game", "游戏卡", "一起玩一局双方都喜欢的游戏", "输赢不重要，和你一起笑才是隐藏奖励。"),
    ("anniversary", "纪念日卡", "为一个重要日子预留共同时间", "把值得记住的日子，认真留给彼此。"),
    ("choice", "选择权卡", "在约定范围内由持卡人做一次选择", "这一回听你的，看看你想把快乐带去哪里。"),
    ("goodnight", "晚安卡", "兑换一次认真道晚安或睡前通话", "把最后一句晚安留给你，梦里也要见面。"),
    ("goodmorning", "早安卡", "送出一次特别的早安问候", "新的一天先想你一下，再去拥抱世界。"),
    ("photo", "合照卡", "一起拍一张想留下来的合照", "把这一刻拍下来，留给很久以后的我们。"),
    ("promise", "兑现承诺卡", "把一个已经说好的小承诺写清并完成", "说过的小约定，我想认真替你实现。"),
    ("custom", "自定义卡", "自己填写卡名、内容和使用说明", "这张留白卡，只装得下属于我们的特别心意。"),
)

CARD_STYLES = (
    ("wish", "Kitty 心愿地图", "girl", "#fff1f5", "#d83e72", "#3d2530", "wish_map", 0),
    ("reconcile", "Kitty 和好信笺", "neutral", "#fff8ef", "#b86b54", "#49332e", "reconcile_letter", 1),
    ("kiss", "Kitty 亲亲草莓", "girl", "#fff0f2", "#ed3e68", "#502630", "kiss_strawberry", 2),
    ("hug", "Kitty 抱抱云朵", "girl", "#fdf1ff", "#b45acb", "#402a48", "hug_cloud", 3),
    ("date", "Kitty 约会日历", "girl", "#ffeef5", "#c62f68", "#472731", "date_calendar", 4),
    ("company", "Kitty 陪伴月光", "boy", "#eef1ff", "#6673bd", "#293052", "company_moon", 5),
    ("massage", "Kitty 温柔花园", "girl", "#fff4f7", "#c8557b", "#4a3038", "massage_garden", 6),
    ("chore_swap", "Kitty 家务格纹", "boy", "#eef8ff", "#3478a7", "#233744", "chore_plaid", 7),
    ("cooking", "Kitty 甜心厨房", "girl", "#fff3ec", "#e66f72", "#4b2e2b", "cooking_kitchen", 8),
    ("breakfast", "Kitty 早餐奶油", "neutral", "#fff9e9", "#d79554", "#4a3927", "breakfast_sunrise", 9),
    ("movie", "Kitty 电影夜", "boy", "#20273d", "#91b8ff", "#f7f8ff", "movie_cinema", 10),
    ("walk", "Kitty 散步晴空", "boy", "#eff9ff", "#1e86ad", "#1f3d4a", "walk_sky", 11),
    ("travel", "Kitty 旅行行李", "boy", "#edf4ff", "#4b75ae", "#25384f", "travel_luggage", 12),
    ("surprise", "Kitty 惊喜礼盒", "girl", "#fff0f6", "#d13f72", "#482731", "surprise_gift", 13),
    ("compliment", "Kitty 夸夸星光", "girl", "#f7f0ff", "#8657bd", "#332b43", "compliment_starlight", 14),
    ("listen", "Kitty 倾听电台", "boy", "#edf8f8", "#378a91", "#263f42", "listen_radio", 15),
    ("comfort", "Kitty 安慰抱枕", "girl", "#fff0f2", "#d55a74", "#4b2b32", "comfort_pillow", 16),
    ("game", "Kitty 游戏派对", "boy", "#eef0ff", "#596fd1", "#293052", "game_board", 17),
    ("anniversary", "Kitty 纪念日玫瑰", "girl", "#f9e9ec", "#9f294d", "#3f252c", "anniversary_rose", 18),
    ("choice", "Kitty 选择券", "neutral", "#fff4e6", "#a76d3e", "#493426", "choice_ticket", 19),
    ("goodnight", "Kitty 晚安星月", "boy", "#20263d", "#8295dc", "#f7f8ff", "goodnight_moon", 20),
    ("goodmorning", "Kitty 早安彩虹", "neutral", "#fff9f6", "#d45f78", "#443336", "goodmorning_rainbow", 21),
    ("photo", "Kitty 合照相机", "girl", "#fff4f1", "#cf5672", "#462e33", "photo_frame", 22),
    ("promise", "Kitty 承诺印章", "neutral", "#f3f5f2", "#657b68", "#304034", "promise_seal", 23),
)

CARD_TEMPLATE_MAP = {item[0]: item for item in CARD_TEMPLATES}
CARD_STYLE_MAP = {item[0]: item for item in CARD_STYLES}


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

    @staticmethod
    def _card_catalog() -> dict:
        return {
            "templates": [
                {"id": key, "name": name, "prompt": prompt, "notice": notice}
                for key, name, prompt, notice in CARD_TEMPLATES
            ],
            "styles": [
                {"id": key, "name": name, "family": family, "paper": paper, "accent": accent, "ink": ink, "pattern": pattern, "sticker_index": sticker_index}
                for key, name, family, paper, accent, ink, pattern, sticker_index in CARD_STYLES
            ],
        }

    @staticmethod
    def _card_text(value: object, *, minimum: int = 1, maximum: int = 500, field: str = "卡片内容") -> str:
        text = " ".join(str(value or "").split())
        if not minimum <= len(text) <= maximum:
            raise ValueError(f"{field}需为 {minimum}–{maximum} 个字符")
        if any(phrase in text for phrase in UNSAFE_PHRASES):
            raise ValueError("卡片内容必须尊重双方同意、安全和隐私")
        return text

    @staticmethod
    def _card_note(row) -> dict:
        try:
            value = json.loads(row["note"] or "{}")
        except (TypeError, json.JSONDecodeError):
            value = {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _card_signature(value: object) -> list[list[list[float]]]:
        if not isinstance(value, list) or not value or len(value) > 20:
            raise ValueError("请在签名板上完成手写签名")
        output: list[list[list[float]]] = []
        point_count = 0
        for stroke in value:
            if not isinstance(stroke, list) or not stroke or len(stroke) > 240:
                raise ValueError("签名笔画无效")
            clean: list[list[float]] = []
            for point in stroke:
                if not isinstance(point, list) or len(point) != 2:
                    raise ValueError("签名坐标无效")
                try:
                    x, y = round(float(point[0]), 4), round(float(point[1]), 4)
                except (TypeError, ValueError) as error:
                    raise ValueError("签名坐标无效") from error
                if not 0 <= x <= 1 or not 0 <= y <= 1:
                    raise ValueError("签名坐标超出范围")
                clean.append([x, y]); point_count += 1
                if point_count > 1200: raise ValueError("签名笔画过多，请清空后重新签名")
            output.append(clean)
        if point_count < 3: raise ValueError("签名笔画太短，请重新签名")
        return output

    def _card_dict(self, row, names: dict[str, str], user_id: str) -> dict:
        note = self._card_note(row)
        template = CARD_TEMPLATE_MAP.get(str(note.get("template_id") or "custom"), CARD_TEMPLATE_MAP["custom"])
        style = CARD_STYLE_MAP.get(str(note.get("style_id") or "wish"), CARD_STYLE_MAP["wish"])
        creator_id = str(row["creator_id"])
        recipient_id = str(note.get("recipient_id") or "")
        status = str(note.get("status") or "awaiting_recipient")
        return {
            "id": str(row["id"]), "title": str(row["text"]), "message": str(note.get("message") or template[2]),
            "template_id": template[0], "template_name": template[1], "template_notice": template[3],
            "style_id": style[0], "style_name": style[1], "style_family": style[2],
            "style": {"id": style[0], "paper": style[3], "accent": style[4], "ink": style[5], "pattern": style[6], "sticker_index": style[7]},
            "creator_id": creator_id, "creator_name": names.get(creator_id, "伴侣"),
            "recipient_id": recipient_id, "recipient_name": names.get(recipient_id, "伴侣"),
            "status": status,
            "creator_signature_name": str(note.get("creator_signature_name") or ""),
            "creator_signature": note.get("creator_signature") if isinstance(note.get("creator_signature"), list) else [],
            "recipient_signature_name": str(note.get("recipient_signature_name") or ""),
            "recipient_signature": note.get("recipient_signature") if isinstance(note.get("recipient_signature"), list) else [],
            "created_at": str(note.get("created_at") or row["created_at"] or ""),
            "signed_at": str(note.get("signed_at") or ""), "version": int(row["version"]),
            "can_sign": status == "awaiting_recipient" and recipient_id == user_id,
            "can_download": status == "signed", "mine": creator_id == user_id,
        }

    def list_couple_cards(self, user_id: str) -> dict:
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            users = db.execute(
                "SELECT id,nickname FROM users WHERE id IN (?,?)",
                (user_id, partner_id),
            ).fetchall()
            names = {str(row["id"]): str(row["nickname"]) for row in users}
            rows = db.execute(
                "SELECT i.* FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id "
                "WHERE l.couple_id=? AND l.type='couple_cards' AND i.deleted_at IS NULL "
                "ORDER BY i.created_at DESC,i.position DESC LIMIT 100",
                (couple["id"],),
            ).fetchall()
            cards = [self._card_dict(row, names, user_id) for row in rows]
        return {**self._card_catalog(), "cards": cards, "pending_for_me": sum(1 for item in cards if item["can_sign"])}

    def create_couple_card(self, payload: dict, user_id: str) -> dict:
        template_id = str(payload.get("template_id") or "").strip()
        style_id = str(payload.get("style_id") or "").strip()
        if template_id not in CARD_TEMPLATE_MAP: raise ValueError("卡片类型无效")
        if style_id not in CARD_STYLE_MAP: raise ValueError("卡片样式无效")
        template = CARD_TEMPLATE_MAP[template_id]
        title = self._card_text(
            payload.get("title") if template_id == "custom" else template[1], maximum=30, field="卡片标题",
        )
        message = self._card_text(payload.get("message") or template[2], maximum=500)
        if payload.get("confirmed") is not True: raise ValueError("请先确认卡片内容并完成发卡人签名")
        creator_signature_name = self._card_text(payload.get("signature_name"), maximum=20, field="发卡人签名")
        creator_signature = self._card_signature(payload.get("signature"))
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            shared = db.execute(
                "SELECT id FROM shared_lists WHERE couple_id=? AND type='couple_cards'", (couple["id"],),
            ).fetchone()
            list_id = str(shared["id"]) if shared else _id("list")
            if not shared:
                db.execute(
                    "INSERT INTO shared_lists(id,couple_id,type,title) VALUES (?,?,?,?)",
                    (list_id, couple["id"], "couple_cards", "双人约定卡"),
                )
            position = int(db.execute(
                "SELECT COALESCE(MAX(position),0)+1 FROM shared_list_items WHERE list_id=?", (list_id,),
            ).fetchone()[0])
            card_id, now = _id("couple_card"), datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            note = {
                "schema_version": 1, "template_id": template_id, "style_id": style_id,
                "message": message, "recipient_id": partner_id, "status": "awaiting_recipient", "created_at": now,
                "creator_signature_name": creator_signature_name, "creator_signature": creator_signature,
            }
            db.execute(
                "INSERT INTO shared_list_items(id,list_id,creator_id,text,note,position,item_kind,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (card_id, list_id, user_id, title, json.dumps(note, ensure_ascii=False), position, "general", now, now),
            )
            self._audit(db, "couple_card.created", "couple_card", card_id, {"template_id": template_id, "style_id": style_id}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "play.updated", card_id, {"resource": "couple_card", "status": "awaiting_recipient"})
            row = db.execute("SELECT * FROM shared_list_items WHERE id=?", (card_id,)).fetchone()
            users = db.execute("SELECT id,nickname FROM users WHERE id IN (?,?)", (user_id, partner_id)).fetchall()
        return self._card_dict(row, {str(value["id"]): str(value["nickname"]) for value in users}, user_id)

    def sign_couple_card(self, card_id: str, payload: dict, user_id: str) -> dict:
        if payload.get("confirmed") is not True: raise ValueError("请先确认已阅读卡片内容并自愿签署")
        signature = self._card_text(payload.get("signature_name"), maximum=20, field="收卡人签名")
        signature_points = self._card_signature(payload.get("signature"))
        expected = int(payload.get("expected_version") or 0)
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT i.* FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id "
                "WHERE i.id=? AND l.couple_id=? AND l.type='couple_cards' AND i.deleted_at IS NULL",
                (card_id, couple["id"]),
            ).fetchone()
            if not row: raise KeyError("卡片不存在")
            note = self._card_note(row)
            if str(note.get("recipient_id") or "") != user_id: raise PermissionError("只有收到卡片的人可以签名")
            if str(note.get("status") or "awaiting_recipient") == "signed": raise ValueError("这张卡片已经签署")
            if int(row["version"]) != expected: raise ConflictError("卡片已经变化，请刷新后重试")
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            note.update({
                "status": "signed", "recipient_signature_name": signature,
                "recipient_signature": signature_points, "signed_at": now,
            })
            db.execute(
                "UPDATE shared_list_items SET note=?,completed_at=?,version=version+1,updated_at=? WHERE id=?",
                (json.dumps(note, ensure_ascii=False), now, now, card_id),
            )
            self._audit(db, "couple_card.signed", "couple_card", card_id, {"template_id": note.get("template_id")}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "play.updated", card_id, {"resource": "couple_card", "status": "signed"})
            updated = db.execute("SELECT * FROM shared_list_items WHERE id=?", (card_id,)).fetchone()
            users = db.execute("SELECT id,nickname FROM users WHERE id IN (?,?)", (user_id, partner_id)).fetchall()
        return self._card_dict(updated, {str(value["id"]): str(value["nickname"]) for value in users}, user_id)

    def couple_card_pdf(self, card_id: str, user_id: str) -> tuple[bytes, str]:
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT i.* FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id "
                "WHERE i.id=? AND l.couple_id=? AND l.type='couple_cards' AND i.deleted_at IS NULL",
                (card_id, couple["id"]),
            ).fetchone()
            if not row: raise KeyError("卡片不存在")
            users = db.execute("SELECT id,nickname FROM users WHERE id IN (?,?)", (user_id, partner_id)).fetchall()
            card = self._card_dict(row, {str(value["id"]): str(value["nickname"]) for value in users}, user_id)
        if card["status"] != "signed": raise ValueError("需要由收卡人签名后才能下载 PDF")
        asset_root = Path(self.db_path).parent / "theme-assets"
        sticker_sheets = {}
        for key, filename in {
            "cards_1": "kitty-couple-cards-1-v2.webp",
            "cards_2": "kitty-couple-cards-2-v2.webp",
            "cards_3": "kitty-couple-cards-3-v2.webp",
            "cards_4": "kitty-couple-cards-4-v2.webp",
        }.items():
            try: sticker_sheets[key] = (asset_root / filename).read_bytes()
            except OSError: pass
        return render_couple_card_pdf(card, sticker_sheets=sticker_sheets), f"love-card-{card_id[-12:]}.pdf"
