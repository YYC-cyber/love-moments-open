from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from love_moments.store import DEMO_COUPLE, DEMO_PARTNER, DEMO_USER, LoveMomentsStore
from tests import VALID_PNG


ROOT = Path(__file__).resolve().parents[1]


class SecurityFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LoveMomentsStore(Path(self.temp.name) / "test.db", seed_demo_data=True)

    def test_pending_media_is_not_partner_readable_before_atomic_attachment(self):
        media = self.store.save_media(VALID_PNG, "pending.png", "image/png", "shared")
        with self.assertRaises(PermissionError):
            self.store.get_media_content(media["id"], DEMO_PARTNER)
        self.assertNotIn(media["id"], {item["id"] for item in self.store.list_media(DEMO_PARTNER)})
        with self.assertRaises(PermissionError):
            self.store.create_moment({"title": "not mine", "media_id": media["id"]}, DEMO_PARTNER)
        self.store.create_moment({"title": "now shared", "media_id": media["id"], "visibility": "shared"})
        self.assertTrue(self.store.get_media_content(media["id"], DEMO_PARTNER)[0])

    def test_delayed_reveal_rechecks_relationship_before_promoting_media(self):
        media = self.store.save_media(VALID_PNG, "delayed.png", "image/png", "private")
        reveal = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        moment = self.store.create_moment({"title": "later", "visibility": "delayed", "reveal_at": reveal, "media_id": media["id"]})
        with self.store.connection() as db:
            db.execute("UPDATE moments SET reveal_at='2020-01-01T00:00:00+00:00' WHERE id=?", (moment["id"],))
            db.execute("UPDATE media_assets SET reveal_at='2020-01-01T00:00:00+00:00' WHERE id=?", (media["id"],))
            db.execute("UPDATE users SET deleted_at='2020-01-01T00:00:00+00:00' WHERE id=?", (DEMO_USER,))
        with self.assertRaises(PermissionError):
            self.store.get_media_content(media["id"], DEMO_PARTNER)
        with self.store.connection() as db:
            self.store._sync_media_visibility(db, media["id"])
            self.assertEqual(db.execute("SELECT visibility FROM media_assets WHERE id=?", (media["id"],)).fetchone()[0], "private")
        self.assertNotIn(moment["id"], {item["id"] for item in self.store.list_moments(user_id=DEMO_PARTNER)})

    def test_atomic_attachment_rolls_back_media_promotion_on_failure(self):
        media = self.store.save_media(VALID_PNG, "rollback.png", "image/png")
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER fail_moment_audit BEFORE INSERT ON audit_events WHEN NEW.action='moment.created' BEGIN SELECT RAISE(ABORT, 'forced failure'); END")
        with self.assertRaises(Exception):
            self.store.create_moment({"title": "rollback", "media_id": media["id"]})
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM moments WHERE media_id=?", (media["id"],)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT visibility FROM media_assets WHERE id=?", (media["id"],)).fetchone()[0], "private")

    def test_recipe_cover_becomes_shared_only_after_authoritative_attachment(self):
        with self.store.connection() as db:
            recipe_id = db.execute("SELECT id FROM recipes WHERE source_type='built_in' LIMIT 1").fetchone()[0]
        recipe = self.store.copy_recipe(recipe_id, {"visibility": "couple"}, DEMO_USER)
        media = self.store.save_media(VALID_PNG, "cover.png", "image/png")
        with self.assertRaises(PermissionError):
            self.store.get_media_content(media["id"], DEMO_PARTNER)
        self.store.add_recipe_media(recipe["id"], {"media_type": "image", "role": "cover", "url": media["url"], "confirmed_exact": True}, DEMO_USER)
        self.assertTrue(self.store.get_media_content(media["id"], DEMO_PARTNER)[0])

    def test_delayed_reveal_keeps_content_private_after_unlink_or_target_deletion(self):
        for change in ("relationship", "target"):
            with self.subTest(change=change):
                media = self.store.save_media(VALID_PNG, f"{change}.png", "image/png")
                future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
                moment = self.store.create_moment({"title": "later", "media_id": media["id"], "visibility": "delayed", "reveal_at": future})
                with self.store.connection() as db:
                    db.execute("UPDATE moments SET reveal_at='2020-01-01T00:00:00+00:00' WHERE id=?", (moment["id"],))
                    if change == "relationship":
                        db.execute("UPDATE couples SET status='unlinked' WHERE id=?", (DEMO_COUPLE,))
                    else:
                        db.execute("UPDATE users SET deleted_at='2020-01-01T00:00:00+00:00' WHERE id=?", (DEMO_PARTNER,))
                    self.store._reconcile_delayed_moments(db)
                    self.assertEqual(db.execute("SELECT visibility FROM moments WHERE id=?", (moment["id"],)).fetchone()[0], "private")
                    self.assertEqual(db.execute("SELECT visibility FROM media_assets WHERE id=?", (media["id"],)).fetchone()[0], "private")
                    db.execute("UPDATE couples SET status='active' WHERE id=?", (DEMO_COUPLE,))
                    db.execute("UPDATE users SET deleted_at=NULL WHERE id=?", (DEMO_PARTNER,))

    def run_browser_case(self, case):
        result = subprocess.run(["node", str(ROOT / "scripts/test_security_foundations.cjs"), case], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_offline_client_error_remains_needs_attention_with_same_idempotency_key(self):
        self.run_browser_case("queue")

    def test_cache_is_partitioned_and_cleared_on_auth_boundaries(self):
        self.run_browser_case("cache")

    def test_mobile_private_cache_clears_all_snapshots_and_manifests_when_identity_changes(self):
        self.run_browser_case("mobile")

    def test_denied_recipe_detail_evicts_cache_instead_of_rendering_it(self):
        self.run_browser_case("recipe_denial")

    def test_private_media_and_moment_events_are_owner_only_in_replay_and_export(self):
        cursor = self.store.list_realtime_events(user_id=DEMO_PARTNER)["latest_sequence"]
        media = self.store.save_media(VALID_PNG, "not-shared.png", "image/png")
        private = self.store.create_moment({"title": "private event", "visibility": "private", "media_id": media["id"]})
        delayed = self.store.create_moment({"title": "delayed event", "visibility": "delayed"})
        private_ids = {media["id"], private["id"], delayed["id"]}
        owner = self.store.list_realtime_events(cursor, user_id=DEMO_USER)
        self.assertTrue(private_ids.issubset({item["resource_id"] for item in owner["items"]}))
        partner = self.store.list_realtime_events(cursor, user_id=DEMO_PARTNER)
        self.assertFalse(private_ids.intersection(item["resource_id"] for item in partner["items"]))
        with self.store.connection() as db:
            for event in db.execute("SELECT sequence,payload_json FROM realtime_events WHERE resource_id IN (?,?,?)", tuple(private_ids)).fetchall():
                legacy_payload = json.loads(event["payload_json"])
                legacy_payload.pop("owner_only", None)
                db.execute("UPDATE realtime_events SET payload_json=? WHERE sequence=?", (json.dumps(legacy_payload), event["sequence"]))
        self.assertFalse(private_ids.intersection(item["resource_id"] for item in self.store.list_realtime_events(cursor, user_id=DEMO_PARTNER)["items"]))
        exported, _ = self.store.account_export(DEMO_PARTNER)
        with zipfile.ZipFile(io.BytesIO(exported)) as archive:
            data = json.loads(archive.read("data.json"))
        self.assertNotIn("realtime_events", data)
        self.assertNotIn("audit_events", data)
        self.assertIn("realtime_events", data["export_exclusions"])
        self.assertIn("audit_events", data["export_exclusions"])
        shared = self.store.create_moment({"title": "shared event", "visibility": "shared"})
        self.assertIn(shared["id"], {item["resource_id"] for item in self.store.list_realtime_events(cursor, user_id=DEMO_PARTNER)["items"]})

    def test_recipe_deletion_revokes_cover_access_in_the_same_transaction(self):
        with self.store.connection() as db:
            source = db.execute("SELECT id FROM recipes WHERE source_type='built_in' LIMIT 1").fetchone()[0]
        recipe = self.store.copy_recipe(source, {"visibility": "couple"}, DEMO_USER)
        media = self.store.save_media(VALID_PNG, "deleted-recipe.png", "image/png")
        self.store.add_recipe_media(recipe["id"], {"media_type": "image", "role": "cover", "url": media["url"], "confirmed_exact": True}, DEMO_USER)
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER fail_recipe_delete BEFORE INSERT ON audit_events WHEN NEW.action='recipe.deleted' BEGIN SELECT RAISE(ABORT, 'forced rollback'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.delete_recipe(recipe["id"], recipe["version"], DEMO_USER)
        with self.store.connection() as db:
            self.assertIsNone(db.execute("SELECT deleted_at FROM recipes WHERE id=?", (recipe["id"],)).fetchone()[0])
            self.assertEqual(db.execute("SELECT visibility FROM media_assets WHERE id=?", (media["id"],)).fetchone()[0], "shared")
            db.execute("DROP TRIGGER fail_recipe_delete")
        self.assertTrue(self.store.get_media_content(media["id"], DEMO_PARTNER)[0])
        self.store.delete_recipe(recipe["id"], recipe["version"], DEMO_USER)
        with self.assertRaises(PermissionError):
            self.store.get_media_content(media["id"], DEMO_PARTNER)
        self.assertTrue(self.store.get_media_content(media["id"], DEMO_USER)[0])

    def test_shared_details_and_comments_revalidate_delayed_release(self):
        for invalid in ("author", "couple"):
            with self.subTest(invalid=invalid):
                moment = self.store.create_moment({"title": "unreleased private content", "visibility": "delayed"})
                with self.store.connection() as db:
                    db.execute("UPDATE moments SET reveal_at='2020-01-01T00:00:00+00:00' WHERE id=?", (moment["id"],))
                    if invalid == "author":
                        db.execute("UPDATE users SET deleted_at='2020-01-01T00:00:00+00:00' WHERE id=?", (DEMO_USER,))
                    else:
                        db.execute("UPDATE couples SET status='unlinked' WHERE id=?", (DEMO_COUPLE,))
                try:
                    with self.assertRaises((KeyError, ValueError)):
                        self.store.get_shared_content("moment", moment["id"], DEMO_PARTNER)
                    with self.assertRaises((KeyError, ValueError)):
                        self.store.create_shared_comment("moment", moment["id"], "must not publish", DEMO_PARTNER)
                    with self.store.connection() as db:
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM shared_comments WHERE resource_id=?", (moment["id"],)).fetchone()[0], 0)
                finally:
                    with self.store.connection() as db:
                        db.execute("UPDATE users SET deleted_at=NULL WHERE id=?", (DEMO_USER,))
                        db.execute("UPDATE couples SET status='active' WHERE id=?", (DEMO_COUPLE,))

    def test_shared_detail_promotes_valid_delayed_moment_before_commenting(self):
        moment = self.store.create_moment({"title": "valid delayed release", "visibility": "delayed"})
        with self.store.connection() as db:
            db.execute("UPDATE moments SET reveal_at='2020-01-01T00:00:00+00:00' WHERE id=?", (moment["id"],))
        self.assertEqual(self.store.get_shared_content("moment", moment["id"], DEMO_PARTNER)["title"], "valid delayed release")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT visibility FROM moments WHERE id=?", (moment["id"],)).fetchone()[0], "shared")
        self.assertEqual(self.store.create_shared_comment("moment", moment["id"], "now shared", DEMO_PARTNER)["body"], "now shared")

    def test_hidden_replay_page_does_not_skip_the_following_shared_message(self):
        private = [self.store.create_moment({"title": f"private {index}", "visibility": "private"}) for index in range(100)]
        shared = self.store.create_message({"content": "deliver after private page"})
        partner_page = self.store.list_realtime_events(0, user_id=DEMO_PARTNER)
        self.assertEqual([item["resource_id"] for item in partner_page["items"]], [shared["id"]])
        owner_page = self.store.list_realtime_events(0, limit=1, user_id=DEMO_USER)
        self.assertEqual([item["resource_id"] for item in owner_page["items"]], [private[0]["id"]])
        self.assertEqual(owner_page["latest_sequence"], owner_page["items"][0]["sequence"])
        owner_next = self.store.list_realtime_events(owner_page["latest_sequence"], limit=200, user_id=DEMO_USER)
        self.assertEqual([item["resource_id"] for item in owner_next["items"]], [item["id"] for item in private[1:]] + [shared["id"]])

    def test_replay_scan_budget_advances_only_to_examined_rows(self):
        with self.store.connection() as db:
            db.executemany("INSERT INTO realtime_events(couple_id,actor_id,event_type,resource_id,payload_json,created_at) VALUES (?,?, 'moment.created',?, '{\"owner_only\":true}', '2026-10-27T00:00:00+00:00')", [(DEMO_COUPLE, DEMO_USER, f"private-{index}") for index in range(1100)])
        first_shared = self.store.create_message({"content": "first shared after scan budget"})
        second_shared = self.store.create_message({"content": "second shared after scan budget"})
        first_page = self.store.list_realtime_events(0, limit=1, user_id=DEMO_PARTNER)
        self.assertEqual(first_page["items"], [])
        self.assertGreater(first_page["latest_sequence"], 0)
        second_page = self.store.list_realtime_events(first_page["latest_sequence"], limit=1, user_id=DEMO_PARTNER)
        self.assertEqual([item["resource_id"] for item in second_page["items"]], [first_shared["id"]])
        self.assertEqual(second_page["latest_sequence"], second_page["items"][0]["sequence"])
        third_page = self.store.list_realtime_events(second_page["latest_sequence"], limit=1, user_id=DEMO_PARTNER)
        self.assertEqual([item["resource_id"] for item in third_page["items"]], [second_shared["id"]])
