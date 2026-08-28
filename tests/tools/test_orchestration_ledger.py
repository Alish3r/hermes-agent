"""Contract tests for the durable orchestration allocation ledger."""

from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest

from tools.orchestration_ledger import (
    FinalizationBlocked,
    GenerationMismatch,
    InvalidTransition,
    OrchestrationLedger,
)


def _ledger(tmp_path):
    return OrchestrationLedger(tmp_path / "state.db", owner_pid=os.getpid())


def test_nested_allocations_inherit_root_and_block_parent_finalization(tmp_path):
    ledger = _ledger(tmp_path)
    root = ledger.allocate(
        allocation_id="deleg_root",
        owner_session_id="session-root",
        role="orchestrator",
        operation_id="launch-root",
    )
    child = ledger.allocate(
        allocation_id="deleg_child",
        owner_session_id="session-child",
        parent_allocation_id=root["allocation_id"],
        role="orchestrator",
        operation_id="launch-child",
    )
    grandchild = ledger.allocate(
        allocation_id="deleg_grandchild",
        owner_session_id="session-grandchild",
        parent_allocation_id=child["allocation_id"],
        role="leaf",
        operation_id="launch-grandchild",
    )

    assert child["root_allocation_id"] == root["allocation_id"]
    assert grandchild["root_allocation_id"] == root["allocation_id"]
    assert grandchild["depth"] == 2

    gate = ledger.finalization_gate(child["allocation_id"])
    assert not gate["allowed"]
    assert gate["active_descendants"] == [grandchild["allocation_id"]]


def test_success_receipt_is_revision_bound_and_cleanup_must_follow_descendants(tmp_path):
    ledger = _ledger(tmp_path)
    root = ledger.allocate(
        allocation_id="deleg_root",
        owner_session_id="session-root",
        role="orchestrator",
        operation_id="launch-root",
    )
    child = ledger.allocate(
        allocation_id="deleg_child",
        owner_session_id="session-child",
        parent_allocation_id=root["allocation_id"],
        role="leaf",
        operation_id="launch-child",
    )

    child_generation = child["generation"]
    receipt = ledger.record_terminal_receipt(
        child["allocation_id"],
        expected_generation=child_generation,
        operation_id="terminal-child",
        task_state="complete",
        verdict="GO",
        terminal_reason="normal",
        result={"summary": "verified child result"},
    )
    assert receipt["state"] == "terminal_success"
    assert receipt["receipt_digest"]
    assert json.loads(receipt["terminal_receipt_json"])["result"]["summary"] == "verified child result"

    gate = ledger.finalization_gate(root["allocation_id"])
    assert gate["unreconciled_successful_descendants"] == [child["allocation_id"]]

    reaped = ledger.mark_resource_reaped(
        child["allocation_id"],
        expected_generation=receipt["generation"],
        operation_id="reap-child",
        resource_receipt={"kind": "process", "verified_absent": True},
    )
    assert reaped["resource_state"] == "reaped"
    assert ledger.finalization_gate(root["allocation_id"])["allowed"]


def test_idempotent_operation_and_generation_fencing(tmp_path):
    ledger = _ledger(tmp_path)
    first = ledger.allocate(
        allocation_id="deleg_root",
        owner_session_id="session-root",
        role="orchestrator",
        operation_id="launch-root",
    )
    replay = ledger.allocate(
        allocation_id="deleg_root",
        owner_session_id="session-root",
        role="orchestrator",
        operation_id="launch-root",
    )
    assert replay == first

    terminal = ledger.record_terminal_receipt(
        first["allocation_id"],
        expected_generation=first["generation"],
        operation_id="terminal-root",
        task_state="complete",
        verdict="NO-GO",
        terminal_reason="normal",
        result={"summary": "review completed with no-go"},
    )
    with pytest.raises(GenerationMismatch):
        ledger.mark_resource_reaped(
            first["allocation_id"],
            expected_generation=first["generation"],
            operation_id="stale-reap",
            resource_receipt={"verified_absent": True},
        )
    assert terminal["generation"] > first["generation"]


def test_illegal_transition_and_active_descendant_terminal_success_fail_closed(tmp_path):
    ledger = _ledger(tmp_path)
    parent = ledger.allocate(
        allocation_id="deleg_parent",
        owner_session_id="session-parent",
        role="orchestrator",
        operation_id="launch-parent",
    )
    ledger.allocate(
        allocation_id="deleg_child",
        owner_session_id="session-child",
        parent_allocation_id=parent["allocation_id"],
        role="leaf",
        operation_id="launch-child",
    )

    with pytest.raises(FinalizationBlocked):
        ledger.record_terminal_receipt(
            parent["allocation_id"],
            expected_generation=parent["generation"],
            operation_id="terminal-parent",
            task_state="complete",
            verdict="GO",
            terminal_reason="normal",
            result={"summary": "too early"},
        )

    with pytest.raises(InvalidTransition):
        ledger.transition(
            parent["allocation_id"],
            expected_generation=parent["generation"],
            operation_id="bad-transition",
            new_state="reaped",
            event={"why": "illegal shortcut"},
        )


