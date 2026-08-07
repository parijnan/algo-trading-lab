# AGENTS.md

Personal algorithmic trading laboratory for backtesting and automating option
strategies on Indian indices (Nifty/Sensex). Executes live via Angel One's
SmartConnect API from a Linode VPS (Ubuntu 24.04). Backtesting runs on a local
Garuda Linux machine.

This file orients any agent (Claude, Gemini, or other) to the repository. For
the exhaustive architectural reference, see `CLAUDE.md`. For the user-facing
overview, see `README.md`.

---

## 1. Purpose

Automate and continuously research a small portfolio of option strategies,
one active at a time, gated by India VIX regime. Live capital is deployed.
Every code change touches money — treat production paths accordingly.

---

## 2. Architecture

```
                 cron 09:15 (Mon–Fri)
                        │
                     leto.py            ← single entry point
                        │
        ┌───────────────┼───────────────┐
        │ login, market-hours/holiday   │
        │ scrip master download         │
        │ VIX read → route              │
        │ re-route loop on handoff      │
        │ terminateSession + EOD report │
        └───────────────┬───────────────┘
                        │
   ┌──────────┬─────────┴──────────┬──────────┐
   ▼          ▼                    ▼          ▼
Artemis    Athena                Iris      Apollo (open positions only)
VIX ≤ 16   16 < VIX ≤ 25         VIX > 25   retired from routing
```

**Entry point — `leto.py`:** Owns the single broker session, market-hours and
holiday gating, scrip master download, VIX-based routing, the re-route loop
(strategies may hand back control if VIX breaches at entry), session teardown,
and the end-of-day Slack report. Strategies never manage the broker session.

**Routing — `leto_config.py`:** Static constants (market hours, VIX thresholds,
index tokens, Slack channels) plus `ROUTING_MODE` / `MANUAL_STRATEGY` loaded
fresh from `data/routing_state.json` on every `importlib.reload()`. Runtime
overrides are persisted to the JSON file by `slack_listener.py` —
`leto_config.py` itself is never modified at runtime.

**Routing priority:**
1. Open position detected (any strategy) → resume unconditionally.
2. Friday, no open position → stand down unless VIX > 25 (Iris) or Athena
   `FORCE_ENTRY` is set.
3. Manual override (Mon–Thu) → force the selected strategy, VIX ≤ 25 guard
   not enforced; override bypasses VIX unconditionally.
4. Auto VIX routing (Mon–Thu): VIX ≤ 16 → Artemis, 16 < VIX ≤ 25 → Athena,
   VIX > 25 → Iris.

**Slack control — `slack_listener.py`:** Socket Mode daemon
(`slack_listener.service`). Posts a Control Panel to `#actions` on start.
Handles: circuit breakers (Exit / Kill / Disable / Clear Flag), manual
mid-session adjustments (Artemis roll, Athena parachute/wing enter/exit),
routing overrides, sizing overrides, manual Leto start, git pull, state reset.

**Real-time feed — `websocket_feed.py`:** `SharedFeed` wraps
`SmartWebSocketV2`. Single daemon thread, `threading.Lock`-protected state.
LTP + per-token OHLC aggregation that resets on read. Own subscription
registry (works around the SDK `RESUBSCRIBE_FLAG` bug). Exponential-backoff
reconnect (5→60s, 5 attempts) with REST fallback. Stale-tick watchdog (30s
check, 2min threshold, 5min per-token alert cooldown). Used by Artemis,
Athena, and Iris.

**Strategy interface:** Each strategy returns `(handoff: bool, summary: dict)`.
`handoff=True` returns control to Leto for re-routing.

---

## 3. Strategies

