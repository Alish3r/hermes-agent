"""Durable compression epoch/cap contracts."""

from __future__ import annotations

from types import SimpleNamespace

from agent.conversation_compression import (
    COMPRESSION_EPOCH_MODEL_CONFIG_KEY,
    compression_epoch_state,
)


class _DB:
    def __init__(self, value):
        self.value = value

    def get_session_model_config_value(self, session_id, key, default=0):
        assert session_id == "session-a"
        assert key == COMPRESSION_EPOCH_MODEL_CONFIG_KEY
        return self.value


class _BrokenDB:
    def get_session_model_config_value(self, session_id, key, default=0):
        raise RuntimeError("database unavailable")


def test_compression_epoch_survives_fresh_agent_from_durable_session_metadata():
    first = SimpleNamespace(
        session_id="session-a", _session_db=_DB(3), max_compression_epochs=4
    )
    resumed = SimpleNamespace(
        session_id="session-a", _session_db=_DB("3"), max_compression_epochs=4
    )

    assert compression_epoch_state(first) == {
        "count": 3,
        "limit": 4,
        "remaining": 1,
        "blocked": False,
    }
    assert compression_epoch_state(resumed) == compression_epoch_state(first)


def test_compression_epoch_cap_fails_closed_and_malformed_legacy_is_safe():
    capped = SimpleNamespace(
        session_id="session-a", _session_db=_DB(4), max_compression_epochs=4
    )
    legacy = SimpleNamespace(
        session_id="session-a", _session_db=_DB("not-an-int"), max_compression_epochs=4
    )

    assert compression_epoch_state(capped)["blocked"] is True
    assert compression_epoch_state(capped)["remaining"] == 0
    assert compression_epoch_state(legacy)["count"] == 4
    assert compression_epoch_state(legacy)["blocked"] is True
    assert compression_epoch_state(legacy)["read_error"] is True


def test_compression_epoch_read_error_fails_closed_at_cap():
    agent = SimpleNamespace(
        session_id="session-a", _session_db=_BrokenDB(), max_compression_epochs=4
    )

    state = compression_epoch_state(agent)

    assert state["count"] == 4
    assert state["remaining"] == 0
    assert state["blocked"] is True
    assert state["read_error"] is True


def test_compression_epoch_limit_reads_durable_config_when_agent_has_no_override(
    monkeypatch,
):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"compression": {"max_epochs": 7}},
    )
    agent = SimpleNamespace(session_id="session-a", _session_db=_DB(4))

    state = compression_epoch_state(agent)

    assert state == {"count": 4, "limit": 7, "remaining": 3, "blocked": False}
