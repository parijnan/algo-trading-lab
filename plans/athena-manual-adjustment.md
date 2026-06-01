# Plan: Slack-Triggered Manual Athena Adjustment (CE Parachute)

**Status:** Implemented  
**Files touched:** `slack_listener.py`, `athena_production/athena_engine.py`

---

## Motivation

The CE Parachute (emergency hedge) in Athena is currently auto-triggered when spot
crosses `ce_sell_strike + EMERGENCY_TRIGGER_OFFSET`, and auto-closed when spot drops
back below `ce_sell_strike + EMERGENCY_EXIT_OFFSET`. There are times when a manual
override is preferable — entering the parachute earlier than the trigger, or closing
it when the threat has visually abated even though spot hasn't crossed the exit level.

---

## Slack UI changes

### Section rename
`CONTROL_PANEL_BLOCKS` section heading changes from:
> *Artemis Manual Adjustment:* ...

to:
> *Manual Adjustment:* Trigger mid-session adjustments for Artemis or Athena. Executed via the algo's own order engine using the same logic as an automatic trigger.

### New button: 🪂 Adjust Athena
Added alongside the existing 🔧 Adjust Artemis button.

### New modal: `view_athena_adjust`
Radio buttons — two options:
- `enter` → "Enter CE Parachute — buy OTM CE hedge (delta-targeted, same logic as auto-trigger)"
- `exit`  → "Exit CE Parachute — close the active CE hedge position"

Handler writes `ATHENA_PARACHUTE:enter` or `ATHENA_PARACHUTE:exit` to
`SLACK_COMMAND.flag`. Same error/success messaging pattern as Artemis adjust.

---

## `athena_engine.py` changes

### 1. `_check_slack_commands()` — new branch

After the KILL branch, before the DISABLE comment:

```python
elif command.startswith("ATHENA_PARACHUTE:"):
    action = command.split(":")[1].lower()
    if action in ('enter', 'exit'):
        self._pending_parachute = action
        os.remove(flag_path)
        logger.info(f"Manual parachute action queued: {action}.")
```

No raise — session continues. Flag deleted immediately.

### 2. `_manage_emergency_hedge()` — add `force=False` parameter

```python
def _manage_emergency_hedge(self, current_spot, force=False):
    if not ENABLE_EMERGENCY_HEDGE: return
    if datetime.now().time() < time(9, 16): return
    if not self.state.emer_active and (force or self.state.emer_attempts < EMERGENCY_MAX_ATTEMPTS):
        if force or current_spot >= (self.state.ce_sell_strike + EMERGENCY_TRIGGER_OFFSET):
            # existing entry code unchanged
            ...
    elif self.state.emer_active:
        if force or current_spot <= (self.state.ce_sell_strike + EMERGENCY_EXIT_OFFSET):
            self._close_emer_if_active()
```

`force=True` bypasses both the spot-level condition checks and the `emer_attempts`
cap on entry. Exit via `force=True` bypasses the spot exit condition and calls
`_close_emer_if_active()` directly. All order placement, state updates, and Slack
messaging inside the method are unchanged.

### 3. Monitoring loop — check pending action after `_manage_emergency_hedge()`

In the `if self.state.status == 'in_trade':` block (line 894), immediately after
the call to `self._manage_emergency_hedge(spot)`:

```python
pending = getattr(self, '_pending_parachute', None)
if pending is not None:
    self._pending_parachute = None
    if pending == 'enter':
        if self.state.emer_active:
            slack_bot_sendtext(
                "⚠️ *Athena*: Manual parachute entry ignored — parachute already active.",
                SLACK_ERRORS_CHANNEL)
        else:
            self._manage_emergency_hedge(spot, force=True)
    elif pending == 'exit':
        if not self.state.emer_active:
            slack_bot_sendtext(
                "⚠️ *Athena*: Manual parachute exit ignored — no active parachute.",
                SLACK_ERRORS_CHANNEL)
        else:
            self._close_emer_if_active()
```

Checked **after** `_manage_emergency_hedge()` so the auto-trigger runs first. If the
auto-trigger already entered the parachute in the same cycle, the manual enter guard
(`emer_active` check) fires a warning instead of double-buying.

---

## Flag command summary

| Flag value | Strategy | Action |
|---|---|---|
| `ADJUST:pe` | Artemis | Exit PE, adjust CE (existing) |
| `ADJUST:ce` | Artemis | Exit CE, adjust PE (existing) |
| `ATHENA_PARACHUTE:enter` | Athena | Force-enter CE parachute |
| `ATHENA_PARACHUTE:exit` | Athena | Force-exit CE parachute |

All four commands delete the flag immediately on receipt and set a pending action on
`self`, consumed in the same monitoring cycle. `SLACK_COMMAND.flag` continues to hold
exactly one command at a time.

---

## Safety guards

| Scenario | Behaviour |
|---|---|
| Enter triggered, parachute already active | Warning to `#error-alerts`, no order |
| Exit triggered, no active parachute | Warning to `#error-alerts`, no order |
| Either triggered, Athena not in `in_trade` | Pending action consumed but not executed; warning to `#error-alerts` |
| Either triggered, Athena not running | Flag sits on disk; consumed on next Athena session start via `_check_slack_commands()` — same caveat as Artemis |
| `ENABLE_EMERGENCY_HEDGE = False` in configs | Force-enter path still gated by `if not ENABLE_EMERGENCY_HEDGE: return` — won't fire |

---

## Control flow walkthrough

**You're mid-session, Nifty approaching CE sell strike. You tap 🪂 Adjust Athena →
select "Enter CE Parachute" → submit.**

```
slack_listener writes "ATHENA_PARACHUTE:enter" to SLACK_COMMAND.flag
posts confirmation to #tradebot-updates

Next monitoring cycle:

  _check_slack_commands()
    reads "ATHENA_PARACHUTE:enter"
    → self._pending_parachute = 'enter'
    → deletes flag
    → returns normally

  [loop: status == 'in_trade']
    _manage_emergency_hedge(spot)   ← auto-trigger check, probably no-ops
    
    pending == 'enter', emer_active == False
    → _manage_emergency_hedge(spot, force=True)
        bypasses spot condition check
        bypasses emer_attempts cap
        computes delta-targeted strike via _find_delta_strike()
        places BUY order
        on fill: sets emer_active=True, saves state
        posts "🪂 Athena EMERGENCY: Bought Parachute CE XXXXX @ YY.Y"
        to #trade-alerts
```

---

## Out of scope
- Manual parachute strike override — not needed; same delta-targeting logic as auto
- Apollo — no mid-trade adjustments exist
