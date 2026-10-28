"""Verify that local Markdown links in project documentation resolve."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def main() -> int:
    private_docs_root = ROOT / ".docs"
    private_docs_available = private_docs_root.is_dir()
    documents = [ROOT / "README.md", *sorted(private_docs_root.rglob("*.md"))]
    checked = 0
    missing: list[str] = []
    for document in documents:
        for raw_target in LINK.findall(document.read_text(encoding="utf-8")):
            target = raw_target.strip().strip("<>").split(maxsplit=1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative = unquote(target.split("#", 1)[0])
            if not relative:
                continue
            # `.docs` is intentionally kept as a local/private project folder
            # by the repository pre-push policy.  Validate every one of those
            # links in maintainer worktrees where the folder exists, while
            # allowing a clean remote checkout to run the same CI check.
            if document == ROOT / "README.md" and relative.startswith(".docs/") and not private_docs_available:
                continue
            checked += 1
            destination = (document.parent / relative).resolve()
            if not destination.exists():
                missing.append(f"{document.relative_to(ROOT)} -> {target}")
    if missing:
        print("Broken local documentation links:")
        print("\n".join(f"- {item}" for item in missing))
        return 1
    print(f"OK: {len(documents)} documents, {checked} local links")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
