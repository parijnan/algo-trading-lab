# Typhon - NATGASMINI Supertrend strategy discovery

Same process as Prometheus (CRUDEOILM), Selene (SILVERMIC), Helios (GOLDPETAL): confirm the
instrument's basics, run a raw ST-multiplier signal-quality sweep with regime identification,
then calibrate exits. Instrument decided 2026-09-30 (user): **NATGASMINI**, MCX Natural Gas Mini.

## Step 1 - basics (2026-09-30)

**Lot size / tick size** (`data_pipeline/data/mcx_instrument_master.csv`): lot size 250 (mmBtu),
tick size 10 paise = Rs 0.10. **1 index point = Rs 250/lot, 1 tick = Rs 25/lot** - do not carry
over Helios's "Rs 1 per lot" framing (GOLDPETAL's lot size happens to be 1; NATGASMINI's is not).

**Rollover cadence**: monthly, same shape as CRUDEOILM/GOLDPETAL (unlike SILVERMIC's quarterly).
Expiries observed ~28-32 days apart.

**Margin**: user-observed ~Rs 14,000/lot "right now" (2026-09-30). Front contract (27OCT26
expiry) last close in the data is 299.7 (2026-09-28) -> notional Rs 74,925/lot -> ratio ~18.7%
of notional (`typhon_configs.MARGIN_SIZING_MULTIPLIER`). Single-point derivation, same caveat as
Selene's/Helios's own - and NatGas's margin steps with volatility far more than Gold/Silver/
Crude's: the very next contract out (24NOV26) was already trading ~12% higher the same day.
Re-check against a second observed margin figure before trusting this for sizing work.

**Tender margin**: user-confirmed 5 working days before expiry - same as CRUDEOILM/SILVERMIC/
GOLDPETAL (`TENDER_ROLL_TRADING_DAYS = 5`).

**Liquidity**: real and growing throughout the available history - median 1-min volume grew from
~4-8 in 2023 (thin, contract still maturing) to 40-86 by 2026, with a Fyers/AngelOne convention
mismatch worth noting (Fyers emits zero-volume placeholder bars in untraded minutes; AngelOne
skips them entirely, so the two aren't a like-for-like median). Session structure is real too -
volume is thinnest 10:00-15:00 IST (median 10-19/lot-min on the AngelOne front contract) and
peaks 17:00-20:00 IST (median 50-70+, US trading hours) - CRUDEOILM, already live, has an
all-history median of just 11, so NATGASMINI's liquidity is not a blocker even in its thinnest
session window, but morning-session next-bar-open fills sit on the thinner side of what's already
accepted elsewhere in this repo.

**DATA_START**: 2023-04-01. Fyers has NATGASMINI from 2023-03-14, but the first ~2.5 weeks are
thin (median volume 4-6) before the contract matured (12+ from 2024 onward). Skipped as an early
thin/partial stretch, same convention as Selene's own 3-month skip for SILVERMIC - open to
override, not a hard constraint.

**The Fyers void** (2026-04-01 to 2026-06-29) that hit Selene's SILVERMIC and Helios's GOLDPETAL
also hits NATGASMINI, same shape: the 2026-04-27 contract file effectively ends 2026-03-31, the
2026-05-26 file has almost no real data, and the next real contract data (2026-07-28 file) starts
2026-06-30. AngelOne's own per-contract mcx/ data covers 2026-01-30 onward, so it fills this gap
for the parity-backtest phase the same way it does for the other three strategies.

**A genuine ~600 print on 2026-01-27 is real, not a data error** - checked directly against the
1-min bars that day (895 bars, sensible volume throughout, price genuinely ranging 494.8-628.8):
a real winter cold-snap-style spike, the kind of event NatGas is known for ("widowmaker"). This
is exactly why regime analysis (Step 2) needs a seasonal cut, not just a split-date check.

### Roll-gap finding - the one structural departure from Prometheus/Selene/Helios's own Phase 2

**NATGASMINI's monthly roll gap is large**: measured at the actual early-roll effective-switch
date (`_p3._effective_contract_for_date`, 5 working days before expiry) across 40 rolls,
2023-04 through 2026-09, comparing both contracts' own price on the SAME reference date (the
last trading day both are still trading in parallel before the switch - see the bug note below):
median absolute gap ~5.6%, mean ~7.5%, up to ~24%. An order of magnitude bigger than CRUDEOILM's
typical sub-1% monthly roll gap, which is why Prometheus/Selene/Helios could all get away with a
naive spliced series (real per-contract prices concatenated at the roll date) for their own
Phase 2 sweeps.

Splicing naively here would inject a real price jump into the series at every roll (~monthly) -
a trade spanning a roll would be credited or debited fake P&L from the gap itself, corrupting
both the multiplier ranking and the regime read. **Fix: `typhon_data_loader.py`'s `back_adjust()`
additively back-adjusts every historical segment by the cumulative sum of every later roll's own
gap**, so the joined series has no roll-day jump. This is not an approximation for a
single-roll-spanning trade: additively shifting the outgoing segment by
`(incoming contract's price - outgoing contract's price)`, both read at the same pre-roll
reference date, exactly reproduces the real point-for-point P&L of "close the outgoing contract
at the roll price, open the incoming one at the roll price" (algebraic identity - verified,
see also the continuity check below). Ratio/Panama-style adjustment was considered and rejected:
it only preserves %-returns, not absolute Rs P&L, and this directory's sweep/calibration work is
P&L-ranking-based throughout, same as Prometheus/Selene/Helios's own.

