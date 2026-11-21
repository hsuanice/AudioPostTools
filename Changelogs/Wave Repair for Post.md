# Wave Repair for Post — Changelog

A separate app from Wave Manager for Post, living in the same folder and sharing its `operations/` engine. Wave Manager *manages* WAV files; Wave Repair *rescues* them. Versions follow the same `YYMMDD.HHMM` (Taipei time) convention as Wave Manager.

## 261121.2233 — Messages are drawn by Tk: no more crash after a message closes

hsuanice: Check for Updates said "latest version", and the app quit unexpectedly right after the message closed.

- **Cause:** the native macOS alert behind `tkinter.messagebox`. On this macOS, with a game controller paired (DualSense / DUALSHOCK 4, even when not connected), GameControllerUI attaches a navigation object to every alert; Tk 9.0's autorelease pool then frees it twice, and the app stops with SIGTRAP as soon as the alert closes. A 10-line Tk script with one `showinfo` crashed the same way, so every message in the app could do it, not only the update check.
- **Fix: every message is now a window drawn by Tk** (`show_dialog` and `messagebox` in `ui_components.py`), with the same calls and answers as `tkinter.messagebox`: showinfo / showwarning / showerror / askyesno / askokcancel. Return presses the default button, Esc cancels, long text scrolls. The app never opens a native alert any more. File and folder pickers stay native (they are not affected). The "missing Python package" window at start-up is a plain Tk window too.
- **Update Available** has its own buttons (Later / Open Download Page) and shows the release notes as plain text in a scrolling box.
- The "latest version" answer was correct: the only release so far is a pre-release of the same version, and pre-releases are ignored.
- Verified: the same real key presses that crashed the native alert (Return / Esc via System Events) closed all six kinds of Tk dialog with the right answers and the app kept running; screenshots checked. 390 tests run, all pass except the one that needs the real broken files (3 new dialog tests).

## 261121.2215 — Check for Updates; moved to the AudioPostTools repository

- **Check for Updates.** The application menu (next to About and Quit) has **Check for Updates…** and **Check for Updates Automatically** (on by default). The automatic check runs quietly a few seconds after launch, at most once a day, and only speaks up when there is a new version. It reads the releases of `hsuanice/AudioPostTools` on GitHub, looks only at this app's tags (`wrfp-v<version>`), and ignores drafts and pre-releases. A new version shows what changed and opens its download page; the app never replaces itself. The request goes through macOS's `/usr/bin/curl` (macOS's own certificates, so it also works inside the .app) and sends nothing about the user. State: `update_check.json` in the app's Application Support folder. Shared code: `operations/update_check.py`, `UpdateChecker` in `ui_components.py`.
- **New home: the AudioPostTools repository** (`~/Developer/AudioPostTools`, published on GitHub). `wave_repair.py` was `WAV_Repair.py`; the changelogs moved to `Changelogs/`. Launch scripts now pick the first Python 3 that has tkinterdnd2 and openpyxl (or `AUDIO_POST_TOOLS_PYTHON`) instead of a fixed path on one Mac. The Soundminer "Send to" scripts are no longer maintained (removed); the Post Tools Inbox itself is unchanged.
- Release tool `.dev/release.py`: checks that the commit is on GitHub, runs the tests, builds the .app, zips it with `ditto`, writes this version's notes, and opens GitHub's new-release page filled in.
- Verified: the built Wave Manager .app ran its automatic check against GitHub (no release yet, so it stayed quiet and recorded the check); a driven dialog with a mocked newer release showed the notes and opened its page; the same version answered "is the latest version". Full suite passes (6 new update-check tests).

## 261003.1312 — Soundminer → Diagnose; "Inspect" is now "Diagnose"

- **"Wave Repair — Diagnose"** (Soundminer script): one selected file opens in Diagnose File. Several are added to the Browse list; what is already there stays. Paths that are missing or are not WAV files are counted and named in the status line and the Log.
- **Wording:** Wave Repair "diagnoses" and Wave Manager's Audio Inspector "inspects", which keeps the two tools' roles distinct (user's choice). The mode and its texts changed: Inspect File → **Diagnose File**, "Inspecting…" → "Diagnosing…", "Open in Diagnose File", "Export diagnosis report", "Nothing diagnosed yet", "Still diagnosing…".
- **Post Tools Inbox** (`operations/post_tools_inbox.py`, shared): `~/Library/Application Support/Post Tools Inbox/`, a folder that belongs to no single app, so later tools can share it. A sender writes a plain-text *batch* there, with `#app=`, `#action=` and one file path per line. A running app checks the folder about once a second; that is one directory listing, and nothing is read until a batch appears. The app takes only the batches addressed to it. Because Soundminer gives no "last record" call, a batch counts as complete once it has stopped growing for 1.5 s. Handled batches move to `Received/` (the last 50 are kept). Batches nobody handled within 10 minutes are moved there unopened, marked "(expired)". A file named `Paused` in the inbox stops all receiving. While an app is busy (Wave Repair writing a repair), a batch stays in the inbox and is retried. `python3 -m operations.post_tools_inbox launch <app>` starts an app unless it is already running. "Running" is checked by the process ID and command line the app registered, so two copies are never started, even while one is still opening.
- **Soundminer scripts** (in the Soundminer Support folder's `Scripts/`): "Wave Repair — Diagnose" and "Wave Manager — Audio Inspector", plus the shared helper `_hsuanice_post_tools.lua`. Soundminer runs the script once per selected record. Each record's FilePath is added to one batch, and the app is launched once per batch, from source for now; it will switch to the .app once stable. Every record returns nil, so nothing in Soundminer or the files is changed. Rescanning metadata after a tool has changed a file is left to Soundminer's own "file changed — rescan?" prompt (user's choice). A read-only probe run in Soundminer 6.2 first confirmed Lua 5.1 with `io.popen`/`io.open` available, one call per record, and FilePath plus metadata fields in each record.
- Tests: `tests/test_post_tools_inbox.py` (11). Among them, the real Lua sender script is run with a local `lua` and its batch is read back by Python, including a path with an apostrophe. A GUI drive of both apps under a real Tk main loop with a temporary inbox gave the results described above. Full suite: 365 passing.

## 261002.1942 — Recover audio: power loss, stale headers, cut-short copies

The user asked why the most common real failure — a recording interrupted by power loss, data size stuck at 0 / 0xFFFFFFFF with all the audio still in the file — couldn't be repaired. It now can, together with two related cases, each confirmed per file (user's choices):

- **Recover audio** (`operations/audio_recover.py`). The audio region is measured from the file itself: from the data chunk to the first metadata chunk that chains cleanly to the end of the file (recorders that write bext/iXML after the audio), or else to the end of the file. The header's data size and RIFF size are then rewritten to match. Nothing is removed or moved; an incomplete last sample frame is completed with zero bytes appended at the end (user's choice; at most one frame, inaudible).
  - **Recording interrupted**: data size 0 / 0xFFFFFFFF. All audio recovered.
  - **Header updated only partway**: bytes follow the declared end. Shown with **evidence**: the extra region's length and peak level next to the audio before it, plus an explicit "far more likely junk" note when the extra is under 10 ms. Similar levels mean the rest of the recording; a handful of bytes at a very different level mean junk. On the corpus: "1.00 s at −18.0 dBFS vs −18.0 dBFS" vs "9 bytes at −1.9 dBFS vs −18.0 dBFS".
  - **Copy cut short**: what's there becomes playable; the missing tail is reported as lost for good, with the advice to re-copy from the source if it still exists. This is repeated in the backup dialog.
  - Not handled yet: RF64/BW64 (the ds64 sizes would need rewriting), and files with any other structural problem alongside.
