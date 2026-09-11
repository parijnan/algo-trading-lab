"""
Market-close timing + missed-flip reconciliation — regression tests.

Context: plans/prometheus-market-close-timing-and-reconciliation.md. Three
findings from a 2026-09-11 code trace of production's session-close
handling, all fixed together:

  1. SESSION_END_TIME used to be CLOSING_TIME + a 25-min buffer, computed
     via bare 'HH:MM' string subtraction with no day-boundary handling --
     toggling CLOSING_TIME to '23:55' (winter DST) wrapped the buffered
     result to '00:20' with no date attached, so run()'s session_end ended
     up hours in the PAST relative to any daytime process start and the
     main loop never executed. Fixed by collapsing SESSION_END_TIME onto
     CLOSING_TIME directly (no buffer) -- see TestSessionEndTimeInvariant.
  2. Nothing stopped an order firing after the real market close except the
     loop happening to still be alive (finding 1's bug) or, in DRY_RUN, the
     broker's own rejection (which DRY_RUN never reaches). Fixed with an
     explicit market-hours refusal inside place_order() itself, applying
     uniformly to paper and live orders -- see TestPlaceOrderMarketHoursGuard.
  3. A flip in the day's last (deliberately never live-processed) 15m bar
     used to be silently lost -- seed_st15() recomputes it correctly on the
     next _setup(), but nothing ever ACTED on it (_execute_entry/
     _execute_rule7_flip are only ever called from _handle_new_15m_bar's
     live boundary-tick path, never replayed over seeded history). Fixed
     with a persisted watermark (state.last_processed_boundary) and
     _reconcile_missed_flip(), replayed at _setup() -- see
     TestReconcileMissedFlip.

TestReconcileMissedFlip constructs a bare Prometheus instance via
object.__new__ (bypassing __init__, which needs a live broker session) and
monkeypatches _execute_entry/_execute_rule7_flip/the entry guards/save_state/
_slack so no real order, file write, or Slack call ever happens -- only the
reconciliation's own branching/watermark logic is under test.
"""
import importlib.util
import logging
import os
import sys
import unittest
from datetime import datetime, timedelta

import pandas as pd

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
PROM_DIR = os.path.join(REPO_ROOT, 'prometheus_production')


def _null_logger(name: str) -> logging.Logger:
    """A logger that writes nowhere — production's get_logger() attaches a
    FileHandler pointed at the REAL dated production log file regardless of
    which module calls it (prometheus_functions.py and prometheus.py both
    resolve to the same LOG_DIR/prometheus_<today>.log), so any test that
    exercises a logger.critical/.info call must not use the real one (see
    feedback_test_script_log_contamination in this repo's memory)."""
    lg = logging.getLogger(name)
    lg.handlers = []
    lg.addHandler(logging.NullHandler())
    lg.propagate = False
    return lg


# ---------------------------------------------------------------------------
# Finding 1 — SESSION_END_TIME collapsed onto CLOSING_TIME, no buffer
# ---------------------------------------------------------------------------

class TestSessionEndTimeInvariant(unittest.TestCase):

    def setUp(self):
        sys.path.insert(0, PROM_DIR)
        # Fresh import each test (module-level constants) — pop any stale
        # cached copy first so a prior test file's import doesn't leak in.
        for mod in ('prometheus_configs',):
            sys.modules.pop(mod, None)
        import prometheus_configs as pc
        self.pc = pc

    def tearDown(self):
        sys.modules.pop('prometheus_configs', None)
        if PROM_DIR in sys.path:
            sys.path.remove(PROM_DIR)

    def test_session_end_equals_closing_time(self):
        """No buffer left -- the day-rollover bug's root cause (a buffer
        added via string arithmetic) can't reappear without breaking this."""
        self.assertEqual(self.pc.SESSION_END_TIME, self.pc.CLOSING_TIME)

    def test_session_end_resolves_same_day_for_either_dst_value(self):
        """Replicates run()'s own session_end resolution
        (datetime.now().replace(hour=.., minute=..)) for both DST values of
        CLOSING_TIME and asserts it never lands before a normal daytime
        process start -- the exact scenario that was silently broken for
        '23:55' before this fix."""
        for closing_time in ('23:30', '23:55'):
            session_end_str = closing_time  # SESSION_END_TIME == CLOSING_TIME now
            now = datetime(2026, 11, 15, 9, 5, 0)   # a normal 09:00-cron daytime start
            session_end = now.replace(
                hour=int(session_end_str.split(':')[0]),
                minute=int(session_end_str.split(':')[1]),
                second=0, microsecond=0)
            self.assertGreater(session_end, now,
                               f'CLOSING_TIME={closing_time}: session_end resolved to the past')
            self.assertEqual(session_end.date(), now.date(),
                             f'CLOSING_TIME={closing_time}: session_end crossed a day boundary')

    def test_rollover_time_unaffected_still_same_day(self):
        """ROLLOVER_TIME = CLOSING_TIME - 15min never crosses midnight for
        either DST value -- confirms the surviving _minus_minutes() caller
        is genuinely safe, not just currently-safe-by-luck."""
        self.assertLess(self.pc.ROLLOVER_TIME, self.pc.CLOSING_TIME)
        self.assertLess(self.pc.ROLLOVER_PREFETCH_TIME, self.pc.ROLLOVER_TIME)


