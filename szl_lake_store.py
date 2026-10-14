"""
szl_lake_store.py — Unified Receipt Ledger durable store
Doctrine v11 LOCKED 749/14/163 | kernel c7c0ba17 | Λ = Conjecture 1 (OPEN)

The one durable store every SZL component appends governance receipts to.

Root cause this fixes: each organ (ouroboros, hatun-mcp, szl-router, szl-mesh,
vsp-otel, szl-trust, a11oy …) previously held receipts in an in-process silo
that never converged. This module is a real, restart-durable, append-only
ledger — NOT an in-memory map that resets.

Storage format (stdlib-only, no parquet/pyarrow dependency):
  NDJSON, one signed-receipt envelope per line, partitioned on disk by
      <root>/<organ>/<YYYY-MM-DD>.ndjson
  append-only — exactly the shape the szl-lake README documents as canonical
  for khipu/ receipt streams.

Khipu hash-chain (F4 / F22 — Khipu = Conjecture 2, ADVISORY BFT, NOT a proven
theorem): per organ, each stored envelope links prev_hash -> chain_hash where

    chain_hash = SHA3-256( canonical_json({
        "prev_hash":   <prev chain_hash or null>,
        "receipt_id":  <this receipt's identity>,
        "organ":       <organ>,
        "ts":          <receipt timestamp>,
        "chain_index": <1-based position in this organ's chain>,
    }) )

so the per-organ chain head is a single value any other component can fetch and
compare for cross-component verification. SHA3-256 is the locked Khipu hash.

Honest labels only: if a receipt carries no measured energy (e.g. NVML/joules
absent) the envelope stores energy as {"label": "UNAVAILABLE"} — joules are
NEVER fabricated.

Recovery contract (fail closed). In-memory state is only ever derived from
on-disk records that passed validation: every persisted line must parse to a
v1 envelope for this organ, carry the next contiguous ``chain_index``, link
``prev_hash`` to the previous record's ``chain_hash``, re-derive its own
``chain_hash``, and bind ``receipt_id`` to its stored body. The first record
that fails (including an unterminated trailing record left by a torn write)
puts that organ into a read-only corrupted state: ``append``/``chain_head``
raise ``LedgerCorruptionError`` naming the file, line and reason, and
``health`` reports it. Nothing is healed, truncated or deleted — the corrupt
bytes stay exactly as found for an operator to inspect. ``reload()`` re-runs
recovery from disk.

Writer contract (multi-process). Each organ has an OS-level exclusive lock
file, ``<root>/<organ>/.writer.lock`` (``fcntl.flock`` on POSIX,
``msvcrt.locking`` on Windows). ``append`` holds it across re-reading and
validating the on-disk head, writing the record and fsync, so independent
ReceiptLedger instances and separate processes sharing one root serialize
onto one chain instead of forking it. Stored order always equals chain order:
a receipt whose ts falls before the organ's newest partition is appended to
that newest partition (its ts is stored unchanged). The lock is advisory, so
it only serializes writers that use this module.

stdlib-only. Apache-2.0 — SZL Holdings 2026.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

if os.name == "nt":  # pragma: no cover - exercised on Windows hosts
    import msvcrt
else:
    import fcntl

# Khipu locked hash: SHA3-256 (advisory BFT chain — Conjecture 2, not proven).
CHAIN_HASH = "sha3_256"
SCHEMA = "szl.lake.receipt/v1"
# Filesystem-safe organ name: lowercase alnum, dash, underscore, dot. Anything
# else is rejected (prevents path traversal — organ becomes a directory name).
_ORGAN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

DEFAULT_ROOT = os.environ.get("SZL_LAKE_DIR", "khipu")
# Per-organ writer lock file. Not a partition (only *.ndjson files are).
LOCK_FILENAME = ".writer.lock"
DEFAULT_LOCK_TIMEOUT = 30.0
_O_BINARY = getattr(os, "O_BINARY", 0)


class LedgerCorruptionError(RuntimeError):
    """A persisted record failed recovery validation; the organ is read-only.

    ``path``/``line`` locate the first bad line (1-based within its partition
    file), ``position`` is the chain position it should have held, ``reason``
    is a stable kind (``unparseable_line``, ``unterminated_record``,
    ``malformed_envelope``, ``organ_mismatch``, ``index_discontinuity``,
    ``prev_hash_mismatch``, ``chain_hash_mismatch``, ``malformed_receipt``,
    ``receipt_id_mismatch``, ``duplicate_receipt_id``, and from ``query`` only
    ``truncated_after_validation``). The bytes on disk are
    left untouched.
    """

    def __init__(self, organ: str, path: str, line: int, position: int,
                 reason: str, detail: str, file: str) -> None:
        self.organ = organ
        self.path = path
        self.file = file
        self.line = line
        self.position = position
        self.reason = reason
        self.detail = detail
        super().__init__(
            f"ledger organ {organ!r} is corrupted at {file}:{line} "
            f"({reason}: {detail}); appends are refused and the on-disk "
            f"records are left untouched")

    def as_dict(self) -> dict:
        return {
            "organ": self.organ,
            "file": self.file,
            "line": self.line,
            "position": self.position,
            "reason": self.reason,
            "detail": self.detail,
        }


class LedgerLockTimeout(TimeoutError):
    """The per-organ writer lock was not acquired within ``lock_timeout``."""


def _try_lock(fd: int) -> bool:
    if os.name == "nt":  # pragma: no cover - exercised on Windows hosts
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLK):
                return False
            raise
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows hosts
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _append_durably(path: str, data: bytes) -> None:
    """Append ``data`` to ``path`` and fsync before returning."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_BINARY,
                 0o666)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _short(value: Any, limit: int = 80) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_json(obj: Any) -> bytes:
    """Compact, sorted-key JSON bytes — the canonical form for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _sha3_256(b: bytes) -> str:
    return hashlib.sha3_256(b).hexdigest()


def canonical_hash(obj: Any) -> str:
    """SHA3-256 over canonical JSON of obj."""
    return _sha3_256(canonical_json(obj))


def _link_hash(prev_hash: Any, receipt_id: Any, organ: Any, ts: Any,
               chain_index: Any) -> str:
    """The Khipu link hash documented in the module docstring."""
    return canonical_hash({
        "prev_hash": prev_hash,
        "receipt_id": receipt_id,
        "organ": organ,
        "ts": ts,
        "chain_index": chain_index,
    })


def normalize_organ(organ: Any) -> str:
    """Lowercase + validate an organ label for safe use as a directory name."""
    if not isinstance(organ, str):
        raise ValueError(f"organ must be a string, got {type(organ).__name__}")
    name = organ.strip().lower()
    if not _ORGAN_RE.match(name):
        raise ValueError(
            f"invalid organ name {organ!r}: must match {_ORGAN_RE.pattern}")
    return name


def receipt_identity(receipt: dict) -> str:
    """Stable dedupe identity for a receipt.

    Prefers the receipt's own id/hash (the DSSE receipt shape uses 'id' or
    'hash'); falls back to a content hash so an id-less receipt is still
    deduplicated by content rather than silently duplicated.
    """
    for key in ("id", "hash", "receipt_id"):
        val = receipt.get(key)
        if isinstance(val, str) and val:
            return val
    return canonical_hash(receipt)


def normalize_energy(receipt: dict) -> Any:
    """Honest energy label. Never fabricate joules.

    Returns the receipt's energy as-is when it carries a real measurement;
    otherwise {"label": "UNAVAILABLE"} (NVML/joules absent).
    """
    energy = receipt.get("energy", None)
    if energy is None:
        return {"label": "UNAVAILABLE"}
    if isinstance(energy, (int, float)):
        return {"label": "MEASURED", "joules": energy}
    if isinstance(energy, dict):
        # Already labelled? trust an explicit honest label; otherwise, if it
        # carries a numeric joules reading, mark MEASURED, else UNAVAILABLE.
        if energy.get("label"):
            return energy
        joules = energy.get("joules")
        if isinstance(joules, (int, float)):
            return {"label": "MEASURED", "joules": joules}
        return {"label": "UNAVAILABLE"}
    return {"label": "UNAVAILABLE"}


def _receipt_ts(receipt: dict) -> str:
    """Best-effort receipt timestamp (ISO string). Falls back to ingest time."""
    for key in ("ts", "timestamp", "time", "created_at"):
        val = receipt.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, (int, float)):
            # epoch seconds
            return datetime.fromtimestamp(val, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ")
    return _now_iso()


def _partition_date(ts: str) -> str:
    """YYYY-MM-DD partition key derived from an ISO timestamp (UTC)."""
    try:
        cleaned = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class _OrganState:
    """In-memory mirror of one organ's VALIDATED on-disk chain."""

    __slots__ = ("seen", "chain_index", "chain_head", "count", "files",
                 "tail", "corruption")

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.chain_index: int = 0
        self.chain_head: str | None = None
        self.count: int = 0
        # partition basename -> [validated byte offset, validated line count],
        # in stored (sorted) order.
        self.files: dict[str, list[int]] = {}
        # (basename, byte offset, raw bytes) of the head record, re-read under
        # the writer lock before the cached state is trusted again.
        self.tail: tuple[str, int, bytes] | None = None
        self.corruption: LedgerCorruptionError | None = None


