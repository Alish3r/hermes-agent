"""Phase 0 shadow orchestration profile contracts and isolation tests."""

from __future__ import annotations

from pathlib import Path
import os
import threading

import pytest
import yaml

from hermes_cli import epic_profiles
from hermes_cli.profiles import list_profiles
from hermes_cli.tools_config import _get_platform_tools
from hermes_cli.tools_config import set_exact_profile_toolset_pin
from model_tools import get_tool_definitions
import model_tools
from toolsets import resolve_multiple_toolsets


EXPECTED_NAMES = {
    "orchestrator",
    "advisory",
    "implementer",
    "integration-writer",
    "verifier",
    "release-operator",
}
MUTATION_TOOLS = {
    "write_file",
    "patch",
    "terminal",
    "process",
    "execute_code",
    "skill_manage",
    "memory",
    "delegate_task",
    "cronjob",
    "computer_use",
    "browser_click",
    "browser_type",
    "kanban_complete",
    "kanban_request_changes",
}


def test_profile_capability_pin_is_exact_and_invalid_pin_fails_closed(caplog):
    config = {
        "tools": {"enabled_toolsets": ["artifact_read"]},
        # Deliberately broad legacy/platform config: the exact profile pin wins.
        "platform_toolsets": {"cli": ["hermes-cli"]},
    }
    assert _get_platform_tools(config, "cli") == {"artifact_read"}

    invalid = {"tools": {"enabled_toolsets": ["artifact_read", "not-a-toolset"]}}
    assert _get_platform_tools(invalid, "cli") == set()
    assert "not-a-toolset" in caplog.text


def test_create_and_read_six_profiles_in_isolated_home(tmp_path, monkeypatch):
    shadow_home = tmp_path / ".hermes"
    manifest = epic_profiles.create_shadow_profiles(shadow_home)
    policies = epic_profiles.read_shadow_profiles(shadow_home)

    assert manifest["schema"] == epic_profiles.SHADOW_MANIFEST_SCHEMA
    assert set(manifest["profiles"]) == EXPECTED_NAMES
    assert set(policies) == EXPECTED_NAMES
    assert "observer" not in policies

    # Prove compatibility with the current profile enumerator using only the
    # disposable home. No live ~/.hermes path is involved.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(shadow_home))
    enumerated = {item.name for item in list_profiles() if not item.is_default}
    assert enumerated == EXPECTED_NAMES

    for name, policy in policies.items():
        config = yaml.safe_load(
            (shadow_home / "profiles" / name / "config.yaml").read_text()
        )
        assert config["tools"]["enabled_toolsets"] == policy["toolsets"]
        assert config["platform_toolsets"]["cli"] == policy["toolsets"]
        assert _get_platform_tools(config, "cli") == set(policy["toolsets"])


def test_profile_contracts_are_least_privilege_and_domain_explicit():
    specs = epic_profiles.SHADOW_PROFILE_SPECS
    assert set(specs) == EXPECTED_NAMES
    assert "observer" not in specs

    for name, spec in specs.items():
        policy = spec.policy()
        assert policy["profile"] == name
        assert policy["shadow_only"] is True
        assert policy["os_sandbox"] is False
        assert policy["production_authority"] is False
        assert policy["credential_domains"]
        assert policy["network_domains"]
        assert policy["denials"]
        assert "artifact_read" in policy["toolsets"]

    assert specs["orchestrator"].toolsets == (
        "artifact_read",
        "session_search",
        "todo",
    )
    assert specs["verifier"].toolsets == ("artifact_read",)
    assert specs["release-operator"].toolsets == ("artifact_read",)
    assert specs["implementer"].toolsets == (
        "artifact_read",
        "file",
        "kanban",
    )
    assert specs["integration-writer"].toolsets == (
        "artifact_read",
        "file",
    )
    assert specs["implementer"].write_domains != specs["integration-writer"].write_domains

    for name in ("orchestrator", "advisory", "verifier", "release-operator"):
        resolved = set(resolve_multiple_toolsets(specs[name].toolsets))
        assert MUTATION_TOOLS.isdisjoint(resolved), (name, MUTATION_TOOLS & resolved)

    implementer = set(resolve_multiple_toolsets(specs["implementer"].toolsets))
    integration = set(resolve_multiple_toolsets(specs["integration-writer"].toolsets))
    assert {"write_file", "patch"} <= implementer
    assert {"write_file", "patch"} <= integration
    # No writable role holds a shell until the sandbox/write-reservation
    # service exists — see test_no_profile_denies_an_authority_its_own_grant.
    assert {"terminal", "process"}.isdisjoint(implementer)
    assert {"terminal", "process"}.isdisjoint(integration)
    # Ticket lifecycle is the implementer's; integration-writer denies
    # "ticket implementation" and so must not carry the kanban surface.
    assert "kanban_complete" in implementer
    assert "kanban_complete" not in integration
    assert {"computer_use", "browser_click"}.isdisjoint(implementer)
    assert {"computer_use", "browser_click"}.isdisjoint(integration)


