"""Ambiguous JSON must not be admitted to the durable receipt ledger."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from szl_lake_store import LedgerCorruptionError, ReceiptLedger, strict_json_loads  # noqa: E402


@pytest.mark.parametrize("raw", [
    '{"organ":"other","organ":"audit"}',
    '{"metadata":{"decision":"DENY","decision":"ADMIT"}}',
    '{"metadata":[{"value":1,"value":2}]}',
    '{"metadata":{"value":1,"va\\u006cue":2}}',
    '{"metadata":{"value":1,"value":1}}',
])
def test_duplicate_keys_are_rejected_at_every_depth(raw):
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        strict_json_loads(raw)


def test_repeated_names_in_distinct_objects_remain_valid():
    raw = '{"rows":[{"value":1},{"value":2}],"metadata":{"value":3}}'
    assert strict_json_loads(raw) == json.loads(raw)


@pytest.mark.parametrize("explicit_id", [False, True])
@pytest.mark.parametrize("level", ["envelope", "receipt"])
def test_ambiguous_persisted_records_fail_closed_without_rewriting(tmp_path, explicit_id, level):
    root = tmp_path / "ledger"
    ledger = ReceiptLedger(str(root))
    receipt = {"organ": "audit", "metadata": {"value": 1}}
    if explicit_id:
        receipt["id"] = "supplied-id"
    ledger.append(receipt)
    path = next(root.rglob("*.ndjson"))
    original = path.read_text()
    if level == "envelope":
        ambiguous = original.replace('"organ":"audit"', '"organ":"other","organ":"audit"', 1)
    else:
        ambiguous = original.replace('"value":1', '"value":2,"value":1', 1)
    assert ambiguous != original
    path.write_text(ambiguous)
    restarted = ReceiptLedger(str(root))
    assert restarted.health()["ok"] is False
    with pytest.raises(LedgerCorruptionError, match="unparseable_line"):
        restarted.query(organ="audit")
    with pytest.raises(LedgerCorruptionError):
        restarted.append({"organ": "audit", "id": "later"})
    assert restarted.verify_chain("audit")["ok"] is False
    assert path.read_text() == ambiguous


@pytest.mark.parametrize("body,content_type", [
    ('{"organ":"other","organ":"audit"}', 'application/json'),
    ('[{"organ":"audit"},{"organ":"audit","metadata":{"value":1,"value":2}}]', 'application/json'),
    ('{"organ":"audit"}\n{"organ":"other","organ":"audit"}\n', 'application/x-ndjson'),
])
def test_http_rejects_entire_ambiguous_batch_before_any_append(tmp_path, body, content_type):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from szl_lake_server import app

    ledger = ReceiptLedger(str(tmp_path / "ledger"))
    previous = getattr(app.state, "ledger", None)
    app.state.ledger = ledger
    try:
        with TestClient(app) as client:
            response = client.post('/api/lake/v1/receipts', content=body,
                                   headers={"Content-Type": content_type})
        assert response.status_code == 400
        assert response.json() == {"error": "duplicate JSON object key"}
        assert ledger.query() == []
        assert list((tmp_path / "ledger").rglob("*.ndjson")) == []
    finally:
        if previous is None:
            del app.state.ledger
        else:
            app.state.ledger = previous


@pytest.mark.parametrize("body,content_type,count", [
    ('{"organ":"audit","id":"one"}', 'application/json', 1),
    ('[{"organ":"audit","id":"one"},{"organ":"audit","id":"two"}]', 'application/json', 2),
    ('{"organ":"audit","id":"one"}\n{"organ":"audit","id":"two"}\n', 'application/x-ndjson', 2),
])
def test_valid_http_formats_keep_repeated_keys_in_separate_objects(tmp_path, body, content_type, count):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from szl_lake_server import app

    ledger = ReceiptLedger(str(tmp_path / "ledger"))
    previous = getattr(app.state, "ledger", None)
    app.state.ledger = ledger
    try:
        with TestClient(app) as client:
            response = client.post('/api/lake/v1/receipts', content=body,
                                   headers={"Content-Type": content_type})
        assert response.status_code == 200
        assert len(ledger.query()) == count
        assert ledger.verify_chain("audit")["ok"] is True
    finally:
        if previous is None:
            del app.state.ledger
        else:
            app.state.ledger = previous
