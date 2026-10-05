# hestia_core

Hestia is the MCX multi-strategy host: one process, one Angel One login, several strategy engines (Prometheus, Selene, Helios, later Typhon) running side by side. Engines decide (entries, exits, stops, the roll); Hestia does the work (orders, fills, retries, reconciliation, data, alerts, teardown). Plans and decisions: `plans/selene-production.md` (design), `plans/hestia-interface-spec.md` (the engine interface, v1.1), `plans/hestia-p1-inventory.md`, `plans/hestia-p4-live-services.md` (this build).

**Status:** built and tested against doubles, and **live on Delos since 2026-09-29** (Prometheus, 1 unit, cap 10, `plans/hestia-p6-cutover.md`), with **Selene and Helios running alongside it, both paper, 1 unit, cap 10** (`plans/hestia-p7-selene-engine.md`, `plans/hestia-p8-helios-engine.md`). `hestia_config.ENGINES` itself has nothing enabled; which machine may trade is a hostname lookup (`hestia_config.TRADING_HOSTS`, or a gitignored per-machine `hestia_local.py` on top of it) — see "Which machine may trade" below — so `hestia.py` on any other machine (the laptop) still exits before it logs in, and cannot evict another process's session.

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
| `flags/hestia_disabled.flag` | a start gate for the whole host (2026-10-05): while it exists `hestia.py`, from cron or the Slack Start button, alerts and exits before logging in; `--check` reports it as a FAIL. Set by the panel's Stop/Disable, removed by its Clear; a running Hestia never reads it |
| `state/` | engine decision state, `ledger.json`, `requests_<date>.jsonl` (the journal), `<engine>_sizing.json` (live-read sizing override; cannot raise the unit cap) |
| `cache/` | per-token intraday 1-minute cache and private backfill files (Hestia never writes the pipeline's files) |
| `trades/<engine>_trades.csv` | closed trades in Prometheus's 26-column format |
| `angel_session.lock` | which process owns the Angel One login; any other login site should call `session_lock.refuse_if_held` first |

After a restart the persisted ledger is checked against the broker's position book, and the broker wins for live engines. A request that was in flight when the previous process died is **in doubt**: never re-sent, reported `UNCONFIRMED`, and a critical alert asks for the broker to be checked.

## Which machine may trade

Nothing is enabled in the committed `hestia_config.py`. The machine allowed to trade (Delos) has a gitignored `hestia_local.py` naming the engines to enable and their fields; anywhere else `python hestia.py` exits before it logs in, because the session lock and pid checks are per machine and cannot see another machine's process. `python hestia.py --check` prints the no-login start report.

## Adding an engine

**Slack wording.** Engine names are capitalised wherever a person reads them (`*Hestia [Selene]*`, the startup line, the session report) through `display.display_name`; logs, file names and the registry keep the lower-case name so greps and paths do not change. The end-of-session report (`reporting.build_session_report`) uses the standalone Prometheus report's layout: a section per engine (instrument, Live or Paper), a block per trade closed this session (entry and exit time and price, reason, points and Rs per unit; an entry from an earlier day carries its date), an Open Position block with unrealised P&L, and a Realized / Unrealized total in Rs per unit; then free cash and, only when present, unconfirmed requests, ledger mismatches and warning-or-worse alert counts. A part-booked position shows only its open lots until the trade closes.

**Fyers candle shadow (Phase 1 of `plans/hestia-fyers-candle-source.md`, built 2026-10-03, off by default).** `hestia_config.CANDLE_SOURCE['mode']` is `'angel'` (nothing changes, no Fyers code runs) or `'shadow'`. In shadow mode Angel One stays the only source any engine sees; `fyers_shadow.ShadowRecorder` asks Fyers for the same one-minute window at every minute tick and records, under `hestia_data/shadow/`, a `polls_<date>.csv` (per poll and side: attempts, whether the just-closed minute was present, how many seconds after the tick, Angel One exhaustion, boundary minute or not) and the closed minutes each source returned with the time each was first seen. It is isolated by construction: its own daemon-thread pool (never joined at exit, never the host's four IO workers), every entry point catches everything, it receives copies of the Angel One frames, it never posts to the kernel or alerts, and a per-token in-flight guard skips a slow Fyers's next minute instead of queueing. `TokenGate` reads `hestia_data/fyers_token.json` (written daily at 06:35 IST by the laptop job) under the same rule as `data_pipeline/fyers_token_refresh.check_token_file` (mode 600, file mtime and `issued_at` both today in IST, before `expires_at`; a test pins the two together), honours the kill-switch flag `hestia_data/flags/fyers_off.flag`, and trips a breaker on an authentication refusal until the token file is replaced. `hestia.py --check` prints the mode and the token state. `research/fyers_mcx_validation/phase1_shadow_report.py --date YYYY-MM-DD` turns a session's files into availability and head-to-head figures and compares Fyers-built Supertrend flips against the boundary lines the engines actually acted on. Enable per host with a `CANDLE_SOURCE` entry in `TRADING_HOSTS` (or `hestia_local.py`); rescue and smart modes arrive with Phases 2 and 3.

**Fyers candle rescue (Phase 2 of `plans/hestia-fyers-candle-source.md`, built 2026-10-05, off unless `CANDLE_SOURCE['mode']='rescue'`).** Everything the shadow records, plus one addition: `candle_fetch.fetch_one_minute_window` takes an optional `fallback`, called once after `rescue_after_attempts` failed Angel One attempts (default 5, i.e. only once the whole burst has failed); `FyersRescue.fetch_window` is that fallback. It asks Fyers for the same window and returns ONLY the closed minutes the engine does not already hold (never overwriting an Angel One minute, zero-volume placeholders dropped), and only when Fyers's answer contains the just-closed minute and that minute has settled for `settle_s` seconds (default 0: by the time the Angel One burst has failed the minute has settled). Fyers's first answer for a minute, about 0.07 s after it closes, is provisional: on 2026-10-05 22% to 82% of first-seen minutes differed from the finalized ones, while finalized Fyers differs from Angel One in only 11% to 22% of minutes; a probe (`research/fyers_mcx_validation/settle_probe.py`, 40 samples per offset) found 60% of snapshots final at +0.1 s, 90% at +0.3 s, 95% at +0.4 s and 100% from +0.8 s; before +0.4 s the wrong values are almost all closes off by a few ticks (worst 0.11%). A frame it returns ends the burst; None, an exception, a closed token gate or a refusal leaves the burst exactly as it was. A rescued window is invisible to the engines (same bar, same Supertrend as a failure-free run: a test pins that), logged (`fyers rescue: ... filled N minute(s) from Fyers after Angel One failed`), recorded in `hestia_data/shadow/rescues_<date>.csv`, counted in the end-of-session report, and shown to the shadow's Angel One record as `rescued`, never as an Angel One frame. The shadow's minute files now keep every version of a minute (a minute is written again when a later poll shows different values), so first-seen and settled values can both be compared.

An `EngineEntry` in `hestia_config.ENGINES` (instrument, `package.module:callable` factory, paper or live, lots per unit, sizing, unit cap) plus the engine package implementing `interface.Engine` against `EngineContext`. No host, flag or report code changes. A new engine is tested against the fake Hestia first, then replayed against recorded days.

## Tests

`python -m pytest tests/` (the Hestia tests are `tests/test_hestia_*.py`; the whole-host tests run a full session in seconds on a simulated clock). Nothing in them can reach a broker; a test asserts that `generateSession` appears in exactly one place (`real_login` in `hestia.py`) and never on import.

**Slack control panel (2026-10-05).** The listener (`slack_listener.py`, a separate service, restart it after a code change and repost the panel) shows one section per engine and one for Hestia, built from `hestia_config.ENGINES`; the logic lives in `slack_bridge.py` so it is tested without Slack (`tests/test_hestia_slack_panel.py`). Every button acts on its OWN flag file only. Per engine: **Exit** writes `EXIT` (refused when Hestia is not running, because the flag would fire unattended at the next start, and when the engine's flag is `KILL`, which a running Hestia would ignore while the EXIT silently replaced the gate), **Kill/Disable** writes `KILL` (it stops a running engine with its position left open, and either way keeps it out at the next start, because the host gates both words), **Clear** removes only that engine's command flag. A killed engine does NOT come back when its flag is cleared: it comes back at the next Hestia start. Hestia: **Start** launches `hestia.py` (refused if the gate is set or it is already running), **Stop/Disable** sets `hestia_disabled.flag` first and then removes `hestia_active.flag` (graceful shutdown, positions left open and unmanaged), **Clear** removes only `hestia_disabled.flag`. The sizing buttons list all four engines and write `<engine>_sizing.json`. The Prometheus-only "Switch Instrument" button was removed. To bring everything back live: Clear the Hestia flag, Clear every engine flag, Start Hestia.

**Session report contents (fixed 2026-10-05).** The report sent at teardown lists every trade whose last exit happened that day, read from each engine's `hestia_data/trades/<engine>_trades.csv` and merged with the trades this process closed (one entry per engine and trade id, the live record winning). Before, it used only trades closed since the process started, so after a mid-session restart earlier trades vanished from it (the 2026-10-05 report said "No trade today" for Prometheus after four closed trades). A trade added to the file by hand is included. An unreadable file contributes nothing and never blocks the report. Tests: `tests/test_hestia_session_report_day.py`.
