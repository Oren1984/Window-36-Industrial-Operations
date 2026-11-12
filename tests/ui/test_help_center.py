"""Help Center: open to everyone, and every UI label it names exists in the real screens."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402

from window36.services.scenario import DISTURBANCES  # noqa: E402

VIEWS = Path(__file__).resolve().parents[2] / "app" / "views"
SCREENS = ["control_room", "procurement_logistics", "plans_qa", "operations_audit"]

# Labels the guide tells users to click, fill in or look for, and the screen that must contain them.
GUIDE_LABELS = {
    "control_room": ["Replan now", "Active blockers", "Changes since the last plan", "Timeline", "Task dependencies"],
    "procurement_logistics": [
        "Supplier evidence",
        "Review note",
        "Accept facts",
        "Reject document",
        "Stage for review",
        "Record inspection",
        "Stock and reservations",
        "Reservation history",
        "Multimodal routes to Valmont",
        "Sites",
    ],
    "plans_qa": [
        "Option comparison",
        "Inspect plan",
        "Cost estimate",
        "Impact on donor site",
        "Policy pre-check for approval",
        "Decision reason (recorded in the audit trail)",
        "Donor-site release",
        "Approve plan revision",
        "Reject plan revision",
        "Execution, QA evidence and readiness",
        "Simulated work completion",
        '"Done"',
        "Record QA evidence",
        "Evidence reference",
        "Record evidence",
        "Readiness note",
        "Record readiness to resume production",
        "Readiness to resume production",
        "Plan and decision history",
    ],
    "operations_audit": [
        "Scenario controls",
        "Disturbances",
        "Advance time",
        "Reset scenario",
        "Health",
        "Alerts",
        "Audit trail",
        "External events",
        "Failed imports",
        "Demo policy catalog",
    ],
}


def help_source() -> str:
    return (VIEWS / "help_center.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("screen", SCREENS)
def test_every_label_named_in_the_guide_exists_on_its_screen(screen):
    source = (VIEWS / f"{screen}.py").read_text(encoding="utf-8")
    guide = help_source()
    for label in GUIDE_LABELS[screen]:
        assert label in source, f"{label!r} is not on {screen}"
        assert label.strip('"') in guide, f"{label!r} expected in the Help Center"


def test_disturbance_buttons_match_the_guide():
    guide = help_source()
    for d in DISTURBANCES.values():
        if d["label"] != "Wrong part revision at goods-in":
            assert d["label"] in guide


def test_help_center_open_without_sign_in(db):
    at = AppTest.from_file(str(VIEWS / "help_center.py"), default_timeout=60).run()
    assert not at.exception
    assert at.title[0].value == "Help Center"
    headers = [h.value for h in at.header]
    for h in ("What is Window 36?", "Why does it exist?", "What does each screen do?", "What do the statuses mean?"):
        assert h in headers
    assert any("simulation" in i.value.lower() for i in at.info)
    assert len(at.expander) >= 14


def test_help_center_as_read_only_viewer(db):
    at = AppTest.from_file(str(VIEWS / "help_center.py"), default_timeout=60)
    at.session_state["w36_user"] = "viewer"
    at.run()
    assert not at.exception
    assert any("Read-only demo viewer" in s.value for s in at.success)  # signed-in badge in the sidebar
    assert any("Developed and built by Oren Salami V1.0" in c.value for c in at.caption)
