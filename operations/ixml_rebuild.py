"""Phase 3: rebuild a missing iXML from the file itself and its related takes.

Every leaf of the rebuilt document gets a provenance:

  original   — read from this file's own bext / fmt / filename (or derived
               from them by a rule that reproduces every related file's
               real value);
  recovered  — identical in every related file that day, or the previous
               and next takes agree;
  inferred   — only one neighbouring take to go by, or an estimate;
  conflict   — the previous and next takes disagree: a human must choose;
  omitted    — unique to every file and not derivable (e.g. a recorder's
               per-file key): copying a neighbour's value would be wrong,
               so it is written empty (the element stays, as in the
               recorder's own files).

Original/recovered are accepted automatically; inferred and conflict need
explicit confirmation in the UI before anything is written. The structure
comes from the nearest related take (same recorder), so the result looks
exactly like that recorder's own iXML. Nothing here writes to a file.
"""
from __future__ import annotations

import datetime as dt
import re
import statistics
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from operations.audio_qc import compute_wav_channel_peaks
from operations.riff_inspect import InspectionReport, inspect_wav
from operations.timecode import parse_speed_field

ORIGINAL, RECOVERED, INFERRED, CONFLICT, OMITTED = "original", "recovered", "inferred", "conflict", "omitted"
NEEDS_REVIEW = (INFERRED, CONFLICT)
CONFIDENCE_LABELS = {ORIGINAL: "🟢 Original", RECOVERED: "🔵 Recovered", INFERRED: "🟡 Inferred", CONFLICT: "🔴 Conflict", OMITTED: "⚪ Omitted"}


@dataclass
class RelatedTake:
    path: Path
    report: InspectionReport
    leaves: dict[str, str]

    @property
    def time_reference(self) -> int:
        return int(self.report.bext.get("time_reference") or 0)


@dataclass
class RebuiltField:
    path: str
    value: Optional[str]
    confidence: str
    source: str
    options: list[tuple[Optional[str], str]] = field(default_factory=list)  # (value, source) choices for review


@dataclass
class RebuildPlan:
    target: Path
    report: InspectionReport
    template: Optional[RelatedTake] = None
    previous: Optional[RelatedTake] = None
    following: Optional[RelatedTake] = None
    related_count: int = 0
    fields: list[RebuiltField] = field(default_factory=list)
    problem: str = ""
    # Per-channel peak level in dBFS (None = silent) for the previous take,
    # this file and the next take — evidence for track-name decisions.
    levels: dict[str, list[Optional[float]]] = field(default_factory=dict)

    def channel_for(self, path: str) -> Optional[int]:
        """The 1-based audio channel a TRACK_LIST/TRACK[n]/… row describes."""
        match = re.match(r"TRACK_LIST/TRACK\[(\d+)\]/", path)
        if match is None or self.template is None:
            return None
        interleave = self.template.leaves.get(f"TRACK_LIST/TRACK[{match.group(1)}]/INTERLEAVE_INDEX", "")
        return int(interleave) if interleave.strip().isdigit() else int(match.group(1))

    def level(self, which: str, channel: Optional[int]) -> str:
        values = self.levels.get(which)
        if channel is None or not values or channel > len(values):
            return "—"
        value = values[channel - 1]
        return "silent" if value is None else f"{value:.0f} dB"

    @property
    def reserved_size(self) -> int:
        chunk = self.report.chunks[-1] if self.report.chunks else None
        return chunk.declared_size if chunk is not None and chunk.chunk_id == "iXML" else 0

    @property
    def ok(self) -> bool:
        return not self.problem

    def needing_review(self) -> list[RebuiltField]:
        return [item for item in self.fields if item.confidence in NEEDS_REVIEW]


# --- reading -----------------------------------------------------------------

def read_ixml_text(path: Path, report: InspectionReport) -> Optional[str]:
    chunk = report.chunk("iXML")
    if chunk is None or chunk.size <= 0 or chunk.size < chunk.declared_size:
        return None
    with path.open("rb") as handle:
        handle.seek(chunk.data_offset)
        raw = handle.read(chunk.size)
    return raw.rstrip(b"\x00 \t\r\n").decode("utf-8", errors="replace") or None


