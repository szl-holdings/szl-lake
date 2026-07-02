#!/usr/bin/env python3
"""test_lake_receipt_spine.py — the honest PCGI-spine fold for szl-lake.

szl_lake_receipt folds a stored lake object (a ReceiptLedger envelope) or an
anchored proof snapshot onto the ONE org-canonical receipt shape by delegating
to the shared ``szl_receipt`` library. These tests assert the binding is honest:

  * subject = the lake object / anchor id;
  * input_digest re-derives from the ingested input; a tampered input flips it;
  * stored_object_digest re-derives from the durable object; a tampered stored
    object flips it;
  * the governing policy id is carried;
  * energy is ALWAYS the honest literal UNAVAILABLE (joules never fabricated);
  * the canonical body is deterministic (identical inputs -> identical digest);
  * a signed receipt verifies with its key and rejects a tampered rebind;
  * keyless emission is UNSIGNED-honest (no fabricated signature);
  * the real ReceiptLedger append envelope folds cleanly.

Skips (never fails) if the optional ``szl_receipt`` library is absent — the
stdlib-only ledger stands on its own.

Run:
    pytest scripts/test_lake_receipt_spine.py -q
or directly:
    python3 scripts/test_lake_receipt_spine.py
"""
from __future__ import annotations

import base64
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

szl_receipt = pytest.importorskip(
    "szl_receipt", reason="optional szl-receipt spine library not installed"
)

import szl_lake_receipt as slr  # noqa: E402
from szl_lake_store import ReceiptLedger  # noqa: E402


def _body(envelope) -> dict:
    return json.loads(base64.b64decode(envelope["payload"]).decode("utf-8"))


LEDGER_ENVELOPE = {
    "receipt_id": "r-abc123",
    "organ": "a11oy",
    "ts": "2026-06-28T10:00:00Z",
    "chain_index": 1,
    "prev_hash": None,
    "chain_hash": "f" * 64,
    "energy": {"label": "UNAVAILABLE"},
    "receipt": {"id": "r-abc123", "organ": "a11oy", "decision": "ADMIT"},
}

ANCHOR = {
    "schema": "szl.khipu.receipt/v1",
    "kind": "theorem-u-anchor",
    "receipt_id": "anchor-777",
    "chain_index": 1,
    "prev_hash": None,
    "subject": {"name": "theorem-u-snapshot", "sha256": "a" * 64},
}


# ---------------------------------------------------------------- binding ----

def test_ledger_envelope_binding_is_honest():
    env = slr.emit_ledger_envelope_receipt(LEDGER_ENVELOPE)
    body = _body(env)
    assert body["schema"] == slr.PCGI_RECEIPT_SCHEMA
    assert body["kind"] == slr.CANONICAL_KIND
    # subject == the lake object id
    assert body["subject"] == "r-abc123"
    # input digest re-derives from the ingested receipt
    exp_in = "sha256:" + slr._digest(LEDGER_ENVELOPE["receipt"])
    assert body["input_digest"] == exp_in
    # stored-object digest re-derives from the durable envelope
    exp_out = "sha256:" + slr._digest(LEDGER_ENVELOPE)
    assert body["stored_object_digest"] == exp_out
    # governing policy id carried
    assert body["policy"]["id"] == slr.DEFAULT_POLICY_ID


def test_energy_is_always_unavailable_never_fabricated():
    body = _body(slr.emit_ledger_envelope_receipt(LEDGER_ENVELOPE))
    assert body["energy"]["status"] == "UNAVAILABLE"
    assert body["energy"]["joules"] is None
    # honesty: no numeric joule value anywhere in the energy binding
    assert "UNAVAILABLE" in slr.ENERGY_UNAVAILABLE


def test_body_is_deterministic():
    a = slr.build_lake_receipt_body(
        anchor_id="r-abc123",
        input=LEDGER_ENVELOPE["receipt"],
        stored_object=LEDGER_ENVELOPE,
    )
    b = slr.build_lake_receipt_body(
        anchor_id="r-abc123",
        input=LEDGER_ENVELOPE["receipt"],
        stored_object=LEDGER_ENVELOPE,
    )
    assert a == b
    assert slr._digest(a) == slr._digest(b)


