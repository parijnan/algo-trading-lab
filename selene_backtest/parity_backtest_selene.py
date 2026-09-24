"""
Selene - production-parity backtest (plan §13).

The sweep/exit work so far ran the raw signal on ONE spliced continuous price
series and overlaid exits per trade. Production never sees a spliced series: it
trades one real contract at a time, computes ST_15 from that contract's OWN
recent history (ST_SEED_DAYS), and handles a contract roll with explicit
machinery. This simulator mirrors that machinery instead of approximating it,
for the decided config (multiplier 2.5, 3% protective stop, trend-flip exit, one
lot per unit, no targets):

  * Per-contract ST: every session's ST is computed from the trading contract's own
    trailing ST_SEED_DAYS calendar days of 1-minute bars plus the day's bars. No splice.
  * Which contract trades on a day: prometheus_functions.resolve_effective_contract,
    i.e. the front contract, rolled to the next one once <= TENDER_ROLL_TRADING_DAYS
    trading days remain to its expiry (data_loader_p3's own mirror of it).
  * Eve of a roll (tomorrow resolves to a different contract), per §18:
      - FLAT at the start of the day, or as soon as the position closes for any
        reason (stop, flip): switch to the new contract immediately, watch its own signal.
      - IN TRADE: both contracts' ST are tracked all day. If the old contract's flip
        closes the position and the new contract flips to the same direction on the
        SAME 15-minute bar, close old and open a fresh position on the new contract
        (two orders, never netted); if no coincident flip, close and switch, watching.
      - STILL IN TRADE at ROLLOVER_TIME: veto check (new contract's ST direction vs the
        position); GO -> flatten old, reopen on the new contract with the stop recalibrated
        off the historical basis (the new contract's price at the ORIGINAL entry time, so
        stop distance keeps the trade's progress); NO-GO -> flatten only, then watch.
  * Fills: at the open of the bar after the signal bar (same convention as every other
    phase); stop at its level or the bar's open on an adverse gap; a session's first
    1-minute bar is exempt from stop checks; entries need >= MIN_ENTRY_BUFFER_MIN since the
    session open.
Not modelled: missed-rollover recovery (process assumed alive), the shelved 1h entry
filter, costs/slippage, sizing. A rolled position is one *trade* made of linked legs;
P&L is the sum of the legs' real fills -- no spread gain from a splice.

Output (data_sweep/): parity_legs.csv (one row per leg), parity_trades.csv (one per trade).
"""

import os
import sys
import datetime as dt

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selene_configs as configs
import selene_data_loader as loader
from selene_data_loader import _p3, resample_ohlcv, compute_st

MIN15 = pd.Timedelta(minutes=15)


class Contract:
    """One contract's 1-minute arrays, 15-minute bars, and per-day ST windows."""

    def __init__(self, expiry, df_1m: pd.DataFrame):
        self.expiry = expiry
        df_1m = df_1m.sort_values('time_stamp').drop_duplicates('time_stamp').set_index('time_stamp')
        self.idx = df_1m.index
        self.o, self.h, self.l, self.c = (df_1m[k].to_numpy(float) for k in ('open', 'high', 'low', 'close'))
        day = self.idx.normalize()
        first = pd.Series(self.idx, index=self.idx).groupby(day).transform('min')
        self.guarded = (self.idx == first.to_numpy())
        self.first_ts = pd.Series(self.idx, index=self.idx).groupby(day).min().to_dict()
        self.last_ts = pd.Series(self.idx, index=self.idx).groupby(day).max().to_dict()
        d15 = df_1m.copy()
        d15['contract_expiry'] = str(expiry)
        self.bars = resample_ohlcv(d15, '15min')
        self._st = {}

    def has_day(self, d) -> bool:
        return pd.Timestamp(d) in self.first_ts

    def day_rows(self, d) -> dict:
        """ts -> row (open, close, trend, trend_flip) for day d, ST seeded from the trailing window."""
        key = pd.Timestamp(d)
        if key not in self._st:
            lo = key - pd.Timedelta(days=configs.ST_SEED_DAYS)
            win = self.bars[(self.bars.index >= lo) & (self.bars.index < key + pd.Timedelta(days=1))]
            rows = {}
            if len(win) > configs.ST_PERIOD + 2:
                st = compute_st(win, configs.ST_PERIOD, configs.DECIDED_MULTIPLIER)
                today = st[st.index.normalize() == key]
                for ts, r in today.iterrows():
                    rows[ts] = {'open': float(r['open']), 'close': float(r['close']),
                                'trend': None if pd.isna(r['trend']) else bool(r['trend']),
                                'flip': bool(r['trend_flip']) and not pd.isna(r['trend'])}
            self._st[key] = rows
        return self._st[key]

    def open_at(self, ts):
        i = self.idx.searchsorted(ts, side='left')
        return float(self.o[i]) if i < len(self.idx) else None

    def elapsed_since_open(self, ts) -> float:
        fb = self.first_ts.get(pd.Timestamp(ts).normalize())
        return (ts - fb).total_seconds() / 60.0 if fb is not None else None

    def scan_stop(self, direction, sl, t0, t1):
        """First 1-min bar in [t0, t1) whose range crosses the stop -> (ts, fill), else None."""
        lo, hi = self.idx.searchsorted(t0, side='left'), self.idx.searchsorted(t1, side='left')
        if hi <= lo:
            return None
        act = ~self.guarded[lo:hi]
        hit = ((self.l[lo:hi] <= sl) if direction == 'bullish' else (self.h[lo:hi] >= sl)) & act
        if not hit.any():
            return None
        k = int(hit.argmax())
        op = self.o[lo + k]
        fill = (op if op < sl else sl) if direction == 'bullish' else (op if op > sl else sl)
        return self.idx[lo + k], float(fill)


