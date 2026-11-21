"""Read-only, damage-tolerant RIFF/WAVE structure inspection.

Unlike `wav_core.scan_wav_chunks()` — which trusts every declared chunk size
and is meant for files already known to be well-formed — this never trusts
a size it can't confirm against the real file. Every chunk boundary is
checked against EOF, every FourCC is checked for being plausible ASCII, and
when the structure genuinely breaks the walk stops there and reports *where*
it broke, listing any recognizable chunk signatures found afterwards only as
candidates rather than guessing which one is real.

Nothing here ever writes to a file. Each `Issue` carries a stable `code`
plus a `repairable` flag so a later repair stage can act on exactly what was
diagnosed, but deciding what to change is deliberately not this module's job.
"""
from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import BinaryIO, Optional

from operations.timecode import samples_to_smpte


SEVERITY_ORDER = {"ok": 0, "info": 1, "warning": 2, "error": 3}
CATEGORIES = ("container", "fmt", "bext", "ixml", "data")

# How far past a structural break (and back from EOF) to look for chunk
# signatures. Bounded so a break right before a multi-GB data payload never
# turns a read-only inspection into a full-file read.
SIGNATURE_SCAN_WINDOW = 16 * 1024 * 1024

BEXT_MIN_SIZE = 602
UINT32_MAX = 0xFFFFFFFF

KNOWN_CHUNK_IDS = (
    b"fmt ", b"data", b"bext", b"iXML", b"ds64", b"LIST", b"JUNK", b"PAD ", b"FLLR",
    b"axml", b"aXML", b"_PMX", b"cue ", b"smpl", b"inst", b"chna", b"umid", b"minf", b"elm1", b"regn", b"fact",
)
# Chunks a recorder may legitimately place after the audio payload. These are
# what a truncated/unfinalized data chunk's tail is searched for.
TRAILING_METADATA_IDS = (b"iXML", b"bext", b"LIST", b"_PMX", b"axml", b"aXML", b"JUNK", b"PAD ", b"cue ", b"smpl", b"umid", b"chna")


@dataclass
class Issue:
    severity: str  # "info" | "warning" | "error"
    category: str  # one of CATEGORIES, or "other"
    code: str
    message: str
    offset: Optional[int] = None
    repairable: bool = False


@dataclass
class ChunkRecord:
    chunk_id: str
    offset: int
    declared_size: int
    size: int  # the size actually used for this chunk (ds64-resolved, clamped to EOF)
    pad_byte: Optional[int] = None  # value of the pad byte when size is odd; None if absent/even

    @property
    def data_offset(self) -> int:
        return self.offset + 8


@dataclass
class Candidate:
    chunk_id: str
    offset: int
    declared_size: int
    chains_to_eof: bool  # a valid chunk chain from here lands exactly on EOF


@dataclass
class InspectionReport:
    path: str
    file_size: int = 0
    container: Optional[str] = None  # "RIFF" | "RF64" | "BW64" | None
    riff_declared_size: Optional[int] = None
    chunks: list[ChunkRecord] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    audio: dict[str, object] = field(default_factory=dict)
    data: dict[str, object] = field(default_factory=dict)
    bext: dict[str, object] = field(default_factory=dict)
    ixml: dict[str, object] = field(default_factory=dict)

    def add(self, severity: str, category: str, code: str, message: str, offset: Optional[int] = None, repairable: bool = False) -> None:
        self.issues.append(Issue(severity, category, code, message, offset, repairable))

    def chunk(self, chunk_id: str) -> Optional[ChunkRecord]:
        return next((chunk for chunk in self.chunks if chunk.chunk_id == chunk_id), None)

    @property
    def worst_severity(self) -> str:
        return max((issue.severity for issue in self.issues), key=SEVERITY_ORDER.__getitem__, default="ok")

    def counts(self) -> dict[str, int]:
        result = {"error": 0, "warning": 0, "info": 0}
        for issue in self.issues:
            result[issue.severity] += 1
        return result

    def category_status(self, category: str) -> str:
        """"ok"/"info"/"warning"/"error" from this category's own issues, or
        "absent" for an optional chunk that simply isn't there."""
        severities = [issue.severity for issue in self.issues if issue.category == category]
        if severities:
            return max(severities, key=SEVERITY_ORDER.__getitem__)
        if category == "bext" and self.chunk("bext") is None:
            return "absent"
        if category == "ixml" and self.chunk("iXML") is None:
            return "absent"
        return "ok"

    @property
    def repairable_count(self) -> int:
        return sum(issue.repairable for issue in self.issues)

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["worst_severity"] = self.worst_severity
        result["counts"] = self.counts()
        result["category_status"] = {category: self.category_status(category) for category in CATEGORIES}
        return result


