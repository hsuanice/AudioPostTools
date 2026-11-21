import importlib.util
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


APP_PATH = Path(__file__).parents[1] / "wave_manager.py"
SPEC = importlib.util.spec_from_file_location("sox_metadata_workflow_characterization", APP_PATH)
assert SPEC and SPEC.loader
WORKFLOW = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WORKFLOW
SPEC.loader.exec_module(WORKFLOW)

from operations.rename_core import RENAME_LIVE_REFRESH_LIMIT, RenameRule, SerialMetadata, apply_name_affix, apply_rename_rules, assign_serial_ids, rename_auto_input_pattern, rename_input_pattern, rename_live_refresh_allowed, rename_number_from_match, render_serial_name, strict_metadata_group_order
from operations.timecode import parse_speed_field, samples_to_smpte


def write_riff(path: Path, chunks: list[tuple[bytes, bytes]]) -> None:
    body = bytearray(b"WAVE")
    for chunk_id, payload in chunks:
        body.extend(struct.pack("<4sI", chunk_id, len(payload)))
        body.extend(payload)
        if len(payload) & 1:
            body.append(0)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def raw_chunk(path: Path, wanted: bytes) -> bytes | None:
    data = path.read_bytes()
    offset = 12
    while offset + 8 <= len(data):
        chunk_id, size = struct.unpack_from("<4sI", data, offset)
        raw_size = 8 + size + (size & 1)
        if chunk_id == wanted:
            return data[offset:offset + raw_size]
        offset += raw_size
    return None


class FilenameCharacterizationTests(unittest.TestCase):
    def test_rename_rules_apply_in_order_and_support_literal_or_regex(self) -> None:
        rules = [RenameRule("scene", "SC"), RenameRule(r"(SC)(\d+)", r"\1_\2", regex=True)]

        self.assertEqual(apply_rename_rules("scene12", rules), "SC_12")

    def test_three_find_replace_rules_apply_in_order(self) -> None:
        rules = [RenameRule(".A", "_Ch"), RenameRule("_Ch1", "_L"), RenameRule("Scene", "Take")]

        self.assertEqual(apply_rename_rules("Scene.A1", rules), "Take_L")

    def test_large_rename_preview_uses_manual_refresh_boundary(self) -> None:
        self.assertTrue(rename_live_refresh_allowed(RENAME_LIVE_REFRESH_LIMIT))
        self.assertFalse(rename_live_refresh_allowed(RENAME_LIVE_REFRESH_LIMIT + 1))

    def test_add_text_preserves_complete_original_stem(self) -> None:
        self.assertEqual(apply_name_affix("T1921", "_test", "Append"), "T1921_test")
        self.assertEqual(apply_name_affix("duplicate.A1", "PRE_", "Prepend"), "PRE_duplicate.A1")

    def test_serial_ids_share_by_base_and_preserve_existing_assignments(self) -> None:
        assignments, next_number = assign_serial_ids(
            ["Scene_B", "Scene_A", "Scene_A"],
            {"Scene_B": "AB1234"},
            "AB",
            4,
            1234,
        )

        self.assertEqual(assignments, {"Scene_B": "AB1234", "Scene_A": "AB1235"})
        self.assertEqual(next_number, 1236)
        self.assertEqual(render_serial_name("{id}.A$", "Scene_A", 2, ".wav", assignments["Scene_A"]), "AB1235.A2.wav")

    def test_serial_ids_allow_empty_prefix(self) -> None:
        assignments, next_number = assign_serial_ids(["Scene"], {}, "", 4, 1)

        self.assertEqual(assignments, {"Scene": "0001"})
        self.assertEqual(next_number, 2)

    def test_strict_metadata_order_requires_consistent_tape_and_tc(self) -> None:
        ordered, blocked = strict_metadata_group_order([
            SerialMetadata("scene-b", "Tape-1", 200),
            SerialMetadata("scene-a", "Tape-1", 100),
            SerialMetadata("missing-tape", "", 300),
            SerialMetadata("conflict", "Tape-1", 400),
            SerialMetadata("conflict", "Tape-2", 400),
        ])

        self.assertEqual(ordered, ["scene-a", "scene-b"])
        self.assertEqual(blocked["missing-tape"], "BLOCKED: missing or inconsistent Tape")
        self.assertEqual(blocked["conflict"], "BLOCKED: missing or inconsistent Tape")

    def test_strict_metadata_order_blocks_duplicate_tape_tc(self) -> None:
        ordered, blocked = strict_metadata_group_order([
            SerialMetadata("scene-a", "Tape-1", 100),
            SerialMetadata("scene-b", "Tape-1", 100),
        ])

        self.assertEqual(ordered, [])
        self.assertEqual(set(blocked), {"scene-a", "scene-b"})

    def test_channel_order_formula_renders_named_split_channel(self) -> None:
        self.assertEqual(render_serial_name("{id}.{channel}", "Scene", 1, ".wav", "AB1234", "Ls"), "AB1234.Ls.wav")
        self.assertEqual(render_serial_name("{ID}.{CHANNEL}", "Scene", 1, ".wav", "AB1234", "Ls"), "AB1234.Ls.wav")
        self.assertEqual(render_serial_name("{base}.A$", "Scene", 1, ".wav", "AB1234", serial_position="prepend"), "AB1234_Scene.A1.wav")
        self.assertEqual(render_serial_name("{base}.A$", "Scene", 1, ".wav", "AB1234", serial_position="append"), "Scene.A1_AB1234.wav")
        match = rename_input_pattern("{base}.{channel}").match("Scene.R.wav")
        self.assertIsNotNone(match)
        self.assertEqual(match["base"], "Scene")
        self.assertEqual(match["channel"], "R")

    def test_rename_default_source_formula_also_discovers_poly_and_renders_base_name(self) -> None:
        match = rename_auto_input_pattern().match("Scene.wav")
        self.assertIsNotNone(match)
        self.assertEqual(match["base"], "Scene")
        self.assertIsNone(match.groupdict().get("ch"))
        self.assertEqual(render_serial_name("{base}.A$", "Scene", 0, ".wav"), "Scene.wav")

    def test_change_number_does_not_add_channel_number_to_bare_poly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            poly = root / "T1921.wav"
            poly.touch()
            jobs = WORKFLOW.build_rename_jobs(
                root,
                "{base}.A$",
                "{base}.A$",
                "Trk#",
                "bwf",
                output_root=root / "out",
                change_number=True,
            )

        self.assertEqual(jobs[0].target.name, "T1921.wav")
        self.assertFalse(jobs[0].has_channel_suffix)

    def test_split_channel_file_reports_a_channel_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            split = root / "Scene.A1.wav"
            split.touch()
            jobs = WORKFLOW.build_rename_jobs(
                root,
                "{base}.A$",
                "{base}.A$",
                "Trk#",
                "bwf",
                output_root=root / "out",
                change_number=True,
            )

        self.assertTrue(jobs[0].has_channel_suffix)

    def test_add_text_to_bare_poly_does_not_create_a0_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            poly = root / "T1921.wav"
            poly.touch()
            jobs = WORKFLOW.build_rename_jobs(
                root,
                "{base}.A$",
                "{base}.A$",
                "Trk#",
                "bwf",
                output_root=root / "out",
                apply_formula=False,
                name_affix="_test",
                name_affix_position="Append",
            )

        self.assertEqual(jobs[0].target.name, "T1921_test.wav")

    def test_rename_auto_source_groups_poly_numeric_and_named_channels_by_base(self) -> None:
        pattern = rename_auto_input_pattern()

        self.assertEqual(pattern.match("duplicate.R.wav")["base"], "duplicate")
        self.assertEqual(pattern.match("duplicate.Ls.wav")["channel"], "Ls")
        self.assertEqual(pattern.match("duplicate.Ch1.wav")["channel"], "Ch1")
        self.assertEqual(pattern.match("duplicate.A2.wav")["ch"], "2")
        self.assertEqual(rename_number_from_match(pattern.match("T1948.WAV")), 0)

    def test_shared_placeholder_validation_preserves_formula_specific_messages(self) -> None:
        with self.assertRaisesRegex(ValueError, "Formula must contain exactly one \\{base\\} and one \\$ placeholder"):
            WORKFLOW.formula_pattern("{base}.wav")
        with self.assertRaisesRegex(ValueError, "Speaker filename pattern must contain exactly one \\{base\\} and one \\$ placeholder"):
            WORKFLOW.speaker_filename_pattern("{base}.wav")

    def test_formula_parse_and_render_contract(self) -> None:
        pattern = WORKFLOW.formula_pattern("{base}_AAP.A$")
        match = pattern.match("Scene_01_AAP.A12.WAV")
        self.assertIsNotNone(match)
        self.assertEqual(match.groupdict(), {"base": "Scene_01", "ch": "12"})
        self.assertEqual(WORKFLOW.render_output_name("{base}_AAP.A$", "Scene_01", 12, ".WAV"), "Scene_01_AAP.A12.WAV")
        with self.assertRaises(ValueError):
            WORKFLOW.formula_pattern("{base}.wav")

    def test_target_formula_regex_and_qc_formula_contract(self) -> None:
        target = WORKFLOW.formula_pattern(r"{base}.A$ render \d+", allow_regex=True)
        self.assertEqual(target.match("Scene.A3 render 42.wav").groupdict(), {"base": "Scene", "ch": "3"})


class TimecodeCharacterizationTests(unittest.TestCase):
    def test_parse_speed_field_reads_fps_and_drop_frame_flag(self) -> None:
        self.assertEqual(parse_speed_field("024.000-ND"), (24.0, False))
        self.assertEqual(parse_speed_field("029.970-DF"), (29.97, True))
        self.assertIsNone(parse_speed_field("no speed here"))

    def test_non_drop_frame_conversion_uses_rounded_nominal_fps(self) -> None:
        # Exactly 4326 seconds at 48000 Hz, 24.000 fps ND -> 01:12:06:00.
        self.assertEqual(samples_to_smpte(4326 * 48000, 48000, 24.0, False), "01:12:06:00")
        # 23.976 fps footage still wraps its frame digits at the rounded 24.
        self.assertEqual(samples_to_smpte(48000, 48000, 23.976, False), "00:00:01:00")

    def test_drop_frame_skips_two_frame_numbers_at_each_non_tenth_minute(self) -> None:
        # 1798 nominal frames/sec*29.97fps math is exercised through real sample counts;
        # these three points are the classic drop-frame boundary check at 29.97 fps:
        # the frame just before minute 1 rolls over, the next one (skips :00 and :01),
        # and exactly the 10-minute mark, where no frames are dropped.
        sample_rate = 48000.0
        fps = 29.97
        frame_1799_samples = round(1799 / fps * sample_rate)
        frame_1800_samples = round(1800 / fps * sample_rate)
        frame_17982_samples = round(17982 / fps * sample_rate)
        self.assertEqual(samples_to_smpte(frame_1799_samples, sample_rate, fps, True), "00:00:59;29")
        self.assertEqual(samples_to_smpte(frame_1800_samples, sample_rate, fps, True), "00:01:00;02")
        self.assertEqual(samples_to_smpte(frame_17982_samples, sample_rate, fps, True), "00:10:00;00")

    def test_invalid_inputs_raise_value_error(self) -> None:
        with self.assertRaises(ValueError):
            samples_to_smpte(-1, 48000, 24.0, False)
        with self.assertRaises(ValueError):
            samples_to_smpte(0, 0, 24.0, False)
        with self.assertRaises(ValueError):
            samples_to_smpte(0, 48000, 0, False)


class AppSettingsPersistenceCharacterizationTests(unittest.TestCase):
    def test_missing_settings_file_returns_empty_dict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "does_not_exist" / "settings.json"

            self.assertEqual(WORKFLOW.load_app_settings(path), {})

    def test_corrupt_settings_file_returns_empty_dict_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.json"
            path.write_text("not valid json {{{", encoding="utf-8")

            self.assertEqual(WORKFLOW.load_app_settings(path), {})

    def test_save_then_load_round_trips_and_creates_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nested" / "settings.json"
            data = {"formula_presets": ["{base}.A$", "{base}_MIX.A$"], "window_geometry": "1180x832+34+64"}

            WORKFLOW.save_app_settings(data, path)

            self.assertEqual(WORKFLOW.load_app_settings(path), data)

    def test_falls_back_to_the_legacy_path_for_the_current_default_path(self) -> None:
        # The app was renamed from "SoX Metadata Workflow" to "Wave Manager
        # for Post", which moved APP_SETTINGS_PATH to a differently-named
        # folder — load_app_settings() should still find an existing
        # install's settings under the old name rather than looking reset.
        # A default parameter is bound once at def() time, so patching the
        # module-level APP_SETTINGS_PATH can't change what a bare
        # load_app_settings() call already resolved to at import — this
        # instead calls it with that same (now-patched) path explicitly,
        # which exercises the exact `path == APP_SETTINGS_PATH` check the
        # real zero-argument call relies on in production, where both
        # names really do refer to the same current value.
        with tempfile.TemporaryDirectory() as temp_dir:
            new_path = Path(temp_dir) / "new" / "settings.json"
            legacy_path = Path(temp_dir) / "legacy" / "settings.json"
            legacy_path.parent.mkdir(parents=True)
            legacy_data = {"formula_presets": ["{base}.A$"], "window_geometry": "1180x900+10+10"}
            legacy_path.write_text(json.dumps(legacy_data), encoding="utf-8")

            with mock.patch.object(WORKFLOW, "APP_SETTINGS_PATH", new_path), mock.patch.object(
                WORKFLOW, "_LEGACY_APP_SETTINGS_PATH", legacy_path
            ):
                self.assertEqual(WORKFLOW.load_app_settings(new_path), legacy_data)
                # A different caller-supplied path that doesn't exist must
                # NOT fall back to the legacy file — only the current
                # default path does.
                other_path = Path(temp_dir) / "unrelated" / "settings.json"
                self.assertEqual(WORKFLOW.load_app_settings(other_path), {})

    def test_prefers_the_new_path_over_the_legacy_one_once_it_has_real_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            new_path = Path(temp_dir) / "new" / "settings.json"
            legacy_path = Path(temp_dir) / "legacy" / "settings.json"
            new_data = {"formula_presets": ["{base}.A$ render \\d+"]}
            WORKFLOW.save_app_settings(new_data, new_path)
            legacy_path.parent.mkdir(parents=True)
            legacy_path.write_text(json.dumps({"formula_presets": ["stale"]}), encoding="utf-8")

            with mock.patch.object(WORKFLOW, "APP_SETTINGS_PATH", new_path), mock.patch.object(
                WORKFLOW, "_LEGACY_APP_SETTINGS_PATH", legacy_path
            ):
                self.assertEqual(WORKFLOW.load_app_settings(new_path), new_data)


class KeybindingCharacterizationTests(unittest.TestCase):
    def test_display_label_renders_modifier_symbols(self) -> None:
        self.assertEqual(WORKFLOW.keybinding_display_label("<Command-Key-1>"), "⌘1")
        self.assertEqual(WORKFLOW.keybinding_display_label("<Command-Shift-Key-i>"), "⌘⇧I")

    def test_display_label_for_a_bare_scoped_key(self) -> None:
        self.assertEqual(WORKFLOW.keybinding_display_label("t"), "T")
        self.assertEqual(WORKFLOW.keybinding_display_label("Return"), "Return")

    def test_display_label_for_unbound_action(self) -> None:
        self.assertEqual(WORKFLOW.keybinding_display_label(""), "(none)")

    def test_every_action_has_a_non_empty_default_and_a_known_scope(self) -> None:
        # Fixed by the script (not user-editable), so every one of these
        # must actually resolve to something real — an action with a
        # blank default or a typo'd scope would silently never fire. Scope
        # is either "global" or a widget attribute name, or a tuple of
        # several (an action bound directly on more than one widget).
        known_widgets = {"browse_file_list", "browse_folder_tree"}
        for action_id, _label, default_sequence, scope in WORKFLOW.KEYBINDING_ACTIONS:
            self.assertTrue(default_sequence, action_id)
            scope_names = scope if isinstance(scope, tuple) else (scope,)
            for name in scope_names:
                self.assertIn(name, known_widgets | {"global"}, action_id)

    def test_toggle_actions_have_distinct_defaults(self) -> None:
        # Audio Inspector and the Execution Log are both separate windows
        # toggled by their own shortcut — regression guard against them
        # ever being accidentally given the same default key.
        self.assertNotEqual(WORKFLOW.DEFAULT_KEYBINDINGS["open_inspector"], WORKFLOW.DEFAULT_KEYBINDINGS["toggle_log"])


class BrowseVolumeCharacterizationTests(unittest.TestCase):
    def test_label_at_zero_db(self) -> None:
        self.assertEqual(WORKFLOW.browse_volume_label(0.0), "+0.0 dB")

    def test_label_for_positive_gain(self) -> None:
        self.assertEqual(WORKFLOW.browse_volume_label(24.0), "+24.0 dB")

    def test_label_for_negative_gain(self) -> None:
        self.assertEqual(WORKFLOW.browse_volume_label(-6.5), "-6.5 dB")

    def test_label_at_floor_reads_as_inf(self) -> None:
        self.assertEqual(WORKFLOW.browse_volume_label(WORKFLOW.BROWSE_VOLUME_MIN_DB), "-inf dB")

    def test_label_below_floor_still_reads_as_inf(self) -> None:
        # The Scale widget should never produce a value below the floor, but
        # the label should degrade gracefully rather than print a huge negative number.
        self.assertEqual(WORKFLOW.browse_volume_label(-999.0), "-inf dB")

    def test_sox_args_at_zero_db(self) -> None:
        self.assertEqual(WORKFLOW.browse_volume_sox_args(0.0), ["vol", "0.00", "dB"])

    def test_sox_args_for_positive_gain(self) -> None:
        self.assertEqual(WORKFLOW.browse_volume_sox_args(24.0), ["vol", "24.00", "dB"])

    def test_sox_args_at_floor_is_true_silence(self) -> None:
        # sox has no literal -inf gain, so the floor is special-cased to an
        # exact "vol 0 amplitude" mute instead of a very-quiet-but-nonzero dB value.
        self.assertEqual(WORKFLOW.browse_volume_sox_args(WORKFLOW.BROWSE_VOLUME_MIN_DB), ["vol", "0", "amplitude"])

    def test_sox_args_below_floor_is_also_true_silence(self) -> None:
        self.assertEqual(WORKFLOW.browse_volume_sox_args(-999.0), ["vol", "0", "amplitude"])

    def test_range_constants_match_requested_spec(self) -> None:
        # -inf..+24dB, defaulting to 0 was the explicit user request; -60dB
        # is the stand-in floor for "-inf" since sox has no literal -inf gain.
        self.assertEqual(WORKFLOW.BROWSE_VOLUME_MIN_DB, -60.0)
        self.assertEqual(WORKFLOW.BROWSE_VOLUME_MAX_DB, 24.0)


class ToolInstallerScriptCharacterizationTests(unittest.TestCase):
    def test_installer_script_without_homebrew_installs_brew_then_tools(self) -> None:
        script = WORKFLOW.build_tool_installer_script(install_brew=True)

        self.assertIn("#!/bin/bash", script)
        self.assertIn("Installing Homebrew", script)
        self.assertIn("Homebrew/install/HEAD/install.sh", script)
        self.assertIn("brew shellenv", script)
        self.assertIn("brew install sox bwfmetaedit", script)
        self.assertLess(script.index("Installing Homebrew"), script.index("brew install sox bwfmetaedit"))

    def test_installer_script_with_homebrew_skips_homebrew_install(self) -> None:
        script = WORKFLOW.build_tool_installer_script(install_brew=False)

        self.assertNotIn("Installing Homebrew", script)
        self.assertNotIn("install.sh", script)
        self.assertIn("brew install sox bwfmetaedit", script)


class ToolVersionCharacterizationTests(unittest.TestCase):
    def test_sox_version_reports_a_real_execution_failure_instead_of_masking_it(self) -> None:
        # Pointing "SoX" at some unrelated existing file (a WAV, say) used to
        # report "OK" with the *system's actual installed Homebrew sox
        # version" — Check Tools verified nothing at all, it just always
        # fell back to `brew list --versions sox` whenever tool_version()'s
        # output didn't look like a version number, execution failure or not.
        with mock.patch.object(WORKFLOW, "tool_version", return_value="error: [Errno 8] Exec format error: '/not/a/real/tool'"):
            with mock.patch.object(WORKFLOW, "find_executable") as mock_find_brew:
                result = WORKFLOW.sox_version("/not/a/real/tool", ["--version"])

        mock_find_brew.assert_not_called()
        self.assertEqual(result, "error: [Errno 8] Exec format error: '/not/a/real/tool'")

    def test_sox_version_still_falls_back_to_homebrew_for_real_but_unparsed_output(self) -> None:
        with mock.patch.object(WORKFLOW, "tool_version", return_value="some unparsed banner line"), mock.patch.object(
            WORKFLOW, "find_executable", return_value="/opt/homebrew/bin/brew"
        ), mock.patch.object(WORKFLOW.subprocess, "run") as mock_run:
            mock_run.return_value = mock.Mock(stdout="sox 14.4.2_6\n", returncode=0)
            result = WORKFLOW.sox_version("/opt/homebrew/bin/sox", ["--version"])

        self.assertEqual(result, "14.4.2_6")


class PathCoreCharacterizationTests(unittest.TestCase):
    def test_qc_treeview_sort_keys_are_numeric_natural_and_include_missing(self) -> None:
        numeric = ["10", "—", "2", "1"]
        filenames = ["Scene.A10.wav", "—", "Scene.A2.wav", "Scene.A1.wav"]

        self.assertEqual(sorted(numeric, key=lambda value: WORKFLOW.treeview_cell_sort_key(value, numeric=True)), ["—", "1", "2", "10"])
        self.assertEqual(sorted(filenames, key=WORKFLOW.treeview_cell_sort_key), ["—", "Scene.A1.wav", "Scene.A2.wav", "Scene.A10.wav"])

    def test_workflow_folder_drop_selects_first_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            folder = root / "folder"
            folder.mkdir()
            file_path = root / "audio.wav"
            file_path.touch()

            self.assertEqual(WORKFLOW.first_dropped_folder([str(file_path), str(folder)]), folder)
            self.assertIsNone(WORKFLOW.first_dropped_folder([str(file_path), str(root / "missing")]))

    def test_qc_drop_accepts_wav_or_folder_and_rejects_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            folder = root / "folder"
            folder.mkdir()
            wav_path = root / "audio.wav"
            text_path = root / "notes.txt"
            wav_path.touch()
            text_path.touch()

            self.assertEqual(WORKFLOW.first_dropped_qc_input([str(text_path), str(wav_path)]), wav_path)
            self.assertEqual(WORKFLOW.first_dropped_qc_input([str(folder)]), folder)
            self.assertIsNone(WORKFLOW.first_dropped_qc_input([str(text_path)]))

    def test_wav_discovery_preserves_planner_and_qc_sort_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "z").mkdir()
            (root / "a").mkdir()
            for relative in ("z/Scene.A10.wav", "z/Scene.A2.WAV", "a/Scene.A2.wav", "a/ignore.txt"):
                (root / relative).touch()

            self.assertEqual([path.relative_to(root).as_posix() for path in WORKFLOW.wav_files(root)], [
                "a/Scene.A2.wav",
                "z/Scene.A2.WAV",
                "z/Scene.A10.wav",
            ])
            self.assertEqual([path.relative_to(root).as_posix() for path in WORKFLOW.list_qc_recursive_wav_files(root)], [
                "z/Scene.A10.wav",
                "a/Scene.A2.wav",
                "z/Scene.A2.WAV",
            ])

    def test_output_root_resolution_preserves_empty_relative_and_absolute_paths(self) -> None:
        source_root = Path("/sessions/source")

        self.assertIsNone(WORKFLOW.resolve_output_root(source_root, "  "))
        self.assertEqual(WORKFLOW.resolve_output_root(source_root, "renders"), Path("/sessions/renders"))
        self.assertEqual(WORKFLOW.resolve_output_root(source_root, "/exports/final"), Path("/exports/final"))