| Strategy | Instrument | Structure | VIX band | Status |
|---|---|---|---|---|
| **Artemis** | Sensex weekly | Iron condor → reinforced directional spread | ≤ 16 | **Disabled since 2026-08-05** (see §5) |
| **Athena** | Nifty weekly | Double calendar condor + safety wings | 16–25 | Live |
| **Iris** | Nifty weekly | Directional scalping, ITM-150 long call/put, ST_FAST (5m+15m) | > 25 | Live (`DRY_RUN=False`, 40 lots static) |
| **Apollo** | Nifty weekly | ITM debit spread, dual supertrend | (was > 25) | Retired from routing — manages open positions only |
| Aphrodite | Nifty weekly | Intraday iron condor | (VIX < 11) | Shelved — premise removed by Apollo retirement |

Each strategy has a `*_production/` (live) and `*_backtest/` directory. The
production directory always contains: entry point, `<strategy>_configs.py`
(single source of truth for parameters), `<strategy>_functions.py` (Slack,
order helpers, rate limiting), `<strategy>_state.py` or CSV state files in
`data/`, and `<strategy>_logger_setup.py`. Strategy-prefixed since 2026-08-07
— these were previously bare `configs.py`/`configs_live.py`/`functions.py`/
`state.py`/`logger_setup.py`, identically named across all four directories,
which collided via Python's `sys.modules` caching whenever `leto.py`'s
re-routing loop imported more than one strategy's modules in the same
process. See `plans/strategy-module-naming-collision-fix.md`.

---

## 4. What's Been Accomplished

**Production stack:**
- Three live strategies (Artemis, Athena, Iris) wired through Leto with
  VIX-gated routing, handoff/re-route loop, and EOD Slack reports.
- Resilient order execution: ID-exclusion ghost recovery, 10-ord/s SEBI rate
  limit, sub-second fill verification, orphan-fill cleanup, session kill
  switch, position reconciliation on restart.
- Real-time WebSocket LTP + order-fill feeds with auto-reconnect and REST
  fallback across all strategies. 500ms monitoring loops.
- Slack interactive control: circuit breakers, routing/sizing overrides,
  manual adjustments, git pull, state reset — no SSH needed during market hours.
- ELM and calendar-spread margin compliance (SEBI circular 2024/132).
  Retail algo compliance (SEBI circular 2025/13): 10 ord/s, static IP VPS.

**Backtesting:**
- Per-strategy backtests for all four strategies.
- `leto_backtest/` — integrated routed-portfolio simulation across the full
  2020–2026 data range with era-split handling.
- Consolidated routed result: 339 trades, ₹2,96,171 P&L, 63.7% win rate,
  max drawdown ₹13,838, Calmar 21.4.

**Research (complete):**
- **VIX router** (`research/vix_router/`) — VRP validated on full 2019–2026
  VIX history. Verdict: symmetric VIX-direction router not supported;
  containment is the dominant Artemis P&L driver (ρ=0.32). Hard VIX gate
  unchanged.
- **OI analysis** (`research/oi_analysis/`) — Vectorised engine across 371
  Nifty expiries. PCR near/broad and `wall_oi_ratio` show consistent IC.
  CE parachute validated against 21 Athena events (80% accuracy). PCR entry
  filter at WEAK-PASS — deployment deferred.
- **Greek analysis** (`research/greek_analysis/`) — All 7 branches complete
  for both Athena and Artemis: P&L attribution, Greek profile, IV term
  structure, realized vs implied vol, IV skew (closed — IC=+0.022,
  sign-unstable), Greek exit triggers (closed — no edge), exit timing
  (closed — no gamma/theta or vega/theta crossover at any DTE). Post-branch
  SL calibration assessment closed: `index_sl` and `option_sl` protect
  against different failure modes and are both retained.
- **Range detection** (`research/range_detection/`) — §7 validation gate
  passed. PA method superior to ADX. Key finding: range direction is
  orthogonal to vega — down-biased ranges earn 2.5× Artemis P&L via
  spot-containment, not vega. Lot sizing and strike placement both found
  to be non-levers (~₹4k over 7 years). SL aftermath investigated and parked.
- **Phase 3** (ML regime adaptation with LightGBM/HMM) — researched and
  shelved; overfits and underperforms the rule-based VIX gate.
