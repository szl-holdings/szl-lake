#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# (c) 2026 Lutar, Stephen P. — SZL Holdings
"""Services/analytics: read-only SQL aggregate over an already validated Khipu ledger.

Source of truth remains the append-only, hash-chained NDJSON ledger. This
builds a bounded, in-memory SQLite projection from a single observed ledger
generation. It never writes back, imports third-party SQL, or accepts SQL text
from a request. Integrity is local hash-chain validation, NOT DSSE authority.
"""
from __future__ import annotations

from datetime import datetime, timezone
import html
import re
import sqlite3
from typing import Any

from szl_lake_store import ReceiptLedger, normalize_organ

SCHEMA = "szl.lake.sql-summary/v1"
MAX_ROWS = 10000
MAX_ORGANS = 128
DECISIONS = frozenset({"ADMIT", "ALLOW", "HOLD", "REVIEW", "DENY",
                       "REJECT", "ABSTAIN", "BLOCK", "UNKNOWN"})
DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T")
SHA3 = re.compile(r"^[0-9a-f]{64}$")


class SqlProjectionUnavailable(RuntimeError):
    """Stable reason only; never reveal storage paths or receipt data."""


def _source_state(snapshot: dict[str, Any]) -> tuple[Any, ...]:
    """Compare owner-validated head/count, not a process-local generated_at."""
    items = snapshot.get("organs")
    if not isinstance(items, dict):
        raise SqlProjectionUnavailable("LEDGER_INVENTORY_UNAVAILABLE")
    if len(items) > MAX_ORGANS:
        raise SqlProjectionUnavailable("LEDGER_ORGAN_LIMIT")
    result = []
    for organ, state in sorted(items.items()):
        if not isinstance(state, dict):
            raise SqlProjectionUnavailable("LEDGER_STATE_INVALID")
        count = state.get("count")
        position = state.get("chain_index")
        head = state.get("chain_head")
        if (type(count) is not int or count < 0
                or type(position) is not int or position < 0
                or count != position
                or (count > 0 and (not isinstance(head, str)
                                   or SHA3.fullmatch(head) is None))):
            raise SqlProjectionUnavailable("LEDGER_STATE_INVALID")
        result.append((organ, count, position, head))
    return tuple(result)


def _project_row(envelope: dict[str, Any]) -> tuple[str, str, str]:
    if not isinstance(envelope, dict):
        raise SqlProjectionUnavailable("ENVELOPE_INVALID")
    receipt = envelope.get("receipt")
    if not isinstance(receipt, dict):
        raise SqlProjectionUnavailable("ENVELOPE_INVALID")
    organ = envelope.get("organ")
    if not isinstance(organ, str):
        raise SqlProjectionUnavailable("ENVELOPE_INVALID")
    try:
        if normalize_organ(organ) != organ:
            raise SqlProjectionUnavailable("ORGAN_INVALID")
    except ValueError:
        raise SqlProjectionUnavailable("ORGAN_INVALID") from None
    decision = receipt.get("decision")
    if not isinstance(decision, str) or decision.upper() not in DECISIONS:
        decision = "OTHER"
    else:
        decision = decision.upper()
    timestamp = envelope.get("ts")
    day = timestamp[:10] if isinstance(timestamp, str) and DAY_PATTERN.match(timestamp) else "UNKNOWN"
    return (organ, day, decision)


