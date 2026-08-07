"""
Strike math tests — Artemis CreditSpread.

What this catches:
  - SL multiplier ladder: given days_to_expiry, _set_sl() applies the correct
    sl_N_dte multiplier from configs. The DTE value comes from busday_count(), so
    tests use weekday-aligned dates to produce exact known values.
  - Index SL offset: PE spread gets sell_strike + index_sl_offset,
    CE spread gets sell_strike - index_sl_offset.
  - Negative DTE edge case: busday_count can return negative (expiry in the past)
    — the else branch returns sell_entry unmodified.

Approach: 'artemis_configs', 'artemis_functions', and 'artemis_logger_setup'
are injected into sys.modules before importing credit_spread, avoiding CSV
reads and API calls.
CreditSpread instances are constructed via object.__new__ (no __init__ side
effects) and attributes set manually.
"""

import logging
import os
import sys
import types
import unittest
from datetime import date, datetime, time

REPO_ROOT   = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
ARTEMIS_DIR = os.path.join(REPO_ROOT, 'artemis_production')

# ---------------------------------------------------------------------------
# Inject module mocks before any artemis import
# ---------------------------------------------------------------------------

_SL_4 = 2.0
_SL_3 = 1.8
_SL_2 = 1.5
_SL_1 = 1.2
_SL_0 = 1.0
_INDEX_OFFSET = 500

_m_conf = types.ModuleType('artemis_configs')
for _attr in (
    'contracts_df', 'strike_iteration_interval', 'hedge_points',
    'expected_option_premium', 'strike_values_iterator', 'qty_freeze',
    'adjustment_distance', 'instrument', 'underlying_token',
    'exchange_segment', 'fo_exchange_segment', 'minimum_gap',
    'minimum_gap_iterator', 'ORDER_TIMEOUT_SEC',
):
    setattr(_m_conf, _attr, 0)
_m_conf.pd          = None
_m_conf.lot_size    = 15
_m_conf.lot_count   = 1
_m_conf.sl_4_dte    = _SL_4
_m_conf.sl_3_dte    = _SL_3
_m_conf.sl_2_dte    = _SL_2
_m_conf.sl_1_dte    = _SL_1
_m_conf.sl_0_dte    = _SL_0
_m_conf.index_sl_offset  = _INDEX_OFFSET
_m_conf.SLACK_TRADE_ALERTS   = '#trade-alerts'
_m_conf.SLACK_ERRORS_CHANNEL = '#error-alerts'
_m_conf.LOG_LEVEL   = 'DEBUG'
_m_conf.LOGS_DIR    = '/tmp'
sys.modules['artemis_configs'] = _m_conf

_m_log = types.ModuleType('artemis_logger_setup')
_m_log.get_logger = logging.getLogger
sys.modules['artemis_logger_setup'] = _m_log

_m_func = types.ModuleType('artemis_functions')
for _name in (
    'slack_bot_sendtext', 'sleep', 'exists', 'handle_exception',
    'increment_poll_counter', 'increment_order_counter',
    'increment_order_book_poll', 'reset_counters',
):
    setattr(_m_func, _name, lambda *a, **kw: None)
sys.modules['artemis_functions'] = _m_func