class MetadataReadCharacterizationTests(unittest.TestCase):
    def test_serial_metadata_reads_tape_from_core_xml(self) -> None:
        metadata = WORKFLOW.MetadataRead(
            core_xml="<BWFMetaEdit><TAPE>220921</TAPE><TimeReference>2383872001</TimeReference></BWFMetaEdit>"
        )
        with mock.patch.object(WORKFLOW, "read_metadata", return_value=metadata):
            tape, time_reference, scene, take, tc_display = WORKFLOW.read_serial_metadata("bwf", Path("source.wav"))

        self.assertEqual((tape, time_reference, scene, take), ("220921", 2383872001, "", ""))
        # No Speed/sample-rate metadata available, so TC falls back to the raw sample count.
        self.assertEqual(tc_display, "2383872001")

    def test_serial_metadata_reads_scene_and_take_from_description_fallback(self) -> None:
        metadata = WORKFLOW.MetadataRead(
            core_xml=(
                "<BWFMetaEdit><Description>sSCENE=S10-6\nsTAKE=03</Description>"
                "<TimeReference>207648001</TimeReference></BWFMetaEdit>"
            )
        )
        with mock.patch.object(WORKFLOW, "read_metadata", return_value=metadata):
            tape, time_reference, scene, take, tc_display = WORKFLOW.read_serial_metadata("bwf", Path("source.wav"))

        self.assertEqual((tape, time_reference, scene, take), ("", 207648001, "S10-6", "03"))
        self.assertEqual(tc_display, "207648001")

    def test_description_fallback_fields_stop_at_line_boundary_not_end_of_block(self) -> None:
        description = (
            "sSPEED=024.000-ND\r\n"
            "sTAKE=03\r\n"
            "sUBITS=$00000000\r\n"
            "sSWVER=8.92.5806\r\n"
            "sSCENE=S10-6\r\n"
            "sFILENAME=S10-6T03.wav\r\n"
            "sTAPE=220921_Hell\r\n"
            "sCIRCLED=FALSE\r\n"
            "sNOTE=\r\n"
            "sTRK1=Mix L\r\n"
            "sTRK2=Mix R"
        )
        metadata = WORKFLOW.MetadataRead(
            core_xml=(
                f"<BWFMetaEdit><Description>{description}</Description>"
                "<TimeReference>207648000</TimeReference>"
                "<CodingHistory>A=PCM,F=48000,W=24,M=stereo,T=</CodingHistory></BWFMetaEdit>"
            )
        )
        with mock.patch.object(WORKFLOW, "read_metadata", return_value=metadata):
            tape, time_reference, scene, take, tc_display = WORKFLOW.read_serial_metadata("bwf", Path("source.wav"))

        self.assertEqual((tape, time_reference, scene, take), ("220921_Hell", 207648000, "S10-6", "03"))
        # sSPEED=024.000-ND (24 fps, non-drop) plus the CodingHistory sample rate (48000 Hz)
        # are enough to render the raw sample count as a real SMPTE timecode.
        self.assertEqual(tc_display, "01:12:06:00")

    def test_parse_timecode_rate_handles_the_rational_ixml_format(self) -> None:
        # iXML's TIMECODE_RATE is always "N/D" (e.g. "24/1", "30000/1001" for
        # 29.97) — a bare float() on that string raises ValueError, which
        # silently failed this branch for every recorder, real iXML value or
        # not, and pushed every file onto the Description-text fallback.
        self.assertEqual(WORKFLOW.parse_timecode_rate("24/1"), 24.0)
        self.assertAlmostEqual(WORKFLOW.parse_timecode_rate("30000/1001"), 29.97002997, places=5)
        self.assertEqual(WORKFLOW.parse_timecode_rate("24"), 24.0)
        self.assertEqual(WORKFLOW.parse_timecode_rate(""), 0.0)
        self.assertEqual(WORKFLOW.parse_timecode_rate("garbage"), 0.0)
        self.assertEqual(WORKFLOW.parse_timecode_rate("24/0"), 0.0)

    def test_serial_speed_reads_the_rational_ixml_timecode_rate(self) -> None:
        metadata = WORKFLOW.MetadataRead(
            core_xml="<BWFMetaEdit></BWFMetaEdit>",
            ixml_root=WORKFLOW.ET.fromstring("<BWFXML><SPEED><TIMECODE_RATE>24/1</TIMECODE_RATE><TIMECODE_FLAG>NDF</TIMECODE_FLAG></SPEED></BWFXML>"),
        )
        self.assertEqual(WORKFLOW.read_serial_speed(metadata, ""), (24.0, False))

    def test_serial_metadata_field_falls_back_to_aaton_and_normalized_description_prefixes(self) -> None:
        # Aaton Cantar uses "a" ("aTAPE=..."); some re-exports/DAW renders
        # normalize to "d" ("dTAPE=...") — both were previously missed since
        # the fallback regex only recognized "$" and "s".
        aaton_metadata = WORKFLOW.MetadataRead(core_xml="<BWFMetaEdit><Description>aTAPE=0110\naSPEED=024.000-ND</Description></BWFMetaEdit>")
        with mock.patch.object(WORKFLOW, "read_metadata", return_value=aaton_metadata):
            tape, *_rest, tc_display = WORKFLOW.read_serial_metadata("bwf", Path("aaton.wav"))
        self.assertEqual(tape, "0110")

        normalized_metadata = WORKFLOW.MetadataRead(core_xml="<BWFMetaEdit><Description>dTAPE=220921\ndSPEED=024.000-NDF</Description></BWFMetaEdit>")
        with mock.patch.object(WORKFLOW, "read_metadata", return_value=normalized_metadata):
            tape, *_rest = WORKFLOW.read_serial_metadata("bwf", Path("normalized.wav"))
        self.assertEqual(tape, "220921")

    def test_serial_sample_rate_falls_back_to_the_wav_files_own_fmt_chunk(self) -> None:
        # Some recorders (e.g. this project's MixPre-6 II test files) write
        # neither a usable iXML sample-rate field nor a CodingHistory "F="
        # value. The WAV's own fmt chunk always has the real rate regardless
        # of what any metadata field says, so it has to be the last resort
        # rather than silently giving up and leaving TC as a raw sample count.
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "no_metadata_rate.wav"
            fmt = struct.pack("<HHIIHH", 1, 1, 48000, 96000, 2, 16)
            write_riff(wav_path, [(b"fmt ", fmt), (b"data", b"\x00\x00")])
            metadata = WORKFLOW.MetadataRead(core_xml="<BWFMetaEdit><Description>dSPEED=024.000-NDF</Description><TimeReference>1152000</TimeReference></BWFMetaEdit>")
            with mock.patch.object(WORKFLOW, "read_metadata", return_value=metadata):
                _tape, _time_reference, _scene, _take, tc_display = WORKFLOW.read_serial_metadata("bwf", wav_path)

        # 1152000 samples @ 48000 Hz, 24 fps -> exactly 00:00:24:00.
        self.assertEqual(tc_display, "00:00:24:00")

    def test_metadata_read_keeps_core_and_ixml_outcomes_independent(self) -> None:
        wav_path = Path("source.wav")
        ixml = "<BWFXML><PROJECT>TEST\x1a</PROJECT><TRACK_LIST /></BWFXML>"
        with mock.patch.object(WORKFLOW, "export_core_xml", side_effect=RuntimeError("Core unavailable")), mock.patch.object(
            WORKFLOW, "export_optional_ixml_for_inspector", return_value=(ixml, "BWF warning")
        ):
            metadata = WORKFLOW.read_metadata("bwf", wav_path)

        self.assertEqual(metadata.core_xml, "")
        self.assertEqual(metadata.core_error, "Core unavailable")
        self.assertEqual(metadata.ixml_warning, "BWF warning")
        self.assertIsNone(metadata.ixml_error)
        self.assertIsNotNone(metadata.ixml_root)
        self.assertEqual(metadata.ixml_root.findtext("PROJECT"), "TEST")

    def test_metadata_read_represents_absent_and_malformed_ixml_without_core_loss(self) -> None:
        wav_path = Path("source.wav")
        with mock.patch.object(WORKFLOW, "export_core_xml", return_value="<BWFMetaEdit><Core /></BWFMetaEdit>"), mock.patch.object(
            WORKFLOW, "export_optional_ixml_for_inspector", return_value=(None, "iXML sidecar was not exported")
        ):
            absent = WORKFLOW.read_metadata("bwf", wav_path)
        self.assertEqual(absent.core_xml, "<BWFMetaEdit><Core /></BWFMetaEdit>")
        self.assertIsNone(absent.core_error)
        self.assertIsNone(absent.ixml_text)
        self.assertEqual(absent.ixml_warning, "iXML sidecar was not exported")
        self.assertIsNone(absent.ixml_error)

        with mock.patch.object(WORKFLOW, "export_core_xml", return_value="<BWFMetaEdit><Core /></BWFMetaEdit>"), mock.patch.object(
            WORKFLOW, "export_optional_ixml_for_inspector", return_value=("<BWFXML><PROJECT>broken & value</PROJECT></BWFXML>", None)
        ):
            malformed = WORKFLOW.read_metadata("bwf", wav_path)
        self.assertEqual(malformed.core_xml, "<BWFMetaEdit><Core /></BWFMetaEdit>")
        self.assertIsNotNone(malformed.ixml_error)
        self.assertIsNone(malformed.ixml_root)


