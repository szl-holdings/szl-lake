#!/usr/bin/env python3
"""test_lake_ledger.py — tests for the Unified Receipt Ledger.

Covers the keystone receipt-ingest + query path end to end:

  * ingest idempotency (dedupe on receipt id/hash)
  * Khipu hash-chain linkage (prev_hash -> chain_hash, monotonic chain_index)
  * NDJSON / JSON-array batch ingest
  * query filters (organ, since, limit) and newest-first ordering
  * chain-head correctness + cross-component verifiability
  * honest energy labels (UNAVAILABLE — joules never fabricated)
  * durability across a simulated process restart (state rebuilt from disk)
  * the fire-and-forget client never raises

Run with the repo test runner:
    pytest scripts/test_lake_ledger.py -q
or directly:
    python3 scripts/test_lake_ledger.py

stdlib + pytest + FastAPI TestClient.
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from szl_lake_store import (  # noqa: E402
    ReceiptLedger,
    canonical_hash,
)
import szl_lake_server  # noqa: E402
import szl_lake_client  # noqa: E402


def make_receipt(rid: str, organ: str = "a11oy", ts: str = "2026-06-28T10:00:00Z",
                 energy=None, decision: str = "ADMIT") -> dict:
    """A receipt in the live DSSE shape a11oy emits."""
    rec = {
        "id": rid,
        "ts": ts,
        "organ": organ,
        "decision": decision,
        "governance": {"lambda": 0.5, "gates": ["spectral", "persistence"]},
        "dsse": {
            "payloadType": "application/vnd.szl.receipt+json",
            "payload": "eyJ4IjoxfQ==",
            "signatures": [{"sig": "MEUCIQ...", "keyid": "a11oy-p256"}],
        },
    }
    if energy is not None:
        rec["energy"] = energy
    return rec


@pytest.fixture()
def ledger(tmp_path):
    return ReceiptLedger(root=str(tmp_path / "khipu"))


@pytest.fixture()
def client(tmp_path):
    led = ReceiptLedger(root=str(tmp_path / "khipu_srv"))
    szl_lake_server.app.state.ledger = led
    c = TestClient(szl_lake_server.app)
    yield c, led
    szl_lake_server.app.state.ledger = None


# ---------------------------------------------------------------- store ----

def test_append_accepts_and_returns_chain_head(ledger):
    r = ledger.append(make_receipt("r1"))
    assert r["accepted"] is True
    assert r["duplicate"] is False
    assert r["receipt_id"] == "r1"
    assert r["chain_index"] == 1
    assert r["chain_head"] and len(r["chain_head"]) == 64  # sha3-256 hex
    assert r["ledger_offset"] == 1


def test_idempotent_on_duplicate_id(ledger):
    first = ledger.append(make_receipt("dup"))
    second = ledger.append(make_receipt("dup", decision="REJECT"))
    assert first["accepted"] is True
    assert second["accepted"] is False
    assert second["duplicate"] is True
    # head must not advance on a duplicate
    assert second["chain_head"] == first["chain_head"]
    assert second["chain_index"] == first["chain_index"]
    head = ledger.chain_head("a11oy")
    assert head["count"] == 1


def test_chain_linkage_and_recompute(ledger):
    ledger.append(make_receipt("c1"))
    ledger.append(make_receipt("c2"))
    ledger.append(make_receipt("c3"))
    envs = sorted(ledger.query(organ="a11oy", limit=100),
                  key=lambda e: e["chain_index"])
    assert [e["chain_index"] for e in envs] == [1, 2, 3]
    # genesis prev_hash is null
    assert envs[0]["prev_hash"] is None
    # each prev_hash links to the prior chain_hash
    assert envs[1]["prev_hash"] == envs[0]["chain_hash"]
    assert envs[2]["prev_hash"] == envs[1]["chain_hash"]
    # chain_hash is the documented SHA3-256 over the canonical link object
    for e in envs:
        recomputed = canonical_hash({
            "prev_hash": e["prev_hash"],
            "receipt_id": e["receipt_id"],
            "organ": e["organ"],
            "ts": e["ts"],
            "chain_index": e["chain_index"],
        })
        assert recomputed == e["chain_hash"]


def test_chain_head_matches_last_link(ledger):
    ledger.append(make_receipt("h1"))
    last = ledger.append(make_receipt("h2"))
    head = ledger.chain_head("a11oy")
    assert head["chain_head"] == last["chain_head"]
    assert head["chain_index"] == 2
    assert head["count"] == 2
    assert head["chain_alg"] == "sha3_256"


def test_per_organ_chains_are_independent(ledger):
    ledger.append(make_receipt("a", organ="ouroboros"))
    ledger.append(make_receipt("b", organ="hatun-mcp"))
    ledger.append(make_receipt("c", organ="ouroboros"))
    assert ledger.chain_head("ouroboros")["count"] == 2
    assert ledger.chain_head("hatun-mcp")["count"] == 1


def test_batch_ndjson_ingest_with_dedupe(ledger):
    batch = [make_receipt(f"b{i}") for i in range(5)] + [make_receipt("b0")]
    res = ledger.append_batch(batch)
    assert res["total"] == 6
    assert res["accepted"] == 5
    assert res["duplicates"] == 1
    assert ledger.chain_head("a11oy")["count"] == 5


def test_query_filters(ledger):
    ledger.append(make_receipt("old", ts="2026-06-01T00:00:00Z"))
    ledger.append(make_receipt("new", ts="2026-06-28T00:00:00Z"))
    ledger.append(make_receipt("other", organ="szl-router",
                               ts="2026-06-28T00:00:00Z"))
    # organ filter
    a11 = ledger.query(organ="a11oy", limit=100)
    assert {e["receipt_id"] for e in a11} == {"old", "new"}
    # since filter
    recent = ledger.query(organ="a11oy", since="2026-06-15T00:00:00Z")
    assert {e["receipt_id"] for e in recent} == {"new"}
    # limit
    assert len(ledger.query(limit=1)) == 1


def test_query_newest_first(ledger):
    ledger.append(make_receipt("first", ts="2026-06-01T00:00:00Z"))
    ledger.append(make_receipt("second", ts="2026-06-20T00:00:00Z"))
    out = ledger.query(organ="a11oy", limit=10)
    assert out[0]["receipt_id"] == "second"


def test_energy_unavailable_when_absent(ledger):
    ledger.append(make_receipt("no-energy"))
    env = ledger.query(organ="a11oy")[0]
    assert env["energy"] == {"label": "UNAVAILABLE"}


def test_energy_measured_when_numeric(ledger):
    ledger.append(make_receipt("with-energy", energy=12.5))
    env = ledger.query(organ="a11oy")[0]
    assert env["energy"]["label"] == "MEASURED"
    assert env["energy"]["joules"] == 12.5


def test_content_dedupe_when_no_id(ledger):
    rec = {"organ": "vsp-otel", "ts": "2026-06-28T00:00:00Z", "decision": "X"}
    a = ledger.append(dict(rec))
    b = ledger.append(dict(rec))
    assert a["accepted"] is True
    assert b["duplicate"] is True


def test_invalid_organ_rejected(ledger):
    with pytest.raises(ValueError):
        ledger.append(make_receipt("bad", organ="../escape"))


def test_durability_across_restart(tmp_path):
    root = str(tmp_path / "persist")
    led1 = ReceiptLedger(root=root)
    led1.append(make_receipt("p1"))
    led1.append(make_receipt("p2"))
    head1 = led1.chain_head("a11oy")
    # simulate a fresh process: brand-new ledger object over the same dir
    led2 = ReceiptLedger(root=root)
    head2 = led2.chain_head("a11oy")
    assert head2 == head1
    # dedupe survives restart
    assert led2.append(make_receipt("p1"))["duplicate"] is True
    # and the chain continues correctly from the rebuilt head
    cont = led2.append(make_receipt("p3"))
    assert cont["chain_index"] == 3
    assert cont["accepted"] is True


# --------------------------------------------------------------- server ----

def test_post_single_receipt(client):
    c, _ = client
    resp = c.post("/api/lake/v1/receipts", json=make_receipt("s1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is True
    assert body["chain_index"] == 1


def test_post_duplicate_returns_duplicate(client):
    c, _ = client
    c.post("/api/lake/v1/receipts", json=make_receipt("sdup"))
    resp = c.post("/api/lake/v1/receipts", json=make_receipt("sdup"))
    assert resp.status_code == 200
    assert resp.json()["duplicate"] is True


def test_post_ndjson_batch(client):
    c, _ = client
    import json as _json
    lines = "\n".join(_json.dumps(make_receipt(f"n{i}")) for i in range(3))
    resp = c.post("/api/lake/v1/receipts", content=lines,
                  headers={"Content-Type": "application/x-ndjson"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 3
    assert body["total"] == 3


def test_post_json_array_batch(client):
    c, _ = client
    resp = c.post("/api/lake/v1/receipts",
                  json=[make_receipt("arr1"), make_receipt("arr2")])
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 2


def test_post_empty_body_is_400(client):
    c, _ = client
    resp = c.post("/api/lake/v1/receipts", content="",
                  headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


def test_get_receipts_query(client):
    c, _ = client
    c.post("/api/lake/v1/receipts", json=make_receipt("q1", organ="szl-mesh"))
    resp = c.get("/api/lake/v1/receipts", params={"organ": "szl-mesh"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["results"][0]["receipt_id"] == "q1"


def test_get_chain_head(client):
    c, _ = client
    c.post("/api/lake/v1/receipts", json=make_receipt("ch1", organ="szl-trust"))
    resp = c.get("/api/lake/v1/chain/head", params={"organ": "szl-trust"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["chain_index"] == 1
    assert body["chain_head"]


def test_get_chain_head_invalid_organ_400(client):
    c, _ = client
    resp = c.get("/api/lake/v1/chain/head", params={"organ": "../x"})
    assert resp.status_code == 400


def test_health(client):
    c, _ = client
    c.post("/api/lake/v1/receipts", json=make_receipt("hh1", organ="ouroboros"))
    c.post("/api/lake/v1/receipts", json=make_receipt("hh2", organ="hatun-mcp"))
    resp = c.get("/api/lake/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_receipts"] == 2
    assert body["chain_alg"] == "sha3_256"
    assert set(body["organs"]) == {"ouroboros", "hatun-mcp"}


# --------------------------------------------------------------- client ----

def test_client_noop_without_sink(monkeypatch):
    monkeypatch.delenv("SZL_RECEIPT_SINK", raising=False)
    out = szl_lake_client.emit_receipt(make_receipt("x"))
    assert out["ok"] is False
    assert "unset" in out["reason"]


def test_client_never_raises_on_bad_target(monkeypatch):
    # Unreachable host must be swallowed, not raised.
    out = szl_lake_client.emit_receipt(
        make_receipt("x"), base_url="http://127.0.0.1:1", timeout=0.2)
    assert out["ok"] is False


def test_client_handles_unserializable(monkeypatch):
    monkeypatch.setenv("SZL_RECEIPT_SINK", "http://127.0.0.1:1")
    out = szl_lake_client.emit_receipt({"bad": {1, 2, 3}})  # set not JSON
    assert out["ok"] is False
    assert "serializable" in out["reason"]


def test_client_round_trip_against_testclient(client, monkeypatch):
    # Drive the real client POST path through an in-process WSGI/ASGI client by
    # pointing urllib at a live uvicorn would be heavy; instead assert the
    # server accepts what the client serializes (shape parity).
    c, led = client
    body = szl_lake_client.json.dumps(make_receipt("rt1")).encode()
    resp = c.post("/api/lake/v1/receipts", content=body,
                  headers={"Content-Type": "application/json"})
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([os.path.abspath(__file__), "-q"]))
