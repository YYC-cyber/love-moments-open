"""Create an owner-only local backup without copying service credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from love_moments.config import load_env  # noqa: E402


def _resolve(value: str, default: str) -> Path:
    path = Path(value or default)
    return path if path.is_absolute() else ROOT / path


def _private(path: Path, mode: int) -> None:
    if os.name != "nt":
        path.chmod(mode)


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ignore_ephemeral_album_exports(directory: str, names: list[str]) -> set[str]:
    path = Path(directory)
    ignored = {name for name in names if name in {".media-access-key", ".web-push-vapid-private.pem"}}
    if path.name == "temp" and "album_exports" in names:
        ignored.add("album_exports")
    return ignored


def create_backup(
    output_root: Path | None = None,
    database_path: Path | None = None,
    media_root: Path | None = None,
    theme_root: Path | None = None,
    source: str = "manual",
) -> Path:
    load_env(ROOT / ".env")
    database = (database_path or _resolve(os.getenv("DATABASE_PATH", ""), "data/love_moments.db")).resolve()
    media = (media_root or _resolve(os.getenv("MEDIA_ROOT", ""), "data/media")).resolve()
    themes = (theme_root or database.parent / "theme-assets").resolve()
    if not database.is_file():
        raise FileNotFoundError(f"数据库不存在：{database}")

    root = (output_root or ROOT / "backups").resolve()
    if root == media or media in root.parents:
        raise ValueError("备份目录不能位于媒体目录内部")
    root.mkdir(parents=True, exist_ok=True)
    _private(root, 0o700)
    source = source if source in {"manual", "automatic"} else "manual"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = root / f"love-moments-{source}-{timestamp}"
    working = root / f".{destination.name}.copying"
    working.mkdir()
    _private(working, 0o700)

    try:
        database_copy = working / "love_moments.db"
        source_connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        target_connection = sqlite3.connect(database_copy)
        try:
            source_connection.backup(target_connection)
            integrity = target_connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            target_connection.close()
            source_connection.close()
        if integrity != "ok":
            raise RuntimeError(f"备份完整性检查失败：{integrity}")
        _private(database_copy, 0o600)

        media_count = 0
        media_bytes = 0
        media_objects = []
        media_copy = working / "media"
        if media.is_dir():
            shutil.copytree(media, media_copy, ignore=_ignore_ephemeral_album_exports)
            for path in media_copy.rglob("*"):
                if path.is_dir():
                    _private(path, 0o700)
                elif path.is_file():
                    media_count += 1
                    media_bytes += path.stat().st_size
                    media_objects.append({
                        "path": path.relative_to(media_copy).as_posix(),
                        "size": path.stat().st_size,
                        "sha256": _checksum(path),
                    })
                    _private(path, 0o600)

        theme_count = 0
        theme_bytes = 0
        theme_objects = []
        theme_copy = working / "theme-assets"
        if themes.is_dir():
            shutil.copytree(themes, theme_copy)
            for path in theme_copy.rglob("*"):
                if path.is_dir():
                    _private(path, 0o700)
                elif path.is_file():
                    theme_count += 1
                    theme_bytes += path.stat().st_size
                    theme_objects.append({
                        "path": path.relative_to(theme_copy).as_posix(),
                        "size": path.stat().st_size,
                        "sha256": _checksum(path),
                    })
                    _private(path, 0o600)

        manifest = {
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "source": source,
            "database_file": database_copy.name,
            "database_sha256": _checksum(database_copy),
            "database_integrity": integrity,
            "media_files": media_count,
            "media_bytes": media_bytes,
            "media_objects": media_objects,
            "theme_files": theme_count,
            "theme_bytes": theme_bytes,
            "theme_objects": theme_objects,
            "private_theme_assets_included": theme_count > 0,
            "includes_env": False,
            "service_secrets_included": False,
            "ephemeral_album_exports_included": False,
        }
        manifest_path = working / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _private(manifest_path, 0o600)
        from love_moments.maintenance import verify_backup

        verification = verify_backup(working)
        if not verification["ok"]:
            raise RuntimeError("备份恢复演练失败")
        working.replace(destination)
    except Exception:
        if working.exists():
            shutil.rmtree(working)
        raise
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="备份恋屿本地数据库与媒体文件")
    parser.add_argument("--output", type=Path, help="备份根目录；默认使用项目 backups 目录")
    parser.add_argument("--source", choices=("manual", "automatic"), default="manual", help="备份来源")
    args = parser.parse_args()
    print(create_backup(args.output, source=args.source).resolve())


if __name__ == "__main__":
    main()
