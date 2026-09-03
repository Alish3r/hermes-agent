# `hermes_cli/` instructions (the root `AGENTS.override.md` still applies)

## Slash commands (`hermes_cli/commands.py`)

Every slash command is a `CommandDef` in the central `COMMAND_REGISTRY`; the CLI dispatcher, gateway dispatch and `/help`, the Telegram BotCommand menu, the Slack `/hermes` subcommand map, autocomplete and the CLI help are all derived from it, so never add a parallel command list. To add a command: add the `CommandDef` (name, description, category, aliases, `args_hint`, `cli_only` / `gateway_only`), add the handler branch on the canonical name in `HermesCLI.process_command()` in `cli.py`, and, if the command works in the gateway, add a handler in `gateway/run.py`. Persistent settings go through `save_config_value()` in `cli.py`. An alias needs only the `aliases` tuple on the existing entry. A `cli_only` command with `gateway_config_gate` set becomes available in the gateway when that config dotpath is truthy; `GATEWAY_KNOWN_COMMANDS` always includes gated commands so the gateway can dispatch them, and help/menus show them only when the gate is open. Commands that mutate system-prompt state default to deferred invalidation with an opt-in `--now` (see `agent/AGENTS.md`).

## Adding configuration (`hermes_cli/config.py`)

New settings go in `DEFAULT_CONFIG`; bump `_config_version` only when a migration is needed. Three loaders exist, `load_cli_config()` (interactive CLI), `load_config()` (subcommands) and the raw YAML read in `gateway/run.py` + `gateway/config.py`; if one surface sees a key and another does not, you are on the wrong loader. Secrets are the only thing that belongs in `.env` (`OPTIONAL_ENV_VARS`); behavior goes in `config.yaml`, never a new `HERMES_*` env var.

## Menus and skins

Interactive menu pickers use `hermes_cli/curses_ui.py` (example: `hermes_cli/tools_config.py`). Skins are pure data in `hermes_cli/skin_engine.py` and `~/.hermes/skins/*.yaml`, loaded user-first with missing values inherited from `default`; a new skin needs no code, so do not add skin-specific branches to display code.

## Profiles

A profile is a fully isolated `HERMES_HOME`; `_apply_profile_override()` in `hermes_cli/main.py` sets the env var before any module import, so every `get_hermes_home()` call scopes automatically (the profile-safe code rules are in `agent/AGENTS.md`). Profile operations themselves are HOME-anchored on purpose: `_get_profiles_root()` returns `Path.home() / ".hermes" / "profiles"`, not `get_hermes_home() / "profiles"`, so `hermes -p coder profile list` sees every profile regardless of which one is active.

## Process identity: never infer it from argv substrings

Classifying a process by `"serve" in cmdline` is the bug class behind about ten fleet-update issues: `kanban --preserve-cache` contains "serve", a flag value can equal a subcommand (`-m dashboard serve`), and truncated cmdlines hide the real subcommand. Use the canonical matchers, `gateway.status.looks_like_gateway_command_line` for gateway processes and `hermes_cli.update_cmd._hermes_holder_subcommand` for the top-level subcommand of any Hermes argv; never hand-roll token scans. Flag sets are derived from the parser (`_holder_value_flags()` introspects `build_top_level_parser()`), never hand-written. Match on full cmdlines and truncate only at display time. Never blanket-exclude ancestors from process scans: when `/update` runs as the gateway's child, the gateway ancestor must stay visible to the pause machinery, so exclude interactive ancestry and carve out gateway-shaped ancestors. Before adding any scan heuristic read the gateway control-socket design (#92091): the socket is becoming the primary coordination mechanism and scans are the fallback for old or crashed processes.

## Update pipeline (`hermes update`)

The updater is transactional: plan, snapshot, apply, restart-per-kind, verify, report. Every stage exists because its absence was a field failure, so a change that weakens a stage must answer for the failure class it guards.

- Plan (`update_inventory.py`, `hermes update --plan`) is a read-only inventory of install kind, all profiles and every live gateway with its supervisor and running code version. `git` installs update in place; `docker`, `nix` and `apt` are not in-place-updatable and the updater prints the correct external command instead of fighting the deployment model.
- Snapshot (`backup.py`) takes a quick snapshot of every profile (the code swap and fleet restart touch all of them), each into its own `state-snapshots/`, identical file set, 1 GiB per-file cap, keep=1. Never add a partial or tiered snapshot set; mixed coverage creates torn restores across schema generations. Quick snapshots are file-loss recovery, not code rollback; `--backup` full mode owns rollback.
- Apply is a git pull. The Windows ZIP fallback fires only when git itself failed (`_should_zip_fallback_on_update_error`, argv-classified; a dependency-install failure must never trigger a tree-clobbering re-download), refuses a dirty working tree (`-uall` plus a pre-swap re-check), and grafts the live `apps/desktop/release/` into the staged swap because the source ZIP has no built desktop app.
- Restart-per-kind is fleet-wide: every `hermes-gateway*` systemd unit and every `ai.hermes.gateway*` LaunchAgent, drain-first (SIGUSR1) with per-unit failure isolation. Restarting only the invoking profile's service leaves siblings on stale `sys.modules` until they crash.
- Verify: gateways stamp `code_sha`/`code_version` into `gateway_state.json` on every runtime-status write (`gateway/status.py`); after restart the updater compares each live gateway against the fresh checkout and prints a fleet version matrix. A provably stale gateway fails the update with exit 1; automation must never treat a mixed-version fleet as healthy.
- Report: every run writes a receipt to `~/.hermes/logs/update_receipts/` (`latest.json` pointer; steps, skips with reasons, restart outcome, plan, fleet snapshot). Finalization belongs to the `cmd_update` command boundary, so early `sys.exit` paths (preflight refusals, fetch failures) still persist a receipt with the real exit code. A begun-but-unwritten receipt is a bug: refused and failed runs are the ones receipts exist for.

Process-scan coordination between the updater, serve/dashboard and the gateway is being replaced by a gateway-owned control socket; do not add new scan heuristics without checking that design.

## Gateway lifecycle vs the desktop app

`hermes serve` (the control plane the desktop app spawns) dies with the app by design. The messaging gateway (`gateway run`) survives it: the serve backend's `/api/gateway/*` endpoints spawn it detached (`_spawn_hermes_action`, `start_new_session` / `DETACHED_PROCESS`), so the app's `before-quit` SIGTERM never reaches it and bots keep running after the app closes. The one known breach is the Windows shim-unlock teardown (`taskkill /T /F` on venv-shim holders), which exists to let updates proceed and is being replaced by the control socket's `pause-for-update`. Do not "fix" gateway-dies-with-app reports by re-parenting the gateway under the backend, and do not "fix" update locks by widening the tree-kill.

## Further reading

- `website/docs/developer-guide/extending-the-cli.md`
- `website/docs/user-guide/features/skins.md`
