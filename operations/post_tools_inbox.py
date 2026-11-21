"""Post Tools Inbox: files sent from Soundminer (or anything else) to the apps.

Soundminer is where files are browsed; its Lua scripts hand a selection over
by writing a *batch* file here and starting the app if it isn't running. A
running app looks at this folder about once a second (one directory listing —
nothing is read until a batch appears) and picks up the batches meant for it.

A batch is plain text, so Lua can write it a line at a time:

    #app=wave_repair
    #action=diagnose
    /path/to/one.wav
    /path/to/two.wav

Soundminer calls a script once per selected record with no "finished" call,
so a batch counts as complete once it has stopped growing for
SETTLE_SECONDS. Handled batches move to Received/ (the last KEEP_RECEIVED
are kept, for checking what was sent); batches nobody handled within MAX_AGE
are moved there too, instead of opening files long after they were sent.
A file named "Paused" in the inbox stops every app from receiving.

The folder belongs to no single app, so later tools can share it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

INBOX_DIR = Path.home() / "Library" / "Application Support" / "Post Tools Inbox"
RECEIVED_DIR_NAME = "Received"
PAUSED_FLAG_NAME = "Paused"
BATCH_SUFFIX = ".batch"
SETTLE_SECONDS = 1.5
MAX_AGE = 10 * 60
KEEP_RECEIVED = 50

WAVE_REPAIR = "wave_repair"
WAVE_MANAGER = "wave_manager"
# app id → (app name, source script beside operations/)
APPS = {
    WAVE_REPAIR: ("Wave Repair for Post", "wave_repair.py"),
    WAVE_MANAGER: ("Wave Manager for Post", "wave_manager.py"),
}
APP_DIR = Path(__file__).resolve().parents[1]


@dataclass
class Batch:
    path: Path
    app: str = ""
    action: str = ""
    files: list[Path] = field(default_factory=list)


def parse_batch(path: Path, text: str) -> Batch:
    batch = Batch(path)
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            key, _sep, value = line[1:].partition("=")
            if key.strip() == "app":
                batch.app = value.strip()
            elif key.strip() == "action":
                batch.action = value.strip()
            continue
        file = Path(line)
        if file not in batch.files:
            batch.files.append(file)
    return batch


def is_paused(inbox: Optional[Path] = None) -> bool:
    return ((inbox or INBOX_DIR) / PAUSED_FLAG_NAME).exists()


def ready_batches(app: str, inbox: Optional[Path] = None, now: Optional[float] = None) -> list[Batch]:
    """This app's complete batches, oldest first. Expired ones are moved
    to Received/ on the way and not returned."""
    inbox = inbox or INBOX_DIR
    now = time.time() if now is None else now
    if is_paused(inbox):
        return []
    try:
        entries = sorted((entry for entry in os.scandir(inbox) if entry.name.endswith(BATCH_SUFFIX) and entry.is_file()), key=lambda entry: entry.name)
    except OSError:
        return []
    batches = []
    for entry in entries:
        try:
            age = now - entry.stat().st_mtime
            if age < SETTLE_SECONDS:
                continue
            batch = parse_batch(Path(entry.path), Path(entry.path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if batch.app != app:
            continue
        if age > MAX_AGE:
            mark_received(batch, inbox, note="expired")
            continue
        batches.append(batch)
    return batches


def mark_received(batch: Batch, inbox: Optional[Path] = None, note: str = "") -> None:
    inbox = inbox or INBOX_DIR
    received = inbox / RECEIVED_DIR_NAME
    try:
        received.mkdir(parents=True, exist_ok=True)
        name = batch.path.stem + (f" ({note})" if note else "") + BATCH_SUFFIX
        os.replace(batch.path, received / name)
    except OSError:
        try:
            batch.path.unlink()
        except OSError:
            pass
        return
    try:
        kept = sorted(received.glob("*" + BATCH_SUFFIX), key=lambda item: item.stat().st_mtime)
        for old in kept[:-KEEP_RECEIVED]:
            old.unlink()
    except OSError:
        pass


# --- which app is running ------------------------------------------------------

def _running_note(app: str, inbox: Optional[Path] = None) -> Path:
    return (inbox or INBOX_DIR) / f".running-{app}"


def register_running(app: str, inbox: Optional[Path] = None) -> None:
    note = _running_note(app, inbox)
    try:
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def unregister_running(app: str, inbox: Optional[Path] = None) -> None:
    note = _running_note(app, inbox)
    try:
        if note.read_text(encoding="utf-8").strip() == str(os.getpid()):
            note.unlink()
    except OSError:
        pass


def is_running(app: str, inbox: Optional[Path] = None) -> bool:
    """Whether the app that last registered is still alive — checked by its
    process id and command line, so a crash (no unregister) or a reused
    process id doesn't count."""
    try:
        pid = int(_running_note(app, inbox).read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    name, script = APPS[app]
    try:
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return script in command or name in command


def launch_command(app: str, frozen: bool = False, python: Optional[str] = None, app_dir: Optional[Path] = None) -> list[str]:
    name, script = APPS[app]
    if frozen:
        return ["open", "-a", name]
    return [python or sys.executable, str((app_dir or APP_DIR) / script)]


def launch(app: str, inbox: Optional[Path] = None) -> bool:
    """Start the app unless it's already running (it then finds the batch on
    its own). Returns whether it was started."""
    if is_running(app, inbox):
        return False
    env = dict(os.environ, TK_SILENCE_DEPRECATION="1")
    process = subprocess.Popen(launch_command(app), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    # Registered right away (the app registers itself again once up), so a
    # second send while it is still starting doesn't start another copy.
    note = _running_note(app, inbox)
    try:
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(str(process.pid), encoding="utf-8")
    except OSError:
        pass
    return True


class InboxPoller:
    """Checks the inbox from the Tk main loop. `handle(batch)` returns True
    once the batch is dealt with; False leaves it for the next check (the
    app is busy) — until it expires."""

    def __init__(self, root, app: str, handle: Callable[[Batch], bool], inbox: Optional[Path] = None, interval_ms: int = 1000) -> None:
        self.root = root
        self.app = app
        self.handle = handle
        self.inbox = inbox or INBOX_DIR
        self.interval_ms = interval_ms
        self._after: Optional[str] = None
        register_running(app, self.inbox)
        self._after = self.root.after(200, self._tick)

    def check(self) -> None:
        for batch in ready_batches(self.app, self.inbox):
            if self.handle(batch):
                mark_received(batch, self.inbox)

    def _tick(self) -> None:
        try:
            self.check()
        except Exception:
            pass  # never let a bad batch stop the polling
        finally:
            self._after = self.root.after(self.interval_ms, self._tick)

    def stop(self) -> None:
        if self._after is not None:
            try:
                self.root.after_cancel(self._after)
            except Exception:
                pass
            self._after = None
        unregister_running(self.app, self.inbox)


def bring_to_front(window) -> None:
    """Show a Tk window above everything, even when the app isn't the
    active one (it was just sent files from Soundminer)."""
    try:
        window.deiconify()
        window.lift()
        window.attributes("-topmost", True)
        window.after(400, lambda: window.attributes("-topmost", False))
        window.focus_force()
    except Exception:
        pass


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "launch" and argv[1] in APPS:
        launch(argv[1])
        return 0
    print("usage: python3 -m operations.post_tools_inbox launch " + "|".join(APPS), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
