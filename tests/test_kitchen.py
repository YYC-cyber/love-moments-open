from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from love_moments.intelligent_ui import ConflictError
from love_moments.store import DEMO_PARTNER, DEMO_USER, LoveMomentsStore


class KitchenTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(Path(self.temp.name) / "kitchen.db", seed_demo_data=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def pantry(self, name: str, *, user_id: str = DEMO_USER, state: str = "have", key: str | None = None, **extra: object) -> dict:
        return self.store.upsert_pantry_item({
            "name": name,
            "state": state,
            "expected_version": 0,
            "idempotency_key": key or f"test:pantry:{uuid.uuid4().hex}",
            **extra,
        }, user_id)


class KitchenProfilePantryTests(KitchenTestCase):
    def test_only_owner_can_update_profile_and_both_members_can_read(self) -> None:
        coriander = self.pantry("香菜")
        initial = self.store.get_food_profiles(DEMO_USER)
        self.assertEqual([item["version"] for item in initial["profiles"]], [0, 0])

        saved = self.store.update_my_food_profile({
            "allergies": [{"allergen_key": "乳制品", "severity": "avoid"}],
            "avoidances": [{"ingredient_id": coriander["ingredient_id"]}],
            "preferences": {"liked_tags": ["清淡"], "disliked_tags": ["过甜"]},
            "expected_version": 0,
            "idempotency_key": "profile-owner-update-0001",
        }, DEMO_USER)
        partner_view = self.store.get_food_profiles(DEMO_PARTNER)
        self.assertEqual(saved, next(item for item in partner_view["profiles"] if item["user_id"] == DEMO_USER))
        self.assertEqual(saved["avoidances"][0]["canonical_name"], "香菜")

        with self.assertRaises(PermissionError):
            self.store.update_my_food_profile({
                "subject_user_id": DEMO_USER,
                "allergies": [], "avoidances": [], "preferences": {},
                "expected_version": 0, "idempotency_key": "profile-forged-owner-0001",
            }, DEMO_PARTNER)
        current = self.store.get_food_profiles(DEMO_USER)
        self.assertEqual(next(item for item in current["profiles"] if item["user_id"] == DEMO_USER)["version"], 1)

    def test_food_profiles_are_per_user_and_never_default_to_soy(self) -> None:
        initial = self.store.get_food_profiles(DEMO_USER)
        self.assertEqual([item["allergies"] for item in initial["profiles"]], [[], []])
        self.store.update_my_food_profile({
            "allergies": [{"allergen_key": "乳制品", "severity": "severe"}],
            "avoidances": [], "preferences": {}, "expected_version": 0,
            "idempotency_key": "profile-dairy-only-0001",
        }, DEMO_USER)
        self.store.update_my_food_profile({
            "allergies": [{"allergen_key": "花生", "severity": "avoid"}],
            "avoidances": [], "preferences": {}, "expected_version": 0,
            "idempotency_key": "profile-peanut-only-0001",
        }, DEMO_PARTNER)
        serialized = json.dumps(self.store.get_food_profiles(DEMO_USER), ensure_ascii=False)
        self.assertIn("乳制品", serialized)
        self.assertIn("花生", serialized)
        self.assertNotIn("大豆", serialized)
        self.assertNotIn("豆类", serialized)

    def test_profile_rejects_unknown_values_and_cross_couple_ingredient(self) -> None:
        outsider_a = self.store.register(f"outside_{uuid.uuid4().hex[:8]}", "Outside-pass-123!", "外甲")
        outsider_b = self.store.register(f"outside_{uuid.uuid4().hex[:8]}", "Outside-pass-456!", "外乙")
        invite = self.store.create_user_invite(outsider_a["id"])
        self.store.accept_user_invite(outsider_b["id"], invite["code"], "2026-01-01")
        foreign = self.pantry("私有香草", user_id=outsider_a["id"])
        invalid = [
            {"allergies": [{"allergen_key": "乳制品", "severity": "critical"}], "avoidances": []},
            {"allergies": [{"allergen_key": "宇宙射线", "severity": "severe"}], "avoidances": []},
            {"allergies": [], "avoidances": [{"ingredient_id": foreign["ingredient_id"]}]},
        ]
        for index, values in enumerate(invalid):
            with self.subTest(values=values), self.assertRaises((ValueError, PermissionError)):
                self.store.update_my_food_profile({
                    **values, "preferences": {}, "expected_version": 0,
                    "idempotency_key": f"profile-invalid-{index:04d}",
                }, DEMO_USER)
        self.assertEqual(next(item for item in self.store.get_food_profiles(DEMO_USER)["profiles"] if item["user_id"] == DEMO_USER)["version"], 0)

    def test_actual_recipe_ingredients_drive_allergy_evidence(self) -> None:
        self.store.update_my_food_profile({
            "allergies": [{"allergen_key": "大豆", "severity": "severe"}],
            "avoidances": [], "preferences": {}, "expected_version": 0,
            "idempotency_key": "profile-soy-safety-0001",
        }, DEMO_USER)
        blocked = self.store.evaluate_recipe_food_safety({
            "ingredients": [{"name": "豆腐"}, {"name": "生抽"}],
            "allergen_tags": ["乳制品"],
        }, DEMO_USER)
        safe = self.store.evaluate_recipe_food_safety({
            "ingredients": [{"name": "红豆"}, {"name": "绿豆"}, {"name": "豆角"}],
            "allergen_tags": ["大豆"],
        }, DEMO_USER)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual({item["allergen_key"] for item in blocked["severe_conflicts"]}, {"大豆"})
        self.assertEqual(safe["status"], "safe")
        self.assertEqual(safe["severe_conflicts"], [])

    def test_compound_food_with_severe_profile_requires_review(self) -> None:
        self.store.update_my_food_profile({
            "allergies": [{"allergen_key": "花生", "severity": "severe"}],
            "avoidances": [], "preferences": {}, "expected_version": 0,
            "idempotency_key": "profile-severe-review-0001",
        }, DEMO_USER)
        result = self.store.evaluate_recipe_food_safety({
            "ingredients": [{"name": "蚝油"}, {"name": "番茄"}],
        }, DEMO_USER)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["review_ingredients"], ["蚝油"])

    def test_pantry_is_unique_by_canonical_ingredient(self) -> None:
        first = self.pantry("番茄")
        second = self.store.upsert_pantry_item({
            "name": "西红柿", "state": "low", "expected_version": first["version"],
            "idempotency_key": "pantry-tomato-alias-0001",
        }, DEMO_PARTNER)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["canonical_name"], "番茄")
        self.assertEqual(len(self.store.list_pantry_items(DEMO_USER)["items"]), 1)

    def test_pantry_uses_have_low_out_and_rejects_quantity_ledger_fields(self) -> None:
        item = self.pantry("大米", quantity_note="半袋")
        self.assertEqual(item["state"], "have")
        self.assertEqual(item["quantity_note"], "半袋")
        for state in ("low", "out"):
            item = self.store.update_pantry_item(item["id"], {
                "state": state, "expected_version": item["version"],
                "idempotency_key": f"pantry-state-{state}-0001",
            }, DEMO_USER)
            self.assertEqual(item["state"], state)
        with self.assertRaises(ValueError):
            self.store.upsert_pantry_item({
                "name": "面粉", "state": "available", "expected_version": 0,
                "idempotency_key": "pantry-available-invalid",
            }, DEMO_USER)
        for index, field in enumerate(("quantity", "quantity_milli", "unit", "batch_id", "reserved")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.upsert_pantry_item({
                    "name": f"测试食材{index}", "state": "have", field: 1,
                    "expected_version": 0, "idempotency_key": f"pantry-ledger-{index:04d}",
                }, DEMO_USER)

        for index, field in enumerate(("amount", "count", "grams", "batches", "unit_family"), start=20):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.upsert_pantry_item({
                    "name": f"未支持字段食材{index}", "state": "have", field: 1,
                    "expected_version": 0, "idempotency_key": f"pantry-unknown-ledger-{index:04d}",
                }, DEMO_USER)

    def test_profile_nested_fields_are_allowlisted_and_noop_is_silent(self) -> None:
        payload = {
            "allergies": [{"allergen_key": "乳制品", "severity": "avoid"}],
            "avoidances": [], "preferences": {}, "expected_version": 0,
            "idempotency_key": "profile-noop-base-0001",
        }
        saved = self.store.update_my_food_profile(payload, DEMO_USER)
        with self.store.connection() as db:
            event_count = db.execute(
                "SELECT COUNT(*) FROM realtime_events WHERE event_type='kitchen.updated' AND resource_id=?",
                (DEMO_USER,),
            ).fetchone()[0]
        replay_payload = {**payload, "expected_version": saved["version"], "idempotency_key": "profile-noop-repeat-0001"}
        repeated = self.store.update_my_food_profile(replay_payload, DEMO_USER)
        self.assertEqual(repeated["version"], saved["version"])
        with self.store.connection() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM realtime_events WHERE event_type='kitchen.updated' AND resource_id=?",
                (DEMO_USER,),
            ).fetchone()[0], event_count)
        for invalid in (
            {**payload, "expected_version": saved["version"], "idempotency_key": "profile-extra-root-0001", "medical_note": "x"},
            {**payload, "expected_version": saved["version"], "idempotency_key": "profile-extra-allergy-0001", "allergies": [{"allergen_key": "乳制品", "severity": "avoid", "provider_label": "x"}]},
            {**payload, "expected_version": saved["version"], "idempotency_key": "profile-extra-avoid-0001", "avoidances": [{"ingredient_id": "x", "amount": 1}]},
        ):
            with self.assertRaises((ValueError, KeyError)):
                self.store.update_my_food_profile(invalid, DEMO_USER)

    def test_stale_partner_pantry_update_conflicts_without_overwrite(self) -> None:
        item = self.pantry("鸡蛋")
        changed = self.store.update_pantry_item(item["id"], {
            "state": "low", "expected_version": item["version"],
            "idempotency_key": "pantry-first-write-0001",
        }, DEMO_USER)
        with self.assertRaises(ConflictError):
            self.store.update_pantry_item(item["id"], {
                "state": "out", "expected_version": item["version"],
                "idempotency_key": "pantry-stale-write-0001",
            }, DEMO_PARTNER)
        current = self.store.list_pantry_items(DEMO_PARTNER)["items"][0]
        self.assertEqual((current["state"], current["version"]), ("low", changed["version"]))

    def test_pantry_same_payload_is_idempotent_and_state_change_returns_undo(self) -> None:
        payload = {
            "name": "牛奶", "state": "have", "storage_location": "chilled",
            "expected_version": 0, "idempotency_key": "pantry-create-milk-0001",
        }
        first = self.store.upsert_pantry_item(payload, DEMO_USER)
        self.assertEqual(first, self.store.upsert_pantry_item(payload, DEMO_USER))
        changed = self.store.update_pantry_item(first["id"], {
            "state": "low", "expected_version": first["version"],
            "idempotency_key": "pantry-milk-low-0001",
        }, DEMO_USER)
        self.assertIn("undo", changed)
        undone = self.store.undo_mutation(changed["undo"]["receipt_id"], DEMO_USER)
        self.assertTrue(undone["undone"])
        self.assertEqual(self.store.list_pantry_items(DEMO_USER)["items"][0]["state"], "have")

    def test_canonical_alias_noop_does_not_require_a_stale_display_version(self) -> None:
        first = self.pantry("番茄")
        alias = self.store.upsert_pantry_item({
            "name": "西红柿", "state": "have", "expected_version": 0,
            "idempotency_key": "pantry-tomato-alias-noop-0001",
        }, DEMO_PARTNER)
        self.assertEqual(alias["id"], first["id"])
        self.assertFalse(alias["changed"])


