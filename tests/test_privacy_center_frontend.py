from __future__ import annotations

import unittest
from pathlib import Path


class PrivacyCenterFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.html = (cls.root / "static/index.html").read_text(encoding="utf-8")
        cls.app = (cls.root / "static/app.js").read_text(encoding="utf-8")
        cls.client = (cls.root / "packages/domain-client/src/index.ts").read_text(encoding="utf-8")

    def test_account_center_has_real_privacy_and_offline_hosts(self) -> None:
        for element_id in ("privacyUsage", "privacyGrants", "offlineSyncStatus"):
            self.assertIn(f'id="{element_id}"', self.html)
            self.assertIn(f'getElementById("{element_id}")', self.app)
        self.assertIn('api("/api/v1/account/privacy")', self.app)
        self.assertIn("listOfflineMutationsForAccount()", self.app)

    def test_revoke_and_queue_controls_call_real_persistence(self) -> None:
        self.assertIn('data-action="revoke-ai-grant"', self.app)
        self.assertIn('/api/v1/account/privacy/grants/${encodeURIComponent(actionEl.dataset.id)}/revoke', self.app)
        self.assertIn('data-action="retry-offline-mutation"', self.app)
        self.assertIn('data-action="discard-offline-mutation"', self.app)
        self.assertIn('db.transaction("pending", "readwrite")', self.app)
        self.assertIn("await flushMealMutations()", self.app)
        self.assertIn("无法收回", self.html)
        self.assertIn("发送中 · 不可丢弃", self.app)
        self.assertIn("已经发出，不能再丢弃", self.app)

    def test_privacy_failure_does_not_block_devices_media_or_offline_queue(self) -> None:
        self.assertIn("Promise.allSettled", self.app)
        self.assertIn("AI 用量与授权暂时无法读取", self.app)
        self.assertIn("设备、媒体和离线操作仍可继续管理", self.app)

    def test_domain_client_exposes_typed_privacy_contract(self) -> None:
        self.assertIn("export interface AccountPrivacyCenter", self.client)
        self.assertIn('privacyCenter() { return this.request<AccountPrivacyCenter>("/api/v1/account/privacy")', self.client)
        self.assertIn("revokeAiContextGrant(id: string, expectedVersion: number)", self.client)


if __name__ == "__main__":
    unittest.main()
