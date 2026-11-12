import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import ui  # noqa: E402
from window36.models import Approval, Plan, QAEvidence  # noqa: E402
from window36.services import approvals, planning, policies, qa  # noqa: E402

actor = ui.page(
    "Plans and QA", "Compare options, approve or reject a plan revision, record QA evidence, verify readiness"
)
if not ui.require_login(actor):
    st.stop()

with ui.read() as s:
    plans = planning.current_plans(s)
    history = s.scalars(select(Plan).order_by(Plan.plan_key, Plan.revision.desc())).all()
    decisions = s.scalars(select(Approval).order_by(Approval.id.desc())).all()
    approved = approvals.approved_plan(s)
    precheck = {}
    current_inputs = {}
    for p in plans:
        trig = "plan_approval"
        precheck[p.id] = policies.evaluate(trig, approvals.approval_facts(s, p))
        current_inputs[p.id] = approvals.inputs_current(s, p)


def fmt_status(p):
    return {
        "approved": "🟢 approved",
        "stale": "⚠️ stale",
        "rejected": "⚪ rejected",
        "superseded": "superseded",
        "proposed": "proposed",
    }[p.status]


st.subheader("Option comparison (current revisions)")
st.dataframe(
    pd.DataFrame(
        [
            {
                "Plan": p.plan_key,
                "Rev": p.revision,
                "Status": fmt_status(p),
                "Feasible": "✓" if p.feasible else "✗",
                "Scope": p.scope,
                "Part": f"{p.source_stock_item_id or '—'} rev {p.part_revision or '—'}",
                "Part available": ui.when(p.part_available_at).split(" (")[0],
                "Expected finish": ui.when(p.expected_finish).split(" (")[0],
                "Slack h": p.slack_h,
                "Est. cost €": round(p.est_cost_eur),
                "Donor impact": (p.donor_impact or {}).get("summary", ""),
                "Blockers": "; ".join(p.reasons),
            }
            for p in plans
        ]
    ),
    hide_index=True,
    use_container_width=True,
)
if not any(p.feasible for p in plans if p.scope == "full"):
    st.error("No full-scope plan fits the shutdown window. Consider reduced scope or postponement.")
if not any(p.feasible for p in plans):
    st.error("No feasible plan at all. Blockers are listed per option above.")

labels = {f"{p.plan_key} r{p.revision} — {p.title}": p for p in plans}
default = next((i for i, p in enumerate(plans) if approved and p.id == approved.id), 0)
choice = st.selectbox("Inspect plan", list(labels), index=default, key="plan_pick")
p = labels[choice]

c1, c2 = st.columns([3, 2])
with c1:
    st.markdown(f"#### {p.plan_key} r{p.revision} · {fmt_status(p)}")
    st.caption(
        f"Computed at {ui.when(p.created_at)} · bound to inputs "
        + ", ".join(f"`{k}` v{v}" for k, v in p.input_versions.items())
    )
    ok, changed = current_inputs[p.id]
    if not ok:
        st.warning("Inputs changed since this revision was computed: " + ", ".join(changed) + " — replan required.")
    for r in p.reasons:
        st.error(r)
    for n in p.notes:
        st.info(n)
    if p.schedule:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Task": t["code"],
                        "Name": t["name"],
                        "Skill": t["skill"],
                        "Start": ui.when(ui.iso(t["start"])),
                        "Finish": ui.when(ui.iso(t["finish"])),
                        "Constraint": t["binding"],
                        "Critical": "●" if t["code"] in p.critical_path else "",
                    }
                    for t in p.schedule
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
with c2:
    st.markdown("**Cost estimate**")
    st.dataframe(
        pd.DataFrame(
            [{"Item": k, "€": round(v)} for k, v in p.cost_breakdown.items()]
            + [{"Item": "Total", "€": round(p.est_cost_eur)}]
        ),
        hide_index=True,
    )
    if p.donor_impact:
        st.markdown("**Impact on donor site**")
        st.warning(p.donor_impact["summary"])
    st.markdown("**Policy pre-check for approval**")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Policy": r["policy_id"],
                    "v": r["version"],
                    "Result": "n/a" if not r["applies"] else ("pass" if r["passed"] else "BLOCK"),
                    "Detail": r["detail"],
                }
                for r in precheck[p.id]
            ]
        ),
        hide_index=True,
    )

st.markdown("#### Decision")
reason = st.text_input("Decision reason (recorded in the audit trail)", key="decision_reason")
b1, b2, b3 = st.columns(3)
cross_site = p.scope == "full" and p.source_site_id and p.source_site_id != "FR-VAL"
if cross_site:
    if b1.button(
        f"Donor-site release ({p.source_site_id})",
        key="donor_release",
        disabled=not ui.can(actor, "donor.release", p.source_site_id),
    ):
        ui.run_action("Donor-site release", approvals.donor_release, p.id, reason)
        st.rerun()