class KitchenShoppingTests(KitchenTestCase):
    def _shopping_item(self, name: str, category: str, *, checked: bool = True) -> dict:
        item = self.store.create_shopping_item({"name": name, "quantity": 1, "unit": "个", "category": category}, DEMO_USER)
        if checked:
            item = self.store.update_shopping_item(item["id"], {"checked": True, "expected_version": item["version"]}, DEMO_USER)
        return item

    def test_default_order_contains_exactly_nine_categories(self) -> None:
        result = self.store.get_shopping_category_order(DEMO_USER)
        self.assertEqual(
            [(item["key"], item["label"], item["position"], item["version"]) for item in result["categories"]],
            [
                ("produce", "蔬菜水果", 0, 0), ("meat_eggs", "肉禽蛋", 1, 0),
                ("seafood", "水产", 2, 0), ("dairy", "乳品", 3, 0),
                ("staples_oils", "粮油", 4, 0), ("seasonings", "调味", 5, 0),
                ("frozen", "冷冻", 6, 0), ("household", "日用品", 7, 0),
                ("other", "其他", 8, 0),
            ],
        )

    def test_category_classification_and_legacy_read_mapping(self) -> None:
        expected = {
            "鸡蛋": "meat_eggs", "牛奶": "dairy", "橄榄油": "staples_oils",
            "速冻饺子": "frozen", "洗洁精": "household",
            "小米椒": "produce", "米醋": "seasonings",
        }
        for name, category in expected.items():
            item = self.store.create_shopping_item({"name": name, "quantity": 1, "unit": "个"}, DEMO_USER)
            self.assertEqual(item["category_key"], category)
        with self.store.connection() as db:
            row = db.execute("SELECT id FROM shopping_list_items ORDER BY rowid LIMIT 1").fetchone()
            db.execute("UPDATE shopping_list_items SET category='临时分类' WHERE id=?", (row["id"],))
        legacy = next(item for item in self.store.get_shopping_list(DEMO_USER)["items"] if item["id"] == row["id"])
        self.assertEqual((legacy["category_key"], legacy["category_label"]), ("other", "其他"))

    def test_category_reorder_is_atomic_and_stale_versions_change_nothing(self) -> None:
        current = self.store.get_shopping_category_order(DEMO_USER)
        keys = [item["key"] for item in current["categories"]]
        versions = {item["key"]: item["version"] for item in current["categories"]}
        reordered = list(reversed(keys))
        saved = self.store.update_shopping_category_order({
            "categories": reordered, "expected_versions": versions,
            "idempotency_key": "category-reorder-0001",
        }, DEMO_USER)
        self.assertEqual([item["key"] for item in saved["categories"]], reordered)
        before = self.store.get_shopping_category_order(DEMO_USER)
        with self.assertRaises(ConflictError):
            self.store.update_shopping_category_order({
                "categories": keys,
                "expected_versions": {key: 0 for key in keys},
                "idempotency_key": "category-stale-0001",
            }, DEMO_PARTNER)
        self.assertEqual(self.store.get_shopping_category_order(DEMO_USER), before)
        for invalid in (keys[:-1], keys[:-1] + [keys[0]], keys[:-1] + ["unknown"]):
            with self.assertRaises(ValueError):
                self.store.update_shopping_category_order({
                    "categories": invalid, "expected_versions": versions,
                    "idempotency_key": f"category-invalid-{uuid.uuid4().hex}",
                }, DEMO_USER)

    def test_shopping_preview_uses_the_couple_category_order(self) -> None:
        current = self.store.get_shopping_category_order(DEMO_USER)
        reversed_keys = list(reversed([item["key"] for item in current["categories"]]))
        self.store.update_shopping_category_order({
            "categories": reversed_keys,
            "expected_versions": {item["key"]: item["version"] for item in current["categories"]},
            "idempotency_key": "category-preview-order-0001",
        }, DEMO_USER)
        recipe = self.store.list_recipes(DEMO_USER, query="番茄炒蛋", limit=5)["items"][0]
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        meal = self.store.add_meal_cart_item({
            "recipe_id": recipe["id"], "planned_date": tomorrow,
            "meal_slot": "dinner", "servings": 2,
            "idempotency_key": "category-preview-meal-0001",
        }, DEMO_USER)
        self.store.update_meal_cart_item(meal["id"], {
            "status": "accepted", "expected_version": meal["version"],
        }, DEMO_PARTNER)
        preview = self.store.preview_shopping_list({"range_start": tomorrow, "range_end": tomorrow}, DEMO_USER)["preview"]
        positions = {key: index for index, key in enumerate(reversed_keys)}
        category_positions = [positions[item["category"]] for item in preview["items"]]
        self.assertEqual(category_positions, sorted(category_positions))

    def test_checked_food_item_stocks_once_but_invalid_items_are_rejected(self) -> None:
        tomato = self._shopping_item("番茄", "produce")
        first = self.store.stock_shopping_item(tomato["id"], {
            "expected_version": tomato["version"], "idempotency_key": "stock-tomato-once-0001",
        }, DEMO_USER)
        replay = self.store.stock_shopping_item(tomato["id"], {
            "expected_version": tomato["version"], "idempotency_key": "stock-tomato-once-0001",
        }, DEMO_USER)
        self.assertEqual(first, replay)
        self.assertTrue(first["changed"])
        self.assertEqual(len(self.store.list_pantry_items(DEMO_PARTNER)["items"]), 1)

        unchecked = self._shopping_item("土豆", "produce", checked=False)
        household = self._shopping_item("洗洁精", "household")
        other = self._shopping_item("自定义可食用物", "other")
        for item in (unchecked, household, other):
            with self.subTest(item=item["display_name"]), self.assertRaises(ConflictError):
                self.store.stock_shopping_item(item["id"], {
                    "expected_version": item["version"],
                    "idempotency_key": f"stock-invalid-{item['id']}",
                }, DEMO_USER)
        confirmed = self.store.stock_shopping_item(other["id"], {
            "expected_version": other["version"], "confirm_food": True,
            "idempotency_key": "stock-other-confirmed-0001",
        }, DEMO_USER)
        self.assertTrue(confirmed["changed"])
    def test_idempotency_keys_are_bound_to_the_url_target(self) -> None:
        first = self.pantry("番茄")
        second = self.pantry("土豆")
        body = {"state": "low", "expected_version": 1, "idempotency_key": "pantry-target-bound-0001"}
        self.store.update_pantry_item(first["id"], body, DEMO_USER)
        with self.assertRaises(ConflictError):
            self.store.update_pantry_item(second["id"], body, DEMO_USER)
        self.assertEqual(next(item for item in self.store.list_pantry_items(DEMO_USER)["items"] if item["id"] == second["id"])["state"], "have")

        apple = self._shopping_item("苹果", "produce")
        pear = self._shopping_item("梨", "produce")
        stock_body = {"expected_version": apple["version"], "idempotency_key": "stock-target-bound-0001"}
        self.store.stock_shopping_item(apple["id"], stock_body, DEMO_USER)
        with self.assertRaises(ConflictError):
            self.store.stock_shopping_item(pear["id"], stock_body, DEMO_USER)
        names = {item["canonical_name"] for item in self.store.list_pantry_items(DEMO_USER)["items"]}
        self.assertIn("苹果", names)
        self.assertNotIn("梨", names)

    def test_stocking_rejects_every_unrecognized_field(self) -> None:
        item = self._shopping_item("苹果", "produce")
        for index, field in enumerate(("amount", "count", "grams", "batches", "unit_family")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.stock_shopping_item(item["id"], {
                    "expected_version": item["version"], field: 1,
                    "idempotency_key": f"stock-unknown-field-{index:04d}",
                }, DEMO_USER)

    def test_complete_and_selected_stocking_commit_or_rollback_together(self) -> None:
        tomato = self._shopping_item("番茄", "produce")
        household = self._shopping_item("厨房纸", "household")
        shopping = self.store.get_shopping_list(DEMO_USER)
        with self.assertRaises(ConflictError):
            self.store.complete_shopping_list({
                "expected_version": shopping["version"], "allow_incomplete": True,
                "stock_checked_item_ids": [tomato["id"], household["id"]],
                "stock_item_versions": {tomato["id"]: tomato["version"], household["id"]: household["version"]},
                "idempotency_key": "complete-stock-rollback-0001",
            }, DEMO_USER)
        self.assertEqual(self.store.get_shopping_list(DEMO_USER)["status"], "active")
        self.assertEqual(self.store.list_pantry_items(DEMO_USER)["items"], [])

        shopping = self.store.get_shopping_list(DEMO_USER)
        completed = self.store.complete_shopping_list({
            "expected_version": shopping["version"], "allow_incomplete": True,
            "stock_checked_item_ids": [tomato["id"]],
            "stock_item_versions": {tomato["id"]: tomato["version"]},
            "idempotency_key": "complete-stock-success-0001",
        }, DEMO_PARTNER)
        self.assertTrue(completed["completed"])
        self.assertEqual(completed["stocked_count"], 1)
        self.assertEqual(self.store.list_pantry_items(DEMO_PARTNER)["items"][0]["canonical_name"], "番茄")

    def test_completion_requires_list_version_and_key_cannot_move_to_a_new_list(self) -> None:
        first_item = self._shopping_item("番茄", "produce")
        first_list = self.store.get_shopping_list(DEMO_USER)
        with self.assertRaises(ValueError):
            self.store.complete_shopping_list({
                "allow_incomplete": True,
                "idempotency_key": "complete-version-required-0001",
            }, DEMO_USER)
        completed = self.store.complete_shopping_list({
            "expected_version": first_list["version"], "allow_incomplete": True,
            "idempotency_key": "complete-target-bound-0001",
        }, DEMO_USER)
        self.assertEqual(completed["id"], first_list["id"])
        self._shopping_item("土豆", "produce")
        second_list = self.store.get_shopping_list(DEMO_USER)
        self.assertNotEqual(second_list["id"], first_list["id"])
        replay = self.store.complete_shopping_list({
            "expected_version": second_list["version"], "allow_incomplete": True,
            "idempotency_key": "complete-target-bound-0001",
        }, DEMO_USER)
        self.assertEqual(replay, completed)
        self.assertEqual(self.store.get_shopping_list(DEMO_USER)["status"], "active")

    def test_atomic_completion_only_accepts_current_list_items_and_exact_food_confirmations(self) -> None:
        archived = self._shopping_item("旧清单番茄", "produce")
        old_list = self.store.get_shopping_list(DEMO_USER)
        self.store.complete_shopping_list({
            "expected_version": old_list["version"],
            "idempotency_key": "complete-old-list-without-stock-0001",
        }, DEMO_USER)

        current_item = self._shopping_item("新清单土豆", "produce")
        current = self.store.get_shopping_list(DEMO_USER)
        with self.assertRaises((KeyError, ConflictError, ValueError)):
            self.store.complete_shopping_list({
                "expected_version": current["version"],
                "stock_checked_item_ids": [archived["id"]],
                "stock_item_versions": {archived["id"]: archived["version"]},
                "idempotency_key": "complete-cannot-stock-old-list-0001",
            }, DEMO_USER)
        self.assertEqual(self.store.get_shopping_list(DEMO_USER)["status"], "active")
        self.assertEqual(self.store.list_pantry_items(DEMO_USER)["items"], [])

        with self.assertRaises(ValueError):
            self.store.complete_shopping_list({
                "expected_version": current["version"],
                "stock_checked_item_ids": [current_item["id"]],
                "stock_item_versions": {current_item["id"]: current_item["version"]},
                "confirm_food_item_ids": ["not-selected"],
                "idempotency_key": "complete-confirmation-must-match-selection-0001",
            }, DEMO_USER)
        self.assertEqual(self.store.get_shopping_list(DEMO_USER)["status"], "active")
        self.assertEqual(self.store.list_pantry_items(DEMO_USER)["items"], [])

    def test_two_accounts_stocking_same_item_never_duplicates(self) -> None:
        tomato = self._shopping_item("番茄", "produce")
        barrier = threading.Barrier(2)
        outcomes: list[dict | Exception] = []

        def worker(user_id: str, key: str) -> None:
            try:
                barrier.wait(timeout=2)
                outcomes.append(self.store.stock_shopping_item(tomato["id"], {
                    "expected_version": tomato["version"], "idempotency_key": key,
                }, user_id))
            except Exception as error:  # assertion below names allowed outcomes
                outcomes.append(error)

        threads = [
            threading.Thread(target=worker, args=(DEMO_USER, "concurrent-stock-user-0001")),
            threading.Thread(target=worker, args=(DEMO_PARTNER, "concurrent-stock-partner-0001")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(all(isinstance(item, dict) or isinstance(item, ConflictError) for item in outcomes), outcomes)
        self.assertEqual(len(self.store.list_pantry_items(DEMO_USER)["items"]), 1)
        changed = [item["changed"] for item in outcomes if isinstance(item, dict)]
        self.assertEqual(changed.count(True), 1)


class WeeklyMealPlanDraftTests(KitchenTestCase):
    def _request(self, *, key: str = "weekly-create-safe-0001") -> dict:
        start = date.today() + timedelta(days=1)
        return {
            "schema_version": 1,
            "starts_on": start.isoformat(),
            "ends_on": (start + timedelta(days=1)).isoformat(),
            "meal_slots": [
                {"meal_slot": "breakfast", "planned_time": "08:00", "max_active_minutes": 60},
                {"meal_slot": "dinner", "planned_time": "19:00", "max_active_minutes": 120},
            ],
            "servings": 2,
            "availability_mode": "either_partner",
            "idempotency_key": key,
        }

    def test_empty_pantry_still_builds_complete_safe_rules_draft(self) -> None:
        created = self.store.create_weekly_meal_plan_draft(self._request(), DEMO_USER)
        self.assertEqual(created["source_type"], "rules")
        self.assertEqual(len(created["items"]), 4)
        self.assertEqual(created["items"], self.store.get_active_weekly_meal_plan_draft(DEMO_PARTNER)["items"])
        self.assertNotIn("expected_versions", created)
        self.assertNotIn("payload_json", created)

    def test_reversed_slot_request_is_canonical_through_preview_and_confirmation(self) -> None:
        payload = self._request(key="weekly-reversed-slots-0001")
        payload["ends_on"] = payload["starts_on"]
        payload["meal_slots"] = [payload["meal_slots"][1], payload["meal_slots"][0]]
        draft = self.store.create_weekly_meal_plan_draft(payload, DEMO_USER)
        self.assertEqual(
            [(item["meal_slot"], item["planned_time"]) for item in draft["items"]],
            [("breakfast", "08:00"), ("dinner", "19:00")],
        )
        first = self.store.confirm_weekly_meal_plan_draft(draft["id"], {
            "expected_version": draft["version"], "idempotency_key": "weekly-reversed-confirm-a-0001",
        }, DEMO_USER)
        self.assertEqual([item["meal_slot"] for item in first["items"]], ["breakfast", "dinner"])
        applied = self.store.confirm_weekly_meal_plan_draft(draft["id"], {
            "expected_version": first["version"], "idempotency_key": "weekly-reversed-confirm-b-0001",
        }, DEMO_PARTNER)
        self.assertEqual([item["meal_slot"] for item in applied["items"]], ["breakfast", "dinner"])

    def test_only_one_active_draft_and_creation_is_target_bound_idempotent(self) -> None:
        payload = self._request(key="weekly-create-idempotent-0001")
        first = self.store.create_weekly_meal_plan_draft(payload, DEMO_USER)
        self.assertEqual(first, self.store.create_weekly_meal_plan_draft(payload, DEMO_USER))
        with self.assertRaises(ConflictError):
            self.store.create_weekly_meal_plan_draft({**payload, "ends_on": payload["starts_on"]}, DEMO_USER)
        with self.assertRaises(ConflictError):
            self.store.create_weekly_meal_plan_draft(self._request(key="weekly-create-second-0001"), DEMO_PARTNER)

    def test_first_confirmation_has_no_downstream_domain_side_effects(self) -> None:
        draft = self.store.create_weekly_meal_plan_draft(self._request(), DEMO_USER)
        self.assertTrue(all(item["status"] == "planned" for item in draft["items"]))
        with self.store.connection() as db:
            before = {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("meal_cart_items", "shopping_lists", "shopping_list_items", "in_app_notifications", "audit_events", "realtime_events")
            }
        confirmed = self.store.confirm_weekly_meal_plan_draft(draft["id"], {
            "expected_version": draft["version"], "idempotency_key": "weekly-confirm-first-0001",
        }, DEMO_USER)
        self.assertEqual(confirmed["status"], "pending_confirmation")
        self.assertEqual(confirmed["version"], draft["version"])
        with self.store.connection() as db:
            after = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
        self.assertEqual(after, before)

    def test_second_confirmation_atomically_applies_meals_and_shopping(self) -> None:
        draft = self.store.create_weekly_meal_plan_draft(self._request(), DEMO_USER)
        first = self.store.confirm_weekly_meal_plan_draft(draft["id"], {
            "expected_version": draft["version"], "idempotency_key": "weekly-confirm-a-0001",
        }, DEMO_USER)
        applied = self.store.confirm_weekly_meal_plan_draft(draft["id"], {
            "expected_version": first["version"], "idempotency_key": "weekly-confirm-b-0001",
        }, DEMO_PARTNER)
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(len(applied["result"]["meal_item_ids"]), len(draft["items"]))
        self.assertNotIn("recipe", json.dumps(applied["result"], ensure_ascii=False))
        calendar = self.store.meal_calendar(draft["starts_on"], draft["ends_on"], DEMO_PARTNER)
        self.assertEqual(sum(len(rows) for day in calendar["days"] for rows in day["meals"].values()), len(draft["items"]))
        self.assertIsNotNone(applied["result"]["shopping_list_id"])

    def test_swap_increments_version_and_clears_confirmations(self) -> None:
        with self.store.connection() as db:
            couple, _ = self.store._couple_context(db, DEMO_USER)
            common = sorted(
                recipe["id"] for recipe in self.store._weekly_recipe_catalog(db, couple["id"]).values()
                if {"lunch", "dinner"}.issubset(set(recipe.get("meal_slots", [])))
            )[:2]
        self.assertEqual(len(common), 2)
        for index, recipe_id in enumerate(common):
            self.store.create_meal_wish({"recipe_id": recipe_id, "title": f"交换菜{index}"}, DEMO_USER)
        request = self._request()
        request["meal_slots"] = [
            {"meal_slot": "lunch", "planned_time": "12:00", "max_active_minutes": 120},
            {"meal_slot": "dinner", "planned_time": "19:00", "max_active_minutes": 120},
        ]
        draft = self.store.create_weekly_meal_plan_draft(request, DEMO_USER)
        pending = self.store.confirm_weekly_meal_plan_draft(draft["id"], {
            "expected_version": draft["version"], "idempotency_key": "weekly-confirm-before-edit",
        }, DEMO_USER)
        first, second = pending["items"][0], pending["items"][1]
        updated = self.store.update_weekly_meal_plan_item(draft["id"], first["id"], {
            "date": second["date"], "meal_slot": second["meal_slot"],
            "swap_with_item_id": second["id"], "expected_version": pending["version"],
            "idempotency_key": "weekly-swap-items-0001",
        }, DEMO_USER)
        self.assertEqual(updated["status"], "draft")
        self.assertEqual(updated["version"], pending["version"] + 1)
        self.assertFalse(updated["confirmations"]["member_a_confirmed"])
        self.assertFalse(updated["confirmations"]["member_b_confirmed"])
        by_id = {item["id"]: item for item in updated["items"]}
        self.assertEqual(by_id[first["id"]]["planned_time"], "19:00")
        self.assertEqual(by_id[second["id"]]["planned_time"], "12:00")

    def test_cancel_is_cas_idempotent_and_has_no_fake_undo(self) -> None:
        draft = self.store.create_weekly_meal_plan_draft(self._request(), DEMO_USER)
        payload = {"expected_version": draft["version"], "idempotency_key": "weekly-cancel-draft-0001"}
        cancelled = self.store.cancel_weekly_meal_plan_draft(draft["id"], payload, DEMO_USER)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled, self.store.cancel_weekly_meal_plan_draft(draft["id"], payload, DEMO_USER))
        self.assertNotIn("undo", cancelled)

    def test_reused_confirmation_key_with_different_payload_is_rejected(self) -> None:
        draft = self.store.create_weekly_meal_plan_draft(self._request(), DEMO_USER)
        self.store.confirm_weekly_meal_plan_draft(draft["id"], {
            "expected_version": draft["version"], "idempotency_key": "weekly-confirm-reused-0001",
        }, DEMO_USER)
        with self.assertRaises(ConflictError):
            self.store.confirm_weekly_meal_plan_draft(draft["id"], {
                "expected_version": draft["version"] + 1, "idempotency_key": "weekly-confirm-reused-0001",
            }, DEMO_USER)

    def test_failure_during_shopping_merge_rolls_back_second_confirmation_and_meals(self) -> None:
        draft = self.store.create_weekly_meal_plan_draft(self._request(), DEMO_USER)
        first = self.store.confirm_weekly_meal_plan_draft(draft["id"], {
            "expected_version": draft["version"], "idempotency_key": "weekly-rollback-first-0001",
        }, DEMO_USER)
        with self.store.connection() as db:
            before = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in (
                "meal_cart_items", "shopping_lists", "shopping_list_items", "in_app_notifications", "audit_events", "realtime_events", "mutation_idempotency",
            )}
        with patch.object(self.store, "_apply_shopping_items_in_connection", side_effect=RuntimeError("fault injection")):
            with self.assertRaises(RuntimeError):
                self.store.confirm_weekly_meal_plan_draft(draft["id"], {
                    "expected_version": first["version"], "idempotency_key": "weekly-rollback-second-0001",
                }, DEMO_PARTNER)
        with self.store.connection() as db:
            after = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
            row = db.execute("SELECT * FROM weekly_meal_plan_drafts WHERE id=?", (draft["id"],)).fetchone()
        self.assertEqual(after, before)
        self.assertEqual(row["status"], "pending_confirmation")
        self.assertIsNotNone(row["confirmed_a_id"])
        self.assertIsNone(row["confirmed_b_id"])

    def test_failure_after_notifications_rolls_back_entire_second_confirmation(self) -> None:
        draft = self.store.create_weekly_meal_plan_draft(self._request(), DEMO_USER)
        first = self.store.confirm_weekly_meal_plan_draft(draft["id"], {
            "expected_version": draft["version"], "idempotency_key": "weekly-event-rollback-first-0001",
        }, DEMO_USER)
        with self.store.connection() as db:
            before = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in (
                "meal_cart_items", "shopping_lists", "shopping_list_items", "in_app_notifications",
                "audit_events", "realtime_events", "mutation_idempotency",
            )}
        with patch.object(self.store, "_event", side_effect=RuntimeError("event fault injection")):
            with self.assertRaises(RuntimeError):
                self.store.confirm_weekly_meal_plan_draft(draft["id"], {
                    "expected_version": first["version"], "idempotency_key": "weekly-event-rollback-second-0001",
                }, DEMO_PARTNER)
        with self.store.connection() as db:
            after = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
            row = db.execute("SELECT status,confirmed_a_id,confirmed_b_id FROM weekly_meal_plan_drafts WHERE id=?", (draft["id"],)).fetchone()
        self.assertEqual(after, before)
        self.assertEqual(row["status"], "pending_confirmation")
        self.assertIsNotNone(row["confirmed_a_id"])
        self.assertIsNone(row["confirmed_b_id"])

    def test_food_profile_change_rejects_confirmation_without_partial_state(self) -> None:
        draft = self.store.create_weekly_meal_plan_draft(self._request(), DEMO_USER)
        self.store.update_my_food_profile({
            "allergies": [], "avoidances": [], "preferences": {"liked_tags": ["清淡"], "disliked_tags": []},
            "expected_version": 0, "idempotency_key": "weekly-stale-profile-change-0001",
        }, DEMO_PARTNER)
        with self.assertRaises(ConflictError):
            self.store.confirm_weekly_meal_plan_draft(draft["id"], {
                "expected_version": draft["version"], "idempotency_key": "weekly-stale-profile-confirm-0001",
            }, DEMO_USER)
        current = self.store.get_active_weekly_meal_plan_draft(DEMO_USER)
        self.assertEqual(current["status"], "draft")
        self.assertFalse(current["confirmations"]["member_a_confirmed"])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM meal_cart_items").fetchone()[0], 0)

    def test_expired_pantry_is_not_promoted_or_exposed_as_likely_on_hand(self) -> None:
        pantry = self.pantry("鸡蛋", expires_on=(date.today() - timedelta(days=1)).isoformat())
        self.assertEqual(pantry["state"], "have")
        draft = self.store.create_weekly_meal_plan_draft(self._request(), DEMO_USER)
        for item in draft["items"]:
            self.assertNotIn("鸡蛋", item["likely_on_hand"])
            self.assertNotIn("pantry_match", item["reason_codes"])
            self.assertNotIn("expires_soon", item["reason_codes"])

    def test_busy_window_uses_maximum_slot_window_and_half_open_gaps(self) -> None:
        day = (date.today() + timedelta(days=1)).isoformat()
        self.store.create_calendar_entry({
            "entry_type": "event", "title": "临近开饭的占用",
            "starts_at": f"{day}T18:50:00+08:00", "ends_at": f"{day}T19:00:00+08:00",
            "all_day": False, "participant_ids": [DEMO_USER, DEMO_PARTNER], "blocks_time": True,
        }, DEMO_USER)
        with self.store.connection() as db:
            couple, _ = self.store._couple_context(db, DEMO_USER)
            available = self.store._weekly_available(
                db, couple=couple, day=day, planned_time="19:00", active_minutes=20,
                max_active_minutes=60, availability_mode="both_partners",
            )
        self.assertTrue(available, "18:00—18:50 仍有足够的共同连续动手时间")

    def test_first_confirmation_rechecks_food_safety(self) -> None:
        draft = self.store.create_weekly_meal_plan_draft(self._request(), DEMO_USER)
        with patch.object(self.store, "_evaluate_recipe_food_safety_in_connection", return_value={"status": "blocked"}):
            with self.assertRaises(ConflictError):
                self.store.confirm_weekly_meal_plan_draft(draft["id"], {
                    "expected_version": draft["version"], "idempotency_key": "weekly-first-safety-recheck-0001",
                }, DEMO_USER)
        current = self.store.get_active_weekly_meal_plan_draft(DEMO_USER)
        self.assertFalse(current["confirmations"]["member_a_confirmed"])

    def test_manual_replacement_cannot_break_weekly_dish_diversity(self) -> None:
        payload = self._request(key="weekly-diversity-create-0001")
        start = date.fromisoformat(payload["starts_on"])
        payload["ends_on"] = (start + timedelta(days=2)).isoformat()
        payload["meal_slots"] = [payload["meal_slots"][1]]
        draft = self.store.create_weekly_meal_plan_draft(payload, DEMO_USER)
        self.assertEqual(len(draft["items"]), 3)
        with self.assertRaises(ConflictError):
            self.store.update_weekly_meal_plan_item(draft["id"], draft["items"][2]["id"], {
                "recipe_id": draft["items"][0]["recipe_id"],
                "expected_version": draft["version"],
                "idempotency_key": "weekly-diversity-replace-0001",
            }, DEMO_USER)
        current = self.store.get_active_weekly_meal_plan_draft(DEMO_USER)
        self.assertEqual(current["version"], draft["version"])

    def test_recurring_blocker_added_after_draft_invalidates_confirmation_snapshot(self) -> None:
        draft = self.store.create_weekly_meal_plan_draft(self._request(), DEMO_USER)
        target = date.fromisoformat(draft["starts_on"])
        anchor = target - timedelta(days=5)
        self.store.create_calendar_entry({
            "entry_type": "event", "title": "每天固定安排",
            "starts_at": f"{anchor.isoformat()}T18:10:00+08:00",
            "ends_at": f"{anchor.isoformat()}T18:40:00+08:00", "all_day": False,
            "recurrence": "daily", "participant_ids": [DEMO_USER], "blocks_time": True,
        }, DEMO_USER)
        with self.assertRaises(ConflictError):
            self.store.confirm_weekly_meal_plan_draft(draft["id"], {
                "expected_version": draft["version"], "idempotency_key": "weekly-recurring-snapshot-0001",
            }, DEMO_USER)

    def test_all_day_is_end_exclusive_and_daily_weekly_occurrences_block_target_day(self) -> None:
        target = date.today() + timedelta(days=7)
        next_day = target + timedelta(days=1)
        self.store.create_calendar_entry({
            "entry_type": "event", "title": "单日全天",
            "starts_at": target.isoformat(), "ends_at": next_day.isoformat(), "all_day": True,
            "participant_ids": [DEMO_USER, DEMO_PARTNER], "blocks_time": True,
        }, DEMO_USER)
        daily_anchor = target - timedelta(days=5)
        self.store.create_calendar_entry({
            "entry_type": "event", "title": "每日占用",
            "starts_at": f"{daily_anchor.isoformat()}T18:20:00+08:00",
            "ends_at": f"{daily_anchor.isoformat()}T18:50:00+08:00", "all_day": False,
            "recurrence": "daily", "participant_ids": [DEMO_USER, DEMO_PARTNER], "blocks_time": True,
        }, DEMO_USER)
        weekly_anchor = target - timedelta(days=7)
        self.store.create_calendar_entry({
            "entry_type": "event", "title": "每周占用",
            "starts_at": f"{weekly_anchor.isoformat()}T17:40:00+08:00",
            "ends_at": f"{weekly_anchor.isoformat()}T18:10:00+08:00", "all_day": False,
            "recurrence": "weekly", "participant_ids": [DEMO_USER, DEMO_PARTNER], "blocks_time": True,
        }, DEMO_USER)
        with self.store.connection() as db:
            couple, _ = self.store._couple_context(db, DEMO_USER)
            blocked_target = self.store._weekly_available(
                db, couple=couple, day=target.isoformat(), planned_time="19:00", active_minutes=40,
                max_active_minutes=90, availability_mode="both_partners",
            )
            # The one-day all-day row must not spill into the next date.  The
            # recurring timed rows still leave a 40-minute gap on that date.
            available_next = self.store._weekly_available(
                db, couple=couple, day=next_day.isoformat(), planned_time="17:00", active_minutes=40,
                max_active_minutes=90, availability_mode="both_partners",
            )
        self.assertFalse(blocked_target)
        self.assertTrue(available_next)

    def test_confirmation_revalidates_stored_slot_compatibility(self) -> None:
        draft = self.store.create_weekly_meal_plan_draft(self._request(), DEMO_USER)
        with self.store.connection() as db:
            row = db.execute("SELECT payload_json FROM weekly_meal_plan_drafts WHERE id=?", (draft["id"],)).fetchone()
            stored = json.loads(row["payload_json"])
            stored["meals"][0]["meal_slot"] = "snack"
            db.execute("UPDATE weekly_meal_plan_drafts SET payload_json=? WHERE id=?", (json.dumps(stored, ensure_ascii=False), draft["id"]))
        with self.assertRaises(ConflictError):
            self.store.confirm_weekly_meal_plan_draft(draft["id"], {
                "expected_version": draft["version"], "idempotency_key": "weekly-confirm-slot-recheck-0001",
            }, DEMO_USER)

    def test_adjacent_meals_prefer_a_different_main_and_method_deterministically(self) -> None:
        with self.store.connection() as db:
            couple, _ = self.store._couple_context(db, DEMO_USER)
            originals = list(self.store._weekly_recipe_catalog(db, couple["id"]).values())[:3]
        recipes = {}
        specs = [
            ("recipe-a", "dish-a", "鸡蛋", "煎"),
            ("recipe-b", "dish-b", "鸡蛋", "蒸"),
            ("recipe-c", "dish-c", "牛肉", "炖"),
        ]
        for original, (recipe_id, dish_id, main, method) in zip(originals, specs):
            value = json.loads(json.dumps(original, ensure_ascii=False))
            value.update({"id": recipe_id, "dish_id": dish_id, "name": recipe_id, "version": 1,
                          "meal_slots": ["dinner"], "cooking_method": method, "variant_name": method,
                          "active_minutes": 10})
            value["ingredients"][0].update({"name": main, "canonical_name": main, "ingredient_group": "main"})
            recipes[recipe_id] = value
        payload = self._request(key="weekly-adjacent-diversity-0001")
        payload["meal_slots"] = [payload["meal_slots"][1]]
        with patch.object(self.store, "_weekly_recipe_catalog", return_value=recipes), patch.object(
            self.store, "_evaluate_recipe_food_safety_in_connection", return_value={"status": "safe"},
        ):
            draft = self.store.create_weekly_meal_plan_draft(payload, DEMO_USER)
        self.assertEqual([item["recipe_id"] for item in draft["items"]], ["recipe-a", "recipe-c"])


