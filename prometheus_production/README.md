# Prometheus Production — MCX Crude Oil Intraday Trend-Following (Standalone)

Live execution module for the Prometheus strategy.
Part of the **Algo Trading Lab** project.

Prometheus watches for ST_15 (single-timeframe, 15-min Supertrend) flips on CRUDEOILM futures
and auto-enters a 2-lot scale-out position on signal. Unlike Artemis/Athena/Apollo, and like
Iris, Prometheus owns its own Angel One session — it is not launched by Leto (different
exchange, different underlying, no VIX coupling; see
[`plans/prometheus-phase2-production.md`](../plans/prometheus-phase2-production.md) §0).

**Status: Phase 3 build complete, `DRY_RUN=True` (paper mode), live-testing on Delos since
2026-09-04.**
[`plans/prometheus-phase3-production.md`](../plans/prometheus-phase3-production.md) is the
current design doc — every section there is `[DECIDED]`. Built: resilient order execution (§1,
2026-09-04), private intraday cache + write-removal + startup retry (§15), no-EOD-flatten (§2),
the `state.token` invariant fix (§3), the rollover trigger/recovery/execution timeline including
the ST-disagreement veto (§4/§5/§6, 2026-09-04), Rule 7's combined order with a stuck-partial-fill
retry marker (§7, 2026-09-04), the historical-basis SL/target recalibration method (§8,
2026-09-04), the two-linked-rows rolled-trade log schema (§9, 2026-09-04), the 15-min-boundary
deferred-bar fix + the resample day-end-boundary fix that also underlies it (§12/§17), the
provisional-boundary computation on top of that (§12a, 2026-09-04 — see below), opening-bar
price-artifact correction (§11), realised/unrealised/total P&L reporting (§13), the ST seed
skip-list (§14), and the 1h/15m entry filter's full wiring (§17, 2026-09-04). **The 1h filter is
built and unit-tested but gated off** — `ENTRY_FILTER_1H_ALIGN_ENABLED = False` in
`prometheus_configs.py`, so it currently has no effect on any entry path; `ST_1H_PERIOD` /
`ST_1H_MULTIPLIER` were unset placeholders, and Phase 4's full backtest (2026-09-04,
`prometheus_backtest/phase4/`) then found no (period, multiplier) combination that beats the
unfiltered baseline — **shelved**, not just deferred; see
[`prometheus_backtest/README.md`](../prometheus_backtest/README.md)'s Phase 4 section. The
rollover mechanics are code-complete and unit-verified (real on-disk historical-basis/ST lookups,
mocked-broker Rule 7 reconciliation and rollover reopens) but not yet exercised by an actual live
rollover (~2026-09-15).

**Live-test day 1 (2026-09-04) findings, all fixed same day:** a tz-parsing crash on the very
first live run (`fetch_one_minute_window` returned tz-aware timestamps from a `%z`-suffixed
broker format, colliding with the rest of the codebase's tz-naive convention — fixed at the
source, commit `9c46fc6`); the mult-2.0 vs mult-2.5 decision made and deployed (`ST_MULTIPLIER`,
`SL_PCT`, `TARGET1_PCT`, `TARGET2_FLAT_PCT` all changed together, since the two candidates are
jointly calibrated, not interchangeable piecemeal — commit `d5872a7`); `_recover_missed_rollover`
found missing the §17 1h-alignment gate that `_execute_rollover_decision` already had (commit
`0adb731`); `INNER_RETRY_ATTEMPTS` raised 3→5 after observing frequent AB1021 bursts (commit
`6ef55b6`); the Slack control panel reordered to put Prometheus's section above the shared
Leto/Athena/Artemis/Iris block (commit `acd3a26`).

---

## Module Structure

| File | Purpose |
|---|---|
| `prometheus.py` | Entry point and main class — `main()` to start, `Prometheus.run()` for the loop |
| `prometheus_configs.py` | All tuneable parameters — deliberate duplicate of `prometheus_backtest/phase2/configs_p2.py`'s calibrated values, not an import (matches `iris_configs.py`'s convention: production must never silently follow future backtest experimentation) |
| `prometheus_state.py` | `PrometheusState` dataclass + CSV-backed `save_state`/`load_state` |
| `prometheus_functions.py` | `SupertrendIndicator`, effective-contract resolution, MCX holiday calendar, resilient 1-min poller, order placement, `OrderFillWatcher`, fill verification, trade-log/counter persistence, guardian check |
| `prometheus_logger_setup.py` | Rotating file logger (`logs/prometheus_YYYYMMDD.log`) |

---

## Execution Flow

Five diagrams: startup/setup, the main loop (exits + 15m bar handling), Rule 7's combined-order
mechanism in detail, the evening-triggered rollover timeline, and missed-rollover recovery at
startup. Split from a single chart because Phase 3's rollover mechanics and Rule 7's combined
order make one diagram unreadable — each is referenced from where it's called.

Every gate touched by the §17 1h-alignment filter is labeled `inert unless
ENTRY_FILTER_1H_ALIGN_ENABLED` — as of this writing that flag is `False` in
`prometheus_configs.py`, so every one of those decision nodes always resolves to its "agrees"
branch. Read them as dormant, not active.

### Startup & Setup

```mermaid
graph TD
    Start([python prometheus_production/prometheus.py]) --> Holiday{MCX fully closed today?\nmcx_holidays.csv}
    Holiday -- Yes --> AbortHoliday([Exit — not starting])
    Holiday -- No --> Disable{prometheus_command.flag\n== DISABLE?}
    Disable -- Yes --> AbortDisable([Exit — disabled via Slack])
    Disable -- No --> Guardian{Guardian check:\nno other active strategy session?}
    Guardian -- Fail --> AbortGuardian([Exit — exclusive session required])
    Guardian -- Pass --> KillOld[SIGTERM old PID if exists]
    KillOld --> Login[Angel One login]
    Login --> Contract[resolve_effective_contract:\nexchange front-month, UNLESS\n< 5 trading days to its expiry\n-- then roll to next contract out.\nAlso reads freeze_qty live off\nthe instrument master, §1]

    Contract --> SeedRetry{seed_st15 attempt\nfailed?}
    SeedRetry -- Yes, attempts left --> SeedWait[Wait SEED_RETRY_INTERVAL_SEC\nlog + one WARNING Slack heads-up\non the first failure only, §15] --> SeedTry
    SeedRetry -- Yes, exhausted --> AbortSeed([Exit -- seed failed after\nSEED_RETRY_ATTEMPTS, CRITICAL alert])
    SeedTry[seed_st15:\npast days from the shared pipeline\nfile; today from the private cache\n+ live gap-fetch for the remainder,\n§15 -- resample, gap-check, compute ST]
    SeedTry --> SeedRetry
    SeedRetry -- No --> TodayAccum[Re-read the private cache\ninto self._df_1m_today]
    TodayAccum --> OpenBarCheck[_maybe_check_opening_bar:\nif today's 09:00 row already\npresent -- a restart -- check it now\nagainst CRUDEOIL, §11]
    OpenBarCheck --> Feed[Start SharedFeed WebSocket\nsubscribe MCX contract token\npermanent for the session.\nAlso subscribe state.token if it\ndiffers -- a missed-roll resume, §3]
    Feed --> OrderWS[Start OrderFillWatcher\nlive only -- skipped in DRY_RUN]
    OrderWS --> Resume{Resuming with\nstatus == in_trade?}
    Resume -- Yes --> ResumeWarn[Rebuild pending_trade_row from state\nSlack: NOT reconciled against broker --\nverify manually]
    Resume -- No --> Watching[status = watching]
    ResumeWarn --> SaveState[save_state]
    Watching --> SaveState

    SaveState --> MissedRoll{§5: state.token !=\nresolved contract's token?\nmissed rollover overnight}
    MissedRoll -- Yes --> MissedRollExec[_recover_missed_rollover --\nsee Missed-Rollover Recovery diagram]
    MissedRoll -- No --> RollTonight
    MissedRollExec --> RollTonight{§4: tomorrow's contract\ndiffers from today's?}
    RollTonight -- Yes --> RollTonightSetup[Confirm roll tonight at ROLLOVER_TIME\nprecompute recalibration basis, §8\nSlack heads-up -- see Rollover\nTimeline diagram]
    RollTonight -- No --> SetupDone
    RollTonightSetup --> SetupDone([Setup complete -- watchdog armed\n-- proceed to Main Loop])
```

