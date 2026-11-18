# SPDX-License-Identifier: Apache-2.0
"""Graph projection is bounded, read-only, and explicitly not attestation.

All receipts below are synthetic; neither cloud storage nor provider secrets
are consulted. The SQL source owns validated ledger head/count readbacks.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

pytest.importorskip("fastapi.testclient")
from fastapi.testclient import TestClient

import szl_lake_graph as graph
import szl_lake_server as server
import szl_lake_sql as sql
from szl_lake_store import ReceiptLedger


@pytest.fixture
def ledger(tmp_path):
    return ReceiptLedger(str(tmp_path / "synthetic-graph-ledger"))


@pytest.fixture
def client(ledger):
    old = getattr(server.app.state, "ledger", None)
    server.app.state.ledger = ledger
    try:
        yield TestClient(server.app)
    finally:
        server.app.state.ledger = old


def receipt(n, organ="audit", decision="ALLOW", **other):
    return {
        "id": f"{organ}-{n}", "organ": organ,
        "ts": f"2026-11-17T15:00:{n % 60:02d}Z",
        "decision": decision, **other,
    }


def add_three(ledger):
    ledger.append(receipt(1, decision="ALLOW", token="RAW_NEVER_IN_GRAPH"))
    ledger.append(receipt(2, decision="DENY", secret="SECRETS_NEVER_IN_GRAPH"))
    ledger.append(receipt(3, organ="model", decision="UNKNOWN"))


def test_feature_disabled_without_ledger_read(client, monkeypatch):
    monkeypatch.delenv("SZL_LAKE_GRAPH_READONLY", raising=False)
    def would_read(_request):
        raise AssertionError("disabled graph endpoint must not read the ledger")
    monkeypatch.setattr(server, "_ledger", would_read)
    for endpoint in ("/api/lake/v1/graph/summary", "/graph"):
        response = client.get(endpoint)
        assert response.status_code == 503
        assert response.json()["code"] == "FEATURE_NOT_ADMITTED"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-robots-tag"] == "noindex"


@pytest.mark.parametrize("enabled", ["true", "yes", "0", "TRUE", "01"])
def test_only_exact_opt_in_is_admitted(client, monkeypatch, enabled):
    monkeypatch.setenv("SZL_LAKE_GRAPH_READONLY", enabled)
    response = client.get("/api/lake/v1/graph/summary")
    assert response.status_code == 503
    assert response.json()["code"] == "FEATURE_NOT_ADMITTED"


def test_enabled_graph_source_head_and_decision_denominators(ledger, client, monkeypatch):
    monkeypatch.setenv("SZL_LAKE_GRAPH_READONLY", "1")
    add_three(ledger)
    before = deepcopy(ledger.health())
    r = client.get("/api/lake/v1/graph/summary")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-robots-tag"] == "noindex"
    doc = r.json()
    assert doc["schema"] == graph.SCHEMA
    assert doc["state"] == "LOCAL_CHAIN_VALIDATED"
    assert doc["projection"] == "READ_ONLY_EPHEMERAL"
    assert doc["claim_scope"] == "LOCAL_CHAIN_INTEGRITY_ONLY"
    assert doc["source"]["total_receipts"] == 3
    assert doc["source"]["selected_receipts"] == 3
    assert [(x["organ"], x["count"]) for x in doc["source"]["source_heads"]] == [
        ("audit", 2), ("model", 1),
    ]
    assert all(x["chain_head"] == before["organs"][x["organ"]]["chain_head"]
               for x in doc["source"]["source_heads"])
    assert len(doc["graph"]["nodes"]) == 1 + 2 + 3
    assert len(doc["graph"]["edges"]) == 2 + 3
    assert sum(x["count"] for x in doc["distributions"]) == 3
    counts = {x["organ"]: x for x in doc["distributions"]}
    assert counts["audit"]["decision_entropy_bits"] == 1.0
    assert counts["model"]["decision_entropy_bits"] == 0.0
    assert counts["audit"]["decision_frequencies"] == [
        {"decision": "ALLOW", "count": 1, "fraction_of_organ": 0.5},
        {"decision": "DENY", "count": 1, "fraction_of_organ": 0.5},
    ]
    assert doc["graph_checksum_signed"] is False
    assert doc["issuer_signatures_verified"] is False
    assert doc["origin_authentication_verified"] is False
    assert doc["tenant_authorization_verified"] is False
    assert doc["causal_inference_performed"] is False
    assert doc["model_training_authorized"] is False
    assert doc["backend_writes"] == doc["external_effects"] == 0
    assert len(doc["graph_sha3_256"]) == 64
    assert "RAW_NEVER_IN_GRAPH" not in r.text
    assert "SECRETS_NEVER_IN_GRAPH" not in r.text
    assert ledger.health()["organs"] == before["organs"]


def test_organ_filter_preserves_total_source_denominator_and_head(ledger, client, monkeypatch):
    monkeypatch.setenv("SZL_LAKE_GRAPH_READONLY", "1")
    add_three(ledger)
    r = client.get("/api/lake/v1/graph/summary", params={"organ": "audit"})
    assert r.status_code == 200
    doc = r.json()
    assert doc["source"]["total_receipts"] == 3
    assert doc["source"]["selected_receipts"] == 2
    assert doc["source"]["organ_filter"] == "audit"
    assert [d["organ"] for d in doc["distributions"]] == ["audit"]
    assert len(doc["graph"]["nodes"]) == 1 + 1 + 2
    assert [h["organ"] for h in doc["source"]["source_heads"]] == ["audit"]


def test_filtered_unknown_organ_is_empty_scope_not_empty_ledger(ledger, client, monkeypatch):
    monkeypatch.setenv("SZL_LAKE_GRAPH_READONLY", "1")
    ledger.append(receipt(1))
    r = client.get("/api/lake/v1/graph/summary", params={"organ": "model"})
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "LOCAL_CHAIN_VALIDATED"
    assert data["source"]["total_receipts"] == 1
    assert data["source"]["selected_receipts"] == 0
    assert data["graph"]["nodes"] == [{
        "id": "lake:validated-snapshot", "kind": "SOURCE_SNAPSHOT",
        "count": 1, "label": "Validated local ledger projection",
    }]
    assert data["graph"]["edges"] == []
    assert data["source"]["source_heads"] == []


def test_empty_graph_is_explicitly_empty_and_not_production_ready(client, monkeypatch):
    monkeypatch.setenv("SZL_LAKE_GRAPH_READONLY", "1")
    data = client.get("/api/lake/v1/graph/summary").json()
    assert data["state"] == "EMPTY"
    assert data["source"]["total_receipts"] == 0
    assert data["graph"]["edges"] == []
    assert data["model_training_authorized"] is False
    assert data["issuer_signatures_verified"] is False


@pytest.mark.parametrize("q", [
    {"sql": "DROP TABLE snapshot"}, {"path": "../"}, {"root": "C:\\secret"},
    {"token": "password"}, {"query": "SELECT * FROM snapshot"},
    {"organ": "../audit"}, {"organ": "audit' OR 1=1--"},
])
def test_disallow_arbitrary_sql_paths_and_unsafe_filters(client, monkeypatch, q):
    monkeypatch.setenv("SZL_LAKE_GRAPH_READONLY", "1")
    for endpoint in ("/api/lake/v1/graph/summary", "/graph"):
        r = client.get(endpoint, params=q)
        assert r.status_code == 400
        assert r.headers["content-type"].startswith("application/json")
        assert r.json()["state"] == "INVALID_REQUEST"


def test_repeated_organ_query_is_rejected(client, monkeypatch):
    monkeypatch.setenv("SZL_LAKE_GRAPH_READONLY", "1")
    r = client.get("/api/lake/v1/graph/summary",
                   params=[("organ", "audit"), ("organ", "model")])
    assert r.status_code == 400
    assert r.json()["code"] == "UNSUPPORTED_QUERY_PARAMETER"


@pytest.mark.parametrize("mutation", [
    lambda doc: doc.update({"signatures_verified": True}),
    lambda doc: doc.update({"coverage_complete": False}),
    lambda doc: doc.update({"schema": "unexpected"}),
    lambda doc: doc.update({"rows_matching_filter": 0}),
    lambda doc: doc["source_heads"].clear(),
    lambda doc: doc["groups"][0]["decision_counts"].update({"FORGED": 1}),
    lambda doc: doc["groups"][0]["decision_counts"].update({"ALLOW": 999}),
    lambda doc: doc["source_heads"][0].update({"chain_head": "0" * 63}),
    lambda doc: doc["source_heads"][0].update({"count": True}),
])
def test_invalid_snapshot_cannot_be_promoted_to_graph(ledger, mutation):
    add_three(ledger)
    summary = sql.summarize(ledger)
    mutation(summary)
    with pytest.raises(graph.GraphProjectionUnavailable):
        graph.project(summary)


def test_graph_checksum_stable_without_generated_at_but_binds_heads_and_counts(ledger):
    add_three(ledger)
    original = sql.summarize(ledger)
    baseline = graph.project(original)
    updated_clock = deepcopy(original)
    updated_clock["generated_at"] = "2099-01-01T00:00:00Z"
    assert graph.project(updated_clock)["graph_sha3_256"] == baseline["graph_sha3_256"]
    changed = deepcopy(original)
    changed["source_heads"][0]["chain_head"] = "0" * 64
    assert graph.project(changed)["graph_sha3_256"] != baseline["graph_sha3_256"]
    # A checksum can be recomputed by a caller: it is NOT issuer authentication.


def test_corrupted_source_is_503_without_altering_disk(ledger, client, monkeypatch):
    monkeypatch.setenv("SZL_LAKE_GRAPH_READONLY", "1")
    ledger.append(receipt(1))
    path = Path(ledger.root) / "audit" / "2026-11-17.ndjson"
    damaged = path.read_bytes() + b'{"unclosed":'
    path.write_bytes(damaged)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    r = client.get("/api/lake/v1/graph/summary")
    assert r.status_code == 503
    assert r.json()["state"] == "UNAVAILABLE"
    assert "traceback" not in r.text.lower()
    assert r.headers["x-robots-tag"] == "noindex"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_torn_snapshot_during_query_is_unavailable_not_soft_green(
    ledger, client, monkeypatch
):
    monkeypatch.setenv("SZL_LAKE_GRAPH_READONLY", "1")
    ledger.append(receipt(1))
    original = ledger.query
    invoked = []

    def racing_query(*args, **kwargs):
        result = original(*args, **kwargs)
        if not invoked:
            invoked.append(True)
            ledger.append(receipt(2))
        return result

    monkeypatch.setattr(ledger, "query", racing_query)
    result = client.get("/api/lake/v1/graph/summary")
    assert result.status_code == 503
    assert result.json()["code"] == "LEDGER_CHANGED_DURING_READ"


def test_graph_html_is_accessible_script_free_and_escapes_untrusted_labels(
    ledger, client, monkeypatch
):
    monkeypatch.setenv("SZL_LAKE_GRAPH_READONLY", "1")
    ledger.append(receipt(1))
    page = client.get("/graph")
    assert page.status_code == 200
    assert "script-src 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-robots-tag"] == "noindex"
    assert "Source-bound evidence graph" in page.text
    assert "<table>" in page.text
    assert "<script" not in page.text
    assert "/api/lake/v1/graph/summary" in page.text
    filtered = client.get("/graph", params={"organ": "audit"})
    assert filtered.status_code == 200
    assert '/api/lake/v1/graph/summary?organ=audit' in filtered.text
    doc = graph.project(sql.summarize(ledger))
    doc["distributions"][0]["organ"] = "<img src=x onerror=alert(1)>"
    escaped = graph.render_html(doc).decode()
    assert "&lt;img src=x onerror=alert(1)&gt;" in escaped
    assert "<img src=" not in escaped


def test_read_only_concurrent_gets_same_source_heads_no_state_changes(
    ledger, client, monkeypatch
):
    monkeypatch.setenv("SZL_LAKE_GRAPH_READONLY", "1")
    add_three(ledger)
    heads = ledger.health()["organs"]

    def request(_):
        r = client.get("/api/lake/v1/graph/summary")
        return r.status_code, r.json().get("graph_sha3_256")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(request, range(16)))
    assert all(status == 200 and digest == results[0][1]
               for status, digest in results)
    assert ledger.health()["organs"] == heads


def test_graph_never_mints_record_or_allows_post(client, monkeypatch):
    monkeypatch.setenv("SZL_LAKE_GRAPH_READONLY", "1")
    for endpoint in ("/graph", "/api/lake/v1/graph/summary"):
        assert client.post(endpoint, json={"effect": "do_it"}).status_code == 405
