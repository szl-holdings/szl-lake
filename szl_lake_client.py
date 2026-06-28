"""
szl_lake_client.py — fire-and-forget receipt emitter for the Unified Ledger
Doctrine v11 LOCKED 749/14/163 | kernel c7c0ba17 | Λ = Conjecture 1 (OPEN)

This is what ouroboros / hatun-mcp / szl-router / szl-mesh / vsp-otel / szl-trust
call to POST a governance receipt to the one szl-lake ledger. It is deliberately
NON-BLOCKING and NEVER raises: a receipt sink being slow or down must never take
down the caller's governed action. Worst case the emit is dropped and reported
via the returned status — callers stay up.

Wire contract (one line per downstream repo):
    export SZL_RECEIPT_SINK=https://<deployed-lake-host>
    from szl_lake_client import emit_receipt
    emit_receipt(my_receipt)         # returns immediately, never raises

If SZL_RECEIPT_SINK is unset, emit_receipt is a no-op (returns
{"ok": False, "reason": "SZL_RECEIPT_SINK unset"}) — so importing this in a
component with no sink configured is safe and silent.

stdlib-only (urllib) — no dependency imposed on callers. Apache-2.0.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

RECEIPTS_PATH = "/api/lake/v1/receipts"
DEFAULT_TIMEOUT = float(os.environ.get("SZL_RECEIPT_TIMEOUT", "1.5"))


def _endpoint(base_url: str) -> str:
    return base_url.rstrip("/") + RECEIPTS_PATH


def emit_receipt(receipt: dict,
                 base_url: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Fire-and-forget POST of a single receipt to the Unified Ledger.

    Never raises. Returns a small status dict for callers that want it; callers
    that don't care can ignore the return entirely.

    base_url defaults to $SZL_RECEIPT_SINK. When neither is set this is a no-op.
    """
    base = base_url or os.environ.get("SZL_RECEIPT_SINK")
    if not base:
        return {"ok": False, "reason": "SZL_RECEIPT_SINK unset"}

    try:
        body = json.dumps(receipt).encode("utf-8")
    except (TypeError, ValueError) as e:
        return {"ok": False, "reason": f"receipt not JSON-serializable: {e}"}

    req = urllib.request.Request(
        _endpoint(base),
        data=body,
        method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "szl-lake-client/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"raw": raw}
            return {"ok": True, "status": resp.status, "response": payload}
    except urllib.error.HTTPError as e:
        return {"ok": False, "reason": f"http {e.code}", "status": e.code}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "reason": f"unreachable: {e}"}
    except Exception as e:  # noqa: BLE001 — never raise to the caller
        return {"ok": False, "reason": f"unexpected: {e}"}


def emit_batch(receipts: list[dict],
               base_url: str | None = None,
               timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Fire-and-forget POST of an NDJSON batch. Never raises."""
    base = base_url or os.environ.get("SZL_RECEIPT_SINK")
    if not base:
        return {"ok": False, "reason": "SZL_RECEIPT_SINK unset"}

    try:
        body = "\n".join(json.dumps(r) for r in receipts).encode("utf-8")
    except (TypeError, ValueError) as e:
        return {"ok": False, "reason": f"receipt not JSON-serializable: {e}"}

    req = urllib.request.Request(
        _endpoint(base),
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-ndjson",
                 "User-Agent": "szl-lake-client/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"raw": raw}
            return {"ok": True, "status": resp.status, "response": payload}
    except urllib.error.HTTPError as e:
        return {"ok": False, "reason": f"http {e.code}", "status": e.code}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "reason": f"unreachable: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"unexpected: {e}"}
