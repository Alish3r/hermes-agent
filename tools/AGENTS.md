# `tools/` instructions (the root `AGENTS.override.md` still applies)

Also covers `toolsets.py` and `model_tools.py` at the repo root, which decide what these tools expose.

## Adding a tool

Settle the footprint question first (root override, footprint ladder): most capabilities should not be core tools. Custom or local-only tools go through the plugin route, `~/.hermes/plugins/<name>/plugin.yaml` plus `__init__.py` registering with `ctx.register_tool(...)`; plugin toolsets are discovered automatically and need no edits under `tools/` or in `toolsets.py`. Use the core route only when explicitly contributing a tool that should ship in the base system.

A core tool needs two files. `tools/<name>.py` calls `registry.register(name=, toolset=, schema=, handler=, check_fn=, requires_env=)` at module top level; any `tools/*.py` with a top-level `registry.register()` is imported automatically, so there is no import list to maintain. Then the tool's name must be added to a toolset in `toolsets.py`, either `_HERMES_CORE_TOOLS` (every platform) or a named toolset: auto-discovery registers the schema, but only a toolset exposes the tool to an agent, and `_HERMES_CORE_TOOLS` is the default bundle every platform's base toolset inherits, not dead code. The registry handles schema collection, dispatch, availability checks and error wrapping; every handler returns a JSON string. `check_fn` results are TTL-cached process-wide (`tools/registry.py`).

Schema descriptions that mention paths (default output directories, for example) use `display_hermes_home()`; the schema is built at import time, after `_apply_profile_override()` set `HERMES_HOME`. Persistent state (caches, logs, checkpoints) lives under `get_hermes_home()`, never `Path.home() / ".hermes"`, so each profile gets its own. Agent-level tools (todo, memory) are intercepted by `run_agent.py` before `handle_function_call()`; `tools/todo_tool.py` shows the pattern.

Tool schema descriptions must not name tools from other toolsets (a `browser_navigate` description saying "prefer web_search", for instance): the other tool may be unavailable (missing key, disabled toolset) and the model will hallucinate calls to it. Add cross-references dynamically in `get_tool_definitions()` in `model_tools.py`, following the `browser_navigate` / `execute_code` post-processing blocks. `_last_resolved_tool_names` in `model_tools.py` is a process-global that `_run_single_child()` in `delegate_tool.py` saves and restores around subagent execution; new code reading it may see a stale value during child runs.

## Toolsets

All toolsets are the single `TOOLSETS` dict in `toolsets.py`; each platform adapter picks a base toolset (Telegram uses `messaging`) and most inherit `_HERMES_CORE_TOOLS`. Users enable or disable per platform through `hermes tools` or the `tools.<platform>.enabled` / `disabled` lists in `config.yaml`. Read the current keys from the file rather than a copied list.

## Surface capability is a property of the session, never of the process env

A tool that only works because of who is on the other end of the connection (desktop panes, the in-app browser, message reactions, Projects) resolves its availability from the session's own source. The client and the backend are separate machines: the desktop app may drive a locally spawned backend, one over SSH, one behind a URL + token, or Hermes Cloud, and only the first two carry `HERMES_DESKTOP=1`. An env-keyed GUI gate is therefore a silent no-op on the other topologies: the tool is stripped from the schema before the model ever sees it while the platform hint claims the session is inside the desktop app.

The pattern that works: keep such tools off `_HERMES_CORE_TOOLS` and in a named toolset (`desktop_ui`, `project`) that the GUI gateway's `_load_enabled_toolsets(platform)` folds in when the session's platform says GUI. `check_fn` answers reachability or user opt-in ("is the renderer bridge wired?", "did the user enable reactions?"), never surface ("was I spawned by Electron?"), and because it is cached process-wide a per-session answer does not belong there. `HERMES_DESKTOP=1` legitimately means "this backend process was spawned by the app" (it gates the cron ticker and web-dist handling), not "a GUI is watching"; the embedded terminal pane (`hermes --tui` against that backend) is the standing counterexample. Test: if the capability would still make sense with the client on another machine, it is session-scoped, and the test asserts the GUI session gets the tool with the env var absent.

## Delegation (`tools/delegate_tool.py`)

`delegate_task` spawns a subagent with an isolated context and terminal session; by default the parent waits for the child's summary, and with `background=true` it gets a delegation id immediately and the result re-enters the conversation through the async-delegation completion queue. Single shape: `goal` (plus optional `context`, `toolsets`); batch shape: `tasks: [...]`, each running concurrently, capped by `delegation.max_concurrent_children`. `role="leaf"` (default) cannot call `delegate_task`, `clarify`, `memory`, `send_message` or `cronjob` but keeps `execute_code`; `role="orchestrator"` keeps `delegate_task`, gated by `delegation.orchestrator_enabled` and bounded by `delegation.max_spawn_depth`. The remaining knobs live under `delegation:` in `config.yaml`. Background delegation is detached from the turn but still process-local: work that must survive a restart uses `cronjob` or `terminal(background=True, notify_on_complete=True)`.

## Further reading

- `website/docs/developer-guide/adding-tools.md`
- `website/docs/developer-guide/tools-runtime.md`
- `website/docs/developer-guide/subagent-lifecycle-api.md`
