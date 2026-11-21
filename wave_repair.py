#!/usr/bin/env python3
"""Wave Repair for Post — diagnose and repair damaged RIFF/WAVE files.

A separate app from Wave Manager for Post that shares its `operations/`
engine. Phase 1 diagnoses container/chunk/padding/data problems; Phase 2
(`operations/riff_repair.py`) repairs only what is 100% certain, after a
verified backup, and never touches a file with a problem it can't safely fix.

Two ways in:
  - Browse: a folder (or a batch of dropped/received files) as a table, one
    row per file, with the selected file's full report underneath.
  - Diagnose File: one file's full report — what a single dropped WAV, or a
    file handed over from Wave Manager, opens straight into.
"""
from __future__ import annotations

import datetime as dt
import os
import time
import json
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from queue import Empty, Queue
from tkinter import filedialog, messagebox, ttk
from typing import Optional

MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # drag-and-drop is a convenience; everything else still works
    DND_FILES = None
    TkinterDnD = None

from operations.path_core import natural_path_key
from operations.riff_inspect import CATEGORIES, SEVERITY_SYMBOLS, InspectionReport, format_report_text, inspect_wav, summary_line
from operations.riff_repair import PLAN_DEFERRED, PLAN_REPAIR, REBUILD_CATEGORY, RepairPlan, RepairResult, default_backup_root, is_inside, plan_repair, project_root_for, rebuild_target_problem, run_rebuilds, run_repairs
from operations.repair_handoff import handoff_scan_root
from operations.post_tools_inbox import WAVE_REPAIR, Batch, InboxPoller, bring_to_front
from operations.rebuild_drafts import ConfirmStore, DraftStore, RebuildDraft
from operations.audio_recover import TRUNCATED, RecoverPlan, plan_recover, recover_kind, run_recovers
from operations.ixml_rebuild import CONFIDENCE_LABELS, CONFLICT, INFERRED, NEEDS_REVIEW, OMITTED, ORIGINAL, RECOVERED, RebuildPlan, history_note, plan_rebuild, render_ixml
from ui_components import ERROR_COLOR, HINT_TEXT_COLOR, INFO_COLOR, PANEL_BACKGROUND, TABLE_BACKGROUND, TEXT_COLOR, WARNING_COLOR, ExecutionLogWindow, TreeviewSorter, apply_app_theme, configure_treeview_column_widths

APP_NAME = "Wave Repair for Post"
VERSION = "261003.1312"
SETTINGS_PATH = Path.home() / "Library" / "Application Support" / APP_NAME / "settings.json"

MODE_BROWSE = "Browse"
MODE_INSPECT = "Diagnose File"

# Same palette as Wave Manager (see ui_components) — light, whatever the macOS appearance.
SEVERITY_COLORS = {"error": ERROR_COLOR, "warning": WARNING_COLOR, "info": INFO_COLOR}


def is_inspectable_wav(path: Path) -> bool:
    # "._*" are macOS AppleDouble sidecars (common on exFAT recorder cards):
    # not audio at all, and would otherwise all show up as "not a RIFF file".
    return path.is_file() and path.suffix.lower() == ".wav" and not path.name.startswith("._")


def list_wavs(folder: Path, recursive: bool) -> list[Path]:
    candidates = folder.rglob("*") if recursive else folder.glob("*")
    return sorted((path for path in candidates if is_inspectable_wav(path)), key=natural_path_key)


def route_incoming_paths(values: list[str], recursive: bool = True) -> tuple[str, list[Path], Optional[Path]]:
    """Where dropped/received paths should go: (mode, files, folder).

    One WAV file → Diagnose File. A folder → Browse that folder. Several
    files (or a mix) → Browse them as a batch. macOS launch noise like
    "-psn_0_12345" and anything that isn't a WAV is ignored."""
    paths = [Path(value).expanduser() for value in values if value and not value.startswith("-psn")]
    folders = [path for path in paths if path.is_dir()]
    files = [path for path in paths if is_inspectable_wav(path)]
    if len(folders) == 1 and not files:
        return MODE_BROWSE, list_wavs(folders[0], recursive), folders[0]
    for folder in folders:
        files.extend(list_wavs(folder, recursive))
    if len(files) == 1 and not folders:
        return MODE_INSPECT, files, None
    return MODE_BROWSE, files, None


def load_settings(path: Optional[Path] = None) -> dict:
    path = path or SETTINGS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict, path: Optional[Path] = None) -> None:
    path = path or SETTINGS_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def report_bundle(reports: list[InspectionReport], root: Optional[Path] = None) -> dict:
    return {
        "app": APP_NAME,
        "version": VERSION,
        "generated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "root": str(root) if root else None,
        "files": [report.to_dict() for report in reports],
    }


# User-facing wording names what happens, never the internal phase numbers.
REPAIR_COLUMN_TEXT = {PLAN_REPAIR: "Will repair", PLAN_DEFERRED: "Not repairable yet"}


def repair_column_text(report: InspectionReport) -> str:
    plan = plan_repair(report)
    if plan.status == PLAN_DEFERRED and rebuild_target_problem(report) == "":
        return "Rebuild iXML"
    return REPAIR_COLUMN_TEXT.get(plan.status, "")


def repair_line(plan: RepairPlan, report: Optional[InspectionReport] = None) -> str:
    """The report's one-line answer to "what would Repair do to this file?"."""
    if plan.status == PLAN_REPAIR:
        return f"Repair: will fix — {plan.category}"
    if report is not None and recover_kind(report)[0] and not recover_kind(report)[1]:
        return "Repair: the audio can be recovered (its data size is wrong) — read and confirm it under Recover audio, then Recover All / Recover Selected"
    if plan.status == PLAN_DEFERRED and report is not None and rebuild_target_problem(report) == "":
        return "Repair: the iXML can be rebuilt from this file and its related takes — review it under Rebuild review, then Rebuild All / Rebuild Selected"
    if plan.status == PLAN_DEFERRED:
        return f"Repair: not repairable yet — Safe repairs won't touch this file — {plan.deferred_reason}"
    return "Repair: nothing to repair"


def display_value(value: Optional[str]) -> str:
    if value is None:
        return "(left empty)"
    if value.strip() == "":
        return "(empty)" if value == "" else f"(blank ×{len(value)})"
    return value.replace("\r\n", " ⏎ ").replace("\n", " ⏎ ")


def rebuild_summary_lines(plan: RebuildPlan, choices: dict[str, Optional[str]]) -> list[str]:
    """For the Repair Log: counts, then every field that wasn't simply read
    from the file or agreed on by the related takes, with its source."""
    counts = {key: sum(item.confidence == key for item in plan.fields) for key in CONFIDENCE_LABELS}
    takes = ", ".join(f"{label} {take.path.name}" for label, take in (("previous", plan.previous), ("next", plan.following)) if take is not None)
    lines = [f"related takes: {plan.related_count} ({takes})", "fields: " + " · ".join(f"{CONFIDENCE_LABELS[key]} {count}" for key, count in counts.items())]
    for item in plan.fields:
        changed = item.confidence not in NEEDS_REVIEW and item.path in choices
        if item.confidence in (INFERRED, CONFLICT, OMITTED) or changed:
            value = choices[item.path] if item.path in choices else item.value
            prefix = "✎ changed by the user:" if changed else ""
            lines.append(f"{prefix} {CONFIDENCE_LABELS[item.confidence]} {item.path} = {display_value(value)} — {item.source}".strip())
    return lines


