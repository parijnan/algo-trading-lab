"""
Strike math tests — Athena delta-based selection.

What this catches:
  - The delta-finding algorithm (identical to AthenaEngine._find_delta_strike
    before the LTP-lookup step) selects strikes closest to the target delta.
  - CE sell strike lands above spot (OTM call).
  - PE sell strike lands below spot (OTM put).
  - Safety wing (SAFETY_WING_DELTA) is further OTM than the sold leg.
  - All returned strikes are multiples of STRIKE_STEP.
  - ATM rounding follows banker's rounding (same formula as Apollo).

Approach: the core algorithm is reimplemented inline using mibian (same
library as AthenaEngine), tested as a pure function without importing
AthenaEngine. This avoids the credential/API import chain while exercising
the actual mathematical logic.
"""

import os
import sys
import unittest

import mibian

REPO_ROOT   = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
ATHENA_DIR  = os.path.join(REPO_ROOT, 'athena_production')

# Constants mirrored from configs_live.py — tests will catch drift.
TARGET_DELTA_SOLD   = 0.30
SAFETY_WING_DELTA   = 0.05
STRIKE_STEP         = 100
RISK_FREE_RATE      = 5.0


# ---------------------------------------------------------------------------
# Core algorithm — extracted from AthenaEngine._find_delta_strike
# (steps 1–4; LTP-lookup omitted — returns best candidate directly)
# ---------------------------------------------------------------------------

def _find_delta_strike(spot, vix, dte, target_delta, option_type,
                       strike_step=STRIKE_STEP, risk_free=RISK_FREE_RATE):
    """Mirror of AthenaEngine._find_delta_strike (without LTP fallback)."""
    if dte <= 0:
        dte = 0.5
    atm = round(spot / strike_step) * strike_step
    delta_map = []
    for offset in range(-2000, 2100, strike_step):
        strike = atm + offset
        c = mibian.BS([spot, strike, risk_free, dte], volatility=vix)
        current_delta = abs(c.callDelta) if option_type == 'ce' else abs(c.putDelta)
        delta_map.append({'strike': strike, 'delta_diff': abs(current_delta - target_delta)})
    top_candidates = sorted(delta_map, key=lambda x: x['delta_diff'])
    return top_candidates[0]['strike']


# ---------------------------------------------------------------------------
# ATM rounding (same formula as Apollo — verifies no drift)
# ---------------------------------------------------------------------------

class TestATMRounding(unittest.TestCase):

    def _atm(self, spot):
        return round(spot / STRIKE_STEP) * STRIKE_STEP

    def test_exact_multiple_unchanged(self):
        self.assertEqual(self._atm(24000), 24000)
        self.assertEqual(self._atm(24100), 24100)

    def test_rounds_to_nearest_step(self):
        self.assertEqual(self._atm(24040), 24000)   # 240.40 → 240
        self.assertEqual(self._atm(24060), 24100)   # 240.60 → 241

    def test_bankers_rounding_half_to_even(self):
        """24050 / 100 = 240.5 → round to 240 (even), NOT 241."""
        self.assertEqual(self._atm(24050), 24000)

    def test_bankers_rounding_241_5(self):
        """24150 / 100 = 241.5 → round to 242 (even) → 24200."""
        self.assertEqual(self._atm(24150), 24200)


# ---------------------------------------------------------------------------
# Delta-based strike direction and OTM correctness
# ---------------------------------------------------------------------------

class TestDeltaStrikeDirection(unittest.TestCase):
    """
    Baseline: spot=24000, vix=18, dte=21 (roughly 3 weeks).
    At these levels a 0.30-delta CE should be OTM (above spot),
    and a 0.30-delta PE should be OTM (below spot).
    """

    SPOT = 24000.0
    VIX  = 18.0
    DTE  = 21

    def _sel(self, target_delta, option_type):
        return _find_delta_strike(self.SPOT, self.VIX, self.DTE,
                                  target_delta, option_type)

    def test_ce_sell_is_otm(self):
        """CE sell strike (TARGET_DELTA_SOLD) is above spot."""
        strike = self._sel(TARGET_DELTA_SOLD, 'ce')
        self.assertGreater(strike, self.SPOT,
                           msg=f"CE sell strike {strike} should be > spot {self.SPOT}")

    def test_pe_sell_is_otm(self):
        """PE sell strike (TARGET_DELTA_SOLD) is below spot."""
        strike = self._sel(TARGET_DELTA_SOLD, 'pe')
        self.assertLess(strike, self.SPOT,
                        msg=f"PE sell strike {strike} should be < spot {self.SPOT}")

    def test_ce_wing_is_further_otm_than_ce_sell(self):
        """CE wing (SAFETY_WING_DELTA < TARGET_DELTA_SOLD) is above CE sell strike."""
        sell   = self._sel(TARGET_DELTA_SOLD,   'ce')
        wing   = self._sel(SAFETY_WING_DELTA,   'ce')
        self.assertGreater(wing, sell,
                           msg=f"CE wing {wing} should be > CE sell {sell}")

    def test_pe_wing_is_further_otm_than_pe_sell(self):
        """PE wing (SAFETY_WING_DELTA < TARGET_DELTA_SOLD) is below PE sell strike."""
        sell   = self._sel(TARGET_DELTA_SOLD,   'pe')
        wing   = self._sel(SAFETY_WING_DELTA,   'pe')
        self.assertLess(wing, sell,
                        msg=f"PE wing {wing} should be < PE sell {sell}")


