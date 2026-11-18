"""Tests for szl_lake_rag: bounded, whitelist-projected, hierarchy-anchored retrieval."""
import json
import re

import pytest

import szl_lake_hier as hier
import szl_lake_rag as rag
import szl_lake_sql as sql
from szl_lake_store import ReceiptLedger

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def receipt(num, organ="audit", day="2026-11-17", decision="ALLOW", **other):
    return {
        "id": f"{organ}-{num}",
        "organ": organ,
        "ts": f"{day}T10:00:{num % 60:02d}Z",
        "decision": decision,
        **other,
    }


@pytest.fixture
def ledger(tmp_path):
    led = ReceiptLedger(str(tmp_path / "lake"))
    led.append(receipt(1, confidential="NEVER_PUBLISH_RAW_PAYLOAD"))
    led.append(receipt(2, decision="DENY"))
    led.append(receipt(3, organ="model", decision="ABSTAIN"))
    led.append(receipt(4, organ="model", decision="ABSTAIN", confidential="NEVER_PUBLISH_RAW_PAYLOAD"))
    return led


@pytest.fixture
def hierarchy(ledger):
    return hier.project_hierarchy(sql.summarize(ledger))


def test_retrieves_chain_validated_evidence_whitelist_only(ledger, hierarchy):
    resp = rag.ask(ledger, "audit allow decisions", hierarchy=hierarchy)
    assert resp["schema"] == rag.SCHEMA
    assert resp["verdict"] == "EVIDENCE_RETRIEVED"
    assert resp["organs_matched"] == ["audit"]
    assert resp["results"], "expected at least one result"
    for row in resp["results"]:
        assert set(row) == set(rag.RESULT_FIELDS)
        assert row["organ"] == "audit"
        assert isinstance(row["receipt_id"], str)
        assert isinstance(row["chain_index"], int)
        assert HEX64.match(row["chain_hash"])
    decisions = sorted({row["decision"] for row in resp["results"]})
    assert decisions == ["ALLOW", "DENY"]


def test_raw_payloads_are_never_projected(ledger, hierarchy):
    resp = rag.ask(ledger, "model abstain", hierarchy=hierarchy)
    blob = json.dumps(resp)
    assert "NEVER_PUBLISH_RAW_PAYLOAD" not in blob
    assert "confidential" not in blob
    assert '"receipt"' not in blob
    assert "receipt_body_hash" not in blob
    for row in resp["results"]:
        assert row["organ"] == "model"
        assert row["decision"] == "ABSTAIN"


def test_hierarchy_anchor_carries_dag_position(ledger, hierarchy):
    resp = rag.ask(ledger, "audit", hierarchy=hierarchy)
    for row in resp["results"]:
        path = row["hierarchy_path"]
        assert path is not None
        assert path["root_digest"] == hierarchy["root"]["digest"]
        assert HEX64.match(path["organ_digest"])
        assert path["chain_head"] == row["chain_hash"] or isinstance(path["chain_head"], str)
        assert row["hierarchy_stale"] is False
    head = ledger.chain_head("audit")
    assert resp["results"][0]["hierarchy_path"]["chain_head"] == head["chain_head"]


def test_stale_hierarchy_reports_instead_of_guessing(ledger, hierarchy):
    # Advance the ledger past the snapshot: 'router' organ is not in the hierarchy.
    ledger.append(receipt(9, organ="router", decision="BLOCK"))
    resp = rag.ask(ledger, "router block", hierarchy=hierarchy)
    assert resp["verdict"] == "EVIDENCE_RETRIEVED"
    for row in resp["results"]:
        assert row["hierarchy_path"] is None
        assert row["hierarchy_stale"] is True


def test_no_match_fails_closed(ledger, hierarchy):
    resp = rag.ask(ledger, "zzzqqq nothingmatches", hierarchy=hierarchy)
    assert resp["verdict"] == "NO_EVIDENCE"
    assert resp["results"] == []
    assert resp["organs_matched"] == []
    assert "fabricated" in resp["synthesis"]


def test_bounds_fail_closed(ledger):
    with pytest.raises(rag.RagError) as caught:
        rag.ask(ledger, "audit", limit=21)
    assert caught.value.code == "RAG_BAD_LIMIT"
    with pytest.raises(rag.RagError) as caught:
        rag.ask(ledger, "audit", limit=0)
    assert caught.value.code == "RAG_BAD_LIMIT"
    with pytest.raises(rag.RagError) as caught:
        rag.ask(ledger, "")
    assert caught.value.code == "RAG_BAD_QUERY"
    with pytest.raises(rag.RagError) as caught:
        rag.ask(ledger, "x" * 300)
    assert caught.value.code == "RAG_BAD_QUERY"
    with pytest.raises(rag.RagError) as caught:
        rag.ask(object(), "audit")
    assert caught.value.code == "RAG_BAD_LEDGER"


def test_limit_bounds_rows_per_organ(ledger, hierarchy):
    resp = rag.ask(ledger, "model", hierarchy=hierarchy, limit=1)
    assert len(resp["results"]) == 1
    assert resp["counts"]["results_returned"] == 1


def test_synthesis_uses_whitelisted_fields_only(ledger, hierarchy):
    resp = rag.ask(ledger, "model abstain receipts", hierarchy=hierarchy)
    assert "model" in resp["synthesis"]
    assert "ABSTAIN=2" in resp["synthesis"]
