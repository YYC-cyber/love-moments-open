from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from love_moments.intelligent_ui import OpenAICompatibleUiComposer
from love_moments.life_assistant import OpenAICompatibleLifeAssistant
from love_moments.providers import OpenAICompatibleDatePlanner, OpenAICompatibleTravelPlanner


class _ModelHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *_args) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append({"path": self.path, "authorization": self.headers.get("Authorization"), "payload": payload})
        system = payload["messages"][0]["content"]
        if "双人生活助理" in system:
            content = {
                "requested_date": "2099-10-16",
                "meal_slot": "dinner",
                "planned_time": "19:15",
                "dish_query": "番茄炒蛋",
                "task_query": "",
                "add_calendar": True,
                "add_shopping": True,
                "add_todo": False,
            }
        elif "界面编排器" in system:
            content = {"components": ["budget_summary", "trip_variant_compare", "approval_state"]}
        elif "约会规划器" in system:
            content = {"plans": [
                {
                    "style": style,
                    "name": name,
                    "summary": "模型约会方案",
                    "expected_cost_cny": 360,
                    "intensity": "适中",
                    "weather_fit": "雨天可切换室内",
                    "items": [
                        {"start_time": "14:00", "title": "看展", "cost_cny": 80, "indoor": True},
                        {"start_time": "18:00", "title": "晚餐", "cost_cny": 280, "needs_reservation": True},
                    ],
                }
                for style, name in (("romantic", "轻松浪漫"), ("classic", "经典充实"), ("value", "预算友好"), ("food", "美食约会"), ("adventure", "新鲜体验"))
            ]}
        else:
            content = {"plans": [
                {
                    "style": style,
                    "name": name,
                    "summary": "模型旅行方案",
                    "budget_cny": 1200,
                    "intensity": "适中",
                    "transport_minutes": 70,
                    "day_themes": [{"day_number": day, "theme": f"{name}主题{day}"} for day in range(1, 4)],
                    "items": [
                        {"day_number": day, "start_time": time, "title": f"{name}第{day}天地点{index}", "duration_minutes": 60, "cost_cny": 80, "details": {"activities": ["体验主题项目", "拍摄双人照片"], "highlights": ["当地特色", "适合情侣"], "recommendation_reason": "与当天路线相邻，适合两个人共同体验。", "practical_tips": ["确认开放时间"], "best_time": "上午", "reservation_note": "按官方要求预约"}}
                        for day in range(1, 4)
                        for index, time in enumerate(("09:00", "14:00"), 1)
                    ],
                }
                for style, name in (("slow", "松弛浪漫"), ("classic", "经典充实"), ("value", "性价比"), ("food", "美食寻味"), ("nature", "自然治愈"))
            ]}
        body = json.dumps({"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class OpenAICompatibleIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _ModelHandler.requests.clear()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_travel_date_and_ui_use_real_openai_compatible_http_contract(self) -> None:
        travel = OpenAICompatibleTravelPlanner("test-key", self.base_url, "test-model", 5)
        trip_plans = travel.generate_variants("三亚", 500000)
        self.assertEqual([plan.style for plan in trip_plans], ["slow", "classic", "value", "food", "nature"])
        self.assertTrue(all(len(plan.items) == 6 for plan in trip_plans))

        date = OpenAICompatibleDatePlanner("test-key", self.base_url, "test-model", 5)
        date_plans = date.generate_variants({"city": "杭州", "budget_cents": 50000, "start_time": "14:00"})
        self.assertEqual([plan["style"] for plan in date_plans], ["romantic", "classic", "value", "food", "adventure"])

        ui = OpenAICompatibleUiComposer("test-key", self.base_url, "test-model", 5)
        components = ui.compose("trip", {"budget": 5000}, "优先比较预算和待确认事项")
        self.assertEqual(components, ["budget_summary", "trip_variant_compare", "approval_state"])

        life = OpenAICompatibleLifeAssistant("test-key", self.base_url, "test-model", 5)
        life_intent = life.parse(
            "2099年10月16日晚上想吃番茄炒蛋，加入日历并生成采购清单",
            timezone_name="Asia/Shanghai",
        )
        self.assertEqual(life_intent.planned_time, "19:15")
        self.assertEqual(life_intent.dish_query, "番茄炒蛋")

        self.assertEqual(len(_ModelHandler.requests), 4)
        self.assertTrue(all(item["path"] == "/v1/chat/completions" for item in _ModelHandler.requests))
        self.assertTrue(all(item["authorization"] == "Bearer test-key" for item in _ModelHandler.requests))
        self.assertTrue(all(item["payload"]["response_format"] == {"type": "json_object"} for item in _ModelHandler.requests))


if __name__ == "__main__":
    unittest.main()
