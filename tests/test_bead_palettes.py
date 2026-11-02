import json
import tempfile
import unittest
from pathlib import Path

from love_moments.bead_palettes import BeadPaletteError, BeadPaletteRepository


ROOT = Path(__file__).resolve().parents[1]


class BeadPaletteRepositoryTests(unittest.TestCase):
    def test_bundled_domestic_5mm_catalogue_is_valid_and_default(self):
        repository = BeadPaletteRepository(ROOT / "love_moments" / "data" / "bead_palettes")
        manifest = repository.list()
        self.assertEqual(manifest["default_id"], "domestic-common-5mm-v1")
        item = manifest["items"][0]
        self.assertEqual(item["bead_size_mm"], 5.0)
        self.assertGreaterEqual(item["color_count"], 90)
        self.assertFalse(item["verified"])
        palette = repository.get(item["id"])
        self.assertEqual(len({color["code"] for color in palette["colors"]}), len(palette["colors"]))

    def test_operator_file_can_add_a_catalogue_without_code_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            colors = [
                {"code": f"T{index:02d}", "name": f"测试色{index}", "hex": f"#{index:02X}3344"}
                for index in range(8)
            ]
            (root / "custom.json").write_text(json.dumps({
                "id": "custom-2.6mm", "name": "自定义小豆", "brand": "我的品牌", "series": "2.6mm 小豆",
                "version": "1", "bead_size_mm": 2.6, "verified": True, "color_value_kind": "measured_physical",
                "colors": colors,
            }, ensure_ascii=False), encoding="utf-8")
            repository = BeadPaletteRepository(ROOT / "love_moments" / "data" / "bead_palettes", root)
            custom = repository.get("custom-2.6mm")
            self.assertEqual(custom["brand"], "我的品牌")
            self.assertTrue(custom["verified"])

    def test_invalid_duplicate_codes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            colors = [{"code": "A1", "name": "重复", "hex": "#123456"}] * 8
            (root / "bad.json").write_text(json.dumps({
                "id": "bad-palette", "name": "错误色卡", "brand": "测试", "version": "1",
                "bead_size_mm": 5, "colors": colors,
            }, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(BeadPaletteError):
                BeadPaletteRepository(root).list()


if __name__ == "__main__":
    unittest.main()
