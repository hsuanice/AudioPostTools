"""Wave Manager → Wave Repair for Post hand-off.

Wave Manager never repairs anything itself; when a file is structurally
damaged it says so (`structural_damage_hint`) and can open the file in the
separate Wave Repair app (`repair_launch_command`).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterable, Optional

from operations.riff_inspect import inspect_wav

REPAIR_APP_NAME = "Wave Repair for Post"
# Wave Manager leaves a note here when it hands files over, naming its Source
# folder, so Wave Repair can keep backups beside that folder (never inside
# it, where Wave Manager would rescan them as takes) — an `open -a`
# document hand-off can carry nothing but the file paths.
HANDOFF_PATH = Path.home() / "Library" / "Application Support" / "Wave Repair for Post" / "handoff.json"
HANDOFF_MAX_AGE = 15 * 60
REPAIR_SCRIPT_NAME = "wave_repair.py"
STRUCTURE_BLOCK_PREFIX = "BLOCKED: structure"


def _own_app_bundle(executable: Path) -> Optional[Path]:
    for parent in executable.parents:
        if parent.suffix == ".app":
            return parent
    return None


def repair_launch_command(paths: Iterable[Path], frozen: bool, executable: Path, script_dir: Path) -> list[str]:
    """The command that opens `paths` in Wave Repair.

    Packaged: `open -a` — prefers a Wave Repair.app sitting next to this
    app's own bundle, otherwise lets LaunchServices find it by name. Files go
    through as an open-documents Apple Event, so an already-running Wave
    Repair receives them instead of a second copy starting.
    From source: runs the sibling wave_repair.py with the same interpreter."""
    files = [str(path) for path in paths]
    if frozen:
        bundle = _own_app_bundle(executable)
        sibling = bundle.parent / f"{REPAIR_APP_NAME}.app" if bundle is not None else None
        target = str(sibling) if sibling is not None and sibling.exists() else REPAIR_APP_NAME
        return ["open", "-a", target, *files]
    return [str(executable), str(script_dir / REPAIR_SCRIPT_NAME), *files]


def structural_damage_hint(paths: Iterable[Optional[Path]]) -> str:
    """A suffix for a BLOCKED status naming the first real structural error
    found among `paths` — empty when every file is structurally fine (the
    block then has some other cause, and this stays out of the way)."""
    paths = list(paths)
    for path in paths:
        if path is None or not path.is_file():
            continue
        try:
            report = inspect_wav(path)
        except Exception:
            continue
        error = next((issue for issue in report.issues if issue.severity == "error"), None)
        if error is not None:
            prefix = f"{path.name}: " if len(paths) > 1 else ""
            return f" — structural damage ({prefix}{error.message}); open in {REPAIR_APP_NAME}"
    return ""


def structure_block_status(path: Path) -> Optional[str]:
    """A BLOCKED status for a source with any structural warning or error
    (info-level notes never block), naming the worst problem; None when the
    file is structurally sound. Statuses built here all start with
    STRUCTURE_BLOCK_PREFIX so Wave Manager can find them again to send
    those files to Wave Repair and rescan just them afterwards."""
    report = inspect_wav(path)
    problems = [issue for issue in report.issues if issue.severity in ("warning", "error")]
    if not problems:
        return None
    worst = next((issue for issue in problems if issue.severity == "error"), problems[0])
    more = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
    return f"{STRUCTURE_BLOCK_PREFIX} — {worst.message}{more}; send to {REPAIR_APP_NAME}"


def is_structure_block(status: str) -> bool:
    return status.startswith(STRUCTURE_BLOCK_PREFIX)


def write_handoff(files: list[Path], scan_root: Optional[Path], path: Optional[Path] = None) -> None:
    path = path or HANDOFF_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "scan_root": str(scan_root) if scan_root else None,
            "files": [str(item) for item in files],
            "written": time.time(),
        }), encoding="utf-8")
    except OSError:
        pass


def handoff_scan_root(files: list[Path], path: Optional[Path] = None) -> Optional[Path]:
    """The Source folder Wave Manager handed these files over from, if a
    recent hand-off note covers every one of them and they sit inside it."""
    path = path or HANDOFF_PATH
    try:
        note = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(note, dict) or not note.get("scan_root") or time.time() - float(note.get("written", 0)) > HANDOFF_MAX_AGE:
        return None
    handed = {str(item) for item in note.get("files", [])}
    root = Path(note["scan_root"])
    if not files or any(str(item) not in handed for item in files):
        return None
    try:
        for item in files:
            item.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return root
