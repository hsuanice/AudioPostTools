from __future__ import annotations

import re


def validate_filename_placeholders(formula: str, base_placeholder: str, number_placeholder: str, label: str) -> None:
    if formula.count(base_placeholder) != 1 or formula.count(number_placeholder) != 1:
        raise ValueError(f"{label} must contain exactly one {base_placeholder} and one {number_placeholder} placeholder.")


def compile_filename_pattern(
    formula: str,
    base_placeholder: str,
    number_placeholder: str,
    base_group: str,
    number_group: str,
    number_pattern: str,
    label: str,
) -> re.Pattern[str]:
    validate_filename_placeholders(formula, base_placeholder, number_placeholder, label)
    expression = re.escape(formula)
    expression = expression.replace(re.escape(base_placeholder), rf"(?P<{base_group}>.+)")
    expression = expression.replace(re.escape(number_placeholder), rf"(?P<{number_group}>{number_pattern})")
    return re.compile(rf"^{expression}\.wav$", re.IGNORECASE)


def formula_pattern(formula: str, allow_regex: bool = False, label: str = "Formula", number_group: str = "ch") -> re.Pattern[str]:
    formula = formula.strip()
    if not allow_regex:
        return compile_filename_pattern(formula, "{base}", "$", "base", number_group, r"[1-9]\d*", label)
    validate_filename_placeholders(formula, "{base}", "$", label)
    expression_parts: list[str] = []
    index = 0
    while index < len(formula):
        if formula.startswith("{base}", index):
            expression_parts.append(r"(?P<base>.+)")
            index += len("{base}")
        elif formula[index] == "$":
            expression_parts.append(rf"(?P<{number_group}>[1-9]\d*)")
            index += 1
        elif formula[index] == "\\" and index + 1 < len(formula):
            expression_parts.append("\\" + formula[index + 1])
            index += 2
            while index < len(formula) and formula[index] in "+*?":
                expression_parts.append(formula[index])
                index += 1
        else:
            expression_parts.append(re.escape(formula[index]))
            index += 1
    expression = "".join(expression_parts)
    return re.compile(rf"^{expression}\.wav$", re.IGNORECASE)


def formula_without_number(formula: str) -> str:
    """The output formula for a file whose name had no channel number (a
    mono original such as "12-10T2.WAV"): the "$" part is dropped together
    with the separator and letters in front of it — "{base}_AAP.A$" ->
    "{base}_AAP", "{base}_A$" -> "{base}", "{base}_Ch$_x" -> "{base}_x"."""
    formula = formula.strip() or "{base}.A$"
    match = re.search(r"[._\- ][A-Za-z]*\$", formula) or re.search(r"[A-Za-z]*\$", formula)
    if not match:
        return formula
    return formula[:match.start()] + formula[match.end():]


def render_output_name_without_number(formula: str, base: str, suffix: str) -> str:
    formula = formula_without_number(formula)
    if formula.count("{base}") != 1:
        raise ValueError("Output name formula must contain exactly one {base} placeholder.")
    name = formula.replace("{base}", base)
    if name.lower().endswith(suffix.lower()):
        return name
    return name + suffix


def render_output_name(formula: str, base: str, number: int, suffix: str) -> str:
    formula = formula.strip() or "{base}.A$"
    validate_filename_placeholders(formula, "{base}", "$", "Output name formula")
    name = formula.replace("{base}", base).replace("$", str(number))
    if name.lower().endswith(suffix.lower()):
        return name
    return name + suffix
