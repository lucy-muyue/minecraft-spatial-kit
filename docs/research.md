# Research and design choices

Research snapshot: **2026-09-27**. This note compares bounded Java-world indexing and spatial-preview routes for Minecraft Spatial Kit. Upstream version and licence statements below apply only to the named release, branch, or repository state checked on that date; they are not guarantees about later versions. The comparison is based on official project pages and documentation, not a benchmark of every candidate.

## Recommended minimum route

Keep the current Python path small and explicit:

1. Use `nbtlib==2.0.4` to decode chunk NBT, a narrow read-only Anvil reader for region records and block-state palettes, and SQLite for the refreshable local index.
2. Export bounded, dimension-aware Scene v1 JSON. Keep absent, corrupt, unsupported, or stale-after-refresh coverage unknown; never infer air from missing data.
3. Render that Scene with the project's offline `trimesh`/NumPy/Pillow path. Without resources, it uses small built-in shape rules and cube proxies. With explicitly supplied client/mod/override resources, it can resolve a supported subset of static JSON blockstates, models, and textures; the manifest names status, reasons, approximations, and fallbacks.

This route keeps the data path local and read-only, avoids requiring Java, Node, Blender, or a GPU, and reuses one Scene contract for indexing, query, preview, and blueprint design. It is the minimum route for the current bounded-inspection use case, not a claim that it renders every Minecraft model exactly.

## Indexing options

| Option | Evidence checked | Assessment for this project |
|---|---|---|
| `nbtlib` + a narrow reader | PyPI lists `nbtlib` 2.0.4 as MIT, Python `>=3.8,<4`; upstream describes the v2 line as unstable and recommends 1.12.1. | Good focused NBT decoder for this Python tool, but it does not read Anvil regions, palettes, chunk status, or source freshness. Keep 2.0.4 pinned and exercise real NBT serialization with fixtures before changing it. |
| `anvil-parser2` | PyPI 0.10.6 (2024-07-21) is listed as MIT, pure Python, and claims 1.18+ support. Its PyPI test note says through 1.19, while the checked `knirch` fork README claims 1.14.4–1.21. | A plausible alternative or reference. The versioned release and fork claim differ, and the package exposes writing too. Reconsider behind a backend boundary after a versioned compatibility suite. |
| Archived `matcool/anvil-parser` | The repository is archived; its README describes tests on 1.14.4/1.15.2 and leaves the 20w17a+ `BlockStates` format as a TODO. | Its checked evidence is too old for the intended modern block-state reader. |
| Amulet Core | PyPI's checked 1.9.46 page says Python `>=3.11`. The checked `2.0` branch metadata says Python `>=3.14`, and that branch's LICENSE requires a purchased licence. | Feature-rich world conversion is broader than bounded reads. Those specific Python/licence terms do not fit this Python 3.10 MIT MVP; recheck the exact release and terms before any future adoption. |
| PrismarineJS Anvil provider | The checked provider README lists Java PC 1.8–1.21 and read/write APIs. | A reasonable option if the tool moves to Node.js. It is not a Python library, and it does not provide the spatial mesh or PNG outputs needed here. |

The `nbtlib` choice has a deliberate trade-off: its upstream warning remains relevant even though this project pins 2.0.4. Pinning makes local installs repeatable; synthetic and real-format fixtures give this project a regression signal, but do not change upstream's stability status. Re-run those fixtures before any dependency upgrade. Peak memory and throughput have not been benchmarked across the alternatives, so no performance ranking is claimed.

## Rendering options

| Route | Strength | Limit for the current workflow |
|---|---|---|
| Project renderer (`trimesh`, NumPy, Pillow) | Local CPU generation of a mesh GLB, PNG views, and center sections from Scene JSON; optionally reads ordered client/mod/override resource roots for supported static blockstate/model JSON and textures. | JSON support is a documented subset, not a Minecraft runtime renderer. Java custom renderers, tinting, animation timing, neighbor context, fluids, and block entities remain outside its scope; unresolved states use proxies. See [the rendering guide](rendering.md) for the current interface and limits. |
| MiEx | Its repository state checked on this date documents Minecraft resource/model-aware world export to USD, including mod and resource-pack content, and labels its licence BSD-3-Clause. | No official GLB/OBJ output was found. The checked headless CLI is experimental, and the full USD conversion route below has not been tested in this project. |
| Mineways | Its checked v13.01 release documents current block updates through 26.2 and supports scripted/headless OBJ/MTL export. | The release does not establish 26.3 support; the project documents no Linux build (Wine is an option), and mod support is limited. It does not directly produce the required GLB and annotated sections. |
| Chunky | Java path tracing can produce high-quality PNGs from a saved scene and has a documented headless launcher. | It is an image renderer rather than a mesh exporter; the checked project documentation says Java 17 and notes mod-block limits. It does not replace Scene-based GLB or section views. |
| BlueMap | The checked v5.24 release targets Minecraft 1.13.2–26.3 and Java 25; its standalone mode produces browsable 3D map data. | It is suited to a browser map, not a generic local GLB export. Initial resource preparation and its runtime make it a separate optional tool. |

