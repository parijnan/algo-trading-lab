"""
Teardown/session-report Slack messaging, regression tests.

Two fixes, same 2026-09-11 session, both in the shutdown path:

1. §_send_session_report date-qualification: user caught 2026-09-10's
   session report showing trade #16's entry as bare "22:15" with no date --
   Phase 3 positions can span multiple sessions (§2, no EOD flatten), so a
   PREVIOUS day's entry read as if it were today's. Fixed with a local
   _ts_str() helper inside _send_session_report: qualifies with the date
   only when it differs from the report's own date, leaving the common
   same-day case exactly as before.

2. §_confirm_logoff: user asked for a "logged off successfully" message,
   symmetric with the login-attempt/login-success messages, positioned
   after the "stopped" message and before the session report in all three
   _teardown() exit branches. The actual terminateSession() call moved from
   main()'s finally into _teardown() itself so the confirmation is tied to
   a real, confirmed logoff -- main()'s finally still calls it too, as a
   defensive fallback for the one path that skips _teardown() entirely.
"""
import importlib.util
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
PROM_DIR = os.path.join(REPO_ROOT, 'prometheus_production')

TRADES_HEADER = ['trade_id', 'contract_expiry', 'direction', 'units', 'entry_ts', 'entry_price',
                 'signal_ts', 'signal_close', 'entry_slippage_points', 'sl_price', 'lot1_target',
                 'lot2_target', 'lot2_target_source', 'lot1_exit_ts', 'lot1_exit_price',
                 'lot1_exit_reason', 'lot1_pnl_points', 'lot1_pnl_rs', 'lot2_exit_ts',
                 'lot2_exit_price', 'lot2_exit_reason', 'lot2_pnl_points', 'lot2_pnl_rs',
                 'total_pnl_points', 'total_pnl_rs', 'parent_trade_id']


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
    mod.logger = _null_logger('test_prometheus_session_report_null')
    return mod


class TestSessionReportDateQualification(unittest.TestCase):

    def setUp(self):
        self.mod = _load_prometheus_module()
        self.p = object.__new__(self.mod.Prometheus)
        self.p._contract = {'symbol_root': 'CRUDEOILM'}
        self.p.state = self.mod.PrometheusState(status='watching')
        self.tmp = tempfile.TemporaryDirectory()
        self.trades_file = Path(self.tmp.name) / 'prometheus_trades.csv'
        self.mod.TRADES_FILE = self.trades_file
        self.captured = []
        self.mod._slack = lambda msg, channel=None: self.captured.append(msg)

    def tearDown(self):
        self.tmp.cleanup()
        for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
                   'prometheus_logger_setup', 'prometheus'):
            sys.modules.pop(mod, None)
        for d in (PROM_DIR, REPO_ROOT):
            if d in sys.path:
                sys.path.remove(d)

    def _write_trade(self, trade_id, entry_ts, exit_ts):
        row = {k: '' for k in TRADES_HEADER}
        row.update({
            'trade_id': trade_id, 'direction': 'bullish', 'units': 1,
            'entry_ts': entry_ts, 'entry_price': 9500.0,
            'lot1_exit_ts': exit_ts, 'lot1_exit_price': 9550.0, 'lot1_exit_reason': 'target1',
            'lot1_pnl_points': 50, 'lot1_pnl_rs': 500,
            'lot2_exit_ts': exit_ts, 'lot2_exit_price': 9550.0, 'lot2_exit_reason': 'target1',
            'lot2_pnl_points': 50, 'lot2_pnl_rs': 500,
            'total_pnl_points': 100, 'total_pnl_rs': 1000,
        })
        pd.DataFrame([row]).to_csv(self.trades_file, index=False)

    def test_previous_day_entry_shown_with_date(self):
        """Trade #16's real shape: entered 22:15 the previous day, exited
        09:15 today -- the entry line must show the date, not bare HH:MM."""
        today = pd.Timestamp.now().normalize()
        yesterday = today - pd.Timedelta(days=1)
        entry_ts = (yesterday + pd.Timedelta(hours=22, minutes=15)).isoformat()
        exit_ts = (today + pd.Timedelta(hours=9, minutes=15)).isoformat()
        self._write_trade(16, entry_ts, exit_ts)

        self.p._send_session_report()

        self.assertEqual(len(self.captured), 1)
        report = self.captured[0]
        self.assertIn(f"Entry: {yesterday.strftime('%d-%b')} 22:15", report)
        self.assertIn('Exit: 09:15', report)   # same-day exit -- bare HH:MM, unchanged

    def test_same_day_entry_and_exit_unchanged(self):
        """Regression guard: the common case (entry and exit both today)
        must NOT gain a date prefix."""
        today = pd.Timestamp.now().normalize()
        entry_ts = (today + pd.Timedelta(hours=10, minutes=0)).isoformat()
        exit_ts = (today + pd.Timedelta(hours=11, minutes=30)).isoformat()
        self._write_trade(17, entry_ts, exit_ts)

        self.p._send_session_report()

        report = self.captured[0]
        self.assertIn('Entry: 10:00', report)
        self.assertIn('Exit: 11:30', report)
        self.assertNotIn(today.strftime('%d-%b'), report)

    def test_open_position_entry_previous_day_shown_with_date(self):
        today = pd.Timestamp.now().normalize()
        yesterday = today - pd.Timedelta(days=1)
        self.p.state = self.mod.PrometheusState(
            status='in_trade', direction='bullish', units=1,
            entry_ts=(yesterday + pd.Timedelta(hours=22, minutes=15)).isoformat(),
            entry_price=9500.0, last_known_ltp=9550.0,
            lot1_status='open', lot1_lots=1, lot2_status='open', lot2_lots=1,
        )
        self.p._compute_trade_pnl = lambda ltp: {
            'realised_pts': 0, 'realised_rs': 0, 'unrealised_pts': 50,
            'unrealised_rs': 500, 'total_rs': 500,
        }
        self.p._send_session_report()
        report = self.captured[0]
        self.assertIn(f"Entry: {yesterday.strftime('%d-%b')} 22:15", report)


