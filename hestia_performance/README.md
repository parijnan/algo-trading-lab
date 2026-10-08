# Hestia performance tracker

A running record of every live trade of the four engines Hestia hosts (Prometheus on the 2.5 multiplier, Selene, Helios, Typhon), built to inform scaling decisions. One private artifact, refreshed daily by the `hestia-performance-tracker` skill (`.claude/skills/hestia-performance-tracker/SKILL.md`, local, not tracked).

- `pull_snapshot.py` runs on Delos over ssh stdin, read-only: the four closed-trade CSVs, the engines' saved state (open positions) and the last cached one-minute close per contract. Prints one JSON snapshot.
- `build_tracker.py` turns a snapshot into `tracker_data.json` and the page `hestia_performance_tracker.html` (template `tracker_template.html`, data embedded as JSON). Pure functions, tested without Delos.
- `tracker_config.json`: which trades count as live (`first_live_trade_id` per engine), lots per unit, rupees per point per lot, and the artifact URL.
- Tests: `tests/test_hestia_performance_tracker.py`.

**Live starts.** Prometheus from trade #64 (first trade after the 2.5 multiplier went live on 2026-10-05 20:41; the 2.0 trades are left out on purpose). Selene #7 (first real order 2026-10-01), Typhon #3 (2026-10-05), Helios #6 (2026-10-06). Earlier rows of the paper-to-live engines in the Delos CSVs are paper trades: their fills carry order ids starting `PAPER` in the request journal.

**Figures.** P&L is the engines' own recorded value, gross of brokerage and charges. `rs_unit` is rupees for one unit and `rs_actual` is `rs_unit` times the units the trade ran at (all 1 today). Drawdown is peak to trough of the cumulative closed-trade value from a start of zero. The page's units control re-prices the recorded points at 2, 5 or 10 units; it is hypothetical and ignores the extra slippage and margin that size would bring.

**Run by hand.**

    ssh delos-ipv6 'cd ~/scripts/algo-trading-lab && python3 -I -' < hestia_performance/pull_snapshot.py > snapshot.json
    python hestia_performance/build_tracker.py --snapshot snapshot.json --out-dir out
