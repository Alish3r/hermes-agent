from __future__ import annotations

import subprocess


def _make_task(kb, *, assignee: str):
    return kb.Task(
        id="t_spawn_tools",
        title="spawn tools",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )


def test_default_spawn_pins_assignee_profile_cli_toolsets(monkeypatch, tmp_path):
    """Manual profile assignment should keep that profile's CLI tools.

    Regression guard for dispatcher-spawned workers that boot with
    HERMES_KANBAN_TASK: the worker must not collapse to only kanban lifecycle
    tools when the assigned profile's top-level ``toolsets`` is the default
    composite. The spawned CLI gets an explicit --toolsets pin resolved from
    platform_toolsets.cli; model_tools appends task-scoped kanban tools later.
    """
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - clarify
    - code_execution
    - delegation
    - file
    - memory
    - session_search
    - skills
    - terminal
    - web
toolsets:
  - hermes-cli
agent:
  disabled_toolsets: []
""".lstrip(),
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pid = kb._default_spawn(_make_task(kb, assignee="elias"), str(workspace))

    assert pid == 4242
    assert captured["env"]["HERMES_HOME"] == str(profile)
    assert captured["env"]["HERMES_KANBAN_TASK"] == "t_spawn_tools"
    assert "--toolsets" in captured["cmd"]
    pinned = captured["cmd"][captured["cmd"].index("--toolsets") + 1].split(",")
    for required in ("terminal", "web", "file", "skills", "code_execution", "delegation"):
        assert required in pinned


def _spawn_capturing(monkeypatch, tmp_path, profile_cfg: str):
    """Build a worker argv for a profile and return the captured command."""
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(profile_cfg, encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4243

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kb._default_spawn(_make_task(kb, assignee="elias"), str(workspace))
    return captured["cmd"]


def test_default_spawn_sends_no_tools_for_an_exact_empty_pin(monkeypatch, tmp_path):
    """An empty exact pin is a deliberate lockdown and must cross the process
    boundary as its own signal.

    ``--toolsets ""`` cannot carry it: the receiving parser treats an empty
    value as absent and the child silently re-resolves from its own config.
    The dispatcher therefore sends ``--no-tools``, which is additive and
    cannot be mistaken for a toolset name by any older child.
    """
    cmd = _spawn_capturing(
        monkeypatch,
        tmp_path,
        "tools:\n  enabled_toolsets: []\n",
    )

    assert "--no-tools" in cmd
    assert "--toolsets" not in cmd, (
        "an empty pin must not be sent as --toolsets; an empty value reads as absent"
    )


def test_default_spawn_still_pins_a_non_empty_exact_pin(monkeypatch, tmp_path):
    """The empty-pin signal must not disturb the ordinary pinned case."""
    cmd = _spawn_capturing(
        monkeypatch,
        tmp_path,
        "tools:\n  enabled_toolsets:\n    - file\n    - terminal\n",
    )

    assert "--no-tools" not in cmd
    assert "--toolsets" in cmd
    pinned = cmd[cmd.index("--toolsets") + 1].split(",")
    assert "file" in pinned and "terminal" in pinned


def test_worker_argv_for_an_empty_pin_parses_to_zero_toolsets(monkeypatch, tmp_path):
    """Integration contract: the real parser must accept the argv the
    dispatcher builds, and the child must see an explicit zero -- not a
    missing flag it would resolve around."""
    from hermes_cli._parser import build_top_level_parser

    cmd = _spawn_capturing(
        monkeypatch,
        tmp_path,
        "tools:\n  enabled_toolsets: []\n",
    )

    parser, _subparsers, _chat_parser = build_top_level_parser()
    # Profile selection is attached by the outer CLI bootstrap, as in the
    # model-override contract test below; strip that prefix.
    assert cmd[1:3] == ["-p", "elias"]
    args = parser.parse_args(cmd[3:])

    assert getattr(args, "no_tools", False) is True
    assert not getattr(args, "toolsets", None)


def test_no_tools_and_toolsets_are_mutually_exclusive():
    """Sending both is a caller bug and must fail loudly, not resolve by
    precedence -- a silent winner is how a lockdown turns into a grant."""
    import pytest

    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat_parser = build_top_level_parser()

    # Guard against passing for the wrong reason: an unknown --no-tools would
    # also raise SystemExit, so first pin that the flag is recognised alone.
    solo = parser.parse_args(["chat", "--no-tools", "-q", "hi"])
    assert getattr(solo, "no_tools", False) is True

    with pytest.raises(SystemExit):
        parser.parse_args(["chat", "--toolsets", "file", "--no-tools", "-q", "hi"])


def test_no_tools_normalises_to_an_explicit_empty_pin():
    """The child must convert the flag into an explicit zero, not leave it None.

    ``None`` means "no pin -- resolve from config", which is exactly the
    widening this flag exists to prevent.
    """
    from types import SimpleNamespace

    from hermes_cli.main import _apply_no_tools

    args = SimpleNamespace(no_tools=True, toolsets=None)
    _apply_no_tools(args)
    assert args.toolsets == []

    untouched = SimpleNamespace(no_tools=False, toolsets="file,terminal")
    _apply_no_tools(untouched)
    assert untouched.toolsets == "file,terminal"

    absent = SimpleNamespace(toolsets=None)
    _apply_no_tools(absent)
    assert absent.toolsets is None


def test_no_tools_with_tui_fails_loudly_instead_of_being_ignored():
    """The TUI resolves its own toolsets from an env var it only sets for a
    non-empty list, so an empty pin would be silently dropped and the session
    would come up with a full tool surface. Refuse the combination rather than
    grant more than was asked for.
    """
    import pytest
    from types import SimpleNamespace

    from hermes_cli.main import _reject_no_tools_with_tui

    with pytest.raises(SystemExit):
        _reject_no_tools_with_tui(SimpleNamespace(no_tools=True), use_tui=True)

    # Allowed combinations must stay silent.
    _reject_no_tools_with_tui(SimpleNamespace(no_tools=True), use_tui=False)
    _reject_no_tools_with_tui(SimpleNamespace(no_tools=False), use_tui=True)
    _reject_no_tools_with_tui(SimpleNamespace(), use_tui=True)


def test_cli_treats_an_empty_toolset_list_as_zero_not_unset():
    """cli.main's resolver must not send an explicit empty pin down the
    'no --toolsets given' path, which collapses to the coding posture or a
    config-resolved surface."""
    import cli

    assert cli._is_explicit_zero_toolsets([]) is True
    assert cli._is_explicit_zero_toolsets(()) is True
    assert cli._is_explicit_zero_toolsets(None) is False
    assert cli._is_explicit_zero_toolsets("file") is False
    assert cli._is_explicit_zero_toolsets(["file"]) is False


def test_default_spawn_model_override_survives_real_cli_parse(monkeypatch, tmp_path):
    """The dispatcher's pre-``chat`` model flag must reach ``args.model``.

    This is an integration contract between Kanban's worker argv builder and
    the real CLI parser. A parser default once erased the explicit override,
    silently sending the worker to its profile default or fallback instead.
    """
    root = tmp_path / ".hermes"
    (root / "profiles" / "elias").mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb
    from hermes_cli._parser import build_top_level_parser

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4244

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = _make_task(kb, assignee="elias")
    task.model_override = "gpt-5.6-sol"
    kb._default_spawn(task, str(workspace))

    parser, _subparsers, _chat_parser = build_top_level_parser()
    # Profile selection is attached by the outer CLI bootstrap rather than
    # build_top_level_parser(); remove that already-validated prefix and parse
    # the worker flags/subcommand through the real shared parser.
    assert captured["cmd"][1:3] == ["-p", "elias"]
    args = parser.parse_args(captured["cmd"][3:])

    assert args.command == "chat"
    assert args.model == "gpt-5.6-sol"
    assert args.query == "work kanban task t_spawn_tools"


def test_resolve_worker_cli_toolsets_uses_profile_home_not_parent_config(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("platform_toolsets:\n  cli:\n    - kanban\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    resolved = kb._resolve_worker_cli_toolsets(str(profile))

    assert resolved is not None
    assert "terminal" in resolved
    assert "web" in resolved
    assert "kanban" in resolved  # recovered worker lifecycle surface
    assert resolved != ["kanban"]


def test_resolve_worker_cli_toolsets_preserves_exact_empty_pin(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "read-only"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("platform_toolsets:\n  cli:\n    - terminal\n")
    profile.joinpath("config.yaml").write_text(
        "tools:\n  enabled_toolsets: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    assert kb._resolve_worker_cli_toolsets(str(profile)) == []
