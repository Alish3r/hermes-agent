"""Behavior contract for the single-machine durable epic state service."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

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
           evidence_digest=None, reconcile=None, before_commit=None):
    return epic_state.append_receipt(
        conn, operation_id=op, scope=scope, operation="record",
        owner=owner, fence_token=token, kind="state", outcome=outcome,
        payload={} if payload is None else payload, evidence_bytes=evidence,
        evidence_digest=evidence_digest, media_type="application/octet-stream",
        reconciliation_ref=reconcile, created_at=now, now=now,
        before_commit=before_commit,
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
    epic_state.initialize_schema(conn)
    epic_state.initialize_schema(conn)
    conn.execute("drop table epic_receipts")  # interrupted/partial additive init simulation
    epic_state.initialize_schema(conn)
    assert conn.execute("select name from sqlite_master where name='epic_receipts'").fetchone()


def test_schema_initialization_refuses_caller_transaction_without_committing_it(tmp_path):
    path = tmp_path / "board.db"
    conn = open_db(path)
    conn.execute("create table caller_state(value text)")
    conn.execute("begin")
    conn.execute("insert into caller_state values ('uncommitted')")

    with pytest.raises(RuntimeError, match="active transaction"):
        epic_state.initialize_schema(conn)
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
        epic_state.initialize_schema(conn)


def test_schema_contract_rejects_same_named_table_without_its_constraints(tmp_path):
    """Same columns, same types, same PK — but the CHECK is gone.

    A column-shape comparison passes this and then stores a negative fence
    token. The contract is the table's DDL, not its column list.
    """
    conn = open_db(tmp_path / "board.db")
    conn.execute("drop table epic_leases")
    conn.execute(
        "create table epic_leases "
        "(scope TEXT PRIMARY KEY, owner TEXT, token INTEGER, expires_at INTEGER)"
    )

    with pytest.raises(epic_state.IntegrityError, match="schema contract"):
        epic_state.initialize_schema(conn)


def test_schema_contract_rejects_same_named_index_on_a_different_expression(tmp_path):
    """Still UNIQUE, still the same partial predicate — indexing the wrong column.

    Substring-matching the predicate accepts this while the one-reconciliation
    invariant it is supposed to enforce is gone.
    """
    conn = open_db(tmp_path / "board.db")
    conn.execute("drop index epic_receipts_one_reconciliation")
    conn.execute(
        "create unique index epic_receipts_one_reconciliation "
        "on epic_receipts(operation_id) where reconciliation_ref is not null"
    )

    with pytest.raises(epic_state.IntegrityError, match="schema contract"):
        epic_state.initialize_schema(conn)


def test_schema_contract_rejects_trigger_keeping_the_shape_and_message(tmp_path):
    """The adversarial trigger: right shape, right message text, no ABORT.

    ``before delete on epic_receipts`` and the append-only wording are both
    present, so a substring check passes — but the trigger selects the message
    instead of raising it, and the delete goes through.
    """
    conn = open_db(tmp_path / "board.db")
    conn.execute("drop trigger epic_receipts_no_delete")
    conn.execute(
        "create trigger epic_receipts_no_delete before delete on epic_receipts "
        "begin select 'epic receipts are append-only'; end"
    )

    with pytest.raises(epic_state.IntegrityError, match="schema contract"):
        epic_state.initialize_schema(conn)


def test_schema_contract_accepts_the_schema_it_installs(tmp_path):
    """Positive control: the authoritative DDL must validate against itself."""
    conn = open_db(tmp_path / "board.db")
    epic_state.initialize_schema(conn)
    epic_state.initialize_schema(conn)


def test_schema_contract_rejects_an_unexpected_epic_object(tmp_path):
    """Anything the derivation does not know about must fail loudly.

    The expected set is parsed out of ``_SCHEMA``. If the parser ever fails to
    recognise a statement — a leading comment, a quoted identifier, a CREATE
    VIEW, a missing terminal semicolon — that object would simply be absent
    from the expectations and go unvalidated forever. Checking the other
    direction as well turns every one of those silent gaps into a startup
    error, and rejects an attacker-planted extra object for free.
    """
    conn = open_db(tmp_path / "board.db")
    conn.execute("create table epic_shadow_ledger (id INTEGER PRIMARY KEY)")

    with pytest.raises(epic_state.IntegrityError, match="unexpected"):
        epic_state.initialize_schema(conn)


def test_schema_contract_rejects_a_trigger_whose_message_only_differs_in_case(tmp_path):
    """Case is meaning inside a string literal, so comparison must preserve it."""
    conn = open_db(tmp_path / "board.db")
    conn.execute("drop trigger epic_receipts_no_update")
    conn.execute(
        "CREATE TRIGGER epic_receipts_no_update BEFORE UPDATE ON epic_receipts "
        "BEGIN SELECT RAISE(ABORT, 'EPIC RECEIPTS ARE APPEND-ONLY'); END"
    )

    with pytest.raises(epic_state.IntegrityError, match="schema contract"):
        epic_state.initialize_schema(conn)


def test_privileged_direct_mutations_are_exactly_the_documented_ones():
    """The module contract must name every mutation that bypasses write_txn.

    Service mutations run inside ``kanban_db.write_txn`` — directly, or in a
    private helper invoked under a caller's transaction. Two operations write
    durable state outside one, using primitives a transaction cannot cover:
    ``executescript`` (schema bootstrap, which predates the schema) and
    ``os.link`` (backup publication, whose artifact is a file, not a row).

    This pins the escape set. A third bypass fails here until it is documented
    in the module contract, so the docstring cannot quietly go stale again.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(epic_state))
    escapes: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Attribute):
                continue
            value = inner.func.value
            if inner.func.attr == "executescript":
                escapes.setdefault(node.name, set()).add("executescript")
            elif (
                inner.func.attr == "link"
                and isinstance(value, ast.Name)
                and value.id == "os"
            ):
                escapes.setdefault(node.name, set()).add("os.link")

    assert escapes == {
        "initialize_schema": {"executescript"},
        "_publish_noreplace": {"os.link"},
    }, escapes

    contract = epic_state.__doc__ or ""
    for named in ("initialize_schema", "backup_service", "write_txn"):
        assert named in contract, f"module contract does not name {named}"