class MetadataTemplateCharacterizationTests(unittest.TestCase):
    # Engine ported from hsuanice's REAPER "Rename Source File from
    # Metadata" script — a free-form $token / ${token} template, distinct
    # from Rename mode's existing {base}/$-formula and Serial ID features.

    def test_umid_to_pt_reformats_64_hex_into_the_pro_tools_grouping(self) -> None:
        umid = "060A2B340101010501010F10130000003D82D1AEEF6C8000A5DF9281790E769C"
        self.assertEqual(
            WORKFLOW.umid_to_pt(umid),
            "060a2b340101010501010f1013-000000-3d82d1aeef6c8000-a5df9281790e-769c",
        )

    def test_umid_to_pt_rejects_anything_that_is_not_exactly_64_hex_chars(self) -> None:
        self.assertEqual(WORKFLOW.umid_to_pt(""), "")
        self.assertEqual(WORKFLOW.umid_to_pt("not-hex-at-all"), "")
        self.assertEqual(WORKFLOW.umid_to_pt("AB" * 31), "")  # 62 chars, one short

    def test_expand_template_substitutes_plain_metadata_tokens(self) -> None:
        fields = {"tape": "220921", "scene": "S2-1", "take": "01"}

        self.assertEqual(WORKFLOW.expand_metadata_template("$tape_$scene_$take", fields), "220921_S2-1_01")

    def test_expand_template_leaves_unknown_tokens_empty(self) -> None:
        self.assertEqual(WORKFLOW.expand_metadata_template("$nope_end", {}), "_end")

    def test_bare_token_stops_before_underscore_so_a_literal_suffix_survives(self) -> None:
        # Known token names are matched as literal substrings (ported from
        # the source script's normalize_tokens()), so "$scene_v2" becomes
        # the token $scene plus the user's own literal "_v2", instead of an
        # unknown "scene_v2" token consuming it and producing nothing.
        fields = {"scene": "S2-1"}

        self.assertEqual(WORKFLOW.expand_metadata_template("$scene_v2", fields), "S2-1_v2")

    def test_a_known_token_directly_followed_by_a_letter_is_not_swallowed(self) -> None:
        # The exact case the user hit: "$sceneT$take" should read as $scene
        # + literal "T" + $take (yielding e.g. "3-25A-1T1"), not an
        # unrecognized "scenet" token that eats the "T" and the take number
        # both. Confirmed against the source script's own documented fix
        # for this (v0.7.3: "Adjacent letters/digits are now safe").
        fields = {"scene": "3-25A-1", "take": "1"}

        self.assertEqual(WORKFLOW.expand_metadata_template("$sceneT$take", fields), "3-25A-1T1")

    def test_longer_token_name_is_matched_before_its_own_prefix(self) -> None:
        # "trkall" must be recognized as one token, not "trk" + literal
        # "all" — normalize_metadata_template_tokens() sorts known names
        # longest-first specifically to guarantee this.
        fields = {"trk": "Boom", "trkall": "Boom_Lav"}

        self.assertEqual(WORKFLOW.expand_metadata_template("$trkall", fields), "Boom_Lav")

    def test_template_token_names_lists_unique_tokens_in_first_seen_order(self) -> None:
        self.assertEqual(
            WORKFLOW.template_token_names("$sceneT$take-$tape-$scene"),
            ["scene", "take", "tape"],
        )

    def test_find_empty_metadata_tokens_reports_only_the_unresolved_ones(self) -> None:
        fields = {"scene": "3-25A-1", "take": "1"}

        self.assertEqual(WORKFLOW.find_empty_metadata_tokens("$sceneT$take-$tape-${umid_pt}", fields), ["tape", "umid_pt"])

    def test_find_empty_metadata_tokens_reports_nothing_when_everything_resolves(self) -> None:
        fields = {"scene": "3-25A-1", "take": "1"}

        self.assertEqual(WORKFLOW.find_empty_metadata_tokens("$sceneT$take", fields), [])

    def test_braced_form_resolves_a_token_name_containing_an_underscore(self) -> None:
        fields = {"umid_pt": "060a2b34-...-769c"}

        self.assertEqual(WORKFLOW.expand_metadata_template("${umid_pt}", fields), "060a2b34-...-769c")

    def test_counter_token_zero_pads_only_inside_braces(self) -> None:
        self.assertEqual(WORKFLOW.expand_metadata_template("${counter:3}", {}, counter=7), "007")
        self.assertEqual(WORKFLOW.expand_metadata_template("$counter", {}, counter=7), "7")

    def test_srcbaseprefix_and_suffix_slice_the_source_base_name(self) -> None:
        fields = {"srcbase": "S2-1T01.A2"}

        self.assertEqual(WORKFLOW.expand_metadata_template("${srcbaseprefix:6}", fields), "S2-1T0")
        self.assertEqual(WORKFLOW.expand_metadata_template("${srcbasesuffix:2}", fields), "A2")

    def test_trkn_resolves_from_the_trk_table_independent_of_the_current_trk_field(self) -> None:
        fields = {"trk": "Mix R"}
        trk_table = {1: "Mix L", 2: "Mix R"}

        self.assertEqual(WORKFLOW.expand_metadata_template("$trk1", fields, trk_table), "Mix L")

    def test_sanitize_strips_unsafe_characters_from_the_resolved_value_only(self) -> None:
        fields = {"scene": "S2/1:Bad*Scene"}

        # The literal underscore separators the user typed must survive —
        # only what came out of metadata gets sanitized.
        self.assertEqual(WORKFLOW.expand_metadata_template("$scene_ok", fields), "S2_1_Bad_Scene_ok")

    def test_sanitize_can_be_disabled_for_non_filename_uses(self) -> None:
        fields = {"scene": "S2/1"}

        self.assertEqual(WORKFLOW.expand_metadata_template("$scene", fields, sanitize=False), "S2/1")

    def test_build_trk_table_keys_by_recorder_channel_and_skips_blank_names(self) -> None:
        rows = [("1", "3", "Boom"), ("2", "4", ""), ("3", "9", "Lav")]

        self.assertEqual(WORKFLOW.build_trk_table_from_ixml_rows(rows), {3: "Boom", 9: "Lav"})

    def test_concat_trk_names_joins_in_channel_order_skipping_blanks(self) -> None:
        self.assertEqual(WORKFLOW.concat_trk_names({9: "Lav", 3: "Boom", 4: ""}), "Boom_Lav")

    def test_read_ixml_history_original_filename_reads_the_standard_tag(self) -> None:
        root = WORKFLOW.ET.fromstring(
            "<BWFXML><HISTORY><ORIGINAL_FILENAME>S2-1T01.wav</ORIGINAL_FILENAME>"
            "<CURRENT_FILENAME>S2-1T01.A2.wav</CURRENT_FILENAME></HISTORY></BWFXML>"
        )

        self.assertEqual(WORKFLOW.read_ixml_history_original_filename(root), "S2-1T01.wav")

    def test_read_ixml_history_original_filename_handles_missing_root_or_tag(self) -> None:
        self.assertEqual(WORKFLOW.read_ixml_history_original_filename(None), "")
        self.assertEqual(WORKFLOW.read_ixml_history_original_filename(WORKFLOW.ET.fromstring("<BWFXML/>")), "")

    def test_collect_metadata_template_fields_reads_the_full_token_set_from_real_metadata(self) -> None:
        # Mirrors this project's real "S2-1T01.A2.wav" test file: iXML
        # TRACK_LIST names channel 2 "Mix R", HISTORY records the pre-split
        # poly filename, and Description carries the sTAPE/sSCENE/sTAKE/
        # sPROJECT/sUBITS fallback fields bwfmetaedit's Core export doesn't
        # break out into their own tags.
        ixml_root = WORKFLOW.ET.fromstring(
            "<BWFXML>"
            "<HISTORY><ORIGINAL_FILENAME>S2-1T01.wav</ORIGINAL_FILENAME></HISTORY>"
            "<TRACK_LIST><TRACK><INTERLEAVE_INDEX>1</INTERLEAVE_INDEX><NAME>Mix R</NAME>"
            "<CHANNEL_INDEX>2</CHANNEL_INDEX></TRACK></TRACK_LIST>"
            "</BWFXML>"
        )
        metadata = WORKFLOW.MetadataRead(
            core_xml=(
                "<BWFMetaEdit><Description>sPROJECT=Hell\nsSCENE=S2-1\nsTAKE=01\nsTAPE=220921\n"
                "sUBITS=00000000</Description>"
                "<OriginationDate>2022-09-21</OriginationDate><OriginationTime>13:59:51</OriginationTime>"
                "<TimeReference>2418384001</TimeReference>"
                "<UMID>060A2B340101010501010F10130000003D82D1AEEF6C8000A5DF9281790E769C</UMID></BWFMetaEdit>"
            ),
            ixml_root=ixml_root,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "S2-1T01.A2.wav"
            wav_path.touch()
            with mock.patch.object(WORKFLOW, "read_metadata", return_value=metadata), mock.patch.object(
                WORKFLOW, "split_recorder_channel", return_value=2
            ), mock.patch.object(
                WORKFLOW, "read_wav_pcm_layout", return_value={"sample_rate": 48000, "channels": 1, "frames": 1344000}
            ):
                fields, trk_table = WORKFLOW.collect_metadata_template_fields("bwf", wav_path)

        self.assertEqual(trk_table, {2: "Mix R"})
        self.assertEqual(fields["project"], "Hell")
        self.assertEqual(fields["scene"], "S2-1")
        self.assertEqual(fields["take"], "01")
        self.assertEqual(fields["tape"], "220921")
        self.assertEqual(fields["ubits"], "00000000")
        self.assertEqual(fields["originationdate"], "2022-09-21")
        self.assertEqual(fields["year"], "2022")
        self.assertEqual(fields["startoffset"], "2418384001")
        self.assertEqual(fields["trk"], "Mix R")
        self.assertEqual(fields["chnum"], "2")
        self.assertEqual(fields["samplerate"], "48000")
        self.assertEqual(fields["channels"], "1")
        self.assertEqual(fields["length"], "28.000")
        self.assertEqual(fields["umid_pt"], "060a2b340101010501010f1013-000000-3d82d1aeef6c8000-a5df9281790e-769c")
        self.assertEqual(fields["origsrcfile"], "S2-1T01.wav")
        self.assertEqual(fields["srcfile"], "S2-1T01.A2.wav")
        self.assertEqual(fields["srcbase"], "S2-1T01.A2")


class QcMetadataComparisonCharacterizationTests(unittest.TestCase):
    def _metadata(self, tape: str, scene: str, take: str, time_reference: str = "100") -> "WORKFLOW.MetadataRead":
        return WORKFLOW.MetadataRead(
            core_xml=(
                f"<BWFMetaEdit><TAPE>{tape}</TAPE><SCENE>{scene}</SCENE><TAKE>{take}</TAKE>"
                f"<TimeReference>{time_reference}</TimeReference><Originator>REC</Originator>"
                "<OriginationDate>2026-01-10</OriginationDate><OriginationTime>07:18:35</OriginationTime>"
                "<Description>notes</Description></BWFMetaEdit>"
            )
        )

    def test_compare_qc_metadata_fields_detects_a_single_differing_field(self) -> None:
        source_metadata = self._metadata("0110", "56-1", "0")
        target_metadata = self._metadata("0110", "56-1", "1")
        with mock.patch.object(WORKFLOW, "read_metadata", side_effect=[source_metadata, target_metadata]):
            comparison = WORKFLOW.compare_qc_metadata_fields("bwf", Path("source.wav"), Path("target.wav"))

        self.assertEqual(comparison["result"], "DIFFERENT")
        take_row = next(row for row in comparison["rows"] if row["label"] == "Take")
        self.assertEqual((take_row["source"], take_row["target"], take_row["result"]), ("0", "1", "≠"))
        tape_row = next(row for row in comparison["rows"] if row["label"] == "Tape")
        self.assertEqual(tape_row["result"], "✓")

    def test_compare_qc_metadata_fields_passes_when_everything_matches(self) -> None:
        metadata = self._metadata("0110", "56-1", "0")
        with mock.patch.object(WORKFLOW, "read_metadata", side_effect=[metadata, metadata]):
            comparison = WORKFLOW.compare_qc_metadata_fields("bwf", Path("source.wav"), Path("target.wav"))

        self.assertEqual(comparison["result"], "PASS")
        self.assertTrue(all(row["result"] == "✓" for row in comparison["rows"]))

    def test_augment_qc_rows_with_metadata_keeps_audio_result_separate_from_metadata_result(self) -> None:
        # Audio and Metadata are shown as their own columns, so a metadata
        # mismatch must not overwrite an already-passing audio result.
        rows = [{"result": "PASS", "source_path": Path("a.wav"), "target_path": Path("b.wav")}]
        with mock.patch.object(WORKFLOW, "compare_qc_metadata_fields", return_value={"rows": [], "result": "DIFFERENT"}):
            WORKFLOW.augment_qc_rows_with_metadata("bwf", rows, "source_path", "target_path")

        self.assertEqual(rows[0]["metadata_result"], "DIFFERENT")
        self.assertEqual(rows[0]["result"], "PASS")

    def test_qc_rows_overall_result_considers_both_audio_and_metadata(self) -> None:
        all_pass = [{"result": "PASS", "metadata_result": "PASS"}]
        metadata_only_fails = [{"result": "PASS", "metadata_result": "DIFFERENT"}]
        audio_only_fails = [{"result": "DIFFERENT", "metadata_result": "PASS"}]
        not_compared = [{"result": "PASS", "metadata_result": "-"}]
        has_blocked = [{"result": "BLOCKED: no iXML", "metadata_result": "-"}]

        self.assertEqual(WORKFLOW.qc_rows_overall_result(all_pass), "PASS")
        self.assertEqual(WORKFLOW.qc_rows_overall_result(metadata_only_fails), "DIFFERENT")
        self.assertEqual(WORKFLOW.qc_rows_overall_result(audio_only_fails), "DIFFERENT")
        self.assertEqual(WORKFLOW.qc_rows_overall_result(not_compared), "PASS")
        self.assertEqual(WORKFLOW.qc_rows_overall_result(has_blocked), "BLOCKED")
        self.assertEqual(WORKFLOW.qc_rows_overall_result([]), "DIFFERENT")

    def test_augment_qc_rows_with_metadata_skips_rows_without_both_paths(self) -> None:
        rows = [{"result": "EXTRA", "source_path": None, "target_path": Path("b.wav")}]
        WORKFLOW.augment_qc_rows_with_metadata("bwf", rows, "source_path", "target_path")

        self.assertEqual(rows[0]["metadata_result"], "-")
        self.assertEqual(rows[0]["result"], "EXTRA")

    def test_build_file_qc_comparison_respects_toggles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.wav"
            target = Path(temp_dir) / "target.wav"
            source.write_bytes(b"0")
            target.write_bytes(b"00")
            with mock.patch.object(WORKFLOW, "compare_basic_audio", return_value={"audio": [{"label": "Sample Rate", "source": "48000", "target": "48000", "result": "✓"}], "result": "PASS"}), mock.patch.object(
                WORKFLOW, "compare_qc_metadata_fields", return_value={"rows": [{"label": "Tape", "source": "A", "target": "B", "result": "≠"}], "result": "DIFFERENT"}
            ):
                both_on = WORKFLOW.build_file_qc_comparison("soxi", "bwf", source, target, True, True)
                audio_only = WORKFLOW.build_file_qc_comparison("soxi", "bwf", source, target, True, False)
                neither = WORKFLOW.build_file_qc_comparison("soxi", "bwf", source, target, False, False)

        self.assertEqual([name for name, _rows in both_on["sections"]], ["FILE", "AUDIO", "METADATA"])
        self.assertEqual(both_on["result"], "DIFFERENT")
        self.assertEqual([name for name, _rows in audio_only["sections"]], ["FILE", "AUDIO"])
        self.assertEqual(audio_only["result"], "PASS")
        self.assertEqual([name for name, _rows in neither["sections"]], ["FILE"])
        self.assertEqual(neither["result"], "SKIPPED")

    def test_apply_qc_metadata_update_copies_bext_clears_list_and_writes_ixml(self) -> None:
        calls = []
        with mock.patch.object(WORKFLOW, "copy_wav_chunk_from_source", side_effect=lambda *a: calls.append(("copy_bext", a))), mock.patch.object(
            WORKFLOW, "remove_wav_chunk", side_effect=lambda *a: calls.append(("remove_list", a))
        ), mock.patch.object(WORKFLOW, "export_optional_ixml_for_inspector", return_value=("<BWFXML/>", None)), mock.patch.object(
            WORKFLOW, "write_ixml", side_effect=lambda *a: calls.append(("write_ixml", a))
        ):
            WORKFLOW.apply_qc_metadata_update("bwf", Path("source.wav"), Path("target.wav"))

        self.assertEqual(
            [name for name, _args in calls],
            ["copy_bext", "remove_list", "write_ixml"],
        )
        self.assertEqual(calls[0][1], (Path("source.wav"), Path("target.wav"), b"bext"))
        self.assertEqual(calls[1][1], (Path("target.wav"), b"LIST"))
        self.assertEqual(calls[2][1], ("bwf", Path("target.wav"), "<BWFXML/>"))

    def test_apply_qc_metadata_update_poly_to_split_uses_the_matching_channel_slice(self) -> None:
        poly_ixml = "<BWFXML><TRACK_LIST><TRACK_COUNT>2</TRACK_COUNT><TRACK><CHANNEL_INDEX>1</CHANNEL_INDEX><INTERLEAVE_INDEX>1</INTERLEAVE_INDEX></TRACK><TRACK><CHANNEL_INDEX>5</CHANNEL_INDEX><INTERLEAVE_INDEX>2</INTERLEAVE_INDEX></TRACK></TRACK_LIST></BWFXML>"
        written = {}
        with mock.patch.object(WORKFLOW, "export_optional_ixml_for_inspector", return_value=(poly_ixml, None)), mock.patch.object(
            WORKFLOW, "copy_wav_chunk_from_source"
        ), mock.patch.object(WORKFLOW, "remove_wav_chunk"), mock.patch.object(
            WORKFLOW, "write_ixml", side_effect=lambda _tool, target, ixml_text: written.update(target=target, ixml_text=ixml_text)
        ):
            WORKFLOW.apply_qc_metadata_update_poly_to_split("bwf", Path("poly.wav"), 2, Path("split.wav"))

        self.assertEqual(written["target"], Path("split.wav"))
        root = WORKFLOW.parse_source_ixml(written["ixml_text"], Path("split.wav"))
        self.assertEqual(root.findtext(".//CHANNEL_INDEX"), "5")
        self.assertEqual(root.findtext(".//INTERLEAVE_INDEX"), "1")

    def test_compare_qc_folders_matches_differently_named_files_by_name_prefix(self) -> None:
        # Mirrors the real case: Source keeps its recorder-original name, the
        # Target went through a DAW render that appended " render 001" — no
        # formula: the Target's name starts with the Source's name, and the
        # recorder channel confirms the pair.
        with tempfile.TemporaryDirectory() as temp_dir:
            source_folder = Path(temp_dir) / "source"
            target_folder = Path(temp_dir) / "target"
            source_folder.mkdir()
            target_folder.mkdir()
            source_file = source_folder / "Scene.A1.wav"
            target_file = target_folder / "Scene.A1 render 001.wav"
            source_file.touch()
            target_file.touch()
            channel_by_path = {source_file: 3, target_file: 3}

            with mock.patch.object(WORKFLOW, "split_recorder_channel", side_effect=lambda _tool, path: channel_by_path[path]), mock.patch.object(
                WORKFLOW, "compare_basic_audio", return_value={"audio": [], "result": "PASS"}
            ):
                comparison = WORKFLOW.compare_qc_folders("soxi", source_folder, target_folder, None, "bwf", True, False)

        self.assertEqual(comparison["result"], "PASS")
        self.assertEqual(len(comparison["rows"]), 1)
        row = comparison["rows"][0]
        self.assertEqual(row["filename"], "Scene.A1.wav ↔ Scene.A1 render 001.wav")
        self.assertEqual(row["result"], "PASS")
        self.assertEqual((row["source_path"], row["target_path"]), (source_file, target_file))

    def test_compare_qc_folders_leaves_differently_named_files_unmatched_without_bwfmetaedit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_folder = Path(temp_dir) / "source"
            target_folder = Path(temp_dir) / "target"
            source_folder.mkdir()
            target_folder.mkdir()
            (source_folder / "Scene.A1.wav").touch()
            (target_folder / "Scene.A1 render 001.wav").touch()

            comparison = WORKFLOW.compare_qc_folders("soxi", source_folder, target_folder)

        results = {row["filename"]: row["result"] for row in comparison["rows"]}
        self.assertEqual(results, {"Scene.A1.wav": "MISSING", "Scene.A1 render 001.wav": "EXTRA"})

    def test_compare_qc_folders_prefers_exact_filename_match_over_formula_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_folder = Path(temp_dir) / "source"
            target_folder = Path(temp_dir) / "target"
            source_folder.mkdir()
            target_folder.mkdir()
            shared_name = "Scene.A1.wav"
            (source_folder / shared_name).touch()
            (target_folder / shared_name).touch()

            with mock.patch.object(WORKFLOW, "split_recorder_channel") as mock_channel, mock.patch.object(
                WORKFLOW, "compare_basic_audio", return_value={"audio": [], "result": "PASS"}
            ):
                comparison = WORKFLOW.compare_qc_folders("soxi", source_folder, target_folder, None, "bwf", True, False)

        mock_channel.assert_not_called()
        self.assertEqual(comparison["rows"][0]["filename"], shared_name)

    def test_compare_qc_folders_marks_audio_not_compared_when_audio_toggle_is_off(self) -> None:
        # A plugin-processed render can keep the exact same sample rate,
        # bit depth, channel count, frame count, and duration as its source
        # while every actual sample differs — compare_basic_audio only ever
        # checks those soxi-derived properties, so leaving it hard-coded to
        # "PASS" when the Audio checkbox is off looked identical to a real
        # pass. It must read as "not compared" (matching the "-" convention
        # already used for a skipped Metadata Result) instead.
        with tempfile.TemporaryDirectory() as temp_dir:
            source_folder = Path(temp_dir) / "source"
            target_folder = Path(temp_dir) / "target"
            source_folder.mkdir()
            target_folder.mkdir()
            (source_folder / "Scene.A1.wav").touch()
            (target_folder / "Scene.A1.wav").touch()

            with mock.patch.object(WORKFLOW, "compare_basic_audio") as mock_compare_audio:
                comparison = WORKFLOW.compare_qc_folders("soxi", source_folder, target_folder, include_audio=False)

        mock_compare_audio.assert_not_called()
        self.assertEqual(comparison["rows"][0]["result"], "-")
        self.assertEqual(comparison["result"], "PASS")

    def test_compare_qc_folders_caches_the_full_audio_breakdown_on_each_row(self) -> None:
        # The Folder QC report needs every individual field (Sample Rate,
        # Bit Depth, ...) alongside its Source/Target values, not just the
        # collapsed PASS/DIFFERENT — caching the full compare_basic_audio()
        # dict on the row means the report writer never has to re-run soxi.
        audio_comparison = {
            "file": [{"label": "File Name", "source": "a.wav", "target": "a.wav", "result": "—"}],
            "audio": [{"label": "Sample Rate", "source": "48000", "target": "48000", "result": "✓"}],
            "result": "PASS",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            source_folder = Path(temp_dir) / "source"
            target_folder = Path(temp_dir) / "target"
            source_folder.mkdir()
            target_folder.mkdir()
            (source_folder / "a.wav").touch()
            (target_folder / "a.wav").touch()

            with mock.patch.object(WORKFLOW, "compare_basic_audio", return_value=audio_comparison):
                comparison = WORKFLOW.compare_qc_folders("soxi", source_folder, target_folder, include_audio=True)

        self.assertEqual(comparison["rows"][0]["audio_comparison"], audio_comparison)

    def test_augment_qc_rows_with_metadata_caches_the_full_field_breakdown(self) -> None:
        metadata_comparison = {"rows": [{"label": "Tape", "source": "0110", "target": "0110", "result": "✓"}], "result": "PASS"}
        rows = [{"result": "PASS", "source_path": Path("a.wav"), "target_path": Path("b.wav")}]
        with mock.patch.object(WORKFLOW, "compare_qc_metadata_fields", return_value=metadata_comparison):
            WORKFLOW.augment_qc_rows_with_metadata("bwf", rows, "source_path", "target_path")

        self.assertEqual(rows[0]["metadata_comparison"], metadata_comparison)

    def test_augment_qc_rows_with_metadata_clears_comparison_when_nothing_to_compare(self) -> None:
        rows = [{"result": "EXTRA", "source_path": None, "target_path": Path("b.wav")}]
        WORKFLOW.augment_qc_rows_with_metadata("bwf", rows, "source_path", "target_path")

        self.assertIsNone(rows[0]["metadata_comparison"])

    def test_augment_qc_rows_with_metadata_reports_progress_per_row(self) -> None:
        # This pass runs two bwfmetaedit reads per row after the Audio pass
        # has already finished reporting — on a large Folder QC batch that
        # used to be a multi-second silent gap between the last "Comparing"
        # log line and the final result, with nothing telling the user what
        # was still running.
        rows = [
            {"source_path": Path("a1.wav"), "target_path": Path("b1.wav")},
            {"source_path": None, "target_path": Path("b2.wav")},
            {"source_path": Path("a3.wav"), "target_path": Path("b3.wav")},
        ]
        calls = []
        with mock.patch.object(WORKFLOW, "compare_qc_metadata_fields", return_value={"rows": [], "result": "PASS"}):
            WORKFLOW.augment_qc_rows_with_metadata("bwf", rows, "source_path", "target_path", progress=lambda *args: calls.append(args))

        self.assertEqual(calls, [("Comparing Metadata", 1, 3), ("Comparing Metadata", 2, 3), ("Comparing Metadata", 3, 3)])


class InspectorScrollCharacterizationTests(unittest.TestCase):
    def test_nearest_available_heading_keeps_an_exact_match(self) -> None:
        nearest = WORKFLOW.AudioInspector._nearest_available_heading
        old_order = ["FILE", "AUDIO", "HUGE", "BEXT"]

        self.assertEqual(nearest("BEXT", old_order, ["FILE", "AUDIO", "BEXT"]), "BEXT")

    def test_nearest_available_heading_falls_back_to_the_closest_earlier_section(self) -> None:
        # Switching to a file whose iXML lacks a section the previous file
        # had (e.g. no Track List) shouldn't reset to the very top — it
        # should land on whatever section came right before it, if the new
        # file has that one.
        nearest = WORKFLOW.AudioInspector._nearest_available_heading
        old_order = ["FILE", "AUDIO", "HUGE", "BEXT"]

        self.assertEqual(nearest("HUGE", old_order, ["FILE", "AUDIO", "BEXT"]), "AUDIO")
        self.assertEqual(nearest("HUGE", old_order, ["FILE", "BEXT"]), "FILE")

    def test_nearest_available_heading_returns_none_when_nothing_earlier_matches(self) -> None:
        nearest = WORKFLOW.AudioInspector._nearest_available_heading
        old_order = ["FILE", "AUDIO", "HUGE", "BEXT"]

        self.assertIsNone(nearest("HUGE", old_order, ["BEXT"]))
        self.assertIsNone(nearest(None, old_order, ["FILE"]))


class InspectorPathLabelResizeCharacterizationTests(unittest.TestCase):
    class _FakeLabel:
        def __init__(self, wraplength: int) -> None:
            self._options = {"wraplength": wraplength}
            self.configure_calls: list[int] = []

        def cget(self, name: str):
            return self._options[name]

        def configure(self, **kwargs) -> None:
            self._options.update(kwargs)
            self.configure_calls.append(kwargs["wraplength"])

    def test_resize_updates_wraplength_on_a_real_width_change(self) -> None:
        resize = WORKFLOW.AudioInspector._resize_path_label
        label = self._FakeLabel(wraplength=480)

        resize(mock.Mock(width=300), label)

        self.assertEqual(label.cget("wraplength"), 284)
        self.assertEqual(label.configure_calls, [284])

    def test_resize_ignores_sub_pixel_jitter_to_avoid_a_configure_feedback_loop(self) -> None:
        # A dynamic wraplength derived from the container's own <Configure>
        # width can hunt forever: re-wrapping the label changes its
        # requested height, which perturbs the frame's geometry negotiation
        # by a couple of pixels, which re-fires <Configure>, which
        # recomputes wraplength again — without a tolerance guard this never
        # converges and pins the Tk main loop (observed as a frozen,
        # "shaking" Inspector window). Small width deltas must be ignored.
        resize = WORKFLOW.AudioInspector._resize_path_label
        label = self._FakeLabel(wraplength=284)

        resize(mock.Mock(width=302), label)  # would compute 286 — a 2px delta

        self.assertEqual(label.cget("wraplength"), 284)
        self.assertEqual(label.configure_calls, [])

    def test_resize_still_tracks_a_real_resize_even_when_it_starts_small(self) -> None:
        resize = WORKFLOW.AudioInspector._resize_path_label
        label = self._FakeLabel(wraplength=284)

        resize(mock.Mock(width=400), label)  # an 8px+ delta must still apply

        self.assertEqual(label.cget("wraplength"), 384)

    def test_qc_input_picker_path_label_wraps_to_its_own_cell_width(self) -> None:
        # QcInputPicker's SOURCE/TARGET path label used a fixed wraplength
        # (420px) instead of tracking its actual cell width, so on a
        # narrower window the path was hard-clipped instead of wrapping to a
        # second line — the label's own <Configure> now drives this the same
        # tolerance-guarded way as the Inspector's path labels.
        label = self._FakeLabel(wraplength=420)

        WORKFLOW.resize_wrapped_label(mock.Mock(width=300), label)

        self.assertEqual(label.cget("wraplength"), 296)


class SourcePathStaleCharacterizationTests(unittest.TestCase):
    def test_not_stale_when_no_jobs_have_been_built_yet(self) -> None:
        self.assertFalse(WORKFLOW.source_path_is_stale("/anything", None))

    def test_not_stale_while_the_field_is_still_being_typed_into_empty(self) -> None:
        self.assertFalse(WORKFLOW.source_path_is_stale("   ", Path("/old")))

    def test_not_stale_when_source_still_matches_the_jobs_root(self) -> None:
        self.assertFalse(WORKFLOW.source_path_is_stale("/old/folder", Path("/old/folder")))

    def test_stale_once_source_points_somewhere_else(self) -> None:
        # This is the exact failure the user hit: typing a new Source path
        # without clicking Preview left the old jobs list (and whatever
        # Execute would act on) silently describing the old folder.
        self.assertTrue(WORKFLOW.source_path_is_stale("/new/folder", Path("/old/folder")))


class TargetPathStaleCharacterizationTests(unittest.TestCase):
    # Split, Combine, and Re-embed all build the jobs list against a cached
    # output root rather than re-resolving Target live (Rename is the one
    # exception, handled separately by not calling this at all) — so this
    # mirrors preview()'s own Target resolution, including its fallback to
    # a synthetic "__preview_output__" folder when Target is blank.
    def test_not_stale_when_target_still_resolves_to_the_jobs_output_root(self) -> None:
        source_root = Path("/source")
        self.assertFalse(WORKFLOW.target_path_is_stale("/target", source_root, Path("/target")))

    def test_not_stale_when_blank_target_still_matches_the_synthetic_fallback(self) -> None:
        source_root = Path("/source")
        jobs_output_root = source_root.parent / "__preview_output__"
        self.assertFalse(WORKFLOW.target_path_is_stale("", source_root, jobs_output_root))

    def test_stale_once_target_resolves_somewhere_else(self) -> None:
        source_root = Path("/source")
        self.assertTrue(WORKFLOW.target_path_is_stale("/new-target", source_root, Path("/old-target")))

    def test_relative_target_resolves_against_the_sources_parent_before_comparing(self) -> None:
        source_root = Path("/root/source")
        self.assertFalse(WORKFLOW.target_path_is_stale("Renamed", source_root, Path("/root/Renamed")))


class QcUpdateSelectedButtonCharacterizationTests(unittest.TestCase):
    def test_disabled_when_nothing_is_selected_even_if_metadata_was_compared(self) -> None:
        # Previously the button stayed enabled the moment any metadata
        # compare had run, regardless of whether a row was actually
        # selected — clicking it with an empty selection (or an
        # all-PASS QC result) only ever led to a "select rows first"
        # dialog, which read as a pointless, always-clickable button.
        self.assertFalse(WORKFLOW.qc_update_selected_button_enabled(has_metadata=True, has_selection=False))

    def test_disabled_when_metadata_was_never_compared(self) -> None:
        self.assertFalse(WORKFLOW.qc_update_selected_button_enabled(has_metadata=False, has_selection=True))

    def test_enabled_once_metadata_was_compared_and_a_row_is_selected(self) -> None:
        self.assertTrue(WORKFLOW.qc_update_selected_button_enabled(has_metadata=True, has_selection=True))


class QcReportDetailCharacterizationTests(unittest.TestCase):
    def test_qc_result_fill_key_classifies_pass_different_and_blocked(self) -> None:
        self.assertEqual(WORKFLOW.qc_result_fill_key("✓"), "pass")
        self.assertEqual(WORKFLOW.qc_result_fill_key("PASS"), "pass")
        self.assertEqual(WORKFLOW.qc_result_fill_key("≠"), "different")
        self.assertEqual(WORKFLOW.qc_result_fill_key("DIFFERENT"), "different")
        self.assertEqual(WORKFLOW.qc_result_fill_key("MISSING"), "different")
        self.assertEqual(WORKFLOW.qc_result_fill_key("EXTRA"), "different")
        self.assertEqual(WORKFLOW.qc_result_fill_key("BLOCKED: no iXML"), "blocked")
        self.assertIsNone(WORKFLOW.qc_result_fill_key("—"))
        self.assertIsNone(WORKFLOW.qc_result_fill_key("-"))

    def test_qc_detail_field_lookup_reads_from_audio_and_metadata_comparisons(self) -> None:
        row = {
            "audio_comparison": {
                "file": [{"label": "File Name", "source": "a.wav", "target": "b.wav", "result": "—"}],
                "audio": [{"label": "Sample Rate", "source": "48000", "target": "44100", "result": "≠"}],
                "result": "DIFFERENT",
            },
            "metadata_comparison": {"rows": [{"label": "Tape", "source": "0110", "target": "0110", "result": "✓"}], "result": "PASS"},
        }

        self.assertEqual(WORKFLOW.qc_detail_field_lookup(row, "Sample Rate")["result"], "≠")
        self.assertEqual(WORKFLOW.qc_detail_field_lookup(row, "Tape")["result"], "✓")
        self.assertIsNone(WORKFLOW.qc_detail_field_lookup(row, "Take"))

    def test_qc_detail_field_lookup_returns_none_when_comparison_never_ran(self) -> None:
        # A Poly row has no audio_comparison at all (Channels legitimately
        # differs between a poly file and its mono split); a Folder row with
        # Audio unchecked, or a MISSING/EXTRA row, has neither comparison.
        self.assertIsNone(WORKFLOW.qc_detail_field_lookup({"metadata_comparison": None}, "Sample Rate"))
        self.assertIsNone(WORKFLOW.qc_detail_field_lookup({}, "Tape"))

    def test_qc_report_detail_cells_blanks_out_fields_that_never_ran(self) -> None:
        row = {
            "audio_comparison": None,
            "metadata_comparison": {"rows": [{"label": "Tape", "source": "0110", "target": "0110", "result": "✓"}], "result": "PASS"},
        }

        cells = WORKFLOW.qc_report_detail_cells(row)
        columns = WORKFLOW.qc_report_detail_columns()

        self.assertEqual(len(cells), len(columns))
        by_column = dict(zip(columns, cells))
        self.assertEqual(by_column["Sample Rate (Source)"], (None, None))
        self.assertEqual(by_column["Tape (Source)"], ("0110", "pass"))
        self.assertEqual(by_column["Tape (Target)"], ("0110", "pass"))


class DisplayStatusCharacterizationTests(unittest.TestCase):
    def _rename_job(self) -> "WORKFLOW.Job":
        return WORKFLOW.Job(Path("source.wav"), Path("target.wav"), 0, None, None, "READY", "rename")

    def test_ready_status_lists_only_enabled_rename_actions(self) -> None:
        job = self._rename_job()

        self.assertEqual(
            WORKFLOW.display_status(job, "Trk#", "Trk#", rename_apply_formula=False, rename_change_number=False),
            "No rename action selected",
        )
        self.assertEqual(
            WORKFLOW.display_status(job, "Trk#", "Trk#", rename_apply_formula=True, rename_change_number=False),
            "Ready to Add Text",
        )
        self.assertEqual(
            WORKFLOW.display_status(
                job, "Trk#", "Trk#", rename_apply_formula=False, rename_change_number=False, rename_serial_enabled=True
            ),
            "Ready to Add ID",
        )

    def test_ready_status_combines_all_four_rename_actions_in_order(self) -> None:
        job = self._rename_job()

        self.assertEqual(
            WORKFLOW.display_status(
                job,
                "Trk#",
                "Trk#",
                rename_apply_formula=True,
                rename_change_number=True,
                rename_find_replace_enabled=True,
                rename_serial_enabled=True,
            ),
            "Ready to Add Text, Find & Replace, Change to Trk#, and Add ID",
        )

    def test_ready_status_uses_plain_and_for_exactly_two_actions(self) -> None:
        job = self._rename_job()

        self.assertEqual(
            WORKFLOW.display_status(
                job, "Trk#", "Trk#", rename_apply_formula=True, rename_change_number=False, rename_serial_enabled=True
            ),
            "Ready to Add Text and Add ID",
        )

    def _poly_rename_job(self, status: str = "READY") -> "WORKFLOW.Job":
        return WORKFLOW.Job(Path("T1921.wav"), Path("T1921_test.wav"), 0, None, None, status, "rename", has_channel_suffix=False)

    def test_change_to_action_omitted_for_poly_files_without_a_channel_suffix(self) -> None:
        job = self._poly_rename_job()

        self.assertEqual(
            WORKFLOW.display_status(
                job, "Trk#", "Trk#", rename_apply_formula=True, rename_change_number=True
            ),
            "Ready to Add Text",
        )

    def test_change_only_request_on_poly_file_reports_no_applicable_channel_number(self) -> None:
        job = self._poly_rename_job(status="UNCHANGED")

        self.assertEqual(
            WORKFLOW.display_status(
                job, "Trk#", "Trk#", rename_apply_formula=False, rename_change_number=True
            ),
            "No change: no channel number to change (Poly / no split suffix)",
        )

    def test_change_to_action_still_applies_when_channel_suffix_is_present(self) -> None:
        job = WORKFLOW.Job(Path("Scene.A1.wav"), Path("Scene.A2.wav"), 1, 2, 1, "READY", "rename", has_channel_suffix=True)

        self.assertEqual(
            WORKFLOW.display_status(job, "Trk#", "Trk#", rename_apply_formula=False, rename_change_number=True),
            "Ready to Change to Trk#",
        )

    def test_metadata_template_label_appears_in_the_action_list(self) -> None:
        job = WORKFLOW.Job(Path("Scene.A1.wav"), Path("Scene.A2.wav"), 1, 2, 1, "READY", "rename", has_channel_suffix=True)

        self.assertEqual(
            WORKFLOW.display_status(
                job, "Trk#", "Trk#", rename_apply_formula=False, rename_change_number=False,
                rename_metadata_template_label="Metadata Template",
            ),
            "Ready to Metadata Template",
        )

    def test_metadata_template_label_can_flag_missing_tokens_in_the_status(self) -> None:
        # This is what the Status column (and the exported TSV, which
        # writes this same status) shows when a token like $tape resolved
        # to nothing for this specific file — a visible flag instead of a
        # silently incomplete filename, since a per-file popup for a large
        # batch would be far more disruptive than useful.
        job = WORKFLOW.Job(Path("Scene.A1.wav"), Path("Scene.A2.wav"), 1, 2, 1, "READY", "rename", has_channel_suffix=True)

        self.assertEqual(
            WORKFLOW.display_status(
                job, "Trk#", "Trk#", rename_apply_formula=False, rename_change_number=False,
                rename_metadata_template_label="Metadata Template (missing: tape)",
            ),
            "Ready to Metadata Template (missing: tape)",
        )

    def test_browse_job_status_passes_through_unchanged(self) -> None:
        # Browse mode jobs never match any of the mode-specific branches
        # above (not rename/reembed/combine/merge) — the status set at
        # creation ("Ready to browse") must survive display_status()
        # untouched rather than falling into the generic "READY (<mode>)"
        # formatting meant for Split/Combine-style jobs.
        job = WORKFLOW.Job(Path("Scene.wav"), None, 0, None, None, "Ready to browse", "browse")

        self.assertEqual(WORKFLOW.display_status(job, "Trk#", "Trk#"), "Ready to browse")


class IxmlCoreCharacterizationTests(unittest.TestCase):
    def test_poly_slot_becomes_mono_without_changing_recorder_channel(self) -> None:
        source = "<BWFXML><TRACK_LIST><TRACK_COUNT>2</TRACK_COUNT><TRACK><CHANNEL_INDEX>1</CHANNEL_INDEX><INTERLEAVE_INDEX>1</INTERLEAVE_INDEX></TRACK><TRACK><CHANNEL_INDEX>5</CHANNEL_INDEX><INTERLEAVE_INDEX>2</INTERLEAVE_INDEX><NAME>Boom</NAME></TRACK></TRACK_LIST></BWFXML>"

        mono_text, recorder_channel = WORKFLOW.extract_mono_ixml(source, 2)
        root = WORKFLOW.parse_source_ixml(mono_text, Path("mono.wav"))
        tracks = root.findall(".//TRACK_LIST/TRACK")

        self.assertEqual(recorder_channel, 5)
        self.assertEqual(root.findtext(".//TRACK_LIST/TRACK_COUNT"), "1")
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].findtext("CHANNEL_INDEX"), "5")
        self.assertEqual(tracks[0].findtext("INTERLEAVE_INDEX"), "1")
        self.assertEqual(tracks[0].findtext("NAME"), "Boom")

    def test_lenient_falls_back_to_its_own_slot_number_when_channel_index_missing(self) -> None:
        # Split's own convention: no real Trk# for this slot (or no
        # TRACK_LIST/iXML at all) shouldn't block it — number it by file
        # order instead, exactly like the "Ch#" naming mode already does.
        source = "<BWFXML><TRACK_LIST><TRACK_COUNT>1</TRACK_COUNT><TRACK><INTERLEAVE_INDEX>1</INTERLEAVE_INDEX></TRACK></TRACK_LIST></BWFXML>"

        mono_text, trk_number = WORKFLOW.extract_mono_ixml(source, 1)

        self.assertEqual(trk_number, 1)
        root = WORKFLOW.parse_source_ixml(mono_text, Path("mono.wav"))
        self.assertEqual(root.findtext(".//TRACK_LIST/TRACK/CHANNEL_INDEX"), "1")

    def test_lenient_synthesizes_a_track_when_the_slot_does_not_exist_at_all(self) -> None:
        # No iXML/TRACK_LIST at all (empty <BWFXML/>) — still produces a
        # usable single-track document rather than raising.
        mono_text, trk_number = WORKFLOW.extract_mono_ixml("<BWFXML></BWFXML>", 3)

        self.assertEqual(trk_number, 3)
        root = WORKFLOW.parse_source_ixml(mono_text, Path("mono.wav"))
        self.assertEqual(root.findtext(".//TRACK_LIST/TRACK/CHANNEL_INDEX"), "3")

    def test_explicit_fallback_channel_overrides_the_track_index(self) -> None:
        # Re-embed's mono-source case: always reads track index 1 (a mono
        # file's only track), but the number to fall back to if that
        # track has no CHANNEL_INDEX is the job's own channel, which can
        # differ from 1.
        source = "<BWFXML><TRACK_LIST><TRACK_COUNT>1</TRACK_COUNT><TRACK><INTERLEAVE_INDEX>1</INTERLEAVE_INDEX></TRACK></TRACK_LIST></BWFXML>"

        _mono_text, trk_number = WORKFLOW.extract_mono_ixml(source, 1, fallback_channel=7)

        self.assertEqual(trk_number, 7)

    def test_strict_raises_when_the_slot_does_not_exist(self) -> None:
        with self.assertRaises(RuntimeError):
            WORKFLOW.extract_mono_ixml("<BWFXML><TRACK_LIST><TRACK_COUNT>0</TRACK_COUNT></TRACK_LIST></BWFXML>", 2, strict=True)

    def test_strict_raises_when_channel_index_is_missing(self) -> None:
        source = "<BWFXML><TRACK_LIST><TRACK_COUNT>1</TRACK_COUNT><TRACK><INTERLEAVE_INDEX>1</INTERLEAVE_INDEX></TRACK></TRACK_LIST></BWFXML>"
        with self.assertRaises(RuntimeError):
            WORKFLOW.extract_mono_ixml(source, 1, strict=True)

    def test_source_parser_sanitizes_control_characters_and_reports_context(self) -> None:
        root = WORKFLOW.parse_source_ixml("<BWFXML><PROJECT>TEST\x1a</PROJECT></BWFXML>", Path("source.wav"))
        self.assertEqual(root.findtext("PROJECT"), "TEST")

        with self.assertRaisesRegex(RuntimeError, r"iXML parse failed for source broken\.wav"):
            WORKFLOW.parse_source_ixml("<BWFXML><PROJECT>broken & value</PROJECT></BWFXML>", Path("broken.wav"))

    def test_ixml_track_rows_reads_the_standard_nested_shape(self) -> None:
        root = WORKFLOW.parse_source_ixml(
            "<BWFXML><TRACK_LIST><TRACK_COUNT>1</TRACK_COUNT><TRACK><CHANNEL_INDEX>5</CHANNEL_INDEX><INTERLEAVE_INDEX>1</INTERLEAVE_INDEX><NAME>FRIEND</NAME></TRACK></TRACK_LIST></BWFXML>",
            Path("nested.wav"),
        )

        self.assertEqual(WORKFLOW.ixml_track_rows(root), [("1", "5", "FRIEND")])

    def test_ixml_track_rows_falls_back_to_the_flattened_shape(self) -> None:
        # A DAW render/glue step can re-export iXML that bwfmetaedit can't
        # map back to a proper TRACK_LIST/TRACK nesting, flattening it to
        # TRACK_LIST_TRACK_CHANNEL_INDEX (with a _2, _3... suffix per extra
        # track) instead. split_recorder_channel() already falls back to
        # this shape to find a file's real channel; the Inspector's Track
        # List table needs the same fallback or it shows nothing for a file
        # the rest of the app can clearly read channel info from.
        root = WORKFLOW.parse_source_ixml(
            "<BWFXML>"
            "<TRACK_LIST_TRACK_CHANNEL_INDEX>5</TRACK_LIST_TRACK_CHANNEL_INDEX>"
            "<TRACK_LIST_TRACK_INTERLEAVE_INDEX>1</TRACK_LIST_TRACK_INTERLEAVE_INDEX>"
            "<TRACK_LIST_TRACK_NAME>FRIEND</TRACK_LIST_TRACK_NAME>"
            "<TRACK_LIST_TRACK_COUNT>1</TRACK_LIST_TRACK_COUNT>"
            "</BWFXML>",
            Path("flattened.wav"),
        )

        self.assertEqual(WORKFLOW.ixml_track_rows(root), [("1", "5", "FRIEND")])

    def test_ixml_track_rows_orders_multiple_flattened_tracks_by_suffix(self) -> None:
        root = WORKFLOW.parse_source_ixml(
            "<BWFXML>"
            "<TRACK_LIST_TRACK_CHANNEL_INDEX>1</TRACK_LIST_TRACK_CHANNEL_INDEX>"
            "<TRACK_LIST_TRACK_INTERLEAVE_INDEX>1</TRACK_LIST_TRACK_INTERLEAVE_INDEX>"
            "<TRACK_LIST_TRACK_NAME>FIRST</TRACK_LIST_TRACK_NAME>"
            "<TRACK_LIST_TRACK_CHANNEL_INDEX_2>5</TRACK_LIST_TRACK_CHANNEL_INDEX_2>"
            "<TRACK_LIST_TRACK_INTERLEAVE_INDEX_2>2</TRACK_LIST_TRACK_INTERLEAVE_INDEX_2>"
            "<TRACK_LIST_TRACK_NAME_2>SECOND</TRACK_LIST_TRACK_NAME_2>"
            "</BWFXML>",
            Path("multi-flattened.wav"),
        )

        self.assertEqual(WORKFLOW.ixml_track_rows(root), [("1", "1", "FIRST"), ("2", "5", "SECOND")])


