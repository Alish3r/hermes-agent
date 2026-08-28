"""Tests for async (background) delegation — tools/async_delegation.py.

Covers the dispatch handle, non-blocking behavior, completion-event delivery
onto the shared process_registry.completion_queue, the rich re-injection block
formatting, capacity rejection, and crash handling.
"""

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from agent.completion_envelope import UntrustedCompletionEnvelope
from tools import async_delegation as ad
from tools.process_registry import process_registry, format_process_notification


@pytest.fixture(autouse=True)
def _clean_state():
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    # Give just-released workers a beat to finalize BEFORE draining, so their
    # completion events land now instead of leaking into the next test's
    # queue (worker threads push events asynchronously; a drain that races an
    # in-flight _finalize misses it).
    deadline = time.monotonic() + 2.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _drain_one(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


def _drain_for(delegation_id, timeout=5.0):
    """Drain until the event for *delegation_id* appears (discarding others).

    Completion events are pushed asynchronously by worker threads, so a
    straggler from a PREVIOUS test can land after that test's teardown drain
    and leak into the current test's queue. Matching on delegation_id makes
    the assertion immune to that cross-test leak.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            evt = process_registry.completion_queue.get_nowait()
            if evt.get("delegation_id") == delegation_id:
                return evt
            continue
        time.sleep(0.02)
    return None


def test_active_for_session_counts_every_live_delegation_state():
    with ad._records_lock:
        ad._records.update(
            {
                "running": {
                    "status": "running",
                    "origin_ui_session_id": "desktop-sid",
                },
                "stalling": {
                    "status": "stalling",
                    "origin_ui_session_id": "desktop-sid",
                },
                "finalizing": {
                    "status": "finalizing",
                    "origin_ui_session_id": "desktop-sid",
                },
                "completed": {
                    "status": "completed",
                    "origin_ui_session_id": "desktop-sid",
                },
                "other-session": {
                    "status": "running",
                    "origin_ui_session_id": "other-sid",
                },
            }
        )

    assert ad.active_for_session("desktop-sid") == 3
    assert ad.active_for_session("other-sid") == 1
    assert ad.active_for_session("") == 0


def test_dispatch_returns_immediately_without_blocking():
    gate = threading.Event()

    def runner():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "done", "api_calls": 1,
                "duration_seconds": 0.1, "model": "m"}

    t0 = time.monotonic()
    res = ad.dispatch_async_delegation(
        goal="g", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=3,
    )
    elapsed = time.monotonic() - t0

    assert res["status"] == "dispatched"
    assert res["delegation_id"].startswith("deleg_")
    # Non-blocking invariant: dispatch returned while the runner is still
    # gated (active), so it cannot have waited on the gate. The active_count
    # check is the environment-independent proof; the generous wall-clock
    # bound is a loose sanity backstop, not the primary assertion (a loaded
    # CI runner can be slow but never anywhere near the runner's 5s gate).
    assert ad.active_count() == 1
    assert elapsed < 4.0, f"dispatch blocked {elapsed:.2f}s (gate is 5s)"
    gate.set()


def test_async_executor_workers_are_daemon_threads():
    gate = threading.Event()

    def runner():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "done"}

    res = ad.dispatch_async_delegation(
        goal="daemon check", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=1,
    )
    assert res["status"] == "dispatched"

    deadline = time.monotonic() + 2
    worker = None
    while time.monotonic() < deadline:
        worker = next(
            (t for t in threading.enumerate() if t.name.startswith("async-delegate")),
            None,
        )
        if worker is not None:
            break
        time.sleep(0.02)
    assert worker is not None
    assert worker.daemon is True
    gate.set()
    assert _drain_one() is not None


def test_completion_event_lands_on_shared_queue_with_session_key():
    def runner():
        return {"status": "completed", "summary": "the result",
                "api_calls": 3, "duration_seconds": 2.0, "model": "test-model"}

    res = ad.dispatch_async_delegation(
        goal="compute X", context="some context", toolsets=["web", "file"],
        role="leaf", model="test-model", session_key="agent:main:cli:dm:local",
        parent_session_id="20260703_parent_sid",
        runner=runner, max_async_children=3,
    )
    assert res["status"] == "dispatched"

    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt["summary"] == "the result"
    assert evt["session_key"] == "agent:main:cli:dm:local"
    assert evt["parent_session_id"] == "20260703_parent_sid"
    assert evt["delegation_id"] == res["delegation_id"]


def test_dispatch_and_completion_write_canonical_reaped_allocation(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ad, "_db_path", lambda: db_path)

    res = ad.dispatch_async_delegation(
        goal="canonical lifecycle",
        context=None,
        toolsets=None,
        role="leaf",
        model="test-model",
        session_key="parent-session",
        parent_session_id="parent-session",
        runner=lambda: {
            "status": "completed",
            "summary": "receipt-backed result",
            "verdict": "GO",
            "api_calls": 1,
        },
        max_async_children=1,
    )
    evt = _drain_for(res["delegation_id"])
    assert evt is not None
    assert evt["allocation_id"] == res["delegation_id"]
    assert evt["root_allocation_id"] == res["delegation_id"]
    assert evt["parent_allocation_id"] is None
    assert evt["task_state"] == "complete"
    assert evt["verdict"] == "GO"
    assert evt["resource_state"] == "reaped"
    assert evt["receipt_digest"]

    from tools.orchestration_ledger import OrchestrationLedger

    row = OrchestrationLedger(db_path).get(res["delegation_id"])
    assert row["state"] == "reaped"
    assert row["task_state"] == "complete"
    assert row["verdict"] == "GO"
    assert row["resource_receipt_digest"]


def test_queue_publication_failure_keeps_durable_terminal_receipt(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ad, "_db_path", lambda: db_path)
    record = {
        "delegation_id": "deleg_queue_failure",
        "goal": "durability probe",
        "context": None,
        "toolsets": None,
        "role": "leaf",
        "model": "test-model",
        "session_key": "parent",
        "origin_ui_session_id": "",
        "origin_session_id": "",
        "parent_session_id": "parent",
        "status": "running",
        "dispatched_at": time.time() - 1,
        "completed_at": time.time(),
    }
    ad._persist_dispatch(record)
    monkeypatch.setattr(
        process_registry.completion_queue,
        "put",
        lambda _event: (_ for _ in ()).throw(RuntimeError("queue unavailable")),
    )

    ad._push_completion_event(
        record,
        {"status": "completed", "summary": "durably complete", "api_calls": 1},
        "completed",
    )

    with sqlite3.connect(db_path) as con:
        state, delivery_state, event_json = con.execute(
            "SELECT state, delivery_state, event_json FROM async_delegations "
            "WHERE delegation_id='deleg_queue_failure'"
        ).fetchone()
    assert state == "completed"
    assert delivery_state == "pending"
    assert json.loads(event_json)["receipt_digest"]

    from tools.orchestration_ledger import OrchestrationLedger

    assert OrchestrationLedger(db_path).get("deleg_queue_failure")["state"] == "reaped"


def test_executor_submit_failure_retains_batch_allocations(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ad, "_db_path", lambda: db_path)

    class BrokenExecutor:
        def submit(self, *_args, **_kwargs):
            raise RuntimeError("executor unavailable")

    monkeypatch.setattr(ad, "_get_executor", lambda _workers: BrokenExecutor())
    monkeypatch.setattr(ad, "_new_delegation_id", lambda: "deleg_submit_failure")
    result = ad.dispatch_async_delegation_batch(
        goals=["one", "two"],
        context=None,
        toolsets=None,
        role="leaf",
        model=None,
        session_key="parent",
        parent_session_id="parent",
        runner=lambda: {"status": "completed"},
        max_async_children=2,
    )
    assert result["status"] == "rejected"

    from tools.orchestration_ledger import OrchestrationLedger

    ledger = OrchestrationLedger(db_path)
    assert ledger.get("deleg_submit_failure")["state"] == "retained_diagnostic"
    assert ledger.get("deleg_submit_failure_0")["state"] == "retained_diagnostic"
    assert ledger.get("deleg_submit_failure_1")["state"] == "retained_diagnostic"
    with sqlite3.connect(db_path) as con:
        state, delivery, adjudication = con.execute(
            """SELECT state, delivery_state, adjudication_state
                 FROM async_delegations WHERE delegation_id='deleg_submit_failure'"""
        ).fetchone()
    assert (state, delivery, adjudication) == ("rejected", "delivered", "adjudicated")


def test_batch_allocates_and_reaps_one_canonical_child_per_result(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ad, "_db_path", lambda: db_path)
    record = {
        "delegation_id": "deleg_batch",
        "origin_session": "cli",
        "session_key": "cli",
        "origin_ui_session_id": "cli",
        "parent_session_id": "parent",
        "origin_session_id": "parent",
        "status": "running",
        "dispatched_at": time.time(),
        "is_batch": True,
        "goals": ["one", "two"],
        "child_roles": ["orchestrator", "leaf"],
    }
    ad._persist_dispatch(record)
    results = [
        {
            "status": "completed",
            "summary": "one done",
            "child_session_id": "child-1",
            "allocation_id": "deleg_batch_0",
        },
        {
            "status": "completed",
            "summary": "two done",
            "child_session_id": "child-2",
            "allocation_id": "deleg_batch_1",
        },
    ]
    ad._persist_completion(
        {
            **record,
            "status": "completed",
            "completed_at": time.time(),
        },
        {"status": "completed", "results": results, "summary": "batch done"},
    )

    from tools.orchestration_ledger import OrchestrationLedger

    ledger = OrchestrationLedger(db_path)
    group = ledger.get("deleg_batch")
    children = ledger.descendants("deleg_batch")
    assert group["state"] == "reaped"
    assert [
        (c["allocation_id"], c["owner_session_id"], c["role"], c["state"])
        for c in children
    ] == [
        ("deleg_batch_0", "child-1", "orchestrator", "reaped"),
        ("deleg_batch_1", "child-2", "leaf", "reaped"),
    ]


def test_synthetic_completion_adjudication_is_durable(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ad, "_db_path", lambda: db_path)
    ad._persist_dispatch(
        {
            "delegation_id": "deleg_adjudicate",
            "origin_session": "cli",
            "origin_ui_session_id": "cli",
            "parent_session_id": "parent",
            "origin_session_id": "parent",
            "status": "running",
            "dispatched_at": time.time(),
            "goal": "review",
        }
    )
    ad._persist_completion(
        {
            "delegation_id": "deleg_adjudicate",
            "status": "completed",
            "completed_at": time.time(),
        },
        {"status": "completed", "summary": "done"},
    )
    assert ad.adjudicate_completion_message(
        "[ASYNC DELEGATION COMPLETE — deleg_adjudicate]\nresult", success=True
    )
    durable = ad.get_durable_delegation("deleg_adjudicate")
    assert durable is not None
    assert durable["adjudication_state"] == "adjudicated"
    assert durable["adjudicated_at"] is not None


def test_duplicate_completion_is_idempotent_and_conflict_is_rejected(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ad, "_db_path", lambda: db_path)
    record = {
        "delegation_id": "deleg_duplicate",
        "session_key": "parent",
        "parent_session_id": "parent",
        "status": "running",
        "dispatched_at": time.time(),
        "goal": "duplicate",
    }
    ad._persist_dispatch(record)
    result = {"status": "completed", "summary": "first", "child_session_id": "child"}
    event = {
        **record,
        "type": "async_delegation",
        "status": "completed",
        "completed_at": time.time(),
        "allocation_id": "deleg_duplicate",
    }
    ad._persist_completion(dict(event), dict(result))
    ad._persist_completion(dict(event), dict(result))

    from tools.orchestration_ledger import OrchestrationLedger

    ledger = OrchestrationLedger(db_path)
    with sqlite3.connect(db_path) as con:
        event_count = con.execute(
            "SELECT count(*) FROM orchestration_events WHERE allocation_id='deleg_duplicate'"
        ).fetchone()[0]
    assert event_count == 3  # allocation + terminal receipt + reap receipt
    assert ledger.get("deleg_duplicate")["state"] == "reaped"

    with pytest.raises(RuntimeError, match="conflicting duplicate completion"):
        ad._persist_completion(dict(event), {**result, "summary": "different"})
    durable = ad.get_durable_delegation("deleg_duplicate")
    assert durable is not None
    assert durable["result"]["summary"] == "first"


def test_restart_recovery_converges_terminal_transport_evidence(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ad, "_db_path", lambda: db_path)
    record = {
        "delegation_id": "deleg_crash_gap",
        "session_key": "parent",
        "parent_session_id": "parent",
        "status": "running",
        "dispatched_at": time.time(),
        "goal": "recover",
    }
    ad._persist_dispatch(record)
    result = {"status": "completed", "summary": "durable", "child_session_id": "child"}
    event = {
        **record,
        "type": "async_delegation",
        "status": "completed",
        "completed_at": time.time(),
        "allocation_id": "deleg_crash_gap",
    }
    with ad._transaction() as conn:
        conn.execute(
            """UPDATE async_delegations SET state='completed', event_json=?, result_json=?,
                      completed_at=?, updated_at=? WHERE delegation_id=?""",
            (json.dumps(event), json.dumps(result), time.time(), time.time(), "deleg_crash_gap"),
        )

    from tools.orchestration_ledger import OrchestrationLedger

    assert OrchestrationLedger(db_path).get("deleg_crash_gap")["state"] == "running"
    ad.recover_abandoned_delegations()
    assert OrchestrationLedger(db_path).get("deleg_crash_gap")["state"] == "reaped"


def test_failed_batch_child_is_retained_while_successful_sibling_reaps(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ad, "_db_path", lambda: db_path)
    record = {
        "delegation_id": "deleg_mixed_batch",
        "session_key": "parent",
        "parent_session_id": "parent",
        "status": "running",
        "dispatched_at": time.time(),
        "is_batch": True,
        "goals": ["ok", "fail"],
    }
    ad._persist_dispatch(record)
    results = [
        {"status": "completed", "summary": "ok", "child_session_id": "child-ok",
         "allocation_id": "deleg_mixed_batch_0"},
        {"status": "error", "error": "boom", "child_session_id": "child-fail",
         "allocation_id": "deleg_mixed_batch_1"},
    ]
    ad._persist_completion(
        {**record, "status": "completed", "completed_at": time.time()},
        {"status": "completed", "summary": "mixed", "results": results},
    )

    from tools.orchestration_ledger import OrchestrationLedger

    ledger = OrchestrationLedger(db_path)
    assert ledger.get("deleg_mixed_batch_0")["state"] == "reaped"
    failed = ledger.get("deleg_mixed_batch_1")
    assert failed["state"] == "terminal_failure"
    assert failed["resource_state"] == "retained"
    assert ledger.get("deleg_mixed_batch")["state"] == "reaped"


def test_durable_collector_sees_live_work_without_in_memory_record(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ad, "_db_path", lambda: db_path)
    ad._reset_for_tests()
    record = {
        "delegation_id": "deleg_durable_collector",
        "session_key": "parent",
        "parent_session_id": "parent-session",
        "status": "running",
        "dispatched_at": time.time(),
        "goal": "trusted collection",
    }
    ad._persist_dispatch(record)
    assert ad.list_async_delegations() == []
    assert ad.has_live_for_session(parent_session_id="parent-session") is True
    event = {
        **record,
        "status": "completed",
        "completed_at": time.time(),
    }
    result = {
        "status": "completed",
        "summary": "done",
        "child_session_id": "child-session",
    }
    ad._persist_completion(event, result)
    assert ad.has_live_for_session(parent_session_id="parent-session") is True
    assert ad.mark_completion_adjudicated(
        "deleg_durable_collector", success=True
    ) is True
    assert ad.has_live_for_session(parent_session_id="parent-session") is False
    # A stale transport row cannot resurrect canonical work after adjudication.
    with ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET state='running' WHERE delegation_id=?",
            ("deleg_durable_collector",),
        )
    assert ad.has_live_for_session(parent_session_id="parent-session") is False
    from tools.orchestration_ledger import OrchestrationLedger

    allocation = OrchestrationLedger(db_path).get("deleg_durable_collector")
    assert allocation["launching_session_id"] == "parent-session"
    assert allocation["owner_session_id"] == "child-session"
    assert allocation["adjudication_state"] == "adjudicated"


def test_rich_reinjection_block_is_self_contained():
    def runner():
        return {"status": "completed", "summary": "The answer is 42.",
                "api_calls": 7, "duration_seconds": 3.5, "model": "test-model"}

    ad.dispatch_async_delegation(
        goal="Compute the meaning of life",
        context="User is a philosopher. Respond tersely.",
        toolsets=["web"], role="leaf", model="test-model",
        session_key="", runner=runner, max_async_children=3,
    )
    evt = _drain_one()
    assert evt is not None
    text = format_process_notification(evt)
    assert text is not None
    for needle in [
        "ASYNC DELEGATION COMPLETE",
        "Compute the meaning of life",
        "Toolsets: web",
        "The answer is 42.",
        "Status: completed",
        "API calls: 7",
    ]:
        assert needle in text, f"missing {needle!r}"
    assert "User is a philosopher" not in text
    assert "dispatch context omitted" in text.lower()


def test_dispatch_rejected_at_capacity():
    ev = threading.Event()

    def blocker():
        ev.wait(timeout=60)
        return {"status": "completed", "summary": "x"}

    for i in range(2):
        r = ad.dispatch_async_delegation(
            goal=f"task{i}", context=None, toolsets=None, role="leaf",
            model="m", session_key="", runner=blocker, max_async_children=2,
        )
        assert r["status"] == "dispatched"

    r3 = ad.dispatch_async_delegation(
        goal="task3", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=blocker, max_async_children=2,
    )
    assert r3["status"] == "rejected"
    assert "capacity reached" in r3["error"]
    ev.set()


def test_interrupt_all_signals_running_children():
    ev = threading.Event()
    interrupted = {"count": 0}
    # No short internal timeout: the blocker holds until interrupt_fn fires.
    # The old ev.wait(timeout=5) made this test a change-detector for CI
    # worker load — on a CPU-starved runner the 5s expired before
    # interrupt_all() ran, the record finalized, and interrupt_all() found
    # nothing running (n == 0). The pytest-level timeout is the real
    # runaway guard.

    def blocker():
        ev.wait(timeout=60)
        return {"status": "interrupted", "summary": None,
                "error": "cancelled"}

    def interrupt_fn():
        interrupted["count"] += 1
        ev.set()

    r = ad.dispatch_async_delegation(
        goal="long task", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=blocker,
        interrupt_fn=interrupt_fn, max_async_children=3,
    )
    n = ad.interrupt_all(reason="test")
    assert n == 1
    assert interrupted["count"] == 1
    # child still emits a completion event after interrupt. Match on THIS
    # delegation's id — straggler 'completed' events from a previous test's
    # workers can finalize after that test's teardown drain and leak into
    # this queue (observed on loaded CI workers).
    evt = _drain_for(r["delegation_id"])
    assert evt is not None
    assert evt["status"] == "interrupted"


def _fast_stale_monitor(monkeypatch, *, idle=0.15, in_tool=0.3, grace=0.15):
    """Shrink the stale-monitor cadence so tests run in milliseconds."""
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.03)
    monkeypatch.setattr(ad, "_STALE_IDLE_SECONDS", idle)
    monkeypatch.setattr(ad, "_STALE_IN_TOOL_SECONDS", in_tool)
    monkeypatch.setattr(ad, "_STALL_GRACE_SECONDS", grace)


def test_stalled_runner_is_interrupted_then_finalized(monkeypatch):
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    interrupted = {"count": 0}

    def stuck_runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "too late"}

    def interrupt_fn():
        interrupted["count"] += 1

    res = ad.dispatch_async_delegation(
        goal="stuck child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=stuck_runner,
        interrupt_fn=interrupt_fn, max_async_children=1,
        # Frozen progress token: the child never advances an API call.
        progress_fn=lambda: ((0, None), False),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    try:
        assert evt is not None
        assert evt["type"] == "async_delegation"
        assert evt["status"] == "stalled"
        assert evt["delegation_id"] == res["delegation_id"]
        assert evt["api_calls"] == 0
        assert "stalled" in evt["error"]
        # Interrupt was requested BEFORE force-finalization (grace window).
        assert interrupted["count"] >= 1
        assert ad.active_count() == 0
    finally:
        gate.set()

    # If the ignored runner eventually returns, it must not enqueue a second
    # completion for a delegation the monitor already finalized.
    assert _drain_one(timeout=0.5) is None


def test_progressing_runner_is_never_stalled(monkeypatch):
    """A child that keeps advancing is left alone no matter how long it runs."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    ticks = {"n": 0}

    def slow_but_alive_runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "done", "api_calls": 7}

    def progress_fn():
        # Token advances on every sample — simulates a child making steady
        # API-call progress.
        ticks["n"] += 1
        return (ticks["n"], None), False

    res = ad.dispatch_async_delegation(
        goal="slow child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=slow_but_alive_runner,
        max_async_children=1, progress_fn=progress_fn,
    )
    assert res["status"] == "dispatched"

    # Run well past the (shrunk) idle threshold — several monitor sweeps.
    time.sleep(0.6)
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"
    assert evt["summary"] == "done"


