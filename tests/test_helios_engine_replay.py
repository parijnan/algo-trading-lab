"""P8.2: the Helios engine replayed on the fake Hestia over REAL GOLDPETAL 1-minute data, compared trade for trade against
helios_backtest/parity_backtest_helios.py's own deterministic output. Skips itself when the pipeline GOLDPETAL data or the
parity backtest's data_sweep output is absent. Mirrors Selene's own test_selene_engine_replay.py."""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MCX_DIR = REPO / 'data_pipeline' / 'data' / 'mcx' / 'GOLDPETAL'
SWEEP_DIR = REPO / 'helios_backtest' / 'data_sweep'
needs_data = pytest.mark.skipif(
    not (MCX_DIR / '2026-10-30_futures.csv').exists() or not (SWEEP_DIR / 'parity_trades.csv').exists(),
    reason='the pipeline GOLDPETAL data or helios_backtest/data_sweep/parity_trades.csv is not present')


@needs_data
def test_the_engine_reproduces_every_live_decision_over_the_angelone_only_window():
    """From 2026-09-02 (helios_configs.ANGELONE_OWN_FROM, where the backtest's Fyers-preferred blend and Hestia's own
    AngelOne-only live data agree) to 2026-09-25 (helios_configs.PARITY_END_EXTENDED). Two known, explained boundary
    cases are excluded, not silently dropped -- verified directly against parity_trades.csv/parity_legs.csv (2026-09-29):
    (1) trade 1128's rollover leg (2026-09-23 23:15, contract switching to 2026-10-30) is a MID-trade leg -- 'live
    decisions' are built from parity_trades.csv's overall per-trade entry/exit only (not parity_legs.csv's individual
    leg entries), so an intermediate rollover exit+reopen never appears as its own live decision, even though the
    replay engine correctly performs it (same category as Selene's own excluded 'forced_roll' boundary case); (2) the
    entry at 2026-09-25 19:00 opens trade 1130, which is still open when the backtest's own simulation window ends
    (PARITY_END_EXTENDED) -- parity_trades.csv only records closed trades, so this entry has no 'live' counterpart to
    match against (same category as Selene's own excluded 'still open at window end' case)."""
    from helios_engine.replay_check import replay
    from datetime import date
    rep = replay(date(2026, 9, 25), verbose=False)
    known_boundary = {'replay exit at 2026-09-23 23:16:00.250000 rollover has no live counterpart',
                      'replay entry at 2026-09-23 23:16:00.450000 bearish has no live counterpart',
                      'replay entry at 2026-09-25 19:00:00.250000 bearish has no live counterpart'}
    unexplained = [d for d in rep.differences if d not in known_boundary]
    assert not unexplained, unexplained
    assert rep.matched == len(rep.live) >= 20
    assert not rep.price_gaps or max(rep.price_gaps) < 0.01     # both simulations use the same 1-minute data: fills should be exact
