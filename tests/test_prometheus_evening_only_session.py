"""
Evening-only session deferred-start — regression tests.

Context: 2026-09-11 investigation into what Prometheus would do on
2026-09-14 (Ganesh Chaturthi — MCX morning leg closed, evening leg open,
the first such day since Prometheus went live 2026-08-31). Confirmed via
local price history that CRUDEOILM genuinely has zero bars before 17:00 on
every historical morning-closed day (Holi 2026-03-03, Maharashtra Day
2026-05-01, Moharram 2026-06-26) — a real dead zone, not a stale-feed
illusion. Left unfixed, the 09:00-17:00 window would have: hammered
fetch_ltp_rest() every tick via _check_dpl_circuit_hit() (guarded
separately, see test_prometheus_dpl_freeze.py's
test_no_bar_yet_today_skips_ltp_call_entirely), and set off
websocket_feed.py's stale-tick watchdog every ~5min for ~8h.

Fixed by deferring the ENTIRE startup (mcx_evening_only_today() gate in
main(), checked before login) until EVENING_SESSION_OPEN_TIME — no login,
no WS subscribe, no REST polling happens at all during the dead zone, so
none of the above has anything to act on. Also added a Slack message (via
_slack, previously missing) to the pre-existing fully-closed exit path,
per the same review.

mcx_evening_only_today() is tested directly against the real, checked-in
data_pipeline/data/mcx_holidays.csv (not mocked) — it's version-controlled
and its rows are exactly the fixture this feature needs to be correct
against. _seconds_until_time() is a small pure helper factored out of
main() so the trickiest part (clamping to 0 if already past the target
clock time, e.g. a manual restart after 17:00 on an evening-only day)
is unit-testable without invoking main()'s full orchestration.
"""
import importlib.util
import logging
import os
import sys
import unittest
from datetime import date, datetime

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
PROM_DIR = os.path.join(REPO_ROOT, 'prometheus_production')


def _null_logger(name: str) -> logging.Logger:
    lg = logging.getLogger(name)
    lg.handlers = []
    lg.addHandler(logging.NullHandler())
    lg.propagate = False
    return lg


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
    mod._slack = lambda *a, **k: None
    mod.save_state = lambda *a, **k: None
    mod.logger = _null_logger('test_prometheus_evening_only_null')
    return mod


class TestMcxEveningOnlyToday(unittest.TestCase):

    def setUp(self):
        self.mod = _load_prometheus_module()

    def tearDown(self):
        for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
                   'prometheus_logger_setup', 'prometheus'):
            sys.modules.pop(mod, None)
        for d in (PROM_DIR, REPO_ROOT):
            if d in sys.path:
                sys.path.remove(d)

    def test_evening_only_holiday_detected(self):
        """2026-09-14 Ganesh Chaturthi: morning_session_closed=True,
        evening_session_closed=False — the exact case this feature exists
        for."""
        evening_only, reason = self.mod.mcx_evening_only_today(date(2026, 9, 14))
        self.assertTrue(evening_only)
        self.assertEqual(reason, 'Ganesh Chaturthi')

    def test_fully_closed_holiday_is_not_evening_only(self):
        """2026-01-26 Republic Day: both sessions closed — mcx_fully_closed_
        today() owns this case, not mcx_evening_only_today()."""
        evening_only, reason = self.mod.mcx_evening_only_today(date(2026, 1, 26))
        self.assertFalse(evening_only)
        self.assertIsNone(reason)

    def test_evening_closed_only_is_not_evening_only(self):
        """2026-01-01 New Year's Day: morning OPEN, evening closed — the
        mirror case (a genuine normal-hours trading day, per
        mcx_fully_closed_today's own docstring). Must not be flagged."""
        evening_only, reason = self.mod.mcx_evening_only_today(date(2026, 1, 1))
        self.assertFalse(evening_only)
        self.assertIsNone(reason)

    def test_regular_trading_day_is_not_evening_only(self):
        evening_only, reason = self.mod.mcx_evening_only_today(date(2026, 9, 15))
        self.assertFalse(evening_only)
        self.assertIsNone(reason)

    def test_weekend_is_not_evening_only(self):
        """Weekends are mcx_fully_closed_today's territory (unreachable from
        main()'s own call order) -- verify the standalone function still
        answers False rather than crashing on a date with no holiday row."""
        saturday = date(2026, 1, 10)
        self.assertEqual(saturday.weekday(), 5)
        evening_only, reason = self.mod.mcx_evening_only_today(saturday)
        self.assertFalse(evening_only)
        self.assertIsNone(reason)


class TestSecondsUntilTime(unittest.TestCase):

    def setUp(self):
        self.mod = _load_prometheus_module()

    def tearDown(self):
        for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
                   'prometheus_logger_setup', 'prometheus'):
            sys.modules.pop(mod, None)
        for d in (PROM_DIR, REPO_ROOT):
            if d in sys.path:
                sys.path.remove(d)

    def test_positive_gap_before_target(self):
        now = datetime(2026, 9, 14, 9, 0, 0)
        wait = self.mod._seconds_until_time('17:00', now)
        self.assertAlmostEqual(wait, 8 * 3600, delta=1)

    def test_clamped_to_zero_if_already_past_target(self):
        """A manual restart at, say, 18:00 on an evening-only day must
        start immediately, not sleep ~23h until tomorrow's 17:00."""
        now = datetime(2026, 9, 14, 18, 0, 0)
        wait = self.mod._seconds_until_time('17:00', now)
        self.assertEqual(wait, 0.0)

    def test_exactly_at_target_is_zero(self):
        now = datetime(2026, 9, 14, 17, 0, 0)
        wait = self.mod._seconds_until_time('17:00', now)
        self.assertEqual(wait, 0.0)


class TestSecondsUntilEveningOpen(unittest.TestCase):
    """_seconds_until_evening_open() = _seconds_until_time(EVENING_SESSION_
    OPEN_TIME, now) minus EVENING_SESSION_WAKE_BUFFER_MIN worth of early-
    wake margin (2026-09-11 review: waking too early is free -- a few extra
    harmless '0 candles' polls -- waking too late silently loses real
    opening bars, so the buffer only ever pulls the wake time earlier)."""

    def setUp(self):
        self.mod = _load_prometheus_module()

    def tearDown(self):
        for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
                   'prometheus_logger_setup', 'prometheus'):
            sys.modules.pop(mod, None)
        for d in (PROM_DIR, REPO_ROOT):
            if d in sys.path:
                sys.path.remove(d)

    def test_wakes_buffer_minutes_before_open(self):
        now = datetime(2026, 9, 14, 9, 0, 0)
        wait = self.mod._seconds_until_evening_open(now)
        expected = 8 * 3600 - self.mod.EVENING_SESSION_WAKE_BUFFER_MIN * 60
        self.assertAlmostEqual(wait, expected, delta=1)

    def test_clamped_to_zero_inside_the_buffer_window(self):
        """A restart 2 minutes before 17:00 -- inside the 5-min buffer --
        must not compute a negative wait."""
        now = datetime(2026, 9, 14, 16, 58, 0)
        wait = self.mod._seconds_until_evening_open(now)
        self.assertEqual(wait, 0.0)

    def test_clamped_to_zero_after_open(self):
        now = datetime(2026, 9, 14, 18, 0, 0)
        wait = self.mod._seconds_until_evening_open(now)
        self.assertEqual(wait, 0.0)


if __name__ == '__main__':
    unittest.main()