def test_stalling_runner_that_honors_interrupt_keeps_its_result(monkeypatch):
    """Interrupt-responsive children finalize through the NORMAL path.

    The monitor's interrupt gives a wedged-looking child a grace window; if
    the runner returns during it, the real result (partial work, api_calls)
    is delivered instead of a synthetic stalled event.
    """
    _fast_stale_monitor(monkeypatch, grace=5.0)
    interrupted = threading.Event()

    def runner():
        # "Wedged" until interrupted, then unwinds and reports partial work.
        interrupted.wait(timeout=10)
        return {
            "status": "interrupted",
            "summary": "partial work saved",
            "api_calls": 3,
        }

    res = ad.dispatch_async_delegation(
        goal="responsive child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=runner,
        interrupt_fn=interrupted.set, max_async_children=1,
        progress_fn=lambda: ((3, None), False),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "interrupted"
    assert evt["summary"] == "partial work saved"
    assert evt["api_calls"] == 3
    assert ad.active_count() == 0


def test_streaming_child_counts_as_alive(monkeypatch):
    """A child mid-stream (api_call_count frozen, last_activity_ts ticking)
    must never be stalled — streamed chunks tick _touch_activity, and the
    progress token includes that timestamp (same liveness signal as the
    compaction inactivity budget, PR #71508)."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    now = {"ts": 1000.0}

    def progress_fn():
        # api_call_count and current_tool frozen (long streaming response in
        # flight), but the activity timestamp advances with every chunk.
        now["ts"] += 1.0
        return ((1, None, now["ts"]),), False

    res = ad.dispatch_async_delegation(
        goal="streaming child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: (gate.wait(timeout=10), {"status": "completed", "summary": "streamed"})[1],
        progress_fn=progress_fn,
    )
    assert res["status"] == "dispatched"

    time.sleep(0.6)  # several sweeps past the shrunk idle threshold
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"


def test_stalled_event_carries_structured_stall_metadata(monkeypatch):
    """The terminal stalled event must expose machine-readable stall context
    (#51690) — quiet duration, tripped threshold, phase, grace — mirroring
    the sync path's timeout_seconds/timed_out_after_seconds/timeout_phase."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()

    res = ad.dispatch_async_delegation(
        goal="stall metadata", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: {} if gate.wait(timeout=10) else {},
        progress_fn=lambda: ((0, "terminal"), True),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    try:
        assert evt is not None
        assert evt["status"] == "stalled"
        assert evt["stalled_after_quiet_seconds"] >= 0.3  # in-tool threshold
        assert evt["stall_threshold_seconds"] == ad._STALE_IN_TOOL_SECONDS
        assert evt["stall_phase"] == "in_tool"
        assert evt["stall_grace_seconds"] == ad._STALL_GRACE_SECONDS
    finally:
        gate.set()


def test_list_async_delegations_exposes_live_activity(monkeypatch):
    """list_async_delegations must expose per-child live activity sampled
    from progress_fn plus seconds_since_progress, for /agents UIs (#51690)."""
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.03)
    gate = threading.Event()
    base_ts = time.time() - 12.0

    res = ad.dispatch_async_delegation(
        goal="live listing", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: {} if gate.wait(timeout=10) else {},
        progress_fn=lambda: (((3, "web_search", base_ts),), True),
    )
    try:
        time.sleep(0.1)  # let the monitor stamp _progress_ts at least once
        item = next(
            d for d in ad.list_async_delegations()
            if d["delegation_id"] == res["delegation_id"]
        )
        assert item["status"] == "running"
        assert item["in_tool"] is True
        assert "seconds_since_progress" in item
        (child,) = item["children_activity"]
        assert child["api_calls"] == 3
        assert child["current_tool"] == "web_search"
        assert 10.0 <= child["seconds_since_activity"] <= 20.0
        # Callables and private bookkeeping must never leak.
        assert "progress_fn" not in item
        assert "interrupt_fn" not in item
        assert not any(k.startswith("_") for k in item)
    finally:
        gate.set()


def test_in_tool_stall_uses_higher_threshold(monkeypatch):
    """A frozen child inside a tool gets the in-tool ceiling, not the idle one."""
    _fast_stale_monitor(monkeypatch, idle=0.1, in_tool=10.0, grace=0.1)
    gate = threading.Event()

    def runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "long tool finished"}

    res = ad.dispatch_async_delegation(
        goal="long tool child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=runner, max_async_children=1,
        # Frozen token but in_tool=True — a legitimately slow terminal
        # command / web fetch. Must NOT be stalled at the idle threshold.
        progress_fn=lambda: ((1, "terminal"), True),
    )
    assert res["status"] == "dispatched"

    time.sleep(0.5)  # far past idle threshold, well under in-tool threshold
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"


def test_real_process_restart_restores_owned_completion_once(tmp_path):
    """Real-import E2E: a fresh interpreter restores a prior process's result."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": repo}
    producer = r'''
import time
from tools import async_delegation as ad
r = ad.dispatch_async_delegation(
    goal="restart", context=None, toolsets=None, role="leaf", model="m",
    session_key="owner-session", parent_session_id="durable-parent",
    runner=lambda: {"status": "completed", "summary": "after restart"},
)
deadline = time.time() + 5
while ad.active_count() and time.time() < deadline:
    time.sleep(.01)
print(r["delegation_id"])
'''
    first = subprocess.run(
        [sys.executable, "-c", producer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    delegation_id = first.stdout.strip().splitlines()[-1]

    consumer = r'''
import json
from tools.process_registry import process_registry
evt = process_registry.completion_queue.get_nowait()
print(json.dumps(evt, sort_keys=True))
'''
    second = subprocess.run(
        [sys.executable, "-c", consumer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    evt = json.loads(second.stdout.strip().splitlines()[-1])
    assert evt["delegation_id"] == delegation_id
    assert evt["session_key"] == "owner-session"
    assert evt["parent_session_id"] == "durable-parent"
    assert evt["summary"] == "after restart"

    acker = f'''
from tools import async_delegation as ad
assert ad.mark_completion_delivered({delegation_id!r})
'''
    subprocess.run(
        [sys.executable, "-c", acker], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    probe = subprocess.run(
        [sys.executable, "-c", "from tools.process_registry import process_registry; print(process_registry.completion_queue.qsize())"],
        cwd=repo, env=env, text=True, capture_output=True, timeout=15, check=True,
    )
    assert probe.stdout.strip().splitlines()[-1] == "0"


# ---------------------------------------------------------------------------
# Integration: delegate_task(background=True) routing
# ---------------------------------------------------------------------------

def test_delegate_task_background_routes_async_and_does_not_block(monkeypatch):
    """delegate_task(background=True) returns a handle without running the
    child synchronously, and the child completes on the background thread.
    A single task is dispatched as a one-item background batch unit."""
    from unittest.mock import MagicMock, patch
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    fake_child._subagent_id = "s1"

    gate = threading.Event()

    def slow_child(task_index, goal, child=None, parent_agent=None, **kw):
        gate.wait(timeout=60)  # a sync impl would hang delegate_task here
        return {
            "task_index": 0, "status": "completed", "summary": f"done: {goal}",
            "api_calls": 1, "duration_seconds": 0.1, "model": "m",
            "exit_reason": "completed",
        }

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    # monkeypatch (not `with`) so patches outlive delegate_task's return and
    # remain active while the background worker runs.
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_run_single_child", slow_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    out = dt.delegate_task(
        goal="the real task", context="ctx",
        background=True, parent_agent=parent,
    )

    import json
    parsed = json.loads(out)
    assert parsed["status"] == "dispatched"
    assert parsed["mode"] == "background"
    assert parsed["delegation_id"].startswith("deleg_")
    # Non-blocking invariant: delegate_task returned while the child is STILL
    # blocked on the closed gate, so no completion event exists yet.
    assert process_registry.completion_queue.empty()
    assert ad.active_count() == 1  # one background batch unit, not finished

    gate.set()
    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    # Single task rides the batch path → carries a 1-item results list.
    assert evt.get("is_batch") is True
    assert len(evt["results"]) == 1
    assert evt["results"][0]["summary"] == "done: the real task"
    text = format_process_notification(evt)
    assert text is not None
    assert "the real task" in text


def test_delegate_task_background_uses_live_tui_agent_session_id(monkeypatch):
    """TUI async delegation must route to the live/compressed agent id.

    Regression: delegate_task captured the stale approval/session context key
    after compression rotated parent_agent.session_id. The resulting completion
    was orphaned and could be consumed by an unrelated desktop session poller.
    """
    import json
    from unittest.mock import MagicMock
    import tools.delegate_tool as dt
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.approval import reset_current_session_key, set_current_session_key

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "post-compress-tip"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    monkeypatch.setattr(
        dt,
        "_run_single_child",
        lambda *a, **k: {
            "task_index": 0,
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "model": "m",
            "exit_reason": "completed",
        },
    )

    approval_token = set_current_session_key("pre-compress-parent")
    session_tokens = set_session_vars(
        source="tui",
        session_key="pre-compress-parent",
        ui_session_id="origin-tab",
    )
    try:
        out = dt.delegate_task(goal="bg task", background=True, parent_agent=parent)
        assert json.loads(out)["status"] == "dispatched"
        evt = _drain_one()
    finally:
        reset_current_session_key(approval_token)
        clear_session_vars(session_tokens)

    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt["session_key"] == "post-compress-tip"
    assert evt["origin_ui_session_id"] == "origin-tab"


def test_concurrent_dispatch_respects_capacity():
    """Two threads racing dispatch with cap=1 must yield exactly one accept
    (capacity check and record insert are atomic under the records lock)."""
    gate = threading.Event()

    def blocker():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "x"}

    results = []
    barrier = threading.Barrier(2)

    def racer():
        barrier.wait(timeout=5)
        results.append(
            ad.dispatch_async_delegation(
                goal="race", context=None, toolsets=None, role="leaf",
                model="m", session_key="", runner=blocker,
                max_async_children=1,
            )
        )

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["dispatched", "rejected"]
    gate.set()


# ---------------------------------------------------------------------------
# Gateway routing: session_key -> platform/chat_id, rich formatting, injection
# ---------------------------------------------------------------------------

def _make_async_evt(**over):
    evt = {
        "type": "async_delegation",
        "delegation_id": "deleg_x1",
        "session_key": "agent:main:telegram:dm:12345:678",
        "goal": "Investigate flaky test",
        "context": "repo /tmp/p",
        "toolsets": ["terminal"],
        "role": "leaf",
        "model": "m",
        "status": "completed",
        "summary": "Found the bug in test_foo",
        "api_calls": 4,
        "duration_seconds": 12.0,
        "dispatched_at": 1000.0,
        "completed_at": 1012.0,
    }
    evt.update(over)
    return evt


def test_gateway_formatter_renders_async_block():
    from gateway.run import _format_gateway_process_notification

    txt = _format_gateway_process_notification(_make_async_evt())
    assert txt is not None
    assert isinstance(txt, UntrustedCompletionEnvelope)
    assert "INTERNAL ASYNC COMPLETION — UNTRUSTED DATA" in txt
    assert "never independently authorizes side effects" in txt
    assert "Found the bug in test_foo" in txt
    assert "Investigate flaky test" in txt


def test_gateway_cli_origin_event_left_unrouted():
    """An empty session_key (CLI origin) is left without routing fields."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    evt = _make_async_evt(session_key="")
    runner._enrich_async_delegation_routing(evt)
    assert "platform" not in evt


def test_single_task_truncation_banner_when_max_iterations():
    """A single async subagent that hit its iteration cap (exit_reason=
    max_iterations) must surface a TRUNCATED marker in the formatted result,
    even though status stays 'completed' (a summary exists)."""
    evt = _make_async_evt(
        status="completed",
        summary="Did part of the work then ran out of budget.",
        exit_reason="max_iterations",
    )
    text = format_process_notification(evt)
    assert text is not None
    assert "TRUNCATED" in text
    assert "max_iterations" in text
    # The summary is still shown, just flagged.
    assert "Did part of the work" in text


def test_single_task_no_banner_when_clean():
    """A cleanly-finished subagent must NOT get a truncation banner."""
    evt = _make_async_evt(status="completed", summary="All done.", exit_reason="completed")
    text = format_process_notification(evt)
    assert text is not None
    assert "TRUNCATED" not in text


def test_batch_truncation_banner_marks_only_truncated_task():
    """In a batch, only the task that hit max_iterations gets the TRUNCATED
    marker; a clean sibling keeps the normal check icon."""
    evt = _make_async_evt(
        is_batch=True,
        goals=["clean task", "truncated task"],
        results=[
            {
                "task_index": 0,
                "status": "completed",
                "summary": "finished cleanly",
                "api_calls": 5,
                "exit_reason": "completed",
                "truncated": False,
            },
            {
                "task_index": 1,
                "status": "completed",
                "summary": "cut off mid-work",
                "api_calls": 250,
                "exit_reason": "max_iterations",
                "truncated": True,
            },
        ],
    )
    text = format_process_notification(evt)
    assert text is not None
    assert "TRUNCATED" in text
    # The clean task's summary and the truncated one's both render...
    assert "finished cleanly" in text
    assert "cut off mid-work" in text
    # ...but the banner is tied to the truncated task, not the clean one.
    trunc_pos = text.index("cut off mid-work")
    clean_pos = text.index("finished cleanly")
    banner_pos = text.index("TRUNCATED")
    # The header banner for task 2 appears after task 1's summary.
    assert banner_pos > clean_pos

