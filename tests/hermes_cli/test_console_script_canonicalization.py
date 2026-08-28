"""Regression tests for canonical public console-script dispatch."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_hermes_agent_console_script_uses_canonical_dispatcher() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    scripts = metadata["project"]["scripts"]
    assert scripts["hermes-agent"] == scripts["hermes"] == "hermes_cli.main:main"


def test_canonical_help_does_not_construct_agent(monkeypatch, capsys) -> None:
    import hermes_cli.main as cli_main

    monkeypatch.setattr(sys, "argv", ["hermes-agent", "--help"])
    monkeypatch.setattr(
        "run_agent.AIAgent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("--help constructed AIAgent")
        ),
    )

    try:
        cli_main.main()
    except SystemExit as exc:
        assert exc.code == 0

    output = capsys.readouterr().out
    assert "Hermes" in output
