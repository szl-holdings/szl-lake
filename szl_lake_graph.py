#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Bounded, read-only evidence graph over one validated SZL Lake SQL snapshot.

The existing SQLite/NDJSON projection owns source validation; this module
projects only allowlisted counts and locally validated per-organ chain heads.
A hash of the projection is an UNSIGNED checksum, never DSSE issuer proof.
No arbitrary SQL, raw receipts, file paths, external providers, or mutations.
"""
from __future__ import annotations

import hashlib
import html
import math
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from szl_lake_sql import (
    DECISIONS,
    MAX_ORGANS,
    MAX_ROWS,
    SCHEMA as SQL_SCHEMA,
)
from szl_lake_store import canonical_json, normalize_organ

SCHEMA = "szl.lake.evidence-graph/v1"
ALLOWED_DECISIONS = frozenset({*DECISIONS, "OTHER"})
_SHA3_HEX = re.compile(r"^[0-9a-f]{64}$")
_MAX_NODES = 1 + MAX_ORGANS * (1 + len(ALLOWED_DECISIONS))


class GraphProjectionUnavailable(RuntimeError):
    """Finite refusal code; never embed receipt or filesystem contents."""


def _integer(value: Any, *, minimum: int = 0, maximum: int = MAX_ROWS) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _source_summary(summary: Mapping[str, Any]) -> tuple[int, int, str | None, list, list]:
    if not isinstance(summary, Mapping):
        raise GraphProjectionUnavailable("GRAPH_SOURCE_INVALID")
    if (summary.get("schema") != SQL_SCHEMA
            or summary.get("analytics_engine") != "sqlite3:memory:"
            or summary.get("authoritative_store") != "szl_lake_store NDJSON"
            or summary.get("coverage_complete") is not True
            or summary.get("ledger_writes") != 0
            or summary.get("receipts_returned") != 0
            or summary.get("signatures_verified") is not False
            or summary.get("production_authorized") is not False):
        raise GraphProjectionUnavailable("GRAPH_SOURCE_UNVERIFIED")

    total, matched = summary.get("rows_in_source"), summary.get("rows_matching_filter")
    if not _integer(total) or not _integer(matched) or matched > total:
        raise GraphProjectionUnavailable("GRAPH_COUNTS_INVALID")
    if summary.get("state") != ("EMPTY" if total == 0 else "LOCAL_CHAIN_VALIDATED"):
        raise GraphProjectionUnavailable("GRAPH_STATE_INVALID")

    selector = summary.get("filter")
    if not isinstance(selector, dict) or set(selector) != {"organ"}:
        raise GraphProjectionUnavailable("GRAPH_FILTER_INVALID")
    organ_filter = selector["organ"]
    if organ_filter is not None:
        try:
            if type(organ_filter) is not str or normalize_organ(organ_filter) != organ_filter:
                raise ValueError("invalid")
        except ValueError:
            raise GraphProjectionUnavailable("GRAPH_FILTER_INVALID") from None

    groups = summary.get("groups")
    heads = summary.get("source_heads")
    if not isinstance(groups, list) or not isinstance(heads, list):
        raise GraphProjectionUnavailable("GRAPH_SOURCE_INVALID")
    if len(groups) > MAX_ORGANS or len(heads) > MAX_ORGANS:
        raise GraphProjectionUnavailable("GRAPH_ORGAN_LIMIT")
    return total, matched, organ_filter, groups, heads


def project(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Project fixed 2-hop ontology from an already validated full SQL snapshot.

    Fractions are empirical observed decision frequencies, not confidence,
    correctness probabilities, causal effects, model scores, or authorization.
    """
    total, matched, selector, groups, heads = _source_summary(summary)
    expected_heads = {}
    for item in heads:
        if not isinstance(item, dict) or set(item) != {
            "organ", "count", "chain_index", "chain_head"
        }:
            raise GraphProjectionUnavailable("GRAPH_HEAD_INVALID")
        organ, n, index, head = (
            item["organ"], item["count"], item["chain_index"], item["chain_head"]
        )
        try:
            if type(organ) is not str or normalize_organ(organ) != organ:
                raise ValueError("bad organ")
        except ValueError:
            raise GraphProjectionUnavailable("GRAPH_HEAD_INVALID") from None
        if organ in expected_heads or (selector is not None and organ != selector):
            raise GraphProjectionUnavailable("GRAPH_HEAD_INVALID")
        if (not _integer(n, minimum=1) or index != n
                or type(index) is not int or not isinstance(head, str)
                or _SHA3_HEX.fullmatch(head) is None):
            raise GraphProjectionUnavailable("GRAPH_HEAD_INVALID")
        expected_heads[organ] = {"count": n, "chain_head": head, "chain_index": n}

    nodes = [{
        "id": "lake:validated-snapshot", "kind": "SOURCE_SNAPSHOT",
        "count": total, "label": "Validated local ledger projection",
    }]
    edges: list[dict[str, Any]] = []
    distributions: list[dict[str, Any]] = []
    seen = set()
    for group in groups:
        if not isinstance(group, dict) or set(group) != {
            "organ", "count", "first_day", "last_day", "decision_counts"
        }:
            raise GraphProjectionUnavailable("GRAPH_GROUP_INVALID")
        organ, n, decisions = group["organ"], group["count"], group["decision_counts"]
        try:
            if type(organ) is not str or normalize_organ(organ) != organ:
                raise ValueError("bad")
        except ValueError:
            raise GraphProjectionUnavailable("GRAPH_GROUP_INVALID") from None
        if (organ in seen or (selector is not None and organ != selector)
                or not _integer(n, minimum=1)
                or not isinstance(decisions, Mapping) or not decisions
                or not set(decisions).issubset(ALLOWED_DECISIONS)):
            raise GraphProjectionUnavailable("GRAPH_GROUP_INVALID")
        if any(not _integer(value, minimum=1) for value in decisions.values()):
            raise GraphProjectionUnavailable("GRAPH_GROUP_INVALID")
        if sum(decisions.values()) != n or expected_heads.get(organ, {}).get("count") != n:
            raise GraphProjectionUnavailable("GRAPH_SOURCE_MISMATCH")
        seen.add(organ)
        organ_node = "organ:" + organ
        nodes.append({"id": organ_node, "kind": "ORGAN", "label": organ, "count": n})
        edges.append({
            "from": organ_node, "to": "lake:validated-snapshot",
            "type": "AGGREGATED_FROM", "count": n,
            "fraction_of_source": round(n / total, 6),
        })
        entropy = -math.fsum((count / n) * math.log2(count / n)
                             for count in decisions.values())
        distributions.append({
            "organ": organ, "count": n, "decision_entropy_bits": round(entropy, 6),
            "decision_frequencies": [
                {"decision": key, "count": count,
                 "fraction_of_organ": round(count / n, 6)}
                for key, count in sorted(decisions.items())
            ],
        })
        for decision, count in sorted(decisions.items()):
            node_id = "decision:" + organ + ":" + decision
            nodes.append({
                "id": node_id, "kind": "DECISION_CLASS",
                "label": decision, "count": count,
            })
            edges.append({
                "from": node_id, "to": organ_node, "type": "AGGREGATED_FROM",
                "count": count, "fraction_of_organ": round(count / n, 6),
            })

    if (sum(x["count"] for x in distributions) != matched
            or seen != set(expected_heads)
            or (selector is None and matched != total)
            or (total > 0 and selector is None and not groups)
            or len(nodes) > _MAX_NODES):
        raise GraphProjectionUnavailable("GRAPH_SOURCE_MISMATCH")
    nodes.sort(key=lambda x: x["id"])
    edges.sort(key=lambda x: (x["from"], x["to"], x["type"]))
    distributions.sort(key=lambda x: x["organ"])
    safe_heads = [
        {"organ": organ, **expected_heads[organ]}
        for organ in sorted(expected_heads)
    ]
    stable = {
        "nodes": nodes,
        "edges": edges,
        "distributions": distributions,
        "source_heads": safe_heads,
        "total_source_receipts": total,
        "selected_receipts": matched,
        "organ_filter": selector,
    }
    digest = hashlib.sha3_256(canonical_json(stable)).hexdigest()
    return {
        "schema": SCHEMA,
        "state": "EMPTY" if total == 0 else "LOCAL_CHAIN_VALIDATED",
        "projection": "READ_ONLY_EPHEMERAL",
        "claim_scope": "LOCAL_CHAIN_INTEGRITY_ONLY",
        "schema_semantics": "SZL_PRIVATE_AGGREGATED_FROM_V1_NOT_PROV_O_CONFORMANCE",
        "source": {"schema": SQL_SCHEMA, "name": "szl_lake_sql.summarize",
                   "source_heads": safe_heads, "total_receipts": total,
                   "selected_receipts": matched, "organ_filter": selector},
        "graph": {"nodes": nodes, "edges": edges},
        "distributions": distributions,
        "graph_sha3_256": digest,
        "graph_checksum_signed": False,
        "independent_chain_heads_verified": False,
        "issuer_signatures_verified": False,
        "origin_authentication_verified": False,
        "tenant_authorization_verified": False,
        "causal_inference_performed": False,
        "model_training_authorized": False,
        "external_effects": 0,
        "backend_writes": 0,
        "limits": (
            "Complete bounded local NDJSON snapshot only. SHA3 checksum and "
            "local heads are not signature, non-equivocation, DSSE, SLSA, "
            "causal proof, legal rights, or production/tenant admission. "
            "Decision fractions and entropy describe this snapshot only."
        ),
    }


