import datetime as dt
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parent))

import wav_factory as wf
from operations import riff_repair
from operations.riff_inspect import inspect_wav
from operations.riff_repair import (
    PLAN_DEFERRED, PLAN_NONE, PLAN_REPAIR, SAFE_REPAIRS, backup_run_folder, default_backup_root, plan_repair, relative_location, run_repairs,
)


def cantar_odd_ixml_file(path: Path) -> Path:
    """The real Cantar quirk: odd-sized iXML as the last chunk, no pad byte,
    RIFF size counting exactly the bytes present."""
    ixml = wf.ixml_payload()
    if not len(ixml) & 1:
        ixml += b" "
    chunks = [wf.chunk(b"fmt ", wf.fmt_payload()), wf.chunk(b"bext", wf.bext_payload()), wf.chunk(b"data", wf.audio_payload(480)), wf.chunk(b"iXML", ixml, pad=None)]
    return wf.write(path, wf.riff(chunks))


def cut_off_ixml_file(path: Path) -> Path:
    chunks = wf.standard_chunks(with_ixml=False) + [wf.chunk(b"iXML", b"\x00" * 61440)]
    full = wf.riff(chunks)
    return wf.write(path, full[: len(full) - 61440])


class RepairTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.project = self.tmp / "ISO POLY"
        (self.project / "2026-02-05.AAN").mkdir(parents=True)
        self.backups = self.tmp / "backups"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def items(self, *paths: Path):
        reports = [inspect_wav(path) for path in paths]
        return [(report, plan_repair(report)) for report in reports]


class PlanTests(RepairTestCase):
    def test_odd_final_ixml_is_safe(self) -> None:
        plan = plan_repair(inspect_wav(cantar_odd_ixml_file(self.project / "a.wav")))
        self.assertEqual(plan.status, PLAN_REPAIR)
        self.assertEqual(plan.category, SAFE_REPAIRS["missing_final_pad"])

    def test_cut_off_chunk_is_deferred_entirely(self) -> None:
        # The RIFF size fix alone would be "safe", but the file also has a
        # problem Phase 2 must not touch — so nothing is done to it at all.
        plan = plan_repair(inspect_wav(cut_off_ixml_file(self.project / "b.wav")))
        self.assertEqual(plan.status, PLAN_DEFERRED)
        self.assertEqual(plan.actions, [])
        self.assertIn("none of its 61,440 bytes", plan.deferred_reason)

    def test_riff_size_smaller_than_file_is_safe(self) -> None:
        data = wf.riff(wf.standard_chunks())
        path = wf.write(self.project / "c.wav", data[:4] + struct.pack("<I", len(data) - 8 - 100) + data[8:])
        plan = plan_repair(inspect_wav(path))
        self.assertEqual((plan.status, plan.category), (PLAN_REPAIR, SAFE_REPAIRS["riff_size_mismatch"]))

    def test_riff_size_larger_than_file_is_deferred(self) -> None:
        # Something the header promised is missing — "fixing" the number would hide it.
        data = wf.riff(wf.standard_chunks())
        path = wf.write(self.project / "d.wav", data[:4] + struct.pack("<I", len(data) - 8 + 61448) + data[8:])
        self.assertEqual(plan_repair(inspect_wav(path)).status, PLAN_DEFERRED)

    def test_missing_pad_between_chunks_is_deferred(self) -> None:
        odd = wf.ixml_payload()
        if not len(odd) & 1:
            odd += b" "
        chunks = [wf.chunk(b"fmt ", wf.fmt_payload()), wf.chunk(b"iXML", odd, pad=None), wf.chunk(b"data", wf.audio_payload(100))]
        self.assertEqual(plan_repair(inspect_wav(wf.write(self.project / "e.wav", wf.riff(chunks)))).status, PLAN_DEFERRED)

    def test_info_only_and_clean_files_need_nothing(self) -> None:
        self.assertEqual(plan_repair(inspect_wav(wf.write(self.project / "f.wav", wf.riff(wf.standard_chunks())))).status, PLAN_NONE)
        odd = wf.ixml_payload()
        if not len(odd) & 1:
            odd += b" "
        chunks = [wf.chunk(b"fmt ", wf.fmt_payload()), wf.chunk(b"iXML", odd, pad=b"\xAA"), wf.chunk(b"data", wf.audio_payload(10))]
        self.assertEqual(plan_repair(inspect_wav(wf.write(self.project / "g.wav", wf.riff(chunks)))).status, PLAN_NONE)


