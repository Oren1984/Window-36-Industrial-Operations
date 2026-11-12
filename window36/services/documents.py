"""Untrusted document intake, staging and human review.

Only a reviewed (accepted) document can change operational state.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import PurePath

from sqlalchemy import select

from window36.config import get_settings
from window36.logging_setup import log
from window36.models import Document, ExtractedFact, FactConflict, StockItem
from window36.services.common import Ctx, DomainError, audit, bump_input, now, require
from window36.services.extraction import REQUIRED_FIELDS, DocumentExtractor, default_extractor, validate_field

logger = logging.getLogger("window36.documents")

ALLOWED_EXTENSIONS = {".txt"}
COMPARED_FIELDS = ("PART_NUMBER", "REVISION", "SERIAL")


def _fail(ctx: Ctx, filename: str, digest: str, size: int, error: str, extractor: str) -> Document:
    doc = Document(
        kind="unknown",
        filename=filename[:200],
        sha256=digest,
        size_bytes=size,
        status="failed",
        extractor=extractor,
        error=error,
        uploaded_by=ctx.actor.username,
        uploaded_at=now(ctx.session),
    )
    ctx.session.add(doc)
    ctx.session.flush()
    audit(
        ctx,
        "document.import",
        "document",
        str(doc.id),
        "blocked",
        f"Import failed: {error}",
        evidence_refs=[f"sha256:{digest[:12]}"],
    )
    log(logger, logging.WARNING, "document import failed", filename=filename, error=error, status="failed")
    return doc


def import_document(ctx: Ctx, filename: str, content: bytes, extractor: DocumentExtractor | None = None) -> Document:
    require(ctx, "document.import", entity="document")
    extractor = extractor or default_extractor()
    settings = get_settings()
    digest = hashlib.sha256(content).hexdigest()
    # Keep only the base name, restricted to a conservative character set (no markup, no paths).
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", PurePath(filename.replace("\\", "/")).name)[:120] or "upload"
    existing = ctx.session.scalar(select(Document).where(Document.sha256 == digest, Document.status != "failed"))
    if existing is not None:
        return existing  # idempotent re-upload
    if len(content) > settings.max_upload_bytes:
        return _fail(
            ctx,
            safe_name,
            digest,
            len(content),
            f"File too large ({len(content)} bytes > {settings.max_upload_bytes})",
            extractor.name,
        )
    if PurePath(safe_name).suffix.lower() not in ALLOWED_EXTENSIONS:
        return _fail(ctx, safe_name, digest, len(content), "Unsupported format (only .txt fixtures)", extractor.name)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return _fail(ctx, safe_name, digest, len(content), "Not valid UTF-8 text", extractor.name)
    if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
        return _fail(ctx, safe_name, digest, len(content), "Contains control characters", extractor.name)

    raw = extractor.extract(text)
    values: dict[str, str] = {}
    facts: list[ExtractedFact] = []
    for f in raw:
        error = validate_field(f.field, f.value)
        facts.append(
            ExtractedFact(
                field=f.field[:40],
                value=f.value[:200],
                provenance=f.provenance,
                valid=error is None,
                validation_error=error,
            )
        )
        if error is None and f.field not in values:
            values[f.field] = f.value
    missing = [k for k in REQUIRED_FIELDS if k not in values]
    if missing:
        return _fail(
            ctx,
            safe_name,
            digest,
            len(content),
            "Missing or invalid required fields: " + ", ".join(missing),
            extractor.name,
        )

    doc = Document(
        kind=values["DOCUMENT_TYPE"],
        filename=safe_name,
        sha256=digest,
        size_bytes=len(content),
        delivery_ref=values["DELIVERY_REF"],
        source_ref=values.get("SOURCE_REF"),
        status="staged",
        extractor=f"{extractor.name} ({extractor.mode})",
        uploaded_by=ctx.actor.username,
        uploaded_at=now(ctx.session),
    )
    ctx.session.add(doc)
    ctx.session.flush()
    for fact in facts:
        fact.document_id = doc.id
        ctx.session.add(fact)
    ctx.session.flush()
    _detect_conflicts(ctx, doc.delivery_ref)
    audit(
        ctx,
        "document.import",
        "document",
        str(doc.id),
        "applied",
        f"Staged {doc.kind} {doc.source_ref or ''} for delivery {doc.delivery_ref}; awaiting review",
        evidence_refs=[f"{doc.filename}", f"sha256:{digest[:12]}"],
    )
    log(logger, logging.INFO, "document staged", document_id=doc.id, delivery=doc.delivery_ref, status="staged")
    return doc


def facts_for(ctx_session, doc_id: int) -> dict[str, ExtractedFact]:
    rows = ctx_session.scalars(
        select(ExtractedFact).where(ExtractedFact.document_id == doc_id, ExtractedFact.valid.is_(True))
    ).all()
    out: dict[str, ExtractedFact] = {}
    for r in rows:
        out.setdefault(r.field, r)
    return out


def _detect_conflicts(ctx: Ctx, delivery_ref: str) -> list[FactConflict]:
    docs = ctx.session.scalars(
        select(Document).where(Document.delivery_ref == delivery_ref, Document.status.in_(["staged", "accepted"]))
    ).all()
    found = []
    for field in COMPARED_FIELDS:
        vals = {}
        for d in docs:
            fact = facts_for(ctx.session, d.id).get(field)
            if fact:
                vals[f"doc:{d.id}"] = f"{fact.value} ({d.kind}, {fact.provenance})"
        distinct = {v.split(" (")[0] for v in vals.values()}
        if len(distinct) > 1:
            row = ctx.session.scalar(
                select(FactConflict).where(FactConflict.delivery_ref == delivery_ref, FactConflict.field == field)
            )
            if row is None:
                row = FactConflict(
                    delivery_ref=delivery_ref, field=field, values=vals, status="open", detected_at=now(ctx.session)
                )
                ctx.session.add(row)
            elif row.status == "open":
                row.values = vals
            found.append(row)
    ctx.session.flush()
    return found


def review_document(ctx: Ctx, doc_id: int, accept: bool, note: str) -> Document:
    doc = ctx.session.get(Document, doc_id, with_for_update=True)
    if doc is None:
        raise DomainError(f"Document {doc_id} not found")
    item = ctx.session.scalar(select(StockItem).where(StockItem.delivery_ref == doc.delivery_ref))
    require(ctx, "document.review", site_id=item.site_id if item else None, entity="document", entity_id=str(doc_id))
    if doc.status != "staged":
        raise DomainError(f"Document {doc_id} is {doc.status}; only staged documents can be reviewed")
    if not note.strip():
        raise DomainError("A review note is required")
    doc.status = "accepted" if accept else "rejected"
    doc.reviewed_by = ctx.actor.username
    doc.reviewed_at = now(ctx.session)
    doc.review_note = note.strip()[:500]
    ctx.session.flush()
    audit(
        ctx,
        "document.review",
        "document",
        str(doc.id),
        "applied",
        f"{'Accepted' if accept else 'Rejected'} {doc.kind} {doc.source_ref}: {doc.review_note}",
        site_id=item.site_id if item else None,
        evidence_refs=[doc.filename],
    )
    if accept and item is not None:
        _apply_accepted(ctx, doc, item)
    return doc


def _apply_accepted(ctx: Ctx, doc: Document, item: StockItem) -> None:
    """Apply reviewed facts. Disagreement among accepted documents (or with the expected
    revision) quarantines the part until receiving and QA evidence resolves it."""
    accepted = ctx.session.scalars(
        select(Document).where(Document.delivery_ref == doc.delivery_ref, Document.status == "accepted")
    ).all()
    revisions = {}
    for d in accepted:
        fact = facts_for(ctx.session, d.id).get("REVISION")
        if fact:
            revisions[d.kind] = fact.value
    distinct = set(revisions.values()) | ({item.revision} if item.revision_status == "expected" else set())
    if item.status in ("inbound", "available") and item.revision_status != "verified" and len(distinct) > 1:
        item.status = "quarantine"
        item.revision_status = "disputed"
        detail = ", ".join(f"{k}: rev {v}" for k, v in sorted(revisions.items()))
        reason = (
            f"Reviewed documents disagree on revision for {item.id} ({detail}; expected rev {item.revision}). "
            "Part unavailable for planning until receiving and QA evidence resolves it."
        )
        ctx.session.flush()
        audit(
            ctx,
            "stock.quarantine",
            "stock_item",
            item.id,
            "applied",
            reason,
            site_id=item.site_id,
            policy_id="POL-REV-001",
            policy_version="1.2",
            evidence_refs=[d.filename for d in accepted],
        )
        bump_input(ctx, f"stock:{item.id}", reason)
    else:
        audit(
            ctx,
            "document.apply",
            "stock_item",
            item.id,
            "info",
            f"Reviewed {doc.kind} consistent with current record for {item.id}",
            site_id=item.site_id,
            evidence_refs=[doc.filename],
        )
