"""Tests for hestia_backtest: trade pairing, ledger statistics and the Fyers-update gap check (all on synthetic data)."""

import datetime
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hestia_backtest'))
import hb_config as cfg  # noqa: E402
import compare_actual as ca  # noqa: E402
import build_ledger as bl  # noqa: E402
import data_check as dc  # noqa: E402


def bt_frame(rows):
    return pd.DataFrame(rows, columns=['direction', 'entry_ts', 'exit_ts', 'entry', 'pts', 'rs', 'reason']).assign(
        entry_ts=lambda d: pd.to_datetime(d['entry_ts']), exit_ts=lambda d: pd.to_datetime(d['exit_ts']))


def live_trade(i, direction, entry_ts, exit_ts, price=100.0, pts=1.0, reasons='trend_flip'):
    return {'trade_id': i, 'direction': direction, 'entry_ts': pd.Timestamp(entry_ts), 'entry_price': price, 'exit_ts': pd.Timestamp(exit_ts), 'reasons': reasons, 'pts': pts}


def test_pair_matches_same_direction_within_tolerance_and_flags_the_rest():
    bt = bt_frame([('bullish', '2026-10-05 09:15', '2026-10-05 12:00', 100.0, 2.0, 40.0, 'trend_flip'),
                   ('bearish', '2026-10-06 10:00', '2026-10-06 11:00', 101.0, 1.0, 20.0, 'stop_loss'),
                   ('bullish', '2026-10-07 10:00', '2026-10-07 11:00', 99.0, 1.0, 20.0, 'trend_flip')])
    live = [live_trade(1, 'bullish', '2026-10-05 09:15:03', '2026-10-05 12:00:02', price=100.2, pts=1.8),
            live_trade(2, 'bullish', '2026-10-06 10:00:01', '2026-10-06 11:00:00')]            # wrong direction for the 10-06 backtest trade: live only
    rows = ca.pair(live, bt, 20)
    st = {(r.get('live_id'), r['status']) for r in rows}
    assert (1, 'matched') in st and (2, 'live only') in st
    assert sum(r['status'] == 'backtest only' for r in rows) == 2         # the 10-06 bearish and the 10-07 bullish trades the live engine did not take


def test_pair_uses_each_backtest_trade_once_and_picks_the_closest():
    bt = bt_frame([('bullish', '2026-10-05 09:15', '2026-10-05 12:00', 100.0, 2.0, 40.0, 'trend_flip'),
                   ('bullish', '2026-10-05 09:30', '2026-10-05 12:30', 100.0, 2.0, 40.0, 'trend_flip')])
    live = [live_trade(1, 'bullish', '2026-10-05 09:29:00', '2026-10-05 12:30:00'), live_trade(2, 'bullish', '2026-10-05 09:16:00', '2026-10-05 12:00:00')]
    rows = {r['live_id']: r for r in ca.pair(live, bt, 20) if 'live_id' in r}
    assert rows[1]['bt_entry_ts'].endswith('09:30:00') and rows[2]['bt_entry_ts'].endswith('09:15:00')


def test_live_trade_after_the_backtest_last_closed_trade_is_not_called_live_only():
    bt = bt_frame([('bullish', '2026-10-05 09:15', '2026-10-05 12:00', 100.0, 2.0, 40.0, 'trend_flip')])
    rows = ca.pair([live_trade(1, 'bullish', '2026-10-09 10:00:00', '2026-10-09 11:00:00')], bt, 20)
    assert rows[0]['status'] == 'not closed in backtest'


def test_reasons_are_compared_after_removing_repeats():
    assert ca.norm_reason('stop_loss/stop_loss') == 'stop_loss' and ca.norm_reason('target1/trend_flip') == 'target1/trend_flip'


