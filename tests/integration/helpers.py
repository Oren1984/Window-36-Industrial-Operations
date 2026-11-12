from __future__ import annotations

from sqlalchemy import select

from window36.db import session_scope
from window36.models import Document, Plan
from window36.services import planning


def current(engine) -> dict[str, Plan]:
    with session_scope(engine) as s:
        return {p.plan_key: p for p in planning.current_plans(s)}


def staged_docs(engine) -> list[int]:
    with session_scope(engine) as s:
        return list(s.scalars(select(Document.id).where(Document.status == "staged").order_by(Document.id)))


def review_all_and_replan(act, engine):
    from window36.services import documents

    for doc_id in staged_docs(engine):
        act("planner_fr", documents.review_document, doc_id, True, "Checked against goods-in paperwork")
    act("planner_fr", planning.replan, "after document review")
    return current(engine)


def complete_all_tasks(act, plan):
    from window36.services import qa

    for t in sorted(plan.schedule, key=lambda t: t["start"]):
        act("operator_fr", qa.record_work, plan.id, t["code"])
