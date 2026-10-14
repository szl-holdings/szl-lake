"""Restart-recovery and writer-serialization controls for ReceiptLedger.

Every fixture lives in a pytest temp dir; stdlib + pytest only. Covers:

  * recovery replays and validates every persisted record before an append is
    admitted (malformed trailing records, altered link metadata);
  * a corrupted organ fails closed: appends are refused with a typed error that
    names the first bad line, and the corrupt bytes stay byte-identical;
  * independent writers (two instances, threads, two real processes) serialize
    on the per-organ OS lock and still produce one contiguous valid chain;
  * clean restarts (including legacy CRLF partitions) keep working.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from szl_lake_store import (  # noqa: E402
    LedgerCorruptionError,
    LedgerLockTimeout,
    ReceiptLedger,
    canonical_hash,
)

ORGAN = "audit"
DAY = "2026-09-30"


def receipt(n, ts=f"{DAY}T00:00:00Z", organ=ORGAN):
    return {"id": f"r{n}", "organ": organ, "ts": ts, "decision": "ADMIT"}


def seeded(tmp_path, n=3):
    root = str(tmp_path / "ledger")
    led = ReceiptLedger(root)
    for i in range(1, n + 1):
        led.append(receipt(i))
    return root, Path(root) / ORGAN / f"{DAY}.ndjson", led


def snapshot(root, organ=ORGAN):
    """SHA-256 of every partition file (the lock file carries no evidence)."""
    d = Path(root) / organ
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.glob("*.ndjson"))}


def read_lines(path):
    return path.read_bytes().splitlines(keepends=True)


def dump(env):
    return (json.dumps(env, sort_keys=True, separators=(",", ":"))
            + "\n").encode("utf-8")


def link_hash(env):
    return canonical_hash({
        "prev_hash": env["prev_hash"],
        "receipt_id": env["receipt_id"],
        "organ": env["organ"],
        "ts": env["ts"],
        "chain_index": env["chain_index"],
    })


def edit(idx, mutate):
    """Line-list mutator: rewrite record ``idx`` after ``mutate(env)``."""
    def apply(lines):
        env = json.loads(lines[idx])
        mutate(env)
        lines[idx] = dump(env)
        return lines
    return apply


def _flip(h):
    return ("0" if h[0] != "0" else "1") + h[1:]


def _set(**fields):
    return lambda env: env.update(fields)


def _reindex_and_rehash(env):
    # A careful tamper: change the index AND recompute the record's own hash.
    env["chain_index"] = 41
    env["chain_hash"] = link_hash(env)


def _drop(key):
    return lambda env: env.pop(key)


# ------------------------------------------------------------ clean restart --

def test_clean_restart_resumes_the_same_chain(tmp_path):
    root, _, first = seeded(tmp_path)
    head = first.chain_head(ORGAN)
    second = ReceiptLedger(root)
    assert second.chain_head(ORGAN) == head
    assert second.append(receipt(1))["duplicate"] is True
    nxt = second.append(receipt(4))
    assert nxt["accepted"] is True
    assert nxt["chain_index"] == 4
    assert nxt["ledger_offset"] == 4
    stored = second.query(organ=ORGAN, limit=1)[0]
    assert stored["prev_hash"] == head["chain_head"]
    assert second.verify_chain(ORGAN)["ok"] is True
    assert ReceiptLedger(root).chain_head(ORGAN) == second.chain_head(ORGAN)


def test_legacy_crlf_partition_still_recovers(tmp_path):
    # Text-mode writes on Windows produced CRLF line endings; those ledgers
    # are valid and must keep recovering and accepting appends.
    root, path, first = seeded(tmp_path)
    head = first.chain_head(ORGAN)
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    led = ReceiptLedger(root)
    assert led.chain_head(ORGAN) == head
    assert led.append(receipt(4))["chain_index"] == 4
    assert led.verify_chain(ORGAN)["ok"] is True
    assert ReceiptLedger(root).chain_head(ORGAN)["chain_index"] == 4


def test_backdated_receipts_keep_stored_order_equal_to_chain_order(tmp_path):
    # A receipt whose ts falls before the newest partition is appended to that
    # newest partition, so a restart replays the chain in order.
    root = str(tmp_path / "ledger")
    led = ReceiptLedger(root)
    led.append(receipt(1, ts="2026-06-02T00:00:05Z"))
    led.append(receipt(2, ts="2026-06-01T23:59:59Z"))
    led.append(receipt(3, ts="2026-06-03T08:00:00Z"))
    led.append(receipt(4, ts="2026-06-02T12:00:00Z"))
    head = led.chain_head(ORGAN)
    assert head["chain_index"] == 4
    assert sorted(snapshot(root)) == ["2026-06-02.ndjson", "2026-06-03.ndjson"]

    restarted = ReceiptLedger(root)
    assert restarted.chain_head(ORGAN) == head
    assert restarted.verify_chain(ORGAN)["ok"] is True
    assert restarted.append(receipt(5))["chain_index"] == 5
    assert restarted.verify_chain(ORGAN)["ok"] is True
    stamps = {e["receipt_id"]: e["ts"] for e in restarted.query(organ=ORGAN)}
    assert stamps["r2"] == "2026-06-01T23:59:59Z"  # ts itself is untouched


# ------------------------------------------------- fail-closed on corruption --

@pytest.mark.parametrize("tail, reason", [
    (b'{"chain_index":4,"trunc', "unterminated_record"),
    (b"not json\n", "unparseable_line"),
    (b"[1, 2]\n", "unparseable_line"),
])
def test_malformed_trailing_record_fails_closed(tmp_path, tail, reason):
    root, path, _ = seeded(tmp_path)
    with open(path, "ab") as fh:
        fh.write(tail)
    before = snapshot(root)

    led = ReceiptLedger(root)
    with pytest.raises(LedgerCorruptionError) as caught:
        led.append(receipt(4))
    err = caught.value
    assert err.reason == reason
    assert err.line == 4
    assert err.position == 4
    assert err.organ == ORGAN
    assert Path(err.path) == path
    assert err.as_dict()["file"] == f"{ORGAN}/{DAY}.ndjson"

    with pytest.raises(LedgerCorruptionError):
        led.chain_head(ORGAN)
    health = led.health()
    assert health["ok"] is False
    assert health["corrupted_organs"] == [ORGAN]
    assert health["organs"][ORGAN]["count"] == 3  # verified prefix only
    assert health["organs"][ORGAN]["corruption"]["line"] == 4
    assert snapshot(root) == before


@pytest.mark.parametrize("mutate, line, reason", [
    (edit(2, _set(chain_index=41)), 3, "index_discontinuity"),
    (edit(2, _reindex_and_rehash), 3, "index_discontinuity"),
    (edit(2, lambda e: e.update(chain_hash=_flip(e["chain_hash"]))), 3,
     "chain_hash_mismatch"),
    (edit(2, _set(prev_hash="0" * 64)), 3, "prev_hash_mismatch"),
    (edit(1, _set(chain_index=7)), 2, "index_discontinuity"),
    (edit(1, _set(receipt_id="forged")), 2, "chain_hash_mismatch"),
    (edit(1, _set(organ="other")), 2, "organ_mismatch"),
    (edit(1, _drop("schema")), 2, "malformed_envelope"),
    (edit(0, _set(chain_index=True)), 1, "malformed_envelope"),
    (edit(1, _set(receipt=None)), 2, "malformed_receipt"),
    (edit(1, lambda e: e["receipt"].update(id="other")), 2,
     "receipt_id_mismatch"),
    (lambda ls: [ls[0], ls[2]], 2, "index_discontinuity"),        # deletion
    (lambda ls: [ls[0], ls[1], ls[1], ls[2]], 3, "index_discontinuity"),
    (lambda ls: [ls[1], ls[0], ls[2]], 1, "index_discontinuity"),  # reorder
])
def test_altered_link_metadata_fails_closed(tmp_path, mutate, line, reason):
    root, path, _ = seeded(tmp_path)
    path.write_bytes(b"".join(mutate(read_lines(path))))
    before = snapshot(root)

    with pytest.raises(LedgerCorruptionError) as caught:
        ReceiptLedger(root).append(receipt(4))
    assert (caught.value.reason, caught.value.line) == (reason, line)
    assert caught.value.position == line
    assert snapshot(root) == before


def test_restart_after_corruption_keeps_refusing_and_preserves_bytes(tmp_path):
    root, path, _ = seeded(tmp_path)
    path.write_bytes(b"".join(edit(
        1, lambda e: e.update(chain_hash=_flip(e["chain_hash"])))(
            read_lines(path))))
    before = snapshot(root)
    listing = sorted(os.listdir(Path(root) / ORGAN))

    for _ in range(3):  # each iteration is a fresh process-equivalent restart
        led = ReceiptLedger(root)
        with pytest.raises(LedgerCorruptionError) as caught:
            led.append(receipt(4))
        assert (caught.value.reason, caught.value.line) == (
            "chain_hash_mismatch", 2)
        # A receipt for a NEW partition date is refused too: no new file.
        with pytest.raises(LedgerCorruptionError):
            led.append(receipt(5, ts="2026-10-01T00:00:00Z"))
        with pytest.raises(LedgerCorruptionError):
            led.append_batch([receipt(6), receipt(7)])
        # Duplicates of verified records are refused as well (fail closed).
        with pytest.raises(LedgerCorruptionError):
            led.append(receipt(1))
        led.reload()

    assert snapshot(root) == before
    assert sorted(os.listdir(Path(root) / ORGAN)) == listing
    assert ReceiptLedger(root).verify_chain(ORGAN)["ok"] is False

    # Corruption is scoped to the damaged organ's chain.
    other = ReceiptLedger(root).append(receipt(1, organ="other"))
    assert other["accepted"] is True
    assert ReceiptLedger(root).verify_chain("other")["ok"] is True


def test_running_writer_rechecks_head_on_disk_before_appending(tmp_path):
    # Tampering behind a live writer: the cached head is not trusted blindly.
    root, path, led = seeded(tmp_path)
    led.chain_head(ORGAN)  # warm the in-memory state
    path.write_bytes(b"".join(edit(2, _set(chain_index=41))(read_lines(path))))
    before = snapshot(root)
    with pytest.raises(LedgerCorruptionError) as caught:
        led.append(receipt(4))
    assert (caught.value.reason, caught.value.line) == (
        "index_discontinuity", 3)
    assert snapshot(root) == before


def test_reads_do_not_create_directories(tmp_path):
    root = tmp_path / "ledger"
    led = ReceiptLedger(str(root))
    assert led.chain_head("never-written")["count"] == 0
    assert led.verify_chain("never-written")["ok"] is True
    assert led.health()["total_receipts"] == 0
    assert not root.exists()


# --------------------------------------------------- independent writers ----

def test_two_instances_append_to_one_chain(tmp_path):
    root = str(tmp_path / "ledger")
    a, b = ReceiptLedger(root), ReceiptLedger(root)
    a.chain_head(ORGAN)
    b.chain_head(ORGAN)  # both warmed on the same (empty) head
    for i in range(1, 11):
        writer = a if i % 2 else b
        res = writer.append(receipt(i))
        assert res["accepted"] is True
        assert res["chain_index"] == i
    assert a.chain_head(ORGAN) == b.chain_head(ORGAN)
    assert b.append(receipt(1))["duplicate"] is True  # written by a
    rep = ReceiptLedger(root).verify_chain(ORGAN)
    assert rep["ok"] is True
    assert rep["count"] == 10


def test_two_instances_across_threads_append_one_chain(tmp_path):
    root = str(tmp_path / "ledger")
    ledgers = [ReceiptLedger(root), ReceiptLedger(root)]
    start = threading.Barrier(4)
    indices: list[int] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def work(led, tag):
        try:
            start.wait()
            for i in range(25):
                res = led.append({"id": f"{tag}-{i}", "organ": ORGAN,
                                  "ts": f"{DAY}T00:00:00Z"})
                assert res["accepted"] is True
                with guard:
                    indices.append(res["chain_index"])
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(ledgers[k % 2], f"t{k}"))
               for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors
    assert sorted(indices) == list(range(1, 101))
    rep = ReceiptLedger(root).verify_chain(ORGAN)
    assert rep["ok"] is True
    assert rep["count"] == 100


WORKER = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from szl_lake_store import ReceiptLedger
root, tag, n, ready, go = (sys.argv[2], sys.argv[3], int(sys.argv[4]),
                           sys.argv[5], sys.argv[6])
led = ReceiptLedger(root)
led.chain_head("audit")  # warm state before the race starts
open(ready, "w").close()
deadline = time.monotonic() + 60
while not os.path.exists(go):
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.002)
out = []
for i in range(n):
    res = led.append({"id": "%s-%d" % (tag, i), "organ": "audit",
                      "ts": "2026-09-30T00:00:00Z"})
    if not res["accepted"]:
        sys.exit(4)
    out.append(res["chain_index"])
print(json.dumps(out))
"""


