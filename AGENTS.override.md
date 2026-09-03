# Hermes Agent - Development Guide (condensed local override)

This file replaces the 96 KB `AGENTS.md` for sessions on this machine (Hermes and Codex both load `AGENTS.override.md` first). It keeps every universal rule and points to the full text for the rest. **The full `AGENTS.md` is still on disk: when a task touches a subsystem listed in the routing table, read that section before editing.** Never give up on the right solution.

## What Hermes is

A personal AI agent with one agent core behind a CLI, a messaging gateway (Telegram, Discord, Slack, ~20 platforms), an Ink TUI, and an Electron desktop app. It learns across sessions (memory + skills), delegates to subagents, runs cron jobs, drives a terminal and a browser. It is extended through **plugins and skills**, not by growing the core.

Two invariants shape every review:

- **Per-conversation prompt caching is sacred.** Never mutate past context, swap toolsets, reload memories, or rebuild the system prompt mid-conversation. The one exception is context compression. The system prompt must be byte-stable for the life of a conversation. Slash commands that change prompt state default to deferred invalidation with an opt-in `--now` (see `/skills install --now`).
- **Narrow waist, capability at the edges.** Every core model tool ships on every API call. Footprint ladder, pick the highest rung that works: 1 extend existing code → 2 CLI command + skill → 3 service-gated tool (`check_fn`) → 4 plugin (`~/.hermes/plugins/` or pip) → 5 MCP server in the catalog → 6 new core tool (last resort: terminal, read_file, web_search, browser_navigate are the bar). When 3+ PRs integrate the same category, build one ABC + orchestrator and turn them into plugins.

Strict message-role alternation: never two same-role messages in a row, never a synthetic user message injected mid-loop.

## Contribution rubric (short form; full text: `AGENTS.md` → "Contribution Rubric")

Want: real bug fixes that reproduce on `main`, point to the exact line, and fix the whole class incl. sibling call paths · new platforms/providers/models/desktop features (breadth at the edges is a goal) as long as they use `hermes tools` / `hermes setup` / auto-install rather than raw env vars · extraction of god-file clusters (`cli.py`, `run_agent.py`, `gateway/run.py`) into modules · extend before duplicate · behavior-contract tests · E2E against a temp `HERMES_HOME` with real imports for resolution chains, config propagation, security boundaries, remote backends, file/network I/O · cherry-pick external work to preserve authorship.

Reject even when well built: speculative hooks with no consumer · new `HERMES_*` env vars for non-secret config (`.env` is secrets only; behavior goes in `config.yaml`) · a core tool where terminal + file or a skill would do · `offset`/`limit` pagination on instructional content · "fixes" that kill the feature they secure (read `git log -p -S` first) · outbound telemetry without an opt-in gate · change-detector tests · dead code wired in without E2E proof · plugins that touch core files · third-party products or someone else's project under `plugins/` (ship as a standalone plugin repo; `plugins/memory/` is closed to new in-tree providers).

Before calling something a bug, verify the premise against the code: intentional isolation is not a gap (profiles are islands by design), a fix must change the line where the symptom manifests, and a "missing" piece may be load-bearing (restoring `__init__.py` files once shadowed a real plugin). When in doubt about intent, ask.

Surface capability is a property of the **session**, never of the process env: GUI-only tools live in a named toolset (`desktop_ui`, `project`) folded in by `_load_enabled_toolsets(platform)`; `check_fn` answers reachability or opt-in, not surface, and is TTL-cached process-wide. `HERMES_DESKTOP=1` means "spawned by the app", not "a GUI is watching".

## Development environment and testing

```bash
source .venv/bin/activate      # or venv/; scripts/run_tests.sh probes .venv, venv, ~/.hermes/hermes-agent/venv
scripts/run_tests.sh                                   # full suite, CI parity (never call pytest directly)
scripts/run_tests.sh tests/agent/test_foo.py -k test_x # file + -k; the runner is file-granular
```

- `run_tests.sh` unsets credential vars, sets TZ=UTC and LANG=C.UTF-8, and runs each test file in its own subprocess. A `⚠ FLAKY` pass-on-retry is a bug to fix (loose wall-clock bounds ≥ 2s, event-based sync, no negative-timing races).
- Tests must not write to `~/.hermes/`; the autouse `_isolate_hermes_home` fixture redirects `HERMES_HOME`. Profile tests also monkeypatch `Path.home()` (pattern in `tests/hermes_cli/test_profiles.py`).
- Tests that assert about `package.json`, lockfiles, `tsconfig.json` or `.ts/.tsx/.js/.mjs/.cjs` belong in the vitest suite, not `tests/*.py` (the CI change classifier will not run Python tests for JS-only PRs).
- Don't fake the host OS: use `@pytest.mark.linux_only` / `macos_only` / `windows_only` (never a bare `skipif`, never a file-local alias; split multi-OS test bodies). Pure functions that take the platform as data may stay unmarked. Live Windows process-topology proof: push to a `wine2e/**` branch.
- No change-detector tests (catalog snapshots, config version literals, enumeration counts). Assert relationships and invariants instead.
- Never read source-file text in a test. Extract the logic into a pure function and call it.