class WavChunkCharacterizationTests(unittest.TestCase):
    def test_chunk_scanner_exposes_offsets_sizes_and_padding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "chunks.wav"
            fmt = struct.pack("<HHIIHH", 1, 1, 48000, 96000, 2, 16)
            write_riff(wav_path, [(b"fmt ", fmt), (b"JUNK", b"odd"), (b"data", b"\x01\x02")])

            header, chunks = WORKFLOW.scan_wav_chunks(wav_path)

            self.assertEqual(header[:4], b"RIFF")
            self.assertEqual([chunk.chunk_id for chunk in chunks], [b"fmt ", b"JUNK", b"data"])
            self.assertEqual(chunks[1].size, 3)
            self.assertEqual(chunks[1].raw_size, 12)
            self.assertEqual(WORKFLOW.read_wav_chunk(wav_path, chunks[1]), b"odd")
            self.assertEqual(WORKFLOW.read_wav_chunk(wav_path, chunks[1], raw=True), b"JUNK\x03\x00\x00\x00odd\x00")

    def test_copy_chunk_inserts_raw_bytes_and_preserves_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            target = root / "target.wav"
            fmt = struct.pack("<HHIIHH", 1, 1, 48000, 144000, 3, 24)
            bext = b"Description line 1\r\nDescription line 2\x00Coding History\r\n"
            audio = b"\x01\x02\x03\x04\x05\x06"
            write_riff(source, [(b"fmt ", fmt), (b"bext", bext), (b"data", audio)])
            write_riff(target, [(b"fmt ", fmt), (b"data", audio)])

            WORKFLOW.copy_wav_chunk_from_source(source, target, b"bext")

            self.assertEqual(raw_chunk(target, b"bext"), raw_chunk(source, b"bext"))
            self.assertEqual(raw_chunk(target, b"data"), raw_chunk(source, b"data"))
            self.assertEqual(struct.unpack_from("<I", target.read_bytes(), 4)[0], target.stat().st_size - 8)

    def test_copy_chunk_replaces_different_sized_chunk_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            target = root / "target.wav"
            fmt = struct.pack("<HHIIHH", 1, 1, 48000, 96000, 2, 16)
            write_riff(source, [(b"fmt ", fmt), (b"bext", b"source\r\nhistory"), (b"data", b"\x10\x20")])
            write_riff(target, [(b"fmt ", fmt), (b"bext", b"old"), (b"data", b"\x10\x20")])

            WORKFLOW.copy_wav_chunk_from_source(source, target, b"bext")

            self.assertEqual(raw_chunk(target, b"bext"), raw_chunk(source, b"bext"))
            self.assertEqual(raw_chunk(target, b"data"), raw_chunk(source, b"data"))

    def test_remove_chunk_removes_target_list_without_changing_bext_or_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "target.wav"
            fmt = struct.pack("<HHIIHH", 1, 1, 48000, 96000, 2, 16)
            bext = b"source bext\r\nhistory"
            audio = b"\x10\x20\x30\x40"
            write_riff(wav_path, [(b"fmt ", fmt), (b"bext", bext), (b"LIST", b"INFOINAM\x04\x00\x00\x00old\x00"), (b"data", audio)])
            original_bext = raw_chunk(wav_path, b"bext")
            original_audio = raw_chunk(wav_path, b"data")

            WORKFLOW.remove_wav_chunk(wav_path, b"LIST")

            self.assertIsNone(raw_chunk(wav_path, b"LIST"))
            self.assertEqual(raw_chunk(wav_path, b"bext"), original_bext)
            self.assertEqual(raw_chunk(wav_path, b"data"), original_audio)
            self.assertEqual(struct.unpack_from("<I", wav_path.read_bytes(), 4)[0], wav_path.stat().st_size - 8)

    def test_split_preservation_uses_only_declared_nonstructural_chunks(self) -> None:
        source = Path("source.wav")
        target = Path("target.wav")
        copied = []
        with mock.patch.object(WORKFLOW, "copy_wav_chunk_from_source", side_effect=lambda src, dst, chunk: copied.append((src, dst, chunk))):
            WORKFLOW.preserve_split_wav_chunks(source, target)

        self.assertEqual([chunk for _source, _target, chunk in copied], list(WORKFLOW.SPLIT_PRESERVED_WAV_CHUNKS))
        self.assertNotIn(b"fmt ", WORKFLOW.SPLIT_PRESERVED_WAV_CHUNKS)
        self.assertNotIn(b"data", WORKFLOW.SPLIT_PRESERVED_WAV_CHUNKS)
        self.assertNotIn(b"iXML", WORKFLOW.SPLIT_PRESERVED_WAV_CHUNKS)

    def test_extensible_channel_mask_set_and_clear_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "extensible.wav"
            fmt = (
                struct.pack("<HHIIHHH", 0xFFFE, 2, 48000, 288000, 6, 24, 22)
                + struct.pack("<HI", 24, 3)
                + struct.pack("<IHH8s", 1, 0, 0x0010, b"\x80\x00\x00\xAA\x00\x38\x9B\x71")
            )
            write_riff(wav_path, [(b"fmt ", fmt), (b"data", b"\x00" * 12)])

            self.assertEqual(WORKFLOW.wav_structure_info(wav_path)["channel_mask"], 3)
            WORKFLOW.set_wav_channel_mask(wav_path, 0x0000060F)
            self.assertEqual(WORKFLOW.wav_structure_info(wav_path)["channel_mask"], 0x0000060F)
            WORKFLOW.clear_wav_channel_mask(wav_path)
            self.assertEqual(WORKFLOW.wav_structure_info(wav_path)["channel_mask"], 0)


