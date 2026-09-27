"""Small, offline renderer for Scene v1.

The meshes in this module are useful spatial proxies. They are deliberately not
presented as Minecraft's resource-pack-rendered block models: texture atlases,
model JSON, biome tint, connected textures, and neighbour-dependent models are
outside the renderer's scope.
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


def _render_view(
    path: Path,
    view: str,
    faces: Sequence[_Face],
    unknown_chunks: Sequence[Mapping[str, Any]],
    bounds_min: Sequence[int],
    bounds_max: Sequence[int],
    *,
    no_known_solids: bool = False,
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
    for face in faces:
        pts3 = np.asarray(face.points, dtype=np.float64)
        normal = np.cross(pts3[1] - pts3[0], pts3[2] - pts3[0])
        if float(np.dot(normal, camera)) <= 1e-10:
            continue  # opaque backfaces cannot be visible from this camera
        projected_face = [screen_float(p) for p in face.points]
        depths = np.asarray([project(p)[2] for p in face.points], dtype=np.float64)
        fill = np.asarray(face.color, dtype=np.uint8)
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
                zview[update] = z[update].astype(np.float32)
                tile = pixels[min_y:max_y + 1, min_x:max_x + 1]
                tile[update] = fill
        line_color = (72, 79, 82) if face.source == "block" else (199, 84, 20)
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
        draw.text((32 * scale, 74 * scale), "No indexed non-air blocks in this bounded Scene", fill=(139, 87, 43), font=body_font)
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


def _render_section(path: Path, axis: int, plane: float, boxes: Sequence[_Box], unknown_chunks: Sequence[Mapping[str, Any]],
                    bounds_min: Sequence[int], bounds_max: Sequence[int], *, no_known_solids: bool = False) -> None:
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
        subtitle += " · no indexed non-air blocks"
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
) -> dict[str, Any]:
    """Export a simplified GLB and labelled CPU-rendered orthographic PNGs.

    Coordinates use X east, Y up, and Z south. Bounds in Scene v1 are inclusive
    block coordinates; mesh vertices are translated by ``world_origin`` and can
    be restored to world coordinates by adding that vector.
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

    bounds_min = [int(v) for v in data["bounds"]["min"]]
    bounds_max = [int(v) for v in data["bounds"]["max"]]
    bounds_max_exclusive = [v + 1 for v in bounds_max]
    block_records = list(data.get("blocks", ()))
    if len(block_records) > max_blocks:
        raise ValueError(f"scene has {len(block_records)} blocks; max_blocks is {max_blocks}")
    palette = data["palette"]
    world_origin = list(bounds_min)

    block_boxes: list[_Box] = []
    shape_counts: Counter[str] = Counter()
    fallback_reasons: Counter[str] = Counter()
    block_names: Counter[str] = Counter()
    for record in block_records:
        pos = [int(v) for v in record["pos"]]
        entry = palette[int(record["palette"])]
        name = str(entry["name"])
        props = entry.get("properties", {})
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

    marker_boxes = _unknown_marker_boxes(data, bounds_min, bounds_max_exclusive)
    block_faces = _surface_faces(block_boxes, cull_internal=True)
    marker_faces = _surface_faces(marker_boxes, cull_internal=False)
    faces = block_faces + marker_faces

    block_mesh = _mesh_from_faces(block_faces, world_origin)
    marker_mesh = _mesh_from_faces(marker_faces, world_origin)
    gltf_scene = trimesh.Scene()
    if block_mesh is not None:
        gltf_scene.add_geometry(block_mesh, geom_name="simplified_block_geometry", node_name="minecraft_spatial_blocks")
    if marker_mesh is not None:
        gltf_scene.add_geometry(marker_mesh, geom_name="unknown_chunk_frames", node_name="unknown_chunk_markers")
    glb_path = output / "model.glb"
    glb_bytes = gltf_scene.export(file_type="glb") if gltf_scene.geometry else _empty_glb()
    glb_path.write_bytes(glb_bytes)

    views_dir = output / "views"
    views_dir.mkdir(exist_ok=True)
    view_paths: dict[str, str] = {}
    no_known_solids = sum(shape_counts.values()) == 0
    for view in requested_views:
        path = views_dir / f"{view}.png"
        _render_view(path, view, faces, data.get("unknown_chunks", ()), bounds_min, bounds_max,
                     no_known_solids=no_known_solids)
        view_paths[view] = str(path.relative_to(output))

    section_paths: dict[str, str] = {}
    section_planes: dict[str, float] = {}
    if sections:
        section_dir = output / "sections"
        section_dir.mkdir(exist_ok=True)
        for axis, axis_name in enumerate(("x", "y", "z")):
            plane = bounds_min[axis] + (bounds_max[axis] + 1 - bounds_min[axis]) / 2
            path = section_dir / f"{axis_name}_mid.png"
            _render_section(path, axis, plane, block_boxes, data.get("unknown_chunks", ()), bounds_min, bounds_max,
                            no_known_solids=no_known_solids)
            section_paths[axis_name] = str(path.relative_to(output))
            section_planes[axis_name] = plane

    complexity_fallback_count = sum(fallback_reasons.values())
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
            "unknown_chunks": len(data.get("unknown_chunks", ())),
            "unknown_marker_boxes": len(marker_boxes),
            "unknown_marker_surface_quads": len(marker_faces),
            "shape_classes": dict(sorted(shape_counts.items())),
            "fallback_blocks": complexity_fallback_count,
            "fallback_reasons": dict(sorted(fallback_reasons.items())),
            "block_names": dict(sorted(block_names.items())),
        },
        "fidelity": {
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
        },
        "provenance": data.get("provenance", {}),
    }
    manifest_path = output / "render_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
