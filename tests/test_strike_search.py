"""
Parity test — CreditSpread._find_sell_strike vs _find_sell_strike_linear.

Mocks _fetch_ltp with a synthetic monotone premium curve so no live API is needed.

Premium model: ltp(dist) = 500 * exp(-dist / 200)
  dist = OTM distance from start_strike in points.
  ltp(0)=500, ltp(300)≈111, ltp(600)≈25, ltp(1200)≈1.2

Three scenarios tested:
  Normal VIX  — target=120, comfortably within initial 1200pt range
  Low VIX     — target=280, near ATM end of range
  High VIX    — target=35, within range but near far boundary
  Extension   — strike_values_iterator patched to 400; target=35 falls outside
                 that range, forcing binary to double the bracket once

Assertions per scenario:
  1. binary_strike == linear_strike (same answer)  [all except extension]
  2. binary uses fewer LTP calls than linear        [normal and low VIX]
  3. binary_ltp is closer to target than linear_ltp [extension only]
"""

import logging
import math
import os
import sys
import types
import unittest

REPO_ROOT   = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
ARTEMIS_DIR = os.path.join(REPO_ROOT, 'artemis_production')

# ---------------------------------------------------------------------------
# Inject module mocks before importing credit_spread
# ---------------------------------------------------------------------------

STRIKE_STEP   = 100
INITIAL_RANGE = 1200
HEDGE         = 1000
FO_SEG        = 'BFO'
PEAK          = 500.0
DECAY         = 200.0   # ltp(0)=500, ltp(300)≈111, ltp(600)≈25, ltp(1200)≈1.2

_m_conf = types.ModuleType('configs')
for _attr in (
    'contracts_df', 'qty_freeze', 'lot_size', 'lot_count', 'lot_capital',
    'adjustment_distance', 'instrument', 'underlying_token', 'exchange_segment',
    'minimum_gap', 'minimum_gap_iterator', 'index_sl_offset',
    'sl_4_dte', 'sl_3_dte', 'sl_2_dte', 'sl_1_dte', 'sl_0_dte',
    'ORDER_TIMEOUT_SEC', 'SLACK_TRADE_ALERTS', 'SLACK_ERRORS_CHANNEL',
):
    setattr(_m_conf, _attr, 0)
_m_conf.pd                        = None
_m_conf.strike_iteration_interval = STRIKE_STEP
_m_conf.strike_values_iterator    = INITIAL_RANGE
_m_conf.expected_option_premium   = 120     # default; overridden per test
_m_conf.hedge_points              = HEDGE
_m_conf.fo_exchange_segment       = FO_SEG
_m_conf.LOG_LEVEL                 = 'DEBUG'
_m_conf.LOGS_DIR                  = '/tmp'
_m_conf.lot_size                  = 15
_m_conf.lot_count                 = 1
sys.modules['configs'] = _m_conf

_m_log = types.ModuleType('logger_setup')
_m_log.get_logger = logging.getLogger
sys.modules['logger_setup'] = _m_log

_m_func = types.ModuleType('functions')
for _name in (
    'slack_bot_sendtext', 'sleep', 'exists', 'handle_exception',
    'increment_poll_counter', 'increment_order_counter',
    'increment_order_book_poll', 'reset_counters',
):
    setattr(_m_func, _name, lambda *a, **kw: None)
sys.modules['functions'] = _m_func

