"""
§11a — MCX DPL circuit-limit detection, regression tests.

Context: plans/prometheus-phase3-production.md §11a. Originally built as a
chain-heuristic detector (2+ consecutive flat 1-min runs, inferred from
OHLC patterns) — replaced 2026-09-11 after confirming live that AngelOne's
SmartAPI publishes MCX's own live circuit limits directly ('upperCircuit'/
'lowerCircuit' via REST getMarketData(mode='FULL'), agreeing exactly with
the WS SNAP_QUOTE fields, and matching the DPL percentage formula computed
off the broker's own previous-close to within 0.01%). Reading the field
directly is strictly better than the old heuristic or a self-computed
percentage ladder: same broker call, no reference-price reconstruction, and
it catches an isolated single-step freeze (e.g. the real 2026-09-10 21:18
instance) that the old chain heuristic explicitly could not.

Detect + Slack-alert ONLY -- zero change to any SL/target/ST/entry/exit
behavior. Uses the same bare-Prometheus-instance + mocked _slack/logger
harness as test_prometheus_market_close.py.
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
    mod.logger = _null_logger('test_prometheus_dpl_null')
    return mod


class TestFetchDplCircuitLimits(unittest.TestCase):

    def setUp(self):
        self.mod = _load_prometheus_module()
        self.p = object.__new__(self.mod.Prometheus)
        self.p._contract = {'symbol_root': 'CRUDEOILM', 'token': '565900', 'symbol': 'CRUDEOILM21SEP26FUT'}

    def tearDown(self):
        for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
                   'prometheus_logger_setup', 'prometheus'):
            sys.modules.pop(mod, None)
        for d in (PROM_DIR, REPO_ROOT):
            if d in sys.path:
                sys.path.remove(d)

    def test_parses_real_response_shape(self):
        """Shape confirmed live 2026-09-11 against AngelOne's actual API."""
        self.p.obj = MagicMock()
        self.p.obj.getMarketData.return_value = {
            'status': True, 'message': 'SUCCESS', 'errorcode': '',
            'data': {'fetched': [{
                'exchange': 'MCX', 'tradingSymbol': 'CRUDEOILM21SEP26FUT', 'symbolToken': '565900',
                'ltp': 9661.0, 'close': 9720.0, 'lowerCircuit': 9332.0, 'upperCircuit': 10108.0,
            }], 'unfetched': []},
        }
        uc, lc = self.p._fetch_dpl_circuit_limits()
        self.assertEqual(uc, 10108.0)
        self.assertEqual(lc, 9332.0)
        self.p.obj.getMarketData.assert_called_once_with(
            mode='FULL', exchangeTokens={'MCX': ['565900']})

    def test_empty_fetched_returns_none(self):
        self.p.obj = MagicMock()
        self.p.obj.getMarketData.return_value = {'status': True, 'data': {'fetched': [], 'unfetched': ['565900']}}
        uc, lc = self.p._fetch_dpl_circuit_limits()
        self.assertIsNone(uc)
        self.assertIsNone(lc)

    def test_exception_returns_none_not_raises(self):
        self.p.obj = MagicMock()
        self.p.obj.getMarketData.side_effect = RuntimeError('network blip')
        uc, lc = self.p._fetch_dpl_circuit_limits()
        self.assertIsNone(uc)
        self.assertIsNone(lc)


class TestRefreshDplCircuitLimits(unittest.TestCase):

    def setUp(self):
        self.mod = _load_prometheus_module()
        self.p = object.__new__(self.mod.Prometheus)
        self.p._contract = {'symbol_root': 'CRUDEOILM', 'token': '565900', 'symbol': 'CRUDEOILM21SEP26FUT'}
        self.p._dpl_uc = None
        self.p._dpl_lc = None
        self.alerts = []
        self.mod._slack = lambda msg, channel=None: self.alerts.append((msg, channel))

    def tearDown(self):
        for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
                   'prometheus_logger_setup', 'prometheus'):
            sys.modules.pop(mod, None)
        for d in (PROM_DIR, REPO_ROOT):
            if d in sys.path:
                sys.path.remove(d)

    def test_refresh_updates_state_and_announces(self):
        self.p._fetch_dpl_circuit_limits = lambda: (10108.0, 9332.0)
        ok = self.p._refresh_dpl_circuit_limits(announce=True)
        self.assertTrue(ok)
        self.assertEqual(self.p._dpl_uc, 10108.0)
        self.assertEqual(self.p._dpl_lc, 9332.0)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn('LC=9332.00', self.alerts[0][0])
        self.assertIn('UC=10108.00', self.alerts[0][0])

    def test_refresh_without_announce_stays_silent(self):
        self.p._fetch_dpl_circuit_limits = lambda: (10108.0, 9332.0)
        self.p._refresh_dpl_circuit_limits(announce=False)
        self.assertEqual(self.alerts, [])

    def test_refresh_failure_leaves_state_untouched(self):
        self.p._dpl_uc, self.p._dpl_lc = 10108.0, 9332.0
        self.p._fetch_dpl_circuit_limits = lambda: (None, None)
        ok = self.p._refresh_dpl_circuit_limits(announce=True)
        self.assertFalse(ok)
        self.assertEqual(self.p._dpl_uc, 10108.0)   # unchanged, not clobbered to None
        self.assertEqual(self.p._dpl_lc, 9332.0)
        self.assertEqual(self.alerts, [])


