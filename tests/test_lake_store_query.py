"""ReceiptLedger.query reads only the validated chain and fails closed.

Before this guard, query() skipped any line that did not parse and returned
the rest as if it were the whole ledger. These temp-dir controls pin that:

  * a clean ledger still returns every record, newest-first, with since/limit;
  * a corrupted organ (torn tail, malformed or altered record) raises
    LedgerCorruptionError instead of silently dropping records, and the
    corrupt bytes stay byte-identical;
  * bytes appended after validation (another writer racing the read) are not
    returned, and validated bytes rewritten in place raise;
  * the HTTP API maps a corrupted organ to 503 ledger_corrupted.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from szl_lake_store import LedgerCorruptionError, ReceiptLedger  # noqa: E402

ORGAN = "audit"
DAY = "2026-09-30"


def receipt(n, ts=f"{DAY}T00:00:0{0}Z"):
    return {"id": f"r{n}", "organ": ORGAN, "ts": ts, "decision": "ADMIT"}


def seeded(tmp_path, n=3):
    root = str(tmp_path / "ledger")
    led = ReceiptLedger(root)
    for i in range(1, n + 1):
        led.append(receipt(i, ts=f"{DAY}T00:00:0{i}Z"))
    return root, Path(root) / ORGAN / f"{DAY}.ndjson", led


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ids(envs):
    return [e["receipt_id"] for e in envs]


def test_clean_ledger_query_is_unchanged(tmp_path):
    root, _, led = seeded(tmp_path)
    fresh = ReceiptLedger(root)
    for reader in (led, fresh):
        out = reader.query(organ=ORGAN)
        assert ids(out) == ["r3", "r2", "r1"]
        assert ids(reader.query(organ=ORGAN, since=f"{DAY}T00:00:02Z")) == ["r3", "r2"]
        assert len(reader.query(limit=1)) == 1
        assert ids(reader.query()) == ["r3", "r2", "r1"]
    assert ReceiptLedger(str(tmp_path / "empty")).query(organ=ORGAN) == []


@pytest.mark.parametrize("damage", ["torn_tail", "garbage_middle", "altered_record"])
def test_corrupted_organ_query_raises_and_leaves_bytes(tmp_path, damage):
    root, path, _ = seeded(tmp_path)
    lines = path.read_bytes().splitlines(keepends=True)
    if damage == "torn_tail":
        path.write_bytes(b"".join(lines) + b'{"receipt_id": "r4", "chain')
    elif damage == "garbage_middle":
        path.write_bytes(lines[0] + b"not json\n" + b"".join(lines[1:]))
    else:
        path.write_bytes(lines[0] + lines[1].replace(b'"r2"', b'"rX"') + lines[2])
    before = digest(path)

    reader = ReceiptLedger(root)
    with pytest.raises(LedgerCorruptionError) as info:
        reader.query(organ=ORGAN)
    assert info.value.organ == ORGAN
    with pytest.raises(LedgerCorruptionError):
        reader.query()  # an all-organ query cannot hide the corrupted organ either
    assert digest(path) == before


def test_bytes_appended_after_validation_are_not_returned(tmp_path, monkeypatch):
    root, path, _ = seeded(tmp_path)
    reader = ReceiptLedger(root)
    original = ReceiptLedger._state

    def racing_state(self, organ):
        st = original(self, organ)
        with open(path, "ab") as fh:  # another writer's line lands after validation
            fh.write(b'{"receipt_id": "late", "half-writ')
        return st

    monkeypatch.setattr(ReceiptLedger, "_state", racing_state)
    assert ids(reader.query(organ=ORGAN)) == ["r3", "r2", "r1"]


def test_validated_bytes_rewritten_in_place_raise(tmp_path, monkeypatch):
    root, path, _ = seeded(tmp_path)
    reader = ReceiptLedger(root)
    original = ReceiptLedger._state

    def tampering_state(self, organ):
        st = original(self, organ)
        data = bytearray(path.read_bytes())
        data[0:1] = b"#"  # same length, no longer JSON
        path.write_bytes(bytes(data))
        return st

    monkeypatch.setattr(ReceiptLedger, "_state", tampering_state)
    with pytest.raises(LedgerCorruptionError) as info:
        reader.query(organ=ORGAN)
    assert info.value.reason == "unparseable_line"
    assert info.value.line == 1


def test_query_after_validation_truncation_raises(tmp_path, monkeypatch):
    root, path, _ = seeded(tmp_path)
    reader = ReceiptLedger(root)
    original = ReceiptLedger._state

    def truncating_state(self, organ):
        st = original(self, organ)
        path.write_bytes(path.read_bytes()[:10])
        return st

    monkeypatch.setattr(ReceiptLedger, "_state", truncating_state)
    with pytest.raises(LedgerCorruptionError) as info:
        reader.query(organ=ORGAN)
    assert info.value.reason == "truncated_after_validation"


def test_http_receipts_maps_corruption_to_503(tmp_path):
    testclient = pytest.importorskip("fastapi.testclient")
    import szl_lake_server

    root, path, _ = seeded(tmp_path)
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(lines[0] + b"not json\n" + b"".join(lines[1:]))
    szl_lake_server.app.state.ledger = ReceiptLedger(root)
    try:
        client = testclient.TestClient(szl_lake_server.app)
        r = client.get("/api/lake/v1/receipts", params={"organ": ORGAN})
        assert r.status_code == 503
        assert r.json()["error"] == "ledger_corrupted"
        assert r.json()["corruption"]["organ"] == ORGAN
    finally:
        szl_lake_server.app.state.ledger = None
