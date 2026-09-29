# Hestia P6: Hestia live with Prometheus only, cutover with rollback ready

**Status: started 2026-09-28.** Phase P6 of `plans/selene-production.md` §10. Gate: clean sessions and a roll, QC report. The rest of this file is the preparation done offline, what still cannot be verified without the broker, the proposed staging, and the runbook (every step that reaches Delos or the broker needs the user's explicit approval at the time; nothing below is authorised by being written here).

## 1. Where things stand

- Prometheus is **flat and stopped** (stopped out on trade 51 at 21:50 on 2026-09-28, killed by the user at 21:55). The standalone state is `idle`, trade counter 51. **A flat account is the cheapest moment to cut over**: no position to migrate.
- The engine is replay-verified (71 of 71 live decisions over eight sessions, faults injected, chained days; `plans/hestia-p5-prometheus-engine.md`). Roll flows and the real broker order path are **not** verifiable offline.
- **Hestia has never logged in to Angel One.** `AngelBrokerPort`, `BrokerGateway`, `LiveData` and the order-update feed are tested against `tests/smartapi_double.py`, a scripted double. The first real login is the first time any of it meets the real API's actual response shapes.

## 2. Done offline in this slice

- `hestia_core/core.py`: **no request is admitted at or after the session close** (`LIMIT_REFUSED`, "market closed"), closing the P5.4 gap (production's `TestPlaceOrderMarketHoursGuard`). The engine separately declines the bar that completes at the close, stops deciding at or after the close, and on a closed-market refusal gives up for the night instead of retrying (found in review: a stop through its level would otherwise be re-sent every half second until `Stop`). A refused Rule 7 re-entry (unit cap, roll window, margin) now falls back to closing the old side only.
- **Nothing is enabled in committed config, and the machine that may trade says so itself.** `hestia_config.py` reads an optional gitignored `hestia_local.py` (Delos only) that names the engines to enable and their fields (`paper`, `static_units`, `unit_cap`) and the Slack switch. A `python hestia.py` on the laptop therefore exits before any login; the session lock and pid checks are per machine and could not see Delos's process, so this is the guard against evicting Delos's order capability (AB1007). `--check` reports whether the override is present.
- `python hestia.py --check` (`hestia_core/preflight.py`): a no-login, no-network, write-nothing start report: engines enabled and building, unit cap, credential columns (names only), holiday list horizon, front and next contract files (stale or short history: fail for the front, warn for the next), saved engine state, command flags, host flag, the standalone pid, the session lock. Safe beside a live process.
- `python -m prometheus_engine.seed_state [--write]`: standalone `prometheus_state.csv` and `trade_counter.txt` become Hestia's saved engine state, so trade ids continue at 52. **Cutover is supported flat**; an open position also converts (levels verbatim, a booked lot's P&L recomputed from its prices, CRUDEOILM lot size 10) but that path is best effort and Hestia's ledger from the broker wins if they disagree. Refuses while the standalone process is alive and refuses to overwrite an existing state without `--force`.
- `hestia_core/slack_bridge.py` and `slack_listener.py`: the Slack buttons (Exit, Kill, Disable, Clear, Start, sizing override) write the standalone files today and Hestia's files when `hestia_config.SLACK_PROMETHEUS_VIA_HESTIA` is True; the sizing payload is translated (`lot_calc`/`lot_count` to `dynamic`/`static_units`); Start launches `hestia.py`; the instrument override is refused under Hestia (the engine is bound to CRUDEOILM). The switch is False in the commit, so deploying it changes nothing.

## 3. What cannot be checked without the broker, and how P6 covers it

| Risk | Why it is open | Covered by |
|---|---|---|
| Login, feed token, WebSocket subscribe, order-update feed | Never run | Stage A |
| Candle fetch, resample, seed, next-contract history (the preflight warns: the Nov-2026 file has only 2026-09-22 onward, under the 18-day seed) | Real response shapes. Checked in the code: a short-history contract does **not** fail the start; `LiveData._backfill_if_needed` fetches the older days from the broker into a private file (one-off broker candle calls, AB1021 exposure) and the seed proceeds, so the WARN stands | Stage A: the log must show `backfilling CRUDEOILM19NOV26FUT ...` and both contracts seeded |
| LTP, margin (`rmsLimit`) and position-book parsing | Scripted double only | Stage A |
| **Order placement and fill confirmation for MCX** (params, quantity units, slicing at the freeze quantity, order-book read) | Cannot be exercised without placing an order | Stage B (1 unit, watched) |
| Roll | Cannot occur before about 2026-10-12/13 | Stage C |

Extra checks to do on Delos, read-only, before Stage A: the **slack_listener systemd unit** (`ExecStart` and `WorkingDirectory`, so the repo-root imports `hestia_config` and `hestia_core.slack_bridge` are known to resolve under the service; the listener's import is unit-tested with Slack stubbed, both with the switch off and on) and the **crontab** (does anything else still log in to this Angel One account? Leto at 09:15 on weekdays, and the 23:56 data downloader; neither calls `session_lock.refuse_if_held`, so the guard against a second login is Hestia's own lock and pid check only) and the Python environment (`SmartApi`, `pyotp`, `pandas`).

## 4. Proposed staging (the user decides; see section 6)

- **Stage A: one paper session.** `prometheus` enabled with `paper=True` in `hestia_config.ENGINES`; standalone Prometheus unscheduled. Real login, real data and feed, real margin and position reads, the whole start-to-teardown path; the engine's orders go to the paper broker, so **no order reaches Angel One**. Cost: that day's signals are not traded (a flat market position, so nothing is lost but the day's opportunity). Gate: the log shows both contracts seeded with at least 18 days, bars matching the chart the way the standalone did on 2026-09-04, no unexplained alert, a clean teardown and a Slack session report.
- **Stage B: live at 1 unit** (`paper=False`, `static_units=1`, unit cap 10). The first real orders; watched by the user through at least one entry and one exit, ideally a flip. Gate: order, fill and ledger agree with the broker terminal after each event; the trade row matches the standalone format.
- **Stage C: 5 units, through the first roll** (about 2026-10-12/13; roll eve the evening before). Gate: the roll behaves as `roll_policy` says, the QC report is clean.

## 5. Runbook

Each numbered step that reaches Delos needs its own approval, stated with host, path and read or write.

**Before Stage A (once)**
1. Local: merge this slice; `python -m pytest tests/` green; commit and push.
2. Delos, read-only: `crontab -l`, `python -c` import check of `SmartApi`/`pyotp`, `git log -1`, and the presence of `user_credentials.csv`. Report back before anything is changed.
3. Delos, write: `git pull`; then `python hestia.py --check` (read-only) and review every warn.
4. Delos, write: create the gitignored `hestia_local.py` on Delos only (`ENGINES = {'prometheus': dict(enabled=True, paper=True, static_units=1, unit_cap=10)}`; the units per section 6). Nothing about it is committed.
5. Delos, write: `python -m prometheus_engine.seed_state` (dry run) then `--write`; delete any stale `prometheus_command.flag` in **both** `prometheus_production/data/` and `hestia_data/flags/`.
6. Delos, write: comment the standalone Prometheus cron line; add `00 9 * * 1-5 cd /home/parijnan/scripts/algo-trading-lab && /home/parijnan/anaconda3/bin/python hestia.py >> logs/hestia_cron_$(date +\%Y\%m\%d).out 2>&1`. **The `%` must be backslash-escaped** (`\%Y\%m\%d`), matching every other line in this crontab — cron treats an unescaped `%` as a newline into the command's stdin, silently truncating it, so the job never runs and nothing appears in any log (found live, 2026-09-28, by installing it unescaped once; fixed before the next 09:00). Verify with `crontab -l | grep hestia.py` and compare byte for byte against the downloader line's escaping. The redirect goes to a **separate** file: `hestia.py` already writes `logs/hestia_YYYYMMDD.log` itself (file plus stream handler), so appending stdout to that file would double every line and break any log parser. Do this **after** the evening, not during a session.
7. Delos, write: put `SLACK_PROMETHEUS_VIA_HESTIA = True` in `hestia_local.py`, restart the `slack_listener` service.

**Each session**: watch `#tradebot-updates` (start, data seeded, running), the Hestia log, `python hestia.py --check` beforehand; after the close, the session report and a QC read. A `hestia-qc` skill (Hestia log, ledger, engine trade file) replaces `prometheus-qc` as the primary check and is written in this phase.

**Rollback (any time, any stage)**: with the position flat (use the Slack Exit button first; if the position cannot be closed, reconstruct the standalone state by hand per the state-reconstruction rule): remove `hestia_active.flag` (graceful stop) or Kill; comment the Hestia cron line; uncomment the standalone line; remove the Slack switch from `hestia_local.py` (or set it False) and restart the listener; disable the engine in `hestia_local.py`; copy `trade_counter` forward if Hestia took trades (the standalone reads `trade_counter.txt`; write the current counter from Hestia's state). The standalone entry point is untouched in the repo.

**Restarting Hestia**: no position-timing condition — user's call, 2026-09-29: restart whenever, including mid-position or mid-transition (a pending flip, an unconfirmed exit, a roll in progress). The restart-recovery path is trusted to handle it: bootstrap takes the broker's ledger as truth for a live engine, and an UNCONFIRMED request at the time of the restart is settled by reading that order at the broker, never blindly re-sent. `hestia_data/state/prometheus_state.json` and the ledger are still worth a read afterward, to confirm the resume landed as expected — not as a precondition before restarting.

## 6. Decisions for the user

1. **Staging.** A then B then C (recommended: Stage A is the only way to check Hestia's real-API parsing without risking an order; Stage B is the only way to check the order path with a small loss cap), or straight to B.
2. **Units and cap.** Static units (1 for Stage B, 5 after) and the hard unit cap (recommend 10; history never exceeded 5).
3. **When.** Cutover happens between sessions, with Prometheus flat, after steps 1 to 7.
4. **How the engine is enabled** (recommended above: the Delos-only `hestia_local.py`, so no committed config can make the laptop log in).
5. **Leto and the data downloader.** Confirm whether Leto still logs in on this account at 09:15 on weekdays. Standalone Prometheus has run alongside the current crontab since 2026-09-15, so Hestia's exposure is the same unless its login pattern differs; the read-only crontab check in section 3 settles it.

Also for later: 09-28's prices arrive through the user's own 02:00 `datasync`, after which `python -m prometheus_engine.replay_check` adds the second recorded stop (trade 51) to Tier 1; run it before Stage B.