def test_ledger_stats_total_drawdown_and_win_rate():
    rs = pd.Series([100.0, -50.0, -80.0, 200.0])
    ts = pd.Series(pd.to_datetime(['2024-01-01', '2024-06-01', '2025-01-01', '2025-06-01']))
    s = bl.stats(rs, ts)
    assert s['total_rs'] == 170 and s['max_dd_rs'] == -130 and s['win_pct'] == 50.0 and s['worst_trade_rs'] == -80


def _stage(tmp, tree, instrument, expiries):
    d = tmp / tree / instrument
    d.mkdir(parents=True, exist_ok=True)
    for e in expiries:
        (d / f'{e}_futures.csv').write_text('time_stamp,open,high,low,close,volume\n')


def test_fyers_update_needed_only_when_the_gap_exceeds_the_usual_spacing(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'ENGINES', {'X': {'instrument': 'X'}})
    monkeypatch.setattr(cfg, 'MCX_DIR', str(tmp_path / 'mcx'))
    monkeypatch.setattr(cfg, 'FYERS_DIR', str(tmp_path / 'fy'))
    monkeypatch.setattr(dc, 'last_complete_session', lambda i: '2026-10-12')
    months = ['2026-01-27', '2026-02-24', '2026-03-26', '2026-04-24', '2026-05-26', '2026-06-25', '2026-07-24', '2026-08-26', '2026-09-25']
    _stage(tmp_path, 'fy', 'X', months)
    _stage(tmp_path, 'mcx', 'X', ['2026-10-27', '2026-11-24'])
    assert dc.check(datetime.date(2026, 10, 13))['X']['fyers_update_needed'] is False       # September staged, October still live
    _stage(tmp_path, 'mcx', 'X', ['2026-12-28'])
    (tmp_path / 'mcx' / 'X' / '2026-10-27_futures.csv').unlink()
    (tmp_path / 'mcx' / 'X' / '2026-11-24_futures.csv').unlink()
    assert dc.check(datetime.date(2026, 10, 13))['X']['fyers_update_needed'] is True        # October and November expiries are neither staged nor live


def test_prometheus_fyers_track_takes_angel_one_only_after_the_last_fyers_day(monkeypatch):
    sys.path.insert(0, os.path.join(cfg.REPO, 'prometheus_backtest', 'phase3_fyers'))
    import data_loader_fyers as dl

    def frame(stamps, src):
        d = pd.DataFrame({'time_stamp': pd.to_datetime(stamps), 'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0, 'volume': 1, 'contract_expiry': 'x'})
        d['data_source'] = src
        return d.set_index('time_stamp')

    fy = frame(['2026-09-18 10:00', '2026-09-21 10:00', '2026-09-21 23:29'], 'fyers')
    ao = frame(['2026-09-18 10:00', '2026-09-21 10:00', '2026-09-22 09:00', '2026-10-12 10:00'], 'angelone')
    monkeypatch.setattr(dl, '_load_fyers_front_month', lambda s: fy)
    monkeypatch.setattr(dl._angelone_loader, 'load_futures_1min', lambda s: ao)
    out = dl.load_futures_1min('CRUDEOILM')
    assert list(out['data_source']) == ['fyers', 'fyers', 'fyers', 'angelone_tail_fill', 'angelone_tail_fill']      # the Angel One rows on 09-18 and 09-21 are not used
    assert out.index.is_monotonic_increasing


def test_live_rupees_per_point_match_the_recorded_trade_rupees():
    # trade CSV rows: Prometheus #72 -178.0 pts (two-lot sum) = -1,780 Rs; Typhon #8 -1.35 pts = -675 Rs; Selene 1 Rs a point; Helios 20 Rs a point
    assert ca.live_rs_per_point(cfg.ENGINES['Prometheus']) == 10
    assert ca.live_rs_per_point(cfg.ENGINES['Typhon']) == 500
    assert ca.live_rs_per_point(cfg.ENGINES['Selene']) == 1
    assert ca.live_rs_per_point(cfg.ENGINES['Helios']) == 20