Back-adjustment is confined to Phase 2 (this sweep) and Phase 3 (exit calibration) - the
eventual parity backtest and production engine will use real per-contract prices with real roll
execution (close old, open new at actually-transacted prices), same as Prometheus/Selene/
Helios's own parity backtests already do; back-adjustment only ever touches the earlier,
already-closed-out segments of history used for signal/exit discovery.

**A real bug was found and fixed while building this** (2026-09-30, caught by a continuity
check before any sweep results were trusted): the first version of `compute_roll_gaps()` measured
the incoming contract's price using its END-OF-DAY value on the switch date itself, rather than
on the same reference date as the outgoing contract's price. That baked a full day of the
incoming contract's own intraday movement into the "gap" (e.g. the 2025-02-18 switch measured a
+27.4pt gap, but the two contracts' actual prices on the last day they traded in parallel,
2025-02-17, were 312.1 vs 312.5 - a real gap of +0.4). Fixed by reading both contracts on the
SAME reference date (the last trading day before the switch). Re-verified after the fix: every
one of the 40 rolls now shows an actual jump in the back-adjusted series of at most ~15pt (normal
session-open noise, same order of magnitude as ordinary overnight moves elsewhere in the series),
down from jumps of up to ~58pt before the fix.

### Design decision carried forward

Phase 2 (`sweep_typhon.py`) and Phase 3 (exit calibration, not yet built) both run on the
back-adjusted series. Every trade_summary.csv's `entry_price`/`exit_price`/MAE/MFE columns are
back-adjusted prices, not real market prices, except in the most recent (current) contract
segment, which carries offset 0 by construction. This is flagged inline in both scripts'
docstrings so it isn't mistaken for real prices later when cross-checking against production.

## Step 2 - ST multiplier sweep + regime identification (2026-09-30)

`sweep_typhon.py`, full back-adjusted history (2023-04-03 -> 2026-09-28, 766k 1-min bars),
ST_PERIOD=10 fixed, ST_MULTIPLIER_GRID 1.0-6.0:

| mult | trades | win % | total P&L Rs | avg hold (hrs) |
|------|--------|-------|--------------|----------------|
| 1.0  | 5343   | 34.7  | 95,500       | 5.7            |
| 1.5  | 3423   | 34.5  | 72,050       | 8.9            |
| 2.0  | 2405   | 34.6  | 128,975      | 12.6           |
| 2.5  | 1824   | 35.6  | 109,075      | 16.6           |
| **3.0**  | **1398**   | **38.7**  | **173,950**      | **21.6**           |
| **3.5**  | **1159**   | **39.9**  | **180,950**      | **26.1**           |
| 4.0  | 974    | 41.8  | 162,925      | 31.0           |
| 4.5  | 846    | 39.5  | 114,200      | 35.8           |
| 5.0  | 734    | 39.5  | 103,750      | 41.3           |
| **5.5**  | **645**    | **42.0**  | **117,200**      | **47.0**           |
| 6.0  | 577    | 40.9  | 90,725       | 52.1           |

