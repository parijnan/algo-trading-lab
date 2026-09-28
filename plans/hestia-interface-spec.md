# Hestia interface spec v1.1 (P2, amended in P3)

**Status: v1.1, adopted by the user 2026-09-28 (v1 below, plus the six P3 clarifications that follow item 6 of §1). v1 was reviewed and confirmed by the user 2026-09-28 (all seven calls in §10 accepted, with the two additions recorded there).** Phase P2 of `plans/selene-production.md` §10. The types are in code, `hestia_core/interface.py` (immutable dataclasses, enums, two Protocols, no I/O, no clock, no broker import), with 14 tests in `tests/test_hestia_interface.py`; this document is the prose that says what they mean, the flows they carry, and the calls I made that the user should confirm (§10). Nothing here trades. The next phase (P3) builds a fake/replay Hestia against this spec and the tests that go with it.

The split, from the user: **each engine evaluates and decides (entries, exits, stops, the roll) on processed data Hestia delivers; Hestia performs the actual work (orders, fills, retries, reconciliation, data, alerts).** An engine sends **one request per decision** and changes its own state **only on a confirmed outcome**.

## 1. Ground rules

1. **Everything across the boundary is an immutable typed value** (frozen dataclasses). No engine reaches into Hestia's objects and Hestia never reaches into an engine's.
2. **One thread per engine, one queue per engine.** Events arrive on the queue through `ctx.next_event(timeout)`. Nothing calls into an engine from another thread. LTP is **pulled** (`ctx.ltp`), not pushed.
3. **Every call to `next_event` is the engine's heartbeat.** Hestia alerts when it has not been called for 3 minutes (warning) and 5 minutes (critical), plan §1.5.
4. **Time comes only from `ctx.now()` and `ctx.wait()`.** An engine that calls `datetime.now()` or `time.sleep()` cannot be replayed. This is the property that lets the same engine code run against the live Hestia and against recorded days.
5. **No engine code touches** the broker, the feed, the session, signals, the process (`sys.exit`, `os._exit`), or shared files. Hestia owns all of them (P1 inventory §1 and §4).
6. **Interface version 1.1**, `INTERFACE_VERSION` in the module; any change to a type bumps it and updates this document in the same commit.

**P3 clarifications, now part of v1.1 (found while building the fake Hestia; adopted by the user 2026-09-28):** (a) the engine tells Hestia which contract it trades with `ctx.set_trading_contract(contract)` at `SessionStart` and again after a roll; `BarComplete` and `ProvisionalBar` events are delivered for that contract only, and any other contract is read through `latest_bar` / `st_series` after `track`, with Hestia guaranteeing every tracked contract's bar for a boundary is ready before the trading contract's `BarComplete` for that boundary is delivered; (b) `BarComplete` with quality `GAP` carries `bar=None` and `st=None`; (c) a `CloseRequest` or `FlattenRequest` against a contract the engine already holds nothing in finishes `FILLED` with zero lots and detail `already flat`, which keeps a re-sent close harmless. That answer is given only against a settled ledger. If an earlier request of the same engine and contract is UNCONFIRMED, Hestia first reads the broker's position book to settle it (decided by the user, 2026-09-28: reconcile against the broker and act accordingly), records the result as that request's final outcome (`FILLED`, `PARTIAL`, or `REJECTED` with detail `... shows no fill`), and only then works the later request against the reconciled ledger, at its own priority class. A stop-loss flatten therefore waits one book read, not forever, and never acts on a ledger the broker may contradict. Hestia also re-reads the book on its own every 60 s while any request is UNCONFIRMED; (d) `request_status` returns `None` only for a request Hestia has never seen, and an outcome with the new non-final status `IN_FLIGHT` for one still being worked, so an engine resuming after a crash can tell the three cases in the restart protocol apart (the v1 signature could not express in-flight); (e) requests of one engine on one contract are served strictly in submit order, and Hestia micro-batches submits over a short dispatch window (50 ms in the fake) so clashes between engines are resolved by priority class rather than by which thread happened to submit first; (f) entries (`OpenRequest`, roll re-open) may occupy at most `workers - reserved_workers` of the execution workers, so a stop or exit never queues behind a wall of slow entries, the same way the order budget reserves a slice for exits (with one worker in total, that slice is empty and entries and exits share it).

