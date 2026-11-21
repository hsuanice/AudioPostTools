"""Unwritten Phase 3 review work: one draft per file, kept until written.

Reviewing a rebuilt iXML (confirming inferred fields, choosing conflicts,
changing values) is separate from writing it: the user reviews any files in
any order, goes back to change one, and writes them all at the end with
Rebuild All / Rebuild Selected. Drafts are saved to disk on every change so
they survive closing the app, and each carries the file's signature (size +
modification time): a draft for a file that has changed since is discarded
rather than applied to different bytes.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from operations.ixml_rebuild import NEEDS_REVIEW, RebuildPlan


def file_signature(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return ""
    return f"{stat.st_size}:{stat.st_mtime_ns}"


@dataclass
class RebuildDraft:
    signature: str
    choices: dict[str, Optional[str]] = field(default_factory=dict)
    confirmed: set[str] = field(default_factory=set)
    overridden: set[str] = field(default_factory=set)  # changed although no review was needed
    left_to_review: Optional[int] = None  # as of the last plan seen; None = never planned

    def remaining(self, plan: RebuildPlan) -> int:
        return sum(item.path not in self.confirmed for item in plan.needing_review())

    def is_reviewed(self, plan: RebuildPlan) -> bool:
        return self.remaining(plan) == 0

    def final_choices(self, plan: RebuildPlan) -> dict[str, Optional[str]]:
        """What render_ixml() should apply: every reviewed field (its chosen
        value, or the proposed one if it was simply confirmed) plus every
        field the user changed."""
        paths = {item.path for item in plan.needing_review()} | self.overridden
        return {item.path: (self.choices[item.path] if item.path in self.choices else item.value) for item in plan.fields if item.path in paths}

    def to_json(self) -> dict:
        return {
            "signature": self.signature,
            "choices": self.choices,
            "confirmed": sorted(self.confirmed),
            "overridden": sorted(self.overridden),
            "left_to_review": self.left_to_review,
        }

    @classmethod
    def from_json(cls, data: dict) -> "RebuildDraft":
        return cls(
            signature=str(data.get("signature", "")),
            choices={str(key): (None if value is None else str(value)) for key, value in dict(data.get("choices", {})).items()},
            confirmed=set(data.get("confirmed", [])),
            overridden=set(data.get("overridden", [])),
            left_to_review=data.get("left_to_review"),
        )


class DraftStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.drafts: dict[str, RebuildDraft] = {}
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.drafts = {key: RebuildDraft.from_json(value) for key, value in dict(data.get("drafts", {})).items()}
        except (OSError, ValueError, AttributeError):
            self.drafts = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + ".tmp")
            temp.write_text(json.dumps({"version": 1, "drafts": {key: draft.to_json() for key, draft in self.drafts.items()}}, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, self.path)
        except OSError:
            pass

    def get(self, target: Path, create: bool = False) -> Optional[RebuildDraft]:
        """The draft for this file — only while the file is unchanged."""
        key, signature = str(target), file_signature(target)
        draft = self.drafts.get(key)
        if draft is not None and draft.signature != signature:
            del self.drafts[key]
            self.save()
            draft = None
        if draft is None and create:
            draft = self.drafts[key] = RebuildDraft(signature)
        return draft

    def discard(self, target: Path) -> None:
        if self.drafts.pop(str(target), None) is not None:
            self.save()


class ConfirmStore:
    """Per-file yes/no confirmations (Recover audio), saved to disk and tied
    to the file's signature like drafts: a confirmation for a file that has
    changed since is dropped."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.items: dict[str, str] = {str(key): str(value) for key, value in dict(data.get("confirmed", {})).items()}
        except (OSError, ValueError, AttributeError):
            self.items = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + ".tmp")
            temp.write_text(json.dumps({"version": 1, "confirmed": self.items}, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, self.path)
        except OSError:
            pass

    def is_confirmed(self, target: Path) -> bool:
        signature = self.items.get(str(target))
        if signature is None:
            return False
        if signature != file_signature(target):
            del self.items[str(target)]
            self._save()
            return False
        return True

    def set_confirmed(self, target: Path, confirmed: bool) -> None:
        if confirmed:
            self.items[str(target)] = file_signature(target)
        else:
            self.items.pop(str(target), None)
        self._save()
