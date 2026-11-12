"""Concurrency, idempotency, ordering and restart persistence."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError

from tests.conftest import TEST_URL, as_user
from tests.integration.helpers import current, review_all_and_replan
from window36.db import session_scope
from window36.models import Approval, AuditEvent, ExternalEvent, Plan, Reservation, RouteLeg
from window36.services import approvals, events, inventory, planning, scenario
from window36.services.common import ConflictError


def _parallel(funcs):
    barrier = threading.Barrier(len(funcs))
    results: list = [None] * len(funcs)

    def wrap(i, f):
        barrier.wait()
        try:
            results[i] = ("ok", f())
        except Exception as exc:  # noqa: BLE001
            results[i] = ("err", exc)

    threads = [threading.Thread(target=wrap, args=(i, f)) for i, f in enumerate(funcs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


def test_simultaneous_reservations_of_same_spare(db, act):
    plans = review_all_and_replan(act, db)
    ids = [plans["FULL-IE-FR-AIR"].id, plans["FULL-IE-FR-SEA"].id]

    def claim(plan_id):
        def f():
            with as_user(db, "approver_fr") as ctx:
                return inventory.reserve_for_plan(ctx, "SP-IE-0001", plan_id, f"plan-{plan_id}").id

        return f

    results = _parallel([claim(ids[0]), claim(ids[1])])
    oks = [r for r in results if r[0] == "ok"]
    errs = [r for r in results if r[0] == "err"]
    assert len(oks) == 1 and len(errs) == 1
    assert isinstance(errs[0][1], ConflictError)
    with session_scope(db) as s:
        n = s.scalar(
            select(func.count())
            .select_from(Reservation)
            .where(Reservation.stock_item_id == "SP-IE-0001", Reservation.status == "active")
        )
        assert n == 1


def test_partial_unique_index_is_the_backstop(db):
    with pytest.raises(DBAPIError), db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO reservation (stock_item_id, holder_type, holder_ref, status, reason, "
                "created_by, created_at) VALUES ('SP-DE-0001','plan','x','active','', 't', now())"
            )
        )


def test_concurrent_duplicate_approval_creates_one(db, act):
    plans = review_all_and_replan(act, db)
    pid = plans["REDUCED-SCOPE"].id

    def approve():
        with as_user(db, "approver_fr") as ctx:
            return approvals.approve_plan(ctx, pid, "same request twice").id

    results = _parallel([approve, approve])
    assert all(r[0] == "ok" for r in results), results
    assert results[0][1] == results[1][1]
    # A plain repeat is also idempotent.
    assert act("approver_fr", approvals.approve_plan, pid, "again").id == results[0][1]
    with session_scope(db) as s:
        assert s.scalar(select(func.count()).select_from(Approval).where(Approval.plan_id == pid)) == 1
        assert s.scalar(select(func.count()).select_from(Reservation).where(Reservation.status == "active")) == 1


def test_repeated_event_does_not_reapply(db, act):
    review_all_and_replan(act, db)
    assert act("planner_fr", scenario.apply_disturbance, "road_pickup_delay")[0] == "applied"
    assert act("planner_fr", scenario.apply_disturbance, "road_pickup_delay")[0] == "duplicate"
    with session_scope(db) as s:
        ev = s.scalar(select(ExternalEvent).where(ExternalEvent.event_id == "EVT-DE-FR-ROAD-0002"))
        assert ev.duplicate_count == 1
        changes = s.scalar(text("select count(*) from input_change where key = 'route:DE-FR-ROAD'"))
        assert changes == 1


def test_repeated_event_after_approval_does_not_create_reservation_or_approval(db, act):
    plans = review_all_and_replan(act, db)
    act("approver_ie", approvals.donor_release, plans["FULL-IE-FR-AIR"].id, "ok")
    act("approver_fr", approvals.approve_plan, plans["FULL-IE-FR-AIR"].id, "air")
    for _ in range(3):
        act(
            "planner_fr",
            events.ingest_event,
            "EVT-IE-FR-AIR-0001",
            "carrier (simulated)",
            "route:IE-FR-AIR",
            1,
            "carrier.status",
            {"status": "Pickup slot confirmed"},
        )
    with session_scope(db) as s:
        assert s.scalar(select(func.count()).select_from(Approval)) == 2  # donor release + approval
        assert s.scalar(select(func.count()).select_from(Reservation).where(Reservation.holder_type == "plan")) == 1


def test_out_of_order_event_does_not_overwrite_newer_state(db, act):
    newer = dict(source="tms", entity_key="route:DE-FR-ROAD", kind="carrier.pickup_delay")
    assert (
        act("planner_fr", events.ingest_event, "EVT-X-3", sequence=3, payload={"leg": 1, "delay_h": 2}, **newer)[0]
        == "applied"
    )
    outcome, ev = act(
        "planner_fr", events.ingest_event, "EVT-X-2", sequence=2, payload={"leg": 1, "delay_h": 16}, **newer
    )
    assert outcome == "out_of_order"
    with session_scope(db) as s:
        leg = s.scalar(select(RouteLeg).where(RouteLeg.route_id == "DE-FR-ROAD"))
        assert leg.delay_h == 2
        assert s.scalar(select(AuditEvent).where(AuditEvent.action == "event.out_of_order")) is not None


def test_state_persists_across_restart(db, act):
    plans = review_all_and_replan(act, db)
    act("approver_fr", approvals.approve_plan, plans["REDUCED-SCOPE"].id, "persist me")
    db.dispose()  # drop every pooled connection, as a process restart would
    fresh = create_engine(TEST_URL)
    try:
        with session_scope(fresh) as s:
            p = s.get(Plan, plans["REDUCED-SCOPE"].id)
            assert p.status == "approved"
            assert approvals.active_approval(s, p.id, "plan_approval") is not None
        assert current(fresh)["REDUCED-SCOPE"].status == "approved"
    finally:
        fresh.dispose()


def test_plan_revisions_are_immutable_in_the_database(db):
    with pytest.raises(DBAPIError, match="immutable"), db.begin() as conn:
        conn.execute(text("UPDATE plan SET est_cost_eur = 1 WHERE id = 1"))
    with db.begin() as conn:  # status changes are allowed
        conn.execute(text("UPDATE plan SET status = 'rejected' WHERE id = 1"))


def test_audit_trail_is_append_only(db):
    with pytest.raises(DBAPIError, match="append-only"), db.begin() as conn:
        conn.execute(text("DELETE FROM audit_event"))


def test_replan_without_changes_creates_no_revision(db, act):
    # The seed computed the baseline at 01:00; the clock is now 02:00, so the first replan may revise
    # clock-dependent plans. Replanning again without any change must not create revisions.
    act("planner_fr", planning.replan, "clock moved since baseline")
    after = {k: p.revision for k, p in current(db).items()}
    act("planner_fr", planning.replan, "no change")
    act("planner_fr", planning.replan, "no change")
    assert {k: p.revision for k, p in current(db).items()} == after
    assert after["REDUCED-SCOPE"] == 1 and after["POSTPONE"] == 1
