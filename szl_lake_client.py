"""Best-effort, synchronous client for the Unified Receipt Ledger.
Doctrine v11 LOCKED 749/14/163 | kernel c7c0ba17 | Λ = Conjecture 1 (OPEN)

Set SZL_RECEIPT_SINK to the existing ledger origin. A success means that the
sink acknowledged the exact serialized receipts; it does not independently
verify signatures, chain history, disk durability, or production readiness.
No queue or automatic retry is created. Callers must inspect the returned
status: missing configuration is NOT_CONFIGURED, invalid local input is
NOT_SENT, and a failed/ambiguous HTTP attempt leaves delivery UNKNOWN. A timeout
or failed batch can follow a partial write, so never report those as zero writes.

The client is standalone standard-library Python. Apache-2.0.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request

RECEIPTS_PATH = "/api/lake/v1/receipts"
DEFAULT_TIMEOUT = 1.5
MAX_ACK_BYTES = 1024 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect is not authority to send a receipt to another destination.
        return None


def _endpoint(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("receipt sink must not contain credentials, query or fragment")
    return base_url.rstrip("/") + RECEIPTS_PATH


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON value")
    return value


def _reject_constant(_text):
    raise ValueError("non-finite JSON value")


def _strict_json(raw):
    return json.loads(raw, object_pairs_hook=_unique_object,
                      parse_constant=_reject_constant, parse_float=_finite_float)


def _body_hash(receipt):
    # Same canonical wire-object digest as ReceiptLedger, without importing a
    # store or opening a local ledger in a standalone client.
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False).encode("utf-8")
    return hashlib.sha3_256(canonical).hexdigest()


def _acknowledges(row, receipt):
    if not isinstance(row, dict) or "error" in row or row.get("ok") is False:
        return False
    accepted, duplicate = row.get("accepted"), row.get("duplicate")
    if type(accepted) is not bool or type(duplicate) is not bool or accepted == duplicate:
        return False
    body_hash = _body_hash(receipt)
    identity = next((receipt[key] for key in ("id", "hash", "receipt_id")
                     if isinstance(receipt.get(key), str) and receipt[key]), body_hash)
    organ = receipt.get("organ", "unknown")
    if not isinstance(organ, str) or row.get("organ") != organ.strip().lower():
        return False
    if row.get("receipt_id") != identity or row.get("receipt_body_hash") != body_hash:
        return False
    for key in ("chain_index", "ledger_offset"):
        if type(row.get(key)) is not int or row[key] < 1:
            return False
    head = row.get("chain_head")
    return isinstance(head, str) and _HEX64.fullmatch(head) is not None


def _valid_ack(payload, receipts):
    if not isinstance(payload, dict) or "error" in payload or payload.get("ok") is False:
        return False
    # The native server returns the single-receipt envelope even for one NDJSON row.
    if len(receipts) == 1 and "results" not in payload:
        return _acknowledges(payload, receipts[0])
    for key in ("accepted", "duplicates", "total"):
        if type(payload.get(key)) is not int or payload[key] < 0:
            return False
    rows = payload.get("results")
    if (payload["total"] != len(receipts) or not isinstance(rows, list)
            or len(rows) != len(receipts)
            or payload["accepted"] + payload["duplicates"] != len(receipts)):
        return False
    if not all(_acknowledges(row, rec) for row, rec in zip(rows, receipts)):
        return False
    return (payload["accepted"] == sum(row["accepted"] for row in rows)
            and payload["duplicates"] == sum(row["duplicate"] for row in rows))


def _failure(reason, state, status=None):
    result = {"ok": False, "reason": reason, "delivery_state": state}
    if status is not None:
        result["status"] = status
    return result


def _emit(value, *, batch, base_url, timeout):
    try:
        base = base_url or os.environ.get("SZL_RECEIPT_SINK")
        if not base:
            return _failure("SZL_RECEIPT_SINK unset", "NOT_CONFIGURED")
        try:
            if batch:
                if not isinstance(value, list):
                    raise ValueError("batch must be a list of receipt objects")
                body = "\n".join(json.dumps(row, allow_nan=False) for row in value).encode("utf-8")
                receipts = [_strict_json(line) for line in body.splitlines()]
            else:
                body = json.dumps(value, allow_nan=False).encode("utf-8")
                receipts = [_strict_json(body)]
        except (TypeError, ValueError, RecursionError) as exc:
            return _failure(f"receipt not JSON-serializable: {exc}", "NOT_SENT")
        req = urllib.request.Request(_endpoint(base), data=body, method="POST",
                                     headers={"Content-Type": "application/x-ndjson" if batch else "application/json",
                                              "Accept": "application/json", "User-Agent": "szl-lake-client/1.1"})
        if req.type not in ("http", "https"):
            return _failure("receipt sink must use HTTP or HTTPS", "NOT_SENT")
        if not receipts or not all(isinstance(row, dict) for row in receipts):
            return _failure("receipt body must contain one or more JSON objects", "NOT_SENT")
        limit = os.environ.get("SZL_RECEIPT_TIMEOUT", str(DEFAULT_TIMEOUT)) if timeout is None else timeout
        if isinstance(limit, bool):
            return _failure("receipt timeout must be finite and positive", "NOT_SENT")
        limit = float(limit)
        if not math.isfinite(limit) or limit <= 0:
            return _failure("receipt timeout must be finite and positive", "NOT_SENT")
        # Compute all expectations before transport; mutable caller data cannot
        # change the identity being acknowledged after the bytes were sent.
        for receipt in receipts:
            _body_hash(receipt)
    except Exception as exc:  # Local failures never escape to the caller.
        return _failure(f"unexpected: {exc}", "NOT_SENT")

    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(req, timeout=limit) as response:
            status = response.status
            if status != 200:
                return _failure("unexpected receipt acknowledgment status", "UNKNOWN", status)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return _failure("receipt acknowledgment is not JSON", "UNKNOWN", status)
            raw = response.read(MAX_ACK_BYTES + 1)
            if len(raw) > MAX_ACK_BYTES:
                return _failure("receipt acknowledgment exceeds byte bound", "UNKNOWN", status)
            try:
                payload = _strict_json(raw.decode("utf-8"))
                valid = _valid_ack(payload, receipts)
            except (ValueError, TypeError, RecursionError):
                valid = False
            if not valid:
                return _failure("receipt acknowledgment does not bind the submitted receipts", "UNKNOWN", status)
            return {"ok": True, "status": status, "response": payload,
                    "delivery_state": "SINK_ACKNOWLEDGED", "signature_verified": False,
                    "durability_verified": False}
    except urllib.error.HTTPError as exc:
        return _failure(f"http {exc.code}", "UNKNOWN", exc.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _failure(f"unreachable: {type(exc).__name__}", "UNKNOWN")
    except Exception as exc:
        return _failure(f"unexpected: {type(exc).__name__}", "UNKNOWN")


def emit_receipt(receipt: dict, base_url: str | None = None,
                 timeout: float | None = None) -> dict:
    """Submit one receipt; never raise. Inspect ok and delivery_state."""
    return _emit(receipt, batch=False, base_url=base_url, timeout=timeout)


def emit_batch(receipts: list[dict], base_url: str | None = None,
               timeout: float | None = None) -> dict:
    """Submit NDJSON once; ambiguous/partial delivery never becomes success."""
    return _emit(receipts, batch=True, base_url=base_url, timeout=timeout)
