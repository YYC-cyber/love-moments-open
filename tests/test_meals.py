from __future__ import annotations

import tempfile
import unittest
import uuid
import sqlite3
import os
import json
import hashlib
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from love_moments.meals import MealConflictError
from love_moments.recipe_catalog_expansion import AMOUNTS, DISHES
from love_moments.recipe_catalog_quality import CONCENTRATED_MAX_GRAMS, safety_tip
from love_moments.recipes import contains_chinese, load_builtin_recipes
from love_moments.schema import SCHEMA_VERSION
from love_moments.store import DEMO_PARTNER, DEMO_USER, LoveMomentsStore
from tests import VALID_PNG


class MealRecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(Path(self.temp.name) / "meals.db", seed_demo_data=True)
        self.tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.recipe = self.store.list_recipes(DEMO_USER, query="番茄炒蛋", limit=5)["items"][0]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_meal(self, user_id: str = DEMO_USER, **overrides):
        return self.store.add_meal_cart_item({
            "recipe_id": self.recipe["id"],
            "planned_date": self.tomorrow,
            "meal_slot": "dinner",
            "servings": 2,
            "idempotency_key": uuid.uuid4().hex,
            **overrides,
        }, user_id)

    def test_schema_v15_recipe_tables_upgrade_without_losing_compatibility(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy-v15.db"
        with sqlite3.connect(legacy_path) as db:
            db.executescript("""
                CREATE TABLE recipe_ingredients (
                    id TEXT PRIMARY KEY, recipe_id TEXT NOT NULL, name TEXT NOT NULL,
                    canonical_name TEXT NOT NULL, quantity_milli INTEGER,
                    quantity_text TEXT NOT NULL DEFAULT '', canonical_unit TEXT NOT NULL,
                    unit_family TEXT NOT NULL, specification TEXT NOT NULL DEFAULT '',
                    optional INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL
                );
                CREATE TABLE recipe_steps (
                    id TEXT PRIMARY KEY, recipe_id TEXT NOT NULL, instruction TEXT NOT NULL,
                    timer_seconds INTEGER, position INTEGER NOT NULL
                );
                PRAGMA user_version = 15;
            """)
        upgraded = LoveMomentsStore(legacy_path, seed_demo_data=True)
        with upgraded.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            ingredient_columns = {row[1] for row in db.execute("PRAGMA table_info(recipe_ingredients)")}
            step_columns = {row[1] for row in db.execute("PRAGMA table_info(recipe_steps)")}
            self.assertTrue({"preparation", "ingredient_group"}.issubset(ingredient_columns))
            self.assertTrue({"title", "heat_level", "key_point"}.issubset(step_columns))
        detail = upgraded.get_recipe("recipe_builtin_tomato_egg", DEMO_USER)
        self.assertEqual(detail["version"], 4)

    def test_every_builtin_recipe_has_measured_ingredients_and_timed_steps(self) -> None:
        recipes = load_builtin_recipes()
        self.assertGreaterEqual(len(recipes), 200)
        for recipe in recipes:
            self.assertTrue(all(item["quantity_milli"] for item in recipe["ingredients"]), recipe["name"])
            self.assertTrue(all(item["preparation"] for item in recipe["ingredients"]), recipe["name"])
            self.assertTrue(all(step["timer_seconds"] for step in recipe["steps"]), recipe["name"])
            self.assertTrue(all(step["heat_level"] not in {"", "按步骤调整"} for step in recipe["steps"]), recipe["name"])
            self.assertTrue(all(step["key_point"] for step in recipe["steps"]), recipe["name"])

    def test_every_builtin_recipe_passes_content_quality_and_safety_review(self) -> None:
        recipes = load_builtin_recipes()
        self.assertEqual(len(recipes), 244)
        self.assertFalse({name for spec in DISHES for name in spec.ingredients} - set(AMOUNTS))
        wait_words = ("浸泡", "泡软", "静置", "醒发", "发酵", "冷藏", "冷却", "腌", "放凉", "吐沙")
        ambiguous = ("或水开后上锅蒸制", "蒸制或中火浸煮", "烤箱提前预热至180～210℃；煎制菜肴")
        for recipe in recipes:
            text = "".join(
                str(step.get(field, ""))
                for step in recipe["steps"]
                for field in ("title", "instruction", "heat_level", "key_point")
            )
            text += "".join(item["preparation"] for item in recipe["ingredients"])
            self.assertFalse(any(value in text for value in ambiguous), recipe["name"])
            self.assertGreaterEqual(recipe["passive_minutes"], 0, recipe["name"])
            if recipe["passive_minutes"]:
                self.assertTrue(any(word in text for word in wait_words), recipe["name"])
            ingredient_names = {item["name"] for item in recipe["ingredients"]}
            self.assertTrue(any(name in text for name in ingredient_names), recipe["name"])
            self.assertTrue(any("室温" in tip and "2小时" in tip for tip in recipe["tips"]), recipe["name"])
            if safety_tip(ingredient_names):
                self.assertTrue(any("食品安全" in tip for tip in recipe["tips"]), recipe["name"])
            for item in recipe["ingredients"]:
                if item["name"] in CONCENTRATED_MAX_GRAMS and item["canonical_unit"] == "g":
                    self.assertLessEqual(item["quantity_milli"] / 1000, CONCENTRATED_MAX_GRAMS[item["name"]], recipe["name"])
        expected = {
            "陈皮焖牛肉": ("陈皮", 8), "肉桂卷": ("肉桂粉", 6),
            "陕西油泼面": ("辣椒粉", 12), "香草煎羊排": ("迷迭香", 5),
            "蒜香橄榄油意面": ("欧芹", 10),
        }
        for recipe_name, (ingredient_name, grams) in expected.items():
            recipe = next(item for item in recipes if item["name"] == recipe_name)
            ingredient = next(item for item in recipe["ingredients"] if item["name"] == ingredient_name)
            self.assertEqual((ingredient["quantity_milli"], ingredient["canonical_unit"]), (grams * 1000, "g"))
        cinnamon_roll = next(item for item in recipes if item["name"] == "肉桂卷")
        self.assertFalse(any("猪牛羊肉" in tip for tip in cinnamon_roll["tips"]))

    def test_generated_detailed_draft_can_be_confirmed(self) -> None:
        with patch.dict(os.environ, {"LLM_API_KEY": ""}, clear=False):
            generated = self.store.generate_recipe_draft({"prompt": "番茄炒蛋"}, DEMO_USER)
        detail = generated["draft"]["recipe"]
        self.assertTrue(all(item["quantity_milli"] for item in detail["ingredients"]))
        self.assertTrue(all(step["timer_seconds"] for step in detail["steps"]))
        uploaded = self.store.save_media(VALID_PNG, "番茄炒蛋成品.png", "image/png", "shared", DEMO_USER)
        confirmed = self.store.confirm_recipe_draft(generated["draft"]["id"], {
            "visibility": "couple",
            "cover_url": uploaded["url"],
            "cover_confirmed_exact": True,
            "cover_attribution": "测试中已核对",
        }, DEMO_USER)
        self.assertEqual(confirmed["name"], "番茄炒蛋")

    def test_builtin_library_has_chinese_and_western_recipes_in_chinese(self) -> None:
        recipes = load_builtin_recipes()
        self.assertGreaterEqual(len(recipes), 200)
        self.assertLessEqual(len(recipes), 300)
        self.assertEqual({item["cuisine"] for item in recipes}, {"chinese", "western"})
        required_categories = {"家常菜", "川菜", "粤菜", "湘菜", "东北菜", "江浙菜", "面食", "汤羹", "早餐", "烘焙", "西餐"}
        self.assertTrue(required_categories.issubset({item["category"] for item in recipes}))
        self.assertEqual(len({item["id"] for item in recipes}), len(recipes))
        self.assertEqual(len({item["normalized_name"] for item in recipes}), len(recipes))
        for item in recipes:
            self.assertTrue(contains_chinese(item["name"]))
            self.assertTrue(all(contains_chinese(step["instruction"]) for step in item["steps"]))
            self.assertGreaterEqual(item["data_version"], 2)
            self.assertGreaterEqual(len(item["steps"]), 4)
            self.assertTrue(any(ingredient["ingredient_group"] == "seasoning" for ingredient in item["ingredients"]))
            self.assertTrue(all(ingredient["preparation"] for ingredient in item["ingredients"]))
            self.assertTrue(all(step["title"] and step["heat_level"] and step["key_point"] for step in item["steps"]))
        tomato = next(item for item in recipes if item["id"] == "recipe_builtin_tomato_egg")
        salt = next(item for item in tomato["ingredients"] if item["name"] == "盐")
        self.assertEqual((salt["quantity_milli"], salt["canonical_unit"]), (2000, "g"))
        self.assertIn("盐 2 克", tomato["steps"][0]["instruction"])
        self.assertNotIn("生鸡肉", tomato["steps"][1]["key_point"])

    def test_curated_builtin_covers_are_local_traceable_and_unique(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "love_moments/data/recipe_cover_manifest.json").read_text(encoding="utf-8"))
        recipes = {item["id"]: item for item in load_builtin_recipes()}
        self.assertEqual(len(manifest), len(recipes))
        self.assertEqual(set(manifest), set(recipes))
        hashes: set[str] = set()
        for recipe_id, cover in manifest.items():
            self.assertIn(recipe_id, recipes)
            self.assertEqual(cover["recipe_name"], recipes[recipe_id]["name"])
            self.assertIn(cover["provider"], {"openverse", "sogou_recipe_page", "bing_recipe_page", "reviewed_recipe_page", "project_generated"})
            if cover["provider"] == "project_generated":
                self.assertTrue(cover["source_page"].startswith("generated://"))
            else:
                self.assertTrue(cover["source_page"].startswith("https://"))
            self.assertTrue(cover["source_title"])
            self.assertIn(cover["match_rule"], {
                "normalized_exact_title", "complete_dish_name_in_recipe_page_title", "curated_equivalent_dish_alias",
                "manually_reviewed_exact_recipe", "generated_exact_dish",
            })
            local_path = root / str(cover["url"]).removeprefix("/")
            self.assertTrue(local_path.is_file(), recipe_id)
            self.assertEqual(hashlib.sha256(local_path.read_bytes()).hexdigest(), cover["sha256"], recipe_id)
            self.assertNotIn(cover["perceptual_hash"], hashes, recipe_id)
            hashes.add(cover["perceptual_hash"])
        tomato = self.store.get_recipe("recipe_builtin_tomato_egg", DEMO_USER)
        self.assertEqual(tomato["cover"]["url"], manifest["recipe_builtin_tomato_egg"]["url"])
        crawfish = manifest["recipe_builtin_exp_hunan_crawfish"]
        self.assertIn("虾", crawfish["source_title"])
        self.assertIn("虾", crawfish["search_name"])
        sha_zhu_cai = manifest["recipe_builtin_exp_suancai_stew"]
        self.assertIn("杀猪菜", sha_zhu_cai["source_title"])
        self.assertIn("血肠", sha_zhu_cai["source_title"])

    def test_recipe_filters_and_pagination_are_bounded(self) -> None:
        result = self.store.list_recipes(
            DEMO_USER, cuisine="western", meal_slot="breakfast", difficulty="easy", max_minutes=20, limit=50,
        )
        self.assertGreater(len(result["items"]), 0)
        self.assertTrue(all(item["cuisine"] == "western" for item in result["items"]))
        self.assertTrue(all("breakfast" in item["meal_slots"] for item in result["items"]))
        self.assertTrue(all(item["difficulty"] == "easy" and item["total_minutes"] <= 20 for item in result["items"]))
        with self.assertRaises(ValueError):
            self.store.list_recipes(DEMO_USER, difficulty="impossible")
        with self.assertRaises(ValueError):
            self.store.list_recipes(DEMO_USER, max_minutes="not-a-number")

    def test_exact_recipe_image_variants_and_video_matching_rules(self) -> None:
        first = self.store.copy_recipe(self.recipe["id"], {"visibility": "couple"}, DEMO_USER)
        uploaded = self.store.save_media(VALID_PNG, "番茄炒蛋成品.png", "image/png", "shared", DEMO_USER)
        with self.assertRaises(ValueError):
            self.store.add_recipe_media(first["id"], {
                "media_type": "image", "role": "cover", "url": uploaded["url"],
            }, DEMO_USER)
        cover = self.store.add_recipe_media(first["id"], {
            "media_type": "image", "role": "cover", "url": uploaded["url"],
            "thumbnail_url": uploaded["thumbnail_url"], "provider": "local",
            "title": "番茄炒蛋经典做法成品图", "confirmed_exact": True,
        }, DEMO_USER)
        self.assertEqual(cover["match_status"], "verified")
        partner_view = self.store.get_recipe(first["id"], DEMO_PARTNER)
        self.assertEqual(partner_view["cover"]["url"], uploaded["url"])

        second = self.store.copy_recipe(self.recipe["id"], {"visibility": "couple"}, DEMO_USER)
        second = self.store.update_recipe(second["id"], {
            "variant_name": "少油滑嫩版", "cooking_method": "少油快炒",
            "method_tags": ["少油", "滑嫩"], "expected_version": second["version"],
            "replacement_cover": {
                "url": uploaded["url"], "thumbnail_url": uploaded["thumbnail_url"], "confirmed_exact": True,
            },
        }, DEMO_USER)
        variants = self.store.list_recipe_variants(first["dish_id"], DEMO_PARTNER)
        self.assertEqual(variants["count"], 2)
        compared = self.store.compare_recipe_variants({"recipe_ids": [first["id"], second["id"]]}, DEMO_PARTNER)
        self.assertEqual(compared["dish_name"], "番茄炒蛋")

        with self.assertRaises(ValueError):
            self.store.add_recipe_media(first["id"], {
                "media_type": "video", "role": "paired", "url": "https://www.bilibili.com/video/BV1test",
                "title": "番茄炒蛋教学", "confirmed_exact": True, "alignment_score": .7,
                "checked_ingredients": True, "checked_steps": True,
            }, DEMO_USER)
        with self.assertRaises(ValueError):
            self.store.add_recipe_media(first["id"], {
                "media_type": "video", "role": "paired", "url": "https://www.bilibili.com/video/BV1unchecked",
                "title": "未逐项核对的教学", "confirmed_exact": True, "alignment_score": .95,
            }, DEMO_USER)
        paired = self.store.add_recipe_media(first["id"], {
            "media_type": "video", "role": "paired", "url": "https://www.bilibili.com/video/BV1paired",
            "title": "已逐项核对的番茄炒蛋教学", "confirmed_exact": True, "alignment_score": .95,
            "checked_ingredients": True, "checked_steps": True,
        }, DEMO_USER)
        self.assertEqual(paired["match_details"]["recipe_version"], first["version"])
        reference = self.store.add_recipe_media(first["id"], {
            "media_type": "video", "role": "reference", "url": "https://www.bilibili.com/video/BV1test",
            "title": "番茄炒蛋参考视频", "alignment_score": .6,
            "differences": ["视频糖量不同，请以页面用量为准"],
        }, DEMO_USER)
        self.assertEqual(reference["match_status"], "reference")
        changed_steps = [dict(step) for step in first["steps"]]
        changed_steps[0]["instruction"] += " 出锅前再次核对咸淡。"
        changed = self.store.update_recipe(first["id"], {
            "steps": changed_steps, "expected_version": first["version"],
        }, DEMO_USER)
        stale = next(item for item in changed["media"] if item["id"] == paired["id"])
        self.assertEqual((stale["role"], stale["match_status"]), ("reference", "reference"))
        self.assertIn("页面配方或步骤已修改，需要重新对照视频", stale["match_details"]["differences"])

    def test_external_recipe_cover_is_provider_bound_and_cached_locally(self) -> None:
        detail = self.store.get_recipe(self.recipe["id"], DEMO_USER)
        value = {
            **detail,
            "media": [{
                "media_type": "image", "role": "cover", "provider": "spoonacular",
                "url": "https://img.spoonacular.com/recipes/123-556x370.png",
                "title": "Tomato Eggs", "attribution": "Spoonacular", "match_status": "verified",
                "match_details": {
                    "binding": "provider_recipe_result", "source_recipe_name": "Tomato Eggs",
                    "source_recipe_id": "123",
                },
            }],
        }
        with self.store.connection() as db:
            couple, _ = self.store._couple_context(db, DEMO_USER)
            draft = self.store._save_recipe_draft(db, couple["id"], DEMO_USER, value, "external", "spoonacular")

        class Headers:
            @staticmethod
            def get_content_type():
                return "image/png"

            @staticmethod
            def get(name, default=None):
                return str(len(VALID_PNG)) if name == "Content-Length" else default

        class Response:
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def geturl():
                return "https://img.spoonacular.com/recipes/123-556x370.png"

            @staticmethod
            def read(_limit):
                return VALID_PNG

        with patch("love_moments.meals.urlrequest.urlopen", return_value=Response()):
            imported = self.store.confirm_recipe_draft(draft["id"], {"visibility": "couple"}, DEMO_USER)
        self.assertTrue(imported["cover"]["url"].startswith("/api/v1/media/media_"))
        cover = next(item for item in imported["media"] if item["media_type"] == "image")
        self.assertEqual(cover["match_details"]["source_recipe_id"], "123")
        self.assertEqual(cover["provider"], "local")

    def test_custom_recipe_visibility_and_private_recipe_cannot_leak_into_shared_calendar(self) -> None:
        uploaded = self.store.save_media(VALID_PNG, "奶油蘑菇汤成品.png", "image/png", "shared", DEMO_USER)
        payload = {
            "name": "我们的奶油蘑菇汤", "cuisine": "western", "category": "西式汤品",
            "meal_slots": ["lunch", "dinner"], "base_servings": 2, "prep_minutes": 10,
            "cook_minutes": 20, "difficulty": "easy",
            "ingredients": [
                {"name": "蘑菇", "quantity": 300, "unit": "克", "preparation": "擦洗干净后切成薄片"},
                {"name": "牛奶", "quantity": 300, "unit": "毫升", "preparation": "提前量好并回温"},
            ],
            "steps": [
                {"title": "炒蘑菇", "instruction": "蘑菇切片后炒香。", "timer_seconds": 300, "heat_level": "中火", "key_point": "蘑菇变软且水汽基本蒸发。"},
                {"title": "煮至浓稠", "instruction": "加入牛奶小火煮至浓稠。", "timer_seconds": 600, "heat_level": "小火", "key_point": "汤汁能薄薄挂在勺背。"},
            ],
            "tips": ["不要大火煮沸。"], "allergen_tags": ["乳制品"], "visibility": "private",
            "media": [{
                "media_type": "image", "role": "cover",
                "url": uploaded["url"], "thumbnail_url": uploaded["thumbnail_url"],
                "title": "奶油蘑菇汤成品图", "provider": "local",
                "attribution": "测试中已核对", "match_status": "verified",
            }],
        }
        private = self.store.create_recipe(payload, DEMO_USER)
        private_detail = self.store.get_recipe(private["id"], DEMO_USER)
        self.assertEqual(private_detail["ingredients"][0]["ingredient_group"], "main")
        self.assertEqual(private_detail["steps"][0]["title"], "炒蘑菇")
        self.assertEqual(private_detail["steps"][0]["heat_level"], "中火")
        with self.assertRaises(KeyError):
            self.store.get_recipe(private["id"], DEMO_PARTNER)
        with self.assertRaises(PermissionError):
            self.store.add_meal_cart_item({**payload, "recipe_id": private["id"], "planned_date": self.tomorrow, "meal_slot": "dinner"}, DEMO_USER)
        shared = self.store.create_recipe({**payload, "name": "我们的蘑菇汤", "visibility": "couple"}, DEMO_USER)
        self.assertEqual(self.store.get_recipe(shared["id"], DEMO_PARTNER)["name"], "我们的蘑菇汤")
        updated = self.store.update_recipe(shared["id"], {
            **payload, "name": "我们的香浓蘑菇汤", "visibility": "couple", "expected_version": shared["version"],
            "replacement_cover": {
                "url": uploaded["url"], "thumbnail_url": uploaded["thumbnail_url"], "confirmed_exact": True,
            },
        }, DEMO_USER)
        self.assertEqual(updated["name"], "我们的香浓蘑菇汤")
        self.assertGreater(updated["version"], shared["version"])
        with self.assertRaises(PermissionError):
            self.store.update_recipe(updated["id"], {"expected_version": updated["version"]}, DEMO_PARTNER)

    def test_wish_and_calendar_are_shared_by_both_accounts(self) -> None:
        wish = self.store.create_meal_wish({"recipe_id": self.recipe["id"], "note": "周末一起做"}, DEMO_USER)
        self.assertEqual(self.store.list_meal_wishes(DEMO_PARTNER)[0]["id"], wish["id"])
        meal = self.add_meal(wish_id=wish["id"])
        self.assertEqual(self.store.list_meal_wishes(DEMO_PARTNER), [])
        accepted = self.store.update_meal_cart_item(meal["id"], {"status": "accepted", "expected_version": meal["version"]}, DEMO_PARTNER)
        day = self.store.meal_calendar(self.tomorrow, self.tomorrow, DEMO_USER)["days"][0]
        self.assertEqual(day["meals"]["dinner"][0]["status"], "accepted")
        self.assertEqual(accepted["cook_user_id"], DEMO_PARTNER)

    def test_free_text_wish_can_be_linked_to_recipe_and_added_by_partner(self) -> None:
        wish = self.store.create_meal_wish({"title": "煎牛排", "note": "见面以后做给我吃"}, DEMO_USER)
        self.assertIsNone(wish["recipe_id"])
        steak = self.store.list_recipes(DEMO_PARTNER, query="煎牛排", limit=5)["items"][0]
        linked = self.store.update_meal_wish(
            wish["id"], {"recipe_id": steak["id"], "expected_version": wish["version"]}, DEMO_PARTNER,
        )
        self.assertEqual(linked["recipe_id"], steak["id"])
        self.assertEqual(linked["recipe_name"], "香煎牛排")
        visible = self.store.list_meal_wishes(DEMO_USER)[0]
        self.assertEqual(visible["recipe_id"], steak["id"])
        meal = self.store.add_meal_cart_item({
            "recipe_id": steak["id"], "wish_id": wish["id"], "planned_date": self.tomorrow,
            "meal_slot": "dinner", "servings": 2, "idempotency_key": uuid.uuid4().hex,
        }, DEMO_USER)
        self.assertEqual(meal["recipe_id"], steak["id"])
        self.assertEqual(self.store.list_meal_wishes(DEMO_PARTNER), [])

    def test_wish_recipe_link_rejects_stale_version_and_private_recipe(self) -> None:
        wish = self.store.create_meal_wish({"title": "想吃一道新菜"}, DEMO_USER)
        linked = self.store.update_meal_wish(
            wish["id"], {"recipe_id": self.recipe["id"], "expected_version": wish["version"]}, DEMO_USER,
        )
        with self.assertRaises(MealConflictError):
            self.store.update_meal_wish(
                wish["id"], {"recipe_id": self.recipe["id"], "expected_version": wish["version"]}, DEMO_PARTNER,
            )
        self.assertGreater(linked["version"], wish["version"])

        detail = self.store.get_recipe(self.recipe["id"], DEMO_USER)
        uploaded = self.store.save_media(VALID_PNG, "私密菜谱.png", "image/png", "private", DEMO_USER)
        private = self.store.create_recipe({
            **detail,
            "id": None,
            "name": "只给自己看的菜谱",
            "dish_name": "只给自己看的菜谱",
            "visibility": "private",
            "media": [{
                "media_type": "image", "role": "cover", "url": uploaded["url"],
                "thumbnail_url": uploaded["thumbnail_url"], "provider": "local",
                "title": "私密菜谱成品图", "attribution": "测试中已核对", "match_status": "verified",
            }],
        }, DEMO_USER)
        second_wish = self.store.create_meal_wish({"title": "不能泄露私密菜谱"}, DEMO_USER)
        with self.assertRaises(PermissionError):
            self.store.update_meal_wish(
                second_wish["id"], {"recipe_id": private["id"], "expected_version": second_wish["version"]}, DEMO_USER,
            )

    def test_duplicate_and_stale_updates_are_explicit_conflicts(self) -> None:
        meal = self.add_meal()
        with self.assertRaises(MealConflictError) as duplicate:
            self.add_meal()
        self.assertEqual(duplicate.exception.code, "DUPLICATE_MEAL")
        updated = self.store.update_meal_cart_item(meal["id"], {"status": "accepted", "expected_version": meal["version"]}, DEMO_PARTNER)
        with self.assertRaises(MealConflictError):
            self.store.update_meal_cart_item(meal["id"], {"note": "旧页面修改", "expected_version": meal["version"]}, DEMO_USER)
        self.assertGreater(updated["version"], meal["version"])

    def test_shopping_preview_scales_merges_and_preserves_manual_lock(self) -> None:
        first = self.add_meal(servings=4)
        first = self.store.update_meal_cart_item(first["id"], {"status": "accepted", "expected_version": first["version"]}, DEMO_PARTNER)
        preview = self.store.preview_shopping_list({"range_start": self.tomorrow, "range_end": self.tomorrow}, DEMO_USER)
        shopping = self.store.apply_shopping_preview({"preview_id": preview["preview_id"], "idempotency_key": uuid.uuid4().hex}, DEMO_USER)
        self.assertGreater(len(shopping["items"]), 0)
        locked = shopping["items"][0]
        locked = self.store.update_shopping_item(locked["id"], {"quantity": 9, "unit": "个", "category": "other", "expected_version": locked["version"]}, DEMO_USER)
        self.assertTrue(locked["manual_locked"])
        self.assertEqual((locked["category_key"], locked["category_label"]), ("other", "其他"))
        checked = self.store.update_shopping_item(locked["id"], {"checked": True, "expected_version": locked["version"]}, DEMO_PARTNER)
        self.assertTrue(checked["checked"])
        locked = checked
        second_preview = self.store.preview_shopping_list({"range_start": self.tomorrow, "range_end": self.tomorrow, "expected_version": self.store.get_shopping_list(DEMO_USER)["version"]}, DEMO_PARTNER)
        self.assertTrue(second_preview["preview"]["diff"]["protected"])
        applied = self.store.apply_shopping_preview({"preview_id": second_preview["preview_id"], "idempotency_key": uuid.uuid4().hex}, DEMO_PARTNER)
        current = next(item for item in applied["items"] if item["id"] == locked["id"])
        self.assertEqual(current["quantity_milli"], locked["quantity_milli"])

    def test_shopping_preview_rejects_changed_meal_plan(self) -> None:
        meal = self.add_meal()
        meal = self.store.update_meal_cart_item(meal["id"], {"status": "accepted", "expected_version": meal["version"]}, DEMO_PARTNER)
        preview = self.store.preview_shopping_list({"range_start": self.tomorrow, "range_end": self.tomorrow}, DEMO_USER)
        self.store.update_meal_cart_item(meal["id"], {"note": "临时少盐", "expected_version": meal["version"]}, DEMO_PARTNER)
        with self.assertRaises(MealConflictError) as conflict:
            self.store.apply_shopping_preview({"preview_id": preview["preview_id"], "idempotency_key": uuid.uuid4().hex}, DEMO_USER)
        self.assertEqual(conflict.exception.code, "MEAL_PLAN_CHANGED")

    def test_archive_and_repeat_keep_original_date_clear(self) -> None:
        meal = self.add_meal()
        meal = self.store.update_meal_cart_item(meal["id"], {"status": "accepted", "expected_version": meal["version"]}, DEMO_PARTNER)
        preview = self.store.preview_shopping_list({"range_start": self.tomorrow, "range_end": self.tomorrow}, DEMO_USER)
        self.store.apply_shopping_preview({"preview_id": preview["preview_id"], "idempotency_key": uuid.uuid4().hex}, DEMO_USER)
        meal = next(item for item in self.store.get_meal_cart(DEMO_USER)["items"] if item["id"] == meal["id"])
        meal = self.store.update_meal_cart_item(meal["id"], {"status": "cooking", "expected_version": meal["version"]}, DEMO_USER)
        meal = self.store.update_meal_cart_item(meal["id"], {"status": "completed", "expected_version": meal["version"]}, DEMO_USER)
        another_day = (date.today() + timedelta(days=8)).isoformat()
        repeated = self.store.repeat_meal_item(meal["id"], {"planned_date": another_day, "idempotency_key": uuid.uuid4().hex}, DEMO_PARTNER)
        self.assertEqual(repeated["planned_date"], another_day)
        archive = self.store.meal_archive(date.fromisoformat(self.tomorrow).year, date.fromisoformat(self.tomorrow).month, DEMO_USER)
        original = next(day for day in archive["days"] if day["date"] == self.tomorrow)
        self.assertEqual(original["meals"]["dinner"][0]["status"], "completed")

    def test_cross_couple_cannot_read_or_change_meal_data(self) -> None:
        meal = self.add_meal()
        outsider = self.store.register("meal_outsider", "outside-pass-123", "外部用户")
        outsider_partner = self.store.register("meal_outsider_partner", "outside-pass-456", "外部伴侣")
        invite = self.store.create_user_invite(outsider["id"])
        self.store.accept_user_invite(outsider_partner["id"], invite["code"], "2024-01-01")
        self.assertEqual(self.store.get_meal_cart(outsider["id"])["items"], [])
        with self.assertRaises(KeyError):
            self.store.update_meal_cart_item(meal["id"], {"status": "accepted", "expected_version": meal["version"]}, outsider["id"])


if __name__ == "__main__":
    unittest.main()