def leaf_values(element: ET.Element, prefix: str = "") -> dict[str, str]:
    """Flatten an iXML tree to {path: text}. Every tag that repeats among
    its siblings is numbered — TRACK_LIST/TRACK[3]/NAME, and recorder blocks
    like Cantar's 24 ALL_TRK_NAME/DATA[n] — so the same entry lines up
    across files; TRACK is always numbered, even when there is only one.
    (Numbering only TRACK once let 24 DATA entries collapse onto one path,
    and a rebuild overwrote the first entry with the last.)"""
    values: dict[str, str] = {}
    totals: dict[str, int] = {}
    for child in element:
        totals[child.tag] = totals.get(child.tag, 0) + 1
    counts: dict[str, int] = {}
    for child in element:
        counts[child.tag] = counts.get(child.tag, 0) + 1
        tag = f"{child.tag}[{counts[child.tag]}]" if child.tag == "TRACK" or totals[child.tag] > 1 else child.tag
        path = f"{prefix}/{tag}" if prefix else tag
        if len(child):
            values.update(leaf_values(child, path))
        else:
            values[path] = child.text or ""
    return values


def find_element(root: ET.Element, path: str) -> Optional[ET.Element]:
    element = root
    for part in path.split("/"):
        match = re.fullmatch(r"(.+)\[(\d+)\]", part)
        if match:
            found = [child for child in element if child.tag == match.group(1)]
            index = int(match.group(2)) - 1
            element = found[index] if index < len(found) else None
        else:
            element = element.find(part)
        if element is None:
            return None
    return element


def description_field(report: InspectionReport, name: str, raw: bool = False) -> Optional[str]:
    """A recorder key from bext Description regardless of its one-letter
    prefix (Aaton `aSCENE=`, Sound Devices `sSCENE=`, …)."""
    for line in re.split(r"\r\n|\n|\r", str(report.bext.get("description", ""))):
        match = re.match(r"^[A-Za-z]([A-Z][A-Z0-9_]*)=(.*)$", line)
        if match and match.group(1) == name:
            return match.group(2) if raw else match.group(2).strip()
    return None


def channel_levels(path: Path) -> list[Optional[float]]:
    """Peak level per channel in dBFS, sampled across the whole file (None
    when no sample shows any signal) — enough to tell which channels were
    actually recording."""
    import math
    try:
        peaks = compute_wav_channel_peaks(path, bucket_count=600)
    except Exception:
        return []
    return [20 * math.log10(max(lane)) if lane and max(lane) > 0 else None for lane in peaks]


def related_takes(target: Path, report: InspectionReport, candidates: Optional[list[Path]] = None) -> list[RelatedTake]:
    """Other files in the same folder (same day / same tape on a recorder
    card) from the same recorder with a readable iXML and the same channel
    count, ordered by timecode."""
    originator = report.bext.get("originator")
    channels = report.audio.get("channels")
    paths = candidates if candidates is not None else sorted(target.parent.glob("*"))
    takes = []
    for path in paths:
        if path == target or path.name.startswith(".") or path.suffix.lower() != ".wav" or not path.is_file():
            continue
        other = inspect_wav(path)
        if other.bext.get("originator") != originator or other.audio.get("channels") != channels:
            continue
        if other.bext.get("time_reference") == report.bext.get("time_reference"):
            continue  # the same recording (e.g. a hand-made "_repaired" copy) is no neighbour
        text = read_ixml_text(path, other)
        if text is None:
            continue
        try:
            leaves = leaf_values(ET.fromstring(text))
        except ET.ParseError:
            continue
        takes.append(RelatedTake(path, other, leaves))
    return sorted(takes, key=lambda take: take.time_reference)


# --- own-file values -----------------------------------------------------------

def _timecode_rate(report: InspectionReport) -> Optional[tuple[str, str]]:
    parsed = parse_speed_field(description_field(report, "SPEED") or "")
    if parsed is None:
        return None
    fps, drop = parsed
    rate = f"{round(fps)}/1" if abs(fps - round(fps)) < 0.001 else f"{round(fps) * 1000}/1001"
    return rate, "DF" if drop else "NDF"