if ARTEMIS_DIR not in sys.path:
    sys.path.insert(0, ARTEMIS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from credit_spread import CreditSpread  # noqa: E402 — must follow sys.modules setup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spread(spread_type, sell_entry, sell_strike, current_date, expiry_date):
    cs = object.__new__(CreditSpread)
    cs.spread_type       = spread_type
    cs.sell_entry        = sell_entry
    cs.sell_strike       = sell_strike
    cs.current_datetime  = datetime.combine(current_date, time(10, 30))
    cs.expiry            = datetime.combine(expiry_date, time(15, 30))
    return cs


def _set_sl(spread_type, sell_entry, sell_strike, current_date, expiry_date):
    cs = _make_spread(spread_type, sell_entry, sell_strike, current_date, expiry_date)
    cs._set_sl()
    return cs


# ---------------------------------------------------------------------------
# SL multiplier ladder
#
# busday_count(start, end) counts weekdays in [start, end).
# Using week of 2026-05-25 (Mon) to 2026-05-29 (Fri) as the test window:
#   Mon→Fri: 4    Tue→Fri: 3    Wed→Fri: 2    Thu→Fri: 1    Fri→Fri: 0
# ---------------------------------------------------------------------------

_EXPIRY = date(2026, 5, 29)   # Friday — expiry date


class TestSLMultiplierLadder(unittest.TestCase):

    SELL_ENTRY  = 100.0
    SELL_STRIKE = 45000

    def _opt_sl(self, current_date):
        return _set_sl('ce', self.SELL_ENTRY, self.SELL_STRIKE,
                       current_date, _EXPIRY).option_sl

    def test_4_dte_uses_sl_4_dte(self):
        """4 trading days to expiry → sl_4_dte multiplier (>3 branch)."""
        sl = self._opt_sl(date(2026, 5, 25))   # Mon: 4 days in [Mon, Fri)
        self.assertAlmostEqual(sl, self.SELL_ENTRY * _SL_4)

    def test_5_plus_dte_also_uses_sl_4_dte(self):
        """More than 4 DTE still falls into the >3 branch."""
        sl = self._opt_sl(date(2026, 5, 18))   # Mon prior week: ≥5 days
        self.assertAlmostEqual(sl, self.SELL_ENTRY * _SL_4)

    def test_3_dte_uses_sl_3_dte(self):
        """Exactly 3 trading days to expiry → sl_3_dte."""
        sl = self._opt_sl(date(2026, 5, 26))   # Tue: 3 days in [Tue, Fri)
        self.assertAlmostEqual(sl, self.SELL_ENTRY * _SL_3)

    def test_2_dte_uses_sl_2_dte(self):
        """Exactly 2 trading days to expiry → sl_2_dte."""
        sl = self._opt_sl(date(2026, 5, 27))   # Wed: 2 days in [Wed, Fri)
        self.assertAlmostEqual(sl, self.SELL_ENTRY * _SL_2)

    def test_1_dte_uses_sl_1_dte(self):
        """Exactly 1 trading day to expiry → sl_1_dte."""
        sl = self._opt_sl(date(2026, 5, 28))   # Thu: 1 day in [Thu, Fri)
        self.assertAlmostEqual(sl, self.SELL_ENTRY * _SL_1)

    def test_0_dte_uses_sl_0_dte(self):
        """Expiry day (0 DTE) → sl_0_dte."""
        sl = self._opt_sl(date(2026, 5, 29))   # Fri: 0 days in [Fri, Fri)
        self.assertAlmostEqual(sl, self.SELL_ENTRY * _SL_0)

    def test_sl_ladder_is_monotonically_decreasing(self):
        """Higher DTE → looser (larger) SL multiple."""
        dates = [
            date(2026, 5, 25),  # 4 DTE
            date(2026, 5, 26),  # 3 DTE
            date(2026, 5, 27),  # 2 DTE
            date(2026, 5, 28),  # 1 DTE
            date(2026, 5, 29),  # 0 DTE
        ]
        sls = [self._opt_sl(d) for d in dates]
        for i in range(len(sls) - 1):
            self.assertGreater(sls[i], sls[i + 1],
                               msg=f"SL at index {i} should be > SL at index {i+1}")

    def test_negative_dte_falls_back_to_sell_entry(self):
        """Expired contract (busday_count < 0) returns sell_entry unmodified."""
        cs = _set_sl('ce', self.SELL_ENTRY, self.SELL_STRIKE,
                     date(2026, 5, 30), _EXPIRY)   # day after expiry
        self.assertAlmostEqual(cs.option_sl, self.SELL_ENTRY)


# ---------------------------------------------------------------------------
# Index SL offset
# ---------------------------------------------------------------------------

class TestIndexSLOffset(unittest.TestCase):

    SELL_ENTRY  = 100.0
    SELL_STRIKE = 45000

    def _set(self, spread_type):
        return _set_sl(spread_type, self.SELL_ENTRY, self.SELL_STRIKE,
                       date(2026, 5, 25), _EXPIRY)

    def test_pe_index_sl_is_above_sell_strike(self):
        """PE spread: index SL = sell_strike + index_sl_offset (index must fall below this)."""
        cs = self._set('pe')
        self.assertEqual(cs.index_sl, self.SELL_STRIKE + _INDEX_OFFSET)

    def test_ce_index_sl_is_below_sell_strike(self):
        """CE spread: index SL = sell_strike - index_sl_offset (index must rise above this)."""
        cs = self._set('ce')
        self.assertEqual(cs.index_sl, self.SELL_STRIKE - _INDEX_OFFSET)

    def test_pe_index_sl_magnitude(self):
        """Offset is applied symmetrically — magnitude equals index_sl_offset."""
        pe = self._set('pe')
        ce = self._set('ce')
        self.assertEqual(pe.index_sl - self.SELL_STRIKE,  _INDEX_OFFSET)
        self.assertEqual(self.SELL_STRIKE - ce.index_sl,  _INDEX_OFFSET)

    def test_index_sl_independent_of_sell_entry(self):
        """index_sl depends only on sell_strike and offset, not sell_entry."""
        cs1 = _set_sl('ce', 50.0,  self.SELL_STRIKE, date(2026, 5, 25), _EXPIRY)
        cs2 = _set_sl('ce', 200.0, self.SELL_STRIKE, date(2026, 5, 25), _EXPIRY)
        self.assertEqual(cs1.index_sl, cs2.index_sl)


if __name__ == '__main__':
    unittest.main(verbosity=2)
