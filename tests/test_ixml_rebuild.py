import re
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parent))

import wav_factory as wf
from operations.ixml_rebuild import (
    CONFLICT, INFERRED, OMITTED, ORIGINAL, RECOVERED, find_element, history_note, leaf_values, plan_rebuild, read_ixml_text, render_ixml,
)
from operations.riff_inspect import inspect_wav
from operations.riff_repair import REBUILD_CATEGORY, rebuild_target_problem, run_rebuild

RATE = 48000
FRAMES = 4800  # 0.1 s
RESERVED = 8192


def cantar_bext(number: int, scene: str, take: str, start_seconds: int) -> bytes:
    payload = bytearray(602)
    description = f"aUBITS=$13012600\r\naSCENE={scene}\r\naTAKE={take}    \r\naTAPE=0113\r\naSPEED=024.000-ND\r\naNOTE=    \r\naTYP=poly"
    payload[0:256] = description.encode().ljust(256, b"\x00")
    payload[256:288] = b"CANTAR 00519 v3.535".ljust(32, b"\x00")
    payload[288:320] = f"AATON                 UB{number}0000".encode().ljust(32, b"\x00")
    payload[320:330] = b"2026-01-13"
    clock = start_seconds + 1
    payload[330:338] = f"{clock // 3600:02}:{clock % 3600 // 60:02}:{clock % 60:02}".encode()
    struct.pack_into("<IIH", payload, 338, start_seconds * RATE + 1, 0, 1)
    return bytes(payload)


def cantar_ixml(number: int, scene: str, take: str, start_seconds: int, names: list[str], key: str) -> str:
    close = start_seconds + 0 + 60  # file end (0.1 s) + a 60 s recorder-clock offset
    uid_time = f"{close // 3600:02}{close % 3600 // 60:02}{close % 60:02}"
    tracks = "".join(f"<TRACK><CHANNEL_INDEX>{i}</CHANNEL_INDEX><INTERLEAVE_INDEX>{i}</INTERLEAVE_INDEX><NAME>{name}</NAME></TRACK>" for i, name in enumerate(names, 1))
    grid = "".join(f"<DATA><INDICE>{i}</INDICE><NAME>Trk {i + 1}</NAME></DATA>" for i in range(4))
    return (
        "<?xml version='1.0' encoding='UTF-8'?>\n<BWFXML><IXML_VERSION>2.01</IXML_VERSION><PROJECT>FISH</PROJECT>"
        f"<SCENE>{scene}</SCENE><TAKE>{take}</TAKE><TAPE>0113</TAPE><NOTE> </NOTE>"
        f"<FILE_UID>AAT19JS0009202600{13:02}{uid_time}{str(number)[-3:]}0011</FILE_UID>"
        "<SPEED><TIMECODE_RATE>24/1</TIMECODE_RATE><TIMECODE_FLAG>NDF</TIMECODE_FLAG>"
        f"<TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO>{start_seconds * RATE + 1}</TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO></SPEED>"
        f"<HISTORY><ORIGINAL_FILENAME>UB{number}.WAV</ORIGINAL_FILENAME></HISTORY>"
        f"<FILE_SET><FAMILY_NAME>{scene}/{take.rjust(6)}</FAMILY_NAME></FILE_SET>"
        f"<TRACK_LIST><TRACK_COUNT>{len(names)}</TRACK_COUNT>{tracks}</TRACK_LIST>"
        f"<BEXT><BWF_ORIGINATOR_REFERENCE>AATON                 UB{number}1011</BWF_ORIGINATOR_REFERENCE></BEXT>"
        f"<AATON_CANTAR><ALL_TRK_NAME>{grid}</ALL_TRK_NAME><CANTAR_KEY>{key}</CANTAR_KEY></AATON_CANTAR></BWFXML>"
    )


def write_take(folder: Path, number: int, scene: str, take: str, start: int, names: list[str], key: str, cut_off: bool = False) -> Path:
    fmt = wf.fmt_payload(channels=len(names), bits=24)
    audio = wf.audio_payload(FRAMES, block_align=3 * len(names))
    ixml = cantar_ixml(number, scene, take, start, names, key).encode().ljust(RESERVED, b"\x00")
    chunks = [wf.chunk(b"bext", cantar_bext(number, scene, take, start)), wf.chunk(b"fmt ", fmt), wf.chunk(b"data", audio), wf.chunk(b"iXML", ixml)]
    data = wf.riff(chunks)
    if cut_off:
        data = data[: len(data) - RESERVED]  # header written, contents never
    return wf.write(folder / f"UB{number}.WAV", data)


class RebuildTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name) / "ISO POLY" / "2026-01-13.AAN"
        self.folder.mkdir(parents=True)
        # Four normal takes around a cut-off one (UB4703). Track 1 is the
        # same everywhere, track 2 changes between the takes either side of
        # the damaged one (a real conflict), track 3 only gets a name later.
        write_take(self.folder, 4700, "23/5", "t1", 70000, ["BOOM", "WU", ""], "k0")
        write_take(self.folder, 4701, "23/5", "t2", 70300, ["BOOM", "WU", ""], "k1")
        write_take(self.folder, 4702, "23/6", "t1", 70600, ["BOOM", "WU", ""], "k2")
        self.target = write_take(self.folder, 4703, "23/6", "t2", 70900, ["BOOM", "?", "?"], "k3", cut_off=True)
        write_take(self.folder, 4704, "23/6", "t3", 71200, ["BOOM", "Clerk", ""], "k4")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def field(self, plan, path):
        return next(item for item in plan.fields if item.path == path)


class PlanTests(RebuildTestCase):
    def test_target_is_eligible_and_finds_neighbours(self) -> None:
        report = inspect_wav(self.target)
        self.assertEqual(rebuild_target_problem(report), "")
        plan = plan_rebuild(self.target, report)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.related_count, 4)
        self.assertEqual((plan.previous.path.name, plan.following.path.name), ("UB4702.WAV", "UB4704.WAV"))
        self.assertEqual(plan.reserved_size, RESERVED)

    def test_field_provenance(self) -> None:
        plan = plan_rebuild(self.target)
        self.assertEqual((self.field(plan, "SCENE").value, self.field(plan, "SCENE").confidence), ("23/6", ORIGINAL))
        self.assertEqual((self.field(plan, "TAKE").value, self.field(plan, "TAKE").confidence), ("t2", ORIGINAL))
        self.assertEqual(self.field(plan, "PROJECT").confidence, RECOVERED)
        self.assertEqual(self.field(plan, "TRACK_LIST/TRACK[1]/NAME").confidence, RECOVERED)
        conflict = self.field(plan, "TRACK_LIST/TRACK[2]/NAME")
        self.assertEqual(conflict.confidence, CONFLICT)
        self.assertEqual([value for value, _source in conflict.options], ["WU", "Clerk", None])
        self.assertEqual(conflict.options[2][1], "leave empty")
        # "" vs "" either side: agreement, not a conflict
        self.assertEqual(self.field(plan, "TRACK_LIST/TRACK[3]/NAME").confidence, RECOVERED)
        self.assertEqual(self.field(plan, "AATON_CANTAR/CANTAR_KEY").confidence, OMITTED)
        self.assertEqual(self.field(plan, "FILE_SET/FAMILY_NAME").value, "23/6/    t2")
        self.assertEqual(self.field(plan, "BEXT/BWF_ORIGINATOR_REFERENCE").value, "AATON                 UB47031011")
        uid = self.field(plan, "FILE_UID")
        self.assertEqual(uid.confidence, INFERRED)
        self.assertEqual(uid.value, "AAT19JS0009202600131942407030011")  # 70900 s + 0.1 s + 60 s → 19:42:40
        self.assertEqual(self.field(plan, "HISTORY/ORIGINAL_FILENAME").confidence, INFERRED)

    def test_hand_renamed_file_disagreeing_with_bext_is_a_conflict(self) -> None:
        # Cantar-style names: "UB4703==23 6     t2==PN.WAV" — rename the
        # cut-off take by hand to 23/7 t1 while its bext still says 23/6 t2.
        for path in self.folder.glob("UB47*.WAV"):
            number = path.stem[2:]
            leaves = read_ixml_text(path, inspect_wav(path)) if path != self.target else None
            scene = take = None
            if leaves:
                root = ET.fromstring(leaves)
                scene, take = root.findtext("SCENE"), root.findtext("TAKE")
            else:
                scene, take = "23/7", "t1"
            path.rename(path.with_name(f"UB{number}=={scene.replace('/', ' ')}     {take}==PN.WAV"))
        target = next(self.folder.glob("UB4703*"))
        plan = plan_rebuild(target)
        scene, take_field = self.field(plan, "SCENE"), self.field(plan, "TAKE")
        self.assertEqual((scene.confidence, take_field.confidence), (CONFLICT, CONFLICT))
        self.assertEqual([value for value, _ in scene.options], ["23/6", "23/7", None])
        self.assertEqual([value for value, _ in take_field.options], ["t2", "t1", None])

    def test_name_agreeing_with_bext_stays_original(self) -> None:
        for path in self.folder.glob("UB47*.WAV"):
            root = ET.fromstring(read_ixml_text(path, inspect_wav(path))) if path != self.target else None
            scene, take = (root.findtext("SCENE"), root.findtext("TAKE")) if root is not None else ("23/6", "t2")
            path.rename(path.with_name(f"UB{path.stem[2:]}=={scene.replace('/', ' ')}     {take}==PN.WAV"))
        plan = plan_rebuild(next(self.folder.glob("UB4703*")))
        self.assertEqual(self.field(plan, "SCENE").confidence, ORIGINAL)

    def test_track_name_conflict_points_at_the_take_whose_channel_matches(self) -> None:
        # Channel 2 silent in the damaged take and the previous one, recording
        # in the next one: the previous take's option is marked as matching.
        import operations.ixml_rebuild as rebuild
        levels = {"UB4702.WAV": [-20.0, None, None], "UB4703.WAV": [-20.0, None, None], "UB4704.WAV": [-20.0, -18.0, None]}
        original = rebuild.channel_levels
        rebuild.channel_levels = lambda path: levels.get(path.name, [])
        try:
            plan = plan_rebuild(self.target)
        finally:
            rebuild.channel_levels = original
        conflict = self.field(plan, "TRACK_LIST/TRACK[2]/NAME")
        self.assertIn("like this file, channel 2 is silent", conflict.options[0][1])
        self.assertNotIn("like this file", conflict.options[1][1])
        self.assertEqual((plan.level("previous", 2), plan.level("target", 2), plan.level("next", 2)), ("silent", "silent", "-18 dB"))

    def test_family_name_from_neighbours_when_no_day_rule_holds(self) -> None:
        # Break the day-wide rule on one far-away take (left-aligned layout):
        # FAMILY_NAME is then learned from the takes either side.
        far = self.folder / "UB4700.WAV"
        data = far.read_bytes().replace(b"<FAMILY_NAME>23/5/    t1</FAMILY_NAME>", b"<FAMILY_NAME>23/5/t1    </FAMILY_NAME>")
        far.write_bytes(data)
        family = self.field(plan_rebuild(self.target), "FILE_SET/FAMILY_NAME")
        self.assertEqual((family.value, family.confidence), ("23/6/    t2", RECOVERED))
        self.assertIn("previous and next takes", family.source)

    def test_user_override_of_any_field_is_written(self) -> None:
        plan = plan_rebuild(self.target)
        body = render_ixml(plan, {"PROJECT": "OTHER", "AATON_CANTAR/CANTAR_KEY": "typed", "TRACK_LIST/TRACK[2]/NAME": "WU"}, "note")
        leaves = leaf_values(ET.fromstring(body.rstrip(b"\x00")))
        self.assertEqual((leaves["PROJECT"], leaves["AATON_CANTAR/CANTAR_KEY"]), ("OTHER", "typed"))

    def test_same_recording_copy_is_not_a_neighbour(self) -> None:
        copy = self.folder / "UB4703_repaired.WAV"
        write_take(self.folder, 4703, "23/6", "t2", 70900, ["BOOM", "X", ""], "kx").rename(copy)
        self.target = write_take(self.folder, 4703, "23/6", "t2", 70900, ["BOOM", "?", "?"], "k3", cut_off=True)
        plan = plan_rebuild(self.target)
        self.assertNotIn(copy, [take.path for take in (plan.previous, plan.following)])
        self.assertEqual(plan.related_count, 4)

    def test_only_one_neighbour_makes_varying_fields_inferred(self) -> None:
        (self.folder / "UB4704.WAV").unlink()
        plan = plan_rebuild(self.target)
        self.assertIsNone(plan.following)
        self.assertEqual(self.field(plan, "TRACK_LIST/TRACK[2]/NAME").confidence, RECOVERED)  # now identical in all remaining
        self.assertEqual(self.field(plan, "TAKE").confidence, ORIGINAL)

    def test_repeated_blocks_keep_their_own_values(self) -> None:
        plan = plan_rebuild(self.target)
        body = render_ixml(plan, {"TRACK_LIST/TRACK[2]/NAME": "WU"}, "note")
        root = ET.fromstring(body.rstrip(b"\x00"))
        names = [element.findtext("NAME") for element in root.find("AATON_CANTAR/ALL_TRK_NAME")]
        self.assertEqual(names, ["Trk 1", "Trk 2", "Trk 3", "Trk 4"])

    def test_render_applies_choices_padding_and_history(self) -> None:
        plan = plan_rebuild(self.target)
        body = render_ixml(plan, {"TRACK_LIST/TRACK[2]/NAME": "Clerk", "FILE_UID": None}, history_note(plan, "test"))
        self.assertEqual(len(body), RESERVED)
        self.assertTrue(body.startswith(b"<?xml version='1.0' encoding='UTF-8'?>"))
        root = ET.fromstring(body.rstrip(b"\x00"))
        leaves = leaf_values(root)
        self.assertEqual(leaves["TRACK_LIST/TRACK[2]/NAME"], "Clerk")
        # "Left empty" keeps the element, empty (never removes it)
        self.assertEqual(leaves["FILE_UID"], "")
        self.assertEqual(leaves["AATON_CANTAR/CANTAR_KEY"], "")
        self.assertIn("rebuilt", find_element(root, "HISTORY/WAVE_REPAIR_NOTE").text)

    def test_left_empty_track_name_keeps_every_track_complete(self) -> None:
        # Regression: leaving a conflicting track name out removed that
        # TRACK's NAME, and empty names were written "<NAME/>" — a real reader
        # then picked up another track's name for every empty track.
        plan = plan_rebuild(self.target)
        body = render_ixml(plan, {"TRACK_LIST/TRACK[2]/NAME": None}, "note").rstrip(b"\x00").decode()
        tracks = re.findall(r"<TRACK>(.*?)</TRACK>", body, re.S)
        self.assertEqual(len(tracks), 3)
        self.assertTrue(all("<NAME>" in track and "</NAME>" in track for track in tracks))
        self.assertNotRegex(body, r"<NAME\s*/>")

    def test_padding_uses_the_templates_own_filler_byte(self) -> None:
        # Real Cantar pads its reserved iXML space with spaces; REAPER rejects
        # an iXML followed by NULs. The rebuilt one must pad like the template.
        for path in self.folder.glob("UB47*.WAV"):
            if path == self.target:
                continue
            data = bytearray(path.read_bytes())
            start = data.rindex(b"<?xml")
            end = data.index(b"</BWFXML>", start) + len(b"</BWFXML>")
            data[end:start + RESERVED] = b" " * (start + RESERVED - end)
            path.write_bytes(bytes(data))
        body = render_ixml(plan_rebuild(self.target), {}, "note")
        self.assertEqual(len(body), RESERVED)
        self.assertNotIn(b"\x00", body)
        self.assertTrue(body.endswith(b" "))

    def test_nul_padded_template_keeps_nul_padding(self) -> None:
        body = render_ixml(plan_rebuild(self.target), {}, "note")
        self.assertTrue(body.endswith(b"\x00"))

    def test_too_big_for_the_reserved_space_is_refused(self) -> None:
        plan = plan_rebuild(self.target)
        with self.assertRaises(ValueError):
            render_ixml(plan, {}, "x" * RESERVED)

    def test_no_related_takes(self) -> None:
        for path in self.folder.glob("UB470[0-24].WAV"):
            path.unlink()
        self.assertIn("no related takes", plan_rebuild(self.target).problem)


