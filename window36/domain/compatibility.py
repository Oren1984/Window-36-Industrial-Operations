"""Explicit part revision compatibility rules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    part_number: str
    line_code: str
    revision: str
    compatible: bool
    note: str


def check(rules: list[Rule], part_number: str, line_code: str, revision: str) -> tuple[bool, str]:
    for r in rules:
        if (r.part_number, r.line_code, r.revision) == (part_number, line_code, revision):
            return r.compatible, r.note
    return False, f"No compatibility rule for {part_number} rev {revision} on {line_code} (treated as incompatible)"
