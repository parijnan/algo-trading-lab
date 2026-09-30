"""
Typhon - production-parity backtest (plan Step 4). Adapted from Helios's own parity backtest
(helios_backtest/parity_backtest_helios.py, itself a verbatim port of Selene's) -- the roll
machinery is untouched; what's genuinely different here is that `simulate()` is parametrized by
ST_MULTIPLIER (and an optional SL%) instead of hardcoded to one decided config, so it can be run
across the whole multiplier shortlist, and NO back-adjustment is used anywhere (the whole point
of building this phase: real per-contract prices sidestep the percentage bug that back-adjustment
caused in Steps 2-3 -- see typhon_configs.py's PROVISIONAL_* section and the plan doc's
"percentage-bug correction").

The sweep/exit work so far (Steps 2-3) ran the raw signal on ONE back-adjusted continuous price
series. Production never sees that: it trades one real contract at a time, computes ST_15 from
that contract's OWN recent history (ST_SEED_DAYS), and handles a contract roll with explicit
machinery. This simulator mirrors that machinery instead of approximating it:

  * Per-contract ST: every session's ST is computed from the trading contract's own trailing
    ST_SEED_DAYS calendar days of 1-minute bars plus the day's bars. No splice, no adjustment.
  * Which contract trades on a day: production's own early-roll rule (the front contract, rolled
    to the next one once <= TENDER_ROLL_TRADING_DAYS trading days remain to its expiry).
  * Eve of a roll (tomorrow resolves to a different contract), per production's own rule:
      - FLAT at the start of the day, or as soon as the position closes for any reason: switch
        to the new contract immediately, watch its own signal.
      - IN TRADE: both contracts' ST are tracked all day. If the old contract's exit (flip or
        stop) happens and the new contract flips to the same direction on the SAME 15-minute
        bar, close old and open a fresh position on the new contract (two orders, never netted);
        otherwise close and switch, watching.
      - STILL IN TRADE at ROLLOVER_TIME: veto check (new contract's ST direction vs the
        position); GO -> flatten old, reopen on the new contract with the stop recalibrated off
        the historical basis (the new contract's price at the ORIGINAL entry time); NO-GO ->
        flatten only, then watch.
  * Fills: at the open of the bar after the signal bar; a stop at its level or the bar's open on
    an adverse gap; a session's first 1-minute bar is exempt from stop checks; entries need
    >= MIN_ENTRY_BUFFER_MIN since the session open.

This run (Step 4, first pass): SL/targets are OFF (sl_pct=None everywhere) -- the raw
trend-flip-only signal, same shape as Step 2's own sweep, but on real prices with real roll
execution, to reconfirm which multipliers actually hold up before spending effort calibrating
exits for any of them. The 2-lot scale-out + target calibration (through real rolls) is a
separate, materially bigger follow-up, not attempted in this file yet.

Not modelled: missed-rollover recovery (process assumed alive), costs/slippage, sizing. A rolled
position is one *trade* made of linked legs; P&L is the sum of the legs' real fills -- no spread
gain from a splice, unlike back-adjustment's synthetic continuity.

Output (data_sweep/): parity_raw_legs_mult{X.X}.csv, parity_raw_trades_mult{X.X}.csv, one pair
per multiplier in configs.PARITY_RAW_MULTIPLIERS.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import typhon_configs as configs
import typhon_data_loader as loader
from typhon_data_loader import _p3, resample_ohlcv, compute_st

MIN15 = pd.Timedelta(minutes=15)


class Contract:
    """One contract's 1-minute arrays, 15-minute bars, and per-(day, multiplier) ST windows."""

    def __init__(self, expiry, df_1m: pd.DataFrame):
        self.expiry = expiry
        df_1m = df_1m.sort_values('time_stamp').drop_duplicates('time_stamp').set_index('time_stamp')
        self.idx = df_1m.index
        self.o, self.h, self.l, self.c = (df_1m[k].to_numpy(float) for k in ('open', 'high', 'low', 'close'))
        self.v = df_1m['volume'].to_numpy(float)
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

    def day_rows(self, d, multiplier: float) -> dict:
        """ts -> row (open, close, trend, trend_flip) for day d, ST seeded from the trailing
        window, at the given multiplier (ST_PERIOD held fixed, same convention as every other
        phase)."""
        key = (pd.Timestamp(d), multiplier)
        if key not in self._st:
            day_key = key[0]
            lo = day_key - pd.Timedelta(days=configs.ST_SEED_DAYS)
            win = self.bars[(self.bars.index >= lo) & (self.bars.index < day_key + pd.Timedelta(days=1))]
            rows = {}
            if len(win) > configs.ST_PERIOD + 2:
                st = compute_st(win, configs.ST_PERIOD, multiplier)
                today = st[st.index.normalize() == day_key]
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

    def scan_exit(self, direction, sl, target, t0, t1):
        """First 1-min bar in [t0, t1) whose range crosses the stop OR the target -> (ts, fill,
        reason), else None. sl=None and/or target=None disables that side -- with both None, this
        always returns None and the trend-flip is the only exit (this pass's raw-signal
        behaviour, unchanged). Stop wins a same-bar tie against the target, same convention as
        exit_calib_typhon.py's own simulate(). Fill: the level, or the bar's open on a gap-through
        (favourable for a target, adverse for the stop)."""
        if sl is None and target is None:
            return None
        lo, hi = self.idx.searchsorted(t0, side='left'), self.idx.searchsorted(t1, side='left')
        if hi <= lo:
            return None
        act = ~self.guarded[lo:hi]
        h, l, o = self.h[lo:hi], self.l[lo:hi], self.o[lo:hi]
        sl_hit = (((l <= sl) if direction == 'bullish' else (h >= sl)) & act) if sl is not None else None
        t_hit = (((h >= target) if direction == 'bullish' else (l <= target)) & act) if target is not None else None
        i_sl = int(sl_hit.argmax()) if sl_hit is not None and sl_hit.any() else len(o)
        i_t = int(t_hit.argmax()) if t_hit is not None and t_hit.any() else len(o)
        if i_sl >= len(o) and i_t >= len(o):
            return None
        if i_sl <= i_t:   # stop wins a same-bar tie
            k, level, reason = i_sl, sl, 'stop_loss'
            fill = (o[k] if o[k] < level else level) if direction == 'bullish' else (o[k] if o[k] > level else level)
        else:
            k, level, reason = i_t, target, 'target'
            fill = (o[k] if o[k] > level else level) if direction == 'bullish' else (o[k] if o[k] < level else level)
        return self.idx[lo + k], float(fill), reason


