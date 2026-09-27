"""Synthetic Anvil integration tests; no user world data is used."""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import tempfile
import unittest
import zlib
from pathlib import Path

import nbtlib
from nbtlib.tag import Byte, Compound, Int, List, LongArray, String

from minecraft_spatial.index import (
    WorldMismatchError,
    export_scene,
    query_block,
    refresh_index,
)
from minecraft_spatial.region import RegionReader, SourceChangedError


def _signed_long(value: int) -> int:
    return value if value < (1 << 63) else value - (1 << 64)


def _packed_air_section(stone_at: tuple[int, int, int], section_y: int) -> Compound:
    # Minecraft's section index is YZX: x is fastest, then z, then y.
    palette = List[Compound]([
        Compound({"Name": String("minecraft:stone")}),
        Compound({"Name": String("minecraft:air")}),
    ])
    words = [0x1111111111111111] * 256
    x, y, z = stone_at
    index = y * 256 + z * 16 + x
    words[index // 16] &= ~(0xF << ((index % 16) * 4))
    states = Compound({"palette": palette, "data": LongArray([_signed_long(v) for v in words])})
    return Compound({"Y": Byte(section_y), "block_states": states})


def _single_block_section(name: str, section_y: int) -> Compound:
    return Compound({
        "Y": Byte(section_y),
        "block_states": Compound({
            "palette": List[Compound]([Compound({"Name": String(name)})]),
        }),
    })


def _corrupt_two_entry_section(section_y: int) -> Compound:
    """Create 4-bit block data whose indices exceed its two-entry palette."""
    return Compound({
        "Y": Byte(section_y),
        "block_states": Compound({
            "palette": List[Compound]([
                Compound({"Name": String("minecraft:stone")}),
                Compound({"Name": String("minecraft:air")}),
            ]),
            "data": LongArray([_signed_long(0x2222222222222222)] * 256),
        }),
    })


def _chunk_bytes(cx: int, cz: int, sections: list[Compound], *, status: str = "minecraft:full") -> bytes:
    chunk = nbtlib.File({
        "DataVersion": Int(3465),
        "xPos": Int(cx),
        "zPos": Int(cz),
        "Status": String(status),
        "sections": List[Compound](sections),
    })
    stream = io.BytesIO()
    chunk.write(stream)
    return stream.getvalue()


def _write_region(
    path: Path,
    chunks: dict[tuple[int, int], tuple[bytes, int]],
) -> None:
    """Write tiny, valid region files with (decompressed NBT, codec) entries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = bytearray(8192)
    body = bytearray()
    sector = 2
    for (cx, cz), (raw_nbt, codec) in sorted(chunks.items()):
        if codec == 1:
            compressed = gzip.compress(raw_nbt)
        elif codec == 2:
            compressed = zlib.compress(raw_nbt)
        elif codec == 3:
            compressed = raw_nbt
        else:
            compressed = zlib.compress(raw_nbt)
        record = len(compressed + bytes([codec])).to_bytes(4, "big") + bytes([codec]) + compressed
        count = (len(record) + 4095) // 4096
        padded = record + bytes(count * 4096 - len(record))
        local_x, local_z = cx % 32, cz % 32
        slot = local_x + local_z * 32
        location_offset = slot * 4
        header[location_offset : location_offset + 3] = sector.to_bytes(3, "big")
        header[location_offset + 3] = count
        timestamp_offset = 4096 + slot * 4
        header[timestamp_offset : timestamp_offset + 4] = (123).to_bytes(4, "big")
        body.extend(padded)
        sector += count
    path.write_bytes(header + body)


class IndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.world = self.base / "world"
        self.world.mkdir()
        self.database = self.base / "index.sqlite"

    def _region(self, dimension: str = "minecraft:overworld") -> Path:
        if dimension == "minecraft:overworld":
            relative = Path("region")
        elif dimension == "minecraft:the_nether":
            relative = Path("DIM-1/region")
        elif dimension == "minecraft:the_end":
            relative = Path("DIM1/region")
        else:
            namespace, dim_path = dimension.split(":", 1)
            relative = Path("dimensions") / namespace / Path(*dim_path.split("/")) / "region"
        return self.world / relative / "r.-1.-1.mca"

    def _install_negative_chunk(self, block_name: str = "minecraft:stone") -> Path:
        path = self._region()
        nbt = _chunk_bytes(
            -1,
            -1,
            [
                _packed_air_section((15, 0, 0), -4)
                if block_name == "minecraft:stone"
                else _single_block_section(block_name, -4),
                _single_block_section("minecraft:lapis_block", 126),
            ],
        )
        _write_region(path, {(-1, -1): (nbt, 2)})
        return path

    def _refresh_low(self, *, force: bool = False) -> dict:
        return refresh_index(
            self.world,
            self.database,
            "minecraft:overworld",
            (-1, -64, -16),
            (-1, -64, -16),
            force=force,
            dimension_bounds=(-64, 2031),
        )

    def test_modern_negative_and_high_sections_query_and_scene_export(self) -> None:
        path = self._install_negative_chunk()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        result = self._refresh_low()
        after = hashlib.sha256(path.read_bytes()).hexdigest()

        self.assertEqual(result["known_chunks"], 1)
        self.assertEqual(before, after, "refresh must not modify the source region")
        low = query_block(self.database, "minecraft:overworld", (-1, -64, -16))
        self.assertEqual(low["status"], "known")
        self.assertEqual(low["block"]["name"], "minecraft:stone")
        known_air = query_block(self.database, "minecraft:overworld", (-1, -63, -16))
        self.assertEqual(known_air["status"], "known")
        self.assertEqual(known_air["block"]["name"], "minecraft:air")
        high = query_block(self.database, "minecraft:overworld", (-16, 2017, -16))
        self.assertEqual(high["status"], "known")
        self.assertEqual(high["block"]["name"], "minecraft:lapis_block")
        above = query_block(self.database, "minecraft:overworld", (-16, 2032, -16))
        self.assertEqual(above["status"], "unknown")
        self.assertEqual(above["reason"], "outside_vertical_coverage")

        scene = export_scene(
            self.database,
            "minecraft:overworld",
            (-1, -64, -16),
            (-1, -64, -16),
        )
        self.assertEqual(scene["kind"], "world")
        self.assertEqual(scene["bounds"]["min"], [-1, -64, -16])
        self.assertEqual(scene["blocks"], [{"pos": [-1, -64, -16], "palette": 0}])
        self.assertEqual(scene["palette"][0]["name"], "minecraft:stone")
        self.assertEqual(scene["unknown_chunks"], [])
        provenance = scene["provenance"]
        self.assertEqual(provenance["data_versions"], [3465])
        self.assertTrue(provenance["coverage_complete"])
        self.assertTrue(provenance["known_as_of_last_refresh"])
        self.assertIsNotNone(provenance["refreshed_utc"])
        self.assertEqual(len(provenance["source_regions"]), 1)
        self.assertEqual(len(provenance["source_regions"][0]["sha256"]), 64)
        self.assertEqual(len(provenance["snapshot_id"]), 64)

    def test_missing_chunk_is_unknown_and_deletion_clears_old_blocks(self) -> None:
        path = self._install_negative_chunk()
        self._refresh_low()
        self.assertEqual(query_block(self.database, "minecraft:overworld", (-1, -64, -16))["status"], "known")

        _write_region(path, {})
        refreshed = self._refresh_low()
        value = query_block(self.database, "minecraft:overworld", (-1, -64, -16))
        self.assertEqual(value["status"], "unknown")
        self.assertEqual(value["reason"], "absent_chunk")
        scene = export_scene(self.database, "minecraft:overworld", (-1, -64, -16), (-1, -64, -16))
        self.assertEqual(scene["blocks"], [])
        self.assertEqual(scene["unknown_chunks"], [{"x": -1, "z": -1, "reason": "absent_chunk"}])
        self.assertEqual(refreshed["unknown_chunks"], 1)

    def test_region_removal_and_corrupt_refresh_never_reuse_old_chunk(self) -> None:
        path = self._install_negative_chunk()
        self._refresh_low()
        path.write_bytes(b"bad region")
        self._refresh_low()
        corrupt = query_block(self.database, "minecraft:overworld", (-1, -64, -16))
        self.assertEqual(corrupt["status"], "unknown")
        self.assertEqual(corrupt["reason"], "corrupt_region")

        self._install_negative_chunk("minecraft:diamond_block")
        self._refresh_low()
        updated = query_block(self.database, "minecraft:overworld", (-1, -64, -16))
        self.assertEqual(updated["status"], "known")
        self.assertEqual(updated["block"]["name"], "minecraft:diamond_block")

        path.unlink()
        self._refresh_low()
        removed = query_block(self.database, "minecraft:overworld", (-1, -64, -16))
        self.assertEqual(removed["status"], "unknown")
        self.assertEqual(removed["reason"], "absent_region")

    def test_unknown_compression_replaces_previous_known_value(self) -> None:
        path = self._install_negative_chunk()
        self._refresh_low()
        record = _chunk_bytes(-1, -1, [_packed_air_section((15, 0, 0), -4)])
        # Put zlib bytes behind an unrecognized compression type.
        _write_region(path, {(-1, -1): (record, 99)})
        self._refresh_low()
        value = query_block(self.database, "minecraft:overworld", (-1, -64, -16))
        self.assertEqual(value["status"], "unknown")
        self.assertEqual(value["reason"], "unsupported_compression")

    def test_corrupt_low_section_makes_whole_column_unknown(self) -> None:
        path = self._region()
        nbt = _chunk_bytes(
            -1,
            -1,
            [
                _corrupt_two_entry_section(-4),
                _single_block_section("minecraft:lapis_block", 126),
            ],
        )
        _write_region(path, {(-1, -1): (nbt, 2)})

        refreshed = self._refresh_low()
        self.assertEqual(refreshed["known_chunks"], 0)
        self.assertEqual(refreshed["unknown_chunks"], 1)
        low = query_block(self.database, "minecraft:overworld", (-1, -64, -16))
        high = query_block(self.database, "minecraft:overworld", (-16, 2017, -16))
        self.assertEqual(low["status"], "unknown")
        self.assertEqual(low["reason"], "corrupt_section_data")
        self.assertEqual(high["status"], "unknown")
        self.assertEqual(high["reason"], "corrupt_section_data")

        scene = export_scene(
            self.database,
            "minecraft:overworld",
            (-16, -64, -16),
            (-1, 2031, -1),
        )
        self.assertEqual(scene["blocks"], [])
        self.assertEqual(scene["unknown_chunks"], [
            {"x": -1, "z": -1, "reason": "corrupt_section_data"},
        ])

    def test_dimensions_are_separate_and_custom_dimensions_need_height(self) -> None:
        nether = self._region("minecraft:the_nether")
        end = self._region("minecraft:the_end")
        _write_region(nether, {(-1, -1): (_chunk_bytes(-1, -1, [_single_block_section("minecraft:netherrack", 0)]), 1)})
        _write_region(end, {(-1, -1): (_chunk_bytes(-1, -1, [_single_block_section("minecraft:end_stone", 0)]), 3)})
        bounds = (-1, 0, -1), (-1, 0, -1)
        refresh_index(self.world, self.database, "minecraft:the_nether", *bounds)
        refresh_index(self.world, self.database, "minecraft:the_end", *bounds)
        self.assertEqual(query_block(self.database, "minecraft:the_nether", (-1, 0, -1))["block"]["name"], "minecraft:netherrack")
        self.assertEqual(query_block(self.database, "minecraft:the_end", (-1, 0, -1))["block"]["name"], "minecraft:end_stone")

        custom = self._region("mod:moon/base")
        _write_region(custom, {(-1, -1): (_chunk_bytes(-1, -1, [_single_block_section("mod:moonstone", 0)]), 2)})
        with self.assertRaises(ValueError):
            refresh_index(self.world, self.database, "mod:moon/base", *bounds)
        result = refresh_index(self.world, self.database, "mod:moon/base", *bounds, dimension_bounds=(0, 31))
        self.assertEqual(result["known_chunks"], 1)
        self.assertEqual(query_block(self.database, "mod:moon/base", (-1, 0, -1))["block"]["name"], "mod:moonstone")

    def test_database_is_bound_to_world_and_cannot_live_inside_source(self) -> None:
        self._install_negative_chunk()
        self._refresh_low()
        other = self.base / "other-world"
        other.mkdir()
        with self.assertRaises(WorldMismatchError):
            refresh_index(other, self.database, "minecraft:overworld", (-1, -64, -16), (-1, -64, -16))
        with self.assertRaises(ValueError):
            refresh_index(self.world, self.world / "index.sqlite", "minecraft:overworld", (-1, -64, -16), (-1, -64, -16))

    def test_region_reader_detects_path_replacement(self) -> None:
        path = self._install_negative_chunk()
        reader = RegionReader(path)
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(path.read_bytes())
        os.replace(replacement, path)
        try:
            with self.assertRaises(SourceChangedError):
                reader.verify_stable()
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()
