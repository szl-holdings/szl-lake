"""Offline regression coverage for malformed receipt sink configuration."""
import unittest
from unittest.mock import patch

import szl_lake_client as client


class RequestConstructionFailureTests(unittest.TestCase):
    def check_malformed_url(self, emitter, body):
        # A malformed URL must fail before transport. No receipt is posted.
        with patch.object(client.urllib.request, "urlopen") as transport:
            transport.side_effect = AssertionError("network is forbidden")
            result = emitter(body, base_url="not-a-url")
            transport.assert_not_called()
        self.assertIs(result["ok"], False)
        self.assertTrue(result["reason"].startswith("unexpected:"), result)
        self.assertIn("unknown url type", result["reason"])

    def test_single_receipt_request_construction_returns_failure(self):
        self.check_malformed_url(client.emit_receipt, {})

    def test_batch_request_construction_returns_failure(self):
        self.check_malformed_url(client.emit_batch, [])


class SerializationDepthFailureTests(unittest.TestCase):
    def check_deep_receipt(self, emitter, *, batch=False):
        receipt = {"synthetic": True}
        # Exceed both Python recursion and the C encoder's separate stack
        # limit on the supported interpreters without using external data.
        for _ in range(10_000):
            receipt = {"nested": receipt}
        payload = [receipt] if batch else receipt
        with patch.object(client.urllib.request, "urlopen") as transport:
            transport.side_effect = AssertionError("network is forbidden")
            result = emitter(payload, base_url="https://example.invalid")
            transport.assert_not_called()
        self.assertIs(result["ok"], False)
        self.assertTrue(
            result["reason"].startswith("receipt not JSON-serializable:"), result
        )

    def test_single_receipt_excessive_depth_returns_failure(self):
        self.check_deep_receipt(client.emit_receipt)

    def test_batch_receipt_excessive_depth_returns_failure(self):
        self.check_deep_receipt(client.emit_batch, batch=True)


if __name__ == "__main__":
    unittest.main()
