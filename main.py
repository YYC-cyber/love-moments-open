"""Love Moments full-stack local MVP server."""

from __future__ import annotations

import json
import hashlib
import os
import posixpath
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from love_moments.config import load_env
from love_moments import LoveMomentsStore
from love_moments.providers import date_planner_from_env, travel_planner_from_env
from love_moments.intelligent_ui import ConflictError, ui_composer_from_env
from love_moments.turn import TurnCredentialProvider
from love_moments.shengwang import ShengwangRtcProvider
from love_moments.weather import OpenMeteoWeather
from love_moments.storage import storage_from_env
from love_moments.admin import AdminService
from love_moments.ocr import real_ocr_configured
from love_moments.quota import QuotaExceededError
from love_moments.webpush import WebPushService
from love_moments.life_assistant import life_assistant_from_env


ROOT = Path(__file__).resolve().parent
load_env(ROOT / ".env")
PROTOTYPE = ROOT / "static" / "index.html"
DB_PATH_VALUE = Path(os.getenv("DATABASE_PATH", "data/love_moments.db"))
DB_PATH = DB_PATH_VALUE if DB_PATH_VALUE.is_absolute() else ROOT / DB_PATH_VALUE
MEDIA_ROOT_VALUE = Path(os.getenv("MEDIA_ROOT", "data/media"))
MEDIA_ROOT = MEDIA_ROOT_VALUE if MEDIA_ROOT_VALUE.is_absolute() else ROOT / MEDIA_ROOT_VALUE
DEMO_MODE = os.getenv("DEMO_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}
STORE = LoveMomentsStore(
    DB_PATH,
    planner=travel_planner_from_env(),
    date_planner=date_planner_from_env(),
    ui_composer=ui_composer_from_env(),
    media_root=MEDIA_ROOT,
    storage=storage_from_env(MEDIA_ROOT),
    life_assistant=life_assistant_from_env(),
    seed_demo_data=DEMO_MODE,
)
PUSH = WebPushService(ROOT, MEDIA_ROOT)
WEATHER = OpenMeteoWeather()
TURN = TurnCredentialProvider()
RTC = ShengwangRtcProvider()
ADMIN = AdminService(lambda: STORE, ROOT)
SESSION_COOKIE = "lm_session"
PUBLIC_FILES = {
    "/static/index.html",
    "/manifest.webmanifest",
    "/static/app.css",
    "/static/app.js",
    "/static/admin.html",
    "/static/admin.css",
    "/static/admin.js",
    "/static/vendor/agora-rtc-sdk-ng-4.24.8.js",
    "/static/assets/couple-lake-hero.png",
    "/static/assets/love-moments-icon.svg",
    "/static/assets/love-moments-icon-180.png",
    "/static/assets/love-moments-icon-192.png",
    "/static/assets/love-moments-icon-512.png",
    "/sw.js",
}
RECIPE_COVER_PATH = re.compile(r"/static/assets/recipe-covers/[A-Za-z0-9_-]+\.jpg")
PRIVATE_THEME_FILES = {
    "kitty": "kitty-stickers-v1.png",
    "daniel": "daniel-stickers-v1.png",
    "kitty_pages": "kitty-page-stickers-v2.png",
    "daniel_pages": "daniel-page-stickers-v2.png",
}


def _private_theme_asset(filename: str) -> Path | None:
    """Only installed character artwork, never arbitrary files beside the DB.

    Artwork is separately provisioned in persistent storage, not bundled into
    the public static directory or repository. Missing art is optional: users
    still have the same usable colour themes without it.
    """
    if filename not in PRIVATE_THEME_FILES.values():
        return None
    directory = Path(STORE.db_path).parent / "theme-assets"
    path = directory / filename
    if directory.is_symlink() or path.is_symlink():
        return None
    try:
        if not path.is_file() or not 8 <= path.stat().st_size <= 10 * 1024 * 1024:
            return None
        with path.open("rb") as stream:
            if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
    except OSError:
        return None
    return path


def _is_public_file(path: str) -> bool:
    return path in PUBLIC_FILES or RECIPE_COVER_PATH.fullmatch(path) is not None


class RequestLimiter:
    """Small in-memory guard for password and registration attempts."""

    def __init__(self, limit: int = 12, window_seconds: int = 60):
        self.limit = limit
        self.window_seconds = window_seconds
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[key]
            while attempts and attempts[0] <= now - self.window_seconds:
                attempts.popleft()
            if len(attempts) >= self.limit:
                return False
            attempts.append(now)
            return True


AUTH_LIMITER = RequestLimiter()
PUSH_TEST_LIMITER = RequestLimiter(limit=3, window_seconds=60)


class ProviderFailureTracker:
    """Raise one operational event only after a provider fails repeatedly."""

    def __init__(self) -> None:
        self._failures: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def success(self, provider: str) -> None:
        with self._lock:
            self._failures[provider] = 0
        ADMIN.resolve_event_condition(
            f"provider:{provider}:consecutive-failure",
            kind="provider_success",
            message="外部服务随后已真实调用成功",
        )

    def failure(self, provider: str, label: str, error: Exception) -> None:
        with self._lock:
            self._failures[provider] += 1
            count = self._failures[provider]
        if count == 3 or (count > 3 and count % 10 == 0):
            ADMIN.record_event(
                f"provider:{provider}:consecutive-failure",
                "warning",
                "external-service",
                f"{label}连续调用失败",
                {"provider": provider, "consecutive_failures": count, "error_type": type(error).__name__},
            )


PROVIDER_FAILURES = ProviderFailureTracker()


def _deliver_web_push(rows: list[dict], payload: dict, ttl_seconds: int = 60) -> dict:
    delivered = 0
    failed = 0
    for row in rows:
        try:
            response = PUSH.send(row["subscription_ciphertext"], payload, ttl=ttl_seconds)
            status = int(getattr(response, "status_code", 201) or 201)
            if status < 200 or status >= 300:
                raise RuntimeError(f"推送服务返回 HTTP {status}")
            STORE.record_push_delivery(row["id"], success=True, status=status)
            PROVIDER_FAILURES.success("webpush")
            delivered += 1
        except Exception as error:
            status = PUSH.response_status(error)
            STORE.record_push_delivery(
                row["id"], success=False, status=status,
                error=f"{type(error).__name__}: {error}",
            )
            PROVIDER_FAILURES.failure("webpush", "Web Push 通知", error)
            failed += 1
    return {"delivered": delivered, "failed": failed}


PUSH_OUTBOX_WAKE = threading.Event()


def process_push_outbox_once(limit: int = 10) -> dict:
    """Claim durable jobs and finish or reschedule each one."""
    processed = delivered = retried = failed = 0
    for job in STORE.claim_push_notifications(limit):
        processed += 1
        try:
            rows = STORE.active_push_subscriptions(job["user_id"])
            if not rows:
                STORE.finish_push_notification(
                    job["id"], delivered=False,
                    error="没有有效的设备订阅", retryable=False,
                )
                failed += 1
                continue
            expires_at = datetime.fromisoformat(job["expires_at"])
            remaining_seconds = max(10, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
            result = _deliver_web_push(rows, job["payload"], ttl_seconds=remaining_seconds)
            if result["delivered"]:
                STORE.finish_push_notification(job["id"], delivered=True)
                delivered += 1
            else:
                outcome = STORE.finish_push_notification(
                    job["id"], delivered=False,
                    error=f"{result['failed']} 个设备发送失败",
                    retryable=bool(STORE.active_push_subscriptions(job["user_id"])),
                )
                if outcome["status"] == "pending":
                    retried += 1
                else:
                    failed += 1
        except Exception as error:
            try:
                outcome = STORE.finish_push_notification(
                    job["id"], delivered=False,
                    error=f"{type(error).__name__}: {error}", retryable=True,
                )
                if outcome["status"] == "pending":
                    retried += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
            PROVIDER_FAILURES.failure("webpush", "Web Push 持久队列", error)
    return {"processed": processed, "delivered": delivered, "retried": retried, "failed": failed}


class PushOutboxWorker:
    def __init__(self, interval_seconds: float = 2.0):
        self.interval_seconds = max(0.2, float(interval_seconds))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if not PUSH.enabled or (self.thread and self.thread.is_alive()):
            return
        STORE.recover_push_outbox()
        self.thread = threading.Thread(target=self._run, name="web-push-outbox", daemon=True)
        self.thread.start()

    def wake(self) -> None:
        PUSH_OUTBOX_WAKE.set()

    def stop(self) -> None:
        self.stop_event.set()
        PUSH_OUTBOX_WAKE.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                result = process_push_outbox_once()
                if result["processed"]:
                    continue
            except Exception as error:
                PROVIDER_FAILURES.failure("webpush", "Web Push 队列调度", error)
            PUSH_OUTBOX_WAKE.wait(self.interval_seconds)
            PUSH_OUTBOX_WAKE.clear()


PUSH_OUTBOX_WORKER = PushOutboxWorker()


TRIP_GENERATION_WAKE = threading.Event()


class TripGenerationWorker:
    """Run slow travel-model calls outside HTTP request threads."""

    def __init__(self, interval_seconds: float = 1.0):
        self.interval_seconds = max(0.2, float(interval_seconds))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        STORE.recover_trip_generation_jobs()
        self.thread = threading.Thread(target=self._run, name="trip-generation-worker", daemon=True)
        self.thread.start()

    def wake(self) -> None:
        TRIP_GENERATION_WAKE.set()

    def stop(self) -> None:
        self.stop_event.set()
        TRIP_GENERATION_WAKE.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                job = STORE.claim_trip_generation_job()
                if job:
                    STORE.run_trip_generation_job(job)
                    continue
            except Exception as error:
                PROVIDER_FAILURES.failure("trip-generation", "旅行生成后台任务", error)
            TRIP_GENERATION_WAKE.wait(self.interval_seconds)
            TRIP_GENERATION_WAKE.clear()


TRIP_GENERATION_WORKER = TripGenerationWorker()


class DailySummaryWorker:
    """Generate privacy-safe couple summaries without blocking HTTP requests."""

    def __init__(self, interval_seconds: float = 60.0):
        self.interval_seconds = max(5.0, float(interval_seconds))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(
            target=self._run, name="daily-couple-summary", daemon=True
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                result = STORE.run_due_daily_summaries()
                for delivery in result.get("push_recipients", []):
                    queue_web_push(
                        delivery["user_id"],
                        {
                            "type": "daily_summary",
                            "title": "今晚的双人摘要",
                            "body": "今天的共同事项已经整理好，点击进入恋屿查看。",
                            "url": "/?screen=home",
                            "tag": (
                                "daily-couple-summary-"
                                f"{delivery['local_date']}"
                            ),
                        },
                        ttl_seconds=21600,
                    )
            except Exception as error:
                PROVIDER_FAILURES.failure(
                    "daily-summary", "双人每日摘要", error
                )
            self.stop_event.wait(self.interval_seconds)


DAILY_SUMMARY_WORKER = DailySummaryWorker()


class FutureLetterWorker:
    """Deliver due letters without exposing their title or body to push payloads."""

    def __init__(self, interval_seconds: float = 20.0):
        self.interval_seconds = max(5.0, float(interval_seconds))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="future-letter-worker", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                result = STORE.deliver_due_future_letters(limit=50)
                for delivery in result.get("push_recipients", []):
                    queue_web_push(
                        delivery["user_id"],
                        {
                            "type": "future_letter",
                            "title": "未来信已送达",
                            "body": "一封定时信件已经可以打开，点击进入恋屿查看。",
                            "url": delivery["url"],
                            "tag": delivery["tag"],
                        },
                        ttl_seconds=86400,
                    )
            except Exception as error:
                PROVIDER_FAILURES.failure("future-letter", "未来信投递任务", error)
            self.stop_event.wait(self.interval_seconds)


FUTURE_LETTER_WORKER = FutureLetterWorker()


ALBUM_EXPORT_WAKE = threading.Event()


class AlbumExportWorker:
    """Generate short-lived PDFs outside HTTP request threads."""

    def __init__(self, interval_seconds: float = 2.0):
        self.interval_seconds = max(0.5, float(interval_seconds))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        STORE.recover_album_export_jobs()
        STORE.cleanup_expired_album_exports()
        self.thread = threading.Thread(target=self._run, name="album-export-worker", daemon=True)
        self.thread.start()

    def wake(self) -> None:
        ALBUM_EXPORT_WAKE.set()

    def stop(self) -> None:
        self.stop_event.set()
        ALBUM_EXPORT_WAKE.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)

    def _run(self) -> None:
        last_cleanup = 0.0
        while not self.stop_event.is_set():
            try:
                job = STORE.claim_album_export_job()
                if job:
                    STORE.run_album_export_job(job)
                    continue
                if time.monotonic() - last_cleanup >= 45:
                    STORE.cleanup_expired_album_exports()
                    last_cleanup = time.monotonic()
            except Exception as error:
                PROVIDER_FAILURES.failure("album-export", "纪念册生成任务", error)
            ALBUM_EXPORT_WAKE.wait(self.interval_seconds)
            ALBUM_EXPORT_WAKE.clear()


ALBUM_EXPORT_WORKER = AlbumExportWorker()


def registration_mode() -> str:
    mode = os.getenv("REGISTRATION_MODE", "open").strip().lower()
    return mode if mode in {"open", "closed"} else "closed"


def queue_web_push(user_id: str, payload: dict, ttl_seconds: int = 60) -> int:
    """Persist before returning, then wake the asynchronous dispatcher."""
    if not PUSH.enabled:
        return 0
    queued = STORE.enqueue_push_notification(user_id, dict(payload), ttl_seconds=ttl_seconds, max_attempts=3)
    if queued["queued"]:
        PUSH_OUTBOX_WORKER.wake()
    return int(queued["active_device_count"])


SHARED_PUSH_SURFACES = {
    "moment": "timeline",
    "trip": "trip",
    "itinerary_item": "trip",
    "meal_wish": "life",
    "meal_item": "life",
    "wishlist_item": "life",
    "calendar_entry": "life",
    "preference": "life",
    "date_plan": "life",
}


def shared_push_url(resource_type: str, resource_id: str) -> str:
    surface = SHARED_PUSH_SURFACES.get(resource_type)
    if not surface or not re.fullmatch(r"[A-Za-z0-9_-]{3,160}", str(resource_id)):
        raise ValueError("共同内容通知地址无效")
    return f"/?screen={surface}&shared_type={resource_type}&shared_id={resource_id}"


def queue_partner_web_push(actor_user_id: str, payload: dict, ttl_seconds: int = 3600) -> int:
    """Queue a partner-only notification without failing the saved domain write."""
    if not PUSH.enabled:
        return 0
    try:
        pair = STORE.notification_pair(actor_user_id)
        return queue_web_push(pair["partner_id"], payload, ttl_seconds=ttl_seconds)
    except Exception as error:
        PROVIDER_FAILURES.failure("webpush-queue", "Web Push 互动通知入队", error)
        return 0


def queue_shared_content_web_push(
    actor_user_id: str,
    resource_type: str,
    resource_id: str,
    payload: dict,
) -> int:
    """Queue only when the resource is currently visible to both partners."""
    try:
        STORE.get_shared_content(resource_type, resource_id, actor_user_id)
        value = dict(payload)
        value["url"] = shared_push_url(resource_type, resource_id)
        return queue_partner_web_push(actor_user_id, value)
    except (KeyError, PermissionError, ValueError):
        return 0


def cancel_call_push(call_id: str) -> None:
    STORE.cancel_push_notification(f"call-{call_id}")
    PUSH_OUTBOX_WORKER.wake()


class LoveMomentsHandler(SimpleHTTPRequestHandler):
    """Same-origin REST API and static/PWA hosting."""

    server_version = "LoveMoments/0.2"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, format: str, *args: object) -> None:
        safe_args = tuple(
            re.sub(r"(/api/v1/memories/search)\?[^\s\"]*", r"\1", str(value))
            if "/api/v1/memories/search?" in str(value) else value
            for value in args
        )
        super().log_message(format, *safe_args)

    def _send_json(self, payload: object, status: int = 200, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Mobile browsers and automated sessions may close a request after
            # receiving enough state. That is not an application/API failure.
            self.close_connection = True

    def _session_token(self) -> str | None:
        authorization = self.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
            if token:
                return token
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _allowed_cors_origin(self) -> str | None:
        origin = self.headers.get("Origin", "").strip()
        if not origin:
            return None
        origin_host = urlparse(origin).netloc.lower()
        request_host = self.headers.get("Host", "").strip().lower()
        if origin_host and origin_host == request_host:
            return origin
        port = os.getenv("APP_PORT", "18765").strip() or "18765"
        configured = os.getenv(
            "CORS_ALLOWED_ORIGINS",
            f"http://127.0.0.1:{port},http://localhost:{port}",
        )
        allowed = {value.strip().rstrip("/") for value in configured.split(",") if value.strip()}
        return origin if origin.rstrip("/") in allowed else None

    def _enforce_write_origin(self) -> None:
        origin = self.headers.get("Origin", "").strip()
        if not origin or self._allowed_cors_origin():
            return
        ADMIN.record_event(
            f"origin:blocked:{hashlib.sha256(origin.encode()).hexdigest()[:16]}",
            "warning",
            "security",
            "已拦截未授权网页来源的写请求",
            {"origin": origin[:200], "path": urlparse(self.path).path, "ip_address": self.client_address[0]},
        )
        raise PermissionError("该网页来源未获准修改恋屿数据")

    def _public_https_request(self) -> bool:
        """Return true only when this request arrived through the configured HTTPS host.

        SakuraFrp terminates TLS before forwarding the request to the local HTTP
        server and does not have to add ``X-Forwarded-Proto``.  Comparing the
        original Host header with PUBLIC_BASE_URL lets us issue Secure cookies
        for the public endpoint without breaking direct localhost development.
        """

        forwarded = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
        if forwarded == "https":
            return True
        public = urlparse(os.getenv("PUBLIC_BASE_URL", "").strip())
        request_host = self.headers.get("Host", "").strip().lower()
        return public.scheme.lower() == "https" and bool(public.netloc) and request_host == public.netloc.lower()

    def _request_secure_context(self) -> bool:
        hostname = (urlparse(f"//{self.headers.get('Host', '')}").hostname or "").lower()
        return self._public_https_request() or hostname in {"localhost", "127.0.0.1", "::1"}

    def _current_user(self) -> dict | None:
        return STORE.user_for_session(self._session_token())

    def _require_user(self) -> dict:
        user = self._current_user()
        if not user:
            raise PermissionError("请先登录")
        return user

    def _require_admin(self) -> dict:
        user = self._require_user()
        configured = {
            value.strip().lower()
            for value in os.getenv("ADMIN_USERNAMES", "").split(",")
            if value.strip()
        }
        if user.get("username", "").lower() not in configured:
            raise PermissionError("当前账号没有后台管理权限")
        return user

    def _session_cookie(self, token: str, clear: bool = False) -> str:
        cookie_mode = os.getenv("COOKIE_SECURE", "false").strip().lower()
        secure_cookie = cookie_mode in {"1", "true", "yes", "on"} or (
            cookie_mode == "auto" and self._public_https_request()
        )
        secure = "; Secure" if secure_cookie else ""
        if clear:
            return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}"
        return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000{secure}"

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Content-Length 无效") from error
        if length < 0:
            raise ValueError("Content-Length 无效")
        if length > 1_000_000:
            raise ValueError("请求内容过大")
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("JSON 格式无效") from error
        if not isinstance(value, dict):
            raise ValueError("请求内容必须是对象")
        return value

    def _attach_idempotency_key(self, payload: dict) -> dict:
        header = str(self.headers.get("Idempotency-Key") or "").strip()
        body = str(payload.get("idempotency_key") or "").strip()
        if header and body and header != body:
            raise ValueError("请求头与请求内容中的幂等键不一致")
        payload["idempotency_key"] = header or body
        return payload

    def _read_bytes(self, maximum: int = 200 * 1024 * 1024) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Content-Length 无效") from error
        if length <= 0:
            raise ValueError("上传内容不能为空")
        if length > maximum:
            raise ValueError("上传文件过大")
        value = self.rfile.read(length)
        if len(value) != length:
            raise ValueError("上传内容不完整")
        return value

    def _send_media(self, media_id: str, user: dict, thumbnail: bool = False, ticket: str | None = None) -> None:
        content, mime_type, _ = STORE.get_media_content(media_id, user_id=user["id"], thumbnail=thumbnail, access_ticket=ticket)
        extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp", "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/webm": ".webm", "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"}.get(mime_type, "")
        filename = f"{re.sub(r'[^A-Za-z0-9_-]', '', media_id)[:100]}{'-thumbnail' if thumbnail else ''}{extension}"
        self.send_response(200)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def _send_export(self, user: dict) -> None:
        content, filename = STORE.account_export(user["id"])
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(content)

    def _send_album_export(self, job_id: str, user: dict) -> None:
        content, filename = STORE.get_album_export_download(job_id, user["id"])
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def _device_metadata(self) -> tuple[str, str, str]:
        agent = self.headers.get("User-Agent", "")[:500]
        lowered = agent.lower()
        family = "手机" if any(value in lowered for value in ("iphone", "android", "mobile")) else "电脑"
        browser = "Safari" if "safari" in lowered and "chrome" not in lowered else "Chrome" if "chrome" in lowered else "浏览器"
        forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        return f"{family} · {browser}", agent, forwarded or self.client_address[0]

    def _send_realtime_stream(self, user: dict, after: object = 0) -> None:
        try:
            cursor = max(0, int(after or 0), int(self.headers.get("Last-Event-ID", "0") or 0))
        except ValueError as error:
            raise ValueError("事件序号必须是整数") from error
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        deadline = time.monotonic() + 20
        last_heartbeat = 0.0
        try:
            while time.monotonic() < deadline:
                result = STORE.list_realtime_events(cursor, user_id=user["id"])
                if result["items"]:
                    for item in result["items"]:
                        cursor = item["sequence"]
                        data = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                        self.wfile.write(f"id: {cursor}\nevent: {item['event_type']}\ndata: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                elif time.monotonic() - last_heartbeat >= 5:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    last_heartbeat = time.monotonic()
                cursor = max(cursor, result["latest_sequence"])
                time.sleep(0.35)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def _api_error(self, error: Exception) -> None:
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            self.close_connection = True
            return
        if isinstance(error, QuotaExceededError):
            self._send_json({
                "error": str(error), "code": "EXTERNAL_QUOTA_EXCEEDED",
                "provider": error.provider, "used": error.used, "limit": error.limit,
                "resets_at": error.resets_at,
            }, 429)
        elif isinstance(error, ConflictError):
            body = {"error": str(error), "code": getattr(error, "code", "VERSION_CONFLICT")}
            details = getattr(error, "details", None)
            if details:
                body["details"] = details
            self._send_json(body, 409)
        elif isinstance(error, KeyError):
            self._send_json({"error": str(error).strip("'")}, 404)
        elif isinstance(error, PermissionError):
            status = 401 if str(error) == "请先登录" else 403
            self._send_json({"error": str(error)}, status)
        elif isinstance(error, ValueError):
            self._send_json({"error": str(error)}, 400)
        elif isinstance(error, RuntimeError):
            self._send_json({"error": str(error)}, 503)
        else:
            ADMIN.record_event(
                f"server:{type(error).__name__}:{urlparse(self.path).path}",
                "critical",
                "server",
                "接口出现未处理异常",
                {"path": urlparse(self.path).path, "error_type": type(error).__name__},
            )
            self._send_json({"error": "服务器暂时无法处理请求"}, 500)

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._do_get()
        except Exception as error:
            self._api_error(error)

    def do_HEAD(self) -> None:  # noqa: N802
        """Apply the same static-file allowlist to metadata-only requests."""
        path = posixpath.normpath(unquote(urlparse(self.path).path))
        if path in {"/", "/index.html", "/prototype", "/prototype/"}:
            self.path = "/static/index.html"
        elif path in {"/admin", "/admin/"}:
            self.path = "/static/admin.html"
        elif path.startswith("/api/"):
            self.send_response(405)
            self.send_header("Allow", "GET, POST, PUT, PATCH, DELETE")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif not _is_public_file(path):
            self.send_error(404, "File not found")
            return
        else:
            self.path = path
        super().do_HEAD()

    def _do_get(self) -> None:
        parsed = urlparse(self.path)
        path = posixpath.normpath(unquote(parsed.path))
        query = parse_qs(parsed.query)
        if path in {"/", "/index.html", "/prototype", "/prototype/"}:
            self.path = "/static/index.html"
        elif path in {"/admin", "/admin/"}:
            self.path = "/static/admin.html"
        elif path == "/api/health":
            self._send_json({"status": "ok", "name": "Love Moments", "mode": "full-stack", **STORE.stats()})
            return
        elif path == "/api/v1/auth/me":
            user = self._current_user()
            self._send_json({
                "authenticated": bool(user), "user": user,
                "pairing": STORE.pairing_state(user["id"]) if user else None,
                "registration_enabled": registration_mode() == "open",
            })
            return
        elif path == "/api/v1/admin/overview":
            self._require_admin()
            self._send_json(ADMIN.overview(self._request_secure_context(), self._session_token()))
            return
        elif path == "/api/v1/bootstrap":
            user = self._require_user()
            payload = STORE.bootstrap(user["id"])
            payload["capabilities"] = {
                "llm_live": bool(os.getenv("LLM_API_KEY", "").strip()),
                "booking_live": bool(
                    (os.getenv("BOOKING_API_URL", "").strip() and os.getenv("BOOKING_API_KEY", "").strip())
                    or (os.getenv("SERPAPI_ENABLED", "false").lower() == "true" and os.getenv("SERPAPI_API_KEY", "").strip())
                    or (os.getenv("JUHE_TRAIN_ENABLED", "false").lower() == "true" and os.getenv("JUHE_TRAIN_API_KEY", "").strip())
                ),
                "hotel_flight_search_live": bool(os.getenv("SERPAPI_ENABLED", "false").lower() == "true" and os.getenv("SERPAPI_API_KEY", "").strip()),
                "train_search_live": bool(os.getenv("JUHE_TRAIN_ENABLED", "false").lower() == "true" and os.getenv("JUHE_TRAIN_API_KEY", "").strip()),
                "amap_live": bool(os.getenv("AMAP_WEB_KEY", "").strip()),
                "geoapify_live": bool(
                    os.getenv("GEOAPIFY_ENABLED", "false").lower() == "true"
                    and os.getenv("GEOAPIFY_API_KEY", "").strip()
                ),
                "ocr_live": real_ocr_configured(),
                "rtc_live": RTC.enabled,
                "web_push_live": PUSH.enabled,
                "media_provider": STORE.stats()["media_storage"],
                "recipe_ai_live": bool(os.getenv("LLM_API_KEY", "").strip()),
                "tianapi_recipe_live": bool(
                    os.getenv("TIANAPI_RECIPE_ENABLED", "false").lower() == "true"
                    and os.getenv("TIANAPI_RECIPE_KEY", "").strip()
                ),
                "themealdb_live": os.getenv("THEMEALDB_ENABLED", "true").lower() == "true",
                "spoonacular_live": bool(
                    os.getenv("SPOONACULAR_ENABLED", "false").lower() == "true"
                    and os.getenv("SPOONACULAR_API_KEY", "").strip()
                ),
            }
            self._send_json(payload)
            return
        elif path == "/api/v1/webrtc/config":
            self._require_user()
            self._send_json(TURN.get_config())
            return
        elif path == "/api/v1/rtc/config":
            self._require_user()
            self._send_json(RTC.get_config())
            return
        elif path == "/api/v1/push/config":
            user = self._require_user()
            status = STORE.push_subscription_status(user["id"])
            self._send_json({
                "enabled": PUSH.enabled,
                "public_key": PUSH.public_key if PUSH.enabled else "",
                "reason": PUSH.reason,
                "secure_context": self._request_secure_context(),
                "active_device_count": status["active_device_count"],
                "privacy": "锁屏只显示通用提醒，不包含聊天、留言或私密正文",
            })
            return
        elif path == "/api/v1/calls/active":
            user = self._require_user()
            self._send_json({"call": STORE.get_active_call(user_id=user["id"])})
            return
        elif match := re.fullmatch(r"/api/v1/calls/([^/]+)/signals", path):
            user = self._require_user()
            self._send_json(STORE.list_call_signals(match.group(1), query.get("after", [0])[0], user_id=user["id"]))
            return
        elif path == "/api/v1/messages":
            user = self._require_user()
            raw_limit = query.get("limit", ["50"])[0]
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                raise ValueError("消息数量必须是整数")
            self._send_json(STORE.message_history_page(
                user_id=user["id"],
                limit=limit,
                before_id=query.get("before", [None])[0],
                around_id=query.get("around", [None])[0],
                query=query.get("q", [None])[0],
            ))
            return
        elif path == "/api/v1/assistant/today":
            user = self._require_user()
            self._send_json(STORE.get_life_assistant_today(user["id"]))
            return
        elif path == "/api/v1/assistant/settings":
            user = self._require_user()
            self._send_json(STORE.get_life_assistant_settings(user["id"]))
            return
        elif path == "/api/v1/assistant/daily-summary":
            user = self._require_user()
            self._send_json({
                "summary": STORE.get_daily_couple_summary(
                    user["id"], local_date=query.get("date", [None])[0]
                )
            })
            return
        elif path == "/api/v1/assistant/plans":
            user = self._require_user()
            self._send_json({
                "items": STORE.list_life_assistant_plans(
                    user["id"],
                    status=query.get("status", [""])[0],
                    limit=query.get("limit", [30])[0],
                )
            })
            return
        elif match := re.fullmatch(r"/api/v1/assistant/plans/([^/]+)", path):
            user = self._require_user()
            self._send_json(
                STORE.get_life_assistant_plan(match.group(1), user["id"])
            )
            return
        elif match := re.fullmatch(r"/api/v1/assistant/jobs/([^/]+)", path):
            user = self._require_user()
            self._send_json(STORE.get_life_assistant_job(match.group(1), user["id"]))
            return
        elif path == "/api/v1/notifications":
            user = self._require_user()
            self._send_json(
                STORE.list_in_app_notifications(
                    user["id"],
                    limit=query.get("limit", [50])[0],
                    unread_only=query.get("unread", ["0"])[0] == "1",
                )
            )
            return
        elif path == "/api/v1/notifications/unread-count":
            user = self._require_user()
            inbox = STORE.list_in_app_notifications(user["id"], limit=1)
            self._send_json({"unread_count": inbox["unread_count"]})
            return
        elif path == "/api/v1/realtime/events":
            user = self._require_user()
            self._send_json(STORE.list_realtime_events(query.get("after", [0])[0], user_id=user["id"]))
            return
        elif path == "/api/v1/realtime/stream":
            user = self._require_user()
            self._send_realtime_stream(user, query.get("after", [0])[0])
            return
        elif path == "/api/v1/media/usage":
            user = self._require_user()
            self._send_json(STORE.media_usage(user_id=user["id"]))
            return
        elif path == "/api/v1/media/trash":
            user = self._require_user()
            self._send_json({"items": STORE.list_media_trash(user["id"])})
            return
        elif match := re.fullmatch(r"/api/v1/media/uploads/([^/]+)", path):
            user = self._require_user()
            self._send_json(STORE.get_media_upload(match.group(1), user["id"]))
            return
        elif path == "/api/v1/media":
            user = self._require_user()
            self._send_json({"items": STORE.list_media(user["id"])})
            return
        elif match := re.fullmatch(r"/api/v1/media/([^/]+)", path):
            user = self._require_user()
            self._send_media(match.group(1), user, query.get("thumbnail", ["0"])[0] == "1", query.get("ticket", [None])[0])
            return
        elif path == "/api/v1/couple/context":
            user = self._require_user()
            self._send_json(WEATHER.enrich(STORE.get_couple_context(user_id=user["id"])))
            return
        elif path == "/api/v1/weekly-reviews/current":
            user = self._require_user()
            self._send_json(STORE.get_weekly_review(user_id=user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/weekly-reviews/([^/]+)", path):
            user = self._require_user()
            self._send_json(STORE.get_weekly_review(match.group(1), user_id=user["id"]))
            return
        elif path == "/api/v1/date-plans":
            user = self._require_user()
            self._send_json({"items": STORE.list_date_plans(user_id=user["id"])})
            return
        elif match := re.fullmatch(r"/api/v1/date-plans/([^/]+)", path):
            user = self._require_user()
            self._send_json(STORE.get_date_plan(match.group(1), user_id=user["id"]))
            return
        elif path == "/api/v1/decisions":
            user = self._require_user()
            self._send_json({
                "items": STORE.list_decisions(
                    user["id"], status=query.get("status", [None])[0],
                )
            })
            return
        elif match := re.fullmatch(r"/api/v1/decisions/([^/]+)", path):
            user = self._require_user()
            self._send_json(STORE.get_decision(match.group(1), user["id"]))
            return
        elif path == "/api/v1/moments":
            user = self._require_user()
            self._send_json({"items": STORE.list_moments(user_id=user["id"])})
            return
        elif match := re.fullmatch(r"/api/v1/voice-memories/([^/]+)", path):
            user = self._require_user()
            self._send_json(STORE.get_voice_memory(match.group(1), user["id"]))
            return
        elif path == "/api/v1/future-letters":
            user = self._require_user()
            self._send_json(STORE.list_future_letters(
                user["id"], scope=query.get("scope", ["mine"])[0],
            ))
            return
        elif match := re.fullmatch(r"/api/v1/future-letters/([^/]+)", path):
            user = self._require_user()
            self._send_json(STORE.get_future_letter(match.group(1), user["id"]))
            return
        elif path == "/api/v1/album-exports":
            user = self._require_user()
            self._send_json(STORE.list_album_export_jobs(user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/album-exports/([^/]+)/download", path):
            user = self._require_user()
            self._send_album_export(match.group(1), user)
            return
        elif match := re.fullmatch(r"/api/v1/album-exports/([^/]+)", path):
            user = self._require_user()
            self._send_json(STORE.get_album_export_job(match.group(1), user["id"]))
            return
        elif path == "/api/v1/memories/search":
            user = self._require_user()
            self._send_json(STORE.search_memories(
                keyword=query.get("q", [""])[0],
                start_date=query.get("start_date", [None])[0],
                end_date=query.get("end_date", [None])[0],
                content_type=query.get("type", ["all"])[0],
                person_id=query.get("person_id", [None])[0],
                cursor=query.get("cursor", [None])[0],
                limit=query.get("limit", [20])[0],
                user_id=user["id"],
            ))
            return
        elif path == "/api/v1/memories/completion":
            user = self._require_user()
            self._send_json(STORE.list_memory_completion_suggestions(user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/memories/(moment|trip)/([^/]+)/descriptions", path):
            user = self._require_user()
            self._send_json(STORE.get_memory_descriptions(match.group(1), match.group(2), user["id"]))
            return
        elif path == "/api/v1/moments/trash":
            user = self._require_user()
            self._send_json({"items": STORE.list_moment_trash(user["id"])})
            return
        elif match := re.fullmatch(r"/api/v1/shared-content/([^/]+)/([^/]+)", path):
            user = self._require_user()
            self._send_json(STORE.get_shared_content(match.group(1), match.group(2), user["id"]))
            return
        elif path == "/api/v1/recipes":
            user = self._require_user()
            self._send_json(STORE.list_recipes(
                user["id"], query=query.get("q", [""])[0], cuisine=query.get("cuisine", [""])[0],
                meal_slot=query.get("meal_slot", [""])[0], difficulty=query.get("difficulty", [""])[0],
                max_minutes=query.get("max_minutes", [""])[0], cursor=query.get("cursor", [0])[0], limit=query.get("limit", [20])[0],
            ))
            return
        elif match := re.fullmatch(r"/api/v1/recipes/jobs/([^/]+)", path):
            user = self._require_user()
            self._send_json(STORE.get_recipe_job(match.group(1), user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/recipe-dishes/([^/]+)/variants", path):
            user = self._require_user()
            self._send_json(STORE.list_recipe_variants(match.group(1), user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/recipes/([^/]+)", path):
            user = self._require_user()
            self._send_json(STORE.get_recipe(match.group(1), user["id"]))
            return
        elif path == "/api/v1/meal-wishes":
            user = self._require_user()
            self._send_json({"items": STORE.list_meal_wishes(user["id"])})
            return
        elif path == "/api/v1/meal-cart":
            user = self._require_user()
            self._send_json(STORE.get_meal_cart(user["id"]))
            return
        elif path == "/api/v1/meal-calendar":
            user = self._require_user()
            self._send_json(STORE.meal_calendar(query.get("from", [None])[0], query.get("to", [None])[0], user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/meal-calendar/day/(\d{4}-\d{2}-\d{2})", path):
            user = self._require_user()
            self._send_json(STORE.meal_calendar(match.group(1), match.group(1), user["id"]))
            return
        elif path == "/api/v1/meal-archive":
            user = self._require_user()
            self._send_json(STORE.meal_archive(query.get("year", [None])[0], query.get("month", [None])[0], user["id"]))
            return
        elif path == "/api/v1/shopping-lists/active":
            user = self._require_user()
            self._send_json(STORE.get_shopping_list(user["id"]))
            return
        elif path == "/api/v1/food-profiles":
            user = self._require_user()
            self._send_json(STORE.get_food_profiles(user["id"]))
            return
        elif path == "/api/v1/pantry/items":
            user = self._require_user()
            self._send_json(STORE.list_pantry_items(user["id"]))
            return
        elif path == "/api/v1/shopping-category-order":
            user = self._require_user()
            self._send_json(STORE.get_shopping_category_order(user["id"]))
            return
        elif path == "/api/v1/weekly-meal-plans/active":
            user = self._require_user()
            self._send_json(STORE.get_active_weekly_meal_plan_draft(user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/meal-cart/items/([^/]+)/ingredient-substitutions", path):
            user = self._require_user()
            self._send_json(STORE.list_ingredient_substitutions(match.group(1), user["id"]))
            return
        elif path == "/api/v1/cook-sessions":
            user = self._require_user()
            self._send_json({"items": STORE.list_cook_sessions(
                user["id"], status=query.get("status", ["active"])[0],
                meal_item_id=query.get("meal_item_id", [None])[0],
            )})
            return
        elif match := re.fullmatch(r"/api/v1/cook-sessions/([^/]+)", path):
            user = self._require_user()
            self._send_json(STORE.get_cook_session(match.group(1), user["id"]))
            return
        elif path == "/api/v1/trips":
            user = self._require_user()
            self._send_json({"items": STORE.list_trips(user["id"])})
            return
        elif match := re.fullmatch(r"/api/v1/trip-generation-jobs/([^/]+)", path):
            user = self._require_user()
            self._send_json(STORE.get_trip_generation_job(match.group(1), user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/route-legs", path):
            user = self._require_user()
            self._send_json(STORE.trip_route_legs(match.group(1), user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/negotiation", path):
            user = self._require_user()
            self._send_json(STORE.get_trip_negotiation(match.group(1), user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/realism-checks/current", path):
            user = self._require_user()
            self._send_json(STORE.get_trip_realism_check(match.group(1), user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/packing", path):
            user = self._require_user()
            self._send_json(STORE.list_trip_packing(match.group(1), user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/emergency", path):
            user = self._require_user()
            self._send_json(STORE.list_trip_emergency(match.group(1), user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/expenses", path):
            user = self._require_user()
            self._send_json(STORE.list_trip_expenses(match.group(1), user["id"]), headers={"Cache-Control": "no-store"})
            return
        elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/offline-snapshot", path):
            user = self._require_user()
            self._send_json(STORE.get_trip_offline_snapshot(match.group(1), user["id"]), headers={"Cache-Control": "no-store"})
            return
        elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/assistant", path):
            user = self._require_user()
            self._send_json(STORE.list_trip_assistant(match.group(1), user["id"], query.get("limit", [80])[0]))
            return
        elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/assistant/recap", path):
            user = self._require_user()
            self._send_json(STORE.list_trip_recap_drafts(match.group(1), user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/trips/([^/]+)", path):
            user = self._require_user()
            self._send_json(STORE.get_trip(match.group(1), user_id=user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/chat-imports/([^/]+)", path):
            user = self._require_user()
            self._send_json(STORE.get_chat_import(match.group(1), user["id"]))
            return
        elif path == "/api/v1/calendar":
            user = self._require_user()
            self._send_json({"items": STORE.list_calendar_entries(user["id"], query.get("type", [None])[0])})
            return
        elif path == "/api/v1/preferences":
            user = self._require_user()
            self._send_json({"items": STORE.list_preferences(user["id"])})
            return
        elif path == "/api/v1/timeline/months":
            user = self._require_user()
            self._send_json({"items": STORE.timeline_months(user["id"])})
            return
        elif path == "/api/v1/timeline/footprints":
            user = self._require_user()
            self._send_json(STORE.timeline_footprints(user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/timeline/reports/(\d{4})", path):
            user = self._require_user()
            self._send_json(STORE.annual_report(int(match.group(1)), user["id"]))
            return
        elif path == "/api/v1/account/privacy":
            user = self._require_user()
            self._send_json(STORE.get_privacy_center(user["id"]), headers={"Cache-Control": "no-store"})
            return
        elif path == "/api/v1/account/devices":
            user = self._require_user()
            self._send_json({"items": STORE.list_devices(user["id"], self._session_token())})
            return
        elif path == "/api/v1/account/export.zip":
            self._send_export(self._require_user())
            return
        elif path == "/api/v1/lists/wishlist":
            user = self._require_user()
            self._send_json(STORE.get_wishlist(user_id=user["id"]))
            return
        elif path == "/api/v1/daily-question":
            user = self._require_user()
            self._send_json(STORE.get_daily_question(user_id=user["id"]))
            return
        elif match := re.fullmatch(r"/api/v1/ui/(home|chat|trip|timeline|life)", path):
            user = self._require_user()
            self._send_json(STORE.get_ui_manifest(match.group(1), user_id=user["id"], current_trip_id=query.get("trip_id", [None])[0]))
            return
        elif match := re.fullmatch(r"/api/v1/ui/jobs/([^/]+)", path):
            user = self._require_user()
            self._send_json(STORE.get_ui_job(match.group(1), user_id=user["id"]))
            return
        elif path.startswith("/api/"):
            self._send_json({"error": "接口不存在"}, 404)
            return
        elif not _is_public_file(path):
            self.send_error(404, "File not found")
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            self._enforce_write_origin()
            if path == "/api/v1/media/uploads":
                user = self._require_user()
                self._send_json(STORE.create_media_upload(self._read_json(), user["id"]), 201)
                return
            elif match := re.fullmatch(r"/api/v1/media/uploads/([^/]+)/thumbnail", path):
                user = self._require_user()
                self._send_json(STORE.set_media_upload_thumbnail(match.group(1), self._read_bytes(3 * 1024 * 1024), user["id"]))
                return
            elif match := re.fullmatch(r"/api/v1/media/uploads/([^/]+)/complete", path):
                user = self._require_user()
                self._read_json()
                self._send_json(STORE.complete_media_upload(match.group(1), user["id"]), 201)
                return
            elif path == "/api/v1/media":
                user = self._require_user()
                data = self._read_bytes()
                result = STORE.save_media(
                    data,
                    unquote(self.headers.get("X-Filename", "media")),
                    self.headers.get("Content-Type", "application/octet-stream"),
                    self.headers.get("X-Media-Visibility", "shared"),
                    user_id=user["id"],
                )
                self._send_json(result, 201)
                return
            payload = self._read_json()
            if path == "/api/v1/auth/register":
                if registration_mode() != "open":
                    raise PermissionError("这座恋屿已关闭公开注册；如需新增账号，请由管理员临时开启注册")
                if not AUTH_LIMITER.allow(f"register:{self.client_address[0]}"):
                    ADMIN.record_event("auth:register:rate-limit", "warning", "authentication", "注册请求触发限流", {"ip_address": self.client_address[0]})
                    self._send_json({"error": "尝试次数过多，请稍后再试"}, 429)
                    return
                user = STORE.register(str(payload.get("username", "")), str(payload.get("password", "")), str(payload.get("nickname", "")))
                token = STORE.create_session(user["id"], *self._device_metadata())
                result = {"user": user, "pairing": STORE.pairing_state(user["id"])}
                if payload.get("token_transport") == "bearer":
                    result["access_token"] = token
                self._send_json(result, 201, {"Set-Cookie": self._session_cookie(token)})
                return
            elif path == "/api/v1/auth/login":
                login_username = str(payload.get("username", "")).strip().lower()
                login_ip = self.client_address[0]
                if not AUTH_LIMITER.allow(f"login:{login_ip}") or not STORE.login_attempt_allowed(login_username, login_ip):
                    ADMIN.record_event("auth:login:rate-limit", "warning", "authentication", "登录请求触发限流", {"ip_address": self.client_address[0]})
                    self._send_json({"error": "连续登录失败次数过多，请 15 分钟后再试"}, 429)
                    return
                try:
                    user = STORE.authenticate(login_username, str(payload.get("password", "")))
                except ValueError:
                    STORE.record_login_failure(login_username, login_ip)
                    username = login_username[:30]
                    ADMIN.record_event(
                        f"auth:login:failed:{hashlib.sha256(username.encode()).hexdigest()[:16]}",
                        "warning",
                        "authentication",
                        "账号登录失败",
                        {"username": username, "ip_address": self.client_address[0]},
                    )
                    raise
                STORE.clear_login_failures(login_username)
                ADMIN.resolve_login_failure(str(payload.get("username", "")))
                token = STORE.create_session(user["id"], *self._device_metadata())
                result = {"user": user, "pairing": STORE.pairing_state(user["id"])}
                if payload.get("token_transport") == "bearer":
                    result["access_token"] = token
                self._send_json(result, headers={"Set-Cookie": self._session_cookie(token)})
                return
            elif path == "/api/v1/auth/logout":
                STORE.delete_session(self._session_token())
                self._send_json({"ok": True}, headers={"Set-Cookie": self._session_cookie("", clear=True)})
                return
            user = self._require_user()
            if path == "/api/v1/admin/checks":
                self._require_admin()
                self._send_json(ADMIN.overview(self._request_secure_context(), self._session_token()))
            elif path == "/api/v1/admin/backups":
                admin = self._require_admin()
                self._send_json(ADMIN.create_backup(admin["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/admin/backups/([^/]+)/verify", path):
                admin = self._require_admin()
                self._send_json(ADMIN.verify_backup(match.group(1), admin["id"]))
            elif match := re.fullmatch(r"/api/v1/admin/events/([^/]+)/resolve", path):
                admin = self._require_admin()
                self._send_json(ADMIN.resolve_event(match.group(1), admin["id"]))
            elif match := re.fullmatch(r"/api/v1/admin/sessions/([^/]+)/revoke", path):
                admin = self._require_admin()
                self._send_json(ADMIN.revoke_session(match.group(1), self._session_token(), admin["id"]))
            elif path == "/api/v1/account/password":
                self._send_json(STORE.change_password(
                    payload.get("current_password"),
                    payload.get("new_password"),
                    payload.get("new_password_confirmation"),
                    user["id"],
                    self._session_token(),
                ))
            elif match := re.fullmatch(r"/api/v1/mutations/([^/]+)/undo", path):
                self._send_json(STORE.undo_mutation(match.group(1), user["id"]))
            elif path == "/api/v1/memories/search":
                allowed = {"q", "start_date", "end_date", "type", "person_id", "cursor", "limit"}
                if set(payload) - allowed:
                    raise ValueError("回忆搜索包含不支持的字段")
                self._send_json(STORE.search_memories(
                    keyword=payload.get("q", ""),
                    start_date=payload.get("start_date"),
                    end_date=payload.get("end_date"),
                    content_type=payload.get("type", "all"),
                    person_id=payload.get("person_id"),
                    cursor=payload.get("cursor"),
                    limit=payload.get("limit", 20),
                    user_id=user["id"],
                ))
            elif path == "/api/v1/memories/completion/refresh":
                self._send_json(STORE.refresh_memory_completion_suggestions(user["id"]))
            elif match := re.fullmatch(r"/api/v1/memories/completion/([^/]+)/respond", path):
                payload["idempotency_key"] = self.headers.get("Idempotency-Key") or payload.get("idempotency_key")
                self._send_json(STORE.respond_memory_completion_suggestion(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/memories/moments/([^/]+)/merge-proposal", path):
                self._send_json(STORE.propose_memory_description_merge(match.group(1), payload, user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/memories/moments/([^/]+)/merge-confirm", path):
                self._send_json(STORE.confirm_memory_description_merge(
                    match.group(1), payload.get("proposal_id"), payload.get("expected_version"), user["id"],
                ))
            elif path == "/api/v1/voice-memories":
                idempotency_key = self.headers.get("Idempotency-Key") or payload.pop("idempotency_key", None)
                self._send_json(STORE.create_voice_memory(
                    payload, user["id"], idempotency_key,
                ), 201)
            elif path == "/api/v1/future-letters":
                idempotency_key = self.headers.get("Idempotency-Key") or payload.pop("idempotency_key", None)
                self._send_json(STORE.create_future_letter(
                    payload, user["id"], idempotency_key,
                ), 201)
            elif path == "/api/v1/album-exports/preview":
                self._send_json(STORE.preview_album_export(payload, user["id"]))
            elif path == "/api/v1/album-exports":
                idempotency_key = self.headers.get("Idempotency-Key") or payload.pop("idempotency_key", None)
                result = STORE.create_album_export_job(payload, user["id"], idempotency_key)
                ALBUM_EXPORT_WORKER.wake()
                self._send_json(result, 202)
            elif match := re.fullmatch(r"/api/v1/album-exports/([^/]+)/cancel", path):
                self._send_json(STORE.cancel_album_export_job(
                    match.group(1), payload.get("expected_version"), user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/future-letters/([^/]+)/seal", path):
                self._send_json(STORE.seal_future_letter(
                    match.group(1), payload.get("deliver_at"), payload.get("expected_version"), user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/future-letters/([^/]+)/cancel", path):
                self._send_json(STORE.cancel_future_letter(
                    match.group(1), payload.get("expected_version"), user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/future-letters/([^/]+)/convert-to-moment", path):
                self._send_json(STORE.convert_future_letter_to_moment(
                    match.group(1), payload.get("expected_version"), user["id"],
                    self.headers.get("Idempotency-Key") or payload.get("idempotency_key"),
                ))
            elif path == "/api/v1/push/subscriptions":
                if not PUSH.enabled:
                    raise RuntimeError(PUSH.reason or "Web Push 尚未启用")
                if not self._request_secure_context():
                    raise ValueError("后台提醒只能通过 HTTPS 或本机安全地址开启")
                subscription = PUSH.normalize_subscription(payload.get("subscription"))
                device_name, user_agent, _ = self._device_metadata()
                saved = STORE.save_push_subscription(
                    endpoint_hash=PUSH.endpoint_hash(subscription),
                    subscription_ciphertext=PUSH.seal(subscription),
                    user_id=user["id"],
                    session_token=self._session_token(),
                    device_name=device_name,
                    user_agent=user_agent,
                )
                saved.update(STORE.push_subscription_status(user["id"], PUSH.endpoint_hash(subscription)))
                self._send_json(saved, 201)
            elif path == "/api/v1/push/test":
                if not PUSH.enabled:
                    raise RuntimeError(PUSH.reason or "Web Push 尚未启用")
                if not PUSH_TEST_LIMITER.allow(f"push-test:{user['id']}"):
                    raise ValueError("测试通知过于频繁，请一分钟后再试")
                subscription = PUSH.normalize_subscription(payload.get("subscription"))
                rows = STORE.active_push_subscriptions(user["id"], PUSH.endpoint_hash(subscription))
                if not rows:
                    raise ValueError("当前设备尚未开启后台提醒")
                result = _deliver_web_push(rows, {
                    "type": "test",
                    "title": "恋屿提醒已就绪",
                    "body": "消息、共同留言和来电提醒都可以送达这台设备。",
                    "url": "/?screen=chat",
                    "tag": "push-test",
                })
                if not result["delivered"]:
                    raise RuntimeError("测试通知发送失败，请重新开启提醒后再试")
                self._send_json(result)
            elif path == "/api/v1/messages":
                created = STORE.create_message(payload, user_id=user["id"])
                queue_partner_web_push(user["id"], {
                    "type": "message",
                    "title": "恋屿有一条新消息",
                    "body": "对方给你发来一条消息，点击进入恋屿查看。",
                    "url": "/?screen=chat",
                    "tag": f"message-{created['id']}",
                })
                self._send_json(created, 201)
            elif path == "/api/v1/assistant/jobs":
                job = STORE.create_life_assistant_job(payload, user["id"])
                if job.get("plan"):
                    queue_partner_web_push(
                        user["id"],
                        {
                            "type": "assistant_plan",
                            "title": "恋屿有一份新的共同方案",
                            "body": "对方创建了一份方案，确认前你也可以先查看完整内容。",
                            "url": (
                                "/?screen=home&assistant-plan="
                                f"{job['plan']['id']}"
                            ),
                            "tag": (
                                "assistant-plan-created-"
                                f"{job['plan']['id']}"
                            ),
                        },
                    )
                self._send_json(job, 201)
            elif match := re.fullmatch(
                r"/api/v1/assistant/plans/([^/]+)/confirm", path
            ):
                result = STORE.confirm_life_assistant_plan(
                    match.group(1), payload, user["id"]
                )
                queue_partner_web_push(
                    user["id"],
                    {
                        "type": "assistant_plan",
                        "title": (
                            "共同方案已生效"
                            if result["applied"]
                            else "共同方案等待你的确认"
                        ),
                        "body": (
                            "双方已经确认，安排已写入共同生活。"
                            if result["applied"]
                            else "对方已经确认当前版本，点击查看完整方案。"
                        ),
                        "url": (
                            "/?screen=home&assistant-plan="
                            f"{match.group(1)}"
                        ),
                        "tag": (
                            "assistant-plan-applied-"
                            if result["applied"]
                            else "assistant-plan-confirmed-"
                        ) + match.group(1),
                    },
                )
                self._send_json(result)
            elif match := re.fullmatch(
                r"/api/v1/assistant/plans/([^/]+)/cancel", path
            ):
                self._send_json(
                    STORE.cancel_life_assistant_plan(match.group(1), user["id"])
                )
            elif match := re.fullmatch(
                r"/api/v1/notifications/([^/]+)/read", path
            ):
                self._send_json(
                    STORE.mark_in_app_notification_read(
                        match.group(1), user["id"]
                    )
                )
            elif path == "/api/v1/notifications/read-all":
                self._send_json(
                    STORE.mark_all_in_app_notifications_read(user["id"])
                )
            elif match := re.fullmatch(
                r"/api/v1/notifications/([^/]+)/archive", path
            ):
                self._send_json(
                    STORE.archive_in_app_notification(
                        match.group(1), user["id"]
                    )
                )
            elif match := re.fullmatch(r"/api/v1/media/([^/]+)/access", path):
                user = self._require_user()
                self._send_json(STORE.issue_media_access(match.group(1), user["id"], bool(payload.get("thumbnail", False)), payload.get("ttl_seconds", 300)))
            elif match := re.fullmatch(r"/api/v1/media/([^/]+)/trash", path):
                user = self._require_user()
                self._send_json(STORE.trash_media(match.group(1), user["id"]))
            elif match := re.fullmatch(r"/api/v1/media/([^/]+)/restore", path):
                user = self._require_user()
                self._send_json(STORE.restore_media(match.group(1), payload.get("expected_version"), user["id"]))
            elif match := re.fullmatch(r"/api/v1/messages/([^/]+)/moment", path):
                self._send_json(STORE.message_to_moment(match.group(1), payload, user["id"]), 201)
            elif path == "/api/v1/messages/read":
                self._send_json(STORE.mark_messages_read(user_id=user["id"]))
            elif path == "/api/v1/moments":
                created = STORE.create_moment(payload, user_id=user["id"])
                if created["visibility"] == "shared":
                    queue_shared_content_web_push(user["id"], "moment", created["id"], {
                        "type": "moment_created",
                        "title": "恋屿新增了一段共同回忆",
                        "body": "对方记录了新的恋爱瞬间，点击查看。",
                        "tag": f"moment-{created['id']}",
                    })
                self._send_json(created, 201)
            elif path == "/api/v1/places/search":
                self._send_json(STORE.search_destination_places(payload, user["id"]))
            elif path == "/api/v1/recipes":
                self._send_json(STORE.create_recipe(payload, user["id"]), 201)
            elif path == "/api/v1/recipes/generate":
                self._send_json(STORE.generate_recipe_draft(payload, user["id"]), 202)
            elif path == "/api/v1/recipes/search-online":
                self._send_json(STORE.search_online_recipes(payload, user["id"]), 200)
            elif path == "/api/v1/recipes/compare":
                self._send_json(STORE.compare_recipe_variants(payload, user["id"]), 200)
            elif path == "/api/v1/recipes/import":
                self._send_json(STORE.import_recipe(payload, user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/recipes/([^/]+)/copy", path):
                self._send_json(STORE.copy_recipe(match.group(1), payload, user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/recipes/([^/]+)/media", path):
                self._send_json(STORE.add_recipe_media(match.group(1), payload, user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/recipe-media/([^/]+)/check", path):
                self._send_json(STORE.check_recipe_media(match.group(1), user["id"]), 200)
            elif match := re.fullmatch(r"/api/v1/recipes/drafts/([^/]+)/confirm", path):
                self._send_json(STORE.confirm_recipe_draft(match.group(1), payload, user["id"]), 201)
            elif path == "/api/v1/meal-wishes":
                self._send_json(STORE.create_meal_wish(payload, user["id"]), 201)
            elif path == "/api/v1/meal-cart/items":
                self._send_json(STORE.add_meal_cart_item(payload, user["id"]), 201)
            elif path == "/api/v1/meal-cart/archive":
                self._send_json(STORE.archive_meal_cart(payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/meal-plans/([^/]+)/repeat", path):
                self._send_json(STORE.repeat_meal_item(match.group(1), payload, user["id"]), 201)
            elif path == "/api/v1/shopping-lists/preview":
                self._send_json(STORE.preview_shopping_list(payload, user["id"]))
            elif path == "/api/v1/shopping-lists/apply":
                self._send_json(STORE.apply_shopping_preview(payload, user["id"]))
            elif path == "/api/v1/shopping-lists/items":
                self._send_json(STORE.create_shopping_item(payload, user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/shopping-lists/items/([^/]+)/stock", path):
                payload["idempotency_key"] = self.headers.get("Idempotency-Key") or payload.get("idempotency_key")
                self._send_json(STORE.stock_shopping_item(match.group(1), payload, user["id"]))
            elif path == "/api/v1/shopping-lists/complete":
                payload["idempotency_key"] = self.headers.get("Idempotency-Key") or payload.get("idempotency_key")
                self._send_json(STORE.complete_shopping_list(payload, user["id"]))
            elif path == "/api/v1/pantry/items":
                payload["idempotency_key"] = self.headers.get("Idempotency-Key") or payload.get("idempotency_key")
                self._send_json(STORE.upsert_pantry_item(payload, user["id"]), 201)
            elif path == "/api/v1/weekly-meal-plans/drafts":
                self._attach_idempotency_key(payload)
                self._send_json(STORE.create_weekly_meal_plan_draft(payload, user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/weekly-meal-plans/drafts/([^/]+)/confirm", path):
                self._attach_idempotency_key(payload)
                self._send_json(STORE.confirm_weekly_meal_plan_draft(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/weekly-meal-plans/drafts/([^/]+)/cancel", path):
                self._attach_idempotency_key(payload)
                self._send_json(STORE.cancel_weekly_meal_plan_draft(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/meal-cart/items/([^/]+)/ingredient-substitutions/preview", path):
                self._attach_idempotency_key(payload)
                self._send_json(STORE.preview_ingredient_substitutions(match.group(1), payload, user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/meal-cart/items/([^/]+)/ingredient-substitutions/([^/]+)/apply", path):
                self._attach_idempotency_key(payload)
                self._send_json(STORE.apply_ingredient_substitution(
                    match.group(1), match.group(2), payload, user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/meal-cart/items/([^/]+)/ingredient-substitutions/([^/]+)/dismiss", path):
                self._attach_idempotency_key(payload)
                self._send_json(STORE.dismiss_ingredient_substitution(
                    match.group(1), match.group(2), payload, user["id"],
                ))
            elif path == "/api/v1/cook-sessions":
                self._attach_idempotency_key(payload)
                self._send_json(STORE.start_cook_session(payload, user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/cook-sessions/([^/]+)/timers", path):
                self._attach_idempotency_key(payload)
                self._send_json(STORE.start_cook_timer(match.group(1), payload, user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/cook-sessions/([^/]+)/timers/([^/]+)/cancel", path):
                self._attach_idempotency_key(payload)
                self._send_json(STORE.cancel_cook_timer(match.group(1), match.group(2), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/cook-sessions/([^/]+)/complete", path):
                self._attach_idempotency_key(payload)
                self._send_json(STORE.complete_cook_session(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/cook-sessions/([^/]+)/cancel", path):
                self._attach_idempotency_key(payload)
                self._send_json(STORE.cancel_cook_session(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/cook-sessions/([^/]+)/pantry", path):
                self._attach_idempotency_key(payload)
                self._send_json(STORE.apply_cook_pantry_updates(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/moments/([^/]+)/trash", path):
                self._send_json(STORE.trash_moment(match.group(1), user["id"]))
            elif match := re.fullmatch(r"/api/v1/moments/([^/]+)/restore", path):
                self._send_json(STORE.restore_moment(match.group(1), payload.get("expected_version"), user["id"]))
            elif match := re.fullmatch(r"/api/v1/moments/([^/]+)/reaction", path):
                moment_id = match.group(1)
                result = STORE.set_moment_reaction(moment_id, payload.get("reaction"), user["id"])
                if result["reaction"]:
                    queue_shared_content_web_push(user["id"], "moment", moment_id, {
                        "type": "moment_reaction",
                        "title": "恋屿有一个新的回忆回应",
                        "body": "对方回应了你们的恋爱瞬间，点击查看。",
                        "tag": f"moment-reaction-{moment_id}-{user['id']}",
                    })
                self._send_json(result)
            elif path == "/api/v1/trips":
                self._send_json(STORE.create_trip(payload, user["id"]), 201)
            elif path == "/api/v1/trip-generation-jobs":
                job = STORE.create_trip_generation_job(
                    payload, user["id"], self.headers.get("Idempotency-Key") or payload.get("idempotency_key"),
                )
                TRIP_GENERATION_WORKER.wake()
                self._send_json(job, 202 if job["status"] in {"queued", "running"} else 200)
            elif path == "/api/v1/calls":
                record = STORE.start_call(user_id=user["id"])
                queued = queue_web_push(record["callee_id"], {
                    "type": "incoming_call",
                    "title": "恋屿语音来电",
                    "body": "对方正在呼叫你，点击进入恋屿接听。",
                    "url": f"/?screen=chat&call={record['id']}",
                    "tag": f"call-{record['id']}",
                    "call_id": record["id"],
                })
                record["push_devices_queued"] = queued
                self._send_json(record, 201)
            elif match := re.fullmatch(r"/api/v1/calls/([^/]+)/rtc-token", path):
                call = STORE.get_call(match.group(1), user_id=user["id"])
                if call["state"] not in {"ringing", "connected"}:
                    raise ValueError("这通电话已经结束")
                try:
                    token = RTC.issue(call["id"], user["id"])
                    PROVIDER_FAILURES.success("shengwang")
                    self._send_json(token)
                except Exception as error:
                    if RTC.provider == "shengwang":
                        PROVIDER_FAILURES.failure("shengwang", "声网 Token 签发", error)
                    raise
            elif match := re.fullmatch(r"/api/v1/calls/([^/]+)/answer", path):
                result = STORE.answer_call(match.group(1), user_id=user["id"])
                cancel_call_push(match.group(1))
                self._send_json(result)
            elif match := re.fullmatch(r"/api/v1/calls/([^/]+)/heartbeat", path):
                self._send_json(STORE.heartbeat_call(match.group(1), user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/calls/([^/]+)/decline", path):
                result = STORE.decline_call(match.group(1), user_id=user["id"])
                cancel_call_push(match.group(1))
                self._send_json(result)
            elif match := re.fullmatch(r"/api/v1/calls/([^/]+)/cancel", path):
                result = STORE.cancel_call(match.group(1), user_id=user["id"])
                cancel_call_push(match.group(1))
                self._send_json(result)
            elif match := re.fullmatch(r"/api/v1/calls/([^/]+)/signal", path):
                self._send_json(STORE.create_call_signal(match.group(1), payload, user_id=user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/calls/([^/]+)/end", path):
                result = STORE.end_call(match.group(1), payload.get("duration_seconds", 0), payload.get("note", ""), user_id=user["id"])
                cancel_call_push(match.group(1))
                self._send_json(result)
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/select-variant", path):
                self._send_json(STORE.select_variant(match.group(1), str(payload.get("style", "")), user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/restore", path):
                self._send_json(STORE.restore_trip(match.group(1), payload.get("expected_version"), user["id"]))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/generate", path):
                self._send_json(STORE.regenerate_trip(match.group(1), user_id=user["id"], preserve_manual=bool(payload.get("preserve_manual", True))))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/compose", path):
                self._send_json(STORE.compose_plan(match.group(1), payload.get("selected_item_ids", []), user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/approve", path):
                self._send_json(STORE.approve_trip(match.group(1), payload.get("revision", 0), user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/realism-checks", path):
                payload["client_request_id"] = self.headers.get("Idempotency-Key") or payload.get("client_request_id") or payload.get("idempotency_key")
                self._send_json(STORE.run_trip_realism_check(match.group(1), payload, user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/packing/generate", path):
                payload["client_request_id"] = self.headers.get("Idempotency-Key") or payload.get("client_request_id") or payload.get("idempotency_key")
                self._send_json(STORE.generate_trip_packing(match.group(1), payload, user["id"], WEATHER), 201)
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/packing", path):
                self._send_json(STORE.create_trip_packing_item(
                    match.group(1), payload,
                    self.headers.get("Idempotency-Key") or payload.get("idempotency_key"), user["id"],
                ), 201)
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/hospitals/search", path):
                self._send_json(STORE.search_trip_hospitals(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/emergency", path):
                self._send_json(STORE.create_trip_emergency(
                    match.group(1), payload,
                    self.headers.get("Idempotency-Key") or payload.get("idempotency_key"), user["id"],
                ), 201)
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/expenses/settle", path):
                self._send_json(STORE.settle_trip_expenses(
                    match.group(1), payload,
                    self.headers.get("Idempotency-Key") or payload.get("idempotency_key"), user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/expenses/([^/]+)/restore", path):
                self._send_json(STORE.restore_trip_expense(
                    match.group(1), match.group(2), payload,
                    self.headers.get("Idempotency-Key") or payload.get("idempotency_key"), user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/expenses", path):
                self._send_json(STORE.create_trip_expense(
                    match.group(1), payload,
                    self.headers.get("Idempotency-Key") or payload.get("idempotency_key"), user["id"],
                ), 201)
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/assistant/live", path):
                self._send_json(STORE.live_trip_status(match.group(1), payload, user["id"], WEATHER))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/assistant/navigation", path):
                self._send_json(STORE.live_trip_navigation(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/assistant/recap", path):
                self._send_json(STORE.create_trip_recap_draft(match.group(1), user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/assistant/recap/([^/]+)/confirm", path):
                self._send_json(STORE.confirm_trip_recap_draft(
                    match.group(1), match.group(2),
                    self.headers.get("Idempotency-Key") or payload.get("idempotency_key"), user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/assistant/recap/([^/]+)/cancel", path):
                self._send_json(STORE.cancel_trip_recap_draft(match.group(1), match.group(2), user["id"]))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/assistant/messages", path):
                self._send_json(STORE.ask_trip_assistant(
                    match.group(1), payload.get("message"), user["id"],
                    self.headers.get("Idempotency-Key") or payload.get("idempotency_key"),
                    payload.get("live_context"), WEATHER,
                ), 201)
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/assistant/actions/([^/]+)/confirm", path):
                self._send_json(STORE.confirm_trip_assistant_action(
                    match.group(1), match.group(2),
                    self.headers.get("Idempotency-Key") or payload.get("idempotency_key"), user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/assistant/actions/([^/]+)/cancel", path):
                self._send_json(STORE.cancel_trip_assistant_action(match.group(1), match.group(2), user["id"]))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/food-recommendations/([^/]+)/select", path):
                self._send_json(STORE.select_food_recommendation(match.group(1), match.group(2), user["id"]))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/food-recommendations", path):
                try:
                    result = STORE.refresh_food_recommendations(
                        match.group(1), user["id"], payload.get("search_area", ""),
                        payload.get("day_areas"), payload.get("day_number"), payload.get("item_id", ""),
                    )
                    if result.get("configured"):
                        PROVIDER_FAILURES.success("amap")
                    self._send_json(result)
                except RuntimeError as error:
                    if isinstance(error, QuotaExceededError):
                        raise
                    if os.getenv("AMAP_WEB_KEY", "").strip():
                        PROVIDER_FAILURES.failure("amap", "高德餐馆搜索", error)
                    raise
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/itinerary-items/([^/]+)/place", path):
                self._send_json(STORE.confirm_itinerary_place(
                    match.group(1), match.group(2), payload.get("candidate_id"), user["id"], payload.get("expected_version"),
                ))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/route-legs/([^/]+)/routes", path):
                try:
                    result = STORE.query_trip_route_leg(match.group(1), match.group(2), payload, user["id"])
                    PROVIDER_FAILURES.success("amap")
                    self._send_json(result)
                except RuntimeError as error:
                    if isinstance(error, QuotaExceededError):
                        raise
                    if os.getenv("AMAP_WEB_KEY", "").strip():
                        PROVIDER_FAILURES.failure("amap", "高德逐段路线", error)
                    raise
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/routes", path):
                try:
                    result = STORE.trip_routes(match.group(1), payload, user["id"])
                    if result.get("degraded"):
                        PROVIDER_FAILURES.failure("amap", "高德路线", RuntimeError("已降级为模拟路线"))
                    else:
                        PROVIDER_FAILURES.success("amap")
                    self._send_json(result)
                except RuntimeError as error:
                    if isinstance(error, QuotaExceededError):
                        raise
                    if os.getenv("AMAP_WEB_KEY", "").strip():
                        PROVIDER_FAILURES.failure("amap", "高德路线", error)
                    raise
            elif path == "/api/v1/quotes/search":
                try:
                    result = STORE.search_quotes(payload, user["id"])
                    PROVIDER_FAILURES.success("booking")
                    self._send_json(result)
                except RuntimeError as error:
                    if isinstance(error, QuotaExceededError):
                        raise
                    if any(os.getenv(name, "").strip() for name in ("BOOKING_API_URL", "SERPAPI_API_KEY", "JUHE_TRAIN_API_KEY")):
                        PROVIDER_FAILURES.failure("booking", "报价服务", error)
                    raise
            elif path == "/api/v1/quotes/manual":
                self._send_json(STORE.create_manual_quote(payload, user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/quotes/([^/]+)/select", path):
                self._send_json(STORE.select_quote(match.group(1), user["id"], payload.get("expected_version")))
            elif path == "/api/v1/chat-imports":
                try:
                    result = STORE.create_chat_import(payload, user["id"])
                    PROVIDER_FAILURES.success("ocr")
                    self._send_json(result, 201)
                except RuntimeError as error:
                    if isinstance(error, QuotaExceededError):
                        raise
                    if real_ocr_configured():
                        PROVIDER_FAILURES.failure("ocr", "OCR 服务", error)
                    raise
            elif match := re.fullmatch(r"/api/v1/chat-imports/([^/]+)/confirm", path):
                self._send_json(STORE.finish_chat_import(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/chat-imports/([^/]+)/cancel", path):
                self._send_json(STORE.cancel_chat_import(match.group(1), user["id"]))
            elif path == "/api/v1/calendar":
                self._send_json(STORE.create_calendar_entry(payload, user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/shared-content/([^/]+)/([^/]+)/comments", path):
                resource_type, resource_id = match.group(1), match.group(2)
                comment = STORE.create_shared_comment(resource_type, resource_id, payload.get("body"), user["id"])
                queue_shared_content_web_push(user["id"], resource_type, resource_id, {
                    "type": "shared_comment",
                    "title": "恋屿有一条新的共同留言",
                    "body": "对方在共同内容中留下了评论，点击查看。",
                    "tag": f"comment-{comment['id']}",
                })
                self._send_json(comment, 201)
            elif path == "/api/v1/lists/wishlist/items":
                self._send_json(STORE.create_wishlist_item(payload, user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/lists/items/([^/]+)/claim", path):
                self._send_json(STORE.claim_wishlist_item(
                    match.group(1), payload.get("private_note", ""),
                    payload.get("reveal_claimant", False), payload.get("expected_version"),
                    self.headers.get("Idempotency-Key") or payload.get("idempotency_key"), user["id"],
                ), 201)
            elif match := re.fullmatch(r"/api/v1/lists/items/([^/]+)/claim/release", path):
                self._send_json(STORE.release_wishlist_claim(
                    match.group(1), payload.get("expected_version"),
                    self.headers.get("Idempotency-Key") or payload.get("idempotency_key"), user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/lists/items/([^/]+)/claim/complete", path):
                self._send_json(STORE.complete_wishlist_claim(
                    match.group(1), payload.get("expected_version"),
                    payload.get("reveal_claimant", False), payload.get("create_moment", False),
                    self.headers.get("Idempotency-Key") or payload.get("idempotency_key"), user["id"],
                ))
            elif path == "/api/v1/expression/suggestions":
                self._send_json(STORE.suggest_expression(
                    mode=payload.get("mode"), message_id=payload.get("message_id"),
                    draft=payload.get("draft"),
                    idempotency_key=self.headers.get("Idempotency-Key") or payload.get("idempotency_key"),
                    user_id=user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/calendar/([^/]+)/complete", path):
                self._send_json(STORE.complete_calendar_entry(match.group(1), bool(payload.get("completed", True)), payload.get("expected_version"), user["id"]))
            elif path == "/api/v1/preferences":
                self._send_json(STORE.create_preference(payload, user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/account/privacy/grants/([^/]+)/revoke", path):
                self._send_json(STORE.revoke_ai_context_grant(
                    match.group(1), payload.get("expected_version"), user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/account/devices/([^/]+)/revoke", path):
                self._send_json(STORE.revoke_device(match.group(1), user["id"], self._session_token()))
            elif path == "/api/v1/couple/unlink":
                result = STORE.unlink_couple(str(payload.get("password", "")), str(payload.get("confirmation", "")), user["id"])
                self._send_json(result, headers={"Set-Cookie": self._session_cookie("", clear=True)})
            elif path == "/api/v1/account/delete":
                result = STORE.delete_account(str(payload.get("password", "")), str(payload.get("confirmation", "")), user["id"])
                self._send_json(result, headers={"Set-Cookie": self._session_cookie("", clear=True)})
            elif path == "/api/v1/daily-question/answer":
                self._send_json(STORE.answer_daily_question(payload.get("answer", ""), user_id=user["id"]))
            elif path == "/api/v1/me/status/clear":
                self._send_json(STORE.clear_status(user_id=user["id"]))
            elif path == "/api/v1/weekly-reviews/generate":
                self._send_json(STORE.generate_weekly_review(user_id=user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/weekly-reviews/([^/]+)/confirm", path):
                self._send_json(STORE.confirm_weekly_review(match.group(1), user_id=user["id"]))
            elif path == "/api/v1/date-plans":
                if not str(payload.get("place_candidate_id") or "").strip():
                    raise ValueError("请先搜索并明确选择一个具体地点")
                self._send_json(STORE.create_date_plan(
                    payload, user_id=user["id"], weather_provider=WEATHER,
                ), 201)
            elif match := re.fullmatch(r"/api/v1/date-plans/([^/]+)/generate", path):
                self._send_json(STORE.generate_date_plan(match.group(1), user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/date-plans/([^/]+)/compose", path):
                self._send_json(STORE.compose_date_plan(match.group(1), payload, user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/date-plans/([^/]+)/confirm", path):
                self._send_json(STORE.confirm_date_plan(match.group(1), payload.get("version"), user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/date-plans/([^/]+)/complete", path):
                self._send_json(STORE.complete_date_plan(match.group(1), payload, user_id=user["id"]))
            elif path == "/api/v1/decisions":
                self._send_json(STORE.create_decision(payload, user["id"]), 201)
            elif match := re.fullmatch(r"/api/v1/decisions/([^/]+)/conversion-preview", path):
                self._send_json(STORE.prepare_decision_conversion(
                    match.group(1), payload.get("target"), payload.get("option_id"), user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/decisions/([^/]+)/resolve", path):
                self._send_json(STORE.resolve_decision(
                    match.group(1), payload.get("action"), payload.get("option_id"),
                    payload.get("expected_version"), user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/decisions/([^/]+)/extend", path):
                self._send_json(STORE.extend_decision(
                    match.group(1), payload.get("deadline_at"),
                    payload.get("expected_version"), user["id"],
                ))
            elif path == "/api/v1/couples/invites":
                self._send_json(STORE.create_user_invite(user["id"]), 201)
            elif path == "/api/v1/couples/invites/accept":
                self._send_json(STORE.accept_user_invite(user["id"], str(payload.get("code", "")), payload.get("anniversary")))
            elif match := re.fullmatch(r"/api/v1/ui/(home|chat|trip|timeline|life)/compose", path):
                self._send_json(STORE.start_ui_compose_job(match.group(1), payload.get("intent"), payload.get("resourceRefs", []), user_id=user["id"], current_trip_id=payload.get("tripId")), 202)
            elif match := re.fullmatch(r"/api/v1/ui/actions/([^/]+)/preview", path):
                self._send_json(STORE.preview_ui_action(match.group(1), user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/ui/actions/([^/]+)/confirm", path):
                self._send_json(STORE.confirm_ui_action(match.group(1), payload.get("idempotencyKey"), user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/ui/actions/([^/]+)/cancel", path):
                self._send_json(STORE.cancel_ui_action(match.group(1), user_id=user["id"]))
            else:
                self._send_json({"error": "接口不存在"}, 404)
        except Exception as error:
            self._api_error(error)

    def do_OPTIONS(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            self.send_response(405)
            self.send_header("Allow", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
            self.end_headers()
            return
        if self.headers.get("Origin") and not self._allowed_cors_origin():
            origin = self.headers.get("Origin", "")
            ADMIN.record_event(
                f"origin:preflight:{hashlib.sha256(origin.encode()).hexdigest()[:16]}",
                "warning",
                "security",
                "已拦截未授权网页来源的跨域预检",
                {"origin": origin[:200], "path": path, "ip_address": self.client_address[0]},
            )
            self._send_json({"error": "该网页来源未获准访问恋屿接口"}, 403)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Idempotency-Key, Last-Event-ID, X-Filename, X-Media-Visibility, X-Upload-Offset")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PUT(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            self._enforce_write_origin()
            user = self._require_user()
            if match := re.fullmatch(r"/api/v1/media/uploads/([^/]+)/chunk", path):
                data = self._read_bytes(8 * 1024 * 1024)
                self._send_json(STORE.append_media_upload(match.group(1), data, self.headers.get("X-Upload-Offset", ""), user["id"]))
                return
            payload = self._read_json()
            if path == "/api/v1/me/status":
                self._send_json(STORE.set_status(payload, user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/memories/(moment|trip)/([^/]+)/descriptions/me", path):
                self._send_json(STORE.save_memory_description(match.group(1), match.group(2), payload, user["id"]))
            elif path == "/api/v1/food-profiles/me":
                payload["idempotency_key"] = self.headers.get("Idempotency-Key") or payload.get("idempotency_key")
                self._send_json(STORE.update_my_food_profile(payload, user["id"]))
            elif path == "/api/v1/shopping-category-order":
                payload["idempotency_key"] = self.headers.get("Idempotency-Key") or payload.get("idempotency_key")
                self._send_json(STORE.update_shopping_category_order(payload, user["id"]))
            elif path == "/api/v1/me/location":
                self._send_json(STORE.set_location(payload, user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/weekly-reviews/([^/]+)/answer", path):
                self._send_json(STORE.answer_weekly_review(match.group(1), payload, user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/date-plans/([^/]+)/vote", path):
                self._send_json(STORE.vote_date_plan(match.group(1), str(payload.get("variant_id", "")), user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/decisions/([^/]+)/vote", path):
                self._send_json(STORE.submit_decision_vote(
                    match.group(1), payload.get("option_ids"), payload.get("expected_version"),
                    self.headers.get("Idempotency-Key") or payload.get("idempotency_key"),
                    user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/negotiation/preferences", path):
                payload["client_request_id"] = self.headers.get("Idempotency-Key") or payload.get("client_request_id") or payload.get("idempotency_key")
                self._send_json(STORE.submit_trip_item_preferences(match.group(1), payload, user["id"]))
            else:
                self._send_json({"error": "接口不存在"}, 404)
        except Exception as error:
            self._api_error(error)

    def do_PATCH(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            self._enforce_write_origin()
            payload = self._read_json()
            user = self._require_user()
            if match := re.fullmatch(r"/api/v1/trips/items/([^/]+)/impact", path):
                self._send_json(STORE.impact_analysis(match.group(1), payload.get("changes", {}), user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/assistant/recap/([^/]+)", path):
                self._send_json(STORE.update_trip_recap_draft(match.group(1), match.group(2), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/trips/items/([^/]+)", path):
                self._send_json(STORE.revise_item(match.group(1), payload.get("changes", {}), str(payload.get("reason", "用户手动调整")), user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/packing/([^/]+)", path):
                self._send_json(STORE.update_trip_packing_item(match.group(1), match.group(2), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/emergency/([^/]+)", path):
                self._send_json(STORE.update_trip_emergency(match.group(1), match.group(2), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/expenses/([^/]+)", path):
                self._send_json(STORE.update_trip_expense(
                    match.group(1), match.group(2), payload,
                    self.headers.get("Idempotency-Key") or payload.get("idempotency_key"), user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)", path):
                self._send_json(STORE.update_trip_logistics(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/lists/items/([^/]+)", path):
                if "completed" in payload:
                    self._send_json(STORE.toggle_wishlist_item(match.group(1), payload.get("completed"), user_id=user["id"]))
                else:
                    self._send_json(STORE.update_wishlist_item(match.group(1), payload, user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/moments/([^/]+)", path):
                self._send_json(STORE.update_moment(match.group(1), payload, user_id=user["id"]))
            elif match := re.fullmatch(r"/api/v1/voice-transcripts/([^/]+)", path):
                self._send_json(STORE.update_voice_transcript(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/future-letters/([^/]+)", path):
                self._send_json(STORE.update_future_letter(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/chat-imports/([^/]+)/items/([^/]+)", path):
                self._send_json(STORE.update_chat_import_item(match.group(1), match.group(2), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/calendar/([^/]+)", path):
                self._send_json(STORE.update_calendar_entry(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/preferences/([^/]+)", path):
                self._send_json(STORE.update_preference(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/recipes/([^/]+)", path):
                self._send_json(STORE.update_recipe(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/meal-wishes/([^/]+)", path):
                self._send_json(STORE.update_meal_wish(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/meal-cart/items/([^/]+)", path):
                self._send_json(STORE.update_meal_cart_item(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/shopping-lists/items/([^/]+)", path):
                self._send_json(STORE.update_shopping_item(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/pantry/items/([^/]+)", path):
                payload["idempotency_key"] = self.headers.get("Idempotency-Key") or payload.get("idempotency_key")
                self._send_json(STORE.update_pantry_item(match.group(1), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/weekly-meal-plans/drafts/([^/]+)/items/([^/]+)", path):
                self._attach_idempotency_key(payload)
                self._send_json(STORE.update_weekly_meal_plan_item(match.group(1), match.group(2), payload, user["id"]))
            elif match := re.fullmatch(r"/api/v1/cook-sessions/([^/]+)/step", path):
                self._attach_idempotency_key(payload)
                self._send_json(STORE.set_cook_session_step(match.group(1), payload, user["id"]))
            elif path == "/api/v1/assistant/settings":
                self._send_json(
                    STORE.update_life_assistant_settings(payload, user["id"])
                )
            elif match := re.fullmatch(
                r"/api/v1/assistant/plans/([^/]+)", path
            ):
                self._send_json(
                    STORE.update_life_assistant_plan(
                        match.group(1), payload, user["id"]
                    )
                )
            elif match := re.fullmatch(r"/api/v1/decisions/([^/]+)", path):
                self._send_json(STORE.update_decision(match.group(1), payload, user["id"]))
            else:
                self._send_json({"error": "接口不存在"}, 404)
        except Exception as error:
            self._api_error(error)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            self._enforce_write_origin()
            payload = self._read_json()
            user = self._require_user()
            if match := re.fullmatch(r"/api/v1/media/uploads/([^/]+)", path):
                self._send_json(STORE.cancel_media_upload(match.group(1), user["id"]))
            elif path == "/api/v1/push/subscriptions":
                subscription = PUSH.normalize_subscription(payload.get("subscription"))
                self._send_json(STORE.remove_push_subscription(PUSH.endpoint_hash(subscription), user["id"]))
            elif match := re.fullmatch(r"/api/v1/media/([^/]+)", path):
                self._send_json(STORE.purge_media(match.group(1), user["id"]))
            elif match := re.fullmatch(r"/api/v1/moments/([^/]+)", path):
                self._send_json(STORE.purge_moment(match.group(1), user["id"]))
            elif match := re.fullmatch(r"/api/v1/future-letters/([^/]+)", path):
                self._send_json(STORE.delete_future_letter(
                    match.group(1), payload.get("expected_version"), user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/shared-comments/([^/]+)", path):
                self._send_json(STORE.delete_shared_comment(match.group(1), payload.get("expected_version"), user["id"]))
            elif match := re.fullmatch(r"/api/v1/calendar/([^/]+)", path):
                self._send_json(STORE.delete_calendar_entry(match.group(1), payload.get("expected_version"), user["id"]))
            elif match := re.fullmatch(r"/api/v1/lists/items/([^/]+)", path):
                self._send_json(STORE.delete_wishlist_item(match.group(1), payload.get("expected_version"), user["id"]))
            elif match := re.fullmatch(r"/api/v1/preferences/([^/]+)", path):
                self._send_json(STORE.delete_preference(match.group(1), payload.get("expected_version"), user["id"]))
            elif match := re.fullmatch(r"/api/v1/recipes/([^/]+)", path):
                self._send_json(STORE.delete_recipe(match.group(1), payload.get("expected_version"), user["id"]))
            elif match := re.fullmatch(r"/api/v1/meal-wishes/([^/]+)", path):
                self._send_json(STORE.delete_meal_wish(match.group(1), payload.get("expected_version"), user["id"]))
            elif match := re.fullmatch(r"/api/v1/meal-cart/items/([^/]+)", path):
                self._send_json(STORE.cancel_meal_cart_item(match.group(1), payload.get("expected_version"), user["id"]))
            elif match := re.fullmatch(r"/api/v1/shopping-lists/items/([^/]+)", path):
                self._send_json(STORE.delete_shopping_item(match.group(1), payload.get("expected_version"), user["id"]))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/packing/([^/]+)", path):
                self._send_json(STORE.delete_trip_packing_item(
                    match.group(1), match.group(2), payload.get("expected_version"), user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/emergency/([^/]+)", path):
                self._send_json(STORE.delete_trip_emergency(
                    match.group(1), match.group(2), payload.get("expected_version"), user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)/expenses/([^/]+)", path):
                self._send_json(STORE.delete_trip_expense(
                    match.group(1), match.group(2), payload,
                    self.headers.get("Idempotency-Key") or payload.get("idempotency_key"), user["id"],
                ))
            elif match := re.fullmatch(r"/api/v1/trips/([^/]+)", path):
                self._send_json(STORE.delete_trip(match.group(1), payload.get("expected_version"), user["id"]))
            elif match := re.fullmatch(r"/api/v1/decisions/([^/]+)", path):
                self._send_json(STORE.cancel_decision(
                    match.group(1), payload.get("expected_version"), user["id"],
                ))
            else:
                self._send_json({"error": "接口不存在"}, 404)
        except Exception as error:
            self._api_error(error)

    def end_headers(self) -> None:
        # ``BaseHTTPRequestHandler`` can call ``send_error`` before ``path``
        # and ``headers`` exist when a tunnel probe sends a malformed or
        # incomplete request line.  Security headers must not turn that normal
        # 400 response into a second exception.
        request_path = urlparse(getattr(self, "path", "")).path
        cors_origin = self._allowed_cors_origin() if hasattr(self, "headers") else None
        if cors_origin:
            self.send_header("Access-Control-Allow-Origin", cors_origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store" if request_path.startswith("/api/") else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(self), microphone=(self), geolocation=(self)")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
            "img-src 'self' data: blob:; media-src 'self' blob:; "
            "connect-src 'self' https://*.agora.io:* wss://*.agora.io:* https://*.sd-rtn.com:* wss://*.sd-rtn.com:*; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'self'",
        )
        super().end_headers()


def main() -> None:
    if not PROTOTYPE.exists():
        raise SystemExit(f"Prototype not found: {PROTOTYPE}")
    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "18765"))
    server = ThreadingHTTPServer((host, port), LoveMomentsHandler)
    PUSH_OUTBOX_WORKER.start()
    TRIP_GENERATION_WORKER.start()
    DAILY_SUMMARY_WORKER.start()
    FUTURE_LETTER_WORKER.start()
    ALBUM_EXPORT_WORKER.start()
    print(f"Love Moments full-stack MVP is running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ALBUM_EXPORT_WORKER.stop()
        FUTURE_LETTER_WORKER.stop()
        DAILY_SUMMARY_WORKER.stop()
        TRIP_GENERATION_WORKER.stop()
        PUSH_OUTBOX_WORKER.stop()
        server.server_close()


if __name__ == "__main__":
    main()
