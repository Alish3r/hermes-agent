"""Profile-local durable audit ledger for cron execution attempts.

The ledger records what is known about each attempt; it is not a retry queue.
Interrupted attempts become ``unknown`` only after their exact owner process is
proved gone. Terminal states are immutable.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

# Optional test override. Production resolves the path at transaction time so
# dashboard operations that temporarily enter another profile cannot leak that
# profile's execution records into the import-time home.
EXECUTIONS_FILE: Optional[Path] = None
MAX_TERMINAL_EXECUTIONS = 1000
HANDOFF_ADOPTION_GRACE_SECONDS = 30.0
_TERMINAL_STATES = ("completed", "failed", "unknown")
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex
# Thread-scoped handoff of an attempt admitted before a store claim.  Manual
# runs admit through the atomic gate first and then pass the run body the
# claimed snapshot exactly as the store returned it; the admitted attempt
# travels on the calling thread and is consumed once by the run body.
_thread_state = threading.local()


class UnknownExecutionBlocked(RuntimeError):
    """A prior unknown outcome requires explicit reconciliation first."""


def _connect() -> sqlite3.Connection:
    from cron.jobs import _ensure_cron_dir

    path = EXECUTIONS_FILE or (get_hermes_home().resolve() / "cron" / "executions.db")
    _ensure_cron_dir(path.parent)
    return sqlite3.connect(path, timeout=5)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    apply_wal_with_fallback(conn, db_label="cron/executions.db")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS executions (
             id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             source TEXT NOT NULL,
             process_id TEXT NOT NULL,
             pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown')),
             handoff_pending INTEGER NOT NULL DEFAULT 0,
             handoff_started_at REAL,
             claimed_at TEXT NOT NULL,
             started_at TEXT,
             finished_at TEXT,
             error TEXT
           )"""
    )
    from hermes_cli.sqlite_util import add_column_if_missing

    add_column_if_missing(
        conn, "executions", "handoff_pending",
        "handoff_pending INTEGER NOT NULL DEFAULT 0",
    )
    add_column_if_missing(
        conn, "executions", "handoff_started_at", "handoff_started_at REAL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
        "ON executions(job_id, claimed_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_status_claimed "
        "ON executions(status, claimed_at DESC, id DESC)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS execution_reconciliations (
             execution_id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             acknowledged_at TEXT NOT NULL
           )"""
    )
    _migrate_reconciliations(conn)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS execution_counters (
             name TEXT PRIMARY KEY,
             value INTEGER NOT NULL DEFAULT 0
           )"""
    )


