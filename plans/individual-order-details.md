# Plan: Replace orderBook() scan in _fetch_order_details with individual_order_details()

## Status: PENDING — awaiting live ws_order_test.py run to confirm response field names

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
| Rate limit headroom | 1/sec (shared with other book polls) | 10/sec (dedicated endpoint) |
| `_increment_order_book_poll()` counter | Incremented each poll | Not applicable — remove for this path |
| Sleep between polls | 1s | 0.5s viable |

The `_increment_order_book_poll()` / `_reset_counters()` bookkeeping used by Apollo and
Artemis applies to the `orderBook` endpoint specifically. Calls to
`individual_order_details` do not count against the orderBook rate limit and should not
increment that counter.

---

## Apollo-Specific Notes

Apollo's `_fetch_order_book()` helper wraps `orderBook()` with its own retry loop and
rate-limit counter. After this change, `_fetch_order_details` will no longer call
`_fetch_order_book()`. The helper remains in use for the ghost-recovery path in
`_place_order` (lines ~1053 and ~1088) and should not be removed.

---

## Implementation Steps

1. Confirm response field names and `filledshares` type from live ws_order_test.py output.
2. **Apollo** `_fetch_order_details` (line ~1096): replace `_fetch_order_book()` call and
   book scan loop with `individual_order_details(oid)` calls per orderid. Remove
   `_increment_order_book_poll()` / `_reset_counters()` from this path. Halve sleep to 0.5s.
3. **Artemis** `_fetch_order_details`: same replacement.
4. **Athena** `_fetch_order_details`: same replacement.
5. Verify: grep for `_fetch_order_book` — should only appear in `_place_order`'s
   ghost-recovery path, not in `_fetch_order_details`.

---

## Verification

1. **DRY_RUN**: No change — DRY_RUN path returns early before any API call.
2. **Live smoke test**: Run each strategy in DRY_RUN to confirm no import/syntax errors.
3. **Live fill test**: Observe that `_fetch_order_details` resolves fills correctly with
   the new endpoint. Confirm `filledshares` cast and `averageprice` field work as expected.
