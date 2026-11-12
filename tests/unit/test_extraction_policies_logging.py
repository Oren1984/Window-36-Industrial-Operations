import json
import logging

from window36.logging_setup import JsonFormatter, redact, redact_mapping
from window36.services import policies
from window36.services.extraction import DeterministicFixtureExtractor, validate_field


def test_extractor_reads_fields_with_provenance():
    text = "# comment\nDOCUMENT_TYPE: supplier_notice\n\nREVISION: C\nnot a field line\n"
    fields = DeterministicFixtureExtractor().extract(text)
    assert [(f.field, f.value, f.provenance) for f in fields] == [
        ("DOCUMENT_TYPE", "supplier_notice", "line 2"),
        ("REVISION", "C", "line 4"),
    ]


def test_field_validation():
    assert validate_field("REVISION", "C") is None
    assert validate_field("REVISION", "CC")
    assert validate_field("PART_NUMBER", "W36-CPL-4471") is None
    assert validate_field("PART_NUMBER", "w36 cpl; drop table")
    assert validate_field("EXPECTED_ARRIVAL", "2026-02-20T01:30:00+00:00") is None
    assert validate_field("EXPECTED_ARRIVAL", "2026-02-20T01:30:00")  # naive time rejected
    assert validate_field("DOCUMENT_TYPE", "invoice")
    assert validate_field("SOMETHING", "x") == "Unknown field (ignored)"


def test_policy_evaluation_records_which_policy_blocked():
    facts = {"needs_part": True, "revision_status": "disputed", "stock_item_id": "SP-1", "est_cost_eur": 10}
    results = policies.evaluate("plan_approval", facts)
    failed = policies.first_failure(results)
    assert failed["policy_id"] == "POL-REV-001"
    ids = [r["policy_id"] for r in results]
    assert ids == [
        "POL-REV-001",
        "POL-DONOR-002",
        "POL-SHIPDOC-003",
        "POL-TRANSPORT-004",
        "POL-CUSTOMS-005",
        "POL-PROC-006",
    ]


def test_shipping_manifest_completeness():
    facts = {
        "manifest": {
            "part_number": "P",
            "revision": "C",
            "serial": None,
            "origin": "A",
            "destination": "B",
            "carrier": "C",
        }
    }
    failed = policies.first_failure(policies.evaluate("donor_release", facts))
    assert failed["policy_id"] == "POL-SHIPDOC-003" and "serial" in failed["detail"]


def test_customs_policy_disabled_by_default(monkeypatch):
    monkeypatch.delenv("W36_POLICY_CUSTOMS_ENABLED", raising=False)
    results = policies.evaluate("plan_approval", {"cross_border": True})
    r = next(r for r in results if r["policy_id"] == "POL-CUSTOMS-005")
    assert not r["applies"] and r["detail"] == "Disabled by configuration"


def test_secret_redaction():
    assert redact("postgresql+psycopg://user:s3cret@db/x") == "postgresql+psycopg://user:***@db/x"
    assert redact("password=hunter2 token: abc") == "password=*** token: ***"
    assert redact_mapping({"db_password": "x", "nested": {"api_key": "y"}, "ok": "fine"}) == {
        "db_password": "***",
        "nested": {"api_key": "***"},
        "ok": "fine",
    }


def test_json_log_has_correlation_and_redacts():
    rec = logging.LogRecord("window36.t", logging.INFO, __file__, 1, "connect %s", ("postgres://u:pw@h/d",), None)
    rec.fields = {"password": "pw", "status": "ok"}
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "connect postgres://u:***@h/d" and out["password"] == "***"
    assert "correlation_id" in out and out["status"] == "ok"
