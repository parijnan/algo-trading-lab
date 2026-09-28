# Hestia P4: live services

**Status: slices 1 and 2 done (2026-09-28), slices 3 to 5 to do.** Phase P4 of `plans/selene-production.md` §10: build the live Hestia so that it passes the same contract tests as the fake. Gate: suite green, plus gateway, idempotency and teardown tests. Several sessions of work; reported and committed per slice, only when the user asks.

## Hard constraints

1. **Nothing in P4 touches Angel One.** Prometheus trades live on Delos on the same account; any `generateSession` from anywhere else evicts its order capability (AB1007), even in DRY_RUN, and the standing rule forbids ad-hoc logins while a live strategy trades. Every live adapter is tested against a scripted `SmartConnect` double (`placeOrderFullResponse`, `orderBook`, `position`, `rmsLimit`, `getCandleData`, `ltpData`, AB1021, `DataException`/`NetworkException`, token and session errors). The first real login is a later, explicit, user-approved step (P6 territory), not part of P4.
2. **Port, do not import, from `prometheus_production/`.** Its modules pull in import-time configuration and write into the real dated log. The parity tests already show the way: compile or copy the algorithm, pin it with a test.
3. **One implementation of the policy.** The fake and the live Hestia must not be two copies of admission, priority, reconciliation and the ledger, or they drift the way the two Supertrend copies could have. So the policy is extracted once into a broker-agnostic core, and the two differ only in their ports.
4. **Paper versus live is per engine, not per process.** P8 runs Selene in DRY_RUN inside the same Hestia as live Prometheus. A paper engine gets a paper broker adapter that fills at live LTP, and its ledger is excluded from broker-book reconciliation, otherwise the first periodic read "corrects" its position to zero.

## Architecture

```
HestiaCore (hestia_core/core.py)
  request registry, admission, priority dispatch with reserved workers, depends_on, ledger, risk (margin, unit cap,
  roll window), the UNCONFIRMED -> reconciling state machine, crash / silence / KILL supervision, EngineContext
  ports: Scheduler   now / at / after        SimKernel (tests)      RealReactor (live)
         BrokerPort  place / read_order /    SimBroker (tests)      AngelBrokerPort over the gateway (live)
                     free_cash / order-update listener
         DataPort    contracts, prices, bars, ST, tracking, boundary events
                                             ReplayData (tests)     LiveData over gateway + feed (live)
         task factory  baton-passed threads (tests)                 free-running threads (live)
```

FakeHestia = core + SimKernel + SimBroker + ReplayData. LiveHestia = core + a real-time reactor (one dispatcher thread runs callbacks; blocking HTTP runs in worker threads that post their completions back to it) + the Angel One adapter + the live data service. The contract tests run against the core through the fake; the live pieces get their own tests plus a small real-time smoke subset of the same contract.

## Slices

| Slice | Work | Gate |
|---|---|---|
| **1** | **Core extraction.** `ports.py`, `core.py`, `replay.py`; `fake.py` becomes a thin composition; `calendar.py` renamed `trading_calendar.py` (it shadowed the stdlib module name). Adds the BrokerPort read of a single order, so an UNCONFIRMED request is settled from its own order row and a still-pending order (`open`, `validation pending`, e.g. held by a DPL lock) is never reported as "no fill" | Every existing Hestia test passes unchanged apart from imports; new tests for the pending-order case |
| **2** | **Broker gateway and the Angel One adapter.** Account-wide per-endpoint budgets with priority lanes, one lock around the HTTP call, `place_order` ported (per-contract lot size, freeze chunking, rejection retry, ghost recovery with a shared placed-id guard, the `CLOSING_TIME` refusal), fills from the order-update WebSocket with REST fallback, settlement of an UNCONFIRMED order by its own `orderBook` row cross-checked against `position()`, ledger-versus-book reconciliation at start and periodically, paper adapter and per-engine paper/live routing | Tests against the scripted `SmartConnect` double; contract test with one paper and one live engine and a reconcile pass |
| **3** | **Reactor, workers, signals, teardown.** Real-time scheduler, free-running engine threads with thread-safe context, signal handling in the main thread, teardown order (stop engines, wait with timeout, flush Slack, then and only then `terminateSession`; an engine crash never reaches it) | Teardown-order and idempotency tests; real-time smoke subset of the contract |
| **4** | **Data service and feed.** Candle polling under the gateway budget, merge and recovery, bar building, per-engine ST, quality flags, provisional bars from the shared feed (one consumer per token), seeding from the pipeline files, per-engine private cache, tracking | Same bars and ST as the replay data for the same minutes; tests against the scripted double |
| **5** | **Host: flags, Slack, state files, session lock, config.** `hestia.py`, `hestia_config.py`, the registry-driven flags (`EXIT`, `KILL`, `DISABLE`, host flag), one Slack queue, dated logs per engine, state and trade files per engine, session lock, holiday and evening-only gates, combined report | Idempotency across a restart from the state files; flag semantics |

## Slice 1 record (2026-09-28)

