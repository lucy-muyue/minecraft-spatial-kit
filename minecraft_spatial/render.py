"""Offline Scene v1 renderer with proxy and supported static resource models.

Without resource packs, meshes remain useful spatial proxies. Optional pack
support resolves a bounded subset of blockstate/model JSON and textures; it does
not reproduce Minecraft's full runtime renderer or its world-dependent effects.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import struct
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFont
import trimesh


_ALLOWED_VIEWS = {"iso", "top", "north", "east", "south", "west", "bottom"}
_VIEW_SIZE = (1040, 780)
_SS = 2
_MAX_RESOURCE_PACK_ROOTS = 64
_MAX_RESOURCE_FACES = 100_000
_MAX_RESOURCE_TEXTURE_PIXELS = 16_777_216
_MAX_RESOURCE_TOTAL_TEXTURE_PIXELS = 32_000_000
_MAX_RESOURCE_LOCAL_COORDINATE = 64.0
_MAX_RESOURCE_UV_COORDINATE = 1024.0


@dataclass(frozen=True)
class _Box:
    """One axis-aligned solid in global Minecraft block coordinates."""

    lo: tuple[float, float, float]
    hi: tuple[float, float, float]
    color: tuple[int, int, int]
    source: str = "block"


@dataclass(frozen=True)
class _Face:
    points: tuple[tuple[float, float, float], ...]
    color: tuple[int, int, int]
    source: str = "block"
    uv: tuple[tuple[float, float], ...] | None = None
    texture: Image.Image | None = None
    texture_key: str | None = None


def _stable_color(name: str) -> tuple[int, int, int]:
    """Return a stable, readable color without depending on Python's hash seed."""
    short = name.split(":", 1)[-1].lower()
    direct = {
        "white": "#E8E6DE", "orange": "#D88642", "magenta": "#B55CA5",
        "light_blue": "#75A9C5", "yellow": "#D3B94D", "lime": "#87A84B",
        "pink": "#D98D9D", "gray": "#73777B", "light_gray": "#A6A7A3",
        "cyan": "#4E9C9B", "purple": "#80599C", "blue": "#526FB2",
        "brown": "#82603E", "green": "#628342", "red": "#B8483C",
        "black": "#34373A", "white_concrete": "#DAD9D1",
        "oak_planks": "#A77B4D", "oak_stairs": "#A77B4D", "oak_slab": "#A77B4D",
        "spruce_planks": "#6E533A", "spruce_stairs": "#6E533A", "spruce_slab": "#6E533A",
        "birch_planks": "#C9B67C", "birch_stairs": "#C9B67C", "birch_slab": "#C9B67C",
        "dark_oak_planks": "#493729", "dark_oak_stairs": "#493729",
        "stone": "#85898B", "cobblestone": "#777B7C", "mossy_cobblestone": "#707D67",
        "deepslate": "#50575B", "bricks": "#A65F50", "glass": "#A7CFD2",
        "glass_pane": "#A7CFD2", "dirt": "#866044", "grass_block": "#78934D",
        "sand": "#D1C18A", "sandstone": "#C3AE79", "snow": "#E7ECEB",
        "water": "#568EB4", "lava": "#DE7136", "leaves": "#66884C",
        "oak_leaves": "#6D9150", "spruce_leaves": "#55754A",
    }
    if short in direct:
        return ImageColor.getrgb(direct[short])

    for token, hex_color in (
        ("white_concrete", "#DAD9D1"), ("orange_concrete", "#D88642"),
        ("red_concrete", "#B8483C"), ("blue_concrete", "#526FB2"),
        ("planks", "#9E744A"), ("log", "#80613D"), ("wood", "#8A6844"),
        ("leaves", "#6D8B50"), ("wool", "#D8D2C5"), ("terracotta", "#A66B55"),
        ("stone", "#85898B"), ("deepslate", "#50575B"), ("copper", "#B57B59"),
        ("iron", "#B7BAB8"), ("gold", "#D2B448"), ("diamond", "#54B9AA"),
        ("emerald", "#3E9B62"), ("netherite", "#51413F"), ("brick", "#A65F50"),
        ("sand", "#D1C18A"), ("dirt", "#866044"), ("grass", "#78934D"),
        ("glass", "#A7CFD2"), ("water", "#568EB4"), ("lava", "#DE7136"),
    ):
        if token in short:
            return ImageColor.getrgb(hex_color)

    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=3, person=b"mc-color").digest()
    # Keep the value and saturation in the middle range so unknown blocks remain legible.
    hue = int.from_bytes(digest, "big") / float(1 << 24)
    import colorsys

    r, g, b = colorsys.hsv_to_rgb(hue, 0.30 + digest[0] / 255 * 0.22, 0.62 + digest[1] / 255 * 0.18)
    return round(r * 255), round(g * 255), round(b * 255)


def _property_int(properties: Mapping[str, Any], key: str, default: int) -> int:
    try:
        return int(properties.get(key, default))
    except (TypeError, ValueError):
        return default


def _block_local_boxes(name: str, props: Mapping[str, Any]) -> tuple[list[tuple[float, ...]], str, str | None]:
    """Return local [x0,y0,z0,x1,y1,z1] boxes, geometry class, fallback reason."""
    short = name.split(":", 1)[-1].lower()
    unit = [(0.0, 0.0, 0.0, 1.0, 1.0, 1.0)]
    if short.endswith("_slab"):
        slab_type = str(props.get("type", "")).lower()
        if slab_type == "bottom":
            return [(0.0, 0.0, 0.0, 1.0, 0.5, 1.0)], "slab", None
        if slab_type == "top":
            return [(0.0, 0.5, 0.0, 1.0, 1.0, 1.0)], "slab", None
        if slab_type == "double":
            return unit, "slab", None
        return unit, "cube_proxy", "slab_type_missing_or_unknown"

    if short.endswith("_stairs"):
        facing = str(props.get("facing", "")).lower()
        half = str(props.get("half", "bottom")).lower()
        shape = str(props.get("shape", "straight")).lower()
        if facing not in {"north", "east", "south", "west"}:
            return unit, "cube_proxy", "stair_facing_missing_or_unknown"
        if half not in {"bottom", "top"}:
            return unit, "cube_proxy", "stair_half_unknown"
        if shape != "straight":
            return unit, "cube_proxy", "stair_shape_not_straight"

        # A straight stair is represented as two half-height boxes. The raised
        # tread lies toward `facing`; inner/outer corner states use the cube proxy.
        if facing == "north":
            z0, z1 = 0.0, 0.5
            x0, x1 = 0.0, 1.0
        elif facing == "south":
            z0, z1 = 0.5, 1.0
            x0, x1 = 0.0, 1.0
        elif facing == "east":
            x0, x1 = 0.5, 1.0
            z0, z1 = 0.0, 1.0
        else:  # west
            x0, x1 = 0.0, 0.5
            z0, z1 = 0.0, 1.0
        if half == "bottom":
            return [
                (0.0, 0.0, 0.0, 1.0, 0.5, 1.0),
                (x0, 0.5, z0, x1, 1.0, z1),
            ], "straight_stair", None
        return [
            (0.0, 0.5, 0.0, 1.0, 1.0, 1.0),
            (x0, 0.0, z0, x1, 0.5, z1),
        ], "straight_stair", None

    if short in {"air", "cave_air", "void_air"}:
        return [], "air", None
    # The Scene palette does not carry resource-pack geometry, so all remaining
    # states are represented as unit cubes, including doors/plants/fluids/etc.
    return unit, "cube_proxy", "resource_pack_model_unavailable"