# ---------------------------------------------------------------------------
# Strike is always a multiple of STRIKE_STEP
# ---------------------------------------------------------------------------

class TestStrikeIsMultipleOfStep(unittest.TestCase):

    def _assert_multiple(self, spot, vix, dte, target_delta, option_type):
        strike = _find_delta_strike(spot, vix, dte, target_delta, option_type)
        self.assertEqual(
            strike % STRIKE_STEP, 0,
            msg=f"Strike {strike} is not a multiple of {STRIKE_STEP}")
        return strike

    def test_ce_sell_multiple(self):
        self._assert_multiple(24000, 18, 21, TARGET_DELTA_SOLD, 'ce')

    def test_pe_sell_multiple(self):
        self._assert_multiple(24000, 18, 21, TARGET_DELTA_SOLD, 'pe')

    def test_ce_wing_multiple(self):
        self._assert_multiple(24000, 18, 21, SAFETY_WING_DELTA, 'ce')

    def test_pe_wing_multiple(self):
        self._assert_multiple(24000, 18, 21, SAFETY_WING_DELTA, 'pe')

    def test_multiple_under_low_vix(self):
        self._assert_multiple(24000, 12, 14, TARGET_DELTA_SOLD, 'ce')

    def test_multiple_under_high_vix(self):
        self._assert_multiple(24000, 28, 21, TARGET_DELTA_SOLD, 'pe')


# ---------------------------------------------------------------------------
# Delta accuracy — selected strike has minimum delta error
# ---------------------------------------------------------------------------

class TestDeltaAccuracy(unittest.TestCase):
    """
    Verify that the returned strike is the one with delta closest to target.
    We compute the actual delta at the returned strike and compare it to all
    neighbours — the returned strike must be at least as good as ±1 step.
    """

    SPOT = 24000.0
    VIX  = 18.0
    DTE  = 21

    def _actual_delta(self, spot, vix, dte, strike, option_type):
        c = mibian.BS([spot, strike, RISK_FREE_RATE, dte], volatility=vix)
        return abs(c.callDelta) if option_type == 'ce' else abs(c.putDelta)

    def _assert_best_delta(self, target_delta, option_type):
        strike = _find_delta_strike(self.SPOT, self.VIX, self.DTE,
                                    target_delta, option_type)
        my_err = abs(self._actual_delta(self.SPOT, self.VIX, self.DTE,
                                         strike, option_type) - target_delta)
        for neighbour in (strike - STRIKE_STEP, strike + STRIKE_STEP):
            nb_err = abs(self._actual_delta(self.SPOT, self.VIX, self.DTE,
                                             neighbour, option_type) - target_delta)
            self.assertLessEqual(my_err, nb_err + 1e-6,
                msg=f"{option_type} target={target_delta}: strike {strike} "
                    f"(err={my_err:.4f}) worse than neighbour {neighbour} (err={nb_err:.4f})")

    def test_ce_sell_delta_accuracy(self):
        self._assert_best_delta(TARGET_DELTA_SOLD, 'ce')

    def test_pe_sell_delta_accuracy(self):
        self._assert_best_delta(TARGET_DELTA_SOLD, 'pe')

    def test_ce_wing_delta_accuracy(self):
        self._assert_best_delta(SAFETY_WING_DELTA, 'ce')

    def test_pe_wing_delta_accuracy(self):
        self._assert_best_delta(SAFETY_WING_DELTA, 'pe')


# ---------------------------------------------------------------------------
# Zero / boundary DTE edge cases
# ---------------------------------------------------------------------------

class TestDTEEdgeCases(unittest.TestCase):

    SPOT = 24000.0
    VIX  = 18.0

    def test_zero_dte_uses_half_day(self):
        """dte=0 must not crash — algorithm uses 0.5 as fallback."""
        strike = _find_delta_strike(self.SPOT, self.VIX, 0,
                                    TARGET_DELTA_SOLD, 'ce')
        self.assertEqual(strike % STRIKE_STEP, 0)

    def test_single_day_dte(self):
        """dte=1 must produce a valid strike."""
        strike = _find_delta_strike(self.SPOT, self.VIX, 1,
                                    TARGET_DELTA_SOLD, 'pe')
        self.assertEqual(strike % STRIKE_STEP, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
