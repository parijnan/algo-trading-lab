"""
§11a — MCX DPL circuit-breaker ladder detection, regression tests.

Context: plans/prometheus-phase3-production.md §11a. A single flat-price
1-min run does NOT reliably separate a real DPL circuit freeze from an
ordinary quiet/thin-liquidity spell (confirmed via a full-history sweep of
local CRUDEOILM data -- many isolated runs with comparable or larger volume
and run-length are clearly not circuit events). What separates cleanly,
zero false positives across the full sweep: a CHAIN of 2+ consecutive
DPL_MIN_STEP_MIN-DPL_MAX_STEP_MIN-minute flat runs starting within
DPL_CHAIN_GAP_MAX_SEC of each other, prices stepping monotonically. This
also surfaced a third, previously-undocumented confirmed instance
(2026-04-08 09:00-09:54), cross-checked against CRUDEOIL at the same
minutes (same identical-timing signature as 2026-03-09).

Detect + Slack-alert ONLY -- zero change to any SL/target/ST/entry/exit
behavior. Uses the same bare-Prometheus-instance + mocked _slack/logger
harness as test_prometheus_market_close.py, for the same reason (avoid
touching the real production log file or sending real Slack messages).
"""
import glob
import importlib.util
import logging
import os
import sys
import unittest
from datetime import datetime, timedelta

import pandas as pd

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
PROM_DIR = os.path.join(REPO_ROOT, 'prometheus_production')
MCX_DATA_DIR = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx', 'CRUDEOILM')


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
    mod.logger = _null_logger('test_prometheus_dpl_null')
    return mod


def _bars(rows):
    """rows: list of (time_str, open, high, low, close, volume)."""
    return pd.DataFrame([{
        'time_stamp': pd.Timestamp(t), 'open': o, 'high': h, 'low': l, 'close': c, 'volume': v,
    } for t, o, h, l, c, v in rows])


def _flat_run(start: str, minutes: int, price: float, vol: int = 10) -> list:
    ts = pd.Timestamp(start)
    return [(ts + timedelta(minutes=i), price, price, price, price, vol) for i in range(minutes)]


class TestFindDplStepRuns(unittest.TestCase):

    def setUp(self):
        self.mod = _load_prometheus_module()
        self.p = object.__new__(self.mod.Prometheus)
        self.p._contract = {'symbol_root': 'CRUDEOILM'}
        self.p._dpl_alerted_chains = set()

    def tearDown(self):
        for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
                   'prometheus_logger_setup', 'prometheus'):
            sys.modules.pop(mod, None)
        for d in (PROM_DIR, REPO_ROOT):
            if d in sys.path:
                sys.path.remove(d)

    def test_flat_run_within_bounds_is_a_candidate_step(self):
        rows = _flat_run('2026-09-10 21:18', 14, 9644.0, vol=50)
        df = _bars(rows)
        now = pd.Timestamp('2026-09-10 21:35')   # well past the run, settled
        runs = self.p._find_dpl_step_runs(df, now)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['run_len'], 14)
        self.assertEqual(runs[0]['price'], 9644.0)

    def test_run_shorter_than_min_step_not_a_candidate(self):
        rows = _flat_run('2026-09-10 21:18', 3, 9644.0, vol=50)   # below DPL_MIN_STEP_MIN=8
        df = _bars(rows)
        now = pd.Timestamp('2026-09-10 21:30')
        runs = self.p._find_dpl_step_runs(df, now)
        self.assertEqual(runs, [])

    def test_run_longer_than_max_step_not_a_candidate(self):
        rows = _flat_run('2026-09-10 21:00', 34, 5622.0, vol=50)   # real noise example from the sweep
        df = _bars(rows)
        now = pd.Timestamp('2026-09-10 22:00')
        runs = self.p._find_dpl_step_runs(df, now)
        self.assertEqual(runs, [])

    def test_zero_volume_bar_breaks_the_run(self):
        """Zero volume distinguishes a feed/connectivity stall from a
        genuine freeze (real trades queuing at the band edge)."""
        rows = _flat_run('2026-09-10 21:18', 5, 9644.0, vol=50) + \
               [(pd.Timestamp('2026-09-10 21:23'), 9644.0, 9644.0, 9644.0, 9644.0, 0)] + \
               _flat_run('2026-09-10 21:24', 5, 9644.0, vol=50)
        df = _bars(rows)
        now = pd.Timestamp('2026-09-10 21:40')
        runs = self.p._find_dpl_step_runs(df, now)
        # Neither side of the zero-volume gap reaches DPL_MIN_STEP_MIN=8 alone.
        self.assertEqual(runs, [])

    def test_unsettled_newest_bars_excluded(self):
        """The newest DPL_SETTLE_LAG_MIN minutes are excluded -- a
        single-tick-so-far minute must not be evaluated as part of a run."""
        rows = _flat_run('2026-09-10 21:18', 10, 9644.0, vol=50)
        df = _bars(rows)
        now = pd.Timestamp('2026-09-10 21:28')   # only 1 min past the run's last bar (21:27) -- unsettled
        runs = self.p._find_dpl_step_runs(df, now)
        # The trailing bars fall inside the settle-lag cutoff and get excluded,
        # shortening the observed run below DPL_MIN_STEP_MIN.
        self.assertTrue(all(r['run_len'] < 10 for r in runs) or runs == [])


