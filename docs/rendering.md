# Rendering a Scene

`mc-spatial render` writes a static GLB model, orthographic PNG views, center-cut PNGs, and a JSON manifest from a validated Scene v1 file. It runs locally with Python, NumPy, Pillow, and trimesh. PNG rasterization uses a CPU depth buffer; it does not require Blender, a display server, OpenGL, or a GPU.

```bash
mc-spatial render --scene ./scene.json --out ./scene-preview
```

The default views are `iso`, `top`, `north`, and `east`. Request the reverse sides and bottom with `--views iso top north east south west bottom`. Add `--no-sections` to skip section images, or set `--max-blocks` to change the safety limit (50,000 blocks by default). Rendering fails clearly if the Scene contains more blocks than the selected limit.

To resolve static block models, pass resource sources in low-to-high priority order: the matching Minecraft client JAR, enabled mod JARs, then user overrides. `--resource-pack` is repeatable and accepts a directory containing `assets/`, a ZIP, or a JAR:

```bash
mc-spatial render --scene ./scene.json --out ./scene-preview \
  --resource-pack /path/to/1.20.1/client.jar \
  --resource-pack /path/to/mods/mod-a.jar \
  --resource-pack /path/to/resourcepacks/overrides.zip
```

For many mods, `--resource-config ./resource-sources.json` reads a JSON object with a `sources` array. Each string is a file, directory, or glob; relative paths use the config file's directory, and glob matches are sorted by path. Explicit `--resource-pack` values are appended after configured sources, at higher priority. The renderer uses the order supplied; it does not infer the mod loader's effective precedence for duplicate resources. For example:

```json
{"sources": ["client.jar", "mods/*.jar", "resourcepacks/overrides.zip"]}
```

The same resource options are available on `blueprint` and `demo`, because both render their generated Scene. Sources are opened read-only during rendering. They are cached in memory for that rendering run and do not cause the world index to be refreshed. Client/mod JARs are treated as ZIP containers; their code is never executed. Minecraft assets are not bundled with this project.

The output directory contains:

| File | Contents |
|---|---|
| `model.glb` | A polygon mesh with proxy geometry, or supported static resource-pack model faces with embedded PNG textures when sources are supplied. Vertices are local to `world_origin` in the manifest. An all-air Scene produces a valid empty GLB scene. |
| `views/{view}.png` | Labelled `iso`, `top`, `north`, `east`, `south`, `west`, or `bottom` orthographic image for each requested view. |
| `sections/x_mid.png` | The center plane perpendicular to X, viewed from west (`-X`). |
| `sections/y_mid.png` | A horizontal center cut; north (`-Z`) is up. |
| `sections/z_mid.png` | The center plane perpendicular to Z, viewed from south (`+Z`). |
| `render_manifest.json` | Coordinates, output paths, scene provenance, shape counts, fallback reasons, and fidelity limits. |

All PNGs label the inclusive Scene bounds. Top view places north (`-Z`) at the top. East/west and north/south view subtitles state their camera side and horizontal orientation. The three section planes are the midpoints of the exported bounds; export a smaller or vertically shifted Scene to inspect a different cut. The section renderer uses half-open shape intervals at the cut plane, so a shape that only ends at the plane is not counted as crossing it.

Unknown chunks are never rendered as empty ground. The GLB includes orange chunk-column frames. PNG views show the frames, and top/section images add an orange hatch and label. If a bounded Scene contains no known non-air blocks, the PNGs say so while preserving any unknown markers and bounds.

## Reading fidelity fields

The manifest always sets `geometry_is_exact_minecraft_render_model` to `false`. Without resource sources, `fidelity.level` is `simplified_voxel_geometry`; with resources it is `resource_pack_static_json_geometry`. In resource mode, inspect `resource_sources` and `resource_states` alongside `counts.resource_statuses` and `counts.fallback_reasons`. Per-state records include resolution status, reasons, model IDs, resolved face counts, and proxy counts. `resolved` means the static JSON path was supported; it does not claim full in-game fidelity. A resource file existing in a pack does not mean it was selected, parsed, or represented exactly.

Without resource sources, the current geometry rules model bottom/top/double slabs and straight stairs using their `type`, `facing`, `half`, and `shape` state; other blocks use full-cube proxies. With resources, supported static JSON blockstate variants and multipart entries can select models; model parents, texture-variable inheritance, element geometry, and PNG textures are resolved from the ordered sources. Inner/outer stairs and unknown stair states still use a proxy when no matching resource model resolves.

The resolver does not run Java custom renderers or mod code. Biome/state tint, connected and neighbor-dependent behavior, game lighting, display transforms, and block-entity rendering are not reproduced. Animated textures use the metadata-selected first frame and mark the state `partial`; animation playback is not reproduced. Fluid flow and shader effects are not modeled. `uvlock` is not applied and is reported as a partial-resolution reason. Weighted model selection is stable for a Scene position but does not use Minecraft's world-seeded random stream. Read each state's reasons to see unsupported or missing pieces; a partially resolved model may still use proxy geometry.

Texture transparency uses a binary alpha cutout at 0.5 in PNG previews and GLB materials; blended or translucent game rendering is not simulated. Model faces and texture reads/pixel totals have implementation safety limits; see `minecraft_spatial/resources.py` and `minecraft_spatial/render.py`. When a model or texture limit is reached for a state, the manifest records the reason and uses proxy geometry for that state.

The proxy renderer shades exposed surfaces; resource mode samples texture colors. Both use per-pixel CPU depth testing for PNG occlusion. These are inspection previews, not in-game screenshots or path-traced renders. The proxy path culls shared box faces from the GLB; resource model faces are kept as authored, so adjacent resource geometry may include hidden faces.

## Higher-fidelity adapters

For resource-pack-aware exports, MiEx documents Java world export to USD and a headless JSON-command interface (`-cli`, `-commandFile`/`-commandStdIn`, `loadWorld`, settings, and `export`). The project documents Java Edition worlds from 1.2.1 onward and mod/resource-pack support. Its output is USD, not GLB; a follow-on USD import and GLB export step would be required. Blender can run in the background and export glTF/GLB, with Cycles CPU rendering available, but that toolchain adds a large optional install and is not part of this renderer. Blender's handling of MiEx's default MaterialX materials has caveats; see the [MiEx/Blender note](https://github.com/BramStoutProductions/MiEx/discussions/6) and its [pipeline guide](https://github.com/BramStoutProductions/MiEx/wiki/04.-Customising-MiEx-and-Pipeline-Integration). These adapters have not been tested by this package.

PrismarineJS provides chunk readers and block collision-shape data, but collision boxes are physics shapes rather than resource-pack visual models. They can inform a later optional proxy-shape adapter, with that same distinction made explicit. See the [collision-shapes documentation](https://github.com/PrismarineJS/minecraft-data/blob/master/doc/blockCollisionShapes.md) and [Prismarine block API](https://github.com/PrismarineJS/prismarine-block).

Minecraft assets are not bundled. For public releases, keep client JARs and textures out of the repository and verify rights for any user-provided resource packs; Mojang's [Usage Guidelines](https://www.minecraft.net/en-us/usage-guidelines) prohibit redistributing game files.
