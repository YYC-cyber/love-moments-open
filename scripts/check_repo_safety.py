#!/usr/bin/env python3
"""Fail when Git tracks private runtime data or likely production credentials."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ENV_FILES = {".env.example"}
FORBIDDEN_TOP_LEVEL = {
    "backups",
    "tmp",
    ".idea",
    ".venv",
    "node_modules",
    ".docs",
    ".planning",
}
ALLOWED_RUNTIME_FILES = {"data/.gitkeep"}
FORBIDDEN_PERSONAL_SCRIPTS = {"prepare_personal_release.py", "test_remote_production.cjs"}
FORBIDDEN_DATA_PREFIXES = ("data/media/", "data/logs/", "data/maintenance/")
FORBIDDEN_SUFFIXES = (
    ".db",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite3",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)

SECRET_PATTERNS = (
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("OpenAI-compatible key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
)
ASSIGNMENT = re.compile(
    r"(?m)^[ \t]*(?:export[ \t]+)?"
    r"([A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN|CERTIFICATE|PASSWORD)[A-Z0-9_]*)"
    # Match literal credential-shaped values, not Python expressions such as
    # ``CATEGORY_KEYS = frozenset(CATEGORY_LABELS)``.
    r"[ \t]*=[ \t]*['\"]?([A-Za-z0-9_./+=:-]{16,})"
)
PLACEHOLDER_PARTS = ("example", "placeholder", "changeme", "your_", "test_", "dummy")


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def forbidden_path(path: str) -> str | None:
    pure = PurePosixPath(path)
    if pure.name in FORBIDDEN_PERSONAL_SCRIPTS:
        return "personal deployment script"
    if path.startswith("data/") and path not in ALLOWED_RUNTIME_FILES:
        return "runtime data"
    if pure.parts and pure.parts[0] in FORBIDDEN_TOP_LEVEL:
        return "private/runtime directory"
    if path.startswith(FORBIDDEN_DATA_PREFIXES):
        return "private data directory"
    if pure.name == ".env" or (pure.name.startswith(".env.") and pure.name not in ALLOWED_ENV_FILES):
        return "environment file"
    if pure.name == "serpapi-usage.json":
        return "external API usage state"
    if pure.name.startswith(".love_moments.db") or path.endswith(FORBIDDEN_SUFFIXES):
        return "database or private key material"
    return None


def scan_text(path: str) -> list[str]:
    file_path = ROOT / path
    if file_path.is_symlink():
        return ["symlink outside the reviewed source boundary"]
    if not file_path.is_file() or file_path.stat().st_size > 5_000_000:
        return []
    try:
        text = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    findings: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(label)
    for match in ASSIGNMENT.finditer(text):
        value = match.group(2).lower()
        if not any(part in value for part in PLACEHOLDER_PARTS):
            findings.append(f"non-empty {match.group(1)} assignment")
            break
    return findings


def main() -> int:
    files = tracked_files()
    failures: list[str] = []
    for path in files:
        reason = forbidden_path(path)
        if reason:
            failures.append(f"{path}: {reason}")
            continue
        for finding in scan_text(path):
            failures.append(f"{path}: possible {finding}")

    if failures:
        print("Repository safety check failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"Repository safety check passed: {len(files)} tracked files inspected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
