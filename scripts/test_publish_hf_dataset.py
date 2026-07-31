import json
import unittest

import publish_hf_dataset as publisher


class PublishDatasetTests(unittest.TestCase):
    def test_source_payload_is_complete_hash_bound_and_licensed(self) -> None:
        payloads = publisher.source_payloads("a" * 40)
        self.assertIn("README.md", payloads)
        self.assertIn("LICENSE", payloads)
        self.assertIn("DATASET_PROVENANCE.json", payloads)
        self.assertIn("khipu/amaru_receipts.parquet", payloads)
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


if __name__ == "__main__":
    unittest.main()
