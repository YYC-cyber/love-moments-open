import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BeadMakerEnhancementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        cls.javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_large_physical_board_options_are_available(self):
        self.assertIn('<option value="58" selected>58 × 58 · 人像推荐</option>', self.html)
        self.assertIn('<option value="87">87 × 87 · 高清人像</option>', self.html)

    def test_ai_portrait_mode_prevents_low_resolution_defaults(self):
        self.assertIn('<option value="48" selected>48 色 · 人像推荐</option>', self.html)
        self.assertIn('if (Number(boardSize?.value || 0) < 58)', self.javascript)
        self.assertIn('if (Number(colorCount?.value || 0) < 48)', self.javascript)

    def test_expanded_colour_options_and_shades_are_available(self):
        self.assertIn('<option value="72">72 色 · 最大还原</option>', self.html)
        catalog = self.javascript.split("const BEAD_COLOR_CATALOG = [", 1)[1].split("].map", 1)[0]
        entries = re.findall(r'\["[A-Z]\d+",\s*"[^"]+",\s*"[0-9A-F]{6}",\s*[01]\]', catalog)
        self.assertGreaterEqual(len(entries), 90)
        for shade in ("深绯红", "淡珊瑚", "森林绿", "淡薄荷", "午夜蓝", "婴儿蓝", "深莓粉", "腮红粉"):
            self.assertIn(shade, catalog)

    def test_local_background_removal_creates_empty_cells(self):
        self.assertIn('id="beadBackgroundMode"', self.html)
        self.assertIn('id="beadBackgroundSensitivity"', self.html)
        self.assertIn("function removeBeadBackground", self.javascript)
        self.assertIn("index < 0 ? -1", self.javascript)
        self.assertIn("去除背景 ${empty} 格", self.javascript)

    def test_subject_tone_is_analysed_locally(self):
        self.assertIn('id="beadToneMode"', self.html)
        self.assertIn("主体整体提亮 · 推荐", self.html)
        self.assertIn("function adjustBeadTones", self.javascript)
        self.assertIn("if (median < 145)", self.javascript)
        self.assertIn('mode === "bright"', self.javascript)

    def test_ai_mode_is_optional_and_retryable(self):
        self.assertIn('id="beadGenerationMode"', self.html)
        self.assertIn("AI 精修 · 人像效果更好", self.html)
        self.assertIn('data-action="bead-ai-retry"', self.html)
        self.assertIn("requestAiBeadPreview", self.javascript)
        self.assertIn("X-Bead-Attempt", self.javascript)
        self.assertIn("每次重试会产生一次百炼图片费用", self.html)
        self.assertIn('id="beadAiVersionList"', self.html)
        self.assertIn("旧版会一直保留", self.html)
        self.assertIn('data-action="bead-ai-use"', self.javascript)
        self.assertIn('data-action="bead-ai-delete"', self.javascript)
        self.assertIn("loadBeadAiVersions", self.javascript)


if __name__ == "__main__":
    unittest.main()
