"""Offline checks: HTTP success alone is not a receipt acknowledgment."""
import copy
import hashlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import szl_lake_client as client

RECEIPT = {"id": "fixture-1", "organ": "test", "value": "λ"}


def digest(receipt):
    return hashlib.sha3_256(json.dumps(receipt, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def acknowledgment(receipt=RECEIPT, duplicate=False):
    identity = next((receipt[k] for k in ("id", "hash", "receipt_id")
                     if isinstance(receipt.get(k), str) and receipt[k]), digest(receipt))
    return {"accepted": not duplicate, "duplicate": duplicate, "receipt_id": identity,
            "receipt_body_hash": digest(receipt), "organ": receipt.get("organ", "unknown").strip().lower(),
            "chain_index": 1, "ledger_offset": 1, "chain_head": "a" * 64}


class Response(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json"}
    def geturl(self):
        return "https://example.invalid/api/lake/v1/receipts"


class AcknowledgmentTests(unittest.TestCase):
    def emit(self, payload, receipts=None, status=200, content_type="application/json"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        response = Response(raw)
        response.status = status
        response.headers = {"Content-Type": content_type}
        with patch.object(client.urllib.request, "urlopen", return_value=response) as direct:
            with patch.object(client.urllib.request, "build_opener",
                              return_value=SimpleNamespace(open=direct)):
                result = (client.emit_receipt(RECEIPT, base_url="https://example.invalid")
                          if receipts is None else client.emit_batch(receipts, base_url="https://example.invalid"))
        return result

    def test_html_success_is_not_delivery(self):
        self.assertIs(self.emit(b"<html>login</html>", content_type="text/html")["ok"], False)

    def test_json_error_is_not_delivery(self):
        self.assertIs(self.emit({"error": "rejected"})["ok"], False)

    def test_empty_success_is_not_delivery(self):
        self.assertIs(self.emit(b"", status=204)["ok"], False)

    def test_unrelated_json_is_not_delivery(self):
        self.assertIs(self.emit({"ok": True})["ok"], False)

    def test_wrong_receipt_id_is_refused(self):
        self.assertIs(self.emit({**acknowledgment(), "receipt_id": "other"})["ok"], False)

    def test_wrong_body_hash_is_refused(self):
        self.assertIs(self.emit({**acknowledgment(), "receipt_body_hash": "b" * 64})["ok"], False)

    def test_explicit_unknown_state_cannot_override_receipt_failure(self):
        for state in ("UNKNOWN", "REJECTED", "LOCAL_DUPLICATE", None):
            with self.subTest(state=state):
                self.assertIs(self.emit({**acknowledgment(), "state": state})["ok"], False)
        self.assertIs(self.emit({**acknowledgment(), "state": "LOCAL_ACKNOWLEDGED"})["ok"], True)
        self.assertIs(self.emit({**acknowledgment(duplicate=True), "state": "LOCAL_DUPLICATE"})["ok"], True)

    def test_wrong_organ_is_refused(self):
        self.assertIs(self.emit({**acknowledgment(), "organ": "other"})["ok"], False)

    def test_boolean_chain_index_is_refused(self):
        self.assertIs(self.emit({**acknowledgment(), "chain_index": True})["ok"], False)

    def test_invalid_chain_head_is_refused(self):
        self.assertIs(self.emit({**acknowledgment(), "chain_head": "not-a-hash"})["ok"], False)

    def test_conflicting_acceptance_flags_are_refused(self):
        self.assertIs(self.emit({**acknowledgment(), "duplicate": True})["ok"], False)

    def test_partial_batch_is_not_success(self):
        payload = {"accepted": 1, "duplicates": 0, "total": 1, "results": [acknowledgment()]}
        self.assertIs(self.emit(payload, receipts=[RECEIPT, RECEIPT])["ok"], False)

    def test_batch_totals_must_match_item_results(self):
        payload = {"accepted": 2, "duplicates": 0, "total": 2,
                   "results": [acknowledgment(), acknowledgment(duplicate=True)]}
        self.assertIs(self.emit(payload, receipts=[RECEIPT, RECEIPT])["ok"], False)

    def test_duplicate_json_members_are_refused(self):
        raw = json.dumps(acknowledgment()).encode()
        raw = b'{"accepted": false,' + raw[1:]
        self.assertIs(self.emit(raw)["ok"], False)

    def test_nonfinite_response_is_refused(self):
        for raw in (b'{"x": NaN}', b'{"x": 1e999}'):
            with self.subTest(raw=raw):
                self.assertIs(self.emit(raw)["ok"], False)

    def test_response_size_is_bounded(self):
        self.assertIs(self.emit(b" " * 1048577)["ok"], False)

    def test_valid_acknowledgment_is_scoped(self):
        result = self.emit(acknowledgment())
        self.assertIs(result["ok"], True)
        self.assertEqual(result.get("delivery_state"), "SINK_ACKNOWLEDGED")
        self.assertIs(result.get("signature_verified"), False)

    def test_duplicate_acknowledgment_is_success(self):
        self.assertIs(self.emit(acknowledgment(duplicate=True))["ok"], True)

    def test_one_item_ndjson_accepts_single_server_envelope(self):
        self.assertIs(self.emit(acknowledgment(), receipts=[RECEIPT])["ok"], True)

    def test_complete_batch_accepts_exact_item_bindings(self):
        payload = {"accepted": 1, "duplicates": 1, "total": 2,
                   "results": [acknowledgment(), acknowledgment(duplicate=True)]}
        self.assertIs(self.emit(payload, receipts=[RECEIPT, RECEIPT])["ok"], True)

    def test_request_snapshot_cannot_be_changed_by_transport(self):
        rec = copy.deepcopy(RECEIPT)
        raw = json.dumps(acknowledgment(rec)).encode()
        def transport(*args, **kwargs):
            rec["value"] = "mutated-after-serialization"
            return Response(raw)
        with patch.object(client.urllib.request, "urlopen", side_effect=transport) as direct:
            with patch.object(client.urllib.request, "build_opener", return_value=SimpleNamespace(open=direct)):
                result = client.emit_receipt(rec, base_url="https://example.invalid")
        self.assertIs(result["ok"], True)



class TransportBoundaryTests(unittest.TestCase):
    def test_invalid_timeout_and_receipt_never_send(self):
        for timeout in (True, 0, -1, float("nan"), float("inf"), "invalid"):
            with self.subTest(timeout=timeout), patch.object(client.urllib.request, "build_opener") as transport:
                result = client.emit_receipt(RECEIPT, base_url="https://example.invalid", timeout=timeout)
                self.assertIs(result["ok"], False)
                self.assertEqual(result["delivery_state"], "NOT_SENT")
                transport.assert_not_called()
        for receipts in ([], [{"value": float("nan")}], ["not-an-object"]):
            with self.subTest(receipts=receipts), patch.object(client.urllib.request, "build_opener") as transport:
                result = client.emit_batch(receipts, base_url="https://example.invalid")
                self.assertIs(result["ok"], False)
                self.assertEqual(result["delivery_state"], "NOT_SENT")
                transport.assert_not_called()

    def test_timeout_configuration_does_not_break_import(self):
        import importlib.util
        import os
        with patch.dict(os.environ, {"SZL_RECEIPT_TIMEOUT": "not-a-number"}):
            spec = importlib.util.spec_from_file_location("isolated_lake_client", client.__file__)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with patch.object(module.urllib.request, "build_opener") as transport:
                result = module.emit_receipt(RECEIPT, base_url="https://example.invalid")
                self.assertIs(result["ok"], False)
                self.assertEqual(result["delivery_state"], "NOT_SENT")
                transport.assert_not_called()

    def test_redirect_does_not_forward_or_replay(self):
        from email.message import Message
        from urllib.response import addinfourl
        for status in (301, 302, 303, 307, 308):
            calls = []
            class SyntheticHTTP(client.urllib.request.HTTPHandler):
                def http_open(self, req):
                    calls.append(req.full_url)
                    headers = Message()
                    headers["Location"] = "http://other.invalid/receive"
                    response = addinfourl(io.BytesIO(b""), headers, req.full_url, status)
                    response.msg = "Redirect"
                    return response
            opener = client.urllib.request.build_opener(client._NoRedirect(), SyntheticHTTP())
            req = client.urllib.request.Request("http://example.invalid/receipt", data=b"private-fixture", method="POST")
            with self.subTest(status=status), self.assertRaises(client.urllib.error.HTTPError):
                opener.open(req)
            self.assertEqual(calls, ["http://example.invalid/receipt"])

    def test_timeout_after_submission_is_unknown_not_not_sent(self):
        with patch.object(client.urllib.request, "build_opener") as builder:
            builder.return_value.open.side_effect = TimeoutError()
            result = client.emit_receipt(RECEIPT, base_url="https://example.invalid")
        self.assertIs(result["ok"], False)
        self.assertEqual(result["delivery_state"], "UNKNOWN")
        builder.return_value.open.assert_called_once()


class EndpointFormsTests(unittest.TestCase):
    def test_origin_is_completed_once(self):
        for base in ('https://example.invalid', 'https://example.invalid/'):
            with self.subTest(base=base):
                self.assertEqual(client._endpoint(base), 'https://example.invalid/api/lake/v1/receipts')

    def test_documented_api_base_is_not_duplicated(self):
        for base in ('https://example.invalid/api/lake/v1', 'https://example.invalid/api/lake/v1/'):
            with self.subTest(base=base):
                self.assertEqual(client._endpoint(base), 'https://example.invalid/api/lake/v1/receipts')

    def test_exact_ingest_url_is_not_duplicated(self):
        for base in ('https://example.invalid/api/lake/v1/receipts', 'https://example.invalid/api/lake/v1/receipts/'):
            with self.subTest(base=base):
                self.assertEqual(client._endpoint(base), 'https://example.invalid/api/lake/v1/receipts')

    def test_reverse_proxy_prefix_is_preserved(self):
        for path in ('/proxy', '/proxy/api/lake/v1', '/proxy/api/lake/v1/receipts'):
            with self.subTest(path=path):
                self.assertEqual(client._endpoint('https://example.invalid'+path),
                                 'https://example.invalid/proxy/api/lake/v1/receipts')


if __name__ == "__main__":
    unittest.main()
