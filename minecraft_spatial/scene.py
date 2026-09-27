"""Validation and local JSON I/O for Scene v1 artifacts."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


class SceneError(ValueError):
    """Raised when a Scene v1 document is malformed or ambiguous."""


_RESOURCE_ID = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_./-]+$")
_AIR_IDS = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}


def _json_value(value: Any, where: str = "provenance") -> None:
    if value is None or type(value) in (str, int, float, bool):
        if type(value) is float and (value != value or value in (float("inf"), float("-inf"))):
            raise SceneError(f"{where} contains a non-finite number")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _json_value(item, f"{where}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str:
                raise SceneError(f"{where} keys must be strings")
            _json_value(item, f"{where}.{key}")
        return
    raise SceneError(f"{where} contains a non-JSON value")


def _vec3(value: Any, where: str) -> list[int]:
    if type(value) is not list or len(value) != 3:
        raise SceneError(f"{where} must be a three-item JSON array")
    if any(type(part) is not int for part in value):
        raise SceneError(f"{where} coordinates must be integers")
    return value


def _resource_id(value: Any, where: str) -> str:
    if type(value) is not str or not _RESOURCE_ID.fullmatch(value):
        raise SceneError(f"{where} must be a namespaced identifier")
    if any(part in (".", "..") for part in value.split(":", 1)[1].split("/")):
        raise SceneError(f"{where} contains a path traversal component")
    return value


def validate_scene(scene: Any) -> dict[str, Any]:
    """Validate and return a Scene v1 mapping without changing its coordinates."""
    if type(scene) is not dict:
        raise SceneError("scene must be a JSON object")
    required = {
        "schema_version",
        "dimension",
        "bounds",
        "palette",
        "blocks",
        "unknown_chunks",
        "provenance",
        "kind",
    }
    if set(scene) != required:
        missing = sorted(required - set(scene))
        extra = sorted(set(scene) - required)
        raise SceneError(f"scene fields mismatch (missing={missing}, extra={extra})")
    if type(scene["schema_version"]) is not int or scene["schema_version"] != 1:
        raise SceneError("schema_version must be integer 1")
    _resource_id(scene["dimension"], "dimension")
    if scene["kind"] not in ("world", "blueprint") or type(scene["kind"]) is not str:
        raise SceneError("kind must be 'world' or 'blueprint'")

    bounds = scene["bounds"]
    if type(bounds) is not dict or set(bounds) != {"min", "max"}:
        raise SceneError("bounds must contain exactly min and max")
    lo = _vec3(bounds["min"], "bounds.min")
    hi = _vec3(bounds["max"], "bounds.max")
    if any(lo[i] > hi[i] for i in range(3)):
        raise SceneError("bounds.min must not exceed bounds.max")

    palette = scene["palette"]
    if type(palette) is not list:
        raise SceneError("palette must be an array")
    palette_keys: set[str] = set()
    for i, item in enumerate(palette):
        where = f"palette[{i}]"
        if type(item) is not dict or set(item) != {"name", "properties"}:
            raise SceneError(f"{where} must contain exactly name and properties")
        name = _resource_id(item["name"], f"{where}.name")
        if name in _AIR_IDS:
            raise SceneError(f"{where} is air; Scene blocks contain non-air blocks only")
        properties = item["properties"]
        if type(properties) is not dict or any(
            type(key) is not str or type(value) is not str for key, value in properties.items()
        ):
            raise SceneError(f"{where}.properties must map strings to strings")
        key = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if key in palette_keys:
            raise SceneError(f"{where} duplicates an earlier block state")
        palette_keys.add(key)

    unknown = scene["unknown_chunks"]
    if type(unknown) is not list:
        raise SceneError("unknown_chunks must be an array")
    unknown_keys: set[tuple[int, int]] = set()
    for i, item in enumerate(unknown):
        where = f"unknown_chunks[{i}]"
        if type(item) is not dict or set(item) != {"x", "z", "reason"}:
            raise SceneError(f"{where} must contain exactly x, z, and reason")
        cx, cz = item["x"], item["z"]
        if type(cx) is not int or type(cz) is not int or type(item["reason"]) is not str or not item["reason"]:
            raise SceneError(f"{where} has invalid coordinates or reason")
        if (cx, cz) in unknown_keys:
            raise SceneError(f"{where} duplicates an earlier unknown chunk")
        if cx * 16 > hi[0] or cx * 16 + 15 < lo[0] or cz * 16 > hi[2] or cz * 16 + 15 < lo[2]:
            raise SceneError(f"{where} does not intersect scene bounds")
        unknown_keys.add((cx, cz))

    blocks = scene["blocks"]
    if type(blocks) is not list:
        raise SceneError("blocks must be an array")
    seen_positions: set[tuple[int, int, int]] = set()
    for i, item in enumerate(blocks):
        where = f"blocks[{i}]"
        if type(item) is not dict or set(item) != {"pos", "palette"}:
            raise SceneError(f"{where} must contain exactly pos and palette")
        pos = _vec3(item["pos"], f"{where}.pos")
        if any(pos[axis] < lo[axis] or pos[axis] > hi[axis] for axis in range(3)):
            raise SceneError(f"{where}.pos is outside inclusive scene bounds")
        palette_index = item["palette"]
        if type(palette_index) is not int or not 0 <= palette_index < len(palette):
            raise SceneError(f"{where}.palette is outside the palette")
        key = tuple(pos)
        if key in seen_positions:
            raise SceneError(f"{where}.pos duplicates an earlier block")
        if (pos[0] // 16, pos[2] // 16) in unknown_keys:
            raise SceneError(f"{where}.pos lies in an unknown chunk")
        seen_positions.add(key)

    provenance = scene["provenance"]
    if type(provenance) is not dict:
        raise SceneError("provenance must be a JSON object")
    _json_value(provenance)
    return scene


def load_scene(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load JSON from a local path and validate it as Scene v1."""
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise SceneError(f"cannot load Scene JSON: {exc}") from exc
    return validate_scene(value)


def save_scene(scene: Any, path: str | os.PathLike[str]) -> Path:
    """Validate and atomically save a local Scene JSON artifact."""
    checked = validate_scene(scene)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=target.parent,
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = stream.name
            json.dump(checked, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return target
