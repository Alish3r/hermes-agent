"""Behavior contract for the single-machine durable epic state service."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent.delegation_context import delegated_child_context
from hermes_cli import epic_state
from hermes_cli import kanban_db as kb


def open_db(path: Path):
    return kb.connect(path)


def lease(conn, *, scope="s", owner="a", op="lease-a", expected=0, now=10, expiry=100):
    return epic_state.acquire_lease(
        conn, operation_id=op, scope=scope, owner=owner,
        expected_token=expected, expires_at=expiry, now=now,
    )


def append(conn, *, op="write-1", scope="s", owner="a", token=1,
           payload=None, outcome="success", now=11, evidence=b"proof",
           evidence_digest=None, media_type="application/octet-stream",
           reconcile=None, before_commit=None):
    return epic_state.append_receipt(
        conn, operation_id=op, scope=scope, operation="record",
        owner=owner, fence_token=token, kind="state", outcome=outcome,
        payload={} if payload is None else payload, evidence_bytes=evidence,
        evidence_digest=evidence_digest, media_type=media_type,
        reconciliation_ref=reconcile, created_at=now, now=now,
        before_commit=before_commit,
    )


def maintenance(operation, operation_id, *, issued_at=None, lifetime=300):
    issued_at = int(time.time()) if issued_at is None else issued_at
    return epic_state.MaintenanceAuthority(
        operation_id=operation_id,
        actor="phase0-v3-test-maintainer",
        reason="deterministic test maintenance",
        issued_at=issued_at,
        expires_at=issued_at + lifetime,
        allowed_operation=operation,
    )


def bootstrap(conn, operation_id="manual-schema-bootstrap"):
    return epic_state.initialize_schema(
        conn,
        authority=maintenance("schema_bootstrap", operation_id),
    )


def make_backup(conn, destination, *, operation_id=None, **kwargs):
    return epic_state.backup_service(
        conn,
        destination,
        authority=maintenance(
            "backup", operation_id or f"backup-{Path(destination).name}"
        ),
        **kwargs,
    )


def perform_restore(source, destination, *, operation_id=None, **kwargs):
    return epic_state.restore_service(
        source,
        destination,
        authority=maintenance(
            "restore", operation_id or f"restore-{Path(destination).name}"
        ),
        **kwargs,
    )


def test_schema_lease_append_replay_and_operation_conflict(tmp_path):
    db = tmp_path / "board.db"
    conn = open_db(db)
    assert lease(conn) == 1
    first = append(conn, payload={"x": 1})
    replay = append(conn, payload={"x": 1})
    assert replay == first
    assert conn.execute("select count(*) from epic_receipts where operation_id='write-1'").fetchone()[0] == 1
    assert conn.execute("select count(*) from epic_evidence").fetchone()[0] == 1
    with pytest.raises(epic_state.OperationConflict):
        append(conn, payload={"x": 2})


def test_stale_expired_and_successor_fences_fail_closed(tmp_path):
    conn = open_db(tmp_path / "board.db")
    with pytest.raises(ValueError, match="expiry"):
        epic_state.acquire_lease(
            conn,
            operation_id="already-expired",
            scope="expired-scope",
            owner="a",
            expected_token=0,
            expires_at=10,
            now=10,
        )
    assert lease(conn) == 1
    with pytest.raises(epic_state.FenceRejected):
        append(conn, op="stale", token=0)
    with pytest.raises(epic_state.FenceRejected):
        append(conn, op="expired", now=100)
    with pytest.raises(epic_state.LeaseConflict, match="unexpired"):
        epic_state.acquire_lease(
            conn,
            operation_id="lease-b-too-early",
            scope="s",
            owner="b",
            expected_token=1,
            expires_at=200,
            now=20,
        )
    assert epic_state.acquire_lease(conn, operation_id="lease-b", scope="s", owner="b", expected_token=1, expires_at=200, now=100) == 2
    with pytest.raises(epic_state.FenceRejected):
        append(conn, op="predecessor", token=1, now=101)


def test_same_expected_token_has_exactly_one_winner(tmp_path):
    path = tmp_path / "board.db"
    open_db(path).close()
    barrier = threading.Barrier(2)
    results = []
    lock = threading.Lock()

    def contender(owner):
        conn = open_db(path)
        barrier.wait()
        try:
            value = lease(conn, owner=owner, op=f"lease-{owner}")
            result = ("win", value)
        except epic_state.LeaseConflict:
            result = ("lose", None)
        finally:
            conn.close()
        with lock:
            results.append(result)

    threads = [threading.Thread(target=contender, args=(owner,)) for owner in ("a", "b")]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sorted(kind for kind, _ in results) == ["lose", "win"]
    assert [value for kind, value in results if kind == "win"] == [1]


def test_lease_acquisition_takes_fence_anchor_before_database_write_lock(
    tmp_path, monkeypatch
):
    conn = open_db(tmp_path / "board.db")
    events = []
    original_reserver = epic_state._fence_token_reserver
    original_write_txn = kb.write_txn

    @contextlib.contextmanager
    def observed_reserver(database):
        events.append("anchor-enter")
        with original_reserver(database) as reserve:
            yield reserve
        events.append("anchor-exit")

    @contextlib.contextmanager
    def observed_write_txn(connection, *args, **kwargs):
        events.append("database-enter")
        with original_write_txn(connection, *args, **kwargs):
            yield connection
        events.append("database-exit")

    monkeypatch.setattr(epic_state, "_fence_token_reserver", observed_reserver)
    monkeypatch.setattr(kb, "write_txn", observed_write_txn)

    assert lease(conn) == 1
    assert events == [
        "anchor-enter",
        "database-enter",
        "database-exit",
        "anchor-exit",
    ]


def test_held_fence_anchor_lock_fails_closed_within_numeric_deadline(tmp_path):
    db_path = tmp_path / "board.db"
    conn = open_db(db_path)
    assert lease(conn, scope="seed", op="seed-lease") == 1
    conn.close()
    anchor = epic_state._fence_anchor_path(db_path)
    before_anchor = anchor.read_bytes()
    lock_path = anchor.with_name(anchor.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = lock_path.open("a+b")

    if os.name == "nt":
        import msvcrt

        holder.seek(0)
        if holder.read(1) == b"":
            holder.seek(0)
            holder.write(b"\0")
            holder.flush()
        holder.seek(0)
        msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    script = """
