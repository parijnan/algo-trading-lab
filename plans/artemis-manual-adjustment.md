# Plan: Slack-Triggered Manual Artemis Adjustment

**Status:** Implemented  
**Files touched:** `slack_listener.py`, `artemis_production/iron_condor.py`

---

## Motivation

Mid-session, there are times when a proactive adjustment is warranted before an SL is
algorithmically triggered (e.g. gap-down risk near close). The adjustment follows the exact
same logic as an algo-triggered SL — exit the stressed side, roll the other side's sell
inward by `adj_dist` (600 pts) with additional hedge lots if before cutoff time.

This feature routes that intent through Slack so the algo's own execution engine handles
the orders and state updates, rather than the user trading manually and then reconstructing
state files.

---

## Design

### No strike override
The new sell strike is always auto-calculated by `adjust_spread()`:
- If `sell_strike - index_ltp > minimum_gap (1000)`: new sell = `sell_strike - adj_dist (600)`
- Else: new sell = `round(index_ltp / 100) * 100 + minimum_gap_iterator (400)`

### Existing infrastructure reused
- `SLACK_COMMAND.flag` — existing flag file, extended with `ADJUST:pe` / `ADJUST:ce`
- `_check_slack_commands()` — extended to parse ADJUST commands
- `pe_spread_result` / `ce_spread_result` — existing result signals, extended with `'manual_sl'`
- `evaluate_handle_sl()` — existing handler, two one-line changes
- `adjust_spread()` — no changes needed

---

## Changes

### `slack_listener.py`
- New button `🔧 Artemis Adjust` in Control Panel
- Modal: radio — PE or CE (no other inputs)
- Handler writes `ADJUST:pe` or `ADJUST:ce` to `SLACK_COMMAND.flag`
- Success → `#tradebot-updates`; failure → `#error-alerts`

### `artemis_production/iron_condor.py`

**`_check_slack_commands()`** — new branch after KILL:
```python
elif command.startswith("ADJUST:"):
    side = command.split(":")[1].lower()
    if side in ('pe', 'ce'):
        self._pending_adjustment = side
        os.remove(flag_path)
        logger.info(f"Manual adjustment queued for {side.upper()} side.")
```
No raise — session continues. Flag deleted immediately on receipt.

**`monitor_trade()`** — at the end, after monitor_spread() has run:
```python
side = getattr(self, '_pending_adjustment', None)
if side is not None:
    self._pending_adjustment = None
    spread = self.pe_spread if side == 'pe' else self.ce_spread
    if spread.spread_status not in ('closed', 'open'):
        if side == 'pe':
            self.pe_spread_result = 'manual_sl'
        else:
            self.ce_spread_result = 'manual_sl'
        logger.info(f"Manual adjustment: overriding {side.upper()} result to manual_sl.")
    else:
        slack_bot_sendtext(
            f"⚠️ *Artemis*: Manual adjustment ignored — {side.upper()} spread is already "
            f"{spread.spread_status}.",
            SLACK_ERRORS_CHANNEL)
```

**`evaluate_handle_sl()`** — add `'manual_sl'` to both conditions:
```python
if self.pe_spread_result in ('index_sl', 'option_sl', 'manual_sl'):
if self.ce_spread_result in ('index_sl', 'option_sl', 'manual_sl'):
```

---

## Control flow

```
User taps 🔧 Artemis Adjust → selects PE → submits
  slack_listener writes "ADJUST:pe" to SLACK_COMMAND.flag
  posts confirmation to #tradebot-updates

Next monitoring cycle (≤ 0.5s):

  _check_slack_commands()
    reads "ADJUST:pe"
    → self._pending_adjustment = 'pe'
    → deletes flag
    → returns (no raise)

  monitor_trade()
    pe_spread.monitor_spread()  → 'continue'
    ce_spread.monitor_spread()  → 'continue'
    [end] _pending_adjustment = 'pe', pe status is active
    → self.pe_spread_result = 'manual_sl'

  evaluate_handle_sl()
    pe_spread_result == 'manual_sl'
    → pe_spread.exit_spread()       closes PE sell + buy
    → _update_trade_book_exit()
    → ce_spread.adjust_spread()     rolls CE sell, adds hedge
    → _update_trade_book_adjustment()
    → _subscribe_active_tokens()
```

---

## Out of scope
- Strike override — not needed; algo calculation is always used
- Adjustment when Artemis is not running — flag persists on disk but is consumed
  on next session start via `_check_slack_commands()`; undesirable, so document
  that this is a live-session-only command