class ReceiptLedger:
    """Durable, append-only, hash-chained receipt ledger.

    Thread-safe within a process and serialized across processes by a
    per-organ OS lock file. Survives process restarts because all state is
    reconstructed from the on-disk NDJSON partitions — there is no
    authoritative in-memory map that could reset — and that reconstruction
    validates every record and fails closed on the first bad one (see the
    module docstring).
    """

    def __init__(self, root: str | None = None, *,
                 lock_timeout: float = DEFAULT_LOCK_TIMEOUT) -> None:
        self.root = os.path.abspath(root or DEFAULT_ROOT)
        self.lock_timeout = float(lock_timeout)
        self._lock = threading.RLock()
        self._states: dict[str, _OrganState] = {}

    # ---- paths ----------------------------------------------------------
    def _organ_dir(self, organ: str) -> str:
        return os.path.join(self.root, organ)

    def _partition_path(self, organ: str, date: str) -> str:
        return os.path.join(self._organ_dir(organ), f"{date}.ndjson")

    def _iter_partition_files(self, organ: str) -> list[str]:
        d = self._organ_dir(organ)
        if not os.path.isdir(d):
            return []
        files = [os.path.join(d, f) for f in os.listdir(d)
                 if f.endswith(".ndjson")]
        # date-named files sort chronologically as strings
        return sorted(files)

    def _list_organs(self) -> list[str]:
        if not os.path.isdir(self.root):
            return []
        return sorted(
            name for name in os.listdir(self.root)
            if os.path.isdir(os.path.join(self.root, name))
            and _ORGAN_RE.match(name)
        )

    # ---- cross-process writer lock --------------------------------------
    @contextlib.contextmanager
    def _organ_lock(self, organ: str, *, write: bool) -> Iterator[str]:
        """Hold the organ's exclusive OS lock.

        Yields ``"held"``; for readers it may instead yield ``"absent"`` (the
        organ directory does not exist, so there is nothing on disk to read)
        or ``"unlockable"`` (the lock file cannot be opened, e.g. a read-only
        root that no writer of this module ever wrote). Writers create the
        organ directory and lock file; readers never create the directory.
        """
        d = self._organ_dir(organ)
        path = os.path.join(d, LOCK_FILENAME)
        fd: int | None = None
        if write:
            os.makedirs(d, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o666)
        elif not os.path.isdir(d):
            yield "absent"
            return
        else:
            for flags in (os.O_RDWR | os.O_CREAT, os.O_RDONLY):
                try:
                    fd = os.open(path, flags | _O_BINARY, 0o666)
                    break
                except OSError:
                    continue
        if fd is None:
            yield "unlockable"
            return
        try:
            deadline = time.monotonic() + self.lock_timeout
            delay = 0.001
            while not _try_lock(fd):
                if time.monotonic() >= deadline:
                    raise LedgerLockTimeout(
                        f"writer lock for organ {organ!r} not acquired within "
                        f"{self.lock_timeout:g}s ({path})")
                time.sleep(delay)
                delay = min(delay * 2, 0.05)
            try:
                yield "held"
            finally:
                _unlock(fd)
        finally:
            os.close(fd)

    # ---- state recovery (validating replay) -----------------------------
    def _cache_is_current_prefix(self, organ: str, st: _OrganState,
                                 listing: list[tuple[str, str, int]]) -> bool:
        """True when disk is a pure append-extension of what st validated.

        Earlier partitions must be unchanged in size, the last validated
        partition may only have grown, and the validated head record must
        still be byte-identical on disk. Anything else forces a full replay.
        """
        cached = list(st.files.items())
        if len(listing) < len(cached):
            return False
        for i, (name, (offset, _)) in enumerate(cached):
            disk_name, _, disk_size = listing[i]
            if disk_name != name:
                return False
            if disk_size < offset or (i < len(cached) - 1
                                      and disk_size != offset):
                return False
        if st.tail is not None:
            name, start, raw = st.tail
            with open(os.path.join(self._organ_dir(organ), name), "rb") as fh:
                fh.seek(start)
                if fh.read(len(raw)) != raw:
                    return False
        return True

    def _admit(self, st: _OrganState, organ: str,
               body: bytes) -> tuple[str, str] | None:
        """Validate one persisted record against st; admit it or say why not."""
        try:
            env = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError) as exc:
            return "unparseable_line", _short(str(exc), 120)
        if not isinstance(env, dict):
            return "unparseable_line", "record is not a JSON object"

        if env.get("schema") != SCHEMA:
            return "malformed_envelope", (
                f"schema must be {SCHEMA!r}, stored {_short(env.get('schema'))}")
        if env.get("chain_alg") != CHAIN_HASH:
            return "malformed_envelope", (
                f"chain_alg must be {CHAIN_HASH!r}, stored "
                f"{_short(env.get('chain_alg'))}")
        if env.get("organ") != organ:
            return "organ_mismatch", (
                f"record organ {_short(env.get('organ'))} stored under "
                f"{organ!r}")
        rid = env.get("receipt_id")
        ts = env.get("ts")
        ci = env.get("chain_index")
        prev = env.get("prev_hash")
        stored_hash = env.get("chain_hash")
        if not isinstance(rid, str) or not rid:
            return "malformed_envelope", "receipt_id must be a non-empty string"
        if not isinstance(ts, str):
            return "malformed_envelope", "ts must be a string"
        if type(ci) is not int:
            return "malformed_envelope", (
                f"chain_index must be an integer, stored {_short(ci)}")
        if prev is not None and not isinstance(prev, str):
            return "malformed_envelope", "prev_hash must be a string or null"
        if not isinstance(stored_hash, str):
            return "malformed_envelope", "chain_hash must be a string"

        expected = st.chain_index + 1
        if ci != expected:
            return "index_discontinuity", f"expected {expected}, stored {ci}"
        if prev != st.chain_head:
            return "prev_hash_mismatch", (
                f"expected {_short(st.chain_head)}, stored {_short(prev)}")
        if _link_hash(prev, rid, organ, ts, ci) != stored_hash:
            return "chain_hash_mismatch", "recomputed link hash != stored"
        receipt = env.get("receipt")
        if not isinstance(receipt, dict):
            return "malformed_receipt", (
                "stored receipt body must be a JSON object")
        try:
            body_id = receipt_identity(receipt)
        except Exception:  # noqa: BLE001
            body_id = None
        if body_id != rid:
            return "receipt_id_mismatch", (
                "stored receipt body no longer hashes to receipt_id")
        if rid in st.seen:
            return "duplicate_receipt_id", (
                f"receipt_id {_short(rid)} already appears earlier in the chain")

        st.seen.add(rid)
        st.chain_index = ci
        st.chain_head = stored_hash
        st.count += 1
        return None

    def _replay(self, organ: str, st: _OrganState,
                listing: list[tuple[str, str, int]]) -> None:
        """Validate every not-yet-validated byte of every partition, in order.

        Stops at the first bad record and records it on ``st.corruption``.
        """
        for name, path, _ in listing:
            pos, lineno = st.files.get(name, (0, 0))
            with open(path, "rb") as fh:
                fh.seek(pos)
                for raw in iter(fh.readline, b""):
                    if not raw.endswith(b"\n"):
                        if raw.strip():
                            st.files[name] = [pos, lineno]
                            self._mark_corrupt(
                                st, organ, path, lineno + 1,
                                "unterminated_record",
                                f"{len(raw)} trailing byte(s) with no newline "
                                f"(torn or partial write)")
                            return
                        pos += len(raw)  # trailing whitespace: not a record
                        break
                    body = raw.strip()
                    if body:
                        problem = self._admit(st, organ, body)
                        if problem is not None:
                            st.files[name] = [pos, lineno]
                            self._mark_corrupt(st, organ, path, lineno + 1,
                                               *problem)
                            return
                        st.tail = (name, pos, raw)
                    pos += len(raw)
                    lineno += 1
            st.files[name] = [pos, lineno]

    def _mark_corrupt(self, st: _OrganState, organ: str, path: str, line: int,
                      reason: str, detail: str) -> None:
        rel = os.path.relpath(path, self.root).replace(os.sep, "/")
        st.corruption = LedgerCorruptionError(
            organ=organ, path=path, line=line, position=st.chain_index + 1,
            reason=reason, detail=detail, file=rel)

    def _refresh_locked(self, organ: str) -> _OrganState:
        """Bring the organ's state up to date with disk (lock already held).

        Validates only what this instance has not yet validated (e.g. records
        another writer appended); falls back to a full validating replay when
        disk is not a pure extension of the cached view. A recorded
        corruption is sticky until ``reload()``.
        """
        st = self._states.get(organ)
        if st is not None and st.corruption is not None:
            return st
        listing = [(os.path.basename(p), p, os.path.getsize(p))
                   for p in self._iter_partition_files(organ)]
        if st is None or not self._cache_is_current_prefix(organ, st, listing):
            st = _OrganState()
        self._replay(organ, st, listing)
        self._states[organ] = st
        return st

    def _state(self, organ: str) -> _OrganState:
        with self._lock, self._organ_lock(organ, write=False) as lock:
            if lock == "absent":
                # Nothing on disk. Do not scan: a writer may be creating the
                # directory right now, outside any lock this reader holds.
                self._states.pop(organ, None)
                return _OrganState()
            return self._refresh_locked(organ)

    @staticmethod
    def _raise_if_corrupt(st: _OrganState) -> None:
        if st.corruption is not None:
            raise st.corruption.with_traceback(None)

    # ---- write ----------------------------------------------------------
    def append(self, receipt: dict) -> dict:
        """Append a single receipt. Idempotent on its id/hash.

        Returns {accepted, duplicate, receipt_id, ledger_offset, chain_index,
        chain_head, organ}. Raises LedgerCorruptionError (nothing written)
        when the organ's persisted chain fails validation, and
        LedgerLockTimeout when the writer lock is not acquired in time.
        """
        if not isinstance(receipt, dict):
            raise ValueError("receipt must be a JSON object")
        organ = normalize_organ(receipt.get("organ", "unknown"))
        rid = receipt_identity(receipt)
        ts = _receipt_ts(receipt)

        with self._lock, self._organ_lock(organ, write=True):
            # Re-read and validate the on-disk head under the writer lock, so
            # the record below always extends the chain that is on disk now.
            st = self._refresh_locked(organ)
            self._raise_if_corrupt(st)

            if rid in st.seen:
                return {
                    "accepted": False,
                    "duplicate": True,
                    "receipt_id": rid,
                    "organ": organ,
                    "ledger_offset": st.count,
                    "chain_index": st.chain_index,
                    "chain_head": st.chain_head,
                }

            chain_index = st.chain_index + 1
            prev_hash = st.chain_head
            chain_hash = _link_hash(prev_hash, rid, organ, ts, chain_index)

            envelope = {
                "schema": SCHEMA,
                "chain_alg": CHAIN_HASH,
                "organ": organ,
                "receipt_id": rid,
                "prev_hash": prev_hash,
                "chain_hash": chain_hash,
                "chain_index": chain_index,
                "ts": ts,
                "ingested_at": _now_iso(),
                "energy": normalize_energy(receipt),
                "receipt": receipt,
            }

            # Never write behind the newest partition: stored order must stay
            # equal to chain order for recovery and verify_chain to replay it.
            name = os.path.basename(
                self._partition_path(organ, _partition_date(ts)))
            newest = next(reversed(st.files), None)
            if newest is not None and newest > name:
                name = newest
            line = json.dumps(envelope, sort_keys=True,
                              separators=(",", ":")) + "\n"
            _append_durably(os.path.join(self._organ_dir(organ), name),
                            line.encode("utf-8"))
            # st is not advanced here: the next refresh validates this record
            # from disk like any other writer's, so state never runs ahead of
            # what is durably on disk.

            return {
                "accepted": True,
                "duplicate": False,
                "receipt_id": rid,
                "organ": organ,
                "ledger_offset": st.count + 1,
                "chain_index": chain_index,
                "chain_head": chain_hash,
            }

    def append_batch(self, receipts: Iterable[dict]) -> dict:
        """Append many receipts (e.g. NDJSON batch). Per-receipt idempotent."""
        results = []
        accepted = duplicates = 0
        with self._lock:
            for rec in receipts:
                r = self.append(rec)
                results.append(r)
                if r["accepted"]:
                    accepted += 1
                elif r["duplicate"]:
                    duplicates += 1
        return {
            "accepted": accepted,
            "duplicates": duplicates,
            "total": len(results),
            "results": results,
        }

    # ---- read -----------------------------------------------------------
    def query(self, organ: str | None = None, since: str | None = None,
              limit: int = 100) -> list[dict]:
        """Return stored envelopes, newest-first, filtered by organ/since.

        since: ISO timestamp; only receipts with ts >= since are returned.

        Reads only each organ's validated chain (the byte prefix recovery
        accepted), so a concurrent writer's in-flight line is never returned
        half-written. Raises LedgerCorruptionError when a requested organ's
        persisted chain failed validation, rather than silently skipping the
        bad records and presenting the remainder as complete.
        """
        limit = max(0, int(limit))
        organs = [normalize_organ(organ)] if organ else self._list_organs()

        since_dt = _parse_dt(since) if since else None
        out: list[dict] = []
        with self._lock:
            for org in organs:
                st = self._state(org)
                self._raise_if_corrupt(st)
                d = self._organ_dir(org)
                for name, (validated_bytes, _lines) in list(st.files.items()):
                    with open(os.path.join(d, name), "rb") as fh:
                        data = fh.read(validated_bytes)
                    if len(data) != validated_bytes:
                        raise LedgerCorruptionError(
                            org, os.path.join(d, name), 0, -1, "truncated_after_validation",
                            f"read {len(data)} of {validated_bytes} validated bytes", name)
                    for lineno, raw in enumerate(data.splitlines(), start=1):
                        if not raw.strip():
                            continue
                        try:
                            env = json.loads(raw)
                        except ValueError:
                            # Validated bytes no longer parse: rewritten in place.
                            raise LedgerCorruptionError(
                                org, os.path.join(d, name), lineno, -1, "unparseable_line",
                                "validated record no longer parses", name) from None
                        if since_dt is not None:
                            env_dt = _parse_dt(env.get("ts"))
                            if env_dt is not None and env_dt < since_dt:
                                continue
                        out.append(env)
        # newest-first by ingest order: sort by (ts, chain_index)
        out.sort(key=lambda e: (str(e.get("ts")), e.get("chain_index") or 0),
                 reverse=True)
        if limit:
            return out[:limit]
        return out

    def chain_head(self, organ: str) -> dict:
        """Current validated hash-chain head + count for one organ.

        Re-read from disk under the writer lock, so it reflects every writer
        sharing this root. Raises LedgerCorruptionError for a corrupted organ.
        """
        org = normalize_organ(organ)
        with self._lock:
            st = self._state(org)
            self._raise_if_corrupt(st)
            return {
                "organ": org,
                "chain_alg": CHAIN_HASH,
                "chain_head": st.chain_head,
                "chain_index": st.chain_index,
                "count": st.count,
            }

    def verify_chain(self, organ: str) -> dict:
        """Re-derive and verify one organ's Khipu chain straight from disk.

        Tamper-EVIDENT (advisory — the Khipu chain is Conjecture 2, an advisory
        BFT construction, NOT a proven theorem): this replays the on-disk NDJSON
        partitions in stored order and, for every envelope, checks that

          1. ``chain_index`` is strictly +1 monotonic from genesis (a gap, a
             re-order, a truncation, or an inserted line breaks this),
          2. ``prev_hash`` links to the previous envelope's stored
             ``chain_hash`` (``None`` only at genesis),
          3. the stored ``chain_hash`` EQUALS SHA3-256 recomputed over the
             canonical link object, so mutating any COMMITTED link field
             (``prev_hash``/``receipt_id``/``organ``/``ts``/``chain_index``)
             is detected, and
          4. the stored receipt body is a JSON object, and the stored
             ``receipt_id`` still EQUALS ``receipt_identity`` of that body,
             which binds a content-addressed (id-less)
             receipt's entire body to the chain and detects tampering of an
             id-bearing receipt's identity field.

        HONEST SCOPE. This verifies hash-chain integrity + ordering. It does NOT
        verify DSSE receipt signatures (a separate concern — see the cosign
        verify path). For a receipt that supplies its OWN id, the chain commits
        to that identity and ordering, not to every payload byte, so tampering a
        NON-identity field of an id-bearing receipt is out of scope here (the
        receipt's DSSE signature covers that). A content-addressed (id-less)
        receipt IS bound in full by check (4).

        Reads authoritative on-disk state directly (never the in-memory cache),
        so it catches tampering done to the files behind a running process. It
        holds the organ's writer lock while reading, so it sees a consistent
        snapshot even while other processes append.

        Returns ``{organ, chain_alg, ok, count, chain_head, chain_index,
        broken:[{position, kind, detail}, ...]}``. ``ok`` is True iff
        ``broken`` is empty.
        """
        org = normalize_organ(organ)
        broken: list[dict] = []
        expected_prev: str | None = None
        pos = 0
        last_hash: str | None = None
        last_index = 0

        with self._lock, self._organ_lock(org, write=False):
            for path in self._iter_partition_files(org):
                with open(path, "r", encoding="utf-8") as fh:
                    for lineno, raw in enumerate(fh, 1):
                        line = raw.strip()
                        if not line:
                            continue
                        pos += 1
                        try:
                            env = json.loads(line)
                        except json.JSONDecodeError:
                            broken.append({
                                "position": pos,
                                "kind": "unparseable_line",
                                "detail": f"{os.path.basename(path)}:{lineno}",
                            })
                            continue
                        if not isinstance(env, dict):
                            broken.append({
                                "position": pos,
                                "kind": "unparseable_line",
                                "detail": f"{os.path.basename(path)}:{lineno}",
                            })
                            continue

                        ci = env.get("chain_index")
                        if ci != pos:
                            broken.append({
                                "position": pos,
                                "kind": "index_discontinuity",
                                "detail": f"expected {pos}, stored {ci!r}",
                            })

                        stored_prev = env.get("prev_hash")
                        if stored_prev != expected_prev:
                            broken.append({
                                "position": pos,
                                "kind": "prev_hash_mismatch",
                                "detail": (f"expected {expected_prev!r}, "
                                           f"stored {stored_prev!r}"),
                            })

                        stored_hash = env.get("chain_hash")
                        recomputed = _link_hash(
                            stored_prev, env.get("receipt_id"),
                            env.get("organ"), env.get("ts"), ci)
                        if recomputed != stored_hash:
                            broken.append({
                                "position": pos,
                                "kind": "chain_hash_mismatch",
                                "detail": "recomputed link hash != stored",
                            })

                        receipt = env.get("receipt")
                        if not isinstance(receipt, dict):
                            broken.append({
                                "position": pos,
                                "kind": "malformed_receipt",
                                "detail": "stored receipt body must be a JSON object",
                            })
                        else:
                            try:
                                rid = receipt_identity(receipt)
                            except Exception:  # noqa: BLE001
                                rid = None
                            if rid != env.get("receipt_id"):
                                broken.append({
                                    "position": pos,
                                    "kind": "receipt_id_mismatch",
                                    "detail": ("stored receipt body no longer "
                                               "hashes to receipt_id"),
                                })

                        expected_prev = stored_hash
                        last_hash = stored_hash
                        if isinstance(ci, int):
                            last_index = ci

        return {
            "organ": org,
            "chain_alg": CHAIN_HASH,
            "ok": not broken,
            "count": pos,
            "chain_head": last_hash,
            "chain_index": last_index,
            "broken": broken,
        }

    def health(self) -> dict:
        """Store reachability, total receipts, and per-organ counts.

        Counts cover validated records only. A corrupted organ is reported
        (``ok`` False, its name in ``corrupted_organs``, and a ``corruption``
        entry naming the first bad line) instead of raising; its count is the
        validated prefix before that line.
        """
        with self._lock:
            per_organ = {}
            total = 0
            corrupted = []
            for org in self._list_organs():
                st = self._state(org)
                entry = {
                    "count": st.count,
                    "chain_index": st.chain_index,
                    "chain_head": st.chain_head,
                }
                if st.corruption is not None:
                    entry["corruption"] = st.corruption.as_dict()
                    corrupted.append(org)
                per_organ[org] = entry
                total += st.count
            out = {
                "ok": (os.path.isdir(self.root) or not per_organ)
                and not corrupted,
                "store": self.root,
                "chain_alg": CHAIN_HASH,
                "schema": SCHEMA,
                "total_receipts": total,
                "organs": per_organ,
            }
            if corrupted:
                out["corrupted_organs"] = corrupted
            return out

    def reload(self) -> None:
        """Drop cached state (including a recorded corruption) so the next
        access re-runs the validating replay from disk."""
        with self._lock:
            self._states.clear()


def _parse_dt(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


_DEFAULT_LEDGER: ReceiptLedger | None = None
_DEFAULT_LOCK = threading.Lock()


def get_default_ledger() -> ReceiptLedger:
    """Process-wide default ledger rooted at $SZL_LAKE_DIR (or ./khipu)."""
    global _DEFAULT_LEDGER
    with _DEFAULT_LOCK:
        if _DEFAULT_LEDGER is None:
            _DEFAULT_LEDGER = ReceiptLedger()
        return _DEFAULT_LEDGER


if __name__ == "__main__":
    import sys
    led = get_default_ledger()
    print(json.dumps(led.health(), indent=2))
    sys.exit(0)
