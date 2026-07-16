#!/usr/bin/env bash
# verify_lake_offline.sh — one command, zero network: re-verify everything in
# this clone that CAN be verified offline, and say honestly what cannot.
#
#   git clone https://github.com/szl-holdings/szl-lake && cd szl-lake
#   bash scripts/verify_lake_offline.sh
#
# Requires: bash, python3, node >= 18. Exit 0 iff every executed check PASSES.
# Doctrine: fail closed; verification proves INTEGRITY + ORIGIN of the signed
# artifacts, never accuracy, performance, or profitability (quant receipts are
# ADVISORY / PAPER-ONLY).
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
FAIL=0
step() { printf '\n== [%s] %s ==\n' "$1" "$2"; }

step 1/4 "vendored-tool self-integrity (pin + byte hash)"
python3 - <<'PY' || FAIL=1
import json, hashlib, re, sys
v = json.load(open("tools/vendored/VENDORED.json"))
bad = 0
for e in v["files"]:
    pin = e["upstream"]["commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", pin):
        print(f"FAIL {e['file']}: pin is not a full 40-hex commit ({pin!r})"); bad = 1; continue
    h = hashlib.sha256(open(e["file"], "rb").read()).hexdigest()
    if h != e["sha256"]:
        print(f"FAIL {e['file']}: sha256 {h[:16]}… != vendored manifest {e['sha256'][:16]}… (TAMPER or drift — refuse to execute)"); bad = 1
    else:
        print(f"OK   {e['file']}  sha256={h[:16]}…  pinned to {e['upstream']['repo']}@{pin[:12]}")
sys.exit(bad)
PY
[ "$FAIL" -ne 0 ] && { echo "ABORT: refusing to run a tampered vendored verifier."; exit 1; }

step 2/4 "quant genesis receipts (DSSE ed25519 vs the lake's pinned engine key)"
( cd data/quant && node "$ROOT/tools/vendored/verify.mjs" --pubkey keys/engine_pubkey.json *.receipt.json ) || FAIL=1

step 3/4 "quant autonomous ledger: full hash-chain walk (seq, prev-pointers, dir coverage, per-file sha256)"
( cd data/quant && node "$ROOT/tools/vendored/verify.mjs" --pubkey keys/engine_pubkey.json --chain ledger/ ) || FAIL=1

step 4/4 "per-file sha256 manifests"
python3 - <<'PY' || FAIL=1
import json, hashlib, pathlib, sys
root = pathlib.Path("data"); bad = 0
m = json.load(open(root/"quant/manifest.json"))
n = 0
for e in m["files"]:
    p = root/"quant"/e["path"]
    if not p.exists(): print(f"FAIL quant: MISSING {e['path']}"); bad = 1; continue
    if hashlib.sha256(p.read_bytes()).hexdigest() != e["sha256"]:
        print(f"FAIL quant: sha256 mismatch {e['path']}"); bad = 1
    n += 1
print(f"OK   quant manifest: {n} files re-hashed, mismatches above (if any)")
t = json.load(open(root/"trajectories/manifest.json"))
for name in t["files"]:
    if not (root/"trajectories"/name).exists(): print(f"FAIL trajectories: MISSING {name}"); bad = 1
print(f"OK   trajectories: {len(t['files'])} listed file(s) present (manifest carries NAMES only — no hashes to check)")
p = json.load(open(root/"papers/manifest.json"))
print(f"OK   papers: {len(p['papers'])} DOI pointer(s) — nothing local to hash (PDFs live at Zenodo by design)")
sys.exit(bad)
PY

printf '\n== HONEST COVERAGE — NOT verified offline by this script ==\n'
cat <<'TXT'
- khipu *.parquet DSSE receipts: per-organ ECDSA P-256 verification tooling is
  not vendored here (keys/ carries the public keys; receipt-chain-live re-walks
  the chain in-browser from the canonical HF dataset).
- khipu/lutar_lean_receipts.ndjson: cosign-KEYLESS attestations — verification
  requires the cosign toolchain plus Rekor/Fulcio (network by definition).
  When online:  python3 scripts/verify_lutar_lean_receipts.py --ndjson \
                data/khipu/lutar_lean_receipts.ndjson
- GitHub <-> HF equality: needs network by definition (hf-sync CI covers it).
- cosign/Rekor anchors (lake_index.json): need the cosign toolchain + network.
- Verifying signatures proves integrity + origin ONLY — never accuracy,
  performance, or profitability. Quant receipts are ADVISORY / PAPER-ONLY.
TXT

if [ "$FAIL" -eq 0 ]; then printf '\nRESULT: PASS — every executed offline check verified.\n'; else printf '\nRESULT: FAIL — at least one check failed (see above).\n'; fi
exit "$FAIL"
