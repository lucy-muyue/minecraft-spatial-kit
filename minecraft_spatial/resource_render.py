"""GLB mesh construction for resource-pack resolved model faces.

This module deliberately handles only static textured geometry. Model and
texture lookup lives in :mod:`minecraft_spatial.resources`; callers pass the
already-resolved faces so this file has no pack-reading side effects.
"""

from __future__ import annotations

from collections import defaultdict
import json
import struct
from typing import Any, Sequence

import numpy as np
from PIL import Image
import trimesh


def glb_nearest_repeat_sampling(glb_bytes: bytes) -> bytes:
    """Set embedded GLB textures to nearest filtering and repeating UVs.

    Trimesh does not currently expose sampler filter settings through
    ``TextureVisuals``. Patching the JSON chunk keeps Minecraft's pixel-art
    sampling behavior aligned with the CPU preview without rewriting images.
    """
    if len(glb_bytes) < 20:
        raise ValueError("invalid GLB data")
    magic, version, total_length = struct.unpack_from("<4sII", glb_bytes, 0)
    if magic != b"glTF" or version != 2 or total_length != len(glb_bytes):
        raise ValueError("invalid GLB header")
    chunks: list[tuple[bytes, bytes]] = []
    cursor = 12
    while cursor < len(glb_bytes):
        chunk_length, chunk_type = struct.unpack_from("<I4s", glb_bytes, cursor)
        cursor += 8
        chunk = glb_bytes[cursor:cursor + chunk_length]
        if len(chunk) != chunk_length:
            raise ValueError("truncated GLB chunk")
        chunks.append((chunk_type, chunk))
        cursor += chunk_length
    if not chunks or chunks[0][0] != b"JSON":
        raise ValueError("GLB is missing its JSON chunk")
    document = json.loads(chunks[0][1].rstrip(b" \t\r\n\0"))
    textures = document.get("textures", [])
    if not textures:
        return glb_bytes
    document["samplers"] = [{"magFilter": 9728, "minFilter": 9728, "wrapS": 10497, "wrapT": 10497}]
    for texture in textures:
        texture["sampler"] = 0
    json_chunk = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    json_chunk += b" " * ((-len(json_chunk)) % 4)
    chunks[0] = (b"JSON", json_chunk)
    rebuilt_chunks = b"".join(struct.pack("<I4s", len(chunk), chunk_type) + chunk for chunk_type, chunk in chunks)
    return struct.pack("<4sII", b"glTF", 2, 12 + len(rebuilt_chunks)) + rebuilt_chunks


def meshes_from_resource_faces(
    faces: Sequence[Any], world_origin: Sequence[int]
) -> list[tuple[str, trimesh.Trimesh]]:
    """Return one mesh per texture (and one color mesh for untextured faces).

    Vertices are kept per face so each model face can carry its own UVs without
    smoothing or accidental UV interpolation across block boundaries. Texture
    images are handed to trimesh's GLB exporter as PIL images and are embedded
    in the resulting GLB.
    """
    groups: dict[tuple[str, int] | tuple[str, str], list[Any]] = defaultdict(list)
    images: dict[tuple[str, int] | tuple[str, str], Image.Image] = {}
    for face in faces:
        image = getattr(face, "texture", None)
        texture_key = getattr(face, "texture_key", None)
        if image is None:
            key: tuple[str, int] | tuple[str, str] = ("color", 0)
        else:
            key = ("texture", str(texture_key)) if texture_key else ("image", id(image))
            images[key] = image.convert("RGBA")
        groups[key].append(face)

    output: list[tuple[str, trimesh.Trimesh]] = []
    origin = np.asarray(world_origin, dtype=np.float64)
    for key in sorted(groups, key=lambda item: (item[0], str(item[1]))):
        grouped_faces = groups[key]
        vertices: list[tuple[float, float, float]] = []
        triangles: list[tuple[int, int, int]] = []
        colors: list[tuple[int, int, int, int]] = []
        uvs: list[tuple[float, float]] = []
        for face in grouped_faces:
            points = tuple(getattr(face, "points"))
            start = len(vertices)
            vertices.extend(tuple(np.asarray(point, dtype=np.float64) - origin) for point in points)
            # Resource-pack block model faces are quads. The resolver validates
            # this shape before it reaches the renderer.
            triangles.extend(((start, start + 1, start + 2), (start, start + 2, start + 3)))
            color = tuple(getattr(face, "color", (255, 255, 255)))
            colors.extend((*color, 255) for _ in range(4))
            face_uv = getattr(face, "uv", None)
            if face_uv is not None:
                # ResolvedFace UVs use the pack's top-left image origin. Trimesh
                # stores texture UVs bottom-left internally and flips V during
                # glTF export, so invert it here to preserve the resolver values
                # in the GLB accessor and keep CPU PNG sampling aligned.
                uvs.extend((float(uv[0]), 1.0 - float(uv[1])) for uv in face_uv)
            else:
                uvs.extend(((0.0, 0.0),) * 4)

        mesh = trimesh.Trimesh(
            vertices=np.asarray(vertices, dtype=np.float64),
            faces=np.asarray(triangles, dtype=np.int64),
            process=False,
        )
        texture_image = images.get(key)
        if texture_image is None:
            mesh.visual.vertex_colors = np.asarray(colors, dtype=np.uint8)
            name = "resource_model_untextured"
        else:
            rgba = np.asarray(texture_image, dtype=np.uint8)
            has_transparency = rgba.shape[2] == 4 and bool(np.any(rgba[:, :, 3] < 255))
            material = trimesh.visual.material.PBRMaterial(
                name=f"resource:{key[1]}",
                baseColorFactor=[255, 255, 255, 255],
                baseColorTexture=texture_image,
                metallicFactor=0.0,
                roughnessFactor=1.0,
                alphaMode="MASK" if has_transparency else "OPAQUE",
                alphaCutoff=0.5 if has_transparency else None,
                doubleSided=False,
            )
            mesh.visual = trimesh.visual.texture.TextureVisuals(
                uv=np.asarray(uvs, dtype=np.float64),
                material=material,
            )
            name = f"resource_model_texture_{key[1]}"
        output.append((name, mesh))
    return output
