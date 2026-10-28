"""Private self-hosted administration and diagnostics.

The admin surface deliberately exposes operational metadata only. It never
returns message bodies, moment bodies, media bytes, password hashes or tokens.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .intelligent_ui import ConflictError
from .schema import SCHEMA_VERSION, account_export_policy_status, schema_inventory
from .store import LoveMomentsStore, new_id, parse_json, utc_now
from .maintenance import verify_backup
from .ocr import real_ocr_configured


def _safe_detail(value: Any, depth: int = 0) -> Any:
    """Bound diagnostic values and remove fields that could contain secrets."""
    if depth > 3:
        return "[已省略]"
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:30]:
            key = str(raw_key)[:80]
            lowered = key.lower()
            if any(word in lowered for word in ("password", "secret", "token", "certificate", "api_key", "authorization", "cookie")):
                cleaned[key] = "[已隐藏]"
            else:
                cleaned[key] = _safe_detail(raw_value, depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_safe_detail(item, depth + 1) for item in list(value)[:30]]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    return str(value)[:500]


def _folder_metrics(path: Path) -> tuple[int, int]:
    count = 0
    size = 0
    service_files = {".media-access-key", ".web-push-vapid-private.pem"}
    if not path.is_dir():
        return count, size
    for item in path.rglob("*"):
        try:
            if item.is_file() and item.name not in service_files:
                count += 1
                size += item.stat().st_size
        except OSError:
            continue
    return count, size


class AdminService:
    def __init__(self, store_getter: Callable[[], LoveMomentsStore], root: Path):
        self._store_getter = store_getter
        self.root = root
        self.started_at = datetime.now(timezone.utc)

    @property
    def store(self) -> LoveMomentsStore:
        return self._store_getter()

    def backup_root(self) -> Path:
        configured = os.getenv("BACKUP_ROOT", "").strip()
        if configured:
            value = Path(configured)
            return value if value.is_absolute() else self.root / value
        database = Path(self.store.db_path).resolve()
        try:
            database.relative_to((self.root / "data").resolve())
        except ValueError:
            return database.parent / "backups"
        return self.root / "backups"

    def _state_root(self) -> Path:
        configured = os.getenv("MAINTENANCE_STATE_ROOT", "").strip()
        path = Path(configured) if configured else self.root / "data" / "maintenance"
        return path if path.is_absolute() else self.root / path

    @staticmethod
    def _local_notification(title: str, message: str) -> None:
        enabled = os.getenv("LOCAL_ADMIN_NOTIFICATIONS", "false").strip().lower() in {"1", "true", "yes", "on"}
        if not enabled or sys.platform != "darwin" or not Path("/usr/bin/osascript").is_file():
            return
        safe_title = str(title).replace("\\", "\\\\").replace('"', '\\"')[:80]
        safe_message = str(message).replace("\\", "\\\\").replace('"', '\\"')[:240]
        try:
            subprocess.run(
                ["/usr/bin/osascript", "-e", f'display notification "{safe_message}" with title "{safe_title}"'],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return

    def record_event(
        self,
        event_key: str,
        severity: str,
        category: str,
        summary: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        severity = severity if severity in {"info", "warning", "critical"} else "warning"
        key = str(event_key).strip()[:180]
        category = str(category).strip()[:60] or "service"
        summary = str(summary).strip()[:240] or "系统异常"
        if not key:
            key = hashlib.sha256(f"{category}:{summary}".encode()).hexdigest()
        now = utc_now()
        payload = json.dumps(_safe_detail(detail or {}), ensure_ascii=False, separators=(",", ":"))
        try:
            with self.store.connection() as db:
                row = db.execute(
                    """
                    INSERT INTO system_events(
                        id,event_key,severity,category,summary,detail_json,
                        occurrences,first_seen_at,last_seen_at,resolved_at,resolved_by
                    ) VALUES (?,?,?,?,?,?,1,?,?,NULL,NULL)
                    ON CONFLICT(event_key) DO UPDATE SET
                        severity=excluded.severity,
                        category=excluded.category,
                        summary=excluded.summary,
                        detail_json=excluded.detail_json,
                        occurrences=system_events.occurrences+1,
                        last_seen_at=excluded.last_seen_at,
                        resolved_at=NULL,
                        resolved_by=NULL
                    """,
                    (new_id("system"), key, severity, category, summary, payload, now, now),
                )
                current = db.execute("SELECT occurrences FROM system_events WHERE event_key=?", (key,)).fetchone()
                db.execute(
                    "DELETE FROM system_events WHERE id IN (SELECT id FROM system_events ORDER BY last_seen_at DESC LIMIT -1 OFFSET 1000)"
                )
            occurrences = int(current["occurrences"]) if current else 1
            if severity == "critical" and occurrences == 1:
                self._local_notification("恋屿出现严重异常", summary)
            elif severity == "warning" and occurrences == 3:
                self._local_notification("恋屿异常连续出现", summary)
        except Exception:
            # Diagnostics must never turn a recoverable API error into another failure.
            return

    def resolve_event(self, event_id: str, admin_user_id: str) -> dict:
        with self.store.connection() as db:
            changed = db.execute(
                "UPDATE system_events SET resolved_at=?,resolved_by=? WHERE id=? AND resolved_at IS NULL",
                (utc_now(), admin_user_id, event_id),
            ).rowcount
            if not db.execute("SELECT 1 FROM system_events WHERE id=?", (event_id,)).fetchone():
                raise KeyError("异常记录不存在")
        return {"id": event_id, "resolved": bool(changed)}

    def _resolve_event_key(self, event_key: str) -> None:
        """Automatically close a condition-based alert after the condition clears."""
        try:
            with self.store.connection() as db:
                db.execute(
                    "UPDATE system_events SET resolved_at=?,resolved_by=NULL WHERE event_key=? AND resolved_at IS NULL",
                    (utc_now(), event_key),
                )
        except Exception:
            return

    @staticmethod
    def _recovery_detail(detail_json: str, kind: str, message: str, verified_at: str) -> str:
        detail = parse_json(detail_json, {})
        if not isinstance(detail, dict):
            detail = {}
        detail["recovery"] = {
            "kind": kind,
            "message": str(message)[:240],
            "verified_at": verified_at,
        }
        return json.dumps(_safe_detail(detail), ensure_ascii=False, separators=(",", ":"))

    def _resolve_with_evidence(
        self,
        db,
        event: Any,
        *,
        kind: str,
        message: str,
        verified_at: str,
    ) -> None:
        """Close an incident only when persisted recovery evidence is newer."""
        if str(verified_at) < str(event["last_seen_at"]):
            return
        db.execute(
            """
            UPDATE system_events
            SET resolved_at=?,resolved_by=NULL,detail_json=?
            WHERE id=? AND resolved_at IS NULL AND last_seen_at<=?
            """,
            (
                verified_at,
                self._recovery_detail(event["detail_json"], kind, message, verified_at),
                event["id"],
                verified_at,
            ),
        )

    def _reconcile_recovered_events(self, db) -> None:
        """Resolve historical incidents from real database evidence, never a fixed UI state."""
        now = datetime.now(timezone.utc).replace(microsecond=0)
        quiet_before = (now - timedelta(hours=24)).isoformat()
        rows = db.execute(
            "SELECT * FROM system_events WHERE resolved_at IS NULL ORDER BY last_seen_at"
        ).fetchall()
        for event in rows:
            detail = parse_json(event["detail_json"], {})
            if not isinstance(detail, dict):
                detail = {}
            if (
                event["category"] == "authentication"
                and event["severity"] == "warning"
                and event["event_key"].startswith("auth:login:failed:")
                and event["last_seen_at"] <= quiet_before
            ):
                self._resolve_with_evidence(
                    db,
                    event,
                    kind="quiet_period",
                    message="连续 24 小时没有再次出现相同账号登录失败",
                    verified_at=now.isoformat(),
                )
                continue
            if event["category"] != "server":
                continue
            path = str(detail.get("path", ""))
            if str(detail.get("error_type", "")) in {"BrokenPipeError", "ConnectionResetError"}:
                self._resolve_with_evidence(
                    db,
                    event,
                    kind="client_disconnect",
                    message="确认是浏览器或网络提前断开，不是数据库或业务处理故障",
                    verified_at=now.isoformat(),
                )
                continue
            job_match = re.fullmatch(r"/api/v1/ui/jobs/(ui_job_[A-Za-z0-9]+)", path)
            if job_match:
                job = db.execute(
                    "SELECT status,completed_at FROM ui_generation_jobs WHERE id=?",
                    (job_match.group(1),),
                ).fetchone()
                if job and job["status"] == "completed" and job["completed_at"]:
                    self._resolve_with_evidence(
                        db,
                        event,
                        kind="completed_job",
                        message="对应智能整理任务已完成并保存结果清单",
                        verified_at=job["completed_at"],
                    )
                continue
            food_match = re.fullmatch(
                r"/api/v1/trips/(trip_[A-Za-z0-9]+)/food-recommendations",
                path,
            )
            if food_match:
                success = db.execute(
                    """
                    SELECT MAX(created_at) AS recovered_at FROM audit_events
                    WHERE action='trip.food_refreshed' AND target_id=? AND created_at>?
                    """,
                    (food_match.group(1), event["last_seen_at"]),
                ).fetchone()
                if success and success["recovered_at"]:
                    self._resolve_with_evidence(
                        db,
                        event,
                        kind="successful_write",
                        message="同一旅行在异常后已成功保存餐馆推荐",
                        verified_at=success["recovered_at"],
                    )

    def resolve_login_failure(self, username: str) -> None:
        """A successful login is direct recovery evidence for the same username."""
        normalized = str(username).strip().lower()[:30]
        if not normalized:
            return
        event_key = f"auth:login:failed:{hashlib.sha256(normalized.encode()).hexdigest()[:16]}"
        now = utc_now()
        try:
            with self.store.connection() as db:
                event = db.execute(
                    "SELECT * FROM system_events WHERE event_key=? AND resolved_at IS NULL",
                    (event_key,),
                ).fetchone()
                if event:
                    self._resolve_with_evidence(
                        db,
                        event,
                        kind="successful_login",
                        message="相同账号随后已成功登录",
                        verified_at=now,
                    )
        except Exception:
            return

    def revoke_session(self, session_id: str, current_token: str | None, admin_user_id: str) -> dict:
        current_hash = hashlib.sha256(current_token.encode()).hexdigest() if current_token else ""
        now = utc_now()
        with self.store.connection() as db:
            row = db.execute(
                "SELECT s.id,s.token_hash,s.revoked_at,u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=?",
                (session_id,),
            ).fetchone()
            if not row:
                raise KeyError("设备会话不存在")
            if row["token_hash"] == current_hash:
                raise ValueError("不能在后台撤销当前管理设备，请使用退出登录")
            db.execute("UPDATE sessions SET revoked_at=COALESCE(revoked_at,?) WHERE id=?", (now, session_id))
            self.store._audit(
                db,
                "admin.session_revoked",
                "session",
                session_id,
                {"username": row["username"]},
                actor_id=admin_user_id,
            )
        return {"id": session_id, "revoked": True}

    def _backup_summary(self) -> dict:
        root = self.backup_root()
        items: list[dict[str, Any]] = []
        if root.is_dir():
            for folder in sorted(root.glob("love-moments-*"), reverse=True):
                manifest = folder / "manifest.json"
                if not folder.is_dir() or not manifest.is_file():
                    continue
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                    verification = data.get("verification") if isinstance(data.get("verification"), dict) else {}
                    items.append({
                        "name": folder.name,
                        "created_at": data.get("created_at"),
                        "database_integrity": data.get("database_integrity"),
                        "media_files": int(data.get("media_files", 0)),
                        "media_bytes": int(data.get("media_bytes", 0)),
                        "source": data.get("source", "manual"),
                        "verified_at": verification.get("verified_at"),
                        "verification_ok": bool(verification.get("ok", False)),
                        "restore_drill": verification.get("restore_drill", "not-run"),
                        "database_checksum_ok": bool(verification.get("database_checksum_ok", False)),
                        "export_policy_ok": bool(
                            isinstance(verification.get("account_export_policy"), dict)
                            and verification["account_export_policy"].get("ok", False)
                        ),
                        "service_secrets_included": bool(data.get("service_secrets_included", True)),
                    })
                except (OSError, ValueError, TypeError):
                    continue
        total_count = len(items)
        latest = items[0] if items else None
        age_hours = None
        if latest and latest.get("created_at"):
            try:
                timestamp = datetime.fromisoformat(str(latest["created_at"]).replace("Z", "+00:00"))
                age_hours = round((datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds() / 3600, 1)
            except ValueError:
                pass
        return {"root_name": root.name, "count": total_count, "latest": latest, "latest_age_hours": age_hours, "items": items[:20]}

    def create_backup(self, admin_user_id: str) -> dict:
        from scripts.backup_local import create_backup

        try:
            destination = create_backup(
                output_root=self.backup_root(),
                database_path=Path(self.store.db_path),
                media_root=self.store.media_root,
                source="manual",
            )
        except Exception as exc:
            self.record_event(
                "backup:manual:failed",
                "critical",
                "backup",
                "管理员手动备份失败",
                {"error_type": type(exc).__name__, "admin_user_id": admin_user_id},
            )
            raise
        self.record_event(
            "backup:manual:success",
            "info",
            "backup",
            "管理员手动备份成功",
            {"backup": destination.name, "admin_user_id": admin_user_id},
        )
        return {"created": True, "backup": destination.name, "summary": self._backup_summary()}

    def verify_backup(self, backup_name: str, admin_user_id: str) -> dict:
        name = str(backup_name).strip()
        if not name.startswith("love-moments-") or name != Path(name).name:
            raise ValueError("备份名称无效")
        root = self.backup_root().resolve()
        target = (root / name).resolve()
        if root not in target.parents or not target.is_dir():
            raise KeyError("备份不存在")
        result = verify_backup(target)
        self.record_event(
            f"backup:verify:{name}",
            "info" if result["ok"] else "critical",
            "backup",
            "备份恢复演练通过" if result["ok"] else "备份恢复演练失败",
            {"backup": name, "admin_user_id": admin_user_id, **result},
        )
        return {"backup": name, "verification": result, "summary": self._backup_summary()}

    def _maintenance_state(self) -> dict:
        root = self._state_root()

        def read(name: str) -> dict:
            try:
                value = json.loads((root / name).read_text(encoding="utf-8"))
                return _safe_detail(value) if isinstance(value, dict) else {}
            except (OSError, json.JSONDecodeError):
                return {}

        return {"watchdog": read("watchdog-state.json"), "daily": read("daily-summary.json")}

    def _media_integrity(self, db: Any) -> dict:
        root = self.store.media_root.resolve()
        referenced: set[str] = set()
        rows = db.execute(
            "SELECT COALESCE(storage_key,relative_path) AS storage_key,thumbnail_path FROM media_assets WHERE storage_provider IN ('local','qiniu') AND purged_at IS NULL"
        ).fetchall()
        for row in rows:
            for value in (row["storage_key"], row["thumbnail_path"]):
                if value:
                    referenced.add(str(value).replace("\\", "/"))
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        album_rows = db.execute(
            """SELECT * FROM album_export_jobs
               WHERE status IN ('queued','running','completed') AND expires_at>?""",
            (now,),
        ).fetchall()
        active_album_bytes = 0
        active_album_count = 0
        stored_album_count = 0
        stored_album_bytes = 0
        album_work_files: set[str] = set()
        for row in album_rows:
            if row["status"] == "completed" and row["artifact_relative_path"]:
                referenced.add(str(row["artifact_relative_path"]).replace("\\", "/"))
                stored_album_count += 1
                stored_album_bytes += int(row["size_bytes"] or 0)
                try:
                    self.store._read_verified_album_artifact(db, row)
                except (KeyError, ValueError, OSError, json.JSONDecodeError, ConflictError):
                    pass
                else:
                    active_album_count += 1
                    active_album_bytes += int(row["size_bytes"] or 0)
            elif row["status"] == "running":
                # A claimed job may not have created its work file yet.  Existing
                # work files are expected, but an absent one is not a missing
                # user-media object.
                album_work_files.add(f"temp/album_exports/{row['id']}.tmp")
                try:
                    self.store._revalidate_album_job(db, row)
                except (KeyError, ValueError, OSError, json.JSONDecodeError, ConflictError):
                    pass
                else:
                    active_album_count += 1
            else:
                try:
                    self.store._revalidate_album_job(db, row)
                except (KeyError, ValueError, OSError, json.JSONDecodeError, ConflictError):
                    pass
                else:
                    active_album_count += 1
        ignored_files = {".media-access-key", ".web-push-vapid-private.pem"}
        configured_vapid_path = os.getenv("WEB_PUSH_PRIVATE_KEY_PATH", "").strip()
        if configured_vapid_path:
            candidate = Path(configured_vapid_path)
            candidate = candidate if candidate.is_absolute() else (self.root / candidate)
            try:
                ignored_files.add(candidate.resolve().relative_to(root).as_posix())
            except ValueError:
                pass
        existing: dict[str, int] = {}
        if root.is_dir():
            for path in root.rglob("*"):
                try:
                    relative = path.relative_to(root).as_posix()
                    if path.is_file() and relative not in ignored_files:
                        existing[relative] = path.stat().st_size
                except OSError:
                    continue
        missing = referenced - set(existing)
        orphan = set(existing) - referenced - album_work_files
        return {
            "available": True,
            "missing_files": len(missing),
            "orphan_files": len(orphan),
            "orphan_bytes": sum(existing[path] for path in orphan),
            "album_exports": {
                "active": active_album_count,
                "downloadable_bytes": active_album_bytes,
                "stored_count": stored_album_count,
                "stored_bytes": stored_album_bytes,
                "orphan_bytes": sum(
                    existing[path] for path in orphan if path.startswith("temp/album_exports/")
                ),
            },
        }

    def _capture_metrics(self, db: Any, values: dict[str, Any]) -> dict:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        latest = db.execute("SELECT captured_at FROM system_metrics ORDER BY captured_at DESC LIMIT 1").fetchone()
        should_insert = True
        if latest:
            try:
                captured = datetime.fromisoformat(str(latest["captured_at"]).replace("Z", "+00:00"))
                should_insert = (now - captured.astimezone(timezone.utc)).total_seconds() >= 3600
            except ValueError:
                pass
        if should_insert:
            db.execute(
                "INSERT INTO system_metrics(captured_at,database_bytes,media_bytes,media_files,disk_free_bytes,backup_age_hours,unresolved_events) VALUES (?,?,?,?,?,?,?)",
                (now.isoformat(), values["database_bytes"], values["media_bytes"], values["media_files"], values["disk_free_bytes"], values.get("backup_age_hours"), values["unresolved_events"]),
            )
            db.execute("DELETE FROM system_metrics WHERE captured_at<?", ((now - timedelta(days=90)).isoformat(),))

        def growth(days: int) -> int | None:
            cutoff = (now - timedelta(days=days)).isoformat()
            row = db.execute("SELECT media_bytes FROM system_metrics WHERE captured_at<=? ORDER BY captured_at DESC LIMIT 1", (cutoff,)).fetchone()
            return values["media_bytes"] - int(row["media_bytes"]) if row else None

        return {"media_24h_bytes": growth(1), "media_7d_bytes": growth(7)}

    def overview(self, secure_context: bool, current_token: str | None = None) -> dict:
        store = self.store
        database = Path(store.db_path)
        media_root = store.media_root
        integrity = "unavailable"
        foreign_key_violations = -1
        users: list[dict[str, Any]] = []
        sessions: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        audits: list[dict[str, Any]] = []
        schema_health: dict[str, Any] = {
            "actual_version": 0,
            "expected_version": SCHEMA_VERSION,
            "ok": False,
        }
        with store.connection() as db:
            self._reconcile_recovered_events(db)
            integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_key_violations = len(db.execute("PRAGMA foreign_key_check").fetchall())
            schema_health = schema_inventory(db, expected_version=SCHEMA_VERSION)
            users = [dict(row) for row in db.execute(
                """
                SELECT u.id,u.username,u.nickname,u.created_at,u.deleted_at,
                       COUNT(s.id) AS session_count,
                       SUM(CASE WHEN s.revoked_at IS NULL AND s.expires_at>? THEN 1 ELSE 0 END) AS active_sessions,
                       MAX(s.last_seen_at) AS last_seen_at
                FROM users u LEFT JOIN sessions s ON s.user_id=u.id
                GROUP BY u.id ORDER BY u.created_at
                """,
                (utc_now(),),
            ).fetchall()]
            current_hash = hashlib.sha256(current_token.encode()).hexdigest() if current_token else ""
            session_rows = db.execute(
                """
                SELECT s.id,s.token_hash,u.username,u.nickname,s.device_name,s.ip_address,
                       s.created_at,s.last_seen_at,s.expires_at,s.revoked_at
                FROM sessions s JOIN users u ON u.id=s.user_id
                ORDER BY s.last_seen_at DESC LIMIT 50
                """
            ).fetchall()
            for row in session_rows:
                item = dict(row)
                item["current"] = item.pop("token_hash") == current_hash
                sessions.append(item)
            event_rows = db.execute(
                "SELECT * FROM system_events ORDER BY (resolved_at IS NULL) DESC,last_seen_at DESC LIMIT 100"
            ).fetchall()
            for row in event_rows:
                item = dict(row)
                item["detail"] = parse_json(item.pop("detail_json", "{}"), {})
                events.append(item)
            audits = [dict(row) for row in db.execute(
                """
                SELECT a.id,a.action,a.target_type,a.created_at,
                       COALESCE(u.nickname,'系统') AS actor_name
                FROM audit_events a LEFT JOIN users u ON u.id=a.actor_id
                ORDER BY a.created_at DESC LIMIT 50
                """
            ).fetchall()]

        media_files, media_bytes = _folder_metrics(media_root)
        disk = shutil.disk_usage(database.parent)
        backups = self._backup_summary()
        export_policy = account_export_policy_status()
        maintenance = self._maintenance_state()
        free_ratio = disk.free / disk.total if disk.total else 0
        try:
            user_quota = max(1, int(os.getenv("MEDIA_USER_QUOTA_MB", "5120"))) * 1024 * 1024
            couple_quota = max(2, int(os.getenv("MEDIA_COUPLE_QUOTA_MB", "10240"))) * 1024 * 1024
        except ValueError:
            user_quota, couple_quota = 5 * 1024**3, 10 * 1024**3
        with store.connection() as db:
            media_integrity = self._media_integrity(db)
            push_outbox = store.push_outbox_status()
            capacity_users = [dict(row) for row in db.execute(
                """
                SELECT u.id,u.username,u.nickname,COALESCE(SUM(CASE WHEN m.purged_at IS NULL THEN m.size_bytes ELSE 0 END),0) AS media_bytes
                FROM users u LEFT JOIN media_assets m ON m.owner_id=u.id
                WHERE u.deleted_at IS NULL GROUP BY u.id ORDER BY u.created_at
                """
            ).fetchall()]
            couple_media_bytes = int(db.execute("SELECT COALESCE(SUM(size_bytes),0) FROM media_assets WHERE purged_at IS NULL").fetchone()[0])
            unresolved_events = int(db.execute("SELECT COUNT(*) FROM system_events WHERE resolved_at IS NULL").fetchone()[0])
            growth = self._capture_metrics(db, {
                "database_bytes": database.stat().st_size if database.is_file() else 0,
                "media_bytes": media_bytes,
                "media_files": media_files,
                "disk_free_bytes": disk.free,
                "backup_age_hours": backups["latest_age_hours"],
                "unresolved_events": unresolved_events,
            })
        configured = {
            "large_model": bool(os.getenv("LLM_API_KEY", "").strip()),
            "amap": bool(os.getenv("AMAP_WEB_KEY", "").strip()),
            "geoapify": bool(
                os.getenv("GEOAPIFY_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
                and os.getenv("GEOAPIFY_API_KEY", "").strip()
            ),
            "shengwang": bool(os.getenv("SHENGWANG_APP_ID", "").strip() and os.getenv("SHENGWANG_APP_CERTIFICATE", "").strip()),
            "turn": bool(os.getenv("CLOUDFLARE_TURN_KEY_ID", "").strip() and os.getenv("CLOUDFLARE_TURN_API_TOKEN", "").strip()),
            "qiniu": bool(
                os.getenv("QINIU_ACCESS_KEY", "").strip()
                and os.getenv("QINIU_SECRET_KEY", "").strip()
                and os.getenv("QINIU_BUCKET", "").strip()
                and os.getenv("QINIU_S3_REGION", "").strip()
            ),
            "supabase": bool(os.getenv("SUPABASE_URL", "").strip() and os.getenv("SUPABASE_ANON_KEY", "").strip()),
            "backup_mirror": bool(os.getenv("BACKUP_MIRROR_ROOT", "").strip()),
            "ocr": real_ocr_configured(),
            "booking": bool(
                os.getenv("BOOKING_API_URL", "").strip()
                or os.getenv("SERPAPI_API_KEY", "").strip()
                or os.getenv("JUHE_TRAIN_API_KEY", "").strip()
            ),
            "web_push": bool(
                os.getenv("WEB_PUSH_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
                and importlib.util.find_spec("pywebpush")
            ),
        }
        quota_enabled = {
            "amap": configured["amap"],
            "geoapify": configured["geoapify"],
            "serpapi": bool(os.getenv("SERPAPI_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"} and os.getenv("SERPAPI_API_KEY", "").strip()),
            "juhe_train": bool(os.getenv("JUHE_TRAIN_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"} and os.getenv("JUHE_TRAIN_API_KEY", "").strip()),
            "aliyun_ocr": bool(os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "").strip() and os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "").strip()),
            "ocr_http": bool(os.getenv("OCR_API_URL", "").strip() and os.getenv("OCR_API_KEY", "").strip()),
            "llm": configured["large_model"],
            "recipe_llm": configured["large_model"],
            "tianapi_recipe": bool(os.getenv("TIANAPI_RECIPE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"} and os.getenv("TIANAPI_RECIPE_KEY", "").strip()),
            "themealdb": os.getenv("THEMEALDB_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
            "spoonacular": bool(os.getenv("SPOONACULAR_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"} and os.getenv("SPOONACULAR_API_KEY", "").strip()),
            "booking": bool(os.getenv("BOOKING_API_URL", "").strip() and os.getenv("BOOKING_API_KEY", "").strip()),
        }
        quotas = store.external_api_quotas()
        for quota in quotas:
            quota["configured"] = quota_enabled.get(quota["provider"], False)
        active_quotas = [quota for quota in quotas if quota["configured"]]
        exhausted_quotas = [quota for quota in active_quotas if quota["exhausted"]]
        warning_quotas = [quota for quota in active_quotas if quota["warning"]]
        cookie_mode = os.getenv("COOKIE_SECURE", "false").strip().lower()
        public_https = os.getenv("PUBLIC_BASE_URL", "").strip().startswith("https://")
        secure_cookie_configured = cookie_mode in {"1", "true", "yes", "on"} or (cookie_mode == "auto" and public_https)
        latest_backup = backups.get("latest") or {}
        backup_verified = bool(
            latest_backup.get("verification_ok")
            and latest_backup.get("restore_drill") == "passed"
            and latest_backup.get("database_checksum_ok")
            and latest_backup.get("export_policy_ok")
            and not latest_backup.get("service_secrets_included", True)
        )
        database_ready = integrity == "ok" and foreign_key_violations == 0 and bool(schema_health["ok"])
        media_ready = bool(media_integrity["available"]) and not media_integrity["missing_files"] and not media_integrity["orphan_files"]
        vault_status = (
            "critical" if not database_ready or not export_policy["ok"] or not media_ready
            else "ok" if backup_verified
            else "warning"
        )
        vault_message = (
            "数据库、媒体或导出权限清单异常"
            if vault_status == "critical"
            else "数据库、媒体、导出权限和最近恢复演练均通过"
            if vault_status == "ok"
            else "在线数据正常，但尚无通过校验的最近恢复演练"
        )
        checks = [
            {
                "id": "database", "label": "数据库完整性",
                "status": "ok" if integrity == "ok" and foreign_key_violations == 0 and schema_health["ok"] else "critical",
                "message": "完整性正常" if integrity == "ok" and foreign_key_violations == 0 and schema_health["ok"] else "数据库完整性、外键或结构清单异常",
            },
            {"id": "disk", "label": "磁盘空间", "status": "ok" if disk.free >= 2 * 1024**3 and free_ratio >= 0.1 else "warning", "message": f"可用 {disk.free / 1024**3:.1f} GB（{free_ratio:.0%}）"},
            {"id": "backup", "label": "最近备份", "status": "ok" if backups["latest_age_hours"] is not None and backups["latest_age_hours"] <= 48 and backups["latest"].get("verification_ok") else "warning", "message": "尚无可验证备份" if backups["latest"] is None else f"{backups['latest_age_hours']} 小时前 · {'恢复验证通过' if backups['latest'].get('verification_ok') else '待恢复验证'}"},
            {"id": "media", "label": "媒体完整性", "status": "critical" if media_integrity["missing_files"] else "warning" if media_integrity["orphan_files"] else "ok", "message": "远程存储需由提供方核验" if not media_integrity["available"] else f"缺失 {media_integrity['missing_files']} · 孤立 {media_integrity['orphan_files']}"},
            {
                "id": "data_vault", "label": "数据保险箱", "status": vault_status,
                "message": vault_message,
            },
            {
                "id": "push_outbox", "label": "消息与来电推送队列",
                "status": "warning" if push_outbox["counts"].get("failed", 0) else "ok",
                "message": f"待发送 {push_outbox['pending']} · 失败 {push_outbox['counts'].get('failed', 0)}",
            },
            {"id": "watchdog", "label": "本机守护", "status": "ok" if maintenance["watchdog"].get("healthy") else "warning", "message": maintenance["watchdog"].get("message", "尚未运行健康守护")},
            {"id": "https", "label": "手机安全连接", "status": "ok" if secure_context else "warning", "message": "HTTPS/本机安全上下文" if secure_context else "当前不是安全上下文，异地手机麦克风和 PWA 可能不可用"},
            {
                "id": "cookie", "label": "安全 Cookie",
                "status": "ok" if secure_cookie_configured else "warning",
                "message": (
                    "公网 HTTPS 自动启用，本机 HTTP 保持可登录"
                    if cookie_mode == "auto" and public_https
                    else "已限制为 HTTPS"
                    if secure_cookie_configured
                    else "本机调试模式；公网 HTTPS 部署时必须开启"
                ),
            },
            {
                "id": "registration", "label": "公开注册",
                "status": "ok" if os.getenv("REGISTRATION_MODE", "open").strip().lower() == "closed" else "warning",
                "message": "已关闭，只允许现有账号登录" if os.getenv("REGISTRATION_MODE", "open").strip().lower() == "closed" else "当前允许任何访问者创建账号",
            },
            {
                "id": "public_url", "label": "异地访问地址",
                "status": "warning",
                "message": (
                    f"已配置 {os.getenv('PUBLIC_BASE_URL', '').strip()}；这里仅核对地址格式，仍需用手机流量验证公网可达"
                    if os.getenv("PUBLIC_BASE_URL", "").strip().startswith("https://")
                    else "尚未配置固定 HTTPS 地址；当前只适合本机或可信私人网络"
                ),
            },
            {
                "id": "backup_mirror", "label": "数据库异盘备份",
                "status": "ok" if configured["backup_mirror"] else "warning",
                "message": "已配置第二存储位置" if configured["backup_mirror"] else "尚未配置；电脑硬盘损坏时本机备份可能一起丢失",
            },
            {
                "id": "external_quota", "label": "免费接口额度",
                "status": "critical" if exhausted_quotas else "warning" if warning_quotas else "ok",
                "message": f"{len(exhausted_quotas)} 个接口已停止真实请求" if exhausted_quotas else f"{len(warning_quotas)} 个接口已超过 80%" if warning_quotas else f"{len(active_quotas)} 个已配置接口均在安全线内",
            },
        ]
        if integrity != "ok" or foreign_key_violations or not schema_health["ok"]:
            self.record_event(
                "database:integrity", "critical", "database", "数据库完整性检查异常",
                {
                    "integrity": integrity,
                    "foreign_key_violations": foreign_key_violations,
                    "missing_tables": schema_health.get("missing_tables", []),
                    "missing_columns": schema_health.get("missing_columns", {}),
                    "missing_foreign_keys": schema_health.get("missing_foreign_keys", {}),
                    "malformed_constraints": schema_health.get("malformed_constraints", []),
                    "missing_indexes": schema_health.get("missing_indexes", []),
                    "malformed_indexes": schema_health.get("malformed_indexes", []),
                    "unexpected_indexes": schema_health.get("unexpected_indexes", []),
                    "actual_schema_version": schema_health.get("actual_version"),
                },
            )
        if disk.free < 1024**3 or free_ratio < 0.05:
            self.record_event("disk:low", "critical", "storage", "服务器磁盘空间不足", {"free_bytes": disk.free, "free_ratio": round(free_ratio, 4)})
        elif disk.free < 2 * 1024**3 or free_ratio < 0.1:
            self.record_event("disk:warning", "warning", "storage", "服务器磁盘空间即将不足", {"free_bytes": disk.free, "free_ratio": round(free_ratio, 4)})
        if backups["latest_age_hours"] is None or backups["latest_age_hours"] > 48:
            self.record_event("backup:stale", "warning", "backup", "超过 48 小时没有成功备份", {"age_hours": backups["latest_age_hours"]})
        elif backups["latest"] and not backups["latest"].get("verification_ok"):
            self.record_event("backup:unverified", "warning", "backup", "最新备份尚未通过恢复验证", {"backup": backups["latest"]["name"]})
        if media_integrity["missing_files"]:
            self.record_event("media:missing", "critical", "storage", "媒体数据库引用存在缺失文件", media_integrity)
        else:
            self._resolve_event_key("media:missing")
        if media_integrity["orphan_files"]:
            self.record_event("media:orphan", "warning", "storage", "媒体目录存在未登记文件", media_integrity)
        else:
            self._resolve_event_key("media:orphan")
        if maintenance["watchdog"].get("consecutive_failures", 0) >= 3:
            self.record_event("watchdog:unhealthy", "critical", "service", "本机健康检查连续失败", maintenance["watchdog"])
        if maintenance["daily"] and not maintenance["daily"].get("ok", False):
            self.record_event("maintenance:daily", "critical", "backup", "最近一次每日维护失败", maintenance["daily"])
        with store.connection() as db:
            events = []
            for row in db.execute("SELECT * FROM system_events ORDER BY (resolved_at IS NULL) DESC,last_seen_at DESC LIMIT 100").fetchall():
                item = dict(row)
                item["detail"] = parse_json(item.pop("detail_json", "{}"), {})
                events.append(item)
        return {
            "generated_at": utc_now(),
            "runtime": {
                "python": sys.version.split()[0],
                "uptime_seconds": int((datetime.now(timezone.utc) - self.started_at).total_seconds()),
                "schema_version": schema_health["actual_version"],
                "expected_schema_version": SCHEMA_VERSION,
                "schema_inventory": schema_health,
                "database": "sqlite",
                "media_storage": store.storage.name,
            },
            "checks": checks,
            "storage": {
                "database_bytes": database.stat().st_size if database.is_file() else 0,
                "media_files": media_files,
                "media_bytes": media_bytes,
                "disk_total_bytes": disk.total,
                "disk_free_bytes": disk.free,
                "media_24h_bytes": growth["media_24h_bytes"],
                "media_7d_bytes": growth["media_7d_bytes"],
                "couple_media_bytes": couple_media_bytes,
                "couple_quota_bytes": couple_quota,
                "user_quota_bytes": user_quota,
                "users": capacity_users,
                "integrity": media_integrity,
            },
            "push_outbox": push_outbox,
            "backups": backups,
            "data_vault": {
                "checked_at": utc_now(),
                "database": {
                    "integrity": integrity,
                    "foreign_key_violations": foreign_key_violations,
                    "schema_actual": schema_health["actual_version"],
                    "schema_expected": SCHEMA_VERSION,
                    "schema_ok": bool(schema_health["ok"]),
                },
                "account_export_policy": {
                    "ok": bool(export_policy["ok"]),
                    "version": export_policy["version"],
                    "covered": export_policy["covered"],
                    "required": export_policy["required"],
                    "by_strategy": export_policy["by_strategy"],
                },
                "media": {
                    "available": bool(media_integrity["available"]),
                    "missing_files": media_integrity["missing_files"],
                    "orphan_files": media_integrity["orphan_files"],
                },
                "latest_backup": {
                    "exists": bool(latest_backup),
                    "verified": backup_verified,
                    "restore_drill": latest_backup.get("restore_drill", "not-run"),
                    "checksum_ok": bool(latest_backup.get("database_checksum_ok", False)),
                    "export_policy_ok": bool(latest_backup.get("export_policy_ok", False)),
                    "age_hours": backups.get("latest_age_hours"),
                },
                "mirror": {
                    "configured": bool(configured["backup_mirror"]),
                    "status": "configured" if configured["backup_mirror"] else "not_configured",
                },
                "privacy": "仅返回完整性、数量和验证状态，不返回正文、媒体内容、完整路径或凭据。",
            },
            "maintenance": maintenance,
            "restore_instructions": [
                "先在后台重新验证目标备份，并下载账号数据导出留作二次保险。",
                f"停止恋屿服务与自动守护，确认 {os.getenv('APP_PORT', '18765').strip() or '18765'} 端口不再监听。",
                "把当前 data 目录另行保留，再将备份中的数据库与 media 目录复制回原位置。",
                "重新启动服务，检查数据库、媒体、双方登录和最近消息；确认无误后再保留为正式数据。",
            ],
            "configured": configured,
            "external_api_quotas": quotas,
            "users": users,
            "sessions": sessions,
            "events": events,
            "audits": audits,
            "privacy": "后台仅显示运行元数据，不读取聊天正文、私密瞬间正文或媒体内容。",
        }
