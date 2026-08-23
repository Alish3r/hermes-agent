"""Effective-context contracts for Codex GPT-5.x compression thresholds."""

from unittest.mock import patch

import pytest

from agent.auxiliary_client import _compression_threshold_for_model
from agent.context_compressor import ContextCompressor


def _compressor(
    *,
    model: str = "gpt-5.6-sol",
    provider: str = "openai-codex",
    context_length: int = 900_000,
    threshold: float = 0.50,
    model_thresholds=None,
    threshold_tokens_cap=None,
    autoraise: bool = True,
) -> ContextCompressor:
    compressor = ContextCompressor(
        model=model,
        provider=provider,
        threshold_percent=threshold,
        model_thresholds=model_thresholds,
        threshold_tokens_cap=threshold_tokens_cap,
        config_context_length=context_length,
        allow_codex_gpt55_autoraise=autoraise,
        quiet_mode=True,
    )
    _ = compressor.context_length
    return compressor


def _build_agent(*, model: str, provider: str, context_length: int, compression=None):
    cfg = {
        "agent": {},
        "model": {"default": model, "provider": provider},
        "compression": {"threshold": 0.50, **(compression or {})},
    }
    base_url = (
        "https://chatgpt.com/backend-api/codex"
        if provider == "openai-codex"
        else "https://openrouter.ai/api/v1"
    )
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("agent.model_metadata.get_model_context_length", return_value=context_length),
        patch("agent.context_compressor.get_model_context_length", return_value=context_length),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        return AIAgent(
            model=model,
            provider=provider,
            api_key="test-key-1234567890",
            base_url=base_url,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )


def test_codex_gpt56_900k_effective_context_keeps_global_threshold():
    """Regression: base gpt-5.6-sol at a resolved 900K window must not use 85%."""
    agent = _build_agent(
        model="gpt-5.6-sol",
        provider="openai-codex",
        context_length=900_000,
    )
    compressor = getattr(agent, "context_compressor")

    assert compressor.context_length == 900_000
    assert compressor.threshold_percent == 0.50
    assert compressor.threshold_tokens == 450_000
    assert getattr(agent, "_compression_threshold_autoraised") is None


@pytest.mark.parametrize("model", ["gpt-5.4", "gpt-5.5", "gpt-5.6-sol"])
def test_codex_small_effective_context_autoraises_all_supported_families(model):
    compressor = _compressor(model=model, context_length=272_000)

    assert compressor.threshold_percent == 0.85
    assert compressor.threshold_tokens == 231_200
    assert compressor._threshold_autoraise_notice == {
        "model": model,
        "from": 0.50,
        "to": 0.85,
    }


@pytest.mark.parametrize("model", ["gpt-5.4", "gpt-5.5", "gpt-5.6-sol"])
def test_codex_large_effective_context_keeps_global_for_all_supported_families(model):
    compressor = _compressor(model=model, context_length=900_000)

    assert compressor.threshold_percent == 0.50
    assert compressor.threshold_tokens == 450_000
    assert compressor._threshold_autoraise_notice is None


def test_explicit_900k_variant_keeps_global_threshold():
    compressor = _compressor(model="gpt-5.6-sol-900k", context_length=900_000)

    assert compressor.threshold_percent == 0.50
    assert compressor.threshold_tokens == 450_000


@pytest.mark.parametrize("provider", ["openai", "openrouter", "copilot", "github"])
def test_non_codex_oauth_routes_do_not_receive_codex_autoraise(provider):
    compressor = _compressor(provider=provider, context_length=272_000)

    # The existing generic small-window floor remains 75%; only Codex OAuth
    # receives the family-specific 85% raise.
    assert compressor.threshold_percent == 0.75
    assert compressor._threshold_autoraise_notice is None


def test_autoraise_flag_false_disables_codex_override():
    compressor = _compressor(context_length=272_000, autoraise=False)

    assert compressor.threshold_percent == 0.75
    assert compressor._threshold_autoraise_notice is None


def test_explicit_model_threshold_precedes_codex_autoraise():
    compressor = _compressor(
        context_length=272_000,
        model_thresholds={"gpt-5.6-sol": 0.90},
    )

    assert compressor.threshold_percent == 0.90
    assert compressor.threshold_tokens == 244_800
    assert compressor._threshold_autoraise_notice is None


def test_absolute_token_threshold_still_caps_ratio_threshold():
    compressor = _compressor(
        context_length=900_000,
        threshold_tokens_cap=300_000,
    )

    assert compressor.threshold_percent == 0.50
    assert compressor.threshold_tokens == 300_000


@pytest.mark.parametrize("effective_context", [None, 0, -1, True])
def test_unknown_or_invalid_effective_context_does_not_widen_trigger(effective_context):
    assert _compression_threshold_for_model(
        "gpt-5.6-sol",
        provider="openai-codex",
        effective_context_length=effective_context,
    ) is None


def test_model_provider_and_context_switches_recompute_from_global_base():
    compressor = _compressor(context_length=900_000)
    assert compressor.threshold_percent == 0.50

    compressor.update_model(
        model="gpt-5.6-sol",
        provider="openai-codex",
        context_length=272_000,
    )
    assert compressor.threshold_percent == 0.85

    compressor.update_model(
        model="gpt-5.6-sol",
        provider="openai-codex",
        context_length=900_000,
    )
    assert compressor.threshold_percent == 0.50

    compressor.update_model(
        model="gpt-5.6-sol",
        provider="openrouter",
        context_length=900_000,
    )
    assert compressor.threshold_percent == 0.50
    assert compressor.threshold_tokens == 450_000


def test_900k_preflight_becomes_eligible_at_configured_threshold_not_765k():
    compressor = _compressor(context_length=900_000)

    assert compressor.should_compress(prompt_tokens=449_999) is False
    assert compressor.should_compress(prompt_tokens=450_001) is True
    assert compressor.threshold_tokens != 765_000
