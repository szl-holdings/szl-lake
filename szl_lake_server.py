"""
szl_lake_server.py — Unified Receipt Ledger HTTP service
Doctrine v11 LOCKED 749/14/163 | kernel c7c0ba17 | Λ = Conjecture 1 (OPEN)

The one endpoint every SZL component POSTs governance receipts to. Backed by the
durable, hash-chained, append-only store in szl_lake_store.ReceiptLedger.

Endpoints (all under /api/lake/v1):
  POST /receipts        ingest one receipt (JSON object) OR an NDJSON batch
                        (Content-Type: application/x-ndjson, or a JSON array)
  GET  /receipts        query the store: ?organ=&since=&limit=
  GET  /chain/head      per-organ Khipu chain head + count: ?organ=
  GET  /health          store reachable, total + per-organ counts

Run locally:
  uvicorn szl_lake_server:app --host 0.0.0.0 --port 8088
Store location is $SZL_LAKE_DIR (default ./khipu).

stdlib + FastAPI (the framework already vendored for this repo's tooling).
Apache-2.0 — SZL Holdings 2026.
"""
from __future__ import annotations

import json

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from szl_lake_store import ReceiptLedger, get_default_ledger

API_PREFIX = "/api/lake/v1"

app = FastAPI(
    title="SZL Unified Receipt Ledger",
    version="1.0.0",
    description=(
        "Durable, hash-chained (SHA3-256 Khipu, F4/F22 — Conjecture 2, "
        "advisory BFT, NOT a proven theorem) receipt ledger for the SZL "
        "substrate. Honest energy labels only."
    ),
)


def _ledger(request: Request) -> ReceiptLedger:
    """Allow tests to inject a ledger via app.state.ledger; else use default."""
    led = getattr(request.app.state, "ledger", None)
    return led if led is not None else get_default_ledger()


def _parse_body(raw: bytes, content_type: str) -> list[dict]:
    """Parse a request body into a list of receipt dicts.

    Supports: a single JSON object, a JSON array of objects, or NDJSON (one
    JSON object per line — signalled by content-type or detected heuristically).
    """
    text = raw.decode("utf-8").strip()
    if not text:
        raise ValueError("empty request body")

    ct = (content_type or "").lower()
    is_ndjson = "ndjson" in ct or "application/x-ndjson" in ct

    if not is_ndjson:
        # Try strict JSON first (object or array).
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            is_ndjson = True  # fall through to line parsing
        else:
            if isinstance(parsed, dict):
                return [parsed]
            if isinstance(parsed, list):
                if not all(isinstance(x, dict) for x in parsed):
                    raise ValueError("JSON array must contain only objects")
                return parsed
            raise ValueError("body must be a JSON object, array, or NDJSON")

    receipts: list[dict] = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"NDJSON line {n} is not valid JSON: {e}")
        if not isinstance(obj, dict):
            raise ValueError(f"NDJSON line {n} is not a JSON object")
        receipts.append(obj)
    if not receipts:
        raise ValueError("no receipts found in body")
    return receipts


@app.post(API_PREFIX + "/receipts")
async def post_receipts(request: Request):
    led = _ledger(request)
    raw = await request.body()
    try:
        receipts = _parse_body(raw, request.headers.get("content-type", ""))
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    try:
        if len(receipts) == 1:
            result = led.append(receipts[0])
            status = 200 if result["accepted"] or result["duplicate"] else 400
            return JSONResponse(status_code=status, content=result)
        batch = led.append_batch(receipts)
        return JSONResponse(status_code=200, content=batch)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get(API_PREFIX + "/receipts")
async def get_receipts(
    request: Request,
    organ: str | None = Query(default=None),
    since: str | None = Query(default=None),
    limit: int = Query(default=100, ge=0, le=10000),
):
    led = _ledger(request)
    try:
        results = led.query(organ=organ, since=since, limit=limit)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return {
        "organ": organ,
        "since": since,
        "limit": limit,
        "count": len(results),
        "results": results,
    }


@app.get(API_PREFIX + "/chain/head")
async def get_chain_head(
    request: Request,
    organ: str = Query(..., description="organ whose chain head to return"),
):
    led = _ledger(request)
    try:
        return led.chain_head(organ)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get(API_PREFIX + "/health")
async def get_health(request: Request):
    led = _ledger(request)
    return led.health()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8088)
