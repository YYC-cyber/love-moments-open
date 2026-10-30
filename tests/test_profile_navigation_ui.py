from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from love_moments import LoveMomentsStore
from love_moments.store import DEMO_PARTNER, DEMO_USER
from tests import VALID_PNG


ROOT = Path(__file__).resolve().parents[1]


class PublicProfilePrivacyTests(unittest.TestCase):
    def test_avatar_photo_is_shared_only_while_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LoveMomentsStore(Path(directory) / "profile.db", seed_demo_data=True)
            media = store.save_media(VALID_PNG, "avatar.png", "image/png", "private", DEMO_USER)
            with self.assertRaises(PermissionError):
                store.get_media_content(media["id"], DEMO_PARTNER)

            updated = store.update_my_profile({"avatar_media_id": media["id"]}, DEMO_USER)
            self.assertIn(media["id"], updated["user"]["avatar_url"])
            self.assertTrue(store.get_media_content(media["id"], DEMO_PARTNER)[0])

            store.update_my_profile({"avatar_media_id": ""}, DEMO_USER)
            with self.assertRaises(PermissionError):
                store.get_media_content(media["id"], DEMO_PARTNER)

    def test_avatar_rejects_another_users_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LoveMomentsStore(Path(directory) / "profile.db", seed_demo_data=True)
            media = store.save_media(VALID_PNG, "partner.png", "image/png", "private", DEMO_PARTNER)
            with self.assertRaisesRegex(ValueError, "自己上传"):
                store.update_my_profile({"avatar_media_id": media["id"]}, DEMO_USER)

    def test_profile_history_exposes_fields_but_not_old_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LoveMomentsStore(Path(directory) / "profile.db", seed_demo_data=True)
            partner_name = store.get_user(DEMO_PARTNER)["nickname"]
            result = store.update_my_profile({"nickname": partner_name}, DEMO_USER)
            history = store.list_profile_history(DEMO_PARTNER)

            self.assertTrue(result["nickname_matches_partner"])
            self.assertEqual(history["items"][0]["changed_fields"], ["昵称"])
            self.assertNotIn("detail", history["items"][0])

    def test_existing_database_gains_avatar_column(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
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
            self.assertIsNone(store.get_user("legacy-user")["avatar_media_id"])


class PublicProfileUiContractTests(unittest.TestCase):
    def test_profile_editor_and_update_prompt_are_present(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")

        for identifier in (
            'id="nicknameInput"', 'id="avatarImageInput"', 'id="profilePreviewAvatar"',
            'id="profileHistoryList"', 'id="appUpdateBanner"',
        ):
            self.assertIn(identifier, html)
        self.assertIn("profileDraftIsDirty", js)
        self.assertIn("SKIP_WAITING", js)
        self.assertIn('data-action="jump-new-messages"', html)
        self.assertIn('scrollChatToLatest({ behavior: "auto" })', js)
        self.assertIn('if (action === "jump-new-messages") { scrollChatToLatest(); return; }', js)
        self.assertIn('点击回到最新消息', js)
        self.assertIn('state.chatHistory.latestLockUntil = screen === "chat" ? Date.now() + 3000 : 0', js)
        self.assertIn('const lockToLatest = state.screen === "chat" && state.chatHistory.latestLockUntil > Date.now()', js)
        self.assertNotIn('function scrollChatToFirstUnread', js)
        self.assertIn('"private, no-cache" if private_theme', (ROOT / "main.py").read_text(encoding="utf-8"))
        self.assertIn(".avatar > img", css)


if __name__ == "__main__":
    unittest.main()
