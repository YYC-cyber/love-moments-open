import json
import subprocess
import unittest
from pathlib import Path


class PracticalFeaturesFrontendTests(unittest.TestCase):
    def test_manifest_has_only_the_two_supported_shortcuts(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "manifest.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["shortcuts"],
            [
                {"name": "发消息", "short_name": "消息", "url": "/?screen=chat"},
                {"name": "记录瞬间", "short_name": "瞬间", "url": "/?action=moment"},
            ],
        )

    def test_shortcuts_and_offline_write_policy(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", "scripts/test_practical_features_frontend.cjs"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])

    def test_album_export_is_real_online_only_flow_on_h5_and_protected_mobile_panel(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "static/index.html").read_text(encoding="utf-8")
        script = (root / "static/app.js").read_text(encoding="utf-8")
        mobile = (root / "clients/mobile/App.tsx").read_text(encoding="utf-8")
        self.assertIn('id="albumExportModal"', html)
        self.assertIn('data-action="open-year-album"', html)
        self.assertIn('data-action="open-trip-album"', html)
        self.assertIn('/api/v1/album-exports/preview', script)
        self.assertIn('/api/v1/album-exports', script)
        self.assertIn('albumExportRecentJobs', html)
        self.assertIn('restore-album-export', script)
        self.assertIn('/api/v1/album-exports/${encodeURIComponent(jobId)}', script)
        self.assertIn("生成 PDF 需要联网", script)
        self.assertIn('setWebPanel("album")', mobile)
        self.assertIn('album=1', mobile)

    def test_shared_details_show_real_visibility_and_fact_provenance(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "static/app.js").read_text(encoding="utf-8")
        style = (root / "static/app.css").read_text(encoding="utf-8")
        self.assertIn('detail.visibility_label || "双方可见"', script)
        self.assertIn("detail.fact_sensitive", script)
        self.assertIn("detail.source_checked_at", script)
        self.assertIn(".shared-detail-provenance", style)

    def test_react_manifest_cards_use_controlled_same_origin_h5_links(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", "scripts/test_controlled_deep_links.cjs"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
