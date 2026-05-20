# Plan: Replace orderBook() scan in _fetch_order_details with individual_order_details()

## Status: BLOCKED — individual_order_details() non-functional on this account

Live testing on 2026-05-20 confirmed that `individual_order_details(orderid)` returns
`AB1007: Order not found` for every call — immediately after fill and 30 seconds later —
for both NFO (Nifty) and BFO (Sensex) orders that are confirmed complete via WebSocket
(AB05). The endpoint is non-functional on this account, likely an API plan restriction.

`orderBook()` remains in use for fill verification. This plan is shelved until Angel One
confirms the endpoint is accessible or the account tier changes.

---

## Context

All three production strategies verify fills by calling `_fetch_order_details`, which
polls the full `orderBook()` endpoint and scans every order in the day's book to find
matching order IDs. This is wasteful and slow:

- `orderBook()` rate limit: **1/sec**
- `orderBook()` returns all orders for the day — O(N) scan per poll
- Polling loop runs up to 10s with 1s sleep between each full-book fetch

`individual_order_details(orderid)` is a targeted GET to
`/rest/secure/angelbroking/order/v1/details/{orderid}` with a rate limit of **10/sec**.
It returns a single order's data directly. No scan needed.

---

## Scope

**Replace:** The inner orderBook scan inside `_fetch_order_details` in all three strategies.

**Do NOT replace:** The `orderBook()` call inside `_place_order`'s ghost-recovery path.
Ghost recovery fires when `placeOrder` raises a `DataException`/`NetworkException` and
we don't yet have a confirmed orderid — `individual_order_details` requires an orderid,
so the full book scan is the only viable path there.

---

## Files to Modify

- `apollo_production/apollo.py`
- `artemis_production/credit_spread.py`
- `athena_production/athena_engine.py`

---

## Current Flow (all three strategies)

```
_fetch_order_details(orderid_list, ...)
  loop until filled / terminal / 10s timeout:
    _fetch_order_book()          ← orderBook() — 1/sec rate limit
    scan entire book for each oid in orderid_list
    accumulate filledshares, averageprice
    check terminal status
    sleep(1)
```

---

## Proposed Flow

```
_fetch_order_details(orderid_list, ...)
  loop until filled / terminal / 10s timeout:
    for oid in orderid_list:
      data = obj.individual_order_details(oid)   ← targeted GET — 10/sec
      accumulate filledshares, averageprice
      check terminal status
    sleep(0.5)   ← can halve the sleep since rate limit is no longer the bottleneck
```

One call per orderid per poll. For a normal 1-lot entry (1 orderid per leg), this is a
single call. For chunked orders (multiple orderids per leg due to qty freeze limits),
it's N calls where N is the number of chunks — typically 1-2 in practice.

---

## Response Field Mapping

**⚠️ To be confirmed by live ws_order_test.py run.**

Expected (based on orderBook field names, which the endpoint should mirror):

| Field | Type | Notes |
|---|---|---|
| `status` | str | `"complete"`, `"open"`, `"rejected"`, `"cancelled"` |
| `orderstatus` | str | Same value, different key — check which one to use |
| `filledshares` | str | Must cast: `int(data['filledshares'])` |
| `averageprice` | float | Already float — no cast needed |
| `updatetime` | str | Format: `'%d-%b-%Y %H:%M:%S'` — confirm against live output |
| `orderid` | str | Echo of the requested ID |

---

## Rate Limit Impact

| Metric | Before | After |
|---|---|---|
| Calls per fill verification (1 orderid) | 1 `orderBook()` per poll | 1 `individual_order_details()` per poll |
| Rate limit headroom | 1/sec (shared with other book polls) | 10/sec (same as order placement) |
| `_increment_order_book_poll()` counter | Incremented each poll | Removed from this path |
| New counter | — | `_increment_individual_order_poll()` per call |
| Sleep between polls | 1s | 0.5s viable |

`individual_order_details` has the same rate limit as order placement (10/sec). Since the
fill-verification loop makes only 1–2 calls per 0.5s poll cycle, it will never come close
to the limit — no additional delays from the counter.