A clear peak at 3.0-3.5 on the naive full-window view, with a secondary local peak at 5.5 (same
plateau shape Helios's own sweep found for GOLDPETAL, coincidentally at similar multiplier
values). Win rate rises monotonically with multiplier (fewer, longer, more selective trades) up
to ~40-42% around 4.0-5.5, never above that - consistent with a genuine trend-catching signal,
not overfit noise (Helios's own found pattern, plan §4).

### Regime identification (`regime_typhon.py`), shortlist [3.0, 3.5, 4.0, 5.5]

**Seasonality (winter Nov-Feb vs. rest of year)**: winter trades earn MORE per trade at every
shortlisted multiplier - avg P&L/trade roughly 1.6x-3x the rest-of-year average (e.g. mult 4.0:
winter avg Rs 309.6/trade on 279 trades vs. rest-of-year Rs 110.1/trade on 695 trades). This
matches the real, expected NatGas seasonal driver (the 2026-01-27 ~600 print, Step 1) - a
structural tailwind, not a risk to hedge against. Only ~28-34% of trades fall in the 4-month
winter window, so the full-window P&L is NOT dominated by winter alone, but winter is
disproportionately profitable per trade.

**Realized-volatility terciles** (trailing 20-day, back-adjusted daily returns): the HIGH-vol
tercile earns more per trade than low/mid at mult 4.0 (Rs 275.5 vs Rs 111.8/45.5) and 5.5 (Rs
237.0 vs Rs 224.8/112.0), consistent with a trend-following signal doing its best work exactly
when the market is actually trending/volatile. Less pronounced at 3.0/3.5, where low and high
buckets are closer together.

**Walk-forward split (2025-01-01, secondary check)**: every shortlisted multiplier is WEAKER
post-2025-01-01 than pre - avg P&L/trade roughly halves at mult 3.0 (Rs 191.8 pre -> Rs 60.4
post) and 3.5 (Rs 207.6 -> Rs 106.3), holds up somewhat better at 4.0 (Rs 188.9 -> Rs 145.5) and
5.5 (Rs 244.3 -> Rs 118.9). Same shape as Helios's own finding (naive full-window pick =/= the
walk-forward-robust pick) - worth weighing at the exit-calibration stage rather than picking
3.0/3.5 on the full-window number alone.

**Open, not yet decided**: which multiplier(s) carry into Step 3. 3.0/3.5 win on full-window
P&L; 4.0/5.5 look more walk-forward-robust. Flagged for the user rather than picked unilaterally
- same posture as Helios's own multiplier shortlist decision (plan §4g there).

## Step 3 - exit calibration (2026-09-30)

User decisions: broad shortlist (3.0/3.5/4.0/5.5) into calibration, not a single pre-committed
multiplier; and run two candidates with their OWN unit sizes side by side rather than picking a
tranche design upfront - "1 lot = 1 unit, no scale-out" (Selene's shape) vs "2 lots = 1 unit,
Prometheus-style scale-out" (`exit_calib_typhon.py`, back-adjusted series, same staged
methodology as Prometheus/Selene/Helios's own).

Before trusting the results: the SL grid's chosen value landed at the grid's tightest tested
point (0.5%) at every multiplier - checked directly (swept 0.1%-0.8% in finer steps) and
confirmed it's a genuine noisy plateau (0.3%-2.5% roughly tied, not a hard climb toward the
edge) rather than a boundary-exhaustion artifact like Helios's own T3/T4 crash. Similarly
checked T1's landing at 0.5% (three of four multipliers) - genuinely a local peak (values below
it are clearly worse), not an artifact.

**Full-window result: scale-out beats SL-only at every single multiplier tested** - real,
data-backed, not assumed (Calmar% 6.55 vs 6.28 at 3.0; 11.42 vs 8.40 at 3.5; 16.91 vs 13.32 at
4.0; 14.61 vs 11.73 at 5.5). Full-window winner: **mult 4.0, scaleout_n2, SL 0.5%, targets
[0.5%, 20%], Calmar% 16.91**. Both chosen targets show a consistent tight-T1/wide-T2 shape at
3.5/4.0/5.5 (a quick partial profit-take on lot 1, letting lot 2 ride to the trend-flip) -
mult 3.0 alone landed both targets wide ([15%, 20%]), the one multiplier whose shape doesn't
match the other three.

