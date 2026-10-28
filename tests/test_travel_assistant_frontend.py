from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TravelAssistantFrontendContractTests(unittest.TestCase):
    def test_trip_page_exposes_conversation_panel_and_actions(self) -> None:
        html = (ROOT / "static/index.html").read_text()
        script = (ROOT / "static/app.js").read_text()
        styles = (ROOT / "static/app.css").read_text()
        self.assertIn('data-trip-section="assistant"', html)
        self.assertIn('id="tripAssistantForm"', html)
        self.assertIn('id="tripAssistantMessages"', html)
        self.assertIn("renderTripAssistant", script)
        self.assertIn('action === "assistant-select-quote"', script)
        self.assertIn('action === "assistant-select-food"', script)
        self.assertIn('action === "assistant-confirm-change"', script)
        self.assertIn(".trip-assistant", styles)

    def test_trip_page_exposes_one_shot_live_assistant(self) -> None:
        html = (ROOT / "static/index.html").read_text()
        script = (ROOT / "static/app.js").read_text()
        styles = (ROOT / "static/app.css").read_text()
        for marker in ('id="tripLivePanel"', 'id="tripLiveRefresh"', 'id="tripLiveResults"'):
            self.assertIn(marker, html)
        self.assertIn("仅本次请求使用，不保存位置", html)
        self.assertIn("navigator.geolocation.getCurrentPosition", script)
        self.assertIn("position.timestamp", script)
        self.assertIn("LIVE_RESULT_REUSE_METERS = 300", script)
        self.assertIn("LIVE_RESULT_REUSE_MS = 10 * 60 * 1000", script)
        self.assertIn("/assistant/live", script)
        self.assertIn("/assistant/navigation", script)
        self.assertIn('action === "trip-live-refresh"', script)
        self.assertIn('action === "assistant-route"', script)
        self.assertIn("location_persisted", script)
        self.assertIn(".trip-live-panel", styles)
        self.assertIn(".trip-live-results", styles)

    def test_all_new_assistant_cards_have_real_actions(self) -> None:
        script = (ROOT / "static/app.js").read_text()
        for card_type in ("day_replan", "next_stop", "quote_group", "trip_recap"):
            self.assertIn(f'card.type === "{card_type}"', script)
        for action in (
            "assistant-recap-edit",
            "assistant-recap-confirm",
            "assistant-recap-cancel",
        ):
            self.assertIn(f'action === "{action}"', script)
        self.assertIn("live_context", script)
        self.assertIn("source_label", script)

    def test_exact_live_location_is_never_written_to_browser_storage(self) -> None:
        script = (ROOT / "static/app.js").read_text()
        self.assertNotIn('localStorage.setItem("love-trip-live', script)
        self.assertNotIn("localStorage.setItem('love-trip-live", script)

    def test_live_context_is_bound_to_active_user_and_reset_at_auth_boundaries(self) -> None:
        script = (ROOT / "static/app.js").read_text()
        self.assertIn("ownerUserId", script)
        self.assertIn("tripLiveEpoch", script)
        self.assertIn("function resetTripLive", script)
        self.assertIn("resetTripLive();\n      state.data = null", script)
        self.assertIn("resetTripLive(); state.data = null", script)
        self.assertIn("state.tripLive.ownerUserId === state.data?.user?.id", script)
        self.assertIn("state.tripLiveEpoch !== requestEpoch", script)

    def test_live_result_reuse_uses_an_immutable_anchor_and_is_labeled_cached(self) -> None:
        script = (ROOT / "static/app.js").read_text()
        self.assertIn("resultAnchorContext", script)
        self.assertIn("liveDistanceMeters(previous.resultAnchorContext, context)", script)
        self.assertIn('deliveryMode: "browser_cache"', script)
        self.assertIn("缓存于", script)


if __name__ == "__main__":
    unittest.main()