def load_contracts(symbol, seg_start, seg_end) -> dict:
    """Fyers per-contract files only: AngelOne's per-contract files mislabel pre-front-month history
    (data_downloader_mcx.py's header), so they are not used inside this segment."""
    out = {}
    for e in _p3._discover_expiries(symbol):
        df = _p3._read_contract_file(_p3.FYERS_DATA_DIR, symbol, e)
        if len(df):
            out[e] = Contract(e, df)
    return out


def simulate(seg_start: str, seg_end: str) -> pd.DataFrame:
    symbol = configs.SYMBOL
    cal = _p3._discover_expiries(symbol)
    closed = loader._load_fully_closed_dates()
    contracts = load_contracts(symbol, seg_start, seg_end)
    sgn = {'bullish': 1.0, 'bearish': -1.0}
    sl_frac = configs.DECIDED_SL_PCT / 100

    legs, stats = [], {'flat_switch': 0, 'coincident': 0, 'noncoincident_switch': 0, 'stop_switch': 0,
                       'fallback_go': 0, 'fallback_nogo': 0, 'fallback_nodata': 0}
    pos = None        # dict(trade_id, contract, direction, entry_ts, entry_px, sl, ref_px, parent_leg)
    pending = None    # dict(close, entry_dir, entry_contract)
    trade_id = 0

    def open_pos(contract, direction, ts, px, ref_px=None, tid=None, parent=None):
        nonlocal trade_id
        if tid is None:
            trade_id += 1
            tid = trade_id
        ref = px if ref_px is None else ref_px
        return {'trade_id': tid, 'contract': contract, 'direction': direction, 'entry_ts': ts, 'entry_px': px,
                'ref_px': ref, 'sl': ref * (1 - sgn[direction] * sl_frac), 'parent': parent,
                'leg_no': 1 if parent is None else parent['leg_no'] + 1}

    def close_pos(ts, px, reason):
        nonlocal pos
        p = pos
        legs.append({'trade_id': p['trade_id'], 'leg_no': p['leg_no'], 'contract': str(p['contract']),
                     'direction': p['direction'], 'entry_ts': p['entry_ts'], 'entry_px': p['entry_px'],
                     'ref_px': p['ref_px'], 'sl_px': round(p['sl'], 2), 'exit_ts': ts, 'exit_px': px,
                     'exit_reason': reason, 'pnl_pts': round(sgn[p['direction']] * (px - p['entry_px']), 2)})
        pos = None
        return p

    days = [d.date() for d in pd.bdate_range(seg_start, seg_end) if d.date() not in closed]
    for d in days:
        C = _p3._plain_resolve(d, cal, closed)
        N = _p3._effective_contract_for_date(d, cal, closed)   # = plain resolve of the next trading day
        if C not in contracts or not contracts[C].has_day(d):
            continue
        eve = N != C and N in contracts and contracts[N].has_day(d)
        if pos is not None:
            A = pos['contract']
            if A != C:
                # position sits on a contract that is no longer today's -- should not happen; close at the open
                close_pos(contracts[A].first_ts.get(pd.Timestamp(d), pd.Timestamp(d)), contracts[A].open_at(pd.Timestamp(d)) or pos['entry_px'], 'orphan')
                A = N if eve else C
            dual = N if eve and A == C else None
        else:
            A, dual = (N, None) if eve else (C, None)
            if eve:
                stats['flat_switch'] += 1

        rows = {c: contracts[c].day_rows(d) for c in {A, C, N} if c in contracts and contracts[c].has_day(d)}
        times = sorted(set().union(*[set(r) for r in rows.values()]))
        last = contracts[C].last_ts[pd.Timestamp(d)]
        rollover_ts = last - pd.Timedelta(minutes=configs.ROLLOVER_BUFFER_MIN)
        fallback_done = False

        def enter(contract, direction, ts, ref_px=None, tid=None, parent=None):
            """Entry at the open of the bar starting at ts, subject to the session-open buffer."""
            cobj = contracts[contract]
            r = rows.get(contract, {}).get(ts)
            el = cobj.elapsed_since_open(ts)
            if r is None or el is None or el < configs.MIN_ENTRY_BUFFER_MIN:
                return None
            return open_pos(contract, direction, ts, r['open'], ref_px, tid, parent)

        for ts in times:
            bar_end = ts + MIN15
            # --- 1. fill whatever the previous bar's close decided, at this bar's open ---
            if pending is not None:
                if pending['close'] and pos is not None:
                    r = rows.get(pos['contract'], {}).get(ts)
                    close_pos(ts, r['open'] if r else contracts[pos['contract']].open_at(ts), 'trend_flip')
                if pending.get('switch') and dual is not None:
                    A, dual = dual, None                 # old contract is done; new one takes over
                if pending['entry_dir'] is not None and pos is None:
                    pos = enter(pending['entry_contract'] or A, pending['entry_dir'], ts)
                pending = None
                if pos is None and dual is not None:
                    A, dual = dual, None                 # flat mid-day on a roll eve -> switch now
                    stats['stop_switch'] += 1

            # --- 2. stop check on the held contract up to the fallback moment / bar end ---
            seg_t1 = bar_end
            do_fallback = (dual is not None and pos is not None and not fallback_done and ts <= rollover_ts < bar_end)
            if do_fallback:
                seg_t1 = rollover_ts
            if pos is not None:
                hit = contracts[pos['contract']].scan_stop(pos['direction'], pos['sl'], ts, seg_t1)
                if hit:
                    close_pos(hit[0], hit[1], 'stop_loss')
                    if dual is not None:
                        A, dual = dual, None
                        stats['stop_switch'] += 1
                    do_fallback = False

            # --- 3. rollover fallback: position survived to ROLLOVER_TIME on the old contract ---
            if do_fallback and pos is not None:
                old, newc = pos['contract'], dual
                fallback_done = True
                # veto: new contract's ST direction from bars completed before this moment
                prior = [k for k in rows[newc] if k + MIN15 <= rollover_ts]
                new_tr = rows[newc][max(prior)]['trend'] if prior else None
                old_px = contracts[old].open_at(rollover_ts)
                new_px = contracts[newc].open_at(rollover_ts)
                # historical basis: new contract's close nearest the ORIGINAL entry time
                nidx = contracts[newc].idx
                ci = nidx.searchsorted(pos['entry_ts'])
                cands = [j for j in (ci - 1, ci) if 0 <= j < len(nidx)]
                basis = None
                if cands:
                    j = min(cands, key=lambda j: abs(nidx[j] - pos['entry_ts']))
                    if abs(nidx[j] - pos['entry_ts']) <= pd.Timedelta(minutes=configs.BASIS_MAX_GAP_MIN):
                        basis = float(contracts[newc].c[j])
                p_old = close_pos(rollover_ts, old_px, 'rollover')
                A, dual = newc, None
                agree = new_tr is not None and (('bullish' if new_tr else 'bearish') == p_old['direction'])
                if new_tr is None or new_px is None:
                    stats['fallback_nodata'] += 1
                elif agree and basis is not None:
                    pos = open_pos(newc, p_old['direction'], rollover_ts, new_px, ref_px=basis, tid=p_old['trade_id'], parent=p_old)
                    pos['reopen_basis'] = basis
                    stats['fallback_go'] += 1
                else:
                    stats['fallback_nogo'] += 1
                if pos is not None:   # stop monitoring on the new contract for the rest of this bar
                    hit = contracts[pos['contract']].scan_stop(pos['direction'], pos['sl'], rollover_ts, bar_end)
                    if hit:
                        close_pos(hit[0], hit[1], 'stop_loss')

            # --- 4. bar close: what does the signal say? ---
            r = rows.get(A, {}).get(ts)
            if r is None or r['trend'] is None or not r['flip']:
                continue
            dir_now = 'bullish' if r['trend'] else 'bearish'
            if pos is not None and dir_now != pos['direction']:
                if dual is not None and pos['contract'] != dual:
                    rn = rows.get(dual, {}).get(ts)
                    coinc = bool(rn and rn['flip'] and rn['trend'] is not None and (('bullish' if rn['trend'] else 'bearish') == dir_now))
                    stats['coincident' if coinc else 'noncoincident_switch'] += 1
                    pending = {'close': True, 'entry_dir': dir_now if coinc else None,
                               'entry_contract': dual if coinc else None, 'switch': True}
                else:
                    pending = {'close': True, 'entry_dir': dir_now, 'entry_contract': None}
            elif pos is None and pending is None:
                pending = {'close': False, 'entry_dir': dir_now, 'entry_contract': None}

    df = pd.DataFrame(legs)
    df.attrs['stats'] = stats
    df.attrs['open_at_end'] = pos is not None
    return df


