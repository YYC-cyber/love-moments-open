import json
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

    def test_simple_one_click_flow_generates_three_local_variants(self):
        self.assertIn('id="beadPurpose"', self.html)
        self.assertIn('id="beadAdvancedSettings"', self.html)
        self.assertIn('高级设置（一般不需要调整）', self.html)
        self.assertIn('id="beadVariantChoices"', self.html)
        self.assertIn('function generateSmartBeadVariants()', self.javascript)
        self.assertIn('专业拼豆版', self.javascript)
        self.assertIn('易制作版', self.javascript)
        self.assertIn('净背景版', self.javascript)
        self.assertIn('只调用一次 AI，再在本机生成三套方案', self.javascript)

    def test_subject_prompt_supports_people_pets_and_mixed_photos(self):
        prompt = (ROOT / "love_moments" / "bead_ai.py").read_text(encoding="utf-8")
        self.assertIn("单人、双人、宠物", prompt)
        self.assertIn("人与宠物互动合照", prompt)
        self.assertIn("动物的物种、毛色、斑纹", prompt)
        self.assertIn("适配重绘底图", prompt)

    def test_professional_board_has_sharpening_and_five_cell_guides(self):
        self.assertIn('id="beadCanvas" width="1044" height="1044"', self.html)
        self.assertIn('data-action="bead-toggle-zoom"', self.html)
        self.assertIn("function sharpenBeadDetails", self.javascript)
        self.assertIn("imageData = sharpenBeadDetails", self.javascript)
        self.assertIn("for (let index = 5; index < pattern.size; index += 5)", self.javascript)
        self.assertNotIn('drawGuide(10', self.javascript)

    def test_expanded_colour_options_and_shades_are_available(self):
        self.assertIn('<option value="72">72 色 · 最大还原</option>', self.html)
        self.assertNotIn("const BEAD_COLOR_CATALOG = [", self.javascript)
        catalog = json.loads((ROOT / "love_moments" / "data" / "bead_palettes" / "domestic-common-5mm-v1.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(catalog["colors"]), 90)
        for shade in ("深绯红", "淡珊瑚", "森林绿", "淡薄荷", "午夜蓝", "婴儿蓝", "深莓粉", "腮红粉"):
            self.assertIn(shade, {item["name"] for item in catalog["colors"]})

    def test_physical_bead_catalogue_is_loaded_from_server_data(self):
        self.assertIn('id="beadPaletteCatalog"', self.html)
        self.assertIn("loadBeadPaletteCatalogs", self.javascript)
        self.assertIn("/api/v1/entertainment/beads/palettes", self.javascript)
        self.assertIn("BEAD_PALETTE_ROOT", (ROOT / "love_moments" / "bead_palettes.py").read_text(encoding="utf-8"))
        self.assertIn("每 5 格一条定位线", self.html)
        self.assertIn("for (let index = 5; index < pattern.size; index += 5)", self.javascript)

    def test_local_background_removal_creates_empty_cells(self):
        self.assertIn('id="beadBackgroundMode"', self.html)
        self.assertIn('id="beadBackgroundSensitivity"', self.html)
        self.assertIn("function removeBeadBackground", self.javascript)
        self.assertIn("index < 0 ? -1", self.javascript)
        self.assertIn("去除背景 ${empty} 格", self.javascript)
        self.assertIn("protectedSubject", self.javascript)
        self.assertIn("safeRemoval", self.javascript)
        self.assertIn("#eee9e5", self.javascript)

    def test_easy_variant_uses_practical_double_board_resolution(self):
        self.assertIn('label: "易制作版"', self.javascript)
        self.assertIn('size: 58, colorCount: 36, detailMode: "easy"', self.javascript)
        self.assertIn("58 格实体双板规格", self.javascript)

    def test_ironing_guidance_covers_large_portraits_and_special_beads(self):
        self.assertIn('id="beadIroningAdvice"', self.html)
        self.assertIn("function beadIroningAdvice", self.javascript)
        self.assertIn("不要垫软毛巾烫", self.javascript)
        self.assertIn("亮闪、透明、夜光", self.javascript)
        self.assertIn("5×5 试片", self.javascript)

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
