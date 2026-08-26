"""Single-machine durable epic coordination and evidence service.

This is a coherent extension of the Kanban board database. Ordinary domain
state/evidence writes use ``kanban_db.write_txn`` and work-item fencing.
Schema bootstrap, backup, and restore are explicitly separate privileged
maintenance operations: each requires a short-lived ``MaintenanceAuthority``
and emits append-only provenance. Bootstrap owns its schema transaction; the
backup wrapper uses SQLite's snapshot API plus no-replace publication.

These controls authorize cooperating single-machine callers. An OS user with
direct SQLite, executable, policy, and anchor-file access remains outside the
service boundary. Model principals and model tools never receive maintenance
capabilities or writable database access.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
import errno
from functools import lru_cache
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


class MaintenanceAuthorityError(DurableStateError):
    pass


class OutcomeUnknownError(DurableStateError):
    pass


@dataclass(frozen=True)
class MaintenanceAuthority:
    """Short-lived capability for privileged non-model maintenance."""

    operation_id: str
    actor: str
    reason: str
    issued_at: int
    expires_at: int
    allowed_operation: str


SCHEMA_VERSION = 4
_SCHEMA_CONTRACT_LABEL = "phase0-state-v4-owned-schema-closure"
_MAINTENANCE_OPERATIONS = frozenset({"schema_bootstrap", "backup", "restore"})
MAX_MAINTENANCE_AUTHORITY_TTL_SECONDS = 300
FENCE_ANCHOR_LOCK_TIMEOUT_SECONDS = 1.0
_FENCE_ANCHOR_LOCK_POLL_SECONDS = 0.05


_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS epic_leases (
       scope TEXT PRIMARY KEY, owner TEXT,
       token INTEGER NOT NULL CHECK(token >= 0),
       expires_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS epic_evidence (
       digest TEXT PRIMARY KEY, media_type TEXT NOT NULL,
       size INTEGER NOT NULL, body BLOB NOT NULL, created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS epic_receipts (
       operation_id TEXT PRIMARY KEY, scope TEXT NOT NULL, operation TEXT NOT NULL,
       request_digest TEXT NOT NULL, owner TEXT NOT NULL, fence_token INTEGER NOT NULL,
       kind TEXT NOT NULL,
       outcome TEXT NOT NULL CHECK(outcome IN ('success','failure','unknown')),
       payload_json TEXT NOT NULL, evidence_digest TEXT,
       predecessor_digest TEXT, reconciliation_ref TEXT, created_at INTEGER NOT NULL,
       transaction_digest TEXT NOT NULL UNIQUE,
       FOREIGN KEY(evidence_digest) REFERENCES epic_evidence(digest),
       FOREIGN KEY(reconciliation_ref) REFERENCES epic_receipts(operation_id)
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS epic_receipts_one_reconciliation
       ON epic_receipts(reconciliation_ref) WHERE reconciliation_ref IS NOT NULL""",
    """CREATE TABLE IF NOT EXISTS epic_chain_heads (
       scope TEXT PRIMARY KEY, head_digest TEXT NOT NULL,
       receipt_count INTEGER NOT NULL CHECK(receipt_count > 0)
    )""",
    """CREATE TABLE IF NOT EXISTS epic_schema_meta (
       singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
       schema_version INTEGER NOT NULL, contract_digest TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS epic_maintenance_receipts (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       operation_id TEXT NOT NULL UNIQUE,
       request_digest TEXT NOT NULL,
       operation TEXT NOT NULL,
       actor TEXT NOT NULL,
       reason TEXT NOT NULL,
       issued_at INTEGER NOT NULL,
       target_identity TEXT NOT NULL,
       outcome TEXT NOT NULL CHECK(outcome IN ('unknown','success','failure')),
       schema_digest TEXT,
       artifact_digest TEXT,
       created_at INTEGER NOT NULL,
       receipt_digest TEXT NOT NULL UNIQUE
    )""",
    """CREATE TRIGGER IF NOT EXISTS epic_receipts_no_update
       BEFORE UPDATE ON epic_receipts
       BEGIN SELECT RAISE(ABORT, 'epic receipts are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS epic_receipts_no_delete
       BEFORE DELETE ON epic_receipts
       BEGIN SELECT RAISE(ABORT, 'epic receipts are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS epic_evidence_no_update
       BEFORE UPDATE ON epic_evidence
       BEGIN SELECT RAISE(ABORT, 'epic evidence is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS epic_evidence_no_delete
       BEFORE DELETE ON epic_evidence
       BEGIN SELECT RAISE(ABORT, 'epic evidence is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS epic_maintenance_no_update
       BEFORE UPDATE ON epic_maintenance_receipts
       BEGIN SELECT RAISE(ABORT, 'epic maintenance receipts are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS epic_maintenance_no_delete
       BEFORE DELETE ON epic_maintenance_receipts
       BEGIN SELECT RAISE(ABORT, 'epic maintenance receipts are append-only'); END""",
)
_SCHEMA = ";\n".join(_SCHEMA_STATEMENTS) + ";"

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


