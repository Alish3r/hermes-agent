from cron.executions import (
    UnknownExecutionBlocked,
    acknowledge_unknown_execution,
    create_execution,
    execution_counters,
    execution_is_blocked,
    recover_interrupted_executions,
)


def test_unknown_recovery_blocks_until_explicit_reconciliation():
    import cron.executions as executions

    row = create_execution("job-unknown", source="test")
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=?, process_started_at=NULL WHERE id=?",
            ("dead-owner", 999999, row["id"]),
        )
    assert recover_interrupted_executions() == 1
    assert execution_is_blocked("job-unknown")
    assert execution_counters()["unknown"] >= 1
    assert acknowledge_unknown_execution("job-unknown", row["id"])
    assert not execution_is_blocked("job-unknown")


def test_shared_execution_creation_gate_rejects_unknown_even_without_a_scheduler():
    import cron.executions as executions
    import pytest

    row = create_execution("job-direct-bypass", source="test")
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=?, process_started_at=NULL WHERE id=?",
            ("dead-owner", 999999, row["id"]),
        )
    assert recover_interrupted_executions() == 1
    with pytest.raises(UnknownExecutionBlocked, match="unknown prior execution"):
        create_execution("job-direct-bypass", source="untrusted-direct-caller")


def test_execution_creation_reserves_the_admission_transaction(monkeypatch):
    import contextlib
    import cron.executions as executions

    actual = executions._transaction
    calls = []

    @contextlib.contextmanager
    def observed_transaction(*args, **kwargs):
        calls.append(kwargs.get("immediate", False))
        with actual(*args, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(executions, "_transaction", observed_transaction)
    create_execution("job-atomic-admission", source="test")
    assert calls == [True]


def test_completed_execution_does_not_block():
    import cron.executions as executions

    row = create_execution("job-ok", source="test")
    executions.mark_execution_running(row["id"])
    executions.finish_execution(row["id"], success=True)
    assert not execution_is_blocked("job-ok")


def test_external_provider_refuses_unknown_before_creating_an_attempt(monkeypatch):
    from cron.scheduler_provider import InProcessCronScheduler

    monkeypatch.setattr("cron.executions.execution_is_blocked", lambda _job_id: True)
    monkeypatch.setattr(
        "cron.executions.create_execution",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not create")),
    )

    assert InProcessCronScheduler().claim_fire("job-unknown") is None


def test_external_provider_treats_atomic_admission_race_as_blocked(monkeypatch):
    from cron.executions import UnknownExecutionBlocked
    from cron.scheduler_provider import InProcessCronScheduler

    monkeypatch.setattr("cron.executions.execution_is_blocked", lambda _job_id: False)
    monkeypatch.setattr(
        "cron.executions.create_execution",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(UnknownExecutionBlocked("raced")),
    )
    assert InProcessCronScheduler().claim_fire("job-raced") is None


def test_direct_run_treats_atomic_admission_race_as_not_processed(monkeypatch):
    from cron.executions import UnknownExecutionBlocked
    from cron import scheduler

    monkeypatch.setattr(
        scheduler,
        "create_execution",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(UnknownExecutionBlocked("raced")),
    )
    assert scheduler.run_one_job({"id": "job-raced"}) is False


def test_manual_run_refuses_unknown_before_claiming(monkeypatch):
    from tools import cronjob_tools

    monkeypatch.setattr("cron.executions.execution_is_blocked", lambda _job_id: True)
    monkeypatch.setattr(
        cronjob_tools,
        "claim_job_for_fire",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not claim")),
    )

    result = cronjob_tools._execute_job_now({"id": "job-unknown"})
    assert result["claimed"] is False
    assert "unknown" in result["error"].lower()


def test_provider_recovery_reconciles_open_governance_routes(monkeypatch):
    from cron.scheduler_provider import InProcessCronScheduler

    recovered = []
    monkeypatch.setattr("cron.executions.recover_interrupted_executions", lambda: 1)
    monkeypatch.setattr("governance.recover_open_routes", lambda: recovered.append(2) or 2)

    assert InProcessCronScheduler().recover_interrupted() == 1
    assert recovered == [2]


def _make_ledger_blocked(job_id):
    """Persist a provably abandoned attempt and recover it so the real gate refuses ``job_id``."""
    import cron.executions as executions

    row = create_execution(job_id, source="test")
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=?, process_started_at=NULL WHERE id=?",
            ("dead-owner", 999999, row["id"]),
        )
    assert recover_interrupted_executions() == 1
    return row


