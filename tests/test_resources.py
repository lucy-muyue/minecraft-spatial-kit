from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from minecraft_spatial.resources import ResourceError, ResourcePackStack, _MAX_JSON_NESTING


def write_json(root: Path, relative: str, value: dict) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value), encoding="utf-8")


def write_texture(root: Path, namespace: str, path: str, color: tuple[int, int, int, int]) -> None:
    target = root / "assets" / namespace / "textures" / f"{path}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (4, 4), color).save(target)


def face_model(texture: str, *, face: str = "north", uv: list[int] | None = None, rotation: dict | None = None) -> dict:
    one_face = {"texture": texture}
    if uv is not None:
        one_face["uv"] = uv
    return {
        "textures": {"side": texture},
        "elements": [{
            "from": [2, 3, 4], "to": [10, 11, 12],
            **({"rotation": rotation} if rotation else {}),
            "faces": {face: one_face},
        }],
    }


class ResourceResolverTests(unittest.TestCase):
    def test_variant_match_inheritance_default_namespace_and_later_texture_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "base"
            override = Path(temp) / "override"
            write_json(base, "assets/test/blockstates/crate.json", {
                "variants": {"facing=north|south,lit=true": {"model": "test:block/child"}},
            })
            write_json(base, "assets/test/models/block/child.json", {
                # Unqualified parent names resolve in minecraft, not test.
                "parent": "block/base",
                "textures": {"all": "test:block/child"},
            })
            write_json(base, "assets/minecraft/models/block/base.json", {
                "textures": {"all": "test:block/base"},
                "elements": [{"from": [0, 0, 0], "to": [16, 8, 16], "faces": {
                    "north": {"texture": "#all", "uv": [0, 0, 16, 16], "rotation": 90, "tintindex": 2},
                }}],
            })
            write_texture(base, "test", "block/base", (255, 0, 0, 255))
            write_quadrant_texture(override, "test", "block/child")

            with ResourcePackStack([base, override]) as stack:
                result = stack.resolve_block("test:crate", {"facing": "north", "lit": "true"}, (3, 4, 5))
                self.assertEqual(result.status, "partial")
                self.assertTrue(any(reason.startswith("tintindex_not_applied:2:north") for reason in result.reasons))
                self.assertEqual(result.model_ids, ("minecraft:block/base", "test:block/child"))
                self.assertEqual(len(result.faces), 1)
                face = result.faces[0]
                self.assertEqual(face.texture, "test:block/child")
                self.assertEqual(face.tintindex, 2)
                texture = result.textures[face.texture]
                self.assertEqual(texture.getpixel((0, 0)), (255, 0, 0, 255))
                self.assertEqual(face.points[0], (0.0, 0.5, 0.0))
                # Asymmetric corner colors expose orientation: after clockwise
                # face rotation, face corners sample TL, BL, BR, TR.
                self.assertEqual(face.uv, ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)))
                sampled = tuple(texture.getpixel((round(u * 15), round(v * 15))) for u, v in face.uv)
                self.assertEqual(sampled, (
                    (255, 0, 0, 255), (255, 255, 0, 255),
                    (0, 0, 255, 255), (0, 255, 0, 255),
                ))
                report = stack.describe()
                self.assertEqual([source["priority"] for source in report["sources"]], [0, 1])
                self.assertTrue(all(source["content_fingerprint"] for source in report["sources"]))
                self.assertEqual(report["sources"][1]["loaded_asset_count"], 1)

    def test_multipart_nested_and_or_and_pipe_alternatives(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(root, "assets/test/blockstates/lamp.json", {"multipart": [
                {"when": {"AND": [{"facing": "north|south"}, {"powered": "true"}]},
                 "apply": {"model": "test:block/base"}},
                {"when": {"OR": [{"axis": "x"}, {"AND": [{"axis": "z"}, {"powered": "false"}]}]},
                 "apply": {"model": "test:block/top"}},
            ]})
            for suffix in ("base", "top"):
                write_json(root, f"assets/test/models/block/{suffix}.json", face_model("test:block/pixel"))
            write_texture(root, "test", "block/pixel", (20, 40, 60, 255))

            with ResourcePackStack([root]) as stack:
                result = stack.resolve_block("test:lamp", {"facing": "south", "powered": "true", "axis": "x"})
                self.assertEqual(result.status, "resolved")
                self.assertEqual(result.model_ids, ("test:block/base", "test:block/top"))
                self.assertEqual(len(result.faces), 2)
                unmatched = stack.resolve_block("test:lamp", {"facing": "east", "powered": "true", "axis": "y"})
                self.assertEqual(unmatched.status, "fallback")
                self.assertIn("no_matching_multipart_parts", unmatched.reasons)

    def test_default_uv_and_blockstate_y_rotation_preserve_face_correspondence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(root, "assets/test/blockstates/panel.json", {
                "variants": {"": {"model": "test:block/panel", "y": 90}},
            })
            write_json(root, "assets/test/models/block/panel.json", face_model("test:block/pixel"))
            write_texture(root, "test", "block/pixel", (1, 2, 3, 255))

            with ResourcePackStack([root]) as stack:
                result = stack.resolve_block("test:panel")
                face = result.faces[0]
                self.assertEqual(result.status, "resolved")
                # Vanilla blockstate y=90 turns north toward east (as in the
                # furnace state variants), while UVs stay attached to corners.
                self.assertEqual(face.points[0], (0.75, 11 / 16, 2 / 16))
                self.assertEqual(face.uv[0], (14 / 16, 5 / 16))
                self.assertEqual(face.uv[1], (6 / 16, 5 / 16))

    def test_blockstate_x_rotation_matches_observer_north_to_down(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(root, "assets/test/blockstates/panel.json", {
                "variants": {"": {"model": "test:block/panel", "x": 90}},
            })
            write_json(root, "assets/test/models/block/panel.json", face_model("test:block/pixel"))
            write_texture(root, "test", "block/pixel", (1, 2, 3, 255))

            with ResourcePackStack([root]) as stack:
                result = stack.resolve_block("test:panel")
                face = result.faces[0]
                self.assertEqual(face.points[0], (2 / 16, 4 / 16, 5 / 16))
                first = face.points[0]
                edge_a = tuple(face.points[1][i] - first[i] for i in range(3))
                edge_b = tuple(face.points[2][i] - first[i] for i in range(3))
                normal_y = edge_a[2] * edge_b[0] - edge_a[0] * edge_b[2]
                self.assertLess(normal_y, 0)

    def test_element_rotation_and_rescale_change_geometry(self) -> None:
        plain = ResourcePackStack._rotate_element_point((0.25, 0.5, 0.0), {"axis": "y", "angle": 22.5})
        rescaled = ResourcePackStack._rotate_element_point(
            (0.25, 0.5, 0.0), {"axis": "y", "angle": 22.5, "rescale": True},
        )
        self.assertNotEqual(plain, rescaled)
        self.assertLess(rescaled[0], plain[0])

    def test_zip_source_overrides_earlier_directory_and_reports_used_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "base"
            archive_path = Path(temp) / "later.jar"
            write_json(base, "assets/test/blockstates/tile.json", {"variants": {"": {"model": "test:block/tile"}}})
            write_json(base, "assets/test/models/block/tile.json", face_model("test:block/pixel"))
            write_texture(base, "test", "block/pixel", (255, 0, 0, 255))
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("assets/test/textures/block/pixel.png", _png_bytes((0, 0, 255, 255)))

            with ResourcePackStack([base, archive_path]) as stack:
                result = stack.resolve_block("test:tile")
                self.assertEqual(result.textures["test:block/pixel"].getpixel((0, 0)), (0, 0, 255, 255))
                report = stack.describe()
                self.assertEqual(report["sources"][1]["kind"], "archive")
                self.assertTrue(report["sources"][1]["content_fingerprint"])

    def test_missing_cycle_custom_loader_and_nonmatching_multipart_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(root, "assets/test/blockstates/cycle.json", {"variants": {"": {"model": "test:block/a"}}})
            write_json(root, "assets/test/models/block/a.json", {"parent": "test:block/b"})
            write_json(root, "assets/test/models/block/b.json", {"parent": "test:block/a"})
            write_json(root, "assets/test/blockstates/custom.json", {"variants": {"": {"model": "test:block/custom"}}})
            write_json(root, "assets/test/models/block/custom.json", {"loader": "example:custom", "elements": []})
            write_json(root, "assets/test/blockstates/never.json", {"multipart": [
                {"when": {"powered": "true"}, "apply": {"model": "test:block/custom"}},
            ]})

            with ResourcePackStack([root]) as stack:
                cycle = stack.resolve_block("test:cycle")
                custom = stack.resolve_block("test:custom")
                never = stack.resolve_block("test:never", {"powered": "false"})
                self.assertEqual(cycle.status, "fallback")
                self.assertTrue(any("model_parent_cycle" in reason for reason in cycle.reasons))
                self.assertEqual(custom.status, "fallback")
                self.assertTrue(any("unsupported_custom_loader" in reason for reason in custom.reasons))
                self.assertEqual(never.status, "fallback")
                self.assertIn("no_matching_multipart_parts", never.reasons)

    def test_deep_json_and_multipart_conditions_return_fallback_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            blockstates = root / "assets/test/blockstates"
            blockstates.mkdir(parents=True)
            deep_json = '{"variants":{"":{"model":"test:block/empty"}},"ignored":' + "[" * 1800 + "0" + "]" * 1800 + "}"
            (blockstates / "deep_json.json").write_text(deep_json, encoding="utf-8")
            max_depth = _MAX_JSON_NESTING - 1  # root object accounts for one level
            accepted_json = (
                '{"variants":{"":{"model":"test:block/empty"}},"ignored":'
                + "[" * max_depth + "0" + "]" * max_depth
                + ',"symbols":"' + "[]{}" * 100 + '"}'
            )
            (blockstates / "at_limit.json").write_text(accepted_json, encoding="utf-8")
            beyond_limit = max_depth + 1
            rejected_json = (
                '{"variants":{"":{"model":"test:block/empty"}},"ignored":'
                + "[" * beyond_limit + "0" + "]" * beyond_limit + "}"
            )
            (blockstates / "over_limit.json").write_text(rejected_json, encoding="utf-8")
            write_json(root, "assets/test/models/block/empty.json", {"elements": []})
            nested_condition: dict = {"powered": "true"}
            for _ in range(70):
                nested_condition = {"AND": [nested_condition]}
            write_json(root, "assets/test/blockstates/deep_condition.json", {
                "multipart": [{"when": nested_condition, "apply": {"model": "test:block/empty"}}],
            })

            with ResourcePackStack([root]) as stack:
                deep_json_result = stack.resolve_block("test:deep_json")
                self.assertEqual(deep_json_result.status, "fallback")
                self.assertTrue(any(reason.startswith("invalid_json_depth_limit:") for reason in deep_json_result.reasons))
                at_limit_result = stack.resolve_block("test:at_limit")
                self.assertEqual(at_limit_result.status, "resolved")
                over_limit_result = stack.resolve_block("test:over_limit")
                self.assertEqual(over_limit_result.status, "fallback")
                self.assertTrue(any(reason.startswith("invalid_json_depth_limit:") for reason in over_limit_result.reasons))
                deep_condition_result = stack.resolve_block("test:deep_condition", {"powered": "true"})
                self.assertEqual(deep_condition_result.status, "fallback")
                self.assertTrue(any(reason.startswith("invalid_json_depth_limit:") for reason in deep_condition_result.reasons))
                with self.assertRaisesRegex(ResourceError, "multipart_condition_depth_limit"):
                    ResourcePackStack._condition_matches(nested_condition, {"powered": "true"})

    def test_empty_model_is_resolved_empty_geometry_while_missing_assets_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(root, "assets/test/blockstates/empty.json", {"variants": {"": {"model": "test:block/empty"}}})
            write_json(root, "assets/test/models/block/empty.json", {"elements": []})
            write_json(root, "assets/test/blockstates/missing.json", {"variants": {"": {"model": "test:block/missing"}}})
            write_json(root, "assets/test/blockstates/runtime.json", {"variants": {"": {"model": "test:block/runtime"}}})
            write_json(root, "assets/test/models/block/runtime.json", {"textures": {"particle": "test:block/pixel"}})
            write_json(root, "assets/test/blockstates/plane.json", {"variants": {"": {"model": "test:block/plane"}}})
            write_json(root, "assets/test/models/block/plane.json", {
                "textures": {"side": "test:block/pixel"},
                "elements": [{"from": [0, 0, 8], "to": [16, 16, 8], "faces": {
                    "north": {"texture": "#side"}, "south": {"texture": "#side"},
                    "up": {"texture": "#side"},
                }}],
            })
            write_texture(root, "test", "block/pixel", (1, 2, 3, 255))

            with ResourcePackStack([root]) as stack:
                empty = stack.resolve_block("test:empty")
                missing = stack.resolve_block("test:missing")
                self.assertEqual(empty.status, "resolved")
                self.assertEqual(empty.faces, ())
                self.assertEqual(missing.status, "fallback")
                self.assertTrue(any("asset_missing" in reason for reason in missing.reasons))
                runtime = stack.resolve_block("test:runtime")
                self.assertEqual(runtime.status, "fallback")
                self.assertTrue(any("no_static_elements_runtime_geometry_unavailable" in reason for reason in runtime.reasons))
                plane = stack.resolve_block("test:plane")
                self.assertEqual(plane.status, "resolved")
                self.assertEqual(len(plane.faces), 2)
                normals_z = []
                for face in plane.faces:
                    edge_a = tuple(face.points[1][i] - face.points[0][i] for i in range(3))
                    edge_b = tuple(face.points[2][i] - face.points[0][i] for i in range(3))
                    normals_z.append(edge_a[0] * edge_b[1] - edge_a[1] * edge_b[0])
                self.assertLess(min(normals_z), 0)
                self.assertGreater(max(normals_z), 0)

    def test_animated_texture_uses_metadata_selected_first_frame_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(root, "assets/test/blockstates/animated.json", {"variants": {"": {"model": "test:block/animated"}}})
            write_json(root, "assets/test/models/block/animated.json", face_model("test:block/animated"))
            target = root / "assets/test/textures/block/animated.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            frames = Image.new("RGBA", (4, 8))
            for y in range(4):
                for x in range(4):
                    frames.putpixel((x, y), (255, 0, 0, 255))
                    frames.putpixel((x, y + 4), (0, 0, 255, 255))
            frames.save(target)
            (target.with_name("animated.png.mcmeta")).write_text(
                json.dumps({"animation": {"frames": [1, 0]}}), encoding="utf-8",
            )

            with ResourcePackStack([root]) as stack:
                result = stack.resolve_block("test:animated")
                self.assertEqual(result.status, "partial")
                self.assertTrue(any(reason.startswith("animated_texture_first_frame_only:test:block/animated") for reason in result.reasons))
                self.assertEqual(result.textures["test:block/animated"].size, (4, 4))
                self.assertEqual(result.textures["test:block/animated"].getpixel((0, 0)), (0, 0, 255, 255))


def _png_bytes(color: tuple[int, int, int, int]) -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    Image.new("RGBA", (4, 4), color).save(buffer, format="PNG")
    return buffer.getvalue()


def write_quadrant_texture(root: Path, namespace: str, path: str) -> None:
    target = root / "assets" / namespace / "textures" / f"{path}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (16, 16))
    colors = {
        "tl": (255, 0, 0, 255), "tr": (0, 255, 0, 255),
        "bl": (255, 255, 0, 255), "br": (0, 0, 255, 255),
    }
    for y in range(16):
        for x in range(16):
            key = ("t" if y < 8 else "b") + ("l" if x < 8 else "r")
            image.putpixel((x, y), colors[key])
    image.save(target)


if __name__ == "__main__":
    unittest.main()