- **Phase 4** (unified Nifty portfolio with dynamic handoffs) — researched
  and shelved; complexity hurts risk-adjusted returns vs Phase 2 baseline.

---

## 5. What's Ongoing

- **CAS (Closing Auction Session) adaptation — Artemis disabled 2026-08-05.**
  NSE/BSE's CAS went live 2026-08-03: continuous trading in the underlying
  stops at 15:15, ~15min call auction with no ticks, terminal print lands
  ~15:16-15:35, derivatives trade on to 15:40. System-side adaptation shipped
  2026-08-04 (`data_downloader_angelone.py` gap-fill/day-end-extend, Artemis/
  Athena monitoring windows extended to 15:40, Artemis force-closes any
  net-open spread at 15:15 on expiry day, `websocket_feed.py` stale-tick
  watchdog suppressed for Nifty/Sensex spot tokens during the auction). Then
  2026-08-05: Nifty's auction-window move has been large and consistently
  one-directional on both observed days (+0.82%, +0.62%) while Sensex's own
  print is comparatively flat — user lost money on the short-call side and
  disabled Artemis indefinitely pending investigation into suspected
  closing-auction manipulation (buy pressure in the cash segment during the
  auction, thin sell-side since fresh short sales can't settle as delivery
  there). New `cas_auction_tracking.csv` (gitignored) logs the daily move
  automatically, CSV-only, no Slack. Only 2 days of evidence so far — don't
  treat the pattern as statistically confirmed yet.
- **Iris live validation** — Iris took the VIX > 25 slot on 2026-06-18,
  replacing Apollo in routing. Static 40 lots; dynamic sizing deferred.
  Apollo remains in Leto solely to manage any residual open positions.
- **Poseidon Step 0 (complete)** — `research/mtm_equity/` built a
  portfolio-level MTM equity curve at 1-min resolution (2020–2026, 347
  trades). Finding: true intraday MTM max DD ₹18,986 vs realized ₹14,537
  (1.3× gap — below the 1.5–2× weak-evidence threshold). The full-sample
  "hidden risk" argument is weak; the case for a trend overlay rests on
  the narrower proactive-window argument (2020 COVID replay showed a
  ₹2,031 dip on ~₹12K equity that recovered in 2 days). Per the plan's
  §8 fallback, check whether lowering Iris's VIX-activation threshold
  covers the same gap more cheaply before building Poseidon.
- **Range detection follow-up** — Apollo chop-filter annotation (step 5).
  No trade filtering discipline: trades taken every week, optimise the trade
  itself.
- **Proposed strategies in `plans/`:**
  - **Ares** (`range-vega-strategy.md`) — range-anchored, vega-adaptive
    strategy unifying both axes.
  - **Poseidon** (`trend-overlay-strategy.md`) — continuous, VIX-independent
    trend-following / crisis-alpha overlay for the proactive window before
    VIX > 25 handoff. Gated on a Step 0 MTM equity-curve diagnostic.
- **Backtest variants** — `athena_backtest/` carries multiple research
  variants (`backtest_tc.py` TRIPLE_CONFIRM, `backtest_wing_salvage.py`,
  `backtest_ml_adaptive.py`, `backtest_adaptive_exit.py`).
  `artemis_backtest/backtest_tc.py` is the parallel TRIPLE_CONFIRM track.

---

## 6. Repository Layout

```
leto.py                      # Session manager + router (cron entry)
leto_config.py               # Leto constants + routing override loader
slack_listener.py            # Slack Socket Mode daemon (control panel)
websocket_feed.py            # SharedFeed — real-time LTP/order fill

{artemis,athena,apollo,iris}_production/   # Live execution per strategy
{artemis,athena,apollo,iris}_backtest/      # Backtesting per strategy

leto_backtest/               # Integrated routed-portfolio simulation
research/                    # Exploratory modules (oi, greek, range, vix, mtm_equity)
plans/                       # Version-controlled implementation plans
data_pipeline/               # Historical data downloaders (AngelOne + ICICI)
data/                        # Shared runtime data (gitignored — creds, holidays, masters, routing_state.json)
logs/                        # Centralized logs
tests/                       # pytest suite
```

