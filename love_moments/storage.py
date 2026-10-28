"""Media object-storage adapters.

The application keeps metadata in SQLite. Binary objects can live on disk,
Qiniu Kodo (S3 compatible), or Supabase Storage. Credentials are read only
by the server and are never returned to clients.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request

from .http_client import TRUSTED_SSL_CONTEXT

try:
    import certifi
except ImportError:  # pragma: no cover - requirements.txt installs it
    certifi = None


@dataclass(frozen=True)
class StoredObject:
    provider: str
    key: str
    size: int
    checksum: str


class MediaStorage:
    name = "unknown"
    keeps_local_copy = False

    def put(self, key: str, data: bytes, content_type: str) -> StoredObject:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


def _safe_key(key: str) -> str:
    normalized = "/".join(part for part in str(key).replace("\\", "/").split("/") if part not in {"", "."})
    if not normalized or any(part == ".." for part in normalized.split("/")):
        raise ValueError("对象存储路径无效")
    return normalized


class LocalMediaStorage(MediaStorage):
    name = "local"
    keeps_local_copy = True

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.root.chmod(0o700)

    def _path(self, key: str) -> Path:
        path = (self.root / _safe_key(key)).resolve()
        if self.root != path and self.root not in path.parents:
            raise ValueError("对象存储路径无效")
        return path

    def put(self, key: str, data: bytes, content_type: str) -> StoredObject:
        del content_type
        safe = _safe_key(key)
        target = self._path(safe)
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            target.parent.chmod(0o700)
        temporary = self._path(f"temp/{hashlib.sha256(safe.encode()).hexdigest()}.upload")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            temporary.parent.chmod(0o700)
        temporary.write_bytes(data)
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(target)
        if os.name != "nt":
            target.chmod(0o600)
        return StoredObject(self.name, safe, len(data), hashlib.sha256(data).hexdigest())

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise KeyError("媒体文件不存在")
        return path.read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class QiniuKodoMediaStorage(MediaStorage):
    """Minimal AWS Signature V4 client for a private Qiniu Kodo bucket."""

    name = "qiniu"

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str,
        endpoint: str = "",
        timeout: int = 30,
    ):
        region = region.strip().lower()
        if not re.fullmatch(r"[a-z0-9-]{3,32}", region):
            raise ValueError("七牛云 S3 Region 无效")
        endpoint = (endpoint.strip() or f"https://s3.{region}.qiniucs.com").rstrip("/")
        parsed_endpoint = parse.urlparse(endpoint)
        hostname = (parsed_endpoint.hostname or "").lower()
        if (
            parsed_endpoint.scheme != "https"
            or not hostname.endswith(".qiniucs.com")
            or parsed_endpoint.username
            or parsed_endpoint.password
            or parsed_endpoint.query
            or parsed_endpoint.fragment
            or parsed_endpoint.path not in {"", "/"}
        ):
            raise ValueError("七牛云 S3 Endpoint 必须是官方 HTTPS 区域地址")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket.strip().lower()):
            raise ValueError("七牛云 S3 空间名无效")
        self.endpoint = endpoint
        self.access_key = access_key.strip()
        self.secret_key = secret_key.strip()
        self.bucket = bucket.strip().lower()
        self.region = region
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context(cafile=certifi.where()) if certifi else ssl.create_default_context()

    def _request(self, method: str, key: str, data: bytes = b"", content_type: str = "application/octet-stream") -> bytes:
        key = _safe_key(key)
        now = datetime.now(timezone.utc)
        amz_date, date_stamp = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(data).hexdigest()
        canonical_uri = f"/{parse.quote(self.bucket, safe='')}/{parse.quote(key, safe='/~')}"
        host = parse.urlparse(self.endpoint).netloc
        headers = {
            "content-type": content_type,
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
        canonical_request = "\n".join([method, canonical_uri, "", canonical_headers, signed_headers, payload_hash])
        scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])

        def sign(key_bytes: bytes, value: str) -> bytes:
            return hmac.new(key_bytes, value.encode(), hashlib.sha256).digest()

        signing_key = sign(sign(sign(sign(("AWS4" + self.secret_key).encode(), date_stamp), self.region), "s3"), "aws4_request")
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
        req = request.Request(
            f"{self.endpoint}{canonical_uri}",
            data=data if method in {"PUT", "POST"} else None,
            headers={**headers, "Authorization": authorization},
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as response:
                return response.read()
        except error.HTTPError as exc:
            raise RuntimeError(f"七牛云 Kodo 请求失败 ({exc.code})") from exc
        except error.URLError as exc:
            raise RuntimeError("七牛云 Kodo 暂时无法连接") from exc

    def put(self, key: str, data: bytes, content_type: str) -> StoredObject:
        self._request("PUT", key, data, content_type)
        return StoredObject(self.name, _safe_key(key), len(data), hashlib.sha256(data).hexdigest())

    def get(self, key: str) -> bytes:
        return self._request("GET", key)

    def delete(self, key: str) -> None:
        self._request("DELETE", key)


class QiniuMirroredMediaStorage(MediaStorage):
    """Store every Qiniu object on the private local disk as well.

    New objects are only reported as saved after both writes succeed. Reads
    prefer the local copy, so a temporary Qiniu outage does not make a photo
    unavailable. The database continues to use ``qiniu`` as the provider name
    for backwards compatibility with existing records.
    """

    name = "qiniu"
    keeps_local_copy = True

    def __init__(self, local: LocalMediaStorage, remote: QiniuKodoMediaStorage):
        self.local = local
        self.remote = remote

    def put(self, key: str, data: bytes, content_type: str) -> StoredObject:
        safe = _safe_key(key)
        expected_checksum = hashlib.sha256(data).hexdigest()
        local_object = self.local.put(safe, data, content_type)
        try:
            remote_object = self.remote.put(safe, data, content_type)
            if (
                remote_object.key != local_object.key
                or remote_object.size != local_object.size
                or remote_object.checksum != expected_checksum
            ):
                raise RuntimeError("七牛云返回的对象信息与本机副本不一致")
        except Exception:
            # The API must not claim dual-copy durability when the cloud write
            # failed. Random media IDs make deleting this just-created key safe.
            try:
                self.remote.delete(safe)
            except Exception:
                pass
            self.local.delete(safe)
            raise
        return StoredObject(self.name, safe, len(data), expected_checksum)

    def get(self, key: str) -> bytes:
        try:
            return self.local.get(key)
        except KeyError:
            # Store-level checksum verification happens before a recovered
            # remote object is allowed to repair the local copy.
            return self.remote.get(key)

    def local_exists(self, key: str) -> bool:
        try:
            self.local.get(key)
            return True
        except KeyError:
            return False

    def repair_local(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> StoredObject:
        return self.local.put(key, data, content_type)

    def delete(self, key: str) -> None:
        # Keep the recoverable local copy if the remote deletion cannot be
        # confirmed. The caller can retry instead of leaving an unknown cloud
        # object behind while marking the database row as purged.
        self.remote.delete(key)
        self.local.delete(key)


class SupabaseMediaStorage(MediaStorage):
    name = "supabase"

    def __init__(self, url: str, api_key: str, bucket: str, timeout: int = 30):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.bucket = bucket
        self.timeout = timeout

    def _url(self, key: str) -> str:
        return f"{self.url}/storage/v1/object/{parse.quote(self.bucket, safe='')}/{parse.quote(_safe_key(key), safe='/~')}"

    def _request(self, method: str, key: str, data: bytes | None = None, content_type: str = "application/octet-stream") -> bytes:
        headers = {"apikey": self.api_key, "Authorization": f"Bearer {self.api_key}", "Content-Type": content_type}
        if method == "POST":
            headers["x-upsert"] = "true"
        req = request.Request(self._url(key), data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout, context=TRUSTED_SSL_CONTEXT) as response:
                return response.read()
        except error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("message", detail)
            except json.JSONDecodeError:
                pass
            raise RuntimeError(f"Supabase Storage 请求失败 ({exc.code}): {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError("Supabase Storage 暂时无法连接") from exc

    def put(self, key: str, data: bytes, content_type: str) -> StoredObject:
        self._request("POST", key, data, content_type)
        return StoredObject(self.name, _safe_key(key), len(data), hashlib.sha256(data).hexdigest())

    def get(self, key: str) -> bytes:
        return self._request("GET", key)

    def delete(self, key: str) -> None:
        self._request("DELETE", key)


def storage_from_env(media_root: str | Path) -> MediaStorage:
    provider = os.getenv("MEDIA_STORAGE_PROVIDER", "local").strip().lower()
    if provider == "qiniu":
        values = [
            os.getenv("QINIU_ACCESS_KEY", ""),
            os.getenv("QINIU_SECRET_KEY", ""),
            os.getenv("QINIU_BUCKET", ""),
            os.getenv("QINIU_S3_REGION", ""),
        ]
        if all(values):
            local = LocalMediaStorage(media_root)
            remote = QiniuKodoMediaStorage(*values, endpoint=os.getenv("QINIU_S3_ENDPOINT", ""))
            return QiniuMirroredMediaStorage(local, remote)
    if provider == "supabase":
        values = [os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_ANON_KEY", ""), os.getenv("SUPABASE_BUCKET", "love-moments")]
        if all(values):
            return SupabaseMediaStorage(*values)
    return LocalMediaStorage(media_root)
