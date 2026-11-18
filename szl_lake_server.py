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

A corrupted organ chain fails closed: ingest and chain-head calls for it answer
HTTP 503 {"error": "ledger_corrupted", "corruption": {file, line, reason, ...}}
and nothing is written; a writer-lock timeout answers 503 too.

Run locally:
  uvicorn szl_lake_server:app --host 0.0.0.0 --port 8088
Store location is $SZL_LAKE_DIR (default ./khipu).

stdlib + FastAPI (the framework already vendored for this repo's tooling).
Apache-2.0 — SZL Holdings 2026.
"""
from __future__ import annotations

import json
import os

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from szl_lake_graph import GraphProjectionUnavailable, project as project_evidence_graph, render_html as render_graph_html
from szl_lake_sql import SqlProjectionUnavailable, render_html, summarize
from szl_lake_store import (
    LedgerCorruptionError,
    LedgerLockTimeout,
    ReceiptLedger,
    get_default_ledger,
    strict_json_loads,
)

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


def _unavailable(exc: Exception) -> JSONResponse:
    """503 for a ledger that refuses work (corrupted chain or lock timeout)."""
    if isinstance(exc, LedgerCorruptionError):
        content = {"error": "ledger_corrupted", "corruption": exc.as_dict()}
    else:
        content = {"error": "ledger_lock_timeout", "detail": str(exc)}
    return JSONResponse(status_code=503, content=content)


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
            parsed = strict_json_loads(text)
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
            obj = strict_json_loads(line)
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
    except (LedgerCorruptionError, LedgerLockTimeout) as e:
        return _unavailable(e)
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
    except (LedgerCorruptionError, LedgerLockTimeout) as e:
        return _unavailable(e)
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
    except (LedgerCorruptionError, LedgerLockTimeout) as e:
        return _unavailable(e)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get(API_PREFIX + "/chain/verify")
async def get_chain_verify(
    request: Request,
    organ: str = Query(..., description="organ whose chain to re-verify"),
):
    """Re-derive an organ's Khipu chain from disk (tamper-EVIDENT, advisory).

    Returns {organ, ok, count, chain_head, chain_index, broken:[...]}. A caller
    can poll this to detect on-disk tampering / truncation / re-ordering; ok is
    False (with per-break detail) when the stored chain no longer re-derives.
    Responds HTTP 200 even when ok is False — a detected break is a valid,
    successful verification result, not a request error.
    """
    led = _ledger(request)
    try:
        return led.verify_chain(organ)
    except LedgerLockTimeout as e:
        return _unavailable(e)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get(API_PREFIX + "/health")
async def get_health(request: Request):
    led = _ledger(request)
    try:
        return led.health()
    except LedgerLockTimeout as e:
        return _unavailable(e)


# SQL is an ephemeral, fixed-query display over owner-validated NDJSON. Neither
# this GET path nor its Python-rendered UI mutates the ledger or signs receipts.
_SQL_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _sql_summary(request: Request, organ: str | None):
    if set(request.query_params.keys()) - {"organ"}:
        return JSONResponse(
            {"state": "INVALID_REQUEST", "code": "UNSUPPORTED_QUERY_PARAMETER"},
            status_code=400, headers=_SQL_HEADERS,
        )
    try:
        return summarize(_ledger(request), organ=organ)
    except ValueError:
        return JSONResponse(
            {"state": "INVALID_REQUEST", "code": "ORGAN_INVALID"},
            status_code=400, headers=_SQL_HEADERS,
        )
    except SqlProjectionUnavailable as exc:
        return JSONResponse(
            {"state": "UNAVAILABLE", "code": str(exc)},
            status_code=503, headers=_SQL_HEADERS,
        )
    except (LedgerCorruptionError, LedgerLockTimeout):
        return JSONResponse(
            {"state": "UNAVAILABLE", "code": "LEDGER_CORRUPTED_OR_LOCKED"},
            status_code=503, headers=_SQL_HEADERS,
        )


@app.get(API_PREFIX + "/sql/summary")
async def get_sql_summary(request: Request, organ: str | None = Query(default=None)):
    result = _sql_summary(request, organ)
    return (result if isinstance(result, JSONResponse)
            else JSONResponse(result, headers=_SQL_HEADERS))


@app.get("/sql")
async def sql_dashboard(request: Request, organ: str | None = Query(default=None)):
    result = _sql_summary(request, organ)
    if isinstance(result, JSONResponse):
        return result
    content = render_html(result)
    return Response(content, media_type="text/html", headers={
        **_SQL_HEADERS,
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'none'; "
            "object-src 'none'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'"
        ),
    })


# The graph is a read-only local presentation derived from the exact SQL
# summary. Off by default: configuring a public network endpoint additionally
# requires independent access/privacy review. Never read source on a disabled
# request, and never infer signature or production authorization from HTTP 200.
_GRAPH_HEADERS = {**_SQL_HEADERS, "X-Robots-Tag": "noindex"}


def _graph_summary(request: Request, organ: str | None):
    if os.environ.get("SZL_LAKE_GRAPH_READONLY") != "1":
        return JSONResponse(
            {"state": "UNAVAILABLE", "code": "FEATURE_NOT_ADMITTED"},
            status_code=503, headers=_GRAPH_HEADERS,
        )
    if (set(request.query_params.keys()) - {"organ"}
            or len(request.query_params.getlist("organ")) > 1):
        return JSONResponse(
            {"state": "INVALID_REQUEST", "code": "UNSUPPORTED_QUERY_PARAMETER"},
            status_code=400, headers=_GRAPH_HEADERS,
        )
    snapshot = _sql_summary(request, organ)
    if isinstance(snapshot, JSONResponse):
        snapshot.headers["X-Robots-Tag"] = "noindex"
        return snapshot
    try:
        return project_evidence_graph(snapshot)
    except GraphProjectionUnavailable as exc:
        return JSONResponse(
            {"state": "UNAVAILABLE", "code": str(exc)},
            status_code=503, headers=_GRAPH_HEADERS,
        )


@app.get(API_PREFIX + "/graph/summary")
async def get_graph_summary(request: Request, organ: str | None = Query(default=None)):
    result = _graph_summary(request, organ)
    return (result if isinstance(result, JSONResponse)
            else JSONResponse(result, headers=_GRAPH_HEADERS))


@app.get("/graph")
async def graph_dashboard(request: Request, organ: str | None = Query(default=None)):
    result = _graph_summary(request, organ)
    if isinstance(result, JSONResponse):
        return result
    return Response(
        render_graph_html(result), media_type="text/html", headers={
            **_GRAPH_HEADERS, "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'none'; "
                "object-src 'none'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'none'"
            ),
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8088)
