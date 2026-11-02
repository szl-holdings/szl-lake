"""Offline receipt-body integrity controls; no server or optional imports."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from szl_lake_store import (  # noqa: E402
    CHAIN_HASH,
    LEGACY_SCHEMA,
    SCHEMA,
    ReceiptIdentityConflict,
    ReceiptLedger,
    _link_hash,
    canonical_hash,
)


@pytest.fixture
def stored_receipt(tmp_path):
    def create(identity_key=None):
        ledger = ReceiptLedger(str(tmp_path / 'ledger'))
        receipt = {
            'organ': 'audit',
            'ts': '2026-09-30T00:00:00Z',
            'decision': 'ADMIT',
        }
        if identity_key:
            receipt[identity_key] = 'synthetic-receipt'
        ledger.append(receipt)
        path = next((tmp_path / 'ledger' / 'audit').glob('*.ndjson'))
        envelope = json.loads(path.read_text(encoding='utf-8'))
        return ledger, path, envelope
    return create


def rewrite(path, envelopes):
    path.write_text(
        ''.join(json.dumps(envelope) + '\n' for envelope in envelopes),
        encoding='utf-8',
    )


@pytest.mark.parametrize('identity_key', [None, 'id', 'hash', 'receipt_id'])
def test_valid_receipt_objects_still_verify(stored_receipt, identity_key):
    ledger, _, envelope = stored_receipt(identity_key)
    result = ledger.verify_chain('audit')
    assert result['ok'] is True
    assert result['broken'] == []
    assert result['chain_head'] == envelope['chain_hash']
    assert result['count'] == result['chain_index'] == 1


@pytest.mark.parametrize('identity_key', [None, 'id'])
@pytest.mark.parametrize('replacement', [None, [], 'receipt', False, 0, 1.5])
def test_nonobject_body_rejects_unchanged_link(
        stored_receipt, identity_key, replacement):
    ledger, path, envelope = stored_receipt(identity_key)
    envelope['receipt'] = replacement
    rewrite(path, [envelope])
    result = ledger.verify_chain('audit')
    assert result['ok'] is False
    assert result['broken'] == [{
        'position': 1,
        'kind': 'malformed_receipt',
        'detail': 'stored receipt body must be a JSON object',
    }]
    # No link field changed; a head-only comparison cannot detect this loss.
    assert result['chain_head'] == envelope['chain_hash']
    assert result['count'] == result['chain_index'] == 1


@pytest.mark.parametrize('identity_key', [None, 'id'])
def test_missing_body_rejects_unchanged_link(stored_receipt, identity_key):
    ledger, path, envelope = stored_receipt(identity_key)
    del envelope['receipt']
    rewrite(path, [envelope])
    result = ledger.verify_chain('audit')
    assert result['ok'] is False
    assert [finding['kind'] for finding in result['broken']] == [
        'malformed_receipt',
    ]
    assert result['chain_head'] == envelope['chain_hash']


def test_empty_object_and_empty_chain_remain_valid(tmp_path):
    ledger = ReceiptLedger(str(tmp_path / 'ledger'))
    empty = ledger.verify_chain('unknown')
    assert empty['ok'] is True
    assert empty['count'] == 0
    assert empty['chain_head'] is None
    ledger.append({})
    assert ledger.verify_chain('unknown')['ok'] is True


def test_idless_body_edit_still_rejects(stored_receipt):
    ledger, path, envelope = stored_receipt()
    envelope['receipt']['decision'] = 'REJECT'
    rewrite(path, [envelope])
    result = ledger.verify_chain('audit')
    assert result['ok'] is False
    assert [finding['kind'] for finding in result['broken']] == [
        'receipt_id_mismatch', 'receipt_body_hash_mismatch',
    ]


def test_v2_explicit_identity_commits_complete_body(stored_receipt):
    ledger, path, envelope = stored_receipt('id')
    assert envelope['schema'] == SCHEMA
    assert envelope['receipt_body_hash'] == canonical_hash(envelope['receipt'])
    envelope['receipt']['decision'] = 'REJECT'
    rewrite(path, [envelope])
    result = ledger.verify_chain('audit')
    assert result['ok'] is False
    assert [finding['kind'] for finding in result['broken']] == [
        'receipt_body_hash_mismatch',
    ]
    # The committed head remains stable while readback exposes the body edit.
    assert result['chain_head'] == envelope['chain_hash']


def test_same_identity_different_body_is_not_acknowledged(tmp_path):
    ledger = ReceiptLedger(str(tmp_path / 'ledger'))
    original = {
        'id': 'same-id',
        'organ': 'audit',
        'ts': '2026-09-30T00:00:00Z',
        'decision': 'ADMIT',
    }
    first = ledger.append(original)
    path = next((tmp_path / 'ledger' / 'audit').glob('*.ndjson'))
    before = path.read_bytes()

    changed = dict(original, decision='REJECT')
    with pytest.raises(ReceiptIdentityConflict) as caught:
        ledger.append(changed)
    assert caught.value.receipt_id == 'same-id'
    assert caught.value.expected_hash == first['receipt_body_hash']
    assert caught.value.received_hash == canonical_hash(changed)
    assert path.read_bytes() == before

    duplicate = ledger.append(original)
    assert duplicate['accepted'] is False
    assert duplicate['duplicate'] is True
    assert duplicate['receipt_body_hash'] == first['receipt_body_hash']


def test_append_response_and_query_readback_share_body_hash(tmp_path):
    ledger = ReceiptLedger(str(tmp_path / 'ledger'))
    receipt = {
        'id': 'readback-id',
        'organ': 'audit',
        'ts': '2026-09-30T00:00:00Z',
        'source_commit': 'a' * 40,
        'artifact_digest': 'sha256:' + 'b' * 64,
    }
    accepted = ledger.append(receipt)
    [stored] = ledger.query(organ='audit')

    assert accepted['receipt_body_hash'] == canonical_hash(receipt)
    assert stored['receipt_body_hash'] == accepted['receipt_body_hash']
    assert stored['receipt'] == receipt
    assert ledger.verify_chain('audit')['ok'] is True


def test_legacy_v1_chain_replays_and_extends_with_v2(tmp_path):
    root = tmp_path / 'ledger'
    organ_dir = root / 'audit'
    organ_dir.mkdir(parents=True)
    receipt = {
        'id': 'legacy-id',
        'organ': 'audit',
        'ts': '2026-09-30T00:00:00Z',
        'decision': 'ADMIT',
    }
    chain_hash = _link_hash(
        None, receipt['id'], receipt['organ'], receipt['ts'], 1)
    legacy = {
        'schema': LEGACY_SCHEMA,
        'chain_alg': CHAIN_HASH,
        'organ': receipt['organ'],
        'receipt_id': receipt['id'],
        'prev_hash': None,
        'chain_hash': chain_hash,
        'chain_index': 1,
        'ts': receipt['ts'],
        'ingested_at': receipt['ts'],
        'energy': {'label': 'UNAVAILABLE'},
        'receipt': receipt,
    }
    rewrite(organ_dir / '2026-09-30.ndjson', [legacy])

    ledger = ReceiptLedger(str(root))
    assert ledger.verify_chain('audit')['ok'] is True
    second = ledger.append({
        'id': 'v2-id',
        'organ': 'audit',
        'ts': '2026-09-30T00:00:01Z',
        'decision': 'ADMIT',
    })
    assert second['chain_index'] == 2
    stored = ledger.query(organ='audit')
    assert [row['schema'] for row in stored] == [SCHEMA, LEGACY_SCHEMA]
    assert ledger.verify_chain('audit')['ok'] is True


def test_malformed_body_collects_other_findings_and_later_entries(stored_receipt):
    ledger, path, first = stored_receipt()
    ledger.append({'organ': 'audit', 'ts': first['ts'], 'id': 'second'})
    second = json.loads(path.read_text(encoding='utf-8').splitlines()[1])
    first['receipt'] = None
    first['ts'] = '1999-01-01T00:00:00Z'
    rewrite(path, [first, second])
    result = ledger.verify_chain('audit')
    assert result['ok'] is False
    assert {(f['position'], f['kind']) for f in result['broken']} == {
        (1, 'malformed_receipt'), (1, 'chain_hash_mismatch'),
    }
    assert result['count'] == result['chain_index'] == 2
    assert result['chain_head'] == second['chain_hash']