def _raise(exc):
    def _inner(*_args, **_kwargs):
        raise exc

    return _inner


def test_manual_run_admits_before_claiming_so_a_late_unknown_cannot_consume_the_claim(monkeypatch):
    """Preflight passed, then recovery committed unknown: the atomic gate refuses before any claim."""
    from tools import cronjob_tools

    _make_ledger_blocked("job-late-unknown")
    monkeypatch.setattr("cron.executions.execution_is_blocked", lambda _job_id: False)
    monkeypatch.setattr(
        cronjob_tools, "claim_job_for_fire", _raise(AssertionError("claim must not be consumed"))
    )

    result = cronjob_tools._execute_job_now({"id": "job-late-unknown"})

    assert result["claimed"] is False
    assert "unknown" in result["error"].lower()


def test_background_run_admits_before_claiming_so_a_late_unknown_cannot_consume_the_claim(monkeypatch):
    from tools import cronjob_tools

    _make_ledger_blocked("job-late-unknown-bg")
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)
    monkeypatch.setattr("tools.approval.get_current_session_key", lambda default="": "agent:test")
    monkeypatch.setattr("cron.executions.execution_is_blocked", lambda _job_id: False)
    monkeypatch.setattr(
        cronjob_tools, "claim_job_for_fire", _raise(AssertionError("claim must not be consumed"))
    )
    monkeypatch.setattr(
        "tools.async_delegation.dispatch_async_delegation", _raise(AssertionError("must not dispatch"))
    )

    result = cronjob_tools._try_dispatch_background_run(
        {"id": "job-late-unknown-bg", "name": "bg"}, session_id=None
    )

    assert result["claimed"] is False
    assert "unknown" in result["error"].lower()


def test_manual_run_terminalizes_the_admitted_attempt_when_the_claim_is_lost(monkeypatch):
    from cron.executions import list_executions
    from tools import cronjob_tools

    monkeypatch.setattr(cronjob_tools, "claim_job_for_fire", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(cronjob_tools, "get_job", lambda _job_id: None)

    result = cronjob_tools._execute_job_now({"id": "job-claim-lost"})

    assert result["claimed"] is False
    rows = list_executions(job_id="job-claim-lost")
    assert [row["status"] for row in rows] == ["failed"]
    assert "claim" in (rows[0]["error"] or "").lower()


def test_manual_run_binds_the_admitted_attempt_to_the_claimed_snapshot(monkeypatch, tmp_path):
    """The shared run body adopts the attempt admitted before the claim instead of creating a second one."""
    from cron import scheduler
    from cron.executions import list_executions
    from cron.jobs import create_job
    from tools import cronjob_tools

    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    job = create_job(prompt="governed manual run", schedule="every 1h")
    seen = {}

    def _handoff(dispatched):
        seen["job"] = dict(dispatched)
        return True

    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", _handoff)

    result = cronjob_tools._execute_job_now(dict(job))

    rows = list_executions(job_id=job["id"])
    assert result["claimed"] is True
    assert [row["source"] for row in rows] == ["direct"]
    assert seen["job"]["execution_id"] == rows[0]["id"]
    assert seen["job"]["fire_claim"]["by"]


def test_run_claimed_job_terminalizes_an_admitted_attempt_it_cannot_start():
    from cron import scheduler
    from cron.executions import get_execution
    from tools import cronjob_tools

    admitted = create_execution("job-busy", source="direct")
    assert scheduler.try_register_running_job("job-busy")
    try:
        result = cronjob_tools._run_claimed_job(
            {"id": "job-busy", "fire_claim": {"by": "owner"}}, admitted=admitted
        )
    finally:
        scheduler.release_running_job("job-busy")

    assert result["claimed"] is True
    assert result["success"] is False
    assert get_execution(admitted["id"])["status"] == "failed"


def test_background_inline_fallback_runs_the_claimed_snapshot(monkeypatch):
    """Pool rejection must run the owner-bearing claimed snapshot, not the pre-claim record."""
    from tools import cronjob_tools
    from tools.approval import _approval_session_key

    seen = {}
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)
    monkeypatch.setattr(
        cronjob_tools, "claim_job_for_fire",
        lambda job_id, **_kwargs: {"id": job_id, "fire_claim": {"by": "bg-owner"}},
    )
    monkeypatch.setattr(
        "tools.async_delegation.dispatch_async_delegation",
        lambda **_kwargs: {"status": "rejected", "error": "capacity"},
    )

    def _capture(job, **_kwargs):
        seen["job"] = dict(job)
        return True

    monkeypatch.setattr("cron.scheduler.run_one_job", _capture)
    monkeypatch.setattr(
        cronjob_tools, "get_job", lambda _job_id: {"last_status": "ok", "last_error": None}
    )
    token = _approval_session_key.set("agent:test")
    try:
        result = cronjob_tools._try_dispatch_background_run({"id": "job-inline", "name": "inline"})
    finally:
        _approval_session_key.reset(token)

    assert result["dispatched"] is False
    assert seen["job"]["fire_claim"] == {"by": "bg-owner"}