- **Same workflow as Rebuild iXML.** Click the file and read the new **Recover audio** tab under the list (what was measured, evidence, result, exactly what changes), then tick **Confirm this recovery**. Confirmations are saved per file (tied to the file's signature, like drafts) and survive closing the app. The Repair column shows "Recover audio" or "Recover — ✓ confirmed". The second toolbar row now has three groups: Safe repairs / **Recover audio: Recover All… · Recover Selected…** / Rebuild iXML. Inspect File has **Recover Audio…**, and the right-click menu has "Recover Selected (n)…". Unconfirmed files are named and skipped. Plans are re-measured right before writing.
- **Writing:** backup copied and verified first; backup categories "Recovered audio (header never finalized)", "Recovered audio (header not updated)" and "Made playable (copy cut short)". Then the data size and RIFF size are written and zeros appended. Afterwards it proves that every original byte except those two 4-byte fields is unchanged, that the appended bytes are zeros, that no data-size, RIFF-size or alignment problem is left, and that the audio now reads as exactly the planned number of frames. Any failure restores the backup.
- **Corpus:** the six data-size cases now expect "Recover audio"; 39/39 as expected. Verified under a real Tk main loop on a corpus copy: five confirmed files recovered and inspect clean, with soxi reading the full 2.000 s for every power-loss and stale-header file (1.000 s for the cut-short copy, as expected); the unconfirmed junk case was left byte-identical.
- Tests: `tests/test_audio_recover.py` (12: the three kinds, frame completion, metadata after audio, evidence, junk hint, cut-short warning, refusals, byte-level proof, restore on verification failure). Full suite: 354 passing.

## 261002.1912 — Plain wording, a broken-file corpus, stray trailing bytes

- **The UI no longer talks in phase numbers.** Those were development-plan terms that users can't see, and the inspection (the old "Phase 1") has no button at all. The UI now names what happens:
  - the header line reads "Safe repairs and Rebuild iXML";
  - the action row reads "Safe repairs:" and "Rebuild iXML (review each file below, then write):";
  - the Repair column says **"Not repairable yet"** instead of "Phase 3";
  - the report and dialogs say "not repairable yet — Safe repairs won't touch this file", or "review it under Rebuild review".
  Docs and changelogs keep the phase numbers.
- **New: a corpus of broken files modelled on common real-world failures.** `tests/make_broken_corpus.py` (18 MB) generates 39 cases from scratch. They use playable sine tones with Sound Devices- or Cantar-style metadata, and each is broken in one realistic way, grouped by cause:
  - recording interrupted: data size 0 or 0xFFFFFFFF, header updated partway, metadata after unfinalized audio;
  - copy/transfer: cut mid-audio or mid-iXML, first 4 KB zeroed, trailing junk or zeros, AppleDouble, empty file, header-less PCM;
  - writer bugs: RIFF size too small or too large, missing final and in-between pad, non-zero pad, misaligned audio, bad block align or byte rate, duplicate data, fmt after data, malformed or cut-short iXML, short bext;
  - bit-level damage: chunk ID or chunk size;
  - large files: healthy RF64, RF64 without ds64, wrong ds64 size, and a >4 GB plain RIFF with wrapped sizes, written as a sparse file;
  - a Rebuild iXML card: one cut-off take mid-day, and one at the end of the day whose file name was changed by hand;
  - healthy controls, and an out-of-scope case (a silent hole inside the audio, which is structurally fine and undetectable by design).
  Each case records the issue codes the Inspector must report and what the app should offer. The run prints a pass/fail table and writes `README.md` (in 臺灣華語) and `manifest.csv` into the corpus. Result: **39/39 as expected**. On a scratch copy, the two "Will repair" cases repaired clean; the mid-day cut-off planned a track-name conflict; the hand-renamed take planned SCENE and TAKE conflicts (bext vs file name).
- **Found by the corpus, fixed: stray bytes after the last chunk read as a broken structure.** Junk of 8 bytes or more appended after the final chunk was reported as an "invalid chunk ID … structure breaks" error. If fmt and data are already in place and no recognizable chunk follows, it is now a "stray byte(s) after the last chunk" warning. It is still only a structure break when real chunks follow. Junk directly after the audio stays genuinely ambiguous: it could be audio the header never counted, a damaged chunk header, or stray data. That message now names all three possibilities instead of two.
- The real-file baseline is unchanged: 6,547 WAVs (this Mac plus the real ISO POLY), only the 447 known info notes. Full suite: 342 passing.

## 261002.1822 — Review first, write later: Phase 3 drafts

The user's redesign after rebuilding the real UB4731/UB4783: the sequential flow made them wait for each next review to pop up, gave no way back to a file already reviewed, and wouldn't scale to 5–10 files. Reviewing and writing are now separate:

- **Click a file, review it right under the list.** For a file whose iXML can be rebuilt, the area under the file list (and in Inspect File) now has **Report / Rebuild review** tabs and opens on the review. It has the same table as before: previous take | value to write | next take, audio evidence, confidence, source, review state, plus Confirm Selected, Confirm All Inferred, right-click to change any row, and a new **Reset This File's Review**. There is no pop-up window any more. The related takes are read in the background the first time a file is shown.
- **Every edit is a draft, not a write.** Review files in any order, switch between them, and go back and change one; nothing touches the file. The Repair column shows each file's state live: "Rebuild iXML" (not opened yet), "Rebuild — n to review", or "Rebuild — ✓ reviewed".
- **Drafts survive closing the app** (user's choice). They are saved on every change to `~/Library/Application Support/Wave Repair for Post/rebuild_drafts.json` (`operations/rebuild_drafts.py`). Each draft carries the file's signature (size + modification time); a draft for a file that has changed since is dropped, never applied to different bytes.
- **Write at the end: Rebuild All… / Rebuild Selected…**, in a new second toolbar row that groups the actions as "Phase 2 — safe repairs: Repair All / Repair Selected" and "Phase 3 — rebuild iXML: Rebuild All / Rebuild Selected". Inspect File has **Write Rebuilt iXML…**, and the right-click menu has "Rebuild Selected (n file(s))…". Only **reviewed** files are written. Unreviewed ones are named in the backup dialog ("1 file(s) not reviewed yet will be skipped: …") and left untouched, with their drafts kept. It all happens in one backup location and one run. Before writing, each draft is checked against a fresh plan, so a draft that no longer matches the related takes is skipped with a log line instead of being written. Written files' drafts are discarded.
- The Repair column is wider (the review states were cut off). The event pump is now cancelled on close, so closing never fires a callback against a destroyed window.
- Verified under a real Tk main loop on a synthetic card with two cut-off takes:
  - Reviewed UB4703 fully; the column showed "✓ reviewed".
  - Half-reviewed UB4705; the column showed "1 to review".
  - Went back to UB4703: its choices were still there, and one more value was changed.
  - Closed and reopened the app: both states were restored.
  - Rebuild All: the dialog named UB4705 as skipped; UB4703 was written with every choice (including the late change) and inspects clean; UB4705 was byte-identical and its draft kept.
- Tests: draft review state and final choices, persistence, and dropping a draft when its file changes. Full suite: 342 passing.
- Snapshot of the previous, user-confirmed state: `_backup/261002.1732----Phase 3 rebuild (sequential review) ok/`.

## 261002.1732 — Rebuild iXML from Browse, for several files at once

- **New "Rebuild iXML…" button in Browse's toolbar.** Before, Browse only offered it on the right-click menu, and only with exactly one file selected; the visible button lived in Inspect File. So with a whole folder loaded (the real ISO POLY: 1,346 files, 2 needing Phase 3) there was no visible way in. The button works on the selected files that need it, or, with none of those selected, on every file in the list whose Repair column says "Rebuild iXML".
- **Several files in one go.** Each file is planned in the background and reviewed in turn ("Rebuild iXML (1/2) — …"). Cancelling one review skips just that file. Then there is **one** backup-location dialog for all of them, and **one** run writes them: one backup run folder and one Repair Log, with the same progress and Execution Log lines as Repair. The engine gained `riff_repair.run_rebuilds()`; `run_rebuild()` is now the one-file case of it. The right-click item also accepts several selected files ("Rebuild iXML (n files)…").
- The header line now names both phases.
- Verified under a real Tk main loop on a synthetic card with two cut-off takes and no selection: two review dialogs in sequence, one backup dialog, both files written and inspecting clean with the right TAKE, and both backups in a single run folder. New test for the shared run folder. Full suite: 340 passing.

## 261002.1637 — Fix: REAPER couldn't read a rebuilt iXML at all (NUL padding)

**Symptom (user, in a fresh REAPER project):** for a rebuilt UB4783, REAPER showed **no** PROJECT / TAPE / SCENE / TAKE, and every empty track resolved to "CMIT 5U". The healthy neighbour UB4782 in the same project read correctly. The Audio Inspector (via BWF MetaEdit) showed the rebuilt file as fine.

**Cause:** Cantar fills the unused part of its reserved iXML space with **spaces (0x20)**: 1,023 of 1,023 reserved-size Cantar iXMLs in the real project do. The rebuild filled it with **NULs**. BWF MetaEdit ignores trailing NULs, but a strict XML reader treats the NULs after `</BWFXML>` as invalid, rejects the whole document and returns no iXML at all. "CMIT 5U" on empty tracks was a downstream effect: with no readable iXML in REAPER, the metadata library's single-name fallback (first non-empty name) took over. (It still found the four real track names through BWF MetaEdit.) The real-answer checks didn't catch this because they read the iXML with trailing NULs *and* spaces stripped, which made the two fillers look the same.

**Fix:** the rebuilt iXML is padded with **the template recorder's own filler byte** (`padding_byte()`, read from the template's chunk), the same principle as copying its formatting. Verified with a strict whole-chunk XML parse, the way a strict reader sees it: healthy UB4782 (spaces) OK; the earlier NUL-padded rebuild **rejected** at the first padding byte; the fixed rebuild (spaces) OK. The one other NUL-padded reserved iXML in the real project is the hand-made `UB4783…_repaired.WAV` from 2026-09-12, which has the same problem.

Also corrected: the previous entry blamed REAPER-side caching. Caching was not the cause.

The user's test copy of UB4783, rebuilt with NUL padding, was restored from its verified backup (identical to the original cut-off file) for re-testing. Tests: space-padded template → space padding with no NUL anywhere; NUL-padded template → NUL padding. Full suite: 339 passing.

## 261002.1602 — Fix: empty track names read as "CMIT 5U"; backups beside the source folder

**Bug (found by the user in REAPER): after rebuilding UB4783, every empty track name showed up as "CMIT 5U".**
- Cause: the rebuilt iXML wrote empty elements as `<NAME/>`, while the recorder writes `<NAME></NAME>`. Both are valid XML, but a real reader of track names then fell through to another track's name. The serializer had normalized empty elements to one form (`<X/>`, from the 2026-01-13 card) instead of following each recorder.
- On top of that, choosing "leave out" for a track name *removed* that TRACK's `<NAME>` element entirely, leaving a TRACK without the field every other track has.

Fixes:
- **The rebuilt iXML is written exactly the way the template recorder wrote its own.** The text before the root element is kept verbatim (one real file has its declaration and `<BWFXML>` on one line). The recorder's line breaks and indentation are kept as parsed, with no re-indenting. Empty elements take the recorder's form per tag: `<X></X>`, `<X/>` or `<X />`, whichever it uses. Verified with a byte-for-byte round trip on every iXML in the real project: all **1,252** Cantar iXMLs reproduce identically.
- **"Leave out" is now "leave empty"**: the element stays, empty. ⚪ fields are written empty too, the same way. No element is ever removed, so every TRACK keeps its full set of fields.
- Re-verified on a copy of UB4783 with the user's choices (all three uncertain fields left empty): 12 TRACKs, each with a `<NAME>…</NAME>`: CMIT 5U, Trk 2, eight empty, MIX L, MIX R. There is no `<NAME/>` anywhere, the file inspects clean, and BWF MetaEdit reads it in strict mode.
- The user's test copy of UB4783, already rebuilt with the bug, was **restored from its verified backup** (SHA-1 identical to the original) so the test can be run again.

**Backups go beside the source folder, as the user expects.**
- 261002.1342 made the default climb past "day folders" on its own, so that a single file handed over from Wave Manager wouldn't back up inside Wave Manager's Source. In a test tree it climbed three levels, to `<drive>/_TEMP - Wave Repair Backups`. No more guessing.
- Files opened directly: the backups go beside the files' own folder, e.g. `Broken Files copy/2026-01-14.AAN - Wave Repair Backups`.
- A folder opened in Browse: beside that folder.
- **Handed over from Wave Manager**: Wave Manager now leaves a small note naming its Source folder (`~/Library/Application Support/Wave Repair for Post/handoff.json`, files + Source + time). The backups then go beside *that* folder, never inside it where Wave Manager would rescan them as takes. The note is only used if it is under 15 minutes old, lists every received file, and those files really are inside that Source folder.

Tests: a regression test that a left-empty track name keeps every TRACK complete with `<NAME>…</NAME>` and no self-closing NAME; the left-empty behavior for ⚪ fields; backup-location tests for the files' folder and a known scan root; hand-off note tests (match, not listed, outside the source, stale, missing). Full suite: 337 passing.

## 261002.1540 — Audio evidence, edit any field, neighbour rules, two UID conventions

From testing UB4783 (which has takes on both sides) in `_TEMP/SOX TEST/Broken Files copy/2026-01-14.AAN`:

- **Audio evidence per channel.** A new **Audio: prev / this / next** column shows each channel's peak level, or "silent", in the previous take, this file and the next take, on every track row. It reuses `audio_qc.compute_wav_channel_peaks()` (Wave Manager's waveform peaks, 600 samples across the file, ~0.2 s per file). On UB4783 it matches what REAPER shows: channels 1, 2, 11 and 12 carry audio; channel 3 is silent, like the previous take, where it's unnamed, while the next take records it and names it "WU". On a track-name conflict, the option whose take matches this file's channel (silent / recording) is labelled "— like this file, channel n is silent there", and the Source says so.
- **Track names are always listed** in the default view, even 🔵 ones. Before, only 🟡/🔴/⚪ rows showed, which made it look as if only track 3 was being considered. Tracks 1/2 (prev and next agree: CMIT 5U, Trk 2) and 11/12 (MIX L/MIX R all day) were already filled in as 🔵.
- **Right-click any row to change it, any time.** This includes rows already chosen or confirmed, ⚪ rows left out by default, and 🟢/🔵 rows. Choices: the proposed value, the previous take's value, the next take's value, leave out, or **Type a value…**. Fields changed without needing review show "✎ changed", and every such change goes into the Execution Log and Repair Log as "✎ changed by the user". Before, a 🔴 that had been chosen, and any ⚪, could not be changed.
- **FAMILY_NAME and the iXML ORIGINATOR_REFERENCE from the neighbouring takes** when no rule holds for the whole day. 2026-01-14 has a block (scene 37) whose iXML is in the "edited" style although its bext isn't, so the day-wide rules failed and both fields were left out. They now fall back to the takes either side: both agreeing gives 🔵, one gives 🟡. UB4783 gets `25/3/    t2` (previous `25/3/    t1`, next `25/3/    t3`), as the user worked out.
- **Two UID conventions on one card.** The scene-37 block stamps FILE_UID/FAMILY_UID with the **start** time; normal takes use the **close** time plus the drifting recorder-clock offset. The convention is taken from the neighbouring takes. Both using start time: this file's own start time. Both using close time: the drift fit, now restricted to takes in the dominant UID layout, with outliers dropped and refitted. One of each (the file sits where the recorder switched): a 🔴 offering both. Measured on the real cards, every 🟡 UID estimate is within −3…+7 s; before this, the scene-37 block was off by up to 332 s. UB4783: close-time estimate ±1 s from 40 takes.
- **The same recording is never its own neighbour.** A take with the same bext TimeReference as the target, such as a hand-made `…_repaired.WAV` copy, is excluded from the related takes.
- The real-answer check is unchanged where it matters: 🟢 2,024/2,024, 🔵 26,285/26,306.
- Tests: audio-matching hint on a track-name conflict, FAMILY_NAME from the neighbours when the day rule breaks, a user override of any field (including a ⚪ one) being written, and a same-recording copy excluded. `test_ixml_rebuild.py` now has 15 tests; full suite 335 passing.

