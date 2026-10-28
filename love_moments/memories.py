"""Private-by-construction relationship search and memory completion helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .intelligent_ui import ConflictError


MEMORY_CONTENT_TYPES = frozenset(
    {
        "all", "moment", "trip", "text", "photo", "video", "voice", "chat",
        "call", "anniversary", "wishlist", "location", "weekly_review", "date",
    }
)
SOURCE_LABELS = {
    "human": "本人填写",
    "real_api": "真实接口",
    "rules": "本地规则",
    "ai": "AI 建议",
    "mock": "模拟数据",
    "cache": "缓存结果",
    "legacy_unknown": "来源未记录",
}
DATE_SOURCE_LABELS = {
    "user_confirmed": "用户确认时间",
    "media_capture": "照片拍摄时间",
    "trip_date": "旅行日期",
    "system_provisional": "系统暂用时间",
    "legacy_unknown": "时间来源未记录",
}
COMPLETION_KINDS = frozenset(
    {"missing_text", "missing_location", "missing_partner_description", "provisional_date"}
)
_PARENT_TYPES = frozenset({"moment", "trip"})
_CURSOR_TYPES = frozenset({"moment", "trip"})
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _literal_like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _date_value(value: object, field: str) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field}必须是 YYYY-MM-DD") from exc
    return parsed.isoformat()


def _integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field}必须是整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}必须是整数") from exc
    if str(value).strip() != str(parsed) and not isinstance(value, int):
        raise ValueError(f"{field}必须是整数")
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{field}必须在 {minimum}–{maximum} 之间")
    return parsed


class MemoryMixin:
    """All Task-14 reads pass through one active-couple visibility contract."""

    @staticmethod
    def _memory_couple_context(db: sqlite3.Connection, user_id: str) -> tuple[sqlite3.Row, str]:
        row = db.execute(
            """SELECT c.* FROM couples c
               JOIN users a ON a.id=c.member_a
               JOIN users b ON b.id=c.member_b
               WHERE c.status='active' AND ? IN (c.member_a,c.member_b)
                 AND a.deleted_at IS NULL AND b.deleted_at IS NULL
               LIMIT 1""",
            (user_id,),
        ).fetchone()
        if not row:
            raise ValueError("请先绑定伴侣")
        partner_id = row["member_b"] if row["member_a"] == user_id else row["member_a"]
        return row, str(partner_id)

    @staticmethod
    def _visible_moment_sql(alias: str = "m") -> str:
        if alias not in {"m", "parent"}:
            raise ValueError("内部瞬间别名无效")
        return (
            f"{alias}.couple_id=? AND {alias}.deleted_at IS NULL AND {alias}.purged_at IS NULL "
            f"AND ({alias}.creator_id=? OR ({alias}.visibility='shared' "
            f"AND ({alias}.reveal_at IS NULL OR {alias}.reveal_at<=?)))"
        )

    @staticmethod
    def _visible_trip_sql(alias: str = "t") -> str:
        if alias not in {"t", "parent"}:
            raise ValueError("内部旅行别名无效")
        return f"{alias}.couple_id=? AND {alias}.deleted_at IS NULL"

    @staticmethod
    def _visible_description_sql(alias: str = "d") -> str:
        if alias not in {"d", "description"}:
            raise ValueError("内部描述别名无效")
        return f"{alias}.deleted_at IS NULL AND ({alias}.author_id=? OR {alias}.visibility='shared')"

    @staticmethod
    def _public_completion_suggestion(row: sqlite3.Row | dict[str, Any], resource_type: str, resource_id: str) -> dict[str, Any]:
        return {
            "id": row["id"],
            "resource_type": resource_type,
            "resource_id": resource_id,
            "suggestion_kind": row["suggestion_kind"],
            "status": row["status"],
            "remind_at": row["remind_at"],
            "notified_at": row["notified_at"],
            "version": int(row["version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _public_moment(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        public_keys = (
            "id", "type", "title", "body", "occurred_at", "location", "mood",
            "visibility", "reveal_at", "media_url", "version", "created_at", "updated_at",
        )
        return {key: row[key] for key in public_keys if key in row.keys()}

    def _visible_memory_parent(
        self,
        db: sqlite3.Connection,
        resource_type: str,
        resource_id: str,
        user_id: str,
        *,
        audience: str = "viewer",
    ) -> dict[str, Any]:
        if resource_type not in _PARENT_TYPES or not _ID_RE.fullmatch(str(resource_id or "")):
            raise KeyError("回忆不存在")
        couple, _ = self._memory_couple_context(db, user_id)
        if resource_type == "moment":
            self._reconcile_delayed_moments(db)
            if audience == "shared":
                row = db.execute(
                    """SELECT m.* FROM moments m
                       WHERE m.id=? AND m.couple_id=? AND m.deleted_at IS NULL AND m.purged_at IS NULL
                         AND m.visibility='shared' AND (m.reveal_at IS NULL OR m.reveal_at<=?)""",
                    (resource_id, couple["id"], _now()),
                ).fetchone()
            else:
                row = db.execute(
                    f"SELECT m.* FROM moments m WHERE m.id=? AND {self._visible_moment_sql('m')}",
                    (resource_id, couple["id"], user_id, _now()),
                ).fetchone()
        else:
            row = db.execute(
                f"SELECT t.* FROM trips t WHERE t.id=? AND {self._visible_trip_sql('t')}",
                (resource_id, couple["id"]),
            ).fetchone()
        if not row:
            raise KeyError("回忆不存在")
        result = dict(row)
        result["resource_type"] = resource_type
        result["resource_id"] = resource_id
        return result

    def resolve_memory_for_audience(
        self,
        db: sqlite3.Connection,
        resource_type: str,
        resource_id: str,
        user_id: str,
        *,
        audience: str = "personal",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        if audience not in {"personal", "shared"}:
            raise ValueError("回忆受众无效")
        if resource_type == "media":
            return self._resolve_album_media(
                db, resource_id, user_id, audience=audience, expected_version=expected_version,
            )
        parent = self._visible_memory_parent(
            db, resource_type, resource_id, user_id,
            audience="shared" if audience == "shared" else "viewer",
        )
        if expected_version is not None and int(parent["version"]) != int(expected_version):
            raise ConflictError("回忆内容已经变化，请刷新后重试")
        descriptions = self._description_rows(db, resource_type, resource_id, user_id, audience=audience)
        provenance_target = "moment_id" if resource_type == "moment" else "trip_id"
        provenance = db.execute(
            f"SELECT source_type,date_source FROM memory_provenance WHERE {provenance_target}=?",
            (resource_id,),
        ).fetchone()
        source_type = provenance["source_type"] if provenance else "legacy_unknown"
        date_source = provenance["date_source"] if provenance else "legacy_unknown"
        media = None
        media_id = parent.get("media_id") if resource_type == "moment" else None
        if media_id:
            media_visibility = (
                "media.visibility='shared' AND (media.reveal_at IS NULL OR media.reveal_at<=?)"
                if audience == "shared"
                else "(media.owner_id=? OR (media.visibility='shared' AND (media.reveal_at IS NULL OR media.reveal_at<=?)))"
            )
            media_parameters: tuple[object, ...] = (media_id, parent["couple_id"])
            if audience == "shared":
                media_parameters += (_now(),)
            else:
                media_parameters += (user_id, _now())
            media_row = db.execute(
                f"""SELECT media.id,media.version,media.kind,media.mime_type,
                           COALESCE(media.thumbnail_path, '') AS thumbnail_storage_key
                    FROM media_assets media
                    WHERE media.id=? AND media.couple_id=? AND media.status='ready'
                      AND media.deleted_at IS NULL AND media.purged_at IS NULL AND {media_visibility}""",
                media_parameters,
            ).fetchone()
            if not media_row:
                raise KeyError("回忆媒体不存在")
            media = {
                "id": media_row["id"], "version": int(media_row["version"]),
                "kind": media_row["kind"], "mime_type": media_row["mime_type"],
                "has_local_thumbnail": bool(media_row["thumbnail_storage_key"]),
            }
        dependency_versions = {f"{resource_type}:{resource_id}": int(parent["version"])}
        if media:
            dependency_versions[f"media:{media['id']}"] = media["version"]
        for description in descriptions:
            dependency_versions[f"description:{description['id']}"] = int(description["version"])
        return {
            "resource_type": resource_type,
            "resource_id": resource_id,
            "title": str(parent.get("title") or ""),
            "body": str(parent.get("body") or ""),
            "event_at": parent.get("occurred_at") or parent.get("start_date"),
            "location": parent.get("location") or parent.get("destination") or parent.get("arrival_city"),
            "version": int(parent["version"]),
            "creator_id": parent.get("creator_id"),
            "visibility": parent.get("visibility", "shared"),
            "source_type": source_type,
            "date_source": date_source,
            "media": media,
            "descriptions": descriptions,
            "dependency_versions": dependency_versions,
        }

    def _resolve_album_media(
        self,
        db: sqlite3.Connection,
        resource_id: str,
        user_id: str,
        *,
        audience: str,
        expected_version: int | None,
    ) -> dict[str, Any]:
        if not _ID_RE.fullmatch(str(resource_id or "")):
            raise KeyError("回忆不存在")
        couple, _ = self._memory_couple_context(db, user_id)
        if audience == "shared":
            visibility_sql = "media.visibility='shared' AND (media.reveal_at IS NULL OR media.reveal_at<=?)"
            parameters: tuple[object, ...] = (resource_id, couple["id"], _now())
        else:
            visibility_sql = "(media.owner_id=? OR (media.visibility='shared' AND (media.reveal_at IS NULL OR media.reveal_at<=?)))"
            parameters = (resource_id, couple["id"], user_id, _now())
        row = db.execute(
            f"""SELECT media.id,media.owner_id,media.kind,media.mime_type,media.visibility,
                       media.version,media.created_at,media.captured_at,media.date_source,media.source_type,
                       media.thumbnail_path
                FROM media_assets media
                WHERE media.id=? AND media.couple_id=? AND media.status='ready'
                  AND media.deleted_at IS NULL AND media.purged_at IS NULL AND {visibility_sql}""",
            parameters,
        ).fetchone()
        if not row:
            raise KeyError("回忆不存在")
        if expected_version is not None and int(row["version"]) != int(expected_version):
            raise ConflictError("回忆内容已经变化，请刷新后重试")
        event_at = row["captured_at"] or row["created_at"]
        date_source = row["date_source"] if row["captured_at"] else "system_provisional"
        return {
            "resource_type": "media", "resource_id": row["id"], "title": "照片",
            "body": "", "event_at": event_at, "location": None,
            "version": int(row["version"]), "creator_id": row["owner_id"],
            "visibility": row["visibility"], "source_type": row["source_type"],
            "date_source": date_source,
            "media": {
                "id": row["id"], "version": int(row["version"]), "kind": row["kind"],
                "mime_type": row["mime_type"], "has_local_thumbnail": bool(row["thumbnail_path"]),
            },
            "descriptions": [],
            "dependency_versions": {f"media:{row['id']}": int(row["version"])},
        }

    def list_memory_candidates_for_album(
        self,
        db: sqlite3.Connection,
        *,
        user_id: str,
        audience: str,
        start_at: str,
        end_at: str,
        limit: int = 121,
    ) -> list[dict[str, Any]]:
        if audience not in {"personal", "shared"} or limit < 1 or limit > 121:
            raise ValueError("纪念册候选参数无效")
        couple, _ = self._memory_couple_context(db, user_id)
        now = _now()
        if audience == "shared":
            moment_visibility = "m.visibility='shared' AND (m.reveal_at IS NULL OR m.reveal_at<=?)"
            moment_params: tuple[object, ...] = (couple["id"], start_at, end_at, now, limit)
            media_visibility = "media.visibility='shared' AND (media.reveal_at IS NULL OR media.reveal_at<=?)"
            media_params: tuple[object, ...] = (couple["id"], start_at, end_at, now, limit)
        else:
            moment_visibility = "(m.creator_id=? OR (m.visibility='shared' AND (m.reveal_at IS NULL OR m.reveal_at<=?)))"
            moment_params = (couple["id"], start_at, end_at, user_id, now, limit)
            media_visibility = "(media.owner_id=? OR (media.visibility='shared' AND (media.reveal_at IS NULL OR media.reveal_at<=?)))"
            media_params = (couple["id"], start_at, end_at, user_id, now, limit)
        moment_rows = db.execute(
            f"""SELECT m.id FROM moments m
                WHERE m.couple_id=? AND m.occurred_at>=? AND m.occurred_at<?
                  AND m.deleted_at IS NULL AND m.purged_at IS NULL AND {moment_visibility}
                  AND NOT EXISTS (SELECT 1 FROM future_letters f WHERE f.delivered_moment_id=m.id)
                ORDER BY m.occurred_at,m.id LIMIT ?""",
            moment_params,
        ).fetchall()
        items: list[dict[str, Any]] = []
        linked_media: set[str] = set()
        for row in moment_rows:
            item = self.resolve_memory_for_audience(db, "moment", row["id"], user_id, audience=audience)
            if item.get("media"):
                linked_media.add(str(item["media"]["id"]))
            items.append(item)
        remaining = max(0, limit - len(items))
        if remaining:
            media_params = media_params[:-1] + (remaining,)
            media_rows = db.execute(
                f"""SELECT media.id FROM media_assets media
                    WHERE media.couple_id=? AND COALESCE(media.captured_at,media.created_at)>=?
                      AND COALESCE(media.captured_at,media.created_at)<?
                      AND media.kind='image' AND media.status='ready'
                      AND media.deleted_at IS NULL AND media.purged_at IS NULL AND {media_visibility}
                    ORDER BY COALESCE(media.captured_at,media.created_at),media.id LIMIT ?""",
                media_params,
            ).fetchall()
            for row in media_rows:
                if row["id"] in linked_media:
                    continue
                items.append(self.resolve_memory_for_audience(db, "media", row["id"], user_id, audience=audience))
        return sorted(
            items[:limit], key=lambda item: (str(item.get("event_at") or ""), item["resource_type"], item["resource_id"]),
        )

    def _description_rows(
        self,
        db: sqlite3.Connection,
        resource_type: str,
        resource_id: str,
        user_id: str,
        *,
        audience: str = "personal",
    ) -> list[dict[str, Any]]:
        target = "moment_id" if resource_type == "moment" else "trip_id"
        visibility = "d.visibility='shared'" if audience == "shared" else self._visible_description_sql("d")
        parameters: tuple[object, ...]
        if audience == "shared":
            parameters = (resource_id,)
        else:
            parameters = (resource_id, user_id)
        rows = db.execute(
            f"""SELECT d.id,d.author_id,u.nickname,d.body,d.visibility,d.source_type,
                       d.version,d.created_at,d.updated_at,
                       CASE WHEN d.merge_proposal_json IS NULL THEN 0 ELSE 1 END AS merge_pending
                FROM memory_descriptions d JOIN users u ON u.id=d.author_id
                WHERE d.{target}=? AND {visibility}
                ORDER BY d.created_at,d.id""",
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _parse_cursor(cursor: object) -> dict[str, str] | None:
        if cursor in {None, ""}:
            return None
        text = str(cursor)
        if len(text) > 800:
            raise ValueError("搜索游标无效")
        try:
            padded = text + "=" * (-len(text) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("搜索游标无效") from exc
        if not isinstance(value, dict) or set(value) != {"event_at", "resource_type", "resource_id"}:
            raise ValueError("搜索游标无效")
        event_at = value.get("event_at")
        resource_type = value.get("resource_type")
        resource_id = value.get("resource_id")
        if not isinstance(event_at, str) or len(event_at) > 50:
            raise ValueError("搜索游标无效")
        try:
            datetime.fromisoformat(event_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("搜索游标无效") from exc
        if resource_type not in _CURSOR_TYPES or not isinstance(resource_id, str) or not _ID_RE.fullmatch(resource_id):
            raise ValueError("搜索游标无效")
        return {"event_at": event_at, "resource_type": resource_type, "resource_id": resource_id}

    @staticmethod
    def _encode_cursor(item: dict[str, Any]) -> str:
        raw = _json({
            "event_at": item["event_at"],
            "resource_type": item["resource_type"],
            "resource_id": item["resource_id"],
        }).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _parse_search_language(
        query: str,
        *,
        members: list[dict[str, str]],
        user_id: str,
        partner_id: str,
        timezone_name: str,
        now: datetime,
    ) -> dict[str, str | None]:
        remaining = query
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            zone = ZoneInfo("Asia/Shanghai")
        local_now = now.astimezone(zone)
        start_date = end_date = None
        month_match = re.search(r"(?<!\d)(\d{4})\s*年\s*(1[0-2]|0?[1-9])\s*月", remaining)
        year_match = re.search(r"(?<!\d)(\d{4})\s*年", remaining)
        month_only_match = re.search(r"(?<!\d)(1[0-2]|0?[1-9])\s*月", remaining)
        if "去年" in remaining:
            year = local_now.year - 1
            start_date, end_date = f"{year:04d}-01-01", f"{year:04d}-12-31"
            remaining = remaining.replace("去年", " ")
        elif "今年" in remaining:
            year = local_now.year
            start_date, end_date = f"{year:04d}-01-01", f"{year:04d}-12-31"
            remaining = remaining.replace("今年", " ")
        elif month_match:
            year, month = int(month_match.group(1)), int(month_match.group(2))
            next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
            start_date = date(year, month, 1).isoformat()
            end_date = (next_month - timedelta(days=1)).isoformat()
            remaining = remaining.replace(month_match.group(0), " ")
        elif year_match:
            year = int(year_match.group(1))
            start_date, end_date = f"{year:04d}-01-01", f"{year:04d}-12-31"
            remaining = remaining.replace(year_match.group(0), " ")
        elif month_only_match:
            year, month = local_now.year, int(month_only_match.group(1))
            next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
            start_date = date(year, month, 1).isoformat()
            end_date = (next_month - timedelta(days=1)).isoformat()
            remaining = remaining.replace(month_only_match.group(0), " ")

        content_type = None
        type_words = (
            ("weekly_review", ("周报", "每周回顾")), ("anniversary", ("纪念日",)),
            ("photo", ("照片", "图片")), ("video", ("视频",)), ("voice", ("语音", "录音")),
            ("call", ("通话",)), ("chat", ("聊天",)), ("trip", ("旅行", "出游")),
        )
        for value, words in type_words:
            matched = next((word for word in words if word in remaining), None)
            if matched:
                content_type = value
                remaining = remaining.replace(matched, " ")
                break

        person_id = None
        # Chinese characters are considered ``\w`` by Python, so a look-behind
        # for a word boundary makes natural phrases such as “去年我在杭州” miss
        # the current-user filter.  Only exclude the plural pronoun “我们”.
        if re.search(r"我(?!们)", remaining):
            person_id = user_id
            remaining = re.sub(r"我(?!们)", " ", remaining, count=1)
        elif re.search(r"对方|\bTA\b", remaining, re.IGNORECASE):
            person_id = partner_id
            remaining = re.sub(r"对方|\bTA\b", " ", remaining, flags=re.IGNORECASE)
        else:
            for member in sorted(members, key=lambda value: len(value["nickname"]), reverse=True):
                nickname = member["nickname"]
                nickname_match = re.search(
                    rf"{re.escape(nickname)}(?=在|的|[\s，,]|$)",
                    remaining,
                ) if nickname else None
                if nickname_match:
                    person_id = member["id"]
                    remaining = remaining[:nickname_match.start()] + " " + remaining[nickname_match.end():]
                    break

        location = None
        location_match = re.search(r"在([\u3400-\u9fffA-Za-z0-9·\-]{1,30}?)(?=记录|拍|发生|留下|的|\s|$)", remaining)
        if not location_match and content_type:
            location_match = re.search(
                r"(?:^|\s)([\u3400-\u9fffA-Za-z0-9·\-]{1,30}?)的(?=\s|$)",
                remaining,
            )
        if location_match:
            location = location_match.group(1)
            remaining = remaining.replace(location_match.group(0), " ", 1)
        remaining = re.sub(r"记录的|记录|留下的|发生的", " ", remaining)
        remaining = re.sub(r"\s+", " ", remaining).strip(" 的，,。")
        return {
            "keyword": remaining,
            "start_date": start_date,
            "end_date": end_date,
            "content_type": content_type,
            "person_id": person_id,
            "location": location,
        }

    def search_memories(
        self,
        keyword: object = "",
        start_date: object = None,
        end_date: object = None,
        content_type: object = "all",
        cursor: object = None,
        limit: object = 20,
        user_id: str = "",
        *,
        person_id: object = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not isinstance(keyword, str):
            raise ValueError("搜索内容必须是文字")
        query = keyword.strip()
        if len(query) > 100:
            raise ValueError("搜索内容不能超过 100 个字符")
        page_size = _integer(limit, "每页数量", 1, 50)
        page_cursor = self._parse_cursor(cursor)
        requested_type = str(content_type or "all").strip()
        if requested_type not in MEMORY_CONTENT_TYPES:
            raise ValueError("回忆类型无效")
        explicit_start = _date_value(start_date, "开始日期")
        explicit_end = _date_value(end_date, "结束日期")
        if explicit_start and explicit_end and explicit_start > explicit_end:
            raise ValueError("开始日期不能晚于结束日期")
        if explicit_start and explicit_end and (date.fromisoformat(explicit_end) - date.fromisoformat(explicit_start)).days > 3660:
            raise ValueError("搜索日期范围不能超过 10 年")

        with self.connection() as db:
            self._reconcile_delayed_moments(db)
            couple, partner_id = self._memory_couple_context(db, user_id)
            user = db.execute("SELECT timezone FROM users WHERE id=?", (user_id,)).fetchone()
            member_rows = db.execute(
                "SELECT id,nickname FROM users WHERE id IN (?,?) AND deleted_at IS NULL",
                (user_id, partner_id),
            ).fetchall()
            parsed = self._parse_search_language(
                query,
                members=[dict(row) for row in member_rows],
                user_id=user_id,
                partner_id=partner_id,
                timezone_name=str(user["timezone"] if user else "Asia/Shanghai"),
                now=now or datetime.now(timezone.utc),
            )
            effective_start = explicit_start or parsed["start_date"]
            effective_end = explicit_end or parsed["end_date"]
            effective_type = requested_type if requested_type != "all" else str(parsed["content_type"] or "all")
            effective_person = str(person_id or parsed["person_id"] or "").strip() or None
            if effective_person not in {None, user_id, partner_id}:
                return {
                    "items": [], "next_cursor": None, "total": 0,
                    "parsed_filters": {
                        "keyword": str(parsed["keyword"] or ""), "start_date": effective_start,
                        "end_date": effective_end, "content_type": effective_type,
                        "person_id": effective_person, "location": parsed["location"],
                    },
                    "empty_reason": "没有找到当前可见的相关回忆",
                }

            residual = str(parsed["keyword"] or "")
            location = str(parsed["location"] or "").strip() or None
            try:
                user_zone = ZoneInfo(str(user["timezone"] if user else "Asia/Shanghai"))
            except ZoneInfoNotFoundError:
                user_zone = ZoneInfo("Asia/Shanghai")
            # Moment timestamps are UTC instants, while trips use local calendar
            # dates.  Convert the user's date bounds once so a late-night memory
            # is not shifted into the wrong day by a raw string prefix compare.
            moment_start = None
            moment_end_exclusive = None
            if effective_start:
                moment_start = datetime.combine(
                    date.fromisoformat(effective_start), datetime.min.time(), tzinfo=user_zone,
                ).astimezone(timezone.utc).isoformat()
            if effective_end:
                moment_end_exclusive = datetime.combine(
                    date.fromisoformat(effective_end) + timedelta(days=1),
                    datetime.min.time(), tzinfo=user_zone,
                ).astimezone(timezone.utc).isoformat()
            selects: list[str] = []
            params: list[object] = []
            if effective_type in MEMORY_CONTENT_TYPES:
                where = [self._visible_moment_sql("m")]
                moment_params: list[object] = [couple["id"], user_id, _now()]
                if effective_type not in {"all", "moment", "trip"}:
                    where.append("m.type=?")
                    moment_params.append(effective_type)
                elif effective_type == "trip":
                    where.append("m.type='trip'")
                if moment_start:
                    where.append("m.occurred_at>=?")
                    moment_params.append(moment_start)
                if moment_end_exclusive:
                    where.append("m.occurred_at<?")
                    moment_params.append(moment_end_exclusive)
                if location:
                    where.append("COALESCE(m.location,'') LIKE ? ESCAPE '\\'")
                    moment_params.append(_literal_like(location))
                if residual:
                    literal = _literal_like(residual)
                    where.append(
                        """(m.title LIKE ? ESCAPE '\\' OR m.body LIKE ? ESCAPE '\\'
                             OR COALESCE(m.location,'') LIKE ? ESCAPE '\\' OR u.nickname LIKE ? ESCAPE '\\'
                             OR EXISTS(SELECT 1 FROM memory_descriptions d
                                WHERE d.moment_id=m.id AND d.deleted_at IS NULL
                                  AND (d.author_id=? OR d.visibility='shared')
                                  AND d.body LIKE ? ESCAPE '\\')
                             OR EXISTS(SELECT 1 FROM voice_transcripts vt
                                JOIN media_assets vm ON vm.id=vt.media_id
                                WHERE vt.moment_id=m.id AND vt.couple_id=m.couple_id
                                  AND vt.status='ready' AND vt.body LIKE ? ESCAPE '\\'
                                  AND vm.status='ready' AND vm.deleted_at IS NULL AND vm.purged_at IS NULL
                                  AND (vt.owner_id=? OR ((vt.visibility='shared'
                                       OR (vt.visibility='delayed' AND vt.reveal_at<=?))
                                       AND vm.visibility='shared'))))"""
                    )
                    moment_params.extend([literal, literal, literal, literal, user_id, literal, literal, user_id, _now()])
                if effective_person:
                    where.append(
                        """(m.creator_id=? OR EXISTS(SELECT 1 FROM memory_descriptions d
                             WHERE d.moment_id=m.id AND d.deleted_at IS NULL AND d.author_id=?
                               AND (d.author_id=? OR d.visibility='shared')))"""
                    )
                    moment_params.extend([effective_person, effective_person, user_id])
                selects.append(
                    """SELECT 'moment' AS resource_type,m.id AS resource_id,m.occurred_at AS event_at,
                              m.type AS event_type,m.title AS title,m.body AS summary,
                              COALESCE(m.location,'') AS location,m.creator_id AS primary_person_id,
                              u.nickname AS primary_person_name,m.version AS version,
                              COALESCE(p.source_type,'legacy_unknown') AS source_type,
                              COALESCE(p.date_source,'legacy_unknown') AS date_source,
                              m.visibility AS visibility
                       FROM moments m JOIN users u ON u.id=m.creator_id
                       LEFT JOIN memory_provenance p ON p.moment_id=m.id
                       WHERE """ + " AND ".join(where)
                )
                params.extend(moment_params)

            if effective_type in {"all", "trip"}:
                where = [self._visible_trip_sql("t")]
                trip_params: list[object] = [couple["id"]]
                if effective_start:
                    where.append("t.start_date>=?")
                    trip_params.append(effective_start)
                if effective_end:
                    where.append("t.start_date<=?")
                    trip_params.append(effective_end)
                if location:
                    literal_location = _literal_like(location)
                    where.append("(t.destination LIKE ? ESCAPE '\\' OR t.arrival_city LIKE ? ESCAPE '\\')")
                    trip_params.extend([literal_location, literal_location])
                if residual:
                    literal = _literal_like(residual)
                    where.append(
                        """(t.title LIKE ? ESCAPE '\\' OR t.destination LIKE ? ESCAPE '\\'
                             OR t.arrival_city LIKE ? ESCAPE '\\'
                             OR EXISTS(SELECT 1 FROM memory_descriptions d
                                WHERE d.trip_id=t.id AND d.deleted_at IS NULL
                                  AND (d.author_id=? OR d.visibility='shared')
                                  AND d.body LIKE ? ESCAPE '\\'))"""
                    )
                    trip_params.extend([literal, literal, literal, user_id, literal])
                if effective_person:
                    where.append(
                        """EXISTS(SELECT 1 FROM memory_descriptions d
                           WHERE d.trip_id=t.id AND d.deleted_at IS NULL AND d.author_id=?
                             AND (d.author_id=? OR d.visibility='shared'))"""
                    )
                    trip_params.extend([effective_person, user_id])
                selects.append(
                    """SELECT 'trip' AS resource_type,t.id AS resource_id,
                              t.start_date || 'T00:00:00+00:00' AS event_at,
                              'trip' AS event_type,t.title AS title,t.destination AS summary,
                              COALESCE(NULLIF(t.arrival_city,''),t.destination) AS location,
                              NULL AS primary_person_id,NULL AS primary_person_name,t.version AS version,
                              COALESCE(p.source_type,'legacy_unknown') AS source_type,
                              COALESCE(p.date_source,'legacy_unknown') AS date_source,
                              'shared' AS visibility
                       FROM trips t LEFT JOIN memory_provenance p ON p.trip_id=t.id
                       WHERE """ + " AND ".join(where)
                )
                params.extend(trip_params)
            if not selects:
                raise ValueError("回忆类型无效")
            cte = "WITH visible_results AS (" + " UNION ALL ".join(selects) + ")"
            total = int(db.execute(cte + " SELECT COUNT(*) FROM visible_results", tuple(params)).fetchone()[0])
            item_params = list(params)
            seek = ""
            if page_cursor:
                seek = (
                    " WHERE (event_at<? OR (event_at=? AND resource_type<?) "
                    "OR (event_at=? AND resource_type=? AND resource_id<?))"
                )
                item_params.extend([
                    page_cursor["event_at"], page_cursor["event_at"], page_cursor["resource_type"],
                    page_cursor["event_at"], page_cursor["resource_type"], page_cursor["resource_id"],
                ])
            rows = db.execute(
                cte + " SELECT * FROM visible_results" + seek +
                " ORDER BY event_at DESC,resource_type DESC,resource_id DESC LIMIT ?",
                tuple(item_params + [page_size + 1]),
            ).fetchall()
            has_more = len(rows) > page_size
            rows = rows[:page_size]
            items: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["descriptions"] = self._description_rows(
                    db, item["resource_type"], item["resource_id"], user_id,
                )
                item["provenance"] = {
                    "source_type": item.pop("source_type"),
                    "source_label": SOURCE_LABELS.get(row["source_type"], "来源未记录"),
                    "date_source": item.pop("date_source"),
                    "date_source_label": DATE_SOURCE_LABELS.get(row["date_source"], "时间来源未记录"),
                }
                items.append(item)
            parsed_filters = {
                "keyword": residual,
                "start_date": effective_start,
                "end_date": effective_end,
                "content_type": effective_type,
                "person_id": effective_person,
                "location": location,
            }
            return {
                "items": items,
                "next_cursor": self._encode_cursor(items[-1]) if has_more and items else None,
                "total": total,
                "parsed_filters": parsed_filters,
                "empty_reason": None if items else "没有找到当前可见的相关回忆",
            }

    @staticmethod
    def _public_description(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item.pop("merge_proposal_json", None)
        item.pop("merge_confirmed_by", None)
        item.pop("merge_confirmed_version", None)
        item.pop("merge_confirmed_at", None)
        item.pop("deleted_at", None)
        return item

    def _save_memory_description_in_connection(
        self,
        db: sqlite3.Connection,
        resource_type: str,
        resource_id: str,
        payload: dict[str, Any],
        user_id: str,
    ) -> dict[str, Any]:
        if resource_type not in _PARENT_TYPES:
            raise ValueError("回忆类型无效")
        allowed = {"body", "visibility", "expected_version"}
        if set(payload) - allowed:
            raise ValueError("描述包含不支持的字段")
        body = payload.get("body")
        if not isinstance(body, str) or not body.strip() or len(body.strip()) > 5000:
            raise ValueError("补充描述需为 1–5000 个字符")
        visibility = str(payload.get("visibility") or "private")
        if visibility not in {"private", "shared"}:
            raise ValueError("描述可见范围无效")
        expected = _integer(payload.get("expected_version", 0), "描述版本", 0, 2_147_483_647)
        parent = self._visible_memory_parent(db, resource_type, resource_id, user_id)
        target = "moment_id" if resource_type == "moment" else "trip_id"
        existing = db.execute(
            f"SELECT * FROM memory_descriptions WHERE {target}=? AND author_id=?",
            (resource_id, user_id),
        ).fetchone()
        now = _now()
        if existing:
            if int(existing["version"]) != expected:
                raise ConflictError("补充描述已经变化，请刷新后重试")
            db.execute(
                """UPDATE memory_descriptions
                   SET body=?,visibility=?,source_type='human',merge_proposal_json=NULL,
                       merge_confirmed_by=NULL,merge_confirmed_version=NULL,merge_confirmed_at=NULL,
                       version=version+1,deleted_at=NULL,updated_at=? WHERE id=?""",
                (body.strip(), visibility, now, existing["id"]),
            )
            description_id = existing["id"]
        else:
            if expected != 0:
                raise ConflictError("补充描述已经变化，请刷新后重试")
            description_id = _id("memory_description")
            db.execute(
                f"""INSERT INTO memory_descriptions(
                    id,couple_id,author_id,{target},body,visibility,source_type,version,created_at,updated_at
                ) VALUES (?,?,?,?,? ,?,'human',1,?,?)""",
                (description_id, parent["couple_id"], user_id, resource_id, body.strip(), visibility, now, now),
            )
        return self._public_description(db.execute("SELECT * FROM memory_descriptions WHERE id=?", (description_id,)).fetchone())

    def save_memory_description(
        self,
        resource_type: str,
        resource_id: str,
        payload: dict[str, Any],
        user_id: str,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("补充描述无效")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            result = self._save_memory_description_in_connection(db, resource_type, resource_id, payload, user_id)
            parent = self._visible_memory_parent(db, resource_type, resource_id, user_id)
            self._audit(db, "memory.description.saved", resource_type, resource_id, {"visibility": result["visibility"]}, actor_id=user_id)
            self._event(
                db, parent["couple_id"], user_id, "life.updated", resource_id,
                {"resource": "memory_description", "owner_only": result["visibility"] == "private"},
            )
            return result

    def get_memory_descriptions(self, resource_type: str, resource_id: str, user_id: str) -> dict[str, Any]:
        with self.connection() as db:
            parent = self._visible_memory_parent(db, resource_type, resource_id, user_id)
            proposal = None
            if resource_type == "moment" and parent.get("visibility", "shared") == "shared":
                proposal_rows = db.execute(
                    """SELECT author_id,version,merge_proposal_json
                       FROM memory_descriptions
                       WHERE moment_id=? AND deleted_at IS NULL AND visibility='shared'
                         AND merge_proposal_json IS NOT NULL""",
                    (resource_id,),
                ).fetchall()
                if len(proposal_rows) > 1:
                    raise ConflictError("存在多份合并提议，请重新整理双方描述")
                if proposal_rows:
                    proposal_row = proposal_rows[0]
                    try:
                        raw_proposal = json.loads(proposal_row["merge_proposal_json"])
                    except (TypeError, json.JSONDecodeError):
                        raw_proposal = None
                    body = str((raw_proposal or {}).get("body") or "").strip()
                    proposal_id = str((raw_proposal or {}).get("proposal_id") or "")
                    if body and _ID_RE.fullmatch(proposal_id) and proposal_id.startswith("memory_merge_"):
                        proposal = {
                            "proposal_id": proposal_id,
                            "version": int(proposal_row["version"]),
                            "body": body,
                            "proposer_id": proposal_row["author_id"],
                            "can_confirm": proposal_row["author_id"] != user_id,
                        }
                    else:
                        raise ConflictError("合并提议已经失效，请重新发起")
            return {
                "resource_type": resource_type,
                "resource_id": resource_id,
                "parent_version": int(parent["version"]),
                "parent_visibility": parent.get("visibility", "shared"),
                "items": self._description_rows(db, resource_type, resource_id, user_id),
                "merge_proposal": proposal,
            }

    def propose_memory_description_merge(self, moment_id: str, payload: dict[str, Any], user_id: str) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) - {"body", "expected_parent_version", "expected_versions"}:
            raise ValueError("合并提议内容无效")
        body = payload.get("body")
        if not isinstance(body, str) or not body.strip() or len(body.strip()) > 5000:
            raise ValueError("共同描述需为 1–5000 个字符")
        expected_parent = _integer(payload.get("expected_parent_version"), "回忆版本", 1, 2_147_483_647)
        expected_versions = payload.get("expected_versions")
        if not isinstance(expected_versions, dict):
            raise ValueError("描述版本无效")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            parent = self._visible_memory_parent(db, "moment", moment_id, user_id, audience="shared")
            couple, partner_id = self._memory_couple_context(db, user_id)
            if int(parent["version"]) != expected_parent:
                raise ConflictError("回忆正文已经变化，请刷新后重试")
            rows = db.execute(
                """SELECT * FROM memory_descriptions WHERE moment_id=? AND deleted_at IS NULL
                   AND visibility='shared' AND author_id IN (?,?)""",
                (moment_id, user_id, partner_id),
            ).fetchall()
            by_author = {row["author_id"]: row for row in rows}
            if set(by_author) != {user_id, partner_id}:
                raise ValueError("需要双方各有一条双方可见描述后才能合并")
            for author_id, row in by_author.items():
                supplied = expected_versions.get(author_id)
                if isinstance(supplied, bool) or not isinstance(supplied, int) or supplied != int(row["version"]):
                    raise ConflictError("补充描述已经变化，请刷新后重试")
            if db.execute(
                """SELECT 1 FROM memory_descriptions WHERE moment_id=? AND deleted_at IS NULL
                   AND visibility='shared' AND merge_proposal_json IS NOT NULL LIMIT 1""",
                (moment_id,),
            ).fetchone():
                raise ConflictError("已有一份合并提议等待确认")
            proposal_id = _id("memory_merge")
            proposal = {
                "proposal_id": proposal_id,
                "body": body.strip(),
                "parent_version": expected_parent,
                "description_versions": {author: int(row["version"]) for author, row in by_author.items()},
                "proposer_id": user_id,
                "created_at": _now(),
            }
            proposer = by_author[user_id]
            changed = db.execute(
                """UPDATE memory_descriptions
                   SET merge_proposal_json=?,merge_confirmed_by=NULL,merge_confirmed_version=NULL,
                       merge_confirmed_at=NULL,version=version+1,updated_at=?
                   WHERE id=? AND version=? AND merge_proposal_json IS NULL""",
                (_json(proposal), _now(), proposer["id"], proposer["version"]),
            ).rowcount
            if changed != 1:
                raise ConflictError("补充描述已经变化，请刷新后重试")
            updated = db.execute("SELECT * FROM memory_descriptions WHERE id=?", (proposer["id"],)).fetchone()
            if not updated or updated["merge_proposal_json"] is None:
                raise ConflictError("已有一份合并提议等待确认")
            self._audit(db, "memory.merge.proposed", "moment", moment_id, {"proposal_version": updated["version"]}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", moment_id, {"resource": "memory_merge_proposal"})
            return {"moment_id": moment_id, "proposal_id": proposal_id, "version": int(updated["version"]), "body": body.strip(), "proposer_id": user_id}

    def confirm_memory_description_merge(self, moment_id: str, proposal_id: object, expected_version: object, user_id: str) -> dict[str, Any]:
        proposal_key = str(proposal_id or "").strip()
        if not _ID_RE.fullmatch(proposal_key) or not proposal_key.startswith("memory_merge_"):
            raise ValueError("合并提议标识无效")
        expected = _integer(expected_version, "合并提议版本", 1, 2_147_483_647)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            parent = self._visible_memory_parent(db, "moment", moment_id, user_id, audience="shared")
            couple, _ = self._memory_couple_context(db, user_id)
            proposal_rows = db.execute(
                """SELECT * FROM memory_descriptions WHERE moment_id=? AND deleted_at IS NULL
                   AND visibility='shared' AND merge_proposal_json IS NOT NULL""",
                (moment_id,),
            ).fetchall()
            if len(proposal_rows) > 1:
                raise ConflictError("存在多份合并提议，请重新整理双方描述")
            proposal_row = None
            proposal = None
            for candidate in proposal_rows:
                try:
                    parsed = json.loads(candidate["merge_proposal_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ConflictError("合并提议已经失效") from exc
                if parsed.get("proposal_id") == proposal_key:
                    proposal_row, proposal = candidate, parsed
            if not proposal_row or not proposal:
                raise KeyError("合并提议不存在")
            if proposal.get("proposer_id") != proposal_row["author_id"]:
                raise ConflictError("合并提议身份校验失败")
            if proposal["proposer_id"] == user_id:
                raise PermissionError("合并提议必须由另一方确认")
            if int(proposal_row["version"]) != expected:
                raise ConflictError("合并提议已经变化，请刷新后重试")
            if int(parent["version"]) != int(proposal.get("parent_version", -1)):
                raise ConflictError("回忆正文已经变化，请重新发起合并")
            expected_descriptions = proposal.get("description_versions")
            if not isinstance(expected_descriptions, dict):
                raise ConflictError("合并提议已经失效")
            rows = db.execute(
                "SELECT * FROM memory_descriptions WHERE moment_id=? AND deleted_at IS NULL AND visibility='shared'",
                (moment_id,),
            ).fetchall()
            current = {row["author_id"]: row for row in rows}
            if set(expected_descriptions) != set(current):
                raise ConflictError("双方描述已经变化，请重新发起合并")
            for author_id, source_version in expected_descriptions.items():
                current_version = int(current[author_id]["version"])
                required = int(source_version) + (1 if author_id == proposal_row["author_id"] else 0)
                if current_version != required:
                    raise ConflictError("双方描述已经变化，请重新发起合并")
            body = str(proposal.get("body") or "").strip()
            if not body:
                raise ConflictError("合并提议已经失效")
            changed = db.execute(
                "UPDATE moments SET body=?,version=version+1 WHERE id=? AND version=?",
                (body[:5000], moment_id, parent["version"]),
            ).rowcount
            if changed != 1:
                raise ConflictError("回忆正文已经变化，请重新发起合并")
            db.execute(
                """UPDATE memory_descriptions SET merge_proposal_json=NULL,merge_confirmed_by=NULL,
                   merge_confirmed_version=NULL,merge_confirmed_at=NULL,updated_at=? WHERE moment_id=?""",
                (_now(), moment_id),
            )
            self._audit(db, "memory.merge.confirmed", "moment", moment_id, {}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", moment_id, {"resource": "moment"})
            return self._public_moment(db.execute("SELECT * FROM moments WHERE id=?", (moment_id,)).fetchone())

    def refresh_memory_completion_suggestions(self, user_id: str) -> dict[str, Any]:
        now = _now()
        created = 0
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile_delayed_moments(db)
            couple, partner_id = self._memory_couple_context(db, user_id)
            moments = db.execute(
                f"""SELECT m.* FROM moments m WHERE {self._visible_moment_sql('m')}
                    ORDER BY m.occurred_at DESC,m.id DESC LIMIT 500""",
                (couple["id"], user_id, now),
            ).fetchall()
            for moment in moments:
                existing_mine = db.execute(
                    "SELECT 1 FROM memory_descriptions WHERE moment_id=? AND author_id=? AND deleted_at IS NULL AND trim(body)<>''",
                    (moment["id"], user_id),
                ).fetchone()
                kinds: list[str] = []
                if moment["type"] in {"photo", "video", "voice"} and not str(moment["body"] or "").strip() and not existing_mine:
                    kinds.append("missing_text")
                if moment["creator_id"] == user_id and not str(moment["location"] or "").strip():
                    kinds.append("missing_location")
                if moment["visibility"] == "shared" and not existing_mine:
                    partner_shared = db.execute(
                        """SELECT 1 FROM memory_descriptions WHERE moment_id=? AND author_id=?
                           AND visibility='shared' AND deleted_at IS NULL AND trim(body)<>''""",
                        (moment["id"], partner_id),
                    ).fetchone()
                    if partner_shared:
                        kinds.append("missing_partner_description")
                provenance = db.execute(
                    "SELECT date_source FROM memory_provenance WHERE moment_id=?",
                    (moment["id"],),
                ).fetchone()
                if moment["creator_id"] == user_id and provenance and provenance["date_source"] == "system_provisional":
                    kinds.append("provisional_date")
                for kind in kinds:
                    suggestion_id = _id("memory_suggestion")
                    changed = db.execute(
                        """INSERT OR IGNORE INTO memory_completion_suggestions(
                            id,couple_id,user_id,moment_id,suggestion_kind,status,version,created_at,updated_at
                        ) VALUES (?,?,?,?,?,'pending',1,?,?)""",
                        (suggestion_id, couple["id"], user_id, moment["id"], kind, now, now),
                    ).rowcount
                    created += int(changed == 1)
            trips = db.execute(
                "SELECT * FROM trips WHERE couple_id=? AND deleted_at IS NULL ORDER BY start_date DESC,id DESC LIMIT 500",
                (couple["id"],),
            ).fetchall()
            for trip in trips:
                mine = db.execute(
                    "SELECT 1 FROM memory_descriptions WHERE trip_id=? AND author_id=? AND deleted_at IS NULL AND trim(body)<>''",
                    (trip["id"], user_id),
                ).fetchone()
                partner_shared = db.execute(
                    """SELECT 1 FROM memory_descriptions WHERE trip_id=? AND author_id=?
                       AND visibility='shared' AND deleted_at IS NULL AND trim(body)<>''""",
                    (trip["id"], partner_id),
                ).fetchone()
                if partner_shared and not mine:
                    created += int(db.execute(
                        """INSERT OR IGNORE INTO memory_completion_suggestions(
                            id,couple_id,user_id,trip_id,suggestion_kind,status,version,created_at,updated_at
                        ) VALUES (?,?,?,?, 'missing_partner_description','pending',1,?,?)""",
                        (_id("memory_suggestion"), couple["id"], user_id, trip["id"], now, now),
                    ).rowcount == 1)
            return {"created": created}

    def list_memory_completion_suggestions(self, user_id: str) -> dict[str, Any]:
        now = _now()
        items: list[dict[str, Any]] = []
        with self.connection() as db:
            couple, _ = self._memory_couple_context(db, user_id)
            rows = db.execute(
                """SELECT * FROM memory_completion_suggestions
                   WHERE couple_id=? AND user_id=?
                     AND (status='pending' OR (status='snoozed' AND remind_at<=?))
                   ORDER BY created_at,id LIMIT 100""",
                (couple["id"], user_id, now),
            ).fetchall()
            for row in rows:
                resource_type = "moment" if row["moment_id"] else "trip"
                resource_id = row["moment_id"] or row["trip_id"]
                try:
                    parent = self._visible_memory_parent(db, resource_type, resource_id, user_id)
                except (KeyError, ValueError):
                    continue
                item = dict(row)
                item["resource_type"] = resource_type
                item["resource_id"] = resource_id
                item["title"] = parent["title"]
                target = "moment_id" if resource_type == "moment" else "trip_id"
                provenance = db.execute(
                    f"SELECT source_type,date_source FROM memory_provenance WHERE {target}=?",
                    (resource_id,),
                ).fetchone()
                source_type = provenance["source_type"] if provenance else "legacy_unknown"
                date_source = provenance["date_source"] if provenance else "legacy_unknown"
                item["provenance"] = {
                    "source_type": source_type,
                    "source_label": SOURCE_LABELS.get(source_type, "来源未记录"),
                    "date_source": date_source,
                    "date_source_label": DATE_SOURCE_LABELS.get(date_source, "时间来源未记录"),
                }
                item.pop("moment_id", None)
                item.pop("trip_id", None)
                item.pop("couple_id", None)
                item.pop("user_id", None)
                if not row["notified_at"]:
                    db.execute("UPDATE memory_completion_suggestions SET notified_at=? WHERE id=? AND notified_at IS NULL", (now, row["id"]))
                    item["notified_at"] = now
                items.append(item)
        return {"items": items, "total": len(items)}

    def respond_memory_completion_suggestion(self, suggestion_id: str, payload: dict[str, Any], user_id: str) -> dict[str, Any]:
        if not isinstance(payload, dict) or not _ID_RE.fullmatch(str(suggestion_id or "")):
            raise ValueError("补全提醒操作无效")
        allowed = {"action", "expected_version", "remind_at", "body", "visibility", "location", "occurred_at", "expected_parent_version", "idempotency_key"}
        if set(payload) - allowed:
            raise ValueError("补全提醒包含不支持的字段")
        action = str(payload.get("action") or "")
        if action not in {"complete", "snooze", "dismiss", "restore"}:
            raise ValueError("补全提醒操作无效")
        expected = _integer(payload.get("expected_version"), "提醒版本", 1, 2_147_483_647)
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        if idempotency_key and not re.fullmatch(r"[A-Za-z0-9:._-]{8,160}", idempotency_key):
            raise ValueError("幂等键格式无效")
        request_hash = hashlib.sha256(_json({
            "suggestion_id": suggestion_id,
            "payload": {key: value for key, value in payload.items() if key != "idempotency_key"},
        }).encode("utf-8")).hexdigest()
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._memory_couple_context(db, user_id)
            operation = f"memory.completion.respond:{suggestion_id}"
            if idempotency_key:
                replay = db.execute(
                    """SELECT result_json FROM mutation_idempotency
                       WHERE couple_id=? AND user_id=? AND operation=? AND idempotency_key=?""",
                    (couple["id"], user_id, operation, idempotency_key),
                ).fetchone()
                if replay:
                    stored = json.loads(replay["result_json"])
                    if stored.get("request_hash") != request_hash:
                        raise ConflictError("幂等键已用于另一项补全操作")
                    response = dict(stored["response"])
                    replay_type = response.get("resource_type") or ("moment" if response.get("moment_id") else "trip")
                    replay_id = response.get("resource_id") or response.get("moment_id") or response.get("trip_id")
                    return self._public_completion_suggestion(response, replay_type, replay_id)
            row = db.execute(
                "SELECT * FROM memory_completion_suggestions WHERE id=? AND couple_id=? AND user_id=?",
                (suggestion_id, couple["id"], user_id),
            ).fetchone()
            if not row:
                raise KeyError("补全提醒不存在")
            if int(row["version"]) != expected:
                raise ConflictError("补全提醒已经变化，请刷新后重试")
            resource_type = "moment" if row["moment_id"] else "trip"
            resource_id = row["moment_id"] or row["trip_id"]
            parent = self._visible_memory_parent(db, resource_type, resource_id, user_id)
            status = row["status"]
            remind_at = row["remind_at"]
            if action == "dismiss":
                status, remind_at = "dismissed", None
            elif action == "restore":
                if status != "dismissed":
                    raise ValueError("只有已忽略的提醒可以恢复")
                status, remind_at = "pending", None
            elif action == "snooze":
                remind_at = str(payload.get("remind_at") or "")
                try:
                    parsed_reminder = datetime.fromisoformat(remind_at.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("稍后提醒时间无效") from exc
                if parsed_reminder.tzinfo is None:
                    parsed_reminder = parsed_reminder.replace(tzinfo=timezone.utc)
                if parsed_reminder.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                    raise ValueError("稍后提醒时间必须晚于当前时间")
                remind_at = parsed_reminder.astimezone(timezone.utc).replace(microsecond=0).isoformat()
                status = "snoozed"
            else:
                kind = row["suggestion_kind"]
                if kind in {"missing_text", "missing_partner_description"}:
                    description_target = "moment_id" if resource_type == "moment" else "trip_id"
                    existing_description = db.execute(
                        f"SELECT version FROM memory_descriptions WHERE {description_target}=? AND author_id=?",
                        (resource_id, user_id),
                    ).fetchone()
                    self._save_memory_description_in_connection(
                        db, resource_type, resource_id,
                        {
                            "body": payload.get("body"),
                            "visibility": payload.get("visibility", "shared"),
                            "expected_version": int(existing_description["version"]) if existing_description else 0,
                        },
                        user_id,
                    )
                elif kind == "missing_location":
                    if resource_type != "moment" or parent["creator_id"] != user_id:
                        raise PermissionError("只能补充自己创建回忆的地点")
                    location = payload.get("location")
                    if not isinstance(location, str) or not location.strip() or len(location.strip()) > 200:
                        raise ValueError("地点需为 1–200 个字符")
                    expected_parent = _integer(payload.get("expected_parent_version"), "回忆版本", 1, 2_147_483_647)
                    if expected_parent != int(parent["version"]):
                        raise ConflictError("回忆已经变化，请刷新后重试")
                    if db.execute("UPDATE moments SET location=?,version=version+1 WHERE id=? AND version=?", (location.strip(), resource_id, expected_parent)).rowcount != 1:
                        raise ConflictError("回忆已经变化，请刷新后重试")
                elif kind == "provisional_date":
                    if resource_type != "moment" or parent["creator_id"] != user_id:
                        raise PermissionError("只能确认自己创建回忆的日期")
                    occurred_at = str(payload.get("occurred_at") or "")
                    try:
                        parsed_date = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
                    except ValueError as exc:
                        raise ValueError("回忆时间无效") from exc
                    if parsed_date.tzinfo is None:
                        parsed_date = parsed_date.replace(tzinfo=timezone.utc)
                    confirmed = parsed_date.astimezone(timezone.utc).replace(microsecond=0).isoformat()
                    expected_parent = _integer(payload.get("expected_parent_version"), "回忆版本", 1, 2_147_483_647)
                    if expected_parent != int(parent["version"]):
                        raise ConflictError("回忆已经变化，请刷新后重试")
                    if db.execute("UPDATE moments SET occurred_at=?,version=version+1 WHERE id=? AND version=?", (confirmed, resource_id, expected_parent)).rowcount != 1:
                        raise ConflictError("回忆已经变化，请刷新后重试")
                    self._upsert_memory_provenance_in_connection(
                        db, "moment", resource_id, parent["couple_id"], user_id,
                        source_type="human", date_source="user_confirmed",
                        resource_version=expected_parent + 1,
                    )
                status, remind_at = "completed", None
            changed = db.execute(
                """UPDATE memory_completion_suggestions
                   SET status=?,remind_at=?,version=version+1,updated_at=?
                   WHERE id=? AND version=?""",
                (status, remind_at, now, suggestion_id, expected),
            ).rowcount
            if changed != 1:
                raise ConflictError("补全提醒已经变化，请刷新后重试")
            result = self._public_completion_suggestion(
                db.execute("SELECT * FROM memory_completion_suggestions WHERE id=?", (suggestion_id,)).fetchone(),
                resource_type,
                resource_id,
            )
            if idempotency_key:
                db.execute(
                    """INSERT INTO mutation_idempotency(
                           couple_id,user_id,operation,idempotency_key,result_json,created_at
                       ) VALUES (?,?,?,?,?,?)""",
                    (couple["id"], user_id, operation, idempotency_key,
                     _json({"request_hash": request_hash, "response": result}), now),
                )
            return result

    @staticmethod
    def _public_voice_transcript(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        keys = (
            "id", "body", "language", "method", "source_type", "status",
            "visibility", "reveal_at", "version", "created_at", "updated_at",
        )
        return {key: row[key] for key in keys if key in row.keys()}

    @staticmethod
    def _voice_datetime(value: object, field: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{field}不能为空")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field}格式无效") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field}必须包含时区")
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    def _voice_transcript_for_viewer(
        self,
        db: sqlite3.Connection,
        moment: sqlite3.Row | dict[str, Any],
        media: sqlite3.Row | dict[str, Any],
        user_id: str,
    ) -> dict[str, Any] | None:
        row = db.execute(
            """SELECT * FROM voice_transcripts
               WHERE moment_id=? AND media_id=? AND couple_id=? AND owner_id=?""",
            (moment["id"], media["id"], moment["couple_id"], moment["creator_id"]),
        ).fetchone()
        if not row:
            return None
        if row["owner_id"] != user_id:
            if (
                row["status"] != "ready"
                or not (
                    row["visibility"] == "shared"
                    or (row["visibility"] == "delayed" and row["reveal_at"] and row["reveal_at"] <= _now())
                )
                or moment["visibility"] != "shared"
                or media["visibility"] != "shared"
            ):
                return None
        return self._public_voice_transcript(row)

    def create_voice_memory(
        self,
        payload: dict[str, Any],
        user_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("语音回忆内容无效")
        key = str(idempotency_key or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9:._-]{8,160}", key):
            raise ValueError("幂等键格式无效")
        allowed = {
            "media_id", "title", "note", "occurred_at", "location", "mood",
            "visibility", "reveal_at", "duration_ms", "transcript",
        }
        if set(payload) - allowed:
            raise ValueError("语音回忆包含未知字段")
        media_id = str(payload.get("media_id") or "").strip()
        if not _ID_RE.fullmatch(media_id):
            raise ValueError("音频标识无效")
        title = str(payload.get("title") or "").strip()
        if not title or len(title) > 200:
            raise ValueError("语音标题需为 1–200 个字符")
        note = str(payload.get("note") or "")
        if len(note) > 5000:
            raise ValueError("语音备注不能超过 5000 个字符")
        occurred_at = self._voice_datetime(payload.get("occurred_at"), "发生时间")
        visibility = str(payload.get("visibility") or "shared").strip()
        if visibility not in {"shared", "private", "delayed"}:
            raise ValueError("语音可见范围无效")
        reveal_at = None
        if visibility == "delayed":
            reveal_at = self._voice_datetime(payload.get("reveal_at"), "公开时间")
            if reveal_at <= _now():
                raise ValueError("公开时间必须晚于当前时间")
        elif payload.get("reveal_at") not in {None, ""}:
            raise ValueError("仅延时公开内容可以设置公开时间")
        duration_ms = _integer(payload.get("duration_ms"), "录音时长", 1, 6 * 60 * 60 * 1000)
        transcript_payload = payload.get("transcript")
        transcript_values: dict[str, str] | None = None
        if transcript_payload is not None:
            if not isinstance(transcript_payload, dict) or set(transcript_payload) - {"body", "language", "method", "status"}:
                raise ValueError("转写内容无效")
            method = str(transcript_payload.get("method") or "manual").strip()
            status = str(transcript_payload.get("status") or "draft").strip()
            body = str(transcript_payload.get("body") or "")
            language = str(transcript_payload.get("language") or "zh-CN").strip()
            if method not in {"manual", "browser"} or status not in {"draft", "ready", "failed"}:
                raise ValueError("转写来源或状态无效")
            if len(body) > 10_000 or not language or len(language) > 30:
                raise ValueError("转写文字或语言无效")
            if status == "ready" and not body.strip():
                raise ValueError("已完成转写不能为空")
            if method == "manual" and not body.strip():
                status = "draft"
            transcript_values = {
                "body": body,
                "language": language,
                "method": method,
                "status": status,
                # Browser speech recognition is local, client-declared text.
                # The server cannot attest that a model produced it, so it
                # must never be promoted to a verified external/API source.
                "source_type": "human",
            }
        request_hash = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
        now = _now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._memory_couple_context(db, user_id)
            replay = self._idempotent_result(db, couple["id"], user_id, "voice.create", key)
            if replay:
                if replay.get("request_hash") != request_hash:
                    raise ConflictError("幂等键已用于另一条语音回忆")
                return dict(replay["response"])
            media = db.execute(
                """SELECT * FROM media_assets
                   WHERE id=? AND couple_id=? AND status='ready' AND purged_at IS NULL""",
                (media_id, couple["id"]),
            ).fetchone()
            if not media:
                raise KeyError("音频不存在")
            if media["owner_id"] != user_id:
                raise PermissionError("只能使用自己上传的音频")
            if media["kind"] != "audio":
                raise ValueError("语音回忆必须使用音频文件")
            if db.execute("SELECT 1 FROM moments WHERE media_id=? AND purged_at IS NULL", (media_id,)).fetchone():
                raise ConflictError("这段音频已经保存为回忆")
            moment_id = _id("moment")
            db.execute(
                """INSERT INTO moments(
                       id,couple_id,creator_id,type,title,body,occurred_at,location,mood,
                       visibility,reveal_at,media_url,media_id
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    moment_id, couple["id"], user_id, "voice", title, note, occurred_at,
                    str(payload.get("location") or "")[:200] or None,
                    str(payload.get("mood") or "")[:50] or None,
                    visibility, reveal_at, f"/api/v1/media/{media_id}", media_id,
                ),
            )
            db.execute("UPDATE media_assets SET duration_ms=? WHERE id=?", (duration_ms, media_id))
            transcript_id = None
            if transcript_values is not None:
                transcript_id = _id("transcript")
                db.execute(
                    """INSERT INTO voice_transcripts(
                           id,couple_id,owner_id,media_id,moment_id,body,language,method,
                           source_type,status,visibility,reveal_at,version,created_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (
                        transcript_id, couple["id"], user_id, media_id, moment_id,
                        transcript_values["body"], transcript_values["language"],
                        transcript_values["method"], transcript_values["source_type"],
                        transcript_values["status"], visibility, reveal_at, now, now,
                    ),
                )
            self._upsert_memory_provenance_in_connection(
                db, "moment", moment_id, couple["id"], user_id,
                source_type="human", date_source="user_confirmed", resource_version=1,
            )
            self._sync_media_visibility(db, media_id)
            self._audit(db, "voice_memory.created", "moment", moment_id, {"visibility": visibility}, actor_id=user_id)
            self._event(
                db, couple["id"], user_id, "moment.created", moment_id,
                {"type": "voice", "visibility": visibility, "owner_only": visibility != "shared"},
            )
            moment = db.execute("SELECT * FROM moments WHERE id=?", (moment_id,)).fetchone()
            media = db.execute("SELECT * FROM media_assets WHERE id=?", (media_id,)).fetchone()
            transcript = db.execute("SELECT * FROM voice_transcripts WHERE id=?", (transcript_id,)).fetchone() if transcript_id else None
            response = {
                "moment": self._public_moment(moment),
                "media": self._media_public(media),
                "transcript": self._public_voice_transcript(transcript) if transcript else None,
            }
            self._save_idempotent_result(
                db, couple["id"], user_id, "voice.create", key,
                {"request_hash": request_hash, "response": response},
            )
            return response

    def get_voice_memory(self, moment_id: str, user_id: str) -> dict[str, Any]:
        with self.connection() as db:
            parent = self._visible_memory_parent(db, "moment", moment_id, user_id)
            if parent["type"] != "voice" or not parent.get("media_id"):
                raise KeyError("语音回忆不存在")
            media = db.execute(
                """SELECT * FROM media_assets
                   WHERE id=? AND couple_id=? AND kind='audio' AND status='ready'
                     AND deleted_at IS NULL AND purged_at IS NULL""",
                (parent["media_id"], parent["couple_id"]),
            ).fetchone()
            if not media or (media["owner_id"] != user_id and media["visibility"] != "shared"):
                raise KeyError("语音回忆不存在")
            return {
                "moment": self._public_moment(parent),
                "media": self._media_public(media),
                "transcript": self._voice_transcript_for_viewer(db, parent, media, user_id),
            }

    def update_voice_transcript(
        self, transcript_id: str, payload: dict[str, Any], user_id: str
    ) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) - {"body", "language", "expected_version"}:
            raise ValueError("转写修改内容无效")
        expected = _integer(payload.get("expected_version"), "转写版本", 1, 2_147_483_647)
        body = str(payload.get("body") or "")
        language = str(payload.get("language") or "zh-CN").strip()
        if len(body) > 10_000 or not language or len(language) > 30:
            raise ValueError("转写文字或语言无效")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM voice_transcripts WHERE id=?", (transcript_id,)).fetchone()
            if not row:
                raise KeyError("语音转写不存在")
            if row["owner_id"] != user_id:
                raise PermissionError("只能修改自己的语音转写")
            self._visible_memory_parent(db, "moment", row["moment_id"], user_id)
            if int(row["version"]) != expected:
                raise ConflictError("语音转写已经变化，请刷新后重试")
            changed = db.execute(
                """UPDATE voice_transcripts SET body=?,language=?,method='manual',source_type='human',
                           status=?,version=version+1,updated_at=?
                   WHERE id=? AND owner_id=? AND version=?""",
                (body, language, "ready" if body.strip() else "draft", _now(), transcript_id, user_id, expected),
            ).rowcount
            if changed != 1:
                raise ConflictError("语音转写已经变化，请刷新后重试")
            updated = db.execute("SELECT * FROM voice_transcripts WHERE id=?", (transcript_id,)).fetchone()
            self._audit(db, "voice_transcript.updated", "voice_transcript", transcript_id, {"version": updated["version"]}, actor_id=user_id)
            return self._public_voice_transcript(updated)

    @staticmethod
    def _upsert_memory_provenance_in_connection(
        db: sqlite3.Connection,
        resource_type: str,
        resource_id: str,
        couple_id: str,
        owner_id: str | None,
        *,
        source_type: str,
        date_source: str,
        resource_version: int,
    ) -> None:
        target = {"moment": "moment_id", "media": "media_id", "trip": "trip_id"}.get(resource_type)
        if target is None:
            raise ValueError("回忆来源类型无效")
        existing = db.execute(f"SELECT id FROM memory_provenance WHERE {target}=?", (resource_id,)).fetchone()
        now = _now()
        safe_evidence: dict[str, object] = {"resource_version": int(resource_version), "confidence": 1.0}
        if date_source == "media_capture":
            safe_evidence["provider"] = "exif"
        evidence = _json(safe_evidence)
        if existing:
            db.execute(
                """UPDATE memory_provenance SET source_type=?,date_source=?,evidence_json=?,
                   version=version+1,updated_at=? WHERE id=?""",
                (source_type, date_source, evidence, now, existing["id"]),
            )
        else:
            db.execute(
                f"""INSERT INTO memory_provenance(
                    id,couple_id,owner_id,{target},source_type,date_source,evidence_json,
                    version,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,1,?,?)""",
                (_id("provenance"), couple_id, owner_id, resource_id, source_type, date_source, evidence, now, now),
            )
