import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parent))

import wav_factory as wf
from operations.riff_inspect import format_report_text, inspect_wav, parse_bext_description, summary_line


def codes(report) -> set[str]:
    return {issue.code for issue in report.issues}


def issue(report, code):
    return next(item for item in report.issues if item.code == code)


class RiffInspectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def wav(self, data: bytes, name: str = "test.wav") -> Path:
        return wf.write(self.tmp / name, data)

    # --- clean files -------------------------------------------------------

    def test_clean_file_has_no_issues_and_reads_everything(self) -> None:
        report = inspect_wav(self.wav(wf.riff(wf.standard_chunks(frames=4800))))
        self.assertEqual(report.issues, [])
        self.assertEqual(report.worst_severity, "ok")
        self.assertEqual(report.container, "RIFF")
        self.assertEqual([chunk.chunk_id for chunk in report.chunks], ["fmt ", "bext", "iXML", "data"])
        self.assertEqual(report.audio["frames"], 4800)
        self.assertAlmostEqual(report.audio["duration_seconds"], 0.1)
        self.assertEqual(report.ixml["scene"], "56-1")
        self.assertEqual([track["name"] for track in report.ixml["tracks"]], ["BOOM", "VICKY"])
        self.assertEqual(report.bext["description_fields"]["sTAPE"], "0110")
        self.assertEqual(report.bext["start_timecode"], "14:00:00:00")
        self.assertEqual(summary_line(report), "OK")

    def test_optional_metadata_chunks_absent_is_not_an_issue(self) -> None:
        report = inspect_wav(self.wav(wf.riff(wf.standard_chunks(with_bext=False, with_ixml=False))))
        self.assertEqual(report.issues, [])
        self.assertEqual(report.category_status("bext"), "absent")
        self.assertEqual(report.category_status("ixml"), "absent")
        self.assertEqual(report.category_status("data"), "ok")

    def test_ixml_trailing_nul_padding_is_normal(self) -> None:
        # Recorders routinely reserve iXML space with NULs — must not alarm.
        report = inspect_wav(self.wav(wf.riff(wf.standard_chunks(ixml=wf.ixml_payload(nul_padding=301)))))
        self.assertEqual(report.issues, [])
        self.assertEqual(report.ixml["take"], "3")

    def test_rf64_with_ds64_resolves_sizes(self) -> None:
        audio = wf.audio_payload(1000)
        chunks = [wf.chunk(b"fmt ", wf.fmt_payload()), wf.chunk(b"data", audio, declared_size=0xFFFFFFFF)]
        report = inspect_wav(self.wav(wf.rf64(chunks, len(audio))))
        self.assertEqual(report.issues, [])
        self.assertEqual(report.container, "RF64")
        self.assertEqual(report.audio["frames"], 1000)

    def test_rf64_without_ds64_is_an_error(self) -> None:
        audio = wf.audio_payload(1000)
        chunks = [wf.chunk(b"fmt ", wf.fmt_payload()), wf.chunk(b"data", audio, declared_size=0xFFFFFFFF)]
        report = inspect_wav(self.wav(wf.rf64(chunks, len(audio), ds64=False)))
        self.assertIn("rf64_missing_ds64", codes(report))
        self.assertIn("rf64_data_size_unknown", codes(report))

    # --- container --------------------------------------------------------

    def test_riff_size_too_small_is_repairable_warning(self) -> None:
        data = wf.riff(wf.standard_chunks())
        data = data[:4] + struct.pack("<I", len(data) - 8 - 100) + data[8:]
        report = inspect_wav(self.wav(data))
        found = issue(report, "riff_size_mismatch")
        self.assertEqual(found.severity, "warning")
        self.assertTrue(found.repairable)
        self.assertEqual(report.category_status("data"), "ok")

    def test_riff_size_larger_than_file_is_error(self) -> None:
        data = wf.riff(wf.standard_chunks())
        data = data[:4] + struct.pack("<I", len(data) + 5000) + data[8:]
        self.assertEqual(issue(inspect_wav(self.wav(data)), "riff_size_mismatch").severity, "error")

    def test_not_a_riff_file(self) -> None:
        report = inspect_wav(self.wav(b"ID3\x04" + b"\x00" * 100))
        self.assertIn("not_riff", codes(report))
        self.assertIsNone(report.container)

    def test_file_too_small(self) -> None:
        self.assertIn("file_too_small", codes(inspect_wav(self.wav(b"RIFF"))))

    def test_trailing_zero_bytes_are_info_only(self) -> None:
        data = wf.riff(wf.standard_chunks())
        report = inspect_wav(self.wav(data + b"\x00\x00\x00"))
        self.assertIn("trailing_zero_bytes", codes(report))
        self.assertEqual(issue(report, "trailing_zero_bytes").severity, "info")
        self.assertEqual(report.worst_severity, "info")

    def test_trailing_garbage_bytes_are_a_warning(self) -> None:
        report = inspect_wav(self.wav(wf.riff(wf.standard_chunks()) + b"\x80\x81"))
        self.assertEqual(issue(report, "trailing_bytes").severity, "warning")

    # --- padding ----------------------------------------------------------

    def test_missing_pad_byte_between_chunks(self) -> None:
        odd_ixml = wf.ixml_payload() + b" "  # force an odd size
        if not len(odd_ixml) & 1:
            odd_ixml += b" "
        chunks = [wf.chunk(b"fmt ", wf.fmt_payload()), wf.chunk(b"iXML", odd_ixml, pad=None), wf.chunk(b"data", wf.audio_payload(100))]
        report = inspect_wav(self.wav(wf.riff(chunks)))
        self.assertIn("missing_pad_byte", codes(report))
        self.assertNotIn("nonzero_pad", codes(report))
        self.assertTrue(issue(report, "missing_pad_byte").repairable)
        self.assertEqual(report.audio["frames"], 100)
        self.assertEqual(report.ixml["scene"], "56-1")

    def test_nonzero_pad_byte_is_info(self) -> None:
        odd_ixml = wf.ixml_payload()
        if not len(odd_ixml) & 1:
            odd_ixml += b" "
        chunks = [wf.chunk(b"fmt ", wf.fmt_payload()), wf.chunk(b"iXML", odd_ixml, pad=b"\xAA"), wf.chunk(b"data", wf.audio_payload(100))]
        report = inspect_wav(self.wav(wf.riff(chunks)))
        self.assertEqual(issue(report, "nonzero_pad").severity, "info")
        self.assertEqual(report.worst_severity, "info")

    def test_missing_final_pad(self) -> None:
        odd_tail = wf.chunk(b"LIST", b"INFOx", pad=None)
        report = inspect_wav(self.wav(wf.riff(wf.standard_chunks() + [odd_tail])))
        self.assertIn("missing_final_pad", codes(report))

    # --- data region -------------------------------------------------------

    def test_unfinalized_data_size_zero(self) -> None:
        chunks = wf.standard_chunks(frames=4800)
        chunks[-1] = wf.chunk(b"data", wf.audio_payload(4800), declared_size=0)
        report = inspect_wav(self.wav(wf.riff(chunks)))
        found = issue(report, "data_unfinalized")
        self.assertEqual(found.severity, "error")
        self.assertTrue(found.repairable)
        self.assertEqual(report.audio["frames"], 4800)

    def test_unfinalized_data_size_ffffffff_in_plain_riff(self) -> None:
        chunks = wf.standard_chunks(frames=480)
        chunks[-1] = wf.chunk(b"data", wf.audio_payload(480), declared_size=0xFFFFFFFF)
        report = inspect_wav(self.wav(wf.riff(chunks)))
        self.assertIn("data_unfinalized", codes(report))
        self.assertEqual(report.audio["frames"], 480)

    def test_unfinalized_data_excludes_trailing_metadata(self) -> None:
        # Recorders that write iXML after the audio: an unfinalized data size
        # must not swallow that iXML as audio.
        chunks = [wf.chunk(b"fmt ", wf.fmt_payload()), wf.chunk(b"data", wf.audio_payload(4800), declared_size=0), wf.chunk(b"iXML", wf.ixml_payload())]
        report = inspect_wav(self.wav(wf.riff(chunks)))
        self.assertIn("data_unfinalized", codes(report))
        self.assertIn("metadata_after_audio", codes(report))
        self.assertEqual(report.audio["frames"], 4800)
        self.assertEqual(report.ixml["scene"], "56-1")
        self.assertEqual([chunk.chunk_id for chunk in report.chunks], ["fmt ", "data", "iXML"])

    def test_truncated_file(self) -> None:
        data = wf.riff(wf.standard_chunks(frames=4800))
        report = inspect_wav(self.wav(data[: len(data) - 1000]))
        self.assertIn("data_truncated", codes(report))
        self.assertEqual(issue(report, "riff_size_mismatch").severity, "error")
        self.assertIn("data_not_frame_aligned", codes(report))  # 1000 isn't a multiple of 6
        self.assertEqual(report.data["available_size"], 4800 * 6 - 1000)

    def test_audio_continues_past_declared_size(self) -> None:
        chunks = wf.standard_chunks(frames=4800)
        chunks[-1] = wf.chunk(b"data", wf.audio_payload(4800), declared_size=2400 * 6)
        report = inspect_wav(self.wav(wf.riff(chunks)))
        found = issue(report, "data_extends_past_declared")
        self.assertEqual(found.severity, "warning")
        self.assertTrue(found.repairable)
        self.assertEqual(report.data["available_size"], 4800 * 6)

    def test_frame_misalignment(self) -> None:
        chunks = [wf.chunk(b"fmt ", wf.fmt_payload()), wf.chunk(b"data", wf.audio_payload(100) + b"\x80\x80\x80\x80")]
        report = inspect_wav(self.wav(wf.riff(chunks)))
        found = issue(report, "data_not_frame_aligned")
        self.assertTrue(found.repairable)
        self.assertEqual(report.audio["frames"], 100)

    def test_missing_data_chunk(self) -> None:
        report = inspect_wav(self.wav(wf.riff([wf.chunk(b"fmt ", wf.fmt_payload())])))
        self.assertIn("missing_data", codes(report))

    # --- broken structure ---------------------------------------------------

    def test_damaged_id_with_intact_size_keeps_walking(self) -> None:
        # sox reads this file fine (it skips the chunk by size) — the
        # structure holds, only that chunk's contents are lost.
        chunks = wf.standard_chunks()
        chunks[1] = b"\x01\x02\x03\x04" + chunks[1][4:]  # corrupt the bext ID only
        report = inspect_wav(self.wav(wf.riff(chunks)))
        found = issue(report, "corrupt_chunk_id")
        self.assertEqual(found.severity, "warning")
        self.assertEqual(found.category, "container")
        self.assertEqual([chunk.chunk_id for chunk in report.chunks][2:], ["iXML", "data"])
        self.assertEqual(report.audio["frames"], 4800)
        self.assertNotIn("invalid_chunk_id", codes(report))

    def test_damaged_ixml_id_is_identified_by_content(self) -> None:
        chunks = wf.standard_chunks()
        chunks[2] = b"\x00\x00\x00\x00" + chunks[2][4:]
        report = inspect_wav(self.wav(wf.riff(chunks)))
        found = issue(report, "corrupt_chunk_id")
        self.assertEqual(found.category, "ixml")
        self.assertIn("look like iXML", found.message)
        self.assertEqual(report.category_status("ixml"), "warning")

    def test_damaged_chunk_header_stops_walk_and_lists_candidates(self) -> None:
        chunks = wf.standard_chunks()
        chunks[1] = b"\x01\x02\x03\x04\xff\xff\x00\x7f" + chunks[1][8:]  # corrupt the bext ID and size
        report = inspect_wav(self.wav(wf.riff(chunks)))
        self.assertIn("invalid_chunk_id", codes(report))
        self.assertEqual([chunk.chunk_id for chunk in report.chunks], ["fmt "])
        self.assertIn("missing_data", codes(report))
        found = {candidate.chunk_id for candidate in report.candidates}
        self.assertTrue({"iXML", "data"} <= found)
        self.assertTrue(all(candidate.chains_to_eof for candidate in report.candidates if candidate.chunk_id in ("iXML", "data")))

    def test_chunk_overrunning_eof_before_data(self) -> None:
        chunks = wf.standard_chunks()
        chunks[1] = wf.chunk(b"bext", wf.bext_payload(), declared_size=10_000_000)
        report = inspect_wav(self.wav(wf.riff(chunks)))
        self.assertIn("chunk_overruns_eof", codes(report))
        self.assertEqual(report.category_status("bext"), "error")

    def test_trailing_ixml_header_without_contents_cantar_style(self) -> None:
        # Real Cantar (v3.535) damage: bext/fmt/data/... all intact, then the
        # final iXML header (61,440 reserved bytes) with nothing after it,
        # while the RIFF size already counts those bytes.
        chunks = wf.standard_chunks(with_ixml=False) + [wf.chunk(b"iXML", b"\x00" * 61440)]
        full = wf.riff(chunks)
        report = inspect_wav(self.wav(full[: len(full) - 61440]))
        cut = issue(report, "chunk_overruns_eof")
        self.assertEqual((cut.category, cut.severity, cut.repairable), ("ixml", "error", True))
        self.assertIn("none of its 61,440 bytes were written", cut.message)
        self.assertNotIn("ixml_empty", codes(report))
        riff = issue(report, "riff_size_mismatch")
        self.assertIn("exactly the missing iXML contents", riff.message)
        self.assertTrue(riff.repairable)
        self.assertEqual(report.category_status("data"), "ok")
        self.assertEqual(report.audio["frames"], 4800)

    def test_partially_written_trailing_ixml(self) -> None:
        chunks = wf.standard_chunks(with_ixml=False) + [wf.chunk(b"iXML", wf.ixml_payload())]
        full = wf.riff(chunks)
        report = inspect_wav(self.wav(full[:-100]))
        self.assertIn("it was cut off", issue(report, "chunk_overruns_eof").message)
        self.assertIn("ixml_truncated", codes(report))
        self.assertIn("exactly the missing iXML contents", issue(report, "riff_size_mismatch").message)

    # --- fmt / metadata contents ---------------------------------------------

    def test_wrong_block_align(self) -> None:
        chunks = [wf.chunk(b"fmt ", wf.fmt_payload(block_align=4)), wf.chunk(b"data", wf.audio_payload(100))]
        report = inspect_wav(self.wav(wf.riff(chunks)))
        self.assertIn("fmt_block_align", codes(report))
        self.assertNotIn("data_not_frame_aligned", codes(report))

    def test_truncated_ixml(self) -> None:
        report = inspect_wav(self.wav(wf.riff(wf.standard_chunks(ixml=wf.ixml_payload()[:-40]))))
        self.assertEqual(issue(report, "ixml_truncated").severity, "error")
        self.assertEqual(report.category_status("ixml"), "error")
        self.assertEqual(report.category_status("data"), "ok")

    def test_short_bext(self) -> None:
        chunks = wf.standard_chunks()
        chunks[1] = wf.chunk(b"bext", wf.bext_payload()[:400])
        report = inspect_wav(self.wav(wf.riff(chunks)))
        self.assertIn("bext_too_small", codes(report))
        self.assertEqual(report.bext["description_fields"]["sSCENE"], "56-1")

    def test_track_count_mismatch_is_info(self) -> None:
        report = inspect_wav(self.wav(wf.riff(wf.standard_chunks(ixml=wf.ixml_payload(track_names=("A", "B", "C"))))))
        self.assertEqual(issue(report, "ixml_track_count_mismatch").severity, "info")

    def test_bext_description_parsing(self) -> None:
        self.assertEqual(parse_bext_description("sSCENE=1A\r\nsTAKE=02\nnote\rsTAPE= 0110 "), {"sSCENE": "1A", "sTAKE": "02", "sTAPE": "0110"})

    # --- output -------------------------------------------------------------

    def test_report_serializes_and_formats(self) -> None:
        chunks = wf.standard_chunks()
        chunks[-1] = wf.chunk(b"data", wf.audio_payload(4800), declared_size=0)
        report = inspect_wav(self.wav(wf.riff(chunks)))
        payload = json.loads(json.dumps(report.to_dict()))
        self.assertEqual(payload["category_status"]["data"], "error")
        text = format_report_text(report)
        self.assertIn("never finalized", text)
        self.assertIn("[repairable]", text)
        self.assertIn("Scene: 56-1", text)

    def test_inspection_never_modifies_the_file(self) -> None:
        chunks = wf.standard_chunks()
        chunks[-1] = wf.chunk(b"data", wf.audio_payload(4800), declared_size=0)
        path = self.wav(wf.riff(chunks))
        before = path.read_bytes()
        inspect_wav(path)
        self.assertEqual(path.read_bytes(), before)



