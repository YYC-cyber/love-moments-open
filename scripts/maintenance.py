#!/usr/bin/env python3
"""Local watchdog and scheduled maintenance for the self-hosted app."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from love_moments.config import load_env  # noqa: E402
from love_moments.maintenance import mirror_backup, prune_automatic_backups  # noqa: E402
from love_moments.store import LoveMomentsStore  # noqa: E402
from scripts.backup_local import create_backup  # noqa: E402

SERVER_LABEL = "cn.love-moments.server"


def _resolve(value: str, default: str) -> Path:
    path = Path(value or default).expanduser()
    return path if path.is_absolute() else ROOT / path


def _private(path: Path, mode: int) -> None:
    if os.name != "nt":
        path.chmod(mode)


def _paths() -> tuple[Path, Path, Path, Path]:
    state_root = _resolve(os.getenv("MAINTENANCE_STATE_ROOT", ""), "data/maintenance")
    state_root.mkdir(parents=True, exist_ok=True)
    _private(state_root, 0o700)
    log_root = _resolve(os.getenv("LOG_ROOT", ""), "data/logs")
    log_root.mkdir(parents=True, exist_ok=True)
    _private(log_root, 0o700)
    return state_root, log_root, _resolve(os.getenv("DATABASE_PATH", ""), "data/love_moments.db"), _resolve(os.getenv("MEDIA_ROOT", ""), "data/media")


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _private(temporary, 0o600)
    temporary.replace(path)


def _read_json(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (OSError, json.JSONDecodeError):
        return default


def _log(message: str) -> None:
    _, log_root, _, _ = _paths()
    path = log_root / "maintenance.log"
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with path.open("a", encoding="utf-8") as target:
        target.write(f"{timestamp} {message[:1000]}\n")
    _private(path, 0o600)


def _notify(title: str, message: str) -> None:
    if os.getenv("LOCAL_ADMIN_NOTIFICATIONS", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    if sys.platform != "darwin" or not Path("/usr/bin/osascript").is_file():
        return
    safe_title = str(title).replace("\\", "\\\\").replace('"', '\\"')[:80]
    safe_message = str(message).replace("\\", "\\\\").replace('"', '\\"')[:240]
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", f'display notification "{safe_message}" with title "{safe_title}"'],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return


def health_check() -> dict:
    load_env(ROOT / ".env")
    state_root, _, _, _ = _paths()
    state_path = state_root / "watchdog-state.json"
    previous = _read_json(state_path, {"consecutive_failures": 0})
    port = os.getenv("APP_PORT", "18765").strip() or "18765"
    url = f"http://127.0.0.1:{port}/api/health"
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    healthy = False
    message = ""
    try:
        with request.urlopen(url, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
            healthy = response.status == 200 and body.get("status") == "ok"
            message = "健康接口正常" if healthy else "健康接口返回异常状态"
    except (error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError, OSError) as exc:
        message = f"健康检查失败：{type(exc).__name__}"
    failures = 0 if healthy else int(previous.get("consecutive_failures", 0)) + 1
    state = {
        "checked_at": now,
        "healthy": healthy,
        "consecutive_failures": failures,
        "message": message,
        "last_healthy_at": now if healthy else previous.get("last_healthy_at"),
        "last_failure_at": None if healthy else now,
    }
    if healthy and int(previous.get("consecutive_failures", 0)) >= 3:
        _log("服务已恢复")
        _notify("恋屿服务已恢复", "本机健康检查已经重新通过。")
    elif not healthy:
        _log(message)
        if failures == 3:
            restarted = False
            if os.getenv("WATCHDOG_AUTO_RESTART", "true").strip().lower() in {"1", "true", "yes", "on"} and sys.platform == "darwin":
                result = subprocess.run(
                    ["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{SERVER_LABEL}"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                restarted = result.returncode == 0
            state["restart_requested"] = restarted
            _notify("恋屿服务连续异常", "健康检查连续失败 3 次，已尝试自动恢复。" if restarted else "健康检查连续失败 3 次，请检查电脑与服务日志。")
    _write_json(state_path, state)
    print(json.dumps(state, ensure_ascii=False))
    return state


def daily_maintenance() -> dict:
    load_env(ROOT / ".env")
    state_root, _, database, media_root = _paths()
    backup_root = _resolve(os.getenv("BACKUP_ROOT", ""), "backups")
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    summary: dict = {"started_at": now, "ok": False}
    try:
        destination = create_backup(backup_root, database, media_root, source="automatic")
        retention = prune_automatic_backups(backup_root)
        mirror_name = None
        mirror_value = os.getenv("BACKUP_MIRROR_ROOT", "").strip()
        if mirror_value:
            mirror_name = str(mirror_backup(destination, _resolve(mirror_value, mirror_value)))
        media_checks = LoveMomentsStore(
            database, media_root=media_root, seed_demo_data=False,
        ).check_due_recipe_media(limit=100)
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
            integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = len(db.execute("PRAGMA foreign_key_check").fetchall())
        disk = shutil.disk_usage(database.parent)
        summary.update({
            "ok": integrity == "ok" and foreign_keys == 0,
            "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "backup": destination.name,
            "mirror": mirror_name,
            "retention": retention,
            "recipe_media_checks": media_checks,
            "database_integrity": integrity,
            "foreign_key_violations": foreign_keys,
            "disk_free_bytes": disk.free,
        })
        _log(f"每日维护完成：{destination.name}")
        _notify("恋屿每日维护完成", f"备份与恢复验证通过，可用空间 {disk.free / 1024**3:.1f} GB。")
    except Exception as exc:
        summary.update({
            "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "error_type": type(exc).__name__,
            "message": str(exc)[:300],
        })
        _log(f"每日维护失败：{type(exc).__name__}: {str(exc)[:300]}")
        _notify("恋屿每日维护失败", "自动备份或恢复验证未完成，请打开后台查看。")
    _write_json(state_root / "daily-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="恋屿本机守护与每日维护")
    parser.add_argument("--task", choices=("health", "daily"), required=True)
    args = parser.parse_args()
    result = health_check() if args.task == "health" else daily_maintenance()
    raise SystemExit(0 if result.get("healthy", result.get("ok", False)) else 1)


if __name__ == "__main__":
    main()
