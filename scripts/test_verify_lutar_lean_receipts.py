#!/usr/bin/env python3
"""test_verify_lutar_lean_receipts.py — negative-fixture self-test.

``verify_lutar_lean_receipts.py`` is the safety net that catches a tampered or
truncated Theorem-U proof ledger. A safety net with no test of its own can be
silently weakened (e.g. an edit that stops detecting a body edit, or that always
returns "OK") and the scheduled monitor would still go green while protecting
nothing.

This self-test feeds the verifier crafted fixtures and asserts each broken one
makes it exit non-zero, while a clean fixture exits 0 — the same negative-fixture
pattern the org's box-script alarm guards use.

The crafted breakages each target a DISTINCT detection path that runs BEFORE the
``cosign`` signature step:

  * tampered snapshot   -> snapshot-integrity check (sha256 no longer reproduces)
  * flipped receipt_id  -> body tamper-evidence check (receipt_id recompute)
  * broken prev_hash     -> chain-continuity check (prev_hash link)
  * below-baseline       -> append-only floor (--min-expected)

so the cosign binary is stubbed to succeed: the signature path is exercised for
real by the scheduled verify-anchor-receipts run against the live trust root;
here we isolate the integrity/continuity/floor logic the self-test can prove
offline. A genuine receipt from the checked-in ledger is the basis for every
fixture so snapshot bytes, embedded sigstore bundle, and DSSE digest stay
internally consistent — only the field under test is broken.

stdlib-only. Run by file path:  python3 scripts/test_verify_lutar_lean_receipts.py
Exit 0 = the verifier still detects every breakage and passes the clean case.
Exit 1 = the verifier has been weakened (a breakage slipped through, or the clean
case failed) — the safety net is no longer honest.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
VERIFIER = os.path.join(HERE, "verify_lutar_lean_receipts.py")
LEDGER = os.path.join(REPO, "data", "khipu", "lutar_lean_receipts.ndjson")


def canonical_hash(obj) -> str:
    """Match the verifier: sha256 of compact, sorted-key JSON."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def recompute_receipt_id(rec: dict) -> dict:
    """Return a copy of rec with receipt_id = canonical hash of its body.

    Used to build fixtures whose body is internally consistent so the
    body-tamper check passes and ONLY the field under test is broken.
    """
    rec = copy.deepcopy(rec)
    body = {k: v for k, v in rec.items() if k != "receipt_id"}
    rec["receipt_id"] = canonical_hash(body)
    return rec


def load_genuine_receipt() -> dict:
    with open(LEDGER, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                return json.loads(line)
    raise SystemExit(f"::error::no receipt found in {LEDGER}")


def make_cosign_stub(tmpdir: str) -> str:
    """A cosign stand-in that always succeeds, isolating non-signature checks."""
    path = os.path.join(tmpdir, "cosign-stub")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, 0o755)
    return path


def write_ndjson(tmpdir: str, name: str, receipts) -> str:
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as fh:
        for rec in receipts:
            fh.write(json.dumps(rec) + "\n")
    return path