def test_two_processes_append_concurrently_one_chain(tmp_path):
    root = str(tmp_path / "ledger")
    go = tmp_path / "go"
    per_worker = 40
    procs, ready = [], []
    for k in range(2):
        flag = tmp_path / f"ready-{k}"
        ready.append(flag)
        procs.append(subprocess.Popen(
            [sys.executable, "-c", WORKER, str(REPO), root, f"p{k}",
             str(per_worker), str(flag), str(go)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    try:
        deadline = time.monotonic() + 60
        while not all(f.exists() for f in ready):
            assert time.monotonic() < deadline, "workers never became ready"
            assert all(p.poll() is None for p in procs), "a worker exited early"
            time.sleep(0.01)
        go.touch()
        outputs = [p.communicate(timeout=120) for p in procs]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
    for p, (out, err) in zip(procs, outputs):
        assert p.returncode == 0, err
    got = [json.loads(out) for out, _ in outputs]
    assert len(got[0]) == len(got[1]) == per_worker
    assert not set(got[0]) & set(got[1])
    assert sorted(got[0] + got[1]) == list(range(1, 2 * per_worker + 1))

    led = ReceiptLedger(root)
    rep = led.verify_chain(ORGAN)
    assert rep["ok"] is True, rep["broken"][:3]
    assert rep["count"] == 2 * per_worker
    assert led.chain_head(ORGAN)["chain_index"] == 2 * per_worker
    assert led.append(receipt(1))["chain_index"] == 2 * per_worker + 1


def test_lock_wait_is_bounded_and_typed(tmp_path):
    root, _, holder = seeded(tmp_path, n=1)
    waiter = ReceiptLedger(root, lock_timeout=0.2)
    with holder._organ_lock(ORGAN, write=True):
        with pytest.raises(LedgerLockTimeout):
            waiter.append(receipt(2))
    assert waiter.append(receipt(2))["chain_index"] == 2

# -------------------------------------- live-instance observed history ------

@pytest.mark.parametrize("verify_first", [False, True])
def test_cached_history_equal_size_non_head_edit_refuses(tmp_path, verify_first):
    root, path, led = seeded(tmp_path, n=2)
    led.chain_head(ORGAN)  # explicitly warm the cached-history regression
    original = read_lines(path)
    changed = edit(0, _set(ts=f"{DAY}T00:00:01Z"))(original.copy())
    path.write_bytes(b"".join(changed))
    assert path.stat().st_size == len(b"".join(original))
    assert read_lines(path)[-1] == original[-1]
    before = snapshot(root)

    if verify_first:
        assert led.verify_chain(ORGAN)["ok"] is False
    with pytest.raises(LedgerCorruptionError) as caught:
        led.append(receipt(3))
    assert (caught.value.reason, caught.value.position, caught.value.line) == (
        "chain_hash_mismatch", 1, 1)
    assert snapshot(root) == before
    assert led.verify_chain(ORGAN)["ok"] is False
    for action in (lambda: led.append(receipt(4)),
                   lambda: led.chain_head(ORGAN),
                   lambda: led.query(organ=ORGAN)):
        with pytest.raises(LedgerCorruptionError):
            action()
    assert snapshot(root) == before


@pytest.mark.parametrize("removal", ["last_line", "newest_partition"])
@pytest.mark.parametrize("refresh", ["append", "head", "health", "query"])
def test_observed_tail_removal_refuses(tmp_path, removal, refresh):
    root, path, led = seeded(tmp_path, n=1)
    latest_ts = (f"{DAY}T00:00:01Z" if removal == "last_line"
                 else "2026-10-01T00:00:00Z")
    led.append(receipt(2, ts=latest_ts))
    observed = led.chain_head(ORGAN)
    assert observed["chain_index"] == 2
    if removal == "last_line":
        path.write_bytes(read_lines(path)[0])
        expected_path, expected_line = path, 2
    else:
        expected_path = Path(root) / ORGAN / "2026-10-01.ndjson"
        expected_path.unlink()
        expected_line = 1
    before = snapshot(root)
    # Independent structural verification cannot know the lost observation.
    assert led.verify_chain(ORGAN)["ok"] is True

    if refresh == "health":
        assert led.health()["ok"] is False
    elif refresh != "append":
        with pytest.raises(LedgerCorruptionError):
            (led.chain_head(ORGAN) if refresh == "head"
             else led.query(organ=ORGAN))
    with pytest.raises(LedgerCorruptionError) as caught:
        led.append(receipt(3, ts="2026-10-02T00:00:00Z"))
    error = caught.value
    assert error.reason == "observed_tail_missing"
    assert (error.position, error.line, Path(error.path)) == (
        2, expected_line, expected_path)
    assert observed["chain_head"] in error.detail
    assert "2" in error.detail
    assert snapshot(root) == before
    assert led.verify_chain(ORGAN)["ok"] is True
    with pytest.raises(LedgerCorruptionError):
        led.append(receipt(4))
    assert snapshot(root) == before


@pytest.mark.parametrize("extra_records", [0, 1])
def test_observed_checkpoint_rejects_self_consistent_rewrite(tmp_path, extra_records):
    root, path, led = seeded(tmp_path, n=2)
    observed = led.chain_head(ORGAN)
    if extra_records:
        ReceiptLedger(root).append(receipt(3))
    envs = [json.loads(line) for line in read_lines(path)]
    envs[0]["ts"] = f"{DAY}T00:00:01Z"
    previous_hash = None
    for env in envs:
        env["prev_hash"] = previous_hash
        env["chain_hash"] = link_hash(env)
        previous_hash = env["chain_hash"]
    path.write_bytes(b"".join(map(dump, envs)))
    before = snapshot(root)
    assert ReceiptLedger(root).verify_chain(ORGAN)["ok"] is True
    with pytest.raises(LedgerCorruptionError) as caught:
        led.append(receipt(4))
    assert caught.value.reason == "observed_head_mismatch"
    assert (caught.value.position, caught.value.line) == (2, 2)
    assert observed["chain_head"] in caught.value.detail
    assert envs[1]["chain_hash"] in caught.value.detail
    for _ in range(2):
        with pytest.raises(LedgerCorruptionError):
            led.chain_head(ORGAN)
        assert led.health()["ok"] is False
        assert led.verify_chain(ORGAN)["ok"] is True
        with pytest.raises(LedgerCorruptionError):
            led.append(receipt(5))
    assert snapshot(root) == before


@pytest.mark.parametrize("new_partition", [False, True])
def test_observed_checkpoint_allows_legitimate_extension(tmp_path, new_partition):
    root, _, led = seeded(tmp_path, n=2)
    observed = led.chain_head(ORGAN)
    ts = "2026-10-01T00:00:00Z" if new_partition else f"{DAY}T00:00:01Z"
    other = ReceiptLedger(root)
    extension = other.append(receipt(3, ts=ts))
    continued = led.append(receipt(4, ts=ts))
    assert extension["chain_index"] == extension["ledger_offset"] == 3
    assert continued["chain_index"] == continued["ledger_offset"] == 4
    assert led.chain_head(ORGAN)["chain_head"] == continued["chain_head"]
    assert led.append(receipt(3))["duplicate"] is True
    assert led.verify_chain(ORGAN)["ok"] is True
    stored = {env["chain_index"]: env for env in led.query(ORGAN)}
    assert stored[3]["prev_hash"] == observed["chain_head"]
    assert stored[4]["prev_hash"] == extension["chain_head"]


def test_observed_missing_directory_refuses_without_creating_on_read(tmp_path):
    import shutil

    root, path, led = seeded(tmp_path, n=2)
    observed = led.chain_head(ORGAN)
    shutil.rmtree(path.parent)
    with pytest.raises(LedgerCorruptionError) as caught:
        led.chain_head(ORGAN)
    assert caught.value.reason == "observed_tail_missing"
    assert (caught.value.position, caught.value.line) == (1, 1)
    assert observed["chain_head"] in caught.value.detail
    assert not path.parent.exists()
    with pytest.raises(LedgerCorruptionError):
        led.append(receipt(3))
    assert snapshot(root) == {}


def test_observed_first_record_corruption_stays_after_directory_removal(tmp_path):
    import shutil

    root, path, _ = seeded(tmp_path, n=1)
    path.write_bytes(b"not json\n")
    led = ReceiptLedger(root)
    with pytest.raises(LedgerCorruptionError) as caught:
        led.chain_head(ORGAN)
    diagnostic = caught.value.as_dict()
    assert diagnostic["position"] == 1
    assert led.health()["organs"][ORGAN]["count"] == 0
    shutil.rmtree(path.parent)
    with pytest.raises(LedgerCorruptionError) as again:
        led.chain_head(ORGAN)
    assert again.value.as_dict() == diagnostic
    assert not path.parent.exists()
    with pytest.raises(LedgerCorruptionError) as refused:
        led.append(receipt(2))
    assert refused.value.as_dict() == diagnostic
    assert snapshot(root) == {}
    # Explicit reload is the existing, intentional reset boundary.
    led.reload()
    assert led.chain_head(ORGAN)["count"] == 0


def test_observed_valid_shortened_chain_has_no_restart_anchor(tmp_path):
    root, path, led = seeded(tmp_path, n=2)
    led.chain_head(ORGAN)
    path.write_bytes(read_lines(path)[0])
    before = snapshot(root)
    fresh = ReceiptLedger(root)
    assert fresh.chain_head(ORGAN)["chain_index"] == 1
    assert fresh.verify_chain(ORGAN)["ok"] is True
    led.reload()
    assert led.chain_head(ORGAN) == fresh.chain_head(ORGAN)
    assert snapshot(root) == before
    assert led.append(receipt(3))["chain_index"] == 2

# ------------------------------------ immediately acknowledged observations --

@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("removal", ["last_line", "newest_partition"])
def test_acknowledged_tail_loss_refuses_before_any_refresh(tmp_path, batch, removal):
    root = str(tmp_path / "ledger")
    led = ReceiptLedger(root)
    last_ts = (f"{DAY}T00:00:01Z" if removal == "last_line"
               else "2026-10-01T00:00:00Z")
    records = [receipt(1), receipt(2, ts=last_ts)]
    if batch:
        accepted = led.append_batch(records)["results"]
    else:
        accepted = [led.append(rec) for rec in records]
    last = accepted[-1]
    assert last["accepted"] is True
    assert last["chain_index"] == last["ledger_offset"] == 2
    # No head, health, query, append, verify or reload between acknowledgment
    # and mutation: the confirmed write itself must retain its checkpoint.
    if removal == "last_line":
        path = Path(root) / ORGAN / f"{DAY}.ndjson"
        path.write_bytes(read_lines(path)[0])
    else:
        path = Path(root) / ORGAN / "2026-10-01.ndjson"
        path.unlink()
    before = snapshot(root)
    with pytest.raises(LedgerCorruptionError) as caught:
        led.append(receipt(3))
    assert caught.value.reason == "observed_tail_missing"
    assert caught.value.position == 2
    assert last["chain_head"] in caught.value.detail
    assert snapshot(root) == before


@pytest.mark.parametrize("batch", [False, True])
def test_acknowledged_rewritten_last_record_refuses_before_refresh(tmp_path, batch):
    root = str(tmp_path / "ledger")
    led = ReceiptLedger(root)
    if batch:
        last = led.append_batch([receipt(1), receipt(2)])["results"][-1]
    else:
        led.append(receipt(1))
        last = led.append(receipt(2))
    path = Path(root) / ORGAN / f"{DAY}.ndjson"
    lines = read_lines(path)
    env = json.loads(lines[-1])
    env["ts"] = f"{DAY}T00:00:01Z"
    env["chain_hash"] = link_hash(env)
    path.write_bytes(lines[0] + dump(env))
    before = snapshot(root)
    with pytest.raises(LedgerCorruptionError) as caught:
        led.append(receipt(3))
    assert caught.value.reason == "observed_head_mismatch"
    assert (caught.value.position, caught.value.line) == (2, 2)
    assert last["chain_head"] in caught.value.detail
    assert snapshot(root) == before
    assert led.verify_chain(ORGAN)["ok"] is True


@pytest.mark.parametrize("batch", [False, True])
def test_acknowledged_state_and_response_stay_consistent(tmp_path, batch):
    root = str(tmp_path / "ledger")
    led = ReceiptLedger(root)
    records = [receipt(1), receipt(2, ts="2026-10-01T00:00:00Z")]
    results = (led.append_batch(records)["results"] if batch
               else [led.append(rec) for rec in records])
    assert [r["chain_index"] for r in results] == [1, 2]
    assert [r["ledger_offset"] for r in results] == [1, 2]
    st = led._states[ORGAN]
    assert st.chain_index == st.count == 2
    assert st.chain_head == results[-1]["chain_head"]
    assert st.seen == {"r1", "r2"}
    for name, (offset, lines) in st.files.items():
        path = Path(root) / ORGAN / name
        assert offset == path.stat().st_size
        assert lines == len(read_lines(path))
    duplicate = led.append(receipt(2))
    assert duplicate["duplicate"] is True
    assert duplicate["chain_index"] == duplicate["ledger_offset"] == 2
    assert duplicate["chain_head"] == results[-1]["chain_head"]
    assert led.chain_head(ORGAN)["chain_head"] == results[-1]["chain_head"]


@pytest.mark.parametrize("failure", ["before_write", "partial_write", "fsync"])
def test_acknowledged_write_failure_never_advances_observation(
        tmp_path, monkeypatch, failure):
    import szl_lake_store as store

    root, path, led = seeded(tmp_path, n=1)
    observed = led.chain_head(ORGAN)
    original_bytes = path.read_bytes()

    def fail_before_write(path_arg, data):
        raise OSError("injected pre-write failure")

    def fail_after_partial_write(path_arg, data):
        with open(path_arg, "ab") as fh:
            fh.write(data[:13])
        raise OSError("injected partial-write failure")

    def fail_fsync(fd):
        raise OSError("injected fsync failure")

    with monkeypatch.context() as patcher:
        if failure == "fsync":
            patcher.setattr(store.os, "fsync", fail_fsync)
        else:
            patcher.setattr(store, "_append_durably", (
                fail_before_write if failure == "before_write"
                else fail_after_partial_write))
        with pytest.raises(OSError, match="injected"):
            led.append(receipt(2))
    st = led._states[ORGAN]
    assert st.chain_index == st.count == observed["chain_index"] == 1
    assert st.chain_head == observed["chain_head"]
    assert st.seen == {"r1"}
    assert st.files[path.name][0] == len(original_bytes)

    if failure == "partial_write":
        dirty = snapshot(root)
        assert path.read_bytes() != original_bytes
        with pytest.raises(LedgerCorruptionError) as caught:
            led.append(receipt(3))
        assert caught.value.reason == "unterminated_record"
        assert snapshot(root) == dirty
    else:
        if failure == "before_write":
            assert path.read_bytes() == original_bytes
        else:
            # Real _append_durably wrote a complete record before fsync failed.
            # Failure does not promise unchanged bytes or durable acknowledgment.
            assert len(read_lines(path)) == 2
            path.write_bytes(original_bytes)
        # Removing an unacknowledged failed write must not trip a phantom
        # checkpoint. The last confirmed record (index 1) is still present.
        assert led.append(receipt(3))["chain_index"] == 2


@pytest.mark.parametrize("failure", ["invalid_item", "partial_write"])
def test_acknowledged_partial_batch_retains_successful_item(
        tmp_path, monkeypatch, failure):
    import szl_lake_store as store

    root = str(tmp_path / "ledger")
    led = ReceiptLedger(root)
    path = Path(root) / ORGAN / f"{DAY}.ndjson"
    original_append = store._append_durably
    calls = 0

    def second_write_fails(path_arg, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            original_append(path_arg, data)
        else:
            with open(path_arg, "ab") as fh:
                fh.write(data[:13])
            raise OSError("injected second-item failure")

    if failure == "invalid_item":
        with pytest.raises(ValueError):
            led.append_batch([receipt(1), []])
    else:
        with monkeypatch.context() as patcher:
            patcher.setattr(store, "_append_durably", second_write_fails)
            with pytest.raises(OSError, match="injected"):
                led.append_batch([receipt(1), receipt(2)])
    # Batch is not all-or-nothing: item 1 succeeded even though no batch
    # response was returned. Its checkpoint must survive the later failure.
    st = led._states[ORGAN]
    assert st.chain_index == st.count == 1
    first = json.loads(read_lines(path)[0])
    assert st.chain_head == first["chain_hash"]
    path.unlink()
    with pytest.raises(LedgerCorruptionError) as caught:
        led.append(receipt(3))
    assert caught.value.reason == "observed_tail_missing"
    assert first["chain_hash"] in caught.value.detail
    assert snapshot(root) == {}