class WeeklyShoppingApplyHelperTests(KitchenTestCase):
    def _accepted_meal(self) -> dict:
        recipe = self.store.list_recipes(DEMO_USER, query="番茄炒蛋", limit=5)["items"][0]
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        meal = self.store.add_meal_cart_item({
            "recipe_id": recipe["id"], "planned_date": tomorrow, "meal_slot": "dinner", "servings": 2,
            "idempotency_key": f"weekly-helper-meal-{uuid.uuid4().hex}",
        }, DEMO_USER)
        return self.store.update_meal_cart_item(meal["id"], {"status": "accepted", "expected_version": meal["version"]}, DEMO_PARTNER)

    def test_rebuild_uses_full_range_and_never_doubles_quantities(self) -> None:
        meal = self._accepted_meal()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            first = self.store._apply_shopping_items_in_connection(
                db, couple_id=meal["couple_id"], actor_id=DEMO_USER, meal_items=[meal],
                range_start=meal["planned_date"], range_end=meal["planned_date"], expected_list_version=0,
            )
        before = {item["canonical_name"]: item["quantity_milli"] for item in first["shopping_list"]["items"]}
        current = self.store.get_meal_cart(DEMO_USER)
        current_meal = next(item for item in current["items"] if item["id"] == meal["id"])
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            second = self.store._apply_shopping_items_in_connection(
                db, couple_id=meal["couple_id"], actor_id=DEMO_USER, meal_items=[current_meal],
                range_start=meal["planned_date"], range_end=meal["planned_date"],
                expected_list_version=first["shopping_list"]["version"],
            )
        after = {item["canonical_name"]: item["quantity_milli"] for item in second["shopping_list"]["items"]}
        self.assertEqual(after, before)

    def test_manual_item_survives_auto_rebuild(self) -> None:
        meal = self._accepted_meal()
        manual = self.store.create_shopping_item({"name": "厨房纸", "quantity": 1, "unit": "包", "category": "household"}, DEMO_USER)
        shopping = self.store.get_shopping_list(DEMO_USER)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            result = self.store._apply_shopping_items_in_connection(
                db, couple_id=meal["couple_id"], actor_id=DEMO_USER, meal_items=[meal],
                range_start=meal["planned_date"], range_end=meal["planned_date"], expected_list_version=shopping["version"],
            )
        by_id = {item["id"]: item for item in result["shopping_list"]["items"]}
        self.assertIn(manual["id"], by_id)
        self.assertTrue(by_id[manual["id"]]["manual_locked"])

    def test_changed_quantity_resets_checked_but_unchanged_preserves_it(self) -> None:
        meal = self._accepted_meal()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            first = self.store._apply_shopping_items_in_connection(
                db, couple_id=meal["couple_id"], actor_id=DEMO_USER, meal_items=[meal],
                range_start=meal["planned_date"], range_end=meal["planned_date"], expected_list_version=0,
            )
        auto = next(item for item in first["shopping_list"]["items"] if not item["manually_added"])
        with self.store.connection() as db:
            db.execute("UPDATE shopping_list_items SET checked=1 WHERE id=?", (auto["id"],))
        with self.store.connection() as db:
            unchanged_meal = self.store._meal_item_dict(db.execute("SELECT * FROM meal_cart_items WHERE id=?", (meal["id"],)).fetchone())
            db.execute("BEGIN IMMEDIATE")
            unchanged = self.store._apply_shopping_items_in_connection(
                db, couple_id=meal["couple_id"], actor_id=DEMO_USER, meal_items=[unchanged_meal],
                range_start=meal["planned_date"], range_end=meal["planned_date"],
                expected_list_version=first["shopping_list"]["version"], mark_accepted_as_shopping=False,
            )
        unchanged_item = next(item for item in unchanged["shopping_list"]["items"] if item["id"] == auto["id"])
        self.assertTrue(unchanged_item["checked"])

        with self.store.connection() as db:
            db.execute("UPDATE meal_cart_items SET servings=4 WHERE id=?", (meal["id"],))
        with self.store.connection() as db:
            changed_meal = self.store._meal_item_dict(db.execute("SELECT * FROM meal_cart_items WHERE id=?", (meal["id"],)).fetchone())
            db.execute("BEGIN IMMEDIATE")
            changed = self.store._apply_shopping_items_in_connection(
                db, couple_id=meal["couple_id"], actor_id=DEMO_USER, meal_items=[changed_meal],
                range_start=meal["planned_date"], range_end=meal["planned_date"],
                expected_list_version=unchanged["shopping_list"]["version"], mark_accepted_as_shopping=False,
            )
        changed_item = next(item for item in changed["shopping_list"]["items"] if item["id"] == auto["id"])
        self.assertFalse(changed_item["checked"])

    def test_helper_never_commits_caller_transaction(self) -> None:
        meal = self._accepted_meal()
        with self.assertRaises(RuntimeError):
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                self.store._apply_shopping_items_in_connection(
                    db, couple_id=meal["couple_id"], actor_id=DEMO_USER, meal_items=[meal],
                    range_start=meal["planned_date"], range_end=meal["planned_date"], expected_list_version=0,
                )
                raise RuntimeError("caller rollback")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM shopping_lists").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM shopping_list_items").fetchone()[0], 0)

    def test_later_preview_rebuild_keeps_sources_from_existing_active_range(self) -> None:
        old_meal = self._accepted_meal()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            initial = self.store._apply_shopping_items_in_connection(
                db, couple_id=old_meal["couple_id"], actor_id=DEMO_USER, meal_items=[old_meal],
                range_start=old_meal["planned_date"], range_end=old_meal["planned_date"], expected_list_version=0,
            )
        new_date = (date.fromisoformat(old_meal["planned_date"]) + timedelta(days=3)).isoformat()
        recipe_id = old_meal["recipe_id"]
        new_meal = self.store.add_meal_cart_item({
            "recipe_id": recipe_id, "planned_date": new_date, "meal_slot": "dinner", "servings": 2,
            "idempotency_key": "weekly-range-new-meal-0001",
        }, DEMO_USER)
        self.store.update_meal_cart_item(new_meal["id"], {
            "status": "accepted", "expected_version": new_meal["version"],
        }, DEMO_PARTNER)
        preview = self.store.preview_shopping_list({"range_start": new_date, "range_end": new_date}, DEMO_USER)
        applied = self.store.apply_shopping_preview({
            "preview_id": preview["preview_id"], "idempotency_key": "weekly-range-apply-0001",
        }, DEMO_USER)
        self.assertEqual(applied["range_start"], old_meal["planned_date"])
        self.assertEqual(applied["range_end"], new_date)
        source_meals = {
            source["meal_item_id"] for item in applied["items"] for source in item.get("sources", [])
        }
        self.assertIn(old_meal["id"], source_meals)
        self.assertIn(new_meal["id"], source_meals)


