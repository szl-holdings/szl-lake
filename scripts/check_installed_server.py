"""Exercise the installed serve extra using only a temporary synthetic ledger.

Run with ``python -I`` outside the source checkout. No provider, credentials,
real receipts, or network listener are used.
"""
from __future__ import annotations

import importlib
from importlib.metadata import distribution
from pathlib import Path
import sys
import tempfile

from fastapi.testclient import TestClient


def main() -> None:
    if not sys.flags.isolated:
        raise RuntimeError("run this check with python -I to exclude checkout imports")
    installed = distribution("szl-lake")
    for name in (
        "szl_lake_client", "szl_lake_query", "szl_lake_receipt",
        "szl_lake_server", "szl_lake_store",
    ):
        module = importlib.import_module(name)
        expected = Path(installed.locate_file(name + ".py")).resolve()
        assert Path(module.__file__).resolve() == expected, name
        assert expected.is_relative_to(Path(sys.prefix).resolve()), name

    from szl_lake_server import API_PREFIX, app
    from szl_lake_store import ReceiptLedger

    fixture = {
        "id": "installed-package-synthetic-1", "organ": "package-smoke",
        "ts": "2026-01-01T00:00:00Z", "decision": "TEST",
        "synthetic": True, "signed": False,
    }
    with tempfile.TemporaryDirectory(prefix="szl-lake-package-") as root:
        app.state.ledger = ReceiptLedger(root)
        try:
            with TestClient(app) as client:
                assert client.get(API_PREFIX + "/health").status_code == 200
                response = client.post(API_PREFIX + "/receipts", json=fixture)
                assert response.status_code == 200, response.text
                assert response.json()["accepted"] is True
                first_head = response.json()["chain_head"]

                # Reopen the on-disk store, then prove persistence and deduplication.
                app.state.ledger = ReceiptLedger(root)
                response = client.get(API_PREFIX + "/receipts", params={"organ": fixture["organ"]})
                assert response.status_code == 200, response.text
                assert response.json()["count"] == 1
                assert response.json()["results"][0]["receipt_id"] == fixture["id"]
                duplicate = client.post(API_PREFIX + "/receipts", json=fixture)
                assert duplicate.status_code == 200, duplicate.text
                assert duplicate.json()["duplicate"] is True
                verified = client.get(API_PREFIX + "/chain/verify", params={"organ": fixture["organ"]})
                assert verified.status_code == 200, verified.text
                assert verified.json()["ok"] is True
                assert verified.json()["count"] == 1
                assert verified.json()["chain_head"] == first_head

                # Damage only the disposable fixture; the installed HTTP API must fail closed.
                partition = Path(root) / fixture["organ"] / "2026-01-01.ndjson"
                damaged = partition.read_bytes() + b"not-json\n"
                partition.write_bytes(damaged)
                response = client.get(API_PREFIX + "/receipts", params={"organ": fixture["organ"]})
                assert response.status_code == 503, response.text
                assert response.json()["error"] == "ledger_corrupted"
                assert partition.read_bytes() == damaged
        finally:
            app.state.ledger = None
    print("installed serve extra: imports, HTTP ingest/query, persistence, deduplication, chain verification, corruption rejection passed")


if __name__ == "__main__":
    main()
