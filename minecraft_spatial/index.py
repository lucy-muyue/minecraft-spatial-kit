"""Incremental, read-only spatial index for bounded Minecraft Java regions."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import numpy as np

from .region import (
    CorruptChunkError,
    RegionReadError,
    RegionReader,
    SourceChangedError,
    UnsupportedCompressionError,
)
from .scene import SceneError, validate_scene


# Deliberately bounded defaults. Callers can tune these module constants for a
# trusted offline batch after validating their memory and runtime envelope.
MAX_REFRESH_CHUNKS = 1024
MAX_EXPORT_VOLUME = 2_000_000
MAX_EXPORT_BLOCKS = 50_000
MIN_SUPPORTED_DATA_VERSION = 2860  # 1.18+: modern root/section layout

STANDARD_DIMENSION_BOUNDS = {
    "minecraft:overworld": (-64, 319),
    "minecraft:the_nether": (0, 255),
    "minecraft:the_end": (0, 255),
}
_DIMENSION_ID = re.compile(r"^([a-z0-9_.-]+):([a-z0-9_./-]+)$")
_AIR = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}


class IndexErrorBase(ValueError):
    """Base error for an invalid request or unreadable index database."""


class WorldMismatchError(IndexErrorBase):
    """A database already bound to a different world path was supplied."""


class IndexLimitError(IndexErrorBase):
    """A request exceeds the configured safe spatial limits."""


class ChunkIndexError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounds(minimum: Iterable[int], maximum: Iterable[int]) -> tuple[list[int], list[int]]:
    try:
        lo, hi = list(minimum), list(maximum)
    except TypeError as exc:
        raise ValueError("minimum and maximum must be three-coordinate iterables") from exc
    if len(lo) != 3 or len(hi) != 3 or any(type(v) is not int for v in lo + hi):
        raise ValueError("minimum and maximum must contain three integers")
    if any(lo[i] > hi[i] for i in range(3)):
        raise ValueError("minimum must not exceed maximum")
    return lo, hi


def _dimension_relative_path(dimension: str) -> PurePosixPath:
    if type(dimension) is not str:
        raise ValueError("dimension must be a namespaced string")
    match = _DIMENSION_ID.fullmatch(dimension)
    if not match:
        raise ValueError("dimension must be a namespaced identifier")
    namespace, path = match.groups()
    if any(part in (".", "..") for part in path.split("/")):
        raise ValueError("dimension path contains a traversal component")
    if dimension == "minecraft:overworld":
        return PurePosixPath(".")
    if dimension == "minecraft:the_nether":
        return PurePosixPath("DIM-1")
    if dimension == "minecraft:the_end":
        return PurePosixPath("DIM1")
    return PurePosixPath("dimensions", namespace, *path.split("/"))


def _dimension_bounds(
    dimension: str,
    requested: tuple[int, int] | None,
) -> tuple[int, int, str]:
    if requested is not None:
        if type(requested) not in (tuple, list) or len(requested) != 2 or any(type(v) is not int for v in requested):
            raise ValueError("dimension_bounds must be a pair of inclusive integer Y limits")
        lo, hi = requested
        source = "caller"
    else:
        if dimension not in STANDARD_DIMENSION_BOUNDS:
            raise ValueError("custom dimensions require explicit dimension_bounds=(min_y, max_y)")
        lo, hi = STANDARD_DIMENSION_BOUNDS[dimension]
        source = "standard_java_assumption"
    if lo > hi:
        raise ValueError("dimension_bounds minimum Y must not exceed maximum Y")
    return lo, hi, source


def _world_key(world: Path) -> str:
    return hashlib.sha256(os.fsencode(str(world))).hexdigest()


def _database_path(database: str | os.PathLike[str], world: Path, *, readonly: bool = False) -> tuple[sqlite3.Connection, Path | None]:
    name = os.fspath(database)
    if name == ":memory:":
        if readonly:
            raise FileNotFoundError("an in-memory database cannot be reopened for a query")
        return sqlite3.connect(":memory:"), None
    db_path = Path(name).expanduser().resolve()
    if db_path == world or world in db_path.parents:
        raise ValueError("index database must be outside the read-only world directory")
    if readonly:
        if not db_path.is_file():
            raise FileNotFoundError(f"index database does not exist: {db_path}")
        uri = db_path.as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True), db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(db_path), db_path


def _query_connection(database: str | os.PathLike[str]) -> sqlite3.Connection:
    name = os.fspath(database)
    if name == ":memory:":
        raise FileNotFoundError("an in-memory database cannot be reopened for a query")
    path = Path(name).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"index database does not exist: {path}")
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chunk_index (
            dimension TEXT NOT NULL,
            cx INTEGER NOT NULL,
            cz INTEGER NOT NULL,
            region_x INTEGER NOT NULL,
            region_z INTEGER NOT NULL,
            state TEXT NOT NULL,
            reason TEXT,
            status TEXT,
            data_version INTEGER,
            chunk_sha256 TEXT,
            source_region_path TEXT,
            source_region_sha256 TEXT,
            source_region_stat TEXT,
            covered_min_y INTEGER,
            covered_max_y INTEGER,
            bounds_source TEXT,
            refreshed_utc TEXT,
            PRIMARY KEY (dimension, cx, cz)
        );
        CREATE INDEX IF NOT EXISTS chunk_index_region
            ON chunk_index(dimension, region_x, region_z);
        CREATE TABLE IF NOT EXISTS section_index (
            dimension TEXT NOT NULL,
            cx INTEGER NOT NULL,
            cz INTEGER NOT NULL,
            section_y INTEGER NOT NULL,
            palette_json TEXT NOT NULL,
            bits_per_block INTEGER NOT NULL,
            packed_data BLOB NOT NULL,
            PRIMARY KEY (dimension, cx, cz, section_y),
            FOREIGN KEY (dimension, cx, cz)
                REFERENCES chunk_index(dimension, cx, cz) ON DELETE CASCADE
        );
        """
    )
    row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    if row is None:
        connection.execute("INSERT INTO metadata(key, value) VALUES('schema_version', '1')")
    elif row[0] != "1":
        raise IndexErrorBase(f"unsupported index schema version: {row[0]}")