class IngredientSubstitutionTests(KitchenTestCase):
    def _meal(
        self,
        ingredient_name: str = "牛奶",
        *,
        quantity_milli: int | None = 250_000,
        canonical_unit: str = "ml",
        unit_family: str = "volume",
        cooking_method: str = "汤羹",
        duplicate_source: bool = False,
    ) -> dict:
        ingredients = [{
            "id": "snapshot-ingredient-1", "name": ingredient_name,
            "canonical_name": ingredient_name, "quantity_milli": quantity_milli,
            "quantity_text": "适量" if quantity_milli is None else "",
            "canonical_unit": canonical_unit, "unit_family": unit_family,
            "specification": "", "preparation": "直接使用",
            "ingredient_group": "main", "optional": 0, "position": 1,
        }]
        if duplicate_source:
            ingredients.append({**ingredients[0], "id": "snapshot-ingredient-2", "position": 2})
        snapshot = {
            "id": "snapshot-recipe-substitution", "name": "替代测试菜",
            "cooking_method": cooking_method, "method_tags": [cooking_method],
            "category": "家常菜", "meal_slots": ["dinner"], "base_servings": 2,
            "ingredients": ingredients, "steps": [], "tips": [],
            "allergen_tags": ["供应商假标签"], "source_type": "real_api",
        }
        meal_id = f"meal-substitution-{uuid.uuid4().hex}"
        planned_date = (date.today() + timedelta(days=1)).isoformat()
        with self.store.connection() as db:
            couple, _ = self.store._couple_context(db, DEMO_USER)
            cart = self.store._active_cart(db, couple["id"], create=True)
            db.execute(
                """
                INSERT INTO meal_cart_items(
                    id,cart_id,couple_id,recipe_id,recipe_snapshot_json,planned_date,meal_slot,
                    planned_time,servings,ordered_by,status,course_type,note,created_at,updated_at
                ) VALUES (?,?,?,NULL,?,?, 'dinner','19:00',2,?,'accepted','main','','now','now')
                """,
                (meal_id, cart["id"], couple["id"], json.dumps(snapshot, ensure_ascii=False), planned_date, DEMO_USER),
            )
            return self.store._meal_item_dict(db.execute(
                "SELECT * FROM meal_cart_items WHERE id=?", (meal_id,),
            ).fetchone())

    @staticmethod
    def _preview_payload(meal: dict, key: str) -> dict:
        return {
            "snapshot_ingredient_ref": "snapshot-ingredient-1",
            "expected_meal_version": meal["version"],
            "expected_shopping_list_version": 0,
            "idempotency_key": key,
        }

    def test_preview_returns_ratio_methods_flavor_texture_added_allergens_and_rules_source(self) -> None:
        meal = self._meal()
        result = self.store.preview_ingredient_substitutions(
            meal["id"], self._preview_payload(meal, "substitution-preview-fields-0001"), DEMO_USER,
        )
        self.assertEqual(result["meal_item_id"], meal["id"])
        self.assertEqual(result["source_ingredient"]["snapshot_ref"], "snapshot-ingredient-1")
        self.assertGreaterEqual(len(result["items"]), 2)
        soy = next(item for item in result["items"] if item["to_ingredient"]["name"] == "无糖豆奶")
        for field in (
            "ratio_note", "suitable_methods", "flavor_change", "texture_change",
            "effect_note", "added_allergens", "safety_status", "safety_reason_codes",
            "safety_message", "can_apply", "source_type", "source_label", "version",
        ):
            self.assertIn(field, soy)
        self.assertEqual(soy["source_type"], "rules")
        self.assertEqual(soy["source_label"], "本地校准规则")
        self.assertEqual([item["name"] for item in soy["added_allergens"]], ["大豆"])
        self.assertTrue(soy["can_apply"])
        self.assertNotIn("绝对安全", soy["safety_message"])
        self.assertNotIn("无过敏原", soy["safety_message"])

    def test_no_rule_returns_empty_without_guessing_or_default_soy(self) -> None:
        meal = self._meal("火星香草")
        result = self.store.preview_ingredient_substitutions(
            meal["id"], self._preview_payload(meal, "substitution-preview-empty-0001"), DEMO_USER,
        )
        self.assertEqual(result["items"], [])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("豆奶", serialized)
        self.assertIn("暂无经过核对的替代规则", result["message"])

    def test_severe_conflict_is_visible_but_direct_apply_is_rejected(self) -> None:
        meal = self._meal()
        self.store.update_my_food_profile({
            "allergies": [{"allergen_key": "大豆", "severity": "severe"}],
            "avoidances": [], "preferences": {}, "expected_version": 0,
            "idempotency_key": "substitution-profile-severe-0001",
        }, DEMO_PARTNER)
        preview = self.store.preview_ingredient_substitutions(
            meal["id"], self._preview_payload(meal, "substitution-preview-blocked-0001"), DEMO_USER,
        )
        soy = next(item for item in preview["items"] if item["to_ingredient"]["name"] == "无糖豆奶")
        self.assertEqual(soy["safety_status"], "blocked")
        self.assertFalse(soy["can_apply"])
        self.assertNotIn("user_id", json.dumps(soy, ensure_ascii=False))
        with self.assertRaises(ConflictError):
            self.store.apply_ingredient_substitution(meal["id"], soy["id"], {
                "expected_meal_version": meal["version"],
                "expected_shopping_list_version": 0,
                "expected_version": soy["version"],
                "idempotency_key": "substitution-apply-blocked-0001",
            }, DEMO_USER)

    def test_compound_package_unknown_and_vague_quantity_are_view_only(self) -> None:
        package_meal = self._meal("生抽", cooking_method="炒")
        package = self.store.preview_ingredient_substitutions(
            package_meal["id"], self._preview_payload(package_meal, "substitution-preview-package-0001"), DEMO_USER,
        )["items"][0]
        self.assertEqual(package["safety_status"], "needs_review")
        self.assertFalse(package["can_apply"])

        vague_meal = self._meal(quantity_milli=None, canonical_unit="vague", unit_family="vague")
        vague = self.store.preview_ingredient_substitutions(
            vague_meal["id"], self._preview_payload(vague_meal, "substitution-preview-vague-0001"), DEMO_USER,
        )
        self.assertTrue(vague["items"])
        self.assertTrue(all(item["safety_status"] == "needs_review" and not item["can_apply"] for item in vague["items"]))

    def test_saved_avoidance_and_incompatible_method_remain_view_only(self) -> None:
        meal = self._meal()
        first = self.store.preview_ingredient_substitutions(
            meal["id"], self._preview_payload(meal, "substitution-preview-avoidance-seed-0001"), DEMO_USER,
        )
        soy = next(item for item in first["items"] if item["to_ingredient"]["name"] == "无糖豆奶")
        self.store.update_my_food_profile({
            "allergies": [],
            "avoidances": [{"ingredient_id": soy["to_ingredient"]["id"]}],
            "preferences": {}, "expected_version": 0,
            "idempotency_key": "substitution-profile-avoidance-0001",
        }, DEMO_PARTNER)
        second = self.store.preview_ingredient_substitutions(
            meal["id"], self._preview_payload(meal, "substitution-preview-avoidance-0002"), DEMO_USER,
        )
        soy_after = next(item for item in second["items"] if item["to_ingredient"]["name"] == "无糖豆奶")
        self.assertEqual(soy_after["safety_status"], "needs_review")
        self.assertFalse(soy_after["can_apply"])
        self.assertIn("saved_avoidance_needs_review", soy_after["safety_reason_codes"])

        pastry = self._meal(cooking_method="起酥")
        incompatible = self.store.preview_ingredient_substitutions(
            pastry["id"], self._preview_payload(pastry, "substitution-preview-method-0001"), DEMO_USER,
        )
        self.assertTrue(incompatible["items"])
        self.assertTrue(all(not item["can_apply"] for item in incompatible["items"]))
        self.assertTrue(all("method_needs_review" in item["safety_reason_codes"] for item in incompatible["items"]))

    def test_allowed_apply_changes_exact_snapshot_row_and_rebuilds_shopping_atomically(self) -> None:
        meal = self._meal(duplicate_source=True)
        before_profiles = self.store.get_food_profiles(DEMO_USER)
        preview = self.store.preview_ingredient_substitutions(
            meal["id"], self._preview_payload(meal, "substitution-preview-apply-0001"), DEMO_USER,
        )
        oat = next(item for item in preview["items"] if item["to_ingredient"]["name"] == "无糖燕麦奶")
        applied = self.store.apply_ingredient_substitution(meal["id"], oat["id"], {
            "expected_meal_version": meal["version"],
            "expected_shopping_list_version": 0,
            "expected_version": oat["version"],
            "idempotency_key": "substitution-apply-safe-0001",
        }, DEMO_USER)
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied, self.store.apply_ingredient_substitution(meal["id"], oat["id"], {
            "expected_meal_version": meal["version"],
            "expected_shopping_list_version": 0,
            "expected_version": oat["version"],
            "idempotency_key": "substitution-apply-safe-0001",
        }, DEMO_USER))
        calendar = self.store.meal_calendar(meal["planned_date"], meal["planned_date"], DEMO_PARTNER)
        stored = calendar["days"][0]["meals"]["dinner"][0]
        by_id = {item["id"]: item for item in stored["recipe_snapshot"]["ingredients"]}
        self.assertEqual(by_id["snapshot-ingredient-1"]["name"], "无糖燕麦奶")
        self.assertEqual(by_id["snapshot-ingredient-2"]["name"], "牛奶")
        self.assertEqual(self.store.get_food_profiles(DEMO_USER), before_profiles)
        shopping = self.store.get_shopping_list(DEMO_PARTNER)
        self.assertIn("无糖燕麦奶", [item["canonical_name"] for item in shopping["items"]])
        self.assertIn("牛奶", [item["canonical_name"] for item in shopping["items"]])

    def test_selected_meal_safe_apply_uses_target_meal_in_atomic_shopping_rebuild(self) -> None:
        meal = self._meal()
        with self.store.connection() as db:
            db.execute("UPDATE meal_cart_items SET status='selected' WHERE id=?", (meal["id"],))
        preview = self.store.preview_ingredient_substitutions(
            meal["id"], self._preview_payload(meal, "substitution-preview-selected-0001"), DEMO_USER,
        )
        oat = next(item for item in preview["items"] if item["to_ingredient"]["name"] == "无糖燕麦奶")
        result = self.store.apply_ingredient_substitution(meal["id"], oat["id"], {
            "expected_meal_version": meal["version"],
            "expected_shopping_list_version": 0,
            "expected_version": oat["version"],
            "idempotency_key": "substitution-apply-selected-0001",
        }, DEMO_USER)
        self.assertEqual(result["status"], "applied")
        stored = self.store.meal_calendar(meal["planned_date"], meal["planned_date"], DEMO_USER)
        self.assertEqual(stored["days"][0]["meals"]["dinner"][0]["status"], "selected")
        self.assertIn("无糖燕麦奶", [item["canonical_name"] for item in self.store.get_shopping_list(DEMO_USER)["items"]])

    def test_profile_change_after_preview_forces_repreview_without_partial_write(self) -> None:
        meal = self._meal()
        preview = self.store.preview_ingredient_substitutions(
            meal["id"], self._preview_payload(meal, "substitution-preview-stale-profile-0001"), DEMO_USER,
        )
        soy = next(item for item in preview["items"] if item["to_ingredient"]["name"] == "无糖豆奶")
        self.store.update_my_food_profile({
            "allergies": [{"allergen_key": "大豆", "severity": "severe"}],
            "avoidances": [], "preferences": {}, "expected_version": 0,
            "idempotency_key": "substitution-profile-changed-0001",
        }, DEMO_PARTNER)
        with self.assertRaises(ConflictError):
            self.store.apply_ingredient_substitution(meal["id"], soy["id"], {
                "expected_meal_version": meal["version"],
                "expected_shopping_list_version": 0,
                "expected_version": soy["version"],
                "idempotency_key": "substitution-apply-stale-profile-0001",
            }, DEMO_USER)
        current = self.store.meal_calendar(meal["planned_date"], meal["planned_date"], DEMO_USER)
        self.assertEqual(current["days"][0]["meals"]["dinner"][0]["recipe_snapshot"]["ingredients"][0]["name"], "牛奶")
        self.assertEqual(self.store.get_shopping_list(DEMO_USER)["version"], 0)

    def test_shopping_change_after_preview_forces_repreview_without_partial_write(self) -> None:
        meal = self._meal()
        preview = self.store.preview_ingredient_substitutions(
            meal["id"], self._preview_payload(meal, "substitution-preview-stale-shopping-0001"), DEMO_USER,
        )
        oat = next(item for item in preview["items"] if item["to_ingredient"]["name"] == "无糖燕麦奶")
        self.store.create_shopping_item({"name": "厨房纸", "quantity": 1, "unit": "包", "category": "household"}, DEMO_PARTNER)
        with self.assertRaises(ConflictError):
            self.store.apply_ingredient_substitution(meal["id"], oat["id"], {
                "expected_meal_version": meal["version"],
                "expected_shopping_list_version": 0,
                "expected_version": oat["version"],
                "idempotency_key": "substitution-apply-stale-shopping-0001",
            }, DEMO_USER)
        current = self.store.meal_calendar(meal["planned_date"], meal["planned_date"], DEMO_USER)
        self.assertEqual(current["days"][0]["meals"]["dinner"][0]["recipe_snapshot"]["ingredients"][0]["name"], "牛奶")
        self.assertEqual([item["canonical_name"] for item in self.store.get_shopping_list(DEMO_USER)["items"]], ["厨房纸"])

    def test_shopping_failure_rolls_back_meal_suggestion_events_and_idempotency(self) -> None:
        meal = self._meal()
        preview = self.store.preview_ingredient_substitutions(
            meal["id"], self._preview_payload(meal, "substitution-preview-rollback-0001"), DEMO_USER,
        )
        oat = next(item for item in preview["items"] if item["to_ingredient"]["name"] == "无糖燕麦奶")
        with self.store.connection() as db:
            before_audit = db.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"]
            before_events = db.execute("SELECT COUNT(*) AS n FROM realtime_events").fetchone()["n"]
        with patch.object(
            self.store, "_apply_shopping_items_in_connection", side_effect=RuntimeError("forced shopping failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced shopping failure"):
                self.store.apply_ingredient_substitution(meal["id"], oat["id"], {
                    "expected_meal_version": meal["version"],
                    "expected_shopping_list_version": 0,
                    "expected_version": oat["version"],
                    "idempotency_key": "substitution-apply-rollback-0001",
                }, DEMO_USER)
        with self.store.connection() as db:
            stored_meal = db.execute("SELECT recipe_snapshot_json,version FROM meal_cart_items WHERE id=?", (meal["id"],)).fetchone()
            stored_suggestion = db.execute("SELECT status,version FROM ingredient_substitutions WHERE id=?", (oat["id"],)).fetchone()
            apply_receipts = db.execute(
                "SELECT COUNT(*) AS n FROM mutation_idempotency WHERE operation LIKE 'ingredient_substitution.apply%'",
            ).fetchone()["n"]
            self.assertEqual(db.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"], before_audit)
            self.assertEqual(db.execute("SELECT COUNT(*) AS n FROM realtime_events").fetchone()["n"], before_events)
        self.assertEqual(stored_meal["version"], meal["version"])
        self.assertEqual(json.loads(stored_meal["recipe_snapshot_json"])["ingredients"][0]["name"], "牛奶")
        self.assertEqual(dict(stored_suggestion), {"status": "suggested", "version": oat["version"]})
        self.assertEqual(apply_receipts, 0)

    def test_late_idempotency_failure_rolls_back_notification_event_and_every_data_change(self) -> None:
        meal = self._meal()
        preview = self.store.preview_ingredient_substitutions(
            meal["id"], self._preview_payload(meal, "substitution-preview-late-failure-0001"), DEMO_USER,
        )
        oat = next(item for item in preview["items"] if item["to_ingredient"]["name"] == "无糖燕麦奶")
        with self.store.connection() as db:
            before = {
                "audit": db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0],
                "events": db.execute("SELECT COUNT(*) FROM realtime_events").fetchone()[0],
                "notifications": db.execute("SELECT COUNT(*) FROM in_app_notifications").fetchone()[0],
            }
        with patch.object(self.store, "_weekly_save", side_effect=RuntimeError("forced late failure")):
            with self.assertRaisesRegex(RuntimeError, "forced late failure"):
                self.store.apply_ingredient_substitution(meal["id"], oat["id"], {
                    "expected_meal_version": meal["version"],
                    "expected_shopping_list_version": 0,
                    "expected_version": oat["version"],
                    "idempotency_key": "substitution-apply-late-failure-0001",
                }, DEMO_USER)
        with self.store.connection() as db:
            row = db.execute("SELECT recipe_snapshot_json,version FROM meal_cart_items WHERE id=?", (meal["id"],)).fetchone()
            suggestion = db.execute("SELECT status,version FROM ingredient_substitutions WHERE id=?", (oat["id"],)).fetchone()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], before["audit"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM realtime_events").fetchone()[0], before["events"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM in_app_notifications").fetchone()[0], before["notifications"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM shopping_lists").fetchone()[0], 0)
        self.assertEqual(row["version"], meal["version"])
        self.assertEqual(json.loads(row["recipe_snapshot_json"])["ingredients"][0]["name"], "牛奶")
        self.assertEqual(dict(suggestion), {"status": "suggested", "version": oat["version"]})

    def test_concurrent_apply_has_one_winner_and_no_database_lock_error(self) -> None:
        meal = self._meal()
        preview = self.store.preview_ingredient_substitutions(
            meal["id"], self._preview_payload(meal, "substitution-preview-concurrent-0001"), DEMO_USER,
        )
        oat = next(item for item in preview["items"] if item["to_ingredient"]["name"] == "无糖燕麦奶")
        barrier = threading.Barrier(2)
        results: list[tuple[str, object]] = []

        def apply(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                value = self.store.apply_ingredient_substitution(meal["id"], oat["id"], {
                    "expected_meal_version": meal["version"],
                    "expected_shopping_list_version": 0,
                    "expected_version": oat["version"],
                    "idempotency_key": f"substitution-apply-concurrent-{index:04d}",
                }, DEMO_USER)
                results.append(("ok", value))
            except Exception as error:  # collected for deterministic assertions below
                results.append(("error", error))

        threads = [threading.Thread(target=apply, args=(index,)) for index in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sum(kind == "ok" for kind, _ in results), 1)
        errors = [value for kind, value in results if kind == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ConflictError)
        self.assertNotIsInstance(errors[0], sqlite3.OperationalError)
        shopping = self.store.get_shopping_list(DEMO_USER)
        oat_rows = [item for item in shopping["items"] if item["canonical_name"] == "无糖燕麦奶"]
        self.assertEqual(len(oat_rows), 1)

    def test_dismiss_and_suggestion_visibility_are_creator_only(self) -> None:
        meal = self._meal()
        partner_before = self.store.list_ingredient_substitutions(meal["id"], DEMO_PARTNER)
        preview = self.store.preview_ingredient_substitutions(
            meal["id"], self._preview_payload(meal, "substitution-preview-private-0001"), DEMO_USER,
        )
        suggestion = preview["items"][0]
        partner_after = self.store.list_ingredient_substitutions(meal["id"], DEMO_PARTNER)
        self.assertEqual(partner_after, partner_before)
        self.assertEqual(partner_after["items"], [])
        self.assertNotIn(suggestion["id"], json.dumps(partner_after, ensure_ascii=False))
        with self.store.connection() as db:
            audit_before = db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        dismissed = self.store.dismiss_ingredient_substitution(meal["id"], suggestion["id"], {
            "expected_version": suggestion["version"],
            "idempotency_key": "substitution-dismiss-private-0001",
        }, DEMO_USER)
        self.assertEqual(dismissed["status"], "dismissed")
        self.assertEqual(dismissed, self.store.dismiss_ingredient_substitution(meal["id"], suggestion["id"], {
            "expected_version": suggestion["version"],
            "idempotency_key": "substitution-dismiss-private-0001",
        }, DEMO_USER))
        self.assertEqual(self.store.list_ingredient_substitutions(meal["id"], DEMO_PARTNER), partner_before)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], audit_before)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM realtime_events WHERE resource_id=?", (meal["id"],),
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM in_app_notifications WHERE resource_id=?", (meal["id"],),
            ).fetchone()[0], 0)
        archive_bytes, _ = self.store.account_export(DEMO_PARTNER)
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            partner_export = archive.read("data.json").decode("utf-8")
        self.assertNotIn(suggestion["id"], partner_export)

    def test_each_candidate_has_its_own_shopping_preview_and_apply_returns_the_same_diff(self) -> None:
        meal = self._meal()
        preview = self.store.preview_ingredient_substitutions(
            meal["id"], self._preview_payload(meal, "substitution-preview-per-candidate-0001"), DEMO_USER,
        )
        soy = next(item for item in preview["items"] if item["to_ingredient"]["name"] == "无糖豆奶")
        oat = next(item for item in preview["items"] if item["to_ingredient"]["name"] == "无糖燕麦奶")
        self.assertEqual(soy["shopping_preview"]["changes"]["add"], ["无糖豆奶"])
        self.assertEqual(oat["shopping_preview"]["changes"]["add"], ["无糖燕麦奶"])
        self.assertNotEqual(soy["shopping_preview"], oat["shopping_preview"])

        applied = self.store.apply_ingredient_substitution(meal["id"], oat["id"], {
            "expected_meal_version": meal["version"],
            "expected_shopping_list_version": oat["shopping_preview"]["expected_list_version"],
            "expected_version": oat["version"],
            "idempotency_key": "substitution-apply-per-candidate-0001",
        }, DEMO_USER)
        self.assertEqual(applied["shopping_preview"], oat["shopping_preview"])
        self.assertEqual(applied["shopping_diff"], oat["shopping_preview"]["diff"])


