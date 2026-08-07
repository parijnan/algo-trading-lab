"""
State persistence round-trip tests — Apollo and Athena.

What this catches:
  - Typed field corruption across the CSV round-trip:
      bool  → CSV stores 'True'/'False' strings → must come back as Python bool
      int   → CSV may widen to float when a NaN appears elsewhere → must cast back
      None  → CSV stores NaN → must come back as Python None, not 'nan' string
  - clear_trade_fields() leaving stale values after a trade closes
  - A new field added to the dataclass without a matching cast in load_state()
  - Missing state file returning a clean idle state on restart

Isolation: each strategy's state module is loaded via importlib.util with a
temporary alias (matching whatever bare name that strategy's state module
imports from) so Apollo and Athena modules never share sys.modules entries
and tests can run in either order. Both strategies' files were renamed
2026-08-07 (configs_live.py -> <strategy>_configs.py, state.py ->
<strategy>_state.py) to fix a cross-strategy sys.modules collision — see
plans/strategy-module-naming-collision-fix.md.
"""

import os
import sys
import tempfile
import unittest
import importlib.util
import pandas as pd
from dataclasses import fields as dc_fields

REPO_ROOT  = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
APOLLO_DIR = os.path.join(REPO_ROOT, 'apollo_production')
ATHENA_DIR = os.path.join(REPO_ROOT, 'athena_production')


def _load_state_module(strategy_dir, prefix, configs_filename, configs_import_name,
                       state_filename):
    """
    Load a strategy's state module without polluting global sys.modules.

    The state file does `from <configs_import_name> import STATE_FILE`. We
    temporarily register the strategy's configs module under that bare name
    so the from-import resolves correctly, then remove the alias immediately
    after. The loaded state module retains its own reference to STATE_FILE.
    """
    configs_spec = importlib.util.spec_from_file_location(
        f'{prefix}_{configs_import_name}',
        os.path.join(strategy_dir, configs_filename))
    configs_mod = importlib.util.module_from_spec(configs_spec)
    sys.modules[configs_import_name] = configs_mod
    configs_spec.loader.exec_module(configs_mod)

    state_spec = importlib.util.spec_from_file_location(
        f'{prefix}_state',
        os.path.join(strategy_dir, state_filename))
    state_mod = importlib.util.module_from_spec(state_spec)
    state_spec.loader.exec_module(state_mod)

    sys.modules.pop(configs_import_name, None)
    return state_mod


# ---------------------------------------------------------------------------
# Apollo
# ---------------------------------------------------------------------------

