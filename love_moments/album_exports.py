"""Private fixed-template album export jobs.

This module owns the short-lived job state.  All selected content is resolved
through ``MemoryMixin.resolve_memory_for_audience``; the job rows retain IDs and
versions only, never memory text or media paths.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .intelligent_ui import ConflictError
from .album_pdf import render_album_pdf
from .memories import DATE_SOURCE_LABELS, SOURCE_LABELS


_RESOURCE_TYPES = frozenset({"moment", "media"})
_FAILURE_CODES = frozenset(
    {"selection_changed", "permission_changed", "media_missing", "render_failed", "expired", "cancelled"}
)
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,160}$")


class AlbumExportConflict(ConflictError):
    def __init__(self, message: str, code: str = "ALBUM_EXPORT_CONFLICT"):
        super().__init__(message)
        self.code = code


def _now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AlbumExportMixin:
    @staticmethod
    def _assert_album_resource_supported(
        db: sqlite3.Connection, resource_type: str, resource_id: str,
    ) -> None:
        # Delivered future letters remain usable in the ordinary timeline, but
        # are intentionally outside printable albums so a converted copy cannot
        # bypass the letter-specific disclosure contract.
        if resource_type == "moment" and db.execute(
            "SELECT 1 FROM future_letters WHERE delivered_moment_id=? LIMIT 1", (resource_id,),
        ).fetchone():
            raise KeyError("回忆不存在")

    def _delete_album_artifacts(self, job_id: str) -> None:
        if not _ID_RE.fullmatch(str(job_id or "")):
            return
        for suffix in (".tmp", ".pdf"):
            candidate = self.album_export_root / f"{job_id}{suffix}"
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _album_scope_binding(scope: dict[str, Any]) -> dict[str, Any]:
        return {
            "template": scope["template"], "year": scope["year"],
            "trip_id": scope["trip_id"], "start_at": scope["start_at"], "end_at": scope["end_at"],
            "trip_version": int(scope["trip"]["version"]) if scope.get("trip") else None,
        }

    @staticmethod
    def _public_album_job(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        allowed = (
            "id", "template", "year", "trip_id", "audience", "status", "failure_code",
            "size_bytes", "version", "expires_at", "created_at", "updated_at", "completed_at",
        )
        value = {key: row[key] for key in allowed if key in row.keys()}
        value["download_ready"] = value.get("status") == "completed"
        value["temporary_artifact"] = True
        try:
            selections = json.loads(row["selection_json"])
            value["warnings"] = sorted({
                code for item in selections for code in item.get("warnings", [])
                if code == "media_missing"
            })
        except (KeyError, TypeError, json.JSONDecodeError):
            value["warnings"] = []
        return value

    def _album_scope(
        self, db: sqlite3.Connection, payload: dict[str, Any], user_id: str,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("纪念册参数无效")
        template = str(payload.get("template") or "").strip()
        audience = str(payload.get("audience") or "personal").strip()
        if template not in {"year", "trip"} or audience not in {"personal", "shared"}:
            raise ValueError("纪念册模板或范围无效")
        couple, _ = self._memory_couple_context(db, user_id)
        user = db.execute(
            "SELECT timezone FROM users WHERE id=? AND deleted_at IS NULL", (user_id,),
        ).fetchone()
        if not user:
            raise KeyError("账号不存在")
        try:
            user_zone = ZoneInfo(str(user["timezone"]))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("账号时区无效，请先更新时区") from exc
        result: dict[str, Any] = {
            "template": template, "audience": audience, "couple_id": couple["id"],
            "year": None, "trip_id": None, "trip": None,
        }
        if template == "year":
            if set(payload).difference({"template", "audience", "year", "selection", "expected_versions"}):
                raise ValueError("纪念册参数包含未知字段")
            year_value = payload.get("year")
            if isinstance(year_value, bool):
                raise ValueError("年份无效")
            try:
                year = int(year_value)
            except (TypeError, ValueError) as exc:
                raise ValueError("年份无效") from exc
            if year < 2000 or year > 2100 or str(year_value).strip() != str(year):
                raise ValueError("年份必须在 2000–2100 之间")
            start_local = datetime.combine(date(year, 1, 1), time.min, tzinfo=user_zone)
            end_local = datetime.combine(date(year + 1, 1, 1), time.min, tzinfo=user_zone)
            result.update(
                year=year, start_at=_iso(start_local), end_at=_iso(end_local),
                title=f"{year} 年恋爱纪念册", date_range=f"{year}-01-01 — {year}-12-31",
            )
            return result
        if set(payload).difference({"template", "audience", "trip_id", "selection", "expected_versions"}):
            raise ValueError("纪念册参数包含未知字段")
        trip_id = str(payload.get("trip_id") or "").strip()
        if not _ID_RE.fullmatch(trip_id):
            raise ValueError("旅行无效")
        trip = db.execute(
            """SELECT id,title,destination,arrival_city,start_date,end_date,version
               FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL""",
            (trip_id, couple["id"]),
        ).fetchone()
        if not trip:
            raise KeyError("旅行不存在")
        start_day = date.fromisoformat(trip["start_date"])
        end_day = date.fromisoformat(trip["end_date"])
        start_local = datetime.combine(start_day, time.min, tzinfo=user_zone)
        end_local = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=user_zone)
        result.update(
            trip_id=trip_id, trip={
                "id": trip_id, "title": trip["title"],
                "destination": trip["destination"] or trip["arrival_city"],
                "start_date": trip["start_date"], "end_date": trip["end_date"],
                "version": int(trip["version"]),
            },
            start_at=_iso(start_local), end_at=_iso(end_local),
            title=f"{trip['title']} · 旅行纪念册",
            date_range=f"{trip['start_date']} — {trip['end_date']}",
        )
        return result

    @staticmethod
    def _preview_album_item(item: dict[str, Any]) -> dict[str, Any]:
        is_private = item.get("visibility") != "shared"
        return {
            "resource_type": item["resource_type"], "resource_id": item["resource_id"],
            "title": item.get("title") or ("照片" if item["resource_type"] == "media" else "未命名回忆"),
            "summary": str(item.get("body") or "")[:160], "event_at": item.get("event_at"),
            "location": item.get("location"), "visibility": item.get("visibility", "shared"),
            "source_type": item.get("source_type", "legacy_unknown"),
            "date_source": item.get("date_source", "legacy_unknown"),
            "version": int(item["version"]), "default_selected": not is_private,
            "has_image": bool(item.get("media") and item["media"].get("kind") == "image"),
        }

    @staticmethod
    def _assert_album_item_in_scope(scope: dict[str, Any], item: dict[str, Any]) -> None:
        try:
            event_at = datetime.fromisoformat(str(item.get("event_at") or ""))
            start_at = datetime.fromisoformat(str(scope["start_at"]))
            end_at = datetime.fromisoformat(str(scope["end_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("回忆时间无效，无法加入纪念册") from exc
        if event_at.tzinfo is None or start_at.tzinfo is None or end_at.tzinfo is None:
            raise ValueError("回忆时间无效，无法加入纪念册")
        if not start_at <= event_at.astimezone(timezone.utc) < end_at:
            raise ValueError("所选回忆不在纪念册日期范围内")

    def preview_album_export(self, payload: dict[str, Any], user_id: str) -> dict[str, Any]:
        with self.connection() as db:
            scope = self._album_scope(db, payload, user_id)
            candidates = self.list_memory_candidates_for_album(
                db, user_id=user_id, audience=scope["audience"],
                start_at=scope["start_at"], end_at=scope["end_at"], limit=121,
            )
            items = [self._preview_album_item(item) for item in candidates[:120]]
            return {
                "template": scope["template"], "audience": scope["audience"],
                "year": scope["year"], "trip_id": scope["trip_id"], "trip": scope["trip"],
                "title": scope["title"], "date_range": scope["date_range"],
                "date_filter_notice": "按旅行日期筛选，请在预览中确认" if scope["template"] == "trip" else None,
                "total": len(items), "truncated": len(candidates) > 120, "items": items,
            }

    @staticmethod
    def _normalize_album_selection(payload: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, int]]:
        raw_selection = payload.get("selection")
        raw_versions = payload.get("expected_versions")
        if not isinstance(raw_selection, list) or not raw_selection or not isinstance(raw_versions, dict):
            raise ValueError("请至少选择一条回忆并提供版本")
        normalized: dict[str, dict[str, str]] = {}
        for item in raw_selection:
            if not isinstance(item, dict) or set(item) != {"resource_type", "resource_id"}:
                raise ValueError("纪念册选择项无效")
            resource_type = str(item.get("resource_type") or "")
            resource_id = str(item.get("resource_id") or "")
            if resource_type not in _RESOURCE_TYPES or not _ID_RE.fullmatch(resource_id):
                raise ValueError("纪念册选择项无效")
            normalized[f"{resource_type}:{resource_id}"] = {
                "resource_type": resource_type, "resource_id": resource_id,
            }
        if len(normalized) > 120:
            raise ValueError("每本纪念册最多选择 120 项")
        expected_keys = set(normalized)
        if set(raw_versions) != expected_keys:
            raise ValueError("回忆版本与选择项不一致")
        versions: dict[str, int] = {}
        for key in expected_keys:
            value = raw_versions[key]
            if isinstance(value, bool):
                raise ValueError("回忆版本无效")
            try:
                version = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("回忆版本无效") from exc
            if version < 1 or str(value).strip() != str(version):
                raise ValueError("回忆版本无效")
            versions[key] = version
        selection = [normalized[key] for key in sorted(normalized)]
        return selection, {key: versions[key] for key in sorted(versions)}

    def _validate_album_selection(
        self,
        db: sqlite3.Connection,
        selection: list[dict[str, str]],
        expected_versions: dict[str, int],
        user_id: str,
        audience: str,
        scope: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], int]:
        internal: list[dict[str, Any]] = []
        image_ids: set[str] = set()
        for selected in selection:
            key = f"{selected['resource_type']}:{selected['resource_id']}"
            self._assert_album_resource_supported(
                db, selected["resource_type"], selected["resource_id"],
            )
            resolved = self.resolve_memory_for_audience(
                db, selected["resource_type"], selected["resource_id"], user_id,
                audience=audience, expected_version=expected_versions[key],
            )
            self._assert_album_item_in_scope(scope, resolved)
            media = resolved.get("media")
            if media and media.get("kind") == "image":
                image_ids.add(str(media["id"]))
            internal.append({
                **selected,
                "dependency_versions": resolved["dependency_versions"],
            })
        if len(image_ids) > 80:
            raise ValueError("每本纪念册最多包含 80 张图片")
        return internal, len(image_ids)

    def create_album_export_job(
        self, payload: dict[str, Any], user_id: str, idempotency_key: str,
    ) -> dict[str, Any]:
        key = str(idempotency_key or "").strip()
        if not _KEY_RE.fullmatch(key):
            raise ValueError("Idempotency-Key 必须为 8–160 位安全字符")
        selection, expected_versions = self._normalize_album_selection(payload)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            scope = self._album_scope(db, payload, user_id)
            canonical = {
                "template": scope["template"], "year": scope["year"], "trip_id": scope["trip_id"],
                "audience": scope["audience"], "selection": selection,
                "expected_versions": expected_versions,
            }
            existing = db.execute(
                "SELECT * FROM album_export_jobs WHERE couple_id=? AND creator_id=? AND client_request_id=?",
                (scope["couple_id"], user_id, key),
            ).fetchone()
            if existing:
                current = {
                    "template": existing["template"], "year": existing["year"], "trip_id": existing["trip_id"],
                    "audience": existing["audience"],
                    "selection": [
                        {"resource_type": item["resource_type"], "resource_id": item["resource_id"]}
                        for item in json.loads(existing["selection_json"])
                    ],
                    "expected_versions": json.loads(existing["expected_versions_json"]),
                }
                if current != canonical:
                    raise ConflictError("该请求标识已用于其他纪念册")
                return self._public_album_job(existing)
            internal, _ = self._validate_album_selection(
                db, selection, expected_versions, user_id, scope["audience"], scope,
            )
            scope_binding = self._album_scope_binding(scope)
            for item in internal:
                item["scope_binding"] = scope_binding
            now = _now_dt()
            job_id = f"album_{uuid.uuid4().hex}"
            db.execute(
                """INSERT INTO album_export_jobs(
                       id,couple_id,creator_id,template,year,trip_id,audience,
                       selection_json,expected_versions_json,status,client_request_id,
                       version,expires_at,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,'queued',?,1,?,?,?)""",
                (
                    job_id, scope["couple_id"], user_id, scope["template"], scope["year"], scope["trip_id"],
                    scope["audience"], _json(internal), _json(expected_versions), key,
                    _iso(now + timedelta(hours=24)), _iso(now), _iso(now),
                ),
            )
            self._audit(
                db, "album_export.created", "album_export", job_id,
                {"template": scope["template"], "audience": scope["audience"], "item_count": len(internal)},
                actor_id=user_id,
            )
            row = db.execute("SELECT * FROM album_export_jobs WHERE id=?", (job_id,)).fetchone()
            return self._public_album_job(row)

    def list_album_export_jobs(self, user_id: str) -> dict[str, Any]:
        self.cleanup_expired_album_exports()
        invalid: list[str] = []
        with self.connection() as db:
            couple, _ = self._memory_couple_context(db, user_id)
            rows = db.execute(
                """SELECT * FROM album_export_jobs
                   WHERE couple_id=? AND creator_id=? ORDER BY created_at DESC,id DESC LIMIT 50""",
                (couple["id"], user_id),
            ).fetchall()
            for row in rows:
                if row["status"] != "completed":
                    continue
                try:
                    self._read_verified_album_artifact(db, row)
                except (KeyError, ValueError, ConflictError, OSError, json.JSONDecodeError):
                    invalid.append(str(row["id"]))
        for job_id in invalid:
            self._expire_album_job(job_id, "permission_changed")
        if invalid:
            with self.connection() as db:
                couple, _ = self._memory_couple_context(db, user_id)
                rows = db.execute(
                    """SELECT * FROM album_export_jobs
                       WHERE couple_id=? AND creator_id=? ORDER BY created_at DESC,id DESC LIMIT 50""",
                    (couple["id"], user_id),
                ).fetchall()
        return {"items": [self._public_album_job(row) for row in rows]}

    def get_album_export_job(self, job_id: str, user_id: str) -> dict[str, Any]:
        if not _ID_RE.fullmatch(str(job_id or "")):
            raise KeyError("纪念册任务不存在")
        with self.connection() as db:
            couple, _ = self._memory_couple_context(db, user_id)
            row = db.execute(
                "SELECT * FROM album_export_jobs WHERE id=? AND couple_id=? AND creator_id=?",
                (job_id, couple["id"], user_id),
            ).fetchone()
            if not row:
                raise KeyError("纪念册任务不存在")
            invalid = False
            if row["status"] == "completed":
                try:
                    self._read_verified_album_artifact(db, row)
                except (KeyError, ValueError, ConflictError, OSError, json.JSONDecodeError):
                    invalid = True
        if invalid:
            self._expire_album_job(job_id, "permission_changed")
            with self.connection() as db:
                couple, _ = self._memory_couple_context(db, user_id)
                row = db.execute(
                    "SELECT * FROM album_export_jobs WHERE id=? AND couple_id=? AND creator_id=?",
                    (job_id, couple["id"], user_id),
                ).fetchone()
                if not row:
                    raise KeyError("纪念册任务不存在")
        return self._public_album_job(row)

    def cancel_album_export_job(self, job_id: str, expected_version: int, user_id: str) -> dict[str, Any]:
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("纪念册任务版本必须为正整数")
        with self._album_artifact_lock:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                couple, _ = self._memory_couple_context(db, user_id)
                row = db.execute(
                    "SELECT * FROM album_export_jobs WHERE id=? AND couple_id=? AND creator_id=?",
                    (job_id, couple["id"], user_id),
                ).fetchone()
                if not row:
                    raise KeyError("纪念册任务不存在")
                if int(row["version"]) != expected_version:
                    raise ConflictError("纪念册任务已经变化")
                if row["status"] in {"cancelled", "expired", "failed"}:
                    return self._public_album_job(row)
                now = _iso(_now_dt())
                updated = db.execute(
                    """UPDATE album_export_jobs SET status='cancelled',failure_code='cancelled',
                              artifact_relative_path=NULL,artifact_sha256=NULL,size_bytes=NULL,
                              version=version+1,updated_at=?
                       WHERE id=? AND version=? AND status IN ('queued','running','completed')""",
                    (now, job_id, expected_version),
                )
                if updated.rowcount != 1:
                    raise ConflictError("纪念册任务已经变化")
                result = db.execute("SELECT * FROM album_export_jobs WHERE id=?", (job_id,)).fetchone()
                self._audit(
                    db, "album_export.cancelled", "album_export", job_id,
                    {"status": "cancelled"}, actor_id=user_id,
                )
            self._delete_album_artifacts(job_id)
            return self._public_album_job(result)

    def claim_album_export_job(self) -> dict[str, Any] | None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _iso(_now_dt())
            row = db.execute(
                """SELECT * FROM album_export_jobs
                   WHERE status='queued' AND expires_at>? ORDER BY created_at,id LIMIT 1""",
                (now,),
            ).fetchone()
            if not row:
                return None
            updated = db.execute(
                """UPDATE album_export_jobs SET status='running',version=version+1,updated_at=?
                   WHERE id=? AND status='queued' AND version=?""",
                (now, row["id"], row["version"]),
            )
            if updated.rowcount != 1:
                return None
            claimed = db.execute("SELECT * FROM album_export_jobs WHERE id=?", (row["id"],)).fetchone()
            return dict(claimed)

    def _revalidate_album_job(
        self, db: sqlite3.Connection, row: sqlite3.Row | dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        couple, _ = self._memory_couple_context(db, row["creator_id"])
        if couple["id"] != row["couple_id"]:
            raise KeyError("纪念册权限已经变化")
        scope_payload: dict[str, Any] = {
            "template": row["template"], "audience": row["audience"],
        }
        if row["template"] == "year":
            scope_payload["year"] = row["year"]
        else:
            scope_payload["trip_id"] = row["trip_id"]
        scope = self._album_scope(db, scope_payload, row["creator_id"])
        selection = json.loads(row["selection_json"])
        expected = json.loads(row["expected_versions_json"])
        scope_binding = self._album_scope_binding(scope)
        if any(item.get("scope_binding") != scope_binding for item in selection):
            raise ConflictError("纪念册范围已经变化")
        resolved_items: list[dict[str, Any]] = []
        for selected in selection:
            resource_type = selected.get("resource_type")
            resource_id = selected.get("resource_id")
            key = f"{resource_type}:{resource_id}"
            if resource_type not in _RESOURCE_TYPES or key not in expected:
                raise ConflictError("纪念册选择已经变化")
            self._assert_album_resource_supported(db, resource_type, resource_id)
            resolved = self.resolve_memory_for_audience(
                db, resource_type, resource_id, row["creator_id"],
                audience=row["audience"], expected_version=int(expected[key]),
            )
            self._assert_album_item_in_scope(scope, resolved)
            if resolved["dependency_versions"] != selected.get("dependency_versions"):
                raise ConflictError("纪念册内容已经变化")
            resolved_items.append(resolved)
        return scope, resolved_items

    def _read_album_media_local(self, db: sqlite3.Connection, media_id: str) -> tuple[bytes | None, str | None]:
        row = db.execute(
            """SELECT id,mime_type,thumbnail_path,thumbnail_checksum,relative_path,storage_key,checksum
               FROM media_assets WHERE id=? AND status='ready' AND deleted_at IS NULL AND purged_at IS NULL""",
            (media_id,),
        ).fetchone()
        if not row:
            return None, "media_missing"
        candidates = (
            (row["thumbnail_path"], row["thumbnail_checksum"]),
            (row["storage_key"] or row["relative_path"], row["checksum"]),
        )
        for key, checksum in candidates:
            if not key:
                continue
            try:
                value = self.local_storage.get(str(key))
            except (KeyError, OSError, ValueError):
                continue
            if checksum and hashlib.sha256(value).hexdigest() != checksum:
                continue
            return value, None
        return None, "media_missing"

    def _album_document(
        self,
        db: sqlite3.Connection,
        scope: dict[str, Any],
        resolved_items: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[str]]:
        warnings: list[str] = []
        items: list[dict[str, Any]] = []
        for item in resolved_items:
            image_bytes = None
            media = item.get("media")
            if media and media.get("kind") == "image":
                image_bytes, warning = self._read_album_media_local(db, str(media["id"]))
                if warning and warning not in warnings:
                    warnings.append(warning)
            items.append({
                "resource_type": item["resource_type"], "resource_id": item["resource_id"],
                "event_at": item.get("event_at"), "title": item.get("title"), "body": item.get("body"),
                "location": item.get("location"), "visibility": item.get("visibility"),
                "source_label": SOURCE_LABELS.get(item.get("source_type"), "来源未记录"),
                "date_source_label": DATE_SOURCE_LABELS.get(item.get("date_source"), "时间来源未记录"),
                "descriptions": [
                    {"nickname": description.get("nickname"), "body": description.get("body")}
                    for description in item.get("descriptions", [])
                ],
                "image_bytes": image_bytes,
                "image_mime_type": media.get("mime_type") if media else None,
                "expects_image": bool(media and media.get("kind") == "image"),
            })
        return {
            "template": scope["template"], "title": scope["title"],
            "date_range": scope["date_range"], "audience": scope["audience"],
            "generated_at": _iso(_now_dt()), "items": items,
        }, warnings

    def _fail_album_job(self, job_id: str, failure_code: str) -> dict[str, Any]:
        if failure_code not in _FAILURE_CODES:
            failure_code = "render_failed"
        with self._album_artifact_lock:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    """UPDATE album_export_jobs SET status='failed',failure_code=?,
                              artifact_relative_path=NULL,artifact_sha256=NULL,size_bytes=NULL,
                              version=version+1,updated_at=?
                       WHERE id=? AND status IN ('queued','running')""",
                    (failure_code, _iso(_now_dt()), job_id),
                )
                row = db.execute("SELECT * FROM album_export_jobs WHERE id=?", (job_id,)).fetchone()
            self._delete_album_artifacts(job_id)
            if not row:
                raise KeyError("纪念册任务不存在")
            return self._public_album_job(row)

    def run_album_export_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job.get("id") or "")
        try:
            with self.connection() as db:
                row = db.execute("SELECT * FROM album_export_jobs WHERE id=? AND status='running'", (job_id,)).fetchone()
                if not row:
                    raise KeyError("纪念册任务不存在")
                scope, items = self._revalidate_album_job(db, row)
                document, warnings = self._album_document(db, scope, items)
        except ConflictError:
            return self._fail_album_job(job_id, "selection_changed")
        except (KeyError, ValueError):
            return self._fail_album_job(job_id, "permission_changed")
        except Exception:
            return self._fail_album_job(job_id, "render_failed")
        try:
            pdf_bytes, render_warnings = render_album_pdf(document)
            warnings = sorted(set(warnings + render_warnings))
        except Exception:
            return self._fail_album_job(job_id, "render_failed")

        temp_path = self.album_export_root / f"{job_id}.tmp"
        final_path = self.album_export_root / f"{job_id}.pdf"
        with temp_path.open("wb") as handle:
            handle.write(pdf_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temp_path.chmod(0o600)
        digest = hashlib.sha256(pdf_bytes).hexdigest()
        with self._album_artifact_lock:
            try:
                with self.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute("SELECT * FROM album_export_jobs WHERE id=? AND status='running'", (job_id,)).fetchone()
                    if not current:
                        raise ConflictError("纪念册任务已经变化")
                    self._revalidate_album_job(db, current)
                    internal = json.loads(current["selection_json"])
                    if warnings:
                        for item in internal:
                            item["warnings"] = warnings
                    os.replace(temp_path, final_path)
                    if os.name != "nt":
                        final_path.chmod(0o600)
                    now = _now_dt()
                    updated = db.execute(
                        """UPDATE album_export_jobs SET status='completed',selection_json=?,
                                  artifact_relative_path=?,artifact_sha256=?,size_bytes=?,failure_code=NULL,
                                  completed_at=?,expires_at=?,updated_at=?,version=version+1
                           WHERE id=? AND status='running' AND version=?""",
                        (
                            _json(internal), f"temp/album_exports/{job_id}.pdf", digest, len(pdf_bytes),
                            _iso(now), _iso(now + timedelta(hours=1)), _iso(now), job_id, current["version"],
                        ),
                    )
                    if updated.rowcount != 1:
                        raise ConflictError("纪念册任务已经变化")
                    self._audit(
                        db, "album_export.completed", "album_export", job_id,
                        {"item_count": len(internal), "size_bytes": len(pdf_bytes), "warnings": warnings},
                        actor_id=current["creator_id"],
                    )
                    result = db.execute("SELECT * FROM album_export_jobs WHERE id=?", (job_id,)).fetchone()
                return self._public_album_job(result)
            except (ConflictError, KeyError, ValueError):
                final_path.unlink(missing_ok=True)
                temp_path.unlink(missing_ok=True)
                return self._fail_album_job(job_id, "permission_changed")

    def _expire_album_job(self, job_id: str, failure_code: str = "expired") -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """UPDATE album_export_jobs SET status='expired',failure_code=?,
                          artifact_relative_path=NULL,artifact_sha256=NULL,size_bytes=NULL,
                          version=version+1,updated_at=?
                   WHERE id=? AND status='completed'""",
                (failure_code, _iso(_now_dt()), job_id),
            )
        self._delete_album_artifacts(job_id)

    def _read_verified_album_artifact(
        self, db: sqlite3.Connection, row: sqlite3.Row | dict[str, Any],
    ) -> bytes:
        if row["status"] != "completed":
            raise KeyError("纪念册任务不存在")
        if _now_dt() >= datetime.fromisoformat(str(row["expires_at"])):
            raise KeyError("纪念册任务不存在")
        self._revalidate_album_job(db, row)
        job_id = str(row["id"])
        relative = str(row["artifact_relative_path"] or "")
        expected = f"temp/album_exports/{job_id}.pdf"
        path = (self.media_root / relative).resolve()
        root = self.album_export_root.resolve()
        if relative != expected or path.parent != root or path.is_symlink() or not path.is_file():
            raise KeyError("纪念册任务不存在")
        data = path.read_bytes()
        if (
            len(data) != int(row["size_bytes"] or -1)
            or hashlib.sha256(data).hexdigest() != row["artifact_sha256"]
        ):
            raise KeyError("纪念册任务不存在")
        return data

    def get_album_export_download(self, job_id: str, user_id: str) -> tuple[bytes, str]:
        with self._album_artifact_lock:
            with self.connection() as db:
                couple, _ = self._memory_couple_context(db, user_id)
                row = db.execute(
                    """SELECT * FROM album_export_jobs
                       WHERE id=? AND couple_id=? AND creator_id=?""",
                    (job_id, couple["id"], user_id),
                ).fetchone()
                if not row:
                    raise KeyError("纪念册任务不存在")
                if row["status"] in {"queued", "running"}:
                    raise AlbumExportConflict("纪念册仍在生成", "ALBUM_NOT_READY")
                if row["status"] != "completed":
                    raise KeyError("纪念册任务不存在")
                try:
                    data = self._read_verified_album_artifact(db, row)
                    revoke = False
                except (KeyError, ValueError, ConflictError, OSError, json.JSONDecodeError):
                    revoke = True
                if not revoke:
                    self._audit(
                        db, "album_export.downloaded", "album_export", job_id,
                        {"size_bytes": len(data)}, actor_id=user_id,
                    )
            if revoke:
                self._expire_album_job(job_id, "permission_changed")
                raise KeyError("纪念册任务不存在")
            return data, "love-moments-album.pdf"

    def recover_album_export_jobs(self) -> dict[str, int]:
        now = _now_dt()
        recovered = expired = invalid = 0
        remove_ids: set[str] = set()
        with self._album_artifact_lock:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                rows = db.execute("SELECT * FROM album_export_jobs").fetchall()
                for row in rows:
                    expires = datetime.fromisoformat(row["expires_at"])
                    if row["status"] == "running" and expires > now and not row["artifact_relative_path"]:
                        db.execute("UPDATE album_export_jobs SET status='queued',version=version+1,updated_at=? WHERE id=?", (_iso(now), row["id"]))
                        recovered += 1
                    elif row["status"] in {"queued", "running", "completed"} and expires <= now:
                        db.execute(
                            """UPDATE album_export_jobs SET status='expired',failure_code='expired',
                                      artifact_relative_path=NULL,artifact_sha256=NULL,size_bytes=NULL,
                                      version=version+1,updated_at=? WHERE id=?""",
                            (_iso(now), row["id"]),
                        )
                        expired += 1
                        remove_ids.add(str(row["id"]))
                    elif row["status"] == "completed":
                        expected = f"temp/album_exports/{row['id']}.pdf"
                        relative = str(row["artifact_relative_path"] or "")
                        path = (self.media_root / relative).resolve()
                        valid = (
                            relative == expected
                            and path.parent == self.album_export_root.resolve()
                            and not path.is_symlink()
                            and path.is_file()
                        )
                        if valid:
                            try:
                                payload = path.read_bytes()
                                valid = (
                                    len(payload) == int(row["size_bytes"] or -1)
                                    and hashlib.sha256(payload).hexdigest() == row["artifact_sha256"]
                                )
                            except OSError:
                                valid = False
                        if not valid:
                            db.execute(
                                """UPDATE album_export_jobs SET status='expired',failure_code='permission_changed',
                                          artifact_relative_path=NULL,artifact_sha256=NULL,size_bytes=NULL,
                                          version=version+1,updated_at=? WHERE id=? AND status='completed'""",
                                (_iso(now), row["id"]),
                            )
                            invalid += 1
                            remove_ids.add(str(row["id"]))
                    elif row["status"] in {"failed", "cancelled", "expired"}:
                        remove_ids.add(str(row["id"]))
            for job_id in remove_ids:
                self._delete_album_artifacts(job_id)
        return {"recovered": recovered, "expired": expired, "invalid": invalid}

    def cleanup_expired_album_exports(self, *, now: datetime | None = None) -> dict[str, int]:
        current = (now or _now_dt()).astimezone(timezone.utc).replace(microsecond=0)
        expired = removed = 0
        with self._album_artifact_lock:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                rows = db.execute(
                    "SELECT id FROM album_export_jobs WHERE status IN ('queued','running','completed') AND expires_at<=?",
                    (_iso(current),),
                ).fetchall()
                for row in rows:
                    db.execute(
                        """UPDATE album_export_jobs SET status='expired',failure_code='expired',
                                  artifact_relative_path=NULL,artifact_sha256=NULL,size_bytes=NULL,
                                  version=version+1,updated_at=? WHERE id=?""",
                        (_iso(current), row["id"]),
                    )
                    expired += 1
            live = set()
            with self.connection() as db:
                for row in db.execute("SELECT id FROM album_export_jobs WHERE status='completed' AND expires_at>?", (_iso(current),)).fetchall():
                    live.add(f"{row['id']}.pdf")
            for path in self.album_export_root.iterdir():
                if path.is_symlink():
                    path.unlink(missing_ok=True)
                    removed += 1
                elif path.is_file() and path.name not in live:
                    if path.suffix == ".pdf" or (path.suffix == ".tmp" and current.timestamp() - path.stat().st_mtime >= 7200):
                        path.unlink(missing_ok=True)
                        removed += 1
        return {"expired": expired, "removed": removed}