## Universal code rules

- Paths: `get_hermes_home()` for state, `display_hermes_home()` for user-facing text (both from `hermes_constants`). Never `~/.hermes` or `Path.home() / ".hermes"`; profiles set `HERMES_HOME` before imports, so module-level constants are fine. Exception: `_get_profiles_root()` is HOME-anchored on purpose.
- Multiplex profile env reads fail closed: under `gateway.multiplex_profiles` never fall through to `os.environ` after a scoped miss (`_get_scoped_secret()` canonical copy in `plugins/platforms/feishu/adapter.py`; gateway authz via `_auth_env()` / `_platform_gate_env()`). Credentials and allowlists alike.
- Gateway adapters with a unique credential take `acquire_scoped_lock()` / `release_scoped_lock()` from `gateway.status`.
- Dependencies need ceilings: PyPI `>=floor,<next_major` (pre-1.0: `<0.(minor+2)`), git URLs pinned to a 40-char SHA, GitHub Actions pinned to SHA + version comment, CI-only pip `==exact`; run `uv lock`. Bare `>=X` is rejected.
- Never infer process identity from argv substrings; use `gateway.status.looks_like_gateway_command_line` and `hermes_cli.update_cmd._hermes_holder_subcommand`, derive flag sets from the parser, match full cmdlines, don't blanket-exclude ancestors. New scan heuristics: read #92091 first (control socket replaces scans).
- Tool schema descriptions must not name tools from other toolsets; add cross-references dynamically in `get_tool_definitions()`. Schema path references use `display_hermes_home()`. All handlers return a JSON string.
- Interactive CLI menus use `hermes_cli/curses_ui.py`. No `\033[K` in spinner/display code (space-pad instead).
- `_last_resolved_tool_names` in `model_tools.py` is process-global and may be stale during subagent runs.
- Before squash-merging, rebase the branch on `origin/main`; verify with `git diff HEAD~1..HEAD` for unexpected deletions.
- Adding a core tool: `tools/your_tool.py` with `registry.register(...)` **and** an entry in a toolset in `toolsets.py` (auto-discovery imports it, but only a toolset exposes it). Prefer the plugin route (`~/.hermes/plugins/<name>/plugin.yaml` + `ctx.register_tool`).
- Adding config: `DEFAULT_CONFIG` in `hermes_cli/config.py`; bump `_config_version` only for migrations. Three loaders exist: `load_cli_config()` (CLI), `load_config()` (subcommands), raw YAML in `gateway/run.py` + `gateway/config.py`; if one surface sees a key and another does not, you are on the wrong loader. Secrets go in `OPTIONAL_ENV_VARS`.
- Adding a slash command: `CommandDef` in `hermes_cli/commands.py` `COMMAND_REGISTRY`, handler in `HermesCLI.process_command()`, optional gateway handler in `gateway/run.py`; aliases need only the registry entry.
- Plugins: `register(ctx)` with lifecycle hooks (`pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_llm_call`, `on_session_start`, `on_session_end`), `ctx.register_tool`, `ctx.register_cli_command`; `discover_plugins()` only runs when `model_tools.py` is imported. Keep documented plugin surfaces additive (contract in `website/docs/developer-guide/plugins/index.md`).
- Background `delegate_task` is process-local; for work that must survive a restart use `cronjob` or `terminal(background=True, notify_on_complete=True)`.

## Gateway, streaming, update and desktop invariants (one line each; details in `AGENTS.md` → "Known Pitfalls", "Update Pipeline", "Bot Mode")

