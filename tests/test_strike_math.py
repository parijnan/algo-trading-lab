"""
Strike selection math tests — Apollo.

What this catches:
  - ATM calculation: Python round() uses banker's rounding (round-half-to-even).
    round(497.5) = 498, round(498.5) = 498 — so spot 24875 AND 24925 both map to
    ATM 24900 (not 24950), which is a counter-intuitive but correct result.
  - Bullish: buy = ATM + BUY_LEG_OFFSET = ATM − 50 (ITM),
             sell = buy  + HEDGE_POINTS  = buy + 300 (OTM).
  - Bearish: buy = ATM − BUY_LEG_OFFSET = ATM + 50 (ITM),
             sell = buy  − HEDGE_POINTS  = buy − 300 (OTM).
  - Symbol/token lookup: correct row selected from a synthetic instrument_df.
  - Missing buy or sell strike → _select_strikes returns None.
  - ATM boundary shift: small spot differences straddle a .5 boundary and can
    shift ATM by one step, moving both legs by STRIKE_STEP.

Approach: Apollo is imported with sys.path manipulation, then instantiated via
object.__new__ (skips __init__ — no API connection needed). instrument_df is
patched to a minimal synthetic DataFrame.
"""

import os
import sys
import unittest
import pandas as pd
from datetime import date

REPO_ROOT  = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
APOLLO_DIR = os.path.join(REPO_ROOT, 'apollo_production')

