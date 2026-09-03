# `agent/` instructions (the root `AGENTS.override.md` still applies)

## Prompt caching must not break

Hermes keeps the per-conversation prompt cache valid for the life of a conversation. Do not alter past context, change toolsets, reload memories or rebuild the system prompt mid-conversation; cache-breaking multiplies cost. The only time context is altered is context compression. Slash commands that mutate system-prompt state (skills, tools, memory) are cache-aware: they default to deferred invalidation (the change takes effect next session) with an opt-in `--now` flag for immediate invalidation, following `/skills install --now`.

## Profile-safe code

1. Use `get_hermes_home()` (from `hermes_constants`) for every path that reads or writes state; never hardcode `~/.hermes` or `Path.home() / ".hermes"`, which breaks profiles.
2. Use `display_hermes_home()` for user-facing messages; it renders `~/.hermes` for the default profile and `~/.hermes/profiles/<name>` otherwise.
3. Module-level constants that cache `get_hermes_home()` at import time are fine, because `_apply_profile_override()` sets `HERMES_HOME` before any module imports.
4. Tests that mock `Path.home()` must also set `HERMES_HOME` in the environment, since the code reads the env var rather than `Path.home()`.

## Curator (`agent/curator.py`, `agent/curator_backup.py`, `tools/skill_usage.py`)

The curator is the background skill-maintenance loop: it tracks usage on agent-created skills, auto-archives stale ones, and takes a tar.gz snapshot before each run; usage telemetry is the sidecar `~/.hermes/skills/.usage.json`. Invariants: it only touches skills with `created_by: "agent"` provenance, so bundled and hub-installed skills are off limits; it never deletes, archive is the most destructive action, and archives under `~/.hermes/skills/.archive/` are restorable; pinned skills are exempt from every auto-transition and from the LLM review pass; `skill_manage(action="delete")` refuses pinned skills while patch, edit, `write_file` and `remove_file` still go through so the agent can keep improving them. Settings live under `curator:` in `config.yaml`; the CLI verbs are wired in `hermes_cli/curator.py`.

## Display

Do not use `\033[K` (ANSI erase-to-EOL) in spinner or display code: it leaks as literal `?[K` under `prompt_toolkit`'s `patch_stdout`. Space-pad the line instead (`f"\r{line}{' ' * pad}"`).

## Further reading

- `website/docs/developer-guide/context-compression-and-caching.md`
- `website/docs/developer-guide/prompt-assembly.md`
- `website/docs/user-guide/features/curator.md`
