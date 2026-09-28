"""Hestia <-> engine interface v1: the invariants the rest of the design leans on (hestia_core/interface.py)."""
import dataclasses
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hestia_core import interface as i  # noqa: E402

REF = i.ContractRef('SILVERMIC', '562058', 'SILVERMIC30NOV26FUT', date(2026, 11, 30))
NOW = datetime(2026, 9, 28, 14, 30)


class TestRequests(unittest.TestCase):

    def test_request_id_is_required(self):
        for bad in ('', '   ', None):
            with self.assertRaises(ValueError):
                i.FlattenRequest(bad, REF, i.ExitReason.MANUAL_EXIT)
            with self.assertRaises(ValueError):
                i.OpenRequest(bad, REF, i.Direction.BULLISH, 1, trade_ref=1)

    def test_lots_must_be_positive_ints(self):
        for bad in (0, -1, 1.5, True):
            with self.assertRaises(ValueError):
                i.OpenRequest('r1', REF, i.Direction.BULLISH, bad, trade_ref=1)
        with self.assertRaises(ValueError):
            i.FlipRequest('r1', REF, i.Direction.BULLISH, close_lots=0, open_lots=2, trade_ref=2)
        with self.assertRaises(ValueError):
            i.FlipRequest('r1', REF, i.Direction.BULLISH, close_lots=2, open_lots=0, trade_ref=2)

    def test_close_all_needs_no_lot_count_but_a_partial_close_must_be_positive(self):
        i.CloseRequest('r1', REF, i.Direction.BULLISH, i.ExitReason.STOP_LOSS)         # lots=None: every lot held
        with self.assertRaises(ValueError):
            i.CloseRequest('r1', REF, i.Direction.BULLISH, i.ExitReason.TREND_FLIP, lots=0)

    def test_requests_are_immutable(self):
        req = i.OpenRequest('r1', REF, i.Direction.BEARISH, 3, trade_ref=7)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            req.lots = 4

    def test_flip_direction(self):
        f = i.FlipRequest('r1', REF, i.Direction.BULLISH, 5, 5, trade_ref=2)
        self.assertEqual(f.to_direction, i.Direction.BEARISH)
        f = i.FlipRequest('r1', REF, i.Direction.BEARISH, 5, 5, trade_ref=2)
        self.assertEqual(f.to_direction, i.Direction.BULLISH)


class TestPriority(unittest.TestCase):
    """Plan section 1.8: risk-reducing before risk-adding, deterministic."""

    def test_classes(self):
        P = i.PriorityClass
        self.assertEqual(i.priority_class(i.FlattenRequest('a', REF, i.ExitReason.MANUAL_EXIT)), P.STOP_OR_FLATTEN)
        self.assertEqual(i.priority_class(i.CloseRequest('a', REF, i.Direction.BULLISH, i.ExitReason.STOP_LOSS)), P.STOP_OR_FLATTEN)
        self.assertEqual(i.priority_class(i.CloseRequest('a', REF, i.Direction.BULLISH, i.ExitReason.TREND_FLIP)), P.CLOSE)
        self.assertEqual(i.priority_class(i.CloseRequest('a', REF, i.Direction.BULLISH, i.ExitReason.ROLL)), P.ROLL_EXIT)
        self.assertEqual(i.priority_class(i.FlipRequest('a', REF, i.Direction.BULLISH, 1, 1, trade_ref=1)), P.CLOSE)
        self.assertEqual(i.priority_class(i.FlipRequest('a', REF, i.Direction.BULLISH, 1, 1, trade_ref=1,
                                                       reason=i.ExitReason.STOP_LOSS)), P.STOP_OR_FLATTEN)
        self.assertEqual(i.priority_class(i.OpenRequest('a', REF, i.Direction.BULLISH, 1, trade_ref=1)), P.OPEN)
        self.assertEqual(i.priority_class(i.OpenRequest('a', REF, i.Direction.BULLISH, 1, trade_ref=1, roll_reopen=True)),
                         P.ROLL_REOPEN)

    def test_every_exit_outranks_every_entry(self):
        P = i.PriorityClass
        self.assertLess(max(P.STOP_OR_FLATTEN, P.CLOSE, P.ROLL_EXIT), min(P.OPEN, P.ROLL_REOPEN))

    def test_unknown_object_is_rejected(self):
        with self.assertRaises(TypeError):
            i.priority_class('not a request')


class TestOutcomes(unittest.TestCase):

    def _out(self, status):
        return i.RequestOutcome('r1', status, i.RequestKind.CLOSE, 5, NOW)

    def test_only_filled_and_partial_are_confirmed(self):
        """The fill-confirmation invariant: an engine mutates state only on a confirmed outcome."""
        confirmed = {s for s in i.OutcomeStatus if self._out(s).confirmed}
        self.assertEqual(confirmed, {i.OutcomeStatus.FILLED, i.OutcomeStatus.PARTIAL})

    def test_unconfirmed_and_abandoned_never_count_as_fills(self):
        self.assertFalse(self._out(i.OutcomeStatus.UNCONFIRMED).confirmed)
        self.assertFalse(self._out(i.OutcomeStatus.ABANDONED).confirmed)


class TestRestartSafety(unittest.TestCase):

    def test_pending_request_round_trips_through_json(self):
        p = i.PendingRequest('selene-12-open-1', 'open', 'trend_flip', '2026-09-28T14:30:00', trade_ref=12)
        self.assertEqual(i.PendingRequest.from_json(p.to_json()), p)


class TestProtocols(unittest.TestCase):

    def test_context_and_engine_protocols_are_checkable(self):
        names = [n for n in i.EngineContext.__dict__ if not n.startswith('_')]

        class Ctx:
            pass
        for n in names:
            setattr(Ctx, n, lambda self, *a, **k: None)
        self.assertTrue(isinstance(Ctx(), i.EngineContext))
        self.assertFalse(isinstance(object(), i.EngineContext))

        class Eng:
            name = 'x'
            spec = i.DataSpec('SILVERMIC', 15, 10, 2.5)

            def run(self, ctx):
                return None
        self.assertTrue(isinstance(Eng(), i.Engine))

    def test_data_spec_defaults(self):
        s = i.DataSpec('SILVERMIC', 15, 10, 2.5)
        self.assertEqual(s.seed_days, 18)
        self.assertTrue(s.provisional.enabled)
        self.assertTrue(s.watch_dpl)

    def test_version(self):
        self.assertEqual(i.INTERFACE_VERSION, 1)


if __name__ == '__main__':
    unittest.main()


class TestTradeRecordFormat(unittest.TestCase):

    def test_columns_match_prometheus_exactly(self):
        """The trade record format is Prometheus's (user decision); this keeps the two from drifting."""
        import ast
        src = (REPO / 'prometheus_production' / 'prometheus_functions.py').read_text()
        cols = None
        for node in ast.parse(src).body:
            if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == 'TRADE_LOG_COLUMNS' for t in node.targets):
                cols = tuple(ast.literal_eval(node.value))
        self.assertIsNotNone(cols)
        self.assertEqual(i.TRADE_RECORD_COLUMNS, cols)

    def test_sizing_config_keeps_both_modes(self):
        s = i.SizingConfig(dynamic=False, static_units=1, unit_cap=50)
        self.assertFalse(s.dynamic)
        self.assertIsNone(s.allocation_rs)
        self.assertTrue(i.SizingConfig(dynamic=True, static_units=1, unit_cap=50, allocation_rs=500000.0).dynamic)
