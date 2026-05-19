# Plan: Orphan Fill Cleanup for Apollo and Artemis

**Status: IMPLEMENTED** — Both files were updated after this plan was written.
Verify with: `grep -n "_fetch_order_details\|_cleanup_orphan_fill" apollo_production/apollo.py artemis_production/credit_spread.py`

---

## Context

Athena already detects when one leg of a multi-leg entry fills more than the minimum across all legs and immediately fires a counter-order to square off the excess ("orphan fill cleanup"). Apollo and Artemis have no equivalent. A partial fill on one leg leaves a naked position — unhedged risk that is invisible to the strategy's monitoring loop. This plan ports the same pattern to both strategies.

---

## Core Design Decisions

| Decision | Choice | Reason |
|---|---|---|
| Return type of `_fetch_order_details` | Change to `(avg_price, filled_lots, fill_time)` everywhere | Matches Athena's 3-tuple; filled_lots already computed internally, just not returned |
| Cleanup scope | **Opening legs only** — not closing/exit legs | A partial close means you still hold residual position (different problem, different fix); cleanup only where new exposure is created |
| Cleanup logic | **Per-leg**: `if filled > expected_for_this_leg, close excess` | Artemis has scenarios with different per-leg lot counts — min-across-legs only works when all legs have the same expected count |
| Helper method | `_cleanup_orphan_fill(tx_type, symbol, token, filled_lots, expected_lots)` added to each class | 9 Artemis scenarios; inline would repeat the same 4 lines each time |
| Verify cleanup order | No — discard the order ID list | Consistent with Athena; cleanup is best-effort, adding a verify poll would stall the execution path |
| DRY_RUN in Apollo | Return `(fill, expected_lots, fill_time)` — filled = expected | No real fills in DRY_RUN; cleanup helper becomes a no-op |

---

## Files Modified

- `apollo_production/apollo.py`
- `artemis_production/credit_spread.py`

---

## Apollo Changes

### 1. `_fetch_order_details` — return type (implemented)

- DRY_RUN path: returns `(fill, expected_lots, fill_time)`
- All non-DRY_RUN paths return `(avg_price, filled_lots, fill_time)` or `(0.0, 0, datetime.now())`

### 2. `_cleanup_orphan_fill` helper (implemented)

```python
def _cleanup_orphan_fill(self, tx_type, symbol, token, filled_lots, expected_lots):
    excess_lots = filled_lots - expected_lots
    if excess_lots <= 0:
        return
    counter_tx = 'SELL' if tx_type == 'BUY' else 'BUY'
    logger.warning(...)
    slack_bot_sendtext(...)
    self._place_order(counter_tx, symbol, token, excess_lots)
```

### 3. `_execute_entry` — unpacks + cleanup + confirmed_lots (implemented)

```python
buy_fill, buy_filled_lots, buy_time = self._fetch_order_details(...)
sell_fill, sell_filled_lots, sell_time = self._fetch_order_details(...)
confirmed_lots = min(buy_filled_lots, sell_filled_lots)
self._cleanup_orphan_fill('BUY',  ...)
self._cleanup_orphan_fill('SELL', ...)
self.state.lots = confirmed_lots
```

### 4. `_execute_exit` — unpacks only, no cleanup (implemented)

```python
sell_exit_fill, _sf, _ = self._fetch_order_details(...)
buy_exit_fill,  _bf, _ = self._fetch_order_details(...)
```

---

## Artemis Changes

### Call site summary (all implemented)

| Scenario | Method | Opening legs (cleanup) | Closing legs (unpack only) |
|---|---|---|---|
| 1 | `execute_spread` | buy (total_lots), sell (total_lots) | — |
| 2 | `exit_spread` normal | — | sell_exit, buy_exit |
| 3 | `exit_spread` addl same-token | — | sell_exit, buy_exit |
| 4 | `exit_spread` addl diff-tokens | — | sell_exit, buy_exit, addl_buy_exit |
| 5 | `adjust_spread` post-cutoff | new_sell (lots) | sell_exit |
| 6 | `adjust_spread` pre-cutoff | addl_buy (addl_lots), new_sell (lots+addl_lots) | sell_exit |
| 7 | `adjust_for_elm` adjusted | new_buy (lots) | buy_exit |
| 8 | `adjust_for_elm` adjusted_additional | new_buy (lots-addl_lots) | addl_sell_exit, buy_exit |
| 9 | `adjust_for_elm` active_additional | — | addl_sell_exit, addl_buy |

### `execute_spread` partial-fill lot correction (implemented)

```python
confirmed_total_lots = min(buy_filled_lots, sell_filled_lots)
self._cleanup_orphan_fill('BUY',  ...)
self._cleanup_orphan_fill('SELL', ...)
if confirmed_total_lots < self.lots:
    self.lots = confirmed_total_lots
    self.additional_lots = 0
elif self.additional_flag:
    self.additional_lots = confirmed_total_lots - self.lots
```

---

## Verification

```bash
grep -n "_fetch_order_details" apollo_production/apollo.py artemis_production/credit_spread.py
# Every hit should unpack 3 values.
```
