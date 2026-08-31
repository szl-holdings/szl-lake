import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as parquet

import publish_hf_dataset as publisher


class FakeAdd:
    def __init__(self, *, path_in_repo: str, path_or_fileobj: io.BytesIO) -> None:
        self.path_in_repo = path_in_repo
        self.body = path_or_fileobj.read()


class FakeApi:
    before = "1" * 40
    after = "2" * 40

    def __init__(
        self,
        *,
        unexpected_after: str | None = None,
        missing_after: str | None = None,
        return_oid: object = None,
    ) -> None:
        self.files_by_revision = {
            self.before: {
                ".gitattributes": b"*.parquet filter=lfs\n",
                "README.md": b"old card",
                "LICENSE": b"old license",
                "lake_index.json": b'{"schema":"stale"}\n',
                "legacy/evidence.json": b'{"preserved":true}\n',
            }
        }
        self.unexpected_after = unexpected_after
        self.missing_after = missing_after
        self.return_oid = self.after if return_oid is None else return_oid
        self.create_kwargs = None

    @staticmethod
    def _info(revision: str, files: dict[str, bytes]) -> SimpleNamespace:
        return SimpleNamespace(
            sha=revision,
            siblings=[SimpleNamespace(rfilename=path) for path in sorted(files)],
        )

    def dataset_info(
        self, repo_id: str, *, revision: str | None = None, files_metadata: bool = False
    ) -> SimpleNamespace:
        assert repo_id == publisher.REPO_ID
        assert files_metadata
        selected = self.before if revision is None else revision
        return self._info(selected, self.files_by_revision[selected])

    def create_commit(self, **kwargs: object) -> SimpleNamespace:
        self.create_kwargs = kwargs
        assert kwargs["repo_id"] == publisher.REPO_ID
        assert kwargs["repo_type"] == "dataset"
        assert kwargs["revision"] == "main"
        assert kwargs["parent_commit"] == self.before
        files = dict(self.files_by_revision[self.before])
        for operation in kwargs["operations"]:
            assert isinstance(operation, FakeAdd)
            files[operation.path_in_repo] = operation.body
        if self.unexpected_after is not None:
            files[self.unexpected_after] = b"drift"
        if self.missing_after is not None:
            files.pop(self.missing_after, None)
        self.files_by_revision[self.after] = files
        return SimpleNamespace(oid=self.return_oid)


class FakeDownloader:
    def __init__(self, api: FakeApi, *, tamper_after: str | None = None) -> None:
        self.api = api
        self.tamper_after = tamper_after
        self.temp = tempfile.TemporaryDirectory()
        self.calls: list[tuple[str, str]] = []

    def close(self) -> None:
        self.temp.cleanup()

    def __call__(self, **kwargs: object) -> str:
        assert kwargs["repo_id"] == publisher.REPO_ID
        assert kwargs["repo_type"] == "dataset"
        assert kwargs["force_download"] is True
        revision = str(kwargs["revision"])
        filename = str(kwargs["filename"])
        body = self.api.files_by_revision[revision][filename]
        if revision == self.api.after and filename == self.tamper_after:
            body += b"tampered"
        target = Path(self.temp.name) / str(len(self.calls)) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        self.calls.append((revision, filename))
        return str(target)