def test_run_one_job_adopts_a_pre_admitted_attempt(monkeypatch):
    from cron import executions, scheduler

    admitted = create_execution("job-pre-admitted", source="direct")
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda _job: True)
    job = {"id": "job-pre-admitted"}

    with executions.pre_admitted_execution(admitted):
        assert scheduler.run_one_job(job) is True

    assert job["execution_id"] == admitted["id"]
    assert [row["id"] for row in executions.list_executions(job_id="job-pre-admitted")] == [
        admitted["id"]
    ]


def test_pre_admitted_attempt_is_thread_local_and_job_scoped():
    import threading

    from cron import executions

    admitted = create_execution("job-a", source="direct")
    with executions.pre_admitted_execution(admitted):
        assert executions.take_pre_admitted_execution("job-b") is None
        other = {}
        worker = threading.Thread(
            target=lambda: other.update(seen=executions.take_pre_admitted_execution("job-a"))
        )
        worker.start()
        worker.join()
        assert other["seen"] is None
        assert executions.take_pre_admitted_execution("job-a")["id"] == admitted["id"]
        assert executions.take_pre_admitted_execution("job-a") is None


def test_retention_never_prunes_an_unacknowledged_unknown_execution(monkeypatch):
    import cron.executions as executions

    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 1)
    unknown = _make_ledger_blocked("job-fenced")
    for _ in range(3):
        row = create_execution("job-busy-neighbour", source="test")
        executions.mark_execution_running(row["id"])
        executions.finish_execution(row["id"], success=True)

    assert execution_is_blocked("job-fenced")
    assert executions.get_execution(unknown["id"])["status"] == "unknown"


def test_recovery_keeps_a_live_owner_whose_start_fingerprint_is_unavailable():
    """A PID that exists but has no start-time fingerprint is not proof of death."""
    import os

    import cron.executions as executions

    row = create_execution("job-foreign-live", source="test")
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=?, process_started_at=NULL WHERE id=?",
            ("other-live-process", os.getppid(), row["id"]),
        )

    assert recover_interrupted_executions() == 0
    assert executions.get_execution(row["id"])["status"] == "claimed"


def test_blocked_tick_releases_the_oneshot_run_claim(monkeypatch):
    """get_due_jobs stamps a one-shot run_claim before the fence is consulted; a
    refused dispatch must release it like every other early exit of the ticker."""
    from cron import scheduler

    _make_ledger_blocked("job-fenced-oneshot")
    due = {
        "id": "job-fenced-oneshot",
        "name": "fenced",
        "schedule": {"kind": "once", "at": "2026-01-01T00:00:00+00:00"},
        "run_claim": {"at": "2026-01-01T00:00:00+00:00", "by": "tick"},
    }
    cleared = []
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda: [dict(due)])
    monkeypatch.setattr(scheduler, "advance_next_runs", lambda _ids: 0)
    monkeypatch.setattr(scheduler, "clear_run_claim", lambda job_id: cleared.append(job_id) or True)
    monkeypatch.setattr("tools.mcp_tool._kill_orphaned_mcp_children", lambda: None)

    assert scheduler.tick(verbose=False) == 0
    assert cleared == ["job-fenced-oneshot"]


