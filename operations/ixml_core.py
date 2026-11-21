from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional


def first_track_list(root: ET.Element) -> ET.Element:
    track_list = root.find(".//TRACK_LIST")
    if track_list is not None:
        return track_list
    return ET.SubElement(root, "TRACK_LIST")


def parse_tracks(ixml_text: str) -> tuple[ET.Element, list[ET.Element]]:
    root = ET.fromstring(ixml_text)
    track_list = first_track_list(root)
    return root, list(track_list.findall("TRACK"))


def text_int(element: ET.Element, tag: str) -> Optional[int]:
    text = (element.findtext(tag) or "").strip()
    return int(text) if text.isdigit() and int(text) > 0 else None


def set_text(element: ET.Element, tag: str, value: str) -> None:
    child = element.find(tag)
    if child is None:
        child = ET.SubElement(element, tag)
    child.text = value


def extract_mono_ixml(ixml_text: str, track_index_1based: int, fallback_channel: Optional[int] = None, strict: bool = False) -> tuple[str, int]:
    """Extracts one TRACK from a source's iXML TRACK_LIST (1-based
    `track_index_1based`) and rebuilds it as a standalone single-track
    iXML document — the shared shape every "give me this one channel's
    iXML as its own mono document" caller needs, whether that channel is
    one of several in a Poly source or the only one a mono source has.

    `fallback_channel` is what a missing/absent CHANNEL_INDEX falls back
    to; it defaults to `track_index_1based` itself (Split's own
    convention: no real Trk#, so number it by file order instead), but a
    caller pulling a mono source's one-and-only track (always at index 1
    regardless of what channel number that whole file represents) passes
    its own fallback explicitly.

    `strict=True` raises instead of falling back — for a caller where a
    missing Trk# means something more consequential than "this file's
    metadata is incomplete": Re-embed's Poly-source case and QC's
    Poly-to-Split metadata fix both use a specific channel's real
    CHANNEL_INDEX to confirm a target file really is the stream they
    matched it to by filename; silently guessing there risks writing one
    channel's real metadata (e.g. a mic name) onto the wrong file with no
    warning. Split has no such pairing to confirm — it's naming and
    tagging a channel entirely by its own file-order position — so it
    stays lenient."""
    if fallback_channel is None:
        fallback_channel = track_index_1based
    root, tracks = parse_tracks(ixml_text)
    if strict and track_index_1based > len(tracks):
        raise RuntimeError(f"source iXML has no TRACK slot {track_index_1based}")
    track = copy.deepcopy(tracks[track_index_1based - 1]) if track_index_1based <= len(tracks) else ET.Element("TRACK")
    trk_number = text_int(track, "CHANNEL_INDEX")
    if trk_number is None:
        if strict:
            raise RuntimeError(f"TRACK slot {track_index_1based} is missing Trk# CHANNEL_INDEX")
        trk_number = fallback_channel
    set_text(track, "CHANNEL_INDEX", str(trk_number))
    set_text(track, "INTERLEAVE_INDEX", "1")
    if track.find("NAME") is None:
        set_text(track, "NAME", "")
    track_list = first_track_list(root)
    for child in list(track_list):
        track_list.remove(child)
    ET.SubElement(track_list, "TRACK_COUNT").text = "1"
    track_list.append(track)
    return ET.tostring(root, encoding="unicode", xml_declaration=True), trk_number


def track_from_mono_ixml(ixml_text: str, fallback_channel: int, interleave_index: int, parsed_root: Optional[ET.Element] = None) -> tuple[ET.Element, int]:
    if parsed_root is None:
        _root, tracks = parse_tracks(ixml_text)
    else:
        tracks = list(first_track_list(parsed_root).findall("TRACK"))
    track = copy.deepcopy(tracks[0]) if tracks else ET.Element("TRACK")
    trk_number = text_int(track, "CHANNEL_INDEX") or fallback_channel
    set_text(track, "CHANNEL_INDEX", str(trk_number))
    set_text(track, "INTERLEAVE_INDEX", str(interleave_index))
    if track.find("NAME") is None:
        set_text(track, "NAME", "")
    return track, trk_number


def parse_source_ixml(ixml_text: str, source: Path) -> ET.Element:
    parsing_copy = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", ixml_text)
    try:
        return ET.fromstring(parsing_copy)
    except ET.ParseError as exc:
        line, column = exc.position
        lines = parsing_copy.splitlines()
        line_text = lines[line - 1] if 0 < line <= len(lines) else ""
        tag_matches = list(re.finditer(r"<([A-Za-z_][\w:.-]*)[^>]*>", line_text[:column]))
        section = f" <{tag_matches[-1].group(1)}>" if tag_matches else ""
        invalid = line_text[column:column + 1]
        codepoint = f" near U+{ord(invalid):04X}" if invalid and ord(invalid) < 0x20 else ""
        raise RuntimeError(f"iXML parse failed for source {source} at{section}, line {line}, column {column}{codepoint}: {exc}") from exc


