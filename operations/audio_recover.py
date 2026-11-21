"""Recover audio whose data size in the header is wrong — confirmed per file.

Three real-world cases, all fixed the same way: the audio region is measured
from the file itself (from the data chunk to the first metadata chunk that
chains cleanly to the end of the file, or to the end of the file), and the
header's data size and RIFF size are rewritten to match. Nothing is removed;
an incomplete final sample frame is completed with zeros (appended, so no
byte moves). Every file needs the user's confirmation first:

  unfinalized — recording interrupted (power loss, card pulled): data size
                still 0 / 0xFFFFFFFF. The audio is all there.
  stale       — the header was only updated partway: bytes follow the
                declared end. They are *probably* the rest of the audio but
                could be junk — evidence (their level vs the audio before)
                is shown for the decision.
  truncated   — a copy cut short: the header promises more audio than the
                file holds. What is there becomes playable; the missing tail
                is gone for good (re-copy from the source if it still exists).
"""
from __future__ import annotations

import math
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from operations.audio_qc import _peak_from_window
from operations.riff_inspect import InspectionReport, _trailing_metadata_start, inspect_wav
from operations.riff_repair import (
    RepairResult, _copy_with_hash, _failed, _file_hash, _restore, backup_run_folder, relative_location, write_repair_log,
)

UNFINALIZED, STALE, TRUNCATED = "unfinalized", "stale", "truncated"
CATEGORIES = {
    UNFINALIZED: "Recovered audio (header never finalized)",
    STALE: "Recovered audio (header not updated)",
    TRUNCATED: "Made playable (copy cut short)",
}
KIND_BY_CODE = {"data_unfinalized": UNFINALIZED, "data_extends_past_declared": STALE, "data_truncated": TRUNCATED}
# Issues that may come along with the case and are fixed by it, or harmless.
ALLOWED_ALONGSIDE = {"riff_size_mismatch", "data_not_frame_aligned", "metadata_after_audio", "missing_final_pad"}


@dataclass
class RecoverPlan:
    path: Path
    kind: str = ""
    problem: str = ""
    data_offset: int = 0          # first audio byte
    declared: int = 0             # data size in the header now
    region_end: int = 0           # end of the audio region in the file
    new_data_size: int = 0
    append: int = 0               # zero bytes appended (frame completion + pad)
    new_riff_size: int = 0
    block_align: int = 0
    sample_rate: int = 0
    trailing_metadata: str = ""   # e.g. "iXML at 12,345,678"
    missing_bytes: int = 0        # truncated: promised but not in the file
    extra_bytes: int = 0          # stale: after the declared end
    level_db: Optional[float] = None        # audio before the declared end
    extra_level_db: Optional[float] = None  # stale: the extra region
    lines: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problem and bool(self.kind)

    @property
    def category(self) -> str:
        return CATEGORIES.get(self.kind, "")

    def seconds(self, size: int) -> float:
        return size / self.block_align / self.sample_rate if self.block_align and self.sample_rate else 0.0


