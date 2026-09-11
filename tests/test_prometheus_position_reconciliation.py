"""
_reconcile_positions() — regression tests.

Context: 2026-09-11, user request — the resume-with-open-position path used
to just log "State was NOT reconciled against the broker's order book —
verify manually if in doubt." with no actual check. User pointed out
SmartAPI's position() endpoint can verify the net QUANTITY (not exact
price, which will legitimately differ from the state file due to brokerage/
other charges). Built matching the existing house pattern already live in
Apollo/Athena/Artemis's own _reconcile_positions() (self.obj.position(),
netqty vs. an expected signed quantity, alert-only on mismatch, never
auto-correct).

Two things specific to Prometheus, both covered below:
- DRY_RUN must skip the check entirely (no real broker position exists in
  paper mode -- calling the broker would only ever "mismatch" against an
  empty book, which is noise).
- The expected quantity must only count lots with status=='open' (a
  booked/closed lot1 with lot2 still open is the routine 2-lot scale-out
  shape -- the broker's real net position reflects lot2 alone).
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
    mod.logger = _null_logger('test_prometheus_reconcile_null')
    return mod


class TestReconcilePositions(unittest.TestCase):

    def setUp(self):
        self.mod = _load_prometheus_module()
        self.p = object.__new__(self.mod.Prometheus)
        self.p._contract = {'symbol_root': 'CRUDEOILM', 'token': '565900', 'symbol': 'CRUDEOILM21SEP26FUT'}
        self.p.state = self.mod.PrometheusState()
        self.p.state.token = '565900'
        self.p.state.direction = 'bearish'
        self.p.state.lot1_status = 'booked'   # lot1 already booked -- only lot2 counts
        self.p.state.lot1_lots = 1
        self.p.state.lot2_status = 'open'
        self.p.state.lot2_lots = 1
        self.alerts = []
        self.mod._slack = lambda msg, channel=None: self.alerts.append((msg, channel))
        self.mod.DRY_RUN = False
        self.mod.LOT_SIZE = 10

    def tearDown(self):
        for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
                   'prometheus_logger_setup', 'prometheus'):
            sys.modules.pop(mod, None)
        for d in (PROM_DIR, REPO_ROOT):
            if d in sys.path:
                sys.path.remove(d)

    def test_skipped_entirely_in_dry_run(self):
        self.mod.DRY_RUN = True
        self.p.obj = MagicMock()
        self.p._reconcile_positions()
        self.p.obj.position.assert_not_called()
        self.assertEqual(self.alerts, [])

    def test_matching_position_logs_ok_no_alert(self):
        # bearish, 1 open lot (lot2) * LOT_SIZE=10 -> expected -10
        self.p.obj = MagicMock()
        self.p.obj.position.return_value = {
            'data': [{'symboltoken': '565900', 'netqty': '-10'}]}
        self.p._reconcile_positions()
        self.assertEqual(self.alerts, [])

    def test_mismatch_alerts_to_errors_channel(self):
        self.p.obj = MagicMock()
        self.p.obj.position.return_value = {
            'data': [{'symboltoken': '565900', 'netqty': '-20'}]}   # broker says 2 lots, state says 1
        self.p._reconcile_positions()
        self.assertEqual(len(self.alerts), 1)
        msg, channel = self.alerts[0]
        self.assertEqual(channel, self.mod.SLACK_ERRORS_CHANNEL)
        self.assertIn('Position mismatch', msg)
        self.assertIn('expected -10', msg)
        self.assertIn('broker=-20', msg)

    def test_only_open_lots_count_toward_expected(self):
        """lot1 already booked (closed) -- must NOT be double-counted
        alongside lot2's still-open quantity."""
        self.p.state.lot1_status = 'booked'
        self.p.state.lot2_status = 'open'
        self.p.obj = MagicMock()
        self.p.obj.position.return_value = {
            'data': [{'symboltoken': '565900', 'netqty': '-10'}]}   # only lot2's 1 lot
        self.p._reconcile_positions()
        self.assertEqual(self.alerts, [])   # would mismatch (-20 expected) if lot1 were wrongly included

    def test_bullish_direction_expects_positive_qty(self):
        self.p.state.direction = 'bullish'
        self.p.obj = MagicMock()
        self.p.obj.position.return_value = {
            'data': [{'symboltoken': '565900', 'netqty': '10'}]}
        self.p._reconcile_positions()
        self.assertEqual(self.alerts, [])

    def test_token_absent_from_broker_book_is_a_mismatch(self):
        self.p.obj = MagicMock()
        self.p.obj.position.return_value = {'data': []}   # broker shows nothing at all for this token
        self.p._reconcile_positions()
        self.assertEqual(len(self.alerts), 1)
        self.assertIn('broker=+0', self.alerts[0][0])

    def test_broker_call_failure_is_a_safe_noop(self):
        self.p.obj = MagicMock()
        self.p.obj.position.side_effect = RuntimeError('network blip')
        self.p._reconcile_positions()   # must not raise
        self.assertEqual(self.alerts, [])

    def test_never_compares_price(self):
        """Only quantity is checked -- price legitimately differs from the
        state file due to brokerage/other charges (user-specified)."""
        self.p.obj = MagicMock()
        self.p.obj.position.return_value = {
            'data': [{'symboltoken': '565900', 'netqty': '-10', 'sellavgprice': '99999.99'}]}
        self.p._reconcile_positions()
        self.assertEqual(self.alerts, [])


if __name__ == '__main__':
    unittest.main()
