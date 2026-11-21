import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parent))

import wav_factory as wf
from operations import audio_recover
from operations.audio_recover import STALE, TRUNCATED, UNFINALIZED, plan_recover, run_recovers
from operations.riff_inspect import inspect_wav

FRAMES = 4800
AUDIO = wf.audio_payload(FRAMES)  # 2 ch, 24-bit → 6-byte frames


def build(chunks: list[bytes]) -> bytes:
    return wf.riff(chunks)


def data_offset(data: bytes) -> int:
    position = 12
    while True:
        cid, size = struct.unpack_from("<4sI", data, position)
        if cid == b"data":
            return position
        position += 8 + size + (size & 1)


def with_size(data: bytes, offset: int, value: int) -> bytes:
    out = bytearray(data)
    struct.pack_into("<I", out, offset, value)
    return bytes(out)


class RecoverTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "card").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        return wf.write(self.tmp / "card" / name, data)

    def recover(self, path: Path):
        report = inspect_wav(path)
        plan = plan_recover(report)
        self.assertTrue(plan.ok, plan.problem)
        run_folder, results = run_recovers([(report, plan)], self.tmp / "backups", self.tmp / "card", "test")
        return plan, results[0], run_folder

    def assert_only_sizes_changed(self, before: bytes, after: bytes, appended: int) -> None:
        offset = data_offset(before)
        self.assertEqual(len(after), len(before) + appended)
        self.assertEqual(after[:4], before[:4])
        self.assertEqual(after[8:offset + 4], before[8:offset + 4])
        self.assertEqual(after[offset + 8:len(before)], before[offset + 8:])
        self.assertEqual(after[len(before):], b"\x00" * appended)


