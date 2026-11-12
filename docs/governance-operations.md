# Governance, security and operations

## Demo policy catalog

A small executable catalog in `window36/services/policies.py`. It is a **demo policy model for a
simulator** — it makes no legal, regulatory or certification claim. Each result (policy ID, version,
pass/block, detail) is stored on the decision and written to the audit trail; a refusal names the policy
that blocked it.

| Policy | Trigger | Evidence required | Responsible |
|---|---|---|---|
| POL-REV-001 v1.2 Part revision verification | plan approval, readiness | Physical revision verified at goods-in; allowed by compatibility rules | site operator |
| POL-DONOR-002 v1.0 Donor-site release | approval of a cross-site transfer | Active release by the donor site's approver, bound to the exact plan revision | approver (donor site) |
| POL-SHIPDOC-003 v1.1 Shipping document completeness | donor release, plan approval | Transfer manifest: part, revision, serial, origin, destination, carrier | planner |
| POL-TRANSPORT-004 v0.9 Transport booking (configurable) | plan approval | Simulated carrier slot confirmed | planner |
| POL-CUSTOMS-005 v0.9 Customs/transit (configurable, **off** by default) | plan approval | Customs/transit reference for cross-border moves | planner |
| POL-PROC-006 v1.0 Procurement spend authority | plan approval | Estimated cost within `W36_APPROVER_SPEND_LIMIT_EUR` | approver |
| POL-QA-007 v1.0 QA release | readiness | All plan tasks done; required QA checks passed for this plan revision and installed part revision | approver |

Configuration: `W36_POLICY_CUSTOMS_ENABLED`, `W36_APPROVER_SPEND_LIMIT_EUR`.

## Roles and permissions

Four roles, site-scoped where relevant; enforced in the service layer (`services/common.require`) as well as
by disabled controls in the UI. Every refused attempt is audited.

| Role | Permissions |
|---|---|
| planner | import/review documents, replan, ingest events, scenario controls |
| site_operator | import documents, receiving inspection, work completion, QA evidence (own site) |
| approver | replan, donor-site release (own site's spare), approve/reject, readiness (own site) |
| viewer | read only |

The audit trail distinguishes `donor.release` (donor site) from `readiness.record` (main site) decisions.

## Security controls

* **Authentication** — local demo accounts, bcrypt-hashed passwords; the shared demo password comes from
  `W36_DEMO_PASSWORD`. Roles are re-read from the database on every page run.
* **Secrets** — none in code. Compose has *local-demo* defaults so a fresh clone starts; real values go in a
  git-ignored `.env` (template: `.env.example`). The gitleaks allowlist names only these placeholders.
* **Least privilege** — migrations and seed run as the owner role in a one-shot `migrate` service; the UI
  connects as `w36_app` with DML only (the smoke test proves it cannot run DDL). The database has no
  published port; the UI binds to `127.0.0.1`. The app container runs as a non-root user with all
  capabilities dropped and `no-new-privileges`.
* **Untrusted documents** — `.txt` only, ≤ 64 KB (Streamlit upload limit 1 MB), strict UTF-8, no control
  characters, path components stripped, per-field validation, duplicate uploads are idempotent; failures
  are recorded and counted. Staged facts change nothing until a planner reviews them.
* **Integrity** — plan revisions immutable and `audit_event` append-only (database triggers).
* **Logs** — JSON lines on stdout with correlation ID and status; DSN passwords and secret-like keys are
  redacted.

## Observability

*Operations and Audit* shows application and database health (latency, server version) and counters:
ingestion failures, documents awaiting review, open conflicts, stale plans and approvals, blocked
decisions, pending QA checks, duplicate and out-of-order events, active reservations. Alerts cover
conflicts, stale plans, no feasible plan, cutoffs within three hours and pending QA.

Audit events (business decisions, in the database) and diagnostic logs (stdout) are kept separate. Audit
records carry actor, role, site, simulated and real timestamps, policy ID and version, evidence references,
reason and correlation ID.

## CI (shift-left)

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | Checks |
|---|---|
| lint | `ruff format --check`, `ruff check` (incl. bandit-style `S` rules) |
| test | unit, PostgreSQL integration and Streamlit UI tests against a Postgres service |
| security | gitleaks secret scan (full history), `pip-audit --strict` on runtime dependencies |
| image-and-smoke | image build, Trivy scan (HIGH/CRITICAL with fixes available), Compose smoke test |

Scan exceptions go in `.trivyignore` with a reason. There are none at V1.0.

## Operations

* Start: `docker compose up --build`. Startup is idempotent: migrations take an advisory lock; users and
  the scenario are seeded only if missing.
* Reset the demo: *Operations and Audit → Reset scenario* (planner). The audit trail is retained.
* Persistent data: the `w36_pgdata` volume. Changing database passwords in `.env` requires recreating the
  volume (`docker compose down -v`).
* Health: container health check on `/_stcore/health`; application/database health in the UI;
  `python -m window36.cli smoke` inside the app container.
