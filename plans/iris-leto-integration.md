# Plan: Iris ↔ Leto Integration (Replacing Apollo at VIX > 25)

**Status:** Implemented (2026-06-18)  
**Scope:** Wire Iris into Leto's routing and Slack control panel, matching Apollo's interface exactly.  
**No code is touched until explicit approval.**

**Post-implementation decisions (2026-06-18):**
- `DRY_RUN = False` — Iris goes live immediately as part of this plan (option A chosen).
- `LOT_COUNT = 40` — static sizing; `LOT_CALC = False`. Dynamic sizing deferred.
- Manual routing overrides (Force Athena/Artemis/Iris) bypass VIX unconditionally.

---

## Open Decisions (answer before implementation begins)

These block implementation — they change what gets written.

### 1. DRY_RUN — critical

`iris_production/configs.py:21` has `DRY_RUN = True`. If we wire Iris into the
VIX > 25 slot but leave this as-is, no live orders will be placed at VIX > 25.
Apollo is no longer routed, Iris places nothing. The system silently does not trade.

**Decision needed:** When is `DRY_RUN` flipped to `False`?

Options:
- **A) Flip now, as part of this plan.** Iris goes live the moment routing switches.
- **B) Keep DRY_RUN=True for a defined paper-trading window post-routing-switch.** Iris
  appears in Leto's routing (and session reports will show paper trades), but no real
  orders fire. After N days of paper parity, flip it manually.
- **C) Routing switch and DRY_RUN flip are two separate commits.** This plan wires the
  routing; a follow-up commit flips DRY_RUN when you're satisfied.

Recommendation: **C**. The routing wiring is independently testable. DRY_RUN=False is
a one-line change and a distinct decision. Keeps the blast radius small.

### 2. "Manage Sizing" Dynamic Auto-Sizing for Iris

The Manage Sizing modal offers two modes: *Dynamic Auto-Sizing* and *Fixed Lots*.
Apollo has `_calculate_lots()` doing margin-based dynamic sizing, so both modes work.
**Iris has no dynamic sizing path** — it uses `LOT_COUNT` directly.

**Decision needed:** Should the modal let users select "Dynamic Auto-Sizing" for Iris?

Options:
- **A) Iris is fixed-lots-only.** Modal still shows Iris as a dropdown option, but
  `lot_calc=True` is silently ignored; only `lot_count` is used. A warning is logged.
  Simple, no new code.
- **B) Block "Dynamic" for Iris in the modal.** The modal shows a note or disables the
  option when Iris is selected (requires client-side JS in the Block Kit, more complex).
- **C) Implement dynamic sizing for Iris** at a later date. For this plan, same as A.

Recommendation: **A**. Iris reads `lot_count` from the override regardless of `lot_calc`.
Log a warning if `lot_calc=True` is found. Document in the modal description.

### 3. Friday Fresh Entry

At VIX > 25 on Friday, the current code routes to Apollo (which enters a directional
trade). After this change, Iris will enter fresh on Fridays at VIX > 25.

Iris is pure intraday — exits by 15:15, no overnight gap risk — so this is safe.
**But it is a behavior change worth noting explicitly.** If you want Fridays at VIX > 25
to stand down (no fresh entries), that is a one-line change in `_route()`.

Recommendation: **keep Friday entry**, same as Apollo. Note it here for sign-off.

---

## Architecture summary

Apollo's interface:
```python
Apollo(obj, auth_token, instrument_df_nifty).run()
# Returns: (False, summary_dict)
# summary keys: strategy, traded, no_trade_reason, direction, lots,
#               entry_time, spot_entry, exit_time, exit_reason,
#               pnl_pts, pnl_rs, peak_pnl_pts
```

After this plan, Iris will match this contract exactly, with one addition: Iris can
trade multiple times per session. The summary will aggregate across all trades.

---

## File 1: `iris_production/iris.py`

Five changes.

### 1a. Constructor — make `api_key`/`client_code` optional

Leto already has `api_key` and `user_name` (= `client_code`) as module-level variables
and will pass them directly. Making them optional allows standalone `main()` to keep
working without change (it reads from CREDS_FILE and passes them explicitly).

```python
# BEFORE
class Iris:
    def __init__(self, obj, auth_token: str, api_key: str,
                 client_code: str, instrument_df: pd.DataFrame):

# AFTER
class Iris:
    def __init__(self, obj, auth_token: str, instrument_df: pd.DataFrame,
                 api_key: str = None, client_code: str = None):
```

If `api_key` or `client_code` is None, read from `CREDS_FILE`:

```python
        if api_key is None or client_code is None:
            _creds     = pd.read_csv(CREDS_FILE).iloc[0]
            api_key    = api_key    or _creds['api_key']
            client_code = client_code or _creds['user_name']
        self._api_key     = api_key
        self._client_code = client_code
```

Note: `main()` currently passes them positionally — update `main()` to use keyword args
after the reorder:
```python
# main() call site (unchanged behaviour, just explicit kwargs)
iris = Iris(obj, auth_token, instrument_df=nifty_df,
            api_key=api_key, client_code=client_code)
```

### 1b. Add `_summary` dict and trade accumulators

Initialise in `__init__` after the existing instance variables:

```python
        self._summary = {
            'strategy':        'Iris',
            'traded':          False,
            'no_trade_reason': 'No signal',
        }
        self._trade_count   = 0
        self._total_pnl_rs  = 0.0
        self._total_pnl_pts = 0.0
        self._peak_pnl_pts  = 0.0   # best intrabar unrealised pts across all trades
```

### 1c. Update `_execute_entry()` to populate summary

At the end of `_execute_entry()`, after the state is saved:

```python
        self._trade_count += 1
        self._summary.update({
            'traded':      True,
            'direction':   direction,
            'lots':        LOT_COUNT,
            'entry_time':  now.strftime('%H:%M'),
            'spot_entry':  self.feed.get_ltp(NIFTY_TOKEN),
        })
        self._summary.pop('no_trade_reason', None)
```

### 1d. Update `_execute_exit()` to accumulate P&L and update summary

At the point where P&L is computed (after `_close_position()` returns), accumulate:

```python
        pnl_pts = self.state.exit_price - self.state.entry_price
        if self.state.direction == 'bearish':
            pnl_pts = -pnl_pts   # long put: gain when price falls
        pnl_rs  = pnl_pts * LOT_SIZE * self._effective_lot_count()

        self._total_pnl_pts += pnl_pts
        self._total_pnl_rs  += pnl_rs

        self._summary.update({
            'exit_time':    datetime.now().strftime('%H:%M'),
            'exit_reason':  reason,
            'pnl_pts':      self._total_pnl_pts,
            'pnl_rs':       self._total_pnl_rs,
            'peak_pnl_pts': self._peak_pnl_pts,
            'trade_count':  self._trade_count,
        })
```

Note: `_check_exit_conditions()` already tracks unrealised P&L for the Slack update.
Extend that method to also update `self._peak_pnl_pts`:

```python
        # inside _check_exit_conditions(), where unrealised_pts is computed
        if unrealised_pts > self._peak_pnl_pts:
            self._peak_pnl_pts = unrealised_pts
```

### 1e. Add `_check_slack_commands()`

