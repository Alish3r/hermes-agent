# `plugins/model-providers/` instructions (the root `AGENTS.override.md` and `plugins/AGENTS.md` still apply)

## Model-provider plugins

Every inference backend ships as a plugin here; each plugin's `__init__.py` calls `providers.register_provider(ProviderProfile(...))` at module load. `providers/__init__.py._discover_providers()` is a lazy, separate discovery system, scanned on the first `get_provider_profile()` or `list_providers()` call and not by the general `PluginManager`. Scan order is bundled (`<repo>/plugins/model-providers/<name>/`), then user (`$HERMES_HOME/plugins/model-providers/<name>/`), then legacy `<repo>/providers/<name>.py` for back-compat. `register_provider()` is last-writer-wins, so a user plugin with the same name overrides the bundled profile; that is how third parties swap out a built-in profile without a repo patch.

The general `PluginManager` records `kind: model-provider` manifests but does not import them, because that would instantiate the `ProviderProfile` twice. A plugin without an explicit `kind:` is auto-coerced by a source-text heuristic (`register_provider` + `ProviderProfile` present in `__init__.py`).

Plugins must not modify core files (see `plugins/AGENTS.md`).

## Further reading

- `website/docs/developer-guide/model-provider-plugin.md`
