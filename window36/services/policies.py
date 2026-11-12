"""Small executable demo policy catalog.

This is a demo policy model for a planning simulator. It makes no legal, regulatory or
certification claim. Each policy has an ID, version, trigger, evidence requirement and
responsible role, and an `evaluate` function over a facts dict. Results are stored with the
decision and in the audit trail.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from window36.config import get_settings


@dataclass(frozen=True)
class Policy:
    id: str
    version: str
    title: str
    triggers: tuple[str, ...]  # donor_release | plan_approval | readiness
    evidence: str
    responsible_role: str
    configurable: bool
    evaluate: Callable[[dict], tuple[bool, str] | None]  # None = not applicable

    def enabled(self) -> bool:
        if self.id == "POL-CUSTOMS-005":
            return get_settings().customs_policy_enabled
        return True


def _rev(f: dict):
    if not f.get("needs_part"):
        return None
    if f.get("revision_status") != "verified":
        return False, f"Part {f.get('stock_item_id')} revision status is '{f.get('revision_status')}', not verified"
    if not f.get("revision_compatible"):
        return False, f"Revision {f.get('part_revision')} is not compatible: {f.get('compatibility_note')}"
    return True, f"Revision {f.get('part_revision')} verified and compatible ({f.get('compatibility_note')})"


def _donor(f: dict):
    if not f.get("cross_site"):
        return None
    rel = f.get("donor_release")
    if not rel:
        return False, f"No active donor-site release from {f.get('source_site_id')} for this plan revision"
    return True, f"Released by {rel['actor']} ({rel['site_id']}) for plan revision {rel['plan_id']}"


_MANIFEST_FIELDS = ("part_number", "revision", "serial", "origin", "destination", "carrier")


def _shipdoc(f: dict):
    manifest = f.get("manifest")
    if manifest is None:
        return None
    missing = [k for k in _MANIFEST_FIELDS if not manifest.get(k)]
    if missing:
        return False, "Transfer manifest incomplete, missing: " + ", ".join(missing)
    return True, "Transfer manifest complete (" + ", ".join(_MANIFEST_FIELDS) + ")"


def _transport(f: dict):
    if not f.get("route_id"):
        return None
    if not f.get("carrier_confirmed"):
        return False, f"Carrier booking for route {f['route_id']} not confirmed (simulated)"
    return True, f"Simulated carrier slot confirmed for {f['route_id']}"


def _customs(f: dict):
    if not f.get("cross_border"):
        return None
    if not f.get("customs_ref"):
        return False, "Cross-border transfer without a customs/transit reference (demo rule)"
    return True, f"Customs/transit reference {f['customs_ref']} present (demo rule)"


def _proc(f: dict):
    limit = get_settings().approver_spend_limit_eur
    cost = f.get("est_cost_eur", 0.0)
    if cost > limit:
        return False, f"Estimated cost €{cost:,.0f} exceeds approver spend authority €{limit:,.0f}"
    return True, f"Estimated cost €{cost:,.0f} within approver spend authority €{limit:,.0f}"


def _qa(f: dict):
    problems = f.get("qa_problems")
    if problems is None:
        return None
    if problems:
        return False, "; ".join(problems)
    return True, "All required tasks complete and QA checks passed for this plan revision and part revision"


CATALOG: list[Policy] = [
    Policy(
        "POL-REV-001",
        "1.2",
        "Part revision verification",
        ("plan_approval", "readiness"),
        "Receiving/QA evidence of the physical part revision; revision allowed by the compatibility rules",
        "site_operator",
        False,
        _rev,
    ),
    Policy(
        "POL-DONOR-002",
        "1.0",
        "Donor-site release",
        ("plan_approval",),
        "Active release by an approver of the donor site, bound to the exact plan revision, donor impact shown",
        "approver (donor site)",
        False,
        _donor,
    ),
    Policy(
        "POL-SHIPDOC-003",
        "1.1",
        "Shipping document completeness",
        ("donor_release", "plan_approval"),
        "Transfer manifest with part number, revision, serial, origin, destination and carrier",
        "planner",
        False,
        _shipdoc,
    ),
    Policy(
        "POL-TRANSPORT-004",
        "0.9",
        "Transport booking check (demo, configurable)",
        ("plan_approval",),
        "Simulated carrier slot confirmation for every leg of the chosen route",
        "planner",
        True,
        _transport,
    ),
    Policy(
        "POL-CUSTOMS-005",
        "0.9",
        "Customs / transit check (demo, configurable, off by default)",
        ("plan_approval",),
        "Customs or transit reference for cross-border transfers",
        "planner",
        True,
        _customs,
    ),
    Policy(
        "POL-PROC-006",
        "1.0",
        "Procurement spend authority",
        ("plan_approval",),
        "Estimated plan cost within the approver's configured spend limit",
        "approver",
        True,
        _proc,
    ),
    Policy(
        "POL-QA-007",
        "1.0",
        "QA release for production restart",
        ("readiness",),
        "All plan tasks complete; required QA checks passed for this plan revision and installed part revision",
        "approver",
        False,
        _qa,
    ),
]


def evaluate(trigger: str, facts: dict) -> list[dict]:
    results = []
    for p in CATALOG:
        if trigger not in p.triggers:
            continue
        if not p.enabled():
            results.append(
                {
                    "policy_id": p.id,
                    "version": p.version,
                    "title": p.title,
                    "applies": False,
                    "passed": True,
                    "detail": "Disabled by configuration",
                    "responsible": p.responsible_role,
                }
            )
            continue
        outcome = p.evaluate(facts)
        if outcome is None:
            results.append(
                {
                    "policy_id": p.id,
                    "version": p.version,
                    "title": p.title,
                    "applies": False,
                    "passed": True,
                    "detail": "Not applicable",
                    "responsible": p.responsible_role,
                }
            )
        else:
            passed, detail = outcome
            results.append(
                {
                    "policy_id": p.id,
                    "version": p.version,
                    "title": p.title,
                    "applies": True,
                    "passed": passed,
                    "detail": detail,
                    "responsible": p.responsible_role,
                }
            )
    return results


def first_failure(results: list[dict]) -> dict | None:
    return next((r for r in results if r["applies"] and not r["passed"]), None)


def catalog_rows() -> list[dict]:
    return [
        {
            "Policy": p.id,
            "Version": p.version,
            "Title": p.title,
            "Triggers": ", ".join(p.triggers),
            "Evidence required": p.evidence,
            "Responsible role": p.responsible_role,
            "Configurable": p.configurable,
            "Enabled": p.enabled(),
        }
        for p in CATALOG
    ]