def _region_location(root: Path, dimension_path: PurePosixPath, rx: int, rz: int) -> Path:
    directory = root / dimension_path / "region"
    return directory / f"r.{rx}.{rz}.mca"


def _tag_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChunkIndexError("unsupported_chunk_layout", f"{where} must be an integer")
    return int(value)


def _normalize_palette(raw_palette: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_palette, list) or not raw_palette:
        raise ChunkIndexError("unsupported_chunk_layout", "block state palette is missing or empty")
    result: list[dict[str, Any]] = []
    for entry in raw_palette:
        if not isinstance(entry, dict) or type(entry.get("Name")) is not str:
            raise ChunkIndexError("unsupported_chunk_layout", "palette entry has no block Name")
        name = entry["Name"]
        if not _DIMENSION_ID.fullmatch(name):
            raise ChunkIndexError("unsupported_chunk_layout", f"block state name is not namespaced: {name!r}")
        properties = entry.get("Properties", {})
        if not isinstance(properties, dict) or any(type(k) is not str or type(v) is not str for k, v in properties.items()):
            raise ChunkIndexError("unsupported_chunk_layout", "block Properties must be strings")
        result.append({"name": name, "properties": dict(sorted(properties.items()))})
    if len(result) > 4096:
        raise ChunkIndexError("unsupported_chunk_layout", "block state palette exceeds section capacity")
    return result


def _pack_section_data(raw_data: Any, palette_size: int) -> tuple[int, bytes]:
    if palette_size == 1:
        if raw_data is not None:
            try:
                values = [int(v) for v in raw_data]
            except (TypeError, ValueError) as exc:
                raise ChunkIndexError("unsupported_chunk_layout", "invalid block state data") from exc
            if any(values):
                raise ChunkIndexError("unsupported_chunk_layout", "single-entry palette has nonzero indices")
        return 0, b""
    if raw_data is None or not isinstance(raw_data, (list, tuple)):
        raise ChunkIndexError("unsupported_chunk_layout", "multi-entry palette has no packed data")
    bits = max(4, (palette_size - 1).bit_length())
    values_per_long = 64 // bits
    expected_longs = (4096 + values_per_long - 1) // values_per_long
    if len(raw_data) != expected_longs:
        raise ChunkIndexError("unsupported_chunk_layout", "packed block state data has an unexpected length")
    packed = bytearray()
    for value in raw_data:
        number = _tag_int(value, "packed block state word")
        if number < -(1 << 63) or number >= (1 << 63):
            raise ChunkIndexError("unsupported_chunk_layout", "packed block state word is outside signed 64-bit range")
        packed.extend(struct.pack(">q", number))
    packed_bytes = bytes(packed)
    # Validate every stored palette index before the chunk can become known.
    # Vectorized 4K-element validation avoids retaining a dense 4096-item
    # Python object list for each section.
    words = np.frombuffer(packed_bytes, dtype=">u8").astype(np.uint64, copy=False)
    shifts = np.arange(values_per_long, dtype=np.uint64) * np.uint64(bits)
    positions = np.arange(expected_longs * values_per_long).reshape(-1, values_per_long)
    used = positions < 4096
    indices = (words.reshape(-1, 1) >> shifts.reshape(1, -1)) & np.uint64((1 << bits) - 1)
    if np.any(used & (indices >= palette_size)):
        raise ChunkIndexError("corrupt_section_data", "packed block state index exceeds palette")
    return bits, packed_bytes


