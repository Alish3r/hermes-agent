"""Transactional aggregate spawn-budget contracts."""

from __future__ import annotations

from tools.orchestration_ledger import OrchestrationLedger
from tools.delegate_tool import _resolve_spawn_budget_root_id


def test_spawn_reservation_is_durable_idempotent_and_root_aggregate(tmp_path):
    db = tmp_path / "state.db"
    first = OrchestrationLedger(db)

    one = first.reserve_spawn(
        root_allocation_id="deleg_root",
        owner_session_id="session-a",
        operation_id="spawn:one",
        limit=2,
    )
    replay = OrchestrationLedger(db).reserve_spawn(
        root_allocation_id="deleg_root",
        owner_session_id="session-a",
        operation_id="spawn:one",
        limit=2,
    )
    two = OrchestrationLedger(db).reserve_spawn(
        root_allocation_id="deleg_root",
        owner_session_id="session-child",
        operation_id="spawn:two",
        limit=2,
    )
    blocked = OrchestrationLedger(db).reserve_spawn(
        root_allocation_id="deleg_root",
        owner_session_id="session-child",
        operation_id="spawn:three",
        limit=2,
    )

    assert one == {"allowed": True, "used": 1, "remaining": 1, "limit": 2}
    assert replay == one
    assert two == {"allowed": True, "used": 2, "remaining": 0, "limit": 2}
    assert blocked == {"allowed": False, "used": 2, "remaining": 0, "limit": 2}


def test_rejected_reservation_is_permanent_and_does_not_create_capacity(tmp_path):
    ledger = OrchestrationLedger(tmp_path / "state.db")
    ledger.reserve_spawn(
        root_allocation_id="deleg_root",
        owner_session_id="s",
        operation_id="spawn:one",
        limit=1,
    )
    rejected = ledger.reserve_spawn(
        root_allocation_id="deleg_root",
        owner_session_id="s",
        operation_id="spawn:rejected",
        limit=1,
    )
    replay = OrchestrationLedger(tmp_path / "state.db").reserve_spawn(
        root_allocation_id="deleg_root",
        owner_session_id="s",
        operation_id="spawn:rejected",
        limit=1,
    )

    assert rejected == replay
    assert rejected["allowed"] is False
    assert ledger.spawn_budget_status("deleg_root", limit=1)["used"] == 1


def test_top_level_budget_key_is_stable_across_dispatch_ids(tmp_path):
    ledger = OrchestrationLedger(tmp_path / "state.db")

    first = _resolve_spawn_budget_root_id(
        ledger=ledger,
        parent_allocation_id="",
        origin_session_id="session-a",
    )
    second = _resolve_spawn_budget_root_id(
        ledger=ledger,
        parent_allocation_id="",
        origin_session_id="session-a",
    )

    assert first == second == "session:session-a"


def test_concurrent_reservations_never_oversubscribe_root_budget(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    db = tmp_path / "state.db"
    OrchestrationLedger(db)  # initialize schema before the barrier
    barrier = threading.Barrier(10)

    def reserve(index):
        barrier.wait()
        return OrchestrationLedger(db).reserve_spawn(
            root_allocation_id="session:root",
            owner_session_id=f"session-{index}",
            operation_id=f"spawn:{index}",
            limit=3,
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(reserve, range(10)))

    assert sum(bool(result["allowed"]) for result in results) == 3
    assert OrchestrationLedger(db).spawn_budget_status(
        "session:root", limit=3
    ) == {"used": 3, "remaining": 0, "limit": 3}