def test_an_older_unacknowledged_unknown_attempt_still_fences_the_job():
    """Concurrent pre-admissions: the winner crashes (unknown) while a later
    loser row is failed. The fence is about unknown side effects, not row order."""
    import cron.executions as executions

    winner = create_execution("job-two-attempts", source="direct")
    loser = create_execution("job-two-attempts", source="direct")
    executions.finish_execution(loser["id"], success=False, error="Fire claim was not acquired")
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=?, process_started_at=NULL WHERE id=?",
            ("dead-owner", 999999, winner["id"]),
        )
    assert recover_interrupted_executions() == 1

    assert execution_is_blocked("job-two-attempts")
    assert acknowledge_unknown_execution("job-two-attempts", winner["id"])
    assert not execution_is_blocked("job-two-attempts")


def test_every_unknown_attempt_needs_acknowledgement_unless_the_job_is_acknowledged():
    import cron.executions as executions

    first = create_execution("job-double-unknown", source="direct")
    second = create_execution("job-double-unknown", source="direct")
    with executions._transaction() as conn:
        for row in (first, second):
            conn.execute(
                "UPDATE executions SET process_id=?, pid=?, process_started_at=NULL WHERE id=?",
                ("dead-owner", 999999, row["id"]),
            )
    assert recover_interrupted_executions() == 2

    assert acknowledge_unknown_execution("job-double-unknown", second["id"])
    assert execution_is_blocked("job-double-unknown"), "the first unknown attempt still fences"
    assert not acknowledge_unknown_execution("job-double-unknown", "not-an-attempt")
    assert acknowledge_unknown_execution("job-double-unknown")  # job-level: every remaining attempt
    assert not execution_is_blocked("job-double-unknown")


def test_legacy_job_keyed_reconciliation_table_is_migrated_with_its_rows():
    import cron.executions as executions

    with executions._transaction() as conn:
        conn.execute("DROP TABLE execution_reconciliations")
        conn.execute(
            "CREATE TABLE execution_reconciliations (job_id TEXT PRIMARY KEY, "
            "execution_id TEXT NOT NULL, acknowledged_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO execution_reconciliations VALUES ('job-legacy','exec-legacy','2026-09-04T00:00:00+00:00')"
        )

    with executions._transaction() as conn:
        rows = conn.execute(
            "SELECT execution_id, job_id FROM execution_reconciliations"
        ).fetchall()
        pk = [row[1] for row in conn.execute("PRAGMA table_info(execution_reconciliations)") if row[5] == 1]
    assert [tuple(row) for row in rows] == [("exec-legacy", "job-legacy")]
    assert pk == ["execution_id"]


def test_background_dispatcher_exception_after_admission_fails_closed(monkeypatch):
    """The dispatcher may raise after it has already submitted the runner, so an
    inline run could double-fire. Fail closed: terminalize the admitted attempt
    (which fences a late start through the running CAS), keep the fire claim,
    and report the failure instead of running twice."""
    from cron.executions import get_execution
    from tools import cronjob_tools
    from tools.approval import _approval_session_key

    marks = []
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)
    monkeypatch.setattr(
        cronjob_tools, "claim_job_for_fire",
        lambda job_id, **_kwargs: {"id": job_id, "fire_claim": {"by": "bg-owner"}},
    )
    monkeypatch.setattr(
        "tools.async_delegation.dispatch_async_delegation",
        _raise(RuntimeError("raised after submit")),
    )
    monkeypatch.setattr(
        "cron.scheduler.run_one_job", _raise(AssertionError("must not run inline"))
    )
    monkeypatch.setattr(
        cronjob_tools, "mark_job_run", lambda *args, **kwargs: marks.append((args, kwargs)) or True
    )
    token = _approval_session_key.set("agent:test")
    try:
        result = cronjob_tools._try_dispatch_background_run({"id": "job-dispatch-raise-2", "name": "x"})
    finally:
        _approval_session_key.reset(token)

    assert result["claimed"] is True
    assert result["dispatched"] is False
    assert result["success"] is False
    assert "raised after submit" in result["error"]
    # The fire claim is deliberately kept: a runner that was already submitted
    # may still be running, and the claim's TTL is what refuses a cross-process
    # retry in the meantime. The terminalized attempt fences a late start.
    assert marks == []
    from cron.executions import list_executions
    assert [row["status"] for row in list_executions(job_id="job-dispatch-raise-2")] == ["failed"]


