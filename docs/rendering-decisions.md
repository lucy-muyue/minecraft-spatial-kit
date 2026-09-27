# Rendering choices and limits

Minecraft Spatial Kit turns a bounded Scene into a static GLB mesh, orthographic PNG views, and center sections. The output is for spatial review and handoff; it is not a resource-pack render or an editable in-game structure.

## Why this renderer

The first version uses local Python geometry and image libraries so a review bundle can be generated without launching Minecraft, Blender, a GPU window, or an external conversion service. GLB provides one portable static model, while the PNGs make the footprint and cross-sections easy to compare in any image viewer. Each output is reproducible from the same Scene and renderer options.

## Geometry fidelity

The renderer uses block IDs and properties where it has a supported geometric rule. The current shape path covers bottom/top/double slabs and common straight stairs, including their facing and upper/lower half. Other block shapes use a full-cube proxy when no rule is implemented. Stair corner shaping is approximate because the renderer does not resolve adjacent block-state model conditions.

The preview does not load vanilla or modded blockstate/model JSON, textures, resource packs, item/entity models, block-entity NBT, lighting, fluids, or gameplay behavior. A cube proxy can therefore differ visibly from the in-game model. The manifest records the fidelity level, state geometry supported, count and reasons for cube fallbacks, and the omitted features. Inspect these fields whenever a block's shape matters to a design.

## Alternatives considered

- **Exact game-model rendering:** It would require reproducing Minecraft's model resolution, blockstate rules, neighbor-dependent multipart logic, textures, and resource-pack overrides. Supporting only a few vanilla models would still look exact while silently being wrong for modded blocks. The project therefore reports geometry limitations instead of claiming game-render fidelity.
- **MiEx/USD export followed by Blender:** This can produce richer assets, but adds an external exporter, USD/Blender version coupling, conversion steps, and a heavier user setup. It remains a possible optional high-fidelity route; it is not part of this offline Python pipeline.
- **Voxel cubes alone:** This keeps geometry simple but makes common half-blocks and stairs misleading. The implementation models a small set of common shapes and logs full-cube fallback for the rest.

Keep Scene bounds small enough for the review task. The CLI's `--max-blocks` limit protects memory and rendering time; export a narrower range if a scene exceeds the limit.