### Main Loop

```mermaid
graph TD
    LoopStart([Main loop armed]) --> Loop{not shutdown AND\nflag exists AND\nnow < SESSION_END_TIME?}
    Loop -- No --> Teardown[_teardown: stop feed,\nclear_today_cache -- §2 Phase 3:\nan open position is LEFT OPEN,\nthe normal case now, not force-exited]
    Teardown --> End([Session terminated])

    Loop -- Yes --> CmdFlag{prometheus_command.flag\n== EXIT or KILL?}
    CmdFlag -- EXIT --> ClearPending1[Drop any pending Rule 7 flip, §7] --> ForceExitAll[_execute_exit_all: slack_exit] --> RaiseStop[Raise -- session terminates]
    CmdFlag -- KILL --> SetKillFlag[_kill_no_exit = True\nposition left untouched] --> RaiseStop
    RaiseStop --> Teardown

    CmdFlag -- No --> PendingFlip{Rule 7 pending flip\nin progress, §7?}
    PendingFlip -- Yes --> RetryFlip[_retry_pending_flip --\nsee Rule 7 detail diagram]
    PendingFlip -- No --> InTrade
    RetryFlip --> InTrade{status == in_trade?}

    InTrade -- Yes --> ExitCheck[_check_exit_conditions_ltp\nevery 0.5-1s tick]
    ExitCheck --> UpdateCadence{TRADE_UPDATE_SEC\nelapsed?}
    UpdateCadence -- Yes --> SlackUpdate[Slack unrealised P&L update]
    UpdateCadence -- No --> BoundaryCheck
    SlackUpdate --> BoundaryCheck

    InTrade -- No --> BoundaryCheck{1-min boundary\nreached?}
    BoundaryCheck -- No --> Sleep[sleep 0.5s in_trade\n1.0s watching] --> Loop
    BoundaryCheck -- Yes --> Recover[Recover any pending windows\nfrom outer retry queue]
    Recover --> RollTiming[_check_rollover_timing, §6 --\nsee Rollover Timeline diagram]
    RollTiming --> Fetch[fetch_one_minute_window\n5-min lookback, inner retry x5]
    Fetch -- Fail --> QueuePending[Push to pending_recovery\nnon-blocking, retried next boundary] --> Merge15Check
    Fetch -- OK --> MergeWrite[_merge_1m: write into the PRIVATE\nintraday cache, §15 -- never the\nshared file any more -- + update\nin-memory today accumulator, then\n_maybe_check_opening_bar §11]
    MergeWrite --> Merge15Check{boundary.minute\n% 15 == 0 AND no\npending 15m boundary?}
    Merge15Check -- Yes --> SetPending[Mark this boundary pending,\ndeadline = boundary + DEFERRED_BAR_CUTOFF_MIN]
    Merge15Check -- No --> PendingCheck{15m boundary\npending?}
    SetPending --> PendingCheck
    PendingCheck -- No --> RunningRowCheck
    PendingCheck -- Yes --> WindowComplete{Window has\nall 15 min? OR\npast cutoff?}
    WindowComplete -- No --> RunningRowCheck
    WindowComplete -- Yes, past cutoff\nbut incomplete --> LoudWarn[WARNING log + Slack:\nbuilding from what's on hand\n§12, cutoff=1min] --> Handle15m
    WindowComplete -- Yes, complete --> Handle15m[_handle_new_15m_bar]
    Handle15m --> RunningRowCheck{status == in_trade?}
    RunningRowCheck -- Yes --> AppendRow[Append running-P&L row\ntrade_logs/trade_NNNN_*.csv]
    RunningRowCheck -- No --> Sleep
    AppendRow --> Sleep

    ExitCheck --> ExitSL{SL hit on LTP?}
    ExitSL -- Yes --> ExitAllSL[_execute_exit_all: stop_loss\nSL wins any same-tick tie vs a target] --> Sleep
    ExitSL -- No --> ExitT1{lot1 open AND\nlot1_target hit?}
    ExitT1 -- Yes --> ExitLot1[_execute_exit_lot 1: target1]
    ExitT1 -- No --> ExitT2
    ExitLot1 --> ExitT2{lot2 open AND\nlot2_target hit?}
    ExitT2 -- Yes --> ExitLot2[_execute_exit_lot 2: target2_source]
    ExitT2 -- No --> BothClosedCheck
    ExitLot2 --> BothClosedCheck{both lots\nnon-open?}
    BothClosedCheck -- Yes --> Finalize[_finalize_trade:\nappend to prometheus_trades.csv\nSlack trade-closed summary\nstate reset to watching]
    BothClosedCheck -- No --> Sleep
    Finalize --> Sleep

    Handle15m --> Build15m[Build 15m OHLCV bar from\nthis window's 1-min data\nalert loudly if < 8/15 bars present]
    Build15m --> RecomputeST[compute_st over full series\npersist_15m_series -- debug visibility only]
    RecomputeST --> FlipCheck{trend_flip == True?}
    FlipCheck -- No --> RunningRowCheck
    FlipCheck -- Yes --> InTradeFlip{status == in_trade?}
    InTradeFlip -- Yes --> DirCheck{flip direction !=\ncurrent position direction?}
    DirCheck -- No --> RunningRowCheck
    DirCheck -- Yes --> Rule7[_execute_rule7_flip, §7 --\nsee Rule 7 detail diagram]
    Rule7 --> RunningRowCheck

    InTradeFlip -- No --> FreshGate{status == watching AND\npast MIN_ENTRY_BUFFER_MIN AND\nnot rollover-suppressed AND\n1h filter agrees, §17\ninert unless\nENTRY_FILTER_1H_ALIGN_ENABLED?}
    FreshGate -- Yes --> Entry[_execute_entry:\nnew direction, same 15m bar]
    FreshGate -- No --> RunningRowCheck

    Entry --> Units[_calculate_units + margin check]
    Units --> PlaceOrder[place_order §1: 2 x units x LOTS_PER_LEG\nlots, chunked to freeze_qty, rejection\nretry, ghost-order recovery on\nDataException/NetworkException --\nreturns a LIST of order IDs]
    PlaceOrder -- Fail --> EntryAbort[Log error -- no position opened] --> RunningRowCheck
    PlaceOrder -- OK --> FillVerify[get_fill_price_and_qty §1:\nWS fast path + REST fallback,\naggregated across the whole\norder-ID list]
    FillVerify -- Fail --> EntryAbort
    FillVerify -- OK --> SplitLots[Split filled lots -> lot1/lot2\npartial-fill aware: lot2 may\nnever open, never retried]
    SplitLots --> Thresholds[resolve_thresholds + resolve_target2:\nsl_price, lot1_target, lot2_target]
    Thresholds --> NewState[New PrometheusState: in_trade\nsave_state, Slack entry message]
    NewState --> RunningRowCheck
```

### Rule 7: Combined-Order Detail (§7, 2026-09-04)

A trend-flip against an open position no longer fires two or three separate orders (old
Phase 2 shape: exit, confirm, then a separate entry). One order for the net quantity
(`old_open_lots + new_trade_lots`) — partial fills reconciled explicitly, retried every main
loop tick until fully resolved, independent of whether a fresh 15m flip is still "current."

