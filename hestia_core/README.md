# hestia_core

Hestia is the MCX multi-strategy host: one process, one Angel One login, several strategy engines (Prometheus, Selene, later Helios and Typhon) running side by side. Engines decide (entries, exits, stops, the roll); Hestia does the work (orders, fills, retries, reconciliation, data, alerts, teardown). Plans and decisions: `plans/selene-production.md` (design), `plans/hestia-interface-spec.md` (the engine interface, v1.1), `plans/hestia-p1-inventory.md`, `plans/hestia-p4-live-services.md` (this build).

**Status:** built and tested against doubles; **not connected to Angel One and not scheduled.** Every entry in `hestia_config.ENGINES` is disabled (the Prometheus engine is ported in `../prometheus_engine/`, replay-verified against recorded live days, and awaiting its cutover, `plans/hestia-p6-cutover.md`; Selene is written in a later phase), and with none enabled `hestia.py` exits before it logs in, so running it cannot evict another process's session. The first real login is a separate, explicitly approved step.

## How it fits together

```
engines (engine threads)                                    the interface: interface.py (frozen dataclasses, two Protocols)
   |  ctx.next_event / ctx.submit / ctx.position ...
HestiaCore (core.py)  policy, once: request registry, admission, priority dispatch, ledger, UNCONFIRMED reconciliation, supervision
   |  three ports (ports.py)
   +-- Scheduler   SimKernel (fake_kernel.py) | RealReactor (reactor.py)
   +-- BrokerPort  SimBroker (replay.py)      | AngelBrokerPort (angel_broker.py) via BrokerGateway (gateway.py); PaperBroker; BrokerRouter picks per engine
   +-- DataPort    ReplayData (replay.py)     | LiveData (live_data.py) over mcx_market.py, history.py, candle_fetch.py, feed_port.py
roll rules for every engine: roll_policy.py (pure functions, pinned to Prometheus's contract resolution and basis lookup)
recorded live days as an oracle and replay input: recorded.py (log parser, trades cross-check, data-path table, LoggedReplayData; `python -m hestia_core.recorded`)
cutover aids: preflight.py (`python hestia.py --check`: no-login start report), slack_bridge.py (where the Slack listener writes Prometheus's flags and sizing, standalone or hosted), ../prometheus_engine/seed_state.py (standalone state to the engine's state); plan plans/hestia-p6-cutover.md
host: host.py (HestiaHost) + lifecycle.py (teardown order, signals) + flags.py + state_store.py + sizing.py + session_lock.py
      + slack_queue.py + alert_router.py + reporting.py; entry point ../hestia.py, configuration ../hestia_config.py
```

The fake Hestia (`fake.py`) and the live one are the same `HestiaCore` on different ports, so the contract tests (`tests/test_hestia_fake_*.py`) exercise the policy that trades. Live pieces have their own tests against scripted doubles (`tests/smartapi_double.py`).

## Guarantees engines can rely on

- One request per decision; Hestia owns retries, partial-fill completion of exits, ghost-order recovery and fill confirmation. Request ids are idempotent, also across a process restart (the request journal).
- An engine changes its state only on a confirmed outcome (`FILLED` / `PARTIAL`). `UNCONFIRMED` is settled by reading that order's own broker row; an order still working at the broker is never reported as "no fill".
- Priority when engines clash: stop or flatten, then closes, then roll exits, then entries, then roll re-opens; entries can hold only part of the execution workers so a stop never queues behind them; requests of one engine on one contract are served in submit order.
- Hard limits at admission that engine bugs cannot bypass: the unit cap (only ever from configuration), no entry inside the roll window (5 trading days to expiry), no request at or after the session close, margin reserved against the engine's own pool (the live account, or a paper engine's own cash).
- Crash containment: an engine that raises is resumed from its saved state with backoff and a retry limit, never touching the session or other engines. A silent engine raises Slack alerts. `KILL` stops one engine and leaves its position untouched.
- Shutdown order: stop engines, wait for their threads, let in-flight requests finish, flush Slack, `terminateSession` once, only if every engine thread finished.

## Running and operating

`python hestia.py` (one cron entry, Mon-Fri 09:00, when it is time to schedule it). Runtime files live in `hestia_data/` (gitignored):

| Path | What |
|---|---|
| `flags/hestia_active.flag` | exists while running; removing it ends everything gracefully |
| `flags/<engine>_command.flag` | `EXIT` (liquidate and re-arm; the flag clears once flat), `KILL` (stop the engine, keep the position; persists across restarts until cleared), `DISABLE` (do not start the engine) |
| `state/` | engine decision state, `ledger.json`, `requests_<date>.jsonl` (the journal), `<engine>_sizing.json` (live-read sizing override; cannot raise the unit cap) |
| `cache/` | per-token intraday 1-minute cache and private backfill files (Hestia never writes the pipeline's files) |
| `trades/<engine>_trades.csv` | closed trades in Prometheus's 26-column format |
| `angel_session.lock` | which process owns the Angel One login; any other login site should call `session_lock.refuse_if_held` first |

After a restart the persisted ledger is checked against the broker's position book, and the broker wins for live engines. A request that was in flight when the previous process died is **in doubt**: never re-sent, reported `UNCONFIRMED`, and a critical alert asks for the broker to be checked.

## Which machine may trade

Nothing is enabled in the committed `hestia_config.py`. The machine allowed to trade (Delos) has a gitignored `hestia_local.py` naming the engines to enable and their fields; anywhere else `python hestia.py` exits before it logs in, because the session lock and pid checks are per machine and cannot see another machine's process. `python hestia.py --check` prints the no-login start report.

## Adding an engine

An `EngineEntry` in `hestia_config.ENGINES` (instrument, `package.module:callable` factory, paper or live, lots per unit, sizing, unit cap) plus the engine package implementing `interface.Engine` against `EngineContext`. No host, flag or report code changes. A new engine is tested against the fake Hestia first, then replayed against recorded days.

## Tests

`python -m pytest tests/` (the Hestia tests are `tests/test_hestia_*.py`; the whole-host tests run a full session in seconds on a simulated clock). Nothing in them can reach a broker; a test asserts that `generateSession` appears in exactly one place (`real_login` in `hestia.py`) and never on import.
