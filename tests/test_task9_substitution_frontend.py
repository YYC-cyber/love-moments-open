from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class IngredientSubstitutionFrontendContractTests(unittest.TestCase):
    def test_h5_has_review_before_apply_flow_and_source_labels(self) -> None:
        html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")
        self.assertIn('id="ingredientSubstitutionModal"', html)
        self.assertIn('id="ingredientSubstitutionContent"', html)
        self.assertIn('data-action="open-ingredient-substitutions"', app)
        self.assertIn('data-action="preview-ingredient-substitutions"', app)
        self.assertIn('data-action="apply-ingredient-substitution"', app)
        self.assertIn('data-action="dismiss-ingredient-substitution"', app)
        self.assertIn("本地校准规则", app)
        self.assertIn("当前配料表", app)
        self.assertIn("can_apply", app)
        self.assertIn("item.shopping_preview", app)
        self.assertIn("采购变化", app)

    def test_mobile_exposes_minimal_review_entry_without_claiming_safety(self) -> None:
        mobile = (ROOT / "clients/mobile/App.tsx").read_text(encoding="utf-8")
        self.assertIn("食材替代建议", mobile)
        self.assertIn("先核对比例、做法变化与双方饮食资料", mobile)
        self.assertNotIn("保证不过敏", mobile)
        self.assertNotIn("绝对安全", mobile)


if __name__ == "__main__":
    unittest.main()