def test_read_only_profile_model_schemas_have_no_mutation_tools():
    for name in ("orchestrator", "verifier", "release-operator"):
        schemas = get_tool_definitions(
            enabled_toolsets=list(epic_profiles.SHADOW_PROFILE_SPECS[name].toolsets),
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
        names = {schema["function"]["name"] for schema in schemas}
        assert {"read_file", "search_files"} <= names
        assert MUTATION_TOOLS.isdisjoint(names), (name, MUTATION_TOOLS & names)


def test_dispatcher_does_not_augment_exact_read_only_profile_with_kanban(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-read-only")
    monkeypatch.setattr(model_tools, "_is_delegated_child_context", lambda: False)
    monkeypatch.setattr(model_tools, "_is_dispatcher_owned_worker", lambda: True)
    import hermes_cli.config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"tools": {"enabled_toolsets": ["artifact_read"]}},
    )
    model_tools._clear_tool_defs_cache()
    names = {
        schema["function"]["name"]
        for schema in get_tool_definitions(
            enabled_toolsets=["artifact_read"],
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
    }
    assert {"read_file", "search_files"} <= names
    assert not any(name.startswith("kanban_") for name in names)


def test_exact_pin_includes_only_explicit_profile_mcp_when_requested(monkeypatch):
    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(
        tools_config,
        "enabled_mcp_server_names",
        lambda _config: {"mcp-explicit"},
    )
    config = {"tools": {"enabled_toolsets": ["artifact_read"]}}
    assert _get_platform_tools(
        config, "cli", include_default_mcp_servers=False
    ) == {"artifact_read"}
    assert _get_platform_tools(
        config, "cli", include_default_mcp_servers=True
    ) == {"artifact_read", "mcp-explicit"}


def test_exact_empty_pin_is_persisted_and_unpin_is_explicit():
    config = {}
    set_exact_profile_toolset_pin(config, [])
    assert config["tools"]["enabled_toolsets"] == []
    set_exact_profile_toolset_pin(config, None)
    assert "enabled_toolsets" not in config["tools"]


def test_existing_or_stale_shadow_target_fails_closed(tmp_path):
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "profiles").mkdir()
    with pytest.raises(epic_profiles.ShadowProfileConflict, match="already exists"):
        epic_profiles.create_shadow_profiles(occupied)

    target = tmp_path / "fresh"
    epic_profiles.create_shadow_profiles(target)
    with pytest.raises(epic_profiles.ShadowProfileConflict, match="already exists"):
        epic_profiles.create_shadow_profiles(target)


def test_tampered_or_conflicting_definition_fails_readback(tmp_path):
    target = tmp_path / "shadow"
    epic_profiles.create_shadow_profiles(target)
    policy_path = target / "profiles" / "verifier" / "orchestration-policy.yaml"
    policy = yaml.safe_load(policy_path.read_text())
    policy["production_authority"] = True
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False))

    with pytest.raises(epic_profiles.ShadowProfileValidationError):
        epic_profiles.read_shadow_profiles(target)


@pytest.mark.parametrize("mutation", ["credential", "skill", "symlink", "extra", "mode"])
def test_complete_shadow_tree_tampering_fails_readback(tmp_path, mutation):
    target = tmp_path / mutation
    epic_profiles.create_shadow_profiles(target)
    profile = target / "profiles" / "verifier"
    if mutation == "credential":
        (profile / ".env").write_text("FAKE_TOKEN=not-a-real-secret\n")
    elif mutation == "skill":
        skill = profile / "skills" / "injected"
        skill.mkdir()
        (skill / "SKILL.md").write_text("injected")
    elif mutation == "symlink":
        (profile / "skills").rmdir()
        (profile / "skills").symlink_to(tmp_path)
    elif mutation == "extra":
        (profile / "unexpected.txt").write_text("unexpected")
    elif mutation == "mode":
        os.chmod(profile / "config.yaml", 0o644)

    with pytest.raises(epic_profiles.ShadowProfileValidationError):
        epic_profiles.read_shadow_profiles(target)


def test_shadow_creation_reserves_target_without_concurrent_clobber(tmp_path):
    target = tmp_path / "shared"
    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def create():
        barrier.wait()
        try:
            epic_profiles.create_shadow_profiles(target)
            outcome = "created"
        except epic_profiles.ShadowProfileConflict:
            outcome = "conflict"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["conflict", "created"]
    assert set(epic_profiles.read_shadow_profiles(target)) == EXPECTED_NAMES


