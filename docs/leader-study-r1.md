# szl-lake domain leader study r1 (propose-only)

Date: 2026-11-24. Method: web-verified leader landscape + governing-seat ratification (Kimi nonce T6-RULING-20261124-K1). Study pinned to szl-lake HEAD 93f4058d428059a21016dcd79bd6c3b4cbf36e28, fetched 2026-11-24. This card is documentation only; it changes no code, CI, config, or existing PR status. All absorb actions are FUTURE PROPOSALS (non-authorizing); implementation and merges remain separately reviewed and gated. Λ Conjecture 1: OPEN (advisory, never a closed theorem). No speedup, energy, or proven-trust claims; upstream performance claims are labeled as upstream claims only.

## Domain: append-only transparency lake - DSSE receipt store (GitHub origin, HF dataset mirror)

## Leaders studied (web-verified 2026-11-24)
1. Rekor (https://github.com/sigstore/rekor) - moat: Sigstore transparency log implementation (RFC 6962-based): append-only Merkle tree with signed tree heads, inclusion proofs, and independent monitors.
2. RFC 6962 (https://www.rfc-editor.org/rfc/rfc6962) - moat: the Certificate Transparency standard defining append-only Merkle logs, consistency proofs between signed tree heads, and monitors/auditors.
3. DSSE (https://github.com/secure-systems-lab/dsse) - moat: envelope + pre-authentication-encoding (PAE) signing standard used by in-toto for artifact attestation; deliberately small trusted surface.

## Absorb actions (FUTURE PROPOSALS, non-authorizing)
1. (Rekor) Propose a design note mapping lake receipt publication to transparency-log inclusion-proof semantics, with explicit separation between GitHub origin and HF dataset mirror verification duties. Cost: documentation and schema-mapping review only; limit: no new log service, no receipt mutation, and no claim that inclusion proofs already exist in szl-lake.
2. (RFC 6962) Propose a future consistency-proof review between successive lake states (signed tree head equivalents), including monitor and auditor responsibilities for detecting forked or rewritten history. Cost: protocol analysis and threat-model review; limit: advisory design only, with append-only history preserved and no weakening of existing receipt validation.
3. (DSSE) Propose preserving byte-exact envelope + PAE verification with an explicit key registry and rotation receipts, documenting canonicalization boundaries and failure handling for invalid signatures. Cost: verification-spec and key-lifecycle documentation; limit: no cryptographic implementation, no bypass of the signature wall, and no interoperability claim without later tested evidence.
4. (Baseline) Propose recording the non-negotiable preservation baseline: append-only receipts, dedupe against landed cards, stop on duplicate or conflicting findings, head-sha-pinned evidence, and verbatim corrected leader URLs. Cost: maintainer review of this card and future proposals; limit: this card authorizes no merge, implementation, integration, deployment, or changes to excluded lanes.

## Do not do (per governing seat)

Do not merge, bypass the signature wall, or claim implementation beyond this card.