def _shade(rgb: tuple[int, int, int], axis: int, sign: int) -> tuple[int, int, int]:
    factor = {(0, 1): 0.86, (0, -1): 0.80, (1, 1): 1.10, (1, -1): 0.62,
              (2, 1): 0.76, (2, -1): 0.91}[(axis, sign)]
    return tuple(max(0, min(255, int(channel * factor))) for channel in rgb)


def _box_vertices(box: _Box, origin: Sequence[float]) -> list[tuple[float, float, float]]:
    x0, y0, z0 = box.lo
    x1, y1, z1 = box.hi
    return [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]


def _face_corners(box: _Box, axis: int, sign: int) -> tuple[list[tuple[float, float, float]], float, int, int]:
    """Quad corners with outward winding; returns projected axes u/v too."""
    lo, hi = box.lo, box.hi
    if axis == 0:
        plane = hi[0] if sign > 0 else lo[0]
        uaxis, vaxis = 1, 2  # Y cross Z = +X
    elif axis == 1:
        plane = hi[1] if sign > 0 else lo[1]
        uaxis, vaxis = 2, 0  # Z cross X = +Y
    else:
        plane = hi[2] if sign > 0 else lo[2]
        uaxis, vaxis = 0, 1  # X cross Y = +Z
    u0, u1 = lo[uaxis], hi[uaxis]
    v0, v1 = lo[vaxis], hi[vaxis]
    points = []
    for u, v in ((u0, v0), (u1, v0), (u1, v1), (u0, v1)):
        p = [0.0, 0.0, 0.0]
        p[axis] = plane
        p[uaxis] = u
        p[vaxis] = v
        points.append(tuple(p))
    if sign < 0:
        points.reverse()
    return points, plane, uaxis, vaxis


def _face_grid_key(box: _Box, axis: int, plane: float, side: int) -> tuple[int, float, int, int, int]:
    other = [i for i in range(3) if i != axis]
    # All block geometry is aligned to full/half-block boundaries and spans no
    # more than one block, so its center identifies a compact spatial bin.
    cu = math.floor((box.lo[other[0]] + box.hi[other[0]]) / 2)
    cv = math.floor((box.lo[other[1]] + box.hi[other[1]]) / 2)
    return axis, plane, side, cu, cv


def _surface_faces(boxes: Sequence[_Box], *, cull_internal: bool = True) -> list[_Face]:
    """Make exposed surface quads, removing full and partial internal faces."""
    face_index: dict[tuple[int, float, int, int, int], list[int]] = defaultdict(list)
    if cull_internal:
        for i, box in enumerate(boxes):
            for axis in range(3):
                for side in (-1, 1):
                    plane = box.lo[axis] if side < 0 else box.hi[axis]
                    key = _face_grid_key(box, axis, plane, side)
                    face_index[key].append(i)

    faces: list[_Face] = []
    for i, box in enumerate(boxes):
        for axis in range(3):
            for sign in (-1, 1):
                corners, plane, uaxis, vaxis = _face_corners(box, axis, sign)
                u0, u1 = box.lo[uaxis], box.hi[uaxis]
                v0, v1 = box.lo[vaxis], box.hi[vaxis]
                candidates: list[_Box] = []
                if cull_internal:
                    # An outward (+) face can only be hidden by a box whose -
                    # face touches it (and vice versa).
                    side = -sign
                    lookup = _face_grid_key(box, axis, plane, side)
                    for j in face_index.get(lookup, ()):
                        if j == i:
                            continue
                        other = boxes[j]
                        other_plane = other.lo[axis] if side < 0 else other.hi[axis]
                        if abs(other_plane - plane) > 1e-9:
                            continue
                        if other.lo[uaxis] < u1 and other.hi[uaxis] > u0 and other.lo[vaxis] < v1 and other.hi[vaxis] > v0:
                            candidates.append(other)

                ucuts = {u0, u1}
                vcuts = {v0, v1}
                for other in candidates:
                    ucuts.update((max(u0, other.lo[uaxis]), min(u1, other.hi[uaxis])))
                    vcuts.update((max(v0, other.lo[vaxis]), min(v1, other.hi[vaxis])))
                us = sorted(ucuts)
                vs = sorted(vcuts)
                for ua, ub in zip(us, us[1:]):
                    for va, vb in zip(vs, vs[1:]):
                        if ub - ua < 1e-9 or vb - va < 1e-9:
                            continue
                        um, vm = (ua + ub) / 2, (va + vb) / 2
                        if any(o.lo[uaxis] <= um <= o.hi[uaxis] and o.lo[vaxis] <= vm <= o.hi[vaxis] for o in candidates):
                            continue
                        p = []
                        for u, v in ((ua, va), (ub, va), (ub, vb), (ua, vb)):
                            xyz = [0.0, 0.0, 0.0]
                            xyz[axis] = plane
                            xyz[uaxis] = u
                            xyz[vaxis] = v
                            p.append(tuple(xyz))
                        # Axis choices above give positive normal before this flip.
                        if sign < 0:
                            p.reverse()
                        faces.append(_Face(tuple(p), _shade(box.color, axis, sign), box.source))
    return faces


def _unknown_marker_boxes(scene: Mapping[str, Any], bounds_min: Sequence[int], bounds_max_exclusive: Sequence[int]) -> list[_Box]:
    boxes: list[_Box] = []
    y0, y1 = float(bounds_min[1]), float(bounds_max_exclusive[1])
    for chunk in scene.get("unknown_chunks", ()):
        cx, cz = int(chunk["x"]), int(chunk["z"])
        x0, x1 = max(float(bounds_min[0]), cx * 16.0), min(float(bounds_max_exclusive[0]), (cx + 1) * 16.0)
        z0, z1 = max(float(bounds_min[2]), cz * 16.0), min(float(bounds_max_exclusive[2]), (cz + 1) * 16.0)
        if x0 >= x1 or z0 >= z1 or y0 >= y1:
            continue
        thickness = max(0.035, min(0.11, min(x1 - x0, z1 - z0) * 0.012))
        orange = (244, 135, 44)
        # Chunk column frame. Its opening and orange color mean unknown data,
        # never known air; the PNGs add a hatch and legend as well.
        for x in (x0, x1 - thickness):
            for z in (z0, z1 - thickness):
                boxes.append(_Box((x, y0, z), (x + thickness, y1, z + thickness), orange, "unknown"))
        for y in (y0, y1 - thickness):
            for x in (x0, x1 - thickness):
                boxes.append(_Box((x, y, z0), (x + thickness, y + thickness, z1), orange, "unknown"))
            for z in (z0, z1 - thickness):
                boxes.append(_Box((x0, y, z), (x1, y + thickness, z + thickness), orange, "unknown"))
    return boxes


