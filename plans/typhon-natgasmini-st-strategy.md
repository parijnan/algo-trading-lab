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

**DECIDED (2026-09-30): mult 4.0, 2-lot scale-out (Prometheus-style), SL 0.5%, T1 0.5%, T2 20%.**
Calmar% 16.91 (best full-window of all 8 combos), holds up on walk-forward (9.11 -> 8.21), and
the 0.5% SL/T1 confirmed robust to realistic slippage stress rather than a zero-cost artifact.
This also settles the unit-sizing question: 2 lots = 1 unit, scale-out beat SL-only at every
multiplier tested.
