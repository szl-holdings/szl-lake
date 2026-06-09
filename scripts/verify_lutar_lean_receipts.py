#!/usr/bin/env python3
"""verify_lutar_lean_receipts.py — re-verify the anchored Theorem-U receipt chain.

A receipt was anchored into szl-lake (chain_index 1) and verified ONCE at
creation time (lutar-lean anchor-szl-lake.yml). That proves signing worked the
day it was minted. It gives no ongoing assurance that the anchor still verifies
against the public Sigstore trust root + Rekor transparency log, nor that the
ledger has not been silently truncated or re-ordered since. This re-checker
closes that gap: it can run on a schedule and fail LOUDLY if the anchor ever
stops verifying.

For every receipt line in a khipu NDJSON ledger it independently checks:

  1. Snapshot integrity — reconstruct the signed snapshot bytes from the
     embedded ``subject.snapshot`` and assert their sha256 equals
     ``subject.sha256``. The original signed artifact (a GitHub Actions upload)
     expires, so the receipt must be self-verifiable from its own embedded
     copy.
  2. Signature — re-run ``cosign verify-blob-attestation --new-bundle-format``
     against the embedded ``signing.bundle_b64``, pinned to the receipt's exact
     Fulcio identity (SAN), OIDC issuer, and predicate type. This re-checks the
     Fulcio cert chain + Rekor inclusion + DSSE signature against the public
     trust root — the same independent path the anchor used at mint time.
  3. Body tamper-evidence — recompute ``receipt_id`` as the canonical hash of
     the receipt with ``receipt_id`` removed (exactly how anchor_szl_lake.py
     builds it) and assert it equals the stored ``receipt_id``. Any edit to the
     receipt body breaks this even if the signed snapshot is untouched.

Across the whole ledger it asserts chain continuity:

  - ``chain_index`` starts at 1 and is strictly monotonic (+1 each row);
  - ``prev_hash`` of row 0 is null; ``prev_hash[i] == receipt_id[i-1]``;
  - the ledger is at least ``--min-expected`` rows long (append-only floor:
    catches a wiped / truncated ledger that would otherwise pass green with
    "nothing to check").

Exit 0 = every receipt verifies and the chain is continuous (or the ledger is
empty AND no baseline floor is set). Exit 1 = a receipt no longer verifies,
the chain drifted, or the ledger regressed below its baseline. Exit 2 =
usage / tooling error (e.g. cosign missing) — never a silent pass.

stdlib-only + the ``cosign`` binary (the same tool the anchor workflow uses).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile

SCHEMA = "szl.khipu.receipt/v1"
KIND = "theorem-u-anchor"
GH_OIDC_ISSUER = "https://token.actions.githubusercontent.com"


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonical_hash(obj) -> str:
    """Match anchor_szl_lake.py: sha256 of compact, sorted-key JSON."""
    return _sha256_bytes(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    )


def reconstruct_snapshot_bytes(snapshot, expected_sha: str):
    """Return the exact bytes whose sha256 == expected_sha, or None.

    The snapshot is embedded parsed; we re-serialize it and accept the first
    candidate that reproduces the signed digest. The anchor writes it as
    ``json.dumps(..., indent=2) + "\\n"`` (ensure_ascii default True), which is
    the primary candidate; the rest are tolerant fallbacks against future
    serializer changes. If NONE matches, the snapshot content was altered
    (tamper) — we return None and the caller fails the receipt.
    """
    candidates = [
        json.dumps(snapshot, indent=2) + "\n",
        json.dumps(snapshot, indent=2),
        json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
        json.dumps(snapshot, indent=2, ensure_ascii=False),
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        json.dumps(snapshot, separators=(",", ":")),
    ]
    for text in candidates:
        data = text.encode("utf-8")
        if _sha256_bytes(data) == expected_sha:
            return data
    return None


def cosign_verify(cosign_bin: str, bundle_path: str, snapshot_path: str,
                  predicate_type: str, identity: str, issuer: str):
    """Run cosign verify-blob-attestation. Returns (ok, detail)."""
    cmd = [
        cosign_bin, "verify-blob-attestation",
        "--new-bundle-format",
        "--bundle", bundle_path,
        "--type", predicate_type,
        "--certificate-identity", identity,
        "--certificate-oidc-issuer", issuer,
        snapshot_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return False, f"cosign binary not found: {cosign_bin}"
    except subprocess.TimeoutExpired:
        return False, "cosign verify timed out (120s)"
    out = (proc.stdout or "") + (proc.stderr or "")
    out = out.strip().replace("\n", " ")[:400]
    return proc.returncode == 0, out


def verify_receipt(rec: dict, idx: int, cosign_bin: str, tmpdir: str):
    """Verify a single receipt. Returns a result dict with status/reason."""
    res = {"index": idx, "chain_index": rec.get("chain_index"),
           "receipt_id": rec.get("receipt_id"), "status": "PASS", "reason": ""}

    def fail(reason):
        res["status"] = "FAIL"
        res["reason"] = reason
        return res

    if rec.get("schema") != SCHEMA:
        return fail(f"unexpected schema {rec.get('schema')!r} (want {SCHEMA})")
    if rec.get("kind") != KIND:
        return fail(f"unexpected kind {rec.get('kind')!r} (want {KIND})")

    # (3) body tamper-evidence — recompute receipt_id over the body.
    stored_id = rec.get("receipt_id")
    if not stored_id:
        return fail("receipt_id missing")
    body = {k: v for k, v in rec.items() if k != "receipt_id"}
    calc_id = canonical_hash(body)
    if calc_id != stored_id:
        return fail(f"receipt_id mismatch (body tampered): stored {stored_id[:16]}… "
                    f"!= recomputed {calc_id[:16]}…")

    subject = rec.get("subject") or {}
    snap = subject.get("snapshot")
    expected_sha = subject.get("sha256")
    if snap is None or not expected_sha:
        return fail("subject.snapshot or subject.sha256 missing")

    # (1) snapshot integrity — reconstruct the signed bytes.
    snap_bytes = reconstruct_snapshot_bytes(snap, expected_sha)
    if snap_bytes is None:
        return fail("could not reproduce subject.sha256 from embedded snapshot "
                    "(snapshot content altered)")

    signing = rec.get("signing") or {}
    bundle_b64 = signing.get("bundle_b64")
    predicate_type = signing.get("predicate_type")
    identity = signing.get("fulcio_identity")
    issuer = signing.get("oidc_issuer") or GH_OIDC_ISSUER
    if not bundle_b64 or not predicate_type or not identity:
        return fail("signing.bundle_b64 / predicate_type / fulcio_identity missing")
    try:
        bundle_bytes = base64.b64decode(bundle_b64)
    except Exception as e:  # noqa: BLE001
        return fail(f"signing.bundle_b64 not valid base64: {e}")

    # Cross-check the DSSE subject digest inside the bundle matches the receipt.
    try:
        bundle_obj = json.loads(bundle_bytes)
        dsse = bundle_obj.get("dsseEnvelope") or bundle_obj.get("dsse_envelope")
        if dsse:
            stmt = json.loads(base64.b64decode(dsse["payload"]))
            digs = {s.get("digest", {}).get("sha256")
                    for s in stmt.get("subject", [])}
            if expected_sha not in digs:
                return fail(f"DSSE subject digest {digs} != subject.sha256 "
                            f"{expected_sha}")
    except Exception as e:  # noqa: BLE001
        return fail(f"could not parse embedded sigstore bundle: {e}")

    snap_path = os.path.join(tmpdir, f"snapshot_{idx}.json")
    bundle_path = os.path.join(tmpdir, f"bundle_{idx}.json")
    with open(snap_path, "wb") as fh:
        fh.write(snap_bytes)
    with open(bundle_path, "wb") as fh:
        fh.write(bundle_bytes)

    # (2) signature — independent cosign re-verification.
    ok, detail = cosign_verify(cosign_bin, bundle_path, snap_path,
                               predicate_type, identity, issuer)
    if not ok:
        return fail(f"cosign verify-blob-attestation failed: {detail}")

    res["rekor_log_index"] = signing.get("rekor_log_index")
    return res


def check_chain(receipts):
    """Return a list of continuity error strings (empty == continuous)."""
    errors = []
    for i, rec in enumerate(receipts):
        expected_ci = i + 1
        ci = rec.get("chain_index")
        if ci != expected_ci:
            errors.append(f"row {i}: chain_index {ci!r} != expected {expected_ci}")
        prev = rec.get("prev_hash")
        if i == 0:
            if prev is not None:
                errors.append(f"row 0: prev_hash {prev!r} != null (genesis)")
        else:
            want = receipts[i - 1].get("receipt_id")
            if prev != want:
                errors.append(
                    f"row {i}: prev_hash {str(prev)[:16]}… != prior receipt_id "
                    f"{str(want)[:16]}… (chain broken)")
    return errors


def load_ndjson(path):
    receipts = []
    with open(path, "r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                receipts.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(
                    f"::error::{path}: line {n} is not valid JSON: {e}")
    return receipts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ndjson", required=True,
                    help="path to lutar_lean_receipts.ndjson")
    ap.add_argument("--surface", default="unknown",
                    help="label for this copy (e.g. github-front-door, hf-canonical)")
    ap.add_argument("--cosign", default=os.environ.get("COSIGN_BIN", "cosign"),
                    help="cosign binary (default: $COSIGN_BIN or 'cosign')")
    ap.add_argument("--min-expected", type=int, default=0,
                    help="append-only floor: fail if fewer than N receipts present")
    ap.add_argument("--summary-out", default="",
                    help="write a JSON summary to this path")
    args = ap.parse_args(argv)

    surface = args.surface
    print(f"== Re-verifying anchored receipt chain :: surface={surface} ==")
    print(f"   ledger: {args.ndjson}")

    if not os.path.exists(args.ndjson):
        print(f"::error::ledger not found: {args.ndjson}")
        return 2

    receipts = load_ndjson(args.ndjson)
    total = len(receipts)
    print(f"   receipts present: {total} (baseline floor: {args.min_expected})")

    results = []
    passed = failed = 0
    baseline_ok = total >= args.min_expected

    with tempfile.TemporaryDirectory() as tmpdir:
        for i, rec in enumerate(receipts):
            r = verify_receipt(rec, i, args.cosign, tmpdir)
            results.append(r)
            tag = "OK  " if r["status"] == "PASS" else "FAIL"
            print(f"   [{tag}] row {i} chain_index={r.get('chain_index')} "
                  f"receipt_id={str(r.get('receipt_id'))[:16]}…"
                  + (f"  -> {r['reason']}" if r["reason"] else ""))
            if r["status"] == "PASS":
                passed += 1
            else:
                failed += 1

    chain_errors = check_chain(receipts)
    for e in chain_errors:
        print(f"   [FAIL] chain continuity: {e}")
    if not baseline_ok:
        print(f"   [FAIL] baseline regression: {total} receipt(s) present, "
              f"require >= {args.min_expected} (ledger truncated/wiped?)")

    ok = (failed == 0 and not chain_errors and baseline_ok)
    summary = {
        "surface": surface,
        "ndjson": args.ndjson,
        "checked": total,
        "passed": passed,
        "failed": failed,
        "min_expected": args.min_expected,
        "baseline_ok": baseline_ok,
        "chain_continuous": not chain_errors,
        "chain_errors": chain_errors,
        "ok": ok,
        "results": results,
    }
    if args.summary_out:
        os.makedirs(os.path.dirname(args.summary_out) or ".", exist_ok=True)
        with open(args.summary_out, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
        print(f"   summary written: {args.summary_out}")

    print(f"== surface={surface}: checked={total} verified={passed} "
          f"failed={failed} chain_continuous={not chain_errors} "
          f"baseline_ok={baseline_ok} -> {'OK' if ok else 'FAILED'} ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