`_increment_order_book_poll()` / `_reset_counters()` applies to the `orderBook` endpoint
only and must be removed from the `_fetch_order_details` path. Each call to
`individual_order_details` in the polling loop must call the new counter instead.

---

## Counter Infrastructure Changes

### Apollo and Athena (`configs_live.py` + `functions.py`)

`configs_live.py` already defines `ORDER_BOOK_POLL_LIMIT`, `LTP_POLL_LIMIT`, etc. as
named constants, which are imported by `functions.py`.

Add to each `configs_live.py`:
```python
INDIVIDUAL_ORDER_POLL_LIMIT = 10
```

Add to each `functions.py`:
- Import `INDIVIDUAL_ORDER_POLL_LIMIT` from `configs_live`
- Add `'individual_order': {'count': 0, 'limit': INDIVIDUAL_ORDER_POLL_LIMIT, 'last_reset': 0}` to `_counters`
- Add `def _increment_individual_order_poll(): _check_limit('individual_order')`

### Artemis (`configs.py` + `functions.py`)

Artemis `functions.py` currently hardcodes the limits directly in `_counters` (2, 1, 10,
10). These should be named constants in `configs.py` (not CSV-sourced — these are API
rate limits, not trade parameters).

Add to `configs.py`:
```python
RMS_POLL_LIMIT               = 2
ORDER_BOOK_POLL_LIMIT        = 1
LTP_POLL_LIMIT               = 10
ORDER_LIMIT                  = 10
INDIVIDUAL_ORDER_POLL_LIMIT  = 10
```

Update `functions.py` to import all five constants from `configs`, and replace the
hardcoded values in `_counters` with the imported names. Add the `'individual_order'`
bucket and `increment_individual_order_poll()` wrapper.

---

## Apollo-Specific Notes

Apollo's `_fetch_order_book()` helper wraps `orderBook()` with its own retry loop and
rate-limit counter. After this change, `_fetch_order_details` will no longer call
`_fetch_order_book()`. The helper remains in use for the ghost-recovery path in
`_place_order` (lines ~1053 and ~1088) and should not be removed.

---

## Implementation Steps

1. Confirm response field names and `filledshares` type from live ws_order_test.py output.

**Counter infrastructure (do before touching `_fetch_order_details`):**

2. **Apollo `configs_live.py`**: add `INDIVIDUAL_ORDER_POLL_LIMIT = 10`.
3. **Apollo `functions.py`**: import the new constant; add `'individual_order'` bucket to
   `_counters`; add `_increment_individual_order_poll()` wrapper.
4. **Athena `configs_live.py`**: same as step 2.
5. **Athena `functions.py`**: same as step 3.
6. **Artemis `configs.py`**: add all five rate limit constants (`RMS_POLL_LIMIT`,
   `ORDER_BOOK_POLL_LIMIT`, `LTP_POLL_LIMIT`, `ORDER_LIMIT`,
   `INDIVIDUAL_ORDER_POLL_LIMIT`).
7. **Artemis `functions.py`**: import all five constants; replace hardcoded values in
   `_counters` with the imported names; add `'individual_order'` bucket and
   `increment_individual_order_poll()` wrapper.

**`_fetch_order_details` replacement:**

8. **Apollo** (line ~1096): replace `_fetch_order_book()` call and book scan with
   `individual_order_details(oid)` per orderid. Call
   `_increment_individual_order_poll()` per call. Remove `_increment_order_book_poll()`
   from this path. Halve sleep to 0.5s.
9. **Artemis** `_fetch_order_details`: same replacement.
10. **Athena** `_fetch_order_details`: same replacement.
11. Verify: grep for `_fetch_order_book` — should only appear in `_place_order`'s
    ghost-recovery path, not in `_fetch_order_details`.

---

## Verification

1. **DRY_RUN**: No change — DRY_RUN path returns early before any API call.
2. **Live smoke test**: Run each strategy in DRY_RUN to confirm no import/syntax errors.
3. **Live fill test**: Observe that `_fetch_order_details` resolves fills correctly with
   the new endpoint. Confirm `filledshares` cast and `averageprice` field work as expected.
