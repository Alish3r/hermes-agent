"""Regression coverage for literal one-shot query labels."""

from io import StringIO
from types import SimpleNamespace

from rich.console import Console

import cli as cli_mod


def test_human_single_query_label_treats_rich_markup_as_literal(monkeypatch):
    """A bracketed URL in ``-q`` must not be parsed as Rich markup."""
    output = StringIO()
    calls = []

    class FakeCLI:
        def __init__(self, **_kwargs):
            self.console = Console(file=output, force_terminal=False)
            self.session_id = "single-query-session"
            self.agent = SimpleNamespace(
                session_id="single-query-session",
                platform="cli",
            )

        def _claim_active_session(self, surface, *, stderr=False):
            return True

        def _show_security_advisories(self):
            pass

        def chat(self, query, images=None):
            calls.append((query, images))
            return "done"

        def _print_exit_summary(self, clear_screen=True):
            pass

    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _cli: None)
    monkeypatch.setattr(
        cli_mod,
        "_drain_oneshot_async_delegations",
        lambda *_args, **_kwargs: None,
    )

    query = "inspect [/fb-images/a/../b.webp?w=336]"
    cli_mod.main(query=query, quiet=False, toolsets="terminal")

    assert query in output.getvalue()
    assert calls == [(query, None)]
