"""Durable, fenced orchestration allocation ledger.

There is exactly one lifecycle authority: ``orchestration_allocations`` plus its
append-only ``orchestration_events``.  ``async_delegations`` is intentionally a
separate transport journal: it records attempted execution, result payload,
delivery, and adjudication, but it never authorizes parent finalization or
resource cleanup.  Dispatch failures are compensated into a diagnostic terminal
allocation, and startup recovery converges terminal transport evidence into the
canonical ledger after a crash between SQLite transactions.

Every state mutation is generation-fenced and idempotent, and terminal/resource
receipts are content-addressed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

from hermes_constants import get_hermes_home


LIVE_STATES = frozenset({"allocated", "running", "waiting_children", "reaping"})
TERMINAL_STATES = frozenset(
    {"terminal_success", "terminal_failure", "interrupted", "unknown", "retained_diagnostic"}
)
_ALLOWED_TRANSITIONS = {
    "allocated": {"running", "waiting_children", "terminal_failure", "interrupted", "unknown"},
    "running": {"waiting_children", "terminal_success", "terminal_failure", "interrupted", "unknown"},
    "waiting_children": {"running", "terminal_success", "terminal_failure", "interrupted", "unknown"},
    "terminal_success": {"reaping", "reaped", "retained_diagnostic"},
    "reaping": {"reaped", "retained_diagnostic"},
    "terminal_failure": {"retained_diagnostic"},
    "interrupted": {"retained_diagnostic"},
    "unknown": {"retained_diagnostic"},
    "retained_diagnostic": set(),
    "reaped": set(),
}


class LedgerError(RuntimeError):
    """Base class for fail-closed ledger errors."""


class AllocationNotFound(LedgerError):
    pass


class InvalidTransition(LedgerError):
    pass


class GenerationMismatch(LedgerError):
    pass


class FinalizationBlocked(LedgerError):
    def __init__(self, allocation_id: str, gate: Mapping[str, Any]):
        self.allocation_id = allocation_id
        self.gate = dict(gate)
        super().__init__(
            f"allocation {allocation_id} cannot finalize: "
            f"active_descendants={gate.get('active_descendants', [])}, "
            "unreconciled_successful_descendants="
            f"{gate.get('unreconciled_successful_descendants', [])}"
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time

        value = get_process_start_time(pid)
        return int(value) if value is not None else None
    except Exception:
        try:
            import psutil

            return int(psutil.Process(pid).create_time() * 1_000_000)
        except Exception:
            return None


def _pid_exists(pid: int) -> bool:
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False


class OrchestrationLedger:
    """SQLite-backed allocation state machine with durable event receipts."""

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        *,
        owner_pid: Optional[int] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else get_hermes_home() / "state.db"
        self.owner_pid = int(owner_pid if owner_pid is not None else os.getpid())
        self.owner_started_at = _process_start_time(self.owner_pid)
        self.clock = clock
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            from hermes_state import apply_wal_with_fallback

            apply_wal_with_fallback(conn, db_label="state.db (orchestration_ledger)")
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._transaction() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS orchestration_allocations (
                    allocation_id TEXT PRIMARY KEY,
                    root_allocation_id TEXT NOT NULL,
                    parent_allocation_id TEXT,
                    owner_session_id TEXT NOT NULL,
                    launching_session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    depth INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    task_state TEXT NOT NULL DEFAULT 'pending',
                    verdict TEXT,
                    adjudication_state TEXT NOT NULL DEFAULT 'pending',
                    adjudicated_at REAL,
                    adjudication_error TEXT,
                    terminal_reason TEXT,
                    resource_state TEXT NOT NULL DEFAULT 'owned',
                    generation INTEGER NOT NULL,
                    owner_pid INTEGER NOT NULL,
                    owner_started_at INTEGER,
                    resource_claims_json TEXT NOT NULL DEFAULT '{}',
                    terminal_receipt_json TEXT,
                    receipt_digest TEXT,
                    resource_receipt_json TEXT,
                    resource_receipt_digest TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(parent_allocation_id)
                        REFERENCES orchestration_allocations(allocation_id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS orchestration_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    allocation_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL UNIQUE,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    event_digest TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(allocation_id)
                        REFERENCES orchestration_allocations(allocation_id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS orchestration_spawn_reservations (
                    operation_id TEXT PRIMARY KEY,
                    root_allocation_id TEXT NOT NULL,
                    owner_session_id TEXT NOT NULL,
                    allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
                    used_after INTEGER NOT NULL,
                    configured_limit INTEGER NOT NULL,
                    created_at REAL NOT NULL
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_orch_spawn_root "
                "ON orchestration_spawn_reservations(root_allocation_id, allowed)"
            )
            allocation_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(orchestration_allocations)")
            }
            for name, sql_type in (
                ("root_allocation_id", "TEXT NOT NULL DEFAULT ''"),
                ("parent_allocation_id", "TEXT"),
                ("owner_session_id", "TEXT NOT NULL DEFAULT ''"),
                ("launching_session_id", "TEXT NOT NULL DEFAULT ''"),
                ("role", "TEXT NOT NULL DEFAULT 'leaf'"),
                ("depth", "INTEGER NOT NULL DEFAULT 0"),
                ("state", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("task_state", "TEXT NOT NULL DEFAULT 'pending'"),
                ("verdict", "TEXT"),
                ("adjudication_state", "TEXT NOT NULL DEFAULT 'pending'"),
                ("adjudicated_at", "REAL"),
                ("adjudication_error", "TEXT"),
                ("terminal_reason", "TEXT"),
                ("resource_state", "TEXT NOT NULL DEFAULT 'owned'"),
                ("generation", "INTEGER NOT NULL DEFAULT 1"),
                ("owner_pid", "INTEGER NOT NULL DEFAULT 0"),
                ("owner_started_at", "INTEGER"),
                ("resource_claims_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("terminal_receipt_json", "TEXT"),
                ("receipt_digest", "TEXT"),
                ("resource_receipt_json", "TEXT"),
                ("resource_receipt_digest", "TEXT"),
                ("created_at", "REAL NOT NULL DEFAULT 0"),
                ("updated_at", "REAL NOT NULL DEFAULT 0"),
            ):
                if name not in allocation_columns:
                    conn.execute(
                        f'ALTER TABLE orchestration_allocations ADD COLUMN "{name}" {sql_type}'
                    )
            conn.execute(
                """UPDATE orchestration_allocations
                      SET root_allocation_id=allocation_id
                    WHERE root_allocation_id=''"""
            )
            conn.execute(
                """UPDATE orchestration_allocations
                      SET launching_session_id=owner_session_id
                    WHERE launching_session_id=''"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_orch_parent ON orchestration_allocations(parent_allocation_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_orch_root ON orchestration_allocations(root_allocation_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_orch_launcher ON orchestration_allocations(launching_session_id)"
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise AllocationNotFound("allocation does not exist")
        return dict(row)

    @staticmethod
    def _operation_replay(
        conn: sqlite3.Connection, operation_id: str, allocation_id: str
    ) -> Optional[dict[str, Any]]:
        event = conn.execute(
            "SELECT allocation_id FROM orchestration_events WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if event is None:
            return None
        if str(event["allocation_id"]) != allocation_id:
            raise LedgerError(
                f"operation_id {operation_id!r} already belongs to another allocation"
            )
        row = conn.execute(
            "SELECT * FROM orchestration_allocations WHERE allocation_id=?",
            (allocation_id,),
        ).fetchone()
        return OrchestrationLedger._row(row)

    def allocate(
        self,
        *,
        allocation_id: str,
        owner_session_id: str,
        role: str,
        operation_id: str,
        parent_allocation_id: Optional[str] = None,
        launching_session_id: Optional[str] = None,
        resource_claims: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        if not allocation_id or not owner_session_id or not operation_id:
            raise ValueError("allocation_id, owner_session_id and operation_id are required")
        if role not in {"leaf", "orchestrator"}:
            raise ValueError("role must be leaf or orchestrator")
        now = self.clock()
        with self._transaction() as conn:
            replay = self._operation_replay(conn, operation_id, allocation_id)
            if replay is not None:
                return replay
            existing = conn.execute(
                "SELECT * FROM orchestration_allocations WHERE allocation_id=?",
                (allocation_id,),
            ).fetchone()
            if existing is not None:
                raise LedgerError(
                    f"allocation {allocation_id} already exists under a different operation"
                )
            if parent_allocation_id:
                parent = conn.execute(
                    "SELECT * FROM orchestration_allocations WHERE allocation_id=?",
                    (parent_allocation_id,),
                ).fetchone()
                if parent is None:
                    raise AllocationNotFound(parent_allocation_id)
                if parent["state"] not in LIVE_STATES:
                    raise InvalidTransition(
                        f"terminal parent {parent_allocation_id} cannot allocate a child"
                    )
                root_id = str(parent["root_allocation_id"])
                depth = int(parent["depth"]) + 1
            else:
                root_id = allocation_id
                depth = 0
            claims = dict(resource_claims or {})
            claims.setdefault("owner_pid", self.owner_pid)
            claims.setdefault("owner_started_at", self.owner_started_at)
            launcher = str(launching_session_id or owner_session_id)
            conn.execute(
                """INSERT INTO orchestration_allocations (
                    allocation_id, root_allocation_id, parent_allocation_id,
                    owner_session_id, launching_session_id, role, depth, state,
                    generation, owner_pid,
                    owner_started_at, resource_claims_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', 1, ?, ?, ?, ?, ?)""",
                (
                    allocation_id,
                    root_id,
                    parent_allocation_id,
                    owner_session_id,
                    launcher,
                    role,
                    depth,
                    self.owner_pid,
                    self.owner_started_at,
                    _canonical_json(claims),
                    now,
                    now,
                ),
            )
            event = {
                "kind": "allocated",
                "owner_session_id": owner_session_id,
                "launching_session_id": launcher,
                "parent_allocation_id": parent_allocation_id,
                "root_allocation_id": root_id,
                "role": role,
                "resource_claims": claims,
            }
            conn.execute(
                """INSERT INTO orchestration_events
                   (allocation_id, operation_id, from_state, to_state, generation,
                    event_json, event_digest, created_at)
                   VALUES (?, ?, NULL, 'running', 1, ?, ?, ?)""",
                (allocation_id, operation_id, _canonical_json(event), _digest(event), now),
            )
            return self._row(
                conn.execute(
                    "SELECT * FROM orchestration_allocations WHERE allocation_id=?",
                    (allocation_id,),
                ).fetchone()
            )

    def reserve_spawn(
        self,
        *,
        root_allocation_id: str,
        owner_session_id: str,
        operation_id: str,
        limit: int,
    ) -> dict[str, Any]:
        """Permanently reserve one dispatch slot for a canonical root lineage."""
        if not root_allocation_id or not owner_session_id or not operation_id:
            raise ValueError(
                "root_allocation_id, owner_session_id and operation_id are required"
            )
        normalized_limit = max(0, int(limit or 0))
        with self._transaction() as conn:
            # The decision is a read-modify-write invariant. A deferred SQLite
            # transaction allows concurrent readers to observe the same count
            # and oversubscribe before either inserts, so acquire the writer
            # reservation before the replay/count checks.
            conn.execute("BEGIN IMMEDIATE")
            replay = conn.execute(
                """SELECT allowed, used_after, configured_limit
                     FROM orchestration_spawn_reservations WHERE operation_id=?""",
                (operation_id,),
            ).fetchone()
            if replay is not None:
                replay_limit = int(replay["configured_limit"])
                used = int(replay["used_after"])
                return {
                    "allowed": bool(replay["allowed"]),
                    "used": used,
                    "remaining": max(0, replay_limit - used),
                    "limit": replay_limit,
                }
            used = int(
                conn.execute(
                    """SELECT COUNT(*) FROM orchestration_spawn_reservations
                         WHERE root_allocation_id=? AND allowed=1""",
                    (root_allocation_id,),
                ).fetchone()[0]
            )
            allowed = normalized_limit <= 0 or used < normalized_limit
            used_after = used + (1 if allowed and normalized_limit > 0 else 0)
            conn.execute(
                """INSERT INTO orchestration_spawn_reservations
                   (operation_id, root_allocation_id, owner_session_id, allowed,
                    used_after, configured_limit, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    operation_id,
                    root_allocation_id,
                    owner_session_id,
                    int(allowed),
                    used_after,
                    normalized_limit,
                    self.clock(),
                ),
            )
            return {
                "allowed": allowed,
                "used": used_after,
                "remaining": max(0, normalized_limit - used_after),
                "limit": normalized_limit,
            }

    def spawn_budget_status(
        self, root_allocation_id: str, *, limit: int
    ) -> dict[str, Any]:
        """Return durable aggregate usage for one canonical root lineage."""
        normalized_limit = max(0, int(limit or 0))
        with self._transaction() as conn:
            used = int(
                conn.execute(
                    """SELECT COUNT(*) FROM orchestration_spawn_reservations
                         WHERE root_allocation_id=? AND allowed=1""",
                    (root_allocation_id,),
                ).fetchone()[0]
            )
        return {
            "used": used,
            "remaining": (
                None if normalized_limit <= 0 else max(0, normalized_limit - used)
            ),
            "limit": normalized_limit,
        }

    def get(self, allocation_id: str) -> dict[str, Any]:
        with self._transaction() as conn:
            return self._row(
                conn.execute(
                    "SELECT * FROM orchestration_allocations WHERE allocation_id=?",
                    (allocation_id,),
                ).fetchone()
            )

    def find_live_by_owner_session(self, owner_session_id: str) -> Optional[dict[str, Any]]:
        """Return the newest live allocation owned by an exact session id."""
        if not owner_session_id:
            return None
        placeholders = ",".join("?" for _ in LIVE_STATES)
        with self._transaction() as conn:
            row = conn.execute(
                f"SELECT * FROM orchestration_allocations "
                f"WHERE owner_session_id=? AND state IN ({placeholders}) "
                "ORDER BY created_at DESC, allocation_id DESC LIMIT 1",
                (owner_session_id, *sorted(LIVE_STATES)),
            ).fetchone()
            return dict(row) if row is not None else None

    def find_live_by_launching_session(
        self, launching_session_id: str
    ) -> Optional[dict[str, Any]]:
        """Return live work commissioned by an exact stable session id."""
        if not launching_session_id:
            return None
        placeholders = ",".join("?" for _ in LIVE_STATES)
        with self._transaction() as conn:
            row = conn.execute(
                f"SELECT * FROM orchestration_allocations "
                f"WHERE launching_session_id=? AND "
                f"(state IN ({placeholders}) OR adjudication_state!='adjudicated') "
                "ORDER BY created_at DESC, allocation_id DESC LIMIT 1",
                (launching_session_id, *sorted(LIVE_STATES)),
            ).fetchone()
            return dict(row) if row is not None else None

    def record_adjudication(
        self,
        allocation_id: str,
        *,
        operation_id: str,
        success: bool,
        error: str = "",
    ) -> dict[str, Any]:
        """Persist parent consumption without changing resource lifecycle state."""
        now = self.clock()
        adjudication_state = "adjudicated" if success else "failed"
        with self._transaction() as conn:
            replay = self._operation_replay(conn, operation_id, allocation_id)
            if replay is not None:
                return replay
            current = self._row(
                conn.execute(
                    "SELECT * FROM orchestration_allocations WHERE allocation_id=?",
                    (allocation_id,),
                ).fetchone()
            )
            generation = int(current["generation"]) + 1
            cursor = conn.execute(
                """UPDATE orchestration_allocations
                      SET adjudication_state=?, adjudicated_at=?,
                          adjudication_error=?, generation=?, updated_at=?
                    WHERE allocation_id=? AND generation=?""",
                (
                    adjudication_state,
                    now,
                    (error or "")[:2000] or None,
                    generation,
                    now,
                    allocation_id,
                    current["generation"],
                ),
            )
            if cursor.rowcount != 1:
                raise GenerationMismatch("allocation generation changed concurrently")
            event = {
                "kind": "completion_adjudicated",
                "success": success,
                "error": (error or "")[:2000] or None,
            }
            conn.execute(
                """INSERT INTO orchestration_events
                   (allocation_id, operation_id, from_state, to_state, generation,
                    event_json, event_digest, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    allocation_id,
                    operation_id,
                    current["state"],
                    current["state"],
                    generation,
                    _canonical_json(event),
                    _digest(event),
                    now,
                ),
            )
            return self._row(
                conn.execute(
                    "SELECT * FROM orchestration_allocations WHERE allocation_id=?",
                    (allocation_id,),
                ).fetchone()
            )

    def record_adjudication_tree(
        self, allocation_id: str, *, success: bool, error: str = ""
    ) -> None:
        """Adjudicate one delivered dispatch and its canonical batch children."""
        allocation_ids = [allocation_id]
        allocation_ids.extend(
            str(row["allocation_id"]) for row in self.descendants(allocation_id)
        )
        outcome = "ok" if success else "failed"
        for child_id in allocation_ids:
            self.record_adjudication(
                child_id,
                operation_id=f"adjudicate:{allocation_id}:{child_id}:{outcome}",
                success=success,
                error=error,
            )

    def descendants(self, allocation_id: str) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            rows = conn.execute(
                """WITH RECURSIVE descendants(allocation_id) AS (
                       SELECT allocation_id FROM orchestration_allocations
                       WHERE parent_allocation_id=?
                       UNION ALL
                       SELECT a.allocation_id FROM orchestration_allocations a
                       JOIN descendants d ON a.parent_allocation_id=d.allocation_id
                   )
                   SELECT a.* FROM orchestration_allocations a
                   JOIN descendants d ON a.allocation_id=d.allocation_id
                   ORDER BY a.depth, a.created_at, a.allocation_id""",
                (allocation_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def finalization_gate(self, allocation_id: str) -> dict[str, Any]:
        self.get(allocation_id)
        descendants = self.descendants(allocation_id)
        active = [r["allocation_id"] for r in descendants if r["state"] in LIVE_STATES]
        unreconciled = [
            r["allocation_id"]
            for r in descendants
            if r["state"] == "terminal_success" and r["resource_state"] != "reaped"
        ]
        return {
            "allocation_id": allocation_id,
            "allowed": not active and not unreconciled,
            "active_descendants": active,
            "unreconciled_successful_descendants": unreconciled,
        }

    def transition(
        self,
        allocation_id: str,
        *,
        expected_generation: int,
        operation_id: str,
        new_state: str,
        event: Mapping[str, Any],
        updates: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        now = self.clock()
        with self._transaction() as conn:
            replay = self._operation_replay(conn, operation_id, allocation_id)
            if replay is not None:
                return replay
            row = conn.execute(
                "SELECT * FROM orchestration_allocations WHERE allocation_id=?",
                (allocation_id,),
            ).fetchone()
            current = self._row(row)
            if int(current["generation"]) != int(expected_generation):
                raise GenerationMismatch(
                    f"expected generation {expected_generation}, current {current['generation']}"
                )
            current_state = str(current["state"])
            if new_state not in _ALLOWED_TRANSITIONS.get(current_state, set()):
                raise InvalidTransition(f"illegal transition {current_state} -> {new_state}")
            generation = int(current["generation"]) + 1
            allowed_columns = {
                "task_state",
                "verdict",
                "terminal_reason",
                "resource_state",
                "terminal_receipt_json",
                "receipt_digest",
                "resource_receipt_json",
                "resource_receipt_digest",
                "owner_session_id",
            }
            values = {k: v for k, v in dict(updates or {}).items() if k in allowed_columns}
            assignments = ["state=?", "generation=?", "updated_at=?"]
            args: list[Any] = [new_state, generation, now]
            for key, value in values.items():
                assignments.append(f"{key}=?")
                args.append(value)
            args.extend([allocation_id, expected_generation])
            cursor = conn.execute(
                f"UPDATE orchestration_allocations SET {', '.join(assignments)} "
                "WHERE allocation_id=? AND generation=?",
                tuple(args),
            )
            if cursor.rowcount != 1:
                raise GenerationMismatch("allocation generation changed concurrently")
            payload = dict(event)
            payload.setdefault("kind", "state_transition")
            conn.execute(
                """INSERT INTO orchestration_events
                   (allocation_id, operation_id, from_state, to_state, generation,
                    event_json, event_digest, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    allocation_id,
                    operation_id,
                    current_state,
                    new_state,
                    generation,
                    _canonical_json(payload),
                    _digest(payload),
                    now,
                ),
            )
            return self._row(
                conn.execute(
                    "SELECT * FROM orchestration_allocations WHERE allocation_id=?",
                    (allocation_id,),
                ).fetchone()
            )

    def record_terminal_receipt(
        self,
        allocation_id: str,
        *,
        expected_generation: int,
        operation_id: str,
        task_state: str,
        verdict: Optional[str],
        terminal_reason: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        current = self.get(allocation_id)
        if current["state"] == "terminal_success":
            with self._transaction() as conn:
                replay = self._operation_replay(conn, operation_id, allocation_id)
                if replay is not None:
                    return replay
        success = task_state == "complete" and terminal_reason == "normal"
        if success:
            gate = self.finalization_gate(allocation_id)
            if not gate["allowed"]:
                raise FinalizationBlocked(allocation_id, gate)
            new_state = "terminal_success"
        elif terminal_reason in {"interrupted", "cancelled"}:
            new_state = "interrupted"
        elif terminal_reason == "unknown":
            new_state = "unknown"
        else:
            new_state = "terminal_failure"
        receipt = {
            "allocation_id": allocation_id,
            "generation": expected_generation,
            "task_state": task_state,
            "verdict": verdict,
            "terminal_reason": terminal_reason,
            "result": dict(result),
            "recorded_at": self.clock(),
        }
        return self.transition(
            allocation_id,
            expected_generation=expected_generation,
            operation_id=operation_id,
            new_state=new_state,
            event={"kind": "terminal_receipt", "receipt_digest": _digest(receipt)},
            updates={
                "task_state": task_state,
                "verdict": verdict,
                "terminal_reason": terminal_reason,
                "terminal_receipt_json": _canonical_json(receipt),
                "receipt_digest": _digest(receipt),
                "resource_state": "owned" if success else "retained",
                **(
                    {"owner_session_id": str(result.get("child_session_id"))}
                    if result.get("child_session_id")
                    else {}
                ),
            },
        )

    def mark_resource_reaped(
        self,
        allocation_id: str,
        *,
        expected_generation: int,
        operation_id: str,
        resource_receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        current = self.get(allocation_id)
        if current["state"] == "reaped":
            with self._transaction() as conn:
                replay = self._operation_replay(conn, operation_id, allocation_id)
                if replay is not None:
                    return replay
        gate = self.finalization_gate(allocation_id)
        if not gate["allowed"]:
            raise FinalizationBlocked(allocation_id, gate)
        receipt = dict(resource_receipt)
        if receipt.get("verified_absent") is not True:
            raise LedgerError("resource receipt must prove verified_absent=true")
        return self.transition(
            allocation_id,
            expected_generation=expected_generation,
            operation_id=operation_id,
            new_state="reaped",
            event={"kind": "resource_reaped", "receipt_digest": _digest(receipt)},
            updates={
                "resource_state": "reaped",
                "resource_receipt_json": _canonical_json(receipt),
                "resource_receipt_digest": _digest(receipt),
            },
        )

    def retain_dispatch_failure(
        self,
        allocation_id: str,
        *,
        reason: str,
        operation_prefix: str,
    ) -> dict[str, Any]:
        """Fail closed after transport persistence rejects an allocated unit."""
        current = self.get(allocation_id)
        if current["state"] in LIVE_STATES:
            current = self.transition(
                allocation_id,
                expected_generation=int(current["generation"]),
                operation_id=f"{operation_prefix}:terminal",
                new_state="terminal_failure",
                event={"kind": "dispatch_persistence_failed", "reason": reason[:2000]},
                updates={
                    "task_state": "failed",
                    "terminal_reason": "dispatch_persistence_failed",
                    "resource_state": "retained",
                },
            )
        if current["state"] == "terminal_failure":
            current = self.transition(
                allocation_id,
                expected_generation=int(current["generation"]),
                operation_id=f"{operation_prefix}:retain",
                new_state="retained_diagnostic",
                event={"kind": "resource_retained", "reason": reason[:2000]},
                updates={"resource_state": "retained"},
            )
        return current

    def recover_stale_owners(self) -> list[str]:
        """Fence live allocations whose exact PID/start identity disappeared."""
        placeholders = ",".join("?" for _ in LIVE_STATES)
        with self._transaction() as conn:
            rows = conn.execute(
                f"SELECT allocation_id FROM orchestration_allocations "
                f"WHERE state IN ({placeholders}) ORDER BY depth DESC, created_at",
                tuple(sorted(LIVE_STATES)),
            ).fetchall()
        recovered: list[str] = []
        for item in rows:
            allocation_id = str(item["allocation_id"])
            measured = self.collect_live_state(allocation_id)
            owner = measured["owner_process"]
            if owner["exists"] and owner["identity_match"]:
                continue
            current = self.get(allocation_id)
            try:
                unknown = self.transition(
                    allocation_id,
                    expected_generation=int(current["generation"]),
                    operation_id=f"recover-stale:{allocation_id}:{current['generation']}",
                    new_state="unknown",
                    event={"kind": "stale_owner", "measured": measured},
                    updates={
                        "task_state": "unknown",
                        "terminal_reason": "owner_identity_lost",
                        "resource_state": "retained",
                    },
                )
                self.transition(
                    allocation_id,
                    expected_generation=int(unknown["generation"]),
                    operation_id=f"retain-stale:{allocation_id}:{unknown['generation']}",
                    new_state="retained_diagnostic",
                    event={"kind": "resource_retained", "reason": "owner_identity_lost"},
                    updates={"resource_state": "retained"},
                )
                recovered.append(allocation_id)
            except (GenerationMismatch, InvalidTransition):
                continue
        return recovered

    def collect_live_state(self, allocation_id: str) -> dict[str, Any]:
        """Measure local ownership facts instead of trusting caller booleans."""
        row = self.get(allocation_id)
        pid = int(row["owner_pid"])
        expected_started = row["owner_started_at"]
        exists = _pid_exists(pid)
        actual_started = _process_start_time(pid) if exists else None
        process = {
            "pid": pid,
            "exists": exists,
            "expected_started_at": expected_started,
            "actual_started_at": actual_started,
            "identity_match": bool(
                exists
                and expected_started is not None
                and actual_started is not None
                and int(expected_started) == int(actual_started)
            ),
        }
        claims = json.loads(row["resource_claims_json"] or "{}")
        tmux_claim = claims.get("tmux") if isinstance(claims, dict) else None
        tmux: Optional[dict[str, Any]] = None
        if isinstance(tmux_claim, dict):
            executable = str(tmux_claim.get("executable") or "")
            socket = str(tmux_claim.get("socket") or "")
            session = str(tmux_claim.get("session") or "")
            tmux = {
                "executable": executable,
                "socket": socket,
                "session": session,
                "exists": False,
                "identity_match": False,
            }
            if executable and socket and session:
                try:
                    completed = subprocess.run(
                        [executable, "-L", socket, "has-session", "-t", session],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    tmux["exists"] = completed.returncode == 0
                    tmux["identity_match"] = completed.returncode == 0
                    tmux["returncode"] = completed.returncode
                except Exception as exc:
                    tmux["error"] = type(exc).__name__
        return {
            "allocation_id": allocation_id,
            "generation": row["generation"],
            "state": row["state"],
            "owner_process": process,
            "tmux": tmux,
            "measured_at": self.clock(),
        }


def get_default_ledger() -> OrchestrationLedger:
    return OrchestrationLedger()
