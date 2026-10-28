#!/usr/bin/env python3
"""Render, install or remove the three per-user macOS LaunchAgents."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABELS = {
    "server": "cn.love-moments.server",
    "watchdog": "cn.love-moments.watchdog",
    "backup": "cn.love-moments.backup",
}


def _launcher_path(path: Path) -> Path:
    """Return an absolute launcher path without dereferencing venv symlinks."""
    return Path(os.path.abspath(path.expanduser()))


def definitions(python: Path) -> dict[str, dict]:
    logs = ROOT / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        logs.chmod(0o700)
    common = {
        "WorkingDirectory": str(ROOT),
        "ProcessType": "Background",
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    return {
        "server": {
            **common,
            "Label": LABELS["server"],
            "ProgramArguments": ["/usr/bin/caffeinate", "-i", str(python), str(ROOT / "main.py")],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "StandardOutPath": str(logs / "server.stdout.log"),
            "StandardErrorPath": str(logs / "server.stderr.log"),
        },
        "watchdog": {
            **common,
            "Label": LABELS["watchdog"],
            "ProgramArguments": [str(python), str(ROOT / "scripts" / "maintenance.py"), "--task", "health"],
            "RunAtLoad": True,
            "StartInterval": 300,
            "StandardOutPath": str(logs / "watchdog.stdout.log"),
            "StandardErrorPath": str(logs / "watchdog.stderr.log"),
        },
        "backup": {
            **common,
            "Label": LABELS["backup"],
            "ProgramArguments": [str(python), str(ROOT / "scripts" / "maintenance.py"), "--task", "daily"],
            "StartCalendarInterval": {"Hour": 3, "Minute": 30},
            "StandardOutPath": str(logs / "backup.stdout.log"),
            "StandardErrorPath": str(logs / "backup.stderr.log"),
        },
    }


def render(output: Path, python: Path) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        output.chmod(0o700)
    paths = []
    for name, value in definitions(python).items():
        path = output / f"{LABELS[name]}.plist"
        with path.open("wb") as target:
            plistlib.dump(value, target, sort_keys=True)
        if os.name != "nt":
            path.chmod(0o600)
        paths.append(path)
    return paths


def install(python: Path) -> None:
    if sys.platform != "darwin":
        raise SystemExit("launchd 只适用于 macOS")
    destination = Path.home() / "Library" / "LaunchAgents"
    paths = render(destination, python)
    domain = f"gui/{os.getuid()}"
    for path in paths:
        subprocess.run(["/bin/launchctl", "bootout", domain, str(path)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["/bin/launchctl", "bootstrap", domain, str(path)], check=True)
    print("已安装并启动恋屿服务守护、健康检查和每日备份。")


def uninstall() -> None:
    if sys.platform != "darwin":
        raise SystemExit("launchd 只适用于 macOS")
    destination = Path.home() / "Library" / "LaunchAgents"
    domain = f"gui/{os.getuid()}"
    for label in LABELS.values():
        path = destination / f"{label}.plist"
        subprocess.run(["/bin/launchctl", "bootout", domain, str(path)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        path.unlink(missing_ok=True)
    print("已卸载恋屿 LaunchAgent，项目数据和备份未删除。")


def main() -> None:
    parser = argparse.ArgumentParser(description="安装恋屿 macOS 自动启动与维护任务")
    parser.add_argument("action", choices=("render", "install", "uninstall"))
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "launchd")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    # Keep a virtual environment launcher path intact. ``Path.resolve()``
    # dereferences ``.venv/bin/python`` to the base interpreter, which makes a
    # LaunchAgent lose the virtual environment's site-packages (for example
    # Pillow) after installation.
    python = _launcher_path(args.python)
    if args.action == "render":
        for path in render(args.output.resolve(), python):
            print(path)
    elif args.action == "install":
        install(python)
    else:
        uninstall()


if __name__ == "__main__":
    main()