class PublishDatasetTests(unittest.TestCase):
    @staticmethod
    def parquet_receipts(count: int) -> bytes:
        body = io.BytesIO()
        parquet.write_table(pa.table({"id": list(range(count))}), body)
        return body.getvalue()

    def test_source_payload_is_complete_hash_bound_and_licensed(self) -> None:
        payloads = publisher.source_payloads("a" * 40)
        self.assertIn("README.md", payloads)
        self.assertIn("LICENSE", payloads)
        self.assertIn("DATASET_PROVENANCE.json", payloads)
        self.assertIn("khipu/amaru_receipts.parquet", payloads)
        self.assertNotIn("lake_index.json", payloads)
        provenance = json.loads(payloads["DATASET_PROVENANCE.json"])
        self.assertEqual(provenance["source"]["revision"], "a" * 40)
        self.assertEqual(provenance["license"]["spdx"], "CC-BY-4.0")
        self.assertEqual(
            provenance["claims"]["reproducible_dataset_bytes"],
            "EXACT_FROM_SOURCE_REVISION",
        )
        declared = {item["path"]: item for item in provenance["payload"]["files"]}
        self.assertEqual(set(declared), set(payloads) - {"DATASET_PROVENANCE.json"})
        for path, body in payloads.items():
            if path == "DATASET_PROVENANCE.json":
                continue
            self.assertEqual(declared[path]["sha256"], publisher.sha256_bytes(body))

    def test_invalid_source_revision_fails_closed(self) -> None:
        with self.assertRaisesRegex(publisher.PublicationError, "exact 40-character"):
            publisher.source_payloads("main")

    def test_publish_requires_token(self) -> None:
        with self.assertRaisesRegex(publisher.PublicationError, "HF_TOKEN"):
            publisher.publish(
                source_revision="a" * 40,
                token="",
                report_path=publisher.ROOT / "reports" / "unused.json",
            )

    def test_symlink_payload_is_refused_before_read(self) -> None:
        original = Path.is_symlink

        def report_one_link(path: Path) -> bool:
            return path.name == "amaru_receipts.parquet" or original(path)

        with mock.patch.object(Path, "is_symlink", report_one_link):
            with self.assertRaisesRegex(publisher.PublicationError, "symlink"):
                publisher.source_payloads("a" * 40)

    def test_card_and_license_symlinks_are_refused_before_read(self) -> None:
        original = Path.is_symlink
        for linked_name in ("README.md", "LICENSE"):
            with self.subTest(linked_name=linked_name):
                def report_fixed_link(path: Path, name: str = linked_name) -> bool:
                    return path.name == name or original(path)

                with mock.patch.object(Path, "is_symlink", report_fixed_link):
                    with self.assertRaisesRegex(publisher.PublicationError, "symlink"):
                        publisher.source_payloads("a" * 40)

    def test_repo_path_rejects_traversal_absolute_backslash_and_controls(self) -> None:
        for value in (
            "../x", "a/../x", "/x", "C:/x", r"a\x", "a//x", "a/./x", "a\x00b"
        ):
            with self.subTest(value=value):
                with self.assertRaises(publisher.PublicationError):
                    publisher.validated_repo_path(value)

    def test_repository_tree_rejects_duplicates_and_missing_siblings(self) -> None:
        duplicate = SimpleNamespace(
            siblings=[
                SimpleNamespace(rfilename="a.json"),
                SimpleNamespace(rfilename="a.json"),
            ]
        )
        with self.assertRaisesRegex(publisher.PublicationError, "duplicate"):
            publisher.repository_paths(duplicate)
        with self.assertRaisesRegex(publisher.PublicationError, "closed sibling"):
            publisher.repository_paths(SimpleNamespace(siblings=None))

    def test_build_index_is_deterministic_closed_and_origin_bound(self) -> None:
        source = {
            "README.md": b"card",
            "khipu/lutar_lean_receipts.ndjson": b'{"id":1}\n{"id":2}\n',
            "khipu/sentra_receipts.parquet": self.parquet_receipts(2),
        }
        preserved = {
            ".gitattributes": b"lfs",
            "khipu/amaru_receipts.ndjson": b'{"id":3}\n',
            "khipu/amaru_receipts.parquet": self.parquet_receipts(3),
            "legacy/evidence.json": b"legacy",
        }
        first = publisher.build_lake_index(
            source_revision="a" * 40,
            predecessor_revision="b" * 40,
            source=source,
            preserved=preserved,
        )
        second = publisher.build_lake_index(
            source_revision="a" * 40,
            predecessor_revision="b" * 40,
            source=dict(reversed(list(source.items()))),
            preserved=dict(reversed(list(preserved.items()))),
        )
        self.assertEqual(first, second)
        index = json.loads(first)
        self.assertEqual(index["schema"], "szl.lake.index/v2")
        self.assertEqual(index["closure"]["indexed_file_count"], 7)
        self.assertEqual(index["closure"]["published_file_count_including_index"], 8)
        self.assertEqual(index["self_reference"]["path"], "lake_index.json")
        self.assertNotIn("lake_index.json", {item["path"] for item in index["files"]})
        entries = {item["path"]: item for item in index["files"]}
        self.assertEqual(entries["README.md"]["origin"], publisher.SOURCE_ORIGIN)
        self.assertEqual(
            entries["legacy/evidence.json"]["origin"], publisher.PRESERVED_ORIGIN
        )
        self.assertEqual(
            entries[".gitattributes"]["origin"], publisher.INFRASTRUCTURE_ORIGIN
        )
        self.assertEqual(
            index["khipu_receipt_counts"],
            {"amaru": 4, "lutar_lean": 2, "sentra": 2},
        )
        self.assertEqual(index["total_khipu_receipts"], 8)

    def test_malformed_parquet_receipts_fail_before_publication(self) -> None:
        with self.assertRaisesRegex(publisher.PublicationError, "valid Parquet"):
            publisher.build_lake_index(
                source_revision="a" * 40,
                predecessor_revision="b" * 40,
                source={"khipu/a_receipts.parquet": b"not-parquet"},
                preserved={},
            )

    def test_build_index_rejects_generated_path_overlap_and_non_bytes(self) -> None:
        with self.assertRaisesRegex(publisher.PublicationError, "generated index"):
            publisher.build_lake_index(
                source_revision="a" * 40,
                predecessor_revision="b" * 40,
                source={"lake_index.json": b"x"},
                preserved={},
            )
        with self.assertRaisesRegex(publisher.PublicationError, "overlap"):
            publisher.build_lake_index(
                source_revision="a" * 40,
                predecessor_revision="b" * 40,
                source={"x": b"x"},
                preserved={"x": b"x"},
            )
        with self.assertRaisesRegex(publisher.PublicationError, "not bytes"):
            publisher.build_lake_index(
                source_revision="a" * 40,
                predecessor_revision="b" * 40,
                source={"x": "not bytes"},
                preserved={},
            )

    def test_malformed_receipts_fail_before_publication(self) -> None:
        for body in (b"not-json\n", b"[]\n", b"\xff"):
            with self.subTest(body=body):
                with self.assertRaises(publisher.PublicationError):
                    publisher.build_lake_index(
                        source_revision="a" * 40,
                        predecessor_revision="b" * 40,
                        source={"khipu/a_receipts.ndjson": body},
                        preserved={},
                    )

    def test_checked_source_index_rejects_duplicate_keys_and_staleness(self) -> None:
        static = {"README.md": b"card", "LICENSE": b"license", "data.json": b"{}\n"}
        valid = None
        with mock.patch.object(publisher, "static_source_payloads", return_value=static):
            valid = publisher.render_checked_source_index()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lake_index.json"
            path.write_bytes(b'{"schema":"one","schema":"two"}\n')
            with mock.patch.object(publisher, "CHECKED_INDEX", path):
                with self.assertRaisesRegex(publisher.PublicationError, "duplicate JSON key"):
                    publisher.check_checked_source_index()
            path.write_bytes(valid)
            with mock.patch.object(publisher, "CHECKED_INDEX", path), mock.patch.object(
                publisher,
                "static_source_payloads",
                return_value={**static, "data.json": b'{"changed":true}\n'},
            ):
                with self.assertRaisesRegex(publisher.PublicationError, "stale"):
                    publisher.check_checked_source_index()

    def test_checked_source_index_declares_both_circular_exclusions(self) -> None:
        index = json.loads(publisher.render_checked_source_index())
        self.assertEqual(
            index["contract"]["self_reference"],
            "EXCLUDED_TO_AVOID_HASH_FIXED_POINT",
        )
        self.assertEqual(
            set(index["contract"]["publication_generated_exclusions"]),
            {"DATASET_PROVENANCE.json"},
        )
        self.assertNotIn("lake_index.json", {item["path"] for item in index["files"]})

    def test_publish_uses_parent_cas_adds_only_and_verifies_closed_tree(self) -> None:
        api = FakeApi()
        downloader = FakeDownloader(api)
        source = {
            "README.md": b"new card",
            "LICENSE": b"new license",
            "DATASET_PROVENANCE.json": b'{"source":"exact"}\n',
            "khipu/lutar_lean_receipts.ndjson": b'{"id":1}\n',
        }
        try:
            with tempfile.TemporaryDirectory() as directory:
                report_path = Path(directory) / "report.json"
                with mock.patch.object(publisher, "CommitOperationAdd", FakeAdd), mock.patch.object(
                    publisher, "source_payloads", return_value=source
                ):
                    report = publisher.publish(
                        source_revision="a" * 40,
                        token="token",
                        report_path=report_path,
                        api=api,
                        downloader=downloader,
                    )
                self.assertEqual(
                    report["status"],
                    "PUBLISHED_CLOSED_INDEX_AND_IMMUTABLE_READBACK_VERIFIED",
                )
                self.assertEqual(report["hf_revision_before"], api.before)
                self.assertEqual(report["hf_revision_after"], api.after)
                self.assertEqual(set(report["files"]), set(api.files_by_revision[api.after]))
                self.assertEqual(json.loads(report_path.read_bytes()), report)
                operations = api.create_kwargs["operations"]
                self.assertTrue(operations)
                self.assertTrue(all(isinstance(item, FakeAdd) for item in operations))
                self.assertNotIn("legacy/evidence.json", {item.path_in_repo for item in operations})
                self.assertEqual(
                    api.files_by_revision[api.after]["legacy/evidence.json"],
                    api.files_by_revision[api.before]["legacy/evidence.json"],
                )
                index = json.loads(api.files_by_revision[api.after]["lake_index.json"])
                self.assertEqual(
                    {item["path"] for item in index["files"]},
                    set(api.files_by_revision[api.after]) - {"lake_index.json"},
                )
        finally:
            downloader.close()

    def test_publish_rejects_unexpected_post_commit_path(self) -> None:
        api = FakeApi(unexpected_after="raced/new.json")
        downloader = FakeDownloader(api)
        try:
            with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                publisher, "CommitOperationAdd", FakeAdd
            ), mock.patch.object(
                publisher,
                "source_payloads",
                return_value={"README.md": b"new", "LICENSE": b"license"},
            ):
                with self.assertRaisesRegex(publisher.PublicationError, "not closed"):
                    publisher.publish(
                        source_revision="a" * 40,
                        token="token",
                        report_path=Path(directory) / "report.json",
                        api=api,
                        downloader=downloader,
                    )
        finally:
            downloader.close()

    def test_publish_rejects_missing_post_commit_path(self) -> None:
        api = FakeApi(missing_after="legacy/evidence.json")
        downloader = FakeDownloader(api)
        try:
            with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                publisher, "CommitOperationAdd", FakeAdd
            ), mock.patch.object(
                publisher,
                "source_payloads",
                return_value={"README.md": b"new", "LICENSE": b"license"},
            ):
                with self.assertRaisesRegex(publisher.PublicationError, "not closed"):
                    publisher.publish(
                        source_revision="a" * 40,
                        token="token",
                        report_path=Path(directory) / "report.json",
                        api=api,
                        downloader=downloader,
                    )
        finally:
            downloader.close()

    def test_publish_rejects_immutable_readback_mismatch(self) -> None:
        api = FakeApi()
        downloader = FakeDownloader(api, tamper_after="README.md")
        try:
            with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                publisher, "CommitOperationAdd", FakeAdd
            ), mock.patch.object(
                publisher,
                "source_payloads",
                return_value={"README.md": b"new", "LICENSE": b"license"},
            ):
                with self.assertRaisesRegex(publisher.PublicationError, "readback mismatch"):
                    publisher.publish(
                        source_revision="a" * 40,
                        token="token",
                        report_path=Path(directory) / "report.json",
                        api=api,
                        downloader=downloader,
                    )
        finally:
            downloader.close()

    def test_publish_rejects_missing_immutable_commit_oid(self) -> None:
        api = FakeApi(return_oid="")
        downloader = FakeDownloader(api)
        try:
            with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                publisher, "CommitOperationAdd", FakeAdd
            ), mock.patch.object(
                publisher,
                "source_payloads",
                return_value={"README.md": b"new", "LICENSE": b"license"},
            ):
                with self.assertRaisesRegex(publisher.PublicationError, "publication revision"):
                    publisher.publish(
                        source_revision="a" * 40,
                        token="token",
                        report_path=Path(directory) / "report.json",
                        api=api,
                        downloader=downloader,
                    )
        finally:
            downloader.close()


if __name__ == "__main__":
    unittest.main()