Done. `hestia_core/core.py` holds the whole policy (`HestiaCore`, `CoreConfig`, the per-engine `CoreContext`); `ports.py` defines the three seams (`Scheduler`, `BrokerPort`, `DataPort`) and the small value types that cross them (`OrderSpec`, `PlaceResult`, `OrderRead`); `ledger.py` holds the position arithmetic shared by the core's ledger and the simulated broker's book; `replay.py` holds the simulated ports (`ReplayData`, `SimBroker`, `BrokerReply`, `ContractSpec`); `fake.py` is now a thin composition (`FakeHestia` = core + `SimKernel` + `SimBroker` + `ReplayData`) that keeps the scenario knobs the tests use. `calendar.py` became `trading_calendar.py` (and its test `test_hestia_trading_calendar.py`), because it shared a name with the standard-library module.

**Gate met:** the 83 Hestia tests that existed before the extraction pass with no edit to any test body, and the whole suite is green (288 passed, the 2 expected skips).

**What changed in behaviour, on purpose.** Only what the new BrokerPort makes natural: an UNCONFIRMED order is now settled by reading that one order (`BrokerPort.read_order`), and a row that says the order is still working (`pending`: `open`, `validation pending`, for example held by a DPL lock) is **never** reported as "no fill": the request stays UNCONFIRMED, later requests on that engine and contract keep waiting, Hestia re-reads every `reconcile_retry_s` (5 s), and raises one critical alert. Only a terminal row settles it (`complete` with the lots filled, which may be zero: rejected or cancelled). Two tests cover it (`test_order_still_working_at_the_broker_is_never_reported_as_no_fill` is mutation-checked). The simulated broker gained `BrokerReply.pending_for` to script it.

**Decisions for slice 2, noted while extracting.** The account-wide order budget (`_Bucket`, 10 a second with 4 reserved for risk-reducing classes) still lives in the core, where it gates each attempt. The gateway in slice 2 owns the per-endpoint budgets for the live calls (candle, LTP, order, book reads), so the order budget should move there and the core should only pass the priority class down; the contract test `test_order_budget_keeps_a_reserved_slice_for_exits` moves with it. The core still counts margin from `BrokerPort.free_cash()` less its own reservations; the live adapter reads `rmsLimit` for that. The paper-versus-live routing per engine and the ledger-versus-book reconciliation (excluding paper ledgers) are slice 2 as well.

## Slice 2 record (2026-09-28)

Done, against a scripted `SmartConnect` double only (`tests/smartapi_double.py`); no Angel One login was made or is possible from this code path in tests.

- **`gateway.py` (`BrokerGateway`).** One door to `obj`: a token bucket per endpoint (orders 10/s with 4 reserved for risk-reducing traffic, candles 3/s, LTP 10/s, and 1/s each for order book, positions and margin, the last three **unverified** against Angel One's published limits), one HTTP call at a time, a **priority lock** so a waiting stop takes the door before waiting normal calls, counters moved after the request even when it raises. No retry policy and no response interpretation live here.
- **`angel_broker.py` (`AngelBrokerPort`).** Ported from `place_order` / `get_fill_price_and_qty`, not imported: per-contract lot size, freeze chunking (a rejected later chunk settles what was placed as a partial and sends no more), rate-limit waits, lost-response recovery from the order book guarded by the ids this process placed and a 60 s freshness window, a bounded number of re-sends and then `unconfirmed` with a critical alert (never a blind re-send), session failures flagged, the market-close refusal, WebSocket fills with REST fallback, a `read_order` that aggregates a multi-chunk order and treats anything not terminal, missing or unreadable as **pending**, the position book converted to lots, and `free_cash` cached from `rmsLimit` and **fail-closed** (0 when missing or older than 120 s). The adapter makes one attempt and reports what happened; rejection retries stay in the core.
- **`order_feed.py` (`OrderUpdateFeed`).** The order-update WebSocket, ported; never connects on import, tests feed it messages directly.
- **Paper versus live per engine.** `BrokerPort` gained `register_engine(name, paper)`, `pool_of`, per-engine `free_cash`, and `read_positions`; `BrokerRouter` sends each engine to the live port or to `PaperBroker` (fills at the live price, a cash pool and position book per paper engine). The core takes `register(..., paper=True)`, subtracts margin reservations only within the engine's own pool, and excludes paper engines from reconciliation.
- **Ledger versus the broker's book.** `HestiaCore.bootstrap_ledger()` adopts an overnight position from the book into the live engine that owns the token's instrument (anything unattributed is a critical alert); `reconcile_ledger()` runs every 300 s and compares live engines' ledgers with the book, alert-only and never auto-corrected, skipping tokens with a request in flight and discarding a read that started before a ledger change (a snapshot older than the ledger proves nothing; mutation-checked).
- **Tests.** 33 new adapter and gateway tests, 10 account tests; every guard mutation-checked (placed-id guard, missing-order-as-complete, chunking, reserved lane, stale cash, snapshot race). The core also runs end to end on the Angel adapter against the double, including an order held open past the wait: UNCONFIRMED, never "no fill", a later close acts on the reconciled position. The whole suite is green (331 passed, the 2 expected skips).

**Deliberately left as is.** The core still runs its own order-rate budget (`_Bucket`) in front of the gateway's; the two stack harmlessly, and the core's is the engine-clash policy, so it stays. The gateway's `SessionError` is reserved for the host (slice 3). `read_order` cross-checks against `position()` only indirectly, through the periodic ledger reconciliation.

**Open for slice 3.** The adapter's blocking loops use an injected `sleep` and `clock`; the live wiring passes the real ones and a `ThreadPoolExecutor`, and needs `Scheduler.post` from the real reactor to be thread-safe.
