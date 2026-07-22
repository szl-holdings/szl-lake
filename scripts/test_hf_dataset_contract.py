#!/usr/bin/env python3
"""Validate the source-controlled Hugging Face card and viewer contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

import pyarrow.dataset as ds
import pyarrow.parquet as pq
import yaml


ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "huggingface" / "README.md"
DATA_ROOT = ROOT / "data"
EMPTY_MANIFEST = DATA_ROOT / "khipu" / "EMPTY_CHAIN_MANIFEST.json"


def _metadata() -> dict:
    text = CARD.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise AssertionError("Hugging Face card must start with YAML frontmatter")
    frontmatter, _body = text[4:].split("\n---\n", 1)
    return yaml.safe_load(frontmatter)


def _size_category(rows: int) -> str:
    limits = [
        (1_000, "n<1K"),
        (10_000, "1K<n<10K"),
        (100_000, "10K<n<100K"),
        (1_000_000, "100K<n<1M"),
        (10_000_000, "1M<n<10M"),
        (100_000_000, "10M<n<100M"),
        (1_000_000_000, "100M<n<1B"),
    ]
    for limit, label in limits:
        if rows < limit:
            return label
    return "n>1B"


class HuggingFaceDatasetContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.metadata = _metadata()
        configs = cls.metadata.get("configs", [])
        cls.receipts = next(
            config for config in configs if config.get("config_name") == "receipts"
        )
        declared_path = cls.receipts["data_files"][0]["path"]
        cls.parquet_files = sorted(DATA_ROOT.glob(declared_path))

    def test_viewer_uses_only_the_homogeneous_receipt_parquets(self) -> None:
        self.assertEqual(
            self.receipts["data_files"],
            [{"split": "train", "path": "khipu/*_receipts.parquet"}],
        )
        self.assertEqual(len(self.parquet_files), 5)
        self.assertTrue(all(path.suffix == ".parquet" for path in self.parquet_files))

        schemas = [pq.ParquetFile(path).schema_arrow for path in self.parquet_files]
        self.assertTrue(all(schema.equals(schemas[0]) for schema in schemas[1:]))

        table = ds.dataset(self.parquet_files, format="parquet").to_table()
        expected_rows = sum(
            pq.ParquetFile(path).metadata.num_rows for path in self.parquet_files
        )
        self.assertEqual(table.num_rows, expected_rows)

    def test_size_category_matches_viewer_rows(self) -> None:
        rows = sum(pq.ParquetFile(path).metadata.num_rows for path in self.parquet_files)
        self.assertEqual(self.metadata.get("size_categories"), [_size_category(rows)])
        self.assertNotEqual(self.metadata.get("size_categories"), ["100M<n<1B"])

    def test_empty_chain_manifest_covers_exact_zero_row_placeholders(self) -> None:
        manifest = json.loads(EMPTY_MANIFEST.read_text(encoding="utf-8"))
        artifacts = manifest["artifacts"]
        declared = {item["path"]: item for item in artifacts}
        actual_empty = {
            path.relative_to(DATA_ROOT).as_posix()
            for path in self.parquet_files
            if pq.ParquetFile(path).metadata.num_rows == 0
        }
        self.assertEqual(set(declared), actual_empty)
        self.assertEqual(len({item["sha256"] for item in artifacts}), 1)

        for relative_path, item in declared.items():
            path = DATA_ROOT / relative_path
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(item["rows"], 0)
            self.assertIs(item["retained"], True)
            self.assertEqual(item["sha256"], digest)


if __name__ == "__main__":
    unittest.main(verbosity=2)