## 261002.1424 — Review column order; renamed takes; filename vs bext check

- **Review column order** is now Field | **Previous take** | **Value (will be written)** | **Next take**, so the value sits between the takes either side of it. Confidence, Source and Review follow.
- **ORIGINAL_FILENAME stays 🟡, with the day's real rename rate.** The user asked why the previous take showed "UB4730==23 5 t3". It is correct: UB4730 was recorded as 23/5 t3 and renamed to 23/6 t1 on the recorder afterwards, and iXML ORIGINAL_FILENAME keeps the first name. On a rushed set the scene/take often isn't changed in time and gets fixed afterwards. I tried promoting the current name to 🟢 when the take's bext looks unedited, but the real card disproved that: UB4701 and UB4712 were renamed with no trace in their bext. So it stays 🟡, and the source now says how many comparable takes that day kept their first name (UB4731: "44 of 46 … 2 had been renamed on the recorder, which can't be detected from the file").
- **New: the file name is cross-checked against the bext scene/take.** Fixing scene/take afterwards by renaming the file by hand leaves the bext saying the old values. If the name and the bext disagree, SCENE and TAKE become 🔴 conflicts offering the bext value, the file-name value, or leaving the field out. The file-name layout is learned, not assumed: Cantar's `UB4731==23 6     t2==PN.WAV` layout is only used once it reproduces the iXML SCENE/TAKE of at least 90% of the related takes. It matches 58/58 on 2026-01-13 and 55/55 on 2026-01-14. On the real data this raised no false conflicts, and UB4731's name agrees with its bext, so its SCENE/TAKE stay 🟢.
- Tests: hand-renamed file → SCENE/TAKE conflicts with both values offered; name agreeing with bext → stays 🟢. Full suite: 331 passing. The real-data check is unchanged: 🟢 2,041/2,041, 🔵 26,442/26,455.