## 2. Registration: the data spec

An engine registers a `DataSpec` and Hestia serves it from session start:

| Field | Prometheus (CRUDEOILM) | Selene (SILVERMIC) |
|---|---|---|
| `instrument` | `CRUDEOILM` | `SILVERMIC` |
| `timeframe_min` | 15 | 15 |
| `st_period`, `st_multiplier` | 10, 2.0 | 10, 2.5 |
| `seed_days` | 18 | 18 |
| `provisional` | enabled (margin guard 0.15%, in the engine, against the previous bar's ST) | enabled (same rule; threshold to calibrate in the DRY_RUN) |
| `watch_dpl` | yes | yes |

The **trading contract** is not in the spec: the engine picks it from `ctx.contracts(instrument)` (a `ContractInfo` per listed contract with token, symbol, expiry, lot size, tick, freeze quantity and holiday-aware `trading_days_left`) and Hestia serves whichever contract the engine trades. Any other contract is a `ctx.track(contract)` request (§5 flow E), answered by `TrackReady` or `TrackFailed`. Stop-loss percentages, targets, sizing formula and roll rule are engine parameters, not part of the data spec.

## 3. Events, Hestia to engine

| Event | When | What the engine does with it |
|---|---|---|
| `SessionStart` | Once per session, after login and seeding: session open (17:00 on an evening-only day), today's rollover time, the contract list with days to expiry, which contracts are seeded | Reconcile with the ledger, choose the trading contract, decide whether a roll is due and `track` the next contract |
| `BarComplete` | At each 15-minute boundary once the bar is built: the bar, its ST point, **`prev_st`**, quality flag, minutes present, and `reconciles_provisional` | Evaluate flips and entries; a `PROVISIONAL` predecessor is reconciled when this arrives |
| `ProvisionalBar` | At a boundary whose candle window was incomplete at that instant: tick-derived bar, provisional ST (with the engine's own period and multiplier), `prev_st` | Apply the margin guard (`|close - prev_st| / close` against the threshold) and, if it holds, send an ordinary request, keeping the pending reconciliation |
| `TrackReady`, `TrackFailed` | Answer to `track` | Continue the roll or abandon it |
| `FeedStale`, `FeedRecovered` | LTP watchdog | Decide whether to keep evaluating stops on a stale price |
| `DplFrozen` | Price pinned at a circuit limit or released | Alert-only in Hestia; the engine may hold entries |
| `CommandEvent` | `EXIT` on this engine's flag | Send a `FlattenRequest(MANUAL_EXIT)` and re-arm to watching |
| `Stop` | `KILL` (this engine or the host), session end, shutdown | Return from `run`; **with `leave_position` the position stays open and untouched** |
| `RequestOutcome` | Final word on a request, or a later update to an `UNCONFIRMED` one | The only thing that changes engine state after a request (§4) |

**Data quality is delivered, not hidden.** `BarQuality` is `COMPLETE`, `RECOVERED`, `PARTIAL`, `PROVISIONAL` or `GAP`. Hestia reports; the engine decides. Prometheus's refusal to act on a gap and its provisional gating stay in the Prometheus engine.

## 4. Requests, acknowledgements and outcomes

**Requests** (`OpenRequest`, `CloseRequest`, `FlipRequest`, `FlattenRequest`), each with an engine-made `request_id` that is **persisted before sending** (§6):
- `OpenRequest(contract, direction, lots, trade_ref, roll_reopen, parent_trade_ref, depends_on)`.
- `CloseRequest(contract, expected_direction, reason, lots=None (all lots), trade_ref, depends_on)`. `expected_direction` is the direction the engine believes it holds; **if the ledger disagrees, Hestia refuses with `REJECTED`** instead of guessing a side.
- `FlipRequest(contract, from_direction, close_lots, open_lots, trade_ref, reason)`: Rule 7, one netted order on one instrument (`close_lots + open_lots` lots), reason typically `TREND_FLIP`.
- `FlattenRequest(contract, reason)`: close everything this engine holds in the contract.
- `depends_on` lets an engine send a roll's reopen together with its close: Hestia sends the second only after the first is `FILLED`, otherwise the second finishes as `DEPENDENCY_FAILED`.

**Acknowledgement.** `submit` returns a `RequestAck` immediately: `ACCEPTED` (queued), `DUPLICATE` (this id was seen before; the existing outcome is available through `request_status`), or `INVALID`. The fill is never returned by `submit`.

**Outcome statuses.** `FILLED` (every lot confirmed); `PARTIAL` (Hestia stopped with fewer lots than requested; only entries end here, see below); `REJECTED` (broker refused after Hestia's retries, or the ledger contradicted the request); `MARGIN_REFUSED`; `LIMIT_REFUSED` (unit cap, roll window, freeze handling, circuit); `DEPENDENCY_FAILED`; `UNCONFIRMED` (an order was placed but its fill could not be confirmed, so the position status is unknown; Hestia keeps reconciling and later sends an updated outcome); `ABANDONED` (the engine was killed before Hestia finished; nothing is cancelled). **Only `FILLED` and `PARTIAL` are `confirmed`**, and an engine changes state only on a confirmed outcome.

**What Hestia guarantees after `ACCEPTED`:**
1. It places the order and does everything the engine used to do in its tick loop: rejection retries, ghost-order recovery, freeze chunking, WebSocket-then-REST fill verification. **The engine never re-sends.**
2. **Exits are completed:** if an exit fills fewer lots than requested, Hestia continues with the remainder, and only ends `UNCONFIRMED` (with a critical alert) if it cannot. (Prometheus today treats any positive fill as done, `get_fill_price_and_qty` returns `filled_lots < requested_lots` for the caller to judge; to be checked in P4.)
3. **Entries are not topped up:** a partial entry ends `PARTIAL` with the lots actually held, and the engine adapts, as `_finalize_new_position` does today.
4. A repeated `request_id` never produces a second order.
5. Service order under contention follows `priority_class`: risk-reducing before risk-adding, first come first served within a class, ties by registry order. Entries reserve margin atomically; a second entry the pool cannot cover gets `MARGIN_REFUSED` at once.
6. Hard limits are enforced regardless of engine bugs: unit cap, one engine per instrument, refusal of any entry into a contract inside its roll window.
7. Every state change reaches the ledger on **confirmed fills only**, and the ledger is reconciled to the broker's position book at startup and periodically.

## 5. Flows

**A. Entry.** `BarComplete` with a flip while flat, guards satisfied → engine persists `PendingRequest`, state `OPENING` → `submit(OpenRequest)` → `RequestOutcome(FILLED)` → state `IN_TRADE` with the real fill, stop level computed from it. Refusal or partial fill: back to `WATCHING`, or `IN_TRADE` with the lots held.

**B. Stop-loss.** Each loop the engine pulls `ctx.ltp`; stop hit (after the first-minute guard) → state `CLOSING`, persists the pending id → `submit(CloseRequest(reason=STOP_LOSS))` → outcome `FILLED` → `WATCHING`. **While the state is `CLOSING` the stop path is suppressed** (this is the request-pending state P1 found necessary, §6). Hestia serves it in the top priority class.

**C. Flip.** `BarComplete` with a flip against the position → `FLIPPING` → `FlipRequest(close_lots=held, open_lots=new)` → one outcome carrying `closed` and `opened` summaries → `IN_TRADE` in the new direction with the new fill.

**D. Provisional.** `ProvisionalBar` arrives at the boundary; the engine computes `|close - prev_st| / close × 100`, and if it exceeds the margin acts as in A or C, recording `provisional_boundary` and the direction. The real `BarComplete` for that boundary arrives with `reconciles_provisional=True`: agreement clears the record; disagreement raises a critical alert through `ctx.alert`, sets the engine's session latch, and takes **no automated reversal**, as today. While a roll or a flip is in progress the engine ignores provisional bars.

**E. Roll on the eve (coincident flip).** At `SessionStart` the engine sees its contract's `trading_days_left` inside the 5-working-day window and tomorrow's contract differs → `ctx.track(next)` → `TrackReady`. While in trade, on its contract's flip it reads the other contract's latest bar and ST (`latest_bar`); coincident and agreeing → `FlipRequest`-equivalent as two requests: `CloseRequest(old, reason=ROLL...)` then `OpenRequest(new, roll_reopen=False, depends_on=close_id)` (a fresh trade, not a linked one, per Prometheus §18), else close and switch to `WATCHING` on the new contract; when done `untrack(old)`.

**F. Roll fallback at the rollover time.** The engine, still in trade at `rollover_time`, does the ST-disagreement veto from `latest_bar(new)`, then `CloseRequest(old, reason=ROLL)` and, on GO with a basis from `price_near(new, entry_ts, 5)`, `OpenRequest(new, roll_reopen=True, parent_trade_ref, depends_on=close_id)` with the stop recalibrated off the basis. Hestia serves the exit in the `ROLL_EXIT` class and the reopen in `ROLL_REOPEN`.

**G. Engine crash and resume (§6).**

**H. Commands.** `EXIT` → `CommandEvent(EXIT)` → engine sends `FlattenRequest(MANUAL_EXIT)`; Hestia clears the flag only after `FILLED` (an unconfirmed liquidation leaves it, and its outcome keeps arriving); engine returns to `WATCHING`, dropping any pending flip. `KILL` → `Stop(KILL, leave_position=True)`, no order, position untouched, other engines unaffected; a host-level `KILL` stops all engines the same way and only then ends the session.

**I. Session.** `SessionStart` first, `Stop(SESSION_END)` at close. A position is expected to stay open across sessions (no end-of-day flatten).

## 6. State and restart

An engine persists an opaque **decision-state string** with `ctx.save_state`: what Prometheus persists today (stop level, targets or none, the recalibration basis, the signal, the last-processed-boundary watermark) plus its **pending request records**. The **ledger, not the engine, is the truth for what is held.**

**The protocol that keeps a restart from double-ordering:**
1. Before `submit`, the engine writes a `PendingRequest` (id, kind, reason, trade ref) into its decision state and saves it.
2. On resume, for every pending record it calls `ctx.request_status(id)`: an outcome exists and is confirmed → apply it and clear the record; the request is known but unresolved → keep waiting for the `RequestOutcome` event (Hestia keeps working it); **unknown → it was never sent, send it again under the same id.**
3. It then reconciles its belief against `ctx.position(contract)`; where they differ **the ledger wins and the engine adopts it** (it never trades to make the ledger match its belief).
4. Everything else is reconstructed as Prometheus's `_setup` does today: the missed-flip scan from the watermark over `st_series`, the missed-roll check from `trading_days_left`, and `ctx.contracts` for the day's facts. Pending flips and roll transitions are not persisted by design, so a restart mid-flip re-derives from the ledger.

## 7. Selene's engine, as a state machine

States: `WATCHING`, `OPENING`, `IN_TRADE`, `CLOSING`, `FLIPPING`, `ROLL_CLOSING`, `ROLL_OPENING`. Every `*ING` state carries the pending request id.

| State | Stops evaluated? | Leaves on |
|---|---|---|
| `WATCHING` | n/a | flip signal and guards → `OPENING`; `CommandEvent(EXIT)` with nothing held → ignore |
| `OPENING` | no | confirmed outcome → `IN_TRADE` (or `WATCHING` if refused or zero lots) |
| `IN_TRADE` | yes, each loop, on `ctx.ltp`, after the first-minute guard | stop → `CLOSING`; flip → `FLIPPING`; roll due → `ROLL_CLOSING` |
| `CLOSING` | **no** (a request is in flight) | confirmed outcome → `WATCHING`; a non-confirmed outcome keeps the state and the engine waits for Hestia's later update |
| `FLIPPING` | no | confirmed outcome → `IN_TRADE` in the new direction (its `closed` half alone means the entry half was refused: → `WATCHING`) |
| `ROLL_CLOSING`, `ROLL_OPENING` | no | as above; `ROLL_OPENING` ends in `IN_TRADE` on the new contract, or `WATCHING` |

Selene has no lots, no targets and one stop, so this is the whole machine. Prometheus's engine has the same skeleton plus its lot1/lot2 bookkeeping and target exits (`CloseRequest(lots=…)` per lot); the ledger tracks net lots, the engine tracks which lot is which.

## 8. What is deliberately not in this spec yet

The ledger schema and trade-log format (`report_trade` is a free-form dict placeholder until P4), the gateway internals, report and Slack formats, the fake/replay Hestia (P3), the registry and flag file layout, and the exact timing of `BarComplete` relative to the boundary instant.

### 8.1 What the fake Hestia does not model (left for P4)

The fake keeps the guarantees and drops the plumbing, so these are the things P4 must add and test against the same contract tests: real Angel One calls, the WebSocket feed and its staleness detection (the fake only injects `FeedStale`), the order-update WebSocket and its reconciliation against the broker's order book (the fake models the outcomes of that reconciliation, not the mechanism), freeze-quantity slicing of large orders, the per-endpoint candle and LTP budgets (only the order budget is modelled), the tick-aggregated provisional bar (the fake takes it from the true bar unless told otherwise), the Slack queue, the state and trade-log files on disk, the flag files that carry EXIT and KILL, and the login and teardown. What the fake does model of reconciliation: the broker's true position after an unconfirmed order is scripted (`BrokerReply(lots=...)`), the fake reads it on demand or every 60 s, and later requests wait for and act on the result. The live Hestia does the same against `getPosition` and the order book.

## 9. Obligations, summarized

**Hestia:** deliver data with quality flags; serve the trading contract and any tracked contract; accept one request per decision and see it through; never place a second order for a repeated id; complete exits; refuse per hard limits; keep the ledger true to confirmed fills and reconcile it to the broker; alert on silence; never end the session for an engine crash; never cancel an in-flight order when an engine stops.
**Engine:** decide; take time and data only from the context; persist a pending record before every submit; change state only on confirmed outcomes; not re-send; not evaluate a stop while its own request is in flight; ignore provisional bars during a roll or flip; reconcile against the ledger at resume and adopt it.

## 10. Calls I made that need the user's confirmation

1. **`CloseRequest.expected_direction`** with a `REJECTED` on ledger mismatch, instead of Hestia deriving the side silently. It makes a sign mistake loud, at the cost of one more field.
2. **`depends_on` chaining** for roll legs, so an engine can send close and reopen together and Hestia sequences them, instead of the engine waiting for the close outcome first. The alternative is simpler for the engine and adds a round trip to the roll.
3. **Exits are completed by Hestia** even if the broker fills them in pieces; **entries are not topped up**.
4. **LTP is pulled**, with the tick's age, and the stop loop runs at the engine's own cadence (about 0.5 seconds in trade, plan §1.5) through `next_event(timeout=0.5)`. Hestia does not push ticks.
5. **First-minute and minimum-entry guards stay in the engine**, using `ctx.session_open()`, since they are strategy policy that Selene shares with Prometheus.
6. **The margin guard's arithmetic is the engine's**, using `prev_st` delivered on both bar events; Hestia does not decide whether a provisional bar is safe to act on.
7. **Trade records use Prometheus's format** (user: "use same format as Prometheus"): `report_trade` takes a dict whose keys are the fixed `TRADE_RECORD_COLUMNS` (26 columns, identical to `TRADE_LOG_COLUMNS` in `prometheus_functions.py`, pinned by a test), reindexed on every write so the file's shape cannot depend on a trade's incidental keys (the 2026-09-08 ragged-file bug). Selene fills the lot1 columns and leaves lot2 and target columns empty; a rolled trade's second leg carries `parent_trade_id` and the `-rollover` direction suffix, as today. The per-minute running rows keep Prometheus's running-row format.

**Confirmed by the user, with additions:** items 1 to 5 as stated. **Item 6** is accepted **as long as both sizing modes are retained**: `SizingConfig` carries `dynamic` and `static_units` (live-read, per engine) plus the `unit_cap` Hestia enforces; the engine applies its own rule for each mode (static: `static_units`; dynamic: Prometheus's `max(1, capital // margin_per_unit)` over the engine's capital), and Selene defaults to static. With several engines on one margin pool, dynamic mode sizes against an explicit `allocation_rs` per engine (added to `SizingConfig`), not the whole account's available cash. **Item 7** as above.
