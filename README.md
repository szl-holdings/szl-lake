> **SZL Holdings** · Doctrine v11 · Λ = Conjecture 1 (advisory, never "green"/theorem) · canonical [a-11-oy.com](https://a-11-oy.com)

# SZL Holdings Data Lake — `szl-lake`

[![HF Dataset](https://img.shields.io/badge/HF%20Dataset-SZLHOLDINGS%2Fszl--lake-FFD21E?style=flat-square&logo=huggingface&logoColor=000)](https://huggingface.co/datasets/SZLHOLDINGS/szl-lake)
[![Doctrine v11 LOCKED](https://img.shields.io/badge/Doctrine-v11_LOCKED_749%2F14%2F163-d4a444?style=flat-square)](https://github.com/szl-holdings/lutar-lean/commit/c7c0ba17)
[![License](https://img.shields.io/badge/License-Apache--2.0_%7C_CC--BY--4.0-blue?style=flat-square)](LICENSE)
[![DOI Thesis v18.0](https://zenodo.org/badge/DOI/10.5281/zenodo.20434276.svg)](https://doi.org/10.5281/zenodo.20434276) [![DOI Concept](https://zenodo.org/badge/DOI/10.5281/zenodo.19944926.svg)](https://doi.org/10.5281/zenodo.19944926)

> **GitHub front door.** This repository holds the README, small JSON indexes, and manifest pointers. The **HF dataset is canonical** for all NDJSON receipts and large binaries:
> **[huggingface.co/datasets/SZLHOLDINGS/szl-lake](https://huggingface.co/datasets/SZLHOLDINGS/szl-lake)**

The SZL Data Lake is the diligence-defensible corpus of governance receipts for SZL Holdings. Every governed action in the SZL substrate emits an ECDSA P-256 DSSE-signed **Khipu receipt** onto a hash-linked Merkle DAG. This dataset aggregates those receipts with formal-verification doctrine snapshots, the Zenodo paper record, SBOM pointers, and compliance attestations — so a reviewer can verify claims end-to-end.

**Doctrine v11 LOCKED · 749 declarations / 14 unique axioms / 163 tracked sorries · kernel commit [`c7c0ba17`](https://github.com/szl-holdings/lutar-lean/commit/c7c0ba17)**

---

## Lake Directories (7)

| Directory | Contents |
|---|---|
| `attestations/` | Section 889 (5 vendors), SLSA L1-honest level record, supply-chain self-attestation (no FedRAMP / Iron Bank / CMMC claimed) |
| `doctrine/` | v11 snapshot (749 declarations · 14 axioms · 163 sorries) pinned to kernel commit `c7c0ba17` |
| `keys/` | ECDSA P-256 cosign public keys per product |
| `khipu/` | DSSE-signed Khipu receipts (NDJSON, append-only) |
| `papers/` | Zenodo paper record references |
| `sboms/` | CycloneDX SBOM pointers |
| `trajectories/` | Bounded-recursion execution traces |
| `quant/` | DSSE-signed quant advisory receipts (ADVISORY / PAPER-ONLY — no execution, no custody, not financial advice; verification = integrity + origin only); `ledger/` is a ~6-hourly CI-verified mirror of the engine's autonomous paper ledger — every receipt + the full hash chain re-verified against the pinned key before each mirror lands |

---

## How to verify

### 0 — One command, offline

```bash
git clone https://github.com/szl-holdings/szl-lake && cd szl-lake
bash scripts/verify_lake_offline.sh
```

Re-verifies everything this clone can prove **without network**: the vendored
pinned verifier's own integrity (40-hex upstream pin + byte hash, tamper =
hard refuse), all quant genesis receipts (DSSE ed25519 vs the lake's pinned
key), the full autonomous-ledger hash chain, and every per-file sha256
manifest — then prints an honest list of what offline verification CANNOT
cover (cosign-keyless rows, GitHub↔HF equality, Rekor anchors). Verification
proves integrity + origin only, never accuracy or profitability. Requires
bash, python3, node ≥ 18.

### 1 — Fetch receipts from HF (canonical source)

```bash
# Pull a Khipu receipt stream (NDJSON, one signed receipt per line)
curl -fsSL \
  "https://huggingface.co/datasets/SZLHOLDINGS/szl-lake/resolve/main/khipu/a11oy_receipts.ndjson" \
  -o a11oy_receipts.ndjson

# Or read the first receipt directly with Python
python3 -c "
import json, urllib.request
url = 'https://huggingface.co/datasets/SZLHOLDINGS/szl-lake/resolve/main/khipu/a11oy_receipts.ndjson'
r = json.loads(urllib.request.urlopen(url).readline())
print(r['receipt_id'], r['dsse_keyid'])
"
```

### 2 — Verify DSSE signature

```bash
# Get the org-level cosign public key
curl -fsSL \
  "https://huggingface.co/datasets/SZLHOLDINGS/szl-lake/resolve/main/keys/org-cosign.pub" \
  -o cosign.pub

# Verify a receipt blob
cosign verify-blob --key cosign.pub --signature <dsse_sig_field> <receipt-payload>
```

### 3 — Check doctrine snapshot

```bash
curl -fsSL \
  "https://huggingface.co/datasets/SZLHOLDINGS/szl-lake/resolve/main/doctrine/manifest.json"
# Must show: "doctrine": "v11", "declarations": 749, "axioms": 14, "sorries": 163,
# "kernel_commit": "c7c0ba17"
```

---

## Unified Receipt Ledger

The **one durable ledger** every SZL component POSTs governance receipts to.
Previously each organ (ouroboros, hatun-mcp, szl-router, szl-mesh, vsp-otel,
szl-trust, a11oy …) held receipts in an in-process silo that never converged,
and `szl_lake_query.py` returned a stub. This is the real ingest + query path.

**Modules**

| File | Role |
|---|---|
| `szl_lake_store.py` | Durable, append-only, hash-chained NDJSON store (`ReceiptLedger`). Restart-safe — all state is rebuilt from disk, never an in-memory map that resets. |
| `szl_lake_server.py` | FastAPI service exposing the ingest + query API. |
| `szl_lake_client.py` | Fire-and-forget `emit_receipt(...)` helper for callers. |
| `szl_lake_query.py` | `query_receipts(...)` now reads the real store (stub path replaced for receipts; `LakeQuery` kept importable for back-compat). |

**Run it**

```bash
uvicorn szl_lake_server:app --host 0.0.0.0 --port 8088
# store root: $SZL_LAKE_DIR (default ./khipu)
```

### Endpoints (`/api/lake/v1`)

| Method · Path | Purpose |
|---|---|
| `POST /api/lake/v1/receipts` | Ingest one receipt (JSON object), a JSON array, or an **NDJSON batch** (`Content-Type: application/x-ndjson`). Idempotent on receipt `id`/`hash`. Returns `{accepted, ledger_offset, chain_head, chain_index, receipt_id}` (batch returns per-receipt results). |
| `GET /api/lake/v1/receipts?organ=&since=&limit=` | Real query over the store, newest-first. `since` is an ISO timestamp. |
| `GET /api/lake/v1/chain/head?organ=` | Current Khipu chain head + count for an organ (for cross-component verification). |
| `GET /api/lake/v1/chain/verify?organ=` | Re-derive an organ's Khipu chain straight from disk and report tamper-evidence: `{ok, count, chain_head, chain_index, broken:[…]}`. Detects on-disk mutation of a committed field, truncation, insertion, and re-ordering. Returns HTTP 200 even when `ok` is `false` (a detected break is a valid result, not a request error). |
| `GET /api/lake/v1/health` | Store reachable, total receipts, per-organ counts. |

The ingest accepts the live **DSSE receipt shape** a11oy emits:
`{id, ts, organ, decision, governance:{lambda, gates}, dsse:{payloadType, payload, signatures:[{sig, keyid}]}, energy}`.
Receipts are partitioned on disk by `khipu/<organ>/<YYYY-MM-DD>.ndjson`.

### `SZL_RECEIPT_SINK` wire contract

Downstream components wire in with one env var + one import:

```bash
export SZL_RECEIPT_SINK=https://<deployed-lake-host>
```

```python
from szl_lake_client import emit_receipt
emit_receipt(my_receipt)   # returns immediately, never raises
```

`emit_receipt` is **non-blocking and never raises** — a slow or down sink must
never take down a caller's governed action. If `SZL_RECEIPT_SINK` is unset it is
a safe no-op. `emit_batch(...)` POSTs an NDJSON batch the same way. Timeout
defaults to `1.5s` (override with `$SZL_RECEIPT_TIMEOUT`).

### Hash-chain semantics

Per organ, each stored envelope links `prev_hash → chain_hash` (the **Khipu**
chain, formulas **F4 / F22**):

```
chain_hash = SHA3-256( canonical_json({
    "prev_hash":   <prev chain_hash, or null at genesis>,
    "receipt_id":  <receipt id/hash, or content hash if id-less>,
    "organ":       <organ>,
    "ts":          <receipt timestamp>,
    "chain_index": <1-based position in this organ's chain>,
}) )
```

`chain_index` is strictly monotonic per organ; `GET /chain/head` returns the
latest `chain_hash` so any component can fetch and compare it for tamper-evident
cross-verification. The Khipu chain is **Conjecture 2 (advisory BFT) — NOT a
proven theorem.**

**Honest labels.** Energy is stored as `{"label": "MEASURED", "joules": …}`
only when a real measurement is supplied; when NVML/joules are absent it is
`{"label": "UNAVAILABLE"}` — joules are never fabricated.

---

## Cross-references

- **Formal proofs**: [lutar-lean](https://github.com/szl-holdings/lutar-lean) — Lean 4 kernel at commit [`c7c0ba17`](https://github.com/szl-holdings/lutar-lean/commit/c7c0ba17)
- **DOI (Thesis v18.0)**: [10.5281/zenodo.20434276](https://doi.org/10.5281/zenodo.20434276)
- **Concept DOI (always-latest)**: [10.5281/zenodo.19944926](https://doi.org/10.5281/zenodo.19944926)
- **Docs**: [docs.szlholdings.com/lake](https://szl-holdings.github.io/docs-site/lake)
- **HF org**: [huggingface.co/SZLHOLDINGS](https://huggingface.co/SZLHOLDINGS)

---

## Index

See [`lake_index.json`](lake_index.json) for the current pointer manifest (HF SHA, sync timestamp, directory list, doctrine constants).

**Sync workflows (two directions, non-overlapping scopes):**

| Workflow | Direction | Scope |
|---|---|---|
| [`sync-from-hf.yml`](.github/workflows/sync-from-hf.yml) | HF → GH | Refreshes the small attestation/doctrine/key manifests + records the current HF SHA in `lake_index.json` (every 6h). |
| [`hf-sync.yml`](.github/workflows/hf-sync.yml) | GH → HF | On push to `main` touching `data/**`, mirrors the receipt/data payload (`data/**`) verbatim to the HF dataset so external verifiers never lag behind GitHub. Receipt contents and their SHA3-256 Khipu chains are never modified; the HF dataset-card README is left untouched. Requires repo secret `HF_TOKEN` (write). |

---

## License

Data: [CC-BY-4.0](LICENSE) · Code/tooling: Apache-2.0

<sub>Doctrine v11 LOCKED · 749 / 14 / 163 · Λ = Conjecture 1 (open, not a theorem) · SLSA L1 honest · L2 verified-provenance on roadmap · no FedRAMP / Iron Bank / CMMC claimed</sub>
