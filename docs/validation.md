# Validation notes

This page records scope and evidence without publishing a world name, path, coordinates, region hash, or map asset.

## Read-only cross-check

The index and exact block-query path was checked against a Java 1.20.1 world using one region chunk. A bounded Scene exported from that chunk contained 3,484 non-air blocks. Twelve sampled positions were then compared with an existing read-only server query interface; all twelve values and block states matched. The exported Scene also rendered successfully to a GLB, four PNG views, and three section images; those private world outputs are not included here.

The probes covered vanilla and modded block IDs, known air, a top slab, stair facing/half/waterlogged properties, and a modded state. The cross-check used only read operations and was limited to those samples; it does not imply support for all modded NBT, every block state, or exact in-game rendering.

## Synthetic automated checks

Unit tests build small synthetic Anvil region files in temporary directories. They cover negative coordinates, negative and high section Y values, block-state palette reads, known air versus missing chunks, dimension separation, custom height bounds, source-change detection, Scene export, and rendering. Blueprint checks cover inclusive cuboid and shell semantics, last-operation-wins behavior, state-preserving export, unknown-data rejection, and CLI export argument dispatch. The current suite has 25 passing tests; CI runs these fixtures without a real save.

The `mc-spatial` CLI was also run end to end against a generated one-chunk Anvil fixture: index, Scene export, known stone query, known-air query, unknown outside-coverage query, then GLB/view/section/manifest rendering all completed successfully. The fixture and its outputs were temporary and synthetic.

For rendering fidelity limits and the meaning of the manifest fields, see [the rendering guide](rendering.md) and [the rendering decisions](rendering-decisions.md).
