"""Synthetic WAV builder + corruption helpers for the riff_inspect tests.

Builds byte-exact RIFF/RF64 files chunk by chunk, so each test can break
exactly one thing (a size, a pad byte, a header) the way a real damaged
recorder file would be broken, and nothing else.
"""
from __future__ import annotations

import struct
from pathlib import Path

PCM_FORMAT = 1


def fmt_payload(channels: int = 2, sample_rate: int = 48000, bits: int = 24, block_align: int | None = None) -> bytes:
    block_align = channels * (bits // 8) if block_align is None else block_align
    return struct.pack("<HHIIHH", PCM_FORMAT, channels, sample_rate, sample_rate * block_align, block_align, bits)


def bext_payload(description: str = "sSCENE=56-1\r\nsTAKE=3\r\nsTAPE=0110\r\n", time_reference: int = 48000 * 3600 * 14) -> bytes:
    payload = bytearray(602)
    payload[0:256] = description.encode("ascii").ljust(256, b"\x00")[:256]
    payload[256:288] = b"SoundDev".ljust(32, b"\x00")
    payload[320:330] = b"2026-10-01"
    payload[330:338] = b"14:00:00"
    struct.pack_into("<IIH", payload, 338, time_reference & 0xFFFFFFFF, time_reference >> 32, 1)
    return bytes(payload)


def ixml_payload(track_names: tuple[str, ...] = ("BOOM", "VICKY"), scene: str = "56-1", take: str = "3", tape: str = "0110", nul_padding: int = 0) -> bytes:
    tracks = "".join(
        f"<TRACK><CHANNEL_INDEX>{index}</CHANNEL_INDEX><INTERLEAVE_INDEX>{index}</INTERLEAVE_INDEX><NAME>{name}</NAME></TRACK>"
        for index, name in enumerate(track_names, start=1)
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><BWFXML><IXML_VERSION>1.61</IXML_VERSION>'
        f"<PROJECT>P.UP_01</PROJECT><SCENE>{scene}</SCENE><TAKE>{take}</TAKE><TAPE>{tape}</TAPE>"
        "<SPEED><TIMECODE_RATE>25/1</TIMECODE_RATE><TIMECODE_FLAG>NDF</TIMECODE_FLAG></SPEED>"
        f"<TRACK_LIST><TRACK_COUNT>{len(track_names)}</TRACK_COUNT>{tracks}</TRACK_LIST></BWFXML>"
    )
    return xml.encode("utf-8") + b"\x00" * nul_padding


def audio_payload(frames: int = 4800, block_align: int = 6) -> bytes:
    # Deterministic, non-silent, and deliberately free of printable runs that
    # could look like a chunk ID to a signature scan.
    return bytes((index * 37 + 0x80) & 0xFF | 0x80 for index in range(frames * block_align))


def chunk(chunk_id: bytes, payload: bytes, declared_size: int | None = None, pad: bytes | None = b"\x00") -> bytes:
    size = len(payload) if declared_size is None else declared_size
    raw = struct.pack("<4sI", chunk_id, size) + payload
    if len(payload) & 1 and pad is not None:
        raw += pad
    return raw


def riff(chunks: list[bytes], form: bytes = b"RIFF", declared_size: int | None = None) -> bytes:
    body = b"WAVE" + b"".join(chunks)
    size = len(body) if declared_size is None else declared_size
    return form + struct.pack("<I", size) + body


def standard_chunks(frames: int = 4800, with_bext: bool = True, with_ixml: bool = True, ixml: bytes | None = None) -> list[bytes]:
    chunks = [chunk(b"fmt ", fmt_payload())]
    if with_bext:
        chunks.append(chunk(b"bext", bext_payload()))
    if with_ixml:
        chunks.append(chunk(b"iXML", ixml if ixml is not None else ixml_payload()))
    chunks.append(chunk(b"data", audio_payload(frames)))
    return chunks


def rf64(chunks_after_ds64: list[bytes], data_size: int, ds64: bool = True) -> bytes:
    body_without_ds64 = b"WAVE" + b"".join(chunks_after_ds64)
    ds64_chunk = b""
    if ds64:
        riff_size = len(body_without_ds64) + 8 + 28
        ds64_chunk = chunk(b"ds64", struct.pack("<QQQI", riff_size, data_size, data_size // 6, 0))
    return b"RF64" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" + ds64_chunk + b"".join(chunks_after_ds64)


def write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path
