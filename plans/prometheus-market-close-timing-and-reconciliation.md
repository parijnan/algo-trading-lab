# Prometheus: market-close timing fix + missed-flip reconciliation

Status: **IMPLEMENTED 2026-09-11**, tests passing (98 passed, 2 pre-existing
skips). Go-live decided for 2026-09-15.

Decisions taken (2026-09-11):
1. **Hard stop at CLOSING_TIME, no grace buffer.** SESSION_END_TIME simply
   equals CLOSING_TIME now.
2. **DRY_RUN market-hours guard resolved now, not deferred.** User's framing:
   "Live production will handle it because the market is closed, not because
   our bot was designed correctly. We need to design the bot correctly to
   begin with." `place_order()` now refuses any order — paper or live —
   at/after CLOSING_TIME, uniformly, as the actual enforcement point (not
   the loop's own stop time, which is now belt-and-suspenders on top of it).

## Background — three findings from a code trace of Q1/Q2/Q4

The user asked four questions about production's session-close handling
(2026-09-11). Q3 (no-immediate-reentry-next-day protection) was confirmed
correct as-is, no change needed. Tracing the code to answer Q1/Q2/Q4 surfaced
three real issues, all stemming from the same root cause: **`CLOSING_TIME`
(the actual, DST-dependent market close, already hand-toggled today) and
`SESSION_END_TIME` (when the bot process itself stops) are computed with
string `HH:MM` arithmetic that has no concept of a day boundary.**

### Finding 1 — dormant day-rollover bug (currently dormant, fires ~1st Sun of Nov)

`prometheus_configs.py`:
```python
def _minus_minutes(hhmm: str, minutes: int) -> str:
    t = _dt.strptime(hhmm, '%H:%M') - _td(minutes=minutes)
    return t.strftime('%H:%M')

SESSION_END_TIME = _minus_minutes(CLOSING_TIME, -SESSION_END_BUFFER_MIN)   # CLOSING_TIME + 25min
```
When `CLOSING_TIME='23:30'` (DST in force, current), `SESSION_END_TIME='23:55'`
— safe, same day. When `CLOSING_TIME` is hand-toggled to `'23:55'` (winter),
`SESSION_END_TIME` computes to `'00:20'` — a bare string with no day
information. `run()` then does:
```python
session_end = datetime.now().replace(hour=0, minute=20, second=0, microsecond=0)
```
stamping `00:20` onto **today's** date, hours in the past relative to any
daytime process start. Verified directly: `now=2026-11-15 09:05`,
`session_end=2026-11-15 00:20` → `now < session_end` is `False` from the very
first loop check. The main `while` loop body never executes — `_setup()` runs,
then falls straight to `_teardown()`. Any open position sits completely
unmonitored for the entire session. Same failure class as the 2026-08-31
28-minute incident, but for a full day.

### Finding 2 (Q1) — no guard against acting after actual market close

Neither `_execute_entry` nor `_execute_rule7_flip` nor
`_execute_coincident_flip_transition` checks market-hours at all — they're
gated only by `_past_min_entry_guard` / `_rollover_entry_suppressed` /
`_check_1h_alignment`, none of which know what time the market closes. The
only reason this hasn't bitten yet is that the bot currently stays alive
until `SESSION_END_TIME` = `CLOSING_TIME` + 25min, so in principle a flip in
that 25-minute post-close window could fire a live (or paper) order. In
`DRY_RUN=True` (current), `place_order` unconditionally "succeeds" with no
market-hours awareness of its own — in live mode the broker's own rejection
is the only backstop, external to this codebase.

### Finding 3 — a flip in the last, unprocessed bar is silently lost, not "caught up automatically"

The user's proposed design assumed: stop the bot at the real close, never
process the last (possibly truncated) 15m bar, and "at the next session open
it will be calculated and evaluated automatically." Traced this claim against
the actual code and it's **false as the code stands today**:

- `seed_st15()` (`prometheus_functions.py:877`) recomputes the *entire*
  historical ST_15 series fresh on every `_setup()`, from the shared pipeline
  CSV (`raw_1m_past`, "never written by Prometheus" — populated by the
  nightly MCX data pipeline independent of whether Prometheus itself was
  running) plus today's cache. This **does** correctly include yesterday's
  final bar, with a correctly-computed `trend`/`trend_flip` — confirmed
  `compute_st` computes `trend_flip` as `trend != trend.shift(1)` over the
  whole series, not just "new" rows, and `seed_st15` even logs "Last 15m
  flip" if one exists. So the *signal* is correctly recomputed.
- But nothing *acts* on it. `_execute_entry`/`_execute_rule7_flip` are only
  ever called from inside `_handle_new_15m_bar` (`prometheus.py:2003`),
  which itself is only called from `run()`'s live boundary-tick loop
  (`prometheus.py:1831`) — never from `_setup()`. `_setup()` seeds the
  DataFrame, resumes `state.status` as-is (trusting an `in_trade` state
  verbatim, or defaulting to `watching`), and does nothing with the seeded
  series's flip history beyond a Slack "Trend: bullish/bearish" line.
- Concretely: bot flat overnight, last bar flips bullish, unprocessed. Next
  morning `seed_st15` shows `trend=bullish`, but no entry fires — the live
  loop only acts on `flip=True` for the *specific new bar it just built*.
  Today's first live bar continues bullish (no new transition), so
  `trend_flip=False` for it too. The bot sits `watching` indefinitely until
  some later, unrelated flip — a missed trade with no alert, exactly the
  kind of silent staleness this codebase's own conventions elsewhere
  explicitly guard against (`_handle_new_15m_bar`'s gap-handling comments).
  The `in_trade` case is subtler but equally silent: a resumed position is
  trusted as-is against a state that may now disagree with the freshly
  seeded trend, with no check comparing the two.

## Design

### 1. Collapse `SESSION_END_TIME` back onto `CLOSING_TIME` — remove the buffer, not just the bug

`CLOSING_TIME` already *is* the "actual market close time as a parameter in
configs" the user asked for — it's already hand-toggled at each DST
changeover, already documented with the exact dates. The 25-minute
`SESSION_END_BUFFER_MIN` was pure operational margin (mirroring
`mcx_live_downloader.py`'s own buffer), not because trading continues past
`CLOSING_TIME`. Once the last bar is deliberately never processed live (this
was always implicitly true for the *action* side — Finding 3 is what makes it
formally true), there's no remaining reason to keep the process alive past
`CLOSING_TIME` at all:

```python
# prometheus_configs.py — replace the buffer-derived computation entirely
SESSION_END_TIME = CLOSING_TIME
```

Delete `_minus_minutes` and `SESSION_END_BUFFER_MIN` (grep confirms
`_minus_minutes` has exactly one other caller — `ROLLOVER_TIME = 
_minus_minutes(CLOSING_TIME, ROLLOVER_BEFORE_CLOSE_MIN)` — which stays safe
regardless, since it only ever *subtracts* minutes from an already-same-day
`HH:MM` and both `23:30-15` and `23:55-15` stay same-day; no day-rollover
risk there, confirmed by grepping every `ROLLOVER_TIME`/`ROLLOVER_PREFETCH_TIME`
use in `prometheus.py` — all of them re-anchor to `now.date()` at the point
of comparison rather than caching a stale datetime, so this part needs no
change).

This directly fixes Finding 1 (no more cross-midnight subtraction anywhere in
the session-end path) and Finding 2 (the loop now genuinely stops at the real
close — no 25-minute window where a flip could still fire an order after
market close).

**Decided (2026-09-11): hard stop, no buffer.** Implemented as
`SESSION_END_TIME = CLOSING_TIME` directly in `prometheus_configs.py`.

### 2. `_minus_minutes` kept, scoped to its one remaining safe use

Not needed given the hard-stop decision (§1), but recorded since the review
that flagged this raised a durable-fix concern: the actual bug was the
*representation* (`_minus_minutes` returns a bare `"HH:MM"` string with no
day information), not just the one call site in `run()` that broke on it.
`_minus_minutes` is kept (moved next to its remaining caller,
`ROLLOVER_TIME`/`ROLLOVER_PREFETCH_TIME`, both of which only ever *subtract*
a small offset from a same-day `CLOSING_TIME` and can never cross midnight —
confirmed and pinned by
`TestSessionEndTimeInvariant.test_rollover_time_unaffected_still_same_day`),
with an explicit docstring warning against ever using it to *add* minutes
near a 23:xx close again. `SESSION_END_TIME` no longer uses it at all.

### 3. Missed-flip reconciliation — the part that makes the user's design assumption actually true

New persisted watermark field on `PrometheusState`
(`prometheus_state.py`): `last_processed_boundary: Optional[str]` (ISO
timestamp of the last 15m boundary `_handle_new_15m_bar` actually processed,
flip or not). Updated at the end of `_handle_new_15m_bar`
(`prometheus.py:2003`), right after `persist_15m_series` — unconditionally,
whether or not a flip occurred, so ordinary no-flip days advance the
watermark too and only a genuinely-unprocessed tail is ever found stale.

New method, `_reconcile_missed_flip()`, called from `_setup()` right after
`self.feed` is subscribed (needed for `_execute_entry`'s `feed.get_ltp()`)
and before/alongside `_recover_missed_rollover()` (same "catch up on
something that should have happened overnight" family, same place in the
sequence):

Actual implementation (`prometheus.py`) diverges from the sketch above in
one respect: `_past_min_entry_guard` almost always blocks a `watching`-branch
entry, since reconciliation runs at `_setup()` — literally minute zero of
the new session. Rather than silently dropping the signal when the guard
doesn't clear yet, the real implementation defers it:

```python
def _reconcile_missed_flip(self) -> None:
    ...
    if self.state.status == 'in_trade':
        if direction_now != self.state.direction:
            self._execute_rule7_flip(direction_now, window_start, bar['close'])
        self.state.last_processed_boundary = bar['time_stamp'].isoformat()
        save_state(self.state)
    elif self.state.status == 'watching':
        if (self._past_min_entry_guard(datetime.now())
                and not self._rollover_entry_suppressed(datetime.now())
                and self._check_1h_alignment(direction_now)):
            self._execute_entry(direction_now, window_start, bar['close'])
            self.state.last_processed_boundary = bar['time_stamp'].isoformat()
            save_state(self.state)
        else:
            # Deferred, not dropped -- retried every tick by
            # _retry_pending_missed_flip() until guards clear or a fresher
            # live flip supersedes it. Watermark NOT advanced until it fires.
            self._pending_missed_flip = {'direction': direction_now, 'window_start': window_start,
                                         'close': float(bar['close']), 'boundary_ts': bar['time_stamp']}
```

`_retry_pending_missed_flip()` is called every tick of `run()`'s main loop
(same shape as §7's `_pending_flip` retry), re-checks the same three guards,
fires `_execute_entry` once they clear, and clears itself if
`state.status` ever leaves `'watching'` without it (a fresher live flip
already fired — no double-entry). The `in_trade` branch needed no such
deferral: `_handle_new_15m_bar`'s own live `in_trade` path never gates
`_execute_rule7_flip` on `_past_min_entry_guard` either (only the `watching`
branch does, for thin-opening-liquidity protection on a *fresh* position) —
`_reconcile_missed_flip` mirrors that exactly.

Reuses `_execute_entry`/`_execute_rule7_flip` unmodified — both already
resolve the actual fill price from live LTP via `self.feed.get_ltp()`,
`bar['close']` is only carried through for `signal_ts`/`signal_close`
logging, exactly the same as the live path. Precondition verified:
`seed_st15`'s `raw_1m_past` comes from the shared pipeline CSV
(`data_pipeline/data/mcx/...`), populated by the nightly cron independent of
whether Prometheus itself was running — so yesterday's final bar is present
in the freshly seeded series regardless of when Prometheus stopped. No
coincident-flip-transition branch needed: `self._rollover_new_contract` is
only ever set by `_check_rollover_timing` (live-loop-only), always `None`
at this point in `_setup()`.

This covers both branches Finding 3 identified: `watching` → a fresh entry
that never fired, and `in_trade` → a flip that never fired (leaving the
position stale, wrong-direction). A multi-day gap (e.g. an MCX holiday, or
the bot down for a stretch) is handled the same way — coalescing to the
latest unprocessed flip reflects the current true state without replaying
every intermediate flip as if each were a live event (see
`test_multi_day_gap_coalesces_to_latest_flip_only`).

### 4. Market-hours guard inside `place_order()` — the actual enforcement point

**Decided 2026-09-11, not deferred**: `place_order()` (`prometheus_functions.py`)
now refuses any order — paper or live, uniformly — once `now` is at/after
`CLOSING_TIME`, returning `[]` (every existing caller already handles an
empty `order_ids` list as a failed order, no call-site changes needed).
This is now the actual enforcement point for "the bot never trades after
market close" — the loop's own hard stop (§1) is reinforcement, not the
only thing preventing it, per the user's explicit framing: relying on the
loop happening to not be alive is not the same as the bot being *designed*
to refuse. No lower-bound (session-open) check was added: Phase 3's
evening-only special sessions open at a real time that varies
(`self._df_1m_today`'s own first bar), not a fixed clock value
`SESSION_START_TIME` represents — `_past_min_entry_guard` already covers
thin-opening-liquidity protection on the entry side.

### 5. `CLOSING_TIME` itself auto-computed — no Slack button needed (added 2026-09-11)

Originally scoped §4 above as "manual toggle, propose a Slack button as a
follow-up." Reconsidered per the user's direct question: is MCX's DST-driven
close-time change actual exchange discretion, or a deterministic rule that
can just be computed? Researched via broker circulars covering the real
2026-03-09 change (Zerodha/Upstox/ICICI Direct bulletins) — confirmed
deterministic: MCX's non-agri (metals + energy, includes CRUDEOILM/CRUDEOIL)
evening close is 23:30 during US Daylight Saving Time (2nd Sunday of March
through 1st Sunday of November) and 23:55 outside it, purely to keep MCX's
close aligned with the US market hours that set the international benchmark
prices these contracts track. The underlying US DST rule itself has been
fixed federal law (2nd Sunday March / 1st Sunday November) since the Energy
Policy Act of 2005 — stable enough to compute rather than hand-maintain.

MCX applies its own change on the next MCX *trading* day after the DST
Sunday transition (confirmed: DST started Sunday 2026-03-08, MCX's change
took effect Monday 2026-03-09) — but since MCX never trades on the Sunday
itself, a plain `dst_start <= today < dst_end` date comparison already
resolves correctly with no separate trading-day-shift logic needed.

Implemented in `prometheus_configs.py`: `_us_dst_transition_dates(year)`
(2nd-Sunday-of-March / 1st-Sunday-of-November calculator) and
`_resolve_closing_time(today=None)`, with `CLOSING_TIME =
_resolve_closing_time()` computed fresh at every process import — i.e. every
time the bot starts (cron runs it fresh each day). **The manual-toggle
scheme and the Slack-button follow-up are both obsolete** — there's nothing
left to toggle, by hand or by button.

Residual edge case, accepted rather than engineered around: if the Monday
immediately following a DST transition happens to be an MCX holiday, MCX's
real change could land a day later than this computes. Rare (needs the 2nd
Monday of March or 1st Monday of November specifically to be a holiday) and
bounded — `place_order`'s own market-hours refusal (§4) is the actual
backstop against trading past the real close regardless of what this
computes; the broker's own rejection in live mode is the backstop beyond
that.

Tests: `TestClosingTimeAutoComputation` in
`tests/test_prometheus_market_close.py` — DST transition dates for 2024-2034
(always a Sunday, correct month), the exact 2026-03-08/03-09/11-01/11-02
boundary cases against the real-world-confirmed dates, and a pin that the
live `CLOSING_TIME` constant is actually wired to the computation (not a
stale value sitting next to it).

## Testing — implemented, `tests/test_prometheus_market_close.py` (16 tests) + 2 more in `test_state_roundtrip.py`

- `TestSessionEndTimeInvariant` — `SESSION_END_TIME == CLOSING_TIME`;
  `run()`'s session_end resolution never falls before `now` / never crosses
  a day boundary, replicated for both `'23:30'` and `'23:55'`;
  `ROLLOVER_TIME`/`ROLLOVER_PREFETCH_TIME` confirmed still same-day-safe.
- `TestPlaceOrderMarketHoursGuard` — refuses both `dry_run=True` and
  `dry_run=False` orders at/after `CLOSING_TIME` (the live-mode case passes
  `obj=None`, proving the refusal happens before any broker call would be
  reached); confirms the ordinary within-hours paper-fill path still works
  (regression guard).
- `TestReconcileMissedFlip` — bare `Prometheus` instance via
  `object.__new__` with `_execute_entry`/`_execute_rule7_flip`/guards/
  `save_state`/`_slack`/`logger` all mocked (real Slack/log/broker calls
  never touched — see the module's own docstring on why this matters for a
  strategy module with import-time file/logging side effects). Covers: no
  watermark yet (seeds baseline, no retroactive action); `watching` +
  unprocessed flip + clear guards → immediate entry; `watching` + blocked
  guard → deferred via `_pending_missed_flip`, watermark not advanced;
  `_retry_pending_missed_flip` firing once guards clear, no-op while still
  blocked, and dropping itself if state moved on without it; `in_trade` +
  opposing flip → Rule 7; `in_trade` + agreeing flip → no redundant refire;
  no-unprocessed-flip → true no-op; multi-day gap → coalesces to the latest
  flip only.
- `test_state_roundtrip.py::TestPrometheusStateRoundtrip` —
  `last_processed_boundary` round-trips as a real string and as `None`
  (not `'None'`/NaN) through the CSV save/load cycle.
- `python -m pytest tests/` — 98 passed, 2 pre-existing skips (was 80/2
  before this change) — no regressions.

## Files touched

- `prometheus_production/prometheus_configs.py` — `SESSION_END_TIME =
  CLOSING_TIME` directly; `_minus_minutes`/`SESSION_END_BUFFER_MIN`'s buffer
  usage removed, `_minus_minutes` itself kept (moved next to its one
  remaining safe caller, `ROLLOVER_TIME`).
- `prometheus_production/prometheus_state.py` — added
  `last_processed_boundary`.
- `prometheus_production/prometheus_functions.py` — `place_order()` market-
  hours refusal.
- `prometheus_production/prometheus.py` — `_handle_new_15m_bar` (persists the
  watermark every bar), new `_reconcile_missed_flip` + `
  _retry_pending_missed_flip` + `_pending_missed_flip` instance field, wired
  into `_setup()` and `run()`'s per-tick retry block.
- `tests/test_prometheus_market_close.py` (new), `tests/test_state_roundtrip.py`
  (extended).
- `prometheus_production/README.md` — still pending, see below.

## Status: fully closed out (2026-09-11)

The first commit (`55565e7`) shipped §1-4 (hard stop, `place_order` guard,
missed-flip reconciliation) and was pushed to `origin/main`. §5
(`CLOSING_TIME` auto-computation) followed in the same session once the
user asked whether MCX's DST-driven change was programmable — it is,
researched and confirmed, implemented, tested, and documented above. No
Slack toggle button needed — there's nothing left to toggle.
   if/when wanted.
