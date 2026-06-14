#!/usr/bin/env python3
"""test_verify_lutar_lean_receipts_real.py — REAL cosign DSSE round-trip proof.

The offline self-test (scripts/test_verify_lutar_lean_receipts.py) protects the
verifier's integrity / chain-continuity / append-only-floor logic, but it
deliberately STUBS cosign (a `--cosign` script that always exits 0) so it can run
without the network. That leaves the ACTUAL signature path unprotected: a silent
weakening of the verify call — dropping `--certificate-identity` /
`--certificate-oidc-issuer`, or accepting an envelope whose DSSE signature no
longer matches — would slip past the offline self-test and only (maybe) trip the
network-dependent scheduled run.

This test closes that gap. It runs `verify_lutar_lean_receipts.py` with the REAL
cosign binary against the public Sigstore trust root + Rekor — no stub, no
monkeypatch — and proves rejection is SPECIFIC:

  * GENUINE control          -> the untouched anchored receipt VERIFIES (exit 0),
                                so a broken trust root or an over-eager guard
                                cannot make the negative cases pass for the wrong
                                reason.
  * signature byte-flip      -> flip one byte of the DSSE signature (payload and
                                subject digest untouched, receipt_id recomputed so
                                the body stays self-consistent). The real DSSE
                                verify REJECTS it -> exit 1. This is the "forged
                                signature" path.
  * wrong fulcio identity    -> present the genuine, cryptographically-valid
                                receipt with a SAN expectation it does not satisfy.
                                cosign's `--certificate-identity` pin REJECTS it.
                                If someone dropped that flag the receipt would
                                wrongly pass and this test goes red. ("weakened
                                identity check" path.)
  * wrong oidc issuer        -> same idea for `--certificate-oidc-issuer`.

mirrors a11oy tests/test_verify_release_receipts_real.py: CI-gated by an explicit
env flag, skipped off-CI. a11oy uses `pytest.importorskip("sigstore")` because its
verifier imports the Sigstore SDK; this verifier shells out to the cosign BINARY,
so the honest binary-analog of importorskip is a module-level skip when cosign is
not on PATH / COSIGN_BIN. A local/default environment never runs this (no false
failures, no accidental network).
"""
from __future__ import annotations

import base64
import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
VERIFIER = HERE / "verify_lutar_lean_receipts.py"
LEDGER = REPO / "data" / "khipu" / "lutar_lean_receipts.ndjson"

# Binary analog of a11oy's `pytest.importorskip("sigstore")`: the real verify is
# impossible without the cosign binary, and a missing tool is never a test
# failure. Resolve it the same way the verifier does ($COSIGN_BIN, else PATH).
COSIGN_BIN = os.environ.get("COSIGN_BIN") or shutil.which("cosign")
if not COSIGN_BIN:
    pytest.skip(
        "cosign binary not found ($COSIGN_BIN / PATH); the REAL lutar receipt "
        "verify cannot run without it",
        allow_module_level=True,
    )

# Even with cosign present, only run where the real verify is explicitly enabled
# (the dedicated CI job sets this). It needs the public Sigstore trust root +
# Rekor over the network; a machine that happens to have cosign must opt in.
pytestmark = pytest.mark.skipif(
    os.environ.get("SZL_LUTAR_RECEIPT_REAL_VERIFY") != "1",
    reason=(
        "real lutar receipt verify needs the public Sigstore trust root + Rekor "
        "over the network; set SZL_LUTAR_RECEIPT_REAL_VERIFY=1 (the dedicated CI "
        "job does)"
    ),
)


def _canonical_hash(obj) -> str:
    """Match the verifier/anchor: sha256 of compact, sorted-key JSON."""
    import hashlib

    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _recompute_receipt_id(rec: dict) -> dict:
    """Return a copy whose receipt_id is the canonical hash of its body, so the
    body-tamper check passes and ONLY the field under test reaches cosign."""
    rec = copy.deepcopy(rec)
    body = {k: v for k, v in rec.items() if k != "receipt_id"}
    rec["receipt_id"] = _canonical_hash(body)
    return rec


def _load_genuine_receipt() -> dict:
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            return json.loads(line)
    raise SystemExit(f"::error::no receipt found in {LEDGER}")


def _dsse_envelope(bundle_obj: dict) -> dict:
    dsse = bundle_obj.get("dsseEnvelope") or bundle_obj.get("dsse_envelope")
    assert dsse, "genuine bundle has no DSSE envelope"
    return dsse


def _flip_signature_byte(rec: dict) -> dict:
    """Flip one byte of the DSSE signature inside signing.bundle_b64, leaving the
    payload (and thus subject digest) intact, then recompute receipt_id so the
    body stays self-consistent. Result: every pre-cosign check passes and the only
    thing that can fail is the cryptographic signature verify itself."""
    rec = copy.deepcopy(rec)
    bundle = json.loads(base64.b64decode(rec["signing"]["bundle_b64"]))
    dsse = _dsse_envelope(bundle)
    sigs = dsse["signatures"]
    assert sigs, "genuine DSSE envelope has no signatures"
    raw = bytearray(base64.b64decode(sigs[0]["sig"]))
    raw[0] ^= 0x01  # flip one bit of the first signature byte
    sigs[0]["sig"] = base64.b64encode(bytes(raw)).decode("ascii")
    rec["signing"]["bundle_b64"] = base64.b64encode(
        json.dumps(bundle).encode("utf-8")
    ).decode("ascii")
    return _recompute_receipt_id(rec)


