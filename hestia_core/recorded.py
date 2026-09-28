"""
Recorded-day tooling (plans/hestia-p5-prometheus-engine.md, slice P5.2): turn live Prometheus's dated session logs and its trades
file into an oracle and into replay inputs for the fake Hestia.

The logs are a complete decision record. Every 15-minute bar is logged with its Supertrend, close and flip verdict; every order,
fill, entry, exit, Rule 7 resolution, provisional-boundary shadow verdict, seed, reconciliation, kill and missed-flip is logged too.
This module parses them into typed events (`parse_session_log`), loads the trades file (`load_trades`), cross-checks the two records
against each other (`check_logs_against_trades`), compares the logged bars with Hestia's own bar and Supertrend path on the
pipeline's 1-minute data (`compare_bar_path`, the table in the P5 plan), and builds `LoggedReplayData`, a replay data source whose
15-minute bars and Supertrend are exactly the logged ones, so an engine can be driven by what live production actually saw
(including its partial bars) instead of by the after-the-fact pipeline data.

Read-only tooling: it reads files under a pull directory (default hestia_data/replay_pull/) and never touches Delos or a broker.
Command line: `python -m hestia_core.recorded [pull_dir]` prints the data-path table and the log-versus-trades cross-check.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from hestia_core.history import resample_1m
from hestia_core.indicators import compute_st
from hestia_core.mcx_market import closing_time_str
from hestia_core.replay import BAR_MIN, ContractSpec, ReplayData

# ---- event kinds ----------------------------------------------------------------------------------------------------------------
BAR, ORDER, FILL, ENTRY, EXIT, TRADE_CLOSED = 'bar', 'order', 'fill', 'entry', 'exit', 'trade_closed'
RULE7_RESOLVED, RULE7_STUCK, RULE7_ABANDONED = 'rule7_resolved', 'rule7_stuck', 'rule7_abandoned'
PROVISIONAL, SEED, RESUME, RECONCILED, START, SESSION_END = 'provisional', 'seed', 'resume', 'reconciled', 'start', 'session_end'
KILL, EXIT_COMMAND, MISSED_FLIP, INCOMPLETE, EFFECTIVE, REJECTED = 'kill', 'exit_command', 'missed_flip', 'incomplete', 'effective', 'rejected'
EXIT_FAILED, CIRCUIT = 'exit_failed', 'circuit'


@dataclass(frozen=True)
class LogEvent:
    ts: datetime
    kind: str
    data: dict


_TS = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+(\w+)\s+(\S+)\s+(.*)$')
_PATTERNS: List[Tuple[str, re.Pattern]] = [
    (BAR, re.compile(r'^15m bar (\d\d:\d\d) — ST=([\d.]+) close=([\d.]+)\s+(no flip|FLIP -> (\w+))')),
    (ORDER, re.compile(r'^Order placed: (BUY|SELL) (\d+) x (\S+) -> orderid=(\S+)')),
    (FILL, re.compile(r'^Fill \((WS|REST)\): (\S+) avg=([\d.]+) qty=(\d+) \((\d+) lot\(s\)\) across (\d+) order')),
    (EXIT, re.compile(r'Lot(\d) exit — (\S+)(?:\s+\(Units: (\d+)\))?\s+Entry ([\d.]+) -> Exit ([\d.]+) \| P&L: ([+-][\d.]+) pts')),
    (ENTRY, re.compile(r'Entered (BULLISH|BEARISH)( \(rollover\))?\s+(\S+) \| Units: (\d+) \((\d+) lots?\)\s+Entry: ([\d.]+)'
                       r'(?: \(recalibration basis ([\d.]+)\))? \| SL: ([\d.]+|n/a)\s+Lot1 target: ([\d.]+|n/a[^|]*) \| '
                       r'Lot2 target: ([\d.]+) \((\w+)\)')),
    (TRADE_CLOSED, re.compile(r'Trade #(\d+) closed\.')),
    (RULE7_RESOLVED, re.compile(r'^Rule 7 pending flip fully resolved')),
    (RULE7_STUCK, re.compile(r'^Rule 7 pending flip stuck: (.*)')),
    (RULE7_ABANDONED, re.compile(r'^Rule 7 pending flip abandoned')),
    (PROVISIONAL, re.compile(r'^Provisional boundary (\d\d:\d\d): close=([\d.]+) ST=([\d.]+)(?: prev_ST=([\d.]+))? direction=(\w+) '
                             r'flip=(\w+) (?:band_dist_pct|clear_prev_st_pct)=([\d.]+) \(margin=([\d.]+)\) clears_margin=(\w+) '
                             r'gating=(\w+)')),
    (SEED, re.compile(r'^Seeded: (\d+) 15-min bars from (\S+) \((\d+) trading day\(s\) of 1-min history\) \| trend=(\w+) ST=([\d.]+)')),
    (RESUME, re.compile(r'^Resuming in-trade state')),
    (RECONCILED, re.compile(r'^Position reconciliation (OK|FAILED)(?:.*?broker netqty ([+-]?\d+))?')),
    (START, re.compile(r'^Prometheus starting \[DRY_RUN=(\w+)\] SYMBOL=(\w+)')),
    (SESSION_END, re.compile(r'^Session end \((\d\d:\d\d)\) reached')),
    (KILL, re.compile(r'Slack `Kill Switch` detected')),
    (EXIT_COMMAND, re.compile(r'Slack `Exit Trade` detected')),
    (MISSED_FLIP, re.compile(r'^Missed flip detected: ST_15 flipped -> (\w+) at (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)')),
    (INCOMPLETE, re.compile(r'^15m bar (\d\d:\d\d)-(\d\d:\d\d) still incomplete \((\d+)/15\)|^Only (\d+)/15 1-min bars for (\d\d:\d\d)-(\d\d:\d\d)')),
    (EFFECTIVE, re.compile(r'^Effective contract: (\S+) \(token (\d+), expiry (\S+)\)( \(rolled early)?')),
    (REJECTED, re.compile(r'^Order rejected \((\d+)/(\d+)\): (\S+) — (.*)')),
    (EXIT_FAILED, re.compile(r'^Lot(\d) exit order FAILED to place')),
    (CIRCUIT, re.compile(r'^DPL (upper|lower) circuit limit reached: LTP=([\d.]+)')),
]


def _convert(kind: str, m: re.Match, ts: datetime) -> dict:
    g = m.groups()
    day = ts.date()
    if kind == BAR:
        hm = g[0]
        start = datetime.combine(day, datetime.strptime(hm, '%H:%M').time())
        return {'bar_start': start, 'st': float(g[1]), 'close': float(g[2]), 'flip': g[3] != 'no flip',
                'direction': g[4]}
    if kind == ORDER:
        return {'side': g[0], 'qty': int(g[1]), 'symbol': g[2], 'orderid': g[3]}
    if kind == FILL:
        return {'source': g[0], 'symbol': g[1], 'avg': float(g[2]), 'qty': int(g[3]), 'lots': int(g[4]), 'orders': int(g[5])}
    if kind == EXIT:
        return {'lot': int(g[0]), 'reason': g[1], 'units': int(g[2]) if g[2] else None, 'entry': float(g[3]), 'exit': float(g[4]),
                'pnl_pts': float(g[5])}
    if kind == ENTRY:
        return {'direction': g[0].lower(), 'rollover': bool(g[1]), 'symbol': g[2], 'units': int(g[3]), 'lots': int(g[4]),
                'entry': float(g[5]), 'basis': float(g[6]) if g[6] else None,
                'sl': None if g[7] == 'n/a' else float(g[7]), 'lot1_target': None if g[8].startswith('n/a') else float(g[8]),
                'lot2_target': float(g[9]), 'lot2_source': g[10]}
    if kind == TRADE_CLOSED:
        return {'trade_id': int(g[0])}
    if kind == RULE7_STUCK:
        return {'detail': g[0]}
    if kind == PROVISIONAL:
        start = datetime.combine(day, datetime.strptime(g[0], '%H:%M').time())
        return {'bar_start': start, 'close': float(g[1]), 'st': float(g[2]), 'prev_st': float(g[3]) if g[3] else None,
                'direction': g[4], 'flip': g[5] == 'True', 'margin_pct': float(g[6]), 'margin': float(g[7]),
                'clears_margin': g[8] == 'True', 'gating': g[9] == 'ON'}
    if kind == SEED:
        return {'bars': int(g[0]), 'symbol': g[1], 'days': int(g[2]), 'trend': g[3], 'st': float(g[4])}
    if kind == RECONCILED:
        return {'ok': g[0] == 'OK', 'netqty': int(g[1]) if g[1] else None}
    if kind == START:
        return {'dry_run': g[0] == 'True', 'symbol_root': g[1]}
    if kind == SESSION_END:
        return {'at': g[0]}
    if kind == MISSED_FLIP:
        return {'direction': g[0], 'bar_start': datetime.strptime(g[1], '%Y-%m-%d %H:%M:%S')}
    if kind == INCOMPLETE:
        if g[0]:
            return {'window_start': datetime.combine(day, datetime.strptime(g[0], '%H:%M').time()), 'rows': int(g[2])}
        return {'window_start': datetime.combine(day, datetime.strptime(g[4], '%H:%M').time()), 'rows': int(g[3])}
    if kind == EFFECTIVE:
        return {'symbol': g[0], 'token': g[1], 'expiry': g[2], 'rolled_early': bool(g[3])}
    if kind == REJECTED:
        return {'attempt': int(g[0]), 'of': int(g[1]), 'symbol': g[2], 'reason': g[3]}
    if kind == EXIT_FAILED:
        return {'lot': int(g[0])}
    if kind == CIRCUIT:
        return {'side': g[0], 'ltp': float(g[1])}
    return {}


def parse_lines(lines: Iterable[str]) -> List[LogEvent]:
    """Typed events from log lines, in file order. Lines that are not decision-relevant (fetch chatter, debug) are skipped."""
    events: List[LogEvent] = []
    for line in lines:
        m = _TS.match(line.rstrip('\n'))
        if not m:
            continue
        ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
        text = m.group(4)
        for kind, pat in _PATTERNS:
            hit = pat.search(text) if kind in (EXIT, ENTRY, TRADE_CLOSED, KILL, EXIT_COMMAND, MISSED_FLIP) else pat.match(text)
            if hit:
                events.append(LogEvent(ts, kind, _convert(kind, hit, ts)))
                break
    return events


@dataclass
class SessionRecord:
    day: date
    path: Path
    events: List[LogEvent]

    def of(self, *kinds: str) -> List[LogEvent]:
        return [e for e in self.events if e.kind in kinds]

    @property
    def bars(self) -> List[LogEvent]:
        """One event per distinct bar (a bar can be logged again after a restart in the same session)."""
        seen, out = set(), []
        for e in self.of(BAR):
            if e.data['bar_start'] not in seen:
                seen.add(e.data['bar_start'])
                out.append(e)
        return out


def parse_session_log(path: Path) -> SessionRecord:
    day = datetime.strptime(Path(path).stem.split('_')[-1], '%Y%m%d').date()
    with open(path, errors='replace') as f:
        return SessionRecord(day, Path(path), parse_lines(f))


def load_sessions(pull_dir: Path) -> List[SessionRecord]:
    return [parse_session_log(p) for p in sorted(Path(pull_dir, 'logs').glob('prometheus_2026*.log'))]


def load_trades(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    for c in ('entry_ts', 'signal_ts', 'lot1_exit_ts', 'lot2_exit_ts'):
        df[c] = pd.to_datetime(df[c], errors='coerce')
    return df


# ---------------------------------------------------------------------------------------------------------------------------------
# Cross-check: the logs against the trades file
# ---------------------------------------------------------------------------------------------------------------------------------

@dataclass
class CrossCheck:
    entries_logged: int = 0
    entries_matched: int = 0
    exits_logged: int = 0
    exits_matched: int = 0
    trades_in_window: int = 0
    trades_with_entry_logged: int = 0
    problems: List[str] = field(default_factory=list)


def check_logs_against_trades(sessions: Sequence[SessionRecord], trades: pd.DataFrame, tol_s: float = 3.0) -> CrossCheck:
    """Every logged entry must be a trade (same price, entry time within `tol_s`), every logged exit a trade's lot exit (same
    price and reason), and every trade entered inside the logged window must have its entry logged."""
    out = CrossCheck()
    first_ts = min(e.ts for s in sessions for e in s.events) if sessions else None
    last_ts = max(e.ts for s in sessions for e in s.events) if sessions else None
    matched_trades = set()
    for s in sessions:
        for e in s.of(ENTRY):
            out.entries_logged += 1
            cand = trades[(abs((trades['entry_ts'] - e.ts).dt.total_seconds()) <= tol_s)
                          & (abs(trades['entry_price'] - e.data['entry']) < 0.006)]
            if len(cand) == 1:
                out.entries_matched += 1
                matched_trades.add(int(cand.iloc[0]['trade_id']))
                t = cand.iloc[0]
                base = t['direction'].split('-')[0]
                if base != e.data['direction'] or int(t['units']) != e.data['units']:
                    out.problems.append(f"{e.ts}: entry direction/units differ from trade {int(t['trade_id'])}")
            else:
                out.problems.append(f'{e.ts}: logged entry at {e.data["entry"]} matches {len(cand)} trades')
        for e in s.of(EXIT):
            out.exits_logged += 1
            lot = e.data['lot']
            col_ts, col_px, col_reason = f'lot{lot}_exit_ts', f'lot{lot}_exit_price', f'lot{lot}_exit_reason'
            cand = trades[(abs((trades[col_ts] - e.ts).dt.total_seconds()) <= tol_s) & (abs(trades[col_px] - e.data['exit']) < 0.006)]
            if len(cand) >= 1 and (cand[col_reason] == e.data['reason']).any():
                out.exits_matched += 1
            else:
                out.problems.append(f"{e.ts}: logged lot{lot} exit {e.data['reason']} at {e.data['exit']} matches "
                                    f'{len(cand)} trade row(s)')
    if first_ts is not None:
        window = trades[(trades['entry_ts'] >= first_ts) & (trades['entry_ts'] <= last_ts)]
        out.trades_in_window = len(window)
        out.trades_with_entry_logged = int(window['trade_id'].isin(matched_trades).sum())
        for _, t in window[~window['trade_id'].isin(matched_trades)].iterrows():
            out.problems.append(f"trade {int(t['trade_id'])} entered at {t['entry_ts']} has no logged entry")
    return out


# ---------------------------------------------------------------------------------------------------------------------------------
# Data path: the logged bars against Hestia's own bars and Supertrend on the pipeline's 1-minute data
# ---------------------------------------------------------------------------------------------------------------------------------

@dataclass
class DayComparison:
    day: date
    contract: str
    bars: int
    matched: int
    close_differs: int
    flip_differs: int
    st_differs: int
    worst_st_diff: float


def _contract_expiry(symbol: str) -> str:
    return datetime.strptime(re.search(r'(\d\d[A-Z]{3}\d\d)FUT', symbol).group(1), '%d%b%y').strftime('%Y-%m-%d')


def load_minute_files(pipeline_dir: Path, instrument: str) -> Dict[str, pd.DataFrame]:
    frames = {}
    for f in Path(pipeline_dir, instrument).glob('*_futures.csv'):
        d = pd.read_csv(f, float_precision='round_trip')
        d['time_stamp'] = pd.to_datetime(d['time_stamp'], format='ISO8601').dt.tz_localize(None)
        frames[f.name[:10]] = d.sort_values('time_stamp').reset_index(drop=True)
    return frames


def computed_series(minute_frame: pd.DataFrame, day: date, st_period: int = 10, st_multiplier: float = 2.0) -> pd.DataFrame:
    """Hestia's bars and Supertrend for `day` from the pipeline's 1-minute data: 15-minute bars anchored at 09:00, Supertrend over
    the whole series up to the end of that day."""
    upto = datetime.combine(day + timedelta(days=1), datetime.min.time())
    bars = resample_1m(minute_frame[minute_frame['time_stamp'] < upto], BAR_MIN, upto, '09:00', closing_time_str)
    st = compute_st(bars, st_period, st_multiplier)
    return st[st['time_stamp'].dt.date == day].set_index('time_stamp')


def compare_bar_path(session: SessionRecord, minute_frames: Dict[str, pd.DataFrame], tol: float = 0.011) -> Optional[DayComparison]:
    seeds = session.of(SEED)
    if not seeds or not session.bars:
        return None
    symbol = seeds[-1].data['symbol']
    expiry = _contract_expiry(symbol)
    if expiry not in minute_frames:
        return None
    computed = computed_series(minute_frames[expiry], session.day)
    n = ok = cb = fb = sb = 0
    worst = 0.0
    for e in session.bars:
        start = e.data['bar_start']
        if start not in computed.index:
            continue
        r = computed.loc[start]
        n += 1
        good = True
        if abs(r['close'] - e.data['close']) > tol:
            cb += 1
            good = False
        if bool(r['trend_flip']) != e.data['flip']:
            fb += 1
            good = False
        d = abs(float(r['supertrend']) - e.data['st'])
        worst = max(worst, d)
        if d > tol:
            sb += 1
            good = False
        ok += good
    return DayComparison(session.day, symbol, n, ok, cb, fb, sb, worst)


# ---------------------------------------------------------------------------------------------------------------------------------
# Replay data whose bars ARE the logged bars
# ---------------------------------------------------------------------------------------------------------------------------------

class LoggedReplayData(ReplayData):
    """A ReplayData whose 15-minute bars and Supertrend are the ones live production logged, not recomputed. The 1-minute frames still
    give prices (for LTP, stops and fills) and each bar's open, high, low and volume where a pipeline bar exists for that window.

    Only valid for the Supertrend live production ran (period 10, multiplier 2.0): the logged values are that, whatever spec an engine
    registers. A bar's `minutes` count comes from the logged 'still incomplete' or 'Only n/15' warnings (a live partial bar is
    reported PARTIAL); a boundary that had a logged provisional verdict is delivered as a provisional bar first and the real bar
    after the delay the log shows (`bar_delay`)."""

    def __init__(self, kernel, sessions: Sequence[SessionRecord], minute_frames: Dict[str, pd.DataFrame], holidays=None):
        super().__init__(kernel, holidays)
        self._sessions = list(sessions)
        self._minute_frames = minute_frames

    def add_logged_contract(self, spec: ContractSpec, expiry_key: str) -> None:
        """Register `spec` (its `minutes` are the pipeline frame) and build its logged bars from every session that traded it."""
        self.add_contract(spec)
        rows, partial = [], {}
        prov_delay: Dict[datetime, float] = {}
        provisional_close: Dict[datetime, float] = {}
        for s in self._sessions:
            seeds = s.of(SEED)
            if not seeds or _contract_expiry(seeds[-1].data['symbol']) != expiry_key:
                continue
            incompletes = {e.data['window_start']: e.data['rows'] for e in s.of(INCOMPLETE)}
            provs = {e.data['bar_start']: e for e in s.of(PROVISIONAL)}
            for e in s.bars:
                d = e.data
                start = d['bar_start']
                boundary = start + timedelta(minutes=BAR_MIN)
                rows.append({'time_stamp': pd.Timestamp(start), 'close': d['close'], 'st': d['st'], 'flip': d['flip'],
                             'minutes': incompletes.get(start, BAR_MIN)})
                if start in incompletes:
                    partial[boundary] = incompletes[start]
                if start in provs:
                    delay = (e.ts - boundary).total_seconds()
                    prov_delay[boundary] = max(delay, 1.0)
                    provisional_close[boundary] = provs[start].data['close']
        df = pd.DataFrame(rows).drop_duplicates('time_stamp', keep='last').sort_values('time_stamp').reset_index(drop=True)
        pipe = computed_pipeline_bars(self._minute_frames[expiry_key])
        merged = df.merge(pipe, on='time_stamp', how='left', suffixes=('', '_pipe'))
        bars = pd.DataFrame({'time_stamp': merged['time_stamp'], 'open': merged['open'].fillna(merged['close']),
                             'high': merged[['high', 'close']].max(axis=1), 'low': merged[['low', 'close']].min(axis=1),
                             'close': merged['close'], 'volume': merged['volume'].fillna(0.0), 'minutes': merged['minutes']})
        token = spec.ref.token
        self._bars[token] = bars
        self._logged_st = getattr(self, '_logged_st', {})
        self._logged_st[token] = pd.DataFrame({'time_stamp': merged['time_stamp'], 'open': bars['open'], 'high': bars['high'],
                                               'low': bars['low'], 'close': bars['close'], 'volume': bars['volume'],
                                               'supertrend': merged['st'], 'trend': bars['close'] > merged['st'],
                                               'trend_flip': merged['flip']})
        for boundary, rows_n in partial.items():
            self.bar_partial[(token, boundary)] = rows_n
        for boundary, delay in prov_delay.items():
            self.bar_delay[(token, boundary)] = delay
            self.provisional_override[(token, boundary)] = {'close': provisional_close[boundary]}

    def _st_for(self, token: str, spec):
        return self._logged_st[token]


def computed_pipeline_bars(minute_frame: pd.DataFrame) -> pd.DataFrame:
    upto = minute_frame['time_stamp'].max() + timedelta(days=1)
    bars = resample_1m(minute_frame, BAR_MIN, upto, '09:00', closing_time_str)
    counts = minute_frame.assign(_b=minute_frame['time_stamp'].dt.floor('15min')).groupby('_b').size()
    bars['minutes'] = bars['time_stamp'].map(counts).fillna(BAR_MIN)
    return bars


# ---------------------------------------------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------------------------------------------

def main(argv: Sequence[str]) -> int:
    root = Path(__file__).resolve().parents[1]
    pull = Path(argv[1]) if len(argv) > 1 else root / 'hestia_data' / 'replay_pull'
    sessions = load_sessions(pull)
    if not sessions:
        print(f'no session logs under {pull}/logs')
        return 1
    frames = load_minute_files(root / 'data_pipeline' / 'data' / 'mcx', 'CRUDEOILM')
    print('data path: logged bars against Hestia on the pipeline 1-minute data')
    tot = [0, 0, 0, 0, 0]
    for s in sessions:
        c = compare_bar_path(s, frames)
        if c is None:
            print(f'  {s.day}: not comparable (no seed line, no bars, or no pipeline file)')
            continue
        print(f'  {c.day} {c.contract}: {c.bars:3d} bars, all match {c.matched:3d}, close off {c.close_differs}, flip off {c.flip_differs}, '
              f'ST off {c.st_differs}, worst ST diff {c.worst_st_diff:.4f}')
        for i, v in enumerate((c.bars, c.matched, c.close_differs, c.flip_differs, c.st_differs)):
            tot[i] += v
    print(f'  total: {tot[0]} bars, {tot[1]} all match, {tot[2]} close off, {tot[3]} flip off, {tot[4]} ST off')
    trades_path = Path(pull, 'data', 'prometheus_trades.csv')
    if trades_path.exists():
        cc = check_logs_against_trades(sessions, load_trades(trades_path))
        print(f'logs against trades: entries {cc.entries_matched}/{cc.entries_logged}, exits {cc.exits_matched}/{cc.exits_logged}, '
              f'trades in window with a logged entry {cc.trades_with_entry_logged}/{cc.trades_in_window}')
        for p in cc.problems[:20]:
            print('  problem:', p)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
