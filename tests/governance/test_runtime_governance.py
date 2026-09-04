from governance import (
    counters,
    finish_route,
    list_routes,
    recover_open_routes,
    start_route,
)


def test_route_start_terminal_and_abandoned_recovery_are_durable(monkeypatch):
    completed = start_route("provider-a", "model-a")
    finish_route(completed["route_id"], "completed")
    open_route = start_route("provider-b", "model-b")
    monkeypatch.setattr("governance._owner_is_live", lambda *_: False)
    assert recover_open_routes() == 1
    rows = list_routes()
    assert [row["status"] for row in rows] == ["completed", "unknown"]
    assert counters()["unknown"] == 1
    assert open_route["route_id"] == rows[-1]["route_id"]


def test_recovery_does_not_mark_a_live_route_unknown():
    route = start_route("provider-a", "model-a")
    assert recover_open_routes() == 0
    assert list_routes()[-1]["route_id"] == route["route_id"]
    assert list_routes()[-1]["status"] == "start"
    finish_route(route["route_id"], "completed")


def test_recovery_preserves_ownerless_legacy_routes(monkeypatch):
    import governance

    route = start_route("legacy-provider", "legacy-model")
    with governance._LOCK:
        conn = governance._connect()
        try:
            conn.execute(
                "UPDATE routes SET owner_pid=NULL, owner_started_at=NULL WHERE route_id=?",
                (route["route_id"],),
            )
            conn.commit()
        finally:
            conn.close()
    assert recover_open_routes() == 0
    assert list_routes()[-1]["status"] == "start"
    finish_route(route["route_id"], "completed")


def test_unknown_is_a_terminal_route_status():
    route = start_route("provider-a", "model-a")
    finished = finish_route(route["route_id"], "unknown", error="provider outcome unavailable")
    assert finished["terminal_persisted"] is True
    assert list_routes()[-1]["status"] == "unknown"


def test_allow_list_refuses_before_execution():
    refused = start_route("denied", "model", allow_list=[("allowed", "model")])
    assert refused["status"] == "refused"
    assert refused["durable"] is True
    assert list_routes()[-1]["status"] == "refused"


def test_route_allow_list_is_scoped_to_one_start_call():
    refused = start_route("denied", "model", allow_list=[("allowed", "model")])
    allowed = start_route("denied", "model")
    assert refused["status"] == "refused"
    assert allowed["status"] == "start"
    finish_route(allowed["route_id"], "completed")


def test_legacy_ledger_without_ownership_columns_migrates_and_keeps_its_rows():
    import os
    import sqlite3

    import governance

    conn = sqlite3.connect(governance._db_path())
    conn.execute(
        """CREATE TABLE routes (
            route_id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
            status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, error TEXT)"""
    )
    conn.execute(
        "INSERT INTO routes VALUES ('legacy-done','p','m','completed',"
        "'2026-09-04T07:30:00+00:00','2026-09-04T07:31:00+00:00',NULL)"
    )
    conn.execute(
        "INSERT INTO routes VALUES ('legacy-open','p','m','start','2026-09-04T07:32:00+00:00',NULL,NULL)"
    )
    conn.commit()
    conn.close()

    fresh = start_route("p", "m")
    finish_route(fresh["route_id"], "completed")
    assert recover_open_routes() == 0

    rows = {row["route_id"]: row for row in list_routes()}
    assert rows["legacy-done"]["status"] == "completed"
    assert rows["legacy-open"]["status"] == "start"
    assert rows["legacy-open"]["owner_pid"] is None
    assert rows[fresh["route_id"]]["owner_pid"] == os.getpid()


def test_non_string_provider_or_model_is_recorded_as_unknown():
    """A route is a pair of strings; anything else is unknown, never a repr."""
    route = start_route(object(), object())
    finish_route(route["route_id"], "completed")
    row = list_routes()[-1]
    assert (row["provider"], row["model"]) == ("", "")
    assert start_route(object(), "model", allow_list=[("p", "model")])["status"] == "refused"


def test_finish_route_records_the_final_route_when_it_changed():
    route = start_route("first", "m1")
    terminal = finish_route(route["route_id"], "completed", provider="second", model="m2")
    assert (terminal["provider"], terminal["model"]) == ("second", "m2")
    row = list_routes()[-1]
    assert (row["provider"], row["model"], row["status"]) == ("second", "m2", "completed")


