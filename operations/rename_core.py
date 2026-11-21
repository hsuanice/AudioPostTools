from __future__ import annotations

from dataclasses import dataclass
import re


RENAME_LIVE_REFRESH_LIMIT = 1000


def rename_live_refresh_allowed(job_count: int, limit: int = RENAME_LIVE_REFRESH_LIMIT) -> bool:
    return job_count <= limit


@dataclass(frozen=True)
class RenameRule:
    find: str
    replace: str
    regex: bool = False


@dataclass(frozen=True)
class SerialMetadata:
    group_key: str
    tape: str
    time_reference: int


def strict_metadata_group_order(rows: list[SerialMetadata]) -> tuple[list[str], dict[str, str]]:
    grouped: dict[str, list[SerialMetadata]] = {}
    for row in rows:
        grouped.setdefault(row.group_key, []).append(row)

    ordered: list[tuple[str, int, str]] = []
    blocked: dict[str, str] = {}
    identities: dict[tuple[str, int], list[str]] = {}
    for group_key, group_rows in grouped.items():
        tapes = {row.tape.strip() for row in group_rows if row.tape.strip()}
        references = {row.time_reference for row in group_rows if row.time_reference >= 0}
        if len(tapes) != 1:
            blocked[group_key] = "BLOCKED: missing or inconsistent Tape"
            continue
        if len(references) != 1:
            blocked[group_key] = "BLOCKED: missing or inconsistent TC"
            continue
        tape = next(iter(tapes))
        time_reference = next(iter(references))
        identities.setdefault((tape.casefold(), time_reference), []).append(group_key)
        ordered.append((tape.casefold(), time_reference, group_key))

    for group_keys in identities.values():
        if len(group_keys) > 1:
            for group_key in group_keys:
                blocked[group_key] = "BLOCKED: duplicate Tape + TC"

    return [group_key for _tape, _tc, group_key in sorted(ordered) if group_key not in blocked], blocked


def rename_input_pattern(formula: str) -> re.Pattern[str]:
    formula = formula.strip()
    if formula.casefold() == "{base}":
        return re.compile(r"^(?P<base>.+)\.wav$", re.IGNORECASE)
    if formula.casefold() == "{base}.a$":
        return re.compile(r"^(?P<base>.+?)(?:\.A(?P<ch>[1-9]\d*))?\.wav$", re.IGNORECASE)
    if formula.count("{base}") != 1 or (formula.count("$") + formula.count("{channel}")) != 1:
        raise ValueError("Rename source formula must contain one {base} and one $ or {channel} placeholder.")
    expression = re.escape(formula)
    expression = expression.replace(r"\{base\}", r"(?P<base>.+?)")
    expression = expression.replace(r"\$", r"(?P<ch>[1-9]\d*)")
    expression = expression.replace(r"\{channel\}", r"(?P<channel>[A-Za-z][A-Za-z0-9]*)")
    return re.compile(rf"^{expression}\.wav$", re.IGNORECASE)


def rename_auto_input_pattern() -> re.Pattern[str]:
    return re.compile(
        r"^(?P<base>.+?)(?:\.A(?P<ch>[1-9]\d*)|\.(?P<channel>[A-Za-z]+\d*))?\.wav$",
        re.IGNORECASE,
    )


def rename_number_from_match(match: re.Match[str]) -> int:
    value = match.groupdict().get("ch")
    return int(value) if value else 0


def apply_rename_rules(value: str, rules: list[RenameRule]) -> str:
    result = value
    for rule in rules:
        if not rule.find:
            raise ValueError("Find text cannot be empty.")
        result = re.sub(rule.find, rule.replace, result) if rule.regex else result.replace(rule.find, rule.replace)
    return result


def apply_name_affix(value: str, text: str, position: str) -> str:
    if not text:
        return value
    return f"{text}{value}" if position.casefold() == "prepend" else f"{value}{text}"


def assign_serial_ids(
    group_keys: list[str],
    existing: dict[str, str],
    prefix: str,
    digits: int,
    start: int,
) -> tuple[dict[str, str], int]:
    if digits < 1:
        raise ValueError("Serial ID digits must be at least 1.")
    if start < 0:
        raise ValueError("Serial ID start must be non-negative.")
    assignments = dict(existing)
    next_number = start
    used_numbers = {
        int(value[len(prefix):])
        for value in assignments.values()
        if value.startswith(prefix) and value[len(prefix):].isdigit()
    }
    if used_numbers:
        next_number = max(next_number, max(used_numbers) + 1)

    for key in sorted(set(group_keys), key=str.casefold):
        if key in assignments:
            continue
        assignments[key] = f"{prefix}{next_number:0{digits}d}"
        next_number += 1
    return assignments, next_number


def assign_serial_ids_in_order(
    group_keys: list[str], existing: dict[str, str], prefix: str, digits: int, start: int
) -> tuple[dict[str, str], int]:
    if digits < 1:
        raise ValueError("Serial ID digits must be at least 1.")
    if start < 0:
        raise ValueError("Serial ID start must be non-negative.")
    assignments = dict(existing)
    next_number = start
    used_numbers = {
        int(value[len(prefix):])
        for value in assignments.values()
        if value.startswith(prefix) and value[len(prefix):].isdigit()
    }
    if used_numbers:
        next_number = max(next_number, max(used_numbers) + 1)
    for group_key in group_keys:
        if group_key not in assignments:
            assignments[group_key] = f"{prefix}{next_number:0{digits}d}"
            next_number += 1
    return assignments, next_number


def render_serial_name(
    formula: str,
    base: str,
    number: int,
    suffix: str,
    serial_id: str = "",
    channel: str = "",
    serial_position: str = "prepend",
) -> str:
    formula = formula.strip() or "{base}.A$"
    if number == 0 and re.fullmatch(r"\{base\}\.A\$", formula, re.IGNORECASE):
        formula = "{base}.{channel}" if channel else "{base}"
    normalized_formula = re.sub(r"\{id\}", "{id}", formula, flags=re.IGNORECASE)
    normalized_formula = re.sub(r"\{base\}", "{base}", normalized_formula, flags=re.IGNORECASE)
    normalized_formula = re.sub(r"\{channel\}", "{channel}", normalized_formula, flags=re.IGNORECASE)
    identity_count = normalized_formula.count("{base}") + normalized_formula.count("{id}")
    channel_count = normalized_formula.count("{channel}")
    if identity_count != 1 or (normalized_formula.count("$") + channel_count != 1 and normalized_formula != "{base}"):
        raise ValueError("Output name formula must contain exactly one {base} or {id}, and one $ or {channel} placeholder.")
    if "{id}" in normalized_formula:
        if normalized_formula.count("{id}") != 1 or not serial_id:
            raise ValueError("Output name formula requires one non-empty {id} value.")
        name = normalized_formula.replace("{id}", serial_id)
    else:
        name = normalized_formula
    if channel_count and not channel:
        raise ValueError("Output name formula requires a non-empty {channel} value.")
    name = name.replace("{base}", base).replace("$", str(number)).replace("{channel}", channel)
    if not name.lower().endswith(suffix.lower()):
        name += suffix
    if serial_id and "{id}" not in normalized_formula:
        if serial_position == "append":
            name = f"{name[:-len(suffix)]}_{serial_id}{suffix}"
        else:
            name = f"{serial_id}_{name}"
    return name
