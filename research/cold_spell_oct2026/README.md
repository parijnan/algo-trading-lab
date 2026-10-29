# October 2026 cold spell: drawdowns, live-versus-backtest gaps and what history says

Written 29 Oct 2026. Covers the week of 26 to 29 Oct 2026, when all four Hestia engines (Prometheus, Selene, Helios, Typhon) whipsawed. Everything here is gross of brokerage and charges unless stated. Scripts in this folder regenerate the P&L, regime and crude figures (`python cold_spells.py`, `regime_er.py`, `crude_range.py`, run from this folder; shared loaders in `common.py`). Supporting data is in `outputs/`.

## 1. Summary

- Live closed trades from 26 Oct to 29 Oct 13:57 IST: 30 trades, 6 wins, −Rs 1,40,134 at actual units (units were 1, 2 and 5 over the period). Prometheus 10 trades 2 wins −49,475; Selene 9/2 −45,827; Helios 6/1 −30,177; Typhon 5/1 −14,655.
- At the same units the backtest took essentially the same trades. At 1 unit each, 26 to 28 Oct: live −Rs 15,459 against backtest −Rs 15,620 (gap +161). The week's loss is the strategies' own losses, not an execution or Supertrend-formula failure.
- The broker trade book agrees with Hestia's ledger to Rs 4 for 26 to 27 Oct (section 5).
- History puts a cold spell like this at roughly three to four weeks from first drawdown to a new equity high, with the cold phase itself typically one to two weeks (section 6). Nothing in the history times the turn.
- Crude is at a 250-session high but its 20-day range is average, not tight (section 8). The "pinned under resistance" idea is only half supported.

## 2. Live versus backtest, sized at exactly 1 unit

