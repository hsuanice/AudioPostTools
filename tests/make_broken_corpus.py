"""Build a corpus of broken WAV files modelled on common real-world failures,
then check what Wave Repair for Post makes of each one.

    python3 tests/make_broken_corpus.py [output folder]
    (default: ./Broken Corpus in the current folder)

Every file is generated from scratch (playable sine tones, Sound Devices- or
Cantar-style metadata) and then broken in exactly one realistic way. Each
case records the issue codes the Inspector must report and what the app
should do with it; the run ends with a pass/fail table and writes
README.md + manifest.csv into the corpus folder.
"""
from __future__ import annotations

import csv
import math
import shutil
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

import wav_factory as wf  # noqa: E402
from operations.riff_inspect import inspect_wav  # noqa: E402
from operations.riff_repair import PLAN_DEFERRED, PLAN_NONE, PLAN_REPAIR, plan_repair, rebuild_target_problem  # noqa: E402
from operations.audio_recover import recover_kind  # noqa: E402

DEFAULT_OUT = Path.cwd() / "Broken Corpus"
RATE, BITS, CHANNELS = 48000, 24, 2
BLOCK = CHANNELS * BITS // 8


# --- building blocks -------------------------------------------------------------

def tone(seconds: float = 2.0, freqs: tuple[float, ...] = (440.0, 660.0), level_db: float = -18.0) -> bytes:
    """Interleaved 24-bit PCM, one sine per channel — audible in any DAW."""
    amplitude = (2 ** 23 - 1) * 10 ** (level_db / 20)
    frames = int(RATE * seconds)
    out = bytearray(frames * BLOCK)
    pos = 0
    for n in range(frames):
        for freq in freqs[:CHANNELS]:
            value = int(amplitude * math.sin(2 * math.pi * freq * n / RATE))
            out[pos:pos + 3] = (value & 0xFFFFFF).to_bytes(3, "little")
            pos += 3
    return bytes(out)


AUDIO = tone()


def fmt() -> bytes:
    return wf.fmt_payload(channels=CHANNELS, sample_rate=RATE, bits=BITS)


def bext(scene: str = "12A", take: str = "3") -> bytes:
    description = f"sSPEED=025.000-ND\r\nsTAKE={take}\r\nsSCENE={scene}\r\nsTAPE=26Y01M14\r\nsTRK1=BOOM\r\nsTRK2=LAV\r\n"
    return wf.bext_payload(description, time_reference=RATE * (14 * 3600 + 32 * 60 + 5))


def ixml(scene: str = "12A", take: str = "3", pad: int = 0, filler: bytes = b" ") -> bytes:
    text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<BWFXML>\n <IXML_VERSION>1.61</IXML_VERSION>\n <PROJECT>CORPUS</PROJECT>\n'
        f" <SCENE>{scene}</SCENE>\n <TAKE>{take}</TAKE>\n <TAPE>26Y01M14</TAPE>\n"
        " <SPEED>\n  <TIMECODE_RATE>25/1</TIMECODE_RATE>\n  <TIMECODE_FLAG>NDF</TIMECODE_FLAG>\n </SPEED>\n"
        " <TRACK_LIST>\n  <TRACK_COUNT>2</TRACK_COUNT>\n"
        "  <TRACK>\n   <CHANNEL_INDEX>1</CHANNEL_INDEX>\n   <INTERLEAVE_INDEX>1</INTERLEAVE_INDEX>\n   <NAME>BOOM</NAME>\n  </TRACK>\n"
        "  <TRACK>\n   <CHANNEL_INDEX>2</CHANNEL_INDEX>\n   <INTERLEAVE_INDEX>2</INTERLEAVE_INDEX>\n   <NAME>LAV</NAME>\n  </TRACK>\n"
        " </TRACK_LIST>\n</BWFXML>\n"
    ).encode()
    return text + filler * pad


def ch(chunk_id: bytes, payload: bytes, **kwargs) -> bytes:
    return wf.chunk(chunk_id, payload, **kwargs)


