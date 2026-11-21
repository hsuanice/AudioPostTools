from __future__ import annotations

import tkinter as tk
import re
from pathlib import Path
from tkinter import ttk
from tkinterdnd2 import DND_FILES


# The shared look of Wave Manager for Post and Wave Repair for Post: ttk's
# light "clam" theme everywhere (so both stay light regardless of the macOS
# appearance), plus the handful of colors both apps' own widgets use.
TEXT_COLOR = "#111827"
MUTED_TEXT_COLOR = "#4b5563"
HINT_TEXT_COLOR = "#666666"
PANEL_BACKGROUND = "#f3f4f6"
TABLE_BACKGROUND = "white"
SECTION_BACKGROUND = "#e5e7eb"
ERROR_COLOR = "#b91c1c"
WARNING_COLOR = "#b45309"
INFO_COLOR = "#1d4ed8"


def apply_app_theme(root: tk.Misc) -> ttk.Style:
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("Toolbutton", padding=(10, 5))
    # The "clam" theme's default Combobox insert-cursor color is
    # invisible against a white field — every plain tk.Entry in this app
    # already sets bg/fg/insertbackground explicitly for the same
    # reason; the editable formula-preset Comboboxes need the ttk
    # equivalent (fieldbackground/foreground/insertcolor) so the caret
    # is visible while typing a formula.
    style.configure("Formula.TCombobox", fieldbackground="white", foreground="black", insertcolor="black")
    # clam's default progress-bar fill is the window background color itself,
    # so a full bar looks empty.
    style.configure("Horizontal.TProgressbar", background=INFO_COLOR, lightcolor=INFO_COLOR, darkcolor=INFO_COLOR, troughcolor=SECTION_BACKGROUND)
    return style


def first_dropped_folder(paths: list[str]) -> Path | None:
    return next((Path(path).expanduser() for path in paths if Path(path).expanduser().is_dir()), None)


def first_dropped_qc_input(paths: list[str]) -> Path | None:
    return next((path for value in paths for path in [Path(value).expanduser()] if path.is_dir() or (path.is_file() and path.suffix.lower() == ".wav")), None)


def treeview_cell_sort_key(value: object, numeric: bool = False) -> tuple[object, ...]:
    text = str(value).strip()
    if text in ("", "-", "—"):
        return (0,)
    if numeric:
        return (1, int(text)) if text.isdigit() else (1, float("inf"), text.casefold())
    parts = re.split(r"(\d+)", text.casefold())
    natural = tuple((1, int(part)) if part.isdigit() else (0, part) for part in parts)
    return (1, natural)


class TreeviewSorter:
    def __init__(self, tree: ttk.Treeview, labels: dict[str, str], numeric_columns: set[str] | None = None, pinned_tags: set[str] | None = None, column_groups: dict[str, tuple[str, ...]] | None = None) -> None:
        self.tree = tree
        self.labels = labels
        self.numeric_columns = numeric_columns or set()
        self.pinned_tags = pinned_tags or set()
        self.column_groups = column_groups or {}
        self.column: str | None = None
        self.reverse = False
        for column, label in labels.items():
            tree.heading(column, text=label, command=lambda selected=column: self.sort(selected))

    def sort(self, column: str, preserve_direction: bool = False) -> None:
        if not preserve_direction:
            if self.column == column:
                self.reverse = not self.reverse
            else:
                self.column = column
                self.reverse = False
        rows = list(self.tree.get_children(""))
        ordered: list[str] = []
        segment: list[str] = []

        def append_segment() -> None:
            sort_columns = self.column_groups.get(column, (column,))
            segment.sort(
                key=lambda iid: tuple(treeview_cell_sort_key(self.tree.set(iid, sort_column), sort_column in self.numeric_columns) for sort_column in sort_columns),
                reverse=self.reverse,
            )
            ordered.extend(segment)
            segment.clear()

        for iid in rows:
            if self.pinned_tags.intersection(self.tree.item(iid, "tags")):
                append_segment()
                ordered.append(iid)
            else:
                segment.append(iid)
        append_segment()
        for index, iid in enumerate(ordered):
            self.tree.move(iid, "", index)
        for table_column, label in self.labels.items():
            indicator = " ▼" if self.reverse else " ▲"
            self.tree.heading(table_column, text=label + indicator if table_column == self.column else label)

    def reapply(self) -> None:
        if self.column is not None:
            self.sort(self.column, preserve_direction=True)


