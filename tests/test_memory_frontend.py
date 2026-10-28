from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MemoryFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        cls.web = (ROOT / "static/app.js").read_text(encoding="utf-8")
        cls.mobile = (ROOT / "clients/mobile/App.tsx").read_text(encoding="utf-8")

    def test_h5_exposes_real_search_and_completion_controls(self) -> None:
        for marker in (
            'id="memorySearchForm"', 'id="memorySearchResults"',
            'id="memoryCompletionList"', 'data-action="refresh-memory-completion"',
        ):
            self.assertIn(marker, self.html)
        for route in (
            "/api/v1/memories/search", "/api/v1/memories/completion",
            "/descriptions/me", "/respond",
        ):
            self.assertIn(route, self.web)
        self.assertIn('api("/api/v1/memories/search", { method: "POST"', self.web)

    def test_private_results_never_open_the_shared_comment_thread(self) -> None:
        self.assertIn('actionEl.dataset.private === "1"', self.web)
        self.assertIn("await describeMemory(resourceType, resourceId)", self.web)
        self.assertIn("await openSharedDetail(resourceType, resourceId)", self.web)

    def test_search_is_session_state_not_local_storage(self) -> None:
        self.assertIn("memorySearchResult: null", self.web)
        self.assertNotIn('setItem("love-memory-search', self.web)
        self.assertIn("resetMemoryDiscovery()", self.web)

    def test_exif_candidate_requires_an_explicit_confirmation(self) -> None:
        self.assertIn("确认发生时间", self.web)
        self.assertIn("确认把回忆时间改为", self.web)
        self.assertIn("expected_parent_version", self.web)

    def test_two_person_descriptions_have_a_discoverable_merge_flow(self) -> None:
        self.assertIn("manage-memory-descriptions", self.web)
        self.assertIn("/merge-proposal", self.web)
        self.assertIn("/merge-confirm", self.web)
        self.assertIn("等待对方确认", self.web)

    def test_react_native_has_a_minimum_memory_entry(self) -> None:
        self.assertIn('setWebPanel("memory")', self.mobile)
        self.assertIn("timeline&memory_search=1", self.mobile)

    def test_web_javascript_parses(self) -> None:
        subprocess.run(["node", "--check", str(ROOT / "static/app.js")], check=True)


if __name__ == "__main__":
    unittest.main()
