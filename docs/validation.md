# Validation notes

This page records scope and evidence without publishing a world name, path, coordinates, region hash, or map asset.

## Read-only cross-check

The index and exact block-query path was checked against a Java 1.20.1 world using one region chunk. A bounded Scene exported from that chunk contained 3,484 non-air blocks. Twelve sampled positions were then compared with an existing read-only server query interface; all twelve values and block states matched. The exported Scene also rendered successfully to a GLB, four PNG views, and three section images; those private world outputs are not included here.

The probes covered vanilla and modded block IDs, known air, a top slab, stair facing/half/waterlogged properties, and a modded state. The cross-check used only read operations and was limited to those samples; it does not imply support for all modded NBT, every block state, or exact in-game rendering.

## Synthetic automated checks

Unit tests build small synthetic Anvil region files in temporary directories. They cover negative coordinates, negative and high section Y values, block-state palette reads, known air versus missing chunks, dimension separation, custom height bounds, source-change detection, Scene export, and rendering. Resource tests use only authored fixtures to cover variants, multipart conditions, model and texture inheritance, orientation, flat cross planes, archive overrides, rendered textures, fallback reporting, and CLI resource-option dispatch. Blueprint checks cover inclusive cuboid and shell semantics, last-operation-wins behavior, state-preserving export, unknown-data rejection, and CLI argument dispatch. The current suite has 41 passing tests; CI uses synthetic inputs without a real save or Minecraft assets.

## Static resource-pack smoke checks

A separate read-only CLI smoke used a matching Minecraft Java 1.20.1 client JAR, the local mod JARs, and local override asset directories. The bounded synthetic Scene probed one unlit east-facing furnace, a poppy cross model, an unconnected oak fence multipart, bottom/east and top/north straight oak stairs, one BetterEnd lantern, and one Rocks variant. The furnace's front quad resolved to the east face. Six states reported `resolved`; the rotated top/north stairs reported `partial` with `uvlock_not_applied`. None of these selected probes used proxy geometry after the flat-plane fix.

This is evidence for those specific states and the supplied local source stack only. It does not indicate all resources from either mod or all block states are supported. The resolver reads assets on demand; manifest source fingerprints cover assets actually read rather than scanning or certifying whole packs. No world data or real mod textures are included in this repository, and the smoke output remains local.

The `mc-spatial` CLI was also run end to end against a generated one-chunk Anvil fixture: index, Scene export, known stone query, known-air query, unknown outside-coverage query, then GLB/view/section/manifest rendering all completed successfully. The fixture and its outputs were temporary and synthetic.

For rendering fidelity limits and the meaning of the manifest fields, see [the rendering guide](rendering.md) and [the rendering decisions](rendering-decisions.md).
