from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Task7KitchenFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        cls.web = (ROOT / "static/app.js").read_text(encoding="utf-8")
        cls.client = (ROOT / "packages/domain-client/src/index.ts").read_text(encoding="utf-8")
        cls.mobile = (ROOT / "clients/mobile/App.tsx").read_text(encoding="utf-8")

    def test_h5_exposes_structured_profiles_pantry_and_explicit_stocking(self) -> None:
        self.assertIn('id="foodProfilePanel"', self.html)
        self.assertIn('id="foodAllergenAvoid"', self.html)
        self.assertIn('id="foodAllergenSevere"', self.html)
        self.assertNotIn('id="foodAllergySeverity"', self.html)
        self.assertIn('id="pantryPanel"', self.html)
        self.assertIn('data-action="stock-shopping-item"', self.web)
        self.assertIn('stock_checked_item_ids', self.web)
        self.assertIn('/api/v1/shopping-category-order', self.web)
        self.assertRegex(
            self.web,
            r"async function loadMeals[\s\S]*?requestUserId[\s\S]*?requestPartition[\s\S]*?state\.data\?\.user\?\.id !== requestUserId[\s\S]*?state\.meals =",
        )
        self.assertNotIn('placeholder="例如临时分类"', self.html)

    def test_domain_client_and_mobile_cover_kitchen_contract(self) -> None:
        for contract in ("FoodProfile", "PantryItem"):
            self.assertIn(f"interface {contract}", self.client)
        self.assertIn("type ShoppingCategoryKey", self.client)
        for method in ("foodProfiles", "updateMyFoodProfile", "pantryItems", "upsertPantryItem", "stockShoppingItem", "shoppingCategoryOrder"):
            self.assertIn(method, self.client)
        self.assertIn("pantryItems", self.mobile)
        self.assertIn("foodProfiles", self.mobile)
        self.assertIn("stockShoppingItem", self.mobile)


if __name__ == "__main__":
    unittest.main()