def _level_db(path: Path, start: int, end: int, report: InspectionReport, windows: int = 400, window_bytes: int = 4096) -> Optional[float]:
    """Peak level (dBFS) sampled across [start, end) — None when silent."""
    bits = int(report.audio.get("bits_per_sample") or 0)
    audio_format = 3 if report.audio.get("encoding") == "IEEE Float" else 1
    block = int(report.audio.get("block_align") or 0)
    if end <= start or not bits or not block:
        return None
    peak = 0.0
    with path.open("rb") as handle:
        span = end - start
        for index in range(windows):
            offset = start + (span * index // windows)
            offset -= (offset - start) % block
            size = min(window_bytes, end - offset)
            size -= size % block
            if size <= 0:
                continue
            handle.seek(offset)
            peak = max(peak, _peak_from_window(handle.read(size), bits, audio_format))
    return 20 * math.log10(peak) if peak > 0 else None


def recover_kind(report: InspectionReport) -> tuple[str, str]:
    """(kind, problem) from the inspection alone, without reading audio —
    cheap enough for every row of the file list. kind is "" when this isn't
    a data-size case at all; problem is "" when it can be recovered."""
    codes = {issue.code for issue in report.issues}
    kinds = [KIND_BY_CODE[code] for code in KIND_BY_CODE if code in codes]
    if not kinds:
        return "", "the data size is not the problem in this file"
    if report.container != "RIFF":
        return kinds[0], "RF64/BW64 audio recovery is not supported yet"
    others = [issue for issue in report.issues if issue.severity in ("warning", "error") and issue.code not in KIND_BY_CODE and issue.code not in ALLOWED_ALONGSIDE]
    if others:
        return kinds[0], f"another problem needs attention first: {others[0].message}"
    return kinds[0], ""


def plan_recover(report: InspectionReport) -> RecoverPlan:
    path = Path(report.path)
    plan = RecoverPlan(path)
    plan.kind, plan.problem = recover_kind(report)
    if plan.problem:
        return plan
    data = report.chunk("data")
    plan.block_align = int(report.audio.get("block_align") or 0)
    plan.sample_rate = int(report.audio.get("sample_rate") or 0)
    if data is None or not plan.block_align or not plan.sample_rate:
        plan.problem = "no usable fmt/data chunk"
        return plan
    plan.data_offset = data.data_offset
    plan.declared = data.declared_size
    file_size = report.file_size
    with path.open("rb") as handle:
        trailing = _trailing_metadata_start(handle, plan.data_offset, file_size)
    plan.region_end = trailing.offset if trailing is not None else file_size
    if trailing is not None:
        plan.trailing_metadata = f"{trailing.chunk_id.strip()} at offset {trailing.offset:,}"
    region = plan.region_end - plan.data_offset
    at_eof = plan.region_end == file_size
    partial = region % plan.block_align
    if partial and not at_eof:
        plan.problem = "the audio region before the metadata isn't a whole number of sample frames — needs manual repair"
        return plan
    frame_fill = (plan.block_align - partial) % plan.block_align if at_eof else 0
    plan.new_data_size = region + frame_fill
    pad = plan.new_data_size & 1 if at_eof else 0
    plan.append = frame_fill + pad
    plan.new_riff_size = file_size + plan.append - 8
    if plan.kind == STALE:
        plan.extra_bytes = plan.region_end - (plan.data_offset + plan.declared)
        plan.level_db = _level_db(path, plan.data_offset, plan.data_offset + plan.declared, report)
        plan.extra_level_db = _level_db(path, plan.data_offset + plan.declared, plan.region_end, report)
    else:
        plan.level_db = _level_db(path, plan.data_offset, plan.region_end, report)
    if plan.kind == TRUNCATED:
        plan.missing_bytes = max(plan.declared - region, 0)
    plan.lines = _describe(plan)
    return plan


def _fmt_level(value: Optional[float]) -> str:
    return "silent" if value is None else f"{value:.1f} dBFS peak"


def _describe(plan: RecoverPlan) -> list[str]:
    seconds = plan.seconds(plan.new_data_size)
    lines = []
    if plan.kind == UNFINALIZED:
        placeholder = "0" if plan.declared == 0 else "0xFFFFFFFF"
        lines.append(f"Recording interrupted: the header still says the audio is {placeholder} bytes long — the audio itself is in the file.")
    elif plan.kind == STALE:
        lines.append(f"The header was only updated partway: it covers {plan.seconds(plan.declared):.2f} s, and {plan.extra_bytes:,} more bytes ({plan.seconds(plan.extra_bytes):.2f} s) follow with no chunk structure.")
        lines.append(f"Evidence — audio before the declared end: {_fmt_level(plan.level_db)}; the extra bytes: {_fmt_level(plan.extra_level_db)}.")
        if plan.seconds(plan.extra_bytes) < 0.01:
            lines.append(f"Only {plan.extra_bytes:,} byte(s) — less than 10 ms: far more likely junk appended to the file than the rest of a recording.")
        lines.append("Similar levels point to the rest of the recording; very few bytes, or a level unlike the audio, point to junk. Confirm only if they belong to the audio.")
    else:
        lines.append(f"Copy cut short: the header promises {plan.declared:,} bytes of audio, the file holds {plan.new_data_size - plan.append:,} — {plan.missing_bytes:,} bytes ({plan.seconds(plan.missing_bytes):.2f} s) at the end are missing and CANNOT be recovered.")
        lines.append("If the original (card / source drive) still exists, copy the file again instead.")
    lines.append(f"Result: {seconds:.3f} s of audio ({plan.new_data_size // plan.block_align:,} frames), level {_fmt_level(plan.level_db if plan.kind != STALE else max(filter(None, (plan.level_db, plan.extra_level_db)), default=None))}.")
    if plan.trailing_metadata:
        lines.append(f"Metadata after the audio kept as metadata: {plan.trailing_metadata}.")
    if plan.append:
        lines.append(f"{plan.append} zero byte(s) appended to complete the last sample frame{' and the chunk pad byte' if plan.new_data_size & 1 else ''}.")
    lines.append(f"Changed: the data size ({plan.declared:,} → {plan.new_data_size:,}) and the RIFF size; no byte is removed or moved.")
    return lines


def recover_file(plan: RecoverPlan, report: InspectionReport, run_folder: Path, scan_root: Optional[Path], step: Optional[Callable[[str], None]] = None) -> RepairResult:
    path = plan.path
    step = step or (lambda _message: None)
    result = RepairResult(str(path), "skipped", plan.category, issues_before=[issue.message for issue in report.issues], size_before=report.file_size)
    if not plan.ok:
        result.message = plan.problem or "nothing to recover"
        return result
    original_size = path.stat().st_size
    if original_size != report.file_size:
        return _failed(result, "file changed since it was inspected — rescan first")
    backup = run_folder / plan.category / relative_location(path, scan_root) / path.name
    result.backup_path = str(backup)
    try:
        step("backing up")
        result.original_sha1 = _copy_with_hash(path, backup)
        step("verifying backup")
        if backup.stat().st_size != original_size or _file_hash(backup) != result.original_sha1:
            return _failed(result, "backup copy did not verify — original not touched")
    except OSError as exc:
        return _failed(result, f"backup failed ({exc}) — original not touched")
    result.verification.append("backup verified (size + SHA-1 identical to original)")

    step("recovering")
    size_field = plan.data_offset - 4
    try:
        with path.open("r+b") as writer:
            writer.seek(size_field)
            writer.write(struct.pack("<I", plan.new_data_size))
            writer.seek(4)
            writer.write(struct.pack("<I", plan.new_riff_size))
            if plan.append:
                writer.seek(0, os.SEEK_END)
                writer.write(b"\x00" * plan.append)
    except OSError as exc:
        _restore(backup, path)
        return _failed(result, f"write failed ({exc}) — original restored from backup")

    step("verifying")
    problems = []
    if path.stat().st_size != original_size + plan.append:
        problems.append("unexpected file size")
    with backup.open("rb") as before, path.open("rb") as after:
        # Every original byte except the two size fields must be unchanged.
        for start, end in ((0, 4), (8, size_field), (size_field + 4, original_size)):
            before.seek(start)
            after.seek(start)
            remaining = end - start
            while remaining > 0:
                block_size = min(4 * 1024 * 1024, remaining)
                if before.read(block_size) != after.read(block_size):
                    problems.append("original bytes changed")
                    remaining = 0
                    break
                remaining -= block_size
        after.seek(original_size)
        if after.read() != b"\x00" * plan.append:
            problems.append("appended bytes are not zero")
    fixed = inspect_wav(path)
    left = [issue for issue in fixed.issues if issue.code in KIND_BY_CODE or issue.code in ("riff_size_mismatch", "data_not_frame_aligned", "missing_final_pad")]
    if left:
        problems.append("still has: " + "; ".join(issue.message for issue in left))
    if fixed.audio.get("frames") != plan.new_data_size // plan.block_align:
        problems.append("audio length doesn't match the plan")
    if problems:
        _restore(backup, path)
        return _failed(result, "verification failed (" + ", ".join(problems) + ") — original restored from backup")
    result.status = "repaired"
    result.size_after = fixed.file_size
    result.issues_after = [issue.message for issue in fixed.issues]
    result.repairs = [plan.category]
    result.verification += [
        "every original byte except the data size and RIFF size fields is unchanged (audio included)",
        f"{plan.append} zero byte(s) appended" if plan.append else "no bytes added",
        f"audio now reads as {fixed.audio.get('frames'):,} frames ({plan.seconds(plan.new_data_size):.3f} s)",
    ] + plan.lines
    result.message = "audio recovered"
    return result


def run_recovers(items: list[tuple[InspectionReport, RecoverPlan]], backup_root: Path, scan_root: Optional[Path], app_label: str,
                 step: Optional[Callable[[int, int, Path, str], None]] = None,
                 finished: Optional[Callable[[int, int, RepairResult], None]] = None) -> tuple[Path, list[RepairResult]]:
    run_folder = backup_run_folder(backup_root)
    run_folder.mkdir(parents=True)
    results: list[RepairResult] = []
    for index, (report, plan) in enumerate(items, start=1):
        file_step = (lambda message, i=index, p=plan.path: step(i, len(items), p, message)) if step is not None else None
        try:
            results.append(recover_file(plan, report, run_folder, scan_root, file_step))
        except Exception as exc:  # one bad file never stops the batch
            results.append(_failed(RepairResult(str(plan.path), "failed", plan.category), f"unexpected error: {exc}"))
        if finished is not None:
            finished(index, len(items), results[-1])
    write_repair_log(run_folder, results, scan_root, app_label)
    return run_folder, results
