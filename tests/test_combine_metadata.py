import importlib.util
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


APP_PATH = Path(__file__).parents[1] / "wave_manager.py"
SPEC = importlib.util.spec_from_file_location("sox_metadata_workflow", APP_PATH)
assert SPEC and SPEC.loader
WORKFLOW = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WORKFLOW
SPEC.loader.exec_module(WORKFLOW)


def wav_channel_mask(wav_path: Path) -> int | None:
    with wav_path.open("rb") as wav_file:
        wav_file.seek(12)
        while True:
            chunk_header = wav_file.read(8)
            if len(chunk_header) != 8:
                raise AssertionError("WAV has no fmt chunk")
            chunk_id, chunk_size = struct.unpack("<4sI", chunk_header)
            if chunk_id == b"fmt ":
                fmt_data = wav_file.read(chunk_size)
                format_tag = struct.unpack_from("<H", fmt_data)[0]
                return struct.unpack_from("<I", fmt_data, 20)[0] if format_tag == 0xFFFE else None
            wav_file.seek(chunk_size + (chunk_size & 1), 1)


class InspectorMetadataTests(unittest.TestCase):
    @staticmethod
    def _planned_merge_job(order: tuple[str, ...]) -> WORKFLOW.Job:
        sources = [Path(f"TEST.{channel}.wav") for channel in order]
        return WORKFLOW.Job(
            sources[0],
            Path("planned-output.WAV"),
            len(sources),
            None,
            len(sources),
            "READY",
            "merge",
            sources=sources,
            combine_kind="speaker",
            speaker_order=order,
        )

    @staticmethod
    def _planned_audio(*_args) -> dict[str, str]:
        return {"sample_rate": "48000", "bit_depth": "24", "encoding": "Signed Integer PCM", "duration": "00:00:01.00", "total_samples": "48000"}

    def test_planned_merge_metadata_combines_valid_ixml_in_job_order(self) -> None:
        order = ("L", "C", "R", "Lss", "Rss", "Lsr", "Rsr", "LFE")
        job = self._planned_merge_job(order)
        original_audio = WORKFLOW.get_audio_info
        original_core = WORKFLOW.export_core_xml
        original_ixml = WORKFLOW.export_optional_ixml_for_inspector
        try:
            WORKFLOW.get_audio_info = self._planned_audio
            WORKFLOW.export_core_xml = lambda *_args: "<BWFMetaEdit><Core><Description>Planned Core</Description></Core></BWFMetaEdit>"
            WORKFLOW.export_optional_ixml_for_inspector = lambda _tool, source: (f"<BWFXML><TRACK_LIST><TRACK_COUNT>1</TRACK_COUNT><TRACK><CHANNEL_INDEX>{order.index(source.stem.rsplit('.', 1)[1]) + 1}</CHANNEL_INDEX><INTERLEAVE_INDEX>1</INTERLEAVE_INDEX><NAME>{source.stem}</NAME></TRACK></TRACK_LIST></BWFXML>", None)
            sections, ixml_root = WORKFLOW.AudioInspector._planned_combine_sections_for_tools("unused", "unused", job)
        finally:
            WORKFLOW.get_audio_info = original_audio
            WORKFLOW.export_core_xml = original_core
            WORKFLOW.export_optional_ixml_for_inspector = original_ixml
        section_map = dict(sections)
        self.assertIn("Planned Filename: planned-output.WAV", section_map["PLANNED FILE"])
        self.assertIn("Channels: 8", section_map["AUDIO"])
        self.assertIn("Description: Planned Core", section_map["BEXT / CORE"])
        self.assertEqual(section_map["CHANNEL CONFIGURATION"], "Speaker-aware Merge\nPlanned Stream Order: L C R Lss Rss Lsr Rsr LFE")
        self.assertEqual(section_map["iXML - Track List"], "Track Count: 8")
        self.assertIsNotNone(ixml_root)
        self.assertEqual([track.findtext("INTERLEAVE_INDEX") for track in ixml_root.findall(".//TRACK")], [str(index) for index in range(1, 9)])

    def test_planned_merge_metadata_tolerates_missing_partial_and_malformed_ixml(self) -> None:
        job = self._planned_merge_job(("L", "R", "C", "LFE", "Ls", "Rs"))
        original_audio = WORKFLOW.get_audio_info
        original_core = WORKFLOW.export_core_xml
        original_ixml = WORKFLOW.export_optional_ixml_for_inspector
        try:
            WORKFLOW.get_audio_info = self._planned_audio
            WORKFLOW.export_core_xml = lambda *_args: "<BWFMetaEdit><Core><Description>TEST</Description></Core></BWFMetaEdit>"
            WORKFLOW.export_optional_ixml_for_inspector = lambda *_args: (None, "iXML sidecar was not exported")
            sections, ixml_root = WORKFLOW.AudioInspector._planned_combine_sections_for_tools("unused", "unused", job)
            self.assertEqual(dict(sections)["iXML"], "No source iXML metadata available for planned merged output")
            self.assertIsNone(ixml_root)

            WORKFLOW.export_optional_ixml_for_inspector = lambda _tool, source: (None, "iXML sidecar was not exported") if source.name == "TEST.R.wav" else ("<BWFXML><TRACK_LIST /></BWFXML>", None)
            sections, ixml_root = WORKFLOW.AudioInspector._planned_combine_sections_for_tools("unused", "unused", job)
            self.assertEqual(dict(sections)["iXML"], "Partial / unavailable: Some source files do not contain iXML metadata.")
            self.assertIsNone(ixml_root)

            WORKFLOW.export_optional_ixml_for_inspector = lambda *_args: ("<BWFXML><PROJECT>broken & value</PROJECT></BWFXML>", None)
            sections, ixml_root = WORKFLOW.AudioInspector._planned_combine_sections_for_tools("unused", "unused", job)
            self.assertTrue(dict(sections)["iXML"].startswith("Unavailable: iXML parse failed"))
            self.assertIsNone(ixml_root)
            self.assertIn("Description: TEST", dict(sections)["BEXT / CORE"])
            self.assertIn("Channels: 6", dict(sections)["AUDIO"])
        finally:
            WORKFLOW.get_audio_info = original_audio
            WORKFLOW.export_core_xml = original_core
            WORKFLOW.export_optional_ixml_for_inspector = original_ixml

    def test_planned_merge_orders_and_path_do_not_execute_or_write(self) -> None:
        orders = (
            ("L", "R", "C", "LFE", "Lss", "Rss", "Lsr", "Rsr"),
            ("L", "C", "R", "Lss", "Rss", "Lsr", "Rsr", "LFE"),
            ("L", "R", "C", "LFE", "Ls", "Rs", "Lrs", "Rrs", "Ltm", "Rtm"),
        )
        original_audio = WORKFLOW.get_audio_info
        original_core = WORKFLOW.export_core_xml
        original_ixml = WORKFLOW.export_optional_ixml_for_inspector
        original_combine = WORKFLOW.combine_with_sox
        original_write_core = WORKFLOW.write_core
        original_write_ixml = WORKFLOW.write_ixml
        try:
            WORKFLOW.get_audio_info = self._planned_audio
            WORKFLOW.export_core_xml = lambda *_args: "<BWFMetaEdit><Core /></BWFMetaEdit>"
            WORKFLOW.export_optional_ixml_for_inspector = lambda *_args: (None, "iXML sidecar was not exported")
            WORKFLOW.combine_with_sox = lambda *_args: self.fail("planned Inspector view must not run SoX")
            WORKFLOW.write_core = lambda *_args: self.fail("planned Inspector view must not write Core metadata")
            WORKFLOW.write_ixml = lambda *_args: self.fail("planned Inspector view must not write iXML metadata")
            for order in orders:
                with self.subTest(order=order):
                    job = self._planned_merge_job(order)
                    sections, _ixml_root = WORKFLOW.AudioInspector._planned_combine_sections_for_tools("unused", "unused", job)
                    self.assertIn(f"Planned Stream Order: {' '.join(order)}", dict(sections)["CHANNEL CONFIGURATION"])
                    self.assertFalse(job.target.exists())
        finally:
            WORKFLOW.get_audio_info = original_audio
            WORKFLOW.export_core_xml = original_core
            WORKFLOW.export_optional_ixml_for_inspector = original_ixml
            WORKFLOW.combine_with_sox = original_combine
            WORKFLOW.write_core = original_write_core
            WORKFLOW.write_ixml = original_write_ixml

    def test_inspector_metadata_reports_missing_ixml_without_failing(self) -> None:
        original_core = WORKFLOW.export_core_xml
        original_ixml = WORKFLOW.export_optional_ixml_for_inspector
        try:
            WORKFLOW.export_core_xml = lambda *_args: "<BWFMetaEdit><Core><Description>TEST</Description></Core></BWFMetaEdit>"
            WORKFLOW.export_optional_ixml_for_inspector = lambda *_args: (None, "iXML sidecar was not exported")
            for channel in ("L", "R", "C", "LFE", "Lss", "Rss", "Lsr", "Rsr"):
                with self.subTest(channel=channel):
                    _core_xml, core_text, ixml_sections, ixml_root = WORKFLOW.inspector_metadata("unused", Path(f"TEST.{channel}.wav"))
                    self.assertIn("Description: TEST", core_text)
                    self.assertEqual(ixml_sections, [("iXML", "Unavailable: iXML sidecar was not exported")])
                    self.assertIsNone(ixml_root)
        finally:
            WORKFLOW.export_core_xml = original_core
            WORKFLOW.export_optional_ixml_for_inspector = original_ixml

    def test_inspector_metadata_parses_valid_and_sanitized_ixml(self) -> None:
        original_core = WORKFLOW.export_core_xml
        original_ixml = WORKFLOW.export_optional_ixml_for_inspector
        try:
            WORKFLOW.export_core_xml = lambda *_args: "<BWFMetaEdit><Core /></BWFMetaEdit>"
            WORKFLOW.export_optional_ixml_for_inspector = lambda *_args: ("<BWFXML><PROJECT>TEST\x1a</PROJECT><TRACK_LIST><TRACK_COUNT>1</TRACK_COUNT></TRACK_LIST></BWFXML>", None)
            _core_xml, _core_text, ixml_sections, ixml_root = WORKFLOW.inspector_metadata("unused", Path("TEST.5.1.wav"))
        finally:
            WORKFLOW.export_core_xml = original_core
            WORKFLOW.export_optional_ixml_for_inspector = original_ixml
        self.assertIsNotNone(ixml_root)
        self.assertEqual(ixml_root.findtext("PROJECT"), "TEST")
        self.assertIn(("iXML - Track List", "Track Count: 1"), ixml_sections)

    def test_optional_inspector_ixml_export_treats_missing_sidecar_as_absent(self) -> None:
        original_run_command = WORKFLOW.run_command
        try:
            WORKFLOW.run_command = lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", "")
            with tempfile.TemporaryDirectory() as temp_dir:
                wav_path = Path(temp_dir) / "TEST.R.wav"
                wav_path.touch()
                self.assertEqual(WORKFLOW.export_optional_ixml_for_inspector("unused", wav_path), (None, "iXML sidecar was not exported"))
        finally:
            WORKFLOW.run_command = original_run_command

    def test_optional_inspector_ixml_export_reports_command_failure_as_warning(self) -> None:
        original_run_command = WORKFLOW.run_command
        try:
            WORKFLOW.run_command = lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "BWF MetaEdit failed")
            with tempfile.TemporaryDirectory() as temp_dir:
                wav_path = Path(temp_dir) / "TEST.C.wav"
                wav_path.touch()
                self.assertEqual(WORKFLOW.export_optional_ixml_for_inspector("unused", wav_path), (None, "BWF MetaEdit failed"))
        finally:
            WORKFLOW.run_command = original_run_command

    def test_structured_ixml_keeps_stream_and_recorder_channels_distinct(self) -> None:
        ixml = """<BWFXML>
            <IXML_VERSION>2.01</IXML_VERSION><PROJECT>P.UP_01</PROJECT>
            <TRACK_LIST><TRACK_COUNT>2</TRACK_COUNT>
                <TRACK><CHANNEL_INDEX>1</CHANNEL_INDEX><INTERLEAVE_INDEX>1</INTERLEAVE_INDEX><NAME>S</NAME></TRACK>
                <TRACK><CHANNEL_INDEX>5</CHANNEL_INDEX><INTERLEAVE_INDEX>2</INTERLEAVE_INDEX><NAME>TZU HSUEH</NAME></TRACK>
            </TRACK_LIST>
            <AATON_CANTAR><REF_LEVEL>-18 dB</REF_LEVEL><ALL_TRK_NAME>
                <DATA><INDICE>0</INDICE><ACTIVE>YES</ACTIVE><NAME>S</NAME></DATA>
            </ALL_TRK_NAME></AATON_CANTAR>
        </BWFXML>"""
        sections = dict(WORKFLOW.structured_ixml_sections(ixml))
        root = ET.fromstring(ixml)
        self.assertIn("iXML - General", sections)
        self.assertEqual(sections["iXML - Track List"], "Track Count: 2")
        self.assertEqual(WORKFLOW.ixml_track_rows(root), [("1", "1", "S"), ("2", "5", "TZU HSUEH")])
        self.assertEqual(WORKFLOW.aaton_track_rows(root), [("0", "YES", "S")])
        self.assertNotIn("<TRACK>", sections["iXML - Track List"])

    def test_channel_configuration_uses_only_actual_wav_mask(self) -> None:
        self.assertEqual(WORKFLOW.channel_configuration(0), "None")
        self.assertEqual(WORKFLOW.channel_configuration(None), "None")
        self.assertEqual(WORKFLOW.channel_configuration(1599), "FL FR FC LFE BL BR SL SR")
        self.assertEqual(WORKFLOW.channel_configuration(0x00000200 | 0x00000400), "SL SR")


