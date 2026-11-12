# Demo and verification runbook

## Start

```bash
docker compose up --build
```

UI: <http://localhost:8501> (or `W36_UI_PORT`). Demo password: `window36-demo` unless overridden in `.env`.
The simulated clock starts at **Fri 20 Feb 2026, 03:00 Paris (02:00 UTC)**, four hours before the window
(07:00 Paris Fri → 19:00 Paris Sat).

## Seeded scenario (all fictional)

| Item | Seed |
|---|---|
| Valmont Plant, France (main) | Extrusion Line 1, 36 h window, 9 tasks (T1–T4 independent, T5–T7 need the coupling, T8 QA run, T9 handover); QA inspector 07:00–23:00 local |
| Part W36-CPL-4471 | Rev C and D compatible with Line 1; rev B incompatible (48 mm bore) |
| Original delivery SP-FR-0001 | Supplier notice says rev C, delivery note says rev B → quarantined once reviewed |
| Hessenfeld, Germany | Rev D spare reserved for job GER-MJ-0226; road transfer €3,100, arrives Fri 20:00 Paris |
| Shannamore, Ireland | Rev C spare. Air (road–air–road) €8,900, SNN cargo cutoff 10:00 UTC, arrives Fri 19:30 Paris. Sea (road–sea–road) €2,350, arrives Sat 19:30 Paris — **after** the window |

Disturbances (Operations and Audit, planner only; each is a fixed, idempotent event):

| Disturbance | Effect |
|---|---|
| Wrong part revision | Goods-in observes rev B → original part rejected |
| Road pickup delay | German pickup +16 h → finish Sun 22 Feb, past the window → approved transfer goes stale |
| Missed air cargo cutoff | Irish feeder truck +2.5 h → ready at SNN after the 10:00 UTC cutoff |

## Default workflow (as recorded for the screenshots)

| # | User | Page → action | Expected result |
|---|---|---|---|
| 1 | planner_fr | Procurement and Logistics → note + *Accept facts* on both documents | Conflict on REVISION; SP-FR-0001 quarantined; control room lists the input change |
| 2 | planner_fr | Control Room → *Replan now* | FULL-ORIGINAL blocked; DE road and IE air feasible (finish Sat 11:00, slack 8 h); IE sea blocked with arrival-after-window reason |
| 3 | approver_de | Plans and QA → select FULL-DE-FR-ROAD → reason → *Donor-site release (DE-HSF)* | Release recorded for that plan revision (donor impact shown) |
| 4 | approver_fr | Plans and QA → same plan → reason → *Approve plan revision* | Approved; German spare reserved for the plan |
| 5 | planner_fr | Operations and Audit → *Road pickup delay* | Approval and plan **stale**; spare back to GER-MJ-0226; control room “Approval stale — replan required” |
| 6 | planner_fr | Operations and Audit → *Missed air cargo cutoff*; Control Room → *Replan now* | No full-scope plan fits; reduced scope and postponement feasible |
| 7 | approver_fr | Plans and QA → REDUCED-SCOPE → *Approve* | Approved |
| 8 | approver_fr | *Record readiness* | **Blocked** by POL-QA-007: tasks not completed, QA-RUN evidence missing |
| 9 | operator_fr | Plans and QA → *Done* for T1, T3, T4, T8, T9; QA-RUN pass + evidence ref | Recorded against REDUCED-SCOPE r1 |
| 10 | approver_fr | *Record readiness* | Readiness recorded; audit shows the blocked and the permitted attempts |

Other paths worth showing: record a receiving inspection of rev C (operator_fr) to make the original part the
cheapest plan; sign in as `viewer` to see every control disabled; *Reset scenario* to start over.

## Verification record (V1.0, 2026-11-12)

Local machine: Windows 11, Docker 29.9, Python 3.12.10. After the dependency fix the suite was also run in a `python:3.12-slim` container (61 passed).

| Check | Command | Result |
|---|---|---|
| Format + lint | `ruff format --check . && ruff check .` | pass (47 files) |
| Unit tests | `pytest tests/unit` | **22 passed** |
| PostgreSQL integration tests | `pytest tests/integration` | **35 passed** |
| Streamlit UI workflow tests (AppTest) | `pytest tests/ui` | **4 passed** |
| Compose smoke test | `bash scripts/compose_smoke.sh` | **passed**: clean start, migrate exit 0, UI health ok, seeded data, sea misses window, app role cannot run DDL, DB port not published, state persists across full stop/start |
| UI demonstration in a real browser | headless Microsoft Edge via DevTools protocol against the Compose stack, steps 1–10 above | **completed**; screenshots in `docs/images/` are from this run |
| Secret, dependency and image scans | CI `security` and `image-and-smoke` jobs | run in GitHub Actions only. First run: gitleaks clean; pip-audit and Trivy flagged Pillow 12.3 (via Streamlit 1.60) and Debian zlib. Fixed by moving to Streamlit 1.66.1 + Pillow 13.0.0 and upgrading OS packages in the image; the full suite (61 tests), smoke test and browser demo were re-run on the fixed image |

What the tests cover: road, air and sea evaluation; missed cutoffs (delay and late dispatch); receiving
hours; site time zones and DST; task dependencies, crew calendars and critical path; donor-site impact; the
sea route missing the original deadline; a no-feasible-plan outcome; simultaneous reservations of one spare;
concurrent duplicate approvals; duplicate and out-of-order events (no extra reservation or approval);
restart persistence; immutable plans and append-only audit; viewer write attempts on every service; site
scoping; stale and superseded plan approval; readiness with missing, failed, wrong-revision and
older-revision evidence; document size/format/encoding/field validation.

## Known limitations

* Model-driven document extraction is not included; only the deterministic fixture parser is verified.
* Advancing the clock is not treated as a material input change: an already approved plan is re-checked
  only when someone replans (a replan that changes its outcome marks the approval stale).
* Postponement assumes standard sea supply before the next window without modelling that week's schedule.
* Local demo authentication only (no SSO, no lockout or rate limiting).
* Compose defaults contain documented local-demo credentials so a fresh clone starts; set real values in
  `.env` for anything beyond a local demo.