class RebuildReviewPanel(ttk.Frame):
    """Review of one file's rebuilt iXML, embedded under the file list: every
    field with the previous take's value, the value to write and the next
    take's value side by side, its audio evidence, confidence and source.
    Every edit is saved straight into that file's draft — nothing is written
    to the file here; Rebuild All / Rebuild Selected do that later, for all
    reviewed files at once."""

    # Previous | value to write | Next, side by side, so the value reads
    # against the takes either side of it.
    COLUMNS = ("field", "previous", "value", "next", "audio", "confidence", "source", "state")

    def __init__(self, parent, app: "WaveRepairApp") -> None:
        super().__init__(parent)
        self.app = app
        self.plan: Optional[RebuildPlan] = None
        self.draft: Optional[RebuildDraft] = None
        self.show_all = tk.BooleanVar(value=False)
        self.title_var = tk.StringVar(value="")
        self.sub_var = tk.StringVar(value="")
        self.counts_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.title_var, font=("SF Pro Display", 13, "bold"), foreground=TEXT_COLOR).pack(anchor=tk.W)
        ttk.Label(self, textvariable=self.sub_var, foreground=HINT_TEXT_COLOR).pack(anchor=tk.W, pady=(2, 0))
        ttk.Label(self, textvariable=self.counts_var, foreground=TEXT_COLOR).pack(anchor=tk.W, pady=(4, 2))
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X)
        ttk.Checkbutton(bar, text="Show all fields", variable=self.show_all, command=self._fill).pack(side=tk.LEFT)
        ttk.Label(
            bar,
            text="Double-click 🟡 to confirm, 🔴 to choose · right-click any row to change it · "
                 "🟢 this file  🔵 all takes / both neighbours agree  🟡 one neighbour or estimate  🔴 neighbours differ  ⚪ left empty (per-file ID)",
            foreground=HINT_TEXT_COLOR,
        ).pack(side=tk.LEFT, padx=(12, 0))
        buttons = ttk.Frame(self)
        buttons.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        ttk.Button(buttons, text="Confirm Selected", command=self._confirm_selected).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Confirm All Inferred", command=self._confirm_all_inferred).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(buttons, text="Reset This File's Review", command=self._reset).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(buttons, text="Reviewed files are written with Rebuild All… / Rebuild Selected…", foreground=HINT_TEXT_COLOR).pack(side=tk.RIGHT)
        table_frame = ttk.Frame(self)
        table_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.table = ttk.Treeview(table_frame, columns=self.COLUMNS, show="headings", selectmode="extended")
        configure_treeview_column_widths(self.table, (
            ("field", 290, True), ("previous", 190, True), ("value", 190, True), ("next", 190, True),
            ("audio", 170, True), ("confidence", 110, True), ("source", 360, True), ("state", 100, True),
        ), stretch_column="")
        self.table.tag_configure("default", background=TABLE_BACKGROUND, foreground=TEXT_COLOR)
        self.table.tag_configure("todo", background=TABLE_BACKGROUND, foreground=WARNING_COLOR)
        self.table.tag_configure("conflict", background=TABLE_BACKGROUND, foreground=ERROR_COLOR)
        scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.table.yview)
        scroll_x = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.table.xview)
        self.table.configure(yscrollcommand=scroll.set, xscrollcommand=scroll_x.set)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.table.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        self.table.bind("<Double-1>", self._on_double_click)
        for sequence in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            self.table.bind(sequence, self._on_right_click)
        self.by_iid: dict[str, object] = {}

    def show_message(self, title: str, message: str) -> None:
        self.plan, self.draft = None, None
        self.title_var.set(title)
        self.sub_var.set(message)
        self.counts_var.set("")
        self.table.delete(*self.table.get_children())

    def show(self, plan: RebuildPlan, draft: RebuildDraft) -> None:
        self.plan, self.draft = plan, draft
        self.title_var.set(f"Rebuild iXML — {plan.target.name}")
        neighbours = " · ".join(f"{label}: {take.path.name}" for label, take in (("previous take", plan.previous), ("next take", plan.following)) if take is not None)
        self.sub_var.set(f"From this file's own bext/fmt/name and {plan.related_count} related takes in the same folder — {neighbours}")
        previous_label = f"Previous take — {plan.previous.path.name}" if plan.previous else "Previous take — (none)"
        next_label = f"Next take — {plan.following.path.name}" if plan.following else "Next take — (none that day)"
        for column, label in zip(self.COLUMNS, ("Field", previous_label, "Value (will be written)", next_label, "Audio: prev / this / next", "Confidence", "Source", "Review")):
            self.table.heading(column, text=label)
        self._fill()

    def _state(self, item) -> tuple[str, str]:
        if item.path in self.draft.overridden:
            return ("✎ changed", "default")
        if item.confidence == INFERRED:
            return ("✓ confirmed", "default") if item.path in self.draft.confirmed else ("confirm", "todo")
        if item.confidence == CONFLICT:
            return ("✓ chosen", "default") if item.path in self.draft.confirmed else ("choose…", "conflict")
        return ("", "default")

    def _value(self, item) -> Optional[str]:
        return self.draft.choices[item.path] if item.path in self.draft.choices else item.value

    def _fill(self) -> None:
        if self.plan is None or self.draft is None:
            return
        self.table.delete(*self.table.get_children())
        self.by_iid = {}
        order = {CONFLICT: 0, INFERRED: 1, OMITTED: 2, ORIGINAL: 3, RECOVERED: 4}
        for item in sorted(self.plan.fields, key=lambda field: order[field.confidence]):
            # Track names are always shown: they are what an editor relies on most.
            is_track_name = item.path.startswith("TRACK_LIST/TRACK[") and item.path.endswith("/NAME")
            if not self.show_all.get() and item.confidence not in (CONFLICT, INFERRED, OMITTED) and not is_track_name and item.path not in self.draft.overridden:
                continue
            state, tag = self._state(item)
            previous = self._neighbour_value(self.plan.previous, item.path)
            following = self._neighbour_value(self.plan.following, item.path)
            value_text = "(choose…)" if item.confidence == CONFLICT and item.path not in self.draft.confirmed else display_value(self._value(item))
            channel = self.plan.channel_for(item.path)
            audio = f"ch {channel}: {self.plan.level('previous', channel)} / {self.plan.level('target', channel)} / {self.plan.level('next', channel)}" if channel else ""
            iid = self.table.insert("", tk.END, values=(item.path, previous, value_text, following, audio, CONFIDENCE_LABELS[item.confidence], item.source, state), tags=(tag,))
            self.by_iid[iid] = item
        left = self.draft.remaining(self.plan)
        counts = {key: sum(field.confidence == key for field in self.plan.fields) for key in CONFIDENCE_LABELS}
        status = "✓ reviewed — ready to write" if left == 0 else f"{left} still to review"
        self.counts_var.set("   ".join(f"{CONFIDENCE_LABELS[key]} {count}" for key, count in counts.items()) + f"      —  {status}")

    def _changed(self) -> None:
        self.draft.left_to_review = self.draft.remaining(self.plan)
        self._fill()
        self.app._on_draft_changed(self.plan.target)

    @staticmethod
    def _neighbour_value(take, path: str) -> str:
        if take is None:
            return "—"
        return display_value(take.leaves[path]) if path in take.leaves else "(not in this take)"

    def _confirm_selected(self) -> None:
        if self.plan is None:
            return
        for iid in self.table.selection():
            item = self.by_iid.get(iid)
            if item is not None and item.confidence == INFERRED:
                self.draft.confirmed.add(item.path)
        self._changed()

    def _confirm_all_inferred(self) -> None:
        if self.plan is None:
            return
        self.draft.confirmed.update(item.path for item in self.plan.fields if item.confidence == INFERRED)
        self._changed()

    def _reset(self) -> None:
        if self.plan is None:
            return
        self.draft.choices.clear()
        self.draft.confirmed.clear()
        self.draft.overridden.clear()
        self._changed()

    def _on_double_click(self, event) -> str:
        iid = self.table.identify_row(event.y)
        item = self.by_iid.get(iid)
        if item is None or item.confidence not in NEEDS_REVIEW:
            return "break"
        if item.confidence == INFERRED and item.path not in self.draft.confirmed:
            self.draft.confirmed.add(item.path)
            self._changed()
            return "break"
        return self._on_right_click(event)

    def _on_right_click(self, event) -> str:
        """Any row, any time — including one already chosen or confirmed, and
        one left empty by default: previous take's value, next take's value,
        the proposed value, leave empty, or type one."""
        iid = self.table.identify_row(event.y)
        item = self.by_iid.get(iid)
        if item is None:
            return "break"
        self.table.selection_set(iid)
        menu = tk.Menu(self.winfo_toplevel(), tearoff=False)
        seen = set()
        candidates = list(item.options) or [(item.value, "proposed value")]
        if item.confidence not in NEEDS_REVIEW and item.value is not None:
            candidates.insert(0, (item.value, f"proposed ({CONFIDENCE_LABELS[item.confidence]})"))
        for label, take in (("previous take", self.plan.previous), ("next take", self.plan.following)):
            if take is not None and item.path in take.leaves:
                candidates.append((take.leaves[item.path], f"{label} {take.path.name}"))
        candidates.append((None, "leave empty"))
        for value, source in candidates:
            key = (value or "").strip() if value is not None else None
            if key in seen:
                continue
            seen.add(key)
            menu.add_command(label=f"{display_value(value)}  —  {source}", command=lambda chosen=value, path=item.path: self._choose(path, chosen))
        menu.add_separator()
        menu.add_command(label="Type a value…", command=lambda path=item.path: self._type_value(path))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _type_value(self, path: str) -> None:
        from tkinter import simpledialog
        current = next((self._value(item) for item in self.plan.fields if item.path == path), "")
        typed = simpledialog.askstring("Type a value", f"{path}:", initialvalue=current or "", parent=self.winfo_toplevel())
        if typed is not None:
            self._choose(path, typed)

    def _choose(self, path: str, value: Optional[str]) -> None:
        self.draft.choices[path] = value
        item = next((field for field in self.plan.fields if field.path == path), None)
        if item is not None and item.confidence in NEEDS_REVIEW:
            self.draft.confirmed.add(path)
        elif item is not None:
            self.draft.overridden.add(path)
        self._changed()


