# Plan: morning versus evening, how the engines' trades fare by time of day (user, 2026-10-13)

**Status: first pass run 2026-10-13 (market structure and trade analysis). Insight only: no strategy change is proposed or tested.** Research only: nothing here touches Hestia, an engine or Delos. Code in `research/session_analysis/` (`session_configs.py`, `session_lib.py`, `session_market.py`, `session_trades.py`, `outputs/`), tests in `tests/test_session_analysis.py`.

The question: much of the real price action looks to happen in the evening and the morning and afternoon look quiet; how do entries, exits, profitability, volatility and range differ by time of day, across the four engines and their instruments? MCX trades continuously 09:00 to 23:30 (23:55 when US daylight saving is off), so the split is a clock one: Morning 09:00 to 13:00, Afternoon 13:00 to 17:00, Evening 17:00 to the close (and Day = Morning + Afternoon). Overnight is the move between one day's last bar and the next day's first bar.

## 1. Method

- **Market structure** (`session_market.py`): 15-minute bars (the engines' timeframe) built per calendar day from the same price series the backtests use (Fyers first, Angel One where Fyers has no file), bars with no traded volume dropped. Per hour and per bucket: bar range (high minus low, percent of the bar's open), absolute close-to-close move, volume, and each bucket's share of the day's movement. Also where each day's high and low is set.
- **Trades** (`session_trades.py`): the closed-trade ledger of the Hestia backtest (`hestia_backtest/outputs/integrated_trades.csv`: Selene, Helios, Typhon production-parity at the live config, Prometheus Fyers track at the live 2.5 config), 6,935 trades. Results are percent of entry price per lot so engines compare on one footing. Entries and exits are bucketed by the clock hour of the trade's entry and exit time.
- **Where the profit is earned**: each trade's result is split over the minutes it was held and booked to the bucket of the bar it was earned in (close-to-close per minute bar, the entry leg at the entry price, the exit leg at the exit price), so an evening entry closed next morning shows what each part contributed. The parts sum to the recorded result for every trade (checked: engine totals reconcile to the recorded percent). A trade closed on the first bar after a gap books the open's jump to the overnight gap and only the rest to the bar (a case fixed 2026-10-13 on the first review of the numbers).

## 2. Findings

**Volatility and range follow the idea for crude oil and natural gas, much less for silver and gold.**

| | CRUDEOILM | NATGASMINI | SILVERMIC | GOLDPETAL |
|---|---|---|---|---|
| Mean 15-minute range %, Morning / Afternoon / Evening | 0.24 / 0.34 / 0.42 | 0.35 / 0.37 / 0.70 | 0.23 / 0.19 / 0.28 | 0.13 / 0.10 / 0.13 |
| Evening share of the day's absolute movement (46% of the bars) | 57% | 65% | 56% | 52% |
| Evening range over day-session range (mean) | 1.22 | 1.65 | 1.17 | 0.88 |
| Day's high or low set in the evening (either / both extremes) | 93% | 97% | 88% | 76% |

The busiest hours: crude 19:00 to 21:00, gas 17:00 to 20:00 (a range about twice its daytime level), silver 18:00 to 20:00. **The 09:00 hour is a second peak in all four**: its mean range is about twice the next hour's (gold 0.22 against 0.10, silver 0.31 against 0.16), with volume to match. 10:00 to 12:00 is the quietest stretch everywhere; for gold the whole day apart from 09:00 and 18:00 to 20:00 sits near 0.10 to 0.15.