def own_values(target: Path, report: InspectionReport) -> dict[str, tuple[str, str]]:
    """{iXML path: (value, where it came from)} for everything this file
    still carries itself."""
    bext, audio = report.bext, report.audio
    values: dict[str, tuple[str, str]] = {}

    def put(path: str, value: Optional[object], source: str) -> None:
        if value is not None and value != "":
            values[path] = (str(value), source)

    for name in ("SCENE", "TAKE", "TAPE"):
        put(name, description_field(report, name), f"bext Description ({name})")
    ubits = description_field(report, "UBITS")
    put("UBITS", ubits.lstrip("$") if ubits else None, "bext Description (UBITS)")
    note = description_field(report, "NOTE", raw=True)
    if note is not None:
        values["NOTE"] = (note, "bext Description (NOTE)")
    rate = _timecode_rate(report)
    if rate:
        put("SPEED/TIMECODE_RATE", rate[0], "bext Description (SPEED)")
        put("SPEED/TIMECODE_FLAG", rate[1], "bext Description (SPEED)")
    time_reference = bext.get("time_reference")
    if isinstance(time_reference, int):
        put("SPEED/TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI", time_reference >> 32, "bext TimeReference")
        put("SPEED/TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO", time_reference & 0xFFFFFFFF, "bext TimeReference")
        put("BEXT/BWF_TIME_REFERENCE_HIGH", time_reference >> 32, "bext TimeReference")
        put("BEXT/BWF_TIME_REFERENCE_LOW", time_reference & 0xFFFFFFFF, "bext TimeReference")
    # Not mirrored from bext: on real Cantar files the iXML BEXT section's
    # ORIGINATOR (padded), ORIGINATOR_REFERENCE (different suffix) and
    # VERSION differ from the bext chunk itself — those are handled by the
    # generic comparison / the reference rule instead.
    for path in ("SPEED/FILE_SAMPLE_RATE", "SPEED/TIMESTAMP_SAMPLE_RATE"):
        put(path, audio.get("sample_rate"), "fmt (sample rate)")
    put("SPEED/AUDIO_BIT_DEPTH", audio.get("bits_per_sample"), "fmt (bit depth)")
    put("TRACK_LIST/TRACK_COUNT", audio.get("channels"), "fmt (channels)")
    put("BEXT/BWF_DESCRIPTION", bext.get("description"), "bext Description")
    put("BEXT/BWF_ORIGINATION_DATE", bext.get("origination_date"), "bext OriginationDate")
    put("BEXT/BWF_ORIGINATION_TIME", bext.get("origination_time"), "bext OriginationTime")
    put("BEXT/BWF_CODING_HISTORY", bext.get("coding_history"), "bext CodingHistory")
    return values


def _same(a: Optional[str], b: Optional[str]) -> bool:
    """Recorders pad names inconsistently ("WU" vs "WU  ", "" vs " "), so
    agreement ignores leading/trailing whitespace."""
    return (a or "").strip() == (b or "").strip() and (a is None) == (b is None)


def _distinct(values: list[Optional[str]]) -> int:
    return len({(value or "").strip() if value is not None else None for value in values})


# --- rules verified against the related takes ----------------------------------

def _reference_kind(report: InspectionReport) -> str:
    """The shape of a bext OriginatorReference with digits masked. A take
    whose metadata was edited on the recorder afterwards gets a rewritten
    bext in a different shape (Cantar: "AATON  UB46820000" becomes
    "AATCAN00519…0002") and its iXML follows different patterns too, so
    rules are only learned from takes of the same kind as the target."""
    return re.sub(r"\d+", "#", str(report.bext.get("originator_reference", "")))


def same_kind(related: list[RelatedTake], report: InspectionReport) -> list[RelatedTake]:
    kind = _reference_kind(report)
    return [take for take in related if _reference_kind(take.report) == kind]


def _family_name_rule(related: list[RelatedTake]) -> Optional[tuple[str, int]]:
    """Cantar's FAMILY_NAME is "<SCENE>/<TAKE padded to a width>" — padded
    on the left on some days, on the right on others. Returns (alignment,
    width) only if one rule reproduces every related take exactly."""
    for align in ("right", "left"):
        widths = set()
        for take in related:
            family = take.leaves.get("FILE_SET/FAMILY_NAME")
            scene, take_name = take.leaves.get("SCENE"), (take.leaves.get("TAKE") or "").strip()
            if family is None or scene is None or not family.startswith(scene + "/"):
                return None
            tail = family[len(scene) + 1:]
            if tail != (take_name.rjust(len(tail)) if align == "right" else take_name.ljust(len(tail))):
                break
            widths.add(len(tail))
        else:
            if len(widths) == 1:
                return align, widths.pop()
    return None


