import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import ui  # noqa: E402
from window36.models import Task  # noqa: E402
from window36.services import approvals, health, planning, qa  # noqa: E402
from window36.services.common import now  # noqa: E402

actor = ui.page("Shutdown Control Room", "Valmont Plant · Extrusion Line 1 · 36-hour shutdown window")
if not ui.require_login(actor):
    st.stop()


def decision_status(s):
    appr = approvals.approved_plan(s)
    if appr is not None:
        if approvals.active_approval(s, appr.id, "readiness"):
            return "✅ Ready to resume production", f"{appr.plan_key} r{appr.revision} complete", appr
        return "🟢 Plan approved", f"{appr.plan_key} r{appr.revision} — {appr.title}", appr
    stale = [p for p in planning.current_plans(s) if p.status == "stale"]
    if stale:
        return "⚠️ Approval stale — replan required", ", ".join(f"{p.plan_key} r{p.revision}" for p in stale), None
    return "⏳ Awaiting decision", "No approved plan for this window", None


SHORT = {
    "✅ Ready to resume production": "Ready",
    "🟢 Plan approved": "Approved",
    "⚠️ Approval stale — replan required": "Stale",
    "⏳ Awaiting decision": "Awaiting",
}

with ui.read() as s:
    t = now(s)
    sd = planning.main_shutdown(s)
    status, detail, approved = decision_status(s)
    plans = planning.current_plans(s)
    changes = planning.changes_since_last_plan(s)
    run = planning.last_run(s)
    alerts = health.alerts(s)
    counters = health.counters(s)
    focus = approved or next((p for p in plans if p.feasible and p.scope == "full" and p.status == "proposed"), None)
    focus = focus or next((p for p in plans if p.feasible), None)
    done = qa.completed_tasks(s, focus.id) if focus else set()

c1, c2, c3 = st.columns(3)
if t < sd.window_start:
    c1.metric("Shutdown starts in", f"{(sd.window_start - t).total_seconds() / 3600:.1f} h", ui.when(sd.window_start))
elif t < sd.window_end:
    c1.metric("Shutdown window ends in", f"{(sd.window_end - t).total_seconds() / 3600:.1f} h", ui.when(sd.window_end))
else:
    c1.metric("Shutdown window", "Closed", ui.when(sd.window_end))
c2.metric("Simulated now", ui.when(t).split(" (")[0], ui.when(t).split(" (")[1].rstrip(")"), delta_color="off")
c3.metric("Decision status", SHORT[status], detail, delta_color="off")
st.markdown(f"### {status}")

left, right = st.columns([3, 2])
with right:
    st.subheader("Active blockers")
    blockers = [(lvl, txt) for lvl, txt in alerts if lvl in ("error", "warning")]
    for p in plans:
        if p.scope == "full" and not p.feasible:
            blockers.append(("blocked", f"**{p.plan_key}** — " + "; ".join(p.reasons)))
    if not blockers:
        st.success("No active blockers")
    for lvl, txt in blockers:
        (st.error if lvl == "error" else st.warning if lvl == "warning" else st.info)(txt)

    st.subheader("Changes since the last plan")
    if run:
        st.caption(f"Last planning run #{run.id} at {ui.when(run.sim_time)} by {run.actor}: {run.trigger}")
    if counters["Documents awaiting review"]:
        st.warning(f"{counters['Documents awaiting review']} supplier document(s) staged, not yet reviewed")
    if not changes:
        st.caption("No material input changes since the last planning run.")
    for ch in changes:
        st.warning(f"`{ch.key}` → v{ch.version}: {ch.reason} ({ch.actor}, {ui.when(ch.sim_time)})")
    if ui.can(actor, "plan.compute", "FR-VAL"):
        if st.button("Replan now", type="primary", key="replan"):
            ui.run_action("Replan", planning.replan, "Manual replan from control room")
            st.rerun()
    else:
        st.caption("Replanning requires the planner or approver role at Valmont.")