def run_verifier(ndjson_path: str, cosign_bin: str, min_expected: int):
    cmd = [
        sys.executable, VERIFIER,
        "--ndjson", ndjson_path,
        "--surface", "selftest",
        "--cosign", cosign_bin,
        "--min-expected", str(min_expected),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    if not os.path.exists(VERIFIER):
        print(f"::error::verifier not found: {VERIFIER}")
        return 2
    if not os.path.exists(LEDGER):
        print(f"::error::genuine ledger not found: {LEDGER}")
        return 2

    genuine = load_genuine_receipt()

    cases = []  # (name, build(genuine) -> (receipts, min_expected), expect_nonzero)

    # CLEAN — the genuine receipt must still verify (exit 0).
    def clean(g):
        return [g], 1
    cases.append(("clean", clean, False))

    # TAMPERED SNAPSHOT — alter subject.snapshot but keep the original
    # subject.sha256; recompute receipt_id so the body-tamper check passes and
    # ONLY the snapshot-integrity reconstruction fails.
    def tampered_snapshot(g):
        rec = copy.deepcopy(g)
        snap = rec["subject"]["snapshot"]
        if isinstance(snap, dict):
            snap["__tamper__"] = "injected"
        elif isinstance(snap, list):
            snap.append("__tamper__")
        else:
            rec["subject"]["snapshot"] = {"__tamper__": snap}
        rec = recompute_receipt_id(rec)  # body now self-consistent
        return [rec], 1
    cases.append(("tampered_snapshot", tampered_snapshot, True))

    # FLIPPED RECEIPT_ID — change the stored receipt_id so the body-tamper
    # recompute no longer matches.
    def flipped_receipt_id(g):
        rec = copy.deepcopy(g)
        rid = rec.get("receipt_id") or ("0" * 64)
        flipped = ("1" if rid[0] != "1" else "0") + rid[1:]
        rec["receipt_id"] = flipped
        return [rec], 1
    cases.append(("flipped_receipt_id", flipped_receipt_id, True))

    # BROKEN PREV_HASH — a 2-row ledger whose second row is internally valid
    # (snapshot, bundle, DSSE, receipt_id all consistent) but whose prev_hash
    # does NOT link to row 0's receipt_id, so only chain continuity fails.
    def broken_prev_hash(g):
        row0 = copy.deepcopy(g)
        row1 = copy.deepcopy(g)
        row1["chain_index"] = 2
        row1["prev_hash"] = "0" * 64  # wrong: should equal row0 receipt_id
        row1 = recompute_receipt_id(row1)  # body self-consistent
        return [row0, row1], 1
    cases.append(("broken_prev_hash", broken_prev_hash, True))

    # BELOW BASELINE — a single genuine receipt against a floor of 2 rows
    # (simulates a truncated/wiped ledger that would otherwise pass green).
    def below_baseline(g):
        return [g], 2
    cases.append(("below_baseline", below_baseline, True))

    # VALID CONJECTURE KIND — relabel the genuine receipt as a
    # conjecture-disclosure-anchor and recompute receipt_id so the body stays
    # self-consistent. With cosign stubbed this must still PASS (exit 0): the
    # ledger legitimately carries this second receipt kind.
    def valid_conjecture_kind(g):
        rec = copy.deepcopy(g)
        rec["kind"] = "conjecture-disclosure-anchor"
        rec = recompute_receipt_id(rec)
        return [rec], 1
    cases.append(("valid_conjecture_kind", valid_conjecture_kind, False))

    # VALID LOCKED-BASELINE KIND — the Doctrine-v11 locked-proven baseline anchor
    # (chain #3, added with the 5->8 lock growth on 2026-06-10). Relabel the
    # genuine receipt and recompute receipt_id; with cosign stubbed this must
    # PASS (exit 0): the ledger legitimately carries this third receipt kind.
    def valid_locked_baseline_kind(g):
        rec = copy.deepcopy(g)
        rec["kind"] = "locked-baseline"
        rec = recompute_receipt_id(rec)
        return [rec], 1
    cases.append(("valid_locked_baseline_kind", valid_locked_baseline_kind, False))

    # UNKNOWN KIND — an out-of-allowlist kind must still be REJECTED (exit
    # non-zero), so the kind allowlist can never be silently widened to wave any
    # arbitrary receipt through.
    def unknown_kind(g):
        rec = copy.deepcopy(g)
        rec["kind"] = "bogus-not-an-anchor"
        rec = recompute_receipt_id(rec)
        return [rec], 1
    cases.append(("unknown_kind", unknown_kind, True))

    failures = []
    with tempfile.TemporaryDirectory() as tmpdir:
        cosign_bin = make_cosign_stub(tmpdir)
        for name, build, expect_nonzero in cases:
            receipts, min_expected = build(genuine)
            ndjson_path = write_ndjson(tmpdir, f"{name}.ndjson", receipts)
            code, out = run_verifier(ndjson_path, cosign_bin, min_expected)
            ok = (code != 0) if expect_nonzero else (code == 0)
            want = "non-zero" if expect_nonzero else "0"
            status = "PASS" if ok else "FAIL"
            print(f"[{status}] {name}: exit={code} (expected {want})")
            if not ok:
                tail = "\n".join(out.strip().splitlines()[-6:])
                print(f"        --- verifier output (tail) ---\n        "
                      + tail.replace("\n", "\n        "))
                failures.append(name)

    if failures:
        print(f"\n::error::verifier self-test FAILED for: {', '.join(failures)} "
              "— the receipt verifier has been weakened (a crafted breakage was "
              "not detected, or the clean fixture did not pass).")
        return 1
    print(f"\nAll {len(cases)} self-test fixtures behaved as expected — the "
          "receipt verifier still detects tampering, chain breaks, and "
          "below-baseline truncation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
