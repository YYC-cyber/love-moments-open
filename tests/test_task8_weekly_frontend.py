from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Task8WeeklyFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        cls.web = (ROOT / "static/app.js").read_text(encoding="utf-8")
        cls.client = (ROOT / "packages/domain-client/src/index.ts").read_text(encoding="utf-8")
        cls.mobile = (ROOT / "clients/mobile/App.tsx").read_text(encoding="utf-8")

    def test_h5_exposes_shared_weekly_draft_and_explicit_confirmation(self) -> None:
        self.assertIn('id="weeklyMealPlanPanel"', self.html)
        self.assertIn('data-action="create-weekly-meal-plan"', self.html)
        self.assertIn('function renderWeeklyMealPlan()', self.web)
        self.assertIn('confirm-weekly-meal-plan', self.web)
        self.assertIn('/api/v1/weekly-meal-plans/active', self.web)

    def test_domain_client_and_mobile_cover_weekly_plan_contract(self) -> None:
        self.assertIn('interface WeeklyMealPlanDraft', self.client)
        for name in ("activeWeeklyMealPlan", "createWeeklyMealPlan", "updateWeeklyMealPlanItem", "confirmWeeklyMealPlan", "cancelWeeklyMealPlan"):
            self.assertIn(name, self.client)
        self.assertIn("weeklyPlan", self.mobile)
        self.assertIn("confirmMobileWeeklyPlan", self.mobile)


if __name__ == "__main__":
    unittest.main()