class WriteTests(RebuildTestCase):
    def test_rebuild_writes_only_after_the_original_bytes(self) -> None:
        report = inspect_wav(self.target)
        original = self.target.read_bytes()
        plan = plan_rebuild(self.target, report)
        payload = render_ixml(plan, {"TRACK_LIST/TRACK[2]/NAME": "WU"}, history_note(plan, "test"))
        backups = self.folder.parents[1] / "backups"
        run_folder, results = run_rebuild(report, payload, backups, self.folder.parent, "test", ["sources: UB4702 / UB4704"])
        self.assertEqual(results[0].status, "repaired", results[0].message)
        repaired = self.target.read_bytes()
        self.assertEqual(repaired[: len(original)], original)
        self.assertEqual(repaired[len(original):], payload)
        after = inspect_wav(self.target)
        self.assertEqual(after.issues, [])
        self.assertEqual(after.ixml["scene"], "23/6")
        leaves = leaf_values(ET.fromstring(read_ixml_text(self.target, after)))
        self.assertEqual(leaves["TRACK_LIST/TRACK[2]/NAME"], "WU")
        self.assertEqual(leaves["AATON_CANTAR/ALL_TRK_NAME/DATA[1]/NAME"], "Trk 1")
        backup = run_folder / REBUILD_CATEGORY / "2026-01-13.AAN" / "UB4703.WAV"
        self.assertEqual(backup.read_bytes(), original)

    def test_several_rebuilds_share_one_run_folder(self) -> None:
        from operations.riff_repair import run_rebuilds
        second = write_take(self.folder, 4705, "23/6", "t4", 71500, ["BOOM", "?", "?"], "k5", cut_off=True)
        items = []
        for target in (self.target, second):
            report = inspect_wav(target)
            plan = plan_rebuild(target, report)
            choices = {item.path: None for item in plan.fields if item.confidence == CONFLICT}
            items.append((report, render_ixml(plan, choices, "note"), ["test"]))
        finished = []
        run_folder, results = run_rebuilds(items, self.folder.parents[1] / "backups", self.folder.parent, "test", finished=lambda i, n, r: finished.append((i, n, r.status)))
        self.assertEqual([result.status for result in results], ["repaired", "repaired"])
        self.assertEqual(finished, [(1, 2, "repaired"), (2, 2, "repaired")])
        backups = sorted(path.name for path in (run_folder / REBUILD_CATEGORY / "2026-01-13.AAN").iterdir())
        self.assertEqual(backups, ["UB4703.WAV", "UB4705.WAV"])
        self.assertEqual(len(list(run_folder.parent.iterdir())), 1)  # one run, not one per file

    def test_wrong_payload_size_and_ineligible_files_are_refused(self) -> None:
        report = inspect_wav(self.target)
        _run, results = run_rebuild(report, b"<BWFXML/>", self.folder.parents[1] / "b", None, "test", [])
        self.assertEqual(results[0].status, "failed")
        healthy = inspect_wav(self.folder / "UB4702.WAV")
        self.assertNotEqual(rebuild_target_problem(healthy), "")



