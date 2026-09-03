# `cron/` instructions (the root `AGENTS.override.md` still applies)

## Scheduled jobs

`cron/jobs.py` is the job store and `cron/scheduler.py` the tick loop; agents schedule through the `cronjob` tool and users through `hermes cron <verb>` or `/cron`. Schedules accept a duration (`30m`, `2h`, `1d`), an "every" phrase (`every 2h`, `every monday 9am`), a 5-field cron expression, or an ISO timestamp for a one-shot. Per-job fields include `skills`, `model` / `provider` overrides, `script` (a pre-run data-collection script whose stdout is injected into the prompt; `no_agent=True` makes the script the entire job), `context_from` (chain one job's last output into another's prompt), `workdir` (run in a directory with its `AGENTS.md`/`CLAUDE.md` loaded), and multi-platform delivery.

## Hardening invariants

- Cron sessions get a 3-minute hard interrupt so a runaway agent loop cannot monopolize the scheduler.
- The catch-up window is half the job's period, clamped to 120 s-2 h; a one-shot job whose fire time was missed gets a 120 s grace window.
- A file lock at `~/.hermes/cron/.tick.lock` prevents duplicate ticks across processes.
- Cron sessions pass `skip_memory=True` by default; memory providers intentionally do not run during cron.
- Deliveries are never mirrored into the target gateway session: they land in their own cron session with a header/footer frame so the main conversation's message-role alternation stays intact.

## Further reading

- `website/docs/developer-guide/cron-internals.md`
- `website/docs/user-guide/features/cron.md`
