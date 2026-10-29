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
    AngelOne-only live data agree) to 2026-09-28. One known, explained boundary case is excluded, not silently dropped: the
    backtest's own unported `forced_roll` artifact (plan section 1, "not ported") re-enters trade 2452 bearish on the new
    contract at 2026-09-02 09:00 at 242,400, a stale direction carried over from 31 Aug with no flip behind it, and exits it at
    18:00 on the flip; the replay engine starts the window flat, so it has no position to exit. (Before the 2026-10-28 standard
    Supertrend formula the same artifact showed up as a replay entry at 2026-09-03 15:30 and a second case, a position still
    open when the backtest's window ends, as an entry at 2026-09-28 20:15; neither appears now.)"""
    from selene_engine.replay_check import replay
    from datetime import date
    rep = replay(date(2026, 9, 28), verbose=False)
    known_boundary = ['live exit at 2026-09-02 18:00:00 trend_flip has no replay counterpart']
    assert rep.differences == known_boundary, rep.differences
    assert rep.matched == len(rep.live) - 1 >= 55
    assert not rep.price_gaps or max(rep.price_gaps) < 0.01     # both simulations use the same 1-minute data: fills should be exact
