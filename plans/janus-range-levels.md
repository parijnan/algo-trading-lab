# Plan: Janus range-based Camarilla levels (user design 2026-10-12)

**Status: designed, not built.** Research only, like the rest of Janus: nothing here touches live code, Hestia or Delos. This plan changes only where the High and Low that feed the Camarilla formulas come from; the formulas, setups, engine and entry modes in `plans/janus-camarilla-research.md` are unchanged. The idea borrows the setter-candle logic from `plans/range-detection-research.md` (sections 1 and 3), used on daily bars of the MCX contracts.

## 1. The change

Today the levels for day D come from the previous session's High, Low and Close. The new source replaces High and Low with the bounds of a price range tracked on daily bars, and keeps Close as the previous session's close.

- **Close:** always the previous session's close (unchanged).
- **High and Low:** the range high and range low as they stand after the previous session's close, known before the open and fixed for the whole day.
- **Formulas:** unchanged (R1-R4 = C plus or minus R times 1.1 over 12, 6, 4, 2; R5 = C times H over L; R6 and R7 by the 1.168 extension; S-side mirrored), with R now the range width (range high minus range low).

## 2. Range rules (the setter-candle logic, stripped down)

Daily bars, one per session, built from the contract being traded that day.

1. The first session of a contract's history is a setter: the range is that bar's High and Low.
2. After each session's close, compare the close with the current range. A close strictly above the range high or strictly below the range low starts a new range: that session's bar is the new setter and its High and Low become the bounds. Close outside is the only trigger.
3. Otherwise (close inside, bounds inclusive) the range extends to include the session's High and Low (wick extension), so the range can only widen while it lasts.
4. No confirmation bars (the Nifty plan's `--breakout-confirm` is set to zero), no minimum range length (a one-bar range already supplies levels), no gap-open rule.

Consequences that follow directly from the rules:
- On a setter session's following day the levels equal today's previous-session levels (the range is that one bar), so the new source only differs from the current rule on days when the previous session closed inside the existing range.
- A breakout beyond R4 or S4 that closes outside the range makes the next day's levels come from that one day's bar alone (new setter).
- While a range ages, R grows, so R3/S3 and R4/S4 sit further from C and the stop distance (0.275 R) widens; the reward multiples in stop-distance units stay the same. C is no longer near the middle of the High-Low span, so the levels can be asymmetric around the range.
- No look-ahead: the levels use only information up to the previous close, with no confirmation lag and no provisional bounds.

## 3. Contract handling (decided)

Janus is intraday with no rollovers. The range is derived from the contract planned for trading that day, using that contract's own sessions, and the Camarilla levels are computed from it. The range therefore restarts whenever the traded contract changes, and the first session on a contract has levels identical to the current rule.

**Working assumption (to confirm with the user):** a contract's range starts at the first session with data for that contract, so ranges are built from at most that contract's available history (roughly a month or less if its data starts when it becomes front month). Earlier back-month sessions, if any exist, are left out.

## 4. Implementation outline (to do, in this order)

1. Add a level-source switch in `janus_backtest/janus_levels.py` (and the setting in `janus_configs.py`, nothing hardcoded): `previous_session` (default, the control) and `range_setter`. Pure function over the per-contract daily bars.
2. Tests (`tests/test_janus_*.py`), each behaviour mutation-checked as elsewhere: first session is a setter; strict close above and below starts a new range; a close exactly on a bound stays inside; extension by a wick; extension never narrows; breakout then next-day reset to the single bar; Close always the previous session's; range restarts at a contract change; the output equals the current rule whenever the range is one bar.
3. Descriptive pass before any P&L: per instrument, the share of sessions whose levels differ from the current rule, the distribution of R(range) over R(previous session), the distribution of range age, and how often a session closes outside the range (setter rate).
4. Rerun the day-simulation engine (`janus_engine.py`) on the new levels with the touch entry as the control and plain thirds (A0) first; same costs grid, same two bar orders. Close-armed entries only if wanted afterwards. Compare against the previous-session control on the same sessions, one variable changed.
5. Record results in this plan and the README. Nothing committed without the user's request.

## 5. Open points and caveats

- The contract-start assumption in section 3.
- All earlier Janus results (thirds, re-buy, counter trade, entries) were produced with previous-session levels and are not guaranteed to carry over; they are the control, not the answer.
- The provisional rules in the Janus plan (first-minute rule, 60-minute entry cut-off, 15-minute hard exit) and the unknown real costs still apply and still decide viability.
- The parked list in the Janus plan (real costs, marketable entry at the next bar's open, breakout-only book, cap test, stop-trigger grid, reclaim-waiting counter) is unchanged; this change is an additional line of work, not a replacement.
- A wider R means fewer touches and a larger stop as a share of price, so trade counts and the cost break-even per leg will shift in a direction not predictable in advance.
