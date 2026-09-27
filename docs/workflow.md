# Offline analysis and mc-builder handoff

Minecraft Spatial Kit and [mc-builder](https://github.com/lucy-muyue/mc-builder) have separate jobs. This tool reads selected Java world files, indexes bounded areas, tracks what data is known, and creates local previews or blueprint files. It has no server connection and no construction command. Server-side installation and configuration are documented in mc-builder's [README](https://github.com/lucy-muyue/mc-builder/blob/main/README.md) and [AGENT_BUILDING_GUIDE](https://github.com/lucy-muyue/mc-builder/blob/main/AGENT_BUILDING_GUIDE.md); this package does not install or configure a server worker.

## Agent-led building design

An agent can create the design directly from the user's dimensions, layout, and material choices; no separate image-generation or building-generation service is required. Keep the design in a small JSON file using mc-builder's `dimension` and `operations` format. Use inclusive integer `from`/`to` bounds and exact block IDs/state strings. The optional `hollow: true` means “place only the cuboid shell”; it never means “replace the interior with air.” The format has no erase operation.

Run `mc-spatial blueprint --input ./building-plan.json --out ./building-review`. Inspect `views/iso.png` and `views/top.png`, then check the three section images and the manifest. If the entrance, roof, route, or support needs work, edit that same plan and rerun with `--overwrite` to replace the generated review files. Keep the plan's dimension and coordinate anchor consistent across revisions.

The Scene and images are review artifacts. If they are based on an indexed world, unknown coverage remains unknown and must be resolved before designing around an assumption of empty space. When the user has already authorized construction, carry the reviewed JSON to the existing mc-builder handoff below; the spatial tool does not ask for new approval or silently start work.

## Read and index

Point `mc-spatial index` at a local Java world folder and select one dimension and an inclusive region of interest. The source world is read-only. The index records section data and coverage; it does not expand the requested region by loading chunks. For custom-height dimensions, pass the actual inclusive vertical bounds with `--dimension-bounds MIN_Y MAX_Y`.

Use `export` to create a portable Scene JSON for a bounded region. A block query can return a known block or a reason that its value is unknown, such as a missing chunk, an unindexed position, or a changed source. Unknown is not air. Do not use a preview's blank region as evidence that it is buildable.

## Inspect a scene

Run `mc-spatial render --scene ... --out ...`. For modded static models, add `--resource-pack` paths in low-to-high priority order: matching client JAR, enabled mod JARs, then user overrides. A `--resource-config` JSON file with ordered `sources` and relative paths/globs is useful for larger installations; command-line packs are appended as higher-priority overrides. Resource inputs are read during rendering and do not refresh the world index.

Inspect the PNG views and section images, then read `render_manifest.json` for source, coverage, per-state resource status/reasons, rendering method, and features that were approximated or omitted. Check the asset paths actually used and any missing/unsupported model reasons. Files being present is not proof the correct asset won resolution or that the game appearance was fully reproduced. The renderer does not run Java custom renderers or mod code, and a static mesh does not include every block entity, entity, modded machine behavior, tint, animation, or fluid effect. Treat the manifest as part of the deliverable.

## Create a blueprint file

Run `mc-spatial blueprint --input ... --out ...` for an mc-builder operations file, or `mc-spatial demo` for a synthetic example. The exporter preserves dimension, integer world coordinates, block IDs and state properties. Its provenance says whether the input is a design or indexed scene. A design is not site evidence or construction permission. If source coverage is unknown, stop and resolve it before creating a plan that assumes that area is empty.

## Hand off to mc-builder

The handoff is manual and explicit:

1. Review the blueprint JSON and its Scene/visual preview.
2. Place the approved JSON in the local mc-builder `plans/` directory and call `preview_build`. Check the returned dimension, bounds, count, material summary, and write policy.
3. If the plan still matches the intended design, separately configure an exact approved area. Use `prepare_build` only when a live site snapshot and protection check are intended. It can reject unloaded positions, block entities, protected features, or occupied targets.
4. Start construction only through mc-builder's explicit `start_build`. Monitor its status and independently read back the completed target. Follow mc-builder's pause, cancel, and rollback rules if needed.

Minecraft Spatial Kit never copies the plan into mc-builder's private directory or invokes `preview_build`, `prepare_build`, `start_build`, or rollback automatically. The separation keeps offline design output reviewable before a user chooses any live-world action.

## Public examples and data handling

Public examples and continuous-integration fixtures must be synthetic. Keep world folders, databases, region files, level metadata, absolute paths, site coordinates, screenshots, and generated previews local unless their owner explicitly prepares a sanitized fixture. Do not commit a local world database or a rendered map as a convenient test input.
