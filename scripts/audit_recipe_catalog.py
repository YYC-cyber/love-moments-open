#!/usr/bin/env python3
"""Print the deterministic, per-recipe quality audit for the offline catalog."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from love_moments.recipe_catalog_quality import audit_normalized_catalog
from love_moments.recipes import load_builtin_recipes


def main() -> int:
    recipes = load_builtin_recipes()
    reports = audit_normalized_catalog(recipes)
    categories = Counter(recipe["category"] for recipe in recipes)
    print(f"菜谱总数: {len(recipes)}")
    print(f"分类数量: {dict(sorted(categories.items()))}")
    print("逐道审核结果:")
    for index, report in enumerate(reports, 1):
        checks = "、".join(report["checks"])
        print(f"PASS {index:03d} {report['id']} {report['name']} [{checks}]")
    print(f"结论: {len(reports)}/{len(recipes)} 道通过，0 道跳过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
