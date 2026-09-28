"""P7.2: the Selene engine replayed on the fake Hestia over REAL SILVERMIC 1-minute data, compared trade for trade against
selene_backtest/parity_backtest_selene.py's own deterministic output. Skips itself when the pipeline SILVERMIC data or the
parity backtest's data_sweep output is absent."""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MCX_DIR = REPO / 'data_pipeline' / 'data' / 'mcx' / 'SILVERMIC'
SWEEP_DIR = REPO / 'selene_backtest' / 'data_sweep'
needs_data = pytest.mark.skipif(
    not (MCX_DIR / '2026-11-30_futures.csv').exists() or not (SWEEP_DIR / 'parity_trades.csv').exists(),
    reason='the pipeline SILVERMIC data or selene_backtest/data_sweep/parity_trades.csv is not present')


@needs_data
def test_the_engine_reproduces_every_live_decision_over_the_angelone_only_window():
    """From 2026-09-02 (selene_configs.ANGELONE_OWN_FROM, where the backtest's Fyers-preferred blend and Hestia's own
    AngelOne-only live data agree) to 2026-09-28. Two known, explained boundary cases are excluded, not silently dropped:
    a stale-direction entry carried from before the window by the backtest's own unported `forced_roll` artifact (plan
    section 1, "not ported"), and the position still open when the backtest's own simulation window ends (its CSVs record
    only closed legs, per `open_at_end` in `parity_backtest_selene.simulate`'s own output)."""
    from selene_engine.replay_check import replay
    from datetime import date
    rep = replay(date(2026, 9, 28), verbose=False)
    known_boundary = {'replay entry at 2026-09-03 15:30:00.250000 bearish has no live counterpart',
                      'replay entry at 2026-09-28 20:15:00.250000 bearish has no live counterpart'}
    unexplained = [d for d in rep.differences if d not in known_boundary]
    assert not unexplained, unexplained
    assert rep.matched == len(rep.live) >= 55
    assert not rep.price_gaps or max(rep.price_gaps) < 0.01     # both simulations use the same 1-minute data: fills should be exact
