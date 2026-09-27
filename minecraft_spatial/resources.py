"""Offline resolution of Minecraft's static blockstate and model JSON assets.

This deliberately implements a bounded, useful subset of the client model
format.  ``resolved`` means that this static JSON subset was resolved; it does
not mean that Java mod loaders, neighbour rules, tinting, or other runtime
rendering behavior is complete or even detectable from the supplied files.

Pack roots are searched in the order supplied, with later roots overriding
earlier roots.  Directories must contain ``assets/``; ZIP and JAR files may be
passed directly. No files are extracted, code is run, or network is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256, blake2b
from io import BytesIO
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable, Mapping
import zipfile

from PIL import Image


_RESOURCE_ID = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_./-]+$")
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 100_000
_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_JSON_NESTING = 64
_MAX_TEXTURE_BYTES = 32 * 1024 * 1024
_MAX_TEXTURE_PIXELS = 16_000_000
_MAX_TEXTURE_SIDE = 8192
_MAX_MODEL_PARENT_DEPTH = 64
_MAX_MODEL_ELEMENTS = 4096
_MAX_RESOLUTION_CONTRIBUTIONS = 1024
_MAX_RESOLUTION_ELEMENTS = 8192
_MAX_RESOLUTION_FACES = 65_536
_MAX_CACHED_ASSET_BYTES = 128 * 1024 * 1024
_MAX_CACHED_TEXTURE_BYTES = 128 * 1024 * 1024


class ResourceError(ValueError):
    """An asset is missing, malformed, unsupported, or exceeds a safety cap."""


def _check_json_nesting(raw: bytes, resource_path: str) -> None:
    """Apply a Python-version-independent structural nesting limit.

    Count JSON arrays and objects while ignoring delimiters inside quoted
    strings. The JSON decoder remains responsible for reporting malformed
    syntax and mismatched brackets.
    """
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):  # { or [
            depth += 1
            if depth > _MAX_JSON_NESTING:
                raise ResourceError(f"invalid_json_depth_limit:{resource_path}")
        elif byte in (0x7D, 0x5D) and depth:  # } or ]
            depth -= 1


@dataclass(frozen=True)
class ResolvedFace:
    """One textured model quad, in block-local coordinates.

    ``points`` and ``uv`` have corresponding corners. UV origin is at the top
    left, and Minecraft's 16-unit texture coordinates are divided by 16. UVs
    are intentionally not clamped, preserving authored repeat coordinates.
    ``texture`` is the canonical namespaced texture resource key, without the
    ``textures/`` prefix or ``.png`` suffix.
    """

    points: tuple[tuple[float, float, float], ...]
    uv: tuple[tuple[float, float], ...]
    texture: str | None
    cullface: str | None = None
    tintindex: int | None = None


@dataclass(frozen=True)
class BlockResolution:
    """Static model resolution result for one block state and position."""

    block: str
    faces: tuple[ResolvedFace, ...]
    textures: Mapping[str, Image.Image]
    status: str
    reasons: tuple[str, ...]
    model_ids: tuple[str, ...]


@dataclass
class _Source:
    path: Path
    kind: str
    available: bool
    error: str | None = None
    archive: zipfile.ZipFile | None = None
    entries: dict[str, zipfile.ZipInfo] | None = None


@dataclass(frozen=True)
class _Model:
    model_id: str
    elements: tuple[Mapping[str, Any], ...]
    has_static_elements: bool
    textures: Mapping[str, Any]
    model_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Contribution:
    model_id: str
    rotation_x: int
    rotation_y: int
    uvlock: bool
    token: str


class ResourcePackStack:
    """Read-only ordered resource pack sources for static block models.

    ``paths`` are directory roots containing ``assets/`` or ZIP/JAR files.
    The last valid source containing a resource wins. The stack caches parsed
    JSON, resolved parent models, and decoded textures in memory only. Model
    geometry is cached independently of block position; weighted choices are
    made for each requested position using a stable, documented hash that is
    deterministic but not intended to reproduce Minecraft's random stream.
    """

    def __init__(self, paths: Iterable[str | Path] = ()) -> None:
        if isinstance(paths, (str, Path)):
            paths = (paths,)
        self.paths = tuple(Path(path).expanduser() for path in paths)
        self._sources: list[_Source] = []
        self._asset_cache: dict[tuple[int, str], bytes] = {}
        self._asset_cache_bytes = 0
        self._asset_winners: dict[str, int | None] = {}
        self._asset_reads: dict[tuple[int, str], dict[str, Any]] = {}
        self._json_cache: dict[str, Mapping[str, Any]] = {}
        self._model_cache: dict[str, _Model] = {}
        self._texture_cache: dict[str, Image.Image] = {}
        self._texture_cache_bytes = 0
        self._texture_warnings: dict[str, str] = {}
        for raw_path in self.paths:
            self._sources.append(self._open_source(raw_path))

    @staticmethod
    def _open_source(path: Path) -> _Source:
        try:
            if path.is_dir():
                if not (path / "assets").is_dir():
                    return _Source(path, "directory", False, "directory_missing_assets")
                return _Source(path.resolve(), "directory", True)
            if not path.is_file():
                return _Source(path, "archive", False, "source_not_found")
            if path.stat().st_size > _MAX_ARCHIVE_BYTES:
                return _Source(path, "archive", False, "archive_exceeds_size_limit")
            archive = zipfile.ZipFile(path, "r")
            infos = archive.infolist()
            if len(infos) > _MAX_ARCHIVE_ENTRIES:
                archive.close()
                return _Source(path, "archive", False, "archive_exceeds_entry_limit")
            entries: dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                # Avoid treating unsafe or platform-specific names as resources.
                name = info.filename
                if "\\" in name or any(part == ".." for part in name.split("/")):
                    continue
                entries[name] = info
            return _Source(path.resolve(), "archive", True, archive=archive, entries=entries)
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            return _Source(path, "archive", False, f"source_unreadable:{type(exc).__name__}")

    def close(self) -> None:
        """Close open ZIP/JAR handles. Safe to call more than once."""
        for source in self._sources:
            if source.archive is not None:
                source.archive.close()
                source.archive = None

    def __enter__(self) -> "ResourcePackStack":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _read_asset(self, resource_path: str, limit: int) -> bytes | None:
        """Read the highest-priority matching asset, with a strict byte cap."""
        relative = PurePosixPath(resource_path)
        if relative.is_absolute() or ".." in relative.parts or "\\" in resource_path:
            raise ResourceError("unsafe_resource_path")
        if resource_path in self._asset_winners:
            winner = self._asset_winners[resource_path]
            if winner is None:
                return None
            return self._asset_cache[(winner, resource_path)]

        for source_index in range(len(self._sources) - 1, -1, -1):
            source = self._sources[source_index]
            if not source.available:
                continue
            data: bytes | None = None
            if source.kind == "directory":
                candidate = source.path.joinpath(*relative.parts)
                try:
                    resolved = candidate.resolve(strict=True)
                    resolved.relative_to(source.path)
                    if not resolved.is_file():
                        continue
                    size = resolved.stat().st_size
                    if size > limit:
                        raise ResourceError(f"resource_exceeds_size_limit:{resource_path}")
                    with resolved.open("rb") as stream:
                        data = stream.read(limit + 1)
                except FileNotFoundError:
                    continue
                except ValueError as exc:
                    raise ResourceError(f"resource_outside_pack_root:{resource_path}") from exc
                except (OSError, RuntimeError) as exc:
                    raise ResourceError(f"resource_read_failed:{resource_path}") from exc
            else:
                assert source.archive is not None and source.entries is not None
                info = source.entries.get(resource_path)
                if info is None:
                    continue
                if info.file_size > limit:
                    raise ResourceError(f"resource_exceeds_size_limit:{resource_path}")
                try:
                    with source.archive.open(info, "r") as stream:
                        data = stream.read(limit + 1)
                except Exception as exc:
                    raise ResourceError(f"resource_read_failed:{resource_path}") from exc
            assert data is not None
            if len(data) > limit:
                raise ResourceError(f"resource_exceeds_size_limit:{resource_path}")
            if self._asset_cache_bytes + len(data) > _MAX_CACHED_ASSET_BYTES:
                raise ResourceError("resource_cache_limit_reached")
            cache_key = (source_index, resource_path)
            self._asset_cache[cache_key] = data
            self._asset_cache_bytes += len(data)
            self._asset_winners[resource_path] = source_index
            self._asset_reads[cache_key] = {"path": resource_path, "sha256": sha256(data).hexdigest(), "bytes": len(data)}
            return data
        self._asset_winners[resource_path] = None
        return None

    @staticmethod
    def _resource_id(value: str, *, default_namespace: str = "minecraft") -> str:
        resource = value if ":" in value else f"{default_namespace}:{value}"
        if not _RESOURCE_ID.fullmatch(resource):
            raise ResourceError(f"invalid_resource_id:{value}")
        namespace, path = resource.split(":", 1)
        if path.startswith("/") or path.endswith("/") or any(part in ("", ".", "..") for part in path.split("/")):
            raise ResourceError(f"unsafe_resource_id:{value}")
        return f"{namespace}:{path}"

    @staticmethod
    def _asset_path(resource_id: str, category: str, suffix: str) -> str:
        namespace, path = resource_id.split(":", 1)
        return f"assets/{namespace}/{category}/{path}{suffix}"

    def _read_json(self, resource_path: str) -> Mapping[str, Any]:
        cached = self._json_cache.get(resource_path)
        if cached is not None:
            return cached
        raw = self._read_asset(resource_path, _MAX_JSON_BYTES)
        if raw is None:
            raise ResourceError(f"asset_missing:{resource_path}")
        _check_json_nesting(raw, resource_path)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ResourceError(f"invalid_json:{resource_path}") from exc
        if not isinstance(value, dict):
            raise ResourceError(f"json_root_not_object:{resource_path}")
        self._json_cache[resource_path] = value
        return value

    def _read_model(self, model_id: str, trail: tuple[str, ...] = ()) -> _Model:
        model_id = self._resource_id(model_id)
        if len(trail) >= _MAX_MODEL_PARENT_DEPTH:
            raise ResourceError("model_parent_depth_limit")
        if model_id in trail:
            cycle = " -> ".join((*trail, model_id))
            raise ResourceError(f"model_parent_cycle:{cycle}")
        cached = self._model_cache.get(model_id)
        if cached is not None:
            return cached
        namespace, path = model_id.split(":", 1)
        resource_path = f"assets/{namespace}/models/{path}.json"
        raw = self._read_json(resource_path)
        if "loader" in raw:
            raise ResourceError(f"unsupported_custom_loader:{model_id}")
        parent_model: _Model | None = None
        if "parent" in raw:
            if not isinstance(raw["parent"], str):
                raise ResourceError(f"invalid_model_parent:{model_id}")
            parent_id = self._resource_id(raw["parent"])
            if parent_id.startswith("minecraft:builtin/"):
                raise ResourceError(f"unsupported_builtin_model:{parent_id}")
            parent_model = self._read_model(parent_id, (*trail, model_id))
        own_textures = raw.get("textures", {})
        if not isinstance(own_textures, dict) or any(not isinstance(key, str) for key in own_textures):
            raise ResourceError(f"invalid_model_textures:{model_id}")
        textures: dict[str, Any] = dict(parent_model.textures) if parent_model else {}
        textures.update(own_textures)
        if "elements" in raw:
            elements_value = raw["elements"]
            if isinstance(elements_value, list) and len(elements_value) > _MAX_MODEL_ELEMENTS:
                raise ResourceError(f"model_element_limit:{model_id}")
            if not isinstance(elements_value, list) or any(not isinstance(item, dict) for item in elements_value):
                raise ResourceError(f"invalid_model_elements:{model_id}")
            elements = tuple(elements_value)
            has_static_elements = True
        else:
            elements = parent_model.elements if parent_model else ()
            has_static_elements = parent_model.has_static_elements if parent_model else False
        model_ids = (parent_model.model_ids if parent_model else ()) + (model_id,)
        model = _Model(model_id, elements, has_static_elements, textures, model_ids)
        self._model_cache[model_id] = model
        return model

    @staticmethod
    def _state_matches(selector: str, properties: Mapping[str, Any]) -> bool:
        if not selector.strip():
            return True
        for clause in selector.split(","):
            if "=" not in clause:
                return False
            name, raw_values = (part.strip() for part in clause.split("=", 1))
            if not name or name not in properties:
                return False
            values = raw_values.split("|")
            if str(properties[name]) not in values:
                return False
        return True

    @staticmethod
    def _condition_matches(condition: Any, properties: Mapping[str, Any], depth: int = 0) -> bool:
        if depth > 64:
            raise ResourceError("multipart_condition_depth_limit")
        if condition is None:
            return True
        if not isinstance(condition, dict):
            raise ResourceError("invalid_multipart_condition")
        for key, value in condition.items():
            if key == "OR":
                if not isinstance(value, list):
                    raise ResourceError("invalid_multipart_or")
                if not any(ResourcePackStack._condition_matches(item, properties, depth + 1) for item in value):
                    return False
            elif key == "AND":
                if not isinstance(value, list):
                    raise ResourceError("invalid_multipart_and")
                if not all(ResourcePackStack._condition_matches(item, properties, depth + 1) for item in value):
                    return False
            else:
                if not isinstance(value, str):
                    raise ResourceError("invalid_multipart_property_condition")
                if key not in properties or str(properties[key]) not in value.split("|"):
                    return False
        return True

    @staticmethod
    def _weighted_choice(value: Any, block: str, properties: Mapping[str, Any], position: tuple[int, int, int], token: str) -> Mapping[str, Any]:
        if isinstance(value, dict):
            return value
        if not isinstance(value, list) or not value:
            raise ResourceError("invalid_or_empty_model_options")
        options: list[tuple[Mapping[str, Any], int]] = []
        total = 0
        for option in value:
            if not isinstance(option, dict):
                raise ResourceError("invalid_weighted_model_option")
            weight = option.get("weight", 1)
            if type(weight) is not int or weight <= 0 or weight > 1_000_000:
                raise ResourceError("invalid_model_weight")
            options.append((option, weight))
            total += weight
        seed_text = json.dumps(
            [block, sorted((str(key), str(value)) for key, value in properties.items()), list(position), token],
            ensure_ascii=True, separators=(",", ":"),
        ).encode("utf-8")
        # Stable across Python processes, unlike hash(). It is intentionally
        # not Minecraft's world-seed/position random stream.
        pick = int.from_bytes(blake2b(seed_text, digest_size=8, person=b"mc-model").digest(), "big") % total
        for option, weight in options:
            if pick < weight:
                return option
            pick -= weight
        raise AssertionError("weighted selection fell through")

    @staticmethod
    def _quarter_turn(value: Any, name: str) -> int:
        if type(value) is not int or value % 90 != 0:
            raise ResourceError(f"invalid_blockstate_rotation:{name}")
        return value % 360

    def resolve_block(
        self,
        name: str,
        properties: Mapping[str, Any] | None = None,
        position: tuple[int, int, int] = (0, 0, 0),
    ) -> BlockResolution:
        """Resolve blockstate variants/multipart into static textured quads.

        Weighted choices use a stable hash of block id, sorted state, position,
        and selector/part token. This gives stable previews per position but is
        not intended to match Minecraft's level-seeded random selection.
        """
        reasons: list[str] = []
        faces: list[ResolvedFace] = []
        textures: dict[str, Image.Image] = {}
        model_ids: list[str] = []
        for index, source in enumerate(self._sources):
            if not source.available:
                reasons.append(f"resource_source_unavailable:{index}:{source.error}")
        state = {str(key): str(value) for key, value in (properties or {}).items()}
        if len(position) != 3 or any(type(value) is not int for value in position):
            return BlockResolution(str(name), (), {}, "fallback", tuple((*reasons, "invalid_position")), ())
        try:
            block = self._resource_id(name)
            state_path = self._asset_path(block, "blockstates", ".json")
            definition = self._read_json(state_path)
        except ResourceError as exc:
            return BlockResolution(str(name), (), {}, "fallback", tuple((*reasons, str(exc))), ())

        contributions: list[_Contribution] = []
        has_variants = "variants" in definition
        has_multipart = "multipart" in definition
        variants = definition.get("variants")
        multipart = definition.get("multipart")
        if has_variants:
            if not isinstance(variants, dict) or any(not isinstance(key, str) for key in variants):
                return BlockResolution(block, (), {}, "fallback", tuple((*reasons, "invalid_blockstate_variants")), ())
            matching = [(selector, value) for selector, value in variants.items() if self._state_matches(selector, state)]
            if not matching:
                reasons.append("no_matching_variant")
            else:
                max_specificity = max(0 if not selector.strip() else len(selector.split(",")) for selector, _ in matching)
                best = [(selector, value) for selector, value in matching if (0 if not selector.strip() else len(selector.split(","))) == max_specificity]
                if len(best) > 1:
                    reasons.append("ambiguous_matching_variants:first_declaration_used")
                selector, value = best[0]
                try:
                    selected = self._weighted_choice(value, block, state, position, f"variant:{selector}")
                    model_id = selected.get("model")
                    if not isinstance(model_id, str):
                        raise ResourceError("variant_model_missing_or_invalid")
                    uvlock = selected.get("uvlock", False)
                    if type(uvlock) is not bool:
                        raise ResourceError("invalid_uvlock_value")
                    contributions.append(_Contribution(
                        self._resource_id(model_id),
                        self._quarter_turn(selected.get("x", 0), "x"),
                        self._quarter_turn(selected.get("y", 0), "y"),
                        uvlock,
                        f"variant:{selector}",
                    ))
                except ResourceError as exc:
                    reasons.append(str(exc))
        if has_multipart:
            if not isinstance(multipart, list) or any(not isinstance(part, dict) for part in multipart):
                return BlockResolution(block, (), {}, "fallback", tuple((*reasons, "invalid_blockstate_multipart")), ())
            matched_parts = 0
            for index, part in enumerate(multipart):
                try:
                    if not self._condition_matches(part.get("when"), state):
                        continue
                    matched_parts += 1
                    selected = self._weighted_choice(part.get("apply"), block, state, position, f"multipart:{index}")
                    model_id = selected.get("model")
                    if not isinstance(model_id, str):
                        raise ResourceError("multipart_model_missing_or_invalid")
                    uvlock = selected.get("uvlock", False)
                    if type(uvlock) is not bool:
                        raise ResourceError("invalid_uvlock_value")
                    contributions.append(_Contribution(
                        self._resource_id(model_id),
                        self._quarter_turn(selected.get("x", 0), "x"),
                        self._quarter_turn(selected.get("y", 0), "y"),
                        uvlock,
                        f"multipart:{index}",
                    ))
                except ResourceError as exc:
                    reasons.append(f"multipart_part_{index}:{exc}")
            if multipart and matched_parts == 0:
                reasons.append("no_matching_multipart_parts")
        if not has_variants and not has_multipart:
            return BlockResolution(block, (), {}, "fallback", tuple((*reasons, "blockstate_has_no_variants_or_multipart")), ())
        if has_variants and has_multipart:
            reasons.append("variants_and_multipart_both_present")
        if len(contributions) > _MAX_RESOLUTION_CONTRIBUTIONS:
            return BlockResolution(block, (), {}, "fallback", tuple((*reasons, "model_contribution_limit")), ())
        if not contributions:
            # A deliberately empty multipart list is a valid empty selection.
            if multipart == [] and not has_variants:
                unique_reasons = tuple(dict.fromkeys(reasons))
                return BlockResolution(block, (), {}, "partial" if unique_reasons else "resolved", unique_reasons, ())
            return BlockResolution(block, (), {}, "fallback", tuple(dict.fromkeys(reasons or ["no_model_selected"])), ())

        elements_seen = 0
        limits_reached = False
        resolved_contributions = 0
        for contribution in contributions:
            try:
                model = self._read_model(contribution.model_id)
                for model_id in model.model_ids:
                    if model_id not in model_ids:
                        model_ids.append(model_id)
                if not model.has_static_elements:
                    raise ResourceError("no_static_elements_runtime_geometry_unavailable")
                if contribution.uvlock:
                    reasons.append(f"uvlock_not_applied:{contribution.token}")
                valid_elements = 0
                for element_index, element in enumerate(model.elements):
                    elements_seen += 1
                    if elements_seen > _MAX_RESOLUTION_ELEMENTS or len(faces) >= _MAX_RESOLUTION_FACES:
                        reasons.append("resolved_geometry_limit_reached")
                        limits_reached = True
                        break
                    try:
                        generated, element_reasons = self._element_faces(
                            element, model.textures, contribution, textures,
                        )
                        valid_elements += 1
                    except ResourceError as exc:
                        reasons.append(f"element_resolution_failed:{contribution.model_id}[{element_index}]:{exc}")
                        continue
                    available_faces = _MAX_RESOLUTION_FACES - len(faces)
                    faces.extend(generated[:available_faces])
                    reasons.extend(f"{reason}:{model.model_id}[{element_index}]" for reason in element_reasons)
                    if len(generated) > available_faces:
                        reasons.append("resolved_geometry_limit_reached")
                        limits_reached = True
                        break
                if limits_reached:
                    break
                if not model.elements or valid_elements:
                    resolved_contributions += 1
            except ResourceError as exc:
                if contribution.model_id not in model_ids:
                    model_ids.append(contribution.model_id)
                reasons.append(f"model_resolution_failed:{contribution.model_id}:{exc}")
        unique_reasons = tuple(dict.fromkeys(reasons))
        if resolved_contributions == 0:
            status = "fallback"
        elif unique_reasons:
            status = "partial"
        else:
            status = "resolved"
        return BlockResolution(block, tuple(faces), textures, status, unique_reasons, tuple(model_ids))

    @staticmethod
    def _default_uv(face_name: str, from_xyz: tuple[float, float, float], to_xyz: tuple[float, float, float]) -> tuple[float, float, float, float]:
        x0, y0, z0 = from_xyz
        x1, y1, z1 = to_xyz
        defaults = {
            "down": (x0, 16 - z1, x1, 16 - z0),
            "up": (x0, z0, x1, z1),
            "north": (16 - x1, 16 - y1, 16 - x0, 16 - y0),
            "south": (x0, 16 - y1, x1, 16 - y0),
            "west": (z0, 16 - y1, z1, 16 - y0),
            "east": (16 - z1, 16 - y1, 16 - z0, 16 - y0),
        }
        return defaults[face_name]

    @staticmethod
    def _base_face_points(face_name: str, lo: tuple[float, float, float], hi: tuple[float, float, float]) -> tuple[tuple[float, float, float], ...]:
        x0, y0, z0 = lo
        x1, y1, z1 = hi
        corners = {
            # Each list has outward winding and follows Minecraft's face axes.
            "down": ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)),
            "up": ((x0, y1, z1), (x1, y1, z1), (x1, y1, z0), (x0, y1, z0)),
            "north": ((x0, y1, z0), (x1, y1, z0), (x1, y0, z0), (x0, y0, z0)),
            "south": ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)),
            "west": ((x0, y0, z1), (x0, y1, z1), (x0, y1, z0), (x0, y0, z0)),
            "east": ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)),
        }
        return corners[face_name]

    @staticmethod
    def _rotate_element_point(point: tuple[float, float, float], rotation: Mapping[str, Any]) -> tuple[float, float, float]:
        axis = rotation.get("axis")
        angle = rotation.get("angle", 0)
        if (axis not in {"x", "y", "z"} or type(angle) not in (int, float)
                or not math.isfinite(float(angle)) or abs(float(angle)) > 45
                or not math.isclose(float(angle) / 22.5, round(float(angle) / 22.5), abs_tol=1e-8)):
            raise ResourceError("invalid_element_rotation")
        rescale = rotation.get("rescale", False)
        if type(rescale) is not bool:
            raise ResourceError("invalid_element_rotation_rescale")
        origin_raw = rotation.get("origin", [8, 8, 8])
        if not isinstance(origin_raw, list) or len(origin_raw) != 3 or any(type(v) not in (int, float) or not math.isfinite(float(v)) for v in origin_raw):
            raise ResourceError("invalid_element_rotation_origin")
        origin = tuple(float(v) / 16 for v in origin_raw)
        v = [point[i] - origin[i] for i in range(3)]
        radians = math.radians(float(angle))
        cosine, sine = math.cos(radians), math.sin(radians)
        if rescale:
            if abs(cosine) < 1e-9:
                raise ResourceError("invalid_element_rescale_angle")
            scale = 1 / cosine
            axes = {"x": (1, 2), "y": (0, 2), "z": (0, 1)}[axis]
            for index in axes:
                v[index] *= scale
        x, y, z = v
        if axis == "x":
            rotated = (x, y * cosine - z * sine, y * sine + z * cosine)
        elif axis == "y":
            rotated = (x * cosine + z * sine, y, -x * sine + z * cosine)
        else:
            rotated = (x * cosine - y * sine, x * sine + y * cosine, z)
        return tuple(rotated[i] + origin[i] for i in range(3))

    @staticmethod
    def _rotate_blockstate_point(point: tuple[float, float, float], rotation_x: int, rotation_y: int) -> tuple[float, float, float]:
        x, y, z = point
        # Minecraft coordinates: +X east, +Y up, +Z south. JSON blockstate X/Y
        # rotations use the negative right-handed angle, applied X then Y about
        # block center. Thus x=90 takes north to down; y=90 takes north east.
        dx, dy, dz = x - 0.5, y - 0.5, z - 0.5
        if rotation_x:
            angle = math.radians(-rotation_x)
            c, s = math.cos(angle), math.sin(angle)
            dy, dz = dy * c - dz * s, dy * s + dz * c
        if rotation_y:
            angle = math.radians(-rotation_y)
            c, s = math.cos(angle), math.sin(angle)
            dx, dz = dx * c + dz * s, -dx * s + dz * c
        return (dx + 0.5, dy + 0.5, dz + 0.5)

    @staticmethod
    def _rotate_cullface(cullface: Any, rotation_x: int, rotation_y: int) -> str | None:
        vectors = {
            "down": (0.0, -1.0, 0.0), "up": (0.0, 1.0, 0.0),
            "north": (0.0, 0.0, -1.0), "south": (0.0, 0.0, 1.0),
            "west": (-1.0, 0.0, 0.0), "east": (1.0, 0.0, 0.0),
        }
        if not isinstance(cullface, str) or cullface not in vectors:
            return None
        rotated = ResourcePackStack._rotate_blockstate_point(
            tuple(vectors[cullface][i] + 0.5 for i in range(3)), rotation_x, rotation_y,
        )
        vector = tuple(round(rotated[i] - 0.5) for i in range(3))
        return next((name for name, candidate in vectors.items() if tuple(int(v) for v in candidate) == vector), None)

    def _resolve_texture(self, value: Any, texture_map: Mapping[str, Any]) -> tuple[str, Image.Image]:
        pending: set[str] = set()
        current = value
        while isinstance(current, str) and current.startswith("#"):
            key = current[1:]
            if key in pending:
                raise ResourceError(f"texture_reference_cycle:{key}")
            if len(pending) >= 256:
                raise ResourceError("texture_reference_depth_limit")
            pending.add(key)
            if key not in texture_map:
                raise ResourceError(f"texture_reference_missing:{key}")
            current = texture_map[key]
        if not isinstance(current, str):
            raise ResourceError("invalid_texture_reference")
        texture_id = self._resource_id(current)
        cached = self._texture_cache.get(texture_id)
        if cached is not None:
            return texture_id, cached
        resource_path = self._asset_path(texture_id, "textures", ".png")
        raw = self._read_asset(resource_path, _MAX_TEXTURE_BYTES)
        if raw is None:
            raise ResourceError(f"texture_missing:{texture_id}")
        try:
            image = Image.open(BytesIO(raw))
            if image.format != "PNG":
                raise ResourceError(f"invalid_texture_png:{texture_id}")
            if image.width > _MAX_TEXTURE_SIDE or image.height > _MAX_TEXTURE_SIDE or image.width * image.height > _MAX_TEXTURE_PIXELS:
                raise ResourceError(f"texture_dimensions_exceed_limit:{texture_id}")
            image.load()
            rgba = image.convert("RGBA")
        except ResourceError:
            raise
        except Exception as exc:
            raise ResourceError(f"invalid_texture_png:{texture_id}") from exc
        namespace, path = texture_id.split(":", 1)
        metadata_path = f"assets/{namespace}/textures/{path}.png.mcmeta"
        metadata_raw = self._read_asset(metadata_path, _MAX_JSON_BYTES)
        if metadata_raw is not None:
            _check_json_nesting(metadata_raw, metadata_path)
            try:
                metadata = json.loads(metadata_raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                raise ResourceError(f"invalid_texture_metadata:{texture_id}") from exc
            if not isinstance(metadata, dict):
                raise ResourceError(f"invalid_texture_metadata:{texture_id}")
            animation = metadata.get("animation")
            if animation is not None:
                if not isinstance(animation, dict):
                    raise ResourceError(f"invalid_texture_animation_metadata:{texture_id}")
                frame_width = animation.get("width", min(rgba.width, rgba.height))
                frame_height = animation.get("height", min(rgba.width, rgba.height))
                if (type(frame_width) is not int or type(frame_height) is not int or frame_width <= 0 or frame_height <= 0
                        or rgba.width % frame_width or rgba.height % frame_height):
                    raise ResourceError(f"invalid_animated_texture_frame_size:{texture_id}")
                frames = animation.get("frames")
                first_frame = 0
                if frames is not None:
                    if not isinstance(frames, list) or not frames:
                        raise ResourceError(f"invalid_animated_texture_frames:{texture_id}")
                    first = frames[0]
                    if isinstance(first, int) and type(first) is int:
                        first_frame = first
                    elif isinstance(first, dict) and type(first.get("index")) is int:
                        first_frame = first["index"]
                    else:
                        raise ResourceError(f"invalid_animated_texture_first_frame:{texture_id}")
                columns = rgba.width // frame_width
                rows = rgba.height // frame_height
                if first_frame < 0 or first_frame >= columns * rows:
                    raise ResourceError(f"animated_texture_frame_out_of_range:{texture_id}")
                left = (first_frame % columns) * frame_width
                top = (first_frame // columns) * frame_height
                rgba = rgba.crop((left, top, left + frame_width, top + frame_height))
                self._texture_warnings[texture_id] = "animated_texture_first_frame_only"
        decoded_bytes = rgba.width * rgba.height * 4
        if self._texture_cache_bytes + decoded_bytes > _MAX_CACHED_TEXTURE_BYTES:
            raise ResourceError("decoded_texture_cache_limit_reached")
        if len(self._texture_cache) >= 256:
            raise ResourceError("texture_count_limit_reached")
        self._texture_cache_bytes += decoded_bytes
        self._texture_cache[texture_id] = rgba
        return texture_id, rgba

    def _element_faces(
        self,
        element: Mapping[str, Any],
        texture_map: Mapping[str, Any],
        contribution: _Contribution,
        textures: dict[str, Image.Image],
    ) -> tuple[list[ResolvedFace], list[str]]:
        reasons: list[str] = []
        from_raw, to_raw = element.get("from"), element.get("to")
        if not isinstance(from_raw, list) or not isinstance(to_raw, list) or len(from_raw) != 3 or len(to_raw) != 3:
            raise ResourceError("element_from_to_invalid")
        if any(type(v) not in (int, float) or not math.isfinite(float(v)) for v in (*from_raw, *to_raw)):
            raise ResourceError("element_from_to_invalid")
        lo = tuple(float(v) / 16 for v in from_raw)
        hi = tuple(float(v) / 16 for v in to_raw)
        if any(lo[i] > hi[i] for i in range(3)):
            raise ResourceError("element_from_to_degenerate")
        if any(value < -1.0 or value > 2.0 for value in (*lo, *hi)):
            raise ResourceError("element_coordinate_out_of_bounds")
        element_rotation = element.get("rotation")
        if element_rotation is not None and not isinstance(element_rotation, dict):
            raise ResourceError("element_rotation_invalid")
        element_faces = element.get("faces")
        if not isinstance(element_faces, dict):
            raise ResourceError("element_faces_invalid")
        if "shade" in element and type(element["shade"]) is not bool:
            reasons.append("invalid_element_shade")
        elif element.get("shade") is False:
            reasons.append("element_shade_flag_not_applied")
        generated: list[ResolvedFace] = []
        for face_name, face in element_faces.items():
            if face_name not in {"down", "up", "north", "south", "west", "east"} or not isinstance(face, dict):
                reasons.append("invalid_element_face")
                continue
            plane_axes = {
                "down": (0, 2), "up": (0, 2), "north": (0, 1),
                "south": (0, 1), "west": (1, 2), "east": (1, 2),
            }[face_name]
            if any(math.isclose(lo[axis], hi[axis]) for axis in plane_axes):
                # Crosses, panes and other flat elements intentionally have
                # some degenerate cube faces. Keep only their real planes.
                continue
            face_texture = face.get("texture")
            if not isinstance(face_texture, str):
                reasons.append(f"face_texture_missing:{face_name}")
                texture_id = None
            else:
                try:
                    texture_id, image = self._resolve_texture(face_texture, texture_map)
                    textures[texture_id] = image
                    if texture_id in self._texture_warnings:
                        reasons.append(f"{self._texture_warnings[texture_id]}:{texture_id}")
                except ResourceError as exc:
                    texture_id = None
                    reasons.append(f"{exc}:{face_name}")
            uv = face.get("uv")
            if uv is None:
                uv_rect = self._default_uv(face_name, tuple(v * 16 for v in lo), tuple(v * 16 for v in hi))
            elif isinstance(uv, list) and len(uv) == 4 and all(type(v) in (int, float) and math.isfinite(float(v)) for v in uv):
                uv_rect = tuple(float(v) for v in uv)
            else:
                reasons.append(f"invalid_face_uv:{face_name}")
                uv_rect = self._default_uv(face_name, tuple(v * 16 for v in lo), tuple(v * 16 for v in hi))
            face_rotation = face.get("rotation", 0)
            if type(face_rotation) is not int or face_rotation not in (0, 90, 180, 270):
                reasons.append(f"invalid_face_rotation:{face_name}")
                face_rotation = 0
            points = self._base_face_points(face_name, lo, hi)
            uv_points: list[tuple[float, float]] = []
            for point in points:
                x, y, z = point
                if face_name == "down":
                    s = (x - lo[0]) / (hi[0] - lo[0]); t = (hi[2] - z) / (hi[2] - lo[2])
                elif face_name == "up":
                    s = (x - lo[0]) / (hi[0] - lo[0]); t = (z - lo[2]) / (hi[2] - lo[2])
                elif face_name == "north":
                    s = (hi[0] - x) / (hi[0] - lo[0]); t = (hi[1] - y) / (hi[1] - lo[1])
                elif face_name == "south":
                    s = (x - lo[0]) / (hi[0] - lo[0]); t = (hi[1] - y) / (hi[1] - lo[1])
                elif face_name == "west":
                    s = (z - lo[2]) / (hi[2] - lo[2]); t = (hi[1] - y) / (hi[1] - lo[1])
                else:  # east
                    s = (hi[2] - z) / (hi[2] - lo[2]); t = (hi[1] - y) / (hi[1] - lo[1])
                # Minecraft's face rotation turns texture content clockwise.
                for _ in range(face_rotation // 90):
                    s, t = t, 1 - s
                # Keep descending authored ranges: reversed UV endpoints are
                # a valid way to mirror a face texture.
                raw_uv = (uv_rect[0] + s * (uv_rect[2] - uv_rect[0]), uv_rect[1] + t * (uv_rect[3] - uv_rect[1]))
                uv_points.append((raw_uv[0] / 16, raw_uv[1] / 16))
            transformed_points = []
            for point in points:
                if element_rotation:
                    point = self._rotate_element_point(point, element_rotation)
                point = self._rotate_blockstate_point(point, contribution.rotation_x, contribution.rotation_y)
                transformed_points.append(point)
            cullface = None if element_rotation else self._rotate_cullface(
                face.get("cullface"), contribution.rotation_x, contribution.rotation_y,
            )
            tintindex = face.get("tintindex")
            if tintindex is not None and type(tintindex) is not int:
                reasons.append(f"invalid_tintindex:{face_name}")
                tintindex = None
            elif tintindex is not None and tintindex >= 0:
                reasons.append(f"tintindex_not_applied:{tintindex}:{face_name}")
            generated.append(ResolvedFace(tuple(transformed_points), tuple(uv_points), texture_id, cullface, tintindex))
        return generated, reasons

    def describe(self) -> dict[str, Any]:
        """Return JSON-ready source priority and content fingerprints."""
        sources: list[dict[str, Any]] = []
        for index, source in enumerate(self._sources):
            assets = [record for (source_index, _path), record in self._asset_reads.items() if source_index == index]
            assets.sort(key=lambda item: item["path"])
            fingerprint = None
            if assets:
                digest = sha256()
                for record in assets:
                    digest.update(record["path"].encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(record["sha256"].encode("ascii"))
                    digest.update(b"\n")
                fingerprint = digest.hexdigest()
            sources.append({
                "priority": index,
                "override_priority": index,
                "path": str(source.path),
                "kind": source.kind,
                "available": source.available,
                "error": source.error,
                "loaded_asset_count": len(assets),
                "loaded_assets": assets,
                "content_fingerprint": fingerprint,
            })
        return {
            "resolution": "supported_static_json_subset",
            "sources": sources,
            "precedence": "later_sources_override_earlier_sources",
            "fingerprint_scope": "successfully_read_assets_only; no full-pack scan is performed",
            "weighted_choice": "stable hash of block, state, position, and selector; deterministic but not Minecraft-game-identical",
            "uv_convention": "top-left origin; Minecraft 16-unit UVs divided by 16; authored repeat coordinates are not clamped",
            "coordinates": "block-local +X east, +Y up, +Z south; blockstate positive X/Y use negative right-handed angles, x=90 takes north to down and y=90 takes north east",
            "limitations": [
                "resolved means supported static JSON was resolved, not complete Minecraft runtime rendering",
                "Java model loaders and runtime-generated models may be absent or impossible to detect from JSON",
                "uvlock is reported as partial and is not applied",
                "animated textures use their first metadata-selected frame and are reported partial",
                "models without static elements fall back so runtime geometry such as water/lava remains visible as a proxy",
                "display transforms, biome tint, connected textures, neighbour-dependent model rules, animation playback, and block entities are not resolved",
            ],
        }