## 261002.1413 — Rebuild review: previous/next take columns

- The **Rebuild iXML review now shows the neighbouring takes' own values side by side**: new **Previous take** and **Next take** columns. Their headings name the files, e.g. "Previous take — UB4730==23 6  t1==PN.WAV" or "Next take — (none that day)". Each row shows what those takes have in that field, next to the value that will be written, so confirming a 🟡 or choosing a 🔴 can be judged at a glance instead of from the Source text alone. A field a neighbouring take doesn't have shows "(not in this take)".
- A one-line **legend** explains each confidence, including ⚪ (not written: an ID unique to every file that can't be worked out, so copying another take's would be wrong).
- A 🔴 conflict not yet decided now shows "(choose…)" as its value instead of "(left out)", which read as if leaving it out had already been decided.
- Verified with screenshots of the real UB4731 (previous take only) and a synthetic card with a real previous/next conflict. Full suite: 329 passing.

## 261002.1342 — Phase 3: rebuild a missing iXML (UB4731)

**What it's for**: a file whose final iXML header was written but whose reserved contents never were. This is the real Cantar power-loss case, UB4731: audio and every other chunk intact, the iXML header declaring 61,440 bytes, nothing after it. The rebuilt iXML is written **into exactly that reserved space**: the bytes are appended after the header, the RIFF size already counts them, and no existing byte changes. The repaired file ends up with exactly the layout of a healthy Cantar file. Everything else stays out of scope for now, including the 14 RODE Wireless PRO files with no metadata at all (user's decision).

