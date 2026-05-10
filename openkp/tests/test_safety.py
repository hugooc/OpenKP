"""Tests for openkp.safety — audit log + dry-run scaffolding."""

from __future__ import annotations

import json
from pathlib import Path

from openkp.safety import (
    DRY_RUN_ENV,
    audit_log_event,
    is_dry_run,
    redact_for_audit,
)

# --- is_dry_run ---


def test_is_dry_run_unset(monkeypatch):
    monkeypatch.delenv(DRY_RUN_ENV, raising=False)
    assert is_dry_run() is False


def test_is_dry_run_truthy_values(monkeypatch):
    for value in ["1", "true", "TRUE", "yes", "on", "  1  "]:
        monkeypatch.setenv(DRY_RUN_ENV, value)
        assert is_dry_run() is True, f"value {value!r} should be truthy"


def test_is_dry_run_falsy_values(monkeypatch):
    for value in ["0", "false", "no", "off", "", "anything-else"]:
        monkeypatch.setenv(DRY_RUN_ENV, value)
        assert is_dry_run() is False, f"value {value!r} should be falsy"


# --- redact_for_audit ---


def test_redact_drops_top_level_sensitive_keys():
    out = redact_for_audit({
        "medication_id": "225...",
        "walletPaymentToken": "secret-token",
        "last4Digit": "2000",
    })
    assert out["medication_id"] == "225..."
    assert out["walletPaymentToken"] == "[redacted]"
    assert out["last4Digit"] == "[redacted]"


def test_redact_is_case_insensitive_on_keys():
    out = redact_for_audit({"WALLETPAYMENTTOKEN": "x", "last_4_digit": "y"})
    assert out["WALLETPAYMENTTOKEN"] == "[redacted]"
    assert out["last_4_digit"] == "[redacted]"


def test_redact_walks_nested_dicts():
    out = redact_for_audit({
        "outer": {"inner": {"email": "x@y.z", "fine": "value"}}
    })
    assert out["outer"]["inner"]["email"] == "[redacted]"
    assert out["outer"]["inner"]["fine"] == "value"


def test_redact_walks_lists():
    out = redact_for_audit([{"email": "a"}, {"email": "b"}])
    assert out == [{"email": "[redacted]"}, {"email": "[redacted]"}]


def test_redact_drops_subtree_when_parent_key_matches():
    """`creditCardDetails: {...}` becomes `[redacted]` — we don't walk into it."""
    out = redact_for_audit({
        "creditCardDetails": {"walletPaymentToken": "x", "fine": "y"}
    })
    assert out["creditCardDetails"] == "[redacted]"


def test_redact_passes_primitives_through():
    assert redact_for_audit("hello") == "hello"
    assert redact_for_audit(42) == 42
    assert redact_for_audit(None) is None
    assert redact_for_audit([1, 2, 3]) == [1, 2, 3]


def test_redact_does_not_mutate_input():
    src = {"email": "x@y.z", "ok": [{"email": "a"}]}
    redact_for_audit(src)
    assert src == {"email": "x@y.z", "ok": [{"email": "a"}]}


# --- audit_log_event ---


def test_audit_log_writes_one_jsonl_line(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(DRY_RUN_ENV, raising=False)
    audit_log_event(
        tmp_path,
        tool="request_refill",
        phase="intent",
        fields={"medication_id": "111", "rx_number": "111"},
    )

    lines = (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["tool"] == "request_refill"
    assert event["phase"] == "intent"
    assert event["dry_run"] is False
    assert event["medication_id"] == "111"
    assert "ts" in event


def test_audit_log_records_dry_run_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(DRY_RUN_ENV, "1")
    audit_log_event(tmp_path, tool="request_refill", phase="intent")
    event = json.loads((tmp_path / "audit.log").read_text(encoding="utf-8"))
    assert event["dry_run"] is True


def test_audit_log_redacts_sensitive_fields_in_event(tmp_path: Path):
    audit_log_event(
        tmp_path,
        tool="request_refill",
        phase="result",
        fields={
            "order_number": "030abc",
            "walletPaymentToken": "secret",
            "creditCardDetails": {"last4Digit": "2000"},
        },
    )
    event = json.loads((tmp_path / "audit.log").read_text(encoding="utf-8"))
    assert event["order_number"] == "030abc"
    assert event["walletPaymentToken"] == "[redacted]"
    assert event["creditCardDetails"] == "[redacted]"


def test_audit_log_appends_subsequent_events(tmp_path: Path):
    audit_log_event(tmp_path, tool="t", phase="intent")
    audit_log_event(tmp_path, tool="t", phase="result")
    lines = (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["phase"] == "intent"
    assert json.loads(lines[1])["phase"] == "result"


def test_audit_log_swallows_filesystem_errors(tmp_path: Path):
    """A broken data_dir must not raise — audit must never break the call it records."""
    # Point at a path under a non-existent parent. Open will fail, helper logs and returns.
    bad_dir = tmp_path / "does" / "not" / "exist"
    audit_log_event(bad_dir, tool="t", phase="intent")  # must not raise