# Folder with the two real broken Cantar files; set the environment variable
# AUDIO_POST_TOOLS_BROKEN_FILES to run these tests (skipped otherwise).
REAL_BROKEN_FILES = Path(os.environ.get("AUDIO_POST_TOOLS_BROKEN_FILES", "/nonexistent"))


@unittest.skipUnless(REAL_BROKEN_FILES.is_dir(), "real broken-file samples not mounted")
class RealBrokenFileTests(unittest.TestCase):
    """Two real Cantar (v3.535) poly files, 2026-01-13/14: every chunk through
    `mixr` intact, then the final iXML header with none of its 61,440 bytes."""

    def test_cantar_files_missing_final_ixml_contents(self) -> None:
        files = sorted(path for path in REAL_BROKEN_FILES.glob("*.WAV") if not path.name.startswith("._"))
        self.assertEqual(len(files), 2)
        for path in files:
            with self.subTest(path.name):
                report = inspect_wav(path)
                self.assertEqual(codes(report), {"chunk_overruns_eof", "riff_size_mismatch"})
                self.assertTrue(all(found.repairable for found in report.issues))
                self.assertEqual(report.chunks[-1].chunk_id, "iXML")
                self.assertEqual((report.chunks[-1].declared_size, report.chunks[-1].size), (61440, 0))
                self.assertEqual(report.category_status("data"), "ok")
                self.assertEqual(report.audio["channels"], 12)
                self.assertIn("aSCENE", report.bext["description_fields"])


if __name__ == "__main__":
    unittest.main()
