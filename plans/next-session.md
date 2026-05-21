# Next Session Plan

## 1. Quick wins — consistency and polish (~30 min)

**A. Remaining hardcoded Slack channel strings**
- `artemis_production/credit_spread.py` — ~20 hardcoded `"#trade-alerts"`, `"#error-alerts"` strings
- `artemis_production/iron_condor.py` — remaining `"#trade-updates"`, `"#trade-alerts"` strings
- Constants already imported in both files — mechanical substitution only

**B. `slack_listener.py` channel constant**
- ~10 `say(channel="#tradebot-updates", ...)` calls
- Add `_CH = "#tradebot-updates"` at module top, replace all call sites

**C. Stale docstrings**
- `iron_condor.py` opening block: "chdir removed — Leto sets cwd" etc. — predates several rounds of changes
- `credit_spread.py` same
- `apollo_production/functions.py` docstring says "Mirrors the structure of Artemis functions.py" — backwards now

**D. Minor code fixes**
- `artemis_production/functions.py` `telegram_bot_sendtext`: URL built with string concatenation — convert to f-string (matches Apollo/Athena)

---

## 2. Observability — small but meaningful (~15 min)

**A. Tighten `OrderFillWatcher` heartbeat: 900s → 300s**
- All three `functions.py` files
- Current 15-min window means a dropped WS order-fill socket goes undetected for up to 15 min mid-session

**B. SlackWorker queue depth logging**
- Add `logger.debug(f"SlackWorker queue depth: {_slack_queue.qsize()}")` inside `_slack_worker` after each `task_done()`
- Silently surfaces chronic Slack latency without adding noise at INFO level

---

## 3. Tests — Artemis and Athena coverage (~1–2 hours)

This is the highest-value gap. Apollo has 20 tests; Athena and Artemis have zero.

**A. `tests/test_artemis_strike_math.py`**
- SL multiplier ladder: given a spread with N DTE, assert the correct `sl_N_dte` multiplier is selected
- Premium-based strike scan: given a synthetic option chain, assert `initialize_spread()` selects the correct buy/sell strikes
- Index SL offset selection: `INDEX_SL_OFFSETS` band lookup

**B. `tests/test_athena_strike_math.py`**
- Delta-based strike selection: given a synthetic mibian output, assert CE and PE sell strikes land at `TARGET_DELTA_SOLD`
- Wing strike selection: assert safety wing lands at `SAFETY_WING_DELTA`
- Strike rounding to nearest `STRIKE_STEP`

**C. `tests/test_state_persistence.py`**
- `ApolloState` round-trip: populate all fields, `save_state()`, `load_state()`, assert equality
- `AthenaState` same
- Missing file → fresh state returned (no crash)

---

## 4. Backtest pyflakes cleanup (~20 min)

Pre-existing warnings that add noise to every `pyflakes` run:
- Unused local variables (`target`, `lose`, `add_lots`, `holidays`, `e`) across multiple athena/artemis backtest files
- f-strings without placeholders in backtest logging lines
- Suppress or fix — whichever is correct per case

---

## Order of execution

1. Quick wins (1A → 1B → 1C → 1D) — commit together
2. Observability (2A + 2B) — commit together
3. Tests (3A → 3B → 3C) — commit per file
4. Backtest cleanup — commit last (lowest risk, lowest value)

Update README for anything that changes documented behaviour (heartbeat interval, test coverage section).
