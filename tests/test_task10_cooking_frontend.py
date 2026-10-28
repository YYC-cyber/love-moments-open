from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CookingFrontendContractTests(unittest.TestCase):
    def test_h5_has_resilient_full_screen_cooking_mode(self) -> None:
        html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")
        css = (ROOT / "static/app.css").read_text(encoding="utf-8")
        self.assertIn('id="cookingScreen"', html)
        self.assertIn('id="cookTimerLive"', html)
        for text in ("上一步", "朗读当前步骤", "下一步", "结束烹饪", "人工更新库存"):
            self.assertIn(text, html + app)
        self.assertIn('"speechSynthesis" in window', app)
        self.assertIn("navigator.wakeLock?.request", app)
        self.assertIn("Date.parse(timer.ends_at)", app)
        self.assertIn('Date.parse(session?.server_now || "")', app)
        self.assertIn("serverOffsetMs", app)
        self.assertIn("receivedAt", app)
        self.assertNotIn("Date.parse(state.cooking.session.server_now) - Date.now()", app)
        self.assertIn("/api/v1/cook-sessions?status=active", app)
        self.assertIn("cook_session", app)
        self.assertIn("history.replaceState", app)
        self.assertIn("visibilitychange", app)
        self.assertIn("pageshow", app)
        self.assertIn('window.addEventListener("focus"', app)
        self.assertIn("cleanupCookingRuntime", app)
        self.assertIn("COOKING_RESTORE_TERMINAL_STATUSES", app)
        self.assertIn("closeInvalidCookingDeepLink", app)
        self.assertIn("[403, 404, 409]", app)
        resume = app.split("async function resumeCookingDeepLink", 1)[1].split(
            'document.addEventListener("visibilitychange"', 1
        )[0]
        self.assertIn("closeInvalidCookingDeepLink", resume)
        self.assertIn("throw error", resume)
        unlink = app.split('if (action === "unlink-couple")', 1)[1].split(
            'if (action === "delete-account")', 1
        )[0]
        self.assertIn("closeCookingScreen()", unlink)
        self.assertIn("state.cooking.restoreChecked = false", unlink)
        bootstrap = app.split("async function loadBootstrap", 1)[1].split(
            "function loadOfflineSnapshot", 1
        )[0]
        self.assertIn("closeCookingForUnpairedAccount", bootstrap)
        auth = app.split("async function initializeAuth", 1)[1].split(
            "function saveDeepLinkTarget", 1
        )[0]
        self.assertIn("closeCookingForUnpairedAccount", auth)
        self.assertIn('"SpeechSynthesisUtterance" in window', app)
        self.assertIn("utterance.onerror", app)
        self.assertIn("屏幕可能熄灭", app)
        self.assertIn('id="cookingPantryReceipts"', html)
        self.assertIn('data-action="undo-cook-pantry"', app)
        self.assertIn("min-height: 56px", css)
        offline_spec = app.split("function offlineMutationSpec", 1)[1].split(
            "function offlineQueueEntryIsValid", 1
        )[0]
        self.assertNotIn("cook-sessions", offline_spec)
        service_worker = (ROOT / "sw.js").read_text(encoding="utf-8")
        self.assertNotIn("cook-sessions", service_worker)

    def test_mobile_only_opens_protected_h5_cooking_route(self) -> None:
        mobile = (ROOT / "clients/mobile/App.tsx").read_text(encoding="utf-8")
        self.assertIn("打开烹饪模式", mobile)
        self.assertIn("cook_recipe", mobile)
        self.assertIn("cook_meal", mobile)
        self.assertNotIn("SpeechSynthesisUtterance", mobile)
        self.assertNotIn("remaining_seconds", mobile)


if __name__ == "__main__":
    unittest.main()
