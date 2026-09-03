# `plugins/` instructions (the root `AGENTS.override.md` still applies)

Subdirectories with their own file: `plugins/memory/`, `plugins/model-providers/`, `plugins/kanban/`. Platform adapters under `plugins/platforms/` follow `gateway/AGENTS.md`.

## General plugins (`hermes_cli/plugins.py` + `plugins/<name>/`)

Repo-shipped plugins live here so they are discovered alongside user plugins in `~/.hermes/plugins/`, `./.hermes/plugins/` and pip entry points. A plugin exposes `register(ctx)` and through it registers lifecycle hooks (`pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_llm_call`, `on_session_start`, `on_session_end`), tools via `ctx.register_tool(...)`, and CLI subcommands via `ctx.register_cli_command(...)`, whose argparse tree is wired into `hermes <plugin> <subcmd>` at startup with no change to `main.py`. Hooks are invoked from `model_tools.py` (tool hooks) and `run_agent.py` (lifecycle). Discovery timing pitfall: `discover_plugins()` runs only as a side effect of importing `model_tools.py`, so code that reads plugin state without that import must call it explicitly (it is idempotent).

Plugins must not modify core files (`run_agent.py`, `cli.py`, `gateway/run.py`, `hermes_cli/main.py` and the like). When a plugin needs a capability the framework does not expose, expand the generic plugin surface with a new hook or ctx method; never hardcode plugin-specific logic into core.

## Compatibility policy

The contract is `website/docs/developer-guide/plugins/index.md#native-plugin-compatibility-contract`. Compatibility is a behavior contract, not a monolithic `PLUGIN_API_VERSION`, a manifest-wide native `api:` match, or version literals on unrelated payloads. Keep documented surfaces additive: add hook payload data as keyword fields and signature-inspect callbacks so old narrow signatures receive only the fields they declare while `**kwargs` callbacks get the full payload; never remove or rename `PluginContext` methods, and make new parameters optional with defaults, keyword-only where possible; ignore unknown native manifest fields; give new provider methods default implementations and signature-inspect optional callback kwargs rather than forwarding them unconditionally; use a local schema version only for a capability with a wire or persisted contract, and preserve old state/config/session replay or ship a migration. A deprecation needs a once-per-process warning, a documented replacement with a migration note, and at least two subsequent minor releases before removal. Compatibility tests load frozen plugins through the real discovery path and assert outcomes, never exact registry or catalog counts, source-reading checks, or "the global version literal changed".

## No new third-party-product plugins in-tree

A plugin that integrates someone else's product or project (observability or metrics backends, vendor SaaS connectors, analytics dashboards, paid-service tie-ins) ships as a standalone plugin repo that users install into `~/.hermes/plugins/` or via pip entry points; it registers through the existing discovery path and uses the ABCs, hooks and ctx surface already exposed, so core needs nothing special. The reason is maintenance load: every absorbed product becomes our burden against a fast-moving core for a backend we do not own. This is a coupling decision, not a quality judgment; the `observability/`, `kanban/` and `disk-cleanup/` directories already here are precedent, not an invitation. Standalone plugins are promoted in the Nous Research Discord (`#plugins-skills-and-skins`).

## Other plugin families

`plugins/context_engine/`, `plugins/image_gen/` and similar follow the same shape: an ABC, an orchestrator, and one directory per plugin. Context engines plug into `agent/context_engine.py`; image-gen providers into `agent/image_gen_provider.py`. Reference and docs-companion plugins (`example-dashboard`, `strike-freedom-cockpit`, `plugin-llm-example`, `plugin-llm-async-example`) live in the `hermes-example-plugins` companion repo (github.com/NousResearch/hermes-example-plugins), not in this tree.

## Further reading

- `website/docs/developer-guide/plugins/index.md`
