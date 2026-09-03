# `tests/` instructions (the root `AGENTS.override.md` still applies)

## Running tests

Always use `scripts/run_tests.sh`, never `pytest` directly. The script enforces CI parity: it unsets credential vars, sets `TZ=UTC` and `LANG=C.UTF-8`, and runs every test file in its own subprocess through `scripts/run_tests_parallel.py` (no xdist, worker count scaled from CPU count), so module-level dicts, sets and ContextVars cannot leak between files. Direct `pytest` on a many-core machine with API keys set diverges from CI in both directions. The script takes a directory, a file, `-k`, and pass-through pytest flags; the runner is file-granular, so `-k` always goes with a file.

Flake policy: the runner retries a failing file once in a fresh subprocess (`--file-retries`; `HERMES_TEST_FILE_RETRIES=0` disables it). Pass-on-retry counts as green but is printed under a `⚠ FLAKY` summary with both attempts, and that report is a bug to fix, not noise: timing-sensitive tests must not assume a quiet runner (wall-clock bounds of at least 2 s, event-based sync, no `assert not _wait_until(...)` negative-timing races).

## Isolation from `~/.hermes/`

Tests must not write to `~/.hermes/`; the autouse `_isolate_hermes_home` fixture in `tests/conftest.py` redirects `HERMES_HOME` to a temp dir, and tests never hardcode `~/.hermes/` paths. Profile tests also monkeypatch `Path.home()` to the temp root and set `HERMES_HOME` to `<tmp>/.hermes`, so `_get_profiles_root()` and `_get_default_hermes_home()` resolve inside it; copy the `profile_env` fixture from `tests/hermes_cli/test_profiles.py`.

## Where a test belongs

The CI change classifier (`scripts/ci/classify_changes.py`) picks jobs from the changed files, so a Python test that asserts about `package.json`, lockfiles, `tsconfig.json` or `.ts/.tsx/.js/.mjs/.cjs` sources will not run on a PR that only touches those files, and can go green on the PR and red on `main`. Such tests belong in the vitest suite, not in `tests/*.py`.

## Don't fake the host OS

Hermes supports Linux, macOS and native Windows, and host-specific behavior is tested by running on that host, not by patching `sys.platform`. Mark host-specific tests `@pytest.mark.linux_only`, `macos_only` or `windows_only`: `scripts/ci/list_os_marked_tests.py` finds files by grepping for the marker name and the lane filters with `-m <marker>`. A bare `@pytest.mark.skipif(sys.platform != "win32")` skips on Linux and is never imported on the Windows lane, so it runs nowhere; a file-local alias (`windows_only = pytest.mark.skipif(...)`) gets the file listed but `-m` deselects everything, and the lane reports green over zero coverage. Do not `pytest.skip()` the non-host rows of a platform parametrize; split into one marked test per OS, and split a test body that walks several platforms, keeping the host-native arm on the Linux lane. The line: if the test needs the interpreter to believe it is on another OS to pass, it belongs on that OS. Host-independent tests stay unmarked: pure functions that take the platform as data (`hidden_windows_child_options(opts, is_windows=True)`) and declaration or packaging invariants that assert about a file rather than runtime; setting a module-level `IS_WINDOWS` flag and then calling the function is a fake host.

Live Windows process-topology claims that mocks cannot reproduce (venv-holder scans, process-tree parentage, launcher/worker chains, detach semantics) are proven on the `wine2e` lane: the on-demand `windows-venv-e2e.yml` workflow runs `tests/hermes_cli/test_venv_holder_windows_live.py` on a real `windows-latest` runner and fires only on pushes to `wine2e/**` branches. Write probes that pin correct behavior, push to reproduce the bug live, fix, iterate until green, then open the PR with that receipt. Extend the live suite when touching that subsystem, and assert against the gateway ancestor found by argv, not the direct parent, because the venv shim makes every spawn a launcher/worker chain.

## Don't write change-detector tests

A change-detector fails whenever data that is expected to change gets updated: model catalogs, config version literals, enumeration counts, hardcoded provider model lists. It adds no behavioral coverage and only guarantees that routine updates break CI. If a test reads like a snapshot of current data, delete it; if it reads like a contract about how two pieces of data relate, keep it. When adding a provider or model, assert the relationship (every catalog entry has a context length) rather than the names.

- Don't: `assert len(_PROVIDER_MODELS["huggingface"]) == 8`
- Do: `assert raw["_config_version"] == DEFAULT_CONFIG["_config_version"]`

## Never read source code in tests

A test that reads a `.py`, `.ts` or `.tsx` file's text tests the shape of the source, not its behavior, and is banned outright. It passes when the implementation is subtly broken and fails when a correct refactor changes formatting or names, cannot run against a bundled artifact, blocks structural cleanup, and gives false confidence because it never executes the path it claims to guard. Extract the logic into a small pure or dependency-injected function and call it for real; when the logic lives inline in a god-file (`main.ts`, `cli.py`, `gateway/run.py`) and extracting feels disruptive, that is the signal to extract, not to regex around it.

- Don't: `assert.match(fs.readFileSync('main.ts', 'utf8'), /spawn\([\s\S]*hiddenWindowsChildOptions/)`
- Do: `assert.equal(hiddenWindowsChildOptions({}, true).windowsHide, true)`

## Further reading

- `scripts/run_tests.sh` and `scripts/run_tests_parallel.py` (the runner)
- `scripts/ci/classify_changes.py`, `scripts/ci/list_os_marked_tests.py` (what CI runs where)
