# Changelog

## 261121.2233 — Messages are drawn by Tk: no more crash after a message closes

hsuanice: Check for Updates said "latest version", and the app quit unexpectedly right after the message closed.

- **Cause:** the native macOS alert behind `tkinter.messagebox`. On this macOS, with a game controller paired (DualSense / DUALSHOCK 4, even when not connected), GameControllerUI attaches a navigation object to every alert; Tk 9.0's autorelease pool then frees it twice, and the app stops with SIGTRAP as soon as the alert closes. A 10-line Tk script with one `showinfo` crashed the same way, so every message in the app could do it, not only the update check.
- **Fix: every message is now a window drawn by Tk** (`show_dialog` and `messagebox` in `ui_components.py`), with the same calls and answers as `tkinter.messagebox`: showinfo / showwarning / showerror / askyesno / askokcancel. Return presses the default button, Esc cancels, long text scrolls. The app never opens a native alert any more. File and folder pickers stay native (they are not affected). The "missing Python package" window at start-up is a plain Tk window too.
- **Update Available** has its own buttons (Later / Open Download Page) and shows the release notes as plain text in a scrolling box.
- The "latest version" answer was correct: the only release so far is a pre-release of the same version, and pre-releases are ignored.
- Verified: the same real key presses that crashed the native alert (Return / Esc via System Events) closed all six kinds of Tk dialog with the right answers and the app kept running; screenshots checked. 390 tests run, all pass except the one that needs the real broken files (3 new dialog tests).

## 261121.2215 — Check for Updates; moved to the AudioPostTools repository