def _reference_suffix_rule(related: list[RelatedTake]) -> Optional[str]:
    """The iXML BWF_ORIGINATOR_REFERENCE is the bext OriginatorReference
    with its last 4 characters replaced by a fixed suffix (Cantar: bext
    "...UB46730000", iXML "...UB46731011"). Returns that suffix if it holds
    for every related take."""
    suffixes = set()
    for take in related:
        ixml_value = take.leaves.get("BEXT/BWF_ORIGINATOR_REFERENCE")
        bext_value = str(take.report.bext.get("originator_reference", ""))
        if not ixml_value or len(bext_value) < 4 or ixml_value.strip()[:-4] != bext_value[:-4]:
            return None
        suffixes.add(ixml_value.strip()[-4:])
    return suffixes.pop() if len(suffixes) == 1 else None


_UID = re.compile(r"^(?P<prefix>.*?)(?P<date>\d{8})(?P<time>\d{6})(?P<serial>\d{3})(?P<suffix>\d{4})$")


def _file_number(path: Path) -> Optional[str]:
    match = re.search(r"(\d{3,})", path.stem)
    return match.group(1) if match else None


def _seconds(clock: str) -> Optional[int]:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})", clock or "")
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3)) if match else None


def _end_seconds(report: InspectionReport) -> Optional[float]:
    start = _seconds(str(report.bext.get("origination_time", "")))
    duration = report.audio.get("duration_seconds")
    return start + float(duration) if start is not None and duration is not None else None


def _start_stamped(take: RelatedTake, path_key: str) -> bool:
    """Whether this take's UID carries its *start* time rather than the
    usual close time — a second convention seen on the same real card
    (a whole block of takes, 2026-01-14 scene 37)."""
    match = _UID.match(take.leaves.get(path_key) or "")
    start = _seconds(str(take.report.bext.get("origination_time", "")))
    return bool(match) and start is not None and _seconds(f"{match.group('time')[:2]}:{match.group('time')[2:4]}:{match.group('time')[4:]}") == start


def _uid_estimate(path_key: str, target: Path, report: InspectionReport, related: list[RelatedTake], neighbours: Optional[list[RelatedTake]] = None) -> Optional[RebuiltField]:
    """Cantar's FILE_UID / FAMILY_UID = prefix + date + HHMMSS + file serial +
    suffix. Everything but the time is checked against every related take;
    the time is the recorder clock when it *closed* the file — measured as
    the file's end plus an offset, estimated from the related takes. A file
    cut off by power loss never closed, so this can only be an estimate."""
    neighbours = neighbours or []
    stamped = [_start_stamped(take, path_key) for take in neighbours]
    start_field: Optional[RebuiltField] = None
    if any(stamped):
        # The takes either side stamp their UID with the start time: so does
        # this one, and that time is in its own bext.
        match = _UID.match(neighbours[stamped.index(True)].leaves.get(path_key) or "")
        number, start = _file_number(target), _seconds(str(report.bext.get("origination_time", "")))
        if match and number and start is not None:
            value = f"{match.group('prefix')}{match.group('date')}{start // 3600:02}{start % 3600 // 60:02}{start % 60:02}{number[-3:]}{match.group('suffix')}"
            source = "this file's start time — the neighbouring take stamps its ID with the start time (not the usual close time)"
            start_field = RebuiltField(path_key, value, INFERRED, source, [(value, source), (None, "leave empty")])
            if all(stamped):
                return start_field
    close_field = _uid_close_estimate(path_key, target, report, related)
    if start_field is not None and close_field is not None:
        # One neighbour each way: the file sits where the recorder switched
        # conventions — don't guess, offer both.
        return RebuiltField(path_key, None, CONFLICT, "the takes either side stamp their IDs differently (start time vs close time)", [start_field.options[0], close_field.options[0], (None, "leave empty")])
    return start_field or close_field


