"""Help Center: what Window 36 is, why it exists, and how to use it. Open to everyone, signed in or not."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st  # noqa: E402

from app import ui  # noqa: E402
from window36.services.scenario import USERS  # noqa: E402

IMAGES = ui.ROOT / "docs" / "images"


def screenshot(name: str, caption: str) -> None:
    path = IMAGES / name
    if path.exists():  # screenshots are shipped in the image; skip quietly if a build left them out
        st.image(str(path), caption=caption)


ui.page("Help Center", "Start here: what Window 36 is, why it exists, and how to use it")

st.info(
    "**Everything here is a simulation.** The plants, parts, carriers, people, times and costs are fictional. "
    "Buttons that say *release*, *approve*, *reserve* or *ship* only change records in this demo database — "
    "nothing is ordered, booked, paid for or switched on."
)

tab_what, tab_why, tab_how, tab_screens, tab_status, tab_accounts = st.tabs(
    ["What is it?", "Why it exists", "How do I use it?", "Screens", "Statuses", "Accounts & roles"]
)

# ---------------------------------------------------------------- 1. What is Window 36?
with tab_what:
    st.header("What is Window 36?")
    st.markdown("""
A factory line can't run forever without maintenance. **Valmont Plant** (France) has booked a
**36-hour planned shutdown** of *Extrusion Line 1*: the line stops on **Friday 07:00** (Paris time) and must be
handed back to production by **Saturday 19:00**. The main job in that window is replacing a worn
**gearbox coupling** (part *W36-CPL-4471*).

Window 36 helps the plant manager answer one question when that part is in doubt:

| Option | What it means |
|---|---|
| **Start as planned** | Do the full work package, using the original part or a spare brought in from another plant. |
| **Reduce the scope** | Do only the jobs that don't need the coupling now; replace the coupling in a later window. |
| **Postpone** | Don't stop the line this week; do the whole job in the next window. |

Three fictional plants share spares:

* **Valmont Plant, France** — the main plant, where the shutdown happens.
* **Hessenfeld Plant, Germany** — has a compatible spare (rev D), but it is **reserved for its own job**.
* **Shannamore Plant, Ireland** — has a compatible spare (rev C) that can travel by **air** or **sea**.

For every option the system shows whether it fits the 36 hours, why not if it doesn't, what it costs and who else
it affects. Decisions need the right person to approve them, and every step is written to an audit trail.
""")

# ---------------------------------------------------------------- 2. Why does it exist?
with tab_why:
    st.header("Why does it exist?")
    st.markdown("""
In real plants this decision is usually made over phone calls, emails and spreadsheets — under time pressure.
Small facts ripple quickly. A short example from the seeded demo:
""")
    st.markdown("""
1. **03:00, four hours before the stop.** The supplier's notice says the new coupling is **rev C**.
   The delivery note on the pallet says **rev B** — and rev B does not fit Line 1.
   Until someone checks the physical part, it can't be used.
2. **The obvious rescue** is Hessenfeld's spare. But that spare is **reserved for Hessenfeld's own repair**
   on Press Line 2. Taking it delays *their* job by about four days. Their manager has to agree.
3. **The truck is then delayed 16 hours.** The part now arrives too late: the coupling swap, alignment and the
   3-hour QA run test can't all finish before Saturday 19:00 — and the QA inspector only works 07:00–23:00.
   The approval given an hour ago is now based on facts that are no longer true.
4. **The air option from Ireland** misses its cargo cutoff, and **sea** is cheap but arrives after the window.

Window 36 keeps all of this in one place: it recomputes the options when a fact changes, marks earlier
approvals **stale** instead of letting them silently stand, gives the borrowed spare back to Hessenfeld, and won't
let anyone declare the line ready until the required QA evidence is recorded and has passed.
""")

# ---------------------------------------------------------------- 3. How do I use it?
with tab_how:
    st.header("How do I use it? — the seeded demo, step by step")
    st.markdown("""
Sign in from the **sidebar** (accounts are on the *Accounts & roles* tab). Switching user: **Sign out**, then sign
in again. Move between screens with the sidebar links — reloading the browser tab signs you out.

The demo clock starts at **Fri 20 Feb 03:00 Paris time** and only moves when someone presses **Advance time**.
To start over, a planner can use **Reset scenario** on *Operations and Audit*.

**Read-only viewer?** You can follow every step: open the screen named in each step and look at the result
described. The buttons will be greyed out for you.
""")

    with st.expander("Step 1 — Review the supplier discrepancy  ·  planner_fr", expanded=True):
        st.markdown("""
1. Open **Procurement and Logistics** → **Supplier evidence**. A red box reports a **Conflict on REVISION** for
   delivery *DEL-FR-0220-01*.
