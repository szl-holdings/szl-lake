"""
szl_lake_receipt.py — fold szl-lake anchor/ledger emission onto the canonical
szl-receipt PCGI spine.
Doctrine v11 LOCKED 749/14/163 | kernel c7c0ba17 | Khipu = Conjecture 2 (advisory)

szl-lake durably stores governance receipts (szl_lake_store.ReceiptLedger) and
anchors signed proof snapshots (scripts/verify_lutar_lean_receipts.py). Those
paths each carry their OWN provenance (the SHA3-256 Khipu chain, the cosign
attestation bundle). This module binds a lake object to the ONE org-canonical
receipt shape by DELEGATING to the shared ``szl_receipt`` library (v0.2.0) — it
does NOT re-implement receipt digests, canonicalization, DSSE signing, or the
in-toto statement. It is the same fold ``yarqa`` and ``governed-inference-meter``
already landed on the spine.

A canonical lake receipt binds, in ONE signed record:

  * ``subject``               — the lake object / anchor id (receipt_id / chain
                                head / anchor id),
  * ``input_digest``          — SHA-256 over the canonical INPUT that produced
                                the stored object (the ingested receipt / anchor
                                metadata),
  * ``stored_object_digest``  — SHA-256 over the object the lake durably stored
                                (the ledger envelope / the anchored snapshot),
  * ``policy``                — the governing policy id,
  * ``energy``                — honest ``UNAVAILABLE`` (szl-lake measures no
                                joules; it only stores + relabels receipts).

Honesty (doctrine v11)
----------------------
* ``energy`` is ALWAYS reported ``UNAVAILABLE`` here — szl-lake is a store, not a
  meter; a joule value is NEVER fabricated. The measured counterpart lives in
  ``governed-inference-meter`` / ``szl-energy-attest``.
* The receipt is EVIDENCE binding a lake object (subject + input +
  stored-object + policy + energy); it is NOT a proof the stored/anchored
  content is correct. The Khipu SHA3-256 hash-chain stays Conjecture 2 (advisory
  BFT), never a proven theorem.
* Keyless => UNSIGNED-honest (``signed=False``); a signature is never faked.
* The canonical body is deterministic (no timestamp / nonce): identical inputs
  serialize to byte-identical canonical JSON and the same digest.
* ``szl_receipt`` is an OPTIONAL dependency (pinned ``szl-receipt==0.2.0`` — see
  ``requirements-attest.txt``). Importing this module never requires it; only
  producing a canonical receipt does. When absent this raises
  :class:`LakeReceiptUnavailable` rather than fabricating a receipt — the
  stdlib-only ledger in :mod:`szl_lake_store` keeps working without it.

stdlib + optional szl-receipt. Apache-2.0 — SZL Holdings 2026.
"""
from __future__ import annotations

from typing import Any, Optional

PCGI_RECEIPT_SCHEMA = "szl.pcgi.receipt/szl-lake-anchor/v1"
CANONICAL_KIND = "szl-lake-anchor"
DEFAULT_POLICY_ID = "szl.pcgi.policy/szl-lake-append-only-honest/v1"
DEFAULT_ORGAN = "szl-lake"
ENERGY_UNAVAILABLE = "UNAVAILABLE"


class LakeReceiptUnavailable(RuntimeError):
    """Raised when the shared ``szl_receipt`` library is not importable.

    Callers MUST treat this as "no canonical receipt here", never as a reason to
    fabricate a receipt or duplicate the shared shapes locally.
    """


def _require_szl_receipt():
    """Lazily import the shared library; fail honestly if it is absent."""
    try:
        import szl_receipt  # type: ignore
        from szl_receipt import attest  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised only without the lib
        raise LakeReceiptUnavailable(
            "szl_receipt (v0.2.0) is not installed; install "
            "`szl-receipt==0.2.0` (see requirements-attest.txt) to fold "
            "szl-lake objects onto the canonical receipt spine. Refusing to "
            "duplicate the shared receipt shapes."
        ) from exc
    return szl_receipt, attest


def _digest(obj: Any) -> str:
    """SHA-256 hex over the shared canonical JSON of ``obj``.

    Uses ``szl_receipt.Receipt.digest`` (SHA-256 over the library's canonical
    JSON) so the digest is byte-for-byte the same primitive that binds every
    other SZL receipt — nothing is re-implemented here.
    """
    szl_receipt, _ = _require_szl_receipt()
    return szl_receipt.Receipt(kind="_digest", body=dict(obj or {})).digest()


