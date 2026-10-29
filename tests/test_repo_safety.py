from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_repo_safety


class RepositorySafetyScannerTests(unittest.TestCase):
    def test_personal_runtime_and_deployment_files_are_rejected(self) -> None:
        for path in ("data/theme-assets/private.png", ".env", ".docs/plan.md", ".planning/tasks.md", "backups/copy.zip", "scripts/prepare_personal_release.py", "scripts/test_remote_production.cjs"):
            with self.subTest(path=path):
                self.assertIsNotNone(check_repo_safety.forbidden_path(path))
        self.assertIsNone(check_repo_safety.forbidden_path("data/.gitkeep"))

    def test_env_example_does_not_bypass_literal_secret_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.example").write_text("SERVICE_API_KEY=0123456789abcdef0123456789abcdef\n", encoding="utf-8")
            with patch.object(check_repo_safety, "ROOT", root):
                self.assertEqual(check_repo_safety.scan_text(".env.example"), ["non-empty SERVICE_API_KEY assignment"])
            (root / ".env.example").write_text("SERVICE_API_KEY=your_service_api_key\n", encoding="utf-8")
            with patch.object(check_repo_safety, "ROOT", root):
                self.assertEqual(check_repo_safety.scan_text(".env.example"), [])

    def test_symlinks_are_rejected_without_reading_the_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "linked.txt").symlink_to(root / "not-present.txt")
            with patch.object(check_repo_safety, "ROOT", root):
                self.assertEqual(check_repo_safety.scan_text("linked.txt"), ["symlink outside the reviewed source boundary"])

    def test_code_expression_is_not_misclassified_as_a_secret_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "module.py").write_text(
                "CATEGORY_KEYS = frozenset(CATEGORY_LABELS)\n", encoding="utf-8"
            )
            with patch.object(check_repo_safety, "ROOT", root):
                self.assertEqual(check_repo_safety.scan_text("module.py"), [])

    def test_long_literal_secret_assignment_is_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "settings.py").write_text(
                'SERVICE_API_KEY="0123456789abcdef0123456789abcdef"\n',
                encoding="utf-8",
            )
            with patch.object(check_repo_safety, "ROOT", root):
                self.assertEqual(
                    check_repo_safety.scan_text("settings.py"),
                    ["non-empty SERVICE_API_KEY assignment"],
                )


if __name__ == "__main__":
    unittest.main()