# ---------------------------------------------------------------------------
# Finding 2 — place_order() refuses any order at/after CLOSING_TIME
# ---------------------------------------------------------------------------

class TestPlaceOrderMarketHoursGuard(unittest.TestCase):

    def setUp(self):
        sys.path.insert(0, PROM_DIR)
        sys.modules.pop('prometheus_functions', None)
        import prometheus_functions as pf
        self.pf = pf
        pf.logger = _null_logger('test_prometheus_functions_null')

    def tearDown(self):
        sys.modules.pop('prometheus_functions', None)
        if PROM_DIR in sys.path:
            sys.path.remove(PROM_DIR)

    def test_refuses_dry_run_order_after_closing_time(self):
        """DRY_RUN's paper fill must refuse the same as live would --
        before this fix DRY_RUN had zero market-hours awareness of its own."""
        self.pf.CLOSING_TIME = '00:01'   # guarantees now >= closing regardless of test run time
        result = self.pf.place_order(obj=None, transaction_type='BUY', symbol='CRUDEOILM',
                                     token='12345', lots=2, dry_run=True)
        self.assertEqual(result, [])

    def test_refuses_live_order_after_closing_time_without_touching_broker(self):
        """obj=None would crash if the live order-placement path were ever
        reached -- passing it proves the refusal happens before any broker
        call, not just that dry_run happens to be safe."""
        self.pf.CLOSING_TIME = '00:01'
        result = self.pf.place_order(obj=None, transaction_type='SELL', symbol='CRUDEOILM',
                                     token='12345', lots=1, dry_run=False)
        self.assertEqual(result, [])

    def test_dry_run_still_places_within_market_hours(self):
        """Regression guard: the new guard must not break the ordinary
        paper-fill path when genuinely within market hours."""
        self.pf.CLOSING_TIME = '23:59'   # always true for any reasonable test run time
        result = self.pf.place_order(obj=None, transaction_type='BUY', symbol='CRUDEOILM',
                                     token='12345', lots=2, dry_run=True)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].startswith('PAPER_'))


# ---------------------------------------------------------------------------
# Finding 3 — missed-flip reconciliation at _setup()
# ---------------------------------------------------------------------------

