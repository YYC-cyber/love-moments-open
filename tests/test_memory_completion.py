from __future__ import annotations

import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from love_moments.intelligent_ui import ConflictError
from love_moments.media_security import sanitize_image
from love_moments.store import DEMO_COUPLE, DEMO_PARTNER, DEMO_USER, LoveMomentsStore


class MemoryCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LoveMomentsStore(Path(self.temp.name) / "completion.db", seed_demo_data=True)
        with self.store.connection() as db:
            db.execute("DELETE FROM memory_completion_suggestions")
            db.execute("DELETE FROM memory_descriptions")
            db.execute("DELETE FROM memory_provenance WHERE moment_id IS NOT NULL")
            db.execute("DELETE FROM moments")

    def _moment(self, moment_id: str, *, creator: str = DEMO_USER, kind: str = "photo", body: str = "", location: str | None = None, visibility: str = "shared", date_source: str = "legacy_unknown") -> None:
        now = "2024-02-14T08:00:00+00:00"
        with self.store.connection() as db:
            db.execute(
                """INSERT INTO moments(id,couple_id,creator_id,type,title,body,occurred_at,location,visibility,version)
                   VALUES (?,?,?,?,?,?,?,?,?,1)""",
                (moment_id, DEMO_COUPLE, creator, kind, moment_id, body, now, location, visibility),
            )
            db.execute(
                """INSERT INTO memory_provenance(
                    id,couple_id,owner_id,moment_id,source_type,date_source,evidence_json,version,created_at,updated_at
                ) VALUES (?,?,?,?,?,?, '{}',1,?,?)""",
                (f"prov-{moment_id}", DEMO_COUPLE, creator, moment_id, "human", date_source, now, now),
            )

    def test_refresh_creates_only_explicit_gaps_and_unknown_date_is_not_provisional(self) -> None:
        self._moment("needs", date_source="system_provisional")
        self._moment("legacy", body="已有文字", location="杭州", date_source="legacy_unknown")
        self.store.refresh_memory_completion_suggestions(DEMO_USER)
        items = self.store.list_memory_completion_suggestions(DEMO_USER)["items"]
        kinds = {(item["resource_id"], item["suggestion_kind"]) for item in items}
        self.assertIn(("needs", "missing_text"), kinds)
        self.assertIn(("needs", "missing_location"), kinds)
        self.assertIn(("needs", "provisional_date"), kinds)
        self.assertNotIn(("legacy", "provisional_date"), kinds)

    def test_private_description_never_triggers_partner_but_shared_description_does(self) -> None:
        self._moment("shared", body="正文", location="杭州")
        mine = self.store.save_memory_description("moment", "shared", {"body": "只给自己", "visibility": "private", "expected_version": 0}, DEMO_USER)
        self.store.refresh_memory_completion_suggestions(DEMO_PARTNER)
        self.assertFalse(any(item["suggestion_kind"] == "missing_partner_description" for item in self.store.list_memory_completion_suggestions(DEMO_PARTNER)["items"]))
        self.store.save_memory_description("moment", "shared", {"body": "一起看到的晚霞", "visibility": "shared", "expected_version": mine["version"]}, DEMO_USER)
        self.store.refresh_memory_completion_suggestions(DEMO_PARTNER)
        self.assertTrue(any(item["suggestion_kind"] == "missing_partner_description" for item in self.store.list_memory_completion_suggestions(DEMO_PARTNER)["items"]))

    def test_dismissed_and_completed_rows_are_not_resurrected_and_snooze_is_respected(self) -> None:
        self._moment("needs")
        self.store.refresh_memory_completion_suggestions(DEMO_USER)
        item = self.store.list_memory_completion_suggestions(DEMO_USER)["items"][0]
        dismiss_payload = {
            "action": "dismiss", "expected_version": item["version"],
            "idempotency_key": "memory-dismiss-repeat-001",
        }
        dismissed = self.store.respond_memory_completion_suggestion(item["id"], dismiss_payload, DEMO_USER)
        self.assertEqual(
            self.store.respond_memory_completion_suggestion(item["id"], dismiss_payload, DEMO_USER),
            dismissed,
        )
        self.store.refresh_memory_completion_suggestions(DEMO_USER)
        self.assertNotIn(dismissed["id"], {row["id"] for row in self.store.list_memory_completion_suggestions(DEMO_USER)["items"]})
        restored = self.store.respond_memory_completion_suggestion(dismissed["id"], {"action": "restore", "expected_version": dismissed["version"]}, DEMO_USER)
        future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
        snoozed = self.store.respond_memory_completion_suggestion(restored["id"], {"action": "snooze", "remind_at": future, "expected_version": restored["version"]}, DEMO_USER)
        self.assertNotIn(snoozed["id"], {row["id"] for row in self.store.list_memory_completion_suggestions(DEMO_USER)["items"]})
        with self.store.connection() as db:
            db.execute("UPDATE memory_completion_suggestions SET remind_at='2020-01-01T00:00:00+00:00' WHERE id=?", (snoozed["id"],))
        self.assertIn(snoozed["id"], {row["id"] for row in self.store.list_memory_completion_suggestions(DEMO_USER)["items"]})

    def test_descriptions_are_separate_and_merge_requires_second_member_and_exact_versions(self) -> None:
        self._moment("merge", body="旧正文", location="杭州")
        first = self.store.save_memory_description("moment", "merge", {"body": "演示甲的描述", "visibility": "shared", "expected_version": 0}, DEMO_USER)
        second = self.store.save_memory_description("moment", "merge", {"body": "演示乙的描述", "visibility": "shared", "expected_version": 0}, DEMO_PARTNER)
        proposal = self.store.propose_memory_description_merge("merge", {"body": "我们一起看到了晚霞", "expected_parent_version": 1, "expected_versions": {DEMO_USER: first["version"], DEMO_PARTNER: second["version"]}}, DEMO_USER)
        self.assertTrue(proposal["proposal_id"].startswith("memory_merge_"))
        waiting = self.store.get_memory_descriptions("moment", "merge", DEMO_USER)["merge_proposal"]
        actionable = self.store.get_memory_descriptions("moment", "merge", DEMO_PARTNER)["merge_proposal"]
        self.assertEqual(waiting, {"proposal_id": proposal["proposal_id"], "version": proposal["version"], "body": "我们一起看到了晚霞", "proposer_id": DEMO_USER, "can_confirm": False})
        self.assertEqual(actionable, {**waiting, "can_confirm": True})
        versions_after = {
            item["author_id"]: item["version"]
            for item in self.store.get_memory_descriptions("moment", "merge", DEMO_PARTNER)["items"]
        }
        with self.assertRaises(ConflictError):
            self.store.propose_memory_description_merge("merge", {
                "body": "并发的另一份提议", "expected_parent_version": 1,
                "expected_versions": versions_after,
            }, DEMO_PARTNER)
        with self.assertRaises(PermissionError):
            self.store.confirm_memory_description_merge("merge", proposal["proposal_id"], proposal["version"], DEMO_USER)
        with self.assertRaises(KeyError):
            self.store.confirm_memory_description_merge("merge", "memory_merge_missing", proposal["version"], DEMO_PARTNER)
        confirmed = self.store.confirm_memory_description_merge("merge", proposal["proposal_id"], proposal["version"], DEMO_PARTNER)
        self.assertEqual(confirmed["body"], "我们一起看到了晚霞")
        self.assertEqual(confirmed["version"], 2)
        for internal in ("couple_id", "creator_id", "deleted_at", "purged_at", "media_id", "source_message_id"):
            self.assertNotIn(internal, confirmed)
        with self.assertRaises((ConflictError, KeyError)):
            self.store.confirm_memory_description_merge("merge", proposal["proposal_id"], proposal["version"], DEMO_PARTNER)

    def test_completion_and_description_lists_recheck_parent_visibility(self) -> None:
        self._moment("private", visibility="private")
        self.store.refresh_memory_completion_suggestions(DEMO_USER)
        self.assertTrue(self.store.list_memory_completion_suggestions(DEMO_USER)["items"])
        self.assertEqual(self.store.list_memory_completion_suggestions(DEMO_PARTNER)["items"], [])
        with self.store.connection() as db:
            db.execute("UPDATE couples SET status='unlinked' WHERE id=?", (DEMO_COUPLE,))
        with self.assertRaises(ValueError):
            self.store.list_memory_completion_suggestions(DEMO_USER)

    def test_two_concurrent_merge_proposals_leave_exactly_one_pending(self) -> None:
        self._moment("merge-race", body="旧正文", location="杭州")
        first = self.store.save_memory_description("moment", "merge-race", {"body": "演示甲", "visibility": "shared", "expected_version": 0}, DEMO_USER)
        second = self.store.save_memory_description("moment", "merge-race", {"body": "演示乙", "visibility": "shared", "expected_version": 0}, DEMO_PARTNER)
        versions = {DEMO_USER: first["version"], DEMO_PARTNER: second["version"]}
        barrier = threading.Barrier(2)

        def propose(user_id: str) -> str:
            barrier.wait()
            try:
                self.store.propose_memory_description_merge("merge-race", {
                    "body": f"{user_id} 的提议", "expected_parent_version": 1,
                    "expected_versions": versions,
                }, user_id)
                return "success"
            except ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(propose, (DEMO_USER, DEMO_PARTNER)))
        self.assertEqual(sorted(outcomes), ["conflict", "success"])
        with self.store.connection() as db:
            pending = db.execute(
                "SELECT COUNT(*) AS count FROM memory_descriptions WHERE moment_id='merge-race' AND merge_proposal_json IS NOT NULL"
            ).fetchone()["count"]
        self.assertEqual(pending, 1)

    def test_audience_resolver_returns_safe_versioned_metadata_only(self) -> None:
        self._moment("resolver", body="共同正文", location="杭州")
        mine = self.store.save_memory_description(
            "moment", "resolver",
            {"body": "我的私密补充", "visibility": "private", "expected_version": 0},
            DEMO_USER,
        )
        with self.store.connection() as db:
            personal = self.store.resolve_memory_for_audience(
                db, "moment", "resolver", DEMO_USER, audience="personal", expected_version=1,
            )
            shared = self.store.resolve_memory_for_audience(
                db, "moment", "resolver", DEMO_PARTNER, audience="shared", expected_version=1,
            )
        self.assertEqual(personal["descriptions"][0]["id"], mine["id"])
        self.assertEqual(shared["descriptions"], [])
        self.assertEqual(personal["dependency_versions"]["moment:resolver"], 1)
        self.assertEqual(personal["source_type"], "human")
        self.assertNotIn("storage_key", str(personal))
        self.assertNotIn("evidence", str(personal))

    def test_exif_capture_time_is_normalized_and_all_metadata_is_removed(self) -> None:
        image = Image.new("RGB", (24, 24), "red")
        exif = Image.Exif()
        exif[36867] = "2025:10:16 20:30:15"
        exif[36881] = "+08:00"
        exif[271] = "Secret Camera"
        source = io.BytesIO()
        image.save(source, format="JPEG", exif=exif)
        sanitized = sanitize_image(source.getvalue(), "image/jpeg", "Asia/Shanghai")
        self.assertEqual(sanitized.captured_at, "2025-10-16T12:30:15+00:00")
        stored = Image.open(io.BytesIO(sanitized.content))
        self.assertEqual(len(stored.getexif()), 0)

    def test_invalid_or_future_exif_never_becomes_capture_time(self) -> None:
        for value, offset in (("0000:00:00 00:00:00", "+08:00"), ("2099:01:01 00:00:00", "+08:00"), ("2025:10:16 20:30:15", "+99:00")):
            image = Image.new("RGB", (12, 12), "blue")
            exif = Image.Exif(); exif[36867] = value; exif[36881] = offset
            source = io.BytesIO(); image.save(source, format="JPEG", exif=exif)
            with self.subTest(value=value, offset=offset):
                self.assertIsNone(sanitize_image(source.getvalue(), "image/jpeg", "Asia/Shanghai").captured_at)

    def test_exif_date_and_offset_tags_are_never_mixed_across_groups(self) -> None:
        image = Image.new("RGB", (12, 12), "green")
        exif = Image.Exif()
        exif[36867] = "2025:10:16 20:30:15"  # DateTimeOriginal
        exif[36882] = "+09:00"  # OffsetTimeDigitized must not apply to Original
        source = io.BytesIO()
        image.save(source, format="JPEG", exif=exif)
        self.assertEqual(
            sanitize_image(source.getvalue(), "image/jpeg", "Asia/Shanghai").captured_at,
            "2025-10-16T12:30:15+00:00",
        )
        second = Image.new("RGB", (12, 12), "yellow")
        second_exif = Image.Exif()
        second_exif[36867] = "not-a-date"
        second_exif[36881] = "+05:00"  # Original group is invalid.
        second_exif[36868] = "2025:10:17 20:30:15"  # Digitized has no own offset.
        second_source = io.BytesIO()
        second.save(second_source, format="JPEG", exif=second_exif)
        self.assertEqual(
            sanitize_image(second_source.getvalue(), "image/jpeg", "Asia/Shanghai").captured_at,
            "2025-10-17T12:30:15+00:00",
        )

    def test_completion_response_is_an_allowlisted_dto(self) -> None:
        self._moment("dto")
        self.store.refresh_memory_completion_suggestions(DEMO_USER)
        item = self.store.list_memory_completion_suggestions(DEMO_USER)["items"][0]
        response = self.store.respond_memory_completion_suggestion(
            item["id"], {"action": "dismiss", "expected_version": item["version"]}, DEMO_USER,
        )
        for internal in ("couple_id", "user_id", "moment_id", "trip_id", "deleted_at", "purged_at"):
            self.assertNotIn(internal, response)


if __name__ == "__main__":
    unittest.main()
