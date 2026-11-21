"""Phase 2: safe structural repair, always backed up first.

Only repairs that are 100% certain and never move or remove any existing
byte are done here: appending a missing final pad byte, and rewriting the
RIFF size field to match the file. Every other byte of the file is proven
identical afterwards. Anything else — a cut-off chunk, unfinalized data, a
pad byte missing *between* chunks (fixing it would shift every later byte,
and recorder-specific chunks may hold absolute offsets) — is left untouched
for Phase 3 / human review, and a file with any such issue isn't touched at
all: the user's rule is that Phase 2 never strips information from a file.

Order for each file:
  1. copy the original into the backup folder, hashing as it's read;
  2. verify the backup (re-read, same hash, same size);
  3. only then edit the original in place (append-only + the 4-byte RIFF
     size field), keeping its name and path; its modification time becomes
     the repair time (the backup keeps the original's), so sync/backup tools
     see a newer file instead of a same-time, different-size conflict;
  4. prove every original byte except the RIFF size field is unchanged, the
     only new bytes are the appended zeros, and the file now inspects clean;
  5. any failure after step 3 restores the original from the verified backup.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from operations.riff_inspect import InspectionReport, Issue, inspect_wav

COPY_BLOCK = 4 * 1024 * 1024

# code -> backup folder name. Folders group files by what was repaired, so
# one kind of problem (e.g. the 92 Cantar files with an odd-sized final
# iXML) ends up together.
SAFE_REPAIRS: dict[str, str] = {
    "missing_final_pad": "Missing pad byte at end of file (odd-sized last chunk)",
    "riff_size_mismatch": "Wrong RIFF size in file header",
}
SAFE_REPAIR_ORDER = tuple(SAFE_REPAIRS)

PLAN_REPAIR = "repair"
PLAN_DEFERRED = "deferred"  # has a problem Phase 2 must not touch
PLAN_NONE = "none"


@dataclass
class RepairPlan:
    path: Path
    actions: list[Issue] = field(default_factory=list)
    blockers: list[Issue] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.blockers:
            return PLAN_DEFERRED
        return PLAN_REPAIR if self.actions else PLAN_NONE

    @property
    def category(self) -> str:
        codes = {issue.code for issue in self.actions}
        return " + ".join(SAFE_REPAIRS[code] for code in SAFE_REPAIR_ORDER if code in codes)

    @property
    def deferred_reason(self) -> str:
        return self.blockers[0].message if self.blockers else ""


def _riff_size_fix_is_safe(report: InspectionReport, codes: set[str]) -> bool:
    """A RIFF size *smaller* than the file only means the header wasn't
    updated after more was written — safe to correct. A *larger* one means
    something the header promised is missing (possibly a whole chunk), and
    "fixing" the size would hide that loss — unless the only thing missing
    is the final pad byte this repair appends anyway."""
    if report.riff_declared_size is None:
        return False
    excess = report.riff_declared_size - (report.file_size - 8)
    return excess < 0 or (excess == 1 and "missing_final_pad" in codes)


def plan_repair(report: InspectionReport) -> RepairPlan:
    """What Phase 2 would do to this file. Info-level notes are never acted
    on; any warning/error outside SAFE_REPAIRS defers the whole file."""
    plan = RepairPlan(Path(report.path))
    actionable = [issue for issue in report.issues if issue.severity in ("warning", "error")]
    if not actionable:
        return plan
    if report.container != "RIFF":
        # RF64 keeps its real sizes in ds64 — not handled by Phase 2 yet.
        plan.blockers = actionable
        return plan
    codes = {issue.code for issue in actionable}
    plan.blockers = [issue for issue in actionable if issue.code not in SAFE_REPAIRS or (issue.code == "riff_size_mismatch" and not _riff_size_fix_is_safe(report, codes))]
    if not plan.blockers:
        plan.actions = actionable
    return plan


def backup_run_folder(backup_root: Path, now: Optional[dt.datetime] = None) -> Path:
    """A new folder per run, named to the minute ("2026-10-02 1430"); a
    second run in the same minute gets "-2", "-3", …"""
    stamp = (now or dt.datetime.now()).strftime("%Y-%m-%d %H%M")
    candidate = backup_root / stamp
    counter = 2
    while candidate.exists():
        candidate = backup_root / f"{stamp}-{counter}"
        counter += 1
    return candidate


def project_root_for(files: list[Path]) -> Path:
    """The folder these files sit in (their common folder)."""
    if not files:
        raise ValueError("no files")
    return Path(os.path.commonpath([str(path.parent) for path in files]))


def default_backup_root(files: list[Path], scan_root: Optional[Path] = None) -> Path:
    """Beside the folder being worked on, never inside it (anything inside
    would be scanned again as if it were more takes). With a known scan root
    — a folder opened in Browse, or the Source folder Wave Manager handed
    the files over from — that's beside the scan root
    (".../LOCATION/ISO POLY - Wave Repair Backups"); otherwise beside the
    files' own folder (".../Broken Files copy/2026-01-14.AAN - Wave Repair
    Backups"). An earlier version climbed past "day folders" on its own and,
    in a test tree, ended up three levels too high — so no guessing."""
    root = scan_root or project_root_for(files)
    return root.parent / f"{root.name} - Wave Repair Backups"


def relative_location(path: Path, scan_root: Optional[Path]) -> Path:
    if scan_root is not None:
        try:
            return path.parent.relative_to(scan_root)
        except ValueError:
            pass
    return Path(path.parent.name)


def is_inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def _copy_with_hash(source: Path, target: Path) -> str:
    digest = hashlib.sha1()
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, target.open("xb") as writer:
        while block := reader.read(COPY_BLOCK):
            digest.update(block)
            writer.write(block)
    shutil.copystat(source, target)
    return digest.hexdigest()


def _file_hash(path: Path, skip_riff_size: bool = False, limit: Optional[int] = None) -> str:
    """SHA-1 of the file (or its first `limit` bytes), optionally with the
    4-byte RIFF size field left out — the one field a repair may change."""
    digest = hashlib.sha1()
    remaining = path.stat().st_size if limit is None else limit
    with path.open("rb") as reader:
        if skip_riff_size:
            digest.update(reader.read(4))
            reader.seek(8)
            remaining -= 8
        while remaining > 0:
            block = reader.read(min(COPY_BLOCK, remaining))
            if not block:
                break
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def _restore(backup: Path, original: Path) -> None:
    temp = original.with_name(f".{original.name}.wave-repair-restore")
    shutil.copy2(backup, temp)
    os.replace(temp, original)


@dataclass
class RepairResult:
    path: str
    status: str  # "repaired" | "skipped" | "failed"
    category: str = ""
    message: str = ""
    backup_path: str = ""
    repairs: list[str] = field(default_factory=list)
    issues_before: list[str] = field(default_factory=list)
    issues_after: list[str] = field(default_factory=list)
    size_before: int = 0
    size_after: int = 0
    original_sha1: str = ""
    verification: list[str] = field(default_factory=list)


def repair_file(plan: RepairPlan, report: InspectionReport, run_folder: Path, scan_root: Optional[Path], step: Optional[Callable[[str], None]] = None) -> RepairResult:
    path = plan.path
    step = step or (lambda _message: None)
    result = RepairResult(str(path), "skipped", plan.category, issues_before=[issue.message for issue in report.issues], size_before=report.file_size)
    if plan.status != PLAN_REPAIR:
        result.message = plan.deferred_reason or "nothing to repair"
        return result
    codes = {issue.code for issue in plan.actions}
    backup = run_folder / plan.category / relative_location(path, scan_root) / path.name
    result.backup_path = str(backup)
    original_size = path.stat().st_size
    if original_size != report.file_size:
        return _failed(result, "file changed since it was inspected — rescan first")

    # 1–2. Backup, verified before anything else happens.
    try:
        step("backing up")
        result.original_sha1 = _copy_with_hash(path, backup)
        step("verifying backup")
        if backup.stat().st_size != original_size or _file_hash(backup) != result.original_sha1:
            return _failed(result, "backup copy did not verify — original not touched")
    except OSError as exc:
        return _failed(result, f"backup failed ({exc}) — original not touched")
    result.verification.append("backup verified (size + SHA-1 identical to original)")
    original_body_hash = _file_hash(backup, skip_riff_size=True)

    # 3. Append-only edit + RIFF size field. The modification time is left
    # to become "now": keeping the old one made FreeFileSync see the repaired
    # file and its backup as same-time/different-size — a conflict it won't
    # resolve. The recording's own time lives in bext, and the backup keeps
    # the original timestamps.
    step("repairing")
    appended = 1 if "missing_final_pad" in codes else 0
    try:
        with path.open("r+b") as writer:
            if appended:
                writer.seek(0, os.SEEK_END)
                writer.write(b"\x00" * appended)
            writer.seek(4)
            writer.write(struct.pack("<I", original_size + appended - 8))
    except OSError as exc:
        _restore(backup, path)
        return _failed(result, f"write failed ({exc}) — original restored from backup")

    # 4. Prove nothing else changed and the file is now clean.
    step("verifying repair")
    problems = []
    if path.stat().st_size != original_size + appended:
        problems.append("unexpected file size")
    if _file_hash(path, skip_riff_size=True, limit=original_size) != original_body_hash:
        problems.append("original bytes changed")
    if appended:
        with path.open("rb") as reader:
            reader.seek(original_size)
            if reader.read() != b"\x00" * appended:
                problems.append("appended bytes are not zero")
    after = inspect_wav(path)
    remaining = [issue for issue in after.issues if issue.severity in ("warning", "error")]
    if remaining:
        problems.append("still has: " + "; ".join(issue.message for issue in remaining))
    if problems:
        _restore(backup, path)
        return _failed(result, "verification failed (" + ", ".join(problems) + ") — original restored from backup")

    result.status = "repaired"
    result.size_after = after.file_size
    result.issues_after = [issue.message for issue in after.issues]
    result.repairs = [SAFE_REPAIRS[code] for code in SAFE_REPAIR_ORDER if code in codes]
    result.verification += [
        "every original byte except the 4-byte RIFF size field is unchanged (audio included)",
        f"{appended} zero byte(s) appended" if appended else "no bytes added",
        "file inspects clean after repair",
    ]
    result.message = "repaired"
    return result


def _failed(result: RepairResult, message: str) -> RepairResult:
    result.status = "failed"
    result.message = message
    return result


def run_repairs(
    items: list[tuple[InspectionReport, RepairPlan]],
    backup_root: Path,
    scan_root: Optional[Path],
    app_label: str,
    progress: Optional[Callable[[int, int, Path], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
    step: Optional[Callable[[int, int, Path, str], None]] = None,
    finished: Optional[Callable[[int, int, RepairResult], None]] = None,
) -> tuple[Path, list[RepairResult]]:
    """`progress` fires as each file starts, `step` for each stage within it
    (backing up / verifying backup / repairing / verifying repair), and
    `finished` with that file's result."""
    run_folder = backup_run_folder(backup_root)
    run_folder.mkdir(parents=True)
    results: list[RepairResult] = []
    for index, (report, plan) in enumerate(items, start=1):
        if cancelled is not None and cancelled():
            break
        if progress is not None:
            progress(index, len(items), plan.path)
        file_step = (lambda message, i=index, p=plan.path: step(i, len(items), p, message)) if step is not None else None
        try:
            results.append(repair_file(plan, report, run_folder, scan_root, file_step))
        except Exception as exc:  # one bad file never stops the batch
            results.append(_failed(RepairResult(str(plan.path), "failed", plan.category), f"unexpected error: {exc}"))
        if finished is not None:
            finished(index, len(items), results[-1])
    write_repair_log(run_folder, results, scan_root, app_label)
    return run_folder, results


