"""Conversion between mc-builder operation plans and Minecraft Spatial scenes.

This module only creates local design files. It has no server or builder client.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .scene import validate_scene


MAX_BLUEPRINT_BYTES = 2 * 1024 * 1024
MAX_OPERATIONS = 10_000
MAX_EXPANDED_BLOCKS = 100_000
DEFAULT_MAX_BLOCKS = 10_000
_DIMENSION = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_./-]+$")
_BLOCK = _DIMENSION
_PROPERTY_KEY = re.compile(r"^[a-z0-9_]{1,40}$")
_PROPERTY_VALUE = re.compile(r"^[a-z0-9_-]{1,40}$")


class BlueprintError(ValueError):
    """An input design cannot be represented as a safe mc-builder blueprint."""


def _position(value: Any, field: str) -> tuple[int, int, int]:
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or any(type(component) is not int for component in value)):
        raise BlueprintError(f"{field} must contain exactly three integer coordinates")
    return tuple(value)


def _block_state(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 16:
        raise BlueprintError("state must be an object with at most 16 string properties")
    if any(not isinstance(key, str) or not _PROPERTY_KEY.fullmatch(key)
           or not isinstance(prop, str) or not _PROPERTY_VALUE.fullmatch(prop)
           for key, prop in value.items()):
        raise BlueprintError("state keys and values must use simple lowercase strings")
    return dict(sorted(value.items()))


def _material(value: Any) -> str:
    if not isinstance(value, str) or not _BLOCK.fullmatch(value):
        raise BlueprintError("block must be a namespaced block ID")
    if value.rsplit(":", 1)[1] in {"air", "cave_air", "void_air"}:
        raise BlueprintError("air targets cannot be exported as placements; blueprints do not clear blocks")
    return value


def _freeze_state(state: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(state.items()))


def expand_operations(
    operations: Any,
    *,
    max_blocks: int = DEFAULT_MAX_BLOCKS,
    max_expanded_blocks: int = MAX_EXPANDED_BLOCKS,
) -> dict[tuple[int, int, int], tuple[str, dict[str, str]]]:
    """Expand inclusive cuboids using mc-builder's last-operation-wins rule."""
    if (not isinstance(operations, list) or not 1 <= len(operations) <= MAX_OPERATIONS):
        raise BlueprintError(f"operations must contain 1–{MAX_OPERATIONS} entries")
    if type(max_blocks) is not int or max_blocks < 1:
        raise BlueprintError("max_blocks must be a positive integer")

    placements: dict[tuple[int, int, int], tuple[str, dict[str, str]]] = {}
    expanded = 0
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise BlueprintError(f"operation {index} must be an object")
        material = _material(operation.get("block"))
        state = _block_state(operation.get("state", {}))
        lo = _position(operation.get("from"), f"operation {index} from")
        hi = _position(operation.get("to", lo), f"operation {index} to")
        sizes = tuple(b - a + 1 for a, b in zip(lo, hi))
        if min(sizes) < 1:
            raise BlueprintError(f"operation {index} has reversed bounds")
        operation_volume = sizes[0] * sizes[1] * sizes[2]
        expanded += operation_volume
        if expanded > max_expanded_blocks:
            raise BlueprintError("operations expand beyond the configured work limit")
        hollow = operation.get("hollow", False)
        if type(hollow) is not bool:
            raise BlueprintError(f"operation {index} hollow must be a boolean")

        for x in range(lo[0], hi[0] + 1):
            for y in range(lo[1], hi[1] + 1):
                for z in range(lo[2], hi[2] + 1):
                    if hollow and all(a < value < b for a, value, b in zip(lo, (x, y, z), hi)):
                        continue
                    placements[(x, y, z)] = (material, state)
                    if len(placements) > max_blocks:
                        raise BlueprintError("blueprint exceeds max_blocks")
    if not placements:
        raise BlueprintError("blueprint contains no placed blocks")
    return placements


