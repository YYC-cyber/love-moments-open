from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LifeAssistantFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "static/index.html").read_text(
            encoding="utf-8"
        )
        cls.script = (ROOT / "static/app.js").read_text(encoding="utf-8")
        cls.styles = (ROOT / "static/app.css").read_text(encoding="utf-8")

    def test_home_has_real_assistant_notification_and_plan_surfaces(self) -> None:
        for marker in (
            'id="lifeAssistantForm"',
            'id="lifeAssistantToday"',
            'id="lifeAssistantPriorities"',
            'id="assistantPlanModal"',
            'id="notificationCenterModal"',
        ):
            self.assertIn(marker, self.html)

    def test_frontend_calls_dedicated_assistant_and_notification_apis(self) -> None:
        for endpoint in (
            "/api/v1/assistant/today",
            "/api/v1/assistant/jobs",
            "/api/v1/assistant/plans",
            "/api/v1/notifications",
        ):
            self.assertIn(endpoint, self.script)
        self.assertIn("confirmLifeAssistantPlan", self.script)
        self.assertIn("renderNotificationCenter", self.script)
        self.assertIn("识别方式", self.script)
        self.assertIn("notificationDateGroup", self.script)

    def test_confirmation_does_not_call_domain_write_endpoints_directly(self) -> None:
        start = self.script.index("async function confirmLifeAssistantPlan")
        end = self.script.index("\n}", start) + 2
        body = self.script[start:end]
        self.assertIn("/confirm", body)
        self.assertNotIn("/api/v1/calendar", body)
        self.assertNotIn("/api/v1/meal-cart", body)
        self.assertNotIn("/api/v1/shopping-lists", body)

    def test_mobile_styles_use_fluid_widths(self) -> None:
        self.assertIn(".life-assistant-home", self.styles)
        self.assertIn(".assistant-plan-sheet", self.styles)
        self.assertNotIn(".life-assistant-home { width: 800px", self.styles)


if __name__ == "__main__":
    unittest.main()
