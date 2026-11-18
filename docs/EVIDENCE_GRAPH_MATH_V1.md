# SZL Lake — Source-bound evidence graph, v1

**Stage:** protected-source candidate, read-only and DISABLED by default.
**Owner:** `szl-holdings/szl-lake` (authoritative ledger + SQL projection).
**Doctrine:** existing locked formula metadata is not modified. The graph
contains descriptive statistics, not new Lean theorems, Byzantine consensus,
causal inference, a paid receipt, a signed attestation, or training authority.

## Reference architectures — study, adapt, do not copy

| Research / public project | Study | SZL's first-party adaptation |
| --- | --- | --- |
| [Palantir Foundry Ontology](https://www.palantir.com/docs/foundry/ontology/overview) | Objects, properties, links; action types and fine-grained object security are separate | Typed aggregate nodes and links; **zero action types**. Existing NDJSON remains authoritative; opt-in projection is not a new data store |
| [Microsoft GraphRAG](https://github.com/microsoft/graphrag) | Local/global/DRIFT query workflows over indexed entity and community artifacts | Provide honest, versioned typed evidence graph for future retrieval *after source and rights admission*. No unqualified generated summaries or GraphRAG-quality claim |
| [Anduril Lattice Python SDK](https://github.com/anduril/lattice-sdk-python) | Typed external entity/events interface, retries and bounded pagination | A stable, typed Python and JSON **read-only** contract; no claim of access to Anduril's proprietary platform or code reuse |
| [NVIDIA OpenUSD / Omniverse](https://github.com/NVIDIA-Omniverse) | Separable scene graph, asset validation and view composition | Frontend consumers may later map typed nodes/edges to OpenUSD or WebGPU, **after** provenance and scene validation. This is presently an accessible HTML table, not a 3D physical twin |
| [W3C PROV-O](https://www.w3.org/TR/prov-o/) | Explicit entities, derivation and activity semantics | Graph edges use **SZL-specific** `AGGREGATED_FROM`; these are aggregate counts, *not* a PROV-O conformance statement or proof of causal derivation |
| [SLSA build provenance](https://slsa.dev/spec/v1.2/build-provenance) | Build subject, predicate, builder identity, signer verification | Reuse exact local chain-head binding but identify SHA3-256 as an **unsigned checksum**, not a SLSA predicate or trusted issuer |

No upstream proprietary source, third-party algorithm implementation or
unlicensed dataset was imported. This uses the existing first-party
`ReceiptLedger`, `szl_lake_sql.summarize` and Python standard library.

## End-to-end data contract

```text
Owner-controlled Khipu NDJSON (hash-chained, append-only)
  -> ReceiptLedger.health + validated full query + recheck heads/count
  -> existing fixed, parameterized, in-memory SQLite summary
  -> szl_lake_graph.project(summary) (pure, zero I/O)
  -> fixed typed graph JSON, optional accessible HTML frontend
```

The graph's only source is an accepted **complete** SQL projection whose
`source_heads` bind each selected organ to its current local chain head.
It checks coverage, source and selected denominators, head formats,
per-organ equality of decision counts, and never includes original receipt
body, private file path, user query, tenant-specific ID or raw payload.

An unknown decision is mapped by the existing SQL owner to `OTHER` before
the graph receives it. Dynamic predicates/edge types, arbitrary SQL, caller
database URLs, source paths, code execution, external fetches and effectful
operations are not accepted.

The graph is typed:

- `SOURCE_SNAPSHOT` — one ephemeral logical projection; count is **all**
  validated source receipts, not the filtered selection.
- `ORGAN` — one node per selected organ, with a public count.
- `DECISION_CLASS` — one node per (organ, allowlisted decision).
- `AGGREGATED_FROM` — deterministic counted edges between a decision class
  and organ, and from organ to source snapshot.

Source heads and graph nodes are returned in stable sorted order. Invalid,
corrupted, truncated, racing or oversize inputs return **UNAVAILABLE**, never
a partial graph advertised as complete. A filter for a missing organ returns
a graph with no selected nodes but retains the **full source denominator**.

## Exact mathematics (purely descriptive)

For organ `o`, let `n(o,d)` be the observed count of class `d`, and
`N(o)=sum_d n(o,d)`. The original source total is `N_source`.
The graph returns:

```text
p_hat(d | o)  = n(o,d) / N(o)         (N(o)>0, observed frequency)
p_hat(o)      = N(o) / N_source      (N_source>0)
H(o)          = -sum_{d:n>0} p_hat(d|o)*log2(p_hat(d|o))  bits
```

Fractions and entropy are rounded to **six decimals** for display.
Counts remain integers and the exact base denominators are retained.
No value is a model-calibrated confidence, probability that a decision
was correct, clinical/physical claim, or causal effect.

Every graph digest is:

```text
graph_sha3_256 = SHA3-256(canonical_JSON({
  nodes, edges, distributions, source_heads,
  total_source_receipts, selected_receipts, organ_filter
}))
```

The timestamp of a query cannot change this stable checksum. Any
alteration of the included selected heads/counts changes it. An observer
able to rewrite both graph and digest can forge a matching checksum:
**no independent signer, cross-node quorum or non-equivocation proof exists
in this API**. The locked F4/F7/F22 declarations must never be cited as
proof of distributed correctness or this endpoint's runtime authority.
Unconditional Lambda uniqueness remains open/false-as-stated.

## Python/frontend/API boundaries

Installation includes `szl_lake_graph` in the existing package.
Public exposure is **not enabled** by package installation.

The exact opt-in is `SZL_LAKE_GRAPH_READONLY=1`. Any other value or unset
setting responds with HTTP **503 FEATURE_NOT_ADMITTED** *before reading*
the ledger. This is a source/developer preview switch, **not a substitute
for authentication**. Never enable it on an externally reachable service
without approved Access/tenant policy and data classification.

- `GET /api/lake/v1/graph/summary[?organ=...]`: fixed JSON schema
  `szl.lake.evidence-graph/v1` with nodes, edges, counts, source heads,
  deterministic SHA3 checksum and explicit false trust flags.
- `GET /graph[?organ=...]`: accessible HTML table of the exact same
  graph; no JavaScript, embedded third-party assets, redirects or cookies.
- Unknown/repeated query fields, raw SQL, URLs, invalid organ identifiers
  are HTTP 400. Corruption, ledger movement and feature HOLD are HTTP 503.
- `Cache-Control: no-store`, `X-Robots-Tag: noindex`; HTML
  CSP forbids scripts, plugins, forms, frames and external network assets.
- No write, train, deploy, payment or receipt-mint endpoint is added.

## Verified development checks and missing gates

Source checks are synthetic and local: corrupted NDJSON, source-head drift,
mid-read append, SQL injection attempts, empty selection, repeated filters,
unexpected decisions, malformed heads, concurrent GETs, secret-content
non-echo, HTML injection and disabled-by-default behavior.

**Not yet proven**: deployed HTTP endpoint; privacy/access review; validated
tenant segregation; independently signed DSSE; external signer trust and
current HF parity; append-only remote evidence; production SLO; consumer
compatibility tests on a real existing dashboard; OpenUSD/3D rendering;
actual cross-organ causal model; revenue settlement; a new PyPI release.

Next release should independently verify an authorized, least-privileged
read-only identity and a precisely scoped proof-visible dataset before
enabling `SZL_LAKE_GRAPH_READONLY` on a hosted service. A second system
can consume this JSON only after explicit schema/policy adoption; no
automatic Cloudflare routing or Hugging Face deployment is performed here.
