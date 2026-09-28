"""
The Supertrend that Prometheus computes live (a copy inside prometheus_functions.py, "production never imports the
backtest") and the one every backtest uses (apollo_production/technical_indicators.py, through
prometheus_backtest/data_loader.compute_st) are two separate copies of the same algorithm. If they ever drift, the
backtests stop describing what production trades, and nothing else would notice. This test pins them together on
seeded random-walk bars with real-looking volatility clustering.

prometheus_functions.py is not imported: importing it pulls in prometheus_configs (instrument override files, logger
setup writing into the real dated log). The two definitions are compiled straight from its source instead.
"""

import ast
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'prometheus_backtest'))


def _load_production_compute_st():
    src = (REPO / 'prometheus_production' / 'prometheus_functions.py').read_text()
    ns = {'pd': pd, 'np': np}
    for node in ast.parse(src).body:
        if ((isinstance(node, ast.ClassDef) and node.name == 'SupertrendIndicator')
                or (isinstance(node, ast.FunctionDef) and node.name == 'compute_st')):
            exec(compile(ast.Module([node], []), 'prometheus_functions', 'exec'), ns)
    return ns['compute_st']


def _bars(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    vol = 0.002 * np.exp(np.cumsum(rng.normal(0, 0.05, n)))          # clustered volatility
    close = 100_000 * np.exp(np.cumsum(rng.normal(0, 1, n) * vol))
    high = close * (1 + np.abs(rng.normal(0, 1, n)) * vol)
    low = close * (1 - np.abs(rng.normal(0, 1, n)) * vol)
    open_ = np.r_[close[0], close[:-1]]
    idx = pd.date_range('2025-01-01 09:00', periods=n, freq='15min')
    return pd.DataFrame({'open': open_, 'high': np.maximum(high, open_), 'low': np.minimum(low, open_),
                         'close': close, 'volume': rng.integers(1, 500, n).astype(float),
                         'contract_expiry': 'x'}, index=idx)


@pytest.mark.parametrize('period,multiplier', [(10, 2.0), (10, 2.5), (10, 3.0), (7, 2.5)])
@pytest.mark.parametrize('seed', [1, 2, 3])
def test_production_and_backtest_supertrend_agree(period, multiplier, seed):
    import data_loader as backtest_loader
    prod = _load_production_compute_st()
    bars = _bars(1500, seed)

    b = backtest_loader.compute_st(bars, period, multiplier)
    p = prod(bars.reset_index().rename(columns={'index': 'time_stamp'}), period, multiplier)

    np.testing.assert_allclose(pd.to_numeric(b['supertrend'], errors='coerce').to_numpy(),
                               p['supertrend'].to_numpy(), rtol=0, atol=1e-9, equal_nan=True)
    assert [None if pd.isna(x) else bool(x) for x in b['trend']] == [None if pd.isna(x) else bool(x) for x in p['trend']]
    assert (b['trend_flip'].to_numpy() == p['trend_flip'].to_numpy()).all()
    assert int(b['trend_flip'].sum()) > 0, 'test bars produced no flips; the check would be vacuous'


@pytest.mark.parametrize('period,multiplier', [(10, 2.0), (10, 2.5), (10, 3.0), (7, 2.5)])
@pytest.mark.parametrize('seed', [1, 2, 3])
def test_hestia_supertrend_agrees_with_production(period, multiplier, seed):
    """hestia_core.indicators.compute_st is the third copy (the Hestia data service's single implementation)."""
    sys.path.insert(0, str(REPO))
    from hestia_core.indicators import compute_st as hestia_compute_st
    prod = _load_production_compute_st()
    bars = _bars(1500, seed)
    frame = bars.reset_index().rename(columns={'index': 'time_stamp'})

    h = hestia_compute_st(frame, period, multiplier)
    p = prod(frame, period, multiplier)

    np.testing.assert_allclose(h['supertrend'].to_numpy(), p['supertrend'].to_numpy(), rtol=0, atol=1e-9, equal_nan=True)
    assert [None if pd.isna(x) else bool(x) for x in h['trend']] == [None if pd.isna(x) else bool(x) for x in p['trend']]
    assert (h['trend_flip'].to_numpy() == p['trend_flip'].to_numpy()).all()
    assert int(h['trend_flip'].sum()) > 0, 'test bars produced no flips; the check would be vacuous'