def to_trades(legs: pd.DataFrame) -> pd.DataFrame:
    g = legs.groupby('trade_id')
    t = g.agg(direction=('direction', 'first'), entry_ts=('entry_ts', 'first'), exit_ts=('exit_ts', 'last'),
              entry_px=('entry_px', 'first'), legs=('leg_no', 'max'), pnl_pts=('pnl_pts', 'sum'),
              last_reason=('exit_reason', 'last')).reset_index()
    t['pnl_pct'] = t['pnl_pts'] / t['entry_px'] * 100
    return t.sort_values('exit_ts').reset_index(drop=True)


def metrics(t: pd.DataFrame) -> dict:
    cum = t['pnl_pts'].cumsum()
    dd = (cum - cum.cummax()).min()
    cp = t['pnl_pct'].cumsum()
    ddp = (cp - cp.cummax()).min()
    return {'trades': len(t), 'win_pct': round((t['pnl_pts'] > 0).mean() * 100, 1), 'pnl_pts': round(t['pnl_pts'].sum()),
            'max_dd_pts': round(dd), 'calmar': round(t['pnl_pts'].sum() / abs(dd), 2) if dd else float('nan'),
            'sum_pct': round(t['pnl_pct'].sum(), 1), 'calmar_pct': round(t['pnl_pct'].sum() / abs(ddp), 2) if ddp else float('nan')}