def plan_to_scene(
    plan: Mapping[str, Any],
    *,
    source_sha256: str | None = None,
    max_blocks: int = DEFAULT_MAX_BLOCKS,
) -> dict[str, Any]:
    """Convert an mc-builder JSON plan into a validated design Scene v1."""
    if not isinstance(plan, Mapping):
        raise BlueprintError("plan must be a JSON object")
    dimension = plan.get("dimension")
    if not isinstance(dimension, str) or not _DIMENSION.fullmatch(dimension):
        raise BlueprintError("dimension must be a namespaced dimension ID")

    unknown = plan.get("unknown_chunks", [])
    if not isinstance(unknown, list):
        raise BlueprintError("unknown_chunks must be a list")
    if unknown:
        raise BlueprintError("plan contains unknown chunks; resolve coverage before blueprint export")

    replace_source_water = plan.get("replace_source_water", False)
    if type(replace_source_water) is not bool:
        raise BlueprintError("replace_source_water must be a boolean")
    if replace_source_water and dimension != "minecraft:overworld":
        raise BlueprintError("source-water replacement is only valid in the overworld")
    replace_blocks = plan.get("replace_blocks", [])
    if not isinstance(replace_blocks, list) or len(replace_blocks) > 20:
        raise BlueprintError("replace_blocks must be a unique list of at most 20 block IDs")
    replace_blocks = [_material(value) for value in replace_blocks]
    if len(set(replace_blocks)) != len(replace_blocks):
        raise BlueprintError("replace_blocks must be a unique list of at most 20 block IDs")

    placements = expand_operations(plan.get("operations"), max_blocks=max_blocks)
    palette_keys = sorted({
        (material, _freeze_state(state)) for material, state in placements.values()
    })
    palette = [
        {"name": material, "properties": dict(state)}
        for material, state in palette_keys
    ]
    palette_index = {key: i for i, key in enumerate(palette_keys)}
    blocks = [
        {"pos": list(pos), "palette": palette_index[(material, _freeze_state(state))]}
        for pos, (material, state) in sorted(
            placements.items(), key=lambda item: (item[0][1], item[0][0], item[0][2])
        )
    ]
    positions = [tuple(block["pos"]) for block in blocks]
    source_digest = source_sha256 or hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    provenance: dict[str, Any] = {
        "source": "mc-builder-plan",
        "source_format": "mc-builder-operations",
        "source_sha256": source_digest,
        "evidence": "design-only; no world site was inspected",
        "source_snapshot_id": None,
        "mc_builder_options": {
            "replace_source_water": replace_source_water,
            "replace_blocks": replace_blocks,
        },
    }
    scene = {
        "schema_version": 1,
        "dimension": dimension,
        "bounds": {
            "min": [min(pos[i] for pos in positions) for i in range(3)],
            "max": [max(pos[i] for pos in positions) for i in range(3)],
        },
        "palette": palette,
        "blocks": blocks,
        "unknown_chunks": [],
        "provenance": provenance,
        "kind": "blueprint",
    }
    return validate_scene(scene)


def scene_to_plan(scene: Mapping[str, Any]) -> dict[str, Any]:
    """Export a known blueprint Scene as compact, inclusive mc-builder operations."""
    checked = validate_scene(dict(scene))
    if checked.get("kind") != "blueprint":
        raise BlueprintError("only a Scene with kind='blueprint' can be exported")
    if checked.get("unknown_chunks"):
        raise BlueprintError("scene contains unknown chunks; unknown cannot be treated as air")

    palette = checked["palette"]
    blocks_by_x: dict[tuple[int, int, str, tuple[tuple[str, str], ...]], list[int]] = {}
    for item in checked["blocks"]:
        pos = _position(item.get("pos"), "scene block pos")
        index = item.get("palette")
        if type(index) is not int or not 0 <= index < len(palette):
            raise BlueprintError("scene block references an invalid palette entry")
        entry = palette[index]
        material = _material(entry.get("name"))
        state = _block_state(entry.get("properties", {}))
        key = (pos[1], pos[2], material, _freeze_state(state))
        blocks_by_x.setdefault(key, []).append(pos[0])

    operations: list[dict[str, Any]] = []
    for (y, z, material, frozen_state), xs in sorted(blocks_by_x.items()):
        ordered_xs = sorted(set(xs))
        start = previous = ordered_xs[0]
        for x in ordered_xs[1:] + [None]:
            if x is not None and x == previous + 1:
                previous = x
                continue
            operation: dict[str, Any] = {
                "from": [start, y, z],
                "to": [previous, y, z],
                "block": material,
            }
            if frozen_state:
                operation["state"] = dict(frozen_state)
            operations.append(operation)
            if x is not None:
                start = previous = x

    if not operations or len(operations) > MAX_OPERATIONS:
        raise BlueprintError("exported plan has no operations or exceeds mc-builder's operation limit")
    result: dict[str, Any] = {
        "dimension": checked["dimension"],
        "operations": operations,
    }
    provenance = checked.get("provenance", {})
    options = provenance.get("mc_builder_options", {}) if isinstance(provenance, dict) else {}
    if not isinstance(options, dict):
        options = {}
    if options.get("replace_source_water") is True:
        result["replace_source_water"] = True
    replace_blocks = options.get("replace_blocks", [])
    if replace_blocks:
        result["replace_blocks"] = list(replace_blocks)
    return result


def load_plan(path: str | Path) -> tuple[dict[str, Any], str]:
    """Read a local mc-builder JSON plan and return its content hash."""
    source = Path(path)
    raw = source.read_bytes()
    if len(raw) > MAX_BLUEPRINT_BYTES:
        raise BlueprintError(f"plan exceeds {MAX_BLUEPRINT_BYTES} bytes")
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BlueprintError(f"invalid JSON plan: {exc}") from exc
    if not isinstance(parsed, dict):
        raise BlueprintError("plan root must be a JSON object")
    return parsed, hashlib.sha256(raw).hexdigest()


def dump_plan(plan: Mapping[str, Any], path: str | Path) -> Path:
    """Write a stable mc-builder plan JSON document."""
    target = Path(path)
    payload = (json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > MAX_BLUEPRINT_BYTES:
        raise BlueprintError(f"generated plan exceeds mc-builder's {MAX_BLUEPRINT_BYTES}-byte limit")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target