The renderer's original v0.1 baseline used simplified slabs/stairs and cube proxies without loading textures or resource-pack models. The current optional resource path adds a supported static JSON subset while retaining the same explicit fidelity limits and fallback reporting; this is a bounded feature addition, not full mod compatibility. Chunky is a possible optional source of more polished PNGs; BlueMap is a distinct browser-map product. Neither should be a core dependency for bounded Scene previews.

## Possible higher-fidelity adapter: MiEx → USD → Blender

A future optional path could prepare MiEx and the target world's Minecraft, mod, and resource-pack assets locally, export USD through MiEx's pipeline commands, then use Blender background mode to export GLB and render PNGs with CPU Cycles. Blender documents background command-line use, glTF/GLB import/export, and CPU rendering. This could retain more game model detail than the current fallback renderer.

This complete MiEx → USD → Blender → GLB/PNG pipeline is **not tested here**. MiEx's headless interface is described as experimental. Its default MaterialX path may not transfer cleanly into Blender; the MiEx project discusses `UsdPreviewSurface` as an alternative, with losses such as biome tint and animated textures. Treat this as a separately installed adapter and validate it against synthetic scenes before relying on it. It does not change the current offline Python default.

The public project should continue to use synthetic examples and should not bundle Minecraft client assets. See Mojang's [Usage Guidelines](https://www.minecraft.net/en-us/usage-guidelines) before distributing game-derived assets.

## Practical implications

- Refresh the index explicitly when starting a new area or after source changes are discovered; queries describe the last refreshed snapshot.
- Start with a small inclusive XYZ box, inspect the overview and center sections, then export a tighter box or different Y layer and query exact positions.
- Keep the dimension attached to every position. Unknown chunks and positions outside indexed vertical coverage stay unknown.
- Refresh stores chunk sections compactly; export expands only its requested bounds. This is a design choice, not a published memory or throughput benchmark.

## Compatibility boundary

The reader currently targets modern root/section block-state data and requires full chunks. Compatibility validation includes Java 1.20.1; that does not certify every later release or modded storage extension. Unsupported compression, external chunk records, malformed palettes, and incomplete chunks remain unknown. The index checks source hashes on explicit refresh and provides per-region consistency, not an atomic snapshot across a changing world. Scene v1 carries block states, not entities, block entities, biomes, lighting, or a guarantee that supported static resource models reproduce the game. The renderer resolves only resources explicitly supplied at render time; it does not scan an installation, execute mod code, or infer loader precedence. These limits are part of the evidence behind the minimal route and should be revisited when a new supported version or adapter is added.

## Primary sources

- Indexing: [`nbtlib` on PyPI](https://pypi.org/project/nbtlib/) and [upstream README](https://github.com/vberlier/nbtlib); [`anvil-parser2` release metadata](https://pypi.org/project/anvil-parser2/) and [source](https://github.com/0xTiger/anvil-parser2); [archived `matcool/anvil-parser`](https://github.com/matcool/anvil-parser).
- Version- and branch-scoped comparison: [Amulet Core 1.9.46 on PyPI](https://pypi.org/project/amulet-core/1.9.46/), [checked 2.0 branch metadata](https://github.com/Amulet-Team/Amulet-Core/blob/2.0/pyproject.toml), and [that branch's LICENSE](https://github.com/Amulet-Team/Amulet-Core/blob/2.0/LICENSE); [Prismarine Anvil provider](https://github.com/PrismarineJS/prismarine-provider-anvil).
- Rendering: [MiEx repository](https://github.com/BramStoutProductions/MiEx), [pipeline/headless documentation](https://github.com/BramStoutProductions/MiEx/wiki/04.-Customising-MiEx-and-Pipeline-Integration), and [Blender command-line manual](https://docs.blender.org/manual/en/latest/advanced/command_line/index.html), [GLB export](https://docs.blender.org/manual/en/3.6/addons/import_export/scene_gltf2.html), and [Cycles rendering](https://docs.blender.org/manual/en/5.0/render/cycles/render_settings/index.html).
- Other renderers: [Mineways releases](https://github.com/erich666/Mineways/releases) and [scripting guide](https://erich666.github.io/Mineways/scripting.html); [Chunky headless guide](https://chunky-dev.github.io/docs/reference/user_interface/chunky_launcher/headless/); [BlueMap releases](https://github.com/BlueMap-Minecraft/BlueMap/releases).
