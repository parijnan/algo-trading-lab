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
2. **Basis drift vs. target distance, over realistic hold windows.** The known backfill quirk (`2026-09-21_futures.csv` already extends back to February, `2026-10-19_futures.csv` into August) means overlapping 1-min data for adjacent contracts already exists on disk. Compute the old-minus-new spread minute-by-minute and measure its drift over 1/2/4-week windows, compared against what a 1% target actually means in rupees at current price levels (roughly ₹80/barrel at 1% of an ₹8,000 contract). If typical drift over a realistic hold is small relative to that, the method is sound; if it's comparable or larger, it isn't.

**Two alternatives worth weighing once those checks are in:**

- **Reset-to-now**: treat the rollover as a fresh synthetic entry on the new contract, basis = new contract's *current* price at rollover time, discarding whatever progress the trade had already made. No basis-drift risk, no historical-price data-quality risk (the new contract's price months before its own front-month tenure may be thin/wide/unreliable — a real concern for the proposed method's historical lookup specifically). Costs: a trade that was most of the way to target on the old contract effectively restarts.
- **Flatten and don't reopen** — go to `watching` on the new contract and wait for a genuine fresh ST_15 flip, rather than carrying the position across at all. This isn't just the simplest option; it sidesteps a *second*, separate problem the other two methods don't address: the new contract's ST series is being **re-seeded from scratch** (§0/§5 — no cross-contract stitching), so its freshly-computed trend state might already *disagree* with the carried position's direction. `_handle_new_15m_bar` only exits on a `trend_flip` *transition* — a freshly-seeded series that opens already bearish produces no flip event, so a carried long would sit against its own newly-computed regime until the next flip fires, with no mechanism to notice the disagreement in the meantime. Carrying a position across the roll means solving *this* coherence problem in addition to the price-basis one; not carrying it sidesteps both at once.

**Recommend**: run the two checks above before deciding. If straddling rolls turn out to be rare *and* basis drift is small relative to target distance, the proposed method is fine and worth keeping (it's more capital-efficient than flattening a good trade early). If either check comes back unfavorable, flatten-and-don't-reopen is the safer default.

---

## 7. Trade-log schema — rolled trades need explicit representation [OPEN]

`prometheus_trades.csv`'s explicit design goal is schema parity with `trade_summary_p2.csv` (a direct diff against the backtest, not a translation exercise). The backtest has **no concept of a rollover** — every backtested trade lives entirely on one contract. A rolled live trade doesn't fit that shape without a decision:

- **Two linked rows** (old-contract leg, new-contract leg), joined by a new `parent_trade_id` — preserves the "one row per contract-leg" shape the backtest already has, but a naive sum over `prometheus_trades.csv` double-counts a single logical trade unless every consumer knows to group by `parent_trade_id`.
- **One row, with `rolled_from_contract`/`rollover_ts` columns added** — a rolled trade stays one logical row, closer to how it's actually experienced, but doesn't parity-check cleanly against a backtest that has no equivalent field at all.

Either way, **this needs deciding before building the trade log, not discovered after the first live rollover produces a row nobody parses correctly.**

---

## 8. Open decisions — summary

1. §4 — missed-rollover recovery: roll immediately at open vs. refuse-and-alert. (Leaning: roll immediately, loudly alerted.)
2. §6 — SL/target recalibration method: the proposed historical-basis method, reset-to-now, or flatten-and-don't-reopen. Blocked on the two empirical checks named there.
3. §7 — rolled-trade log schema: two linked rows vs. one row with rollover columns.
4. Whether `TENDER_ROLL_TRADING_DAYS=5` itself (inherited from Phase 2, not independently re-verified here) is actually where MCX's tender margin kicks in — worth confirming once, since everything else in §3 is built on top of it being correct.
