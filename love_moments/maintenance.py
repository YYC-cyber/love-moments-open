"""Backup verification, retention and small self-hosted maintenance helpers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import io
import tarfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .schema import SCHEMA_VERSION, account_export_policy_status, schema_inventory


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest(folder: Path) -> dict[str, Any]:
    manifest_path = folder / "manifest.json"
    if not folder.is_dir() or not manifest_path.is_file():
        raise ValueError("备份目录或清单不存在")
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("备份清单格式无效")
    return value


def _safe_relative_path(value: object, label: str) -> Path:
    text = str(value or "").replace("\\", "/").strip()
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"{label}路径无效")
    return path


def verify_backup(folder: str | Path, *, update_manifest: bool = True) -> dict[str, Any]:
    """Exercise a backup in a temporary restore directory without touching live data."""
    backup = Path(folder).resolve()
    manifest = _read_manifest(backup)
    database_relative = _safe_relative_path(manifest.get("database_file", "love_moments.db"), "数据库")
    database = backup / database_relative
    if not database.is_file():
        raise ValueError("备份数据库不存在")
    expected_hash = str(manifest.get("database_sha256", ""))
    checksum_ok = bool(expected_hash) and _checksum(database) == expected_hash
    missing_media = 0
    invalid_media = 0
    orphan_media = 0
    media_count_matches = False
    media_bytes_matches = False
    theme_count_matches = False
    theme_bytes_matches = False
    invalid_theme_assets = 0
    integrity = "unavailable"
    foreign_key_violations = -1
    schema_version = 0
    inventory: dict[str, Any] = {"ok": False, "missing_tables": ["database"]}
    export_policy = account_export_policy_status()
    with tempfile.TemporaryDirectory(prefix="love-moments-restore-") as temporary:
        restored = Path(temporary) / "love_moments.db"
        shutil.copy2(database, restored)
        with closing(sqlite3.connect(restored)) as db:
            integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_key_violations = len(db.execute("PRAGMA foreign_key_check").fetchall())
            schema_version = int(db.execute("PRAGMA user_version").fetchone()[0])
            inventory = schema_inventory(db, expected_version=schema_version)
            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_assets'").fetchone()
            if table:
                rows = db.execute(
                    "SELECT storage_provider,COALESCE(storage_key,relative_path),thumbnail_path FROM media_assets WHERE purged_at IS NULL"
                ).fetchall()
                for provider, key, thumbnail in rows:
                    if provider not in {"local", "qiniu"}:
                        continue
                    for relative in (key, thumbnail):
                        if relative and not (backup / "media" / str(relative)).is_file():
                            missing_media += 1
    declared_paths: set[str] = set()
    declared_bytes = 0
    for item in manifest.get("media_objects", []):
        if not isinstance(item, dict):
            invalid_media += 1
            continue
        try:
            relative_path = _safe_relative_path(item.get("path"), "媒体")
        except ValueError:
            invalid_media += 1
            continue
        relative = relative_path.as_posix()
        if relative in declared_paths:
            invalid_media += 1
            continue
        declared_paths.add(relative)
        target = (backup / "media" / relative_path).resolve()
        media_root = (backup / "media").resolve()
        if (target != media_root and media_root not in target.parents) or not target.is_file():
            invalid_media += 1
            continue
        size = target.stat().st_size
        declared_bytes += size
        if int(item.get("size", -1)) != size:
            invalid_media += 1
        if item.get("sha256") and _checksum(target) != item["sha256"]:
            invalid_media += 1
    actual_paths: set[str] = set()
    actual_bytes = 0
    media_root = backup / "media"
    if media_root.is_dir():
        for path in media_root.rglob("*"):
            if path.is_file():
                actual_paths.add(path.relative_to(media_root).as_posix())
                actual_bytes += path.stat().st_size
    orphan_media = len(actual_paths - declared_paths)
    media_count_matches = (
        int(manifest.get("media_files", -1)) == len(declared_paths) == len(actual_paths)
    )
    media_bytes_matches = (
        int(manifest.get("media_bytes", -1)) == declared_bytes == actual_bytes
    )
    declared_theme_paths: set[str] = set()
    declared_theme_bytes = 0
    theme_root = backup / "theme-assets"
    for item in manifest.get("theme_objects", []):
        if not isinstance(item, dict):
            invalid_theme_assets += 1
            continue
        try:
            relative_path = _safe_relative_path(item.get("path"), "主题贴纸")
        except ValueError:
            invalid_theme_assets += 1
            continue
        relative = relative_path.as_posix()
        target = (theme_root / relative_path).resolve()
        if relative in declared_theme_paths or (target != theme_root.resolve() and theme_root.resolve() not in target.parents) or not target.is_file():
            invalid_theme_assets += 1
            continue
        declared_theme_paths.add(relative)
        size = target.stat().st_size
        declared_theme_bytes += size
        if int(item.get("size", -1)) != size or (item.get("sha256") and _checksum(target) != item["sha256"]):
            invalid_theme_assets += 1
    actual_theme_paths = {
        path.relative_to(theme_root).as_posix()
        for path in theme_root.rglob("*")
        if path.is_file()
    } if theme_root.is_dir() else set()
    actual_theme_bytes = sum((theme_root / path).stat().st_size for path in actual_theme_paths)
    theme_count_matches = int(manifest.get("theme_files", 0)) == len(declared_theme_paths) == len(actual_theme_paths)
    theme_bytes_matches = int(manifest.get("theme_bytes", 0)) == declared_theme_bytes == actual_theme_bytes
    restore_ok = (
        integrity == "ok" and foreign_key_violations == 0 and checksum_ok
        and missing_media == 0 and invalid_media == 0 and orphan_media == 0
        and media_count_matches and media_bytes_matches
        and invalid_theme_assets == 0 and theme_count_matches and theme_bytes_matches
        and bool(inventory["ok"]) and bool(export_policy["ok"])
    )
    result = {
        "ok": restore_ok,
        "verified_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "database_integrity": integrity,
        "database_checksum_ok": checksum_ok,
        "foreign_key_violations": foreign_key_violations,
        "schema_version": schema_version,
        "expected_schema_version": SCHEMA_VERSION,
        "schema_inventory": inventory,
        "account_export_policy": export_policy,
        "upgrade_required": schema_version < SCHEMA_VERSION,
        "missing_media": missing_media,
        "invalid_media": invalid_media,
        "orphan_media": orphan_media,
        "media_count_matches": media_count_matches,
        "media_bytes_matches": media_bytes_matches,
        "invalid_theme_assets": invalid_theme_assets,
        "theme_count_matches": theme_count_matches,
        "theme_bytes_matches": theme_bytes_matches,
        "theme_files": len(actual_theme_paths),
        "manifest_media_count": int(manifest.get("media_files", -1)),
        "actual_media_count": len(actual_paths),
        "manifest_media_bytes": int(manifest.get("media_bytes", -1)),
        "actual_media_bytes": actual_bytes,
        "restore_drill": "passed" if restore_ok else "failed",
    }
    if update_manifest:
        manifest["verification"] = result
        manifest_path = backup / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            manifest_path.chmod(0o600)
    return result


def mirror_backup_to_qiniu(folder: str | Path) -> dict[str, Any]:
    """Upload an encrypted database/theme disaster bundle to private Qiniu Kodo."""
    from cryptography.fernet import Fernet
    from .storage import QiniuKodoMediaStorage

    source = Path(folder).resolve()
    manifest = _read_manifest(source)
    secret = os.getenv("BACKUP_ENCRYPTION_KEY", "").strip()
    if len(secret) < 32:
        raise ValueError("BACKUP_ENCRYPTION_KEY 必须至少 32 个字符")
    required = ("QINIU_ACCESS_KEY", "QINIU_SECRET_KEY", "QINIU_BUCKET", "QINIU_S3_REGION")
    values = [os.getenv(name, "").strip() for name in required]
    if not all(values):
        raise ValueError("七牛云备份配置不完整")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        tar.add(source / manifest.get("database_file", "love_moments.db"), arcname="love_moments.db")
        tar.add(source / "manifest.json", arcname="manifest.json")
        if (source / "theme-assets").is_dir():
            tar.add(source / "theme-assets", arcname="theme-assets")
    key = hashlib.sha256(secret.encode("utf-8")).digest()
    import base64
    encrypted = Fernet(base64.urlsafe_b64encode(key)).encrypt(archive.getvalue())
    storage = QiniuKodoMediaStorage(*values, endpoint=os.getenv("QINIU_S3_ENDPOINT", ""))
    prefix = os.getenv("BACKUP_QINIU_PREFIX", "private-disaster-backups").strip().strip("/") or "private-disaster-backups"
    object_key = f"{prefix}/{source.name}.tar.gz.fernet"
    stored = storage.put(object_key, encrypted, "application/octet-stream")
    downloaded = storage.get(object_key)
    checksum = hashlib.sha256(encrypted).hexdigest()
    if hashlib.sha256(downloaded).hexdigest() != checksum:
        storage.delete(object_key)
        raise RuntimeError("七牛云灾备上传后的校验失败")
    return {"object_key": stored.key, "bytes": stored.size, "sha256": checksum, "verified": True}


def prune_automatic_backups(
    root: str | Path,
    *,
    now: datetime | None = None,
    daily_keep: int = 7,
    weekly_keep: int = 4,
) -> dict[str, list[str]]:
    """Keep one automatic backup per recent day, then one per older ISO week."""
    backup_root = Path(root).resolve()
    reference = (now or datetime.now(timezone.utc)).astimezone()
    automatic: list[tuple[Path, datetime]] = []
    for folder in backup_root.glob("love-moments-*") if backup_root.is_dir() else []:
        try:
            manifest = _read_manifest(folder)
            if manifest.get("source") != "automatic":
                continue
            created = datetime.fromisoformat(str(manifest["created_at"]).replace("Z", "+00:00")).astimezone()
            automatic.append((folder, created))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    automatic.sort(key=lambda item: item[1], reverse=True)
    keep: set[Path] = set()
    daily_cutoff = reference.date() - timedelta(days=max(0, daily_keep - 1))
    seen_days: set[object] = set()
    for folder, created in automatic:
        if created.date() >= daily_cutoff and created.date() not in seen_days:
            keep.add(folder)
            seen_days.add(created.date())
    seen_weeks: set[tuple[int, int]] = set()
    for folder, created in automatic:
        if folder in keep or created.date() >= daily_cutoff:
            continue
        iso = created.isocalendar()
        week = (iso.year, iso.week)
        if week not in seen_weeks and len(seen_weeks) < weekly_keep:
            keep.add(folder)
            seen_weeks.add(week)
    removed: list[str] = []
    for folder, _ in automatic:
        if folder not in keep:
            shutil.rmtree(folder)
            removed.append(folder.name)
    return {"kept": sorted(path.name for path in keep), "removed": sorted(removed)}


def mirror_backup(folder: str | Path, mirror_root: str | Path) -> Path:
    source = Path(folder).resolve()
    _read_manifest(source)
    root = Path(mirror_root).expanduser().resolve()
    if root == source or source in root.parents:
        raise ValueError("镜像目录不能位于源备份目录内部")
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        root.chmod(0o700)
    destination = root / source.name
    if destination == source:
        raise ValueError("镜像目录不能与源备份目录相同")
    temporary = root / f".{source.name}.copying"
    if temporary.exists():
        shutil.rmtree(temporary)
    try:
        shutil.copytree(source, temporary)
        if destination.exists():
            shutil.rmtree(destination)
        temporary.replace(destination)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return destination
