"""Non-finite values must not poison durable receipt JSON or read responses."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from szl_lake_store import LedgerCorruptionError, ReceiptLedger  # noqa: E402


TOKENS = ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("explicit_id", [False, True])
def test_store_rejects_nonfinite_nested_values_without_writing(tmp_path, value, explicit_id):
    ledger = ReceiptLedger(str(tmp_path / "ledger"))
    receipt = {"organ": "audit", "metadata": {"values": [value]}}
    if explicit_id:
        receipt["id"] = "supplied-id"
    with pytest.raises(ValueError):
        ledger.append(receipt)
    assert ledger.query(organ="audit") == []
    assert list((tmp_path / "ledger").rglob("*.ndjson")) == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_duplicate_identity_cannot_acknowledge_malformed_payload(tmp_path, value):
    ledger = ReceiptLedger(str(tmp_path / "ledger"))
    receipt = {"organ": "audit", "id": "existing", "value": 1}
    ledger.append(receipt)
    original_head = ledger.chain_head("audit")
    with pytest.raises(ValueError):
        ledger.append({**receipt, "value": value})
    assert ledger.chain_head("audit") == original_head
    assert ledger.query(organ="audit")[0]["receipt"] == receipt


@pytest.mark.parametrize("token", TOKENS)
@pytest.mark.parametrize("explicit_id", [False, True])
def test_persisted_nonfinite_values_fail_recovery_and_verification(tmp_path, token, explicit_id):
    root = tmp_path / "ledger"
    ledger = ReceiptLedger(str(root))
    receipt = {"organ": "audit", "metadata": {"value": "POISON"}}
    if explicit_id:
        receipt["id"] = "supplied-id"
    ledger.append(receipt)
    path = next(root.rglob("*.ndjson"))
    poisoned = path.read_text().replace('"POISON"', token)
    path.write_text(poisoned)
    restarted = ReceiptLedger(str(root))
    assert restarted.health()["ok"] is False
    with pytest.raises(LedgerCorruptionError, match="unparseable_line"):
        restarted.query(organ="audit")
    with pytest.raises(LedgerCorruptionError):
        restarted.append({"organ": "audit", "id": "later"})
    verification = restarted.verify_chain("audit")
    assert verification["ok"] is False
    assert [entry["kind"] for entry in verification["broken"]] == ["unparseable_line"]
    assert path.read_text() == poisoned