def resize_wrapped_label(event, label: ttk.Label, padding: int = 4, minimum: int = 120, tolerance: int = 8) -> None:
    """Keep a path Label's wraplength tracking its own allocated cell width,
    so the full path is always visible (wrapped onto more lines) instead of
    being hard-clipped when the window is narrower than a fixed wraplength.
    `tolerance` ignores sub-threshold width changes — re-wrapping changes the
    label's requested height, which can perturb the surrounding grid's width
    negotiation by a couple of pixels and re-fire <Configure>; without this
    guard that never converges (see AudioInspector._resize_path_label for
    the same fix applied to the Inspector's own path labels)."""
    new_wraplength = max(minimum, event.width - padding)
    try:
        current_wraplength = int(label.cget("wraplength"))
    except (TypeError, ValueError):
        current_wraplength = 0
    if abs(new_wraplength - current_wraplength) < tolerance:
        return
    label.configure(wraplength=new_wraplength)


class QcInputPicker(ttk.Frame):
    def __init__(self, parent, source_variable: tk.StringVar, target_variable: tk.StringVar, choose_file, choose_folder, changed) -> None:
        super().__init__(parent)
        self.source_variable = source_variable
        self.target_variable = target_variable
        self.changed = changed
        for column, (side, title, variable) in enumerate((("source", "SOURCE", source_variable), ("target", "TARGET", target_variable))):
            panel = ttk.LabelFrame(self, text=title, padding=8)
            panel.grid(row=0, column=column, sticky="nsew", padx=(0, 4) if column == 0 else (4, 0))
            ttk.Button(panel, text="Load WAV", command=lambda selected_side=side: choose_file(selected_side)).grid(row=0, column=0)
            ttk.Button(panel, text="Select Folder", command=lambda selected_side=side: choose_folder(selected_side)).grid(row=0, column=1, padx=(8, 0))
            path_label = ttk.Label(panel, textvariable=variable, wraplength=420, justify=tk.LEFT)
            path_label.grid(row=0, column=2, sticky="ew", padx=(10, 0))
            path_label.bind("<Configure>", lambda event, widget=path_label: resize_wrapped_label(event, widget))
            panel.columnconfigure(2, weight=1)
            for widget in (panel, *panel.winfo_children()):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", lambda event, selected_side=side: self._drop_input(event, selected_side))
            self.columnconfigure(column, weight=1, uniform="qc-input")

    def _drop_input(self, event, side: str) -> str:
        path = first_dropped_qc_input(list(self.tk.splitlist(event.data)))
        if path is not None:
            self.changed(side, path)
        return "break"


class ExecutionLogPanel(ttk.LabelFrame):
    def __init__(self, parent, status_variable: tk.StringVar) -> None:
        super().__init__(parent, padding=8)
        header = ttk.Frame(self)
        ttk.Label(header, text="Execution log").pack(side=tk.LEFT)
        ttk.Label(header, textvariable=status_variable).pack(side=tk.LEFT, padx=(16, 0))
        self.configure(labelwidget=header)
        self.text = tk.Text(self, height=9, wrap=tk.WORD, state=tk.DISABLED, font=("SF Mono", 10))
        self.text.pack(fill=tk.BOTH, expand=True)

    def append(self, text: str) -> None:
        self.text.configure(state=tk.NORMAL)
        self.text.insert(tk.END, text + "\n")
        self.text.see(tk.END)
        self.text.configure(state=tk.DISABLED)


class ExecutionLogWindow:
    """The Execution Log as its own floating window instead of a fixed
    chunk of every mode's vertical layout — Rename mode's panel in
    particular can already run tall enough (Metadata Template's token
    buttons) to push a docked log off a screen that's already at its
    maximum height, with nothing the user could do about it. Created once
    at startup and kept alive (withdrawn, not destroyed) so background
    logging always has somewhere to go regardless of whether the window is
    currently visible. Shared by Wave Manager and Wave Repair so both
    tools' logs look and behave the same; `status_variable` is the app's
    live status line, shown in the log's header."""

    def __init__(self, root: tk.Misc, status_variable: tk.StringVar, title: str = "Execution Log") -> None:
        self.window = tk.Toplevel(root)
        self.window.title(title)
        self.window.geometry("900x340")
        self.window.minsize(480, 200)
        self.window.protocol("WM_DELETE_WINDOW", self.window.withdraw)
        self.panel = ExecutionLogPanel(self.window, status_variable)
        self.panel.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.window.withdraw()

    def show(self) -> None:
        self.window.deiconify()
        self.window.lift()

    def append(self, text: str) -> None:
        self.panel.append(text)