if b2.button(
    "Approve plan revision", key="approve", type="primary", disabled=not ui.can(actor, "plan.approve", "FR-VAL")
):
    ui.run_action("Approve plan", approvals.approve_plan, p.id, reason)
    st.rerun()
if b3.button("Reject plan revision", key="reject", disabled=not ui.can(actor, "plan.reject", "FR-VAL")):
    ui.run_action("Reject plan", approvals.reject_plan, p.id, reason)
    st.rerun()
if not (ui.can(actor, "plan.approve", "FR-VAL") or ui.can(actor, "donor.release")):
    st.caption(
        "Decisions require an approver. Donor release needs the approver of the donor site; plan approval "
        "and readiness need the Valmont approver."
    )

st.divider()
st.subheader("Execution, QA evidence and readiness")
if approved is None:
    st.info("No approved plan. Work completion and QA evidence are recorded against an approved plan revision.")
else:
    with ui.read() as s:
        done = qa.completed_tasks(s, approved.id)
        evidence = s.scalars(
            select(QAEvidence)
            .where(QAEvidence.plan_id.in_(select(Plan.id).where(Plan.plan_key == approved.plan_key)))
            .order_by(QAEvidence.id.desc())
        ).all()
        problems = qa.readiness_problems(s, approved)
        ready = approvals.active_approval(s, approved.id, "readiness")
    st.markdown(f"Approved plan **{approved.plan_key} r{approved.revision}** — {approved.title}")
    e1, e2 = st.columns(2)
    with e1:
        st.markdown("**Simulated work completion**")
        for task in sorted(approved.schedule, key=lambda x: x["start"]):
            cols = st.columns([4, 1])
            cols[0].write(f"{'✅' if task['code'] in done else '⬜'} {task['code']} {task['name']}")
            if task["code"] not in done and cols[1].button(
                "Done", key=f"done_{task['code']}", disabled=not ui.can(actor, "work.record", "FR-VAL")
            ):
                ui.run_action(f"Complete {task['code']}", qa.record_work, approved.id, task["code"])
                st.rerun()
    with e2:
        st.markdown("**Record QA evidence**")
        req = qa.required_checks(approved)
        with st.form("qa_form"):
            check = st.selectbox("Check", req or ["—"], format_func=lambda c: f"{c} · {qa.CHECKS.get(c, '')}")
            result = st.radio("Result", ["pass", "fail"], horizontal=True)
            part_rev = st.text_input("Installed part revision", value=approved.part_revision or "", max_chars=1)
            item = st.text_input("Installed spare id", value=approved.source_stock_item_id or "")
            ref = st.text_input("Evidence reference", placeholder="QA-RUN-0221-01")
            if st.form_submit_button("Record evidence", disabled=not ui.can(actor, "qa.record", "FR-VAL") or not req):
                ui.run_action(
                    "Record QA evidence",
                    qa.record_qa_evidence,
                    approved.id,
                    check,
                    result,
                    ref,
                    part_rev or None,
                    item or None,
                )
                st.rerun()
    if evidence:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Plan id": e.plan_id,
                        "Check": e.check_code,
                        "Result": e.result.upper(),
                        "Part rev": e.part_revision,
                        "Spare": e.stock_item_id,
                        "Evidence": e.evidence_ref,
                        "By": e.actor,
                        "At": ui.when(e.sim_time),
                    }
                    for e in evidence
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
    st.markdown("**Readiness to resume production** (POL-QA-007)")
    if ready:
        st.success(f"Readiness recorded by {ready.actor} at {ui.when(ready.sim_time)} — {ready.reason}")
    else:
        for prob in problems:
            st.error(prob)
        if not problems:
            st.success("All required tasks and QA evidence are complete for this plan revision.")
        note = st.text_input("Readiness note", key="ready_note")
        if st.button(
            "Record readiness to resume production",
            key="readiness",
            disabled=not ui.can(actor, "readiness.record", "FR-VAL"),
        ):
            ui.run_action("Record readiness", qa.record_readiness, approved.id, note)
            st.rerun()

st.divider()
st.subheader("Plan and decision history")
h1, h2 = st.columns(2)
with h1:
    st.caption("All plan revisions (immutable; only status changes)")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Plan": x.plan_key,
                    "Rev": x.revision,
                    "Status": x.status,
                    "Feasible": x.feasible,
                    "Computed": ui.when(x.created_at).split(" (")[0],
                    "Note": x.status_reason or "",
                }
                for x in history
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )
with h2:
    st.caption("Decisions (donor release, approval, rejection, readiness)")
    by_id = {x.id: x for x in history}
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Decision": d.decision_type,
                    "Plan": f"{by_id[d.plan_id].plan_key} r" f"{by_id[d.plan_id].revision}",
                    "Site": d.site_id,
                    "By": d.actor,
                    "Status": d.status,
                    "At": ui.when(d.sim_time).split(" (")[0],
                    "Note": d.status_reason or "",
                }
                for d in decisions
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )

ui.footer()
