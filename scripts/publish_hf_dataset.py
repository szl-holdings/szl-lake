#!/usr/bin/env python3
"""Atomically publish and close the SZL Lake immutable file index."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import pyarrow.parquet as parquet
from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CHECKED_INDEX = DATA / "lake_index.json"
INDEX_PATH = "lake_index.json"
REPO_ID = "SZLHOLDINGS/szl-lake"
SOURCE_REPOSITORY = "szl-holdings/szl-lake"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
RECEIPT_PATH = re.compile(
    r"^khipu/([a-z0-9_]+)_receipts\.(ndjson|parquet)$"
)
MAX_REPOSITORY_FILES = 10_000
MAX_REMOTE_FILE_BYTES = 128 * 1024 * 1024
MAX_REMOTE_TOTAL_BYTES = 512 * 1024 * 1024
RESERVED_HF_PATHS = frozenset({".gitattributes"})

SOURCE_ORIGIN = "SOURCE_CONTROLLED"
PRESERVED_ORIGIN = "PRESERVED_REMOTE_AT_IMMUTABLE_REVISION"
INFRASTRUCTURE_ORIGIN = "HF_REPOSITORY_INFRASTRUCTURE_AT_IMMUTABLE_REVISION"

INDEX_CONTRACT = {
    "index_path": INDEX_PATH,
    "self_reference": "EXCLUDED_TO_AVOID_HASH_FIXED_POINT",
    "tree_closure": "EXACT_IMMUTABLE_POST_COMMIT_TREE",
    "source_origin": SOURCE_ORIGIN,
    "preserved_origin": PRESERVED_ORIGIN,
    "infrastructure_origin": INFRASTRUCTURE_ORIGIN,
}
CHECKED_INDEX_CONTRACT = {
    "index_path": INDEX_PATH,
    "self_reference": "EXCLUDED_TO_AVOID_HASH_FIXED_POINT",
    "publication_generated_exclusions": {
        "DATASET_PROVENANCE.json": "EXCLUDED_BECAUSE_BYTES_BIND_THE_FINAL_SOURCE_REVISION"
    },
    "scope": "SOURCE_CONTROLLED_STATIC_PAYLOAD",
}
METADATA = {
    "name": "SZL Holdings Data Lake",
    "doctrine": "v11 LOCKED 749/14/163",
    "lambda": "Conjecture 1 (open)",
    "concept_doi": "10.5281/zenodo.19944926",
    "umbrella_doi": "10.5281/zenodo.20434276",
    "license": "CC-BY-4.0",
}


class PublicationError(RuntimeError):
    """Raised when exact publication or readback cannot be established."""


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def strict_json(payload: bytes, *, label: str) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PublicationError(f"{label} contains duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"{label} is not UTF-8 JSON: {exc}") from exc


def exact_revision(value: object, *, label: str) -> str:
    revision = str(value).lower()
    if FULL_SHA.fullmatch(revision) is None:
        raise PublicationError(f"{label} must be an exact 40-character Git SHA")
    return revision


def validated_repo_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise PublicationError("repository path must be a non-empty string")
    if value.startswith("/") or "\\" in value or ":" in value:
        raise PublicationError(f"repository path is not relative POSIX syntax: {value!r}")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise PublicationError(f"repository path contains a control character: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PublicationError(f"repository path contains an unsafe segment: {value!r}")
    return value


def static_source_payloads() -> dict[str, bytes]:
    fixed_paths = {
        "README.md": ROOT / "huggingface" / "README.md",
        "LICENSE": ROOT / "LICENSE",
    }
    payloads: dict[str, bytes] = {}
    for relative, path in fixed_paths.items():
        if path.is_symlink():
            raise PublicationError(
                f"source payload contains a symlink and is refused: {relative}"
            )
        payloads[relative] = path.read_bytes()
    for path in sorted(DATA.rglob("*")):
        if path.is_symlink():
            raise PublicationError(
                f"source payload contains a symlink and is refused: {path.relative_to(DATA)}"
            )
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(DATA).as_posix()
        if relative == INDEX_PATH:
            continue
        relative = validated_repo_path(relative)
        if relative in payloads:
            raise PublicationError(f"duplicate source payload path: {relative}")
        payloads[relative] = path.read_bytes()
    if len(payloads) < 3:
        raise PublicationError("source-controlled lake payload is empty")
    return payloads


def render_checked_source_index() -> bytes:
    entries = [
        {"path": path, "bytes": len(body), "sha256": sha256_bytes(body)}
        for path, body in sorted(static_source_payloads().items())
    ]
    return canonical_json({
        "schema": "szl.lake.source-index/v2",
        **METADATA,
        "contract": CHECKED_INDEX_CONTRACT,
        "indexed_file_count": len(entries),
        "indexed_bytes": sum(item["bytes"] for item in entries),
        "entries_sha256": sha256_bytes(canonical_json(entries)),
        "files": entries,
    })


def check_checked_source_index() -> dict[str, Any]:
    actual = CHECKED_INDEX.read_bytes()
    parsed = strict_json(actual, label="checked source index")
    if not isinstance(parsed, dict) or parsed.get("schema") != "szl.lake.source-index/v2":
        raise PublicationError("checked source index schema is unsupported")
    if parsed.get("contract") != CHECKED_INDEX_CONTRACT:
        raise PublicationError("checked source index contract is unsupported")
    files = parsed.get("files")
    if not isinstance(files, list):
        raise PublicationError("checked source index files must be a list")
    paths: list[str] = []
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
            raise PublicationError("checked source index entry has unknown or missing fields")
        paths.append(validated_repo_path(entry["path"]))
    if len(paths) != len(set(paths)):
        raise PublicationError("checked source index contains duplicate paths")
    expected = render_checked_source_index()
    if actual != expected:
        raise PublicationError("checked source index is stale; run --write-index")
    return parsed


def source_payloads(source_revision: str) -> dict[str, bytes]:
    source_revision = exact_revision(source_revision, label="source revision")
    check_checked_source_index()
    payloads = static_source_payloads()
    file_evidence = [
        {"path": path, "bytes": len(body), "sha256": sha256_bytes(body)}
        for path, body in sorted(payloads.items())
    ]
    provenance = {
        "schema": "szl.dataset-source-attestation/v2",
        "dataset": REPO_ID,
        "source": {
            "repository": SOURCE_REPOSITORY,
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


def repository_paths(info: object) -> list[str]:
    siblings = getattr(info, "siblings", None)
    if not isinstance(siblings, list):
        raise PublicationError("Hugging Face repository did not return a closed sibling list")
    if len(siblings) > MAX_REPOSITORY_FILES:
        raise PublicationError("Hugging Face repository file count exceeds the safety bound")
    paths: list[str] = []
    for sibling in siblings:
        raw = sibling.get("rfilename") if isinstance(sibling, dict) else getattr(sibling, "rfilename", None)
        paths.append(validated_repo_path(raw))
    if len(paths) != len(set(paths)):
        raise PublicationError("Hugging Face repository returned duplicate paths")
    return sorted(paths)


def read_remote_payload(
    downloader: Callable[..., str], *, path: str, revision: str, token: str
) -> bytes:
    local_path = Path(downloader(
        repo_id=REPO_ID, repo_type="dataset", filename=path,
        revision=revision, token=token, force_download=True,
    ))
    try:
        size = local_path.stat().st_size
    except OSError as exc:
        raise PublicationError(f"immutable remote payload is unreadable: {path}") from exc
    if size > MAX_REMOTE_FILE_BYTES:
        raise PublicationError(f"immutable remote payload exceeds the safety bound: {path}")
    body = local_path.read_bytes()
    if len(body) != size:
        raise PublicationError(f"immutable remote payload changed during read: {path}")
    return body


def capture_remote_only_payloads(
    *, remote_paths: list[str], source_paths: set[str], revision: str,
    token: str, downloader: Callable[..., str],
) -> dict[str, bytes]:
    captured: dict[str, bytes] = {}
    total = 0
    for path in remote_paths:
        if path == INDEX_PATH or path in source_paths:
            continue
        body = read_remote_payload(downloader, path=path, revision=revision, token=token)
        total += len(body)
        if total > MAX_REMOTE_TOTAL_BYTES:
            raise PublicationError("preserved remote payload exceeds the aggregate safety bound")
        captured[path] = body
    return captured


def receipt_counts(payloads: dict[str, bytes]) -> tuple[dict[str, int], dict[str, int]]:
    organ_counts: Counter[str] = Counter()
    file_counts: dict[str, int] = {}
    for path, body in sorted(payloads.items()):
        match = RECEIPT_PATH.fullmatch(path)
        if match is None:
            continue
        if match.group(2) == "parquet":
            try:
                count = parquet.read_metadata(io.BytesIO(body)).num_rows
            except Exception as exc:
                raise PublicationError(f"receipt file is not valid Parquet: {path}") from exc
        else:
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PublicationError(f"receipt file is not UTF-8: {path}") from exc
            count = 0
            for number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    receipt = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise PublicationError(
                        f"receipt file has invalid JSON at {path}:{number}"
                    ) from exc
                if not isinstance(receipt, dict):
                    raise PublicationError(f"receipt is not an object at {path}:{number}")
                count += 1
        file_counts[path] = count
        organ_counts[match.group(1)] += count
    return dict(sorted(organ_counts.items())), file_counts


def build_lake_index(
    *, source_revision: str, predecessor_revision: str,
    source: dict[str, bytes], preserved: dict[str, bytes],
) -> bytes:
    source_revision = exact_revision(source_revision, label="source revision")
    predecessor_revision = exact_revision(predecessor_revision, label="Hugging Face predecessor revision")
    normalized_source = {validated_repo_path(path): body for path, body in source.items()}
    normalized_preserved = {validated_repo_path(path): body for path, body in preserved.items()}
    if INDEX_PATH in normalized_source or INDEX_PATH in normalized_preserved:
        raise PublicationError("the generated index path cannot be supplied as an indexed payload")
    overlap = set(normalized_source) & set(normalized_preserved)
    if overlap:
        raise PublicationError(f"source and preserved payload paths overlap: {sorted(overlap)[0]}")
    entries: list[dict[str, Any]] = []
    for path, body in sorted(normalized_source.items()):
        if not isinstance(body, bytes):
            raise PublicationError(f"source payload is not bytes: {path}")
        entries.append({
            "path": path, "bytes": len(body), "sha256": sha256_bytes(body),
            "origin": SOURCE_ORIGIN, "binding_revision": source_revision,
        })
    for path, body in sorted(normalized_preserved.items()):
        if not isinstance(body, bytes):
            raise PublicationError(f"preserved payload is not bytes: {path}")
        origin = INFRASTRUCTURE_ORIGIN if path in RESERVED_HF_PATHS else PRESERVED_ORIGIN
        entries.append({
            "path": path, "bytes": len(body), "sha256": sha256_bytes(body),
            "origin": origin, "binding_revision": predecessor_revision,
        })
    entries.sort(key=lambda item: item["path"])
    combined = {**normalized_source, **normalized_preserved}
    organ_counts, receipt_file_counts = receipt_counts(combined)
    origin_counts = dict(sorted(Counter(item["origin"] for item in entries).items()))
    index = {
        "schema": "szl.lake.index/v2",
        **METADATA,
        "bindings": {
            "source_repository": SOURCE_REPOSITORY,
            "source_revision": source_revision,
            "hf_repository": REPO_ID,
            "hf_predecessor_revision": predecessor_revision,
        },
        "closure": {
            "status": "EXACT_POST_COMMIT_TREE_REQUIRED",
            "indexed_file_count": len(entries),
            "published_file_count_including_index": len(entries) + 1,
            "indexed_bytes": sum(item["bytes"] for item in entries),
            "entries_sha256": sha256_bytes(canonical_json(entries)),
            "origin_counts": origin_counts,
        },
        "self_reference": {
            "path": INDEX_PATH,
            "status": INDEX_CONTRACT["self_reference"],
            "reason": "A cryptographic hash cannot include its own final bytes without a fixed-point construction.",
        },
        "khipu_receipt_counts": organ_counts,
        "khipu_receipt_file_counts": receipt_file_counts,
        "total_khipu_receipts": sum(organ_counts.values()),
        "files": entries,
    }
    return canonical_json(index)


def publish(
    *, source_revision: str, token: str, report_path: Path,
    api: HfApi | None = None, downloader: Callable[..., str] = hf_hub_download,
) -> dict[str, Any]:
    if not token:
        raise PublicationError("HF_TOKEN is required")
    source_revision = exact_revision(source_revision, label="source revision")
    api = api or HfApi(token=token)
    source = source_payloads(source_revision)
    mutable_head = api.dataset_info(REPO_ID, files_metadata=True)
    predecessor = exact_revision(getattr(mutable_head, "sha", None), label="Hugging Face predecessor revision")
    immutable_before = api.dataset_info(REPO_ID, revision=predecessor, files_metadata=True)
    if exact_revision(getattr(immutable_before, "sha", None), label="immutable predecessor revision") != predecessor:
        raise PublicationError("immutable predecessor lookup returned a different revision")
    before_paths = repository_paths(immutable_before)
    preserved = capture_remote_only_payloads(
        remote_paths=before_paths, source_paths=set(source), revision=predecessor,
        token=token, downloader=downloader,
    )
    index_body = build_lake_index(
        source_revision=source_revision, predecessor_revision=predecessor,
        source=source, preserved=preserved,
    )
    committed_payloads = {**source, INDEX_PATH: index_body}
    commit = api.create_commit(
        repo_id=REPO_ID, repo_type="dataset", revision="main", parent_commit=predecessor,
        operations=[
            CommitOperationAdd(path_in_repo=path, path_or_fileobj=io.BytesIO(body))
            for path, body in sorted(committed_payloads.items())
        ],
        commit_message=f"Publish closed szl-lake index from {source_revision[:12]}",
    )
    revision = exact_revision(getattr(commit, "oid", None), label="Hugging Face publication revision")
    immutable_after = api.dataset_info(REPO_ID, revision=revision, files_metadata=True)
    if exact_revision(getattr(immutable_after, "sha", None), label="immutable publication revision") != revision:
        raise PublicationError("immutable publication lookup returned a different revision")
    after_paths = repository_paths(immutable_after)
    expected_paths = sorted(set(source) | set(preserved) | {INDEX_PATH})
    if after_paths != expected_paths:
        missing = sorted(set(expected_paths) - set(after_paths))
        unexpected = sorted(set(after_paths) - set(expected_paths))
        raise PublicationError(
            f"post-commit tree is not closed (missing={missing[:3]}, unexpected={unexpected[:3]})"
        )
    expected_bodies = {**source, **preserved, INDEX_PATH: index_body}
    observed: dict[str, Any] = {}
    index_entries = {item["path"]: item for item in json.loads(index_body)["files"]}
    for path, expected in sorted(expected_bodies.items()):
        body = read_remote_payload(downloader, path=path, revision=revision, token=token)
        if body != expected:
            raise PublicationError(f"immutable readback mismatch: {path}")
        evidence: dict[str, Any] = {"bytes": len(body), "sha256": sha256_bytes(body)}
        evidence["origin"] = "GENERATED_CLOSED_INDEX" if path == INDEX_PATH else index_entries[path]["origin"]
        observed[path] = evidence
    report = {
        "schema": "szl.lake-publication/v3",
        "status": "PUBLISHED_CLOSED_INDEX_AND_IMMUTABLE_READBACK_VERIFIED",
        "source_repository": SOURCE_REPOSITORY,
        "source_revision": source_revision,
        "hf_revision_before": predecessor,
        "hf_revision_after": revision,
        "files_verified": len(observed),
        "bytes_verified": sum(item["bytes"] for item in observed.values()),
        "origin_counts": dict(sorted(Counter(item["origin"] for item in observed.values()).items())),
        "index_sha256": sha256_bytes(index_body),
        "files": observed,
        "signed_release_receipt": "UNAVAILABLE_NO_APPROVED_OWNER_KEY_IN_WORKFLOW",
        "verification_scope": "complete immutable HF tree including generated index; receipt validity remains separately verified",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(canonical_json(report))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-index", action="store_true")
    modes.add_argument("--write-index", action="store_true")
    parser.add_argument("--source-revision")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.write_index:
        body = render_checked_source_index()
        CHECKED_INDEX.write_bytes(body)
        print(canonical_json({"status": "SOURCE_INDEX_WRITTEN", "sha256": sha256_bytes(body)}).decode("utf-8"), end="")
        return 0
    if args.check_index:
        index = check_checked_source_index()
        print(canonical_json({"status": "SOURCE_INDEX_CURRENT", "files": index["indexed_file_count"], "sha256": sha256_bytes(CHECKED_INDEX.read_bytes())}).decode("utf-8"), end="")
        return 0
    if not args.source_revision or args.report is None:
        parser.error("publication requires --source-revision and --report")
    result = publish(
        source_revision=args.source_revision,
        token=os.getenv("HF_TOKEN", ""),
        report_path=args.report,
    )
    print(canonical_json(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
