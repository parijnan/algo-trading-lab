# Plan: Reactive PE Wing — Athena Production Port

## Context

The always-on 0.05-delta PE safety wing costs −455 pts over 114 trades (−4 pts/trade on average).
A reactive variant — buy only when spot drops 1.75% below `entry_spot`, sell when spot recovers
above `entry_spot` — was backtested and corrected for a 1-bar timing lookahead.

**Post-fix backtest results (1.75%, 124 trades, 2020–2026):**
- Total P&L: +₹179,764 vs baseline +₹149,130 → **+₹30,634 (+21%)**
- Win Rate: 62.9% (baseline 58.9%)
- R:R: 1.66 (baseline 1.65)
- Max Loss: −₹8,489 (baseline −₹8,229 — within range)
- Wing P&L: +16.25 pts (baseline −455 pts always-on)
- Wing transactions: 48 over 124 trades (0.39 avg/trade)

See `athena_backtest/README.md` for full sweep table and sustainability analysis.

---

## Active Trade Safety

There is currently an active trade. It carries `wings_enabled = True` in the state CSV (always-on
wing bought at entry). The two new state fields introduced by this plan default to `False` when
absent from the CSV. The new `_manage_reactive_wing` method is gated on `use_reactive_wing`, which
will be `False` for the active trade. The active trade exits Monday 10:25 and is completely
unaffected by this deployment.

**Deployment can happen any time before Monday 10:30.** Recommended: after the 10:25 exit and
before the 10:30 entry (clean restart with no open position). Deploying before Monday while the
active trade is running is also safe.

---

## Files to Edit

### 1. `athena_production/configs_live.py`

**Two changes:**

```python
# Change:
ENABLE_SAFETY_WINGS = False    # was True — reactive mode: no wing at entry

# Add (after SAFETY_WING_DELTA line):
REACTIVE_WING_PCT   = 1.75     # % of entry_spot below which PE wing is bought
```

`SAFETY_WING_DELTA = 0.05` already exists and is reused for strike selection.

---

### 2. `athena_production/state.py`

**Add two fields to `AthenaState`** after the `running_realised_pl` field:

```python
reactive_wing_active: bool  = False   # True while reactive PE wing is currently held
use_reactive_wing:    bool  = False   # True when this trade uses reactive (not always-on) logic
```

`load_state` already tolerates missing CSV columns (`if f.name not in row.index: continue`),
so the active trade's CSV loads both as `False`. No migration needed.

---

### 3. `athena_production/athena_engine.py`

#### 3a. Import

Add `REACTIVE_WING_PCT` to the import block from `configs_live`.

#### 3b. 11 condition substitutions

Every place that asks "do we have a live PE wing?" must accept either the always-on or the
reactive wing. Pattern: `self.state.wings_enabled` → `self.state.wings_enabled or self.state.reactive_wing_active`.

| Location | Old | New |
|---|---|---|
| `_poll_prices` WebSocket path | `if self.state.wings_enabled` | `if self.state.wings_enabled or self.state.reactive_wing_active` |
| `_poll_prices_rest` REST path | same | same |
| `_reconcile_positions` | `if self.state.wings_enabled and self.state.pe_wing_token` | `if (self.state.wings_enabled or self.state.reactive_wing_active) and self.state.pe_wing_token` |
| `_execute_exit` — add to exit legs | `if self.state.wings_enabled` | `if self.state.wings_enabled or self.state.reactive_wing_active` |
| `_execute_exit` — P&L calc | `if self.state.wings_enabled` | `if self.state.wings_enabled or self.state.reactive_wing_active` |
| `_send_trade_update` — P&L | `if self.state.wings_enabled` | `if self.state.wings_enabled or self.state.reactive_wing_active` |
| `_append_trade_log_row` — P&L | `if self.state.wings_enabled` | `if self.state.wings_enabled or self.state.reactive_wing_active` |
| `_append_trade_log_row` — log fields | `if self.state.wings_enabled` | `if self.state.wings_enabled or self.state.reactive_wing_active` |
| `run()` — subscribe tokens on restart | `if self.state.wings_enabled and self.state.pe_wing_token` | `if (self.state.wings_enabled or self.state.reactive_wing_active) and self.state.pe_wing_token` |
| `run()` — subscribe tokens after entry | `if self.state.wings_enabled and self.state.pe_wing_token` | `if (self.state.wings_enabled or self.state.reactive_wing_active) and self.state.pe_wing_token` |
| Overnight hold P&L snapshot | `if self.state.wings_enabled and p.get('pe_wing')` | `if (self.state.wings_enabled or self.state.reactive_wing_active) and p.get('pe_wing')` |

#### 3c. `_execute_entry` — stamp trade mode

After the existing line `self.state.wings_enabled = ENABLE_SAFETY_WINGS`, add:

```python
self.state.use_reactive_wing = not ENABLE_SAFETY_WINGS
```

This stamps whether the trade uses reactive logic. Persists through restarts.

#### 3d. New method `_manage_reactive_wing`

