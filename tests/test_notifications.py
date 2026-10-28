from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from love_moments.store import DEMO_PARTNER, DEMO_USER, LoveMomentsStore


class InAppNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(
            Path(self.temp.name) / "notifications.db", seed_demo_data=True
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_for_partner(self, *, dedupe_key: str = "assistant-plan:one") -> dict:
        return self.store.create_in_app_notification(
            recipient_id=DEMO_PARTNER,
            actor_id=DEMO_USER,
            event_type="assistant.plan_created",
            resource_type="assistant_plan",
            resource_id="assistant_plan_one",
            title="新的共同方案",
            body="演示甲创建了一份待确认方案",
            url="/?screen=home&assistant-plan=assistant_plan_one",
            dedupe_key=dedupe_key,
        )

    def test_unread_state_is_per_user_and_can_be_marked_or_archived(self) -> None:
        item = self.create_for_partner()
        partner = self.store.list_in_app_notifications(DEMO_PARTNER)
        mine = self.store.list_in_app_notifications(DEMO_USER)
        self.assertEqual(partner["unread_count"], 1)
        self.assertEqual(len(partner["items"]), 1)
        self.assertEqual(mine["unread_count"], 0)

        marked = self.store.mark_in_app_notification_read(item["id"], DEMO_PARTNER)
        self.assertTrue(marked["read"])
        self.assertEqual(
            self.store.list_in_app_notifications(DEMO_PARTNER)["unread_count"], 0
        )

        archived = self.store.archive_in_app_notification(
            item["id"], DEMO_PARTNER
        )
        self.assertTrue(archived["archived"])
        self.assertEqual(
            self.store.list_in_app_notifications(DEMO_PARTNER)["items"], []
        )

    def test_dedupe_key_does_not_create_a_second_inbox_item(self) -> None:
        first = self.create_for_partner()
        second = self.create_for_partner()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            len(self.store.list_in_app_notifications(DEMO_PARTNER)["items"]), 1
        )

    def test_mark_all_only_affects_current_recipient(self) -> None:
        self.create_for_partner(dedupe_key="assistant-plan:partner")
        self.store.create_in_app_notification(
            recipient_id=DEMO_USER,
            actor_id=DEMO_PARTNER,
            event_type="assistant.plan_updated",
            resource_type="assistant_plan",
            resource_id="assistant_plan_two",
            title="共同方案有更新",
            body="演示乙调整了待确认方案",
            url="/?screen=home&assistant-plan=assistant_plan_two",
            dedupe_key="assistant-plan:mine",
        )
        result = self.store.mark_all_in_app_notifications_read(DEMO_PARTNER)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(
            self.store.list_in_app_notifications(DEMO_PARTNER)["unread_count"], 0
        )
        self.assertEqual(
            self.store.list_in_app_notifications(DEMO_USER)["unread_count"], 1
        )

    def test_notification_rejects_external_urls_and_oversized_copy(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_in_app_notification(
                recipient_id=DEMO_PARTNER,
                actor_id=DEMO_USER,
                event_type="assistant.plan_created",
                resource_type="assistant_plan",
                resource_id="assistant_plan_three",
                title="不安全跳转",
                body="摘要",
                url="https://example.com/private",
                dedupe_key="assistant-plan:unsafe",
            )
        with self.assertRaises(ValueError):
            self.store.create_in_app_notification(
                recipient_id=DEMO_PARTNER,
                actor_id=DEMO_USER,
                event_type="assistant.plan_created",
                resource_type="assistant_plan",
                resource_id="assistant_plan_four",
                title="摘要过长",
                body="私密正文" * 100,
                url="/?screen=home",
                dedupe_key="assistant-plan:oversized",
            )

    def test_message_and_comment_create_privacy_safe_partner_notifications(self) -> None:
        secret_message = "消息正文不能复制到通知"
        message = self.store.create_message(
            {"content": secret_message}, DEMO_USER
        )
        inbox = self.store.list_in_app_notifications(DEMO_PARTNER)
        message_notice = next(
            item
            for item in inbox["items"]
            if item["resource_id"] == message["id"]
        )
        self.assertNotIn(secret_message, message_notice["body"])
        self.assertEqual(message_notice["url"], "/?screen=chat")

        wish = self.store.create_wishlist_item(
            {"text": "周末一起散步"}, DEMO_USER
        )
        secret_comment = "评论正文也不能复制到通知"
        comment = self.store.create_shared_comment(
            "wishlist_item", wish["id"], secret_comment, DEMO_USER
        )
        inbox = self.store.list_in_app_notifications(DEMO_PARTNER)
        comment_notice = next(
            item
            for item in inbox["items"]
            if item["dedupe_key"] == f"shared-comment:{comment['id']}"
        )
        self.assertNotIn(secret_comment, comment_notice["body"])
        self.assertIn(wish["id"], comment_notice["url"])
        self.assertIn("shared_type=wishlist_item", comment_notice["url"])
        self.assertIn(f"shared_id={wish['id']}", comment_notice["url"])

    def test_opening_chat_marks_matching_message_notifications_read(self) -> None:
        message = self.store.create_message(
            {"content": "今晚见"}, DEMO_USER
        )
        before = self.store.list_in_app_notifications(DEMO_PARTNER)
        notice = next(
            item for item in before["items"] if item["resource_id"] == message["id"]
        )
        self.assertFalse(notice["read"])
        self.store.mark_messages_read(DEMO_PARTNER)
        after = self.store.list_in_app_notifications(DEMO_PARTNER)
        updated = next(
            item for item in after["items"] if item["resource_id"] == message["id"]
        )
        self.assertTrue(updated["read"])

    def test_call_and_trip_changes_are_durable_partner_notifications(self) -> None:
        call = self.store.start_call(DEMO_USER)
        self.store.cancel_call(call["id"], DEMO_USER)
        self.store.select_variant("demo-trip", "classic", DEMO_USER)
        inbox = self.store.list_in_app_notifications(DEMO_PARTNER)
        by_resource = {
            (item["resource_type"], item["resource_id"]): item
            for item in inbox["items"]
        }
        self.assertIn(("call", call["id"]), by_resource)
        self.assertIn(("trip", "demo-trip"), by_resource)
        self.assertEqual(
            by_resource[("trip", "demo-trip")]["url"],
            "/?screen=trip&shared_type=trip&shared_id=demo-trip",
        )


if __name__ == "__main__":
    unittest.main()