def _is_fourcc(raw: bytes) -> bool:
    return len(raw) == 4 and all(0x20 <= byte <= 0x7E for byte in raw)


def _fourcc_text(raw: bytes) -> str:
    return raw.decode("ascii", errors="replace") if _is_fourcc(raw) else raw.hex(" ")


def _read_at(handle: BinaryIO, offset: int, size: int) -> bytes:
    handle.seek(offset)
    return handle.read(size)


def _chunk_header_at(handle: BinaryIO, offset: int, file_size: int) -> Optional[tuple[bytes, int]]:
    if offset + 8 > file_size:
        return None
    raw = _read_at(handle, offset, 8)
    if len(raw) != 8:
        return None
    chunk_id, size = struct.unpack("<4sI", raw)
    return chunk_id, size


def _plausible_header(handle: BinaryIO, offset: int, file_size: int, allow_data_overrun: bool = False, allow_known_overrun: bool = False) -> bool:
    """A valid FourCC whose size fits in the file. Optionally also a `data`
    chunk, or any well-known chunk, that runs past EOF — a cut-off chunk is
    still a real chunk boundary, just a damaged one."""
    header = _chunk_header_at(handle, offset, file_size)
    if header is None or not _is_fourcc(header[0]):
        return False
    if offset + 8 + header[1] <= file_size:
        return True
    return (allow_data_overrun and header[0] == b"data") or (allow_known_overrun and header[0] in KNOWN_CHUNK_IDS)


def _chain_reaches_eof(handle: BinaryIO, offset: int, file_size: int, max_hops: int = 64) -> bool:
    """Whether walking valid chunk headers from `offset` lands exactly on
    EOF (a missing final pad byte is tolerated) — strong evidence `offset`
    really is a chunk boundary rather than four lucky bytes of audio."""
    for _hop in range(max_hops):
        if offset == file_size:
            return True
        header = _chunk_header_at(handle, offset, file_size)
        if header is None or not _is_fourcc(header[0]):
            return False
        chunk_id, size = header
        end = offset + 8 + size
        if end > file_size:
            return False
        if end == file_size:
            return True
        offset = end + (size & 1)
    return False


def _candidate_payload_plausible(handle: BinaryIO, chunk_id: bytes, offset: int, size: int) -> bool:
    if chunk_id == b"fmt ":
        return size in (16, 18, 40)
    if chunk_id == b"bext":
        return size >= BEXT_MIN_SIZE
    if chunk_id in (b"iXML", b"axml", b"aXML", b"_PMX"):
        head = _read_at(handle, offset + 8, min(size, 64)).lstrip(b"\x00 \t\r\n\xef\xbb\xbf")
        return head.startswith(b"<")
    return size > 0


def _scan_signatures(handle: BinaryIO, start: int, end: int, file_size: int, wanted: tuple[bytes, ...] = KNOWN_CHUNK_IDS) -> list[Candidate]:
    start = max(start, 0)
    end = min(end, file_size)
    if end - start < 8:
        return []
    window = _read_at(handle, start, end - start)
    found: dict[int, Candidate] = {}
    for chunk_id in wanted:
        position = window.find(chunk_id)
        while position != -1:
            offset = start + position
            header = _chunk_header_at(handle, offset, file_size)
            if header is not None:
                size = header[1]
                if offset + 8 + size <= file_size and _candidate_payload_plausible(handle, chunk_id, offset, size):
                    found[offset] = Candidate(chunk_id.decode("ascii"), offset, size, _chain_reaches_eof(handle, offset, file_size))
            position = window.find(chunk_id, position + 1)
    return [found[offset] for offset in sorted(found)]


def _scan_after_break(handle: BinaryIO, offset: int, file_size: int) -> list[Candidate]:
    candidates = _scan_signatures(handle, offset, offset + SIGNATURE_SCAN_WINDOW, file_size)
    tail_start = max(offset + SIGNATURE_SCAN_WINDOW, file_size - SIGNATURE_SCAN_WINDOW)
    if tail_start < file_size:
        candidates += _scan_signatures(handle, tail_start, file_size, file_size)
    return candidates


def _trailing_metadata_start(handle: BinaryIO, data_offset: int, file_size: int) -> Optional[Candidate]:
    """The earliest metadata chunk found in the tail of an audio region whose
    declared size can't be trusted, provided a valid chain from it reaches
    EOF exactly. Only chains to EOF count — a lone "LIST" inside audio is
    four random bytes far more often than it's a real chunk."""
    scan_start = max(data_offset, file_size - SIGNATURE_SCAN_WINDOW)
    for candidate in _scan_signatures(handle, scan_start, file_size, file_size, TRAILING_METADATA_IDS):
        if candidate.chains_to_eof:
            return candidate
    return None