class SpeakerAliasTests(unittest.TestCase):
    def test_dolby_and_existing_aliases_preserve_channel_identity(self) -> None:
        mapping = WORKFLOW.parse_speaker_mapping(WORKFLOW.DEFAULT_SPEAKER_MAPPING)
        expected_aliases = {
            "Lss": "Lss", "Rss": "Rss", "Lsr": "Lsr", "Rsr": "Rsr", "Lrs": "Lrs", "Rrs": "Rrs",
            "FL": "L", "FR": "R", "FC": "C", "SL": "Ls", "SR": "Rs", "BL": "Lb", "BR": "Rb",
        }
        for token, canonical in expected_aliases.items():
            self.assertEqual(mapping[token.casefold()], canonical)
        for token in "L R C Lc Rc LFE Lfh Rfh S Cs Ls Rs Lscr Rscr Lsd Rsd Lb Rb Lvh Cvh Rvh Lrs Rrs Lw Rw Tbl Tbr Tfl Tfr Tl Tr Ts Ltm Rtm Lts Rts SW".split():
            self.assertIn(token.casefold(), mapping)
        self.assertNotIn("xyz", mapping)

    def test_supported_layout_orders_are_exact(self) -> None:
        expected = {
            ("5.1", "SMPTE"): ("L", "R", "C", "LFE", "Ls", "Rs"),
            ("5.1", "FILM"): ("L", "C", "R", "Ls", "Rs", "LFE"),
            ("7.1", "SMPTE"): ("L", "R", "C", "LFE", "Lss", "Rss", "Lsr", "Rsr"),
            ("7.1", "FILM"): ("L", "C", "R", "Lss", "Rss", "Lsr", "Rsr", "LFE"),
            ("7.1.2", "DOLBY ATMOS"): ("L", "R", "C", "LFE", "Ls", "Rs", "Lrs", "Rrs", "Ltm", "Rtm"),
        }
        for key, order in expected.items():
            self.assertEqual(WORKFLOW.speaker_order(*key, channels=set(order)), order)
        self.assertEqual(
            WORKFLOW.speaker_order("7.1", "SMPTE", channels=set(expected[("7.1.2", "DOLBY ATMOS")])),
            expected[("7.1.2", "DOLBY ATMOS")],
        )

    def test_source_layout_detection_ignores_global_layout_filter(self) -> None:
        cases = {
            ("SMPTE", frozenset(("L", "R"))): ("L", "R"),
            ("FILM", frozenset(("L", "R"))): ("L", "R"),
            ("SMPTE", frozenset(("L", "R", "LFE"))): ("L", "R", "LFE"),
            ("SMPTE", frozenset(("L", "R", "C"))): ("L", "R", "C"),
            ("FILM", frozenset(("L", "R", "C"))): ("L", "C", "R"),
            ("SMPTE", frozenset(("L", "R", "C", "LFE"))): ("L", "R", "C", "LFE"),
            ("FILM", frozenset(("L", "R", "C", "LFE"))): ("L", "C", "R", "LFE"),
            ("SMPTE", frozenset(("L", "R", "C", "LFE", "Ls", "Rs"))): ("L", "R", "C", "LFE", "Ls", "Rs"),
            ("FILM", frozenset(("L", "R", "C", "LFE", "Ls", "Rs"))): ("L", "C", "R", "Ls", "Rs", "LFE"),
            ("SMPTE", frozenset(("L", "R", "C", "LFE", "Lss", "Rss", "Lsr", "Rsr"))): ("L", "R", "C", "LFE", "Lss", "Rss", "Lsr", "Rsr"),
            ("FILM", frozenset(("L", "R", "C", "LFE", "Lss", "Rss", "Lsr", "Rsr"))): ("L", "C", "R", "Lss", "Rss", "Lsr", "Rsr", "LFE"),
            ("SMPTE", frozenset(("L", "R", "C", "LFE", "Ls", "Rs", "Lrs", "Rrs", "Ltm", "Rtm"))): ("L", "R", "C", "LFE", "Ls", "Rs", "Lrs", "Rrs", "Ltm", "Rtm"),
        }
        for (order_name, channels), expected in cases.items():
            self.assertEqual(WORKFLOW.speaker_order("7.1", order_name, set(channels)), expected)
        with self.assertRaises(ValueError):
            WORKFLOW.speaker_order("7.1", "SMPTE", {"L", "R", "C", "Ls"})