class WaveformPreviewCharacterizationTests(unittest.TestCase):
    def test_duration_matches_frames_over_sample_rate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "duration.wav"
            fmt = struct.pack("<HHIIHH", 1, 1, 48000, 96000, 2, 16)
            audio = struct.pack("<8192h", *([0] * 8192))
            write_riff(wav_path, [(b"fmt ", fmt), (b"data", audio)])

            self.assertAlmostEqual(WORKFLOW.wav_duration_seconds(wav_path), 8192 / 48000, places=6)

    def test_peaks_reflect_each_bucket_own_amplitude(self) -> None:
        # Four 2048-frame (4096-byte) segments, each a constant amplitude —
        # sized to exactly match compute_wav_peaks' default sample window
        # (4096 bytes) so each bucket's sampled window falls entirely
        # inside its own segment, not spilling into a neighbor.
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "peaks.wav"
            fmt = struct.pack("<HHIIHH", 1, 1, 48000, 96000, 2, 16)
            segments = [0, -32768, 1000, 32767]
            audio = b"".join(struct.pack("<2048h", *([value] * 2048)) for value in segments)
            write_riff(wav_path, [(b"fmt ", fmt), (b"data", audio)])

            peaks = WORKFLOW.compute_wav_peaks(wav_path, bucket_count=4, sample_window_bytes=4096)

            self.assertEqual(len(peaks), 4)
            self.assertAlmostEqual(peaks[0], 0.0, places=3)
            self.assertAlmostEqual(peaks[1], 1.0, places=3)
            self.assertAlmostEqual(peaks[2], 1000 / 32768, places=3)
            self.assertAlmostEqual(peaks[3], 32767 / 32768, places=3)

    def test_peaks_for_24bit_pcm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "peaks24.wav"
            fmt = struct.pack("<HHIIHH", 1, 1, 48000, 144000, 3, 24)
            # A single max-negative 24-bit frame (0x800000), little-endian.
            audio = b"\x00\x00\x80"
            write_riff(wav_path, [(b"fmt ", fmt), (b"data", audio)])

            peaks = WORKFLOW.compute_wav_peaks(wav_path, bucket_count=1, sample_window_bytes=4096)

            self.assertEqual(len(peaks), 1)
            self.assertAlmostEqual(peaks[0], 1.0, places=3)

    def test_peaks_for_float32(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "peaksf32.wav"
            fmt = struct.pack("<HHIIHH", 3, 1, 48000, 192000, 4, 32)
            audio = struct.pack("<4f", 0.1, -0.75, 0.5, -0.2)
            write_riff(wav_path, [(b"fmt ", fmt), (b"data", audio)])

            peaks = WORKFLOW.compute_wav_peaks(wav_path, bucket_count=1, sample_window_bytes=4096)

            self.assertEqual(len(peaks), 1)
            self.assertAlmostEqual(peaks[0], 0.75, places=3)

    def test_empty_data_chunk_returns_no_peaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "empty.wav"
            fmt = struct.pack("<HHIIHH", 1, 1, 48000, 96000, 2, 16)
            write_riff(wav_path, [(b"fmt ", fmt), (b"data", b"")])

            self.assertEqual(WORKFLOW.compute_wav_peaks(wav_path), [])
            self.assertEqual(WORKFLOW.wav_duration_seconds(wav_path), 0.0)

    def test_channel_peaks_keep_each_channel_separate_for_a_poly_source(self) -> None:
        # Two interleaved channels, four 1024-frame (4096-byte) segments —
        # sized to exactly match the default 4096-byte sample window, same
        # trick as test_peaks_reflect_each_bucket_own_amplitude, but with a
        # distinct (left, right) pair per segment so a bug that averaged or
        # otherwise mixed the channels together would show up immediately.
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "poly_peaks.wav"
            fmt = struct.pack("<HHIIHH", 1, 2, 48000, 192000, 4, 16)
            segments = [(0, 0), (-32768, 1000), (1000, -32768), (32767, 32767)]
            audio = b"".join(struct.pack("<2h", left, right) * 1024 for left, right in segments)
            write_riff(wav_path, [(b"fmt ", fmt), (b"data", audio)])

            channel_peaks = WORKFLOW.compute_wav_channel_peaks(wav_path, bucket_count=4, sample_window_bytes=4096)

            self.assertEqual(len(channel_peaks), 2)
            left_peaks, right_peaks = channel_peaks
            self.assertEqual(len(left_peaks), 4)
            self.assertEqual(len(right_peaks), 4)
            self.assertAlmostEqual(left_peaks[0], 0.0, places=3)
            self.assertAlmostEqual(left_peaks[1], 1.0, places=3)
            self.assertAlmostEqual(left_peaks[2], 1000 / 32768, places=3)
            self.assertAlmostEqual(left_peaks[3], 32767 / 32768, places=3)
            self.assertAlmostEqual(right_peaks[0], 0.0, places=3)
            self.assertAlmostEqual(right_peaks[1], 1000 / 32768, places=3)
            self.assertAlmostEqual(right_peaks[2], 1.0, places=3)
            self.assertAlmostEqual(right_peaks[3], 32767 / 32768, places=3)

    def test_channel_peaks_for_a_mono_file_is_one_lane_matching_the_combined_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "mono_peaks.wav"
            fmt = struct.pack("<HHIIHH", 1, 1, 48000, 96000, 2, 16)
            audio = struct.pack("<2h", -16384, 16384) * 1024
            write_riff(wav_path, [(b"fmt ", fmt), (b"data", audio)])

            channel_peaks = WORKFLOW.compute_wav_channel_peaks(wav_path, bucket_count=1, sample_window_bytes=4096)
            combined_peaks = WORKFLOW.compute_wav_peaks(wav_path, bucket_count=1, sample_window_bytes=4096)

            self.assertEqual(len(channel_peaks), 1)
            self.assertAlmostEqual(channel_peaks[0][0], combined_peaks[0], places=3)

    def test_channel_peaks_for_empty_data_chunk_returns_one_empty_lane_per_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "empty_poly.wav"
            fmt = struct.pack("<HHIIHH", 1, 4, 48000, 384000, 8, 16)
            write_riff(wav_path, [(b"fmt ", fmt), (b"data", b"")])

            self.assertEqual(WORKFLOW.compute_wav_channel_peaks(wav_path), [[], [], [], []])


class AudioContentComparisonCharacterizationTests(unittest.TestCase):
    def _audio_info(self, total_samples: int = 4) -> dict[str, str]:
        return {
            "sample_rate": "48000", "bit_depth": "16", "channels": "1",
            "total_samples": str(total_samples), "duration": "00:00:00.00", "encoding": "16-bit Signed Integer PCM",
        }

    def test_compare_wav_pcm_content_detects_a_silenced_sample_region(self) -> None:
        # Mirrors a real RX repair: format, sample rate, and frame count all
        # stay identical while a handful of samples in the middle get zeroed
        # out — soxi-derived properties alone can never catch this.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fmt = struct.pack("<HHIIHH", 1, 1, 48000, 96000, 2, 16)
            original = struct.pack("<4h", 100, 200, 300, 400)
            silenced = struct.pack("<4h", 100, 0, 0, 400)
            source = root / "source.wav"
            target = root / "target.wav"
            write_riff(source, [(b"fmt ", fmt), (b"data", original)])
            write_riff(target, [(b"fmt ", fmt), (b"data", silenced)])

            self.assertFalse(WORKFLOW.compare_wav_pcm_content(source, target))
            self.assertTrue(WORKFLOW.compare_wav_pcm_content(source, source))

    def test_compare_basic_audio_catches_content_difference_when_headers_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fmt = struct.pack("<HHIIHH", 1, 1, 48000, 96000, 2, 16)
            source = root / "source.wav"
            target = root / "target.wav"
            write_riff(source, [(b"fmt ", fmt), (b"data", struct.pack("<4h", 100, 200, 300, 400))])
            write_riff(target, [(b"fmt ", fmt), (b"data", struct.pack("<4h", 100, 0, 0, 400))])

            with mock.patch.object(WORKFLOW, "get_audio_info", return_value=self._audio_info()):
                comparison = WORKFLOW.compare_basic_audio("soxi", "bwf", source, target)

        self.assertEqual(comparison["result"], "DIFFERENT")
        content_row = next(row for row in comparison["audio"] if row["label"] == "Audio Content")
        self.assertEqual(content_row["result"], "≠")
        # The value cells carry their own meaning too (not just the color a
        # report applies from `result`) — useful in a colorless/printed copy.
        self.assertEqual((content_row["source"], content_row["target"]), ("Different", "Different"))

    def test_compare_basic_audio_skips_content_check_when_headers_already_differ(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            target = root / "target.wav"
            source.write_bytes(b"0")
            target.write_bytes(b"00")
            source_info = self._audio_info()
            target_info = {**self._audio_info(), "bit_depth": "24"}

            with mock.patch.object(WORKFLOW, "get_audio_info", side_effect=[source_info, target_info]), mock.patch.object(
                WORKFLOW, "compare_wav_pcm_content"
            ) as mock_compare_content:
                comparison = WORKFLOW.compare_basic_audio("soxi", "bwf", source, target)

        mock_compare_content.assert_not_called()
        self.assertEqual(comparison["result"], "DIFFERENT")
        content_row = next(row for row in comparison["audio"] if row["label"] == "Audio Content")
        self.assertEqual(content_row["result"], "—")

    def test_compare_poly_mono_content_extracts_the_matching_channel(self) -> None:
        poly = Path("poly.wav")
        mono = Path("mono.wav")
        with mock.patch.object(WORKFLOW, "split_recorder_channel", return_value=5), mock.patch.object(
            WORKFLOW, "poly_track_mapping", return_value=[(1, 1), (2, 3), (3, 5)]
        ), mock.patch.object(WORKFLOW, "compare_wav_pcm_stream", return_value=(1000, "PASS")) as mock_compare_stream:
            self.assertTrue(WORKFLOW.compare_poly_mono_content("bwf", poly, mono))

        mock_compare_stream.assert_called_once_with(poly, 3, mono)

    def test_compare_poly_mono_content_raises_when_no_channel_matches(self) -> None:
        with mock.patch.object(WORKFLOW, "split_recorder_channel", return_value=9), mock.patch.object(
            WORKFLOW, "poly_track_mapping", return_value=[(1, 1), (2, 3)]
        ):
            with self.assertRaisesRegex(RuntimeError, "no channel matching"):
                WORKFLOW.compare_poly_mono_content("bwf", Path("poly.wav"), Path("mono.wav"))

    def test_compare_basic_audio_compares_the_matching_channel_for_a_poly_vs_mono_pair(self) -> None:
        # A whole Poly recording checked File ↔ File against one already-
        # split channel of it (e.g. loaded directly via Load WAV rather than
        # through Poly ↔ Split's folder pickers) used to always read as
        # DIFFERENT — their PCM layouts are fundamentally different (multi-
        # channel interleaved vs. one channel's worth of bytes), independent
        # of whether the content actually matches.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            poly_path = root / "poly.wav"
            mono_path = root / "mono.wav"
            poly_path.touch()
            mono_path.touch()
            poly_info = {**self._audio_info(), "channels": "3"}
            mono_info = {**self._audio_info(), "channels": "1"}

            with mock.patch.object(WORKFLOW, "get_audio_info", side_effect=[poly_info, mono_info]), mock.patch.object(
                WORKFLOW, "compare_wav_pcm_content"
            ) as mock_compare_content, mock.patch.object(WORKFLOW, "compare_poly_mono_content", return_value=True) as mock_compare_poly_mono:
                comparison = WORKFLOW.compare_basic_audio("soxi", "bwf", poly_path, mono_path)

        mock_compare_content.assert_not_called()
        mock_compare_poly_mono.assert_called_once_with("bwf", poly_path, mono_path)
        self.assertEqual(comparison["result"], "PASS")
        content_row = next(row for row in comparison["audio"] if row["label"] == "Audio Content")
        self.assertEqual(content_row["result"], "✓")

    def test_compare_basic_audio_blocks_when_channels_differ_and_neither_side_is_mono(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            target = root / "target.wav"
            source.touch()
            target.touch()
            source_info = {**self._audio_info(), "channels": "3"}
            target_info = {**self._audio_info(), "channels": "5"}

            with mock.patch.object(WORKFLOW, "get_audio_info", side_effect=[source_info, target_info]):
                comparison = WORKFLOW.compare_basic_audio("soxi", "bwf", source, target)

        self.assertEqual(comparison["result"], "DIFFERENT")
        content_row = next(row for row in comparison["audio"] if row["label"] == "Audio Content")
        self.assertTrue(content_row["result"].startswith("BLOCKED"))


class ProcessingSummaryCharacterizationTests(unittest.TestCase):
    # A batch containing a real BLOCKED row (e.g. a truncated/corrupt
    # source file caught during Preview) used to report "0 failed" in the
    # completion summary, reading as fully successful at a glance — the
    # row and its real status were always written to the saved TSV, but
    # the one-line summary most people actually read hid it completely.
    def test_ok_and_failed_rows_are_counted_as_before(self) -> None:
        rows = [{"status": "OK"}, {"status": "OK"}, {"status": "FAIL: disk full"}, {"status": "CANCELLED"}]

        self.assertEqual(WORKFLOW.summarize_processing_rows(rows), (2, 2, 0))

    def test_blocked_rows_are_counted_as_other_not_silently_dropped(self) -> None:
        rows = [
            {"status": "OK"},
            {"status": "BLOCKED: invalid Wave: truncated (The RIFF chunk should be 230241546 bytes, but is 230180106 bytes.)"},
        ]

        self.assertEqual(WORKFLOW.summarize_processing_rows(rows), (1, 0, 1))

    def test_skip_and_merge_metadata_only_failures_also_land_in_other(self) -> None:
        rows = [
            {"status": "SKIP EXISTS"},
            {"status": "SKIPPED: conflict"},
            {"status": "MERGE SUCCEEDED; METADATA FAILED: no bwfmetaedit"},
        ]

        self.assertEqual(WORKFLOW.summarize_processing_rows(rows), (0, 0, 3))

    def test_counts_always_add_up_to_the_full_row_count(self) -> None:
        rows = [{"status": s} for s in ("OK", "FAIL: x", "BLOCKED: y", "CANCELLED", "SKIP EXISTS")]

        successes, failures, other = WORKFLOW.summarize_processing_rows(rows)

        self.assertEqual(successes + failures + other, len(rows))


class JobBuilderCharacterizationTests(unittest.TestCase):
    def setUp(self) -> None:
        # These tests drive build_split_jobs with empty placeholder files and
        # mocked soxi/BWF MetaEdit; the structural pre-check (which reads the
        # real bytes) is covered separately in StructureBlockCharacterizationTests.
        patcher = mock.patch.object(WORKFLOW, "structure_block_status", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_preview_cell_sort_keys_are_numeric_natural_and_missing_last(self) -> None:
        numeric = ["10", "-", "2", "1"]
        filenames = ["Scene.A10.wav", "-", "Scene.A2.wav", "Scene.A1.wav"]

        self.assertEqual(sorted(numeric, key=lambda value: WORKFLOW.preview_cell_sort_key("trk", value)), ["1", "2", "10", "-"])
        self.assertEqual(sorted(filenames, key=lambda value: WORKFLOW.preview_cell_sort_key("target", value)), ["Scene.A1.wav", "Scene.A2.wav", "Scene.A10.wav", "-"])

    def test_rename_jobs_without_selected_action_are_not_executable(self) -> None:
        job = WORKFLOW.Job(Path("source.wav"), Path("target.wav"), 1, 1, 1, "READY", "rename")

        jobs = WORKFLOW.mark_rename_jobs_no_change([job])

        self.assertEqual(jobs[0].status, "NO CHANGE")
        self.assertFalse(WORKFLOW.job_is_executable(jobs[0]))

    def test_operation_control_capabilities_cover_preview_and_execute_phases(self) -> None:
        self.assertEqual(WORKFLOW.operation_control_capabilities("preview"), WORKFLOW.OperationControlCapabilities(True, True, "item"))
        self.assertEqual(WORKFLOW.operation_control_capabilities("qc"), WORKFLOW.OperationControlCapabilities(True, True, "item"))
        self.assertEqual(WORKFLOW.operation_control_capabilities("execute", {"split"}), WORKFLOW.OperationControlCapabilities(True, True, "job"))
        self.assertEqual(WORKFLOW.operation_control_capabilities("execute", {"rename"}), WORKFLOW.OperationControlCapabilities(True, True, "transaction"))
        self.assertEqual(WORKFLOW.operation_control_capabilities("idle"), WORKFLOW.OperationControlCapabilities())

    def test_split_conflict_status_covers_all_three_policies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            existing = Path(temp_dir) / "existing.wav"
            existing.touch()
            missing = Path(temp_dir) / "missing.wav"

            self.assertEqual(WORKFLOW.split_conflict_status(missing, "Ask"), "READY")
            self.assertEqual(WORKFLOW.split_conflict_status(existing, "Replace existing"), "READY REPLACE")
            self.assertEqual(WORKFLOW.split_conflict_status(existing, "Skip existing"), "SKIP EXISTS")
            # "Ask" (replacing the old "Stop on conflict", which silently let
            # the rest of the batch run anyway — not what "Stop" implied)
            # marks the job as pending a decision rather than deciding for
            # the user, so it can be routed to the Execute-time conflict
            # dialog instead of being treated as a dead end.
            self.assertEqual(WORKFLOW.split_conflict_status(existing, "Ask"), "CONFLICT: target exists")

    def test_conflict_jobs_are_actionable_but_not_ready(self) -> None:
        conflict_job = WORKFLOW.Job(Path("source.wav"), Path("target.wav"), 1, 1, 1, "CONFLICT: target exists", "split")

        # Not "ready" (Preview's ready/blocked counts should still show it
        # as pending), but IS actionable — Execute must still hand it to the
        # worker loop so the user actually gets asked, instead of it being
        # silently excluded the way a permanently-blocked job is.
        self.assertFalse(WORKFLOW.job_is_ready(conflict_job))
        self.assertFalse(WORKFLOW.job_is_executable(conflict_job))
        self.assertTrue(WORKFLOW.job_needs_conflict_decision(conflict_job))
        self.assertTrue(WORKFLOW.job_is_actionable(conflict_job))

    def test_actionable_still_requires_a_real_target_and_source(self) -> None:
        no_target = WORKFLOW.Job(Path("source.wav"), None, 1, 1, 1, "CONFLICT: target exists", "split")

        self.assertFalse(WORKFLOW.job_is_actionable(no_target))

    def test_ready_and_blocked_jobs_are_actionable_and_not_respectively(self) -> None:
        ready_job = WORKFLOW.Job(Path("source.wav"), Path("target.wav"), 1, 1, 1, "READY", "split")
        blocked_job = WORKFLOW.Job(Path("source.wav"), None, 1, 1, 1, "BLOCKED: missing Trk#", "split")

        self.assertTrue(WORKFLOW.job_is_actionable(ready_job))
        self.assertFalse(WORKFLOW.job_is_actionable(blocked_job))

    def test_qc_control_cancels_at_cooperative_boundary(self) -> None:
        pause_requested = type("Pause", (), {"is_set": staticmethod(lambda: False)})()
        cancel_requested = type("Cancel", (), {"is_set": staticmethod(lambda: True)})()

        with self.assertRaises(WORKFLOW.QcCancelled):
            WORKFLOW.wait_for_qc_control(pause_requested, cancel_requested)

    def test_in_place_rename_cancel_rolls_back_all_staged_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sources = [root / "Scene.A1.wav", root / "Scene.A2.wav"]
            targets = [root / "Scene.A3.wav", root / "Scene.A4.wav"]
            for source in sources:
                source.write_bytes(source.name.encode())
            jobs = [WORKFLOW.Job(source, target, 1, index, index, "READY", "rename") for index, (source, target) in enumerate(zip(sources, targets), start=1)]
            pause_requested = type("Pause", (), {"is_set": staticmethod(lambda: False)})()

            class CancelAfterFirstStage:
                requested = False

                @classmethod
                def is_set(cls):
                    return cls.requested

            def progress(_index, _total, status, _elapsed, _eta, _job):
                if status == "STAGED":
                    CancelAfterFirstStage.requested = True

            rows = WORKFLOW.execute_rename_jobs_stepwise(jobs, progress, pause_requested, CancelAfterFirstStage())

            self.assertEqual([row["status"] for row in rows], ["CANCELLED", "CANCELLED"])
            self.assertTrue(all(source.exists() for source in sources))
            self.assertTrue(all(not target.exists() for target in targets))
            self.assertEqual(list(root.glob(".*.rename-*")), [])

    def test_preview_control_cancels_at_cooperative_boundary(self) -> None:
        pause_requested = type("Pause", (), {"is_set": staticmethod(lambda: False)})()
        cancel_requested = type("Cancel", (), {"is_set": staticmethod(lambda: True)})()

        with self.assertRaises(WORKFLOW.PreviewCancelled):
            WORKFLOW.wait_for_preview_control(pause_requested, cancel_requested)

    def test_inspector_contexts_define_payload_and_scroll_policy(self) -> None:
        job = WORKFLOW.Job(Path("source.wav"), Path("target.wav"), 1, 1, 1, "READY", "reembed")
        qc_row = {"poly_path": Path("poly.wav"), "split_path": Path("split.wav")}

        file_context = WORKFLOW.InspectorContext.for_file(Path("standalone.wav"))
        job_context = WORKFLOW.InspectorContext.for_job(job)
        qc_context = WORKFLOW.InspectorContext.for_qc(qc_row)
        initial_qc_context = WORKFLOW.InspectorContext.for_qc(qc_row, reset_scroll=True)

        self.assertEqual((file_context.kind, file_context.wav_path, file_context.reset_scroll), ("file", Path("standalone.wav"), True))
        self.assertEqual((job_context.kind, job_context.job, job_context.reset_scroll), ("job", job, False))
        self.assertEqual((qc_context.kind, qc_context.qc_row, qc_context.reset_scroll), ("qc", qc_row, False))
        self.assertTrue(initial_qc_context.reset_scroll)

    def test_inspector_render_model_defines_comparison_layout(self) -> None:
        model = WORKFLOW.InspectorRenderModel(
            [("FILES", "source list")],
            [("PLANNED FILE", "target")],
            left_header="SOURCES",
            right_header="PLANNED TARGET",
            path_label="Combine job: target.wav",
            blank_missing_sections=True,
            pair_first_sections=True,
        )

        self.assertEqual((model.left_header, model.right_header), ("SOURCES", "PLANNED TARGET"))
        self.assertEqual(model.path_label, "Combine job: target.wav")
        self.assertTrue(model.blank_missing_sections)
        self.assertTrue(model.pair_first_sections)

    def test_progress_event_timing_uses_overall_progress(self) -> None:
        event = WORKFLOW.ProgressEvent(
            "workflow_preview",
            2,
            4,
            "Scanning sources",
            Path("Scene.A2.wav"),
            started=10.0,
            overall_index=5,
            overall_total=10,
        )

        self.assertEqual(event.timing(now=15.0), (5.0, 5.0))

    def test_progress_event_supports_request_scoped_progress_without_item(self) -> None:
        event = WORKFLOW.ProgressEvent("qc_poly", 2, 5, "Comparing", started=20.0, request_id=7)

        self.assertIsNone(event.item)
        self.assertEqual(event.request_id, 7)
        self.assertEqual(event.timing(now=22.0), (2.0, 3.0))

    @staticmethod
    def _mono_ixml(channels: list[int]) -> str:
        tracks = "".join(
            f"<TRACK><CHANNEL_INDEX>{channel}</CHANNEL_INDEX><INTERLEAVE_INDEX>{index}</INTERLEAVE_INDEX></TRACK>"
            for index, channel in enumerate(channels, start=1)
        )
        return f"<BWFXML><TRACK_LIST><TRACK_COUNT>{len(channels)}</TRACK_COUNT>{tracks}</TRACK_LIST></BWFXML>"

    def test_split_jobs_preserve_slot_and_recorder_channel_numbering(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            output_root = root / "output"
            source_root.mkdir()
            source = source_root / "Scene.WAV"
            source.touch()
            with mock.patch.object(WORKFLOW, "soxi_channels", return_value=3), mock.patch.object(
                WORKFLOW, "export_ixml", return_value=self._mono_ixml([1, 5, 8])
            ):
                trk_jobs = WORKFLOW.build_split_jobs(source_root, output_root, "bwf", "soxi", "Trk#", "{base}.A$", "Skip existing")
                stream_jobs = WORKFLOW.build_split_jobs(source_root, output_root, "bwf", "soxi", "Stream#", "{base}.A$", "Skip existing")

            self.assertEqual([(job.channel, job.trk_number, job.stream_number) for job in trk_jobs], [(1, 1, 1), (2, 5, 2), (3, 8, 3)])
            self.assertEqual([job.target.name for job in trk_jobs], ["Scene.A1.WAV", "Scene.A5.WAV", "Scene.A8.WAV"])
            self.assertEqual([job.target.name for job in stream_jobs], ["Scene.A1.WAV", "Scene.A2.WAV", "Scene.A3.WAV"])

    def test_split_only_files_restricts_the_batch_to_the_given_paths(self) -> None:
        # Backs the drag-and-drop "import just this file" flow — dropping
        # one file onto the list shouldn't scan (or split) any other poly
        # source already sitting in the same folder.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            output_root = root / "output"
            source_root.mkdir()
            wanted = source_root / "Wanted.WAV"
            ignored = source_root / "Ignored.WAV"
            wanted.touch()
            ignored.touch()
            with mock.patch.object(WORKFLOW, "soxi_channels", return_value=1), mock.patch.object(
                WORKFLOW, "export_ixml", return_value=self._mono_ixml([1])
            ):
                jobs = WORKFLOW.build_split_jobs(
                    source_root, output_root, "bwf", "soxi", "Trk#", "{base}.A$", "Skip existing",
                    only_files=[wanted],
                )

            self.assertEqual([job.source for job in jobs], [wanted])

    def test_missing_trk_number_falls_back_to_file_order_instead_of_blocking(self) -> None:
        # A slot whose TRACK has no CHANNEL_INDEX used to block that whole
        # channel outright — now it's numbered by its own file-order
        # position instead, the same convention "Ch#" naming already uses.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            output_root = root / "output"
            source_root.mkdir()
            source = source_root / "Scene.WAV"
            source.touch()
            # Slot 2 has no CHANNEL_INDEX at all.
            ixml = (
                "<BWFXML><TRACK_LIST><TRACK_COUNT>3</TRACK_COUNT>"
                "<TRACK><CHANNEL_INDEX>1</CHANNEL_INDEX></TRACK>"
                "<TRACK></TRACK>"
                "<TRACK><CHANNEL_INDEX>8</CHANNEL_INDEX></TRACK>"
                "</TRACK_LIST></BWFXML>"
            )
            with mock.patch.object(WORKFLOW, "soxi_channels", return_value=3), mock.patch.object(
                WORKFLOW, "export_ixml", return_value=ixml
            ):
                jobs = WORKFLOW.build_split_jobs(source_root, output_root, "bwf", "soxi", "Trk#", "{base}.A$", "Skip existing")

            self.assertEqual([job.status for job in jobs], ["READY", "READY", "READY"])
            self.assertEqual([job.trk_number for job in jobs], [1, 2, 8])
            self.assertEqual([job.target.name for job in jobs], ["Scene.A1.WAV", "Scene.A2.WAV", "Scene.A8.WAV"])

    def test_no_ixml_at_all_falls_back_to_file_order_for_every_channel(self) -> None:
        # export_ixml() raising "iXML sidecar was not exported" (the
        # source genuinely has none) used to block the entire file —
        # every channel now just gets numbered by file order instead.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            output_root = root / "output"
            source_root.mkdir()
            source = source_root / "Scene.WAV"
            source.touch()
            with mock.patch.object(WORKFLOW, "soxi_channels", return_value=2), mock.patch.object(
                WORKFLOW, "export_ixml", side_effect=RuntimeError("iXML sidecar was not exported")
            ):
                jobs = WORKFLOW.build_split_jobs(source_root, output_root, "bwf", "soxi", "Trk#", "{base}.A$", "Skip existing")

            self.assertEqual([job.status for job in jobs], ["READY", "READY"])
            self.assertEqual([job.trk_number for job in jobs], [1, 2])
            self.assertEqual([job.target.name for job in jobs], ["Scene.A1.WAV", "Scene.A2.WAV"])

    def test_a_real_error_still_blocks_the_whole_file(self) -> None:
        # Only the two specific "no iXML" messages get the lenient
        # fallback — any other error (a corrupt/truncated WAV, a tool
        # crash, ...) still blocks, matching the previous behavior; WAV
        # repair itself is deliberately out of scope for this fallback.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            output_root = root / "output"
            source_root.mkdir()
            source = source_root / "Scene.WAV"
            source.touch()
            with mock.patch.object(WORKFLOW, "soxi_channels", return_value=2), mock.patch.object(
                WORKFLOW, "export_ixml", side_effect=RuntimeError("invalid Wave: truncated")
            ):
                jobs = WORKFLOW.build_split_jobs(source_root, output_root, "bwf", "soxi", "Trk#", "{base}.A$", "Skip existing")

            self.assertEqual(len(jobs), 1)
            self.assertIn("BLOCKED: invalid Wave: truncated", jobs[0].status)

    def test_mono_source_with_copy_toggle_off_still_splits_normally(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            output_root = root / "output"
            source_root.mkdir()
            source = source_root / "Lav.WAV"
            source.touch()
            with mock.patch.object(WORKFLOW, "soxi_channels", return_value=1), mock.patch.object(
                WORKFLOW, "export_ixml", side_effect=RuntimeError("iXML sidecar was not exported")
            ):
                jobs = WORKFLOW.build_split_jobs(source_root, output_root, "bwf", "soxi", "Trk#", "{base}.A$", "Skip existing", copy_mono=False)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].mode, "split")
            self.assertEqual(jobs[0].status, "READY")
            self.assertEqual(jobs[0].trk_number, 1)

    def test_mono_source_with_copy_toggle_on_becomes_a_straight_copy_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            output_root = root / "output"
            source_root.mkdir()
            source = source_root / "Lav.WAV"
            source.touch()
            with mock.patch.object(WORKFLOW, "soxi_channels", return_value=1):
                # export_ixml() is deliberately not mocked/reachable here —
                # the copy path must never need to read metadata at all.
                jobs = WORKFLOW.build_split_jobs(source_root, output_root, "bwf", "soxi", "Trk#", "{base}.A$", "Skip existing", copy_mono=True)

            self.assertEqual(len(jobs), 1)
            job = jobs[0]
            self.assertEqual(job.mode, "split_copy")
            self.assertEqual(job.status, "READY COPY")
            # Original filename preserved — not renamed via the Output
            # name formula the way a real split channel would be.
            self.assertEqual(job.target.name, "Lav.WAV")

    def test_poly_source_is_unaffected_by_the_copy_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            output_root = root / "output"
            source_root.mkdir()
            source = source_root / "Scene.WAV"
            source.touch()
            with mock.patch.object(WORKFLOW, "soxi_channels", return_value=2), mock.patch.object(
                WORKFLOW, "export_ixml", return_value=self._mono_ixml([1, 2])
            ):
                jobs = WORKFLOW.build_split_jobs(source_root, output_root, "bwf", "soxi", "Trk#", "{base}.A$", "Skip existing", copy_mono=True)

            self.assertEqual([job.mode for job in jobs], ["split", "split"])

    def test_reembed_matching_preserves_recorder_and_stream_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            sources = [source_root / name for name in ("Scene.A1.wav", "Scene.A5.wav", "Scene.A8.wav")]
            targets = [target_root / name for name in ("Scene.A1 render 1.wav", "Scene.A2 render 1.wav", "Scene.A3 render 1.wav")]
            for path in sources + targets:
                path.touch()
            recorder_channels = {
                sources[0]: 1,
                sources[1]: 5,
                sources[2]: 8,
                targets[0]: 1,
                targets[1]: 5,
                targets[2]: 8,
            }
            with mock.patch.object(WORKFLOW, "split_recorder_channel", side_effect=lambda _tool, path: recorder_channels[path]):
                jobs = WORKFLOW.build_reembed_jobs(
                    source_root,
                    target_root,
                    rename_formula="{base}_fixed.A$",
                    rename_number_mode="Stream#",
                    bwfmetaedit="bwf",
                )

            self.assertEqual([(job.trk_number, job.stream_number, job.status) for job in jobs], [(1, 1, "READY"), (5, 2, "READY"), (8, 3, "READY")])
            self.assertEqual([job.target.name for job in jobs], ["Scene.A1 render 1.wav", "Scene.A2 render 1.wav", "Scene.A3 render 1.wav"])
            self.assertEqual([job.final_target.name for job in jobs], ["Scene_fixed.A1.wav", "Scene_fixed.A2.wav", "Scene_fixed.A3.wav"])

    def test_reembed_stream_numbering_skips_gaps_left_by_unmatched_channels(self) -> None:
        # A real case: channels 1, 2 and 4 all got rendered, but channel 3's
        # render is missing (NO TARGET) — Trk# should still say 1/2/4 (the
        # real recorder channel), but Stream# is meant to mean "1st, 2nd,
        # 3rd... of whatever actually made it through", so it should read
        # 1/2/3, not 1/2/4 with a gap where the missing channel would be.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            sources = [source_root / name for name in ("Scene.A1.wav", "Scene.A2.wav", "Scene.A3.wav", "Scene.A4.wav")]
            targets = [target_root / name for name in ("Scene.A1 render 1.wav", "Scene.A2 render 1.wav", "Scene.A4 render 1.wav")]
            for path in sources + targets:
                path.touch()
            recorder_channels = {
                sources[0]: 1, sources[1]: 2, sources[2]: 3, sources[3]: 4,
                targets[0]: 1, targets[1]: 2, targets[2]: 4,
            }
            with mock.patch.object(WORKFLOW, "split_recorder_channel", side_effect=lambda _tool, path: recorder_channels[path]):
                jobs = WORKFLOW.build_reembed_jobs(
                    source_root,
                    target_root,
                    rename_formula="{base}_fixed.A$",
                    rename_number_mode="Stream#",
                    bwfmetaedit="bwf",
                )

            ready = [job for job in jobs if job.status == "READY"]
            self.assertEqual([(job.trk_number, job.stream_number) for job in ready], [(1, 1), (2, 2), (4, 3)])
            self.assertEqual([job.final_target.name for job in ready], ["Scene_fixed.A1.wav", "Scene_fixed.A2.wav", "Scene_fixed.A3.wav"])
            blocked = next(job for job in jobs if job.status == "NO TARGET")
            self.assertEqual(blocked.trk_number, 3)

    def test_reembed_matches_poly_source_channels_via_track_mapping(self) -> None:
        # A take that was never split into mono files at all — nothing in
        # Source matches the {base}.A$ filename pattern, so this only works
        # if Re-embed falls back to reading the Poly file's own embedded
        # Track List (the same thing QC's Poly comparison already reads),
        # matching each stream to a Target by real recorder channel instead
        # of by filename.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            poly_source = source_root / "Scene.wav"
            poly_source.touch()
            targets = [target_root / name for name in ("Scene.A2 render 1.wav", "Scene.A3 render 1.wav", "Scene.A5 render 1.wav")]
            for path in targets:
                path.touch()
            recorder_channels = {targets[0]: 2, targets[1]: 3, targets[2]: 5}
            mapping = [(1, 2), (2, 3), (3, 5)]  # (stream_index, recorder_channel)
            with mock.patch.object(WORKFLOW, "read_wav_pcm_layout", return_value={"channels": 3}), mock.patch.object(
                WORKFLOW, "poly_track_mapping", return_value=mapping
            ), mock.patch.object(WORKFLOW, "split_recorder_channel", side_effect=lambda _tool, path: recorder_channels[path]):
                jobs = WORKFLOW.build_reembed_jobs(
                    source_root,
                    target_root,
                    rename_formula="{base}_fixed.A$",
                    rename_number_mode="Stream#",
                    bwfmetaedit="bwf",
                )

            ready = [job for job in jobs if job.status == "READY"]
            self.assertEqual(
                [(job.source, job.channel, job.trk_number, job.stream_number) for job in ready],
                [(poly_source, 1, 2, 1), (poly_source, 2, 3, 2), (poly_source, 3, 5, 3)],
            )
            self.assertEqual([job.final_target.name for job in ready], ["Scene_fixed.A1.wav", "Scene_fixed.A2.wav", "Scene_fixed.A3.wav"])

    def test_reembed_poly_source_missing_track_list_is_blocked_not_silently_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            poly_source = source_root / "Scene.wav"
            poly_source.touch()
            with mock.patch.object(WORKFLOW, "read_wav_pcm_layout", return_value={"channels": 3}), mock.patch.object(
                WORKFLOW, "poly_track_mapping", side_effect=RuntimeError("Poly Track List unavailable")
            ):
                jobs = WORKFLOW.build_reembed_jobs(source_root, target_root, bwfmetaedit="bwf")

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].source, poly_source)
            self.assertTrue(jobs[0].status.startswith("BLOCKED: cannot read Poly Track List"))

    def _reembed(self, source_names, target_names, channels, **kwargs):
        """Build Re-embed jobs for empty placeholder files; `channels` maps a
        file name to its recorder channel."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            for name in source_names:
                (source_root / name).touch()
            for name in target_names:
                (target_root / name).touch()
            with mock.patch.object(WORKFLOW, "split_recorder_channel", side_effect=lambda _tool, path: channels[path.name]):
                jobs = WORKFLOW.build_reembed_jobs(source_root, target_root, bwfmetaedit="bwf", **kwargs)
            return [
                (job.source.name if job.source else None, job.target.name if job.target else None, job.status,
                 job.final_target.name if job.final_target else None)
                for job in jobs
            ]

    def test_reembed_pairs_a_mono_take_without_channel_suffix(self) -> None:
        # Real case (2026-11-21): mono takes in iso_split are "12-10T2.WAV"
        # (no ".A3"), their AAP renders "12-10T2 render 001.wav" — the old
        # "{base}.A$ render \d+" formula skipped all 21 of them silently.
        rows = self._reembed(
            ["12-10T2.wav", "12-5T2.A3.wav", "12-5T2.A4.wav"],
            ["12-10T2 render 001.wav", "12-5T2.A3 render 001.wav", "12-5T2.A4 render 001.wav"],
            {"12-10T2.wav": 3, "12-10T2 render 001.wav": 3, "12-5T2.A3.wav": 3, "12-5T2.A3 render 001.wav": 3,
             "12-5T2.A4.wav": 4, "12-5T2.A4 render 001.wav": 4},
            rename_formula="{base}_AAP.A$", rename_number_mode="Stream#",
        )
        self.assertEqual(rows, [
            ("12-5T2.A3.wav", "12-5T2.A3 render 001.wav", "READY", "12-5T2_AAP.A1.wav"),
            ("12-5T2.A4.wav", "12-5T2.A4 render 001.wav", "READY", "12-5T2_AAP.A2.wav"),
            # A mono original has no channel number, so its new name has none either
            ("12-10T2.wav", "12-10T2 render 001.wav", "READY", "12-10T2_AAP.wav"),
        ])

    def test_reembed_name_prefix_stops_at_a_separator(self) -> None:
        # "12-1T10 render 001" must never fall into take "12-1T1".
        rows = self._reembed(
            ["12-1T1.wav", "12-1T10.wav"],
            ["12-1T10 render 001.wav", "12-1T1 render 001.wav"],
            {"12-1T1.wav": 3, "12-1T10.wav": 3, "12-1T1 render 001.wav": 3, "12-1T10 render 001.wav": 3},
        )
        self.assertEqual(sorted(row[:3] for row in rows), [
            ("12-1T1.wav", "12-1T1 render 001.wav", "READY"),
            ("12-1T10.wav", "12-1T10 render 001.wav", "READY"),
        ])

    def test_reembed_digits_only_suffix_is_a_channel_only_when_it_matches_the_recorder_channel(self) -> None:
        # "SC12-3": take 3, recorded on channel 1 -> {base} keeps "-3".
        # "Mix_2": channel 2 -> {base} is "Mix".
        rows = self._reembed(
            ["SC12-3.wav", "Mix_2.wav"],
            ["SC12-3 render 001.wav", "Mix_2-AtAlgnP_01.wav"],
            {"SC12-3.wav": 1, "SC12-3 render 001.wav": 1, "Mix_2.wav": 2, "Mix_2-AtAlgnP_01.wav": 2},
            rename_formula="{base}_AAP.A$", rename_number_mode="Trk#",
        )
        self.assertEqual(sorted(rows), [
            ("Mix_2.wav", "Mix_2-AtAlgnP_01.wav", "READY", "Mix_AAP.A2.wav"),
            ("SC12-3.wav", "SC12-3 render 001.wav", "READY", "SC12-3_AAP.wav"),
        ])

    def test_reembed_ignores_outputs_of_an_earlier_run_in_target(self) -> None:
        # "Scene_AAP.A1.wav" is what the rename formula makes: an output left
        # by an earlier run, not a Target to embed into (and not NO SOURCE).
        rows = self._reembed(
            ["Scene.A1.wav"],
            ["Scene.A1 render 1.wav", "Scene_AAP.A1.wav"],
            {"Scene.A1.wav": 1, "Scene.A1 render 1.wav": 1, "Scene_AAP.A1.wav": 1},
            rename_formula="{base}_AAP.A$", rename_number_mode="Stream#", conflict_policy="Skip existing",
        )
        self.assertEqual(rows, [("Scene.A1.wav", "Scene.A1 render 1.wav", "SKIP EXISTS", "Scene_AAP.A1.wav")])

    def test_formula_without_number_drops_the_channel_part(self) -> None:
        from operations.filename_formula import formula_without_number
        self.assertEqual(formula_without_number("{base}_AAP.A$"), "{base}_AAP")
        self.assertEqual(formula_without_number("{base}.A$"), "{base}")
        self.assertEqual(formula_without_number("{base}_A$"), "{base}")
        self.assertEqual(formula_without_number("{base}_Ch$_x"), "{base}_x")
        self.assertEqual(formula_without_number("{base}_trk$"), "{base}")

    def test_reembed_ignores_an_earlier_unnumbered_mono_output(self) -> None:
        # Second run: "15_WILDT01_AAP.wav" (made by the first run) is an output,
        # not a second Target for the mono take.
        rows = self._reembed(
            ["15_WILDT01.wav"],
            ["15_WILDT01 render 001.wav", "15_WILDT01_AAP.wav"],
            {"15_WILDT01.wav": 3, "15_WILDT01 render 001.wav": 3, "15_WILDT01_AAP.wav": 3},
            rename_formula="{base}_AAP.A$", conflict_policy="Skip existing",
        )
        self.assertEqual(rows, [("15_WILDT01.wav", "15_WILDT01 render 001.wav", "SKIP EXISTS", "15_WILDT01_AAP.wav")])

    def test_reembed_never_guesses_when_two_sources_reach_one_target(self) -> None:
        # "Scene render 1" starts with "Scene", a name both "Scene_3" and
        # "Scene_4" may stand for (the "_3"/"_4" could be a channel), and
        # all three are on channel 1 -> both claim it -> AMBIGUOUS SOURCE.
        rows = self._reembed(
            ["Scene_3.wav", "Scene_4.wav"],
            ["Scene render 1.wav"],
            {"Scene_3.wav": 1, "Scene_4.wav": 1, "Scene render 1.wav": 1},
        )
        self.assertEqual(sorted(rows), [
            ("Scene_3.wav", None, "AMBIGUOUS SOURCE", None),
            ("Scene_4.wav", None, "AMBIGUOUS SOURCE", None),
        ])

    def test_reembed_mono_file_without_any_target_is_listed_as_no_target(self) -> None:
        # No Source formula any more: every WAV in Source is read, so a mono
        # file with no Target is listed (NO TARGET) instead of silently left
        # out — 21 mono takes went missing that way (2026-11-21). It is still
        # never sent down the Poly Track List path, and its metadata isn't
        # read when nothing in Target could belong to it.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            (source_root / "Unrelated.wav").touch()
            with mock.patch.object(WORKFLOW, "read_wav_pcm_layout", return_value={"channels": 1}), mock.patch.object(
                WORKFLOW, "poly_track_mapping", side_effect=AssertionError("must not be called for a mono file")
            ), mock.patch.object(
                WORKFLOW, "split_recorder_channel", side_effect=AssertionError("no metadata read without a Target")
            ):
                jobs = WORKFLOW.build_reembed_jobs(source_root, target_root, bwfmetaedit="bwf")

            self.assertEqual([(job.source.name, job.target, job.status) for job in jobs], [("Unrelated.wav", None, "NO TARGET")])

    def test_reembed_includes_target_only_recorder_channel_as_no_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            source = source_root / "Scene.A5.wav"
            matched_target = target_root / "Scene_AAP.A3.wav"
            target_only = target_root / "Scene_AAP.A1.wav"
            for path in (source, matched_target, target_only):
                path.touch()
            recorder_channels = {source: 5, matched_target: 5, target_only: 1}
            with mock.patch.object(WORKFLOW, "split_recorder_channel", side_effect=lambda _tool, path: recorder_channels[path]):
                jobs = WORKFLOW.build_reembed_jobs(source_root, target_root, bwfmetaedit="bwf")

            self.assertEqual([(job.source, job.target, job.trk_number, job.status) for job in jobs], [
                (None, target_only, 1, "NO SOURCE"),
                (source, matched_target, 5, "READY"),
            ])
            self.assertFalse(WORKFLOW.job_is_executable(jobs[0]))
            self.assertTrue(WORKFLOW.job_is_executable(jobs[1]))

    def test_reembed_sorts_target_rows_before_source_only_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            source_a = source_root / "Alpha.A5.wav"
            source_b = source_root / "Bravo.A2.wav"
            target_a = target_root / "Alpha_AAP.A1.wav"
            target_b = target_root / "Bravo_AAP.A2.wav"
            for path in (source_a, source_b, target_a, target_b):
                path.touch()
            recorder_channels = {source_a: 5, source_b: 2, target_a: 1, target_b: 2}
            with mock.patch.object(WORKFLOW, "split_recorder_channel", side_effect=lambda _tool, path: recorder_channels[path]):
                jobs = WORKFLOW.build_reembed_jobs(source_root, target_root, bwfmetaedit="bwf")

            self.assertEqual([(job.source, job.target, job.status) for job in jobs], [
                (None, target_a, "NO SOURCE"),
                (source_b, target_b, "READY"),
                (source_a, None, "NO TARGET"),
            ])

    def test_reembed_falls_back_to_metadata_identity_when_base_name_is_rewritten(self) -> None:
        # Mirrors a real Pro Tools consolidate: it rewrites the take-number
        # digits inside the base name itself ("66-1-1" -> "66-1-01"), so the
        # exact-string {base} match can never pair these — Tape/Scene/Take/
        # OriginationDate/OriginationTime (confirmed to survive that render
        # untouched) plus the real Recorder Channel are the only usable key.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            source = source_root / "66-1-1.A1.wav"
            target = target_root / "66-1-01.A1-AtAlgnP_01.wav"
            source.touch()
            target.touch()
            recorder_channels = {source: 1, target: 1}
            identity = ("25Y07M09", "66-1", "1", "2025-07-09", "20:33:48")
            with mock.patch.object(WORKFLOW, "split_recorder_channel", side_effect=lambda _tool, path: recorder_channels[path]), mock.patch.object(
                WORKFLOW, "read_reembed_identity", return_value=identity
            ):
                jobs = WORKFLOW.build_reembed_jobs(
                    source_root, target_root, rename_formula="{base}_AAP.A$", bwfmetaedit="bwf",
                )

            self.assertEqual([(job.source, job.target, job.status) for job in jobs], [(source, target, "READY")])
            self.assertEqual(jobs[0].final_target.name, "66-1-1_AAP.A1.wav")

    def test_reembed_reports_progress_during_the_last_resort_identity_pairing(self) -> None:
        # The metadata-identity pairing above reads two bwfmetaedit-backed
        # fields per orphan Target and per NO TARGET Source with nothing
        # marking the wait — every earlier phase (scanning, Trk#, the main
        # classification loop) reports progress, so this pass used to make
        # a batch heavy on unmatched channels look finished (100%) while it
        # silently kept running.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            source = source_root / "66-1-1.A1.wav"
            target = target_root / "66-1-01.A1-AtAlgnP_01.wav"
            source.touch()
            target.touch()
            recorder_channels = {source: 1, target: 1}
            identity = ("25Y07M09", "66-1", "1", "2025-07-09", "20:33:48")
            progress = []

            def report(index: int, total: int, item: Path, _trk: int, status: str) -> None:
                progress.append((index, total, item, status))

            with mock.patch.object(WORKFLOW, "split_recorder_channel", side_effect=lambda _tool, path: recorder_channels[path]), mock.patch.object(
                WORKFLOW, "read_reembed_identity", return_value=identity
            ):
                jobs = WORKFLOW.build_reembed_jobs(
                    source_root, target_root,
                    rename_formula="{base}_AAP.A$", bwfmetaedit="bwf", progress=report,
                )

            self.assertEqual(jobs[0].status, "READY")
            orphan_progress = [entry for entry in progress if entry[3] == "Resolving orphan targets"]
            self.assertEqual(orphan_progress, [(1, 2, target, "Resolving orphan targets"), (2, 2, source, "Resolving orphan targets")])

    def test_reembed_reports_progress_while_reading_target_metadata(self) -> None:
        # Reading every Target candidate's recorder channel used to have no
        # progress reporting at all — for a Target root spanning many
        # dates/folders (the normal case once Poly matching no longer
        # requires Source and Target to be the same size), this was the
        # largest silent gap of all: it reads every Target across every
        # base, not just whichever one a previous phase was looking at.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            sources = [source_root / name for name in ("Scene.A1.wav", "Scene.A2.wav")]
            targets = [target_root / name for name in ("Scene.A1 render 1.wav", "Scene.A2 render 1.wav")]
            for path in sources + targets:
                path.touch()
            recorder_channels = {sources[0]: 1, sources[1]: 2, targets[0]: 1, targets[1]: 2}
            progress = []

            def report(index: int, total: int, item: Path, _trk: int, status: str) -> None:
                progress.append((index, total, item, status))

            with mock.patch.object(WORKFLOW, "split_recorder_channel", side_effect=lambda _tool, path: recorder_channels[path]):
                jobs = WORKFLOW.build_reembed_jobs(
                    source_root, target_root, bwfmetaedit="bwf", progress=report,
                )

            self.assertEqual([job.status for job in jobs], ["READY", "READY"])
            target_metadata_progress = [entry for entry in progress if entry[3] == "Reading target metadata"]
            self.assertEqual(len(target_metadata_progress), 2)
            self.assertEqual({entry[0] for entry in target_metadata_progress}, {1, 2})

    def test_reembed_reports_ambiguous_when_metadata_identity_matches_several_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            source = source_root / "66-1-1.A1.wav"
            target_one = target_root / "66-1-01.A1-AtAlgnP_01.wav"
            target_two = target_root / "77-2-01.A1-AtAlgnP_01.wav"
            for path in (source, target_one, target_two):
                path.touch()
            recorder_channels = {source: 1, target_one: 1, target_two: 1}
            identity = ("25Y07M09", "66-1", "1", "2025-07-09", "20:33:48")
            with mock.patch.object(WORKFLOW, "split_recorder_channel", side_effect=lambda _tool, path: recorder_channels[path]), mock.patch.object(
                WORKFLOW, "read_reembed_identity", return_value=identity
            ):
                jobs = WORKFLOW.build_reembed_jobs(source_root, target_root, bwfmetaedit="bwf")

            source_job = next(job for job in jobs if job.source == source)
            self.assertEqual(source_job.status, "AMBIGUOUS TARGET (metadata match)")
            self.assertIsNone(source_job.target)
            self.assertFalse(WORKFLOW.job_is_executable(source_job))

    def test_reembed_blocks_target_without_recorder_channel_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            source = source_root / "Scene.A5.wav"
            target = target_root / "Scene_AAP.A3.wav"
            source.touch()
            target.touch()

            def recorder_channel(_tool, path):
                if path == source:
                    return 5
                raise RuntimeError("Recorder Ch metadata unavailable")

            with mock.patch.object(WORKFLOW, "split_recorder_channel", side_effect=recorder_channel):
                jobs = WORKFLOW.build_reembed_jobs(source_root, target_root, bwfmetaedit="bwf")

            self.assertEqual([(job.trk_number, job.status) for job in jobs], [(5, "BLOCKED: cannot read target Trk# (Recorder Ch metadata unavailable)")])

    def test_rename_jobs_detect_collisions_after_stream_renumbering(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "Scene.A1.wav"
            second = root / "Scene.A2.wav"
            first.touch()
            second.touch()
            with mock.patch.object(WORKFLOW, "read_mono_trk_number", return_value=5):
                jobs = WORKFLOW.build_rename_jobs(root, "{base}.A$", "{base}.A$", "Trk#", "bwf")

            self.assertEqual([job.trk_number for job in jobs], [5, 5])
            self.assertEqual([job.target.name for job in jobs], ["Scene.A5.wav", "Scene.A5.wav"])
            self.assertEqual([job.status for job in jobs], ["COLLISION", "COLLISION"])

    def test_rename_jobs_apply_serial_id_and_find_replace_on_initial_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "BJ9496.A2.wav"
            source.touch()
            serial_ids: dict[str, str] = {}
            with mock.patch.object(WORKFLOW, "read_mono_trk_number", return_value=2):
                jobs = WORKFLOW.build_rename_jobs(
                    root,
                    "{base}.A$",
                    "{base}.A$",
                    "Trk#",
                    "bwf",
                    output_root=root / "out",
                    rename_rules=[RenameRule("BJ", "AB")],
                    serial_id_map=serial_ids,
                    serial_id_enabled=True,
                    serial_prefix="FB",
                    serial_digits=4,
                    serial_start=1,
                )

            self.assertEqual(jobs[0].target.name, "FB0001_AB9496.A2.wav")
            self.assertEqual(serial_ids, {f"{str(root).casefold()}::bj9496": "FB0001"})

    def test_serial_id_group_keys_are_scoped_by_source_path(self) -> None:
        first_root = Path("/source/A")
        second_root = Path("/source/B")

        self.assertNotEqual(
            f"{str(first_root).casefold()}::scene",
            f"{str(second_root).casefold()}::scene",
        )

    def test_rename_jobs_assign_ids_in_strict_metadata_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            early = root / "Z-late-name.wav"
            late = root / "A-early-name.wav"
            early.touch()
            late.touch()
            metadata = {early: ("Tape-1", 100, "Scene-1", "01", "100"), late: ("Tape-1", 200, "Scene-2", "02", "200")}
            jobs = WORKFLOW.build_rename_jobs(
                root,
                "{base}.A$",
                "{base}.A$",
                "Trk#",
                "bwf",
                output_root=root / "out",
                apply_formula=False,
                serial_id_map={},
                serial_id_enabled=True,
                serial_prefix="ABC",
                serial_digits=4,
                serial_start=1,
                serial_order_mode="Metadata Tape + TC",
                serial_metadata_reader=lambda path: metadata[path],
            )

        scenes = {job.source.name: job.serial_scene for job in jobs}
        takes = {job.source.name: job.serial_take for job in jobs}
        self.assertEqual(scenes, {"Z-late-name.wav": "Scene-1", "A-early-name.wav": "Scene-2"})
        self.assertEqual(takes, {"Z-late-name.wav": "01", "A-early-name.wav": "02"})
        targets = {job.source.name: job.target.name for job in jobs}
        self.assertEqual(targets["Z-late-name.wav"], "ABC0001_Z-late-name.wav")
        self.assertEqual(targets["A-early-name.wav"], "ABC0002_A-early-name.wav")

    def test_rename_jobs_report_progress_while_reading_tape_tc_metadata(self) -> None:
        # This confirmation dialog for this exact feature already warns it
        # "may take a while for large folders" — but the read loop behind
        # it had no progress reporting at all until this fix.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "Scene.A1.wav"
            source.touch()
            progress = []

            def report(index: int, total: int, item: Path, _trk: int, status: str) -> None:
                progress.append((index, total, item, status))

            WORKFLOW.build_rename_jobs(
                root, "{base}.A$", "{base}.A$", "Trk#", "bwf",
                output_root=root / "out",
                serial_id_map={},
                serial_id_enabled=True,
                serial_order_mode="Metadata Tape + TC",
                serial_metadata_reader=lambda _path: ("Tape-1", 100, "Scene-1", "01", "100"),
                progress=report,
            )

            self.assertIn((1, 1, source, "Reading Tape + TC metadata"), progress)

    def test_metadata_template_defines_the_whole_stem_with_no_automatic_channel_suffix(self) -> None:
        # Metadata Template and Formula (Add Text/Change $) are mutually
        # exclusive naming sources, not stacked — the template defines the
        # entire renamed stem itself, so no {base}.A$ channel-number suffix
        # gets appended on top. A user who wants one kept adds $trk/$chnum
        # to the template on purpose.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "Scene.A1.wav"
            source.touch()
            fields = {"tape": "220921", "scene": "S2-1"}
            jobs = WORKFLOW.build_rename_jobs(
                root, "{base}.A$", "{base}.A$", "Trk#", "bwf",
                output_root=root / "out",
                metadata_template="$tape_$scene",
                metadata_template_fields_reader=lambda path: (fields, {}),
            )

        self.assertEqual(jobs[0].target.name, "220921_S2-1.wav")
        # The fields/trk_table are cached on the Job so a later live-refresh
        # (settings changed, e.g. toggling Change $) can re-expand the same
        # template without re-reading metadata from disk.
        self.assertEqual(jobs[0].metadata_template_fields, fields)

    def test_metadata_template_falls_back_to_the_plain_base_when_it_expands_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "Scene.A1.wav"
            source.touch()
            jobs = WORKFLOW.build_rename_jobs(
                root, "{base}.A$", "{base}.A$", "Trk#", "bwf",
                output_root=root / "out",
                metadata_template="$nonexistenttoken",
                metadata_template_fields_reader=lambda path: ({"tape": "220921"}, {}),
            )

        self.assertEqual(jobs[0].target.name, "Scene.A1.wav")

    def test_metadata_template_still_composes_with_serial_id(self) -> None:
        # Find & Replace and Serial ID are universal post-processing steps
        # that apply regardless of which naming source built the stem —
        # they're not part of the mutually-exclusive Formula/Metadata
        # Template choice.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "Scene.A1.wav"
            source.touch()
            fields = {"tape": "220921", "scene": "S2-1"}
            jobs = WORKFLOW.build_rename_jobs(
                root, "{base}.A$", "{base}.A$", "Trk#", "bwf",
                output_root=root / "out",
                metadata_template="$tape_$scene",
                metadata_template_fields_reader=lambda path: (fields, {}),
                serial_id_map={},
                serial_id_enabled=True,
                serial_prefix="ABC",
                serial_digits=4,
                serial_start=1,
            )

        self.assertEqual(jobs[0].target.name, "ABC0001_220921_S2-1.wav")

    def test_metadata_template_disabled_leaves_the_plain_base_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "Scene.A1.wav"
            source.touch()
            reader_calls = []
            jobs = WORKFLOW.build_rename_jobs(
                root, "{base}.A$", "{base}.A$", "Trk#", "bwf",
                output_root=root / "out",
                metadata_template_fields_reader=lambda path: reader_calls.append(path) or ({}, {}),
            )

        self.assertEqual(jobs[0].target.name, "Scene.A1.wav")
        self.assertEqual(reader_calls, [])  # never even reads metadata when the template is blank

    def test_metadata_template_reports_progress_while_reading_fields(self) -> None:
        # Same silent-read risk as the Tape + TC pass — this feature's own
        # confirmation dialog also warns it may take a while for large
        # folders, but the read loop behind it had no progress reporting.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "Scene.A1.wav"
            source.touch()
            progress = []

            def report(index: int, total: int, item: Path, _trk: int, status: str) -> None:
                progress.append((index, total, item, status))

            WORKFLOW.build_rename_jobs(
                root, "{base}.A$", "{base}.A$", "Trk#", "bwf",
                output_root=root / "out",
                metadata_template="$tape",
                metadata_template_fields_reader=lambda _path: ({"tape": "220921"}, {}),
                progress=report,
            )

            self.assertIn((1, 1, source, "Reading Metadata Template fields"), progress)

    def test_rename_only_files_restricts_the_batch_to_the_given_paths(self) -> None:
        # Backs the drag-and-drop "import just this file" flow — dropping
        # one or two files onto the list shouldn't scan (or rename)
        # anything else already sitting in the same folder.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wanted = root / "Scene.A1.wav"
            ignored = root / "Scene.A2.wav"
            wanted.touch()
            ignored.touch()

            jobs = WORKFLOW.build_rename_jobs(
                root, "{base}.A$", "{base}.A$", "Trk#", "bwf",
                output_root=root / "out",
                only_files=[wanted],
            )

        self.assertEqual([job.source for job in jobs], [wanted])

    def test_combine_execution_raw_copies_first_bext_then_writes_rebuilt_ixml(self) -> None:
        sources = [Path("Scene.A1.wav"), Path("Scene.A5.wav")]
        target = Path("Scene.wav")
        job = WORKFLOW.Job(sources[0], target, 2, None, 2, "READY", "combine", sources=sources, combine_kind="generic")
        calls = []
        with mock.patch.object(WORKFLOW, "combine_with_sox", side_effect=lambda *args: calls.append(("combine", args))), mock.patch.object(
            WORKFLOW, "poly_ixml_from_mono_sources", side_effect=lambda *args: calls.append(("build_ixml", args)) or "<BWFXML />"
        ), mock.patch.object(WORKFLOW, "write_combine_metadata", side_effect=lambda *args: calls.append(("write_combine_metadata", args))), mock.patch.object(
            WORKFLOW, "write_core", side_effect=AssertionError("Combine must not serialize Core/BEXT")
        ):
            WORKFLOW.execute_combine_job(job, "sox", "bwf", replace=False)

        self.assertEqual([name for name, _args in calls], ["combine", "build_ixml", "write_combine_metadata"])
        self.assertEqual(calls[-1][1], ("bwf", sources, target, "<BWFXML />"))

    def test_combine_metadata_raw_copies_first_bext_then_writes_rebuilt_ixml(self) -> None:
        sources = [Path("Scene.A1.wav"), Path("Scene.A5.wav")]
        target = Path("Scene.wav")
        calls = []
        with mock.patch.object(WORKFLOW, "copy_wav_chunk_from_source", side_effect=lambda *args: calls.append(("copy_bext", args))), mock.patch.object(
            WORKFLOW, "write_ixml", side_effect=lambda *args: calls.append(("write_ixml", args))
        ):
            WORKFLOW.write_combine_metadata("bwf", sources, target, "<BWFXML />")

        self.assertEqual([name for name, _args in calls], ["copy_bext", "write_ixml"])
        self.assertEqual(calls[0][1], (sources[0], target, b"bext"))
        self.assertEqual(calls[1][1], ("bwf", target, "<BWFXML />"))

    def test_reembed_execution_raw_copies_bext_after_writing_rebuilt_ixml(self) -> None:
        source = Path("Scene.A5.wav")
        target = Path("Scene_AAP.A3.wav")
        job = WORKFLOW.Job(source, target, 1, 5, 3, "READY", "reembed")
        calls = []

        class Value:
            def get(self):
                return "unused"

        class Worker:
            bwf_var = Value()
            sox_var = Value()
            conflict_policy_var = Value()
            pause_requested = type("Pause", (), {"is_set": staticmethod(lambda: False)})()
            cancel_requested = type("Cancel", (), {"is_set": staticmethod(lambda: False)})()
            events = type("Events", (), {"put": staticmethod(lambda _event: None)})()
            _resolve_conflict_decision = staticmethod(lambda _target: "skip")

        with mock.patch.object(WORKFLOW, "export_ixml", side_effect=lambda *args: calls.append(("export_ixml", args)) or "<BWFXML />"), mock.patch.object(
            WORKFLOW, "read_wav_pcm_layout", return_value={"channels": 1}
        ), mock.patch.object(
            WORKFLOW, "extract_mono_ixml", side_effect=lambda *args, **kwargs: calls.append(("extract_mono_ixml", (args, kwargs))) or ("<BWFXML />", 5)
        ), mock.patch.object(WORKFLOW, "write_ixml", side_effect=lambda *args: calls.append(("write_ixml", args))), mock.patch.object(
            WORKFLOW, "copy_wav_chunk_from_source", side_effect=lambda *args: calls.append(("copy_bext", args))
        ), mock.patch.object(WORKFLOW, "remove_wav_chunk", side_effect=lambda *args: calls.append(("remove_list", args))
        ), mock.patch.object(WORKFLOW, "write_core", side_effect=AssertionError("Re-embed must not serialize Core/BEXT")), mock.patch.object(
            WORKFLOW, "finish_reembed_renames", side_effect=lambda *_args: None
        ):
            WORKFLOW.WorkflowApp._execute_worker(Worker(), [job], Path("."))

        self.assertEqual([name for name, _args in calls], ["export_ixml", "extract_mono_ixml", "write_ixml", "copy_bext", "remove_list"])
        # Mono source: reads track index 1, not strict (a missing Trk#
        # there just falls back to job.channel, doesn't block).
        extract_args, extract_kwargs = calls[1][1]
        self.assertEqual(extract_args[1], 1)
        self.assertNotIn("strict", extract_kwargs)
        self.assertEqual(calls[-2][1], (source, target, b"bext"))
        self.assertEqual(calls[-1][1], (target, b"LIST"))

    def test_reembed_execution_uses_the_poly_slot_reader_for_a_multichannel_source(self) -> None:
        # A Poly-source Re-embed job's `channel` is that file's own
        # stream_index (see build_reembed_jobs) — track index 1 (what the
        # mono-source branch always reads) would silently grab the wrong
        # channel's metadata for anything past stream 1, so a genuinely
        # multi-channel source must read its own real stream index,
        # strictly (no silent fallback if that channel's Trk# is missing).
        source = Path("Scene.wav")
        target = Path("Scene.A3 render 1.wav")
        job = WORKFLOW.Job(source, target, 2, 3, 2, "READY", "reembed")
        calls = []

        class Value:
            def get(self):
                return "unused"

        class Worker:
            bwf_var = Value()
            sox_var = Value()
            conflict_policy_var = Value()
            pause_requested = type("Pause", (), {"is_set": staticmethod(lambda: False)})()
            cancel_requested = type("Cancel", (), {"is_set": staticmethod(lambda: False)})()
            events = type("Events", (), {"put": staticmethod(lambda _event: None)})()
            _resolve_conflict_decision = staticmethod(lambda _target: "skip")

        with mock.patch.object(WORKFLOW, "export_ixml", return_value="<BWFXML />"), mock.patch.object(
            WORKFLOW, "read_wav_pcm_layout", return_value={"channels": 3}
        ), mock.patch.object(
            WORKFLOW, "extract_mono_ixml", side_effect=lambda *args, **kwargs: calls.append(("extract_mono_ixml", (args, kwargs))) or ("<BWFXML />", 3)
        ), mock.patch.object(WORKFLOW, "write_ixml"), mock.patch.object(
            WORKFLOW, "copy_wav_chunk_from_source"
        ), mock.patch.object(WORKFLOW, "remove_wav_chunk"), mock.patch.object(
            WORKFLOW, "finish_reembed_renames", side_effect=lambda *_args: None
        ):
            WORKFLOW.WorkflowApp._execute_worker(Worker(), [job], Path("."))

        self.assertEqual([name for name, _args in calls], ["extract_mono_ixml"])
        args, kwargs = calls[0][1]
        self.assertEqual(args, ("<BWFXML />", 2))
        # Poly source: strict, so a missing Trk# for this stream fails
        # loudly instead of silently guessing which channel this is.
        self.assertTrue(kwargs.get("strict"))

    def test_split_execution_preserves_chunks_before_writing_mono_ixml(self) -> None:
        source = Path("Scene.WAV")
        target = Path("Scene.A5.WAV")
        job = WORKFLOW.Job(source, target, 3, 5, 3, "READY", "split")
        calls = []

        class Value:
            def get(self):
                return "unused"

        class Worker:
            bwf_var = Value()
            sox_var = Value()
            conflict_policy_var = Value()
            pause_requested = type("Pause", (), {"is_set": staticmethod(lambda: False)})()
            cancel_requested = type("Cancel", (), {"is_set": staticmethod(lambda: False)})()
            events = type("Events", (), {"put": staticmethod(lambda _event: None)})()
            _resolve_conflict_decision = staticmethod(lambda _target: "skip")

        with mock.patch.object(WORKFLOW, "export_ixml", return_value="<BWFXML />"), mock.patch.object(
            WORKFLOW, "extract_mono_ixml", return_value=("<BWFXML />", 5)
        ), mock.patch.object(WORKFLOW, "split_with_sox", side_effect=lambda *args: calls.append(("split", args))), mock.patch.object(
            WORKFLOW, "write_split_metadata", side_effect=lambda *args: calls.append(("write_split_metadata", args))
        ), mock.patch.object(WORKFLOW, "finish_reembed_renames", side_effect=lambda *_args: None
        ):
            WORKFLOW.WorkflowApp._execute_worker(Worker(), [job], Path("."))

        self.assertEqual([name for name, _args in calls], ["split", "write_split_metadata"])
        self.assertEqual(calls[-1][1], ("unused", source, target, "<BWFXML />"))


class ReembedRenameConflictCharacterizationTests(unittest.TestCase):
    # Re-embed's own "If output exists" dropdown used to have zero effect —
    # a rename-after-embed target that already existed always hard-failed
    # regardless of what was selected, and only after the file had already
    # been embedded (metadata written into it) with no way back. The fix
    # moved the conflict decision up to job-planning time, the same point
    # Split/Combine decide theirs, so a whole conflicted job — embed *and*
    # rename — can be skipped/asked-about before anything happens to it,
    # instead of the embed always happening and only the rename being
    # skippable after the fact.
    def test_build_reembed_jobs_marks_a_rename_conflict_per_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            source = source_root / "AAA.A1.wav"
            source.touch()
            matched_target = target_root / "AAA.A1 render 01.wav"
            matched_target.touch()
            # The rename-after-embed formula "{base}.A$" would produce this
            # exact name for channel 1 — pre-existing, so it conflicts.
            conflicting_final = target_root / "AAA.A1.wav"
            conflicting_final.touch()

            def build(policy: str) -> str:
                with mock.patch.object(WORKFLOW, "split_recorder_channel", return_value=1):
                    jobs = WORKFLOW.build_reembed_jobs(
                        source_root, target_root,
                        rename_formula="{base}.A$", rename_number_mode="Trk#", conflict_policy=policy,
                    )
                self.assertEqual(len(jobs), 1)
                return jobs[0].status

            self.assertEqual(build("Skip existing"), "SKIP EXISTS")
            self.assertEqual(build("Replace existing"), "READY REPLACE")
            self.assertEqual(build("Ask"), "CONFLICT: target exists")

    def test_build_reembed_jobs_stays_ready_when_the_rename_target_is_free(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            source = source_root / "AAA.A1.wav"
            source.touch()
            (target_root / "AAA.A1 render 01.wav").touch()

            with mock.patch.object(WORKFLOW, "split_recorder_channel", return_value=1):
                jobs = WORKFLOW.build_reembed_jobs(
                    source_root, target_root,
                    rename_formula="{base}.A$", rename_number_mode="Trk#", conflict_policy="Ask",
                )

            self.assertEqual(jobs[0].status, "READY")

    def test_finish_reembed_renames_overwrites_when_conflict_was_already_resolved_to_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            embedded = root / "embedded.wav"
            embedded.write_text("embedded content")
            final_target = root / "final.wav"
            final_target.write_text("stale content")
            # READY REPLACE is what the execute loop sets once a CONFLICT
            # job's decision resolves to "replace" — this is the only
            # signal finish_reembed_renames trusts to allow the overwrite.
            job = WORKFLOW.Job(Path("source.wav"), embedded, 1, 1, 1, "READY REPLACE", "reembed", final_target=final_target)
            row = {"status": "OK", "target": str(embedded)}

            WORKFLOW.finish_reembed_renames([(job, row)])

            self.assertEqual(row["status"], "OK")
            self.assertEqual(final_target.read_text(), "embedded content")
            self.assertEqual(job.target, final_target)

    def test_finish_reembed_renames_still_fails_an_unresolved_conflict(self) -> None:
        # A final_target existing here without the job having been marked
        # READY REPLACE means the conflict was never decided (a race since
        # Preview, not a normal, already-handled case) — that must still be
        # a hard failure, not a silent skip or a second prompt.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            embedded = root / "embedded.wav"
            embedded.write_text("embedded content")
            final_target = root / "final.wav"
            final_target.write_text("pre-existing content")
            job = WORKFLOW.Job(Path("source.wav"), embedded, 1, 1, 1, "READY", "reembed", final_target=final_target)
            row = {"status": "OK", "target": str(embedded)}

            WORKFLOW.finish_reembed_renames([(job, row)])

            self.assertEqual(row["status"], "FAIL: rename target exists: final.wav")
            self.assertEqual(final_target.read_text(), "pre-existing content")
            self.assertEqual(job.target, embedded)


class QCCoreCharacterizationTests(unittest.TestCase):
    def test_qc_input_detection_handles_files_folders_and_poly_direction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_file = root / "source.wav"
            target_file = root / "target.wav"
            source_file.touch()
            target_file.touch()
            self.assertEqual(WORKFLOW.detect_qc_inputs(source_file, target_file).kind, "file")

            left = root / "left"
            right = root / "right"
            left.mkdir()
            right.mkdir()
            left_wav = left / "Scene.wav"
            right_wav = right / "Scene.A1.wav"
            left_wav.touch()
            right_wav.touch()
            channels = {left_wav: 8, right_wav: 1}
            reader = lambda path: {"channels": channels[path]}

            detection = WORKFLOW.detect_qc_inputs(left, right, reader)
            self.assertEqual((detection.kind, detection.source, detection.target), ("poly_split", left, right))
            reversed_detection = WORKFLOW.detect_qc_inputs(right, left, reader)
            self.assertEqual((reversed_detection.kind, reversed_detection.source, reversed_detection.target), ("poly_split", left, right))
            self.assertIn("reversed", reversed_detection.message)

            channels[left_wav] = 1
            self.assertEqual(WORKFLOW.detect_qc_inputs(left, right, reader).kind, "folder")

    def test_qc_input_detection_rejects_mixed_path_types(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wav_path = root / "source.wav"
            folder = root / "target"
            wav_path.touch()
            folder.mkdir()

            detection = WORKFLOW.detect_qc_inputs(wav_path, folder)

            self.assertEqual(detection.kind, "invalid")
            self.assertIn("both be files or both be folders", detection.message)

    def test_qc_input_detection_uses_name_prefix_evidence_for_mixed_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            left = root / "left"
            right = root / "right"
            left.mkdir()
            right.mkdir()
            left_paths = [left / "Scene.wav", left / "Other.A1.wav"]
            right_paths = [right / "Scene.A1.wav", right / "Scene.A2.wav", right / "Other.wav"]
            for path in left_paths + right_paths:
                path.touch()
            channels = {
                left_paths[0]: 8,
                left_paths[1]: 1,
                right_paths[0]: 1,
                right_paths[1]: 1,
                right_paths[2]: 2,
            }

            detection = WORKFLOW.detect_qc_inputs(left, right, lambda path: {"channels": channels[path]})

            self.assertEqual((detection.kind, detection.source, detection.target), ("poly_split", left, right))

    def test_qc_input_detection_finds_poly_split_for_any_split_naming(self) -> None:
        # No formula: "_TEST.A1", "_AAP.A1", " render 001" all start with the
        # Poly's name, so the folders are still detected as Poly ↔ Split.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            poly_folder = root / "poly"
            split_folder = root / "split"
            poly_folder.mkdir()
            split_folder.mkdir()
            poly_wav = poly_folder / "Scene.wav"
            split_wavs = [split_folder / "Scene_TEST.A1.wav", split_folder / "Scene render 001.wav"]
            poly_wav.touch()
            for path in split_wavs:
                path.touch()
            channels = {poly_wav: 8, **{path: 1 for path in split_wavs}}
            reader = lambda path: {"channels": channels[path]}

            detection = WORKFLOW.detect_qc_inputs(poly_folder, split_folder, reader)
            self.assertEqual((detection.kind, detection.source, detection.target), ("poly_split", poly_folder, split_folder))

    def test_qc_input_detection_does_not_treat_a_longer_take_number_as_a_split(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            left = root / "left"
            right = root / "right"
            left.mkdir()
            right.mkdir()
            poly_wav = left / "12-1T1.wav"
            mono_wav = right / "12-1T10.A1.wav"
            poly_wav.touch()
            mono_wav.touch()
            channels = {poly_wav: 4, mono_wav: 1}

            detection = WORKFLOW.detect_qc_inputs(left, right, lambda path: {"channels": channels[path]})
            self.assertEqual(detection.kind, "folder")

    def test_folder_qc_recursively_matches_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            for folder in (source_root / "Day01", source_root / "Day02", target_root / "Day01", target_root / "Day03"):
                folder.mkdir(parents=True)
            source_match = source_root / "Day01" / "Scene.wav"
            target_match = target_root / "Day01" / "Scene.wav"
            source_only = source_root / "Day02" / "Scene.wav"
            target_only = target_root / "Day03" / "Scene.wav"
            for path in (source_match, target_match, source_only, target_only):
                path.touch()

            progress = []
            with mock.patch.object(WORKFLOW, "compare_basic_audio", return_value={"result": "PASS"}):
                result = WORKFLOW.compare_qc_folders("soxi", source_root, target_root, lambda stage, current, total: progress.append((stage, current, total)))

            self.assertEqual([(row["filename"], row["result"]) for row in result["rows"]], [
                ("Day01/Scene.wav", "PASS"),
                ("Day02/Scene.wav", "MISSING"),
                ("Day03/Scene.wav", "EXTRA"),
            ])
            self.assertIn(("Scanning Source", 2, 2), progress)
            self.assertIn(("Scanning Target", 2, 2), progress)
            self.assertEqual(progress[-1], ("Comparing", 3, 3))

    def test_match_qc_folder_files_by_name_reports_progress(self) -> None:
        # One bwfmetaedit-backed recorder channel read per file that has a
        # name candidate, before the main comparison loop — must report
        # progress so a big Folder QC run never goes silent here.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "Scene.A1.wav"
            target_path = root / "Scene_AAP.A1.wav"
            source_path.touch()
            target_path.touch()
            progress = []
            with mock.patch.object(WORKFLOW, "split_recorder_channel", return_value=1):
                pairs, ambiguous_sources, ambiguous_targets = WORKFLOW.match_qc_folder_files_by_name(
                    "bwf",
                    {"Scene.A1.wav": source_path},
                    {"Scene_AAP.A1.wav": target_path},
                    lambda stage, current, total: progress.append((stage, current, total)),
                )

            self.assertEqual(pairs, [("Scene.A1.wav", "Scene_AAP.A1.wav")])
            self.assertEqual((ambiguous_sources, ambiguous_targets), (set(), set()))
            self.assertIn(("Matching by name + recorder channel", 2, 2), progress)

    def _qc_folders(self, source_names, target_names, channels):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_folder = Path(temp_dir) / "source"
            target_folder = Path(temp_dir) / "target"
            source_folder.mkdir()
            target_folder.mkdir()
            by_name = {}
            for folder, names in ((source_folder, source_names), (target_folder, target_names)):
                for name in names:
                    path = folder / name
                    path.touch()
                    by_name[path] = channels[(folder.name, name)]
            with mock.patch.object(WORKFLOW, "split_recorder_channel", side_effect=lambda _tool, path: by_name[path]), mock.patch.object(
                WORKFLOW, "compare_basic_audio", return_value={"audio": [], "result": "PASS"}
            ):
                comparison = WORKFLOW.compare_qc_folders("soxi", source_folder, target_folder, None, "bwf", True, False)
        return {row["filename"]: row["result"] for row in comparison["rows"]}

    def test_compare_qc_folders_pairs_in_both_directions(self) -> None:
        # Source = render (longer name), Target = original: still paired.
        results = self._qc_folders(
            ["Scene_AAP.A1.wav", "Scene_AAP.A2.wav", "15_WILDT01_AAP.wav"],
            ["Scene.A1.wav", "Scene.A2.wav", "15_WILDT01.wav"],
            {
                ("source", "Scene_AAP.A1.wav"): 3, ("source", "Scene_AAP.A2.wav"): 4, ("source", "15_WILDT01_AAP.wav"): 1,
                ("target", "Scene.A1.wav"): 3, ("target", "Scene.A2.wav"): 4, ("target", "15_WILDT01.wav"): 1,
            },
        )
        self.assertEqual(results, {
            "15_WILDT01_AAP.wav ↔ 15_WILDT01.wav": "PASS",
            "Scene_AAP.A1.wav ↔ Scene.A1.wav": "PASS",
            "Scene_AAP.A2.wav ↔ Scene.A2.wav": "PASS",
        })

    def test_compare_qc_folders_recorder_channel_decides_and_take_numbers_stay_apart(self) -> None:
        results = self._qc_folders(
            ["12-1T1.A1.wav", "12-1T10.A1.wav"],
            ["12-1T1.A1 render 001.wav", "12-1T10.A1 render 001.wav", "12-1T1.A2 render 001.wav"],
            {
                ("source", "12-1T1.A1.wav"): 1, ("source", "12-1T10.A1.wav"): 1,
                ("target", "12-1T1.A1 render 001.wav"): 1, ("target", "12-1T10.A1 render 001.wav"): 1,
                ("target", "12-1T1.A2 render 001.wav"): 2,
            },
        )
        self.assertEqual(results, {
            "12-1T1.A1.wav ↔ 12-1T1.A1 render 001.wav": "PASS",
            "12-1T10.A1.wav ↔ 12-1T10.A1 render 001.wav": "PASS",
            "12-1T1.A2 render 001.wav": "EXTRA",
        })

    def test_compare_qc_folders_never_guesses_between_two_candidates(self) -> None:
        # Two renders of the same take and channel: shown as AMBIGUOUS.
        results = self._qc_folders(
            ["Scene.A1.wav"],
            ["Scene.A1 render 001.wav", "Scene.A1 render 002.wav"],
            {("source", "Scene.A1.wav"): 1, ("target", "Scene.A1 render 001.wav"): 1, ("target", "Scene.A1 render 002.wav"): 1},
        )
        self.assertEqual(results, {
            "Scene.A1.wav": "AMBIGUOUS",
            "Scene.A1 render 001.wav": "AMBIGUOUS",
            "Scene.A1 render 002.wav": "AMBIGUOUS",
        })
        self.assertEqual(WORKFLOW.qc_result_fill_key("AMBIGUOUS"), "blocked")

    def test_compare_qc_folders_prefers_the_longest_shared_name(self) -> None:
        # "Scene_3_Mix_AAP.A1" starts with both "Scene_3" and "Scene_3_Mix":
        # the longer name wins instead of being called ambiguous.
        results = self._qc_folders(
            ["Scene_3.A1.wav", "Scene_3_Mix.A1.wav"],
            ["Scene_3_AAP.A1.wav", "Scene_3_Mix_AAP.A1.wav"],
            {
                ("source", "Scene_3.A1.wav"): 1, ("source", "Scene_3_Mix.A1.wav"): 1,
                ("target", "Scene_3_AAP.A1.wav"): 1, ("target", "Scene_3_Mix_AAP.A1.wav"): 1,
            },
        )
        self.assertEqual(results, {
            "Scene_3.A1.wav ↔ Scene_3_AAP.A1.wav": "PASS",
            "Scene_3_Mix.A1.wav ↔ Scene_3_Mix_AAP.A1.wav": "PASS",
        })

    def test_formula_discovers_split_but_metadata_defines_recorder_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            poly = source_root / "AAA.WAV"
            poly.touch()
            splits = [target_root / f"aaa_aap.a{number}.wav" for number in range(1, 5)]
            for path in splits:
                path.touch()
            recorder_channels = dict(zip(splits, (1, 2, 5, 6)))
            progress = []
            prepare_progress = []
            with mock.patch.object(WORKFLOW, "read_wav_pcm_layout", return_value={"channels": 4}), mock.patch.object(
                WORKFLOW, "poly_track_mapping", return_value=[(1, 1), (2, 2), (3, 5), (4, 6)]
            ), mock.patch.object(
                WORKFLOW, "split_recorder_channel", side_effect=lambda _tool, path: recorder_channels[path]
            ), mock.patch.object(WORKFLOW, "compare_wav_pcm_stream", return_value=(1000, "PASS")):
                result = WORKFLOW.compare_poly_split_folders(
                    "bwf",
                    source_root,
                    target_root,
                    lambda stage, current, total: progress.append((stage, current, total)),
                    lambda stage, current, total: prepare_progress.append((stage, current, total)),
                )

            self.assertEqual(result["result"], "PASS")
            self.assertEqual(
                [(row["stream"], row["channel"], row["split_file"]) for row in result["rows"]],
                [("1", "1", "aaa_aap.a1.wav"), ("2", "2", "aaa_aap.a2.wav"), ("3", "5", "aaa_aap.a3.wav"), ("4", "6", "aaa_aap.a4.wav")],
            )
            self.assertEqual(progress, [("Comparing", 1, 4), ("Comparing", 2, 4), ("Comparing", 3, 4), ("Comparing", 4, 4)])
            self.assertIn(("Inspecting Poly layouts", 1, 1), prepare_progress)
            self.assertIn(("Reading Split metadata", 4, 4), prepare_progress)
            self.assertIn(("Reading Poly metadata", 1, 1), prepare_progress)

    def test_poly_split_pairs_any_split_naming_without_a_formula(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            (source_root / "AAA.WAV").touch()
            names = ["AAA_AAP.A1.wav", "AAA.A2 render 001.wav", "AAA10.A1.wav", "BBB.A1.wav"]
            for name in names:
                (target_root / name).touch()
            recorder_channels = {"AAA_AAP.A1.wav": 1, "AAA.A2 render 001.wav": 2}
            read_channels = []

            def fake_channel(_tool, path):
                read_channels.append(path.name)
                return recorder_channels[path.name]

            with mock.patch.object(WORKFLOW, "read_wav_pcm_layout", return_value={"channels": 2}), mock.patch.object(
                WORKFLOW, "poly_track_mapping", return_value=[(1, 1), (2, 2)]
            ), mock.patch.object(WORKFLOW, "split_recorder_channel", side_effect=fake_channel), mock.patch.object(
                WORKFLOW, "compare_wav_pcm_stream", return_value=(1000, "PASS")
            ):
                result = WORKFLOW.compare_poly_split_folders("bwf", source_root, target_root)

            self.assertEqual(
                sorted((row["poly_file"], row["stream"], row["split_file"], row["result"]) for row in result["rows"]),
                [
                    ("AAA.WAV", "1", "AAA_AAP.A1.wav", "PASS"),
                    ("AAA.WAV", "2", "AAA.A2 render 001.wav", "PASS"),
                    ("—", "—", "AAA10.A1.wav", "EXTRA"),
                    ("—", "—", "BBB.A1.wav", "EXTRA"),
                ],
            )
            # Files that start no Poly name are never read.
            self.assertEqual(sorted(read_channels), ["AAA.A2 render 001.wav", "AAA_AAP.A1.wav"])

    def test_poly_split_keeps_a_mono_originals_render_on_its_skip_row(self) -> None:
        # A mono take in the Poly folder is SKIP: NOT POLY; its render
        # ("10-1T1_AAP.wav") belongs to that row, not an EXTRA.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            poly = source_root / "AAA.WAV"
            mono = source_root / "10-1T1.WAV"
            poly.touch()
            mono.touch()
            split = target_root / "AAA.A1.wav"
            mono_render = target_root / "10-1T1_AAP.wav"
            split.touch()
            mono_render.touch()
            layouts = {poly: 2, mono: 1}
            with mock.patch.object(WORKFLOW, "read_wav_pcm_layout", side_effect=lambda path: {"channels": layouts[path]}), mock.patch.object(
                WORKFLOW, "poly_track_mapping", return_value=[(1, 1), (2, 2)]
            ), mock.patch.object(WORKFLOW, "split_recorder_channel", return_value=1), mock.patch.object(
                WORKFLOW, "compare_wav_pcm_stream", return_value=(1000, "PASS")
            ):
                result = WORKFLOW.compare_poly_split_folders("bwf", source_root, target_root, include_audio=False)

            results = sorted((row["poly_file"], row["split_file"], row["result"]) for row in result["rows"])
            self.assertEqual(results, [
                ("10-1T1.WAV", "10-1T1_AAP.wav", "SKIP: NOT POLY"),
                ("AAA.WAV", "AAA.A1.wav", "-"),
                ("AAA.WAV", "—", "MISSING"),
            ])

    def test_poly_split_in_one_folder_never_pairs_a_poly_with_itself(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            poly = folder / "AAA.WAV"
            split = folder / "AAA.A1.wav"
            poly.touch()
            split.touch()
            with mock.patch.object(WORKFLOW, "read_wav_pcm_layout", side_effect=lambda path: {"channels": 1 if path == split else 2}), mock.patch.object(
                WORKFLOW, "poly_track_mapping", return_value=[(1, 1), (2, 2)]
            ), mock.patch.object(WORKFLOW, "split_recorder_channel", return_value=1), mock.patch.object(
                WORKFLOW, "compare_wav_pcm_stream", return_value=(1000, "PASS")
            ):
                result = WORKFLOW.compare_poly_split_folders("bwf", folder, folder)

            rows = [(row["stream"], row["split_file"], row["result"]) for row in result["rows"] if row["result"] != "SKIP: NOT POLY"]
            self.assertEqual(rows, [("1", "AAA.A1.wav", "PASS"), ("2", "—", "MISSING")])

    def test_compare_poly_split_folders_marks_audio_not_compared_when_audio_toggle_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            poly = source_root / "AAA.WAV"
            poly.touch()
            splits = [target_root / f"AAA.A{number}.wav" for number in (1, 2)]
            for path in splits:
                path.touch()
            recorder_channels = dict(zip(splits, (1, 2)))

            with mock.patch.object(WORKFLOW, "read_wav_pcm_layout", return_value={"channels": 2}), mock.patch.object(
                WORKFLOW, "poly_track_mapping", return_value=[(1, 1), (2, 2)]
            ), mock.patch.object(
                WORKFLOW, "split_recorder_channel", side_effect=lambda _tool, path: recorder_channels[path]
            ), mock.patch.object(WORKFLOW, "compare_wav_pcm_stream") as mock_compare_pcm:
                result = WORKFLOW.compare_poly_split_folders(
                    "bwf", source_root, target_root, include_audio=False,
                )

        mock_compare_pcm.assert_not_called()
        self.assertTrue(all(row["result"] == "-" for row in result["rows"]))
        self.assertEqual(result["result"], "PASS")

    def test_compare_poly_split_folders_reports_progress_during_the_metadata_pass(self) -> None:
        # augment_qc_rows_with_metadata's own docstring warns this pass runs
        # two bwfmetaedit reads per row with nothing marking the wait unless
        # its caller forwards a progress callback — compare_poly_split_folders
        # used to call it with none at all, so a batch would sit at the
        # Audio pass's "100%" for as long as this silent pass took.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            poly = source_root / "AAA.WAV"
            poly.touch()
            splits = [target_root / f"AAA.A{number}.wav" for number in (1, 2)]
            for path in splits:
                path.touch()
            recorder_channels = dict(zip(splits, (1, 2)))
            progress = []

            with mock.patch.object(WORKFLOW, "read_wav_pcm_layout", return_value={"channels": 2}), mock.patch.object(
                WORKFLOW, "poly_track_mapping", return_value=[(1, 1), (2, 2)]
            ), mock.patch.object(
                WORKFLOW, "split_recorder_channel", side_effect=lambda _tool, path: recorder_channels[path]
            ), mock.patch.object(WORKFLOW, "compare_wav_pcm_stream", return_value=(1000, "PASS")), mock.patch.object(
                WORKFLOW, "compare_qc_metadata_fields", return_value={"result": "PASS", "rows": []}
            ):
                WORKFLOW.compare_poly_split_folders(
                    "bwf", source_root, target_root,
                    lambda stage, current, total: progress.append((stage, current, total)),
                    include_metadata=True,
                )

        self.assertIn(("Comparing Metadata", 1, 2), progress)
        self.assertIn(("Comparing Metadata", 2, 2), progress)

    def test_poly_group_blocked_row_identifies_the_split_file_that_failed(self) -> None:
        # A collapsed BLOCKED row (one Split candidate can't yield a Recorder
        # Channel) used to drop that candidate's actual Path, leaving
        # split_file/split_path empty on the row — which both read as if the
        # *Poly* file itself (the only filename left visible on the row) was
        # the problem, and made "Reveal Split File in Finder" impossible
        # since there was no real path to reveal.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            poly = source_root / "AAA.WAV"
            poly.touch()
            good_split = target_root / "AAA.A1.wav"
            bad_split = target_root / "AAA.A2.wav"
            good_split.touch()
            bad_split.touch()

            def fake_split_recorder_channel(_tool, path):
                if path == bad_split:
                    raise RuntimeError(f"Recorder Ch metadata unavailable: {path.name}")
                return 1

            with mock.patch.object(WORKFLOW, "read_wav_pcm_layout", return_value={"channels": 2}), mock.patch.object(
                WORKFLOW, "poly_track_mapping", return_value=[(1, 1), (2, 2)]
            ), mock.patch.object(WORKFLOW, "split_recorder_channel", side_effect=fake_split_recorder_channel):
                result = WORKFLOW.compare_poly_split_folders("bwf", source_root, target_root)

            self.assertEqual(len(result["rows"]), 1)
            row = result["rows"][0]
            self.assertEqual(row["poly_file"], "AAA.WAV")
            self.assertEqual(row["split_file"], "AAA.A2.wav")
            self.assertEqual(row["split_path"], bad_split)
            self.assertIn("BLOCKED", row["result"])

    def test_split_without_recorder_channel_metadata_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            split = Path(temp_dir) / "AAA.A3.wav"
            split.touch()
            with mock.patch.object(WORKFLOW, "export_optional_ixml_for_inspector", return_value=(None, "no iXML")):
                with self.assertRaisesRegex(RuntimeError, "Recorder Ch metadata unavailable"):
                    WORKFLOW.split_recorder_channel("bwf", split)

    def test_split_recorder_channel_falls_back_to_flattened_track_list(self) -> None:
        # A DAW render/glue can produce iXML that bwfmetaedit re-exports with
        # TRACK_LIST/TRACK/CHANNEL_INDEX flattened into
        # TRACK_LIST_TRACK_CHANNEL_INDEX (seen from a real REAPER render)
        # instead of properly nested — split_recorder_channel must still
        # find the channel number rather than reporting it unavailable.
        flattened_ixml = "<BWFXML><TRACK_LIST_TRACK_CHANNEL_INDEX>6</TRACK_LIST_TRACK_CHANNEL_INDEX></BWFXML>"
        with tempfile.TemporaryDirectory() as temp_dir:
            split = Path(temp_dir) / "AAA.A3.wav"
            split.touch()
            with mock.patch.object(WORKFLOW, "export_optional_ixml_for_inspector", return_value=(flattened_ixml, None)):
                self.assertEqual(WORKFLOW.split_recorder_channel("bwf", split), 6)

    def test_split_recorder_channel_flattened_fallback_handles_multiple_tracks(self) -> None:
        flattened_ixml = (
            "<BWFXML><TRACK_LIST_TRACK_CHANNEL_INDEX>1</TRACK_LIST_TRACK_CHANNEL_INDEX>"
            "<TRACK_LIST_TRACK_CHANNEL_INDEX_2>2</TRACK_LIST_TRACK_CHANNEL_INDEX_2></BWFXML>"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            split = Path(temp_dir) / "AAA.A3.wav"
            split.touch()
            with mock.patch.object(WORKFLOW, "export_optional_ixml_for_inspector", return_value=(flattened_ixml, None)):
                with self.assertRaisesRegex(RuntimeError, "Recorder Ch metadata unavailable"):
                    WORKFLOW.split_recorder_channel("bwf", split)

    @unittest.skipUnless(shutil.which("sox"), "SoX is required for PCM format characterization")
    def test_integer_and_float_pcm_equivalence_and_difference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            integer = root / "integer.wav"
            floating = root / "floating.wav"
            subprocess.run(["sox", "-n", "-r", "48000", "-b", "24", "-c", "1", str(integer), "synth", "0.02", "sine", "997"], check=True)
            subprocess.run(["sox", str(integer), "-e", "floating-point", "-b", "32", str(floating)], check=True)

            self.assertEqual(WORKFLOW.read_wav_pcm_layout(floating)["audio_format"], 3)
            self.assertEqual(WORKFLOW.compare_wav_pcm_stream(integer, 1, floating)[1], "PASS")

            layout = WORKFLOW.read_wav_pcm_layout(floating)
            with floating.open("r+b") as wav_file:
                wav_file.seek(layout["data_offset"])
                sample = struct.unpack("<f", wav_file.read(4))[0]
                wav_file.seek(layout["data_offset"])
                wav_file.write(struct.pack("<f", sample + 0.01))
            self.assertEqual(WORKFLOW.compare_wav_pcm_stream(integer, 1, floating)[1], "DIFFERENT")



    def test_qc_progress_log_goes_by_time_not_count(self) -> None:
        state = {}
        due = lambda label, index, now: WORKFLOW.qc_progress_log_due(state, "qc_auto", label, index, 824, now)
        self.assertTrue(due("QC — Comparing", 1, 0.0))      # first step of a stage
        self.assertFalse(due("QC — Comparing", 2, 2.5))
        self.assertFalse(due("QC — Comparing", 100, 4.9))   # count alone no longer logs
        self.assertTrue(due("QC — Comparing", 101, 5.0))    # 5 s since the last line
        self.assertFalse(due("QC — Comparing", 102, 6.0))
        self.assertTrue(due("QC — Comparing", 824, 6.1))    # last step
        self.assertTrue(due("QC — Comparing Metadata", 3, 6.2))  # new stage
        self.assertTrue(WORKFLOW.qc_progress_log_due(state, "qc_folder", "Folder QC — Comparing", 7, 824, 6.3))  # scopes are separate

    def test_chunked_integer_float_check_matches_the_per_sample_rule(self) -> None:
        # The chunk-at-a-time check must give exactly the old per-sample
        # answer: round(float * 2^(bits-1)) == integer, including values a
        # hair off and the most negative / positive samples.
        import random
        from operations.audio_qc import _int_float_chunk_equal
        rng = random.Random(7)
        for width, bits in ((2, 16), (3, 24), (4, 32)):
            low, high = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
            values = [low, high, 0, -1, 1] + [rng.randint(low, high) for _ in range(300)]
            scale = float(1 << (bits - 1))
            for offset in (0.0, 0.2 / scale, 0.49 / scale, 0.51 / scale, 1.0 / scale):
                floats = [struct.unpack("<f", struct.pack("<f", value / scale + offset))[0] for value in values]
                int_bytes = b"".join(value.to_bytes(width, "little", signed=True) for value in values)
                float_bytes = struct.pack(f"<{len(floats)}f", *floats)
                expected = all(round(f * scale) == value for f, value in zip(floats, values))
                self.assertEqual(_int_float_chunk_equal(int_bytes, width, bits, float_bytes, 4), expected, (width, offset))
        self.assertIsNone(_int_float_chunk_equal(b"\x00" * 8, 2, 16, b"\x00" * 16, 8))


class StructureInspectorCharacterizationTests(unittest.TestCase):
    """The Audio Inspector's STRUCTURE section, read from the bytes directly
    so it still shows on files soxi / BWF MetaEdit can't open."""

    def test_damaged_file_still_gets_structure_when_tools_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "power_loss.wav"
            write_riff(path, [(b"fmt ", struct.pack("<HHIIHH", 1, 2, 48000, 288000, 6, 24)), (b"data", b"\x80" * 600)])
            data = bytearray(path.read_bytes())
            struct.pack_into("<I", data, 40, 0)  # data size never finalized
            path.write_bytes(bytes(data))
            sections, ixml_root = WORKFLOW.AudioInspector._file_sections_for_tools("/nonexistent/soxi", "/nonexistent/bwfmetaedit", path)
        titles = [title for title, _text in sections]
        self.assertEqual(titles, ["FILE", "STRUCTURE", "AUDIO"])
        structure = dict(sections)["STRUCTURE"]
        self.assertIn("never finalized", structure)
        self.assertIn("Open in Wave Repair for Post", structure)
        self.assertTrue(dict(sections)["AUDIO"].startswith("Unable to read:"))
        self.assertIsNone(ixml_root)



class StructureBlockCharacterizationTests(unittest.TestCase):
    """Split blocks structurally damaged sources before sox/BWF MetaEdit ever
    see them (both happily read many damaged files), and Wave Manager can
    later re-preview just those files and merge them back in."""

    @staticmethod
    def _wav(path: Path, odd_final_ixml: bool = False) -> Path:
        chunks = [(b"fmt ", struct.pack("<HHIIHH", 1, 1, 48000, 144000, 3, 24)), (b"data", b"\x80" * 300)]
        if odd_final_ixml:
            chunks.append((b"iXML", b"<BWFXML></BWFXML> "))  # 18 bytes... made odd below
        write_riff(path, chunks)
        if odd_final_ixml:
            data = bytearray(path.read_bytes())
            # Rewrite the iXML as odd-sized with no pad byte — the Cantar quirk.
            ixml_offset = data.rindex(b"iXML")
            payload = b"<BWFXML></BWFXML>"
            data = data[:ixml_offset] + b"iXML" + struct.pack("<I", len(payload)) + payload
            struct.pack_into("<I", data, 4, len(data) - 8)
            path.write_bytes(bytes(data))
        return path

    def test_damaged_source_is_blocked_before_any_tool_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            root.mkdir()
            self._wav(root / "A.wav", odd_final_ixml=True)
            with mock.patch.object(WORKFLOW, "soxi_channels", side_effect=AssertionError("soxi must not run")):
                jobs = WORKFLOW.build_split_jobs(root, Path(tmp) / "out", "bwf", "soxi", "Trk#", "{base}.A$", "Skip existing")
        self.assertEqual(len(jobs), 1)
        self.assertTrue(WORKFLOW.is_structure_block(jobs[0].status))
        self.assertIn("pad byte", jobs[0].status)
        self.assertIn("Wave Repair for Post", jobs[0].status)

    def test_sound_source_is_not_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._wav(Path(tmp) / "B.wav")
            self.assertIsNone(WORKFLOW.structure_block_status(path))

    def test_merge_refreshed_jobs_replaces_all_channels_in_place(self) -> None:
        a, b, c = Path("/x/a.wav"), Path("/x/b.wav"), Path("/x/c.wav")
        job = lambda source, channel, status="READY": WORKFLOW.Job(source, None, channel, None, None, status, "split")
        existing = [job(a, 1), job(a, 2), job(b, 0, "BLOCKED: structure — x"), job(c, 1)]
        refreshed = [job(b, ch) for ch in range(1, 13)] + [job(Path("/x/new.wav"), 1)]
        merged = WORKFLOW.merge_refreshed_jobs(existing, refreshed)
        self.assertEqual([(j.source.name, j.channel) for j in merged][:2], [("a.wav", 1), ("a.wav", 2)])
        self.assertEqual([j.channel for j in merged if j.source == b], list(range(1, 13)))
        self.assertEqual(merged[2].source, b)
        self.assertEqual(merged[14].source, c)
        self.assertEqual(merged[-1].source.name, "new.wav")
        self.assertEqual(len(merged), 4 - 1 + 12 + 1)


if __name__ == "__main__":
    unittest.main()