import json
import sys
import time
from pathlib import Path
from hermes_cli import epic_state
from hermes_cli import kanban_db as kb

conn = kb.connect(Path(sys.argv[1]))
started = time.monotonic()
try:
    epic_state.acquire_lease(
        conn,
        operation_id="held-lock-lease",
        scope="held",
        owner="contender",
        expected_token=0,
        expires_at=200,
        now=100,
    )
except epic_state.LeaseConflict as exc:
    print(json.dumps({"elapsed": time.monotonic() - started, "error": str(exc)}))
    raise SystemExit(0)
raise SystemExit(2)
"""
    result = None
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(db_path)],
            cwd=Path(__file__).parents[2],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("fence-anchor acquisition exceeded the bounded deadline")
    finally:
        if os.name == "nt":
            holder.seek(0)
            msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    assert result is not None
    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert getattr(epic_state, "FENCE_ANCHOR_LOCK_TIMEOUT_SECONDS", None) == 1.0
    assert 0.75 <= payload["elapsed"] < 2.5
    assert "fence anchor lock" in payload["error"].lower()
    assert anchor.read_bytes() == before_anchor
    check = sqlite3.connect(db_path)
    try:
        assert check.execute(
            "SELECT token FROM epic_leases WHERE scope = 'held'"
        ).fetchone() is None
    finally:
        check.close()


def test_delegated_lease_refusal_precedes_all_fence_anchor_filesystem_writes(tmp_path):
    db_path = tmp_path / "board.db"
    conn = open_db(db_path)
    anchor = epic_state._fence_anchor_path(db_path)
    lock_path = anchor.with_name(anchor.name + ".lock")

    with delegated_child_context(), pytest.raises(
        PermissionError,
        match="delegate_task child contexts cannot mutate",
    ):
        lease(conn)

    assert not anchor.exists()
    assert not lock_path.exists()


def test_rollback_and_lost_response_replay_are_recoverable(tmp_path):
    conn = open_db(tmp_path / "board.db")
    lease(conn)
    def explode(): raise RuntimeError("before commit")
    with pytest.raises(RuntimeError, match="before commit"):
        append(conn, before_commit=explode)
    assert conn.execute("select count(*) from epic_receipts where operation_id='write-1'").fetchone()[0] == 0
    assert conn.execute("select count(*) from epic_evidence").fetchone()[0] == 0
    durable = append(conn)
    assert append(conn) == durable  # caller-lost response is reconciled by operation id


def test_unknown_requires_distinct_reconciliation(tmp_path):
    conn = open_db(tmp_path / "board.db")
    lease(conn)
    unknown = append(conn, op="uncertain", outcome="unknown", payload={"attempt": 1})
    assert append(conn, op="uncertain", outcome="unknown", payload={"attempt": 1}) == unknown
    with pytest.raises(epic_state.OperationConflict):
        append(conn, op="uncertain", outcome="success", payload={"attempt": 1})
    reconciled = append(conn, op="reconcile", outcome="success", reconcile="uncertain", payload={"terminal": True})
    assert reconciled["reconciliation_ref"] == "uncertain"


def test_unknown_accepts_only_one_terminal_reconciliation(tmp_path):
    conn = open_db(tmp_path / "board.db")
    lease(conn)
    append(conn, op="uncertain", outcome="unknown", payload={"attempt": 1})
    append(
        conn,
        op="reconcile-success",
        outcome="success",
        reconcile="uncertain",
        payload={"terminal": True},
    )

    with pytest.raises(epic_state.OperationConflict, match="already reconciled"):
        append(
            conn,
            op="reconcile-failure",
            outcome="failure",
            reconcile="uncertain",
            payload={"terminal": True},
        )


def test_evidence_substitution_missing_and_corruption_fail_closed(tmp_path):
    conn = open_db(tmp_path / "board.db")
    lease(conn)
    digest = epic_state.sha256_bytes(b"proof")
    with pytest.raises(epic_state.IntegrityError):
        append(conn, op="substitute", evidence=b"other", evidence_digest=digest)
    receipt = append(conn)
    assert epic_state.read_evidence(conn, receipt["evidence_digest"]) == b"proof"
    conn.execute("drop trigger epic_evidence_no_update")
    conn.execute("update epic_evidence set body=? where digest=?", (b"corrupt", receipt["evidence_digest"]))
    with pytest.raises(epic_state.IntegrityError): epic_state.read_evidence(conn, receipt["evidence_digest"])
    conn.rollback()
    conn.execute("drop trigger epic_evidence_no_delete")
    conn.commit()
    conn.execute("pragma foreign_keys=off")
    conn.execute("delete from epic_evidence where digest=?", (receipt["evidence_digest"],))
    with pytest.raises(epic_state.IntegrityError): epic_state.validate_integrity(conn)


def test_immutable_rows_and_chain_substitution_are_rejected_or_detected(tmp_path):
    conn = open_db(tmp_path / "board.db")
    lease(conn); append(conn)
    for sql in ("update epic_receipts set outcome='failure'", "delete from epic_receipts", "update epic_evidence set media_type='x'", "delete from epic_evidence"):
        with pytest.raises(sqlite3.IntegrityError): conn.execute(sql)
    conn.execute("drop trigger epic_receipts_no_update")
    conn.execute("update epic_receipts set payload_json='{\"tampered\":true}' where operation_id='write-1'")
    with pytest.raises(epic_state.IntegrityError): epic_state.validate_integrity(conn)


def test_schema_addition_preserves_kanban_and_is_idempotent_recoverable(tmp_path, monkeypatch):
    path = tmp_path / "board.db"
    conn = open_db(path)
    task_id = kb.create_task(conn, title="kept", initial_status="running")
    conn.close()
    conn = open_db(path)
    assert kb.get_task(conn, task_id).title == "kept"
    bootstrap(conn)
    bootstrap(conn)
    conn.execute("drop table epic_receipts")  # interrupted/partial additive init simulation
    bootstrap(conn)
    assert conn.execute("select name from sqlite_master where name='epic_receipts'").fetchone()


def test_schema_initialization_refuses_caller_transaction_without_committing_it(tmp_path):
    path = tmp_path / "board.db"
    conn = open_db(path)
    conn.execute("create table caller_state(value text)")
    conn.execute("begin")
    conn.execute("insert into caller_state values ('uncommitted')")

    with pytest.raises(RuntimeError, match="active transaction"):
        bootstrap(conn)
    assert conn.in_transaction is True
    observer = sqlite3.connect(path)
    assert observer.execute("select count(*) from caller_state").fetchone()[0] == 0
    observer.close()
    conn.rollback()


def test_schema_contract_rejects_stale_same_named_trigger(tmp_path):
    conn = open_db(tmp_path / "board.db")
    conn.execute("drop trigger epic_receipts_no_delete")
    conn.execute(
        "create trigger epic_receipts_no_delete before delete on epic_receipts "
        "begin select 1; end"
    )

    with pytest.raises(epic_state.IntegrityError, match="schema contract"):
        bootstrap(conn)


def test_chain_head_anchor_detects_valid_prefix_tail_deletion(tmp_path):
    conn = open_db(tmp_path / "board.db")
    lease(conn)
    append(conn, op="first")
    append(conn, op="second", payload={"n": 2})
    conn.execute("drop trigger epic_receipts_no_delete")
    conn.execute("delete from epic_receipts where operation_id='second'")
    conn.execute(
        "create trigger if not exists epic_receipts_no_delete before delete on epic_receipts "
        "begin select raise(abort, 'epic receipts are append-only'); end"
    )

    with pytest.raises(epic_state.IntegrityError, match="chain head"):
        epic_state.validate_integrity(conn)


def test_evidence_digest_rejects_conflicting_media_type(tmp_path):
    conn = open_db(tmp_path / "board.db")
    lease(conn)
    append(conn, op="text", media_type="text/plain")

    with pytest.raises(epic_state.IntegrityError, match="metadata"):
        append(conn, op="json", media_type="application/json")


def test_cached_connect_does_not_rerun_epic_schema_initialization(tmp_path, monkeypatch):
    path = tmp_path / "board.db"
    open_db(path).close()
    calls = []
    original = epic_state.initialize_schema

    def track(conn, *, authority):
        calls.append("called")
        return original(conn, authority=authority)

    monkeypatch.setattr(epic_state, "initialize_schema", track)
    open_db(path).close()
    assert calls == []


def test_backup_restore_invalidates_leases_and_refuses_bad_or_existing(tmp_path):
    source = tmp_path / "source.db"
    conn = open_db(source); lease(conn); append(conn)
    backup = tmp_path / "backup.db"
    make_backup(conn, backup)
    restored = tmp_path / "restored.db"
    perform_restore(backup, restored)
    restored_conn = open_db(restored)
    assert epic_state.validate_integrity(restored_conn)
    row = restored_conn.execute("select owner, token, expires_at from epic_leases where scope='s'").fetchone()
    assert tuple(row) == (None, 2, 0)
    recovery = restored_conn.execute(
        "select owner, fence_token, operation, outcome, payload_json "
        "from epic_receipts where kind='recovery' and scope='s'"
    ).fetchone()
    assert recovery is not None
    assert tuple(recovery[:4]) == ("__restore__", 2, "restore_service", "success")
    assert recovery["payload_json"] == '{"new_token":2,"previous_token":1}'
    with pytest.raises(epic_state.FenceRejected): append(restored_conn, op="old", token=1)
    with pytest.raises(FileExistsError): perform_restore(backup, restored)
    bad = tmp_path / "bad.db"; bad.write_bytes(b"not sqlite")
    with pytest.raises(epic_state.IntegrityError): perform_restore(bad, tmp_path / "never.db")
    assert not (tmp_path / "never.db").exists()


def test_backup_and_restore_publish_without_clobbering_concurrent_target(tmp_path):
    source = tmp_path / "source.db"
    conn = open_db(source)
    lease(conn)
    append(conn)

    def create_winner(path):
        with sqlite3.connect(path) as winner:
            winner.execute("create table concurrent_winner(value integer)")
            winner.execute("insert into concurrent_winner values (1)")

    backup_target = tmp_path / "backup-race.db"
    with pytest.raises(FileExistsError):
        make_backup(conn, backup_target, _before_publish=create_winner)
    with sqlite3.connect(backup_target) as winner:
        assert winner.execute("select value from concurrent_winner").fetchone()[0] == 1

    clean_backup = tmp_path / "clean-backup.db"
    make_backup(conn, clean_backup)
    restore_target = tmp_path / "restore-race.db"
    with pytest.raises(FileExistsError):
        perform_restore(
            clean_backup,
            restore_target,
            _before_publish=create_winner,
        )
    with sqlite3.connect(restore_target) as winner:
        assert winner.execute("select value from concurrent_winner").fetchone()[0] == 1


def test_restore_fence_anchor_survives_database_rollback_at_same_path(tmp_path):
    source = tmp_path / "source.db"
    conn = open_db(source)
    assert lease(conn) == 1
    backup = tmp_path / "backup.db"
    make_backup(conn, backup)

    restored = tmp_path / "authoritative.db"
    perform_restore(backup, restored)
    first = open_db(restored)
    first_restore_token = first.execute(
        "select token from epic_leases where scope='s'"
    ).fetchone()[0]
    issued_after_restore = epic_state.acquire_lease(
        first,
        operation_id="post-restore-owner",
        scope="s",
        owner="post-restore",
        expected_token=first_restore_token,
        expires_at=500,
        now=300,
    )
    first.close()
    restored.unlink()
    for suffix in ("-wal", "-shm", "-journal"):
        restored.with_name(restored.name + suffix).unlink(missing_ok=True)

    perform_restore(backup, restored)
    second = open_db(restored)
    repeated_restore_token = second.execute(
        "select token from epic_leases where scope='s'"
    ).fetchone()[0]
    assert repeated_restore_token > issued_after_restore
    assert epic_state.acquire_lease(
        second,
        operation_id="after-repeat-restore",
        scope="s",
        owner="new-owner",
        expected_token=repeated_restore_token,
        expires_at=800,
        now=600,
    ) > repeated_restore_token


def test_empty_restore_still_has_recovery_fence_and_receipt(tmp_path):
    source = tmp_path / "empty.db"
    conn = open_db(source)
    backup = tmp_path / "empty-backup.db"
    make_backup(conn, backup)
    restored = tmp_path / "empty-restored.db"
    perform_restore(backup, restored)

    restored_conn = open_db(restored)
    receipt = restored_conn.execute(
        "select scope,owner,kind,operation from epic_receipts "
        "where scope='__recovery__'"
    ).fetchone()
    assert tuple(receipt) == (
        "__recovery__",
        "__restore__",
        "recovery",
        "restore_service",
    )


def test_schema_contract_rejects_same_columns_without_check_constraint(tmp_path):
    conn = open_db(tmp_path / "stale-table.db")
    conn.execute("drop table epic_leases")
    conn.execute(
        "create table epic_leases ("
        "scope text primary key, owner text, token integer not null, "
        "expires_at integer not null)"
    )
    with pytest.raises(epic_state.IntegrityError, match="schema contract"):
        epic_state.validate_integrity(conn)
    with pytest.raises(epic_state.IntegrityError, match="schema contract"):
        bootstrap(conn, "reject-stale-table")
    conn.execute(
        "insert into epic_leases(scope,owner,token,expires_at) values('bad','x',-1,0)"
    )
    assert conn.execute("select token from epic_leases").fetchone()[0] == -1


def test_schema_contract_rejects_wrong_reconciliation_index_expression(tmp_path):
    conn = open_db(tmp_path / "stale-index.db")
    conn.execute("drop index epic_receipts_one_reconciliation")
    conn.execute(
        "create unique index epic_receipts_one_reconciliation "
        "on epic_receipts(operation_id) where reconciliation_ref is not null"
    )
    with pytest.raises(epic_state.IntegrityError, match="schema contract"):
        epic_state.validate_integrity(conn)
    with pytest.raises(epic_state.IntegrityError, match="schema contract"):
        bootstrap(conn, "reject-stale-index")


@pytest.mark.parametrize(
    "ddl",
    [
        "create index unprefixed_receipt_index on epic_receipts(operation_id)",
        "create trigger unprefixed_receipt_trigger before insert on epic_receipts "
        "begin select 1; end",
    ],
)
def test_schema_contract_rejects_unprefixed_authored_objects_on_epic_tables(
    tmp_path, ddl
):
    conn = open_db(tmp_path / ("unprefixed-" + ddl.split()[1] + ".db"))
    conn.execute(ddl)

    with pytest.raises(epic_state.IntegrityError, match="schema contract"):
        epic_state.validate_integrity(conn)
    with pytest.raises(epic_state.IntegrityError, match="schema contract"):
        bootstrap(conn, "reject-" + ddl.split()[2])


def test_schema_contract_uses_literal_epic_prefix_and_ignores_lookalikes(tmp_path):
    conn = sqlite3.connect(tmp_path / "lookalike.db")
    conn.row_factory = sqlite3.Row
    conn.execute("create table epicX_unrelated(id integer primary key)")

    epic_state.initialize_schema(
        conn,
        authority=maintenance("schema_bootstrap", "literal-prefix-bootstrap"),
    )

    assert epic_state.validate_integrity(conn) is True
    assert conn.execute(
        "select count(*) from sqlite_schema where name='epicX_unrelated'"
    ).fetchone()[0] == 1


def test_schema_contract_digest_binds_validator_policy_label(monkeypatch):
    original = epic_state.schema_contract_digest()
    monkeypatch.setattr(
        epic_state,
        "_SCHEMA_CONTRACT_LABEL",
        epic_state._SCHEMA_CONTRACT_LABEL + "-changed",
    )
    assert epic_state.schema_contract_digest() != original


def test_schema_history_binds_reference_digest_actor_and_success(tmp_path):
    conn = open_db(tmp_path / "history.db")
    digest = epic_state.schema_contract_digest()
    meta = conn.execute(
        "select schema_version,contract_digest from epic_schema_meta where singleton=1"
    ).fetchone()
    assert tuple(meta) == (epic_state.SCHEMA_VERSION, digest)
    rows = conn.execute(
        "select operation,actor,outcome,schema_digest from epic_maintenance_receipts "
        "where operation='schema_bootstrap'"
    ).fetchall()
    assert rows
    assert all(row["outcome"] == "success" and row["schema_digest"] == digest for row in rows)


def test_schema_bootstrap_requires_explicit_correct_nondelegated_authority(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "authority.db")
    conn.row_factory = sqlite3.Row
    with pytest.raises(epic_state.MaintenanceAuthorityError):
        epic_state.initialize_schema(conn, authority=None)
    with pytest.raises(epic_state.MaintenanceAuthorityError, match="schema_bootstrap"):
        epic_state.initialize_schema(
            conn,
            authority=maintenance("backup", "wrong-bootstrap-authority"),
        )
    expired = epic_state.MaintenanceAuthority(
        operation_id="expired-bootstrap",
        actor="expired-maintainer",
        reason="must fail",
        issued_at=1,
        expires_at=2,
        allowed_operation="schema_bootstrap",
    )
    with pytest.raises(epic_state.MaintenanceAuthorityError, match="not currently valid"):
        epic_state.initialize_schema(conn, authority=expired)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    with pytest.raises(PermissionError):
        bootstrap(conn, "delegated-bootstrap")


def test_maintenance_authority_lifetime_is_positive_and_capped_at_five_minutes(
    tmp_path, monkeypatch
):
    now = 10_000
    monkeypatch.setattr(epic_state.time, "time", lambda: now)

    accepted = sqlite3.connect(tmp_path / "five-minute-authority.db")
    accepted.row_factory = sqlite3.Row
    epic_state.initialize_schema(
        accepted,
        authority=maintenance(
            "schema_bootstrap",
            "five-minute-authority",
            issued_at=now - 100,
            lifetime=300,
        ),
    )

    for lifetime in (0, 301, 10 * 365 * 24 * 60 * 60):
        rejected = sqlite3.connect(tmp_path / f"rejected-authority-{lifetime}.db")
        rejected.row_factory = sqlite3.Row
        with pytest.raises(epic_state.MaintenanceAuthorityError, match="lifetime"):
            epic_state.initialize_schema(
                rejected,
                authority=maintenance(
                    "schema_bootstrap",
                    f"rejected-authority-{lifetime}",
                    issued_at=now - 100,
                    lifetime=lifetime,
                ),
            )
        assert rejected.execute(
            "select count(*) from sqlite_schema where name like 'epic_%'"
        ).fetchone()[0] == 0


def test_bootstrap_receipt_is_append_only_and_replay_idempotent(tmp_path):
    conn = open_db(tmp_path / "bootstrap-replay.db")
    bootstrap(conn, "manual-bootstrap-replay")
    bootstrap(conn, "manual-bootstrap-replay")
    assert conn.execute(
        "select count(*) from epic_maintenance_receipts where operation_id=?",
        ("manual-bootstrap-replay",),
    ).fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("delete from epic_maintenance_receipts")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("update epic_maintenance_receipts set actor='tampered'")


def test_backup_requires_authority_and_records_intent_completion_and_digest(tmp_path):
    conn = open_db(tmp_path / "backup-source.db")
    destination = tmp_path / "authorized-backup.db"
    with pytest.raises(epic_state.MaintenanceAuthorityError):
        epic_state.backup_service(conn, destination, authority=None)
    with pytest.raises(epic_state.MaintenanceAuthorityError, match="backup"):
        epic_state.backup_service(
            conn,
            destination,
            authority=maintenance("schema_bootstrap", "wrong-backup-authority"),
        )

    make_backup(conn, destination, operation_id="authorized-backup")
    artifact_digest = epic_state.sha256_bytes(destination.read_bytes())
    rows = conn.execute(
        "select operation_id,operation,actor,outcome,target_identity,artifact_digest "
        "from epic_maintenance_receipts where operation_id like 'authorized-backup:%' "
        "order by id"
    ).fetchall()
    assert [row["operation"] for row in rows] == ["backup_intent", "backup_complete"]
    assert [row["outcome"] for row in rows] == ["unknown", "success"]
    assert rows[-1]["artifact_digest"] == artifact_digest
    assert rows[-1]["target_identity"] == str(destination.resolve())
    assert all(row["actor"] == "phase0-v3-test-maintainer" for row in rows)


def test_backup_temp_cleanup_failure_does_not_erase_known_completion(
    tmp_path, monkeypatch
):
    conn = open_db(tmp_path / "cleanup-failure-source.db")
    destination = tmp_path / "published-with-cleanup-failure.db"
    original_unlink = Path.unlink

    def fail_backup_temp_cleanup(path, *args, **kwargs):
        if path.parent == tmp_path and path.name.endswith(".backup"):
            raise OSError("injected backup temp cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_backup_temp_cleanup)
    assert make_backup(
        conn,
        destination,
        operation_id="cleanup-failure-backup",
    ) == destination
    assert destination.is_file()
    completion = conn.execute(
        "select outcome,artifact_digest from epic_maintenance_receipts "
        "where operation_id='cleanup-failure-backup:complete'"
    ).fetchone()
    assert tuple(completion) == (
        "success",
        epic_state.sha256_bytes(destination.read_bytes()),
    )


def test_backup_completion_receipt_failure_is_outcome_unknown(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "receipt-failure-source.db")
    destination = tmp_path / "published-without-completion-receipt.db"
    original_record = epic_state._record_maintenance

    def fail_completion_receipt(conn, **kwargs):
        if kwargs["operation"] == "backup_complete":
            raise sqlite3.OperationalError("injected completion receipt failure")
        return original_record(conn, **kwargs)

    monkeypatch.setattr(epic_state, "_record_maintenance", fail_completion_receipt)
    with pytest.raises(epic_state.OutcomeUnknownError, match="published"):
        make_backup(conn, destination, operation_id="receipt-failure-backup")

    assert destination.is_file()
    assert conn.execute(
        "select count(*) from epic_maintenance_receipts "
        "where operation_id='receipt-failure-backup:complete'"
    ).fetchone()[0] == 0


def test_backup_hash_failure_after_publication_is_outcome_unknown(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "hash-failure-source.db")
    destination = tmp_path / "published-without-digest.db"

    def fail_hash(_path):
        raise OSError("injected post-publication hashing failure")

    monkeypatch.setattr(epic_state, "_sha256_file", fail_hash)
    with pytest.raises(epic_state.OutcomeUnknownError, match="published"):
        make_backup(conn, destination, operation_id="hash-failure-backup")

    assert destination.is_file()
    rows = conn.execute(
        "select operation,outcome from epic_maintenance_receipts "
        "where operation_id like 'hash-failure-backup:%' order by id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("backup_intent", "unknown")]


def test_backup_conflicting_authority_replay_and_known_failure_fail_closed(tmp_path):
    conn = open_db(tmp_path / "backup-conflict.db")
    first = tmp_path / "first.db"
    make_backup(conn, first, operation_id="same-backup-operation")
    with pytest.raises(epic_state.OperationConflict):
        make_backup(
            conn,
            tmp_path / "different.db",
            operation_id="same-backup-operation",
        )

    race = tmp_path / "race.db"
    def create_competitor(path):
        sqlite3.connect(path).close()
    with pytest.raises(FileExistsError):
        make_backup(
            conn,
            race,
            operation_id="failed-backup-operation",
            _before_publish=create_competitor,
        )
    failure = conn.execute(
        "select outcome from epic_maintenance_receipts where operation_id=?",
        ("failed-backup-operation:failure",),
    ).fetchone()
    assert failure is not None and failure["outcome"] == "failure"
