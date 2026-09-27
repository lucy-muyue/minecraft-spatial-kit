from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image
import trimesh

from minecraft_spatial.render import (
    _Box,
    _block_local_boxes,
    _stable_color,
    _surface_faces,
    _view_basis,
    render_scene,
)


def scene(
    *,
    minimum: list[int],
    maximum: list[int],
    palette: list[dict] | None = None,
    blocks: list[dict] | None = None,
    unknown: list[dict] | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "dimension": "minecraft:overworld",
        "bounds": {"min": minimum, "max": maximum},
        "palette": palette or [],
        "blocks": blocks or [],
        "unknown_chunks": unknown or [],
        "provenance": {"source": "synthetic test"},
        "kind": "world",
    }


class RenderGeometryTests(unittest.TestCase):
    def test_adjacent_cubes_hide_the_shared_faces(self) -> None:
        color = (120, 130, 140)
        boxes = [
            _Box((0, 0, 0), (1, 1, 1), color),
            _Box((1, 0, 0), (2, 1, 1), color),
        ]
        self.assertEqual(len(_surface_faces(boxes)), 10)

    def test_slab_and_straight_stair_states_have_distinct_geometry(self) -> None:
        top_slab, kind, reason = _block_local_boxes("minecraft:oak_slab", {"type": "top"})
        self.assertEqual((kind, reason), ("slab", None))
        self.assertEqual(top_slab, [(0.0, 0.5, 0.0, 1.0, 1.0, 1.0)])

        north, kind, reason = _block_local_boxes(
            "minecraft:stone_brick_stairs", {"facing": "north", "half": "bottom", "shape": "straight"}
        )
        south, _, _ = _block_local_boxes(
            "minecraft:stone_brick_stairs", {"facing": "south", "half": "bottom", "shape": "straight"}
        )
        self.assertEqual((kind, reason), ("straight_stair", None))
        self.assertEqual(len(north), 2)
        self.assertNotEqual(north[1], south[1])
        self.assertEqual((north[1][2], north[1][5]), (0.0, 0.5))
        self.assertEqual((south[1][2], south[1][5]), (0.5, 1.0))

    def test_unknown_stair_shapes_are_reported_as_cube_fallbacks(self) -> None:
        boxes, kind, reason = _block_local_boxes(
            "minecraft:oak_stairs", {"facing": "north", "half": "bottom", "shape": "inner_left"}
        )
        self.assertEqual(kind, "cube_proxy")
        self.assertEqual(reason, "stair_shape_not_straight")
        self.assertEqual(boxes, [(0.0, 0.0, 0.0, 1.0, 1.0, 1.0)])

    def test_palette_color_is_deterministic(self) -> None:
        self.assertEqual(_stable_color("mod:unknown_block"), _stable_color("mod:unknown_block"))

    def test_north_view_looks_from_north_with_west_on_screen_right(self) -> None:
        right, up, camera, label = _view_basis("north")
        np.testing.assert_array_equal(right, [-1.0, 0.0, 0.0])
        np.testing.assert_array_equal(up, [0.0, 1.0, 0.0])
        np.testing.assert_array_equal(camera, [0.0, 0.0, -1.0])
        self.assertIn("west is right", label)