def _normalize_sql(value: str) -> str:
    return " ".join((value or "").lower().replace("\n", " ").split())


def _schema_object_map(conn: sqlite3.Connection) -> dict[str, tuple[str, str, str]]:
    return {
        str(row["name"]): (
            str(row["type"]),
            str(row["tbl_name"]),
            _normalize_sql(str(row["sql"])),
        )
        for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE (substr(name,1,5) = 'epic_' "
            "OR substr(tbl_name,1,5) = 'epic_') "
            "AND sql IS NOT NULL"
        )
    }


def _schema_semantics(conn: sqlite3.Connection) -> dict[str, Any]:
    objects = _schema_object_map(conn)
    tables = sorted(name for name, value in objects.items() if value[0] == "table")
    indexes = sorted(name for name, value in objects.items() if value[0] == "index")
    return {
        "tables": {
            table: {
                "xinfo": [tuple(row) for row in conn.execute(
                    "SELECT cid,name,type,\"notnull\",dflt_value,pk,hidden "
                    "FROM pragma_table_xinfo(?) ORDER BY cid",
                    (table,),
                )],
                "foreign_keys": [tuple(row) for row in conn.execute(
                    "SELECT id,seq,\"table\",\"from\",\"to\",on_update,on_delete,match "
                    "FROM pragma_foreign_key_list(?) ORDER BY id,seq",
                    (table,),
                )],
            }
            for table in tables
        },
        "indexes": {
            index: [tuple(row) for row in conn.execute(
                "SELECT seqno,cid,name,desc,coll,key "
                "FROM pragma_index_xinfo(?) ORDER BY seqno",
                (index,),
            )]
            for index in indexes
        },
    }


@lru_cache(maxsize=1)
def _expected_schema_contract() -> tuple[dict[str, tuple[str, str, str]], dict[str, Any]]:
    reference = sqlite3.connect(":memory:")
    reference.row_factory = sqlite3.Row
    try:
        reference.executescript(_SCHEMA)
        return _schema_object_map(reference), _schema_semantics(reference)
    finally:
        reference.close()


def schema_contract_digest() -> str:
    objects, semantics = _expected_schema_contract()
    return _digest({
        "contract_label": _SCHEMA_CONTRACT_LABEL,
        "objects": objects,
        "semantics": semantics,
    })


