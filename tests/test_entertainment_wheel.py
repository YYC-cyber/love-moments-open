from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from love_moments.intelligent_ui import ConflictError
from love_moments.store import DEMO_PARTNER, DEMO_USER, LoveMomentsStore


class CoupleWheelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(Path(self.temp.name) / "wheel.db", seed_demo_data=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_three_modes_have_distinct_safe_presets(self) -> None:
        normal = self.store.get_couple_wheel("normal", DEMO_USER)
        intimate = self.store.get_couple_wheel("intimate", DEMO_USER)
        wild = self.store.get_couple_wheel("wild", DEMO_USER)
        self.assertEqual(normal["mode_label"], "普通版")
        self.assertEqual(intimate["mode_label"], "情趣版")
        self.assertEqual(wild["mode_label"], "变态版")
        self.assertGreaterEqual(len(normal["options"]), 8)
        self.assertNotEqual({item["label"] for item in normal["options"]}, {item["label"] for item in wild["options"]})
        self.assertIn("成年", intimate["consent_notice"])
        self.assertIn("随时", wild["consent_notice"])

    def test_custom_option_and_result_are_shared_between_partners(self) -> None:
        created = self.store.create_wheel_option({"mode": "normal", "label": "一起买一杯奶茶"}, DEMO_USER)
        partner = self.store.get_couple_wheel("normal", DEMO_PARTNER)
        self.assertIn(created["id"], {item["id"] for item in partner["options"]})

        selected = [created["id"], partner["options"][0]["id"]]
        result = self.store.spin_couple_wheel({"mode": "normal", "option_ids": selected}, DEMO_PARTNER)
        self.assertIn(result["option_id"], selected)
        owner_history = self.store.get_couple_wheel("normal", DEMO_USER)["history"]
        self.assertEqual(owner_history[0]["id"], result["id"])
        self.assertEqual(owner_history[0]["spun_by"], DEMO_PARTNER)

    def test_custom_option_supports_versioned_update_and_delete(self) -> None:
        created = self.store.create_wheel_option({"mode": "wild", "label": "互相设计一张挑战卡"}, DEMO_USER)
        updated = self.store.update_wheel_option(created["id"], {"label": "互相设计一张大胆约会卡", "expected_version": 1}, DEMO_PARTNER)
        self.assertEqual(updated["version"], 2)
        with self.assertRaises(ConflictError):
            self.store.update_wheel_option(created["id"], {"label": "旧版本覆盖", "expected_version": 1}, DEMO_USER)
        deleted = self.store.delete_wheel_option(created["id"], 2, DEMO_USER)
        self.assertTrue(deleted["deleted"])
        self.assertNotIn(created["id"], {item["id"] for item in self.store.get_couple_wheel("wild", DEMO_PARTNER)["options"]})

    def test_boundaries_reject_invalid_or_coercive_content(self) -> None:
        with self.assertRaisesRegex(ValueError, "模式"):
            self.store.get_couple_wheel("unknown", DEMO_USER)
        with self.assertRaisesRegex(ValueError, "随时拒绝"):
            self.store.create_wheel_option({"mode": "intimate", "label": "对方不能拒绝"}, DEMO_USER)
        with self.assertRaisesRegex(ValueError, "至少选择两个"):
            option = self.store.get_couple_wheel("normal", DEMO_USER)["options"][0]
            self.store.spin_couple_wheel({"mode": "normal", "option_ids": [option["id"]]}, DEMO_USER)


if __name__ == "__main__":
    unittest.main()
