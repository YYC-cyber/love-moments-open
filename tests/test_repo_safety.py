from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_repo_safety


class RepositorySafetyScannerTests(unittest.TestCase):
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
