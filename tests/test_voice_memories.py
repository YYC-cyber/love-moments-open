from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from love_moments.intelligent_ui import ConflictError
from love_moments.store import DEMO_PARTNER, DEMO_USER, LoveMomentsStore


def minimal_wav() -> bytes:
    return b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little") + b"\x01\x00\x01\x00\x40\x1f\x00\x00\x80\x3e\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"


class VoiceMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LoveMomentsStore(Path(self.temp.name) / "voice.db", seed_demo_data=True)

    def _audio(self, owner: str = DEMO_USER) -> dict:
        return self.store.save_media(minimal_wav(), "voice.wav", "audio/wav", "private", owner)

    def _payload(self, media_id: str, **overrides: object) -> dict:
        value = {
            "media_id": media_id,
            "title": "今晚想说的话",
            "note": "",
            "occurred_at": "2026-10-27T20:30:00+08:00",
            "location": "",
            "mood": "想念",
            "visibility": "shared",
            "reveal_at": None,
            "duration_ms": 1200,
        }
        value.update(overrides)
        return value

    def test_voice_memory_keeps_original_audio_when_transcription_unavailable(self) -> None:
        media = self._audio()
        created = self.store.create_voice_memory(self._payload(media["id"]), DEMO_USER, "voice-create-no-text")
        self.assertIsNone(created["transcript"])
        self.assertEqual(created["moment"]["type"], "voice")
        self.assertEqual(created["media"]["duration_ms"], 1200)
        owner = self.store.get_voice_memory(created["moment"]["id"], DEMO_USER)
        partner = self.store.get_voice_memory(created["moment"]["id"], DEMO_PARTNER)
        self.assertIsNone(owner["transcript"])
        self.assertEqual(partner["media"]["id"], media["id"])
        content, mime, _ = self.store.get_media_content(media["id"], DEMO_PARTNER)
        self.assertEqual(content, minimal_wav())
        self.assertEqual(mime, "audio/wav")

    def test_chrome_audio_webm_is_stored_as_audio_not_video(self) -> None:
        ebml = b"\x1aE\xdf\xa3" + b"\x9fB\x86\x81\x01webm-opus-fixture"
        audio = self.store.save_media(ebml, "recording.webm", "audio/webm;codecs=opus", "private", DEMO_USER)
        self.assertEqual((audio["kind"], audio["mime_type"]), ("audio", "audio/webm"))
        self.assertTrue(audio["original_name"].endswith(".webm"))
        video = self.store.save_media(ebml + b"video", "clip.webm", "video/webm", "private", DEMO_USER)
        self.assertEqual((video["kind"], video["mime_type"]), ("video", "video/webm"))
        with self.assertRaises(ValueError):
            self.store.save_media(ebml + b"mismatch", "bad.webm", "audio/wav", "private", DEMO_USER)
        # Chrome on macOS can report MediaRecorder audio as audio/mp4 while
        # using the generic ISO base media brand instead of an M4A brand.
        isom = b"\x00\x00\x00\x24ftypisom\x00\x00\x02\x00isomiso2mp41"
        chrome_mp4 = self.store.save_media(isom, "recording.m4a", "audio/mp4", "private", DEMO_USER)
        self.assertEqual((chrome_mp4["kind"], chrome_mp4["mime_type"]), ("audio", "audio/mp4"))
        video_mp4 = self.store.save_media(isom + b"video", "clip.mp4", "video/mp4", "private", DEMO_USER)
        self.assertEqual((video_mp4["kind"], video_mp4["mime_type"]), ("video", "video/mp4"))

    def test_voice_creation_is_atomic_and_target_bound_idempotent(self) -> None:
        media = self._audio()
        payload = self._payload(media["id"], transcript={
            "body": "真实识别文字", "language": "zh-CN", "method": "browser", "status": "ready",
        })
        original_audit = self.store._audit
        with patch.object(self.store, "_audit", side_effect=RuntimeError("forced audit failure")):
            with self.assertRaises(RuntimeError):
                self.store.create_voice_memory(payload, DEMO_USER, "voice-atomic-failure")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM moments WHERE media_id=?", (media["id"],)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT visibility FROM media_assets WHERE id=?", (media["id"],)).fetchone()[0], "private")
        self.store._audit = original_audit
        first = self.store.create_voice_memory(payload, DEMO_USER, "voice-idempotent-target")
        second = self.store.create_voice_memory(payload, DEMO_USER, "voice-idempotent-target")
        self.assertEqual(first["moment"]["id"], second["moment"]["id"])
        changed = dict(payload, title="另一条内容")
        with self.assertRaises(ConflictError):
            self.store.create_voice_memory(changed, DEMO_USER, "voice-idempotent-target")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM moments WHERE media_id=?", (media["id"],)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM voice_transcripts WHERE media_id=?", (media["id"],)).fetchone()[0], 1)

    def test_transcript_binding_and_effective_visibility_fail_closed(self) -> None:
        partner_media = self._audio(DEMO_PARTNER)
        with self.assertRaises(PermissionError):
            self.store.create_voice_memory(self._payload(partner_media["id"]), DEMO_USER, "voice-partner-media")
        own = self._audio()
        future = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()
        hidden = self.store.create_voice_memory(self._payload(
            own["id"], visibility="delayed", reveal_at=future,
            transcript={"body": "不会提前公开的秘密", "language": "zh-CN", "method": "manual", "status": "ready"},
        ), DEMO_USER, "voice-delayed")
        with self.assertRaises(KeyError):
            self.store.get_voice_memory(hidden["moment"]["id"], DEMO_PARTNER)
        partner_search = self.store.search_memories("不会提前公开的秘密", None, None, "all", None, 20, DEMO_PARTNER)
        self.assertEqual((partner_search["items"], partner_search["total"]), ([], 0))
        owner_search = self.store.search_memories("不会提前公开的秘密", None, None, "all", None, 20, DEMO_USER)
        self.assertEqual([item["resource_id"] for item in owner_search["items"]], [hidden["moment"]["id"]])
        with self.store.connection() as db:
            db.execute("UPDATE moments SET visibility='shared',reveal_at=NULL WHERE id=?", (hidden["moment"]["id"],))
            db.execute("UPDATE media_assets SET visibility='private',reveal_at=NULL WHERE id=?", (own["id"],))
        with self.assertRaises(KeyError):
            self.store.get_voice_memory(hidden["moment"]["id"], DEMO_PARTNER)

    def test_manual_correction_changes_origin_and_checks_version(self) -> None:
        media = self._audio()
        created = self.store.create_voice_memory(self._payload(media["id"], transcript={
            "body": "浏览器原文", "language": "zh-CN", "method": "browser", "status": "ready",
        }), DEMO_USER, "voice-correction")
        transcript = created["transcript"]
        self.assertEqual((transcript["method"], transcript["source_type"]), ("browser", "human"))
        changed = self.store.update_voice_transcript(transcript["id"], {
            "body": "我校正后的文字", "language": "zh-CN", "expected_version": transcript["version"],
        }, DEMO_USER)
        self.assertEqual((changed["method"], changed["source_type"], changed["status"]), ("manual", "human", "ready"))
        with self.assertRaises(PermissionError):
            self.store.update_voice_transcript(transcript["id"], {"body": "越权", "expected_version": changed["version"]}, DEMO_PARTNER)
        with self.assertRaises(ConflictError):
            self.store.update_voice_transcript(transcript["id"], {"body": "旧版本", "expected_version": transcript["version"]}, DEMO_USER)
        cleared = self.store.update_voice_transcript(transcript["id"], {
            "body": "", "language": "zh-CN", "expected_version": changed["version"],
        }, DEMO_USER)
        self.assertEqual((cleared["status"], cleared["method"], cleared["source_type"]), ("draft", "manual", "human"))

    def test_client_cannot_promote_local_browser_text_to_verified_api_source(self) -> None:
        media = self._audio()
        created = self.store.create_voice_memory(self._payload(media["id"], transcript={
            "body": "客户端声称来自浏览器识别", "language": "zh-CN",
            "method": "browser", "status": "ready",
        }), DEMO_USER, "voice-untrusted-browser-source")
        transcript = created["transcript"]
        self.assertEqual(transcript["method"], "browser")
        self.assertEqual(transcript["source_type"], "human")
        with self.assertRaises(ValueError):
            self.store.update_voice_transcript(transcript["id"], {
                "body": "先人工改动再伪装浏览器识别", "language": "zh-CN",
                "method": "browser", "expected_version": transcript["version"],
            }, DEMO_USER)
        exported = json.dumps(self._export_voice_rows(DEMO_USER), ensure_ascii=False)
        self.assertIn('"method": "browser"', exported)
        self.assertIn('"source_type": "human"', exported)

    def _export_voice_rows(self, user_id: str) -> list[dict]:
        archive, _ = self.store.account_export(user_id)
        with zipfile.ZipFile(io.BytesIO(archive)) as exported:
            return json.loads(exported.read("data.json"))["voice_transcripts"]

    def test_voice_export_uses_effective_parent_visibility(self) -> None:
        shared_media = self._audio()
        shared = self.store.create_voice_memory(self._payload(shared_media["id"], transcript={
            "body": "双方可以导出的文字", "language": "zh-CN", "method": "manual", "status": "ready",
        }), DEMO_USER, "voice-export-shared")
        private_media = self._audio()
        self.store.create_voice_memory(self._payload(private_media["id"], visibility="private", transcript={
            "body": "只有作者导出的语音文字", "language": "zh-CN", "method": "manual", "status": "ready",
        }), DEMO_USER, "voice-export-private")
        partner_zip, _ = self.store.account_export(DEMO_PARTNER)
        with zipfile.ZipFile(io.BytesIO(partner_zip)) as archive:
            text = archive.read("data.json").decode()
            names = set(archive.namelist())
        self.assertIn("双方可以导出的文字", text)
        self.assertNotIn("只有作者导出的语音文字", text)
        self.assertTrue(any(shared_media["id"] in name for name in names))
        self.assertFalse(any(private_media["id"] in name for name in names))
        detail = self.store.get_voice_memory(shared["moment"]["id"], DEMO_PARTNER)
        self.assertNotIn("relative_path", json.dumps(detail, ensure_ascii=False))

    def test_failed_transcript_and_detached_or_trashed_parent_never_leak(self) -> None:
        media = self._audio()
        created = self.store.create_voice_memory(self._payload(media["id"], transcript={
            "body": "", "language": "zh-CN", "method": "browser", "status": "failed",
        }), DEMO_USER, "voice-failed-transcript")
        self.assertEqual(created["transcript"]["status"], "failed")
        result = self.store.search_memories("识别失败", None, None, "all", None, 20, DEMO_PARTNER)
        self.assertEqual((result["items"], result["total"]), ([], 0))
        with self.store.connection() as db:
            db.execute("UPDATE voice_transcripts SET body='绝不能作为孤立文字公开',status='ready',moment_id=NULL WHERE id=?", (created["transcript"]["id"],))
        detached = self.store.search_memories("绝不能作为孤立文字公开", None, None, "all", None, 20, DEMO_PARTNER)
        self.assertEqual((detached["items"], detached["total"]), ([], 0))
        self.store.trash_moment(created["moment"]["id"], DEMO_USER)
        with self.assertRaises(KeyError):
            self.store.get_voice_memory(created["moment"]["id"], DEMO_PARTNER)

    def test_delayed_transcript_becomes_visible_only_after_parent_reconciliation(self) -> None:
        media = self._audio()
        reveal = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
        created = self.store.create_voice_memory(self._payload(
            media["id"], visibility="delayed", reveal_at=reveal,
            transcript={"body": "到期后才共同可见", "language": "zh-CN", "method": "manual", "status": "ready"},
        ), DEMO_USER, "voice-delayed-due")
        with self.assertRaises(KeyError):
            self.store.get_voice_memory(created["moment"]["id"], DEMO_PARTNER)
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(microsecond=0).isoformat()
        with self.store.connection() as db:
            db.execute("UPDATE moments SET reveal_at=? WHERE id=?", (past, created["moment"]["id"]))
            db.execute("UPDATE media_assets SET reveal_at=? WHERE id=?", (past, media["id"]))
            db.execute("UPDATE voice_transcripts SET reveal_at=? WHERE id=?", (past, created["transcript"]["id"]))
        visible = self.store.get_voice_memory(created["moment"]["id"], DEMO_PARTNER)
        self.assertEqual(visible["transcript"]["body"], "到期后才共同可见")
        result = self.store.search_memories("到期后才共同可见", None, None, "all", None, 20, DEMO_PARTNER)
        self.assertEqual([item["resource_id"] for item in result["items"]], [created["moment"]["id"]])


if __name__ == "__main__":
    unittest.main()