Rupees per point per unit: Prometheus 10 (summed over two lots), Selene 1, Helios 20, Typhon 500. Per-trade table: `outputs/live_vs_backtest_1unit.csv` (a roll's two legs are joined into one trade). Full matching output of `hestia_backtest/compare_actual.py` through 28 Oct: `outputs/live_vs_backtest_comparison_to_28oct.csv`.

| Window | Prometheus | Selene | Helios | Typhon | Total |
|---|---|---|---|---|---|
| 26 to 28 Oct, live | −5,250 | −10,000 | −1,569 | +1,360 | −15,459 |
| 26 to 28 Oct, backtest | −4,812 | −9,698 | −1,380 | +270 | −15,620 |
| Difference (live minus backtest) | −437 | −302 | −189 | +1,090 | +161 |

Full live history (first live trade to EOD 28 Oct) at 1 unit: live Rs 14,328 against backtest Rs 17,943, a gap of −Rs 3,615 per unit (Prometheus −1,756, Selene −1,126, Helios −123, Typhon −610). Most of that accrued before 26 Oct. In points through EOD 28 Oct: Prometheus 37/37 trades live 165.3 against backtest 340.9; Selene 34/34 −1,202.4 against −76.0 (average entry 16.3 points worse live); Helios 18/17 −139.2 against −133.0; Typhon 22/22 33.3 against 34.5. Sized at 5 units the full-history gap is about −Rs 18,030 (Prometheus −8,780, Selene −5,630, Typhon −3,000, Helios −620), a figure that is only indicative because live ran at 1, 2 and then 5 units.

The Supertrend formula change (commit 2857686, standard Zerodha/TradingView final-band rule) did not drive the week: formulas disagree on 0.04% to 0.17% of bars per instrument over 2021 to 2026. One recent episode was an exception: on NATGASMINI 24NOV26 the old and new trend states differ on 39 of 606 bars since 14 Oct, all in one episode from 26 Oct 20:45 (see section 4).

## 3. Reconciling live Rs −1,17,909 to the backtest (26 to 28 Oct at 5 units)

The two windows are not the same, which is why the numbers first looked inconsistent.

| Step | Rs |
|---|---|
| Live trades exiting 26 to 28 Oct at actual units | −1,17,909 |
| Friday carry-ins priced at 5 units instead of the 2 they actually ran (Prometheus #93 +7,503, Selene #32 −4,173, Typhon #21 +14,955) | +18,285 |
| Helios #17 and #18: the backtest books them as one 5-unit trade; live had a first leg at 2 units outside the window (+11,005) and the 2-to-5 scaling (+11,327) | +22,332 |
| Equals backtest at 1 unit times 5 (−15,458 × 5) | −77,290 |
| Real 1-unit gap (see section 2) | −810 |
| Backtest as the compare tool prices it (−15,620 × 5) | −78,101 |

Trades that exited on 29 Oct were −Rs 22,225 (Prometheus #101 −9,990, #102 −5,735, Typhon #25 −6,500), which is why the 26 to 29 Oct total is −1,40,134 and the 26 to 28 Oct total −1,17,909. One structural point: the Helios backtest does model its contract roll (`helios_backtest/data_sweep/parity_legs.csv`, trade 1153 has a rollover leg +278 and a second leg −91, against live +275.12 and −86.30), but `parity_trades.csv`, `build_ledger` and `compare_actual` collapse the legs into one trade dated at the final exit. Booking each leg on its own exit date would remove the window mismatch. Not changed.

## 4. Typhon, 26 Oct (the divergence that went in our favour)

On NATGASMINI 24NOV26 the live log used the old-formula band (292.40), flipped bearish at the 19:30 bar, stopped at 20:02, and flipped bullish at the 21:45 bar close of 292.50, entering at 22:00 at 292.60 with a stop at 290.26. Live 15-minute closes at 20:30 and 20:45 were 290.50 and 291.70, against 291.1 and 292.4 in the stored files. The stored files show unusual volume in the last minutes of those bars (20:44 13,145; 20:46 12,842; 20:55 11,456; 20:59 11,322, against about 60 to 500 normally, with adjacent zero-volume placeholder rows), which look like late prints the live bar lacked. The new-formula line at 291.71 would have been cleared only by the stored 292.4; live's 291.70 missed it by 0.01. Net effect: Typhon +Rs 1,070 per unit better live than the backtest in the window (+5,350 at 5 units), by chance. Not yet checked: whether live flagged provisional or incomplete bars at 20:45 and 21:00, and whether the spike minutes are real prints or a Fyers artifact.

## 5. Broker trade book reconciliation (26 to 27 Oct)

Source: the broker's trade history export for 1 Sep to 28 Oct 2026 (not copied here, it carries the client id). Rows tagged "(BF)" have no order or trade id: they re-open each carried position at the previous day's settlement price, so fills cannot be matched fill by fill. Valued as daily mark-to-market per contract (cash of all rows that day plus end-of-day position times the next day's BF settlement price), gross:

| Engine | Hestia ledger | Broker | Difference |
|---|---|---|---|
| Prometheus | −24,860 | −24,860 | 0 |
| Selene | −22,966 | −22,966 | 0 |
| Helios | −24,267 | −24,268 | +1 |
| Typhon | −15,855 | −15,850 | −5 |
| Total | −87,948 | −87,944 | −4 |

Bridge from the trade-level −1,17,909 to −87,948: −2,400 (Prometheus #93's first lot was booked on 22 Oct) gives −1,20,309 lot-level; remove 28 Oct exits (−15,350: Prometheus −11,192, Selene −6,718, Helios +2,560) gives −1,04,959 realised on 26 to 27 Oct; add unrealised at the 27 Oct settlement +30,532 (Prometheus +6,677, Selene +15,618, Helios +8,236); subtract the same positions' value at the 25 Oct settlement, +13,521 (Prometheus +6,580, Selene −525, Helios −234, Typhon +7,700); result −87,948. End-of-day positions in the book match the engines (Selene 5, Helios 100, Typhon 2,500 then 0, Prometheus 0 then −50 bbl). 28 Oct cannot be marked because the file ends before that day's settlement price. Charges (not in any Hestia figure): Rs 3,233 for 26 to 27 Oct, Rs 5,419 for 26 to 28 Oct (Prometheus 1,512, Selene 1,739, Helios 1,583, Typhon 585), Rs 17,927.52 for the whole file.

## 6. Drawdown spells and cold windows, from the four-engine backtest

Source: `hestia_backtest/outputs/integrated_trades.csv` at 1 unit each, Apr 2021 to 28 Oct 2026, 1,453 weekdays, trades dated by exit. Current drawdown −Rs 18,682, open since 16 Oct (9 weekdays); worst ever −Rs 41,954. Spells in `outputs/underwater_spells.csv`, cold windows in `outputs/cold_windows.csv` (`cold_spells.py`).

| Spells | Count | Peak to trough, weekdays (median / 90th pct) | Trough to new high (median / 90th pct) | Total underwater (median / 90th pct / max) |
|---|---|---|---|---|
| All | 162 | 1 / 7 | 2 / 7 | 3 / 14 / 102 |
| Depth Rs 15k or more | 18 | 6 / 14 | 7 / 24 | 16 / 33 / 89 |
| Depth Rs 30k or more | 3 | 10 / 11 | 17 / 21 | 27 / 32 / 33 |

Cold five-session windows (total at or below −Rs 11,700, about the 4th percentile): 20 episodes. The five-session total turned positive after a median of 3 sessions (worst 10). The next 20 sessions were positive in 16 of the 18 episodes that have 20 sessions of data (the 2 negative: −1,289 in Aug 2023, and −12,814 from 30 Oct 2025, the longest at 28 calendar days with 10 sessions to recover); the last two episodes (5 Oct and 27 Oct 2026) do not yet have 20 sessions. The "about 7 more weekdays" read from the median is a subtraction, not a conditional estimate.

Earlier in the same investigation: the 5-session combined window ending 28 Oct was −11,700 at 1 unit (4th percentile; Selene 0.9th and Helios 2.6th percentile on their own). All four engines negative in a window occurred in only 2.7% (5-session) and 1.4% of windows. After past worst-5% clusters the next 10 sessions averaged +17,877 against a normal 11,965 (74% positive) and the next 20 +46,205 against 23,930 (89% positive). Win rate after 1, 2 or 3 losses is about the engine's overall win rate: no loss clustering.

## 7. What can be done about whipsaws: options and what has already been tried

Consulted advisor on 29 Oct; recommendation was to change nothing because of one week. Options discussed: accept it; cut units by hand; volatility or equity-curve sizing; a chop or regime entry filter; flip hysteresis; a portfolio-level breaker; add a range-earning engine (Janus/Oleus plan exists); hedging with options (`plans/mcx-option-hedging.md`, which does not address whipsaw). Prior verdicts to respect: the 1h/15m alignment filter was shelved on 2026-09-04, a binary volatility gate did not help, an ML classifier was advised against. Volatility-based sizing and the portfolio breaker are testable on the existing trade list; not run.

Janus (CRUDEOILM, research only) entry filter "sit out after N failed trades in a session", failure defined as net loss per trade, sit out until the next session, post-hoc on `trades.csv`: baseline 2,427 trades Rs 2,59,087, drawdown −47,965, Calmar per year 1.52. N=1: Rs 93,568. N=2: 1,709 trades Rs 1,87,030, drawdown −47,644, Calmar 1.10 (skipped 718 trades worth Rs 72,057, win rate 41.6% against 42.6%). N=3: Rs 2,39,329, Calmar 1.37. The filter does not help: a losing streak does not predict the next trade. Only these three settings were tested.

## 8. Price view: is the market ranging, and for how long?

Daily-close efficiency ratio (net move over the sum of absolute moves across 5 sessions; near 0 means going nowhere), per instrument, roll-free (`regime_er.py`, `outputs/efficiency_ratio_5d.csv`), 21 Mar 2023 to 28 Oct 2026, 927 sessions; "ranging" means at or below that instrument's own 30th percentile.

- Three or more of the four ranging together: 11.1% of sessions, 74 runs, median run 1 session, longest 4. All four together: 1.1% of sessions, 10 runs, each one session.
- No persistence: given three or more ranging today, the chance of the same 3, 5 and 10 sessions later is 7%, 11% and 8%, against an 11% base rate.
- Flagged sessions earned less: Rs 665 a day (three or more) or −Rs 1,666 (all four) against about Rs 1,700 otherwise; the following five sessions averaged Rs 6,857 and Rs 7,832 against Rs 8,032.
- On 28 Oct the 5-session ratios were Prometheus 0.52, Selene 0.89, Helios 0.44, Typhon 0.46. Only Prometheus had been low (0.25 to 0.29 on 26 to 27 Oct). So the other three are not ranging on daily closes; their losses come from 15-minute Supertrend flips inside daily moves, which this measure cannot see.

## 9. Crude near its highs in a tight range?

Idea from the strategy owner: crude is high with little head room, not cooling because of the geopolitical backdrop, so it chops. Test (`crude_range.py`, `outputs/crude_range_features.csv`, `outputs/prometheus_by_crude_setup.csv`): level is the roll-free cumulated daily move anchored to the last real close (9,791 on 28 Oct); "setup" is a 20-day high-low range at or below its 25th percentile (10.4% of price) while within 5% of the 120-day high.

- Crude on 28 Oct: at its 120-day high (0.00% below) and the 100th percentile of the last 250 sessions, so "on the higher side" is true.
- Its 20-day range is 14.6% of price, the 53rd percentile (median 13.7%): not tight. The market is moving a lot inside the range, not sitting still. Today is not a "setup" day.
- Setup days are rare: 13 of 821 sessions in 4 episodes (median 2.5 sessions, longest 7). The next 10 sessions after those days rose in 12 of 13 cases (mean +1,354 against +38 otherwise). That is a small sample and says nothing reliable.
- Prometheus entries made on setup days: 24 trades, mean Rs 139.5 per trade at 1 unit, win rate 54.2%, against Rs 91.4 and 39.5% for the other 1,677 trades. With 24 trades this is not distinguishable from noise; it gives no sign that the setup hurts the engine.

Reading: crude at a multi-month high with a wide, choppy range fits a market that overshoots both ways. The data neither confirms nor excludes the geopolitical cause, and it covers one of the four engines. Gold and silver could share the safe-haven driver; natural gas has its own.

## 10. Open items

- Four options-hedging plan questions are unanswered (scope, adoption thresholds, a read-only quote recorder, hedge-aware engine variants).
- Not run: intraday whipsaw measure from trade lists (losing trades closed within a few hours, per engine and week, and whether bad weeks cluster); volatility sizing and portfolio-breaker tests; booking roll legs on their own exit dates in `build_ledger`/`compare_actual`; a Delos log check of provisional bars at Typhon 20:45/21:00 on 26 Oct; counting spike-minute occurrences in the data files.
- Uncommitted at the time of writing: `hestia_backtest/outputs/*` (refreshed by the compare run), `plans/mcx-option-hedging.md`, and this folder.
