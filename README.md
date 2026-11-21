# Audio Post Tools

macOS apps for production sound and dialogue editing, by hsuanice.

| App | What it does |
|---|---|
| **Wave Manager for Post** | Manages field-recorder WAV files: split Poly files into mono files and keep their metadata; re-embed metadata into rendered files (for example after a DAW render); rename from metadata; combine mono files into a Poly file; QC (compare files, folders, or a Poly file with its split files); Audio Inspector. |
| **Wave Repair for Post** | Diagnoses damaged WAV files (container, chunks, padding, audio data) and repairs only what is certain, after a verified backup. |

Both apps share the same engine (`operations/`).

## Install

Requirements: a Mac with Apple Silicon (M1 or later).

1. Open [Releases](https://github.com/hsuanice/AudioPostTools/releases) and download the zip of the app you want.
2. Unzip it and drag the app into **Applications**.
3. The first time you open it, macOS blocks it, because the app is not signed by Apple. Right-click the app and choose **Open**, then **Open** again. (Or: System Settings → Privacy & Security → **Open Anyway**.) You need to do this again after each new version.
4. Wave Manager for Post uses two free command-line tools, **SoX** and **BWF MetaEdit**. If they are missing, the app offers to install them with Homebrew (a Terminal window opens and runs `brew install sox bwfmetaedit`).

## Updates

The app checks for a new version on GitHub once a day. You can also check any time: in the menu bar, click the app's name (for example **Wave Manager for Post**) → **Check for Updates…**. Turn the daily check off there with **Check for Updates Automatically**. When there is one, it shows what changed and opens the download page. Download the new zip and replace the app in Applications.

Each release is tagged by app and version: `wmfp-v261121.2036` (Wave Manager for Post), `wrfp-v261003.1312` (Wave Repair for Post). Versions are the release date and time, `YYMMDD.HHMM` (Taipei time). Releases marked **Pre-release** are not tested yet.

## Run from source

For development. Requires Python 3.13 with Tk.

```sh
pip3 install -r requirements.txt
brew install sox bwfmetaedit
./"Launch Wave Manager for Post.command"
./"Launch Wave Repair for Post.command"
```

`wave_manager.py` and `wave_repair.py` are the two apps; `operations/` is the shared engine; `Changelogs/` has the full history of each app.

Build the apps with `Build Wave Manager for Post.command` and `Build Wave Repair for Post.command` (PyInstaller). Tests: `python3 -m unittest discover -s tests`.

## Credits

- [SoX](https://sourceforge.net/projects/sox/): splitting and reading audio.
- [BWF MetaEdit](https://mediaarea.net/BWFMetaEdit) by MediaArea: reading and writing BWF and iXML metadata.
- [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2): drag and drop.
- [openpyxl](https://openpyxl.readthedocs.io/): QC reports in Excel format.
- [PyInstaller](https://pyinstaller.org/): building the macOS apps.
- [Homebrew](https://brew.sh/): installing SoX and BWF MetaEdit.
- The Metadata Template tokens are ported from hsuanice's REAPER script "Rename Source File from Metadata" ([hsuanice/ReaScripts](https://github.com/hsuanice/ReaScripts)).

These tools are installed separately or bundled by PyInstaller. Each keeps its own license.

## Built with AI

These apps were written by hsuanice with AI coding assistants: Claude and Claude Code (Anthropic). hsuanice designed and tested every feature on real production files.

## Releasing (maintainer)

```sh
python3 .dev/release.py wmfp        # or wrfp; add --pre for an untested version
```

It checks that the commit is on GitHub, runs the tests, builds and zips the app, and opens GitHub's new-release page with the tag and notes filled in. Drag the zip onto the page and publish.

## License

No license. The apps are shared with friends, free to use. The source code is here to read; all rights reserved.