def render_html(doc: Mapping[str, Any]) -> bytes:
    """Accessible, script-free projection; no invented 3D or live claims."""
    if doc.get("schema") != SCHEMA or doc.get("projection") != "READ_ONLY_EPHEMERAL":
        raise ValueError("invalid graph document")
    esc = lambda value: html.escape(str(value), quote=True)
    totals = doc["source"]
    rows = []
    for dist in doc["distributions"]:
        entries = ", ".join(
            f"{esc(d['decision'])}: {esc(d['count'])}/{esc(dist['count'])}"
            for d in dist["decision_frequencies"]
        )
        rows.append(
            "<tr><th scope='row'>" + esc(dist["organ"]) + "</th>"
            "<td>" + esc(dist["count"]) + "</td>"
            "<td>" + esc(dist["decision_entropy_bits"]) + "</td>"
            "<td>" + entries + "</td></tr>"
        )
    table = "".join(rows) or '<tr><td colspan="4">EMPTY — no observed receipts</td></tr>'
    selector = totals["organ_filter"]
    suffix = "?organ=" + quote(selector, safe="") if selector is not None else ""
    page = (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>SZL Lake — source-bound evidence graph</title>'
        '<style>body{font:1rem/1.6 system-ui;max-width:72rem;margin:2rem auto;'
        'padding:0 1rem;color:#101827;background:#fff}table{border-collapse:collapse;'
        'width:100%}th,td{padding:.7rem;text-align:left;border:1px solid #64748b;'
        'vertical-align:top}th{background:#e2e8f0}code{overflow-wrap:anywhere}'
        'a:focus{outline:3px solid #2563eb}</style>'
        '<main><h1>Source-bound evidence graph — local projection only</h1>'
        '<p>The graph has one snapshot node, organ nodes, and decision-class nodes.'
        ' AGGREGATED_FROM edges encode counted observations, not causal relationships.</p>'
        '<p>Observed denominator: <strong>' + esc(totals["total_receipts"]) +
        '</strong> source receipts; selected: <strong>' + esc(totals["selected_receipts"]) +
        '</strong>. Local chain validation is not issuer signature verification.</p>'
        '<p>Checksum (SHA3-256, unsigned): <code>' + esc(doc["graph_sha3_256"]) +
        '</code>.</p><table><caption>Organ to decision evidence graph</caption>'
        '<thead><tr><th scope="col">Organ node</th><th scope="col">Count</th>'
        '<th scope="col">Shannon entropy (bits)</th>'
        '<th scope="col">Decision-class edges (count / organ count)</th></tr></thead>'
        '<tbody>' + table + '</tbody></table>'
        '<p><a href="/api/lake/v1/graph/summary' + esc(suffix) +
        '">Typed JSON graph</a>.'
        ' No external effects, model training, signatures or causal conclusions.</p>'
        '</main></html>'
    )
    return page.encode("utf-8")