def _chunk_sections(record: Any, dimension_bounds: tuple[int, int]) -> tuple[int, str, list[tuple[int, str, int, bytes]]]:
    nbt = record.nbt
    data_version = _tag_int(nbt.get("DataVersion"), "DataVersion")
    if data_version < MIN_SUPPORTED_DATA_VERSION:
        raise ChunkIndexError("unsupported_data_version", f"DataVersion {data_version} predates supported modern chunks")
    status = nbt.get("Status")
    if type(status) is not str or status not in ("full", "minecraft:full"):
        raise ChunkIndexError("incomplete_chunk", f"chunk status {status!r} is not full")
    raw_sections = nbt.get("sections")
    if not isinstance(raw_sections, list):
        raise ChunkIndexError("unsupported_chunk_layout", "modern sections list is missing")
    seen: set[int] = set()
    sections: list[tuple[int, str, int, bytes]] = []
    for section in raw_sections:
        if not isinstance(section, dict):
            raise ChunkIndexError("unsupported_chunk_layout", "section entry is not a compound")
        sy = _tag_int(section.get("Y"), "section Y")
        if sy in seen:
            raise ChunkIndexError("unsupported_chunk_layout", f"duplicate section Y {sy}")
        seen.add(sy)
        states = section.get("block_states")
        if states is None:
            # Full chunk sections with no block_states contain only air.
            continue
        if not isinstance(states, dict):
            raise ChunkIndexError("unsupported_chunk_layout", "block_states is not a compound")
        palette = _normalize_palette(states.get("palette"))
        bits, packed = _pack_section_data(states.get("data"), len(palette))
        palette_json = json.dumps(palette, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        sections.append((sy, palette_json, bits, packed))
    sections.sort(key=lambda item: item[0])
    return data_version, status, sections


def _region_stat_json(reader: RegionReader) -> str:
    return json.dumps(reader.signature, separators=(",", ":"))


def _delete_sections(connection: sqlite3.Connection, dimension: str, cx: int, cz: int) -> None:
    connection.execute(
        "DELETE FROM section_index WHERE dimension=? AND cx=? AND cz=?",
        (dimension, cx, cz),
    )


def _set_unknown(
    connection: sqlite3.Connection,
    *,
    dimension: str,
    cx: int,
    cz: int,
    rx: int,
    rz: int,
    reason: str,
    region_path: str | None,
    region_sha256: str | None,
    region_stat: str | None,
    covered: tuple[int, int],
    bounds_source: str,
    timestamp: str,
) -> None:
    _delete_sections(connection, dimension, cx, cz)
    connection.execute(
        """INSERT INTO chunk_index
        (dimension,cx,cz,region_x,region_z,state,reason,status,data_version,chunk_sha256,
         source_region_path,source_region_sha256,source_region_stat,covered_min_y,covered_max_y,
         bounds_source,refreshed_utc)
        VALUES(?,?,?,?,?,'unknown',?,NULL,NULL,NULL,?,?,?,?,?,?,?)
        ON CONFLICT(dimension,cx,cz) DO UPDATE SET
          region_x=excluded.region_x, region_z=excluded.region_z,
          state='unknown', reason=excluded.reason, status=NULL, data_version=NULL,
          chunk_sha256=NULL, source_region_path=excluded.source_region_path,
          source_region_sha256=excluded.source_region_sha256,
          source_region_stat=excluded.source_region_stat,
          covered_min_y=excluded.covered_min_y, covered_max_y=excluded.covered_max_y,
          bounds_source=excluded.bounds_source, refreshed_utc=excluded.refreshed_utc""",
        (dimension, cx, cz, rx, rz, reason, region_path, region_sha256, region_stat,
         covered[0], covered[1], bounds_source, timestamp),
    )


def _mark_region_unknown(
    connection: sqlite3.Connection,
    dimension: str,
    rx: int,
    rz: int,
    reason: str,
    timestamp: str,
) -> None:
    rows = connection.execute(
        "SELECT cx,cz FROM chunk_index WHERE dimension=? AND region_x=? AND region_z=?",
        (dimension, rx, rz),
    ).fetchall()
    for cx, cz in rows:
        _set_unknown(
            connection, dimension=dimension, cx=cx, cz=cz, rx=rx, rz=rz,
            reason=reason, region_path=None, region_sha256=None, region_stat=None,
            covered=(0, -1), bounds_source="unknown", timestamp=timestamp,
        )


def _db_init(connection: sqlite3.Connection, world_key: str) -> None:
    _ensure_schema(connection)
    row = connection.execute("SELECT value FROM metadata WHERE key='world_key'").fetchone()
    if row is None:
        connection.execute("INSERT INTO metadata(key,value) VALUES('world_key',?)", (world_key,))
    elif row[0] != world_key:
        raise WorldMismatchError("this index database is bound to a different world directory; use a new database")


def refresh_index(
    world: str | os.PathLike[str],
    database: str | os.PathLike[str],
    dimension: str,
    minimum: Iterable[int],
    maximum: Iterable[int],
    *,
    force: bool = False,
    dimension_bounds: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Refresh an inclusive ROI from one Java dimension without writing to it.

    The cache stores complete block-state sections for every requested chunk
    column. ``minimum``/``maximum`` bound the requested X/Y/Z ROI; a custom
    dimension's full inclusive Y range must be supplied with dimension_bounds.
    Missing, changing, unsupported, or corrupt source chunks are written as
    unknown records and replace any older cached value for those chunk columns.
    """
    root = Path(world).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"world directory does not exist: {root}")
    dim_path = _dimension_relative_path(dimension)
    lo, hi = _bounds(minimum, maximum)
    covered = _dimension_bounds(dimension, dimension_bounds)
    requested_min_y, requested_max_y, bounds_source = covered
    chunk_min_x, chunk_max_x = lo[0] // 16, hi[0] // 16
    chunk_min_z, chunk_max_z = lo[2] // 16, hi[2] // 16
    chunk_count = (chunk_max_x - chunk_min_x + 1) * (chunk_max_z - chunk_min_z + 1)
    if chunk_count > MAX_REFRESH_CHUNKS:
        raise IndexLimitError(f"refresh covers {chunk_count} chunks; limit is {MAX_REFRESH_CHUNKS}")
    if type(force) is not bool:
        raise ValueError("force must be a boolean")

    world_key = _world_key(root)
    connection, resolved_db = _database_path(database, root)
    connection.row_factory = sqlite3.Row
    try:
        _db_init(connection, world_key)
        connection.commit()
        timestamp = _now()
        requested: list[tuple[int, int, int, int]] = []
        old_rows: dict[tuple[int, int], sqlite3.Row] = {}
        for cx in range(chunk_min_x, chunk_max_x + 1):
            for cz in range(chunk_min_z, chunk_max_z + 1):
                rx, rz = cx // 32, cz // 32
                key = (cx, cz)
                old = connection.execute(
                    "SELECT * FROM chunk_index WHERE dimension=? AND cx=? AND cz=?",
                    (dimension, cx, cz),
                ).fetchone()
                if old is not None:
                    old_rows[key] = old
                requested.append((cx, cz, rx, rz))
        with connection:
            for cx, cz, rx, rz in requested:
                connection.execute(
                    """INSERT INTO chunk_index
                    (dimension,cx,cz,region_x,region_z,state,reason,covered_min_y,covered_max_y,
                     bounds_source,refreshed_utc)
                    VALUES(?,?,?,?,?,'stale','refresh_in_progress',?,?,?,?)
                    ON CONFLICT(dimension,cx,cz) DO UPDATE SET
                      region_x=excluded.region_x, region_z=excluded.region_z,
                      state='stale', reason='refresh_in_progress', refreshed_utc=excluded.refreshed_utc""",
                    (dimension, cx, cz, rx, rz, requested_min_y, requested_max_y, bounds_source, timestamp),
                )

        groups: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for cx, cz, rx, rz in requested:
            groups.setdefault((rx, rz), []).append((cx, cz))
        for (rx, rz), columns in sorted(groups.items()):
            region_path = _region_location(root, dim_path, rx, rz)
            relative_region = region_path.relative_to(root).as_posix()
            try:
                reader = RegionReader.open_if_present(region_path)
            except (RegionReadError, OSError) as exc:
                reason = getattr(exc, "reason", "region_unreadable")
                with connection:
                    _mark_region_unknown(connection, dimension, rx, rz, reason, _now())
                    for cx, cz in columns:
                        _set_unknown(
                            connection, dimension=dimension, cx=cx, cz=cz, rx=rx, rz=rz,
                            reason=reason, region_path=relative_region, region_sha256=None,
                            region_stat=None, covered=covered[:2], bounds_source=bounds_source,
                            timestamp=_now(),
                        )
                continue

            if reader is None:
                with connection:
                    _mark_region_unknown(connection, dimension, rx, rz, "absent_region", _now())
                    for cx, cz in columns:
                        _set_unknown(
                            connection, dimension=dimension, cx=cx, cz=cz, rx=rx, rz=rz,
                            reason="absent_region", region_path=relative_region,
                            region_sha256=None, region_stat=None, covered=covered[:2],
                            bounds_source=bounds_source, timestamp=_now(),
                        )
                continue

            try:
                region_stat = _region_stat_json(reader)
                with connection:
                    changed_cached = connection.execute(
                        """SELECT cx,cz FROM chunk_index
                           WHERE dimension=? AND region_x=? AND region_z=?
                             AND source_region_sha256 IS NOT NULL
                             AND source_region_sha256<>?""",
                        (dimension, rx, rz, reader.sha256),
                    ).fetchall()
                    requested_keys = {(cx, cz) for cx, cz in columns}
                    for stale in changed_cached:
                        old_cx, old_cz = int(stale["cx"]), int(stale["cz"])
                        if (old_cx, old_cz) not in requested_keys:
                            _set_unknown(
                                connection, dimension=dimension, cx=old_cx, cz=old_cz,
                                rx=rx, rz=rz, reason="region_changed_needs_refresh",
                                region_path=relative_region, region_sha256=reader.sha256,
                                region_stat=region_stat, covered=covered[:2],
                                bounds_source=bounds_source, timestamp=_now(),
                            )

                for cx, cz in columns:
                    previous = old_rows.get((cx, cz))
                    can_reuse = (
                        not force
                        and previous is not None
                        and previous["state"] == "known"
                        and previous["source_region_sha256"] == reader.sha256
                        and previous["covered_min_y"] == requested_min_y
                        and previous["covered_max_y"] == requested_max_y
                        and previous["bounds_source"] == bounds_source
                    )
                    if can_reuse:
                        with connection:
                            connection.execute(
                                """UPDATE chunk_index SET state='stale', reason='pending_region_verification',
                                     source_region_path=?, source_region_sha256=?, source_region_stat=?,
                                     refreshed_utc=? WHERE dimension=? AND cx=? AND cz=?""",
                                (relative_region, reader.sha256, region_stat, _now(), dimension, cx, cz),
                            )
                        continue
                    try:
                        record = reader.read_chunk(cx, cz)
                        if record is None:
                            with connection:
                                _set_unknown(
                                    connection, dimension=dimension, cx=cx, cz=cz, rx=rx, rz=rz,
                                    reason="absent_chunk", region_path=relative_region,
                                    region_sha256=reader.sha256, region_stat=region_stat,
                                    covered=covered[:2], bounds_source=bounds_source,
                                    timestamp=_now(),
                                )
                            continue
                        version, status, sections = _chunk_sections(record, covered[:2])
                        with connection:
                            _delete_sections(connection, dimension, cx, cz)
                            connection.executemany(
                                """INSERT INTO section_index
                                   (dimension,cx,cz,section_y,palette_json,bits_per_block,packed_data)
                                   VALUES(?,?,?,?,?,?,?)""",
                                [
                                    (dimension, cx, cz, sy, palette, bits, packed)
                                    for sy, palette, bits, packed in sections
                                ],
                            )
                            connection.execute(
                                """UPDATE chunk_index SET state='stale', reason='pending_region_verification',
                                     status=?, data_version=?, chunk_sha256=?, source_region_path=?,
                                     source_region_sha256=?, source_region_stat=?, covered_min_y=?,
                                     covered_max_y=?, bounds_source=?, refreshed_utc=?
                                   WHERE dimension=? AND cx=? AND cz=?""",
                                (status, version, record.compressed_sha256, relative_region,
                                 reader.sha256, region_stat, requested_min_y, requested_max_y,
                                 bounds_source, _now(), dimension, cx, cz),
                            )
                    except (RegionReadError, ChunkIndexError, OSError, ValueError) as exc:
                        reason = getattr(exc, "reason", "corrupt_chunk")
                        if isinstance(exc, UnsupportedCompressionError):
                            reason = "unsupported_compression"
                        with connection:
                            _set_unknown(
                                connection, dimension=dimension, cx=cx, cz=cz, rx=rx, rz=rz,
                                reason=reason, region_path=relative_region,
                                region_sha256=reader.sha256, region_stat=region_stat,
                                covered=covered[:2], bounds_source=bounds_source,
                                timestamp=_now(),
                            )
                reader.verify_stable()
                with connection:
                    connection.execute(
                        """UPDATE chunk_index SET state='known', reason=NULL
                           WHERE dimension=? AND region_x=? AND region_z=?
                             AND state='stale' AND reason='pending_region_verification'
                             AND source_region_sha256=?""",
                        (dimension, rx, rz, reader.sha256),
                    )
                    connection.execute(
                        """UPDATE chunk_index SET refreshed_utc=?, source_region_stat=?
                           WHERE dimension=? AND region_x=? AND region_z=?
                             AND state='known' AND source_region_sha256=?""",
                        (_now(), region_stat, dimension, rx, rz, reader.sha256),
                    )
            except (SourceChangedError, RegionReadError, OSError) as exc:
                reason = getattr(exc, "reason", "region_unreadable")
                with connection:
                    _mark_region_unknown(connection, dimension, rx, rz, reason, _now())
                    for cx, cz in columns:
                        _set_unknown(
                            connection, dimension=dimension, cx=cx, cz=cz, rx=rx, rz=rz,
                            reason=reason, region_path=relative_region,
                            region_sha256=None, region_stat=None, covered=covered[:2],
                            bounds_source=bounds_source, timestamp=_now(),
                        )
            finally:
                reader.close()

        # A hard failure leaves no requested row looking current. Stale rows
        # are query-unknown as well, but replacing them with a reason makes the
        # failure useful to callers and keeps crash recovery explicit.
        with connection:
            connection.execute(
                """UPDATE chunk_index SET state='unknown', reason='refresh_failed'
                   WHERE dimension=? AND state='stale' AND reason IN
                     ('refresh_in_progress','pending_region_verification')""",
                (dimension,),
            )
            connection.execute(
                """DELETE FROM section_index WHERE dimension=? AND EXISTS (
                     SELECT 1 FROM chunk_index c WHERE c.dimension=section_index.dimension
                       AND c.cx=section_index.cx AND c.cz=section_index.cz
                       AND c.state='unknown' AND c.reason='refresh_failed')""",
                (dimension,),
            )
        counts = dict(connection.execute(
            """SELECT state, COUNT(*) FROM chunk_index
               WHERE dimension=? AND cx BETWEEN ? AND ? AND cz BETWEEN ? AND ?
               GROUP BY state""",
            (dimension, chunk_min_x, chunk_max_x, chunk_min_z, chunk_max_z),
        ).fetchall())
        rows = connection.execute(
            """SELECT cx,cz,reason,source_region_sha256,chunk_sha256,data_version,state
               FROM chunk_index WHERE dimension=? AND cx BETWEEN ? AND ? AND cz BETWEEN ? AND ?
               ORDER BY cx,cz""",
            (dimension, chunk_min_x, chunk_max_x, chunk_min_z, chunk_max_z),
        ).fetchall()
        manifest_rows = [
            [row["cx"], row["cz"], row["state"], row["reason"], row["source_region_sha256"], row["chunk_sha256"], row["data_version"]]
            for row in rows
        ]
        snapshot_id = hashlib.sha256(json.dumps(
            [world_key, dimension, requested_min_y, requested_max_y, manifest_rows],
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        connection.commit()
        return {
            "snapshot_id": snapshot_id,
            "dimension": dimension,
            "bounds": {"min": lo, "max": hi},
            "dimension_bounds": {"min_y": requested_min_y, "max_y": requested_max_y},
            "dimension_bounds_source": bounds_source,
            "requested_chunks": chunk_count,
            "known_chunks": int(counts.get("known", 0)),
            "unknown_chunks": int(counts.get("unknown", 0) + counts.get("stale", 0)),
            "refreshed_utc": _now(),
            "database": str(resolved_db) if resolved_db is not None else ":memory:",
            "source_consistency": "per-region-stable; regions are not an atomic world snapshot",
        }
    except Exception:
        # A Python exception after invalidation must never revive old rows.
        try:
            with connection:
                connection.execute(
                    """UPDATE chunk_index SET state='unknown', reason='refresh_failed'
                       WHERE state='stale' AND reason IN
                         ('refresh_in_progress','pending_region_verification')"""
                )
                connection.execute(
                    """DELETE FROM section_index WHERE EXISTS (
                         SELECT 1 FROM chunk_index c WHERE c.dimension=section_index.dimension
                           AND c.cx=section_index.cx AND c.cz=section_index.cz
                           AND c.state='unknown' AND c.reason='refresh_failed')"""
                )
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def _chunk_row(connection: sqlite3.Connection, dimension: str, cx: int, cz: int) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM chunk_index WHERE dimension=? AND cx=? AND cz=?",
        (dimension, cx, cz),
    ).fetchone()


def _unknown_result(dimension: str, pos: list[int], reason: str, row: sqlite3.Row | None = None) -> dict[str, Any]:
    return {
        "status": "unknown",
        "dimension": dimension,
        "pos": pos,
        "block": None,
        "reason": reason,
        "known_as_of_last_refresh": row["refreshed_utc"] if row is not None else None,
    }


def _palette_at_index(palette: list[dict[str, Any]], packed: bytes, bits: int, cell_index: int) -> int:
    if bits == 0:
        palette_index = 0
    else:
        values_per_long = 64 // bits
        word_index = cell_index // values_per_long
        shift = (cell_index % values_per_long) * bits
        if word_index * 8 + 8 > len(packed):
            raise ChunkIndexError("corrupt_section_data", "packed block state word is missing")
        signed = struct.unpack_from(">q", packed, word_index * 8)[0]
        word = signed & ((1 << 64) - 1)
        palette_index = (word >> shift) & ((1 << bits) - 1)
    if palette_index >= len(palette):
        raise ChunkIndexError("corrupt_section_data", "packed block state index exceeds palette")
    return palette_index


def _block_at_known_chunk(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    dimension: str,
    pos: list[int],
) -> dict[str, Any]:
    if pos[1] < row["covered_min_y"] or pos[1] > row["covered_max_y"]:
        return _unknown_result(dimension, pos, "outside_vertical_coverage", row)
    sy = pos[1] // 16
    section = connection.execute(
        """SELECT palette_json,bits_per_block,packed_data FROM section_index
           WHERE dimension=? AND cx=? AND cz=? AND section_y=?""",
        (dimension, pos[0] // 16, pos[2] // 16, sy),
    ).fetchone()
    if section is None:
        block = {"name": "minecraft:air", "properties": {}}
    else:
        palette = json.loads(section["palette_json"])
        local_x, local_y, local_z = pos[0] % 16, pos[1] % 16, pos[2] % 16
        index = local_y * 256 + local_z * 16 + local_x
        try:
            palette_index = _palette_at_index(palette, section["packed_data"], section["bits_per_block"], index)
        except ChunkIndexError as exc:
            return _unknown_result(dimension, pos, exc.reason, row)
        block = palette[palette_index]
    return {
        "status": "known",
        "dimension": dimension,
        "pos": pos,
        "block": block,
        "known_as_of_last_refresh": row["refreshed_utc"],
        "source_region_sha256": row["source_region_sha256"],
        "source_consistency": "per-region-stable; not an atomic world snapshot",
    }


def query_block(
    database: str | os.PathLike[str],
    dimension: str,
    pos: Iterable[int],
) -> dict[str, Any]:
    """Query one global block coordinate from the last refreshed cache."""
    _dimension_relative_path(dimension)
    try:
        point = list(pos)
    except TypeError as exc:
        raise ValueError("pos must contain three integers") from exc
    if len(point) != 3 or any(type(v) is not int for v in point):
        raise ValueError("pos must contain three integers")
    connection = _query_connection(database)
    connection.row_factory = sqlite3.Row
    try:
        row = _chunk_row(connection, dimension, point[0] // 16, point[2] // 16)
        if row is None:
            return _unknown_result(dimension, point, "not_indexed")
        if row["state"] != "known":
            return _unknown_result(dimension, point, row["reason"] or "unknown", row)
        return _block_at_known_chunk(connection, row, dimension, point)
    finally:
        connection.close()


def export_scene(
    database: str | os.PathLike[str],
    dimension: str,
    minimum: Iterable[int],
    maximum: Iterable[int],
) -> dict[str, Any]:
    """Create Scene v1 JSON for an inclusive ROI in the last refreshed cache."""
    _dimension_relative_path(dimension)
    lo, hi = _bounds(minimum, maximum)
    volume = math.prod(hi[i] - lo[i] + 1 for i in range(3))
    if volume > MAX_EXPORT_VOLUME:
        raise IndexLimitError(f"scene volume is {volume}; limit is {MAX_EXPORT_VOLUME}")
    cx_lo, cx_hi, cz_lo, cz_hi = lo[0] // 16, hi[0] // 16, lo[2] // 16, hi[2] // 16
    column_count = (cx_hi - cx_lo + 1) * (cz_hi - cz_lo + 1)
    if column_count > MAX_REFRESH_CHUNKS:
        raise IndexLimitError(f"scene covers {column_count} chunk columns; limit is {MAX_REFRESH_CHUNKS}")

    connection = _query_connection(database)
    connection.row_factory = sqlite3.Row
    try:
        chunk_rows: dict[tuple[int, int], sqlite3.Row | None] = {}
        unknown: list[dict[str, Any]] = []
        source_rows: list[list[Any]] = []
        source_regions: dict[str, str] = {}
        data_versions: set[int] = set()
        refreshed_times: list[str] = []
        bounds_sources: set[str] = set()
        for cx in range(cx_lo, cx_hi + 1):
            for cz in range(cz_lo, cz_hi + 1):
                row = _chunk_row(connection, dimension, cx, cz)
                chunk_rows[(cx, cz)] = row
                reason = "not_indexed" if row is None else (row["reason"] or "unknown")
                if row is None or row["state"] != "known":
                    unknown.append({"x": cx, "z": cz, "reason": reason})
                    source_rows.append([cx, cz, "unknown", reason])
                    continue
                bounds_sources.add(row["bounds_source"] or "unknown")
                if lo[1] < row["covered_min_y"] or hi[1] > row["covered_max_y"]:
                    unknown.append({"x": cx, "z": cz, "reason": "outside_vertical_coverage"})
                    source_rows.append([cx, cz, "unknown", "outside_vertical_coverage", row["source_region_sha256"], row["chunk_sha256"]])
                    continue
                source_rows.append([cx, cz, "known", row["source_region_sha256"], row["chunk_sha256"], row["data_version"]])
                if row["source_region_path"] and row["source_region_sha256"]:
                    source_regions[row["source_region_path"]] = row["source_region_sha256"]
                if row["data_version"] is not None:
                    data_versions.add(int(row["data_version"]))
                if row["refreshed_utc"]:
                    refreshed_times.append(row["refreshed_utc"])

        unknown_keys = {(item["x"], item["z"]) for item in unknown}
        palette: list[dict[str, Any]] = []
        palette_lookup: dict[str, int] = {}
        blocks: list[dict[str, Any]] = []
        y_min_section, y_max_section = lo[1] // 16, hi[1] // 16
        for cx in range(cx_lo, cx_hi + 1):
            for cz in range(cz_lo, cz_hi + 1):
                if (cx, cz) in unknown_keys:
                    continue
                row = chunk_rows[(cx, cz)]
                assert row is not None
                x_start, x_end = max(lo[0], cx * 16), min(hi[0], cx * 16 + 15)
                z_start, z_end = max(lo[2], cz * 16), min(hi[2], cz * 16 + 15)
                for sy in range(y_min_section, y_max_section + 1):
                    y_start, y_end = max(lo[1], sy * 16), min(hi[1], sy * 16 + 15)
                    section = connection.execute(
                        """SELECT palette_json,bits_per_block,packed_data FROM section_index
                           WHERE dimension=? AND cx=? AND cz=? AND section_y=?""",
                        (dimension, cx, cz, sy),
                    ).fetchone()
                    if section is None:
                        continue
                    states = json.loads(section["palette_json"])
                    packed, bits = section["packed_data"], int(section["bits_per_block"])
                    for y in range(y_start, y_end + 1):
                        local_y = y % 16
                        for z in range(z_start, z_end + 1):
                            local_z = z % 16
                            for x in range(x_start, x_end + 1):
                                local_x = x % 16
                                cell_index = local_y * 256 + local_z * 16 + local_x
                                try:
                                    state = states[_palette_at_index(states, packed, bits, cell_index)]
                                except (ChunkIndexError, IndexError) as exc:
                                    # A malformed packed index makes the whole column
                                    # unknown in this scene; no old/partial block rows survive.
                                    unknown.append({"x": cx, "z": cz, "reason": getattr(exc, "reason", "corrupt_section_data")})
                                    unknown_keys.add((cx, cz))
                                    blocks = [item for item in blocks if not (item["pos"][0] // 16 == cx and item["pos"][2] // 16 == cz)]
                                    break
                                if state["name"] in _AIR:
                                    continue
                                state_key = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                                palette_index = palette_lookup.get(state_key)
                                if palette_index is None:
                                    if len(palette) >= MAX_EXPORT_BLOCKS:
                                        raise IndexLimitError(f"scene has more than {MAX_EXPORT_BLOCKS} distinct/output blocks")
                                    palette_index = len(palette)
                                    palette_lookup[state_key] = palette_index
                                    palette.append(state)
                                blocks.append({"pos": [x, y, z], "palette": palette_index})
                                if len(blocks) > MAX_EXPORT_BLOCKS:
                                    raise IndexLimitError(f"scene has more than {MAX_EXPORT_BLOCKS} non-air blocks")
                            if (cx, cz) in unknown_keys:
                                break
                        if (cx, cz) in unknown_keys:
                            break
                    if (cx, cz) in unknown_keys:
                        break

        unknown.sort(key=lambda item: (item["x"], item["z"], item["reason"]))
        # A duplicate can occur if a malformed column was encountered after its
        # initial coverage check. Keep exactly one reason per chunk.
        unique_unknown: list[dict[str, Any]] = []
        unknown_seen: set[tuple[int, int]] = set()
        for item in unknown:
            key = (item["x"], item["z"])
            if key not in unknown_seen:
                unique_unknown.append(item)
                unknown_seen.add(key)

        metadata = connection.execute("SELECT value FROM metadata WHERE key='world_key'").fetchone()
        world_id = metadata[0] if metadata else "unknown"
        snapshot_id = hashlib.sha256(json.dumps(
            [world_id, dimension, lo, hi, source_rows], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        scene = {
            "schema_version": 1,
            "dimension": dimension,
            "bounds": {"min": lo, "max": hi},
            "palette": palette,
            "blocks": blocks,
            "unknown_chunks": unique_unknown,
            "provenance": {
                "snapshot_id": snapshot_id,
                "world_id": world_id,
                "data_versions": sorted(data_versions),
                "source_regions": [
                    {"path": path, "sha256": digest}
                    for path, digest in sorted(source_regions.items())
                ],
                "dimension_bounds_sources": sorted(bounds_sources),
                "refreshed_utc": max(refreshed_times) if refreshed_times else None,
                "exported_utc": _now(),
                "known_as_of_last_refresh": True,
                "source_consistency": "Per-region file hashes and stat stability; region files do not form one atomic world snapshot.",
                "coverage_complete": not unique_unknown,
            },
            "kind": "world",
        }
        return validate_scene(scene)
    finally:
        connection.close()