- Two message guards (base adapter `_pending_messages`, runner intercepts of `/stop /new /queue /status /approve /deny`): any command that must reach a blocked agent bypasses both inline.
- Stream-is-the-message adapters (`draft_stream_is_message is True`): draft frames prefix-stable, consumer declares the final via `finish(final_text)`, interim sends carry `metadata["_interim_send"]=True`, reconcile by `edit_message` before plain send. Tests: `tests/gateway/test_stream_final_contract.py`.
- Cron deliveries land in their own session with a header/footer frame, never mirrored into the gateway session; cron sessions run with `skip_memory=True` and a 3-minute hard interrupt.
- `hermes update` = plan → snapshot → apply → restart-per-kind → verify → report. Full-profile snapshots only, fleet-wide drain-first restarts, receipts in `~/.hermes/logs/update_receipts/` even on refused runs, mixed-version fleet = exit 1. Windows ZIP fallback fires only when git itself failed.
- `hermes serve` dies with the desktop app; the messaging gateway is spawned detached and survives it. Do not re-parent the gateway or widen the tree-kill.
- Desktop Bot Mode: one bot = one canonical chat identified by (profile, title "Bot Chat"). No session-id pointer, no recency, no per-bot session browser. Reject PRs that reintroduce any of them.
- Desktop slash palette: curation hides terminal-only/messaging-only built-ins, never skill or `quick_commands` extensions (`isDesktopSlashExtensionCommand` must flow into both completion paths). Do not rebuild the chat transcript/composer in React for the dashboard; extend Ink.
- Skill authoring standards are HARDLINE: `description` ≤ 60 chars, name native Hermes tools not shell utilities, `platforms:` audited against imports, human contributor credited first, modern section order, scripts under `scripts/`, tests at `tests/skills/test_<skill>_skill.py`. Full checklist: the `hermes-agent-skill-authoring` skill.

## Local checkout conventions (this machine)

- This checkout follows `origin/main` (NousResearch) from the branch `local/alisher`. Commit local adaptations on `local/alisher`, never on `main`: `hermes update` hard-resets a diverged `main` but merges `origin/main` into a custom branch. Do not open PRs against NousResearch from here.
- Keep this file under 20,000 chars (the Hermes floor cap; `hermes prompt-size` does not warn when it is exceeded) and any per-directory `AGENTS.md` under 8,000 chars (the lazy-hint cap; `apps/desktop/AGENTS.md` already exceeds it and is cut when loaded). Check with `wc -m AGENTS.override.md`.
- Hermes and Codex load this file instead of `AGENTS.md`; Claude Code loads it through `CLAUDE.md` (`@AGENTS.override.md`). The full `AGENTS.md` stays untouched so upstream updates merge cleanly.

## Routing table: read before editing

| Working in | Read |
|---|---|
| `run_agent.py`, agent loop, `AIAgent` params | `AGENTS.md` → "AIAgent Class", "Agent Loop" |
| `cli.py`, slash commands, skins | `AGENTS.md` → "CLI Architecture", "Skin/Theme System"; `website/docs/user-guide/features/skins.md` |
| `ui-tui/`, `tui_gateway/`, dashboard `/chat` | `AGENTS.md` → "TUI Architecture" |
| `apps/desktop/` | `apps/desktop/AGENTS.md` (loads automatically) + `AGENTS.md` → "Electron Desktop Chat App", "Bot Mode" |
| `tools/`, `toolsets.py`, new tools | `AGENTS.md` → "Adding New Tools", "Toolsets"; `website/docs/developer-guide/adding-tools.md` |
| `hermes_cli/config.py`, `.env` vars | `AGENTS.md` → "Adding Configuration" |
| `plugins/`, memory or model providers | `AGENTS.md` → "Plugins"; `website/docs/developer-guide/plugins/index.md`, `model-provider-plugin.md` |
| `skills/`, `optional-skills/` | `hermes-agent-skill-authoring` skill; `AGENTS.md` → "Skills" |
| `tools/delegate_tool.py` | `AGENTS.md` → "Delegation" |
| `agent/curator.py`, `hermes_cli/curator.py` | `AGENTS.md` → "Curator"; `website/docs/user-guide/features/curator.md` |
| `cron/` | `AGENTS.md` → "Cron" |
| `plugins/kanban/`, `hermes_cli/kanban.py` | `AGENTS.md` → "Kanban"; `website/docs/user-guide/features/kanban.md` |
| `hermes_cli/update_cmd.py`, `backup.py`, `update_inventory.py` | `AGENTS.md` → "Update Pipeline" |
| `gateway/`, `plugins/platforms/` | `AGENTS.md` → "Known Pitfalls" (guards, streaming contract), "Profiles"; `ADDING_A_PLATFORM.md` |
| `hermes_cli/profiles*`, anything reading `HERMES_HOME` | `AGENTS.md` → "Profiles: Multi-Instance Support" |
| TypeScript anywhere | `AGENTS.md` → "TypeScript Style" |
| `tests/` | `AGENTS.md` → "Testing" (full text of the rules above) |

Project layout, config-section lists, toolset keys and command verbs are in the code; run `ls`, `grep`, or `hermes --help` rather than trusting a copied list. Logs: `~/.hermes/logs/` (`agent.log`, `errors.log`, `gateway.log`), browse with `hermes logs`.
