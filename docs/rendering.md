# Rendering a Scene

`mc-spatial render` writes a static GLB model, orthographic PNG views, center-cut PNGs, and a JSON manifest from a validated Scene v1 file. It runs locally with Python, NumPy, Pillow, and trimesh. PNG rasterization uses a CPU depth buffer; it does not require Blender, a display server, OpenGL, or a GPU.

```bash
mc-spatial render --scene ./scene.json --out ./scene-preview
```

The default views are `iso`, `top`, `north`, and `east`. Request the reverse sides and bottom with `--views iso top north east south west bottom`. Add `--no-sections` to skip section images, or set `--max-blocks` to change the safety limit (50,000 blocks by default). Rendering fails clearly if the Scene contains more blocks than the selected limit.

The output directory contains:

| File | Contents |
|---|---|
| `model.glb` | A true polygon mesh with simplified block geometry. Vertices are local to `world_origin` in the manifest. An all-air Scene produces a valid empty GLB scene. |
| `views/{view}.png` | Labelled `iso`, `top`, `north`, `east`, `south`, `west`, or `bottom` orthographic image for each requested view. |
| `sections/x_mid.png` | The center plane perpendicular to X, viewed from west (`-X`). |
| `sections/y_mid.png` | A horizontal center cut; north (`-Z`) is up. |
| `sections/z_mid.png` | The center plane perpendicular to Z, viewed from south (`+Z`). |
| `render_manifest.json` | Coordinates, output paths, scene provenance, shape counts, fallback reasons, and fidelity limits. |

All PNGs label the inclusive Scene bounds. Top view places north (`-Z`) at the top. East/west and north/south view subtitles state their camera side and horizontal orientation. The three section planes are the midpoints of the exported bounds; export a smaller or vertically shifted Scene to inspect a different cut. The section renderer uses half-open shape intervals at the cut plane, so a shape that only ends at the plane is not counted as crossing it.

Unknown chunks are never rendered as empty ground. The GLB includes orange chunk-column frames. PNG views show the frames, and top/section images add an orange hatch and label. If a bounded Scene contains no known non-air blocks, the PNGs say so while preserving any unknown markers and bounds.

## Reading fidelity fields

The manifest deliberately reports `fidelity.level` as `simplified_voxel_geometry` and sets `geometry_is_exact_minecraft_render_model` to `false`. It lists `supported_state_geometry`, `limitations`, and `textures_or_resource_packs_used`; the count fields include `fallback_blocks`, `fallback_reasons`, `shape_classes`, and rendered surface quads.

The current geometry rules model bottom/top/double slabs and straight stairs using their `type`, `facing`, `half`, and `shape` state. Inner/outer stairs and unknown stair states fall back to a full-cube proxy. Other blocks use full-cube proxies because Scene v1 does not include resource-pack models. That includes non-cube blocks such as doors, plants, fences, and fluids. Colors are deterministic material hints; Minecraft textures, UVs, biome tints, connected textures, animation, block entities, and mod resource packs are not loaded.

The PNG renderer shades exposed surfaces and uses per-pixel CPU depth testing for occlusion. It is an inspection preview, not an in-game screenshot or path-traced render. Faces shared by adjacent shapes are culled from the GLB to keep the mesh compact.

## Higher-fidelity adapters

For resource-pack-aware exports, MiEx documents Java world export to USD and a headless JSON-command interface (`-cli`, `-commandFile`/`-commandStdIn`, `loadWorld`, settings, and `export`). The project documents Java Edition worlds from 1.2.1 onward and mod/resource-pack support. Its output is USD, not GLB; a follow-on USD import and GLB export step would be required. Blender can run in the background and export glTF/GLB, with Cycles CPU rendering available, but that toolchain adds a large optional install and is not part of this renderer. Blender's handling of MiEx's default MaterialX materials has caveats; see the [MiEx/Blender note](https://github.com/BramStoutProductions/MiEx/discussions/6) and its [pipeline guide](https://github.com/BramStoutProductions/MiEx/wiki/04.-Customising-MiEx-and-Pipeline-Integration). These adapters have not been tested by this package.

PrismarineJS provides chunk readers and block collision-shape data, but collision boxes are physics shapes rather than resource-pack visual models. They can inform a later optional proxy-shape adapter, with that same distinction made explicit. See the [collision-shapes documentation](https://github.com/PrismarineJS/minecraft-data/blob/master/doc/blockCollisionShapes.md) and [Prismarine block API](https://github.com/PrismarineJS/prismarine-block).

Minecraft assets are not bundled. For public releases, keep client JARs and textures out of the repository and verify rights for any user-provided resource packs; Mojang's [Usage Guidelines](https://www.minecraft.net/en-us/usage-guidelines) prohibit redistributing game files.