```mermaid
graph TD
    R7Start([_execute_rule7_flip\ncalled from Handle15m on a\nflip against an open position]) --> OldLots[old_open_lots = lot1_lots if open\n+ lot2_lots if open]
    OldLots --> ReentryGate{now_after_min_entry AND\nnot rollover-suppressed AND\n1h filter agrees, §17\ninert unless\nENTRY_FILTER_1H_ALIGN_ENABLED?}
    ReentryGate -- No --> NoReentry[reentry_allowed = False\nnew_trade_lots = 0 --\nexit half still always happens]
    ReentryGate -- Yes --> UnitsCalc[_calculate_units\nnew_trade_lots = units x LOTS_PER_LEG x 2]
    UnitsCalc --> MarginCheck{Margin sufficient\nfor the new leg?}
    MarginCheck -- No --> ZeroNew[new_trade_lots = 0\nstill closes the old leg below]
    MarginCheck -- Yes --> HaveTarget[new_trade_lots_target set]

    NoReentry --> OldZero1{old_open_lots == 0?}
    ZeroNew --> PendingSet
    HaveTarget --> PendingSet
    OldZero1 -- Yes, reentry allowed --> PlainEntry([Plain _execute_entry --\nnothing to combine])
    OldZero1 -- Yes, no reentry --> NoOp([Nothing to do -- return])

    OldZero1 -- No --> PendingSet[Set self._pending_flip:\ndirection, signal_ts/close, units,\nnew_trade_lots_target, opened_lots=0]
    PendingSet --> Retry[_retry_pending_flip]

    Retry --> Requested[requested = old_open_lots\n+ max 0, target - opened_lots]
    Requested --> PlaceCombined[place_order: ONE order,\nnet quantity = requested]
    PlaceCombined -- Fail --> AlertStuck1[_alert_pending_flip_stuck:\nCRITICAL, then debounced re-alert\nevery PENDING_FLIP_REALERT_DEBOUNCE_SEC] --> WaitNextTick
    PlaceCombined -- OK --> FillCombined[get_fill_price_and_qty]
    FillCombined -- Unconfirmed --> AlertStuck1
    FillCombined -- OK --> Reconcile[closed_qty = min filled, old_open_lots\nopened_qty = filled - closed_qty]

    Reconcile --> CloseLot2{closed_qty > 0 AND\nlot2 open?}
    CloseLot2 -- Yes --> ApplyLot2[_apply_confirmed_lot_exit 2\ntrend_flip -- lot2-before-lot1\ntie-break, DECIDED]
    CloseLot2 -- No --> CloseLot1
    ApplyLot2 --> CloseLot1{remaining closed_qty > 0\nAND lot1 open?}
    CloseLot1 -- Yes --> ApplyLot1[_apply_confirmed_lot_exit 1\ntrend_flip]
    CloseLot1 -- No --> AccumNew
    ApplyLot1 --> AccumNew{opened_qty > 0?}
    AccumNew -- Yes --> Accum[Accumulate opened_lots +\nprice-weighted sum for a\ncorrect blended entry price]
    AccumNew -- No --> CheckDone
    Accum --> CheckDone{old lots fully closed\nAND new lots fully opened?}
    CheckDone -- Yes --> Finalize7[_finalize_new_position\nusing the blended avg price\npending_flip cleared]
    CheckDone -- No --> WaitNextTick([Retried next main-loop tick\nregardless of status, §7 --\nnot gated on a fresh trend_flip])
```

### Rollover Timeline (§4/§6/§8/§9, evening-triggered)

```mermaid
graph TD
    Evening([Every _setup, after\nresolving today's contract]) --> CheckTonight{§4: tomorrow's trading day\nresolves to a different\ncontract than today's?}
    CheckTonight -- No --> NoRoll([No rollover due tonight])
    CheckTonight -- Yes --> Basis[_precompute_rollover_basis, §8:\nhistorical_basis_price lookup for\nthe new contract at the old\nentry's timestamp -- pure historical,\nno live fetch. No-op if flat.]
    Basis --> SlackHeads[Slack heads-up: rolling\ntonight at ROLLOVER_TIME]
    SlackHeads --> Armed([_rollover_new_contract set --\nchecked every 1-min tick by\n_check_rollover_timing])

    Armed --> Prefetch{now >=\nROLLOVER_PREFETCH_TIME?}
    Prefetch -- Yes, not done yet --> DoPrefetch[_do_rollover_prefetch:\nbulk-fetch new contract's today\nseries, subscribe its WS feed]
    Prefetch -- Done already --> TopUp{now <\nROLLOVER_TIME?}
    DoPrefetch --> TopUp
    TopUp -- Yes --> DoTopUp[_do_rollover_topup_poll:\nsame 5-min lookback poll as the\nold contract, keeps the new\ncontract's series current]
    DoTopUp --> RolloverTimeCheck
    TopUp -- No --> RolloverTimeCheck{now >= ROLLOVER_TIME?}

    RolloverTimeCheck -- No --> Armed
    RolloverTimeCheck -- Yes --> Decision[_execute_rollover_decision]

    Decision --> InTradeCheck{status == in_trade?}
    InTradeCheck -- No --> FlatGo[go = True -- pure housekeeping,\nnothing to veto]
    InTradeCheck -- Yes --> RefreshBasis[Refresh the basis precompute\nright before use -- not stale,\neven if position changed today]
    RefreshBasis --> NewST{compute_st_for_contract\non the NEW contract, 15m:\nST unavailable / NaN?}
    NewST -- Yes --> NoGoST[go = False\nSlack: ST unavailable, no-go]
    NewST -- No --> STAgree{new_direction ==\ncarried position's direction?}
    STAgree -- No --> NoGoDisagree[st_agree = False\nSlack: 15m ST disagrees, no-go]
    STAgree -- Yes --> AlignCheck{§17: _check_1h_alignment\non the NEW contract\ninert unless\nENTRY_FILTER_1H_ALIGN_ENABLED?}
    AlignCheck -- No --> NoGoAlign[align_agree = False\nSlack: 1h ST disagrees, no-go]
    AlignCheck -- Yes --> BothAgree[go = st_agree AND align_agree\n= True]

    FlatGo --> FlattenStep
    NoGoST --> FlattenStep
    NoGoDisagree --> FlattenStep
    NoGoAlign --> FlattenStep
    BothAgree --> FlattenStep[Step 5: flatten the OLD position\nunconditionally, regardless of\ngo/no-go, via _execute_exit_all]

    FlattenStep --> ExitConfirmed{Exit confirmed\nfully closed?}
    ExitConfirmed -- No --> RetryNextTick([Return -- retried\nnext tick, like any other\nunconfirmed exit])
    ExitConfirmed -- Yes --> Unsub[Unsubscribe old contract's WS\nSwap self._contract to the new one]
    Unsub --> ReopenGate{go == True AND\nbasis available?}
    ReopenGate -- No --> ClearState([Flatten-only outcome --\nclear rollover state, done])
    ReopenGate -- Yes --> Reopen[_execute_rollover_reopen:\nplace_order sized to\nlots_to_reopen, get_fill_price_and_qty,\n_finalize_new_position with\nparent_trade_id + basis_price\nkept separate from the real fill, §8/§9]
    Reopen --> ClearState

    ClearState --> Executed([_rollover_executed_today = True\n_df_1m_today = _df_1m_today_new])
```

### Missed-Rollover Recovery (§5, at startup)

Same veto shape as the evening timeline above, triggered instead when the process wasn't alive
at `ROLLOVER_TIME` (crash, MCX holiday, KILL, DISABLE) and resumes to find `state.token` no
longer matches the freshly-resolved effective contract. By this point in `_setup()`,
`self._contract` and `self._df_15m` are already the new contract's — no separate prefetch/poll
needed, `_setup()`'s own normal flow already did that work.

```mermaid
graph TD
    SetupMissed([_setup, after WS feed\nis subscribed to state.token]) --> MissedCheck{status == in_trade AND\nstate.token != resolved\ncontract's token?}
    MissedCheck -- No --> NoMissed([Normal case -- no missed roll])
    MissedCheck -- Yes --> MissedBasis[historical_basis_price for the\nNEW contract -- already resolved\nas self._contract by this point]
    MissedBasis --> MissedST{self._df_15m --\nalready seeded for the new\ncontract -- trend NaN?}
    MissedST -- Yes --> MissedNoGoST[go = False -- ST still warming up]
    MissedST -- No --> MissedSTAgree{new_direction ==\ncarried position's direction?}
    MissedSTAgree -- No --> MissedNoGoDisagree[st_agree = False]
    MissedSTAgree -- Yes --> MissedAlign{§17: _check_1h_alignment,\nself._contract/self._df_1m_today\n-- already the new one's\ninert unless\nENTRY_FILTER_1H_ALIGN_ENABLED?}
    MissedAlign -- No --> MissedNoGoAlign[align_agree = False]
    MissedAlign -- Yes --> MissedGo[go = st_agree AND align_agree]

    MissedNoGoST --> MissedFlatten
    MissedNoGoDisagree --> MissedFlatten
    MissedNoGoAlign --> MissedFlatten
    MissedGo --> MissedFlatten[_execute_exit_all: rollover\nunconditional, same as the\nevening path's step 5]
    MissedFlatten --> MissedConfirmed{Exit confirmed?}
    MissedConfirmed -- No --> MissedRetry([Leave state as in_trade --\nretried on next restart])
    MissedConfirmed -- Yes --> MissedUnsub[Unsubscribe old token's WS]
    MissedUnsub --> MissedReopenGate{go AND basis\navailable?}
    MissedReopenGate -- Yes --> MissedReopen[_execute_rollover_reopen\non self._contract -- same\nfunction the evening path uses]
    MissedReopenGate -- No --> MissedDone
    MissedReopen --> MissedDone([save_state -- setup continues\nto §4's evening check next])
```

