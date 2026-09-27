"""Read-only Java Anvil region access for modern, bounded index builds."""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SECTOR_BYTES = 4096
HEADER_BYTES = 2 * SECTOR_BYTES
MAX_REGION_BYTES = 512 * 1024 * 1024
MAX_CHUNK_NBT_BYTES = 64 * 1024 * 1024


class RegionReadError(ValueError):
    """A region or chunk record could not be read safely."""

    reason = "region_read_error"


class SourceChangedError(RegionReadError):
    reason = "source_changed_during_read"


class UnsupportedCompressionError(RegionReadError):
    reason = "unsupported_compression"


class CorruptRegionError(RegionReadError):
    reason = "corrupt_region"


class CorruptChunkError(RegionReadError):
    reason = "corrupt_chunk"


@dataclass(frozen=True)
class ChunkRecord:
    x: int
    z: int
    nbt: dict[str, Any]
    compressed_sha256: str
    timestamp: int


def _signature(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def _plain(value: Any) -> Any:
    """Convert nbtlib tags to ordinary Python JSON-like values."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "tolist"):
        return _plain(value.tolist())
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    if value is None:
        return value
    if hasattr(value, "unpack"):
        return _plain(value.unpack())
    raise CorruptChunkError(f"unsupported NBT value type {type(value).__name__}")


def _decompress(compression: int, payload: bytes) -> bytes:
    if compression == 3:
        if len(payload) > MAX_CHUNK_NBT_BYTES:
            raise CorruptChunkError("uncompressed chunk NBT exceeds the configured limit")
        return payload
    if compression not in (1, 2):
        raise UnsupportedCompressionError(f"unsupported region compression type {compression}")

    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if compression == 1 else zlib.MAX_WBITS)
    try:
        result = decoder.decompress(payload, MAX_CHUNK_NBT_BYTES + 1)
        if len(result) > MAX_CHUNK_NBT_BYTES or decoder.unconsumed_tail:
            raise CorruptChunkError("decompressed chunk NBT exceeds the configured limit")
        result += decoder.flush(MAX_CHUNK_NBT_BYTES + 1 - len(result))
    except zlib.error as exc:
        raise CorruptChunkError(f"chunk decompression failed: {exc}") from exc
    if len(result) > MAX_CHUNK_NBT_BYTES:
        raise CorruptChunkError("decompressed chunk NBT exceeds the configured limit")
    if not decoder.eof or decoder.unused_data:
        raise CorruptChunkError("compressed chunk is truncated or has trailing data")
    return result


class RegionReader:
    """One stable, read-only view of a single ``r.<x>.<z>.mca`` file."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._stream: io.BufferedReader | None = None
        try:
            self._stream = self.path.open("rb")
            before = os.fstat(self._stream.fileno())
            if before.st_size < HEADER_BYTES or before.st_size % SECTOR_BYTES:
                raise CorruptRegionError("region size is not a valid sector-aligned Anvil file")
            if before.st_size > MAX_REGION_BYTES:
                raise CorruptRegionError("region file exceeds the configured size limit")
            self.signature = _signature(before)
            digest = hashlib.sha256()
            self._stream.seek(0)
            while True:
                part = self._stream.read(1024 * 1024)
                if not part:
                    break
                digest.update(part)
            if _signature(os.fstat(self._stream.fileno())) != self.signature:
                raise SourceChangedError("region file changed while its hash was computed")
            self.sha256 = digest.hexdigest()
            self._stream.seek(0)
            header = self._stream.read(HEADER_BYTES)
            if len(header) != HEADER_BYTES:
                raise CorruptRegionError("region header is truncated")
            self._locations = header[:SECTOR_BYTES]
            self._timestamps = header[SECTOR_BYTES:HEADER_BYTES]
        except Exception:
            self.close()
            raise

    @property
    def size(self) -> int:
        return self.signature[2]

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def __enter__(self) -> "RegionReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def verify_stable(self) -> None:
        if self._stream is None:
            raise RegionReadError("region reader is closed")
        try:
            current_fd = os.fstat(self._stream.fileno())
            current_path = self.path.stat()
        except OSError as exc:
            raise SourceChangedError(f"region file is no longer readable: {exc}") from exc
        if _signature(current_fd) != self.signature or _signature(current_path) != self.signature:
            raise SourceChangedError("region file changed or was replaced during chunk reads")

    def chunk_present(self, local_x: int, local_z: int) -> bool:
        if not (0 <= local_x < 32 and 0 <= local_z < 32):
            raise ValueError("local chunk coordinates must be in 0..31")
        slot = local_x + local_z * 32
        return self._locations[slot * 4 : slot * 4 + 4] != b"\x00\x00\x00\x00"

    def read_chunk(self, chunk_x: int, chunk_z: int) -> ChunkRecord | None:
        if self._stream is None:
            raise RegionReadError("region reader is closed")
        local_x, local_z = chunk_x % 32, chunk_z % 32
        slot = local_x + local_z * 32
        entry = self._locations[slot * 4 : slot * 4 + 4]
        offset = int.from_bytes(entry[:3], "big")
        sectors = entry[3]
        if offset == 0 and sectors == 0:
            return None
        if offset < 2 or sectors == 0 or offset * SECTOR_BYTES + sectors * SECTOR_BYTES > self.size:
            raise CorruptChunkError("chunk sector allocation is invalid")
        self.verify_stable()
        self._stream.seek(offset * SECTOR_BYTES)
        sector_data = self._stream.read(sectors * SECTOR_BYTES)
        self.verify_stable()
        if len(sector_data) != sectors * SECTOR_BYTES or len(sector_data) < 5:
            raise CorruptChunkError("chunk sector data is truncated")
        length = int.from_bytes(sector_data[:4], "big")
        if length < 1 or length + 4 > len(sector_data):
            raise CorruptChunkError("chunk record length exceeds its sector allocation")
        compression_flag = sector_data[4]
        if compression_flag & 0x80:
            raise UnsupportedCompressionError("external .mcc chunk records are not supported")
        compression = compression_flag
        compressed = sector_data[5 : 4 + length]
        raw = _decompress(compression, compressed)
        try:
            import nbtlib

            stream = io.BytesIO(raw)
            parsed = nbtlib.File.parse(stream)
            if stream.tell() != len(raw):
                raise CorruptChunkError("chunk NBT has trailing bytes")
            tree = _plain(parsed)
        except RegionReadError:
            raise
        except Exception as exc:
            raise CorruptChunkError(f"chunk NBT parse failed: {exc}") from exc
        root_x = tree.get("xPos")
        root_z = tree.get("zPos")
        if type(root_x) is not int or type(root_z) is not int or (root_x, root_z) != (chunk_x, chunk_z):
            raise CorruptChunkError("chunk NBT coordinates do not match its region slot")
        timestamp = int.from_bytes(self._timestamps[slot * 4 : slot * 4 + 4], "big")
        return ChunkRecord(
            x=chunk_x,
            z=chunk_z,
            nbt=tree,
            compressed_sha256=hashlib.sha256(sector_data[: 4 + length]).hexdigest(),
            timestamp=timestamp,
        )

    @classmethod
    def open_if_present(cls, path: str | os.PathLike[str]) -> "RegionReader | None":
        try:
            return cls(path)
        except FileNotFoundError:
            return None