def healthy(meta_after_data: bool = False) -> list[bytes]:
    meta = [ch(b"bext", bext()), ch(b"iXML", ixml())]
    return [ch(b"fmt ", fmt())] + ([ch(b"data", AUDIO)] + meta if meta_after_data else meta + [ch(b"data", AUDIO)])


def set_u32(data: bytes, offset: int, value: int) -> bytes:
    out = bytearray(data)
    struct.pack_into("<I", out, offset, value & 0xFFFFFFFF)
    return bytes(out)


def offset_of(data: bytes, chunk_id: bytes) -> int:
    """Offset of a chunk header found by walking the (still intact) structure."""
    position = 12
    while position + 8 <= len(data):
        cid, size = struct.unpack_from("<4sI", data, position)
        if cid == chunk_id:
            return position
        position += 8 + size + (size & 1)
    raise KeyError(chunk_id)


def odd_ixml() -> bytes:
    payload = ixml()
    return payload if len(payload) & 1 else payload + b" "


# --- the cases -------------------------------------------------------------------

@dataclass
class Case:
    folder: str
    name: str
    cause: str            # the real-world cause
    expect: set[str]      # issue codes the Inspector must report (subset)
    action: str           # what the app should offer
    build: object = None  # callable(path) or bytes
    note: str = ""
    extra: dict = field(default_factory=dict)


