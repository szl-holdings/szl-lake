"""Tests for szl_lake_hier: leveled Merkle hierarchy over validated summaries."""
import hashlib
import re

import pytest

import szl_lake_hier as hier
import szl_lake_sql as sql
from szl_lake_store import ReceiptLedger, canonical_json

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
    return ReceiptLedger(str(tmp_path / "lake"))


def filled(ledger):
    ledger.append(receipt(1, confidential="NEVER_PUBLISH_RAW_PAYLOAD"))
    ledger.append(receipt(2, decision="DENY"))
    ledger.append(receipt(3, organ="model", decision="ABSTAIN"))
    return sql.summarize(ledger)


def raises_code(summary, code):
    with pytest.raises(hier.HierarchyError) as caught:
        hier.project_hierarchy(summary)
    return caught.value.code == code


def test_project_builds_three_levels_with_sha3_digests(ledger):
    doc = hier.project_hierarchy(filled(ledger))
    assert doc["schema"] == hier.SCHEMA
    assert doc["levels"] == 3
    assert doc["hash_alg"] == "sha3_256"
    assert HEX64.match(doc["root"]["digest"])
    assert doc["root"]["organ_count"] == 2
    assert doc["total_rows"] == 3
    assert [n["organ"] for n in doc["organs"]] == ["audit", "model"]
    assert all(HEX64.match(n["digest"]) for n in doc["organs"])
    # L0 leaves: audit(ALLOW, DENY), model(ABSTAIN)
    assert len(doc["leaves"]) == 3
    assert all(HEX64.match(leaf["digest"]) for leaf in doc["leaves"])


def test_digests_are_deterministic_and_recomputable(ledger):
    summary = filled(ledger)
    first = hier.project_hierarchy(summary)
    second = hier.project_hierarchy(sql.summarize(ledger))
    assert first["root"]["digest"] == second["root"]["digest"]
    assert [n["digest"] for n in first["organs"]] == [n["digest"] for n in second["organs"]]
    # Independent recompute of the root digest from the published payload.
    payload = {
        "level": 2,
        "total_rows": first["total_rows"],
        "organ_digests": [[n["organ"], n["digest"]] for n in first["organs"]],
    }
    blob = canonical_json(payload)
    if isinstance(blob, str):
        blob = blob.encode("utf-8")
    recomputed = hashlib.sha3_256(blob).hexdigest()
    assert recomputed == first["root"]["digest"]


def test_empty_ledger_projects_empty_hierarchy(ledger):
    doc = hier.project_hierarchy(sql.summarize(ledger))
    assert doc["source"]["state"] == "EMPTY"
    assert doc["organs"] == []
    assert doc["leaves"] == []
    assert doc["total_rows"] == 0
    assert HEX64.match(doc["root"]["digest"])


def test_organ_node_binds_chain_head(ledger):
    summary = filled(ledger)
    doc = hier.project_hierarchy(summary)
    head = ledger.chain_head("audit")
    node = next(n for n in doc["organs"] if n["organ"] == "audit")
    assert node["chain_head"] == head["chain_head"]
    assert node["chain_index"] == head["chain_index"]


def test_organ_path_and_unknown_organ(ledger):
    doc = hier.project_hierarchy(filled(ledger))
    path = hier.organ_path(doc, "audit")
    assert path["organ"] == "audit"
    assert path["root_digest"] == doc["root"]["digest"]
    assert HEX64.match(path["organ_digest"])
    with pytest.raises(hier.HierarchyError) as caught:
        hier.organ_path(doc, "nonexistent")
    assert caught.value.code == "HIER_UNKNOWN_ORGAN"


def test_source_contract_violations_fail_closed(ledger):
    summary = filled(ledger)

    bad_schema = dict(summary, schema="szl.lake.evil/v1")
    assert raises_code(bad_schema, "HIER_BAD_SCHEMA")

    written = dict(summary, ledger_writes=1)
    assert raises_code(written, "HIER_LEDGER_WRITTEN")

    leaked = dict(summary, receipts_returned=3)
    assert raises_code(leaked, "HIER_RECEIPTS_LEAKED")

    signed = dict(summary, signatures_verified=True)
    assert raises_code(signed, "HIER_SIGNATURES_CLAIMED")

    produced = dict(summary, production_authorized=True)
    assert raises_code(produced, "HIER_PRODUCTION_CLAIMED")

    engine = dict(summary, analytics_engine="postgres:prod")
    assert raises_code(engine, "HIER_BAD_ENGINE")

    covered = dict(summary, coverage_complete=False)
    assert raises_code(covered, "HIER_BAD_COVERAGE")

    filtered = dict(summary, filter={"organ": "audit", "extra": 1})
    assert raises_code(filtered, "HIER_BAD_FILTER")


def test_group_and_head_band_violations_fail_closed(ledger):
    summary = filled(ledger)

    groups = [dict(item) for item in summary["groups"]]
    groups[0]["extra_key"] = True
    assert raises_code(dict(summary, groups=groups), "HIER_BAD_GROUPS")

    heads = [dict(item) for item in summary["source_heads"]]
    heads[0] = dict(heads[0], count=heads[0]["count"] + 1,
                   chain_index=heads[0]["chain_index"] + 1)
    assert raises_code(dict(summary, source_heads=heads), "HIER_HEAD_COUNT_MISMATCH")

    heads2 = [dict(item) for item in summary["source_heads"]]
    heads2[0] = dict(heads2[0], chain_index=heads2[0]["count"] + 5)
    assert raises_code(dict(summary, source_heads=heads2), "HIER_BAD_HEADS")

    heads3 = [dict(item) for item in summary["source_heads"][1:]]
    assert raises_code(dict(summary, source_heads=heads3), "HIER_HEAD_MISMATCH")

    counts = dict(summary, rows_matching_filter=2)
    assert raises_code(counts, "HIER_COUNT_MISMATCH")


def test_summary_must_be_a_dict():
    assert raises_code(["not", "a", "dict"], "HIER_SOURCE_NOT_DICT")
