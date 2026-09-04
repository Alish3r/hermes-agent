from governance import list_routes
from tools.delegate_tool import _run_single_child


class FakeChild:
    provider = "denied"
    model = "model"
    _route_allow_list = [("allowed", "model")]
    tool_progress_callback = None
    _subagent_id = None
    _credential_pool = None
    _delegate_saved_tool_names = []

    def __init__(self):
        self.ran = False
        self.closed = False

    def run_conversation(self, **kwargs):
        self.ran = True
        raise AssertionError("disallowed route must be refused before child run")

    def close(self):
        self.closed = True


def test_disallowed_route_is_refused_before_child_run(monkeypatch):
    monkeypatch.setattr(
        "tools.delegate_tool._get_route_allow_list", lambda: [("allowed", "model")]
    )
    child = FakeChild()
    result = _run_single_child(0, "redacted task", child=child)
    assert result["status"] == "failed"
    assert result["route"]["status"] == "refused"
    assert result["route"]["durable"] is True
    assert child.ran is False
    assert child.closed is True
    assert list_routes()[-1]["status"] == "refused"


def test_child_cannot_supply_its_own_route_allow_list(monkeypatch):
    child = FakeChild()
    child._route_allow_list = [("denied", "model")]
    monkeypatch.setattr("tools.delegate_tool._get_route_allow_list", lambda: [])

    result = _run_single_child(0, "redacted task", child=child)

    assert result["route"]["status"] == "refused"
    assert child.ran is False


def test_terminal_write_failure_preserves_the_assembled_terminal_status(monkeypatch):
    child = FakeChild()
    monkeypatch.setattr("tools.delegate_tool._get_route_allow_list", lambda: None)
    monkeypatch.setattr(
        "tools.delegate_tool.finish_route",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ledger unavailable")),
    )

    result = _run_single_child(0, "redacted task", child=child)

    assert result["route"]["status"] == "failed"
    assert result["route"]["terminal_persisted"] is False
    assert result["route"]["durable"] is False


def test_route_allow_list_reads_the_delegation_config_subtree(monkeypatch):
    from tools.delegate_tool import _get_route_allow_list

    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {
            "delegation": {"route_allow_list": ["allowed:model"]},
            "route_allow_list": ["decoy:model"],
        },
    )

    assert _get_route_allow_list() == ["allowed:model"]


def test_route_allow_list_absent_keeps_allow_by_default(monkeypatch):
    from tools.delegate_tool import _get_route_allow_list

    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"delegation": {"max_concurrent_children": 2}},
    )

    assert _get_route_allow_list() is None


import pytest


@pytest.mark.parametrize("malformed", ["openai:gpt", 3, True, {"provider": "p"}])
def test_route_allow_list_malformed_value_fails_closed(monkeypatch, malformed):
    from tools.delegate_tool import _get_route_allow_list

    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"delegation": {"route_allow_list": malformed}},
    )

    assert _get_route_allow_list() == []


def test_governed_entry_carries_no_per_run_identifier(monkeypatch):
    """The parent-visible route block reports facts, not ledger keys: two identical
    children must produce byte-identical entries (upstream pins this contract)."""
    import json

    monkeypatch.setattr("tools.delegate_tool._get_route_allow_list", lambda: None)
    entries = []
    for _ in range(2):
        entry = _run_single_child(0, "redacted task", child=FakeChild())
        entry.pop("duration_seconds", None)
        entries.append(json.dumps(entry, sort_keys=True, default=str))

    assert entries[0] == entries[1]
    assert "route_id" not in json.loads(entries[0])["route"]


def test_allow_list_prunes_the_inherited_fallback_chain_before_the_run(monkeypatch):
    """Fallback activation rewrites provider/model mid-run; a governed child must
    never be able to fall back onto a route the allow-list refuses."""
    monkeypatch.setattr(
        "tools.delegate_tool._get_route_allow_list",
        lambda: [("allowed", "model"), ("allowed", "m3")],
    )
    child = FakeChild()
    child.provider = "allowed"
    child.model = "model"
    child._fallback_chain = [
        {"provider": "denied", "model": "m2"},
        {"provider": "allowed", "model": "m3"},
    ]

    result = _run_single_child(0, "redacted task", child=child)

    assert result["route"]["status"] == "failed"  # FakeChild.run_conversation raises
    assert child._fallback_chain == [{"provider": "allowed", "model": "m3"}]


def test_terminal_route_records_the_route_that_actually_ran(monkeypatch):
    """An allowed fallback changes the child's route mid-run; the ledger and the
    parent-visible block must report the final route, not the admitted one."""
    monkeypatch.setattr("tools.delegate_tool._get_route_allow_list", lambda: None)

    class SwitchingChild(FakeChild):
        provider = "first"
        model = "m1"

        def run_conversation(self, **kwargs):
            self.provider = "second"
            self.model = "m2"
            raise AssertionError("simulated failure after fallback")

    result = _run_single_child(0, "redacted task", child=SwitchingChild())

    assert (result["route"]["provider"], result["route"]["model"]) == ("second", "m2")
    row = list_routes()[-1]
    assert (row["provider"], row["model"]) == ("second", "m2")


def test_start_persistence_failure_keeps_the_route_block_deterministic(monkeypatch):
    import json

    monkeypatch.setattr("tools.delegate_tool._get_route_allow_list", lambda: None)
    monkeypatch.setattr(
        "tools.delegate_tool.start_route",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ledger down")),
    )
    entries = []
    for _ in range(2):
        child = FakeChild()
        child.provider = object()
        child.model = object()
        entry = _run_single_child(0, "redacted task", child=child)
        entry.pop("duration_seconds", None)
        entries.append(json.dumps(entry, sort_keys=True, default=str))

    assert entries[0] == entries[1]
    assert json.loads(entries[0])["route"]["durable"] is False


def test_allow_list_is_honored_from_a_raw_config_yaml(monkeypatch, tmp_path):
    """E2E: the key needs no DEFAULT_CONFIG entry to reach the executor."""
    from tools.delegate_tool import _get_route_allow_list

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "delegation:\n  route_allow_list:\n    - 'allowed:model'\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)

    assert _get_route_allow_list() == ["allowed:model"]


def test_timed_out_child_with_a_live_worker_is_recorded_unknown_not_interrupted(monkeypatch):
    """The executor gave up at its deadline but the worker thread is still running:
    the ledger cannot claim the child finished, so the terminal status is unknown."""
    import threading

    release = threading.Event()

    class StuckChild(FakeChild):
        provider = "p"
        model = "m"

        def run_conversation(self, **kwargs):
            release.wait(timeout=30)
            return {"final_response": "late", "completed": True, "interrupted": False,
                    "api_calls": 1, "messages": []}

    monkeypatch.setattr("tools.delegate_tool._get_route_allow_list", lambda: None)
    monkeypatch.setattr("tools.delegate_tool._get_child_timeout", lambda: 0.2)
    try:
        result = _run_single_child(0, "redacted task", child=StuckChild())
        assert result["route"]["status"] == "unknown"
        assert list_routes()[-1]["status"] == "unknown"
    finally:
        release.set()
