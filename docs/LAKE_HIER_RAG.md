# Lake hierarchy + bounded RAG

Two new read-only lake modules advance the DAG frontier:

## szl_lake_hier.py -- leveled Merkle hierarchy (schema szl.lake.hierarchy/v1)

Builds a three-level hierarchy over ONE validated `szl_lake_sql` summary:

- **L0 leaf**: one node per (organ, decision) class observed in the summary.
- **L1 organ**: one node per organ, binding its L0 leaf digests plus the
  organ's CURRENT chain head and chain index.
- **L2 estate**: a single root digest binding every L1 node.

Digests are SHA3-256 over `szl_lake_store.canonical_json` of level-stable
payloads: identical sources always yield identical digests. Digests are
UNSIGNED checksums that position evidence in the DAG. They are not DSSE
proofs and never claim to be.

The module re-validates its source summary with the same invariants the
evidence graph enforces (analytics engine `sqlite3:memory:`, zero ledger
writes, zero receipt bodies returned, no signature or production claims)
and refuses with stable `HIER_*` codes otherwise.

## szl_lake_rag.py -- bounded hierarchy-anchored retrieval (schema szl.lake.rag/v1)

A RAG surface with the lake's doctrine applied end to end:

1. **Deterministic lexical match** over organ names (no embeddings, no
   external providers, no network).
2. **Source-bound fetch**: evidence comes from `ReceiptLedger.query` on the
   validated hash chain, never from a cache.
3. **Whitelist projection**: results carry only organ, receipt id, decision,
   ts, chain_index, chain_hash. Raw receipt payloads are never projected.
4. **Hierarchy anchor**: with a hierarchy snapshot each result carries its
   DAG position (organ digest, chain head, root digest). A stale snapshot
   reports `hierarchy_stale` instead of guessing a position.
5. **Fail-closed**: no lexical match means verdict `NO_EVIDENCE`, nothing
   fabricated.

Bounds: query <= 256 chars / <= 32 tokens, limit 1..20 results per organ,
<= 8 organs matched per query.

## Why not reuse szl_lake_query.py

`szl_lake_query.query_receipts` returns full raw envelopes (including
complete receipt payloads). The RAG layer is intentionally stricter: it is
the bounded, whitelist-projected surface safe to expose to higher layers.
