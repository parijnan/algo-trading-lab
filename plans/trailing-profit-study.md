# Trailing-profit overlay study (Selene, Helios, Typhon) — 2026-10-08

Question: these engines hold until the stop, the Supertrend flip (or Typhon's target), so does a trailing-profit overlay add anything? The Supertrend flip is itself a trailing stop, so the overlay only helps if it locks in profit sooner than the flip does.

Code: `research/trailing_profit/` (`trail_configs.py`, `trailing_overlay.py`, `outputs/trailing_results.csv`), tests `tests/test_trailing_overlay.py`. The overlay is applied to each engine's decided trades on the decided exits; rules are trail from entry, activate-then-trail, and breakeven-after-gain. Intrabar order is pessimistic (the peak is updated with the bar's high before the low is tested), and a bar that moves the stop does not fill at its own open. Baseline reproduction is asserted per trade. The sample splits at 2026-01-01. Results are in percent of entry price per lot, price-only, no slippage.

## Results

| Engine | Baseline total / Calmar | Best candidate | Verdict |
|---|---|---|---|
| Selene | 322.8% / 13.31 | none: 0 of 38 rules beat baseline on Calmar in both halves | do nothing |
| Helios | 184.2% / 17.97 | trail 0.5% or 1.5% after +5%: 193% / 18.8 | negligible, only ~21 trades (2%) reach +5% |
| Typhon | 138.5% / 6.72 | breakeven after +1.0%: 151.4% / 8.34 | only modest candidate |

Walk-forward picks (rule chosen on the pre-2026 half) were worse than baseline out of sample for Selene and Helios.

Typhon detail (per-year, then total after an extra cost per stop exit; the baseline pays the same cost on its own 565 stop exits):

| | 2023 | 2024 | 2025 | 2026 | 0.03% | 0.05% | 0.10% |
|---|---|---|---|---|---|---|---|
| baseline | 22.8 | 86.5 | 29.7 | -0.4 | 121.6 | 110.3 | 82.0 |
| breakeven after +0.5% | 26.1 | 75.7 | 39.3 | 13.8 | 127.7 | 109.6 | 64.3 |
| breakeven after +1.0% | 23.1 | 82.8 | 40.4 | 5.1 | 130.4 | 116.3 | 81.2 |
| trail 1.0% from entry | 20.1 | 76.3 | 18.1 | 12.0 | 94.4 | 73.1 | 19.8 |

Breakeven after +0.5% and the plain trail turn many winners into scratch stops (stop exits 565 to 906 and 1066) and lose their edge at realistic exit slippage. Breakeven after +1.0% holds up better (stop exits 702; still ahead at 0.05%, equal to baseline at 0.10%) but loses in 2024 and gains mostly in 2025 and 2026.

## Conclusion

Selene and Helios: no change. Typhon: breakeven after +1.0% is the only candidate with some robustness, but the gain (~13 points over 3 years, ~+1.6 Calmar) disappears at 0.10% extra cost per stop exit. Adopting it would need a roll-aware parity backtest with real prices and slippage plus a small engine change (move `sl_price` to entry once price gains 1%). Not adopted.

Prior notes: Prometheus lot-2 trail after T1 (README 2026-09-09) looked good on CRUDEOILM and failed on CRUDEOIL; Iris trailing stops (15-30%) were worse; Janus has trailing as open item 8.