Add this method (modelled directly on Apollo's implementation):

```python
    def _check_slack_commands(self) -> None:
        flag_file = Path(REPO_ROOT) / 'data' / 'SLACK_COMMAND.flag'
        if not flag_file.exists():
            return
        try:
            cmd = flag_file.read_text().strip().upper()
        except Exception:
            return
        if cmd == 'EXIT':
            logger.info('SLACK EXIT command received — liquidating and halting.')
            _slack('⚠️ *Iris*: EXIT command received. Liquidating.', SLACK_ERRORS_CHANNEL)
            if self.state.status == 'in_trade':
                self._execute_exit('slack_exit')
            self._shutdown = True
        elif cmd == 'KILL':
            logger.info('SLACK KILL command received — dropping control immediately.')
            _slack('🚨 *Iris*: KILL command received. Dropping control.', SLACK_ERRORS_CHANNEL)
            self._shutdown = True
        elif cmd == 'DISABLE':
            logger.info('SLACK DISABLE command received — halting after current trade.')
            _slack('⏸️ *Iris*: DISABLE command received. Will not enter new trades.',
                   SLACK_ERRORS_CHANNEL)
            self._shutdown = True
```

Call this at the top of the `while` loop (before the market-close check):

```python
        while not self._shutdown and FLAG_PATH.exists():
            self._check_slack_commands()   # ← add this line
            now = datetime.now()
            ...
```

### 1f. Add `sizing_override.json` support

Read the override at startup (in `_setup()`, before subscribing feeds), falling back to
`LOT_COUNT` from configs if absent. Iris is fixed-lots-only; if `lot_calc=True` is
written by the Slack modal, ignore it and log a warning.

Add a helper (near other helpers, not inside a method):

```python
def _effective_lot_count() -> int:
    override_path = DATA_DIR / 'sizing_override.json'
    if not override_path.exists():
        return LOT_COUNT
    try:
        import json
        ov = json.loads(override_path.read_text())
        if ov.get('lot_calc'):
            logger.warning('sizing_override lot_calc=True ignored; Iris is fixed-lots-only.')
        count = int(ov.get('lot_count', LOT_COUNT))
        return count if count > 0 else LOT_COUNT
    except Exception as e:
        logger.warning(f'Could not read sizing_override.json: {e}')
        return LOT_COUNT
```

Replace every reference to `LOT_COUNT` in order-placement code with
`_effective_lot_count()`. (Grep for `LOT_COUNT` to find all placement sites.)

### 1g. Fix `run()` return type

```python
# BEFORE
    def run(self) -> None:
        if not self._setup():
            return
        ...
        self._teardown()

# AFTER
    def run(self) -> tuple[bool, dict]:
        if not self._setup():
            self._summary['no_trade_reason'] = 'Setup failed'
            return False, self._summary
        ...
        self._teardown()
        return False, self._summary
```

Iris never hands back to Leto mid-session (it runs until market close), so the first
element is always `False`.

---

## File 2: `leto.py`

Five changes.

### 2a. Add `IRIS_DIR` constant

After the existing `APOLLO_DIR` line:

```python
APOLLO_DIR = os.path.join(REPO_ROOT, "apollo_production")
IRIS_DIR   = os.path.join(REPO_ROOT, "iris_production")   # ← add
```

### 2b. Add `_iris_trade_open()`

After `_apollo_trade_open()`:

```python
def _iris_trade_open():
    """Return True if iris_state.csv records an active trade."""
    state_file = os.path.join(IRIS_DIR, 'data', 'iris_state.csv')
    if not os.path.exists(state_file):
        return False
    try:
        df = pd.read_csv(state_file)
        if df.empty:
            return False
        return str(df.iloc[0].get('status', 'idle')) == 'in_trade'
    except Exception as e:
        logger.error(f"Could not read Iris state file: {e}")
        return False
```

Note: Iris uses `watching` (armed but flat) and `in_trade`. Only `in_trade` represents
an open position that needs resumption.

### 2c. Add `_run_iris()`

After `_run_apollo()`:

```python
def _run_iris(obj, auth_token, instrument_df_nifty):
    """Run Iris. Returns (handback: bool, summary: dict)."""
    logger.info("Starting Iris.")
    if IRIS_DIR not in sys.path:
        sys.path.insert(0, IRIS_DIR)
    from iris import Iris  # type: ignore

    flag_path = os.path.join(IRIS_DIR, 'data', 'iris_active.flag')
    try:
        open(flag_path, 'w').close()   # arm the watchdog (same as standalone main())
        iris = Iris(obj, auth_token, instrument_df_nifty,
                    api_key=api_key, client_code=user_name)
        handoff, summary = iris.run()
    finally:
        try:
            os.remove(flag_path)
        except FileNotFoundError:
            pass   # teardown already removed it

    logger.info(f"Iris returned. Handoff signal: {handoff}")
    return bool(handoff), summary
```

`api_key` and `user_name` are Leto module-level variables (lines 65–66). Iris will not
re-read CREDS_FILE when these are supplied.

### 2d. Update `_route()` — 4 Apollo call sites → Iris

**Priority 1: open position check (transition safety)**

Keep `_apollo_trade_open()` in place until Apollo is confirmed clear. After Apollo has
no live positions, remove it in a separate cleanup commit. Add Iris check immediately
after:

```python
    # Priority 1: resume open positions unconditionally
    if _apollo_trade_open():
        logger.info("Open Apollo trade detected. Routing to Apollo.")
        _slack("*Leto*: Open Apollo trade detected. Routing to Apollo.")
        _, summary = _run_apollo(obj, auth_token, instrument_df_nifty)
        return False, summary

    if _iris_trade_open():                                          # ← new
        logger.info("Open Iris trade detected. Routing to Iris.")  # ← new
        _slack("*Leto*: Open Iris trade detected. Routing to Iris.") # ← new
        _, summary = _run_iris(obj, auth_token, instrument_df_nifty) # ← new
        return False, summary                                       # ← new
```

**Friday VIX > 25 (line 369–373):**

```python
        if vix > VIX_ATHENA_MAX:
            logger.info(f"Friday. VIX {vix:.2f} > {VIX_ATHENA_MAX}. Routing to Iris.")
            _slack(f"*Leto*: Friday. VIX {vix:.2f} > {VIX_ATHENA_MAX}. Routing to Iris.")
            _, summary = _run_iris(obj, auth_token, instrument_df_nifty)
            return False, summary
```

**Auto-routing VIX > 25 (lines 395–410), comment update + call site:**

```python
    # Auto routing (also handles manual + VIX > 25 → Iris)
    ...
    else:
        logger.info(f"VIX {vix:.2f} > {VIX_ATHENA_MAX}. Routing to Iris.")
        _slack(f"*Leto*: VIX {vix:.2f}. Routing to *Iris*.")
        _, summary = _run_iris(obj, auth_token, instrument_df_nifty)
        return False, summary
```

### 2e. Update session report

Add `'Iris'` to `_STRATEGY_SUBTITLE`:

```python
_STRATEGY_SUBTITLE = {
    'Apollo':  'Nifty Debit Spread',
    'Iris':    'Nifty Scalping',          # ← add
    'Athena':  'Nifty Double Calendar',
    'Artemis': 'Sensex Iron Condor',
}
```

Add Iris block in `_send_session_report()`, after the Apollo block (line 499–501):

```python
        if strategy == 'Apollo':
            direction = s.get('direction', '?').capitalize()
            lines.append(f"  ↳ Direction  : {direction}  |  Lots: {lots}")
        elif strategy == 'Iris':
            direction   = s.get('direction', '?').capitalize()
            trade_count = s.get('trade_count', 1)
            count_str   = f"{trade_count} trade{'s' if trade_count != 1 else ''}"
            lines.append(f"  ↳ Direction  : {direction}  |  Lots: {lots}  |  {count_str}")
        elif strategy == 'Athena':
            ...
```

For multi-trade sessions, `direction` reflects the most recent trade (set on each
`_execute_entry()` call, so it's the last one). `trade_count` shows the total.
P&L (`pnl_pts`, `pnl_rs`) is the aggregate across all trades — the existing
`total_rs += pnl_rs` accumulation in the report loop handles it correctly.

---

## File 3: `slack_listener.py`

Four changes.

### 3a. Replace Apollo with Iris in `SIZING_OVERRIDE_PATHS` (line 24)

```python
# BEFORE
SIZING_OVERRIDE_PATHS = {
    'Artemis': os.path.join(BASE_DIR, 'artemis_production', 'data', 'sizing_override.json'),
    'Athena':  os.path.join(BASE_DIR, 'athena_production',  'data', 'sizing_override.json'),
    'Apollo':  os.path.join(BASE_DIR, 'apollo_production',  'data', 'sizing_override.json'),
}

# AFTER
SIZING_OVERRIDE_PATHS = {
    'Artemis': os.path.join(BASE_DIR, 'artemis_production', 'data', 'sizing_override.json'),
    'Athena':  os.path.join(BASE_DIR, 'athena_production',  'data', 'sizing_override.json'),
    'Iris':    os.path.join(BASE_DIR, 'iris_production',    'data', 'sizing_override.json'),
}
```

### 3b. Add `IRIS_STATE` constant, replace in `reset_all_states()` (lines 30, 317)

```python
# BEFORE (line 30)
APOLLO_STATE  = os.path.join(BASE_DIR, "apollo_production",  "data", "apollo_state.csv")

# AFTER — keep APOLLO_STATE for the transition period if needed; add IRIS_STATE
APOLLO_STATE  = os.path.join(BASE_DIR, "apollo_production",  "data", "apollo_state.csv")
IRIS_STATE    = os.path.join(BASE_DIR, "iris_production",    "data", "iris_state.csv")
```

In `reset_all_states()` at line 315–318, replace Apollo with Iris:

```python
# BEFORE
    for label, path, col in [
        ("Athena", ATHENA_STATE, "status"),
        ("Apollo", APOLLO_STATE, "status"),
    ]:

# AFTER
    for label, path, col in [
        ("Athena", ATHENA_STATE, "status"),
        ("Iris",   IRIS_STATE,   "status"),
    ]:
```

Note: once Apollo is confirmed clear, `APOLLO_STATE` can be removed in the cleanup
commit. During the transition, it's harmless — the file simply won't exist and the
reset will skip it cleanly.

### 3c. Update sizing modal dropdown (line 623)

```python
# BEFORE
{"text": {"type": "plain_text", "text": "Apollo (Nifty Trend)"}, "value": "Apollo"}

# AFTER
{"text": {"type": "plain_text", "text": "Iris (Nifty Scalping)"}, "value": "Iris"}
```

### 3d. Update routing description text (line 188)

```python
# BEFORE
"text": {"type": "mrkdwn", "text": "*Routing and Sizing Override:*\nForce strategy selection for the next Mon–Thu entry and manage position sizing across strategies. Apollo always runs if VIX > 25."}

# AFTER
"text": {"type": "mrkdwn", "text": "*Routing and Sizing Override:*\nForce strategy selection for the next Mon–Thu entry and manage position sizing across strategies. Iris runs at VIX > 25."}
```

---

## File 4: `README.md`

Five sections to update.

### 4a. Routing logic section

Update the VIX routing table and description to replace Apollo with Iris at VIX > 25.

### 4b. Mermaid diagram

Replace the `Apollo` node and the `VIX > 25 → Apollo` arrow with Iris.

### 4c. VIX Regime table

| VIX | Strategy | Notes |
|-----|----------|-------|
| ≤ 16 | Artemis | Sensex iron condor |
| 16–25 | Athena | Nifty double calendar |
| > 25 | ~~Apollo~~ **Iris** | Nifty scalping |

### 4d. Remove the "Pending upgrade" block

The `plans/leto-routing-optimisation.md` reference in README is the pending note for
this exact change. Remove that block once the plan is approved and implemented.

### 4e. Apollo description

Update Apollo strategy description from "Active at VIX > 25 (retiring)" to
"Retired from routing — manages remaining open positions only (open-position resume
only, no new entries)."

---

## File 5: `iris_production/configs.py`

No code changes in this plan. The DRY_RUN decision (Open Decision 1) determines
whether we add a step here. If Decision 1C is chosen (recommended), the flip is a
one-line change done in a separate commit with a clear commit message:

```python
DRY_RUN = False   # live trading; paper parity confirmed YYYY-MM-DD
```

---

## Transition strategy

### Apollo open-position bridge

Apollo may still have positions from its final live sessions. The plan keeps
`_apollo_trade_open()` and `_run_apollo()` in `leto.py` as a priority-0 check (before
Iris). When Apollo's `apollo_state.csv` shows `idle`, this check returns False and
Leto falls through to Iris.

Once Apollo is confirmed idle (check `apollo_production/data/apollo_state.csv`):

```python
# Remove these in a cleanup commit:
# - _apollo_trade_open() in leto.py
# - _run_apollo() in leto.py
# - APOLLO_DIR constant (if not needed)
# - APOLLO_STATE in slack_listener.py
# - Apollo in reset_all_states() (already removed by this plan)
```

### Rollback

If Iris needs to be pulled from live routing:
1. Revert the three `_run_iris()` call sites in `_route()` back to `_run_apollo()`.
2. Revert `_STRATEGY_SUBTITLE` and the session report block.
3. No state files are affected — Iris state resets independently.

A single `git revert` of the routing commit achieves this without touching Iris's own
code.

---

## Test plan

Run before and after the implementation, showing output:

```bash
python -m pytest tests/
```

Additional checks after implementation:

1. **Import audit** — verify the new wiring:
   ```bash
   # Confirm Iris is importable from IRIS_DIR context
   python -c "import sys; sys.path.insert(0, 'iris_production'); from iris import Iris; print('OK')"

   # Confirm leto.py has no import errors
   python -c "import ast; ast.parse(open('leto.py').read()); print('AST OK')"
   ```

2. **State roundtrip** — `iris_state.csv` status transitions:
   ```bash
   # Manually set status to in_trade, confirm _iris_trade_open() returns True
   # Set to idle, confirm it returns False
   ```

3. **Sizing override read** — create a test `iris_production/data/sizing_override.json`
   with `{"lot_calc": false, "lot_count": 2}`, confirm Iris reads it. Test `lot_calc: true`
   logs a warning and uses `lot_count` anyway.

4. **Session report smoke test** — construct a sample Iris summary dict and call
   `_send_session_report([summary], date.today())` in isolation to confirm formatting.

5. **Circuit breaker** — with `DRY_RUN=True`, write `EXIT` to
   `data/SLACK_COMMAND.flag`, confirm `_check_slack_commands()` sets `_shutdown=True`
   and calls `_execute_exit()`.

---

## Implementation order

1. `iris_production/iris.py` — all changes (1a–1g)
2. `leto.py` — all changes (2a–2e)
3. `slack_listener.py` — all changes (3a–3d)
4. `README.md` — all changes (4a–4e)
5. Run `pytest tests/` — confirm clean
6. Manual smoke-test session in DRY_RUN mode
7. (Separate commit) Flip `DRY_RUN = False` when ready