def main():
    os.makedirs(configs.DATA_SWEEP_DIR, exist_ok=True)
    legs = simulate(configs.DATA_START, configs.PARITY_END)
    legs.to_csv(os.path.join(configs.DATA_SWEEP_DIR, 'parity_legs.csv'), index=False)
    trades = to_trades(legs)
    trades.to_csv(os.path.join(configs.DATA_SWEEP_DIR, 'parity_trades.csv'), index=False)
    print('roll events:', legs.attrs['stats'], '| position open at end:', legs.attrs['open_at_end'])
    print('parity  :', metrics(trades))
    print('exit reasons (legs):', legs['exit_reason'].value_counts().to_dict(), '| multi-leg trades:', int((trades['legs'] > 1).sum()))

    # same config on the spliced continuous series (Phase 2/3 machinery), same window, 1 lot
    import exit_calib_selene as ec
    prices = ec.PriceSeries(loader.load_futures_1min())
    off = configs.DISABLED_PCT
    sp = ec.run_variant(ec.load_trades(configs.DECIDED_MULTIPLIER, prices), prices, configs.DECIDED_SL_PCT, off, off * 2)
    sp['pnl_pts'] = sp['pnl_pts'] / 2
    sp['pnl_pct'] = sp['pnl_pct'] / 2
    sp = sp[sp['entry_ts'] <= pd.Timestamp(configs.PARITY_END) + pd.Timedelta(days=1)].sort_values('trade_id')
    print('spliced :', metrics(sp.rename(columns={}).reset_index(drop=True)))
    sp.to_csv(os.path.join(configs.DATA_SWEEP_DIR, 'parity_spliced_reference.csv'), index=False)


if __name__ == '__main__':
    main()
