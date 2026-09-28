# Plan: Hestia, an MCX multi-strategy host and shared engine, with Selene as its first new strategy

**Status: PROPOSAL v3.2, 2026-09-28, nothing built.** v3.1 recorded the user's answers to v3's open questions and two clarifications (§1.3, §1.4). v3.2 fixes the remaining values: retry and silence thresholds derived from a measured Angel One call pattern (§1.5, §5), and the order priority rules for clashes between engines (§1.8). History: v1 recommended a second Angel One account (ruled out by the user); v2 proposed a Leto-style host running Prometheus and Selene as threads; this version follows two further decisions by the user. **(1)** The host is designed independent of Leto: MCX only, hosting 2 to 4 strategies (Prometheus, Selene, and candidates such as Gold Petal and Natural Gas Mini). **(2)** The split of responsibility is the user's: **each strategy engine evaluates and decides (entries, exits, stop-losses, and the roll decision, since it is part of the core strategy) and receives processed data (LTP, candles, ST) from a shared engine; the shared engine performs the actual work (orders, fills, data, resilience).** The host is named **Hestia**, after the goddess of the hearth: hospitality is what a host is, and her fire was never allowed to go out. Read after two advisor reviews and the code of `leto.py`, `websocket_feed.py`, `prometheus_production/`, `plans/prometheus-phase3-production.md` and the AB1007 incident record. The backtest side is in `plans/selene-silvermic-st-strategy.md` (§12–§16): SILVERMIC, Supertrend 10 / 2.5 on 15-minute bars, trend-flip exit, 3.0% protective stop, no targets, one lot per unit, positional. The user manages leverage manually and wants sizing conservative: `DYNAMIC_SIZING` off, `STATIC_UNITS` the only lever.

**Why one process at all.** Angel One allows one live trading session per account: a second `generateSession()` evicts the order-placing ability of any session already running (AB1007, which caused the 2026-08-31 and 2026-09-15 live incidents), even for a DRY_RUN process. So strategies on this account can only coexist inside one process that owns the login. Leto shows the pattern (one login, `obj` injected, strategies never touch the session); Hestia generalizes it to strategies that run **concurrently** and hold positions overnight, which is the scope beyond Leto.

**The user's split is also the best way to get "the same failsafes for every strategy":** the failsafes (ghost-order recovery, fill-confirmation invariant, freeze chunking, margin check, reconciliation, DPL detection, gap recovery, price-artifact protection, Slack, logs, kill flags) live once, in Hestia, instead of being copied into each strategy fork. The cost is that Prometheus's data and execution code moves out of its 3,531-line engine, which is a far bigger change than v2's refactor and is planned as a **port beside the old code, not an in-place refactor** (§4).

---

## 1. The engine ↔ Hestia interface (everything else hangs off this)

### 1.1 Who owns what

| Hestia (shared engine and host) | Each strategy engine |
|---|---|
| The one login and teardown; calendar, holiday and evening-only gates; PID, flags, signals | Signal logic and parameters (ST period and multiplier, stop %, targets or none, sizing policy) |
| Instrument master, contract calendar, lot size, tick, freeze quantity, days to expiry | **Which contract to trade and when to roll**, including early-roll timing, coincident-flip logic, veto, basis recalibration |
| Candle fetching, seeding, the private today-cache, gap recovery, bar building, per-engine ST computation, price-artifact protection, provisional-boundary computation | **Entry, exit and stop-loss decisions**, evaluated on the data Hestia delivers |
| LTP feed with staleness watchdog and REST fallback, DPL limits | Its own decision state (see §1.6) |
| Order execution: placement, chunking, ghost-order recovery, fill confirmation, Rule 7 netting, retries; margin reservation and limits | Sizing policy: how many units to ask for |
| Position ledger and reconciliation with the broker book; trade log; Slack; session report; logs | Its own summary and its per-strategy report fields |

### 1.2 Data Hestia delivers

Each engine registers a **data spec**: a set of instruments and contracts (not a single token), the timeframe, and the ST period and multiplier. ST is therefore computed **per engine**: Prometheus uses 2.0 and Selene 2.5, so it is never computed once per token.