def write_repair_log(run_folder: Path, results: list[RepairResult], scan_root: Optional[Path], app_label: str) -> None:
    from dataclasses import asdict

    summary: dict[str, int] = {}
    for result in results:
        key = result.category if result.status == "repaired" else result.status
        summary[key] = summary.get(key, 0) + 1
    payload = {
        "app": app_label,
        "run_folder": str(run_folder),
        "scan_root": str(scan_root) if scan_root else None,
        "finished": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": summary,
        "files": [asdict(result) for result in results],
    }
    (run_folder / "Repair Log.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [f"{app_label} — Repair Log", f"Run folder: {run_folder}", f"Scanned folder: {scan_root or '-'}", f"Finished: {payload['finished']}", ""]
    lines += [f"  {count:>5}  {name}" for name, count in summary.items()]
    for result in results:
        lines += ["", f"[{result.status.upper()}] {result.path}"]
        if result.backup_path and result.status != "skipped":
            lines.append(f"  Backup: {result.backup_path}")
        if result.repairs:
            lines.append("  Repairs: " + "; ".join(result.repairs))
        if result.status != "repaired":
            lines.append(f"  Reason: {result.message}")
        lines += [f"  ✓ {line}" for line in result.verification]
    (run_folder / "Repair Log.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- Phase 3: write a rebuilt iXML into a cut-off final iXML chunk ------------

REBUILD_CATEGORY = "Rebuilt iXML from related takes"


def rebuild_target_problem(report: InspectionReport) -> str:
    """Empty if this file is exactly the Phase 3 case: everything intact
    except a final iXML whose header is there and whose reserved contents
    were never written (the RIFF size already counting them). Otherwise,
    why not."""
    if report.container != "RIFF" or not report.chunks:
        return "not a plain RIFF file"
    last = report.chunks[-1]
    if last.chunk_id != "iXML" or last.size != 0 or last.declared_size <= 0:
        return "the last chunk is not an iXML header with nothing written after it"
    if report.file_size != last.data_offset:
        return "unexpected bytes after the iXML header"
    if report.riff_declared_size is None or report.riff_declared_size + 8 != report.file_size + last.declared_size + (last.declared_size & 1):
        return "the RIFF size does not match the reserved iXML size"
    other = [issue for issue in report.issues if issue.severity in ("warning", "error") and issue.code not in ("chunk_overruns_eof", "riff_size_mismatch")]
    if other:
        return other[0].message
    return ""


def rebuild_file(report: InspectionReport, payload: bytes, run_folder: Path, scan_root: Optional[Path], summary: list[str], step: Optional[Callable[[str], None]] = None) -> RepairResult:
    """Back up, then append the rebuilt iXML contents after the existing
    header — the only change to the file. Every original byte (header, RIFF
    size, audio, all other chunks) is proven unchanged afterwards."""
    path = Path(report.path)
    step = step or (lambda _message: None)
    result = RepairResult(str(path), "skipped", REBUILD_CATEGORY, issues_before=[issue.message for issue in report.issues], size_before=report.file_size)
    problem = rebuild_target_problem(report)
    last = report.chunks[-1] if report.chunks else None
    if problem or last is None:
        result.message = problem or "nothing to rebuild"
        return result
    expected = last.declared_size + (last.declared_size & 1)
    if len(payload) != expected:
        return _failed(result, f"rebuilt iXML is {len(payload):,} bytes, the file reserves {expected:,}")
    original_size = path.stat().st_size
    if original_size != report.file_size:
        return _failed(result, "file changed since it was inspected — rescan first")
    backup = run_folder / REBUILD_CATEGORY / relative_location(path, scan_root) / path.name
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

    step("writing rebuilt iXML")
    try:
        with path.open("r+b") as writer:
            writer.seek(0, os.SEEK_END)
            writer.write(payload)
    except OSError as exc:
        _restore(backup, path)
        return _failed(result, f"write failed ({exc}) — original restored from backup")

    step("verifying")
    problems = []
    if path.stat().st_size != original_size + len(payload):
        problems.append("unexpected file size")
    if _file_hash(path, limit=original_size) != result.original_sha1:
        problems.append("original bytes changed")
    with path.open("rb") as reader:
        reader.seek(original_size)
        if reader.read() != payload:
            problems.append("written iXML does not read back identically")
    after = inspect_wav(path)
    remaining = [issue for issue in after.issues if issue.severity in ("warning", "error")]
    if remaining:
        problems.append("still has: " + "; ".join(issue.message for issue in remaining))
    if problems:
        _restore(backup, path)
        return _failed(result, "verification failed (" + ", ".join(problems) + ") — original restored from backup")
    result.status = "repaired"
    result.size_after = after.file_size
    result.issues_after = [issue.message for issue in after.issues]
    result.repairs = [REBUILD_CATEGORY]
    result.verification += [
        "every original byte is unchanged (header, RIFF size, audio, all other chunks)",
        f"{len(payload):,} bytes of rebuilt iXML written into the space the recorder had reserved",
        "file inspects clean and its iXML parses",
    ] + summary
    result.message = "iXML rebuilt"
    return result


def run_rebuilds(
    items: list[tuple[InspectionReport, bytes, list[str]]],
    backup_root: Path,
    scan_root: Optional[Path],
    app_label: str,
    step: Optional[Callable[[int, int, Path, str], None]] = None,
    finished: Optional[Callable[[int, int, RepairResult], None]] = None,
) -> tuple[Path, list[RepairResult]]:
    """Write several reviewed rebuilds in one run: one backup run folder,
    one Repair Log. Each item is (report, rebuilt iXML bytes, summary lines)."""
    run_folder = backup_run_folder(backup_root)
    run_folder.mkdir(parents=True)
    results: list[RepairResult] = []
    for index, (report, payload, summary) in enumerate(items, start=1):
        path = Path(report.path)
        file_step = (lambda message, i=index, p=path: step(i, len(items), p, message)) if step is not None else None
        try:
            results.append(rebuild_file(report, payload, run_folder, scan_root, summary, file_step))
        except Exception as exc:  # one bad file never stops the batch
            results.append(_failed(RepairResult(str(path), "failed", REBUILD_CATEGORY), f"unexpected error: {exc}"))
        if finished is not None:
            finished(index, len(items), results[-1])
    write_repair_log(run_folder, results, scan_root, app_label)
    return run_folder, results


def run_rebuild(report: InspectionReport, payload: bytes, backup_root: Path, scan_root: Optional[Path], app_label: str, summary: list[str], step: Optional[Callable[[int, int, Path, str], None]] = None) -> tuple[Path, list[RepairResult]]:
    return run_rebuilds([(report, payload, summary)], backup_root, scan_root, app_label, step)