def test_custom_policy_id_is_carried():
    body = _body(
        slr.emit_ledger_envelope_receipt(
            LEDGER_ENVELOPE, policy_id="szl.pcgi.policy/custom/v9"
        )
    )
    assert body["policy"]["id"] == "szl.pcgi.policy/custom/v9"


# ---------------------------------------------------------------- anchor -----

def test_anchor_binding_uses_precomputed_snapshot_digest():
    body = _body(slr.emit_anchor_receipt(ANCHOR))
    assert body["subject"] == "anchor-777"
    # stored-object digest == the anchor's own signed-snapshot sha256
    assert body["stored_object_digest"] == "sha256:" + "a" * 64
    assert body["energy"]["status"] == "UNAVAILABLE"


def test_anchor_without_sha256_is_rejected():
    bad = dict(ANCHOR)
    bad["subject"] = {"name": "x"}  # no sha256
    with pytest.raises(ValueError):
        slr.emit_anchor_receipt(bad)


# -------------------------------------------------------------- signing ------

def test_signed_receipt_verifies_and_rebinds():
    priv, pub = szl_receipt.generate_keypair()
    env = slr.emit_ledger_envelope_receipt(
        LEDGER_ENVELOPE, private_key_pem=priv, keyid="lake-p256"
    )
    assert env["signed"] is True
    ok, detail = slr.verify_lake_receipt(env, public_key_pem=pub)
    assert ok, detail
    # rebind against the true object passes
    ok2, d2 = slr.verify_lake_receipt(
        env,
        public_key_pem=pub,
        anchor_id="r-abc123",
        input=LEDGER_ENVELOPE["receipt"],
        stored_object=LEDGER_ENVELOPE,
    )
    assert ok2, d2


def test_tampered_stored_object_fails_rebind():
    priv, pub = szl_receipt.generate_keypair()
    env = slr.emit_ledger_envelope_receipt(
        LEDGER_ENVELOPE, private_key_pem=priv
    )
    tampered = dict(LEDGER_ENVELOPE)
    tampered["chain_hash"] = "0" * 64  # silently edited after the fact
    ok, detail = slr.verify_lake_receipt(
        env, public_key_pem=pub, stored_object=tampered
    )
    assert ok is False
    assert "stored-object-digest-rebind-mismatch" in detail


def test_tampered_input_fails_rebind():
    priv, pub = szl_receipt.generate_keypair()
    env = slr.emit_ledger_envelope_receipt(
        LEDGER_ENVELOPE, private_key_pem=priv
    )
    bad_input = {"id": "r-abc123", "organ": "a11oy", "decision": "REJECT"}
    ok, detail = slr.verify_lake_receipt(
        env, public_key_pem=pub, input=bad_input
    )
    assert ok is False
    assert "input-digest-rebind-mismatch" in detail


def test_keyless_is_unsigned_honest_never_faked():
    env = slr.emit_ledger_envelope_receipt(LEDGER_ENVELOPE)
    assert env["signed"] is False
    ok, detail = slr.verify_lake_receipt(env)
    assert ok is False
    assert detail == "unsigned-honest"


# -------------------------------------------------- real ledger integration --

def test_folds_a_real_ledger_append_envelope(tmp_path):
    led = ReceiptLedger(root=str(tmp_path / "khipu"))
    led.append({
        "id": "live-1",
        "ts": "2026-06-28T10:00:00Z",
        "organ": "a11oy",
        "decision": "ADMIT",
    })
    stored = led.query(organ="a11oy")[0]
    priv, pub = szl_receipt.generate_keypair()
    env = slr.emit_ledger_envelope_receipt(stored, private_key_pem=priv)
    ok, detail = slr.verify_lake_receipt(
        env, public_key_pem=pub, stored_object=stored
    )
    assert ok, detail
    body = _body(env)
    assert body["subject"] == stored["receipt_id"]
    assert body["energy"]["status"] == "UNAVAILABLE"


if __name__ == "__main__":
    sys.exit(pytest.main([os.path.abspath(__file__), "-q"]))