class WorkflowFolderPicker(ttk.Frame):
    def __init__(
        self,
        parent,
        source_variable: tk.StringVar,
        target_variable: tk.StringVar,
        choose_source,
        choose_target,
        source_changed,
        target_changed,
    ) -> None:
        super().__init__(parent)
        self.source_variable = source_variable
        self.target_variable = target_variable
        self.source_changed = source_changed
        self.target_changed = target_changed
        self.source_entry: tk.Entry | None = None
        self.target_entry: tk.Entry | None = None

        for column, (title, variable, choose_command, drop_command) in enumerate((
            ("SOURCE", source_variable, choose_source, self._drop_source),
            ("TARGET", target_variable, choose_target, self._drop_target),
        )):
            panel = ttk.LabelFrame(self, text=title, padding=8)
            panel.grid(row=0, column=column, sticky="nsew", padx=(0, 4) if column == 0 else (4, 0))
            entry = tk.Entry(panel, textvariable=variable, bg="white", fg="black", insertbackground="black")
            entry.grid(row=0, column=0, sticky="ew")
            if column == 0:
                self.source_entry = entry
            else:
                self.target_entry = entry
            ttk.Button(panel, text="Select Folder", command=choose_command).grid(row=0, column=1, padx=(8, 0))
            panel.columnconfigure(0, weight=1)
            for widget in (panel, *panel.winfo_children()):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", drop_command)
            self.columnconfigure(column, weight=1, uniform="workflow-folder")

    def _drop_folder(self, event, variable: tk.StringVar, changed) -> str:
        folder = first_dropped_folder(list(self.tk.splitlist(event.data)))
        if folder is not None:
            variable.set(str(folder))
            changed(folder)
        return "break"

    def _drop_source(self, event) -> str:
        return self._drop_folder(event, self.source_variable, self.source_changed)

    def _drop_target(self, event) -> str:
        return self._drop_folder(event, self.target_variable, self.target_changed)


def configure_treeview_column_widths(tree: ttk.Treeview, specs: tuple[tuple[str, int, bool], ...], stretch_column: str) -> None:
    """Configure width/minwidth/anchor/stretch for every column in `specs`
    (each a (column, width, is_text) tuple) so that only `stretch_column`
    absorbs leftover width on resize. ttk's Treeview otherwise redistributes
    width across *every* column marked stretch=True on each layout pass,
    silently overriding a manual drag-resize on any of them except by
    coincidence — this is the one place that decision is made, so every
    Treeview in both apps (Wave Manager's preview + QC tables, Wave Repair's file table) shares
    the same fix instead of re-deriving it per table.

    The implicit "#0" tree column is also part of this: show="headings"
    only hides it visually, it still keeps Tk's default width/stretch=True
    and still participates in ttk's layout pass, so it silently soaks up
    (or gives back) slack meant for `stretch_column` on every resize. It has
    to be pinned to width=0/stretch=False here too or a manual drag-resize
    on a real column keeps getting redistributed away from user intent."""
    tree.column("#0", width=0, minwidth=0, stretch=False)
    for column, width, is_text in specs:
        tree.column(column, width=width, minwidth=120 if is_text else width, stretch=column == stretch_column, anchor=tk.W if is_text else tk.CENTER)


