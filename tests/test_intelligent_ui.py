from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from love_moments.intelligent_ui import ConflictError, SURFACES, validate_manifest
from love_moments.store import DEMO_PARTNER, DEMO_TRIP, LoveMomentsStore


class CapturingComposer:
    name = "ai"

    def __init__(self, result: list[str] | None = None):
        self.result = result or ["couple_overview"]
        self.context = None

    def compose(self, surface, context, intent):
        self.context = context
        return self.result


class IntelligentUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(Path(self.temp.name) / "ui-test.db", seed_demo_data=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_all_rule_surfaces_return_valid_manifests(self) -> None:
        for surface in SURFACES:
            with self.subTest(surface=surface):
                manifest = validate_manifest(self.store.get_ui_manifest(surface))
                self.assertEqual(manifest["surface"], surface)
                self.assertEqual(manifest["generatedBy"], "rules")
                self.assertLessEqual(len(manifest["sections"]) + len(manifest["personalOverlay"]), 24)

    def test_shared_identity_matches_but_personal_overlay_is_isolated(self) -> None:
        self.store.create_moment({"title": "只有我知道", "visibility": "private"})
        mine = self.store.get_ui_manifest("timeline")
        partner = self.store.get_ui_manifest("timeline", user_id=DEMO_PARTNER)
        self.assertEqual(mine["sharedManifestId"], partner["sharedManifestId"])
        self.assertNotEqual(mine["personalOverlay"][0]["props"], partner["personalOverlay"][0]["props"])

    def test_shared_recomposition_reaches_partner_with_partner_owned_actions(self) -> None:
        composed = self.store.compose_ui("trip", "采用性价比方案并优先看预算")
        partner = self.store.get_ui_manifest("trip", user_id=DEMO_PARTNER)
        self.assertEqual([node["type"] for node in composed["manifest"]["sections"]], [node["type"] for node in partner["sections"]])
        self.assertEqual(composed["manifest"]["sharedManifestId"], partner["sharedManifestId"])
        self.assertFalse(any(action["requiresConfirmation"] for node in partner["sections"] for action in node["actions"]))
        mine = next(action for node in composed["manifest"]["sections"] for action in node["actions"] if action["requiresConfirmation"])
        self.assertEqual(self.store.preview_ui_action(mine["id"])["id"], mine["id"])

    def test_default_context_excludes_bodies_and_explicit_grant_is_audited(self) -> None:
        composer = CapturingComposer(["message_summary"])
        self.store.ui_composer = composer
        self.store.compose_ui("chat", "整理重点")
        self.assertEqual(composer.context["authorizedContent"], [])
        self.assertNotIn("刚刚看到", str(composer.context))
        self.store.compose_ui("chat", "只整理这一条", ["message:msg-1"])
        self.assertEqual(composer.context["authorizedContent"][0]["ref"], "message:msg-1")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_context_grants").fetchone()[0], 1)

    def test_invalid_model_component_falls_back_without_data_change(self) -> None:
        self.store.ui_composer = CapturingComposer(["script_injection"])
        before = len(self.store.list_messages())
        job = self.store.compose_ui("home", "随便整理")
        self.assertEqual(job["errorCode"], "MODEL_FALLBACK")
        self.assertEqual(job["manifest"]["generatedBy"], "rules")
        self.assertEqual(self.store.get_ui_job(job["id"])["errorCode"], "MODEL_FALLBACK")
        self.assertEqual(len(self.store.list_messages()), before)

    def test_preview_confirm_cancel_and_idempotency(self) -> None:
        manifest = self.store.compose_ui("home", "记录瞬间：今天一起看了晚霞")["manifest"]
        action = next(action for node in manifest["sections"] for action in node["actions"] if action["requiresConfirmation"])
        before = len(self.store.list_moments())
        preview = self.store.preview_ui_action(action["id"])
        self.assertEqual(len(self.store.list_moments()), before)
        first = self.store.confirm_ui_action(action["id"], "idem-home-0001")
        second = self.store.confirm_ui_action(action["id"], "idem-home-0001")
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(len(self.store.list_moments()), before + 1)

        life = self.store.compose_ui("life", "添加愿望：一起去看极光")["manifest"]
        life_action = next(action for node in life["sections"] for action in node["actions"] if action["requiresConfirmation"])
        self.assertTrue(self.store.cancel_ui_action(life_action["id"])["ok"])
        with self.assertRaises(ValueError):
            self.store.confirm_ui_action(life_action["id"], "idem-life-0001")

    def test_action_idempotency_is_scoped_to_creator_couple_and_action(self) -> None:
        alice = self.store.register("alice_private", "safe-password-100", "甲")
        bob = self.store.register("bob_private", "safe-password-200", "乙")
        invite = self.store.create_user_invite(alice["id"])
        self.store.accept_user_invite(bob["id"], invite["code"], "2024-02-14")
        foreign = self.store.compose_ui("home", "记录瞬间：另一对的私密测试", user_id=alice["id"])["manifest"]
        foreign_action = next(action for node in foreign["sections"] for action in node["actions"] if action["requiresConfirmation"])
        foreign_result = self.store.confirm_ui_action(foreign_action["id"], "idem-isolation-0001", user_id=alice["id"])
        self.assertIn("另一对的私密测试", foreign_result["result"]["title"])

        own = self.store.compose_ui("home", "记录瞬间：当前情侣自己的内容")["manifest"]
        own_action = next(action for node in own["sections"] for action in node["actions"] if action["requiresConfirmation"])
        with self.assertRaises(ConflictError):
            self.store.confirm_ui_action(own_action["id"], "idem-isolation-0001")
        with self.assertRaises(KeyError):
            self.store.confirm_ui_action(foreign_action["id"], "idem-isolation-0001")

    def test_committed_domain_write_recovers_an_executing_action_without_duplicate(self) -> None:
        manifest = self.store.compose_ui("home", "记录瞬间：崩溃恢复只创建一次")["manifest"]
        action = next(action for node in manifest["sections"] for action in node["actions"] if action["requiresConfirmation"])
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER fail_ui_confirmation BEFORE INSERT ON audit_events WHEN NEW.action='ui.action_confirmed' BEGIN SELECT RAISE(ABORT, 'forced finalization failure'); END")
        with self.assertRaises(Exception):
            self.store.confirm_ui_action(action["id"], "idem-recovery-0001")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM ui_action_drafts WHERE id=?", (action["id"],)).fetchone()[0], "executing")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM moments WHERE title='崩溃恢复只创建一次'").fetchone()[0], 1)
            db.execute("DROP TRIGGER fail_ui_confirmation")
        recovered = self.store.confirm_ui_action(action["id"], "idem-recovery-0001")
        self.assertFalse(recovered["idempotent"])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM ui_action_drafts WHERE id=?", (action["id"],)).fetchone()[0], "confirmed")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM moments WHERE title='崩溃恢复只创建一次'").fetchone()[0], 1)

    def test_committed_trip_selection_recovers_without_a_second_revision(self) -> None:
        manifest = self.store.compose_ui("trip", "采用经典充实方案")["manifest"]
        action = next(
            action
            for node in manifest["sections"]
            if node["type"] == "trip_variant_compare"
            for action in node["actions"]
            if action["type"] == "select_trip_variant"
        )
        before = self.store.get_trip(DEMO_TRIP)
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER fail_trip_ui_confirmation BEFORE INSERT ON audit_events WHEN NEW.action='ui.action_confirmed' BEGIN SELECT RAISE(ABORT, 'forced trip finalization failure'); END")
        with self.assertRaises(Exception):
            self.store.confirm_ui_action(action["id"], "idem-trip-recovery-0001")
        changed = self.store.get_trip(DEMO_TRIP)
        self.assertEqual(changed["active_variant"], "classic")
        self.assertEqual(changed["current_revision"], before["current_revision"] + 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM ui_action_drafts WHERE id=?", (action["id"],)).fetchone()[0], "executing")
            db.execute("DROP TRIGGER fail_trip_ui_confirmation")
        recovered = self.store.confirm_ui_action(action["id"], "idem-trip-recovery-0001")
        self.assertFalse(recovered["idempotent"])
        final = self.store.get_trip(DEMO_TRIP)
        self.assertEqual(final["current_revision"], changed["current_revision"])
        self.assertEqual(final["version"], changed["version"])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM ui_action_drafts WHERE id=?", (action["id"],)).fetchone()[0], "confirmed")
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM plan_revisions WHERE trip_id=? AND reason='采用整套方案'", (DEMO_TRIP,)).fetchone()[0],
                1,
            )

    def test_orphaned_ui_jobs_are_closed_after_server_restart(self) -> None:
        with self.store.connection() as db:
            couple = db.execute("SELECT id FROM couples LIMIT 1").fetchone()[0]
            user = db.execute("SELECT member_a FROM couples WHERE id=?", (couple,)).fetchone()[0]
            db.execute(
                "INSERT INTO ui_generation_jobs(id,couple_id,user_id,surface,intent_hash,status,created_at) VALUES (?,?,?,?,?,'queued',?)",
                ("ui_job_restart_queued", couple, user, "home", "queued-hash", "2026-10-28T00:00:00+00:00"),
            )
            db.execute(
                "INSERT INTO ui_generation_jobs(id,couple_id,user_id,surface,intent_hash,status,created_at) VALUES (?,?,?,?,?,'running',?)",
                ("ui_job_restart_running", couple, user, "trip", "running-hash", "2026-10-28T00:00:00+00:00"),
            )
            db.execute(
                "INSERT INTO ui_generation_jobs(id,couple_id,user_id,surface,intent_hash,status,error_code,created_at,completed_at) VALUES (?,?,?,?,?,'completed',NULL,?,?)",
                ("ui_job_restart_completed", couple, user, "life", "completed-hash", "2026-10-28T00:00:00+00:00", "2026-10-28T00:01:00+00:00"),
            )

        LoveMomentsStore(self.store.db_path)
        with self.store.connection() as db:
            queued = db.execute("SELECT status,error_code,completed_at FROM ui_generation_jobs WHERE id='ui_job_restart_queued'").fetchone()
            running = db.execute("SELECT status,error_code,completed_at FROM ui_generation_jobs WHERE id='ui_job_restart_running'").fetchone()
            completed = db.execute("SELECT status,error_code,completed_at FROM ui_generation_jobs WHERE id='ui_job_restart_completed'").fetchone()
        self.assertEqual((queued["status"], queued["error_code"]), ("failed", "SERVER_RESTARTED"))
        self.assertEqual((running["status"], running["error_code"]), ("failed", "SERVER_RESTARTED"))
        self.assertTrue(queued["completed_at"])
        self.assertTrue(running["completed_at"])
        self.assertEqual((completed["status"], completed["error_code"]), ("completed", None))
        self.assertEqual(completed["completed_at"], "2026-10-28T00:01:00+00:00")

    def test_stale_trip_action_is_rejected(self) -> None:
        manifest = self.store.compose_ui("trip", "采用经典充实方案")["manifest"]
        action = next(action for node in manifest["sections"] if node["type"] == "trip_variant_compare" for action in node["actions"] if action["type"] == "select_trip_variant")
        self.store.select_variant(DEMO_TRIP, "value")
        with self.assertRaises(ConflictError):
            self.store.confirm_ui_action(action["id"], "idem-trip-0001")

    def test_job_is_scoped_to_creator(self) -> None:
        job = self.store.compose_ui("life", "只看待确认事项")
        self.assertEqual(self.store.get_ui_job(job["id"])["status"], "completed")
        with self.assertRaises(KeyError):
            self.store.get_ui_job(job["id"], user_id=DEMO_PARTNER)

    def test_every_smart_card_has_real_navigation(self) -> None:
        for surface in SURFACES:
            manifest = self.store.get_ui_manifest(surface)
            for node in [*manifest["sections"], *manifest["personalOverlay"]]:
                with self.subTest(surface=surface, component=node["type"]):
                    action = next((item for item in node["actions"] if item["type"] == "navigate"), None)
                    self.assertIsNotNone(action)
                    self.assertFalse(action["requiresConfirmation"])

    def test_anniversary_intent_previews_confirms_and_is_visible_to_both(self) -> None:
        job = self.store.compose_ui("life", "添加纪念日 2026年12月24日 第一次一起看雪")
        result = self.store.get_ui_job(job["id"])
        node = next(item for item in result["manifest"]["sections"] if item["type"] == "anniversary")
        action = next(item for item in node["actions"] if item["type"] == "create_calendar_entry")
        preview = self.store.preview_ui_action(action["id"])
        self.assertIn("第一次一起看雪", preview["preview"]["after"])
        confirmed = self.store.confirm_ui_action(action["id"], "idem-anniversary-0001")
        self.assertEqual(confirmed["result"]["entry_type"], "anniversary")
        self.assertEqual(confirmed["result"]["starts_at"], "2026-12-24")
        mine = self.store.list_calendar_entries(DEMO_PARTNER, "anniversary")
        self.assertTrue(any(item["id"] == confirmed["result"]["id"] for item in mine))

    def test_anniversary_without_valid_date_does_not_claim_a_write(self) -> None:
        job = self.store.compose_ui("life", "帮我添加一个纪念日")
        node = next(item for item in job["manifest"]["sections"] if item["type"] == "anniversary")
        self.assertFalse(any(item["type"] == "create_calendar_entry" for item in node["actions"]))


if __name__ == "__main__":
    unittest.main()