with left:
    st.subheader("Timeline")
    if focus is None or not focus.schedule:
        st.error("No feasible plan to show. See blockers.")
    else:
        label = "approved plan" if focus is approved else "best feasible proposal (not approved)"
        st.caption(
            f"Showing {focus.plan_key} r{focus.revision} — {label}. Critical path: " + " → ".join(focus.critical_path)
        )
        rows = []
        for task in focus.schedule:
            rows.append(
                {
                    "Task": f"{task['code']} {task['name']}",
                    "Start": pd.Timestamp(task["start"]).tz_convert(ui.MAIN_TZ).tz_localize(None),
                    "Finish": pd.Timestamp(task["finish"]).tz_convert(ui.MAIN_TZ).tz_localize(None),
                    "Type": "Critical path"
                    if task["code"] in focus.critical_path
                    else ("Needs replacement part" if task["requires_part"] else "Independent"),
                    "Status": "done" if task["code"] in done else "planned",
                }
            )
        df = pd.DataFrame(rows)
        bars = (
            alt.Chart(df)
            .mark_bar(cornerRadius=3)
            .encode(
                x=alt.X("Start:T", title="Valmont local time"),
                x2="Finish:T",
                y=alt.Y("Task:N", sort=None, title=None),
                color=alt.Color(
                    "Type:N",
                    scale=alt.Scale(
                        domain=["Critical path", "Needs replacement part", "Independent"],
                        range=["#d1495b", "#edae49", "#00798c"],
                    ),
                ),
                tooltip=["Task", "Start", "Finish", "Type", "Status"],
            )
        )
        marks = pd.DataFrame(
            {
                "t": [
                    pd.Timestamp(x).tz_convert(ui.MAIN_TZ).tz_localize(None)
                    for x in (focus.window_start, focus.window_end)
                ],
                "label": ["Window opens", "Window closes"],
            }
        )
        rules = alt.Chart(marks).mark_rule(strokeDash=[6, 4], color="gray").encode(x="t:T", tooltip=["label", "t"])
        st.altair_chart((bars + rules).properties(height=320), use_container_width=True)

        f1, f2, f3 = st.columns(3)
        finish = focus.expected_finish.astimezone(ZoneInfo(ui.MAIN_TZ))
        f1.metric(
            "Expected finish (Valmont)", finish.strftime("%a %H:%M"), finish.strftime("%d %b %Z"), delta_color="off"
        )
        f2.metric("Slack to window end", f"{focus.slack_h:.1f} h" if focus.slack_h is not None else "—")
        f3.metric("Estimated cost", f"€{focus.est_cost_eur:,.0f}")

if focus is not None and focus.schedule:
    st.subheader("Task dependencies")
    crit = set(focus.critical_path)
    lines = ['digraph G { rankdir=LR; node [shape=box, style="rounded,filled", fontname=Helvetica];']
    for task in focus.schedule:
        fill = "#f6c1c7" if task["code"] in crit else ("#fbe3b8" if task["requires_part"] else "#cfe8ec")
        mark = " ✓" if task["code"] in done else ""
        lines.append(f'"{task["code"]}" [label="{task["code"]}{mark}\\n{task["name"][:26]}", fillcolor="{fill}"];')
    codes = {task["code"] for task in focus.schedule}
    with ui.read() as s:
        deps = {x.code: x.depends_on for x in s.scalars(select(Task).where(Task.shutdown_id == sd.id))}
    for code in codes:
        for d in deps.get(code, []):
            if d in codes:
                color = "#d1495b" if d in crit and code in crit else "#777777"
                lines.append(f'"{d}" -> "{code}" [color="{color}"];')
    if "PART" in crit:
        lines.append('"PART" [label="Replacement part\\navailable", shape=oval, fillcolor="#f6c1c7"];')
        first = next((c for c in focus.critical_path if c != "PART"), None)
        if first:
            lines.append(f'"PART" -> "{first}" [color="#d1495b"];')
    lines.append("}")
    st.graphviz_chart("\n".join(lines), use_container_width=True)

ui.footer()
