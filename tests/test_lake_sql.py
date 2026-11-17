"""Read-only SQLite projection of validated synthetic ledger receipts.

These tests never use remote databases, production keys or real receipt data.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

import pytest

# The core package is stdlib-only. Installed-server CI supplies the optional
# [serve] dependency; the generic Python base gate does not.
pytest.importorskip("fastapi.testclient")
from fastapi.testclient import TestClient

import szl_lake_sql as sql
import szl_lake_server as server
from szl_lake_store import ReceiptLedger


@pytest.fixture
def ledger(tmp_path):
    root = tmp_path / "lake"
    return ReceiptLedger(str(root))


def receipt(num, organ="audit", day="2026-11-17", decision="ALLOW", **other):
    return {
        "id": f"{organ}-{num}",
        "organ": organ,
        "ts": f"{day}T10:00:{num % 60:02d}Z",
        "decision": decision,
        **other,
    }


@pytest.fixture
def client(ledger):
    before = getattr(server.app.state, "ledger", None)
    server.app.state.ledger = ledger
    try:
        yield TestClient(server.app)
    finally:
        server.app.state.ledger = before


def test_source_summary_runs_real_parameterized_sql_without_ledger_writes(ledger, client):
    ledger.append(receipt(1, decision="ALLOW", confidential="NEVER_PUBLISH_RAW_PAYLOAD"))
    ledger.append(receipt(2, decision="DENY"))
    ledger.append(receipt(3, organ="model", decision="ABSTAIN"))
    first = ledger.health()
    value = client.get("/api/lake/v1/sql/summary")
    assert value.status_code == 200
    assert value.headers["cache-control"] == "no-store"
    result = value.json()
    assert result["schema"] == sql.SCHEMA
    assert result["state"] == "LOCAL_CHAIN_VALIDATED"
    assert result["rows_in_source"] == result["rows_matching_filter"] == 3
    assert result["coverage_complete"] is True
    assert result["analytics_engine"] == "sqlite3:memory:"
    assert result["authoritative_store"] == "szl_lake_store NDJSON"
    assert result["ledger_writes"] == result["receipts_returned"] == 0
    assert result["signatures_verified"] is False
    assert result["production_authorized"] is False
    groups = {row["organ"]: row for row in result["groups"]}
    assert groups["audit"]["count"] == 2
    assert groups["audit"]["decision_counts"] == {"ALLOW": 1, "DENY": 1}
    assert groups["model"]["decision_counts"] == {"ABSTAIN": 1}
    assert "NEVER_PUBLISH_RAW_PAYLOAD" not in value.text
    assert ledger.health()["organs"] == first["organs"]


def test_query_filter_is_bounded_and_parameterized(ledger, client):
    for n in range(1, 4):
        ledger.append(receipt(n, "audit" if n < 3 else "model"))
    result = client.get("/api/lake/v1/sql/summary", params={"organ": "audit"})
    assert result.status_code == 200
    assert result.json()["rows_matching_filter"] == 2
    assert [row["organ"] for row in result.json()["groups"]] == ["audit"]
    assert result.json()["rows_in_source"] == 3
    for text in ("audit' OR 1=1 --", "../audit", "model; DROP TABLE snapshot"):
        assert client.get("/api/lake/v1/sql/summary", params={"organ": text}).status_code == 400
    # A caller may not send raw SQL, a target database, or a signer to the GET.
    for key in ("query", "sql", "database", "sign", "url", "root"):
        response = client.get("/api/lake/v1/sql/summary", params={key: "SELECT 1"})
        assert response.status_code == 400
        assert response.json()["code"] == "UNSUPPORTED_QUERY_PARAMETER"


def test_empty_ledger_does_not_masquerade_as_ready(client):
    response = client.get("/api/lake/v1/sql/summary")
    assert response.status_code == 200
    assert response.json()["state"] == "EMPTY"
    assert response.json()["rows_matching_filter"] == 0
    assert response.json()["groups"] == []


def test_unknown_decision_is_sanitized_not_displayed(ledger, client):
    ledger.append(receipt(1, decision="<script>alert(1)</script>", token="SECRET"))
    data = client.get("/api/lake/v1/sql/summary").json()
    assert data["groups"][0]["decision_counts"] == {"OTHER": 1}
    assert "SECRET" not in json.dumps(data)
    assert "<script>" not in json.dumps(data)


def test_oversized_source_refuses_instead_of_silent_truncation(ledger, client, monkeypatch):
    ledger.append(receipt(1))
    ledger.append(receipt(2))
    monkeypatch.setattr(sql, "MAX_ROWS", 1)
    response = client.get("/api/lake/v1/sql/summary")
    assert response.status_code == 503
    assert response.json() == {"state": "UNAVAILABLE", "code": "LEDGER_ROW_LIMIT"}
    assert ledger.health()["total_receipts"] == 2


@pytest.mark.parametrize("damage", ["garbage", "torn", "mutate"])
def test_corrupted_source_fails_closed_and_never_rewrites_bytes(ledger, client, damage):
    ledger.append(receipt(1))
    ledger.append(receipt(2))
    path = Path(ledger.root) / "audit" / "2026-11-17.ndjson"
    data = path.read_bytes()
    if damage == "garbage":
        damaged = b"bad-json\n" + data
    elif damage == "torn":
        damaged = data + b'{"half":'
    else:
        damaged = data.replace(b'"receipt_id":"audit-1"', b'"receipt_id":"audit-X"', 1)
    path.write_bytes(damaged)
    saved = hashlib.sha256(damaged).hexdigest()
    response = client.get("/api/lake/v1/sql/summary")
    assert response.status_code == 503
    assert response.json()["state"] == "UNAVAILABLE"
    assert "confidential" not in response.text
    assert hashlib.sha256(path.read_bytes()).hexdigest() == saved


def test_changed_source_during_query_does_not_paint_success(ledger, client, monkeypatch):
    ledger.append(receipt(1))
    original = ledger.query
    invoked = []

    def read_then_append(*args, **kwargs):
        rows = original(*args, **kwargs)
        if not invoked:
            ledger.append(receipt(2))
            invoked.append(True)
        return rows

    monkeypatch.setattr(ledger, "query", read_then_append)
    response = client.get("/api/lake/v1/sql/summary")
    assert response.status_code == 503
    assert response.json()["code"] == "LEDGER_CHANGED_DURING_READ"


def test_restarted_process_uses_original_persisted_lake_not_in_memory_cache(ledger):
    ledger.append(receipt(1, decision="REVIEW"))
    first = sql.summarize(ledger)
    reopened = ReceiptLedger(ledger.root)
    second = sql.summarize(reopened)
    assert first["groups"] == second["groups"]
    assert first["source_heads"] == second["source_heads"]
    assert second["rows_matching_filter"] == 1


def test_python_rendered_frontend_escapes_text_and_has_no_client_script(ledger, client):
    ledger.append(receipt(1))
    response = client.get("/sql")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "script-src 'none'" in response.headers["content-security-policy"]
    assert "<script" not in response.text
    assert "<table>" in response.text
    assert "SQL evidence summary" in response.text
    assert "/api/lake/v1/sql/summary" in response.text
    assert "LOCAL_CHAIN_VALIDATED" in response.text
    synthetic = sql.summarize(ledger)
    synthetic["groups"][0]["organ"] = "<img src=x onerror=alert(1)>"
    escaped = sql.render_html(synthetic).decode()
    assert "&lt;img src=x onerror=alert(1)&gt;" in escaped
    assert "<img " not in escaped


def test_frontend_errors_are_honest_json_not_html_soft_200(client):
    for path in ("/sql", "/api/lake/v1/sql/summary"):
        r = client.get(path, params={"sql": "DROP TABLE snapshot"})
        assert r.status_code == 400
        assert r.headers["content-type"].startswith("application/json")
        assert r.json()["state"] == "INVALID_REQUEST"


def test_threaded_read_concurrency_is_independent_and_leaves_no_state(ledger, client):
    for n in range(1, 8):
        ledger.append(receipt(n, organ="audit" if n < 4 else "model"))
    before = ledger.health()["organs"]

    def one(_):
        r = client.get("/api/lake/v1/sql/summary")
        return r.status_code, r.json().get("rows_matching_filter")

    with ThreadPoolExecutor(max_workers=5) as pool:
        output = list(pool.map(one, range(40)))
    assert all(row == (200, 7) for row in output)
    assert ledger.health()["organs"] == before
