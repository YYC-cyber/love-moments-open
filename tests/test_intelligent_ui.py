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