class TestSessionTotalRealizedUnrealizedSplit(unittest.TestCase):
    """2026-09-24 (user-requested): the report's bottom "Session Total" is
    split into Realized / Unrealized, no combined total. Realized counts only
    lots whose exit was booked TODAY; per-trade blocks stay full-trade."""

    def setUp(self):
        self.mod = _load_prometheus_module()
        self.p = object.__new__(self.mod.Prometheus)
        self.p._contract = {'symbol_root': 'CRUDEOILM'}
        self.p.state = self.mod.PrometheusState(status='watching')
        self.tmp = tempfile.TemporaryDirectory()
        self.trades_file = Path(self.tmp.name) / 'prometheus_trades.csv'
        self.mod.TRADES_FILE = self.trades_file
        self.captured = []
        self.mod._slack = lambda msg, channel=None: self.captured.append(msg)
        self.today = pd.Timestamp.now().normalize()
        self.yesterday = self.today - pd.Timedelta(days=1)

    def tearDown(self):
        self.tmp.cleanup()
        for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
                   'prometheus_logger_setup', 'prometheus'):
            sys.modules.pop(mod, None)
        for d in (PROM_DIR, REPO_ROOT):
            if d in sys.path:
                sys.path.remove(d)

    def _trade(self, trade_id, units, entry_ts, lot1_ts, lot1_rs, lot2_ts, lot2_rs):
        row = {k: '' for k in TRADES_HEADER}
        row.update({
            'trade_id': trade_id, 'direction': 'bearish', 'units': units,
            'entry_ts': entry_ts.isoformat(), 'entry_price': 9000.0,
            'lot1_exit_ts': lot1_ts.isoformat(), 'lot1_exit_reason': 'target1',
            'lot1_pnl_points': 10, 'lot1_pnl_rs': lot1_rs,
            'lot2_exit_ts': lot2_ts.isoformat(), 'lot2_exit_reason': 'trend_flip',
            'lot2_pnl_points': 5, 'lot2_pnl_rs': lot2_rs,
            'total_pnl_points': 15, 'total_pnl_rs': lot1_rs + lot2_rs,
        })
        return row

    def _run(self, rows):
        if rows:
            pd.DataFrame(rows).to_csv(self.trades_file, index=False)
        self.p._send_session_report()
        self.assertEqual(len(self.captured), 1)
        return self.captured[0]

    def test_carryover_trade_counts_only_todays_lot_in_realized(self):
        """lot1 booked yesterday (5,000 = 1,000/unit), lot2 today (2,500 = 500/unit):
        the trade block shows the FULL trade (+1,500/unit), Realized only +500."""
        row = self._trade(40, 5, self.yesterday + pd.Timedelta(hours=10),
                          self.yesterday + pd.Timedelta(hours=15), 5000,
                          self.today + pd.Timedelta(hours=10), 2500)
        report = self._run([row])
        self.assertIn('(+1,500 Rs/unit)', report)                # trade block: whole trade
        self.assertIn('Realized P&L   :  *+500 Rs/unit*', report)  # session: today's lot only
        self.assertIn('Unrealized P&L :  *+0 Rs/unit*', report)
        self.assertNotIn('Session Total  :', report)             # old combined line is gone

    def test_both_lots_today_both_count(self):
        row = self._trade(41, 5, self.today + pd.Timedelta(hours=9),
                          self.today + pd.Timedelta(hours=11), 5000,
                          self.today + pd.Timedelta(hours=12), 2500)
        report = self._run([row])
        self.assertIn('Realized P&L   :  *+1,500 Rs/unit*', report)

    def test_multiple_trades_realized_sums_per_unit_unweighted(self):
        a = self._trade(42, 1, self.today + pd.Timedelta(hours=9),
                        self.today + pd.Timedelta(hours=10), 100,
                        self.today + pd.Timedelta(hours=10), 100)      # 200/unit
        b = self._trade(43, 5, self.today + pd.Timedelta(hours=11),
                        self.today + pd.Timedelta(hours=12), 5000,
                        self.today + pd.Timedelta(hours=12), 5000)     # 2,000/unit
        report = self._run([a, b])
        self.assertIn('Realized P&L   :  *+2,200 Rs/unit*', report)

    def test_open_position_lot1_booked_today_counts_realized_and_unrealized_split(self):
        lot_size = self.mod.LOT_SIZE
        self.p.state = self.mod.PrometheusState(
            status='in_trade', direction='bearish', units=5,
            entry_ts=(self.today + pd.Timedelta(hours=9)).isoformat(), entry_price=9000.0,
            last_known_ltp=8950.0,
            lot1_status='booked', lot1_lots=5, lot1_exit_price=8900.0,
            lot1_exit_ts=(self.today + pd.Timedelta(hours=11)).isoformat(),
            lot2_status='open', lot2_lots=5,
        )
        self.p._compute_trade_pnl = lambda ltp: {
            'realised_pts': 100.0, 'realised_rs': 100.0 * 5 * lot_size,
            'unrealised_pts': 50.0, 'unrealised_rs': 50.0 * 5 * lot_size,
            'total_rs': 150.0 * 5 * lot_size,
        }
        report = self._run([])
        self.assertIn(f'Realized P&L   :  *+{100.0 * lot_size:,.0f} Rs/unit*', report)
        self.assertIn(f'Unrealized P&L :  *+{50.0 * lot_size:,.0f} Rs/unit*', report)
        # per-trade open block unchanged: still shows its own Realised/Unrealised/P&L
        self.assertIn('*Open Position*', report)
        self.assertIn(f'P&L        : *+{150.0 * lot_size:,.0f} Rs/unit*', report)

    def test_open_position_lot1_booked_earlier_session_excluded_from_realized(self):
        lot_size = self.mod.LOT_SIZE
        self.p.state = self.mod.PrometheusState(
            status='in_trade', direction='bearish', units=5,
            entry_ts=(self.yesterday + pd.Timedelta(hours=9)).isoformat(), entry_price=9000.0,
            last_known_ltp=8950.0,
            lot1_status='booked', lot1_lots=5, lot1_exit_price=8900.0,
            lot1_exit_ts=(self.yesterday + pd.Timedelta(hours=11)).isoformat(),
            lot2_status='open', lot2_lots=5,
        )
        self.p._compute_trade_pnl = lambda ltp: {
            'realised_pts': 100.0, 'realised_rs': 100.0 * 5 * lot_size,
            'unrealised_pts': 50.0, 'unrealised_rs': 50.0 * 5 * lot_size,
            'total_rs': 150.0 * 5 * lot_size,
        }
        report = self._run([])
        self.assertIn('Realized P&L   :  *+0 Rs/unit*', report)     # lot1 was yesterday's
        self.assertIn(f'Unrealized P&L :  *+{50.0 * lot_size:,.0f} Rs/unit*', report)
        self.assertIn(f'Realised   : +100.0 pts  (+{100.0 * lot_size:,.0f} Rs/unit)', report)  # block unchanged

    def test_no_trade_today_shows_zero_split(self):
        report = self._run([])
        self.assertIn('No trade today', report)
        self.assertIn('Realized P&L   :  *+0 Rs/unit*', report)
        self.assertIn('Unrealized P&L :  *+0 Rs/unit*', report)


