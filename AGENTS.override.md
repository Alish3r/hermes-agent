# Hermes Agent - Development Guide (condensed local override)

This file replaces the 96 KB `AGENTS.md` for sessions on this machine (Hermes and Codex load `AGENTS.override.md` first). It keeps every universal rule and every rule about the root-level files (`run_agent.py`, `cli.py`, `model_tools.py`, `toolsets.py`). Subsystem rules live in per-directory `AGENTS.md` files (routing table below); Hermes appends one to the tool result the first time a tool touches its directory. The full `AGENTS.md` stays on disk. Never give up on the right solution.

## What Hermes is

A personal AI agent with one agent core behind a CLI, a messaging gateway (Telegram, Discord, Slack, ~20 platforms), an Ink TUI, and an Electron desktop app. It learns across sessions (memory + skills), delegates to subagents, runs cron jobs, drives a terminal and a browser. It is extended through **plugins and skills**, not by growing the core.

Two invariants shape every review:

- **Per-conversation prompt caching is sacred.** Never mutate past context, swap toolsets, reload memories, or rebuild the system prompt mid-conversation. The one exception is context compression. The system prompt must be byte-stable for the life of a conversation. Slash commands that change prompt state default to deferred invalidation with an opt-in `--now` (see `/skills install --now`).
- **Narrow waist, capability at the edges.** Every core model tool ships on every API call. Footprint ladder, pick the highest rung that works: 1 extend existing code → 2 CLI command + skill → 3 service-gated tool (`check_fn`) → 4 plugin (`~/.hermes/plugins/` or pip) → 5 MCP server in the catalog → 6 new core tool (last resort; terminal, read_file, web_search are the bar). When 3+ PRs integrate the same category, build one ABC + orchestrator and turn them into plugins.

Strict message-role alternation: never two same-role messages in a row, never a synthetic user message injected mid-loop.

## Contribution rubric (short form; full text: `AGENTS.md` → "Contribution Rubric")

Want: real bug fixes that reproduce on `main`, point to the exact line, and fix the whole class incl. sibling call paths · new platforms/providers/models/desktop features as long as they use `hermes tools` / `hermes setup` / auto-install rather than raw env vars · extraction of god-file clusters (`cli.py`, `run_agent.py`, `gateway/run.py`) into modules · extend before duplicate · behavior-contract tests · E2E against a temp `HERMES_HOME` with real imports for resolution chains, config propagation, security boundaries, remote backends, file/network I/O.

Reject even when well built: speculative hooks with no consumer · new `HERMES_*` env vars for non-secret config (`.env` is secrets only; behavior goes in `config.yaml`) · a core tool where terminal + file or a skill would do · `offset`/`limit` pagination on instructional content · "fixes" that kill the feature they secure (read `git log -p -S` first) · outbound telemetry without an opt-in gate · change-detector tests · dead code wired in without E2E proof · plugins that touch core files · third-party products or someone else's project under `plugins/` (standalone plugin repos only; `plugins/memory/` is closed).

Before calling something a bug, verify the premise against the code: intentional isolation is not a gap (profiles are islands by design), a fix must change the line where the symptom manifests, and a "missing" piece may be load-bearing. When in doubt about intent, ask.

Surface capability is a property of the **session**, never of the process env: GUI-only tools live in a named toolset folded in per session, and `HERMES_DESKTOP=1` means "spawned by the app", not "a GUI is watching" (`tools/AGENTS.md`).

## Development environment and testing

```bash
source .venv/bin/activate      # or venv/
scripts/run_tests.sh                                   # full suite, CI parity (never call pytest directly)
scripts/run_tests.sh tests/agent/test_foo.py -k test_x # file + -k; the runner is file-granular
```

- `run_tests.sh` unsets credential vars, sets TZ=UTC and LANG=C.UTF-8, and runs each test file in its own subprocess; a `⚠ FLAKY` pass-on-retry is a bug to fix. Tests never write to `~/.hermes/` (the autouse fixture redirects `HERMES_HOME`).
- No change-detector tests, never read source-file text in a test, JS-artifact assertions go in vitest, host-OS behavior uses `@pytest.mark.<os>_only` markers. Full rules: `tests/AGENTS.md`.

## Universal code rules

