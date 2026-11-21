from __future__ import annotations

import os
import shutil
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class WavChunk:
    chunk_id: bytes
    offset: int
    size: int

    @property
    def data_offset(self) -> int:
        return self.offset + 8

    @property
    def raw_size(self) -> int:
        return 8 + self.size + (self.size & 1)


def scan_wav_chunks(wav_path: Path) -> tuple[bytes, list[WavChunk]]:
    with wav_path.open("rb") as wav_file:
        header = wav_file.read(12)
        if len(header) != 12 or header[:4] not in (b"RIFF", b"RF64") or header[8:] != b"WAVE":
            raise RuntimeError(f"not a RIFF/WAVE file: {wav_path}")
        chunks = []
        while True:
            chunk_offset = wav_file.tell()
            chunk_header = wav_file.read(8)
            if len(chunk_header) != 8:
                break
            chunk_id, chunk_size = struct.unpack("<4sI", chunk_header)
            chunks.append(WavChunk(chunk_id, chunk_offset, chunk_size))
            wav_file.seek(chunk_size + (chunk_size & 1), os.SEEK_CUR)
    return header, chunks


def find_wav_chunk(wav_path: Path, chunk_id: bytes) -> tuple[bytes, Optional[WavChunk]]:
    if len(chunk_id) != 4:
        raise ValueError("WAV chunk id must be exactly 4 bytes")
    header, chunks = scan_wav_chunks(wav_path)
    return header, next((chunk for chunk in chunks if chunk.chunk_id == chunk_id), None)


def read_wav_chunk(wav_path: Path, chunk: WavChunk, raw: bool = False) -> bytes:
    offset = chunk.offset if raw else chunk.data_offset
    size = chunk.raw_size if raw else chunk.size
    with wav_path.open("rb") as wav_file:
        wav_file.seek(offset)
        data = wav_file.read(size)
    if len(data) != size:
        name = chunk.chunk_id.decode(errors="replace")
        raise RuntimeError(f"truncated {name} chunk: {wav_path}")
    return data


def remove_wav_chunk(wav_path: Path, chunk_id: bytes) -> None:
    header, chunk = find_wav_chunk(wav_path, chunk_id)
    if chunk is None:
        return
    if header[:4] == b"RF64":
        raise RuntimeError(f"{chunk_id.decode(errors='replace')} cannot be safely removed from RF64 output")
    temp_path = wav_path.with_name(f".{wav_path.name}.remove-{uuid.uuid4().hex}")
    try:
        with wav_path.open("rb") as source_file, temp_path.open("wb") as rebuilt:
            remaining_before = chunk.offset
            while remaining_before:
                block = source_file.read(min(1024 * 1024, remaining_before))
                if not block:
                    raise RuntimeError("WAV truncated before metadata chunk")
                rebuilt.write(block)
                remaining_before -= len(block)
            source_file.seek(chunk.offset + chunk.raw_size)
            shutil.copyfileobj(source_file, rebuilt, length=1024 * 1024)
        with temp_path.open("r+b") as rebuilt:
            rebuilt.seek(4)
            rebuilt.write(struct.pack("<I", temp_path.stat().st_size - 8))
        os.replace(temp_path, wav_path)
    finally:
        temp_path.unlink(missing_ok=True)


