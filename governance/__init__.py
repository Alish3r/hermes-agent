"""Small executor-owned runtime governance ledger.

This package records route lifecycle facts without storing delegated prompts or
other task content. Policy is intentionally additive: absent an explicit
allow-list, existing routes remain allowed.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
# Terminal routes kept per ledger; open routes are never pruned and counters
# are kept separately, so retention never changes a policy decision.
MAX_TERMINAL_ROUTES = 1000
_TERMINAL = frozenset({"completed", "failed", "interrupted", "unknown", "refused"})
# Delegation-only processes never cross a cron recovery boundary, so each
# ledger self-heals on its first use in each process instead of leaving routes
# abandoned by a crashed predecessor open forever. Holds (pid, ledger path)
# pairs that completed first-use recovery: routes.db is profile-local, so a
# multiplex process recovers every profile it touches; a forked child
# recovers again; a failed attempt retries.
_RECOVERED_LEDGERS: set = set()


def _owner_start_time(pid: int) -> Optional[int]:
    """Use cron's PID-reuse-resistant owner timestamp when it is available."""
    from cron.executions import _process_start_time

    return _process_start_time(pid)


def _owner_is_live(pid: Any, started_at: Any) -> bool:
    """True only when this exact process instance is still alive."""
    from cron.executions import _owner_is_live

    # Legacy rows lack process-instance attribution.  Their owner cannot be
    # proved dead, so recovery must preserve them rather than falsely marking
    # a possibly live pre-migration route as unknown.
    if pid is None:
        return True
    try:
        return _owner_is_live(int(pid), int(started_at) if started_at is not None else None)
    except (TypeError, ValueError):
        return True


def _db_path() -> Path:
    path = get_hermes_home().resolve() / "governance" / "routes.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("""CREATE TABLE IF NOT EXISTS routes (
        route_id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        error TEXT,
        owner_pid INTEGER,
        owner_started_at INTEGER
    )""")
    # Existing profile-local ledgers predate ownership attribution.  Preserve
    # their rows, but treat any open legacy row as unowned during recovery.
    # Two processes can both see the column missing on their first connection
    # after an upgrade; the shared helper swallows the duplicate-column race.
    from hermes_cli.sqlite_util import add_column_if_missing

    columns = {row[1] for row in conn.execute("PRAGMA table_info(routes)")}
    if "owner_pid" not in columns:
        add_column_if_missing(conn, "routes", "owner_pid", "owner_pid INTEGER")
    if "owner_started_at" not in columns:
        add_column_if_missing(
            conn, "routes", "owner_started_at", "owner_started_at INTEGER"
        )
    conn.execute("""CREATE TABLE IF NOT EXISTS counters (
        name TEXT PRIMARY KEY,
        value INTEGER NOT NULL DEFAULT 0
    )""")
    conn.commit()
    return conn


def route_pair(provider: Any, model: Any) -> tuple[str, str]:
    """Public normalization used wherever a child's route is reported."""
    return _route(provider, model)


def _prune_terminal_routes(conn: sqlite3.Connection) -> None:
    limit = max(0, int(MAX_TERMINAL_ROUTES))
    conn.execute(
        """DELETE FROM routes WHERE route_id IN (
             SELECT route_id FROM routes WHERE status != 'start'
             ORDER BY finished_at DESC, started_at DESC, route_id DESC
             LIMIT -1 OFFSET ?
           )""",
        (limit,),
    )


def _route(provider: Any, model: Any) -> tuple[str, str]:
    # A route is a pair of strings.  Anything else (None, a test double, an
    # object) is recorded as unknown rather than as a repr that would never
    # match an allow-list entry or another run of the same child.
    return (
        provider if isinstance(provider, str) else "",
        model if isinstance(model, str) else "",
    )


def _normalize_allow_list(routes: Iterable[Any]) -> frozenset[tuple[str, str]]:
    """Normalize one immutable authorization decision for one route start."""
    normalized: set[tuple[str, str]] = set()
    for item in routes:
        if isinstance(item, Mapping):
            pair = _route(item.get("provider"), item.get("model"))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            pair = _route(item[0], item[1])
        elif isinstance(item, str) and ":" in item:
            provider, model = item.split(":", 1)
            pair = _route(provider, model)
        else:
            continue
        # An entry that does not name both a provider and a model is
        # malformed; it must never authorize the unresolved ("", "") route.
        if pair[0] and pair[1]:
            normalized.add(pair)
    return frozenset(normalized)


def route_allowed(provider: Any, model: Any, *, allow_list: Optional[Iterable[Any]] = None) -> bool:
    # The authorization decision belongs to this exact executor invocation.
    # Never retain it in module state: concurrent children may legitimately
    # have different policies and one child must not authorize another.
    return allow_list is None or _route(provider, model) in _normalize_allow_list(allow_list)