class RenderOutputTests(unittest.TestCase):
    def test_glb_vertices_are_local_and_manifest_restores_world_origin(self) -> None:
        source = scene(
            minimum=[-1234567, 64, 9000], maximum=[-1234567, 64, 9000],
            palette=[{"name": "minecraft:stone", "properties": {}}],
            blocks=[{"pos": [-1234567, 64, 9000], "palette": 0}],
        )
        with tempfile.TemporaryDirectory() as temp:
            result = render_scene(source, temp, views=(), sections=False)
            loaded = trimesh.load(Path(temp) / "model.glb", force="scene")
            np.testing.assert_allclose(loaded.bounds, [[0, 0, 0], [1, 1, 1]])
            self.assertEqual(result["world_origin"], [-1234567, 64, 9000])
            self.assertEqual(result["glb_local_to_world"], "world_position = glb_local_position + world_origin")

    def test_unknown_chunk_is_rendered_in_mesh_png_and_manifest(self) -> None:
        source = scene(
            minimum=[-2, 4, 0], maximum=[2, 6, 4],
            unknown=[{"x": -1, "z": 0, "reason": "not indexed"}],
        )
        with tempfile.TemporaryDirectory() as temp:
            result = render_scene(source, temp, views=("iso", "top"), sections=True)
            self.assertEqual(result["counts"]["unknown_chunks"], 1)
            self.assertGreater(result["counts"]["unknown_marker_surface_quads"], 0)
            loaded = trimesh.load(Path(temp) / "model.glb", force="scene")
            self.assertTrue(any("unknown" in str(name) for name in loaded.geometry))
            pixels = np.asarray(Image.open(Path(temp) / "views/top.png").convert("RGB"))
            orange = (pixels[:, :, 0] > 190) & (pixels[:, :, 1] > 70) & (pixels[:, :, 1] < 210) & (pixels[:, :, 2] < 130)
            self.assertGreater(int(orange.sum()), 10)
            self.assertTrue((Path(temp) / "sections/x_mid.png").is_file())

    def test_depth_buffer_keeps_the_near_north_block_in_front(self) -> None:
        source = scene(
            minimum=[0, 0, 0], maximum=[0, 0, 1],
            palette=[
                {"name": "minecraft:red_concrete", "properties": {}},
                {"name": "minecraft:blue_concrete", "properties": {}},
            ],
            blocks=[{"pos": [0, 0, 0], "palette": 0}, {"pos": [0, 0, 1], "palette": 1}],
        )
        with tempfile.TemporaryDirectory() as temp:
            render_scene(source, Path(temp) / "red_front", views=("north",), sections=False)
            red_front = np.asarray(Image.open(Path(temp) / "red_front/views/north.png").convert("RGB"))
            source["blocks"] = [{"pos": [0, 0, 0], "palette": 1}, {"pos": [0, 0, 1], "palette": 0}]
            render_scene(source, Path(temp) / "blue_front", views=("north",), sections=False)
            blue_front = np.asarray(Image.open(Path(temp) / "blue_front/views/north.png").convert("RGB"))
            red_crop = red_front[390:430, 500:540].astype(np.int16)
            blue_crop = blue_front[390:430, 500:540].astype(np.int16)
            # The near block's color wins; swapping depth order swaps the pixel color.
            self.assertGreater(float((red_crop[:, :, 0] - red_crop[:, :, 2]).mean()), 40.0)
            self.assertGreater(float((blue_crop[:, :, 2] - blue_crop[:, :, 0]).mean()), 40.0)

    def test_fully_known_empty_scene_still_emits_labeled_images_and_empty_glb(self) -> None:
        source = scene(minimum=[-3, 10, 7], maximum=[2, 12, 9])
        with tempfile.TemporaryDirectory() as temp:
            result = render_scene(source, temp, views=("iso",), sections=True)
            self.assertEqual(result["counts"]["rendered_non_air_blocks"], 0)
            self.assertEqual(result["counts"]["unknown_chunks"], 0)
            self.assertTrue((Path(temp) / "model.glb").is_file())
            self.assertFalse(trimesh.load(Path(temp) / "model.glb", force="scene").geometry)
            image = np.asarray(Image.open(Path(temp) / "views/iso.png").convert("RGB"))
            self.assertGreater(len(np.unique(image.reshape(-1, 3), axis=0)), 10)
            self.assertTrue((Path(temp) / "sections/y_mid.png").is_file())

    def test_block_limit_is_enforced(self) -> None:
        source = scene(
            minimum=[0, 0, 0], maximum=[1, 0, 0],
            palette=[{"name": "minecraft:stone", "properties": {}}],
            blocks=[{"pos": [0, 0, 0], "palette": 0}, {"pos": [1, 0, 0], "palette": 0}],
        )
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "max_blocks"):
                render_scene(source, temp, max_blocks=1)


if __name__ == "__main__":
    unittest.main()