def _parse_ds64(payload: bytes) -> dict[str, int]:
    if len(payload) < 24:
        return {}
    riff_size, data_size, sample_count = struct.unpack_from("<QQQ", payload)
    return {"riff_size": riff_size, "data_size": data_size, "sample_count": sample_count}


def _inspect_fmt(report: InspectionReport, payload: bytes, offset: int) -> None:
    if len(payload) < 16:
        report.add("error", "fmt", "fmt_too_small", f"fmt chunk is only {len(payload)} bytes (needs at least 16)", offset)
        return
    format_tag, channels, sample_rate, byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", payload)
    audio_format = format_tag
    if format_tag == 0xFFFE:
        if len(payload) >= 40:
            audio_format = struct.unpack_from("<I", payload, 24)[0]
        else:
            report.add("error", "fmt", "fmt_extensible_too_small", f"WAVE_FORMAT_EXTENSIBLE fmt chunk is only {len(payload)} bytes (needs 40)", offset)
    if len(payload) not in (16, 18, 40):
        report.add("info", "fmt", "fmt_unusual_size", f"fmt chunk size {len(payload)} is unusual (expected 16, 18 or 40)", offset)
    report.audio.update({
        "format_tag": format_tag,
        "encoding": {1: "PCM", 3: "IEEE Float"}.get(audio_format, f"0x{audio_format:04X}"),
        "channels": channels,
        "sample_rate": sample_rate,
        "bits_per_sample": bits,
        "block_align": block_align,
        "byte_rate": byte_rate,
    })
    if audio_format not in (1, 3):
        report.add("info", "fmt", "fmt_unsupported_encoding", f"audio encoding 0x{audio_format:04X} is not PCM/IEEE Float — frame checks skipped", offset)
        return
    if channels == 0 or sample_rate == 0 or bits == 0:
        report.add("error", "fmt", "fmt_zero_field", f"fmt has a zero field (channels={channels}, sample rate={sample_rate}, bits={bits})", offset)
        return
    expected_align = channels * ((bits + 7) // 8)
    if block_align != expected_align:
        report.add("error", "fmt", "fmt_block_align", f"block align is {block_align}, expected {expected_align} for {channels} ch × {bits}-bit", offset)
    elif byte_rate != sample_rate * block_align:
        report.add("warning", "fmt", "fmt_byte_rate", f"byte rate is {byte_rate}, expected {sample_rate * block_align}", offset)


def _cstring(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()


def parse_bext_description(description: str) -> dict[str, str]:
    """Recorder key=value lines in bext Description (`sTAPE=`, `sSCENE=`, ...)."""
    fields = {}
    for line in re.split(r"[\r\n]+", description):
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match:
            fields[match.group(1)] = match.group(2).strip()
    return fields


def _inspect_bext(report: InspectionReport, payload: bytes, offset: int) -> None:
    if len(payload) < BEXT_MIN_SIZE:
        report.add("error", "bext", "bext_too_small", f"bext chunk is only {len(payload)} bytes (a valid one is at least {BEXT_MIN_SIZE})", offset)
        if len(payload) < 346:  # through TimeReference + Version
            return
    description = _cstring(payload[0:256])
    time_low, time_high, version = struct.unpack_from("<IIH", payload, 338)
    report.bext.update({
        "description": description,
        "description_fields": parse_bext_description(description),
        "originator": _cstring(payload[256:288]),
        "originator_reference": _cstring(payload[288:320]),
        "origination_date": _cstring(payload[320:330]),
        "origination_time": _cstring(payload[330:338]),
        "time_reference": time_low | (time_high << 32),
        "version": version,
        "coding_history": _cstring(payload[BEXT_MIN_SIZE:]) if len(payload) > BEXT_MIN_SIZE else "",
    })


def _ixml_text(root: ET.Element, path: str) -> str:
    return (root.findtext(path) or "").strip()


def _inspect_ixml(report: InspectionReport, payload: bytes, offset: int) -> None:
    body = payload.rstrip(b"\x00 \t\r\n")
    if not body:
        report.add("info", "ixml", "ixml_empty", "iXML chunk is empty", offset)
        return
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        if b"</BWFXML>" not in body:
            report.add("error", "ixml", "ixml_truncated", f"iXML is cut off (no closing </BWFXML>): {exc}", offset)
        else:
            report.add("error", "ixml", "ixml_malformed", f"iXML is not valid XML: {exc}", offset)
        return
    track_list = root.find(".//TRACK_LIST")
    tracks = list(track_list.findall("TRACK")) if track_list is not None else []
    track_count_text = _ixml_text(track_list, "TRACK_COUNT") if track_list is not None else ""
    report.ixml.update({
        "project": _ixml_text(root, "PROJECT"),
        "scene": _ixml_text(root, "SCENE"),
        "take": _ixml_text(root, "TAKE"),
        "tape": _ixml_text(root, "TAPE"),
        "note": _ixml_text(root, "NOTE"),
        "timecode_rate": _ixml_text(root, "SPEED/TIMECODE_RATE"),
        "timecode_flag": _ixml_text(root, "SPEED/TIMECODE_FLAG"),
        "track_count": int(track_count_text) if track_count_text.isdigit() else None,
        "tracks": [
            {"channel_index": _ixml_text(track, "CHANNEL_INDEX"), "interleave_index": _ixml_text(track, "INTERLEAVE_INDEX"), "name": _ixml_text(track, "NAME")}
            for track in tracks
        ],
    })
    high, low = _ixml_text(root, "SPEED/TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI"), _ixml_text(root, "SPEED/TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO")
    if high.isdigit() and low.isdigit():
        report.ixml["timestamp_samples"] = (int(high) << 32) | int(low)


def _timecode_fps(rate_text: str) -> Optional[float]:
    match = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", rate_text)
    if match and int(match.group(2)):
        return int(match.group(1)) / int(match.group(2))
    try:
        return float(rate_text) if rate_text else None
    except ValueError:
        return None


def format_time_reference(samples: int, sample_rate: int, timecode_rate: str = "", timecode_flag: str = "") -> str:
    """SMPTE when an iXML timecode rate is known, otherwise clock time."""
    if sample_rate <= 0:
        return str(samples)
    fps = _timecode_fps(timecode_rate)
    if fps:
        return samples_to_smpte(samples, sample_rate, fps, timecode_flag.upper() == "DF")
    milliseconds = round(samples * 1000 / sample_rate)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"


def _resolve_data_region(report: InspectionReport, handle: BinaryIO, chunk: ChunkRecord, ds64: dict[str, int]) -> tuple[int, Optional[int]]:
    """Works out how much audio this data chunk really holds and returns the
    offset the walk should continue from. Never trusts an unconfirmed size:
    a size that overruns EOF, or a placeholder (0 / 0xFFFFFFFF) that was
    never finalized, is resolved against the real file, and any metadata
    found in the tail is excluded from the audio rather than counted as it.
    Also returns the unpadded end of the payload when its size is odd, for
    the walk's missing-pad-byte check."""
    file_size = report.file_size
    data_offset = chunk.data_offset
    declared = chunk.declared_size
    rf64 = report.container in ("RF64", "BW64")
    if rf64 and declared == UINT32_MAX:
        if "data_size" in ds64:
            declared = ds64["data_size"]
        else:
            report.add("error", "data", "rf64_data_size_unknown", "RF64 data size is 0xFFFFFFFF but there's no usable ds64 chunk to resolve it", chunk.offset)
    report.data.update({"offset": data_offset, "declared_size": declared})

    unfinalized = declared == 0 or (not rf64 and declared == UINT32_MAX)
    overruns = data_offset + declared > file_size
    if unfinalized or overruns:
        trailing = _trailing_metadata_start(handle, data_offset, file_size)
        available_end = trailing.offset if trailing is not None else file_size
        available = available_end - data_offset
        if trailing is not None:
            report.candidates.append(trailing)
            report.add("info", "data", "metadata_after_audio", f"{trailing.chunk_id} chunk found after the audio at offset {trailing.offset} — excluded from the audio region", trailing.offset)
        if unfinalized:
            placeholder = "0" if declared == 0 else "0xFFFFFFFF"
            report.add("error", "data", "data_unfinalized", f"data size is {placeholder} — the header was never finalized (typical of a recording interrupted by power loss); {available:,} bytes of audio are present", chunk.offset, repairable=True)
        else:
            missing = data_offset + declared - file_size
            report.add("error", "data", "data_truncated", f"data declares {declared:,} bytes but only {available:,} are present ({missing:,} missing — file truncated or copy incomplete)", chunk.offset, repairable=True)
        report.data["available_size"] = available
        chunk.size = available
        return available_end, None

    chunk.size = declared
    end = data_offset + declared
    odd_end = end if declared & 1 else None
    if odd_end is not None and end == file_size:
        report.add("warning", "container", "missing_final_pad", "data has an odd size but the file ends without its pad byte", end, repairable=True)
        odd_end = None
    next_offset = end + (declared & 1)
    if file_size - next_offset >= 8 and not _plausible_header(handle, next_offset, file_size, allow_known_overrun=True) and not (odd_end is not None and _plausible_header(handle, end, file_size, allow_known_overrun=True)):
        # Nothing valid follows the declared end. Either the recorder stopped
        # updating the header mid-take and the audio keeps going, or a chunk
        # header right there is itself damaged — this can't tell which, so it
        # says so. A 4 GiB wrap of a plain-RIFF size is the one unambiguous case.
        trailing = _trailing_metadata_start(handle, next_offset, file_size)
        extra_end = trailing.offset if trailing is not None else file_size
        if trailing is not None:
            report.candidates.append(trailing)
        else:
            report.candidates.extend(_scan_signatures(handle, next_offset, next_offset + SIGNATURE_SCAN_WINDOW, file_size, (b"iXML", b"bext", b"axml", b"_PMX")))
        if not rf64 and file_size > UINT32_MAX:
            report.add("error", "data", "data_size_wrapped", f"data size {declared:,} looks wrapped past 4 GiB — the audio actually runs {extra_end - data_offset:,} bytes", chunk.offset, repairable=True)
        else:
            report.add("warning", "data", "data_extends_past_declared", f"{extra_end - end:,} bytes after the declared end of data have no valid chunk structure — either the audio continues past its declared size (header not updated after recording stopped), a chunk header there is damaged, or it is stray data appended to the file", end, repairable=True)
        report.data["available_size"] = extra_end - data_offset
        report.data["declared_available_size"] = declared
        return extra_end, None
    report.data["available_size"] = declared
    return next_offset, odd_end


def inspect_wav(wav_path: Path) -> InspectionReport:
    report = InspectionReport(path=str(wav_path))
    try:
        report.file_size = wav_path.stat().st_size
        handle = wav_path.open("rb")
    except OSError as exc:
        report.add("error", "container", "unreadable", f"cannot open file: {exc}")
        return report
    with handle:
        _walk(report, handle)
    _cross_checks(report)
    return report


def _walk(report: InspectionReport, handle: BinaryIO) -> None:
    file_size = report.file_size
    header = _read_at(handle, 0, 12)
    if len(header) < 12:
        report.add("error", "container", "file_too_small", f"file is only {file_size} bytes — too small to be a WAV")
        return
    riff_id, riff_size, wave_id = struct.unpack("<4sI4s", header)
    if riff_id in (b"RIFF", b"RF64", b"BW64"):
        report.container = riff_id.decode("ascii")
    else:
        report.add("error", "container", "not_riff", f"file does not start with RIFF/RF64 (found {_fourcc_text(riff_id)})", 0)
        report.candidates = _scan_after_break(handle, 0, file_size)
        return
    report.riff_declared_size = riff_size
    if wave_id != b"WAVE":
        report.add("error", "container", "not_wave", f"RIFF form type is {_fourcc_text(wave_id)}, not WAVE", 8)

    ds64: dict[str, int] = {}
    offset = 12
    previous_odd_end: Optional[int] = None  # end of the previous chunk's payload, when its size was odd
    while offset < file_size:
        chunk_header = _chunk_header_at(handle, offset, file_size)
        if chunk_header is None:
            tail = _read_at(handle, offset, file_size - offset)
            if tail.strip(b"\x00"):
                report.add("warning", "container", "trailing_bytes", f"{len(tail)} stray byte(s) after the last chunk", offset)
            else:
                report.add("info", "container", "trailing_zero_bytes", f"{len(tail)} zero byte(s) after the last chunk", offset)
            break
        chunk_id, size = chunk_header
        if not _plausible_header(handle, offset, file_size, allow_data_overrun=True) and previous_odd_end is not None and _plausible_header(handle, previous_odd_end, file_size, allow_data_overrun=True):
            previous = report.chunks[-1].chunk_id
            # The "pad byte" read for it was really the next chunk's first byte.
            report.issues = [issue for issue in report.issues if not (issue.code == "nonzero_pad" and issue.offset == previous_odd_end)]
            report.add("warning", "container", "missing_pad_byte", f"{previous} has an odd size but no pad byte — the next chunk starts one byte early", previous_odd_end, repairable=True)
            report.chunks[-1].pad_byte = None
            offset = previous_odd_end
            previous_odd_end = None
            continue
        if not _is_fourcc(chunk_id):
            end = offset + 8 + size
            next_offset = end + (size & 1)
            if end <= file_size and (next_offset >= file_size or _plausible_header(handle, next_offset, file_size, allow_data_overrun=True)):
                # Only the ID is damaged: the size still leads exactly to the
                # next valid chunk (or EOF), so the structure itself holds —
                # readers like sox just skip it. Its contents are what's lost.
                looks_like_xml = _read_at(handle, offset + 8, min(size, 64)).lstrip(b"\x00 \t\r\n\xef\xbb\xbf").startswith(b"<")
                guess = " — its contents look like iXML, which is now unreadable" if looks_like_xml else ""
                report.add("warning", "ixml" if looks_like_xml else "container", "corrupt_chunk_id", f"chunk at offset {offset} has a damaged ID ({_fourcc_text(chunk_id)}) but an intact size; its {size:,} bytes are skipped{guess}", offset)
                report.chunks.append(ChunkRecord(_fourcc_text(chunk_id), offset, size, size))
                previous_odd_end = end if size & 1 and end < file_size else None
                offset = min(next_offset, file_size)
                continue
            candidates = _scan_after_break(handle, offset, file_size)
            if {"fmt ", "data"} <= {chunk.chunk_id for chunk in report.chunks} and not candidates:
                # Everything essential is already in place and nothing that
                # looks like a chunk follows: stray bytes appended to the
                # file, not a broken structure.
                report.add("warning", "container", "trailing_bytes", f"{file_size - offset:,} stray byte(s) after the last chunk", offset)
                break
            report.add("error", "container", "invalid_chunk_id", f"chunk structure breaks at offset {offset}: {_fourcc_text(chunk_id)} is not a valid chunk ID", offset)
            report.candidates.extend(candidates)
            break
        chunk_text = chunk_id.decode("ascii")
        record = ChunkRecord(chunk_text, offset, size, size)
        report.chunks.append(record)

        if chunk_id == b"data":
            offset, previous_odd_end = _resolve_data_region(report, handle, record, ds64)
            continue

        end = offset + 8 + size
        if end > file_size:
            available = max(file_size - offset - 8, 0)
            record.size = available
            if available == 0:
                # Seen on real Cantar files: the final iXML header made it to
                # disk, its reserved contents never did. Everything before it
                # (audio included) is intact.
                message = f"{chunk_text} header is present but none of its {size:,} bytes were written — the file ends right after the header (recording likely stopped before this final metadata was saved)"
            else:
                message = f"{chunk_text} declares {size:,} bytes but only {available:,} remain in the file — it was cut off"
            report.add("error", _category_for(chunk_text), "chunk_overruns_eof", message, offset, repairable=True)
            if available:
                _inspect_payload(report, chunk_id, _read_at(handle, offset + 8, available), offset, ds64)
            break
        _inspect_payload(report, chunk_id, _read_at(handle, offset + 8, size) if chunk_id in (b"fmt ", b"bext", b"iXML", b"ds64") else b"", offset, ds64)
        if size & 1:
            if end == file_size:
                report.add("warning", "container", "missing_final_pad", f"{chunk_text} has an odd size but the file ends without its pad byte", end, repairable=True)
            else:
                record.pad_byte = _read_at(handle, end, 1)[0]
                if record.pad_byte:
                    report.add("info", "container", "nonzero_pad", f"{chunk_text}'s pad byte is 0x{record.pad_byte:02X} instead of 0x00", end)
            previous_odd_end = end
            offset = end + 1
        else:
            previous_odd_end = None
            offset = end
        if chunk_id == b"ds64":
            ds64.update(_parse_ds64(_read_at(handle, record.data_offset, size)))

    _container_checks(report, ds64)


def _category_for(chunk_text: str) -> str:
    return {"fmt ": "fmt", "bext": "bext", "iXML": "ixml", "data": "data"}.get(chunk_text, "container")


def _inspect_payload(report: InspectionReport, chunk_id: bytes, payload: bytes, offset: int, ds64: dict[str, int]) -> None:
    if chunk_id == b"fmt ":
        if "channels" in report.audio:
            return  # duplicates are reported in _container_checks
        _inspect_fmt(report, payload, offset)
    elif chunk_id == b"bext" and not report.bext:
        _inspect_bext(report, payload, offset)
    elif chunk_id == b"iXML" and not report.ixml:
        _inspect_ixml(report, payload, offset)


def _riff_gap_is_cut_off_chunk(report: InspectionReport) -> bool:
    """Whether the RIFF size overshoots the file by exactly what the last,
    cut-off chunk is missing — i.e. the header was written for a complete
    file and only that chunk's tail never reached the disk."""
    if not report.chunks or report.riff_declared_size is None:
        return False
    last = report.chunks[-1]
    if last.chunk_id == "data" or last.size >= last.declared_size:
        return False
    missing = last.declared_size + (last.declared_size & 1) - last.size
    return report.riff_declared_size - (report.file_size - 8) in (missing, missing - (last.declared_size & 1))


def _container_checks(report: InspectionReport, ds64: dict[str, int]) -> None:
    file_size = report.file_size
    ids = [chunk.chunk_id for chunk in report.chunks]
    structure_broke = any(issue.code in ("invalid_chunk_id", "chunk_overruns_eof") for issue in report.issues)

    if report.container in ("RF64", "BW64"):
        if not ds64:
            report.add("error", "container", "rf64_missing_ds64", f"{report.container} file has no usable ds64 chunk")
        elif ds64["riff_size"] != file_size - 8:
            report.add("error" if ds64["riff_size"] > file_size - 8 else "warning", "container", "rf64_riff_size_mismatch", f"ds64 RIFF size is {ds64['riff_size']:,}, the file holds {file_size - 8:,}", repairable=True)
    elif report.riff_declared_size is not None:
        if file_size - 8 > UINT32_MAX:
            report.add("error", "container", "riff_over_4gb", f"file is {file_size:,} bytes — over the 4 GiB limit of plain RIFF (needs RF64)", 4)
        elif report.riff_declared_size != file_size - 8 and _riff_gap_is_cut_off_chunk(report):
            cut = report.chunks[-1]
            report.add("error", "container", "riff_size_mismatch", f"RIFF size is {report.riff_declared_size:,}, the file holds {file_size - 8:,} — the difference is exactly the missing {cut.chunk_id.strip()} contents", 4, repairable=True)
        elif report.riff_declared_size != file_size - 8 and not any(
            # A RIFF size that stops exactly where harmless trailing zero
            # bytes begin is correct, and those bytes already have their note.
            issue.code == "trailing_zero_bytes" and issue.offset == report.riff_declared_size + 8 for issue in report.issues
        ):
            severity = "error" if report.riff_declared_size > file_size - 8 else "warning"
            report.add(severity, "container", "riff_size_mismatch", f"RIFF size is {report.riff_declared_size:,}, the file holds {file_size - 8:,}", 4, repairable=True)

    for chunk_id in ("fmt ", "data", "bext", "iXML", "ds64"):
        if ids.count(chunk_id) > 1:
            report.add("warning", _category_for(chunk_id), "duplicate_chunk", f"{ids.count(chunk_id)} {chunk_id.strip()} chunks — only the first is used")
    if "fmt " not in ids:
        report.add("error", "fmt", "missing_fmt", "no fmt chunk" + (" before the structure broke" if structure_broke else ""))
    if "data" not in ids:
        report.add("error", "data", "missing_data", "no data chunk" + (" before the structure broke" if structure_broke else ""))
    if "fmt " in ids and "data" in ids and ids.index("fmt ") > ids.index("data"):
        report.add("warning", "fmt", "fmt_after_data", "fmt comes after data — some readers can't open this")


def _cross_checks(report: InspectionReport) -> None:
    block_align = int(report.audio.get("block_align") or 0)
    sample_rate = int(report.audio.get("sample_rate") or 0)
    available = report.data.get("available_size")
    if isinstance(available, int) and block_align > 0 and not any(issue.code.startswith("fmt_block") for issue in report.issues):
        remainder = available % block_align
        if remainder:
            report.add("warning", "data", "data_not_frame_aligned", f"audio is {available:,} bytes — {remainder} byte(s) short of a whole {block_align}-byte frame", report.data.get("offset"), repairable=True)
        frames = available // block_align
        report.audio["frames"] = frames
        if sample_rate:
            report.audio["duration_seconds"] = frames / sample_rate

    channels = report.audio.get("channels")
    track_count = report.ixml.get("track_count")
    if isinstance(channels, int) and isinstance(track_count, int) and track_count != channels:
        report.add("info", "ixml", "ixml_track_count_mismatch", f"iXML TRACK_COUNT is {track_count}, the audio has {channels} channel(s)")

    time_reference = report.bext.get("time_reference")
    timestamp = report.ixml.get("timestamp_samples")
    if isinstance(time_reference, int) and isinstance(timestamp, int) and time_reference != timestamp:
        report.add("info", "bext", "timestamp_mismatch", f"bext TimeReference ({time_reference}) differs from iXML timestamp ({timestamp})")
    if isinstance(time_reference, int) and sample_rate:
        try:
            report.bext["start_timecode"] = format_time_reference(time_reference, sample_rate, str(report.ixml.get("timecode_rate", "")), str(report.ixml.get("timecode_flag", "")))
        except ValueError:
            pass


SEVERITY_SYMBOLS = {"ok": "✓", "info": "ℹ", "warning": "⚠", "error": "✗", "absent": "—"}


def format_duration(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours}:{minutes:02}:{secs:02}.{milliseconds:03}" if hours else f"{minutes:02}:{secs:02}.{milliseconds:03}"


def summary_line(report: InspectionReport) -> str:
    counts = report.counts()
    if not counts["error"] and not counts["warning"]:
        return "OK" if not counts["info"] else f"OK ({counts['info']} note{'s' * (counts['info'] != 1)})"
    parts = [f"{counts[name]} {label}{'s' * (counts[name] != 1)}" for name, label in (("error", "error"), ("warning", "warning")) if counts[name]]
    return ", ".join(parts)


def format_report_text(report: InspectionReport, include_metadata: bool = True) -> str:
    """The full human-readable report — shared by Wave Repair's detail pane
    and Wave Manager's Audio Inspector STRUCTURE section."""
    lines = [f"Status: {summary_line(report)}"]
    if report.repairable_count:
        lines.append(f"Repairable issues: {report.repairable_count}")
    lines += ["", "Container"]
    lines.append(f"  Type: {report.container or '-'}    File size: {report.file_size:,} bytes")
    if report.riff_declared_size is not None:
        lines.append(f"  RIFF size: {report.riff_declared_size:,}  (file holds {max(report.file_size - 8, 0):,})")
    for category in CATEGORIES:
        status = report.category_status(category)
        lines.append(f"  {SEVERITY_SYMBOLS[status]} {category if category != 'ixml' else 'iXML'}")

    # Issues right under the summary — a recorder file can list dozens of
    # chunks, and the problems shouldn't be below all of them.
    if report.issues:
        lines += ["", "Issues"]
        for issue in sorted(report.issues, key=lambda issue: -SEVERITY_ORDER[issue.severity]):
            fix = "  [repairable]" if issue.repairable else ""
            lines.append(f"  {SEVERITY_SYMBOLS[issue.severity]} {issue.message}{fix}")

    lines += ["", "Chunks"]
    for chunk in report.chunks:
        size_note = f"{chunk.declared_size:,}" if chunk.size == chunk.declared_size else f"{chunk.declared_size:,} declared → {chunk.size:,} usable"
        pad = f"  pad=0x{chunk.pad_byte:02X}" if chunk.pad_byte else ""
        lines.append(f"  {chunk.chunk_id!r:8} @ {chunk.offset:>12,}  size {size_note}{pad}")
    if not report.chunks:
        lines.append("  (none readable)")
    if report.candidates:
        lines += ["", "Chunk signatures found (candidates, not confirmed)"]
        for candidate in report.candidates:
            chain = "  — chains cleanly to EOF" if candidate.chains_to_eof else ""
            lines.append(f"  {candidate.chunk_id!r:8} @ {candidate.offset:>12,}  size {candidate.declared_size:,}{chain}")

    if report.audio:
        lines += ["", "Audio"]
        audio = report.audio
        lines.append(f"  {audio.get('encoding', '-')}  {audio.get('sample_rate', '-')} Hz  {audio.get('bits_per_sample', '-')}-bit  {audio.get('channels', '-')} ch")
        if "frames" in audio:
            duration = format_duration(float(audio.get("duration_seconds", 0)))
            lines.append(f"  Frames: {audio['frames']:,}   Duration: {duration}")
    if report.data:
        data = report.data
        declared, available = data.get("declared_size"), data.get("available_size")
        line = f"  data @ {data.get('offset', 0):,}: declared {declared:,}" if isinstance(declared, int) else ""
        if line and isinstance(available, int) and available != declared:
            line += f", actually present {available:,}"
        if line:
            lines.append(line)

    if include_metadata and report.bext:
        bext = report.bext
        lines += ["", "BEXT"]
        for label, key in (("Description", "description"), ("Originator", "originator"), ("Date/Time", None), ("TimeReference", "time_reference"), ("Start TC", "start_timecode")):
            if key is None:
                value = f"{bext.get('origination_date', '')} {bext.get('origination_time', '')}".strip()
            else:
                value = str(bext.get(key, "")).replace("\r\n", " | ").replace("\n", " | ").replace("\r", " | ")
            if value:
                lines.append(f"  {label}: {value}")
    if include_metadata and report.ixml:
        ixml = report.ixml
        lines += ["", "iXML"]
        for label, key in (("Project", "project"), ("Scene", "scene"), ("Take", "take"), ("Tape", "tape"), ("TC Rate", "timecode_rate"), ("Track Count", "track_count")):
            value = ixml.get(key)
            if value not in (None, ""):
                lines.append(f"  {label}: {value}")
        names = [track["name"] for track in ixml.get("tracks", []) if track.get("name")]
        if names:
            lines.append(f"  Tracks: {', '.join(names)}")
    return "\n".join(lines)


def format_structure_brief(report: InspectionReport) -> str:
    """Wave Manager's Audio Inspector STRUCTURE section: one line when the
    file is structurally sound, the full structural report (no metadata —
    the Inspector shows that itself) when it isn't."""
    if report.worst_severity in ("ok", "info"):
        chunk_list = ", ".join(chunk.chunk_id.strip() for chunk in report.chunks)
        lines = [f"Status: OK — {report.container}, chunks: {chunk_list}"]
        lines += [f"{SEVERITY_SYMBOLS['info']} {issue.message}" for issue in report.issues]
        return "\n".join(lines)
    return format_report_text(report, include_metadata=False)