class CookingTestCase(KitchenTestCase):
    recipe_id = "recipe_builtin_kungpao_chicken"

    def start(self, *, visibility: str = "shared", user_id: str = DEMO_USER, key: str | None = None) -> dict:
        return self.store.start_cook_session({
            "recipe_id": self.recipe_id,
            "visibility": visibility,
            "idempotency_key": key or f"cook-start-{uuid.uuid4().hex}",
        }, user_id)

    def meal(self) -> dict:
        planned = (date.today() + timedelta(days=1)).isoformat()
        item = self.store.add_meal_cart_item({
            "recipe_id": self.recipe_id,
            "planned_date": planned,
            "meal_slot": "dinner",
            "servings": 2,
            "idempotency_key": f"cook-meal-{uuid.uuid4().hex}",
        }, DEMO_USER)
        return self.store.update_meal_cart_item(item["id"], {
            "status": "accepted", "expected_version": item["version"],
        }, DEMO_USER)


class CookingSessionTests(CookingTestCase):
    def test_direct_recipe_snapshot_survives_source_edit_and_delete(self) -> None:
        started = self.start(key="cook-source-freeze-start-0001")
        frozen = json.loads(json.dumps(started["recipe_snapshot"], ensure_ascii=False))
        with self.store.connection() as db:
            db.execute(
                "UPDATE recipes SET name='源菜谱后来改名',deleted_at=?,updated_at=? WHERE id=?",
                (datetime.now().isoformat(), datetime.now().isoformat(), self.recipe_id),
            )
        restored = self.store.get_cook_session(started["id"], DEMO_USER)
        self.assertEqual(restored["recipe_snapshot"], frozen)
        self.assertEqual(restored["name"], frozen["name"])

    def test_meal_start_freezes_snapshot_and_resumes_without_duplicate_session(self) -> None:
        meal = self.meal()
        started = self.store.start_cook_session({
            "meal_item_id": meal["id"], "visibility": "shared",
            "expected_meal_version": meal["version"],
            "idempotency_key": "cook-start-meal-snapshot-0001",
        }, DEMO_USER)
        original = json.loads(json.dumps(started["recipe_snapshot"], ensure_ascii=False))
        with self.store.connection() as db:
            changed = json.loads(db.execute(
                "SELECT recipe_snapshot_json FROM meal_cart_items WHERE id=?", (meal["id"],),
            ).fetchone()[0])
            changed["name"] = "后来修改的菜单快照"
            db.execute("UPDATE meal_cart_items SET recipe_snapshot_json=? WHERE id=?", (json.dumps(changed), meal["id"]))
        restored = self.store.get_cook_session(started["id"], DEMO_PARTNER)
        self.assertEqual(restored["recipe_snapshot"], original)
        resumed = self.store.start_cook_session({
            "meal_item_id": meal["id"], "visibility": "shared",
            "expected_meal_version": started["source_meal_version"],
            "idempotency_key": "cook-start-meal-snapshot-0002",
        }, DEMO_PARTNER)
        self.assertEqual(resumed["id"], started["id"])
        self.assertTrue(resumed["resumed"])
        with self.store.connection() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM cook_sessions WHERE meal_item_id=? AND status='active'", (meal["id"],),
            ).fetchone()[0], 1)

    def test_private_meal_session_is_rejected_without_shared_side_effect_or_visibility_change(self) -> None:
        meal = self.meal()
        partner_sessions_before = self.store.list_cook_sessions(DEMO_PARTNER, status="all")
        partner_cart_before = self.store.get_meal_cart(DEMO_PARTNER)
        with self.store.connection() as db:
            row = db.execute(
                "SELECT status,version,cook_user_id FROM meal_cart_items WHERE id=?", (meal["id"],),
            ).fetchone()
            stored_before = (row["status"], row["version"], row["cook_user_id"])
        with self.assertRaises(ValueError):
            self.store.start_cook_session({
                "meal_item_id": meal["id"], "visibility": "private",
                "expected_meal_version": meal["version"],
                "idempotency_key": "cook-private-meal-rejected-0001",
            }, DEMO_USER)
        self.assertEqual(self.store.list_cook_sessions(DEMO_PARTNER, status="all"), partner_sessions_before)
        self.assertEqual(self.store.get_meal_cart(DEMO_PARTNER), partner_cart_before)
        with self.store.connection() as db:
            current = db.execute(
                "SELECT status,version,cook_user_id FROM meal_cart_items WHERE id=?", (meal["id"],),
            ).fetchone()
            self.assertEqual((current["status"], current["version"], current["cook_user_id"]), stored_before)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM mutation_idempotency WHERE idempotency_key=?",
                ("cook-private-meal-rejected-0001",),
            ).fetchone()[0], 0)

    def test_private_session_is_unobservable_but_shared_session_supports_versioned_steps(self) -> None:
        before = self.store.list_cook_sessions(DEMO_PARTNER)
        private = self.start(visibility="private", key="cook-private-start-0001")
        self.assertEqual(self.store.list_cook_sessions(DEMO_PARTNER), before)
        with self.assertRaises(KeyError):
            self.store.get_cook_session(private["id"], DEMO_PARTNER)
        with self.assertRaises(KeyError):
            self.store.set_cook_session_step(private["id"], {
                "step_index": 1, "expected_version": private["version"],
                "idempotency_key": "cook-private-partner-step-0001",
            }, DEMO_PARTNER)

        shared = self.store.start_cook_session({
            "recipe_id": "recipe_builtin_mapo_tofu", "visibility": "shared",
            "idempotency_key": "cook-shared-start-0001",
        }, DEMO_USER)
        moved = self.store.set_cook_session_step(shared["id"], {
            "step_index": 1, "expected_version": shared["version"],
            "idempotency_key": "cook-shared-step-0001",
        }, DEMO_PARTNER)
        self.assertEqual(moved["current_step"], 1)
        with self.assertRaises(ConflictError):
            self.store.set_cook_session_step(shared["id"], {
                "step_index": 2, "expected_version": shared["version"],
                "idempotency_key": "cook-shared-step-stale-0001",
            }, DEMO_USER)

    def test_start_is_target_bound_idempotent_and_rejects_invalid_sources(self) -> None:
        first = self.start(key="cook-start-idempotent-0001")
        self.assertEqual(first, self.start(key="cook-start-idempotent-0001"))
        with self.assertRaises(ConflictError):
            self.store.start_cook_session({
                "recipe_id": self.recipe_id, "visibility": "private",
                "idempotency_key": "cook-start-idempotent-0001",
            }, DEMO_USER)

    def test_private_recipe_cannot_be_shared_or_started_by_partner(self) -> None:
        with self.store.connection() as db:
            couple, _ = self.store._couple_context(db, DEMO_USER)
            db.execute(
                "UPDATE recipes SET visibility='private',owner_id=?,couple_id=? WHERE id=?",
                (DEMO_USER, couple["id"], self.recipe_id),
            )
        with self.assertRaises(PermissionError):
            self.store.start_cook_session({
                "recipe_id": self.recipe_id, "visibility": "shared",
                "idempotency_key": "cook-private-recipe-shared-0001",
            }, DEMO_USER)
        with self.assertRaises(KeyError):
            self.store.start_cook_session({
                "recipe_id": self.recipe_id, "visibility": "private",
                "idempotency_key": "cook-private-recipe-partner-0001",
            }, DEMO_PARTNER)
        self.assertEqual(self.store.list_cook_sessions(DEMO_USER), [])

    def test_meal_completion_rejects_changed_source_version_and_rolls_back_session(self) -> None:
        meal = self.meal()
        started = self.store.start_cook_session({
            "meal_item_id": meal["id"], "visibility": "shared",
            "expected_meal_version": meal["version"],
            "idempotency_key": "cook-source-cas-start-0001",
        }, DEMO_USER)
        with self.store.connection() as db:
            db.execute("UPDATE meal_cart_items SET note='对方刚补充',version=version+1 WHERE id=?", (meal["id"],))
        with self.assertRaises(ConflictError):
            self.store.complete_cook_session(started["id"], {
                "expected_version": started["version"], "idempotency_key": "cook-source-cas-complete-0001",
            }, DEMO_USER)
        self.assertEqual(self.store.get_cook_session(started["id"], DEMO_USER)["status"], "active")

    def test_private_sessions_have_no_shared_audit_event_and_partner_export_cannot_see_them(self) -> None:
        with self.store.connection() as db:
            couple, _ = self.store._couple_context(db, DEMO_USER)
            before = {
                "audit": db.execute("SELECT COUNT(*) FROM audit_events WHERE couple_id=?", (couple["id"],)).fetchone()[0],
                "events": db.execute("SELECT COUNT(*) FROM realtime_events WHERE couple_id=?", (couple["id"],)).fetchone()[0],
            }
        private = self.start(visibility="private", key="cook-private-export-start-0001")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_events WHERE couple_id=?", (couple["id"],)).fetchone()[0], before["audit"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM realtime_events WHERE couple_id=?", (couple["id"],)).fetchone()[0], before["events"])
        archive, _ = self.store.account_export(DEMO_PARTNER)
        with zipfile.ZipFile(io.BytesIO(archive)) as exported:
            partner_data = json.loads(exported.read("data.json"))
        self.assertNotIn(private["id"], {item["id"] for item in partner_data["cook_sessions"]})
        archive, _ = self.store.account_export(DEMO_USER)
        with zipfile.ZipFile(io.BytesIO(archive)) as exported:
            owner_data = json.loads(exported.read("data.json"))
        self.assertIn(private["id"], {item["id"] for item in owner_data["cook_sessions"]})
        with self.assertRaises(ValueError):
            self.store.start_cook_session({
                "recipe_id": self.recipe_id, "meal_item_id": "meal-x",
                "visibility": "shared", "idempotency_key": "cook-start-both-0001",
            }, DEMO_USER)