class RecoverPanel(ttk.Frame):
    """Recover audio for one file: what was measured, the evidence, and the
    user's confirmation (saved immediately; nothing is written here)."""

    def __init__(self, parent, app: "WaveRepairApp") -> None:
        super().__init__(parent)
        self.app = app
        self.plan: Optional[RecoverPlan] = None
        self.title_var = tk.StringVar(value="")
        self.confirm_var = tk.BooleanVar(value=False)
        ttk.Label(self, textvariable=self.title_var, font=("SF Pro Display", 13, "bold"), foreground=TEXT_COLOR).pack(anchor=tk.W)
        self.text = tk.Text(self, wrap=tk.WORD, height=10, relief=tk.FLAT, borderwidth=0, highlightthickness=0,
                            background=PANEL_BACKGROUND, foreground=TEXT_COLOR, font=("SF Pro Display", 12), padx=8, pady=6)
        self.text.tag_configure("warning", foreground=WARNING_COLOR)
        self.text.tag_configure("error", foreground=ERROR_COLOR)
        self.text.pack(fill=tk.BOTH, expand=True, pady=(6, 6))
        row = ttk.Frame(self)
        row.pack(fill=tk.X)
        self.confirm_check = ttk.Checkbutton(row, text="Confirm this recovery", variable=self.confirm_var, command=self._toggled)
        self.confirm_check.pack(side=tk.LEFT)
        ttk.Label(row, text="Confirmed files are written with Recover All… / Recover Selected…", foreground=HINT_TEXT_COLOR).pack(side=tk.RIGHT)

    def show(self, plan: RecoverPlan) -> None:
        self.plan = plan
        self.title_var.set(f"Recover audio — {plan.path.name}")
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        if not plan.ok:
            self.text.insert(tk.END, f"Can't recover this file: {plan.problem}\n", ("warning",))
            self.confirm_var.set(False)
            self.confirm_check.configure(state=tk.DISABLED)
        else:
            for line in plan.lines:
                tag = ("error",) if "CANNOT" in line else (("warning",) if line.startswith(("Evidence", "Similar levels", "If the original", "Only ")) else ())
                self.text.insert(tk.END, line + "\n\n", tag)
            self.confirm_var.set(self.app.recover_confirmed.is_confirmed(plan.path))
            self.confirm_check.configure(state=tk.NORMAL)
        self.text.configure(state=tk.DISABLED)

    def _toggled(self) -> None:
        if self.plan is not None:
            self.app.recover_confirmed.set_confirmed(self.plan.path, self.confirm_var.get())
            self.app._refresh_rows_for(self.plan.path)


class DetailArea(ttk.Frame):
    """The area under the file list (and in Diagnose File): the inspection
    report, or — for a file whose iXML can be rebuilt — its rebuild review."""

    REPORT, REVIEW, RECOVER = "Report", "Rebuild review", "Recover audio"

    def __init__(self, parent, app: "WaveRepairApp") -> None:
        super().__init__(parent)
        self.mode = tk.StringVar(value=self.REPORT)
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, pady=(0, 4))
        ttk.Radiobutton(bar, text=self.REPORT, value=self.REPORT, variable=self.mode, style="Toolbutton", command=self._show).pack(side=tk.LEFT)
        self.review_toggle = ttk.Radiobutton(bar, text=self.REVIEW, value=self.REVIEW, variable=self.mode, style="Toolbutton", command=self._show, state=tk.DISABLED)
        self.review_toggle.pack(side=tk.LEFT, padx=(4, 0))
        self.recover_toggle = ttk.Radiobutton(bar, text=self.RECOVER, value=self.RECOVER, variable=self.mode, style="Toolbutton", command=self._show, state=tk.DISABLED)
        self.recover_toggle.pack(side=tk.LEFT, padx=(4, 0))
        self.report_frame = ttk.Frame(self)
        self.text = app._report_text(self.report_frame)
        self.review = RebuildReviewPanel(self, app)
        self.recover = RecoverPanel(self, app)
        self._show()

    def _show(self) -> None:
        for widget in (self.report_frame, self.review, self.recover):
            widget.pack_forget()
        {self.REVIEW: self.review, self.RECOVER: self.recover}.get(self.mode.get(), self.report_frame).pack(fill=tk.BOTH, expand=True)

    def set_available(self, rebuildable: bool, recoverable: bool) -> None:
        self.review_toggle.configure(state=tk.NORMAL if rebuildable else tk.DISABLED)
        self.recover_toggle.configure(state=tk.NORMAL if recoverable else tk.DISABLED)
        self.mode.set(self.REVIEW if rebuildable else (self.RECOVER if recoverable else self.REPORT))
        self._show()


