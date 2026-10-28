from __future__ import annotations

import unittest
from pathlib import Path

from love_moments.store import DEMO_USER

from tests.test_kitchen import KitchenTestCase


class Task7ReviewRegressionTests(KitchenTestCase):
    def test_completed_list_replays_after_a_new_active_list_exists(self) -> None:
        self.store.create_shopping_item(
            {"name": "苹果", "quantity": 1, "unit": "个", "category": "produce"},
            DEMO_USER,
        )
        first_list = self.store.get_shopping_list(DEMO_USER)
        request = {
            "expected_version": first_list["version"],
            "allow_incomplete": True,
            "stock_checked_item_ids": [],
            "stock_item_versions": {},
            "idempotency_key": "complete-replay-after-new-list-0001",
        }
        completed = self.store.complete_shopping_list(request, DEMO_USER)
        self.store.create_shopping_item(
            {"name": "香蕉", "quantity": 1, "unit": "个", "category": "produce"},
            DEMO_USER,
        )

        self.assertEqual(self.store.complete_shopping_list(request, DEMO_USER), completed)
        active = self.store.get_shopping_list(DEMO_USER)
        self.assertEqual(active["status"], "active")
        self.assertEqual([item["display_name"] for item in active["items"]], ["香蕉"])

    def test_profile_tags_must_be_strings(self) -> None:
        for index, value in enumerate(({"nested": "清淡"}, 1, True, None)):
            current = next(
                profile for profile in self.store.get_food_profiles(DEMO_USER)["profiles"]
                if profile["user_id"] == DEMO_USER
            )
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.update_my_food_profile({
                    "allergies": [],
                    "avoidances": [],
                    "preferences": {"liked_tags": [value], "disliked_tags": []},
                    "expected_version": current["version"],
                    "idempotency_key": f"profile-tag-type-{index:04d}",
                }, DEMO_USER)

    def test_pantry_response_exposes_canonical_aliases_for_safe_client_cas(self) -> None:
        item = self.pantry("番茄")
        self.assertIn("西红柿", item["aliases"])
        listed = self.store.list_pantry_items(DEMO_USER)["items"][0]
        self.assertEqual(listed["id"], item["id"])
        self.assertIn("西红柿", listed["aliases"])

    def test_h5_and_mobile_match_aliases_before_upsert(self) -> None:
        root = Path(__file__).resolve().parents[1]
        h5 = (root / "static/app.js").read_text(encoding="utf-8")
        mobile = (root / "clients/mobile/App.tsx").read_text(encoding="utf-8")
        client = (root / "packages/domain-client/src/index.ts").read_text(encoding="utf-8")
        self.assertIn("(item.aliases || []).includes(name)", h5)
        self.assertIn("item.aliases.includes(name)", mobile)
        self.assertIn("aliases: string[]", client)
        self.assertIn("ingredient_id: current.ingredient_id", h5)
        self.assertIn("ingredient_id: current.ingredient_id", mobile)


if __name__ == "__main__":
    unittest.main()