class BackupLayoutTests(RepairTestCase):
    def test_run_folder_per_minute_with_suffix(self) -> None:
        now = dt.datetime(2026, 10, 2, 14, 30, 59)
        first = backup_run_folder(self.backups, now)
        self.assertEqual(first.name, "2026-10-02 1430")
        first.mkdir(parents=True)
        self.assertEqual(backup_run_folder(self.backups, now).name, "2026-10-02 1430-2")

    def test_default_backup_root_is_beside_the_files_folder(self) -> None:
        # The user's rule: the folder above the source. For files handed in
        # from ".../Broken Files copy/2026-01-14.AAN" that's "Broken Files copy".
        files = [self.project / "2026-02-05.AAN" / "a.wav", self.project / "2026-02-05.AAN" / "b.wav"]
        self.assertEqual(default_backup_root(files), self.project / "2026-02-05.AAN - Wave Repair Backups")

    def test_known_scan_root_puts_backups_beside_it(self) -> None:
        # Browsing "ISO POLY", or Wave Manager's Source "ISO POLY": never inside it.
        files = [self.project / "2026-02-05.AAN" / "a.wav", self.project / "2026-01-13.AAN" / "b.wav"]
        self.assertEqual(default_backup_root(files, self.project), self.tmp / "ISO POLY - Wave Repair Backups")
        self.assertEqual(default_backup_root(files[:1], self.project), self.tmp / "ISO POLY - Wave Repair Backups")

    def test_relative_location(self) -> None:
        path = self.project / "2026-02-05.AAN" / "a.wav"
        self.assertEqual(relative_location(path, self.project), Path("2026-02-05.AAN"))
        self.assertEqual(relative_location(path, self.tmp / "elsewhere"), Path("2026-02-05.AAN"))


class RunRepairsTests(RepairTestCase):
    def test_repairs_in_place_with_verified_categorized_backup(self) -> None:
        path = cantar_odd_ixml_file(self.project / "2026-02-05.AAN" / "UB5800.WAV")
        original = path.read_bytes()
        os.utime(path, (1_700_000_000, 1_700_000_000))
        run_folder, results = run_repairs(self.items(path), self.backups, self.project, "test")
        result = results[0]
        self.assertEqual(result.status, "repaired", result.message)

        # Original name/place kept, exactly one zero byte appended, RIFF size +1, nothing else changed.
        repaired = path.read_bytes()
        self.assertEqual(len(repaired), len(original) + 1)
        self.assertEqual(repaired[:4] + repaired[8:-1], original[:4] + original[8:])
        self.assertEqual(repaired[-1:], b"\x00")
        self.assertEqual(struct.unpack_from("<I", repaired, 4)[0], len(repaired) - 8)
        self.assertEqual(inspect_wav(path).issues, [])
        # Repaired file gets the repair time; the backup keeps the original's.
        self.assertGreater(path.stat().st_mtime, 1_700_000_000 + 86400)

        # Backup: grouped by problem, original relative path and name, byte-identical.
        backup = run_folder / SAFE_REPAIRS["missing_final_pad"] / "2026-02-05.AAN" / "UB5800.WAV"
        self.assertEqual(Path(result.backup_path), backup)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(int(backup.stat().st_mtime), 1_700_000_000)
        log = json.loads((run_folder / "Repair Log.json").read_text())
        self.assertEqual(log["summary"], {SAFE_REPAIRS["missing_final_pad"]: 1})
        self.assertIn("UB5800.WAV", (run_folder / "Repair Log.txt").read_text())

    def test_deferred_file_is_never_touched_or_backed_up(self) -> None:
        path = cut_off_ixml_file(self.project / "2026-02-05.AAN" / "UB4731.WAV")
        original = path.read_bytes()
        run_folder, results = run_repairs(self.items(path), self.backups, self.project, "test")
        self.assertEqual(results[0].status, "skipped")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(sorted(item.name for item in run_folder.iterdir()), ["Repair Log.json", "Repair Log.txt"])

    def test_verification_failure_restores_original(self) -> None:
        path = cantar_odd_ixml_file(self.project / "2026-02-05.AAN" / "x.WAV")
        original = path.read_bytes()
        real_hash = riff_repair._file_hash
        calls = {"n": 0}

        def corrupt_after_edit(target, skip_riff_size=False, limit=None):
            calls["n"] += 1
            return "bogus" if limit is not None else real_hash(target, skip_riff_size, limit)

        with mock.patch.object(riff_repair, "_file_hash", side_effect=corrupt_after_edit):
            _run_folder, results = run_repairs(self.items(path), self.backups, self.project, "test")
        self.assertEqual(results[0].status, "failed")
        self.assertIn("restored from backup", results[0].message)
        self.assertEqual(path.read_bytes(), original)

    def test_file_changed_since_inspection_is_refused(self) -> None:
        path = cantar_odd_ixml_file(self.project / "2026-02-05.AAN" / "y.WAV")
        items = self.items(path)
        with path.open("ab") as handle:
            handle.write(b"\x00\x00")
        _run_folder, results = run_repairs(items, self.backups, self.project, "test")
        self.assertEqual(results[0].status, "failed")
        self.assertIn("changed since it was inspected", results[0].message)

    def test_progress_callbacks_report_each_stage(self) -> None:
        path = cantar_odd_ixml_file(self.project / "2026-02-05.AAN" / "s.WAV")
        steps, finished = [], []
        run_repairs(self.items(path), self.backups, self.project, "test", step=lambda i, n, p, m: steps.append(m), finished=lambda i, n, r: finished.append(r.status))
        self.assertEqual(steps, ["backing up", "verifying backup", "repairing", "verifying repair"])
        self.assertEqual(finished, ["repaired"])

    def test_mixed_batch(self) -> None:
        good = cantar_odd_ixml_file(self.project / "2026-02-05.AAN" / "a.WAV")
        bad = cut_off_ixml_file(self.project / "2026-02-05.AAN" / "b.WAV")
        _run_folder, results = run_repairs(self.items(good, bad), self.backups, self.project, "test")
        self.assertEqual([result.status for result in results], ["repaired", "skipped"])


if __name__ == "__main__":
    unittest.main()