def _recover_once() -> None:
    key = (os.getpid(), str(_db_path()))
    with _LOCK:
        if key in _RECOVERED_LEDGERS:
            return
        try:
            recover_open_routes()
        except Exception as exc:
            # Best-effort: a ledger hiccup must not refuse the child; the next
            # route start retries the recovery.
            logger.warning("Governance route recovery deferred: %s", exc)
            return
        _RECOVERED_LEDGERS.add(key)


def start_route(provider: Any, model: Any, *, allow_list: Optional[Iterable[Any]] = None) -> dict[str, Any]:
    """Persist a fresh route id before child execution."""
    _recover_once()
    provider, model = _route(provider, model)
    route_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    owner_pid = os.getpid()
    owner_started_at = _owner_start_time(owner_pid)
    status = "start" if route_allowed(provider, model, allow_list=allow_list) else "refused"
    error = None if status == "start" else "Route is not permitted by the executor allow-list."
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO routes
                   (route_id, provider, model, status, started_at, finished_at,
                    error, owner_pid, owner_started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (route_id, provider, model, status, now, now if error else None,
                 error, owner_pid, owner_started_at),
            )
            if status == "refused":
                conn.execute("INSERT INTO counters(name,value) VALUES('refused',1) ON CONFLICT(name) DO UPDATE SET value=value+1")
                _prune_terminal_routes(conn)
            conn.commit()
        finally:
            conn.close()
    return {"route_id": route_id, "provider": provider, "model": model,
            "status": status, "durable": True,
            "terminal_persisted": status == "refused"}


def finish_route(
    route_id: str,
    status: str,
    *,
    error: Optional[str] = None,
    provider: Any = None,
    model: Any = None,
) -> dict[str, Any]:
    """Write the terminal status once.

    ``provider``/``model`` record the route that actually ran: an allowed
    fallback can change a child's route mid-run, and the ledger must describe
    the final route rather than the admitted one.
    """
    if status not in _TERMINAL:
        raise ValueError(f"invalid route terminal status: {status}")
    now = datetime.now(timezone.utc).isoformat()
    final = _route(provider, model) if (provider is not None or model is not None) else None
    with _LOCK:
        conn = _connect()
        try:
            if final is None:
                cur = conn.execute(
                    "UPDATE routes SET status=?, finished_at=?, error=? WHERE route_id=? AND status='start'",
                    (status, now, str(error) if error else None, route_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE routes SET status=?, finished_at=?, error=?, provider=?, model=? "
                    "WHERE route_id=? AND status='start'",
                    (status, now, str(error) if error else None, final[0], final[1], route_id),
                )
            if cur.rowcount != 1:
                raise RuntimeError("route is missing or already terminal")
            if status in ("failed", "unknown"):
                conn.execute(
                    "INSERT INTO counters(name,value) VALUES(?,1) "
                    "ON CONFLICT(name) DO UPDATE SET value=value+1",
                    (status,),
                )
            _prune_terminal_routes(conn)
            conn.commit()
        finally:
            conn.close()
    result: dict[str, Any] = {"route_id": route_id, "status": status, "durable": True,
                              "terminal_persisted": True}
    if final is not None:
        result["provider"], result["model"] = final
    return result


def recover_open_routes() -> int:
    """Mark open routes unknown and count them durably.

    A process restart cannot establish whether a provider call or child side
    effect completed, so ``interrupted`` would overstate what the executor
    knows. Unknown routes are reported (ledger rows and the ``unknown``
    counter) for operator review; routes do not gate execution.
    """
    with _LOCK:
        conn = _connect()
        try:
            now = datetime.now(timezone.utc).isoformat()
            rows = conn.execute(
                "SELECT route_id, owner_pid, owner_started_at FROM routes WHERE status='start'"
            ).fetchall()
            recovered = 0
            for row in rows:
                if _owner_is_live(row["owner_pid"], row["owner_started_at"]):
                    continue
                cur = conn.execute(
                    """UPDATE routes SET status='unknown', finished_at=?, error=?
                       WHERE route_id=? AND status='start'""",
                    (now, "Executor recovered an abandoned open route; terminal outcome was unknown.", row["route_id"]),
                )
                recovered += cur.rowcount
            if recovered:
                conn.execute("INSERT INTO counters(name,value) VALUES('unknown',?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value", (recovered,))
                _prune_terminal_routes(conn)
            conn.commit()
            return recovered
        finally:
            conn.close()


def list_routes() -> list[dict[str, Any]]:
    with _LOCK:
        conn = _connect()
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM routes ORDER BY started_at").fetchall()]
        finally:
            conn.close()


def counters() -> dict[str, int]:
    with _LOCK:
        conn = _connect()
        try:
            return {row["name"]: int(row["value"]) for row in conn.execute("SELECT name,value FROM counters")}
        finally:
            conn.close()
