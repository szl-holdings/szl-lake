#!/usr/bin/env python3
"""Regenerate data/quant/manifest.json after a ledger mirror.

Idempotent by design: timestamps only move when CONTENT moved, so a no-drift
run leaves the file byte-identical and the sync workflow no-ops cleanly.
Run from the repo root. LEDGER_SHA + VERIFIER_PIN come from the environment
(set by sync-quant-ledger.yml); both are required when ledger content exists.
"""
import hashlib, json, os, sys
from datetime import datetime, timezone

ROOT = "data/quant"
MANIFEST = os.path.join(ROOT, "manifest.json")

def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()

def build_files():
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
            if rel == "manifest.json":
                continue
            out.append({"path": rel, "sha256": sha256(full), "bytes": os.path.getsize(full)})
    out.sort(key=lambda e: e["path"])
    return out

def main():
    with open(MANIFEST) as f:
        old = json.load(f)
    new = dict(old)
    new["files"] = build_files()
    ledger_files = [e for e in new["files"] if e["path"].startswith("ledger/")]
    genesis_receipts = [e for e in new["files"]
                        if "/" not in e["path"] and e["path"].endswith(".receipt.json")]
    if ledger_files:
        ledger_sha = os.environ.get("LEDGER_SHA", "")
        verifier_pin = os.environ.get("VERIFIER_PIN", "")
        if not (len(ledger_sha) == 40 and len(verifier_pin) == 40):
            print("ERROR: LEDGER_SHA / VERIFIER_PIN must be 40-hex when ledger/ exists", file=sys.stderr)
            return 1
        runs = sorted({e["path"].split("/")[1] for e in ledger_files
                       if e["path"].count("/") >= 2})
        new["ledger"] = {
            "source_branch": "szl-holdings/szl-quant@ledger",
            "ledger_commit": ledger_sha,
            "verifier_pin": verifier_pin,
            "runs": len(runs),
            "files": len(ledger_files),
            "mirrored_at_utc": old.get("ledger", {}).get("mirrored_at_utc", ""),
            "verification": (
                "every run dir re-verified (DSSE PAE + ed25519) and the full hash "
                "chain walked in lake CI against data/quant/keys/engine_pubkey.json "
                "BEFORE this mirror was committed; a verification failure aborts the "
                "sync. The mirror follows the ledger branch tip -- ledger_commit "
                "records the exact tree mirrored. Honest limit (verifier's own note, "
                "kept verbatim): wholesale deletion of the newest link(s) needs "
                "external witnesses (Actions logs, INDEX git history)."
            ),
        }
    new["counts"] = {"genesis_receipts": len(genesis_receipts), "ledger_files": len(ledger_files)}

    def norm(m):
        c = json.loads(json.dumps(m))
        c.pop("generated_at_utc", None)
        if "ledger" in c:
            c["ledger"].pop("mirrored_at_utc", None)
        return c

    if norm(new) == norm(old):
        print("manifest: no content change; left byte-identical")
        return 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new["generated_at_utc"] = now
    if "ledger" in new:
        new["ledger"]["mirrored_at_utc"] = now
    with open(MANIFEST, "w") as f:
        json.dump(new, f, indent=2)
        f.write("\n")
    print(f"manifest: regenerated ({len(new['files'])} files, "
          f"{new['counts']['ledger_files']} ledger, ts {now})")
    return 0

if __name__ == "__main__":
    sys.exit(main())
