from __future__ import annotations

import json
import hashlib
import os
import plistlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from love_moments import LoveMomentsStore
from love_moments.maintenance import prune_automatic_backups, verify_backup
from scripts.backup_local import create_backup
from scripts import install_launchd
from scripts import maintenance as scheduled


class _HealthyResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @staticmethod
    def read() -> bytes:
        return b'{"status":"ok"}'


class MaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "data" / "love.db"
        self.media = self.root / "data" / "media"
        self.store = LoveMomentsStore(self.database, media_root=self.media, seed_demo_data=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_backup_verification_and_daily_mirror(self) -> None:
        backups = self.root / "backups"
        mirror = self.root / "mirror"
        state = self.root / "state"
        logs = self.root / "logs"
        with patch.dict(os.environ, {
            "DATABASE_PATH": str(self.database),
            "MEDIA_ROOT": str(self.media),
            "BACKUP_ROOT": str(backups),
            "BACKUP_MIRROR_ROOT": str(mirror),
            "MAINTENANCE_STATE_ROOT": str(state),
            "LOG_ROOT": str(logs),
            "LOCAL_ADMIN_NOTIFICATIONS": "false",
        }, clear=False):
            result = scheduled.daily_maintenance()
        self.assertTrue(result["ok"])
        destination = backups / result["backup"]
        self.assertTrue(verify_backup(destination)["ok"])
        self.assertTrue((mirror / result["backup"] / "manifest.json").is_file())
        self.assertTrue((state / "daily-summary.json").is_file())

    def test_backup_excludes_ephemeral_album_exports(self) -> None:
        export_root = self.media / "temp" / "album_exports"
        export_root.mkdir(parents=True, exist_ok=True)
        (export_root / "job.pdf").write_bytes(b"%PDF-private-temporary")
        (export_root / "job.tmp").write_bytes(b"working")

        destination = create_backup(self.root / "backups-album", self.database, self.media)
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))

        self.assertFalse(manifest["ephemeral_album_exports_included"])
        self.assertFalse((destination / "media" / "temp" / "album_exports").exists())
        self.assertFalse(any("album_exports" in item["path"] for item in manifest["media_objects"]))
        self.assertTrue(verify_backup(destination)["ok"])

    def test_backup_excludes_service_secrets_and_records_policy_coverage(self) -> None:
        (self.media / ".media-access-key").write_text("do-not-copy", encoding="utf-8")
        (self.media / ".web-push-vapid-private.pem").write_text("do-not-copy", encoding="utf-8")
        destination = create_backup(self.root / "backups-secret", self.database, self.media)
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["service_secrets_included"])
        self.assertFalse((destination / "media" / ".media-access-key").exists())
        self.assertFalse((destination / "media" / ".web-push-vapid-private.pem").exists())
        verified = verify_backup(destination, update_manifest=False)
        self.assertTrue(verified["account_export_policy"]["ok"])
        self.assertEqual(
            verified["account_export_policy"]["covered"],
            verified["account_export_policy"]["required"],
        )

    def test_backup_verification_rejects_path_traversal_or_unlisted_media(self) -> None:
        image = self.media / "images" / "visible.jpg"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"visible")
        destination = create_backup(self.root / "backups-tamper", self.database, self.media)
        manifest_path = destination / "manifest.json"
        original = json.loads(manifest_path.read_text(encoding="utf-8"))

        traversal = dict(original)
        traversal["database_file"] = "../love_moments.db"
        manifest_path.write_text(json.dumps(traversal), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "路径无效"):
            verify_backup(destination, update_manifest=False)

        manifest_path.write_text(json.dumps(original), encoding="utf-8")
        (destination / "media" / "images" / "unlisted.jpg").write_bytes(b"orphan")
        result = verify_backup(destination, update_manifest=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["orphan_media"], 1)
        self.assertFalse(result["media_count_matches"])
        self.assertFalse(result["media_bytes_matches"])

    def test_retention_keeps_manual_seven_days_and_four_weeks(self) -> None:
        root = self.root / "backups"
        root.mkdir()
        now = datetime(2026, 10, 13, 3, 30, tzinfo=timezone.utc)
        manual = root / "love-moments-manual-keep"
        manual.mkdir()
        (manual / "manifest.json").write_text(json.dumps({"source": "manual", "created_at": now.isoformat()}), encoding="utf-8")
        for days in range(50):
            folder = root / f"love-moments-automatic-{days:02d}"
            folder.mkdir()
            created = now - timedelta(days=days)
            (folder / "manifest.json").write_text(json.dumps({"source": "automatic", "created_at": created.isoformat()}), encoding="utf-8")
        result = prune_automatic_backups(root, now=now)
        self.assertTrue(manual.is_dir())
        self.assertLessEqual(len(result["kept"]), 11)
        self.assertGreater(len(result["removed"]), 0)

    def test_watchdog_failure_threshold_and_recovery(self) -> None:
        state = self.root / "state"
        logs = self.root / "logs"
        env = {
            "APP_PORT": "1",
            "MAINTENANCE_STATE_ROOT": str(state),
            "LOG_ROOT": str(logs),
            "LOCAL_ADMIN_NOTIFICATIONS": "false",
            "WATCHDOG_AUTO_RESTART": "false",
        }
        with patch.dict(os.environ, env, clear=False):
            for expected in (1, 2, 3):
                self.assertEqual(scheduled.health_check()["consecutive_failures"], expected)
            with patch.object(scheduled.request, "urlopen", return_value=_HealthyResponse()):
                recovered = scheduled.health_check()
        self.assertTrue(recovered["healthy"])
        self.assertEqual(recovered["consecutive_failures"], 0)

    def test_launchd_files_are_valid_and_reference_project(self) -> None:
        output = self.root / "launchd"
        paths = install_launchd.render(output, Path(os.sys.executable))
        self.assertEqual(len(paths), 3)
        values = []
        for path in paths:
            with path.open("rb") as source:
                values.append(plistlib.load(source))
        server = next(item for item in values if item["Label"].endswith("server"))
        self.assertTrue(server["RunAtLoad"])
        self.assertTrue(server["KeepAlive"])
        self.assertEqual(server["WorkingDirectory"], str(install_launchd.ROOT))

    def test_launchd_preserves_virtual_environment_launcher(self) -> None:
        base_python = self.root / "base" / "python3"
        base_python.parent.mkdir()
        base_python.touch()
        launcher = self.root / ".venv" / "bin" / "python"
        launcher.parent.mkdir(parents=True)
        launcher.symlink_to(base_python)

        selected = install_launchd._launcher_path(launcher)
        self.assertEqual(selected, launcher.absolute())
        self.assertNotEqual(selected, launcher.resolve())
        definition = install_launchd.definitions(selected)["server"]
        self.assertEqual(definition["ProgramArguments"][2], str(launcher.absolute()))

    def test_backup_rejects_recursive_roots_and_leaves_no_partial_folder(self) -> None:
        with self.assertRaisesRegex(ValueError, "媒体目录内部"):
            create_backup(self.media / "backups", self.database, self.media)
        source = create_backup(self.root / "backups", self.database, self.media)
        with self.assertRaisesRegex(ValueError, "源备份目录内部"):
            from love_moments.maintenance import mirror_backup

            mirror_backup(source, source / "mirror")
        self.assertFalse(any(path.name.endswith(".copying") for path in self.root.rglob("*")))

    def test_backup_requires_local_copy_for_qiniu_mirrored_media(self) -> None:
        media_file = self.media / "images" / "mirrored.jpg"
        media_file.parent.mkdir(parents=True, exist_ok=True)
        media_file.write_bytes(b"mirrored")
        with self.store.connection() as db:
            couple = db.execute("SELECT id FROM couples LIMIT 1").fetchone()[0]
            owner = db.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
            db.execute(
                "INSERT INTO media_assets(id,couple_id,owner_id,kind,original_name,mime_type,relative_path,size_bytes,visibility,status,storage_provider,storage_key,checksum,created_at) VALUES (?,?,?,?,?,?,?,?,?,'ready','qiniu',?,?,?)",
                ("media-mirror", couple, owner, "image", "mirrored.jpg", "image/jpeg", "images/mirrored.jpg", 8, "shared", "images/mirrored.jpg", "", datetime.now(timezone.utc).isoformat()),
            )
        destination = create_backup(self.root / "backups-mirror", self.database, self.media)
        self.assertTrue(verify_backup(destination)["ok"])
        (destination / "media" / "images" / "mirrored.jpg").unlink()
        self.assertFalse(verify_backup(destination)["ok"])

    def test_backup_verification_rejects_structurally_incomplete_v34_database(self) -> None:
        destination = create_backup(self.root / "backups-contract", self.database, self.media)
        copied_database = destination / "love_moments.db"
        db = sqlite3.connect(copied_database)
        try:
            db.execute("DROP INDEX idx_decisions_queue")
            db.commit()
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            db.close()
        manifest_path = destination / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["database_sha256"] = hashlib.sha256(copied_database.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        result = verify_backup(destination, update_manifest=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["restore_drill"], "failed")
        self.assertIn("idx_decisions_queue", result["schema_inventory"]["missing_indexes"])


if __name__ == "__main__":
    unittest.main()