def copy_wav_chunk_from_source(source: Path, target: Path, chunk_id: bytes) -> None:
    """Copy one RIFF/WAVE chunk byte-for-byte from source into target."""
    _source_header, source_chunk = find_wav_chunk(source, chunk_id)
    if source_chunk is None:
        return
    source_raw = read_wav_chunk(source, source_chunk, raw=True)

    target_header, target_chunk = find_wav_chunk(target, chunk_id)
    if target_chunk is not None:
        target_offset = target_chunk.offset
        if source_chunk.size == target_chunk.size:
            with target.open("r+b") as wav_file:
                wav_file.seek(target_offset)
                wav_file.write(source_raw)
            return
    else:
        _header, data_chunk = find_wav_chunk(target, b"data")
        if data_chunk is None:
            raise RuntimeError(f"target has no data chunk: {target}")
        target_offset = data_chunk.offset

    if target_header[:4] == b"RF64":
        raise RuntimeError(f"{chunk_id.decode(errors='replace')} cannot be safely inserted/replaced in RF64 output")

    temp_path = target.with_name(f".{target.name}.preserve-{uuid.uuid4().hex}")
    try:
        with target.open("rb") as target_file, temp_path.open("wb") as rebuilt:
            remaining_before = target_offset
            while remaining_before:
                block = target_file.read(min(1024 * 1024, remaining_before))
                if not block:
                    raise RuntimeError("target WAV truncated before metadata chunk")
                rebuilt.write(block)
                remaining_before -= len(block)

            rebuilt.write(source_raw)

            if target_chunk is not None:
                target_file.seek(target_offset + target_chunk.raw_size)

            shutil.copyfileobj(target_file, rebuilt, length=1024 * 1024)

        new_file_size = temp_path.stat().st_size
        with temp_path.open("r+b") as rebuilt:
            rebuilt.seek(4)
            rebuilt.write(struct.pack("<I", new_file_size - 8))

        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)


def clear_wav_channel_mask(wav_path: Path) -> None:
    try:
        _header, fmt_chunk = find_wav_chunk(wav_path, b"fmt ")
    except RuntimeError as exc:
        raise RuntimeError("combined output is not a RIFF/WAVE file") from exc
    if fmt_chunk is None:
        raise RuntimeError("combined output has no fmt chunk")
    fmt_data = read_wav_chunk(wav_path, fmt_chunk)
    format_tag = struct.unpack_from("<H", fmt_data)[0]
    if format_tag != 0xFFFE:
        return
    if fmt_chunk.size < 24:
        raise RuntimeError("combined output has an invalid extensible fmt chunk")
    with wav_path.open("r+b") as wav_file:
        wav_file.seek(fmt_chunk.data_offset + 20)
        wav_file.write(struct.pack("<I", 0))


def set_wav_channel_mask(wav_path: Path, channel_mask: int) -> None:
    try:
        _header, fmt_chunk = find_wav_chunk(wav_path, b"fmt ")
    except RuntimeError as exc:
        raise RuntimeError("merged output is not a RIFF/WAVE file") from exc
    if fmt_chunk is None:
        raise RuntimeError("merged output has no fmt chunk")
    fmt_data = read_wav_chunk(wav_path, fmt_chunk)
    if len(fmt_data) < 24 or struct.unpack_from("<H", fmt_data)[0] != 0xFFFE:
        raise RuntimeError("merged output has no extensible channel mask")
    with wav_path.open("r+b") as wav_file:
        wav_file.seek(fmt_chunk.data_offset + 20)
        wav_file.write(struct.pack("<I", channel_mask))


def wav_structure_info(wav_path: Path) -> dict[str, object]:
    try:
        _header, chunks = scan_wav_chunks(wav_path)
    except RuntimeError as exc:
        raise RuntimeError("not a RIFF/WAVE file") from exc
    info: dict[str, object] = {"format": "WAV", "block_align": "-", "data_start": "-", "channel_mask": None}
    for chunk in chunks:
        if chunk.chunk_id == b"fmt ":
            fmt_data = read_wav_chunk(wav_path, chunk)
            if len(fmt_data) < 14:
                raise RuntimeError("truncated fmt chunk")
            info["block_align"] = str(struct.unpack_from("<H", fmt_data, 12)[0])
            if len(fmt_data) >= 24 and struct.unpack_from("<H", fmt_data)[0] == 0xFFFE:
                info["channel_mask"] = struct.unpack_from("<I", fmt_data, 20)[0]
        elif chunk.chunk_id == b"data":
            info["data_start"] = str(chunk.data_offset)
            return info
    return info