def _validate_schema_contract(
    conn: sqlite3.Connection, *, allow_missing: bool = False
) -> None:
    expected_objects, expected_semantics = _expected_schema_contract()
    actual_objects = _schema_object_map(conn)
    if allow_missing:
        unexpected = set(actual_objects) - set(expected_objects)
        mismatched = {
            name
            for name in actual_objects
            if name in expected_objects and actual_objects[name] != expected_objects[name]
        }
        if unexpected or mismatched:
            raise IntegrityError(
                "schema contract mismatch for existing objects: "
                f"unexpected={sorted(unexpected)} mismatched={sorted(mismatched)}"
            )
    elif actual_objects != expected_objects:
        missing = set(expected_objects) - set(actual_objects)
        extra = set(actual_objects) - set(expected_objects)
        mismatched = {
            name
            for name in set(actual_objects) & set(expected_objects)
            if actual_objects[name] != expected_objects[name]
        }
        raise IntegrityError(
            "schema contract mismatch: "
            f"missing={sorted(missing)} extra={sorted(extra)} "
            f"mismatched={sorted(mismatched)}"
        )

    actual_semantics = _schema_semantics(conn)
    if allow_missing:
        for table, value in actual_semantics["tables"].items():
            if value != expected_semantics["tables"].get(table):
                raise IntegrityError(f"schema contract semantic mismatch for table {table}")
        for index, value in actual_semantics["indexes"].items():
            if value != expected_semantics["indexes"].get(index):
                raise IntegrityError(f"schema contract semantic mismatch for index {index}")
    elif actual_semantics != expected_semantics:
        raise IntegrityError("schema contract PRAGMA semantic snapshot mismatch")

    if "epic_schema_meta" in actual_objects:
        meta = conn.execute(
            "SELECT schema_version,contract_digest FROM epic_schema_meta WHERE singleton=1"
        ).fetchone()
        if (
            meta is None
            or int(meta["schema_version"]) != SCHEMA_VERSION
            or str(meta["contract_digest"]) != schema_contract_digest()
        ):
            raise IntegrityError("schema contract metadata mismatch")
    elif not allow_missing:
        raise IntegrityError("schema contract metadata missing")


def _require_maintenance_authority(
    authority: Optional[MaintenanceAuthority],
    operation: str,
    *,
    now: Optional[int] = None,
) -> MaintenanceAuthority:
    if not isinstance(authority, MaintenanceAuthority):
        raise MaintenanceAuthorityError(f"explicit {operation} authority is required")
    if operation not in _MAINTENANCE_OPERATIONS or authority.allowed_operation != operation:
        raise MaintenanceAuthorityError(
            f"authority does not permit {operation}: {authority.allowed_operation!r}"
        )
    if not authority.operation_id.strip() or not authority.actor.strip() or not authority.reason.strip():
        raise MaintenanceAuthorityError("maintenance authority identity and reason are required")
    lifetime = int(authority.expires_at) - int(authority.issued_at)
    if lifetime <= 0 or lifetime > MAX_MAINTENANCE_AUTHORITY_TTL_SECONDS:
        raise MaintenanceAuthorityError(
            "maintenance authority lifetime must be between 1 and "
            f"{MAX_MAINTENANCE_AUTHORITY_TTL_SECONDS} seconds"
        )
    observed = int(time.time()) if now is None else int(now)
    if int(authority.issued_at) > observed or int(authority.expires_at) <= observed:
        raise MaintenanceAuthorityError("maintenance authority is not currently valid")
    kanban_db._assert_not_delegated_child_mutation()
    return authority


def _connection_identity(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return str(Path(row[2]).resolve()) if row[2] else ":memory:"
    return ":unknown:"


def system_bootstrap_authority(conn: sqlite3.Connection) -> MaintenanceAuthority:
    now = int(time.time())
    identity = _connection_identity(conn)
    operation_id = "schema-bootstrap-" + _digest(
        {"target": identity, "schema_digest": schema_contract_digest()}
    )
    return MaintenanceAuthority(
        operation_id=operation_id,
        actor="hermes-kanban-connect",
        reason="install or validate the built-in epic state schema",
        issued_at=now,
        expires_at=now + 300,
        allowed_operation="schema_bootstrap",
    )


def _maintenance_receipt_fields(
    *,
    operation_id: str,
    operation: str,
    authority: MaintenanceAuthority,
    target_identity: str,
    outcome: str,
    schema_digest: Optional[str],
    artifact_digest: Optional[str],
    created_at: int,
) -> dict[str, Any]:
    request = {
        "operation": operation,
        "actor": authority.actor,
        "reason": authority.reason,
        "target_identity": target_identity,
        "outcome": outcome,
        "schema_digest": schema_digest,
        "artifact_digest": artifact_digest,
    }
    fields = {
        "operation_id": operation_id,
        "request_digest": _digest(request),
        **request,
        "issued_at": int(authority.issued_at),
        "created_at": int(created_at),
    }
    fields["receipt_digest"] = _digest(fields)
    return fields


def _insert_maintenance_receipt(
    conn: sqlite3.Connection,
    fields: Mapping[str, Any],
) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT * FROM epic_maintenance_receipts WHERE operation_id=?",
        (fields["operation_id"],),
    ).fetchone()
    if existing is not None:
        if existing["request_digest"] != fields["request_digest"]:
            raise OperationConflict(
                "maintenance operation_id was reused for a different request"
            )
        return dict(existing)
    columns = (
        "operation_id", "request_digest", "operation", "actor", "reason",
        "issued_at", "target_identity", "outcome", "schema_digest",
        "artifact_digest", "created_at", "receipt_digest",
    )
    conn.execute(
        "INSERT INTO epic_maintenance_receipts (" + ",".join(columns) + ") "
        "VALUES (" + ",".join("?" for _ in columns) + ")",
        tuple(fields[column] for column in columns),
    )
    return dict(fields)