def load_contracts(symbol, seg_start, seg_end) -> dict:
    """Per-contract 1-minute frames: Fyers where it has the contract's day, AngelOne only where
    it doesn't (typhon_configs.ANGELONE_OWN_FROM), same convention as Helios's/Selene's own
    loaders -- fills the Fyers void (2026-04-01..06-29, confirmed identical for NATGASMINI,
    plan Step 4) and nothing else."""
    cal = _p3._discover_expiries(symbol)
    fy = {e: _p3._read_contract_file(_p3.FYERS_DATA_DIR, symbol, e) for e in cal}
    fy_days = {e: set(d['time_stamp'].dt.date) for e, d in fy.items() if len(d)}
    parts = {e: [fy[e]] if len(fy[e]) else [] for e in cal}
    own_from = pd.Timestamp(configs.ANGELONE_OWN_FROM)
    for e in cal:
        ao = _p3._read_contract_file(_p3.ANGELONE_DATA_DIR, symbol, e)
        if ao.empty:
            continue
        own = ao[ao['time_stamp'] >= own_from]
        if len(own):
            parts[e].append(own)
        pre = ao[ao['time_stamp'] < own_from]
        for day, chunk in pre.groupby(pre['time_stamp'].dt.date):
            f = _p3._naive_front_month_for_date(day, cal)
            if f is not None and day not in fy_days.get(f, set()):
                parts[f].append(chunk)
    out = {}
    for e, ps in parts.items():
        ps = [x for x in ps if len(x)]
        if ps:
            df = pd.concat(ps, ignore_index=True).drop_duplicates('time_stamp', keep='first')
            out[e] = Contract(e, df)
    return out


