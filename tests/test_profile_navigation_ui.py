from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from love_moments import LoveMomentsStore
from love_moments.store import DEMO_PARTNER, DEMO_USER
from tests import VALID_PNG


ROOT = Path(__file__).resolve().parents[1]


class ProfileIdentityTests(unittest.TestCase):
    def test_avatar_update_only_changes_the_authenticated_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LoveMomentsStore(Path(directory) / "profile.db", seed_demo_data=True)
            partner_before = store.get_user(DEMO_PARTNER)

            result = store.update_my_profile({"avatar_label": "🌙"}, DEMO_USER)

            self.assertEqual(result["user"]["avatar_label"], "🌙")
            self.assertEqual(store.get_user(DEMO_PARTNER), partner_before)
            partner_events = store.list_realtime_events(user_id=DEMO_PARTNER)["items"]
            self.assertTrue(any(item["event_type"] == "profile.updated" for item in partner_events))

    def test_nickname_can_be_changed_after_login_without_changing_partner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LoveMomentsStore(Path(directory) / "profile.db", seed_demo_data=True)
            partner_before = store.get_user(DEMO_PARTNER)

            result = store.update_my_profile({"nickname": "糖糖"}, DEMO_USER)

            self.assertEqual(result["user"]["nickname"], "糖糖")
            self.assertEqual(store.get_user(DEMO_PARTNER), partner_before)

    def test_avatar_photo_is_private_until_selected_then_visible_only_to_couple(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LoveMomentsStore(Path(directory) / "profile.db", seed_demo_data=True)
            media = store.save_media(VALID_PNG, "avatar.png", "image/png", "private", DEMO_USER)

            with self.assertRaises(PermissionError):
                store.get_media_content(media["id"], DEMO_PARTNER)
            updated = store.update_my_profile({"avatar_media_id": media["id"]}, DEMO_USER)

            self.assertEqual(updated["user"]["avatar_media_id"], media["id"])
            self.assertIn(media["id"], updated["user"]["avatar_url"])
            self.assertTrue(store.get_media_content(media["id"], DEMO_PARTNER)[0])
            store.update_my_profile({"avatar_media_id": ""}, DEMO_USER)
            with self.assertRaises(PermissionError):
                store.get_media_content(media["id"], DEMO_PARTNER)

    def test_avatar_photo_rejects_another_users_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LoveMomentsStore(Path(directory) / "profile.db", seed_demo_data=True)
            media = store.save_media(VALID_PNG, "partner.png", "image/png", "private", DEMO_PARTNER)

            with self.assertRaisesRegex(ValueError, "自己上传"):
                store.update_my_profile({"avatar_media_id": media["id"]}, DEMO_USER)

    def test_profile_history_is_couple_visible_and_does_not_store_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LoveMomentsStore(Path(directory) / "profile.db", seed_demo_data=True)
            partner_name = store.get_user(DEMO_PARTNER)["nickname"]

            result = store.update_my_profile({"nickname": partner_name}, DEMO_USER)
            history = store.list_profile_history(DEMO_PARTNER)

            self.assertTrue(result["nickname_matches_partner"])
            self.assertEqual(history["items"][0]["changed_fields"], ["昵称"])
            self.assertNotIn("detail", history["items"][0])
            self.assertNotIn(partner_name, repr(history["items"][0].get("changed_fields")))

    def test_registration_uses_username_as_initial_nickname_when_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LoveMomentsStore(Path(directory) / "profile.db", seed_demo_data=False)

            user = store.register("new_account", "safe-password-123", "")

            self.assertEqual(user["nickname"], "new_account")
            self.assertEqual(user["avatar_label"], "n")

    def test_avatar_update_rejects_unknown_fields_and_oversized_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LoveMomentsStore(Path(directory) / "profile.db", seed_demo_data=True)
            with self.assertRaisesRegex(ValueError, "不支持的字段"):
                store.update_my_profile({"avatar_label": "超", "partner_avatar": "唐"}, DEMO_USER)
            with self.assertRaisesRegex(ValueError, "最多 4 个字符"):
                store.update_my_profile({"avatar_label": "这个头像太长了"}, DEMO_USER)

    def test_theme_is_persisted_per_account_without_changing_partner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LoveMomentsStore(Path(directory) / "profile.db", seed_demo_data=True)
            partner_before = store.get_user(DEMO_PARTNER)

            updated = store.update_my_profile({"ui_theme": "kitty"}, DEMO_USER)

            self.assertEqual(updated["user"]["ui_theme"], "kitty")
            self.assertEqual(store.get_user(DEMO_PARTNER), partner_before)
            with self.assertRaisesRegex(ValueError, "有效的界面主题"):
                store.update_my_profile({"ui_theme": "untrusted-css"}, DEMO_USER)

    def test_display_preferences_are_persisted_per_account_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LoveMomentsStore(Path(directory) / "profile.db", seed_demo_data=True)
            partner_before = store.get_user(DEMO_PARTNER)

            updated = store.update_my_profile(
                {"ui_text_size": "large", "ui_sticker_density": "full", "ui_layout_mode": "iphone13"},
                DEMO_USER,
            )

            self.assertEqual(updated["user"]["ui_text_size"], "large")
            self.assertEqual(updated["user"]["ui_sticker_density"], "full")
            self.assertEqual(updated["user"]["ui_layout_mode"], "iphone13")
            self.assertEqual(store.get_user(DEMO_PARTNER), partner_before)
            with self.assertRaisesRegex(ValueError, "有效的文字大小"):
                store.update_my_profile({"ui_text_size": "huge"}, DEMO_USER)
            with self.assertRaisesRegex(ValueError, "有效的贴纸数量"):
                store.update_my_profile({"ui_sticker_density": "unlimited"}, DEMO_USER)
            with self.assertRaisesRegex(ValueError, "有效的页面布局"):
                store.update_my_profile({"ui_layout_mode": "iphone15"}, DEMO_USER)

    def test_existing_database_gains_safe_display_preference_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.db"
            with sqlite3.connect(path) as db:
                db.execute(
                    """CREATE TABLE users (
                        id TEXT PRIMARY KEY, phone TEXT UNIQUE, username TEXT UNIQUE,
                        password_hash TEXT, nickname TEXT NOT NULL, avatar_label TEXT NOT NULL,
                        ui_theme TEXT NOT NULL DEFAULT 'classic', locale TEXT NOT NULL DEFAULT 'zh-CN',
                        timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai', created_at TEXT NOT NULL,
                        deleted_at TEXT
                    )"""
                )
                db.execute(
                    """INSERT INTO users(
                        id,phone,username,password_hash,nickname,avatar_label,ui_theme,
                        locale,timezone,created_at,deleted_at
                    ) VALUES ('legacy-user',NULL,'legacy',NULL,'旧用户','旧','classic',
                        'zh-CN','Asia/Shanghai','2026-10-01T00:00:00+00:00',NULL)"""
                )
                db.execute("PRAGMA user_version=34")
            store = LoveMomentsStore(path, seed_demo_data=False)
            user = store.get_user("legacy-user")
            self.assertEqual(user["ui_text_size"], "standard")
            self.assertEqual(user["ui_sticker_density"], "cute")
            self.assertEqual(user["ui_layout_mode"], "adaptive")
            self.assertIsNone(user["avatar_media_id"])


class MobileLayoutContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        cls.js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_identity_and_avatar_editor_are_explicit(self) -> None:
        self.assertIn('class="avatar-wrap avatar-wrap-me"', self.html)
        self.assertIn('class="avatar-wrap avatar-wrap-partner"', self.html)
        self.assertIn('id="avatarProfileForm"', self.html)
        self.assertIn('id="avatarLabelInput"', self.html)
        self.assertIn('id="nicknameInput"', self.html)
        self.assertIn('id="avatarImageInput"', self.html)
        self.assertIn('id="profilePreviewAvatar"', self.html)
        self.assertIn('id="profileHistoryList"', self.html)
        self.assertIn('id="appUpdateBanner"', self.html)
        self.assertNotIn('id="authNickname"', self.html)
        self.assertIn('id="themeProfileForm"', self.html)
        self.assertIn('id="accountCenterMenu"', self.html)
        for section in ("profile", "appearance", "notifications", "privacy", "security"):
            self.assertIn(f'data-account-section="{section}"', self.html)
            self.assertIn(f'data-account-panel="{section}"', self.html)
        self.assertIn("async function loadAccountSection", self.js)
        self.assertIn('showAccountSection("overview")', self.js)
        self.assertIn('value="kitty"', self.html)
        self.assertIn('value="daniel"', self.html)
        self.assertIn('name="uiTextSize"', self.html)
        self.assertIn('name="stickerDensity"', self.html)
        self.assertIn('name="layoutMode"', self.html)
        self.assertIn("applyUiDisplayPreferences", self.js)
        self.assertIn('document.body.dataset.uiLayout = layoutMode', self.js)
        self.assertIn("theme-sticker--${pose}", self.js)
        self.assertIn("伴侣状态 · ${partner.nickname}", self.js)
        self.assertIn('id="sharedCommentVisibilityNote"', self.html)
        self.assertNotIn("唐和超登录后都能看到", self.html)
        self.assertIn("function partnerNickname", self.js)
        self.assertIn("function notificationNicknameText", self.js)
        self.assertIn("profileDraftIsDirty", self.js)
        self.assertIn("SKIP_WAITING", self.js)

    def test_navigation_is_viewport_fixed_and_resets_both_scroll_owners(self) -> None:
        self.assertNotIn("container-type: inline-size", self.css)
        self.assertIn("window.scrollTo({ top: 0, behavior })", self.js)
        self.assertIn('document.querySelector(".phone-shell")?.scrollTo', self.js)
        self.assertIn("bottom: max(8px, env(safe-area-inset-bottom))", self.css)
        self.assertIn("padding-bottom: calc(112px + env(safe-area-inset-bottom))", self.css)

    def test_chat_composer_and_message_actions_have_stable_mobile_layout(self) -> None:
        self.assertIn("grid-template-columns: 36px minmax(0,1fr) 36px 36px", self.css)
        self.assertIn('class="chat-offline-note"', self.html)
        self.assertIn('class="message-meta"', self.js)
        self.assertIn('class="message-menu"', self.js)
        self.assertIn("max-width: 74%", self.css)

    def test_chat_always_opens_at_latest_and_exposes_new_message_counter(self) -> None:
        self.assertIn('id="chatNewMessages"', self.html)
        self.assertIn('data-action="jump-new-messages"', self.html)
        self.assertIn('scrollChatToLatest({ behavior: "auto" })', self.js)
        self.assertIn('button.textContent = `新消息（${state.chatHistory.newCount}）↓`', self.js)
        self.assertIn(
            'if (action === "jump-new-messages") { scrollChatToLatest({ behavior: "auto", settleMs: 1400 }); return; }',
            self.js,
        )
        self.assertIn('点击回到最新消息', self.js)
        self.assertIn('state.chatHistory.latestLockUntil = screen === "chat" ? Date.now() + 3000 : 0', self.js)
        self.assertIn('const lockToLatest = state.screen === "chat" && state.chatHistory.latestLockUntil > Date.now()', self.js)
        self.assertNotIn('function scrollChatToFirstUnread', self.js)
        self.assertNotIn("viewport.scrollTop = state.chatHistory.savedScrollTop", self.js)

    def test_personal_themes_use_allowlisted_body_attributes(self) -> None:
        self.assertIn('body[data-ui-theme="kitty"]', self.css)
        self.assertIn('body[data-ui-theme="daniel"]', self.css)
        self.assertIn("normalizeUiTheme", self.js)
        self.assertIn('document.body.dataset.uiTheme = theme', self.js)
        self.assertIn('body[data-ui-layout="iphone13"]', self.css)


if __name__ == "__main__":
    unittest.main()
