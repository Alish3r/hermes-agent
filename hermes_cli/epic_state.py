"""Single-machine durable epic coordination and evidence service.

This is a coherent extension of the Kanban board database: callers pass the
exact board ``sqlite3.Connection`` and every service mutation uses
``kanban_db.write_txn``. Fencing authorizes cooperating callers using this API;
an OS user with direct SQLite/file access is outside the authorization boundary.
Model principals and model tools must never receive a writable connection or
direct database access.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from hermes_cli import kanban_db


class DurableStateError(RuntimeError):
    pass


class LeaseConflict(DurableStateError):
    pass


class OperationConflict(DurableStateError):
    pass


class FenceRejected(DurableStateError):
    pass


class IntegrityError(DurableStateError):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS epic_leases (
 scope TEXT PRIMARY KEY, owner TEXT, token INTEGER NOT NULL CHECK(token >= 0),
 expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS epic_evidence (
 digest TEXT PRIMARY KEY, media_type TEXT NOT NULL, size INTEGER NOT NULL,
 body BLOB NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS epic_receipts (
 operation_id TEXT PRIMARY KEY, scope TEXT NOT NULL, operation TEXT NOT NULL,
 request_digest TEXT NOT NULL, owner TEXT NOT NULL, fence_token INTEGER NOT NULL,
 kind TEXT NOT NULL, outcome TEXT NOT NULL CHECK(outcome IN ('success','failure','unknown')),
 payload_json TEXT NOT NULL, evidence_digest TEXT,
 predecessor_digest TEXT, reconciliation_ref TEXT, created_at INTEGER NOT NULL,
 transaction_digest TEXT NOT NULL UNIQUE,
 FOREIGN KEY(evidence_digest) REFERENCES epic_evidence(digest),
 FOREIGN KEY(reconciliation_ref) REFERENCES epic_receipts(operation_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS epic_receipts_one_reconciliation
 ON epic_receipts(reconciliation_ref) WHERE reconciliation_ref IS NOT NULL;
CREATE TABLE IF NOT EXISTS epic_chain_heads (
 scope TEXT PRIMARY KEY, head_digest TEXT NOT NULL,
 receipt_count INTEGER NOT NULL CHECK(receipt_count > 0)
);
CREATE TABLE IF NOT EXISTS epic_schema_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
 schema_version INTEGER NOT NULL, contract_digest TEXT NOT NULL
);
INSERT OR IGNORE INTO epic_schema_meta(singleton,schema_version,contract_digest)
 VALUES(1,2,'phase0-state-v2-chain-anchor');
CREATE TRIGGER IF NOT EXISTS epic_receipts_no_update BEFORE UPDATE ON epic_receipts
 BEGIN SELECT RAISE(ABORT, 'epic receipts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS epic_receipts_no_delete BEFORE DELETE ON epic_receipts
 BEGIN SELECT RAISE(ABORT, 'epic receipts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS epic_evidence_no_update BEFORE UPDATE ON epic_evidence
 BEGIN SELECT RAISE(ABORT, 'epic evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS epic_evidence_no_delete BEFORE DELETE ON epic_evidence
 BEGIN SELECT RAISE(ABORT, 'epic evidence is append-only'); END;
"""

