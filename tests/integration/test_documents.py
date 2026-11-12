"""Untrusted document intake: limits, validation, provenance, conflicts, idempotency."""

from __future__ import annotations

from sqlalchemy import select

from tests.integration.helpers import staged_docs
from window36.db import session_scope
from window36.models import Document, ExtractedFact, FactConflict, StockItem
from window36.services import documents, health


def test_seeded_documents_are_staged_with_provenance_and_conflict(db):
    with session_scope(db) as s:
        docs = s.scalars(select(Document).order_by(Document.id)).all()
        assert [d.status for d in docs] == ["staged", "staged"]
        assert all("deterministic" in d.extractor for d in docs)
        rev = s.scalar(
            select(ExtractedFact).where(ExtractedFact.document_id == docs[1].id, ExtractedFact.field == "REVISION")
        )
        assert rev.value == "B" and rev.provenance.startswith("line ")
        conflict = s.scalar(select(FactConflict).where(FactConflict.field == "REVISION"))
        assert conflict.status == "open" and len(conflict.values) == 2
        # Staging alone does not change operational state.
        assert s.get(StockItem, "SP-FR-0001").status == "inbound"


def test_rejects_oversize_wrong_format_binary_and_missing_fields(db, act):
    big = b"DOCUMENT_TYPE: supplier_notice\n" + b"X" * 70_000
    cases = [
        ("big.txt", big),
        ("evil.exe", b"MZ"),
        ("bin.txt", b"\xff\xfe\x00"),
        ("ctrl.txt", b"DOCUMENT_TYPE: supplier_notice\x07"),
        ("partial.txt", b"DOCUMENT_TYPE: supplier_notice"),
    ]
    for name, content in cases:
        doc = act("operator_fr", documents.import_document, name, content)
        assert doc.status == "failed", name
    with session_scope(db) as s:
        assert health.counters(s)["Ingestion failures"] == len(cases)


def test_invalid_field_values_are_flagged(db, act):
    content = (
        b"DOCUMENT_TYPE: shipping_document\nDELIVERY_REF: DEL-XYZ-1\nPART_NUMBER: W36-CPL-4471\n"
        b"REVISION: C\nQUANTITY: lots\nEXPECTED_ARRIVAL: tomorrow\nORIGIN: <script>x</script>\n"
    )
    doc = act("operator_fr", documents.import_document, "x.txt", content)
    assert doc.status == "staged"
    with session_scope(db) as s:
        bad = {
            f.field
            for f in s.scalars(
                select(ExtractedFact).where(ExtractedFact.document_id == doc.id, ExtractedFact.valid.is_(False))
            )
        }
    assert bad == {"QUANTITY", "EXPECTED_ARRIVAL", "ORIGIN"}


def test_reupload_is_idempotent(db, act):
    content = b"DOCUMENT_TYPE: supplier_notice\nDELIVERY_REF: DEL-1\nPART_NUMBER: P-1\nREVISION: A\n"
    a = act("operator_fr", documents.import_document, "a.txt", content)
    b = act("operator_fr", documents.import_document, "a-copy.txt", content)
    assert a.id == b.id


def test_path_components_are_stripped_from_filenames(db, act):
    content = b"DOCUMENT_TYPE: supplier_notice\nDELIVERY_REF: DEL-2\nPART_NUMBER: P-2\nREVISION: A\n"
    doc = act("operator_fr", documents.import_document, "../../etc/passwd.txt", content)
    assert doc.filename == "passwd.txt"


def test_only_reviewed_documents_change_state(db, act):
    first, second = staged_docs(db)
    act("planner_fr", documents.review_document, first, True, "notice ok")
    with session_scope(db) as s:
        assert s.get(StockItem, "SP-FR-0001").status == "inbound"
    act("planner_fr", documents.review_document, second, True, "delivery note says rev B")
    with session_scope(db) as s:
        item = s.get(StockItem, "SP-FR-0001")
        assert (item.status, item.revision_status) == ("quarantine", "disputed")


def test_filenames_cannot_carry_markup(db, act):
    content = b"DOCUMENT_TYPE: supplier_notice\nDELIVERY_REF: DEL-3\nPART_NUMBER: P-3\nREVISION: A\n"
    doc = act("operator_fr", documents.import_document, "[click](http:x) <b>.txt", content)
    assert doc.filename == "_click__http_x___b_.txt"
