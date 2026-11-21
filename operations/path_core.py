from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


def natural_path_key(path: Path) -> list[str | int]:
    parts = re.split(r"(\d+)", path.as_posix().casefold())
    return [int(part) if part.isdigit() else part for part in parts]


def wav_files(root: Path) -> list[Path]:
    return sorted((path for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".wav"), key=natural_path_key)


def list_qc_recursive_wav_files(folder: Path) -> list[Path]:
    return sorted(
        (path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() == ".wav"),
        key=lambda path: (path.name.casefold(), path.as_posix().casefold()),
    )


def resolve_output_root(source_root: Path, output_text: str) -> Optional[Path]:
    output_text = output_text.strip()
    if not output_text:
        return None
    output_path = Path(output_text).expanduser()
    if output_path.is_absolute():
        return output_path
    return source_root.parent / output_path