def test_recovery_keeps_a_live_owner_whose_current_fingerprint_is_unreadable(monkeypatch):
    import os

    import cron.executions as executions

    row = create_execution("job-foreign-unreadable", source="test")
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=?, process_started_at=12345 WHERE id=?",
            ("other-live-process", os.getppid(), row["id"]),
        )
    monkeypatch.setattr(executions, "_process_start_time", lambda _pid: None)

    assert recover_interrupted_executions() == 0
    assert executions.get_execution(row["id"])["status"] == "claimed"


def test_sync_manual_run_recovers_dead_owner_attempts_before_admission(monkeypatch):
    """`hermes cron run` (no async delivery) must not admit a new attempt while a
    dead owner's claimed attempt has not yet been classified unknown."""
    import cron.executions as executions
    from tools import cronjob_tools

    row = create_execution("job-unrecovered", source="direct")
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=?, process_started_at=NULL WHERE id=?",
            ("dead-owner", 999999, row["id"]),
        )
    monkeypatch.setattr(
        cronjob_tools, "claim_job_for_fire", _raise(AssertionError("claim must not be consumed"))
    )

    result = cronjob_tools._execute_job_now({"id": "job-unrecovered"})

    assert result["claimed"] is False
    assert "unknown" in result["error"].lower()
    assert executions.get_execution(row["id"])["status"] == "unknown"


def test_interrupted_legacy_reconciliation_rebuild_is_completed_on_the_next_connection():
    import cron.executions as executions

    unknown = _make_ledger_blocked("job-legacy-ack")
    with executions._transaction() as conn:
        # Simulate a process that died right after RENAME: only the legacy
        # table, holding a real acknowledgement, is left behind.
        conn.execute("DROP TABLE execution_reconciliations")
        conn.execute(
            "CREATE TABLE execution_reconciliations_legacy (job_id TEXT PRIMARY KEY, "
            "execution_id TEXT NOT NULL, acknowledged_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO execution_reconciliations_legacy VALUES ('job-legacy-ack', ?, '2026-09-04T00:00:00+00:00')",
            (unknown["id"],),
        )

    assert not execution_is_blocked("job-legacy-ack")
    with executions._transaction() as conn:
        leftover = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='execution_reconciliations_legacy'"
        ).fetchone()
    assert leftover is None