- **Bar-complete events** carry the bar, the ST value, trend and flip, and **data-quality flags**: `complete`, `recovered`, `partial`, `provisional`, `gap`. Hestia reports quality; **the engine decides what to do with it.** Prometheus's refusal to act on a gap and its gating of provisional-boundary decisions stay in the Prometheus engine.
- **Latest LTP with its age**, and tick-aggregated OHLC (pulled by the engine, not pushed).
- **Provisional bar events** (§1.9): at a 15-minute boundary whose REST window is incomplete, Hestia builds the bar from its own tick-OHLC harvest and computes the provisional ST with the engine's parameters, and later emits the real bar flagged as reconciling that boundary.
- **Position book filtered by the engine's tokens**, from Hestia's ledger.
- **DPL limits** for the engine's contracts, and **available cash** (the shared pool).
- **Session events**: session start (with the seed state), session end, a rollover-eve notice from the calendar (days to expiry are data, the decision is the engine's).

### 1.3 Roll support (data, not decisions), and how Hestia knows to track another contract

Hestia never decides that a second contract is needed; **the engine tells it.** The mechanism, in the same order production does it today:
1. Hestia publishes each instrument's **contract list** as data: every listed contract with its token, expiry, lot size, tick, freeze quantity and **trading days to expiry** (holiday-aware, from `mcx_holidays.csv`).
2. At session start the engine's roll policy evaluates that data against its own rule (for every engine today, the roll window of 5 working days, §1.7) and asks whether tomorrow's contract differs from today's, exactly what `_check_rollover_tonight` does in Prometheus.
3. If a roll is due, the engine sends Hestia `track(contract)` for the next contract. Hestia seeds it from the pipeline files, builds its bars and computes its ST with **that engine's** period and multiplier, keeps it current all day, and answers point lookups (`historical_basis_price` and `compute_st_for_contract` equivalents). It costs polling budget, which is why it is on demand and only on roll-eve days, not permanent; the nightly pipeline already captures the next-month file for every instrument.
4. When the roll completes, the engine sends `untrack` and the new contract becomes its trading contract.

So the engine's data spec names its **trading contract** (Hestia serves it from the start), and everything else is a `track` request the engine makes when its own logic says so. The roll logic itself (the eve check, coincident-flip re-entry, the fallback at rollover time with the ST-disagreement veto, basis recalibration) stays in the engine, built from a **shared roll-policy library** that every engine imports and parametrizes: reusable functions, not engine core, so an engine can adopt or replace them.

### 1.4 Execution API

- **Requests:** `open`, `close`, `flip` (Rule 7 combined order, netted by Hestia), `flatten`. **An engine sends one request and waits for the outcome. Everything after that is Hestia's job:** placing the order, chunking, retrying rejections, ghost-order recovery, verifying the fill, and reporting a final outcome. Engines never retry an order. (This is a change from today, where the engine's tick loop re-issues an unconfirmed exit.) A roll transition stays **two separate requests with a confirmation between them**, because different tokens cannot be netted (Prometheus §18 issue 1); Hestia guarantees the second is sent only after the first is confirmed.
- **Outcomes:** `filled`, `partial`, `rejected`, `margin_refused`, `limit_refused`, `unconfirmed` (a state Hestia keeps working on until it resolves, after which a final outcome is delivered). **The fill-confirmation invariant is an interface rule: an engine changes its state only on a confirmed outcome.**
- **Request IDs, and why they still exist.** The one case where a duplicate can still arise is an engine **restart**: with auto-resume from saved state (§1.5), an engine that crashes right after sending a request cannot know whether it was sent or is still in flight, and would send it again. So each request carries an ID that the engine creates when it makes the decision and persists **before** sending. Hestia keeps every ID it has seen with its outcome, and a repeated ID gets the existing status or outcome back instead of a second order. This is a small safety net for restarts, not a retry mechanism. **Consequence for engines (found in P1, `plans/hestia-p1-inventory.md` §1 item 2):** today's stop path retries implicitly, because an unconfirmed exit leaves the state `open` and the next tick fires it again. An engine therefore needs an explicit **request-pending state**: while its exit request is in flight, its stop evaluation must not produce a second request.
- **Engine crash.** Hestia never cancels an order that is in flight; it persists the outcome so the engine's resume reconciles against it.

### 1.5 Threading and latency