def test_chain_head_anchor_detects_valid_prefix_tail_deletion(tmp_path):
    conn = open_db(tmp_path / "board.db")
    lease(conn)
    append(conn, op="first")
    append(conn, op="second", payload={"n": 2})
    conn.execute("drop trigger epic_receipts_no_delete")
    conn.execute("delete from epic_receipts where operation_id='second'")

    with pytest.raises(epic_state.IntegrityError, match="chain head"):
        epic_state.validate_integrity(conn)


def test_cached_connect_does_not_rerun_epic_schema_initialization(tmp_path, monkeypatch):
    path = tmp_path / "board.db"
    open_db(path).close()
    calls = []
    original = epic_state.initialize_schema

    def track(conn):
        calls.append("called")
        return original(conn)

    monkeypatch.setattr(epic_state, "initialize_schema", track)
    open_db(path).close()
    assert calls == []


def test_backup_restore_invalidates_leases_and_refuses_bad_or_existing(tmp_path):
    source = tmp_path / "source.db"
    conn = open_db(source); lease(conn); append(conn)
    backup = tmp_path / "backup.db"
    epic_state.backup_service(conn, backup)
    restored = tmp_path / "restored.db"
    epic_state.restore_service(backup, restored)
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
    with pytest.raises(FileExistsError): epic_state.restore_service(backup, restored)
    bad = tmp_path / "bad.db"; bad.write_bytes(b"not sqlite")
    with pytest.raises(epic_state.IntegrityError): epic_state.restore_service(bad, tmp_path / "never.db")
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
        epic_state.backup_service(conn, backup_target, _before_publish=create_winner)
    with sqlite3.connect(backup_target) as winner:
        assert winner.execute("select value from concurrent_winner").fetchone()[0] == 1

    clean_backup = tmp_path / "clean-backup.db"
    epic_state.backup_service(conn, clean_backup)
    restore_target = tmp_path / "restore-race.db"
    with pytest.raises(FileExistsError):
        epic_state.restore_service(
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
    epic_state.backup_service(conn, backup)

    restored = tmp_path / "authoritative.db"
    epic_state.restore_service(backup, restored)
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

    epic_state.restore_service(backup, restored)
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
    epic_state.backup_service(conn, backup)
    restored = tmp_path / "empty-restored.db"
    epic_state.restore_service(backup, restored)

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
