"""
_calculate_margin_per_unit() / _calculate_units() / _check_margin_sufficient()
-- regression tests for the 2026-09-11 dynamic-margin change.

Context: user noticed the actual per-lot margin required by the broker has
risen with crude prices (~Rs.25,000/lot when the static MARGIN_PER_UNIT=100000
was calibrated, ~Rs.30,000/lot now) since MARGIN_PER_UNIT is tied to LTP but
was frozen as a config constant. Replaced with a fresh-per-call computation:
LTP * LOT_SIZE / 3 * 4 (LTP*LOT_SIZE = contract value, /3 = conservative
approx. of actual per-lot margin, *4 reproduces the original 2-lot + 40%
drawdown + 10% -ve MTM calibration -- exactly how 25000 -> 100000 was derived).

Falls back to the static MARGIN_PER_UNIT constant when live LTP is
unavailable. _check_margin_sufficient() always uses the live-computed figure
regardless of DYNAMIC_SIZING, since real affordability is a fact about
current price, not about which sizing mode picked the unit count.
"""
import importlib.util
import logging
import os
import sys
import unittest
from unittest.mock import MagicMock

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
    mod.logger = _null_logger('test_prometheus_dynamic_margin_null')
    return mod


class TestCalculateMarginPerUnit(unittest.TestCase):

    def setUp(self):
        self.mod = _load_prometheus_module()
        self.p = object.__new__(self.mod.Prometheus)
        self.p._contract = {'symbol_root': 'CRUDEOILM', 'token': '565900', 'symbol': 'CRUDEOILM21SEP26FUT'}
        self.mod.LOT_SIZE = 10
        self.mod.MARGIN_PER_UNIT = 100000

    def test_formula_matches_spec(self):
        # LTP=6000, LOT_SIZE=10 -> contract value 60000 -> /3 = 20000 -> *4 = 80000
        self.p._get_contract_ltp = MagicMock(return_value=6000.0)
        self.assertAlmostEqual(self.p._calculate_margin_per_unit(), 80000.0)

    def test_reproduces_original_calibration_at_original_ltp(self):
        # user's own calibration point: ~25000/lot margin -> LTP*LOT_SIZE/3 == 25000
        # -> LTP == 7500 for LOT_SIZE=10 -- result should land on the original 100000.
        self.p._get_contract_ltp = MagicMock(return_value=7500.0)
        self.assertAlmostEqual(self.p._calculate_margin_per_unit(), 100000.0)

    def test_reflects_higher_current_margin(self):
        # user's stated current reality: ~30000/lot -> LTP*LOT_SIZE/3 == 30000
        # -> LTP == 9000 for LOT_SIZE=10 -- result should exceed the stale 100000.
        self.p._get_contract_ltp = MagicMock(return_value=9000.0)
        self.assertAlmostEqual(self.p._calculate_margin_per_unit(), 120000.0)
        self.assertGreater(self.p._calculate_margin_per_unit(), self.mod.MARGIN_PER_UNIT)

    def test_falls_back_to_static_constant_when_ltp_unavailable(self):
        self.p._get_contract_ltp = MagicMock(return_value=None)
        self.assertEqual(self.p._calculate_margin_per_unit(), 100000)

    def test_falls_back_to_static_constant_on_zero_ltp(self):
        """A bad/zero print (§11's known live failure mode) must fall back
        too, not just a None -- 0.0 would otherwise silently zero out the
        margin requirement and defeat _check_margin_sufficient's own guard."""
        self.p._get_contract_ltp = MagicMock(return_value=0.0)
        self.assertEqual(self.p._calculate_margin_per_unit(), 100000)

    def test_uses_main_contract_lot_size(self):
        self.mod.LOT_SIZE = 100   # CRUDEOIL, not CRUDEOILM
        self.p._get_contract_ltp = MagicMock(return_value=6000.0)
        self.assertAlmostEqual(self.p._calculate_margin_per_unit(), 800000.0)