@unittest.skipUnless(shutil.which("sox"), "SoX is required for Combine integration tests")
class CombineMetadataTests(unittest.TestCase):
    def test_unified_combine_merge_groups_generic_and_speaker_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("AAA.A3.wav", "AAA.A5.wav", "AAA.A6.wav", "BBB.L.wav", "BBB.R.wav", "CCC.FL.wav", "CCC.FR.wav", "CCC.FC.wav", "CCC.LFE.wav", "CCC.SL.wav", "CCC.SR.wav", "SINGLE.R.wav"):
                subprocess.run(["sox", "-n", "-r", "48000", "-b", "24", "-c", "1", str(root / name), "synth", "0.01", "sine", "1000"], check=True)
            jobs = WORKFLOW.build_combine_jobs(root, root / "out", "Skip existing", copy_non_combined=True)
            by_target = {job.target.stem: job for job in jobs if job.target is not None}
            self.assertEqual([source.name for source in by_target["AAA"].sources], ["AAA.A3.wav", "AAA.A5.wav", "AAA.A6.wav"])
            self.assertEqual(by_target["AAA"].combine_kind, "generic")
            self.assertEqual(by_target["AAA"].channel_mask, 0)
            self.assertEqual(by_target["BBB"].speaker_order, ("L", "R"))
            self.assertEqual(by_target["BBB"].channel_mask, 0x00000003)
            self.assertEqual(by_target["CCC"].speaker_order, ("L", "R", "C", "LFE", "Ls", "Rs"))
            self.assertEqual(by_target["CCC"].channel_mask, 0x0000060F)
            self.assertEqual(by_target["SINGLE.R"].mode, "combine_copy")

    def test_only_files_combines_just_what_was_sent_and_blocks_partial_groups(self) -> None:
        # Files sent from Soundminer: whole groups combine; a group sent
        # without all of its channel files is blocked, naming what's missing.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("AAA.A1.wav", "AAA.A2.wav", "BBB.A1.wav", "BBB.A2.wav", "BBB.A3.wav", "CCC.L.wav", "CCC.R.wav"):
                subprocess.run(["sox", "-n", "-r", "48000", "-b", "24", "-c", "1", str(root / name), "synth", "0.01", "sine", "1000"], check=True)
            sent = [root / "AAA.A1.wav", root / "AAA.A2.wav", root / "BBB.A1.wav", root / "BBB.A3.wav"]
            jobs = WORKFLOW.build_combine_jobs(root, root / "out", "Skip existing", only_files=sent)
            by_target = {job.target.stem: job for job in jobs}
            self.assertEqual(sorted(by_target), ["AAA", "BBB"])  # CCC wasn't sent
            self.assertEqual(by_target["AAA"].status, "READY")
            self.assertEqual(by_target["BBB"].status, "BLOCKED: only 2 of 3 channel files were sent — missing BBB.A2.wav")

    def test_speaker_merge_keeps_unspecified_mask_like_generic_combine(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            left = root / "L.wav"
            right = root / "R.wav"
            for source in (left, right):
                subprocess.run(["sox", "-n", "-r", "48000", "-b", "24", "-c", "1", str(source), "synth", "0.01", "sine", "1000"], check=True)
            generic = root / "generic.wav"
            merged = root / "merged.wav"
            WORKFLOW.combine_with_sox("sox", [left, right], generic, overwrite=False)
            WORKFLOW.combine_with_sox("sox", [left, right], merged, overwrite=False)
            self.assertEqual(wav_channel_mask(generic), 0)
            self.assertEqual(wav_channel_mask(merged), 0)

    def test_execute_speaker_merge_keeps_5_1_smpte_and_film_masks_unspecified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for token in ("L", "R", "C", "LFE", "Ls", "Rs"):
                subprocess.run(["sox", "-n", "-r", "48000", "-b", "24", "-c", "1", str(root / f"M.{token}.wav"), "synth", "0.01", "sine", "1000"], check=True)
            sources = [root / f"M.{token}.wav" for token in ("L", "R", "C", "LFE", "Ls", "Rs")]
            original = (WORKFLOW.export_core_xml, WORKFLOW.poly_ixml_from_mono_sources, WORKFLOW.write_core, WORKFLOW.write_ixml)
            try:
                WORKFLOW.export_core_xml = lambda *_args: "<Core/>"
                WORKFLOW.poly_ixml_from_mono_sources = lambda *_args: "<BWFXML><TRACK_LIST><TRACK_COUNT>6</TRACK_COUNT></TRACK_LIST></BWFXML>"
                WORKFLOW.write_core = lambda *_args: None
                WORKFLOW.write_ixml = lambda *_args: None
                for order_name, order in (("SMPTE", ("L", "R", "C", "LFE", "Ls", "Rs")), ("FILM", ("L", "C", "R", "Ls", "Rs", "LFE"))):
                    target = root / f"{order_name}.wav"
                    by_token = {source.stem.rsplit(".", 1)[1]: source for source in sources}
                    ordered_sources = [by_token[token] for token in order]
                    job = WORKFLOW.Job(ordered_sources[0], target, 6, None, 6, "READY", "merge", sources=ordered_sources, combine_kind="speaker", speaker_order=order, channel_mask=0x0000060F)
                    WORKFLOW.execute_combine_job(job, "sox", "unused", replace=False)
                    with target.open("rb") as wav_file:
                        wav_file.seek(20)
                        format_tag, channels = struct.unpack("<HH", wav_file.read(4))
                    self.assertEqual(format_tag, 0xFFFE)
                    self.assertEqual(channels, 6)
                    self.assertEqual(wav_channel_mask(target), 0)
            finally:
                WORKFLOW.export_core_xml, WORKFLOW.poly_ixml_from_mono_sources, WORKFLOW.write_core, WORKFLOW.write_ixml = original

    def test_combine_has_no_speaker_layout_for_2_6_and_8_channels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_source = root / "A.A1.wav"
            subprocess.run(
                ["sox", "-n", "-r", "48000", "-b", "24", "-c", "1", str(first_source), "synth", "0.01", "sine", "1000"],
                check=True,
            )
            for channel in range(2, 9):
                shutil.copyfile(first_source, root / f"A.A{channel}.wav")

            for channel_count in (2, 6, 8):
                target = root / f"combined-{channel_count}.wav"
                WORKFLOW.combine_with_sox(
                    "sox",
                    [root / f"A.A{channel}.wav" for channel in range(1, channel_count + 1)],
                    target,
                    overwrite=False,
                )
                self.assertEqual(wav_channel_mask(target), 0)

    def test_combine_ixml_preserves_recorder_channels_and_resequences_streams(self) -> None:
        sources = [Path(f"A.A{channel}.wav") for channel in (1, 3, 5, 6, 7, 8)]
        original_export_ixml = WORKFLOW.export_ixml

        def fake_export_ixml(_bwfmetaedit: str, source: Path) -> str:
            channel = int(source.stem.rsplit("A", 1)[1])
            return (
                "<BWFXML><TRACK_LIST><TRACK_COUNT>1</TRACK_COUNT><TRACK>"
                f"<CHANNEL_INDEX>{channel}</CHANNEL_INDEX><INTERLEAVE_INDEX>1</INTERLEAVE_INDEX>"
                "</TRACK></TRACK_LIST></BWFXML>"
            )

        try:
            WORKFLOW.export_ixml = fake_export_ixml
            root = ET.fromstring(WORKFLOW.poly_ixml_from_mono_sources("unused", sources))
        finally:
            WORKFLOW.export_ixml = original_export_ixml

        tracks = root.findall(".//TRACK")
        self.assertEqual(root.findtext(".//TRACK_COUNT"), "6")
        self.assertEqual([track.findtext("CHANNEL_INDEX") for track in tracks], ["1", "3", "5", "6", "7", "8"])
        self.assertEqual([track.findtext("INTERLEAVE_INDEX") for track in tracks], ["1", "2", "3", "4", "5", "6"])


if __name__ == "__main__":
    unittest.main()