class TestApolloStateRoundtrip(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m   = _load_state_module(APOLLO_DIR, 'apollo',
                                      'apollo_configs.py', 'apollo_configs', 'apollo_state.py')
        self.m.STATE_FILE = os.path.join(self.tmp.name, 'apollo_state.csv')

    def tearDown(self):
        self.tmp.cleanup()

    def _populated(self):
        s = self.m.ApolloState()
        s.status              = 'in_trade'
        s.direction           = 'bullish'
        s.buy_strike          = 24800
        s.sell_strike         = 25100
        s.option_type         = 'ce'
        s.expiry              = '2026-05-29'
        s.buy_token           = '12345'
        s.sell_token          = '67890'
        s.buy_symbol          = 'NIFTY29MAY2624800CE'
        s.sell_symbol         = 'NIFTY29MAY2625100CE'
        s.buy_entry           = 145.50
        s.sell_entry          = 42.25
        s.lots                = 3
        s.net_debit           = 103.25
        s.max_profit          = 196.75
        s.profit_target_pts   = 68.86
        s.hard_stop_pts       = 40.0
        s.entry_time          = '2026-05-21 10:30:00'
        s.entry_spot          = 24823.45
        s.entry_vix           = 28.5
        s.gate_date           = '2026-05-22'
        s.gate_checked        = False
        s.gate_min_profit_pct = 0.25
        s.max_unrealised_pl   = 18.5
        s.last_buy_ltp        = 148.0
        s.last_sell_ltp       = 44.0
        return s

    def test_full_roundtrip_types(self):
        """All field types survive save → load with correct Python types."""
        self.m.save_state(self._populated())
        r = self.m.load_state()

        self.assertEqual(r.status,    'in_trade')
        self.assertEqual(r.direction, 'bullish')
        self.assertIsInstance(r.direction, str)

        self.assertEqual(r.buy_strike, 24800)
        self.assertIsInstance(r.buy_strike, int)     # not float
        self.assertEqual(r.sell_strike, 25100)
        self.assertEqual(r.lots, 3)
        self.assertIsInstance(r.lots, int)

        self.assertAlmostEqual(r.buy_entry, 145.50)
        self.assertIsInstance(r.buy_entry, float)
        self.assertAlmostEqual(r.max_unrealised_pl, 18.5)

        self.assertIs(r.gate_checked, False)         # bool, not string
        self.assertIsInstance(r.gate_checked, bool)

    def test_gate_checked_true_roundtrip(self):
        """gate_checked=True must come back as bool True, not string 'True'."""
        s = self._populated()
        s.gate_checked = True
        self.m.save_state(s)
        r = self.m.load_state()
        self.assertIs(r.gate_checked, True)
        self.assertIsInstance(r.gate_checked, bool)

    def test_none_fields_roundtrip(self):
        """An idle state with all-None trade fields round-trips cleanly."""
        self.m.save_state(self.m.ApolloState())
        r = self.m.load_state()
        self.assertEqual(r.status, 'idle')
        self.assertIsNone(r.direction)
        self.assertIsNone(r.buy_strike)
        self.assertIsNone(r.buy_entry)
        self.assertIsNone(r.gate_date)
        self.assertIs(r.gate_checked, False)
        self.assertIsInstance(r.gate_checked, bool)
        self.assertAlmostEqual(r.max_unrealised_pl, 0.0)

    def test_clear_trade_fields(self):
        """clear_trade_fields resets every trade field; status → idle."""
        s = self._populated()
        self.m.clear_trade_fields(s)
        self.assertEqual(s.status, 'idle')
        self.assertIsNone(s.direction)
        self.assertIsNone(s.buy_strike)
        self.assertIsNone(s.buy_entry)
        self.assertIsNone(s.gate_date)
        self.assertIs(s.gate_checked, False)
        self.assertIsInstance(s.gate_checked, bool)
        self.assertEqual(s.lots, 1)
        self.assertAlmostEqual(s.max_unrealised_pl, 0.0)

    def test_missing_file_returns_idle(self):
        """load_state() with no file on disk returns a clean idle state."""
        r = self.m.load_state()     # state file was never written
        self.assertEqual(r.status, 'idle')
        self.assertIsNone(r.direction)

    def test_unknown_column_ignored(self):
        """Extra CSV column (forward-compatibility) does not crash load."""
        s   = self._populated()
        row = {f.name: getattr(s, f.name) for f in dc_fields(s)}
        row['future_field_xyz'] = 'some_value'
        pd.DataFrame([row]).to_csv(self.m.STATE_FILE, index=False)
        r = self.m.load_state()
        self.assertEqual(r.status, 'in_trade')


# ---------------------------------------------------------------------------
# Athena
# ---------------------------------------------------------------------------

class TestAthenaStateRoundtrip(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m   = _load_state_module(ATHENA_DIR, 'athena',
                                      'athena_configs.py', 'athena_configs', 'athena_state.py')
        self.m.STATE_FILE = os.path.join(self.tmp.name, 'athena_state.csv')

    def tearDown(self):
        self.tmp.cleanup()

    def _populated(self):
        s = self.m.AthenaState()
        s.status              = 'in_trade'
        s.wings_enabled       = True
        s.sell_expiry         = '2026-05-29'
        s.buy_expiry          = '2026-06-26'
        s.ce_sell_strike      = 24900
        s.ce_sell_token       = '11111'
        s.ce_sell_symbol      = 'NIFTY29MAY2624900CE'
        s.ce_sell_entry       = 85.0
        s.ce_buy_strike       = 24900
        s.ce_buy_token        = '22222'
        s.ce_buy_symbol       = 'NIFTY26JUN2624900CE'
        s.ce_buy_entry        = 142.0
        s.pe_sell_strike      = 24600
        s.pe_sell_token       = '33333'
        s.pe_sell_symbol      = 'NIFTY29MAY2624600PE'
        s.pe_sell_entry       = 75.0
        s.pe_buy_strike       = 24600
        s.pe_buy_token        = '44444'
        s.pe_buy_symbol       = 'NIFTY26JUN2624600PE'
        s.pe_buy_entry        = 120.0
        s.pe_wing_strike      = 24200
        s.pe_wing_token       = '55555'
        s.pe_wing_symbol      = 'NIFTY26JUN2624200PE'
        s.pe_wing_entry       = 25.0
        s.emer_active         = False
        s.emer_attempts       = 0
        s.lots                = 5
        s.net_debit           = 107.0
        s.entry_time          = '2026-05-21 10:30:00'
        s.entry_spot          = 24750.5
        s.entry_vix           = 19.2
        s.max_unrealised_pl   = 22.3
        s.running_realised_pl = 0.0
        return s

    def test_full_roundtrip_types(self):
        self.m.save_state(self._populated())
        r = self.m.load_state()

        self.assertEqual(r.status, 'in_trade')
        self.assertEqual(r.ce_sell_strike, 24900)
        self.assertIsInstance(r.ce_sell_strike, int)
        self.assertAlmostEqual(r.ce_sell_entry, 85.0)
        self.assertIsInstance(r.ce_sell_entry, float)
        self.assertIs(r.wings_enabled, True)
        self.assertIsInstance(r.wings_enabled, bool)
        self.assertIs(r.emer_active, False)
        self.assertIsInstance(r.emer_active, bool)
        self.assertEqual(r.lots, 5)
        self.assertIsInstance(r.lots, int)
        self.assertEqual(r.emer_attempts, 0)
        self.assertIsInstance(r.emer_attempts, int)

    def test_emer_active_roundtrip(self):
        """Emergency hedge active — bool True + int fields round-trip cleanly."""
        s = self._populated()
        s.emer_active   = True
        s.emer_strike   = 25100
        s.emer_token    = '99999'
        s.emer_entry    = 35.0
        s.emer_attempts = 2
        self.m.save_state(s)
        r = self.m.load_state()
        self.assertIs(r.emer_active, True)
        self.assertIsInstance(r.emer_active, bool)
        self.assertEqual(r.emer_strike, 25100)
        self.assertIsInstance(r.emer_strike, int)
        self.assertEqual(r.emer_attempts, 2)
        self.assertIsInstance(r.emer_attempts, int)

    def test_wings_disabled_roundtrip(self):
        """wings_enabled=False with None wing fields round-trips cleanly."""
        s = self._populated()
        s.wings_enabled  = False
        s.pe_wing_token  = None
        s.pe_wing_symbol = None
        s.pe_wing_entry  = None
        self.m.save_state(s)
        r = self.m.load_state()
        self.assertIs(r.wings_enabled, False)
        self.assertIsInstance(r.wings_enabled, bool)
        self.assertIsNone(r.pe_wing_token)
        self.assertIsNone(r.pe_wing_entry)

    def test_clear_trade_fields(self):
        """clear_trade_fields resets all trade fields to fresh defaults."""
        s = self._populated()
        self.m.clear_trade_fields(s)
        self.assertEqual(s.status, 'idle')
        self.assertIsNone(s.ce_sell_strike)
        self.assertIsNone(s.ce_sell_entry)
        self.assertIs(s.emer_active, False)
        self.assertEqual(s.emer_attempts, 0)
        self.assertAlmostEqual(s.max_unrealised_pl, 0.0)
        self.assertAlmostEqual(s.running_realised_pl, 0.0)

    def test_missing_file_returns_idle(self):
        r = self.m.load_state()
        self.assertEqual(r.status, 'idle')
        self.assertIsNone(r.ce_sell_strike)

    def test_unknown_column_ignored(self):
        s   = self._populated()
        row = {f.name: getattr(s, f.name) for f in dc_fields(s)}
        row['future_field_xyz'] = 42
        pd.DataFrame([row]).to_csv(self.m.STATE_FILE, index=False)
        r = self.m.load_state()
        self.assertEqual(r.status, 'in_trade')


if __name__ == '__main__':
    unittest.main(verbosity=2)