def _reconciliation_pk(conn: sqlite3.Connection) -> List[str]:
    return [
        row[1]
        for row in conn.execute("PRAGMA table_info(execution_reconciliations)")
        if row[5] == 1
    ]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _migrate_reconciliations(conn: sqlite3.Connection) -> None:
    """Rebuild a legacy job-keyed acknowledgement table keyed by attempt.

    Earlier ledgers keyed acknowledgements by job, which could not express
    several unknown attempts for one job. The rebuild is idempotent and runs
    in its own write transaction: an interrupted rebuild leaves the legacy
    table behind and the next connection finishes the import, and concurrent
    first connections serialize on the write lock and re-check inside it.
    """
    legacy = "execution_reconciliations_legacy"
    if _reconciliation_pk(conn) != ["job_id"] and not _table_exists(conn, legacy):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _reconciliation_pk(conn) == ["job_id"]:
            conn.execute(f"ALTER TABLE execution_reconciliations RENAME TO {legacy}")
            conn.execute(
                """CREATE TABLE execution_reconciliations (
                     execution_id TEXT PRIMARY KEY,
                     job_id TEXT NOT NULL,
                     acknowledged_at TEXT NOT NULL
                   )"""
            )
        if _table_exists(conn, legacy):
            conn.execute(
                "INSERT OR IGNORE INTO execution_reconciliations(execution_id, job_id, acknowledged_at) "
                f"SELECT execution_id, job_id, acknowledged_at FROM {legacy}"
            )
            conn.execute(f"DROP TABLE {legacy}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def _transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, always close.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back
    the transaction; it does not close the connection. Relying on that alone
    leaks a connection (and its WAL/SHM file descriptors) on every call,
    since closing then depends on the garbage collector. Schema init runs
    inside the ``try`` too, so a PRAGMA/DDL failure after a successful
    ``connect()`` still closes the connection instead of leaking it.
    """
    with _lock:
        conn = _connect()
        try:
            _initialize_schema(conn)
            if immediate:
                # Admission is check-plus-insert, not two independently
                # committed operations.  Reserve the writer slot before the
                # blocked-state read so recovery cannot interleave a new
                # unknown row between that read and the attempted claim.
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            else:
                with conn:
                    yield conn
        finally:
            conn.close()


def _record(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def _emit_execution_state(
    record: Optional[Dict[str, Any]], *, delivery_outcome: Optional[str] = None
) -> None:
    """Project durable state to monitoring without affecting ledger behavior."""
    try:
        from agent.monitoring.cron_health import emit_execution_state

        emit_execution_state(record, delivery_outcome=delivery_outcome)
    except Exception:
        pass


def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_is_live(pid: int, started_at: Optional[int]) -> bool:
    try:
        from gateway.status import _pid_exists
        if not _pid_exists(pid):
            return False
    except Exception:
        return True  # fail safe: inability to prove death must not rewrite state
    if started_at is None:
        # No start-time fingerprint was captured for this owner.  An existing
        # PID is then not proof of death (it may be the original owner, or one
        # this process cannot inspect), and recovery must not rewrite an
        # attempt it cannot prove abandoned.
        return True
    current = _process_start_time(pid)
    if current is None:
        # The live fingerprint cannot be read (inspection denied, zombie).
        # Same rule: not proof of death.
        return True
    return current == started_at


def _prune_unlocked(conn: sqlite3.Connection) -> None:
    # An unacknowledged unknown attempt is a safety fence, not history:
    # retention must never lift it silently, so it is not a prune candidate.
    limit = max(0, int(MAX_TERMINAL_EXECUTIONS))
    conn.execute(
        """DELETE FROM executions WHERE id IN (
             SELECT id FROM executions
             WHERE status IN ('completed','failed','unknown')
               AND NOT (status='unknown' AND id NOT IN
                        (SELECT execution_id FROM execution_reconciliations))
             ORDER BY finished_at DESC, claimed_at DESC, id DESC LIMIT -1 OFFSET ?
           )""",
        (limit,),
    )
    # An acknowledgement only means something for an attempt that still
    # exists; do not let them accumulate past their pruned attempts.
    conn.execute(
        "DELETE FROM execution_reconciliations "
        "WHERE execution_id NOT IN (SELECT id FROM executions)"
    )


def create_execution(job_id: str, *, source: str) -> Dict[str, Any]:
    """Persist a claimed attempt before executor/provider dispatch."""
    # This is the final shared admission boundary.  Entry-point preflights
    # avoid claiming/advancing schedules first, while this guard prevents a
    # newly-added executor from silently bypassing the unknown-outcome fence.
    # The fence classifies this job's abandoned attempts itself, inside this
    # write transaction, so no recovery schedule and no interleaving writer
    # can let a crashed attempt be followed by a duplicate run.
    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    recovered: List[Dict[str, Any]] = []
    row = None
    with _transaction(immediate=True) as conn:
        blocked = _execution_is_blocked_unlocked(conn, str(job_id), recovered)
        if not blocked:
            conn.execute(
                """INSERT INTO executions
                   (id, job_id, source, process_id, pid, process_started_at,
                    status, claimed_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?)""",
                (execution_id, str(job_id), str(source), _PROCESS_ID, pid,
                 _process_start_time(pid), now),
            )
            row = conn.execute(
                "SELECT * FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
    for abandoned in recovered:
        _emit_execution_state(abandoned)
    if blocked:
        raise UnknownExecutionBlocked(
            f"Cron job {job_id!r} has an unknown prior execution; reconcile it before retrying."
        )
    record = _record(row)
    _emit_execution_state(record)
    return record  # type: ignore[return-value]


def mark_execution_handoff_pending(execution_id: str) -> Optional[Dict[str, Any]]:
    """Fence restart recovery while an external worker is adopting a claim."""
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET handoff_pending=1, handoff_started_at=?
               WHERE id=? AND status='claimed'
                 AND process_id=? AND pid=?""",
            (time.time(), execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        record = _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())
    _emit_execution_state(record)
    return record


def adopt_claimed_execution(execution_id: str) -> Optional[Dict[str, Any]]:
    """Atomically transfer and start an attempt in its worker process.

    The dispatching gateway creates the row before spawning a restart-safe
    worker.  Adoption is the single ``claimed`` → ``running`` gate: only the
    winner may acknowledge ownership or run side effects.
    """
    pid = os.getpid()
    process_started_at = _process_start_time(pid)
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET process_id=?, pid=?, process_started_at=?,
                   status='running', started_at=?, handoff_pending=0,
                   handoff_started_at=NULL
               WHERE id=? AND status='claimed' AND handoff_pending=1""",
            (_PROCESS_ID, pid, process_started_at, now, execution_id),
        )
        if cur.rowcount != 1:
            return None
        record = _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())
    _emit_execution_state(record)
    return record


def mark_execution_running(execution_id: str) -> Optional[Dict[str, Any]]:
    """Transition one claimed attempt to running exactly once."""
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status='running', started_at=?, handoff_pending=0,
                   handoff_started_at=NULL
               WHERE id=? AND status='claimed' AND handoff_pending=0
                 AND process_id=? AND pid=?""",
            (now, execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        record = _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())
    _emit_execution_state(record)
    return record


def finish_execution(
    execution_id: str, *, success: bool, error: Optional[str] = None,
    delivery_outcome: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten."""
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status=?, finished_at=?, error=?, handoff_pending=0,
                   handoff_started_at=NULL
               WHERE id=? AND status IN ('claimed','running')
                 AND process_id=? AND pid=?""",
            (status, now, detail, execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        record = _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())
    _emit_execution_state(record, delivery_outcome=delivery_outcome)
    return record


def terminalize_unstarted_execution(execution_id: str, *, error: str) -> bool:
    """Fail an admitted attempt that never started; never touch one that did.

    Admission-side cleanup (claim lost, dispatcher failure, in-flight dedupe)
    must not rewrite an attempt the run body already owns: a started attempt
    that later dies has to surface as ``unknown``, so only ``claimed`` rows of
    this exact process are eligible. A transient ledger failure is retried
    once and then logged loudly, because a ``claimed`` row owned by a live
    process is skipped by same-process recovery until the process exits.
    Returns True when the attempt is terminal afterwards.
    """
    now = _hermes_now().isoformat()
    last_error: Optional[BaseException] = None
    for _attempt in (1, 2):
        try:
            record = None
            terminal = False
            with _transaction() as conn:
                cur = conn.execute(
                    """UPDATE executions
                       SET status='failed', finished_at=?, error=?,
                           handoff_pending=0, handoff_started_at=NULL
                       WHERE id=? AND status='claimed'
                         AND process_id=? AND pid=?""",
                    (now, str(error), str(execution_id), _PROCESS_ID, os.getpid()),
                )
                if cur.rowcount == 1:
                    _prune_unlocked(conn)
                    record = _record(conn.execute(
                        "SELECT * FROM executions WHERE id=?", (str(execution_id),)
                    ).fetchone())
                else:
                    row = conn.execute(
                        "SELECT status FROM executions WHERE id=?", (str(execution_id),)
                    ).fetchone()
                    terminal = row is not None and row["status"] in _TERMINAL_STATES
            if record is not None:
                _emit_execution_state(record)
                return True
            return terminal
        except Exception as exc:
            last_error = exc
    logger.warning(
        "Could not terminalize admitted cron attempt %s (%s); it stays claimed "
        "until this process exits and recovery classifies it unknown",
        execution_id, last_error,
    )
    return False


_OPEN_ATTEMPT_COLUMNS = (
    "id, status, process_id, pid, process_started_at, handoff_pending, handoff_started_at"
)


def _classify_abandoned_unlocked(
    conn: sqlite3.Connection, rows: List[sqlite3.Row], now: str
) -> List[Dict[str, Any]]:
    """Mark the open attempts among ``rows`` unknown when their owner is gone.

    Shared by periodic recovery and the admission fence, so an abandoned
    attempt counts as unknown wherever it is first observed. Attempts owned
    by this process, by a live process, or inside the handoff adoption grace
    are left untouched. Returns the recovered records; the caller emits them
    after its transaction commits.
    """
    recovered: List[Dict[str, Any]] = []
    for row in rows:
        if row["process_id"] == _PROCESS_ID:
            continue
        if _owner_is_live(int(row["pid"]), row["process_started_at"]):
            continue
        handoff_started_at = row["handoff_started_at"]
        if (
            row["handoff_pending"]
            and handoff_started_at is not None
            and time.time() - float(handoff_started_at)
            < HANDOFF_ADOPTION_GRACE_SECONDS
        ):
            continue
        cur = conn.execute(
            """UPDATE executions
               SET status='unknown', finished_at=?, error=?,
                   handoff_pending=0, handoff_started_at=NULL
               WHERE id=? AND status=? AND process_id=? AND pid=?
                 AND handoff_pending=?
                 AND handoff_started_at IS ?""",
            (now,
             "Scheduler restarted after this execution's owner exited before a durable "
             "terminal state; whether side effects ran is unknown.",
             row["id"], row["status"], row["process_id"], row["pid"],
             row["handoff_pending"], row["handoff_started_at"]),
        )
        if cur.rowcount:
            record = _record(conn.execute(
                "SELECT * FROM executions WHERE id=?", (row["id"],)
            ).fetchone())
            if record is not None:
                recovered.append(record)
    if recovered:
        conn.execute(
            "INSERT INTO execution_counters(name,value) VALUES('unknown',?) "
            "ON CONFLICT(name) DO UPDATE SET value=value+excluded.value",
            (len(recovered),),
        )
        _prune_unlocked(conn)
    return recovered


def recover_interrupted_executions() -> int:
    """Mark provably abandoned attempts unknown without scheduling retries."""
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        rows = conn.execute(
            f"SELECT {_OPEN_ATTEMPT_COLUMNS} FROM executions "
            "WHERE status IN ('claimed','running')"
        ).fetchall()
        recovered = _classify_abandoned_unlocked(conn, rows, now)
    for record in recovered:
        _emit_execution_state(record)
    return len(recovered)


def list_executions(
    *, job_id: Optional[str] = None, limit: int = 50,
    before_claimed_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return indexed, newest-first execution history with cursor pagination."""
    clauses: List[str] = []
    params: List[Any] = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(str(job_id))
    if before_claimed_at is not None:
        clauses.append("claimed_at < ?")
        params.append(str(before_claimed_at))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM executions" + where
            + " ORDER BY claimed_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_execution(execution_id: str) -> Optional[Dict[str, Any]]:
    """Return one exact execution attempt, or ``None`` when it is absent."""
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM executions WHERE id=?",
            (str(execution_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def latest_execution(job_id: str) -> Optional[Dict[str, Any]]:
    rows = list_executions(job_id=job_id, limit=1)
    return rows[0] if rows else None


def latest_executions(job_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Load latest execution for many jobs in one indexed query."""
    clean = [str(job_id) for job_id in dict.fromkeys(job_ids) if job_id]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    with _transaction() as conn:
        rows = conn.execute(
            f"""SELECT e.* FROM executions e
                WHERE e.job_id IN ({placeholders})
                  AND e.id=(SELECT e2.id FROM executions e2
                            WHERE e2.job_id=e.job_id
                            ORDER BY e2.claimed_at DESC, e2.id DESC LIMIT 1)""",
            clean,
        ).fetchall()
    return {row["job_id"]: dict(row) for row in rows}


def _execution_is_blocked_unlocked(
    conn: sqlite3.Connection,
    job_id: str,
    recovered: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """Evaluate the unknown fence inside the caller's existing transaction.

    Open attempts of the job whose owner is provably gone are classified
    unknown first (``recovered`` collects them for emission after commit), so
    the fence never depends on a recovery schedule elsewhere. Every
    unacknowledged unknown attempt then fences the job, whatever its position
    in the history: a later attempt that merely lost its fire claim says
    nothing about the side effects of the one that crashed.
    """
    open_rows = conn.execute(
        f"SELECT {_OPEN_ATTEMPT_COLUMNS} FROM executions "
        "WHERE job_id=? AND status IN ('claimed','running')",
        (str(job_id),),
    ).fetchall()
    if open_rows:
        newly = _classify_abandoned_unlocked(conn, open_rows, _hermes_now().isoformat())
        if recovered is not None:
            recovered.extend(newly)
    row = conn.execute(
        """SELECT 1 FROM executions
           WHERE job_id=? AND status='unknown'
             AND id NOT IN (SELECT execution_id FROM execution_reconciliations)
           LIMIT 1""",
        (str(job_id),),
    ).fetchone()
    return row is not None


def execution_is_blocked(job_id: str) -> bool:
    """Return true while any unacknowledged unknown attempt fences the job."""
    recovered: List[Dict[str, Any]] = []
    # The fence may classify (write) abandoned attempts; take the write lock
    # up front so a concurrent classifier waits on busy_timeout instead of
    # failing a deferred-to-write upgrade.
    with _transaction(immediate=True) as conn:
        blocked = _execution_is_blocked_unlocked(conn, str(job_id), recovered)
    for record in recovered:
        _emit_execution_state(record)
    return blocked


def acknowledge_unknown_execution(job_id: str, execution_id: Optional[str] = None) -> bool:
    """Code-level reconciliation of unknown attempts.

    With ``execution_id`` only that attempt is acknowledged, and it must be an
    unknown attempt of this job (fails closed otherwise). Without it, every
    unacknowledged unknown attempt of the job is acknowledged: the operator has
    checked the job's side effects as a whole.
    """
    with _transaction() as conn:
        if execution_id is not None:
            rows = conn.execute(
                """SELECT id FROM executions
                   WHERE id=? AND job_id=? AND status='unknown'
                     AND id NOT IN (SELECT execution_id FROM execution_reconciliations)""",
                (str(execution_id), str(job_id)),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id FROM executions
                   WHERE job_id=? AND status='unknown'
                     AND id NOT IN (SELECT execution_id FROM execution_reconciliations)""",
                (str(job_id),),
            ).fetchall()
        if not rows:
            return False
        now = _hermes_now().isoformat()
        for row in rows:
            conn.execute(
                "INSERT INTO execution_reconciliations(execution_id, job_id, acknowledged_at) "
                "VALUES(?,?,?) ON CONFLICT(execution_id) DO UPDATE SET acknowledged_at=excluded.acknowledged_at",
                (row["id"], str(job_id), now),
            )
        return True


def execution_counters() -> Dict[str, int]:
    with _transaction() as conn:
        rows = conn.execute("SELECT name,value FROM execution_counters").fetchall()
    return {row["name"]: int(row["value"]) for row in rows}


@contextmanager
def pre_admitted_execution(record: Optional[Dict[str, Any]]) -> Iterator[None]:
    """Offer an already-admitted attempt to the run body on this thread.

    ``run_one_job`` adopts it through ``take_pre_admitted_execution`` instead
    of admitting a second attempt.  The binding is thread-local and restored
    on exit, so pool threads and nested manual runs never inherit a stale
    attempt.  ``None`` is a no-op.
    """
    if record is None:
        yield
        return
    previous = getattr(_thread_state, "pre_admitted", None)
    _thread_state.pre_admitted = dict(record)
    try:
        yield
    finally:
        _thread_state.pre_admitted = previous


def take_pre_admitted_execution(job_id: str) -> Optional[Dict[str, Any]]:
    """Consume the attempt this thread pre-admitted for ``job_id``, if any."""
    record = getattr(_thread_state, "pre_admitted", None)
    if record is None or record.get("job_id") != str(job_id):
        return None
    _thread_state.pre_admitted = None
    return record