- **Check for Updates.** The application menu (next to About and Quit) has **Check for Updates…** and **Check for Updates Automatically** (on by default). The automatic check runs quietly a few seconds after launch, at most once a day, and only speaks up when there is a new version. It reads the releases of `hsuanice/AudioPostTools` on GitHub, looks only at this app's tags (`wmfp-v<version>`), and ignores drafts and pre-releases. A new version shows what changed and opens its download page; the app never replaces itself. The request goes through macOS's `/usr/bin/curl` (macOS's own certificates, so it also works inside the .app) and sends nothing about the user. State: `update_check.json` in the app's Application Support folder. Shared code: `operations/update_check.py`, `UpdateChecker` in `ui_components.py`.
- **New home: the AudioPostTools repository** (`~/Developer/AudioPostTools`, published on GitHub). `wave_manager.py` was `SoX_Metadata_Workflow.py`; the changelogs moved to `Changelogs/`. Launch scripts now pick the first Python 3 that has tkinterdnd2 and openpyxl (or `AUDIO_POST_TOOLS_PYTHON`) instead of a fixed path on one Mac. The Soundminer "Send to" scripts are no longer maintained (removed); the Post Tools Inbox itself is unchanged.
- Release tool `.dev/release.py`: checks that the commit is on GitHub, runs the tests, builds the .app, zips it with `ditto`, writes this version's notes, and opens GitHub's new-release page filled in.
- Verified: the built Wave Manager .app ran its automatic check against GitHub (no release yet, so it stayed quiet and recorded the check); a driven dialog with a mocked newer release showed the notes and opened its page; the same version answered "is the latest version". Full suite passes (6 new update-check tests).

## 261121.2103 — QC: Poly ↔ Split audio compare about 4x faster; progress log every 5 seconds

hsuanice: Poly ↔ Split compare of 824 files showed "ETA 30m", and the Execution Log stayed silent for minutes between lines.

- **Faster integer vs float compare.** A 24-bit Poly stream against a 32-bit float render (an AAP render, for example) was checked one sample at a time. It is now checked a whole chunk at a time: the integer samples are widened to 32-bit by byte slicing, and both sides are decoded with Python's `array`. The rule is unchanged: a float sample, scaled back to the integer range and rounded, must equal the integer sample. Real file (4,176,000 frames): 2.45 s → 0.55 s. 8-bit and 64-bit float files keep the old per-sample check.
- **Progress log by time, not by count.** QC used to write one Execution Log line every 100 steps, which meant minutes of silence on slow steps. Now each stage logs its first and last step, and otherwise one line every 5 seconds (all QC stages: auto Compare, Folder QC, Poly ↔ Split). The status line still updates on every step.
- Verified on a feature film's dailies: Poly ↔ Split `iso` (312) → `iso_aap copy` (824) with audio compare: 803 PASS, 21 SKIP: NOT POLY, overall PASS, in 6 min (about 34 min before). 382 tests pass, 1 skipped (real broken-file samples not attached) (2 new: the chunked check gives the same answer as the per-sample rule for 16/24/32-bit, including values just off by under / over half a step; the log timing).

## 261121.2036 — QC and Poly ↔ Split: no formula; pairing by name prefix + recorder channel

Same pairing as Re-embed (261121.1652), now for QC too. The formulas missed names such as `_AAP.A1`, ` render 001` or a mono `_AAP.wav` unless they were set exactly right.

- **QC has no Base Formula fields any more** (Source, Target, and the hidden Poly ↔ Split BASE NAME FORMULA). One line under the inputs explains the pairing. The formula presets are still used by Split, Rename and Re-embed naming.
- **Auto-detect:** Poly ↔ Split when the Poly files' names start the other folder's mono names (followed by a separator or the end). If Poly is on the right, the folders are swapped, as before.
- **Folder QC:** the same relative path still pairs first. The files that are left pair when either name starts with the other's name, or with the other's name without its channel suffix (`.A3`, `_3` ...). This works **in both directions**: Source can be the original (`X.A1`) or the render (`X_AAP.A1`). The recorder channel decides. The longest shared name wins, so `Scene_3_Mix_AAP.A1` goes to `Scene_3_Mix`, not `Scene_3`. A file that still has two candidates is shown as **AMBIGUOUS** (yellow) and is never guessed. Recorder channels are read only for files that have a candidate.
- **Poly ↔ Split:** every WAV in the Split folder is read. A Split belongs to the Poly file whose name starts its name (longest, then a separator or the end), and its recorder channel picks the stream. `.A1`, `_A1`, `_AAP.A1` and ` render 001` all work. Splits that start no Poly name are EXTRA. A mono original in the Poly folder (SKIP: NOT POLY) now shows its own render or copy on its row instead of hiding it. With the same folder on both sides, a file is never its own Split.
- Verified on a feature film's dailies (`iso` 312, `iso_split` 824, `iso_aap copy` 824). Folder QC `iso_split` → `iso_aap copy` and the reverse: 824 pairs each. The previous version paired 803; the 21 mono takes were MISSING/EXTRA and are now paired. Poly ↔ Split `iso` → `iso_split` and `iso` → `iso_aap copy`: the same 803 stream pairs as the previous version, and the 21 mono takes are SKIP rows that show their file. In the app (copies of 3 takes), the Compare button detected both kinds and the swapped direction. 380 tests pass (9 new).

## 261121.1716 — Re-embed: a name without a channel number gets none after rename

hsuanice: the channel number (`$`) belongs only to split files from a Poly recording; a mono original such as `15_WILDT01.WAV` has none, so its renamed file should have none either.

- Rename after embed: a Source whose name has no channel number (a mono original like `12-10T2.WAV`, or a digits-only suffix that is not its recorder channel, like `SC12-3`) drops the `$` part of the Output name formula together with the separator and letters in front of it: `{base}_AAP.A$` -> `15_WILDT01_AAP.wav`. Split files and Poly streams keep `.A1`, `.A2` ... as before. Same in the preview refresh when the numbering mode changes.
- New `formula_without_number()` (operations/filename_formula.py): `{base}_AAP.A$` -> `{base}_AAP`, `{base}_A$` -> `{base}`, `{base}_Ch$_x` -> `{base}_x`.
- A second run ignores these unnumbered outputs (`15_WILDT01_AAP.wav`) as Targets too, like the numbered ones.
- Verified: real folders still 824 READY (21 mono takes now `..._AAP.wav`, no duplicate names); a real Preview + Execute on copies renamed `12-10T2 render 001.wav` to `12-10T2_AAP.wav` with the source timecode (3895008001), split files to `12-5T2_AAP.A1/A2.wav`; a second run treated all three as outputs. 242 tests pass (2 new).

## 261121.1652 — Re-embed: no Source / Target formula; mono takes are no longer skipped

In a feature film's dailies, 21 mono takes (for example `12-10T2.WAV`, rendered by AAP as `12-10T2 render 001.wav`) were never re-embedded or renamed. Their names have no `.A3` channel suffix, so neither the Source formula `{base}.A$` nor the Target formula `{base}.A$ render \d+` matched them. The Source side also skipped a mono file that didn't match without saying so. The renders kept timecode 00:00:00:00, and CLB later conformed them as silent takes.

- **No Source / Target formula any more.** Every WAV in Source and Target is read. A Target belongs to the Source whose name its own name starts with, followed by a separator (space `.` `_` `-`) or the end. The longest match wins, so `12-1T10 render 001` never falls into `12-1T1`. A mono Source also claims Targets that start with its name minus the channel suffix, for renders that renumber channels. Within that group the recorder channel (iXML CHANNEL_INDEX) still decides, as before; the metadata-identity fallback is unchanged.
- **{base} for renaming** is the Source name without its channel suffix. A lettered suffix (`.A3`, `_A3`, `-A3`) is always removed. A digits-only one (`.3`, `_3`, `-3`) is removed only when it equals the file's recorder channel, so `SC12-3` (take 3) keeps its digits. A mono take is named like a split channel: `12-10T2_AAP.A1.wav`.
- **Outputs of an earlier run are ignored as Targets.** A file already named the way the rename formula names outputs (`12-5T2_AAP.A1.wav`) only counts as a rename conflict.
- **Two Sources reaching one Target** are both marked AMBIGUOUS SOURCE instead of guessed.
- **Unpaired files are listed.** The Preview status line counts Targets without a Source and Sources without a Target; the Log lists them (up to 50 each). A mono Source with no Target is now a NO TARGET row instead of being left out.
- The Re-embed panel shows how pairing works instead of the two formula fields. QC and Poly/Split compare still used their formulas here (moved to the same pairing in 261121.2036).
- Verified on the real folders (`iso_split` → `iso_aap`, 824 files): 824 READY. The 803 jobs the previous version found are identical (pairing, Trk#, Stream#, new names); the 21 mono takes are new. A real Preview + Execute in the app, on copies of one mono and one split take, wrote the source timecode (12-10T2: 3895008001) and renamed to `12-10T2_AAP.A1.wav` / `12-5T2_AAP.A1.wav` / `A2.wav`, with the audio data unchanged. 240 characterization tests pass (5 new).

## 261003.1350 — Drag & drop from Soundminer: same rules as Send, and nothing left out silently

The user found it simplest to drag everything from Soundminer, with Reference Original on. A selection of 1,346 files imported only 1,345 with no visible reason. The missing file was a stale Soundminer record: the database still listed `…t2==PN_repaired.WAV`, which no longer exists, and lacked the real `…t2==PN.WAV`. Wave Manager had left it out, but said so only in the Log, and the next status line replaced that message.

- **Left-out files are visible:** a drop or send names every path that isn't an existing WAV file in the Log (up to 50), marking "(file not found)" where it applies. The final status line keeps the count, for example "Imported 1345 file(s) into the list. (1 of 1346 left out: not an existing WAV file — see Log)".
- **Dropping on the list follows the same rules as a Soundminer Send** (`_add_files_to_list`):
  - Combine / Merge mode now accepts drops as well as Split and Rename.
  - Files outside the current Source no longer stop with "must be inside the current Source folder". Wave Manager asks whether to replace the list (Source = the files' folder); with no list to lose, it just switches.
  - While a Preview or Execute is running, a drop is refused with a Busy message, as before.
- Verified under a real Tk main loop: a Combine drop that included one missing file showed T1 READY plus the left-out note. A drop from another folder asked first, and after Yes the list was replaced with Source switched. Full suite: 366 passing.

## 261003.1338 — Soundminer → Send to Split + Embed / Combine & Merge / Rename

Changed after the first real use. With Soundminer's **Reference Original** preference on, dragging a file from Soundminer hands over the original, so single files are simply dragged into the Audio Inspector or Wave Repair. Soundminer's own default drag hands over a copy it makes, which isn't the original file. The scripts are for **batches** (user's decision).

- **Removed** the "Wave Manager — Audio Inspector" and "Wave Repair — Diagnose" Soundminer scripts, and the probe script, along with the Inspector's receive path. Wave Repair still receives batches; no script sends to it now.
- **New Soundminer scripts:** "Wave Manager — Send to Split + Embed", "— Send to Combine & Merge" and "— Send to Rename". Each one switches to that mode and adds the files to the list, exactly like dropping them on it: imported in the background with progress, and **never executed**. To send everything currently listed, select all (⌘A) first.
  - **Source rule:** files inside the current Source join the list there. Otherwise, because every job's paths are worked out from one Source, a new list is needed, with Source set to the files' folder. When there is a list to lose (files outside Source, or a list from another mode), Wave Manager first asks "Replace the current list (N jobs) with these files?". Answering No keeps everything as it was (user's choice). With no list yet, it just switches.
  - While a Preview or Execute is running, the batch waits in the inbox and is retried.
- **Combine / Merge for a chosen set of files:** `build_combine_jobs(…, only_files=)`. Dropped and sent files can now be imported in Combine mode too. A group sent without all of its channel files, while the missing ones sit right beside it, is **BLOCKED** with a message like "only 2 of 3 channel files were sent — missing BBB.A2.wav". It is not combined into a poly short of tracks.
- Verified under a real Tk main loop with a temporary inbox: a Combine batch gave T1 READY and T2 blocked as partial. A Rename batch from another folder asked first: "No" kept the list, "Yes" replaced it and switched mode. A Split batch of one real Cantar poly gave 8 READY channel jobs. Wave Manager's settings file was unchanged. The question "send everything without selecting" is being checked with a read-only probe of Soundminer's Lua API.
- Tests: `test_only_files_combines_just_what_was_sent_and_blocks_partial_groups`. Full suite: 366 passing.

## 261003.1312 — Soundminer → Audio Inspector

Browsing now happens in Soundminer; the tools are called from it (user's request). Wave Manager's own browser stays.

- **"Wave Manager — Audio Inspector"** (Soundminer script): opens the selected files in the Audio Inspector. One file is shown on its own (left). Two are shown side by side for comparison. With three or more, the first two are shown and the status line says how many were not (user's choice). The current list and work in Wave Manager are not touched.
- **Post Tools Inbox** (`operations/post_tools_inbox.py`, shared): `~/Library/Application Support/Post Tools Inbox/`, a folder that belongs to no single app, so later tools can share it. A sender writes a plain-text *batch* there, with `#app=`, `#action=` and one file path per line. A running app checks the folder about once a second; that is one directory listing, and nothing is read until a batch appears. The app takes only the batches addressed to it. Because Soundminer gives no "last record" call, a batch counts as complete once it has stopped growing for 1.5 s. Handled batches move to `Received/` (the last 50 are kept). Batches nobody handled within 10 minutes are moved there unopened, marked "(expired)". A file named `Paused` in the inbox stops all receiving. While an app is busy (Wave Repair writing a repair), a batch stays in the inbox and is retried. `python3 -m operations.post_tools_inbox launch <app>` starts an app unless it is already running. "Running" is checked by the process ID and command line the app registered, so two copies are never started, even while one is still opening.
- **Soundminer scripts** (in the Soundminer Support folder's `Scripts/`): "Wave Repair — Diagnose" and "Wave Manager — Audio Inspector", plus the shared helper `_hsuanice_post_tools.lua`. Soundminer runs the script once per selected record. Each record's FilePath is added to one batch, and the app is launched once per batch, from source for now; it will switch to the .app once stable. Every record returns nil, so nothing in Soundminer or the files is changed. Rescanning metadata after a tool has changed a file is left to Soundminer's own "file changed — rescan?" prompt (user's choice). A read-only probe run in Soundminer 6.2 first confirmed Lua 5.1 with `io.popen`/`io.open` available, one call per record, and FilePath plus metadata fields in each record.
- Tests: `tests/test_post_tools_inbox.py` (11). Among them, the real Lua sender script is run with a local `lua` and its batch is read back by Python, including a path with an apostrophe. A GUI drive of both apps under a real Tk main loop with a temporary inbox gave the results described above. Full suite: 365 passing.

## 261002.1912

- Shared structure inspection (Audio Inspector's STRUCTURE section, and the Split structure block): stray bytes appended after a file's last chunk are now reported as "stray byte(s) after the last chunk" (warning) instead of a broken chunk structure (error), when fmt and data are complete and nothing chunk-like follows. Such files are still blocked in Split. See CHANGELOG_Wave_Repair.md 261002.1912.
- Full suite: 342 passing.

## 261002.1602

- **Open in / Send Blocked to Wave Repair now tells Wave Repair which Source folder the files came from.** Before launching, it writes a small hand-off note (`~/Library/Application Support/Wave Repair for Post/handoff.json`: the files, the Source folder, the time). Wave Repair then keeps its backups beside the Source folder instead of guessing, so they never land inside it, where Split/QC would scan them as more takes. See CHANGELOG_Wave_Repair.md 261002.1602.
- Full suite: 337 passing.

## 261002.1319

- `ExecutionLogWindow` moved into `ui_components.py` (now `ExecutionLogWindow(root, status_var)`) so Wave Repair for Post uses the exact same Execution Log window, keeping the two tools visually one family. No visible change in Wave Manager.
- Full suite: 318 passing.

## 261002.1233

- **Rescan Blocked (and dropping files onto the list) now run in the background with progress.** Both used to run synchronously on the main thread: the window froze while soxi and BWF MetaEdit ran on every file, and nothing showed progress. They now work exactly like Preview:
  - the status line updates as each file is read (`Rescan 3/12 | elapsed | ETA`);
  - the Log shows a `[rescan n/total] <file> | jobs=… | <status>` line per file;
  - Pause and Cancel work;
  - the results are merged back into the list when done.
  - Every Tk variable is read on the main thread before the worker starts; the worker only receives plain values.
- `configure_treeview_column_widths()` moved into `ui_components.py` so Wave Repair's table can use the same fix for column widths snapping back. No change in Wave Manager.
- The shared theme's progress-bar fill is now visible: `clam` drew it in the window background color.
- Full suite: 318 passing.

## 261002.1210

- **Split now blocks structurally damaged sources** before soxi or BWF MetaEdit run. Both tools happily read many damaged files: the 92 real Cantar files with a missing final pad byte, a never-finalized data size, and so on. Their success therefore says nothing about whether a file is sound. Any structural **warning or error** from `riff_inspect` gives "BLOCKED: structure — <the problem>; send to Wave Repair for Post". Info-level notes never block. The check reads only headers, about 3 ms per file.
  - Why the 92 pad-byte files block too, at the user's request: BWF MetaEdit refuses to *write* metadata into them (even with `--continue-errors`), and refuses to read their iXML without that flag. Measured on a real one (UB5800): sox, ffmpeg, CoreAudio, mediainfo and Python all decode bit-identical audio either way.
- **New "Damaged files:" row (Split mode only)**:
  - **Send Blocked to Wave Repair** hands every structure-blocked source to Wave Repair in one go.
  - **Rescan Blocked**, after repairing, re-previews **only those files** and merges their fresh jobs back into the current list instead of rescanning the whole folder.
  - The row is separate because the main action row is already as wide as the default window; added to it, the new buttons were pushed out of view.
- **Fixed: dropping WAV files onto the preview list never worked.** `_import_dropped_files()` used a `mode` variable it never defined, so every drop ended in "Unable to import dropped file(s): name 'mode' is not defined". Found because Rescan Blocked reuses that function.
- **Fixed: re-importing a Split source kept only one of its channels.** The old in-place refresh mapped each source to a single list slot, so a 12-channel source's 12 new jobs overwrote one another and only the last survived. New `merge_refreshed_jobs()` replaces all of a source's jobs together, at the position of its first old job.
- Tests: the existing Split job-builder tests (which use empty placeholder files with mocked tools) now patch the structural pre-check out. New `StructureBlockCharacterizationTests` cover blocking before any tool runs, sound files passing, and the merge. Full suite: 315 passing. Verified end-to-end under a real Tk main loop on copies of real project files (UB4730 healthy, UB4731 cut-off iXML, UB4840/UB4885 odd iXML):
  - Preview blocked exactly the 3 damaged files.
  - Send Blocked handed over those 3.
  - Wave Repair repaired 2 and skipped UB4731.
  - Rescan Blocked brought the 2 back as 12 READY jobs each, with UB4731 still blocked.

## 261002.1122

- **Theme setup moved into `ui_components.py`** (`apply_app_theme()` + shared color constants) so Wave Repair for Post can use the exact same look; Wave Manager now calls it instead of configuring `clam` inline, and the Audio Inspector's own panel/text colors come from the same constants. No visual change here.
- The Audio Inspector's STRUCTURE section picks up Wave Repair's diagnostic improvements from real damaged Cantar files (a cut-off final iXML is now named as such and marked repairable, and a cut-off chunk directly after `data` is no longer mistaken for audio running past its declared size) — see `CHANGELOG_Wave_Repair.md` 261002.1122.
- Full suite: 297 passing, including one real-file test that runs only when its drive is mounted.

## 261001.1859

- **Wave Repair for Post integration (Phase 1)**. Wave Repair is a new, separate app in this same folder that shares the `operations/` engine (see `CHANGELOG_Wave_Repair.md`). Wave Manager still never repairs anything itself, but it now shows structural damage and can hand files over.
- **Audio Inspector: new STRUCTURE section**, right after FILE, from `operations/riff_inspect.py`. It's read straight from the bytes rather than through soxi or BWF MetaEdit. A sound file shows one line ("Status: OK — RIFF, chunks: fmt, bext, iXML, data"). A damaged one shows the full structural report and points to Wave Repair. The Inspector also no longer fails outright when soxi or BWF MetaEdit can't read a file: it falls back to FILE + STRUCTURE + "AUDIO: Unable to read: …" instead of a single "Unable to inspect" line, since a damaged file is exactly when the structure matters.
- **BLOCKED statuses now name structural damage** when there is any. Where a file-read failure blocks a row (Split's channel and iXML reads, Re-embed's source Trk# and Poly Track List reads, Combine's source-format check, and QC's Poly layout, Poly mapping and Split channel reads), the status gains a suffix like " — structural damage (data size is 0 — the header was never finalized …); open in Wave Repair for Post". The suffix only appears when the inspector finds a real error, so blocks with other causes read exactly as before. The `BLOCKED` prefix is unchanged, so every existing `startswith("BLOCKED")` check is unaffected.
- **Right-click → "Open in Wave Repair for Post"** on Browse's file list, the Preview job table (source and target), and all three QC tables (added to their existing Reveal menus). From source, this runs the sibling `WAV_Repair.py` with the same Python. Packaged, it runs `open -a`, preferring a `Wave Repair for Post.app` sitting next to this app and otherwise finding it by name. That goes through as an Apple Event, so an already-running Wave Repair receives the file. The logic is `operations/repair_handoff.py`.
- **Build script fix**: `Build Wave Manager for Post.command` used to `rm -rf build dist`, which would also delete Wave Repair's build now that the two share those folders. It now removes only its own `build/`/`dist/` outputs and passes `--noconfirm`.
- **Found while verifying, not changed:** soxi and BWF MetaEdit happily read two common kinds of damage — a power-loss file whose data size was never finalized, and a file whose bext chunk ID is corrupt but whose size is intact. Split Preview therefore shows those files as plain **READY**, not BLOCKED. Whether Preview should warn about them is left as an open design question rather than changed silently.
- Tests: added a characterization test confirming the Inspector still returns FILE/STRUCTURE/AUDIO with the damage named when both tools are unavailable, plus the new `tests/test_riff_inspect.py` and `tests/test_wave_repair.py` suites. Full suite: 294 passing. Verified with a real `WorkflowApp`: the STRUCTURE section renders for healthy, power-loss and corrupt-header files with real soxi and BWF MetaEdit, and the context-menu hand-off really launched Wave Repair with the selected file.

## 260917.0015

- Split no longer blocks a file just because its metadata is incomplete or absent — found via two real, common cases in a real production sound folder: a mono wireless lav/boom file sitting alongside a Poly recorder's takes (no iXML at all — "BLOCKED: iXML sidecar was not exported"), and one with iXML but no CHANNEL_INDEX for its track ("BLOCKED: missing Trk#"). Both used to block the file outright; both are now handled:
- **New "Copy non-Poly (mono) source files" toggle** (Split panel, default **ON**) — mirrors Combine's own "Copy non-combined/non-merged files": a source with exactly one channel has nothing to split, so it's copied straight through instead (new `split_copy` job mode, `shutil.copy2` — no sox/bwfmetaedit involved at all), keeping the original filename and whatever metadata the source already has, complete, partial, or none. Off reverts to sending mono sources through the ordinary split pipeline, one real channel of one.
- **A missing Trk# no longer blocks a channel at all** (with the toggle off, or for a genuinely Poly source) — it falls back to that channel's own file-order position instead, the same convention "Ch#" naming already uses. A source with no iXML whatsoever falls back the same way for every one of its channels, rather than the whole file being blocked before any of them are even considered.
- One boundary kept firm: only the exact "no iXML at all" case gets this treatment. A genuinely corrupt/truncated WAV (bwfmetaedit reporting something like `invalid Wave: truncated (...)`) still blocks exactly as before — repairing broken WAV files is explicitly future work, not something this fallback should paper over.
- **Merged Split and Re-embed's two separate "extract one channel's iXML as its own mono document" functions into one** (`extract_mono_ixml()` in `operations/ixml_core.py`, replacing `mono_ixml_from_poly_slot()` and `normalize_mono_ixml()`), after noticing they were structurally identical aside from strictness — evaluated merging them fully lenient everywhere, but Re-embed's Poly-source case (and QC's Poly→Split metadata fix) keep `strict=True`: those two are confirming a specific already-existing target file really is the channel they matched it to by filename before writing that channel's real metadata into it, where a missing Trk# means the pairing itself is unconfirmed — silently guessing there risks writing one channel's metadata onto the wrong file with no warning, unlike Split, which is naming and tagging a channel entirely by its own file-order position with nothing to confirm.
- Added 6 new tests for `build_split_jobs()`'s new fallback/copy behavior and 5 for `extract_mono_ixml()`'s lenient vs. strict modes; updated 3 existing `_execute_worker` tests for the merged function and the new `split_copy` dispatch. Full suite now at 231 passing tests. Verified end-to-end against the real files from the report: both the wireless lav file (no iXML) and the Leo lav file (iXML, no Trk#) now produce a clean `OK` through the real `bwfmetaedit`/`sox` pipeline in both toggle states — copy-through (byte-identical, confirmed via file size) and real split+embed (confirmed mono output with correct channel-1 fallback numbering) — and a real synthetically-truncated WAV was confirmed to still `BLOCKED` even with the copy toggle on.

## 260916.1959

- **Renamed the app: "SoX Metadata Workflow" → "Wave Manager for Post"** — settled on after going through several rounds ("Wave Editor for Post" risked implying actual waveform/audio editing, which this app has never done; "Wave Surfer" collides with an existing, unrelated open-source audio tool of that exact name; "Wave Catcher" was a close second). `Manager` was picked specifically because it's accurate: this app has always been about managing WAV files and their metadata — splitting, renaming, re-embedding, combining, QC-comparing, browsing — never about editing audio content itself.
- Updated everywhere the old name was hardcoded: the `APP_NAME` constant (which the window title, every dialog, and the Terminal tool-installer script all already derive from), the missing-Python-package startup error dialog (shown before `APP_NAME` is even defined, so it has its own literal copy), the PyInstaller `.spec` file's three `name=` fields (`EXE`/`COLLECT`/`BUNDLE`, plus the `.app` bundle name itself), and `operations/__init__.py`'s module docstring. Renamed the files themselves to match: `SoX Metadata Workflow.spec` → `Wave Manager for Post.spec`, `Build SoX Metadata Workflow.command` → `Build Wave Manager for Post.command` (its echoed messages and the `.spec`/`.app` names it references updated too), `Launch SoX Metadata Workflow.command` → `Launch Wave Manager for Post.command` (no text changes needed there — it only ever pointed at the `.py` file directly by path, never referenced the name as a string). The project folder itself (`_SoX Metadata Workflow/`) and the main script's filename (`SoX_Metadata_Workflow.py`) were deliberately left alone — renaming those would ripple through this whole codebase's own file paths and every existing reference to it, well beyond what a display-name change calls for; ask explicitly if those should change too.
- `APP_SETTINGS_PATH` moved from `~/Library/Application Support/SoX Metadata Workflow/settings.json` to `.../Wave Manager for Post/settings.json` to match — with a one-time migration fallback (`_LEGACY_APP_SETTINGS_PATH`) so this doesn't look like a settings reset for existing installs (confirmed this matters: the real settings file on this machine already has real saved formula presets and window geometry from this session's own work). `load_app_settings()` now reads the legacy path only when the new path has no valid data of its own yet — once anything is saved to the new path (the very next quit), it takes over and the legacy file is simply left alone, never touched or deleted.
- Added 2 new tests for the legacy-settings fallback (falls back to the old path when the new one is empty/missing; prefers the new path once it actually has data, ignoring a stale legacy file) — full suite now at 220 passing tests. Verified end-to-end with a real `WorkflowApp` instance and a real settings file: window title reads "Wave Manager for Post `<version>`"; a simulated existing install's legacy formula presets and window geometry were correctly picked up on first launch under the new name; and closing the app correctly wrote them back out to the new settings path, completing the migration. Also confirmed via a real app launch that nothing broke.
- The old `dist/SoX Metadata Workflow.app` build from before the rename is still sitting in `dist/` — harmless, and the Build script already `rm -rf`s `build`/`dist` on every run, so the next build replaces it with the correctly-named one automatically; not deleted here since that's a generated build artifact, not something this change needed to touch.

## 260915.2359

- Browse mode's two player checkboxes — **Auto-play on select** and **Solo X-OR** — are now remembered across restarts, the same way window geometry and the QC formula presets already are: saved to the existing `settings.json` on quit (`_on_close()`), restored at startup. No UI change; both still default to off for a brand-new settings file.
- No new regression tests (Tk `BooleanVar` init from a loaded dict plus an existing, already-tested save/load path — not new pure logic). Verified directly with a real settings file in a temp directory: a fresh launch defaults both to off; turning Solo X-OR on and closing the app persists `"browse_solo_exclusive": true` into `settings.json` alongside the existing keys; a second launch against that same file correctly restores Solo X-OR on and Auto-play still off. Full suite still at 218 passing tests.

## 260915.1650

- Added a **Solo X-OR** toggle next to "Auto-play on select" in Browse mode — named after Pro Tools' own "Solo Mode: X-OR" preference, which this matches: off (default) keeps the existing additive behavior (any number of channels can be soloed together), on makes soloing a channel automatically un-solo every other one, so only one channel is ever soloed at a time.
- Implementation: each Solo button's `command` now passes its own channel index through to `_toggle_browse_solo_channel(channel_index)` (previously took no argument, since nothing needed to know which button fired). When X-OR is on and the click just turned a channel's solo *on*, every other channel's `BooleanVar` is cleared before the usual playback-restart/redraw logic runs — clicking an already-soloed channel off is unaffected, since only turning one on triggers the drop.
- No new regression tests (Tk `BooleanVar`/Checkbutton wiring, not pure logic). Verified directly against a real 12-channel file: additive mode confirmed to keep multiple channels soloed together as before; switching X-OR on and soloing a new channel confirmed to drop the previous one automatically (checked both via `_browse_solo_channel_indices()` and the real sox `remix` argument on a live process, which correctly switched from `remix 3` to `remix 8` rather than becoming `remix 3,8`); un-soloing the sole active channel in X-OR mode confirmed to just clear it with no side effects; and switching back to additive mode confirmed multi-solo accumulates again. Full suite still at 218 passing tests.

## 260915.1641

- Fixed the reported "playhead jitters during playback, feels laggy" in Browse mode — a real, distinct issue from the per-click selection lag fixed in the previous entry, not the same bug. Root cause: the tick loop (running at 20Hz while playing) called the *full* waveform redraw every single time just to move the playhead — for a Poly file that meant deleting and recreating all ~7200 bar/label/highlight canvas items 20 times a second purely to shift one line a few pixels.
- The playhead is now a single persistent canvas item, moved via `coords()` on every tick instead of being deleted and recreated — a full redraw (`_draw_browse_waveform()`, still doing everything as before) is now reserved for when the waveform content itself actually changes (a new file loading, Solo being toggled, the canvas resizing), never for the tick loop, which calls a new, minimal `_update_browse_playhead()` instead. Measured 20 consecutive playhead-only updates at a combined 0.0002s, versus the old per-tick cost of repeatedly rebuilding thousands of items.
- Fixed a related smaller issue found while verifying this: resizing the waveform canvas mid-playback (e.g. dragging the pane's sash) used to blank the playhead until the next regular tick redrew it, because the resize handler called the full redraw with no explicit position, and clearing the position is exactly what deletes the playhead item. It now passes the real current elapsed position through instead.
- No new regression tests (Tk canvas item lifecycle, not pure logic). Verified directly against a real 12-channel file and a real sox subprocess: the playhead's canvas item id stays identical across repeated ticks (confirming it's moved, not recreated) while the "wave"-tagged item count stays exactly constant throughout playback; the playhead's x-coordinate was confirmed to keep advancing normally; Solo toggling was confirmed to still force a real full content redraw (the green highlight still appears) without disturbing the playhead item; and stopping playback was confirmed to still clear the playhead as before. Full suite still at 218 passing tests.

## 260915.1612

- Widened Browse mode's WAV list Path column and made it auto-size to content: was a fixed 220px regardless of what's actually in it, now floors at 260px and grows to fit the longest path value shown for the current folder (clamped at 640px so one absurdly deep subfolder chain can't blow out the whole pane). Recomputed every time the list is populated, since a recursive "list all files in subfolders" listing's path values can be dramatically longer or shorter than a plain folder's (always blank).
- Reduced remaining per-click lag in Browse mode after the previous entry's threading fix cut it from ~0.4s to ~0.2s — three further real causes found and fixed:
- `_load_browse_player()` was still calling `read_wav_pcm_layout()` **twice** per click (once via `wav_duration_seconds()`, once for channel count), each walking the file's *entire* chunk list including seeking past its (often hundreds-of-MB) data payload to enumerate chunks stored after it. New `read_wav_pcm_layout_fast()` in `operations/audio_qc.py` stops as soon as both `fmt ` and `data` are found — `fmt ` always precedes `data` in a valid WAV, so the chunks that matter here never require seeking past the payload at all.
- The bigger find: even after backgrounding the peaks/label computation, **starting that background thread immediately still stalled the main thread's own very next lines of code** — a real, measured CPython GIL effect, not a bug in the threading itself. A background thread's CPU-bound work (the struct-unpacking in `compute_wav_channel_peaks`) can still contend for the GIL with the main thread's immediately-following Python bytecode, delaying the exact UI updates threading was meant to keep instant. Fixed by moving the thread-start to `self.root.after_idle(...)`, deferred until after the placeholder UI has already been pushed out for this event-loop pass — cut the measured per-click cost from ~0.2s to ~0.04s.
- The mono-file path had the same un-backgrounded peaks read the poly path already had fixed — now uses the same background-thread-plus-placeholder pattern (new `_load_browse_mono_peaks_worker`/`_apply_browse_mono_peaks`, via the same `events` queue and request-id guard as the poly path), rather than only fixing the reported case and leaving the symmetric one slow.
- **Found and reverted a second real hang while verifying the color-highlight fix from the previous entry**: the Solo buttons' fuller styling (`bg`, `fg`, `activebackground`, `relief=RAISED`, `bd=1`, `padx`, `highlightthickness=0`, alongside `selectcolor`) reproduced the *exact same* "next Tk event-loop pass blocks for ~the remaining playback duration" hang as the earlier forced-canvas-resize bug — confirmed deterministic (6/6 and 3/6 repeated real-file runs both hung every time, in both a clean process environment and one with leftover test-run sox processes still around, ruling that out as a factor). Every individual style option and several pairs tested safe in isolation; only the full combination reproduced it, most likely from costlier native widget compositing on this Tk/macOS combination. `selectcolor` alone — the one option actually needed for the on/off color swap — was confirmed stable across 6 repeated real-file runs and is now the only override kept.
- Also cleaned up a real mess made across this whole session's Browse-mode testing: dozens of orphaned, `SIGSTOP`'d `sox` processes had accumulated in the background from various verification runs across many earlier entries whose driver scripts exited without resuming-then-killing a paused player. Not an application bug (the shipped `_browse_player_kill_process()` already resumes-then-terminates correctly) — purely test-script hygiene, killed manually; noted here since it was a real, if separate, thing found and fixed today.
- No new regression tests (Tk widget styling/GIL-timing/file-I/O-path behavior, not pure logic) beyond what the previous two entries already added. Verified directly against a real 12-channel file and real sox subprocesses: per-click synchronous cost measured at ~0.04s after all three fixes (down from ~0.2-0.27s); the Path-column auto-sizing confirmed against both a shallow real folder (stays at the 260px floor) and synthetic long/absurd paths (widens correctly, clamps correctly); and the Solo-button hang fix re-confirmed with 6 consecutive clean real-file play attempts after reverting to `selectcolor`-only, versus a reproducible hang on every attempt with the fuller styling. Full suite still at 218 passing tests.

## 260915.1537

- Fixed four issues reported after the Solo entry shipped, all in Browse mode's player:
- **Selecting a file no longer auto-opens the Audio Inspector.** This was deliberate Finder/Quick-Look-style behavior from an earlier entry, but browsing through files to just listen shouldn't also pop a window open every time. If the Inspector is already open, it still keeps refreshing to track the current selection.
- **Solo buttons now visibly change color when engaged**, and **soloed lanes are highlighted directly on the waveform** (bright green waveform + a subtle green lane background) while every non-soloed lane dims to grey — the previous ttk "Toolbutton" style gave almost no visible feedback for which channel(s) were actually engaged, which was the reported problem. Switched the buttons themselves from ttk to plain `tk.Checkbutton(indicatoron=False, selectcolor=...)`, whose on/off color swap is automatic and tied directly to the variable.
- **Reduced the "clicking through files feels laggy" delay.** Traced to `_load_browse_player()` computing a Poly file's full 12-channel waveform peaks and reading its iXML track names synchronously on the main thread before anything could render — measured at ~0.4s per file for a real 12-channel field-recorder file (~0.2s of that alone in the peaks read). Both now run in a background thread — placeholder "ChN" labels and empty lanes show immediately, replaced once the real data arrives — cutting the synchronous part of a click down to under 0.2s. A per-load request id discards a background result for a file the user has since clicked away from, so rapid re-selection can't let a slower, older file's data clobber a newer selection.
- **Found and fixed a real thread-safety bug while building the fix above**: the background worker's first attempt used `self.root.after(0, ...)` to hand its result back to the main thread, which raised `RuntimeError: main thread is not in main loop` — Tk's `after()`/`after_idle()` aren't safe to call from a non-main thread. Fixed by routing through this app's own pre-existing worker-thread pattern instead (the `self.events` `Queue` already used by every other background operation's progress/`*_done` events, drained on the main thread every 100ms via `_drain_events()`) rather than inventing a second cross-thread mechanism — a new `"browse_poly_details"` event kind.
- No new regression tests (Tk widget styling, canvas color state, and thread-safety are not pure logic). Verified directly against a real 12-channel file: confirmed the Inspector stays `None`/unopened after a real selection; confirmed the Solo button's `selectcolor` and the actual canvas item fill colors change correctly on solo/un-solo (green + dimmed grey while soloed, plain blue once cleared); confirmed placeholder "ChN" labels and empty lanes appear immediately on selection with real peaks/names arriving moments later without blocking; and confirmed a rapid double-selection (before the first file's background load finished) ends up with the *second* file's real data, not a stale overwrite from the first. Full suite still at 218 passing tests.

## 260915.1525

- Added per-channel **Solo** toggles for Poly sources in Browse mode — multiple channels can be soloed together (standard mixer behavior: they mix down and play together, everything else stays silent), each a small button embedded directly on the waveform canvas, right-aligned in its own lane so it never overlaps that lane's track-name label regardless of how long the name is. Un-soloing every channel returns to the normal full mix.
- Soloing is implemented as a sox `remix <a>,<b>,...` effect (1-based input channel numbers, mixed to one output channel) inserted into the same playback command used everywhere else in the player; toggling Solo while playing restarts from the current position with the new remix applied, exactly like a live volume change already does — sox has no way to change either on a process that's already running.
- Each channel's lane now needs a real, usable row height instead of the previous "compress everything into a fixed total height" — a Solo button needs room to actually be clicked. The per-channel floor is now ~30px (a Toolbutton's own natural size) instead of the previous 20px, so a high channel count asks the pane for more height than before rather than staying technically visible but unclickable.
- **Found and fixed a serious real bug while verifying this against a real file**: forcing a canvas-embedded window item (via `create_window(..., width=, height=)` or a later `itemconfigure(..., width=, height=)`) to clip smaller than its own natural size, while a sox CoreAudio stream is open, blocks the very next Tk event-loop pass for roughly the remaining playback duration — confirmed directly: a 2-second test clip produced a ~2.6s hang, the full 42-second file produced a ~42.7s hang, and disabling only the forced-size call (keeping everything else, including the exact same Solo buttons and an active sox process) made the hang disappear entirely. Root cause narrowed down through elimination (widget type, plain vs. interactive, create-time vs. later resize) to specifically "explicit width/height smaller than natural size" — not sox, not the remix effect, not the embedded widgets themselves. Fixed by never resizing these canvas windows: they render at their natural size, positioned (not resized) via `coords()` only.
- No new regression tests for the canvas-embedding/hang fix itself (Tk geometry-manager and native-event-loop behavior, not pure logic) or for the Solo buttons' own Tk wiring — both verified directly against a real 12-channel field recorder file and real sox subprocesses: soloing channel 1 alone produces a `remix 1` command and a genuinely different sox PID; soloing channels 1 and 11 together produces `remix 1,11`; un-soloing both returns to a plain command with no `remix` at all; and — the specific case that would have shipped the hang — starting playback, toggling Solo, and pumping the real Tk event loop afterward stays fast (tens of milliseconds per cycle) with the process alive throughout, instead of the multi-second block observed before the fix. Full suite still at 218 passing tests (no pure-logic surface added this entry).
- Also fixed (reported directly): the Solo button was drawn at each lane's left edge, overlapping the track-name label already there — moved it to the right edge of the lane instead, with the label kept on the left where there's actually room for a full channel name.

## 260915.1447

- Investigated a reported "digital crackle / distorted playback" and traced it to the test machine's own audio hardware, not the app: `sox`'s coreaudio driver ignores whatever output channel count is actually requested (confirmed directly — `-c 2`, an explicit `channels 2` effect, and `remix -` all still get silently overridden) and always reopens the current macOS default device at its own full native channel count. On this machine that device is a 10-output professional interface (Universal Audio Thunderbolt), so a 12-channel Poly file was getting spread across 10 physical outputs never intended to receive it — outside anything this app's sox invocation can control. No code change from this; noted here since it was a real investigation, not a shrug.
- Added per-channel waveform lanes for genuine Poly (multi-channel) sources in Browse mode: selecting a file with more than one channel now draws one thin waveform lane per channel (stacked to fill the same fixed canvas height a mono file's single line already used, per explicit request — many-channel files get thinner lanes rather than a taller or scrollable canvas), instead of one line combining every channel together. A plain mono/stereo-interleaved-as-mono file is unaffected — it keeps exactly the single-line waveform from the previous entry.
- Each lane is labeled with that channel's iXML `TRACK_LIST` name when the file's own metadata has one (reusing `ixml_track_rows()`, the same lenient reader the Inspector's Poly Track List already relies on), falling back to a plain "ChN" for any channel the metadata doesn't name — verified against a real 12-channel field recorder file, correctly pulling real names ("CMIT 5U", "WU", "MIX L", "MIX R", etc.) for the channels that had them and falling back cleanly for the rest.
- New `compute_wav_channel_peaks()` in `operations/audio_qc.py` is the per-channel counterpart to the existing `compute_wav_peaks()` — same sparse, fixed-I/O sampling strategy (one small window per bucket, evenly spaced across the file), but keeps each interleaved channel's amplitude separate instead of collapsing them into one combined value. Playback itself is unchanged — there's still one shared playhead and one sox process; this is a visual change only, not a per-channel mixer.
- Added a **Reset** button next to the volume slider, setting it straight back to 0dB — a follow-up request after shipping the -inf..+24dB control, since there was previously no quick way back to unity gain besides dragging by eye.
- Added 3 new pure-function tests for `compute_wav_channel_peaks()` (channel separation with distinct per-segment left/right amplitudes, a mono file's single lane matching the existing combined preview exactly, and an empty-data-chunk edge case returning one empty lane per channel) — full suite now at 218 passing tests. Verified everything else directly against a real 12-channel file and a real Tk event loop: 12 lanes drawn with real per-channel peak data and real iXML-derived labels, a synthetic mono file confirmed to still take the old single-line path unchanged, and the volume Reset button confirmed to return both the `DoubleVar` and its live dB label to exactly 0/+0.0dB after being moved.

## 260915.1357

- Added the two features requested right after the Media Explorer rework shipped: click-to-seek on the waveform ("playhead can be set by mouse click, playing from there"), and a volume control spanning -inf to +24dB, defaulting to 0dB. Also added a Space-bar shortcut for play/pause, requested as a quick follow-up.
- Click-to-seek: `_on_browse_waveform_click()`, bound to `<Button-1>` on the waveform canvas, maps the click's x-position to a fraction of the file's duration and starts playback from there.
- Volume: a `ttk.Scale` from -60 to +24 dB (`BROWSE_VOLUME_MIN_DB`/`BROWSE_VOLUME_MAX_DB`) sits next to a live dB readout. -60 is a stand-in floor for "-inf" since sox has no literal -inf gain — at or below it, `browse_volume_sox_args()` emits an exact `vol 0 amplitude` mute instead of a very-quiet-but-nonzero dB value. Sox can't change a running process's gain, so a volume change while playing restarts the sox subprocess from the current elapsed position with the new gain — debounced 150ms so dragging the slider doesn't spawn a process per pixel of motion.
- Both features route through a new shared core, `_browse_player_start_from(start_seconds)`, which kills whatever's currently playing and spawns a fresh `sox <file> -d trim <start> vol <gain> dB` (or `vol 0 amplitude` at the floor) from that offset — used by the ordinary Play button (start=0), click-to-seek, and the live-volume restart alike, replacing the old Play-only inline spawn logic.
- Extracted the pure logic behind the dB label and the sox `vol` arguments into module-level functions, `browse_volume_label(db)` and `browse_volume_sox_args(db)`, so they're unit-testable without a running Tk instance; the corresponding instance methods now just delegate to them.
- Space shortcut: added a new `browse_play_pause` keybinding action (default `space`, no modifier, matching Browse mode's existing bare `Return` convention since these widgets never hold text input) bound to both `browse_file_list` and `browse_folder_tree`. This required generalizing the keybinding system's `scope` field to accept either a single widget-attribute name or a tuple of several — `_apply_keybinding()` now binds/unbinds across every widget named in the scope instead of assuming exactly one.
- Verified against a real file and a real sox subprocess: click-to-seek starts playback within 0.5s of the expected offset for a 60% click position on a 42-second file; changing the volume slider while playing kills the old process and starts a genuinely different PID with the new gain, confirmed via `ps`; Space toggles play/pause (confirmed via real `ps -o stat` showing `T`↔`S`) on both the file list and the folder tree, including back-to-back toggles. One synthetic-test-only flake was traced and ruled out: `.focus_set()` immediately before `event_generate()` sometimes lost the race against Tk's async focus delivery (the same class of issue previously fixed for the Audio Inspector) — using `.focus_force()` with a longer settle delay made the test reliable across 6+ repeated runs; this is a test-harness timing artifact; the shortcut itself fires correctly once focus has actually landed, exactly as it would from a real click.
- Added 10 new pure-function tests for `browse_volume_label()`/`browse_volume_sox_args()` (zero/positive/negative gain, the -inf floor and below it, and the range constants) — full suite now at 215 passing tests.

## 260915.1332

- Reworked Browse mode into a REAPER Media Explorer-style browse-and-audition view, replacing the accumulating "collected list" from the last several entries entirely — the user decided pure browse-and-play was the actual goal, not a saved shortlist. Removed along with it: the Tag keyboard shortcut, drag-from-the-file-list-to-the-collected-list, "Add Selected to List", and the column-resize/drag-source conflict workaround from the previous entry (its root cause — registering the file list as a DND drag source — no longer exists, so the workaround has nothing left to guard).
- Layout is now tree (left) | WAV file list over a play-and-waveform preview (right, top/bottom split) — three real `PanedWindow` sashes total. `table_frame` (the ordinary Split/Re-embed/Rename/Combine list) no longer shares a `PanedWindow` with Browse's own frame at all; that whole arrangement existed only to give the old collected list a resizable divider next to the explorer, which is now moot, so `table_frame` went back to being packed directly like every other mode.
- The player itself: selecting a file loads its waveform (see below) and duration and enables Play; **Play/Pause/Stop**, a **duration-scaled waveform** (drawn from real decoded peaks, not a placeholder), a live elapsed/total time readout, and an **"Auto-play on select" toggle** (off by default) so flipping through files with the arrow keys can optionally start playing each one immediately. Deliberately no pitch/rate/volume/loop/scrubbing — the user asked for "just waveform and play," not Media Explorer's full transport; Inspector already covers detailed metadata.
- Playback runs as a plain `sox <file> -d trim 0` subprocess — sox is already a hard dependency of this app (no new tool path, no new external dependency), and the user explicitly chose it over `afplay` once decided. Pause/Resume are real process control (`SIGSTOP`/`SIGCONT`), not a restart-from-a-remembered-offset simulation — confirmed directly that a stopped sox process's `ps` state actually shows `T` (stopped) and returns to `S` (running) on resume. Switching away from Browse mode, or quitting the app, stops playback and fully reaps the child process (a bug caught while verifying this: an initial version left a zombie behind after `terminate()`, since nothing ever called `wait()` on it — fixed by waiting up to 1s, with `kill()` as a last resort).
- Waveform generation (`compute_wav_peaks()`, new in `operations/audio_qc.py`, alongside a small `wav_duration_seconds()` helper) is a fast, approximate preview: one small 4KB window sampled per bucket, evenly spaced across the file, not a full decode — total I/O stays a few MB regardless of source file size. Supports 16/24/32-bit PCM and 32-bit float, matching every format `read_wav_pcm_layout()` already recognizes elsewhere in this app.
- Added 5 new pure-function tests for `compute_wav_peaks()`/`wav_duration_seconds()` (duration math, per-bucket amplitude accuracy for 16-bit PCM with known planted values, 24-bit PCM sign-extension, float32, and an empty-data-chunk edge case) — full suite now at 205 passing tests. The player/layout itself is Tkinter widget wiring with no more pure logic to characterize, but verified thoroughly against a real file and a real subprocess: duration and 600 real peaks loaded on selection; Play spawning a genuine playing `sox` process; Pause freezing elapsed time exactly (confirmed the process is truly suspended, not just visually paused) and Resume continuing it; Stop and a mode switch both fully terminating and reaping the process (no zombie, no still-alive PID); and the same confirmed again through a real `_on_close()` call. All prior modes (including Browse itself, cycled back into after visiting every other mode) still switch cleanly with `table_frame`'s reverted, simpler packing.

## 260915.1303

- Investigated "dragging a column border to resize enters a weird state — after releasing the mouse, the width keeps following it" in the WAV Files list. Likely cause: `drag_source_register()` (added for dragging files onto the collected list) inserts tkdnd's own `TkDND_Drag1` bindtag ahead of Treeview's built-in class bindings, listening for `ButtonPress-1`/`B1-Motion` across the *entire* widget — including the header, where Treeview's own native column-resize drag already uses that exact same gesture. The two compete for the same mouse-down-and-drag: resizing a column can have its `ButtonRelease` swallowed by tkdnd's own machinery, leaving Treeview's internal resize state stuck believing the button is still held — which reads as exactly "width keeps following the mouse with nothing pressed."
- `browse_file_list` now suspends its drag-source registration for the duration of any press that starts on the column header or a separator (`identify_region()` returns "heading"/"separator"), and re-registers on release — row dragging (onto the collected list) is unaffected, since a press on an actual row never triggers the suspension.
- Flagging directly: this class of bug — two different mouse-gesture-detection systems (Tk's own Treeview internals and tkdnd's Tcl-level bindtag, both triggered by the same click-and-drag) racing at the bindtag-dispatch level — is inherently hard to prove fixed from a script; synthetic `event_generate()` calls confirm the suspend/resume logic itself is correct (header press suspends the drag source, release re-enables it, a real row press leaves it untouched, and a genuine row drag still produces valid data afterward) but can't fully reproduce the real mouse-driven interaction that triggered the original report. Please confirm column resizing feels normal now.
- No new regression tests (Tk bindtag/event-dispatch behavior, not pure logic). Full suite still at 200 passing tests.

## 260915.1256

- Found the real cause behind "the Filename column width snaps back" in the WAV Files list, and checked whether any other Browse mode column had the same problem — one did. Root cause, confirmed with a direct test: a `stretch=True` column's width gets recomputed to fill whatever's left over on *any* resize of its containing widget, not just the app window growing — and Browse mode's panes now sit inside two nested, actively-dragged `PanedWindow`s (the tree↔file-list split, and the whole explorer↔collected-list split from two entries ago), so a stretch column there gets recomputed on nearly every sash drag nearby, undoing a manual resize almost immediately. Verified directly: setting Filename's width to 400 and then just dragging either sash reset it to whatever fit, every time.
- Neither column in the WAV Files list is a stretch column now — Filename and Path both keep exactly whatever width they're dragged to, with the horizontal scrollbar (added two entries ago) picking up any slack. Checked the collected list on the right too, since it sits in the same now-resizable `main_split`: Browse mode's Source column had the identical problem, so it's no longer the stretch column there either — Split/Re-embed/Rename/Combine's own stretch columns (Target/Planned Filename) are untouched, since their pane isn't dragged the way Browse's is.
- No new regression tests (pure Tk geometry-manager behavior). Verified directly against the running widgets: manually setting Filename to 400px and the collected list's Source to 500px, then dragging both the outer and inner sashes, confirmed both widths held exactly — 400 and 500 respectively — through every drag; a follow-up check confirmed Split mode's Target and Rename mode's Planned Filename still correctly report `stretch=1`, unaffected by this Browse-only change. Full suite still at 200 passing tests.

## 260915.1241

- Reversed course on shortcut customization per the user's follow-up: Keyboard Shortcuts in Settings is now **read-only** — every shortcut is fixed by the app (`DEFAULT_KEYBINDINGS`, unchanged at runtime), just still listed there so nothing is hidden in the code. Removed the "Change..." capture dialog, conflict-reassignment prompt, and the settings.json persistence path entirely (`_open_keybinding_capture_dialog`, `_set_keybinding`, `_save_keybindings_now`, and `build_keybinding_sequence` — all now dead code) — `_on_close()` goes back to saving only `formula_presets`/`window_geometry`, same as before shortcuts existed at all.
- Added **⌘L** to toggle the Execution Log window.
- Audio Inspector's and the Execution Log's shortcuts (⌘I / ⌘L) now **toggle** — press again to hide — instead of always just showing, since both are separate windows a keystroke can just as easily want to dismiss as summon. Toggling the Inspector off withdraws it (keeps the instance and whatever it last had loaded) rather than destroying it the way its own window close button does, so toggling back on is instant and doesn't need to re-scan anything. The existing "Audio Inspector"/"Log" toolbar buttons are deliberately unchanged (still always show-and-focus) — the request was specifically about the keyboard shortcuts, and a toolbar click is a more deliberate action than a quick dismiss keystroke.
- Removed 6 now-obsolete tests for `build_keybinding_sequence()` (the function itself is gone) and added 2 in its place: every action has a real default and a recognized scope, and Inspector/Log don't accidentally share a default key. `keybinding_display_label()`'s own tests are untouched — it's still what renders the read-only Settings list. Verified against a real pumped Tk event loop: ⌘L and ⌘I each toggle their window between withdrawn and shown, a second ⌘I toggle confirmed the *same* Inspector instance comes back (not destroyed and rebuilt), and the Settings page confirmed to have zero "Change..." buttons anywhere. Full suite now at 200 passing tests.

## 260915.0041

- Three more layout fixes from real use of Browse mode and the new Keyboard Shortcuts editor:
  - Dragging the outer sash (between the whole folder-browser and the collected list on the right) was also silently shifting the inner sash (between the folder tree and the WAV Files list) — reported as "resizing the right side also moves the left side." Root cause: the tree pane's `weight=1` meant ttk's PanedWindow treated it as eligible to absorb a share of any resize that changed the inner split's *total* width, including one caused by dragging the *outer* sash, not just a direct drag on its own sash. Set the tree pane's weight to 0 — it now only ever changes width from a drag on its own sash.
  - The WAV Files list is now two columns instead of one combined "folder/filename" string: **Filename** (the stretch column, so it's never truncated or scrolled out of view) and **Path** (fixed-width, drag to widen, with its own horizontal scrollbar) — a recursive listing's path can get long, and filename was the part actually worth always seeing in full.
  - The Keyboard Shortcuts list in Settings is now a fixed-height, scrollable list (Canvas + Scrollbar, mouse wheel included) instead of a plain stack of grid rows — with only the 7 default shortcuts, the rows already ran taller than a reasonably-sized Settings window, so anything below would have been genuinely unreachable without this.
- No new regression tests (all three are Tkinter widget/geometry-manager behavior, not pure logic), but each verified against a real, pumped Tk event loop: dragging the outer sash via `sashpos()` and confirming the inner sash's position is bit-for-bit unchanged, then confirming the inner sash still moves on its own when dragged directly; a real recursive + non-recursive listing producing the correct (filename, path) pair per row; and the Settings shortcuts canvas's scrollregion genuinely exceeding its visible height with `yview_scroll()` actually moving the view. Full suite still at 204 passing tests.

## 260915.0027

- Added a **Keyboard Shortcuts** editor to Settings, saved across launches (the app's very first setting to persist beyond window geometry / formula presets). Covers every action requested: switching to each mode (Browse/Split/Re-embed/Rename/Combine/QC/Settings), Open Audio Inspector, Preview, Execute, QC Compare, plus Browse mode's own Tag-into-list and list-subfolders-recursively actions. Confirmed the modifier convention with the user first: every one of those app-wide shortcuts requires a modifier (⌘/⌃/⌥) by default, so it can never get typed into a text field by accident — except Browse mode's two, which stay bare/unmodified (`t`, `Return`) since that list and tree never hold any text input to collide with.
- Settings shows each action's current shortcut with a "Change..." button opening a small dialog: checkboxes for the modifiers (required for the app-wide actions, hidden for Browse's two) plus a key field. Reassigning a combination already used elsewhere asks before stealing it back from whichever action had it — never silently double-bound to two things at once, since Tk's own bind table would just have the second one silently win.
- `_build_keybinding_handlers()`/`_apply_keybinding()` route every action through one shared mechanism instead of scattered ad-hoc `.bind()` calls (which is what Browse mode's Tag/recurse actions used to be, from the earlier two entries — both now flow through this same system, so they're editable and persisted like everything else, with identical runtime behavior). App-wide actions bind via `bind_all`; Browse's two bind directly on `browse_file_list`/`browse_folder_tree` only.
- Fixed a real bug caught by writing the persistence test: `_on_close()` already saved `window_geometry`/`formula_presets` with a fresh dict passed straight to `save_app_settings()` (a full overwrite, not a merge) — adding keybindings without also including them there would have silently erased any shortcut change made during the session the instant the app was closed normally, even though `_set_keybinding()` itself already writes them to disk immediately on every change (in case of a crash before a clean close reaches `_on_close()` at all).
- Added 9 pure-function tests for `build_keybinding_sequence()`/`keybinding_display_label()` (modifier combinations, case-folding a bare single-letter key, the empty-key error, and a round-trip check against every action's own factory default). The rest is Tkinter widget/event wiring with no more pure logic to characterize; verified thoroughly instead with real pumped Tk event loops: every default mode-switch shortcut (⌘1–⌘7) firing correctly from a fresh app; Browse's `t`/`T` tag action and `Return` recursive-listing action still working unchanged after being rewired through the new system; reassigning a shortcut live (old sequence stops firing, new one fires) and — most importantly — a genuine two-process-equivalent restart (`WorkflowApp` → `_on_close()` → a second, independent `WorkflowApp`) confirming a changed shortcut survives exactly as it would across a real quit and relaunch. Full suite now at 204 passing tests.

## 260915.0014

- Browse mode's folder tree now recurses into every subfolder when you press Enter (or the numpad Enter) on a selected folder — the normal Up/Down/Left/Right navigation (ttk.Treeview's own built-in bindings, untouched) still only lists that folder's own top-level WAV files, cheaply. Recursing into every subfolder is a deliberately separate, heavier action gated behind an explicit Enter, not something that happens on every arrow-key move.
- A recursive listing labels each row with its path *relative to the folder that was opened* (e.g. `2026-01-13.AAN/UB4673==4 1 t1==PN.WAV`) instead of the bare filename — recorder files routinely reuse the same filename across sibling date folders, so the bare name alone would make two entirely different files look identical once subfolders are mixed into one list. The stored path per row (used by Inspector/Add-to-List/drag) is always the real full path regardless of the label shown.
- Verified against real files: selecting `ISO POLY` itself (no files directly inside it — everything is one level down, inside each date subfolder) correctly showed 0 with plain arrow-key navigation, then a real `<Return>` keypress on that same folder correctly found and listed all 1346 real WAV files across every one of its `*.AAN` subfolders, each row's stored path resolving to the real file it claims to be. No new regression tests (same Tkinter-wiring category as the rest of Browse mode). Full suite still at 195 passing tests.

## 260915.0004

- Three more Browse mode refinements from real use, on top of the previous two entries:
  - The collected list's Source column showed only the filename for files added from the same folder, since it was relative to a common-ancestor root that happens to collapse to "just the filename" when everything so far came from one place — two different folders' same-named files would then look identical and ambiguous. It now always shows the full absolute path for Browse jobs specifically (every other mode's Source column is untouched).
  - The middle WAV Files list can now be dragged straight onto the collected list on the right (single or multi-select) — needed no new drop-side code at all, since the collected list was already a DND_FILES drop target with a handler that already understands Browse mode; a drag from the file list just produces the same kind of data a real Finder drop would.
  - "T" (or Shift-T) now tags the current selection into the collected list without reaching for the mouse, mirroring "Add Selected to List". Wiring this up surfaced a real focus-stealing bug along the way: clicking a file for the first time auto-opens the Audio Inspector (from the previous entry), and on this Tk/macOS combination, that window's own `deiconify()` steals keyboard focus away from the file list on a later, OS-mediated tick — neither a synchronous `focus_set()` nor one deferred via `after_idle()` reliably won that race (both fired before the OS actually handed focus over); a short real delay (`after(80, ...)`) does. Without this, "T" would silently do nothing immediately after the very click that opened the Inspector, only working after an extra click back onto the file list.
- No new regression tests (same Tkinter-wiring category as the rest of Browse mode), but each of the three was verified against real files and a real pumped Tk event loop: two files from genuinely different folders both showing distinct full paths in the collected list; `<<DragInitCmd>>`'s returned data round-tripping correctly through `tk.splitlist()` (including a real path containing a space and Chinese characters) and correctly returning `None` when nothing is selected; and the exact real-world sequence — select a file (Inspector auto-opens) → press `t` immediately, no intervening click — landing 2 files in the collected list. Full suite still at 195 passing tests.

## 260914.2353

- Two follow-up fixes on the previous entry's Finder-style Browse mode, both from real first use: the outer boundary between the folder browser and the collected list on the right was two frames just packed side by side with no actual divider — dragging where a sash should be did nothing, which is what "the middle WAV list can't be resized" turned out to mean (the inner tree↔file-list divider already worked; this outer one never existed as a real sash at all). And the collected list on the right was showing every Split/Rename column (Slot, Trk#, Stream#, Target, Planned Filename, Tape, TC, Scene, Take) as a row of dashes, since none of them mean anything for a browsed file — wasted width for no information.
- Fixed both. The table now sits inside a real `ttk.PanedWindow` (`main_split`) instead of being packed directly — table_frame is always one of its panes (every other mode sees what's functionally a plain frame, since a one-pane PanedWindow behaves identically), and Browse mode additionally inserts the folder-browser frame as a second, genuinely drag-resizable pane to its left, removed again via `.forget()` (not destroyed) when leaving Browse mode. This needed table_frame's own master to become the PanedWindow at construction time, since a widget's master can't be changed after creation in Tk — a bigger change than it looks from the diff, but invisible to every mode except Browse.
- Browse mode's collected list now shows only the Source column (`self.table.configure(displaycolumns=("source",))`) — already a full path with the filename as its last segment, so nothing else needed adding; every other mode still gets `displaycolumns="#all"`.
- No new regression tests (same Tkinter-wiring category as the rest of Browse mode), but verified thoroughly with a real event loop: `ttk.PanedWindow.sashpos()` used to genuinely drag the new outer divider and confirmed its position actually moved; mode-switch cycling confirmed the folder-browser pane is added exactly once per entry into Browse mode and fully removed on the way out (checked via `main_split.panes()`, not just visually); confirmed `displaycolumns` flips between `("source",)` and `("#all",)` correctly on each switch; re-ran the full real-files Browse walkthrough from the previous entry (volume → subfolder → file click → Inspector → Add Selected to List) to confirm none of this regressed it. Full suite still at 195 passing tests.

## 260914.2343

- The previous entry's Browse mode (drag-only, one accumulating list) wasn't actually what was asked for — the user clarified they wanted to navigate folders inside the app itself, like Finder, not just drop files in from it. Kept the drag-and-drop and the accumulating list (still genuinely useful for collecting scattered picks across folders), and added an actual Finder-style navigator alongside it.
- Browse mode is now a left/right split: **left** is a folder tree (starts at Home plus every mounted volume under `/Volumes`, lazy-loaded — a folder's real subfolders are only read from disk the first time it's expanded, so the tree costs nothing for parts never opened) with a file list showing the selected folder's WAV files; **right** is the same accumulating list from the previous entry, fed by both drag-and-drop and a new "Add Selected to List" button. Clicking a file in the folder browser previews it in the Audio Inspector immediately, live-preview style — the first click opens the Inspector if it wasn't already up, every click after that just refreshes its content without stealing focus back from wherever the window is, so clicking through a folder doesn't fight the user for window focus on every row.
- Factored `open_inspector()`'s "create the window if it doesn't exist yet" logic into `_ensure_inspector_exists()` so the new live-preview path could reuse it instead of duplicating it.
- No new regression tests — this is Tkinter widget/event wiring with no pure logic to characterize (matching the Execution Log window and Inspector's own mechanics), but verified thoroughly against real files with a real Tk event loop pumped end-to-end: selecting a real mounted volume in the tree, expanding it to reveal real subfolders (including a Chinese-named project folder), navigating to a real 61-file folder, clicking a real 12-channel Poly file (which correctly opened the Inspector and rendered its actual File/Audio sections), and using "Add Selected to List" to merge two files into the accumulating list. Full suite still at 195 passing tests (nothing pure-logic changed).

## 260914.2332

- Added a new **Browse** mode — its own tab (now the first tab, and the default mode on launch), with no Source/Target folder, no Preview/Execute, no conflict policy. Drag any WAV file(s) in from Finder — from anywhere, including several completely unrelated folders in the same drop or across drops — and they accumulate into a list; select a row while Audio Inspector is open to flip through each file's metadata and channel info. Requested after the drag-and-drop-into-Split/Rename feature's "must be inside the current Source folder" restriction got in the way of a workflow that was never about Split/Rename/Combine in the first place: casually checking scattered files from Finder.
- That restriction itself is a real constraint, not an arbitrary one — Split/Rename/Combine compute every Target path as `output_root / source.relative_to(source_root)`, which only works when every job's source shares one root. Browse mode sidesteps the question entirely instead of relaxing it: a dropped file becomes an inert `Job(source, target=None, ..., mode="browse")` with nothing to resolve, no Target math involved, and nothing that could ever reach Execute.
- Reused the existing accumulate-on-drop, table-row-to-Job (`preview_jobs`), and select-while-Inspector-is-open-live-refreshes machinery as-is — the only Inspector-side change was routing `job.mode == "browse"` to the same clean "No Target" label the Rename modes already use, instead of the Re-embed-flavored "Target Not Found". `_apply_table_stretch_column()` now flexes the Source column in Browse mode (the only column with real content there) instead of Target.
- Added `test_browse_job_status_passes_through_unchanged`. Verified against real files end-to-end via two driver scripts (Tk mainloop pumped for real): dropping a 12-channel Poly file and a completely unrelated render file from a different volume path both landed in the list with no root-mismatch error; opening Audio Inspector and selecting the Poly file's row correctly rendered its full File/Audio/Channel Configuration sections on the left and "No Target" on the right; switching from Browse back to Split mode with a valid Source correctly re-enabled the Preview button (browsing needed to force it off, but must not leave it stuck off in a real workflow mode afterward). Full suite now at 195 passing tests.

## 260913.1623

- The previous entry's fix wasn't the whole story — the user reported the exact same "100%/ETA <0.1s but nothing in the log" freeze still happening, right after a phase that previous fix hadn't touched: "Reading Poly track list". Went through every mode's job builders looking for the same pattern (a bwfmetaedit-backed read, per file, in a loop with no progress callback) instead of only patching the one already reported, and found three more:
  - `build_reembed_jobs()`'s Target-metadata read (a `split_recorder_channel` call for every Target candidate across every base) — the very next phase after "Reading Poly track list", and potentially the largest of all once Poly matching means Source and Target no longer need to be the same size.
  - `build_rename_jobs()`'s "Metadata Tape + TC" Serial ID ordering pass and its Metadata Template fields pass — both already have their own "this may take a while for large folders" confirmation dialog before the user even enables them, but the read loops behind those warnings had no progress reporting at all.
  - `match_qc_folder_files_by_formula()` (Folder QC's fallback pairing when filenames don't match exactly) — reads a recorder channel per remaining file on each side, before the main comparison loop that does report progress even starts.
- All four now report progress the same way the rest of their own function already does — new status labels ("Reading target metadata", "Reading Tape + TC metadata", "Reading Metadata Template fields", "Matching by Base Formula (Source/Target)") feeding the same existing progress-event plumbing, no new UI wiring needed anywhere.
- Added `test_reembed_reports_progress_while_reading_target_metadata`, `test_rename_jobs_report_progress_while_reading_tape_tc_metadata`, `test_metadata_template_reports_progress_while_reading_fields`, and `test_match_qc_folder_files_by_formula_reports_progress_on_both_sides`. Re-verified against the same real 636-job batch from the previous entry with full event timestamps: "Reading target metadata" now correctly reports (0.5s–1.33s in this batch — small here since this Target folder only had 131 files, but wired the same way regardless of size), and the previous entry's "Resolving orphan targets" fix is confirmed still covering the dominant ~8.5s gap. Full suite now at 194 passing tests.

## 260913.1508

- Fixed the "progress and ETA say 100% done, but the app just sits there with no log" gap the user reported across Preview, Execute, and Compare. Found two distinct silent passes, both running real `bwfmetaedit` subprocess calls per file with zero progress reporting, after whatever loop had *just* reported 100%:
  - `build_reembed_jobs()`'s last-resort metadata-identity pairing (the pass that rescues a `NO TARGET` source when a DAW consolidate/render rewrote its Target's base name) reads two `bwfmetaedit`-backed fields per orphan Target *and* per remaining `NO TARGET` source, with no progress callback at all. Verified against the user's real files (`ISO POLY/2026-02-04.AAN` → `ISO AAP/2026-02-04.AAP`, 636 jobs, 505 of them `NO TARGET`): the main scan reported "636/636" at 1.7s, then ran silently for another **8.8 seconds** doing exactly this pass before the Preview actually finished — precisely the symptom described.
  - `augment_qc_rows_with_metadata()` (the Metadata pass QC's Poly comparison runs after its own Audio pass) already had a `progress` parameter and its own docstring warning about this exact risk — `compare_poly_split_folders()` (the "Compare" path backing both the Poly QC tab and the auto-detect QC flow) just wasn't passing it through.
- Both now report progress: the orphan-pairing pass emits periodic `"Resolving orphan targets"` events (reusing Re-embed preview's existing progress-event plumbing, so no new UI wiring was needed); `compare_poly_split_folders()` now forwards its own `progress` into the Metadata pass, which was already built to accept it. Along the way, `compare_poly_split_folders()`'s `progress` callback changed from a 2-arg `(current, total)` shape (which could only ever say "Comparing") to the same 3-arg `(stage, current, total)` shape the sibling `compare_qc_folders()` already used — both of its callers (the dedicated Poly QC tab and the QC auto-detect flow) updated to match, and the auto-detect flow's two now-redundant lambda wrappers were removed since its own `report` callback already had the right shape.
- Added `test_reembed_reports_progress_during_the_last_resort_identity_pairing` and `test_compare_poly_split_folders_reports_progress_during_the_metadata_pass`; updated one existing test whose `progress` lambda still assumed the old 2-arg shape. Verified against real files as above — the same 636-job batch that used to go silent for 8.8s now surfaces 12 `"Resolving orphan targets"` progress events spanning that exact window. Full suite now at 190 passing tests.

## 260913.1324

- Re-embed can now match a Poly (un-split, multi-channel) source directly against rendered mono Targets, the same "Poly-to-Split" pairing QC's Poly comparison already does — no need to materialize individual mono split files first just to Re-embed. Requested after noticing QC could do this comparison but Re-embed couldn't.
- Confirmed the approach with the user first: any Source file that doesn't match the mono `{base}.A$` Source formula is now auto-checked for whether it's genuinely multi-channel (a cheap WAV-header read, not a new "Source type" toggle to flip — the user asked whether auto-detection was feasible instead of a manual switch, and it is, since checking channel count needs no more than the header). A genuinely multi-channel candidate has its own embedded iXML Track List read (`poly_track_mapping()`, the exact function QC's Poly comparison already uses) to get each stream's real recorder channel, then matched against Target files by that recorder channel — independent of the Poly file's own filename. A mono file that simply doesn't match the pattern is left alone exactly as before; a genuine Poly file whose Track List can't be read is now a visible `BLOCKED: cannot read Poly Track List` row instead of silently vanishing.
- The execute step now reads the correct single-channel TRACK entry out of a Poly source's full iXML (`mono_ixml_from_poly_slot()`, the slot-aware reader Split already uses) instead of `normalize_mono_ixml()`, which always reads `tracks[0]` — correct for an actually-mono source, but silently wrong (always channel 1's metadata) for any Poly stream past the first. Which reader runs is decided per job by re-checking the source's real channel count at execute time, not by a stored flag, so it stays correct even if a job list is rebuilt in between.
- The existing Stream# numbering (fixed in the previous entry to skip gaps from unmatched channels) and the rename-after-embed conflict handling both apply uniformly to Poly-derived jobs with no special-casing needed — they were already generic over "whatever ended up READY," not specifically over "a mono source."
- Added `test_reembed_matches_poly_source_channels_via_track_mapping`, `test_reembed_poly_source_missing_track_list_is_blocked_not_silently_skipped`, `test_reembed_plain_mono_file_that_just_does_not_match_is_left_alone`, and `test_reembed_execution_uses_the_poly_slot_reader_for_a_multichannel_source`. Verified against the user's real files: pointing Source directly at the original `ISO POLY/2026-01-31.AAN/UB5545==76 2 t1==PN.WAV` (12 real channels) against the real rendered `ISO AAP` targets matched channels 2–9 as READY (Stream# 1–8, gaps skipped exactly as the previous fix intended) and correctly left 1/10/11/12 as NO TARGET — with no mono split file for this take ever needing to exist on disk. Full suite now at 188 passing tests.

## 260912.2157

- Fixed Re-embed's "Filename numbering: Stream#" not meaning what the user actually wanted from it: they described the intended distinction as Trk# = the recorder's real physical channel number, gaps and all; Stream# = whatever channels actually got matched/mapped, numbered 1, 2, 3... in order regardless of what the original channel numbers were. `build_reembed_jobs()` instead numbered *every* source channel that matched the Source formula, READY or not, before knowing which ones would actually find a Target — so a take where channel 1's render was simply never produced (confirmed against the user's real files: the Target folder only had render files for A2–A9, nothing for A1/A10/A11/A12) still burned Stream# slot 1 on the missing channel, and the renamed output started at `.A2` instead of `.A1`.
- Stream# is now assigned in a final pass, after every job's READY/not-READY status is fully settled (including the last-resort metadata-identity pairing, which can still turn a NO TARGET into READY) — only READY jobs consume a Stream# slot, numbered 1..N per base in recorder-channel order. A not-READY job keeps a fallback number (its own original channel number) just so the table/TSV has something to show; it's never used for a rename since nothing not-READY executes.
- Incidental fix that fell out of unifying the two paths into one finalize step: a job that only became READY via the metadata-identity pairing (a DAW consolidate/render rewriting the take-number digits in its base name) previously skipped the rename-target conflict check entirely; it now gets the same `Skip existing`/`Replace existing`/`Ask` handling as a normally-matched job.
- Added `test_reembed_stream_numbering_skips_gaps_left_by_unmatched_channels`, and verified directly against the user's real files (`ISO SPLIT/2026-01-31.AAN` → `ISO AAP/Rendered Files`, `{base}.A$` → `{base}.A$ render \d+`): Trk# still reads 2–9 for the affected take (its real recorder channels), Stream# now reads 1–8, and the rename-after-embed output now starts at `.A1` instead of `.A2`. Full suite now at 184 passing tests.

## 260912.1638

- Fixed manually dragging "Target" (or, in Rename mode, "Planned Filename") wider snapping straight back — checked every mode's list columns as requested, and traced it to `_apply_table_stretch_column()`. It's called from `_update_mode_ui()`, which itself fires on every mode-tab click *and* on nearly every Rename-panel control change (`_on_rename_option_changed()` calls it before every live-refresh) — and until now it re-ran the full `configure_treeview_column_widths()` each time, which unconditionally reapplies `TABLE_COLUMNS`' static default width to every column. So a manual resize on the stretch column (Target normally, Planned Filename in Rename mode) survived only until the next mode switch or Rename option tweak, which for Rename could be almost immediately — any checkbox, dropdown, or Find & Replace edit did it.
- `_apply_table_stretch_column()` now only repoints the `stretch` flag between "target" and "planned" on mode switch — it never touches `width` again after the one-time construction call that sets the initial defaults. A manual resize on any column now persists across mode switches and Rename option changes.
- Checked the three QC tables (file/folder/poly) too: each calls `configure_treeview_column_widths()` exactly once at construction and never again, so they were never affected by this — confirmed via grep, no change needed there.
- No regression test added (same class as the earlier stretch-column fix — pure Tk widget-option wiring), but verified directly against the running app: a driver script created a real `WorkflowApp`, dragged Target to 555px and Planned Filename to 444px, then drove the exact sequence that used to reset them (`_update_mode_ui()` on a mode switch to Rename and back, plus `_on_rename_option_changed()`) and confirmed both widths held throughout. Full suite still at 183 passing tests.

## 260912.1416

- Addressed the actual root cause behind the previous entry's incident, per the user's own diagnosis: since the Execution Log moved into its own closable window (`260910.2204`), the main window had no persistent way to show a Preview/Execute was still running — the only status label bound to `status_var` lived on the Settings tab, so on any other tab, with the Log window closed (or just not glanced at), an operation in progress was invisible, which is exactly how a drag-and-drop import got fired off during a still-running background Preview.
- Added a status line that's part of every mode's fixed header (right under the toolbar, above the table/QC/settings area) — bound to the same `status_var` already updated with live progress, so it needed no new state, and always visible regardless of which tab is active or whether the Log window is open. It turns red (`#b91c1c`) whenever Pause/Cancel are enabled (a real operation is running) and back to the normal text color when idle, so a running operation is now visible at a glance without opening the Log. Removed the old Settings-tab-only status label it supersedes.
- No regression coverage — this is Tkinter widget/color wiring with no pure logic to characterize, the same category as the Log window and Inspector's own mechanics. Verified by launching the real app (no construction-time error) and running the full suite, still 183 passing tests.

## 260912.1323

- Fixed Pause/Cancel becoming permanently disabled — with no way to stop a still-running operation — after dropping files into the list while a full folder Preview was already running in the background. Root cause: dropping files calls `_import_dropped_files()`, which (unlike the Preview/Execute buttons themselves) has no busy check, so it can run concurrently with a real background Preview. It ends by calling `_show_preview()` to render the merged job list — the same method the real Preview's worker thread calls when it actually finishes — and `_show_preview()` cannot tell those two callers apart: seeing `active_operation_phase` still set to `"preview"`, it treated the drop as "the Preview finished," cleared that flag, and disabled Pause/Cancel — while the real scan (in the case reported, a 4,698-file folder, ~9 minutes in with ~21 minutes left) kept running completely unattended and now uncancellable in the background. The user could also click Execute during this window, since the drop's `_show_preview()` call had already re-enabled it despite the real Preview still being active.
- `_import_dropped_files()` now refuses the drop while `active_operation_phase` is already set (Preview, Execute, or QC), logging "Busy (<phase>) — wait for it to finish before importing more files." instead of silently racing it. No regression test added — this guards a live Tkinter instance's operation-phase state, the same class of UI-only wiring the Execution Log window and Inspector's own mechanics already go without unit coverage for; verified by launching the real app and confirming no construction-time error. Full suite holds at 183 passing tests.
- Separately investigated, not a bug: an earlier one-off report of Pause/Cancel appearing to render in the wrong position (overlapping the list instead of their own toolbar row) could not be reproduced — a restart alone put both buttons back in the correct place with normal behavior. Left unexplained for now; if it recurs, a screenshot taken at the moment it happens (rather than after) would help pin down whether it's a genuine layout bug or a transient Tk repaint glitch under the heavy log/table update load a large batch produces.

## 260912.1205

- Added drag-and-drop file import for Split and Rename mode's preview table, mirroring Sound Devices Wave Agent's own "drop what you want to work on" flow — dropping one or more WAV files directly onto the list adds jobs for just those files, without pointing Source at (and scanning) their whole containing folder first. Requested after a real corrupt-file case where the user only wanted to test-split the one flagged file, not rescan the large folder it came from. Multiple drops accumulate into one list (dropping a file already present refreshes its job in place rather than duplicating it); if Source is still blank, the first drop's file(s) adopt their common folder as Source automatically; if Source is already set, dropped files must be inside it (a file from elsewhere is rejected with an explanation, since the app's relative-path math throughout assumes one shared Source root per job list).
- `build_split_jobs()` and `build_rename_jobs()` both gained an `only_files` parameter — when given, it replaces the normal whole-folder scan while every other rule (status, conflict policy, Metadata Template, Serial ID, ...) behaves identically to a real folder Preview containing just those files. Verified against real files (a 6-channel Poly source dropped into Split, and one Split channel file dropped into Rename) — only the dropped file's own jobs are built, everything else already in the same real folder is correctly left alone.
- Added regression coverage for `only_files` on both builders. Full suite now at 183 passing tests.
- Still open from the same conversation, not implemented here: making Split more tolerant of a corrupted-but-partially-readable source (the truncated-iXML case that started this) — proceeding with a sequential Trk# fallback and whatever BEXT metadata survived, instead of blocking the whole file the moment iXML can't be read. Offered to the user as a follow-up; needs their go-ahead first.

## 260912.1133

- Fixed the Execute completion summary ("Done: N OK; M failed/cancelled") silently undercounting — it only recognized a status starting with "OK" or "FAIL"/"CANCELLED" as worth counting, so a row that was neither (`BLOCKED: ...` from a source file Preview couldn't even read — the user hit this with a truncated WAV whose RIFF/iXML chunk sizes didn't match its real byte count — `SKIP EXISTS`, `SKIPPED: conflict`, a merge whose audio succeeded but metadata failed, ...) wasn't counted as a failure *or* a success. The summary read "0 failed" and looked fully successful at a glance, even though that file was never processed. The row and its real status were always written to the saved TSV — nothing was silently dropped there — but the one line most people actually check hid it completely, which is how the user only discovered a file was missing later, during actual work.
- The summary now has a third bucket: `Done: N OK; M failed/cancelled; K not processed (see Status column)` whenever anything falls outside the first two, so the counts always add up to the full batch and nothing worth a second look can hide behind "0 failed" again.
- Added `summarize_processing_rows()` with regression coverage: OK/FAIL/CANCELLED counted as before, BLOCKED and the other non-OK/non-FAIL statuses landing in the new third bucket instead of vanishing, and the three counts always summing to the total row count. Full suite now at 181 passing tests.

## 260910.2204

- Moved the Execution Log out of the main window into its own floating window (`ExecutionLogWindow`, the same `Toplevel` pattern the Audio Inspector already uses) — the previous release's mutually-exclusive Formula/Metadata Template redesign helped, but the user found even the single remaining active card plus the table could still push a docked log past the bottom of an already-maximized screen, which no window resize can fix since there's no more screen left to use. The log is no longer part of any mode's fixed vertical layout at all: it opens automatically when Preview, Execute, or a QC folder/poly compare starts, and a new "Log" button next to "Audio Inspector" in the toolbar reopens it anytime (closing it just hides it — logging keeps happening in the background either way, so nothing is lost while it's closed). Applies uniformly to every mode, not just Rename.
- No regression coverage for this — it's UI-only window/geometry wiring (there's no pure logic change), matching how the Inspector's own window mechanics aren't unit-tested either. Full suite holds at 177 passing tests.

- Fixed `$sceneT$take` (a known token directly followed by a letter, e.g. to build "3-25A-1T1" from Scene + Take) resolving wrong — the greedy bare-token matcher read "sceneT" as one unrecognized token name and produced nothing. Reworked `expand_metadata_template()` to match how the source REAPER script actually handles this: known token names are recognized as literal substrings first (longest name checked first, so `$trkall` is never partly consumed by the shorter `$trk`), *then* expanded — so `$sceneT$take` now correctly reads as `$scene` + literal "T" + `$take`. This also makes more bare tokens usable without needing `${...}` braces (e.g. `$umid_pt` now works unbraced too).
- Fixed the Rename table's stretch column staying pinned to "Before Rename" (the old filename) in Rename mode, so trying to widen "Planned Filename" — the column actually worth seeing in full there — kept snapping back. The stretch column is now mode-aware: "Planned Filename" in Rename mode, "Target" everywhere else that column still means an output path.
- Fixed the Metadata Template toggle being invisible to three separate "is any rename action selected?" checks that all predate it (the live-refresh guard, `execute()`'s own guard, and `display_status()`'s per-row action list) — enabling only Metadata Template (nothing else) correctly ran the rename and produced the right Planned Filename, but every row's Status still said "No rename action selected" and Execute could refuse to run at all. All three now know about it.
- Added a **missing-token flag**: a token that resolves to nothing for a specific file (e.g. `$tape` on a file with no Tape metadata) now shows right in that row's Status — `Metadata Template (missing: tape)` — instead of silently leaving a gap in the filename. This is recorded, not a popup: for a batch of many files a per-file dialog would be far more disruptive than a status column entry (or a details table) already skimmable in the list and saved to the TSV.
- The exported TSV's settings header now records the Metadata Template toggle and its template text, alongside the other Rename settings it already recorded.
- **Rename mode's base-naming redesign**, decided with the user after testing raised exactly the ambiguity flagged in the previous draft of this entry: Formula (Add Text + Change $) and Metadata Template are now a mutually-exclusive choice — a "Rename base using: ○ Formula ○ Metadata Template" radio at the top of Rename mode — instead of Metadata Template quietly overriding `{base}` while Change $'s `.A$` channel-number suffix still got appended on top regardless (so a template like `${srcbaseprefix:6}` could already make "{base}" meaningless, yet still ended up with an auto-numbered suffix bolted on). Metadata Template now defines the *entire* renamed stem (extension untouched) with no automatic suffix — add `$trk`/`$chnum` to the template yourself if a channel-distinguishing token should survive. Find & Replace and Serial ID remain universal post-processing steps that apply either way, matching the source script's own Take Name Renamer being a separate, always-available pass.
- Only the selected naming source's card is now shown — Add Text + Change $ hide when Metadata Template is selected, and vice versa. This also fixes the real problem behind wanting a taller window: Metadata Template's token-button grid (by far the tallest thing in Rename mode) no longer sits on screen unconditionally pushing the Execution Log out of view — it only takes up space while actually selected, and Formula's own two cards free up that space when Metadata Template is active instead. (The window default/minimum size was still bumped slightly — 1180×900 / 980×760 — as a smaller, separate cushion.)
- Added regression coverage for the token-adjacency fix (`$sceneT$take`, and `$trkall` not being swallowed by `$trk`), the new `template_token_names()`/`find_empty_metadata_tokens()` helpers, `display_status()`'s new label-based Metadata Template parameter (including the missing-token case), Metadata Template defining the whole stem with no auto-suffix, and Serial ID still composing correctly on top of it. Full suite now at 177 passing tests.

## 260910.1950

- Added the UI for "Rename by Metadata" (Stage 2/3 of the port from hsuanice's REAPER "Rename Source File from Metadata" script — Stage 1, `260910.1918`, was the engine only). Rename mode gets a new toggleable "Metadata Template" card, alongside Add Text/Change $/Find & Replace/Serial ID: a template field plus two rows of click-to-insert token buttons (File/Session vs. File Metadata, color-grouped like the source script). When enabled, it replaces `{base}` with the expanded template — Change $/Serial ID/Find & Replace still apply on top of it afterward, in the same order as before, so all of Rename mode's existing features compose with it rather than being replaced by it.
- Enabling it the first time triggers the same "this reads metadata from every file, may take a while" confirmation and full re-Preview as Serial ID's "Metadata Tape + TC" already does, for the same reason (the tokens need to know things a filename alone can't say). Once read, the fields are cached on each Job (`metadata_template_fields`/`metadata_template_trk_table`), so afterward editing the template text, or any other Rename setting, only recomputes the token expansion — it never re-reads metadata from disk.
- **Deep Inspector integration**, as decided when this was scoped: every file the Inspector opens now has a "Metadata Template Tokens" section — one row per resolvable token with its live value, each with an "Insert" button. Clicking one switches the app to Rename mode (if it wasn't already) and inserts that token at the cursor in the Metadata Template field, so a value can be looked up and used without retyping it or memorizing the token name.
- `build_rename_jobs()` gained `metadata_template`/`metadata_template_fields_reader` parameters (the latter is a test/injection seam, mirroring how `serial_metadata_reader` already works for the Metadata Tape + TC feature) — metadata is only read at all when a template is actually set, so the common case (feature untouched) pays zero extra cost.
- Added regression coverage: the template replacing the pattern-matched base, falling back to the plain base when it expands to nothing (an unresolvable token, matching the source script's own fallback), and confirming the feature literally never reads metadata when disabled. Full suite now at 169 passing tests. Verified the app still launches cleanly with the new panel and Inspector section built (no construction-time errors); the interactive behavior — token buttons, the confirmation dialog, and the actual rename output — needs real use to verify, same as every other feature this session.

## 260910.1918

- Added the engine (no UI yet — Stage 1 of the "Rename by Metadata" port from hsuanice's REAPER "Rename Source File from Metadata" script): a new `operations/metadata_template.py` implements the script's `$token`/`${token}` free-form template system as a plain, file-agnostic function (`expand_metadata_template`), plus `umid_to_pt()` (BWF UMID → Pro Tools' 26-6-16-12-4 grouping). Bare `$name` tokens deliberately stop before `_`/`:` so `$scene_v2` means the token plus a literal suffix rather than an unknown "scene_v2" token — parameterized/underscored token names (`${counter:3}`, `${umid_pt}`) need the brace form, matching the source script's own actual behavior.
- Added `collect_metadata_template_fields()` (main file) — the per-file field/track-name-table builder the engine needs. All the "already have this data" tokens (project/scene/take/tape/ubits/framerate/speed/date/time/year/originationdate/originationtime/startoffset/samplerate/channels/length) reuse this session's existing `read_serial_metadata`-family readers; the two genuinely new reads are UMID (`CORE_FIELDS` already requested it from bwfmetaedit — it just wasn't exposed anywhere) and per-channel track names (`$trk`/`$trkall`/`$trkN`, from the same iXML TRACK_LIST the Inspector's Track List table already parses). `$origsrcfile` (the pre-rename filename, for rollback) reads from the WAV's own standard iXML `HISTORY/ORIGINAL_FILENAME` — cleaner than the source script's approach of stashing it in a REAPER item note, since a WAV file has no note field to co-opt.
- Deliberately not ported: `$track`/`$marker`/`$curtake`/`$curnote`/`$clearnote` (REAPER track/marker/take/note — no equivalent on a bare WAV file) and `$overlapindex`/`$rangeindex` (REAPER-timeline-overlap-based numbering — no timeline for a folder of files). Everything else in the source script's token set now has a working, tested resolver.
- Verified against this project's real `S2-1T01.A2.wav` test file: `collect_metadata_template_fields()` correctly pulls Project/Scene/Take/Tape/UBITS/OriginationDate/Year/TimeReference/Trk name/Recorder Channel/sample rate/channel count/duration, computes the matching UMID (PT) grouping, and reads back the exact pre-split poly filename ("S2-1T01.wav") from that file's real iXML HISTORY block.
- Added regression coverage for the template engine (plain substitution, unknown tokens, the bare-vs-braced underscore distinction, `${counter:N}` vs bare `$counter`, `${srcbaseprefix/suffix:N}`, `$trkN` via the track table, per-value sanitization with literal text left untouched, sanitize=False), `umid_to_pt()`, the trk-table helpers, the iXML HISTORY reader, and the full field aggregator against synthetic-but-realistic metadata. Full suite now at 166 passing tests.
- Not yet done (next stage, pending its own discussion before implementing): the actual Rename mode UI — a template field, token-insert buttons, and the planned deep Inspector integration (click a field in Inspector to insert its token) — plus wiring the execute path to actually write the `HISTORY/ORIGINAL_FILENAME` update after a rename.

## 260910.1759

- Fixed the previous release's Re-embed conflict fix landing at the wrong point in the pipeline: testing showed the embed step always ran first regardless of policy, and "Skip"/"Ask→Skip" only skipped the *rename* — the file still got metadata written into it either way, which the user correctly flagged as not matching "Skip" for the whole operation. Moved the conflict check to job-planning time instead (`build_reembed_jobs()` and `refresh_filename_mode()`'s Re-embed branch), the same point Split/Combine already decide theirs: a rename-after-embed target that already exists now marks the *entire* job — `SKIP EXISTS` (excluded from execution altogether, matching Split), `READY REPLACE`, or `CONFLICT: target exists` (asked about, before anything happens to the file) — so choosing Skip or Stop now means the file is never embedded either, not just left un-renamed.
- `finish_reembed_renames()` no longer does its own conflict resolution (that would have double-prompted "Ask" for the same file, once before embed and again at rename time) — it now trusts that a conflict reaching it was already decided: `READY REPLACE` proceeds and overwrites, anything else finding an unexpected existing final_target is a hard failure (a genuine race since Preview, or a duplicate target within the batch — not a normal, already-handled case).
- Replaced the previous release's `ReembedRenameConflictCharacterizationTests` (which tested conflict-handling inside `finish_reembed_renames()` — no longer where that decision happens) with coverage of the new job-planning-time behavior: `build_reembed_jobs()` marks the correct status per policy when the rename target already exists (and stays plain `READY` when it doesn't), and `finish_reembed_renames()` now only overwrites when the job already carries `READY REPLACE`, still hard-failing an unresolved conflict. Full suite now at 150 passing tests.
- Verified against the user's real test files (a 6-channel-derived Poly re-embed scenario with one channel's rename target pre-existing): `build_reembed_jobs()` now correctly produces `SKIP EXISTS` / `CONFLICT: target exists` / `READY REPLACE` for that one channel under the three policies respectively (with `job_is_actionable` excluding it entirely for Skip), while the other channels stay `READY` in all three cases.

## 260910.1740

- Fixed Re-embed's "If output exists" dropdown (flagged in the previous release as a known no-op) — it shares the setting with Split/Combine but `finish_reembed_renames()`, which decides what happens when a "rename after embed" target already exists, ignored it entirely and always hard-failed that file (`FAIL: rename target exists: ...`) no matter what was selected. It now actually applies the chosen policy to that one genuine conflict point Re-embed has (the *renamed* destination colliding with an unrelated existing file — the embed target itself always "exists" by definition, since it was found by scanning, so that was never the real conflict): "Skip existing" leaves the file embedded but un-renamed (`SKIPPED: rename target exists (...)`, no longer a failure); "Replace existing" lets the rename overwrite it; "Ask" shows the same Skip/Replace/Stop dialog Split/Combine now use, and "Stop" abandons every rename not yet performed in that batch (already-completed ones stay put).
- `finish_reembed_renames()` gained `conflict_policy`/`resolve_conflict` parameters (both optional, defaulting to the old "always skip" behavior) and `_resolve_conflict_decision()` now takes a plain `Path` instead of a `Job` so both the main execute loop and this rename pass can share the same interactive-prompt/apply-to-all machinery.
- Added regression coverage: Skip leaves the file embedded-but-unrenamed without touching the pre-existing target, Replace overwrites it, Ask routes through a supplied decision function, and choosing Stop mid-batch cancels every remaining pending rename (not just the one that conflicted) without touching files past that point. Full suite now at 150 passing tests.

## 260910.1731

- Replaced the "Stop on conflict" policy (Split/Combine's "If output exists" setting) with "Ask", at the user's request after testing confirmed "Stop" didn't actually stop anything — it only marked the one conflicting file as `BLOCKED: target exists` while every other file in the batch executed normally, functionally identical to "Skip existing" apart from the label. "Ask" now does what the name says: a conflicting file is marked `CONFLICT: target exists` at Preview time (still excluded from the plain READY count, since it's not decided yet), and at Execute time, hitting one pops a Finder-style modal — **Skip** / **Replace** / **Stop**, plus an "Apply this choice to all remaining conflicts in this run" checkbox. Checking the box and picking Skip or Replace answers every later conflict in the same run automatically; leaving it unchecked asks again for each one. **Stop** immediately halts everything not yet processed (already-written files stay as they are — this can't be undone), matching what "Stop" actually implied all along.
- Implementation reuses the same background-thread-waits-on-an-Event pattern already used for Pause/Resume: the execute worker thread blocks in a new `_resolve_conflict_decision()` while the main thread shows the dialog (`_prompt_conflict_decision()`) and hands the answer back — architecturally consistent with the rest of the app's cooperative pause/cancel machinery.
- `job_is_executable()` (which jobs Preview counts as immediately ready) is unchanged; a new `job_is_actionable()` additionally includes conflict-pending jobs so they still reach the Execute worker to actually be asked about, instead of being silently excluded the way a truly-blocked job is. `split_conflict_status()` returns `CONFLICT: target exists` for the "Ask" policy instead of the old `BLOCKED: target exists`.
- Verified job-planning against a real 6-channel Poly file with one channel's Target output pre-existing (`build_split_jobs(..., "Ask")` correctly marks exactly that one channel `CONFLICT: target exists` and leaves the other five `READY`, and `job_is_actionable` correctly includes the conflict job while `job_is_ready`/`job_is_executable` correctly exclude it).
- Added regression coverage for `split_conflict_status()` across all three policies (including the new Ask/CONFLICT case) and for `job_is_actionable()`/`job_needs_conflict_decision()` (a conflict job is actionable-but-not-ready, a ready job is actionable, a permanently-blocked job is not, and a target/source-less job is never actionable regardless of status). Full suite now at 146 passing tests. The interactive dialog/thread-handoff itself isn't unit-tested — like Pause/Cancel, it needs real interaction to verify.
- Noted for later: the Re-embed panel has its own "If output exists" dropdown sharing the same setting, but `build_reembed_jobs()` doesn't actually consume it — Re-embed writes metadata into files it already matched by scanning Target, so there's no "does this output path already exist" case for it to apply to. Flagged to the user as pre-existing, unrelated to this change; not touched here.

## 260910.1649

- Fixed Target field typing lag on large job lists (the user hit this on 11,052 jobs), confirming their own hunch that Target shouldn't refresh in true real time. The previous fix (`260910.1639`) made Split/Combine/Rename recompute output paths live off Target's text — correct, but wiring it to fire on every keystroke (same as the settings toggles) meant every character typed rebuilt the entire preview table, which is expensive at that scale and made typing visibly stutter. Target no longer live-refreshes per keystroke: it now updates once editing actually settles — on leaving the field (Tab/click away), pressing Return, using Select Folder, or a drag-and-drop drop — all of which already only fire once per change anyway, so nothing about the live-update behavior itself is lost, just how often it recomputes while you're still typing. Execute itself is unaffected either way, since it already calls `refresh_filename_mode()` synchronously before running.
- No new regression coverage — this only changes which UI events call the existing (already-tested) refresh/clear logic, not the logic itself.

## 260910.1639

- Fixed a regression from the previous release's Target-staleness safety net: in Split and Combine mode, editing Target after a (potentially very expensive — the user hit this on a 1,794-file scan) Preview now wiped the entire Preview list, forcing a full re-scan of Source just to pick a destination folder. Root cause: `refresh_filename_mode()`'s Split and Combine branches build target paths from the *cached* `current_output_root` rather than the live Target text, so the previous fix had to treat any Target edit as unrecoverable staleness and clear the list — but for these two modes, Target never actually needs a rescan, since neither builds its job list from Target's contents (only Re-embed does — it scans Target to find existing render files to match against Source, which genuinely can't be done without rescanning). Both branches now resolve Target live (the same way Rename already did) and keep `current_output_root` in sync, so editing Target after Preview updates the planned output paths immediately and cheaply — no data loss, no re-scan, and Execute always acts on the Target actually shown. The stale-clear safety net is now scoped to Re-embed only, where it's still correct and necessary.
- No new regression coverage needed beyond the previous release's `target_path_is_stale()` tests — this is a caller-scoping change (which modes call the clear at all) plus routing Split/Combine's existing target-path computation through the live Target text instead of the cached one, not new pure logic.

## 260910.1530

- Extended the previous release's Source-staleness safety net to Target, at the user's request — checked each mode's `refresh_filename_mode()` and found Split, Combine, and Re-embed all build/execute jobs against the *cached* `current_output_root` from the last Preview, not the live Target text (only Rename already re-resolves Target live, since it doesn't need to rescan Source just to change where renamed copies land). So in those three modes, editing Target without clicking Preview again had the same silent-stale-execute risk as the Source case. Editing Target now clears the Preview list in Split/Combine/Re-embed the same way (with a matching status message); Rename is deliberately exempted since clearing there would undo its existing live-refresh-on-Target-edit behavior.
- Added regression coverage for the new `target_path_is_stale()` (matches the jobs' output root, matches the blank-Target synthetic-fallback case, a genuinely different Target, and a relative Target resolved against Source's parent the same way `preview()` does). Full suite now at 142 passing tests.

## 260910.1437

- Fixed Poly ↔ Split QC's collapsed BLOCKED row (one Split candidate can't yield a Recorder Channel) dropping the actual Path of the file that failed — the row's `split_file`/`split_path` were left empty (`—`/`None`), so the message read as if the *Poly* file itself was the problem (its name was the only one still visible on the row) even though the message text named a different, similarly-named Split file, and there was no real path for the new right-click "Reveal in Finder" to open. Confirmed against the user's real repro: `S2-1T01.wav` (Poly) reported "Recorder Ch metadata unavailable: S2-1T01.A2.wav" with nothing to reveal; verified with `bwfmetaedit` that the Split candidate genuinely has no readable iXML (the user had intentionally overwritten samples in it via RX). `compare_poly_split_folders()` now keeps the actual Path of whichever Split candidate failed and puts it on the row (`split_file` shows its name instead of `—`), so the row is unambiguous and "Reveal Split File in Finder" now actually works for it.
- Fixed the QC panels' SOURCE/TARGET path label using a fixed `wraplength=420` instead of tracking its own cell's actual width — on a normal or narrow window the path was hard-clipped at the panel edge instead of wrapping to a second line (visible in every QC screenshot this session). Extracted the Inspector's existing tolerance-guarded dynamic-wraplength fix (`260909.2014`) into a shared `resize_wrapped_label()` in `ui_components.py`; `QcInputPicker`'s path label now uses it too, so the full path is always reachable by wrapping instead of being cut off. (The other four modes' Source/Target fields are plain `Entry` boxes, not wrapped labels — already fully readable via click + Home/End/select-all — so they're unchanged for now.)
- Fixed Execute silently acting on a stale Source folder when the Source field is changed (typed, dropped, or picked) after a Preview list already exists, without clicking Preview again — confirmed by the user's real repro: loaded a large ISO Poly folder in Split mode, switched Source to a different folder, clicked Execute without re-Previewing, and it processed the *original* folder because `self.jobs`/`self.current_source_root` are only ever rebuilt by an explicit Preview click. The Source field's change now clears the (now-stale) Preview list and disables Execute as soon as Source no longer matches the folder the list was actually built from, with a status message explaining why — forcing a fresh Preview instead of silently executing against files no longer shown on screen. (Scoped to Source only for now: Target's cached-vs-live handling differs by mode — e.g. Rename mode already live-refreshes off the *current* Target text — so widening this to Target needs a per-mode look rather than the same blanket rule.)
- Added regression coverage: the BLOCKED row now carries the failed Split candidate's real path, the QC path label rewraps the same tolerance-guarded way as the Inspector's, and the new stale-Source detection (still valid with no jobs yet, blank field, matching path, and a genuinely different path). Full suite now at 138 passing tests.

## 260909.2050

- Added a right-click "Reveal in Finder" context menu to all three QC tables (File ↔ File, Folder ↔ Folder, Poly ↔ Split), at the user's request — until now the Audio Inspector was the only way to jump from a problem row to the actual files, which only helped for metadata problems (Inspector shows both sides' fields side by side); an Audio Result problem had no equivalent one-click way to get to the file itself. Right-clicking a row now opens a menu with "Reveal Source/Target in Finder" (or "Reveal Poly File/Split File in Finder" for the Poly panel), each disabled when that row has no path on that side (e.g. a MISSING/EXTRA row only has one). Bound to `<Button-2>`, `<Button-3>`, and `<Control-Button-1>` together so it works regardless of how the user's mouse/trackpad sends a right-click on macOS.
- No regression suite changes — this is thin Tk/Finder wiring (an `open -R` subprocess call and a context menu) with no independently testable logic branch beyond what real interactive use verifies.

## 260909.2035

- Fixed both QC panels' "Update Metadata to Selected" button staying enabled the moment any metadata compare had run, regardless of whether a row was actually selected in the table — confirmed by the user with a real all-PASS Poly ↔ Split result where the button was still clickable with nothing selected, which only ever led to a "Select one or more rows first." dialog. `_refresh_qc_folder_summary()`/`_refresh_qc_poly_summary()` now cache whether metadata was compared at all (`_qc_folder_has_metadata`/`_qc_poly_has_metadata`), and a new shared `qc_update_selected_button_enabled(has_metadata, has_selection)` combines that with the table's *current* selection; the two tables' `<<TreeviewSelect>>` handlers (`_on_qc_folder_select`/`_on_qc_poly_select`) now also re-sync the button live as the selection changes, so it only ever lights up once there's actually a selected row it could act on.
- Added regression coverage for the new combining rule (disabled with no selection even if metadata was compared, disabled if metadata was never compared, enabled only once both are true). Full suite now at 132 passing tests.

## 260909.2014

- Fixed a real app freeze reported by the user with a screen recording: the Audio Inspector window became completely unresponsive to clicks while its TARGET header appeared to "shake." Diagnosed by extracting the recording's frames — the TARGET path label's line-wrapping (1 line vs. 2 lines) and the apparent Track List row count below it were flipping back and forth every ~100ms with no corresponding user action (only a mouse hover, no click, no new QC run). Root cause: `AudioInspector._resize_path_label()` recomputes the path label's `wraplength` from the *live* pixel width reported by its own container's `<Configure>` event, and unconditionally re-applies it on every firing. Re-wrapping the label changes its requested height, which perturbs the frame's own geometry negotiation by a couple of pixels, which re-fires `<Configure>`, which recomputes wraplength again — right at the boundary where the path text flips between fitting on one line and needing two, this never converges. The resulting flood of layout passes pinned the Tk main loop (blocking all click handling) and shifted everything below the header up/down each cycle, which is what made the (unchanged) Track List content below appear to gain/lose a row in the recording. Fixed by ignoring width changes smaller than 8px in `_resize_path_label` — small enough to still track any real window resize immediately, large enough to absorb the geometry-negotiation jitter that was driving the loop.
- Added regression coverage: a real width change still updates the wraplength, a sub-8px jitter is now ignored (the specific case that was looping forever), and a resize that starts small but is genuinely large still applies. Full suite now at 129 passing tests.

## 260909.1942

- Added `requirements.txt` (`tkinterdnd2`, `openpyxl` — the only two third-party Python packages this app needs; everything else is standard library) so running the raw `.py` script elsewhere is a single `pip3 install -r requirements.txt` instead of guessing.
- Added a friendly startup check for those same two packages when running the raw script without them installed — previously a missing package crashed with a raw Python traceback before any window could even appear; now a plain message box explains what's missing and the exact `pip3 install` command to fix it, then exits cleanly.
- Confirmed (rebuilt the app fresh via the project's own Build script and launched it directly) that a PyInstaller-built `.app` already bundles both packages correctly with zero `.spec` file changes needed — both ship their own PyInstaller hook (`hook-tkinterdnd2.py`, `hook-openpyxl.py`, from `pyinstaller-hooks-contrib`, already present in this environment), verified via `PyInstaller.utils.cliutils.archive_viewer` (openpyxl's full module tree is in the built `PYZ` archive) and via `find`/`du` (tkinterdnd2's native `tkdnd` Tcl/dylib files are in `Contents/Frameworks`). So the startup check above only ever matters for someone running the `.py` file directly — a distributed, pre-built `.app` never hits it.

## 260909.1931

- Fixed Settings' "Check Tools" reporting SoX/Soxi as "OK" with a real-looking version number even when pointed at a completely unrelated file (confirmed with the user's own test: an arbitrary WAV file). Root cause: `sox_version()` falls back to `brew list --versions sox` whenever the tool's own output doesn't look like a version string — but it did this unconditionally, including when `tool_version()` had already reported the executable failed to run at all (wrong file entirely, exec format error, permission denied). That fallback then reported *the system's actual installed Homebrew SoX version*, as if it had verified the user's chosen path, when it had verified nothing — a bogus path could silently masquerade as a working tool right up until it was actually used in a real batch. `sox_version()` now returns a real execution failure as-is instead of falling through to the Homebrew lookup; the fallback still applies for the case it was actually meant for (a real tool whose version-string format isn't recognized). `check_tools()`'s OK/MISSING label is also now based on whether the tool actually ran (not just whether some file exists at that path).
- Verified against the user's exact reproduction: SoX/Soxi/BWF MetaEdit pointed at a WAV file now all correctly report MISSING with the real "Exec format error", instead of two of the three silently reporting OK.
- Added regression coverage: a genuine execution failure is returned unchanged rather than masked by the Homebrew fallback, and the fallback still works for real tools with unparsed output. Full suite now at 143 passing tests.

## 260909.1920

- Removed the duplicate "Inspector" button from the Poly ↔ Split panel — it existed only because that panel's own row-selection binding could only *update* an already-open Inspector, never open one. The shared "Audio Inspector" button in the top toolbar now does both: it opens (or refocuses) the Inspector and immediately loads whatever row is selected in whichever QC sub-type is currently showing (File ↔ File, Folder ↔ Folder, or Poly ↔ Split), via a new shared `_load_selected_qc_row_in_inspector()` — so there is exactly one Inspector button in the app again, not a QC-only copy of it.
- Fixed QC's Source/Target starting empty every time QC is opened, even right after typing a path into Split/Re-embed/Rename/Combine — those four modes already share one Source/Target between themselves, so landing on QC's own separate, always-blank fields after already setting one up felt like redundant re-entry. The first time QC is reached with its own fields still unset, they're now pre-filled from whatever Source/Target the other four modes currently hold (only when those resolve to real, existing folders). QC keeps its own independent fields from that point on — picking something different for QC (or a single file, which the other four modes can't point at) is never overwritten by revisiting QC again, verified directly: pre-fills once, then holds a user's own later QC-specific choice across mode switches.
- Verified against the user's real "orphan channel" test file (a Split file's Recorder Channel metadata pointed at a channel absent from its Poly parent): Poly ↔ Split QC correctly reports it as `EXTRA` rather than forcing a wrong match.

## 260909.1704

- Added Poly ↔ Mono awareness to File ↔ File's Audio Content check, at the user's request. Loading a whole Poly recording as Source and one already-split mono channel of it as Target directly (via Load WAV, not through Poly ↔ Split's folder pickers) always read as "Different" — every other property matched, but `compare_wav_pcm_content()` did a literal whole-file byte comparison, and a poly file's interleaved multi-channel bytes can never equal a mono file's bytes even when the content is genuinely identical. `compare_basic_audio()` now checks the two files' channel counts: when they're equal it behaves exactly as before (whole-file byte comparison); when exactly one side is mono and the other is multi-channel, it instead extracts the one channel of the Poly file that actually corresponds to the mono file's real Recorder Channel (the same metadata-driven approach Poly ↔ Split QC already uses) via the new `compare_poly_mono_content()`, and compares just that. If the channel counts differ but neither side is mono (e.g. 3ch vs 5ch), there's no sensible per-channel mapping to attempt, so it's reported as `BLOCKED` rather than silently guessing.
- Verified against the real files that prompted this: a Poly file vs. its own split channel 3 now correctly reads "Audio Content: Identical" (previously "Different"); a same-channel-count File ↔ File pair still uses the unchanged whole-file comparison.
- `compare_basic_audio()` gained a required `bwfmetaedit` parameter (needed to read the Recorder Channel / Poly track mapping for the new check) — both call sites (`compare_qc_folders()`, `build_file_qc_comparison()`) already had it in scope.
- Added regression coverage: the new poly/mono channel-matching helper (correct stream selection, and a clear error when no channel matches), `compare_basic_audio()` routing to it for a poly/mono pair, and the BLOCKED outcome when channel counts differ with neither side mono. Full suite now at 141 passing tests.

## 260909.1541

- Fixed Serial ID's "Metadata Tape + TC" TC column showing a raw sample count (e.g. `1263072001`) instead of SMPTE (`hh:mm:ss:ff`) for some recorders while others displayed correctly, confirmed against the real test batch. Root causes, both in the SMPTE-conversion input readers, not the conversion itself:
  - `read_serial_speed()` read iXML's `TIMECODE_RATE` with a bare `float()` — but iXML always writes that field as a rational "N/D" string (`24/1`, `30000/1001` for 29.97), which `float()` can never parse, so it raised and fell straight to the Description-text fallback for every recorder, valid iXML value or not. Added `parse_timecode_rate()` to actually parse the rational form.
  - That Description-text fallback (for both speed and Tape/Scene/Take) only recognized `$` and `s` prefixed keys (Sound Devices' convention) — Aaton Cantar's `a` prefix and a `d`-prefixed normalized form seen elsewhere in this project's test set fell through it too. Widened the shared prefix pattern to `[$sda]`.
  - `read_serial_sample_rate()` had no fallback beyond iXML fields and a CodingHistory `F=` value — some recorders (this project's MixPre-6 II test files) write neither. Added a last-resort fallback to the WAV file's own `fmt` chunk sample rate, which is always correct regardless of what any metadata field says.
  Verified against all 69 files in the real test batch: zero files still show a raw sample count.
- Fixed the exported processing log (.tsv) not recording which settings actually produced its results — reopening an old log gave no way to tell what formula/toggle combination was active for that run. `_save_processing_log()` now writes a `#`-prefixed settings summary (mode, Source/Target, and every mode-specific formula/toggle — Add Text/Change $/Find & Replace/Serial ID for Rename, Source+Target formula/rename-after-embed for Re-embed, etc.) before the header row.
- Fixed the main preview table's Status column snapping back to its default width instead of holding a manual resize — it was the table's designated stretch column, so its width kept getting recomputed to "whatever's left over" on every layout pass (the same class of bug already fixed for the QC tables). Target (the path column, whose content varies far more in length) is the stretch column now instead; Status's own default width was also widened so the common "Ready to X, Y, and Z" phrasing fits without truncation immediately.
- Added regression coverage for the rational TIMECODE_RATE parser, the widened Description-prefix fallback (Aaton `a` / normalized `d`), and the WAV-fmt-chunk sample-rate fallback. Full suite now at 137 passing tests.

## 260909.1430

- Added a metadata-identity fallback to Re-embed matching, closing the Pro Tools case from last version where "66-1-1"/"S10-13-002" couldn't pair against Pro Tools' consolidated renders ("66-1-01"/"S10-13-02") even though the real Recorder Channel matched fine. Pro Tools rewrites the digits inside a Target's own base name during consolidate/render — no regex on `{base}` can recover from that since the two sides' extracted base strings are genuinely different text. Verified directly against real files that Tape, Scene, Take, OriginationDate, and OriginationTime all survive that rewrite completely untouched, across Pro Tools *and* both REAPER render variants ("stream" and "trk") this project's test set covers — Nuendo not yet tested. `read_reembed_identity()` reads that five-field tuple (returning None if any field is blank, so an all-blank tuple can't spuriously "match" unrelated files); `build_reembed_jobs()` now gives every Source stuck at NO TARGET one more chance against whatever Targets are still unclaimed (NO SOURCE), pairing on that identity tuple plus the real Recorder Channel instead of the filename. If more than one unclaimed Target shares the same tuple + channel, the Source is marked `AMBIGUOUS TARGET (metadata match)` rather than guessed at — this only ever activates for pairs the filename-formula match already failed to make, so it can't override or interfere with a real base-name match.
- Verified against the real Pro Tools batch: all 21 previously-orphaned files across "66-1-1" and "S10-13-002" now pair correctly (including one where the Target's own `.A#` number no longer matches the Source's at all — `66-1-1.A6.wav` → `66-1-01.A7-AtAlgnP_01.wav` — confirming the match is driven entirely by metadata, not filename numbers). No regression on the REAPER-rendered folders, which continue to match via the existing base+channel path without ever touching this fallback.
- Added regression coverage: a rewritten base name still pairs via the metadata-identity fallback (including a correct `final_target` when Rename-after-embed is on), and a tie between two unclaimed Targets sharing the same identity+channel is reported as ambiguous rather than guessed. Full suite now at 133 passing tests.

## 260909.1413

- Fixed the Audio Inspector's "iXML - Track List" section showing "No Track List metadata" for files whose iXML uses the flattened `TRACK_LIST_TRACK_CHANNEL_INDEX` shape (e.g. this REAPER-rendered test batch, confirmed against the real "Recorder Rendered Reaper stream original" files) — even though `split_recorder_channel()` already has a fallback for exactly this shape and correctly determines the file's real channel from it. This was the actual bug behind the "Inspector shows no way to tell Trk# but the script clearly uses it" report from last version — that report was investigated against the wrong folder (an already-Re-embedded one, which happened to have a proper nested TRACK_LIST) and incorrectly closed as a non-issue. `ixml_track_rows()` (which backs that section's Stream/Recorder Ch/Name table) now falls back to reading the flattened `TRACK_LIST_TRACK_CHANNEL_INDEX`/`_INTERLEAVE_INDEX`/`_NAME` elements (with their `_2`, `_3`... suffixes for additional tracks) the same way `split_recorder_channel()` already does, so the Inspector and the actual matching logic now agree on every file. Verified against all 49 files in the real reported folder: every row now matches `split_recorder_channel()` exactly.
- Added regression coverage for the flattened-shape fallback (single track, and multiple tracks ordered by their numeric suffix) alongside the existing nested-shape case. Full suite now at 131 passing tests.

## 260909.1401

- Fixed the four Rename cards (Add Text, Change $, Find & Replace, Serial ID) and Re-embed's "Rename matched targets after embed" card disabling every field inside them whenever their own checkbox was off — you had to turn a card on just to type into it, then could turn it back off without losing what you typed. `_toggleable_labelframe()` no longer disables its body's widgets based on the checkbox; the checkbox now only controls whether that card's action is applied to jobs, not whether it can be configured. Removed the now-unused `_set_row_state()`/`_all_descendants()` helpers.
- Fixed the processing log (.tsv, saved automatically after Execute Ready Jobs) silently omitting every job that wasn't actually executed — NO TARGET, BLOCKED, AMBIGUOUS TARGET, CANCELLED, etc. Only Combine mode built a full "every previewed job, ready or not" row list before this; Split, Re-embed, and Rename only ever saw the already-filtered ready subset, so a batch with unmatched files produced a log that looked like a smaller, fully-successful run — with no way to tell from the log alone that anything had been skipped. `execute()` now builds this full row list (using the same `display_status()` the live Preview table already shows) for every mode, and `_execute_worker()` merges it in correctly for both its code paths — the general loop (mutates the ready subset's rows in place, same as Combine already did) and Rename's stepwise path (prepends only the blocked/skipped rows, since its own result already covers the ready ones — avoiding duplicating them).
- Added reordering to the Filename Formula Presets list in Settings ("Move Up" / "Move Down" buttons) so the order shown in every formula dropdown (Split, Re-embed, QC) is user-controlled instead of fixed to insertion order.
- Investigated a report that the Audio Inspector shows no way to tell a file's Trk# from its displayed metadata, despite Re-embed/QC clearly using it. Scanned every file in the reported REAPER-rendered test folder: `ixml_track_rows()` (which backs the "iXML - Track List" section's Stream/Recorder Ch/Name table) returned the exact same channel every `split_recorder_channel()`-based match relies on, for every single file — the Inspector does show it, in that section's table specifically, not as plain text elsewhere. No code change; flagged so the report can be closed if a further screenshot doesn't show a genuine gap.
- Investigated a report that Re-embed couldn't match some Pro Tools–rendered files ("66-1-1" / "S10-13-002") that a REAPER-rendered batch matched fine. Confirmed against the real files: Pro Tools' render/consolidate step rewrites the take-number digits inside the base name itself (`66-1-1` → `66-1-01`, `S10-13-002` → `S10-13-02`), which breaks the exact-string `{base}` match Re-embed's pairing key depends on (pairing is `{base}` string match + real Recorder Channel — the channel half already tolerates a mismatched filename number, but the base-string half cannot tolerate Pro Tools changing the number's digit count). Not an app bug — no formula can generically undo an inconsistent, unpredictable digit-padding change — documented for future reference if a "Trk Name" / smarter base-matching feature is designed.

## 260909.0153

- Fixed QC auto-detect blocking with "Base Name Formula must contain exactly one {base} and one $ placeholder" whenever a Poly ↔ Split comparison's Poly-side folder had a Base Formula without a `$` (e.g. plain `{base}`, since whole poly recorder files have no per-channel number to describe). `detect_qc_inputs()` used to compile both folders' formulas unconditionally up front — but a formula is only ever needed to parse a *mono* file's base name, and an all-poly folder never has one, so a formula that's irrelevant there was still blocking detection outright. Each folder's formula is now compiled lazily, the first time (and only if) a mono file actually shows up in it — a pure-poly folder's formula is never touched regardless of its content, while a folder that genuinely has mono/split files to match still raises the same clear error as before if its formula is broken.
- This only clears the crash — matching the real Poly ↔ Split pair still needs the Target (Split) folder's Base Formula to actually describe its filenames. Confirmed against the user's real test folders (`Recorder Poly` / `Split TEST`): the Split files are named `<base>_TEST.A#.wav`, so the Target formula must be `{base}_TEST.A$`, not just `{base}.A$` — otherwise `{base}` (being a greedy match-anything) silently swallows `_TEST` into the extracted base, and it never matches the Poly file's actual stem.
- Added regression coverage: an all-poly folder's unusable formula no longer blocks detection, while a folder that genuinely has mono files with a broken formula still raises the same error. Full suite now at 128 passing tests.

## 260909.0142

- Fixed Folder ↔ Folder QC going completely silent for several seconds after "Comparing 389/389" and before the final "Detected Folder ↔ Folder | RESULT: ..." line, with nothing in the log explaining the wait. Cause: with Metadata checked, `compare_qc_folders()` runs the Audio pass to completion (reporting "Comparing" progress the whole way), then calls `augment_qc_rows_with_metadata()` — which does two more bwfmetaedit reads per file — with no progress callback at all. On the real 389-file batch this second pass alone took ~7.5s of its own, on top of the Audio pass's ~9.6s, entirely unreported. `augment_qc_rows_with_metadata()` now takes an optional `progress(stage, current, total)` callback and reports "Comparing Metadata" the same way the Audio pass reports "Comparing" — `compare_qc_folders()` threads its own `progress` through automatically, so this shows up in the Execution log with no other changes needed (the log line format and 100-row throttling were already stage-name-agnostic). Poly ↔ Split's own call to the same function is unaffected (its progress callback has a different, counter-based shape not easily adapted here, and its batches are typically much smaller than a 389-file Folder run) — left as `progress=None`, its prior behavior, pending a real report of the same silent-gap symptom there.
- Verified against the real 389-file batch: the log now reads Scanning Source/Target → Comparing 1..389 → **Comparing Metadata 1..389** → Detected result, accounting for the full ~17s instead of leaving the last ~7.5s unexplained.
- Added regression coverage for the new progress callback (fires once per row, in order, with the correct stage name and total). Full suite now at 126 passing tests.

## 260909.0134

- Fixed the Inspector's "stay on the same section" scrolling (260909.0125) landing on the wrong section when the viewport was scrolled partway into a long one's body rather than sitting right at its heading. `_heading_at_top()` previously picked whichever section's row the top edge had scrolled *into* — but by the time you're partway down a long section (its own heading already off-screen above the edge), what you're actually looking at is the *next* section coming into view, not the one you've scrolled past. It now returns the first section whose row starts at or below the top edge instead of the last one whose row starts at or above it — landing on the next section down when scrolled into the middle of one, and still landing on the current section exactly when the edge sits right at its heading. Verified with a synthetic long section: scrolled 70% through it, `_heading_at_top()` now correctly reports the section after it instead of the one being scrolled through.

## 260909.0125

- Fixed the Audio Inspector visibly double-flickering when selecting a File ↔ File or Folder ↔ Folder QC result row, compared to a single mild flicker everywhere else (Split/Rename/Combine's Source↔Target, and Poly ↔ Split QC). Cause: `_load_qc_pair_into_inspector()` (added earlier this session to fix the Inspector not updating on QC row selection) called `load_inspector_source()` and `load_inspector_target()` independently — each queues its own background load and, on arrival, tears down and rebuilds the *entire* comparison canvas on its own, so one row click meant two full rebuilds back to back. Every other caller (`load_job`, `load_qc_mapping`) already builds both sides together and renders once. Added `AudioInspector.load_pair()` (a new `InspectorContext` kind, "pair") that fetches Source and Target metadata together in one background pass and renders once, and switched `_load_qc_pair_into_inspector()` to use it. Verified live: one QC row selection now triggers exactly one `_render_comparison()` call, down from two.
- Added "stay on the same section" scrolling to the Inspector, at the user's request, so switching between files (in any mode — this is the one shared render path behind all of them) keeps the same *section* in view instead of the same raw scroll percentage. Previously `_render_comparison()` saved `canvas.yview()[0]` (a 0.0–1.0 fraction of total height) and reapplied it verbatim after rebuilding with a different file's content — since section sizes vary file to file (Description length, iXML field count, Track List entries), the same fraction could land in an unrelated section. It now records which section heading the viewport is scrolled into before rebuilding (`_heading_at_top()`), and afterward scrolls back to that same heading's row (`_scroll_to_heading()`) — or, if the new file doesn't have that section (e.g. no Track List), walks backward to the nearest earlier section the new file *does* have (`_nearest_available_heading()`), rather than resetting to the top. `reset_scroll=True` callers (a freshly opened Inspector, a brand-new single-file load) are unaffected and still jump to the top as before.
- Verified live against real files: scrolling to "BEXT / CORE" on one QC pair and switching to a different pair correctly lands back on "BEXT / CORE" rather than an arbitrary offset.
- Added regression coverage for the nearest-available-heading fallback (exact match kept, falls back to the closest earlier section that still exists, falls back further when several are missing, returns to the top when nothing matches). Full suite now at 125 passing tests.

## 260909.0102

- Fixed "Update Metadata to Selected/Problems" (Folder ↔ Folder and Poly ↔ Split) re-running the entire folder comparison after writing — for a 389-file batch this re-scanned and re-compared every file just to reflect 3 real writes, taking ~10s and making an untouched, already-passing batch look like it had just been re-verified from scratch. The write itself was already correctly scoped to just the selected/problem targets (confirmed with the user: the confirmation dialog's file count was correct); only the post-write refresh was wasteful. An Update action only ever rewrites BEXT/iXML on the Target (or Split) file — it never touches the audio/data chunk — so Audio Result can't have changed by it; only Metadata Result needs re-checking. `_update_qc_folder_metadata_worker`/`_update_qc_poly_metadata_worker` now re-run just `compare_qc_metadata_fields()` for each file they actually wrote to, and new `_apply_qc_folder_row_refresh()`/`_apply_qc_poly_row_refresh()` update only those rows' Metadata Result in place (Treeview cell + cached row dict), then recompute the RESULT summary row and the Update buttons' enabled state from the full row set already held in memory — no folder rescan, no re-running Audio comparison on files nothing touched.
- Extracted `_refresh_qc_folder_summary()` / `_refresh_qc_poly_summary()` from what was duplicated tail logic in `_show_qc_folder_results()` / `_show_qc_poly_results()`, now shared by both the initial Compare render and the new in-place post-update refresh.
- Verified live: updating 1 of 5 rows now leaves the other 4 rows and the underlying comparison data completely untouched, the RESULT summary and button states recompute correctly, and `compare_qc_folders()` (the full rescan) is never invoked by the update path anymore.

## 260909.0053

- Fixed the editable formula-preset Comboboxes (Split/Re-embed's own formula fields, and QC's Base Formula fields) showing no visible text cursor while typing. The app's "clam" ttk theme leaves a Combobox's insert-cursor color effectively invisible against its white field — every plain `tk.Entry` in the app already sets `bg`/`fg`/`insertbackground` explicitly for the same reason, but that fix was never carried over when the formula fields were switched from `tk.Entry` to `ttk.Combobox` for the shared preset dropdown. Added a `Formula.TCombobox` ttk style (`fieldbackground`/`foreground`/`insertcolor`, all explicit) and applied it to both the shared `_formula_row()` combobox and `QcInputPicker`'s Base Formula comboboxes.
- Investigated a report that "Update Metadata to Selected" with exactly one Folder QC row selected appeared to update every file. Verified directly (selecting one row and calling the underlying `_collect_qc_folder_update_targets("selected")`) that only the one selected row's Source/Target pair is ever collected and written — the write path is not the source of this. The confirmation dialog before the write always states the exact file count ("Overwrite metadata on N file(s)..."), and both Update buttons finish by re-running the full Folder Compare to refresh the table, which can make an unrelated, already-passing batch look like "everything changed" if skimmed quickly. No code change made pending confirmation of which button was pressed and what the confirmation dialog reported, since the collection logic couldn't be made to reproduce the reported behavior.

## 260909.0044

- Reworked the QC report saved after Compare (introduced last version as a plain TSV) into a real per-field breakdown, at the user's request: instead of one collapsed AUDIO RESULT / METADATA RESULT column per file pair, the Folder ↔ Folder and Poly ↔ Split reports now carry every individual field — File Name, File Size, Sample Rate, Bit Depth, Channels, Frames, Duration, Audio Format, Audio Content, Tape, Scene, Take, Circled, Originator, Origination Date, Origination Time, TC (samples), Description — each as its own Source/Target column pair, matching the detail File ↔ File QC already showed for a single pair. `compare_qc_folders()` now caches the full `compare_basic_audio()` breakdown on each row (`audio_comparison`) instead of discarding everything but the pass/fail verdict, and `augment_qc_rows_with_metadata()` does the same for `compare_qc_metadata_fields()` (`metadata_comparison`) — so the report writer reuses data the compare pass already computed rather than re-running soxi/bwfmetaedit per file.
- Switched the report format from `.tsv` to `.xlsx` and dropped the ✓/≠ result column entirely — cell background color now carries the same information (green = match, red = mismatch, yellow = blocked), which the user asked for on top of the field breakdown above since a plain-text TSV can't carry cell formatting. This adds a new third-party dependency, `openpyxl` (`pip install openpyxl`), alongside the existing `tkinterdnd2` requirement — worth remembering before sharing the script with anyone else.
- The "Audio Content" row/column (added last version) now shows "Identical"/"Different" instead of a bare "—" placeholder, so the cell still means something on its own in a black-and-white printout or for a colorblind reader, not just via its fill color.
- Added regression coverage for the cached `audio_comparison`/`metadata_comparison` breakdowns, the fill-color classification (`qc_result_fill_key`), and the field-lookup/flattening helpers (`qc_detail_field_lookup`, `qc_report_detail_cells`) that back the new report. Full suite now at 122 passing tests.

## 260909.0023

- Fixed "Clear List" doing nothing while in QC mode (the Execution log said "Preview list cleared" but the on-screen QC table stayed populated). `clear_preview_list()` only ever cleared the Split/Rename/Combine preview table (`self.table`) — it had no idea QC mode even existed, since QC's three result tables (File/Folder/Poly ↔ Split) live in entirely separate `ttk.Treeview` widgets. It's now mode-aware: in QC mode it clears whichever of the three tables has content, plus the `qc_folder_rows`/`qc_poly_rows` lookup dicts and the Update Metadata buttons' enabled state, and logs "QC list cleared." instead of the misleading shared message. Verified live: inserting a row into `qc_folder_table` then calling Clear List now empties it and disables its Update Metadata buttons.
- Added real PCM sample comparison to Audio Result for File ↔ File and Folder ↔ Folder QC, closing the gap confirmed by the user's own test: silencing a block of samples in the middle of a real take (RX, format/duration/frame-count all left untouched) still read as Audio Result PASS, because `compare_basic_audio()` only ever compared soxi-derived properties (sample rate, bit depth, channels, frames, duration, format) — the same limitation already suspected and documented in 260908.2356 for the "Rendered Files test3" report, now independently reproduced and fixed. Added `compare_wav_pcm_content()` (byte-for-byte data-chunk comparison, chunked) to `operations/audio_qc.py`, and a new "Audio Content" row in `compare_basic_audio()` that runs it only once every other property already matches (a header mismatch already fails the pair, so the extra I/O would be wasted) — shown as "—" (not compared) when skipped, matching the existing "-" convention. Verified against the user's real RX-edited file: previously PASS, now correctly DIFFERENT with `Audio Content: ≠` while every other row still reads ✓. Per-pair overhead is negligible (~17ms including both soxi calls for a 7MB/24-bit file), so this runs automatically whenever Audio is checked rather than needing a separate slower toggle.
- Added a QC report export, matching the TSV processing log the Split/Rename/Combine/Re-embed modes already save automatically after Execute Ready Jobs. After the shared auto-detect Compare button finishes (File ↔ File, Folder ↔ Folder, or Poly ↔ Split), a Save dialog now opens for a `.tsv` report containing the full result — for Folder/Poly this includes columns the on-screen table doesn't show (full Source/Target or Poly/Split paths), not just the ✓/— presence marks. Status line and log now also record where the report was saved (or "not saved" if the dialog was cancelled).
- Added regression coverage for `compare_wav_pcm_content()` (a silenced sample block is detected; an identical file passes) and `compare_basic_audio()`'s new content-check gating (runs and catches a mismatch when headers agree; skipped and shown as "—" when they don't). Full suite now at 115 passing tests.

## 260908.2356

- Fixed Audio Result showing "PASS" on every row of the Folder ↔ Folder and Poly ↔ Split QC tables whenever the Audio checkbox was simply left unchecked — `compare_qc_folders()` and `compare_poly_split_qc()` both hard-coded `result = "PASS"` when `include_audio` was False, which is indistinguishable from a genuine pass in the same column. Both now report `"-"` (not compared) instead, matching the convention Metadata Result already uses when it isn't checked. `qc_rows_overall_result()` and `compare_poly_split_qc()`'s own per-file aggregate now treat `"-"` the same as `"PASS"` so an audio-skipped, metadata-only run still reads as an overall PASS rather than a false DIFFERENT.
- Investigated the related report that a REAPER-plugin-processed render ("Rendered Files test3") showed Audio Result PASS for every file even though the plugin should have altered every sample. Confirmed directly against the real files: `compare_basic_audio()` only ever compares soxi-derived properties (sample rate, bit depth, channels, frames, duration, format) — it has never done sample-level PCM comparison for File ↔ File / Folder ↔ Folder QC (only Poly ↔ Split QC's `compare_wav_pcm_stream()` does that). For this specific test3 pair the format actually did change (24-bit PCM → 32-bit float) and `compare_basic_audio()` correctly reports DIFFERENT when Audio is checked — the all-PASS result the report described matches the Audio checkbox being left off (as seen in this session's own screenshot of the same test run), which the "-" fix above now makes visually obvious instead of silently reading as a real pass.
- Fixed the QC column-width "springs back" bug persisting after the previous `configure_treeview_column_widths()` fix. Root cause: every `ttk.Treeview` has an implicit `"#0"` tree column that `show="headings"` only hides visually — it still keeps Tk's default `stretch=True` and default width, and still participates in ttk's layout pass, so it kept silently soaking up (or giving back) width slack that was meant for the table's one designated stretch column on every resize. `configure_treeview_column_widths()` now also pins `"#0"` to `width=0, minwidth=0, stretch=False` for every table it configures. Verified programmatically: a manually-resized non-stretch column now holds its exact width across both widening and narrowing the window, which it did not before this fix even with only one visible column marked `stretch=True`.
- Added regression coverage for the Audio Result "-" behavior on both `compare_qc_folders()` and `compare_poly_split_folders()`. Full suite now at 112 passing tests.

## 260908.2331

- Fixed the QC result tables (File ↔ File, Folder ↔ Folder, Poly ↔ Split) having the same "column width springs back" bug already fixed on the main Preview list — but never fixed there, because each of the 4 Treeviews had its own independently duplicated column-setup code, not a shared one. `qc_table` and `qc_poly_table` each had two columns marked `stretch=True` (the same root cause as before: ttk redistributes width across every stretch column on each layout pass, overriding a manual resize on any of them).
- Extracted `configure_treeview_column_widths()` — one shared function that sets exactly one stretch column per table — and now use it for the main Preview list and all three QC tables, so this bug class can't reappear piecemeal in only one of them again.
- Split Audio Result and Metadata Result into their own columns in the Folder ↔ Folder and Poly ↔ Split tables (File ↔ File already showed them as separate FILE/AUDIO/METADATA sections). Previously a metadata mismatch silently overwrote an already-passing audio result in the one shared `result` column, so e.g. a render with a correct bit-depth change but a broken TC showed the same bare "DIFFERENT" as a render with real audio corruption — now they read `AUDIO RESULT: DIFFERENT | METADATA RESULT: PASS` and the reverse, respectively. Added `qc_rows_overall_result()` (audio pass AND metadata pass, BLOCKED takes priority) shared by both comparison functions' aggregate result line.
- Verified against the field's real ISO/render folders: a batch with only a bit-depth/format change now reads Audio DIFFERENT / Metadata PASS, correctly distinct from the earlier TC=0 batch's Metadata DIFFERENT.
- Updated/added regression coverage for the non-mutating metadata augmentation and the new combined-result rule. Full suite now at 110 passing tests.

## 260908.2315

- Fixed the new `Update Metadata (Source → Target)` / `Update Metadata to Selected` / `Update Metadata to Problems` buttons being invisible on every QC type. Cause: each type's frame (File/Folder/Poly ↔ Split) has an `actions` row containing a per-type `Compare` button that's deliberately hidden at startup (`.pack_forget()`) since the always-visible auto-detect Compare supersedes it — I'd added the new buttons into that same row, so they were hidden right along with it, for both the auto-detect and manual-tab flows. Moved all Update Metadata buttons into their own row outside that hidden container, so they now show whenever their QC type is active.
- Fixed the pre-existing Poly ↔ Split `Inspector` button, which had the identical problem (also inside that hidden row) and so was never actually clickable in either flow — moved alongside the Update Metadata buttons.
- Fixed the Audio Inspector never updating when selecting a row in the File ↔ File or Folder ↔ Folder QC result tables — only Poly ↔ Split had a `<<TreeviewSelect>>` binding to push the selected pair into an already-open Inspector. Added the same binding for both, reusing the Inspector's existing `load_inspector_source`/`load_inspector_target` (the same calls its own Source/Target Load buttons use).
- Verified programmatically that all five buttons are actually mapped (`winfo_ismapped()`) for their respective QC type, and that all three result tables carry a `<<TreeviewSelect>>` binding.

## 260908.2256

- Fixed Folder ↔ Folder QC (both auto-detect and the manual Folder tab) ignoring the Source/Target Base Formula fields entirely — it only ever matched files by exact identical filename, so a Source ISO (`Scene.A1.wav`) and its differently-named render (`Scene.A1 render 001.wav`) always showed as MISSING/EXTRA even with both Base Formula fields set correctly, since nothing in `compare_qc_folders()` ever read them.
- `compare_qc_folders()` now does a second pass after exact-filename matching: for whatever's left unmatched on both sides, it pairs Source and Target files using each side's own Base Formula (`{base}` capture, regex allowed) plus recorder-channel metadata — the same matching approach Re-embed already uses. Exact-name matches still win first, so a plain folder-vs-folder sync with identical filenames behaves exactly as before.
- Matched-by-formula rows show both filenames (`source name ↔ target name`) so it's clear the pairing came from the formulas rather than an identical name.
- Verified against the field's real ISO-vs-rendered folders: every file now pairs correctly by recorder channel, and Metadata comparison caught a genuine problem in one render batch — `TC (samples)` reset to `0` on every Target file while everything else (Tape/Scene/Take/Originator/Description) matched Source correctly.
- Added regression coverage: formula-based pairing across differently-named files, no regression when formulas are blank, and exact-filename matches taking priority over the formula lookup. Full suite now at 109 passing tests.

## 260908.2249

- Fixed QC's auto-detect (and Poly ↔ Split) blocking with "Base Name Formula must contain exactly one {base} and one $ placeholder" whenever the Base Formula field held a regex-style formula such as `{base}.A$ render \d+` — one of the three shared formula presets added this session. `qc_split_formula_pattern()` had its own non-regex compiler that outright rejected any `\` in the formula, unlike Re-embed's Target formula field which explicitly supports that exact syntax. `detect_qc_inputs()` calls this on both Base Formula fields before it even determines File/Folder/Poly↔Split, so picking that preset broke type detection immediately, before Compare could do anything.
- `qc_split_formula_pattern()` now delegates to the already-regex-capable `formula_pattern(..., allow_regex=True)`, so it accepts the same formulas Re-embed's Target formula does. Added `label` and `number_group` parameters to `formula_pattern()` (both default to prior behavior) so QC keeps its own error label ("Base Name Formula") and capture-group name ("stream") — existing callers and tests are unaffected.
- Removed the now-unused `compile_filename_pattern`/`validate_filename_placeholders` imports from the main module.

## 260908.2211

- Added Metadata comparison to QC, alongside the existing Audio comparison — previously QC only ever compared soxi-derived audio properties (sample rate, bit depth, channels, frames, duration, format); there was no metadata comparison at all.
- Added `[Audio]` `[Metadata]` checkboxes next to Compare (both on by default) controlling every QC comparison path — the auto-detect flow and the three manual File / Folder / Poly↔Split tabs, since they all share the same underlying comparison functions and result tables.
- Metadata fields compared: Tape, Scene, Take, Circled, Originator, Origination Date, Origination Time, TC (raw BWF sample count), Description — the same identity fields already used by Rename's Serial ID and the Inspector, read via the same iXML → Core XML → Description fallback chain.
- Poly ↔ Split reuses the existing recorder-channel auto-matching (`split_recorder_channel`, including this session's flattened-iXML fallback) to pair each Poly channel with its Split file for the metadata check — no separate matching logic needed.
- Added `Update Metadata (Source → Target)` (File view, single pair) and `Update Metadata to Selected` / `Update Metadata to Problems` (Folder and Poly ↔ Split views, multi-row) — QC's first write action. Confirms with a dialog stating how many files will be overwritten before doing anything. Direction is always Source → Target, and it's a full replace (BEXT chunk copied whole, LIST chunk cleared, iXML rewritten — the same primitives Re-embed's own metadata-embed step already uses), not a per-field patch. Poly → Split slices the correct single-channel iXML via the existing `mono_ixml_from_poly_slot`.
- Buttons stay disabled until a comparison actually finds a metadata problem to act on; after updating, the affected view automatically re-compares so the table reflects the real post-update state rather than an assumed one.
- Verified the full detect → fix → re-verify cycle against real files from the field report (copies, not the originals): deliberately mismatched one BEXT field, confirmed QC reported `DIFFERENT`, ran the update, confirmed it now reads `PASS`.
- Added regression coverage for the new comparison/update primitives (field-level diffing, toggle behavior, BEXT+iXML copy sequencing, Poly channel-slice selection). Full suite now at 106 passing tests.

## 260908.2024

- Fixed Preview list column widths not sticking when dragged (or snapping to a nearby value instead of what was dragged to). Root cause: `source`, `target`/`Before Rename`, `Planned Filename`, and `Status` were all marked `stretch=True`, so Tk's Treeview redistributes total width across every stretch column on each layout pass and silently overrides a manual resize on any of them except by coincidence. Only the trailing `Status` column now stretches to absorb leftover window width; every other column keeps whatever width it's dragged to. Verified directly: narrowing `source` to 180px and widening `Planned Filename` to 500px both now hold exactly, instead of Tk correcting them back (confirmed the bug first with a minimal reproduction: requesting width=150 on a competing stretch column actually landed at 167).
- Fixed `Re-embed` reporting `BLOCKED: cannot read target Trk#` for every file rendered out of REAPER as an Re-embed target. Root cause: `split_recorder_channel()` expects iXML's `TRACK_LIST/TRACK/CHANNEL_INDEX` nested exactly as the recorder writes it; bwfmetaedit re-exporting the iXML chunk REAPER embeds during render can't map it back to that nesting and flattens it to `TRACK_LIST_TRACK_CHANNEL_INDEX` (with `_2`, `_3`... for additional tracks) instead. Added that flattened shape as a fallback when the nested lookup finds nothing — verified against real REAPER-rendered files, all now resolve their correct recorder channel (1, 2, 5, 6, 7, 8, matching the original source ISOs).
- Removed QC's `Inspector`, `Pause`, and `Cancel` buttons from the top action row — they duplicated controls that already exist elsewhere with identical behavior (Poly ↔ Split QC has its own `Inspector` button; the persistent top action bar's `Pause`/`Cancel` already control every long-running operation including QC, since both were already wired to the same shared capability state). `Compare` is unchanged.
- QC's `Base Formula` fields (Source and Target) now use the same shared, editable formula-preset dropdown as Split/Re-embed's formula fields, instead of a plain text entry — one preset list, one place to edit it (Settings), reflected everywhere.
- Investigated the field report that the SMPTE `TC` column always parses to `:00` frames: confirmed against the field's own files that every take's raw BWF sample count divides evenly by its sample rate — the recorder genuinely starts every take on a whole-second boundary, so `:00` frames are correct, not a parsing failure. While investigating, found and worked around `TIMECODE_RATE` sometimes being written as a fraction (`24/1`) rather than a plain number; the existing Description `$SPEED=` fallback already covers it correctly, so no fix was needed there this round.
- Added regression coverage for the flattened-iXML recorder-channel fallback (including its "must be exactly one channel" invariant). Full suite now at 99 passing tests.

## 260908.1932

- Added settings persistence: window size/position and the Settings-page Filename Formula Presets list are now saved to `~/Library/Application Support/SoX Metadata Workflow/settings.json` when the app window is closed, and restored on the next launch. Source/Target paths and other in-session Rename/Combine toggles are intentionally left unsaved, matching the app's existing "everything else resets" behavior.
- Added `load_app_settings()`/`save_app_settings()` (tolerant of a missing or corrupt settings file — falls back to defaults rather than raising) with regression coverage for the round trip and both failure cases.
- Investigated a report that the Rename Preview's new SMPTE `TC` column always shows `:00` frames. Checked against the field's own real files (Aaton Cantar recorder): every take's raw BWF sample count divides evenly by its 48000 Hz sample rate, so the take genuinely starts on an exact whole-second boundary — confirmed independently by bwfmetaedit's own `TimeReference_translated` field, which also reads `.000` ms for the same files. The `:00` frames are correct, not a parsing failure; no code change was needed for this.

## 260908.1737

- Replaced the per-field `Default` button on every formula field (Split's Output name; Re-embed's Source formula, Target formula, and its own rename-after-embed Output name) with a shared, editable dropdown (`ttk.Combobox`) listing saved formula presets — typing a formula still works exactly as before, the dropdown just adds one-click reuse.
- Added a "Filename Formula Presets" section in Settings to add, remove, and restore the preset list (seeded with the 3 formulas already used as examples across the app: `{base}.A$`, `{base}.A$ render \d+`, `{base}_AAP.A$`) — edited once, reflected immediately in all four fields, since every formula Combobox is registered against the same in-memory list and refreshed together (`_refresh_formula_combo_values`). Not persisted across launches, matching this app's existing settings model.
- Removed the now-unused `_entry_row` helper (replaced by `_formula_row`, the single shared builder every formula field goes through).
- Verified via a scripted check that all 4 formula Comboboxes stay in sync through add / remove / restore. Full suite still at 94 passing tests (no pure-logic behavior changed).

## 260908.1732

- Rename Preview's `TC` column now shows real SMPTE timecode (`HH:MM:SS:FF`, `;` before frames for drop-frame) instead of the raw BWF sample-since-midnight count, whenever frame rate and sample rate can be determined from metadata — falls back to the raw sample count when they can't, so nothing regresses for files without that metadata.
- Frame rate + drop-frame flag: read from iXML `SPEED/TIMECODE_RATE` + `TIMECODE_FLAG` first, falling back to the recorder's `$SPEED=`/`sSPEED=` Description field (e.g. Sound Devices' `sSPEED=024.000-ND`), same fallback style already used for Tape/Scene/Take.
- Sample rate: read from iXML `SPEED/FILE_SAMPLE_RATE` (or `TIMESTAMP_SAMPLE_RATE`) first, falling back to the `F=` field in the BEXT `CodingHistory` string (e.g. `A=PCM,F=48000,W=24,...`).
- Added `operations/timecode.py` (`parse_speed_field`, `samples_to_smpte`) with a correctly-verified drop-frame algorithm (checked against the classic 29.97 boundary: frame 1799→00:00:59;29, frame 1800→00:01:00;02 skipping :00/:01, frame 17982→00:10:00;00 with no drop at the 10-minute mark) plus non-drop conversion for arbitrary fps (23.976, 24, 25, 30, 48, 50, 60...).
- Added regression coverage for the new module and for `read_serial_metadata`'s end-to-end SMPTE rendering (including the exact Description shape from the field-reported example); the full suite now contains 94 passing tests.

## 260908.1704

- Fixed "Rename matched targets after embed" having no effect: the redesigned Re-embed panel exposed the Trk#/Stream# choice but never exposed the output name formula field, so the rename step used whatever was left over in `rename_formula_var` — a variable actually owned by Rename mode's Add Text field, which holds plain text (e.g. `_MIX`), not a `{base}`/`$` formula. It was empty in practice, so the rename step's `if rename_formula.strip():` guard skipped it entirely.
- Added a dedicated `reembed_rename_formula_var` (default `{base}.A$`) with its own Output name field, Default button, and help text inside the "Rename matched targets after embed" box, so e.g. `{base}_AAP.A$` renamed to Stream# now works from the Re-embed tab itself, independent of whatever is in Rename mode's Add Text field.

## 260908.1651

- Fixed the Execution log jumping above the source/settings area after visiting QC mode, and staying there for every other mode until Settings was visited once. Tk's `pack()` keeps an already-managed widget's existing stacking slot instead of moving it when it's packed again, so repacking `qc_frame` while `log_frame` stayed packed from a prior mode silently reordered them.
- `_update_mode_ui()` now unconditionally `pack_forget()`s `table_frame` / `qc_frame` / `settings_frame` / `log_frame` before repacking exactly the ones the current mode needs, in a fixed order — so the stacking order can no longer drift based on which modes were visited before.

## 260908.1642

- Redesigned the workflow settings area: Split, Re-embed, Rename, and Combine/Merge each now get their own dedicated settings panel instead of sharing one Tk grid toggled by a ~110-line show/hide function.
- Replaced the Mode dropdown with a row of tab buttons (Split + Embed / Re-embed / Rename / Combine / Merge / QC / Settings) so every mode is visible at a glance; switching modes still just changes the same `mode_var`, so all existing mode-dependent logic is unaffected.
- Rename's four actions (Add Text, Change $, Find & Replace, Serial ID) and Re-embed's "Rename matched targets after embed" are now toggleable group boxes — a checkbox sits in the section's own title bar, and its fields grey out immediately when unchecked (`_toggleable_labelframe`, synced via a variable trace so it stays correct however the checkbox gets toggled, including the Serial ID confirm/decline flow).
- Exposed Re-embed's post-embed Trk#/Stream# choice in its own panel; it already shared `rename_number_mode_var` with Rename mode's Change $, but previously had no visible control while in Re-embed mode.
- Split's Filename numbering and If-output-exists moved into its own panel (bound to the same `name_mode_var`/`conflict_policy_var`); Re-embed and Combine/Merge get their own If-output-exists control on the same shared `conflict_policy_var`, since Tk widgets can share one variable across panels without any behavior change.
- `QC` and `Settings` are unchanged; the outer skeleton (mode row → Source/Target → settings panel → action buttons → list → Execution log) is unchanged.

## 260907.2329

- After launching the Terminal installer, the app now polls every 3 seconds (up to 15 minutes) for SoX / BWF MetaEdit to appear, instead of requiring a manual restart.
- As soon as both are detected, `sox`/`soxi`/`bwfmetaedit` paths are refreshed in place and Status confirms readiness; no reopen needed.
- If installation is still not done after 15 minutes of polling, Status asks the user to reopen the app once it finishes.

## 260907.2312

- On startup, the app now checks for SoX and BWF MetaEdit and, if either is missing, shows a dialog explaining why and asking whether to open Terminal and install them.
- If Homebrew itself is also missing, the same dialog and the launched Terminal script cover installing Homebrew first (via the official install script), then `brew install sox bwfmetaedit`.
- Declining the dialog leaves the app untouched; the check runs again on the next launch until both tools are found.
- Added `build_tool_installer_script()` (pure, testable) that generates the `.command` script both branches (Homebrew present/absent) run in Terminal.
- Added regression coverage for both installer-script branches; the full suite now contains 90 passing tests.

## 260907.2258

- Added `Job.has_channel_suffix`, set from the same Poly/`.A#`/named-channel detection already used to keep Change `$` numberless for bare Poly files.
- Rename Preview Status now omits `Change to Trk#`/`Change to Stream#` from the action list for Poly (or otherwise suffix-less) files, since Change `$` has no effect on them; combined actions like Add Text no longer imply a channel-number change that will not happen.
- When Change `$` is the only enabled Rename action and the file has no channel suffix, Status now reads `No change: no channel number to change (Poly / no split suffix)` instead of the bare `UNCHANGED`.
- Added regression coverage for `has_channel_suffix` detection and the corresponding Status wording; the full suite now contains 88 passing tests.

## 260907.2250

- Turning on Serial ID (with ID order already `Metadata Tape + TC`) or switching ID order to `Metadata Tape + TC` (with Serial ID already on) now asks for confirmation before scanning, since it triggers a full metadata re-read of every file in the list.
- Confirming runs the same full rebuild as clicking `Preview / Refresh`; declining reverts the control you just changed (Serial ID back off, or ID order back to `Folder Path + Filename`) and leaves the list untouched.
- The prompt only fires on the transition into an unscanned Metadata Tape + TC state; it does not repeat while that state stays active across other Rename setting changes.

## 260907.2229

- Rename Preview Status now lists every enabled Rename action instead of only Add Text and Change $; e.g. `Ready to Add Text, Find & Replace, Change to Trk#, and Add ID`.
- Added `Tape`, `TC`, `Scene`, `Take` Preview columns, populated whenever Serial ID is enabled with ID order `Metadata Tape + TC`; otherwise shown as `-`.
- `read_serial_metadata()` now also reads Scene and Take using the same iXML → Core XML → Description (`$SCENE=`/`sSCENE=`, `$TAKE=`/`sTAKE=`) fallback chain already used for Tape.
- Fixed the Description fallback for Tape (and the new Scene/Take fields) capturing every following `key=value` line instead of stopping at the current line, because the shared XML-value reader had already collapsed newlines to spaces before the fallback regex ran. The fallback now reads the field from the raw, newline-preserving Description text.
- Renamed the Rename-mode `Target` column to `Before Rename`, now showing the original filename instead of duplicating `Planned Filename`; other modes keep the prior `Target` label and value.
- Added regression coverage for Status action wording and the Description fallback line-boundary fix; the full suite now contains 84 passing tests.

## 260907.2134

- Fixed strict Metadata Tape + TC ordering falsely reporting missing Tape when recorder Tape metadata is stored in Core XML rather than the separately exported iXML document.
- Tape reading now accepts standard iXML `TAPE`, Core XML `TAPE`, and recorder `$TAPE=`/`sTAPE=` Description encoding as representations of the same Tape field.
- Verified against a real recorder file (`S2-3T03.wav`), returning Tape `220921` and TimeReference `2383872001`.
- Added Core Tape regression coverage; the full suite now contains 79 passing tests.

## 260907.2130

- Added Serial ID order modes: `Folder Path + Filename` and strict `Metadata Tape + TC`.
- Metadata ordering reads iXML `TAPE` and Core/BEXT `TimeReference`; missing, invalid, or inconsistent values block the entire base group with no fallback.
- Different bases with the same Tape and TimeReference are blocked as duplicate metadata identities.
- Cached Tape/TC values are retained in Rename jobs, so live setting changes re-sort without rereading metadata.
- Serial ID cache identity now includes Source root, order mode, Prefix, Digits, and Start.
- Added strict ordering and end-to-end planner coverage; the full suite now contains 78 passing tests.

## 260907.2112

- Fixed toggling Find & Replace after Preview adding `.A1` to bare Poly filenames.
- Numeric suffix detection now comes exclusively from the original filename's `.A#` capture and never falls back to cached Slot, Trk#, Stream#, or metadata values.
- Added regression coverage proving `T1948.WAV` has no numeric Rename suffix.

## 260907.2107

- Removed the no-action warning from Rename Preview. A source list can now be loaded with every Rename action disabled.
- No-action Preview rows remain unchanged and Execute stays disabled; the no-change guard is retained at Execute time.

## 260907.2102

- Made every Rename setting editable before its action toggle is enabled.
- Add Text content and placement, Change $ numbering mode, all Find / Replace fields, Regex, and all Serial ID settings can now be prepared in advance; toggles only control whether each action is applied.

## 260907.2058

- Changed Rename startup defaults so Add Text and Change $ are unchecked.
- Find & Replace, Regex, and Serial ID remain unchecked, leaving every Rename action opt-in by default.

## 260907.2055

- Scoped Serial ID assignments to the current Source root; changing Source folders now clears the prior map and starts the new folder from the configured Start value.
- Fixed cached live refresh stopping at `updating cached Preview` for bare Poly files when the automatic parser returned no channel suffix.
- Added `Clear List` beside Cancel. It cancels any pending live refresh and clears Preview rows, the in-memory jobs snapshot, and Serial ID assignments while retaining paths and Rename settings.
- Added Source-path grouping coverage; the full suite now contains 75 passing tests.

## 260907.2049

- Replaced the ambiguous Rename formula action with `Add Text`, using a plain text field plus Prepend/Append placement.
- Add Text now operates on the original complete filename stem and never creates or rewrites a channel suffix; `T1921.wav` plus `_test` becomes `T1921_test.wav`, not `.A0`.
- Find & Replace remains the only Rename action that can modify existing `.A#`, `.Ch#`, or named-channel text.
- Added immediate cached-Preview update feedback and regression coverage for Poly and Split stem preservation; the full suite now contains 74 passing tests.

## 260907.2039

- Rebuilt the Rename control row layout so `Rename`, its formula and Default button, `Change $`, and the Trk#/Stream# selector occupy stable, non-overlapping cells.
- Kept `Find & Replace`, `Regex`, and `Serial ID` on the second control row with the three Find/Replace rows below.
- Fixed mode refresh restoring the hidden Rename formula label over the Rename toggle.
- Routed Rename and Change $ toggles through the state-aware callback so their dependent controls update immediately.

## 260907.2028

- Restored the visible `Rename` toggle to the left of the Rename formula field; it was previously hidden behind the Example/help grid cell.
- Repositioned `Change $` and the Trk#/Stream# selector into separate cells on the same row.

## 260907.1949

- Fixed `Change $` adding an artificial `.A1` to bare Poly files such as `T1921.wav`; files without a numbered or named channel suffix now ignore Change `$` and retain their base filename.
- Removed the large-list manual-refresh stop for Rename settings. Find / Replace and other Rename setting changes now refresh automatically against the existing in-memory snapshot, including lists larger than 1000 rows.
- Added regression coverage for bare Poly Change `$`; the full suite now contains 72 passing tests.

## 260907.1940

- Added a Rename live-refresh boundary at 1000 preview rows.
- Rename rows from the initial Preview now act as the in-memory snapshot for setting-only refreshes; large lists no longer rebuild the entire Treeview on every keystroke.
- For more than 1000 rows, changing Rename settings keeps the current snapshot and shows a clear `Preview / Refresh` prompt instead of blocking the UI with a full rebuild.
- Added regression coverage for the hybrid automatic/manual refresh policy; the full suite now contains 71 passing tests.

## 260907.1931

- Added three ordered Find / Replace rows; empty rows are skipped and non-empty rows apply from top to bottom.
- Added live-refresh traces for all six Find / Replace fields so edits in any row refresh the current Rename Preview.
- Changed Serial ID defaults to Prefix `ABC`, Digits `4`, Start `1`.
- Added regression coverage for three sequential Find / Replace rules; the full suite now contains 70 passing tests.

## 260907.1923

- Rename no longer requires a user-configured Source formula; Rename mode now automatically recognizes bare Poly names, numeric Split names such as `base.A2`, named channel names such as `base.Ls`, and `base.Ch1` names.
- Files sharing the same base are grouped together for Serial ID assignment regardless of whether their suffix is Poly, A#, speaker channel, or Ch#.
- Find & Replace now applies to the complete planned filename stem, so `.A` to `_Ch` works as expected.
- Hide the Re-embed-only Source formula row while Rename mode is active.
- Added automatic source matching coverage; the full suite now contains 69 passing tests.

## 260907.1914

- Allowed an empty Serial ID Prefix, producing numeric-only IDs such as `0001` while retaining the configured digit width.
- Moved the `Rename` toggle to the formula example position beside `Change $` and the Trk#/Stream# selector; removed the redundant example and `Rename $` labels.
- Moved `Find & Replace` and `Regex` above their Find/Replace input row.
- Kept Serial ID placement and formatting controls on the right side of the revised Rename layout.
- Added regression coverage for zero-character prefixes; the full suite now contains 68 passing tests.

## 260907.1905

- Renamed the workflow mode from `Rename Existing Split Files` to `Rename`.
- Rename source discovery now accepts bare Poly WAVs alongside Split `A#` files when using the default `{base}.A$` formula.
- Bare Poly Rename jobs remain numberless and render the base filename instead of producing artificial `.A0` or `.A1` names.
- Added regression coverage for Poly discovery and base-only output; the full suite now contains 67 passing tests.

## 260907.1901

- Fixed initial Rename Preview not receiving Find / Replace and Serial ID settings, which caused the first plan to fall back to the old `AB1234` behavior.
- Serial IDs are now assigned inside the Rename planner during initial Preview and retained in the caller-owned map for refreshes.
- Changing Prefix, digits, or start resets the old assignment map so new settings take effect immediately.
- Added regression coverage for initial Preview output such as `FB0001_AB9496.A2.wav`; the full suite now contains 66 passing tests.

## 260907.1854

- Fixed Rename option controls remaining disabled after their toggles were clicked.
- Added a shared Rename option callback that updates widget states before refreshing Preview.
- Regex now becomes usable when `Find & Replace` is enabled; Serial ID placement, Prefix, digits, and start become usable when `Serial ID` is enabled.
- Rename formula and Trk#/Stream# controls now follow the `Rename` and `Change $` toggles respectively.

## 260907.1849

- Split Rename actions into four independent toggles: `Rename`, `Find & Replace`, `Change $`, and `Serial ID`.
- Find and Replace rules are applied only when their toggle is enabled; their inputs are disabled otherwise.
- Serial ID is applied only when enabled and now uses the selected `Prepend` or `Append` placement instead of requiring an `{ID}` token in the filename formula.
- Updated no-action validation for Preview and Execute to recognize all four Rename actions.

## 260907.1836

- Made all Rename text inputs use a visible black insertion cursor on macOS, including the new Find, Replace, Prefix, digits, and start fields.
- Added separate `Find` and `Replace` labels for the two replacement inputs.
- Added live-refresh traces for all new Rename settings so Preview updates after typing or changing the controls.
- Made `{ID}`, `{BASE}`, and `{CHANNEL}` token spelling case-insensitive; `{ID}` now renders the assigned Serial ID instead of remaining literal text.
- Added regression coverage for uppercase Rename tokens; the full suite remains at 65 passing tests.

## 260907.1828

- Connected Rename source parsing for named channel-order files through `{base}.{channel}` formulas, including `.L`, `.R`, and `.Ls` style names.
- Rename Preview now carries the captured channel label into `{channel}` output formulas, so `{id}.{channel}` can produce names such as `AB1234.Ls.wav`.
- Poly WAV Rename remains a separate planner task; a Poly file will not be incorrectly treated as multiple Split channel jobs.

## 260907.1827

- Added usable Rename-mode controls for Find / Replace, Regex, and Serial ID Prefix, Digits, and Start values.
- Rename Preview now applies the entered replacement and assigns one stable Serial ID per base group during the current workflow session.
- Added `{channel}` output rendering for named split channels such as `.L`, `.R`, and `.Ls`; planner detection for channel-order and Poly inputs remains the next Rename slice.
- Added regression coverage for channel-order rendering; the full suite now contains 65 passing tests.

## 260907.1711

- Added a Rename core for ordered literal or regex Find & Replace rules.
- Added stable Serial ID assignment by Rename group, preserving existing IDs while assigning new IDs in sorted order.
- Extended Rename planning to accept replacement rules and Serial ID mappings, including `{id}` formulas such as `{id}.A$`; existing Rename calls remain compatible.
- Deferred Audio/Metadata QC and Metadata-token Rename while Rename work is prioritized.

## 260907.1701

- Set the main window default to `1180x800` with a smaller `980x700` minimum, matching the compact working layout instead of inheriting an oversized Tk requested width.
- Reduced the Preview table's initial column total from about 1510px to 1070px; Source, Target, Planned Filename, and Status now share available width responsively while Slot, Trk#, and Stream# remain fixed.

## 260907.1558

- Added visible Pause and Cancel controls to automatic QC, sharing the same cooperative operation contract as Preview and Execute; cancelled QC results are discarded and controls reset cleanly.
- QC control checks run at detection, scanning, metadata-read, and PCM comparison boundaries. File QC finishes its single active comparison before discarding a cancellation.
- Changed paired QC sorting so missing values participate normally instead of being forced to the bottom; Source/Target headings use Source-first, Target-second composite ordering for immediate missing/extra comparison. The full suite now contains 62 passing tests.

## 260907.1538

- Added clickable ascending/descending sorting to File, Folder, and Poly/Split QC result tables through reusable `TreeviewSorter`.
- Stream, Recorder Ch, and Frames sort numerically; filenames and result text use case-insensitive natural ordering; missing values remain at the bottom in both directions.
- File/AUDIO section rows and bottom AUDIO RESULT summaries remain pinned, and the active QC sort is reapplied after a new comparison. The full suite now contains 61 passing tests.

## 260907.1534

- Added independent Source and Target Base Formula inputs to the unified QC picker and standardized QC formula syntax/defaults on `{base}.A$`.
- Improved automatic Poly/Split detection for folders that both contain mixed mono and multichannel WAVs: direction is selected by the number of cross-folder split-name matches for each Poly base.
- After direction normalization, Poly/Split comparison uses the formula belonging to the detected Split side. Added mixed-folder evidence coverage; the full suite now contains 60 passing tests.

## 260907.1525

- Replaced the manual QC Type radios and three separate input areas with one SOURCE/TARGET picker that accepts WAV files or folders through buttons and drag-and-drop.
- Compare now detects File, recursive Folder, or Poly/Split QC in a background worker, normalizes reversed Split/Poly input direction, switches to the matching result table, and enables Inspector only for Poly/Split results.
- Added detailed `Detecting Source` and `Detecting Target` progress before comparison, and snapshots tool/formula settings before entering the worker thread. The full suite now contains 59 passing tests.

## 260907.1505

- Added a conservative automatic QC input detector: WAV file pairs select File QC, folder pairs select recursive Folder QC unless one side is clearly multichannel Poly and the other clearly mono Split.
- Poly/Split direction is normalized automatically when the Poly folder is supplied on the right; mixed File/Folder inputs return an explicit validation error.
- Ambiguous folder contents fall back to Folder QC instead of guessing. Added direct detection and direction coverage; the full suite now contains 58 passing tests.

## 260907.1448

- Moved Folder-to-Folder QC scanning and Soxi comparison off the Tk main thread so large folder trees no longer make the interface appear frozen.
- Added live `Scanning Source`, `Scanning Target`, and `Comparing` stages to the shared status/log panel, including discovered-file counts, comparison current/total, elapsed time, and ETA.
- Progress logging is throttled to stage boundaries and each 100 discovered files to keep the Execution log readable.

## 260907.1436

- Added recursive subfolder support to Folder-to-Folder QC.
- Folder pairs now match WAV files by their relative path instead of basename alone, so identical filenames in different subfolders remain distinct and are correctly reported as PASS, MISSING, or EXTRA.
- Added direct recursive-folder characterization coverage. The full suite now contains 56 passing tests.

## 260907.1430

- Removed the duplicate status labels below Compare controls in all three QC views now that live and final status is consistently displayed in the shared Execution log header.
- QC result tables now begin directly below their action controls; internal QC message state and behavior remain unchanged.

## 260907.1426

- Kept the shared Execution log and live status header visible below QC results instead of hiding the panel in QC mode.
- QC result views retain the expandable main area, with the existing fixed-height log panel at the bottom; Settings mode still hides the log.

## 260907.1418

- Extracted the Execution log frame into reusable `ExecutionLogPanel`, including its title, live status label, text widget, and append behavior.
- The main application retains its existing `log_frame` and `log` references, so mode visibility and current callers remain unchanged.

## 260907.1405

- Moved live Preview/Execute status and progress text from beside the Cancel button into the Execution log header, immediately to the right of its title.
- The action row now remains compact while all existing status updates, log content, and workflow mode visibility behavior remain unchanged.

## 260907.1356

- Added the first reusable UI component, `WorkflowFolderPicker`, with SOURCE fixed on the left and TARGET fixed on the right for every non-QC workflow mode.
- Both panels retain editable path fields and Select Folder dialogs and now accept folder drag-and-drop across the panel, entry, and button surfaces.
- The component continues to write the existing Source/Output variables, preserving relative Split/Rename output paths, planner traces, dialog history, and mode behavior. The full suite now contains 55 passing tests.

## 260907.1329

- Added staged Poly/Split QC preparation progress before PCM comparison: scanning Poly files, inspecting Poly layouts, scanning Split files, reading Split metadata, and reading Poly metadata.
- Existing PCM comparison progress remains unchanged, while blocked, skipped, duplicate, and metadata-error preparation paths now still reach their stage totals.

## 260907.1321

- Added clickable sorting to every Preview list column with ascending/descending heading indicators.
- Slot, Recorder Ch, and Stream columns sort numerically; file and status columns use case-insensitive natural ordering; blank and `-` values remain at the bottom in both directions.
- Preview refresh preserves the active sort, and Treeview row movement keeps Inspector and Execute job mappings intact. The full suite now contains 54 passing tests.

## 260907.1311

- Fixed Rename mode retaining stale executable jobs after both `Rename to` and `Change $ to` were unchecked.
- With no Rename action selected, every Rename Preview row now shows `NO CHANGE`, Execute is disabled, and `execute()` independently rejects the operation with a `No change` status message.
- Added regression coverage proving no-action Rename jobs are not executable. The full suite now contains 53 passing tests.

## 260907.1246

- Added one explicit Pause/Cancel capability contract covering both Preview loading and Execute phases, with item, job, and Rename transaction boundaries.
- Made in-place Rename cancellation transaction-safe: cancellation before commit restores every staged source, creates no targets, leaves no hidden `.rename-*` files, and reports every planned rename as cancelled.
- Rename commit is now uninterrupted once it starts, preventing partial batches; all other Execute modes retain cooperative control between jobs. The full suite now contains 52 passing tests.

## 260907.1029

- Added cooperative Pause and Cancel controls while loading Preview plans for Split, Re-embed, Rename, Combine, and Merge.
- Preview workers now check control state at planner item boundaries and before publishing results; cancellation discards partial plans and restores all action-button states cleanly.
- A currently running Soxi or BWF MetaEdit item finishes before Pause/Cancel takes effect, matching Execute's existing command-boundary behavior. The full suite now contains 50 passing tests.

## 260907.1024

- Extracted shared recursive WAV discovery, natural path sorting, QC filename-first sorting, and output-root resolution into `operations/path_core.py`.
- Added direct characterization coverage for planner/QC ordering differences and empty, relative, and absolute output roots. The full suite now contains 49 passing tests.
- The main application continues to re-export the existing path API for planners and UI callers.

## 260907.1018

- Extracted pure iXML parsing and mono-track transformations into `operations/ixml_core.py`.
- TRACK_LIST creation, positive integer fields, control-character sanitization, source parse diagnostics, poly-slot extraction, fallback track creation, and mono normalization now sit below BWF MetaEdit I/O orchestration.
- Added direct coverage for preserving Recorder Ch while normalizing Interleave Index and for sanitized/error-context parsing. The full suite now contains 47 passing tests.

## 260907.1015

- Extracted speaker filename parsing, alias mapping, layout/order detection, channel-mask calculation, layout naming, and merged-target naming into `operations/speaker_layout.py`.
- Speaker constants now live with their domain rules; the UI's editable default alias mapping remains in the main application.
- The main application re-exports the existing speaker API so Merge planning, Inspector display, and current tests remain unchanged.

## 260907.1012

- Fixed Inspector mouse-wheel scrolling over the beige comparison background and gaps between rendered sections by binding the local comparison frame to the existing canvas scroll handler.
- The binding remains scoped to the Inspector and does not intercept scrolling in the main window.

## 260907.1006

- Extracted pure WAV PCM layout parsing and poly-stream/mono comparison into `operations/audio_qc.py`.
- Integer PCM, IEEE Float, exact byte comparison, cross-format sample normalization, and truncation checks now live below the BWF metadata mapping layer.
- The main application re-exports the existing QC binary API; higher-level Recorder Ch matching and folder progress behavior remain unchanged.

## 260907.1003

- Extracted the shared filename formula engine into `operations/filename_formula.py`, including placeholder validation, literal pattern compilation, regex-aware Re-embed target matching, and output-name rendering.
- Speaker and Poly/Split QC formula wrappers continue to use the shared compiler while preserving their specific defaults, capture-group names, and validation messages.
- The main application re-exports the existing formula API so planners and UI refresh behavior remain unchanged.

## 260907.0953

- Moved WAV extensible channel-mask clear/set operations and RIFF structure inspection into `operations/wav_core.py`.
- The main application continues to re-export the same helper names, preserving Combine, Merge, Inspector, and test call sites.
- Recorded that Pause and Cancel support must become explicit per-mode capabilities when UI/Core execution controls are extracted.

## 260907.0942

- Created the first non-UI operations module, `operations/wav_core.py`, containing `WavChunk` and the shared RIFF/WAVE scan, find, read, remove, and raw-copy functions.
- The main application explicitly re-exports the existing WAV core API, so current callers and characterization tests remain unchanged.
- Added module-path initialization so adjacent operations modules work both during normal launch and dynamic test or embedded loading.

## 260907.0939

- Introduced a typed `InspectorRenderModel` for prepared comparison results, replacing the worker queue's positional 10-field tuples.
- Inspector sections, parsed iXML roots, headers, scroll policy, path label, and Combine layout flags now cross the worker/UI boundary through explicit fields without changing rendering behavior.
- Added render-layout contract coverage. The full suite now contains 45 passing tests.

## 260906.2118

- Fixed Poly/Split QC Inspector row navigation so selecting another mapping preserves the current vertical scroll position instead of jumping to the top.
- Opening the QC Inspector for the first time still starts at the top; the scroll reset policy is now explicit in `InspectorContext`.

## 260906.1913

- Introduced a typed `InspectorContext` model for standalone file, Preview job, and Poly/Split QC comparison requests.
- Replaced positional Inspector request tuples with explicit payload and scroll-policy fields while preserving existing rendering, stale-request filtering, and QC scroll behavior.
- Added Inspector context contract coverage. The full suite now contains 44 passing tests.

## 260906.1814

- Migrated Poly/Split QC progress onto the shared typed `ProgressEvent` queue contract while preserving request-id filtering so stale workers cannot overwrite newer comparisons.
- Extended the shared progress model to support request-scoped work without a single file item; all continuous workflow and QC progress now uses one event type.
- Added request-scoped timing coverage. The full suite now contains 43 passing tests.

## 260906.1803

- Introduced a typed `ProgressEvent` model with shared elapsed/ETA calculation.
- Unified Split Preview, Re-embed/Rename/Combine Preview, and Execute progress messages onto one queue event contract while preserving their existing status and log text.
- Added deterministic overall-progress timing coverage. The full suite now contains 42 passing tests.

## 260906.1801

- Re-embed Preview now includes target-only WAVs as non-executable `NO SOURCE` rows and sorts all rows with Target paths first in natural filename order, followed by source-only `NO TARGET` rows.
- Added a shared `job_is_executable` contract used by Preview counts, Execute filtering, processing-report alignment, and Execute button state so preview-only rows cannot enter execution.
- Added characterization coverage for bilateral Re-embed target visibility, target-first ordering, and execution eligibility. The full suite now contains 41 passing tests.

## 260906.1552

- Consolidated explicit metadata write policies without changing their behavior: Split preserves its declared raw chunks before writing mono iXML; Combine/Merge raw-copies first-source BEXT before writing rebuilt poly iXML; Re-embed writes mono iXML, raw-copies source BEXT, then removes stale target `LIST/INFO`.
- Verified all three policies with focused regressions and manual Split, Re-embed, Combine, and Merge tests.

## 260906.1442

- Added a read-only `MetadataRead` model that records Core and iXML content, warnings, parsed iXML, and errors independently.
- Audio Inspector now consumes the shared model while preserving its existing Core/iXML sections and sanitized parsing behavior. No metadata write policy changed.

## 260906.1435

- Re-embed now removes stale target `LIST/INFO` metadata after writing rebuilt iXML and raw-copying source BEXT, preventing BWF MetaEdit Core reports from combining preserved BEXT fields with target-only INFO fields.
- Added raw WAV chunk removal coverage confirming target LIST removal preserves BEXT bytes, audio bytes, and RIFF size integrity.

## 260906.1405

- Re-embed now matches source files to target files by each source group's derived Stream number, while retaining source iXML `CHANNEL_INDEX` as its Recorder Ch identity. A source `A5` at Stream 3 now maps to target Stream `A3` rather than target filename `A5`.
- Re-embed now raw-copies the source `bext` chunk after rebuilding iXML, preserving BEXT bytes, CRLF formatting, Coding History, and version fields without Core XML reserialization.

## 260906.1307

- Introduced a shared in-file WAV core (`WavChunk`, `scan_wav_chunks`, `find_wav_chunk`, and `read_wav_chunk`) used by raw chunk preservation, channel-mask access, Inspector structure reads, and PCM layout reads.
- Added characterization coverage for filename formulas, Split/Re-embed/Rename job semantics, raw chunk preservation, Combine BEXT/iXML policy, QC metadata mapping, PCM integer/float equivalence, and direct WAV-core behavior. The full suite now contains 31 passing tests.

## 260905.1758

- Poly/Split QC now uses each candidate Split WAV's own iXML `CHANNEL_INDEX` as Recorder Ch; filename A# is used only for Base Name Formula discovery and never as a channel fallback.
- MISSING, EXTRA, and duplicate-channel decisions now use metadata Recorder Ch values. Split files without a reliable `CHANNEL_INDEX` block mapping instead of being guessed from their filename.

## 260905.1713

- Poly/Split PCM QC now supports uncompressed 32-bit IEEE Float WAVs and exact integer-to-float sample equivalence, while still detecting changed samples.
- An already-open QC Inspector now follows mapping-row selection; selecting rows never creates Inspector, and summary rows remain ignored.
- Poly/Split Compare now runs in a background worker and streams per-row current/total, elapsed, and ETA updates through the existing event queue while Compare is disabled.

## 260905.1650

- Added independent vertical scrollbars to the File, Folder, and Poly/Split QC result lists.
- Added a configurable Poly/Split Base Name Formula, defaulting to `base.A$`, with case-insensitive support for fixed naming text such as `base_AAP.A$` while preserving non-contiguous Recorder Ch matching.

## 260905.1631

- Poly/Split row selection no longer opens or updates Audio Inspector automatically; a new `Inspector` button beside `Compare` performs the existing cached-row inspection on demand.
- Missing selection and overall summary selection now show a prompt without changing the current Inspector window.

## 260905.1620

- Selecting a Poly/Split mapping row now opens or updates the existing modeless Audio Inspector with the real Poly and Split WAV metadata plus a top-level QC COMPARISON section.
- PASS, DIFFERENT, MISSING, and EXTRA rows reuse cached QC context without rerunning folder discovery or PCM comparison; the overall summary row is ignored.

## 260905.1610

- Expanded Poly/Split batch results to one selectable row per expected or extra Split file, showing Poly File, Stream, Recorder Ch, Split File, Frames, and PCM Result.
- Each displayed row now retains its full Poly/Split paths and mapping/result context for a future Inspector QC detail view; the overall Audio Result remains at the bottom.

## 260905.1556

- Upgraded `Poly ↔ Split` QC to recursively compare a Source Poly folder with a Target Split folder using case-insensitive base-name matching independent of relative paths.
- Added batch summary results, mono-source skipping, missing/extra base reporting, duplicate Poly-base blocking, and reuse of the existing exact Track List/PCM stream comparison for each matched Poly.

## 260905.1526

- Added `Poly ↔ Split` QC with automatic file/folder direction detection and strict iXML Track List `INTERLEAVE_INDEX` to `CHANNEL_INDEX` mapping.
- QC now extracts each interleaved PCM stream and compares its exact sample bytes with the matching `.A#` mono WAV, including packed 24-bit PCM. Missing, extra, duplicate, unavailable mapping, and PCM differences are reported without modifying audio or metadata.

## 260905.1423

- Added a QC Type selector with the existing `File ↔ File` workflow as the default and a new `Folder ↔ Folder` workflow.
- Folder QC scans only first-level WAV files, matches exact filenames, reuses basic Audio QC for matched files, and reports `PASS`, `DIFFERENT`, `MISSING`, or `EXTRA` with an overall result.

## 260905.1410

- Added read-only QC Mode v1 with independent Source/Target WAV Load and drag-and-drop controls.
- QC compares File Name/Size plus Sample Rate, Bit Depth, Channels, Frames, Duration, and Audio Format. Channel count and file properties are informational; only required audio properties determine `PASS` or `DIFFERENT`.

## 260905.1358

- Removed the Audio Inspector Channel Configuration Help button and its help content.
- Source and Target paths now wrap within fixed equal-width columns, and each side accepts WAV drops across its full rendered metadata area.

## 260905.1348

- Audio Inspector now has independent Source and Target Load controls, paths, and WAV drag-and-drop zones. Updating either side preserves the other side's current file and metadata.
- Preview List job inspection remains supported, including the existing `PLANNED TARGET` metadata view; manual Inspector loading remains read-only.

## 260904.1514

- Preview List Combine/Merge Inspector jobs now render a display-only Planned Target from the existing Job sources and planned stream order, without creating audio or writing metadata.
- Planned iXML is tolerant of absent, partial, or unparseable source iXML: those states are reported in the iXML section while planned FILE, AUDIO, CHANNEL CONFIGURATION, and BEXT / CORE remain visible.

## 260904.1359

- Replaced the expanded Channel Configuration panel with a compact `Supported Channel Layouts` information menu beside Order.
- The menu is documentation-only: choosing an entry does not change Order, `Detected / Auto`, Preview, validation, source detection, or Merge behavior.

## 260904.1351

- Removed the editable speaker Mapping controls from the Merge panel; Preview planning now always uses the existing fixed internal canonical/alias mapping.
- Reorganized the Merge panel with operational controls on the left and the supported Channel Configuration reference on the right. Layout remains `Detected / Auto` and source-layout detection behavior is unchanged.

## 260904.1337

- Speaker-aware Merge now detects each source group's layout from its actual canonical channel set; the retained Layout field displays `Detected / Auto` and no longer filters all groups by one global channel count.
- SMPTE/Film selects the output path order for each detected Stereo, 2.1, 3.0, 3.1, 5.1, or 7.1 group. Detected 7.1.2 Dolby Atmos Bed groups always use their fixed Dolby Atmos order.
- Incomplete, duplicate, unknown, or unsupported channel sets remain blocked. No Generic Combine, metadata, Inspector, TSV, rename, or Channel Mask behavior was changed.

## 260904.1324

- Restricted standard speaker-aware Merge selection to Stereo, 2.1, 3.1, 5.1 SMPTE/Film, 7.1 SMPTE/Film, and 7.1.2 Dolby Atmos Bed with their explicit path orders. Layouts beyond 7.1.2 are not supported.
- Added a Layout selector and an in-panel Channel Configuration reference. Selecting 7.1.2 restricts Order to Dolby Atmos; all other supported layouts use SMPTE or Film.
- Corrected canonical identities so Pro Tools `Lss/Rss/Lsr/Rsr` and Dolby Atmos `Lrs/Rrs` remain distinct. Existing `FL/FR/FC/SL/SR/BL/BR` aliases remain unchanged.
- No Channel Mask / `dwChannelMask` behavior was changed.

## 260904.1238

- Added Dolby channel abbreviation recognition while preserving distinct channel identities. Explicit aliases include `Lss -> Ls`, `Rss -> Rs`, `Lsr/Lrs -> Lb`, and `Rsr/Rrs -> Rb`; existing `FL/FR/FC/SL/SR/BL/BR` aliases remain unchanged.
- Dolby positions without an explicit equivalence are retained as their own canonical identities and do not infer or write a channel mask.

## 260904.1050

- Combine/Merge Processing Report now exports exactly one row per Preview Job. Execute updates each ready row's status in place to OK, FAIL, or CANCELLED instead of appending duplicate execution rows; BLOCKED and SKIP rows remain unchanged.

## 260904.1043

- Combine/Merge TSV export is now a complete Processing Report: it snapshots every current Preview Job status, including READY GENERIC, READY MERGE, READY COPY, BLOCKED reasons, and SKIP SINGLE, then appends execution OK/FAIL/CANCELLED outcomes.
- Planning rows use the existing Preview Job objects and display-status formatter without rerunning planning. The existing TSV columns remain unchanged.

## 260904.0846

- Unified Combine/Merge Preview now rebuilds its current Job model from the existing `build_combine_jobs()` planner whenever planning settings change, so the Preview List cannot retain stale order, status, mapping, copy, or output-name results.
- Speaker pattern, order, custom order, mapping text, copy, rename, format/layout, prepend/append, custom layout name, and output-conflict policy now use the same centralized live-refresh path. Mapping refreshes after typing, pasting, or leaving the field.
- Refresh preserves the currently selected row when its source remains in the rebuilt plan and does not invoke SoX or execute any jobs.

## 260903.2114

- Unified `Combine Split Files to Poly` as `Combine / Merge Mono Files to Poly`. One Preview can now plan generic `.A#` groups and mapped speaker-aware mono groups together.
- Generic groups retain numeric source order without requiring `.A1` or contiguous numbers and continue to clear `dwChannelMask` to `0`. Single generic or speaker mono files follow `Copy non-combined / non-merged files` when enabled.
- Added speaker filename pattern, editable `TOKEN=Canonical` mapping, explicit SMPTE/FILM/CUSTOM channel orders, and optional prepend/append format/layout naming for speaker-aware merged targets.
- Speaker-aware Merge validates mono compatible sources and explicit mapped channels, preserves Recorder Ch in iXML, rebuilds Stream order using `poly_ixml_from_mono_sources()`, and writes the selected Windows WAVEFORMATEXTENSIBLE channel mask to the resulting WAV after SoX.
- Combine/Merge Inspector planned output now distinguishes Generic Multichannel from Speaker-aware Merge and displays its planned canonical order and channel mask.

## 260903.2000

- Combine Inspector now presents the right column as a Planned Combined Target, not an inspection of the normally nonexistent Preview output WAV.
- Planned output shows its computed filename, folder, and `Will be created on Execute` status; it predicts audio data from the source files, declares Generic Multichannel with no speaker configuration, uses Core/BEXT from the same first source as Combine Execute, and renders iXML directly from `poly_ixml_from_mono_sources()`.
- Planned Combine inspection reads source metadata only. It does not inspect, create, modify, or require `job.target`, and no longer shows `Target Not Found` during Combine Preview.

## 260903.1951

- Combine Inspector now pairs `SOURCES` `FILES` and the Combined Target `FILE` section in the first shared comparison row. Existing target metadata starts directly beside the source list; missing targets show `Target Not Found` with their path in that same right-hand content area.

## 260903.1942

- Combine Inspector now leaves missing source-side comparison sections blank for alignment instead of rendering redundant `FILE` / `No Data` placeholders. The left side remains `SOURCES` with `FILES`; the right `COMBINED TARGET` retains its complete target inspection.

## 260903.1855

- Added Finder WAV drag-and-drop directly to the Audio Inspector content area using `tkinterdnd2`. Dropping a valid WAV loads the first accepted file through the existing single-file Inspector workflow.
- Inspector drop data uses Tcl/Tk list parsing to preserve Finder paths containing spaces, Unicode, parentheses, brackets, and other valid characters. Non-WAV entries are ignored without a modal error.
- Drag-and-drop is not registered anywhere in the main Workflow UI and does not alter Source, Preview, Execute, or Inspector Preview-row behavior.

## 260903.1842

- Added a Source-only Finder drag-and-drop zone using the existing `tkinterdnd2` dependency. Dropped paths are parsed as a Tcl/Tk file list, preserving spaces, Unicode, brackets, and other valid path characters.
- A single folder sets Source directly. One or more WAV files from the same folder set that existing Source folder; non-WAV files are ignored with a status/log summary, and resolved duplicate paths are ignored.
- Multiple source folders or WAV files from different folders are rejected without changing Source. Drop does not start Preview or Execute.

## 260903.1812

- Added an `Inspect Target` checkbutton beside `Audio Inspector`. Mode changes default it to OFF for Split, Rename, and Combine, and ON for Re-embed; manual changes remain in effect until the mode changes again.
- When `Inspect Target` is OFF, the Inspector background worker reads and prepares only Source data, renders a full-width single Source column, and does not call SoXi, BWF MetaEdit, WAV parsing, or iXML parsing for the target path.
- When ON, Preview selection and toggle changes use the existing request-id worker path to render Source and Target together without changing the Inspector window, scrolling, comparison alignment, or channel-configuration behavior.

## 260903.1752

- Audio Inspector now prepares Source/Target SoXi, BWF MetaEdit, WAV, and iXML data in a background worker. The current comparison remains visible until the complete new data set is ready for one main-thread render.
- Added a monotonically increasing Inspector request id so stale background results from rapid Preview selections cannot overwrite the most recent selection. No debounce, off-screen frame swap, scroll restoration, or scrollbar/geometry lifecycle change was reintroduced.
- Manual `Select File...` requests share the same token path and invalidate prior Preview loads.

## 260903.1710

- Reverted the experimental Inspector off-screen frame swap, delayed scroll settlement, and latest-selection debounce lifecycle because they could leave the comparison blank when switching Preview rows away from the top scroll position.
- Restored the stable direct shared-comparison refresh baseline. Preview changes now display normally at top, middle, and bottom scroll positions while retaining the existing outer scrollbar, synchronized comparison layout, metadata rendering, and selectable text.

## 260903.1640

- Preview List driven Inspector updates now use a latest-selection debounce. During preparation, the current visible Source/Target comparison remains untouched; rapid row changes discard superseded pending jobs and commit only the latest completed comparison.
- A manual `Select File...` load cancels a queued Preview update, so it cannot be overwritten by a prior row selection.

## 260903.1630

- Reduced Audio Inspector flicker during Preview List changes by building and measuring replacement Source/Target comparison content off-screen, then swapping it into the existing shared canvas in one visible update.
- Text auto-height and scroll-region work is now batched and coalesced. Delayed Configure events from replaced frames are ignored, preventing a stale layout pass from moving the shared scrollbar.
- Preview updates retain the existing Toplevel, shared scrollbar, window position, selectable text, aligned sections, and settled scroll position.

## 260903.1607

- Fixed Audio Inspector selectable-text sizing so each section includes the final display line and no longer clips fields such as `Start Offset`.
- Text height now reflows after idle when column width changes, allowing long metadata to word-wrap without overflow. Short-side section content remains top-aligned while the paired comparison row keeps the shared maximum height.

## 260903.1552

- Restyled Audio Inspector selectable metadata as borderless light-background text with dark text, matching the existing light Treeview visual language.
- `No Data`, `No Target`, and `Target Not Found` now use a muted dark-gray text treatment rather than a black content block. Selection, Command/Ctrl+C, shared comparison scrolling, and all metadata rendering behavior are unchanged.

## 260903.1544

- Preview / Refresh is now disabled until Source contains a valid folder. Preview also rejects blank or invalid Source paths before resolving a path, preventing an empty field from accidentally scanning the application's current working directory.

## 260903.1539

- Removed the Audio Inspector `Copy` / Copy All mechanism. Metadata bodies are now read-only selectable text, so Command+C and Control+C copy only the user-selected text.
- Track List and Aaton `ttk.Treeview` tables retain their UI and now copy selected rows as TSV with column headers via Command+C or Control+C.
- Selectable Inspector text wraps within its comparison column and expands in height without adding inner scrollbars; shared comparison scrolling and section alignment are unchanged.

## 260903.1529

- Audio Inspector now keeps `SOURCE` and `TARGET` as fixed column headers above the shared scrollable comparison content, so they no longer repeat as scrolling sections.
- The shared comparison canvas now spans both equal-width columns, eliminating the unused right-side space while preserving the single synchronized scrollbar.
- Added the fixed headers and shared comparison canvas that later Inspector copy improvements build on.

## 260903.1505

- Reworked Audio Inspector into a shared Source/Target comparison viewer with one visible vertical scrollbar and one common scroll coordinate system.
- Matching metadata sections now occupy the same grid row, so Source and Target headers remain vertically aligned even when one side contains more metadata or table rows.
- Preview List job changes preserve the Inspector window position and current comparison scroll position; only a standalone `Select File...` load resets scrolling to the top.
- Track List and Aaton tables remain scrollbar-free and expand within the shared comparison surface. Target rendering and Generic Multichannel `Configuration: None` behavior are unchanged.

## 260903.1451

- Audio Inspector Source and Target panes retain independent vertical scrolling and now reset to the top whenever a new file or Preview Job is rendered.
- Embedded iXML Track List and Aaton tables now expand to their actual row count, up to 100 rows, with the name column taking available width; the outer Inspector pane remains the sole vertical scroll surface.
- Preview Jobs with an existing target use the normal full Target metadata renderer. Planned targets that are not yet on disk now show `Target Not Found` with their full path, distinct from Rename's `No Target` state.

## 260903.1346

- Audio Inspector now uses Microsoft Windows `WAVEFORMATEXTENSIBLE` `dwChannelMask` abbreviations: `FL`, `FR`, `FC`, `LFE`, `BL`, `BR`, `FLC`, `FRC`, `BC`, `SL`, `SR`, `TC`, `TFL`, `TFC`, `TFR`, `TBL`, `TBC`, and `TBR`.
- Inspector displays only speaker positions set in the actual WAV mask, in Windows bit order. A mask of `0` is Generic Multichannel and displays `Configuration: None`; channel count, filenames, and iXML names are never used to infer layout.
- Inspector now renders iXML Track List and Aaton Cantar track-name data in embedded `ttk.Treeview` tables instead of text tables, and includes an in-app Channel Configuration Help window.
- Removed the duplicate standalone Inspector `FILE` heading. Drag-and-drop remains deferred and no dependency was added.

## 260903.1316

- Audio Inspector now renders iXML as readable General, Speed, History, File Set, Track List, BEXT, Aaton Cantar, and recorder-specific sections instead of raw XML.
- Inspector now shows Source and Target panels side by side; Combine jobs list their existing `Job.sources` beside the Combined Target, while Rename jobs retain a `No Target` panel.
- Preview List rows now map directly to their existing `Job`; a single click updates an already-open Inspector, and opening the Inspector loads the current selected row.
- Inspector displays Channel Configuration only from the WAV's actual non-zero channel mask. Generic 2-, 6-, and 8-channel Combine files with mask `0` display `None`.
- Drag-and-drop remains deferred and no dependency was added.

## 260903.1302

- Added a standalone modeless Audio Inspector with `Select File...` support for inspecting one WAV without changing any workflow mode or processing behavior.
- Inspector now displays file/audio data from Soxi plus WAV block alignment and data start, BEXT/Core XML, iXML, and separate Recorder Ch (`CHANNEL_INDEX`) and Stream (`INTERLEAVE_INDEX`) values when present.
- Preview List linking and drag-and-drop are intentionally deferred to later Inspector phases.

## 260903.1047

- Combine now clears the `WAVE_FORMAT_EXTENSIBLE` channel mask that SoX `-M` assigns from the output channel count, so generic multichannel output carries no speaker-layout / Channel Configuration meaning.
- Added Combine regression tests for 2-, 6-, and 8-channel output masks, plus preservation of non-contiguous Recorder Ch `CHANNEL_INDEX` values with newly contiguous `INTERLEAVE_INDEX` values.

## 260902.1753

- Folder chooser dialogs now open at the current Source or Output/Target path when available, remember the last folder chosen during the session, and use the Source parent as the Output/Target fallback.

## 260902.0803

- `Check Tools` now applies the same Homebrew `sox` formula version fallback to Soxi when `soxi --help` returns a truncated `SoX v` string.

## 260902.0748

- Restored the missing `tool_version()` helper used by `Check Tools`, fixing an empty tool-check log and `NameError` callback failure.

## 260902.0743

- Restored the `Check Tools` button for checking SoX, Soxi, and BWF MetaEdit executable availability and versions.
- Restored the `Open Terminal` button that opens a Terminal window with suggested Homebrew install/update commands for SoX and BWF MetaEdit.

## 260902.0644

- Re-embed execute now defers target renames until after metadata embedding completes.
- Re-embed target renames now use temporary hidden filenames before moving to final names, so chained renames such as `.A4 -> .A5` while `.A5` already exists in the same batch no longer fail.
- Re-embed rename execution now detects duplicate final rename targets and external existing targets before the final move.

## 260902.0623

- Re-embed preview now reports elapsed time and ETA while scanning targets, scanning sources, reading source metadata, building jobs, and rendering preview rows.
- Re-embed preview ETA now uses overall progress across preview phases instead of only the current phase.
- Re-embed preview final elapsed time now includes table rendering time.
- Re-embed preview throttles high-volume progress/log updates during job building to keep the UI responsive.
- Re-embed `NO TARGET` and `AMBIGUOUS TARGET` rows now read Trk# from source metadata when available, matching `Ready to Embed` rows.
- Stream# assignment in re-embed mode is now derived from source groups regardless of target match status.
- Restored the original light `clam` UI theme behavior.