def initialize_schema(
    conn: sqlite3.Connection,
    *,
    authority: Optional[MaintenanceAuthority],
) -> dict[str, Any]:
    """Install/validate schema under explicit privileged bootstrap authority."""
    authority = _require_maintenance_authority(authority, "schema_bootstrap")
    if conn.in_transaction:
        raise RuntimeError("initialize_schema refuses an active transaction")
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE (substr(name,1,5) = 'epic_' "
            "OR substr(tbl_name,1,5) = 'epic_') AND sql IS NOT NULL"
        )
    }
    if existing and "epic_schema_meta" not in existing:
        raise IntegrityError("schema contract metadata missing for existing epic objects")
    if "epic_schema_meta" in existing:
        _validate_schema_contract(conn, allow_missing=True)

    digest = schema_contract_digest()
    target = _connection_identity(conn)
    created_at = int(time.time())
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
        meta = conn.execute(
            "SELECT schema_version,contract_digest FROM epic_schema_meta WHERE singleton=1"
        ).fetchone()
        if meta is None:
            conn.execute(
                "INSERT INTO epic_schema_meta(singleton,schema_version,contract_digest) "
                "VALUES(1,?,?)",
                (SCHEMA_VERSION, digest),
            )
        elif tuple(meta) != (SCHEMA_VERSION, digest):
            raise IntegrityError("schema contract metadata mismatch")
        receipt = _insert_maintenance_receipt(
            conn,
            _maintenance_receipt_fields(
                operation_id=authority.operation_id,
                operation="schema_bootstrap",
                authority=authority,
                target_identity=target,
                outcome="success",
                schema_digest=digest,
                artifact_digest=None,
                created_at=created_at,
            ),
        )
        _validate_schema_contract(conn)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return receipt


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
    kanban_db._assert_not_delegated_child_mutation()
    if int(expires_at) <= int(now):
        raise ValueError("lease expiry must be later than the acquisition time")
    request = {
        "scope": scope, "owner": owner, "expected_token": int(expected_token),
        "expires_at": int(expires_at), "now": int(now), "operation": "acquire_lease",
    }
    request_digest = _digest(request)
    # Match restore's anchor-lock -> DB-lock order. The anchor reservation is
    # deliberately rollback-resistant, so an aborted acquisition may burn a
    # token but can never make a token reusable.
    with _fence_token_reserver(_database_path(conn)) as reserve_token:
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
                if (
                    bytes(existing["body"]) != evidence_bytes
                    or int(existing["size"]) != len(evidence_bytes)
                    or str(existing["media_type"]) != media_type
                ):
                    raise IntegrityError("existing evidence bytes or metadata do not match digest")
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
    _validate_schema_contract(conn)
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
    maintenance_fields = (
        "operation_id", "request_digest", "operation", "actor", "reason",
        "issued_at", "target_identity", "outcome", "schema_digest",
        "artifact_digest", "created_at",
    )
    for row in conn.execute("SELECT * FROM epic_maintenance_receipts ORDER BY id"):
        fields = {name: row[name] for name in maintenance_fields}
        if _digest(fields) != row["receipt_digest"]:
            raise IntegrityError("maintenance receipt digest mismatch")
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
    acquired = False
    try:
        deadline = time.monotonic() + FENCE_ANCHOR_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LeaseConflict(
                        "timed out acquiring fence anchor lock after "
                        f"{FENCE_ANCHOR_LOCK_TIMEOUT_SECONDS:.1f} seconds"
                    ) from exc
                time.sleep(min(_FENCE_ANCHOR_LOCK_POLL_SECONDS, remaining))
        tokens = _load_fence_anchor(anchor)

        def reserve(scope: str, floor: int) -> int:
            token = max(int(floor), int(tokens.get(scope, 0))) + 1
            tokens[scope] = token
            _write_fence_anchor(anchor, tokens)
            return token

        yield reserve
    finally:
        try:
            if acquired and os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif acquired:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _publish_noreplace(temp: Path, destination: Path) -> None:
    """Atomically publish a file only when destination is still absent."""
    os.link(temp, destination)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_maintenance(
    conn: sqlite3.Connection,
    *,
    authority: MaintenanceAuthority,
    operation_id: str,
    operation: str,
    target_identity: str,
    outcome: str,
    artifact_digest: Optional[str] = None,
) -> dict[str, Any]:
    fields = _maintenance_receipt_fields(
        operation_id=operation_id,
        operation=operation,
        authority=authority,
        target_identity=target_identity,
        outcome=outcome,
        schema_digest=schema_contract_digest(),
        artifact_digest=artifact_digest,
        created_at=int(time.time()),
    )
    with kanban_db.write_txn(conn):
        return _insert_maintenance_receipt(conn, fields)


