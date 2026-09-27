from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from minecraft_spatial.render import render_scene


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_north_face_pack(root: Path) -> None:
    assets = root / "assets" / "testmod"
    for block, texture in (("cutout", "cutout"), ("back", "back")):
        uv_right = 32 if block == "cutout" else 16
        _write_json(
            assets / "blockstates" / f"{block}.json",
            {"variants": {"": {"model": f"testmod:block/{block}"}}},
        )
        _write_json(
            assets / "models" / "block" / f"{block}.json",
            {
                "textures": {"all": f"testmod:block/{texture}"},
                "elements": [
                    {
                        "from": [0, 0, 0],
                        "to": [16, 16, 16],
                        "faces": {"north": {"texture": "#all", "uv": [0, 0, uv_right, 16]}},
                    }
                ],
            },
        )
    cutout = Image.new("RGBA", (4, 4), (230, 32, 24, 0))
    for y in range(4):
        for x in range(2):
            cutout.putpixel((x, y), (230, 32, 24, 255) if y < 2 else (32, 220, 40, 255))
    texture_dir = assets / "textures" / "block"
    texture_dir.mkdir(parents=True, exist_ok=True)
    cutout.save(texture_dir / "cutout.png")
    Image.new("RGBA", (4, 4), (24, 48, 230, 255)).save(texture_dir / "back.png")