def test_task_local_lineage_drives_real_nested_async_allocation(monkeypatch, tmp_path):
    from agent.delegation_context import delegated_child_context
    from tools import async_delegation as ad
    from tools.process_registry import process_registry

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ad, "_db_path", lambda: db_path)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()

    ledger = OrchestrationLedger(db_path)
    parent = ledger.allocate(
        allocation_id="deleg_parent",
        owner_session_id="root-session",
        role="orchestrator",
        operation_id="launch-parent",
    )
    with delegated_child_context("child-orchestrator", parent["allocation_id"]):
        dispatched = ad.dispatch_async_delegation(
            goal="grandchild work",
            context=None,
            toolsets=None,
            role="leaf",
            model="test-model",
            session_key="child-orchestrator",
            parent_session_id="child-orchestrator",
            runner=lambda: {
                "status": "completed",
                "summary": "grandchild complete",
                "api_calls": 1,
            },
            max_async_children=1,
        )
    assert dispatched["status"] == "dispatched"

    deadline = time.monotonic() + 5
    event = None
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            candidate = process_registry.completion_queue.get_nowait()
            if candidate.get("delegation_id") == dispatched["delegation_id"]:
                event = candidate
                break
        time.sleep(0.01)
    assert event is not None

    child = ledger.get(dispatched["delegation_id"])
    assert child["parent_allocation_id"] == parent["allocation_id"]
    assert child["root_allocation_id"] == parent["allocation_id"]
    assert child["state"] == "reaped"
    assert ledger.finalization_gate(parent["allocation_id"])["allowed"]

    ad._reset_for_tests()


def test_trusted_process_collector_checks_pid_start_identity(tmp_path):
    ledger = _ledger(tmp_path)
    allocation = ledger.allocate(
        allocation_id="deleg_process",
        owner_session_id="session-process",
        role="leaf",
        operation_id="launch-process",
    )
    live = ledger.collect_live_state(allocation["allocation_id"])
    assert live["owner_process"]["pid"] == os.getpid()
    assert live["owner_process"]["exists"] is True
    assert live["owner_process"]["identity_match"] is True

    with sqlite3.connect(tmp_path / "state.db") as conn:
        conn.execute(
            "UPDATE orchestration_allocations SET owner_started_at=owner_started_at+1 WHERE allocation_id=?",
            (allocation["allocation_id"],),
        )
        conn.commit()
    drifted = ledger.collect_live_state(allocation["allocation_id"])
    assert drifted["owner_process"]["exists"] is True
    assert drifted["owner_process"]["identity_match"] is False


def test_pid_reuse_identity_is_retained_not_reaped(tmp_path):
    ledger = _ledger(tmp_path)
    allocation = ledger.allocate(
        allocation_id="deleg_reused_pid",
        owner_session_id="session-reused",
        role="leaf",
        operation_id="launch-reused",
    )
    with sqlite3.connect(tmp_path / "state.db") as conn:
        conn.execute(
            "UPDATE orchestration_allocations SET owner_started_at=owner_started_at+1 "
            "WHERE allocation_id=?",
            (allocation["allocation_id"],),
        )
        conn.commit()

    assert ledger.recover_stale_owners() == [allocation["allocation_id"]]
    recovered = ledger.get(allocation["allocation_id"])
    assert recovered["state"] == "retained_diagnostic"
    assert recovered["task_state"] == "unknown"
    assert recovered["resource_state"] == "retained"
    assert recovered["terminal_reason"] == "owner_identity_lost"


def test_dispatch_transport_failure_retains_allocations(monkeypatch, tmp_path):
    from tools import async_delegation as ad

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ad, "_db_path", lambda: db_path)
    with ad._transaction() as conn:
        conn.execute(
            """INSERT INTO async_delegations
               (delegation_id, origin_session, state, dispatched_at, updated_at)
               VALUES ('deleg_collision', 'old', 'completed', 1, 1)"""
        )
    record = {
        "delegation_id": "deleg_collision",
        "session_key": "parent",
        "parent_session_id": "parent",
        "role": "leaf",
        "dispatched_at": time.time(),
    }

    with pytest.raises(sqlite3.IntegrityError):
        ad._persist_dispatch(record)

    retained = OrchestrationLedger(db_path).get("deleg_collision")
    assert retained["state"] == "retained_diagnostic"
    assert retained["resource_state"] == "retained"


def test_legacy_schema_migration_matches_direct_and_sessiondb(tmp_path):
    from hermes_state import SessionDB
    from tools import async_delegation as ad

    central_path = tmp_path / "central.db"
    with sqlite3.connect(central_path) as conn:
        conn.executescript("""
            CREATE TABLE async_delegations (
                delegation_id TEXT PRIMARY KEY, origin_session TEXT NOT NULL,
                origin_ui_session_id TEXT NOT NULL DEFAULT '', parent_session_id TEXT,
                state TEXT NOT NULL, dispatched_at REAL NOT NULL, completed_at REAL,
                updated_at REAL NOT NULL, event_json TEXT, result_json TEXT,
                delivery_state TEXT NOT NULL DEFAULT 'pending',
                delivery_attempts INTEGER NOT NULL DEFAULT 0, delivered_at REAL,
                owner_pid INTEGER, owner_started_at INTEGER, task_json TEXT,
                delivery_claim TEXT, delivery_claimed_at REAL
            );
            CREATE TABLE orchestration_allocations (allocation_id TEXT PRIMARY KEY);
        """)
    session_db = SessionDB(db_path=central_path)
    session_db.close()

    direct_path = tmp_path / "direct.db"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ad, "_db_path", lambda: direct_path)
    try:
        with ad._transaction():
            pass
        OrchestrationLedger(direct_path)
    finally:
        monkeypatch.undo()

    def columns(path, table):
        with sqlite3.connect(path) as conn:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    def column_specs(path, table):
        with sqlite3.connect(path) as conn:
            return {
                row[1]: (row[2], row[3], row[4], row[5])
                for row in conn.execute(f"PRAGMA table_info({table})")
            }

    assert column_specs(central_path, "async_delegations") == column_specs(
        direct_path, "async_delegations"
    )
    assert columns(central_path, "orchestration_allocations") == columns(
        direct_path, "orchestration_allocations"
    )
    assert {"adjudication_state", "adjudicated_at", "adjudication_error"} <= columns(
        central_path, "async_delegations"
    )