class UpdateChecker:
    """"Check for Updates…" and "Check for Updates Automatically" in the
    macOS application menu (next to About / Quit), plus one quiet automatic
    check a few seconds after launch, at most once a day. The GitHub request
    runs on a worker thread; its result comes back to Tk through after().
    A quiet check says nothing unless there is a new version."""

    def __init__(self, root: tk.Tk, app_name: str, version: str) -> None:
        from operations import update_check

        self.root = root
        self.app_name = app_name
        self.version = version
        self.update_check = update_check
        self.state_path = update_check.state_path(app_name)
        self.state = update_check.load_state(self.state_path)
        self.auto_var = tk.BooleanVar(value=self.state["auto"])
        self.busy = False

        menubar = tk.Menu(root)
        app_menu = tk.Menu(menubar, name="apple", tearoff=False)
        app_menu.add_command(label="Check for Updates…", command=lambda: self.check(quiet=False))
        app_menu.add_checkbutton(label="Check for Updates Automatically", variable=self.auto_var, command=self._auto_changed)
        app_menu.add_separator()
        menubar.add_cascade(menu=app_menu)
        window_menu = tk.Menu(menubar, name="window", tearoff=False)
        menubar.add_cascade(menu=window_menu, label="Window")
        root.configure(menu=menubar)
        if update_check.auto_check_due(self.state):
            root.after(4000, lambda: self.check(quiet=True))

    def _auto_changed(self) -> None:
        self.state["auto"] = bool(self.auto_var.get())
        self.update_check.save_state(self.state_path, self.state)

    def check(self, quiet: bool) -> None:
        if self.busy:
            return
        self.busy = True
        import threading

        result: dict = {}

        def work() -> None:
            try:
                releases = self.update_check.fetch_releases()
                result["release"] = self.update_check.newer_release(releases, self.app_name, self.version)
            except Exception as exc:  # shown only for a manual check
                result["error"] = str(exc)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()

        def wait() -> None:
            if thread.is_alive():
                self.root.after(200, wait)
                return
            self.busy = False
            self._show(result, quiet)

        self.root.after(200, wait)

    def _show(self, result: dict, quiet: bool) -> None:
        import webbrowser

        if "error" in result:
            if not quiet:
                messagebox.showwarning("Check for Updates", f"{result['error']}\n\nTry again later, or open:\n{self.update_check.RELEASES_PAGE}", parent=self.root)
            return
        self.state["last_check"] = __import__("time").time()
        self.update_check.save_state(self.state_path, self.state)
        release = result.get("release")
        if release is None:
            if not quiet:
                messagebox.showinfo("Check for Updates", f"{self.app_name} {self.version} is the latest version.", parent=self.root)
            return
        # GitHub notes are Markdown; show them as plain text.
        notes = release.notes.strip().replace("\r\n", "\n").replace("**", "").replace("`", "")
        message = (
            f"{self.app_name} {release.version} is available. You have {self.version}.\n\n"
            "Download the zip, unzip it, and replace the app in Applications. "
            "The first time, right-click the new app and choose Open."
        )
        choice = show_dialog("Update Available", message, ("Later", "Open Download Page"), cancel="Later", kind="info",
                             parent=self.root, detail=f"What's new:\n\n{notes}" if notes else "")
        if choice == "Open Download Page":
            webbrowser.open(release.url)


# --- Message dialogs drawn by Tk ------------------------------------------
# tkinter.messagebox shows a native macOS alert (NSAlert). On recent macOS
# with a game controller paired (DualSense / DUALSHOCK, even when not
# connected), GameControllerUI attaches a navigation object to every alert,
# and Tk's autorelease pool then frees it twice: the app crashes with
# SIGTRAP right after the alert closes (reproduced with a 10-line Tk script,
# 2026-11-21). These dialogs are plain Tk windows with the same calls and
# return values, so the apps never open an NSAlert. File and folder
# pickers (NSOpenPanel / NSSavePanel) are not affected and stay native.

_DIALOG_KINDS = {
    "info": ("i", INFO_COLOR),
    "warning": ("!", WARNING_COLOR),
    "error": ("×", ERROR_COLOR),
    "question": ("?", INFO_COLOR),
}


def _dialog_parent(parent):
    if parent is not None:
        return parent
    root = getattr(tk, "_default_root", None)
    if root is None:
        root = tk.Tk()
        root.withdraw()
    return root


