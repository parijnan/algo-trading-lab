# Plan: Prometheus DRY_RUN → Live Cutover (1 unit)

**Status (2026-09-14): scheduled for tomorrow, execution on the user's explicit go-ahead.** User has decided to go live with `STATIC_UNITS=1`. Claude has Delos access and will perform the actual edits once given the go-ahead — but each Delos-reaching command still needs fresh explicit approval at execution time, same as every other Delos action in this project (`CLAUDE.md`'s standing protocol), regardless of this plan existing.

---

## 0. Pre-flight check — the DRY_RUN reversal condition

`prometheus_configs.py`'s own `DRY_RUN` line carries a self-imposed gate from a real prior incident, not just a generic caution:

**2026-08-31 incident (real capital):** a trend-flip exit's SELL order failed at the broker (`placeOrderFullResponse` returned `data.orderid=None`, no exception raised) — `_execute_exit_lot` had no check for this (unlike `_execute_entry`, which already did). Fill resolution timed out on both WS and REST, and the old code then did `if fill_price is None: fill_price = ltp` — silently fabricating a fill from the current market price, marking both lots "booked," finalizing the trade, and returning status to `watching`. The real 2-lot long position stayed fully open at the broker, completely unmonitored (Prometheus believed it was flat, so no SL/target checks ran at all), for ~28 minutes until caught manually via the broker terminal.

**Fixed same day, commit `8b7bc5b`** ("never mark a lot closed without a confirmed fill") — a three-layer confirmation check, propagated through Rule 7 too. Verified at the time via two targeted reproductions covering the actual failure mode: order-placement failure, fill-timeout failure, `execute_exit_all`'s own failure-propagation, and the genuine-success path all checked at the `_execute_exit_lot`/`_execute_exit_all` level; plus confirmation that `_teardown()` correctly refuses to clear status over an unconfirmed exit and posts a loud, distinct Slack alert instead of the normal quiet one.

The comment's own explicit condition: *"Only flip back to False after the fix above has held up under a fresh DRY_RUN pass."*

