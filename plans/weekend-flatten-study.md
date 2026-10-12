# Weekend-flatten study (all four engines) — 2026-10-12

**Status: first look done, result negative for all four engines. Research only: nothing in production changed, nothing committed.** Question (user, 2026-10-12): how would the engines fare if every open position were flattened before the weekend, at 23:15 (23:30 close) or 23:45 (23:55 close) on the week's last session (Thursday if Friday is a holiday), with new positions the following week taken as per the signal?

Code: `research/weekend_flatten/` (`flatten_configs.py`, `weekend_flatten.py`, `outputs/` with `weekend_flatten_results.csv` and the per-trade CSVs), tests `tests/test_weekend_flatten.py` (15, each behaviour broken on purpose and caught). Run: `python research/weekend_flatten/weekend_flatten.py` (about 4 minutes).

## Method

- **Engines at their live / decided configs through their own parity simulators** (the same ones as the trailing-profit and early-MFE studies): Selene 2.5 / SL 3.0% (2,487 trades), Helios 3.5 / 1.6% (1,174), Typhon 3.0 / 0.8% / 15% (1,403), Prometheus ST 2.5, SL 1.0 / T1 1.25 / T2 4.0 on the Fyers-history track (1,871, the two lots averaged). The baseline reproduces each module simulator on every trade (asserted). Results are percent of entry price per lot, price-only, summed in entry order.
- **Calendar from the data, not a DST rule:** each calendar week's last session present in the data; its close is the last 1-minute bar plus one minute; the flatten minute is 15 minutes before a 23:30 close and 10 before a 23:55 close (the user's 23:15 / 23:45), with the engine's own roll times (23:15 / 23:40) as a variant. Results are almost identical for the two variants.
- **Rule:** an open position is closed at the OPEN of the flatten minute (plus an optional extra cost on that fill: 0, 0.02, 0.05%); no entry at or after the flatten minute on that session; re-entry only on a fresh Supertrend flip, so every later trade is unchanged. Prometheus flattens only the lots still open. A stop or target that comes before the flatten minute is untouched.
- **Weekend gap split:** for each flattened trade, the gap is the direction-adjusted move from the flatten fill to the next session's first open; the rest of the lost result is what the trade made after that.
- **Second arm (Selene, Helios, Typhon only):** re-enter in the same direction at the open 15 minutes into the next session if the trade would still be open, with a fresh stop and target from that price, exit at the original flip. Not run for Prometheus.

## Results (flatten at the open of the flatten minute, no extra cost)

| Engine | Trades flattened / dropped | Baseline total / DD / Calmar | Flatten total / DD / Calmar | Flatten at 0.05% extra |
|---|---|---|---|---|
| Selene | 283 (11%) / 3 | 322.8 / -24.2 / 13.31 | 252.2 / -21.2 / 11.90 | 238.1 |
| Helios | 251 (21%) / 4 | 184.2 / -10.3 / 17.97 | 121.0 / -8.1 / 15.00 | 108.5 |
| Typhon | 140 (10%) / 1 | 138.5 / -20.6 / 6.72 | 128.1 / -23.1 / 5.54 | 121.1 |
| Prometheus | 158 (8%) / 3 | 108.0 / -25.0 / 4.31 | 97.3 / -25.9 / 3.75 | 90.7 |

- **Total and Calmar fall on every engine.** Drawdown improves only for Selene and Helios; it gets slightly worse for Typhon and Prometheus.
- **The weekend gap is not a cost the engines pay on average.** Mean gap in the trade's direction: Selene +0.079%, Helios +0.038%, Typhon -0.014%, Prometheus +0.015%, with about half of the gaps positive. Flattening gives up the trend continuation across the weekend and after Monday's open, which is where the winners earn: flattening is better on 54 to 57% of the flattened trades but loses overall because the runners are cut.
- **Tail does improve for two engines:** worst single trade Typhon -4.92 to -2.79, Prometheus -5.64 to -2.42, Selene -7.27 to -6.26, Helios -1.95 unchanged. Worst weekend gap against the position: Selene -2.11%, Helios -0.88%, Typhon -3.64%, Prometheus -2.16% (13 to 38 trades per engine had a gap worse than -0.5%).
- **By period:** Selene and Helios lose almost every year. Typhon: 2023 -15.9, 2024 -4.3, 2025 -2.4, 2026 +12.1. Prometheus: 2023 -6.4, 2024 +3.5, 2025 +7.1, 2026 -14.7. Before / after 2026-01-01 totals: Selene 190.4 to 156.6 and 132.4 to 95.7; Helios 112.3 to 71.5 and 72.0 to 49.6; Typhon 139.0 to 116.4 and -0.4 to +11.7; Prometheus 25.5 to 29.6 and 82.5 to 67.8.
- **Re-entry arm (Selene / Helios / Typhon):** totals 255.0 / 149.8 / 132.7 against 252.2 / 121.0 / 128.1 flattened and 322.8 / 184.2 / 138.5 baseline. It recovers little, and at 0.05% extra it is no better than the plain flatten for Selene (229.7 vs 238.1) and Typhon (120.8 vs 121.1). Caveat: the re-entered lot gets a fresh stop from Monday's price, so the original trade's cushion (the 3% Selene stop is mostly irrelevant once a trade is well in profit) is gone, and it enters at the volatile open. One hand-checked case: the 2025-12-29 silver spike, re-entered at 253,100 and stopped at -3.0% while the original trade gained 4.1%.

## Caveats and checks not done

- **Rolls:** Selene and Helios series are NOT back-adjusted (Typhon is). A weekend that spans a roll shows the contract basis as a gap in those two, which affects both the baseline and the gap figures; weeks containing a roll have not been separated.
- **Gap fills** follow the repo convention (a stop that gaps fills at the open, the first minute of a session is exempt), the same as the baseline. This morning's trade 71 (Prometheus, 8824 long, filled 8658 on the Monday open against a 1.0% stop) is outside the data range and was not replayed.
- Prometheus: one week in the data ends Monday to Wednesday (a possible data hole, not removed); only the Fyers-history track was run (not the Angel One 2026 track or CRUDEOIL); the second arm was not run.
- Exit slippage on the baseline's own exits is not modelled for any arm; late Friday liquidity on the flatten fill is covered by the 0.02 / 0.05% grid only.
- Single flatten rule only: no partial flatten, no flatten only when the position is in profit, no conditional rules (for example flatten only below some unrealised level).

## Possible next steps (not decided)

1. Exclude weeks that contain a roll for Selene / Helios and redo the gap split.
2. A narrower rule aimed at the tail (flatten only when the position is underwater, or only the largest weekend-gap risk), since the full flatten costs the trend winners.
3. Prometheus second arm and the other two Prometheus tracks.
