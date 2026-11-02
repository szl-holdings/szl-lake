"""Aggregate reads retain observed history without recreating missing storage."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from szl_lake_store import LedgerCorruptionError, ReceiptLedger  # noqa: E402


def receipt(organ, number=1):
    return {"id": f"{organ}-{number}", "organ": organ,
            "ts": "2026-11-02T00:00:00Z"}


@pytest.mark.parametrize("observation", ["append", "read"])
@pytest.mark.parametrize("removal", ["organ", "root"])
@pytest.mark.parametrize("first_read", ["health", "query"])
def test_aggregate_reads_reject_disappeared_observed_history(
        tmp_path, observation, removal, first_read):
    root = tmp_path / "ledger"
    ledger = ReceiptLedger(str(root))
    ledger.append(receipt("missing"))
    if observation == "read":
        ledger = ReceiptLedger(str(root))
        assert ledger.health()["total_receipts"] == 1
    removed = root / "missing" if removal == "organ" else root
    shutil.rmtree(removed)

    if first_read == "health":
        assert ledger.health()["ok"] is False
    else:
        with pytest.raises(LedgerCorruptionError) as caught:
            ledger.query()
        assert caught.value.reason == "observed_tail_missing"

    # Corruption remains visible on subsequent aggregate reads, including
    # after another entry point has already cached the zero-length prefix.
    for _ in range(2):
        health = ledger.health()
        assert health["ok"] is False
        assert health["corrupted_organs"] == ["missing"]
        assert health["organs"]["missing"]["corruption"]["reason"] == "observed_tail_missing"
        assert health["total_receipts"] == 0
        with pytest.raises(LedgerCorruptionError):
            ledger.query(limit=1, since="2099-01-01T00:00:00Z")
        assert not removed.exists()

    # Observations remain process-local; explicit reload is the existing reset.
    ledger.reload()
    assert ledger.health()["ok"] is True
    assert ledger.query() == []
    assert not removed.exists()


@pytest.mark.parametrize("removal", ["organ", "root"])
def test_aggregate_reads_keep_corruption_with_no_valid_prefix(tmp_path, removal):
    root = tmp_path / "ledger"
    writer = ReceiptLedger(str(root))
    writer.append(receipt("missing"))
    partition = next((root / "missing").glob("*.ndjson"))
    partition.write_text("not JSON\n", encoding="utf-8")
    ledger = ReceiptLedger(str(root))
    health = ledger.health()
    diagnostic = health["organs"]["missing"]["corruption"]
    assert health["total_receipts"] == 0
    assert diagnostic["reason"] == "unparseable_line"
    removed = root / "missing" if removal == "organ" else root
    shutil.rmtree(removed)
    health = ledger.health()
    assert health["ok"] is False
    assert health["organs"]["missing"]["corruption"] == diagnostic
    with pytest.raises(LedgerCorruptionError) as caught:
        ledger.query()
    assert caught.value.as_dict() == diagnostic
    assert not removed.exists()


def test_unobserved_and_empty_storage_remain_empty_without_creation(tmp_path):
    root = tmp_path / "absent"
    ledger = ReceiptLedger(str(root))
    assert ledger.chain_head("unknown")["count"] == 0
    assert ledger.health()["ok"] is True
    assert ledger.health()["organs"] == {}
    assert ledger.query() == []
    assert not root.exists()
    # A formerly empty directory has no validated history to retain.
    (root / "empty").mkdir(parents=True)
    assert ledger.health()["organs"]["empty"]["count"] == 0
    shutil.rmtree(root)
    assert ledger.health()["ok"] is True
    assert ledger.health()["organs"] == {}
    assert not root.exists()


def test_new_disk_organs_and_filtered_queries_keep_their_scope(tmp_path):
    root = tmp_path / "ledger"
    ledger = ReceiptLedger(str(root))
    ledger.append(receipt("missing"))
    writer = ReceiptLedger(str(root))
    writer.append(receipt("healthy"))
    assert set(ledger.health()["organs"]) == {"missing", "healthy"}
    assert len(ledger.query()) == 2  # observed names are not duplicated
    shutil.rmtree(root / "missing")
    health = ledger.health()
    assert health["ok"] is False
    assert health["total_receipts"] == 1
    assert health["corrupted_organs"] == ["missing"]
    assert [item["organ"] for item in ledger.query(organ="healthy")] == ["healthy"]
    with pytest.raises(LedgerCorruptionError):
        ledger.query()
    assert not (root / "missing").exists()