---

## Signal: ST_15

- **Timeframe**: single 15-min Supertrend (`ST_PERIOD=10`, `ST_MULTIPLIER=2.0`) — no regime gate,
  unlike Iris's dual-timeframe design. `ST_MULTIPLIER` was `3.0` (Phase 2's value, inherited from
  Iris, never itself calibrated for crude) through Phase 3's first live-test start on 2026-09-04;
  after confirming that value's live ST matched the chart correctly, changed to `2.0` the same
  day — Phase 3 was designed for a 2.0-vs-2.5 multiplier, and the user chose 2.0.
  `prometheus_backtest/phase2/backtest_p2.py`'s own calibration used `3.0`; `2.0` is untested by
  that backtest.
- **Seed at startup**: `seed_st15()` combines two sources (§15) — past calendar days from the
  *shared* MCX data pipeline file (`data_pipeline/data_downloader_mcx.py`, never written by
  Prometheus), and *today* from Prometheus's own private intraday cache plus a live gap-fetch for
  whatever the cache doesn't already cover. Resampled to 15-min, gap-checked, ST computed from
  scratch — refuses to seed across a hole rather than silently computing over one. Wrapped in a
  bounded retry (`SEED_RETRY_ATTEMPTS=5`, `SEED_RETRY_INTERVAL_SEC=120`) — see Private Intraday
  Cache below for why this is safe to block on here specifically.
- **Opening-bar artifact correction** (§11): CRUDEOILM's very first 1-min candle of the session
  has shown a recurring thin-liquidity price-discovery artifact (7 confirmed instances,
  2026-03 through 2026-09) that distorts ST's ATR for ~`ST_PERIOD` bars afterward.
  `_maybe_check_opening_bar()` runs once per session, the first time today's 09:00 row is
  available, and compares it against a live poll of CRUDEOIL's own 09:00 print (confirmed
  reliable at that exact minute every time). Gated by `OPENING_BAR_CORRECTION_ENABLED`
  (**default `False`**) — the check always runs and logs what it would have done, but only
  patches `self._df_1m_today` in place (before any resample reads it) when the toggle is on.
  Left off by default so ST accuracy can be validated against the raw, uncorrected broker chart
  first — not a temporary flag with a removal date, a validation gate.
- **Live update**: every 1-min boundary, the last 5 minutes are polled and merged into the
  in-memory today-accumulator (and the private cache, §15); on each 15-min boundary
  (`minute % 15 == 0`), a fresh 15m bar is built from that window — **but not necessarily
  immediately** (§12, see below) — appended to the full series, and ST is recomputed over the
  whole thing (`compute_st`, not incremental — matches the backtest's own computation exactly).
  Persisted to `data/prometheus_15m_series.csv` after every bar — for inspection/restart-recovery
  visibility only, never resumed from directly on restart (ST always recomputes fresh from the
  seeded + accumulated 1-min history).
- **Deferred-bar computation** (§12): a 15-min boundary tick doesn't compute the bar the instant
  it arrives — it waits (re-checking every 1-min cycle) until `self._df_1m_today` genuinely has
  all 15 minutes for that window, up to `DEFERRED_BAR_CUTOFF_MIN=1` minute, before falling back to
  building it from whatever's on hand with a loud warning. Confirmed live 2026-09-03: an AB1021
  stretch can span ~60s, and building a bar from a merely-short tail (14/15 present) previously
  triggered no warning at all — the old `< 8/15` check only caught a much worse case. SL/target
  monitoring is completely unaffected by the wait either way (`_check_exit_conditions_ltp` runs
  every tick regardless of whether a 15m bar is pending). Also fixed the underlying resample
  function's day-end boundary handling in the same pass (§17) — see that section below.
- **Missing-bar handling**: if the window has zero 1-min bars even after the deferred-bar cutoff,
  the 15m bar is skipped outright (a gap in the ST series, alerted loudly); if it has fewer than
  8 of the expected 15, the bar is still built from what's available, also alerted — "no silent
  staleness."

---

## Entry / Exit Priority

Checked in this order on every loop tick while `status == in_trade` (`_check_exit_conditions_ltp`):

| Priority | Trigger | Notes |
|---|---|---|
| 1 | Stop loss | Single shared level protecting whichever lot(s) remain open; wins any same-tick tie against a target (`backtest_p2.py` convention) |
| 2 | Lot 1 target | `TARGET1_PCT=1.0%` of entry |
| 3 | Lot 2 target | `TARGET2_MODE='flat_pct'`, `TARGET2_FLAT_PCT=2.3%` of entry |
| — | Trend flip | Checked separately, only on a 15-min boundary — closes whichever lot(s) remain open AND is itself the entry for the opposite direction, resolved at the same bar ("rule 7") |

**§2, Phase 3: no EOD square-off tier any more.** `configs_p3.py` was never calibrated with one —
a position is expected to carry across sessions (and, once §4–§9 are built, a contract roll), not
force-flattened at close.

Entries (fresh or rule-7 re-entry) require the session to have genuinely been open for at least
`MIN_ENTRY_BUFFER_MIN` (15) minutes — **no cutoff before close any more** (§2:
`LAST_ENTRY_TIME`/`MAX_ENTRY_BEFORE_CLOSE_MIN` are gone, not renamed, matching `configs_p3.py`).
Fixed 2026-09-04: this used to be a hardcoded `MIN_ENTRY_TIME='09:15'` clock-time check, which
silently gave zero minutes of protection on the evening-only special sessions (real open 17:00,
already past 09:15 on the clock) — now keyed off the actual first 1-min bar of today's session
(`_past_min_entry_guard`), same fix as the first-minute exit guard below.

**Rule 7's re-entry is gated on confirmed exit.** `_execute_exit_all` returns `True` only if the
position ended up genuinely flat; a same-bar opposite-direction entry only fires if that's
`True`. Firing a fresh entry while the exit itself failed to confirm would mean attempting to
hold both directions at once against a position with an unknown real state — the exact class of
bug behind the 2026-08-31 incident (see Status).

---

## Fill-Confirmation Invariant (hard rule, added after a real incident)

`_execute_exit_lot` returns `True` **only** if the exit is genuinely confirmed: a real order ID,
a real fill, filled quantity > 0. On any failure, that lot's status is left untouched (still
`open`) — no fabricated fill price, no P&L, no Slack success message. The next tick's exit-check
(every 0.5–1s) retries automatically; no separate retry loop is needed, and no lie enters the
state file or trade log.

This exists because of a real 2026-08-31 incident: a trend-flip exit order failed at the broker
(`orderid=None`) with no guard against it, and the prior code fabricated a fill from LTP anyway
— both lots marked closed internally while the real 2-lot long stayed open and unmonitored at
the broker for ~28 minutes until caught manually. Fixed via a confirmation check that this
invariant and rule 7's re-entry gate both share. `DRY_RUN` was reverted to `True` the same day
and has not been flipped back — see Status. (`_teardown()`'s own exit-confirmation check from
that fix is gone now, not because the invariant weakened, but because §2's no-EOD-flatten change
means teardown no longer attempts an exit at all in its normal path — see Process Lifecycle.)

---

## Instrument & Contract Roll

