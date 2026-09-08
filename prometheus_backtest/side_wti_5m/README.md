# Prometheus — WTI 5-minute side project (2026-09-08)

**Side project, not a validation.** This is a loose approximation cross-check of Prometheus's
already-decided Phase 3 combo against WTI crude oil data — explicitly *not* the same kind of
cross-validation as `phase3_crudeoil/` (same commodity, same exchange, same data pipeline). WTI
here is NYMEX/Globex, a different exchange from MCX, and the data source (Kaggle) is of unknown
provenance and quality. Kept out of the numbered `phase*/` folders on purpose — this doesn't feed
into any live production decision.

## Data

`data_pipeline/data/wti/WTI-Crude-Oil-5-Minute-OHLC-Candles.csv` (user-provided, ~110MB, 1.53M
rows). Schema: `time,date,instrument,granularity,open,high,low,close` — UTC timestamps, `OIL`
instrument tag, `5Min` granularity, **no volume column**. Range: 2011-09-23 to 2026-04-29 (~14.6
years) — far longer than CRUDEOILM's own ~6.5-month history, so this is as much a long-run,
multi-regime robustness check (2014-16 crash, 2020 COVID negative-price event, 2022 spike all fall
inside this window) as it is a cross-instrument one.

**Real data-quality finding, not assumed:** the raw file is not a genuinely gapped exchange
session — closed-market periods are filled with flat (`open==high==low==close`, repeating the
last real price) synthetic bars instead of being absent. Confirmed by direct inspection: Saturday
is 100% flat, Sunday 93.8% flat; real weekdays sit at 8.7-17.6% (Friday and Monday elevated from
the week's close/reopen transition). This is the same root contamination
`prometheus_backtest/data_loader.py::load_futures_1min` already drops Sat/Sun bars for on MCX data
— `wti_data_loader.py` applies the identical fix (`dayofweek < 5`), dropping 438,048 of 1,531,906
rows (28.6%). Residual weekday flat-bar rate after that: 11.0% — plausible for real illiquid
periods plus week-transition artifacts, not filtered further (see Caveats).

No volume field exists in the source at all — irrelevant to the signal itself (`compute_st` is
pure OHLC), but means no participation/slippage analysis is possible here, unlike CRUDEOILM's.

## Method

Reuses `prometheus_backtest/data_loader.py`'s `resample_ohlcv`/`compute_st` unchanged (both
instrument-agnostic — the whole reason this side project is tractable at all). Cannot reuse
`load_futures_1min` — it assumes MCX's per-contract-expiry file layout and front-month
de-duplication, which a Kaggle continuous series has no equivalent of; `wti_data_loader.py` is a
new loader producing the same output shape instead. `LOT_SIZE=1` throughout (no currency/margin
concept applies to a hypothetical WTI position) — every `*_pnl_rs`-named column below is actually
**USD points**, not Rs; kept for schema parity with the phase3 pipeline this mirrors, not because
any Rs conversion happens.

Tests the two already-decided Phase 3 combos **unchanged** — `(mult, sl%, t1%, t2%)`:
`(2.0, 2.2, 2.0, 5.0)` (live production) and `(2.5, 1.0, 1.25, 4.0)` (runner-up). No recalibration
— this is a stress test of whether the decided combo holds up directionally on a different market,
not a fresh optimization.

`_simulate_trade_detailed` (`bespoke_wti.py`) and `_summarize` (`analysis_wti.py`) are **copied**
from `phase3/bespoke_2lot_p3.py`/`phase3/exit_calib_p3.py`, not imported — both read `LOT_SIZE` at
module scope from `configs_p3` (=10, CRUDEOILM's), which would silently apply the wrong value here
if imported directly. `_target_fill_price`/`_stop_fill_price` (confirmed pure — no configs
coupling) and `save_trade_paths_p3` (confirmed pure — takes a trades df + any bar-indexed df) ARE
imported directly from the phase3 pipeline, unchanged.

## Result: the combo does not clearly transfer

| Multiplier | SL% | T1% | T2% | Trades | Win rate | Total P&L (pts) | Max DD (pts) | Calmar |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2.0 (live) | 2.2 | 2.0 | 5.0 | 17,177 | 34.5% | +6.5 | −135.1 | 0.05 |
| 2.5 (runner-up) | 1.0 | 1.25 | 4.0 | 12,937 | 35.5% | −41.8 | −146.1 | −0.29 |

Both are effectively flat-to-negative — Calmar near zero or negative, a sharp contrast with
CRUDEOILM's own 10.21/10.78 on the same combos (`prometheus_backtest/README.md`'s Phase 3 table).

**Why, as far as this can tell:** the overwhelming majority of trades exit via `trend_flip` rather
than ever reaching either threshold — 88.2% for mult 2.0 (15,139/17,177), 62.7% for mult 2.5
(8,112/12,937), vs. `target1`/`stop_loss` combined making up the rest. The signal chops far more
on this data than on CRUDEOILM at the same (period, multiplier) setting — average trade size is
tiny relative to entry price (mean win ~$1.2, mean loss ~$0.6-0.7, on a ~$69 average entry), so
most trades reverse well before the 2.0-2.2%-scale SL/target levels calibrated for CRUDEOILM's own
volatility profile are ever in reach. Trade frequency is also much higher per year here (~1,176/yr
implied vs. CRUDEOILM's own ~644/yr) — consistent with a genuinely choppier signal on this
instrument/data combination, not a sample-size artifact (17,177 and 12,937 trades are large
samples).

**Can't fully separate two possible explanations, and don't try to here:** (1) WTI genuinely trends
differently than CRUDEOILM at this specific signal setting — plausible, different markets; or (2)
residual noise in this specific Kaggle series (the 11% weekday flat-bar rate, and whatever
compression/interpolation produced the weekend-fill artifact in the first place) inflates spurious
flips beyond what real WTI price action would produce. Distinguishing these needs either a cleaner
WTI data source or a recalibration pass on this data — both out of scope for a side project whose
whole premise was "approximation, better than nothing."

## Caveats

1. **Session structure**: WTI's near-continuous session has no MCX-style defined open/close to
   anchor entry-time buffers against — the whole weekday stream is treated as continuous, no
   `MIN_ENTRY_TIME`-equivalent gate exists here.
2. **Currency**: USD points throughout, not Rs — see Method above.
3. **Bar granularity**: 5-min source data (vs. the existing pipeline's 1-min) — coarser fill-price
   and MAE/MFE-path resolution, on top of the cross-market gap itself.
4. **Data provenance/quality**: this Kaggle file's `OIL`/`5Min` tagging gives no indication of
   whether it's genuine NYMEX exchange data, a CFD/spot proxy, or some other derived series — the
   weekend synthetic-fill behavior found here suggests real processing/interpolation happened
   somewhere upstream, quality otherwise unverified beyond the checks in `wti_data_loader.py`.
5. **No recalibration**: literal port of the already-decided combo(s) — see Method above.
6. **No transaction costs**: `SLIPPAGE_ENABLED=False`, matching every other phase.

## Running

```bash
python prometheus_backtest/side_wti_5m/bespoke_wti.py    # both combos, ~a few minutes (1.09M weekday bars)
python prometheus_backtest/side_wti_5m/analysis_wti.py   # prints + saves the summary table above
```