def simulate(seg_start: str, seg_end: str, multiplier: float, sl_pct: float = None, target_pct: float = None,
            contracts: dict = None) -> pd.DataFrame:
    """sl_pct=None and target_pct=None (this file's own first/raw pass): no stop or target at
    all, trend-flip is the only exit, same shape as Step 2's own raw sweep. Single lot only --
    the 2-lot scale-out candidate (two independent targets through a roll) is a separate,
    materially bigger extension, not attempted here (plan Step 4). `contracts` may be passed in
    (already loaded) to avoid re-reading the same 1-minute files across a multiplier/grid loop --
    Contract.day_rows() caches ST by (day, multiplier), so re-running with a different sl_pct/
    target_pct for the SAME multiplier only re-walks the (cheap) state machine, not the ST
    computation (measured 2026-09-30: ~90x faster on a warm cache, 112s cold -> ~1.3s warm)."""
    symbol = configs.SYMBOL
    cal = _p3._discover_expiries(symbol)
    closed = loader._load_fully_closed_dates()
    if contracts is None:
        contracts = load_contracts(symbol, seg_start, seg_end)
    sgn = {'bullish': 1.0, 'bearish': -1.0}
    sl_frac = None if sl_pct is None else sl_pct / 100
    target_frac = None if target_pct is None else target_pct / 100

    legs, stats = [], {'flat_switch': 0, 'coincident': 0, 'noncoincident_switch': 0, 'stop_switch': 0,
                       'fallback_go': 0, 'fallback_nogo': 0, 'fallback_nodata': 0, 'forced_roll': 0, 'naive_day': 0}
    pos = None        # dict(trade_id, contract, direction, entry_ts, entry_px, sl, target, ref_px, parent_leg)
    pending = None    # dict(close, entry_dir, entry_contract)
    trade_id = 0

    def sl_for(ref_px, direction):
        return None if sl_frac is None else ref_px * (1 - sgn[direction] * sl_frac)

    def target_for(ref_px, direction):
        return None if target_frac is None else ref_px * (1 + sgn[direction] * target_frac)

    def open_pos(contract, direction, ts, px, ref_px=None, tid=None, parent=None):
        nonlocal trade_id
        if tid is None:
            trade_id += 1
            tid = trade_id
        ref = px if ref_px is None else ref_px
        return {'trade_id': tid, 'contract': contract, 'direction': direction, 'entry_ts': ts, 'entry_px': px,
                'ref_px': ref, 'sl': sl_for(ref, direction), 'target': target_for(ref, direction), 'parent': parent,
                'leg_no': 1 if parent is None else parent['leg_no'] + 1}

    def close_pos(ts, px, reason):
        nonlocal pos
        p = pos
        sl_display = round(p['sl'], 2) if p['sl'] is not None else None
        target_display = round(p['target'], 2) if p['target'] is not None else None
        legs.append({'trade_id': p['trade_id'], 'leg_no': p['leg_no'], 'contract': str(p['contract']),
                     'direction': p['direction'], 'entry_ts': p['entry_ts'], 'entry_px': p['entry_px'],
                     'ref_px': p['ref_px'], 'sl_px': sl_display, 'target_px': target_display, 'exit_ts': ts,
                     'exit_px': px, 'exit_reason': reason, 'pnl_pts': round(sgn[p['direction']] * (px - p['entry_px']), 2)})
        pos = None
        return p

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
            p_old = close_pos(ts_roll, px_old, 'forced_roll')
            new_open = contracts[C].open_at(first_new)
            pos = open_pos(C, p_old['direction'], first_new, new_open, ref_px=new_open, tid=p_old['trade_id'], parent=p_old)
            stats['forced_roll'] += 1

        if pos is not None:
            A = pos['contract']
            dual = N if eve and A == C else None
        else:
            A, dual = (N, None) if eve else (C, None)
            if eve:
                stats['flat_switch'] += 1

        rows = {c: contracts[c].day_rows(d, multiplier) for c in {A, C, N} if c in contracts and contracts[c].has_day(d)}
        times = sorted(set().union(*[set(r) for r in rows.values()]))
        last = contracts[C].last_ts[pd.Timestamp(d)]
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
                    close_pos(ts, r['open'] if r else contracts[pos['contract']].open_at(ts), 'trend_flip')
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
                hit = contracts[pos['contract']].scan_exit(pos['direction'], pos['sl'], pos['target'], ts, seg_t1)
                if hit:
                    close_pos(hit[0], hit[1], hit[2])
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
                if pos is not None:
                    hit = contracts[pos['contract']].scan_exit(pos['direction'], pos['sl'], pos['target'], rollover_ts, bar_end)
                    if hit:
                        close_pos(hit[0], hit[1], hit[2])

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
    if legs.empty:
        return pd.DataFrame(columns=['trade_id', 'direction', 'entry_ts', 'exit_ts', 'entry_px', 'legs', 'pnl_pts',
                                     'last_reason', 'pnl_pct'])
    g = legs.groupby('trade_id')
    t = g.agg(direction=('direction', 'first'), entry_ts=('entry_ts', 'first'), exit_ts=('exit_ts', 'last'),
              entry_px=('entry_px', 'first'), legs=('leg_no', 'max'), pnl_pts=('pnl_pts', 'sum'),
              last_reason=('exit_reason', 'last')).reset_index()
    t['pnl_pct'] = t['pnl_pts'] / t['entry_px'] * 100
    return t.sort_values('exit_ts').reset_index(drop=True)