def summarize_plans(plans: list[RepairPlan]) -> tuple[dict[str, int], list[RepairPlan]]:
    """(repairable file count per backup category, deferred plans)."""
    by_category: dict[str, int] = {}
    for plan in plans:
        if plan.status == PLAN_REPAIR:
            by_category[plan.category] = by_category.get(plan.category, 0) + 1
    return by_category, [plan for plan in plans if plan.status == PLAN_DEFERRED]


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{size} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def format_duration_short(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def repair_eta(elapsed: float, done_bytes: int, total_bytes: int) -> Optional[float]:
    """Remaining time from bytes processed so far — files vary from tens of
    MB to over half a GB, so a per-file average would swing wildly."""
    if done_bytes <= 0 or elapsed <= 0:
        return None
    return elapsed / done_bytes * max(total_bytes - done_bytes, 0)


class RepairProgress:
    """Drives a running repair's feedback the same way Wave Manager does:
    the live status line (shown in the shared Execution Log window's header
    and at the bottom of the main window), one log line per file, and a
    byte-based progress bar under the file table."""

    def __init__(self, app: "WaveRepairApp") -> None:
        self.app = app
        self.run_folder: Optional[Path] = None

    def show(self) -> None:
        self.app.log_window.show()

    def append(self, line: str) -> None:
        self.app.log_window.append(line)

    def start(self, total_files: int, total_bytes: int, backup_root: Path) -> None:
        self.total_files, self.total_bytes, self.done_bytes, self.done_files = total_files, total_bytes, 0, 0
        self.started = time.monotonic()
        self.run_folder = None
        self.app.open_backup_button.configure(state=tk.DISABLED)
        self.app.progress_bar.configure(maximum=max(total_bytes, 1), value=0)
        self.app.status_var.set(f"Repairing {total_files} file(s)…")
        self.append(f"=== Repair {total_files} file(s) ({format_size(total_bytes)}) — backups under {backup_root}")
        self.show()

    def _timing(self) -> str:
        elapsed = time.monotonic() - self.started
        eta = repair_eta(elapsed, self.done_bytes, self.total_bytes)
        return f"elapsed {format_duration_short(elapsed)}" + (f" · about {format_duration_short(eta)} left" if eta is not None else "")

    def step(self, index: int, total: int, path: Path, message: str) -> None:
        self.app.status_var.set(f"Repairing {index}/{total} — {path.name} — {message}… · {self.done_files} done · {self._timing()}")

    def finished(self, index: int, total: int, result: RepairResult) -> None:
        self.done_files = index
        self.done_bytes += result.size_before
        self.app.progress_bar.configure(value=self.done_bytes)
        name = Path(result.path).name
        if result.status == "repaired":
            self.append(f"[repair {index}/{total}] ✓ {name} | {'; '.join(result.repairs)}")
        elif result.status == "skipped":
            self.append(f"[repair {index}/{total}] — {name} | not touched: {result.message}")
        else:
            self.append(f"[repair {index}/{total}] ✗ {name} | FAILED: {result.message}")
        self.app.status_var.set(f"Repairing — {self.done_files}/{self.total_files} done · {self._timing()}")

    def done(self, run_folder: Path, results: list[RepairResult]) -> None:
        self.run_folder = run_folder
        self.app.open_backup_button.configure(state=tk.NORMAL)
        self.app.progress_bar.configure(value=self.app.progress_bar.cget("maximum"))
        counts = {status: sum(result.status == status for result in results) for status in ("repaired", "skipped", "failed")}
        elapsed = format_duration_short(time.monotonic() - self.started)
        summary = f"Repair done in {elapsed}: {counts['repaired']} repaired · {counts['skipped']} not touched (Rebuild iXML or not repairable yet) · {counts['failed']} failed (original kept / restored)"
        self.app.status_var.set(summary)
        self.append(summary)
        self.append(f"Backups and Repair Log: {run_folder}")
        self.show()

    def open_backup(self) -> None:
        if self.run_folder is not None:
            subprocess.Popen(["open", str(self.run_folder)])


class WaveRepairApp:
    TABLE_COLUMNS = ("name", "folder", "size", "container", "fmt", "bext", "ixml", "data", "status", "repair")
    TABLE_LABELS = {"name": "File", "folder": "Folder", "size": "Size", "container": "Container", "fmt": "fmt", "bext": "bext", "ixml": "iXML", "data": "data", "status": "Status", "repair": "Repair"}

    def __init__(self, root: tk.Tk, initial_paths: Optional[list[str]] = None) -> None:
        self.root = root
        self.settings = load_settings()
        self.root.title(f"{APP_NAME} {VERSION}")
        self.root.geometry(self.settings.get("geometry", "1180x760"))
        self.root.minsize(820, 520)

        self.mode_var = tk.StringVar(value=MODE_BROWSE)
        self.recursive_var = tk.BooleanVar(value=bool(self.settings.get("recursive", True)))
        self.only_issues_var = tk.BooleanVar(value=bool(self.settings.get("only_issues", False)))
        self.browse_source_var = tk.StringVar(value="Choose a folder, or drop a folder / WAV files here")
        self.browse_summary_var = tk.StringVar(value="")
        self.inspect_title_var = tk.StringVar(value="Drop a WAV file here, or Open File…")
        self.status_var = tk.StringVar(value="Ready.")

        self.events: Queue[tuple] = Queue()
        self.scan_generation = 0
        self.inspect_generation = 0
        self.browse_folder: Optional[Path] = None
        self.browse_files: list[Path] = []
        self.results: list[tuple[Path, InspectionReport]] = []
        self.row_reports: dict[str, InspectionReport] = {}
        self.inspect_report: Optional[InspectionReport] = None
        self.repair_running = False
        self.scanning = False
        self.display_root: Optional[Path] = None
        self.handoff_root: Optional[Path] = None
        # Phase 3: per-file review drafts (saved to disk) and the plans they
        # were made against (memory only; re-read when needed).
        self.drafts = DraftStore(SETTINGS_PATH.parent / "rebuild_drafts.json")
        self.rebuild_plans: dict[str, RebuildPlan] = {}
        self.planning: set[str] = set()
        # Recover audio: per-file confirmations (saved) and measured plans.
        self.recover_confirmed = ConfirmStore(SETTINGS_PATH.parent / "recover_confirmations.json")
        self.recover_plans: dict[str, RecoverPlan] = {}

        self._build_ui()
        self._register_drop_targets()
        # Files opened from Finder ("Open With", drop on the Dock icon) or
        # handed over by Wave Manager via `open -a` arrive as Apple Events.
        self.root.createcommand("::tk::mac::OpenDocument", lambda *paths: self.receive_paths(list(paths)))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._drain_after = self.root.after(50, self._drain_events)
        # Files sent from Soundminer ("Wave Repair — Diagnose") arrive here.
        self.inbox = InboxPoller(self.root, WAVE_REPAIR, self.receive_batch)

        if initial_paths:
            self.receive_paths(initial_paths)
        elif self.settings.get("last_folder") and Path(self.settings["last_folder"]).is_dir():
            self.browse_source_var.set(f"Last folder: {self.settings['last_folder']}  (Rescan to diagnose)")
            self.browse_folder = Path(self.settings["last_folder"])
        self._show_mode()

    # --- UI ---------------------------------------------------------------

    def _build_ui(self) -> None:
        style = apply_app_theme(self.root)
        self.root.configure(background=style.lookup("TFrame", "background"))
        outer = ttk.Frame(self.root, padding=(10, 8, 10, 8))
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=tk.X)
        for mode in (MODE_BROWSE, MODE_INSPECT):
            ttk.Radiobutton(header, text=mode, value=mode, variable=self.mode_var, style="Toolbutton", command=self._show_mode).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(header, text="Safe repairs, Recover audio and Rebuild iXML — every original is backed up and verified first", foreground=HINT_TEXT_COLOR).pack(side=tk.RIGHT)

        self.body = ttk.Frame(outer)
        self.body.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.browse_frame = self._build_browse(self.body)
        self.inspect_frame = self._build_inspect(self.body)
        # Same Execution Log window as Wave Manager (title, layout, status
        # header), with the repair progress bar and backup button in the
        # main window like Wave Manager keeps its status line there.
        status_row = ttk.Frame(outer)
        # Packed at the bottom *before* the body so it always keeps its
        # height — packed after, the expanding body squeezed it out of
        # view whenever the window wasn't tall enough.
        status_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0), before=self.body)
        self.progress_bar = ttk.Progressbar(status_row, mode="determinate", maximum=1, length=220)
        self.progress_bar.pack(side=tk.LEFT)
        ttk.Label(status_row, textvariable=self.status_var, foreground=TEXT_COLOR).pack(side=tk.LEFT, padx=(10, 0), fill=tk.X, expand=True)
        self.open_backup_button = ttk.Button(status_row, text="Open Backup Folder", state=tk.DISABLED, command=lambda: self.progress_window.open_backup())
        self.open_backup_button.pack(side=tk.RIGHT)
        self.log_window = ExecutionLogWindow(self.root, self.status_var)
        self.progress_window = RepairProgress(self)

    def _build_browse(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent)
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="Choose Folder…", command=self.choose_folder).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Add Files…", command=self.choose_files).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(toolbar, text="Rescan", command=self.rescan).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Checkbutton(toolbar, text="Include subfolders", variable=self.recursive_var, command=self.rescan).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Checkbutton(toolbar, text="Only files with issues", variable=self.only_issues_var, command=self._refill_table).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="Export Report…", command=self.export_browse_report).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text="Log", command=lambda: self.progress_window.show()).pack(side=tk.RIGHT, padx=(0, 4))
        actions = ttk.Frame(frame)
        actions.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(actions, text="Safe repairs:", foreground=HINT_TEXT_COLOR).pack(side=tk.LEFT)
        self.repair_all_button = ttk.Button(actions, text="Repair All…", command=self.repair_browse)
        self.repair_all_button.pack(side=tk.LEFT, padx=(6, 0))
        self.repair_selected_button = ttk.Button(actions, text="Repair Selected…", command=self.repair_selected)
        self.repair_selected_button.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(actions, text="Recover audio:", foreground=HINT_TEXT_COLOR).pack(side=tk.LEFT, padx=(20, 0))
        self.recover_all_button = ttk.Button(actions, text="Recover All…", command=self.recover_all)
        self.recover_all_button.pack(side=tk.LEFT, padx=(6, 0))
        self.recover_selected_button = ttk.Button(actions, text="Recover Selected…", command=self.recover_selected)
        self.recover_selected_button.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(actions, text="Rebuild iXML:", foreground=HINT_TEXT_COLOR).pack(side=tk.LEFT, padx=(20, 0))
        self.rebuild_all_button = ttk.Button(actions, text="Rebuild All…", command=self.rebuild_all)
        self.rebuild_all_button.pack(side=tk.LEFT, padx=(6, 0))
        self.rebuild_selected_button = ttk.Button(actions, text="Rebuild Selected…", command=self.rebuild_selected)
        self.rebuild_selected_button.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(frame, textvariable=self.browse_source_var, foreground=HINT_TEXT_COLOR).pack(fill=tk.X, pady=(6, 4))

        # Summary line reserved at the bottom first, for the same reason as
        # the main window's status row.
        ttk.Label(frame, textvariable=self.browse_summary_var).pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        panes = ttk.PanedWindow(frame, orient=tk.VERTICAL)
        panes.pack(fill=tk.BOTH, expand=True)
        table_frame = ttk.Frame(panes)
        # extended: click, Shift-click and Command-click selection.
        self.table = ttk.Treeview(table_frame, columns=self.TABLE_COLUMNS, show="headings", selectmode="extended")
        # No column stretches at all: ttk re-divides leftover width among
        # stretchable columns on every layout pass, so even the one "stretch"
        # column (and the column after it, whose left edge is its right edge)
        # snapped back when dragged. Every width now stays exactly where it's
        # dragged; anything wider than the window scrolls horizontally.
        configure_treeview_column_widths(self.table, (
            ("name", 260, True), ("folder", 150, True), ("size", 80, False), ("container", 80, False),
            ("fmt", 46, False), ("bext", 46, False), ("ixml", 46, False), ("data", 46, False),
            ("status", 170, True), ("repair", 170, True),
        ), stretch_column="")
        self.sorter = TreeviewSorter(self.table, self.TABLE_LABELS)
        self.table.tag_configure("default", background=TABLE_BACKGROUND, foreground=TEXT_COLOR)
        for severity, color in SEVERITY_COLORS.items():
            self.table.tag_configure(severity, background=TABLE_BACKGROUND, foreground=color)
        scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.table.yview)
        scroll_x = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.table.xview)
        self.table.configure(yscrollcommand=scroll.set, xscrollcommand=scroll_x.set)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.table.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        self.table.bind("<<TreeviewSelect>>", self._on_table_select)
        self.table.bind("<Double-1>", self._on_table_double_click)
        self.table.bind("<Return>", self._on_table_double_click)
        for sequence in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            self.table.bind(sequence, self._on_table_context_menu)
        panes.add(table_frame, weight=3)

        self.browse_detail_area = DetailArea(panes, self)
        self.browse_detail = self.browse_detail_area.text
        panes.add(self.browse_detail_area, weight=2)

        return frame

    def _build_inspect(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent)
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="Open File…", command=self.choose_single_file).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Reveal in Finder", command=lambda: self._reveal(self.inspect_report)).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(toolbar, text="Copy Report", command=self.copy_inspect_report).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(toolbar, text="Export Report…", command=self.export_inspect_report).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text="Log", command=lambda: self.progress_window.show()).pack(side=tk.RIGHT, padx=(0, 4))
        self.inspect_rebuild_button = ttk.Button(toolbar, text="Write Rebuilt iXML…", command=lambda: self.rebuild_write([self.inspect_report] if self.inspect_report else []))
        self.inspect_rebuild_button.pack(side=tk.RIGHT, padx=(0, 4))
        self.inspect_recover_button = ttk.Button(toolbar, text="Recover Audio…", command=lambda: self.recover_write([self.inspect_report] if self.inspect_report else []))
        self.inspect_recover_button.pack(side=tk.RIGHT, padx=(0, 4))
        ttk.Button(toolbar, text="Repair…", command=lambda: self.repair_reports([self.inspect_report] if self.inspect_report else [])).pack(side=tk.RIGHT, padx=(0, 4))
        ttk.Label(frame, textvariable=self.inspect_title_var, font=("SF Pro Display", 13, "bold"), foreground=TEXT_COLOR).pack(fill=tk.X, pady=(8, 6))
        self.inspect_detail_area = DetailArea(frame, self)
        self.inspect_detail_area.pack(fill=tk.BOTH, expand=True)
        self.inspect_text = self.inspect_detail_area.text
        return frame

    def _monospace_family(self) -> str:
        # The report's chunk table is column-aligned, so it needs a font that
        # really is installed — Tk silently swaps a missing one ("SF Mono"
        # isn't on every Mac) for a proportional font.
        available = set(tkfont.families(self.root))
        return next((family for family in ("SF Mono", "Menlo", "Monaco") if family in available), "Courier")

    def _report_text(self, parent: ttk.Frame) -> tk.Text:
        mono = self._monospace_family()
        text = tk.Text(
            parent, wrap=tk.WORD, font=(mono, 11), padx=8, pady=6, relief=tk.FLAT, borderwidth=0, highlightthickness=0,
            background=PANEL_BACKGROUND, foreground=TEXT_COLOR, insertbackground=TEXT_COLOR,
        )
        scroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scroll.set, state=tk.DISABLED)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        for severity, color in SEVERITY_COLORS.items():
            text.tag_configure(severity, foreground=color)
        text.tag_configure("heading", font=(mono, 11, "bold"))
        return text

    def _set_report_text(self, widget: tk.Text, content: str) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        symbol_tags = {SEVERITY_SYMBOLS["error"]: "error", SEVERITY_SYMBOLS["warning"]: "warning", SEVERITY_SYMBOLS["info"]: "info"}
        for line in content.split("\n"):
            stripped = line.lstrip()
            tag = symbol_tags.get(stripped[:1]) if stripped else None
            if tag is None and line and not line.startswith(" ") and not line.startswith(("Status", "Repair:")):
                tag = "heading"
            widget.insert(tk.END, line + "\n", (tag,) if tag else ())
        widget.configure(state=tk.DISABLED)

    def _register_drop_targets(self) -> None:
        if DND_FILES is None or not hasattr(self.root, "drop_target_register"):
            return
        self.root.drop_target_register(DND_FILES)
        self.root.dnd_bind("<<Drop>>", self._on_drop)

    def _on_drop(self, event) -> str:
        self.receive_paths(list(self.root.tk.splitlist(event.data)))
        return "copy"

    def _set_window_subject(self, subject: str) -> None:
        self.root.title(f"{APP_NAME} {VERSION}" + (f" — {subject}" if subject else ""))

    def _show_mode(self) -> None:
        self.browse_frame.pack_forget()
        self.inspect_frame.pack_forget()
        (self.browse_frame if self.mode_var.get() == MODE_BROWSE else self.inspect_frame).pack(fill=tk.BOTH, expand=True)

    # --- input --------------------------------------------------------------

    def receive_paths(self, values: list[str]) -> None:
        mode, files, folder = route_incoming_paths(values, self.recursive_var.get())
        if not files and folder is None:
            messagebox.showinfo(APP_NAME, "No WAV files found in what was dropped.")
            return
        # Handed over by Wave Manager? Then its Source folder is the scan root
        # (backups go beside it, never inside it).
        self.handoff_root = handoff_scan_root(files) if folder is None else None
        if mode == MODE_INSPECT:
            self.inspect_file(files[0])
        elif folder is not None:
            self.browse(folder=folder)
        else:
            self.browse(files=files)
        self.root.deiconify()
        self.root.lift()

    def receive_batch(self, batch: Batch) -> bool:
        """A selection sent from Soundminer: one file opens in Diagnose File;
        several are added to the Browse list (what's already there stays).
        Returns False — try again shortly — while a repair is writing."""
        if self.repair_running:
            return False
        files = [path for path in batch.files if is_inspectable_wav(path)]
        if not files:
            self.status_var.set(f"Received {len(batch.files)} file(s) from Soundminer — none of them is a WAV file that exists.")
            return True
        self.handoff_root = None
        skipped = len(batch.files) - len(files)
        note = f" ({skipped} skipped: not a WAV file or not found)" if skipped else ""
        if len(files) == 1:
            self.inspect_file(files[0])
        else:
            existing = list(self.browse_files)
            self.browse(files=existing + [path for path in files if path not in existing])
        self.log_window.append(f"=== Received {len(files)} file(s) from Soundminer{note}")
        self.status_var.set(f"Received {len(files)} file(s) from Soundminer{note}.")
        bring_to_front(self.root)
        return True

    def choose_folder(self) -> None:
        initial = self.settings.get("last_folder") or str(Path.home())
        folder = filedialog.askdirectory(title="Choose a folder of WAV files", initialdir=initial)
        if folder:
            self.browse(folder=Path(folder))

    def choose_files(self) -> None:
        files = filedialog.askopenfilenames(title="Add WAV files", filetypes=[("WAV files", "*.wav *.WAV"), ("All files", "*")])
        if files:
            existing = list(self.browse_files) if self.browse_folder is None else []
            self.browse(files=existing + [Path(path) for path in files if Path(path) not in existing])

    def choose_single_file(self) -> None:
        path = filedialog.askopenfilename(title="Open WAV file", filetypes=[("WAV files", "*.wav *.WAV"), ("All files", "*")])
        if path:
            self.inspect_file(Path(path))

    # --- Browse -------------------------------------------------------------

    def browse(self, folder: Optional[Path] = None, files: Optional[list[Path]] = None) -> None:
        self.mode_var.set(MODE_BROWSE)
        self._show_mode()
        self.browse_folder = folder
        if folder is not None:
            self.settings["last_folder"] = str(folder)
            files = list_wavs(folder, self.recursive_var.get())
            self.browse_source_var.set(f"Folder: {folder}")
            self._set_window_subject(folder.name)
        else:
            parents = [str(path.parent) for path in files or []]
            self.display_root = Path(os.path.commonpath(parents)) if parents else None
            location = f" from {self.display_root}" if self.display_root else ""
            self.browse_source_var.set(f"{len(files or [])} file(s) added{location}")
            self._set_window_subject(f"{len(files or [])} file(s)")
        self.browse_files = list(files or [])
        self._start_scan()

    def rescan(self) -> None:
        if self.browse_folder is not None:
            self.browse(folder=self.browse_folder)
        elif self.browse_files:
            self._start_scan()

    def _start_scan(self) -> None:
        self.scan_generation += 1
        self.results = []
        self.row_reports = {}
        self.table.delete(*self.table.get_children())
        self._set_report_text(self.browse_detail, "")
        total = len(self.browse_files)
        self.browse_summary_var.set(f"Diagnosing {total} file(s)…" if total else "No WAV files.")
        if total:
            self.log_window.append(f"=== Diagnose {total} file(s)" + (f" in {self.browse_folder}" if self.browse_folder else ""))
        self.scanning = bool(total)
        if total:
            threading.Thread(target=self._scan_worker, args=(self.scan_generation, list(self.browse_files)), daemon=True).start()

    def _scan_worker(self, generation: int, files: list[Path]) -> None:
        for path in files:
            if generation != self.scan_generation:
                return
            self.events.put(("scan_result", generation, path, inspect_wav(path)))
        self.events.put(("scan_done", generation))

    def _relative_folder(self, path: Path) -> str:
        root = self.browse_folder or self.display_root
        if root is not None:
            try:
                parent = path.parent.relative_to(root)
                return "" if str(parent) == "." else str(parent)
            except ValueError:
                pass
        return str(path.parent)

    def _row_values(self, path: Path, report: InspectionReport) -> tuple[str, ...]:
        statuses = [SEVERITY_SYMBOLS[report.category_status(category)] for category in CATEGORIES]
        container = report.container or "?"
        if report.category_status("container") != "ok":
            container = f"{container} {statuses[0]}"
        repair = self._repair_column(report)
        return (path.name, self._relative_folder(path), format_size(report.file_size), container, *statuses[1:], summary_line(report), repair)

    def _insert_row(self, path: Path, report: InspectionReport) -> None:
        if self.only_issues_var.get() and report.worst_severity in ("ok", "info"):
            return
        severity = report.worst_severity
        iid = self.table.insert("", tk.END, values=self._row_values(path, report), tags=(severity,) if severity in SEVERITY_COLORS else ("default",))
        self.row_reports[iid] = report

    def _repair_column(self, report: InspectionReport) -> str:
        kind, problem = recover_kind(report)
        if kind and not problem:
            return "Recover — ✓ confirmed" if self.recover_confirmed.is_confirmed(Path(report.path)) else "Recover audio"
        if plan_repair(report).status == PLAN_DEFERRED and rebuild_target_problem(report) == "":
            draft = self.drafts.get(Path(report.path))
            if draft is not None and draft.left_to_review is not None:
                return "Rebuild — ✓ reviewed" if draft.left_to_review == 0 else f"Rebuild — {draft.left_to_review} to review"
            return "Rebuild iXML"
        return repair_column_text(report)

    def _refresh_rows_for(self, path: Path) -> None:
        for iid, report in self.row_reports.items():
            if Path(report.path) == path:
                self.table.item(iid, values=self._row_values(path, report))

    def _refill_table(self) -> None:
        self.table.delete(*self.table.get_children())
        self.row_reports = {}
        for path, report in self.results:
            self._insert_row(path, report)
        self.sorter.reapply()
        self._update_browse_summary(done=True)

    def _update_browse_summary(self, done: bool) -> None:
        totals = {"error": 0, "warning": 0, "ok": 0}
        for _path, report in self.results:
            worst = report.worst_severity
            totals["ok" if worst in ("ok", "info") else worst] += 1
        prefix = "" if done else f"Diagnosing {len(self.results)}/{len(self.browse_files)} — "
        self.browse_summary_var.set(f"{prefix}{len(self.results)} file(s): {totals['error']} with errors · {totals['warning']} with warnings · {totals['ok']} OK")

    def _selected_report(self) -> Optional[InspectionReport]:
        """The report for the row last clicked (the focus) — with several
        rows selected, the detail pane follows the one you just clicked."""
        selected = self.table.selection()
        if not selected:
            return None
        focus = self.table.focus()
        return self.row_reports.get(focus if focus in selected else selected[-1])

    def _selected_reports(self) -> list[InspectionReport]:
        return [self.row_reports[iid] for iid in self.table.selection() if iid in self.row_reports]

    def _on_table_select(self, _event=None) -> None:
        report = self._selected_report()
        if report is not None:
            self._set_report_text(self.browse_detail, self._report_with_repair_line(report))
            self._show_review(self.browse_detail_area, report)

    def _on_table_double_click(self, _event=None) -> str:
        report = self._selected_report()
        if report is not None:
            self._show_inspect_report(report)
        return "break"

    def _on_table_context_menu(self, event) -> None:
        row = self.table.identify_row(event.y)
        if not row:
            return
        if row not in self.table.selection():
            self.table.selection_set(row)
        self.table.focus(row)
        report = self.row_reports.get(row)
        selected = self._selected_reports()
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="Open in Diagnose File", command=lambda: self._show_inspect_report(report))
        menu.add_command(label="Reveal in Finder", command=lambda: self._reveal(report))
        menu.add_separator()
        repairable = sum(plan_repair(item).status == PLAN_REPAIR for item in selected)
        menu.add_command(label=f"Repair Selected ({repairable} repairable of {len(selected)})…", command=self.repair_selected, state=tk.NORMAL if repairable else tk.DISABLED)
        rebuildable = [item for item in selected if rebuild_target_problem(item) == ""]
        menu.add_command(label=f"Rebuild Selected ({len(rebuildable)} file(s))…", command=lambda: self.rebuild_write(rebuildable), state=tk.NORMAL if rebuildable else tk.DISABLED)
        recoverable = [item for item in selected if recover_kind(item)[0] and not recover_kind(item)[1]]
        menu.add_command(label=f"Recover Selected ({len(recoverable)} file(s))…", command=lambda: self.recover_write(recoverable), state=tk.NORMAL if recoverable else tk.DISABLED)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def export_browse_report(self) -> None:
        if not self.results:
            messagebox.showinfo(APP_NAME, "Nothing diagnosed yet.")
            return
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
        target = filedialog.asksaveasfilename(title="Export diagnosis report", defaultextension=".json", initialfile=f"wave_repair_diagnosis_{stamp}.json", filetypes=[("JSON", "*.json")])
        if target:
            Path(target).write_text(json.dumps(report_bundle([report for _path, report in self.results], self.browse_folder), indent=2, ensure_ascii=False), encoding="utf-8")

    # --- Repair ----------------------------------------------------------------

    def _report_with_repair_line(self, report: InspectionReport) -> str:
        text = format_report_text(report)
        first, _newline, rest = text.partition("\n")
        return f"{report.path}\n\n{first}\n{repair_line(plan_repair(report), report)}\n{rest}"

    def repair_selected(self) -> None:
        if self.scanning:
            messagebox.showinfo(APP_NAME, "Still diagnosing — wait for the scan to finish before repairing.")
            return
        selected = self._selected_reports()
        if not selected:
            messagebox.showinfo(APP_NAME, "Select one or more files first (Shift-click / Command-click to select several).")
            return
        self.repair_reports(selected)

    def repair_browse(self) -> None:
        if self.scanning:
            messagebox.showinfo(APP_NAME, "Still diagnosing — wait for the scan to finish before repairing.")
            return
        self.repair_reports([report for _path, report in self.results])

    def repair_reports(self, reports: list[InspectionReport]) -> None:
        if self.repair_running:
            messagebox.showinfo(APP_NAME, "A repair is already running.")
            return
        plans = [(report, plan_repair(report)) for report in reports if report is not None]
        by_category, deferred = summarize_plans([plan for _report, plan in plans])
        if not by_category:
            detail = f"\n\n{len(deferred)} file(s) are not touched by Safe repairs (Rebuild iXML, or not repairable yet)." if deferred else ""
            messagebox.showinfo(APP_NAME, "Nothing here can be repaired safely." + detail)
            return
        files = [plan.path for _report, plan in plans if plan.status == PLAN_REPAIR]
        scan_root = self._scan_root(files)
        backup_root = self._confirm_repair(by_category, deferred, files, scan_root)
        if backup_root is None:
            return
        items = [(report, plan) for report, plan in plans if plan.status in (PLAN_REPAIR, PLAN_DEFERRED)]
        self.repair_running = True
        self._set_repair_buttons(tk.DISABLED)
        self.browse_summary_var.set(f"Repairing 0/{len(items)}…")
        self.progress_window.start(len(items), sum(report.file_size for report, _plan in items), backup_root)
        threading.Thread(target=self._repair_worker, args=(items, backup_root, scan_root), daemon=True).start()

    def _set_repair_buttons(self, state: str) -> None:
        for button in (self.repair_all_button, self.repair_selected_button, self.rebuild_all_button, self.rebuild_selected_button, self.inspect_rebuild_button,
                       self.recover_all_button, self.recover_selected_button, self.inspect_recover_button):
            button.configure(state=state)

    # Phase 3: review any file, in any order, any number of times (drafts);
    # write all reviewed files at the end with Rebuild All / Rebuild Selected.

    def _recover_plan(self, report: InspectionReport) -> RecoverPlan:
        plan = self.recover_plans.get(report.path)
        if plan is None:
            plan = self.recover_plans[report.path] = plan_recover(report)
        return plan

    def _show_review(self, area: DetailArea, report: InspectionReport) -> None:
        rebuildable = rebuild_target_problem(report) == ""
        kind, problem = recover_kind(report)
        recoverable = bool(kind) and not problem
        area.set_available(rebuildable, recoverable)
        if recoverable:
            area.recover.show(self._recover_plan(report))
        if not rebuildable:
            return
        path = Path(report.path)
        plan = self.rebuild_plans.get(str(path))
        if plan is not None:
            if plan.ok:
                area.review.show(plan, self.drafts.get(path, create=True))
            else:
                area.review.show_message(f"Rebuild iXML — {path.name}", f"Can't rebuild: {plan.problem}")
            return
        area.review.show_message(f"Rebuild iXML — {path.name}", "Reading related takes…")
        if str(path) not in self.planning:
            self.planning.add(str(path))
            threading.Thread(target=lambda: self.events.put(("rebuild_plan_ready", report, plan_rebuild(path, report))), daemon=True).start()

    def _on_plan_ready(self, report: InspectionReport, plan: RebuildPlan) -> None:
        path = Path(report.path)
        self.planning.discard(str(path))
        self.rebuild_plans[str(path)] = plan
        if plan.ok:
            draft = self.drafts.get(path, create=True)
            draft.left_to_review = draft.remaining(plan)
            self.drafts.save()
            self._refresh_rows_for(path)
        for area, current in ((self.browse_detail_area, self._selected_report()), (self.inspect_detail_area, self.inspect_report)):
            if current is not None and Path(current.path) == path:
                self._show_review(area, current)

    def _on_draft_changed(self, path: Path) -> None:
        self.drafts.save()
        self._refresh_rows_for(path)

    def rebuild_all(self) -> None:
        self.rebuild_write([report for _path, report in self.results])

    def rebuild_selected(self) -> None:
        selected = self._selected_reports()
        if not selected:
            messagebox.showinfo(APP_NAME, "Select one or more files first (Shift-click / Command-click to select several).")
            return
        self.rebuild_write(selected)

    def rebuild_write(self, reports: list[InspectionReport]) -> None:
        """Write the rebuilt iXML of every *reviewed* file among `reports`:
        one backup location, one run. Files not reviewed yet are listed and
        skipped — never written with unreviewed values."""
        if self.repair_running:
            messagebox.showinfo(APP_NAME, "A repair is already running.")
            return
        if self.scanning:
            messagebox.showinfo(APP_NAME, "Still diagnosing — wait for the scan to finish first.")
            return
        targets = [report for report in reports if report is not None and rebuild_target_problem(report) == ""]
        if not targets:
            messagebox.showinfo(APP_NAME, "None of these files needs its iXML rebuilt (the Repair column shows \"Rebuild …\" for those that do).")
            return
        reviewed, unreviewed = [], []
        for report in targets:
            path = Path(report.path)
            draft = self.drafts.get(path)
            plan = self.rebuild_plans.get(str(path))
            done = draft is not None and (draft.is_reviewed(plan) if plan is not None and plan.ok else draft.left_to_review == 0)
            (reviewed if done else unreviewed).append(report)
        if not reviewed:
            names = ", ".join(Path(report.path).name for report in unreviewed[:5])
            messagebox.showinfo(APP_NAME, f"Nothing reviewed yet. Click each file, review it under the list (confirm 🟡, choose 🔴), then write.\n\nNot reviewed: {names}")
            return
        paths = [Path(report.path) for report in reviewed]
        scan_root = self._scan_root(paths)
        note = ""
        if unreviewed:
            note = f"{len(unreviewed)} file(s) not reviewed yet will be skipped: " + ", ".join(Path(report.path).name for report in unreviewed[:5]) + (" …" if len(unreviewed) > 5 else "")
        backup_root = self._confirm_repair({REBUILD_CATEGORY: len(reviewed)}, [], paths, scan_root, note)
        if backup_root is None:
            return
        # Everything the worker needs, captured here on the main thread.
        jobs = [(report, self.rebuild_plans.get(report.path), self.drafts.get(Path(report.path))) for report in reviewed]
        self.repair_running = True
        self._set_repair_buttons(tk.DISABLED)
        self.log_window.append(f"=== Rebuild iXML: {len(reviewed)} reviewed file(s)" + (f"; {note}" if note else ""))
        self.progress_window.start(len(reviewed), sum(report.file_size for report in reviewed), backup_root)

        def work() -> None:
            try:
                items, problems = [], []
                for report, plan, draft in jobs:
                    path = Path(report.path)
                    plan = plan if plan is not None and plan.ok else plan_rebuild(path, report)
                    if not plan.ok or draft is None or not draft.is_reviewed(plan):
                        problems.append(f"{path.name}: its review no longer matches the related takes — review it again")
                        continue
                    choices = draft.final_choices(plan)
                    payload = render_ixml(plan, choices, history_note(plan, f"{APP_NAME} {VERSION}"))
                    items.append((report, payload, rebuild_summary_lines(plan, choices)))
                for problem in problems:
                    self.events.put(("log", f"Skipped — {problem}"))
                if not items:
                    self.events.put(("repair_failed", "nothing left to write"))
                    return
                run_folder, results = run_rebuilds(
                    items, backup_root, scan_root, f"{APP_NAME} {VERSION}",
                    step=lambda index, total, file_path, message: self.events.put(("repair_step", index, total, file_path, message)),
                    finished=lambda index, total, result: self.events.put(("repair_file_done", index, total, result)),
                )
                self.events.put(("repair_done", run_folder, results))
            except Exception as exc:
                self.events.put(("repair_failed", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    # Recover audio: confirm each file under the list, then write them together.

    def recover_all(self) -> None:
        self.recover_write([report for _path, report in self.results])

    def recover_selected(self) -> None:
        selected = self._selected_reports()
        if not selected:
            messagebox.showinfo(APP_NAME, "Select one or more files first (Shift-click / Command-click to select several).")
            return
        self.recover_write(selected)

    def recover_write(self, reports: list[InspectionReport]) -> None:
        if self.repair_running:
            messagebox.showinfo(APP_NAME, "A repair is already running.")
            return
        if self.scanning:
            messagebox.showinfo(APP_NAME, "Still diagnosing — wait for the scan to finish first.")
            return
        targets = [report for report in reports if report is not None and recover_kind(report)[0] and not recover_kind(report)[1]]
        if not targets:
            messagebox.showinfo(APP_NAME, "None of these files has audio to recover (the Repair column shows \"Recover …\" for those that do).")
            return
        confirmed = [report for report in targets if self.recover_confirmed.is_confirmed(Path(report.path))]
        unconfirmed = [report for report in targets if report not in confirmed]
        if not confirmed:
            names = ", ".join(Path(report.path).name for report in unconfirmed[:5])
            messagebox.showinfo(APP_NAME, f"Nothing confirmed yet. Click each file, read Recover audio under the list, tick \"Confirm this recovery\", then write.\n\nNot confirmed: {names}")
            return
        paths = [Path(report.path) for report in confirmed]
        scan_root = self._scan_root(paths)
        by_category: dict[str, int] = {}
        for report in confirmed:
            category = self._recover_plan(report).category
            by_category[category] = by_category.get(category, 0) + 1
        note = ""
        if unconfirmed:
            note = f"{len(unconfirmed)} file(s) not confirmed yet will be skipped: " + ", ".join(Path(report.path).name for report in unconfirmed[:5]) + (" …" if len(unconfirmed) > 5 else "")
        truncated = [report for report in confirmed if self._recover_plan(report).kind == TRUNCATED]
        if truncated:
            note = (note + "\n" if note else "") + f"{len(truncated)} copy-cut-short file(s): their missing tail stays lost — re-copy from the source instead if it still exists."
        backup_root = self._confirm_repair(by_category, [], paths, scan_root, note)
        if backup_root is None:
            return
        self.repair_running = True
        self._set_repair_buttons(tk.DISABLED)
        self.log_window.append(f"=== Recover audio: {len(confirmed)} confirmed file(s)" + (f"; {note}" if note else ""))
        self.progress_window.start(len(confirmed), sum(report.file_size for report in confirmed), backup_root)

        def work() -> None:
            try:
                items = [(report, plan_recover(report)) for report in confirmed]  # re-measured right before writing
                run_folder, results = run_recovers(
                    items, backup_root, scan_root, f"{APP_NAME} {VERSION}",
                    step=lambda index, total, file_path, message: self.events.put(("repair_step", index, total, file_path, message)),
                    finished=lambda index, total, result: self.events.put(("repair_file_done", index, total, result)),
                )
                self.events.put(("repair_done", run_folder, results))
            except Exception as exc:
                self.events.put(("repair_failed", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _scan_root(self, files: list[Path]) -> Optional[Path]:
        """The folder these files were scanned from — the folder opened in
        Browse, or Wave Manager's Source folder for a hand-off — if known."""
        for root in (self.browse_folder, self.handoff_root):
            if root is not None and all(is_inside(path, root) for path in files):
                return root
        return None

    def _confirm_repair(self, by_category: dict[str, int], deferred: list[RepairPlan], files: list[Path], scan_root: Optional[Path], note: str = "") -> Optional[Path]:
        """Modal summary + backup-location picker. Returns the chosen backup
        root, or None if cancelled."""
        default_root = default_backup_root(files, scan_root)
        dialog = tk.Toplevel(self.root)
        dialog.title("Repair Files")
        dialog.transient(self.root)
        dialog.configure(background=ttk.Style().lookup("TFrame", "background"))
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        total = sum(by_category.values())
        ttk.Label(body, text=f"Repair {total} file(s)", font=("SF Pro Display", 14, "bold"), foreground=TEXT_COLOR).pack(anchor=tk.W)
        for category, count in by_category.items():
            ttk.Label(body, text=f"  •  {category} — {count} file(s)", foreground=TEXT_COLOR).pack(anchor=tk.W, pady=(4, 0))
        if deferred:
            names = ", ".join(plan.path.name for plan in deferred[:5]) + (" …" if len(deferred) > 5 else "")
            ttk.Label(body, text=f"{len(deferred)} file(s) will NOT be touched by Safe repairs (they need Rebuild iXML, or aren't repairable yet): {names}", foreground=WARNING_COLOR, wraplength=620, justify=tk.LEFT).pack(anchor=tk.W, pady=(10, 0))
        if note:
            ttk.Label(body, text=note, foreground=WARNING_COLOR, wraplength=620, justify=tk.LEFT).pack(anchor=tk.W, pady=(10, 0))
        ttk.Label(body, text="Backup location", font=("SF Pro Display", 12, "bold"), foreground=TEXT_COLOR).pack(anchor=tk.W, pady=(16, 2))
        location = tk.StringVar(value=str(default_root))
        row = ttk.Frame(body)
        row.pack(fill=tk.X)
        tk.Entry(row, textvariable=location, width=80, bg="white", fg="black", insertbackground="black", state="readonly", readonlybackground="white").pack(side=tk.LEFT, fill=tk.X, expand=True)

        def choose() -> None:
            chosen = filedialog.askdirectory(parent=dialog, title="Choose where backups go", initialdir=str(Path(location.get()).parent))
            if chosen:
                location.set(chosen)

        ttk.Button(row, text="Choose…", command=choose).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(
            body,
            text="Each run makes a new folder named by date and time, with one subfolder per kind of problem. "
                 "Every original is copied there and verified before it is changed; it keeps its name and place.",
            foreground=HINT_TEXT_COLOR, wraplength=620, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))
        answer: dict[str, Optional[Path]] = {"root": None}

        def start() -> None:
            root = Path(location.get()).expanduser()
            guard = scan_root or project_root_for(files)
            if is_inside(root, guard):
                messagebox.showerror(APP_NAME, "Choose a backup location outside the folder being repaired — anything inside it would be scanned again as more takes.", parent=dialog)
                return
            needed = sum(path.stat().st_size for path in files)
            probe = root
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            free = shutil.disk_usage(probe).free
            if free < needed * 1.05:
                messagebox.showerror(APP_NAME, f"Not enough free space for the backups: {format_size(needed)} needed, {format_size(free)} free.", parent=dialog)
                return
            answer["root"] = root
            dialog.destroy()

        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X, pady=(16, 0))
        ttk.Button(buttons, text="Start Repair", command=start).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT, padx=(0, 6))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.grab_set()
        self.root.wait_window(dialog)
        return answer["root"]

    def _repair_worker(self, items: list[tuple[InspectionReport, RepairPlan]], backup_root: Path, scan_root: Optional[Path]) -> None:
        try:
            run_folder, results = run_repairs(
                items, backup_root, scan_root, f"{APP_NAME} {VERSION}",
                progress=lambda index, total, path: self.events.put(("repair_progress", index, total, path)),
                step=lambda index, total, path, message: self.events.put(("repair_step", index, total, path, message)),
                finished=lambda index, total, result: self.events.put(("repair_file_done", index, total, result)),
            )
            self.events.put(("repair_done", run_folder, results))
        except Exception as exc:
            self.events.put(("repair_failed", str(exc)))

    def _apply_repair_results(self, run_folder: Path, results: list[RepairResult]) -> None:
        self.repair_running = False
        self._set_repair_buttons(tk.NORMAL)
        self.progress_window.done(run_folder, results)
        touched = {Path(result.path) for result in results if result.status != "skipped"}
        for result in results:
            if result.status == "repaired":
                self.recover_confirmed.set_confirmed(Path(result.path), False)
                self.recover_plans.pop(result.path, None)
            if result.status == "repaired" and result.category == REBUILD_CATEGORY:
                self.drafts.discard(Path(result.path))
                self.rebuild_plans.pop(result.path, None)
        # Only the files Repair touched are re-inspected, not the whole folder.
        self.results = [(path, inspect_wav(path) if path in touched else report) for path, report in self.results]
        self._refill_table()
        if self.inspect_report is not None and Path(self.inspect_report.path) in touched:
            self._show_inspect_report(inspect_wav(Path(self.inspect_report.path)))
        counts = {status: sum(result.status == status for result in results) for status in ("repaired", "skipped", "failed")}
        self.browse_summary_var.set(f"Repair done — {counts['repaired']} repaired, {counts['skipped']} not touched, {counts['failed']} failed. " + self.browse_summary_var.get())

    # --- Diagnose File -------------------------------------------------------

    def inspect_file(self, path: Path) -> None:
        self.mode_var.set(MODE_INSPECT)
        self._show_mode()
        self.inspect_generation += 1
        self.inspect_title_var.set(f"{path.name} — diagnosing…")
        self._set_report_text(self.inspect_text, "")
        threading.Thread(target=self._inspect_worker, args=(self.inspect_generation, path), daemon=True).start()

    def _inspect_worker(self, generation: int, path: Path) -> None:
        self.events.put(("inspect_result", generation, inspect_wav(path)))

    def _show_inspect_report(self, report: Optional[InspectionReport]) -> None:
        if report is None:
            return
        self.mode_var.set(MODE_INSPECT)
        self._show_mode()
        self.inspect_report = report
        path = Path(report.path)
        self._set_window_subject(path.name)
        self.inspect_title_var.set(f"{path.name}    {SEVERITY_SYMBOLS[report.worst_severity if report.worst_severity != 'info' else 'ok']} {summary_line(report)}")
        self._set_report_text(self.inspect_text, self._report_with_repair_line(report))
        self._show_review(self.inspect_detail_area, report)

    def copy_inspect_report(self) -> None:
        if self.inspect_report is None:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(f"{self.inspect_report.path}\n\n{format_report_text(self.inspect_report)}")

    def export_inspect_report(self) -> None:
        if self.inspect_report is None:
            return
        name = Path(self.inspect_report.path).stem
        target = filedialog.asksaveasfilename(title="Export diagnosis report", defaultextension=".json", initialfile=f"{name}_diagnosis.json", filetypes=[("JSON", "*.json")])
        if target:
            Path(target).write_text(json.dumps(report_bundle([self.inspect_report]), indent=2, ensure_ascii=False), encoding="utf-8")

    # --- shared -------------------------------------------------------------

    def _reveal(self, report: Optional[InspectionReport]) -> None:
        if report is not None and Path(report.path).exists():
            subprocess.Popen(["open", "-R", report.path])

    def _drain_events(self) -> None:
        handled = 0
        try:
            while handled < 200:
                event = self.events.get_nowait()
                handled += 1
                kind = event[0]
                if kind == "scan_result" and event[1] == self.scan_generation:
                    _kind, _generation, path, report = event
                    self.results.append((path, report))
                    self._insert_row(path, report)
                    if len(self.results) % 25 == 0:
                        self._update_browse_summary(done=False)
                elif kind == "scan_done" and event[1] == self.scan_generation:
                    self.scanning = False
                    self.sorter.reapply()
                    self._update_browse_summary(done=True)
                    self.log_window.append(self.browse_summary_var.get())
                elif kind == "inspect_result" and event[1] == self.inspect_generation:
                    self._show_inspect_report(event[2])
                elif kind == "repair_progress":
                    _kind, index, total, path = event
                    self.browse_summary_var.set(f"Repairing {index}/{total} — {path.name}")
                elif kind == "repair_step":
                    _kind, index, total, path, message = event
                    self.progress_window.step(index, total, path, message)
                elif kind == "repair_file_done":
                    _kind, index, total, result = event
                    self.progress_window.finished(index, total, result)
                elif kind == "rebuild_plan_ready":
                    self._on_plan_ready(event[1], event[2])
                elif kind == "log":
                    self.log_window.append(event[1])
                elif kind == "repair_done":
                    self._apply_repair_results(event[1], event[2])
                elif kind == "repair_failed":
                    self.repair_running = False
                    self._set_repair_buttons(tk.NORMAL)
                    self.progress_window.append(f"Repair stopped: {event[1]}")
                    messagebox.showerror(APP_NAME, f"Repair stopped: {event[1]}")
        except Empty:
            pass
        self._drain_after = self.root.after(50, self._drain_events)

    def _on_close(self) -> None:
        # Stop the event pump first, so nothing fires against a destroyed window.
        try:
            self.root.after_cancel(self._drain_after)
        except (AttributeError, tk.TclError):
            pass
        self.inbox.stop()
        self.settings.update({
            "geometry": self.root.geometry(),
            "recursive": self.recursive_var.get(),
            "only_issues": self.only_issues_var.get(),
        })
        save_settings(self.settings)
        self.root.destroy()


def main() -> None:
    root = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
    WaveRepairApp(root, sys.argv[1:])
    root.mainloop()


if __name__ == "__main__":
    main()
