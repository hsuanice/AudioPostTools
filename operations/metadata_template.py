from __future__ import annotations

import re

# Ported from hsuanice's REAPER "Rename Source File from Metadata" Lua
# script's token set — every token here has a file-only equivalent (the
# script's REAPER-session-only tokens, like $track/$marker/$curtake or the
# timeline-overlap-based $overlapindex/$rangeindex, have no meaning for a
# folder of WAV files and are intentionally not ported).
METADATA_TOKEN_NAMES = (
    "srcfile", "srcbase", "srcext", "srcpath", "srcdir",
    "samplerate", "channels", "length",
    "project", "scene", "take", "tape", "ubits", "framerate", "speed",
    "date", "time", "year", "originationdate", "originationtime", "startoffset",
    "umid", "umid_pt",
    "trk", "trkall", "chnum", "interleave", "baseindex",
    "origsrcfile",
)

_SANITIZE_PATTERN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_BRACED_TOKEN_PATTERN = re.compile(r"\$\{([^}]+)\}")
_BARE_TOKEN_PATTERN = re.compile(r"\$([A-Za-z0-9]+)")
_PARAMETERIZED_TOKEN_PATTERN = re.compile(r"\$(trk\d+|counter:\d+|srcbaseprefix:\d+|srcbasesuffix:\d+)\b")
# "counter" has no file-metadata equivalent to look up by name, so it isn't
# a `fields` key — it still needs to be in this list so a *bare* $counter
# gets wrapped in braces the same as every other known token.
_KNOWN_TOKEN_NAMES_LONGEST_FIRST = tuple(sorted(METADATA_TOKEN_NAMES + ("counter",), key=len, reverse=True))


def normalize_metadata_template_tokens(template: str) -> str:
    """Wrap every recognizable bare $token as ${token} — ported from the
    source REAPER script's own normalize_tokens(). Known token names are
    substituted as plain substrings, longest name first, so "$sceneT$take"
    becomes "${scene}T${take}" instead of an unrecognized "scenet" token
    swallowing the "T" the user typed between them. Parameterized forms
    ($trk3, $counter:2, $srcbaseprefix:6, $srcbasesuffix:6) are normalized
    first, since their own digits/colon would otherwise stop a naive bare
    match early. Anything still bare after this is a genuinely unknown
    token and resolves to "" through the normal lookup, same as before."""
    text = _PARAMETERIZED_TOKEN_PATTERN.sub(lambda match: "${" + match.group(1) + "}", template)
    for name in _KNOWN_TOKEN_NAMES_LONGEST_FIRST:
        text = text.replace(f"${name}", "${" + name + "}")
    return text


def sanitize_token_value(value: str) -> str:
    """Replace filename-unsafe characters in a single resolved token's
    value with "_" — applied per-token (matching the source script), so a
    literal separator the user typed into the template itself is never
    touched, only what came out of metadata."""
    return _SANITIZE_PATTERN.sub("_", value)


def umid_to_pt(umid_hex: str) -> str:
    """Reformat a 64-hex-character BWF UMID into the Pro Tools-style
    26-6-16-12-4 hyphenated, lowercase grouping. Returns "" for anything
    that isn't exactly 64 hex characters (once whitespace is stripped)."""
    hex_only = re.sub(r"\s+", "", umid_hex or "").lower()
    if len(hex_only) != 64 or not re.fullmatch(r"[0-9a-f]{64}", hex_only):
        return ""
    groups = (hex_only[0:26], hex_only[26:32], hex_only[32:48], hex_only[48:60], hex_only[60:64])
    return "-".join(groups)