**Entries.** Evening entries are 52 to 64% of trades (the evening is 46% of the bars). Per trade the picture is mixed: evening entries have the higher total for Selene (228 against 119 percent summed), Typhon (188 against 100) and Helios (123 against 99), with similar profit factors; for Prometheus Day entries are slightly better (profit factor 1.21 against 1.08). Splitting the day in three, **afternoon entries (13:00 to 17:00) are the weakest bucket for three engines**: Selene breakeven (profit factor 0.98, 530 trades), Typhon 1.14, Prometheus 1.15, against morning-entry profit factors of 1.69, 1.71 and 1.33; Helios is the exception (afternoon 1.83, similar to the others). Morning entries are the best per trade for all four but are fewer. By hour, entries at 13:00 to 14:00 are the weakest (mean negative or near zero for Selene, Prometheus, Typhon and Helios at 13:00), entries at 09:00 to 10:00 (the post-open flips) are consistently positive, and Helios's 22:00 hour (mean 0.97%) is only 37 trades. Hourly buckets hold 20 to 340 trades, so read hours as hints, not findings. The stop-out rate is a property of the engine (Typhon about 50 to 65%, Prometheus 15 to 27%, Selene and Helios under 2%) and is higher for evening entries in Prometheus (27% against 16%).

**Exits.** Where a trade ends differs from where it started: Typhon's day exits average +0.48% against +0.08% for evening exits (win rate 36.5% against 23.1%; evening exits are mostly stops, 67%); Helios's evening exits are better (+0.28% against +0.10%); Prometheus's evening exits are net negative (profit factor 0.96, stop-out 26%) while its day exits are positive (1.36). Selene is balanced.

**Where the profit is earned** (percent of entry, summed over all trades; the parts reconcile to the engine totals):

| | Morning (27% of bars) | Afternoon (27%) | Evening (46%) | Overnight gap |
|---|---|---|---|---|
| Selene (total 346.6) | 146.5 | 54.0 | 174.9 | -28.8 |
| Helios (221.8) | 133.2 | 16.8 | 77.1 | -5.2 |
| Typhon (288.5) | 112.5 | 28.1 | 167.8 | -19.9 |
| Prometheus (104.8) | 53.8 | 1.6 | 71.0 | -21.6 |

Per bar of time, **the morning earns the most**, the evening comes second and **the afternoon earns almost nothing**: afternoon is 27% of the bars but 2 to 16% of the net profit. The overnight gap costs every engine (-2 to -21% of its net; the engines are not hedged against the open). Inside the morning, **the 09:00 hour carries a large part of it**: Helios books 105 of its 222 percent in that one hour, Selene 69 of 347; Prometheus and Typhon do not show the same concentration. The evening profit is spread over the hours, with no single stand-out hour.

**Before and after 2026.** The split by entry bucket is stable: in both periods Day and Evening entries have similar profit factors for Helios, Typhon and Prometheus; Selene's evening entries pull ahead in 2026 (profit factor 1.90 against 1.45). 2026 is better than earlier years for Selene, Helios and Prometheus overall, which is the same regime effect seen in every other study.

## 3. Caveats

- Backtest trades, gross of costs and brokerage; Prometheus on the Fyers track (2023 on), Typhon from 2023, Selene from 2021, Helios from late 2021; windows differ.
- The 09:00 profit concentration may be partly a data effect: opening-bar prices differ between the Fyers and Angel One feeds (the crude loader already carries a known opening-bar correction table for Angel One), and a position held overnight books the open's reaction there. It is worth checking the first-bar behaviour by source before drawing anything from it.
- Bucket tables flag fewer than 30 trades as thin; hourly entry means rest on 20 to 340 trades.
- Evening length varies (23:30 or 23:55 close), which changes the evening's share slightly; per-bar and per-hour measures are unaffected.
- This is descriptive: busier does not mean better to trade (a busier hour has bigger stops hit as well as bigger moves).

## 4. Open leads (none started; nothing is being changed)

- Is the 09:00 hour's profit real or an opening-bar data artefact (source split, first-bar versus rest)?
- Why are afternoon entries the weakest? Does the 13:00 to 14:00 weakness hold when the entry is a flip that follows a quiet morning, and does it survive an out-of-sample split?
- Evening stops: Prometheus and Typhon are stopped far more often when they enter in the evening; is that the wider evening range against a fixed stop percent?
- The overnight gap cost by engine, compared with how much of the next morning's open it gives back.
- Per-hour profile of the first minutes after an entry (adverse excursion by entry bucket).
