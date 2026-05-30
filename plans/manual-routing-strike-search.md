# Plan: Manual Routing Override + Binary Search Strike Selection

**Status:** Implemented (`4086386`)  
**Priority:** High — needed before Monday market open  
**Files touched:** `leto.py`, `slack_listener.py`, `artemis_production/credit_spread.py`, `configs_live.py` (new at repo root), `tests/test_strike_search.py` (new)

---

## Task 1 — Manual Routing Override

### Motivation

VIX-based routing is correct most of the time, but can assign the wrong strategy during transitional
events: rising VIX near a budget/event (Artemis zone by number, but Athena is the better trade),
or falling VIX after a conflict resolution (Athena zone by number, but Artemis is right). A manual
switch lets you override Artemis/Athena selection without touching code. Apollo is never overridden
— if VIX > 25, Apollo runs regardless of the switch.

### Routing semantics (verified against existing code)

Priority order in `_route()` is unchanged:

1. **Open position resume** — unconditional, override has no effect.
   - `_apollo_trade_open()` → Apollo
   - `_athena_trade_open()` → Athena
   - `_artemis_trade_open()` → Artemis

2. **Friday block** — unchanged, override has no effect.
   - No open trade + VIX > 25 → Apollo
   - No open trade + VIX ≤ 25 → Stand down
   - (FORCE_ENTRY exception for Athena dry-run remains untouched)

3. **Mon–Thu, no open positions** — override applies here only:
   - `mode = 'manual'` and `vix ≤ 25` → route to `strategy` (artemis or athena)
   - `mode = 'manual'` and `vix > 25` → falls through to Apollo (VIX overrules manual)
   - `mode = 'auto'` → existing 3-way VIX logic unchanged

### Storage: `configs_live.py` at repo root (new file)

```python
from datetime import time

# Market hours
MARKET_OPEN         = time(9, 15)
MARKET_CLOSE        = time(15, 30)

# VIX routing thresholds
VIX_ARTEMIS_MAX     = 16.0
VIX_ATHENA_MAX      = 25.0

# Angel One tokens
NIFTY_INDEX_TOKEN   = "99926000"
VIX_TOKEN           = "99926017"

# Angel One scrip master
SCRIP_MASTER_URL    = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# Slack channel for Leto-level messages
SLACK_CHANNEL       = "#tradebot-updates"

# Routing override — edited by slack_listener.py
ROUTING_MODE        = 'auto'      # 'auto' | 'manual'
MANUAL_STRATEGY     = 'artemis'   # 'artemis' | 'athena'
```

- Consistent with the existing pattern: Athena and Apollo configs are also Python files modified by
  the Slack listener via regex substitution.
- Static constants (`MARKET_OPEN` … `SLACK_CHANNEL`) are imported once at module load in `leto.py`.
- `ROUTING_MODE` / `MANUAL_STRATEGY` are read via `importlib.reload()` inside `_load_route_override()`
  so each reroute-loop iteration picks up any change the Slack listener made mid-session.
- Missing file → silent fallback for routing override; for static constants Leto will raise on import
  (caught in the `except` block of main, reported to Slack and logs).

---

### `leto.py` changes

#### 1. Import static constants from `configs_live`

Replace the `# Constants` block (lines ~74–85 — `MARKET_OPEN`, `MARKET_CLOSE`,
`_SCRIP_MASTER_URL`, `_NIFTY_INDEX_TOKEN`, `_VIX_TOKEN`, `_SLACK_CHANNEL`,
`VIX_ARTEMIS_MAX`, `VIX_ATHENA_MAX`) with a single import at the top of the file:

```python
from configs_live import (
    MARKET_OPEN, MARKET_CLOSE,
    VIX_ARTEMIS_MAX, VIX_ATHENA_MAX,
    NIFTY_INDEX_TOKEN, VIX_TOKEN,
    SCRIP_MASTER_URL,
    SLACK_CHANNEL,
)
```

All downstream usages of the old names are updated:
- `_SLACK_CHANNEL` → `SLACK_CHANNEL`
- `_NIFTY_INDEX_TOKEN` → `NIFTY_INDEX_TOKEN`
- `_VIX_TOKEN` → `VIX_TOKEN`
- `_SCRIP_MASTER_URL` → `SCRIP_MASTER_URL`

