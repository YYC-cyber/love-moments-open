import subprocess
import unittest
from pathlib import Path


class ServiceWorkerTests(unittest.TestCase):
    def test_notification_routing_and_api_cache_isolation(self):
        root = Path(__file__).resolve().parents[1]
        for command in (["node", "scripts/test_service_worker.cjs"], ["node", "scripts/test_security_foundations.cjs", "worker"]):
            result = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
