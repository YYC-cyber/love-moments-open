from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from love_moments.recipe_providers import (
    OnlineRecipeSearch,
    OpenAICompatibleRecipeGenerator,
    SpoonacularProvider,
    TheMealDbProvider,
    TianApiRecipeProvider,
    _finalize_external_recipe,
    _llm_base_url,
)
from love_moments.recipe_catalog_quality import allergen_report, inferred_allergens
from love_moments.store import LoveMomentsStore


class RecipeProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(Path(self.temp.name) / "provider.db", seed_demo_data=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_themealdb_cache_avoids_a_second_remote_request_and_quota_charge(self) -> None:
        response = {"meals": [{"idMeal": "1", "strMeal": "Tomato Pasta", "strCategory": "Pasta", "strArea": "Italian", "strIngredient1": "Tomato", "strMeasure1": "200 g", "strInstructions": "Cook it"}]}
        with patch("love_moments.recipe_providers._json_request", return_value=response) as remote:
            provider = TheMealDbProvider(self.store)
            self.assertEqual(provider.search("tomato", 5)[0]["external_id"], "1")
            self.assertEqual(provider.search("tomato", 5)[0]["external_id"], "1")
        self.assertEqual(remote.call_count, 1)
        with self.store.connection() as db:
            used = db.execute("SELECT used_count FROM external_api_usage WHERE provider='themealdb'").fetchone()[0]
        self.assertEqual(used, 1)

    def test_online_results_never_show_english_without_localization_model(self) -> None:
        provider = TheMealDbProvider(self.store)
        with patch.dict(os.environ, {"LLM_API_KEY": ""}, clear=False), patch.object(provider, "search", return_value=[{"name": "English Only"}]):
            result = OnlineRecipeSearch(provider, self.store).search_and_localize("番茄", 3)
        self.assertTrue(result["mock"])
        self.assertTrue(all(any("\u4e00" <= char <= "\u9fff" for char in item["name"]) for item in result["items"]))

    def test_invalid_ai_recipe_falls_back_to_editable_chinese_draft(self) -> None:
        generator = OpenAICompatibleRecipeGenerator("secret", "https://example.invalid/v1", "test", self.store.reserve_external_apis)
        with patch("love_moments.recipe_providers._json_request", return_value={"choices": []}):
            result = generator.generate({"prompt": "两人份少辣晚餐"})
        self.assertTrue(result["mock"])
        self.assertIn("两人份少辣晚餐", result["recipe"]["name"])
        self.assertGreaterEqual(len(result["recipe"]["steps"]), 2)

    def test_spoonacular_html_is_removed_before_localization(self) -> None:
        response = {"results": [{"id": 7, "title": "Soup", "summary": "<b>Rich</b> &amp; warm", "extendedIngredients": []}]}
        with patch("love_moments.recipe_providers._json_request", return_value=response):
            row = SpoonacularProvider(self.store, "secret").search("soup", 1)[0]
        self.assertEqual(row["instructions"], "Rich & warm")

    def test_documented_llm_base_url_is_used_for_recipe_localization(self) -> None:
        with patch.dict(os.environ, {"LLM_BASE_URL": "https://example.test/openai/v1", "LLM_API_URL": "https://old.invalid/v1"}, clear=False):
            self.assertEqual(_llm_base_url(), "https://example.test/openai/v1")

    def test_external_recipe_gets_deterministic_safety_storage_and_allergens(self) -> None:
        recipe = _finalize_external_recipe({
            "name": "香煎鸡胸配牛奶酱",
            "cuisine": "western",
            "category": "西式主菜",
            "meal_slots": ["lunch", "dinner"],
            "base_servings": 2,
            "prep_minutes": 10,
            "cook_minutes": 15,
            "passive_minutes": 0,
            "difficulty": "easy",
            "ingredients": [
                {"name": "鸡胸肉", "quantity": 300, "unit": "克", "group": "main", "preparation": "擦干后拍至厚薄均匀"},
                {"name": "牛奶", "quantity": 100, "unit": "毫升", "group": "main", "preparation": "提前量好"},
                {"name": "盐", "quantity": 2, "unit": "克", "group": "seasoning", "preparation": "提前称好"},
            ],
            "steps": [
                {"title": "调味", "instruction": "鸡胸肉加盐腌制。", "timer_seconds": 300, "heat_level": "无需加热", "key_point": "表面均匀沾盐。"},
                {"title": "煎熟", "instruction": "中火煎鸡胸肉并加入牛奶收汁。", "timer_seconds": 900, "heat_level": "中火", "key_point": "鸡胸肉最厚处达到 74℃。"},
            ],
            "tips": ["食品安全：模型给出的温度不可信。"],
            "allergen_tags": ["花生", "大豆"],
        })
        self.assertIn("乳制品", recipe["allergen_tags"])
        self.assertNotIn("花生", recipe["allergen_tags"])
        self.assertNotIn("大豆", recipe["allergen_tags"])
        dairy = next(item for item in recipe["allergen_details"] if item["name"] == "乳制品")
        self.assertEqual(dairy["sources"], ["牛奶"])
        self.assertTrue(any("室温" in tip and "2 小时" in tip for tip in recipe["tips"]))
        self.assertTrue(any("74℃" in tip for tip in recipe["tips"]))
        self.assertFalse(any("模型给出" in tip for tip in recipe["tips"]))

    def test_allergens_use_exact_ingredients_and_do_not_treat_every_bean_as_soy(self) -> None:
        ordinary_legumes = {"红豆", "绿豆", "四季豆", "豆角", "豌豆", "白芸豆"}
        self.assertNotIn("大豆", inferred_allergens(ordinary_legumes))

        report = allergen_report({"豆腐", "生抽", "蚝油", "椰奶", "燕麦片"})
        soy = next(item for item in report["allergens"] if item["name"] == "大豆")
        self.assertEqual(soy["sources"], ["生抽", "豆腐"])
        self.assertNotIn("乳制品", [item["name"] for item in report["allergens"]])
        self.assertIn("可能含麸质谷物", [item["name"] for item in report["allergens"]])
        self.assertEqual(report["review_ingredients"], ["生抽", "蚝油"])

    def test_external_english_ingredients_use_the_same_evidence_based_allergen_rules(self) -> None:
        report = allergen_report({
            "Cream", "Yogurt", "Soy Sauce", "Red Beans", "Sesame Oil",
            "Eggplant", "Milkfish", "Coconut Cream",
        })
        details = {item["name"]: item["sources"] for item in report["allergens"]}
        self.assertEqual(details["乳制品"], ["Cream", "Yogurt"])
        self.assertEqual(details["大豆"], ["Soy Sauce"])
        self.assertEqual(details["芝麻"], ["Sesame Oil"])
        self.assertNotIn("Red Beans", details["大豆"])
        self.assertNotIn("蛋类", details)
        self.assertNotIn("Milkfish", details["乳制品"])
        self.assertEqual(details["鱼类"], ["Milkfish"])
        self.assertEqual(report["review_ingredients"], ["Soy Sauce"])

    def test_external_recipe_rejects_excess_concentrated_spice(self) -> None:
        payload = {
            "name": "肉桂烤苹果", "cuisine": "western", "category": "甜品", "meal_slots": ["snack"],
            "base_servings": 2, "prep_minutes": 10, "cook_minutes": 20, "difficulty": "easy",
            "ingredients": [
                {"name": "苹果", "quantity": 400, "unit": "克", "group": "main", "preparation": "去核切块"},
                {"name": "肉桂粉", "quantity": 100, "unit": "克", "group": "seasoning", "preparation": "提前称好"},
            ],
            "steps": [
                {"title": "拌匀", "instruction": "苹果与肉桂粉拌匀。", "timer_seconds": 120, "heat_level": "无需加热", "key_point": "表面均匀。"},
                {"title": "烤熟", "instruction": "放入烤箱烤至苹果变软。", "timer_seconds": 1200, "heat_level": "烤箱180℃", "key_point": "苹果软熟。"},
            ],
        }
        with self.assertRaisesRegex(ValueError, "肉桂粉"):
            _finalize_external_recipe(payload)

    def test_external_localization_retries_one_empty_json_response(self) -> None:
        localized = {
            "recipe": {
                "name": "番茄意大利面", "cuisine": "western", "category": "意面", "meal_slots": ["lunch", "dinner"],
                "base_servings": 2, "prep_minutes": 10, "cook_minutes": 15, "passive_minutes": 0, "difficulty": "easy",
                "ingredients": [
                    {"name": "意大利面", "quantity": 200, "unit": "克", "group": "main", "preparation": "按包装时间煮制"},
                    {"name": "番茄", "quantity": 300, "unit": "克", "group": "main", "preparation": "洗净切丁"},
                    {"name": "盐", "quantity": 2, "unit": "克", "group": "seasoning", "preparation": "提前称好"},
                ],
                "steps": [
                    {"title": "煮面", "instruction": "意大利面放入沸水煮熟。", "timer_seconds": 600, "heat_level": "中火", "key_point": "面条无生硬芯。"},
                    {"title": "炒酱", "instruction": "番茄与盐中火炒软，再与意大利面拌匀。", "timer_seconds": 600, "heat_level": "中火", "key_point": "酱汁均匀附着面条。"},
                ],
                "tips": [], "allergen_tags": [],
            },
        }
        responses = [
            {"choices": [{"message": {"content": ""}}]},
            {"choices": [{"message": {"content": __import__("json").dumps(localized, ensure_ascii=False)}}]},
        ]
        provider = TheMealDbProvider(self.store)
        with patch.dict(os.environ, {"LLM_BASE_URL": "https://example.test/v1"}, clear=False), patch(
            "love_moments.recipe_providers._json_request", side_effect=responses,
        ) as remote:
            items = OnlineRecipeSearch(provider, self.store)._localize_many([{"name": "Tomato Pasta"}], "secret")
        self.assertEqual(items[0]["name"], "番茄意大利面")
        self.assertEqual(remote.call_count, 2)
        self.assertIn("response_format", remote.call_args_list[0].kwargs["data"])
        self.assertNotIn("response_format", remote.call_args_list[1].kwargs["data"])

    def test_tianapi_recipe_result_keeps_chinese_content_and_uses_cache(self) -> None:
        response = {
            "code": 200,
            "msg": "success",
            "result": {"list": [{
                "id": 4836,
                "cp_name": "蓑衣黄瓜",
                "type_name": "凉菜类",
                "yuanliao": "黄瓜250克；胡萝卜25克",
                "tiaoliao": "香醋3克；盐9克；白糖1.5克",
                "zuofa": "黄瓜洗净切花刀，腌制后淋汁。",
                "tishi": "料汁放凉后再使用。",
            }]},
        }
        with patch("love_moments.recipe_providers._json_request", return_value=response) as remote:
            provider = TianApiRecipeProvider(self.store, "secret")
            first = provider.search("黄瓜", 3)
            second = provider.search("黄瓜", 3)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["name"], "蓑衣黄瓜")
        self.assertEqual(first[0]["ingredients"][2]["group"], "seasoning")
        self.assertEqual(remote.call_count, 1)
        with self.store.connection() as db:
            used = db.execute("SELECT used_count FROM external_api_usage WHERE provider='tianapi_recipe'").fetchone()[0]
        self.assertEqual(used, 1)


if __name__ == "__main__":
    unittest.main()
