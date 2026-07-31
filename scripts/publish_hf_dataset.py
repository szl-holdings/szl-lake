#!/usr/bin/env python3
"""Atomically publish and verify the source-controlled SZL Lake payload."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
from pathlib import Path
from typing import Any

from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REPO_ID = "SZLHOLDINGS/szl-lake"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class PublicationError(RuntimeError):
    """Raised when exact publication or readback cannot be established."""


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def source_payloads(source_revision: str) -> dict[str, bytes]:
    source_revision = source_revision.lower()
    if FULL_SHA.fullmatch(source_revision) is None:
        raise PublicationError("source revision must be an exact 40-character Git SHA")
    payloads: dict[str, bytes] = {
        "README.md": (ROOT / "huggingface" / "README.md").read_bytes(),
        "LICENSE": (ROOT / "LICENSE").read_bytes(),
    }
    for path in sorted(DATA.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            payloads[path.relative_to(DATA).as_posix()] = path.read_bytes()
    if len(payloads) < 3:
        raise PublicationError("source-controlled lake payload is empty")
    file_evidence = [
        {"path": path, "bytes": len(body), "sha256": sha256_bytes(body)}
        for path, body in sorted(payloads.items())
    ]
    provenance = {
        "schema": "szl.dataset-source-attestation/v2",
        "dataset": REPO_ID,
        "source": {
            "repository": "szl-holdings/szl-lake",
            "revision": source_revision,
            "relation": "CANONICAL_SOURCE_CONTROLLED_PAYLOAD",
        },
        "license": {
            "spdx": "CC-BY-4.0",
            "file": "LICENSE",
            "sha256": sha256_bytes(payloads["LICENSE"]),
            "scope": "source-controlled dataset payload",
        },
        "payload": {
            "files": file_evidence,
            "file_count": len(file_evidence),
            "bytes": sum(item["bytes"] for item in file_evidence),
        },
        "claims": {
            "source_binding": "EXACT_GIT_REVISION",
            "publication_readback": "REQUIRED_BEFORE_SUCCESS",
            "receipt_signature_validity": "SEPARATE_PER_RECEIPT_VERIFIERS",
            "receipt_truth_or_accuracy": "NOT_CLAIMED",
            "reproducible_dataset_bytes": "EXACT_FROM_SOURCE_REVISION",
        },
    }
    payloads["DATASET_PROVENANCE.json"] = canonical_json(provenance)
    return payloads


def publish(
    *, source_revision: str, token: str, report_path: Path, api: HfApi | None = None
) -> dict[str, Any]:
    if not token:
        raise PublicationError("HF_TOKEN is required")
    api = api or HfApi(token=token)
    payloads = source_payloads(source_revision)
    before = api.dataset_info(REPO_ID, files_metadata=True)
    commit = api.create_commit(
        repo_id=REPO_ID,
        repo_type="dataset",
        operations=[
            CommitOperationAdd(path_in_repo=path, path_or_fileobj=io.BytesIO(body))
            for path, body in payloads.items()
        ],
        commit_message=f"Publish exact szl-lake payload from {source_revision[:12]}",
    )
    revision = getattr(commit, "oid", None) or api.dataset_info(REPO_ID).sha
    if FULL_SHA.fullmatch(str(revision)) is None:
        raise PublicationError("publication did not return an immutable revision")
    observed: dict[str, Any] = {}
    for path, expected in payloads.items():
        body = Path(
            hf_hub_download(
                repo_id=REPO_ID,
                repo_type="dataset",
                filename=path,
                revision=revision,
                token=token,
                force_download=True,
            )
        ).read_bytes()
        if body != expected:
            raise PublicationError(f"immutable readback mismatch: {path}")
        observed[path] = {"bytes": len(body), "sha256": sha256_bytes(body)}
    report = {
        "schema": "szl.lake-publication/v2",
        "status": "PUBLISHED_AND_IMMUTABLE_READBACK_VERIFIED",
        "source_repository": "szl-holdings/szl-lake",
        "source_revision": source_revision,
        "hf_revision_before": before.sha,
        "hf_revision_after": revision,
        "files_verified": len(observed),
        "bytes_verified": sum(item["bytes"] for item in observed.values()),
        "files": observed,
        "signed_release_receipt": "UNAVAILABLE_NO_APPROVED_OWNER_KEY_IN_WORKFLOW",
        "verification_scope": "source-controlled payload bytes; receipt validity remains separately verified",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(canonical_json(report))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = publish(
        source_revision=args.source_revision,
        token=os.getenv("HF_TOKEN", ""),
        report_path=args.report,
    )
    print(canonical_json(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