def _uid_close_estimate(path_key: str, target: Path, report: InspectionReport, related: list[RelatedTake]) -> Optional[RebuiltField]:
    samples = []
    for take in related:
        if _start_stamped(take, path_key):
            continue  # the other convention: not part of the close-time drift
        match = _UID.match(take.leaves.get(path_key) or "")
        number = _file_number(take.path)
        end = _end_seconds(take.report)
        if match is None or number is None or end is None or match.group("serial") != number[-3:]:
            continue
        offset = _seconds(f"{match.group('time')[:2]}:{match.group('time')[2:4]}:{match.group('time')[4:]}") - end
        samples.append(((match.group("prefix"), match.group("date"), match.group("suffix")), end, offset))
    # Only takes in the dominant layout count (a block of takes rewritten in
    # another style on the same card mustn't block or skew the estimate),
    # and the layout must cover at least half the related takes.
    if not samples:
        return None
    layouts: dict[tuple, int] = {}
    for parts_key, _end, _offset in samples:
        layouts[parts_key] = layouts.get(parts_key, 0) + 1
    dominant, count = max(layouts.items(), key=lambda item: item[1])
    if count < 3 or count < 0.5 * len(related):
        return None
    parts = [dominant]
    starts = [end for parts_key, end, _offset in samples if parts_key == dominant]
    offsets = [offset for parts_key, _end, offset in samples if parts_key == dominant]
    number, end = _file_number(target), _end_seconds(report)
    if number is None or end is None:
        return None
    # The recorder's clock drifts against timecode through the day (one real
    # card: +108 s in the morning, +54 s by evening), so the offset is a
    # straight-line fit over the day, evaluated at this file's end.
    # Takes edited on the recorder afterwards can carry times that don't fit
    # (one real card: a take 5 minutes off the line) — fit, drop the points
    # far off the line, fit again.
    points = list(zip(starts, offsets))
    for _round in range(2):
        if len(points) < 2 or max(p[0] for p in points) == min(p[0] for p in points):
            break
        slope, intercept = statistics.linear_regression([p[0] for p in points], [p[1] for p in points])
        residuals = [abs(o - (slope * s_ + intercept)) for s_, o in points]
        limit = max(5.0, 3 * statistics.median(residuals))
        kept = [point for point, residual_ in zip(points, residuals) if residual_ <= limit]
        if len(kept) == len(points) or len(kept) < 3:
            break
        points = kept
    starts = [p[0] for p in points]
    offsets = [p[1] for p in points]
    if len(starts) >= 2 and max(starts) > min(starts):
        slope, intercept = statistics.linear_regression(starts, offsets)
        offset = slope * end + intercept
        residual = max(abs(o - (slope * s_ + intercept)) for s_, o in zip(starts, offsets))
    else:
        offset, residual = statistics.median(offsets), 0.0
    clock = int(round(end + offset)) % 86400
    prefix, date, suffix = parts[0]
    value = f"{prefix}{date}{clock // 3600:02}{clock % 3600 // 60:02}{clock % 60:02}{number[-3:]}{suffix}"
    source = f"estimate: file end + {offset:.0f} s (recorder clock drift fitted over {len(starts)} related takes, ±{residual:.0f} s) — the real value was never written"
    return RebuiltField(path_key, value, INFERRED, source, [(value, source), (None, "leave empty")])


# Filename layouts that carry scene/take. Only used once one of them is
# confirmed to reproduce the iXML SCENE/TAKE of (nearly) every related take.
FILENAME_SCENE_TAKE_PATTERNS = (
    # Cantar: "UB4731==23 6     t2==PN.WAV" -> scene "23/6", take "t2"
    (re.compile(r"==(?P<scene>.+?)\s+(?P<take>\S+)=="), lambda scene: scene.replace(" ", "/")),
)


def filename_scene_take(path: Path, related: list[RelatedTake]) -> Optional[tuple[str, str, int]]:
    """(scene, take, takes-confirming) read from this file's name, using a
    layout that reproduces the iXML SCENE/TAKE of at least 90% of the
    related takes; None if no layout is confirmed or the name doesn't fit."""
    for pattern, scene_of in FILENAME_SCENE_TAKE_PATTERNS:
        confirmed = 0
        for take in related:
            match = pattern.search(take.path.name)
            if match and scene_of(match.group("scene")) == (take.leaves.get("SCENE") or "").strip() and match.group("take") == (take.leaves.get("TAKE") or "").strip():
                confirmed += 1
        if related and confirmed >= 0.9 * len(related):
            match = pattern.search(path.name)
            return (scene_of(match.group("scene")), match.group("take"), confirmed) if match else None
    return None


# --- the plan ------------------------------------------------------------------