class CookingTimerTests(CookingTestCase):
    def test_multiple_timers_use_server_absolute_ends_and_restore_after_restart(self) -> None:
        session = self.start(key="cook-timers-start-0001")
        timers = [
            self.store.start_cook_timer(session["id"], {
                "step_index": 0, "duration_seconds": seconds, "label": label,
                "idempotency_key": key,
            }, DEMO_USER)
            for seconds, label, key in ((60, "焯水", "cook-timer-a-0001"), (120, "焖煮", "cook-timer-b-0001"))
        ]
        self.assertEqual(len({timer["id"] for timer in timers}), 2)
        for timer in timers:
            started = datetime.fromisoformat(timer["started_at"])
            ends = datetime.fromisoformat(timer["ends_at"])
            self.assertEqual(int((ends - started).total_seconds()), timer["duration_seconds"])
            self.assertNotIn("remaining_seconds", timer)
        restarted = LoveMomentsStore(Path(self.store.db_path), seed_demo_data=False)
        restored = restarted.get_cook_session(session["id"], DEMO_USER)
        self.assertEqual({timer["id"]: timer["ends_at"] for timer in restored["timers"]}, {
            timer["id"]: timer["ends_at"] for timer in timers
        })

    def test_elapsed_timer_is_effectively_complete_and_cannot_be_cancelled(self) -> None:
        session = self.start(key="cook-elapsed-start-0001")
        timer = self.store.start_cook_timer(session["id"], {
            "step_index": 0, "duration_seconds": 60, "label": "短计时",
            "idempotency_key": "cook-elapsed-timer-0001",
        }, DEMO_USER)
        with self.store.connection() as db:
            db.execute(
                "UPDATE cook_timers SET started_at=?,ends_at=? WHERE id=?",
                ("1999-12-31T23:59:00+00:00", "2000-01-01T00:00:00+00:00", timer["id"]),
            )
        current = self.store.get_cook_session(session["id"], DEMO_USER)
        elapsed = next(item for item in current["timers"] if item["id"] == timer["id"])
        self.assertEqual(elapsed["status"], "completed")
        with self.assertRaises(ConflictError):
            self.store.cancel_cook_timer(session["id"], timer["id"], {
                "expected_version": timer["version"], "idempotency_key": "cook-cancel-elapsed-0001",
            }, DEMO_USER)

    def test_timer_rejects_client_clock_fields_and_changed_idempotency_payload(self) -> None:
        session = self.start(key="cook-timer-validation-start-0001")
        with self.assertRaises(ValueError):
            self.store.start_cook_timer(session["id"], {
                "step_index": 0, "duration_seconds": 60, "label": "非法",
                "ends_at": "2099-01-01T00:00:00+00:00",
                "idempotency_key": "cook-timer-clock-0001",
            }, DEMO_USER)
        timer = self.store.start_cook_timer(session["id"], {
            "step_index": 0, "duration_seconds": 60, "label": "合法",
            "idempotency_key": "cook-timer-idem-0001",
        }, DEMO_USER)
        self.assertEqual(timer, self.store.start_cook_timer(session["id"], {
            "step_index": 0, "duration_seconds": 60, "label": "合法",
            "idempotency_key": "cook-timer-idem-0001",
        }, DEMO_USER))
        with self.assertRaises(ConflictError):
            self.store.start_cook_timer(session["id"], {
                "step_index": 0, "duration_seconds": 90, "label": "合法",
                "idempotency_key": "cook-timer-idem-0001",
            }, DEMO_USER)