**Where this stands as of 2026-09-14:** DRY_RUN has run continuously and cleanly since the fix — the trade history already reviewed this session (trades #9–#18) includes multiple `trend_flip` exits, all resolving normally. Reads as satisfied. **One real gap, not resolved by this plan:** there's no permanent automated regression test for this specific failure scenario in `tests/` — the verification was a one-time targeted check at commit time, not a standing guard against a future regression to this code path. Flagged to the user 2026-09-14; proceeding on their explicit call, not blocked on adding that test first.

---

## 1. Why a naive `DRY_RUN=False` flip is dangerous with an open position

`DRY_RUN` is a single global constant, re-read fresh from config on every call — it is **not** tracked per-position in `prometheus_state.csv`. `place_order()` (`prometheus_functions.py:1233`) branches purely on the *current* value of `dry_run` at call time: `True` returns a synthetic `PAPER_...` ID with no broker call at all; `False` places a real market order via `obj.placeOrderFullResponse(...)`. Nothing checks whether the position currently being acted on was ever actually opened at the broker.

If `DRY_RUN` flips to `False` while a paper position is open, the next thing that happens to it (a target hit, an SL, a trend-flip) places a **real market order** to "close" a position that was never really bought — MCX allows shorting futures freely, so this wouldn't get rejected, it would silently open a real, unintended position with real margin at risk, sized and priced off a purely simulated entry.

**Confirmed live and current** (read 2026-09-14 via Delos): `prometheus_state.csv` shows an open paper position — `status=in_trade`, `direction=bullish`, entry 2026-09-11T22:30, both lots `open`. Whether this specific position is still open, replaced by a fresh one (if tonight's evening session trend-flips), or already closed by cutover time, the same rule applies below.

---

## 2. The cutover procedure

**Hard constraint: steps 1–5 below only happen while the Prometheus process is NOT running** — mid-tick this would race a live `save_state()` write. Safe windows: tonight after logoff, or tomorrow morning before the 09:00 cron start. Not safe: any time the evening session (17:00 onward today) or the next live session is actually running.

**Two of the edits below are asymmetric in where they happen, per the user's own correction (2026-09-14): `prometheus_state.csv` is gitignored runtime data, edited directly on Delos — but `prometheus_configs.py` is a tracked source file, so `DRY_RUN` gets flipped locally (this checkout), committed, and pushed, then pulled on Delos — never hand-edited directly on the server.** This matches how every other code change in this project already reaches Delos; the state-file reset is the one genuine exception, because it isn't a code change at all.

1. **Read the current live state** (Delos, read-only): `cat prometheus_production/data/prometheus_state.csv`. Also confirm the process isn't running (`ps aux | grep prometheus`, absence of `prometheus_active.flag`/`prometheus.pid`) before touching anything.
2. **If `status == 'in_trade'`**: reset the row to a clean flat state, directly on Delos (this file doesn't exist in git). Correct target status is `idle`, not `watching` — that's exactly what `prometheus.py`'s own clean teardown writes when flat (`prometheus.py:1868`); `watching` only gets set at the *next* `_setup()` once a new session begins (`prometheus.py:1751`), so writing `idle` now makes the reset indistinguishable from "it happened to close naturally right before shutdown." Blank every trade-identity and trade-tracking field; `contract_expiry`/`symbol`/`token` are safe to blank too since those are only read when `status == 'in_trade'` — a flat start resolves the current effective contract fresh, not from stale state. `last_known_ltp`/`last_processed_boundary` are just restart-recovery aids, harmless to blank, no need to preserve them specifically.

   ```
   status,direction,units,entry_price,recalibration_basis_price,entry_ts,signal_ts,signal_close,contract_expiry,symbol,token,sl_price,lot1_target,lot1_lots,lot1_status,lot1_exit_price,lot1_exit_ts,lot1_exit_reason,lot2_target,lot2_target_source,lot2_lots,lot2_status,lot2_exit_price,lot2_exit_ts,lot2_exit_reason,last_known_ltp,last_processed_boundary,last_updated
   idle,,,,,,,,,,,,,,,,,,,,,,,,,,,<timestamp of the edit>
   ```

   **If `status` is already `idle` or `watching`** at check time (position closed naturally with no new entry), skip this step entirely — nothing to reset.
3. **Flip `DRY_RUN = False` locally**, in this checkout's `prometheus_production/prometheus_configs.py`, replacing the current `True` and its dated comment (keep the historical comment as a record, add a new line noting the go-live decision and this plan). Confirm sizing is already correct in the same pass — `DEFAULT_STATIC_UNITS = 1` and `DEFAULT_DYNAMIC_SIZING = False` were already the live config as of this session (no change needed), but re-check fresh rather than assume, since this file drifts.
4. **Commit and push** — the user's go-ahead to execute this cutover is the explicit authorization for this specific commit, matching the rest of this plan's procedure; no separate mid-procedure confirmation needed for this one push.
5. **Pull on Delos** (`git pull`) to bring the flipped config onto the server — the running process (if any survived to this point, which it shouldn't per the hard constraint above) would not pick this up until its next restart either way, since Python reads config at import time.
6. **Let the next session start normally** (09:00 cron tomorrow, or a manual restart if the reset happens well before then) — first entry going forward will be a genuine fresh one with a real fill behind it.

---

## 3. Post-cutover verification — this is the first time this code has placed a real order since the 8b7bc5b fix

Don't treat the flag flip as the finish line. Once the first live session is running:

- **Immediately after start**: confirm Slack messages and log lines have dropped the `[PAPER]` prefix, confirm login/setup completed cleanly, confirm `prometheus_state.csv` shows `watching` (not a leftover `idle`/stale row).
- **On the first real entry**: confirm the order ID logged is a genuine broker order ID, not a `PAPER_...` string — this is the simplest possible smoke test that `DRY_RUN=False` actually took effect.
- **On the first real exit (especially a trend-flip exit — the exact mechanism that failed on 2026-08-31)**: this is the highest-value moment to actually watch closely rather than assume the fix holds. Confirm the exit fill is genuine (real price, real order ID, not a fabricated LTP-based fill), confirm both lots only get marked closed with real confirmation, confirm Rule 7 (if it's a flip) resolves the combined order correctly.
- **Run a `prometheus-qc` pass** shortly after the first live trade (entry or exit) completes — same checklist as every other QC this session, but this is the first time it's being run against genuinely live data instead of `[PAPER]`-tagged paper trades, so treat it as a real first-live-trade audit, not a routine check.
- **Watch margin sufficiency in practice** — `_check_margin_sufficient()` now uses the dynamic per-trade margin calculation (§26, `project_prometheus_production` memory); confirm the real capital on hand comfortably covers what that calculation actually requires at 1 unit, not just that it passes.

---

## 4. Explicitly not in scope for this cutover

- No sizing change beyond `STATIC_UNITS=1` — dynamic sizing stays off.
- No changes to the Fyers data-source work (`plans/fyers-mcx-data-integration.md`) — unrelated, running in parallel, still blocked on account verification.
- No new automated regression test for the 8b7bc5b failure mode — flagged as a gap in §0, not resolved here; worth a separate follow-up if it's ever wanted before this matters again (e.g., before scaling units up).