- Each engine is **one thread**. Nothing calls back into an engine's state from another thread. Events and fill outcomes arrive on a **per-engine queue**; LTP is pulled.
- Order placement and fill waits run **concurrently across engines**, so one engine's stop never waits behind another's 30-second fill wait. Hestia's broker gateway holds its lock only around the HTTP call, never around a wait, and has priority lanes so order, position and LTP calls never queue behind bulk candle fetches (for example Selene's 18-day seed).
- **A crash is contained and auto-resumed (user decision).** An exception in an engine is caught at the thread boundary and alerted; Hestia **restarts that engine from its saved state** with a retry limit (**3 restarts within 30 minutes**, backoff of 5 s, 30 s and 120 s with about 20% jitter, the counter clearing after 60 quiet minutes; if two or more engines are restarting at once their restarts are serialized 10 s apart, and a restart's seed fetches run on the gateway's low-priority lane so they cannot starve an engine that holds a position) and the other engines and the session continue. The resumed engine reconciles against Hestia's ledger (§1.6) and against any in-flight request (§1.4). When the limit is exhausted the engine is marked failed, and Hestia sends a critical Slack alert that repeats on a debounce while any position of that engine is open.
- **A silent engine raises an alert (user decision).** Each engine has a heartbeat. If an engine has not completed a callback or heartbeat within a threshold (**warning after 3 minutes and critical after 5 minutes** without a heartbeat during market hours, repeated every 5 minutes; the heartbeat is the engine's own loop cycle, not market ticks, so a quiet market does not trip it, and monitoring pauses while an engine is in a known deferred start such as an evening-only session), Hestia fires a critical Slack message naming the engine, its open position and the age of its last heartbeat, and repeats it on a debounce until the engine recovers or a human acts. Hestia does **not** enforce a stop on the engine's behalf: stop evaluation stays entirely in the engine, as specified. A hung Python thread cannot be killed safely, so silence is an alert, not an automatic action.

### 1.6 State

Two kinds, and the boundary matters for resume: **decision state** (what the engine believes: last processed bar, pending intents, roll bookkeeping) is the engine's, persisted by Hestia as an opaque blob per engine; the **position and order ledger** (what actually filled, per token) is Hestia's and is the single source of truth, because it is built from broker-confirmed fills and reconciled against `position()` at startup and periodically. On restart the engine reads the ledger and reconciles its decision state against it; where they differ, the ledger wins and the engine is told. **P1 found that Prometheus already works this way:** its pending markers and rollover latches are deliberately in memory only, and recovery is reconstruction at setup from the persisted state file, the broker's position book, the missed-flip watermark and the missed-rollover check, so the decision-state blob need only hold what Prometheus persists today plus the request IDs. The one case needing explicit design is a restart inside a session with a half-executed flip or roll.

### 1.7 Roll window, and the hard limits Hestia enforces

**One rule for every commodity (user decision):** roll before the tender margin period starts, which is **5 working days before expiry**, the same logic as Prometheus today: on a roll day, if the trend flips, exit the old contract and enter the new one **provided the new contract's ST agrees**; if the trend has not flipped by the rollover time (23:15, or 23:40 when the session closes at 23:55), close and reopen on the new contract with the ST-disagreement veto and the stop recalibrated off the historical basis. The 5-day default lives in `hestia_config.py` and can be overridden per instrument, but is uniform to begin with (Natural Gas Mini is cash-settled and does not strictly need it; Gold Petal is physical and does). Because engines roll first, the delivery-window situation should never occur.

Hestia still enforces a small set of **hard limits at the execution layer** that keep the last safeguards out of reach of engine bugs, without Hestia making strategy decisions: freeze quantity handling, the per-engine unit cap, margin reservation, and a **refusal of any entry into a contract inside its roll window**. If a position is ever found held inside the window, Hestia does **not** flatten it; it fires a critical Slack alert (the roll machinery has failed, and the user decides) and keeps repeating it.

### 1.8 Order priority when engines clash

Throughput is not the problem: the account-wide order cap in use today is 10 orders a second (`ORDER_LIMIT` in the Apollo and Athena configs), an entry or exit is one order (Rule 7 nets a flip into one), and chunking only happens above the 600-lot freeze quantity. The clashes that matter are **margin** and **latency**. Rules, all in Hestia:

1. **Risk-reducing before risk-adding.** Priority classes, highest first: (1) stop-loss exits and `flatten`; (2) other closes, including the exit half of a `flip`; (3) a roll transition's exit leg; (4) entries and the entry half of a `flip`; (5) a roll's reopen. Within a class, first come first served by request time; ties break by the engine's position in the registry, so the order is deterministic and testable.
2. **Exits go first so entries see the freed margin.** Entries **reserve margin atomically at admission** against the shared pool; if the pool cannot cover a second simultaneous entry, it gets `margin_refused` immediately (no waiting in a queue for cash, because a stale entry is worse than a skipped one) and its engine decides what to do, as Prometheus does today with an insufficient-margin entry.
3. **No head-of-line blocking.** Each request runs in its own worker from a bounded pool, so an exit is never stuck behind another engine's 30-second fill wait. The order token bucket (10 a second) **reserves a share (4 a second) for the risk-reducing classes**, so a burst of entry chunks cannot starve a stop-out, and a large chunked entry is scheduled chunk by chunk so an exit from any engine can interleave.
4. **One engine per instrument**, enforced when the registry loads, because two engines on one contract would net at the broker while Hestia's ledger attributes fills to engines.
5. **Same-engine order is preserved** (a roll's exit is confirmed before its reopen is sent), and there is **no netting across engines**.
6. **Coinciding roll times** (every engine rolls at 23:15, or 23:40 on 23:55 sessions): all exit legs run first, then the reopens; registry `roll_offset_sec` values stagger the engines by a few seconds so they do not hit the API in the same instant.

### 1.9 Provisional-boundary trading (applies to every engine that opts in, including Selene)

**What it is.** When Angel One's candle endpoint has not delivered the full 15-minute window at the boundary instant (the `AB1021` stretches), Prometheus builds a provisional bar from the WebSocket's tick-aggregated OHLC, computes a provisional ST verdict, and, if the verdict is a flip and the margin condition holds, **acts immediately** (a flip or an entry) instead of waiting minutes for the real bar; when the real bar arrives it reconciles, and any disagreement raises a critical alert and switches provisional action off for the rest of the session, with no automated reversal. **Why it matters for Selene:** every backtest in this project fills at the open of the bar after the signal bar, that is, at the boundary price. Without provisional action, a live fill on an `AB1021` day happens minutes later than the backtest assumes; with it, live timing matches the backtest convention again.

**Split under Hestia.** Hestia harvests the tick OHLC (one consumer per token, because `SharedFeed.get_ohlc` resets on every read, so two readers would starve each other), builds the provisional bar, computes the provisional ST with the engine's own period and multiplier, and delivers it with the `provisional` flag. The **engine decides whether to act**, sends an ordinary `flip` or `open` request, keeps the pending-reconciliation record, compares against the real bar when Hestia delivers it, and latches provisional action off for the session on a disagreement. Provisional action stays suppressed while a roll or a flip is in progress, as today.

**A defect in the margin guard, found while checking this (P1 follow-up).** Prometheus's guard `PROVISIONAL_MARGIN_PCT = 0.15` (a placeholder, "not calibrated") measures `band_dist_pct = |close - supertrend| / close`, but on a flip bar the supertrend has already switched to the *new* band, which sits far from the close. Measured on real data, that distance on flip bars has a median of 0.74% for SILVERMIC (2.5) and 1.35% for CRUDEOILM (2.0), and it is at or below 0.15% for **0.0% of SILVERMIC flips and 0.2% of CRUDEOILM flips**, so the guard passes essentially every flip and does not gate anything. What would matter is how far the closing price cleared the *old* band it had to cross: for SILVERMIC the median is 0.091% of price, **34% of flips cleared it by 0.05% or less and 65% by 0.15% or less** (CRUDEOILM: 18% and 44%). Those razor-thin flips are exactly where a tick-derived close and the exchange candle's close could land on opposite sides. So the guard as written protects nothing in either engine.

**Decision and fix (user, 2026-09-28): the check is made against the previous bar's supertrend, for Prometheus and for Selene.** The provisional close must clear the **previous bar's ST**, the line it had to cross, by more than the margin (`PROVISIONAL_MARGIN_PCT`, a config value per engine). Done in Prometheus's live code the same day (`_evaluate_provisional_boundary`, `prometheus_production/prometheus.py`, with the margin comment in `prometheus_configs.py`) and pinned by `tests/test_prometheus_provisional_margin.py` (7 cases, of which the three thin-cross ones fail on the old code and pass on the fix). It takes effect on Delos after a pull and a restart, which is the user's call and follows the restart procedure. The Prometheus engine in the Hestia port and the Selene engine both implement this same rule, and the replay harness reproduces provisional cases by degrading the candle feed while keeping ticks.

**The threshold now matters, and 0.15% is not neutral.** With the corrected measure the guard is a real gate. On history, a 0.15% margin lets 35% of SILVERMIC flips (ST 2.5) and 56% of CRUDEOILM flips (ST 2.0) act provisionally; the rest wait for the real bar, exactly as if the feature were off. Selene starts at the same 0.15% as Prometheus, as a per-engine config value. Whether 0.15% suits silver (median clearance of a flip 0.09%, against 0.18% for crude) is to be settled in the Selene DRY_RUN from Hestia's shadow log for every provisional verdict (provisional and real direction, and the clearance): the threshold is raised or lowered from observed agreement, the disagreement latch stays, and a stretch of clean agreement is required before Selene's first live session.

---

## 2. Hestia's components

| Component | Notes |
|---|---|
| **Session and lifecycle** | The only `generateSession`/`terminateSession`, run after every engine thread has finished; holiday and evening-only gates from `mcx_holidays.csv` (one reader); PID and active flags; SIGINT/SIGTERM handled in the main thread; teardown order: stop engines, wait with timeout, flush Slack, terminate |
| **Session lock** | A lock file that whichever process owns the Angel One login claims; any other login site (Leto if it is ever active, the 23:56 downloader, one-off scripts) checks it and refuses. This generalizes today's guardians, which only read state files for open positions and run after the login that already did the damage. Independent of Leto's architecture; Leto would need a three-line check |
| **Broker gateway** | Wraps `obj`; lock only around the HTTP call; per-endpoint budgets account-wide (the strictest values already in use: candle 3/s, LTP 10/s, orders 10/s; **check them against Angel One's published SmartAPI limits in P1**, since only the repo's caps are verified here), replacing the module-global `_candle_counter` and `_ltp_counter`; priority lanes; used by the data and execution services |
| **Data service** | Contract resolution facts, candle fetching, seeding from the pipeline files, the private today-cache, gap recovery with the pending-recovery queue, bar building, per-engine ST, price-artifact protection, provisional boundary, contract tracking on request (§1.3); budgeting for up to 8 tokens when two instruments are both near a roll |
| **Market feed** | One host-owned `SharedFeed`; engines subscribe and unsubscribe only their own tokens through Hestia; watchdog and REST fallback for LTP |
| **Execution service** | `place_order` with chunking and ghost recovery; the **one** `OrderFillWatcher`, routing by order id; the shared `_placed_order_ids` collision guard (today one module-level set); Rule 7 netting; idempotency (§1.4); **order slicing across minutes** with a participation cap for large sizes, which no engine or backtest has today and which belongs here so every engine benefits |
| **Ledger and reconciliation** | Position and order ledger per token (already keyed by `symboltoken` in `prometheus.py` around line 1626); startup and periodic reconciliation; ghost recovery already matches `tradingsymbol` (verify it cannot pick up another engine's order) |
| **Risk service** | Margin pool (`rmsLimit()['data']['availablecash']` is one pool): reservation across engines so two entries in the same second do not both pass on the same cash, a cushion, a portfolio cap and per-engine allocation limits; DPL detection (alert-only, Prometheus §11a) with per-instrument limits read from the broker; the delivery-window refusal (§1.7) |
| **Reporting** | One Slack queue and worker with flush; per-engine tags; per-unit Rs convention; the realized/unrealized session-report split; a combined P&L and margin report; trade logs per engine; a named logger and dated log per engine plus Hestia's own |
| **Flags** | A host flag (ends everything) and one flag per engine (ends only that engine); `EXIT`, `KILL`, `DISABLE` semantics per flag; a Slack listener driven by the engine registry, so a new engine needs a config entry, not new listener code |

**Per-engine isolation (P1 findings).** Configuration is per-engine objects, not import-time module globals (Prometheus resolves `SYMBOL`, `LOT_SIZE`, file paths and sizing at import); the intraday cache and the 15-minute series dump are per engine and per token (today one shared cache file is rewritten whole on a contract switch, which would race between threads); trade counters, state files and logs are per engine; the ledger writes on change, not on every LTP tick.

**Configuration.** `hestia_config.py` at the repo root (matching `leto_config.py`) holds the engine registry: for each engine its instrument, enabled flag, sizing cap, budgets, per-instrument settlement type, delivery window, session hours, and liquidity gate. Services live in a `hestia_core/` package with prefixed module names; engines in `prometheus_engine/`, `selene_engine/` and so on (bare module names such as `prometheus_functions` collide in `sys.modules` with shared imports, per the repo rule).

---

## 3. Engines

- **Prometheus engine (port).** The decision layer of today's Prometheus, rebuilt against §1: 2-lot scale-out, T1/T2 targets, stop, the §4–§9 and §18 roll logic (as the shared policy library), lot-aware sizing. `prometheus_production/` stays untouched as the rollback and as the reference for verification.
- **Selene engine (new, small).** Single position, `units` lots, no targets, stop 3.0% fixed at entry and recalibrated off the historical basis only on a fallback roll, ST 10 / 2.5 on 15-minute bars, **provisional-boundary trading on (user decision, 2026-09-28; §1.9)**, flips of `2 × units` lots through `flip`, the shared roll-policy library, sizing from a manual override read live per entry (§19 pattern), margin per unit `price × 1 / 8 × 4` computed live (§26 pattern) and checked through Hestia. No crude-specific pieces (opening-bar correction, reference symbol).
- **Future engines, in the user's order: Gold Petal, then Natural Gas Mini.** Working names: **Helios** for Gold Petal (the Titan of the sun, the classical pairing of the Sun with gold, and Selene's brother, both children of Hyperion and Theia) and, for Natural Gas Mini, **Typhon** (the fire-breathing storm-giant, for a volatile, weather-driven fuel; chosen by the user, 2026-09-28). Each is an engine plus a registry entry. **Onboarding checklist per instrument:** the research trajectory (raw sweep, exits, parity backtest, sizing, ruin) exists; the liquidity gate is passed (`research/mcx_liquidity_screen`; **Gold Petal stays blocked on the open zero-volume-bar discrepancy** until it is resolved); settlement type and delivery window (Gold Petal settles physically, Natural Gas Mini is cash-settled); lot size, tick, freeze quantity and DPL; roll window and next-contract availability in the pipeline (`mcx_underlyings.csv` enabled, next-month tracking); session hours.
- **Selene-specific inputs.** SILVERMIC is enabled in the pipeline; today's files are Nov-2026 and Feb-2027, both with genuine capture since 2026-09-02 (16 sessions; `SEED_DAYS = 18` calendar days already met). Nov-2026 expires 2026-11-30 (roll eve about 2026-11-20); Feb-2027 expires 2027-02-26 (Apr-2027 is in the instrument master and is tracked as next-month from 2026-12-01). The risk is a next contract not yet listed or tracked at roll time; the roll-window refusal (§1.7) and the engine's roll policy must both treat "no next contract" as flatten, not "carry through the tender window", which is what `resolve_effective_contract` does today with a warning.

---

## 4. Migration: port Prometheus, do not refactor it in place

Under this split Prometheus's data and execution code leaves its engine. **Build `prometheus_engine` against Hestia beside the old code; do not touch `prometheus_production/`.** That standalone process, with its own login, is the rollback cron line and the only thing that can trade while Hestia is being built.

- **No live shadow.** Because the standalone live Prometheus holds the only session, a Hestia-hosted Prometheus cannot run alongside it, not even in DRY_RUN. Verification is **replay only**: recorded live days replayed through the fake Hestia and compared with what live Prometheus actually did (`prometheus_trades.csv`, the dated logs, the recorded state), decision for decision, then a cutover in which the old process is stopped and Hestia runs Prometheus alone for several sessions. The recorded live data is on Delos (per-command approval to pull) and covers real fills since 2026-09-15.
- **Cutover gate:** replay agreement on the recorded days including at least one trend flip with Rule 7, a stop, a roll or the machinery around one, and the injected-fault cases; a full test suite; the concurrency and idempotency tests of §8.
- The old entry point stays in the repo and unscheduled after cutover, so returning to it is a cron edit.

---

## 5. One account, and who else logs in

The account's login consumers today: Prometheus (from 09:00), the data pipeline's merged 23:56 downloader (after Prometheus closes), one-off scripts, and Leto's NSE/BSE strategies, whose **cron the user confirms is disabled** (2026-09-28). Leto's `_login()` checks nothing for Prometheus (the existing guardians read only state files and run after the login), so if Leto is ever re-enabled the same eviction hazard returns; the **session lock** (§2) is therefore still worth having, but it is a small item now rather than a prerequisite. Hestia never logs in twice, and never hosts NSE/BSE strategies (out of scope).

Unverified and to settle in P1: whether concurrent calls on one `obj` are safe (`SmartConnect` holds a `requests.Session`; the gateway serializes them regardless); Angel One's per-account limits on WebSocket and order-update connections (Hestia uses one of each); and refining the candle-call measurement below with the live sessions since 2026-09-15.

**Measured candle-call pattern (from the full-session Prometheus log of 2026-09-01, DRY_RUN, `prometheus_cron_20260901.log`, on this machine):** Prometheus makes **one `getCandleData` call per minute** (a rolling 5-minute window) in steady state, so about 60 an hour, which is what the quiet evening hours 17:00 to 22:00 show. During 09:00 to 16:00 it rose to 89 to 154 attempts an hour because **35% of the day's attempts failed** with Angel One's `AB1021` (484 of 1,378), producing 70 exhausted bursts and up to 6 attempts inside one minute; 213 of the 894 successes needed more than one attempt. Even so the longest gap between attempts was 61 s and between successes 124 s, so a healthy engine's loop cycles about once a minute, and the silence thresholds in §1.5 sit well above that. Sizing the budget for four engines: one token each is about 4 a minute steady; all four on a roll eve (two tokens each) about 8 a minute; at the 2.6 times inflation seen that day about 21 a minute, roughly 0.35 a second, against a client cap of 3 a second. **The client-side cap is therefore not the constraint; Angel One's server-side `AB1021` refusals are, and they are not ours to budget** (closed with Angel One, no remedy proposed here). What Hestia can do is not amplify them: today an engine calls exactly at each minute boundary, so four engines would fire in the same second and trip the 3 a second limiter; registry `poll_offset_sec` values (for example 0, 5, 10 and 15 seconds) spread the calls. Since 2026-09-04 the inner retry is 5 attempts, not 3, so live sessions will show somewhat more attempts than this log.

---

## 6. Silver hazards and where each is handled

| Hazard | Handled by |
|---|---|
| **Physical delivery** (compulsory in SILVERMIC): holding into the tender period is an obligation, not a margin cost | The engine's roll policy (5 working days before expiry, confirmed by the user, uniform across commodities) **and** Hestia's refusal of any entry inside the roll window plus a critical alert if a position is ever found there (no forced flatten, by the user's decision); never re-enter that contract |
| **Overnight gaps and price limits** (the worst backtest trade lost 7.3% of price per lot because a stop filled through a gap; a locked market cannot fill a stop at all) | Hestia's DPL detection (alert-only) reading silver's real limits from the broker; the engine's sizing, since unit count, not the 3% stop, is the real protection |
| **Liquidity** (median volume 300 lots in a fill minute; a comfortable order is 30 to 75 lots, a sensible ceiling near 100 units) | Hestia's execution slicing with a participation cap above roughly 50 units; config cap of 50 units to start |
| **Thin or absent next-contract data at roll eve** | The data service (contract tracking, seed completeness flags); the engine (no next contract means flatten) |
| **Costs are thin relative to edge** (average trade 0.14% of price; 3 bps per trade more than doubles the ruin probability at 2× notional) | Sizing and slippage study in the backtest plan §15–§16; a real coefficient from live fills replaces the assumed one |

---

## 7. Sizing and portfolio risk

- Manual and small: `DYNAMIC_SIZING = False`; `STATIC_UNITS` from a per-engine `sizing_override.json` set through the registry-driven Slack listener, read live per entry; hard cap in config (50); first live entries at 1 unit.
- Backtest input to the manual choice: at `× 4` (2× notional to capital) the two-year bootstrap gives P(drawdown > 40%) 18.9% and P(ruin) 5.1%; at 1.5× it is 4.1% and 1.4%; at 1× 0.1% and 0.1%.
- **Portfolio.** Prometheus and Selene are both trend-following commodity positions and both lived through 2026; their drawdowns are not independent and they share one margin pool. With 2 to 4 engines the risk service (§2) is what stops them from over-committing that one pool. A **combined P&L and margin-utilisation report** (all engines, one message) and a host flag that stops everything are proposed.

---

## 8. Testing, in the order it must happen

1. **Interface contract tests** against a **fake/replay Hestia**: every event type and outcome, data-quality flags, idempotent intents, unconfirmed intents resolving later, engine restart against the ledger.
2. **Concurrency tests**: a raising engine, a hung engine, a slow order, rate-limit fairness and the priority lane, a per-engine KILL that leaves the other running, signal handling and teardown ordering (session terminated only after every engine has finished), duplicate intents with the same key.
3. **Engine unit tests**: the four parity branches the historical data never exercised (flat switch, stop on a roll eve, NO-GO veto, missing-data fallback; required by backtest plan §13), the roll-window refusal, single-position Rule 7 lot math, fallback-roll stop recalibration, per-token reconciliation, the sizing override path, and ports of the Prometheus resilience tests (`tests/test_prometheus_*.py`, about 1,750 lines).
4. **Replay vs live Prometheus** (§4) and **replay vs the parity backtest** for Selene (trade for trade on the same window, except where faults are injected; the parity simulator matched the earlier pipeline on 99% of trades).
5. **Hestia live with Prometheus only**, several sessions and ideally through a CRUDEOILM roll, watched with the existing QC skill.
6. **Selene DRY_RUN inside Hestia**, safe because there is no second login, through a full SILVERMIC roll (next roll eve about 2026-11-20).
7. **Live at 1 unit**, then the user's ramp; Prometheus's own ramp is the template.

A note for later: because engines are decision functions over Hestia's events, the **same engine code can be driven by the backtest data loop through the fake Hestia**, which would replace the hand-written parity simulator as the backtest of record and remove a class of backtest/production drift. Not required, worth considering once the interface exists.

---

## 9. Operations

- **Cron:** one entry for Hestia at `00 9 * * 1-5`; the old Prometheus entry stays as the rollback and is unscheduled after cutover.
- **Deploy coupling:** restarting Hestia to ship any engine's fix restarts every engine and redoes the login. Add a **no-restart window** around open-position events (an open roll transition, an in-flight intent, an unconfirmed exit) and make the restart procedure read engine and ledger state first; update the restart skill to a Hestia restart.
- **QC:** `hestia-qc` (reads Hestia's log, the ledger, and each engine's log and trade file) replacing per-engine QC skills as the primary check; per-engine views as sub-checks.
- **Delos:** every command that reaches Delos still needs the user's per-command approval.
- **Docs:** `hestia_core/README.md`, each engine's README, root `README.md` and `REQUIREMENTS.md` in the same commit as the code (repo rule).

---

## 10. Build order and gates

| Phase | Work | Gate |
|---|---|---|
| **P0** | Decisions in §11; Delos checks | User answers |
| **P1 (done 2026-09-28, `plans/hestia-p1-inventory.md`)** | Measurements and inventory: refine the candle-call measurement (§5) with the recent live logs; every module-level state, `sys.exit`, `signal`, `terminateSession`, `feed` and lot2 reference in the code to be ported; the data spec each engine needs | Written inventory |
| **P2 (drafted 2026-09-28: `plans/hestia-interface-spec.md`, `hestia_core/interface.py`)** | **The interface spec** (§1) written down as typed events, intents and outcomes | Reviewed by the user |
| **P3 (built 2026-09-28: `hestia_core/fake.py`, `fake_kernel.py`, `indicators.py`, `calendar.py`; `tests/test_hestia_fake_*.py`)** | **Fake/replay Hestia** built with the spec, plus the contract and concurrency tests (§8 items 1 and 2). A deterministic simulator: simulated clock, engines on real threads with one running at a time, replayed 1-minute data with per-engine Supertrend, a simulated broker with injectable rejections, partial fills, ghost orders and unconfirmed fills, and the account-wide order budget, priority classes, margin and hard-limit admission, `depends_on`, idempotent request ids, crash containment with auto-resume, silence alerts and KILL semantics. Building it changed the v1 interface in six small ways, listed under "P3 clarifications" in `plans/hestia-interface-spec.md` | Tests green (done: 78 Hestia tests, the guards mutation-checked) |
| **P4** | **Live Hestia** services (§2) | Suite green; gateway, idempotency and teardown tests |
| **P5** | **Prometheus engine parity** against replay of recorded live days | Decision-for-decision agreement |
| **P6** | **Hestia live with Prometheus only**, cutover with rollback ready | Clean sessions and a roll, QC report |
| **P7** | **Selene engine** (small), new tests, replay vs the parity backtest | Trade-for-trade agreement |
| **P8** | **Selene DRY_RUN inside Hestia** through a SILVERMIC roll; QC skill | Clean roll |
| **P9** | Selene live at 1 unit, then ramp | User's call at each step |

The effort is in P4 and P5: building the shared engine out of Prometheus's proven execution and data code without changing its behaviour. Selene itself (P7) is small once the interface exists, and each later instrument is an engine plus a checklist.

---

## 11. Decisions recorded and what is still open

**Decided by the user, 2026-09-28:** MCX-only host independent of Leto; the split (engines decide, Hestia executes); the name Hestia; one roll rule for every commodity (5 working days, coincident-flip entry when ST agrees, else rollover at 23:15 with the veto); Hestia refuses entries in the window and alerts, no forced flatten; no dead-man stop, a Slack alert when an engine goes silent; auto-resume from saved state with a retry limit; Leto's cron is disabled; a host flag and a combined P&L and margin report, **posted to `#tradebot-updates`**; start now; **Selene's first live size 1 unit with a 50-unit ceiling**; the retry, silence, polling and order-priority values in §1.5, §1.8 and §5 (proposed by me on the user's instruction to base them on the rate limits and to decide the clash order in Hestia); next instruments **Gold Petal (Helios), then Natural Gas Mini (Typhon)**; **the shared roll-policy library is written first, as a standalone module with its own tests**; **Gold Petal is deferred until Selene is running** (its zero-volume-bar question in `research/mcx_liquidity_screen/README.md` is not a blocker for now).

**Still open:** nothing blocking. Values to revisit after the first live sessions: the restart limit, the silence thresholds and the poll offsets. The 4/s share of the order cap reserved for risk-reducing orders is a starting value.

## 12. Non-goals

Hosting NSE/BSE strategies, any change to `prometheus_production/` while it is the live rollback, the shelved 1h entry filter, and any AB1021 remedy (closed with Angel One; the plan only budgets the shared rate limit).