def show_dialog(title: str, message: str, buttons: tuple[str, ...] = ("OK",), default: str | None = None,
                cancel: str | None = None, kind: str = "info", parent=None, detail: str = "") -> str | None:
    """A modal Tk dialog. Returns the label of the button clicked; `cancel`
    (or None when there is none) for Esc / the window's close button.
    Return presses the default button (the last one unless given). Long
    text, or anything in `detail`, goes into a scrollable read-only box."""
    owner = _dialog_parent(parent)
    toplevel = owner.winfo_toplevel()
    default = default or buttons[-1]
    window = tk.Toplevel(toplevel)
    window.withdraw()
    window.title(title or "")
    window.configure(background=PANEL_BACKGROUND)
    window.resizable(False, False)
    if toplevel.winfo_viewable():
        window.transient(toplevel)
    result: dict[str, str | None] = {"value": cancel}

    def close(value: str | None) -> None:
        result["value"] = value
        try:
            window.grab_release()
        except tk.TclError:
            pass
        window.destroy()

    body = tk.Frame(window, background=PANEL_BACKGROUND, padx=18, pady=16)
    body.pack(fill=tk.BOTH, expand=True)
    symbol, color = _DIALOG_KINDS.get(kind, _DIALOG_KINDS["info"])
    tk.Label(body, text=symbol, font=("Helvetica", 22, "bold"), foreground="white", background=color, width=2).grid(row=0, column=0, sticky="n", padx=(0, 14))
    text_frame = tk.Frame(body, background=PANEL_BACKGROUND)
    text_frame.grid(row=0, column=1, sticky="nsew")
    message = str(message or "")
    long_text = detail or len(message) > 700 or message.count("\n") > 14
    if long_text and not detail:
        lead, rest = "", message
    else:
        lead, rest = message, str(detail or "")
    if lead:
        tk.Label(text_frame, text=lead, justify=tk.LEFT, anchor="w", wraplength=460, foreground=TEXT_COLOR, background=PANEL_BACKGROUND).pack(anchor="w", fill=tk.X)
    if rest:
        box = tk.Frame(text_frame, background=PANEL_BACKGROUND)
        box.pack(fill=tk.BOTH, expand=True, pady=(8 if lead else 0, 0))
        lines = min(18, max(4, rest.count("\n") + 2 + len(rest) // 70))
        text = tk.Text(box, width=64, height=lines, wrap=tk.WORD, relief=tk.FLAT, borderwidth=1, highlightthickness=0,
                       foreground=TEXT_COLOR, background="white", padx=8, pady=6, font="TkDefaultFont")
        scroll = ttk.Scrollbar(box, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", rest)
        text.configure(state=tk.DISABLED)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
    button_row = tk.Frame(body, background=PANEL_BACKGROUND)
    button_row.grid(row=1, column=0, columnspan=2, sticky="e", pady=(16, 0))
    default_button = None
    for label in buttons:
        button = ttk.Button(button_row, text=label, command=lambda value=label: close(value))
        button.pack(side=tk.LEFT, padx=(8, 0))
        if label == default:
            button.configure(default="active")
            default_button = button
    body.columnconfigure(1, weight=1)

    window.protocol("WM_DELETE_WINDOW", lambda: close(cancel))
    window.bind("<Return>", lambda _event: close(default))
    window.bind("<KP_Enter>", lambda _event: close(default))
    window.bind("<Escape>", lambda _event: close(cancel))

    window.update_idletasks()
    width, height = window.winfo_reqwidth(), window.winfo_reqheight()
    if toplevel.winfo_viewable():
        x = toplevel.winfo_rootx() + (toplevel.winfo_width() - width) // 2
        y = toplevel.winfo_rooty() + max(40, (toplevel.winfo_height() - height) // 3)
    else:
        x = (window.winfo_screenwidth() - width) // 2
        y = (window.winfo_screenheight() - height) // 3
    window.geometry(f"+{max(0, x)}+{max(0, y)}")
    window.deiconify()
    window.lift()
    try:
        window.grab_set()
    except tk.TclError:
        pass
    (default_button or window).focus_force()
    window.wait_window()
    return result["value"]


class TkMessageBox:
    """Same calls and return values as tkinter.messagebox, drawn by Tk
    (see the note above). Use: `from ui_components import messagebox`."""

    @staticmethod
    def showinfo(title=None, message=None, **options) -> str:
        show_dialog(title, message, ("OK",), cancel="OK", kind="info", parent=options.get("parent"), detail=options.get("detail", ""))
        return "ok"

    @staticmethod
    def showwarning(title=None, message=None, **options) -> str:
        show_dialog(title, message, ("OK",), cancel="OK", kind="warning", parent=options.get("parent"), detail=options.get("detail", ""))
        return "ok"

    @staticmethod
    def showerror(title=None, message=None, **options) -> str:
        show_dialog(title, message, ("OK",), cancel="OK", kind="error", parent=options.get("parent"), detail=options.get("detail", ""))
        return "ok"

    @staticmethod
    def askyesno(title=None, message=None, **options) -> bool:
        default = "No" if options.get("default") == "no" else "Yes"
        return show_dialog(title, message, ("No", "Yes"), default=default, cancel="No", kind="question", parent=options.get("parent"), detail=options.get("detail", "")) == "Yes"

    @staticmethod
    def askokcancel(title=None, message=None, **options) -> bool:
        default = "Cancel" if options.get("default") == "cancel" else "OK"
        return show_dialog(title, message, ("Cancel", "OK"), default=default, cancel="Cancel", kind=options.get("icon", "question") if options.get("icon") in _DIALOG_KINDS else "question", parent=options.get("parent"), detail=options.get("detail", "")) == "OK"


messagebox = TkMessageBox()