def _mesh_from_faces(faces: Sequence[_Face], world_origin: Sequence[int]) -> trimesh.Trimesh | None:
    if not faces:
        return None
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []
    colors: list[tuple[int, int, int, int]] = []
    origin = np.asarray(world_origin, dtype=np.float64)
    for face in faces:
        start = len(vertices)
        vertices.extend(tuple(np.asarray(point) - origin) for point in face.points)
        triangles.extend(((start, start + 1, start + 2), (start, start + 2, start + 3)))
        color = (*face.color, 255)
        colors.extend((color, color, color, color))
    mesh = trimesh.Trimesh(vertices=np.asarray(vertices, dtype=np.float64), faces=np.asarray(triangles, dtype=np.int64), process=False)
    mesh.visual.vertex_colors = np.asarray(colors, dtype=np.uint8)
    return mesh


def _empty_glb() -> bytes:
    """Build a minimal valid glTF 2.0 binary container with an empty scene."""
    document = {
        "asset": {"version": "2.0", "generator": "minecraft-spatial-kit"},
        "scene": 0,
        "scenes": [{"nodes": []}],
        "nodes": [],
    }
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    payload += b" " * ((-len(payload)) % 4)
    total_length = 12 + 8 + len(payload)
    return struct.pack("<4sII", b"glTF", 2, total_length) + struct.pack("<I4s", len(payload), b"JSON") + payload


