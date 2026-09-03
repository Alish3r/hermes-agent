# `plugins/memory/` instructions (the root `AGENTS.override.md` and `plugins/AGENTS.md` still apply)

## Memory-provider plugins

Memory backends use a separate discovery system from the general `PluginManager`. It covers the same four sources (bundled, `$HERMES_HOME/plugins/`, `./.hermes/plugins/` opt-in via `HERMES_ENABLE_PROJECT_PLUGINS`, and `hermes_agent.memory_providers` entry points) but with bundled-first precedence, the reverse of the general system's later-wins order: a provider is activated by name, so a dropped-in directory must not be able to shadow a shipped one. Discovery enumerates without importing; nothing runs until `memory.provider` in config.yaml names it.

Each provider implements the `MemoryProvider` ABC in `agent/memory_provider.py` and is orchestrated by `agent/memory_manager.py`; the lifecycle hooks are `sync_turn(turn_messages)`, `prefetch(query)`, `shutdown()` and the optional `post_setup(hermes_home, config)` for setup-wizard integration. If a provider's `cli.py` defines `register_cli(subparser)`, `discover_plugin_cli_commands()` wires it into `hermes <plugin>` at argparse setup time, but only for the currently active provider, so disabled providers do not clutter `hermes --help`.

Providers must not modify core files; when one needs something core does not expose, expand the generic plugin surface instead (see `plugins/AGENTS.md`).

## The set of in-tree providers is closed

No new directories under `plugins/memory/`. A new backend ships as a standalone plugin repo that users install into `~/.hermes/plugins/` or via pip entry points: it implements the same `MemoryProvider` ABC, registers through the same discovery path, and integrates through `hermes memory setup` / `post_setup()` without landing in this tree. Existing providers stay, and bug fixes to them are welcome.

## Further reading

- `website/docs/developer-guide/memory-provider-plugin.md`