class TestConfirmLogoff(unittest.TestCase):

    def setUp(self):
        self.mod = _load_prometheus_module()
        self.p = object.__new__(self.mod.Prometheus)
        self.p._client_code = 'p436059'
        self.captured = []
        self.mod._slack = lambda msg, channel=None: self.captured.append(msg)

    def tearDown(self):
        for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
                   'prometheus_logger_setup', 'prometheus'):
            sys.modules.pop(mod, None)
        for d in (PROM_DIR, REPO_ROOT):
            if d in sys.path:
                sys.path.remove(d)

    def test_success_calls_terminate_session_and_announces(self):
        calls = []
        self.p.obj = type('Obj', (), {'terminateSession': lambda self_, code: calls.append(code)})()
        self.p._confirm_logoff('*Prometheus [CRUDEOILM]*')
        self.assertEqual(calls, ['p436059'])
        self.assertEqual(len(self.captured), 1)
        self.assertIn('logged off successfully', self.captured[0])

    def test_failure_is_non_fatal_and_does_not_announce_false_success(self):
        def _raise(self_, code):
            raise RuntimeError('network blip')
        self.p.obj = type('Obj', (), {'terminateSession': _raise})()
        self.p._confirm_logoff('*Prometheus [CRUDEOILM]*')   # must not raise
        self.assertEqual(self.captured, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