class DraftTests(RebuildTestCase):
    def test_draft_review_state_and_final_choices(self) -> None:
        from operations.rebuild_drafts import RebuildDraft, file_signature
        plan = plan_rebuild(self.target)
        draft = RebuildDraft(file_signature(self.target))
        self.assertEqual(draft.remaining(plan), len(plan.needing_review()))
        for item in plan.needing_review():
            if item.confidence == CONFLICT:
                draft.choices[item.path] = None
            draft.confirmed.add(item.path)
        draft.choices["PROJECT"] = "OTHER"
        draft.overridden.add("PROJECT")
        self.assertTrue(draft.is_reviewed(plan))
        final = draft.final_choices(plan)
        self.assertIsNone(final["TRACK_LIST/TRACK[2]/NAME"])
        self.assertEqual(final["PROJECT"], "OTHER")
        self.assertEqual(final["FILE_UID"], self.field(plan, "FILE_UID").value)  # confirmed as proposed

    def test_store_persists_and_drops_drafts_for_changed_files(self) -> None:
        from operations.rebuild_drafts import DraftStore
        store_path = self.folder.parents[1] / "drafts.json"
        store = DraftStore(store_path)
        draft = store.get(self.target, create=True)
        draft.confirmed.add("FILE_UID")
        draft.choices["TRACK_LIST/TRACK[2]/NAME"] = None
        draft.left_to_review = 2
        store.save()
        reloaded = DraftStore(store_path).get(self.target)
        self.assertEqual(reloaded.confirmed, {"FILE_UID"})
        self.assertIsNone(reloaded.choices["TRACK_LIST/TRACK[2]/NAME"])
        self.assertEqual(reloaded.left_to_review, 2)
        with self.target.open("ab") as handle:  # the file changes on disk
            handle.write(b"\x00")
        store = DraftStore(store_path)
        self.assertIsNone(store.get(self.target))
        self.assertNotIn(str(self.target), DraftStore(store_path).drafts)


if __name__ == "__main__":
    unittest.main()
