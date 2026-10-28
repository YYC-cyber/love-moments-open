from __future__ import annotations

import json
import io
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from love_moments.intelligent_ui import ConflictError
from love_moments.store import DEMO_COUPLE, DEMO_PARTNER, DEMO_USER, LoveMomentsStore


class AlbumExportResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LoveMomentsStore(
            Path(self.temp.name) / "album.db",
            media_root=Path(self.temp.name) / "media",
            seed_demo_data=True,
        )
        with self.store.connection() as db:
            db.execute("DELETE FROM album_export_jobs")
            db.execute("DELETE FROM memory_descriptions")
            db.execute("DELETE FROM memory_provenance WHERE moment_id IS NOT NULL")
            db.execute("DELETE FROM future_letters")
            db.execute("DELETE FROM moments")

    def _moment(
        self,
        moment_id: str,
        *,
        creator: str = DEMO_USER,
        visibility: str = "shared",
        occurred_at: str = "2026-05-01T08:00:00+00:00",
        reveal_at: str | None = None,
        deleted_at: str | None = None,
        purged_at: str | None = None,
        couple_id: str = DEMO_COUPLE,
        version: int = 1,
    ) -> None:
        with self.store.connection() as db:
            db.execute(
                """INSERT INTO moments(
                       id,couple_id,creator_id,type,title,body,occurred_at,location,
                       visibility,reveal_at,deleted_at,purged_at,version
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    moment_id, couple_id, creator, "photo", f"标题 {moment_id}",
                    f"正文 {moment_id}", occurred_at, "杭州", visibility, reveal_at,
                    deleted_at, purged_at, version,
                ),
            )
            db.execute(
                """INSERT INTO memory_provenance(
                       id,couple_id,owner_id,moment_id,source_type,date_source,
                       evidence_json,version,created_at,updated_at
                   ) VALUES (?,?,?,?,?,'user_confirmed','{}',1,?,?)""",
                (
                    f"provenance-{moment_id}", couple_id, creator, moment_id,
                    "human", occurred_at, occurred_at,
                ),
            )

    @staticmethod
    def _selection(*ids: str) -> tuple[list[dict[str, str]], dict[str, int]]:
        return (
            [{"resource_type": "moment", "resource_id": value} for value in ids],
            {f"moment:{value}": 1 for value in ids},
        )

    def test_preview_reuses_audience_resolver_without_hidden_count_side_channel(self) -> None:
        self._moment("shared")
        self._moment("own-private", visibility="private")
        self._moment("partner-private", creator=DEMO_PARTNER, visibility="private")
        future = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0).isoformat()
        self._moment("own-delayed", visibility="delayed", reveal_at=future)
        self._moment("partner-delayed", creator=DEMO_PARTNER, visibility="delayed", reveal_at=future)
        self._moment("deleted", deleted_at="2026-05-02T00:00:00+00:00")
        self._moment("purged", purged_at="2026-05-02T00:00:00+00:00")

        personal = self.store.preview_album_export(
            {"template": "year", "year": 2026, "audience": "personal"}, DEMO_USER,
        )
        self.assertEqual({item["resource_id"] for item in personal["items"]}, {
            "shared", "own-private", "own-delayed",
        })
        private = {item["resource_id"]: item for item in personal["items"]}
        self.assertTrue(private["shared"]["default_selected"])
        self.assertFalse(private["own-private"]["default_selected"])
        self.assertFalse(private["own-delayed"]["default_selected"])

        shared = self.store.preview_album_export(
            {"template": "year", "year": 2026, "audience": "shared"}, DEMO_USER,
        )
        self.assertEqual(shared["total"], 1)
        self.assertEqual([item["resource_id"] for item in shared["items"]], ["shared"])

        partner_shared = self.store.preview_album_export(
            {"template": "year", "year": 2026, "audience": "shared"}, DEMO_PARTNER,
        )
        self.assertEqual(partner_shared["total"], shared["total"])

    def test_future_letters_converted_moments_and_old_couple_never_enter_candidates(self) -> None:
        self._moment("safe")
        self._moment("converted")
        with self.store.connection() as db:
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            db.execute(
                """INSERT INTO future_letters(
                       id,couple_id,author_id,recipient_id,title,body,status,delivered_at,
                       delivered_moment_id,version,created_at,updated_at
                   ) VALUES ('letter-delivered',?,?,?,?,?,'delivered',?,'converted',1,?,?)""",
                (DEMO_COUPLE, DEMO_USER, DEMO_PARTNER, "绝密", "未来正文", now, now, now),
            )
            db.execute(
                """INSERT INTO couples(id,member_a,member_b,paired_at,anniversary,status)
                   VALUES ('old-couple',?,?,?,?,'unlinked')""",
                (DEMO_USER, DEMO_PARTNER, "2020-01-01", "2020-01-01"),
            )
        self._moment("old", couple_id="old-couple")

        preview = self.store.preview_album_export(
            {"template": "year", "year": 2026, "audience": "personal"}, DEMO_USER,
        )
        self.assertEqual(preview["total"], 1)
        self.assertEqual(preview["items"][0]["resource_id"], "safe")
        serialized = json.dumps(preview, ensure_ascii=False)
        self.assertNotIn("converted", serialized)
        self.assertNotIn("letter-delivered", serialized)
        self.assertNotIn("未来正文", serialized)
        with self.store.connection() as db:
            # The ordinary timeline resolver keeps the explicit conversion
            # usable; only the printable-album boundary rejects it.
            visible_conversion = self.store.resolve_memory_for_audience(
                db, "moment", "converted", DEMO_USER, audience="shared",
            )
        self.assertEqual(visible_conversion["resource_id"], "converted")
        selection, versions = self._selection("converted")
        with self.assertRaises(KeyError):
            self.store.create_album_export_job(
                {
                    "template": "year", "year": 2026, "audience": "personal",
                    "selection": selection, "expected_versions": versions,
                },
                DEMO_USER,
                "album-future-conversion-direct",
            )

    def test_create_requires_explicit_private_selection_and_exact_versions(self) -> None:
        self._moment("shared")
        self._moment("private", visibility="private")
        selection, versions = self._selection("shared", "private")
        payload = {
            "template": "year", "year": 2026, "audience": "personal",
            "selection": selection, "expected_versions": versions,
        }
        job = self.store.create_album_export_job(payload, DEMO_USER, "album-request-0001")
        self.assertEqual(job["status"], "queued")
        self.assertNotIn("selection", job)
        self.assertNotIn("artifact_relative_path", job)

        with self.assertRaises((ValueError, ConflictError, KeyError)):
            self.store.create_album_export_job(
                {**payload, "audience": "shared"}, DEMO_USER, "album-request-0002",
            )
        with self.assertRaises(ValueError):
            self.store.create_album_export_job(
                {**payload, "expected_versions": {"moment:shared": 1}},
                DEMO_USER, "album-request-0003",
            )

    def test_direct_selection_cannot_escape_the_bound_year(self) -> None:
        self._moment("outside-year", occurred_at="2025-12-31T08:00:00+00:00")
        selection, versions = self._selection("outside-year")

        with self.assertRaises(ValueError):
            self.store.create_album_export_job(
                {
                    "template": "year", "year": 2026, "audience": "shared",
                    "selection": selection, "expected_versions": versions,
                },
                DEMO_USER,
                "album-outside-year",
            )

    def test_create_is_target_bound_idempotent_and_partner_guess_is_not_found(self) -> None:
        self._moment("shared")
        selection, versions = self._selection("shared")
        payload = {
            "template": "year", "year": 2026, "audience": "shared",
            "selection": selection, "expected_versions": versions,
        }
        first = self.store.create_album_export_job(payload, DEMO_USER, "album-request-0004")
        second = self.store.create_album_export_job(payload, DEMO_USER, "album-request-0004")
        self.assertEqual(first["id"], second["id"])
        with self.assertRaises(ConflictError):
            self.store.create_album_export_job(
                {**payload, "audience": "personal"}, DEMO_USER, "album-request-0004",
            )
        with self.assertRaises(KeyError):
            self.store.get_album_export_job(first["id"], DEMO_PARTNER)


class AlbumExportJobTests(AlbumExportResolverTests):
    def _queued_job(self) -> dict[str, object]:
        self._moment("shared")
        selection, versions = self._selection("shared")
        return self.store.create_album_export_job(
            {
                "template": "year", "year": 2026, "audience": "shared",
                "selection": selection, "expected_versions": versions,
            },
            DEMO_USER,
            f"album-job-{len(self.store.list_album_export_jobs(DEMO_USER)['items']):04d}",
        )

    def test_claim_is_cas_and_only_one_worker_wins(self) -> None:
        job = self._queued_job()
        first = self.store.claim_album_export_job()
        second = self.store.claim_album_export_job()
        self.assertEqual(first["id"], job["id"])
        self.assertIsNone(second)
        self.assertEqual(self.store.get_album_export_job(job["id"], DEMO_USER)["status"], "running")

    def test_cancel_revokes_authority_before_artifact_cleanup(self) -> None:
        job = self._queued_job()
        cancelled = self.store.cancel_album_export_job(job["id"], job["version"], DEMO_USER)
        self.assertEqual(cancelled["status"], "cancelled")
        with self.assertRaises(KeyError):
            self.store.get_album_export_download(job["id"], DEMO_USER)

    def test_cancel_version_rejects_bool_float_string_and_malformed_values(self) -> None:
        job = self._queued_job()
        for value in (True, 1.0, "1", "not-a-version", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.cancel_album_export_job(job["id"], value, DEMO_USER)
        self.assertEqual(self.store.get_album_export_job(job["id"], DEMO_USER)["status"], "queued")

    def test_worker_revalidation_fails_after_visibility_or_version_change(self) -> None:
        job = self._queued_job()
        claimed = self.store.claim_album_export_job()
        self.assertEqual(claimed["id"], job["id"])
        with self.store.connection() as db:
            db.execute("UPDATE moments SET visibility='private',version=version+1 WHERE id='shared'")
        result = self.store.run_album_export_job(claimed)
        self.assertEqual(result["status"], "failed")
        self.assertIn(result["failure_code"], {"permission_changed", "selection_changed"})
        with self.assertRaises(KeyError):
            self.store.get_album_export_download(job["id"], DEMO_USER)

    def test_renderer_validation_error_is_not_misreported_as_permission_change(self) -> None:
        self._queued_job()
        claimed = self.store.claim_album_export_job()
        with patch("love_moments.album_exports.render_album_pdf", side_effect=ValueError("bad document")):
            result = self.store.run_album_export_job(claimed)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_code"], "render_failed")

    def test_trip_job_is_bound_to_the_exact_trip_version(self) -> None:
        with self.store.connection() as db:
            trip = db.execute(
                "SELECT id,start_date,version FROM trips WHERE couple_id=? AND deleted_at IS NULL LIMIT 1",
                (DEMO_COUPLE,),
            ).fetchone()
        self._moment("trip-shared", occurred_at=f"{trip['start_date']}T08:00:00+00:00")
        selection, versions = self._selection("trip-shared")
        job = self.store.create_album_export_job(
            {
                "template": "trip", "trip_id": trip["id"], "audience": "shared",
                "selection": selection, "expected_versions": versions,
            },
            DEMO_USER,
            "album-trip-scope-version",
        )
        claimed = self.store.claim_album_export_job()
        self.assertEqual(claimed["id"], job["id"])
        with self.store.connection() as db:
            db.execute("UPDATE trips SET version=version+1 WHERE id=?", (trip["id"],))

        result = self.store.run_album_export_job(claimed)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_code"], "selection_changed")

    def test_successful_worker_download_and_exact_expiry(self) -> None:
        job = self._queued_job()
        claimed = self.store.claim_album_export_job()
        completed = self.store.run_album_export_job(claimed)
        self.assertEqual(completed["status"], "completed")
        data, filename = self.store.get_album_export_download(job["id"], DEMO_USER)
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertEqual(filename, "love-moments-album.pdf")
        with self.store.connection() as db:
            audits = db.execute(
                "SELECT action,detail FROM audit_events WHERE target_type='album_export' AND target_id=? ORDER BY created_at,id",
                (job["id"],),
            ).fetchall()
        self.assertTrue({"album_export.created", "album_export.completed", "album_export.downloaded"}.issubset({row["action"] for row in audits}))
        audit_text = json.dumps([dict(row) for row in audits], ensure_ascii=False)
        self.assertNotIn("正文 shared", audit_text)
        self.assertNotIn("moment:shared", audit_text)
        with self.store.connection() as db:
            db.execute(
                "UPDATE album_export_jobs SET expires_at=? WHERE id=?",
                (datetime.now(timezone.utc).replace(microsecond=0).isoformat(), job["id"]),
            )
        with self.assertRaises(KeyError):
            self.store.get_album_export_download(job["id"], DEMO_USER)
        expired = self.store.get_album_export_job(job["id"], DEMO_USER)
        self.assertEqual(expired["status"], "expired")
        self.assertFalse((self.store.album_export_root / f"{job['id']}.pdf").exists())

    def test_download_rechecks_permission_and_checksum(self) -> None:
        job = self._queued_job()
        completed = self.store.run_album_export_job(self.store.claim_album_export_job())
        self.assertEqual(completed["status"], "completed")
        with self.store.connection() as db:
            db.execute("UPDATE moments SET visibility='private',version=version+1 WHERE id='shared'")
        with self.assertRaises(KeyError):
            self.store.get_album_export_download(job["id"], DEMO_USER)
        self.assertFalse((self.store.album_export_root / f"{job['id']}.pdf").exists())

    def test_job_listing_revalidates_completed_download_authority(self) -> None:
        job = self._queued_job()
        completed = self.store.run_album_export_job(self.store.claim_album_export_job())
        self.assertTrue(completed["download_ready"])
        listed = self.store.list_album_export_jobs(DEMO_USER)["items"]
        self.assertEqual(listed[0]["id"], job["id"])
        self.assertTrue(listed[0]["download_ready"])
        self.assertEqual(self.store.list_album_export_jobs(DEMO_PARTNER)["items"], [])

        with self.store.connection() as db:
            db.execute("UPDATE moments SET visibility='private',version=version+1 WHERE id='shared'")

        refreshed = self.store.list_album_export_jobs(DEMO_USER)["items"]
        current = next(item for item in refreshed if item["id"] == job["id"])
        self.assertEqual(current["status"], "expired")
        self.assertFalse(current["download_ready"])
        self.assertFalse((self.store.album_export_root / f"{job['id']}.pdf").exists())

    def test_restart_recovery_revokes_completed_job_with_missing_artifact(self) -> None:
        job = self._queued_job()
        completed = self.store.run_album_export_job(self.store.claim_album_export_job())
        self.assertEqual(completed["status"], "completed")
        (self.store.album_export_root / f"{job['id']}.pdf").unlink()

        recovered = self.store.recover_album_export_jobs()

        self.assertEqual(recovered["invalid"], 1)
        current = self.store.get_album_export_job(job["id"], DEMO_USER)
        self.assertEqual(current["status"], "expired")
        self.assertEqual(current["failure_code"], "permission_changed")
        with self.assertRaises(KeyError):
            self.store.get_album_export_download(job["id"], DEMO_USER)

    def test_account_export_contains_safe_job_metadata_only(self) -> None:
        job = self._queued_job()
        archive, _ = self.store.account_export(DEMO_USER)
        with zipfile.ZipFile(io.BytesIO(archive)) as value:
            exported = json.loads(value.read("data.json"))
        item = next(row for row in exported["album_export_jobs"] if row["id"] == job["id"])
        self.assertFalse(item["temporary_artifact_included"])
        serialized = json.dumps(item, ensure_ascii=False)
        for secret in ("selection_json", "expected_versions_json", "artifact_relative_path", "artifact_sha256", "正文 shared"):
            self.assertNotIn(secret, serialized)


if __name__ == "__main__":
    unittest.main()
