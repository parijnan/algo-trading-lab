"""
The Supertrend is pinned to real Zerodha output: 250 CRUDEOILM 15-minute bars (22-28 Oct 2026) exported from a Kite chart with
Supertrend(10, 2.5) switched on, in tests/fixtures/zerodha_crudeoilm_15m_st_10_2.5.csv. All three copies of compute_st
(hestia_core, the backtest loader, prometheus_functions) must reproduce Zerodha's column. The slice starts cold, so its first bars
still carry the ATR warm-up difference; from bar 60 on the lines agree to the two decimals Zerodha prints.

On 28 Oct 2026 Hestia's previous ratchet kept the band at 9753.38 where Zerodha's stayed at 9735.20, so Zerodha flipped bullish on
the 19:45 bar (close 9748) and Hestia did not until 20:30.
"""

import ast
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'prometheus_backtest'))

FIXTURE = REPO / 'tests' / 'fixtures' / 'zerodha_crudeoilm_15m_st_10_2.5.csv'
SETTLED_FROM = 60


def _production_compute_st():
    src = (REPO / 'prometheus_production' / 'prometheus_functions.py').read_text()
    ns = {'pd': pd, 'np': np}
    for node in ast.parse(src).body:
        if ((isinstance(node, ast.ClassDef) and node.name == 'SupertrendIndicator')
                or (isinstance(node, ast.FunctionDef) and node.name == 'compute_st')):
            exec(compile(ast.Module([node], []), 'prometheus_functions', 'exec'), ns)
    return ns['compute_st']


def _hestia(frame):
    from hestia_core.indicators import compute_st
    return compute_st(frame, 10, 2.5)


def _backtest(frame):
    import data_loader
    out = data_loader.compute_st(frame.set_index(pd.to_datetime(frame['time_stamp'])), 10, 2.5)
    return out.reset_index(drop=True)


def _production(frame):
    return _production_compute_st()(frame, 10, 2.5)


@pytest.fixture(scope='module')
def zerodha():
    return pd.read_csv(FIXTURE)


@pytest.mark.parametrize('compute', [_hestia, _backtest, _production], ids=['hestia', 'backtest', 'production'])
def test_supertrend_matches_zerodha_column(zerodha, compute):
    result = compute(zerodha)
    diff = (pd.to_numeric(result['supertrend'], errors='coerce') - zerodha['zerodha_st']).abs().iloc[SETTLED_FROM:]
    assert diff.max() <= 0.01, f'worst difference {diff.max():.3f} at {zerodha.loc[diff.idxmax(), "time_stamp"]}'


@pytest.mark.parametrize('compute', [_hestia, _backtest, _production], ids=['hestia', 'backtest', 'production'])
def test_28_oct_2026_band_holds_and_flips_bullish_at_1945(zerodha, compute):
    result = compute(zerodha).set_index(zerodha['time_stamp'])
    after_flip_down = result.loc['2026-10-28 15:15:00':'2026-10-28 19:30:00']
    assert np.allclose(pd.to_numeric(after_flip_down['supertrend']), 9735.20, atol=0.01)
    assert not after_flip_down['trend'].astype(bool).any()
    assert bool(result.loc['2026-10-28 15:15:00', 'trend_flip'])
    flip = result.loc['2026-10-28 19:45:00']
    assert bool(flip['trend_flip']) and bool(flip['trend']) is True
    assert not result.loc['2026-10-28 15:30:00':'2026-10-28 19:30:00', 'trend_flip'].any()