def _energy_unavailable() -> dict[str, Any]:
    """Honest energy binding for the lake: always ``UNAVAILABLE`` (no meter)."""
    return {
        "status": ENERGY_UNAVAILABLE,
        "joules": None,
        "reason": (
            "szl-lake stores and anchors receipts; it measures no joules -> "
            "reported UNAVAILABLE, never fabricated."
        ),
    }


def build_lake_receipt_body(
    *,
    anchor_id: Any,
    input: Any = None,
    stored_object: Any = None,
    policy_id: str = DEFAULT_POLICY_ID,
    input_digest: Optional[str] = None,
    stored_object_digest: Optional[str] = None,
    organ: str = DEFAULT_ORGAN,
) -> dict[str, Any]:
    """Assemble the DETERMINISTIC canonical PCGI body for one lake object.

    Provide either the raw ``input`` / ``stored_object`` (they will be digested)
    or a precomputed ``input_digest`` / ``stored_object_digest`` (e.g. the
    anchor's already-computed ``subject.sha256``). The body carries no timestamp
    or nonce, so identical inputs serialize byte-identically.
    """
    idig = input_digest if input_digest is not None else _digest(input)
    odig = (
        stored_object_digest
        if stored_object_digest is not None
        else _digest(stored_object)
    )
    return {
        "schema": PCGI_RECEIPT_SCHEMA,
        "kind": CANONICAL_KIND,
        "organ": str(organ),
        "subject": str(anchor_id),
        "input_digest": "sha256:" + idig,
        "stored_object_digest": "sha256:" + odig,
        "policy": {"id": str(policy_id)},
        "energy": _energy_unavailable(),
        "honesty": {
            "asserts": "integrity/append-only provenance, NOT correctness",
            "receipt_is": (
                "evidence trail binding this lake object (subject+input+"
                "stored-object+policy+energy), NOT a proof the anchored content "
                "is correct"
            ),
            "chain": (
                "the Khipu SHA3-256 hash-chain is Conjecture 2 (advisory BFT), "
                "never a proven theorem"
            ),
        },
    }


def lake_receipt_body_digest(
    *,
    anchor_id: Any,
    input: Any = None,
    stored_object: Any = None,
    policy_id: str = DEFAULT_POLICY_ID,
    input_digest: Optional[str] = None,
    stored_object_digest: Optional[str] = None,
    organ: str = DEFAULT_ORGAN,
) -> str:
    """Independently (re-)derive the canonical receipt's content digest."""
    return _digest(
        build_lake_receipt_body(
            anchor_id=anchor_id,
            input=input,
            stored_object=stored_object,
            policy_id=policy_id,
            input_digest=input_digest,
            stored_object_digest=stored_object_digest,
            organ=organ,
        )
    )


def emit_lake_receipt(
    *,
    anchor_id: Any,
    input: Any = None,
    stored_object: Any = None,
    policy_id: str = DEFAULT_POLICY_ID,
    input_digest: Optional[str] = None,
    stored_object_digest: Optional[str] = None,
    private_key_pem: Optional[str | bytes] = None,
    organ: str = DEFAULT_ORGAN,
    keyid: str = "",
) -> dict[str, Any]:
    """Emit ONE canonical szl-receipt (DSSE envelope) for a lake object.

    Binds subject (lake object / anchor id) + input digest + stored-object
    digest + governing policy id + honest ``UNAVAILABLE`` energy into a shared
    :class:`szl_receipt.Receipt` and signs it via
    :func:`szl_receipt.sign_receipt` (DSSE/ECDSA-P256-SHA256, cosign-compatible).

    With a PEM ECDSA-P256 ``private_key_pem`` the envelope is signed; keyless it
    is UNSIGNED-honest (``signed=False``) — never a fabricated signature. It is
    an EVIDENCE trail for the lake object, not a proof its content is correct.
    """
    szl_receipt, _ = _require_szl_receipt()
    body = build_lake_receipt_body(
        anchor_id=anchor_id,
        input=input,
        stored_object=stored_object,
        policy_id=policy_id,
        input_digest=input_digest,
        stored_object_digest=stored_object_digest,
        organ=organ,
    )
    receipt = szl_receipt.Receipt(kind=CANONICAL_KIND, body=body)
    return szl_receipt.sign_receipt(
        receipt, private_key_pem, organ=organ, keyid=keyid
    )


