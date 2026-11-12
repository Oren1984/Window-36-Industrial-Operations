"""Document extraction interface.

The default (and only shipped) extractor is a deterministic rule-based parser for the
`KEY: value` fixture format. It uses no AI model. A model-driven adapter could implement the
same `DocumentExtractor` protocol; it would feed the same staging and human review workflow and
could never approve plans, reserve stock or trigger actions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class RawField:
    field: str
    value: str
    provenance: str


class DocumentExtractor(Protocol):
    name: str
    mode: str  # "deterministic" | "model"

    def extract(self, text: str) -> list[RawField]: ...


class DeterministicFixtureExtractor:
    name = "deterministic-fixture-parser v1"
    mode = "deterministic"
    _line = re.compile(r"^([A-Z_]{2,40})\s*:\s*(.*?)\s*$")

    def extract(self, text: str) -> list[RawField]:
        out: list[RawField] = []
        for no, line in enumerate(text.splitlines(), start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = self._line.match(line.strip())
            if m:
                out.append(RawField(m.group(1), m.group(2), f"line {no}"))
        return out


_ID = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,39}$")
_TEXT = re.compile(r"^[\w .,'()/&@+-]{1,120}$", re.UNICODE)

KNOWN_FIELDS = {
    "DOCUMENT_TYPE",
    "SOURCE_REF",
    "DELIVERY_REF",
    "PART_NUMBER",
    "REVISION",
    "SERIAL",
    "QUANTITY",
    "SHIP_DATE",
    "EXPECTED_ARRIVAL",
    "ORIGIN",
    "DESTINATION",
    "CARRIER",
}
REQUIRED_FIELDS = ("DOCUMENT_TYPE", "DELIVERY_REF", "PART_NUMBER", "REVISION")
DOC_TYPES = {"supplier_notice", "shipping_document"}


def validate_field(field: str, value: str) -> str | None:
    """Return an error message, or None if valid."""
    if field not in KNOWN_FIELDS:
        return "Unknown field (ignored)"
    if field == "DOCUMENT_TYPE":
        return None if value in DOC_TYPES else f"Unsupported document type '{value}'"
    if field in {"SOURCE_REF", "DELIVERY_REF", "PART_NUMBER", "SERIAL"}:
        return None if _ID.match(value) else "Invalid identifier format"
    if field == "REVISION":
        return None if re.fullmatch(r"[A-Z]", value) else "Revision must be a single letter A–Z"
    if field == "QUANTITY":
        return None if value.isdigit() and 1 <= int(value) <= 1000 else "Quantity must be 1–1000"
    if field in {"EXPECTED_ARRIVAL", "SHIP_DATE"}:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return "Not an ISO 8601 date/time"
        if field == "EXPECTED_ARRIVAL" and parsed.tzinfo is None:
            return "Expected arrival needs an explicit UTC offset"
        return None
    return None if _TEXT.match(value) else "Contains unsupported characters or is too long"


def default_extractor() -> DocumentExtractor:
    return DeterministicFixtureExtractor()
