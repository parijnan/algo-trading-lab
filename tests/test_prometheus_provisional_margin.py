"""
_evaluate_provisional_boundary()'s margin guard -- regression tests for the 2026-09-28 fix.

The guard is meant to hold back a provisional (tick-reconstructed) flip whose close only barely crossed the line it
had to cross, since a tick-derived close and the exchange candle's close could land on opposite sides of a
razor-thin cross. It used to measure the provisional close against THIS bar's supertrend, which on a flip bar has
already switched to the opposite band about 2.5 ATR away, so it passed essentially every flip and gated nothing
(median distance 1.35% on CRUDEOILM flips, 0.74% on SILVERMIC, against a 0.15% threshold). It now measures against the
PREVIOUS bar's supertrend, the line the price had to cross.
"""
import importlib.util
import logging
import os
import sys
import unittest
from datetime import timedelta
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

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
    mod.logger = _null_logger('test_prometheus_provisional_margin_null')
    return mod


def _trending_series(n: int, up: bool) -> pd.DataFrame:
    """A steady trend with small noise, so ST is decisively in that direction at the last bar."""
    rng = np.random.default_rng(7)
    step = 12.0 if up else -12.0
    close = 9000.0 + np.cumsum(step + rng.normal(0, 3, n))
    open_ = np.r_[close[0] - step, close[:-1]]
    high = np.maximum(open_, close) + 4
    low = np.minimum(open_, close) - 4
    ts = pd.date_range('2026-09-01 09:00', periods=n, freq='15min')
    return pd.DataFrame({'time_stamp': ts, 'open': open_, 'high': high, 'low': low, 'close': close,
                         'volume': 100.0})


class TestProvisionalMarginGuard(unittest.TestCase):

    MARGIN = 0.15

    def _setup(self, up: bool, status: str):
        self.mod = _load_prometheus_module()
        m = self.mod
        m.PROVISIONAL_BOUNDARY_ENABLED = True
        m.PROVISIONAL_MARGIN_PCT = self.MARGIN
        p = object.__new__(m.Prometheus)
        p._contract = {'symbol_root': 'CRUDEOILM', 'token': '565900'}
        self.series = _trending_series(120, up=up)
        p._df_15m = self.series
        p._rollover_new_contract = None
        p._pending_flip = None
        p._provisional_disabled_this_session = False
        p._provisional_pending = None
        p.state = m.PrometheusState(status=status, direction=('bullish' if up else 'bearish') if status == 'in_trade' else None)
        p._past_min_entry_guard = MagicMock(return_value=True)
        p._rollover_entry_suppressed = MagicMock(return_value=False)
        p._check_1h_alignment = MagicMock(return_value=True)
        p._execute_entry = MagicMock()
        p._execute_rule7_flip = MagicMock()
        self.p = p
        st = m.compute_st(self.series, m.ST_PERIOD, m.ST_MULTIPLIER)
        self.prev_st = float(st.iloc[-1]['supertrend'])
        self.assertEqual(bool(st.iloc[-1]['trend']), up, 'test series must end in the intended trend')

    def _run(self, up: bool, clear_pct: float):
        """A provisional bar that closes `clear_pct` percent of price beyond the previous supertrend, against the trend."""
        close = self.prev_st * (1 - clear_pct / 100) if up else self.prev_st * (1 + clear_pct / 100)
        prev_close = float(self.series.iloc[-1]['close'])
        window_start = self.series.iloc[-1]['time_stamp'] + timedelta(minutes=15)
        self.p._tick_ohlc_accum = {'open': prev_close, 'high': max(prev_close, close), 'low': min(prev_close, close),
                                   'close': close}
        self.p._evaluate_provisional_boundary(window_start + timedelta(minutes=15), window_start)

    def test_thin_cross_is_held_back_entry(self):
        self._setup(up=True, status='watching')
        self._run(up=True, clear_pct=0.05)          # bearish flip clearing the previous ST by 0.05% < 0.15%
        self.p._execute_entry.assert_not_called()
        self.assertIsNone(self.p._provisional_pending)

    def test_clear_cross_acts_entry(self):
        self._setup(up=True, status='watching')
        self._run(up=True, clear_pct=0.40)
        self.p._execute_entry.assert_called_once()
        self.assertEqual(self.p._execute_entry.call_args[0][0], 'bearish')
        self.assertEqual(self.p._provisional_pending['provisional_direction'], 'bearish')

    def test_thin_cross_is_held_back_flip(self):
        self._setup(up=True, status='in_trade')
        self._run(up=True, clear_pct=0.05)
        self.p._execute_rule7_flip.assert_not_called()

    def test_clear_cross_acts_flip(self):
        self._setup(up=True, status='in_trade')
        self._run(up=True, clear_pct=0.40)
        self.p._execute_rule7_flip.assert_called_once()
        self.assertEqual(self.p._execute_rule7_flip.call_args[0][0], 'bearish')

    def test_bullish_flip_from_a_downtrend(self):
        self._setup(up=False, status='watching')
        self._run(up=False, clear_pct=0.05)
        self.p._execute_entry.assert_not_called()
        self._setup(up=False, status='watching')
        self._run(up=False, clear_pct=0.40)
        self.p._execute_entry.assert_called_once()
        self.assertEqual(self.p._execute_entry.call_args[0][0], 'bullish')

    def test_old_measure_would_have_passed_the_thin_cross(self):
        """The defect this fix removes: against the provisional bar's OWN supertrend, a 0.05% cross looks like a
        multi-percent 'margin'. Recomputed here exactly as the old code did."""
        self._setup(up=True, status='watching')
        m = self.mod
        close = self.prev_st * (1 - 0.05 / 100)
        prev_close = float(self.series.iloc[-1]['close'])
        window_start = self.series.iloc[-1]['time_stamp'] + timedelta(minutes=15)
        prov = pd.DataFrame([{'time_stamp': window_start, 'open': prev_close, 'high': prev_close, 'low': close,
                              'close': close, 'volume': 0}])
        combined = pd.concat([self.series, prov], ignore_index=True)
        row = m.compute_st(combined, m.ST_PERIOD, m.ST_MULTIPLIER).iloc[-1]
        self.assertTrue(bool(row['trend_flip']))
        old_measure = abs(row['close'] - row['supertrend']) / row['close'] * 100
        self.assertGreater(old_measure, self.MARGIN * 2, 'the old measure should have passed with room to spare')
        new_measure = abs(row['close'] - self.prev_st) / row['close'] * 100
        self.assertLess(new_measure, self.MARGIN)

    def test_warmup_previous_bar_is_skipped(self):
        self._setup(up=True, status='watching')
        self.p._df_15m = self.series.iloc[:5]                 # too short for a previous supertrend
        self.p._tick_ohlc_accum = {'open': 9000.0, 'high': 9010.0, 'low': 8990.0, 'close': 9000.0}
        window_start = self.series.iloc[4]['time_stamp'] + timedelta(minutes=15)
        self.p._evaluate_provisional_boundary(window_start + timedelta(minutes=15), window_start)
        self.p._execute_entry.assert_not_called()
        self.p._execute_rule7_flip.assert_not_called()


if __name__ == '__main__':
    unittest.main()