def emit_ledger_envelope_receipt(
    envelope: dict[str, Any],
    *,
    policy_id: str = DEFAULT_POLICY_ID,
    private_key_pem: Optional[str | bytes] = None,
    keyid: str = "",
) -> dict[str, Any]:
    """Fold a stored :class:`szl_lake_store.ReceiptLedger` envelope onto the spine.

    Binds ``subject`` = the envelope's ``receipt_id`` (or chain head), the
    INPUT = the receipt that was ingested (``envelope["receipt"]``), and the
    STORED OBJECT = the durable envelope the ledger persisted. Energy is honest
    ``UNAVAILABLE``.
    """
    if not isinstance(envelope, dict):
        raise ValueError("envelope must be a JSON object")
    anchor_id = (
        envelope.get("receipt_id")
        or envelope.get("chain_hash")
        or envelope.get("id")
    )
    if not anchor_id:
        raise ValueError("envelope has no receipt_id / chain_hash / id to bind")
    ingested = envelope.get("receipt", {})
    return emit_lake_receipt(
        anchor_id=anchor_id,
        input=ingested,
        stored_object=envelope,
        policy_id=policy_id,
        organ=str(envelope.get("organ") or DEFAULT_ORGAN),
        private_key_pem=private_key_pem,
        keyid=keyid,
    )


def emit_anchor_receipt(
    anchor: dict[str, Any],
    *,
    policy_id: str = DEFAULT_POLICY_ID,
    private_key_pem: Optional[str | bytes] = None,
    keyid: str = "",
) -> dict[str, Any]:
    """Fold an anchored proof-snapshot receipt (khipu NDJSON) onto the spine.

    Binds ``subject`` = the anchor's ``receipt_id``, the INPUT = the anchor
    metadata (schema / kind / chain position), and the STORED OBJECT digest =
    the anchor's already-computed ``subject.sha256`` (the signed snapshot's own
    SHA-256). Energy is honest ``UNAVAILABLE``.
    """
    if not isinstance(anchor, dict):
        raise ValueError("anchor must be a JSON object")
    anchor_id = anchor.get("receipt_id") or anchor.get("id")
    if not anchor_id:
        raise ValueError("anchor has no receipt_id / id to bind")
    subject = anchor.get("subject") or {}
    stored_sha = subject.get("sha256")
    if not stored_sha:
        raise ValueError("anchor.subject.sha256 missing — nothing to bind")
    input_meta = {
        "schema": anchor.get("schema"),
        "kind": anchor.get("kind"),
        "chain_index": anchor.get("chain_index"),
        "prev_hash": anchor.get("prev_hash"),
        "subject_name": subject.get("name"),
    }
    return emit_lake_receipt(
        anchor_id=anchor_id,
        input=input_meta,
        stored_object_digest=stored_sha,
        policy_id=policy_id,
        organ=str(anchor.get("organ") or DEFAULT_ORGAN),
        private_key_pem=private_key_pem,
        keyid=keyid,
    )


def verify_lake_receipt(
    envelope: dict[str, Any],
    *,
    public_key_pem: Optional[str | bytes] = None,
    anchor_id: Any = None,
    input: Any = None,
    stored_object: Any = None,
    policy_id: str = DEFAULT_POLICY_ID,
    input_digest: Optional[str] = None,
    stored_object_digest: Optional[str] = None,
) -> tuple[bool, str]:
    """Verify a signed lake receipt (and optionally rebind it to its object).

    Delegates the cryptographic check to :func:`szl_receipt.verify_receipt`
    (keyless envelopes honestly return ``(False, "unsigned-honest")``). When any
    rebind argument is supplied, additionally confirms the signed body's
    ``input_digest`` / ``stored_object_digest`` re-derive from the object held
    independently — so any post-hoc edit flips a digest and fails the rebind.
    """
    szl_receipt, _ = _require_szl_receipt()
    ok, detail = szl_receipt.verify_receipt(envelope, public_key_pem)
    if not ok:
        return ok, detail

    rebind = any(
        x is not None
        for x in (input, stored_object, input_digest, stored_object_digest)
    )
    if not rebind:
        return True, "ok"

    import base64
    import json

    try:
        body = json.loads(base64.b64decode(envelope["payload"]).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"payload decode error: {exc}"

    if input is not None or input_digest is not None:
        exp_idig = (
            "sha256:" + input_digest
            if input_digest is not None
            else "sha256:" + _digest(input)
        )
        if body.get("input_digest") != exp_idig:
            return False, "input-digest-rebind-mismatch"

    if stored_object is not None or stored_object_digest is not None:
        exp_odig = (
            "sha256:" + stored_object_digest
            if stored_object_digest is not None
            else "sha256:" + _digest(stored_object)
        )
        if body.get("stored_object_digest") != exp_odig:
            return False, "stored-object-digest-rebind-mismatch"

    if anchor_id is not None and body.get("subject") != str(anchor_id):
        return False, "subject-rebind-mismatch"

    return True, "ok"
