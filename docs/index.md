# Indexing bounded areas

`mc-spatial` reads local Minecraft Java region files and writes a separate SQLite index. It never writes to the world directory or contacts a game server. The index is an offline cache: queries and exports describe what was known at the last successful refresh, not a live view of a running world.

## Pick a small region

Coordinates are global block coordinates. Bounds are inclusive, and a radius is applied independently on each axis. Start with the region needed for the current question; refresh is limited to 1,024 chunk columns by default.

```sh
mc-spatial index \
  --world /path/to/world \
  --db ./world-index.sqlite \
  --dimension minecraft:overworld \
  --center 1000 80 -1000 --radius 16 16 16
```

For explicit corners, pass `--min X Y Z --max X Y Z`. To inspect another area later, use the same database and refresh that area. A chunk column covers a 16×16 X/Z footprint; refresh stores complete block-state sections for each requested column so later exports can crop smaller inclusive bounds without rereading the source.

The standard Java vertical assumptions are `-64..319` for the Overworld and `0..255` for the Nether and End. These are recorded as assumptions in index metadata; the tool does not infer a custom modded height from `level.dat`. Pass the actual inclusive Y range for custom-height or modded worlds:

```sh
mc-spatial index \
  --world /path/to/world \
  --db ./world-index.sqlite \
  --dimension minecraft:overworld \
  --min 1000 64 -1000 --max 1015 95 -985 \
  --dimension-bounds CUSTOM_MIN_Y CUSTOM_MAX_Y
```

The coordinate values above are synthetic examples. Replace the custom-height placeholders with bounds from the intended world's dimension configuration.

The reader accepts the modern root/section layout with a DataVersion guard of 2860 or later, but the modern-region implementation has been verified against synthetic fixtures and Java 1.20.1. That is not a claim that every later format variant has been tested. The read path supports region compression types 1 (gzip), 2 (zlib), and 3 (uncompressed); LZ4/type 4 and external `.mcc` chunk records currently produce unknown. It indexes block states only: block entities, entities, biomes, ticks, and mod-specific payloads are not represented in Scene v1.

Each requested chunk is either known for its declared vertical coverage or unknown with a reason. A missing chunk, incomplete chunk, unsupported compression, corrupt data, unreadable file, or failed refresh is unknown. Unknown never means air. Within a known complete chunk, absent section records are air only inside the declared Y coverage. A query above or below that coverage returns unknown.

## Reader and dependency choice

The Python implementation uses [nbtlib](https://github.com/vberlier/nbtlib) for NBT object decoding, then a small bounded Anvil reader for region headers, chunk records, compression, source hashes, and the supported section palette layout. The upstream project advertises Python 3.8+ and MIT licensing, while its current README labels the 2.0 line unstable and suggests 1.12.1; the project pins 2.0.4 and covers it with real NBT round-trip fixtures. Recheck that upstream caveat before upgrading the pin. The limited region parser keeps version-sensitive behavior visible and fails closed when a chunk uses a layout or compression it does not recognize.

The archived [matcool/anvil-parser](https://github.com/matcool/anvil-parser) repository documents testing through Java 1.15.2 and leaves modern block-state data on its TODO list. Its [anvil-parser2 fork](https://github.com/knirch/anvil-parser2) says it additionally supports 1.18+, but the released PyPI 0.10.6 text says it was tested through 1.19 while the current fork README says 1.21; it is a broader parser that also exposes region writing. [Amulet Core](https://github.com/Amulet-Team/Amulet-Core) is a multi-format world reader/writer/converter, but its current release requires Python 3.11+ and the 2.0 branch sets a purchased licence. [PrismarineJS's Anvil provider](https://github.com/PrismarineJS/prismarine-provider-anvil) reports Java support through 1.21 and offers a more complete cross-version implementation, but it is Node.js and includes save methods. These projects are useful compatibility references or future backends; the MVP needs Python 3.10 and a narrow read-only boundary, which NBT decoding plus a limited tested file reader provides directly.

## Refresh after source changes

Refresh explicitly whenever new source data is needed. It hashes the relevant region file and reuses a requested chunk only when the file hash and recorded height assumptions match. If the region changed, the requested chunks are reread; cached chunks in that same changed region but outside the requested area become unknown until refreshed too. `--force` rereads requested chunks even when the file hash matches.

Deleting a region or removing a chunk slot during refresh clears its cached blocks and marks that area unknown. A corrupt or unsupported replacement also invalidates the previous known value. If a source file changes or is replaced while it is being read, the refresh fails closed and the affected region is unknown. If a process stops partway through refresh, rows still marked stale are returned as unknown on the next query.

Refresh hashes and verifies one region file at a time. Multiple region files do not form a single atomic snapshot of a world that is actively being written. For a coherent survey, stop the server or work from a filesystem snapshot/copy, then refresh the needed bounds. Query and export do not stat the world files, so their results remain “known as of last refresh” until a later refresh checks the source again.

The SQLite database is bound to the resolved world directory; use a new database for a different world. Keep it outside the source world directory. Chunk rows also keep dimensions separate, including `DIM-1`, `DIM1`, and `dimensions/<namespace>/<path>`.

## Export and query

Export a smaller inclusive area as a validated Scene v1 JSON document:

```sh
mc-spatial export \
  --db ./world-index.sqlite \
  --dimension minecraft:overworld \
  --min 1000 64 -1000 --max 1015 95 -985 \
  --out ./site-scene.json
```

The Scene retains global coordinates and the dimension. Its palette stores block IDs plus string state properties; `blocks` contains only known non-air blocks. `unknown_chunks` records chunk X/Z and a reason. Air is omitted only within known coverage. The provenance records the world identifier, region hashes, DataVersions, selected vertical-bounds source, refresh time, and snapshot identifier.

Query one global block coordinate without exporting a Scene:

```sh
mc-spatial query --db ./world-index.sqlite \
  --dimension minecraft:overworld --pos 1000 70 -1000
```

The response has `status: "known"` with a full block state, or `status: "unknown"` with a reason. Both carry refresh provenance when available. The query only reads the SQLite cache; run `index` again to check whether the world source has changed.

## Resource limits

The conservative defaults are 1,024 refreshed or exported chunk columns, 2,000,000 inclusive Scene volume cells, and 50,000 non-air blocks in an exported Scene. These bound common mistakes such as selecting a very large radius across a tall modded dimension. Python callers may deliberately tune the `MAX_REFRESH_CHUNKS`, `MAX_EXPORT_VOLUME`, and `MAX_EXPORT_BLOCKS` module constants for a controlled offline job. Raising them increases runtime and memory use; the reader keeps palette-packed section data compressed in SQLite and expands blocks only while exporting.

## Python API

The core API lives in `minecraft_spatial.index`:

```python
from minecraft_spatial.index import export_scene, query_block, refresh_index

refresh_index(
    world, database, "minecraft:overworld",
    minimum=(1000, 64, -1000), maximum=(1015, 95, -985),
    dimension_bounds=(-128, 511),  # synthetic custom-height example
)
block = query_block(database, "minecraft:overworld", (1000, 70, -1000))
scene = export_scene(database, "minecraft:overworld", (1000, 64, -1000), (1015, 95, -985))
```

`refresh_index(...)` returns counts, refresh time, the inclusive bounds, and a hash-derived snapshot ID. `query_block(...)` and `export_scene(...)` use only the last refresh. Scene file validation and JSON I/O are in `minecraft_spatial.scene`: `validate_scene(scene) -> dict`, `load_scene(path) -> dict`, and `save_scene(scene, path) -> Path`.