```python
def _manage_reactive_wing(self, current_spot):
    if not self.state.use_reactive_wing:
        return
    if not self.state.entry_spot:
        return

    trigger_level = self.state.entry_spot * (1.0 - REACTIVE_WING_PCT / 100.0)

    if not self.state.reactive_wing_active:
        if current_spot < trigger_level:
            buy_exp = datetime.strptime(self.state.buy_expiry, '%Y-%m-%d').date()
            vix = self.feed.get_ltp(VIX_TOKEN) or self._get_ltp(EXCHANGE_NSE, 'INDIA VIX', VIX_TOKEN) or 18.0
            stk = self._find_delta_strike(current_spot, vix, buy_exp, SAFETY_WING_DELTA, 'pe')
            if stk:
                sym, tok = self._fetch_symbol_and_token(stk, 'pe', buy_exp)
                if sym:
                    oids = self._place_order('BUY', sym, tok, self.state.lots)
                    fill, q, ft = self._fetch_order_details(oids, tok, sym, self.state.lots)
                    if fill > 0:
                        self.state.reactive_wing_active = True
                        self.state.pe_wing_strike = stk
                        self.state.pe_wing_symbol = sym
                        self.state.pe_wing_token  = tok
                        self.state.pe_wing_entry  = fill
                        save_state(self.state)
                        self.feed.subscribe_options([tok])
                        slack_bot_sendtext(
                            f"🛡️ *Athena WING*: Bought PE {stk} @ {fill:.1f} | "
                            f"spot={current_spot:.0f} trigger={trigger_level:.0f}",
                            SLACK_TRADE_ALERTS)
                    else:
                        logger.warning(f"Reactive wing BUY zero fill at {current_spot:.0f}. Will retry.")
                        slack_bot_sendtext(
                            f"⚠️ *Athena*: Reactive wing BUY zero fill (spot={current_spot:.0f}).",
                            SLACK_ERRORS_CHANNEL)

    elif self.state.reactive_wing_active:
        if current_spot > self.state.entry_spot:
            oids = self._place_order('SELL', self.state.pe_wing_symbol, self.state.pe_wing_token, self.state.lots)
            fill, q, ft = self._fetch_order_details(oids, self.state.pe_wing_token, self.state.pe_wing_symbol, self.state.lots)
            if fill > 0:
                realised = round(fill - self.state.pe_wing_entry, 2)
                self.state.running_realised_pl  += realised
                self.state.reactive_wing_active  = False
                self.state.pe_wing_strike        = None
                self.state.pe_wing_symbol        = None
                self.state.pe_wing_token         = None
                self.state.pe_wing_entry         = 0.0
                save_state(self.state)
                slack_bot_sendtext(
                    f"🛡️ *Athena WING*: Sold PE wing @ {fill:.1f} | "
                    f"pl={realised:+.1f} pts | spot={current_spot:.0f}",
                    SLACK_TRADE_ALERTS)
            else:
                logger.warning(f"Reactive wing SELL zero fill at {current_spot:.0f}. Will retry.")
                slack_bot_sendtext(
                    f"⚠️ *Athena*: Reactive wing SELL zero fill (spot={current_spot:.0f}).",
                    SLACK_ERRORS_CHANNEL)
```

**Key design points:**
- Gated on `use_reactive_wing` — the active trade (always-on wing) is completely skipped.
- Zero-fill on BUY: no state change → retries naturally on next poll cycle (~20s).
- Zero-fill on SELL: no state change → wing stays active, retries on next cycle. `running_realised_pl`
  is not touched until a confirmed fill.
- SELL locks P&L into `running_realised_pl` (same pattern as `_close_emer_if_active`) and clears all
  `pe_wing_*` fields.
- Multiple buy/sell cycles per trade are allowed (matches backtest behaviour — no attempt counter).
  Whipsaw protection is built into the trigger levels: buy at −1.75%, sell at 0%.

#### 3e. Call site in `run()` main loop

In the `if self.state.status == 'in_trade':` block, after `self._manage_emergency_hedge(spot)`:

```python
self._manage_emergency_hedge(spot)
self._manage_reactive_wing(spot)   # no-op if use_reactive_wing is False
```

---

## What Does NOT Change

- CE emergency hedge (`_manage_emergency_hedge`) — identical
- ELM exit (`exit_timestamp` logic) — identical
- Order placement, fill verification, ghost-order recovery — identical
- WebSocket feed, order watcher daemon — identical
- Slack command handling (EXIT, KILL, ATHENA_PARACHUTE) — identical
- `_execute_exit` core leg logic (CE/PE calendar) — identical
- Trade log CSV columns — no new columns; `pe_wing_*` columns already exist and cover the reactive wing
- Backtest files — not touched

---

## Verification Checklist (first live trade after deployment)

1. **Entry Slack message:** net debit does not include `pe_wing_entry` (no wing at entry).
2. **State CSV after entry:** `wings_enabled=False`, `use_reactive_wing=True`, `reactive_wing_active=False`,
   all `pe_wing_*` fields empty.
3. **Wing buy fires:** When spot drops 1.75% below `entry_spot`, Slack shows
   "🛡️ Athena WING: Bought PE …". State: `reactive_wing_active=True`, `pe_wing_*` populated.
4. **Wing sell fires:** When spot recovers above `entry_spot`, Slack shows
   "🛡️ Athena WING: Sold PE wing …". State: `reactive_wing_active=False`, `pe_wing_*` cleared,
   `running_realised_pl` updated.
5. **Exit P&L:** Includes wing P&L — via `running_realised_pl` if sold mid-trade, or via direct
   fill if wing was still held at the 10:25 exit.
6. **Restart while wing is active:** Reconciliation sees `reactive_wing_active=True` → expects
   pe_wing position at broker → no mismatch alert.
7. **Restart while wing is not active:** `reactive_wing_active=False` → pe_wing not in reconciliation
   → no spurious alert.
