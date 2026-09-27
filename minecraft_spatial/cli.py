"""Command line interface for offline indexing, rendering, and blueprints."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from .blueprint import (
    BlueprintError,
    dump_plan,
    load_plan,
    plan_to_scene,
    scene_to_plan,
)
from .scene import load_scene, save_scene


def _bounds_args(parser: argparse.ArgumentParser, *, allow_center: bool = False) -> None:
    if allow_center:
        selector = parser.add_mutually_exclusive_group(required=True)
        selector.add_argument("--min", nargs=3, type=int, metavar=("X", "Y", "Z"))
        selector.add_argument("--center", nargs=3, type=int, metavar=("X", "Y", "Z"))
        parser.add_argument("--max", nargs=3, type=int, metavar=("X", "Y", "Z"))
        parser.add_argument("--radius", nargs=3, type=int, metavar=("RX", "RY", "RZ"))
    else:
        parser.add_argument("--min", nargs=3, type=int, required=True, metavar=("X", "Y", "Z"))
        parser.add_argument("--max", nargs=3, type=int, required=True, metavar=("X", "Y", "Z"))


def _resolve_bounds(args: argparse.Namespace) -> tuple[list[int], list[int]]:
    center = getattr(args, "center", None)
    radius = getattr(args, "radius", None)
    if center is not None:
        if radius is None or args.max is not None:
            raise ValueError("--center requires --radius and cannot be combined with --max")
        if any(value < 0 for value in radius):
            raise ValueError("radii must be non-negative")
        minimum = [coordinate - value for coordinate, value in zip(center, radius)]
        maximum = [coordinate + value for coordinate, value in zip(center, radius)]
    else:
        if args.max is None or radius is not None:
            raise ValueError("--min requires --max and cannot be combined with --radius")
        minimum, maximum = list(args.min), list(args.max)
    if any(lo > hi for lo, hi in zip(minimum, maximum)):
        raise ValueError("bounds are inclusive; each --min component must be <= --max")
    return minimum, maximum


def _add_dimension_y_bounds(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dimension-bounds",
        nargs=2,
        type=int,
        metavar=("MIN_Y", "MAX_Y"),
        help="inclusive vertical limits for a custom-height dimension",
    )


def _add_overwrite(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace files with the same generated names in the output location",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mc-spatial",
        description="Index, query, and render local Minecraft Java world data; export reviewed blueprint files.",
    )
    parser.add_argument("--version", action="version", version="mc-spatial 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)

    index_parser = commands.add_parser("index", help="index an inclusive region from a local Java world folder")
    index_parser.add_argument("--world", type=Path, required=True, help="local world folder (read-only source)")
    index_parser.add_argument("--db", type=Path, required=True, help="local SQLite index path")
    index_parser.add_argument("--dimension", required=True, help="namespaced dimension ID")
    _bounds_args(index_parser, allow_center=True)
    _add_dimension_y_bounds(index_parser)
    index_parser.add_argument("--force", action="store_true", help="re-read source files even when hashes match")

    export_parser = commands.add_parser("export", help="export a bounded index range as Scene JSON")
    export_parser.add_argument("--db", type=Path, required=True)
    export_parser.add_argument("--dimension", required=True)
    _bounds_args(export_parser)
    export_parser.add_argument("--out", type=Path, required=True, help="output Scene JSON path")
    _add_overwrite(export_parser)

    query_parser = commands.add_parser("query", help="query one exact block position from an index")
    query_parser.add_argument("--db", type=Path, required=True)
    query_parser.add_argument("--dimension", required=True)
    query_parser.add_argument("--pos", nargs=3, type=int, required=True, metavar=("X", "Y", "Z"))

    render_parser = commands.add_parser("render", help="render an existing Scene JSON to GLB, PNG views, and sections")
    render_parser.add_argument("--scene", type=Path, required=True)
    render_parser.add_argument("--out", type=Path, required=True, help="output directory")
    render_parser.add_argument("--max-blocks", type=int, default=50_000)
    render_parser.add_argument("--views", nargs="+", default=["iso", "top", "north", "east"])
    render_parser.add_argument("--no-sections", action="store_true")
    _add_overwrite(render_parser)

    blueprint_parser = commands.add_parser("blueprint", help="make a Scene, visual preview, and mc-builder plan from a plan JSON")
    blueprint_parser.add_argument("--input", type=Path, required=True, help="mc-builder operations JSON")
    blueprint_parser.add_argument("--out", type=Path, required=True, help="output directory")
    _add_overwrite(blueprint_parser)

    demo_parser = commands.add_parser("demo", help="generate a synthetic pavilion scene and visual review bundle")
    demo_parser.add_argument("--out", type=Path, required=True, help="output directory")
    _add_overwrite(demo_parser)
    return parser


def _ensure_output_dir(path: Path, *, overwrite: bool) -> Path:
    if path.exists() and not path.is_dir():
        raise ValueError(f"output path exists and is not a directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if not overwrite and any(path.iterdir()):
        raise FileExistsError(f"output directory is not empty: {path} (use --overwrite to replace generated files)")
    return path


def _ensure_output_file(path: Path, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"output file already exists: {path} (use --overwrite to replace it)")


def _print_result(result: Any) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def synthetic_pavilion_plan() -> dict[str, Any]:
    """A small, entirely fictional pavilion with an open entrance and stair."""
    stair_state = {
        "facing": "south",
        "half": "bottom",
        "shape": "straight",
        "waterlogged": "false",
    }
    slab_state = {"type": "bottom", "waterlogged": "false"}
    operations: list[dict[str, Any]] = [
        {"from": [-3, 0, -3], "to": [3, 0, 3], "block": "minecraft:stone"},
        {"from": [-3, 1, -3], "to": [-3, 4, -3], "block": "minecraft:white_concrete"},
        {"from": [3, 1, -3], "to": [3, 4, -3], "block": "minecraft:white_concrete"},
        {"from": [-3, 1, 3], "to": [-3, 4, 3], "block": "minecraft:white_concrete"},
        {"from": [3, 1, 3], "to": [3, 4, 3], "block": "minecraft:white_concrete"},
        {"from": [-2, 1, 3], "to": [2, 1, 3], "block": "minecraft:white_concrete"},
        {"from": [-3, 1, -2], "to": [-3, 1, 2], "block": "minecraft:white_concrete"},
        {"from": [3, 1, -2], "to": [3, 1, 2], "block": "minecraft:white_concrete"},
        {
            "from": [-4, 4, -4], "to": [4, 4, 4],
            "block": "minecraft:oak_slab", "state": slab_state,
        },
        {
            "from": [0, 0, -4], "block": "minecraft:stone_brick_stairs",
            "state": stair_state,
        },
    ]
    return {"dimension": "minecraft:overworld", "operations": operations}


def _write_render(scene: dict[str, Any], out: Path, *, overwrite: bool, max_blocks: int = 50_000) -> dict[str, Any]:
    from .render import render_scene

    _ensure_output_dir(out, overwrite=overwrite)
    return render_scene(scene, out, max_blocks=max_blocks)


def _run_blueprint(plan: dict[str, Any], source_sha256: str | None, out: Path, *, overwrite: bool) -> dict[str, Any]:
    from .scene import save_scene

    _ensure_output_dir(out, overwrite=overwrite)
    scene = plan_to_scene(plan, source_sha256=source_sha256)
    scene_path = save_scene(scene, out / "scene.json")
    builder_plan = scene_to_plan(scene)
    plan_path = dump_plan(builder_plan, out / "mc-builder-blueprint.json")
    rendered = _write_render(scene, out, overwrite=True)
    return {
        "scene": str(scene_path),
        "blueprint": str(plan_path),
        "dimension": scene["dimension"],
        "bounds": scene["bounds"],
        "blocks": len(scene["blocks"]),
        "materials": dict(sorted(Counter(
            scene["palette"][block["palette"]]["name"] for block in scene["blocks"]
        ).items())),
        "render": rendered,
        "construction_started": False,
    }


def _execute(args: argparse.Namespace) -> int:
    if args.command == "index":
        from .index import refresh_index

        minimum, maximum = _resolve_bounds(args)
        dimension_bounds = tuple(args.dimension_bounds) if args.dimension_bounds else None
        if dimension_bounds is not None and dimension_bounds[0] > dimension_bounds[1]:
            raise ValueError("--dimension-bounds MIN_Y must be <= MAX_Y")
        result = refresh_index(
            args.world,
            args.db,
            args.dimension,
            minimum,
            maximum,
            force=args.force,
            dimension_bounds=dimension_bounds,
        )
        _print_result(result)
        return 0

    if args.command == "export":
        from .index import export_scene

        _ensure_output_file(args.out, overwrite=args.overwrite)
        minimum, maximum = _resolve_bounds(args)
        scene = export_scene(args.db, args.dimension, minimum, maximum)
        path = save_scene(scene, args.out)
        _print_result({"scene": str(path), "dimension": scene["dimension"], "bounds": scene["bounds"], "blocks": len(scene["blocks"])})
        return 0

    if args.command == "query":
        from .index import query_block

        _print_result(query_block(args.db, args.dimension, list(args.pos)))
        return 0

    if args.command == "render":
        from .render import render_scene

        scene = load_scene(args.scene)
        _ensure_output_dir(args.out, overwrite=args.overwrite)
        result = render_scene(
            scene,
            args.out,
            max_blocks=args.max_blocks,
            views=tuple(args.views),
            sections=not args.no_sections,
        )
        _print_result(result)
        return 0

    if args.command == "blueprint":
        plan, source_sha256 = load_plan(args.input)
        _print_result(_run_blueprint(plan, source_sha256, args.out, overwrite=args.overwrite))
        return 0

    if args.command == "demo":
        plan = synthetic_pavilion_plan()
        _print_result(_run_blueprint(plan, None, args.out, overwrite=args.overwrite))
        return 0

    raise ValueError(f"unsupported command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _execute(args)
    except (BlueprintError, FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"mc-spatial: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