def _view_basis(view: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
    if view == "top":
        return np.array([1., 0., 0.]), np.array([0., 0., -1.]), np.array([0., 1., 0.]), "north is up (-Z)"
    if view == "bottom":
        return np.array([1., 0., 0.]), np.array([0., 0., 1.]), np.array([0., -1., 0.]), "viewed upward; south is up (+Z)"
    if view == "north":
        return np.array([-1., 0., 0.]), np.array([0., 1., 0.]), np.array([0., 0., -1.]), "camera at north (-Z), west is right (-X)"
    if view == "south":
        return np.array([1., 0., 0.]), np.array([0., 1., 0.]), np.array([0., 0., 1.]), "camera at south (+Z), east is right (+X)"
    if view == "east":
        return np.array([0., 0., -1.]), np.array([0., 1., 0.]), np.array([1., 0., 0.]), "camera at east (+X), north is right (-Z)"
    if view == "west":
        return np.array([0., 0., 1.]), np.array([0., 1., 0.]), np.array([-1., 0., 0.]), "camera at west (-X), south is right (+Z)"
    camera = np.array([1., 0.86, -1.], dtype=np.float64)
    camera /= np.linalg.norm(camera)
    right = np.cross(np.array([0., 1., 0.]), camera)
    right /= np.linalg.norm(right)
    up = np.cross(camera, right)
    up /= np.linalg.norm(up)
    return right, up, camera, "camera from northeast (+X, -Z)"


def _load_font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial.ttf"):
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def _world_corners(bounds_min: Sequence[int], bounds_max_exclusive: Sequence[int]) -> list[tuple[float, float, float]]:
    return [(float(x), float(y), float(z)) for x in (bounds_min[0], bounds_max_exclusive[0])
            for y in (bounds_min[1], bounds_max_exclusive[1]) for z in (bounds_min[2], bounds_max_exclusive[2])]


def _texture_indices(uv: np.ndarray, width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    """Map normalized top-left-origin UVs to nearest repeating texels."""
    def wrap(values: np.ndarray) -> np.ndarray:
        nearest = np.rint(values)
        snapped = np.where(np.abs(values - nearest) <= 1e-10, nearest, values)
        return np.mod(snapped, 1.0)

    u = wrap(uv[..., 0])
    v = wrap(uv[..., 1])
    x = np.minimum((u * width).astype(np.int64), width - 1)
    y = np.minimum((v * height).astype(np.int64), height - 1)
    return x, y


def _render_view(
    path: Path,
    view: str,
    faces: Sequence[_Face],
    unknown_chunks: Sequence[Mapping[str, Any]],
    bounds_min: Sequence[int],
    bounds_max: Sequence[int],
    *,
    no_known_solids: bool = False,
    no_solids_message: str | None = None,
) -> None:
    width, height = _VIEW_SIZE
    scale = _SS
    image = Image.new("RGB", (width * scale, height * scale), (242, 245, 247))
    draw = ImageDraw.Draw(image)
    right, up, camera, orientation = _view_basis(view)

    def project(point: Sequence[float]) -> tuple[float, float, float]:
        p = np.asarray(point, dtype=np.float64)
        return float(np.dot(p, right)), float(np.dot(p, up)), float(np.dot(p, camera))

    all_points = [p for face in faces for p in face.points]
    if not all_points:
        all_points = _world_corners(bounds_min, [v + 1 for v in bounds_max])
    projected = [project(p) for p in all_points]
    min_u, max_u = min(p[0] for p in projected), max(p[0] for p in projected)
    min_v, max_v = min(p[1] for p in projected), max(p[1] for p in projected)
    span_u, span_v = max(max_u - min_u, 1.0), max(max_v - min_v, 1.0)
    margin_left, margin_right, margin_top, margin_bottom = 62, 62, 108, 70
    content_w = (width - margin_left - margin_right) * scale
    content_h = (height - margin_top - margin_bottom) * scale
    fit = min(content_w / span_u, content_h / span_v)
    offset_x = (width * scale - span_u * fit) / 2 - min_u * fit
    offset_y = margin_top * scale + (content_h - span_v * fit) / 2 + max_v * fit

    def screen_float(point: Sequence[float]) -> tuple[float, float]:
        u, v, _ = project(point)
        return offset_x + u * fit, offset_y - v * fit

    def screen(point: Sequence[float]) -> tuple[int, int]:
        x, y = screen_float(point)
        return round(x), round(y)

    # Faint ground grid gives the isometric view scale without pretending that
    # missing chunks are a rendered terrain surface.
    if view == "iso":
        low_x, _, low_z = bounds_min
        high_x, _, high_z = (v + 1 for v in bounds_max)
        step = 1 if max(high_x - low_x, high_z - low_z) <= 24 else (4 if max(high_x - low_x, high_z - low_z) <= 96 else 16)
        floor_y = float(bounds_min[1]) - 0.025
        grid_color = (224, 229, 232)
        for x in range((low_x // step) * step, high_x + step, step):
            a, b = screen((x, floor_y, low_z)), screen((x, floor_y, high_z))
            draw.line((a[0], a[1], b[0], b[1]), fill=grid_color, width=1 * scale)
        for z in range((low_z // step) * step, high_z + step, step):
            a, b = screen((low_x, floor_y, z)), screen((high_x, floor_y, z))
            draw.line((a[0], a[1], b[0], b[1]), fill=grid_color, width=1 * scale)

    # A CPU z-buffer resolves intersecting/overlapping surfaces without relying
    # on painter ordering. Rasterization is vectorized over each triangle's
    # small screen-space bounding box; no OpenGL, EGL, or GPU is used.
    pixels = np.asarray(image, dtype=np.uint8).copy()
    depth_buffer = np.full((height * scale, width * scale), -np.inf, dtype=np.float32)
    image_h, image_w = depth_buffer.shape
    texture_cache: dict[int, np.ndarray] = {}
    for face in faces:
        pts3 = np.asarray(face.points, dtype=np.float64)
        normal = np.cross(pts3[1] - pts3[0], pts3[2] - pts3[0])
        if float(np.dot(normal, camera)) <= 1e-10:
            continue  # opaque backfaces cannot be visible from this camera
        projected_face = [screen_float(p) for p in face.points]
        depths = np.asarray([project(p)[2] for p in face.points], dtype=np.float64)
        fill = np.asarray(face.color, dtype=np.uint8)
        face_uv = np.asarray(face.uv, dtype=np.float64) if face.uv is not None and face.texture is not None else None
        texture_rgba: np.ndarray | None = None
        if face.texture is not None and face_uv is not None:
            texture_id = id(face.texture)
            texture_rgba = texture_cache.get(texture_id)
            if texture_rgba is None:
                texture_rgba = np.asarray(face.texture.convert("RGBA"), dtype=np.uint8)
                texture_cache[texture_id] = texture_rgba
        for i0, i1, i2 in ((0, 1, 2), (0, 2, 3)):
            tri = np.asarray([projected_face[i0], projected_face[i1], projected_face[i2]], dtype=np.float64)
            tri_z = np.asarray([depths[i0], depths[i1], depths[i2]], dtype=np.float64)
            min_x = max(0, int(math.floor(float(np.min(tri[:, 0])))))
            max_x = min(image_w - 1, int(math.ceil(float(np.max(tri[:, 0])))))
            min_y = max(0, int(math.floor(float(np.min(tri[:, 1])))))
            max_y = min(image_h - 1, int(math.ceil(float(np.max(tri[:, 1])))))
            if min_x > max_x or min_y > max_y:
                continue
            ax, ay = tri[0]
            bx, by = tri[1]
            cx, cy = tri[2]
            den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if abs(den) < 1e-12:
                continue
            xx, yy = np.meshgrid(np.arange(min_x, max_x + 1, dtype=np.float64) + 0.5,
                                 np.arange(min_y, max_y + 1, dtype=np.float64) + 0.5)
            w0 = ((by - cy) * (xx - cx) + (cx - bx) * (yy - cy)) / den
            w1 = ((cy - ay) * (xx - cx) + (ax - cx) * (yy - cy)) / den
            w2 = 1.0 - w0 - w1
            inside = (w0 >= -1e-8) & (w1 >= -1e-8) & (w2 >= -1e-8)
            z = w0 * tri_z[0] + w1 * tri_z[1] + w2 * tri_z[2]
            zview = depth_buffer[min_y:max_y + 1, min_x:max_x + 1]
            update = inside & (z > zview)
            if np.any(update):
                tile = pixels[min_y:max_y + 1, min_x:max_x + 1]
                if texture_rgba is None or face_uv is None:
                    zview[update] = z[update].astype(np.float32)
                    tile[update] = fill
                else:
                    uv_tri = face_uv[[i0, i1, i2]]
                    sample_uv = (w0[..., None] * uv_tri[0] + w1[..., None] * uv_tri[1]
                                 + w2[..., None] * uv_tri[2])
                    tx, ty = _texture_indices(sample_uv, texture_rgba.shape[1], texture_rgba.shape[0])
                    sampled = texture_rgba[ty, tx]
                    # Resource-pack cutout texels do not claim depth. Partial
                    # alpha is treated as a thresholded cutout in these PNGs.
                    visible_texel = sampled[:, :, 3] >= 128
                    update &= visible_texel
                    zview[update] = z[update].astype(np.float32)
                    if np.any(update):
                        rgb = sampled[:, :, :3]
                        if np.any(fill != 255):
                            rgb = ((rgb.astype(np.uint16) * fill.astype(np.uint16)) // 255).astype(np.uint8)
                        tile[update] = rgb[update]
        line_color = (72, 79, 82) if face.source in {"block", "resource"} else (199, 84, 20)
        # Keep a supersampled edge only where its interpolated depth matches
        # the visible surface; hidden seams therefore cannot leak through walls.
        for edge in range(4):
            p0, p1 = projected_face[edge], projected_face[(edge + 1) % 4]
            z0, z1 = depths[edge], depths[(edge + 1) % 4]
            steps = max(1, int(math.ceil(max(abs(p1[0] - p0[0]), abs(p1[1] - p0[1])))))
            t = np.linspace(0.0, 1.0, steps + 1)
            ex = np.rint(p0[0] + (p1[0] - p0[0]) * t).astype(int)
            ey = np.rint(p0[1] + (p1[1] - p0[1]) * t).astype(int)
            ez = z0 + (z1 - z0) * t
            valid = (ex >= 0) & (ex < image_w) & (ey >= 0) & (ey < image_h)
            if texture_rgba is not None and face_uv is not None:
                uv0, uv1 = face_uv[edge], face_uv[(edge + 1) % 4]
                edge_uv = uv0[None, :] + (uv1 - uv0)[None, :] * t[:, None]
                tx, ty = _texture_indices(edge_uv, texture_rgba.shape[1], texture_rgba.shape[0])
                valid &= texture_rgba[ty, tx, 3] >= 128
            valid_indices = np.flatnonzero(valid)
            if len(valid_indices):
                visible = np.abs(depth_buffer[ey[valid_indices], ex[valid_indices]] - ez[valid_indices]) < 0.01
                selected = valid_indices[visible]
                pixels[ey[selected], ex[selected]] = line_color
    image = Image.fromarray(pixels, mode="RGB")
    draw = ImageDraw.Draw(image)

    # A plan-view hatch makes every unknown chunk unmistakable even where its
    # column-frame edges are partly occluded by known blocks.
    if view == "top":
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        mask = Image.new("L", image.size, 0)
        mdraw = ImageDraw.Draw(mask)
        for chunk in unknown_chunks:
            cx, cz = int(chunk["x"]), int(chunk["z"])
            x0, x1 = max(bounds_min[0], cx * 16), min(bounds_max[0] + 1, (cx + 1) * 16)
            z0, z1 = max(bounds_min[2], cz * 16), min(bounds_max[2] + 1, (cz + 1) * 16)
            if x0 >= x1 or z0 >= z1:
                continue
            poly = [screen(p) for p in ((x0, bounds_max[1] + 1, z0), (x1, bounds_max[1] + 1, z0),
                                         (x1, bounds_max[1] + 1, z1), (x0, bounds_max[1] + 1, z1))]
            mdraw.polygon(poly, fill=255)
            odraw.polygon(poly, fill=(245, 137, 49, 58), outline=(216, 101, 29, 230), width=2 * scale)
            bx = [p[0] for p in poly]; by = [p[1] for p in poly]
            left, top, right_px, bottom = min(bx), min(by), max(bx), max(by)
            pitch = max(8 * scale, round(12 * scale))
            for d in range(left - (bottom - top), right_px + (bottom - top), pitch):
                odraw.line((d, bottom, d + (bottom - top), top), fill=(224, 107, 36, 195), width=1 * scale)
        hatch = Image.new("RGBA", image.size, (0, 0, 0, 0))
        hatch.paste(overlay, (0, 0), mask)
        image = Image.alpha_composite(image.convert("RGBA"), hatch).convert("RGB")
        draw = ImageDraw.Draw(image)

    # Header/footer are part of the image contract for agents inspecting PNGs.
    header_font = _load_font(20 * scale)
    body_font = _load_font(12 * scale)
    title = f"{view.upper()} VIEW"
    draw.text((32 * scale, 20 * scale), title, fill=(33, 45, 53), font=header_font)
    draw.text((32 * scale, 51 * scale), orientation, fill=(79, 91, 98), font=body_font)
    if no_known_solids:
        draw.text((32 * scale, 74 * scale), no_solids_message or "No indexed non-air blocks in this bounded Scene",
                  fill=(139, 87, 43), font=body_font)
    extents = f"X [{bounds_min[0]}..{bounds_max[0]}]  Y [{bounds_min[1]}..{bounds_max[1]}]  Z [{bounds_min[2]}..{bounds_max[2]}]  (inclusive)"
    draw.text((32 * scale, (height - 30) * scale), extents, fill=(56, 66, 73), font=body_font)
    if unknown_chunks:
        legend = "Orange frame / hatch = unindexed chunk (unknown, not air)"
        text_box = draw.textbbox((0, 0), legend, font=body_font)
        draw.rounded_rectangle((width * scale - (text_box[2] - text_box[0]) - 44 * scale, 22 * scale,
                                width * scale - 24 * scale, 50 * scale), radius=6 * scale,
                               fill=(255, 248, 236), outline=(221, 133, 67), width=scale)
        draw.text((width * scale - (text_box[2] - text_box[0]) - 34 * scale, 28 * scale), legend,
                  fill=(133, 76, 34), font=body_font)
    image.resize((width, height), Image.Resampling.LANCZOS).save(path, format="PNG", optimize=True)


def _format_coord(value: float) -> str:
    if abs(value - round(value)) < 1e-8:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _resource_state_key(name: str, properties: Mapping[str, Any]) -> str:
    if not properties:
        return name
    canonical = json.dumps(dict(sorted(properties.items())), ensure_ascii=False, separators=(",", ":"))
    return name + "[" + canonical + "]"


def _resource_proxy_boxes(
    name: str,
    properties: Mapping[str, Any],
    position: Sequence[int],
    fallback_reasons: Sequence[str],
) -> tuple[list[_Box], str, tuple[str, ...]]:
    """Use the existing state proxy while retaining resolver fallback reasons."""
    local_boxes, geometry_class, legacy_reason = _block_local_boxes(name, properties)
    reasons = tuple(sorted({str(reason) for reason in fallback_reasons if str(reason)}))
    if not reasons and legacy_reason:
        reasons = (legacy_reason,)
    if not reasons:
        reasons = ("resource_model_unavailable",)
    # The resource resolver's reason is authoritative in pack mode. The older
    # proxy-specific reason remains useful when the resolver had no detail.
    color = _stable_color(name)
    boxes = [
        _Box(
            tuple(int(position[i]) + local[i] for i in range(3)),
            tuple(int(position[i]) + local[i + 3] for i in range(3)),
            color,
            "block",
        )
        for local in local_boxes
    ]
    return boxes, geometry_class, reasons


def _render_section(path: Path, axis: int, plane: float, boxes: Sequence[_Box], unknown_chunks: Sequence[Mapping[str, Any]],
                    bounds_min: Sequence[int], bounds_max: Sequence[int], *, no_known_solids: bool = False,
                    resource_approximation: bool = False) -> None:
    width, height = _VIEW_SIZE
    s = _SS
    image = Image.new("RGB", (width * s, height * s), (245, 247, 248))
    draw = ImageDraw.Draw(image)
    if axis == 0:
        h_axis, v_axis = 2, 1
        h0, h1 = bounds_min[2], bounds_max[2] + 1
        v0, v1 = bounds_min[1], bounds_max[1] + 1
        title = f"SECTION X = {_format_coord(plane)} · viewed from -X"
        hlabel = "Z increases to the right (south)"
    elif axis == 1:
        h_axis, v_axis = 0, 2
        h0, h1 = bounds_min[0], bounds_max[0] + 1
        v0, v1 = bounds_min[2], bounds_max[2] + 1
        title = f"SECTION Y = {_format_coord(plane)} · horizontal slice"
        hlabel = "X east → ; north (-Z) is up"
    else:
        h_axis, v_axis = 0, 1
        h0, h1 = bounds_min[0], bounds_max[0] + 1
        v0, v1 = bounds_min[1], bounds_max[1] + 1
        title = f"SECTION Z = {_format_coord(plane)} · viewed from +Z"
        hlabel = "X increases to the right (east)"
    span_h, span_v = max(h1 - h0, 1), max(v1 - v0, 1)
    margin_l, margin_r, margin_t, margin_b = 62, 62, 108, 70
    fit = min((width - margin_l - margin_r) * s / span_h, (height - margin_t - margin_b) * s / span_v)
    ox = (width * s - span_h * fit) / 2 - h0 * fit
    oy = margin_t * s + ((height - margin_t - margin_b) * s - span_v * fit) / 2 + v1 * fit

    def xy(h: float, v: float) -> tuple[int, int]:
        # For a horizontal plan section, north (-Z) belongs at the top.
        if axis == 1:
            section_top = margin_t * s + ((height - margin_t - margin_b) * s - span_v * fit) / 2
            return round(ox + h * fit), round(section_top + (v - v0) * fit)
        return round(ox + h * fit), round(oy - v * fit)

    for box in boxes:
        if box.lo[axis] - 1e-8 <= plane < box.hi[axis] - 1e-8:
            hu0, hu1 = box.lo[h_axis], box.hi[h_axis]
            vu0, vu1 = box.lo[v_axis], box.hi[v_axis]
            points = [xy(hu0, vu0), xy(hu1, vu0), xy(hu1, vu1), xy(hu0, vu1)]
            draw.rectangle((min(p[0] for p in points), min(p[1] for p in points),
                            max(p[0] for p in points), max(p[1] for p in points)),
                           fill=box.color, outline=(67, 76, 81), width=s)

    # Overlay chunk-column hatch on the section plane.
    for chunk in unknown_chunks:
        cx, cz = int(chunk["x"]), int(chunk["z"])
        cx0, cx1 = max(bounds_min[0], cx * 16), min(bounds_max[0] + 1, (cx + 1) * 16)
        cz0, cz1 = max(bounds_min[2], cz * 16), min(bounds_max[2] + 1, (cz + 1) * 16)
        if axis == 0:
            if not (cx0 <= plane < cx1):
                continue
            rect = (xy(cz0, bounds_min[1]), xy(cz1, bounds_max[1] + 1))
        elif axis == 1:
            if not (bounds_min[1] <= plane < bounds_max[1] + 1):
                continue
            rect = (xy(cx0, cz0), xy(cx1, cz1))
        else:
            if not (cz0 <= plane < cz1):
                continue
            rect = (xy(cx0, bounds_min[1]), xy(cx1, bounds_max[1] + 1))
        x0, y0 = rect[0]
        x1, y1 = rect[1]
        left, right = min(x0, x1), max(x0, x1)
        top, bottom = min(y0, y1), max(y0, y1)
        region = [(left, top), (right, top), (right, bottom), (left, bottom)]
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        odraw.polygon(region, fill=(245, 137, 49, 68), outline=(193, 80, 20, 235), width=2 * s)
        pitch = 12 * s
        for d in range(left - (bottom - top), right + (bottom - top), pitch):
            odraw.line((d, bottom, d + (bottom - top), top), fill=(211, 100, 38, 225), width=2 * s)
        mask = Image.new("L", image.size, 0)
        ImageDraw.Draw(mask).polygon(region, fill=255)
        clipped = Image.new("RGBA", image.size, (0, 0, 0, 0))
        clipped.paste(overlay, (0, 0), mask)
        image = Image.alpha_composite(image.convert("RGBA"), clipped).convert("RGB")
        draw = ImageDraw.Draw(image)

    header_font, body_font = _load_font(20 * s), _load_font(12 * s)
    draw.text((32 * s, 20 * s), title, fill=(33, 45, 53), font=header_font)
    subtitle = hlabel + " · orange hatch = unknown chunk"
    if no_known_solids:
        subtitle += " · " + ("no visible static geometry" if resource_approximation else "no indexed non-air blocks")
    if resource_approximation:
        subtitle += " · resource geometry uses approximate bounds"
    draw.text((32 * s, 51 * s), subtitle, fill=(79, 91, 98), font=body_font)
    extents = f"Scene X [{bounds_min[0]}..{bounds_max[0]}]  Y [{bounds_min[1]}..{bounds_max[1]}]  Z [{bounds_min[2]}..{bounds_max[2]}] (inclusive)"
    draw.text((32 * s, (height - 30) * s), extents, fill=(56, 66, 73), font=body_font)
    image.resize((width, height), Image.Resampling.LANCZOS).save(path, format="PNG", optimize=True)


def render_scene(
    scene: Mapping[str, Any],
    output_dir: str | Path,
    *,
    max_blocks: int = 50000,
    views: Sequence[str] = ("iso", "top", "north", "east"),
    sections: bool = True,
    resource_packs: Sequence[str | Path] = (),
) -> dict[str, Any]:
    """Export a GLB and labelled CPU-rendered orthographic PNGs.

    Coordinates use X east, Y up, and Z south. Bounds in Scene v1 are inclusive
    block coordinates; mesh vertices are translated by ``world_origin`` and can
    be restored to world coordinates by adding that vector. Resource-pack roots
    are read in sequence from lowest to highest priority and are never copied
    beside the source tree; referenced model textures are embedded in the GLB.
    """
    from .scene import validate_scene

    normalized = validate_scene(scene)
    data = normalized if isinstance(normalized, Mapping) else scene
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    requested_views = tuple(views)
    unknown_views = set(requested_views) - _ALLOWED_VIEWS
    if unknown_views:
        raise ValueError(f"unsupported view(s): {', '.join(sorted(unknown_views))}; allowed: {', '.join(sorted(_ALLOWED_VIEWS))}")
    if len(set(requested_views)) != len(requested_views):
        raise ValueError("views must not contain duplicates")
    if max_blocks < 0:
        raise ValueError("max_blocks must be non-negative")
    if isinstance(resource_packs, (str, Path)):
        raise TypeError("resource_packs must be a sequence of paths, not one path")
    if len(resource_packs) > _MAX_RESOURCE_PACK_ROOTS:
        raise ValueError(f"resource_packs has {len(resource_packs)} roots; max is {_MAX_RESOURCE_PACK_ROOTS}")

    bounds_min = [int(v) for v in data["bounds"]["min"]]
    bounds_max = [int(v) for v in data["bounds"]["max"]]
    bounds_max_exclusive = [v + 1 for v in bounds_max]
    block_records = list(data.get("blocks", ()))
    if len(block_records) > max_blocks:
        raise ValueError(f"scene has {len(block_records)} blocks; max_blocks is {max_blocks}")
    palette = data["palette"]
    world_origin = list(bounds_min)

    resource_stack = None
    resource_sources: Any = []
    if resource_packs:
        from .resources import ResourcePackStack

        resource_stack = ResourcePackStack(resource_packs)

    block_boxes: list[_Box] = []
    resource_section_boxes: list[_Box] = []
    resource_faces: list[_Face] = []
    shape_counts: Counter[str] = Counter()
    fallback_reasons: Counter[str] = Counter()
    block_names: Counter[str] = Counter()
    resource_status_counts: Counter[str] = Counter()
    resource_state_counts: dict[str, dict[str, Any]] = {}
    resource_approximation_reasons: Counter[str] = Counter()
    resource_texture_sizes: dict[tuple[str, int], tuple[int, int]] = {}
    resource_total_texture_pixels = 0
    empty_resource_model_count = 0
    for record in block_records:
        pos = [int(v) for v in record["pos"]]
        entry = palette[int(record["palette"])]
        name = str(entry["name"])
        props = entry.get("properties", {})
        if resource_stack is None:
            local_boxes, geometry_class, fallback_reason = _block_local_boxes(name, props)
            if geometry_class == "air":
                continue
            shape_counts[geometry_class] += 1
            block_names[name] += 1
            if fallback_reason:
                fallback_reasons[fallback_reason] += 1
            color = _stable_color(name)
            for local in local_boxes:
                lo = tuple(pos[i] + local[i] for i in range(3))
                hi = tuple(pos[i] + local[i + 3] for i in range(3))
                block_boxes.append(_Box(lo, hi, color, "block"))
            continue

        resolution = resource_stack.resolve_block(name, props, position=tuple(pos))
        status = str(getattr(resolution, "status", "fallback"))
        if status not in {"resolved", "partial", "fallback"}:
            raise ValueError(f"resource resolver returned unsupported status {status!r} for {name}")
        reasons = tuple(sorted({str(reason) for reason in getattr(resolution, "reasons", ()) if str(reason)}))
        model_ids = tuple(sorted({str(model_id) for model_id in getattr(resolution, "model_ids", ()) if str(model_id)}))
        textures = getattr(resolution, "textures", {}) or {}
        model_faces = tuple(getattr(resolution, "faces", ()))
        state_key = _resource_state_key(name, props)
        state_record = resource_state_counts.setdefault(
            state_key,
            {"blocks": 0, "statuses": Counter(), "reasons": Counter(), "model_ids": Counter(),
             "resolved_faces": 0, "proxy_blocks": 0},
        )
        state_record["blocks"] += 1
        state_record["statuses"][status] += 1
        resource_status_counts[status] += 1
        state_record["model_ids"].update(model_ids)

        if status == "partial":
            partial_reasons = reasons or ("partial_static_model_support",)
            for reason in partial_reasons:
                state_record["reasons"][reason] += 1
                resource_approximation_reasons[reason] += 1

        # Validate a complete block resolution before adding any of its faces.
        # A malformed or missing texture falls back as one explicit state,
        # avoiding a half-model that looks authoritative.
        prepared_faces: list[_Face] = []
        validation_reason: str | None = None
        if status in {"resolved", "partial"}:
            for model_face in model_faces:
                try:
                    points = tuple(tuple(float(component) for component in point) for point in model_face.points)
                    if len(points) != 4 or any(len(point) != 3 for point in points):
                        raise ValueError("resource_face_not_quad")
                    if not np.isfinite(np.asarray(points, dtype=np.float64)).all():
                        raise ValueError("resource_face_non_finite_geometry")
                    if np.max(np.abs(np.asarray(points, dtype=np.float64))) > _MAX_RESOURCE_LOCAL_COORDINATE:
                        raise ValueError("resource_face_extent_limit")
                    raw_uv = getattr(model_face, "uv", None)
                    uv = None if raw_uv is None else tuple(tuple(float(component) for component in item) for item in raw_uv)
                    if uv is not None and (len(uv) != 4 or any(len(item) != 2 for item in uv)
                                           or not np.isfinite(np.asarray(uv, dtype=np.float64)).all()):
                        raise ValueError("resource_face_invalid_uv")
                    if uv is not None and np.max(np.abs(np.asarray(uv, dtype=np.float64))) > _MAX_RESOURCE_UV_COORDINATE:
                        raise ValueError("resource_face_uv_extent_limit")
                    texture_key_value = getattr(model_face, "texture", None)
                    texture_key = None if texture_key_value is None else str(texture_key_value)
                    texture = textures.get(texture_key_value) if texture_key_value is not None else None
                    if texture_key_value is None:
                        raise ValueError("resource_face_texture_missing")
                    if texture is None:
                        raise ValueError("resource_texture_missing")
                    if texture is not None:
                        if not isinstance(texture, Image.Image):
                            raise ValueError("resource_texture_invalid_image")
                        texture_size_key = (texture_key or "", id(texture))
                        if texture_size_key not in resource_texture_sizes:
                            pixels = int(texture.width) * int(texture.height)
                            if pixels <= 0 or pixels > _MAX_RESOURCE_TEXTURE_PIXELS:
                                raise ValueError("resource_texture_size_limit")
                            if resource_total_texture_pixels + pixels > _MAX_RESOURCE_TOTAL_TEXTURE_PIXELS:
                                raise ValueError("resource_texture_pixel_budget")
                            resource_texture_sizes[texture_size_key] = (texture.width, texture.height)
                            resource_total_texture_pixels += pixels
                        if uv is None:
                            raise ValueError("resource_textured_face_missing_uv")
                    prepared_faces.append(
                        _Face(
                            tuple(tuple(pos[i] + point[i] for i in range(3)) for point in points),
                            (255, 255, 255) if texture is not None else _stable_color(name),
                            "resource",
                            uv,
                            texture,
                            texture_key,
                        )
                    )
                    if len(resource_faces) + len(prepared_faces) > _MAX_RESOURCE_FACES:
                        raise ValueError("resource_model_face_limit")
                except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                    validation_reason = str(exc) or "resource_face_invalid"
                    break

        if validation_reason:
            previous_status = status
            reasons = tuple(sorted(set(reasons) | {validation_reason}))
            status = "fallback"
            state_record["statuses"]["fallback"] += 1
            state_record["statuses"][previous_status] -= 1
            resource_status_counts["fallback"] += 1
            resource_status_counts[previous_status] -= 1

        if status == "fallback":
            proxy_boxes, geometry_class, proxy_reasons = _resource_proxy_boxes(name, props, pos, reasons)
            if geometry_class == "air":
                continue
            shape_counts[geometry_class] += 1
            block_names[name] += 1
            state_record["proxy_blocks"] += 1
            for reason in proxy_reasons:
                state_record["reasons"][reason] += 1
                fallback_reasons[f"resource:{reason}"] += 1
            block_boxes.extend(proxy_boxes)
            continue

        resource_faces.extend(prepared_faces)
        state_record["resolved_faces"] += len(prepared_faces)
        if not prepared_faces:
            # A face-less resolved/partial model is intentionally left empty;
            # any parser approximation reason remains visible in the manifest.
            empty_resource_model_count += 1
            continue
        block_names[name] += 1
        class_name = "resource_model_partial" if status == "partial" else "resource_model_resolved"
        shape_counts[class_name] += 1

        # Cross-section pictures are 2D occupancy diagrams; the model's local
        # bounding box is intentionally used as an approximation there.
        all_points = np.asarray([point for face in prepared_faces for point in face.points], dtype=np.float64)
        lo = np.min(all_points, axis=0)
        hi = np.max(all_points, axis=0)
        for axis in range(3):
            if hi[axis] - lo[axis] < 1e-6:
                hi[axis] = lo[axis] + 0.001
        resource_section_boxes.append(
            _Box(tuple(lo.tolist()), tuple(hi.tolist()), _stable_color(name), "resource_section_proxy")
        )

    if resource_stack is not None:
        # describe() records content hashes for assets actually read during
        # resolution, so refresh it after the per-block work is complete.
        resource_sources = resource_stack.describe()
        resource_stack.close()

    marker_boxes = _unknown_marker_boxes(data, bounds_min, bounds_max_exclusive)
    # Once arbitrary resource geometry or transparent shapes may be present,
    # geometry is kept without hidden-face removal. This avoids suppressing a
    # neighboring face through a transparent or partial model.
    block_faces = _surface_faces(block_boxes, cull_internal=resource_stack is None)
    marker_faces = _surface_faces(marker_boxes, cull_internal=False)
    faces = block_faces + resource_faces + marker_faces

    block_mesh = _mesh_from_faces(block_faces, world_origin)
    marker_mesh = _mesh_from_faces(marker_faces, world_origin)
    gltf_scene = trimesh.Scene()
    if block_mesh is not None:
        mesh_name = "block_proxy_geometry" if resource_stack is not None else "simplified_block_geometry"
        gltf_scene.add_geometry(block_mesh, geom_name=mesh_name, node_name="minecraft_spatial_blocks")
    if resource_faces:
        from .resource_render import meshes_from_resource_faces

        for mesh_name, resource_mesh in meshes_from_resource_faces(resource_faces, world_origin):
            gltf_scene.add_geometry(resource_mesh, geom_name=mesh_name, node_name=mesh_name)
    if marker_mesh is not None:
        gltf_scene.add_geometry(marker_mesh, geom_name="unknown_chunk_frames", node_name="unknown_chunk_markers")
    glb_path = output / "model.glb"
    glb_bytes = gltf_scene.export(file_type="glb") if gltf_scene.geometry else _empty_glb()
    if resource_faces:
        from .resource_render import glb_nearest_repeat_sampling

        glb_bytes = glb_nearest_repeat_sampling(glb_bytes)
    glb_path.write_bytes(glb_bytes)

    views_dir = output / "views"
    views_dir.mkdir(exist_ok=True)
    view_paths: dict[str, str] = {}
    no_known_solids = sum(shape_counts.values()) == 0
    for view in requested_views:
        path = views_dir / f"{view}.png"
        _render_view(path, view, faces, data.get("unknown_chunks", ()), bounds_min, bounds_max,
                     no_known_solids=no_known_solids,
                     no_solids_message=("No visible static geometry resolved in this bounded Scene"
                                        if resource_stack is not None else None))
        view_paths[view] = str(path.relative_to(output))

    section_paths: dict[str, str] = {}
    section_planes: dict[str, float] = {}
    if sections:
        section_dir = output / "sections"
        section_dir.mkdir(exist_ok=True)
        for axis, axis_name in enumerate(("x", "y", "z")):
            plane = bounds_min[axis] + (bounds_max[axis] + 1 - bounds_min[axis]) / 2
            path = section_dir / f"{axis_name}_mid.png"
            _render_section(path, axis, plane, block_boxes + resource_section_boxes,
                            data.get("unknown_chunks", ()), bounds_min, bounds_max,
                            no_known_solids=no_known_solids,
                            resource_approximation=resource_stack is not None)
            section_paths[axis_name] = str(path.relative_to(output))
            section_planes[axis_name] = plane

    complexity_fallback_count = (
        sum(int(record["proxy_blocks"]) for record in resource_state_counts.values())
        if resource_stack is not None else sum(fallback_reasons.values())
    )
    resource_states_manifest = {
        key: {
            "blocks": int(record["blocks"]),
            "status_counts": {name: count for name, count in sorted(record["statuses"].items()) if count > 0},
            "reasons": {name: count for name, count in sorted(record["reasons"].items()) if count > 0},
            "model_ids": {name: count for name, count in sorted(record["model_ids"].items()) if count > 0},
            "resolved_faces": int(record["resolved_faces"]),
            "proxy_blocks": int(record["proxy_blocks"]),
        }
        for key, record in sorted(resource_state_counts.items())
    }
    if resource_stack is None:
        fidelity: dict[str, Any] = {
            "level": "simplified_voxel_geometry",
            "geometry_is_exact_minecraft_render_model": False,
            "textures_or_resource_packs_used": False,
            "lighting": "flat directional face shading; no game lighting or ambient occlusion",
            "occlusion": "per-pixel CPU depth buffer",
            "supported_state_geometry": ["slab bottom/top/double", "straight stairs with facing and half"],
            "limitations": [
                "Stairs are two-box straight proxies; stair shape is not neighbor-resolved and inner/outer corners fall back to a cube.",
                "Blocks without a dedicated slab/stair rule are rendered as a full cube, including plants, doors, fences, fluids, and modded states.",
                "Block textures, UVs, tint, connected textures, block entities, and resource-pack JSON models are not loaded.",
                "PNG output is an orthographic software preview, not a Minecraft or path-traced render.",
                "Unknown chunks are orange framed/hatch-marked and must not be interpreted as air.",
            ],
        }
    else:
        fidelity = {
            "level": "resource_pack_static_json_geometry",
            "geometry_is_exact_minecraft_render_model": False,
            "textures_or_resource_packs_used": True,
            "resource_model_scope": "supported static blockstate and model JSON faces; resolver statuses describe parser coverage, not game-exact output",
            "texture_sampling": "nearest texel with repeated authored UVs; PNG views and GLB use alpha cutout at 0.5, and referenced PNGs are embedded in GLB materials",
            "lighting": "unlit texture colors; no game lighting or ambient occlusion",
            "occlusion": "per-pixel CPU depth buffer without resource-face hidden-face culling",
            "sections": "resource model sections use local geometry bounding-box proxies; section PNGs are approximate",
            "approximations": dict(sorted(resource_approximation_reasons.items())),
            "limitations": [
                "Resolved means supported static JSON was read; output does not reproduce Minecraft rendering exactly.",
                "Java renderers and runtime-loaded models are not executed, and some dynamic behavior may not be detectable from JSON.",
                "Biome tint, animation timing, connected textures, neighbor-dependent context, block entities, and Java model loaders are not evaluated.",
                "PNG texture alpha is treated as a cutout mask; blended transparency, game lighting, and ambient occlusion are not simulated.",
                "Resource model cross-sections use local bounding-box proxies rather than exact texture or face intersections.",
                "Unsupported, missing, or invalid resource states use a documented per-state proxy fallback.",
                "Unknown chunks are orange framed/hatch-marked and must not be interpreted as air.",
            ],
        }
    manifest = {
        "schema_version": 1,
        "renderer": {"name": "minecraft-spatial-kit software voxel renderer", "version": "0.1"},
        "dimension": data["dimension"],
        "kind": data.get("kind", "world"),
        "bounds": {"min": bounds_min, "max": bounds_max, "max_exclusive": bounds_max_exclusive},
        "coordinate_system": {"x": "east", "y": "up", "z": "south", "units": "blocks"},
        "world_origin": world_origin,
        "glb_local_to_world": "world_position = glb_local_position + world_origin",
        "outputs": {"glb": "model.glb", "views": view_paths, "sections": section_paths,
                    "manifest": "render_manifest.json"},
        "section_planes": section_planes,
        "counts": {
            "scene_block_records": len(block_records),
            "rendered_non_air_blocks": sum(shape_counts.values()),
            "rendered_solid_boxes": len(block_boxes),
            "block_surface_quads": len(block_faces),
            "resource_model_faces": len(resource_faces),
            "resource_texture_images": len({(face.texture_key, id(face.texture)) for face in resource_faces
                                              if face.texture is not None}),
            "resource_empty_models": empty_resource_model_count,
            "resource_section_proxy_blocks": len(resource_section_boxes),
            "unknown_chunks": len(data.get("unknown_chunks", ())),
            "unknown_marker_boxes": len(marker_boxes),
            "unknown_marker_surface_quads": len(marker_faces),
            "shape_classes": dict(sorted(shape_counts.items())),
            "fallback_blocks": complexity_fallback_count,
            "fallback_reasons": dict(sorted(fallback_reasons.items())),
            "block_names": dict(sorted(block_names.items())),
            "resource_statuses": {name: count for name, count in sorted(resource_status_counts.items()) if count > 0},
            "resource_approximation_reasons": dict(sorted(resource_approximation_reasons.items())),
        },
        "resource_sources": resource_sources,
        "resource_states": resource_states_manifest,
        "fidelity": fidelity,
        "provenance": data.get("provenance", {}),
    }
    manifest_path = output / "render_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