2. Each staged document (🟡) is shown open. Its table shows every extracted field and the **line of the file it came
   from** (*Provenance*). The supplier notice says **C**; the delivery note says **B**.
3. For each document, enter a **Review note** and press **Accept facts**.

**Result:** the original part *SP-FR-0001* is quarantined (see **Stock and reservations**). On
**Shutdown Control Room**, **Changes since the last plan** now lists `stock:SP-FR-0001`.
""")

    with st.expander("Step 2 — Recompute and compare road, air and sea  ·  planner_fr"):
        st.markdown("""
1. On **Shutdown Control Room** press **Replan now**.
2. On **Procurement and Logistics** scroll to **Multimodal routes to Valmont**. Each route shows every leg with its
   *Ready*, *Departs*, *Arrives* and *Cutoff* times in the local time of that place (UTC in brackets):
   * 🟢 **Hessenfeld → Valmont, road** — €3,100; part ready at Valmont Fri 21:00.
   * 🟢 **Shannamore → Shannon → Paris CDG → Valmont, road–air–road** — €8,900; part ready Fri 20:30.
   * 🔴 **Shannamore → Rosslare → Cherbourg → Valmont, road–sea–road** — €2,350, but the part is only available
     Sat 20:30, **after the window ends** (Sat 19:00).
3. On **Plans and QA**, **Option comparison** lists all six options side by side: finish time, slack, cost,
   donor impact and blockers.
""")
        screenshot("02-route-comparison.png", "Multimodal routes: road and air fit, sea arrives too late")

    with st.expander("Step 3 — Inspect the impact on the donor plant  ·  anyone"):
        st.markdown("""
On **Plans and QA**, pick **FULL-DE-FR-ROAD** in **Inspect plan**. Under **Impact on donor site**:
Hessenfeld job *GER-MJ-0226* (Press Line 2) loses its spare; a replacement takes 10 days, so that job slips about
4 days, estimated at €4,200. That cost is included in the plan's **Cost estimate** (total €7,300).

The **Policy pre-check for approval** table shows **POL-DONOR-002 → BLOCK**: Hessenfeld hasn't released the spare yet.
""")

    with st.expander("Step 4 — Release the spare and approve the plan  ·  approver_de, then approver_fr"):
        st.markdown("""
1. Sign in as **approver_de**. On **Plans and QA**, choose **FULL-DE-FR-ROAD**, write a
   **Decision reason (recorded in the audit trail)** and press **Donor-site release (DE-HSF)**.
2. Sign in as **approver_fr**. Choose the same plan, write a reason and press **Approve plan revision**.

**Result:** **Shutdown Control Room** shows **Plan approved**, the task **Timeline** and the critical path.
On **Procurement and Logistics → Stock and reservations**, *SP-DE-0001* is now *Claimed by* the plan instead of
Hessenfeld's job.

Approving before the release is refused, and the refusal names **POL-DONOR-002**.
""")
        screenshot("01-control-room.png", "Control room after approval: timeline, critical path, blockers")

    with st.expander("Step 5 — Trigger the shipment delay and see the approval go stale  ·  planner_fr"):
        st.markdown("""
1. Sign in as **planner_fr**. On **Operations and Audit → Scenario controls → Disturbances**, press
   **Road pickup delay (Hessenfeld → Valmont)** (the truck is held 16 hours).
2. Open **Shutdown Control Room**: the status is **⚠️ Approval stale — replan required**.

**Why?** The approval was given for one exact plan revision, computed from one version of the road route. The
route has changed (**Changes since the last plan** shows `route:DE-FR-ROAD → v2` with the reason), so the
approval no longer stands. The German spare goes back to job *GER-MJ-0226* automatically.

Pressing **Approve plan revision** on the stale plan is refused.
""")
        screenshot("03-stale-approval.png", "After the road delay: approval stale, replan required")

    with st.expander("Step 6 — Compare the revised plans  ·  planner_fr, then approver_fr"):
        st.markdown("""
1. On **Shutdown Control Room** press **Replan now**. The new road plan revision is blocked — it would finish
   on Sunday, after the window. The **air** option from Ireland still fits at this point.
2. To follow the demo story, go to **Operations and Audit** and press
   **Missed air cargo cutoff (Shannamore → Shannon)**, then **Replan now** again.
3. Now **No full-scope plan fits the shutdown window** (see **Active blockers**). **REDUCED-SCOPE** and
   **POSTPONE** still fit.