def plan_rebuild(target: Path, report: Optional[InspectionReport] = None, candidates: Optional[list[Path]] = None) -> RebuildPlan:
    report = report or inspect_wav(target)
    plan = RebuildPlan(target, report)
    related = related_takes(target, report, candidates)
    plan.related_count = len(related)
    if not related:
        plan.problem = "no related takes from the same recorder with a readable iXML in this folder"
        return plan
    own_time = int(report.bext.get("time_reference") or 0)
    earlier = [take for take in related if take.time_reference < own_time]
    later = [take for take in related if take.time_reference > own_time]
    plan.previous = earlier[-1] if earlier else None
    plan.following = later[0] if later else None
    plan.template = plan.previous or plan.following
    plan.levels = {"target": channel_levels(target)}
    if plan.previous:
        plan.levels["previous"] = channel_levels(plan.previous.path)
    if plan.following:
        plan.levels["next"] = channel_levels(plan.following.path)
    neighbours = [take for take in (plan.previous, plan.following) if take is not None]
    own = own_values(target, report)
    kin = same_kind(related, report)
    rules_ok = len(kin) >= 3
    family_rule = _family_name_rule(kin) if rules_ok else None
    reference_suffix = _reference_suffix_rule(kin) if rules_ok else None
    # When no rule holds for the whole day (one real card has takes whose
    # iXML is in the "edited" style even though their bext isn't), learn it
    # from just the takes either side: both agreeing → recovered, one → inferred.
    family_rule_near = _family_name_rule(neighbours) if family_rule is None and neighbours else None
    reference_suffix_near = _reference_suffix_rule(neighbours) if reference_suffix is None and neighbours else None
    near_confidence = RECOVERED if len(neighbours) == 2 else INFERRED
    near_note = "the previous and next takes" if len(neighbours) == 2 else f"the only neighbouring take ({neighbours[0].path.name})" if neighbours else ""
    kin_note = f"all {len(kin)} related takes of the same kind" + (f" ({len(related) - len(kin)} edited on the recorder afterwards left out)" if len(kin) < len(related) else "")

    # On a rushed set the scene/take is often fixed afterwards by renaming the
    # file by hand, which leaves the bext saying the old scene/take. If the
    # name disagrees with the bext, that's for a human to decide.
    from_name = filename_scene_take(target, related)
    for name_key, index in (("SCENE", 0), ("TAKE", 1)):
        if from_name is not None and name_key in own and own[name_key][0].strip() != from_name[index]:
            bext_value, bext_source = own.pop(name_key)
            name_source = f"file name (its layout matches {from_name[2]} of {len(related)} related takes)"
            plan.fields.append(RebuiltField(name_key, None, CONFLICT, f"bext says {bext_value!r}, the file name says {from_name[index]!r} — was the file renamed by hand?", [(bext_value, bext_source), (from_name[index], name_source), (None, "leave empty")]))
    handled = {item.path for item in plan.fields}

    for path, template_value in plan.template.leaves.items():
        if path in handled:
            continue
        if path in own:
            value, source = own[path]
            plan.fields.append(RebuiltField(path, value, ORIGINAL, source))
            continue
        if path == "FILE_SET/FAMILY_NAME" and (family_rule or family_rule_near) is not None and "SCENE" in own and "TAKE" in own:
            align, width = family_rule or family_rule_near
            take_name = own["TAKE"][0].strip()
            value = f"{own['SCENE'][0]}/{take_name.rjust(width) if align == 'right' else take_name.ljust(width)}"
            if family_rule is not None:
                plan.fields.append(RebuiltField(path, value, ORIGINAL, f"SCENE/TAKE by the recorder's own pattern (reproduces {kin_note})"))
            else:
                source = f"SCENE/TAKE in the same layout as {near_note}"
                plan.fields.append(RebuiltField(path, value, near_confidence, source, [(value, source), (None, "leave empty")]))
            continue
        if path == "BEXT/BWF_ORIGINATOR_REFERENCE" and (reference_suffix or reference_suffix_near) is not None and report.bext.get("originator_reference"):
            value = str(report.bext["originator_reference"])[:-4] + (reference_suffix or reference_suffix_near)
            if reference_suffix is not None:
                plan.fields.append(RebuiltField(path, value, ORIGINAL, f"bext OriginatorReference with the recorder's fixed iXML suffix (reproduces {kin_note})"))
            else:
                source = f"bext OriginatorReference with the same iXML suffix as {near_note}"
                plan.fields.append(RebuiltField(path, value, near_confidence, source, [(value, source), (None, "leave empty")]))
            continue
        if path == "HISTORY/ORIGINAL_FILENAME":
            # The name a take was first recorded under; a take renamed on the
            # recorder afterwards keeps its earlier name here (UB4730 was
            # recorded as "23 5 t3", then renamed to 23/6 t1). A rename
            # doesn't always leave a trace in the bext either (UB4701, UB4712
            # on 2026-01-13 were renamed and still look unedited), so the
            # current name is always a guess — shown with the day's actual
            # rate so it can be judged.
            pool = kin if rules_ok else related
            kept = sum(take.leaves.get(path) == take.path.name for take in pool)
            source = (
                f"current file name — {kept} of {len(pool)} comparable takes that day kept their first name here; "
                f"{len(pool) - kept} had been renamed on the recorder, which can't be detected from the file, so this can't be certain"
            )
            plan.fields.append(RebuiltField(path, target.name, INFERRED, source, [(target.name, source), (None, "leave empty")]))
            continue
        if path in ("FILE_UID", "FILE_SET/FAMILY_UID"):
            estimate = _uid_estimate(path, target, report, kin if rules_ok else related, neighbours)
            if estimate is not None:
                plan.fields.append(estimate)
                continue
        values = [take.leaves.get(path) for take in related]
        if re.search(r"(KEY|KEY_ID|UID)$", path.rsplit("/", 1)[-1]) and _distinct(values) > 1:
            # A per-file identity (a recorder key or ID) with no rule to
            # derive it: copying another take's would give two files one ID.
            plan.fields.append(RebuiltField(path, None, OMITTED, "a per-file identifier with no rule to derive it — not copied from another take"))
            continue
        if _distinct(values) == 1:
            plan.fields.append(RebuiltField(path, template_value, RECOVERED, f"identical in all {len(related)} related takes"))
            continue
        if len(related) > 2 and _distinct(values) >= 0.8 * len(values):
            plan.fields.append(RebuiltField(path, None, OMITTED, f"different in nearly every file ({_distinct(values)} values across {len(related)} takes) and not derivable — not copied from another take"))
            continue
        before = plan.previous.leaves.get(path) if plan.previous else None
        after = plan.following.leaves.get(path) if plan.following else None
        if plan.previous and plan.following:
            if _same(before, after):
                plan.fields.append(RebuiltField(path, before, RECOVERED, f"previous and next takes agree ({plan.previous.path.name} / {plan.following.path.name})"))
            else:
                before_source, after_source = f"previous take {plan.previous.path.name}", f"next take {plan.following.path.name}"
                channel = plan.channel_for(path)
                hint = ""
                if channel is not None and path.endswith("/NAME"):
                    states = {which: plan.level(which, channel) == "silent" for which in ("previous", "target", "next")}
                    if states["previous"] != states["next"]:
                        matching = "previous" if states["previous"] == states["target"] else "next"
                        note = f" — like this file, channel {channel} is {'silent' if states['target'] else 'recording'} there"
                        if matching == "previous":
                            before_source += note
                        else:
                            after_source += note
                        hint = f"; this file's channel {channel} is {'silent' if states['target'] else 'recording'}, like the {matching} take"
                options = [(before, before_source), (after, after_source), (None, "leave empty")]
                plan.fields.append(RebuiltField(path, None, CONFLICT, "previous and next takes differ" + hint, options))
        else:
            neighbour = plan.previous or plan.following
            value = neighbour.leaves.get(path)
            source = f"{'previous' if plan.previous else 'next'} take {neighbour.path.name} (the only neighbouring take)"
            plan.fields.append(RebuiltField(path, value, INFERRED, source, [(value, source), (None, "leave empty")]))
    return plan