_RECEIPT_FIELDS = (
    "operation_id", "scope", "operation", "request_digest", "owner",
    "fence_token", "kind", "outcome", "payload_json", "evidence_digest",
    "predecessor_digest", "reconciliation_ref", "created_at", "transaction_digest",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _digest(value: Any) -> str:
    return sha256_bytes(_canonical(value).encode("utf-8"))


_EXPECTED_TABLE_COLUMNS = {
    "epic_leases": [
        ("scope", "TEXT", 1), ("owner", "TEXT", 0),
        ("token", "INTEGER", 0), ("expires_at", "INTEGER", 0),
    ],
    "epic_evidence": [
        ("digest", "TEXT", 1), ("media_type", "TEXT", 0),
        ("size", "INTEGER", 0), ("body", "BLOB", 0),
        ("created_at", "INTEGER", 0),
    ],
    "epic_receipts": [
        ("operation_id", "TEXT", 1), ("scope", "TEXT", 0),
        ("operation", "TEXT", 0), ("request_digest", "TEXT", 0),
        ("owner", "TEXT", 0), ("fence_token", "INTEGER", 0),
        ("kind", "TEXT", 0), ("outcome", "TEXT", 0),
        ("payload_json", "TEXT", 0), ("evidence_digest", "TEXT", 0),
        ("predecessor_digest", "TEXT", 0), ("reconciliation_ref", "TEXT", 0),
        ("created_at", "INTEGER", 0), ("transaction_digest", "TEXT", 0),
    ],
    "epic_chain_heads": [
        ("scope", "TEXT", 1), ("head_digest", "TEXT", 0),
        ("receipt_count", "INTEGER", 0),
    ],
    "epic_schema_meta": [
        ("singleton", "INTEGER", 1), ("schema_version", "INTEGER", 0),
        ("contract_digest", "TEXT", 0),
    ],
}
_EXPECTED_TRIGGERS = {
    "epic_receipts_no_update": ("before update on epic_receipts", "epic receipts are append-only"),
    "epic_receipts_no_delete": ("before delete on epic_receipts", "epic receipts are append-only"),
    "epic_evidence_no_update": ("before update on epic_evidence", "epic evidence is append-only"),
    "epic_evidence_no_delete": ("before delete on epic_evidence", "epic evidence is append-only"),
}
_SCHEMA_VERSION = 2
_SCHEMA_CONTRACT_DIGEST = "phase0-state-v2-chain-anchor"


def _normalize_sql(value: str) -> str:
    return " ".join((value or "").lower().replace("\n", " ").split())


def _validate_schema_contract(
    conn: sqlite3.Connection, *, allow_missing: bool = False
) -> None:
    objects = {
        row["name"]: (row["type"], row["sql"] or "")
        for row in conn.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'epic_%'"
        )
    }
    for table, expected in _EXPECTED_TABLE_COLUMNS.items():
        if table not in objects:
            if allow_missing:
                continue
            raise IntegrityError(f"schema contract missing table {table}")
        actual = [
            (str(row["name"]), str(row["type"]).upper(), int(row["pk"]))
            for row in conn.execute(
                "SELECT name,type,pk FROM pragma_table_info(?)", (table,)
            )
        ]
        if actual != expected:
            raise IntegrityError(f"schema contract mismatch for table {table}")
    for trigger, (shape, message) in _EXPECTED_TRIGGERS.items():
        if trigger not in objects:
            if allow_missing:
                continue
            raise IntegrityError(f"schema contract missing trigger {trigger}")
        sql = _normalize_sql(objects[trigger][1])
        if shape not in sql or message not in sql:
            raise IntegrityError(f"schema contract mismatch for trigger {trigger}")
    index = objects.get("epic_receipts_one_reconciliation")
    if index is None:
        if not allow_missing:
            raise IntegrityError("schema contract missing reconciliation index")
    else:
        sql = _normalize_sql(index[1])
        if "unique index" not in sql or "where reconciliation_ref is not null" not in sql:
            raise IntegrityError("schema contract mismatch for reconciliation index")
    if "epic_schema_meta" in objects:
        meta = conn.execute(
            "SELECT schema_version,contract_digest FROM epic_schema_meta WHERE singleton=1"
        ).fetchone()
        if (
            meta is None
            or int(meta["schema_version"]) != _SCHEMA_VERSION
            or str(meta["contract_digest"]) != _SCHEMA_CONTRACT_DIGEST
        ):
            raise IntegrityError("schema contract metadata mismatch")
    elif not allow_missing:
        raise IntegrityError("schema contract metadata missing")


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Install and validate the additive schema without owning caller work."""
    if conn.in_transaction:
        raise RuntimeError("initialize_schema refuses an active transaction")
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'epic_%'"
        )
    }
    if existing and "epic_schema_meta" not in existing:
        raise IntegrityError("schema contract metadata missing for existing epic objects")
    if "epic_schema_meta" in existing:
        _validate_schema_contract(conn, allow_missing=True)
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA + "\nCOMMIT;")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    _validate_schema_contract(conn)


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {name: row[name] for name in _RECEIPT_FIELDS}


def _existing_operation(conn: sqlite3.Connection, operation_id: str, request_digest: str):
    row = conn.execute(
        "SELECT * FROM epic_receipts WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if row is None:
        return None
    if row["request_digest"] != request_digest:
        raise OperationConflict("operation_id was already used for a different canonical request")
    return _row_dict(row)


def _last_digest(conn: sqlite3.Connection, scope: str) -> Optional[str]:
    row = conn.execute(
        "SELECT transaction_digest FROM epic_receipts WHERE scope=? ORDER BY rowid DESC LIMIT 1",
        (scope,),
    ).fetchone()
    return row[0] if row else None


def _transaction_digest(fields: Mapping[str, Any]) -> str:
    return _digest({key: fields[key] for key in _RECEIPT_FIELDS[:-1]})


def _insert_receipt(conn: sqlite3.Connection, fields: dict[str, Any]) -> dict[str, Any]:
    anchor = conn.execute(
        "SELECT head_digest,receipt_count FROM epic_chain_heads WHERE scope=?",
        (fields["scope"],),
    ).fetchone()
    expected_predecessor = anchor["head_digest"] if anchor else None
    if fields["predecessor_digest"] != expected_predecessor:
        raise IntegrityError("receipt predecessor disagrees with durable chain head")
    fields["transaction_digest"] = _transaction_digest(fields)
    conn.execute(
        "INSERT INTO epic_receipts (" + ",".join(_RECEIPT_FIELDS) + ") VALUES (" +
        ",".join("?" for _ in _RECEIPT_FIELDS) + ")",
        tuple(fields[name] for name in _RECEIPT_FIELDS),
    )
    if anchor is None:
        conn.execute(
            "INSERT INTO epic_chain_heads(scope,head_digest,receipt_count) VALUES(?,?,1)",
            (fields["scope"], fields["transaction_digest"]),
        )
    else:
        conn.execute(
            "UPDATE epic_chain_heads SET head_digest=?,receipt_count=? WHERE scope=?",
            (
                fields["transaction_digest"],
                int(anchor["receipt_count"]) + 1,
                fields["scope"],
            ),
        )
    return dict(fields)


def acquire_lease(
    conn: sqlite3.Connection, *, operation_id: str, scope: str, owner: str,
    expected_token: int, expires_at: int, now: int,
) -> int:
    """CAS-acquire a per-scope lease and advance its never-reused fence once."""
    if int(expires_at) <= int(now):
        raise ValueError("lease expiry must be later than the acquisition time")
    request = {
        "scope": scope, "owner": owner, "expected_token": int(expected_token),
        "expires_at": int(expires_at), "now": int(now), "operation": "acquire_lease",
    }
    request_digest = _digest(request)
    with kanban_db.write_txn(conn):
        replay = _existing_operation(conn, operation_id, request_digest)
        if replay is not None:
            return int(json.loads(replay["payload_json"])["token"])
        row = conn.execute(
            "SELECT owner,token,expires_at FROM epic_leases WHERE scope=?", (scope,)
        ).fetchone()
        current = int(row["token"]) if row else 0
        if current != int(expected_token):
            raise LeaseConflict(f"expected fence {expected_token}, current fence is {current}")
        if (
            row is not None
            and row["owner"] not in (None, owner)
            and int(row["expires_at"]) > int(now)
        ):
            raise LeaseConflict("another owner holds an unexpired lease")
        with _fence_token_reserver(_database_path(conn)) as reserve_token:
            token = reserve_token(scope, current)
            if row:
                changed = conn.execute(
                    "UPDATE epic_leases SET owner=?,token=?,expires_at=? WHERE scope=? AND token=?",
                    (owner, token, int(expires_at), scope, current),
                ).rowcount
                if changed != 1:
                    raise LeaseConflict("lease changed concurrently")
            else:
                conn.execute(
                    "INSERT INTO epic_leases(scope,owner,token,expires_at) VALUES(?,?,?,?)",
                    (scope, owner, token, int(expires_at)),
                )
            fields = {
                "operation_id": operation_id, "scope": scope, "operation": "acquire_lease",
                "request_digest": request_digest, "owner": owner, "fence_token": token,
                "kind": "coordination", "outcome": "success",
                "payload_json": _canonical({"token": token, "expires_at": int(expires_at)}),
                "evidence_digest": None, "predecessor_digest": _last_digest(conn, scope),
                "reconciliation_ref": None, "created_at": int(now),
            }
            _insert_receipt(conn, fields)
    return token


def append_receipt(
    conn: sqlite3.Connection, *, operation_id: str, scope: str, operation: str,
    owner: str, fence_token: int, kind: str, outcome: str, payload: Any,
    evidence_bytes: Optional[bytes] = None, evidence_digest: Optional[str] = None,
    media_type: str = "application/octet-stream", reconciliation_ref: Optional[str] = None,
    created_at: int, now: int, before_commit: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    if outcome not in {"success", "failure", "unknown"}:
        raise ValueError("outcome must be success, failure, or unknown")
    computed_evidence = sha256_bytes(evidence_bytes) if evidence_bytes is not None else None
    if evidence_digest is not None and evidence_digest != computed_evidence:
        raise IntegrityError("supplied evidence digest does not match evidence bytes")
    evidence_digest = evidence_digest or computed_evidence
    payload_json = _canonical(payload)
    request = {
        "scope": scope, "operation": operation, "owner": owner,
        "fence_token": int(fence_token), "kind": kind, "outcome": outcome,
        "payload": json.loads(payload_json), "evidence_digest": evidence_digest,
        "media_type": media_type, "reconciliation_ref": reconciliation_ref,
        "created_at": int(created_at),
    }
    request_digest = _digest(request)
    with kanban_db.write_txn(conn):
        replay = _existing_operation(conn, operation_id, request_digest)
        if replay is not None:
            return replay
        lease = conn.execute(
            "SELECT owner,token,expires_at FROM epic_leases WHERE scope=?", (scope,)
        ).fetchone()
        if (lease is None or lease["owner"] != owner or
                int(lease["token"]) != int(fence_token) or int(lease["expires_at"]) <= int(now)):
            raise FenceRejected("writer does not hold the current unexpired owner/fence")
        if reconciliation_ref is not None:
            if outcome == "unknown":
                raise IntegrityError("reconciliation outcome must be terminal")
            prior = conn.execute(
                "SELECT scope,outcome FROM epic_receipts WHERE operation_id=?", (reconciliation_ref,)
            ).fetchone()
            if prior is None or prior["scope"] != scope or prior["outcome"] != "unknown":
                raise IntegrityError("reconciliation must reference an unknown receipt in this scope")
            already = conn.execute(
                "SELECT operation_id FROM epic_receipts WHERE reconciliation_ref=?",
                (reconciliation_ref,),
            ).fetchone()
            if already is not None:
                raise OperationConflict(
                    f"unknown operation is already reconciled by {already['operation_id']}"
                )
        if evidence_bytes is not None:
            existing = conn.execute(
                "SELECT body,size,media_type FROM epic_evidence WHERE digest=?", (evidence_digest,)
            ).fetchone()
            if existing:
                if bytes(existing["body"]) != evidence_bytes or int(existing["size"]) != len(evidence_bytes):
                    raise IntegrityError("existing evidence bytes do not match digest")
            else:
                conn.execute(
                    "INSERT INTO epic_evidence(digest,media_type,size,body,created_at) VALUES(?,?,?,?,?)",
                    (evidence_digest, media_type, len(evidence_bytes), evidence_bytes, int(created_at)),
                )
        fields = {
            "operation_id": operation_id, "scope": scope, "operation": operation,
            "request_digest": request_digest, "owner": owner, "fence_token": int(fence_token),
            "kind": kind, "outcome": outcome, "payload_json": payload_json,
            "evidence_digest": evidence_digest, "predecessor_digest": _last_digest(conn, scope),
            "reconciliation_ref": reconciliation_ref, "created_at": int(created_at),
        }
        try:
            receipt = _insert_receipt(conn, fields)
        except sqlite3.IntegrityError as exc:
            if reconciliation_ref is not None and "reconciliation_ref" in str(exc):
                raise OperationConflict("unknown operation was already reconciled") from exc
            raise
        if before_commit is not None:
            before_commit()
    return receipt


def read_evidence(conn: sqlite3.Connection, digest: str) -> bytes:
    row = conn.execute(
        "SELECT body,size FROM epic_evidence WHERE digest=?", (digest,)
    ).fetchone()
    if row is None:
        raise IntegrityError(f"missing evidence {digest}")
    body = bytes(row["body"])
    if len(body) != int(row["size"]) or sha256_bytes(body) != digest:
        raise IntegrityError(f"corrupt or substituted evidence {digest}")
    return body


def validate_integrity(conn: sqlite3.Connection) -> bool:
    result = conn.execute("PRAGMA integrity_check").fetchall()
    if not result or any(row[0] != "ok" for row in result):
        raise IntegrityError("SQLite integrity_check failed")
    expected_by_scope: dict[str, Optional[str]] = {}
    count_by_scope: dict[str, int] = {}
    for row in conn.execute("SELECT * FROM epic_receipts ORDER BY rowid"):
        fields = _row_dict(row)
        expected_predecessor = expected_by_scope.get(row["scope"])
        if row["predecessor_digest"] != expected_predecessor:
            raise IntegrityError("receipt predecessor chain mismatch")
        if _transaction_digest(fields) != row["transaction_digest"]:
            raise IntegrityError("receipt transaction digest mismatch")
        expected_by_scope[row["scope"]] = row["transaction_digest"]
        count_by_scope[row["scope"]] = count_by_scope.get(row["scope"], 0) + 1
        if row["evidence_digest"] is not None:
            read_evidence(conn, row["evidence_digest"])
        if row["reconciliation_ref"] is not None:
            prior = conn.execute(
                "SELECT scope,outcome,rowid FROM epic_receipts WHERE operation_id=?",
                (row["reconciliation_ref"],),
            ).fetchone()
            if prior is None or prior["scope"] != row["scope"] or prior["outcome"] != "unknown" or prior["rowid"] >= row["rowid"]:
                raise IntegrityError("invalid reconciliation reference")
    anchors = {
        row["scope"]: (row["head_digest"], int(row["receipt_count"]))
        for row in conn.execute(
            "SELECT scope,head_digest,receipt_count FROM epic_chain_heads"
        )
    }
    if set(anchors) != set(expected_by_scope):
        raise IntegrityError("chain head scope set mismatch")
    for scope, head in expected_by_scope.items():
        if anchors[scope] != (head, count_by_scope[scope]):
            raise IntegrityError(f"chain head mismatch for scope {scope}")
    return True


def _database_path(conn: sqlite3.Connection) -> Path:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return Path(row[2])
    raise IntegrityError("connection has no file-backed main database")


def _fence_anchor_path(database: Path) -> Path:
    return database.with_name(database.name + ".epic-fence-anchor.json")


def _load_fence_anchor(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise IntegrityError(f"invalid fence anchor type: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        payload = document["payload"]
        if document["digest"] != _digest(payload) or payload["schema"] != 1:
            raise ValueError("digest/schema mismatch")
        tokens = payload["tokens"]
        if not isinstance(tokens, dict):
            raise ValueError("tokens must be a mapping")
        parsed = {str(scope): int(token) for scope, token in tokens.items()}
        if any(token < 0 for token in parsed.values()):
            raise ValueError("negative token")
        return parsed
    except Exception as exc:
        raise IntegrityError(f"corrupt fence anchor {path}: {exc}") from exc


def _write_fence_anchor(path: Path, tokens: Mapping[str, int]) -> None:
    payload = {
        "schema": 1,
        "tokens": {scope: int(tokens[scope]) for scope in sorted(tokens)},
    }
    document = {"payload": payload, "digest": _digest(payload)}
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temp.unlink(missing_ok=True)


@contextlib.contextmanager
def _fence_token_reserver(database: Path):
    """Serialize and durably reserve non-reusable tokens outside the DB backup."""
    anchor = _fence_anchor_path(database)
    lock_path = anchor.with_name(anchor.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        tokens = _load_fence_anchor(anchor)

        def reserve(scope: str, floor: int) -> int:
            token = max(int(floor), int(tokens.get(scope, 0))) + 1
            tokens[scope] = token
            _write_fence_anchor(anchor, tokens)
            return token

        yield reserve
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _publish_noreplace(temp: Path, destination: Path) -> None:
    """Atomically publish a file only when destination is still absent."""
    os.link(temp, destination)
    temp.unlink()


def backup_service(
    conn: sqlite3.Connection,
    destination: Path,
    *,
    _before_publish: Optional[Callable[[Path], None]] = None,
) -> Path:
    from hermes_cli.backup import copy_db_and_verify

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    validate_integrity(conn)
    temp = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.backup"
    )
    try:
        if not copy_db_and_verify(_database_path(conn), temp):
            raise IntegrityError("unable to create verified SQLite backup")
        check = sqlite3.connect(temp)
        check.row_factory = sqlite3.Row
        try:
            validate_integrity(check)
        finally:
            check.close()
        if _before_publish is not None:
            _before_publish(destination)
        _publish_noreplace(temp, destination)
        return destination
    finally:
        temp.unlink(missing_ok=True)


def restore_service(
    backup: Path,
    destination: Path,
    *,
    _before_publish: Optional[Callable[[Path], None]] = None,
) -> Path:
    """Publish a verified restore with rollback-resistant fence reservations."""
    from hermes_cli.backup import copy_db_and_verify

    backup, destination = Path(backup), Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.restore"
    )
    try:
        if not copy_db_and_verify(backup, temp):
            raise IntegrityError("backup is not a valid SQLite database")
        conn = sqlite3.connect(temp)
        conn.row_factory = sqlite3.Row
        try:
            validate_integrity(conn)
            with _fence_token_reserver(destination) as reserve_token:
                with kanban_db.write_txn(conn):
                    restored_at = int(time.time())
                    leases = {
                        str(row["scope"]): int(row["token"])
                        for row in conn.execute(
                            "SELECT scope,token FROM epic_leases"
                        )
                    }
                    leases.setdefault("__recovery__", 0)
                    for scope in sorted(leases):
                        previous_token = leases[scope]
                        new_token = reserve_token(scope, previous_token)
                        changed = conn.execute(
                            "UPDATE epic_leases SET token=?, owner=NULL, expires_at=0 "
                            "WHERE scope=? AND token=?",
                            (new_token, scope, previous_token),
                        ).rowcount
                        if changed == 0:
                            if previous_token != 0:
                                raise LeaseConflict(
                                    "restored lease changed during invalidation"
                                )
                            conn.execute(
                                "INSERT INTO epic_leases(scope,owner,token,expires_at) "
                                "VALUES(?,NULL,?,0)",
                                (scope, new_token),
                            )
                        request = {
                            "operation": "restore_service",
                            "scope": scope,
                            "previous_token": previous_token,
                            "new_token": new_token,
                        }
                        predecessor = _last_digest(conn, scope)
                        operation_id = "restore-" + _digest(
                            {**request, "predecessor_digest": predecessor}
                        )
                        fields = {
                            "operation_id": operation_id,
                            "scope": scope,
                            "operation": "restore_service",
                            "request_digest": _digest(request),
                            "owner": "__restore__",
                            "fence_token": new_token,
                            "kind": "recovery",
                            "outcome": "success",
                            "payload_json": _canonical(
                                {
                                    "previous_token": previous_token,
                                    "new_token": new_token,
                                }
                            ),
                            "evidence_digest": None,
                            "predecessor_digest": predecessor,
                            "reconciliation_ref": None,
                            "created_at": restored_at,
                        }
                        _insert_receipt(conn, fields)
            validate_integrity(conn)
        finally:
            conn.close()
        if _before_publish is not None:
            _before_publish(destination)
        _publish_noreplace(temp, destination)
        return destination
    finally:
        temp.unlink(missing_ok=True)