def cases() -> list[Case]:
    full = wf.riff(healthy())
    full_meta_after = wf.riff(healthy(meta_after_data=True))
    data_at = offset_of(full, b"data")
    full_ixml_last = wf.riff([ch(b"fmt ", fmt()), ch(b"bext", bext()), ch(b"data", AUDIO), ch(b"iXML", ixml())])
    meta_after_data_at = offset_of(full_meta_after, b"data")
    out: list[Case] = []
    add = out.append

    # 01 recording interrupted
    f = "01 Recording interrupted (power loss, card pulled)"
    add(Case(f, "data size 0 (header never finalized).wav", "Power lost while recording: the recorder writes data size 0 at the start and fills it in at the end, so it stays 0; the RIFF size also keeps its starting value",
             {"data_unfinalized", "riff_size_mismatch"}, "Recover audio",
             set_u32(set_u32(full, data_at + 4, 0), 4, data_at + 8 - 8)))
    add(Case(f, "data size 0xFFFFFFFF placeholder.wav", "Some recorders (certain Zoom and Tascam models) use 0xFFFFFFFF as a placeholder, which stays there after a power loss",
             {"data_unfinalized"}, "Recover audio", set_u32(full, data_at + 4, 0xFFFFFFFF)))
    add(Case(f, "header updated only partway (audio continues).wav", "The recorder updates the header only every few seconds, so after a power loss the header length is shorter than the audio recorded",
             {"data_extends_past_declared"}, "Recover audio", set_u32(full, data_at + 4, len(AUDIO) // 2)))
    unfinal_after = set_u32(full_meta_after, meta_after_data_at + 4, 0)
    add(Case(f, "data size 0 with metadata written after audio.wav", "A recorder that writes bext/iXML after the audio lost power before filling in the data size; the data after it must not be read as audio",
             {"data_unfinalized", "metadata_after_audio"}, "Recover audio", unfinal_after))

    # 02 copy / transfer
    f = "02 Copy and transfer problems"
    add(Case(f, "copy interrupted mid-audio.wav", "Copy or download interrupted: the file is cut off in the middle of the audio",
             {"data_truncated", "riff_size_mismatch"}, "Recover audio", full[: data_at + 8 + len(AUDIO) // 2 + 1]))
    add(Case(f, "copy interrupted inside final iXML.wav", "A file with iXML at the end, cut off in the middle of the iXML while copying",
             {"chunk_overruns_eof", "ixml_truncated", "riff_size_mismatch"}, "Not repairable yet", full_ixml_last[:-120]))
    zero_head = bytearray(full)
    zero_head[:4096] = b"\x00" * 4096
    add(Case(f, "first 4 KB zeroed (bad card sector).wav", "Bad card sector or damaged file system: the first sectors are zeros and the whole header is gone",
             {"not_riff"}, "Not repairable yet", bytes(zero_head), note="Inspector lists the chunk signatures it can still find as candidates"))
    add(Case(f, "trailing garbage after final iXML.wav", "A copy tool or the file system left extra data at the end (after the final iXML)",
             {"trailing_bytes"}, "Not repairable yet", full_ixml_last + b"\x13\x37garbage"))
    add(Case(f, "trailing garbage right after audio.wav", "Extra data at the end again, but right after the audio: it can't be told apart from audio the header never counted, so the options are listed and nothing is repaired automatically",
             {"data_extends_past_declared"}, "Recover audio", full + b"\x13\x37garbage",
             note="genuinely ambiguous: stray bytes, unrecorded-in-header audio and a damaged chunk header look the same here"))
    add(Case(f, "trailing zero bytes.wav", "A few zero bytes at the end (left by sector alignment); harmless",
             {"trailing_zero_bytes"}, "OK (note only)", full + b"\x00" * 6))
    add(Case(f, "._AppleDouble companion.wav", "A hidden file macOS creates on exFAT cards (name starts with ._); not an audio file",
             {"not_riff"}, "Skipped by the app", b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        " + b"\x00" * 64,
             note="file name starts with ._ in the corpus; Browse skips it"))
    add(Case(f, "empty file (0 bytes).wav", "Nothing was written after the file was created (recording failed at once, or the copy failed)", {"file_too_small"}, "Not repairable yet", b""))
    add(Case(f, "raw PCM without header.wav", "Raw audio recovered by a rescue tool, with no WAV header at all", {"not_riff"}, "Not repairable yet", AUDIO[:96000]))

    # 03 writer bugs
    f = "03 Software wrote it wrong"
    add(Case(f, "RIFF size too small.wav", "Software appended data but did not update the RIFF size",
             {"riff_size_mismatch"}, "Will repair", set_u32(full, 4, len(full) - 8 - 300)))
    add(Case(f, "RIFF size too large.wav", "The RIFF size is larger than the file: a whole section may be missing, so the number can't just be corrected",
             {"riff_size_mismatch"}, "Not repairable yet", set_u32(full, 4, len(full) - 8 + 61448)))
    add(Case(f, "missing final pad byte.wav", "The final chunk has an odd size but no pad byte (Cantar's iXML, for example); some strict tools refuse to edit the file",
             {"missing_final_pad"}, "Will repair",
             wf.riff([ch(b"fmt ", fmt()), ch(b"bext", bext()), ch(b"data", AUDIO), ch(b"iXML", odd_ixml(), pad=None)])))
    add(Case(f, "missing pad byte between chunks.wav", "An odd-sized chunk in the middle has no pad byte, so every chunk after it is off by one byte",
             {"missing_pad_byte"}, "Not repairable yet",
             wf.riff([ch(b"fmt ", fmt()), ch(b"iXML", odd_ixml(), pad=None), ch(b"bext", bext()), ch(b"data", AUDIO)])))
    add(Case(f, "non-zero pad byte.wav", "The pad byte is not zero (common in Pro Tools libraries); harmless",
             {"nonzero_pad"}, "OK (note only)",
             wf.riff([ch(b"fmt ", fmt()), ch(b"iXML", odd_ixml(), pad=b"\xAA"), ch(b"data", AUDIO)])))
    add(Case(f, "audio not frame aligned.wav", "The audio data length is not a whole number of sample frames (a few extra bytes)",
             {"data_not_frame_aligned"}, "Not repairable yet",
             wf.riff([ch(b"fmt ", fmt()), ch(b"data", AUDIO + b"\x01\x02\x03\x04")])))
    add(Case(f, "fmt block align wrong.wav", "The block align in fmt doesn't match the channels and bit depth",
             {"fmt_block_align"}, "Not repairable yet",
             wf.riff([ch(b"fmt ", wf.fmt_payload(channels=CHANNELS, sample_rate=RATE, bits=BITS, block_align=4)), ch(b"data", AUDIO)])))
    bad_rate = bytearray(fmt())
    struct.pack_into("<I", bad_rate, 8, 12345)
    add(Case(f, "fmt byte rate wrong.wav", "The byte rate in fmt is wrong (common with old software); most players ignore it",
             {"fmt_byte_rate"}, "Not repairable yet", wf.riff([ch(b"fmt ", bytes(bad_rate)), ch(b"data", AUDIO)])))
    add(Case(f, "two data chunks.wav", "Software wrote two data chunks", {"duplicate_chunk"}, "Not repairable yet",
             wf.riff([ch(b"fmt ", fmt()), ch(b"data", AUDIO), ch(b"data", AUDIO[:6000])])))
    add(Case(f, "fmt after data.wav", "fmt comes after data; some software can't open the file", {"fmt_after_data"}, "Not repairable yet",
             wf.riff([ch(b"data", AUDIO), ch(b"fmt ", fmt())])))
    bad_xml = ixml().replace(b"<PROJECT>CORPUS</PROJECT>", b"<PROJECT>R&D CORPUS</PROJECT>")
    add(Case(f, "iXML malformed (unescaped &).wav", "Software wrote a bare "&" into the iXML, so the XML is invalid",
             {"ixml_malformed"}, "Not repairable yet", wf.riff([ch(b"fmt ", fmt()), ch(b"bext", bext()), ch(b"iXML", bad_xml), ch(b"data", AUDIO)])))
    add(Case(f, "iXML text cut short (size consistent).wav", "Only half of the iXML text was written, but the chunk size is right (a bug in the writer)",
             {"ixml_truncated"}, "Not repairable yet", wf.riff([ch(b"fmt ", fmt()), ch(b"iXML", ixml()[:200]), ch(b"data", AUDIO)])))
    add(Case(f, "bext too short.wav", "The bext chunk is shorter than the spec (old or home-made software)", {"bext_too_small"}, "Not repairable yet",
             wf.riff([ch(b"fmt ", fmt()), ch(b"bext", bext()[:400]), ch(b"data", AUDIO)])))

    # 04 bit-level damage
    f = "04 Data damage"
    corrupt_id = bytearray(full)
    bext_at = offset_of(full, b"bext")
    corrupt_id[bext_at:bext_at + 4] = b"\x01\x02\x03\x04"
    add(Case(f, "chunk ID damaged, size intact.wav", "Storage bit error: a chunk ID is damaged but its size is intact, so the structure can still be walked (sox still plays it)",
             {"corrupt_chunk_id"}, "Not repairable yet", bytes(corrupt_id)))
    add(Case(f, "chunk size damaged.wav", "Storage bit error: a chunk size became a huge number, so the rest of the structure can't be found",
             {"chunk_overruns_eof"}, "Not repairable yet", set_u32(full, bext_at + 4, 0x7F00FFFF),
             note="Inspector lists the chunk signatures it still finds after the break as candidates"))

    # 05 large files
    f = "05 Large files (RF64)"
    rf_chunks = [ch(b"fmt ", fmt()), ch(b"data", AUDIO, declared_size=0xFFFFFFFF)]
    add(Case(f, "RF64 healthy.wav", "A healthy RF64 file (the format for files over 4 GB); should have no problem at all", set(), "OK", wf.rf64(rf_chunks, len(AUDIO))))
    add(Case(f, "RF64 without ds64.wav", "An RF64 header without a ds64 chunk, so the real length can't be known",
             {"rf64_missing_ds64", "rf64_data_size_unknown"}, "Not repairable yet", wf.rf64(rf_chunks, len(AUDIO), ds64=False)))
    rf = bytearray(wf.rf64(rf_chunks, len(AUDIO)))
    struct.pack_into("<Q", rf, 20, len(rf) + 5000)  # ds64 riff size wrong
    add(Case(f, "RF64 ds64 size wrong.wav", "The size in ds64 doesn't match the file (interrupted recording or a software bug)",
             {"rf64_riff_size_mismatch"}, "Not repairable yet", bytes(rf)))
    add(Case(f, "over 4 GB as plain RIFF (sparse).wav", "Over 4 GB written as plain WAV, so the size fields wrapped around (a sparse file that takes little real space)",
             {"riff_over_4gb"}, "Not repairable yet", build=build_over_4gb,
             note="sparse file: ~4.1 GB apparent size, a few MB on disk — copying it elsewhere expands it"))

    # 07 healthy controls
    f = "07 Healthy controls"
    add(Case(f, "healthy (bext + iXML).wav", "A healthy file; should have no problem at all", set(), "OK", full))
    add(Case(f, "healthy, iXML padded with NUL.wav", "Space reserved with NUL bytes after the iXML (common with Sound Devices and others); fine", set(), "OK",
             wf.riff([ch(b"fmt ", fmt()), ch(b"bext", bext()), ch(b"iXML", ixml(pad=512, filler=b"\x00")), ch(b"data", AUDIO)])))
    add(Case(f, "healthy, JUNK reserve chunk.wav", "A JUNK reserve chunk (many recorders and DAWs write one); fine", set(), "OK",
             wf.riff([ch(b"JUNK", b"\x00" * 92), ch(b"fmt ", fmt()), ch(b"bext", bext()), ch(b"data", AUDIO)])))
    add(Case(f, "healthy, metadata after audio.wav", "bext/iXML written after the audio (some recorders); fine", set(), "OK", full_meta_after))

    # 08 out of scope
    f = "08 Out of scope (audio damage)"
    holey = bytearray(full)
    holey[data_at + 8 + 60000: data_at + 8 + 160000] = b"\x00" * 100000
    add(Case(f, "silence block inside audio.wav", "A stretch in the middle of the audio is zeros (bad sector), but the file structure is fine",
             set(), "OK (not detectable)", bytes(holey), note="Structure is fine, so the Inspector reports OK — damage inside the audio itself is outside what it checks"))
    return out


def build_over_4gb(path: Path) -> None:
    """A plain-RIFF file whose real audio runs past 4 GiB: both size fields
    wrapped modulo 2^32. Written sparse (only the header and the last block
    are real bytes)."""
    header_fmt = ch(b"fmt ", fmt())
    real_audio = (1 << 32) + 6 * 1000
    data_header = struct.pack("<4sI", b"data", real_audio & 0xFFFFFFFF)
    head = b"RIFF" + struct.pack("<I", (4 + len(header_fmt) + 8 + real_audio) & 0xFFFFFFFF) + b"WAVE" + header_fmt + data_header
    with path.open("wb") as handle:
        handle.write(head)
        handle.seek(len(head) + real_audio - 6)
        handle.write(b"\x00" * 6)


# --- the Rebuild iXML card ------------------------------------------------------

def build_rebuild_card(folder: Path) -> list[Case]:
    """A day folder of Cantar-style takes: one cut off mid-card (both
    neighbours), one cut off at the end of the day whose file name was also
    changed by hand (scene/take no longer match its bext)."""
    import test_ixml_rebuild as T

    folder.mkdir(parents=True, exist_ok=True)
    plan = [
        (4700, "23/5", "t1", 70000, ["BOOM", "WU", ""], "k0", False),
        (4701, "23/5", "t2", 70300, ["BOOM", "WU", ""], "k1", False),
        (4702, "23/6", "t1", 70600, ["BOOM", "WU", ""], "k2", False),
        (4703, "23/6", "t2", 70900, ["BOOM", "?", "?"], "k3", True),
        (4704, "23/6", "t3", 71200, ["BOOM", "Clerk", ""], "k4", False),
        (4705, "23/7", "t1", 71500, ["BOOM", "Clerk", ""], "k5", False),
        (4706, "23/7", "t2", 71800, ["BOOM", "?", "?"], "k6", True),
    ]
    made = []
    for number, scene, take, start, names, key, cut in plan:
        written = T.write_take(folder, number, scene, take, start, names, key, cut_off=cut)
        shown_scene, shown_take = (("23 8", "t1") if number == 4706 else (scene.replace("/", " "), take))
        final = written.with_name(f"UB{number}=={shown_scene}     {shown_take}==PN.WAV")
        written.rename(final)
        made.append((number, final, cut))
    rel = folder.name
    return [
        Case(f"06 Rebuild iXML card/{rel}", made[3][1].name, "Cantar power loss: the final iXML has only its header and no content (takes before and after can be used as a reference)",
             {"chunk_overruns_eof", "riff_size_mismatch"}, "Rebuild iXML", note="the take either side disagree on track 2 → a conflict to choose"),
        Case(f"06 Rebuild iXML card/{rel}", made[6][1].name, "Same as above, but it is the last file of the day, and it was renamed by hand on a computer later (scene/take don't match bext)",
             {"chunk_overruns_eof", "riff_size_mismatch"}, "Rebuild iXML", note="file name says 23/8 t1, bext says 23/7 t2 → SCENE and TAKE become conflicts"),
    ]


# --- run --------------------------------------------------------------------------

def app_action(report) -> str:
    kind, problem = recover_kind(report)
    if kind and not problem:
        return "Recover audio"
    plan = plan_repair(report)
    if plan.status == PLAN_REPAIR:
        return "Will repair"
    if plan.status == PLAN_DEFERRED:
        return "Rebuild iXML" if rebuild_target_problem(report) == "" else "Not repairable yet"
    return "OK (note only)" if report.issues else "OK"


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    all_cases = cases()
    for case in all_cases:
        folder = out / case.folder
        folder.mkdir(parents=True, exist_ok=True)
        name = case.name if not case.name.startswith("._") else case.name
        path = folder / name
        if callable(case.build):
            case.build(path)
        else:
            path.write_bytes(case.build)
    all_cases += build_rebuild_card(out / "06 Rebuild iXML card" / "2026-01-13.AAN")

    rows, failures = [], 0
    for case in all_cases:
        path = out / case.folder / case.name
        report = inspect_wav(path)
        found = {issue.code for issue in report.issues}
        action = "Skipped by the app" if path.name.startswith("._") else app_action(report)
        expected_action = case.action
        codes_ok = case.expect <= found and (case.expect or not {i.code for i in report.issues if i.severity in ("warning", "error")})
        action_ok = action == expected_action or (expected_action.startswith("OK") and action.startswith("OK"))
        ok = codes_ok and action_ok
        failures += not ok
        rows.append({
            "folder": case.folder, "file": case.name, "result": "PASS" if ok else "FAIL",
            "real-world cause": case.cause, "expected issues": ", ".join(sorted(case.expect)) or "(none)",
            "found issues": ", ".join(sorted(found)) or "(none)", "expected action": expected_action, "app action": action, "note": case.note,
        })
    with (out / "manifest.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    lines = [
        "# Broken Corpus — test files for Wave Repair for Post", "",
        "Generated by `tests/make_broken_corpus.py` (can be regenerated any time). Each file is made from sine tones and simulated recorder metadata, then broken in one way that is common in real life.", "",
        "| Result | Folder | File | Real-world cause | Should detect | What the app does |", "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(f"| {row['result']} | {row['folder']} | {row['file']} | {row['real-world cause']} | {row['expected issues']} | {row['app action']} |")
    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    width = max(len(row["file"]) for row in rows)
    for row in rows:
        mark = "ok  " if row["result"] == "PASS" else "FAIL"
        print(f"{mark} {row['file']:<{width}}  {row['app action']:<20} found: {row['found issues']}")
        if row["result"] != "PASS":
            print(f"     expected issues: {row['expected issues']} | expected action: {row['expected action']}")
    print(f"\n{len(rows) - failures}/{len(rows)} as expected — corpus at {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
