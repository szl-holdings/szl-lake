---
license: cc-by-4.0
language:
  - en
pretty_name: SZL Holdings Data Lake
tags:
  - agentic-ai
  - governance
  - provable-provenance
  - cosign
  - dsse
  - khipu
  - formal-verification
  - lean4
  - slsa
  - doi:10.5281/zenodo.19944926
size_categories:
  - n<1K
task_categories:
  - other
configs:
  - config_name: receipts
    default: true
    data_files:
      - split: train
        path: khipu/*_receipts.parquet
---

<div align="center">
<p>

[![dataset](https://img.shields.io/badge/dataset-data%20lake-3af4c8?style=flat-square)](https://huggingface.co/datasets/SZLHOLDINGS/szl-lake/tree/main)
[![files](https://img.shields.io/badge/files-391-5b8dee?style=flat-square)](https://huggingface.co/datasets/SZLHOLDINGS/szl-lake/tree/main)
[![license](https://img.shields.io/badge/license-cc--by--4.0-7e8aa3?style=flat-square)](https://huggingface.co/datasets/SZLHOLDINGS/szl-lake)

</p>
</div>

# 🪢 SZL Holdings Data Lake (`szl-lake`)

> **A public, signed, diligence-defensible corpus of governance provenance.**
> Every governed action in the SZL substrate emits an ECDSA P-256 DSSE-signed **Khipu receipt** onto a hash-linked Merkle DAG. This dataset aggregates those receipts together with formal-verification doctrine snapshots, the Zenodo paper record, SBOM pointers, and compliance attestations so a reviewer can verify the claims end-to-end.

**Doctrine v11 LOCKED · 749 declarations / 14 unique axioms / 163 tracked sorries · Λ = Conjecture 1 (open, not a theorem).**

## Viewer contract

The Dataset Viewer intentionally exposes only `khipu/*_receipts.parquet` as the default `receipts` configuration. Those files share one fixed schema. The lake's other JSON, NDJSON, evidence, doctrine, and manifest files remain available by path, but they are heterogeneous artifacts and are not coerced into one false table.

The three byte-identical empty receipt chains are valid fixed-schema Parquet placeholders, not duplicated observations. They are retained for stable per-organ paths, contribute zero rows to the viewer, and are recorded in `khipu/EMPTY_CHAIN_MANIFEST.json` with their hashes and row counts.

## What's inside

```
SZLHOLDINGS/szl-lake/
├── README.md                         # this card
├── lake_index.json                   # master index — every file + sha256 + receipt counts
├── khipu/                            # live signed Khipu receipts, one parquet per organ
│   ├── amaru_receipts.parquet        # 14 real DSSE-signed tick receipts
│   ├── sentra_receipts.parquet       # 2 real signed verdict receipts
│   ├── a11oy_receipts.parquet        # live chain (currently empty — honest)
│   ├── rosie_receipts.parquet        # live chain (currently empty — honest)
│   ├── killinchu_receipts.parquet    # live chain (endpoint not yet exposed — honest)
│   └── EMPTY_CHAIN_MANIFEST.json     # hashes + zero-row declarations for empty chains
├── papers/manifest.json              # paper + concept/umbrella/version DOI links
├── trajectories/                     # multi-turn agent sessions (schema published; seeding pending)
├── sboms/manifest.json               # CycloneDX + SPDX pointers
├── doctrine/                         # periodic state snapshots + cosign fingerprints
├── attestations/                     # Section 889 · SLSA L1 (honest) attestations
└── keys/                             # cosign P-256 public keys + manifest
```

### Receipt schema (`khipu/*.parquet`)

| column | meaning |
|---|---|
| `receipt_id` / `actual_hash` | SHA-256 of the committed receipt (Khipu chain node) |
| `predicted_hash` | parent / predicted chain head (`receipts.in ≡ receipts.out`) |
| `organ` · `kind` · `index` | emitting organ, receipt kind, chain index |
| `lambda` · `lambda_pass` | 13-axis geometric-mean Λ score and pass flag |
| `lutar_anchor` | lutar-lean kernel SHA the runtime pins (`c7c0ba17`) |
| `decl` · `axioms` · `sorries` | doctrine numbers carried in the receipt payload |
| `dsse_sig` · `dsse_keyid` · `dsse_pae_sha256` · `dsse_signed` | DSSE signature material |
| `verify_key_url` | where to fetch the cosign public key |

> **No PII.** Receipts carry only hashes, axis scores, organ/kind, doctrine numbers, and timestamps. A PII scan (email/SSN/phone patterns) ran clean before publication.

## Read it

```python
from datasets import load_dataset

receipts = load_dataset("SZLHOLDINGS/szl-lake", "receipts")
print(receipts["train"])
```

Or read one Parquet artifact directly:

```python
import pyarrow.parquet as pq

table = pq.read_table(
    "hf://datasets/SZLHOLDINGS/szl-lake/khipu/amaru_receipts.parquet"
)
print(table.to_pandas().head())
```

## How to verify a receipt with cosign

Each receipt is signed with an ECDSA P-256 key (`keyid: szlholdings-cosign`, or per-organ keys under `keys/`).

```bash
curl -sL https://huggingface.co/datasets/SZLHOLDINGS/szl-lake/resolve/main/keys/org-cosign.pub -o cosign.pub
cosign verify-blob --key cosign.pub --signature <dsse_sig> <receipt-payload>
```

Per-organ fingerprints (SHA-256 of the public key) are pinned in `keys/MANIFEST.json` and `doctrine/v11_snapshot_20260602.json`.

## Honesty notes (diligence-defensible)

- **`lake build` status:** the published `main` of `szl-holdings/lutar-lean` currently **fails to compile** at `Lutar/KhipuConsensus.lean` (`unknown identifier 'Vector'` under Mathlib v4.13.0; CI run 26786461244). The runtime organs nonetheless serve the locked `749/14/163` constant. The source-level count on `main` HEAD is `774 decl / 14 axioms / 161 noncomment sorries` because it includes additive experimental modules excluded from the locked v11 baseline. See `doctrine/v11_snapshot_20260602.json`.
- **Empty chains are shown as empty.** Three organ chains have no receipts yet; their Parquet files are valid but empty rather than padded with synthetic data.
- **SBOMs and trajectories** are pointer manifests where the corpus has not yet been seeded; the schemas are published so consumers can build against them today.

## Cite this

Part of the SZL Holdings *Ouroboros Thesis* (Governed Post-Determinism).

Concept DOI (always-latest): [10.5281/zenodo.19944926](https://doi.org/10.5281/zenodo.19944926). Author: Stephen P. Lutar Jr. · [ORCID 0009-0001-0110-4173](https://orcid.org/0009-0001-0110-4173) · License CC-BY-4.0. Full DOI-pinned lineage and the paper index: [szl-papers PAPERS_INDEX](https://github.com/szl-holdings/szl-papers/blob/main/PAPERS_INDEX.md).

Honesty (Doctrine v11): Λ unconditional uniqueness is **Conjecture 1** (machine-checked false as stated), never a theorem; conditional uniqueness is **Theorem U** (axiom-free). Locked-proven formulas are **exactly 8** {F1,F4,F7,F11,F12,F18,F19,F22}; experimental theorems are a separate CI-green tier; Khipu BFT safety is Conjecture 2. Trust never 100%.

```bibtex
@dataset{szl_holdings_data_lake,
  title        = {SZL Holdings Data Lake},
  author       = {Lutar, Stephen Paul and {SZL Holdings}},
  year         = {2026},
  publisher    = {Hugging Face},
  doi          = {10.5281/zenodo.20434276},
  note         = {Concept DOI: 10.5281/zenodo.19944926. Doctrine v11 LOCKED 749/14/163.},
  url          = {https://huggingface.co/datasets/SZLHOLDINGS/szl-lake}
}
```

### Explore the SZL Holdings estate

[a-11-oy.com](https://a-11-oy.com) · [a11oy Space](https://huggingface.co/spaces/SZLHOLDINGS/a11oy) · [killinchu](https://huggingface.co/spaces/SZLHOLDINGS/killinchu) · [all SZLHOLDINGS datasets and models](https://huggingface.co/SZLHOLDINGS) · [GitHub org](https://github.com/szl-holdings)

