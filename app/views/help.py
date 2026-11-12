import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st  # noqa: E402

from app import ui  # noqa: E402
from window36.services.scenario import USERS  # noqa: E402

ui.page("Help", "What Window 36 does and how to run the demo")

st.markdown("""
**Window 36** helps a plant manager decide whether to **start, reduce or postpone** a 36-hour production
shutdown when the critical spare part is in doubt. It connects procurement, inventory, multimodal logistics,
task scheduling, QA and approvals in one workflow.

> **Simulation only.** All sites, parts, carriers, costs and procedures are fictional. Nothing here places
> orders, books carriers, takes payments or controls machinery.

### The situation (simulated 20 Feb 2026, 03:00 Paris time)
* Valmont (France) starts a 36 h shutdown of Extrusion Line 1 in four hours. The coupling delivered for it is
  in doubt: the supplier notice says **rev C**, the delivery note says **rev B** (incompatible).
* Hessenfeld (Germany) has a compatible **rev D** spare, reserved for its own job — a road transfer fits,
  but only after a **donor-site release**.
* Shannamore (Ireland) has a **rev C** spare: **air** (road–air–road) fits; **sea** is cheaper but too late.

### Demo walkthrough
1. **Procurement and Logistics** (as `planner_fr`): review both staged documents → the part is quarantined.
2. **Shutdown Control Room**: *Replan now*. The original part is blocked; compare routes.
3. **Plans and QA**: select `FULL-DE-FR-ROAD`. As `approver_de`, give the donor-site release.
   As `approver_fr`, approve it — the German spare is reserved atomically.
4. **Operations and Audit** (as `planner_fr`): apply *Road pickup delay*. The approval becomes **stale** and
   the spare returns to the German job. Apply *Missed air cutoff*, then replan.
5. **Plans and QA** (as `approver_fr`): no full-scope plan fits — approve **REDUCED-SCOPE**.
6. As `operator_fr`, mark tasks done and record QA evidence; as `approver_fr`, try readiness — it stays
   **blocked** until the required evidence is attached and passes.

Times are shown in each site's local time with UTC in brackets; everything is stored in UTC.
""")

st.markdown("### Roles and demo accounts")
st.caption("All demo accounts share the password configured in `W36_DEMO_PASSWORD` (see `.env.example`).")
st.table([{"Username": u, "Role": r, "Site": s or "all", "Description": d} for u, d, r, s in USERS])
st.markdown("""
| Role | Can |
|---|---|
| Planner | import and review documents, replan, run scenario controls |
| Site operator | import documents, record receiving inspections, task completion and QA evidence (own site) |
| Approver | donor-site release (own site's spares), approve/reject plans and record readiness (own site) |
| Read-only demo viewer | view everything, change nothing |

Permissions are enforced in the service layer as well as in the UI; refused attempts are written to the audit trail.
""")
ui.footer()