class TestCheckDplFreezeChaining(unittest.TestCase):

    def setUp(self):
        self.mod = _load_prometheus_module()
        self.p = object.__new__(self.mod.Prometheus)
        self.p._contract = {'symbol_root': 'CRUDEOILM'}
        self.p._dpl_alerted_chains = set()
        self.alerts = []
        orig_slack = self.mod._slack
        def _capture_slack(msg, channel=None):
            self.alerts.append(msg)
        self.mod._slack = _capture_slack

    def tearDown(self):
        for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
                   'prometheus_logger_setup', 'prometheus'):
            sys.modules.pop(mod, None)
        for d in (PROM_DIR, REPO_ROOT):
            if d in sys.path:
                sys.path.remove(d)

    def _set_now(self, now: pd.Timestamp):
        import prometheus as prom_mod
        # datetime.now() is called inside _check_dpl_freeze via the module's
        # own `datetime` import -- monkeypatch a fixed-now stand-in class.
        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        prom_mod.datetime = _FixedDatetime

    def test_single_isolated_step_does_not_alert(self):
        """A single freeze step (like the real 2026-09-10 21:18 instance)
        deliberately does NOT alert -- calibration found no reliable
        single-instrument signal to separate it from ordinary quiet spells."""
        rows = _flat_run('2026-09-10 21:18', 14, 9644.0, vol=50)
        self.p._df_1m_today = _bars(rows)
        self._set_now(pd.Timestamp('2026-09-10 21:35'))
        self.p._check_dpl_freeze()
        self.assertEqual(self.alerts, [])

    def test_two_step_chain_alerts_once(self):
        rows = (_flat_run('2026-03-09 09:01', 15, 8864.0, vol=50)
                + _flat_run('2026-03-09 09:17', 14, 9115.0, vol=40))
        self.p._df_1m_today = _bars(rows)
        self._set_now(pd.Timestamp('2026-03-09 09:35'))
        self.p._check_dpl_freeze()
        self.assertEqual(len(self.alerts), 1)
        self.assertIn('2 consecutive price-freeze steps', self.alerts[0])

    def test_does_not_realert_same_chain_on_next_tick(self):
        rows = (_flat_run('2026-03-09 09:01', 15, 8864.0, vol=50)
                + _flat_run('2026-03-09 09:17', 14, 9115.0, vol=40))
        self.p._df_1m_today = _bars(rows)
        self._set_now(pd.Timestamp('2026-03-09 09:35'))
        self.p._check_dpl_freeze()
        self.p._check_dpl_freeze()   # same data, called again next tick
        self.assertEqual(len(self.alerts), 1)

    def test_third_step_extends_chain_without_realerting(self):
        rows = (_flat_run('2026-03-09 09:01', 15, 8864.0, vol=50)
                + _flat_run('2026-03-09 09:17', 14, 9115.0, vol=40))
        self.p._df_1m_today = _bars(rows)
        self._set_now(pd.Timestamp('2026-03-09 09:35'))
        self.p._check_dpl_freeze()
        self.assertEqual(len(self.alerts), 1)
        # A third step arrives on a later tick -- same chain (same first-step
        # key), must not fire a second alert.
        rows2 = rows + _flat_run('2026-03-09 09:32', 14, 9366.0, vol=30)
        self.p._df_1m_today = _bars(rows2)
        self._set_now(pd.Timestamp('2026-03-09 09:50'))
        self.p._check_dpl_freeze()
        self.assertEqual(len(self.alerts), 1)

    def test_gap_too_large_does_not_chain(self):
        """Two flat runs separated by more than DPL_CHAIN_GAP_MAX_SEC are
        unrelated events, not a ladder -- must not alert."""
        rows = (_flat_run('2026-09-10 09:01', 10, 8864.0, vol=50)
                + _flat_run('2026-09-10 09:30', 10, 9115.0, vol=40))   # ~19 min gap
        self.p._df_1m_today = _bars(rows)
        self._set_now(pd.Timestamp('2026-09-10 09:45'))
        self.p._check_dpl_freeze()
        self.assertEqual(self.alerts, [])

    def test_disabled_flag_suppresses_alert(self):
        self.mod.DPL_FREEZE_ALERT_ENABLED = False
        rows = (_flat_run('2026-03-09 09:01', 15, 8864.0, vol=50)
                + _flat_run('2026-03-09 09:17', 14, 9115.0, vol=40))
        self.p._df_1m_today = _bars(rows)
        self._set_now(pd.Timestamp('2026-03-09 09:35'))
        self.p._check_dpl_freeze()
        self.assertEqual(self.alerts, [])


