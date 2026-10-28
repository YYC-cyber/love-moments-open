from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from love_moments import LoveMomentsStore
from love_moments.admin import AdminService
from love_moments.schema import ACCOUNT_EXPORT_POLICIES, REQUIRED_TABLES_V34, SCHEMA_VERSION
from love_moments.store import DEMO_COUPLE, DEMO_PARTNER, DEMO_USER


class AdminServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = LoveMomentsStore(self.root / "data" / "love.db", media_root=self.root / "data" / "media", seed_demo_data=True)
        self.admin = AdminService(lambda: self.store, self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_events_are_redacted_deduplicated_resolved_and_reopened(self) -> None:
        self.admin.record_event("login:test", "warning", "authentication", "登录失败", {"username": "alice", "api_key": "must-not-leak"})
        self.admin.record_event("login:test", "warning", "authentication", "登录失败", {"username": "alice", "token": "must-not-leak"})
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM system_events WHERE event_key='login:test'").fetchone()
        self.assertEqual(row["occurrences"], 2)
        detail = json.loads(row["detail_json"])
        self.assertEqual(detail["token"], "[已隐藏]")
        self.assertNotIn("must-not-leak", row["detail_json"])
        self.assertTrue(self.admin.resolve_event(row["id"], DEMO_USER)["resolved"])
        self.admin.record_event("login:test", "warning", "authentication", "登录失败")
        with self.store.connection() as db:
            reopened = db.execute("SELECT * FROM system_events WHERE id=?", (row["id"],)).fetchone()
        self.assertEqual(reopened["occurrences"], 3)
        self.assertIsNone(reopened["resolved_at"])

    def test_overview_hides_secrets_and_marks_current_session(self) -> None:
        token = self.store.create_session(DEMO_USER, "测试电脑", "test agent", "127.0.0.1")
        with patch.dict(os.environ, {
            "LLM_API_KEY": "secret-value",
            "COOKIE_SECURE": "true",
            "PUBLIC_BASE_URL": "https://love.example.test",
        }, clear=False):
            overview = self.admin.overview(True, token)
        encoded = json.dumps(overview, ensure_ascii=False)
        self.assertNotIn("secret-value", encoded)
        self.assertTrue(overview["configured"]["large_model"])
        self.assertEqual(overview["runtime"]["schema_version"], SCHEMA_VERSION)
        self.assertEqual(len(overview["external_api_quotas"]), 12)
        llm_quota = next(item for item in overview["external_api_quotas"] if item["provider"] == "llm")
        self.assertTrue(llm_quota["configured"])
        self.assertTrue(llm_quota["unlimited"] or llm_quota["remaining"] > 0)
        self.assertTrue(any(item["current"] for item in overview["sessions"]))
        self.assertEqual(next(item for item in overview["checks"] if item["id"] == "database")["status"], "ok")
        public_url = next(item for item in overview["checks"] if item["id"] == "public_url")
        self.assertEqual(public_url["status"], "warning")
        self.assertIn("手机流量验证", public_url["message"])
        vault = overview["data_vault"]
        self.assertEqual(vault["account_export_policy"]["covered"], len(REQUIRED_TABLES_V34))
        self.assertEqual(vault["account_export_policy"]["required"], len(REQUIRED_TABLES_V34))
        self.assertEqual(set(ACCOUNT_EXPORT_POLICIES), REQUIRED_TABLES_V34)
        self.assertNotIn(str(self.store.db_path), encoded)

    def test_backup_is_private_and_database_is_valid(self) -> None:
        result = self.admin.create_backup(DEMO_USER)
        destination = self.root / "backups" / result["backup"]
        self.assertTrue((destination / "manifest.json").is_file())
        with closing(sqlite3.connect(destination / "love_moments.db")) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        if os.name != "nt":
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
            self.assertEqual((destination / "love_moments.db").stat().st_mode & 0o777, 0o600)
        verified = self.admin.verify_backup(result["backup"], DEMO_USER)
        self.assertTrue(verified["verification"]["ok"])
        self.assertTrue(verified["verification"]["schema_inventory"]["ok"])
        overview = self.admin.overview(True)
        vault = overview["data_vault"]
        self.assertTrue(vault["latest_backup"]["verified"])
        self.assertTrue(vault["latest_backup"]["checksum_ok"])
        self.assertEqual(next(item for item in overview["checks"] if item["id"] == "data_vault")["status"], "ok")

    def test_admin_health_uses_observed_schema_and_detects_missing_required_table(self) -> None:
        overview = self.admin.overview(True)
        self.assertEqual(overview["runtime"]["schema_version"], SCHEMA_VERSION)
        self.assertEqual(overview["runtime"]["expected_schema_version"], SCHEMA_VERSION)
        self.assertTrue(overview["runtime"]["schema_inventory"]["ok"])
        with self.store.connection() as db:
            db.execute("DROP TABLE ai_usage_events")
        broken = self.admin.overview(True)
        self.assertIn("ai_usage_events", broken["runtime"]["schema_inventory"]["missing_tables"])
        database = next(item for item in broken["checks"] if item["id"] == "database")
        self.assertEqual(database["status"], "critical")

    def test_storage_growth_and_orphan_file_are_reported(self) -> None:
        first = self.admin.overview(True)
        old = (datetime.now(timezone.utc) - timedelta(days=8)).replace(microsecond=0).isoformat()
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO system_metrics(captured_at,database_bytes,media_bytes,media_files,disk_free_bytes,unresolved_events) VALUES (?,?,?,?,?,0)",
                (old, 1, 0, 0, 1),
            )
        orphan = self.store.media_root / "images" / "orphan.jpg"
        orphan.write_bytes(b"not a real photo")
        overview = self.admin.overview(True)
        self.assertGreaterEqual(overview["storage"]["media_7d_bytes"], orphan.stat().st_size)
        self.assertEqual(overview["storage"]["integrity"]["orphan_files"], 1)
        self.assertIn("restore_instructions", overview)
        self.assertTrue(any(os.getenv("APP_PORT", "18765") in item for item in overview["restore_instructions"]))
        self.assertIn("maintenance", overview)
        self.assertIn("media_24h_bytes", first["storage"])

    def test_media_integrity_ignores_service_keys_and_resolves_false_alert(self) -> None:
        (self.store.media_root / ".web-push-vapid-private.pem").write_text("private", encoding="utf-8")
        self.admin.record_event(
            "media:orphan", "warning", "storage", "媒体目录存在未登记文件",
            {"orphan_files": 1},
        )
        overview = self.admin.overview(True)
        self.assertEqual(overview["storage"]["integrity"]["orphan_files"], 0)
        with self.store.connection() as db:
            event = db.execute("SELECT resolved_at,resolved_by FROM system_events WHERE event_key='media:orphan'").fetchone()
        self.assertIsNotNone(event["resolved_at"])
        self.assertIsNone(event["resolved_by"])

    def test_running_album_work_file_is_not_missing_or_orphan_media(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        job_id = "album-running-integrity-00000000001"
        work_file = self.store.album_export_root / f"{job_id}.tmp"
        with self.store.connection() as db:
            db.execute(
                """INSERT INTO album_export_jobs(
                       id,couple_id,creator_id,template,year,audience,selection_json,
                       expected_versions_json,status,version,expires_at,created_at,updated_at
                   ) VALUES (?,?,?,'year',2026,'personal','[]','{}','running',1,?,?,?)""",
                (
                    job_id, DEMO_COUPLE, DEMO_USER,
                    (now + timedelta(hours=1)).isoformat(), now.isoformat(), now.isoformat(),
                ),
            )

        absent = self.admin.overview(True)["storage"]["integrity"]

        self.assertEqual(absent["missing_files"], 0)
        self.assertEqual(absent["orphan_files"], 0)

        work_file.write_bytes(b"working")
        integrity = self.admin.overview(True)["storage"]["integrity"]

        self.assertEqual(integrity["missing_files"], 0)
        self.assertEqual(integrity["orphan_files"], 0)
        self.assertEqual(integrity["album_exports"]["active"], 1)

    def test_album_storage_metrics_revalidate_current_download_authority(self) -> None:
        occurred_at = "2026-05-01T08:00:00+00:00"
        with self.store.connection() as db:
            db.execute("DELETE FROM moments")
            db.execute(
                """INSERT INTO moments(
                       id,couple_id,creator_id,type,title,body,occurred_at,visibility,version
                   ) VALUES ('album-admin-visible',?,?, 'photo','可打印','正文',?,'shared',1)""",
                (DEMO_COUPLE, DEMO_USER, occurred_at),
            )
            db.execute(
                """INSERT INTO memory_provenance(
                       id,couple_id,owner_id,moment_id,source_type,date_source,
                       evidence_json,version,created_at,updated_at
                   ) VALUES ('album-admin-provenance',?,?,?,'human','user_confirmed','{}',1,?,?)""",
                (DEMO_COUPLE, DEMO_USER, "album-admin-visible", occurred_at, occurred_at),
            )
        job = self.store.create_album_export_job(
            {
                "template": "year", "year": 2026, "audience": "shared",
                "selection": [{"resource_type": "moment", "resource_id": "album-admin-visible"}],
                "expected_versions": {"moment:album-admin-visible": 1},
            },
            DEMO_USER,
            "album-admin-metrics-job",
        )
        completed = self.store.run_album_export_job(self.store.claim_album_export_job())
        self.assertEqual(completed["status"], "completed")
        before = self.admin.overview(True)["storage"]["integrity"]["album_exports"]
        self.assertEqual(before["active"], 1)
        self.assertGreater(before["downloadable_bytes"], 0)

        with self.store.connection() as db:
            db.execute(
                "UPDATE moments SET visibility='private',version=version+1 WHERE id='album-admin-visible'"
            )

        after_permission_change = self.admin.overview(True)["storage"]["integrity"]["album_exports"]
        self.assertEqual(after_permission_change["active"], 0)
        self.assertEqual(after_permission_change["downloadable_bytes"], 0)
        self.assertEqual(after_permission_change["stored_count"], 1)
        with self.assertRaises(KeyError):
            self.store.get_album_export_download(job["id"], DEMO_USER)

    def test_historical_events_close_only_with_real_recovery_evidence(self) -> None:
        job_id = "ui_job_recoveryevidence000000000001"
        trip_id = "trip_recoveryevidence00000000000001"
        now = datetime.now(timezone.utc).replace(microsecond=0)
        failure_at = (now - timedelta(minutes=10)).isoformat()
        recovered_at = (now - timedelta(minutes=5)).isoformat()
        old_login_at = (now - timedelta(hours=25)).isoformat()
        with self.store.connection() as db:
            couple = db.execute("SELECT id FROM couples LIMIT 1").fetchone()["id"]
            db.execute(
                """
                INSERT INTO ui_generation_jobs(
                    id,couple_id,user_id,surface,intent_hash,status,error_code,retry_count,created_at,completed_at
                ) VALUES (?,?,?,?,?,'completed','MODEL_FALLBACK',0,?,?)
                """,
                (job_id, couple, DEMO_USER, "home", "test", failure_at, recovered_at),
            )
            db.execute(
                "INSERT INTO trips(id,couple_id,title,destination,start_date,end_date,budget_cents,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (trip_id, couple, "恢复测试", "杭州", "2027-01-01", "2027-01-02", 10000, "planning", failure_at, recovered_at),
            )
            db.execute(
                "INSERT INTO audit_events(id,actor_id,couple_id,action,target_type,target_id,created_at) VALUES (?,?,?,?,?,?,?)",
                ("audit_recovery", DEMO_USER, couple, "trip.food_refreshed", "trip", trip_id, recovered_at),
            )
        self.admin.record_event(
            f"server:OperationalError:/api/v1/ui/jobs/{job_id}",
            "critical", "server", "接口出现未处理异常",
            {"path": f"/api/v1/ui/jobs/{job_id}", "error_type": "OperationalError"},
        )
        self.admin.record_event(
            f"server:RuntimeError:/api/v1/trips/{trip_id}/food-recommendations",
            "critical", "server", "接口出现未处理异常",
            {"path": f"/api/v1/trips/{trip_id}/food-recommendations", "error_type": "RuntimeError"},
        )
        self.admin.record_event(
            "auth:login:failed:quiet", "warning", "authentication", "账号登录失败",
            {"username": "missing"},
        )
        with self.store.connection() as db:
            db.execute(
                "UPDATE system_events SET first_seen_at=?,last_seen_at=? WHERE event_key='auth:login:failed:quiet'",
                (old_login_at, old_login_at),
            )
            db.execute(
                "UPDATE system_events SET first_seen_at=?,last_seen_at=? WHERE category='server'",
                (failure_at, failure_at),
            )
        overview = self.admin.overview(True)
        recovered = {
            event["event_key"]: event for event in overview["events"]
            if event["event_key"].startswith(("server:", "auth:login:failed:quiet"))
        }
        self.assertTrue(all(event["resolved_at"] for event in recovered.values()))
        self.assertEqual(recovered[f"server:OperationalError:/api/v1/ui/jobs/{job_id}"]["detail"]["recovery"]["kind"], "completed_job")
        self.assertEqual(recovered[f"server:RuntimeError:/api/v1/trips/{trip_id}/food-recommendations"]["detail"]["recovery"]["kind"], "successful_write")
        self.assertEqual(recovered["auth:login:failed:quiet"]["detail"]["recovery"]["kind"], "quiet_period")

    def test_successful_login_resolves_only_the_same_username_failure(self) -> None:
        username = "test"
        key = f"auth:login:failed:{__import__('hashlib').sha256(username.encode()).hexdigest()[:16]}"
        self.admin.record_event(key, "warning", "authentication", "账号登录失败", {"username": username})
        self.admin.record_event("auth:login:failed:other", "warning", "authentication", "账号登录失败", {"username": "other"})
        self.admin.resolve_login_failure(username)
        with self.store.connection() as db:
            resolved = db.execute("SELECT resolved_at FROM system_events WHERE event_key=?", (key,)).fetchone()
            other = db.execute("SELECT resolved_at FROM system_events WHERE event_key='auth:login:failed:other'").fetchone()
        self.assertIsNotNone(resolved["resolved_at"])
        self.assertIsNone(other["resolved_at"])

    def test_admin_can_revoke_another_session_but_not_current(self) -> None:
        current = self.store.create_session(DEMO_USER, "当前设备")
        other = self.store.create_session(DEMO_PARTNER, "另一设备")
        with self.store.connection() as db:
            other_id = db.execute("SELECT id FROM sessions WHERE token_hash=?", (__import__("hashlib").sha256(other.encode()).hexdigest(),)).fetchone()["id"]
            current_id = db.execute("SELECT id FROM sessions WHERE token_hash=?", (__import__("hashlib").sha256(current.encode()).hexdigest(),)).fetchone()["id"]
        self.assertTrue(self.admin.revoke_session(other_id, current, DEMO_USER)["revoked"])
        self.assertIsNone(self.store.user_for_session(other))
        with self.assertRaisesRegex(ValueError, "当前管理设备"):
            self.admin.revoke_session(current_id, current, DEMO_USER)


if __name__ == "__main__":
    unittest.main()