def test_terminalizing_an_admitted_attempt_retries_a_transient_ledger_failure(monkeypatch):
    import cron.executions as executions
    from tools import cronjob_tools

    import contextlib

    admitted = create_execution("job-transient", source="direct")
    real_transaction = executions._transaction
    calls = {"n": 0}

    @contextlib.contextmanager
    def flaky_transaction(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient ledger failure")
        with real_transaction(*args, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(executions, "_transaction", flaky_transaction)

    assert cronjob_tools._terminalize_admitted_attempt(admitted, "Fire claim was not acquired") is True
    monkeypatch.setattr(executions, "_transaction", real_transaction)
    assert executions.get_execution(admitted["id"])["status"] == "failed"


def test_exact_acknowledgement_is_not_repeatable():
    row = _make_ledger_blocked("job-ack-twice")
    assert acknowledge_unknown_execution("job-ack-twice", row["id"])
    assert not acknowledge_unknown_execution("job-ack-twice", row["id"])


def test_external_claim_fire_recovers_dead_owner_attempts_before_admission(monkeypatch):
    """A webhook fire arrives long after startup recovery; an attempt whose owner
    died since then must be classified unknown before a new attempt is admitted."""
    import cron.executions as executions
    from cron.scheduler_provider import InProcessCronScheduler

    row = create_execution("job-external-unrecovered", source="external")
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=?, process_started_at=NULL WHERE id=?",
            ("dead-owner", 999999, row["id"]),
        )
    monkeypatch.setattr(
        "cron.jobs.claim_job_for_fire", _raise(AssertionError("claim must not be consumed"))
    )

    assert InProcessCronScheduler().claim_fire("job-external-unrecovered") is None
    assert executions.get_execution(row["id"])["status"] == "unknown"
    assert [r["id"] for r in executions.list_executions(job_id="job-external-unrecovered")] == [row["id"]]


def test_terminalizing_an_admitted_attempt_never_touches_one_that_started():
    """Once the run body owns the attempt (claimed->running), the admission-side
    cleanup must not fail it: a crash afterwards has to surface as unknown."""
    import cron.executions as executions
    from tools import cronjob_tools

    admitted = create_execution("job-started", source="direct")
    executions.mark_execution_running(admitted["id"])

    assert cronjob_tools._terminalize_admitted_attempt(admitted, "dispatcher raised") is False
    assert executions.get_execution(admitted["id"])["status"] == "running"


def test_external_claim_loss_retries_terminalizing_the_admitted_attempt(monkeypatch):
    import cron.executions as executions
    from cron.scheduler_provider import InProcessCronScheduler

    monkeypatch.setattr("cron.jobs.claim_job_for_fire", lambda *_args, **_kwargs: False)
    real_transaction = executions._transaction
    state = {"n": 0}

    import contextlib

    @contextlib.contextmanager
    def flaky_transaction(*args, **kwargs):
        if (
            state.get("admitted")
            and not state.get("failed_once")
            and not kwargs.get("immediate")
        ):
            # The first non-admission ledger write after admission is the
            # terminalization of the attempt that lost its claim: fail it
            # exactly once. (The fence and admission take the write lock
            # up front; they are not the target here.)
            state["failed_once"] = True
            raise RuntimeError("transient ledger failure")
        if kwargs.get("immediate"):
            state["admitted"] = True
        with real_transaction(*args, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(executions, "_transaction", flaky_transaction)

    assert InProcessCronScheduler().claim_fire("job-external-claim-lost") is None
    rows = executions.list_executions(job_id="job-external-claim-lost")
    assert [row["status"] for row in rows] == ["failed"]


def test_governance_recovery_failure_does_not_stop_cron_recovery(monkeypatch):
    import sqlite3

    from cron.scheduler_provider import InProcessCronScheduler

    monkeypatch.setattr("cron.executions.recover_interrupted_executions", lambda: 4)
    monkeypatch.setattr(
        "governance.recover_open_routes", _raise(sqlite3.DatabaseError("routes.db is corrupt"))
    )

    assert InProcessCronScheduler().recover_interrupted() == 4


def _dead_owner_row(job_id, *, running=False):
    """An open attempt whose owner is provably gone, with NO recovery run."""
    import cron.executions as executions

    row = create_execution(job_id, source="test")
    if running:
        executions.mark_execution_running(row["id"])
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=?, process_started_at=NULL WHERE id=?",
            ("dead-owner", 999999, row["id"]),
        )
    return row


def test_the_fence_classifies_a_dead_owner_attempt_without_waiting_for_recovery():
    """Whatever path consults the fence, an attempt whose owner is provably gone
    must count as unknown right there, not only after some scheduler's throttled
    recovery happens to run."""
    import cron.executions as executions

    row = _dead_owner_row("job-dead-unrecovered", running=True)

    assert execution_is_blocked("job-dead-unrecovered")
    assert executions.get_execution(row["id"])["status"] == "unknown"
    assert execution_counters()["unknown"] >= 1


def test_admission_classifies_a_dead_owner_attempt_under_the_write_lock():
    import pytest

    _dead_owner_row("job-dead-admission")
    with pytest.raises(UnknownExecutionBlocked):
        create_execution("job-dead-admission", source="direct")


def test_the_fence_leaves_a_live_owner_attempt_alone():
    import cron.executions as executions

    live = create_execution("job-live-open", source="test")  # owned by this process
    assert not execution_is_blocked("job-live-open")
    assert executions.get_execution(live["id"])["status"] == "claimed"


def test_the_fence_honors_the_handoff_adoption_grace():
    import time

    import cron.executions as executions

    row = create_execution("job-handoff-grace", source="test")
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=?, process_started_at=NULL, "
            "handoff_pending=1, handoff_started_at=? WHERE id=?",
            ("dead-owner", 999999, time.time(), row["id"]),
        )

    assert not execution_is_blocked("job-handoff-grace")
    assert executions.get_execution(row["id"])["status"] == "claimed"


