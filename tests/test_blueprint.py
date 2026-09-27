import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from minecraft_spatial.blueprint import (
    BlueprintError,
    expand_operations,
    plan_to_scene,
    scene_to_plan,
)
from minecraft_spatial.cli import main, synthetic_pavilion_plan


class BlueprintTests(unittest.TestCase):
    def test_plan_round_trip_preserves_voxels_and_block_states(self):
        plan = synthetic_pavilion_plan()
        scene = plan_to_scene(plan)
        exported = scene_to_plan(scene)

        self.assertEqual(
            expand_operations(plan["operations"]),
            expand_operations(exported["operations"]),
        )
        by_name = {entry["name"]: entry["properties"] for entry in scene["palette"]}
        self.assertEqual(by_name["minecraft:oak_slab"]["type"], "bottom")
        self.assertEqual(by_name["minecraft:stone_brick_stairs"]["shape"], "straight")
        self.assertEqual(scene["kind"], "blueprint")
        self.assertIsNone(scene["provenance"]["source_snapshot_id"])

    def test_hollow_is_shell_and_last_operation_wins(self):
        plan = {
            "dimension": "minecraft:overworld",
            "operations": [
                {"from": [0, 0, 0], "to": [2, 2, 2], "block": "minecraft:stone", "hollow": True},
                {"from": [1, 1, 1], "block": "minecraft:glass"},
            ],
        }
        placements = expand_operations(plan["operations"])
        self.assertEqual(len(placements), 27)
        self.assertEqual(placements[(1, 1, 1)][0], "minecraft:glass")

    def test_air_targets_are_rejected_instead_of_clearing(self):
        with self.assertRaisesRegex(BlueprintError, "do not clear"):
            expand_operations([
                {"from": [0, 0, 0], "block": "minecraft:air"},
            ])

    def test_unknown_coverage_cannot_be_exported(self):
        plan = synthetic_pavilion_plan()
        plan["unknown_chunks"] = [{"x": 1, "z": -2, "reason": "absent_chunk"}]
        with self.assertRaisesRegex(BlueprintError, "unknown chunks"):
            plan_to_scene(plan)

    def test_invalid_replacement_list_is_a_blueprint_error(self):
        plan = synthetic_pavilion_plan()
        plan["replace_blocks"] = [{}]
        with self.assertRaises(BlueprintError):
            plan_to_scene(plan)

    def test_public_example_matches_synthetic_demo(self):
        example_path = Path(__file__).parents[1] / "examples" / "pavilion.json"
        self.assertEqual(json.loads(example_path.read_text(encoding="utf-8")), synthetic_pavilion_plan())

    def test_readme_export_command_parses_and_dispatches(self):
        scene = plan_to_scene({
            "dimension": "minecraft:overworld",
            "operations": [{"from": [-32, 48, -32], "block": "minecraft:stone"}],
        })
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "scene.json"
            stdout = io.StringIO()
            with patch("minecraft_spatial.index.export_scene", return_value=scene) as export_scene, \
                    patch("minecraft_spatial.cli.save_scene", return_value=output) as save_scene, \
                    redirect_stdout(stdout):
                code = main([
                    "export", "--db", str(Path(tmp) / "world-index.sqlite"),
                    "--dimension", "minecraft:overworld",
                    "--min", "-32", "48", "-32",
                    "--max", "31", "96", "31",
                    "--out", str(output),
                ])
            self.assertEqual(code, 0)
            export_scene.assert_called_once_with(
                Path(tmp) / "world-index.sqlite",
                "minecraft:overworld",
                [-32, 48, -32],
                [31, 96, 31],
            )
            save_scene.assert_called_once_with(scene, output)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["dimension"], "minecraft:overworld")
            self.assertEqual(result["blocks"], 1)


if __name__ == "__main__":
    unittest.main()
