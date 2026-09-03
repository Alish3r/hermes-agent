"""Policy tests for explicit small-context local custom providers."""

from types import SimpleNamespace

from agent.agent_init import _allows_explicit_small_local_context
from agent.conversation_loop import _ollama_context_limit_error


def _agent(*, context_length, base_url, providers):
    return SimpleNamespace(
        _config_context_length=64_000,
        context_compressor=SimpleNamespace(context_length=context_length),
        base_url=base_url,
        _custom_providers=providers,
    )


def test_explicit_opted_in_loopback_provider_allows_small_context():
    agent = _agent(
        context_length=16_384,
        base_url="http://127.0.0.1:11435/v1",
        providers=[
            {
                "base_url": "http://127.0.0.1:11435/v1",
                "allow_below_minimum_context": True,
            }
        ],
    )

    assert _allows_explicit_small_local_context(agent) is True


def test_opted_in_small_context_route_allows_only_reserved_request_budget():
    agent = _agent(
        context_length=16_384,
        base_url="http://127.0.0.1:11435/v1",
        providers=[
            {
                "base_url": "http://127.0.0.1:11435/v1",
                "allow_below_minimum_context": True,
            }
        ],
    )
    agent.tools = [object()]
    agent._ollama_num_ctx = 16_384
    agent.model = "qwen2.5-1.5b-16k"
    agent.provider = "custom"

    assert _ollama_context_limit_error(agent, 12_288) is None
    assert _ollama_context_limit_error(agent, 12_289) is not None


def test_small_context_requires_matching_explicit_opt_in():
    agent = _agent(
        context_length=16_384,
        base_url="http://127.0.0.1:11435/v1",
        providers=[
            {
                "base_url": "http://127.0.0.1:11435/v1",
                "allow_below_minimum_context": False,
            }
        ],
    )

    assert _allows_explicit_small_local_context(agent) is False


def test_small_context_is_never_allowed_for_a_public_endpoint():
    agent = _agent(
        context_length=16_384,
        base_url="https://example.com/v1",
        providers=[
            {
                "base_url": "https://example.com/v1",
                "allow_below_minimum_context": True,
            }
        ],
    )

    assert _allows_explicit_small_local_context(agent) is False


def test_normal_context_does_not_need_the_small_context_exception():
    agent = _agent(
        context_length=64_000,
        base_url="http://127.0.0.1:11435/v1",
        providers=[
            {
                "base_url": "http://127.0.0.1:11435/v1",
                "allow_below_minimum_context": True,
            }
        ],
    )

    assert _allows_explicit_small_local_context(agent) is False


def test_keyed_provider_config_preserves_explicit_small_context_opt_in():
    from hermes_cli.config import providers_dict_to_custom_providers

    providers = providers_dict_to_custom_providers(
        {
            "surface": {
                "name": "Surface",
                "api": "http://127.0.0.1:11435/v1",
                "context_length": 16_384,
                "allow_below_minimum_context": True,
            }
        }
    )

    assert providers[0]["allow_below_minimum_context"] is True


def test_reads_route_catalog_when_agent_does_not_retain_it(monkeypatch):
    from hermes_cli import config

    route = {
        "base_url": "http://127.0.0.1:11435/v1",
        "allow_below_minimum_context": True,
    }
    monkeypatch.setattr(config, "load_config_readonly", lambda: {})
    monkeypatch.setattr(config, "get_compatible_custom_providers", lambda _: [route])

    agent = _agent(
        context_length=16_384,
        base_url="http://127.0.0.1:11435/v1",
        providers=[],
    )

    assert _allows_explicit_small_local_context(agent) is True