def metrics(t: pd.DataFrame) -> dict:
    if t.empty:
        return {'trades': 0, 'win_pct': float('nan'), 'pnl_pts': 0, 'max_dd_pts': 0, 'calmar': float('nan'),
                'sum_pct': 0, 'calmar_pct': float('nan')}
    cum = t['pnl_pts'].cumsum()
    dd = (cum - cum.cummax()).min()
    cp = t['pnl_pct'].cumsum()
    ddp = (cp - cp.cummax()).min()
    return {'trades': len(t), 'win_pct': round((t['pnl_pts'] > 0).mean() * 100, 1), 'pnl_pts': round(t['pnl_pts'].sum()),
            'max_dd_pts': round(dd), 'calmar': round(t['pnl_pts'].sum() / abs(dd), 2) if dd else float('nan'),
            'sum_pct': round(t['pnl_pct'].sum(), 1), 'calmar_pct': round(t['pnl_pct'].sum() / abs(ddp), 2) if ddp else float('nan')}


def main():
    end = sys.argv[1] if len(sys.argv) > 1 else configs.PARITY_END_EXTENDED
    os.makedirs(configs.DATA_SWEEP_DIR, exist_ok=True)

    print(f'Loading {configs.SYMBOL} per-contract 1-min data (no splice, no back-adjustment)...')
    contracts = load_contracts(configs.SYMBOL, configs.DATA_START, end)
    print(f'  {len(contracts)} contracts loaded')

    split = pd.Timestamp(configs.PARITY_WALKFORWARD_SPLIT_DATE)
    summary_rows = []
    for mult in configs.ST_MULTIPLIER_GRID:
        legs = simulate(configs.DATA_START, end, mult, sl_pct=None, contracts=contracts)
        trades = to_trades(legs)
        legs.to_csv(os.path.join(configs.DATA_SWEEP_DIR, f'parity_raw_legs_mult{mult:.1f}.csv'), index=False)
        trades.to_csv(os.path.join(configs.DATA_SWEEP_DIR, f'parity_raw_trades_mult{mult:.1f}.csv'), index=False)

        all_m = metrics(trades)
        pre = metrics(trades[trades['entry_ts'] < split].reset_index(drop=True)) if len(trades) else all_m
        post = metrics(trades[trades['entry_ts'] >= split].reset_index(drop=True)) if len(trades) else all_m
        print(f"mult {mult:.1f}: all n={all_m['trades']} win%={all_m['win_pct']} pnl_pts={all_m['pnl_pts']} "
              f"calmar={all_m['calmar']} | pre n={pre['trades']} calmar={pre['calmar']} | "
              f"post n={post['trades']} calmar={post['calmar']}")
        summary_rows.append({'multiplier': mult, 'stats': legs.attrs['stats'], 'open_at_end': legs.attrs['open_at_end'],
                             **{f'all_{k}': v for k, v in all_m.items()}, **{f'pre_{k}': v for k, v in pre.items()},
                             **{f'post_{k}': v for k, v in post.items()}})

    pd.DataFrame(summary_rows).to_csv(os.path.join(configs.DATA_SWEEP_DIR, 'parity_raw_summary.csv'), index=False)
    print(f'\nwindow {configs.DATA_START} -> {end}, walk-forward split {configs.PARITY_WALKFORWARD_SPLIT_DATE}')


if __name__ == '__main__':
    main()