**Walk-forward check on the calibrated P&L (pre/post 2025-01-01, same split as Step 2) repeats
and sharpens Step 2's own finding**: mult 3.0's Calmar% collapses post-2025 (10.11 -> 2.04
sl_only, 10.11 -> 2.31 scaleout) and mult 3.5 also drops hard (9.14 -> 3.98 / 8.15 -> 5.09).
Mult 4.0 holds up much better, especially scaleout (9.11 -> 8.21, barely moves). **Mult 5.5 is
the most walk-forward-robust of all four - its Calmar% actually IMPROVES post-2025** (4.57 ->
10.37 scaleout, the best post-period number in the whole table), despite being the weakest
full-window candidate. Same shape as Helios's own finding: the naive full-window pick (3.0/3.5)
is not the walk-forward-robust one (4.0/5.5).

**Before deciding, the user pushed back on trusting Calmar% alone** ("Stop only looking at
Calmar... total trades, total returns, win rate, avg winning trade, avg losing trade, max
drawdown etc"). Full stats for all 8 (multiplier x candidate) combos, full-window:

| mult | candidate | trades | win% | avg win | avg loss | profit factor | max DD | max DD as % of return | total return |
|------|-----------|--------|------|---------|----------|----------------|--------|------------------------|--------------|
| 3.0 | sl_only | 1399 | 26.9 | Rs 1,733 | -Rs 479 | 1.33 | -Rs 17,908 | 11.0% | Rs 163,038 |
| 3.0 | scaleout | 1399 | 26.9 | Rs 3,495 | -Rs 959 | 1.34 | -Rs 35,817 | 10.6% | Rs 336,978 |
| 3.5 | sl_only | 1160 | 32.5 | Rs 1,956 | -Rs 706 | 1.34 | -Rs 20,961 | 11.2% | Rs 187,319 |
| 3.5 | scaleout | 1160 | 42.5 | Rs 1,973 | -Rs 1,115 | 1.31 | -Rs 20,032 | 8.8% | Rs 228,522 |
| 4.0 | sl_only | 975 | 26.7 | Rs 1,984 | -Rs 496 | 1.46 | -Rs 12,107 | 7.5% | Rs 161,825 |
| 4.0 | scaleout | 975 | 33.9 | Rs 2,060 | -Rs 998 | 1.48 | -Rs 13,295 | 6.0% | Rs 220,027 |
| 5.5 | sl_only | 646 | 31.3 | Rs 2,492 | -Rs 781 | 1.45 | -Rs 15,419 | 9.8% | Rs 156,661 |
| 5.5 | scaleout | 646 | 39.3 | Rs 2,760 | -Rs 1,566 | 1.43 | -Rs 16,952 | 8.0% | Rs 212,520 |

Profit factor comfortably >1 everywhere (1.31-1.48), no candidate fragile; every combo has the
classic trend-following shape (low win rate, average win 2-4x average loss). Mult 4.0 has the
smoothest equity curve of the four (lowest max-drawdown-as-%-of-return, 6.0-7.5%) and the
highest raw total return of any multiplier for both candidates. Nothing here changed the
earlier walk-forward tension (4.0 = better full-window + solid robustness; 5.5 = weaker
full-window but most robust) - it confirmed neither candidate has a hidden red flag.

Before trusting the 0.5% SL/T1, its robustness was checked directly against a flat round-trip
slippage cost (0/1/2/3/4 ticks = Rs 0/25/50/75/100 per lot, since Phase 2/3 costs are otherwise
excluded throughout this plan): **0.5% remains the best SL choice at every cost level tested**,
though total edge erodes substantially under stress (Rs 161k -> Rs 64k at 4 ticks, ~60%). T2 at
20% is confirmed to functionally mean "ride to the trend-flip" for the overwhelming majority of
trades - checked directly, only 0.1% of trades' raw-signal MFE ever reaches 20% (median MFE
1.30%, p90 4.84%) - not a flaw, a legitimate finding that a near-uncapped second leg beats a
tighter one.