def resolve_metadata_token(
    name: str,
    fields: dict[str, str],
    trk_table: dict[int, str] | None = None,
    counter: int | None = None,
) -> str:
    """Look up one already-isolated token name (no `$`/braces) against
    `fields`/`trk_table`/`counter`. Shared by expand_metadata_template()
    and find_empty_metadata_tokens() so "what does this token resolve to"
    is answered exactly once."""
    trk_table = trk_table or {}
    lowered = name.lower()

    counter_match = re.fullmatch(r"counter(?::(\d+))?", lowered)
    if counter_match:
        digits = int(counter_match.group(1)) if counter_match.group(1) else 0
        value = str(counter if counter is not None else 1)
        return value.zfill(digits) if digits else value

    prefix_match = re.fullmatch(r"srcbaseprefix:(\d+)", lowered)
    if prefix_match:
        n = int(prefix_match.group(1))
        return (fields.get("srcbase") or "")[:n]

    suffix_match = re.fullmatch(r"srcbasesuffix:(\d+)", lowered)
    if suffix_match:
        n = int(suffix_match.group(1))
        srcbase = fields.get("srcbase") or ""
        return srcbase[-n:] if n > 0 else ""

    trk_match = re.fullmatch(r"trk(\d+)", lowered)
    if trk_match:
        return trk_table.get(int(trk_match.group(1)), "")

    return fields.get(lowered, "")


def template_token_names(template: str) -> list[str]:
    """Every distinct token name referenced in `template` (normalized
    first, so "$sceneT$take" correctly yields ["scene", "take"] rather than
    a single unrecognized "scenet"), in first-seen order."""
    normalized = normalize_metadata_template_tokens(template)
    seen: dict[str, None] = {}
    for match in _BRACED_TOKEN_PATTERN.finditer(normalized):
        seen.setdefault(match.group(1), None)
    remaining = _BRACED_TOKEN_PATTERN.sub("", normalized)
    for match in _BARE_TOKEN_PATTERN.finditer(remaining):
        seen.setdefault(match.group(1), None)
    return list(seen)


def find_empty_metadata_tokens(
    template: str,
    fields: dict[str, str],
    trk_table: dict[int, str] | None = None,
    counter: int | None = None,
) -> list[str]:
    """Every token in `template` that resolves to an empty/whitespace-only
    value — e.g. $scene when the file has no Scene metadata at all. Ported
    from the source script's own empty-token check (there, it drives a
    "skip this rename" option and a details table; here it's what a
    missing-token warning/log entry is based on)."""
    return [
        name
        for name in template_token_names(template)
        if not resolve_metadata_token(name, fields, trk_table, counter).strip()
    ]


def expand_metadata_template(
    template: str,
    fields: dict[str, str],
    trk_table: dict[int, str] | None = None,
    counter: int | None = None,
    sanitize: bool = True,
) -> str:
    """Expand $token / ${token} references in `template`.

    `fields` supplies every plain token (case-insensitive key lookup) —
    srcfile/srcbase/.../origsrcfile, one string value each. `trk_table`
    resolves $trk1, $trk2, ... (per-channel track names keyed by channel
    number) independently of `fields["trk"]`, which is the *current* job's
    own channel. `counter` feeds ${counter:N} (zero-padded to N digits) and
    bare $counter (unpadded). Unknown tokens expand to "". Each resolved
    value is sanitized on its own before insertion — literal template text
    the user typed (separators, punctuation) is left exactly as written.

    Known token names are recognized even packed directly against other
    letters — "$sceneT$take" resolves as $scene + literal "T" + $take, not
    an unrecognized "scenet" token (see normalize_metadata_template_tokens
    for exactly how). A name explicitly wrapped in `${...}` always works
    too, known or not, via a plain `fields` lookup — that's the only way
    to reach a token this app doesn't know the name of ahead of time.
    """
    def substitute(match: re.Match[str]) -> str:
        value = resolve_metadata_token(match.group(1), fields, trk_table, counter)
        return sanitize_token_value(value) if sanitize else value

    normalized = normalize_metadata_template_tokens(template)
    normalized = _BRACED_TOKEN_PATTERN.sub(substitute, normalized)
    return _BARE_TOKEN_PATTERN.sub(substitute, normalized)


def build_trk_table_from_ixml_rows(rows: list[tuple[str, str, str]]) -> dict[int, str]:
    """`rows` is ixml_track_rows()'s (stream, recorder_ch, name) output —
    keys the resulting table by Recorder Channel (what $trkN and the
    current job's own $trk/$chnum mean in this app), not stream index."""
    table: dict[int, str] = {}
    for _stream, recorder_channel, name in rows:
        if name and recorder_channel.strip().lstrip("-").isdigit():
            table[int(recorder_channel)] = name
    return table


def concat_trk_names(trk_table: dict[int, str]) -> str:
    return "_".join(trk_table[key] for key in sorted(trk_table) if trk_table[key])