def _scene(palette: list[dict], blocks: list[dict], minimum: list[int], maximum: list[int], unknown: list[dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "dimension": "minecraft:overworld",
        "bounds": {"min": minimum, "max": maximum},
        "palette": palette,
        "blocks": blocks,
        "unknown_chunks": unknown or [],
        "provenance": {"source": "synthetic resource render test"},
        "kind": "world",
    }


def _read_glb(path: Path) -> tuple[dict, bytes]:
    data = path.read_bytes()
    magic, version, total_length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2 or total_length != len(data):
        raise AssertionError("invalid GLB header")
    json_length, json_type = struct.unpack_from("<I4s", data, 12)
    if json_type != b"JSON":
        raise AssertionError("GLB does not start with a JSON chunk")
    document = json.loads(data[20:20 + json_length])
    cursor = 20 + json_length
    bin_length, bin_type = struct.unpack_from("<I4s", data, cursor)
    if bin_type != b"BIN\0":
        raise AssertionError("GLB has no binary chunk")
    binary = data[cursor + 8:cursor + 8 + bin_length]
    return document, binary


def _accessor_array(document: dict, binary: bytes, accessor_index: int) -> np.ndarray:
    accessor = document["accessors"][accessor_index]
    view = document["bufferViews"][accessor["bufferView"]]
    dtype = {5126: np.dtype("<f4"), 5123: np.dtype("<u2"), 5125: np.dtype("<u4")}[accessor["componentType"]]
    offset = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    shape = {"SCALAR": (), "VEC2": (2,), "VEC3": (3,), "VEC4": (4,)}[accessor["type"]]
    count = int(accessor["count"])
    if shape:
        return np.frombuffer(binary, dtype=dtype, count=count * shape[0], offset=offset).reshape((count, *shape))
    return np.frombuffer(binary, dtype=dtype, count=count, offset=offset)


class ResourceRenderTests(unittest.TestCase):
    def test_glb_embeds_texture_uv_geometry_and_png_alpha_does_not_claim_depth(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pack = root / "pack"
            _write_north_face_pack(pack)
            origin = [-1234567, 64, 9000]
            source = _scene(
                [
                    {"name": "testmod:cutout", "properties": {}},
                    {"name": "testmod:back", "properties": {}},
                ],
                [{"pos": origin, "palette": 0}, {"pos": [origin[0], origin[1], origin[2] + 1], "palette": 1}],
                origin, [origin[0], origin[1], origin[2] + 1],
            )
            result = render_scene(source, root / "out", resource_packs=(pack,), views=("north",), sections=False)

            self.assertEqual(result["counts"]["resource_statuses"], {"resolved": 2})
            self.assertEqual(result["counts"]["resource_model_faces"], 2)
            self.assertEqual(result["world_origin"], origin)
            self.assertEqual(result["resource_states"]["testmod:cutout"]["status_counts"], {"resolved": 1})
            self.assertGreater(result["resource_sources"]["sources"][0]["loaded_asset_count"], 0)

            pixels = np.asarray(Image.open(root / "out" / "views" / "north.png").convert("RGB"))
            red = (pixels[:, :, 0] > 170) & (pixels[:, :, 1] < 100) & (pixels[:, :, 2] < 100)
            green = (pixels[:, :, 1] > 150) & (pixels[:, :, 0] < 100) & (pixels[:, :, 2] < 100)
            blue = (pixels[:, :, 2] > 170) & (pixels[:, :, 0] < 100) & (pixels[:, :, 1] < 100)
            # The red front model covers half of its face; its clear texels let
            # the farther blue face win the CPU depth test.
            self.assertGreater(int(red.sum()), 20_000)
            self.assertGreater(int(green.sum()), 20_000)
            self.assertGreater(int(blue.sum()), 20_000)
            red_y = np.where(red)[0].mean()
            green_y = np.where(green)[0].mean()
            self.assertLess(red_y, green_y)
            opaque_fraction = (int(red.sum()) + int(green.sum())) / (
                int(red.sum()) + int(green.sum()) + int(blue.sum())
            )
            self.assertGreater(opaque_fraction, 0.40)
            self.assertLess(opaque_fraction, 0.60)

            document, binary = _read_glb(root / "out" / "model.glb")
            self.assertEqual(len(document["images"]), 2)
            self.assertTrue(all("bufferView" in image and image["mimeType"] == "image/png" for image in document["images"]))
            materials = document["materials"]
            self.assertIn("MASK", {material.get("alphaMode", "OPAQUE") for material in materials})
            self.assertEqual(document["samplers"][0], {"magFilter": 9728, "minFilter": 9728,
                                                        "wrapS": 10497, "wrapT": 10497})
            textured_uvs = []
            positions = []
            for mesh in document["meshes"]:
                for primitive in mesh["primitives"]:
                    if "TEXCOORD_0" in primitive["attributes"]:
                        textured_uvs.append(_accessor_array(document, binary, primitive["attributes"]["TEXCOORD_0"]))
                        positions.append(_accessor_array(document, binary, primitive["attributes"]["POSITION"]))
            self.assertEqual(len(textured_uvs), 2)
            np.testing.assert_allclose(np.vstack(positions).min(axis=0), [0, 0, 0])
            np.testing.assert_allclose(np.vstack(positions).max(axis=0), [1, 1, 1])
            corner_sets = [{tuple(np.round(uv, 5)) for uv in mesh_uvs[:4]} for mesh_uvs in textured_uvs]
            self.assertIn({(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)}, corner_sets)
            self.assertIn({(0.0, 0.0), (2.0, 0.0), (2.0, 1.0), (0.0, 1.0)}, corner_sets)
            cutout_mesh_index = next(index for index, mesh_uvs in enumerate(textured_uvs)
                                     if float(mesh_uvs[:, 0].max()) > 1.5)
            for position, uv in zip(positions[cutout_mesh_index], textured_uvs[cutout_mesh_index]):
                if position[1] > 0.99:
                    self.assertAlmostEqual(float(uv[1]), 0.0, places=5)
                elif position[1] < 0.01:
                    self.assertAlmostEqual(float(uv[1]), 1.0, places=5)

    def test_unknown_chunks_and_missing_models_are_reported_with_fallback_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pack = root / "pack"
            _write_north_face_pack(pack)
            source = _scene(
                [{"name": "testmod:no_model", "properties": {}}],
                [{"pos": [1, 0, 1], "palette": 0}],
                [1, 0, 1], [1, 0, 1],
            )
            result = render_scene(source, root / "fallback", resource_packs=(pack,), views=(), sections=True)
            state = result["resource_states"]["testmod:no_model"]
            self.assertEqual(state["status_counts"], {"fallback": 1})
            self.assertEqual(state["proxy_blocks"], 1)
            self.assertTrue(state["reasons"])
            self.assertEqual(result["counts"]["fallback_blocks"], 1)

            unknown = _scene([], [], [0, 0, 0], [2, 3, 2], [{"x": 0, "z": 0, "reason": "not indexed"}])
            unknown_result = render_scene(unknown, root / "unknown", resource_packs=(pack,), views=("top",), sections=False)
            self.assertEqual(unknown_result["counts"]["unknown_chunks"], 1)
            self.assertGreater(unknown_result["counts"]["unknown_marker_surface_quads"], 0)

    def test_resolved_empty_model_is_counted_without_claiming_visible_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pack = root / "pack"
            assets = pack / "assets" / "testmod"
            _write_json(assets / "blockstates" / "empty.json", {"variants": {"": {"model": "testmod:block/empty"}}})
            _write_json(assets / "models" / "block" / "empty.json", {"elements": []})
            source = _scene(
                [{"name": "testmod:empty", "properties": {}}],
                [{"pos": [4, 8, 12], "palette": 0}],
                [4, 8, 12], [4, 8, 12],
            )
            result = render_scene(source, root / "out", resource_packs=(pack,), views=("north",), sections=True)
            self.assertEqual(result["resource_states"]["testmod:empty"]["status_counts"], {"resolved": 1})
            self.assertEqual(result["counts"]["resource_empty_models"], 1)
            self.assertEqual(result["counts"]["rendered_non_air_blocks"], 0)
            self.assertTrue((root / "out" / "sections" / "y_mid.png").is_file())


if __name__ == "__main__":
    unittest.main()