- Paths: `get_hermes_home()` for state, `display_hermes_home()` for user-facing text (both from `hermes_constants`). Never `~/.hermes` or `Path.home() / ".hermes"`; profiles set `HERMES_HOME` before imports, so module-level constants are fine (`_get_profiles_root()` is HOME-anchored on purpose).
- Multiplex profile env reads fail closed (never fall through to `os.environ` after a scoped miss) and adapters with a unique credential take `acquire_scoped_lock()` / `release_scoped_lock()` from `gateway.status`. Details: `gateway/AGENTS.md`.
- Dependencies need ceilings: PyPI `>=floor,<next_major` (pre-1.0: `<0.(minor+2)`), git URLs pinned to a 40-char SHA, GitHub Actions pinned to SHA + version comment; run `uv lock`. Bare `>=X` is rejected.
- Never infer process identity from argv substrings; use `gateway.status.looks_like_gateway_command_line` / `hermes_cli.update_cmd._hermes_holder_subcommand`, derive flag sets from the parser, match full cmdlines. Details: `hermes_cli/AGENTS.md`.
- Tool schema descriptions must not name tools from other toolsets; add cross-references dynamically in `get_tool_definitions()` in `model_tools.py`. All handlers return a JSON string.
- Interactive CLI menus use `hermes_cli/curses_ui.py`. No `\033[K` in spinner/display code (space-pad instead).
- `_last_resolved_tool_names` in `model_tools.py` is process-global and may be stale during subagent runs.
- Adding a core tool: `tools/your_tool.py` with `registry.register(...)` **and** an entry in a toolset in `toolsets.py` (only a toolset exposes it); prefer the plugin route. Details: `tools/AGENTS.md`.
- Adding config: `DEFAULT_CONFIG` in `hermes_cli/config.py`; three loaders exist, and a key visible on one surface but not another means the wrong loader. Details: `hermes_cli/AGENTS.md`.
- Adding a slash command: `CommandDef` in `COMMAND_REGISTRY` (`hermes_cli/commands.py`) plus a handler branch in `HermesCLI.process_command()` in `cli.py`; aliases need only the registry entry. Details: `hermes_cli/AGENTS.md`.
- Plugins: `register(ctx)` hooks and `ctx.register_*`; `discover_plugins()` only runs when `model_tools.py` is imported; plugins never touch core files. Details: `plugins/AGENTS.md`.
- Background `delegate_task` is process-local; work that must survive a restart uses `cronjob` or `terminal(background=True, notify_on_complete=True)`. Details: `tools/AGENTS.md`.

## Subsystem invariants: where they live

- `gateway/AGENTS.md`: two message guards, streaming delivery contract, background-process notifications, cron delivery framing, token locks, multiplex fail-closed reads.
- `hermes_cli/AGENTS.md`: slash commands, config loaders, menus, skins, profiles, argv process identity, `hermes update` pipeline, gateway lifecycle vs desktop.
- `tools/AGENTS.md`: adding tools, toolsets, session-scoped surface capability, delegation.
- `agent/AGENTS.md`: prompt caching, profile-safe code, curator invariants, spinner rule.
- `apps/desktop/AGENTS.override.md`: desktop architecture, slash-palette curation, Bot Mode.
- `skills/AGENTS.md`, `tests/AGENTS.md`, `plugins/AGENTS.md` (+ `memory/`, `model-providers/`, `kanban/`), `cron/AGENTS.md`, `ui-tui/AGENTS.md` (includes TypeScript style).

## Local checkout conventions (this machine)

- This checkout follows `origin/main` (NousResearch) from the branch `local/alisher`. Commit local adaptations on `local/alisher`, never on `main`: `hermes update` hard-resets a diverged `main` but merges `origin/main` into a custom branch. Do not open PRs against NousResearch from here.
- Keep this file under 20,000 chars (the Hermes floor cap; `hermes prompt-size` does not warn when it is exceeded) and any per-directory `AGENTS.md` under 8,000 chars (the lazy-hint cap; `apps/desktop/AGENTS.md` already exceeds it and is cut when loaded). Check with `wc -m AGENTS.override.md`.
- Hermes and Codex load this file instead of `AGENTS.md`; Claude Code loads it through `CLAUDE.md` (`@AGENTS.override.md`). The full `AGENTS.md` stays untouched so upstream updates merge cleanly.

## Routing table: read before editing

| Working in | Read first | Then, if needed (`AGENTS.md` section) |
|---|---|---|
| `run_agent.py`, agent loop, `AIAgent` params | this file | "AIAgent Class", "Agent Loop" |
| `cli.py`, `hermes_cli/` (commands, config, skins, profiles, update) | `hermes_cli/AGENTS.md` | "CLI Architecture", "Adding Configuration", "Profiles", "Update Pipeline" |
| `gateway/`, `plugins/platforms/` | `gateway/AGENTS.md` | `gateway/platforms/ADDING_A_PLATFORM.md`; "Known Pitfalls" |
| `tools/`, `toolsets.py`, `model_tools.py`, `tools/delegate_tool.py` | `tools/AGENTS.md` | "Adding New Tools", "Toolsets", "Delegation" |
| `agent/`, curator, prompt caching | `agent/AGENTS.md` | "Important Policies", "Curator" |
| `apps/desktop/` | `apps/desktop/AGENTS.override.md` | `apps/desktop/AGENTS.md`; "Electron Desktop Chat App", "Bot Mode" |
| `ui-tui/`, `tui_gateway/`, dashboard `/chat`, TypeScript anywhere | `ui-tui/AGENTS.md` | "TUI Architecture", "TypeScript Style" |
| `plugins/` (memory, model providers, kanban), `hermes_cli/kanban.py` | `plugins/AGENTS.md` + the subdirectory's file | "Plugins", "Kanban" |
| `skills/`, `optional-skills/` | `skills/AGENTS.md` | `hermes-agent-skill-authoring` skill |
| `cron/` | `cron/AGENTS.md` | "Cron" |
| `tests/` | `tests/AGENTS.md` | "Testing" |

Project layout, config keys, toolset keys and command verbs are in the code; run `ls`, `grep`, or `hermes --help` rather than trusting a copied list. Logs: `hermes logs`.