def padding_byte(path: Path, report: InspectionReport) -> bytes:
    """The byte the recorder fills its reserved iXML space with, read from
    the template's own chunk. Real Cantar cards fill with spaces (0x20), not
    NULs: an iXML padded with NULs reads fine in BWF MetaEdit but REAPER
    rejects the whole document — SCENE, TAKE, PROJECT, TAPE and track names
    all came back empty in REAPER for a rebuilt UB4783 padded with NULs."""
    chunk = report.chunk("iXML")
    if chunk is None or chunk.size <= 0:
        return b" "
    with path.open("rb") as handle:
        handle.seek(chunk.data_offset + chunk.size - 1)
        last = handle.read(1)
    return last if last in (b" ", b"\x00", b"\n") else b" "


def serialize_like(root: ET.Element, template_text: str) -> bytes:
    """Serialize `root` exactly the way the recorder wrote `template_text`:
    its XML declaration line verbatim, its own line breaks and indentation
    (kept as parsed — nothing is re-indented), and empty elements in the
    recorder's form. Real Cantar cards write an empty track name as
    "<NAME></NAME>"; writing "<NAME/>" (equally valid XML) made a real
    reader fall through to the next name, so every empty track came out
    named "CMIT 5U". An empty element is self-closed only for tags the
    template itself only ever self-closes."""
    body = ET.tostring(root, encoding="utf-8", short_empty_elements=False)
    self_closed: dict[str, str] = {}
    for tag, space in re.findall(r"<([A-Za-z_][\w.-]*)(\s*)/>", template_text):
        self_closed.setdefault(tag, f"<{tag}{space}/>")  # "<X/>" or "<X />", as the recorder wrote it
    paired_empty = set(re.findall(r"<([A-Za-z_][\w.-]*)></\1>", template_text))
    for tag, form in self_closed.items():
        if tag not in paired_empty:
            body = body.replace(f"<{tag}></{tag}>".encode(), form.encode())
    # Everything before the root element (declaration, its line break or
    # none — one real file has "<?xml …?><BWFXML>" on one line) verbatim.
    start = template_text.find("<" + root.tag)
    prefix = template_text[:start] if start > 0 else '<?xml version="1.0" encoding="UTF-8"?>\n'
    return prefix.encode("utf-8") + body


