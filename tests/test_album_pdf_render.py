from __future__ import annotations

import io
import unittest
from unittest.mock import patch

import pdfplumber
from PIL import Image
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4

from love_moments.album_pdf import render_album_pdf


class AlbumPdfRenderTests(unittest.TestCase):
    def test_chinese_fixed_template_is_valid_and_markup_is_literal(self) -> None:
        pdf, warnings = render_album_pdf(
            {
                "template": "year",
                "title": "2026 年恋爱纪念册 ♥",
                "date_range": "2026-01-01 — 2026-12-31",
                "audience": "personal",
                "generated_at": "2026-10-28T12:00:00+00:00",
                "items": [
                    {
                        "resource_type": "moment", "resource_id": "moment-safe",
                        "event_at": "2026-05-01T08:00:00+00:00",
                        "title": "杭州晚霞 <img src=\"https://invalid.example/secret.png\">",
                        "body": "这是很长的一段共同回忆。" * 30,
                        "location": "杭州西湖", "visibility": "private",
                        "source_label": "本人填写", "date_source_label": "用户确认时间",
                        "descriptions": [{"nickname": "演示甲", "body": "那天的风很温柔。"}],
                        "image_bytes": None, "image_mime_type": None, "expects_image": True,
                    }
                ],
            }
        )
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertIn(b"%%EOF", pdf[-1024:])
        self.assertEqual(warnings, ["media_missing"])
        self.assertLess(len(pdf), 64 * 1024 * 1024)
        reader = PdfReader(io.BytesIO(pdf))
        self.assertEqual(len(reader.pages), 3)
        for page in reader.pages:
            self.assertAlmostEqual(float(page.mediabox.width), A4[0], places=2)
            self.assertAlmostEqual(float(page.mediabox.height), A4[1], places=2)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertIn("2026 年恋爱纪念册", text)
        self.assertIn("杭州晚霞 <img src=", text)
        self.assertIn("https://invalid.example/secret.png", text)
        self.assertIn("这是很长的一段共同回忆", text)
        self.assertTrue(any(
            "NotoSansSC" in str(font.get_object().get("/BaseFont", ""))
            for page in reader.pages
            for font in page["/Resources"]["/Font"].values()
        ))
        with pdfplumber.open(io.BytesIO(pdf)) as document:
            self.assertEqual(len(document.pages), 3)
            self.assertIn("照片暂不可用", document.pages[-1].extract_text() or "")

    def test_missing_and_corrupt_images_use_placeholder_without_breaking_pdf(self) -> None:
        base = {
            "template": "trip", "title": "杭州旅行纪念册", "date_range": "2026-05-01 — 2026-05-03",
            "audience": "shared", "generated_at": "2026-10-28T12:00:00+00:00",
        }
        pdf, warnings = render_album_pdf(
            {
                **base,
                "items": [
                    {
                        "resource_type": "media", "resource_id": "media-broken",
                        "event_at": "2026-05-02T08:00:00+00:00", "title": "湖边照片",
                        "body": "", "location": None, "visibility": "shared",
                        "source_label": "本人填写", "date_source_label": "照片拍摄时间",
                        "descriptions": [], "image_bytes": b"not-an-image", "image_mime_type": "image/jpeg",
                        "expects_image": True,
                    }
                ],
            }
        )
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertEqual(warnings, ["media_missing"])

    def test_decompression_bomb_warning_uses_placeholder(self) -> None:
        document = {
            "template": "year", "title": "安全图片", "date_range": "2026",
            "audience": "personal", "generated_at": "2026-10-28T12:00:00+00:00",
            "items": [{
                "title": "超大图片", "event_at": "2026-01-01", "body": "正文",
                "image_bytes": b"image", "expects_image": True,
            }],
        }
        with patch(
            "love_moments.album_pdf.Image.open",
            side_effect=Image.DecompressionBombWarning("too large"),
        ):
            pdf, warnings = render_album_pdf(document)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertEqual(warnings, ["media_missing"])


if __name__ == "__main__":
    unittest.main()