| | |
|---|---|
| Instrument | `CRUDEOILM` (default) or `CRUDEOIL` — Slack-switchable (`btn_prometheus_instrument`), writes `data/instrument_override.json` (`symbol` + `margin_per_unit` together, since the two are coupled — different lot sizes, 10 vs 100 barrels) |
| Lot size / freeze qty | Both looked up live from `data_pipeline/data/mcx_instrument_master.csv`, never hardcoded (§1) |
| Roll rule | `resolve_effective_contract()`: the exchange's own front month, **unless** fewer than `TENDER_ROLL_TRADING_DAYS=5` trading days (via `mcx_holidays.csv`) remain until its expiry — then the *next* contract out |
| Why roll early | Capital efficiency over strict backtest parity — avoids MCX's elevated tender-margin window on energy contracts in a departing contract's final days |
| ST computation | Per-contract only, never spliced across a roll — a raw price-level jump at the roll boundary would otherwise risk a spurious flip driven by nothing but switching instruments |

**Phase 3 (built 2026-09-04): a position can now genuinely span the roll**, not just the contract
housekeeping above — see Contract Rollover Mechanics below for the full timeline.

---

## Contract Rollover Mechanics (§3–§9, built 2026-09-04)

Phase 2 never had to solve this: positions were always flat by end of day, so whatever
`resolve_effective_contract()` returned each morning was trivially correct. Phase 3 positions can
run for days to weeks, so the day the effective contract flips can land in the middle of an open
trade — nothing above (which only governs *housekeeping* — which contract file/token Prometheus
tracks) says what happens to a *position* caught mid-roll. This section does.

**`state.token` invariant (§3).** While `status == 'in_trade'`, every price read and every order
keys off `state.token`/`state.symbol` — never `self._contract`, which is consulted only while
`watching`/entering and to detect an upcoming roll. `_get_ltp()`'s WebSocket branch and `_setup()`'s
subscription set (which now also subscribes `state.token`'s feed on a resume, if it differs from
the freshly-resolved contract) both follow this.

**Evening trigger (§4).** Checked once, at `_setup()`: does *tomorrow's* trading day (walked
forward via `mcx_holidays.csv`, not a naive `today+1`) resolve to a different contract than
today's? If so, the roll is confirmed for tonight, and:
- The recalibrated basis price is precomputed immediately (§8 below) — pure historical lookup, no
  reason to wait.
- Fresh and Rule-7 entries are suppressed once the clock reaches `ROLLOVER_TIME` — otherwise a
  fresh position could open seconds before being flattened straight into the roll.

**Missed-rollover recovery (§5).** If the process wasn't alive at `ROLLOVER_TIME` (a crash, `KILL`,
`DISABLE`, an MCX holiday), the next `_setup()` finds `state.token != self._contract['token']` and
rolls **immediately at today's open** rather than walking into the `state.token` problem above with
a position on a contract `resolve_effective_contract()` no longer returns. Simpler than the evening
path: `self._contract` and `self._df_15m` are already the new contract's, freshly resolved and
seeded by the normal startup flow.

**The timeline (§6), evening path:**

| Time | What happens |
|---|---|
| As soon as confirmed (well before `ROLLOVER_TIME`) | Basis price precomputed (§8) — refreshed again right before use at `ROLLOVER_TIME`, so it can never go stale if the position closed/reopened/flipped in between |
| `ROLLOVER_PREFETCH_TIME` (`ROLLOVER_TIME` − 5 min → 23:10) | Bulk-fetch the new contract's *today* series; subscribe its WS feed. Both contracts stay subscribed simultaneously through the window — the old one isn't unsubscribed until its exit is confirmed |
| `ROLLOVER_PREFETCH_TIME` → `ROLLOVER_TIME` | Both contracts polled every minute — the old one exactly as always (uninterrupted SL/target monitoring), the new one topped up toward a complete series |
| `ROLLOVER_TIME` (23:15) | New contract's ST computed off its now-complete series; go/no-go veto decided **once** (§8) and cached — retries below only retry *execution*, never re-litigate the decision. Incomplete data here defaults to no-go |
| — | Old position flattened unconditionally (`exit_reason='rollover'`), fully subject to the fill-confirmation invariant — an unconfirmed exit leaves state as `in_trade` and retries next tick. WS unsubscribed only once confirmed |
| — | Reopen on the new contract only if go, sized to however many lots actually survived (§8) |
| — | `self._contract` swapped, rollover state cleared, persisted |

**Rule 7, extended for a stuck partial fill (§7).** The combined-order mechanics (see Rule 7 section
above) now include a `_pending_flip` marker: if a combined order doesn't fully resolve (`filled` <
`old_open_lots + new_trade_lots`), the reconciliation applies what *did* fill (lot2 closes before
lot1 on a partial close — the tie-break decided so the nearer-target lot survives, exiting sooner on
a favorable reversal) and retries the *remainder* every tick until fully resolved — not gated on a
fresh 15-min `trend_flip` transition, which only fires once. An immediate CRITICAL alert fires the
first time it gets stuck, then a debounced re-alert (`PENDING_FLIP_REALERT_DEBOUNCE_SEC=300`) while
it stays stuck.

**SL/target recalibration — historical-basis method (§8).** The reopened position's SL/targets are
computed off what the *new* contract was trading at, at the *same historical timestamp* the
original entry happened on the *old* contract (`historical_basis_price()`) — preserving the trade's
progress-so-far, at the cost of an accepted, currently-unvalidated basis-drift risk (explicit user
call). The real fill price and the recalibration basis are two different numbers, both persisted
separately (`entry_price` vs. `state.recalibration_basis_price`) — P&L always uses the real fill;
SL/target *levels* are computed once, off the basis, then persist as ordinary absolute levels. If
only one lot survived to the roll, the reopened position is sized and targeted as a lone lot2 (the
farther target), not a fresh lot1+lot2 split. Carrying the position at all is conditional on the
new contract's ST agreeing with the direction being carried (the veto above) — if it disagrees, the
position is flattened only, no reopen, exactly like `_execute_rollover_decision`'s no-go path.

**Trade-log schema (§9).** A rolled trade is two linked rows in `prometheus_trades.csv`, joined by
a new `parent_trade_id` column: the old-contract leg closes normally (`exit_reason='rollover'`),
the new-contract leg is an ordinary row except `parent_trade_id` points at the old leg's `trade_id`
and `direction` carries a `-rollover` suffix (`bullish-rollover`/`bearish-rollover`) — `state.direction`
itself never does, it stays the plain binary everywhere it drives real logic. A trade that rolls
and *doesn't* reopen (the veto fired) is just one row with `exit_reason='rollover'` — no second row,
since nothing reopened. Summing `total_pnl_rs` naively over the file double-counts a rolled trade's
continuation unless grouped by `parent_trade_id` — a documented convention, not solved structurally.

---

## State Model

State is persisted to `data/prometheus_state.csv` on every change (atomic tmp-rename write).

