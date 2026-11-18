"""
szl_lake_rag.py -- Bounded, read-only, hierarchy-anchored retrieval over the lake

This is NOT a generative model and NOT an embedding search:

- matching is deterministic lexical matching over organ names (no embeddings,
  no external providers, no network);
- evidence is fetched source-bound from the validated hash chain via
  ReceiptLedger.query, never from a cache or a copy;
- results are whitelist-projected: only receipt id / decision plus the
  envelope chain fields (organ, ts, chain_index, chain_hash). Raw receipt
  payloads are NEVER projected, even when they parse. Test fixtures carry
  confidential="NEVER_PUBLISH_RAW_PAYLOAD" to prove it;
- fail-closed: when nothing matches, the verdict is NO_EVIDENCE and nothing
  is fabricated;
- optional hierarchy anchor: when a szl_lake_hier snapshot is supplied, each
  result carries its DAG position (organ digest, chain head, root digest).
  A stale snapshot never fabricates positions; a missing organ is reported
  as hierarchy_stale, not guessed.

Author: Yachay <yachay@szlholdings.ai>
Apache-2.0 -- SZL Holdings 2026
"""
from __future__ import annotations

import datetime as _dt
import re

from szl_lake_hier import HierarchyError, organ_path
from szl_lake_store import normalize_organ

SCHEMA = "szl.lake.rag/v1"

MAX_QUERY_CHARS = 256
MAX_TOKENS = 32
MAX_RESULTS = 20
MAX_ORGANS_MATCHED = 8

RESULT_FIELDS = (
    "organ",
    "receipt_id",
    "chain_index",
    "chain_hash",
    "ts",
    "decision",
    "hierarchy_path",
    "hierarchy_stale",
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class RagError(ValueError):
    """Stable, fail-closed refusal. The 'code' attribute carries RAG_* reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _tokenize(query: str) -> list:
    tokens = _TOKEN_RE.findall(query.lower())
    if not tokens:
        raise RagError("RAG_BAD_QUERY", "query must contain at least one alphanumeric token")
    return tokens[:MAX_TOKENS]


def _match_organs(tokens: list, organ_names: list) -> list:
    """Deterministic lexical match. Score = distinct tokens hitting the name."""
    matched = []
    for name in organ_names:
        lowered = name.lower()
        words = set(_TOKEN_RE.findall(lowered))
        score = sum(1 for tok in set(tokens) if tok in words or tok in lowered)
        if score > 0:
            matched.append((name, score))
    matched.sort(key=lambda pair: (-pair[1], pair[0]))
    return [name for name, _ in matched[:MAX_ORGANS_MATCHED]]


def _project(organ: str, envelope: dict, path) -> dict:
    """Whitelist projection of ONE validated envelope. Nothing else passes."""
    if not isinstance(envelope, dict):
        raise RagError("RAG_BAD_ENVELOPE", "ledger returned a non-object envelope")
    for key in ("receipt_id", "chain_index", "chain_hash", "ts"):
        if key not in envelope:
            raise RagError("RAG_BAD_ENVELOPE", "envelope missing " + key)
    receipt = envelope.get("receipt")
    decision = None
    if isinstance(receipt, dict):
        value = receipt.get("decision")
        if isinstance(value, str):
            decision = value
    return {
        "organ": organ,
        "receipt_id": envelope["receipt_id"],
        "chain_index": envelope["chain_index"],
        "chain_hash": envelope["chain_hash"],
        "ts": envelope["ts"],
        "decision": decision,
        "hierarchy_path": path,
        "hierarchy_stale": path is None,
    }


def _synthesize(tokens: list, organs: list, results: list) -> str:
    """Template-bound synthesis over whitelisted fields only."""
    per_organ = []
    for organ in organs:
        rows = [r for r in results if r["organ"] == organ]
        tally: dict = {}
        for row in rows:
            key = row["decision"] if row["decision"] else "UNKNOWN"
            tally[key] = tally.get(key, 0) + 1
        decisions = ", ".join(
            key + "=" + str(tally[key]) for key in sorted(tally)
        )
        per_organ.append(
            "organ '" + organ + "': " + str(len(rows))
            + " chain-validated receipt(s), newest first; decisions " + decisions
        )
    return ("Retrieved evidence for " + str(len(organs)) + " organ(s); "
            + "; ".join(per_organ) + ". No raw receipt payloads are projected.")


def ask(ledger, query: str, hierarchy=None, limit: int = 10) -> dict:
    """Answer one bounded query against the live ledger, fail-closed."""
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise RagError("RAG_BAD_QUERY", "query must be a non-empty string of at most 256 chars")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
        raise RagError("RAG_BAD_LIMIT", "limit must be an int in 1.." + str(MAX_RESULTS))
    if not hasattr(ledger, "query") or not hasattr(ledger, "health"):
        raise RagError("RAG_BAD_LEDGER", "ledger must be a ReceiptLedger")

    tokens = _tokenize(query)

    health = ledger.health()
    if not isinstance(health, dict) or not isinstance(health.get("organs"), dict):
        raise RagError("RAG_LEDGER_UNREADABLE", "ledger health did not return an organ inventory")
    organ_names = [normalize_organ(name) for name in health["organs"]]

    matched = _match_organs(tokens, organ_names)

    root_digest = None
    if hierarchy is not None:
        if not isinstance(hierarchy, dict):
            raise RagError("RAG_BAD_HIERARCHY", "hierarchy must be a hierarchy document or None")
        root_digest = hierarchy.get("root", {}).get("digest")

    if not matched:
        return {
            "schema": SCHEMA,
            "generated_at": _now(),
            "verdict": "NO_EVIDENCE",
            "query_tokens": tokens,
            "organs_matched": [],
            "results": [],
            "counts": {"organs_considered": len(organ_names), "results_returned": 0},
            "hierarchy_root_digest": root_digest,
            "synthesis": ("No organ matches the query tokens; fail-closed: "
                          "no evidence was retrieved and nothing was fabricated."),
            "bounds": {"max_results_per_organ": MAX_RESULTS, "max_organs_matched": MAX_ORGANS_MATCHED},
            "limitations": [
                "Lexical matching only: a semantically related organ with no shared token is not returned.",
                "NO_EVIDENCE means no lexical match, not an assertion the evidence does not exist.",
            ],
        }

    results = []
    for organ in matched:
        path = None
        if hierarchy is not None:
            try:
                path = organ_path(hierarchy, organ)
            except HierarchyError:
                path = None
        for envelope in ledger.query(organ=organ, limit=limit):
            results.append(_project(organ, envelope, path))

    return {
        "schema": SCHEMA,
        "generated_at": _now(),
        "verdict": "EVIDENCE_RETRIEVED",
        "query_tokens": tokens,
        "organs_matched": matched,
        "results": results,
        "counts": {
            "organs_considered": len(organ_names),
            "organs_matched": len(matched),
            "results_returned": len(results),
        },
        "hierarchy_root_digest": root_digest,
        "synthesis": _synthesize(tokens, matched, results),
        "bounds": {
            "max_results_per_organ": limit,
            "max_organs_matched": MAX_ORGANS_MATCHED,
            "result_fields": list(RESULT_FIELDS),
        },
        "limitations": [
            "Retrieval is bounded and lexical; ranking is newest-first by chain order.",
            "Raw receipt payloads are never projected; synthesis uses whitelisted fields only.",
        ],
    }
