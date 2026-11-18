"""
szl_lake_hier.py -- Leveled Merkle hierarchy over one validated SQL summary

Doctrine: read-only, source-bound, deterministic, fail-closed.

Consumes ONLY a summary produced by szl_lake_sql.summarize (the same source
contract szl_lake_graph validates). It never reads the ledger directly, never
writes anything, and never claims signatures or production authority.

Levels:
  L0 leaf   : one (organ, decision) decision-class node per observed class
  L1 organ  : one node per organ, binding its L0 leaves + current chain head
  L2 estate : a single root binding every L1 node

Digests are SHA3-256 (chain_alg "sha3_256", the locked Khipu hash) over
szl_lake_store.canonical_json of a level-stable payload. They are UNSIGNED
checksums that position evidence in the DAG; they are NOT DSSE proofs and
must never be presented as such.

Author: Yachay <yachay@szlholdings.ai>
Apache-2.0 -- SZL Holdings 2026
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re

from szl_lake_store import canonical_json, normalize_organ

SCHEMA = "szl.lake.hierarchy/v1"
HASH_ALG = "sha3_256"
SOURCE_SCHEMA = "szl.lake.sql-summary/v1"
VALID_STATES = frozenset({"EMPTY", "LOCAL_CHAIN_VALIDATED"})

MAX_ORGANS = 128
MAX_ROWS = 10000
MAX_DECISION_CLASSES = 64
MAX_DECISION_LEN = 64

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DECISION_RE = re.compile(r"^[A-Z][A-Z0-9_-]*$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class HierarchyError(ValueError):
    """Stable, fail-closed refusal. The 'code' attribute carries HIER_* reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _sha3(payload: dict) -> str:
    data = canonical_json(payload)
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha3_256(data).hexdigest()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _source(summary: dict) -> dict:
    """Validate the summary the same way the graph does, with HIER_* codes."""
    if not isinstance(summary, dict):
        raise HierarchyError("HIER_SOURCE_NOT_DICT", "summary must be a JSON object")
    if summary.get("schema") != SOURCE_SCHEMA:
        raise HierarchyError("HIER_BAD_SCHEMA", "expected " + SOURCE_SCHEMA)
    if summary.get("state") not in VALID_STATES:
        raise HierarchyError("HIER_BAD_STATE", "state must be EMPTY or LOCAL_CHAIN_VALIDATED")
    if summary.get("analytics_engine") != "sqlite3:memory:":
        raise HierarchyError("HIER_BAD_ENGINE", "analytics engine must be sqlite3:memory:")
    if summary.get("authoritative_store") != "szl_lake_store NDJSON":
        raise HierarchyError("HIER_BAD_STORE", "authoritative store must be szl_lake_store NDJSON")
    if summary.get("coverage_complete") is not True:
        raise HierarchyError("HIER_BAD_COVERAGE", "coverage_complete must be True")
    if summary.get("ledger_writes") != 0:
        raise HierarchyError("HIER_LEDGER_WRITTEN", "summary must not have written the ledger")
    if summary.get("receipts_returned") != 0:
        raise HierarchyError("HIER_RECEIPTS_LEAKED", "summary must not return receipt bodies")
    if summary.get("signatures_verified") is not False:
        raise HierarchyError("HIER_SIGNATURES_CLAIMED", "signatures_verified must stay False")
    if summary.get("production_authorized") is not False:
        raise HierarchyError("HIER_PRODUCTION_CLAIMED", "production_authorized must stay False")

    rows_in = summary.get("rows_in_source")
    rows_match = summary.get("rows_matching_filter")
    if not _is_int(rows_in) or rows_in < 0:
        raise HierarchyError("HIER_BAD_COUNTS", "rows_in_source must be a non-negative int")
    if not _is_int(rows_match) or rows_match < 0 or rows_match > rows_in:
        raise HierarchyError("HIER_BAD_COUNTS", "rows_matching_filter must be within rows_in_source")
    if rows_in > MAX_ROWS:
        raise HierarchyError("HIER_TOO_MANY_ROWS", "rows_in_source exceeds the bound")

    flt = summary.get("filter")
    if not isinstance(flt, dict) or set(flt) != {"organ"}:
        raise HierarchyError("HIER_BAD_FILTER", "filter must be exactly one organ key")
    organ_filter = flt["organ"]
    if organ_filter is not None:
        if not isinstance(organ_filter, str) or normalize_organ(organ_filter) != organ_filter:
            raise HierarchyError("HIER_BAD_FILTER", "filter organ must be None or normalized")

    groups = summary.get("groups")
    heads = summary.get("source_heads")
    if not isinstance(groups, list) or not isinstance(heads, list):
        raise HierarchyError("HIER_BAD_SOURCE_BANDS", "groups and source_heads must be lists")
    if len(groups) > MAX_ORGANS:
        raise HierarchyError("HIER_TOO_MANY_ORGANS", "organ count exceeds the bound")

    if summary["state"] == "EMPTY" and (groups or heads or rows_in or rows_match):
        raise HierarchyError("HIER_EMPTY_NONZERO", "EMPTY summary must carry zero rows")

    by_organ: dict = {}
    for item in groups:
        if not isinstance(item, dict) or set(item) != {
            "organ", "count", "first_day", "last_day", "decision_counts"
        }:
            raise HierarchyError("HIER_BAD_GROUPS", "group items must carry exactly the 5 summary keys")
        name, count = item["organ"], item["count"]
        if not isinstance(name, str) or normalize_organ(name) != name:
            raise HierarchyError("HIER_BAD_GROUPS", "unnormalized organ in groups")
        if not _is_int(count) or count < 1:
            raise HierarchyError("HIER_BAD_GROUPS", "organ count must be at least 1")
        if not isinstance(item["first_day"], str) or not _DAY_RE.match(item["first_day"]):
            raise HierarchyError("HIER_BAD_GROUPS", "first_day malformed")
        if not isinstance(item["last_day"], str) or not _DAY_RE.match(item["last_day"]):
            raise HierarchyError("HIER_BAD_GROUPS", "last_day malformed")
        if item["first_day"] > item["last_day"]:
            raise HierarchyError("HIER_BAD_GROUPS", "day range inverted")
        counts = item["decision_counts"]
        if not isinstance(counts, dict) or not counts or len(counts) > MAX_DECISION_CLASSES:
            raise HierarchyError("HIER_BAD_GROUPS", "decision_counts malformed")
        total = 0
        for decision, hits in counts.items():
            if (
                not isinstance(decision, str)
                or not 1 <= len(decision) <= MAX_DECISION_LEN
                or not _DECISION_RE.match(decision)
            ):
                raise HierarchyError("HIER_BAD_GROUPS", "bad decision label")
            if not _is_int(hits) or hits < 1:
                raise HierarchyError("HIER_BAD_GROUPS", "decision hits must be at least 1")
            total += hits
        if total != count:
            raise HierarchyError("HIER_BAD_GROUPS", "decision counts do not sum to group count")
        if name in by_organ:
            raise HierarchyError("HIER_BAD_GROUPS", "duplicate organ in groups")
        by_organ[name] = item

    head_by_organ: dict = {}
    for item in heads:
        if not isinstance(item, dict) or set(item) != {"organ", "count", "chain_index", "chain_head"}:
            raise HierarchyError("HIER_BAD_HEADS", "head items must carry exactly the 4 head keys")
        name = item["organ"]
        if not isinstance(name, str) or normalize_organ(name) != name:
            raise HierarchyError("HIER_BAD_HEADS", "unnormalized organ in heads")
        if not _is_int(item["count"]) or item["count"] < 1:
            raise HierarchyError("HIER_BAD_HEADS", "head count must be at least 1")
        if item["count"] != item["chain_index"]:
            raise HierarchyError("HIER_BAD_HEADS", "head count must equal chain_index")
        if not isinstance(item["chain_head"], str) or not _HEX64_RE.match(item["chain_head"]):
            raise HierarchyError("HIER_BAD_HEADS", "chain_head must be sha3-256 hex")
        if name in head_by_organ:
            raise HierarchyError("HIER_BAD_HEADS", "duplicate head")
        head_by_organ[name] = item

    if set(by_organ) != set(head_by_organ):
        raise HierarchyError("HIER_HEAD_MISMATCH", "groups and source_heads cover different organs")
    for name, group in by_organ.items():
        if head_by_organ[name]["count"] != group["count"]:
            raise HierarchyError("HIER_HEAD_COUNT_MISMATCH", "head count differs from group count")

    if sum(g["count"] for g in by_organ.values()) != rows_match:
        raise HierarchyError("HIER_COUNT_MISMATCH", "group counts do not sum to rows_matching_filter")

    return {
        "state": summary["state"],
        "rows_in_source": rows_in,
        "rows_matching_filter": rows_match,
        "filter": flt,
        "groups": by_organ,
        "heads": head_by_organ,
    }


