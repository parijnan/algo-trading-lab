"""
Regression test — Iris §8 Path A seeding (1-min reconstruction -> 5m -> 15m).

Context: plans/iris-signal-pipeline-hardening.md §8. Before wiring
_build_past_days_5m into production, its output was verified bar-for-bar
against the live AngelOne chart for 2026-08-03 through 2026-08-07 (the CAS-era
window) — matched to the decimal except 3 flip-bar values (1.5-11 point
drift), which trace to Supertrend's "dormant band" property (confirmed not a
data or reconstruction bug: identical result seeding from 6 weeks vs. the
full 7-year history, and confirmed self-correcting over subsequent bars).
That chart-validated output is frozen in research/iris_st_verification/ as
the reference this test replays production code against.

This exercises the *actual* production functions (_build_past_days_5m,
_resample_to_15m, compute_st from iris_functions.py), not a reimplementation
-- it verifies what actually ships, not the standalone verification script
that originally produced the reference CSVs.

Two things this does NOT test:
  - Path B (today's live 5-min polling) -- needs a live broker session.
  - The 3 known flip-bar mismatches -- those are chart-vs-our-cold-start
    differences (see §8), not a code correctness question; the reference
    CSVs already encode the chart-validated (not ours-only) values there.

Skips (does not fail) if the machine's nifty.csv doesn't have the
2026-08-03..08-07 window fully reconstructed -- this file is gitignored and
machine-specific (see CLAUDE.md's local-vs-Delos data note), so a fresh
checkout or a machine that hasn't run the CAS backfill can't run this check.
"""

import os
import sys
import unittest

import pandas as pd

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
IRIS_DIR  = os.path.join(REPO_ROOT, 'iris_production')
REF_DIR   = os.path.join(REPO_ROOT, 'research', 'iris_st_verification')

REF_5M  = os.path.join(REF_DIR, 'iris_st5_20260803_onward.csv')
REF_15M = os.path.join(REF_DIR, 'iris_st15_20260803_onward.csv')

# Reference window is fixed to the days the chart was actually checked
# against -- not "from 2026-08-03 to whenever nifty.csv currently ends".
CUTOFF   = pd.Timestamp('2026-08-03')
WINDOW_END_DATE = pd.Timestamp('2026-08-08')  # exclusive; treats 08-03..08-07 as past days

# ST value tolerance: real regressions found in this investigation were
# 1.5-11 points (dormant-band drift at a genuine code/data bug); the only
# benign residual observed between equivalent resample paths was ~9.25e-05
# (two-step 1m->5m->15m vs. one-step 1m->15m floating-point noise). 1e-2
# comfortably separates the two by two orders of magnitude.
ST_TOLERANCE = 1e-2

# The 3 flip-bar mismatches already accepted as expected Supertrend behavior
# (dormant-band cold-start difference vs. the chart) -- excluded here since
# the reference CSVs encode the CHART's value at those bars, not what any
# from-scratch recomputation (ours or a future one) would independently
# produce. Re-verifying them would be re-litigating an already-closed,
# understood discrepancy, not catching a regression.
KNOWN_FLIP_BAR_EXCEPTIONS = {
    ('5m',  pd.Timestamp('2026-08-06 13:10:00')),
    ('15m', pd.Timestamp('2026-08-04 15:15:00')),
    ('15m', pd.Timestamp('2026-08-07 10:15:00')),
}


class TestIrisPathASeeding(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(REF_5M) or not os.path.exists(REF_15M):
            raise unittest.SkipTest('reference CSVs missing from research/iris_st_verification/')

        sys.path.insert(0, IRIS_DIR)
        from iris_configs import NIFTY_INDEX_CSV, ST_PERIOD, ST_MULTIPLIER
        from iris_functions import _build_past_days_5m, _resample_to_15m, compute_st

        if not os.path.exists(NIFTY_INDEX_CSV):
            raise unittest.SkipTest(f'{NIFTY_INDEX_CSV} not present on this machine (gitignored, '
                                     f'machine-specific data file)')

        now = WINDOW_END_DATE.replace(hour=9, minute=15).to_pydatetime()
        df_5m_raw = _build_past_days_5m(now)
        if df_5m_raw.empty:
            raise unittest.SkipTest('no past-day data available for the reference window '
                                     '(nifty.csv does not cover 2026-08-03..08-07)')
        df_15m_raw = _resample_to_15m(df_5m_raw)

        cls.prod_5m  = compute_st(df_5m_raw, ST_PERIOD, ST_MULTIPLIER)
        cls.prod_15m = compute_st(df_15m_raw, ST_PERIOD, ST_MULTIPLIER)
        cls.ref_5m   = pd.read_csv(REF_5M, parse_dates=['time_stamp'])
        cls.ref_15m  = pd.read_csv(REF_15M, parse_dates=['time_stamp'])

    def _compare(self, label, prod, ref):
        prod = prod[prod['time_stamp'] >= CUTOFF].reset_index(drop=True)
        ref  = ref[ref['time_stamp'] >= CUTOFF].reset_index(drop=True)

        self.assertEqual(len(prod), len(ref),
                          f'{label}: row count mismatch (production={len(prod)}, reference={len(ref)})')

        merged = prod.merge(ref, on='time_stamp', suffixes=('_prod', '_ref'))

        for col in ('open', 'high', 'low', 'close'):
            diff = (merged[f'{col}_prod'] - merged[f'{col}_ref']).abs()
            bad = merged[diff > 1e-9]
            self.assertTrue(bad.empty,
                             f'{label}: OHLC column {col!r} mismatch at '
                             f'{bad["time_stamp"].tolist()} -- reconstruction/resampling changed, '
                             f'not just floating-point noise')

        excluded_ts = {ts for (tf, ts) in KNOWN_FLIP_BAR_EXCEPTIONS if tf == label}
        checkable = merged[~merged['time_stamp'].isin(excluded_ts)]
        diff = (checkable['supertrend_prod'] - checkable['supertrend_ref']).abs()
        bad = checkable[diff > ST_TOLERANCE]
        self.assertTrue(bad.empty,
                         f'{label}: supertrend mismatch beyond {ST_TOLERANCE} at '
                         f'{bad["time_stamp"].tolist()} -- diffs: {diff[diff > ST_TOLERANCE].tolist()}')

    def test_5m_matches_chart_validated_reference(self):
        self._compare('5m', self.prod_5m, self.ref_5m)

    def test_15m_matches_chart_validated_reference(self):
        self._compare('15m', self.prod_15m, self.ref_15m)


if __name__ == '__main__':
    unittest.main()
