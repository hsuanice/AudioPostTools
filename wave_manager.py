#!/usr/bin/env python3
"""SoX + BWF MetaEdit batch workflow app.

Modes:
- Split poly WAVs with SoX, then embed per-channel iXML/BWF metadata.
- Re-embed metadata from matched source WAVs to rendered target WAVs.

This tool writes audio files and metadata only when Execute is pressed.
Preview and scanning are read-only except for temporary BWF MetaEdit sidecars,
which are removed immediately.
"""

from __future__ import annotations

import copy
import csv
import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue

# A PyInstaller-built .app always has these bundled (verified: both ship
# their own PyInstaller hook, so `--clean` builds include them with no
# spec-file changes) — this check only ever matters when running the raw
# .py directly without having pip-installed them first, where the plain
# `import` below would otherwise crash with a raw traceback before any
# window could even appear.
_MISSING_PACKAGES = [name for name in ("tkinterdnd2", "openpyxl") if importlib.util.find_spec(name) is None]
if _MISSING_PACKAGES:
    import tkinter as _tk

    # A plain Tk window, not tkinter.messagebox: a native macOS alert can
    # crash Tk (see the note on dialogs in ui_components.py), and
    # ui_components itself needs tkinterdnd2.
    _root = _tk.Tk()
    _root.title("Wave Manager for Post — Missing Python package(s)")
    _tk.Label(
        _root,
        justify=_tk.LEFT,
        padx=18,
        pady=16,
        text="This app needs the following Python package(s), which aren't installed:\n\n"
        + "\n".join(f"  • {name}" for name in _MISSING_PACKAGES)
        + "\n\nInstall them by running this in Terminal:\n\n"
        + f"    pip3 install {' '.join(_MISSING_PACKAGES)}\n\n"
        "Then relaunch this app.\n\n"
        "(A pre-built .app from this project's own Build script already "
        "includes these — this only applies when running the .py file directly.)",
    ).pack()
    _tk.Button(_root, text="Quit", command=_root.quit).pack(pady=(0, 14))
    _root.bind("<Return>", lambda _event: _root.quit())
    _root.mainloop()
    _root.destroy()
    sys.exit(1)

from tkinter import filedialog, ttk
import tkinter as tk
import tkinter.font as tkfont
from tkinterdnd2 import COPY, DND_FILES, TkinterDnD
from typing import Optional

import openpyxl
from openpyxl.styles import Font, PatternFill

MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from operations.wav_core import WavChunk, clear_wav_channel_mask, copy_wav_chunk_from_source, find_wav_chunk, read_wav_chunk, remove_wav_chunk, scan_wav_chunks, set_wav_channel_mask, wav_structure_info
from operations.filename_formula import formula_pattern, formula_without_number, render_output_name, render_output_name_without_number
from operations.audio_qc import compare_wav_pcm_content, compare_wav_pcm_stream, compute_wav_channel_peaks, compute_wav_peaks, read_wav_pcm_layout, read_wav_pcm_layout_fast, wav_duration_seconds
from operations.speaker_layout import DEFAULT_SPEAKER_PATTERN, SPEAKER_CANONICAL_CHANNELS, SPEAKER_LAYOUT_ORDERS, SPEAKER_MASK_BITS, merge_target_name, parse_speaker_mapping, speaker_channel_mask, speaker_filename_pattern, speaker_layout_name, speaker_order
from operations.ixml_core import extract_mono_ixml, first_track_list, parse_source_ixml, parse_tracks, set_text, text_int, track_from_mono_ixml
from operations.path_core import list_qc_recursive_wav_files, natural_path_key, resolve_output_root, wav_files
from operations.rename_core import RenameRule, SerialMetadata, apply_name_affix, apply_rename_rules, assign_serial_ids, assign_serial_ids_in_order, rename_auto_input_pattern, rename_input_pattern, rename_live_refresh_allowed, rename_number_from_match, render_serial_name, strict_metadata_group_order
from operations.timecode import parse_speed_field, samples_to_smpte
from operations.post_tools_inbox import WAVE_MANAGER, Batch, InboxPoller, bring_to_front
from operations.repair_handoff import REPAIR_APP_NAME, is_structure_block, repair_launch_command, structural_damage_hint, structure_block_status, write_handoff
from operations.riff_inspect import format_structure_brief, inspect_wav
from operations.metadata_template import build_trk_table_from_ixml_rows, concat_trk_names, expand_metadata_template, find_empty_metadata_tokens, template_token_names, umid_to_pt
from ui_components import messagebox, MUTED_TEXT_COLOR, PANEL_BACKGROUND, TEXT_COLOR, ExecutionLogWindow, apply_app_theme, configure_treeview_column_widths
from ui_components import ExecutionLogPanel, QcInputPicker, TreeviewSorter, UpdateChecker, WorkflowFolderPicker, first_dropped_folder, first_dropped_qc_input, resize_wrapped_label, treeview_cell_sort_key


APP_NAME = "Wave Manager for Post"
# Soundminer "Send to …" scripts: batch action → mode tab, and its name in messages.
SEND_ACTION_MODES = {
    "split": "Split Poly with SoX + Embed",
    "combine": "Combine / Merge Mono Files to Poly",
    "rename": "Rename",
}
SEND_ACTION_LABELS = {"split": "Split + Embed", "combine": "Combine / Merge", "rename": "Rename"}
# The modes whose list takes dropped / sent files.
SEND_MODE_LABELS = {SEND_ACTION_MODES[action]: label for action, label in SEND_ACTION_LABELS.items()}
VERSION = "261121.2233"
A_NUMBER_PATTERN = re.compile(r"^(?P<base>.+)\.A(?P<ch>[1-9]\d*)\.wav$", re.IGNORECASE)
DEFAULT_OUTPUT_FORMULA = "{base}.A$"
DEFAULT_SOURCE_FORMULA = "{base}.A$"
DEFAULT_TARGET_FORMULA = r"{base}.A$ render \d+"
DEFAULT_FORMULA_PRESETS = ("{base}.A$", r"{base}.A$ render \d+", "{base}_AAP.A$")
APP_SETTINGS_PATH = Path.home() / "Library" / "Application Support" / "Wave Manager for Post" / "settings.json"
# Settings lived here under the app's previous name ("SoX Metadata
# Workflow") — load_app_settings() falls back to reading this once so
# renaming the app doesn't look like a settings reset for anyone who
# already had saved formula presets/window geometry/etc.
_LEGACY_APP_SETTINGS_PATH = Path.home() / "Library" / "Application Support" / "SoX Metadata Workflow" / "settings.json"

# Metadata Template token buttons, grouped like hsuanice's REAPER "Rename
# Source File from Metadata" script (file/session-derived vs. embedded
# BWF/iXML metadata) so the two kinds stay visually distinct.
METADATA_TEMPLATE_SESSION_TOKENS = (
    "$srcfile", "$srcbase", "$srcext", "$srcpath", "$srcdir",
    "${srcbaseprefix:3}", "${srcbasesuffix:3}",
    "$samplerate", "$channels", "$length", "${counter:2}",
    "$chnum", "$interleave", "$baseindex",
)
METADATA_TEMPLATE_META_TOKENS = (
    "$project", "$scene", "$take", "$tape", "$ubits", "$framerate", "$speed",
    "$date", "$time", "$year", "$originationdate", "$originationtime", "$startoffset",
    "${umid}", "${umid_pt}", "$trk", "$trkall", "$origsrcfile",
)
# Inspector's per-row "Metadata Template Tokens" panel — same fields, in
# display order, minus the two full-path ones (srcpath/srcdir) that are
# already shown in full in the FILE section above it.
METADATA_TEMPLATE_DISPLAY_FIELDS = (
    "srcfile", "srcbase", "srcext", "samplerate", "channels", "length",
    "chnum", "interleave",
    "project", "scene", "take", "tape", "ubits", "framerate", "speed",
    "date", "time", "year", "originationdate", "originationtime", "startoffset",
    "umid", "umid_pt", "trk", "trkall", "origsrcfile",
)


def load_app_settings(path: Path = APP_SETTINGS_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        return data
    if path == APP_SETTINGS_PATH:
        # First launch under the new name — the settings file may still
        # be sitting under the app's previous name's folder instead.
        try:
            legacy_data = json.loads(_LEGACY_APP_SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return legacy_data if isinstance(legacy_data, dict) else {}
    return {}


def save_app_settings(data: dict, path: Path = APP_SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# Each entry: (action_id, human label, Tk bind sequence, scope). Fixed by
# the script, not user-editable — Settings only ever shows this list
# read-only. scope "global" means bound app-wide via bind_all (only ever
# given a modifier'd sequence — a bare key bound globally would get typed
# into every ordinary text field in the app); any other scope names a
# widget attribute (e.g. "browse_file_list"), or a tuple of several, the
# action is bound to directly instead, which is what lets Browse mode's
# own actions safely use a bare, unmodified key: those widgets never hold
# text input at all. Inspector and the Execution Log are both separate
# windows, so their shortcuts toggle (show if hidden, hide if shown)
# rather than just always-show — see
# _toggle_inspector()/_toggle_log_window().
KEYBINDING_ACTIONS: tuple[tuple[str, str, str, object], ...] = (
    ("mode_browse", "Switch to Browse mode", "<Command-Key-1>", "global"),
    ("mode_split", "Switch to Split + Embed mode", "<Command-Key-2>", "global"),
    ("mode_reembed", "Switch to Re-embed mode", "<Command-Key-3>", "global"),
    ("mode_rename", "Switch to Rename mode", "<Command-Key-4>", "global"),
    ("mode_combine", "Switch to Combine / Merge mode", "<Command-Key-5>", "global"),
    ("mode_qc", "Switch to QC mode", "<Command-Key-6>", "global"),
    ("mode_settings", "Switch to Settings mode", "<Command-Key-7>", "global"),
    ("open_inspector", "Toggle Audio Inspector", "<Command-Key-i>", "global"),
    ("toggle_log", "Toggle Execution Log", "<Command-Key-l>", "global"),
    ("preview", "Preview / Refresh", "<Command-Key-p>", "global"),
    ("execute", "Execute Ready Jobs", "<Command-Key-e>", "global"),
    ("compare", "QC Compare", "<Command-Key-k>", "global"),
    ("browse_recurse", "Browse: list all files in subfolders of the selected folder", "Return", "browse_folder_tree"),
    ("browse_play_pause", "Browse: play/pause", "space", ("browse_file_list", "browse_folder_tree")),
)
DEFAULT_KEYBINDINGS: dict[str, str] = {action_id: default for action_id, _label, default, _scope in KEYBINDING_ACTIONS}
KEYBINDING_SCOPES: dict[str, str] = {action_id: scope for action_id, _label, _default, scope in KEYBINDING_ACTIONS}


def keybinding_display_label(sequence: str) -> str:
    """A short, readable form of a Tk bind sequence for the read-only
    Settings list — "<Command-Key-1>" -> "⌘1", "Return" -> "Return"."""
    if not sequence:
        return "(none)"
    if not sequence.startswith("<"):
        return sequence.capitalize() if len(sequence) > 1 else sequence.upper()
    parts = sequence.strip("<>").split("-")
    if len(parts) >= 2 and parts[-2] == "Key":
        modifiers, key = parts[:-2], parts[-1]
    else:
        modifiers, key = parts[:-1], parts[-1]
    symbols = {"Command": "⌘", "Control": "⌃", "Option": "⌥", "Shift": "⇧"}
    prefix = "".join(symbols.get(modifier, modifier + "+") for modifier in modifiers)
    return f"{prefix}{key.upper() if len(key) == 1 else key}"


# The Browse player's volume range is -60..+24 dB — -60 stands in for -inf
# (sox has no literal -inf gain), so the bottom of the slider swaps to a
# true `vol 0 amplitude` (exact silence) instead of just "very quiet".
BROWSE_VOLUME_MIN_DB = -60.0
BROWSE_VOLUME_MAX_DB = 24.0

# Approximate width of a Solo toggle's natural (unclipped) size, used only
# to right-align its position in the waveform lane — never passed as an
# explicit width to canvas.itemconfigure()/create_window(), which triggers
# a confirmed real hang: forcing a canvas window item to clip smaller than
# its own natural size blocks the very next Tk event-loop pass for roughly
# the remaining sox playback duration if a CoreAudio stream is open at the
# same time. The button's actual on-screen size is always its natural one.
BROWSE_SOLO_BUTTON_WIDTH = 24


def browse_volume_label(db: float) -> str:
    return "-inf dB" if db <= BROWSE_VOLUME_MIN_DB else f"{db:+.1f} dB"


def browse_volume_sox_args(db: float) -> list[str]:
    if db <= BROWSE_VOLUME_MIN_DB:
        return ["vol", "0", "amplitude"]
    return ["vol", f"{db:.2f}", "dB"]


DEFAULT_RENAME_FORMULA = "{base}.A$"
DEFAULT_SPEAKER_MAPPING = "FL=L\nFR=R\nFC=C\nSL=Ls\nSR=Rs\nBL=Lb\nBR=Rb\nLss=Lss\nRss=Rss\nLsr=Lsr\nRsr=Rsr\nLrs=Lrs\nRrs=Rrs\nL=L\nR=R\nC=C\nLc=Lc\nRc=Rc\nLFE=LFE\nLfh=Lfh\nRfh=Rfh\nS=S\nCs=Cs\nLs=Ls\nRs=Rs\nLscr=Lscr\nRscr=Rscr\nLsd=Lsd\nRsd=Rsd\nLb=Lb\nRb=Rb\nLvh=Lvh\nCvh=Cvh\nRvh=Rvh\nLw=Lw\nRw=Rw\nTbl=Tbl\nTbr=Tbr\nTfl=Tfl\nTfr=Tfr\nTl=Tl\nTr=Tr\nTs=Ts\nLtm=Ltm\nRtm=Rtm\nLts=Lts\nRts=Rts\nSW=SW"
INSTALL_COMMAND_TEXT = "brew install sox bwfmetaedit\nbrew upgrade sox bwfmetaedit"
TABLE_COLUMNS = (
    ("source", "Source", 250),
    ("channel", "Slot", 42),
    ("trk", "Trk#", 42),
    ("stream", "Stream#", 56),
    ("target", "Target", 250),
    ("planned", "Planned Filename", 220),
    ("tape", "Tape", 90),
    ("tc", "TC", 100),
    ("scene", "Scene", 90),
    ("take", "Take", 60),
    ("status", "Status", 320),
)
TABLE_COLUMN_LABELS = {column: label for column, label, _width in TABLE_COLUMNS}
CORE_FIELDS = (
    "Description", "Originator", "OriginatorReference", "OriginationDate",
    "OriginationTime", "IARL", "IART", "ICMT", "ICRD", "INAM", "ICOP",
    "ICMS", "IGNR", "ISFT", "ISBJ", "ITCH", "UMID",
)
WINDOWS_SPEAKER_CHANNELS = {
    0x00000001: "FL", 0x00000002: "FR", 0x00000004: "FC", 0x00000008: "LFE",
    0x00000010: "BL", 0x00000020: "BR", 0x00000040: "FLC", 0x00000080: "FRC",
    0x00000100: "BC", 0x00000200: "SL", 0x00000400: "SR", 0x00000800: "TC",
    0x00001000: "TFL", 0x00002000: "TFC", 0x00004000: "TFR", 0x00008000: "TBL",
    0x00010000: "TBC", 0x00020000: "TBR",
}
@dataclass
class Job:
    source: Optional[Path]
    target: Optional[Path]
    channel: int
    trk_number: Optional[int]
    stream_number: Optional[int]
    status: str
    mode: str
    rel_target: Optional[Path] = None
    final_target: Optional[Path] = None
    sources: list[Path] = field(default_factory=list)
    combine_kind: str = ""
    speaker_order: tuple[str, ...] = ()
    channel_mask: int = 0
    serial_tape: str = ""
    serial_time_reference: int = -1
    serial_scene: str = ""
    serial_take: str = ""
    serial_tc_display: str = ""
    has_channel_suffix: bool = True
    metadata_template_fields: dict[str, str] = field(default_factory=dict)
    metadata_template_trk_table: dict[int, str] = field(default_factory=dict)
    naming_base: str = ""  # Re-embed: {base} for the rename formula (source name without its channel suffix)


@dataclass(frozen=True)
class ProgressEvent:
    scope: str
    index: int
    total: int
    status: str
    item: Optional[Path] = None
    started: Optional[float] = None
    elapsed: Optional[float] = None
    eta: Optional[float] = None
    label: str = ""
    detail_label: str = ""
    detail_value: Optional[int] = None
    overall_index: Optional[int] = None
    overall_total: Optional[int] = None
    request_id: Optional[int] = None

    def timing(self, now: Optional[float] = None) -> tuple[float, float]:
        elapsed = self.elapsed
        if elapsed is None:
            elapsed = max(0.0, (time.monotonic() if now is None else now) - (self.started or 0.0))
        if self.eta is not None:
            return elapsed, self.eta
        current = self.overall_index if self.overall_index is not None else self.index
        total = self.overall_total if self.overall_total is not None else self.total
        eta = (elapsed / current) * (total - current) if current else 0.0
        return elapsed, eta


@dataclass(frozen=True)
class InspectorContext:
    kind: str
    wav_path: Optional[Path] = None
    target_path: Optional[Path] = None
    job: Optional[Job] = None
    qc_row: Optional[dict[str, object]] = None
    reset_scroll: bool = False

    @classmethod
    def for_file(cls, wav_path: Path) -> InspectorContext:
        return cls("file", wav_path=wav_path, reset_scroll=True)

    @classmethod
    def for_job(cls, job: Job) -> InspectorContext:
        return cls("job", job=job)

    @classmethod
    def for_qc(cls, row: dict[str, object], reset_scroll: bool = False) -> InspectorContext:
        return cls("qc", qc_row=row, reset_scroll=reset_scroll)

    @classmethod
    def for_pair(cls, source_path: Optional[Path], target_path: Optional[Path], reset_scroll: bool = False) -> InspectorContext:
        return cls("pair", wav_path=source_path, target_path=target_path, reset_scroll=reset_scroll)


@dataclass
class InspectorRenderModel:
    left_sections: list[tuple[str, str]]
    right_sections: list[tuple[str, str]]
    left_ixml: Optional[ET.Element] = None
    right_ixml: Optional[ET.Element] = None
    reset_scroll: bool = False
    left_header: str = "SOURCE"
    right_header: str = "TARGET"
    path_label: str = ""
    blank_missing_sections: bool = False
    pair_first_sections: bool = False


class MergeMetadataError(RuntimeError):
    pass


class PreviewCancelled(RuntimeError):
    pass


class QcCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class OperationControlCapabilities:
    pause: bool = False
    cancel: bool = False
    boundary: str = "none"


@dataclass(frozen=True)
class QcInputDetection:
    kind: str
    source: Path
    target: Path
    message: str = ""


def detect_qc_inputs(source: Path, target: Path, layout_reader=None, progress=None) -> QcInputDetection:
    if source.is_file() and target.is_file():
        if source.suffix.lower() == ".wav" and target.suffix.lower() == ".wav":
            return QcInputDetection("file", source, target)
        return QcInputDetection("invalid", source, target, "Both files must be WAV files.")
    if not source.is_dir() or not target.is_dir():
        return QcInputDetection("invalid", source, target, "Source and Target must both be files or both be folders.")

    read_layout = layout_reader or read_wav_pcm_layout

    def folder_profile(folder: Path, label: str) -> tuple[list[str], set[str]]:
        # No formula: a mono file belongs to a Poly file when the Poly's
        # name starts the mono's name (followed by a separator or the end),
        # the same rule Re-embed pairs by.
        mono_stems: list[str] = []
        poly_stems: set[str] = set()
        wav_paths = list_qc_recursive_wav_files(folder)
        for index, wav_path in enumerate(wav_paths, start=1):
            try:
                channels = read_layout(wav_path)["channels"]
            except Exception:
                channels = 0
            if channels == 1:
                mono_stems.append(wav_path.stem)
            elif channels > 1:
                poly_stems.add(wav_path.stem.casefold())
            if progress is not None and (index == 1 or index == len(wav_paths) or index % 100 == 0):
                progress(f"Detecting {label}", index, len(wav_paths))
        return mono_stems, poly_stems

    def poly_score(poly_stems: set[str], mono_stems: list[str]) -> int:
        if not poly_stems:
            return 0
        return sum(1 for stem in mono_stems if reembed_target_group(stem, poly_stems) is not None)

    source_mono, source_poly = folder_profile(source, "Source")
    target_mono, target_poly = folder_profile(target, "Target")
    source_poly_score = poly_score(source_poly, target_mono)
    target_poly_score = poly_score(target_poly, source_mono)
    if source_poly_score > target_poly_score and source_poly_score > 0:
        return QcInputDetection("poly_split", source, target)
    if target_poly_score > source_poly_score and target_poly_score > 0:
        return QcInputDetection("poly_split", target, source, "Source/Target direction reversed: Poly folder detected on the right.")
    return QcInputDetection("folder", source, target)


def operation_control_capabilities(phase: str, modes: Optional[set[str]] = None) -> OperationControlCapabilities:
    if phase == "preview":
        return OperationControlCapabilities(True, True, "item")
    if phase == "qc":
        return OperationControlCapabilities(True, True, "item")
    if phase == "execute":
        return OperationControlCapabilities(True, True, "transaction" if modes == {"rename"} else "job")
    return OperationControlCapabilities()


def wait_for_preview_control(pause_requested: threading.Event, cancel_requested: threading.Event) -> None:
    while pause_requested.is_set() and not cancel_requested.is_set():
        time.sleep(0.05)
    if cancel_requested.is_set():
        raise PreviewCancelled()


QC_PROGRESS_LOG_SECONDS = 5.0


def qc_progress_log_due(state: dict, scope: str, label: str, index: int, total: int, now: float) -> bool:
    """Execution Log lines for QC progress go by time, not by count: the
    first and last step of every stage, and otherwise one line every
    QC_PROGRESS_LOG_SECONDS. A fixed "every 100" flooded nothing on fast
    stages but left minutes of silence on Poly ↔ Split audio compares
    (several seconds per file). `state` keeps (label, time) of the last
    logged line per scope."""
    last_label, last_time = state.get(scope, (None, None))
    due = (
        label != last_label
        or index <= 1
        or (total and index >= total)
        or last_time is None
        or now - last_time >= QC_PROGRESS_LOG_SECONDS
    )
    if due:
        state[scope] = (label, now)
    return bool(due)


def wait_for_qc_control(pause_requested: threading.Event, cancel_requested: threading.Event) -> None:
    while pause_requested.is_set() and not cancel_requested.is_set():
        time.sleep(0.05)
    if cancel_requested.is_set():
        raise QcCancelled()


@dataclass
class MetadataRead:
    core_xml: str = ""
    core_error: Optional[str] = None
    ixml_text: Optional[str] = None
    ixml_warning: Optional[str] = None
    ixml_root: Optional[ET.Element] = None
    ixml_error: Optional[str] = None


def job_is_ready(job: Job) -> bool:
    return job.status.startswith("READY")


def job_is_executable(job: Job) -> bool:
    return job_is_ready(job) and job.target is not None and (job.source is not None or bool(job.sources))


def job_needs_conflict_decision(job: Job) -> bool:
    return job.status.startswith("CONFLICT")


def job_is_actionable(job: Job) -> bool:
    """Everything Execute will actually attempt: ready jobs, plus jobs
    pending an "Ask" conflict decision — those aren't ready yet, but they
    still need to reach the execute loop so the user can be asked, rather
    than being silently left out the way a permanently-blocked job is."""
    if job.target is None or not (job.source is not None or bool(job.sources)):
        return False
    return job_is_ready(job) or job_needs_conflict_decision(job)


def mark_rename_jobs_no_change(jobs: list[Job]) -> list[Job]:
    for job in jobs:
        if job.mode in ("rename", "rename_copy"):
            job.status = "NO CHANGE"
    return jobs


def preview_cell_sort_key(column: str, value: object) -> tuple[object, ...]:
    text = str(value).strip()
    if text in ("", "-"):
        return (1,)
    if column in ("channel", "trk", "stream"):
        return (0, int(text)) if text.isdigit() else (0, float("inf"), text.casefold())
    parts = re.split(r"(\d+)", text.casefold())
    natural = tuple((1, int(part)) if part.isdigit() else (0, part) for part in parts)
    return (0, natural)


def qc_report_cell(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def processing_report_row(job: Job, status: str) -> dict[str, str]:
    return {
        "mode": job.mode,
        "source": str(job.source or ""),
        "target": str(job.target or ""),
        "final_target": str(job.final_target or ""),
        "channel": str(job.channel),
        "trk_number": str(job.trk_number or ""),
        "stream_number": str(job.stream_number or ""),
        "status": status,
    }


def merge_refreshed_jobs(existing: list[Job], refreshed: list[Job]) -> list[Job]:
    """Replace every job of each refreshed source with that source's new
    jobs, in place (at the position of its first old job); sources not seen
    before are appended. A Split source has one job per channel — the old
    per-job replacement let a 12-channel source's 12 new jobs overwrite one
    another in a single slot, keeping only the last."""
    refreshed_by_source: dict[Optional[Path], list[Job]] = {}
    for job in refreshed:
        refreshed_by_source.setdefault(job.source, []).append(job)
    merged: list[Job] = []
    placed: set[Optional[Path]] = set()
    for job in existing:
        if job.source in refreshed_by_source:
            if job.source not in placed:
                merged.extend(refreshed_by_source[job.source])
                placed.add(job.source)
            continue
        merged.append(job)
    for source, jobs in refreshed_by_source.items():
        if source not in placed:
            merged.extend(jobs)
    return merged


def summarize_processing_rows(rows: list[dict[str, str]]) -> tuple[int, int, int]:
    """(successes, failures, other) for the Execute completion summary.
    "other" is every row that's neither a literal success nor a
    failure/cancellation — BLOCKED (e.g. a truncated/corrupt source file
    caught during Preview), SKIP EXISTS, SKIPPED: conflict, a merge whose
    audio succeeded but metadata failed, etc. Counting only successes and
    failures let a batch containing real BLOCKED rows report "0 failed"
    and read as fully successful at a glance — the row and its real status
    were always written to the saved TSV, but nothing surfaced it in the
    one line most people actually read. This is what makes "other" show
    up in that line instead of only being discoverable by reading the
    whole TSV."""
    successes = sum(row["status"].startswith("OK") for row in rows)
    failures = sum(row["status"].startswith(("FAIL", "CANCELLED")) for row in rows)
    other = len(rows) - successes - failures
    return successes, failures, other


def run_command(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def find_executable(names: tuple[str, ...], candidates: tuple[str, ...]) -> str:
    for candidate in candidates + names:
        if Path(candidate).is_file() or shutil.which(candidate):
            return candidate
    return ""


def build_tool_installer_script(install_brew: bool) -> str:
    lines = ["#!/bin/bash", "set -e", "clear", f"echo '{APP_NAME} — installing required tools'", "echo"]
    if install_brew:
        lines += [
            "echo 'Homebrew not found. Installing Homebrew...'",
            "echo 'You may be asked to press RETURN or enter your Mac password below.'",
            "echo",
            '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
            "if [ -x /opt/homebrew/bin/brew ]; then",
            '  eval "$(/opt/homebrew/bin/brew shellenv)"',
            "elif [ -x /usr/local/bin/brew ]; then",
            '  eval "$(/usr/local/bin/brew shellenv)"',
            "fi",
            "echo",
        ]
    lines += [
        "echo 'Installing SoX and BWF MetaEdit...'",
        "brew install sox bwfmetaedit",
        "echo",
        f"echo 'Done. Close this window, then reopen {APP_NAME}.'",
        "read -n 1 -s -r -p 'Press any key to close this window...'",
        "echo",
    ]
    return "\n".join(lines) + "\n"


def tool_version(executable: str, args: list[str]) -> str:
    if not executable:
        return "missing"
    try:
        result = run_command([executable] + args, timeout=10)
    except Exception as exc:
        return f"error: {exc}"
    text = (result.stdout or result.stderr or "").strip().splitlines()
    return text[0] if text else ("OK" if result.returncode == 0 else f"exit {result.returncode}")


def sox_version(executable: str, args: list[str]) -> str:
    version = tool_version(executable, args)
    if version.startswith("error:"):
        # tool_version() only prefixes "error:" when the executable never
        # even ran (wrong file entirely, exec format error, permission
        # denied, ...) — falling through to the Homebrew lookup below would
        # report *the system's actual installed SoX version* as if it had
        # verified the user's chosen path, when it verified nothing at all.
        return version
    if re.search(r"\bSoX\s+v\s*\d", version, re.IGNORECASE) or re.search(r"\b\d+(?:\.\d+)+", version):
        return version
    brew = find_executable(("brew",), ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"))
    if brew:
        try:
            env = os.environ.copy()
            env.setdefault("HOMEBREW_NO_BOOTSNAP", "1")
            result = subprocess.run([brew, "list", "--versions", "sox"], capture_output=True, text=True, timeout=10, env=env)
            for part in (result.stdout or "").strip().split():
                if re.match(r"^\d+(?:\.\d+)+(?:_\d+)?$", part):
                    return part
        except Exception:
            pass
    return version if version and version != "OK" else "Unknown"


def is_split_wav(path: Path) -> bool:
    return A_NUMBER_PATTERN.match(path.name) is not None


def export_ixml(bwfmetaedit: str, wav_path: Path) -> str:
    sidecar = Path(str(wav_path) + ".iXML.xml")
    sidecar.unlink(missing_ok=True)
    try:
        result = run_command([bwfmetaedit, "--out-iXML-xml", "--continue-errors", str(wav_path)], timeout=90)
        if not sidecar.exists():
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "iXML sidecar was not exported")
        data = sidecar.read_text(encoding="utf-8", errors="replace")
        if not data.strip():
            raise RuntimeError("source iXML is empty")
        return data
    finally:
        sidecar.unlink(missing_ok=True)


def export_optional_ixml_for_inspector(bwfmetaedit: str, wav_path: Path) -> tuple[Optional[str], Optional[str]]:
    sidecar = Path(str(wav_path) + ".iXML.xml")
    sidecar.unlink(missing_ok=True)
    try:
        result = run_command([bwfmetaedit, "--out-iXML-xml", "--continue-errors", str(wav_path)], timeout=90)
        warning = result.stderr.strip() or result.stdout.strip() if result.returncode != 0 else None
        if not sidecar.exists():
            return None, warning or "iXML sidecar was not exported"
        data = sidecar.read_text(encoding="utf-8", errors="replace")
        if not data.strip():
            return None, warning or "iXML sidecar is empty"
        return data, warning
    finally:
        sidecar.unlink(missing_ok=True)


def export_core_xml(bwfmetaedit: str, wav_path: Path) -> str:
    result = run_command([bwfmetaedit, "--out-xml=-", "--continue-errors", str(wav_path)], timeout=90)
    if not result.stdout.strip():
        raise RuntimeError(result.stderr.strip() or "CORE XML export failed")
    return result.stdout


def read_metadata(bwfmetaedit: str, wav_path: Path) -> MetadataRead:
    metadata = MetadataRead()
    try:
        metadata.core_xml = export_core_xml(bwfmetaedit, wav_path)
    except Exception as exc:
        metadata.core_error = str(exc)
    try:
        metadata.ixml_text, metadata.ixml_warning = export_optional_ixml_for_inspector(bwfmetaedit, wav_path)
        if metadata.ixml_text is not None:
            metadata.ixml_root = parse_source_ixml(metadata.ixml_text, wav_path)
    except Exception as exc:
        metadata.ixml_error = str(exc)
    return metadata


# Recorder/DAW-specific single-letter prefixes seen in the BEXT Description
# fallback for $KEY=value pairs: "$" and "s" (Sound Devices), "a" (Aaton
# Cantar), "d" (the normalized form some re-exports and DAW renders write).
_DESCRIPTION_KEY_PREFIX = r"[\$sda]"


def read_serial_metadata_field(metadata: MetadataRead, tag: str, description: str) -> str:
    value = (metadata.ixml_root.findtext(f".//{tag}") or "").strip() if metadata.ixml_root is not None else ""
    if not value:
        value = report_value(metadata.core_xml or "", tag)
    if not value:
        match = re.search(rf"(?:^|\n){_DESCRIPTION_KEY_PREFIX}{re.escape(tag)}=([^\r\n]+)", description, re.IGNORECASE)
        value = match.group(1).strip() if match else ""
    return value


def parse_timecode_rate(rate_text: str) -> float:
    """iXML's TIMECODE_RATE is a rational "N/D" string (e.g. "24/1",
    "30000/1001" for 29.97), not a plain number — a bare float() on it always
    raised ValueError, silently failing this branch for every file even
    though the iXML value was perfectly valid, and pushing every recorder
    onto the Description-text fallback instead."""
    rate_text = rate_text.strip()
    if not rate_text:
        return 0.0
    numerator_text, separator, denominator_text = rate_text.partition("/")
    try:
        if separator:
            denominator = float(denominator_text)
            return float(numerator_text) / denominator if denominator else 0.0
        return float(numerator_text)
    except ValueError:
        return 0.0


def read_serial_speed(metadata: MetadataRead, description: str) -> Optional[tuple[float, bool]]:
    if metadata.ixml_root is not None:
        fps = parse_timecode_rate(metadata.ixml_root.findtext(".//TIMECODE_RATE") or "")
        if fps > 0:
            flag_text = (metadata.ixml_root.findtext(".//TIMECODE_FLAG") or "").strip().upper()
            return fps, flag_text == "DF"
    match = re.search(rf"(?:^|\n){_DESCRIPTION_KEY_PREFIX}SPEED=([^\r\n]+)", description, re.IGNORECASE)
    if match:
        return parse_speed_field(match.group(1))
    return None


def read_serial_sample_rate(metadata: MetadataRead, wav_path: Path) -> float:
    if metadata.ixml_root is not None:
        rate_text = (metadata.ixml_root.findtext(".//FILE_SAMPLE_RATE") or metadata.ixml_root.findtext(".//TIMESTAMP_SAMPLE_RATE") or "").strip()
        if rate_text.isdigit():
            return float(rate_text)
    coding_history = report_value(metadata.core_xml or "", "CodingHistory")
    match = re.search(r"F=(\d+)", coding_history)
    if match:
        return float(match.group(1))
    # Some recorders (e.g. this project's MixPre-6 II test files) write
    # neither a usable iXML sample-rate field nor a CodingHistory "F="
    # value — the WAV's own fmt chunk always has the real sample rate
    # regardless of what any metadata field says, so it's the last resort.
    try:
        return float(read_wav_pcm_layout(wav_path)["sample_rate"])
    except Exception:
        return 0.0


def read_serial_metadata(bwfmetaedit: str, wav_path: Path) -> tuple[str, int, str, str, str]:
    metadata = read_metadata(bwfmetaedit, wav_path)
    description = report_raw_value(metadata.core_xml or "", "Description")
    tape = read_serial_metadata_field(metadata, "TAPE", description)
    scene = read_serial_metadata_field(metadata, "SCENE", description)
    take = read_serial_metadata_field(metadata, "TAKE", description)
    time_reference_text = report_value(metadata.core_xml or "", "TimeReference")
    time_reference = int(time_reference_text) if time_reference_text.isdigit() else -1
    tc_display = str(time_reference) if time_reference >= 0 else ""
    if time_reference >= 0:
        speed = read_serial_speed(metadata, description)
        sample_rate = read_serial_sample_rate(metadata, wav_path)
        if speed is not None and sample_rate > 0:
            fps, drop_frame = speed
            try:
                tc_display = samples_to_smpte(time_reference, sample_rate, fps, drop_frame)
            except ValueError:
                pass
    return tape, time_reference, scene, take, tc_display


def read_ixml_history_original_filename(ixml_root: Optional[ET.Element]) -> str:
    if ixml_root is None:
        return ""
    return (ixml_root.findtext(".//HISTORY/ORIGINAL_FILENAME") or "").strip()


def collect_metadata_template_fields(bwfmetaedit: str, wav_path: Path) -> tuple[dict[str, str], dict[int, str]]:
    """Build the field dict + per-channel track-name table that
    expand_metadata_template() needs for one file — the "Rename by
    Metadata" counterpart of read_serial_metadata(), covering the wider
    token set ported from hsuanice's REAPER "Rename Source File from
    Metadata" script. A caller building an actual rename job should
    override interleave/chnum/baseindex with the job's own known
    stream/trk numbers where available; this function only knows what a
    single file, read in isolation, can tell on its own."""
    metadata = read_metadata(bwfmetaedit, wav_path)
    description = report_raw_value(metadata.core_xml or "", "Description")
    tape = read_serial_metadata_field(metadata, "TAPE", description)
    scene = read_serial_metadata_field(metadata, "SCENE", description)
    take = read_serial_metadata_field(metadata, "TAKE", description)
    project = read_serial_metadata_field(metadata, "PROJECT", description)
    ubits = read_serial_metadata_field(metadata, "UBITS", description)
    time_reference_text = report_value(metadata.core_xml or "", "TimeReference")
    time_reference = int(time_reference_text) if time_reference_text.isdigit() else -1
    speed_info = read_serial_speed(metadata, description)
    framerate = f"{speed_info[0]:g}" if speed_info else ""
    origination_date = report_value(metadata.core_xml or "", "OriginationDate")
    origination_time = report_value(metadata.core_xml or "", "OriginationTime")
    umid_hex = report_value(metadata.core_xml or "", "UMID")
    origsrcfile = read_ixml_history_original_filename(metadata.ixml_root)

    trk_table: dict[int, str] = build_trk_table_from_ixml_rows(ixml_track_rows(metadata.ixml_root)) if metadata.ixml_root is not None else {}
    try:
        recorder_channel = split_recorder_channel(bwfmetaedit, wav_path)
    except Exception:
        recorder_channel = None
    if recorder_channel is not None and recorder_channel in trk_table:
        trk = trk_table[recorder_channel]
    else:
        trk = next((trk_table[key] for key in sorted(trk_table) if trk_table[key]), "")
    chnum = str(recorder_channel) if recorder_channel is not None else ""

    try:
        layout = read_wav_pcm_layout(wav_path)
        samplerate = str(layout["sample_rate"])
        channels = str(layout["channels"])
        length = f"{layout['frames'] / layout['sample_rate']:.3f}" if layout["sample_rate"] else ""
    except Exception:
        samplerate = channels = length = ""

    fields = {
        "srcfile": wav_path.name,
        "srcbase": wav_path.stem,
        "srcext": wav_path.suffix.lstrip("."),
        "srcpath": str(wav_path),
        "srcdir": str(wav_path.parent),
        "samplerate": samplerate,
        "channels": channels,
        "length": length,
        "project": project,
        "scene": scene,
        "take": take,
        "tape": tape,
        "ubits": ubits,
        "framerate": framerate,
        "speed": framerate,
        "date": origination_date,
        "time": origination_time,
        "year": origination_date[:4] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", origination_date or "") else "",
        "originationdate": origination_date,
        "originationtime": origination_time,
        "startoffset": str(time_reference) if time_reference >= 0 else "",
        "umid": umid_hex,
        "umid_pt": umid_to_pt(umid_hex),
        "trk": trk,
        "trkall": concat_trk_names(trk_table),
        "chnum": chnum,
        "interleave": chnum,
        "baseindex": "",
        "origsrcfile": origsrcfile,
    }
    return fields, trk_table


def report_raw_value(xml_text: str, tag: str) -> str:
    match = re.search(rf"<{re.escape(tag)}(?:\s[^>]*)?>(.*?)</{re.escape(tag)}>", xml_text, re.DOTALL)
    return match.group(1) if match else ""


def report_value(xml_text: str, tag: str) -> str:
    match = re.search(rf"<{re.escape(tag)}(?:\s[^>]*)?>(.*?)</{re.escape(tag)}>", xml_text, re.DOTALL)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def write_core(bwfmetaedit: str, target: Path, core_xml: str) -> None:
    args = [bwfmetaedit]
    for field in CORE_FIELDS:
        value = report_value(core_xml, field)
        if value:
            args.append(f"--{field}={value}")
    time_reference = report_value(core_xml, "TimeReference")
    if time_reference.isdigit():
        args.append(f"--Timereference={time_reference}")
    if len(args) == 1:
        return
    result = run_command(args + [str(target)], timeout=90)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "CORE write failed")


# Standard RIFF/WAVE chunks whose payload is not changed by extracting one
# channel. Structural/audio chunks (fmt, data, fact) are generated by SoX,
# while iXML is rebuilt because its TRACK_LIST must become mono.
SPLIT_PRESERVED_WAV_CHUNKS = (
    b"JUNK", b"bext", b"LIST", b"ID3 ", b"cue ", b"smpl", b"inst", b"PAD ",
)


def preserve_split_wav_chunks(source: Path, target: Path) -> None:
    """Preserve standard non-structural WAV chunks after a SoX split."""
    for chunk_id in SPLIT_PRESERVED_WAV_CHUNKS:
        copy_wav_chunk_from_source(source, target, chunk_id)


def write_ixml(bwfmetaedit: str, target: Path, ixml_text: str) -> None:
    sidecar = Path(str(target) + ".iXML.xml")
    sidecar.write_text(ixml_text, encoding="utf-8")
    try:
        result = run_command([bwfmetaedit, "--in-iXML-xml", "--continue-errors", str(target)], timeout=90)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "iXML write failed")
    finally:
        sidecar.unlink(missing_ok=True)


def write_reembed_metadata(bwfmetaedit: str, source: Path, target: Path, mono_ixml: str) -> None:
    """Apply Re-embed's channel-specific iXML and raw metadata policy."""
    write_ixml(bwfmetaedit, target, mono_ixml)
    copy_wav_chunk_from_source(source, target, b"bext")
    remove_wav_chunk(target, b"LIST")


def write_split_metadata(bwfmetaedit: str, source: Path, target: Path, mono_ixml: str) -> None:
    """Apply Split's raw chunk preservation and channel-specific iXML policy."""
    preserve_split_wav_chunks(source, target)
    write_ixml(bwfmetaedit, target, mono_ixml)


def write_combine_metadata(bwfmetaedit: str, sources: list[Path], target: Path, combined_ixml: str) -> None:
    """Apply Combine's first-source BEXT and rebuilt poly iXML policy."""
    copy_wav_chunk_from_source(sources[0], target, b"bext")
    write_ixml(bwfmetaedit, target, combined_ixml)


def poly_ixml_from_mono_sources(bwfmetaedit: str, sources: list[Path]) -> str:
    if not sources:
        raise RuntimeError("No mono sources to combine")
    root = parse_source_ixml(export_ixml(bwfmetaedit, sources[0]), sources[0])
    combined_tracks: list[ET.Element] = []
    for index, source in enumerate(sources, start=1):
        match = A_NUMBER_PATTERN.match(source.name)
        fallback_channel = int(match["ch"]) if match else index
        source_ixml = export_ixml(bwfmetaedit, source)
        parsed_root = parse_source_ixml(source_ixml, source)
        track, _trk_number = track_from_mono_ixml(source_ixml, fallback_channel, index, parsed_root)
        combined_tracks.append(track)
    track_list = first_track_list(root)
    for child in list(track_list):
        track_list.remove(child)
    ET.SubElement(track_list, "TRACK_COUNT").text = str(len(combined_tracks))
    for track in combined_tracks:
        track_list.append(track)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def planned_combined_ixml_for_inspector(bwfmetaedit: str, sources: list[Path]) -> tuple[list[tuple[str, str]], Optional[ET.Element]]:
    if not sources:
        return [("iXML", "No source iXML metadata available for planned merged output")], None
    source_ixml: list[tuple[Path, str, ET.Element]] = []
    missing_sources: list[Path] = []
    for source in sources:
        ixml_text, _warning = export_optional_ixml_for_inspector(bwfmetaedit, source)
        if ixml_text is None:
            missing_sources.append(source)
            continue
        source_ixml.append((source, ixml_text, parse_source_ixml(ixml_text, source)))
    if len(missing_sources) == len(sources):
        return [("iXML", "No source iXML metadata available for planned merged output")], None
    if missing_sources:
        return [("iXML", "Partial / unavailable: Some source files do not contain iXML metadata.")], None

    root = copy.deepcopy(source_ixml[0][2])
    combined_tracks: list[ET.Element] = []
    for index, (source, ixml_text, parsed_root) in enumerate(source_ixml, start=1):
        match = A_NUMBER_PATTERN.match(source.name)
        fallback_channel = int(match["ch"]) if match else index
        track, _trk_number = track_from_mono_ixml(ixml_text, fallback_channel, index, parsed_root)
        combined_tracks.append(track)
    track_list = first_track_list(root)
    for child in list(track_list):
        track_list.remove(child)
    ET.SubElement(track_list, "TRACK_COUNT").text = str(len(combined_tracks))
    for track in combined_tracks:
        track_list.append(track)
    planned_ixml = ET.tostring(root, encoding="unicode", xml_declaration=True)
    return structured_ixml_sections(planned_ixml, root), root


def read_mono_trk_number(bwfmetaedit: str, wav_path: Path, fallback_channel: int) -> int:
    ixml = export_ixml(bwfmetaedit, wav_path)
    _mono_ixml, trk_number = extract_mono_ixml(ixml, 1, fallback_channel=fallback_channel)
    return trk_number


def soxi_channels(soxi: str, wav_path: Path) -> int:
    result = run_command([soxi, "-c", str(wav_path)], timeout=30)
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        raise RuntimeError(result.stderr.strip() or "soxi channel read failed")
    return int(result.stdout.strip())


def split_with_sox(sox: str, source: Path, target: Path, channel: int, overwrite: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        raise RuntimeError("target exists")
    result = run_command([sox, str(source), str(target), "remix", str(channel)], timeout=300)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "SoX split failed")


def combine_with_sox(sox: str, sources: list[Path], target: Path, overwrite: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        raise RuntimeError("target exists")
    result = run_command([sox, "-M"] + [str(source) for source in sources] + [str(target)], timeout=600)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "SoX combine failed")
    clear_wav_channel_mask(target)


def format_file_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
        value /= 1024
    return f"{size} B"


def format_start_offset(time_reference: str, sample_rate: str) -> str:
    if not time_reference.isdigit() or not sample_rate.isdigit() or int(sample_rate) == 0:
        return "-"
    milliseconds = round(int(time_reference) * 1000 / int(sample_rate))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours}:{minutes:02}:{seconds:02}.{milliseconds:03}"


def get_audio_info(soxi: str, wav_path: Path, core_xml: str = "") -> dict[str, str]:
    result = run_command([soxi, str(wav_path)], timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "soxi info read failed")
    labels: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if ":" in line:
            label, value = line.split(":", 1)
            labels[label.strip()] = value.strip()
    structure = wav_structure_info(wav_path)
    duration = labels.get("Duration", "-").split(" =", 1)[0]
    total_samples = "-"
    duration_match = re.search(r"=\s*([\d,]+)\s+samples", labels.get("Duration", ""))
    if duration_match:
        total_samples = duration_match.group(1).replace(",", "")
    sample_rate = labels.get("Sample Rate", "-")
    return {
        "format": str(structure["format"]),
        "sample_rate": sample_rate,
        "bit_depth": labels.get("Precision", "-").replace("-bit", ""),
        "encoding": labels.get("Sample Encoding", "-"),
        "channels": labels.get("Channels", "-"),
        "duration": duration,
        "total_samples": total_samples,
        "block_align": str(structure["block_align"]),
        "data_start": str(structure["data_start"]),
        "start_offset": format_start_offset(report_value(core_xml, "TimeReference"), sample_rate),
        "channel_configuration": channel_configuration(structure["channel_mask"]),
    }


def compare_poly_mono_content(bwfmetaedit: str, poly_path: Path, mono_path: Path) -> bool:
    """Whether `mono_path`'s samples exactly match the one channel of
    `poly_path` that actually corresponds to it — determined the same way
    Poly ↔ Split QC does it, via real Recorder Channel metadata rather than
    a filename or column position. Backs File ↔ File's "Audio Content" check
    when the pair is a whole Poly recording versus one already-split channel
    of it, where a literal whole-file byte comparison would always disagree
    (their PCM layouts are fundamentally different — interleaved multi-
    channel vs. one channel's worth of bytes) even when the content is
    genuinely identical."""
    recorder_channel = split_recorder_channel(bwfmetaedit, mono_path)
    mapping = poly_track_mapping(bwfmetaedit, poly_path)
    stream_index = next((stream for stream, channel in mapping if channel == recorder_channel), None)
    if stream_index is None:
        raise RuntimeError(f"Poly file has no channel matching {mono_path.name}'s Recorder Ch {recorder_channel}")
    _frames, result = compare_wav_pcm_stream(poly_path, stream_index, mono_path)
    return result == "PASS"


def compare_basic_audio(soxi: str, bwfmetaedit: str, source_path: Path, target_path: Path) -> dict[str, object]:
    source_audio = get_audio_info(soxi, source_path)
    target_audio = get_audio_info(soxi, target_path)
    audio_fields = (
        ("Sample Rate", "sample_rate", True),
        ("Bit Depth", "bit_depth", True),
        ("Channels", "channels", False),
        ("Frames", "total_samples", True),
        ("Duration", "duration", True),
        ("Audio Format", "encoding", True),
    )
    rows = []
    compared_results = []
    duration_equal = (
        source_audio["total_samples"] == target_audio["total_samples"]
        and source_audio["sample_rate"] == target_audio["sample_rate"]
    )
    for label, key, is_compared in audio_fields:
        equal = duration_equal if key == "duration" else source_audio[key] == target_audio[key]
        rows.append({"label": label, "source": source_audio[key], "target": target_audio[key], "result": "✓" if is_compared and equal else ("≠" if is_compared else "—")})
        if is_compared:
            compared_results.append(equal)
    # A plugin/render pass can leave every soxi-reported property above
    # identical while silently altering the actual samples (dropouts, gain,
    # DSP) — only worth the I/O of a real byte compare once those properties
    # already agree, since a header mismatch already fails the row above.
    if all(compared_results):
        source_channels = int(source_audio["channels"])
        target_channels = int(target_audio["channels"])
        try:
            if source_channels == target_channels:
                content_equal = compare_wav_pcm_content(source_path, target_path)
            elif target_channels == 1 and source_channels > 1:
                content_equal = compare_poly_mono_content(bwfmetaedit, source_path, target_path)
            elif source_channels == 1 and target_channels > 1:
                content_equal = compare_poly_mono_content(bwfmetaedit, target_path, source_path)
            else:
                raise RuntimeError(f"channel counts differ ({source_channels} vs {target_channels}) and neither file is mono")
            content_text = "Identical" if content_equal else "Different"
            rows.append({"label": "Audio Content", "source": content_text, "target": content_text, "result": "✓" if content_equal else "≠"})
            compared_results.append(content_equal)
        except Exception as exc:
            rows.append({"label": "Audio Content", "source": f"BLOCKED: {exc}", "target": f"BLOCKED: {exc}", "result": f"BLOCKED: {exc}"})
            compared_results.append(False)
    else:
        rows.append({"label": "Audio Content", "source": "—", "target": "—", "result": "—"})
    return {
        "file": [
            {"label": "File Name", "source": source_path.name, "target": target_path.name, "result": "—"},
            {"label": "File Size", "source": format_file_size(source_path.stat().st_size), "target": format_file_size(target_path.stat().st_size), "result": "—"},
        ],
        "audio": rows,
        "result": "PASS" if all(compared_results) else "DIFFERENT",
    }


QC_METADATA_FIELDS: tuple[str, ...] = (
    "Tape", "Scene", "Take", "Circled", "Originator",
    "Origination Date", "Origination Time", "TC (samples)", "Description",
)


def read_qc_metadata_fields(bwfmetaedit: str, wav_path: Path) -> dict[str, str]:
    metadata = read_metadata(bwfmetaedit, wav_path)
    description = report_raw_value(metadata.core_xml or "", "Description")
    core = metadata.core_xml or ""
    return {
        "Tape": read_serial_metadata_field(metadata, "TAPE", description),
        "Scene": read_serial_metadata_field(metadata, "SCENE", description),
        "Take": read_serial_metadata_field(metadata, "TAKE", description),
        "Circled": read_serial_metadata_field(metadata, "CIRCLED", description),
        "Originator": report_value(core, "Originator").strip(),
        "Origination Date": report_value(core, "OriginationDate").strip(),
        "Origination Time": report_value(core, "OriginationTime").strip(),
        "TC (samples)": report_value(core, "TimeReference").strip(),
        "Description": description.strip(),
    }


def read_reembed_identity(bwfmetaedit: str, wav_path: Path) -> Optional[tuple[str, str, str, str, str]]:
    """A (Tape, Scene, Take, OriginationDate, OriginationTime) tuple that a
    DAW's consolidate/render step leaves untouched even when it rewrites the
    filename's own digits (confirmed against real Pro Tools and REAPER
    renders) — used as Re-embed's last-resort pairing key, alongside the
    real Recorder Channel, once a Target's filename no longer shares an
    exact {base} with its Source. Returns None if any field is blank, since
    an all-blank tuple would spuriously "match" between unrelated files."""
    metadata = read_metadata(bwfmetaedit, wav_path)
    core = metadata.core_xml or ""
    description = report_raw_value(core, "Description")
    identity = (
        read_serial_metadata_field(metadata, "TAPE", description),
        read_serial_metadata_field(metadata, "SCENE", description),
        read_serial_metadata_field(metadata, "TAKE", description),
        report_value(core, "OriginationDate").strip(),
        report_value(core, "OriginationTime").strip(),
    )
    return identity if all(identity) else None


def compare_qc_metadata_fields(bwfmetaedit: str, source_path: Path, target_path: Path) -> dict[str, object]:
    source_fields = read_qc_metadata_fields(bwfmetaedit, source_path)
    target_fields = read_qc_metadata_fields(bwfmetaedit, target_path)
    rows = []
    all_equal = True
    for label in QC_METADATA_FIELDS:
        source_value = source_fields.get(label, "")
        target_value = target_fields.get(label, "")
        equal = source_value == target_value
        all_equal = all_equal and equal
        rows.append({"label": label, "source": source_value or "-", "target": target_value or "-", "result": "✓" if equal else "≠"})
    return {"rows": rows, "result": "PASS" if all_equal else "DIFFERENT"}


def augment_qc_rows_with_metadata(bwfmetaedit: str, rows: list[dict], source_key: str, target_key: str, progress=None) -> None:
    """Mutate a list of QC rows in place, adding a 'metadata_result' key kept
    separate from the row's existing (audio/structural) 'result' — the two
    are shown as their own columns rather than blended into one, so a reader
    can tell a metadata mismatch apart from an audio one. Rows without both
    a source and target Path are left with metadata_result='-' (nothing to
    compare). This runs two bwfmetaedit reads per row after the Audio pass
    already finished, with nothing else marking the wait — a large Folder QC
    batch could sit for several silent seconds here, so callers should pass
    `progress(stage, current, total)` the same way the Audio pass does."""
    total = len(rows)
    for index, row in enumerate(rows, start=1):
        source_path = row.get(source_key)
        target_path = row.get(target_key)
        if not isinstance(source_path, Path) or not isinstance(target_path, Path):
            row["metadata_result"] = "-"
            row["metadata_comparison"] = None
            if progress is not None:
                progress("Comparing Metadata", index, total)
            continue
        try:
            metadata_comparison = compare_qc_metadata_fields(bwfmetaedit, source_path, target_path)
            row["metadata_result"] = metadata_comparison["result"]
            row["metadata_comparison"] = metadata_comparison
        except Exception as exc:
            row["metadata_result"] = f"BLOCKED: {exc}"
            row["metadata_comparison"] = None
        if progress is not None:
            progress("Comparing Metadata", index, total)


def qc_rows_overall_result(rows: list[dict]) -> str:
    if not rows:
        return "DIFFERENT"
    for row in rows:
        if str(row.get("result", "")).startswith("BLOCKED"):
            return "BLOCKED"
    audio_pass = all(row.get("result") in ("PASS", "-") for row in rows)
    metadata_pass = all(row.get("metadata_result", "-") in ("-", "PASS") for row in rows)
    return "PASS" if audio_pass and metadata_pass else "DIFFERENT"


QC_REPORT_DETAIL_FIELDS: tuple[str, ...] = (
    "File Name", "File Size", "Sample Rate", "Bit Depth", "Channels",
    "Frames", "Duration", "Audio Format", "Audio Content",
) + QC_METADATA_FIELDS


def qc_detail_field_lookup(row: dict, label: str) -> Optional[dict]:
    """Find the {label, source, target, result} entry for one detail field
    (e.g. "Sample Rate" or "Tape") inside a Folder/Poly QC row's cached
    per-pair breakdowns. Returns None when that field's check never ran for
    this row — a Poly row has no `audio_comparison` (Channels legitimately
    differs between a poly file and its mono split, so `compare_basic_audio`
    is never meaningful there), and either comparison can be None when its
    checkbox was off or a path was missing (MISSING/EXTRA rows)."""
    audio_comparison = row.get("audio_comparison")
    if audio_comparison:
        for field_row in audio_comparison["file"] + audio_comparison["audio"]:
            if field_row["label"] == label:
                return field_row
    metadata_comparison = row.get("metadata_comparison")
    if metadata_comparison:
        for field_row in metadata_comparison["rows"]:
            if field_row["label"] == label:
                return field_row
    return None


QC_REPORT_FILLS: dict[str, PatternFill] = {
    "pass": PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid"),
    "different": PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"),
    "blocked": PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid"),
}


def qc_result_fill_key(result: object) -> Optional[str]:
    text = str(result)
    if text.startswith("BLOCKED") or text == "AMBIGUOUS":
        return "blocked"
    if text in ("✓", "PASS"):
        return "pass"
    if text in ("≠", "DIFFERENT", "MISSING", "EXTRA") or text.startswith("DIFFERENT"):
        return "different"
    return None


def source_path_is_stale(source_text: str, jobs_source_root: Optional[Path]) -> bool:
    """True once the live Source text no longer points at the folder the
    current jobs list was actually built from — the cached list (and
    whatever Execute would act on) still describes the old folder at that
    point, even though the Source field on screen already shows the new
    one."""
    if jobs_source_root is None:
        return False
    text = source_text.strip()
    if not text:
        return False
    return Path(text).expanduser() != jobs_source_root


def target_path_is_stale(output_text: str, jobs_source_root: Path, jobs_output_root: Path) -> bool:
    """True once the live Target text would resolve to a different folder
    than the one the current jobs list was actually built against — mirrors
    exactly how `preview()` resolves Target (falling back to the same
    synthetic `__preview_output__` folder when Target is blank), so a
    Target that hasn't meaningfully changed since the last Preview never
    triggers a false clear."""
    resolved = resolve_output_root(jobs_source_root, output_text) or (jobs_source_root.parent / "__preview_output__")
    return resolved != jobs_output_root


def qc_update_selected_button_enabled(has_metadata: bool, has_selection: bool) -> bool:
    """"Update Metadata to Selected" only makes sense once the user has
    actually selected a row to update — a metadata compare having run at
    all isn't enough, since with no row selected (or every row already
    PASS) the button led to nothing but a "select rows first" dialog."""
    return has_metadata and has_selection


def qc_report_detail_columns() -> list[str]:
    columns = []
    for label in QC_REPORT_DETAIL_FIELDS:
        columns.extend((f"{label} (Source)", f"{label} (Target)"))
    return columns


def qc_report_detail_cells(row: dict) -> list[tuple[object, Optional[str]]]:
    """One (value, fill_key) pair per Source/Target column in
    `qc_report_detail_columns()`, in the same order — the caller colors both
    cells of a field the same way instead of adding a separate ✓/≠ column."""
    cells: list[tuple[object, Optional[str]]] = []
    for label in QC_REPORT_DETAIL_FIELDS:
        field_row = qc_detail_field_lookup(row, label)
        if field_row is None:
            cells.extend(((None, None), (None, None)))
        else:
            fill_key = qc_result_fill_key(field_row["result"])
            cells.append((field_row["source"], fill_key))
            cells.append((field_row["target"], fill_key))
    return cells


def build_file_qc_comparison(soxi: str, bwfmetaedit: str, source: Path, target: Path, include_audio: bool, include_metadata: bool) -> dict[str, object]:
    sections = [("FILE", [
        {"label": "File Name", "source": source.name, "target": target.name, "result": "—"},
        {"label": "File Size", "source": format_file_size(source.stat().st_size), "target": format_file_size(target.stat().st_size), "result": "—"},
    ])]
    category_results = []
    if include_audio:
        audio_comparison = compare_basic_audio(soxi, bwfmetaedit, source, target)
        sections.append(("AUDIO", audio_comparison["audio"]))
        category_results.append(audio_comparison["result"] == "PASS")
    if include_metadata:
        metadata_comparison = compare_qc_metadata_fields(bwfmetaedit, source, target)
        sections.append(("METADATA", metadata_comparison["rows"]))
        category_results.append(metadata_comparison["result"] == "PASS")
    result = "PASS" if category_results and all(category_results) else ("DIFFERENT" if category_results else "SKIPPED")
    return {"sections": sections, "result": result}


def apply_qc_metadata_update(bwfmetaedit: str, source_path: Path, target_path: Path) -> None:
    """Copy Source's BEXT chunk and iXML onto Target, replacing what's there."""
    copy_wav_chunk_from_source(source_path, target_path, b"bext")
    remove_wav_chunk(target_path, b"LIST")
    ixml_text, warning = export_optional_ixml_for_inspector(bwfmetaedit, source_path)
    if ixml_text is None:
        raise RuntimeError(warning or f"Source iXML unavailable: {source_path.name}")
    write_ixml(bwfmetaedit, target_path, ixml_text)


def apply_qc_metadata_update_poly_to_split(bwfmetaedit: str, poly_path: Path, stream_index: int, split_path: Path) -> None:
    """Copy Poly's BEXT chunk and the channel-specific slice of its iXML onto one Split target."""
    poly_ixml_text, warning = export_optional_ixml_for_inspector(bwfmetaedit, poly_path)
    if poly_ixml_text is None:
        raise RuntimeError(warning or f"Poly iXML unavailable: {poly_path.name}")
    mono_ixml, _recorder_channel = extract_mono_ixml(poly_ixml_text, stream_index, strict=True)
    copy_wav_chunk_from_source(poly_path, split_path, b"bext")
    remove_wav_chunk(split_path, b"LIST")
    write_ixml(bwfmetaedit, split_path, mono_ixml)


def list_qc_wav_files(folder: Path, progress=None) -> dict[str, Path]:
    wav_paths = []
    if progress is not None:
        progress(0, 0)
    for path in folder.rglob("*"):
        if path.is_file() and path.suffix.lower() == ".wav":
            wav_paths.append(path)
            if progress is not None and len(wav_paths) % 100 == 0:
                progress(len(wav_paths), 0)
    wav_paths.sort(key=lambda path: path.relative_to(folder).as_posix().casefold())
    if progress is not None:
        progress(len(wav_paths), len(wav_paths))
    return {path.relative_to(folder).as_posix(): path for path in wav_paths}


def qc_name_prefix_keys(stem: str, keys: dict[str, list[str]]) -> list[str]:
    """Every key in `keys` that starts this name, followed by a separator
    or the end of the name (same rule as reembed_target_group), longest
    first."""
    name = stem.casefold()
    found = []
    for length in range(len(name), 0, -1):
        prefix = name[:length]
        if prefix in keys and (length == len(name) or name[length] in REEMBED_NAME_DELIMITERS):
            found.append(prefix)
    return found


def match_qc_folder_files_by_name(
    bwfmetaedit: str,
    remaining_source: dict[str, Path],
    remaining_target: dict[str, Path],
    progress=None,
) -> tuple[list[tuple[str, str]], set[str], set[str]]:
    """Pair up whatever's left after exact-filename matching, without any
    formula: a Source and a Target belong together when either one's name
    starts with the other's name or the other's name without its channel
    suffix (".A3", "_3" ...) — both directions, so Source may be the
    original ("X.A1") or the render ("X_AAP.A1"). The recorder channel then
    decides. Never guessed: a file that still has two candidates (after
    preferring the longest shared name) is returned as ambiguous.
    Returns (pairs, ambiguous source filenames, ambiguous target filenames);
    does not mutate the inputs."""
    def key_index(files: dict[str, Path]) -> dict[str, list[str]]:
        index: dict[str, list[str]] = {}
        for filename, path in files.items():
            for key in reembed_source_bases(path.stem):
                index.setdefault(key.casefold(), []).append(filename)
        return index

    source_keys = key_index(remaining_source)
    target_keys = key_index(remaining_target)
    # (source, target) -> length of the longest name they share
    related: dict[tuple[str, str], int] = {}
    for filename, path in remaining_target.items():
        for key in qc_name_prefix_keys(path.stem, source_keys):
            for source_filename in source_keys[key]:
                pair_key = (source_filename, filename)
                related[pair_key] = max(related.get(pair_key, 0), len(key))
    for filename, path in remaining_source.items():
        for key in qc_name_prefix_keys(path.stem, target_keys):
            for target_filename in target_keys[key]:
                pair_key = (filename, target_filename)
                related[pair_key] = max(related.get(pair_key, 0), len(key))
    if not related:
        return [], set(), set()

    # One bwfmetaedit read per file that has a candidate at all.
    involved = sorted({("source", s) for s, _t in related} | {("target", t) for _s, t in related}, key=lambda item: (item[0], item[1].casefold()))
    channels: dict[tuple[str, str], Optional[int]] = {}
    for index, (side, filename) in enumerate(involved, start=1):
        path = remaining_source[filename] if side == "source" else remaining_target[filename]
        try:
            channels[(side, filename)] = split_recorder_channel(bwfmetaedit, path)
        except Exception:
            channels[(side, filename)] = None
        if progress is not None and (index == 1 or index == len(involved) or index % 100 == 0):
            progress("Matching by name + recorder channel", index, len(involved))

    # Same recorder channel only; then each Target keeps only the Sources
    # sharing its longest name, and each Source only the Targets sharing its
    # longest name — so "Scene_3_Mix_AAP.A1" goes to "Scene_3_Mix", not
    # "Scene_3".
    same_channel = {
        pair_key: score
        for pair_key, score in related.items()
        if channels[("source", pair_key[0])] is not None and channels[("source", pair_key[0])] == channels[("target", pair_key[1])]
    }
    best_for_target: dict[str, int] = {}
    for (_source_filename, target_filename), score in same_channel.items():
        best_for_target[target_filename] = max(best_for_target.get(target_filename, 0), score)
    kept = {pair_key: score for pair_key, score in same_channel.items() if score == best_for_target[pair_key[1]]}
    best_for_source: dict[str, int] = {}
    for (source_filename, _target_filename), score in kept.items():
        best_for_source[source_filename] = max(best_for_source.get(source_filename, 0), score)
    kept = {pair_key: score for pair_key, score in kept.items() if score == best_for_source[pair_key[0]]}

    targets_of: dict[str, list[str]] = {}
    sources_of: dict[str, list[str]] = {}
    for source_filename, target_filename in kept:
        targets_of.setdefault(source_filename, []).append(target_filename)
        sources_of.setdefault(target_filename, []).append(source_filename)
    pairs: list[tuple[str, str]] = []
    ambiguous_sources: set[str] = set()
    ambiguous_targets: set[str] = set()
    for source_filename, target_filenames in targets_of.items():
        if len(target_filenames) == 1 and len(sources_of[target_filenames[0]]) == 1:
            pairs.append((source_filename, target_filenames[0]))
        else:
            ambiguous_sources.add(source_filename)
            ambiguous_targets.update(target_filenames)
    for target_filename, source_filenames in sources_of.items():
        if len(source_filenames) > 1:
            ambiguous_targets.add(target_filename)
            ambiguous_sources.update(source_filenames)
    pairs = [pair for pair in pairs if pair[0] not in ambiguous_sources and pair[1] not in ambiguous_targets]
    pairs.sort(key=lambda pair: pair[0].casefold())
    return pairs, ambiguous_sources, ambiguous_targets


def compare_qc_folders(soxi: str, source_folder: Path, target_folder: Path, progress=None, bwfmetaedit: str = "", include_audio: bool = True, include_metadata: bool = False) -> dict[str, object]:
    source_files = list_qc_wav_files(source_folder, lambda current, total: progress("Scanning Source", current, total) if progress is not None else None)
    target_files = list_qc_wav_files(target_folder, lambda current, total: progress("Scanning Target", current, total) if progress is not None else None)

    remaining_source = dict(source_files)
    remaining_target = dict(target_files)
    pairs: list[tuple[str, Optional[Path], Optional[Path]]] = []
    for filename in sorted(remaining_source, key=str.casefold):
        if filename in remaining_target:
            pairs.append((filename, remaining_source.pop(filename), remaining_target.pop(filename)))

    ambiguous_sources: set[str] = set()
    ambiguous_targets: set[str] = set()
    if bwfmetaedit and remaining_source and remaining_target:
        name_pairs, ambiguous_sources, ambiguous_targets = match_qc_folder_files_by_name(bwfmetaedit, remaining_source, remaining_target, progress)
        for source_filename, target_filename in name_pairs:
            display_name = f"{source_filename} ↔ {target_filename}" if source_filename != target_filename else source_filename
            pairs.append((display_name, remaining_source.pop(source_filename), remaining_target.pop(target_filename)))

    pairs.extend((filename, path, None) for filename, path in remaining_source.items())
    pairs.extend((filename, None, path) for filename, path in remaining_target.items())
    pairs.sort(key=lambda item: item[0].casefold())

    rows = []
    total = len(pairs)
    for index, (display_name, source_path, target_path) in enumerate(pairs, start=1):
        audio_comparison = None
        if source_path is None:
            result = "AMBIGUOUS" if display_name in ambiguous_targets else "EXTRA"
        elif target_path is None:
            result = "AMBIGUOUS" if display_name in ambiguous_sources else "MISSING"
        elif include_audio:
            audio_comparison = compare_basic_audio(soxi, bwfmetaedit, source_path, target_path)
            result = str(audio_comparison["result"])
        else:
            result = "-"
        rows.append({
            "filename": display_name,
            "source": "✓" if source_path is not None else "—",
            "target": "✓" if target_path is not None else "—",
            "result": result,
            "source_path": source_path,
            "target_path": target_path,
            "audio_comparison": audio_comparison,
        })
        if progress is not None:
            progress("Comparing", index, total)
    if include_metadata:
        augment_qc_rows_with_metadata(bwfmetaedit, rows, "source_path", "target_path", progress)
    return {
        "rows": rows,
        "result": qc_rows_overall_result(rows),
    }


def poly_track_mapping(bwfmetaedit: str, poly_path: Path) -> list[tuple[int, int]]:
    ixml_text, _warning = export_optional_ixml_for_inspector(bwfmetaedit, poly_path)
    if ixml_text is None:
        raise RuntimeError("Poly Track List unavailable")
    root = parse_source_ixml(ixml_text, poly_path)
    tracks = root.findall(".//TRACK_LIST/TRACK")
    mapping = []
    for track in tracks:
        recorder_channel = text_int(track, "CHANNEL_INDEX")
        stream_index = text_int(track, "INTERLEAVE_INDEX")
        if recorder_channel is None or stream_index is None:
            raise RuntimeError("Poly Track List unavailable")
        mapping.append((stream_index, recorder_channel))
    poly_channels = read_wav_pcm_layout(poly_path)["channels"]
    if len(mapping) != poly_channels or len({stream for stream, _channel in mapping}) != len(mapping) or len({channel for _stream, channel in mapping}) != len(mapping):
        raise RuntimeError("Poly Track List unavailable")
    mapping.sort()
    if [stream for stream, _channel in mapping] != list(range(1, poly_channels + 1)):
        raise RuntimeError("Poly Track List unavailable")
    return mapping


def split_recorder_channel(bwfmetaedit: str, split_path: Path) -> int:
    ixml_text, _warning = export_optional_ixml_for_inspector(bwfmetaedit, split_path)
    if ixml_text is None:
        raise RuntimeError(f"Recorder Ch metadata unavailable: {split_path.name}")
    root = parse_source_ixml(ixml_text, split_path)
    recorder_channels = {
        recorder_channel
        for track in root.findall(".//TRACK_LIST/TRACK")
        for recorder_channel in [text_int(track, "CHANNEL_INDEX")]
        if recorder_channel is not None
    }
    if not recorder_channels:
        # Some re-exports (e.g. bwfmetaedit reading iXML written by a DAW
        # render/glue rather than the original recorder) can't map the
        # TRACK_LIST/TRACK/CHANNEL_INDEX hierarchy back to proper nesting and
        # flatten it to TRACK_LIST_TRACK_CHANNEL_INDEX (with a _2, _3... for
        # each additional track) instead. Fall back to that shape.
        recorder_channels = {
            int(element.text.strip())
            for element in root.iter()
            if re.fullmatch(r"TRACK_LIST_TRACK_CHANNEL_INDEX(?:_\d+)?", element.tag)
            and element.text
            and element.text.strip().lstrip("-").isdigit()
        }
    if len(recorder_channels) != 1:
        raise RuntimeError(f"Recorder Ch metadata unavailable: {split_path.name}")
    return recorder_channels.pop()


def compare_poly_split_qc(
    bwfmetaedit: str,
    poly_path: Path,
    split_folder: Path,
    split_candidates: Optional[list[Path]] = None,
    split_recorder_channels: Optional[dict[Path, int]] = None,
    track_mapping: Optional[list[tuple[int, int]]] = None,
    progress=None,
    include_audio: bool = True,
) -> dict[str, object]:
    if track_mapping is None:
        try:
            mapping = poly_track_mapping(bwfmetaedit, poly_path)
        except Exception as exc:
            return {"rows": [], "result": "BLOCKED", "message": f"BLOCKED: {exc}{structural_damage_hint([poly_path])}"}
    else:
        mapping = track_mapping
    candidates: dict[int, list[Path]] = {}
    base = poly_path.stem.casefold()
    if split_candidates is None:
        for path in split_folder.iterdir():
            if not path.is_file():
                continue
            match = A_NUMBER_PATTERN.match(path.name)
            if match and match["base"].casefold() == base:
                try:
                    recorder_channel = split_recorder_channel(bwfmetaedit, path)
                except Exception as exc:
                    return {"rows": [], "result": "BLOCKED", "message": f"BLOCKED: {exc}{structural_damage_hint([path])}"}
                candidates.setdefault(recorder_channel, []).append(path)
    else:
        for path in split_candidates:
            try:
                recorder_channel = split_recorder_channels[path] if split_recorder_channels is not None else split_recorder_channel(bwfmetaedit, path)
            except Exception as exc:
                return {"rows": [], "result": "BLOCKED", "message": f"BLOCKED: {exc}{structural_damage_hint([path])}"}
            candidates.setdefault(recorder_channel, []).append(path)
    duplicate_channels = sorted(channel for channel, paths in candidates.items() if len(paths) > 1)
    if duplicate_channels:
        channels = ", ".join(str(channel) for channel in duplicate_channels)
        return {"rows": [], "result": "BLOCKED", "message": f"BLOCKED: DUPLICATE CHANNEL {channels}"}

    rows = []
    expected_channels = {channel for _stream, channel in mapping}
    for stream_index, recorder_channel in mapping:
        split_path = candidates.get(recorder_channel, [None])[0]
        if split_path is None:
            rows.append({"stream": str(stream_index), "channel": str(recorder_channel), "split_file": "—", "split_path": None, "frames": "—", "result": "MISSING"})
            if progress is not None:
                progress()
            continue
        if include_audio:
            try:
                frames, result = compare_wav_pcm_stream(poly_path, stream_index, split_path)
                frame_text = str(frames)
            except Exception as exc:
                frame_text = "—"
                result = f"DIFFERENT: {exc}"
        else:
            frame_text, result = "—", "-"
        rows.append({"stream": str(stream_index), "channel": str(recorder_channel), "split_file": split_path.name, "split_path": split_path, "poly_path": poly_path, "frames": frame_text, "result": result})
        if progress is not None:
            progress()
    for recorder_channel in sorted(set(candidates) - expected_channels):
        split_path = candidates[recorder_channel][0]
        rows.append({"stream": "—", "channel": str(recorder_channel), "split_file": split_path.name, "split_path": split_path, "frames": "—", "result": "EXTRA"})
        if progress is not None:
            progress()
    return {
        "rows": rows,
        "result": "PASS" if rows and all(row["result"] in ("PASS", "-") for row in rows) else "DIFFERENT",
        "message": "",
    }


def compare_poly_split_folders(bwfmetaedit: str, poly_folder: Path, split_folder: Path, progress=None, prepare_progress=None, include_audio: bool = True, include_metadata: bool = False) -> dict[str, object]:
    """No formula: a Split file belongs to the Poly file whose name starts
    its name (longest, followed by a separator or the end — ".A1", "_A1",
    "_AAP.A1", " render 001" all work), and its recorder channel picks the
    Poly stream. Split files that start no Poly name are EXTRA."""
    poly_by_base: dict[str, list[Path]] = {}
    skipped_rows = []
    blocked_rows = []
    if prepare_progress is not None:
        prepare_progress("Scanning Poly files", 0, 1)
    poly_files = list_qc_recursive_wav_files(poly_folder)
    if prepare_progress is not None:
        prepare_progress("Scanning Poly files", 1, 1)
    for index, path in enumerate(poly_files, start=1):
        try:
            channels = read_wav_pcm_layout(path)["channels"]
        except Exception as exc:
            blocked_rows.append({"poly_file": path.name, "poly_path": path, "stream": "—", "channel": "—", "split_file": "—", "split_path": None, "frames": "—", "result": f"BLOCKED: {exc}{structural_damage_hint([path])}", "poly_result": "BLOCKED"})
            if prepare_progress is not None:
                prepare_progress("Inspecting Poly layouts", index, len(poly_files))
            continue
        if channels == 1:
            skipped_rows.append({"poly_file": path.name, "poly_path": path, "stream": "—", "channel": "—", "split_file": "—", "split_path": None, "frames": "—", "result": "SKIP: NOT POLY", "poly_result": "SKIP: NOT POLY"})
            if prepare_progress is not None:
                prepare_progress("Inspecting Poly layouts", index, len(poly_files))
            continue
        poly_by_base.setdefault(path.stem.casefold(), []).append(path)
        if prepare_progress is not None:
            prepare_progress("Inspecting Poly layouts", index, len(poly_files))

    split_by_base: dict[str, list[Path]] = {}
    split_recorder_channels: dict[Path, int] = {}
    split_metadata_errors: dict[Path, str] = {}
    if prepare_progress is not None:
        prepare_progress("Scanning Split files", 0, 1)
    split_files = list_qc_recursive_wav_files(split_folder)
    if prepare_progress is not None:
        prepare_progress("Scanning Split files", 1, 1)
    poly_stems = set(poly_by_base)
    poly_paths_all = {path for paths in poly_by_base.values() for path in paths}
    # A mono original in the Poly folder (SKIP: NOT POLY) keeps its own
    # renders/copies on its row instead of listing them as EXTRA.
    skipped_by_stem: dict[str, dict] = {}
    for row in skipped_rows:
        skipped_by_stem.setdefault(row["poly_path"].stem.casefold(), row)
    skipped_paths = {row["poly_path"] for row in skipped_rows}
    unmatched_split_files: list[Path] = []
    matched_split_files: list[tuple[str, Path]] = []
    for path in split_files:
        if path in poly_paths_all:
            continue  # same folder on both sides: a Poly file is never its own Split
        base = reembed_target_group(path.stem, poly_stems)
        if base is not None:
            matched_split_files.append((base, path))
            continue
        if path in skipped_paths:
            continue  # same folder on both sides: the mono file is already its own SKIP row
        mono_stem = reembed_target_group(path.stem, set(skipped_by_stem)) if skipped_by_stem else None
        if mono_stem is not None:
            skipped_row = skipped_by_stem[mono_stem]
            if skipped_row["split_path"] is None:
                skipped_row.update(split_file=path.name, split_path=path)
            else:
                skipped_rows.append({**skipped_row, "split_file": path.name, "split_path": path})
            continue
        unmatched_split_files.append(path)
    # Same folder on both sides: a mono file that is a Split of a Poly is not
    # also a SKIP row.
    matched_paths = {path for _base, path in matched_split_files}
    skipped_rows[:] = [row for row in skipped_rows if row["poly_path"] not in matched_paths]
    for index, (base, path) in enumerate(matched_split_files, start=1):
        split_by_base.setdefault(base, []).append(path)
        try:
            split_recorder_channels[path] = split_recorder_channel(bwfmetaedit, path)
        except Exception as exc:
            split_metadata_errors[path] = str(exc)
        if prepare_progress is not None:
            prepare_progress("Reading Split metadata", index, len(matched_split_files))

    mappings: dict[Path, list[tuple[int, int]]] = {}
    mapping_errors: dict[Path, str] = {}
    total = len(blocked_rows) + len(skipped_rows) + len(unmatched_split_files)
    poly_groups = list(poly_by_base.items())
    for index, (base, poly_paths) in enumerate(poly_groups, start=1):
        candidates = split_by_base.get(base, [])
        if len(poly_paths) > 1:
            total += len(poly_paths)
            if prepare_progress is not None:
                prepare_progress("Reading Poly metadata", index, len(poly_groups))
            continue
        poly_path = poly_paths[0]
        try:
            mapping = poly_track_mapping(bwfmetaedit, poly_path)
            mappings[poly_path] = mapping
            if any(path in split_metadata_errors for path in candidates):
                total += 1
                if prepare_progress is not None:
                    prepare_progress("Reading Poly metadata", index, len(poly_groups))
                continue
            candidate_counts: dict[int, int] = {}
            for path in candidates:
                channel = split_recorder_channels[path]
                candidate_counts[channel] = candidate_counts.get(channel, 0) + 1
            if any(count > 1 for count in candidate_counts.values()):
                total += 1
                if prepare_progress is not None:
                    prepare_progress("Reading Poly metadata", index, len(poly_groups))
                continue
            candidate_channels = {split_recorder_channels[path] for path in candidates}
            expected_channels = {channel for _stream, channel in mapping}
            total += len(mapping) + len(candidate_channels - expected_channels)
        except Exception as exc:
            mapping_errors[poly_path] = f"BLOCKED: {exc}{structural_damage_hint([poly_path])}"
            total += 1
        if prepare_progress is not None:
            prepare_progress("Reading Poly metadata", index, len(poly_groups))

    completed = 0

    def report_one() -> None:
        nonlocal completed
        completed += 1
        if progress is not None:
            progress("Comparing", completed, total)

    rows = []
    for base in sorted(poly_by_base, key=str.casefold):
        poly_paths = poly_by_base[base]
        candidates = split_by_base.get(base, [])
        if len(poly_paths) > 1:
            for poly_path in poly_paths:
                rows.append({"poly_file": poly_path.name, "poly_path": poly_path, "stream": "—", "channel": "—", "split_file": "—", "split_path": None, "frames": "—", "result": "BLOCKED: DUPLICATE POLY BASE", "poly_result": "BLOCKED"})
                report_one()
            continue
        poly_path = poly_paths[0]
        if poly_path in mapping_errors:
            rows.append({"poly_file": poly_path.name, "poly_path": poly_path, "stream": "—", "channel": "—", "split_file": "—", "split_path": None, "frames": "—", "result": mapping_errors[poly_path], "poly_result": "BLOCKED"})
            report_one()
            continue
        error_path = next((path for path in candidates if path in split_metadata_errors), None)
        if error_path is not None:
            rows.append({"poly_file": poly_path.name, "poly_path": poly_path, "stream": "—", "channel": "—", "split_file": error_path.name, "split_path": error_path, "frames": "—", "result": f"BLOCKED: {split_metadata_errors[error_path]}", "poly_result": "BLOCKED"})
            report_one()
            continue
        detail = compare_poly_split_qc(bwfmetaedit, poly_path, split_folder, candidates, split_recorder_channels, mappings[poly_path], report_one, include_audio)
        if detail["rows"]:
            for detail_row in detail["rows"]:
                rows.append({"poly_file": poly_path.name, "poly_path": poly_path, **detail_row, "poly_result": detail["result"]})
        else:
            rows.append({"poly_file": poly_path.name, "poly_path": poly_path, "stream": "—", "channel": "—", "split_file": "—", "split_path": None, "frames": "—", "result": detail["message"] or detail["result"], "poly_result": detail["result"]})
            report_one()

    for split_path in sorted(unmatched_split_files, key=lambda path: path.name.casefold()):
        rows.append({"poly_file": "—", "poly_path": None, "stream": "—", "channel": "—", "split_file": split_path.name, "split_path": split_path, "frames": "—", "result": "EXTRA", "poly_result": "EXTRA"})
        report_one()
    rows.extend(blocked_rows)
    rows.extend(skipped_rows)
    for _row in blocked_rows + skipped_rows:
        report_one()
    rows.sort(key=lambda row: str(row["poly_file"]).casefold())
    if include_metadata:
        # Same silent-tail risk augment_qc_rows_with_metadata's own
        # docstring warns about — this used to call it with no progress
        # callback at all, so a large batch would sit at "100%" (the Audio
        # pass's own total) for as long as the Metadata pass's bwfmetaedit
        # reads took, with nothing in the log to say why.
        augment_qc_rows_with_metadata(bwfmetaedit, rows, "poly_path", "split_path", progress)

    relevant_rows = [row for row in rows if row["result"] != "SKIP: NOT POLY"]
    result = qc_rows_overall_result(relevant_rows)
    return {"rows": rows, "result": result}


def element_lines(element: Optional[ET.Element], indent: int = 0) -> list[str]:
    if element is None or not list(element):
        return []
    lines: list[str] = []
    for child in element:
        if list(child):
            lines.append("  " * indent + f"{child.tag}:")
            lines.extend(element_lines(child, indent + 1))
        else:
            value = (child.text or "").strip()
            if value:
                lines.append("  " * indent + f"{child.tag}: {value}")
    return lines


def channel_configuration(channel_mask: object) -> str:
    if not isinstance(channel_mask, int) or channel_mask == 0:
        return "None"
    names = [abbreviation for flag, abbreviation in WINDOWS_SPEAKER_CHANNELS.items() if channel_mask & flag]
    return " ".join(names) if names else "None"


def format_xml_fields(element: Optional[ET.Element], fields: Optional[tuple[str, ...]] = None) -> str:
    if element is None:
        return ""
    children = list(element)
    if fields is not None:
        children = [element.find(field) for field in fields]
    lines = []
    for child in children:
        if child is not None and not list(child):
            lines.append(f"{child.tag.replace('_', ' ').title()}: {(child.text or '').strip() or '-'}")
    return "\n".join(lines)


def ixml_track_rows(ixml_root: ET.Element) -> list[tuple[str, str, str]]:
    track_list = ixml_root.find(".//TRACK_LIST")
    if track_list is not None:
        return [(track.findtext("INTERLEAVE_INDEX") or "-", track.findtext("CHANNEL_INDEX") or "-", track.findtext("NAME") or "-") for track in track_list.findall("TRACK")]
    # Some re-exports (e.g. bwfmetaedit reading iXML written by a DAW
    # render/glue rather than the original recorder) can't map the
    # TRACK_LIST/TRACK/CHANNEL_INDEX hierarchy back to proper nesting and
    # flatten it to TRACK_LIST_TRACK_CHANNEL_INDEX (with a _2, _3... suffix
    # per extra track) instead. split_recorder_channel() already falls back
    # to this shape to find a file's real channel — the Inspector's table
    # needs the same fallback, or it shows "No Track List metadata" for a
    # file the rest of the app can clearly read channel info from.
    suffixes = sorted(
        {match.group(1) or "" for element in ixml_root.iter() if (match := re.fullmatch(r"TRACK_LIST_TRACK_CHANNEL_INDEX(_\d+)?", element.tag))},
        key=lambda suffix: int(suffix[1:]) if suffix else 0,
    )
    return [
        (
            ixml_root.findtext(f"TRACK_LIST_TRACK_INTERLEAVE_INDEX{suffix}") or "-",
            ixml_root.findtext(f"TRACK_LIST_TRACK_CHANNEL_INDEX{suffix}") or "-",
            ixml_root.findtext(f"TRACK_LIST_TRACK_NAME{suffix}") or "-",
        )
        for suffix in suffixes
    ]


def format_aaton_cantar(cantar: Optional[ET.Element]) -> str:
    if cantar is None:
        return "No Aaton Cantar metadata"
    lines = element_lines(cantar.find("INPUT_SETTINGS"))
    summary = format_xml_fields(cantar, ("REF_LEVEL", "CANTAR_KEY", "SCTK_TEMPLATE"))
    if summary:
        lines = summary.splitlines() + (["", "Input Settings"] + lines if lines else [])
    return "\n".join(lines) or "No Aaton Cantar metadata"


def aaton_track_rows(ixml_root: ET.Element) -> list[tuple[str, str, str]]:
    return [(data.findtext("INDICE") or "-", data.findtext("ACTIVE") or "-", data.findtext("NAME") or "-") for data in ixml_root.findall("./AATON_CANTAR/ALL_TRK_NAME/DATA")]


def structured_ixml_sections(ixml_text: str, root: Optional[ET.Element] = None) -> list[tuple[str, str]]:
    root = root if root is not None else ET.fromstring(ixml_text)
    sections: list[tuple[str, str]] = []
    general_fields = ("IXML_VERSION", "PROJECT", "SCENE", "TAKE", "TAPE", "CIRCLED", "UBITS", "NOTE", "FILE_UID", "TAKE_TYPE")
    sections.append(("iXML - General", format_xml_fields(root, general_fields) or "No General metadata"))
    for tag, title in (("SPEED", "iXML - Speed"), ("HISTORY", "iXML - History"), ("FILE_SET", "iXML - File Set"), ("BEXT", "iXML - BEXT")):
        sections.append((title, format_xml_fields(root.find(tag)) or f"No {title.removeprefix('iXML - ')} metadata"))
    track_list = root.find(".//TRACK_LIST")
    sections.append(("iXML - Track List", f"Track Count: {track_list.findtext('TRACK_COUNT') if track_list is not None else '-'}" if track_list is not None else "No Track List metadata"))
    sections.append(("Aaton Cantar", format_aaton_cantar(root.find("AATON_CANTAR"))))
    known = set(general_fields) | {"SPEED", "HISTORY", "FILE_SET", "TRACK_LIST", "BEXT", "AATON_CANTAR", "SYNC_POINT_LIST", "USER"}
    for child in root:
        if child.tag not in known and list(child):
            sections.append((f"Recorder - {child.tag.replace('_', ' ').title()}", "\n".join(element_lines(child)) or f"No {child.tag} metadata"))
    return sections


def inspector_metadata(bwfmetaedit: str, wav_path: Path) -> tuple[str, str, list[tuple[str, str]], Optional[ET.Element]]:
    metadata = read_metadata(bwfmetaedit, wav_path)
    try:
        if metadata.core_error is not None:
            raise RuntimeError(metadata.core_error)
        core_root = ET.fromstring(metadata.core_xml)
        core_lines = element_lines(core_root.find(".//Core"))
        core_text = "\n".join(core_lines) if core_lines else "No BEXT / Core metadata"
    except Exception as exc:
        metadata.core_xml = ""
        core_text = f"Unavailable: {exc}"
    if metadata.ixml_error is not None:
        ixml_sections = [("iXML", f"Unavailable: {metadata.ixml_error}")]
    elif metadata.ixml_text is None:
        ixml_sections = [("iXML", f"Unavailable: {metadata.ixml_warning or 'iXML export failed'}")]
    else:
        ixml_sections = structured_ixml_sections(metadata.ixml_text, metadata.ixml_root)
        if metadata.ixml_warning:
            ixml_sections.insert(0, ("iXML - Warning", f"BWF MetaEdit warning: {metadata.ixml_warning}"))
    return metadata.core_xml, core_text, ixml_sections, metadata.ixml_root


class AudioInspector:
    BACKGROUND = PANEL_BACKGROUND
    TEXT_COLOR = TEXT_COLOR
    MUTED_TEXT_COLOR = MUTED_TEXT_COLOR

    def __init__(self, app: "WorkflowApp") -> None:
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("Audio Inspector")
        self.window.geometry("1180x760")
        self.window.minsize(680, 480)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        content = ttk.Frame(self.window, padding=12)
        content.pack(fill=tk.BOTH, expand=True)
        self.left_header_var = tk.StringVar(value="SOURCE")
        self.right_header_var = tk.StringVar(value="TARGET")
        empty_prompt = "Drag & Drop a WAV file here, or Load a file"
        self.source_path: Optional[Path] = None
        self.target_path: Optional[Path] = None
        self.source_path_var = tk.StringVar(value=empty_prompt)
        self.target_path_var = tk.StringVar(value=empty_prompt)
        self.source_frame = ttk.Frame(content, padding=(8, 6))
        self.source_frame.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 4))
        self.target_frame = ttk.Frame(content, padding=(8, 6))
        self.target_frame.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 4))
        ttk.Label(self.source_frame, textvariable=self.left_header_var, font=("SF Pro Display", 14, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Button(self.source_frame, text="Load", command=self.browse_inspector_source).grid(row=0, column=1, sticky="e")
        self.source_path_label = ttk.Label(self.source_frame, textvariable=self.source_path_var, wraplength=480, justify=tk.LEFT)
        self.source_path_label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(self.target_frame, textvariable=self.right_header_var, font=("SF Pro Display", 14, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Button(self.target_frame, text="Load", command=self.browse_inspector_target).grid(row=0, column=1, sticky="e")
        self.target_path_label = ttk.Label(self.target_frame, textvariable=self.target_path_var, wraplength=480, justify=tk.LEFT)
        self.target_path_label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.source_frame.columnconfigure(0, weight=1)
        self.target_frame.columnconfigure(0, weight=1)
        self.source_frame.bind("<Configure>", lambda event: self._resize_path_label(event, self.source_path_label))
        self.target_frame.bind("<Configure>", lambda event: self._resize_path_label(event, self.target_path_label))
        for widget in (self.source_frame, *self.source_frame.winfo_children()):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self.drop_inspector_source)
        for widget in (self.target_frame, *self.target_frame.winfo_children()):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self.drop_inspector_target)
        self.canvas = tk.Canvas(content, highlightthickness=0, background=self.BACKGROUND)
        self.scrollbar = ttk.Scrollbar(content, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self.scrollbar.grid(row=0, column=2, rowspan=2, sticky="ns")
        self.comparison_frame = ttk.Frame(self.canvas)
        self.comparison_window = self.canvas.create_window((0, 0), window=self.comparison_frame, anchor="nw")
        self.comparison_frame.bind("<Configure>", self._update_scroll_region)
        self.comparison_frame.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<Configure>", self._resize_comparison_frame)
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self._scroll_region_after: Optional[str] = None
        self._load_request_id = 0
        self._load_results: Queue[tuple[int, InspectorRenderModel]] = Queue()
        self._side_request_ids = {"source": 0, "target": 0}
        self._side_load_results: Queue[tuple[str, int, list[tuple[str, str]], Optional[ET.Element]]] = Queue()
        self._left_sections = [("FILE", empty_prompt)]
        self._right_sections = [("FILE", empty_prompt)]
        self._left_ixml: Optional[ET.Element] = None
        self._right_ixml: Optional[ET.Element] = None
        self._left_header = "SOURCE"
        self._right_header = "TARGET"
        self._blank_missing_sections = False
        self._pair_first_sections = False
        self._current_heading_order: list[str] = []
        self._load_results_after: Optional[str] = None
        content.rowconfigure(1, weight=1)
        content.columnconfigure(0, weight=1, uniform="inspector-column")
        content.columnconfigure(1, weight=1, uniform="inspector-column")
        self._show_empty()
        self._poll_load_results()

    @staticmethod
    def _resize_path_label(event, label: ttk.Label) -> None:
        resize_wrapped_label(event, label, padding=16, minimum=120, tolerance=8)

    def _update_scroll_region(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _schedule_scroll_region_update(self) -> None:
        if self._scroll_region_after is None:
            self._scroll_region_after = self.window.after_idle(self._flush_scroll_region_update)

    def _flush_scroll_region_update(self) -> None:
        self._scroll_region_after = None
        self._update_scroll_region()

    def _resize_comparison_frame(self, event) -> None:
        self.canvas.itemconfigure(self.comparison_window, width=event.width)

    def _on_mouse_wheel(self, event) -> str:
        self.canvas.yview_scroll(-int(event.delta / 120), "units")
        return "break"

    def close(self) -> None:
        if self._load_results_after is not None:
            self.window.after_cancel(self._load_results_after)
        self.window.destroy()
        self.app.inspector = None
        self.app.inspector_window = None

    def _browse_inspector_file(self, side: str) -> None:
        initialdir = self.app._existing_dialog_dir(self.app.source_var.get(), self.app.last_source_dialog_dir)
        path = filedialog.askopenfilename(
            parent=self.window,
            title=f"Load {side.title()} WAV",
            initialdir=initialdir,
            filetypes=(("WAV audio", "*.wav *.WAV"), ("All files", "*")),
        )
        if path:
            wav_path = Path(path)
            if not wav_path.is_file() or wav_path.suffix.lower() != ".wav":
                path_var = self.source_path_var if side == "source" else self.target_path_var
                path_var.set("Select a WAV file.")
                return
            if side == "source":
                self.load_inspector_source(wav_path)
            else:
                self.load_inspector_target(wav_path)

    def browse_inspector_source(self) -> None:
        self._browse_inspector_file("source")

    def browse_inspector_target(self) -> None:
        self._browse_inspector_file("target")

    def _drop_inspector_file(self, event, side: str) -> str:
        dropped_paths = [Path(path).expanduser() for path in self.window.tk.splitlist(event.data)]
        wav_path = next((path for path in dropped_paths if path.is_file() and path.suffix.lower() == ".wav"), None)
        if wav_path is not None:
            if side == "source":
                self.load_inspector_source(wav_path)
            else:
                self.load_inspector_target(wav_path)
        elif dropped_paths:
            path_var = self.source_path_var if side == "source" else self.target_path_var
            path_var.set("Drop a WAV file here.")
        return "break"

    def drop_inspector_source(self, event) -> str:
        return self._drop_inspector_file(event, "source")

    def drop_inspector_target(self, event) -> str:
        return self._drop_inspector_file(event, "target")

    def _poll_load_results(self) -> None:
        self._load_results_after = None
        try:
            while True:
                request_id, result = self._load_results.get_nowait()
                if request_id == self._load_request_id and self.window.winfo_exists():
                    self._apply_prepared_comparison(result)
        except Empty:
            pass
        try:
            while True:
                side, request_id, sections, ixml_root = self._side_load_results.get_nowait()
                if request_id == self._side_request_ids[side] and self.window.winfo_exists():
                    self._apply_side_result(side, sections, ixml_root)
        except Empty:
            pass
        if self.window.winfo_exists():
            self._load_results_after = self.window.after(25, self._poll_load_results)

    def _show_empty(self) -> None:
        self._render_comparison(self._left_sections, self._right_sections, reset_scroll=True)

    def _render_comparison(
        self,
        left_sections: list[tuple[str, str]],
        right_sections: list[tuple[str, str]],
        left_ixml: Optional[ET.Element] = None,
        right_ixml: Optional[ET.Element] = None,
        reset_scroll: bool = False,
        left_header: str = "SOURCE",
        right_header: str = "TARGET",
        blank_missing_sections: bool = False,
        pair_first_sections: bool = False,
    ) -> None:
        old_heading_order = self._current_heading_order
        target_heading = None if reset_scroll else self._heading_at_top()
        self.left_header_var.set(left_header)
        self.right_header_var.set(right_header)
        left_by_heading = dict(left_sections)
        right_by_heading = dict(right_sections)
        headings = list(left_by_heading)
        headings.extend(heading for heading in right_by_heading if heading not in left_by_heading)
        for child in self.comparison_frame.winfo_children():
            child.destroy()
        self.comparison_frame.columnconfigure(0, weight=1, uniform="comparison")
        self.comparison_frame.columnconfigure(1, weight=1, uniform="comparison")
        new_heading_order: list[str] = []
        if pair_first_sections and left_sections and right_sections:
            left_heading, left_body = left_sections[0]
            right_heading, right_body = right_sections[0]
            self._create_section(self.comparison_frame, 0, 0, left_heading, left_body, left_ixml)
            self._create_section(self.comparison_frame, 0, 1, right_heading, right_body, right_ixml)
            left_by_heading.pop(left_heading, None)
            right_by_heading.pop(right_heading, None)
            headings = [heading for heading in headings if heading not in (left_heading, right_heading)]
            row_offset = 1
            new_heading_order.append(left_heading)
        else:
            row_offset = 0
        for row, heading in enumerate(headings, start=row_offset):
            if blank_missing_sections and heading not in left_by_heading:
                self._create_empty_section(self.comparison_frame, row, 0)
            else:
                self._create_section(self.comparison_frame, row, 0, heading, left_by_heading.get(heading, "No Data"), left_ixml)
            if blank_missing_sections and heading not in right_by_heading:
                self._create_empty_section(self.comparison_frame, row, 1)
            else:
                self._create_section(self.comparison_frame, row, 1, heading, right_by_heading.get(heading, "No Data"), right_ixml)
            new_heading_order.append(heading)
        self.window.update_idletasks()
        self._update_scroll_region()
        self._current_heading_order = new_heading_order
        if reset_scroll:
            self.canvas.yview_moveto(0.0)
        else:
            self._scroll_to_heading(self._nearest_available_heading(target_heading, old_heading_order, new_heading_order))

    def _heading_at_top(self) -> Optional[str]:
        """Which section (by heading) is closest to the viewport's top edge
        — the first row whose top edge is at or below the current scroll
        position, not whichever section's body the edge happens to be
        scrolled partway into. Scrolled halfway down a long section's body,
        that section's own heading is already out of view above the edge;
        the section the user actually lands back on is the next one down.
        Used before a rebuild, while the old layout's grid geometry is still
        valid, to know what to scroll back to afterward."""
        if not self._current_heading_order:
            return None
        top_y = self.canvas.canvasy(0)
        for row, heading in enumerate(self._current_heading_order):
            bbox = self.comparison_frame.grid_bbox(0, row)
            if bbox is None:
                continue
            if bbox[1] >= top_y:
                return heading
        return self._current_heading_order[-1]

    @staticmethod
    def _nearest_available_heading(target: Optional[str], old_order: list[str], new_headings: list[str]) -> Optional[str]:
        """`target` is the heading the viewport was scrolled into before the
        rebuild. If the new file has that same section, keep it. If not
        (e.g. it had no Track List but the old one did), walk backward
        through the section that used to precede it until one is found that
        the new file also has — landing on the closest earlier section
        rather than snapping back to the very top."""
        if target is None:
            return None
        new_set = set(new_headings)
        if target in new_set:
            return target
        if target not in old_order:
            return None
        for candidate in reversed(old_order[:old_order.index(target)]):
            if candidate in new_set:
                return candidate
        return None

    def _scroll_to_heading(self, heading: Optional[str]) -> None:
        if heading is None or heading not in self._current_heading_order:
            self.canvas.yview_moveto(0.0)
            return
        row = self._current_heading_order.index(heading)
        bbox = self.comparison_frame.grid_bbox(0, row)
        scroll_bbox = self.canvas.bbox("all")
        if bbox is None or scroll_bbox is None:
            self.canvas.yview_moveto(0.0)
            return
        total_height = scroll_bbox[3] - scroll_bbox[1]
        if total_height <= 0:
            self.canvas.yview_moveto(0.0)
            return
        self.canvas.yview_moveto(max(0.0, min(1.0, bbox[1] / total_height)))

    def _create_empty_section(self, parent: ttk.Frame, row: int, column: int) -> None:
        section = tk.Frame(parent, background=self.BACKGROUND, padx=8, pady=8)
        section.grid(row=row, column=column, sticky="nsew", padx=(0, 4) if column == 0 else (4, 0), pady=2)
        self._bind_mouse_wheel(section)
        self._bind_side_drop(section, column)

    def _create_section(self, parent: ttk.Frame, row: int, column: int, heading: str, body: str, ixml_root: Optional[ET.Element]) -> None:
        section = tk.Frame(parent, background=self.BACKGROUND, padx=8, pady=8)
        section.grid(row=row, column=column, sticky="nsew", padx=(0, 4) if column == 0 else (4, 0), pady=2)
        tk.Label(section, text=heading, font=("SF Pro Display", 14, "bold"), background=self.BACKGROUND, foreground=self.TEXT_COLOR).pack(anchor="w")
        ttk.Separator(section, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(3, 7))
        if heading == "iXML - Track List" and ixml_root is not None:
            self._create_table(section, ("Stream", "Recorder Ch", "Name"), ixml_track_rows(ixml_root), "No Track List metadata")
        elif heading == "Aaton Cantar" and ixml_root is not None:
            if body:
                self._create_selectable_text(section, body, pady=(0, 8))
            self._create_table(section, ("Index", "Active", "Name"), aaton_track_rows(ixml_root), "No Aaton track-name metadata")
        elif heading == "Metadata Template Tokens":
            self._create_metadata_token_table(section, self.source_path if column == 0 else self.target_path)
        else:
            self._create_selectable_text(section, body or "No Data")
        self._bind_mouse_wheel(section)
        self._bind_side_drop(section, column)

    def _bind_side_drop(self, widget: tk.Misc, column: int) -> None:
        handler = self.drop_inspector_source if column == 0 else self.drop_inspector_target
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<Drop>>", handler)
        for child in widget.winfo_children():
            self._bind_side_drop(child, column)

    def _create_selectable_text(self, parent: ttk.Frame, body: str, pady=0) -> tk.Text:
        text = tk.Text(
            parent,
            height=max(1, body.count("\n") + 1),
            wrap=tk.WORD,
            relief=tk.FLAT,
            borderwidth=0,
            background=self.BACKGROUND,
            foreground=self.MUTED_TEXT_COLOR if body in ("No Data", "No Target") or body.startswith("Target Not Found") else self.TEXT_COLOR,
            insertbackground=self.TEXT_COLOR,
            highlightthickness=0,
            font=("SF Mono", 11),
            padx=0,
            pady=0,
        )
        text.insert("1.0", body)
        text.configure(state=tk.DISABLED)
        text.pack(anchor=tk.NW, fill=tk.X, pady=pady)
        text.bind("<Configure>", lambda _event, widget=text: self._schedule_text_height(widget), add="+")
        text.bind("<Command-c>", self._copy_text_selection, add="+")
        text.bind("<Control-c>", self._copy_text_selection, add="+")
        return text

    def _schedule_text_height(self, text: tk.Text) -> None:
        after_id = getattr(text, "_height_after_id", None)
        if after_id is None:
            text._height_after_id = text.after_idle(lambda: self._fit_text_height(text))

    def _fit_text_height(self, text: tk.Text) -> None:
        text._height_after_id = None
        try:
            display_line_count = text.count("1.0", "end-1c", "displaylines")
            if display_line_count:
                required_height = max(1, int(display_line_count[0]) + 1)
                if int(text.cget("height")) != required_height:
                    text.configure(height=required_height)
                    self._schedule_scroll_region_update()
        except tk.TclError:
            pass

    def _copy_text_selection(self, event) -> str:
        text = event.widget
        try:
            selected_text = text.get("sel.first", "sel.last")
        except tk.TclError:
            return "break"
        self.window.clipboard_clear()
        self.window.clipboard_append(selected_text)
        return "break"

    def _create_table(self, parent: ttk.Frame, headings: tuple[str, str, str], rows: list[tuple[str, str, str]], empty_message: str) -> None:
        if not rows:
            self._create_selectable_text(parent, empty_message)
            return
        table = ttk.Treeview(parent, columns=("one", "two", "three"), show="headings", height=min(len(rows), 100))
        for column, heading, width, stretch in zip(("one", "two", "three"), headings, (90, 110, 240), (False, False, True)):
            table.heading(column, text=heading)
            table.column(column, width=width, minwidth=70, stretch=stretch)
        for row in rows:
            table.insert("", tk.END, values=row)
        table.pack(fill=tk.X, expand=True)
        table.bind("<Command-c>", self._copy_table_selection, add="+")
        table.bind("<Control-c>", self._copy_table_selection, add="+")
        self._bind_mouse_wheel(table)

    def _create_metadata_token_table(self, parent: ttk.Frame, path: object) -> None:
        """One row per resolvable Rename "Metadata Template" token, each
        with a button that inserts it into Rename mode's template field
        (switching the app to Rename mode first if needed) — the Inspector
        counterpart of the source script's per-field token buttons, so a
        value can be looked up and used without retyping it by hand."""
        if not isinstance(path, Path) or not path.is_file():
            self._create_selectable_text(parent, "No file loaded")
            return
        try:
            fields, trk_table = collect_metadata_template_fields(self.app.bwf_var.get(), path)
        except Exception as exc:
            self._create_selectable_text(parent, f"Unable to read metadata: {exc}")
            return
        rows: list[tuple[str, str]] = [(name, fields.get(name, "")) for name in METADATA_TEMPLATE_DISPLAY_FIELDS if fields.get(name)]
        rows.extend((f"trk{channel}", name) for channel, name in sorted(trk_table.items()))
        if not rows:
            self._create_selectable_text(parent, "No metadata available for template tokens")
            return
        table = ttk.Frame(parent)
        table.pack(fill=tk.X)
        for token_name, value in rows:
            row = tk.Frame(table, background=self.BACKGROUND)
            row.pack(fill=tk.X, pady=1)
            ttk.Button(row, text=f"${token_name}", width=16, command=lambda tk_text=f"${token_name}": self.app._insert_metadata_token(tk_text)).pack(side=tk.LEFT)
            tk.Label(row, text=value, background=self.BACKGROUND, foreground=self.TEXT_COLOR, font=("SF Mono", 11), anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        self._bind_mouse_wheel(table)

    def _copy_table_selection(self, event) -> str:
        table = event.widget
        selected = table.selection()
        if not selected:
            return "break"
        headings = [table.heading(column, "text") for column in table["columns"]]
        rows = [table.item(item, "values") for item in selected]
        copied = "\n".join(("\t".join(headings), *("\t".join(row) for row in rows)))
        self.window.clipboard_clear()
        self.window.clipboard_append(copied)
        return "break"

    def _bind_mouse_wheel(self, widget: tk.Misc) -> None:
        widget.bind("<MouseWheel>", self._on_mouse_wheel, add="+")
        for child in widget.winfo_children():
            self._bind_mouse_wheel(child)

    @staticmethod
    def _file_sections_for_tools(soxi: str, bwfmetaedit: str, wav_path: Path) -> tuple[list[tuple[str, str]], Optional[ET.Element]]:
        if not wav_path.is_file():
            return [("FILE", f"Target Not Found\n{wav_path}")], None
        # Structure comes first and on its own: it's read straight from the
        # bytes, so it still works on a file soxi/BWF MetaEdit refuse to
        # open — exactly the files it matters most for.
        structure = inspect_wav(wav_path)
        structure_text = format_structure_brief(structure)
        if structure.worst_severity == "error":
            structure_text += f"\n\nRight-click the file → Open in {REPAIR_APP_NAME}"
        try:
            core_xml, core_text, ixml_sections, ixml_root = inspector_metadata(bwfmetaedit, wav_path)
            audio = get_audio_info(soxi, wav_path, core_xml)
        except Exception as exc:
            file_text = f"File Name: {wav_path.name}\nFull Path: {wav_path}\nFile Size: {format_file_size(wav_path.stat().st_size)}"
            return [("FILE", file_text), ("STRUCTURE", structure_text), ("AUDIO", f"Unable to read: {exc}")], None
        file_text = "\n".join((
            f"File Name: {wav_path.name}",
            f"Full Path: {wav_path}",
            f"File Size: {format_file_size(wav_path.stat().st_size)}",
            f"Format: {audio['format']}",
        ))
        audio_text = "\n".join((
            f"Sample Rate: {audio['sample_rate']}",
            f"Bit Depth: {audio['bit_depth']}",
            f"Encoding: {audio['encoding']}",
            f"Channels: {audio['channels']}",
            f"Duration: {audio['duration']}",
            f"Total Samples: {audio['total_samples']}",
            f"Block Align: {audio['block_align']}",
            f"Data Start: {audio['data_start']}",
            f"Start Offset: {audio['start_offset']}",
        ))
        sections = [("FILE", file_text), ("STRUCTURE", structure_text), ("AUDIO", audio_text), ("CHANNEL CONFIGURATION", f"Channels: {audio['channels']}\nConfiguration: {audio['channel_configuration']}"), ("BEXT / CORE", core_text)] + ixml_sections + [("Metadata Template Tokens", "")]
        return sections, ixml_root

    @staticmethod
    def _source_list_sections_for_tools(soxi: str, sources: list[Path]) -> list[tuple[str, str]]:
        lines = []
        for index, source in enumerate(sources, start=1):
            if source.is_file():
                try:
                    audio = get_audio_info(soxi, source)
                    lines.append(f"{index}. {source.name}\n   Channels: {audio['channels']} | Sample Rate: {audio['sample_rate']} | Duration: {audio['duration']}")
                except Exception as exc:
                    lines.append(f"{index}. {source.name}\n   Unavailable: {exc}")
            else:
                lines.append(f"{index}. {source.name}\n   Source does not exist")
        return [("FILES", "\n\n".join(lines) or "No Sources")]

    @staticmethod
    def _planned_combine_sections_for_tools(soxi: str, bwfmetaedit: str, job: Job) -> tuple[list[tuple[str, str]], Optional[ET.Element]]:
        if job.target is None or not job.sources:
            raise RuntimeError("missing Combine target or sources")
        source_audio = [(source, get_audio_info(soxi, source)) for source in job.sources]
        reference_audio = source_audio[0][1]
        format_fields = ("sample_rate", "bit_depth", "encoding", "duration", "total_samples")
        consistent = all(all(audio[field] == reference_audio[field] for field in format_fields) for _source, audio in source_audio[1:])
        planned_file = "\n".join((
            f"Planned Filename: {job.target.name}",
            f"Target Path: {job.target.parent}",
            "Status: Will be created on Execute",
        ))
        if consistent:
            planned_audio = "\n".join((
                f"Channels: {len(job.sources)}",
                f"Sample Rate: {reference_audio['sample_rate']}",
                f"Bit Depth: {reference_audio['bit_depth']}",
                f"Encoding: {reference_audio['encoding']}",
                f"Duration: {reference_audio['duration']}",
                f"Total Samples: {reference_audio['total_samples']}",
            ))
        else:
            planned_audio = "INCONSISTENT SOURCE FORMAT\n" + "\n".join(
                f"{source.name}: " + ", ".join(f"{field.replace('_', ' ').title()}={audio[field]}" for field in format_fields)
                for source, audio in source_audio
            )
        try:
            core_xml = export_core_xml(bwfmetaedit, job.sources[0])
            core_root = ET.fromstring(core_xml)
            core_lines = element_lines(core_root.find(".//Core"))
            core_text = "\n".join(core_lines) if core_lines else "No BEXT / Core metadata"
        except Exception as exc:
            core_text = f"Unavailable: {exc}"
        try:
            ixml_sections, ixml_root = planned_combined_ixml_for_inspector(bwfmetaedit, job.sources)
        except Exception as exc:
            ixml_sections = [("iXML", f"Unavailable: {exc}")]
            ixml_root = None
        if job.combine_kind == "speaker":
            planned_configuration = "\n".join((
                "Speaker-aware Merge",
                f"Planned Stream Order: {' '.join(job.speaker_order)}",
            ))
        else:
            planned_configuration = "Generic Multichannel\nPlanned Stream Order: Source order"
        sections = [
            ("PLANNED FILE", planned_file),
            ("AUDIO", planned_audio),
            ("CHANNEL CONFIGURATION", planned_configuration),
            ("BEXT / CORE", core_text),
        ] + ixml_sections
        return sections, ixml_root

    def load_inspector_source(self, wav_path: Path) -> None:
        if not wav_path.is_file() or wav_path.suffix.lower() != ".wav":
            self.source_path_var.set("Select a WAV file.")
            return
        self.source_path = wav_path
        self.source_path_var.set(str(wav_path))
        self._queue_side_load("source", wav_path)

    def load_inspector_target(self, wav_path: Path) -> None:
        if not wav_path.is_file() or wav_path.suffix.lower() != ".wav":
            self.target_path_var.set("Select a WAV file.")
            return
        self.target_path = wav_path
        self.target_path_var.set(str(wav_path))
        self._queue_side_load("target", wav_path)

    def load_file(self, wav_path: Path) -> None:
        self.load_inspector_source(wav_path)

    def _queue_side_load(self, side: str, wav_path: Path) -> None:
        self._load_request_id += 1
        self._side_request_ids[side] += 1
        request_id = self._side_request_ids[side]
        soxi = self.app.soxi_var.get().strip()
        bwfmetaedit = self.app.bwf_var.get().strip()
        threading.Thread(target=self._prepare_side_worker, args=(side, request_id, wav_path, soxi, bwfmetaedit), daemon=True).start()

    def _prepare_side_worker(self, side: str, request_id: int, wav_path: Path, soxi: str, bwfmetaedit: str) -> None:
        try:
            sections, ixml_root = self._file_sections_for_tools(soxi, bwfmetaedit, wav_path)
        except Exception as exc:
            sections, ixml_root = [("FILE", f"Unable to inspect: {exc}")], None
        self._side_load_results.put((side, request_id, sections, ixml_root))

    def _apply_side_result(self, side: str, sections: list[tuple[str, str]], ixml_root: Optional[ET.Element]) -> None:
        if side == "source":
            self._left_sections = sections
            self._left_ixml = ixml_root
            self._left_header = "SOURCE"
        else:
            self._right_sections = sections
            self._right_ixml = ixml_root
            self._right_header = "TARGET"
        self._render_comparison(
            self._left_sections,
            self._right_sections,
            self._left_ixml,
            self._right_ixml,
            left_header=self._left_header,
            right_header=self._right_header,
            blank_missing_sections=self._blank_missing_sections,
            pair_first_sections=self._pair_first_sections,
        )

    def load_job(self, job: Job) -> None:
        self._side_request_ids["source"] += 1
        self._side_request_ids["target"] += 1
        self.source_path = job.source
        self.target_path = job.target or job.final_target
        self.source_path_var.set(str(job.source) if job.source is not None else "MISSING")
        self.target_path_var.set(str(self.target_path) if self.target_path is not None else "No Target")
        self._queue_load(InspectorContext.for_job(job))

    def load_qc_mapping(self, row: dict[str, object], reset_scroll: bool = False) -> None:
        self._side_request_ids["source"] += 1
        self._side_request_ids["target"] += 1
        poly_path = row.get("poly_path")
        split_path = row.get("split_path")
        self.source_path = poly_path if isinstance(poly_path, Path) else None
        self.target_path = split_path if isinstance(split_path, Path) else None
        self.source_path_var.set(str(self.source_path) if self.source_path is not None else "MISSING")
        self.target_path_var.set(str(self.target_path) if self.target_path is not None else "MISSING")
        self.window.title("Audio Inspector — QC Poly ↔ Split")
        self._queue_load(InspectorContext.for_qc(row, reset_scroll))

    def load_pair(self, source_path: Optional[Path], target_path: Optional[Path], reset_scroll: bool = False) -> None:
        """Load Source and Target together as one comparison, in a single
        render — used wherever both sides can change at once (e.g. selecting
        a QC result row). Calling `load_inspector_source()` then
        `load_inspector_target()` back to back instead triggers two
        independent background loads that each tear down and rebuild the
        whole canvas on arrival, which visibly double-flickers for a change
        that's really one update."""
        self.source_path = source_path
        self.target_path = target_path
        self.source_path_var.set(str(source_path) if source_path is not None else "MISSING")
        self.target_path_var.set(str(target_path) if target_path is not None else "MISSING")
        self._queue_load(InspectorContext.for_pair(source_path, target_path, reset_scroll))

    def _queue_load(self, context: InspectorContext) -> None:
        self._load_request_id += 1
        request_id = self._load_request_id
        soxi = self.app.soxi_var.get().strip()
        bwfmetaedit = self.app.bwf_var.get().strip()
        threading.Thread(target=self._prepare_comparison_worker, args=(request_id, context, soxi, bwfmetaedit), daemon=True).start()

    def _prepare_comparison_worker(self, request_id: int, context: InspectorContext, soxi: str, bwfmetaedit: str) -> None:
        try:
            if context.kind == "file":
                wav_path = context.wav_path
                if wav_path is None or not wav_path.is_file():
                    result = InspectorRenderModel([("FILE", "Selected file does not exist")], [("FILE", "No Target")], reset_scroll=context.reset_scroll, path_label="Selected file does not exist")
                else:
                    left_sections, left_ixml = self._file_sections_for_tools(soxi, bwfmetaedit, wav_path)
                    result = InspectorRenderModel(left_sections, [("FILE", "No Target")], left_ixml=left_ixml, reset_scroll=context.reset_scroll, path_label=f"{wav_path.name}  |  {wav_path}")
            elif context.kind == "pair":
                source_path = context.wav_path
                target_path = context.target_path
                if isinstance(source_path, Path):
                    try:
                        left_sections, left_ixml = self._file_sections_for_tools(soxi, bwfmetaedit, source_path)
                    except Exception as exc:
                        left_sections, left_ixml = [("FILE", f"Unable to inspect: {exc}")], None
                else:
                    left_sections, left_ixml = [("FILE", "MISSING")], None
                if isinstance(target_path, Path):
                    try:
                        right_sections, right_ixml = self._file_sections_for_tools(soxi, bwfmetaedit, target_path)
                    except Exception as exc:
                        right_sections, right_ixml = [("FILE", f"Unable to inspect: {exc}")], None
                else:
                    right_sections, right_ixml = [("FILE", "MISSING")], None
                result = InspectorRenderModel(left_sections, right_sections, left_ixml, right_ixml, context.reset_scroll, path_label="QC — Source ↔ Target")
            elif context.kind == "qc":
                value = context.qc_row
                if value is None:
                    return
                poly_path = value.get("poly_path")
                split_path = value.get("split_path")
                comparison_text = "\n".join((
                    f"Poly File: {value.get('poly_file') if isinstance(poly_path, Path) else 'MISSING'}",
                    f"Split File: {value.get('split_file') if isinstance(split_path, Path) else 'MISSING'}",
                    f"Stream: {value.get('stream', '—')}",
                    f"Recorder Ch: {value.get('channel', '—')}",
                    f"Frames: {value.get('frames', '—')}",
                    f"PCM RESULT: {value.get('result', '—')}",
                ))
                if isinstance(poly_path, Path):
                    try:
                        left_sections, left_ixml = self._file_sections_for_tools(soxi, bwfmetaedit, poly_path)
                    except Exception as exc:
                        left_sections, left_ixml = [("FILE", f"Unable to inspect: {exc}")], None
                else:
                    left_sections, left_ixml = [("FILE", "MISSING")], None
                if isinstance(split_path, Path):
                    try:
                        right_sections, right_ixml = self._file_sections_for_tools(soxi, bwfmetaedit, split_path)
                    except Exception as exc:
                        right_sections, right_ixml = [("FILE", f"Unable to inspect: {exc}")], None
                else:
                    right_sections, right_ixml = [("FILE", "MISSING")], None
                left_sections.insert(0, ("QC COMPARISON", comparison_text))
                right_sections.insert(0, ("QC COMPARISON", comparison_text))
                result = InspectorRenderModel(left_sections, right_sections, left_ixml, right_ixml, context.reset_scroll, path_label="QC — Poly ↔ Split")
            else:
                job = context.job
                if job is None:
                    return
                if job.mode in ("combine", "merge"):
                    right_sections, right_ixml = self._planned_combine_sections_for_tools(soxi, bwfmetaedit, job)
                    result = InspectorRenderModel(self._source_list_sections_for_tools(soxi, job.sources), right_sections, right_ixml=right_ixml, left_header="SOURCES", right_header="PLANNED TARGET", path_label=f"Combine job: {job.target or '-'}", blank_missing_sections=True, pair_first_sections=True)
                else:
                    if job.source is None:
                        left_sections, left_ixml = [("FILE", "MISSING")], None
                    else:
                        left_sections, left_ixml = self._file_sections_for_tools(soxi, bwfmetaedit, job.source)
                    if job.mode in ("rename", "rename_copy", "browse"):
                        right_sections, right_ixml = [("FILE", "No Target")], None
                    else:
                        target = job.target or job.final_target
                        right_sections, right_ixml = self._file_sections_for_tools(soxi, bwfmetaedit, target) if target else ([("FILE", "Target Not Found")], None)
                    result = InspectorRenderModel(left_sections, right_sections, left_ixml, right_ixml, path_label=f"Source: {job.source or 'MISSING'}")
        except Exception as exc:
            result = InspectorRenderModel([("FILE", f"Unable to inspect: {exc}")], [("FILE", "No Target")], reset_scroll=context.reset_scroll, path_label="Inspector load failed")
        self._load_results.put((request_id, result))

    def _apply_prepared_comparison(self, model: InspectorRenderModel) -> None:
        self._left_sections = model.left_sections
        self._right_sections = model.right_sections
        self._left_ixml = model.left_ixml
        self._right_ixml = model.right_ixml
        self._left_header = model.left_header
        self._right_header = model.right_header
        self._blank_missing_sections = model.blank_missing_sections
        self._pair_first_sections = model.pair_first_sections
        self._render_comparison(model.left_sections, model.right_sections, model.left_ixml, model.right_ixml, model.reset_scroll, model.left_header, model.right_header, model.blank_missing_sections, model.pair_first_sections)

def split_conflict_status(target: Path, conflict_policy: str) -> str:
    if not target.exists():
        return "READY"
    if conflict_policy == "Replace existing":
        return "READY REPLACE"
    if conflict_policy == "Skip existing":
        return "SKIP EXISTS"
    return "CONFLICT: target exists"


def display_status(
    job: Job,
    name_mode: str,
    rename_number_mode: str,
    rename_apply_formula: bool = True,
    rename_change_number: bool = True,
    rename_find_replace_enabled: bool = False,
    rename_serial_enabled: bool = False,
    rename_metadata_template_label: str = "",
) -> str:
    if job.mode == "merge" and job.status.startswith("READY"):
        return f"{job.status} MERGE ({speaker_layout_name(job.speaker_order)} {' '.join(job.speaker_order)})"
    if job.mode == "combine" and job.status.startswith("READY"):
        return f"{job.status} GENERIC"
    if job.mode in ("combine", "merge", "combine_copy"):
        return job.status
    if job.mode == "split_copy":
        # A straight file copy, not a numbered channel — "READY (Trk#)"
        # would be misleading, so this shows its raw status exactly like
        # Combine's own "copy non-combined" jobs already do.
        return job.status
    if job.mode == "reembed" and job.status == "READY":
        if job.final_target is not None:
            return f"Ready to Embed and Rename ({rename_number_mode})"
        return "Ready to Embed"
    if job.mode in ("rename", "rename_copy") and job.status in ("READY", "UNCHANGED"):
        actions: list[str] = []
        if rename_metadata_template_label:
            actions.append(rename_metadata_template_label)
        if rename_apply_formula:
            actions.append("Add Text")
        if rename_find_replace_enabled:
            actions.append("Find & Replace")
        change_number_inapplicable = rename_change_number and not job.has_channel_suffix
        if rename_change_number and job.has_channel_suffix:
            actions.append(f"Change to {rename_number_mode}")
        if rename_serial_enabled:
            actions.append("Add ID")
        if job.status == "UNCHANGED":
            if change_number_inapplicable and not actions:
                return "No change: no channel number to change (Poly / no split suffix)"
            return job.status
        if not actions:
            return "No rename action selected"
        if len(actions) == 1:
            return f"Ready to {actions[0]}"
        if len(actions) == 2:
            return f"Ready to {actions[0]} and {actions[1]}"
        return f"Ready to {', '.join(actions[:-1])}, and {actions[-1]}"
    if job.status == "READY":
        return f"READY ({name_mode})"
    if job.status == "READY REPLACE":
        return f"READY REPLACE ({name_mode})"
    return job.status


# The two exact messages export_ixml() raises when bwfmetaedit ran fine
# but the source simply has no iXML of its own — as opposed to any other
# message, which reflects a real problem (a corrupt/truncated WAV, a tool
# crash, ...) that Split should still block on rather than paper over.
_NO_IXML_MESSAGES = ("iXML sidecar was not exported", "source iXML is empty")


def build_split_jobs(
    source_root: Path,
    output_root: Path,
    bwfmetaedit: str,
    soxi: str,
    name_mode: str,
    output_formula: str,
    conflict_policy: str,
    progress=None,
    only_files: Optional[list[Path]] = None,
    copy_mono: bool = False,
) -> list[Job]:
    jobs: list[Job] = []
    sources = [source for source in (only_files if only_files is not None else wav_files(source_root)) if not is_split_wav(source)]
    total = len(sources)
    for index, source in enumerate(sources, start=1):
        if is_split_wav(source):
            continue
        # Checked first, straight from the bytes: sox and BWF MetaEdit both
        # read many damaged files without complaint (a never-finalized data
        # size, a missing pad byte), so their success says nothing about
        # whether the file is sound. Any structural warning/error blocks the
        # file so it goes through Wave Repair first.
        structure_block = structure_block_status(source)
        if structure_block is not None:
            jobs.append(Job(source, None, 0, None, None, structure_block, "split"))
            if progress is not None:
                progress(index, total, source, 0, structure_block)
            continue
        rel_parent = source.relative_to(source_root).parent
        try:
            channels = soxi_channels(soxi, source)
        except Exception as exc:
            jobs.append(Job(source, None, 0, None, None, f"BLOCKED: {exc}{structural_damage_hint([source])}", "split"))
            if progress is not None:
                progress(index, total, source, 0, str(exc))
            continue

        if channels == 1 and copy_mono:
            # Already mono — nothing to split. A straight file copy
            # (matching Combine's own "copy non-combined" behavior)
            # carries over whatever metadata the source has, complete,
            # partial, or none, without needing any of it read here.
            target = output_root / source.relative_to(source_root)
            status = split_conflict_status(target, conflict_policy)
            if status == "READY":
                status = "READY COPY"
            jobs.append(Job(source, target, 1, None, None, status, "split_copy", rel_target=source.relative_to(source_root)))
            if progress is not None:
                progress(index, total, source, 1, "OK")
            continue

        try:
            ixml = export_ixml(bwfmetaedit, source)
            _, tracks = parse_tracks(ixml)
        except Exception as exc:
            if str(exc) in _NO_IXML_MESSAGES:
                # No iXML at all doesn't block the whole file — every slot
                # just has no real Trk# to read, falling back to its own
                # file-order position a few lines down (same as a slot
                # whose TRACK is merely missing CHANNEL_INDEX).
                tracks = []
            else:
                jobs.append(Job(source, None, 0, None, None, f"BLOCKED: {exc}{structural_damage_hint([source])}", "split"))
                if progress is not None:
                    progress(index, total, source, 0, str(exc))
                continue
        made = 0
        for slot in range(1, channels + 1):
            trk_number = text_int(tracks[slot - 1], "CHANNEL_INDEX") if slot <= len(tracks) else None
            if trk_number is None:
                trk_number = slot
            output_number = trk_number if name_mode == "Trk#" else slot
            target_name = render_output_name(output_formula, source.stem, output_number, source.suffix)
            rel_target = rel_parent / target_name
            target = output_root / rel_target
            status = split_conflict_status(target, conflict_policy)
            jobs.append(Job(source, target, slot, trk_number, slot, status, "split", rel_target=rel_target))
            made += 1
        if progress is not None:
            progress(index, total, source, made, "OK")
    return jobs


# Channel suffix at the end of a mono source's name: ".A3", "_A3", "-A3"
# (lettered) or ".3", "_3", "-3" (digits only).
REEMBED_CHANNEL_SUFFIX = re.compile(r"^(?P<base>.+?)(?P<sep>[._-])(?P<letter>[Aa]?)(?P<number>[1-9]\d*)$")
# What may follow a source's name inside a Target's name: nothing, or a
# separator (" render 001", ".A3 render 001", "_AAP.A3", "-AtAlgnP_01" ...).
REEMBED_NAME_DELIMITERS = " ._-"


def reembed_source_bases(stem: str) -> list[str]:
    """Every name a Target may start with to belong to this mono Source:
    its full name, and the name without a channel suffix (".A3", "_3" ...).
    Longest first."""
    bases = [stem]
    match = REEMBED_CHANNEL_SUFFIX.match(stem)
    if match:
        bases.append(match["base"])
    return bases


def reembed_naming_base(stem: str, recorder_channel: Optional[int]) -> tuple[str, Optional[int]]:
    """{base} for the rename formula, and the channel number read from the
    name (None if it has none). A lettered suffix (".A3", "_A3", "-A3") is
    always a channel number. A digits-only one (".3", "_3", "-3") only when
    it equals the file's own recorder channel, so a name like "SC12-3"
    (take 3, not channel 3) keeps its digits."""
    match = REEMBED_CHANNEL_SUFFIX.match(stem)
    if not match:
        return stem, None
    number = int(match["number"])
    if match["letter"] or (recorder_channel is not None and number == recorder_channel):
        return match["base"], number
    return stem, None


def reembed_target_group(target_stem: str, bases: set[str]) -> Optional[str]:
    """The longest known Source base (casefolded) that the Target's name
    starts with, followed by a separator or the end of the name — so
    "12-1T10 render 001" never falls into "12-1T1"."""
    name = target_stem.casefold()
    for length in range(len(name), 0, -1):
        prefix = name[:length]
        if prefix in bases and (length == len(name) or name[length] in REEMBED_NAME_DELIMITERS):
            return prefix
    return None


def build_reembed_jobs(
    source_root: Path,
    target_root: Path,
    rename_formula: str = "",
    rename_number_mode: str = "Stream#",
    bwfmetaedit: str = "",
    progress=None,
    conflict_policy: str = "Skip existing",
) -> list[Job]:
    """Pair every WAV under Target with its Source by name and metadata — no
    Source / Target formula. A Target belongs to the Source whose name its
    own name starts with (longest match, followed by a separator or the end:
    "12-5T2.A3 render 001" -> "12-5T2.A3", "12-10T2 render 001" -> the mono
    take "12-10T2"); a mono Source also claims Targets that start with its
    name minus the channel suffix ("Scene.A5" <- "Scene.A2 render 1" when
    the render renumbered the channels). Within that group the real
    recorder channel (iXML CHANNEL_INDEX) decides, as before."""
    def report(index: int, total: int, item: Path, trk: int, status: str) -> None:
        if progress is not None and (index == 1 or index == total or index % 100 == 0):
            progress(index, total, item, trk, status)

    # --- Sources: every WAV. Multi-channel files are Poly sources. ---------
    source_files = wav_files(source_root)
    mono_sources: list[Path] = []
    poly_candidates: list[Path] = []
    for index, source in enumerate(source_files, start=1):
        try:
            channels = read_wav_pcm_layout(source)["channels"]
        except Exception:
            channels = 1  # unreadable header: treated as mono, the Trk# read will report it
        (poly_candidates if channels > 1 else mono_sources).append(source)
        report(index, len(source_files), source, 0, "Scanning sources")

    poly_items: list[tuple[Path, int, int]] = []  # (poly_path, stream_index, recorder_channel)
    poly_errors: dict[Path, Exception] = {}
    for index, source in enumerate(poly_candidates, start=1):
        try:
            mapping = poly_track_mapping(bwfmetaedit, source)
        except Exception as exc:
            poly_errors[source] = exc
            continue
        poly_items.extend((source, stream_index, recorder_channel) for stream_index, recorder_channel in mapping)
        report(index, len(poly_candidates), source, 0, "Reading Poly track list")

    # Group keys (casefolded names a Target may start with) -> sources
    sources_by_key: dict[str, list[Path]] = {}
    keys_by_source: dict[Path, list[str]] = {}
    for source in mono_sources:
        keys = [base.casefold() for base in reembed_source_bases(source.stem)]
        keys_by_source[source] = keys
        for key in keys:
            sources_by_key.setdefault(key, []).append(source)
    for source in list(poly_errors) + sorted({item[0] for item in poly_items}, key=natural_path_key):
        keys_by_source[source] = [source.stem.casefold()]
        sources_by_key.setdefault(source.stem.casefold(), []).append(source)
    all_keys = set(sources_by_key)

    # --- Targets: every WAV, grouped by the longest Source name prefix. ----
    # A file already named the way the rename formula names outputs (left by
    # an earlier run, e.g. "12-5T2_AAP.A3.wav") is an output, not an input:
    # it only matters as a rename conflict below.
    naming_keys: set[str] = set()
    for source in mono_sources:
        naming_keys.add(reembed_naming_base(source.stem, None)[0].casefold())
        naming_keys.add(source.stem.casefold())
    output_patterns: list[re.Pattern[str]] = []
    if rename_formula.strip():
        try:
            output_patterns.append(formula_pattern(rename_formula))
        except ValueError:
            pass
        # ...and the name a mono original without a channel number gets ("15_WILDT01_AAP")
        unnumbered = formula_without_number(rename_formula)
        if unnumbered.count("{base}") == 1:
            output_patterns.append(re.compile("^" + re.escape(unnumbered).replace(re.escape("{base}"), "(?P<base>.+)") + r"\.wav$", re.IGNORECASE))
    target_files = []
    for target in wav_files(target_root):
        if any(
            (output_match := pattern.match(target.name)) and output_match["base"].casefold() in (naming_keys | all_keys)
            for pattern in output_patterns
        ):
            continue
        target_files.append(target)
    targets_by_key: dict[str, list[Path]] = {}
    ungrouped_targets: list[Path] = []
    for index, target in enumerate(target_files, start=1):
        key = reembed_target_group(target.stem, all_keys)
        if key is None:
            ungrouped_targets.append(target)
        else:
            targets_by_key.setdefault(key, []).append(target)
        report(index, len(target_files), target, 0, "Scanning targets")

    # Recorder channel of every Target (shown on NO SOURCE rows too)
    target_channel: dict[Path, int] = {}
    target_errors: dict[Path, Exception] = {}
    for index, target in enumerate(target_files, start=1):
        try:
            target_channel[target] = split_recorder_channel(bwfmetaedit, target)
        except Exception as exc:
            target_errors[target] = exc
        report(index, len(target_files), target, target_channel.get(target, 0), "Reading target metadata")

    # Recorder channel of the mono Sources that have Targets in their group
    # (the others are NO TARGET; read later only if the metadata fallback needs it)
    def starts_with_name(target: Path, key: str) -> bool:
        name = target.stem.casefold()
        return name.startswith(key) and (len(name) == len(key) or name[len(key)] in REEMBED_NAME_DELIMITERS)

    def grouped_targets(source: Path) -> list[Path]:
        """Every Target whose name starts with this Source's name or its
        name without the channel suffix — also ones grouped under another
        Source's exact name, since a render may renumber channels."""
        if source not in grouped_cache:
            keys = keys_by_source.get(source, [])
            grouped_cache[source] = [target for target in target_files if any(starts_with_name(target, key) for key in keys)]
        return grouped_cache[source]

    grouped_cache: dict[Path, list[Path]] = {}

    trk_numbers: dict[Path, int] = {}
    trk_errors: dict[Path, Exception] = {}
    mono_with_targets = [source for source in mono_sources if grouped_targets(source)]
    for index, source in enumerate(mono_with_targets, start=1):
        try:
            trk_numbers[source] = split_recorder_channel(bwfmetaedit, source)
        except Exception as exc:
            trk_errors[source] = exc
        report(index, len(mono_with_targets), source, trk_numbers.get(source, 0), "Reading source metadata")

    # --- Pair: same group, same recorder channel. --------------------------
    jobs: list[Job] = []
    claimed: dict[Path, list[Job]] = {}

    def pair(source: Path, channel_index: int, recorder_channel: Optional[int], candidates_from: list[Path]) -> Job:
        if recorder_channel is None:
            return Job(source, None, channel_index, None, None, "NO TARGET", "reembed")
        exact_key = source.stem.casefold()
        exact = [t for t in targets_by_key.get(exact_key, []) if target_channel.get(t) == recorder_channel]
        candidates = exact or [t for t in candidates_from if target_channel.get(t) == recorder_channel]
        if len(candidates) == 1:
            job = Job(source, candidates[0], channel_index, recorder_channel, None, "READY", "reembed")
            claimed.setdefault(candidates[0], []).append(job)
            return job
        if not candidates:
            unreadable = [t for t in candidates_from if t in target_errors]
            if unreadable:
                return Job(source, None, channel_index, recorder_channel, None, f"BLOCKED: cannot read target Trk# ({target_errors[unreadable[0]]})", "reembed")
            return Job(source, None, channel_index, recorder_channel, None, "NO TARGET", "reembed")
        return Job(source, None, channel_index, recorder_channel, None, "AMBIGUOUS TARGET", "reembed")

    total = len(mono_sources)
    for index, source in enumerate(mono_sources, start=1):
        if source in trk_errors:
            job = Job(source, None, 1, None, None, f"BLOCKED: cannot read source Trk# ({trk_errors[source]}){structural_damage_hint([source])}", "reembed")
        else:
            job = pair(source, 1, trk_numbers.get(source), grouped_targets(source))
        jobs.append(job)
        report(index, total, source, job.trk_number or 0, job.status)

    # Poly sources: each stream is matched on its own recorder channel;
    # `channel` is the stream index execute() needs to pull that TRACK out
    # of the Poly file's iXML.
    for index, (source, stream_index, recorder_channel) in enumerate(poly_items, start=1):
        job = pair(source, stream_index, recorder_channel, targets_by_key.get(source.stem.casefold(), []))
        jobs.append(job)
        report(index, len(poly_items), source, recorder_channel, job.status)
    for poly_source, exc in poly_errors.items():
        jobs.append(Job(poly_source, None, 0, None, None, f"BLOCKED: cannot read Poly Track List ({exc}){structural_damage_hint([poly_source])}", "reembed"))

    # One Target claimed by two Sources (e.g. "Scene_3" and "Scene_4" both
    # reaching a "Scene ..." render) is never guessed at.
    for target, claim_jobs in claimed.items():
        if len(claim_jobs) > 1:
            for job in claim_jobs:
                job.target = None
                job.status = "AMBIGUOUS SOURCE"

    # Naming base / channel number from the name, for rename and Stream#
    base_by_source: dict[Path, str] = {}
    matched_number_by_source: dict[Path, int] = {}
    unnumbered_sources: set[Path] = set()  # mono originals named without a channel number
    for source in mono_sources:
        base, number = reembed_naming_base(source.stem, trk_numbers.get(source))
        base_by_source[source] = base
        matched_number_by_source[source] = number if number is not None else 1
        if number is None:
            unnumbered_sources.add(source)
    for source in keys_by_source:
        if source not in base_by_source:
            base_by_source[source] = source.stem
    source_base_by_path: dict[Path, str] = {source: base.casefold() for source, base in base_by_source.items()}

    # Targets no Source claimed: NO SOURCE (or BLOCKED when unreadable)
    used_targets = {job.target for job in jobs if job.target is not None}
    ambiguous_targets = {target for target, claim_jobs in claimed.items() if len(claim_jobs) > 1}
    target_only_jobs_by_base: dict[str, list[Job]] = {}
    blocked_source_groups = {key for job in jobs if job.status.startswith("BLOCKED: cannot read target") for key in keys_by_source.get(job.source, [])}
    for target in target_files:
        if target in used_targets or target in ambiguous_targets:
            continue
        group = reembed_target_group(target.stem, all_keys) or target.stem.casefold()
        if target in target_errors:
            if group in blocked_source_groups:
                continue  # already reported on its Source's row
            target_only_jobs_by_base.setdefault(group, []).append(Job(None, target, 0, None, None, f"BLOCKED: cannot read target Trk# ({target_errors[target]})", "reembed"))
        else:
            target_only_jobs_by_base.setdefault(group, []).append(Job(None, target, 0, target_channel.get(target), None, "NO SOURCE", "reembed"))

    # The metadata fallback below needs the recorder channel of NO TARGET
    # mono Sources that were never read (nothing in their group).
    if any(job.status == "NO SOURCE" for base_jobs in target_only_jobs_by_base.values() for job in base_jobs):
        for job in jobs:
            if job.status == "NO TARGET" and job.trk_number is None and job.source is not None and job.source not in trk_numbers:
                try:
                    job.trk_number = split_recorder_channel(bwfmetaedit, job.source)
                    trk_numbers[job.source] = job.trk_number
                except Exception:
                    pass

    # Last-resort pairing: a DAW consolidate/render can rewrite the digits
    # inside a Target's own base name (Pro Tools padding a take number, for
    # instance), which the exact-string {base} match above can never
    # recover from — but Tape/Scene/Take/OriginationDate/OriginationTime
    # survive that untouched (verified against real Pro Tools and REAPER
    # renders), so a Source stuck at NO TARGET gets one more chance against
    # whatever Targets are still unclaimed (NO SOURCE), using that identity
    # tuple plus the real Recorder Channel instead of the filename. A tie
    # (2+ Targets sharing the same tuple + channel) is reported as
    # AMBIGUOUS rather than guessed at.
    no_target_jobs = [job for job in jobs if job.status == "NO TARGET" and job.source is not None and job.trk_number is not None]
    if no_target_jobs:
        orphan_target_jobs = [
            job
            for base_jobs in target_only_jobs_by_base.values()
            for job in base_jobs
            if job.status == "NO SOURCE" and job.target is not None
        ]
        # This reads two bwfmetaedit-backed fields per orphan Target and per
        # NO TARGET Source (read_reembed_identity), with nothing marking the
        # wait before this fix — every earlier phase (scanning, reading Trk#,
        # classifying) reports progress, so a batch heavy on unmatched
        # channels would look finished (progress bar and ETA both at 100%)
        # while this pass silently kept running for as long as the rest of
        # the scan, sometimes longer.
        orphan_total = len(orphan_target_jobs) + len(no_target_jobs)
        orphan_completed = 0

        def report_orphan_progress(item: Path) -> None:
            nonlocal orphan_completed
            orphan_completed += 1
            if progress is not None and (orphan_completed == 1 or orphan_completed == orphan_total or orphan_completed % 50 == 0):
                progress(orphan_completed, orphan_total, item, 0, "Resolving orphan targets")

        orphan_identities: dict[Path, Optional[tuple[str, str, str, str, str]]] = {}
        for orphan_job in orphan_target_jobs:
            try:
                orphan_identities[orphan_job.target] = read_reembed_identity(bwfmetaedit, orphan_job.target)
            except Exception:
                orphan_identities[orphan_job.target] = None
            report_orphan_progress(orphan_job.target)
        for job in no_target_jobs:
            try:
                source_identity = read_reembed_identity(bwfmetaedit, job.source)
            except Exception:
                source_identity = None
            report_orphan_progress(job.source)
            if source_identity is None:
                continue
            matches = [
                orphan_job
                for orphan_job in orphan_target_jobs
                if orphan_job.trk_number == job.trk_number and orphan_identities.get(orphan_job.target) == source_identity
            ]
            if len(matches) == 1:
                matched_job = matches[0]
                job.target = matched_job.target
                job.status = "READY"
                orphan_target_jobs.remove(matched_job)
                for base_jobs in target_only_jobs_by_base.values():
                    if matched_job in base_jobs:
                        base_jobs.remove(matched_job)
                        break
            elif len(matches) > 1:
                job.status = "AMBIGUOUS TARGET (metadata match)"

    for target_only_jobs in target_only_jobs_by_base.values():
        jobs.extend(target_only_jobs)

    # Now that every job's final READY/not-READY status is settled, assign
    # Stream# per base: only the READY jobs, in recorder-channel order,
    # numbered 1..N with no gaps for whatever didn't make it through. A
    # not-READY job keeps its original channel number just so the table/TSV
    # has something sensible to show — it's never actually used for
    # renaming since nothing not-READY gets executed.
    ready_by_base: dict[str, list[Job]] = {}
    for job in jobs:
        if job.status == "READY" and job.source is not None:
            ready_by_base.setdefault(source_base_by_path[job.source], []).append(job)
    for ready_jobs in ready_by_base.values():
        ready_jobs.sort(key=lambda job: (job.trk_number if job.trk_number is not None else matched_number_by_source[job.source], job.source.name.casefold()))
        for stream_number, job in enumerate(ready_jobs, start=1):
            job.stream_number = stream_number
    for job in jobs:
        if job.stream_number is None and job.source is not None:
            job.stream_number = matched_number_by_source.get(job.source)

    # Compute (or recompute) the rename-after-embed target now that Stream#
    # is final — covers both the directly-matched READY jobs and the ones
    # that only became READY via the metadata-identity pairing just above,
    # which used to skip this conflict check entirely.
    if rename_formula.strip():
        for job in jobs:
            if job.status == "READY" and job.source is not None and job.target is not None:
                rename_number = job.trk_number if rename_number_mode == "Trk#" else job.stream_number
                if job.source in unnumbered_sources:
                    # A name without a channel number keeps none: "15_WILDT01" -> "15_WILDT01_AAP"
                    final_name = render_output_name_without_number(rename_formula, base_by_source[job.source], job.target.suffix)
                else:
                    final_name = render_output_name(rename_formula, base_by_source[job.source], rename_number, job.target.suffix)
                final_target = job.target.with_name(final_name)
                job.final_target = final_target
                if final_target != job.target and final_target.exists():
                    job.status = split_conflict_status(final_target, conflict_policy)

    for job in jobs:
        if job.source is not None:
            job.naming_base = base_by_source.get(job.source, job.source.stem)
            job.has_channel_suffix = job.source not in unnumbered_sources
    return sorted(jobs, key=lambda job: (job.target is None, natural_path_key(job.target or job.source)))


def build_rename_jobs(
    root: Path,
    source_formula: str,
    output_formula: str,
    name_mode: str,
    bwfmetaedit: str,
    output_root: Optional[Path] = None,
    apply_formula: bool = True,
    change_number: bool = True,
    progress=None,
    rename_rules: Optional[list[RenameRule]] = None,
    serial_id_map: Optional[dict[str, str]] = None,
    serial_id_enabled: bool = False,
    serial_prefix: str = "AB",
    serial_digits: int = 4,
    serial_start: int = 1234,
    name_affix: str = "",
    name_affix_position: str = "Append",
    serial_order_mode: str = "Folder Path + Filename",
    serial_metadata_reader=None,
    metadata_template: str = "",
    metadata_template_fields_reader=None,
    only_files: Optional[list[Path]] = None,
) -> list[Job]:
    source_pattern = rename_auto_input_pattern() if source_formula.casefold() == DEFAULT_SOURCE_FORMULA.casefold() else rename_input_pattern(source_formula)
    matched: list[tuple[Path, str, int, str]] = []
    for source in (only_files if only_files is not None else wav_files(root)):
        match = source_pattern.match(source.name)
        if match:
            matched.append((source, match["base"], rename_number_from_match(match), match.groupdict().get("channel", "")))

    trk_numbers: dict[Path, int] = {}
    total = len(matched)
    for index, (source, _base, matched_number, _channel) in enumerate(matched, start=1):
        if not matched_number:
            trk_numbers[source] = 0
            if progress is not None:
                progress(index, total, source, 0, "POLY")
            continue
        try:
            trk_numbers[source] = read_mono_trk_number(bwfmetaedit, source, matched_number or index)
        except Exception:
            trk_numbers[source] = matched_number
        if progress is not None:
            progress(index, total, source, trk_numbers[source], "OK")

    stream_numbers: dict[Path, int] = {}
    groups: dict[tuple[Path, str], list[Path]] = {}
    for source, base, _matched_number, _channel in matched:
        groups.setdefault((source.parent, base.casefold()), []).append(source)
    for group in groups.values():
        for stream_number, source in enumerate(sorted(group, key=lambda item: (trk_numbers.get(item, 0), item.name.casefold())), start=1):
            stream_numbers[source] = stream_number

    metadata_by_path: dict[Path, tuple[str, int, str, str, str]] = {}
    blocked_serial_groups: dict[str, str] = {}
    if serial_id_enabled and serial_order_mode == "Metadata Tape + TC":
        # A bwfmetaedit read per file with no progress reporting at all
        # until this fix — exactly the "may take a while for large folders"
        # case the confirmation dialog for this feature already warns
        # about before it even starts.
        reader = serial_metadata_reader or (lambda path: read_serial_metadata(bwfmetaedit, path))
        metadata_rows: list[SerialMetadata] = []
        metadata_total = len(matched)
        for metadata_index, (source, base, _matched_number, _channel) in enumerate(matched, start=1):
            group_key = f"{str(source.parent).casefold()}::{base.casefold()}"
            try:
                tape, time_reference, scene, take, tc_display = reader(source)
            except Exception:
                tape, time_reference, scene, take, tc_display = "", -1, "", "", ""
            metadata_by_path[source] = (tape, time_reference, scene, take, tc_display)
            metadata_rows.append(SerialMetadata(group_key, tape, time_reference))
            if progress is not None and (metadata_index == 1 or metadata_index == metadata_total or metadata_index % 100 == 0):
                progress(metadata_index, metadata_total, source, 0, "Reading Tape + TC metadata")
        ordered_keys, blocked_serial_groups = strict_metadata_group_order(metadata_rows)
        assigned_ids, _next_serial = assign_serial_ids_in_order(ordered_keys, serial_id_map or {}, serial_prefix, serial_digits, serial_start)
        if serial_id_map is not None:
            serial_id_map.clear()
            serial_id_map.update(assigned_ids)
        serial_id_map = assigned_ids
    elif serial_id_enabled:
        group_keys = [f"{str(source.parent).casefold()}::{base.casefold()}" for source, base, _matched_number, _channel in matched]
        assigned_ids, _next_serial = assign_serial_ids(group_keys, serial_id_map or {}, serial_prefix, serial_digits, serial_start)
        if serial_id_map is not None:
            serial_id_map.clear()
            serial_id_map.update(assigned_ids)
        serial_id_map = assigned_ids

    metadata_template_by_path: dict[Path, tuple[dict[str, str], dict[int, str]]] = {}
    if metadata_template.strip():
        # Same silent-read risk as the Tape + TC pass above — this is the
        # other feature whose own confirmation dialog already warns it may
        # take a while for large folders.
        fields_reader = metadata_template_fields_reader or (lambda path: collect_metadata_template_fields(bwfmetaedit, path))
        template_total = len(matched)
        for template_index, (source, _base, _matched_number, _channel) in enumerate(matched, start=1):
            try:
                metadata_template_by_path[source] = fields_reader(source)
            except Exception:
                metadata_template_by_path[source] = ({}, {})
            if progress is not None and (template_index == 1 or template_index == template_total or template_index % 100 == 0):
                progress(template_index, template_total, source, 0, "Reading Metadata Template fields")

    jobs: list[Job] = []
    planned_targets: dict[Path, int] = {}
    for index, (source, base, matched_number, channel) in enumerate(matched, start=1):
        trk_number = trk_numbers[source]
        stream_number = stream_numbers[source]
        has_numbered_suffix = bool(matched_number or channel)
        output_number = ((trk_number if name_mode == "Trk#" else stream_number) if change_number else matched_number) if has_numbered_suffix else 0
        template_fields, template_trk_table = metadata_template_by_path.get(source, ({}, {}))
        renamed_base = base
        metadata_template_active = False
        if metadata_template.strip() and template_fields:
            expanded_base = expand_metadata_template(metadata_template, template_fields, template_trk_table, counter=index)
            renamed_base = expanded_base or base
            metadata_template_active = bool(expanded_base)
        group_key = f"{str(source.parent).casefold()}::{base.casefold()}"
        serial_id = (serial_id_map or {}).get(group_key, "") if serial_id_enabled else ""
        formula = output_formula if apply_formula else DEFAULT_OUTPUT_FORMULA
        target_parent = (output_root / source.relative_to(root).parent) if output_root else source.parent
        try:
            if metadata_template_active:
                # The template defines the whole stem itself — no {base}.A$
                # channel-number suffix gets appended (the user adds
                # $trk/$chnum on purpose if they want one kept); reusing
                # render_serial_name with a bare "{base}" formula still
                # gets Serial ID's own prepend/append for free, since that's
                # unrelated to which naming source built the base.
                target_name = render_serial_name("{base}", renamed_base, 0, source.suffix, serial_id, "")
            else:
                target_name = render_serial_name(formula, renamed_base, output_number, source.suffix, serial_id, channel or "")
            target_stem = Path(target_name).stem
            target_stem = apply_name_affix(target_stem, name_affix, name_affix_position)
            target_name = apply_rename_rules(target_stem, rename_rules or []) + Path(target_name).suffix
            status = "UNCHANGED" if target_name == source.name and target_parent == source.parent else "READY"
            if group_key in blocked_serial_groups:
                status = blocked_serial_groups[group_key]
        except ValueError as exc:
            target_name = source.name
            status = f"BLOCKED: {exc}"
        target = target_parent / target_name
        if status == "READY" and target == source:
            status = "UNCHANGED"
        mode = "rename_copy" if output_root else "rename"
        rel_target = target.relative_to(output_root) if output_root else Path(target.name)
        tape, time_reference, scene, take, tc_display = metadata_by_path.get(source, ("", -1, "", "", ""))
        jobs.append(Job(source, target, 1 if matched_number else 0, trk_number, stream_number, status, mode, rel_target=rel_target, serial_tape=tape, serial_time_reference=time_reference, serial_scene=scene, serial_take=take, serial_tc_display=tc_display, has_channel_suffix=has_numbered_suffix, metadata_template_fields=template_fields, metadata_template_trk_table=template_trk_table))
        if status == "READY":
            planned_targets[target] = planned_targets.get(target, 0) + 1
    source_paths = {job.source for job in jobs}
    for job in jobs:
        if job.status != "READY" or job.target is None:
            continue
        if planned_targets.get(job.target, 0) > 1:
            job.status = "COLLISION"
        elif job.target.exists() and (output_root is not None or job.target not in source_paths):
            job.status = "TARGET EXISTS"
    return jobs


def build_combine_jobs(
    source_root: Path,
    output_root: Path,
    conflict_policy: str,
    copy_non_combined: bool = False,
    progress=None,
    soxi: str = "soxi",
    speaker_pattern: str = DEFAULT_SPEAKER_PATTERN,
    speaker_mapping_text: str = DEFAULT_SPEAKER_MAPPING,
    layout_name: str = "",
    order_name: str = "SMPTE",
    custom_order: str = "",
    rename_merged: bool = False,
    rename_include_format: bool = True,
    rename_include_layout: bool = True,
    rename_position: str = "Append",
    custom_layout_name: str = "",
    only_files: Optional[list[Path]] = None,
) -> list[Job]:
    """`only_files`: combine just these (files sent from Soundminer) — a
    group with channel files beside them that weren't sent is BLOCKED,
    naming what's missing, rather than combined into a poly short of tracks."""
    generic_groups: dict[tuple[Path, str], list[tuple[int, Path]]] = {}
    speaker_groups: dict[tuple[Path, str], list[tuple[str, Path]]] = {}
    passthrough_sources: list[Path] = []
    pattern = speaker_filename_pattern(speaker_pattern)
    mapping = parse_speaker_mapping(speaker_mapping_text)

    def group_key(source: Path) -> Optional[tuple[Path, str]]:
        match = A_NUMBER_PATTERN.match(source.name) or pattern.match(source.name)
        return (source.relative_to(source_root).parent, match["base"]) if match else None

    for source in (only_files if only_files is not None else wav_files(source_root)):
        generic_match = A_NUMBER_PATTERN.match(source.name)
        if generic_match:
            key = (source.relative_to(source_root).parent, generic_match["base"])
            generic_groups.setdefault(key, []).append((int(generic_match["ch"]), source))
            continue
        speaker_match = pattern.match(source.name)
        if speaker_match:
            key = (source.relative_to(source_root).parent, speaker_match["base"])
            speaker_groups.setdefault(key, []).append((speaker_match["token"], source))
        else:
            passthrough_sources.append(source)
    not_sent: dict[tuple[Path, str], list[Path]] = {}
    if only_files is not None:
        sent = set(only_files)
        for folder in sorted({source.parent for source in only_files}):
            for sibling in wav_files(folder):
                key = group_key(sibling) if sibling.parent == folder and not sibling.name.startswith("._") else None
                if key is not None and sibling not in sent:
                    not_sent.setdefault(key, []).append(sibling)

    jobs: list[Job] = []
    keys = sorted(set(generic_groups) | set(speaker_groups), key=lambda item: (item[0].as_posix().casefold(), item[1].casefold()))
    for index, key in enumerate(keys, start=1):
        rel_parent, base = key
        generic_entries = generic_groups.get(key, [])
        speaker_entries = speaker_groups.get(key, [])
        sources = [source for _number, source in generic_entries] + [source for _token, source in speaker_entries]
        target = output_root / rel_parent / f"{base}.WAV"
        status = split_conflict_status(target, conflict_policy)
        mode = "combine"
        kind = "generic"
        order: tuple[str, ...] = ()
        channel_mask = 0
        if generic_entries and speaker_entries:
            status = "BLOCKED: ambiguous filename pattern / channel interpretation"
            ordered_sources = sources
        elif generic_entries:
            ordered_sources = [source for _number, source in sorted(generic_entries, key=lambda item: item[0])]
        else:
            canonical_entries: list[tuple[str, Path]] = []
            unknown_tokens = []
            for token, source in speaker_entries:
                canonical = mapping.get(token.casefold())
                if canonical is None:
                    unknown_tokens.append(token)
                else:
                    canonical_entries.append((canonical, source))
            duplicates = {channel for channel, _source in canonical_entries if sum(item[0] == channel for item in canonical_entries) > 1}
            if unknown_tokens:
                status = f"BLOCKED: unknown channel token {unknown_tokens[0]}"
            elif duplicates:
                status = f"BLOCKED: duplicate channel {sorted(duplicates)[0]}"
            elif len(canonical_entries) == 1:
                ordered_sources = [canonical_entries[0][1]]
            else:
                channels = {channel for channel, _source in canonical_entries}
                try:
                    order = speaker_order("", order_name, channels)
                except ValueError:
                    order = ()
                if not order:
                    status = "BLOCKED: channels do not match selected order"
                else:
                    by_channel = dict(canonical_entries)
                    ordered_sources = [by_channel[channel] for channel in order]
                    mode = "merge"
                    kind = "speaker"
                    channel_mask = speaker_channel_mask(order) if all(channel in SPEAKER_MASK_BITS for channel in order) else 0
                    target = output_root / rel_parent / merge_target_name(base, source.suffix, order_name, order, rename_merged, rename_include_format, rename_include_layout, rename_position, custom_layout_name)
                    status = split_conflict_status(target, conflict_policy)
            if status.startswith("BLOCKED"):
                ordered_sources = [source for _token, source in speaker_entries]
        missing = not_sent.get(key, [])
        if missing and not status.startswith("BLOCKED"):
            names = ", ".join(path.name for path in missing[:3]) + (f" (+{len(missing) - 3} more)" if len(missing) > 3 else "")
            status = f"BLOCKED: only {len(ordered_sources)} of {len(ordered_sources) + len(missing)} channel files were sent — missing {names}"
        if not status.startswith("BLOCKED") and len(ordered_sources) == 1:
            if copy_non_combined:
                target = output_root / ordered_sources[0].relative_to(source_root)
                status = split_conflict_status(target, conflict_policy)
                if status == "READY":
                    status = "READY COPY"
                mode = "combine_copy"
            else:
                status = "SKIP SINGLE"
        if not status.startswith("BLOCKED") and len(ordered_sources) > 1:
            try:
                audio_infos = [get_audio_info(soxi, source) for source in ordered_sources]
                if any(info["channels"] != "1" for info in audio_infos):
                    status = "BLOCKED: source is not mono"
                elif any(any(info[field] != audio_infos[0][field] for field in ("sample_rate", "bit_depth", "encoding", "duration", "total_samples")) for info in audio_infos[1:]):
                    status = "BLOCKED: inconsistent source format"
            except Exception as exc:
                status = f"BLOCKED: {exc}{structural_damage_hint(ordered_sources)}"
        job = Job(ordered_sources[0], target, len(ordered_sources), None, len(ordered_sources), status, mode, rel_target=target.relative_to(output_root), sources=ordered_sources, combine_kind=kind, speaker_order=order, channel_mask=channel_mask)
        jobs.append(job)
        if progress is not None:
            progress(index, len(keys), ordered_sources[0], len(ordered_sources), status)

    if copy_non_combined:
        for source in passthrough_sources:
            target = output_root / source.relative_to(source_root)
            status = split_conflict_status(target, conflict_policy)
            if status == "READY":
                status = "READY COPY"
            jobs.append(Job(source, target, 1, None, None, status, "combine_copy", rel_target=source.relative_to(source_root), sources=[source]))
    return jobs


def execute_rename_jobs(jobs: list[Job]) -> list[dict[str, str]]:
    ready = [job for job in jobs if job.status == "READY" and job.target is not None]
    temp_pairs: list[tuple[Job, Path]] = []
    rows: list[dict[str, str]] = []
    if ready and ready[0].mode == "rename_copy":
        for job in ready:
            try:
                job.target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(job.source, job.target)
                rows.append({"mode": job.mode, "source": str(job.source), "target": str(job.target), "final_target": "", "channel": str(job.channel), "trk_number": str(job.trk_number or ""), "stream_number": str(job.stream_number or ""), "status": "OK"})
            except OSError as exc:
                rows.append({"mode": job.mode, "source": str(job.source), "target": str(job.target), "final_target": "", "channel": str(job.channel), "trk_number": str(job.trk_number or ""), "stream_number": str(job.stream_number or ""), "status": f"FAIL: {exc}"})
        return rows

    try:
        for job in ready:
            temp_path = job.source.with_name(f".{job.source.name}.rename-{uuid.uuid4().hex}")
            os.rename(job.source, temp_path)
            temp_pairs.append((job, temp_path))
        for job, temp_path in temp_pairs:
            os.rename(temp_path, job.target)
            rows.append({"mode": job.mode, "source": str(job.source), "target": str(job.target), "final_target": "", "channel": str(job.channel), "trk_number": str(job.trk_number or ""), "stream_number": str(job.stream_number or ""), "status": "OK"})
    except OSError as exc:
        for job, temp_path in reversed(temp_pairs):
            if temp_path.exists() and not job.source.exists():
                os.rename(temp_path, job.source)
        rows.append({"mode": "rename", "source": "", "target": "", "final_target": "", "channel": "", "trk_number": "", "stream_number": "", "status": f"FAIL: {exc}"})
    return rows


def execute_rename_jobs_stepwise(jobs: list[Job], progress, pause_requested: threading.Event, cancel_requested: threading.Event) -> list[dict[str, str]]:
    ready = [job for job in jobs if job.status == "READY" and job.target is not None]
    rows: list[dict[str, str]] = []
    started = time.monotonic()
    total = len(ready)

    def wait_or_cancel() -> bool:
        while pause_requested.is_set() and not cancel_requested.is_set():
            time.sleep(0.2)
        return cancel_requested.is_set()

    if ready and ready[0].mode == "rename_copy":
        for index, job in enumerate(ready, start=1):
            if wait_or_cancel():
                rows.append({"mode": job.mode, "source": str(job.source), "target": str(job.target), "final_target": "", "channel": str(job.channel), "trk_number": str(job.trk_number or ""), "stream_number": str(job.stream_number or ""), "status": "CANCELLED"})
                continue
            status = "OK"
            try:
                job.target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(job.source, job.target)
            except OSError as exc:
                status = f"FAIL: {exc}"
            rows.append({"mode": job.mode, "source": str(job.source), "target": str(job.target), "final_target": "", "channel": str(job.channel), "trk_number": str(job.trk_number or ""), "stream_number": str(job.stream_number or ""), "status": status})
            elapsed = time.monotonic() - started
            eta = (elapsed / index) * (total - index) if index else 0
            progress(index, total, status, elapsed, eta, job)
        return rows

    temp_pairs: list[tuple[Job, Path]] = []

    def cancelled_rows() -> list[dict[str, str]]:
        return [processing_report_row(job, "CANCELLED") for job in ready]

    def rollback_staged() -> None:
        for job, temp_path in reversed(temp_pairs):
            if temp_path.exists() and not job.source.exists():
                os.rename(temp_path, job.source)
            elif job.target.exists() and not job.source.exists():
                os.rename(job.target, job.source)

    try:
        for index, job in enumerate(ready, start=1):
            if wait_or_cancel():
                rollback_staged()
                return cancelled_rows()
            temp_path = job.source.with_name(f".{job.source.name}.rename-{uuid.uuid4().hex}")
            os.rename(job.source, temp_path)
            temp_pairs.append((job, temp_path))
            elapsed = time.monotonic() - started
            eta = (elapsed / max(index, 1)) * (total - index)
            progress(index, total, "STAGED", elapsed, eta, job)

        if wait_or_cancel():
            rollback_staged()
            return cancelled_rows()

        for index, (job, temp_path) in enumerate(temp_pairs, start=1):
            os.rename(temp_path, job.target)
            rows.append({"mode": job.mode, "source": str(job.source), "target": str(job.target), "final_target": "", "channel": str(job.channel), "trk_number": str(job.trk_number or ""), "stream_number": str(job.stream_number or ""), "status": "OK"})
            elapsed = time.monotonic() - started
            eta = (elapsed / max(index, 1)) * (len(temp_pairs) - index)
            progress(index, len(temp_pairs), "OK", elapsed, eta, job)
    except OSError as exc:
        rollback_staged()
        rows.append({"mode": "rename", "source": "", "target": "", "final_target": "", "channel": "", "trk_number": "", "stream_number": "", "status": f"FAIL: {exc}"})
    return rows


def execute_combine_job(job: Job, sox: str, bwfmetaedit: str, replace: bool) -> None:
    if not job.target or not job.sources:
        raise RuntimeError("missing combine target or sources")
    if job.mode == "combine_copy" or len(job.sources) == 1:
        job.target.parent.mkdir(parents=True, exist_ok=True)
        if job.target.exists() and not replace:
            raise RuntimeError("target exists")
        shutil.copy2(job.sources[0], job.target)
        return
    combine_with_sox(sox, job.sources, job.target, replace)
    try:
        combined_ixml = poly_ixml_from_mono_sources(bwfmetaedit, job.sources)
        write_combine_metadata(bwfmetaedit, job.sources, job.target, combined_ixml)
    except Exception as exc:
        raise MergeMetadataError(str(exc)) from exc


def finish_reembed_renames(pending: list[tuple[Job, dict[str, str]]]) -> None:
    """By the time a job gets here, its "If output exists" conflict (a
    rename-after-embed target that already existed) was already resolved
    up front — build_reembed_jobs()/refresh_filename_mode() mark a real
    conflict CONFLICT/SKIP EXISTS (excluded from execution entirely, so it
    never reaches here at all) or READY REPLACE (conflict explicitly
    resolved to overwrite, including via the "Ask" dialog — see
    _execute_worker's main loop), matching Split/Combine's own pattern of
    deciding before execution rather than partway through it. So a
    `final_target` that unexpectedly still exists here — outside of that
    READY REPLACE case — reflects something not accounted for (e.g. a race
    since Preview ran, or a duplicate final_target within this same batch)
    rather than a normal, already-decided conflict, and is a hard failure
    rather than something to quietly skip or ask about again."""
    final_counts: dict[Path, int] = {}
    moving_targets = {job.target for job, _row in pending if job.target is not None}
    for job, _row in pending:
        if job.final_target is not None:
            final_counts[job.final_target] = final_counts.get(job.final_target, 0) + 1

    ready: list[tuple[Job, dict[str, str]]] = []
    for job, row in pending:
        if job.target is None or job.final_target is None:
            row["status"] = "FAIL: missing rename target"
        elif final_counts.get(job.final_target, 0) > 1:
            row["status"] = f"FAIL: duplicate rename target: {job.final_target.name}"
        elif job.final_target.exists() and job.final_target not in moving_targets and job.status != "READY REPLACE":
            row["status"] = f"FAIL: rename target exists: {job.final_target.name}"
        else:
            ready.append((job, row))

    temp_pairs: list[tuple[Job, dict[str, str], Path]] = []
    try:
        for job, row in ready:
            temp_path = job.target.with_name(f".{job.target.name}.reembed-rename-{uuid.uuid4().hex}")
            os.rename(job.target, temp_path)
            temp_pairs.append((job, row, temp_path))
        for job, row, temp_path in temp_pairs:
            os.rename(temp_path, job.final_target)
            job.target = job.final_target
            row["target"] = str(job.target)
            row["status"] = "OK"
    except OSError as exc:
        for job, _row, temp_path in reversed(temp_pairs):
            if job.target is not None and temp_path.exists() and not job.target.exists():
                os.rename(temp_path, job.target)
        for _job, row in ready:
            row["status"] = f"FAIL: rename {exc}"


class WorkflowApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_NAME} {VERSION}")
        saved_settings = load_app_settings()
        self._saved_settings = saved_settings
        # Fixed by the script, not user-editable — Settings shows this
        # list read-only (see KEYBINDING_ACTIONS' own docstring comment).
        self.keybindings: dict[str, str] = dict(DEFAULT_KEYBINDINGS)
        self._bound_keybinding_sequences: dict[str, str] = {}
        self.root.geometry(saved_settings.get("window_geometry") or "1180x900")
        self.root.minsize(980, 760)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.mode_var = tk.StringVar(value="Browse")
        self.source_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.qc_source_var = tk.StringVar(value="Not loaded")
        self.qc_target_var = tk.StringVar(value="Not loaded")
        self.qc_message_var = tk.StringVar(value="Load Source and Target WAV files.")
        self.qc_source_path: Optional[Path] = None
        self.qc_target_path: Optional[Path] = None
        self.qc_input_source_var = tk.StringVar(value="Not loaded")
        self.qc_input_target_var = tk.StringVar(value="Not loaded")
        self.qc_input_source_path: Optional[Path] = None
        self.qc_input_target_path: Optional[Path] = None
        self._qc_auto_request_id = 0
        self._qc_progress_log_state: dict = {}
        self.qc_type_var = tk.StringVar(value="File ↔ File")
        self.qc_source_folder_var = tk.StringVar(value="Not loaded")
        self.qc_target_folder_var = tk.StringVar(value="Not loaded")
        self.qc_folder_message_var = tk.StringVar(value="Load Source and Target folders.")
        self.qc_source_folder: Optional[Path] = None
        self.qc_target_folder: Optional[Path] = None
        self._qc_folder_request_id = 0
        self.qc_poly_source_folder_var = tk.StringVar(value="Not loaded")
        self.qc_poly_target_folder_var = tk.StringVar(value="Not loaded")
        self.qc_poly_message_var = tk.StringVar(value="Load Source and Target folders.")
        self.qc_poly_source_folder: Optional[Path] = None
        self.qc_poly_target_folder: Optional[Path] = None
        self.qc_poly_rows: dict[str, dict[str, object]] = {}
        self.qc_folder_rows: dict[str, dict[str, object]] = {}
        self._qc_poly_has_metadata = False
        self._qc_folder_has_metadata = False
        self.qc_file_comparison: Optional[dict[str, object]] = None
        self.qc_compare_audio_var = tk.BooleanVar(value=True)
        self.qc_compare_metadata_var = tk.BooleanVar(value=True)
        self._qc_poly_request_id = 0
        self.output_formula_var = tk.StringVar(value=DEFAULT_OUTPUT_FORMULA)
        self.source_formula_var = tk.StringVar(value=DEFAULT_SOURCE_FORMULA)
        self.rename_formula_var = tk.StringVar()
        self.rename_text_position_var = tk.StringVar(value="Append")
        self.rename_find_vars = [tk.StringVar() for _index in range(3)]
        self.rename_replace_vars = [tk.StringVar() for _index in range(3)]
        self.rename_find_replace_enabled_var = tk.BooleanVar(value=False)
        self.rename_regex_var = tk.BooleanVar(value=False)
        self.rename_serial_prefix_var = tk.StringVar(value="ABC")
        self.rename_serial_digits_var = tk.StringVar(value="4")
        self.rename_serial_start_var = tk.StringVar(value="1")
        self.rename_serial_enabled_var = tk.BooleanVar(value=False)
        self.rename_serial_position_var = tk.StringVar(value="Prepend")
        self.rename_serial_order_var = tk.StringVar(value="Folder Path + Filename")
        self.rename_after_embed_var = tk.BooleanVar(value=False)
        self.reembed_rename_formula_var = tk.StringVar(value=DEFAULT_OUTPUT_FORMULA)
        self.rename_apply_formula_var = tk.BooleanVar(value=False)
        self.rename_change_number_var = tk.BooleanVar(value=False)
        self.rename_metadata_template_var = tk.StringVar(value="$srcfile")
        self.rename_metadata_template_enabled_var = tk.BooleanVar(value=False)
        self.rename_base_mode_var = tk.StringVar(value="Formula")
        self._metadata_template_scan_current = False
        self.combine_copy_non_combined_var = tk.BooleanVar(value=False)
        self.speaker_pattern_var = tk.StringVar(value=DEFAULT_SPEAKER_PATTERN)
        self.merge_layout_var = tk.StringVar(value="Detected / Auto")
        self.merge_order_var = tk.StringVar(value="SMPTE")
        self.merge_custom_order_var = tk.StringVar(value="L,R,C,LFE,Ls,Rs")
        self.merge_rename_var = tk.BooleanVar(value=False)
        self.merge_include_format_var = tk.BooleanVar(value=True)
        self.merge_include_layout_var = tk.BooleanVar(value=True)
        self.merge_rename_position_var = tk.StringVar(value="Append")
        self.merge_custom_layout_name_var = tk.StringVar(value="custom")
        self.rename_number_mode_var = tk.StringVar(value="Trk#")
        self.name_mode_var = tk.StringVar(value="Trk#")
        self.conflict_policy_var = tk.StringVar(value="Skip existing")
        # Default on: an already-mono source (a wireless lav/boom file
        # sitting alongside a poly recorder's takes, say) has nothing to
        # split — copy it through as-is rather than blocking it on a
        # Trk#/iXML it was never going to have anyway.
        self.split_copy_mono_var = tk.BooleanVar(value=True)
        self.sox_var = tk.StringVar(value=find_executable(("sox",), ("/opt/homebrew/bin/sox", "/usr/local/bin/sox")))
        self.soxi_var = tk.StringVar(value=find_executable(("soxi",), ("/opt/homebrew/bin/soxi", "/usr/local/bin/soxi")))
        self.bwf_var = tk.StringVar(value=find_executable(("bwfmetaedit",), ("/opt/homebrew/bin/bwfmetaedit", "/usr/local/bin/bwfmetaedit")))
        self.status_var = tk.StringVar(value="Choose folders, then Preview.")
        self.preview_output_root: Optional[Path] = None
        self.current_source_root: Optional[Path] = None
        self.current_output_root: Optional[Path] = None
        self.last_source_dialog_dir: Optional[Path] = None
        self.last_output_dialog_dir: Optional[Path] = None
        self.inspector_window: Optional[tk.Toplevel] = None
        self.inspector: Optional[AudioInspector] = None
        self.jobs: list[Job] = []
        saved_presets = saved_settings.get("formula_presets")
        if isinstance(saved_presets, list) and saved_presets and all(isinstance(item, str) and item for item in saved_presets):
            self.formula_presets: list[str] = saved_presets
        else:
            self.formula_presets: list[str] = list(DEFAULT_FORMULA_PRESETS)
        self._formula_combo_widgets: list[ttk.Combobox] = []
        self.rename_serial_ids: dict[str, str] = {}
        self.rename_serial_signature: Optional[tuple[object, ...]] = None
        self._rename_metadata_request_id = 0
        self._rename_metadata_loaded_root: Optional[Path] = None
        self._rename_metadata_loading_root: Optional[Path] = None
        self.preview_jobs: dict[str, Job] = {}
        self._table_column_labels: dict[str, str] = dict(TABLE_COLUMN_LABELS)
        self._metadata_scan_current = False
        self.preview_sort_column: Optional[str] = None
        self.preview_sort_reverse = False
        self.pause_requested = threading.Event()
        self.cancel_requested = threading.Event()
        self._conflict_response_event = threading.Event()
        self._conflict_response: Optional[tuple[str, bool]] = None
        self._conflict_apply_all_decision: Optional[str] = None
        self.active_operation_phase: Optional[str] = None
        self.active_control_capabilities = OperationControlCapabilities()
        self.events: Queue[tuple[str, object]] = Queue()
        self._import_note = ""  # "N left out" from the last drop/send, kept for its final status line
        self._live_refresh_after: Optional[str] = None
        self._tool_poll_after: Optional[str] = None
        self._build_ui()
        self._install_live_refresh_traces()
        self._update_mode_ui()
        self._apply_all_keybindings()
        self.root.after(100, self._drain_events)
        self.root.after(300, self._check_required_tools_on_startup)
        # Lists sent from Soundminer ("Wave Manager — Send to …") arrive here.
        self.inbox = InboxPoller(self.root, WAVE_MANAGER, self.receive_batch)

    def receive_batch(self, batch: Batch) -> bool:
        """A list sent from Soundminer ("Wave Manager — Send to Split /
        Combine / Rename"): switch to that mode and add the files to the
        list, exactly as if they had been dropped on it — never executed.
        Returns False — try again shortly — while a Preview or Execute is
        running."""
        mode_value = SEND_ACTION_MODES.get(batch.action)
        if mode_value is None:
            return True
        if self.active_operation_phase is not None:
            return False
        bring_to_front(self.root)
        self._add_files_to_list(batch.files, mode_value, "Soundminer sent")
        return True

    def _add_files_to_list(self, paths: list[Path], mode_value: str, origin: str) -> None:
        """Add dropped or sent files to the list in `mode_value` (Split,
        Rename or Combine). Files inside the current Source join the list
        there; otherwise every job's paths would need a different Source, so
        the list is replaced (Source = the files' folder) — asked first when
        there is a list to lose (files outside Source, or a list from another
        mode). Anything that isn't an existing WAV file is left out and
        named in the Log, and the count stays in the final status line, so a
        selection of 1,346 that imports 1,345 says why."""
        wanted = list(dict.fromkeys(paths))
        files = sorted({path for path in wanted if path.is_file() and path.suffix.lower() == ".wav" and not path.name.startswith("._")}, key=natural_path_key)
        kept = set(files)
        skipped = [path for path in wanted if path not in kept]
        label = SEND_MODE_LABELS.get(mode_value, mode_value)
        self.append_log(f"=== {origin} {len(wanted)} file(s) for {label}")
        if skipped:
            self.append_log(f"Left out {len(skipped)} — not an existing WAV file:")
            for path in skipped[:50]:
                self.append_log(f"  {path}" + ("  (file not found)" if not path.exists() else ""))
            if len(skipped) > 50:
                self.append_log(f"  … and {len(skipped) - 50} more")
        self._import_note = f"{len(skipped)} of {len(wanted)} left out: not an existing WAV file — see Log" if skipped else ""
        if not files:
            self.status_var.set(f"{origin} {len(wanted)} file(s) for {label} — none of them is an existing WAV file (see Log).")
            self.append_log(self.status_var.get())
            return
        source_text = self.source_var.get().strip()
        source_root = Path(source_text).expanduser() if source_text else None
        inside = source_root is not None and all(path.is_relative_to(source_root) for path in files)
        same_mode = self.mode_var.get() == mode_value
        if self.jobs and not (same_mode and inside):
            why = f"they are outside the current Source ({source_root})" if not inside else f"the current list is for {self.mode_var.get()}"
            if not messagebox.askyesno(
                APP_NAME,
                f"{origin} {len(files)} file(s) for {label}, but {why}.\n\n"
                f"Replace the current list ({len(self.jobs)} job(s)) with these files?",
                parent=self.root,
            ):
                self.status_var.set(f"{len(files)} file(s) for {label} not added — the current list was kept.")
                self.append_log(self.status_var.get())
                return
            self.clear_preview_list(reason=f"Replaced by {len(files)} file(s).")
        if not inside:
            self.source_var.set("")  # the import adopts the files' own folder as Source
        if not same_mode:
            self._switch_to_mode(mode_value)
        self._import_dropped_files(files)

    def _check_required_tools_on_startup(self) -> None:
        missing_names = []
        if not self.sox_var.get().strip():
            missing_names.append("SoX")
        if not self.bwf_var.get().strip():
            missing_names.append("BWF MetaEdit")
        if not missing_names:
            return
        brew_path = find_executable(("brew",), ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"))
        message = f"{' and '.join(missing_names)} {'is' if len(missing_names) == 1 else 'are'} required but not installed.\n\n"
        if not brew_path:
            message += "Homebrew (the package manager used to install them) is also not installed.\n\n"
            message += "Open Terminal and install Homebrew, then SoX and BWF MetaEdit now?"
        else:
            message += "Open Terminal and install them with Homebrew now?"
        if messagebox.askyesno(APP_NAME, message):
            self._launch_tool_installer(install_brew=not brew_path)

    def _launch_tool_installer(self, install_brew: bool) -> None:
        script_path = Path("/tmp/sox_metadata_workflow_auto_install.command")
        script_path.write_text(build_tool_installer_script(install_brew), encoding="utf-8")
        script_path.chmod(0o755)
        try:
            subprocess.Popen(["open", "-a", "Terminal", str(script_path)])
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open Terminal: {exc}")
            return
        self._start_tool_recheck_polling()

    def _start_tool_recheck_polling(self, attempts_left: int = 300) -> None:
        if self._tool_poll_after is not None:
            self.root.after_cancel(self._tool_poll_after)
            self._tool_poll_after = None
        self.status_var.set("Waiting for SoX / BWF MetaEdit installation to finish in Terminal...")
        self._tool_poll_after = self.root.after(3000, self._poll_for_installed_tools, attempts_left)

    def _poll_for_installed_tools(self, attempts_left: int) -> None:
        self._tool_poll_after = None
        sox_path = find_executable(("sox",), ("/opt/homebrew/bin/sox", "/usr/local/bin/sox"))
        soxi_path = find_executable(("soxi",), ("/opt/homebrew/bin/soxi", "/usr/local/bin/soxi"))
        bwf_path = find_executable(("bwfmetaedit",), ("/opt/homebrew/bin/bwfmetaedit", "/usr/local/bin/bwfmetaedit"))
        if sox_path:
            self.sox_var.set(sox_path)
        if soxi_path:
            self.soxi_var.set(soxi_path)
        if bwf_path:
            self.bwf_var.set(bwf_path)
        if sox_path and bwf_path:
            self.status_var.set("SoX and BWF MetaEdit detected — no restart needed, you're ready to go.")
            self.append_log("Detected newly installed SoX / BWF MetaEdit.")
            return
        if attempts_left <= 1:
            self.status_var.set("Still waiting for SoX / BWF MetaEdit. Reopen this app once installation finishes.")
            return
        self._tool_poll_after = self.root.after(3000, self._poll_for_installed_tools, attempts_left - 1)

    def _toggleable_labelframe(self, parent: ttk.Frame, title: str, variable: tk.BooleanVar) -> tuple[ttk.LabelFrame, ttk.Frame, ttk.Checkbutton]:
        # The body's fields stay editable regardless of the checkbox — the
        # checkbox only controls whether this card's action is applied, not
        # whether its settings can be configured. Disabling them while off
        # meant you had to turn a card on just to set it up before turning
        # it (maybe) back off, which the user flagged as unnecessary friction.
        header = ttk.Checkbutton(parent, text=title, variable=variable)
        frame = ttk.LabelFrame(parent, labelwidget=header, padding=8)
        body = ttk.Frame(frame)
        body.pack(fill=tk.BOTH, expand=True)
        return frame, body, header

    def _numbering_combo(self, parent: ttk.Frame, variable: tk.StringVar, command=None) -> ttk.Combobox:
        combo = ttk.Combobox(parent, textvariable=variable, values=("Trk#", "Stream#"), state="readonly", width=10)
        if command is not None:
            combo.bind("<<ComboboxSelected>>", lambda _event: command())
        return combo

    def _build_split_panel(self, parent: ttk.Frame) -> ttk.Frame:
        panel = ttk.Frame(parent)
        box = ttk.LabelFrame(panel, text="Split & Embed", padding=8)
        box.pack(fill=tk.X)
        self._formula_row(box, 0, "Output name", self.output_formula_var, "Use {base} and $. Examples: {base}.A$, {base}_A$, {base}_Ch$, {base}_trk$")
        ttk.Label(box, text="Filename numbering", width=16).grid(row=1, column=0, sticky="w", pady=(8, 3))
        self.name_mode_combo = self._numbering_combo(box, self.name_mode_var, self.refresh_filename_mode)
        self.name_mode_combo.grid(row=1, column=1, sticky="w", pady=(8, 3))
        ttk.Label(box, text="If output exists", width=16).grid(row=2, column=0, sticky="w", pady=3)
        self.split_conflict_combo = ttk.Combobox(box, textvariable=self.conflict_policy_var, values=("Skip existing", "Ask", "Replace existing"), state="readonly", width=17)
        self.split_conflict_combo.grid(row=2, column=1, sticky="w", pady=3)
        self.split_copy_mono_check = ttk.Checkbutton(box, text="Copy non-Poly (mono) source files", variable=self.split_copy_mono_var)
        self.split_copy_mono_check.grid(row=3, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Label(box, text="Metadata (iXML / BEXT) is embedded into every split file automatically.", foreground="#666666").grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        return panel

    def _build_reembed_panel(self, parent: ttk.Frame) -> ttk.Frame:
        panel = ttk.Frame(parent)
        match_box = ttk.LabelFrame(panel, text="Match Source → Target", padding=8)
        match_box.pack(fill=tk.X)
        ttk.Label(
            match_box,
            text="Every WAV in Source and Target is read; no formula needed. A Target belongs to the Source its\n"
            "name starts with (\"12-5T2.A3 render 001\" → 12-5T2.A3, \"12-10T2 render 001\" → mono 12-10T2),\n"
            "then the recorder channel (iXML) must match. Unpaired files on either side are listed.",
            foreground="#666666",
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        ttk.Label(match_box, text="If output exists", width=16).grid(row=2, column=0, sticky="w", pady=3)
        self.reembed_conflict_combo = ttk.Combobox(match_box, textvariable=self.conflict_policy_var, values=("Skip existing", "Ask", "Replace existing"), state="readonly", width=17)
        self.reembed_conflict_combo.grid(row=2, column=1, sticky="w", pady=3)
        rename_box, rename_body, self.rename_check = self._toggleable_labelframe(panel, "Rename matched targets after embed", self.rename_after_embed_var)
        rename_box.pack(fill=tk.X, pady=(8, 0))
        self.rename_check.configure(command=self.refresh_filename_mode)
        self._formula_row(rename_body, 0, "Output name", self.reembed_rename_formula_var, "Use {base} and $. Example: {base}_AAP.A$")
        ttk.Label(rename_body, text="Filename numbering", width=16).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.reembed_number_combo = self._numbering_combo(rename_body, self.rename_number_mode_var, self.refresh_filename_mode)
        self.reembed_number_combo.grid(row=1, column=1, sticky="w", pady=(6, 0))
        return panel

    def _build_rename_panel(self, parent: ttk.Frame) -> ttk.Frame:
        panel = ttk.Frame(parent)

        mode_row = ttk.Frame(panel)
        mode_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(mode_row, text="Rename base using:").pack(side=tk.LEFT)
        ttk.Radiobutton(mode_row, text="Formula", variable=self.rename_base_mode_var, value="Formula", command=self._on_rename_base_mode_changed).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Radiobutton(mode_row, text="Metadata Template", variable=self.rename_base_mode_var, value="Metadata Template", command=self._on_rename_base_mode_changed).pack(side=tk.LEFT, padx=(8, 0))

        cards = ttk.Frame(panel)
        cards.pack(fill=tk.X)
        cards.columnconfigure(0, weight=1, uniform="rename-cards")
        cards.columnconfigure(1, weight=1, uniform="rename-cards")

        self.rename_addtext_box, addtext_body, self.rename_apply_check = self._toggleable_labelframe(cards, "Add Text", self.rename_apply_formula_var)
        self.rename_addtext_box.grid(row=0, column=0, sticky="new", padx=(0, 6), pady=(0, 8))
        self.rename_apply_check.configure(command=self._on_rename_option_changed)
        self.rename_formula_entry = tk.Entry(addtext_body, textvariable=self.rename_formula_var, width=18, bg="white", fg="black", insertbackground="black")
        self.rename_formula_entry.grid(row=0, column=0, sticky="w")
        self.rename_text_position_combo = ttk.Combobox(addtext_body, textvariable=self.rename_text_position_var, values=("Prepend", "Append"), state="readonly", width=10)
        self.rename_text_position_combo.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.rename_text_position_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_rename_option_changed())

        self.rename_change_box, change_body, self.rename_change_check = self._toggleable_labelframe(cards, "Change $", self.rename_change_number_var)
        self.rename_change_box.grid(row=0, column=1, sticky="new", padx=(6, 0), pady=(0, 8))
        self.rename_change_check.configure(command=self._on_rename_option_changed)
        ttk.Label(change_body, text="Renumber to").grid(row=0, column=0, sticky="w")
        self.rename_number_combo = self._numbering_combo(change_body, self.rename_number_mode_var, self.refresh_filename_mode)
        self.rename_number_combo.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(change_body, text="No effect on Poly files or names with no channel suffix.", foreground="#666666").grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        fr_box, fr_body, self.rename_find_replace_check = self._toggleable_labelframe(cards, "Find & Replace", self.rename_find_replace_enabled_var)
        fr_box.grid(row=1, column=0, sticky="new", padx=(0, 6))
        self.rename_find_replace_check.configure(command=self._on_rename_option_changed)
        self.rename_regex_check = ttk.Checkbutton(fr_body, text="Regex", variable=self.rename_regex_var, command=self._on_rename_option_changed)
        self.rename_regex_check.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
        self.rename_find_entries = []
        self.rename_replace_entries = []
        for rule_index in range(3):
            row = rule_index + 1
            ttk.Label(fr_body, text="Find", width=6).grid(row=row, column=0, sticky="w", pady=2)
            find_entry = tk.Entry(fr_body, textvariable=self.rename_find_vars[rule_index], width=14, bg="white", fg="black", insertbackground="black")
            find_entry.grid(row=row, column=1, sticky="w", pady=2)
            ttk.Label(fr_body, text="Replace", width=7).grid(row=row, column=2, sticky="e", padx=(8, 4), pady=2)
            replace_entry = tk.Entry(fr_body, textvariable=self.rename_replace_vars[rule_index], width=14, bg="white", fg="black", insertbackground="black")
            replace_entry.grid(row=row, column=3, sticky="w", pady=2)
            self.rename_find_entries.append(find_entry)
            self.rename_replace_entries.append(replace_entry)

        serial_box, serial_body, self.rename_serial_check = self._toggleable_labelframe(cards, "Serial ID", self.rename_serial_enabled_var)
        serial_box.grid(row=1, column=1, sticky="new", padx=(6, 0))
        self.rename_serial_check.configure(command=self._on_rename_serial_enabled_changed)
        ttk.Label(serial_body, text="prefix", width=6).grid(row=0, column=0, sticky="w", pady=2)
        self.rename_serial_prefix_entry = tk.Entry(serial_body, textvariable=self.rename_serial_prefix_var, width=8, bg="white", fg="black", insertbackground="black")
        self.rename_serial_prefix_entry.grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(serial_body, text="digits", width=6).grid(row=1, column=0, sticky="w", pady=2)
        self.rename_serial_digits_entry = tk.Entry(serial_body, textvariable=self.rename_serial_digits_var, width=8, bg="white", fg="black", insertbackground="black")
        self.rename_serial_digits_entry.grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(serial_body, text="start", width=6).grid(row=2, column=0, sticky="w", pady=2)
        self.rename_serial_start_entry = tk.Entry(serial_body, textvariable=self.rename_serial_start_var, width=8, bg="white", fg="black", insertbackground="black")
        self.rename_serial_start_entry.grid(row=2, column=1, sticky="w", pady=2)
        self.rename_serial_position_combo = ttk.Combobox(serial_body, textvariable=self.rename_serial_position_var, values=("Prepend", "Append"), state="readonly", width=10)
        self.rename_serial_position_combo.grid(row=2, column=2, sticky="w", padx=(8, 0), pady=2)
        self.rename_serial_position_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_rename_option_changed())
        ttk.Label(serial_body, text="ID order", width=6).grid(row=3, column=0, sticky="w", pady=2)
        self.rename_serial_order_combo = ttk.Combobox(serial_body, textvariable=self.rename_serial_order_var, values=("Folder Path + Filename", "Metadata Tape + TC"), state="readonly", width=22)
        self.rename_serial_order_combo.grid(row=3, column=1, columnspan=2, sticky="w", pady=2)
        self.rename_serial_order_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_rename_serial_order_changed())

        self.rename_metadata_template_box = ttk.LabelFrame(panel, text="Metadata Template", padding=8)
        ttk.Label(self.rename_metadata_template_box, text="Defines the whole renamed filename (before the extension) from a free-form $token template — no automatic channel-number suffix; add $trk/$chnum yourself if you want one kept.", foreground="#666666", wraplength=520, justify=tk.LEFT).grid(row=0, column=0, columnspan=2, sticky="w")
        self.rename_metadata_template_entry = tk.Entry(self.rename_metadata_template_box, textvariable=self.rename_metadata_template_var, width=40, bg="white", fg="black", insertbackground="black")
        self.rename_metadata_template_entry.grid(row=1, column=0, sticky="ew", pady=(4, 6))
        self.rename_metadata_template_entry.bind("<Return>", lambda _event: self._on_rename_option_changed())
        self.rename_metadata_template_entry.bind("<FocusOut>", lambda _event: self._on_rename_option_changed())
        self.rename_metadata_template_box.columnconfigure(0, weight=1)

        next_row = 2
        tokens_per_line = 7
        for label, tokens in (
            ("File / Session", METADATA_TEMPLATE_SESSION_TOKENS),
            ("File Metadata (BWF/iXML)", METADATA_TEMPLATE_META_TOKENS),
        ):
            ttk.Label(self.rename_metadata_template_box, text=label, foreground="#666666").grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(2, 0))
            next_row += 1
            for line_start in range(0, len(tokens), tokens_per_line):
                token_row = ttk.Frame(self.rename_metadata_template_box)
                token_row.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(0, 1))
                next_row += 1
                for token in tokens[line_start:line_start + tokens_per_line]:
                    ttk.Button(token_row, text=token, width=len(token) + 1, command=lambda tk_text=token: self._insert_metadata_token(tk_text)).pack(side=tk.LEFT, padx=(0, 2), pady=1)

        self.rename_status_hint_label = ttk.Label(panel, text="Status lists exactly which of these apply to each file — e.g. “Ready to Add Text, Find & Replace, and Add ID”.", foreground="#666666")
        self.rename_status_hint_label.pack(anchor="w", pady=(4, 0))
        self._apply_rename_base_mode_visibility()
        return panel

    def _apply_rename_base_mode_visibility(self) -> None:
        # Formula (Add Text + Change $) and Metadata Template are mutually
        # exclusive ways to build the renamed base name, not two things
        # that stack — showing only the relevant one also keeps this,
        # easily the tallest mode's panel, from pushing the Execution Log
        # off a screen that's already at its maximum height. Find &
        # Replace and Serial ID stay visible either way: both are generic
        # post-processing steps that make sense regardless of how the base
        # name itself was built.
        metadata_mode = self.rename_base_mode_var.get() == "Metadata Template"
        if metadata_mode:
            self.rename_addtext_box.grid_remove()
            self.rename_change_box.grid_remove()
            self.rename_metadata_template_box.pack(fill=tk.X, pady=(0, 8), before=self.rename_status_hint_label)
        else:
            self.rename_addtext_box.grid()
            self.rename_change_box.grid()
            self.rename_metadata_template_box.pack_forget()

    def _on_rename_base_mode_changed(self) -> None:
        metadata_mode = self.rename_base_mode_var.get() == "Metadata Template"
        if metadata_mode and not self._metadata_template_scan_current and self.source_var.get().strip():
            if not self._confirm_metadata_scan("Metadata Template"):
                self.rename_base_mode_var.set("Formula")
                return
        self.rename_metadata_template_enabled_var.set(metadata_mode)
        if metadata_mode:
            self.rename_apply_formula_var.set(False)
            self.rename_change_number_var.set(False)
        self._apply_rename_base_mode_visibility()
        if metadata_mode and not self._metadata_template_scan_current:
            self._update_mode_ui()
            self.preview()
            return
        self._on_rename_option_changed()

    def _insert_metadata_token(self, token: str) -> None:
        if self.mode_var.get() != "Rename":
            self.mode_var.set("Rename")
            self._update_mode_ui()
        entry = self.rename_metadata_template_entry
        variable = self.rename_metadata_template_var
        try:
            cursor = entry.index(tk.INSERT)
        except tk.TclError:
            cursor = len(variable.get())
        current = variable.get()
        variable.set(current[:cursor] + token + current[cursor:])
        entry.icursor(cursor + len(token))
        entry.focus_set()

    def _metadata_template_status_label(self, job: Job) -> str:
        """"Metadata Template" for display_status()'s action list — or, if
        this specific job has a token that resolved to nothing (e.g. a file
        with no Scene metadata using $scene), a version naming which ones,
        so a missing value shows up right in the Status column and in the
        exported TSV instead of just quietly leaving a gap in the filename."""
        if not self.rename_metadata_template_enabled_var.get():
            return ""
        template = self.rename_metadata_template_var.get()
        if not template.strip() or not job.metadata_template_fields:
            return "Metadata Template"
        missing = find_empty_metadata_tokens(template, job.metadata_template_fields, job.metadata_template_trk_table)
        if missing:
            return f"Metadata Template (missing: {', '.join(missing)})"
        return "Metadata Template"

    def _build_combine_panel(self, parent: ttk.Frame) -> ttk.Frame:
        panel = ttk.Frame(parent)
        general_box = ttk.LabelFrame(panel, text="General", padding=8)
        general_box.pack(fill=tk.X)
        self.combine_copy_check = ttk.Checkbutton(general_box, text="Copy non-combined / non-merged files", variable=self.combine_copy_non_combined_var)
        self.combine_copy_check.grid(row=0, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Label(general_box, text="If output exists", width=16).grid(row=1, column=0, sticky="w", pady=3)
        self.combine_conflict_combo = ttk.Combobox(general_box, textvariable=self.conflict_policy_var, values=("Skip existing", "Ask", "Replace existing"), state="readonly", width=17)
        self.combine_conflict_combo.grid(row=1, column=1, sticky="w", pady=3)
        self.merge_options = ttk.LabelFrame(panel, text="Speaker-aware Merge", padding=6)
        self.merge_options.pack(fill=tk.X, pady=(8, 0))
        merge_controls = ttk.Frame(self.merge_options)
        merge_controls.grid(row=0, column=0, sticky="nw", padx=(0, 18))
        ttk.Label(merge_controls, text="Pattern").grid(row=0, column=0, sticky="w")
        tk.Entry(merge_controls, textvariable=self.speaker_pattern_var, width=18, bg="white", fg="black").grid(row=0, column=1, sticky="w", padx=(4, 12))
        ttk.Label(merge_controls, text="Order").grid(row=0, column=2, sticky="w")
        self.merge_order_combo = ttk.Combobox(merge_controls, textvariable=self.merge_order_var, values=("SMPTE", "FILM"), state="readonly", width=12)
        self.merge_order_combo.grid(row=0, column=3, sticky="w", padx=4)
        supported_layouts_button = ttk.Menubutton(merge_controls, text="Supported Channel Layouts", padding=(4, 0))
        supported_layouts_menu = tk.Menu(supported_layouts_button, tearoff=False)
        for layout_help in (
            "Stereo\n  SMPTE / Film: L R",
            "2.1\n  SMPTE / Film: L R LFE",
            "3.0\n  SMPTE / Film: L C R",
            "3.1\n  SMPTE: L R C LFE\n  Film: L C R LFE",
            "5.1\n  SMPTE: L R C LFE Ls Rs\n  Film: L C R Ls Rs LFE",
            "7.1\n  SMPTE: L R C LFE Lss Rss Lsr Rsr\n  Film: L C R Lss Rss Lsr Rsr LFE",
            "7.1.2 — Dolby Atmos Bed\n  L R C LFE Ls Rs Lrs Rrs Ltm Rtm",
        ):
            supported_layouts_menu.add_command(label=layout_help)
        supported_layouts_menu.add_separator()
        supported_layouts_menu.add_command(label="Layouts beyond 7.1.2 are not supported.")
        supported_layouts_button.configure(menu=supported_layouts_menu)
        supported_layouts_button.grid(row=0, column=4, sticky="w", padx=(12, 0))
        ttk.Label(merge_controls, text="Layout").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.merge_layout_combo = ttk.Combobox(merge_controls, textvariable=self.merge_layout_var, values=("Detected / Auto",), state="readonly", width=15)
        self.merge_layout_combo.grid(row=1, column=1, sticky="w", padx=(4, 12), pady=(6, 0))
        ttk.Checkbutton(merge_controls, text="Rename merged files", variable=self.merge_rename_var).grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Checkbutton(merge_controls, text="Format", variable=self.merge_include_format_var).grid(row=2, column=1, sticky="w", pady=(8, 0))
        ttk.Checkbutton(merge_controls, text="Layout", variable=self.merge_include_layout_var).grid(row=2, column=2, sticky="w", pady=(8, 0))
        ttk.Combobox(merge_controls, textvariable=self.merge_rename_position_var, values=("Append", "Prepend"), state="readonly", width=8).grid(row=3, column=0, sticky="w", pady=(6, 0))
        tk.Entry(merge_controls, textvariable=self.merge_custom_layout_name_var, width=16, bg="white", fg="black").grid(row=3, column=1, sticky="w", padx=(4, 0), pady=(6, 0))
        return panel

    def _build_ui(self) -> None:
        apply_app_theme(self.root)
        controls = ttk.Frame(self.root, padding=12)
        controls.pack(fill=tk.X)
        self.controls = controls

        tabbar = ttk.Frame(controls)
        tabbar.pack(fill=tk.X, pady=(0, 8))
        self.mode_tab_buttons: dict[str, ttk.Radiobutton] = {}
        for value, label in (
            ("Browse", "Browse"),
            ("Split Poly with SoX + Embed", "Split + Embed"),
            ("Re-embed Matched Render Files", "Re-embed"),
            ("Rename", "Rename"),
            ("Combine / Merge Mono Files to Poly", "Combine / Merge"),
            ("QC", "QC"),
            ("Settings", "Settings"),
        ):
            button = ttk.Radiobutton(tabbar, text=label, variable=self.mode_var, value=value, style="Toolbutton", command=self._update_mode_ui)
            button.pack(side=tk.LEFT, padx=(0, 2))
            self.mode_tab_buttons[value] = button

        self.workflow_folder_picker = WorkflowFolderPicker(
            controls,
            self.source_var,
            self.output_var,
            self.choose_source,
            self.choose_output,
            self._workflow_source_folder_changed,
            self._workflow_target_folder_changed,
        )
        self.workflow_folder_picker.pack(fill=tk.X, pady=(0, 8))
        if self.workflow_folder_picker.target_entry is not None:
            self.workflow_folder_picker.target_entry.bind("<FocusOut>", lambda _event: self._on_output_var_changed(), add="+")
            self.workflow_folder_picker.target_entry.bind("<Return>", lambda _event: self._on_output_var_changed(), add="+")

        self.mode_settings_host = ttk.Frame(controls)
        self.mode_settings_host.pack(fill=tk.X)
        self.split_frame = self._build_split_panel(self.mode_settings_host)
        self.reembed_frame = self._build_reembed_panel(self.mode_settings_host)
        self.rename_frame = self._build_rename_panel(self.mode_settings_host)
        self.combine_frame = self._build_combine_panel(self.mode_settings_host)

        actions = ttk.Frame(controls)
        actions.pack(fill=tk.X, pady=(10, 0))
        self.preview_button = ttk.Button(actions, text="Preview / Refresh", command=self.preview, state=tk.DISABLED)
        self.preview_button.pack(side=tk.LEFT)
        self.execute_button = ttk.Button(actions, text="Execute Ready Jobs", command=self.execute, state=tk.DISABLED)
        self.execute_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="Audio Inspector", command=self.open_inspector).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="Log", command=lambda: self.log_window.show()).pack(side=tk.LEFT, padx=(8, 0))
        self.pause_button = ttk.Button(actions, text="Pause", command=self.toggle_pause, state=tk.DISABLED)
        self.pause_button.pack(side=tk.LEFT, padx=(8, 0))
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self.cancel_processing, state=tk.DISABLED)
        self.cancel_button.pack(side=tk.LEFT, padx=(8, 0))
        self.clear_list_button = ttk.Button(actions, text="Clear List", command=self.clear_preview_list)
        self.clear_list_button.pack(side=tk.LEFT, padx=(8, 0))
        # Structurally damaged sources (Split mode only): hand them to Wave
        # Repair, then bring back just those files instead of re-previewing
        # the whole folder. Its own row — the main action row is already as
        # wide as the default window.
        self.actions_row = actions
        self.structure_actions = ttk.Frame(controls)
        ttk.Label(self.structure_actions, text="Damaged files:", foreground="#666666").pack(side=tk.LEFT)
        self.send_to_repair_button = ttk.Button(self.structure_actions, text="Send Blocked to Wave Repair", command=self.send_structure_blocked_to_repair, state=tk.DISABLED)
        self.send_to_repair_button.pack(side=tk.LEFT, padx=(8, 0))
        self.rescan_blocked_button = ttk.Button(self.structure_actions, text="Rescan Blocked", command=self.rescan_structure_blocked, state=tk.DISABLED)
        self.rescan_blocked_button.pack(side=tk.LEFT, padx=(8, 0))
        # A persistent, always-visible status line — every mode's tab shows
        # it, and it isn't tied to the Log window being open. Before this,
        # the only place status_var showed up as a label was the Settings
        # tab, so with the Log now a separate closable window there was no
        # way to glance at the main Split/Rename/Combine/QC view and tell a
        # Preview or Execute was still running in the background — which is
        # exactly how a drag-and-drop import got fired off during one.
        self.header_status_label = ttk.Label(controls, textvariable=self.status_var, font=("SF Pro Display", 11, "bold"), foreground="#111827")
        self.header_status_label.pack(fill=tk.X, pady=(6, 0))
        table_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        self.table_frame = table_frame
        columns = tuple(col[0] for col in TABLE_COLUMNS)
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings")
        for col, label, _width in TABLE_COLUMNS:
            self.table.heading(col, text=label, command=lambda column=col: self._sort_preview_table(column))
        # Status was the stretch column, but stretch columns get their width
        # recomputed to "whatever's left over" on every layout pass — a
        # manual drag-resize on it looked like it worked, then snapped back
        # on the next resize (the same class of bug already fixed for the
        # QC tables). Status's content is a fairly bounded set of short
        # phrases, so it doesn't need to flex — the Target path column does,
        # since paths vary widely in length; it's stretch now instead
        # (except in Rename mode, where that same column becomes "Before
        # Rename" and the *new* Planned Filename is what's worth flexing —
        # see _apply_table_stretch_column()). This one-time construction
        # call is the only place that sets the actual pixel widths; every
        # later mode switch only repoints which column has stretch=True
        # (see _apply_table_stretch_column() below) without touching
        # width, or a manual drag-resize on ANY column would get wiped the
        # next time the user so much as switched tabs or toggled a Rename
        # checkbox.
        text_columns = {"source", "target", "planned", "status"}
        configure_treeview_column_widths(
            self.table,
            tuple((col, width, col in text_columns) for col, _label, width in TABLE_COLUMNS),
            stretch_column="target",
        )
        scroll_y = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.table.yview)
        scroll_x = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.table.xview)
        self.table.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.table.bind("<<TreeviewSelect>>", self._on_preview_select)
        self._bind_context_menu(self.table, self._on_preview_context_menu)
        self.table.drop_target_register(DND_FILES)
        self.table.dnd_bind("<<Drop>>", self._drop_files_into_list)
        self.table.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.table.tag_configure("default", background="white", foreground="#111827")
        self.log_window = ExecutionLogWindow(self.root, self.status_var)
        self.log_frame = self.log_window
        self.log = self.log_window.panel.text
        self._build_browse_explorer_ui()
        self._build_qc_ui()
        self._build_settings_ui()

    def _build_browse_explorer_ui(self) -> None:
        """Browse mode's own view, in the spirit of REAPER's Media
        Explorer: a Finder-like folder tree on the left; on the right, the
        matching WAV file list on top and a simple play-and-look-at-the-
        waveform preview player underneath — no detailed metadata here at
        all, that's what the Audio Inspector (still one click/selection
        away) is for. There is deliberately no "keep a shortlist" list
        anymore — an earlier version had one alongside a Tag shortcut and
        drag-to-list, removed once the user decided pure browse-and-
        audition was the actual goal, matching Media Explorer's own scope."""
        self.browse_explorer_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        paned = ttk.PanedWindow(self.browse_explorer_frame, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        tree_frame = ttk.Frame(paned)
        self.browse_folder_tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        tree_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.browse_folder_tree.yview)
        self.browse_folder_tree.configure(yscrollcommand=tree_scroll.set)
        self.browse_folder_tree.bind("<<TreeviewOpen>>", self._on_browse_tree_open)
        self.browse_folder_tree.bind("<<TreeviewSelect>>", self._on_browse_tree_select)
        # Arrow-key navigation (Up/Down move, Left/Right collapse/expand —
        # ttk.Treeview's own built-in bindings, untouched) stays cheap:
        # selecting a folder only ever lists that folder's own top-level
        # files. Recursing into every subfolder is a deliberately separate,
        # heavier action, bound below in _apply_all_keybindings() (default
        # Return — see KEYBINDING_ACTIONS / Settings > Keyboard Shortcuts),
        # not something that happens on every arrow-key move.
        self.browse_folder_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll.grid(row=0, column=1, sticky="ns")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        # weight=0: this pane's width only ever changes from a direct drag
        # on its own sash, not as a side effect of the right side's own
        # panes being resized (ttk distributes a container-driven resize
        # across every pane by weight, not just the one actually dragged).
        paned.add(tree_frame, weight=0)

        right_frame = ttk.Frame(paned)
        right_paned = ttk.PanedWindow(right_frame, orient=tk.VERTICAL)
        right_paned.pack(fill=tk.BOTH, expand=True)
        paned.add(right_frame, weight=1)

        file_frame = ttk.Frame(right_paned)
        file_status_row = ttk.Frame(file_frame)
        file_status_row.pack(fill=tk.X, pady=(0, 4))
        self.browse_file_count_var = tk.StringVar(value="Select a folder on the left.")
        ttk.Label(file_status_row, textvariable=self.browse_file_count_var, foreground="#666666").pack(side=tk.LEFT)
        file_list_container = ttk.Frame(file_frame)
        file_list_container.pack(fill=tk.BOTH, expand=True)
        # Two columns instead of one combined "folder/filename" string: a
        # recursive listing's path can get long, and Filename is the part
        # worth always seeing in full rather than truncated or scrolled
        # out of view. Neither is the stretch column — this list sits
        # inside a resizable PanedWindow, so a stretch=True column's width
        # gets recomputed to fill whatever's left over on every resize of
        # its containing pane, not just window-level ones, undoing a
        # manual drag-resize almost immediately. Both columns keep
        # whatever width they're dragged to; the horizontal scrollbar
        # below picks up the slack if their combined width doesn't fit.
        self.browse_file_list = ttk.Treeview(file_list_container, columns=("filename", "path"), show="headings", selectmode="browse")
        self.browse_file_list.heading("filename", text="Filename")
        self.browse_file_list.heading("path", text="Path")
        configure_treeview_column_widths(
            self.browse_file_list,
            (("filename", 400, True), ("path", 800, True)),
            stretch_column="",
        )
        file_scroll_y = ttk.Scrollbar(file_list_container, orient=tk.VERTICAL, command=self.browse_file_list.yview)
        file_scroll_x = ttk.Scrollbar(file_list_container, orient=tk.HORIZONTAL, command=self.browse_file_list.xview)
        self.browse_file_list.configure(yscrollcommand=file_scroll_y.set, xscrollcommand=file_scroll_x.set)
        self.browse_file_list.bind("<<TreeviewSelect>>", self._on_browse_file_select)
        self._bind_context_menu(self.browse_file_list, self._on_browse_file_context_menu)
        self.browse_file_list.grid(row=0, column=0, sticky="nsew")
        file_scroll_y.grid(row=0, column=1, sticky="ns")
        file_scroll_x.grid(row=1, column=0, sticky="ew")
        file_list_container.rowconfigure(0, weight=1)
        file_list_container.columnconfigure(0, weight=1)
        right_paned.add(file_frame, weight=2)

        self._build_browse_player_ui(right_paned)

        self.browse_file_list_paths: dict[str, Path] = {}
        self._populate_browse_tree_roots()

    def _build_browse_player_ui(self, right_paned: ttk.PanedWindow) -> None:
        """A simple play-and-look-at-the-waveform preview, not a full
        transport — no pitch/rate/loop. Playback is a plain
        `sox <file> -d trim <seconds> vol <gain> dB` subprocess (sox is
        already a hard dependency of this app, so this needs no new tool
        path); Pause/Resume are real — SIGSTOP/SIGCONT on that process —
        rather than restarting from a remembered offset. Seeking (clicking
        the waveform) and a live volume change both *do* need a fresh
        process, since sox has no way to change either on one already
        running — both go through the same _browse_player_start_from()."""
        self.browse_player_process: Optional[subprocess.Popen] = None
        self.browse_player_path: Optional[Path] = None
        self.browse_player_duration: float = 0.0
        self.browse_player_peaks: list[float] = []
        # Only populated for a genuine Poly (multi-channel) source — one
        # peaks list per channel, drawn as stacked lanes instead of the
        # single combined line `browse_player_peaks` gives a mono file.
        self.browse_player_channel_peaks: list[list[float]] = []
        self.browse_player_channel_labels: list[str] = []
        # The single persistent playhead canvas item, moved via coords()
        # every tick rather than deleted and recreated along with the rest
        # of the waveform — see _update_browse_playhead().
        self.browse_playhead_item_id: Optional[int] = None
        # Bumped on every _load_browse_player() call; the background poly-
        # details worker stamps its result with the id it was given, so a
        # result for a file the user has since clicked away from is
        # discarded instead of clobbering the (newer) selection's state.
        self._browse_load_request_id = 0
        # One Solo toggle per channel for a Poly source, embedded directly
        # on the waveform canvas (positioned to track each lane) rather
        # than a separate widget tree — this is the only way to keep a
        # real, individually clickable control aligned with a lane whose
        # height depends on the channel count and the pane's current size.
        # Multiple channels can be soloed together (a standard mixer
        # convention), mixed down to one signal via a sox `remix` effect.
        self.browse_solo_vars: list[tk.BooleanVar] = []
        self.browse_solo_window_ids: list[int] = []
        self.browse_player_paused = False
        self.browse_player_start_wall: Optional[float] = None
        self.browse_player_elapsed_base: float = 0.0
        self._browse_player_tick_after: Optional[str] = None
        self._browse_volume_restart_after: Optional[str] = None

        player_frame = ttk.Frame(right_paned, padding=(0, 6, 0, 0))
        controls = ttk.Frame(player_frame)
        controls.pack(fill=tk.X, pady=(0, 4))
        self.browse_play_button = ttk.Button(controls, text="▶ Play", command=self._browse_player_toggle_play_pause, state=tk.DISABLED)
        self.browse_play_button.pack(side=tk.LEFT)
        self.browse_stop_button = ttk.Button(controls, text="■ Stop", command=self._browse_player_stop, state=tk.DISABLED)
        self.browse_stop_button.pack(side=tk.LEFT, padx=(6, 0))
        # Both remembered across restarts (see _on_close()'s save_app_settings()
        # call and this constructor's own load right below), like the app's
        # other persisted settings (window geometry, formula presets).
        self.browse_autoplay_var = tk.BooleanVar(value=bool(self._saved_settings.get("browse_autoplay_on_select", False)))
        ttk.Checkbutton(controls, text="Auto-play on select", variable=self.browse_autoplay_var).pack(side=tk.LEFT, padx=(12, 0))
        # Pro Tools calls this pair of solo behaviors "Solo Mode: X-OR"
        # (exclusive — soloing one channel un-solos every other) vs. its
        # default additive/multi-solo mode. Off (default) keeps today's
        # behavior: any number of channels can be soloed together.
        self.browse_solo_exclusive_var = tk.BooleanVar(value=bool(self._saved_settings.get("browse_solo_exclusive", False)))
        ttk.Checkbutton(controls, text="Solo X-OR", variable=self.browse_solo_exclusive_var).pack(side=tk.LEFT, padx=(12, 0))
        self.browse_player_time_var = tk.StringVar(value="0:00 / 0:00")
        ttk.Label(controls, textvariable=self.browse_player_time_var, font=("SF Mono", 10)).pack(side=tk.RIGHT)

        volume_row = ttk.Frame(player_frame)
        volume_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(volume_row, text="Volume:").pack(side=tk.LEFT)
        # -60 dB stands in for -inf (sox has no literal -inf gain) — the
        # bottom of the slider swaps to a true `vol 0 amplitude` (exact
        # silence) rather than just "very quiet", matching what the user
        # actually asked for.
        self.browse_volume_db_var = tk.DoubleVar(value=0.0)
        volume_scale = ttk.Scale(volume_row, from_=BROWSE_VOLUME_MIN_DB, to=BROWSE_VOLUME_MAX_DB, orient=tk.HORIZONTAL, variable=self.browse_volume_db_var, command=self._on_browse_volume_changed)
        volume_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        self.browse_volume_label_var = tk.StringVar(value="+0.0 dB")
        ttk.Label(volume_row, textvariable=self.browse_volume_label_var, font=("SF Mono", 10), width=9).pack(side=tk.LEFT)
        ttk.Button(volume_row, text="Reset", command=self._reset_browse_volume, width=6).pack(side=tk.LEFT, padx=(6, 0))

        self.browse_player_name_var = tk.StringVar(value="No file selected.")
        ttk.Label(player_frame, textvariable=self.browse_player_name_var, foreground="#666666").pack(anchor="w", pady=(0, 4))

        self.browse_waveform_canvas = tk.Canvas(player_frame, height=110, background="#1b1f24", highlightthickness=0)
        self.browse_waveform_canvas.pack(fill=tk.BOTH, expand=True)
        self.browse_waveform_canvas.bind("<Configure>", self._on_browse_waveform_canvas_configure)
        # Click anywhere on the waveform to seek there and start playing
        # immediately — the whole point of a waveform view over just a
        # bare Play button.
        self.browse_waveform_canvas.bind("<Button-1>", self._on_browse_waveform_click)
        right_paned.add(player_frame, weight=1)

    def _reset_browse_player_ui(self, message: str) -> None:
        self.browse_play_button.configure(state=tk.DISABLED, text="▶ Play")
        self.browse_stop_button.configure(state=tk.DISABLED)
        self.browse_player_name_var.set(message)
        self.browse_player_time_var.set("0:00 / 0:00")
        self.browse_waveform_canvas.delete("all")

    def _load_browse_player(self, path: Path) -> None:
        self._browse_player_stop()
        self.browse_player_path = path
        self._browse_load_request_id += 1
        request_id = self._browse_load_request_id
        # read_wav_pcm_layout_fast() instead of two separate
        # read_wav_pcm_layout() calls (one via wav_duration_seconds(), one
        # for channels) that each walked the *entire* chunk list, including
        # seeking past the (often hundreds-of-MB) data payload to find any
        # chunk stored after it — on this app's network-mounted files, that
        # seek alone measured as real, visible per-click latency. The fast
        # version stops as soon as fmt+data are found.
        try:
            layout = read_wav_pcm_layout_fast(path)
            channels = layout["channels"]
            self.browse_player_duration = (layout["frames"] / layout["sample_rate"]) if layout["sample_rate"] else 0.0
        except Exception:
            channels = 1
            self.browse_player_duration = 0.0
        if channels > 1:
            # A genuine Poly source: one waveform lane per channel instead
            # of one combined line, so multi-mic/multi-track content is
            # actually legible rather than looking like a single blob.
            self.browse_player_peaks = []
            self.browse_player_channel_peaks = [[] for _ in range(channels)]
            self.browse_player_channel_labels = [f"Ch{index + 1}" for index in range(channels)]
            self._build_browse_solo_buttons(channels)
            bwfmetaedit = self.bwf_var.get().strip()
            start_worker = lambda: threading.Thread(target=self._load_browse_poly_details_worker, args=(request_id, path, channels, bwfmetaedit), daemon=True).start()
        else:
            # The peaks read (~0.2s+ for a large file, same as the poly
            # case) is likewise backgrounded — this and the poly branch
            # above were both the main source of a reported "clicking
            # through files feels laggy"; placeholders show immediately,
            # replaced once the real data is ready.
            self.browse_player_channel_peaks = []
            self.browse_player_channel_labels = []
            self.browse_player_peaks = []
            self._build_browse_solo_buttons(0)
            start_worker = lambda: threading.Thread(target=self._load_browse_mono_peaks_worker, args=(request_id, path), daemon=True).start()
        self.browse_play_button.configure(state=tk.NORMAL, text="▶ Play")
        self.browse_stop_button.configure(state=tk.DISABLED)
        self.browse_player_name_var.set(path.name)
        self._update_browse_player_time_label(0.0)
        self._draw_browse_waveform()
        # Deferred one idle-loop pass, after the placeholder UI above has
        # actually been drawn: CPython's GIL means a background thread's
        # CPU-bound work (the struct-unpacking in compute_wav_*_peaks) can
        # still stall this same synchronous tail if the thread starts
        # immediately before it — measured as the dominant remaining
        # source of per-click lag even though the heavy work itself was
        # already off the main thread.
        self.root.after_idle(start_worker)
        if self.browse_autoplay_var.get():
            self._browse_player_play()

    def _load_browse_poly_details_worker(self, request_id: int, path: Path, channels: int, bwfmetaedit: str) -> None:
        try:
            channel_peaks = compute_wav_channel_peaks(path)
        except Exception:
            channel_peaks = [[] for _ in range(channels)]
        labels = self._browse_channel_labels(bwfmetaedit, path, channels)
        # Tk's own after()/after_idle() aren't safe to call from a
        # background thread — this app's existing worker-thread pattern
        # (see the "progress"/"*_done" event kinds) already solves that via
        # a plain Queue drained on the main thread every 100ms; reused here
        # rather than inventing a second cross-thread mechanism.
        self.events.put(("browse_poly_details", (request_id, channel_peaks, labels)))

    def _apply_browse_poly_details(self, request_id: int, channel_peaks: list[list[float]], labels: list[str]) -> None:
        if request_id != self._browse_load_request_id:
            return  # a different file was selected while this was loading — discard
        self.browse_player_channel_peaks = channel_peaks
        self.browse_player_channel_labels = labels
        self._draw_browse_waveform(self._current_browse_playhead_elapsed())

    def _load_browse_mono_peaks_worker(self, request_id: int, path: Path) -> None:
        try:
            peaks = compute_wav_peaks(path)
        except Exception:
            peaks = []
        self.events.put(("browse_mono_peaks", (request_id, peaks)))

    def _apply_browse_mono_peaks(self, request_id: int, peaks: list[float]) -> None:
        if request_id != self._browse_load_request_id:
            return  # a different file was selected while this was loading — discard
        self.browse_player_peaks = peaks
        self._draw_browse_waveform(self._current_browse_playhead_elapsed())

    def _current_browse_playhead_elapsed(self) -> Optional[float]:
        """Current playhead position for a mid-playback redraw triggered by
        something other than the regular tick (a solo toggle, or the
        background peaks/labels load landing) — None while stopped, so the
        playhead line is simply omitted, matching the tick loop's own
        convention."""
        if self.browse_player_process is not None and not self.browse_player_paused:
            return self.browse_player_elapsed_base + (time.monotonic() - self.browse_player_start_wall)
        if self.browse_player_paused:
            return self.browse_player_elapsed_base
        return None

    def _browse_channel_labels(self, bwfmetaedit: str, path: Path, channels: int) -> list[str]:
        """iXML TRACK_LIST names, keyed by interleave (stream) index, with a
        plain "ChN" fallback for any channel the file's own metadata doesn't
        name — reuses `ixml_track_rows()`, the same lenient reader the
        Inspector's Poly Track List already relies on, rather than
        `poly_track_mapping()`, which raises on any inconsistency and is
        meant for pairing logic, not a best-effort display label. Takes
        `bwfmetaedit` explicitly (read from the StringVar by the caller on
        the main thread) since this runs off-thread — Tk variables aren't
        safe to touch from a background thread."""
        labels = [f"Ch{index + 1}" for index in range(channels)]
        if not bwfmetaedit:
            return labels
        try:
            ixml_text, _warning = export_optional_ixml_for_inspector(bwfmetaedit, path)
            if not ixml_text:
                return labels
            root = parse_source_ixml(ixml_text, path)
            for interleave_str, _channel_str, name in ixml_track_rows(root):
                if not name or name == "-" or not interleave_str.isdigit():
                    continue
                index = int(interleave_str) - 1
                if 0 <= index < channels:
                    labels[index] = name
        except Exception:
            pass
        return labels

    def _reset_browse_volume(self) -> None:
        self.browse_volume_db_var.set(0.0)
        self._on_browse_volume_changed()

    def _build_browse_solo_buttons(self, channel_count: int) -> None:
        """(Re)build the per-channel Solo toggles for a newly loaded file.
        Embedded directly on the waveform canvas via `create_window` so each
        one can be repositioned to track its lane's height, rather than a
        separate widget tree that would need to stay pixel-synced with it.

        When the new file has the exact same channel count as whatever was
        already showing (the common case while browsing a folder of
        same-format takes), the existing buttons are reused — just cleared
        back to un-soloed — instead of destroying and recreating N native
        widgets on every single click, which was part of the reported
        per-file lag."""
        canvas = self.browse_waveform_canvas
        if channel_count > 0 and len(self.browse_solo_vars) == channel_count:
            for var in self.browse_solo_vars:
                var.set(False)
            return
        for window_id in self.browse_solo_window_ids:
            widget_path = canvas.itemcget(window_id, "window")
            canvas.delete(window_id)
            if widget_path:
                canvas.nametowidget(widget_path).destroy()
        self.browse_solo_window_ids = []
        self.browse_solo_vars = []
        if channel_count <= 0:
            canvas.configure(height=110)
            return
        # A lane thin enough to fit the "compressed, fixed total height"
        # waveform (from the previous entry) is too small to click — Solo
        # controls need a floor of ~30px/channel (a Toolbutton's own natural
        # size) to actually be usable, so a high channel count now asks the
        # pane for more height than before rather than staying unclickable.
        canvas.configure(height=max(110, channel_count * 30))
        # Forces the geometry change through immediately so the height read
        # in _reposition_browse_solo_buttons() below (called synchronously,
        # not on a later <Configure>) reflects it, not the widget's old size.
        canvas.update_idletasks()
        for index in range(channel_count):
            var = tk.BooleanVar(value=False)
            # A plain tk.Checkbutton, not ttk — indicatoron=False plus
            # selectcolor gives an automatic, unmistakable on/off color
            # swap tied directly to the variable (no per-toggle restyling
            # code needed), which the ttk "Toolbutton" style did not make
            # visually obvious. command also redraws the waveform so the
            # soloed lane's color highlight (see _draw_browse_waveform_
            # lanes) updates in lockstep with the button.
            #
            # selectcolor ONLY — deliberately not also setting bg/fg/
            # activebackground/relief/bd/padx/highlightthickness. A real
            # bug was found the hard way: that fuller styling combination
            # (each option individually harmless, confirmed by testing
            # every one alone and in pairs) reproduced the exact same
            # "next Tk event-loop pass blocks for ~the remaining playback
            # duration while a sox CoreAudio stream is open" hang as the
            # earlier forced-canvas-window-resize bug, apparently from
            # costlier native widget compositing — 6/6 repeated real-file
            # runs confirmed selectcolor alone is stable, so it's the only
            # override kept.
            button = tk.Checkbutton(
                canvas, text="S", indicatoron=False, variable=var, takefocus=False,
                selectcolor="#22c55e",
                command=lambda ch=index: self._toggle_browse_solo_channel(ch),
            )
            # Deliberately no explicit width/height here (or in
            # _reposition_browse_solo_buttons below): forcing a canvas
            # window item to clip smaller than its natural size triggers a
            # confirmed real hang — while a sox CoreAudio stream is open,
            # the very next Tk event-loop pass blocks for roughly the
            # remaining playback duration. Only `coords()` (pure
            # repositioning, no resize) is safe here.
            window_id = canvas.create_window(2, 0, window=button, anchor="nw")
            self.browse_solo_vars.append(var)
            self.browse_solo_window_ids.append(window_id)
        self._reposition_browse_solo_buttons()

    def _reposition_browse_solo_buttons(self) -> None:
        if not self.browse_solo_window_ids:
            return
        canvas = self.browse_waveform_canvas
        height = canvas.winfo_height()
        width = canvas.winfo_width()
        channel_count = len(self.browse_solo_window_ids)
        if height <= 1 or width <= 1:
            return
        lane_height = height / channel_count
        # Right-aligned, clear of the track-name label at the lane's left
        # edge regardless of how long that name is. Position only (never an
        # explicit width/height on the window item — see the note in
        # _build_browse_solo_buttons about why that hangs).
        x = max(2, width - BROWSE_SOLO_BUTTON_WIDTH - 4)
        for index, window_id in enumerate(self.browse_solo_window_ids):
            lane_top = index * lane_height
            canvas.coords(window_id, x, lane_top + 1)

    def _browse_solo_channel_indices(self) -> list[int]:
        return [index for index, var in enumerate(self.browse_solo_vars) if var.get()]

    def _toggle_browse_solo_channel(self, channel_index: int) -> None:
        # The Checkbutton already flipped its own variable before this
        # command fires. In X-OR (exclusive) mode, soloing a channel drops
        # every other solo the same way Pro Tools' "Solo Mode: X-OR"
        # does — only meaningful when this click just turned the channel
        # ON; clicking an already-soloed channel off needs no such change.
        if self.browse_solo_exclusive_var.get() and self.browse_solo_vars[channel_index].get():
            for index, var in enumerate(self.browse_solo_vars):
                if index != channel_index:
                    var.set(False)
        # sox can't change a running process's channel selection, so a
        # solo toggle while playing restarts from the current position
        # exactly like a live volume change does.
        if self.browse_player_process is not None and not self.browse_player_paused:
            elapsed = self.browse_player_elapsed_base + (time.monotonic() - self.browse_player_start_wall)
            self._browse_player_start_from(elapsed)
        else:
            # start_from() above already redraws when actively playing;
            # when stopped/paused, nothing else would refresh the lane
            # highlight to match the new solo selection, so do it here.
            self._draw_browse_waveform(self._current_browse_playhead_elapsed())

    def _on_browse_waveform_canvas_configure(self, _event=None) -> None:
        self._reposition_browse_solo_buttons()
        # Passes the real current position rather than defaulting to None —
        # a resize mid-playback would otherwise blank the playhead (delete
        # it, since _update_browse_playhead(None) does) until the next
        # regular tick recreated it.
        self._draw_browse_waveform(self._current_browse_playhead_elapsed())

    def _browse_player_toggle_play_pause(self) -> None:
        if self.browse_player_process is None:
            self._browse_player_play()
        elif self.browse_player_paused:
            self._browse_player_resume()
        else:
            self._browse_player_pause()

    def _browse_player_play(self) -> None:
        self._browse_player_start_from(0.0)

    def _format_browse_volume_label(self, db: float) -> str:
        return browse_volume_label(db)

    def _browse_player_volume_effect_args(self) -> list[str]:
        return browse_volume_sox_args(self.browse_volume_db_var.get())

    def _browse_player_remix_effect_args(self) -> list[str]:
        soloed = self._browse_solo_channel_indices()
        if not soloed:
            return []
        # sox's `remix <a>,<b>,...` mixes those (1-based) input channels
        # down to a single output channel — exactly "solo these channels
        # together", with no need to pick a pan/output layout for them.
        return ["remix", ",".join(str(index + 1) for index in sorted(soloed))]

    def _on_browse_volume_changed(self, _value: str = "") -> None:
        self.browse_volume_label_var.set(self._format_browse_volume_label(self.browse_volume_db_var.get()))
        if self._browse_volume_restart_after is not None:
            self.root.after_cancel(self._browse_volume_restart_after)
            self._browse_volume_restart_after = None
        # Sox has no way to change a running process's gain, so a volume
        # drag while actively playing has to restart from the current
        # position with the new gain — debounced, so dragging the slider
        # doesn't spawn a fresh sox process on every pixel of motion.
        if self.browse_player_process is not None and not self.browse_player_paused:
            self._browse_volume_restart_after = self.root.after(150, self._apply_browse_volume_live)

    def _apply_browse_volume_live(self) -> None:
        self._browse_volume_restart_after = None
        if self.browse_player_process is not None and not self.browse_player_paused:
            elapsed = self.browse_player_elapsed_base + (time.monotonic() - self.browse_player_start_wall)
            self._browse_player_start_from(elapsed)

    def _on_browse_waveform_click(self, event: tk.Event) -> None:
        if self.browse_player_path is None or self.browse_player_duration <= 0:
            return
        width = self.browse_waveform_canvas.winfo_width()
        if width <= 0:
            return
        fraction = max(0.0, min(1.0, event.x / width))
        self._browse_player_start_from(fraction * self.browse_player_duration)

    def _browse_player_start_from(self, start_seconds: float) -> None:
        if self.browse_player_path is None:
            return
        if self.browse_player_duration:
            start_seconds = max(0.0, min(start_seconds, self.browse_player_duration))
        else:
            start_seconds = max(0.0, start_seconds)
        self._browse_player_kill_process()
        sox_path = self.sox_var.get().strip() or "sox"
        command = [sox_path, str(self.browse_player_path), "-d", "trim", f"{start_seconds:.3f}"]
        command.extend(self._browse_player_remix_effect_args())
        command.extend(self._browse_player_volume_effect_args())
        try:
            self.browse_player_process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self.status_var.set(f"Playback failed: {exc}")
            self.append_log(self.status_var.get())
            return
        self.browse_player_paused = False
        self.browse_player_elapsed_base = start_seconds
        self.browse_player_start_wall = time.monotonic()
        self.browse_play_button.configure(text="⏸ Pause")
        self.browse_stop_button.configure(state=tk.NORMAL)
        self._update_browse_player_time_label(start_seconds)
        self._draw_browse_waveform(start_seconds)
        self._start_browse_player_tick()

    def _browse_player_pause(self) -> None:
        if self.browse_player_process is None or self.browse_player_paused:
            return
        try:
            os.kill(self.browse_player_process.pid, signal.SIGSTOP)
        except ProcessLookupError:
            self._browse_player_stop()
            return
        self.browse_player_elapsed_base += time.monotonic() - self.browse_player_start_wall
        self.browse_player_paused = True
        self.browse_play_button.configure(text="▶ Play")
        if self._browse_player_tick_after is not None:
            self.root.after_cancel(self._browse_player_tick_after)
            self._browse_player_tick_after = None

    def _browse_player_resume(self) -> None:
        if self.browse_player_process is None or not self.browse_player_paused:
            return
        try:
            os.kill(self.browse_player_process.pid, signal.SIGCONT)
        except ProcessLookupError:
            self._browse_player_stop()
            return
        self.browse_player_paused = False
        self.browse_player_start_wall = time.monotonic()
        self.browse_play_button.configure(text="⏸ Pause")
        self._start_browse_player_tick()

    def _browse_player_kill_process(self) -> None:
        if self.browse_player_process is not None:
            process = self.browse_player_process
            try:
                if self.browse_player_paused:
                    # A stopped (SIGSTOP'd) process can't act on SIGTERM at
                    # all until it's running again — resume it first or
                    # terminate() below would just leave a zombie forever.
                    os.kill(process.pid, signal.SIGCONT)
                process.terminate()
            except ProcessLookupError:
                pass
            # wait() reaps the child immediately instead of leaving a
            # zombie around until this app process eventually exits —
            # bounded and short (sox normally exits well under this on
            # SIGTERM), with a hard kill() as a last resort.
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=1.0)
                except Exception:
                    pass
            self.browse_player_process = None
        if self._browse_player_tick_after is not None:
            self.root.after_cancel(self._browse_player_tick_after)
            self._browse_player_tick_after = None

    def _browse_player_stop(self) -> None:
        if not hasattr(self, "browse_player_process"):
            return  # called from _update_mode_ui() before the player UI exists yet
        self._browse_player_kill_process()
        self.browse_player_paused = False
        self.browse_player_elapsed_base = 0.0
        if hasattr(self, "browse_play_button"):
            self.browse_play_button.configure(text="▶ Play", state=tk.NORMAL if self.browse_player_path is not None else tk.DISABLED)
            self.browse_stop_button.configure(state=tk.DISABLED)
            self._update_browse_player_time_label(0.0)
            self._draw_browse_waveform()

    def _start_browse_player_tick(self) -> None:
        self._browse_player_tick_after = self.root.after(50, self._browse_player_tick)

    def _browse_player_tick(self) -> None:
        self._browse_player_tick_after = None
        if self.browse_player_process is None or self.browse_player_paused:
            return
        if self.browse_player_process.poll() is not None:
            self._browse_player_stop()
            return
        elapsed = self.browse_player_elapsed_base + (time.monotonic() - self.browse_player_start_wall)
        if self.browse_player_duration and elapsed >= self.browse_player_duration:
            self._browse_player_stop()
            return
        self._update_browse_player_time_label(elapsed)
        self._update_browse_playhead(elapsed)
        self._start_browse_player_tick()

    def _update_browse_player_time_label(self, elapsed: float) -> None:
        def fmt(seconds: float) -> str:
            seconds = max(0, int(seconds))
            return f"{seconds // 60}:{seconds % 60:02d}"

        self.browse_player_time_var.set(f"{fmt(elapsed)} / {fmt(self.browse_player_duration)}")

    def _draw_browse_waveform(self, elapsed: Optional[float] = None) -> None:
        """Full redraw: waveform content (bars/lanes/labels) plus the
        playhead. Cheap enough for a one-off (file load, solo toggle,
        canvas resize) but NOT for the once-per-tick case while playing —
        see _update_browse_playhead(), which the tick loop uses instead so
        it isn't deleting and recreating potentially thousands of bar items
        just to move one line 20 times a second. That redraw churn was
        measured as the real cause of a reported "playhead jitters, feels
        laggy" — visually distinct from (and unrelated to) the per-click
        selection lag fixed in the previous entry."""
        self._redraw_browse_waveform_content()
        self._update_browse_playhead(elapsed)

    def _redraw_browse_waveform_content(self) -> None:
        canvas = self.browse_waveform_canvas
        # Only the "wave" tag — the Solo buttons (real embedded windows)
        # and the playhead (its own persistent item, moved via coords()
        # rather than recreated) must both survive this.
        canvas.delete("wave")
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        if self.browse_player_channel_peaks:
            self._draw_browse_waveform_lanes(canvas, width, height)
        else:
            mid_y = height / 2
            peaks = self.browse_player_peaks
            if peaks:
                step = width / len(peaks)
                for index, peak in enumerate(peaks):
                    x = index * step
                    bar_height = max(1.0, peak * (height - 4))
                    canvas.create_line(x, mid_y - bar_height / 2, x, mid_y + bar_height / 2, fill="#5fb3ff", tags="wave")
            else:
                canvas.create_line(0, mid_y, width, mid_y, fill="#3a3f47", tags="wave")
        # The wave content was just recreated on top of everything else on
        # the canvas (new items stack above older ones) — re-raise the
        # persistent playhead line so it stays visible above the bars.
        canvas.tag_raise("playhead")

    def _update_browse_playhead(self, elapsed: Optional[float]) -> None:
        """Moves the single persistent playhead line via coords() instead
        of deleting and recreating it (and everything else tagged "wave")
        on every tick — the tick loop calls only this, at 20Hz, while a
        full _draw_browse_waveform() stays reserved for whenever the
        waveform content itself actually changes."""
        canvas = self.browse_waveform_canvas
        if elapsed is None or not self.browse_player_duration:
            if self.browse_playhead_item_id is not None:
                canvas.delete(self.browse_playhead_item_id)
                self.browse_playhead_item_id = None
            return
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        playhead_x = min(width, (elapsed / self.browse_player_duration) * width)
        if self.browse_playhead_item_id is not None:
            canvas.coords(self.browse_playhead_item_id, playhead_x, 0, playhead_x, height)
        else:
            self.browse_playhead_item_id = canvas.create_line(playhead_x, 0, playhead_x, height, fill="#ffffff", tags="playhead")

    def _draw_browse_waveform_lanes(self, canvas: tk.Canvas, width: int, height: int) -> None:
        """One waveform lane per channel, each with its own Solo toggle
        (a real embedded widget, positioned by `_reposition_browse_solo_
        buttons`, not drawn here) right-aligned so it never overlaps the
        track-name label at the lane's left edge. A soloed lane's waveform
        and label turn bright green (matching the Solo button's own
        selectcolor) and every non-soloed lane dims, so which channel(s)
        are actually audible is obvious at a glance rather than only
        readable from the small button state."""
        channel_peaks = self.browse_player_channel_peaks
        channel_count = len(channel_peaks)
        if channel_count == 0:
            return
        labels = self.browse_player_channel_labels
        soloed = set(self._browse_solo_channel_indices())
        any_solo = bool(soloed)
        lane_height = height / channel_count
        font_size = max(6, min(9, int(lane_height) - 2))
        show_labels = lane_height >= 8
        label_x = 4
        for lane_index, peaks in enumerate(channel_peaks):
            lane_top = lane_index * lane_height
            lane_mid = lane_top + lane_height / 2
            is_soloed = lane_index in soloed
            if is_soloed:
                wave_color, label_color = "#22c55e", "#22c55e"
                canvas.create_rectangle(0, lane_top, width, lane_top + lane_height, fill="#173a26", outline="", tags="wave")
            elif any_solo:
                wave_color, label_color = "#3d434c", "#5b6472"
            else:
                wave_color, label_color = "#5fb3ff", "#9fb4c7"
            if peaks:
                step = width / len(peaks)
                bar_max = max(1.0, lane_height - 2)
                for index, peak in enumerate(peaks):
                    x = index * step
                    bar_height = max(1.0, peak * bar_max)
                    canvas.create_line(x, lane_mid - bar_height / 2, x, lane_mid + bar_height / 2, fill=wave_color, tags="wave")
            else:
                canvas.create_line(0, lane_mid, width, lane_mid, fill="#3a3f47", tags="wave")
            if lane_index > 0:
                canvas.create_line(0, lane_top, width, lane_top, fill="#2a2f36", tags="wave")
            if show_labels:
                label = labels[lane_index] if lane_index < len(labels) else f"Ch{lane_index + 1}"
                canvas.create_text(label_x, lane_top + 1, anchor="nw", text=label, fill=label_color, font=("SF Mono", font_size), tags="wave")

    def _populate_browse_tree_roots(self) -> None:
        home = Path.home()
        self._add_browse_tree_folder("", home, f"🏠 {home.name}")
        volumes_root = Path("/Volumes")
        if volumes_root.is_dir():
            try:
                volumes = sorted((path for path in volumes_root.iterdir() if path.is_dir()), key=lambda path: path.name.casefold())
            except Exception:
                volumes = []
            for volume in volumes:
                self._add_browse_tree_folder("", volume, f"\U0001F4BE {volume.name}")

    def _add_browse_tree_folder(self, parent_iid: str, path: Path, label: str) -> str:
        iid = self.browse_folder_tree.insert(parent_iid, tk.END, text=label, values=(str(path),))
        # A placeholder child is what gives a never-yet-expanded folder its
        # expand triangle at all — real children only get read from disk on
        # <<TreeviewOpen>>, so opening a folder with thousands of
        # subfolders elsewhere in the tree costs nothing until it's
        # actually clicked open.
        self.browse_folder_tree.insert(iid, tk.END, text="", tags=("placeholder",))
        return iid

    def _on_browse_tree_open(self, _event=None) -> None:
        iid = self.browse_folder_tree.focus()
        if iid:
            self._populate_browse_tree_children(iid)

    def _populate_browse_tree_children(self, iid: str) -> None:
        children = self.browse_folder_tree.get_children(iid)
        if not children or "placeholder" not in self.browse_folder_tree.item(children[0], "tags"):
            return  # already populated (or already showed an error) — don't re-scan on every re-open
        self.browse_folder_tree.delete(*children)
        folder = Path(self.browse_folder_tree.item(iid, "values")[0])
        try:
            subfolders = sorted(
                (path for path in folder.iterdir() if path.is_dir() and not path.name.startswith(".")),
                key=lambda path: path.name.casefold(),
            )
        except Exception as exc:
            self.browse_folder_tree.insert(iid, tk.END, text=f"(cannot read: {exc})", tags=("error",))
            return
        for subfolder in subfolders:
            self._add_browse_tree_folder(iid, subfolder, subfolder.name)

    def _on_browse_tree_select(self, _event=None) -> None:
        selected = self.browse_folder_tree.selection()
        if not selected:
            return
        values = self.browse_folder_tree.item(selected[0], "values")
        if values:
            self._populate_browse_file_list(Path(values[0]))

    def _on_browse_tree_enter(self, _event=None) -> None:
        selected = self.browse_folder_tree.selection()
        if not selected:
            return
        values = self.browse_folder_tree.item(selected[0], "values")
        if not values:
            return
        folder = Path(values[0])
        self.status_var.set(f"Scanning {folder} and its subfolders...")
        self.append_log(self.status_var.get())
        self.root.update_idletasks()
        self._populate_browse_file_list(folder, recursive=True)
        self.status_var.set(self.browse_file_count_var.get())
        self.append_log(self.status_var.get())

    def _populate_browse_file_list(self, folder: Path, recursive: bool = False) -> None:
        self.browse_file_list.delete(*self.browse_file_list.get_children())
        self.browse_file_list_paths.clear()
        try:
            if recursive:
                candidates = folder.rglob("*")
            else:
                candidates = folder.iterdir()
            wav_files_here = sorted(
                (path for path in candidates if path.is_file() and path.suffix.lower() == ".wav"),
                key=lambda path: str(path.relative_to(folder)).casefold() if recursive else path.name.casefold(),
            )
        except Exception as exc:
            self.browse_file_count_var.set(f"Cannot read folder: {exc}")
            return
        path_labels = []
        for path in wav_files_here:
            # A recursive listing's Path column shows the subfolder chain
            # relative to the folder that was opened (blank at that
            # folder's own top level) — recorder files from different
            # dates/takes routinely reuse the same filename across sibling
            # folders, so the bare name alone would make two entirely
            # different files look identical. Filename is its own column
            # (and the stretch one) specifically so it's never truncated
            # or scrolled out of view just because Path happens to be long.
            if recursive:
                relative_parent = path.relative_to(folder).parent
                path_label = "" if str(relative_parent) == "." else str(relative_parent)
            else:
                path_label = ""
            path_labels.append(path_label)
            iid = self.browse_file_list.insert("", tk.END, values=(path.name, path_label))
            self.browse_file_list_paths[iid] = path
        self._auto_size_browse_path_column(path_labels)
        scope = " (including subfolders)" if recursive else ""
        self.browse_file_count_var.set(f"{len(wav_files_here)} WAV file(s) in {folder.name}{scope}")

    def _auto_size_browse_path_column(self, path_labels: list[str]) -> None:
        """Widens the Path column to fit whatever's actually in it for this
        folder (a recursive listing's subfolder chains vary a lot in
        length), with a higher floor than before so it doesn't look
        cramped even when every entry is blank (a plain, non-recursive
        listing)."""
        font = tkfont.nametofont("TkDefaultFont")
        widest_content = max((font.measure(label) for label in path_labels), default=0)
        width = max(260, widest_content + 24)
        width = min(width, 640)  # one absurdly deep subfolder chain shouldn't blow out the whole pane
        self.browse_file_list.column("path", width=width)

    def _on_browse_file_select(self, _event=None) -> None:
        # Selecting a file no longer auto-opens the Inspector (it used to,
        # Finder+Quick-Look style) — clicking through a folder to just
        # listen shouldn't also pop a window open every time. If the
        # Inspector happens to already be open, keep refreshing it in
        # place so it still tracks the current selection for anyone who
        # deliberately opened it.
        selected = self.browse_file_list.selection()
        if not selected:
            return
        path = self.browse_file_list_paths.get(selected[-1])
        if path is None:
            return
        if self.inspector is not None and self.inspector_window is not None and self.inspector_window.winfo_exists() and self.inspector_window.state() != "withdrawn":
            self.inspector.load_job(Job(path, None, 0, None, None, "Ready to browse", "browse"))
        self._load_browse_player(path)

    def _switch_to_mode(self, mode_value: str) -> None:
        self.mode_var.set(mode_value)
        self._update_mode_ui()

    def _invoke_button_if_enabled(self, button: ttk.Button) -> None:
        if str(button["state"]) != "disabled":
            button.invoke()

    def _build_keybinding_handlers(self) -> dict[str, object]:
        return {
            "mode_browse": lambda: self._switch_to_mode("Browse"),
            "mode_split": lambda: self._switch_to_mode("Split Poly with SoX + Embed"),
            "mode_reembed": lambda: self._switch_to_mode("Re-embed Matched Render Files"),
            "mode_rename": lambda: self._switch_to_mode("Rename"),
            "mode_combine": lambda: self._switch_to_mode("Combine / Merge Mono Files to Poly"),
            "mode_qc": lambda: self._switch_to_mode("QC"),
            "mode_settings": lambda: self._switch_to_mode("Settings"),
            "open_inspector": self._toggle_inspector,
            "toggle_log": self._toggle_log_window,
            "preview": lambda: self._invoke_button_if_enabled(self.preview_button),
            "execute": lambda: self._invoke_button_if_enabled(self.execute_button),
            "compare": lambda: self._invoke_button_if_enabled(self.qc_auto_compare_button),
            "browse_recurse": self._on_browse_tree_enter,
            "browse_play_pause": self._browse_player_toggle_play_pause,
        }

    @staticmethod
    def _keybinding_tk_sequences(sequence: str) -> list[str]:
        """A stored keybinding value is either an already-bracketed Tk
        sequence ("<Command-Key-1>", for the "global" scope) or a bare key
        name ("t", "Return", for a widget-scoped action). A bare single
        letter binds both its lower and upper case (Shift) forms, so "T"
        keeps working exactly as it always has, whichever case is typed;
        this is the only place that distinction is handled, so every
        caller can just deal in the one configured value."""
        if not sequence:
            return []
        if sequence.startswith("<"):
            return [sequence]
        if len(sequence) == 1 and sequence.isalpha():
            return [f"<KeyPress-{sequence.lower()}>", f"<KeyPress-{sequence.upper()}>"]
        return [f"<KeyPress-{sequence}>"]

    def _apply_keybinding(self, action_id: str) -> None:
        scope = KEYBINDING_SCOPES[action_id]
        handler = self._keybinding_handlers[action_id]
        sequence = self.keybindings.get(action_id, "")
        old_sequence = self._bound_keybinding_sequences.get(action_id, "")
        if scope == "global":
            bind_unbind_pairs = [(self.root.bind_all, self.root.unbind_all)]
        else:
            widget_names = scope if isinstance(scope, tuple) else (scope,)
            targets = [getattr(self, name) for name in widget_names]
            bind_unbind_pairs = [(target.bind, target.unbind) for target in targets]
        for bind, unbind in bind_unbind_pairs:
            for tk_sequence in self._keybinding_tk_sequences(old_sequence):
                try:
                    unbind(tk_sequence)
                except tk.TclError:
                    pass
            for tk_sequence in self._keybinding_tk_sequences(sequence):
                bind(tk_sequence, lambda _event, fn=handler: fn())
        self._bound_keybinding_sequences[action_id] = sequence

    def _apply_all_keybindings(self) -> None:
        self._keybinding_handlers = self._build_keybinding_handlers()
        for action_id, _label, _default, _scope in KEYBINDING_ACTIONS:
            self._apply_keybinding(action_id)

    def _build_settings_ui(self) -> None:
        """Build the app-wide tool settings page.

        The StringVars deliberately remain the existing in-memory settings: the
        application has no settings-file persistence to preserve or replace.
        """
        self.settings_frame = ttk.Frame(self.root, padding=12)
        tools = ttk.LabelFrame(self.settings_frame, text="Tools", padding=10)
        tools.pack(fill=tk.X, anchor="n")
        ttk.Label(
            tools,
            text="These paths apply to every workflow immediately. They use the existing startup detection and are not saved between launches.",
            wraplength=900,
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._path_row(tools, 1, "SoX", self.sox_var, self.choose_sox, file=True)
        self._path_row(tools, 2, "Soxi", self.soxi_var, self.choose_soxi, file=True)
        self._path_row(tools, 3, "BWF MetaEdit", self.bwf_var, self.choose_bwf, file=True)
        actions = ttk.Frame(tools)
        actions.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(actions, text="Check Tools", command=self.check_tools).pack(side=tk.LEFT)
        ttk.Button(actions, text="Open Terminal", command=self.open_install_command).pack(side=tk.LEFT, padx=(8, 0))
        self.tool_check_var = tk.StringVar(value="Click Check Tools to show each tool's path and version.")
        ttk.Label(
            tools,
            textvariable=self.tool_check_var,
            wraplength=900,
            justify=tk.LEFT,
            font=("SF Mono", 10),
        ).grid(row=5, column=0, columnspan=6, sticky="w", pady=(10, 0))

        presets = ttk.LabelFrame(self.settings_frame, text="Filename Formula Presets", padding=10)
        presets.pack(fill=tk.X, anchor="n", pady=(12, 0))
        ttk.Label(
            presets,
            text="Shown in the dropdown next to every Output name / Source formula / Target formula field (Split, Re-embed). Shared across all of them — edit once here.",
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 8))
        preset_list_row = ttk.Frame(presets)
        preset_list_row.pack(fill=tk.X)
        self.formula_preset_listbox = tk.Listbox(preset_list_row, height=5, bg="white", fg="black", exportselection=False)
        self.formula_preset_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        for preset in self.formula_presets:
            self.formula_preset_listbox.insert(tk.END, preset)
        preset_buttons = ttk.Frame(preset_list_row)
        preset_buttons.pack(side=tk.LEFT, padx=(8, 0), fill=tk.Y)
        ttk.Button(preset_buttons, text="Move Up", command=lambda: self._move_formula_preset(-1)).pack(fill=tk.X)
        ttk.Button(preset_buttons, text="Move Down", command=lambda: self._move_formula_preset(1)).pack(fill=tk.X, pady=(6, 0))
        ttk.Button(preset_buttons, text="Remove Selected", command=self._remove_formula_preset).pack(fill=tk.X, pady=(6, 0))
        ttk.Button(preset_buttons, text="Restore Defaults", command=self._restore_formula_presets).pack(fill=tk.X, pady=(6, 0))
        preset_add_row = ttk.Frame(presets)
        preset_add_row.pack(fill=tk.X, pady=(8, 0))
        self.new_formula_preset_var = tk.StringVar()
        tk.Entry(preset_add_row, textvariable=self.new_formula_preset_var, width=30, bg="white", fg="black", insertbackground="black").pack(side=tk.LEFT)
        ttk.Button(preset_add_row, text="Add", command=self._add_formula_preset).pack(side=tk.LEFT, padx=(8, 0))

        self._build_keybindings_settings_ui(self.settings_frame)

    def _build_keybindings_settings_ui(self, parent: ttk.Frame) -> None:
        # Fixed by the script, not user-editable — this list is read-only,
        # here so every shortcut is still discoverable without having to
        # go hunting through the code for it.
        shortcuts = ttk.LabelFrame(parent, text="Keyboard Shortcuts", padding=10)
        shortcuts.pack(fill=tk.X, anchor="n", pady=(12, 0))
        ttk.Label(
            shortcuts,
            text="Fixed by the app, not user-editable. Mode-switch and action shortcuts always require a modifier so they never get typed into a text field by accident; the two Browse-only ones don't, since that list and tree never hold any text input. Audio Inspector and the Execution Log are separate windows, so their shortcuts toggle — press again to hide.",
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 8))
        # A fixed-height scrollable list, not just a plain grid of rows —
        # the Settings page itself doesn't scroll, so once there are more
        # shortcuts than fit (or the window is short), rows below would
        # otherwise be cut off with no way to reach them at all.
        list_frame = ttk.Frame(shortcuts)
        list_frame.pack(fill=tk.BOTH, expand=True)
        canvas_background = ttk.Style().lookup("TFrame", "background") or self.root.cget("background")
        canvas = tk.Canvas(list_frame, height=220, highlightthickness=0, background=canvas_background)
        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.LEFT, fill=tk.Y)
        rows_frame = ttk.Frame(canvas)
        rows_window = canvas.create_window((0, 0), window=rows_frame, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(rows_window, width=event.width))
        rows_frame.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))

        def on_mouse_wheel(event: tk.Event) -> str:
            canvas.yview_scroll(-int(event.delta / 120), "units")
            return "break"

        for widget in (canvas, rows_frame):
            widget.bind("<MouseWheel>", on_mouse_wheel)

        for row, (action_id, label, _default, _scope) in enumerate(KEYBINDING_ACTIONS):
            ttk.Label(rows_frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Label(
                rows_frame,
                text=keybinding_display_label(self.keybindings.get(action_id, "")),
                font=("SF Mono", 11, "bold"),
            ).grid(row=row, column=1, sticky="w", padx=(12, 0))
            row_widget = rows_frame.grid_slaves(row=row, column=0)[0]
            row_widget.bind("<MouseWheel>", on_mouse_wheel)
        rows_frame.columnconfigure(0, weight=1)

    def _build_qc_ui(self) -> None:
        self.qc_frame = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        self.qc_input_picker = QcInputPicker(
            self.qc_frame,
            self.qc_input_source_var,
            self.qc_input_target_var,
            self._choose_qc_input_file,
            self._choose_qc_input_folder,
            self._set_qc_input,
        )
        self.qc_input_picker.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(
            self.qc_frame,
            text="Pairing (folders): same file name first, then name prefix + recorder channel, either direction. "
            "Poly ↔ Split: a Split belongs to the Poly whose name starts its name. No formula needed.",
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 8))
        qc_actions = ttk.Frame(self.qc_frame)
        qc_actions.pack(fill=tk.X, pady=(0, 6))
        self.qc_auto_compare_button = ttk.Button(qc_actions, text="Compare", command=self.compare_qc_inputs)
        self.qc_auto_compare_button.pack(side=tk.LEFT)
        ttk.Checkbutton(qc_actions, text="Audio", variable=self.qc_compare_audio_var).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Checkbutton(qc_actions, text="Metadata", variable=self.qc_compare_metadata_var).pack(side=tk.LEFT, padx=(8, 0))

        self.qc_file_frame = ttk.Frame(self.qc_frame)
        inputs = ttk.Frame(self.qc_file_frame)
        inputs.pack(fill=tk.X)
        for column, (title, variable, browse_command, drop_command) in enumerate((
            ("SOURCE", self.qc_source_var, self.choose_qc_source, self.drop_qc_source),
            ("TARGET", self.qc_target_var, self.choose_qc_target, self.drop_qc_target),
        )):
            side = ttk.LabelFrame(inputs, text=title, padding=8)
            side.grid(row=0, column=column, sticky="nsew", padx=(0, 4) if column == 0 else (4, 0))
            ttk.Button(side, text="Load WAV", command=browse_command).pack(anchor="w")
            ttk.Label(side, textvariable=variable, wraplength=520, justify=tk.LEFT).pack(anchor="w", fill=tk.X, pady=(6, 0))
            for widget in (side, *side.winfo_children()):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", drop_command)
            inputs.columnconfigure(column, weight=1, uniform="qc-input")
        qc_file_actions = ttk.Frame(self.qc_file_frame)
        qc_file_actions.pack(anchor="w", pady=(10, 6))
        self.qc_file_compare_button = ttk.Button(qc_file_actions, text="Compare", command=self.compare_qc_files)
        self.qc_file_compare_button.pack(side=tk.LEFT)
        inputs.pack_forget()
        qc_file_actions.pack_forget()
        # This row is deliberately outside qc_file_actions: that row is
        # hidden above (the manual per-type Compare button is superseded by
        # the always-visible auto-detect Compare), but Update Metadata must
        # stay visible whenever this type is showing, from either flow.
        qc_file_update_row = ttk.Frame(self.qc_file_frame)
        qc_file_update_row.pack(anchor="w", pady=(0, 6))
        self.qc_file_update_button = ttk.Button(qc_file_update_row, text="Update Metadata (Source → Target)", command=self.update_qc_file_metadata, state=tk.DISABLED)
        self.qc_file_update_button.pack(side=tk.LEFT)
        qc_table_frame = ttk.Frame(self.qc_file_frame)
        qc_table_frame.pack(fill=tk.BOTH, expand=True)
        self.qc_table = ttk.Treeview(qc_table_frame, columns=("field", "source", "target", "result"), show="headings")
        for column, heading in (("field", ""), ("source", "SOURCE"), ("target", "TARGET"), ("result", "RESULT")):
            self.qc_table.heading(column, text=heading)
        configure_treeview_column_widths(
            self.qc_table,
            (("field", 180, True), ("source", 360, True), ("target", 360, True), ("result", 100, False)),
            stretch_column="target",
        )
        self.qc_table.grid(row=0, column=0, sticky="nsew")
        self.qc_table.bind("<<TreeviewSelect>>", self._on_qc_file_select)
        self._bind_context_menu(self.qc_table, self._on_qc_file_context_menu)
        self.qc_scrollbar = ttk.Scrollbar(qc_table_frame, orient=tk.VERTICAL, command=self.qc_table.yview)
        self.qc_scrollbar.grid(row=0, column=1, sticky="ns")
        self.qc_table.configure(yscrollcommand=self.qc_scrollbar.set)
        qc_table_frame.rowconfigure(0, weight=1)
        qc_table_frame.columnconfigure(0, weight=1)
        self.qc_table.tag_configure("section", background="#e5e7eb", font=("TkDefaultFont", 11, "bold"))
        self.qc_table_sorter = TreeviewSorter(
            self.qc_table,
            {"field": "", "source": "SOURCE", "target": "TARGET", "result": "RESULT"},
            pinned_tags={"section"},
            column_groups={"source": ("source", "target", "field"), "target": ("source", "target", "field")},
        )

        self.qc_folder_frame = ttk.Frame(self.qc_frame)
        folder_inputs = ttk.Frame(self.qc_folder_frame)
        folder_inputs.pack(fill=tk.X)
        for column, (title, variable, browse_command, drop_command) in enumerate((
            ("SOURCE FOLDER", self.qc_source_folder_var, self.choose_qc_source_folder, self.drop_qc_source_folder),
            ("TARGET FOLDER", self.qc_target_folder_var, self.choose_qc_target_folder, self.drop_qc_target_folder),
        )):
            side = ttk.LabelFrame(folder_inputs, text=title, padding=8)
            side.grid(row=0, column=column, sticky="nsew", padx=(0, 4) if column == 0 else (4, 0))
            ttk.Button(side, text="Select Folder", command=browse_command).pack(anchor="w")
            ttk.Label(side, textvariable=variable, wraplength=520, justify=tk.LEFT).pack(anchor="w", fill=tk.X, pady=(6, 0))
            for widget in (side, *side.winfo_children()):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", drop_command)
            folder_inputs.columnconfigure(column, weight=1, uniform="qc-folder-input")
        qc_folder_actions = ttk.Frame(self.qc_folder_frame)
        qc_folder_actions.pack(anchor="w", pady=(10, 6))
        self.qc_folder_compare_button = ttk.Button(qc_folder_actions, text="Compare", command=self.compare_qc_folders)
        self.qc_folder_compare_button.pack(side=tk.LEFT)
        folder_inputs.pack_forget()
        qc_folder_actions.pack_forget()
        qc_folder_update_row = ttk.Frame(self.qc_folder_frame)
        qc_folder_update_row.pack(anchor="w", pady=(0, 6))
        self.qc_folder_update_selected_button = ttk.Button(qc_folder_update_row, text="Update Metadata to Selected", command=lambda: self.update_qc_folder_metadata("selected"), state=tk.DISABLED)
        self.qc_folder_update_selected_button.pack(side=tk.LEFT)
        self.qc_folder_update_problems_button = ttk.Button(qc_folder_update_row, text="Update Metadata to Problems", command=lambda: self.update_qc_folder_metadata("problems"), state=tk.DISABLED)
        self.qc_folder_update_problems_button.pack(side=tk.LEFT, padx=(8, 0))
        qc_folder_table_frame = ttk.Frame(self.qc_folder_frame)
        qc_folder_table_frame.pack(fill=tk.BOTH, expand=True)
        self.qc_folder_table = ttk.Treeview(qc_folder_table_frame, columns=("filename", "source", "target", "audio_result", "metadata_result"), show="headings")
        for column, heading in (
            ("filename", "FILE NAME"), ("source", "SOURCE"), ("target", "TARGET"),
            ("audio_result", "AUDIO RESULT"), ("metadata_result", "METADATA RESULT"),
        ):
            self.qc_folder_table.heading(column, text=heading)
        configure_treeview_column_widths(
            self.qc_folder_table,
            (("filename", 380, True), ("source", 90, False), ("target", 90, False), ("audio_result", 150, False), ("metadata_result", 150, False)),
            stretch_column="filename",
        )
        self.qc_folder_table.grid(row=0, column=0, sticky="nsew")
        self.qc_folder_table.bind("<<TreeviewSelect>>", self._on_qc_folder_select)
        self._bind_context_menu(self.qc_folder_table, self._on_qc_folder_context_menu)
        self.qc_folder_scrollbar = ttk.Scrollbar(qc_folder_table_frame, orient=tk.VERTICAL, command=self.qc_folder_table.yview)
        self.qc_folder_scrollbar.grid(row=0, column=1, sticky="ns")
        self.qc_folder_table.configure(yscrollcommand=self.qc_folder_scrollbar.set)
        qc_folder_table_frame.rowconfigure(0, weight=1)
        qc_folder_table_frame.columnconfigure(0, weight=1)
        self.qc_folder_table.tag_configure("summary", background="#e5e7eb", font=("TkDefaultFont", 11, "bold"))
        self.qc_folder_table_sorter = TreeviewSorter(
            self.qc_folder_table,
            {"filename": "FILE NAME", "source": "SOURCE", "target": "TARGET", "audio_result": "AUDIO RESULT", "metadata_result": "METADATA RESULT"},
            pinned_tags={"summary"},
            column_groups={"source": ("source", "target", "filename"), "target": ("source", "target", "filename")},
        )

        self.qc_poly_frame = ttk.Frame(self.qc_frame)
        poly_inputs = ttk.Frame(self.qc_poly_frame)
        poly_inputs.pack(fill=tk.X)
        for column, (title, variable, browse_command, drop_command) in enumerate((
            ("SOURCE FOLDER", self.qc_poly_source_folder_var, self.choose_qc_poly_source_folder, self.drop_qc_poly_source_folder),
            ("TARGET FOLDER", self.qc_poly_target_folder_var, self.choose_qc_poly_target_folder, self.drop_qc_poly_target_folder),
        )):
            side = ttk.LabelFrame(poly_inputs, text=title, padding=8)
            side.grid(row=0, column=column, sticky="nsew", padx=(0, 4) if column == 0 else (4, 0))
            ttk.Button(side, text="Select Folder", command=browse_command).pack(anchor="w")
            ttk.Label(side, textvariable=variable, wraplength=520, justify=tk.LEFT).pack(anchor="w", fill=tk.X, pady=(6, 0))
            for widget in (side, *side.winfo_children()):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", drop_command)
            poly_inputs.columnconfigure(column, weight=1, uniform="qc-poly-input")
        poly_actions = ttk.Frame(self.qc_poly_frame)
        poly_actions.pack(anchor="w", pady=(10, 6))
        self.qc_poly_compare_button = ttk.Button(poly_actions, text="Compare", command=self.compare_qc_poly_folders)
        self.qc_poly_compare_button.pack(side=tk.LEFT)
        poly_inputs.pack_forget()
        poly_actions.pack_forget()
        # Kept outside poly_actions (hidden above) so these two stay usable
        # regardless of whether Poly ↔ Split was reached via auto-detect or
        # this tab's own (currently unused) Compare button. There is no
        # QC-only "Inspector" button here — the shared "Audio Inspector"
        # button in the top toolbar already loads whatever row is selected
        # in whichever QC table is active, so a second button here would
        # just duplicate it.
        qc_poly_update_row = ttk.Frame(self.qc_poly_frame)
        qc_poly_update_row.pack(anchor="w", pady=(0, 6))
        self.qc_poly_update_selected_button = ttk.Button(qc_poly_update_row, text="Update Metadata to Selected", command=lambda: self.update_qc_poly_metadata("selected"), state=tk.DISABLED)
        self.qc_poly_update_selected_button.pack(side=tk.LEFT, padx=(8, 0))
        self.qc_poly_update_problems_button = ttk.Button(qc_poly_update_row, text="Update Metadata to Problems", command=lambda: self.update_qc_poly_metadata("problems"), state=tk.DISABLED)
        self.qc_poly_update_problems_button.pack(side=tk.LEFT, padx=(8, 0))
        qc_poly_table_frame = ttk.Frame(self.qc_poly_frame)
        qc_poly_table_frame.pack(fill=tk.BOTH, expand=True)
        self.qc_poly_table = ttk.Treeview(qc_poly_table_frame, columns=("poly", "stream", "channel", "split", "frames", "audio_result", "metadata_result"), show="headings")
        for column, heading in (
            ("poly", "POLY FILE"), ("stream", "STREAM"), ("channel", "RECORDER CH"), ("split", "SPLIT FILE"),
            ("frames", "FRAMES"), ("audio_result", "AUDIO RESULT"), ("metadata_result", "METADATA RESULT"),
        ):
            self.qc_poly_table.heading(column, text=heading)
        configure_treeview_column_widths(
            self.qc_poly_table,
            (
                ("poly", 260, True), ("stream", 70, False), ("channel", 100, False),
                ("split", 260, True), ("frames", 90, False), ("audio_result", 140, False), ("metadata_result", 150, False),
            ),
            stretch_column="split",
        )
        self.qc_poly_table.grid(row=0, column=0, sticky="nsew")
        self.qc_poly_scrollbar = ttk.Scrollbar(qc_poly_table_frame, orient=tk.VERTICAL, command=self.qc_poly_table.yview)
        self.qc_poly_scrollbar.grid(row=0, column=1, sticky="ns")
        self.qc_poly_table.configure(yscrollcommand=self.qc_poly_scrollbar.set)
        qc_poly_table_frame.rowconfigure(0, weight=1)
        qc_poly_table_frame.columnconfigure(0, weight=1)
        self.qc_poly_table.bind("<<TreeviewSelect>>", self._on_qc_poly_select)
        self._bind_context_menu(self.qc_poly_table, self._on_qc_poly_context_menu)
        self.qc_poly_table.tag_configure("summary", background="#e5e7eb", font=("TkDefaultFont", 11, "bold"))
        self.qc_poly_table_sorter = TreeviewSorter(
            self.qc_poly_table,
            {"poly": "POLY FILE", "stream": "STREAM", "channel": "RECORDER CH", "split": "SPLIT FILE", "frames": "FRAMES", "audio_result": "AUDIO RESULT", "metadata_result": "METADATA RESULT"},
            numeric_columns={"stream", "channel", "frames"},
            pinned_tags={"summary"},
            column_groups={"poly": ("poly", "split", "stream"), "split": ("poly", "split", "stream")},
        )
        self._update_qc_type_ui()

    def _update_qc_type_ui(self) -> None:
        self.qc_file_frame.pack_forget()
        self.qc_folder_frame.pack_forget()
        self.qc_poly_frame.pack_forget()
        if self.qc_type_var.get() == "Folder ↔ Folder":
            self.qc_folder_frame.pack(fill=tk.BOTH, expand=True)
        elif self.qc_type_var.get() == "Poly ↔ Split":
            self.qc_poly_frame.pack(fill=tk.BOTH, expand=True)
        else:
            self.qc_file_frame.pack(fill=tk.BOTH, expand=True)

    def _show_detected_qc_type(self, kind: str) -> None:
        labels = {"file": "File ↔ File", "folder": "Folder ↔ Folder", "poly_split": "Poly ↔ Split"}
        self.qc_type_var.set(labels[kind])
        self._update_qc_type_ui()

    @staticmethod
    def _path_row(parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, command, file: bool = False) -> None:
        ttk.Label(parent, text=label, width=14).grid(row=row, column=0, sticky="w", pady=3)
        tk.Entry(parent, textvariable=variable, width=90, bg="white", fg="black", insertbackground="black").grid(row=row, column=1, columnspan=4, sticky="ew", pady=3)
        ttk.Button(parent, text="Choose", command=command).grid(row=row, column=5, padx=(8, 0), pady=3)
        parent.columnconfigure(1, weight=1)

    def _formula_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, help_text: str) -> ttk.Combobox:
        ttk.Label(parent, text=label, width=14).grid(row=row, column=0, sticky="w", pady=3)
        combo = ttk.Combobox(parent, textvariable=variable, values=tuple(self.formula_presets), width=28, style="Formula.TCombobox")
        combo.grid(row=row, column=1, sticky="w", pady=3)
        ttk.Label(parent, text=help_text).grid(row=row, column=2, columnspan=3, sticky="w", padx=(8, 0))
        self._formula_combo_widgets.append(combo)
        return combo

    def _refresh_formula_combo_values(self) -> None:
        values = tuple(self.formula_presets)
        for combo in self._formula_combo_widgets:
            combo.configure(values=values)

    def _add_formula_preset(self) -> None:
        value = self.new_formula_preset_var.get().strip()
        if not value:
            return
        if value in self.formula_presets:
            self.status_var.set(f"'{value}' is already in the formula preset list.")
            return
        self.formula_presets.append(value)
        self.formula_preset_listbox.insert(tk.END, value)
        self.new_formula_preset_var.set("")
        self._refresh_formula_combo_values()

    def _move_formula_preset(self, direction: int) -> None:
        selection = self.formula_preset_listbox.curselection()
        if not selection:
            return
        index = selection[0]
        new_index = index + direction
        if not 0 <= new_index < len(self.formula_presets):
            return
        self.formula_presets[index], self.formula_presets[new_index] = self.formula_presets[new_index], self.formula_presets[index]
        self.formula_preset_listbox.delete(0, tk.END)
        for preset in self.formula_presets:
            self.formula_preset_listbox.insert(tk.END, preset)
        self.formula_preset_listbox.selection_set(new_index)
        self.formula_preset_listbox.see(new_index)
        self._refresh_formula_combo_values()

    def _remove_formula_preset(self) -> None:
        selection = self.formula_preset_listbox.curselection()
        if not selection:
            return
        index = selection[0]
        del self.formula_presets[index]
        self.formula_preset_listbox.delete(index)
        self._refresh_formula_combo_values()

    def _restore_formula_presets(self) -> None:
        self.formula_presets[:] = list(DEFAULT_FORMULA_PRESETS)
        self.formula_preset_listbox.delete(0, tk.END)
        for preset in self.formula_presets:
            self.formula_preset_listbox.insert(tk.END, preset)
        self._refresh_formula_combo_values()

    def _on_close(self) -> None:
        self.inbox.stop()
        self._browse_player_stop()  # don't leave an orphaned `sox ... -d` process behind
        try:
            save_app_settings({
                "formula_presets": self.formula_presets,
                "window_geometry": self.root.geometry(),
                "browse_autoplay_on_select": self.browse_autoplay_var.get(),
                "browse_solo_exclusive": self.browse_solo_exclusive_var.get(),
            })
        except OSError:
            pass
        self.root.destroy()

    def _install_live_refresh_traces(self) -> None:
        self.source_var.trace_add("write", lambda *_args: self._on_source_var_changed())
        # Target intentionally does NOT live-refresh on every keystroke like
        # the settings below do — refresh_filename_mode() rebuilds the whole
        # preview table, and with a large job list (thousands of rows) that
        # rebuild is expensive enough to make typing visibly laggy. Instead
        # it only fires once editing actually settles: FocusOut/Return on
        # the entry (bound in __init__ below), or immediately from
        # choose_output()/drag-and-drop, which already only fire once per
        # pick anyway.
        for variable in (
            self.output_formula_var,
            self.rename_formula_var,
            self.rename_text_position_var,
            self.rename_find_replace_enabled_var,
            self.rename_regex_var,
            self.rename_serial_prefix_var,
            self.rename_serial_digits_var,
            self.rename_serial_start_var,
            self.rename_serial_position_var,
            self.rename_number_mode_var,
            self.rename_apply_formula_var,
            self.rename_change_number_var,
            self.rename_after_embed_var,
            self.reembed_rename_formula_var,
            self.conflict_policy_var,
            self.combine_copy_non_combined_var,
            self.speaker_pattern_var,
            self.merge_layout_var,
            self.merge_order_var,
            self.merge_custom_order_var,
            self.merge_rename_var,
            self.merge_include_format_var,
            self.merge_include_layout_var,
            self.merge_rename_position_var,
            self.merge_custom_layout_name_var,
        ):
            variable.trace_add("write", lambda *_args: self._schedule_live_refresh())
        for variable in (*self.rename_find_vars, *self.rename_replace_vars):
            variable.trace_add("write", lambda *_args: self._schedule_live_refresh())

    def _update_preview_button_state(self) -> None:
        if self.mode_var.get() == "QC":
            self.preview_button.configure(state=tk.DISABLED)
            return
        source_text = self.source_var.get().strip()
        source_root = Path(source_text).expanduser() if source_text else None
        self.preview_button.configure(state=tk.NORMAL if source_root is not None and source_root.is_dir() else tk.DISABLED)

    def _on_source_var_changed(self) -> None:
        self._update_preview_button_state()
        self._clear_stale_jobs_if_source_changed()

    def _clear_stale_jobs_if_source_changed(self) -> None:
        """If Source is edited (typed, dropped, or picked) to point somewhere
        other than the folder the current Preview list was actually built
        from, that list — and whatever Execute would act on — silently
        describes the *old* folder. Clear it so Execute can never run
        against a Source that no longer matches what's displayed, instead of
        relying on the user to remember to click Preview again."""
        if self.mode_var.get() == "QC" or not self.jobs or self.current_source_root is None:
            return
        if source_path_is_stale(self.source_var.get(), self.current_source_root):
            self.clear_preview_list(reason="Source changed — Preview list cleared. Click Preview / Refresh before executing.")

    def _on_output_var_changed(self) -> None:
        self._clear_stale_jobs_if_output_changed()
        self._schedule_live_refresh()

    def _clear_stale_jobs_if_output_changed(self) -> None:
        """Re-embed's refresh_filename_mode() only tweaks the final-rename
        naming on top of job.target values that were fixed by *scanning the
        Target folder* during the last Preview — an existing render file
        found there is what job.target actually is, so a genuinely
        different Target can only be picked up by rescanning it, not by a
        cheap live recompute. (Split, Combine, and Rename all instead
        re-resolve Target live in refresh_filename_mode() itself, since none
        of them need to rescan anything just because Target's text changed
        — forcing a full re-Preview there would just throw away an
        expensive Source scan for no reason, which is exactly what
        happened before this method was scoped down to Re-embed only.)"""
        mode = self.mode_var.get()
        if not mode.startswith("Re-embed") or not self.jobs or self.current_source_root is None or self.current_output_root is None:
            return
        if target_path_is_stale(self.output_var.get(), self.current_source_root, self.current_output_root):
            self.clear_preview_list(reason="Target changed — Preview list cleared. Click Preview / Refresh before executing.")

    def clear_preview_list(self, reason: str = "Preview list cleared.") -> None:
        if self.mode_var.get() == "QC":
            self.qc_table.delete(*self.qc_table.get_children())
            self.qc_folder_table.delete(*self.qc_folder_table.get_children())
            self.qc_poly_table.delete(*self.qc_poly_table.get_children())
            self.qc_folder_rows.clear()
            self.qc_poly_rows.clear()
            self.qc_file_comparison = None
            self.qc_file_update_button.configure(state=tk.DISABLED)
            self._qc_folder_has_metadata = False
            self._qc_poly_has_metadata = False
            self.qc_folder_update_selected_button.configure(state=tk.DISABLED)
            self.qc_folder_update_problems_button.configure(state=tk.DISABLED)
            self.qc_poly_update_selected_button.configure(state=tk.DISABLED)
            self.qc_poly_update_problems_button.configure(state=tk.DISABLED)
            self.status_var.set("QC list cleared.")
            self.append_log(self.status_var.get())
            return
        if self._live_refresh_after is not None:
            self.root.after_cancel(self._live_refresh_after)
            self._live_refresh_after = None
        self.jobs = []
        self.preview_jobs.clear()
        self.table.delete(*self.table.get_children())
        self.rename_serial_ids = {}
        self.rename_serial_signature = None
        self._metadata_scan_current = False
        self._metadata_template_scan_current = False
        self.execute_button.configure(state=tk.DISABLED)
        self._update_structure_block_buttons()
        self.status_var.set(reason)
        self.append_log(self.status_var.get())

    def _structure_blocked_sources(self) -> list[Path]:
        return sorted({job.source for job in self.jobs if job.source is not None and is_structure_block(job.status)}, key=natural_path_key)

    def _update_structure_block_buttons(self) -> None:
        if not hasattr(self, "send_to_repair_button"):
            return
        split_mode = self.mode_var.get().startswith("Split")
        if split_mode and not self.structure_actions.winfo_manager():
            self.structure_actions.pack(fill=tk.X, pady=(6, 0), after=self.actions_row)
        elif not split_mode and self.structure_actions.winfo_manager():
            self.structure_actions.pack_forget()
        state = tk.NORMAL if split_mode and self._structure_blocked_sources() else tk.DISABLED
        self.send_to_repair_button.configure(state=state)
        self.rescan_blocked_button.configure(state=state)

    def send_structure_blocked_to_repair(self) -> None:
        sources = self._structure_blocked_sources()
        if sources:
            self._open_in_wave_repair(sources)

    def rescan_structure_blocked(self) -> None:
        """After Wave Repair: re-preview only the files that were blocked for
        structure, merging their fresh jobs back into the current list."""
        sources = [path for path in self._structure_blocked_sources() if path.is_file()]
        if sources:
            self._import_dropped_files(sources, purpose="rescan")

    def _schedule_live_refresh(self) -> None:
        if not self.jobs or self.current_source_root is None or self.current_output_root is None:
            return
        if self._live_refresh_after is not None:
            self.root.after_cancel(self._live_refresh_after)
        if self.mode_var.get().startswith("Rename"):
            self.status_var.set("Rename settings changed; updating cached Preview...")
        self._live_refresh_after = self.root.after(250, self._run_live_refresh)

    def _run_live_refresh(self) -> None:
        self._live_refresh_after = None
        self.refresh_filename_mode()

    def _start_rename_metadata_refresh(self) -> None:
        if self.current_source_root is None:
            return
        if self._rename_metadata_loading_root == self.current_source_root:
            return
        self._rename_metadata_request_id += 1
        request_id = self._rename_metadata_request_id
        source_root = self.current_source_root
        self._rename_metadata_loading_root = source_root
        sources = [job.source for job in self.jobs if job.source is not None]
        self.status_var.set(f"Loading Tape + TC metadata for {len(sources):,} files...")
        threading.Thread(
            target=self._rename_metadata_worker,
            args=(request_id, source_root, sources, self.bwf_var.get()),
            daemon=True,
        ).start()

    def _rename_metadata_worker(self, request_id: int, source_root: Path, sources: list[Path], bwfmetaedit: str) -> None:
        metadata_by_source: dict[Path, tuple[str, int, str, str, str]] = {}
        for source in sources:
            metadata_by_source[source] = read_serial_metadata(bwfmetaedit, source)
        self.events.put(("rename_metadata_done", (request_id, source_root, metadata_by_source)))

    def _apply_table_stretch_column(self) -> None:
        """Re-point which column has stretch=True for the current mode —
        called from _update_mode_ui() on every mode switch and (via
        _on_rename_option_changed()) on nearly every Rename control change.
        Deliberately touches only the `stretch` option, never `width`: the
        construction-time configure_treeview_column_widths() call already
        set every column's initial pixel width once, and re-passing those
        same static defaults here every time this runs would silently wipe
        out a manual column drag-resize the moment the user touched
        anything else in the panel — that's the bug this used to have."""
        if not hasattr(self, "table"):
            return
        mode = self.mode_var.get()
        stretch_column = "planned" if mode.startswith("Rename") else "target"
        for column in ("source", "target", "planned"):
            self.table.column(column, stretch=column == stretch_column)

    def _update_mode_ui(self) -> None:
        if hasattr(self, "send_to_repair_button"):
            self._update_structure_block_buttons()
        mode = self.mode_var.get()
        qc_mode = mode == "QC"
        settings_mode = mode == "Settings"
        browse_mode = mode == "Browse"
        workflow_mode = not qc_mode and not settings_mode and not browse_mode
        self._apply_table_stretch_column()
        if workflow_mode:
            self.workflow_folder_picker.pack(fill=tk.X, pady=(0, 8))
            self.mode_settings_host.pack(fill=tk.X)
        else:
            self.workflow_folder_picker.pack_forget()
            self.mode_settings_host.pack_forget()

        # Always forget every mutually-exclusive bottom-area frame first, then
        # repack only what's needed, in a fixed order. Tk's pack() keeps an
        # already-managed widget's existing stacking slot instead of moving
        # it, so repacking only *some* of these silently reorders them;
        # forgetting all of them first guarantees the same order every time
        # regardless of which modes were visited before this call. The
        # Execution Log is a separate floating window now (ExecutionLogWindow),
        # not one of these — it's shown/hidden independently of mode.
        for frame in (self.table_frame, self.qc_frame, self.settings_frame, self.browse_explorer_frame):
            frame.pack_forget()
        if not browse_mode:
            # Leaving Browse mode (or never having been in it this call) —
            # don't leave a `sox ... -d` process playing quietly in the
            # background with no visible transport controls anymore.
            self._browse_player_stop()

        if settings_mode:
            self.settings_frame.pack(fill=tk.BOTH, expand=True)
            self.status_var.set("Settings: tool paths apply to all workflows.")
            return
        if qc_mode:
            self.qc_frame.pack(fill=tk.BOTH, expand=True)
            self._seed_qc_inputs_from_workflow_paths()
            self.status_var.set("QC mode: compare basic File and Audio information. WAV files are read-only.")
            return
        if browse_mode:
            # No Source/Target root, no Preview/Execute, no collected list
            # — a Finder-like folder browser on the left, the matching WAV
            # file list and a simple waveform-and-play preview on the
            # right, in the spirit of REAPER's own Media Explorer. An
            # earlier version paired this with an accumulating "collected
            # list" (drag/Tag-to-add) on the right instead of the player;
            # removed once pure browse-and-audition turned out to be the
            # actual goal.
            self.browse_explorer_frame.pack(fill=tk.BOTH, expand=True)
            self.preview_button.configure(state=tk.DISABLED)
            self.execute_button.configure(state=tk.DISABLED)
            self.status_var.set("Browse mode: pick a folder on the left, click a file to preview and play it.")
            return
        self.table_frame.pack(fill=tk.BOTH, expand=True)

        frame_by_mode = {
            "Split Poly with SoX + Embed": self.split_frame,
            "Re-embed Matched Render Files": self.reembed_frame,
            "Rename": self.rename_frame,
            "Combine / Merge Mono Files to Poly": self.combine_frame,
        }
        for key, frame in frame_by_mode.items():
            if key == mode:
                frame.pack(fill=tk.X)
            else:
                frame.pack_forget()

        rename_mode = mode == "Rename"
        target_label = "Before Rename" if rename_mode else "Target"
        if self._table_column_labels.get("target") != target_label:
            self._table_column_labels["target"] = target_label
            self.table.heading("target", text=target_label)

        if mode.startswith("Split"):
            self.status_var.set("Split mode: Output/Target can be absolute or a folder name next to Source root.")
        elif rename_mode:
            self.status_var.set("Rename mode: Source formula identifies files to rename or copy. Metadata is not modified.")
        elif mode.startswith("Combine"):
            self.status_var.set("Combine / Merge mode: generic .A# and speaker-aware mono files can share one Preview.")
        else:
            self.status_var.set("Choose folders, then Preview.")
        # Browse mode forces Preview off regardless of Source's own
        # validity — switching back to a real workflow mode needs this to
        # reflect Source's actual state again, since nothing else re-runs
        # this check on a mode switch alone (only Source itself changing
        # does, via its own trace).
        self._update_preview_button_state()

    def _existing_dialog_dir(self, text: str, fallback: Optional[Path] = None) -> Optional[str]:
        value = text.strip()
        if value:
            path = Path(value).expanduser()
            if path.is_dir():
                return str(path)
            if path.parent.is_dir():
                return str(path.parent)
        if fallback and fallback.is_dir():
            return str(fallback)
        return None

    def choose_source(self) -> None:
        initialdir = self._existing_dialog_dir(self.source_var.get(), self.last_source_dialog_dir)
        path = filedialog.askdirectory(title="Choose source root", initialdir=initialdir)
        if path:
            self.last_source_dialog_dir = Path(path)
            self.source_var.set(path)

    def _workflow_source_folder_changed(self, folder: Path) -> None:
        self.last_source_dialog_dir = folder
        if self.current_source_root is None or folder.resolve() != self.current_source_root.resolve():
            self.rename_serial_ids = {}
            self.rename_serial_signature = None
            self._rename_metadata_request_id += 1
            self._rename_metadata_loaded_root = None
            self._rename_metadata_loading_root = None
            self._metadata_scan_current = False
            self._metadata_template_scan_current = False

    def _workflow_target_folder_changed(self, folder: Path) -> None:
        self.last_output_dialog_dir = folder
        self._on_output_var_changed()

    def _set_qc_input(self, side: str, path: Path) -> None:
        variable = self.qc_input_source_var if side == "source" else self.qc_input_target_var
        variable.set(str(path))
        if side == "source":
            self.qc_input_source_path = path
            self.last_source_dialog_dir = path if path.is_dir() else path.parent
        else:
            self.qc_input_target_path = path
            self.last_output_dialog_dir = path if path.is_dir() else path.parent
        self.status_var.set("Ready to detect QC type and compare." if self.qc_input_source_path and self.qc_input_target_path else "Load both Source and Target.")

    def _seed_qc_inputs_from_workflow_paths(self) -> None:
        """The first time QC is reached with its own Source/Target still
        unset, pre-fill them from whatever Source/Target the other four
        modes (Split/Re-embed/Rename/Combine, which already share one
        Source/Target between themselves) currently have — landing on an
        empty QC after already typing a path elsewhere felt like redundant
        re-entry. QC keeps its own independent fields from this point on
        (unlike those four, it can point at a single file instead of only a
        folder, so it can't just permanently share the same variable)."""
        if self.qc_input_source_path is None:
            source_path = Path(self.source_var.get().strip()).expanduser() if self.source_var.get().strip() else None
            if source_path is not None and source_path.is_dir():
                self._set_qc_input("source", source_path)
        if self.qc_input_target_path is None:
            target_path = Path(self.output_var.get().strip()).expanduser() if self.output_var.get().strip() else None
            if target_path is not None and target_path.is_dir():
                self._set_qc_input("target", target_path)

    def _choose_qc_input_file(self, side: str) -> None:
        current = self.qc_input_source_path if side == "source" else self.qc_input_target_path
        path = filedialog.askopenfilename(
            parent=self.root,
            title=f"Load QC {side.title()} WAV",
            initialdir=self._existing_dialog_dir(str(current or ""), self.last_source_dialog_dir),
            filetypes=(("WAV audio", "*.wav *.WAV"), ("All files", "*")),
        )
        if path:
            self._set_qc_input(side, Path(path))

    def _choose_qc_input_folder(self, side: str) -> None:
        current = self.qc_input_source_path if side == "source" else self.qc_input_target_path
        path = filedialog.askdirectory(parent=self.root, title=f"Select QC {side.title()} Folder", initialdir=self._existing_dialog_dir(str(current or ""), self.last_source_dialog_dir))
        if path:
            self._set_qc_input(side, Path(path))

    def compare_qc_inputs(self) -> None:
        source = self.qc_input_source_path
        target = self.qc_input_target_path
        if source is None or target is None:
            self.status_var.set("Load both Source and Target before comparing.")
            return
        self._qc_auto_request_id += 1
        request_id = self._qc_auto_request_id
        started = time.monotonic()
        soxi = self.soxi_var.get().strip()
        bwfmetaedit = self.bwf_var.get().strip()
        include_audio = self.qc_compare_audio_var.get()
        include_metadata = self.qc_compare_metadata_var.get()
        self.pause_requested.clear()
        self.cancel_requested.clear()
        self.active_operation_phase = "qc"
        self._set_operation_controls(operation_control_capabilities("qc"))
        self.qc_auto_compare_button.configure(state=tk.DISABLED)
        self.status_var.set("QC — Detecting input type...")
        self.append_log(self.status_var.get())
        self.log_window.show()
        threading.Thread(target=self._qc_auto_compare_worker, args=(request_id, source, target, soxi, bwfmetaedit, started, include_audio, include_metadata), daemon=True).start()

    def _qc_auto_compare_worker(self, request_id: int, source: Path, target: Path, soxi: str, bwfmetaedit: str, started: float, include_audio: bool = True, include_metadata: bool = False) -> None:
        try:
            def report(stage: str, current: int, total: int) -> None:
                wait_for_qc_control(self.pause_requested, self.cancel_requested)
                self.events.put(("progress", ProgressEvent("qc_auto", current, total, stage, started=started, label=f"QC — {stage}", request_id=request_id)))

            wait_for_qc_control(self.pause_requested, self.cancel_requested)
            detection = detect_qc_inputs(source, target, progress=report)
            if detection.kind == "invalid":
                raise ValueError(detection.message)

            if detection.kind == "file":
                report("Comparing files", 0, 1)
                comparison = build_file_qc_comparison(soxi, bwfmetaedit, detection.source, detection.target, include_audio, include_metadata)
                report("Comparing files", 1, 1)
            elif detection.kind == "folder":
                comparison = compare_qc_folders(soxi, detection.source, detection.target, report, bwfmetaedit, include_audio, include_metadata)
            else:
                comparison = compare_poly_split_folders(
                    bwfmetaedit,
                    detection.source,
                    detection.target,
                    report,
                    report,
                    include_audio,
                    include_metadata,
                )
            wait_for_qc_control(self.pause_requested, self.cancel_requested)
        except QcCancelled:
            self.events.put(("qc_auto_cancelled", request_id))
        except Exception as exc:
            self.events.put(("qc_auto_error", (request_id, str(exc))))
        else:
            self.events.put(("qc_auto_done", (request_id, detection, comparison)))

    def _show_qc_file_results(self, comparison: dict[str, object], source_path: Optional[Path] = None, target_path: Optional[Path] = None) -> None:
        self.qc_table.delete(*self.qc_table.get_children())
        has_metadata_section = False
        metadata_mismatch = False
        for section_name, rows in comparison["sections"]:
            self.qc_table.insert("", tk.END, values=(section_name, "", "", ""), tags=("section",))
            for row in rows:
                self.qc_table.insert("", tk.END, values=(row["label"], row["source"], row["target"], row["result"]))
                if section_name == "METADATA":
                    has_metadata_section = True
                    if row["result"] == "≠":
                        metadata_mismatch = True
        result = str(comparison["result"])
        self.qc_table.insert("", tk.END, values=("RESULT", "", "", result), tags=("section",))
        self.qc_message_var.set(f"RESULT: {result}")
        self.qc_table_sorter.reapply()
        self.qc_file_comparison = {"source_path": source_path, "target_path": target_path}
        self.qc_file_update_button.configure(state=tk.NORMAL if has_metadata_section and metadata_mismatch and source_path and target_path else tk.DISABLED)

    def _choose_qc_file(self, side: str) -> None:
        current = self.qc_source_path if side == "source" else self.qc_target_path
        initialdir = self._existing_dialog_dir(str(current or ""), self.last_source_dialog_dir)
        path = filedialog.askopenfilename(
            parent=self.root,
            title=f"Load QC {side.title()} WAV",
            initialdir=initialdir,
            filetypes=(("WAV audio", "*.wav *.WAV"), ("All files", "*")),
        )
        if path:
            self._load_qc_file(side, Path(path))

    def choose_qc_source(self) -> None:
        self._choose_qc_file("source")

    def choose_qc_target(self) -> None:
        self._choose_qc_file("target")

    def _load_qc_file(self, side: str, path: Path) -> None:
        if not path.is_file() or path.suffix.lower() != ".wav":
            self.qc_message_var.set(f"Please load a valid {side.title()} WAV file.")
            return
        if side == "source":
            self.qc_source_path = path
            self.qc_source_var.set(str(path))
        else:
            self.qc_target_path = path
            self.qc_target_var.set(str(path))
        self.qc_message_var.set("Ready to compare." if self.qc_source_path and self.qc_target_path else "Please load both Source and Target WAV files.")

    def _drop_qc_file(self, event, side: str) -> str:
        dropped_paths = [Path(path).expanduser() for path in self.root.tk.splitlist(event.data)]
        wav_path = next((path for path in dropped_paths if path.is_file() and path.suffix.lower() == ".wav"), None)
        if wav_path is not None:
            self._load_qc_file(side, wav_path)
        elif dropped_paths:
            self.qc_message_var.set(f"Please drop a valid {side.title()} WAV file.")
        return "break"

    def drop_qc_source(self, event) -> str:
        return self._drop_qc_file(event, "source")

    def drop_qc_target(self, event) -> str:
        return self._drop_qc_file(event, "target")

    def compare_qc_files(self) -> None:
        if self.qc_source_path is None or self.qc_target_path is None:
            self.qc_message_var.set("Please load both Source and Target WAV files.")
            return
        try:
            comparison = build_file_qc_comparison(
                self.soxi_var.get().strip(),
                self.bwf_var.get().strip(),
                self.qc_source_path,
                self.qc_target_path,
                self.qc_compare_audio_var.get(),
                self.qc_compare_metadata_var.get(),
            )
        except Exception as exc:
            self.qc_message_var.set(f"Unable to compare: {exc}")
            return
        self._show_qc_file_results(comparison, self.qc_source_path, self.qc_target_path)

    def _choose_qc_folder(self, side: str) -> None:
        current = self.qc_source_folder if side == "source" else self.qc_target_folder
        initialdir = self._existing_dialog_dir(str(current or ""), self.last_source_dialog_dir)
        path = filedialog.askdirectory(parent=self.root, title=f"Select QC {side.title()} Folder", initialdir=initialdir)
        if path:
            self._load_qc_folder(side, Path(path))

    def choose_qc_source_folder(self) -> None:
        self._choose_qc_folder("source")

    def choose_qc_target_folder(self) -> None:
        self._choose_qc_folder("target")

    def _load_qc_folder(self, side: str, path: Path) -> None:
        if not path.is_dir():
            self.qc_folder_message_var.set(f"Please select a valid {side.title()} folder.")
            return
        if side == "source":
            self.qc_source_folder = path
            self.qc_source_folder_var.set(str(path))
        else:
            self.qc_target_folder = path
            self.qc_target_folder_var.set(str(path))
        self.qc_folder_message_var.set("Ready to compare." if self.qc_source_folder and self.qc_target_folder else "Please load both Source and Target folders.")

    def _drop_qc_folder(self, event, side: str) -> str:
        dropped_paths = [Path(path).expanduser() for path in self.root.tk.splitlist(event.data)]
        folder = next((path for path in dropped_paths if path.is_dir()), None)
        if folder is not None:
            self._load_qc_folder(side, folder)
        elif dropped_paths:
            self.qc_folder_message_var.set(f"Please drop a valid {side.title()} folder.")
        return "break"

    def drop_qc_source_folder(self, event) -> str:
        return self._drop_qc_folder(event, "source")

    def drop_qc_target_folder(self, event) -> str:
        return self._drop_qc_folder(event, "target")

    def compare_qc_folders(self) -> None:
        if self.qc_source_folder is None or self.qc_target_folder is None:
            self.qc_folder_message_var.set("Please load both Source and Target folders.")
            return
        self._qc_folder_request_id += 1
        request_id = self._qc_folder_request_id
        started = time.monotonic()
        self.qc_folder_compare_button.configure(state=tk.DISABLED)
        self.status_var.set("Folder QC — preparing comparison...")
        self.log_window.show()
        threading.Thread(
            target=self._qc_folder_compare_worker,
            args=(
                request_id,
                self.soxi_var.get().strip(),
                self.bwf_var.get().strip(),
                self.qc_source_folder,
                self.qc_target_folder,
                started,
                self.qc_compare_audio_var.get(),
                self.qc_compare_metadata_var.get(),
            ),
            daemon=True,
        ).start()

    def _qc_folder_compare_worker(self, request_id: int, soxi: str, bwfmetaedit: str, source_folder: Path, target_folder: Path, started: float, include_audio: bool = True, include_metadata: bool = False) -> None:
        try:
            def report(stage: str, current: int, total: int) -> None:
                self.events.put(("progress", ProgressEvent("qc_folder", current, total, stage, started=started, label=f"Folder QC — {stage}", request_id=request_id)))

            comparison = compare_qc_folders(soxi, source_folder, target_folder, report, bwfmetaedit, include_audio, include_metadata)
        except Exception as exc:
            self.events.put(("qc_folder_error", (request_id, str(exc))))
        else:
            self.events.put(("qc_folder_done", (request_id, comparison)))

    def _show_qc_folder_results(self, comparison: dict[str, object]) -> None:
        self.qc_folder_table.delete(*self.qc_folder_table.get_children())
        self.qc_folder_rows.clear()
        rows = comparison["rows"]
        if not rows:
            self.qc_folder_message_var.set("No WAV files found.")
            self._qc_folder_has_metadata = False
            self.qc_folder_update_selected_button.configure(state=tk.DISABLED)
            self.qc_folder_update_problems_button.configure(state=tk.DISABLED)
            return
        for row in rows:
            item_id = self.qc_folder_table.insert("", tk.END, values=(row["filename"], row["source"], row["target"], row["result"], row.get("metadata_result", "-")))
            self.qc_folder_rows[item_id] = row
        self.qc_folder_table.insert("", tk.END, values=("RESULT", "", "", "", ""), tags=("summary",))
        self.qc_folder_table_sorter.reapply()
        self._refresh_qc_folder_summary()

    def _refresh_qc_folder_summary(self) -> None:
        """Recompute the RESULT summary row, the status message, and the
        Update Metadata buttons' enabled state from whatever is currently in
        `self.qc_folder_rows` — shared by the initial Compare render and by
        `_apply_qc_folder_row_refresh()` so an in-place row update (after
        writing metadata to a handful of files) doesn't need to re-run the
        full folder scan just to keep these in sync."""
        rows = list(self.qc_folder_rows.values())
        result = qc_rows_overall_result(rows) if rows else "DIFFERENT"
        summary_id = next((iid for iid in self.qc_folder_table.get_children() if "summary" in self.qc_folder_table.item(iid, "tags")), None)
        if summary_id is not None:
            self.qc_folder_table.set(summary_id, "audio_result", result)
            self.qc_folder_table.set(summary_id, "metadata_result", result)
        self.qc_folder_message_var.set(f"RESULT: {result}")
        any_problem = any(
            row.get("metadata_result", "-") not in ("-", "PASS") and isinstance(row.get("source_path"), Path) and isinstance(row.get("target_path"), Path)
            for row in rows
        )
        self._qc_folder_has_metadata = any(row.get("metadata_result", "-") != "-" for row in rows)
        self._sync_qc_folder_update_selected_button()
        self.qc_folder_update_problems_button.configure(state=tk.NORMAL if any_problem else tk.DISABLED)

    def _sync_qc_folder_update_selected_button(self) -> None:
        has_selection = bool(self.qc_folder_table.selection())
        enabled = qc_update_selected_button_enabled(self._qc_folder_has_metadata, has_selection)
        self.qc_folder_update_selected_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _apply_qc_folder_row_refresh(self, refreshed_rows: list[dict]) -> None:
        """Update just the rows an Update Metadata action actually wrote to,
        in place — an Update action never touches the audio/data chunk (only
        BEXT/iXML), so Audio Result can't have changed and only Metadata
        Result needs re-checking; re-running the whole Folder Compare here
        was needlessly rescanning and re-comparing every other file too."""
        by_target_path = {row["target_path"]: row for row in refreshed_rows}
        for item_id, row in self.qc_folder_rows.items():
            updated = by_target_path.get(row.get("target_path"))
            if updated is None:
                continue
            row["metadata_result"] = updated["metadata_result"]
            row["metadata_comparison"] = updated["metadata_comparison"]
            self.qc_folder_table.set(item_id, "metadata_result", row["metadata_result"])
        self._refresh_qc_folder_summary()

    def _choose_qc_poly_folder(self, side: str) -> None:
        current = self.qc_poly_source_folder if side == "source" else self.qc_poly_target_folder
        initialdir = self._existing_dialog_dir(str(current or ""), self.last_source_dialog_dir)
        path = filedialog.askdirectory(parent=self.root, title=f"Select {side.title()} Folder", initialdir=initialdir)
        if path:
            self._load_qc_poly_folder(side, Path(path))

    def choose_qc_poly_source_folder(self) -> None:
        self._choose_qc_poly_folder("source")

    def choose_qc_poly_target_folder(self) -> None:
        self._choose_qc_poly_folder("target")

    def _load_qc_poly_folder(self, side: str, path: Path) -> None:
        if not path.is_dir():
            self.qc_poly_message_var.set(f"Please select a valid {side.title()} folder.")
            return
        if side == "source":
            self.qc_poly_source_folder = path
            self.qc_poly_source_folder_var.set(str(path))
        else:
            self.qc_poly_target_folder = path
            self.qc_poly_target_folder_var.set(str(path))
        self.qc_poly_message_var.set("Ready to compare." if self.qc_poly_source_folder and self.qc_poly_target_folder else "Load Source and Target folders.")

    def _drop_qc_poly_folder(self, event, side: str) -> str:
        dropped_paths = [Path(path).expanduser() for path in self.root.tk.splitlist(event.data)]
        folder = next((path for path in dropped_paths if path.is_dir()), None)
        if folder is not None:
            self._load_qc_poly_folder(side, folder)
        elif dropped_paths:
            self.qc_poly_message_var.set(f"Please drop a valid {side.title()} folder.")
        return "break"

    def drop_qc_poly_source_folder(self, event) -> str:
        return self._drop_qc_poly_folder(event, "source")

    def drop_qc_poly_target_folder(self, event) -> str:
        return self._drop_qc_poly_folder(event, "target")

    def compare_qc_poly_folders(self) -> None:
        source_folder = self.qc_poly_source_folder
        target_folder = self.qc_poly_target_folder
        if source_folder is None or target_folder is None:
            self.qc_poly_message_var.set("Please load both Source and Target folders.")
            return
        self._qc_poly_request_id += 1
        request_id = self._qc_poly_request_id
        started = time.monotonic()
        self.qc_poly_compare_button.configure(state=tk.DISABLED)
        self.qc_poly_message_var.set("QC Poly ↔ Split: preparing comparison...")
        self.log_window.show()
        threading.Thread(
            target=self._qc_poly_compare_worker,
            args=(
                request_id,
                self.bwf_var.get().strip(),
                source_folder,
                target_folder,
                started,
                self.qc_compare_audio_var.get(),
                self.qc_compare_metadata_var.get(),
            ),
            daemon=True,
        ).start()

    def _qc_poly_compare_worker(self, request_id: int, bwfmetaedit: str, source_folder: Path, target_folder: Path, started: float, include_audio: bool = True, include_metadata: bool = False) -> None:
        try:
            def report(stage: str, current: int, total: int) -> None:
                self.events.put(("progress", ProgressEvent("qc_poly", current, total, stage, started=started, label=f"QC Poly ↔ Split — {stage}", request_id=request_id)))

            def report_prepare(stage: str, current: int, total: int) -> None:
                self.events.put(("progress", ProgressEvent("qc_poly_prepare", current, total, stage, started=started, label=f"QC Poly ↔ Split — {stage}", request_id=request_id)))

            comparison = compare_poly_split_folders(bwfmetaedit, source_folder, target_folder, report, report_prepare, include_audio, include_metadata)
        except Exception as exc:
            self.events.put(("qc_poly_error", (request_id, str(exc))))
        else:
            self.events.put(("qc_poly_done", (request_id, comparison)))

    def _show_qc_poly_results(self, comparison: dict[str, object]) -> None:
        self.qc_poly_table.delete(*self.qc_poly_table.get_children())
        self.qc_poly_rows.clear()
        for row in comparison["rows"]:
            item_id = self.qc_poly_table.insert("", tk.END, values=(row["poly_file"], row["stream"], row["channel"], row["split_file"], row["frames"], row["result"], row.get("metadata_result", "-")))
            self.qc_poly_rows[item_id] = row
        self.qc_poly_table.insert("", tk.END, values=("RESULT", "", "", "", "", "", ""), tags=("summary",))
        self.qc_poly_table_sorter.reapply()
        self._refresh_qc_poly_summary()

    def _refresh_qc_poly_summary(self) -> None:
        """Poly ↔ Split counterpart of `_refresh_qc_folder_summary()` — see
        that method for why this is shared between the initial Compare
        render and an in-place post-update refresh."""
        rows = list(self.qc_poly_rows.values())
        relevant_rows = [row for row in rows if row.get("result") != "SKIP: NOT POLY"]
        result = qc_rows_overall_result(relevant_rows) if relevant_rows else "DIFFERENT"
        summary_id = next((iid for iid in self.qc_poly_table.get_children() if "summary" in self.qc_poly_table.item(iid, "tags")), None)
        if summary_id is not None:
            self.qc_poly_table.set(summary_id, "audio_result", result)
            self.qc_poly_table.set(summary_id, "metadata_result", result)
        self.qc_poly_message_var.set(f"RESULT: {result}")
        any_problem = any(
            row.get("metadata_result", "-") not in ("-", "PASS") and isinstance(row.get("poly_path"), Path) and isinstance(row.get("split_path"), Path)
            for row in rows
        )
        self._qc_poly_has_metadata = any(row.get("metadata_result", "-") != "-" for row in rows)
        self._sync_qc_poly_update_selected_button()
        self.qc_poly_update_problems_button.configure(state=tk.NORMAL if any_problem else tk.DISABLED)

    def _sync_qc_poly_update_selected_button(self) -> None:
        has_selection = bool(self.qc_poly_table.selection())
        enabled = qc_update_selected_button_enabled(self._qc_poly_has_metadata, has_selection)
        self.qc_poly_update_selected_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _apply_qc_poly_row_refresh(self, refreshed_rows: list[dict]) -> None:
        """Poly ↔ Split counterpart of `_apply_qc_folder_row_refresh()` — an
        Update action only ever rewrites BEXT/iXML on the Split file, so the
        PCM stream check (Frames/Audio Result) can't have changed; only
        Metadata Result needs re-checking."""
        by_split_path = {row["split_path"]: row for row in refreshed_rows}
        for item_id, row in self.qc_poly_rows.items():
            updated = by_split_path.get(row.get("split_path"))
            if updated is None:
                continue
            row["metadata_result"] = updated["metadata_result"]
            row["metadata_comparison"] = updated["metadata_comparison"]
            self.qc_poly_table.set(item_id, "metadata_result", row["metadata_result"])
        self._refresh_qc_poly_summary()

    def update_qc_file_metadata(self) -> None:
        info = self.qc_file_comparison or {}
        source_path = info.get("source_path")
        target_path = info.get("target_path")
        if not isinstance(source_path, Path) or not isinstance(target_path, Path):
            return
        if not messagebox.askokcancel(
            APP_NAME,
            f"Overwrite metadata on 1 file?\n\n{target_path.name}\n\nwill have its BEXT and iXML metadata fully replaced with the metadata from:\n{source_path.name}",
        ):
            return
        self.qc_file_update_button.configure(state=tk.DISABLED)
        self.status_var.set("Updating metadata...")
        self.append_log(self.status_var.get())
        bwfmetaedit = self.bwf_var.get().strip()
        soxi = self.soxi_var.get().strip()
        include_audio = self.qc_compare_audio_var.get()
        threading.Thread(target=self._update_qc_file_metadata_worker, args=(bwfmetaedit, soxi, source_path, target_path, include_audio), daemon=True).start()

    def _update_qc_file_metadata_worker(self, bwfmetaedit: str, soxi: str, source_path: Path, target_path: Path, include_audio: bool) -> None:
        try:
            apply_qc_metadata_update(bwfmetaedit, source_path, target_path)
            comparison = build_file_qc_comparison(soxi, bwfmetaedit, source_path, target_path, include_audio, True)
        except Exception as exc:
            self.events.put(("qc_update_error", str(exc)))
            return
        self.events.put(("qc_file_update_done", (comparison, source_path, target_path)))

    def _collect_qc_folder_update_targets(self, mode: str) -> list[tuple[Path, Path]]:
        item_ids = self.qc_folder_table.selection() if mode == "selected" else list(self.qc_folder_rows.keys())
        targets = []
        for item_id in item_ids:
            row = self.qc_folder_rows.get(item_id)
            if row is None:
                continue
            source_path = row.get("source_path")
            target_path = row.get("target_path")
            if not isinstance(source_path, Path) or not isinstance(target_path, Path):
                continue
            if mode == "problems" and row.get("metadata_result", "-") in ("-", "PASS"):
                continue
            targets.append((source_path, target_path))
        return targets

    def update_qc_folder_metadata(self, mode: str) -> None:
        targets = self._collect_qc_folder_update_targets(mode)
        if not targets:
            messagebox.showinfo(APP_NAME, "No files with a metadata problem found." if mode == "problems" else "Select one or more rows first.")
            return
        if not messagebox.askokcancel(APP_NAME, f"Overwrite metadata on {len(targets)} file(s) in Target, using each matching Source file's metadata?"):
            return
        self.qc_folder_update_selected_button.configure(state=tk.DISABLED)
        self.qc_folder_update_problems_button.configure(state=tk.DISABLED)
        self.status_var.set(f"Updating metadata on {len(targets)} file(s)...")
        self.append_log(self.status_var.get())
        bwfmetaedit = self.bwf_var.get().strip()
        threading.Thread(target=self._update_qc_folder_metadata_worker, args=(bwfmetaedit, targets), daemon=True).start()

    def _update_qc_folder_metadata_worker(self, bwfmetaedit: str, targets: list[tuple[Path, Path]]) -> None:
        failures = []
        refreshed_rows = []
        for source_path, target_path in targets:
            try:
                apply_qc_metadata_update(bwfmetaedit, source_path, target_path)
            except Exception as exc:
                failures.append(f"{target_path.name}: {exc}")
                continue
            try:
                metadata_comparison = compare_qc_metadata_fields(bwfmetaedit, source_path, target_path)
                metadata_result = metadata_comparison["result"]
            except Exception as exc:
                metadata_comparison = None
                metadata_result = f"BLOCKED: {exc}"
            refreshed_rows.append({"target_path": target_path, "metadata_result": metadata_result, "metadata_comparison": metadata_comparison})
        self.events.put(("qc_folder_update_done", (len(targets), failures, refreshed_rows)))

    def _collect_qc_poly_update_targets(self, mode: str) -> list[tuple[Path, int, Path]]:
        item_ids = self.qc_poly_table.selection() if mode == "selected" else list(self.qc_poly_rows.keys())
        targets = []
        for item_id in item_ids:
            row = self.qc_poly_rows.get(item_id)
            if row is None:
                continue
            poly_path = row.get("poly_path")
            split_path = row.get("split_path")
            if not isinstance(poly_path, Path) or not isinstance(split_path, Path):
                continue
            try:
                stream_index = int(str(row.get("stream")))
            except ValueError:
                continue
            if mode == "problems" and row.get("metadata_result", "-") in ("-", "PASS"):
                continue
            targets.append((poly_path, stream_index, split_path))
        return targets

    def update_qc_poly_metadata(self, mode: str) -> None:
        targets = self._collect_qc_poly_update_targets(mode)
        if not targets:
            messagebox.showinfo(APP_NAME, "No files with a metadata problem found." if mode == "problems" else "Select one or more rows first.")
            return
        if not messagebox.askokcancel(APP_NAME, f"Overwrite metadata on {len(targets)} Split file(s), using the matching Poly channel's metadata?"):
            return
        self.qc_poly_update_selected_button.configure(state=tk.DISABLED)
        self.qc_poly_update_problems_button.configure(state=tk.DISABLED)
        self.status_var.set(f"Updating metadata on {len(targets)} file(s)...")
        self.append_log(self.status_var.get())
        bwfmetaedit = self.bwf_var.get().strip()
        threading.Thread(target=self._update_qc_poly_metadata_worker, args=(bwfmetaedit, targets), daemon=True).start()

    def _update_qc_poly_metadata_worker(self, bwfmetaedit: str, targets: list[tuple[Path, int, Path]]) -> None:
        failures = []
        refreshed_rows = []
        for poly_path, stream_index, split_path in targets:
            try:
                apply_qc_metadata_update_poly_to_split(bwfmetaedit, poly_path, stream_index, split_path)
            except Exception as exc:
                failures.append(f"{split_path.name}: {exc}")
                continue
            try:
                metadata_comparison = compare_qc_metadata_fields(bwfmetaedit, poly_path, split_path)
                metadata_result = metadata_comparison["result"]
            except Exception as exc:
                metadata_comparison = None
                metadata_result = f"BLOCKED: {exc}"
            refreshed_rows.append({"split_path": split_path, "metadata_result": metadata_result, "metadata_comparison": metadata_comparison})
        self.events.put(("qc_poly_update_done", (len(targets), failures, refreshed_rows)))

    def _on_qc_poly_select(self, _event=None) -> None:
        self._sync_qc_poly_update_selected_button()
        if self.inspector is None or self.inspector_window is None or not self.inspector_window.winfo_exists():
            return
        selected = self.qc_poly_table.selection()
        if not selected:
            return
        row = self.qc_poly_rows.get(selected[0])
        if row is None:
            return
        self.inspector.load_qc_mapping(row, reset_scroll=False)

    def _load_qc_pair_into_inspector(self, source_path: object, target_path: object, reset_scroll: bool = False) -> None:
        if self.inspector is None or self.inspector_window is None or not self.inspector_window.winfo_exists():
            return
        source = source_path if isinstance(source_path, Path) else None
        target = target_path if isinstance(target_path, Path) else None
        if source is None and target is None:
            return
        self.inspector.load_pair(source, target, reset_scroll=reset_scroll)

    def _on_qc_file_select(self, _event=None) -> None:
        info = self.qc_file_comparison
        if not info:
            return
        self._load_qc_pair_into_inspector(info.get("source_path"), info.get("target_path"))

    def _on_qc_folder_select(self, _event=None) -> None:
        self._sync_qc_folder_update_selected_button()
        selected = self.qc_folder_table.selection()
        if not selected:
            return
        row = self.qc_folder_rows.get(selected[0])
        if row is None:
            return
        self._load_qc_pair_into_inspector(row.get("source_path"), row.get("target_path"))

    def _bind_context_menu(self, widget: tk.Misc, handler) -> None:
        for sequence in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            widget.bind(sequence, handler)

    def _reveal_path_in_finder(self, path: object) -> None:
        if not isinstance(path, Path) or not path.exists():
            messagebox.showinfo(APP_NAME, "File not found on disk.")
            return
        subprocess.Popen(["open", "-R", str(path)])

    def _popup_reveal_menu(self, event, source_path: object, target_path: object, source_label: str = "Source", target_label: str = "Target") -> None:
        source = source_path if isinstance(source_path, Path) else None
        target = target_path if isinstance(target_path, Path) else None
        if source is None and target is None:
            return
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(
            label=f"Reveal {source_label} in Finder",
            command=lambda: self._reveal_path_in_finder(source),
            state=tk.NORMAL if source is not None else tk.DISABLED,
        )
        menu.add_command(
            label=f"Reveal {target_label} in Finder",
            command=lambda: self._reveal_path_in_finder(target),
            state=tk.NORMAL if target is not None else tk.DISABLED,
        )
        menu.add_separator()
        for label, path in ((source_label, source), (target_label, target)):
            menu.add_command(
                label=f"Open {label} in {REPAIR_APP_NAME}",
                command=lambda selected=path: self._open_in_wave_repair([selected]),
                state=tk.NORMAL if path is not None and path.is_file() else tk.DISABLED,
            )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_in_wave_repair(self, paths: list[object]) -> None:
        """Hands files to the separate Wave Repair for Post app — Wave
        Manager itself never repairs anything."""
        files = [path for path in paths if isinstance(path, Path) and path.is_file()]
        if not files:
            messagebox.showinfo(APP_NAME, "File not found on disk.")
            return
        frozen = bool(getattr(sys, "frozen", False))
        source_text = self.source_var.get().strip()
        source_root = Path(source_text).expanduser() if source_text else None
        write_handoff(files, source_root if source_root is not None and source_root.is_dir() else None)
        command = repair_launch_command(files, frozen, Path(sys.executable), MODULE_DIR)
        if not frozen and not Path(command[1]).is_file():
            messagebox.showerror(APP_NAME, f"{REPAIR_APP_NAME} was not found next to this script:\n{command[1]}")
            return
        try:
            if frozen:
                result = subprocess.run(command, capture_output=True, text=True, timeout=20)
                if result.returncode != 0:
                    messagebox.showerror(APP_NAME, f"Couldn't open {REPAIR_APP_NAME} — put it next to {APP_NAME} or in Applications.\n\n{result.stderr.strip()}")
                    return
            else:
                subprocess.Popen(command)
        except (OSError, subprocess.TimeoutExpired) as exc:
            messagebox.showerror(APP_NAME, f"Couldn't open {REPAIR_APP_NAME}: {exc}")
            return
        self.append_log(f"Opened in {REPAIR_APP_NAME}: " + ", ".join(path.name for path in files))

    def _on_browse_file_context_menu(self, event) -> None:
        row_id = self.browse_file_list.identify_row(event.y)
        if not row_id:
            return
        self.browse_file_list.selection_set(row_id)
        path = self.browse_file_list_paths.get(row_id)
        if path is None:
            return
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="Reveal in Finder", command=lambda: self._reveal_path_in_finder(path))
        menu.add_command(label=f"Open in {REPAIR_APP_NAME}", command=lambda: self._open_in_wave_repair([path]))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_preview_context_menu(self, event) -> None:
        row_id = self.table.identify_row(event.y)
        if not row_id:
            return
        self.table.selection_set(row_id)
        job = self.preview_jobs.get(row_id)
        if job is None:
            return
        self._popup_reveal_menu(event, job.source, job.target)

    def _on_qc_file_context_menu(self, event) -> None:
        info = self.qc_file_comparison or {}
        self._popup_reveal_menu(event, info.get("source_path"), info.get("target_path"))

    def _on_qc_folder_context_menu(self, event) -> None:
        row_id = self.qc_folder_table.identify_row(event.y)
        if not row_id:
            return
        self.qc_folder_table.selection_set(row_id)
        row = self.qc_folder_rows.get(row_id)
        if row is None:
            return
        self._popup_reveal_menu(event, row.get("source_path"), row.get("target_path"))

    def _on_qc_poly_context_menu(self, event) -> None:
        row_id = self.qc_poly_table.identify_row(event.y)
        if not row_id:
            return
        self.qc_poly_table.selection_set(row_id)
        row = self.qc_poly_rows.get(row_id)
        if row is None:
            return
        self._popup_reveal_menu(event, row.get("poly_path"), row.get("split_path"), source_label="Poly File", target_label="Split File")

    def _load_selected_qc_row_in_inspector(self, reset_scroll: bool = False) -> None:
        """Whichever QC sub-type is currently showing, load its selected
        row into the Inspector — the single shared entry point for the top
        toolbar's Audio Inspector button and every QC table's own selection
        binding, so there's exactly one Inspector button in the app instead
        of a QC-only duplicate of the shared one."""
        if self.inspector is None:
            return
        qc_type = self.qc_type_var.get()
        if qc_type == "Poly ↔ Split":
            selected = self.qc_poly_table.selection()
            if selected and selected[0] in self.qc_poly_rows:
                self.inspector.load_qc_mapping(self.qc_poly_rows[selected[0]], reset_scroll=reset_scroll)
        elif qc_type == "Folder ↔ Folder":
            selected = self.qc_folder_table.selection()
            row = self.qc_folder_rows.get(selected[0]) if selected else None
            if row is not None:
                self._load_qc_pair_into_inspector(row.get("source_path"), row.get("target_path"), reset_scroll=reset_scroll)
        else:
            info = self.qc_file_comparison
            if info:
                self._load_qc_pair_into_inspector(info.get("source_path"), info.get("target_path"), reset_scroll=reset_scroll)

    def choose_output(self) -> None:
        source_root = Path(self.source_var.get()).expanduser()
        output_root = resolve_output_root(source_root, self.output_var.get()) if source_root.is_dir() else None
        fallback = self.last_output_dialog_dir or (source_root.parent if source_root.is_dir() else None) or self.last_source_dialog_dir
        initialdir = self._existing_dialog_dir(str(output_root or self.output_var.get()), fallback)
        path = filedialog.askdirectory(title="Choose output or target root", initialdir=initialdir)
        if path:
            self.last_output_dialog_dir = Path(path)
            self.output_var.set(path)
            self._on_output_var_changed()

    def choose_sox(self) -> None:
        path = filedialog.askopenfilename(title="Choose sox executable")
        if path:
            self.sox_var.set(path)

    def choose_soxi(self) -> None:
        path = filedialog.askopenfilename(title="Choose soxi executable")
        if path:
            self.soxi_var.set(path)

    def choose_bwf(self) -> None:
        path = filedialog.askopenfilename(title="Choose bwfmetaedit executable")
        if path:
            self.bwf_var.set(path)

    def _ensure_inspector_exists(self) -> bool:
        """Create the Inspector window if it doesn't exist yet; leaves it
        exactly where it is (front, back, wherever the user put it) if it
        already does. Returns whether it already existed, so a caller can
        tell a fresh open apart from a live-refresh of one already up."""
        inspector_existed = self.inspector_window is not None and self.inspector_window.winfo_exists()
        if not inspector_existed:
            self.inspector = AudioInspector(self)
            self.inspector_window = self.inspector.window
        return inspector_existed

    def open_inspector(self) -> None:
        inspector_existed = self._ensure_inspector_exists()
        self.inspector_window.deiconify()
        self.inspector_window.lift()
        self.inspector_window.focus_force()
        if self.mode_var.get() == "QC":
            self._load_selected_qc_row_in_inspector(reset_scroll=not inspector_existed)
        else:
            self._load_selected_preview_in_inspector()

    def _toggle_inspector(self) -> None:
        # Only the keyboard shortcut toggles — the "Audio Inspector"
        # toolbar button keeps its existing always-show-and-refresh
        # behavior unchanged, since that's a deliberate click, not a quick
        # "get this out of my way" keystroke. A withdrawn (not destroyed)
        # window still counts as "existing" for _ensure_inspector_exists(),
        # so the next toggle-on cleanly deiconifies the same instance
        # (still showing whatever it last had loaded) rather than
        # rebuilding it from scratch the way the window's own close button
        # (AudioInspector.close(), a real destroy) does.
        if self.inspector_window is not None and self.inspector_window.winfo_exists() and self.inspector_window.state() != "withdrawn":
            self.inspector_window.withdraw()
        else:
            self.open_inspector()

    def _toggle_log_window(self) -> None:
        if self.log_window.window.state() != "withdrawn":
            self.log_window.window.withdraw()
        else:
            self.log_window.show()

    def _on_preview_select(self, _event=None) -> None:
        if self.inspector is not None and self.inspector_window is not None and self.inspector_window.winfo_exists():
            self._load_selected_preview_in_inspector()

    def _load_selected_preview_in_inspector(self) -> None:
        if self.inspector is None:
            return
        selected = self.table.selection()
        if selected:
            job = self.preview_jobs.get(selected[0])
            if job is not None:
                self.inspector.load_job(job)

    def check_tools(self) -> None:
        tools = (
            ("SoX", self.sox_var.get().strip(), ["--version"]),
            ("Soxi", self.soxi_var.get().strip(), ["--help"]),
            ("BWF MetaEdit", self.bwf_var.get().strip(), ["--version"]),
        )
        self.append_log("=== Tool Check ===")
        missing = []
        results = []
        for name, executable, args in tools:
            exists = bool(executable) and (Path(executable).exists() or shutil.which(executable))
            version = sox_version(executable, args) if exists and name in ("SoX", "Soxi") else (tool_version(executable, args) if exists else "missing")
            # A path that merely exists on disk (a WAV file, a folder, ...)
            # isn't necessarily a working tool — only report OK once it's
            # both found *and* actually ran without error.
            ok = exists and not version.startswith("error:")
            result = f"{name}: {'OK' if ok else 'MISSING'} | {executable or '(not set)'} | {version}"
            self.append_log(result)
            results.append(result)
            if not ok:
                missing.append(name)
        self.tool_check_var.set("\n".join(results))
        self.status_var.set("Tools OK" if not missing else "Missing required tools: " + ", ".join(missing))

    def open_install_command(self) -> None:
        script_path = Path("/tmp/sox_metadata_workflow_install.command")
        script_path.write_text(
            "#!/bin/zsh\nclear\necho 'Suggested install/update commands:'\necho\ncat <<'EOF'\n"
            + INSTALL_COMMAND_TEXT
            + "\nEOF\necho\necho 'Review the commands above. Copy/paste the line you want to run.'\necho 'This shell will stay open; type exit when finished.'\necho\nexec /bin/zsh -l\n",
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        subprocess.Popen(["open", "-a", "Terminal", str(script_path)])

    def append_log(self, text: str) -> None:
        self.log_frame.append(text)

    def _drop_files_into_list(self, event) -> str:
        """Drop one or more WAV files directly onto the preview table in
        Split, Rename or Combine mode to add just those files to the list —
        mirrors Wave Agent's own "drop what you want to work on" flow, and
        is how a selection is brought over from Soundminer (with its
        Reference Original preference on, a drag hands over the original
        files). Same rules as a Soundminer "Send to": see
        _add_files_to_list()."""
        mode = self.mode_var.get()
        if mode not in SEND_MODE_LABELS:
            return "break"
        if self.active_operation_phase is not None:
            self.status_var.set(f"Busy ({self.active_operation_phase}) — wait for it to finish before importing more files.")
            self.append_log(self.status_var.get())
            return "break"
        dropped_paths = [Path(path).expanduser() for path in self.table.tk.splitlist(event.data)]
        if dropped_paths:
            self._add_files_to_list(dropped_paths, mode, "Dropped")
        return "break"

    def _import_dropped_files(self, wav_paths: list[Path], purpose: str = "import") -> None:
        # Preview/Execute already guard against overlapping runs by
        # disabling their own buttons while active — but a drop lands on
        # the table itself, bypassing that. Without this check, dropping
        # files while a real Preview scan is still running in the
        # background reaches _show_preview() below, which would think that
        # background scan just finished (clearing active_operation_phase
        # and disabling Pause/Cancel) while it's actually still running
        # unattended and now uncancellable.
        if self.active_operation_phase is not None:
            self.status_var.set(f"Busy ({self.active_operation_phase}) — wait for it to finish before importing more files.")
            self.append_log(self.status_var.get())
            return
        mode = self.mode_var.get()
        source_text = self.source_var.get().strip()
        if source_text:
            source_root = Path(source_text).expanduser()
            outside = [path for path in wav_paths if not path.is_relative_to(source_root)]
            if outside:
                messagebox.showerror(
                    APP_NAME,
                    "Dropped file(s) must be inside the current Source folder:\n" + "\n".join(str(path) for path in outside[:5]),
                )
                return
        else:
            # First drop with no Source set yet — adopt the dropped file(s)'
            # own common folder as Source, the same way it would be if you'd
            # picked that folder yourself, so the rest of the app (Target
            # resolution, rel_target math, a later ordinary folder Preview)
            # keeps working exactly as it already does everywhere else.
            source_root = wav_paths[0].parent if len(wav_paths) == 1 else Path(os.path.commonpath([str(path.parent) for path in wav_paths]))
            self.source_var.set(str(source_root))
        output_root = resolve_output_root(source_root, self.output_var.get()) or source_root.parent / "__preview_output__"
        bwfmetaedit = self.bwf_var.get().strip()
        soxi = self.soxi_var.get().strip()
        # Every Tk variable is read here, on the main thread; the worker below
        # only gets plain values. It runs in the background with the same
        # progress, Log lines and Pause/Cancel as a full Preview — importing
        # (or rescanning) dozens of files runs soxi + BWF MetaEdit on each,
        # and used to freeze the window with no progress at all.
        try:
            if mode.startswith("Combine"):
                build_args = dict(
                    kind="combine", source_root=source_root, output_root=output_root,
                    combine_args=(
                        self.conflict_policy_var.get(), self.combine_copy_non_combined_var.get(), None, soxi,
                        self.speaker_pattern_var.get(), DEFAULT_SPEAKER_MAPPING, self.merge_layout_var.get(),
                        self.merge_order_var.get(), self.merge_custom_order_var.get(), self.merge_rename_var.get(),
                        self.merge_include_format_var.get(), self.merge_include_layout_var.get(),
                        self.merge_rename_position_var.get(), self.merge_custom_layout_name_var.get(),
                    ),
                )
            elif mode.startswith("Split"):
                output_formula = self.output_formula_var.get().strip() or "{base}.A$"
                if "/" in output_formula or ":" in output_formula:
                    raise ValueError("Output name formula cannot contain path separators.")
                build_args = dict(
                    kind="split", source_root=source_root, output_root=output_root, bwfmetaedit=bwfmetaedit, soxi=soxi,
                    name_mode=self.name_mode_var.get(), output_formula=output_formula, conflict_policy=self.conflict_policy_var.get(),
                    copy_mono=self.split_copy_mono_var.get(),
                )
            else:
                rename_output_root = resolve_output_root(source_root, self.output_var.get())
                rename_rules = [
                    RenameRule(find_var.get(), replace_var.get(), self.rename_regex_var.get())
                    for find_var, replace_var in zip(self.rename_find_vars, self.rename_replace_vars)
                    if find_var.get()
                ] if self.rename_find_replace_enabled_var.get() else []
                build_args = dict(
                    kind="rename", source_root=source_root, output_root=rename_output_root or source_root,
                    rename_args=(
                        source_root,
                        self.source_formula_var.get(),
                        DEFAULT_OUTPUT_FORMULA,
                        self.rename_number_mode_var.get(),
                        bwfmetaedit,
                        rename_output_root,
                        False,
                        self.rename_change_number_var.get(),
                    ),
                    rename_kwargs=dict(
                        rename_rules=rename_rules,
                        serial_ids=self.rename_serial_ids,
                        serial_enabled=self.rename_serial_enabled_var.get(),
                        serial_prefix=self.rename_serial_prefix_var.get(),
                        serial_digits=int(self.rename_serial_digits_var.get()),
                        serial_start=int(self.rename_serial_start_var.get()),
                        output_formula=self.rename_formula_var.get() if self.rename_apply_formula_var.get() else "",
                        text_position=self.rename_text_position_var.get(),
                        serial_order=self.rename_serial_order_var.get(),
                        metadata_template=self.rename_metadata_template_var.get() if self.rename_metadata_template_enabled_var.get() else "",
                    ),
                )
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Unable to import dropped file(s): {exc}")
            return
        verb = "Rescanning" if purpose == "rescan" else "Importing"
        self.status_var.set(f"{verb} {len(wav_paths)} file(s)...")
        self.append_log(self.status_var.get())
        self.preview_button.configure(state=tk.DISABLED)
        self.execute_button.configure(state=tk.DISABLED)
        self.pause_requested.clear()
        self.cancel_requested.clear()
        self.active_operation_phase = "preview"
        self._set_operation_controls(operation_control_capabilities("preview"))
        self._update_structure_block_buttons()
        self.log_window.show()
        threading.Thread(target=self._import_worker, args=(list(wav_paths), build_args, purpose, time.monotonic()), daemon=True).start()

    def _import_worker(self, wav_paths: list[Path], build_args: dict, purpose: str, started: float) -> None:
        label = "Rescan" if purpose == "rescan" else "Import"

        def report(index: int, total: int, source: Path, made: int, status: str) -> None:
            wait_for_preview_control(self.pause_requested, self.cancel_requested)
            self.events.put(("progress", ProgressEvent("split_preview", index, total, status, source, started=started, label=label, detail_label="jobs", detail_value=made)))

        try:
            if build_args["kind"] == "combine":
                conflict_policy, copy_non_combined, _progress, *rest = build_args["combine_args"]
                new_jobs = build_combine_jobs(
                    build_args["source_root"], build_args["output_root"], conflict_policy, copy_non_combined,
                    lambda index, total, source, made, status: report(index, total, source, made, status),
                    *rest, only_files=wav_paths,
                )
            elif build_args["kind"] == "split":
                new_jobs = build_split_jobs(
                    build_args["source_root"], build_args["output_root"], build_args["bwfmetaedit"], build_args["soxi"],
                    build_args["name_mode"], build_args["output_formula"], build_args["conflict_policy"], report,
                    only_files=wav_paths, copy_mono=build_args["copy_mono"],
                )
            else:
                kwargs = build_args["rename_kwargs"]
                new_jobs = build_rename_jobs(
                    *build_args["rename_args"],
                    None,
                    kwargs["rename_rules"],
                    kwargs["serial_ids"],
                    kwargs["serial_enabled"],
                    kwargs["serial_prefix"],
                    kwargs["serial_digits"],
                    kwargs["serial_start"],
                    kwargs["output_formula"],
                    kwargs["text_position"],
                    kwargs["serial_order"],
                    metadata_template=kwargs["metadata_template"],
                    only_files=wav_paths,
                )
            wait_for_preview_control(self.pause_requested, self.cancel_requested)
            self.events.put(("import", (build_args["source_root"], build_args["output_root"], wav_paths, new_jobs, purpose, started)))
        except PreviewCancelled:
            self.events.put(("preview_cancelled", None))
        except Exception as exc:
            self.events.put(("error", f"Unable to import dropped file(s): {exc}"))

    def _apply_imported_jobs(self, source_root: Path, output_root: Path, wav_paths: list[Path], new_jobs: list[Job], purpose: str, started: float) -> None:
        # Accumulate: a file already in the list (dropped again, rescanned,
        # or already there from a folder Preview) gets its jobs refreshed in
        # place; anything new is appended, so multiple drops build up one
        # list instead of each drop replacing the last.
        self._show_preview(source_root, output_root, merge_refreshed_jobs(self.jobs, new_jobs), started)
        if purpose == "rescan":
            refreshed = set(wav_paths)
            still = sum(1 for path in self._structure_blocked_sources() if path in refreshed)
            self.status_var.set(f"Rescanned {len(wav_paths)} blocked file(s): {len(wav_paths) - still} now ready to process, {still} still blocked.")
        else:
            note, self._import_note = self._import_note, ""
            self.status_var.set(f"Imported {len(wav_paths)} file(s) into the list." + (f" ({note})" if note else ""))
        self.append_log(self.status_var.get())

    def preview(self) -> None:
        source_text = self.source_var.get().strip()
        if not source_text:
            messagebox.showwarning(APP_NAME, "Choose a source folder before previewing.")
            self._update_preview_button_state()
            return
        source_root = Path(source_text).expanduser()
        if not source_root.is_dir():
            messagebox.showerror(APP_NAME, "Choose a valid source folder before previewing.")
            self._update_preview_button_state()
            return
        split_mode = self.mode_var.get().startswith("Split")
        rename_mode = self.mode_var.get().startswith("Rename")
        combine_mode = self.mode_var.get().startswith("Combine")
        output_root = resolve_output_root(source_root, self.output_var.get()) or source_root.parent / "__preview_output__"
        if not source_root.is_dir() or (not split_mode and not rename_mode and not combine_mode and not output_root.is_dir()):
            messagebox.showerror(APP_NAME, "Choose a valid source folder" + (" and target folder." if not split_mode else "."))
            return
        self.preview_button.configure(state=tk.DISABLED)
        self.execute_button.configure(state=tk.DISABLED)
        self.pause_requested.clear()
        self.cancel_requested.clear()
        self.active_operation_phase = "preview"
        self._set_operation_controls(operation_control_capabilities("preview"))
        self.status_var.set("Building preview...")
        self.log_window.show()
        threading.Thread(target=self._preview_worker, args=(source_root, output_root, time.monotonic()), daemon=True).start()

    def _preview_worker(self, source_root: Path, output_root: Path, preview_started: float) -> None:
        try:
            mode = self.mode_var.get()
            wait_for_preview_control(self.pause_requested, self.cancel_requested)
            if mode.startswith("Split"):
                def report(index: int, total: int, source: Path, made: int, status: str) -> None:
                    wait_for_preview_control(self.pause_requested, self.cancel_requested)
                    self.events.put(("progress", ProgressEvent("split_preview", index, total, status, source, started=preview_started, label="Preview", detail_label="jobs", detail_value=made)))

                output_formula = self.output_formula_var.get().strip() or "{base}.A$"
                if "/" in output_formula or ":" in output_formula:
                    raise ValueError("Output name formula cannot contain path separators.")
                jobs = build_split_jobs(
                    source_root,
                    output_root,
                    self.bwf_var.get(),
                    self.soxi_var.get(),
                    self.name_mode_var.get(),
                    output_formula,
                    self.conflict_policy_var.get(),
                    report,
                    copy_mono=self.split_copy_mono_var.get(),
                )
            else:
                progress_label = "Re-embed preview"
                stage_starts: dict[str, float] = {}
                phase_totals: dict[str, int] = {}

                def report(index: int, total: int, source: Path, trk_number: int, status: str) -> None:
                    wait_for_preview_control(self.pause_requested, self.cancel_requested)
                    now = time.monotonic()
                    stage_key = status if status in ("Scanning targets", "Scanning sources", "Reading source metadata") else "Building jobs"
                    stage_starts.setdefault(stage_key, now)
                    phase_totals[stage_key] = total
                    elapsed = now - preview_started
                    if progress_label == "Re-embed preview":
                        if stage_key == "Scanning targets":
                            overall_index = index
                            overall_total = total
                        elif stage_key == "Scanning sources":
                            target_total = phase_totals.get("Scanning targets", 0)
                            overall_index = target_total + index
                            overall_total = target_total + total
                        elif stage_key == "Reading source metadata":
                            target_total = phase_totals.get("Scanning targets", 0)
                            source_total = phase_totals.get("Scanning sources", 0)
                            overall_index = target_total + source_total + index
                            overall_total = target_total + source_total + total
                        else:
                            target_total = phase_totals.get("Scanning targets", 0)
                            source_total = phase_totals.get("Scanning sources", 0)
                            metadata_total = phase_totals.get("Reading source metadata", 0)
                            overall_index = target_total + source_total + metadata_total + index
                            overall_total = target_total + source_total + metadata_total + total
                        eta = (elapsed / overall_index) * (overall_total - overall_index) if overall_index else 0
                    else:
                        overall_index = index
                        overall_total = total
                        stage_elapsed = now - stage_starts[stage_key]
                        eta = (stage_elapsed / index) * (total - index) if index else 0
                    self.events.put(("progress", ProgressEvent("workflow_preview", index, total, status, source, started=preview_started, label=progress_label, detail_label="Trk#", detail_value=trk_number, overall_index=overall_index, overall_total=overall_total)))

                if mode.startswith("Combine"):
                    progress_label = "Combine preview"
                    jobs = build_combine_jobs(
                        source_root,
                        output_root,
                        self.conflict_policy_var.get(),
                        self.combine_copy_non_combined_var.get(),
                        report,
                        self.soxi_var.get(),
                        self.speaker_pattern_var.get(),
                        DEFAULT_SPEAKER_MAPPING,
                        self.merge_layout_var.get(),
                        self.merge_order_var.get(),
                        self.merge_custom_order_var.get(),
                        self.merge_rename_var.get(),
                        self.merge_include_format_var.get(),
                        self.merge_include_layout_var.get(),
                        self.merge_rename_position_var.get(),
                        self.merge_custom_layout_name_var.get(),
                    )
                elif mode.startswith("Rename"):
                    progress_label = "Rename preview"
                    rename_output_root = resolve_output_root(source_root, self.output_var.get())
                    rename_rules = [
                        RenameRule(find_var.get(), replace_var.get(), self.rename_regex_var.get())
                        for find_var, replace_var in zip(self.rename_find_vars, self.rename_replace_vars)
                        if find_var.get()
                    ] if self.rename_find_replace_enabled_var.get() else []
                    serial_signature = (
                        str(source_root.resolve()).casefold(),
                        self.rename_serial_order_var.get(),
                        self.rename_serial_prefix_var.get(),
                        self.rename_serial_digits_var.get(),
                        self.rename_serial_start_var.get(),
                    )
                    serial_map = self.rename_serial_ids if serial_signature == self.rename_serial_signature else {}
                    jobs = build_rename_jobs(
                        source_root,
                        self.source_formula_var.get(),
                        DEFAULT_OUTPUT_FORMULA,
                        self.rename_number_mode_var.get(),
                        self.bwf_var.get(),
                        rename_output_root,
                        False,
                        self.rename_change_number_var.get(),
                        report,
                        rename_rules,
                        serial_map,
                        self.rename_serial_enabled_var.get(),
                        self.rename_serial_prefix_var.get(),
                        int(self.rename_serial_digits_var.get()),
                        int(self.rename_serial_start_var.get()),
                        self.rename_formula_var.get() if self.rename_apply_formula_var.get() else "",
                        self.rename_text_position_var.get(),
                        self.rename_serial_order_var.get(),
                        metadata_template=self.rename_metadata_template_var.get() if self.rename_metadata_template_enabled_var.get() else "",
                    )
                    self.rename_serial_ids = serial_map
                    self.rename_serial_signature = serial_signature
                    output_root = rename_output_root or source_root
                else:
                    rename_formula = self.reembed_rename_formula_var.get() if self.rename_after_embed_var.get() else ""
                    jobs = build_reembed_jobs(
                        source_root,
                        output_root,
                        rename_formula,
                        self.rename_number_mode_var.get(),
                        self.bwf_var.get(),
                        report,
                        self.conflict_policy_var.get(),
                    )
            wait_for_preview_control(self.pause_requested, self.cancel_requested)
            self.events.put(("preview", (source_root, output_root, jobs, preview_started)))
        except PreviewCancelled:
            self.events.put(("preview_cancelled", None))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _show_preview(self, source_root: Path, output_root: Path, jobs: list[Job], preview_started: Optional[float] = None) -> None:
        self.current_source_root = source_root
        self.current_output_root = output_root
        self.jobs = jobs
        self._metadata_scan_current = self.mode_var.get().startswith("Rename") and self._serial_metadata_order_active()
        self._metadata_template_scan_current = self.mode_var.get().startswith("Rename") and self.rename_metadata_template_enabled_var.get()
        self.table.delete(*self.table.get_children())
        self.preview_jobs.clear()
        ready = 0
        total_jobs = len(jobs)
        for index, job in enumerate(jobs, start=1):
            ready += int(job_is_executable(job))
            if job.mode == "browse":
                # Browse mode's own "source_root" is just a common-ancestor
                # guess that shifts as more folders get added — showing the
                # full path here instead means two different folders'
                # same-named files never look identical or ambiguous.
                source_label = str(job.source) if job.source is not None else "-"
            else:
                source_label = str(job.source.relative_to(source_root)) if job.source is not None and job.source.is_relative_to(source_root) else (str(job.source) if job.source is not None else "-")
            if job.mode in ("rename", "rename_copy") and job.source is not None:
                before_label = job.source.name
            else:
                before_label = str(job.target.relative_to(output_root)) if job.target and job.target.is_relative_to(output_root) else (str(job.target) if job.target else "-")
            planned_filename = job.final_target.name if job.final_target else (job.target.name if job.target else "-")
            tape_label = job.serial_tape or "-"
            tc_label = job.serial_tc_display or "-"
            scene_label = job.serial_scene or "-"
            take_label = job.serial_take or "-"
            iid = self.table.insert("", tk.END, values=(
                source_label, job.channel or "-", job.trk_number or "-", job.stream_number or "-",
                before_label, planned_filename, tape_label, tc_label, scene_label, take_label, display_status(
                    job,
                    self.name_mode_var.get(),
                    self.rename_number_mode_var.get(),
                    self.rename_apply_formula_var.get(),
                    self.rename_change_number_var.get(),
                    self.rename_find_replace_enabled_var.get(),
                    self.rename_serial_enabled_var.get(),
                    self._metadata_template_status_label(job),
                ),
            ), tags=("default",))
            self.preview_jobs[iid] = job
            if preview_started is not None and total_jobs and (index == 1 or index == total_jobs or index % 500 == 0):
                elapsed = time.monotonic() - preview_started
                eta = (elapsed / index) * (total_jobs - index) if index else 0
                self.status_var.set(f"Rendering preview rows {index}/{total_jobs} | elapsed {self._duration(elapsed)} | ETA {self._duration(eta)}")
                self.root.update_idletasks()
        if self.preview_sort_column is not None:
            self._sort_preview_table(self.preview_sort_column, preserve_direction=True)
        summary = f"Preview: {ready} ready; {len(jobs) - ready} blocked/skipped."
        unpaired_targets = [job.target for job in jobs if job.mode == "reembed" and job.source is None and job.target is not None]
        unpaired_sources = [job.source for job in jobs if job.mode == "reembed" and job.target is None and job.source is not None]
        if unpaired_targets or unpaired_sources:
            summary += f" Unpaired: {len(unpaired_targets)} target(s) without a source, {len(unpaired_sources)} source(s) without a target (see Log)."
        if preview_started is not None:
            elapsed = time.monotonic() - preview_started
            summary += f" elapsed {self._duration(elapsed)} | ETA {self._duration(0)}"
        self.status_var.set(summary)
        self.append_log(self.status_var.get())
        for label, paths in (("Target(s) without a source", unpaired_targets), ("Source(s) without a target", unpaired_sources)):
            if paths:
                self.append_log(f"{label}: {len(paths)}")
                for path in paths[:50]:
                    self.append_log(f"  {path.name}")
                if len(paths) > 50:
                    self.append_log(f"  ... and {len(paths) - 50} more (status column in the table)")
        self.preview_button.configure(state=tk.NORMAL)
        self.execute_button.configure(state=tk.NORMAL if ready else tk.DISABLED)
        self._update_structure_block_buttons()
        if self.active_operation_phase == "preview":
            self.active_operation_phase = None
            self._set_operation_controls(OperationControlCapabilities())

    def _sort_preview_table(self, column: str, preserve_direction: bool = False) -> None:
        if not preserve_direction:
            if self.preview_sort_column == column:
                self.preview_sort_reverse = not self.preview_sort_reverse
            else:
                self.preview_sort_column = column
                self.preview_sort_reverse = False
        rows = list(self.table.get_children(""))
        valued_rows = [iid for iid in rows if str(self.table.set(iid, column)).strip() not in ("", "-")]
        missing_rows = [iid for iid in rows if str(self.table.set(iid, column)).strip() in ("", "-")]
        valued_rows.sort(key=lambda iid: preview_cell_sort_key(column, self.table.set(iid, column)), reverse=self.preview_sort_reverse)
        rows = valued_rows + missing_rows
        for index, iid in enumerate(rows):
            self.table.move(iid, "", index)
        for table_column, label in self._table_column_labels.items():
            indicator = " ▼" if self.preview_sort_reverse else " ▲"
            self.table.heading(table_column, text=label + indicator if table_column == self.preview_sort_column else label)

    def _on_rename_option_changed(self) -> None:
        self._update_mode_ui()
        self.refresh_filename_mode()

    def _serial_metadata_order_active(self) -> bool:
        return self.rename_serial_enabled_var.get() and self.rename_serial_order_var.get() == "Metadata Tape + TC"

    def _confirm_metadata_scan(self, feature: str = "Metadata Tape + TC") -> bool:
        return messagebox.askyesno(
            APP_NAME,
            f"Switching to {feature} will refresh the list to read metadata from each file. "
            "This may take a while for large folders.\n\nContinue?",
        )

    def _on_rename_serial_enabled_changed(self) -> None:
        if self._serial_metadata_order_active() and not self._metadata_scan_current and self.source_var.get().strip():
            if not self._confirm_metadata_scan():
                self.rename_serial_enabled_var.set(False)
                return
            self._update_mode_ui()
            self.preview()
            return
        self._on_rename_option_changed()

    def _on_rename_serial_order_changed(self) -> None:
        if self._serial_metadata_order_active() and not self._metadata_scan_current and self.source_var.get().strip():
            if not self._confirm_metadata_scan():
                self.rename_serial_order_var.set("Folder Path + Filename")
                return
            self._update_mode_ui()
            self.preview()
            return
        self._on_rename_option_changed()

    def refresh_filename_mode(self) -> None:
        if not self.jobs or self.current_source_root is None or self.current_output_root is None:
            return
        if self.mode_var.get().startswith("Combine"):
            output_root = resolve_output_root(self.current_source_root, self.output_var.get()) or self.current_source_root.parent / "__preview_output__"
            selected = self.table.selection()
            selected_source = self.preview_jobs.get(selected[0]).source if selected and selected[0] in self.preview_jobs else None
            try:
                refreshed = build_combine_jobs(
                    self.current_source_root,
                    output_root,
                    self.conflict_policy_var.get(),
                    self.combine_copy_non_combined_var.get(),
                    soxi=self.soxi_var.get(),
                    speaker_pattern=self.speaker_pattern_var.get(),
                    speaker_mapping_text=DEFAULT_SPEAKER_MAPPING,
                    layout_name=self.merge_layout_var.get(),
                    order_name=self.merge_order_var.get(),
                    custom_order=self.merge_custom_order_var.get(),
                    rename_merged=self.merge_rename_var.get(),
                    rename_include_format=self.merge_include_format_var.get(),
                    rename_include_layout=self.merge_include_layout_var.get(),
                    rename_position=self.merge_rename_position_var.get(),
                    custom_layout_name=self.merge_custom_layout_name_var.get(),
                )
            except Exception as exc:
                self.status_var.set(f"Preview settings error: {exc}")
                return
            self.current_output_root = output_root
            self._show_preview(self.current_source_root, output_root, refreshed)
            if selected_source is not None:
                for iid, job in self.preview_jobs.items():
                    if job.source == selected_source:
                        self.table.selection_set(iid)
                        self.table.focus(iid)
                        break
            return
        if self.mode_var.get().startswith("Re-embed"):
            refreshed: list[Job] = []
            for job in self.jobs:
                if job.mode != "reembed" or job.source is None or job.target is None:
                    refreshed.append(job)
                    continue
                final_target = None
                status = "READY"
                if self.rename_after_embed_var.get() and self.reembed_rename_formula_var.get().strip():
                    base = job.naming_base or job.source.stem
                    rename_number = job.trk_number if self.rename_number_mode_var.get() == "Trk#" else job.stream_number
                    if not job.has_channel_suffix:
                        final_target = job.target.with_name(render_output_name_without_number(self.reembed_rename_formula_var.get(), base, job.target.suffix))
                        if final_target != job.target and final_target.exists():
                            status = split_conflict_status(final_target, self.conflict_policy_var.get())
                    elif rename_number is None:
                        status = f"BLOCKED: missing {self.rename_number_mode_var.get()}"
                    else:
                        final_target = job.target.with_name(render_output_name(self.reembed_rename_formula_var.get(), base, rename_number, job.target.suffix))
                        if final_target != job.target and final_target.exists():
                            status = split_conflict_status(final_target, self.conflict_policy_var.get())
                refreshed.append(Job(job.source, job.target, job.channel, job.trk_number, job.stream_number, status, job.mode, rel_target=job.rel_target, final_target=final_target, sources=job.sources, naming_base=job.naming_base, has_channel_suffix=job.has_channel_suffix))
            self._show_preview(self.current_source_root, self.current_output_root, refreshed)
            self.append_log(f"Refreshed re-embed rename mode: {self.rename_number_mode_var.get()}")
            return
        if self.mode_var.get().startswith("Rename"):
            if not any((
                self.rename_apply_formula_var.get(),
                self.rename_find_replace_enabled_var.get(),
                self.rename_change_number_var.get(),
                self.rename_serial_enabled_var.get(),
                self.rename_metadata_template_enabled_var.get(),
            )):
                refreshed = mark_rename_jobs_no_change(self.jobs)
                self._show_preview(self.current_source_root, self.current_output_root, refreshed)
                self.status_var.set("No change: enable Rename, Find & Replace, Change $, Serial ID, or Metadata Template.")
                self.append_log(self.status_var.get())
                self.execute_button.configure(state=tk.DISABLED)
                return
            output_root = resolve_output_root(self.current_source_root, self.output_var.get())
            try:
                rules = [
                    RenameRule(find_var.get(), replace_var.get(), self.rename_regex_var.get())
                    for find_var, replace_var in zip(self.rename_find_vars, self.rename_replace_vars)
                    if find_var.get()
                ] if self.rename_find_replace_enabled_var.get() else []
                digits = int(self.rename_serial_digits_var.get())
                start = int(self.rename_serial_start_var.get())
                serial_signature = (
                    str(self.current_source_root.resolve()).casefold(),
                    self.rename_serial_order_var.get(),
                    self.rename_serial_prefix_var.get(),
                    self.rename_serial_digits_var.get(),
                    self.rename_serial_start_var.get(),
                )
                if serial_signature != self.rename_serial_signature:
                    self.rename_serial_ids = {}
                source_pattern = rename_auto_input_pattern() if self.source_formula_var.get().casefold() == DEFAULT_SOURCE_FORMULA.casefold() else rename_input_pattern(self.source_formula_var.get())
                group_keys = [
                    f"{str(job.source.parent).casefold()}::{source_pattern.match(job.source.name)['base'].casefold()}"
                    for job in self.jobs
                    if job.source is not None and source_pattern.match(job.source.name)
                ]
                blocked_serial_groups: dict[str, str] = {}
                if self.rename_serial_enabled_var.get() and self.rename_serial_order_var.get() == "Metadata Tape + TC":
                    metadata_rows = [
                        SerialMetadata(
                            f"{str(job.source.parent).casefold()}::{source_pattern.match(job.source.name)['base'].casefold()}",
                            job.serial_tape,
                            job.serial_time_reference,
                        )
                        for job in self.jobs
                        if job.source is not None and source_pattern.match(job.source.name)
                    ]
                    ordered_keys, blocked_serial_groups = strict_metadata_group_order(metadata_rows)
                    self.rename_serial_ids, _next_serial = assign_serial_ids_in_order(ordered_keys, self.rename_serial_ids, self.rename_serial_prefix_var.get(), digits, start)
                else:
                    self.rename_serial_ids, _next_serial = assign_serial_ids(group_keys, self.rename_serial_ids, self.rename_serial_prefix_var.get(), digits, start)
                self.rename_serial_signature = serial_signature
            except (TypeError, ValueError, AttributeError) as exc:
                self.status_var.set(f"Rename settings blocked: {exc}")
                self.append_log(self.status_var.get())
                self.execute_button.configure(state=tk.DISABLED)
                return
            metadata_template = self.rename_metadata_template_var.get() if self.rename_metadata_template_enabled_var.get() else ""
            refreshed: list[Job] = []
            metadata_template_counter = 0
            for job in self.jobs:
                if job.mode not in ("rename", "rename_copy") or job.target is None:
                    refreshed.append(job)
                    continue
                source_pattern = rename_auto_input_pattern() if self.source_formula_var.get().casefold() == DEFAULT_SOURCE_FORMULA.casefold() else rename_input_pattern(self.source_formula_var.get())
                match = source_pattern.match(job.source.name)
                if not match:
                    refreshed.append(job)
                    continue
                is_poly = not match.groupdict().get("ch") and not match.groupdict().get("channel")
                matched_number = rename_number_from_match(match)
                has_numbered_suffix = bool(matched_number or match.groupdict().get("channel"))
                output_number = 0 if not has_numbered_suffix else ((job.trk_number if self.rename_number_mode_var.get() == "Trk#" else job.stream_number) if self.rename_change_number_var.get() else matched_number)
                metadata_template_counter += 1
                base = match["base"]
                metadata_template_active = False
                if metadata_template.strip() and job.metadata_template_fields:
                    expanded_base = expand_metadata_template(metadata_template, job.metadata_template_fields, job.metadata_template_trk_table, counter=metadata_template_counter)
                    base = expanded_base or base
                    metadata_template_active = bool(expanded_base)
                group_key = f"{str(job.source.parent).casefold()}::{match['base'].casefold()}"
                serial_id = self.rename_serial_ids.get(group_key, "") if self.rename_serial_enabled_var.get() else ""
                if metadata_template_active:
                    target_name = render_serial_name(
                        "{base}", base, 0, job.source.suffix, serial_id, "", self.rename_serial_position_var.get().lower(),
                    )
                else:
                    target_name = render_serial_name(
                        DEFAULT_OUTPUT_FORMULA,
                        base,
                        output_number,
                        job.source.suffix,
                        serial_id,
                        match.groupdict().get("channel") or "",
                        self.rename_serial_position_var.get().lower(),
                    )
                target_stem = Path(target_name).stem
                target_stem = apply_name_affix(
                    target_stem,
                    self.rename_formula_var.get() if self.rename_apply_formula_var.get() else "",
                    self.rename_text_position_var.get(),
                )
                target_name = apply_rename_rules(target_stem, rules) + Path(target_name).suffix
                target_parent = (output_root / job.source.relative_to(self.current_source_root).parent) if output_root else job.source.parent
                target = target_parent / target_name
                status = "UNCHANGED" if target == job.source else "READY"
                if group_key in blocked_serial_groups:
                    status = blocked_serial_groups[group_key]
                mode = "rename_copy" if output_root else "rename"
                rel_target = target.relative_to(output_root) if output_root else Path(target.name)
                refreshed.append(Job(job.source, target, job.channel, job.trk_number, job.stream_number, status, mode, rel_target=rel_target, sources=job.sources, serial_tape=job.serial_tape, serial_time_reference=job.serial_time_reference, serial_scene=job.serial_scene, serial_take=job.serial_take, serial_tc_display=job.serial_tc_display, has_channel_suffix=has_numbered_suffix, metadata_template_fields=job.metadata_template_fields, metadata_template_trk_table=job.metadata_template_trk_table))
            target_counts: dict[Path, int] = {}
            for job in refreshed:
                if job.status == "READY" and job.target is not None:
                    target_counts[job.target] = target_counts.get(job.target, 0) + 1
            source_paths = {job.source for job in refreshed}
            for job in refreshed:
                if job.status != "READY" or job.target is None:
                    continue
                if target_counts.get(job.target, 0) > 1:
                    job.status = "COLLISION"
                elif job.target.exists() and (output_root is not None or job.target not in source_paths):
                    job.status = "TARGET EXISTS"
            self._show_preview(self.current_source_root, output_root or self.current_source_root, refreshed)
            self.append_log(f"Refreshed rename plan: {self.rename_number_mode_var.get()}")
            return
        output_formula = self.output_formula_var.get().strip() or "{base}.A$"
        output_root = resolve_output_root(self.current_source_root, self.output_var.get()) or self.current_source_root.parent / "__preview_output__"
        refreshed: list[Job] = []
        for job in self.jobs:
            if job.rel_target is None or job.target is None or job.trk_number is None or job.stream_number is None:
                refreshed.append(job)
                continue
            output_number = job.trk_number if self.name_mode_var.get() == "Trk#" else job.stream_number
            rel_parent = job.source.relative_to(self.current_source_root).parent
            rel_target = rel_parent / render_output_name(output_formula, job.source.stem, output_number, job.source.suffix)
            target = output_root / rel_target
            status = split_conflict_status(target, self.conflict_policy_var.get())
            refreshed.append(Job(job.source, target, job.channel, job.trk_number, job.stream_number, status, job.mode, rel_target=rel_target))
        self.current_output_root = output_root
        self._show_preview(self.current_source_root, output_root, refreshed)
        self.append_log(f"Refreshed filename mode: {self.name_mode_var.get()}")

    def execute(self) -> None:
        self.refresh_filename_mode()
        if self.mode_var.get().startswith("Rename") and not any((
            self.rename_apply_formula_var.get(),
            self.rename_find_replace_enabled_var.get(),
            self.rename_change_number_var.get(),
            self.rename_serial_enabled_var.get(),
            self.rename_metadata_template_enabled_var.get(),
        )):
            self.status_var.set("No change: enable Rename, Find & Replace, Change $, Serial ID, or Metadata Template.")
            self.append_log(self.status_var.get())
            self.execute_button.configure(state=tk.DISABLED)
            return
        # Built for every mode (not just Combine) so a job that never got
        # executed — NO TARGET, BLOCKED, AMBIGUOUS TARGET, etc. — still shows
        # up in the saved processing log with its actual status, instead of
        # silently vanishing and leaving the log looking like a smaller,
        # fully-successful batch than what was actually previewed.
        planning_rows = [
            processing_report_row(
                job,
                display_status(
                    job,
                    self.name_mode_var.get(),
                    self.rename_number_mode_var.get(),
                    self.rename_apply_formula_var.get(),
                    self.rename_change_number_var.get(),
                    self.rename_find_replace_enabled_var.get(),
                    self.rename_serial_enabled_var.get(),
                    self._metadata_template_status_label(job),
                ),
            )
            for job in self.jobs
        ]
        ready_planning_rows = [row for job, row in zip(self.jobs, planning_rows) if job_is_actionable(job)]
        blocked_planning_rows = [row for job, row in zip(self.jobs, planning_rows) if not job_is_actionable(job)]
        ready_jobs = [job for job in self.jobs if job_is_actionable(job)]
        if not ready_jobs:
            return
        if ready_jobs[0].mode in ("split", "combine", "merge"):
            source_root = Path(self.source_var.get())
            output_root = resolve_output_root(source_root, self.output_var.get())
            if output_root is None:
                chosen = filedialog.askdirectory(title="Choose output root folder")
                if not chosen:
                    return
                output_root = Path(chosen)
            ready_jobs = [
                Job(job.source, output_root / job.rel_target, job.channel, job.trk_number, job.stream_number, job.status, job.mode, rel_target=job.rel_target, sources=job.sources, combine_kind=job.combine_kind, speaker_order=job.speaker_order, channel_mask=job.channel_mask)
                for job in ready_jobs
            ]
        if not messagebox.askokcancel(APP_NAME, f"Execute {len(ready_jobs)} ready job(s)?"):
            return
        self.pause_requested.clear()
        self.cancel_requested.clear()
        self._conflict_apply_all_decision = None
        self.active_operation_phase = "execute"
        self._set_operation_controls(operation_control_capabilities("execute", {job.mode for job in ready_jobs}))
        self.preview_button.configure(state=tk.DISABLED)
        self.execute_button.configure(state=tk.DISABLED)
        self.log_window.show()
        threading.Thread(target=self._execute_worker, args=(ready_jobs, Path(self.source_var.get()), planning_rows, ready_planning_rows, blocked_planning_rows), daemon=True).start()

    def _set_operation_controls(self, capabilities: OperationControlCapabilities) -> None:
        self.active_control_capabilities = capabilities
        self.pause_button.configure(state=tk.NORMAL if capabilities.pause else tk.DISABLED, text="Pause")
        self.cancel_button.configure(state=tk.NORMAL if capabilities.cancel else tk.DISABLED)
        busy = capabilities.pause or capabilities.cancel
        self.header_status_label.configure(foreground="#b91c1c" if busy else "#111827")

    def toggle_pause(self) -> None:
        if not self.active_control_capabilities.pause:
            return
        if self.pause_requested.is_set():
            self.pause_requested.clear()
            self.pause_button.configure(text="Pause")
            self.append_log(f"{(self.active_operation_phase or 'Processing').title()} resumed.")
        else:
            self.pause_requested.set()
            self.pause_button.configure(text="Resume")
            self.append_log("Pause requested. Current command will finish first.")

    def cancel_processing(self) -> None:
        if not self.active_control_capabilities.cancel:
            return
        self.cancel_requested.set()
        self.pause_requested.clear()
        self.pause_button.configure(text="Pause", state=tk.DISABLED)
        self.cancel_button.configure(state=tk.DISABLED)
        self.append_log(f"Cancel requested. Current {self.active_control_capabilities.boundary} will finish first.")

    @staticmethod
    def _duration(seconds: float) -> str:
        seconds = max(0.0, seconds)
        if seconds < 60:
            return "<0.1s" if 0 < seconds < 0.1 else f"{seconds:.1f}s"
        seconds_int = int(seconds)
        minutes, seconds = divmod(seconds_int, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}m{seconds:02d}s"

    def _execute_worker(self, jobs: list[Job], source_root: Path, planning_rows: Optional[list[dict[str, str]]] = None, ready_planning_rows: Optional[list[dict[str, str]]] = None, blocked_planning_rows: Optional[list[dict[str, str]]] = None) -> None:
        if jobs and jobs[0].mode in ("rename", "rename_copy"):
            def report(index: int, total: int, status: str, elapsed: float, eta: float, job: Job) -> None:
                self.events.put(("progress", ProgressEvent("execute", index, total, status, job.target or job.source, elapsed=elapsed, eta=eta)))

            # execute_rename_jobs_stepwise() only covers `jobs` (the ready
            # subset) — planning_rows would duplicate those with their stale
            # pre-execution status, so only the blocked/skipped rows get
            # prepended here, not the full planning_rows.
            rows = execute_rename_jobs_stepwise(jobs, report, self.pause_requested, self.cancel_requested)
            self.events.put(("done", ((blocked_planning_rows or []) + rows, source_root)))
            return

        rows: list[dict[str, str]] = list(planning_rows or [])
        pending_reembed_renames: list[tuple[Job, dict[str, str]]] = []
        source_cache: dict[Path, str] = {}
        started = time.monotonic()

        def mark_cancelled(from_index: int) -> None:
            for skipped_index, skipped in enumerate(jobs[from_index - 1:], start=from_index - 1):
                if ready_planning_rows:
                    ready_planning_rows[skipped_index]["status"] = "CANCELLED"
                else:
                    rows.append(processing_report_row(skipped, "CANCELLED"))

        for index, job in enumerate(jobs, start=1):
            while self.pause_requested.is_set() and not self.cancel_requested.is_set():
                time.sleep(0.2)
            if self.cancel_requested.is_set():
                mark_cancelled(index)
                break
            replace_this_job = self.conflict_policy_var.get() == "Replace existing"
            skip_this_job = False
            if job_needs_conflict_decision(job):
                decision = self._resolve_conflict_decision(job.target)
                if decision == "stop":
                    self.cancel_requested.set()
                    mark_cancelled(index)
                    break
                replace_this_job = decision == "replace"
                skip_this_job = decision == "skip"
                if replace_this_job:
                    # finish_reembed_renames() runs after this loop and does
                    # its own final_target.exists() check for the rename
                    # step — this records that the conflict was already
                    # resolved to Replace here, so it overwrites instead of
                    # asking (or failing) about the same conflict again.
                    job.status = "READY REPLACE"
            status = "OK"
            if skip_this_job:
                status = "SKIPPED: conflict"
            else:
                try:
                    if job.mode == "split_copy":
                        # Genuinely mono source, "Copy non-Poly (mono)
                        # source files" enabled — a straight file copy
                        # instead of the sox+bwfmetaedit pipeline, so
                        # whatever metadata the source has (complete,
                        # partial, or none) comes along for free rather
                        # than needing its own embed step.
                        job.target.parent.mkdir(parents=True, exist_ok=True)
                        if job.target.exists() and not replace_this_job:
                            raise RuntimeError("target exists")
                        shutil.copy2(job.source, job.target)
                    elif job.mode == "split":
                        if job.source not in source_cache:
                            try:
                                source_cache[job.source] = export_ixml(self.bwf_var.get(), job.source)
                            except Exception as exc:
                                if str(exc) in _NO_IXML_MESSAGES:
                                    # No iXML at all doesn't block the split —
                                    # each channel just has no real Trk# to
                                    # carry over, matching build_split_jobs()'s
                                    # own fallback for the exact same case.
                                    source_cache[job.source] = "<BWFXML></BWFXML>"
                                else:
                                    raise
                        ixml = source_cache[job.source]
                        mono_ixml, _trk = extract_mono_ixml(ixml, job.channel)
                        split_with_sox(self.sox_var.get(), job.source, job.target, job.channel, replace_this_job)
                        write_split_metadata(self.bwf_var.get(), job.source, job.target, mono_ixml)
                    elif job.mode in ("combine", "merge", "combine_copy"):
                        execute_combine_job(job, self.sox_var.get(), self.bwf_var.get(), replace_this_job)
                    else:
                        ixml = export_ixml(self.bwf_var.get(), job.source)
                        # A Poly-source Re-embed job's `channel` is that
                        # file's own stream_index (see build_reembed_jobs) —
                        # track index 1 (what the mono-source branch below
                        # always reads) would be the wrong channel for
                        # anything past stream 1, so a genuinely multi-
                        # channel source reads its own real stream index
                        # instead, strictly: a missing Trk# there means we
                        # can't confirm this target really is the stream we
                        # matched it to by filename, unlike Split (which
                        # isn't confirming a match against anything, just
                        # numbering a channel by its own file-order slot).
                        if read_wav_pcm_layout(job.source)["channels"] > 1:
                            mono_ixml, _trk = extract_mono_ixml(ixml, job.channel, strict=True)
                        else:
                            mono_ixml, _trk = extract_mono_ixml(ixml, 1, fallback_channel=job.channel)
                        write_reembed_metadata(self.bwf_var.get(), job.source, job.target, mono_ixml)
                except MergeMetadataError as exc:
                    status = f"MERGE SUCCEEDED; METADATA FAILED: {exc}"
                except Exception as exc:
                    status = f"FAIL: {exc}"
            if ready_planning_rows:
                row = ready_planning_rows[index - 1]
                row["status"] = status
            else:
                row = processing_report_row(job, status)
                rows.append(row)
            if status == "OK" and job.mode == "reembed" and job.final_target is not None and job.final_target != job.target:
                pending_reembed_renames.append((job, row))
            elapsed = time.monotonic() - started
            eta = (elapsed / index) * (len(jobs) - index) if index else 0
            self.events.put(("progress", ProgressEvent("execute", index, len(jobs), status, job.target or job.source, elapsed=elapsed, eta=eta)))
        finish_reembed_renames(pending_reembed_renames)
        self.events.put(("done", (rows, source_root)))

    def _resolve_conflict_decision(self, target: Path) -> str:
        """Runs on the execute worker thread. Blocks until the main thread
        answers the conflict dialog it's about to be asked to show (or until
        an "apply to all" decision from an earlier conflict this run already
        covers it) — the same wait-on-an-Event pattern as pause/cancel,
        just with a three-way answer instead of a boolean."""
        if self._conflict_apply_all_decision is not None:
            return self._conflict_apply_all_decision
        self._conflict_response = None
        self._conflict_response_event.clear()
        self.events.put(("conflict_prompt", target))
        while not self._conflict_response_event.is_set():
            if self.cancel_requested.is_set():
                return "stop"
            time.sleep(0.05)
        decision, apply_to_all = self._conflict_response or ("stop", False)
        if apply_to_all and decision in ("skip", "replace"):
            self._conflict_apply_all_decision = decision
        return decision

    def _prompt_conflict_decision(self, target: Path) -> None:
        """Runs on the main thread (from _drain_events). Shows a modal
        Skip/Replace/Stop dialog for one conflicting target, with an
        "apply to all" checkbox, then hands the answer back to whichever
        execute-worker thread is blocked in _resolve_conflict_decision()."""
        dialog = tk.Toplevel(self.root)
        dialog.title(APP_NAME)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        content = ttk.Frame(dialog, padding=16)
        content.pack(fill=tk.BOTH, expand=True)
        ttk.Label(content, text="This file already exists in Target:", font=("SF Pro Display", 12, "bold")).pack(anchor="w")
        ttk.Label(content, text=str(target), wraplength=420, justify=tk.LEFT).pack(anchor="w", pady=(4, 12))
        apply_to_all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(content, text="Apply this choice to all remaining conflicts in this run", variable=apply_to_all_var).pack(anchor="w", pady=(0, 12))
        button_row = ttk.Frame(content)
        button_row.pack(fill=tk.X)

        def respond(decision: str) -> None:
            self._conflict_response = (decision, apply_to_all_var.get())
            self._conflict_response_event.set()
            dialog.destroy()

        ttk.Button(button_row, text="Stop", command=lambda: respond("stop")).pack(side=tk.LEFT)
        ttk.Button(button_row, text="Skip", command=lambda: respond("skip")).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(button_row, text="Replace", command=lambda: respond("replace")).pack(side=tk.RIGHT)
        dialog.protocol("WM_DELETE_WINDOW", lambda: respond("stop"))
        dialog.grab_set()
        dialog.wait_window()

    def _run_settings_lines(self) -> list[str]:
        """A snapshot of the settings that actually produced this run's
        results — without it, re-opening an old processing log gives no way
        to tell what formula/toggle combination was used to get there."""
        mode = self.mode_var.get()
        on_off = {True: "ON", False: "OFF"}
        lines = [f"Mode: {mode}", f"Source: {self.source_var.get()}", f"Target/Output: {self.output_var.get()}"]
        if mode.startswith("Split"):
            lines += [
                f"Output name formula: {self.output_formula_var.get()}",
                f"Filename numbering: {self.name_mode_var.get()}",
                f"If output exists: {self.conflict_policy_var.get()}",
                f"Copy non-Poly (mono) source files: {on_off[self.split_copy_mono_var.get()]}",
            ]
        elif mode.startswith("Re-embed"):
            lines += [
                "Pairing: Source name prefix + recorder channel (no formula)",
                f"If output exists: {self.conflict_policy_var.get()}",
                f"Rename matched targets after embed: {on_off[self.rename_after_embed_var.get()]}",
            ]
            if self.rename_after_embed_var.get():
                lines += [
                    f"  Output name formula: {self.reembed_rename_formula_var.get()}",
                    f"  Filename numbering: {self.rename_number_mode_var.get()}",
                ]
        elif mode.startswith("Rename"):
            lines.append(f"Metadata Template: {on_off[self.rename_metadata_template_enabled_var.get()]} | Template={self.rename_metadata_template_var.get()!r}")
            lines.append(f"Add Text: {on_off[self.rename_apply_formula_var.get()]} | Text={self.rename_formula_var.get()!r} | Position={self.rename_text_position_var.get()}")
            lines.append(f"Change $: {on_off[self.rename_change_number_var.get()]} | Numbering={self.rename_number_mode_var.get()}")
            rules = "; ".join(f"{find.get()}->{replace.get()}" for find, replace in zip(self.rename_find_vars, self.rename_replace_vars) if find.get())
            lines.append(f"Find & Replace: {on_off[self.rename_find_replace_enabled_var.get()]} | Regex={on_off[self.rename_regex_var.get()]} | Rules=[{rules}]")
            lines.append(
                f"Serial ID: {on_off[self.rename_serial_enabled_var.get()]} | Prefix={self.rename_serial_prefix_var.get()} | "
                f"Digits={self.rename_serial_digits_var.get()} | Start={self.rename_serial_start_var.get()} | "
                f"Position={self.rename_serial_position_var.get()} | Order={self.rename_serial_order_var.get()}"
            )
        elif mode.startswith("Combine"):
            lines += [
                f"Copy non-combined/non-merged files: {on_off[self.combine_copy_non_combined_var.get()]}",
                f"If output exists: {self.conflict_policy_var.get()}",
                f"Speaker pattern: {self.speaker_pattern_var.get()}",
                f"Merge order: {self.merge_order_var.get()}",
                f"Rename merged files: {on_off[self.merge_rename_var.get()]}",
            ]
            if self.merge_rename_var.get():
                lines += [
                    f"  Include Format: {on_off[self.merge_include_format_var.get()]}",
                    f"  Include Layout: {on_off[self.merge_include_layout_var.get()]}",
                    f"  Position: {self.merge_rename_position_var.get()}",
                    f"  Custom layout name: {self.merge_custom_layout_name_var.get()}",
                ]
        return lines

    def _save_processing_log(self, rows: list[dict[str, str]], source_root: Path) -> Optional[Path]:
        initial_dir = source_root.parent if source_root.exists() else Path.home()
        log_path = filedialog.asksaveasfilename(
            title="Save processing log",
            initialdir=str(initial_dir),
            defaultextension=".tsv",
            initialfile=f"sox_metadata_workflow_{dt.datetime.now():%Y%m%d_%H%M%S}.tsv",
            filetypes=(("TSV", "*.tsv"), ("All files", "*.*")),
        )
        if not log_path:
            return None
        path = Path(log_path)
        with path.open("w", newline="", encoding="utf-8") as handle:
            for line in self._run_settings_lines():
                handle.write(f"# {line}\n")
            writer = csv.DictWriter(handle, fieldnames=("mode", "source", "target", "final_target", "channel", "trk_number", "stream_number", "status"), delimiter="\t")
            writer.writeheader()
            writer.writerows(rows)
        return path

    def _save_qc_workbook(self, initial_dir: Path, initial_name: str, headers: list[str], sheet_rows: list[list[tuple[object, Optional[str]]]]) -> Optional[Path]:
        report_path = filedialog.asksaveasfilename(
            title="Save QC report",
            initialdir=str(initial_dir if initial_dir.exists() else Path.home()),
            defaultextension=".xlsx",
            initialfile=initial_name,
            filetypes=(("Excel Workbook", "*.xlsx"), ("All files", "*.*")),
        )
        if not report_path:
            return None
        path = Path(report_path)
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for row_cells in sheet_rows:
            sheet.append([qc_report_cell(value) for value, _fill_key in row_cells])
            row_index = sheet.max_row
            for column_index, (_value, fill_key) in enumerate(row_cells, start=1):
                fill = QC_REPORT_FILLS.get(fill_key) if fill_key else None
                if fill is not None:
                    sheet.cell(row=row_index, column=column_index).fill = fill
        workbook.save(path)
        return path

    def _save_qc_file_report(self, comparison: dict[str, object], source_path: Optional[Path], target_path: Optional[Path]) -> Optional[Path]:
        sheet_rows: list[list[tuple[object, Optional[str]]]] = []
        for section_name, section_rows in comparison["sections"]:
            for row in section_rows:
                fill_key = qc_result_fill_key(row["result"])
                sheet_rows.append([(section_name, None), (row["label"], None), (row["source"], fill_key), (row["target"], fill_key)])
        initial_dir = (target_path or source_path or Path.home()).parent
        return self._save_qc_workbook(
            initial_dir,
            f"qc_file_{dt.datetime.now():%Y%m%d_%H%M%S}.xlsx",
            ["SECTION", "LABEL", "SOURCE", "TARGET"],
            sheet_rows,
        )

    def _save_qc_folder_report(self, comparison: dict[str, object], source_folder: Path, target_folder: Path) -> Optional[Path]:
        headers = ["FILE NAME", "SOURCE PATH", "TARGET PATH"] + qc_report_detail_columns()
        sheet_rows = [
            [(row.get("filename"), None), (row.get("source_path"), None), (row.get("target_path"), None)] + qc_report_detail_cells(row)
            for row in comparison["rows"]
        ]
        return self._save_qc_workbook(
            target_folder.parent,
            f"qc_folder_{dt.datetime.now():%Y%m%d_%H%M%S}.xlsx",
            headers,
            sheet_rows,
        )

    def _save_qc_poly_report(self, comparison: dict[str, object], source_folder: Path, target_folder: Path) -> Optional[Path]:
        headers = ["POLY FILE", "STREAM", "RECORDER CH", "SPLIT FILE", "POLY PATH", "SPLIT PATH", "FRAMES", "AUDIO RESULT"] + qc_report_detail_columns()
        sheet_rows = [
            [
                (row.get("poly_file"), None), (row.get("stream"), None), (row.get("channel"), None), (row.get("split_file"), None),
                (row.get("poly_path"), None), (row.get("split_path"), None), (row.get("frames"), None),
                (row.get("result"), qc_result_fill_key(row.get("result"))),
            ] + qc_report_detail_cells(row)
            for row in comparison["rows"]
        ]
        return self._save_qc_workbook(
            target_folder.parent,
            f"qc_poly_split_{dt.datetime.now():%Y%m%d_%H%M%S}.xlsx",
            headers,
            sheet_rows,
        )

    def _drain_events(self) -> None:
        try:
            for _ in range(100):
                kind, payload = self.events.get_nowait()
                if kind == "preview":
                    self._show_preview(*payload)
                elif kind == "import":
                    self._apply_imported_jobs(*payload)
                elif kind == "progress":
                    progress = payload
                    elapsed, eta = progress.timing()
                    if progress.scope == "split_preview":
                        scope_label = progress.label if progress.label in ("Rescan", "Import") else "Preview"
                        self.status_var.set(f"{scope_label} {progress.index}/{progress.total} | elapsed {self._duration(elapsed)} | ETA {self._duration(eta)}")
                        self.append_log(f"[{scope_label.casefold()} {progress.index}/{progress.total}] {progress.item.name} | jobs={progress.detail_value} | {progress.status} | elapsed {self._duration(elapsed)} | ETA {self._duration(eta)}")
                    elif progress.scope == "workflow_preview":
                        progress_text = f"{progress.overall_index}/{progress.overall_total} ({progress.status} {progress.index}/{progress.total})" if progress.label == "Re-embed preview" else f"{progress.index}/{progress.total} | {progress.status}"
                        self.status_var.set(f"{progress.label} {progress_text} | elapsed {self._duration(elapsed)} | ETA {self._duration(eta)}")
                        detail_text = f"{progress.detail_label}={progress.detail_value} | " if progress.detail_value else ""
                        self.append_log(f"[{progress.label.casefold()} {progress.index}/{progress.total}] {progress.item.name} | {detail_text}{progress.status} | overall {progress.overall_index}/{progress.overall_total} | elapsed {self._duration(elapsed)} | ETA {self._duration(eta)}")
                    elif progress.scope in ("qc_poly", "qc_poly_prepare"):
                        if progress.request_id != self._qc_poly_request_id:
                            continue
                        progress_text = f"{progress.label} {progress.index}/{progress.total} | elapsed {self._duration(elapsed)} | ETA {self._duration(eta)}"
                        self.qc_poly_message_var.set(progress_text)
                        self.status_var.set(progress_text)
                        if qc_progress_log_due(self._qc_progress_log_state, progress.scope.removesuffix("_prepare"), progress.label, progress.index, progress.total, time.monotonic()):
                            self.append_log(progress_text)
                    elif progress.scope == "qc_folder":
                        if progress.request_id != self._qc_folder_request_id:
                            continue
                        if progress.total:
                            count_text = f"{progress.index}/{progress.total}"
                        else:
                            count_text = f"{progress.index} files found"
                        progress_text = f"{progress.label} {count_text} | elapsed {self._duration(elapsed)} | ETA {self._duration(eta)}"
                        self.qc_folder_message_var.set(progress_text)
                        self.status_var.set(progress_text)
                        if qc_progress_log_due(self._qc_progress_log_state, progress.scope, progress.label, progress.index, progress.total, time.monotonic()):
                            self.append_log(progress_text)
                    elif progress.scope == "qc_auto":
                        if progress.request_id != self._qc_auto_request_id:
                            continue
                        count_text = f"{progress.index}/{progress.total}" if progress.total else f"{progress.index} files found"
                        progress_text = f"{progress.label} {count_text} | elapsed {self._duration(elapsed)} | ETA {self._duration(eta)}"
                        self.status_var.set(progress_text)
                        if qc_progress_log_due(self._qc_progress_log_state, progress.scope, progress.label, progress.index, progress.total, time.monotonic()):
                            self.append_log(progress_text)
                    else:
                        self.status_var.set(f"Processing {progress.index}/{progress.total} | elapsed {self._duration(elapsed)} | ETA {self._duration(eta)}")
                        self.append_log(f"[{progress.index}/{progress.total}] {progress.item.name} | {progress.status} | elapsed {self._duration(elapsed)} | ETA {self._duration(eta)}")
                elif kind == "qc_poly_done":
                    request_id, comparison = payload
                    if request_id == self._qc_poly_request_id:
                        self._show_qc_poly_results(comparison)
                        self.status_var.set(self.qc_poly_message_var.get())
                        self.qc_poly_compare_button.configure(state=tk.NORMAL)
                elif kind == "qc_folder_done":
                    request_id, comparison = payload
                    if request_id == self._qc_folder_request_id:
                        self._show_qc_folder_results(comparison)
                        self.status_var.set(self.qc_folder_message_var.get())
                        self.append_log(self.status_var.get())
                        self.qc_folder_compare_button.configure(state=tk.NORMAL)
                elif kind == "qc_folder_error":
                    request_id, error_text = payload
                    if request_id == self._qc_folder_request_id:
                        message = f"Folder QC blocked: {error_text}"
                        self.qc_folder_message_var.set(message)
                        self.status_var.set(message)
                        self.append_log(message)
                        self.qc_folder_compare_button.configure(state=tk.NORMAL)
                elif kind == "qc_auto_done":
                    request_id, detection, comparison = payload
                    if request_id == self._qc_auto_request_id:
                        self._show_detected_qc_type(detection.kind)
                        if detection.kind == "file":
                            self.qc_source_path = detection.source
                            self.qc_target_path = detection.target
                            self._show_qc_file_results(comparison, detection.source, detection.target)
                            report_path = self._save_qc_file_report(comparison, detection.source, detection.target)
                        elif detection.kind == "folder":
                            self.qc_source_folder = detection.source
                            self.qc_target_folder = detection.target
                            self._show_qc_folder_results(comparison)
                            report_path = self._save_qc_folder_report(comparison, detection.source, detection.target)
                        else:
                            self.qc_poly_source_folder = detection.source
                            self.qc_poly_target_folder = detection.target
                            self.qc_poly_source_folder_var.set(str(detection.source))
                            self.qc_poly_target_folder_var.set(str(detection.target))
                            self._show_qc_poly_results(comparison)
                            report_path = self._save_qc_poly_report(comparison, detection.source, detection.target)
                        type_label = {"file": "File ↔ File", "folder": "Folder ↔ Folder", "poly_split": "Poly ↔ Split"}[detection.kind]
                        result_text = str(comparison["result"])
                        direction_text = f" | {detection.message}" if detection.message else ""
                        report_text = f" | Report: {report_path}" if report_path else " | Report: not saved"
                        self.status_var.set(f"Detected {type_label} | RESULT: {result_text}{direction_text}{report_text}")
                        self.append_log(self.status_var.get())
                        self.qc_auto_compare_button.configure(state=tk.NORMAL)
                        self.active_operation_phase = None
                        self._set_operation_controls(OperationControlCapabilities())
                elif kind == "qc_auto_error":
                    request_id, error_text = payload
                    if request_id == self._qc_auto_request_id:
                        message = f"QC blocked: {error_text}"
                        self.status_var.set(message)
                        self.append_log(message)
                        self.qc_auto_compare_button.configure(state=tk.NORMAL)
                        self.active_operation_phase = None
                        self._set_operation_controls(OperationControlCapabilities())
                elif kind == "qc_auto_cancelled":
                    request_id = payload
                    if request_id == self._qc_auto_request_id:
                        self.status_var.set("QC cancelled.")
                        self.append_log("QC cancelled.")
                        self.qc_auto_compare_button.configure(state=tk.NORMAL)
                        self.active_operation_phase = None
                        self._set_operation_controls(OperationControlCapabilities())
                elif kind == "qc_poly_error":
                    request_id, error_text = payload
                    if request_id == self._qc_poly_request_id:
                        message = f"AUDIO RESULT: BLOCKED — {error_text}"
                        self.qc_poly_message_var.set(message)
                        self.status_var.set(message)
                        self.qc_poly_compare_button.configure(state=tk.NORMAL)
                elif kind == "qc_file_update_done":
                    comparison, source_path, target_path = payload
                    self._show_qc_file_results(comparison, source_path, target_path)
                    self.status_var.set(f"Metadata updated on {target_path.name}. RESULT: {comparison['result']}")
                    self.append_log(self.status_var.get())
                elif kind == "qc_folder_update_done":
                    count, failures, refreshed_rows = payload
                    if failures:
                        self.status_var.set(f"Updated {count - len(failures)}/{count} file(s); {len(failures)} failed.")
                        self.append_log(self.status_var.get())
                        for failure in failures:
                            self.append_log(f"  FAILED: {failure}")
                        messagebox.showerror(APP_NAME, "Some files failed to update:\n\n" + "\n".join(failures))
                    else:
                        self.status_var.set(f"Updated metadata on {count} file(s).")
                        self.append_log(self.status_var.get())
                    self._apply_qc_folder_row_refresh(refreshed_rows)
                elif kind == "qc_poly_update_done":
                    count, failures, refreshed_rows = payload
                    if failures:
                        self.status_var.set(f"Updated {count - len(failures)}/{count} file(s); {len(failures)} failed.")
                        self.append_log(self.status_var.get())
                        for failure in failures:
                            self.append_log(f"  FAILED: {failure}")
                        messagebox.showerror(APP_NAME, "Some files failed to update:\n\n" + "\n".join(failures))
                    else:
                        self.status_var.set(f"Updated metadata on {count} file(s).")
                        self.append_log(self.status_var.get())
                    self._apply_qc_poly_row_refresh(refreshed_rows)
                elif kind == "qc_update_error":
                    message = f"Metadata update failed: {payload}"
                    self.status_var.set(message)
                    self.append_log(message)
                    self.qc_file_update_button.configure(state=tk.NORMAL)
                    messagebox.showerror(APP_NAME, str(payload))
                elif kind == "conflict_prompt":
                    self._prompt_conflict_decision(payload)
                elif kind == "done":
                    rows, source_root = payload
                    successes, failures, other = summarize_processing_rows(rows)
                    log_path = self._save_processing_log(rows, source_root)
                    log_text = str(log_path) if log_path else "not saved"
                    summary = f"Done: {successes} OK; {failures} failed/cancelled"
                    if other:
                        summary += f"; {other} not processed (see Status column)"
                    self.status_var.set(f"{summary}. Log: {log_text}")
                    self.append_log(self.status_var.get())
                    self.preview_button.configure(state=tk.NORMAL)
                    self.execute_button.configure(state=tk.NORMAL if any(job_is_actionable(job) for job in self.jobs) else tk.DISABLED)
                    self._set_operation_controls(OperationControlCapabilities())
                    self.active_operation_phase = None
                elif kind == "preview_cancelled":
                    self.active_operation_phase = None
                    self.status_var.set("Preview cancelled.")
                    self.append_log("Preview cancelled.")
                    self.preview_button.configure(state=tk.NORMAL)
                    self.execute_button.configure(state=tk.DISABLED)
                    self._set_operation_controls(OperationControlCapabilities())
                elif kind == "error":
                    self.active_operation_phase = None
                    self.status_var.set("Failed.")
                    self.preview_button.configure(state=tk.NORMAL)
                    self._set_operation_controls(OperationControlCapabilities())
                    messagebox.showerror(APP_NAME, str(payload))
                elif kind == "browse_poly_details":
                    self._apply_browse_poly_details(*payload)
                elif kind == "browse_mono_peaks":
                    self._apply_browse_mono_peaks(*payload)
        except Empty:
            pass
        self.root.after(100, self._drain_events)


def main() -> None:
    root = TkinterDnD.Tk()
    WorkflowApp(root)
    root.update_checker = UpdateChecker(root, APP_NAME, VERSION)  # keep a reference
    root.mainloop()


if __name__ == "__main__":
    main()