def project_hierarchy(summary: dict) -> dict:
    """Build the leveled Merkle hierarchy from one validated SQL summary."""
    src = _source(summary)
    leaves: list = []
    organs: list = []
    for name in sorted(src["groups"]):
        group = src["groups"][name]
        head = src["heads"][name]
        decision_digests: list = []
        for decision in sorted(group["decision_counts"]):
            leaf_payload = {
                "level": 0,
                "organ": name,
                "decision": decision,
                "count": group["decision_counts"][decision],
            }
            leaf = dict(leaf_payload)
            leaf["node_id"] = "L0/" + name + "/" + decision
            leaf["digest"] = _sha3(leaf_payload)
            leaves.append(leaf)
            decision_digests.append(leaf["digest"])
        organ_payload = {
            "level": 1,
            "organ": name,
            "count": group["count"],
            "chain_index": head["chain_index"],
            "chain_head": head["chain_head"],
            "first_day": group["first_day"],
            "last_day": group["last_day"],
            "decision_digests": decision_digests,
        }
        node = dict(organ_payload)
        node["node_id"] = "L1/" + name
        node["digest"] = _sha3(organ_payload)
        organs.append(node)

    root_payload = {
        "level": 2,
        "total_rows": src["rows_matching_filter"],
        "organ_digests": [[n["organ"], n["digest"]] for n in organs],
    }
    root = dict(root_payload)
    root["node_id"] = "L2/estate"
    root["digest"] = _sha3(root_payload)
    root["organ_count"] = len(organs)

    return {
        "schema": SCHEMA,
        "generated_at": _now(),
        "hash_alg": HASH_ALG,
        "levels": 3,
        "total_rows": src["rows_matching_filter"],
        "root": root,
        "organs": organs,
        "leaves": leaves,
        "source": {
            "schema": SOURCE_SCHEMA,
            "state": src["state"],
            "filter": src["filter"],
            "rows_in_source": src["rows_in_source"],
            "rows_matching_filter": src["rows_matching_filter"],
        },
        "invariants": [
            "Digests are SHA3-256 over canonical JSON; identical sources yield identical digests.",
            "An organ node binds its current chain head, so evidence anchored here is chain-located.",
            "Digests are unsigned checksums for DAG positioning, not DSSE proofs.",
            "No ledger writes, no receipt bodies, no signature or production claims.",
        ],
        "limitations": [
            "A hierarchy is a snapshot: the live ledger may have advanced past it.",
            "Local chain validation is tamper-evident, not independent non-equivocation.",
        ],
    }


def organ_path(hierarchy: dict, organ: str) -> dict:
    """Return the L2->L1->L0 anchor for one organ, fail-closed."""
    if not isinstance(hierarchy, dict) or hierarchy.get("schema") != SCHEMA:
        raise HierarchyError("HIER_BAD_HIERARCHY", "not a szl.lake.hierarchy/v1 document")
    root = hierarchy.get("root")
    if not isinstance(root, dict) or not isinstance(root.get("digest"), str) \
            or not _HEX64_RE.match(root["digest"]):
        raise HierarchyError("HIER_BAD_HIERARCHY", "root digest must be sha3-256 hex")
    nodes = hierarchy.get("organs")
    if not isinstance(nodes, list):
        raise HierarchyError("HIER_BAD_HIERARCHY", "organs band malformed")
    wanted = normalize_organ(organ)
    for node in nodes:
        if isinstance(node, dict) and node.get("organ") == wanted:
            return {
                "organ": wanted,
                "chain_index": node.get("chain_index"),
                "chain_head": node.get("chain_head"),
                "organ_digest": node.get("digest"),
                "root_digest": root["digest"],
                "levels": 3,
            }
    raise HierarchyError("HIER_UNKNOWN_ORGAN", "organ not in this hierarchy snapshot")