#### 2. Add `_load_route_override()`

Insert after `_check_circuit_breaker()`:

```python
def _load_route_override():
    """
    Import configs_live.py from the repo root and return (ROUTING_MODE, MANUAL_STRATEGY).
    Returns ('auto', 'artemis') on any error or missing file.
    """
    import importlib
    try:
        import configs_live as _cfg
        importlib.reload(_cfg)          # fresh read each call (handles reroute loop)
        mode     = _cfg.ROUTING_MODE
        strategy = _cfg.MANUAL_STRATEGY
        if mode not in ('auto', 'manual') or strategy not in ('artemis', 'athena'):
            raise ValueError(f"unexpected values: mode={mode!r}, strategy={strategy!r}")
        return mode, strategy
    except ModuleNotFoundError:
        return 'auto', 'artemis'
    except Exception as e:
        logger.warning(f"configs_live.py unreadable ({e}); defaulting to auto.")
        return 'auto', 'artemis'
```

#### 3. Modify `_route()` — Priority 3 block

Replace:
```python
# Priority 3: Mon–Thu, no open positions — 3-way route
if vix <= VIX_ARTEMIS_MAX:
    ...
elif vix <= VIX_ATHENA_MAX:
    ...
else:
    ...
```

With:
```python
# Priority 3: Mon–Thu, no open positions — manual override or 3-way VIX route
mode, strategy = _load_route_override()
if mode == 'manual' and vix <= VIX_ATHENA_MAX:
    logger.info(f"Manual override active. VIX {vix:.2f}. Routing to {strategy.capitalize()}.")
    _slack(f"*Leto*: ⚙️ Manual override active. Routing to *{strategy.capitalize()}* (VIX {vix:.2f}).")
    if strategy == 'artemis':
        handoff, summary = _run_artemis(obj, auth_token, instrument_df_sensex)
    else:
        handoff, summary = _run_athena(obj, auth_token, instrument_df_nifty)
    return handoff, summary

# Auto routing (also handles manual + VIX > 25 → Apollo)
if vix <= VIX_ARTEMIS_MAX:
    logger.info(f"VIX {vix:.2f} <= {VIX_ARTEMIS_MAX}. Routing to Artemis.")
    _slack(f"*Leto*: VIX {vix:.2f}. Routing to *Artemis*.")
    handoff, summary = _run_artemis(obj, auth_token, instrument_df_sensex)
    return handoff, summary
elif vix <= VIX_ATHENA_MAX:
    logger.info(f"VIX {vix:.2f} in (16, 25]. Routing to Athena.")
    _slack(f"*Leto*: VIX {vix:.2f}. Routing to *Athena*.")
    handoff, summary = _run_athena(obj, auth_token, instrument_df_nifty)
    return handoff, summary
else:
    logger.info(f"VIX {vix:.2f} > {VIX_ATHENA_MAX}. Routing to Apollo.")
    _slack(f"*Leto*: VIX {vix:.2f}. Routing to *Apollo*.")
    _, summary = _run_apollo(obj, auth_token, instrument_df_nifty)
    return False, summary
```

---

### `slack_listener.py` changes

#### 1. Add path constant (after existing constants)

```python
LETO_CONFIG = os.path.join(BASE_DIR, "configs_live.py")
```

#### 2. Add `write_route_override()` helper

Uses the same regex-substitution pattern as the existing `update_python_config()`:

```python
def write_route_override(mode, strategy):
    """Update ROUTING_MODE and MANUAL_STRATEGY in configs_live.py."""
    try:
        with open(LETO_CONFIG, 'r') as f:
            content = f.read()
        content = re.sub(r"(ROUTING_MODE\s*=\s*)'[^']*'",    f"\\g<1>'{mode}'",     content)
        content = re.sub(r"(MANUAL_STRATEGY\s*=\s*)'[^']*'", f"\\g<1>'{strategy}'", content)
        with open(LETO_CONFIG, 'w') as f:
            f.write(content)
        logger.info(f"Route override set: ROUTING_MODE={mode!r}, MANUAL_STRATEGY={strategy!r}")
        return True
    except Exception as e:
        logger.error(f"Failed to update configs_live.py: {e}")
        return False
```

