from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from minecraft_spatial.cli import main
from minecraft_spatial.scene import save_scene


def one_block_scene() -> dict:
    return {
        "schema_version": 1,
        "dimension": "minecraft:overworld",
        "bounds": {"min": [0, 0, 0], "max": [0, 0, 0]},
        "palette": [{"name": "spatial_demo:crystal_lantern", "properties": {"facing": "east"}}],
        "blocks": [{"pos": [0, 0, 0], "palette": 0}],
        "unknown_chunks": [],
        "provenance": {"source": "synthetic CLI test"},
        "kind": "world",
    }


class ResourceCliTests(unittest.TestCase):
    def _write_config(self, root: Path) -> tuple[Path, tuple[Path, ...]]:
        (root / "mods").mkdir(parents=True)
        sources = [
            root / "client.jar",
            root / "mods" / "alpha.jar",
            root / "mods" / "zeta.jar",
            root / "override_pack",
        ]
        sources[0].write_bytes(b"synthetic client archive placeholder")
        sources[1].write_bytes(b"synthetic mod archive placeholder")
        sources[2].write_bytes(b"synthetic mod archive placeholder")
        sources[3].mkdir()
        config = root / "resource-sources.json"
        config.write_text(
            json.dumps({"sources": ["client.jar", "mods/*.jar", "override_pack"]}),
            encoding="utf-8",
        )
        return config, tuple(sources)

    def test_render_passes_config_and_explicit_resource_packs_in_priority_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, configured = self._write_config(root)
            explicit = root / "extra_override.zip"
            explicit.write_bytes(b"synthetic override archive placeholder")
            scene_path = save_scene(one_block_scene(), root / "scene.json")
            output = root / "render"
            stdout = StringIO()
            with patch("minecraft_spatial.render.render_scene", return_value={"ok": True}) as render_scene, \
                    redirect_stdout(stdout):
                code = main([
                    "render", "--scene", str(scene_path), "--out", str(output),
                    "--resource-config", str(config), "--resource-pack", str(explicit),
                    "--views", "iso", "--no-sections",
                ])

            self.assertEqual(code, 0)
            render_scene.assert_called_once()
            self.assertEqual(
                render_scene.call_args.kwargs["resource_packs"],
                (*configured, explicit),
            )
            self.assertTrue(json.loads(stdout.getvalue())["ok"])

    def test_blueprint_passes_repeatable_resource_packs_to_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            low, high = root / "low", root / "high"
            low.mkdir()
            high.mkdir()
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps({
                "dimension": "minecraft:overworld",
                "operations": [{"from": [0, 0, 0], "block": "spatial_demo:crystal_lantern", "state": {"facing": "east"}}],
            }), encoding="utf-8")
            output = root / "blueprint"
            stdout = StringIO()
            with patch("minecraft_spatial.render.render_scene", return_value={"ok": True}) as render_scene, \
                    redirect_stdout(stdout):
                code = main([
                    "blueprint", "--input", str(plan_path), "--out", str(output),
                    "--resource-pack", str(low), "--resource-pack", str(high),
                ])

            self.assertEqual(code, 0)
            render_scene.assert_called_once()
            self.assertEqual(render_scene.call_args.kwargs["resource_packs"], (low, high))
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["blocks"], 1)
            self.assertTrue((output / "mc-builder-blueprint.json").is_file())

    def test_resource_config_rejects_missing_glob_before_render(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "resources.json"
            config.write_text(json.dumps({"sources": ["mods/*.jar"]}), encoding="utf-8")
            scene_path = save_scene(one_block_scene(), root / "scene.json")
            stdout = StringIO()
            with patch("minecraft_spatial.render.render_scene") as render_scene, \
                    redirect_stdout(stdout):
                code = main([
                    "render", "--scene", str(scene_path), "--out", str(root / "render"),
                    "--resource-config", str(config),
                ])

            self.assertEqual(code, 2)
            render_scene.assert_not_called()


if __name__ == "__main__":
    unittest.main()
