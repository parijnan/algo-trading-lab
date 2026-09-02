# Plan: Prometheus Phase 3 — Production Architecture

**Status: DRAFT, first pass (2026-09-01).** Builds on `prometheus_production/` (Phase 2's build — see `plans/prometheus-phase2-production.md`), not a rewrite. Most of the process architecture — contract resolution mechanics, ST seeding, resilient polling, order execution, fill verification, guardian check, circuit breaker, sizing, state persistence pattern, Slack reporting — carries over. What's genuinely new is a single problem Phase 2 never had to solve: **a position can now be open at the moment a contract needs to roll.** Everything below either reuses Phase 2 unchanged, simplifies it (no EOD flatten), or exists specifically to handle that one new problem.

**The 2.0-vs-2.5 multiplier decision is still pending** (`prometheus_backtest/README.md`'s Phase 3 section) — this plan is written to be agnostic to it; whichever wins just fills in `ST_MULTIPLIER`/`SL_PCT`/`TARGET1_PCT`/`TARGET2_FLAT_PCT` in `configs_p3.py`'s production counterpart. Nothing below depends on which candidate is chosen.

**Several sections are marked OPEN — genuine decisions, not filled in yet.** Don't treat this document as ready to build from until those are resolved.

---

## 0. What reuses unchanged from `prometheus_production/`

- `resolve_effective_contract()` — the tender-margin-early-roll mechanics (`TENDER_ROLL_TRADING_DAYS`, `_count_trading_days_inclusive`) are reused **as the underlying primitive**, just called differently (§3) — no changes to the function itself.
- `seed_st15`, `compute_st`, `backfill_contract_if_needed`, `_tail_read_contract_csv` — ST seeding/computation, including the "no cross-contract stitching" rule (each contract gets its own Supertrend from scratch).
- `fetch_one_minute_window`, the inner/outer retry queue — resilient 1-min polling, unchanged.
- `place_order`, `get_fill_price_and_qty`, `OrderFillWatcher` — order execution and the **fill-confirmation invariant** from the 2026-08-31 incident (never mark a lot closed without a genuine confirmed fill). This invariant is load-bearing for the rollover flatten too (§5) — a rollover exit that can't confirm closed must behave exactly like any other unconfirmed exit (leave state as-is, retry, alert loudly), not a special case.
- `check_no_active_strategies` (guardian), `prometheus_command.flag` (own circuit breaker), `_calculate_units`/`_check_margin_sufficient` (sizing) — unchanged.
- State persistence pattern (atomic tmp-rename CSV write), `trade_counter.txt`, the cumulative trade log's general shape — reused, with new fields for rolled trades (§7, open).
- Slack reporting pattern (`_send_session_report`, periodic trade updates) — unchanged in mechanism; message copy needs updates wherever it currently assumes EOD-flat (§1).

---

## 1. What's simplified: no EOD flatten

- Exit priority drops the EOD-square-off tier entirely — SL → lot1 target → lot2 target → trend_flip only, matching `configs_p3.py`'s calibrated design (backtest has no EOD exit; a production EOD tier would silently diverge from what was calibrated).
- `MIN_ENTRY_TIME` still applies (skip thin opening liquidity). `MAX_ENTRY_BEFORE_CLOSE_MIN` / `LAST_ENTRY_TIME` are **dropped** — `configs_p3.py` never gated entries near close (positional, nothing to "hold until"), and production must match or it trades a different signal than the one calibrated. See §3 for why this isn't quite unconditional — entries still need sequencing around the rollover event specifically.
- **`_teardown()`'s auto-flatten inverts — a default change, not a tweak.** Phase 2 exits any open position at session end, because "always flat by EOD" is the whole invariant the strategy is built on. Phase 3 is the opposite: the position is *expected* to still be open at session end on most days. Teardown's normal path becomes "leave `in_trade`, stop the feed, save state, exit cleanly" — not "exit and go idle." The post-2026-08-31 `exit_confirmed` / "don't force `status='idle'` over an unconfirmed exit" safety logic is preserved unchanged underneath this — it still governs the rare cases Phase 3 *does* need to exit before teardown (SL/target/trend_flip firing right as the loop ends, or a rollover flatten that's still in flight, §5).
- Session-report and Slack copy that currently reads as "stopped, flat" needs auditing — anywhere it implies the day always ends flat needs to instead report "stopped, position left open" as the normal case, not an edge case.

---

## 2. The new problem: a position can span a contract roll

`resolve_effective_contract()` is called once per session, at `_setup()`, and never re-derived mid-session (`prometheus_functions.py:206-260`'s own docstring: "resolved once ... for the ENTIRE session"). Phase 2 never carries a position past EOD, so whatever this function returns each morning is trivially correct — there's never an open position that could disagree with it. Phase 3 can hold a position for days to weeks (`avg_hold_hours` in the raw backtest ranges 17.9–51.0 across multipliers, max observed 103–145h), so the day the effective contract flips can land in the middle of an open trade.

**Concrete latent bug this exposes if Phase 2's code is reused unchanged**: `_get_ltp()` (`prometheus.py:724-729`) reads the WebSocket feed keyed on `self._contract['token']` (freshly resolved *this session*) but falls back to REST keyed on `self.state.symbol`/`self.state.token` (persisted, possibly the *old* contract) if the feed is down. On a roll day with an open position, **which contract drives the SL/target comparison depends on whether the socket happens to be connected at that tick** — and `state.last_known_ltp` gets written from whichever one answered, contaminating the running trade log and session-report P&L regardless. Meanwhile `_execute_exit_lot` correctly places orders on `self.state.symbol`/`token` — so the actual failure mode isn't "the wrong contract gets traded," it's worse: **a wrong-contract price trips an SL/target trigger, and the exit then fires correctly on the real position, at a price level that meant nothing.** `_setup()`'s subscription set (`self.feed.subscribe_options([self._contract['token']], ...)`) has the same root issue — it only ever subscribes the freshly-resolved contract's feed, never the persisted `state.token`'s, on a resume.

**Fix, stated as an invariant**: **while `status == 'in_trade'`, every price read and every order keys off `state.token`/`state.symbol`; `self._contract` is consulted only while `watching`/entering, and to detect an upcoming roll (§3).** `_execute_entry` already follows this rule. Two call sites need to change to match it: `_get_ltp()`'s WS branch (read `state.token`, not `self._contract['token']`), and `_setup()`'s subscription set (must *also* subscribe `state.token`'s feed when resuming `in_trade`, since it may differ from the freshly-resolved `self._contract`).

---

## 3. Rollover trigger — proactive lookahead, not reactive discovery

**Mechanism**: each evening, check whether tomorrow's `resolve_effective_contract()` would return a *different* contract than today's `self._contract`. If so, trigger the rollover sequence (§5) tonight, at `ROLLOVER_TIME` — so that by the time tomorrow's fresh `_setup()` runs, state and the freshly-resolved contract already agree.

```python
tomorrow_contract = resolve_effective_contract(SYMBOL, today=next_trading_day)
if tomorrow_contract['token'] != self._contract['token']:
    # trigger rollover tonight
```

This reuses `resolve_effective_contract()` completely unchanged — just called with a forward-dated `today`. No new trading-day-counting logic.

- **"Tomorrow" must be the next *trading* day, not literally `today + 1`.** `resolve_effective_contract`'s own `_count_trading_days_inclusive` already skips weekends when counting days to expiry, but a naive `today + timedelta(days=1)` lookahead only happens to work correctly across a weekend by accident of that separate function's behavior — it should be derived explicitly (walk forward from today using `mcx_holidays.csv`, same source `_count_trading_days_inclusive` already reads, until landing on a day that isn't a full closure) rather than relied on implicitly.
- **This gets the user's "roll a day before the extra margin requirement kicks in" for free, without touching `TENDER_ROLL_TRADING_DAYS=5` at all.** `TENDER_ROLL_TRADING_DAYS` still means exactly what it did in Phase 2 — the last trading day you're allowed to still be on the old contract. By evaluating *tonight* whether *tomorrow* would already be past that boundary, the transition completes before the margin-affected day ever begins. (Worth flagging honestly: "5 trading days = when tender margin kicks in" is inherited from Phase 2's plan as a stated decision, not something independently verified against MCX's own rules here — if that assumption is wrong, this whole early-buffer discussion inherits the error.)
- **Entry-ordering rule, new**: the rollover check runs before entry-signal evaluation on every loop tick. On an evening where a roll is about to trigger, suppress fresh and rule-7 entries once `now >= ROLLOVER_TIME` — otherwise a fresh position could open seconds before being flattened straight into a roll.
- `ROLLOVER_TIME` — new config constant, derived the same way Phase 2 derives `EOD_SQUAREOFF_TIME` (`CLOSING_TIME` minus a buffer), same DST-hand-toggle caveat carried over. Under the current DST-in-force `CLOSING_TIME='23:30'` this computes to 23:15, matching what was asked for — but it should be its own named constant, not a reuse of Phase 2's `EOD_SQUAREOFF_TIME` (which doesn't exist as a concept in Phase 3 at all).

---

## 4. Startup reconciliation — the safety net for a missed rollover [OPEN]

`ROLLOVER_TIME` firing correctly depends on the process being alive at 23:15 on that specific evening. It might not be: an MCX holiday, an earlier crash, `KILL` having dropped control, `DISABLE` being set. If the rollover is missed, the next morning's `_setup()` walks straight into §2's problem with an open position on a contract `resolve_effective_contract()` no longer returns.

**`_setup()` needs an explicit check**: if `status == 'in_trade'` and `state.token != self._contract['token']`, that's a missed roll. Two options, not yet decided:

- **(a) Roll immediately at today's open** — run the full rollover sequence (§5) as part of `_setup()` itself, before arming. Keeps the system self-healing, but means the recalibration math (§6) now has to run against whatever price the new contract opened at, likely a worse moment than a deliberate 23:15 roll.
- **(b) Refuse to arm, alert loudly, wait for manual intervention** — safer in the sense of never taking an automated action nobody watched happen, but leaves a real position sitting on an effectively-delisted-from-tracking contract until a human acts.

**Recommend (a) with a loud Slack alert either way** — consistent with this codebase's existing bias (the fill-confirmation invariant, the "leave state as `in_trade`, alert, let a restart resume monitoring" pattern in `_teardown()`) toward keeping the system self-recovering rather than halting, provided every automated recovery step is loudly reported. Flag this for confirmation before building — it's a genuine judgment call, not a ported pattern.

---

## 5. Rollover execution mechanics

1. Flatten the current position (if any) on the old contract, via the existing exit-order/fill-verification machinery (`_execute_exit_lot`/`_execute_exit_all`), new exit reason `'rollover'`. Fully subject to the fill-confirmation invariant — an unconfirmed rollover exit must leave state as `in_trade` on the old contract and retry, exactly like any other unconfirmed exit, not skip ahead to opening the new position regardless.
2. **Re-seed ST and backfill the new contract's history tonight, at rollover time — not deferred to tomorrow's `_setup()`.** The roll now happens the evening *before* the new contract becomes effective, so `seed_st15`/`backfill_contract_if_needed` need to run for the new contract as part of this sequence, not wait for a `_setup()` that won't run until tomorrow.
3. Recalibrate SL/targets for the carried position, if one is being reopened (§6 — open).
4. Reopen (or don't — §6) on the new contract, reusing the existing entry order/fill path.
5. Update `self._contract` to the new contract; persist state.

---

## 6. SL/target recalibration — NOT YET SETTLED, two checks needed first [OPEN]

**Proposed method** (as described): look up what the *new* contract was trading at, at the *same historical timestamp* the original entry happened on the *old* contract. Use that price as the basis, and reapply the calibrated `SL_PCT`/`TARGET1_PCT`/`TARGET2_FLAT_PCT` formulas against it, exactly as if it had been the entry price all along.

**What this assumes, precisely**: if the calendar spread between the two contracts (old price minus new price) has stayed roughly constant between the original entry date and the rollover date, this exactly preserves the trade's progress-so-far — the new contract's current price minus this historical basis equals the same point-move the old contract actually experienced. If the spread has drifted, the recalibrated distance-to-target is off by however much it drifted, silently.

**Two things need checking before this is trusted, both computable from data already on disk — neither requires a broker call:**

1. **How often does a position actually straddle a roll?** Cross each candidate's `bespoke_trade_summary.csv` entry/exit timestamps against the per-contract trading-day calendar (already built once this session for the Phase 2 plan's drop-analysis re-verification). If straddling rolls are rare (a couple of instances across the whole backtest), a simple scheme is fine even if imperfect; if it's common, the method matters a lot more. The raw backtest's `avg_hold_hours` (17.9 at mult 2.5, up to 51.0 at 5.5, maxima 103–145h) suggests *rare but real* — but that's the *unmanaged* hold time; the calibrated 2-lot versions exit earlier on average and need their own check.
2. **Basis drift vs. target distance, over realistic hold windows — tried this against a real instance, blocked.** `TENDER_ROLL_TRADING_DAYS=5` off the Aug-19 expiry puts a real rollover at **2026-08-12, 23:15** (confirmed against `_count_trading_days_inclusive` and the real MCX holiday calendar), and both mult 2.0 and mult 2.5's bespoke backtests had a position open at exactly that moment (trade 344 / trade 264, both bearish, both lots still open). Tried computing the basis directly from this instance and hit a data problem, not a market one: `2026-09-21_futures.csv`'s price is an **exact clone** of whichever contract was genuinely front-month at each point in its backfilled history — checked against August (its own eventual predecessor), July, and June, all three showing **zero spread across the entire overlap, to the last paisa, over ~18-19k one-minute bars each**. Ruled out our own code as the cause (`download_futures_contract` correctly requests the contract's own token from AngelOne); the clone is coming from the broker's historical response for a token that far from expiry. Confirmed with the user this reads as a broker/historical-API artifact, not a genuine MCX quoting convention — i.e., a *live* LTP poll for the next contract at the time would likely be genuine; it's specifically a *retroactive historical* request for an inactive token that comes back corrupted. **This means check #2 cannot be run against current historical data at all** — the "overlapping 1-min data" once assumed usable for this is exactly the corrupted stretch. It can only be redone once genuine live-captured history exists (see the new prerequisite below).

**New prerequisite this surfaced, specific to the historical-basis method — now partially built (2026-09-02).** At the time this was written, nothing in the pipeline ever captured live price data for a contract before it became front-month: `select_front_month_contracts()` (used by both `data_downloader_mcx.py`'s offline backfill and `mcx_live_downloader.py`'s live poller) returned exactly one row per underlying, and `resolve_effective_contract()` resolved to that same single contract — so the only way to learn what the next contract was trading at some point in the past was a historical backfill request after the fact, the exact request type just shown to come back corrupted.

`data_downloader_mcx.py` now also tracks the **next-month** contract for every enabled underlying, including CRUDEOIL/CRUDEOILM (`select_next_month_contracts()` — second-nearest unexpired expiry, downloaded via the same `download_futures_contract()` path, forward-only from first sight, same as front-month; initially scoped to just the traded pair via a `track_next_month` config column, simplified 2026-09-02 to apply uniformly to all 22 enabled underlyings instead, alongside a wider redesign — see below). Run nightly (`run_mcx_downloader.sh`, weekdays 23:56 IST, right after the session closes), this means genuine live-captured history now accumulates on the next contract for however long it sits at next-month before rolling — which is exactly what check #2 above needs, and directly closes the "no genuine live-captured history exists" half of this prerequisite.

What this does *not* yet close: the history only starts accumulating from 2026-09-02 forward (no backfill, by design — see the LOOKBACK_DAYS section of `data_pipeline/README.md`), so check #2 still can't be run against the 2026-08-12 rollover instance above — that gap in the data predates this fix and can't be recovered. It also doesn't retroactively fix the already-corrupted `2026-09-21_futures.csv`/etc. on-disk files described above; those stay corrupted (`data_pipeline/README.md`'s "Known limitation, not yet cleaned up"). Whether the accumulated next-month history by *some future* rollover is enough — or whether "started early enough (ideally from first listing)" needs a deeper fix — remains open; re-evaluate once a rollover happens after 2026-09-02 with a real open position at the time.

**Two alternatives worth weighing once those checks are in:**

- **Reset-to-now**: treat the rollover as a fresh synthetic entry on the new contract, basis = new contract's *current* price at rollover time, discarding whatever progress the trade had already made. No basis-drift risk, no historical-price data-quality risk (the new contract's price months before its own front-month tenure may be thin/wide/unreliable — a real concern for the proposed method's historical lookup specifically). Costs: a trade that was most of the way to target on the old contract effectively restarts.
- **Flatten and don't reopen** — go to `watching` on the new contract and wait for a genuine fresh ST_15 flip, rather than carrying the position across at all. This isn't just the simplest option; it sidesteps a *second*, separate problem the other two methods don't address: the new contract's ST series is being **re-seeded from scratch** (§0/§5 — no cross-contract stitching), so its freshly-computed trend state might already *disagree* with the carried position's direction. `_handle_new_15m_bar` only exits on a `trend_flip` *transition* — a freshly-seeded series that opens already bearish produces no flip event, so a carried long would sit against its own newly-computed regime until the next flip fires, with no mechanism to notice the disagreement in the meantime. Carrying a position across the roll means solving *this* coherence problem in addition to the price-basis one; not carrying it sidesteps both at once.

**Recommend**: run check #1 now (it's answerable from data already on disk); check #2 is blocked until the new prerequisite above is built and has run long enough to accumulate genuine live-captured history — it can't be shortcut retroactively. Given that real cost, weigh it against the alternatives honestly rather than defaulting to the historical-basis method because it was the first one proposed: if straddling rolls turn out to be rare (check #1), flatten-and-don't-reopen costs little and needs no new infrastructure at all, which may be the better trade against building and waiting on live next-contract tracking just to validate a method for a rare event. If straddling rolls are common, the capital-efficiency case for the historical-basis method is stronger and the infrastructure cost easier to justify.

---

## 7. Trade-log schema — rolled trades need explicit representation [OPEN]

`prometheus_trades.csv`'s explicit design goal is schema parity with `trade_summary_p2.csv` (a direct diff against the backtest, not a translation exercise). The backtest has **no concept of a rollover** — every backtested trade lives entirely on one contract. A rolled live trade doesn't fit that shape without a decision:

- **Two linked rows** (old-contract leg, new-contract leg), joined by a new `parent_trade_id` — preserves the "one row per contract-leg" shape the backtest already has, but a naive sum over `prometheus_trades.csv` double-counts a single logical trade unless every consumer knows to group by `parent_trade_id`.
- **One row, with `rolled_from_contract`/`rollover_ts` columns added** — a rolled trade stays one logical row, closer to how it's actually experienced, but doesn't parity-check cleanly against a backtest that has no equivalent field at all.

Either way, **this needs deciding before building the trade log, not discovered after the first live rollover produces a row nobody parses correctly.**

---

## 8. First-minute exit guard — avoid SL/target/trend_flip on session-open price-discovery noise

**Real incident, 2026-09-02**: CRUDEOILM's very first 1-min candle of the day (09:00:00) printed O=8199 / H=8627 / L=8180 / C=8605 — a 447-point (5.45%) high-low range inside a single minute, on real volume (201 contracts), not a stray tick. User-reported (not independently verified against a file in this repo — the local `CRUDEOIL` data only runs to 2026-08-28, no 09-01/09-02 coverage): the full-size CRUDEOIL contract's same minute showed a normal ~10-point range (8603/8611/8601/8604), suggesting this was thin-liquidity price discovery specific to the mini contract's first minute rather than a genuine underlying move — plausible and consistent with everything below, but flagged here as user-reported pending independent confirmation once CRUDEOIL data for these dates is available locally. Prometheus was flat at the time (last position had closed the evening before), so nothing fired — but this is exactly the shape of print that could trigger a spurious SL or target exit on a real open position, and Phase 3 makes that the *normal* case, not a rare one: positions now routinely sit open across a session's opening bar (§2), where Phase 2 almost never did (always flat by EOD, so its exit-check logic essentially never runs at session start — except in the rare case of a genuinely unconfirmed EOD-exit failure carrying a position into the next day, per the post-2026-08-31 fill-confirmation invariant).

**Precedent already in this codebase**: Apollo has exactly this guard. `apollo_configs.py`: `NO_EXIT_BEFORE = '09:16'`, with the comment "No exit on 09:15 candle close — defer SL check to 09:16." Applied as a plain, non-blocking early return at the top of both exit-check functions (`apollo.py:920-922`, `:942-944`): `if datetime.now().time() < self._no_exit_time: return False`. Artemis has a heavier version (`credit_spread.py:948-991`) that blocks with `sleep()` until 9:16 and does a single fresh re-check when an SL condition fires early, rather than just skipping. Apollo's version is the right fit here — Prometheus's own loop already re-ticks every 0.5–1s, so the very next tick after the guard time re-evaluates with a fresh price automatically; no explicit sleep needed.

**Requirement**: add `NO_EXIT_BEFORE` to `prometheus_configs.py`'s production counterpart, set to **09:01** — one minute past MCX's actual session open (`SESSION_START_TIME='09:00'`), mirroring the *same principle* Apollo/Artemis apply at NSE's 9:15 open (one minute of price-discovery buffer), not the literal clock value, since MCX opens 15 minutes earlier than NSE. Gate the continuous LTP-driven SL/target checks (`_check_exit_conditions_ltp`) behind it.

**This guard does NOT cover trend_flip — a real gap, not a simplification.** Checked `_handle_new_15m_bar` (`prometheus.py:551-628`) directly: it runs unconditionally on every 15-min boundary, with no time gate anywhere in it. It builds the new bar, calls `compute_st` on the combined series, persists it, and reads `flip`/`direction_now` straight off the just-recomputed trend — completely independent of `NO_EXIT_BEFORE`, which only guards the separate continuous-LTP check path. A corrupted 09:00 bar that happened to also cross the existing ST band would have produced a *recorded* `trend_flip`, and under Rule 7 that's simultaneously an exit and a reversed entry, fired immediately at 09:15:04-ish when the bar closes — `NO_EXIT_BEFORE` would not have stopped it. §9 below covers this properly, since it's really the same underlying problem (one bad print distorting the signal) rather than a second, separate first-minute issue.

---

## 9. Price-artifact protection for ST_15 — a single bad print can freeze the signal, not just trigger one bad exit

**The same 2026-09-02 candle corrupted the Supertrend calculation itself, not just the momentary price.** Computed `compute_st` directly against real CRUDEOILM data across the anomalous bar, both multipliers:

- **Mult 3.0: completely frozen.** ST = 8410.370081 at 23:15 (the bar before), stays at *exactly* 8410.370081 through 09:00 (the anomalous bar), 09:15, and 09:30 — three consecutive 15-min bars, 45 minutes, zero movement, while price rallied from ~8528 to ~8617.
- **Mult 2.0: frozen during the bad bar, then a muted creep.** ST = 8452.246721 at 23:15 and stays there through 09:00, then only creeps to 8456.50 (09:15) and 8472.75 (09:30) — far slower than the move in price would suggest.

**Mechanism**: Supertrend's ratchet takes `max(previous_lower_band, basic_lower)` in an uptrend, where `basic_lower = midpoint − multiplier × ATR`. The 447-point true range on that one bar spiked ATR, which pushed `basic_lower` *down* — below the already-ratcheted previous value — so the ratchet just kept the old band unchanged. The close (8606) itself was fine; it's the *range* that corrupted ATR, and ATR's own rolling window then carries that one inflated reading forward for roughly `ST_PERIOD` (10) more bars, not just this one — meaning the signal can stay distorted well past the bad print itself, not just risk one bad immediate trigger. This is a different, more fundamental problem than §8: §8 stops the SL/target *action*; this is the *signal itself* being wrong for an extended window afterward, including the trend/flip state that Rule 7 acts on.

**Before assuming this is fixable by filtering at all, checked whether the artifact is distinguishable from genuine volatility by size.** Computed the full 15-min true-range distribution across CRUDEOILM's whole history (8,574 bars, mean 48.9, std 43.8). Today's 447-point bar ranks **#11 largest, 99.87th percentile, ~9.1 std devs above the mean** — large, but **not uniquely so**: at least 10 historical bars are equal or bigger, up to 809 points (2026-03-09). **Pure magnitude alone can't separate "bad print" from "real large move"** — but cross-checking each of those 10 bars against CRUDEOIL (the full-size contract, same underlying, same exchange, same minute) at the same timestamps found a much cleaner signal than magnitude:

| Timestamp | CRUDEOILM TR | CRUDEOIL TR | Ratio | Session-opening bar? |
|---|---|---|---|---|
| 2026-03-09 10:45 | 809 | 715 | 0.88 | No |
| 2026-03-23 16:30 | 654 | 654 | 1.00 | No |
| 2026-04-08 09:30 | 640 | 320 | 0.50 | No (09:30) |
| 2026-04-06 09:00 | 618 | 160 | 0.26 | **Yes** |
| 2026-03-09 09:00 | 525 | 365 | 0.70 | **Yes** |
| 2026-03-09 15:00 | 521 | 521 | 1.00 | No |
| 2026-03-31 22:00 | 515 | 480 | 0.93 | No |
| 2026-05-19 09:00 | 490 | 21 | 0.04 | **Yes** |
| 2026-06-03 09:00 | 477 | 26 | 0.05 | **Yes** |
| 2026-05-25 09:00 | 468 | 180 | 0.38 | **Yes** |
| 2026-09-02 09:00 | 447 | n/a (no local coverage) | — | **Yes** |
| 2026-03-09 19:30 | 442 | 469 | 1.06 | No |
| 2026-04-02 20:00 | 428 | 424 | 0.99 | No |
| 2026-03-09 11:15 | 422 | 418 | 0.99 | No |
| 2026-05-11 09:00 | 397 | 114 | 0.29 | **Yes** |

**Every non-opening-bar large-range event has CRUDEOIL moving in near-lockstep with CRUDEOILM (ratio 0.88–1.06) — these are real market moves.** Every session-*opening*-bar large-range event but one has CRUDEOIL's range at a fraction of CRUDEOILM's (ratio 0.04–0.38) — CRUDEOIL barely moved while CRUDEOILM spiked. Today's incident is the **sixth** confirmed instance of this exact pattern over ~7 months, not a one-off.

**2026-03-09's opening bar (ratio 0.70) is excluded from that pattern, not a partial exception to it.** User's own external research: that date had MCX-side backend changes that may have corrupted data independent of anything about price discovery — a separate, unrelated data-quality issue, not a milder version of the same artifact. Whatever caused it is out of scope here; it shouldn't be forced into the "opening bar" story just because it happened to also be an opening bar, and it shouldn't inform the fix below (its data is potentially unreliable on both instruments, so CRUDEOIL isn't a trustworthy reference for that specific date either).

**Decided: cross-instrument substitution, not filtering.** Since all six confirmed artifacts land in the same single bar (09:00, the very first candle of the session) and CRUDEOIL is confirmed reliable at that exact moment every time, the fix doesn't need to guess at a magnitude threshold or discard information — it can directly replace the bad print with a known-good one. Runs once per session, right after the 09:00 candle downloads; no comparison for any other bar, any other time:

```python
def patch_opening_bar_if_artifact(m_bar: dict, o_bar: dict, threshold: float = 0.5) -> dict:
    """
    m_bar/o_bar: the 09:00 1-min candle for CRUDEOILM/CRUDEOIL, each
    {'open','high','low','close'}. Same underlying, same per-barrel price
    (only lot size differs -- no scaling needed, see prometheus_backtest/
    README.md's CRUDEOIL cross-validation convention). If CRUDEOIL's true
    range is under `threshold` of CRUDEOILM's at this exact minute,
    CRUDEOILM's own print is thin-liquidity noise -- substitute CRUDEOIL's
    OHLC outright. Runs once, right after the 09:00 candle downloads; BAU
    for the rest of the session either way.
    """
    m_tr = m_bar['high'] - m_bar['low']
    o_tr = o_bar['high'] - o_bar['low']
    if m_tr > 0 and (o_tr / m_tr) < threshold:
        logger.warning(f"Opening-bar artifact: CRUDEOILM TR={m_tr} vs CRUDEOIL "
                        f"TR={o_tr} (ratio {o_tr/m_tr:.2f}) -- substituting.")
        return o_bar.copy()
    return m_bar
```

`threshold=0.5` is a deliberate, evidence-backed choice: it clears every confirmed artifact (0.04–0.38, all comfortably below) with margin, and sits below every confirmed real-move ratio (0.88 and up) from the non-opening-bar evidence — a clean gap, not a fitted cutoff. Because the check only ever runs against the 09:00 bar, it never touches 2026-04-08's 09:30 bar (ratio 0.50, ambiguous, but out of scope by construction) or 2026-03-09's excluded case.

**This beats both insertion points considered earlier** (1-min raw clamp, and ATR-input winsorizing): it doesn't rewrite broad swaths of history the way a general clamp would (only ever touches one candle, once a day, and only when the check actually fires), and it doesn't need a guessed cap or a discarded bar the way winsorizing/exclusion would — CRUDEOIL's own print *is* the correct value, not an estimate of one.

**New production requirement this creates**: the pipeline needs CRUDEOIL's own 09:00 candle available at the moment CRUDEOILM's downloads, every session — a live poll for that one instrument, once a day, right at the open. Smaller and more targeted than §6's "track every listed contract continuously" requirement (that one's for arbitrary historical lookback across a rollover's whole holding period; this one only ever needs a single, same-day candle), but it's the same class of gap — nothing in the current pipeline polls CRUDEOIL at all today (`select_front_month_contracts` per underlying, §6) — so this still needs building, not assuming.

**Applies to both backtest and production, but not identically implemented**: the backtest correction (`prometheus_backtest/data_loader.py`, `_CRUDEOILM_OPENING_BAR_CORRECTIONS`) is a hardcoded dict of the 6 confirmed dates and their known-correct OHLC values — a fixed, reviewed substitution for reproducing this calibration, not a live `threshold=0.5` check. It does not auto-detect a 7th instance if one occurs in data added later; a future date showing the same pattern needs to be confirmed and added to the dict by hand, the same way these 6 were. Production runs the actual live `patch_opening_bar_if_artifact()` check described above, every session, against whatever CRUDEOIL prints that day — that one *is* general-purpose. Don't assume the backtest's behavior generalizes past these 6 dates.

**Cost of getting this wrong either way**: this changes the ST_15 series again, which means the mult 2.0-vs-2.5 comparison (already re-run once this session for the weekend-bar fix) needs re-running a second time once this is implemented. Known cost, not a surprise.

---

## 10. Reporting: realised, unrealised, and total P&L in trade updates

**Requirement (Phase 3 only — Phase 2 keeps its current reporting unchanged):** every periodic trade update and the "still open at session end" line in the session report must show realised P&L, unrealised P&L, and their total, not unrealised alone.

**Why Phase 2 doesn't need this and Phase 3 does:** in Phase 2 a position is open for at most one session, so the brief window where lot1 is booked and lot2 is still running is transitional — the trade finishes and its true total lands in the session report within the same day regardless. In Phase 3 a position can run for days to weeks (§2), so "lot1 booked, lot2 still open" isn't a brief transitional state — it's the normal shape of a trade for most of its life. Reporting only the open lot's mark-to-market during that whole stretch hides the majority of what's actually locked in.

**This is also a real, currently-existing gap, not just a missing feature — worth fixing precisely because of what it's currently silently dropping:**

- `_send_trade_update()` (`prometheus.py:941-956`) computes P&L only from `lots_open` — whichever of lot1/lot2 still has `status == 'open'`. Once a lot books, its P&L disappears from every subsequent update; there's no realised term at all.
- `_send_session_report()`'s "still open" fallback block (`prometheus.py:414-429`) has the same shape: it computes one `pnl_pts`/`pnl_rs` off `lots_open` only, and labels the whole thing `_(unrealised)_`. A trade that has lot1 already booked and lot2 still running never gets appended to `prometheus_trades.csv` — `_finalize_trade()` only fires once *both* lots are closed (`prometheus.py:763-764`) — so `trades_today` never picks up lot1's locked-in P&L either. **The already-realised portion of a still-open trade is currently invisible in both places it could show up**, and `total_rs` (the session-report grand total) silently understates by exactly that amount whenever a trade is mid-way through its scale-out at report time.

**Fix, both call sites**: compute per-lot realised P&L from whichever of `lot1_exit_price`/`lot2_exit_price` is populated (lot status not in `{'open', 'never_opened'}`) against `entry_price`, direction-signed, times that lot's filled quantity — this is already exactly what `_finalize_trade()` computes per lot, just needed earlier and un-conditioned on both lots being closed. Compute unrealised P&L the same way the current code does, but only over lots still `status == 'open'`. Report all three:

```
Realised   : <sum of booked lots' pnl>
Unrealised : <mark-to-market of still-open lots at current LTP>
Total      : <realised + unrealised>
```

**Interacts with §7's open trade-log-schema decision**: if a rollover flattens and reopens a trade (two-linked-rows option), does "realised" for the new leg start back at zero, or carry forward the old leg's already-booked P&L as one continuous running total? Resolve this alongside §7, not independently — the two decisions should produce a single consistent answer for what "realised" means across a roll.

---

## 11. ST-15 seed skip-list — excluding known-bad sessions from the daily re-seed window

Since `prometheus.py` is a fresh cron-launched process every session (§2), it never carries an in-memory Supertrend state across days — `seed_st15` recomputes ST_15 from scratch every morning off a rolling `SEED_DAYS=18` calendar-day tail-read (`prometheus_configs.py`). Every single day's regime state depends on that same ~18-day (~13-14 trading-day) window of raw 1-min history, recomputed fresh — so a known-bad session anywhere in that window corrupts the live seed for as long as it stays in the window, not just the one day it happened on.

This is the live-production version of exactly what was just fixed for the backtest: 2026-02-01's MCX Union Budget special session (WTI not trading, thin/disconnected price action) was confirmed this session to distort both raw ST_15 signal flips and SL/target fills, understating the true achievable P&L of both multiplier candidates by real money (₹4,368 at mult 2.0, ₹8,314 at mult 2.5, once fixed). The backtest fix was permanent — drop the date from the historical file, once, forever (`prometheus_backtest/data_loader.py`'s `load_futures_1min`). Live seeding can't do that the same way: the same date keeps re-entering the rolling window every morning for as long as it's within `SEED_DAYS` of today, and a *new* anomalous session (a future Budget day, a muhurat/special session, an exchange-declared correction) could appear at any time, not just retroactively.

**Add `ST_SEED_SKIP_DATES` to `prometheus_configs.py`** (production counterpart of `configs_p3.py`): a plain list of ISO date strings, empty by default, manually populated by an operator when a known-bad session is identified —

```python
ST_SEED_SKIP_DATES = [
    '2026-02-01',  # MCX Union Budget special session, WTI not trading -- added 2026-09-XX
]
```

`seed_st15`/`_tail_read_contract_csv` filters these dates out of the raw 1-min tail-read before resampling to 15-min and computing Supertrend — same mechanism, same place in the pipeline, as the backtest fix. Applies wherever `seed_st15` runs, which includes the rollover re-seed (§5, item 2) as well as the normal daily `_setup()` seed — one function, one fix, no separate handling needed for either caller.

**Lifecycle, per direction**: rarely populated, manually pruned, not a permanent growing list. Once a skipped date falls outside the rolling `SEED_DAYS` window (today minus `SEED_DAYS` calendar days > the skipped date), the entry is a no-op — it's excluding a date the window was never going to include anyway — and should be deleted from `configs.py` at that point. `SEED_DAYS=18` calendar days ≈ 13-14 trading days, so roughly 3 weeks after adding an entry it's due for removal. Annotate each entry with the date it was added (as above) so staleness is easy to eyeball rather than tracked separately.

**Recommended, not required**: a non-blocking startup log line (in `seed_st15` or `_setup()`) listing any `ST_SEED_SKIP_DATES` entries that have already aged out of the current window — costs nothing, and turns "did anyone remember to prune this" into something the logs surface on their own rather than relying on an operator's memory.

---

## 12. Prometheus write-removal — stop writing to the shared MCX contract CSV [OPEN, PROPOSED 2026-09-02]

**The idea.** `data_downloader_mcx.py` was redesigned 2026-09-02 to run once nightly (23:56 IST, right after MCX close) and be the *sole* writer of every contract's on-disk CSV — front-month and next-month, every enabled underlying (`data_pipeline/README.md`). For that to actually hold, Prometheus has to stop writing into the same files during the day: keep its own in-memory intraday 1-min series for its own decision-making, never persist it, and rebuild it from scratch (yesterday off disk, today off a live re-fetch) on every startup — fresh morning start and mid-day crash-restart treated identically, one code path. This removes the write-race the old `_already_up_to_date()` skip existed to avoid (already removed from `data_downloader_mcx.py` — see `data_pipeline/README.md`'s "sequencing note": until *this* section is implemented, Prometheus still writes intraday, so the nightly downloader will redundantly, harmlessly re-fetch CRUDEOIL/CRUDEOILM's session each night).

**Why now, not just "would be nice":** Phase 3 positions span rollovers (§2-§6), and a front-month-only, no-backfill downloader (§6, this session's earlier fix) means the new front-month contract has *zero* history the instant it becomes front-month — unless something tracks it before then. The 2026-09-02 redesign's universal next-month tracking closes that gap from the data-pipeline side. This section is the other half: making Prometheus actually rely on the pipeline as the single source of truth, instead of half-relying on its own historical writes into the same file.

**Every current write into the shared contract CSV, enumerated** (`grep`-verified against `prometheus.py`/`prometheus_functions.py`, 2026-09-02):

| Call site | What it does today | Disposition |
|---|---|---|
| `_merge_1m` (`prometheus.py:530-545`) | Writes every 1-min poll to `_merge_and_save(self._contract['filepath'], new_df)` **and** appends to `self._df_1m_today` in-memory | **Delete the write, keep the in-memory append.** One line removed (`_merge_and_save(...)`); everything downstream (`_handle_new_15m_bar`, called right after) already reads only `self._df_1m_today`/`self._df_15m` in-memory, never the disk file — confirmed by reading it end to end. This is the only change needed to the intraday loop itself. |
| `backfill_contract_if_needed` (`prometheus_functions.py:293-322`) | On a newly-effective contract whose file's oldest row isn't old enough for `SEED_DAYS`, fetches and writes the missing older history | **Keep, as a defensive fallback — orthogonal to this section.** This fixes genuinely-missing *historical* data (an under-tracked contract), not the intraday-poll write point 4 is about. Under the new pipeline design it should rarely fire (front+next month are tracked nightly for every underlying), but if the nightly cron ever fails or the VPS is down, this is the thing that lets Prometheus self-heal instead of silently seeding short. Leave it writing — extending the pipeline's own file when the pipeline itself fell behind is the correct behavior, not a violation of "Prometheus doesn't write intraday data." |
| `backfill_recent_gap_if_needed` (`prometheus_functions.py:325-369`) | Closes the gap between the file's last on-disk row and `now`, sized for downtime *Prometheus's own intraday writes* would otherwise have left | **Dead under the new design — remove the call from `seed_st15`.** Its entire reason to exist was that Prometheus used to be a co-writer of the file's *today* rows, so a restart could leave a same-day hole in them (this is literally the function that fixed the 2026-08-31 Kill Switch incident, minutes 12:03-12:08 lost). Once Prometheus never writes today's rows to disk at all, there is no such hole to close — the file will always end at yesterday's last row, every single day, and the *replacement* mechanism (below) is a different operation, not a repurposing of this one. |
| `_setup`'s "seed today's in-memory accumulator from whatever the file already has" (`prometheus.py:229-236`) | Reads today's rows out of the shared file, if any exist (relevant today because a live process, including Prometheus itself, might have already written some) | **Replace, don't keep as-is.** Under the new design the shared file will *never* have today's rows (data_pipeline only writes overnight), so this block would always find zero and silently no-op — worse than deleting it, because it looks like it does something. Replace with an unconditional live re-fetch of today 09:00→`now` (see below), which subsumes both the fresh-morning case (an essentially-empty fetch, since `now` ≈ 09:00) and the crash-restart case (the real gap-filler) in one code path. |
| `seed_st15` (`prometheus_functions.py:470-507`) | Tail-reads the file (past days *and* whatever of today it has), resamples, gap-checks, computes ST — treats "the file" as the single source for both | **Restructure, not delete.** Past days stay disk-sourced (now robustly guaranteed by the nightly pipeline instead of best-effort). Today has no disk source anymore under this design — it must come from the new live re-fetch step, concatenated with the disk-sourced past days *before* `_resample_1m_to_15m`/`_find_15m_gaps` run, so gap-checking still sees one contiguous series, not two independently-validated halves. |
| `persist_15m_series`, `_append_running_row`/trade-log CSVs | Separate files (`SERIES_15M_FILE` debug dump, `TRADES_FILE`/cumulative trade log) | **Unrelated, unchanged.** Neither is the shared MCX contract CSV `data_downloader_mcx.py` maintains — flagged here only so a future reader doesn't lump them in. |
| `PrometheusState` (`prometheus_state.py`, `save_state`/`load_state`, `_setup`'s "Resuming in-trade state" block) | Trade-state (in_trade/watching, entry price, lot status) persistence and resumption | **Unrelated, unchanged.** A completely separate recovery mechanism from the 1-min OHLCV series this section is about — point 4's "watch out for crashes and restarts" already has an answer here that this section doesn't touch. |

**The new startup sequence** (`_setup`/`seed_st15`, both a fresh 09:00 start and a mid-day crash-restart, same code path):
1. `backfill_contract_if_needed` — defensive, as above, rarely fires.
2. Tail-read the shared file for calendar days *before* today only (an explicit cutoff, not "whatever's on the file" — the file may contain nothing for today, or, during the transition described in the sequencing note above, it may still contain some of today's rows from an old build; excluding today explicitly avoids depending on which is true).
3. Fetch today `SESSION_START_TIME`→`now` live from the broker (same `fetch_one_minute_window`/resilient-poller mechanics §3 already built, reused — not new infrastructure), building `self._df_1m_today` from scratch every single startup regardless of restart vs. fresh start.
4. Concatenate (2) and (3) into one 1-min series, *then* resample to 15-min, gap-check, compute ST — same `_resample_1m_to_15m`/`_find_15m_gaps`/`compute_st` functions, unchanged, fed a differently-assembled input.

**Two incident-derived constraints this must not lose, both already enforced by existing code and unaffected by the restructuring above — call this out explicitly when it's built, don't just assume it carries over:**
- `_resample_1m_to_15m`'s "a window only counts once it has genuinely finished elapsing by `now`" guard (2026-08-31, 12:13:49 restart produced ST=8105.67 from a 13-of-15-minute partial bar vs. the chart's correct 8102.33). Applies identically to a bar built from step 3's live-fetched today data — the function itself doesn't change, but it's now doing more of the work (all of today, not just whatever gap remained), so it deserves being named as still-load-bearing, not silently assumed.
- The *intent* behind `backfill_recent_gap_if_needed` (never let a same-day hole go unrecovered) doesn't disappear — it moves entirely into step 3 above (an unconditional full re-fetch, not a diff against a stale on-disk marker). This is a strictly simpler mechanism than the old gap-diff approach, but it changes the failure mode described next.

**New robustness trade-off this introduces — flag to the user, not a detail to bury:** today's data now has no on-disk fallback. Old design: if a live poll failed, whatever had already been written to disk for today survived a restart untouched, and only the actual gap needed re-fetching. New design: a broker/network hiccup during step 3's re-fetch blocks `seed_st15` entirely for the *whole* day so far, not just the failed window — `_find_15m_gaps` still refuses to seed on any hole, and there's no cached partial copy to fall back to. This is a real, deliberate cost of removing the shared-file write, not a bug to fix — but it means a rough network patch on a rollover morning specifically (exactly when the new front-month's own intraday history is thinnest) is more disruptive than it would have been under the old design. Worth deciding, explicitly, whether that trade is acceptable before this ships.

**Status: proposed, not implemented.** Confirm the sequence above (especially the "today has no disk fallback" trade-off) before touching `prometheus.py`/`prometheus_functions.py` — this is live-trading crash-recovery code, not yet live-tested even under `DRY_RUN=True`, and CLAUDE.md's Production QC rule (read the entry path end-to-end before any live session) applies with extra weight here given how much of this section is incident-derived from real 2026-08-31 failures.

---

## 13. Open decisions — summary

1. §4 — missed-rollover recovery: roll immediately at open vs. refuse-and-alert. (Leaning: roll immediately, loudly alerted.)
2. §6 — SL/target recalibration method: the proposed historical-basis method, reset-to-now, or flatten-and-don't-reopen. Blocked on the two empirical checks named there.
3. §7 — rolled-trade log schema: two linked rows vs. one row with rollover columns.
4. Whether `TENDER_ROLL_TRADING_DAYS=5` itself (inherited from Phase 2, not independently re-verified here) is actually where MCX's tender margin kicks in — worth confirming once, since everything else in §3 is built on top of it being correct.
5. §10 — whether "realised" resets to zero on a rollover's flatten-and-reopen leg, or carries the prior leg's booked P&L forward as one continuous total. Tied to the §7 schema choice — resolve together.
6. §9 — decided in direction (cross-instrument substitution on the 09:00 bar only, `threshold=0.5`), not yet built. Remaining work is implementation, not design: add a live CRUDEOIL poll for the opening candle to the production pipeline (new requirement, nothing polls CRUDEOIL today), apply the same substitution to the backtest's historical data, and re-run the mult 2.0-vs-2.5 comparison a second time once that's done.
7. §12 — Prometheus write-removal: confirm the proposed startup sequence (especially the "today has no on-disk fallback if the live re-fetch fails" trade-off) before implementing.