**~~DECIDED~~ PROVISIONAL (2026-09-30, same day): mult 4.0, 2-lot scale-out, SL 0.5%, T1 0.5%,
T2 20%.** Downgraded from "decided" the same day it was made -- see the "percentage-bug
correction" section immediately below. Every percentage-based number in this Step 3 section
(and Step 2's regime/vol-tercile numbers) is SUPERSEDED, kept only as a record of what was
found and why the approach changed, not as a number to trust. The per-lot comparison (scale-out
gives up ~32% of per-lot return for ~45% less drawdown at mult 4.0, Calmar/lot still favours it)
is a genuine correction worth carrying forward, independent of the percentage bug.

### Percentage-bug correction (2026-09-30, same day as the "decision" above)

Found via advisor review, verified directly before acting on it: additive back-adjustment
preserves POINT distances across history, not percentages. Every SL/target grid, `calmar_pct`,
and MFE-reach-rate in Step 3 (and the realized-vol terciles in Step 2) was computed as "X% of
the back-adjusted entry price" -- but for an old trade the adjusted price sits far above the
real price (the earliest offset is +338.4 points on a real price of ~195), so "0.5%" tested on
2023 trades was actually ~1.35% of the real price, while a current trade's offset is ~0 so
"0.5%" is genuinely 0.5%. Verified by computing `back_adjust_offset` at each trade's own entry
bar and confirming the ratio grows from 1.0 (now) to ~2.7 (2023).

**What this does and doesn't affect**: Step 2's own raw walk-forward (points/Rs P&L, no SL or
targets) is UNAFFECTED -- additive back-adjustment preserves points exactly, so "mult 3.0/3.5
weaken post-2025" stands on its own. It is specifically Step 3's CALIBRATED walk-forward
(percentage-based SL/target levels) and the slippage-robustness check run afterward (which
ranked those same percentage-of-adjusted-price SL values) that are confounded -- older trades
were tested with an effectively much wider, more forgiving stop than newer trades, purely from
this measurement artifact, which plausibly (not certainly) contributed to mult 3.0/3.5 looking
weaker walk-forward than they may really be.

**Decision, per the user (2026-09-30)**: rather than patch `exit_calib_typhon.py` to compute
percentages against the real (unadjusted) entry price and rerun, build Step 4 (the
production-parity backtest) now instead -- it uses real per-contract prices with real roll
execution throughout, so it never needs back-adjustment and sidesteps the bug entirely, and is
needed for production regardless. Advisor-reviewed scope for that build:
- Calibration must run inside the roll-aware simulator (a trade spanning a roll is flattened and
  possibly reopened with a basis-recalibrated stop; whether it's even still open at the roll
  eve depends on the SL/targets) -- not a flat slice like `exit_calib_typhon.py`'s current one.
  ~300+ simulator runs across the shortlist x candidates; time Helios's own parity script first
  to decide whether a per-segment overlay (walk each contract segment, translate levels across
  each roll via `basis_price`, prove it matches the full simulator on 2-3 grid points) is needed.
- Call `hestia_core.roll_policy` functions directly (`effective_contract`, `coincident_flip`,
  `decide_rollover`, `basis_price`, `reopen_plan`) so the backtest and the future live engine
  can't drift apart -- same discipline as Prometheus/Selene/Helios's own parity backtests.
- The 2-lot scale-out candidate needs lot1/lot2 roll handling (`reopen_plan` covers this) that
  neither Helios's nor Selene's own parity scripts needed (both single-tranche) -- check what
  Prometheus's own parity backtest already has for this before writing anything fresh.
- Regime metrics (daily returns, MFE%) must be computed WITHIN each contract, not off the
  effective-contract close across a roll (which reintroduces the same fake-jump problem
  back-adjustment existed to fix) -- use the prior close of the SAME contract on a switch day.
- Scope: rerun the raw pass (no SL/targets) across the full 1.0-6.0 grid (~11 runs, cheap) to
  reconfirm which multipliers survive, then calibrate only the survivors. The parity window
  stops 2026-03-31 (the Fyers void) then resumes on AngelOne data -- the post-2025 walk-forward
  sample will be shorter than Step 2's own, worth flagging in the eventual report.
- This is a Helios-parity-sized build (a few hundred lines of roll state machine), not a patch.

## Step 5 - the production engine (2026-09-30)

Built `typhon_engine/` (engine.py, levels.py, state.py, engine_configs.py, __init__.py), ported
from `selene_engine/` -- Typhon's decided shape (single lot, one target alongside the stop) is
structurally much closer to Selene's/Helios's own single-lot engines than to Prometheus's 2-lot
scale-out (Rule 7, per-lot partial exits, lot1/lot2 coordination) -- none of that machinery is
needed here, since a target hit on a single lot exits the whole position exactly like the stop
already does (`_send_exit_all`, not `_send_exit_lot`).

The only genuinely new capability: `levels.py`'s `build_levels()` gained a `target_price` field
alongside `sl_price`, computed from the SAME `threshold_price` the stop already uses (a normal
entry's real fill, or a roll's historical-basis recalibration price) -- so the target survives a
roll's basis translation automatically, no separate code path. `_check_stop` now checks the
target after the stop (stop wins if both were somehow reachable, though a single LTP can never
actually cross both sides of a real position at once). Exit-reason plumbing reuses
`ExitReason.OTHER` with a `'target'` descriptive string, exactly Prometheus's own convention for
its own target legs -- no change to the shared `hestia_core.interface.ExitReason` enum needed.

**Porting the test suite (mirrored from Selene's own 4 files, ~780 lines) surfaced two real
categories of bugs, both fixed:**
1. Mechanical sed-port artifacts: several assertions hardcoded Selene's own 3% SL / 8-4 margin
   divisor-multiplier pair rather than deriving from `DEFAULT` -- fixed to reference
   `DEFAULT.sl_pct`/`DEFAULT.target_pct`/`DEFAULT.margin_contract_value_divisor`/
   `DEFAULT.margin_sizing_multiplier` directly, so a future config change can't silently
   desync the tests again.
2. A genuine scripted-price-path incompatibility: Typhon's decided SL (0.8%) is far tighter than
   Selene's (3%) or Helios's (1.6%) -- two tests (`test_ledger_holds_a_position_the_engine_never_
   knew_about`, and implicitly the shared `FLIP_PATH`/`HOLD_PATH` fixtures other roll tests reuse)
   seeded a position close enough to the scripted path's own opening price that the TIGHT stop
   fired almost immediately, turning an intended ledger-adoption/roll test into an unrelated
   stop-out test. Fixed by widening the seeded entry price's margin from the path's opening level
   (99.0 -> 99.5, matching the margin `seed_bearish()`'s own default already used successfully in
   the roll tests) rather than changing the shared price paths themselves.
- `typhon_configs.py`'s own `PROVISIONAL_*` constants (from the percentage-bug correction, Step 3
  above) were themselves stale by the time the engine was built -- they still held the OLD
  back-adjusted decision (mult 4.0, SL 0.5%, 2-lot). Replaced with `DECIDED_*` constants holding
  the real, parity-calibrated Step 4 values (mult 3.0, SL 0.8%, target 15%, 1 lot) -- a
  `test_engine_config_matches_the_decided_backtest_config` test (ported from Selene's/Helios's
  own equivalent) pins the engine's config to these so the two can never silently drift again.
- All target-specific behaviour (a target hit exiting the whole position, the stop being checked
  first, direction-correct target math, a target surviving both a fallback-roll basis
  recalibration and a coincident-flip re-entry) has no Selene/Helios precedent to port from --
  written fresh and mutation-tested (reverted the target-setting/checking code, confirmed the new
  tests catch it, restored) rather than assumed to work by analogy.

Registered in `hestia_config.ENGINES['typhon']` (`NATGASMINI`, factory
`typhon_engine.engine:build`, `enabled=False, paper=True`) -- not yet deployed anywhere. Full
suite (63 engine-specific tests + the wider Hestia regression suite) green.

## Step 6 - replay_check.py, the trade-for-trade gate (2026-09-30)

Built `typhon_engine/replay_check.py` (ported from `selene_engine/replay_check.py`): the engine replayed on FakeHestia over real NATGASMINI 1-minute data, 2026-09-02 -> 2026-09-29 (where AngelOne-only data == what the parity backtest used), compared against `parity_decided_trades.csv`/`parity_decided_legs.csv`. **Final result: 61/61 oracle decisions reproduced exactly, fill price gap 0.00 mean/max.** One trailing replay entry (09-29 18:30 bearish) has no oracle row, proven benign (below). Getting there found and fixed five real issues, none in the engine's decision logic:

1. **Expired-contract token drop**: `mcx_instrument_master.csv` is a live snapshot with no row for the already-expired Sep-2026 contract, so the token lookup silently dropped it (price file was on disk). Token is pure ledger bookkeeping in FakeHestia, so a synthetic `SYN<expiry>` token is used as fallback.
2. **Registering the full multi-year archive broke front-contract selection**: `roll_policy.effective_from_days_left()` sorts every REGISTERED contract by expiry and takes the two earliest as front/next; `count_trading_days_inclusive` clamps past expiries to 0, so an ancient contract displaced the real one and the engine went dead silent after its first exit. Live Hestia only ever knows 2-3 current contracts, so this is a replay-tool-only problem. Fix: register only contracts with expiry >= REPLAY_START (the full dict is kept as a lookup table for the carried-leg seed). Shared code untouched.
3. **Harness price feed is coarser than the backtest's exit model**: `ReplayData._price_at` exposes only a bar's open (first 30s) then close to a poller, never the intrabar high/low that the oracle's `scan_exit()` uses. With Typhon's tight 0.8% stop, a wick that touches the stop and retreats is invisible to the replay, so exits fire later (up to hours) and at worse prices than the oracle's idealized fill, sometimes cascading into a later trend-flip exit instead. A wider time tolerance was tried first and rejected (its "no gap over ~90s" comment was falsified by 20-60 minute gaps). Replaced with `predict_live_exit()`, which mirrors the harness's open/close sampling exactly (including the per-session first-minute guard) and is what exits are compared against; verified exact on timestamp AND price. This is a limit of the test harness, not the engine: production polls real broker LTP ticks, which fixes stop-detection TIMING, though a stop still fills at market rather than exactly at the stop level.
4. **Carried-position seed** now also sets `target_price` (Selene's/Helios's seed only had a stop).
5. **Open-at-end position missing from the oracle CSVs**: `simulate()` only writes a leg in `close_pos()`, so a position still open when data ends (`legs.attrs['open_at_end']` is True) has no row in the trades/legs files. A traced copy of `simulate()` confirmed the oracle opens the same 18:30 bearish position the engine does. Re-running against a later end date would show it as an ordinary matching entry.

Known approximation: the predictor's guard start for the first bar uses entry_ts + 1 min and treats a >60 min bar gap as a session boundary; a rare edge (a stop breaching in the first bar of a session the position was entered at the very open of) would surface as a residual diff to investigate, not silently pass. Multi-leg (rolled) trades fall back to the oracle's raw exit.

**Next (not yet asked)**: paper deployment alongside Selene/Helios, per the established precedent.

## Re-run on the Fyers-filled data, 2026-10-06

The Fyers void of 2026-04-01 to 2026-06-29 was filled on 2026-10-06 (63 of 64 weekdays; cross-checked against Angel One for NATGASMINI: 63 of 64 void days compared, median 97% of minute closes identical, none more than 0.5% apart), the September contracts were pulled from Fyers too, and the data runs to 2026-10-05. The loaders prefer Fyers wherever the effective contract's file has the date, so the 2026-04 to 06 stretch now comes from Fyers. `sweep_typhon.py`, `exit_calib_typhon.py`, `exit_calib_parity_typhon.py` and `parity_backtest_typhon.py` were re-run, and the decided-config oracle (`parity_decided_trades.csv`/`parity_decided_legs.csv`, produced by an ad-hoc call to `simulate` at mult 3.0 / SL 0.8% / target 15% / one lot through `PARITY_END_EXTENDED`, not by a committed script) was regenerated the same way. The earlier steps stay as the record of their own date.

**The decided configuration (mult 3.0, SL 0.8%, target 15%, one lot) stands, and the real-price calibration picked exactly the same parameters again.**

| | Before | After |
|---|---|---|
| Raw sweep, mult 3.0 / 3.5 (trades, points) | 1,398, 695.8 / 1,159, 723.8 | 1,403, 730.0 / 1,159, 716.8 |
| Parity calibration winner, mult 3.0 | SL 0.8, target 15; 1,392 trades, win 27.2%, avg win/loss 1,845 / -533 Rs | the same; 1,395 trades, win 27.3%, 1,846 / -534 |
| Parity calibration winner, mult 2.5 | SL 1.6, target 6.0 | SL 0.5, target 6.0 (not the decision) |
| Decided config (trades, points, max DD, Calmar) | 1,392, 659.9, -70.5, 9.36 | 1,395, 669.3, -70.5 (2025-11-27), 9.50 |
| Entries before 2026-04 | 1,199, 642.8 | identical |
| Entries 2026-04 to 06 | 88, 57.1 | 94, 60.9 |
| Entries from 2026-07 | 105, -40.0 | 102, -34.4 |

Only the filled stretch changed: about 22 old trades and 25 new ones differ, all in April and May. **The `replay_check` gate passes against the regenerated oracle: 57 of 57 live decisions reproduced, fill price gap 0.00, with one replay-side extra (the known still-open-at-window-end entry on 2026-09-29).** (The oracle blend and the replay both read the same loader for this instrument, which is why the September Fyers contract does not disturb it the way it does Helios's replay test.)