def test_first_route_start_in_a_process_recovers_abandoned_routes(monkeypatch):
    """Delegation-only processes never cross a cron recovery boundary; the ledger
    must self-heal on first use instead of leaving crashed routes open forever."""
    import governance

    with governance._LOCK:
        conn = governance._connect()
        try:
            conn.execute(
                "INSERT INTO routes(route_id, provider, model, status, started_at, owner_pid, owner_started_at)"
                " VALUES ('abandoned','p','m','start','2026-09-04T00:00:00+00:00', 999999, 1)"
            )
            conn.commit()
        finally:
            conn.close()
    monkeypatch.setattr(governance, "_RECOVERED_LEDGERS", set())

    route = start_route("p", "m")
    finish_route(route["route_id"], "completed")

    rows = {row["route_id"]: row for row in list_routes()}
    assert rows["abandoned"]["status"] == "unknown"
    assert counters()["unknown"] >= 1


def test_first_use_recovery_retries_after_a_transient_failure(monkeypatch):
    import governance

    with governance._LOCK:
        conn = governance._connect()
        try:
            conn.execute(
                "INSERT INTO routes(route_id, provider, model, status, started_at, owner_pid, owner_started_at)"
                " VALUES ('abandoned-2','p','m','start','2026-09-04T00:00:00+00:00', 999999, 1)"
            )
            conn.commit()
        finally:
            conn.close()
    monkeypatch.setattr(governance, "_RECOVERED_LEDGERS", set())
    real_recover = governance.recover_open_routes
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("transient ledger failure")
        return real_recover()

    monkeypatch.setattr(governance, "recover_open_routes", flaky)

    first = start_route("p", "m")  # recovery hiccup must not refuse the child
    finish_route(first["route_id"], "completed")
    second = start_route("p", "m")
    finish_route(second["route_id"], "completed")

    assert {row["route_id"]: row["status"] for row in list_routes()}["abandoned-2"] == "unknown"


def test_first_use_recovery_is_per_ledger_not_per_process(monkeypatch, tmp_path):
    """routes.db is profile-local: a multiplex process that already recovered
    profile A must still recover profile B on B's first route start."""
    import governance

    def _abandon(home):
        monkeypatch.setenv("HERMES_HOME", str(home))
        with governance._LOCK:
            conn = governance._connect()
            try:
                conn.execute(
                    "INSERT INTO routes(route_id, provider, model, status, started_at, owner_pid, owner_started_at)"
                    " VALUES ('abandoned','p','m','start','2026-09-04T00:00:00+00:00', 999999, 1)"
                )
                conn.commit()
            finally:
                conn.close()

    home_a, home_b = tmp_path / "a", tmp_path / "b"
    _abandon(home_a)
    _abandon(home_b)

    monkeypatch.setenv("HERMES_HOME", str(home_a))
    finish_route(start_route("p", "m")["route_id"], "completed")
    monkeypatch.setenv("HERMES_HOME", str(home_b))
    finish_route(start_route("p", "m")["route_id"], "completed")

    assert {row["route_id"]: row["status"] for row in list_routes()}["abandoned"] == "unknown"


def test_allow_list_entries_that_resolve_to_an_empty_route_never_authorize():
    from governance import route_allowed

    assert not route_allowed(object(), object(), allow_list=[{}])
    assert not route_allowed(None, None, allow_list=[("", "")])
    assert not route_allowed("p", None, allow_list=[["p", None]])
    assert route_allowed("p", "m", allow_list=[{}, ("p", "m")])


def test_terminal_routes_are_bounded_by_retention(monkeypatch):
    import governance

    monkeypatch.setattr(governance, "MAX_TERMINAL_ROUTES", 2, raising=False)
    open_route = start_route("p", "open")
    for index in range(4):
        finish_route(start_route("p", f"m{index}")["route_id"], "completed")

    rows = list_routes()
    assert sum(1 for row in rows if row["status"] == "completed") == 2
    assert any(row["route_id"] == open_route["route_id"] for row in rows)
    finish_route(open_route["route_id"], "completed")


def test_finishing_a_route_as_unknown_counts_it_as_unknown():
    before = counters().get("unknown", 0)
    route = start_route("p", "m")
    finish_route(route["route_id"], "unknown", error="worker still running at the deadline")
    assert counters().get("unknown", 0) == before + 1
    assert counters().get("failed", 0) == 0