def test_ticker_terminalizes_its_admitted_attempt_when_the_claim_raises(monkeypatch):
    """The ticker admits before its worker claims; a jobs-file failure in that
    claim must not leave the admitted attempt claimed by the live process."""
    import cron.executions as executions
    from cron import scheduler

    due = {"id": "job-tick-claim-raises", "name": "t", "schedule": {"kind": "interval", "every": 600}}
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda: [dict(due)])
    monkeypatch.setattr(scheduler, "advance_next_runs", lambda _ids: 0)
    monkeypatch.setattr(scheduler, "claim_job_for_fire", _raise(OSError("jobs.json unwritable")))
    monkeypatch.setattr("tools.mcp_tool._kill_orphaned_mcp_children", lambda: None)

    scheduler.tick(verbose=False, sync=True)

    rows = executions.list_executions(job_id="job-tick-claim-raises")
    assert [row["status"] for row in rows] == ["failed"]
    assert "jobs.json" in (rows[0]["error"] or "")


def test_manual_refusal_describes_an_unknown_prior_attempt_not_the_latest(monkeypatch):
    import cron.executions as executions
    from tools import cronjob_tools

    winner = create_execution("job-msg", source="direct")
    loser = create_execution("job-msg", source="direct")
    executions.finish_execution(loser["id"], success=False, error="Fire claim was not acquired")
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=?, process_started_at=NULL WHERE id=?",
            ("dead-owner", 999999, winner["id"]),
        )
    monkeypatch.setattr(cronjob_tools, "claim_job_for_fire", _raise(AssertionError("must not claim")))

    result = cronjob_tools._execute_job_now({"id": "job-msg"})

    assert result["claimed"] is False
    assert "unknown" in result["error"].lower()
    assert "latest" not in result["error"].lower()


