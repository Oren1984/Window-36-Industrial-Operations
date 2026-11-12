import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import ui  # noqa: E402
from window36.models import AuditEvent, Document, ExternalEvent  # noqa: E402
from window36.services import health, policies, scenario  # noqa: E402

actor = ui.page("Operations and Audit", "Health, counters, alerts, audit trail, events and scenario controls")
if not ui.require_login(actor):
    st.stop()

db = health.db_health(ui.engine())
with ui.read() as s:
    counters = health.counters(s)
    alerts = health.alerts(s)
    audit_rows = s.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(500)).all()
    events = s.scalars(select(ExternalEvent).order_by(ExternalEvent.id.desc())).all()
    failed = s.scalars(select(Document).where(Document.status == "failed").order_by(Document.id.desc())).all()

st.subheader("Health")
h1, h2, h3 = st.columns(3)
h1.metric("Application", "OK")
h2.metric(
    "Database",
    "OK" if db["ok"] else "DOWN",
    f"{db.get('latency_ms')} ms" if db["ok"] else db.get("error"),
    delta_color="off",
)
h3.metric("PostgreSQL", db.get("server", "—"))
cols = st.columns(5)
for i, (k, v) in enumerate(counters.items()):
    cols[i % 5].metric(k, v)

st.subheader("Alerts")
if not alerts:
    st.success("No alerts")
for lvl, txt in alerts:
    {"error": st.error, "warning": st.warning}.get(lvl, st.info)(txt)

st.subheader("Scenario controls")
allowed = ui.can(actor, "scenario.control", "FR-VAL")
if not allowed:
    st.caption("Scenario controls are limited to the Valmont planner.")
c1, c2, c3 = st.columns(3)
with c1:
    st.markdown("**Simulated clock**")
    hrs = st.number_input("Hours", min_value=0.5, max_value=48.0, value=1.0, step=0.5, key="adv_hours")
    if st.button("Advance time", key="advance", disabled=not allowed):
        ui.run_action("Advance clock", scenario.advance_clock, float(hrs))
        st.rerun()
with c2:
    st.markdown("**Disturbances** (reproducible, idempotent events)")
    for code, d in scenario.DISTURBANCES.items():
        if st.button(d["label"], key=f"dist_{code}", help=d["description"], disabled=not allowed):
            ui.run_action(d["label"], scenario.apply_disturbance, code)
            st.rerun()
with c3:
    st.markdown("**Reset scenario**")
    confirm = st.checkbox("I want to reset the demo to its opening state", key="reset_confirm")
    if st.button("Reset scenario", key="reset", disabled=not (allowed and confirm)):
        ui.run_action("Reset scenario", scenario.reset_scenario)
        st.rerun()

st.subheader("Audit trail")
st.caption("Business audit events (append-only in the database). Diagnostic logs are separate JSON lines on stdout.")
f1, f2 = st.columns(2)
outcomes = f1.multiselect("Outcome", ["applied", "permitted", "blocked", "info"], key="aud_outcome")
action_q = f2.text_input("Action contains", key="aud_action", placeholder="e.g. donor.release, readiness, approve")
rows = [a for a in audit_rows if (not outcomes or a.outcome in outcomes) and (action_q.lower() in a.action.lower())]
st.dataframe(
    pd.DataFrame(
        [
            {
                "#": a.id,
                "Sim time": ui.when(a.sim_time).split(" (")[0],
                "Actor": a.actor,
                "Role": a.actor_role,
                "Site": a.site_id or "",
                "Action": a.action,
                "Outcome": a.outcome,
                "Entity": f"{a.entity_type}:{a.entity_id}",
                "Policy": f"{a.policy_id} v{a.policy_version}" if a.policy_id else "",
                "Reason": a.reason,
                "Evidence": ", ".join(a.evidence_refs),
                "Correlation": a.correlation_id,
            }
            for a in rows
        ]
    ),
    hide_index=True,
    use_container_width=True,
    height=360,
)

e1, e2 = st.columns(2)
with e1:
    st.subheader("External events")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Event": e.event_id,
                    "Entity": e.entity_key,
                    "Seq": e.sequence,
                    "Kind": e.kind,
                    "Outcome": e.outcome,
                    "Duplicates ignored": e.duplicate_count,
                    "Note": e.note or "",
                }
                for e in events
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )
with e2:
    st.subheader("Failed imports")
    if not failed:
        st.caption("None")
    else:
        st.dataframe(
            pd.DataFrame([{"#": d.id, "File": d.filename, "Error": d.error, "By": d.uploaded_by} for d in failed]),
            hide_index=True,
            use_container_width=True,
        )

st.subheader("Demo policy catalog")
st.caption("A demo policy model for this simulator — not a legal, regulatory or certification claim.")
st.dataframe(pd.DataFrame(policies.catalog_rows()), hide_index=True, use_container_width=True)

ui.footer()