def backup_service(
    conn: sqlite3.Connection,
    destination: Path,
    *,
    authority: Optional[MaintenanceAuthority],
    _before_publish: Optional[Callable[[Path], None]] = None,
) -> Path:
    """Create a verified backup under explicit privileged authority/provenance."""
    from hermes_cli.backup import copy_db_and_verify

    authority = _require_maintenance_authority(authority, "backup")
    destination = Path(destination)
    target_identity = str(destination.resolve())
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    validate_integrity(conn)
    _record_maintenance(
        conn,
        authority=authority,
        operation_id=authority.operation_id + ":intent",
        operation="backup_intent",
        target_identity=target_identity,
        outcome="unknown",
    )
    temp = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.backup"
    )
    try:
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
        except Exception:
            _record_maintenance(
                conn,
                authority=authority,
                operation_id=authority.operation_id + ":failure",
                operation="backup_failure",
                target_identity=target_identity,
                outcome="failure",
            )
            raise
        try:
            artifact_digest = _sha256_file(destination)
            _record_maintenance(
                conn,
                authority=authority,
                operation_id=authority.operation_id + ":complete",
                operation="backup_complete",
                target_identity=target_identity,
                outcome="success",
                artifact_digest=artifact_digest,
            )
        except Exception as exc:
            raise OutcomeUnknownError(
                "backup published but artifact verification or completion provenance "
                "could not be finalized"
            ) from exc
        return destination
    finally:
        # Publication/provenance determines the operation outcome. A stale
        # private temp is recoverable and must not overwrite that outcome.
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)


def restore_service(
    backup: Path,
    destination: Path,
    *,
    authority: Optional[MaintenanceAuthority],
    _before_publish: Optional[Callable[[Path], None]] = None,
) -> Path:
    """Publish a verified restore under explicit privileged authority."""
    from hermes_cli.backup import copy_db_and_verify

    authority = _require_maintenance_authority(authority, "restore")
    backup, destination = Path(backup), Path(destination)
    target_identity = str(destination.resolve())
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
                    _insert_maintenance_receipt(
                        conn,
                        _maintenance_receipt_fields(
                            operation_id=authority.operation_id + ":complete",
                            operation="restore_complete",
                            authority=authority,
                            target_identity=target_identity,
                            outcome="success",
                            schema_digest=schema_contract_digest(),
                            artifact_digest=None,
                            created_at=restored_at,
                        ),
                    )
            validate_integrity(conn)
        finally:
            conn.close()
        if _before_publish is not None:
            _before_publish(destination)
        _publish_noreplace(temp, destination)
        return destination
    finally:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