# Add paths once; safe to do at module level since Apollo imports succeed cleanly.
if APOLLO_DIR not in sys.path:
    sys.path.insert(0, APOLLO_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from apollo import Apollo                              # noqa: E402  (after sys.path setup)
from apollo_configs import STRIKE_STEP, BUY_LEG_OFFSET, HEDGE_POINTS  # noqa: E402

_EXPIRY = date(2026, 5, 29)


def _make_df(expiry_date, ce_strikes=(), pe_strikes=()):
    """Build a minimal synthetic instrument_df."""
    expiry_str = expiry_date.strftime('%d%b%Y').upper()
    rows = []
    for s in ce_strikes:
        rows.append({
            'expiry': expiry_str,
            'strike': s * 100,
            'symbol': f'NIFTY{expiry_str}{s}CE',
            'token':  str(s),
        })
    for s in pe_strikes:
        rows.append({
            'expiry': expiry_str,
            'strike': s * 100,
            'symbol': f'NIFTY{expiry_str}{s}PE',
            'token':  str(s + 1),     # distinct from CE tokens
        })
    return pd.DataFrame(rows)


def _bare_apollo(df):
    """Return an Apollo instance with instrument_df set, no __init__ side-effects."""
    a = object.__new__(Apollo)
    a.instrument_df = df
    return a


# ---------------------------------------------------------------------------
# ATM rounding
# ---------------------------------------------------------------------------

class TestATMRounding(unittest.TestCase):
    """
    The inline ATM formula in _select_strikes is:
        atm = round(spot / STRIKE_STEP) * STRIKE_STEP
    These tests verify exact-multiple, normal rounding, and banker's rounding behaviour.
    """

    def _atm(self, spot):
        return round(spot / STRIKE_STEP) * STRIKE_STEP

    def test_exact_multiples(self):
        """Spot already on a strike boundary → unchanged."""
        self.assertEqual(self._atm(24800.0), 24800)
        self.assertEqual(self._atm(24850.0), 24850)
        self.assertEqual(self._atm(25000.0), 25000)

    def test_rounds_up(self):
        """Spot above the midpoint between two strikes → higher strike."""
        self.assertEqual(self._atm(24840.0), 24850)   # 496.80 → 497 → 24850
        self.assertEqual(self._atm(24876.0), 24900)   # 497.52 → 498 → 24900

    def test_rounds_down(self):
        """Spot below the midpoint between two strikes → lower strike."""
        self.assertEqual(self._atm(24810.0), 24800)   # 496.20 → 496 → 24800
        self.assertEqual(self._atm(24824.0), 24800)   # 496.48 → 496 → 24800

    def test_bankers_rounding_497_5(self):
        """24875 / 50 = 497.5 → round to 498 (even) → 24900."""
        self.assertEqual(self._atm(24875.0), 24900)

    def test_bankers_rounding_498_5(self):
        """24925 / 50 = 498.5 → round to 498 (even) → 24900, NOT 24950."""
        self.assertEqual(self._atm(24925.0), 24900)

    def test_bankers_rounding_496_5(self):
        """24825 / 50 = 496.5 → round to 496 (even) → 24800."""
        self.assertEqual(self._atm(24825.0), 24800)

    def test_bankers_rounding_499_5(self):
        """24975 / 50 = 499.5 → round to 500 (even) → 25000."""
        self.assertEqual(self._atm(24975.0), 25000)


# ---------------------------------------------------------------------------
# Strike pair selection
# ---------------------------------------------------------------------------

class TestStrikePairGeneration(unittest.TestCase):
    """
    Tests call Apollo._select_strikes() directly on a bare Apollo instance
    so that the real constants (BUY_LEG_OFFSET, HEDGE_POINTS, STRIKE_STEP)
    and the real lookup logic are exercised.

    Baseline: spot=24840.0 → ATM=24850
        bullish CE: buy=24800 (ATM−50), sell=25100 (buy+300)
        bearish PE: buy=24900 (ATM+50), sell=24600 (buy−300)
    """

    def setUp(self):
        self.expiry = _EXPIRY
        self.spot   = 24840.0
        self.df = _make_df(
            self.expiry,
            ce_strikes=[24800, 24850, 25100, 25150],
            pe_strikes=[24600, 24900],
        )
        self.apollo = _bare_apollo(self.df)

    def _strikes(self, direction, spot=None):
        return self.apollo._select_strikes(
            direction, spot or self.spot, self.expiry)

    # --- bullish ---

    def test_bullish_buy_is_atm_minus_offset(self):
        """Bullish buy strike = ATM + BUY_LEG_OFFSET = ATM − 50."""
        result = self._strikes('bullish')
        self.assertIsNotNone(result)
        buy_strike = result[0]
        expected_atm = round(self.spot / STRIKE_STEP) * STRIKE_STEP
        self.assertEqual(buy_strike, int(expected_atm + BUY_LEG_OFFSET))

    def test_bullish_spread_width(self):
        """Bullish sell − buy = HEDGE_POINTS."""
        result = self._strikes('bullish')
        self.assertIsNotNone(result)
        self.assertEqual(result[1] - result[0], HEDGE_POINTS)

    def test_bullish_option_type_is_ce(self):
        result = self._strikes('bullish')
        self.assertEqual(result[2], 'ce')

    def test_bullish_absolute_strikes(self):
        """Exact strike values for spot=24840 (ATM=24850)."""
        result = self._strikes('bullish')
        self.assertEqual(result[0], 24800)
        self.assertEqual(result[1], 25100)

    # --- bearish ---

    def test_bearish_buy_is_atm_plus_offset(self):
        """Bearish buy strike = ATM − BUY_LEG_OFFSET = ATM + 50."""
        result = self._strikes('bearish')
        self.assertIsNotNone(result)
        buy_strike = result[0]
        expected_atm = round(self.spot / STRIKE_STEP) * STRIKE_STEP
        self.assertEqual(buy_strike, int(expected_atm - BUY_LEG_OFFSET))

    def test_bearish_spread_width(self):
        """Bearish buy − sell = HEDGE_POINTS."""
        result = self._strikes('bearish')
        self.assertIsNotNone(result)
        self.assertEqual(result[0] - result[1], HEDGE_POINTS)

    def test_bearish_option_type_is_pe(self):
        result = self._strikes('bearish')
        self.assertEqual(result[2], 'pe')

    def test_bearish_absolute_strikes(self):
        """Exact strike values for spot=24840 (ATM=24850)."""
        result = self._strikes('bearish')
        self.assertEqual(result[0], 24900)
        self.assertEqual(result[1], 24600)

    # --- symbol / token lookup ---

    def test_symbol_token_lookup_bullish(self):
        """Correct symbol and token are returned for the bullish legs."""
        result = self._strikes('bullish')
        self.assertIsNotNone(result)
        _, _, _, buy_sym, buy_tok, sell_sym, sell_tok = result
        self.assertTrue(buy_sym.endswith('CE'),  buy_sym)
        self.assertTrue(sell_sym.endswith('CE'), sell_sym)
        self.assertIn('24800', buy_sym)
        self.assertIn('25100', sell_sym)
        # tokens were set as str(strike) for CE
        self.assertEqual(buy_tok, '24800')
        self.assertEqual(sell_tok, '25100')

    # --- missing strikes ---

    def test_missing_buy_strike_returns_none(self):
        """If buy leg is absent from instrument_df, _select_strikes returns None."""
        df_no_buy = _make_df(self.expiry, ce_strikes=[25100], pe_strikes=[24600, 24900])
        result = _bare_apollo(df_no_buy)._select_strikes('bullish', self.spot, self.expiry)
        self.assertIsNone(result)

    def test_missing_sell_strike_returns_none(self):
        """If sell leg is absent from instrument_df, _select_strikes returns None."""
        df_no_sell = _make_df(self.expiry, ce_strikes=[24800], pe_strikes=[24600, 24900])
        result = _bare_apollo(df_no_sell)._select_strikes('bullish', self.spot, self.expiry)
        self.assertIsNone(result)

    def test_both_legs_missing_returns_none(self):
        df_empty = _make_df(self.expiry, ce_strikes=[], pe_strikes=[24600, 24900])
        result = _bare_apollo(df_empty)._select_strikes('bullish', self.spot, self.expiry)
        self.assertIsNone(result)

    # --- ATM boundary shift ---

    def test_atm_shift_at_banker_boundary(self):
        """
        spot=24875 → ATM=24900 (banker's rounds 497.5 to 498 = even).
        spot=24925 → ATM=24900 (banker's rounds 498.5 to 498 = even).
        Both spots give identical strike pairs: buy=24850, sell=25150.
        """
        for spot in (24875.0, 24925.0):
            with self.subTest(spot=spot):
                result = self.apollo._select_strikes('bullish', spot, self.expiry)
                self.assertIsNotNone(result, f"Strike lookup failed for spot={spot}")
                self.assertEqual(result[0], 24850, f"spot={spot}: expected buy=24850")
                self.assertEqual(result[1], 25150, f"spot={spot}: expected sell=25150")


if __name__ == '__main__':
    unittest.main(verbosity=2)