class TestCalculateUnits(unittest.TestCase):

    def setUp(self):
        self.mod = _load_prometheus_module()
        self.p = object.__new__(self.mod.Prometheus)
        self.p._contract = {'symbol_root': 'CRUDEOILM', 'token': '565900', 'symbol': 'CRUDEOILM21SEP26FUT'}
        self.mod.LOT_SIZE = 10
        self.mod.MARGIN_PER_UNIT = 100000

    def test_static_mode_ignores_live_margin_entirely(self):
        self.mod.resolve_live_sizing = MagicMock(return_value=(False, 3))
        self.p._fetch_available_margin = MagicMock(side_effect=AssertionError('should not be called'))
        self.p._get_contract_ltp = MagicMock(side_effect=AssertionError('should not be called'))
        self.assertEqual(self.p._calculate_units(), 3)

    def test_dynamic_mode_uses_live_margin_per_unit(self):
        self.mod.resolve_live_sizing = MagicMock(return_value=(True, 1))
        self.p._fetch_available_margin = MagicMock(return_value=250000.0)
        self.p._get_contract_ltp = MagicMock(return_value=9000.0)   # -> margin_per_unit=120000
        self.assertEqual(self.p._calculate_units(), 2)   # 250000 // 120000 = 2

    def test_dynamic_mode_falls_back_to_static_on_rms_failure(self):
        self.mod.resolve_live_sizing = MagicMock(return_value=(True, 5))
        self.p._fetch_available_margin = MagicMock(side_effect=RuntimeError('rms down'))
        self.p._get_contract_ltp = MagicMock(return_value=9000.0)
        self.assertEqual(self.p._calculate_units(), 5)

    def test_dynamic_mode_at_least_one_unit(self):
        self.mod.resolve_live_sizing = MagicMock(return_value=(True, 1))
        self.p._fetch_available_margin = MagicMock(return_value=1000.0)   # far too little for even 1
        self.p._get_contract_ltp = MagicMock(return_value=9000.0)
        self.assertEqual(self.p._calculate_units(), 1)


class TestCheckMarginSufficient(unittest.TestCase):

    def setUp(self):
        self.mod = _load_prometheus_module()
        self.p = object.__new__(self.mod.Prometheus)
        self.p._contract = {'symbol_root': 'CRUDEOILM', 'token': '565900', 'symbol': 'CRUDEOILM21SEP26FUT'}
        self.mod.LOT_SIZE = 10
        self.mod.MARGIN_PER_UNIT = 100000
        self.alerts = []
        self.mod._slack = lambda msg, channel=None: self.alerts.append((msg, channel))

    def test_uses_live_margin_even_in_static_sizing_mode(self):
        """The affordability check must reflect the live margin requirement
        regardless of DYNAMIC_SIZING -- a stale static 100000 would silently
        pass an entry that actually needs the live-computed 120000."""
        self.p._fetch_available_margin = MagicMock(return_value=110000.0)
        self.p._get_contract_ltp = MagicMock(return_value=9000.0)   # -> margin_per_unit=120000
        self.assertFalse(self.p._check_margin_sufficient(1))
        self.assertEqual(len(self.alerts), 1)
        self.assertIn('insufficient margin', self.alerts[0][0].lower())

    def test_sufficient_margin_passes(self):
        self.p._fetch_available_margin = MagicMock(return_value=150000.0)
        self.p._get_contract_ltp = MagicMock(return_value=9000.0)   # -> margin_per_unit=120000
        self.assertTrue(self.p._check_margin_sufficient(1))
        self.assertEqual(self.alerts, [])

    def test_falls_back_to_static_margin_when_ltp_unavailable(self):
        self.p._fetch_available_margin = MagicMock(return_value=100000.0)
        self.p._get_contract_ltp = MagicMock(return_value=None)   # -> falls back to static 100000
        self.assertTrue(self.p._check_margin_sufficient(1))   # 100000 >= 1*100000

    def test_zero_ltp_falls_back_instead_of_silently_passing(self):
        """A zero/bad LTP print must not zero out the required margin and
        thereby make the affordability check pass unconditionally -- it
        must fall back to the static MARGIN_PER_UNIT like any other
        LTP-unavailable case."""
        self.p._fetch_available_margin = MagicMock(return_value=50000.0)   # too little for even 1 static unit
        self.p._get_contract_ltp = MagicMock(return_value=0.0)
        self.assertFalse(self.p._check_margin_sufficient(1))   # 50000 < static 100000

    def test_rms_failure_proceeds_without_check(self):
        self.p._fetch_available_margin = MagicMock(side_effect=RuntimeError('rms down'))
        self.p._get_contract_ltp = MagicMock(return_value=9000.0)
        self.assertTrue(self.p._check_margin_sufficient(1))
        self.assertEqual(self.alerts, [])


if __name__ == '__main__':
    unittest.main()