def summarize(ledger: ReceiptLedger, organ: str | None = None) -> dict[str, Any]:
    """Fixed, parameterized SQLite aggregation; at most MAX_ROWS canonical rows.

    The ledger's own query still fully replays its store before it returns. The
    bound rejects oversized observed stores instead of presenting a truncated
    SQL slice as a complete inventory. Concurrent changes invalidate this
    attempt, not silently accepted as fresh proof.
    """
    if organ is not None:
        try:
            organ = normalize_organ(organ)
        except ValueError:
            raise ValueError("invalid organ filter") from None
    before = ledger.health()
    if not isinstance(before, dict) or before.get("ok") is not True:
        raise SqlProjectionUnavailable("LEDGER_UNAVAILABLE_OR_CORRUPTED")
    heads = _source_state(before)
    total = before.get("total_receipts")
    if type(total) is not int or total < 0 or total != sum(h[1] for h in heads):
        raise SqlProjectionUnavailable("LEDGER_COUNT_INVALID")
    if total > MAX_ROWS:
        raise SqlProjectionUnavailable("LEDGER_ROW_LIMIT")
    # The query validates stored envelope bytes; it never accepts a partial
    # prefix from a corrupt organ. No caller-provided SQL is executed.
    envelopes = ledger.query(limit=0)
    after = ledger.health()
    if (not isinstance(after, dict) or after.get("ok") is not True
            or _source_state(after) != heads or len(envelopes) != total):
        raise SqlProjectionUnavailable("LEDGER_CHANGED_DURING_READ")
    parsed = [_project_row(item) for item in envelopes]
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE snapshot (organ TEXT NOT NULL, day TEXT NOT NULL, "
            "decision TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO snapshot (organ, day, decision) VALUES (?, ?, ?)",
            parsed,
        )
        connection.execute("PRAGMA query_only=ON")
        where, params = (" WHERE organ=?", (organ,)) if organ is not None else ("", ())
        rows = connection.execute(
            "SELECT organ, COUNT(*), MIN(day), MAX(day) "
            "FROM snapshot" + where + " GROUP BY organ ORDER BY organ",
            params,
        ).fetchall()
        decisions = connection.execute(
            "SELECT organ, decision, COUNT(*) FROM snapshot" + where
            + " GROUP BY organ, decision ORDER BY organ, decision",
            params,
        ).fetchall()
        counted = connection.execute(
            "SELECT COUNT(*) FROM snapshot" + where, params
        ).fetchone()[0]
    finally:
        connection.close()
    by_decision: dict[str, dict[str, int]] = {}
    for key, decision, n in decisions:
        by_decision.setdefault(key, {})[decision] = n
    groups = [
        {"organ": key, "count": n, "first_day": start,
         "last_day": end, "decision_counts": by_decision.get(key, {})}
        for key, n, start, end in rows
    ]
    return {
        "schema": SCHEMA,
        "state": "EMPTY" if total == 0 else "LOCAL_CHAIN_VALIDATED",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analytics_engine": "sqlite3:memory:",
        "authoritative_store": "szl_lake_store NDJSON",
        "rows_in_source": total,
        "rows_matching_filter": counted,
        "coverage_complete": True,
        "max_rows": MAX_ROWS,
        "filter": {"organ": organ},
        "groups": groups,
        "source_heads": [
            {"organ": name, "count": count, "chain_index": position,
             "chain_head": head}
            for name, count, position, head in heads if organ is None or name == organ
        ],
        "ledger_writes": 0,
        "receipts_returned": 0,
        "signatures_verified": False,
        "production_authorized": False,
        "limitations": [
            "Local hash-chain integrity and exact current heads are not independent non-equivocation.",
            "No DSSE signature, signer trust, tenant authorization or model inference is verified.",
            "The SQL projection is ephemeral and is not another durable database.",
            "An HTTP 200 means this local snapshot answered, not that all SZL databases are live.",
        ],
    }


def render_html(document: dict[str, Any]) -> bytes:
    """Server-rendered Python UI, zero JavaScript and no external assets."""
    if document.get("schema") != SCHEMA:
        raise ValueError("wrong schema")
    def escape(value: Any) -> str:
        return html.escape(str(value), quote=True)

    summary = (
        "<p class='note'>This SQL table is a read-only, ephemeral projection "
        "of the validated append-only ledger. It is NOT a durable PostgreSQL "
        "replica, not a signature verifier, and not a promotion authority.</p>"
    )
    cells = []
    for item in document["groups"]:
        decisions = ", ".join(
            f"{escape(k)}: {escape(v)}"
            for k, v in sorted(item["decision_counts"].items())
        ) or "No classified decisions"
        cells.append(
            f"<tr><th scope='row'>{escape(item['organ'])}</th>"
            f"<td>{escape(item['count'])}</td>"
            f"<td>{escape(item['first_day'])}</td>"
            f"<td>{escape(item['last_day'])}</td>"
            f"<td>{decisions}</td></tr>"
        )
    if not cells:
        cells.append("<tr><td colspan='5'>No rows observed; EMPTY is not a readiness claim.</td></tr>")
    style = """
    :root{color-scheme:dark;--bg:#111920;--fg:#edf2f4;--sub:#abc0cb;--line:#35515f;--accent:#96e1ce}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
    font:1rem/1.65 system-ui,sans-serif}main{max-width:1050px;margin:auto;padding:clamp(1rem,5vw,3rem)}
    a{color:var(--accent)}h1{line-height:1.2}table{width:100%;border-collapse:collapse}
    th,td{padding:.65rem;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}
    .scroll{overflow-x:auto}.note{color:var(--sub)}code{word-break:break-all}
    a:focus-visible{outline:3px solid var(--accent);outline-offset:4px}
    """
    body = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>SZL Lake · SQL evidence summary</title>"
        f"<style>{style}</style></head><body><main>"
        "<nav><a href='/api/lake/v1/health'>Ledger health JSON</a> · "
        "<a href='/api/lake/v1/sql/summary'>SQL summary JSON</a></nav>"
        "<h1>SQL evidence summary</h1>"
        f"<p role='status'>State: <strong>{escape(document['state'])}</strong> · "
        f"Observed {escape(document['rows_matching_filter'])} row(s) from "
        f"{escape(document['rows_in_source'])} local ledger receipts.</p>"
        + summary
        + "<div class='scroll'><table><caption>Canonical organ-level counts; no receipt bodies</caption>"
        "<thead><tr><th>Organ</th><th>Count</th><th>First day</th>"
        "<th>Last day</th><th>Classified decisions</th></tr></thead>"
        f"<tbody>{''.join(cells)}</tbody></table></div>"
        f"<p class='note'>Captured: {escape(document['generated_at'])}. "
        "Refreshing does not mint a signature. No external providers contacted.</p>"
        "</main></body></html>"
    )
    return body.encode("utf-8")