**How each field is decided** (`operations/ixml_rebuild.py`). The structure is copied from the nearest related take: same folder (one day/card), same recorder (bext Originator), same channel count, readable iXML, ordered by timecode. Each field then gets a provenance:
- 🟢 **Original**: read from the file's own bext Description (SCENE, TAKE, TAPE, UBITS, NOTE, SPEED), bext TimeReference/OriginationDate/Time/CodingHistory, fmt (rate, bit depth, channels), or derived from those by a rule that reproduces every related take's real value: FAMILY_NAME ("SCENE/TAKE" padded, with the alignment learned from the data) and the iXML BWF_ORIGINATOR_REFERENCE (bext reference with the recorder's fixed suffix).
- 🔵 **Recovered**: identical in every related take, or the previous and next takes agree.
- 🟡 **Inferred**: only one neighbouring take to go by; ORIGINAL_FILENAME (a take renamed on the recorder keeps its first name there, so the current name is a guess); and Cantar's FILE_UID/FAMILY_UID. Their time component is the recorder clock when it *closed* the file, which a power-cut file never did, so it's estimated from the file's end plus the recorder-clock offset. That offset drifts through the day (+108 s in the morning to +54 s in the evening on the real card), so it's a straight-line fit over the related takes.
- 🔴 **Conflict**: the previous and next takes differ. You choose either value, or leave the field out.
- ⚪ **Omitted**: per-file identifiers with no rule (Cantar's CANTAR_KEY / CANTAR_KEY_ID) and fields that differ in nearly every take. They are never copied from another take, because two files would then share one ID.
- Values that differ only in leading/trailing whitespace count as the same: the recorder pads names inconsistently ("WU" / "WU  ", "" / " "), and treating those as different produced dozens of false conflicts.
- Rules are learned only from takes of the **same kind** as the target. Takes whose metadata was edited on the recorder afterwards get a rewritten bext ("AATCAN…0002" instead of "AATON  UB…0000") and different iXML patterns. 12 of the 58 on 2026-01-13 are like that.

**Measured against real answers before writing anything.** Every healthy take on 2026-01-13 and 2026-01-14 was rebuilt as if its iXML were missing, then compared field by field with its real iXML:
- 🟢 Original: **2,041 / 2,041** correct.
- 🔵 Recovered: **26,442 / 26,455** correct. The 13 misses are values that genuinely changed on that one take.
- UID time estimate: within **−3…+7 s**.
- 🟡 and 🔴 fields are the ones that genuinely vary, which is why they go to review.

This measurement found and fixed four real problems before any file was written:
- (1) Repeated recorder blocks (Cantar's 24 `ALL_TRK_NAME/DATA` entries) collapsed onto one path, so a rebuild overwrote entry 1 with entry 24. Every repeated sibling tag is now numbered.
- (2) The iXML BEXT section is not a copy of the bext chunk: ORIGINATOR is padded, ORIGINATOR_REFERENCE has a different suffix, VERSION differs. The earlier hand-repaired `UB4783…_repaired.WAV` copied them, so it is wrong in those fields.
- (3) The UID time is not the start time. The earlier hand repair used the start time.
- (4) ORIGINAL_FILENAME is not always the current name.

**Review dialog** ("Rebuild iXML — <file>"):
- It lists the counts per confidence, the previous and next takes used, and every field with its value, confidence, source and review state. By default it shows only fields needing attention; "Show all fields" shows the rest.
- 🟢🔵 fields are accepted automatically.
- 🟡 fields: double-click, or Confirm Selected / Confirm All Inferred, to confirm. A right-click offers "leave out".
- 🔴 fields: double-click to choose either take's value, or leave the field out.
- **Write Rebuilt iXML…** stays disabled until nothing is left to review. Then comes the usual backup-location dialog.
- A `HISTORY/WAVE_REPAIR_NOTE` in the rebuilt iXML records that it was rebuilt, when, from which takes, and how many fields were inferred, chosen or omitted (user's choice of location). Every inferred, chosen or omitted field, with its source, goes into the Execution Log and the Repair Log.
- The recorder's own formatting is kept: its XML declaration line, `<TAG/>` for empty elements, one-space indentation, and NUL padding to the reserved size.

**Writing** (`riff_repair.run_rebuild`):
- Same safety sequence as Phase 2: backup copied and verified first (backup category "Rebuilt iXML from related takes").
- It then proves that every original byte is unchanged, the appended bytes read back identically, and the file inspects clean. Any failure restores the backup.
- Only the exact Phase 3 case is accepted (`rebuild_target_problem`): last chunk an iXML header with nothing after it, and the RIFF size counting exactly its reserved size. Anything else is refused.
- In the app: the Repair column shows **Rebuild iXML** for these files. There is a **Rebuild iXML…** button in Inspect File and a right-click item in Browse. The related takes are read in the background.

**Backups beside the project, also for single files.** A single file handed over from Wave Manager used to back up to `ISO POLY/2026-01-13.AAN - Wave Repair Backups`, which is *inside* the project, so Wave Manager would rescan the backups as takes. The default now climbs past day/card folders: a folder whose siblings also hold WAVs is one card of a bigger set. On the real project that gives `LOCATION/ISO POLY - Wave Repair Backups`. The dialog also refuses any location inside that project folder.

**Verified end-to-end on a copy of the real UB4731** (its 58 same-day takes linked in read-only):
- The Repair column showed "Rebuild iXML".
- The review dialog listed 26 🟡 fields, with Write disabled until Confirm All Inferred.
- After writing, the file inspected **OK** with SCENE 23/6, TAKE t2 and all tracks.
- BWF MetaEdit reads its iXML in strict mode, and soxi reads the full 179.76 s.
- A line diff against the template take UB4730 shows only the intended fields changed.

Tests: `tests/test_ixml_rebuild.py` (9) on a synthetic Cantar-style card. It covers provenance of every kind, a real previous/next conflict, whitespace-only agreement, repeated blocks keeping their own values, choices and "leave out", padding to the reserved size and refusing to exceed it, the append-only write proof, and refusal of ineligible files. Plus 2 new backup-location tests. Full suite: 329 passing.

## 261002.1319 — Same Execution Log window as Wave Manager

- **The log is now Wave Manager's own Execution Log window**, at the user's request so the two tools read as one family. It has the same title ("Execution Log"), the same layout (an "Execution log" header with the live status line beside it, and the dark monospaced text area), and the same size and close-to-hide behavior. `ExecutionLogWindow` moved from Wave Manager into `ui_components.py` and both apps create it, so it's literally the same code and can't drift. It replaces the separate "Repair Progress" window from 261002.1233.
- Repair progress follows Wave Manager's pattern:
  - The live status shows in the log's header and in a new status row at the bottom of the main window: `Repairing 38/92 — <file> — verifying backup… · 37 done · elapsed · about … left`.
  - Each file gets a `[repair n/total] ✓ / — / ✗ <file> | …` log line, in the same style as Wave Manager's `[preview n/total]` lines.
  - The byte-based **progress bar** and **Open Backup Folder** button now live in that bottom row. Wave Manager's log window has no progress bar, so it stays out of the shared window.
  - Scans are logged too: `=== Inspect n file(s) in …` and the summary.
- Fixed while verifying: **the bottom summary line (and the new status row) could disappear entirely.** They were packed after the expanding report area, so a window that wasn't tall enough gave them zero height. They are now reserved at the bottom first and are always visible.
- Full suite: 318 passing.

## 261002.1304 — Repaired files get the repair time; no column snaps back

- **A repaired file's modification time is now the time of the repair**, decided by the user after FreeFileSync refused to copy backups back over repaired files. Keeping the original time made each pair the same time but a different size, which FreeFileSync flags as a conflict it won't resolve. Sync and backup tools now see the repaired file as the newer one. The **backup keeps the original modification time**, and the recording's own time lives in bext, so nothing is lost. Restoring from a backup (`_restore`, `copy2`) brings the original time back along with the original bytes.
- **Status and Repair columns no longer snap back when resized.** The previous fix left Status as the single stretch column, and ttk re-divides leftover width among stretch columns on every layout pass. That reverted any drag on Status, and on Repair too, since Repair's left edge is Status's right edge. No column stretches now: every width stays where it's dragged, and anything wider than the window scrolls horizontally. Verified: after setting File/Folder/Status/Repair to new widths and resizing the window three times, all four kept their widths.
- Tests: the in-place repair test now checks that the repaired file gets a new modification time and the backup keeps the original. Full suite: 318 passing.
- Snapshot of the previous, user-confirmed state: `_backup/261002.1233----Wave Repair Phase 2 ok/`.

## 261002.1233 — Repair progress, Repair Selected, multi-select, column widths

Four things reported after the first real repair run:
- **Repair now shows progress and a log.** It used to show only a one-line status at the bottom of Browse, so there was no way to tell how far along a long batch was or what it was doing.
  - A new **Repair Progress** window opens automatically when a repair starts. It shows the current file (n/total) and its stage (backing up / verifying backup / repairing / verifying repair), a progress bar, files done, elapsed time and an ETA, plus a running log line per file (✓ repaired, — not touched with the reason, ✗ failed with the reason).
  - When the repair finishes it shows a summary and an **Open Backup Folder** button. This replaces the old modal "open the backup folder?" question.
  - A **Log** button in both Browse and Inspect File reopens the window.
  - The progress bar and ETA are byte-based, because files range from tens of MB to over half a GB and a per-file average would swing wildly.
  - The engine gained `step` and `finished` callbacks in `run_repairs()` and a `step` callback in `repair_file()` to drive this.
- **Repair All… / Repair Selected…** replace the single Repair… button in Browse. The table now allows multi-select: click, Shift-click for a range, Command-click to toggle (Tk 9 binds `<<ToggleSelection>>` to Command-click on macOS). The right-click menu offers "Repair Selected (n repairable of m)…", and the detail pane follows the row last clicked.
- **Resized columns no longer snap back.** This is the same ttk behavior Wave Manager already fixed for its own tables: every `stretch=True` column gets its width redistributed on each layout pass, overriding a drag-resize. `configure_treeview_column_widths()` moved from Wave Manager into `ui_components.py`, and both apps now use it. Only Status absorbs leftover width. The table also gained a horizontal scrollbar.
- The **Folder column is relative** for a batch of files (e.g. 93 handed over from Wave Manager): it shows `2026-01-15.AAN` instead of the full absolute path on every row, and the line above the table names the common root.
- The progress bar is now actually visible. The `clam` theme's default bar fill is the window background color, so a full bar looked empty. Fixed in the shared theme.

Tests: byte-based ETA, duration formatting, and the order of the per-stage callbacks. Verified under a real Tk main loop: Repair Selected repaired exactly the 2 selected repairable files and left the selected Phase-3 file and every unselected file alone; a dragged Folder width survived window resizes; the Folder column showed date folders for a handed-over batch.

## 261002.1210 — Phase 2: safe repair with categorized, verified backups

**Repair** (`operations/riff_repair.py`). Phase 2 only does repairs that are 100% certain and never move or remove an existing byte. A file with any problem outside that set is **not touched at all**: Phase 2 never strips information from a file, and such files wait for Phase 3.
- **Safe repairs:**
  - `missing_final_pad`: append the missing zero byte.
  - `riff_size_mismatch`: rewrite the RIFF size field, but only when the header says *less* than the file holds (the header simply wasn't updated), or exactly one byte more alongside a missing final pad. A RIFF size *larger* than the file means something the header promised is missing — possibly a whole chunk, as on UB4731 — and "fixing" the number would hide that loss, so it's deferred.
- **Deliberately not in Phase 2:** a pad byte missing *between* chunks. Fixing it would shift every later byte, and recorder-specific chunks (Aaton's included) may hold absolute offsets. Also cut-off chunks, unfinalized/truncated data, frame misalignment, and all of RF64.
- **Per file, in order:**
  1. Copy the original into the backup, hashing it on the way.
  2. Re-read the backup and verify its size and SHA-1.
  3. Only then edit the original in place: append-only, plus the 4-byte RIFF size. The original keeps its name, path and timestamps.
  4. Prove every original byte except the RIFF size field is unchanged (audio included), the only new bytes are zeros, and the file now inspects clean.
  5. Any failure after the edit restores the original from the verified backup. A file that changed on disk since it was inspected is refused. One bad file never stops the batch.
- **Backups**, as the user specified. They go next to the repaired folder, never inside it: anything inside would be rescanned as more takes, as the project's existing `UB4783…_repaired.WAV` already is. The default is `<folder> - Wave Repair Backups/`, chosen or changed in a confirmation dialog on every run.
  - Each run gets a **new folder named to the minute** (`2026-10-02 1430`; a second run in the same minute gets `-2`).
  - Inside it there is **one subfolder per kind of problem** (e.g. `Missing pad byte at end of file (odd-sized last chunk)/`), then the original relative path and the **original filename**.
  - `Repair Log.txt` and `Repair Log.json` record every file's backup path, repairs, reason (for skipped and failed files) and verification steps.
  - The dialog refuses a backup location inside the folder being repaired, and checks free space first.

**App**:
- A new **Repair** column shows "Will repair" or "Phase 3".
- **Repair…** buttons in both Browse (all repairable files in the list) and Inspect File, plus **Repair This File…** on the right-click menu.
- Every report shows a "Repair:" line saying what would happen to that file.
- The confirmation dialog shows how many files fall in each category, names the files that will NOT be touched, and has the backup location picker.
- Repair runs on a background thread with progress. Afterwards only the touched files are re-inspected, and a summary offers to open the backup folder.
- Repair is refused while a scan is still running.

**Real-data verification** (on copies, never the originals): 3 of the 92 Cantar odd-iXML files plus UB4731 were run through `run_repairs`. The 3 were repaired; afterwards BWF MetaEdit can **write** to them and read their iXML in strict mode, both of which it refused before. UB4731 was skipped untouched. The backups landed in the category folder with their original date folders and names. Tests: `tests/test_riff_repair.py` (14 tests: plan rules, backup layout, in-place repair with byte-level proof, deferred files untouched, automatic restore on verification failure, changed-file refusal, mixed batch) and a summary test in `test_wave_repair.py`.

## 261002.1122 — First real damaged files; Wave Manager's look

**First real damaged files**: two Cantar poly takes (`CANTAR 00519 v3.535.C12`, 12 ch, 2026-01-13/14, `UB4731` and `UB4783`). The damage is identical in both. Every chunk is intact — `bext`, `fmt `, Aaton's `film`/`aton`, `data`, `ond1`–`ondC` and `mixr` — and then the file ends right after a final `iXML` header that declares 61,440 bytes, none of which were written. The RIFF size already counts those 61,440 bytes, so the header was written for a complete file and only the last metadata write never reached the disk. The audio is fully intact. The bext Description still carries `aSCENE`/`aTAKE`/`aTAPE`/`aUBITS`, and TimeReference is still there. **Track names are lost**: they only ever live in iXML. `film` holds only TC, date and filename, `aton` is zero-filled, `ond*` are per-channel binary overview data, and `mixr` is mixer data. Recovering track names will therefore need the related-file approach (Phase 3).

Changes from what those files showed:
- A cut-off final chunk now says what actually happened. If none of its contents exist: "iXML header is present but none of its 61,440 bytes were written — the file ends right after the header…". If some exist: "…it was cut off". It is now marked **repairable**, since everything before it is intact. The redundant "iXML chunk is empty" note that used to follow it is gone.
- When the RIFF size overshoots the file by exactly what the cut-off chunk is missing, the RIFF size message now says so ("— the difference is exactly the missing iXML contents"), so the two errors read as one cause rather than two unrelated problems.
- **Fixed a real misdiagnosis found while testing this.** When a cut-off chunk sits *directly* after `data`, it used to be read as "audio continues past its declared size". A valid FourCC that runs past EOF only counted as a chunk boundary for `data` itself. Now any well-known chunk ID does. The real Cantar files didn't hit this only because `ond*`/`mixr` sit between `data` and the iXML.
- The report's Issues section now comes right after the status summary instead of after the chunk list, which on a Cantar file is 19 lines long.

**Same look as Wave Manager for Post**: ttk's light `clam` theme regardless of the macOS appearance (it previously followed Dark Mode), with the same white table rows, text, secondary text and panel colors. Severity colors are taken from Wave Manager's red (`#b91c1c`) with matching amber and blue. The theme setup and the colors now live in `ui_components.py` (`apply_app_theme()` + color constants), and both apps call the same function, so they can't drift apart. The report text now uses the first installed monospace font (SF Mono → Menlo → Monaco). SF Mono isn't installed on this Mac, and Tk silently fell back to a proportional font, which broke the chunk table's alignment.

Tests: 2 synthetic cases recreating the Cantar damage (and a partially written trailing iXML), plus a real-file regression test against the two Cantar files that runs only when that drive is mounted (skipped otherwise). `test_riff_inspect.py` now has 35 tests. The real-file baseline is unchanged: 5,293 local WAVs, 0 errors, 0 warnings.

## 261001.1859 — Phase 1: Inspector (read-only)

First version. Diagnoses structural damage in RIFF/WAVE files and **never writes to a WAV file**. Repair (Phase 2) and cross-file metadata recovery (Phase 3) come later and will act on exactly what this phase diagnoses.

**Core: `operations/riff_inspect.py`** — a damage-tolerant scanner, deliberately separate from `wav_core.scan_wav_chunks()` (which trusts every declared size and is meant for files already known to be well-formed). `inspect_wav(path)` returns an `InspectionReport`: the chunk list (offset, declared size, the size actually usable, pad byte), audio layout, the resolved data region, parsed bext/iXML, and a list of `Issue`s. Each issue has a severity (`info`/`warning`/`error`), a category (container/fmt/bext/ixml/data), a stable `code`, and a `repairable` flag for Phase 2.

What it checks:
- **Container**: RIFF/RF64/BW64 + WAVE IDs. RIFF size against the real file size. RF64 sizes resolved through `ds64` (the old scanner never read `ds64`, so every RF64 file would otherwise have looked broken). Plain RIFF files over 4 GiB.
- **Chunk walk**: never trusts a declared size. A chunk that runs past EOF is reported, and the walk stops there. An invalid chunk ID stops the walk at that offset, with "chunk structure breaks at offset X", and any recognizable chunk signatures after it are listed as **candidates, not confirmed**. Only candidates whose chunk chain lands exactly on EOF are flagged as strong. A damaged ID whose size is still intact (the next chunk is valid) is a separate, milder case: sox and bwfmetaedit both read such a file fine by skipping that chunk, so it's a warning that names what was lost, and says when the contents look like iXML.
- **Padding**: a missing pad byte after an odd-sized chunk is detected by finding the next header one byte early, and the "pad byte" misread there is withdrawn. Also covers a missing final pad, and non-zero pad bytes (info only).
- **data**: placeholder sizes that were never finalized (0, or 0xFFFFFFFF in plain RIFF — the typical power-loss case), declared sizes past EOF (truncated or incomplete copy), audio continuing past its declared size, and frame alignment. When the size can't be trusted, the tail of the file is searched for metadata chunks the recorder wrote *after* the audio, and those are excluded from the audio region rather than counted as samples.
- **fmt**: block align, byte rate, size, and encoding.
- **bext**: minimum size, Description key=value fields (`sTAPE=`, `sSCENE=`, …), and TimeReference shown as SMPTE when the iXML timecode rate is known.
- **iXML**: whether it parses. A truncated document (no `</BWFXML>`) is distinguished from malformed XML. Trailing NUL padding is treated as normal.
- **Cross-checks** (info): iXML TRACK_COUNT against channel count, and bext TimeReference against the iXML timestamp.

Harmless quirks common on real recorders are **info**, not warnings, so the Status column doesn't cry wolf. Validated against **5,290 real WAV files** on this machine (3,911 Cantar, Sound Devices 788T/Mix664, Pro Tools, Premiere, RX, Logic, Resolve, REAPER, …): **0 errors, 0 warnings**. The only flags were 447 info notes for non-zero pad bytes, all in Pro Tools library loops' `DGDA` chunks. Inspection runs at about 3 ms per file.

**App: `WAV_Repair.py`** (Tk + tkinterdnd2, the same dependencies as Wave Manager):
- **Browse**: choose or drop a folder (optionally including subfolders), or add or drop several files. Shows one row per file with Container / fmt / bext / iXML / data status symbols and a summary, colored by worst severity, with sortable columns and an "Only files with issues" filter. The selected file's full report shows underneath. Double-click a row, or right-click → Open in Inspect File, for the full view. Export Report… writes a JSON bundle of every inspected file.
- **Inspect File**: one file's full report, with Open File…, Reveal in Finder, Copy Report, and Export Report… (JSON).
- Routing: dropping or receiving a single WAV opens Inspect File; a folder or several files opens Browse.
- macOS AppleDouble `._*` files (common on exFAT recorder cards) are skipped instead of showing up as "not a RIFF file".
- Files from Finder ("Open With", dropping on the Dock icon) and from Wave Manager arrive as open-document Apple Events via `::tk::mac::OpenDocument`, so an already-running Wave Repair receives them instead of a second copy starting. The window title names the current file or folder.
- Settings (window geometry, last folder, subfolders, issues-only filter) are saved to `~/Library/Application Support/Wave Repair for Post/settings.json`.
- Inspection runs on a background thread. Results come back through a queue drained on the Tk main thread, the same pattern Wave Manager uses.

**Packaging**: added `Wave Repair for Post.spec` (declares WAV and folder document types so Open With and Dock drops work), `Build Wave Repair for Post.command`, and `Launch Wave Repair for Post.command`. Both apps' build scripts now clean only their own outputs: they share `build/` and `dist/`, and the old `rm -rf build dist` would have deleted the other app's build.

**Tests**: `tests/wav_factory.py` builds byte-exact synthetic WAV and RF64 files and breaks exactly one thing each — a size, a pad byte, a header, truncation, or a power-loss-style placeholder. `tests/test_riff_inspect.py` has 32 tests and `tests/test_wave_repair.py` has 13 (routing, launch command, the BLOCKED hint, report bundle, settings). Verified end-to-end:
- a real `WaveRepairApp` driven against a folder of synthetic damaged files;
- a real PyInstaller build launched with `open -a` for a cold launch, a file sent to the already-running instance, and a folder — all three correct, one instance;
- the real hand-off from a running Wave Manager, which launched `WAV_Repair.py` with the file.
