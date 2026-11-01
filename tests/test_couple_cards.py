from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from love_moments.store import DEMO_PARTNER, DEMO_USER, LoveMomentsStore


SIGNATURE = [[[0.05, 0.7], [0.25, 0.2], [0.45, 0.75]], [[0.5, 0.6], [0.72, 0.3], [0.92, 0.55]]]


class CoupleCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(Path(self.temp.name) / "cards.db", seed_demo_data=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_card(self) -> dict:
        return self.store.create_couple_card({
            "template_id": "wish", "style_id": "wish", "message": "一起去看冬天的海",
            "signature_name": "唐", "signature": SIGNATURE, "confirmed": True,
        }, DEMO_USER)

    def test_catalog_has_24_practical_types_and_24_kitty_styles(self) -> None:
        catalog = self.store.list_couple_cards(DEMO_USER)
        self.assertEqual(len(catalog["templates"]), 25)
        self.assertEqual(len(catalog["styles"]), 24)
        self.assertEqual({item["sticker_index"] for item in catalog["styles"]}, set(range(24)))
        self.assertEqual(len({item["pattern"] for item in catalog["styles"]}), 24)
        self.assertEqual(len({item["name"] for item in catalog["styles"]}), 24)
        self.assertEqual(len({(item["paper"], item["accent"], item["ink"]) for item in catalog["styles"]}), 24)
        practical = [item for item in catalog["templates"] if item["id"] != "custom"]
        self.assertEqual(len({item["name"] for item in practical}), 24)
        self.assertEqual(len({item["prompt"] for item in practical}), 24)
        self.assertEqual(len({item["notice"] for item in practical}), 24)
        self.assertTrue(all("必须" not in item["notice"] and "不代表" not in item["notice"] for item in practical))
        self.assertTrue(all("Kitty" in item["name"] for item in catalog["styles"]))
        self.assertIn("愿望卡", {item["name"] for item in catalog["templates"]})
        self.assertIn("和好卡", {item["name"] for item in catalog["templates"]})
        self.assertIn("亲亲卡", {item["name"] for item in catalog["templates"]})

    def test_both_signatures_are_required_before_pdf(self) -> None:
        created = self.create_card()
        self.assertEqual(created["status"], "awaiting_recipient")
        self.assertTrue(created["creator_signature"])
        with self.assertRaisesRegex(ValueError, "签名后"):
            self.store.couple_card_pdf(created["id"], DEMO_USER)
        signed = self.store.sign_couple_card(created["id"], {
            "signature_name": "超", "signature": SIGNATURE, "confirmed": True,
            "expected_version": created["version"],
        }, DEMO_PARTNER)
        self.assertEqual(signed["status"], "signed")
        self.assertTrue(signed["recipient_signature"])
        pdf, filename = self.store.couple_card_pdf(created["id"], DEMO_USER)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertGreater(len(pdf), 5000)
        self.assertTrue(filename.endswith(".pdf"))

    def test_only_recipient_can_sign_and_versions_are_checked(self) -> None:
        created = self.create_card()
        payload = {"signature_name": "唐", "signature": SIGNATURE, "confirmed": True, "expected_version": 1}
        with self.assertRaises(PermissionError): self.store.sign_couple_card(created["id"], payload, DEMO_USER)
        with self.assertRaisesRegex(ValueError, "签名板"):
            self.store.sign_couple_card(created["id"], {**payload, "signature_name": "超", "signature": []}, DEMO_PARTNER)

    def test_custom_card_rejects_coercive_content(self) -> None:
        with self.assertRaisesRegex(ValueError, "双方同意"):
            self.store.create_couple_card({
                "template_id": "custom", "style_id": "promise", "title": "命令卡",
                "message": "你必须服从", "signature_name": "唐", "signature": SIGNATURE, "confirmed": True,
            }, DEMO_USER)

    def test_selected_style_is_preserved_for_saved_card_and_pdf(self) -> None:
        created = self.store.create_couple_card({
            "template_id": "travel", "style_id": "promise", "message": "一起去看海",
            "signature_name": "唐", "signature": SIGNATURE, "confirmed": True,
        }, DEMO_USER)
        self.assertEqual(created["style"]["id"], "promise")
        self.assertEqual(created["style"]["pattern"], "promise_seal")


if __name__ == "__main__":
    unittest.main()
