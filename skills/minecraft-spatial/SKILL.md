---
name: minecraft-spatial
description: Inspect local Minecraft Java world regions, create spatial previews, or export mc-builder-compatible blueprint files for human review.
---

# Minecraft Spatial

Use the `mc-spatial` CLI for offline region indexing, block queries, bounded scene rendering, synthetic demos, and mc-builder blueprint export. First locate the repository or clone it with `git clone https://github.com/lucy-muyue/minecraft-spatial-kit.git`, then run package commands from its root; if this skill was copied separately, do not assume the current directory is the checkout. Install with `python -m pip install -e .`. See the [project README](https://github.com/lucy-muyue/minecraft-spatial-kit/blob/main/README.md) and [workflow guide](https://github.com/lucy-muyue/minecraft-spatial-kit/blob/main/docs/workflow.md) for current options and output details.

Useful commands:

```bash
mc-spatial --help
mc-spatial demo --out ./pavilion-preview
mc-spatial index --world /path/to/world --db ./world-index.sqlite \
  --dimension minecraft:overworld --min -16 48 -16 --max 15 96 15
mc-spatial export --db ./world-index.sqlite --dimension minecraft:overworld \
  --min -16 48 -16 --max 15 96 15 --out ./scene.json
mc-spatial query --db ./world-index.sqlite --dimension minecraft:overworld --pos 0 64 0
mc-spatial render --scene ./scene.json --out ./scene-preview
mc-spatial blueprint --input ./plan.json --out ./plan-review
```

After rendering, inspect the PNG views and `render_manifest.json`; check source coverage, unknown chunks, and any simplified or missing features. A missing or unindexed position is unknown, never air. Keep world databases, coordinates, screenshots, and generated assets local unless a sanitized synthetic fixture is intentionally prepared.

Blueprint export is a design artifact. Review its JSON and images, then use the user's existing mc-builder workflow for its separate local `preview_build` validation. Offline export does not approve a construction site or authorize writing. Do not trigger `prepare_build`, `start_build`, or rollback unless the active user request already authorizes those live-world actions; do not add a new approval step when that authorization is already explicit.