#### 3. New section in `CONTROL_PANEL_BLOCKS`

Add after the existing two `actions` rows:

```python
{
    "type": "divider"
},
{
    "type": "section",
    "text": {"type": "mrkdwn", "text": "*Routing Override:*\nForce strategy selection for next Mon–Thu entry (Apollo always runs if VIX > 25)."}
},
{
    "type": "actions",
    "elements": [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "⚡ Auto (VIX)"},
            "style": "primary",
            "action_id": "btn_route_auto"
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "🔵 Force Artemis"},
            "action_id": "btn_route_artemis"
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "🟢 Force Athena"},
            "action_id": "btn_route_athena"
        }
    ]
},
```

#### 4. Three new action handlers

Follows the position-sizing pattern: multi-line `\n*Field:* value` format for config changes;
success → `_CH`; failure → `_CH_ERRORS` (matching `handle_pos_sizing_submission`).

```python
@app.action("btn_route_auto")
def handle_route_auto(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if write_route_override("auto", "artemis"):
        say(channel=_CH, text=(
            f"⚡ *Routing Override Cleared* by <@{user_id}>\n"
            f"*Mode:* Auto (VIX-based)\n"
            f"_Next entry follows standard VIX routing._"
        ))
    else:
        say(channel=_CH_ERRORS, text=f"❌ *Error*: Failed to clear routing override. Check daemon logs on VPS.")

@app.action("btn_route_artemis")
def handle_route_artemis(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if write_route_override("manual", "artemis"):
        say(channel=_CH, text=(
            f"🔵 *Routing Override Set* by <@{user_id}>\n"
            f"*Mode:* Manual\n"
            f"*Strategy:* Artemis (Sensex IC)\n"
            f"_Apollo routing is unaffected — VIX > 25 always routes to Apollo._"
        ))
    else:
        say(channel=_CH_ERRORS, text=f"❌ *Error*: Failed to set routing override. Check daemon logs on VPS.")

@app.action("btn_route_athena")
def handle_route_athena(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if write_route_override("manual", "athena"):
        say(channel=_CH, text=(
            f"🟢 *Routing Override Set* by <@{user_id}>\n"
            f"*Mode:* Manual\n"
            f"*Strategy:* Athena (Nifty Calendar)\n"
            f"_Apollo routing is unaffected — VIX > 25 always routes to Apollo._"
        ))
    else:
        say(channel=_CH_ERRORS, text=f"❌ *Error*: Failed to set routing override. Check daemon logs on VPS.")
```

No confirmation dialogs — low-risk preference buttons; a mistake is recoverable by tapping Auto before Leto runs.

#### 5. Leto announcement when override fires

Already in the `_route()` change above — when manual override routes a strategy, Leto posts to
`SLACK_CHANNEL` (imported from `configs_live`):

```
⚙️ Manual override active. Routing to *Artemis* (VIX 14.20).
```

This mirrors the auto-routing announcements (`Routing to *Artemis*`, `Routing to *Athena*`, etc.)
and makes it unambiguous in the trade log that a human choice, not VIX, drove the decision.

---

## Task 2 — Binary Search Strike Selection (Artemis)

### Motivation

Current `initialize_spread()` fetches LTP for every strike from ATM to ATM ± `strike_values_iterator`
linearly — 13 calls per leg, 26 per iron condor entry at the 10-call/s poll cap. This wastes calls at
normal VIX (iterating far OTM strikes with near-zero premium) and under-shoots at high VIX (can
exhaust the window before reaching the target premium, silently falling back to the boundary).

Binary search reduces fetches to O(log N) ≈ 4–5 per leg and handles high-VIX range extension
explicitly rather than silently.

### Premium monotonicity

Options are priced monotonically: CE premium **decreases** as strike increases; PE premium
**decreases** as strike decreases. Binary search is valid.

### Algorithm: `_find_sell_strike(start_strike, direction)`

`start_strike`: ATM rounded (floor for PE, ceil for CE)  
`direction`: `+1` for CE, `-1` for PE  
Returns `(sell_strike, sell_symbol, sell_token)`

