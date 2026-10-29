"""Run synthetic browser-session contracts without any production access."""
import json
import subprocess
import unittest
from pathlib import Path


class FrontendSessionLifecycleTests(unittest.TestCase):
    def test_pending_connections_and_input_preservation(self):
        result = subprocess.run(
            ["node", "scripts/test_realtime_lifecycle.cjs"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
