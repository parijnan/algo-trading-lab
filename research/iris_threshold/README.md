# Iris VIX-Activation Threshold Sweep

**Poseidon plan §8 fallback test.** The MTM equity diagnostic
(`research/mtm_equity/`) found the full-sample "hidden risk" case for
Poseidon was weak (1.3× gap, below the 1.5–2× gate) and the 2020 COVID
proactive-window dip was real but modest (₹2,031, recovered in 2 days). Per
the plan's own fallback rule, before building a new trend-following engine,
check whether the same benefit is available more cheaply by lowering
`ROUTING_VIX_HIGH` (Iris's activation floor) in `leto_backtest/configs.py`.

**Status: COMPLETE. Rejected — do not lower the threshold.**

---

## Method

Reused `leto_backtest`'s own `loader.py` / `router.py` / `simulator.py`
unmodified. Monkeypatched only `router.ROUTING_VIX_HIGH` per sweep value
(25.0 baseline, then 22.0 / 20.0 / 18.0) and re-ran the full 2020–2026
simulation for each. One variable changed per run, per repo convention.
`python research/iris_threshold/run_sweep.py` reproduces.

## Result

| `ROUTING_VIX_HIGH` | Trades | Total P&L | Athena P&L | Iris P&L | Max DD | Calmar | COVID window P&L (Feb 24–Mar 12 2020) |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 25.0 (baseline) | 347 | ₹3,22,733 | ₹1,52,155 | ₹25,589 | ₹14,537 | 22.20 | **+₹6,877** (2 trades) |
| 22.0 | 377 | ₹3,05,762 | ₹1,19,529 | ₹41,245 | ₹14,537 | 21.03 | +₹708 (3 trades) |
| 20.0 | 436 | ₹2,89,249 | ₹69,518 | ₹74,743 | ₹14,537 | 19.90 | +₹3,307 (5 trades) |
| 18.0 | 505 | ₹2,76,951 | ₹41,561 | ₹92,522 | ₹11,115 | 24.92 | +₹3,307 (5 trades) |

Full data: `data/threshold_sweep_summary.csv`.

## Why it fails

`ROUTING_VIX_HIGH` is a single shared boundary — Athena's ceiling and Iris's
floor at once. There's no way to give Iris an earlier activation without
shrinking Athena's 16–25 band, and 20–25 is where Athena earns its fattest
premiums (₹1,52,155 of the book's ₹1,49,036 Athena total sits in that
sub-band alone, per the 25→18 P&L collapse above). Every ₹1 Iris gains from
extra activation days is bought with more than ₹1 of foregone Athena P&L —
book total P&L falls monotonically as the threshold drops.

The specific window this was meant to fix gets **worse, not better**: at
22.0, Athena's winning Mar 4 entry (+₹4,992) is replaced by an Iris entry
that has no signal that week except a Mar 3 loss (-₹2,379), collapsing the
window's realized P&L from +₹6,877 to +₹708. At 20.0/18.0 it partially
recovers to +₹3,307 but still well below baseline.

Max drawdown is unchanged at ₹14,537 down to threshold 20 (the 2026 Athena
losing streak that drives it sits inside a VIX band none of these thresholds
touch) and only shifts at 18.0 — alongside a ₹46,000 total P&L give-up, not
a trade worth making.

## The real proactive-window mechanism (found, not fixed)

Tracing the baseline COVID window explains *why* Iris didn't fire even
though VIX crossed 25 on Mar 6 (26.44) and Mar 9 (28.41): Athena's Mar 4
trade was still an open active position (exits Mar 11 10:25, `pre_expiry`)
and the simulator's no-concurrent-trade constraint blocks all routing
checks — including Iris's — until that trade's slot frees up. Iris cannot
preempt an open Athena/Artemis position on a VIX spike; it can only take
over once that position exits on its own terms.

That is the actual proactive-window gap — not a misrouted VIX threshold.
Closing it for real would mean adding mid-trade VIX-escalation preemption
logic to Athena/Artemis (exit early if VIX spikes past some level while a
trade is open), which is a change to production strategy logic, not a
router config value. Given the diagnostic already sized this gap at ₹2,031
on ~₹12K equity, recovered within 2 days, that engineering cost isn't
justified by the payoff.

## Conclusion

No cheap fix exists here. Combined with `research/mtm_equity/`'s Step 0
result, **Poseidon is shelved** with no substitute action taken — the book's
realized-P&L Calmar (22.2) stands as reported, and the 2020 proactive-window
gap is accepted as a known, modest, unaddressed edge case rather than
engineered around.

## Files

| File | Purpose |
|---|---|
| `sweep_configs.py` | Threshold list, paths, COVID window dates |
| `run_sweep.py` | Entry point — monkeypatches `router.ROUTING_VIX_HIGH`, reruns simulation per value |
| `data/threshold_sweep_summary.csv` | Sweep results |
