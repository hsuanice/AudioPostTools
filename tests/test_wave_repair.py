"""Wave Repair for Post app logic + its hand-off from Wave Manager."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).parent))

import wav_factory as wf
from operations.repair_handoff import REPAIR_APP_NAME, repair_launch_command, structural_damage_hint
from operations.riff_inspect import format_structure_brief, inspect_wav

_SPEC = importlib.util.spec_from_file_location("wav_repair_app", APP_DIR / "wave_repair.py")
REPAIR = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(REPAIR)


class WaveRepairTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def healthy(self, name: str) -> Path:
        return wf.write(self.tmp / name, wf.riff(wf.standard_chunks()))

    def power_loss(self, name: str) -> Path:
        chunks = wf.standard_chunks()
        chunks[-1] = wf.chunk(b"data", wf.audio_payload(4800), declared_size=0)
        return wf.write(self.tmp / name, wf.riff(chunks))


class RouteIncomingPathsTests(WaveRepairTestCase):
    def test_single_wav_opens_inspect_file(self) -> None:
        path = self.healthy("a.WAV")
        self.assertEqual(REPAIR.route_incoming_paths([str(path)]), (REPAIR.MODE_INSPECT, [path], None))

    def test_folder_opens_browse(self) -> None:
        self.healthy("b.wav")
        (self.tmp / "sub").mkdir()
        nested = self.healthy("sub/c.wav")
        mode, files, folder = REPAIR.route_incoming_paths([str(self.tmp)])
        self.assertEqual((mode, folder), (REPAIR.MODE_BROWSE, self.tmp))
        self.assertEqual(len(files), 2)
        _mode, flat, _folder = REPAIR.route_incoming_paths([str(self.tmp)], recursive=False)
        self.assertNotIn(nested, flat)

    def test_several_files_browse_as_batch(self) -> None:
        paths = [self.healthy("a.wav"), self.healthy("b.wav")]
        mode, files, folder = REPAIR.route_incoming_paths([str(path) for path in paths])
        self.assertEqual((mode, files, folder), (REPAIR.MODE_BROWSE, paths, None))

    def test_ignores_appledouble_non_wav_and_launch_noise(self) -> None:
        path = self.healthy("a.wav")
        (self.tmp / "._a.wav").write_bytes(b"\x00\x05\x16\x07")
        (self.tmp / "notes.txt").write_text("x")
        values = ["-psn_0_12345", str(path), str(self.tmp / "._a.wav"), str(self.tmp / "notes.txt")]
        self.assertEqual(REPAIR.route_incoming_paths(values), (REPAIR.MODE_INSPECT, [path], None))
        self.assertEqual(REPAIR.list_wavs(self.tmp, recursive=True), [path])


class RepairHandoffTests(WaveRepairTestCase):
    def test_launch_from_source_runs_sibling_script(self) -> None:
        wav = self.healthy("a.wav")
        command = repair_launch_command([wav], frozen=False, executable=Path("/usr/bin/python3"), script_dir=APP_DIR)
        self.assertEqual(command, ["/usr/bin/python3", str(APP_DIR / "wave_repair.py"), str(wav)])
        self.assertTrue(Path(command[1]).is_file())

    def test_packaged_prefers_sibling_app_bundle(self) -> None:
        manager = self.tmp / "Wave Manager for Post.app" / "Contents" / "MacOS"
        manager.mkdir(parents=True)
        (self.tmp / f"{REPAIR_APP_NAME}.app").mkdir()
        command = repair_launch_command([Path("/x/a.wav")], frozen=True, executable=manager / "Wave Manager for Post", script_dir=self.tmp)
        self.assertEqual(command, ["open", "-a", str(self.tmp / f"{REPAIR_APP_NAME}.app"), "/x/a.wav"])

    def test_packaged_falls_back_to_launchservices_name(self) -> None:
        manager = self.tmp / "Wave Manager for Post.app" / "Contents" / "MacOS"
        manager.mkdir(parents=True)
        command = repair_launch_command([Path("/x/a.wav")], frozen=True, executable=manager / "Wave Manager for Post", script_dir=self.tmp)
        self.assertEqual(command, ["open", "-a", REPAIR_APP_NAME, "/x/a.wav"])

    def test_hint_is_empty_for_healthy_or_missing_files(self) -> None:
        self.assertEqual(structural_damage_hint([self.healthy("a.wav"), None, self.tmp / "missing.wav"]), "")

    def test_hint_names_the_damage(self) -> None:
        hint = structural_damage_hint([self.power_loss("a.wav")])
        self.assertIn("never finalized", hint)
        self.assertIn(REPAIR_APP_NAME, hint)
        self.assertNotIn("a.wav:", hint)

    def test_hint_names_which_file_when_several(self) -> None:
        hint = structural_damage_hint([self.healthy("a.wav"), self.power_loss("b.wav")])
        self.assertIn("b.wav: ", hint)

    def test_structure_brief(self) -> None:
        self.assertEqual(format_structure_brief(inspect_wav(self.healthy("a.wav"))), "Status: OK — RIFF, chunks: fmt, bext, iXML, data")
        damaged = format_structure_brief(inspect_wav(self.power_loss("b.wav")))
        self.assertIn("Status: 1 error", damaged)
        self.assertNotIn("Scene:", damaged)  # metadata stays in the Inspector's own sections


class ReportBundleTests(WaveRepairTestCase):
    def test_bundle_carries_version_and_files(self) -> None:
        bundle = REPAIR.report_bundle([inspect_wav(self.power_loss("a.wav"))], self.tmp)
        self.assertEqual(bundle["app"], REPAIR_APP_NAME)
        self.assertEqual(bundle["version"], REPAIR.VERSION)
        self.assertEqual(bundle["files"][0]["category_status"]["data"], "error")

    def test_settings_roundtrip_and_corrupt_file(self) -> None:
        path = self.tmp / "settings" / "settings.json"
        REPAIR.save_settings({"recursive": False}, path)
        self.assertEqual(REPAIR.load_settings(path), {"recursive": False})
        path.write_text("{not json")
        self.assertEqual(REPAIR.load_settings(path), {})



class RepairSummaryTests(WaveRepairTestCase):
    def test_summarize_and_repair_line(self) -> None:
        from operations.riff_repair import plan_repair
        from test_riff_repair import cantar_odd_ixml_file, cut_off_ixml_file
        plans = [plan_repair(inspect_wav(path)) for path in (cantar_odd_ixml_file(self.tmp / "a.wav"), cantar_odd_ixml_file(self.tmp / "b.wav"), cut_off_ixml_file(self.tmp / "c.wav"), self.healthy("d.wav"))]
        by_category, deferred = REPAIR.summarize_plans(plans)
        self.assertEqual(list(by_category.values()), [2])
        self.assertEqual([plan.path.name for plan in deferred], ["c.wav"])
        self.assertTrue(REPAIR.repair_line(plans[0]).startswith("Repair: will fix — Missing pad byte"))
        self.assertIn("not repairable yet", REPAIR.repair_line(plans[2]))
        self.assertEqual(REPAIR.repair_line(plans[3]), "Repair: nothing to repair")



class ProgressFormattingTests(unittest.TestCase):
    def test_eta_is_byte_based(self) -> None:
        self.assertIsNone(REPAIR.repair_eta(10, 0, 100))
        self.assertAlmostEqual(REPAIR.repair_eta(10, 25, 100), 30)
        self.assertEqual(REPAIR.repair_eta(10, 100, 100), 0)

    def test_duration_format(self) -> None:
        self.assertEqual(REPAIR.format_duration_short(65), "1:05")
        self.assertEqual(REPAIR.format_duration_short(3725), "1:02:05")



class HandoffNoteTests(WaveRepairTestCase):
    def test_note_names_wave_managers_source_folder(self) -> None:
        from operations.repair_handoff import handoff_scan_root, write_handoff
        source = self.tmp / "ISO POLY"
        (source / "2026-01-14.AAN").mkdir(parents=True)
        wav = source / "2026-01-14.AAN" / "UB4783.WAV"
        wav.write_bytes(b"x")
        note = self.tmp / "handoff.json"
        write_handoff([wav], source, note)
        self.assertEqual(handoff_scan_root([wav], note), source)
        other = self.tmp / "elsewhere.wav"
        other.write_bytes(b"x")
        self.assertIsNone(handoff_scan_root([other], note))  # not in the note
        write_handoff([other], source, note)
        self.assertIsNone(handoff_scan_root([other], note))  # not inside the source

    def test_stale_or_missing_note_is_ignored(self) -> None:
        import json, time
        from operations.repair_handoff import HANDOFF_MAX_AGE, handoff_scan_root
        wav = self.tmp / "a.wav"
        wav.write_bytes(b"x")
        note = self.tmp / "handoff.json"
        self.assertIsNone(handoff_scan_root([wav], note))
        note.write_text(json.dumps({"scan_root": str(self.tmp), "files": [str(wav)], "written": time.time() - HANDOFF_MAX_AGE - 5}))
        self.assertIsNone(handoff_scan_root([wav], note))


if __name__ == "__main__":
    unittest.main()
