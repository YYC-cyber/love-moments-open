"""Versioned, data-driven physical bead colour catalogues.

The web client deliberately does not own colour numbers.  Bundled reference
catalogues and optional operator-provided catalogues use the same validated
JSON format, so adding or correcting a real brand does not require changing
JavaScript.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


_ID = re.compile(r"[a-z0-9][a-z0-9._-]{1,63}")
_HEX = re.compile(r"#[0-9A-Fa-f]{6}")
_CODE = re.compile(r"[^\s]{1,24}")


class BeadPaletteError(ValueError):
    """Raised when a catalogue cannot safely be used."""


@dataclass(frozen=True)
class BeadPaletteRepository:
    bundled_root: Path
    override_root: Path | None = None

    @classmethod
    def from_environment(cls, app_root: Path) -> "BeadPaletteRepository":
        configured = os.getenv("BEAD_PALETTE_ROOT", "").strip()
        return cls(
            app_root / "love_moments" / "data" / "bead_palettes",
            Path(configured).expanduser() if configured else None,
        )

    def _files(self) -> list[Path]:
        by_name: dict[str, Path] = {}
        for root in (self.bundled_root, self.override_root):
            if root is None or not root.is_dir() or root.is_symlink():
                continue
            for path in sorted(root.glob("*.json")):
                if path.is_file() and not path.is_symlink():
                    by_name[path.name] = path
        return list(by_name.values())

    @staticmethod
    def _validate(payload: object, source: Path) -> dict:
        if not isinstance(payload, dict):
            raise BeadPaletteError(f"{source.name}: 色卡必须是 JSON 对象")
        palette_id = str(payload.get("id", "")).strip().lower()
        if not _ID.fullmatch(palette_id):
            raise BeadPaletteError(f"{source.name}: id 格式无效")
        name = str(payload.get("name", "")).strip()
        brand = str(payload.get("brand", "")).strip()
        version = str(payload.get("version", "")).strip()
        if not name or not brand or not version:
            raise BeadPaletteError(f"{source.name}: name、brand、version 不能为空")
        try:
            diameter = float(payload.get("bead_size_mm"))
            board_pitch = float(payload.get("board_pitch_mm", diameter))
        except (TypeError, ValueError) as error:
            raise BeadPaletteError(f"{source.name}: 拼豆尺寸必须是数字") from error
        if not 1.5 <= diameter <= 10 or not 1.5 <= board_pitch <= 10:
            raise BeadPaletteError(f"{source.name}: 拼豆尺寸超出合理范围")
        raw_colors = payload.get("colors")
        if not isinstance(raw_colors, list) or not 8 <= len(raw_colors) <= 600:
            raise BeadPaletteError(f"{source.name}: 色卡颜色数量必须在 8～600 之间")
        colors: list[dict] = []
        seen_codes: set[str] = set()
        for index, item in enumerate(raw_colors):
            if not isinstance(item, dict):
                raise BeadPaletteError(f"{source.name}: 第 {index + 1} 个颜色格式无效")
            code = str(item.get("code", "")).strip()
            color_name = str(item.get("name", "")).strip()
            color_hex = str(item.get("hex", "")).strip().upper()
            if not _CODE.fullmatch(code) or not color_name or not _HEX.fullmatch(color_hex):
                raise BeadPaletteError(f"{source.name}: 第 {index + 1} 个颜色缺少有效色号、名称或 HEX")
            if code.casefold() in seen_codes:
                raise BeadPaletteError(f"{source.name}: 色号 {code} 重复")
            seen_codes.add(code.casefold())
            colors.append({
                "code": code,
                "name": color_name[:48],
                "hex": color_hex,
                "common": bool(item.get("common", True)),
                "family": str(item.get("family", "")).strip()[:24],
            })
        kind = str(payload.get("color_value_kind", "engineering_reference")).strip()
        if kind not in {"engineering_reference", "official_digital", "measured_physical"}:
            raise BeadPaletteError(f"{source.name}: color_value_kind 无效")
        return {
            "schema_version": "1.0",
            "id": palette_id,
            "name": name[:80],
            "brand": brand[:60],
            "series": str(payload.get("series", "")).strip()[:60],
            "version": version[:32],
            "bead_size_mm": diameter,
            "board_pitch_mm": board_pitch,
            "default": bool(payload.get("default", False)),
            "color_value_kind": kind,
            "verified": kind in {"official_digital", "measured_physical"} and bool(payload.get("verified", False)),
            "source_label": str(payload.get("source_label", "自定义色卡")).strip()[:100],
            "notice": str(payload.get("notice", "")).strip()[:240],
            "colors": colors,
        }

    def _load_all(self) -> list[dict]:
        catalogues: dict[str, dict] = {}
        errors: list[str] = []
        for path in self._files():
            try:
                with path.open("r", encoding="utf-8") as stream:
                    palette = self._validate(json.load(stream), path)
                catalogues[palette["id"]] = palette
            except (OSError, json.JSONDecodeError, BeadPaletteError) as error:
                errors.append(str(error))
        if not catalogues:
            detail = f"：{'；'.join(errors)}" if errors else ""
            raise BeadPaletteError(f"没有可用的拼豆色卡{detail}")
        values = sorted(catalogues.values(), key=lambda item: (not item["default"], item["name"]))
        if sum(1 for item in values if item["default"]) > 1:
            first = next(item for item in values if item["default"])
            for item in values:
                item["default"] = item["id"] == first["id"]
        return values

    @staticmethod
    def _summary(palette: dict) -> dict:
        return {key: value for key, value in palette.items() if key != "colors"} | {
            "color_count": len(palette["colors"]),
        }

    def list(self) -> dict:
        palettes = self._load_all()
        return {
            "default_id": next((item["id"] for item in palettes if item["default"]), palettes[0]["id"]),
            "items": [self._summary(item) for item in palettes],
        }

    def get(self, palette_id: str) -> dict:
        if not _ID.fullmatch(str(palette_id).strip().lower()):
            raise KeyError("色卡不存在")
        for palette in self._load_all():
            if palette["id"] == palette_id:
                return palette
        raise KeyError("色卡不存在")