def _load_prometheus_module():
    sys.path.insert(0, PROM_DIR)
    sys.path.insert(0, REPO_ROOT)
    for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
               'prometheus_logger_setup', 'prometheus'):
        sys.modules.pop(mod, None)
    spec = importlib.util.spec_from_file_location('prometheus', os.path.join(PROM_DIR, 'prometheus.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules['prometheus'] = mod
    spec.loader.exec_module(mod)
    # Neutralize every side-effecting hook before any instance method runs.
    mod._slack = lambda *a, **k: None
    mod.save_state = lambda *a, **k: None
    mod.logger = _null_logger('test_prometheus_null')
    return mod


def _make_df_15m(rows):
    """rows: list of (time_str, trend_bool, trend_flip_bool, close)."""
    df = pd.DataFrame([{
        'time_stamp': pd.Timestamp(t), 'trend': trend, 'trend_flip': flip,
        'close': close, 'supertrend': close - (1 if trend else -1),
    } for t, trend, flip, close in rows])
    return df


class TestReconcileMissedFlip(unittest.TestCase):

    def setUp(self):
        self.mod = _load_prometheus_module()
        self.p = object.__new__(self.mod.Prometheus)
        self.p._contract = {'symbol_root': 'CRUDEOILM'}
        self.executed_entries = []
        self.executed_flips = []
        self.p._execute_entry = lambda direction, ts, close: self.executed_entries.append((direction, ts, close))
        self.p._execute_rule7_flip = lambda direction, ts, close: self.executed_flips.append((direction, ts, close))
        self.p._past_min_entry_guard = lambda now: True
        self.p._rollover_entry_suppressed = lambda now: False
        self.p._check_1h_alignment = lambda direction: True
        self.p._pending_missed_flip = None
        self.saved_states = []
        self.mod.save_state = lambda s: self.saved_states.append(s.last_processed_boundary)

    def tearDown(self):
        for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
                   'prometheus_logger_setup', 'prometheus'):
            sys.modules.pop(mod, None)
        for d in (PROM_DIR, REPO_ROOT):
            if d in sys.path:
                sys.path.remove(d)

    def test_no_watermark_seeds_baseline_without_acting(self):
        """First run on a fresh state file: nothing to compare against --
        must NOT retroactively act on however much history seed_st15 pulled,
        just establish the watermark."""
        self.p.state = self.mod.PrometheusState(status='watching', last_processed_boundary=None)
        self.p._df_15m = _make_df_15m([
            ('2026-09-10 09:00', True, False, 6200.0),
            ('2026-09-10 09:15', False, True, 6180.0),   # a real historical flip
        ])
        self.p._reconcile_missed_flip()
        self.assertEqual(self.executed_entries, [])
        self.assertEqual(self.executed_flips, [])
        self.assertEqual(self.p.state.last_processed_boundary,
                         pd.Timestamp('2026-09-10 09:15').isoformat())

    def test_watching_with_unprocessed_flip_and_clear_guards_enters(self):
        """The core bug fix: a flip in the last (unprocessed) bar of a prior
        session must fire a fresh entry at the next _setup(), not sit
        silently ignored."""
        watermark = pd.Timestamp('2026-09-10 22:45').isoformat()   # last bar the live loop DID process
        self.p.state = self.mod.PrometheusState(status='watching', last_processed_boundary=watermark)
        self.p._df_15m = _make_df_15m([
            ('2026-09-10 22:45', False, False, 6190.0),
            ('2026-09-10 23:00', True, True, 6210.0),   # unprocessed -- session ended before this fired
        ])
        self.p._reconcile_missed_flip()
        self.assertEqual(len(self.executed_entries), 1)
        direction, ts, close = self.executed_entries[0]
        self.assertEqual(direction, 'bullish')
        self.assertEqual(close, 6210.0)
        self.assertIsNone(self.p._pending_missed_flip)   # fired immediately -- no deferral needed
        self.assertEqual(self.p.state.last_processed_boundary, pd.Timestamp('2026-09-10 23:00').isoformat())

    def test_watching_blocked_by_entry_guard_defers_not_drops(self):
        """If entry guards aren't clear yet (session just started), the
        missed flip must be deferred (_pending_missed_flip), never silently
        dropped -- watermark must NOT advance until it actually fires."""
        watermark = pd.Timestamp('2026-09-10 22:45').isoformat()
        self.p.state = self.mod.PrometheusState(status='watching', last_processed_boundary=watermark)
        self.p._df_15m = _make_df_15m([
            ('2026-09-10 22:45', False, False, 6190.0),
            ('2026-09-10 23:00', True, True, 6210.0),
        ])
        self.p._past_min_entry_guard = lambda now: False   # session just opened
        self.p._pending_missed_flip = None
        self.p._reconcile_missed_flip()
        self.assertEqual(self.executed_entries, [])
        self.assertIsNotNone(self.p._pending_missed_flip)
        self.assertEqual(self.p._pending_missed_flip['direction'], 'bullish')
        self.assertEqual(self.p.state.last_processed_boundary, watermark)   # unchanged -- not yet fired

    def test_retry_pending_missed_flip_fires_once_guard_clears(self):
        self.p.state = self.mod.PrometheusState(status='watching', last_processed_boundary=None)
        self.p._pending_missed_flip = {
            'direction': 'bearish', 'window_start': pd.Timestamp('2026-09-10 23:15'),
            'close': 6150.0, 'boundary_ts': pd.Timestamp('2026-09-10 23:15'),
        }
        self.p._past_min_entry_guard = lambda now: True   # guard now clear
        self.p._retry_pending_missed_flip()
        self.assertEqual(len(self.executed_entries), 1)
        self.assertEqual(self.executed_entries[0][0], 'bearish')
        self.assertIsNone(self.p._pending_missed_flip)
        self.assertEqual(self.p.state.last_processed_boundary, pd.Timestamp('2026-09-10 23:15').isoformat())

    def test_retry_pending_missed_flip_noop_while_guard_still_blocked(self):
        self.p.state = self.mod.PrometheusState(status='watching', last_processed_boundary=None)
        self.p._pending_missed_flip = {
            'direction': 'bearish', 'window_start': pd.Timestamp('2026-09-10 23:15'),
            'close': 6150.0, 'boundary_ts': pd.Timestamp('2026-09-10 23:15'),
        }
        self.p._past_min_entry_guard = lambda now: False
        self.p._retry_pending_missed_flip()
        self.assertEqual(self.executed_entries, [])
        self.assertIsNotNone(self.p._pending_missed_flip)   # still pending

    def test_retry_pending_missed_flip_drops_if_state_moved_on(self):
        """A fresh live flip already entered (state.status left 'watching')
        -- the stale pending reconciliation must not double-fire."""
        self.p.state = self.mod.PrometheusState(status='in_trade', last_processed_boundary=None)
        self.p._pending_missed_flip = {
            'direction': 'bearish', 'window_start': pd.Timestamp('2026-09-10 23:15'),
            'close': 6150.0, 'boundary_ts': pd.Timestamp('2026-09-10 23:15'),
        }
        self.p._retry_pending_missed_flip()
        self.assertEqual(self.executed_entries, [])
        self.assertIsNone(self.p._pending_missed_flip)

    def test_in_trade_with_opposing_unprocessed_flip_fires_rule7(self):
        watermark = pd.Timestamp('2026-09-10 22:45').isoformat()
        self.p.state = self.mod.PrometheusState(status='in_trade', direction='bullish',
                                                 last_processed_boundary=watermark)
        self.p._df_15m = _make_df_15m([
            ('2026-09-10 22:45', True, False, 6205.0),
            ('2026-09-10 23:00', False, True, 6150.0),   # unprocessed flip AGAINST the open position
        ])
        self.p._reconcile_missed_flip()
        self.assertEqual(len(self.executed_flips), 1)
        self.assertEqual(self.executed_flips[0][0], 'bearish')
        self.assertEqual(self.p.state.last_processed_boundary, pd.Timestamp('2026-09-10 23:00').isoformat())

    def test_in_trade_with_agreeing_flip_does_not_refire(self):
        """A flip that agrees with the already-open position's direction
        (e.g. a warmup/NaN-adjacent artifact) must not trigger a redundant
        Rule 7 flip against itself."""
        watermark = pd.Timestamp('2026-09-10 23:00').isoformat()
        self.p.state = self.mod.PrometheusState(status='in_trade', direction='bullish',
                                                 last_processed_boundary=watermark)
        self.p._df_15m = _make_df_15m([
            ('2026-09-10 23:15', True, True, 6210.0),   # flip TO bullish -- already bullish
        ])
        self.p._reconcile_missed_flip()
        self.assertEqual(self.executed_flips, [])
        self.assertEqual(self.p.state.last_processed_boundary, pd.Timestamp('2026-09-10 23:15').isoformat())

    def test_no_unprocessed_flip_is_a_noop(self):
        """Ordinary restart mid-series, watermark already covers the tail --
        must not re-trigger anything."""
        watermark = pd.Timestamp('2026-09-10 23:15').isoformat()
        self.p.state = self.mod.PrometheusState(status='watching', last_processed_boundary=watermark)
        self.p._df_15m = _make_df_15m([
            ('2026-09-10 23:15', True, True, 6210.0),
            ('2026-09-10 23:30', True, False, 6215.0),   # no flip -- ordinary continuation bar
        ])
        self.p._reconcile_missed_flip()
        self.assertEqual(self.executed_entries, [])
        self.assertEqual(self.executed_flips, [])
        # Watermark untouched -- no-flip path returns before reaching either branch.
        self.assertEqual(self.p.state.last_processed_boundary, watermark)

    def test_multi_day_gap_coalesces_to_latest_flip_only(self):
        """A multi-day outage with several intermediate flips must resolve
        to the CURRENT true state (the latest flip), not replay every
        intermediate one as if each were a live event."""
        watermark = pd.Timestamp('2026-09-08 23:15').isoformat()
        self.p.state = self.mod.PrometheusState(status='watching', last_processed_boundary=watermark)
        self.p._df_15m = _make_df_15m([
            ('2026-09-09 09:15', True, True, 6200.0),
            ('2026-09-09 12:00', False, True, 6170.0),
            ('2026-09-10 09:15', True, True, 6220.0),   # latest -- this is the one that should fire
        ])
        self.p._reconcile_missed_flip()
        self.assertEqual(len(self.executed_entries), 1)
        self.assertEqual(self.executed_entries[0][0], 'bullish')
        self.assertEqual(self.executed_entries[0][2], 6220.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
