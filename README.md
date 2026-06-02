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
size_categories:
  - 100M<n<1B
task_categories:
  - other
---

# 🪢 SZL Holdings Data Lake (`szl-lake`)

> **A public, signed, diligence-defensible corpus of governance provenance.**
> Every governed action in the SZL substrate emits an ECDSA P-256 DSSE-signed **Khipu receipt** onto a hash-linked Merkle DAG. This dataset aggregates those receipts together with the formal-verification doctrine snapshots, the Zenodo paper record, SBOM pointers, and compliance attestations — so a reviewer can verify the claims end-to-end.

**Doctrine v11 LOCKED · 749 declarations / 14 unique axioms / 163 tracked sorries · Λ = Conjecture 1 (open, not a theorem).**

---

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
│   └── killinchu_receipts.parquet    # live chain (endpoint not yet exposed — honest)
├── papers/manifest.json              # 8 papers + concept/umbrella/version DOIs (links to Zenodo; no PDF duplication)
├── trajectories/                     # multi-turn agent sessions (schema published; seeding pending)
│   ├── manifest.json
│   └── trajectories.parquet
├── sboms/manifest.json               # CycloneDX + SPDX pointers (CI artifacts, per repo)
├── doctrine/                         # periodic 749/14/163 state snapshots + cosign fingerprints
│   ├── v11_snapshot_20260602.json
│   └── manifest.json
├── attestations/                     # Section 889 · CMMC L1 self-attest · SLSA level
│   ├── section_889_attestation.json
│   ├── cmmc_l1_self_attest.json
│   └── slsa_level.json
└── keys/                             # cosign P-256 public keys (org + 5 organs) + MANIFEST
```

### Receipt schema (`khipu/*.parquet`)

| column | meaning |
|---|---|
| `receipt_id` / `actual_hash` | SHA-256 of the committed receipt (Khipu chain node) |
| `predicted_hash` | parent / predicted chain head (`receipts.in ≡ receipts.out`) |
| `organ` · `kind` · `index` | emitting organ, receipt kind (tick/verdict), chain index |
| `lambda` · `lambda_pass` | 13-axis geometric-mean Λ score and pass flag |
| `lutar_anchor` | lutar-lean kernel SHA the runtime pins (`c7c0ba17`) |
| `decl` · `axioms` · `sorries` | doctrine numbers carried in the receipt payload (749/14/163) |
| `dsse_sig` · `dsse_keyid` · `dsse_pae_sha256` · `dsse_signed` | DSSE signature material (ECDSA-P256-SHA256) |
| `verify_key_url` | where to fetch the cosign public key |

> **No PII.** Receipts carry only hashes, axis scores, organ/kind, doctrine numbers, and timestamps. A PII scan (email/SSN/phone patterns) ran clean before publication.

---

## How to verify a receipt with cosign

Each receipt is signed with an ECDSA P-256 key (`keyid: szlholdings-cosign`, or per-organ keys under `keys/`).

```bash
# 1. Get the public key (org-level)
curl -sL https://huggingface.co/datasets/SZLHOLDINGS/szl-lake/resolve/main/keys/org-cosign.pub -o cosign.pub

# 2. Reconstruct the DSSE PAE payload for a receipt and verify
cosign verify-blob --key cosign.pub --signature <dsse_sig> <receipt-payload>

# Or use a live organ's verify endpoint:
curl https://szlholdings-amaru.hf.space/khipu/pubkey?keyid=amaru-cosign
```

Per-organ fingerprints (SHA-256 of the public key) are pinned in `keys/MANIFEST.json` and `doctrine/v11_snapshot_20260602.json`.

---

## Read it

```python
import pyarrow.parquet as pq
t = pq.read_table("hf://datasets/SZLHOLDINGS/szl-lake/khipu/amaru_receipts.parquet")
print(t.to_pandas().head())
```

---

## Honesty notes (diligence-defensible)

- **`lake build` status:** the published `main` of `szl-holdings/lutar-lean` currently **fails to compile** at `Lutar/KhipuConsensus.lean` (`unknown identifier 'Vector'` under Mathlib v4.13.0; CI run 26786461244). The **runtime** organs nonetheless serve the locked `749/14/163` constant. The source-level count on `main` HEAD is `774 decl / 14 axioms / 161 noncomment sorries` because it includes additive experimental modules that are excluded from the locked v11 baseline. See `doctrine/v11_snapshot_20260602.json`.
- **Empty chains are shown as empty.** Three organ chains have no receipts yet; their parquet files are valid but empty rather than padded with synthetic data.
- **SBOMs & trajectories** are pointer manifests where the corpus has not yet been seeded — the schemas are published so consumers can build against them today.

---

## Cite this

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

---

<sub>🪢 Khipu chain · Lean 4 (Mathlib v4.13.0) · Sigstore Rekor · CC-BY-4.0 · Concept DOI [10.5281/zenodo.19944926](https://doi.org/10.5281/zenodo.19944926) · Umbrella DOI [10.5281/zenodo.20434276](https://doi.org/10.5281/zenodo.20434276) · **Doctrine v11 LOCKED · 749 / 14 / 163 · Λ Conjecture 1 (open)** · Signed-off-by: Yachay (CTO) · Co-Authored-By: Perplexity Computer Agent</sub>
