"""
Prometheus - production-parity backtest (2026-10-16). Built after live trade #76 (bearish, 2026-10-14 20:15) had no twin in the Fyers-history track: that track
chose the NEAREST-expiry contract and ran one Supertrend across a spliced series, while production trades the early-rolled contract (5 trading days before expiry)
and computes the Supertrend from that contract's own history. Same design as the Selene/Helios/Typhon parity backtests (this file is Typhon's simulator with the
two-lot exits added), mirroring the live engine (prometheus_engine/engine.py, hestia_core/roll_policy.py):

  * One real contract at a time, no splice. Which contract trades on a day: the plain roll rule evaluated on the NEXT trading day (data_loader_p3), i.e. the
    nearest unexpired contract, rolled to the next one once <= 5 trading days remain; a flat engine switches on the eve of the roll.
  * Per-contract ST: every session's Supertrend (period 10, multiplier 2.5) is computed from the trading contract's own trailing ST_SEED_DAYS calendar days.
  * Entry on a flip at the OPEN of the next 15-minute bar, not before MIN_ENTRY_BUFFER_MIN after the session open. One unit = two lots: lot 1 books at T1, lot 2 at
    T2, both stops are one shared stop that does not move after T1. Stop wins a same-bar tie against a target; fills at the level, or at the bar's open on a gap
    through it; the session's first 1-minute bar is exempt from stop and target checks. A flip exits whatever lots are still open at the next bar's open and, in the
    same step, opens the new direction with both lots.
  * Eve of a roll with a position: the old and the new contract are tracked all day. An old-contract flip exits; if the new contract flips to the same direction
    on the SAME bar a fresh two-lot entry follows on it. A position still open at the rollover time (session close - 15 min) is flattened and reopened on the new
    contract only if the new contract's Supertrend agrees (the veto) and a basis price exists; the reopen is sized to the lots that survived (a lot-2-only reopen
    keeps no T1) and its stop and targets are cut from the basis (the new contract's price at the OLD entry time), not the fill.
Not modelled: missed-rollover recovery, the provisional-bar entries (live since 2026-10-08, they act before the bar closes), costs/slippage, sizing.

Output (data_sweep/parity/): parity_legs.csv (one row per leg), parity_trades.csv (one per trade, in the bespoke_trade_summary column layout the hestia_backtest scripts read:
points are summed over the two lots, Rs = points x LOT_SIZE).

    python parity_backtest_prometheus.py [END_DATE]
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_p3 as configs  # noqa: E402
import data_loader_fyers as fyers_loader  # noqa: E402  (puts prometheus_backtest on sys.path)
import data_loader as _angelone_loader  # noqa: E402
import data_loader_p3 as _p3  # noqa: E402

resample_ohlcv, compute_st = fyers_loader.resample_ohlcv, fyers_loader.compute_st
MIN15 = pd.Timedelta(minutes=15)
SGN = {'bullish': 1.0, 'bearish': -1.0}

assert _p3.TENDER_ROLL_TRADING_DAYS == 5, 'data_loader_p3.TENDER_ROLL_TRADING_DAYS is the production roll window (5 trading days)'


def fully_closed_dates() -> set:
    """Dates on which BOTH MCX sessions were closed: the 2022-2026 calendar and production's own mcx_holidays.csv (same union as the other engines' loaders)."""
    h = pd.read_csv(configs.HOLIDAYS_FILE)
    h['d'] = pd.to_datetime(h['Date'], format='%d %b %Y').dt.date
    return set(h[(h['Morning Session'] == 'Closed') & (h['Evening Session'] == 'Closed')]['d']) | _p3._load_fully_closed_dates()


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
        """ts -> row (open, close, trend, flip) for day d, ST seeded from the trailing window."""
        key = pd.Timestamp(d)
        if key not in self._st:
            lo = key - pd.Timedelta(days=configs.ST_SEED_DAYS)
            win = self.bars[(self.bars.index >= lo) & (self.bars.index < key + pd.Timedelta(days=1))]
            rows = {}
            if len(win) > configs.ST_PERIOD + 2:
                st = compute_st(win, configs.ST_PERIOD, configs.PARITY_ST_MULTIPLIER)
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

    def scan_exit(self, direction, sl, t1, t2, t0, t_end):
        """First event in [t0, t_end): (ts, fill, kind) with kind 'stop_loss', 'target1' or 'target2', else None. t1/t2 None disables that target. The stop wins a
        same-bar tie, then T1 before T2. Fill: the level, or the bar's open on a gap through it (adverse for the stop, favourable for a target). The session's
        first 1-minute bar never triggers anything."""
        lo, hi = self.idx.searchsorted(t0, side='left'), self.idx.searchsorted(t_end, side='left')
        if hi <= lo:
            return None
        act = ~self.guarded[lo:hi]
        h, l, o = self.h[lo:hi], self.l[lo:hi], self.o[lo:hi]
        bull = direction == 'bullish'
        n = len(o)

        def first(mask):
            return int(mask.argmax()) if mask.any() else n

        i_sl = first(((l <= sl) if bull else (h >= sl)) & act)
        i_1 = first(((h >= t1) if bull else (l <= t1)) & act) if t1 is not None else n
        i_2 = first(((h >= t2) if bull else (l <= t2)) & act) if t2 is not None else n
        k = min(i_sl, i_1, i_2)
        if k >= n:
            return None
        if i_sl == k:
            fill = (o[k] if o[k] < sl else sl) if bull else (o[k] if o[k] > sl else sl)
            return self.idx[lo + k], float(fill), 'stop_loss'
        level, kind = (t1, 'target1') if i_1 == k else (t2, 'target2')
        fill = (o[k] if o[k] > level else level) if bull else (o[k] if o[k] < level else level)
        return self.idx[lo + k], float(fill), kind


def load_contracts() -> dict:
    """Per-contract 1-minute frames: Fyers where it has the contract's day, Angel One only where it doesn't (the live-tree files hold a contract's own prices from
    ANGELONE_OWN_FROM; earlier Angel One rows are a relabelled front month and are used only to fill days the front contract's Fyers file lacks, the 2026-03/06
    Fyers void). The known Angel One thin-liquidity opening bars are corrected on Angel One rows only (data_loader._CRUDEOILM_OPENING_BAR_CORRECTIONS)."""
    symbol = configs.SYMBOL
    cal = _p3._discover_expiries(symbol)
    fy = {e: _p3._read_contract_file(_p3.FYERS_DATA_DIR, symbol, e) for e in cal}
    fy_days = {e: set(d['time_stamp'].dt.date) for e, d in fy.items() if len(d)}
    parts = {e: [fy[e]] if len(fy[e]) else [] for e in cal}
    own_from = pd.Timestamp(configs.ANGELONE_OWN_FROM)

    def corrected(df):
        df = df.copy()
        for ts, ohlc in _angelone_loader._CRUDEOILM_OPENING_BAR_CORRECTIONS.items():
            m = df['time_stamp'] == ts
            if m.any():
                df.loc[m, ['open', 'high', 'low', 'close']] = [ohlc['open'], ohlc['high'], ohlc['low'], ohlc['close']]
        return df

    for e in cal:
        ao = _p3._read_contract_file(_p3.ANGELONE_DATA_DIR, symbol, e)
        if ao.empty:
            continue
        own = ao[ao['time_stamp'] >= own_from]
        if len(own):
            parts[e].append(corrected(own))
        pre = ao[ao['time_stamp'] < own_from]
        for day, chunk in pre.groupby(pre['time_stamp'].dt.date):
            f = _p3._naive_front_month_for_date(day, cal)
            if f is not None and day not in fy_days.get(f, set()):
                parts[f].append(corrected(chunk))
    out = {}
    for e, ps in parts.items():
        ps = [x for x in ps if len(x)]
        if ps:
            df = pd.concat(ps, ignore_index=True).drop_duplicates('time_stamp', keep='first')
            out[e] = Contract(e, df)
    return out


def simulate(seg_start: str, seg_end: str, contracts: dict = None) -> pd.DataFrame:
    symbol = configs.SYMBOL
    cal = _p3._discover_expiries(symbol)
    closed = fully_closed_dates()
    if contracts is None:
        contracts = load_contracts()
    sl_frac, t1_frac, t2_frac = configs.PARITY_SL_PCT / 100, configs.PARITY_T1_PCT / 100, configs.PARITY_T2_PCT / 100

    legs, stats = [], {'flat_switch': 0, 'coincident': 0, 'noncoincident_switch': 0, 'stop_switch': 0,
                       'fallback_go': 0, 'fallback_nogo': 0, 'fallback_nodata': 0, 'forced_roll': 0, 'naive_day': 0}
    pos = None        # dict(trade_id, leg_no, contract, direction, entry_ts, entry_px, ref_px, sl, t1, t2, lot1/lot2 = None (never opened) | 'open' | (ts, px, reason))
    pending = None    # dict(close, entry_dir, entry_contract, switch)
    trade_id = 0

    def open_pos(contract, direction, ts, px, ref_px=None, tid=None, parent=None, lots=('lot1', 'lot2')):
        nonlocal trade_id
        if tid is None:
            trade_id += 1
            tid = trade_id
        ref = px if ref_px is None else ref_px
        s = SGN[direction]
        return {'trade_id': tid, 'contract': contract, 'direction': direction, 'entry_ts': ts, 'entry_px': px, 'ref_px': ref,
                'sl': ref * (1 - s * sl_frac), 't1': ref * (1 + s * t1_frac) if 'lot1' in lots else None, 't2': ref * (1 + s * t2_frac),
                'lot1': 'open' if 'lot1' in lots else None, 'lot2': 'open' if 'lot2' in lots else None,
                'leg_no': 1 if parent is None else parent['leg_no'] + 1}

    def finish_leg():
        nonlocal pos
        p = pos
        s = SGN[p['direction']]

        def cell(k):
            v = p[k]
            return v if isinstance(v, tuple) else (None, None, None)

        l1, l2 = cell('lot1'), cell('lot2')
        pts = sum(round(s * (v[1] - p['entry_px']), 2) for v in (l1, l2) if v[0] is not None)
        legs.append({'trade_id': p['trade_id'], 'leg_no': p['leg_no'], 'contract': str(p['contract']), 'direction': p['direction'], 'entry_ts': p['entry_ts'],
                     'entry_px': p['entry_px'], 'ref_px': p['ref_px'], 'sl_px': round(p['sl'], 2),
                     'lot1_target': None if p['t1'] is None else round(p['t1'], 2), 'lot2_target': round(p['t2'], 2),
                     'lot1_exit_ts': l1[0], 'lot1_exit_px': l1[1], 'lot1_exit_reason': l1[2],
                     'lot2_exit_ts': l2[0], 'lot2_exit_px': l2[1], 'lot2_exit_reason': l2[2], 'pnl_pts': round(pts, 2)})
        pos = None
        return p

    def book(lot, ts, px, reason):
        """Close one lot of the open position; the leg ends when no lot is left open."""
        pos[lot] = (ts, px, reason)
        if pos['lot1'] != 'open' and pos['lot2'] != 'open':
            return finish_leg()
        return None

    def close_all(ts, px, reason):
        last = None
        for lot in ('lot1', 'lot2'):
            if pos is not None and pos[lot] == 'open':
                last = book(lot, ts, px, reason) or last
        return last

    def run_exits(t0, t_end):
        """Walk the open position forward through [t0, t_end): book every stop/target event in order. Returns True when the position closed."""
        t = t0
        while pos is not None:
            hit = contracts[pos['contract']].scan_exit(pos['direction'], pos['sl'], pos['t1'] if pos['lot1'] == 'open' else None,
                                                       pos['t2'] if pos['lot2'] == 'open' else None, t, t_end)
            if not hit:
                return False
            ts, px, kind = hit
            if kind == 'stop_loss':
                close_all(ts, px, 'stop_loss')
            elif kind == 'target1':
                book('lot1', ts, px, 'target1')
            else:
                book('lot2', ts, px, 'target2')
            t = ts
        return True

    days = [d.date() for d in pd.bdate_range(seg_start, seg_end) if d.date() not in closed]
    for d in days:
        dts = pd.Timestamp(d)

        def has(c):
            return c in contracts and contracts[c].has_day(d)

        C = _p3._plain_resolve(d, cal, closed)
        N = _p3._effective_contract_for_date(d, cal, closed)   # = plain resolve of the next trading day
        if not has(C):
            nf = _p3._naive_front_month_for_date(d, cal)
            if nf is None or not has(nf):
                continue
            C = nf
            stats['naive_day'] += 1
        eve = N != C and has(N)

        if pos is not None and pos['contract'] != C:
            old = pos['contract']
            first_new = contracts[C].first_ts[dts]
            if has(old):
                ts_roll, px_old = first_new, contracts[old].open_at(first_new)
            else:
                j = contracts[old].idx.searchsorted(dts) - 1
                ts_roll, px_old = contracts[old].idx[j], float(contracts[old].c[j])
            lots = tuple(k for k in ('lot1', 'lot2') if pos[k] == 'open')
            p_old = close_all(ts_roll, px_old, 'forced_roll')
            new_open = contracts[C].open_at(first_new)
            pos = open_pos(C, p_old['direction'], first_new, new_open, ref_px=new_open, tid=p_old['trade_id'], parent=p_old, lots=lots)
            stats['forced_roll'] += 1

        if pos is not None:
            A = pos['contract']
            dual = N if eve and A == C else None
        else:
            A, dual = (N, None) if eve else (C, None)
            if eve:
                stats['flat_switch'] += 1

        rows = {c: contracts[c].day_rows(d) for c in {A, C, N} if c in contracts and contracts[c].has_day(d)}
        times = sorted(set().union(*[set(r) for r in rows.values()]))
        last = contracts[C].last_ts[dts]
        rollover_ts = last - pd.Timedelta(minutes=configs.ROLLOVER_BUFFER_MIN)
        fallback_done = False

        def enter(contract, direction, ts, ref_px=None, tid=None, parent=None):
            cobj = contracts[contract]
            r = rows.get(contract, {}).get(ts)
            el = cobj.elapsed_since_open(ts)
            if r is None or el is None or el < configs.MIN_ENTRY_BUFFER_MIN:
                return None
            return open_pos(contract, direction, ts, r['open'], ref_px, tid, parent)

        for ts in times:
            bar_end = ts + MIN15
            if pending is not None:
                if pending['close'] and pos is not None:
                    r = rows.get(pos['contract'], {}).get(ts)
                    close_all(ts, r['open'] if r else contracts[pos['contract']].open_at(ts), 'trend_flip')
                if pending.get('switch') and dual is not None:
                    A, dual = dual, None
                if pending['entry_dir'] is not None and pos is None:
                    pos = enter(pending['entry_contract'] or A, pending['entry_dir'], ts)
                pending = None
                if pos is None and dual is not None:
                    A, dual = dual, None
                    stats['stop_switch'] += 1

            seg_t1 = bar_end
            do_fallback = (dual is not None and pos is not None and not fallback_done and ts <= rollover_ts < bar_end)
            if do_fallback:
                seg_t1 = rollover_ts
            if pos is not None:
                if run_exits(ts, seg_t1):
                    if dual is not None:
                        A, dual = dual, None
                        stats['stop_switch'] += 1
                    do_fallback = False

            if do_fallback and pos is not None:
                old, newc = pos['contract'], dual
                fallback_done = True
                prior = [k for k in rows[newc] if k + MIN15 <= rollover_ts]
                new_tr = rows[newc][max(prior)]['trend'] if prior else None
                old_px = contracts[old].open_at(rollover_ts)
                new_px = contracts[newc].open_at(rollover_ts)
                nidx = contracts[newc].idx
                ci = nidx.searchsorted(pos['entry_ts'])
                cands = [j for j in (ci - 1, ci) if 0 <= j < len(nidx)]
                basis = None
                if cands:
                    j = min(cands, key=lambda j: abs(nidx[j] - pos['entry_ts']))
                    if abs(nidx[j] - pos['entry_ts']) <= pd.Timedelta(minutes=configs.BASIS_MAX_GAP_MIN):
                        basis = float(contracts[newc].c[j])
                survivors = tuple(k for k in ('lot1', 'lot2') if pos[k] == 'open')
                p_old = close_all(rollover_ts, old_px, 'rollover')
                A, dual = newc, None
                agree = new_tr is not None and (('bullish' if new_tr else 'bearish') == p_old['direction'])
                if new_tr is None or new_px is None:
                    stats['fallback_nodata'] += 1
                elif agree and basis is not None:
                    pos = open_pos(newc, p_old['direction'], rollover_ts, new_px, ref_px=basis, tid=p_old['trade_id'], parent=p_old, lots=survivors)
                    stats['fallback_go'] += 1
                else:
                    stats['fallback_nogo'] += 1
                if pos is not None:
                    run_exits(rollover_ts, bar_end)

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
    """One row per trade (rolled legs linked by trade_id), in the bespoke_trade_summary layout. The lot exit columns are the trade's FINAL exit of each lot (its last
    leg carrying that lot); points and rupees are summed over all legs and both lots."""
    cols = ['trade_id', 'contract_expiry', 'direction', 'entry_ts', 'entry_price', 'legs', 'sl_price', 'lot1_target', 'lot2_target',
            'lot1_exit_ts', 'lot1_exit_price', 'lot1_exit_reason', 'lot2_exit_ts', 'lot2_exit_price', 'lot2_exit_reason', 'total_pnl_points', 'total_pnl_rs']
    if legs.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for tid, g in legs.groupby('trade_id', sort=True):
        g = g.sort_values('leg_no')
        first, lastleg = g.iloc[0], g.iloc[-1]
        l1 = g[g['lot1_exit_ts'].notna()].iloc[-1] if g['lot1_exit_ts'].notna().any() else lastleg
        l2 = g[g['lot2_exit_ts'].notna()].iloc[-1] if g['lot2_exit_ts'].notna().any() else lastleg
        pts = round(g['pnl_pts'].sum(), 2)
        rows.append({'trade_id': tid, 'contract_expiry': first['contract'], 'direction': first['direction'], 'entry_ts': first['entry_ts'], 'entry_price': first['entry_px'],
                     'legs': len(g), 'sl_price': first['sl_px'], 'lot1_target': first['lot1_target'], 'lot2_target': first['lot2_target'],
                     'lot1_exit_ts': l1['lot1_exit_ts'], 'lot1_exit_price': l1['lot1_exit_px'], 'lot1_exit_reason': l1['lot1_exit_reason'],
                     'lot2_exit_ts': l2['lot2_exit_ts'], 'lot2_exit_price': l2['lot2_exit_px'], 'lot2_exit_reason': l2['lot2_exit_reason'],
                     'total_pnl_points': pts, 'total_pnl_rs': round(pts * configs.LOT_SIZE * configs.LOTS_PER_LEG, 2)})
    return pd.DataFrame(rows, columns=cols)


def metrics(t: pd.DataFrame) -> dict:
    if t.empty:
        return {'trades': 0}
    rs = t.sort_values('lot2_exit_ts')['total_pnl_rs']
    cum = rs.cumsum()
    dd = (cum - cum.cummax()).min()
    return {'trades': len(t), 'win_pct': round(float((rs > 0).mean() * 100), 1), 'total_rs': round(float(rs.sum())), 'max_dd_rs': round(float(dd))}


def main():
    end = sys.argv[1] if len(sys.argv) > 1 else str(pd.Timestamp.today().date())
    os.makedirs(configs.PARITY_OUT_DIR, exist_ok=True)
    print(f'Loading {configs.SYMBOL} per-contract 1-min data (no splice)...', flush=True)
    contracts = load_contracts()
    print(f'  {len(contracts)} contracts loaded', flush=True)
    legs = simulate(configs.PARITY_DATA_START, end, contracts)
    trades = to_trades(legs)
    legs.to_csv(os.path.join(configs.PARITY_OUT_DIR, 'parity_legs.csv'), index=False)
    trades.to_csv(os.path.join(configs.PARITY_OUT_DIR, 'parity_trades.csv'), index=False)
    print(f'window {configs.PARITY_DATA_START} -> {end}: {metrics(trades)}  open at end: {legs.attrs["open_at_end"]}')
    print('stats:', legs.attrs['stats'])


if __name__ == '__main__':
    main()
