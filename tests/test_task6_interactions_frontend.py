from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Task6InteractionsFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        cls.web = (ROOT / "static/app.js").read_text(encoding="utf-8")
        cls.client = (ROOT / "packages/domain-client/src/index.ts").read_text(encoding="utf-8")
        cls.mobile = (ROOT / "clients/mobile/App.tsx").read_text(encoding="utf-8")

    def test_h5_has_private_gift_claim_controls_without_creator_disclosure(self) -> None:
        self.assertIn('id="wishlistKind"', self.html)
        self.assertIn('data-action="claim-wish"', self.web)
        self.assertIn('data-action="release-wish-claim"', self.web)
        self.assertIn('data-action="complete-wish-claim"', self.web)
        self.assertIn("item.my_claim.private_note", self.web)
        self.assertIn("item.my_claim?.private_note", self.mobile)
        self.assertIn("private_note?: string", self.client)
        self.assertNotIn("item.claimant_name", self.web)

    def test_h5_expression_assistant_applies_to_composer_but_never_sends(self) -> None:
        self.assertIn('id="expressionModal"', self.html)
        self.assertIn('data-action="expression-help"', self.web)
        self.assertIn('/api/v1/expression/suggestions', self.web)
        self.assertIn('document.getElementById("chatInput").value =', self.web)
        self.assertNotIn('sendMessage(state.expressionSuggestion', self.web)

    def test_status_contract_uses_server_duration_and_exact_presets(self) -> None:
        self.assertIn('duration_minutes', self.web)
        self.assertNotIn('status_code: statusCode, text, expires_at:', self.web)
        for code in ("busy", "want_chat", "need_company", "feeling_low", "moving", "home", "custom"):
            self.assertIn(f'value="{code}"', self.html)

    def test_domain_client_and_mobile_cover_claims_status_and_expression(self) -> None:
        for method in ("claimWishlistItem", "releaseWishlistClaim", "completeWishlistClaim", "suggestExpression"):
            self.assertIn(method, self.client)
        self.assertIn("duration_minutes", self.client)
        self.assertIn("claimWishlistItem", self.mobile)
        self.assertIn("releaseWishlistClaim", self.mobile)
        self.assertIn("status_text", self.mobile)


if __name__ == "__main__":
    unittest.main()