@unittest.skipUnless(os.path.isdir(MCX_DATA_DIR) and glob.glob(os.path.join(MCX_DATA_DIR, '*_futures.csv')),
                     'local CRUDEOILM data (gitignored, machine-specific) not present')
class TestRealHistorySweep(unittest.TestCase):
    """Integration check against real local data, not a reimplementation --
    exercises the actual production _find_dpl_step_runs. Skips (does not
    fail) on a fresh checkout with no local data, matching
    test_iris_st_seeding.py's existing convention for gitignored data."""

    def setUp(self):
        self.mod = _load_prometheus_module()
        self.p = object.__new__(self.mod.Prometheus)
        self.p._contract = {'symbol_root': 'CRUDEOILM'}
        self.p._dpl_alerted_chains = set()
        files = sorted(glob.glob(os.path.join(MCX_DATA_DIR, '*_futures.csv')))
        frames = [pd.read_csv(f, parse_dates=['time_stamp']) for f in files]
        self.all_df = (pd.concat(frames, ignore_index=True)
                       .drop_duplicates(subset=['time_stamp']).sort_values('time_stamp').reset_index(drop=True))
        # _merge_1m strips tz before self._df_1m_today ever holds it (dt.tz_localize(None)) --
        # match that here so `now` (tz-naive) compares correctly, same as production.
        self.all_df['time_stamp'] = self.all_df['time_stamp'].dt.tz_localize(None)

    def tearDown(self):
        for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
                   'prometheus_logger_setup', 'prometheus'):
            sys.modules.pop(mod, None)
        for d in (PROM_DIR, REPO_ROOT):
            if d in sys.path:
                sys.path.remove(d)

    def test_catches_2026_03_09_ladder(self):
        window = self.all_df[(self.all_df['time_stamp'] >= '2026-03-09 08:00')
                             & (self.all_df['time_stamp'] <= '2026-03-09 12:00')]
        if window.empty:
            self.skipTest('2026-03-09 not present in local data')
        now = pd.Timestamp('2026-03-09 12:00')
        runs = self.p._find_dpl_step_runs(window, now)
        chain_starts = sorted(r['start'] for r in runs)
        self.assertGreaterEqual(len(runs), 6, 'expected the full 6+ step ladder')

    def test_catches_2026_04_08_ladder(self):
        window = self.all_df[(self.all_df['time_stamp'] >= '2026-04-08 08:00')
                             & (self.all_df['time_stamp'] <= '2026-04-08 11:00')]
        if window.empty:
            self.skipTest('2026-04-08 not present in local data')
        now = pd.Timestamp('2026-04-08 11:00')
        runs = self.p._find_dpl_step_runs(window, now)
        self.assertGreaterEqual(len(runs), 4, 'expected the full 4-step ladder')


if __name__ == '__main__':
    unittest.main(verbosity=2)
