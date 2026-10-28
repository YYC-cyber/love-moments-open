from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from urllib.error import URLError

from love_moments.providers import LocalDatePlanner, LocalTravelPlanner, OpenAICompatibleDatePlanner, OpenAICompatibleTravelPlanner


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = OpenAICompatibleTravelPlanner("secret", "https://example.test/v1", "test-model", 5)

    def model_payload(self) -> dict:
        plans = []
        for style in ("slow", "classic", "value", "food", "nature"):
            items = []
            for day_number in range(1, 4):
                for index, start_time in enumerate(("09:00", "14:00"), 1):
                    items.append({
                        "day_number": day_number,
                        "start_time": start_time,
                        "title": f"{style}第{day_number}天地点{index}",
                        "duration_minutes": 60,
                        "cost_cny": 10,
                        "details": {
                            "activities": ["参加主题体验", "在指定区域拍照"],
                            "highlights": ["路线衔接顺畅", "适合两个人共同体验"],
                            "recommendation_reason": "与当天主题相符，也符合情侣同行的节奏。",
                            "practical_tips": ["出发前确认营业状态"],
                            "best_time": "上午",
                            "reservation_note": "以官方预约页为准",
                        },
                    })
            plans.append({
                "style": style,
                "name": style,
                "summary": "结构化方案",
                "budget_cny": 1000,
                "intensity": "适中",
                "transport_minutes": 60,
                "day_themes": [{"day_number": day, "theme": f"{style}主题{day}"} for day in range(1, 4)],
                "items": items,
            })
        content = json.dumps({"plans": plans}, ensure_ascii=False)
        return {"choices": [{"message": {"content": content}}]}

    def single_model_payload(self, style: str) -> dict:
        all_plans = json.loads(self.model_payload()["choices"][0]["message"]["content"])["plans"]
        selected = next(plan for plan in all_plans if plan["style"] == style)
        content = json.dumps({"plans": [selected]}, ensure_ascii=False)
        return {"choices": [{"message": {"content": content}}]}

    def test_openai_compatible_provider_parses_structured_response(self) -> None:
        with patch("love_moments.providers.request.urlopen", return_value=FakeResponse(self.model_payload())) as mocked:
            plans = self.planner.generate_variants("杭州", 300000)
        self.assertEqual([plan.style for plan in plans], ["slow", "classic", "value", "food", "nature"])
        self.assertTrue(all(plan.generation_source == "ai" for plan in plans))
        sent_request = mocked.call_args.args[0]
        self.assertEqual(sent_request.full_url, "https://example.test/v1/chat/completions")
        self.assertEqual(sent_request.headers["Authorization"], "Bearer secret")

    def test_provider_network_failure_uses_local_fallback(self) -> None:
        with patch("love_moments.providers.request.urlopen", side_effect=URLError("offline")):
            plans = self.planner.generate_variants("杭州", 300000)
        self.assertEqual(len(plans), 5)
        self.assertEqual(plans[0].style, "slow")
        self.assertEqual(plans[0].generation_source, "rules")
        self.assertIn("超时", plans[0].generation_message)

    def test_deepseek_structured_travel_disables_thinking(self) -> None:
        planner = OpenAICompatibleTravelPlanner("secret", "https://api.deepseek.com", "deepseek-flash", 5)
        with patch("love_moments.providers.request.urlopen", return_value=FakeResponse(self.model_payload())) as mocked:
            plans = planner.generate_variants("上海迪士尼", 300000)
        sent = json.loads(mocked.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(sent["thinking"], {"type": "disabled"})
        self.assertTrue(all(plan.generation_source == "ai" for plan in plans))

    def test_travel_prompt_requires_distinct_executable_days(self) -> None:
        with patch("love_moments.providers.request.urlopen", return_value=FakeResponse(self.model_payload())) as mocked:
            self.planner.generate_variants("上海迪士尼", 300000, {"departure_city": "杭州", "arrival_city": "上海迪士尼"})
        sent = json.loads(mocked.call_args.args[0].data.decode("utf-8"))
        system = sent["messages"][0]["content"]
        user = json.loads(sent["messages"][1]["content"])
        self.assertIn("旅行规划总监", system)
        self.assertIn("同一地点不能跨天重复", system)
        self.assertEqual(user["logistics"]["departure_city"], "杭州")
        self.assertTrue(any("五套方案必须体现主题差异" in rule for rule in user["planning_rules"]))
        self.assertTrue(any("可点开的详情" in rule for rule in user["planning_rules"]))
        self.assertIn("recommendation_reason", user["output"]["plans"][0]["items"][0]["details"])

    def test_travel_contract_requires_concrete_clickable_details(self) -> None:
        payload = json.loads(self.model_payload()["choices"][0]["message"]["content"])
        payload["plans"][0]["items"][0]["details"]["activities"] = []
        with self.assertRaisesRegex(ValueError, "详情不完整"):
            self.planner._validate(payload, 3)

        plans = self.planner._validate(json.loads(self.model_payload()["choices"][0]["message"]["content"]), 3)
        detail = plans[0].items[0]["details"]
        self.assertEqual(len(detail["activities"]), 2)
        self.assertIn("情侣", detail["recommendation_reason"])

    def test_confirmed_destination_scope_rejects_foreign_same_name_place(self) -> None:
        payload = json.loads(self.model_payload()["choices"][0]["message"]["content"])
        for plan in payload["plans"]:
            for item in plan["items"]:
                item["details"]["location_scope"] = "上海市浦东新区"
        place = {"name": "上海迪士尼度假区", "country": "中国", "province": "上海市", "city": "上海市", "district": "浦东新区"}
        plans = self.planner._validate(payload, 3, destination_place=place)
        self.assertEqual(plans[0].items[0]["details"]["location_scope"], "上海市浦东新区")
        payload["plans"][0]["items"][0]["details"]["location_scope"] = "美国佛罗里达州奥兰多"
        with self.assertRaisesRegex(ValueError, "未归属于"):
            self.planner._validate(payload, 3, destination_place=place)

    def test_travel_contract_rejects_repeated_places_across_days(self) -> None:
        payload = json.loads(self.model_payload()["choices"][0]["message"]["content"])
        for plan in payload["plans"]:
            if plan["style"] != "food":
                plan["items"][2]["title"] = plan["items"][0]["title"]
        with self.assertRaises(ValueError):
            self.planner._validate(payload, 3)

    def test_local_travel_fallback_never_invents_hangzhou_places_for_other_destinations(self) -> None:
        plans = LocalTravelPlanner().generate_variants(
            "望仙谷",
            300000,
            {"start_date": "2026-11-01", "end_date": "2026-11-03"},
        )
        titles = [item["title"] for plan in plans for item in plan.items]
        self.assertTrue(titles)
        self.assertFalse(any(name in title for title in titles for name in ("断桥", "灵隐寺", "雷峰塔", "北山街")))
        self.assertEqual({item["day_number"] for item in plans[0].items}, {1, 2, 3})

    def test_invalid_model_json_uses_local_fallback(self) -> None:
        invalid = {"choices": [{"message": {"content": "not-json"}}]}
        with patch("love_moments.providers.request.urlopen", return_value=FakeResponse(invalid)):
            plans = self.planner.generate_variants("杭州", 300000)
        self.assertEqual(len(plans), 5)

    def test_invalid_model_json_recovers_with_one_request_per_style(self) -> None:
        invalid = {"choices": [{"message": {"content": "{\"plans\":["}}]}
        styles = ["slow", "classic", "value", "food", "nature"]
        with patch(
            "love_moments.providers.request.urlopen",
            side_effect=[FakeResponse(invalid)] + [FakeResponse(self.single_model_payload(style)) for style in styles],
        ) as mocked:
            plans = self.planner.generate_variants(
                "杭州", 300000, {"start_date": "2026-11-01", "end_date": "2026-11-03"},
            )
        self.assertEqual(mocked.call_count, 6)
        self.assertTrue(all(plan.generation_source == "ai" for plan in plans))
        self.assertIn("分套生成", plans[0].generation_message)

    def test_single_style_recovery_accepts_full_or_unwrapped_model_envelope(self) -> None:
        payload = json.loads(self.model_payload()["choices"][0]["message"]["content"])
        selected = self.planner._validate(payload, 3, expected_styles=["food"])
        self.assertEqual([plan.style for plan in selected], ["food"])

        food_row = next(row for row in payload["plans"] if row["style"] == "food")
        unwrapped = self.planner._validate(food_row, 3, expected_styles=["food"])
        self.assertEqual([plan.style for plan in unwrapped], ["food"])

    def test_single_style_recovery_retries_only_invalid_style(self) -> None:
        invalid = FakeResponse({"choices": [{"message": {"content": "{not-json"}}]})
        styles = ["slow", "classic", "value", "food", "nature"]
        responses = [
            invalid,
            FakeResponse(self.single_model_payload("slow")),
            *[FakeResponse(self.single_model_payload(style)) for style in styles[1:]],
        ]
        with patch("love_moments.providers.request.urlopen", side_effect=responses) as mocked:
            plans = self.planner._generate_style_variants(
                "杭州", 300000,
                {"start_date": "2026-11-01", "end_date": "2026-11-03"},
                3,
                {"style": "slow", "items": []},
            )
        self.assertEqual(mocked.call_count, 6)
        self.assertEqual([plan.style for plan in plans], styles)

    def test_local_date_planner_always_returns_five_distinct_variants(self) -> None:
        plans = LocalDatePlanner().generate_variants({"city": "杭州", "budget_cents": 50000, "start_time": "14:00"})
        self.assertEqual([plan["style"] for plan in plans], ["romantic", "classic", "value", "food", "adventure"])
        self.assertTrue(all(2 <= len(plan["items"]) <= 6 for plan in plans))

    def test_date_model_contract_rejects_time_reversal(self) -> None:
        rows = []
        for style in ("romantic", "classic", "value"):
            rows.append({
                "style": style,
                "name": style,
                "summary": "测试",
                "expected_cost_cny": 300,
                "items": [
                    {"start_time": "18:00", "title": "晚餐"},
                    {"start_time": "16:00", "title": "散步"},
                ],
            })
        with self.assertRaises(ValueError):
            OpenAICompatibleDatePlanner._validate({"plans": rows}, 50000)

    def test_local_date_planner_respects_window_busy_time_and_marks_provenance(self) -> None:
        plans = LocalDatePlanner().generate_variants({
            "city": "杭州", "budget_cents": 50000, "start_time": "13:00", "end_time": "20:00",
            "busy_intervals": [{"start": "14:00", "end": "16:00", "all_day": False}],
            "place_candidates": [{
                "candidate_id": "candidate-1", "provider": "amap", "provider_place_id": "poi-1",
                "name": "西湖景区", "formatted_address": "杭州市西湖区", "latitude": 30.2,
                "longitude": 120.1, "source_type": "real_api", "evidence_at": "2026-10-27T10:00:00+00:00",
            }],
            "weather": {"source_type": "real_api", "provider": "Open-Meteo", "evidence_at": "2026-10-27T10:00:00+00:00"},
        })
        self.assertEqual([plan["style"] for plan in plans], ["romantic", "classic", "value", "food", "adventure"])
        for plan in plans:
            self.assertEqual(plan["generation_source"], "rules")
            self.assertEqual(len(plan["items"]), 2)
            self.assertLessEqual(plan["expected_cost_cents"], 50000)
            for item in plan["items"]:
                self.assertIn(item["recommendationSource"], {"rules", "ai"})
                self.assertEqual(item["priceSource"], "rules")
                self.assertEqual(item["weatherSource"], "real_api")
                start = int(item["startTime"][:2]) * 60 + int(item["startTime"][3:])
                end = start + item["durationMinutes"]
                self.assertGreaterEqual(start, 13 * 60)
                self.assertLessEqual(end, 20 * 60)
                self.assertFalse(start < 16 * 60 and end > 14 * 60)

    def test_date_model_rejects_unverified_places_and_server_overrides_source_claims(self) -> None:
        rows = []
        for style in ("romantic", "classic", "value", "food", "adventure"):
            rows.append({
                "style": style, "name": style, "summary": "测试", "expected_cost_cny": 200,
                "intensity": "适中", "weather_fit": "以实时天气为准", "items": [
                    {"start_time": "16:00", "duration_minutes": 60, "title": "西湖景区",
                     "place_candidate_id": "candidate-1", "cost_cny": 50,
                     "recommendationSource": "real_api", "placeSource": "real_api"},
                    {"start_time": "18:00", "duration_minutes": 60, "title": "地点待选择",
                     "cost_cny": 50},
                ],
            })
        context = {
            "start_time": "13:00", "end_time": "20:00", "busy_intervals": [{"start": "14:00", "end": "16:00"}],
            "place_candidates": [{"candidate_id": "candidate-1", "provider": "amap", "provider_place_id": "poi-1", "name": "西湖景区", "formatted_address": "杭州", "source_type": "real_api", "evidence_at": "2026-10-27T10:00:00+00:00"}],
            "weather": {"source_type": "unavailable"},
        }
        plans = OpenAICompatibleDatePlanner._validate({"plans": rows}, 50000, context)
        first = plans[0]["items"][0]
        self.assertEqual(first["recommendationSource"], "ai")
        self.assertEqual(first["placeSource"], "real_api")
        self.assertEqual(first["provider"], "amap")
        self.assertEqual(first["priceSource"], "rules")
        self.assertEqual(first["weatherSource"], "unavailable")

        rows[0]["items"][0]["place_candidate_id"] = "forged-place"
        with self.assertRaisesRegex(ValueError, "地点候选"):
            OpenAICompatibleDatePlanner._validate({"plans": rows}, 50000, context)

        rows[0]["items"][0]["place_candidate_id"] = "candidate-1"
        rows[0]["items"][1].update({
            "title": "西湖景区", "place_candidate_id": "candidate-1",
        })
        with self.assertRaisesRegex(ValueError, "重复安排"):
            OpenAICompatibleDatePlanner._validate({"plans": rows}, 50000, context)

    def test_date_model_rejects_two_candidate_ids_for_the_same_physical_place(self) -> None:
        rows = []
        for style in ("romantic", "classic", "value", "food", "adventure"):
            rows.append({
                "style": style, "name": style, "summary": "测试", "expected_cost_cny": 200,
                "intensity": "适中", "weather_fit": "请确认天气", "items": [
                    {"start_time": "14:00", "duration_minutes": 60, "title": "候选一", "place_candidate_id": "candidate-1", "cost_cny": 50},
                    {"start_time": "16:00", "duration_minutes": 60, "title": "候选二", "place_candidate_id": "candidate-2", "cost_cny": 50},
                ],
            })
        context = {
            "start_time": "13:00", "end_time": "20:00", "busy_intervals": [],
            "place_candidates": [
                {"candidate_id": "candidate-1", "provider": "amap", "provider_place_id": "same-poi", "name": "同一家餐厅", "formatted_address": "杭州市湖滨", "latitude": 30.25, "longitude": 120.16, "source_type": "real_api"},
                {"candidate_id": "candidate-2", "provider": "amap", "provider_place_id": "same-poi", "name": "同一家餐厅分词标题", "formatted_address": "杭州市湖滨", "latitude": 30.250001, "longitude": 120.160001, "source_type": "cache"},
            ],
            "weather": {"source_type": "unavailable"},
        }
        with self.assertRaisesRegex(ValueError, "重复安排"):
            OpenAICompatibleDatePlanner._validate({"plans": rows}, 50000, context)

    def test_date_model_named_venue_without_server_candidate_falls_back_honestly(self) -> None:
        rows = []
        for style in ("romantic", "classic", "value", "food", "adventure"):
            rows.append({
                "style": style, "name": style, "summary": "测试", "expected_cost_cny": 200,
                "intensity": "适中", "weather_fit": "请确认天气", "items": [
                    {"start_time": "14:00", "duration_minutes": 60,
                     "title": "模型虚构的网红餐厅", "cost_cny": 50},
                    {"start_time": "16:00", "duration_minutes": 60,
                     "title": "附近散步（地点待选择）", "cost_cny": 20},
                ],
            })
        model_response = {"choices": [{"message": {"content": json.dumps({"plans": rows}, ensure_ascii=False)}}]}
        planner = OpenAICompatibleDatePlanner("secret", "https://example.test/v1", "test-model", 5)
        with patch("love_moments.providers.request.urlopen", return_value=FakeResponse(model_response)):
            plans = planner.generate_variants({
                "city": "杭州", "budget_cents": 50000,
                "start_time": "13:00", "end_time": "20:00",
                "place_candidates": [], "busy_intervals": [],
                "weather": {"source_type": "unavailable"},
            })
        self.assertTrue(all(plan["generation_source"] == "rules" for plan in plans))
        self.assertTrue(all("模型" in plan["generation_message"] for plan in plans))
        for plan in plans:
            for item in plan["items"]:
                self.assertNotEqual(item["title"], "模型虚构的网红餐厅")
                if not item.get("placeCandidateId"):
                    self.assertIn("待选择", item["title"])


if __name__ == "__main__":
    unittest.main()