def render_ixml(plan: RebuildPlan, choices: dict[str, Optional[str]], history_note: str) -> bytes:
    """The rebuilt document: the template's structure with every field set to
    its accepted value (`choices` overrides review fields; None removes the
    element), plus a provenance note in HISTORY. Padded with NULs to exactly
    the reserved size with the same filler byte the recorder uses."""
    assert plan.template is not None
    root = ET.fromstring(read_ixml_text(plan.template.path, plan.template.report) or "")
    for item in plan.fields:
        value = choices[item.path] if item.path in choices else item.value
        element = find_element(root, item.path)
        if element is None:
            continue
        # "Left empty" keeps the element, empty — never removes it. Removing
        # a track's NAME left that TRACK without one, and readers that walk
        # the track list expect every TRACK to have the same fields.
        element.text = "" if value is None else value
    history = root.find("HISTORY")
    if history is None:
        history = ET.SubElement(root, "HISTORY")
    note = ET.SubElement(history, "WAVE_REPAIR_NOTE")
    note.text = history_note
    # Match the indentation of HISTORY's existing children.
    siblings = list(history)
    if len(siblings) > 1:
        note.tail = siblings[-2].tail
        siblings[-2].tail = siblings[0].tail if len(siblings) > 2 else (history.text or "\n")
    template_text = read_ixml_text(plan.template.path, plan.template.report) or ""
    body = serialize_like(root, template_text)
    trailing = template_text[len(template_text.rstrip()):] if template_text else ""
    body += trailing.encode() if trailing else b"\n"
    if plan.reserved_size and len(body) > plan.reserved_size:
        raise ValueError(f"rebuilt iXML is {len(body):,} bytes, more than the {plan.reserved_size:,} bytes reserved for it")
    return body + padding_byte(plan.template.path, plan.template.report) * (plan.reserved_size - len(body)) if plan.reserved_size else body


def history_note(plan: RebuildPlan, app_label: str, when: Optional[dt.datetime] = None) -> str:
    stamp = (when or dt.datetime.now()).strftime("%Y-%m-%d %H:%M")
    sources = ", ".join(take.path.name for take in (plan.previous, plan.following) if take is not None)
    counts = {label: sum(item.confidence == key for item in plan.fields) for key, label in ((INFERRED, "inferred"), (CONFLICT, "chosen"), (OMITTED, "omitted"))}
    return (
        f"iXML rebuilt by {app_label} on {stamp}: the recorder never wrote this file's iXML. "
        f"Own values from this file's bext/fmt/name; others from related takes ({sources}); "
        f"{counts['inferred']} inferred, {counts['chosen']} chosen by the user, {counts['omitted']} omitted. Details in the Wave Repair log."
    )