if ARTEMIS_DIR not in sys.path:
    sys.path.insert(0, ARTEMIS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from credit_spread import CreditSpread  # noqa: E402
import credit_spread as _cs_module      # noqa: E402 — for per-test rebinding

# credit_spread.py freezes these at import time via `from configs import ...`.
# Our mock sets them to 0 (satisfying the import), but the cached module would
# then contaminate test_artemis_strike_math.py which expects production values.
# Rebind here so any subsequent test file that reuses the cached module sees
# the correct values; these are irrelevant to strike-search correctness.
_cs_module.sl_4_dte       = 2.0
_cs_module.sl_3_dte       = 1.8
_cs_module.sl_2_dte       = 1.5
_cs_module.sl_1_dte       = 1.2
_cs_module.sl_0_dte       = 1.0
_cs_module.index_sl_offset = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def synthetic_ltp(dist: int) -> float:
    return PEAK * math.exp(-dist / DECAY)


def _make_spread(target_premium: float, start_strike: int) -> CreditSpread:
    """
    Minimal CreditSpread with mocked LTP and symbol lookup.
    Token encodes the raw strike value; _fetch_ltp derives dist from |strike - start_strike|.
    Also rebinds expected_option_premium in credit_spread's module namespace.
    """
    _cs_module.expected_option_premium = target_premium

    cs = object.__new__(CreditSpread)
    cs._call_count = 0

    def _fetch_ltp_mock(exchange, symbol, token):
        cs._call_count += 1
        dist = abs(int(token) - start_strike)
        return synthetic_ltp(dist)

    def _fetch_sym_tok_mock(strike):
        return f'SYM{strike}', str(strike)

    cs._fetch_ltp = _fetch_ltp_mock
    cs._fetch_symbol_and_token = _fetch_sym_tok_mock
    cs.feed = None
    return cs


def run_linear(target: float, start: int, direction: int):
    cs = _make_spread(target, start)
    strike, _, _ = cs._find_sell_strike_linear(start, direction)
    return strike, cs._call_count


def run_binary(target: float, start: int, direction: int):
    cs = _make_spread(target, start)
    strike, _, _ = cs._find_sell_strike(start, direction)
    return strike, cs._call_count


START_CE = 80100
START_PE = 80000
LINEAR_CALLS = INITIAL_RANGE // STRIKE_STEP   # 12 calls


# ---------------------------------------------------------------------------
# CE direction tests
# ---------------------------------------------------------------------------

class TestStrikeSearchCE(unittest.TestCase):

    def _run(self, target):
        lin_s, lin_n = run_linear(target, START_CE, +1)
        bin_s, bin_n = run_binary(target, START_CE, +1)
        return lin_s, lin_n, bin_s, bin_n

    # Normal VIX: target=120, crossover near dist=300 (ltp≈111)
    def test_normal_vix_same_strike(self):
        lin_s, _, bin_s, _ = self._run(120)
        self.assertEqual(bin_s, lin_s)

    def test_normal_vix_fewer_calls(self):
        _, lin_n, _, bin_n = self._run(120)
        self.assertLess(bin_n, lin_n)

    def test_normal_vix_call_budget(self):
        _, _, _, bin_n = self._run(120)
        self.assertLessEqual(bin_n, 9)

    # Low VIX: target=280, crossover near dist=100 (ltp≈303)
    def test_low_vix_same_strike(self):
        lin_s, _, bin_s, _ = self._run(280)
        self.assertEqual(bin_s, lin_s)

    def test_low_vix_fewer_calls(self):
        _, lin_n, _, bin_n = self._run(280)
        self.assertLess(bin_n, lin_n)

    # High VIX (within range): target=35, crossover near dist=500 (ltp≈41)
    def test_high_vix_within_range_same_strike(self):
        lin_s, _, bin_s, _ = self._run(35)
        self.assertEqual(bin_s, lin_s)

    # Extension: reduce strike_values_iterator to 400; target=35 lies outside that window.
    # Binary doubles to 800 and finds a closer strike; linear is stuck at 4-strike boundary.
    def test_extension_binary_closer_to_target(self):
        original = _cs_module.strike_values_iterator
        try:
            _cs_module.strike_values_iterator = 400
            lin_s, _, bin_s, _ = (run_linear(35, START_CE, +1)[0], 0,
                                  run_binary(35, START_CE, +1)[0], 0)
            lin_ltp = synthetic_ltp(abs(lin_s - START_CE))
            bin_ltp = synthetic_ltp(abs(bin_s - START_CE))
            self.assertLessEqual(abs(bin_ltp - 35), abs(lin_ltp - 35),
                                 msg=f"binary ltp {bin_ltp:.1f} not closer to 35 than linear ltp {lin_ltp:.1f}")
        finally:
            _cs_module.strike_values_iterator = original

    def test_extension_binary_differs_from_linear(self):
        """Confirms the extension actually matters — binary finds a different (better) strike."""
        original = _cs_module.strike_values_iterator
        try:
            _cs_module.strike_values_iterator = 400
            lin_s, _ = run_linear(35, START_CE, +1)
            bin_s, _ = run_binary(35, START_CE, +1)
            self.assertNotEqual(bin_s, lin_s)
        finally:
            _cs_module.strike_values_iterator = original


# ---------------------------------------------------------------------------
# PE direction tests
# ---------------------------------------------------------------------------

class TestStrikeSearchPE(unittest.TestCase):

    def _run(self, target):
        lin_s, lin_n = run_linear(target, START_PE, -1)
        bin_s, bin_n = run_binary(target, START_PE, -1)
        return lin_s, lin_n, bin_s, bin_n

    def test_normal_vix_same_strike(self):
        lin_s, _, bin_s, _ = self._run(120)
        self.assertEqual(bin_s, lin_s)

    def test_normal_vix_fewer_calls(self):
        _, lin_n, _, bin_n = self._run(120)
        self.assertLess(bin_n, lin_n)

    def test_high_vix_within_range_same_strike(self):
        lin_s, _, bin_s, _ = self._run(35)
        self.assertEqual(bin_s, lin_s)

    def test_binary_call_budget(self):
        _, _, _, bin_n = self._run(120)
        self.assertLessEqual(bin_n, 9)


def tearDownModule():
    # Evict the shared mocks so subsequent test files get a clean import.
    for _mod in ('configs', 'credit_spread', 'functions', 'logger_setup'):
        sys.modules.pop(_mod, None)


if __name__ == '__main__':
    unittest.main(verbosity=2)
