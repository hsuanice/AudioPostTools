#!/usr/bin/env python3
"""Prepare a GitHub Release of one app (local maintenance tool).

usage (from the repository root):
  python3 .dev/release.py wmfp          # Wave Manager for Post
  python3 .dev/release.py wrfp          # Wave Repair for Post
  add --pre for a version hsuanice has not tested yet (GitHub "pre-release":
  the apps' update check ignores it)
  add --skip-tests only when the tests already ran on this exact commit

What it does, stopping at the first problem:
  1. checks that every change is committed and pushed (origin/main = this
     commit), so the release's tag points at the code that was built
  2. runs all tests
  3. builds the .app with PyInstaller (the app's .spec), into dist/
  4. zips it with ditto (keeps the macOS app bundle intact):
     dist/release/<App Name> <VERSION>.zip
  5. writes the release notes (this version's section of Changelogs/<App>.md)
     to dist/release/<tag>.md
  6. opens GitHub's "new release" page with tag, title and notes filled in,
     and shows the zip in Finder

hsuanice then drags the zip onto the page and clicks Publish release.
The tag (wmfp-v<VERSION> / wrfp-v<VERSION>) is created by GitHub on publish.
"""
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "hsuanice/AudioPostTools"
APPS = {
    "wmfp": ("Wave Manager for Post", "wave_manager.py"),
    "wrfp": ("Wave Repair for Post", "wave_repair.py"),
}
MAX_URL = 7000  # longer notes are pasted by hand from the .md file


def fail(message: str) -> None:
    print(f"\nSTOPPED: {message}")
    sys.exit(1)


def run(command: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=ROOT, **kwargs)


def git(*args: str) -> str:
    return run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def app_version(script: str) -> str:
    match = re.search(r'^VERSION = "([^"]+)"', (ROOT / script).read_text(encoding="utf-8"), re.MULTILINE)
    if not match or not re.fullmatch(r"\d{6}\.\d{4}", match[1]):
        fail(f"no VERSION = \"YYMMDD.HHMM\" in {script}")
    return match[1]


def release_notes(app_name: str, version: str) -> str:
    text = (ROOT / "Changelogs" / f"{app_name}.md").read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(version)}\b.*?$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        fail(f"Changelogs/{app_name}.md has no section \"## {version}\"")
    title = match[0].splitlines()[0][3:].strip()
    return f"**{title}**\n\n{match[1].strip()}\n"


def main() -> None:
    args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    flags = {arg for arg in sys.argv[1:] if arg.startswith("--")}
    if len(args) != 1 or args[0] not in APPS or flags - {"--pre", "--skip-tests"}:
        print(__doc__)
        sys.exit(2)
    key = args[0]
    app_name, script = APPS[key]
    version = app_version(script)
    tag = f"{key}-v{version}"
    prerelease = "--pre" in flags
    print(f"{app_name} {version}  ->  tag {tag}{'  (pre-release)' if prerelease else ''}")

    # 1. committed and pushed
    if git("status", "--porcelain"):
        fail("there are uncommitted changes; commit them first")
    run(["git", "fetch", "--quiet", "origin"], check=False)
    head = git("rev-parse", "HEAD")
    remote = run(["git", "rev-parse", "origin/main"], capture_output=True, text=True).stdout.strip()
    if head != remote:
        fail("this commit is not on GitHub yet; press Sync Changes in VS Code first")
    existing = run(["git", "ls-remote", "--tags", "origin", tag], capture_output=True, text=True).stdout.strip()
    if existing:
        fail(f"tag {tag} already exists on GitHub; bump VERSION for a new release")
    notes = release_notes(app_name, version)

    # 2. tests
    if "--skip-tests" not in flags:
        print("\nRunning tests...")
        if run([sys.executable, "-m", "unittest", "discover", "-s", "tests"]).returncode != 0:
            fail("tests failed")

    # 3. build
    print(f"\nBuilding {app_name}.app...")
    for leftover in (ROOT / "build" / app_name, ROOT / "dist" / app_name, ROOT / "dist" / f"{app_name}.app"):
        if leftover.exists():
            run(["rm", "-rf", str(leftover)], check=True)
    if run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", f"{app_name}.spec"]).returncode != 0:
        fail("PyInstaller build failed")
    app_path = ROOT / "dist" / f"{app_name}.app"
    if not app_path.is_dir():
        fail(f"{app_path} was not created")

    # 4. zip
    out = ROOT / "dist" / "release"
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / f"{app_name} {version}.zip"
    zip_path.unlink(missing_ok=True)
    run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app_path), str(zip_path)], check=True)

    # 5. notes
    notes_path = out / f"{tag}.md"
    notes_path.write_text(notes, encoding="utf-8")

    # 6. GitHub page + Finder
    query = {"tag": tag, "target": "main", "title": f"{app_name} {version}", "body": notes}
    if prerelease:
        query["prerelease"] = "1"
    url = f"https://github.com/{REPOSITORY}/releases/new?" + urllib.parse.urlencode(query)
    notes_in_url = len(url) <= MAX_URL
    if not notes_in_url:
        query.pop("body")
        url = f"https://github.com/{REPOSITORY}/releases/new?" + urllib.parse.urlencode(query)
    run(["open", url], check=False)
    run(["open", "-R", str(zip_path)], check=False)

    size = zip_path.stat().st_size / 1_000_000
    print(f"\nREADY: {zip_path.name} ({size:.1f} MB)")
    print("On the GitHub page that just opened:")
    if not notes_in_url:
        print(f"  - paste the notes from {notes_path.relative_to(ROOT)} (too long to fill in automatically)")
    print("  - drag the zip (shown in Finder) onto \"Attach binaries\"")
    print(f"  - {'keep' if prerelease else 'leave off'} \"Set as a pre-release\", then click Publish release")


if __name__ == "__main__":
    main()
