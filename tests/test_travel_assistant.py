from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from love_moments.travel_assistant import (
    OpenAICompatibleTravelAssistant,
    RuleBasedTravelAssistant,
    TravelAssistantIntent,
)


class TravelAssistantIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = RuleBasedTravelAssistant()
        self.context = {
            "destination": "上海迪士尼度假区",
            "confirmed_place": {"city": "上海市", "district": "浦东新区", "name": "上海迪士尼度假区"},
        }

    def test_hotel_query_extracts_budget_without_changing_confirmed_scope(self) -> None:
        intent = self.provider.interpret("帮我找每晚 600 元以内的酒店，离迪士尼近一点", self.context, [])
        self.assertEqual(intent.kind, "hotel_search")
        self.assertEqual(intent.max_price_cents, 60_000)
        self.assertEqual(intent.scope, "上海市·浦东新区·上海迪士尼度假区")

    def test_restaurant_query_extracts_day_meal_rating_and_distance(self) -> None:
        intent = self.provider.interpret("找第二天晚餐，4.5分以上，2公里内", self.context, [])
        self.assertEqual((intent.kind, intent.day_number, intent.meal_type), ("restaurant_search", 2, "dinner"))
        self.assertEqual(intent.min_rating, 4.5)
        self.assertEqual(intent.max_distance_meters, 2000)

    def test_plan_comparison_and_itinerary_change_are_distinct(self) -> None:
        self.assertEqual(self.provider.interpret("比较五套方案哪个更轻松", self.context, []).kind, "plan_compare")
        changed = self.provider.interpret("把第二天下午的行程改到15:30", self.context, [])
        self.assertEqual(changed.kind, "itinerary_change")
        self.assertEqual(changed.day_number, 2)
        self.assertEqual(changed.changes, {"start_time": "15:30"})

    def test_unknown_request_is_safe_general_intent(self) -> None:
        intent = self.provider.interpret("你觉得呢", self.context, [])
        self.assertEqual(intent.kind, "general")
        self.assertEqual(intent.scope, "上海市·浦东新区·上海迪士尼度假区")

    def test_live_trip_intents_are_distinct_and_delay_is_extracted(self) -> None:
        rainy = self.provider.interpret("第二天可能下雨，换成室内项目", self.context, [])
        delayed = self.provider.interpret("今天晚了 60 分钟，重新安排后面的内容", self.context, [])
        self.assertEqual((rainy.kind, rainy.day_number), ("day_replan", 2))
        self.assertEqual((delayed.kind, delayed.delay_minutes), ("day_replan", 60))
        self.assertEqual(self.provider.interpret("根据我现在的位置推荐下一站", self.context, []).kind, "next_stop")
        self.assertEqual(self.provider.interpret("统一比较酒店、机票和火车票", self.context, []).kind, "transport_compare")
        self.assertEqual(self.provider.interpret("旅行结束了，生成共同回忆", self.context, []).kind, "trip_recap")

    def test_intent_rejects_unknown_kind_and_untrusted_facts(self) -> None:
        with self.assertRaises(ValueError):
            TravelAssistantIntent.from_dict({"kind": "book_now", "hotel_name": "模型虚构酒店"}, self.context)


class OpenAICompatibleTravelAssistantTests(unittest.TestCase):
    def test_invalid_or_hostile_model_json_falls_back_to_rules(self) -> None:
        provider = OpenAICompatibleTravelAssistant("key", "https://example.test/v1", "model")
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": '{"kind":"book_now","hotel_name":"假酒店"}'}}]}).encode())
        response.__enter__ = lambda value: value
        response.__exit__ = lambda *args: None
        with patch("love_moments.travel_assistant.request.urlopen", return_value=response):
            intent = provider.interpret("找 500 元以内酒店", {"confirmed_place": {"city": "杭州"}}, [])
        self.assertEqual(intent.kind, "hotel_search")
        self.assertEqual(intent.max_price_cents, 50_000)

    def test_hotel_destination_resolver_returns_valid_international_query(self) -> None:
        operations = []
        provider = OpenAICompatibleTravelAssistant(
            "key", "https://api.deepseek.com/v1", "deepseek-chat", before_request=operations.append,
        )
        response = io.BytesIO(json.dumps({
            "choices": [{"message": {"content": '{"query":"Qiandao Lake, Hangzhou"}'}}],
        }).encode())
        response.__enter__ = lambda value: value
        response.__exit__ = lambda *args: None
        place = {"country": "中国", "province": "浙江省", "city": "杭州市", "district": "淳安县", "name": "千岛湖风景区"}
        with patch("love_moments.travel_assistant.request.urlopen", return_value=response) as opened:
            query = provider.resolve_hotel_destination(place, "杭州市 淳安县 千岛湖风景区")
        self.assertEqual(query, "Qiandao Lake, Hangzhou")
        self.assertEqual(operations, ["hotel destination resolution"])
        payload = json.loads(opened.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_hotel_destination_resolver_rejects_non_ascii_or_url_output(self) -> None:
        provider = OpenAICompatibleTravelAssistant("key", "https://example.test/v1", "model")
        for model_query in ("千岛湖", "https://example.com/hotel"):
            response = io.BytesIO(json.dumps({
                "choices": [{"message": {"content": json.dumps({"query": model_query})}}],
            }).encode())
            response.__enter__ = lambda value: value
            response.__exit__ = lambda *args: None
            with patch("love_moments.travel_assistant.request.urlopen", return_value=response):
                self.assertEqual(provider.resolve_hotel_destination({"name": "千岛湖"}, "千岛湖"), "")

    def test_hotel_name_localizer_returns_only_verified_chinese_names(self) -> None:
        operations = []
        provider = OpenAICompatibleTravelAssistant(
            "key", "https://api.deepseek.com/v1", "deepseek-chat", before_request=operations.append,
        )
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps({"names": [
            {"original": "Phoenix Residence Qiandao Lake", "zh_cn": "千岛湖凤凰公寓"},
            {"original": "Unknown Hotel", "zh_cn": "未知酒店"},
            {"original": "Novotel Qiandao Lake", "zh_cn": "https://example.com"},
        ]}, ensure_ascii=False)}}]}).encode())
        response.__enter__ = lambda value: value
        response.__exit__ = lambda *args: None
        names = ["Phoenix Residence Qiandao Lake", "Novotel Qiandao Lake"]
        with patch("love_moments.travel_assistant.request.urlopen", return_value=response):
            localized = provider.localize_hotel_names(names, {"country": "中国", "city": "杭州市", "name": "千岛湖风景区"})
        self.assertEqual(localized, {"Phoenix Residence Qiandao Lake": "千岛湖凤凰公寓"})
        self.assertEqual(operations, ["hotel name localization"])


if __name__ == "__main__":
    unittest.main()