def test_existing_target_symlink_is_never_replaced(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "shadow-link"
    target.symlink_to(outside)
    with pytest.raises(epic_profiles.ShadowProfileConflict):
        epic_profiles.create_shadow_profiles(target)
    assert target.is_symlink()
    assert list(outside.iterdir()) == []


def test_live_or_overlapping_home_is_refused(tmp_path, monkeypatch):
    live_home = tmp_path / ".hermes"
    live_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(live_home))

    with pytest.raises(epic_profiles.ShadowProfileConflict, match="live Hermes home"):
        epic_profiles.create_shadow_profiles(live_home)
    with pytest.raises(epic_profiles.ShadowProfileConflict, match="live Hermes home"):
        epic_profiles.create_shadow_profiles(live_home / "nested")


SHELL_TOOLS = {"terminal", "process", "execute_code"}


def test_writer_profiles_grant_no_shell_and_are_enforceably_distinct():
    """The two writable roles must differ where it is enforced, and neither may
    hold unrestricted shell.

    ``terminal`` resolves to ``terminal``+``process`` — a shell that reaches
    ``git merge``, ``git push`` and any deploy script. Granting it while the
    same spec publishes ``merge``/``deploy`` denials makes the policy artifact
    assert a denial its own grant defeats. Phase 0 follows the convention the
    ``verifier`` spec already uses: withhold the capability and name the future
    service instead of over-granting now.
    """
    specs = epic_profiles.SHADOW_PROFILE_SPECS

    for name in ("implementer", "integration-writer"):
        resolved = set(resolve_multiple_toolsets(specs[name].toolsets))
        assert SHELL_TOOLS.isdisjoint(resolved), (
            f"{name} holds shell/exec tools "
            f"{sorted(SHELL_TOOLS & resolved)} while denying "
            f"{sorted(specs[name].denials)}"
        )

    assert specs["implementer"].toolsets != specs["integration-writer"].toolsets, (
        "implementer and integration-writer are identical at the enforceable "
        "layer; write_domains strings are declarative and enforce nothing"
    )


# Denials that name an authority some tool actually confers. A profile
# publishing one of these must not resolve to the mapped tools.
TOOL_CONFERRED_DENIALS = {
    "merge": SHELL_TOOLS,
    "deploy": SHELL_TOOLS,
    "commit": SHELL_TOOLS,
    "push": SHELL_TOOLS,
    "review verdict": {"kanban_request_changes"},
    "verdict mutation": {"kanban_request_changes"},
    "independent review": {"kanban_request_changes"},
    "lifecycle authority": {"kanban_create", "kanban_link", "kanban_unblock"},
}

# Denials that constrain *scope* (which path, which ticket, whose budget), which
# no Phase 0 toolset can express. These are declarative: the policy artifact
# says so, ``os_sandbox`` is False, and nothing here is presented as a control.
DECLARATIVE_DENIALS = {
    "source writes",
    "source implementation",
    "integration writes",
    "unrelated worktree writes",
    "candidate edits",
    "ticket implementation",
    "release approval",
    "exception acceptance",
    "self budget or lease extension",
    "deploy without human capability",
}


def test_no_profile_denies_an_authority_its_own_toolsets_grant():
    """Cross-profile honesty invariant: a published denial must not be
    defeated by the same profile's toolset grant."""
    for name, spec in epic_profiles.SHADOW_PROFILE_SPECS.items():
        resolved = set(resolve_multiple_toolsets(spec.toolsets))
        for denial in spec.denials:
            forbidden = TOOL_CONFERRED_DENIALS.get(denial.lower())
            if forbidden is None:
                continue
            assert forbidden.isdisjoint(resolved), (
                f"{name} denies {denial!r} but its toolsets "
                f"{list(spec.toolsets)} resolve to "
                f"{sorted(forbidden & resolved)}"
            )


def test_every_published_denial_is_classified():
    """No denial may sit outside both tables.

    Without this, adding a denial phrase nobody mapped would silently skip the
    honesty invariant above — which is exactly how the ``review verdict``
    contradiction survived the first review.
    """
    known = set(TOOL_CONFERRED_DENIALS) | DECLARATIVE_DENIALS
    published = {
        denial.lower()
        for spec in epic_profiles.SHADOW_PROFILE_SPECS.values()
        for denial in spec.denials
    }
    assert published <= known, (
        "unclassified denial(s) — add to TOOL_CONFERRED_DENIALS (if a tool "
        f"grants it) or DECLARATIVE_DENIALS: {sorted(published - known)}"
    )