def _wrong_fulcio_identity(rec: dict) -> dict:
    rec = copy.deepcopy(rec)
    rec["signing"]["fulcio_identity"] = (
        "https://github.com/szl-holdings/not-lutar-lean/.github/workflows/"
        "anchor-szl-lake.yml@refs/heads/main"
    )
    return _recompute_receipt_id(rec)


def _wrong_oidc_issuer(rec: dict) -> dict:
    rec = copy.deepcopy(rec)
    rec["signing"]["oidc_issuer"] = "https://accounts.google.com"
    return _recompute_receipt_id(rec)


def _write_ndjson(tmp_path: Path, name: str, receipts) -> Path:
    p = tmp_path / name
    with p.open("w", encoding="utf-8") as fh:
        for rec in receipts:
            fh.write(json.dumps(rec) + "\n")
    return p


def _run_verifier(ndjson_path: Path, summary_path: Path) -> tuple[int, dict]:
    cmd = [
        sys.executable,
        str(VERIFIER),
        "--ndjson",
        str(ndjson_path),
        "--surface",
        "real-selftest",
        "--cosign",
        COSIGN_BIN,
        "--min-expected",
        "1",
        "--summary-out",
        str(summary_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    return proc.returncode, summary


@pytest.fixture(scope="module")
def genuine() -> dict:
    assert VERIFIER.exists(), f"missing verifier: {VERIFIER}"
    assert LEDGER.exists(), f"missing genuine ledger: {LEDGER}"
    return _load_genuine_receipt()


def test_genuine_fixture_is_a_real_signed_receipt(genuine):
    """Guard the fixture: it must be a genuine Sigstore keyless receipt with an
    embedded DSSE bundle, or the negative proofs below would be meaningless."""
    signing = genuine.get("signing") or {}
    assert signing.get("bundle_b64"), "genuine receipt has no signing.bundle_b64"
    bundle = json.loads(base64.b64decode(signing["bundle_b64"]))
    dsse = _dsse_envelope(bundle)
    assert dsse["signatures"], "genuine receipt DSSE envelope has no signatures"
    assert signing.get("fulcio_identity"), "genuine receipt has no fulcio identity"


def test_genuine_receipt_verifies(tmp_path, genuine):
    """Symmetric control: the untouched anchored receipt PASSES the real cosign
    verify (exit 0). Without this, a broken trust root could make the negative
    cases below 'pass' for the wrong reason."""
    nd = _write_ndjson(tmp_path, "genuine.ndjson", [genuine])
    rc, summary = _run_verifier(nd, tmp_path / "genuine.summary.json")
    assert rc == 0, "the genuine anchored receipt must verify (exit 0)"
    assert summary["checked"] == 1
    assert summary["passed"] == 1
    assert summary["failed"] == 0
    assert summary["results"][0]["status"] == "PASS"


def test_flipped_signature_is_rejected(tmp_path, genuine):
    """A byte-flipped DSSE signature is REJECTED by the real cosign verify. Every
    pre-cosign check still passes (snapshot, receipt_id, DSSE subject digest), so
    the failure can ONLY come from the cryptographic signature path."""
    nd = _write_ndjson(tmp_path, "flip.ndjson", [_flip_signature_byte(genuine)])
    rc, summary = _run_verifier(nd, tmp_path / "flip.summary.json")
    assert rc == 1, "a forged/flipped signature MUST fail the run (exit 1)"
    assert summary["failed"] == 1
    entry = summary["results"][0]
    assert entry["status"] == "FAIL"
    # The failure is the cosign signature step, not a structural pre-check.
    assert "cosign verify-blob-attestation failed" in entry["reason"], entry["reason"]


def test_wrong_fulcio_identity_is_rejected(tmp_path, genuine):
    """A genuine, cryptographically-valid receipt presented with a SAN it does not
    satisfy is REJECTED via cosign's `--certificate-identity` pin. If that flag
    were ever dropped (a 'weakened' check) the receipt would wrongly pass and this
    test would go red — distinct from the byte-flip crypto path."""
    nd = _write_ndjson(tmp_path, "wid.ndjson", [_wrong_fulcio_identity(genuine)])
    rc, summary = _run_verifier(nd, tmp_path / "wid.summary.json")
    assert rc == 1, "an unexpected signer identity MUST fail the run (exit 1)"
    entry = summary["results"][0]
    assert entry["status"] == "FAIL"
    assert "cosign verify-blob-attestation failed" in entry["reason"], entry["reason"]


def test_wrong_oidc_issuer_is_rejected(tmp_path, genuine):
    """Same as above for `--certificate-oidc-issuer`: a wrong OIDC issuer
    expectation is rejected by cosign, proving the issuer pin is enforced."""
    nd = _write_ndjson(tmp_path, "wiss.ndjson", [_wrong_oidc_issuer(genuine)])
    rc, summary = _run_verifier(nd, tmp_path / "wiss.summary.json")
    assert rc == 1, "a wrong OIDC issuer MUST fail the run (exit 1)"
    entry = summary["results"][0]
    assert entry["status"] == "FAIL"
    assert "cosign verify-blob-attestation failed" in entry["reason"], entry["reason"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