| Field | Values |
|---|---|
| `status` | `idle` · `watching` · `in_trade` |
| `direction` | `bullish` · `bearish` · `None` |
| `units` | integer — **persisted verbatim, never recomputed on restart** (a config change between a crash and a restart must not silently change an already-open trade's risk parameters) |
| `entry_price` / `entry_ts` | fill price / ISO timestamp |
| `recalibration_basis_price` | §8, rollover reopen only — the historical-basis price SL/target levels were computed off, kept separate from `entry_price` (the real fill). `None` for a never-rolled trade |
| `signal_ts` / `signal_close` | the 15m bar whose close triggered the flip |
| `contract_expiry` / `symbol` / `token` | the resolved effective contract at entry |
| `sl_price` | persisted verbatim, not recomputed |
| `lot1_target` / `lot2_target` / `lot2_target_source` | persisted verbatim, not recomputed |
| `lot1_lots` / `lot2_lots` | actual filled lot count per leg — partial-fill aware |
| `lot1_status` / `lot2_status` | `open` · `booked` · `never_opened` (per-lot lifecycle) |
| `lot1_exit_*` / `lot2_exit_*` | price, timestamp, reason — set only on a confirmed fill |
| `last_known_ltp` | most recent LTP, for restart recovery and session-report fallback |

**On restart mid-trade**: `_setup()` reconstructs `_pending_trade_row` from the persisted state
(so a crash doesn't lose `trade_id`/entry fields when the trade eventually closes) and posts a
loud Slack notice — the resumed state is trusted as-is, **not** reconciled against the broker's
actual order book (a flagged gap, same as Iris/Athena today; see the plan's §4 discussion).

---

## Process Lifecycle

| File | Purpose |
|---|---|
| `data/prometheus_active.flag` | Watchdog file — loop exits if deleted (clean stop) |
| `data/prometheus.pid` | PID of running process — `main()` SIGTERMs old PID on restart |
| `data/prometheus_command.flag` | Circuit breaker: `EXIT` / `KILL` / `DISABLE` — Prometheus's own dedicated flag, separate from the shared NSE/BSE `SLACK_COMMAND.flag`, so an operator managing one exchange can't accidentally kill the other |
| `data/prometheus_state.csv` | Persistent trade state |
| `data/prometheus_trades.csv` | Cumulative append-only trade tracker, schema-matched to `trade_summary_p2.csv` plus `units` |
| `data/trade_logs/trade_NNNN_*.csv` | Per-trade running log — one row per 1-min poll cycle while in-trade, same columns as the backtest's own per-trade logs |
| `data/trade_counter.txt` | Persistent sequential trade ID counter (Apollo's convention, survives restarts) |
| `data/prometheus_today_1m.csv` | Private intraday 1-min cache (§15) — Prometheus-only, cleared at every normal teardown, re-fetched fresh each day |
| `logs/prometheus_YYYYMMDD.log` | Daily rotating log |

**`EXIT`** liquidates any open position and terminates the session. **`KILL`** drops control
immediately and leaves any open position **untouched** — deliberately, per its own promised
contract ("Control dropped. Position remains OPEN.") — `_teardown()` checks `_kill_no_exit` and
skips straight to stopping the feed, no exit attempt, no cache clear (a same-day restart after
KILL should still get the cache's benefit). **`DISABLE`** is a startup-only gate, checked in
`main()` before the `Prometheus` object is even constructed.

**§2, Phase 3: teardown's normal path no longer force-exits an open position at all.** Where
Phase 2 always flattened at session end, Phase 3 leaves it open — that's the expected shape most
days (§3 of the plan: positions can span a contract roll). The only exits that happen are the
ones already firing during `run()` itself (SL/target/trend-flip); teardown just stops the feed,
clears the private cache, saves state as-is, and reports.

---

## Guardian Check

Prometheus refuses to start if Apollo, Athena, Artemis, or Iris has an open position (shared
Angel One account, shared rate-limit budget — a second concurrent login can disrupt an already-
running session). Symmetric with Iris's own guardian check against the other three.

---

## Key Parameters (`prometheus_configs.py`)

| Parameter | Value | Notes |
|---|---|---|
| `DRY_RUN` | `True` | Paper mode — see Status below before flipping this |
| `SYMBOL` | `CRUDEOILM` | Slack-switchable; `CRUDEOIL` for the full-size contract |
| `LOT_SIZE` | looked up live | 10 barrels (CRUDEOILM) / 100 barrels (CRUDEOIL) |
| `LOTS_PER_LEG` | 1 | 1 unit = 2 lots (1 lot each leg) |
| `DYNAMIC_SIZING` | `False` | Static at go-live; Artemis's margin-based formula when enabled |
| `STATIC_UNITS` | 1 | Starting size |
| `MARGIN_PER_UNIT` | 100,000 | ₹ — coupled to `SYMBOL`, overridden together via Slack |
| `ST_PERIOD` / `ST_MULTIPLIER` | 10 / 2.0 | Phase 3 live-test value (2026-09-04) — Phase 2's calibrated `3.0` was the live-test starting point, changed to `2.0` after confirming `3.0`'s live ST matched the chart |
| `SL_PCT` | 2.2% | Single shared stop — the mult-2.0 candidate's own calibrated value (2026-09-04), not Phase 2's 1.8%; a wider tail-risk backstop rather than an active trade manager (`prometheus_backtest/README.md`'s Phase 3 section) |
| `TARGET1_PCT` | 2.0% | Lot 1 — mult-2.0 candidate's value; landed at the top of its own tested grid (0.5–2.0%), a known open caveat |
| `TARGET2_MODE` / `TARGET2_FLAT_PCT` | `flat_pct` / 5.0% | Lot 2 — mult-2.0 candidate's value; hardcoded `'pct'` mode in production per Rollout step 5 |
| `TENDER_ROLL_TRADING_DAYS` | 5 | Trading days before expiry to roll early |
| `SEED_DAYS` | 18 | Calendar days of 1-min history tail-read for ST seeding |
| `MIN_ENTRY_BUFFER_MIN` | 15 | No entry until the session's real open + this many minutes (fixed 2026-09-04 from a hardcoded `MIN_ENTRY_TIME` clock time — see the Entry/Exit Priority section) — the only entry-timing gate left (§2, Phase 3: no cutoff before close) |
| `NO_EXIT_BEFORE_BUFFER_MIN` | 1 | No SL/target exit check until the session's real open + this many minutes (§10, built 2026-09-04) |
| `CLOSING_TIME` | 23:30 | **DST-dependent — must be hand-toggled around US DST changes** (→23:30 ~2nd Sun March, →23:55 ~1st Sun Nov) |
| `SESSION_END_BUFFER_MIN` | 25 | → `SESSION_END_TIME`, the main loop's own hard exit clock (process lifecycle only — unaffected by §2's no-EOD-flatten change) |
| `CANDLE_POLL_LIMIT` / `LTP_POLL_LIMIT` | 3 / 10 per sec | Broker-wide client-side rate caps |
| `ORDER_TIMEOUT_SEC` | 30 | WS fast path + REST fallback timeout |
| `TRADE_UPDATE_SEC` | 20 | Slack update cadence while in-trade — matches Artemis's/Athena's convention, not Iris's 10s |
| `DEFERRED_BAR_CUTOFF_MIN` | 1 | §12 — how long to wait for a 15m window to fully arrive before building it from what's on hand |
| `SEED_RETRY_ATTEMPTS` / `SEED_RETRY_INTERVAL_SEC` | 5 / 120 | §15 — bounded, blocking startup retry around `seed_st15` (safe pre-position, no concurrent loop to starve) |
| `OPENING_BAR_CORRECTION_ENABLED` | `False` | §11 — the opening-bar fix always runs and logs; only patches when `True` |
| `OPENING_BAR_ARTIFACT_THRESHOLD` | 0.5 | §11 — CRUDEOIL/CRUDEOILM true-range ratio below this at 09:00 triggers the substitution |
| `ST_SEED_SKIP_DATES` | `[]` | §14 — manually populated dates excluded from the daily seed's tail-read (whole bad sessions, e.g. a Budget special session) |
| `REJECTION_RETRY_ATTEMPTS` / `_COOLDOWN_SEC` | 3 / 1 | §1 — `place_order`'s retry on an actual broker rejection |
| `GHOST_RECOVERY_COOLDOWN_SEC` / `_LOOKBACK_SEC` | 2 / 60 | §1 — `place_order`'s order-book check on a `DataException`/`NetworkException` |
| `ROLLOVER_TIME` | 23:15 | §4/§6 — evening cutoff where a confirmed roll actually executes (`CLOSING_TIME` − `ROLLOVER_BEFORE_CLOSE_MIN=15`, same DST caveat as `CLOSING_TIME`) |
| `ROLLOVER_PREFETCH_TIME` | 23:10 | §6 step 2 — `ROLLOVER_TIME` − `ROLLOVER_PREFETCH_BUFFER_MIN=5`; new contract's today series bulk-fetched + WS subscribed here |
| `PENDING_FLIP_REALERT_DEBOUNCE_SEC` | 300 | §7 — re-alert cadence for a stuck Rule 7 `_pending_flip`, matches the stale-tick-watchdog's existing convention |

---

## Resilient Order Execution (§1)

`place_order()` was ported from Athena's `_place_order` pattern (`athena_engine.py`) 2026-09-04 —
the original single-`try`/`except` version (Iris's simpler pattern, ported for Phase 2) didn't do
any of this:

1. **Freeze-limit quantity splitting** — chunks a request bigger than the broker will accept in
   one order into several, each placed separately. `freeze_qty` is read live off the resolved
   contract (`resolve_effective_contract()`'s own dict, sourced from
   `data_pipeline/data/mcx_instrument_master.csv`), not a hardcoded constant like the other four
   strategies' `QTY_FREEZE` — MCX freeze quantities are set per-commodity. Confirmed
   `freeze_qty=10000` for CRUDEOILM (`lotsize=10`) → up to 1000 lots per order; today's 2-4 lot
   sizing never exercises the split.
2. **Rejection retry** — an actual `'rejected'` broker response retries up to
   `REJECTION_RETRY_ATTEMPTS` times before giving up on that chunk.
3. **Ghost-order recovery** — on `DataException`/`NetworkException` specifically (a lost
   response, not necessarily a lost order), checks the order book for a matching order (same
   symbol/type/quantity, recently updated, not already claimed by this run) before assuming
   nothing happened and retrying placement — avoids a genuine double-fill on a network blip.

**Returns a LIST of order IDs, not one.** `get_fill_price_and_qty()` aggregates fill
quantity/value across the whole list (summed, then a blended average price) before returning —
mirrors Athena's `_fetch_order_details`. At today's sizing this list always has one element and
the aggregation degenerates to the original single-order behavior.

`get_fill_price_and_qty()`'s own two-path approach is otherwise unchanged from Phase 2:
1. **Fast path**: `OrderFillWatcher` (WebSocket `SmartWebSocketOrderUpdate`) — resolves as soon
   as every order ID in the list is confirmed
2. **Fallback**: REST order-book poll if WS doesn't confirm within `ORDER_TIMEOUT_SEC`
3. **DRY_RUN**: returns the current feed LTP immediately (no order placed)

**Partial fills** (fewer lots filled than requested) are treated as an intentional smaller
trade — lot 2 simply may never open, and is never retried (retrying introduces its own
entry-price-drift risk). If some chunks of a multi-chunk order fill and others time out
(unreachable at today's sizing, since chunking never fires), that's treated as a hard failure
rather than a partial-across-orders reconciliation — consistent with "never fabricate, never
guess."

---

## Private Intraday Cache (§15)

Prometheus maintains its own 1-min OHLCV cache, `data/prometheus_today_1m.csv` — separate from
the shared MCX contract CSV `data_downloader_mcx.py` maintains, which Prometheus stopped writing
to entirely (2026-09-04). Nothing else reads or writes this file, so none of the write-race/
single-source-of-truth reasoning that removed Prometheus's writes to the *shared* file applies to
it.

- **Written incrementally**: every 1-min poll (`_merge_1m`) appends to it, same merge-dedup
  logic (`_merge_and_save`) the shared file uses.
- **Read on startup**: `seed_st15` reads the cache for today's data and only live-fetches the
  *gap* since its last row — a mid-day crash-restart goes from "re-fetch the whole session" to
  "re-fetch a few minutes."
- **Cleared at every normal teardown** (`clear_today_cache()`) — except the `KILL` path, which
  explicitly anticipates a same-day restart and shouldn't lose the cache's benefit.
- **Date-filtered on every read** (`read_today_cache`) as a second line of defense — even an
  ungraceful crash that skips the clear can't leave a stale prior-day cache silently misread as
  today's data.

A real, pre-existing bug in `_merge_and_save` was found and fixed while wiring this in: writing
to the same file twice in a row (on-disk rows re-parsed to a fixed-offset tz, freshly-localized
new rows to a named-zone tz — numerically identical, different pandas dtypes) silently turned the
older rows into `NaT` on the second write. This affected the shared per-contract files too
(`backfill_contract_if_needed`'s multi-chunk backfills), not just this new cache — fixed by
normalizing both sides to tz-naive before concatenating.

**A second, related tz bug — caught live, first real Delos run of Phase 3 (2026-09-04):**
`fetch_one_minute_window` parses the broker's `+05:30`-suffixed timestamps with a format string
that includes `%z`, so its output was tz-**aware** (fixed offset), while every other in-memory
series in this file (`_tail_read_contract_csv`, `read_today_cache`, `_df_1m_today`) is tz-naive.
Three of five call sites already stripped this defensively right after calling it —
`seed_st15`'s own gap-fetch (`prometheus_functions.py`) didn't, so on the very first live run (a
fresh, empty private cache) the live gap-fetch's tz-aware rows collided with the shared
pipeline's tz-naive past-days rows on the next concat: `TypeError: Cannot compare tz-naive and
tz-aware timestamps`, crashing `_setup()` before the session even reached the main loop. Fixed at
the source — `fetch_one_minute_window` itself now strips tz to naive before returning — so every
caller gets consistent naive timestamps without needing to remember to do it themselves.

---

## Setup on Delos

```bash
# Ensure data/ symlinks are in place:
#   data/user_credentials.csv -> ../../data/user_credentials.csv
ls prometheus_production/data/instrument_override.json 2>/dev/null   # optional, absent = SYMBOL default

# Verify DRY_RUN before any live run — should read False only after the
# Rollout plan's steps 2-4 are complete (order-update WS parity check,
# backtest/live parity, a fresh DRY_RUN pass under real conditions)
grep DRY_RUN prometheus_production/prometheus_configs.py

# Start (from repo root) — Prometheus owns its own session, not Leto-launched
python prometheus_production/prometheus.py

# Stop cleanly
rm prometheus_production/data/prometheus_active.flag
```

---

## Backtest Reference

See [`prometheus_backtest/README.md`](../prometheus_backtest/README.md) for the full calibration
journey, both phases. **This production module now runs Phase 3's mult-2.0 candidate**
(`prometheus_backtest/phase3/`), decided 2026-09-04 — `ST_MULTIPLIER=2.0`, `SL_PCT=2.2`,
`TARGET1_PCT=2.0`, `TARGET2_FLAT_PCT=5.0`, replacing the Phase 2 config this table used to show.

| Metric | Phase 3 mult 2.0 (live) | Phase 2 (superseded reference) |
|---|---|---|
| Config | `ST_MULTIPLIER=2.0`, `SL_PCT=2.2`, `TARGET1_PCT=2.0`, `TARGET2_MODE='flat_pct'`, `TARGET2_FLAT_PCT=5.0` | `ST_MULTIPLIER=3.0`, `SL_PCT=1.8`, `TARGET1_PCT=1.0`, `TARGET2_MODE='flat_pct'`, `TARGET2_FLAT_PCT=2.3` |
| Trades | 380 (refreshed 2026-09-04, through 2026-09-03) | 226 (refreshed 2026-09-04, through 2026-09-03) |
| Win rate | 44.5% | 55.8% |
| Total P&L | ₹169,779 | ₹42,778 |
| Max drawdown | −₹16,235 (per-trade series) / −₹16,625 (per-lot-exit-event series) | −₹14,943 |
| Calmar | 10.21 (per-lot-exit-event, methodology-comparable to the mult-2.5 candidate) | 2.86 (unitless) / 4.84 (annualized, ₹1L capital basis) |
| Cross-validation | CRUDEOILM only — **not yet cross-validated on CRUDEOIL** (open caveat, see `prometheus_backtest/README.md`'s Phase 3 section) | Confirmed on CRUDEOIL (full-size contract) before being trusted |

Trade count is much higher for Phase 3 because it's positional (no EOD square-off, no
entry-time gate) — not directly comparable to Phase 2's win rate/trade-count without accounting
for that structural difference; Calmar is the fairer cross-phase comparison.

---

## Status

- [x] Effective-contract resolution — exchange front-month with 5-trading-day-early roll, plus
      `freeze_qty` read live off the instrument master (§1)
- [x] ST_15 seeding — past days from the shared pipeline file, today from the private cache + live
      gap-fetch, gap-checked, bounded blocking startup retry (§15)
- [x] Resilient 1-min polling — inner retry + non-blocking outer recovery queue
- [x] Deferred 15m-bar computation — waits up to `DEFERRED_BAR_CUTOFF_MIN` for a genuinely
      complete window before building from what's on hand (§12); underlying resample function's
      day-end boundary bug fixed in the same pass (§17)
- [x] Opening-bar price-artifact correction — built, gated `OPENING_BAR_CORRECTION_ENABLED=False`
      pending chart validation (§11)
- [x] 2-lot scale-out entry — partial-fill aware, lot 2 never retried if it doesn't fill
- [x] Three exit conditions — SL, lot1 target, lot2 target, plus trend-flip (Rule 7 — now a single
      combined net order for the exit+re-entry, not the old two/three-order sequence, §7). **No EOD
      square-off any more** (§2) — a position carries across sessions
- [x] Fill-confirmation invariant — no lot is ever marked closed without a genuine confirmed fill
- [x] Resilient order execution — freeze-limit chunking, rejection retry, ghost-order recovery,
      list-based order IDs aggregated on fill (§1)
- [x] Realised/unrealised/total P&L reporting — both the periodic Slack update and the
      session-report "still open" fallback (§13)
- [x] ST seed skip-list — `ST_SEED_SKIP_DATES` for known-bad sessions (§14)
- [x] `state.token` invariant — `_get_ltp()` now keys off the position's own token, not whichever
      contract is currently "effective" (§3, ahead of rollover landing)
- [x] State persistence — atomic CSV write, mid-trade restart recovery (not yet broker-reconciled)
- [x] Guardian check — blocks start if another strategy has an open position
- [x] Own circuit breaker — `prometheus_command.flag`, separate from the shared NSE/BSE one
- [x] DRY_RUN paper mode — LTP-based fills, no real orders
- [x] Session report — per-trade + session-total Slack summary at teardown
- [x] **Contract rollover — trigger, recovery, timing, veto, recalibration, trade-log schema
      (plan §3–§9, built 2026-09-04)**: `state.token` invariant (§3); the evening lookahead +
      entry suppression near `ROLLOVER_TIME` (§4); missed-rollover recovery at next startup, per
      the decided "roll immediately, loudly alerted" option (§5); the full `ROLLOVER_PREFETCH_TIME`
      → dual-poll → `ROLLOVER_TIME` → veto → flatten → reopen timeline, with the old contract's WS
      only unsubscribed once its exit is confirmed (§6); Rule 7's combined-order mechanics extended
      with a `_pending_flip` retry-until-resolved marker for a stuck partial fill (§7); the
      historical-basis recalibration method with its own ST-disagreement veto and a
      `recalibration_basis_price` field kept separate from the real fill price (§8); and the
      two-linked-rows trade-log schema (`parent_trade_id`, `bullish-rollover`/`bearish-rollover`
      direction labels) (§9). Verified against real on-disk CRUDEOILM/CRUDEOIL data (the historical
      basis lookup, the ST-for-a-not-yet-effective-contract computation) and mocked broker
      responses (full/partial-fill Rule 7 reconciliation with the lot2-first tie-break, the
      rollover reopen's basis-vs-fill-price separation) — not yet live-tested end-to-end, since
      that needs an actual rollover (~2026-09-15, `TENDER_ROLL_TRADING_DAYS=5` off the Sept-21
      CRUDEOILM expiry) to exercise for real.
- [x] **Provisional boundary computation (§12a, built 2026-09-04)** — at a 15m-aligned boundary,
      if REST is already incomplete at that instant, build a provisional candle from
      `SharedFeed.get_ohlc()`'s genuine tick-aggregated OHLC (not sampled — Apollo already reads
      this for Nifty/VIX), act on it (entry and/or exit, same real order-placement paths as a
      normal flip) only if it clears `PROVISIONAL_MARGIN_PCT` past the band, then reconcile
      against the real bar once REST recovers. **Enabled** (`PROVISIONAL_BOUNDARY_ENABLED=True`,
      commit `806aeb0`) specifically to stress-test it under DRY_RUN — deliberate departure from
      the "shadow-log first, calibrate on real data" pattern used for
      `OPENING_BAR_CORRECTION_ENABLED`/`ENTRY_FILTER_1H_ALIGN_ENABLED`, safe here only because
      every action it gates is a DRY_RUN-simulated fill, not a real order; do **not** carry this
      `True` into a `DRY_RUN=False` flip without reviewing how it actually behaved first. Still
      shadow-logs the verdict unconditionally regardless, so `PROVISIONAL_MARGIN_PCT` (currently
      an uncalibrated placeholder) can eventually be sized on real agreement data — see
      `plans/prometheus-phase3-production.md` §12a for the full design and the
      margin-guard-over-unwind-path reasoning. Mock-verified (14/14 checks) before deploy; live
      behavior not yet observed — the trigger condition is rare (never breached the existing
      1-minute cutoff in ~34 hours of DRY_RUN observation before this was built).
- [x] **First-minute exit guard (§10, built 2026-09-04)** — `_past_first_minute_guard` gates
      `_check_exit_conditions_ltp` for `NO_EXIT_BEFORE_BUFFER_MIN` (1 min) after today's session
      genuinely opens, protecting against a repeat of the 2026-09-02 447-point single-minute
      price-discovery print. Keyed off the actual first 1-min bar seen today, not a hardcoded
      clock time — the plan's original `NO_EXIT_BEFORE='09:01'` proposal would have silently done
      nothing on the ~7/153 evening-only special sessions, where the real open is 17:00, not
      09:00. Mock-verified (8/8 checks, including the evening-only case). Building this surfaced a
      related, already-live bug, fixed the same day: `MIN_ENTRY_TIME='09:15'` had the identical
      hardcoded-clock-time flaw and had been gating real entries since Phase 3 went live — replaced
      by `_past_min_entry_guard`/`MIN_ENTRY_BUFFER_MIN=15` (shares `_minutes_since_session_open`
      with the exit guard above, one dynamic-anchor mechanism, not two). The old bare
      `now_after_min_entry()` function is gone; all three call sites (fresh entry, Rule 7
      re-entry, the provisional-boundary path) now call `self._past_min_entry_guard`. Mock-verified
      (8/8 checks, including the evening-only case).
- [x] **1h/15m entry filter — built, unit-tested, gated off (§17, 2026-09-04)**:
      `_check_1h_alignment` computes ST_1H via the generalized `compute_st_for_contract`
      (`minutes=60, st_period=ST_1H_PERIOD, st_multiplier=ST_1H_MULTIPLIER`), reusing the same
      "no silent staleness" gap-refusal as the 15m rollover veto. Wired into all three entry
      paths uniformly, no per-site exceptions: fresh `watching`→`in_trade` entry, Rule 7
      re-entry (gates only the re-entry half of the combined order — the exit half always
      proceeds), and the rollover reopen (ANDed on top of §8's own 15m ST-disagreement veto —
      either failing lands on the same flatten-and-wait outcome). Unit-tested (toggle on/off,
      agree/disagree in both directions, empty/NaN ST result treated as disagree, and the
      toggle-off short-circuit never touching `self`). **Gated off**:
      `ENTRY_FILTER_1H_ALIGN_ENABLED = False`; `ST_1H_PERIOD`/`ST_1H_MULTIPLIER` are unset
      placeholders (same starting values as the 15m ST params), not calibrated — left off so
      live ST_15 flip accuracy can be validated against the chart first, per the same reasoning
      as §11's opening-bar correction toggle.
- [ ] **Order-update WebSocket unverified for MCX** — worked during the brief 2026-08-31 live
      window (four real fills resolved via WS), but that window was cut short by the incident
      below before a full session could confirm it under sustained live conditions
- [ ] **DRY_RUN=False live deployment** — reverted to `True` on 2026-08-31 after a real incident:
      a trend-flip exit order failed at the broker with no guard against it, and the code
      fabricated a fill from LTP, marking both lots closed internally while the real position
      stayed open and unmonitored for ~28 minutes. Fixed via the fill-confirmation invariant
      above (commit `8b7bc5b`). **Do not flip `DRY_RUN` back to `False` until that fix has held
      up under a fresh DRY_RUN pass**, per `prometheus_configs.py`'s own comment.
- [ ] Backtest/live parity check (Rollout step 3, plan)
- [ ] Broker-side reconciliation on mid-trade restart (flagged gap, shared with Iris/Athena)
