# `gateway/` instructions (the root `AGENTS.override.md` still applies)

Covers `gateway/` and the platform adapters under `plugins/platforms/`.

## Two message guards; control commands bypass both

While an agent runs, an inbound message passes two sequential guards: the base adapter (`gateway/platforms/base.py`) queues it in `_pending_messages` when its session key is in `_active_sessions`, and the runner (`gateway/run.py`) intercepts `/stop`, `/new`, `/queue`, `/status`, `/approve`, `/deny` before they reach `running_agent.interrupt()`. Any new command that must reach the runner while the agent is blocked (approval prompts, for example) must bypass both guards and be dispatched inline, never through `_process_message_background()`, which races the session lifecycle.

## Streaming delivery contract (stream-is-the-message adapters)

Adapters with `draft_stream_is_message = True` (relay Slack native streaming) keep one cumulative native stream per turn, and that stream is the final message. Four invariants, each learned from a live duplicate-final incident; break one and you get a duplicate or a frozen stream:

1. Draft frames are prefix-stable: frame N is a string prefix of frame N+1. Never mutate frames per tick (no fence-closing, no cursor suffix, no segment-state resets at tool boundaries, no mrkdwn conversion); a non-prefix frame makes the platform re-append the whole snapshot ("stacked copies"). Only the finalize path may transform the real final.
2. The consumer declares the final; the adapter never guesses. `finish(final_text)` carries the completed `final_response` (verifier footer and completion explainer included) as the authoritative finalize payload. Any post-stream augmentation must ride that payload; mutating `final_response` after the stream sealed produces a `delivered_final_matches` mismatch and a corrective duplicate send.
3. Interim sends carry `metadata["_interim_send"] = True`. Any consumer-side `adapter.send()` that is not the turn-final (commentary, segment-tail flushes) must set it, or seal-interception seals the live stream with interim text. Seal-interception lives at both egress doors, `send()` and `send_for_platform()`; a new egress door needs the same two checks.
4. Reconcile by edit, never by plain send. A lane that delivers a final beside an already-sealed stream (queued follow-ups, media-accompanied finals) first tries `edit_message` on the consumer's `message_id`; plain `send()` is the fallback only when no editable message exists. A sealed native stream is a regular message and `chat.update` works on it.

Contract tests: `tests/gateway/test_stream_final_contract.py` (all four invariants, mutation-checked). Slack ground truth: `chat.*Stream` speaks standard markdown, not mrkdwn; `stopStream.markdown_text` appends rather than replaces; `startStream`/`stopStream` are rate-limit Tier 2 (about 20/min). Check `draft_stream_is_message` with `is True`, because MagicMock adapters in older tests auto-create truthy attributes.

## Background process notifications

For `terminal(background=true, notify_on_complete=true)` the gateway runs a watcher that detects completion and triggers a new agent turn. Verbosity is `display.background_process_notifications` in config.yaml (or `HERMES_BACKGROUND_NOTIFICATIONS`): `concise` (default: one line, plus a short output tail on failure), `all` (running-output updates plus the final raw output), `result` (final raw output only), `error` (final raw output only on a non-zero exit), `off`.

## Cron delivery framing

Cron deliveries are never mirrored into the target gateway session. They land in their own cron session with a header/footer frame so the main conversation's message-role alternation stays intact.

## Token locks for adapters

An adapter that connects with a unique credential (bot token, API key) calls `acquire_scoped_lock()` from `gateway.status` in `connect()`/`start()` and `release_scoped_lock()` in `disconnect()`/`stop()`, so two profiles cannot use one credential. Canonical pattern: `plugins/platforms/irc/adapter.py`.

## Multiplex profile-scoped env reads fail closed

Under `gateway.multiplex_profiles`, `os.environ` holds the default profile's values; a secondary profile's `.env` lives only in its secret scope, installed per turn by `_profile_runtime_scope` (`agent/secret_scope.py` contract). Every profile-level env read, credentials and authorization alike (`app_secret`, tokens, `FEISHU_ALLOWED_USERS`, `{PLATFORM}_ALLOW_ALL_USERS`, `GATEWAY_ALLOW_ALL_USERS`, `group_policy`, `allow_bots`), goes through a scope-aware reader: adapters use `_get_scoped_secret()` (canonical fail-closed copy in `plugins/platforms/feishu/adapter.py`), gateway authz uses `_auth_env()` / `_platform_gate_env()` in `gateway/authz_mixin.py`.

- Scope installed and multiplex active: a scoped miss returns the default value. Never fall through to `os.environ`; that leaks another profile's value and silently breaks routing or admission (a leaked allowlist skips the allow-all check and rejects every secondary-profile sender).
- The unscoped default-profile path (`UnscopedSecretError`) and single-profile deployments keep the `os.environ` read, because there it is the profile's own value.
- Authorization config is the sharpest edge: allowlist and allow-all leaks show up only as missing replies, or fail open.
- The `_get_scoped_secret` wrapper is copy-pasted across roughly 15 adapters. When touching any of them keep the fail-closed shape, and never reintroduce `except _UnscopedSecretError: val = os.getenv(...)` after a miss.

## Further reading

- `website/docs/developer-guide/gateway-internals.md`
- `gateway/platforms/ADDING_A_PLATFORM.md`