class TestCheckDplCircuitHit(unittest.TestCase):

    def setUp(self):
        self.mod = _load_prometheus_module()
        self.p = object.__new__(self.mod.Prometheus)
        self.p._contract = {'symbol_root': 'CRUDEOILM', 'token': '565900', 'symbol': 'CRUDEOILM21SEP26FUT'}
        self.p._dpl_uc = 10108.0
        self.p._dpl_lc = 9332.0
        self.p._dpl_frozen = False
        self.p._dpl_frozen_price = None
        self.alerts = []
        self.mod._slack = lambda msg, channel=None: self.alerts.append((msg, channel))
        self.mod.DPL_CIRCUIT_POLL_ENABLED = True

    def tearDown(self):
        for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
                   'prometheus_logger_setup', 'prometheus'):
            sys.modules.pop(mod, None)
        for d in (PROM_DIR, REPO_ROOT):
            if d in sys.path:
                sys.path.remove(d)

    def test_noop_when_within_band(self):
        self.p._get_contract_ltp = lambda: 9700.0
        self.p._check_dpl_circuit_hit()
        self.assertFalse(self.p._dpl_frozen)
        self.assertEqual(self.alerts, [])

    def test_hits_upper_circuit_freezes_and_alerts(self):
        self.p._get_contract_ltp = lambda: 10108.0
        self.p._check_dpl_circuit_hit()
        self.assertTrue(self.p._dpl_frozen)
        self.assertEqual(self.p._dpl_frozen_price, 10108.0)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn('upper circuit', self.alerts[0][0])
        self.assertIn('10108.00', self.alerts[0][0])

    def test_hits_lower_circuit_freezes_and_alerts(self):
        self.p._get_contract_ltp = lambda: 9332.0
        self.p._check_dpl_circuit_hit()
        self.assertTrue(self.p._dpl_frozen)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn('lower circuit', self.alerts[0][0])

    def test_ltp_above_upper_still_registers_as_frozen(self):
        """Should never happen (exchange enforces the band) but the check
        uses >=/<=, not ==, as a defensive margin."""
        self.p._get_contract_ltp = lambda: 10200.0
        self.p._check_dpl_circuit_hit()
        self.assertTrue(self.p._dpl_frozen)

    def test_does_not_realert_while_still_frozen(self):
        self.p._get_contract_ltp = lambda: 10108.0
        self.p._check_dpl_circuit_hit()
        self.p._check_dpl_circuit_hit()
        self.p._check_dpl_circuit_hit()
        self.assertEqual(len(self.alerts), 1)

    def test_unfreeze_refetches_and_announces_new_band(self):
        self.p._dpl_frozen = True
        self.p._dpl_frozen_price = 10108.0
        self.p._get_contract_ltp = lambda: 10120.0   # moved past the old level -- freeze released
        self.p._refresh_dpl_circuit_limits = lambda announce: (
            setattr(self.p, '_dpl_uc', 11065.0) or setattr(self.p, '_dpl_lc', 8375.0))
        self.p._check_dpl_circuit_hit()
        self.assertFalse(self.p._dpl_frozen)
        self.assertIsNone(self.p._dpl_frozen_price)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn('unfroze from 10108.00', self.alerts[0][0])
        self.assertIn('LC=8375.00', self.alerts[0][0])
        self.assertIn('UC=11065.00', self.alerts[0][0])

    def test_disabled_flag_suppresses_everything(self):
        self.mod.DPL_CIRCUIT_POLL_ENABLED = False
        self.p._get_contract_ltp = lambda: 10108.0
        self.p._check_dpl_circuit_hit()
        self.assertFalse(self.p._dpl_frozen)
        self.assertEqual(self.alerts, [])

    def test_noop_without_known_band(self):
        self.p._dpl_uc = None
        self.p._dpl_lc = None
        self.p._get_contract_ltp = lambda: 10108.0
        self.p._check_dpl_circuit_hit()
        self.assertFalse(self.p._dpl_frozen)
        self.assertEqual(self.alerts, [])

    def test_ltp_unavailable_is_a_safe_noop(self):
        self.p._get_contract_ltp = lambda: None
        self.p._check_dpl_circuit_hit()
        self.assertFalse(self.p._dpl_frozen)
        self.assertEqual(self.alerts, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
