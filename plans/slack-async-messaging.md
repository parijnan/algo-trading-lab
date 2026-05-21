# Plan: Fire-and-Forget Slack/Telegram Messaging

## Problem

`slack_bot_sendtext()` and `telegram_bot_sendtext()` are synchronous HTTP calls on the main
trading thread. Both have `timeout=5`. If Slack is slow or unreachable, the main thread
blocks for up to 5 seconds per call.

With Athena and Artemis now running 500ms monitoring loops, a single 5s Slack timeout = 10
missed SL/parachute checks. During a fast-moving market event (the exact moment Slack is
most likely to be slow under load), this is the worst possible time to block.

Affected call sites:
- Trade update messages sent every `TRADE_UPDATE_INTERVAL` / `monitor_frequency`
- SL trigger confirmations (highest urgency)
- Entry/exit order confirmations
- OrderFillWatcher heartbeat alerts
- Error alerts from `handle_exception()`

---

## Proposed Solution

Replace the synchronous call inside `slack_bot_sendtext()` with a queue put. A single
daemon worker thread drains the queue and makes the actual HTTP call. The main thread
returns immediately after enqueuing.

```
Main thread                 Worker thread
────────────────            ─────────────────────────────
slack_bot_sendtext()        while True:
  _queue.put((ch, msg))       channel, msg = _queue.get()
  return                       _send_raw(channel, msg)   # HTTP here
                               _queue.task_done()
```

---

## Design Decisions

### 1. Queue type — bounded vs unbounded

**Choice: bounded, `maxsize=200`**

An unbounded queue grows forever if Slack is down for an extended period. At ~1 message per
500ms monitoring tick, 200 items = ~100 seconds of backlog before dropping. That's enough
to survive a short Slack outage; beyond that the oldest messages are no longer useful anyway.

Drop policy: if the queue is full, `put_nowait()` raises `queue.Full` — catch it, log a
warning via `logger.warning()`, and discard the message. Never block the main thread.

### 2. Telegram fallback location

The fallback (`telegram_bot_sendtext()` when Slack fails) stays inside the worker thread.
The main thread only ever calls `slack_bot_sendtext()` — it queues and returns. The worker
handles retry/fallback logic.

Telegram calls for other purposes (not Slack fallback) are already infrequent and acceptable
as synchronous calls. No change needed for direct `telegram_bot_sendtext()` call sites.

### 3. Single worker per functions.py instance

Each strategy has its own `functions.py` module (apollo, athena, artemis). Each gets its
own `_msg_queue` and `_worker` thread. This is simpler than a cross-process singleton and
matches the existing per-strategy isolation model.

Thread is started at module import time (module-level code), marked `daemon=True` so it
dies with the main process. No explicit shutdown logic needed.

### 4. Message ordering

A single worker thread per module preserves FIFO order. Messages within one strategy
arrive at Slack in the same order they were sent. This matters for trade update sequences.

### 5. Worker crash recovery

The worker runs in an infinite loop. If an unexpected exception escapes (not a network
error — those are caught inside `_send_raw`), the worker dies silently and the queue
fills up. Add an outer `try/except Exception` in the worker loop with a `logger.error()`
and a brief sleep before continuing, so the thread stays alive.

### 6. Behaviour changes visible to the caller

- **Return value**: `slack_bot_sendtext()` currently returns the Slack API response dict.
  After this change it returns `None` immediately. No existing call site uses the return value —
  confirmed by grep (`slack_bot_sendtext(...)` is always called as a statement).
- **Timing**: Messages may arrive at Slack slightly after the code line that called them.
  Under normal conditions this delay is imperceptible (<10ms). Only observable in tests
  that check Slack delivery synchronously — those tests would need a `_queue.join()` call
  to flush before asserting.

### 7. No change to `handle_exception()`

`handle_exception()` calls `slack_bot_sendtext()` which will now be async. This is
intentional — there's no reason an error alert needs to block the thread either. The
logger has already captured the exception before the Slack call.

---

## Files to Change

All three `functions.py` files are identical in structure. Changes are the same in each:

```python
# --- add at top of module ---
import queue

_msg_queue = queue.Queue(maxsize=200)

def _slack_worker():
    while True:
        try:
            channel, msg = _msg_queue.get()
            _send_slack_raw(channel, msg)
            _msg_queue.task_done()
        except Exception as e:
            logger.error(f"SlackWorker unexpected error: {e}")

_slack_worker_thread = threading.Thread(
    target=_slack_worker, daemon=True, name='SlackWorker'
)
_slack_worker_thread.start()

# --- rename current slack_bot_sendtext body to ---
def _send_slack_raw(msg, channel):
    # ... existing HTTP POST logic + Telegram fallback ...

# --- replace slack_bot_sendtext with ---
def slack_bot_sendtext(msg, channel):
    try:
        _msg_queue.put_nowait((channel, msg))
    except queue.Full:
        logger.warning(f"SlackWorker queue full — dropping message to {channel}")
```

`threading` is already imported in all three files (used by `OrderFillWatcher`).

---

## What Does Not Change

- `telegram_bot_sendtext()` signature and call sites — unchanged
- `handle_exception()` — unchanged
- Rate limit counters — unchanged
- All strategy-level call sites — unchanged (fire-and-forget is transparent to callers)
- OrderFillWatcher — has its own WS threads, unrelated

---

## Verification

1. Run Apollo in `DRY_RUN`. Confirm trade update messages arrive on Slack without delay.
   Confirm no regression in WS order fill or LTP feed behaviour (separate threads).
2. Simulate a Slack timeout: set an invalid `slack_token` temporarily. Confirm the main
   loop continues uninterrupted. Confirm `logger.error()` captures the failure. Confirm
   Telegram fallback fires.
3. Queue saturation test: flood `slack_bot_sendtext()` 300 times rapidly. Confirm the
   first 200 are enqueued, the next 100 log a `queue.Full` warning and are discarded.
   Confirm the worker drains without error.

---

## Out of Scope

- Making `telegram_bot_sendtext()` async (low urgency — it's only called from the worker
  or from very infrequent paths like login failure)
- A shared cross-strategy queue (over-engineering — strategies run as separate processes)
- Message persistence across restarts (messages in flight at crash are acceptable to lose)