```
dist_lo = 0                              # ATM end (higher premium)
dist_hi = strike_values_iterator         # OTM end (lower premium)

lo_ltp = fetch(start + direction * 0)                # 1 call
hi_ltp = fetch(start + direction * dist_hi)          # 1 call

# Edge: target >= ATM premium — impossible for fresh entry; use ATM strike
if lo_ltp <= expected_option_premium:
    return start_strike, ...

# Extension (high VIX): double range until target is bracketed or ltp == 0
max_doublings = 3   # caps extension at 8× initial range (9600 pts for Sensex)
doublings = 0
while hi_ltp > expected_option_premium and hi_ltp > 0 and doublings < max_doublings:
    dist_hi *= 2
    hi_ltp = fetch(start + direction * dist_hi)       # 1 call per doubling
    doublings += 1

# Edge: target still beyond range after max extension — use boundary strike
if hi_ltp >= expected_option_premium:
    sell_strike = start + direction * dist_hi
    return sell_strike, ...

# Binary search
while dist_hi - dist_lo > strike_iteration_interval:
    dist_mid = ((dist_lo + dist_hi) // (2 * strike_iteration_interval)) * strike_iteration_interval
    mid_ltp = fetch(start + direction * dist_mid)     # 1 call per iteration
    if mid_ltp > expected_option_premium:
        dist_lo, lo_ltp = dist_mid, mid_ltp
    else:
        dist_hi, hi_ltp = dist_mid, mid_ltp

# Return the bracket end whose ltp is closer to target
lo_strike = start + direction * dist_lo
hi_strike_val = start + direction * dist_hi
if abs(lo_ltp - expected_option_premium) <= abs(hi_ltp - expected_option_premium):
    sell_strike = lo_strike
else:
    sell_strike = hi_strike_val
return sell_strike, symbol, token
```

**Call budget per leg:**
- Normal VIX: 2 (bracket) + 0 (no extension) + ~4 (binary) = **6 calls**
- High VIX (1 extension): 2 + 1 + ~4 = **7 calls**
- High VIX (2 extensions): 2 + 2 + ~5 = **9 calls**
- Current linear: **13 calls always**

**Total per iron condor entry: 12–18 calls vs current 26.**

### Fallback

Any exception inside `_find_sell_strike()` → catch, log warning, fall back to the existing
linear scan (current code, unchanged as a private helper `_find_sell_strike_linear()`).
This means a bug in the binary search cannot break entry.

### `credit_spread.py` changes

1. Extract current linear scan for each leg into `_find_sell_strike_linear(start, direction)` — no logic change, just wrapped.
2. Add `_find_sell_strike(start, direction)` with the binary search above.
3. In `initialize_spread()`, replace the two `for i in range(...)` blocks with calls to `_find_sell_strike()`.
4. Both methods resolve `(sell_strike, sell_symbol, sell_token)` and assign into `self.sell_strike`, `self.sell_symbol`, `self.sell_token`.

### Parity test: `tests/test_strike_search.py`

A standalone script (no live API). Mocks `_fetch_ltp` with a synthetic decreasing curve
`ltp(dist) = 500 * exp(-dist / 2000)` to model realistic premium decay.

Tests three scenarios:
| Scenario | `expected_option_premium` | Expected behaviour |
|---|---|---|
| Normal VIX | 120 | Target mid-range; binary and linear agree |
| High VIX | 35 | Target near/beyond boundary; binary extends, both agree |
| Low VIX | 280 | Target near ATM; both return ATM-adjacent strike |

Assert: `binary_strike == linear_strike` for all three. Assert call count ≤ 9 for binary.

---

## Sequencing

1. Write plan → **await approval** (here)
2. Implement Task 2 (strike search) — self-contained, no external dependencies
3. Run parity test — confirm correctness
4. Implement Task 1 (routing override) — `leto.py` + `slack_listener.py` + create `configs_live.py` at repo root
5. Commit and push

---

## Out of scope

- `FORCE_ENTRY` in Athena's `configs_live.py` is left as-is (separate debug mechanism)
- No changes to Apollo or Athena strategies
- No changes to open-position resume logic or Friday stand-down
