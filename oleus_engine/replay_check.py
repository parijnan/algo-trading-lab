"""
Replay parity of the Oleus engine against the research machine (plan step 3, the gate before paper trading).

For each of the last N CRUDEOILM sessions of the backtest (`janus_data.load_sessions`, previous-session levels), the engine runs on the fake
Hestia over the session's real one-minute bars. The price it sees follows the SAME assumed path inside a minute that the backtest walks (open,
low, high, close on a rising bar, open, high, low, close on a falling one; one point per 15 seconds), so the comparison is exact in order
rather than statistical. The fill prices differ by design (the engine trades at the market after the touch, the backtest at the level), so
trades are compared by setup, entry minute, exit reason and exit minute of each lot; prices are summarised as the shortfall.

    python -m oleus_engine.replay_check [--sessions 30] [--end 2026-10-28]
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'janus_backtest'))

from hestia_core.fake import FakeConfig, FakeHestia              # noqa: E402
from hestia_core.interface import ContractRef                    # noqa: E402
from hestia_core.replay import ContractSpec, ReplayData          # noqa: E402
from oleus_engine.engine import OleusEngine                      # noqa: E402
from oleus_engine.engine_configs import DEFAULT                  # noqa: E402

CFG = dataclasses.replace(DEFAULT, instrument='XX')


class PathData(ReplayData):
    """The price at second s of a minute is the s//15-th point of the minute bar's assumed path; the session close is one minute after the last bar."""

    def _price_at(self, token, t):
        frame = self._minute_frame(token)
        if frame is None:
            return None
        minute = pd.Timestamp(t).floor('min')
        pos = frame.index.searchsorted(minute, side='right') - 1
        if pos < 0:
            return None
        r = frame.iloc[pos]
        if frame.index[pos] != minute:
            return float(r['close'])
        o, h, l, c = (float(r[k]) for k in ('open', 'high', 'low', 'close'))
        pts = [o, l, h, c] if c >= o else [o, h, l, c]
        return pts[min(3, t.second // 15)]

    def begin_session(self, session_date, session_open=None):
        super().begin_session(session_date, session_open)
        if self.session_close is not None:
            self.session_close = self.session_close + timedelta(minutes=1)


def _prev_frame(prev: dict) -> pd.DataFrame:
    n = 130
    d = prev['date']
    ts = [datetime.combine(d, time(9, 0)) + timedelta(minutes=m) for m in range(n)]
    mid = (prev['high'] + prev['low']) / 2
    close = [mid] * n
    high, low = [mid] * n, [mid] * n
    high[10], low[20] = prev['high'], prev['low']
    close[-1] = prev['close']
    return pd.DataFrame({'time_stamp': ts, 'open': close, 'high': high, 'low': low, 'close': close, 'volume': 1.0})


def engine_trades(session: dict) -> list:
    d = session['date']
    front = ContractRef('XX', 'T1', 'XX99FUT', d + timedelta(days=90))
    nxt = ContractRef('XX', 'T2', 'XX99NXT', d + timedelta(days=120))
    bars = session['bars'].reset_index().rename(columns={'index': 'time_stamp'})
    if 'time_stamp' not in bars:
        bars = session['bars'].reset_index()
        bars = bars.rename(columns={bars.columns[0]: 'time_stamp'})
    bars = bars[['time_stamp', 'open', 'high', 'low', 'close', 'volume']]
    minutes = pd.concat([_prev_frame(session['prev']), bars], ignore_index=True)
    h = FakeHestia(datetime.combine(d, time(8, 50)), FakeConfig(), data_factory=lambda k, hol: PathData(k, hol))
    for ref in (front, nxt):
        h.add_contract(ContractSpec(ref, lot_size=10, tick_size=1.0, freeze_qty_lots=100, minutes=minutes))
    h.register('oleus', lambda: OleusEngine(CFG, 'oleus'), lots_per_unit=3)
    h.start_session(d)
    h.run_until(datetime.combine(d, time(23, 59)))
    out = [r for _, r in h.trades]
    crit = [a.text for a in h.alerts_for('critical')]
    h.close()
    return out, crit


def research_trades(session: dict) -> list:
    import janus_machine as mach
    res = mach.simulate_session(session, {'rebuy': False, 'counter': False, 'tsl': False}, 'neutral')
    grouped = {}
    for x in res['legs']:
        grouped.setdefault((x['setup'], x['trade']), []).append(x)
    return sorted(grouped.values(), key=lambda g: min(x['entry_bar'] for x in g))


def _mins(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def _minute(ts) -> str:
    return pd.Timestamp(ts).floor('min').strftime('%H:%M')


def compare(session: dict) -> dict:
    mine, crit = engine_trades(session)
    ref = research_trades(session)
    mine_sig = [(r['setup'], _minute(r['entry_ts']), tuple((r[f'lot{n}_exit_reason'], _minute(r[f'lot{n}_exit_ts'])) for n in (1, 2, 3))) for r in mine]
    ref_sig = []
    for g in ref:
        g = sorted(g, key=lambda x: x['lot'])
        ref_sig.append((g[0]['setup'], _minute(g[0]['entry_ts']), tuple((x['reason'], _minute(x['exit_ts'])) for x in g)))
    slip = [r['entry_slippage_points'] for r in mine]
    # The research hard exit is 15 minutes before the last bar IN THE DATA; the engine's is 15 before the market's last bar (close - 1 minute).
    # The Angel One tail of recent sessions carries a 23:30 bar, which moves the research exit one minute later: that single minute is not a difference.
    def _norm(sig, other):
        out = []
        for (setup, entry, lots), (_, _, olots) in zip(sig, other):
            fixed = tuple((r, ('HX' if r == 'hard_exit' and abs(_mins(m) - _mins(om)) <= 1 else m)) for (r, m), (_, om) in zip(lots, olots))
            out.append((setup, entry, fixed))
        return out
    if len(mine_sig) == len(ref_sig):
        mine_sig, ref_sig = _norm(mine_sig, ref_sig), _norm(ref_sig, mine_sig)
    return {'date': session['date'], 'engine': len(mine_sig), 'research': len(ref_sig), 'same': mine_sig == ref_sig, 'mine': mine_sig, 'ref': ref_sig,
            'critical': crit, 'pnl_engine': sum(r['total_pnl_points'] for r in mine), 'slip': slip}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--sessions', type=int, default=30)
    ap.add_argument('--end', default=None)
    a = ap.parse_args()
    from janus_data import load_sessions
    sessions = load_sessions('CRUDEOILM', level_source='previous_session', live_tail=True)
    if a.end:
        sessions = [s for s in sessions if str(s['date']) <= a.end]
    sessions = sessions[-a.sessions:]
    same = 0
    for s in sessions:
        r = compare(s)
        same += r['same']
        print(f"{r['date']}  engine {r['engine']} trades  research {r['research']} trades  {'SAME' if r['same'] else 'DIFFERENT'}"
              f"  pnl {r['pnl_engine']:+.2f} pts  avg entry shortfall {sum(r['slip']) / len(r['slip']) if r['slip'] else 0:+.2f}" +
              (f"  CRITICAL {r['critical']}" if r['critical'] else ''), flush=True)
        if not r['same']:
            print('   engine  :', r['mine'])
            print('   research:', r['ref'])
    print(f'{same} of {len(sessions)} sessions identical')


if __name__ == '__main__':
    main()
