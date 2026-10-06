# Plan: Prometheus to ST multiplier 2.5 (live, 2026-10-05)

**Status: config changed 2026-10-05 on the user's decision; goes live at the next Hestia restart after the user flattens the open position.** Only values change, no logic.

## Change

`prometheus_engine/engine_configs.py` and (kept equal, rollback-only) `prometheus_production/prometheus_configs.py`: ST multiplier 2.0 -> 2.5, SL 2.2 -> 1.0, T1 2.2 -> 1.25, T2 5.0 -> 4.0 (percent, flat_pct second target). Everything else unchanged (period 10, 2 lots per unit, provisional-bar margin 0.15, sizing from Hestia's config).

## Why (data to 2026-10-01, CRUDEOILM, per unit, no slippage unless stated)

- Mult 2.0 with its exits: 447 trades, P&L 189,349, max drawdown -26,331 (2026-09-29), Calmar 7.2, in a 27-trade slide of -22,891 from the 2026-09-22 peak (win rate 26%, 93% trend-flip exits).
- Mult 2.5 with its calibrated exits: 332 trades, P&L 128,989, max drawdown -11,973, Calmar 10.8. Entries since the 2026-09-04 decision: 2.0 made -171, 2.5 made +22,679 (CRUDEOIL full contract: -20,631 vs +197,521).
- Cross test (signal x exits): signal 2.5 beats signal 2.0 with either exit set, so the signal, not the exits, drives the gap. Exits: no recalibration.
- Plateau check on 2.5's exits (51 combinations): stop 0.8% to 1.2% is a broad good region (Calmar 9 to 12), T1/T2 matter little, all 48 stop x T1 cells profitable overall, in both halves and since 2026-09-04. 1.0 / 1.25 / 4.0 is not a spike; best cell (1.2 / 1.25) is within noise and was not chased.
- Sized simulation with slippage (CRUDEOILM): 2.0 returns more (+152.5% vs +100.5%) but draws down more (-24.6% vs -20.4%) with P(drawdown > 40%) 5.4% vs 0.5%. The case for 2.5 is drawdown, tail risk and the recent regime, not total return (March and April carried 2.0).

## Known caveats (accepted by the user)

- About four weeks and 33 trades of out-of-sample evidence. A 1.0% stop risks roughly half the rupees per stop of 2.2% at the same units.
- No parity replay for 2.5 (the user judged the engine logic unchanged, so the 2.0 replay verification carries over).
- 24 tests pin the old numbers and fail until updated: `tests/test_prometheus_engine.py` (6: hardcoded 2.2% / 5% levels), `tests/test_prometheus_engine_roll.py` (3, same), `tests/test_prometheus_engine_replay.py` (15: they replay recorded live days produced at mult 2.0, so they need an explicit 2.0 config). Deliberately not touched at the user's instruction; the full suite is red on these until then.
- The open position at the switch is flattened by the user beforehand, so the engine starts flat and enters on the next 2.5 flip.

## Rollback

Revert the two config files, push, pull on Delos, restart (the `hestia-restart` skill). A position open under 2.5's levels would keep them until it exits.

## Addendum 2026-10-06: figures re-run on the Fyers-filled data

The figures above are from the first run (data to 2026-10-01, Angel One in the 2026-04 to 06 void). After the Fyers void was filled (63 of 64 weekdays covered; cross-checked against Angel One: median 85% to 97% of minute closes identical) and the data extended to 2026-10-05, the same pipeline gives, CRUDEOILM per unit: mult 2.0 452 trades, 42.26%, Rs 150,114, max drawdown Rs -35,896, Calmar 4.18; mult 2.5 338 trades, 47.04%, Rs 115,467, max drawdown Rs -14,856, Calmar 7.77. Entries since 2026-09-04: Rs -15,828 vs Rs 14,599. With sizing and slippage mult 2.5 now also leads on Calmar (3.82 vs 3.09), and P(drawdown > 40%) is 11.59% vs 0.67%. The decision stands and is stronger on drawdown; both multipliers look somewhat less profitable than the first run showed (April, the big month, shrank). The cross test and the plateau check were not repeated on the new data. Three changes are mixed in that comparison: the void on Fyers, the September contracts on Fyers (the loaders prefer Fyers wherever the effective contract has it), and two more trading days (mult 2.0 had five more losing trades on 2026-10-05).