def test_misfire_catchup_terminalizes_its_admitted_attempt_when_the_worker_cannot_start(monkeypatch):
    """claim_fire admits the attempt before the misfire worker thread starts; an
    OS thread-exhaustion failure must not strand that attempt for a later fence."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    import cron.executions as executions
    from cron import scheduler_provider

    admitted = create_execution("job-misfire", source="external")
    overdue = {
        "id": "job-misfire", "name": "m", "enabled": True, "state": "scheduled",
        "schedule": {"kind": "interval", "every": 600},
        "next_run_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    }
    monkeypatch.setattr("cron.jobs.load_jobs", lambda: [dict(overdue)])
    monkeypatch.setattr(scheduler_provider, "_misfire_grace_minutes", lambda: 1.0)  # 0 disables catch-up

    import threading

    real_thread = threading.Thread

    class BoomThread(real_thread):
        def start(self):
            if str(self.name).startswith("cron-misfire-"):
                raise RuntimeError("can't start new thread")
            return super().start()

    monkeypatch.setattr(threading, "Thread", BoomThread)
    provider = SimpleNamespace(
        claim_fire=lambda job_id, **_kw: {"id": job_id, "execution_id": admitted["id"], "fire_claim": {"by": "o"}},
        fire_claimed=lambda *_a, **_k: True,
    )

    assert scheduler_provider.fire_overdue_jobs(provider) == 0
    assert executions.get_execution(admitted["id"])["status"] == "failed"


def test_provider_recovery_reports_only_cron_attempts_but_still_recovers_routes(monkeypatch):
    """Callers log the return value as interrupted cron executions; abandoned
    delegated routes are recovered on the same boundary but counted apart."""
    from cron.scheduler_provider import InProcessCronScheduler

    calls = []
    monkeypatch.setattr("cron.executions.recover_interrupted_executions", lambda: 1)
    monkeypatch.setattr("governance.recover_open_routes", lambda: calls.append("routes") or 2)

    assert InProcessCronScheduler().recover_interrupted() == 1
    assert calls == ["routes"]


def test_acknowledgements_do_not_outlive_their_pruned_attempts(monkeypatch):
    import cron.executions as executions

    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 1)
    acked = _make_ledger_blocked("job-acked-then-pruned")
    assert acknowledge_unknown_execution("job-acked-then-pruned", acked["id"])
    for _ in range(3):
        row = create_execution("job-neighbour-2", source="test")
        executions.mark_execution_running(row["id"])
        executions.finish_execution(row["id"], success=True)

    assert executions.get_execution(acked["id"]) is None, "acknowledged unknown rows are prunable"
    with executions._transaction() as conn:
        orphans = conn.execute(
            "SELECT COUNT(*) FROM execution_reconciliations WHERE execution_id NOT IN (SELECT id FROM executions)"
        ).fetchone()[0]
    assert orphans == 0


def test_run_claimed_job_releases_its_fire_claim_when_the_job_is_already_running(monkeypatch, tmp_path):
    """A manual run that loses the in-flight dedupe never ran, so its fire claim
    must not sit on the job until the TTL: the ticker that registered first
    claims AFTER registering, and a stranded claim makes it lose too, leaving
    nobody to run the job for 300s."""
    from cron import scheduler
    from cron.executions import get_execution
    from cron.jobs import claim_job_for_fire, create_job, get_job
    from tools import cronjob_tools

    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    job = create_job(prompt="governed manual run", schedule="every 1h")
    job_id = job["id"]
    admitted = create_execution(job_id, source="direct")
    claimed = claim_job_for_fire(job_id, return_job=True)
    assert isinstance(claimed, dict) and claimed["fire_claim"]["by"]
    before = get_job(job_id)

    assert scheduler.try_register_running_job(job_id), "a ticker registered first"
    try:
        result = cronjob_tools._run_claimed_job(claimed, admitted=admitted)
    finally:
        scheduler.release_running_job(job_id)

    assert result["claimed"] is True
    assert result["success"] is False
    assert "already running" in result["error"]
    assert get_execution(admitted["id"])["status"] == "failed"
    after = get_job(job_id)
    assert after.get("fire_claim") is None
    # Nothing ran: no run result was recorded on the way out.
    assert after.get("last_run_at") is None
    assert after.get("last_status") == before.get("last_status")
    assert after["next_run_at"] == before["next_run_at"]
    assert claim_job_for_fire(job_id) is True


def test_misfire_catchup_releases_its_fire_claim_when_the_worker_cannot_start(monkeypatch, tmp_path):
    """Nothing ran, so the claim the sweep took goes back together with the
    attempt: the next housekeeping pass (or an external retry) can fire the
    job instead of waiting out the claim TTL."""
    import threading
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    import cron.executions as executions
    from cron import scheduler_provider
    from cron.jobs import claim_job_for_fire, create_job, get_job, load_jobs, save_jobs

    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    job = create_job(prompt="misfired", schedule="every 1h")
    job_id = job["id"]
    records = load_jobs()
    records[0]["next_run_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    save_jobs(records)
    monkeypatch.setattr(scheduler_provider, "_misfire_grace_minutes", lambda: 1.0)

    admitted = {}

    def claim_fire(jid, **_kw):
        admitted["row"] = create_execution(jid, source="external")
        claimed = claim_job_for_fire(jid, return_job=True)
        assert isinstance(claimed, dict)
        claimed["execution_id"] = admitted["row"]["id"]
        return claimed

    real_thread = threading.Thread

    class BoomThread(real_thread):
        def start(self):
            if str(self.name).startswith("cron-misfire-"):
                raise RuntimeError("can't start new thread")
            return super().start()

    monkeypatch.setattr(threading, "Thread", BoomThread)
    provider = SimpleNamespace(claim_fire=claim_fire, fire_claimed=lambda *_a, **_k: True)

    assert scheduler_provider.fire_overdue_jobs(provider) == 0
    assert executions.get_execution(admitted["row"]["id"])["status"] == "failed"
    assert get_job(job_id).get("fire_claim") is None
    assert claim_job_for_fire(job_id) is True