4. Sign in as **approver_fr**. On **Plans and QA**, choose **REDUCED-SCOPE**, write a reason and press
   **Approve plan revision**. (Postponement is the alternative if you'd rather not stop the line this week.)

Old revisions stay visible under **Plan and decision history**.
""")

    with st.expander("Step 7 — Check what QA evidence is needed before readiness  ·  approver_fr, operator_fr"):
        st.markdown("""
1. As **approver_fr**, on **Plans and QA → Execution, QA evidence and readiness**, write a **Readiness note** and
   press **Record readiness to resume production**. It is **blocked**, and the red boxes say why:
   *Tasks not completed: T1, T3, T4, T8, T9* and *QA-RUN: evidence missing*.
2. Sign in as **operator_fr**. Under **Simulated work completion** press **Done** for each task, top to bottom
   (a task can't be completed before the tasks it depends on).
3. Under **Record QA evidence** keep **Check = QA-RUN**, **Result = pass**, type an **Evidence reference**
   (for example `QA-RUN-0221-01`) and press **Record evidence**.
4. Sign in as **approver_fr** again and press **Record readiness to resume production** — this time it is recorded.

A **fail** result, a missing check, or evidence recorded against an older plan revision keeps readiness blocked.
Full-scope plans need three checks (**QA-RCV**, **QA-ALIGN**, **QA-RUN**) and the installed part revision and
spare id must match the plan.
""")
        screenshot("04-qa-readiness-blocked.png", "Readiness blocked until the tasks and QA evidence are complete")

# ---------------------------------------------------------------- 4. What does each screen do?
with tab_screens:
    st.header("What does each screen do?")
    st.caption("Everyone who is signed in can see every screen. Buttons you are not allowed to use are greyed out.")

    with st.expander("🕹️ Shutdown Control Room — “where do we stand right now?”", expanded=True):
        st.markdown("""
**Use it** first and often: it is the one-page summary.

**Look at:** the countdown to the window, the simulated time, the **Decision status** (Awaiting / Approved /
Stale / Ready), **Active blockers**, **Changes since the last plan**, the task **Timeline** (red bars are the
critical path) and **Task dependencies**.

| Role | Can do here |
|---|---|
| planner, approver (Valmont) | **Replan now** |
| site operator, viewer | look only |

**Viewer:** follows every step of the demo here.
""")
    with st.expander("🚚 Procurement and Logistics — “what parts exist and how can they get here?”"):
        st.markdown("""
**Use it** to check supplier paperwork, stock and transport.

**Look at:** **Sites** (local time and receiving hours), **Stock and reservations** (who has *claimed* each spare),
**Supplier evidence** (documents, extracted fields with provenance, conflicts) and **Multimodal routes to
Valmont** (each leg's times and cutoff, and why a route fails).

| Role | Can do here |
|---|---|
| planner (Valmont) | **Accept facts** / **Reject document**; **Stage for review** a new `.txt` document |
| site operator | **Stage for review** a document; **Record inspection** (observed revision at goods-in) |
| approver, viewer | look only |

**Viewer:** can follow steps 1–2; reviewing needs **planner_fr**.
""")
    with st.expander("✅ Plans and QA — “which option, and is the line safe to restart?”"):
        st.markdown("""
**Use it** to compare options, decide, and track the work and QA evidence.

**Look at:** **Option comparison**, then pick one in **Inspect plan** to see its schedule, **Cost estimate**,
**Impact on donor site** and **Policy pre-check for approval**. Lower down: **Execution, QA evidence and
readiness** and **Plan and decision history**.

| Role | Can do here |
|---|---|
| approver of the donor plant (approver_de / approver_ie) | **Donor-site release** for its own spare |
| approver (Valmont) | **Approve plan revision**, **Reject plan revision**, **Record readiness to resume production** |
| site operator (Valmont) | **Done** on tasks, **Record evidence** for QA |
| planner, viewer | look only |

**Viewer:** can follow steps 3, 6 and 7 here; deciding needs an approver, task and QA records need an operator.
""")
    with st.expander("📋 Operations and Audit — “what happened, who did it, and is the system healthy?”"):
        st.markdown("""
**Use it** to run the demo's clock and disturbances, and to check the record.

**Look at:** **Health** (application and database), the counters (stale plans, blocked decisions, pending QA, …),
**Alerts**, the **Audit trail** (every action, including refused ones and the policy that refused them),
**External events**, **Failed imports** and the **Demo policy catalog**.

| Role | Can do here |
|---|---|
| planner (Valmont) | **Advance time**, the three **Disturbances**, **Reset scenario** |
| everyone else | look only |

**Viewer:** can read everything here; the scenario buttons need **planner_fr**.
""")
    with st.expander("❓ Help Center — this page"):
        st.markdown("Open to everyone, even before signing in.")

# ---------------------------------------------------------------- 5. What do the statuses mean?
with tab_status:
    st.header("What do the statuses mean?")
    st.caption("All statuses describe simulated records in this demo, not real equipment or shipments.")

    with st.expander("🔴 Blocked plan (✗ in the *Feasible* column)", expanded=True):
        st.markdown("""
**What it means:** this option cannot work as computed. It can't be approved.

**Typical causes:** the part is quarantined or the wrong revision; a shipment misses a cutoff; the part arrives
after the window ends; the work would finish after the window; no later window exists.

**Evidence:** the *Blockers* column in **Option comparison**, the red boxes under **Inspect plan**, the leg
*Reason* on **Multimodal routes to Valmont**, and **Active blockers** on the control room.

**What to do:** pick another option, or fix the cause (for example record a goods-in inspection) and press
**Replan now**. If every option is blocked, the control room says **No feasible plan at all — escalate**.
""")
    with st.expander("⚠️ Stale plan / stale approval"):
        st.markdown("""
**What it means:** something the plan depended on has changed since it was computed or approved — a route
delay, a part found at goods-in, reviewed documents. The old decision no longer counts.

**Caused by:** a material input change (shown as `stock:…`, `route:…`, `tasks:…` with a new version number).

**Evidence:** **Changes since the last plan** on the control room; *status* `stale` in **Plan and decision
history**; `approval.stale` entries in the **Audit trail**.

**What to do:** press **Replan now**, compare the new revisions, and approve again. If the stale plan had
reserved a spare, the spare has already been returned to its previous holder.
""")
    with st.expander("📦 Stock reservations (“Claimed by”)"):
        st.markdown("""
**What it means:** each spare can have **one active claim** at a time — either a site's own job
(`site_job: GER-MJ-0226`) or an approved plan (`plan: FULL-DE-FR-ROAD/r2`).

**Caused by:** approving a transfer plan moves the claim from the donor job to the plan (only after the donor
plant's release). A stale approval moves it back. Readiness marks the spare as used (`consumed`).

**Evidence:** **Procurement and Logistics → Stock and reservations** and the **Reservation history** below it.

**What to do:** nothing by hand — claims follow the decisions. A second plan cannot claim a spare that is
already claimed.
""")
    with st.expander("✅ QA release and readiness to resume production"):
        st.markdown("""
**What it means:** the approver at Valmont confirms the line can restart. Policy **POL-QA-007** only allows it
when every task in the approved plan is done and every required QA check has a **pass** recorded against
**this plan revision** (and, for full scope, the installed part revision and spare id).

**Blocked by:** unfinished tasks, a missing check, a **failed** check, evidence for a different part revision,
or evidence recorded against an older revision of the plan (*stale evidence*).

**Evidence:** the red boxes under **Readiness to resume production**, the QA evidence table, and
`readiness.record` entries (blocked and permitted) in the **Audit trail**.

**What to do:** have the site operator finish the tasks and record passing evidence, then try again.
""")
    with st.expander("Other labels you will see"):
        st.markdown("""
| Label | Meaning |
|---|---|
| *proposed* | Computed and waiting for a decision |
| *approved* | The current approved plan (only one at a time) |
| *rejected* | Turned down by an approver, with a reason |
| *superseded* | Replaced by a newer revision of the same option |
| *quarantine* (stock) | Part on hold because its documents disagree; needs a goods-in inspection |
| *rejected* (stock) | Goods-in found an incompatible revision |
| 🟡 staged (document) | Read by the system, waiting for a planner's review |
| 🟢 accepted / ⚪ rejected (document) | Reviewed and applied / reviewed and ignored |
| 🔴 failed (document) | Could not be read (wrong format, too large, missing fields) |
| *duplicate*, *out_of_order* (events) | A repeated or older carrier message that was ignored on purpose |
""")

# ---------------------------------------------------------------- Accounts and roles
with tab_accounts:
    st.header("Accounts and roles")
    st.caption(
        "All demo accounts share one password: `window36-demo` by default, or the value of "
        "W36_DEMO_PASSWORD in your .env file."
    )
    st.table([{"Username": u, "Role": r, "Site": s or "all sites", "Description": d} for u, d, r, s in USERS])
    st.markdown("""
| Role | In short |
|---|---|
| **Planner** | Reviews documents, recomputes plans, runs the demo clock and disturbances |
| **Site operator** | Records goods-in inspections, task completion and QA evidence at their own plant |
| **Approver** | Releases their own plant's spares, approves or rejects plans and records readiness at their own plant |
| **Read-only demo viewer** | Sees everything, changes nothing |

Permissions are checked by the system itself, not just by greyed-out buttons. A refused attempt is recorded in
the **Audit trail** with the reason.
""")

ui.footer()
