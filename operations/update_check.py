"""Check GitHub Releases for a newer version of an app.

Both apps are released from one repository, hsuanice/AudioPostTools. Each
release is tagged with its app's prefix and version, for example
"wmfp-v261121.2103" (Wave Manager for Post) or "wrfp-v261003.1312" (Wave
Repair for Post), so each app looks only at its own releases. Releases
marked "pre-release" (not tested yet) and drafts are ignored.

The request goes through /usr/bin/curl, which uses macOS's own certificate
store: a Python built into the .app by PyInstaller may not find any CA
certificates on a friend's Mac. Nothing about the user is sent; the request
is a plain anonymous GitHub API call.

The app never replaces itself: it shows what changed and opens the download
page. The state (automatic check on/off, time of the last check) is kept in
update_check.json next to the app's other settings.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPOSITORY = "hsuanice/AudioPostTools"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=50"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases"
TAG_PREFIXES = {
    "Wave Manager for Post": "wmfp",
    "Wave Repair for Post": "wrfp",
}
AUTO_CHECK_INTERVAL = 24 * 60 * 60  # seconds
VERSION_RE = re.compile(r"^(\d{6})\.(\d{4})$")


@dataclass
class Release:
    tag: str
    version: str
    url: str
    notes: str


def parse_version(text: str) -> Optional[tuple[int, int]]:
    """"261121.2103" -> (261121, 2103); anything else -> None."""
    match = VERSION_RE.match(text.strip())
    return (int(match[1]), int(match[2])) if match else None


def release_tag(app_name: str, version: str) -> str:
    return f"{TAG_PREFIXES[app_name]}-v{version}"


def latest_release(releases: list, app_name: str) -> Optional[Release]:
    """The newest published, non-pre-release release of this app, from the
    GitHub API's release list."""
    prefix = TAG_PREFIXES[app_name] + "-v"
    best: Optional[Release] = None
    best_key: Optional[tuple[int, int]] = None
    for item in releases:
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
            continue
        tag = str(item.get("tag_name") or "")
        if not tag.startswith(prefix):
            continue
        version = tag[len(prefix):]
        key = parse_version(version)
        if key is None:
            continue
        if best_key is None or key > best_key:
            best_key = key
            best = Release(tag, version, str(item.get("html_url") or RELEASES_PAGE), str(item.get("body") or ""))
    return best


def newer_release(releases: list, app_name: str, current_version: str) -> Optional[Release]:
    """The latest release if it is newer than the running version, else None."""
    latest = latest_release(releases, app_name)
    current = parse_version(current_version)
    if latest is None or current is None:
        return None
    return latest if parse_version(latest.version) > current else None


def fetch_releases(timeout: int = 10) -> list:
    """Raises RuntimeError with a readable message when GitHub can't be reached."""
    try:
        completed = subprocess.run(
            ["/usr/bin/curl", "-fsSL", "--max-time", str(timeout),
             "-H", "Accept: application/vnd.github+json",
             "-A", "AudioPostTools-update-check", RELEASES_API],
            capture_output=True, text=True, timeout=timeout + 5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not reach GitHub ({exc}).") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"curl exit code {completed.returncode}"
        raise RuntimeError(f"Could not reach GitHub ({detail}).")
    try:
        data = json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError("GitHub sent an answer that could not be read.") from exc
    if not isinstance(data, list):
        raise RuntimeError("GitHub sent an unexpected answer.")
    return data


def state_path(app_name: str) -> Path:
    return Path.home() / "Library" / "Application Support" / app_name / "update_check.json"


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"auto": True, "last_check": 0}
    if not isinstance(data, dict):
        return {"auto": True, "last_check": 0}
    return {"auto": bool(data.get("auto", True)), "last_check": float(data.get("last_check", 0) or 0)}


def save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def auto_check_due(state: dict, now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    return bool(state.get("auto", True)) and now - float(state.get("last_check", 0) or 0) >= AUTO_CHECK_INTERVAL


def short_notes(notes: str, limit: int = 1200) -> str:
    text = notes.strip().replace("\r\n", "\n")
    return text if len(text) <= limit else text[:limit].rstrip() + "\n…"
