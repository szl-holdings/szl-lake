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


if __name__ == "__main__":
    unittest.main()