---

## 7. Working With This Codebase

**Read `CLAUDE.md` first.** It is the authoritative conventions reference. Key
rules an agent must follow:

- **Parameters go in `configs.py` only.** No magic numbers in backtest or
  production scripts. `configs.py` is the source of truth, not READMEs or
  prior conversation — always read it before assuming any value.
- **One variable changed per experiment.** Strict controlled-testing
  discipline for parameter calibration.
- **Function-based, not class-based** for the backtest layer. Do not refactor
  backtest functions into classes.
- **Module naming at repo root** — never name a repo-root Python file the same
  as a file inside a strategy directory (Python caches by module name in
  `sys.modules`). Hence `leto_config.py`, not `configs_live.py`, at root.
- **Module naming across strategy directories** — the same collision applies
  strategy-to-strategy, not just root-to-strategy: `leto.py`'s re-routing loop
  can import more than one `*_production/` strategy's modules within a single
  process. Hence `iris_configs.py`/`iris_functions.py`/`iris_state.py`/
  `iris_logger_setup.py` (and the equivalent `athena_*`/`artemis_*`/`apollo_*`
  names), never bare `configs.py`/`functions.py`/`state.py`/`logger_setup.py`
  inside any `*_production/` directory. Fixed 2026-08-07 — see
  `plans/strategy-module-naming-collision-fix.md`.
- **ELM exits are SEBI-regulatory, not risk-management SLs.** Never include
  them in SL optimisation. Only `index_sl` and `option_sl` are tunable.
- **State files** — only reconstruct fields that persist unchanged (booked PL,
  entry prices, tokens, symbols, strikes). Fields recomputed at startup
  (e.g., `option_sl`, `index_sl` in Artemis) are derived from entry data.
- **Security** — credentials in `data/user_credentials.csv` (gitignored).
  Never commit credentials. Never log secrets.
- **Plans** — write plan files to `plans/<name>.md` (version-controlled), not
  to `.claude/`.
- **Python execution** — never use `python -c "..."` for ad-hoc analysis.
  Write to `/tmp/claude_<name>.py`, run it, then delete it.
- **README and REQUIREMENTS.md** — update in the same commit as any feature
  that touches documented architecture.

**Production QC before any live session, especially after code changes:**
1. Run the full test suite: `python -m pytest tests/`
2. Read the entry-path code end-to-end (e.g.,
   `initialize_spread → execute_spread → execute_trade`).
3. For import audits, grep each source module's exported names and each
   consumer's import lines explicitly. Do not report "all clear" without
   shown work.

**Common commands:**
```bash
python -m pytest tests/                              # tests
python leto.py                                        # production session (cron)
sudo systemctl start slack_listener                   # Slack daemon
python artemis_backtest/backtest.py                   # backtest (example)
python leto_backtest/run.py                           # integrated routed backtest
bash data_pipeline/run_angelone_downloader.sh         # Sensex data (VPS daily)
bash data_pipeline/run_icicidirect_downloader.sh      # Nifty data (local weekly)
```

**Slack channels** (defined in `leto_config.py`):
- `#tradebot-updates` — session lifecycle (login, logout, WS status, EOD report)
- `#trade-alerts` — entries, exits, SL hits
- `#trade-updates` — periodic in-trade P&L
- `#error-alerts` — exceptions, feed failures

---

## 8. Dependencies

Core: `SmartApi` (Angel One SDK), `breeze-connect` (ICICI data), `pandas`,
`numpy`, `pyotp`, `requests`, `mibian` (options Greeks), `slack-bolt`,
`websocket-client` / `SmartWebSocketV2`. See `REQUIREMENTS.md` for the full
list and install commands.
