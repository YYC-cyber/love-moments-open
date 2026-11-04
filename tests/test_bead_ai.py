import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from love_moments.bead_ai import BailianBeadProvider, bead_ai_status
from love_moments.store import DEMO_PARTNER, DEMO_USER, LoveMomentsStore


class _Response:
    def __init__(self, payload: bytes, headers=None):
        self.payload = payload
        self.headers = headers or {}

    def read(self, _maximum=None):
        return self.payload

    def close(self):
        return None


def _image_bytes(size=(480, 640), colour=(244, 205, 214)):
    output = io.BytesIO()
    Image.new("RGB", size, colour).save(output, "JPEG", quality=90)
    return output.getvalue()


class BeadAiProviderTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(
            os.environ,
            {
                "DASHSCOPE_API_KEY": "test-secret",
                "DASHSCOPE_BASE_URL": "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                "DASHSCOPE_IMAGE_MODEL": "qwen-image-3.0",
                "DASHSCOPE_IMAGE_FINAL_MODEL": "qwen-image-3.0-pro",
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def test_status_only_exposes_non_secret_configuration(self):
        status = bead_ai_status()
        self.assertTrue(status["configured"])
        self.assertEqual(status["model"], "qwen-image-3.0")
        self.assertNotIn("api_key", status)

    def test_enhance_sends_original_and_downloads_temporary_result(self):
        generated = _image_bytes((768, 1024), (240, 212, 218))
        provider_response = json.dumps({
            "data": [{"url": "https://dashscope-a717.oss-accelerate.aliyuncs.com/result.png"}]
        }).encode()
        calls = []

        def fake_urlopen(req, **_kwargs):
            calls.append(req)
            if len(calls) == 1:
                body = json.loads(req.data.decode("utf-8"))
                self.assertEqual(body["model"], "qwen-image-3.0")
                self.assertTrue(body["image"].startswith("data:image/jpeg;base64,"))
                self.assertIn("本次变化重点", body["prompt"])
                self.assertIsInstance(body["seed"], int)
                return _Response(provider_response)
            return _Response(generated, {"Content-Length": str(len(generated))})

        with mock.patch("love_moments.bead_ai.request.urlopen", side_effect=fake_urlopen):
            result = BailianBeadProvider().enhance(_image_bytes(), "image/jpeg", attempt=2)
        self.assertEqual(result.model, "qwen-image-3.0")
        self.assertEqual(result.attempt, 2)
        self.assertEqual(result.mime_type, "image/jpeg")
        with Image.open(io.BytesIO(result.image)) as image:
            self.assertEqual(image.size, (768, 1024))

    def test_final_quality_uses_pro_model(self):
        provider = BailianBeadProvider()
        with mock.patch.object(provider, "_download_result", return_value=(_image_bytes(), "image/jpeg")), mock.patch(
            "love_moments.bead_ai.request.urlopen",
            return_value=_Response(json.dumps({"data": [{"url": "https://dashscope-a717.oss-accelerate.aliyuncs.com/result.png"}]}).encode()),
        ) as opened:
            result = provider.enhance(_image_bytes(), "image/jpeg", final_quality=True)
        self.assertEqual(result.model, "qwen-image-3.0-pro")
        payload = json.loads(opened.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["model"], "qwen-image-3.0-pro")

    def test_rejects_untrusted_result_host(self):
        response = _Response(json.dumps({"data": [{"url": "https://example.org/result.png"}]}).encode())
        with mock.patch("love_moments.bead_ai.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "未授权"):
                BailianBeadProvider().enhance(_image_bytes(), "image/jpeg")


class BeadAiVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = LoveMomentsStore(root / "bead-versions.db", media_root=root / "media", seed_demo_data=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_versions_are_private_and_only_explicit_delete_removes_one(self):
        first = self.store.save_bead_ai_preview(_image_bytes(colour=(240, 210, 220)), "image/jpeg", "qwen-image-3.0", 1, DEMO_USER)
        second = self.store.save_bead_ai_preview(_image_bytes(colour=(235, 205, 215)), "image/jpeg", "qwen-image-3.0", 2, DEMO_USER)
        owner_items = self.store.list_bead_ai_previews(DEMO_USER)["items"]
        self.assertEqual({item["id"] for item in owner_items}, {first["id"], second["id"]})
        self.assertEqual(self.store.list_bead_ai_previews(DEMO_PARTNER)["items"], [])

        self.store.delete_bead_ai_preview(second["id"], DEMO_USER)
        remaining = self.store.list_bead_ai_previews(DEMO_USER)["items"]
        self.assertEqual([item["id"] for item in remaining], [first["id"]])


if __name__ == "__main__":
    unittest.main()
