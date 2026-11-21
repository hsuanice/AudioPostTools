from __future__ import annotations

import re
from typing import Optional

from operations.filename_formula import compile_filename_pattern


DEFAULT_SPEAKER_PATTERN = "{base}.$"
SPEAKER_CANONICAL_CHANNELS = ("L", "R", "C", "Lc", "Rc", "LFE", "Lfh", "Rfh", "S", "Cs", "Ls", "Rs", "Lss", "Rss", "Lsr", "Rsr", "Lrs", "Rrs", "Lscr", "Rscr", "Lsd", "Rsd", "Lb", "Rb", "Lvh", "Cvh", "Rvh", "Lw", "Rw", "Tbl", "Tbr", "Tfl", "Tfr", "Tl", "Tr", "Ts", "Ltm", "Rtm", "Lts", "Rts", "SW")
SPEAKER_LAYOUT_ORDERS = {
    ("Stereo", "SMPTE"): ("L", "R"),
    ("Stereo", "FILM"): ("L", "R"),
    ("2.1", "SMPTE"): ("L", "R", "LFE"),
    ("2.1", "FILM"): ("L", "R", "LFE"),
    ("3.0", "SMPTE"): ("L", "R", "C"),
    ("3.0", "FILM"): ("L", "C", "R"),
    ("3.1", "SMPTE"): ("L", "R", "C", "LFE"),
    ("3.1", "FILM"): ("L", "C", "R", "LFE"),
    ("5.1", "SMPTE"): ("L", "R", "C", "LFE", "Ls", "Rs"),
    ("5.1", "FILM"): ("L", "C", "R", "Ls", "Rs", "LFE"),
    ("7.1", "SMPTE"): ("L", "R", "C", "LFE", "Lss", "Rss", "Lsr", "Rsr"),
    ("7.1", "FILM"): ("L", "C", "R", "Lss", "Rss", "Lsr", "Rsr", "LFE"),
    ("7.1.2", "DOLBY ATMOS"): ("L", "R", "C", "LFE", "Ls", "Rs", "Lrs", "Rrs", "Ltm", "Rtm"),
}
SPEAKER_MASK_BITS = {"L": 0x00000001, "R": 0x00000002, "C": 0x00000004, "LFE": 0x00000008, "Lb": 0x00000010, "Rb": 0x00000020, "Ls": 0x00000200, "Rs": 0x00000400}


def speaker_filename_pattern(pattern: str) -> re.Pattern[str]:
    pattern = pattern.strip() or DEFAULT_SPEAKER_PATTERN
    return compile_filename_pattern(pattern, "{base}", "$", "base", "token", r"[^.]+", "Speaker filename pattern")


def parse_speaker_mapping(mapping_text: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for line in mapping_text.splitlines():
        if not line.strip():
            continue
        if "=" not in line:
            raise ValueError(f"Invalid speaker mapping: {line}")
        token, canonical = (part.strip() for part in line.split("=", 1))
        if not token or canonical not in SPEAKER_CANONICAL_CHANNELS:
            raise ValueError(f"Invalid speaker mapping: {line}")
        mapping[token.casefold()] = canonical
    return mapping


def speaker_order(layout_name: str, order_name: str, channels: Optional[set[str]] = None) -> tuple[str, ...]:
    if channels is None:
        order = SPEAKER_LAYOUT_ORDERS.get((layout_name, order_name), ())
    else:
        detected = [(layout, order_format, order) for (layout, order_format), order in SPEAKER_LAYOUT_ORDERS.items() if set(order) == channels]
        if any(layout == "7.1.2" for layout, _order_format, _order in detected):
            order = next((order for layout, order_format, order in detected if layout == "7.1.2" and order_format == "DOLBY ATMOS"), ())
        else:
            order = next((order for _layout, order_format, order in detected if order_format == order_name), ())
    if not order:
        raise ValueError("Channels do not match a supported source configuration / order.")
    return order


def speaker_channel_mask(order: tuple[str, ...]) -> int:
    return sum(SPEAKER_MASK_BITS[channel] for channel in order)


def speaker_layout_name(order: tuple[str, ...]) -> str:
    layouts = {
        ("L", "R"): "stereo",
        ("L", "R", "LFE"): "2.1",
        ("L", "R", "C", "LFE"): "3.1",
        ("L", "C", "R", "LFE"): "3.1",
        ("L", "R", "C", "LFE", "Ls", "Rs"): "5.1",
        ("L", "C", "R", "Ls", "Rs", "LFE"): "5.1",
        ("L", "R", "C", "LFE", "Lss", "Rss", "Lsr", "Rsr"): "7.1",
        ("L", "C", "R", "Lss", "Rss", "Lsr", "Rsr", "LFE"): "7.1",
        ("L", "R", "C", "LFE", "Ls", "Rs", "Lrs", "Rrs", "Ltm", "Rtm"): "7.1.2",
    }
    return layouts.get(order, "custom")


def merge_target_name(base: str, suffix: str, order_name: str, order: tuple[str, ...], rename: bool, include_format: bool, include_layout: bool, position: str, custom_layout_name: str) -> str:
    if not rename:
        return base + suffix
    tokens = []
    if include_format:
        tokens.append(order_name.casefold())
    if include_layout:
        tokens.append((custom_layout_name.strip() or "custom") if order_name == "CUSTOM" else speaker_layout_name(order))
    if not tokens:
        return base + suffix
    token_text = "_".join(tokens)
    return f"{token_text}_{base}{suffix}" if position == "Prepend" else f"{base}_{token_text}{suffix}"
