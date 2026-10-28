from __future__ import annotations

import io
import tempfile
import threading
import unittest
import os
from collections import namedtuple
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from love_moments.providers import OpenAICompatibleTravelPlanner
from love_moments.intelligent_ui import ConflictError
from love_moments.store import DEMO_COUPLE, DEMO_PARTNER, DEMO_TRIP, DEMO_USER, LoveMomentsStore, next_annual_occurrence
from tests import VALID_PNG


class LoveMomentsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(Path(self.temp.name) / "test.db", seed_demo_data=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_bootstrap_contains_complete_shared_space(self) -> None:
        data = self.store.bootstrap()
        self.assertEqual(data["user"]["nickname"], "演示甲")
        self.assertEqual(data["partner"]["nickname"], "演示乙")
        self.assertEqual(data["couple"]["anniversary"], "2024-02-14")
        self.assertEqual(len(data["trip"]["variants"]), 5)
        self.assertGreaterEqual(len(data["messages"]), 3)
        trip_message = next(message for message in data["messages"] if message["type"] == "trip")
        self.assertEqual(trip_message["metadata"]["subtitle"], "湖景房 · ¥ 568 / 晚")

    def test_both_partners_can_bootstrap_before_weekly_review_exists(self) -> None:
        with self.store.connection() as db:
            db.execute("DELETE FROM weekly_review_answers")
            db.execute("DELETE FROM weekly_reviews")
        barrier = threading.Barrier(2)
        results: list[dict] = []
        errors: list[Exception] = []

        def load(user_id: str) -> None:
            try:
                barrier.wait(timeout=3)
                results.append(self.store.bootstrap(user_id))
            except Exception as error:  # pragma: no cover - assertion reports the captured race
                errors.append(error)

        threads = [threading.Thread(target=load, args=(DEMO_USER,)), threading.Thread(target=load, args=(DEMO_PARTNER,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual({item["weeklyReview"]["id"] for item in results}, {results[0]["weeklyReview"]["id"]})
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM weekly_reviews").fetchone()[0], 1)

    def test_testccount_uses_documented_credentials(self) -> None:
        user = self.store.authenticate("test", "test123")
        self.assertEqual(user["nickname"], "演示甲")
        with self.assertRaises(ValueError):
            self.store.authenticate("legacy_demo", "love1234")

    def test_existing_demo_password_is_never_reset_on_restart(self) -> None:
        self.store.change_password("test123", "a-private-password", "a-private-password", DEMO_USER)
        restarted = LoveMomentsStore(self.store.db_path, seed_demo_data=True)
        self.assertEqual(restarted.authenticate("test", "a-private-password")["id"], DEMO_USER)
        with self.assertRaises(ValueError):
            restarted.authenticate("test", "test123")

    def test_demo_mode_can_be_disabled_for_an_empty_personal_database(self) -> None:
        store = LoveMomentsStore(Path(self.temp.name) / "personal.db", seed_demo_data=False)
        with self.assertRaises(ValueError):
            store.authenticate("test", "test123")
        created = store.register("private_user", "a-private-password", "小岛")
        self.assertEqual(created["username"], "private_user")

    def test_demo_seed_never_calls_remote_travel_planner(self) -> None:
        class RemotePlannerMustNotRun:
            def generate_variants(self, destination: str, budget_cents: int):
                raise AssertionError("demo seed must not call a remote planner")

        store = LoveMomentsStore(
            Path(self.temp.name) / "remote-planner-seed.db",
            planner=RemotePlannerMustNotRun(),
            seed_demo_data=True,
        )
        self.assertEqual(len(store.bootstrap()["trip"]["variants"]), 5)

    def test_password_change_keeps_current_session_and_revokes_other_devices(self) -> None:
        current = self.store.create_session(DEMO_USER, "当前设备")
        other = self.store.create_session(DEMO_USER, "旧设备")
        result = self.store.change_password("test123", "a-private-password", "a-private-password", DEMO_USER, current)
        self.assertGreaterEqual(result["revoked_sessions"], 1)
        self.assertIsNotNone(self.store.user_for_session(current))
        self.assertIsNone(self.store.user_for_session(other))
        self.assertEqual(self.store.authenticate("test", "a-private-password")["id"], DEMO_USER)
        with self.assertRaises(ValueError):
            self.store.authenticate("test", "test123")

    def test_push_subscriptions_follow_login_session_and_failure_lifecycle(self) -> None:
        token = self.store.create_session(DEMO_USER, "测试 iPhone")
        endpoint_hash = "a" * 64
        saved = self.store.save_push_subscription(
            endpoint_hash=endpoint_hash,
            subscription_ciphertext="encrypted-subscription",
            user_id=DEMO_USER,
            session_token=token,
            device_name="iPhone · Safari",
            user_agent="Mobile Safari",
        )
        self.assertTrue(saved["subscribed"])
        self.assertTrue(self.store.push_subscription_status(DEMO_USER, endpoint_hash)["subscribed"])
        self.assertFalse(self.store.push_subscription_status(DEMO_PARTNER, endpoint_hash)["subscribed"])
        rows = self.store.active_push_subscriptions(DEMO_USER, endpoint_hash)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subscription_ciphertext"], "encrypted-subscription")
        for _ in range(3):
            self.store.record_push_delivery(rows[0]["id"], success=False, status=503, error="temporary")
        self.assertEqual(self.store.active_push_subscriptions(DEMO_USER, endpoint_hash), [])

    def test_revoking_device_deactivates_its_push_subscription(self) -> None:
        token = self.store.create_session(DEMO_USER, "旧 iPhone")
        endpoint_hash = "b" * 64
        self.store.save_push_subscription(
            endpoint_hash=endpoint_hash,
            subscription_ciphertext="encrypted-subscription",
            user_id=DEMO_USER,
            session_token=token,
            device_name="旧 iPhone",
            user_agent="Mobile Safari",
        )
        self.store.delete_session(token)
        self.assertEqual(self.store.active_push_subscriptions(DEMO_USER), [])

    def test_push_outbox_persists_retries_recovers_and_cancels(self) -> None:
        token = self.store.create_session(DEMO_PARTNER, "伴侣 iPhone")
        self.store.save_push_subscription(
            endpoint_hash="c" * 64,
            subscription_ciphertext="encrypted-subscription",
            user_id=DEMO_PARTNER,
            session_token=token,
            device_name="伴侣 iPhone",
            user_agent="Mobile Safari",
        )
        payload = {
            "type": "incoming_call",
            "title": "恋屿语音来电",
            "body": "对方正在呼叫你，点击进入恋屿接听。",
            "url": "/?screen=chat&call=call_" + "1" * 32,
            "tag": "call-call_" + "1" * 32,
            "call_id": "call_" + "1" * 32,
        }
        queued = self.store.enqueue_push_notification(DEMO_PARTNER, payload)
        self.assertTrue(queued["queued"])
        claimed = self.store.claim_push_notifications()
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["payload"], payload)

        reopened = LoveMomentsStore(self.store.db_path, seed_demo_data=False)
        self.assertEqual(reopened.recover_push_outbox()["recovered"], 1)
        claimed_again = reopened.claim_push_notifications()
        self.assertEqual(len(claimed_again), 1)
        retry = reopened.finish_push_notification(
            claimed_again[0]["id"], delivered=False, error="temporary", retryable=True,
        )
        self.assertEqual(retry["status"], "pending")
        with reopened.connection() as db:
            db.execute("UPDATE push_outbox SET next_attempt_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", claimed_again[0]["id"]))
        third = reopened.claim_push_notifications()[0]
        self.assertEqual(reopened.finish_push_notification(third["id"], delivered=True)["status"], "sent")

        second_payload = dict(payload)
        second_payload["tag"] = "call-call_" + "2" * 32
        second_payload["call_id"] = "call_" + "2" * 32
        second_payload["url"] = "/?screen=chat&call=" + second_payload["call_id"]
        reopened.enqueue_push_notification(DEMO_PARTNER, second_payload)
        self.assertEqual(reopened.cancel_push_notification(second_payload["tag"])["cancelled"], 1)
        self.assertEqual(reopened.push_outbox_status()["counts"]["cancelled"], 1)

    def test_notification_pair_returns_only_the_current_partner_and_actor_label(self) -> None:
        tang = self.store.notification_pair(DEMO_USER)
        chao = self.store.notification_pair(DEMO_PARTNER)
        self.assertEqual(tang, {
            "couple_id": DEMO_COUPLE,
            "actor_id": DEMO_USER,
            "actor_name": "演示甲",
            "partner_id": DEMO_PARTNER,
        })
        self.assertEqual(chao["actor_name"], "演示乙")
        self.assertEqual(chao["partner_id"], DEMO_USER)

    def test_normal_push_outbox_keeps_a_one_hour_delivery_window(self) -> None:
        token = self.store.create_session(DEMO_PARTNER, "伴侣通知设备")
        self.store.save_push_subscription(
            endpoint_hash="d" * 64,
            subscription_ciphertext="encrypted-subscription",
            user_id=DEMO_PARTNER,
            session_token=token,
            device_name="伴侣通知设备",
            user_agent="Mobile Safari",
        )
        queued = self.store.enqueue_push_notification(DEMO_PARTNER, {
            "type": "message",
            "title": "演示甲发来一条消息",
            "body": "打开恋屿查看新消息。",
            "url": "/?screen=chat",
            "tag": "message-msg_12345678",
        }, ttl_seconds=3600)
        self.assertTrue(queued["queued"])
        with self.store.connection() as db:
            row = db.execute("SELECT created_at,expires_at FROM push_outbox WHERE id=?", (queued["job_id"],)).fetchone()
        created = datetime.fromisoformat(row["created_at"])
        expires = datetime.fromisoformat(row["expires_at"])
        self.assertEqual(int((expires - created).total_seconds()), 3600)

    def test_message_is_persisted(self) -> None:
        created = self.store.create_message({"type": "text", "content": "周末一起看日落"})
        self.assertTrue(created["mine"])
        self.assertEqual(self.store.list_messages()[-1]["content"], "周末一起看日落")

    def test_message_idempotency_and_realtime_event(self) -> None:
        payload = {"type": "text", "content": "只发送一次", "idempotency_key": "test:message:0001"}
        first = self.store.create_message(payload)
        second = self.store.create_message(payload)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(sum(item["content"] == "只发送一次" for item in self.store.list_messages()), 1)
        events = self.store.list_realtime_events()
        self.assertTrue(any(item["event_type"] == "message.created" and item["resource_id"] == first["id"] for item in events["items"]))

    def test_status_location_and_private_media_are_scoped(self) -> None:
        status = self.store.set_status({"status_code": "miss_you", "text": "早点见面"})
        self.assertEqual(status["text"], "早点见面")
        partner_context = self.store.get_couple_context(user_id=DEMO_PARTNER)
        shared_status = next(item for item in partner_context["members"] if item["id"] != DEMO_PARTNER)
        self.assertEqual(shared_status["status_code"], "miss_you")
        location = self.store.set_location({"city": "东京", "timezone": "Asia/Tokyo"})
        self.assertEqual(location["city"], "东京")
        context = self.store.get_couple_context()
        self.assertEqual(next(item for item in context["members"] if item["is_me"])["timezone"], "Asia/Tokyo")

        png = VALID_PNG
        media = self.store.save_media(png, "memory.png", "image/png", "private")
        path, mime, size = self.store.get_media(media["id"])
        self.assertTrue(path.is_file())
        self.assertEqual(mime, "image/png")
        self.assertGreater(size, 0)
        with self.assertRaises(PermissionError):
            self.store.get_media(media["id"], user_id=DEMO_PARTNER)
        self.assertTrue(self.store.clear_status()["cleared"])

    def test_audio_video_media_detection_and_limits(self) -> None:
        wav = b"RIFF\x10\x00\x00\x00WAVEfmt " + b"\x00" * 16
        audio = self.store.save_media(wav, "voice.wav", "audio/wav")
        self.assertEqual(audio["kind"], "audio")
        self.assertEqual(audio["mime_type"], "audio/wav")

        mp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20
        video = self.store.save_media(mp4, "memory.mp4", "video/mp4")
        self.assertEqual(video["kind"], "video")
        self.assertEqual(video["mime_type"], "video/mp4")

        png = b"\x89PNG\r\n\x1a\n" + b"content"
        with self.assertRaises(ValueError):
            self.store.save_media(png, "fake.wav", "audio/wav")
        with self.assertRaises(ValueError):
            self.store.save_media(b"\x89PNG\r\n\x1a\n" + b"x" * (20 * 1024 * 1024), "too-large.png", "image/png")

    def test_resumable_video_upload_resume_thumbnail_and_idempotent_completion(self) -> None:
        video = b"\x00\x00\x00\x18ftypisom" + b"video-payload" * 40
        upload = self.store.create_media_upload({
            "name": "our-trip.mp4", "mime_type": "video/mp4", "size_bytes": len(video), "visibility": "shared",
        }, DEMO_USER)
        self.assertEqual(upload["received_size"], 0)
        first = self.store.append_media_upload(upload["id"], video[:127], 0, DEMO_USER)
        self.assertEqual(first["received_size"], 127)
        self.assertEqual(self.store.get_media_upload(upload["id"], DEMO_USER)["received_size"], 127)
        with self.assertRaises(ConflictError) as conflict:
            self.store.append_media_upload(upload["id"], video[127:], 0, DEMO_USER)
        self.assertEqual(conflict.exception.details["expected_offset"], 127)
        with self.assertRaises(KeyError):
            self.store.get_media_upload(upload["id"], DEMO_PARTNER)
        self.store.set_media_upload_thumbnail(upload["id"], VALID_PNG, DEMO_USER)
        self.store.append_media_upload(upload["id"], video[127:], 127, DEMO_USER)
        completed = self.store.complete_media_upload(upload["id"], DEMO_USER)
        self.assertEqual(completed["kind"], "video")
        thumbnail, mime, _ = self.store.get_media_content(completed["id"], DEMO_PARTNER, thumbnail=True)
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(thumbnail.startswith(b"\xff\xd8\xff"))
        self.assertEqual(self.store.complete_media_upload(upload["id"], DEMO_USER)["id"], completed["id"])

    def test_resumable_upload_cancel_checksum_and_capacity_boundaries(self) -> None:
        video = b"\x00\x00\x00\x18ftypisom" + b"x" * 20
        upload = self.store.create_media_upload({
            "name": "wrong.mp4", "mime_type": "video/mp4", "size_bytes": len(video), "sha256": "0" * 64,
        }, DEMO_USER)
        self.store.append_media_upload(upload["id"], video, 0, DEMO_USER)
        with self.assertRaisesRegex(ValueError, "完整性校验失败"):
            self.store.complete_media_upload(upload["id"], DEMO_USER)
        cancelled = self.store.cancel_media_upload(upload["id"], DEMO_USER)
        self.assertTrue(cancelled["cancelled"])
        with self.assertRaisesRegex(ValueError, "已经结束"):
            self.store.append_media_upload(upload["id"], b"x", len(video), DEMO_USER)
        with self.assertRaisesRegex(ValueError, "文件过大"):
            self.store.create_media_upload({"name": "huge.png", "mime_type": "image/png", "size_bytes": 20 * 1024 * 1024 + 1}, DEMO_USER)

    def test_duplicate_media_is_reused_and_capacity_is_reported(self) -> None:
        first = self.store.save_media(VALID_PNG, "same.png", "image/png", user_id=DEMO_USER)
        second = self.store.save_media(VALID_PNG, "same-again.png", "image/png", user_id=DEMO_USER)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["duplicate"])
        usage = self.store.media_usage(DEMO_USER)
        self.assertEqual(usage["owner_bytes"], first["size_bytes"])
        self.assertGreater(usage["owner_quota_bytes"], usage["owner_bytes"])

    def test_media_quota_and_low_disk_pause_new_uploads(self) -> None:
        first = self.store.save_media(VALID_PNG, "quota.png", "image/png", user_id=DEMO_USER)
        with self.store.connection() as db:
            db.execute("UPDATE media_assets SET size_bytes=? WHERE id=?", (1024 * 1024, first["id"]))
        wav = b"RIFF\x10\x00\x00\x00WAVEfmt " + b"\x00" * 16
        with patch.dict(os.environ, {"MEDIA_USER_QUOTA_MB": "1", "MEDIA_COUPLE_QUOTA_MB": "2"}, clear=False):
            with self.assertRaisesRegex(ValueError, "个人媒体空间"):
                self.store.save_media(wav, "quota.wav", "audio/wav", user_id=DEMO_USER)

        mp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20
        DiskUsage = namedtuple("DiskUsage", "total used free")
        with patch("love_moments.store.shutil.disk_usage", return_value=DiskUsage(1024**3, 900 * 1024**2, 100 * 1024**2)):
            with self.assertRaisesRegex(ValueError, "暂停视频上传"):
                self.store.save_media(mp4, "clip.mp4", "video/mp4", user_id=DEMO_PARTNER)

    def test_large_photo_is_resized_on_the_server(self) -> None:
        image = Image.new("RGB", (5000, 20), (130, 80, 90))
        raw = io.BytesIO()
        image.save(raw, format="JPEG", quality=95)
        with patch.dict(os.environ, {"MEDIA_IMAGE_MAX_EDGE": "1280"}, clear=False):
            media = self.store.save_media(raw.getvalue(), "wide.jpg", "image/jpeg", user_id=DEMO_USER)
        self.assertEqual(media["width"], 1280)
        self.assertLess(media["height"], 20)

    def test_image_metadata_thumbnail_delayed_visibility_and_usage_are_private(self) -> None:
        source = io.BytesIO()
        image = Image.new("RGB", (1200, 800), (140, 63, 82))
        exif = Image.Exif()
        exif[0x010F] = "Private Camera Maker"
        exif[0x0110] = "Private Device Model"
        image.save(source, format="JPEG", quality=95, exif=exif)
        uploaded = self.store.save_media(source.getvalue(), "holiday\r\nsecret.jpg", "image/jpeg", "private")

        self.assertTrue(uploaded["metadata_stripped"])
        self.assertEqual((uploaded["width"], uploaded["height"]), (1200, 800))
        original, mime, _ = self.store.get_media_content(uploaded["id"])
        thumbnail, thumb_mime, _ = self.store.get_media_content(uploaded["id"], thumbnail=True)
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(thumb_mime, "image/jpeg")
        self.assertNotEqual(original, thumbnail)
        with Image.open(io.BytesIO(original)) as stored_image:
            self.assertFalse(stored_image.getexif())
        with Image.open(io.BytesIO(thumbnail)) as preview:
            self.assertLessEqual(max(preview.size), 640)
        self.assertNotIn(uploaded["id"], {item["id"] for item in self.store.list_media(DEMO_PARTNER)})
        partner_usage = self.store.media_usage(DEMO_PARTNER)
        self.assertEqual(partner_usage["total_bytes"], 0)
        self.assertNotIn("couple_bytes", partner_usage)

        reveal_at = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0).isoformat()
        self.store.create_moment({"title": "稍后公开", "type": "photo", "visibility": "delayed", "reveal_at": reveal_at, "media_id": uploaded["id"]})
        with self.assertRaises(PermissionError):
            self.store.get_media_content(uploaded["id"], user_id=DEMO_PARTNER)
        with self.store.connection() as db:
            db.execute("UPDATE media_assets SET reveal_at='2020-01-01T00:00:00+00:00' WHERE id=?", (uploaded["id"],))
        self.assertTrue(self.store.get_media_content(uploaded["id"], user_id=DEMO_PARTNER)[0])

    def test_media_access_ticket_and_recycle_bin_are_fail_closed(self) -> None:
        media = self.store.save_media(VALID_PNG, "standalone.png", "image/png", "private")
        ticket = self.store.issue_media_access(media["id"], DEMO_USER, thumbnail=True)
        raw_ticket = ticket["url"].split("ticket=", 1)[1]
        self.assertTrue(self.store.get_media_content(media["id"], thumbnail=True, access_ticket=raw_ticket)[0])
        with self.assertRaises(PermissionError):
            self.store.get_media_content(media["id"], thumbnail=True, access_ticket=raw_ticket + "x")
        with self.assertRaises(PermissionError):
            self.store.get_media_content(media["id"], thumbnail=False, access_ticket=raw_ticket)

        self.store.trash_media(media["id"], DEMO_USER)
        with self.assertRaises(KeyError):
            self.store.get_media_content(media["id"], DEMO_USER)
        trashed = self.store.list_media_trash(DEMO_USER)[0]
        with self.assertRaises(ConflictError):
            self.store.restore_media(media["id"], trashed["version"] - 1, DEMO_USER)
        restored = self.store.restore_media(media["id"], trashed["version"], DEMO_USER)
        self.assertEqual(restored["status"], "ready")
        self.store.trash_media(media["id"], DEMO_USER)
        self.assertTrue(self.store.purge_media(media["id"], DEMO_USER)["purged"])
        with self.assertRaises(KeyError):
            self.store.get_media_content(media["id"], DEMO_USER)

    def test_referenced_media_cannot_be_trashed_and_partner_cannot_republish_it(self) -> None:
        media = self.store.save_media(VALID_PNG, "message.png", "image/png", "shared")
        self.store.create_message({"type": "image", "content": "照片", "metadata": {"mediaId": media["id"]}}, DEMO_USER)
        with self.assertRaisesRegex(ValueError, "消息"):
            self.store.trash_media(media["id"], DEMO_USER)
        with self.assertRaises(PermissionError):
            self.store.create_message({"type": "image", "content": "再次发送", "metadata": {"mediaId": media["id"]}}, DEMO_PARTNER)

    def test_phase_two_boundary_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save_media(b"not-a-real-image", "fake.png", "image/png")
        with self.assertRaises(ValueError):
            self.store.set_status({"status_code": "unknown", "text": "test"})
        with self.assertRaises(ValueError):
            self.store.set_location({"city": "测试", "timezone": "Mars/Olympus"})
        with self.assertRaises(ValueError):
            self.store.create_date_plan({"plan_date": "2026-10-18", "city": "杭州", "start_time": "22:00", "end_time": "12:00", "budget": 500})

    def test_leap_day_anniversary_is_safe_in_non_leap_year(self) -> None:
        self.assertEqual(next_annual_occurrence(date(2024, 2, 29), date(2026, 1, 10)), date(2026, 2, 28))
        self.assertEqual(next_annual_occurrence(date(2024, 2, 29), date(2026, 3, 1)), date(2027, 2, 28))

    def test_weekly_review_reveals_and_creates_one_moment(self) -> None:
        review = self.store.get_weekly_review()
        self.store.answer_weekly_review(review["id"], {"favorite": "一起散步", "gratitude": "谢谢你等我", "next_week": "一起吃饭"})
        partner_view = self.store.get_weekly_review(review["id"], user_id=DEMO_PARTNER)
        self.assertTrue(any(answer.get("locked") for answer in partner_view["answers"]))
        self.store.answer_weekly_review(review["id"], {"favorite": "晚安电话", "gratitude": "谢谢你倾听", "next_week": "一起看电影"}, user_id=DEMO_PARTNER)
        self.store.confirm_weekly_review(review["id"])
        final = self.store.confirm_weekly_review(review["id"], user_id=DEMO_PARTNER)
        self.assertEqual(final["status"], "confirmed")
        self.assertEqual(sum(item["type"] == "weekly_review" for item in self.store.list_moments()), 1)
        repeated = self.store.confirm_weekly_review(review["id"], user_id=DEMO_PARTNER)
        self.assertEqual(repeated["moment_id"], final["moment_id"])

    def test_date_plan_vote_confirm_and_complete_flow(self) -> None:
        plan = self.store.create_date_plan({
            "title": "秋日约会",
            "plan_date": "2026-10-18",
            "city": "杭州",
            "start_time": "14:00",
            "end_time": "21:00",
            "budget": 600,
            "preferences": {"interest": "展览"},
        })
        self.assertEqual(len(plan["variants"]), 5)
        first_variant = plan["variants"][0]["id"]
        second_variant = plan["variants"][1]["id"]
        mine = self.store.vote_date_plan(plan["id"], first_variant)
        self.assertFalse(mine["both_voted"])
        revealed = self.store.vote_date_plan(plan["id"], second_variant, user_id=DEMO_PARTNER)
        self.assertTrue(revealed["both_voted"])
        composed = self.store.compose_date_plan(plan["id"], {"variant_id": first_variant})
        self.assertEqual(composed["status"], "pending_confirmation")
        self.store.confirm_date_plan(plan["id"], composed["version"])
        confirmed = self.store.confirm_date_plan(plan["id"], composed["version"], user_id=DEMO_PARTNER)
        self.assertEqual(confirmed["status"], "confirmed")
        completed = self.store.complete_date_plan(plan["id"], {"note": "一起看了很美的展", "actual_cost": 520})
        self.assertEqual(completed["status"], "completed")
        self.assertIn(completed["moment_id"], {item["id"] for item in self.store.list_moments()})

    def test_date_plan_stale_confirmation_is_rejected(self) -> None:
        plan = self.store.create_date_plan({"plan_date": "2026-10-19", "city": "杭州", "start_time": "14:00", "end_time": "20:00", "budget": 300})
        variant = plan["variants"][0]["id"]
        first = self.store.compose_date_plan(plan["id"], {"variant_id": variant})
        second = self.store.compose_date_plan(plan["id"], {"variant_id": variant})
        self.assertGreater(second["version"], first["version"])
        with self.assertRaises(ConflictError):
            self.store.confirm_date_plan(plan["id"], first["version"])

    def test_message_rejects_unknown_type_and_non_object_metadata(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_message({"type": "script", "content": "test"})
        with self.assertRaises(ValueError):
            self.store.create_message({"type": "text", "content": "test", "metadata": []})

    def test_empty_message_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_message({"content": "  "})

    def test_call_end_creates_timeline_moment(self) -> None:
        call = self.store.start_call()
        self.assertEqual(call["state"], "ringing")
        self.store.answer_call(call["id"], user_id=DEMO_PARTNER)
        answered_at = (datetime.now(timezone.utc) - timedelta(seconds=42)).replace(microsecond=0).isoformat()
        with self.store.connection() as db:
            db.execute("UPDATE call_records SET answered_at=? WHERE id=?", (answered_at, call["id"]))
        ended = self.store.end_call(call["id"], 42, "晚安电话")
        self.assertIn(ended["duration_seconds"], {42, 43})
        self.assertEqual(self.store.list_moments()[0]["type"], "call")

    def test_call_end_is_idempotent(self) -> None:
        call = self.store.start_call()
        self.store.answer_call(call["id"], user_id=DEMO_PARTNER)
        answered_at = (datetime.now(timezone.utc) - timedelta(seconds=12)).replace(microsecond=0).isoformat()
        with self.store.connection() as db:
            db.execute("UPDATE call_records SET answered_at=? WHERE id=?", (answered_at, call["id"]))
        self.store.end_call(call["id"], 12, "第一次结束")
        first_count = len([item for item in self.store.list_moments() if item["type"] == "call"])
        ended = self.store.end_call(call["id"], 99, "重复结束")
        second_count = len([item for item in self.store.list_moments() if item["type"] == "call"])
        self.assertEqual(ended["duration_seconds"], 12)
        self.assertEqual(first_count, second_count)

    def test_abandoned_connected_call_expires_and_unblocks_next_call(self) -> None:
        initial_call_moments = sum(item["type"] == "call" for item in self.store.list_moments())
        call = self.store.start_call()
        self.store.answer_call(call["id"], user_id=DEMO_PARTNER)
        old = (datetime.now(timezone.utc) - timedelta(minutes=3)).replace(microsecond=0).isoformat()
        with self.store.connection() as db:
            db.execute("UPDATE call_records SET answered_at=?, updated_at=? WHERE id=?", (old, old, call["id"]))

        replacement = self.store.start_call()
        self.assertNotEqual(replacement["id"], call["id"])
        with self.store.connection() as db:
            expired = db.execute("SELECT state, note FROM call_records WHERE id=?", (call["id"],)).fetchone()
        self.assertEqual(expired["state"], "ended")
        self.assertEqual(expired["note"], "连接中断自动结束")
        self.assertEqual(sum(item["type"] == "call" for item in self.store.list_moments()), initial_call_moments + 1)

    def test_call_heartbeat_keeps_connected_call_active(self) -> None:
        call = self.store.start_call()
        self.store.answer_call(call["id"], user_id=DEMO_PARTNER)
        old = (datetime.now(timezone.utc) - timedelta(minutes=3)).replace(microsecond=0).isoformat()
        with self.store.connection() as db:
            db.execute("UPDATE call_records SET updated_at=? WHERE id=?", (old, call["id"]))
        heartbeat = self.store.heartbeat_call(call["id"], user_id=DEMO_PARTNER)
        self.assertEqual(heartbeat["state"], "connected")
        self.assertEqual(self.store.get_active_call()["id"], call["id"])

    def test_webrtc_signaling_is_scoped_and_ordered(self) -> None:
        call = self.store.start_call()
        incoming = self.store.get_active_call(user_id=DEMO_PARTNER)
        self.assertEqual(incoming["id"], call["id"])
        self.assertFalse(incoming["is_caller"])

        offer = {"type": "offer", "sdp": "v=0\r\n"}
        created_offer = self.store.create_call_signal(call["id"], {"type": "offer", "payload": offer})
        partner_signals = self.store.list_call_signals(call["id"], user_id=DEMO_PARTNER)
        self.assertEqual(partner_signals["items"][0]["payload"], offer)
        self.assertEqual(partner_signals["latest_sequence"], created_offer["sequence"])
        with self.assertRaises(PermissionError):
            self.store.create_call_signal(call["id"], {"type": "offer", "payload": offer}, user_id=DEMO_PARTNER)
        with self.assertRaises(ValueError):
            self.store.create_call_signal(call["id"], {"type": "offer", "payload": {"type": "offer"}})
        with self.assertRaises(ValueError):
            self.store.create_call_signal(call["id"], {"type": "ice", "payload": {"candidate": ""}})

        answered = self.store.answer_call(call["id"], user_id=DEMO_PARTNER)
        self.assertEqual(answered["state"], "connected")
        answer = {"type": "answer", "sdp": "v=0\r\n"}
        self.store.create_call_signal(call["id"], {"type": "answer", "payload": answer}, user_id=DEMO_PARTNER)
        caller_signals = self.store.list_call_signals(call["id"], after=created_offer["sequence"])
        self.assertEqual(caller_signals["items"][0]["payload"], answer)

        self.store.create_call_signal(call["id"], {"type": "ice", "payload": {"candidate": "candidate:1 1 udp 1 127.0.0.1 9 typ host"}})
        ended = self.store.end_call(call["id"], 8, "真实链路测试", user_id=DEMO_PARTNER)
        self.assertEqual(ended["state"], "ended")
        self.assertIsNone(self.store.get_active_call())
        event_types = {item["event_type"] for item in self.store.list_realtime_events()["items"]}
        self.assertTrue({"call.invited", "call.answered", "call.signal", "call.ended"}.issubset(event_types))

    def test_call_decline_and_cancel_permissions(self) -> None:
        call = self.store.start_call()
        with self.assertRaises(PermissionError):
            self.store.decline_call(call["id"])
        rejected = self.store.decline_call(call["id"], user_id=DEMO_PARTNER)
        self.assertEqual(rejected["state"], "rejected")

        next_call = self.store.start_call()
        with self.assertRaises(PermissionError):
            self.store.cancel_call(next_call["id"], user_id=DEMO_PARTNER)
        cancelled = self.store.cancel_call(next_call["id"])
        self.assertEqual(cancelled["state"], "cancelled")

    def test_call_rejects_invalid_duration_and_note(self) -> None:
        call = self.store.start_call()
        with self.assertRaises(ValueError):
            self.store.end_call(call["id"], None)
        with self.assertRaises(ValueError):
            self.store.end_call(call["id"], 10, None)

    def test_private_moment_is_visible_to_creator(self) -> None:
        moment = self.store.create_moment({"title": "一个小秘密", "visibility": "仅自己"})
        self.assertEqual(moment["visibility"], "private")
        self.assertIn(moment["id"], {item["id"] for item in self.store.list_moments()})
        self.assertNotIn(moment["id"], {item["id"] for item in self.store.list_moments(user_id=DEMO_PARTNER)})

    def test_text_moment_accepts_explicit_null_media_id(self) -> None:
        created = self.store.create_moment({"title": "没有附件的文字瞬间", "media_id": None})
        self.assertIsNone(created["media_url"])
        with self.assertRaises(ValueError):
            self.store.create_moment({"title": "错误媒体", "media_id": ["not-a-media-id"]})

    def test_delayed_moment_is_hidden_from_partner_until_reveal(self) -> None:
        moment = self.store.create_moment({"title": "明天见", "visibility": "延时公开"})
        self.assertNotIn(moment["id"], {item["id"] for item in self.store.list_moments(user_id=DEMO_PARTNER)})

    def test_moment_visibility_can_only_be_changed_by_creator(self) -> None:
        moment = self.store.create_moment({"title": "权限测试"})
        updated = self.store.update_moment_visibility(moment["id"], "private")
        self.assertEqual(updated["visibility"], "private")
        with self.assertRaises(PermissionError):
            self.store.update_moment_visibility(moment["id"], "shared", user_id=DEMO_PARTNER)

    def test_moment_creator_can_edit_content_and_partner_sees_it(self) -> None:
        moment = self.store.create_moment({"title": "原来的标题", "body": "原来的正文"})
        updated = self.store.update_moment(moment["id"], {
            "title": "修改后的标题",
            "body": "修改后的正文",
            "occurred_at": "2026-10-18T20:30:00+08:00",
            "location": "杭州西湖",
            "mood": "开心",
            "visibility": "shared",
            "expected_version": moment["version"],
        })
        self.assertEqual(updated["title"], "修改后的标题")
        self.assertEqual(updated["body"], "修改后的正文")
        self.assertEqual(updated["location"], "杭州西湖")
        self.assertEqual(updated["mood"], "开心")
        self.assertEqual(updated["occurred_at"], "2026-10-18T12:30:00+00:00")
        self.assertEqual(updated["version"], moment["version"] + 1)
        partner_copy = next(item for item in self.store.list_moments(user_id=DEMO_PARTNER) if item["id"] == moment["id"])
        self.assertEqual(partner_copy["title"], "修改后的标题")

    def test_moment_edit_rejects_partner_stale_version_and_invalid_fields(self) -> None:
        moment = self.store.create_moment({"title": "只能我改"})
        with self.assertRaises(PermissionError):
            self.store.update_moment(moment["id"], {"title": "替别人改"}, DEMO_PARTNER)
        updated = self.store.update_moment(moment["id"], {"title": "第一次修改", "expected_version": moment["version"]})
        with self.assertRaises(ConflictError):
            self.store.update_moment(moment["id"], {"title": "覆盖新版本", "expected_version": moment["version"]})
        with self.assertRaises(ValueError):
            self.store.update_moment(moment["id"], {"title": "", "expected_version": updated["version"]})
        with self.assertRaises(ValueError):
            self.store.update_moment(moment["id"], {"occurred_at": "not-a-date", "expected_version": updated["version"]})

    def test_invalid_moment_dates_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_moment({"title": "错误日期", "occurred_at": "not-a-date"})
        with self.assertRaises(ValueError):
            self.store.create_moment({"title": "过去公开", "visibility": "delayed", "reveal_at": "2020-01-01T00:00:00Z"})

    def test_trip_revision_protects_manual_edit(self) -> None:
        trip = self.store.get_trip(DEMO_TRIP)
        item = trip["variants"][0]["days"][0]["items"][0]
        result = self.store.revise_item(item["id"], {"start_time": "10:30"})
        updated_item = result["trip"]["variants"][0]["days"][0]["items"][0]
        self.assertEqual(updated_item["start_time"], "10:30")
        self.assertEqual(updated_item["locked"], 1)
        self.assertEqual(result["revision"], 2)

    def test_trip_regeneration_replaces_only_generated_variants(self) -> None:
        before = self.store.get_trip(DEMO_TRIP)
        regenerated = self.store.regenerate_trip(DEMO_TRIP)
        self.assertEqual(len(regenerated["variants"]), 5)
        self.assertEqual(regenerated["current_revision"], before["current_revision"] + 1)
        self.assertEqual(regenerated["hotels"][0]["id"], before["hotels"][0]["id"])
        self.assertEqual(regenerated["revisions"][0]["author_type"], "rules")
        self.assertEqual(regenerated["variants"][0]["generation_source"], "rules")

    def test_trip_regeneration_preserves_manual_locked_item(self) -> None:
        trip = self.store.get_trip(DEMO_TRIP)
        item = trip["variants"][0]["days"][0]["items"][0]
        self.store.revise_item(item["id"], {"start_time": "12:34", "note": "必须保留"})
        regenerated = self.store.regenerate_trip(DEMO_TRIP)
        protected = regenerated["variants"][0]["days"][0]["items"][0]
        self.assertEqual(protected["start_time"], "12:34")
        self.assertEqual(protected["note"], "必须保留")
        self.assertEqual(protected["locked"], 1)

    def test_invalid_trip_edits_are_rejected(self) -> None:
        item = self.store.get_trip(DEMO_TRIP)["variants"][0]["days"][0]["items"][0]
        for changes in ({"start_time": "25:99"}, {"duration_minutes": 0}, {"position": -1}, {"title": "  "}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.revise_item(item["id"], changes)

    def test_composed_plan_requires_items(self) -> None:
        with self.assertRaises(ValueError):
            self.store.compose_plan(DEMO_TRIP, [])
        with self.assertRaises(ValueError):
            self.store.compose_plan(DEMO_TRIP, "not-a-list")

    def test_wishlist_toggle_is_persisted(self) -> None:
        updated = self.store.toggle_wishlist_item("wish-2", True)
        self.assertIsNotNone(updated["completed_at"])
        item = next(item for item in self.store.get_wishlist()["items"] if item["id"] == "wish-2")
        self.assertIsNotNone(item["completed_at"])

    def test_wishlist_requires_boolean_state(self) -> None:
        with self.assertRaises(ValueError):
            self.store.toggle_wishlist_item("wish-2", "true")

    def test_wishlist_full_crud_is_shared(self) -> None:
        created = self.store.create_wishlist_item({"text": "一起看极光", "note": "冬天"})
        self.assertTrue(any(item["id"] == created["id"] for item in self.store.get_wishlist(DEMO_PARTNER)["items"]))
        updated = self.store.update_wishlist_item(created["id"], {"text": "一起看极光和星空", "note": "冬天", "expected_version": created["version"]}, DEMO_PARTNER)
        self.assertEqual(updated["text"], "一起看极光和星空")
        removed = self.store.delete_wishlist_item(created["id"], updated["version"])
        self.assertTrue(removed["deleted"])
        self.assertFalse(any(item["id"] == created["id"] for item in self.store.get_wishlist()["items"]))

    def test_shared_content_detail_and_comments_are_visible_to_both(self) -> None:
        wish = self.store.create_wishlist_item({"text": "一起去露营", "note": "带上相机"}, DEMO_USER)
        mine = self.store.get_shared_content("wishlist_item", wish["id"], DEMO_USER)
        partner = self.store.get_shared_content("wishlist_item", wish["id"], DEMO_PARTNER)
        self.assertEqual((mine["title"], mine["summary"]), ("一起去露营", "带上相机"))
        self.assertEqual(partner["resource_id"], wish["id"])

        first = self.store.create_shared_comment("wishlist_item", wish["id"], "我来准备帐篷", DEMO_USER)
        partner_view = self.store.get_shared_content("wishlist_item", wish["id"], DEMO_PARTNER)
        self.assertEqual(partner_view["comments"][0]["body"], "我来准备帐篷")
        self.assertFalse(partner_view["comments"][0]["mine"])
        with self.assertRaises(PermissionError):
            self.store.delete_shared_comment(first["id"], first["version"], DEMO_PARTNER)

        reply = self.store.create_shared_comment("wishlist_item", wish["id"], "我来准备吃的", DEMO_PARTNER)
        creator_view = self.store.get_shared_content("wishlist_item", wish["id"], DEMO_USER)
        self.assertEqual([item["body"] for item in creator_view["comments"]], ["我来准备帐篷", "我来准备吃的"])
        self.assertFalse(creator_view["comments"][1]["mine"])
        with self.assertRaises(ConflictError):
            self.store.delete_shared_comment(first["id"], first["version"] + 1, DEMO_USER)
        self.assertTrue(self.store.delete_shared_comment(first["id"], first["version"], DEMO_USER)["deleted"])
        self.assertEqual(self.store.get_shared_content("wishlist_item", wish["id"], DEMO_PARTNER)["comment_count"], 1)
        self.assertEqual(reply["author_name"], "演示乙")

        private_moment = self.store.create_moment({"title": "只给自己", "visibility": "private"}, DEMO_USER)
        with self.assertRaises(KeyError):
            self.store.get_shared_content("moment", private_moment["id"], DEMO_PARTNER)

    def test_every_supported_shared_resource_resolves_real_detail(self) -> None:
        bootstrap = self.store.bootstrap(DEMO_USER)
        shared_moment = next(item for item in bootstrap["moments"] if item["visibility"] == "shared")
        trip = bootstrap["trip"]
        itinerary_item = trip["variants"][0]["days"][0]["items"][0]
        wish = self.store.create_wishlist_item({"text": "一起看展", "note": "周末"}, DEMO_USER)
        calendar = self.store.create_calendar_entry({
            "entry_type": "event", "title": "共同看展", "starts_at": "2027-01-18", "all_day": True,
            "recurrence": "none", "location": "美术馆", "note": "提前预约",
        }, DEMO_USER)
        preference = self.store.create_preference({
            "category": "口味", "label": "辣度", "value": "微辣", "note": "不要花椒",
        }, DEMO_USER)
        date_plan = self.store.create_date_plan({
            "title": "周末约会", "city": "上海", "plan_date": "2027-02-14",
            "start_time": "14:00", "end_time": "21:00", "budget": 600,
        }, DEMO_USER)
        recipe = self.store.list_recipes(DEMO_USER, query="番茄炒蛋", limit=1)["items"][0]
        meal_wish = self.store.create_meal_wish({"recipe_id": recipe["id"], "note": "今晚想吃"}, DEMO_USER)
        meal_item = self.store.add_meal_cart_item({
            "recipe_id": recipe["id"], "planned_date": "2027-02-15", "meal_slot": "dinner",
            "servings": 2, "idempotency_key": "shared-detail-all-resources",
        }, DEMO_USER)
        probes = {
            "moment": shared_moment["id"], "trip": trip["id"], "itinerary_item": itinerary_item["id"],
            "meal_wish": meal_wish["id"], "meal_item": meal_item["id"], "wishlist_item": wish["id"],
            "calendar_entry": calendar["id"], "preference": preference["id"], "date_plan": date_plan["id"],
        }
        for resource_type, resource_id in probes.items():
            with self.subTest(resource_type=resource_type):
                mine = self.store.get_shared_content(resource_type, resource_id, DEMO_USER)
                partner = self.store.get_shared_content(resource_type, resource_id, DEMO_PARTNER)
                self.assertEqual(mine["resource_id"], resource_id)
                self.assertEqual(partner["title"], mine["title"])
                self.assertIsInstance(mine["fields"], list)

    def test_daily_answers_unlock_only_after_both_answer(self) -> None:
        before = self.store.get_daily_question()
        self.assertFalse(before["answered_by_me"])
        after = self.store.answer_daily_question("想去看极光")
        self.assertEqual(len(after["answers"]), 2)

    def test_daily_question_matches_for_both_members(self) -> None:
        mine = self.store.get_daily_question()
        partner = self.store.get_daily_question(user_id=DEMO_PARTNER)
        self.assertEqual(mine["question"], partner["question"])
        self.assertTrue(mine["question"].endswith("？"))

    def test_daily_answer_rejects_non_text_and_excessive_length(self) -> None:
        with self.assertRaises(ValueError):
            self.store.answer_daily_question(None)
        with self.assertRaises(ValueError):
            self.store.answer_daily_question("很" * 1001)

    def test_password_registration_and_session(self) -> None:
        user = self.store.register("new_user", "safe-pass-123", "小岛")
        self.assertNotIn("password_hash", user)
        authenticated = self.store.authenticate("new_user", "safe-pass-123")
        self.assertEqual(authenticated["id"], user["id"])
        token = self.store.create_session(user["id"])
        self.assertEqual(self.store.user_for_session(token)["id"], user["id"])
        self.store.delete_session(token)
        self.assertIsNone(self.store.user_for_session(token))

    def test_session_count_is_capped_per_user(self) -> None:
        user = self.store.register("session_user", "safe-pass-123", "会话")
        tokens = [self.store.create_session(user["id"]) for _ in range(12)]
        self.assertIsNone(self.store.user_for_session(tokens[0]))
        self.assertIsNotNone(self.store.user_for_session(tokens[-1]))
        with self.store.connection() as db:
            count = db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (user["id"],)).fetchone()[0]
        self.assertEqual(count, 10)

    def test_invalid_password_is_rejected(self) -> None:
        self.store.register("safe_user", "safe-pass-123", "安全")
        with self.assertRaises(ValueError):
            self.store.authenticate("safe_user", "wrong-password")

    def test_two_registered_users_can_pair_once(self) -> None:
        first = self.store.register("first_user", "first-pass-123", "一一")
        second = self.store.register("second_user", "second-pass-123", "二二")
        invite = self.store.create_user_invite(first["id"])
        pairing = self.store.accept_user_invite(second["id"], invite["code"], "2024-01-01")
        self.assertTrue(pairing["paired"])
        self.assertFalse(self.store.bootstrap(first["id"])["needs_pairing"])
        self.assertIsNone(self.store.bootstrap(first["id"])["trip"])
        self.assertEqual(self.store.bootstrap(first["id"])["couple"]["anniversary"], "2024-01-01")
        with self.assertRaises(ValueError):
            self.store.accept_user_invite(second["id"], invite["code"], "2024-01-01")

    def test_paired_users_can_write_messages_concurrently(self) -> None:
        first = self.store.register("chat_first", "first-pass-123", "小满")
        second = self.store.register("chat_second", "second-pass-123", "小安")
        invite = self.store.create_user_invite(first["id"])
        self.store.accept_user_invite(second["id"], invite["code"], "2024-01-01")
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def send(user_id: str, label: str) -> None:
            try:
                barrier.wait()
                for number in range(5):
                    self.store.create_message({"type": "text", "content": f"{label}-{number}"}, user_id=user_id)
            except Exception as error:
                errors.append(error)

        threads = [
            threading.Thread(target=send, args=(first["id"], "A")),
            threading.Thread(target=send, args=(second["id"], "B")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(errors)
        first_view = self.store.list_messages(user_id=first["id"])
        second_view = self.store.list_messages(user_id=second["id"])
        self.assertEqual(len(first_view), 10)
        self.assertEqual([item["id"] for item in first_view], [item["id"] for item in second_view])
        self.assertEqual({item["sender_name"] for item in first_view}, {"小满", "小安"})

    def test_new_invite_invalidates_previous_invite(self) -> None:
        creator = self.store.register("invite_owner", "owner-pass-123", "邀请人")
        guest = self.store.register("invite_guest", "guest-pass-123", "访客")
        old_invite = self.store.create_user_invite(creator["id"])
        new_invite = self.store.create_user_invite(creator["id"])
        with self.assertRaises(ValueError):
            self.store.accept_user_invite(guest["id"], old_invite["code"], "2024-01-01")
        self.assertTrue(self.store.accept_user_invite(guest["id"], new_invite["code"], "2024-01-01")["paired"])

    def test_invite_acceptance_is_atomic_under_concurrency(self) -> None:
        creator = self.store.register("race_owner", "owner-pass-123", "邀请人")
        first = self.store.register("race_first", "first-pass-123", "甲")
        second = self.store.register("race_second", "second-pass-123", "乙")
        invite = self.store.create_user_invite(creator["id"])
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def accept(user_id: str) -> None:
            barrier.wait()
            try:
                self.store.accept_user_invite(user_id, invite["code"], "2024-01-01")
                outcomes.append("paired")
            except ValueError:
                outcomes.append("rejected")

        threads = [threading.Thread(target=accept, args=(first["id"],)), threading.Thread(target=accept, args=(second["id"],))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertCountEqual(outcomes, ["paired", "rejected"])

    def test_other_couple_cannot_read_demo_trip(self) -> None:
        first = self.store.register("other_first", "first-pass-123", "甲")
        second = self.store.register("other_second", "second-pass-123", "乙")
        invite = self.store.create_user_invite(first["id"])
        self.store.accept_user_invite(second["id"], invite["code"], "2024-01-01")
        with self.assertRaises(KeyError):
            self.store.get_trip(DEMO_TRIP, user_id=first["id"])

    def test_trip_can_be_mixed_and_confirmed_by_both_members(self) -> None:
        trip = self.store.get_trip(DEMO_TRIP)
        selected = [variant["days"][0]["items"][0]["id"] for variant in trip["variants"][:2]]
        composed = self.store.compose_plan(DEMO_TRIP, selected)
        first = self.store.approve_trip(DEMO_TRIP, composed["revision"])
        second = self.store.approve_trip(DEMO_TRIP, composed["revision"], user_id=DEMO_PARTNER)
        self.assertEqual(first["approvals"], 1)
        self.assertTrue(second["confirmed"])

    def test_whole_trip_variant_can_be_adopted_and_confirmed_by_both_members(self) -> None:
        before = self.store.get_trip(DEMO_TRIP)
        adopted = self.store.select_variant(DEMO_TRIP, "classic")
        self.assertEqual(adopted["active_variant"], "classic")
        self.assertEqual(adopted["status"], "pending_approval")
        self.assertEqual(adopted["current_revision"], before["current_revision"] + 1)
        self.assertEqual(adopted["approval_state"]["approved_count"], 0)
        self.assertEqual(adopted["revisions"][0]["reason"], "采用整套方案")
        first = self.store.approve_trip(DEMO_TRIP, adopted["current_revision"])
        after_first = self.store.get_trip(DEMO_TRIP)
        self.assertEqual(first["approvals"], 1)
        self.assertTrue(after_first["approval_state"]["approved_by_me"])
        second = self.store.approve_trip(DEMO_TRIP, adopted["current_revision"], user_id=DEMO_PARTNER)
        self.assertTrue(second["confirmed"])
        self.assertEqual(self.store.get_trip(DEMO_TRIP)["status"], "confirmed")

    def test_model_response_validation_keeps_five_plan_contract(self) -> None:
        planner = OpenAICompatibleTravelPlanner("test", "https://example.invalid", "test")
        rows = []
        for style, name in (("slow", "松弛浪漫"), ("classic", "经典充实"), ("value", "性价比"), ("food", "美食寻味"), ("nature", "自然治愈")):
            rows.append({
                "style": style,
                "name": name,
                "summary": "测试方案",
                "budget_cny": 1000,
                "intensity": "适中",
                "transport_minutes": 60,
                "day_themes": [{"day_number": day, "theme": f"{name}主题{day}"} for day in range(1, 4)],
                "items": [
                    {"day_number": day, "start_time": time, "title": f"{name}第{day}天地点{index}", "duration_minutes": 60, "cost_cny": 10, "details": {"activities": ["体验项目", "拍照留念"], "highlights": ["主题鲜明", "动线顺畅"], "recommendation_reason": "符合当天路线和两个人的游览节奏。", "practical_tips": ["确认开放时间"], "best_time": "上午", "reservation_note": "以官方信息为准"}}
                    for day in range(1, 4)
                    for index, time in enumerate(("09:00", "14:00"), 1)
                ],
            })
        plans = planner._validate({"plans": rows})
        self.assertEqual([plan.style for plan in plans], ["slow", "classic", "value", "food", "nature"])
        self.assertEqual(plans[0].items[0]["cost_cents"], 1000)
        self.assertEqual(plans[0].items[0]["details"]["activities"], ["体验项目", "拍照留念"])


if __name__ == "__main__":
    unittest.main()