class PlanTests(RecoverTestCase):
    def test_power_loss_at_end_of_file(self) -> None:
        full = build(wf.standard_chunks(frames=FRAMES))
        path = self.write("a.wav", with_size(full, data_offset(full) + 4, 0))
        plan = plan_recover(inspect_wav(path))
        self.assertEqual((plan.kind, plan.new_data_size, plan.append), (UNFINALIZED, len(AUDIO), 0))
        self.assertIn("Recording interrupted", plan.lines[0])

    def test_incomplete_last_frame_is_completed_with_zeros(self) -> None:
        full = build(wf.standard_chunks(frames=FRAMES))
        cut = with_size(full, data_offset(full) + 4, 0xFFFFFFFF)[:-4]  # last frame only 2 of 6 bytes
        plan = plan_recover(inspect_wav(self.write("b.wav", cut)))
        self.assertEqual(plan.kind, UNFINALIZED)
        self.assertEqual(plan.new_data_size, len(AUDIO))
        self.assertEqual(plan.append, 4)

    def test_metadata_after_audio_stays_metadata(self) -> None:
        chunks = [wf.chunk(b"fmt ", wf.fmt_payload()), wf.chunk(b"data", AUDIO, declared_size=0), wf.chunk(b"iXML", wf.ixml_payload())]
        plan = plan_recover(inspect_wav(self.write("c.wav", build(chunks))))
        self.assertEqual((plan.new_data_size, plan.append), (len(AUDIO), 0))
        self.assertIn("iXML", plan.trailing_metadata)

    def test_stale_header_has_evidence(self) -> None:
        full = build(wf.standard_chunks(frames=FRAMES))
        plan = plan_recover(inspect_wav(self.write("d.wav", with_size(full, data_offset(full) + 4, len(AUDIO) // 2))))
        self.assertEqual(plan.kind, STALE)
        self.assertEqual(plan.extra_bytes, len(AUDIO) // 2)
        self.assertIsNotNone(plan.level_db)
        self.assertIsNotNone(plan.extra_level_db)
        self.assertTrue(any("Evidence" in line for line in plan.lines))

    def test_a_few_stray_bytes_are_flagged_as_likely_junk(self) -> None:
        full = build(wf.standard_chunks(frames=FRAMES))
        plan = plan_recover(inspect_wav(self.write("junk.wav", full + b"\x13\x37garbage")))
        self.assertEqual((plan.kind, plan.extra_bytes), (STALE, 9))
        self.assertTrue(any("far more likely junk" in line for line in plan.lines))

    def test_truncated_copy_warns_about_the_lost_tail(self) -> None:
        full = build(wf.standard_chunks(frames=FRAMES))
        plan = plan_recover(inspect_wav(self.write("e.wav", full[:-1000])))
        self.assertEqual(plan.kind, TRUNCATED)
        self.assertEqual(plan.missing_bytes, 1000)
        self.assertTrue(any("CANNOT be recovered" in line for line in plan.lines))

    def test_not_for_healthy_rf64_or_other_problems(self) -> None:
        self.assertFalse(plan_recover(inspect_wav(self.write("ok.wav", build(wf.standard_chunks())))).ok)
        rf = wf.rf64([wf.chunk(b"fmt ", wf.fmt_payload()), wf.chunk(b"data", AUDIO, declared_size=0xFFFFFFFF)], 0)
        self.assertIn("RF64", plan_recover(inspect_wav(self.write("rf.wav", rf))).problem)
        chunks = [wf.chunk(b"fmt ", wf.fmt_payload(block_align=4)), wf.chunk(b"data", AUDIO, declared_size=0)]
        self.assertIn("another problem", plan_recover(inspect_wav(self.write("bad.wav", build(chunks)))).problem)


class WriteTests(RecoverTestCase):
    def test_power_loss_recovery_changes_only_the_size_fields(self) -> None:
        full = build(wf.standard_chunks(frames=FRAMES))
        before = with_size(with_size(full, data_offset(full) + 4, 0), 4, 36)
        path = self.write("UB1.WAV", before)
        plan, result, run_folder = self.recover(path)
        self.assertEqual(result.status, "repaired", result.message)
        after = path.read_bytes()
        self.assert_only_sizes_changed(before, after, 0)
        report = inspect_wav(path)
        self.assertEqual(report.issues, [])
        self.assertEqual(report.audio["frames"], FRAMES)
        self.assertEqual((run_folder / plan.category / "UB1.WAV").read_bytes(), before)

    def test_incomplete_frame_and_odd_sizes(self) -> None:
        full = build(wf.standard_chunks(frames=FRAMES))
        before = with_size(full, data_offset(full) + 4, 0)[:-3]
        path = self.write("UB2.WAV", before)
        _plan, result, _run = self.recover(path)
        self.assertEqual(result.status, "repaired", result.message)
        self.assert_only_sizes_changed(before, path.read_bytes(), 3)
        self.assertEqual(inspect_wav(path).issues, [])

    def test_truncated_copy_becomes_playable(self) -> None:
        full = build(wf.standard_chunks(frames=FRAMES))
        before = full[:-1001]
        path = self.write("UB3.WAV", before)
        plan, result, _run = self.recover(path)
        self.assertEqual(result.status, "repaired", result.message)
        self.assert_only_sizes_changed(before, path.read_bytes(), plan.append)
        self.assertEqual(inspect_wav(path).issues, [])

    def test_metadata_after_audio_case_writes_cleanly(self) -> None:
        chunks = [wf.chunk(b"fmt ", wf.fmt_payload()), wf.chunk(b"data", AUDIO, declared_size=0), wf.chunk(b"iXML", wf.ixml_payload())]
        path = self.write("UB4.WAV", build(chunks))
        _plan, result, _run = self.recover(path)
        self.assertEqual(result.status, "repaired", result.message)
        report = inspect_wav(path)
        self.assertEqual(report.issues, [])
        self.assertEqual(report.ixml["scene"], "56-1")

    def test_verification_failure_restores_the_original(self) -> None:
        full = build(wf.standard_chunks(frames=FRAMES))
        before = with_size(full, data_offset(full) + 4, 0)
        path = self.write("UB5.WAV", before)
        report = inspect_wav(path)
        plan = plan_recover(report)
        plan.new_data_size += 6  # a wrong plan: verification must catch it
        _run, results = run_recovers([(report, plan)], self.tmp / "backups", None, "test")
        self.assertEqual(results[0].status, "failed")
        self.assertIn("restored from backup", results[0].message)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