class CookingInventoryFinishTests(CookingTestCase):
    def test_complete_only_returns_unselected_suggestions_and_never_changes_pantry(self) -> None:
        session = self.start(key="cook-complete-start-0001")
        before = self.store.list_pantry_items(DEMO_USER)
        completed = self.store.complete_cook_session(session["id"], {
            "expected_version": session["version"], "idempotency_key": "cook-complete-0001",
        }, DEMO_USER)
        self.assertEqual(self.store.list_pantry_items(DEMO_USER), before)
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(completed["pantry_suggestions"])
        for item in completed["pantry_suggestions"]:
            self.assertIsNone(item["selected_state"])
            self.assertEqual(item["allowed_states"], ["low", "out"])

    def test_explicit_finalize_is_atomic_returns_receipts_and_replays(self) -> None:
        session = self.start(key="cook-finalize-start-0001")
        completed = self.store.complete_cook_session(session["id"], {
            "expected_version": session["version"], "idempotency_key": "cook-finalize-complete-0001",
        }, DEMO_USER)
        chosen = completed["pantry_suggestions"][:2]
        payload = {
            "expected_version": completed["version"],
            "updates": [
                {"ingredient_ref": chosen[0]["ingredient_ref"], "state": "low", "expected_pantry_version": chosen[0]["pantry_version"]},
                {"ingredient_ref": chosen[1]["ingredient_ref"], "state": "out", "expected_pantry_version": chosen[1]["pantry_version"]},
            ],
            "idempotency_key": "cook-finalize-apply-0001",
        }
        result = self.store.apply_cook_pantry_updates(session["id"], payload, DEMO_USER)
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(all(item.get("undo", {}).get("receipt_id") for item in result["items"]))
        self.assertEqual(result, self.store.apply_cook_pantry_updates(session["id"], payload, DEMO_USER))
        with self.assertRaises(ConflictError):
            self.store.apply_cook_pantry_updates(session["id"], {
                **payload, "updates": [{**payload["updates"][0], "state": "out"}],
            }, DEMO_USER)

    def test_undo_newly_created_pantry_item_deletes_it_instead_of_inventing_have(self) -> None:
        session = self.start(key="cook-finalize-created-undo-start-0001")
        completed = self.store.complete_cook_session(session["id"], {
            "expected_version": session["version"],
            "idempotency_key": "cook-finalize-created-undo-complete-0001",
        }, DEMO_USER)
        chosen = [item for item in completed["pantry_suggestions"] if item["pantry_version"] == 0][:2]
        self.assertEqual(len(chosen), 2)
        applied = self.store.apply_cook_pantry_updates(session["id"], {
            "expected_version": completed["version"],
            "updates": [
                {"ingredient_ref": chosen[0]["ingredient_ref"], "state": "low", "expected_pantry_version": 0},
                {"ingredient_ref": chosen[1]["ingredient_ref"], "state": "out", "expected_pantry_version": 0},
            ],
            "idempotency_key": "cook-finalize-created-undo-apply-0001",
        }, DEMO_USER)
        created = applied["items"][0]
        self.assertEqual(created["state"], "low")
        undone = self.store.undo_mutation(created["undo"]["receipt_id"], DEMO_USER)
        self.assertTrue(undone["undone"])
        self.assertTrue(self.store.undo_mutation(created["undo"]["receipt_id"], DEMO_USER)["idempotent"])
        concurrent = applied["items"][1]
        with self.store.connection() as db:
            self.assertIsNone(db.execute(
                "SELECT id FROM pantry_items WHERE id=?", (created["id"],),
            ).fetchone())
            db.execute("UPDATE pantry_items SET version=version+1 WHERE id=?", (concurrent["id"],))
        with self.assertRaises(ConflictError):
            self.store.undo_mutation(concurrent["undo"]["receipt_id"], DEMO_USER)
        with self.store.connection() as db:
            self.assertIsNotNone(db.execute(
                "SELECT id FROM pantry_items WHERE id=?", (concurrent["id"],),
            ).fetchone())

    def test_finalize_rejects_implicit_or_stale_updates_without_partial_writes(self) -> None:
        session = self.start(key="cook-finalize-rollback-start-0001")
        completed = self.store.complete_cook_session(session["id"], {
            "expected_version": session["version"], "idempotency_key": "cook-finalize-rollback-complete-0001",
        }, DEMO_USER)
        first, second = completed["pantry_suggestions"][:2]
        existing = self.pantry(second["name"], state="have", key="cook-finalize-stale-pantry-0001")
        with self.store.connection() as db:
            before = {
                "pantry": [dict(row) for row in db.execute("SELECT * FROM pantry_items ORDER BY id").fetchall()],
                "session": db.execute("SELECT version FROM cook_sessions WHERE id=?", (session["id"],)).fetchone()[0],
                "receipts": db.execute("SELECT COUNT(*) FROM mutation_receipts").fetchone()[0],
                "events": db.execute("SELECT COUNT(*) FROM realtime_events").fetchone()[0],
            }
        with self.assertRaises(ConflictError):
            self.store.apply_cook_pantry_updates(session["id"], {
                "expected_version": completed["version"],
                "updates": [
                    {"ingredient_ref": first["ingredient_ref"], "state": "low", "expected_pantry_version": first["pantry_version"]},
                    {"ingredient_ref": second["ingredient_ref"], "state": "out", "expected_pantry_version": 0},
                ],
                "idempotency_key": "cook-finalize-stale-apply-0001",
            }, DEMO_USER)
        with self.store.connection() as db:
            self.assertEqual([dict(row) for row in db.execute("SELECT * FROM pantry_items ORDER BY id").fetchall()], before["pantry"])
            self.assertEqual(db.execute("SELECT version FROM cook_sessions WHERE id=?", (session["id"],)).fetchone()[0], before["session"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM mutation_receipts").fetchone()[0], before["receipts"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM realtime_events").fetchone()[0], before["events"])
        self.assertEqual(existing["state"], "have")
        with self.assertRaises(ValueError):
            self.store.apply_cook_pantry_updates(session["id"], {
                "expected_version": completed["version"],
                "updates": [{"ingredient_ref": first["ingredient_ref"], "state": "have", "expected_pantry_version": 0}],
                "idempotency_key": "cook-finalize-have-0001",
            }, DEMO_USER)


if __name__ == "__main__":
    unittest.main()
