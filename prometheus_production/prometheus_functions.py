"""
Prometheus production utilities.

Includes:
  - SupertrendIndicator + compute_st (copied verbatim from
    apollo_production/technical_indicators.py, same as iris_functions.py —
    production never imports the backtest, per §0/CLAUDE.md "Module naming")
  - Effective-contract resolution (§1) — single authoritative source, applies
    the 5-trading-day-early tender-margin roll
  - Seeding (Path A tail-read + Path B live poll unified via the same
    on-disk per-contract CSV, §1)
  - Resilient 1-min poller (§3), ported from mcx_live_downloader.py
  - Order placement + fill tracking via OrderFillWatcher (§2), ported from
    iris_functions.py (already strategy-agnostic)
  - Trade log / cumulative tracker helpers (§4), Apollo's naming convention
  - Guardian check (§0) — refuse to start if any NSE/BSE strategy is live
"""
import json
import sys
import threading
import time
from datetime import datetime, timedelta, date
from pathlib import Path

import pandas as pd

from prometheus_logger_setup import get_logger
from prometheus_configs import (
    REPO_ROOT, MCX_DATA_DIR, INSTRUMENT_MASTER_FILE, MCX_HOLIDAYS_FILE,
    FO_EXCHANGE, LOT_SIZE, SYMBOL,
    ST_PERIOD, ST_MULTIPLIER, SEED_DAYS, SESSION_START_TIME, CLOSING_TIME,
    ST_SEED_SKIP_DATES, TODAY_1M_CACHE_FILE,
    OPENING_BAR_CORRECTION_ENABLED, OPENING_BAR_ARTIFACT_THRESHOLD, CRUDEOIL_REFERENCE_SYMBOL,
    TENDER_ROLL_TRADING_DAYS, TARGET1_PCT, TARGET2_MODE, TARGET2_FLAT_PCT, SL_PCT,
    CANDLE_POLL_LIMIT, LTP_POLL_LIMIT, INNER_RETRY_ATTEMPTS, INNER_RETRY_INTERVAL_SEC,
    CANDLE_CLOSE_BUFFER_SEC, ORDER_TIMEOUT_SEC,
    REJECTION_RETRY_ATTEMPTS, REJECTION_RETRY_COOLDOWN_SEC,
    GHOST_RECOVERY_COOLDOWN_SEC, GHOST_RECOVERY_LOOKBACK_SEC,
    SEED_RETRY_ATTEMPTS, SEED_RETRY_INTERVAL_SEC,
    TRADES_FILE, TRADE_LOGS_DIR, COUNTER_FILE, SERIES_15M_FILE, SERIES_15M_RETENTION_DAYS,
)

sys.path.insert(0, str(REPO_ROOT / 'data_pipeline'))
import data_downloader_mcx as dl  # noqa: E402 — reused for get_futures_filepath,
                                   # date_range_chunks, fetch_candle_chunk, format_timestamp,
                                   # OHLCV_HEADERS (§1). Its own logging.basicConfig() at
                                   # import claims the root logger, but prometheus_logger_setup's
                                   # named-logger pattern is unaffected either way.

try:
    from SmartApi.smartWebSocketOrderUpdate import SmartWebSocketOrderUpdate
    _ORDER_WS_AVAILABLE = True
except ImportError:
    _ORDER_WS_AVAILABLE = False

try:
    from SmartApi.smartExceptions import DataException, NetworkException
except ImportError:
    class DataException(Exception):
        pass

    class NetworkException(Exception):
        pass

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Supertrend (copied verbatim from apollo_production/technical_indicators.py,
# same precedent as iris_functions.py — production never imports the backtest)
# ---------------------------------------------------------------------------

class SupertrendIndicator:
    def __init__(self, period=10, multiplier=3.0):
        self.period     = period
        self.multiplier = multiplier

    def calculate(self, df):
        df = df.copy()
        df['H-L']  = df['High'] - df['Low']
        df['H-PC'] = abs(df['High'] - df['Close'].shift(1))
        df['L-PC'] = abs(df['Low']  - df['Close'].shift(1))
        df['TR']   = df[['H-L', 'H-PC', 'L-PC']].max(axis=1)
        df['ATR']  = df['TR'].ewm(alpha=1 / self.period, adjust=False).mean()

        hl2              = (df['High'] + df['Low']) / 2
        df['UpperBand']  = hl2 + self.multiplier * df['ATR']
        df['LowerBand']  = hl2 - self.multiplier * df['ATR']

        trend      = True
        supertrend = []
        for i in range(len(df)):
            if i < self.period:
                supertrend.append(None)
                continue
            curr_close  = df['Close'].iloc[i]
            prev_upper  = df['UpperBand'].iloc[i - 1]
            prev_lower  = df['LowerBand'].iloc[i - 1]
            if curr_close > prev_upper:
                trend = True
            elif curr_close < prev_lower:
                trend = False
            if trend and df['LowerBand'].iloc[i] < prev_lower:
                df.loc[df.index[i], 'LowerBand'] = prev_lower
            if not trend and df['UpperBand'].iloc[i] > prev_upper:
                df.loc[df.index[i], 'UpperBand'] = prev_upper
            supertrend.append(df['LowerBand'].iloc[i] if trend
                              else df['UpperBand'].iloc[i])

        df['Supertrend'] = supertrend
        return df.drop(columns=['H-L', 'H-PC', 'L-PC', 'TR', 'ATR',
                                 'UpperBand', 'LowerBand'])


def compute_st(df: pd.DataFrame, period: int, multiplier: float) -> pd.DataFrame:
    """
    Add supertrend, trend (bool/NA), trend_flip to a lowercase-ohlcv df
    (time_stamp column, not index). Always recomputed from scratch over the
    FULL series passed in — never resumed from a persisted series (§1: "the
    Supertrend ratchet path is history-dependent and resuming mid-stream
    would silently diverge from a from-scratch computation").
    """
    base_cols = [c for c in ('time_stamp', 'open', 'high', 'low', 'close', 'volume')
                 if c in df.columns]
    d = df[base_cols].rename(columns={'open': 'Open', 'high': 'High',
                                      'low': 'Low', 'close': 'Close'})
    ind = SupertrendIndicator(period=period, multiplier=multiplier)
    r   = ind.calculate(d).rename(columns={
        'Open': 'open', 'High': 'high', 'Low': 'low',
        'Close': 'close', 'Supertrend': 'supertrend'})
    r['supertrend'] = pd.to_numeric(r['supertrend'], errors='coerce')
    r['trend'] = (r['close'] > r['supertrend']).astype(object)
    r.loc[r['supertrend'].isna(), 'trend'] = pd.NA
    r['trend_flip'] = r['trend'] != r['trend'].shift(1)
    r.loc[r['supertrend'].isna(), 'trend_flip'] = False
    first_valid_idx = r['supertrend'].first_valid_index()
    if first_valid_idx is not None:
        r.loc[first_valid_idx, 'trend_flip'] = False
    return r


# ---------------------------------------------------------------------------
# §1: Effective-contract resolution — single authoritative source, re-derived
# fresh once per session, applies the 5-trading-day-early tender-margin roll.
# ---------------------------------------------------------------------------

def _parse_expiry_from_master(expiry_str: str) -> datetime:
    return datetime.strptime(str(expiry_str).strip(), '%d%b%Y')


def _load_mcx_holidays() -> pd.DataFrame:
    """
    Returns a DataFrame with columns date/morning_session_closed/
    evening_session_closed. Empty (weekends-only) if the file is missing —
    logged loudly, since a missing holiday calendar silently corrupts the
    trading-day count used for the roll decision.
    """
    if not MCX_HOLIDAYS_FILE.exists():
        logger.error(f'mcx_holidays.csv not found at {MCX_HOLIDAYS_FILE} — '
                     f'trading-day counts will only exclude weekends, not MCX holidays. '
                     f'Roll timing may be wrong.')
        return pd.DataFrame(columns=['date', 'morning_session_closed', 'evening_session_closed'])
    df = pd.read_csv(MCX_HOLIDAYS_FILE)
    df['date'] = pd.to_datetime(df['date']).dt.date
    return df


def mcx_fully_closed_today(today: date = None) -> tuple:
    """
    §0's "Prometheus's own startup/daily gate" — distinct from Leto's
    NSE/BSE holiday check (data/holidays.csv), and distinct from
    _count_trading_days_inclusive's roll-day counting above (that's about
    which contract to trade once running; this is about whether to start
    running at all today). Weekends always count as closed even if
    mcx_holidays.csv has no row for the date (it only lists actual
    holidays, not every Saturday/Sunday).

    A day only counts as "fully closed" if BOTH sessions are closed — same
    rule as the roll-day counter, for the same reason (2026-01-01: morning
    open, evening closed, is a genuine trading day, not a day to skip).

    Returns (fully_closed, reason_or_None).
    """
    today = today or date.today()
    if today.weekday() >= 5:
        return True, 'weekend'
    holidays_df = _load_mcx_holidays()
    if holidays_df.empty:
        return False, None
    row = holidays_df[holidays_df['date'] == today]
    if row.empty:
        return False, None
    row = row.iloc[0]
    if bool(row['morning_session_closed']) and bool(row['evening_session_closed']):
        return True, str(row.get('holiday_name', 'MCX holiday'))
    return False, None


def next_trading_day(today: date) -> date:
    """
    §4: the next day after `today` that isn't fully closed (weekend or an
    MCX holiday where both sessions are closed) — NOT a naive `today + 1`,
    which only happens to be correct across a weekend by accident of
    `_count_trading_days_inclusive`'s own separate skip-weekends behavior.
    Same fully-closed definition as `mcx_fully_closed_today`.
    """
    holidays_df = _load_mcx_holidays()
    fully_closed = set()
    if not holidays_df.empty:
        for _, row in holidays_df.iterrows():
            if bool(row['morning_session_closed']) and bool(row['evening_session_closed']):
                fully_closed.add(row['date'])
    d = today + timedelta(days=1)
    while d.weekday() >= 5 or d in fully_closed:
        d += timedelta(days=1)
    return d


def _count_trading_days_inclusive(start_date: date, end_date: date,
                                  holidays_df: pd.DataFrame) -> int:
    """
    Trading days in [start_date, end_date] inclusive. A date counts as a
    trading day unless BOTH sessions are closed — a date closing only one
    session (e.g. 2026-01-01: morning open, evening closed) still counts.
    This is deliberate, not an oversight: an earlier draft assumed "morning
    closed" alone meant a non-trading day, which mcx_holidays.csv's own
    2026-01-01 row disproves (see plan §0).
    """
    if start_date > end_date:
        return 0
    fully_closed = set()
    if not holidays_df.empty:
        for _, row in holidays_df.iterrows():
            if bool(row['morning_session_closed']) and bool(row['evening_session_closed']):
                fully_closed.add(row['date'])
    days = pd.date_range(start_date, end_date, freq='D')
    return sum(1 for d in days if d.weekday() < 5 and d.date() not in fully_closed)


def resolve_effective_contract(symbol: str = None, today: date = None) -> dict:
    """
    Return the contract Prometheus trades for the ENTIRE session — token,
    expiry, symbol, on-disk file path — resolved once, never re-derived
    mid-session (§1's "single, authoritative effective contract" decision).

    Applies the tender-margin early roll: the exchange front-month contract,
    unless fewer than TENDER_ROLL_TRADING_DAYS trading days remain until its
    expiry, in which case the next contract out is used instead.
    """
    symbol = symbol or SYMBOL
    today  = today or date.today()

    instruments_df = pd.read_csv(INSTRUMENT_MASTER_FILE)
    instruments_df['expiry_parsed'] = instruments_df['expiry'].apply(_parse_expiry_from_master)
    candidates = instruments_df[
        (instruments_df['name'] == symbol) &
        (instruments_df['expiry_parsed'].dt.date >= today)
    ].sort_values('expiry_parsed').reset_index(drop=True)

    if candidates.empty:
        raise RuntimeError(f'No live {symbol} contract found in {INSTRUMENT_MASTER_FILE} '
                           f'— has the shared data pipeline refreshed the instrument master today?')

    holidays_df = _load_mcx_holidays()
    front = candidates.iloc[0]
    trading_days_left = _count_trading_days_inclusive(today, front['expiry_parsed'].date(), holidays_df)

    rolled_early = False
    if trading_days_left <= TENDER_ROLL_TRADING_DAYS:
        if len(candidates) > 1:
            chosen = candidates.iloc[1]
            rolled_early = True
            logger.info(f'{symbol}: front-month {front["symbol"]} has {trading_days_left} trading '
                        f'day(s) left (<= {TENDER_ROLL_TRADING_DAYS}) — rolling early to {chosen["symbol"]}.')
        else:
            chosen = front
            logger.warning(f'{symbol}: front-month {front["symbol"]} has only {trading_days_left} '
                           f'trading day(s) left and NO next contract is listed in the instrument '
                           f'master yet — cannot roll early. Trading the outgoing contract through '
                           f'the tender-margin window. Check if data_downloader_mcx.py needs a fresher '
                           f'scrip master.')
    else:
        chosen = front

    expiry_date = chosen['expiry_parsed'].to_pydatetime()
    return {
        'symbol_root':          symbol,
        'symbol':                chosen['symbol'],
        'token':                 str(chosen['token']),
        'expiry_date':           expiry_date,
        'filepath':              dl.get_futures_filepath(symbol, expiry_date),
        'rolled_early':          rolled_early,
        'trading_days_to_expiry': trading_days_left if not rolled_early else None,
        # §1: read live off the instrument master, not a hardcoded constant
        # like the other four strategies' QTY_FREEZE — MCX freeze quantities
        # are set per-commodity and could differ or change over time.
        'freeze_qty':            int(chosen['freeze_qty']),
    }


# ---------------------------------------------------------------------------
# §1: Backfill a newly-effective contract's own history the first time it's
# needed, and tail-read for seeding — same merge-dedup logic
# data_downloader_mcx.py / mcx_live_downloader.py already use.
# ---------------------------------------------------------------------------

def _merge_and_save(filepath: str, new_df: pd.DataFrame) -> int:
    """
    Fixed 2026-09-04 — a real, pre-existing bug, not something new to this
    session's changes: `on_disk` (re-read from a file already carrying
    `+05:30`-suffixed timestamps) parses to a FIXED-OFFSET tz
    (`UTC+05:30`), while `new_df` used to be explicitly localized to the
    NAMED zone `Asia/Kolkata` — numerically identical, but different pandas
    tz dtypes. Concatenating the two silently fell back to `object` dtype,
    and the subsequent `pd.to_datetime(..., utc=False)` on that mixed-
    representation column silently turned the OLDER (on-disk) rows into
    NaT on every second-or-later call against the same file — i.e. on
    every multi-chunk backfill and every private-cache write this session
    added (§15), not a contrived edge case. Fix: normalize both sides to
    tz-naive before concatenating; `dl.format_timestamp` already localizes
    a naive timestamp to Asia/Kolkata per-row at save time on its own, so
    the separate pre-localization step wasn't even needed.
    """
    if new_df is None or new_df.empty:
        return 0
    import os
    on_disk = (pd.read_csv(filepath, parse_dates=['time_stamp'])
               if os.path.exists(filepath) else pd.DataFrame(columns=dl.OHLCV_HEADERS))
    if not on_disk.empty:
        on_disk['time_stamp'] = pd.to_datetime(on_disk['time_stamp'], utc=False, errors='coerce')
        if on_disk['time_stamp'].dt.tz is not None:
            on_disk['time_stamp'] = on_disk['time_stamp'].dt.tz_localize(None)
    new_df = new_df.copy()
    if new_df['time_stamp'].dt.tz is not None:
        new_df['time_stamp'] = new_df['time_stamp'].dt.tz_localize(None)
    before = len(on_disk)
    merged = pd.concat([on_disk, new_df], ignore_index=True)
    merged['time_stamp'] = pd.to_datetime(merged['time_stamp'], utc=False, errors='coerce')
    merged.drop_duplicates(subset=['time_stamp'], keep='first', inplace=True)
    merged.sort_values('time_stamp', inplace=True)
    merged.reset_index(drop=True, inplace=True)
    new_rows = len(merged) - before
    save_df = merged.copy()
    save_df['time_stamp'] = save_df['time_stamp'].apply(lambda ts: dl.format_timestamp(ts, dl.OPTIONS_TS_FMT))
    save_df.to_csv(filepath, index=False)
    return new_rows


def backfill_contract_if_needed(obj, contract: dict, seed_days: int = SEED_DAYS) -> None:
    """
    §1: "Prometheus's own seed logic can fetch a new effective contract's
    needed window directly from AngelOne the first time it needs it, and
    persist the result into that contract's own file... Once the shared
    pipeline's own roll eventually reaches that contract, it just continues
    extending the same file."
    """
    import os
    filepath = contract['filepath']
    needed_from = datetime.now() - timedelta(days=seed_days)

    if os.path.exists(filepath):
        existing = pd.read_csv(filepath, parse_dates=['time_stamp'])
        if not existing.empty:
            existing['time_stamp'] = pd.to_datetime(existing['time_stamp'], utc=False, errors='coerce')
            if existing['time_stamp'].min().replace(tzinfo=None) <= needed_from:
                return   # already has enough history

    fetch_to = min(contract['expiry_date'], datetime.now())
    if needed_from > fetch_to:
        return
    logger.info(f"Backfilling {contract['symbol']} from {needed_from:%Y-%m-%d} to "
                f"{fetch_to:%Y-%m-%d} for ST seeding (new effective contract).")
    total_new = 0
    for chunk_start, chunk_end in dl.date_range_chunks(needed_from, fetch_to):
        chunk_df = dl.fetch_candle_chunk(obj, contract['token'], chunk_start, chunk_end)
        if not chunk_df.empty:
            total_new += _merge_and_save(filepath, chunk_df)
    logger.info(f"Backfill complete: {total_new} new row(s) added to {filepath}")


# ---------------------------------------------------------------------------
# §15 (2026-09-04): private, Prometheus-only intraday cache — TODAY_1M_CACHE_FILE.
# Nothing else reads or writes this file (unlike the shared per-contract CSV
# above), so none of the write-race/single-source-of-truth reasoning that
# removed Prometheus's writes to the shared file applies here. Written
# incrementally through the day (_merge_and_save, reused as-is — it's
# already generic over `filepath`), cleared at logoff, and always
# date-filtered on read as a second line of defense against an ungraceful
# crash that skipped the clear.
# ---------------------------------------------------------------------------

def read_today_cache(now: datetime) -> pd.DataFrame:
    import os
    if not os.path.exists(TODAY_1M_CACHE_FILE):
        return pd.DataFrame(columns=dl.OHLCV_HEADERS)
    df = pd.read_csv(TODAY_1M_CACHE_FILE, parse_dates=['time_stamp'])
    if df.empty:
        return df
    df['time_stamp'] = pd.to_datetime(df['time_stamp'], utc=False, errors='coerce').dt.tz_localize(None)
    today = now.date()
    return df[df['time_stamp'].dt.date == today].sort_values('time_stamp').reset_index(drop=True)


def clear_today_cache() -> None:
    """Called from _teardown() at end of session — the cache only ever
    represents "today," so it must never survive unfiltered into the next
    session. Belt-and-suspenders: read_today_cache() also date-filters, so
    a crash that skips this still can't corrupt the next day's seed."""
    import os
    try:
        if os.path.exists(TODAY_1M_CACHE_FILE):
            os.remove(TODAY_1M_CACHE_FILE)
    except Exception as e:
        logger.warning(f'clear_today_cache: failed to remove {TODAY_1M_CACHE_FILE}: {e}')


def _tail_read_contract_csv(filepath: str, now: datetime, n_days: int) -> pd.DataFrame:
    """
    Tail-based read of the current effective contract's own file — same
    68x-speedup pattern as iris_functions.py's _tail_read_nifty_csv, sized
    for MCX's ~870-900 rows/trading day (09:00 -> 23:30-23:55).
    """
    import os
    import subprocess
    from io import StringIO
    if not os.path.exists(filepath):
        return pd.DataFrame(columns=dl.OHLCV_HEADERS)

    tail_lines_per_day = 950
    tail_n = tail_lines_per_day * (n_days + 5)
    header = subprocess.run(['head', '-1', str(filepath)], capture_output=True, text=True).stdout
    tail   = subprocess.run(['tail', f'-n{tail_n}', str(filepath)], capture_output=True, text=True).stdout
    if not tail.strip():
        return pd.DataFrame(columns=dl.OHLCV_HEADERS)
    df = pd.read_csv(StringIO(header + tail))
    df['time_stamp'] = pd.to_datetime(df['time_stamp'], utc=False, errors='coerce').dt.tz_localize(None)
    cutoff = pd.Timestamp(now).normalize() - timedelta(days=n_days)
    df = df[df['time_stamp'] >= cutoff]
    # §15 (2026-09-04): explicit cutoff, not "whatever's on the file" — the
    # shared file may still contain some of today's rows during the
    # transition to write-removal, or none at all once it's fully deployed.
    # Excluding today explicitly avoids depending on which is true; today's
    # data always comes from the private cache + live gap-fetch instead
    # (seed_st15).
    df = df[df['time_stamp'].dt.date < now.date()]
    if ST_SEED_SKIP_DATES:
        skip = {pd.Timestamp(d).date() for d in ST_SEED_SKIP_DATES}
        df = df[~df['time_stamp'].dt.date.isin(skip)]
    return df.sort_values('time_stamp').reset_index(drop=True)


def _resample_1m_to_Nmin(df_1m: pd.DataFrame, minutes: int, now: datetime = None) -> pd.DataFrame:
    """
    Resample 1-min OHLCV to N-min, anchored at SESSION_START_TIME (09:00),
    day-by-day — same fixed-clock-time-bucket approach as
    iris_functions.py's _resample_to_15m/_resample_1m_to_5m, one level up.
    Each day's buckets stop at CLOSING_TIME (variable, DST-dependent).
    Generalized from the original 15-min-only _resample_1m_to_15m (plan §17,
    2026-09-04) so a future 1h resample shares this one guard instead of a
    second, potentially-drifting copy of it.

    A window is only included once it's genuinely done -- either it has
    fully elapsed by `now` (defaults to datetime.now()), or the session
    itself has already closed for that day. These are deliberately NOT the
    same check:
      - "hasn't fully elapsed, session still open" -> skip. More 1-min rows
        are still going to arrive for this window; computing it now would
        build a bar from partial data that silently changes once the rest
        arrives. This is the original 2026-08-31 failure mode: a 12:13:49
        restart produced ST=8105.67 from a bar built on only ~13 of the
        12:00-12:15 window's 15 minutes, vs. the chart's correct 8102.33.
      - "hasn't fully elapsed, but the session has already closed for that
        day" -> compute it anyway, from whatever real rows exist. Nothing
        more will EVER arrive for it, so it's exactly as complete as it's
        going to get -- and the chart shows this trailing bucket as one bar
        regardless of duration, not a dropped one. Concretely: on a
        CLOSING_TIME=23:55 day, the last 15-min bucket only ever has 10
        real minutes (895/15=59.67); on EITHER CLOSING_TIME, the last
        60-min bucket only ever has 30 or 55 real minutes (870/60=14.5,
        895/60=14.92) -- every single day, not just an edge case.
    """
    now = now or datetime.now()
    candles = []
    for day, day_df in df_1m.groupby(df_1m['time_stamp'].dt.date):
        anchor       = pd.Timestamp(f'{day} {SESSION_START_TIME}')
        day_cutoff   = pd.Timestamp(f'{day} {CLOSING_TIME}')
        day_has_closed = now >= day_cutoff   # true for any past day; true for
                                              # today only once today's own close has passed
        while anchor <= day_cutoff:
            window_end = anchor + timedelta(minutes=minutes) - timedelta(minutes=1)
            if anchor + timedelta(minutes=minutes) > now and not day_has_closed:
                break   # still forming, live session -- more data still coming, wait
            window = day_df[(day_df['time_stamp'] >= anchor) & (day_df['time_stamp'] <= window_end)]
            if not window.empty:
                candles.append({
                    'time_stamp': anchor,
                    'open':       window['open'].iloc[0],
                    'high':       window['high'].max(),
                    'low':        window['low'].min(),
                    'close':      window['close'].iloc[-1],
                    'volume':     window['volume'].sum(),
                })
            anchor += timedelta(minutes=minutes)
    if not candles:
        return pd.DataFrame()
    return pd.DataFrame(candles).reset_index(drop=True)


# ---------------------------------------------------------------------------
# §11 (2026-09-04): opening-bar price-artifact protection. CRUDEOILM's very
# first 1-min candle of the session has shown a recurring thin-liquidity
# price-discovery artifact (7 confirmed instances, 2026-03 through 2026-09)
# that distorts ST_15's ATR for ~ST_PERIOD bars afterward. CRUDEOIL is
# confirmed reliable at that exact minute every time.
# ---------------------------------------------------------------------------

def patch_opening_bar_if_artifact(m_bar: dict, o_bar: dict,
                                  threshold: float = OPENING_BAR_ARTIFACT_THRESHOLD) -> dict:
    """
    m_bar/o_bar: the 09:00 1-min candle for CRUDEOILM/CRUDEOIL, each
    {'open','high','low','close'}. Same underlying, same per-barrel price
    (only lot size differs — no scaling needed). If CRUDEOIL's true range is
    under `threshold` of CRUDEOILM's at this exact minute, CRUDEOILM's own
    print is thin-liquidity noise — substitute CRUDEOIL's OHLC outright.
    Runs once, right after the 09:00 candle downloads; BAU for the rest of
    the session either way.

    Gated by OPENING_BAR_CORRECTION_ENABLED (default False, 2026-09-04):
    when disabled, this still runs and logs what it WOULD have done, but
    returns m_bar unpatched — lets the user validate ST accuracy against
    the raw, uncorrected broker chart before trusting a live substitution.
    """
    m_tr = m_bar['high'] - m_bar['low']
    o_tr = o_bar['high'] - o_bar['low']
    if m_tr > 0 and (o_tr / m_tr) < threshold:
        verb = 'substituting' if OPENING_BAR_CORRECTION_ENABLED else 'would substitute (correction disabled)'
        logger.warning(f'Opening-bar artifact: CRUDEOILM TR={m_tr} vs CRUDEOIL TR={o_tr} '
                       f'(ratio {o_tr / m_tr:.2f}) — {verb}.')
        if OPENING_BAR_CORRECTION_ENABLED:
            return dict(o_bar)
    return m_bar


def fetch_crudeoil_opening_bar(obj, session_start_ts: datetime) -> dict:
    """
    Live poll of CRUDEOIL's own 09:00 1-min candle — needed once per session
    as the reference for patch_opening_bar_if_artifact. Reuses
    resolve_effective_contract (already supports an arbitrary symbol) and
    fetch_one_minute_window (same resilient 3-attempt burst as every other
    1-min fetch in this file) — not new infrastructure, a new call site.
    Returns None on failure; caller must treat that as "reference
    unavailable," never fabricate one.
    """
    try:
        contract = resolve_effective_contract(CRUDEOIL_REFERENCE_SYMBOL, session_start_ts.date())
    except Exception as e:
        logger.error(f'fetch_crudeoil_opening_bar: could not resolve {CRUDEOIL_REFERENCE_SYMBOL} '
                     f'contract: {e}')
        return None
    df = fetch_one_minute_window(obj, contract['token'], session_start_ts,
                                 session_start_ts + timedelta(minutes=1))
    if df is None or df.empty:
        logger.error(f'fetch_crudeoil_opening_bar: no {CRUDEOIL_REFERENCE_SYMBOL} data for '
                     f'{session_start_ts:%H:%M}.')
        return None
    row = df.iloc[0]
    return {'open': row['open'], 'high': row['high'], 'low': row['low'], 'close': row['close']}


def _find_Nmin_gaps(df: pd.DataFrame, minutes: int = 15) -> list:
    """Same principle as iris_functions.py's _find_5m_gaps — refuse to seed
    silently over a non-contiguous reconstructed series. Generalized
    (2026-09-04, §17) from the original 15-min-only _find_15m_gaps so
    seed_st15 (15m) and the 1h-alignment filter's own ST computation (§17)
    share one gap-check instead of a second copy that could drift."""
    gaps = []
    freq = f'{minutes}min'
    for day, day_df in df.groupby(df['time_stamp'].dt.date):
        ts = day_df['time_stamp'].sort_values()
        expected = pd.date_range(ts.iloc[0], ts.iloc[-1], freq=freq)
        missing = sorted(set(expected) - set(ts))
        if missing:
            gaps.append((day, missing))
    return gaps


# ---------------------------------------------------------------------------
# §6/§8 (2026-09-04): rollover support — ST for a not-yet-effective contract
# (the veto-check), and the historical-basis price lookup.
# ---------------------------------------------------------------------------

def compute_st_for_contract(contract: dict, today_1m: pd.DataFrame, now: datetime,
                            minutes: int = 15, st_period: int = None,
                            st_multiplier: float = None) -> pd.DataFrame:
    """
    Assembles past-days (shared pipeline file) + today (given,
    already-fetched live) for a contract, resamples to `minutes`-minute
    buckets, computes ST from scratch. Not routed through seed_st15
    itself: that function's private-cache/backfill machinery is specific
    to self._contract, the CURRENTLY effective one — this can be called
    against a contract that isn't (yet or ever) that.

    Originally built for §6 step 4's rollover go/no-go veto (15-min,
    st_period/st_multiplier default to ST_PERIOD/ST_MULTIPLIER) — reused
    as-is for §17's 1h-alignment filter (minutes=60,
    st_period=ST_1H_PERIOD, st_multiplier=ST_1H_MULTIPLIER) rather than a
    second copy of this shape, matching the same "generalize, don't
    duplicate" principle §17 already applied to _resample_1m_to_Nmin.

    Refuses (returns empty) on any gap, same "no silent staleness"
    convention as seed_st15 — for the rollover veto this means no-go; for
    the 1h filter it means "can't confirm agreement," treated the same way.
    """
    st_period = st_period if st_period is not None else ST_PERIOD
    st_multiplier = st_multiplier if st_multiplier is not None else ST_MULTIPLIER
    raw_1m_past = _tail_read_contract_csv(contract['filepath'], now, SEED_DAYS)
    raw_1m = (pd.concat([raw_1m_past, today_1m], ignore_index=True)
              .sort_values('time_stamp').reset_index(drop=True))
    if raw_1m.empty:
        return pd.DataFrame()
    df_Nm_raw = _resample_1m_to_Nmin(raw_1m, minutes, now)
    if df_Nm_raw.empty:
        return pd.DataFrame()
    if _find_Nmin_gaps(df_Nm_raw, minutes):
        logger.error(f"compute_st_for_contract: gap(s) in reconstructed {minutes}m series for "
                     f"{contract['symbol']} — refusing.")
        return pd.DataFrame()
    return compute_st(df_Nm_raw, st_period, st_multiplier)


def historical_basis_price(new_contract: dict, historical_ts: datetime) -> float:
    """
    §8: the historical-basis method's core lookup — what the NEW contract
    was trading at, at the SAME historical timestamp the original entry
    happened on the OLD contract. Reads the new contract's own on-disk file
    directly (not a tail-read — this needs one specific historical point,
    not a recent window; the file has real history because the nightly
    pipeline has been tracking this contract as next-month since before
    today, per the plan's prerequisite). Returns None — never a guessed or
    interpolated price — if nothing is close enough to trust.
    """
    import os
    filepath = new_contract['filepath']
    if not os.path.exists(filepath):
        logger.error(f"historical_basis_price: no file for {new_contract['symbol']} at {filepath}.")
        return None
    df = pd.read_csv(filepath, parse_dates=['time_stamp'])
    if df.empty:
        logger.error(f"historical_basis_price: {filepath} is empty.")
        return None
    df['time_stamp'] = pd.to_datetime(df['time_stamp'], utc=False, errors='coerce').dt.tz_localize(None)
    target = pd.Timestamp(historical_ts)
    if target.tzinfo is not None:
        target = target.tz_localize(None)
    df['_delta'] = (df['time_stamp'] - target).abs()
    nearest = df.loc[df['_delta'].idxmin()]
    if nearest['_delta'] > pd.Timedelta(minutes=5):
        logger.error(f"historical_basis_price: nearest {new_contract['symbol']} row to "
                     f"{historical_ts} is {nearest['_delta']} away — too far to trust.")
        return None
    return float(nearest['close'])


def persist_15m_series(df_15m: pd.DataFrame) -> None:
    """
    §4: size-bounded debug dump, trailing SERIES_15M_RETENTION_DAYS trading
    days (~65 bars/day). Never read back as authoritative (§1) — purely for
    human visibility.
    """
    try:
        trimmed = df_15m
        days = sorted(df_15m['time_stamp'].dt.date.unique())
        if len(days) > SERIES_15M_RETENTION_DAYS:
            keep_from = days[-SERIES_15M_RETENTION_DAYS]
            trimmed = df_15m[df_15m['time_stamp'].dt.date >= keep_from]
        trimmed.to_csv(SERIES_15M_FILE, index=False)
    except Exception as e:
        logger.warning(f'persist_15m_series: failed to write debug CSV: {e}')


def seed_st15(obj, contract: dict, now: datetime) -> pd.DataFrame:
    """
    §15 (2026-09-04): past days come from the shared pipeline file (never
    written by Prometheus); today comes from the private intraday cache
    plus a live gap-fetch for whatever it doesn't already cover — same code
    path for a fresh 09:00 start (an essentially-empty gap) and a mid-day
    crash-restart (the real gap-filler), same as the old design's intent,
    just re-targeted at a file only Prometheus ever touches. Resample to
    15-min, gap-check, compute ST from scratch. Refuses to seed (returns
    empty df) on any gap or missing data, rather than seeding silently stale.
    """
    backfill_contract_if_needed(obj, contract)   # defensive: genuinely missing OLDER history
    raw_1m_past = _tail_read_contract_csv(contract['filepath'], now, SEED_DAYS)

    cached_today = read_today_cache(now)
    session_start = pd.Timestamp(f'{now.date()} {SESSION_START_TIME}')
    gap_from = (cached_today['time_stamp'].max() + timedelta(minutes=1)
                if not cached_today.empty else session_start)
    if gap_from < now:
        gap_df = fetch_one_minute_window(obj, contract['token'], gap_from, now)
        if gap_df is not None and not gap_df.empty:
            _merge_and_save(TODAY_1M_CACHE_FILE, gap_df)
            cached_today = (pd.concat([cached_today, gap_df], ignore_index=True)
                             .drop_duplicates(subset=['time_stamp'], keep='last')
                             .sort_values('time_stamp').reset_index(drop=True))
        elif cached_today.empty:
            logger.error('seed_st15: no cached today data and live gap-fetch failed — cannot seed.')
            return pd.DataFrame()
        else:
            logger.warning(f'seed_st15: live gap-fetch failed, proceeding with cached data only '
                           f'(through {cached_today["time_stamp"].max()}).')

    raw_1m = (pd.concat([raw_1m_past, cached_today], ignore_index=True)
              .sort_values('time_stamp').reset_index(drop=True))
    if raw_1m.empty:
        logger.error('seed_st15: no 1-min history available after backfill + cache + live fetch.')
        return pd.DataFrame()

    df_15m_raw = _resample_1m_to_Nmin(raw_1m, 15, now)
    if df_15m_raw.empty:
        logger.error('seed_st15: resample produced no 15-min bars.')
        return pd.DataFrame()

    gaps = _find_Nmin_gaps(df_15m_raw, 15)
    if gaps:
        logger.error(f'seed_st15: gap(s) in reconstructed 15m series, refusing to seed: {gaps}')
        return pd.DataFrame()

    df_15m = compute_st(df_15m_raw, ST_PERIOD, ST_MULTIPLIER)
    logger.info(f'Seeded: {len(df_15m)} 15-min bars from {contract["symbol"]} '
                f'({raw_1m["time_stamp"].dt.date.nunique()} trading day(s) of 1-min history) | '
                f'trend={df_15m.iloc[-1]["trend"]} ST={df_15m.iloc[-1]["supertrend"]:.2f}')

    flips = df_15m[df_15m['trend_flip'] == True]
    if not flips.empty:
        last = flips.iloc[-1]
        logger.info(f'  Last 15m flip: {last["time_stamp"]} -> '
                    f'{"bullish" if last["trend"] == True else "bearish"}  '
                    f'close={last["close"]:.2f}  ST={last["supertrend"]:.2f}')

    persist_15m_series(df_15m)
    return df_15m


# ---------------------------------------------------------------------------
# §3: Resilient 1-min REST poller — ported directly from
# mcx_live_downloader.py's proven pattern (471 cycles, 0 unrecovered
# windows, overnight 2026-08-28 run).
# ---------------------------------------------------------------------------

_candle_counter = {'count': 0, 'limit': CANDLE_POLL_LIMIT, 'last_reset': 0.0}


def _check_candle_limit():
    now = time.time()
    if now - _candle_counter['last_reset'] > 1.0:
        _candle_counter['count'] = 0
        _candle_counter['last_reset'] = now
    if _candle_counter['count'] >= _candle_counter['limit']:
        time.sleep(1.1)
        _candle_counter['count'] = 0
        _candle_counter['last_reset'] = time.time()
        return
    _candle_counter['count'] += 1


def sleep_until_next_boundary(buffer_sec: float = CANDLE_CLOSE_BUFFER_SEC) -> datetime:
    """Re-derived from wall-clock time every call — immune to drift across a
    long unattended session (mcx_live_downloader.py's _sleep_until_next_boundary)."""
    now = datetime.now()
    boundary = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    target = boundary + timedelta(seconds=buffer_sec)
    sleep_for = (target - datetime.now()).total_seconds()
    if sleep_for > 0:
        time.sleep(sleep_for)
    return boundary


def fetch_one_minute_window(obj, token: str, from_dt: datetime, to_dt: datetime):
    """
    Inner burst: 3 attempts, 1s apart, every attempt logged. Returns a
    DataFrame (possibly empty on success) or None if the whole burst fails —
    caller defers to the outer, non-blocking pending-recovery queue.
    """
    from_str = from_dt.strftime('%Y-%m-%d %H:%M')
    to_str   = to_dt.strftime('%Y-%m-%d %H:%M')

    for attempt in range(1, INNER_RETRY_ATTEMPTS + 1):
        try:
            _check_candle_limit()
            response = obj.getCandleData({
                'exchange': FO_EXCHANGE, 'symboltoken': token, 'interval': 'ONE_MINUTE',
                'fromdate': from_str, 'todate': to_str,
            })
            raw = response.get('data') if isinstance(response, dict) else None
            if raw is not None:
                logger.info(f'Fetch OK [{from_str} -> {to_str}] attempt {attempt}/{INNER_RETRY_ATTEMPTS} '
                            f'({len(raw)} candle(s))')
                df = pd.DataFrame(raw, columns=dl.OHLCV_HEADERS)
                df['time_stamp'] = pd.to_datetime(df['time_stamp'], format=dl.METHOD_TS_FMT,
                                                   utc=False, errors='coerce')
                # Real bug, caught live 2026-09-04: METHOD_TS_FMT's '%z' makes
                # this tz-AWARE (fixed offset, from the broker's '+05:30'
                # suffix), while every other in-memory series in this file
                # (_tail_read_contract_csv, read_today_cache, _df_1m_today) is
                # tz-naive. Three of five call sites already strip this
                # defensively right after calling this function; seed_st15's
                # gap-fetch (the very first live call, first-ever run) didn't,
                # so a fresh empty cache's gap_df collided with the tz-naive
                # shared-pipeline past-days data on the next concat --
                # "Cannot compare tz-naive and tz-aware timestamps". Fixed at
                # the source so every caller gets naive without having to
                # remember to strip it themselves.
                if df['time_stamp'].dt.tz is not None:
                    df['time_stamp'] = df['time_stamp'].dt.tz_localize(None)
                return df
            logger.warning(f'Fetch failed (empty response) [{from_str} -> {to_str}] '
                           f'attempt {attempt}/{INNER_RETRY_ATTEMPTS}: {response}')
        except Exception as e:
            error_code = 'AB1021' if 'exceeding access rate' in str(e) else 'EXCEPTION'
            log_fn = logger.warning if error_code == 'AB1021' else logger.error
            log_fn(f'Fetch failed [{from_str} -> {to_str}] attempt {attempt}/{INNER_RETRY_ATTEMPTS}: '
                  f'{error_code} — {e}')
        if attempt < INNER_RETRY_ATTEMPTS:
            time.sleep(INNER_RETRY_INTERVAL_SEC)

    logger.error(f'Inner burst exhausted [{from_str} -> {to_str}] — deferring to pending-recovery queue')
    return None


# ---------------------------------------------------------------------------
# §2: Thresholds, target/stop resolution — ported from backtest_p2.py,
# identical fill-price conventions (parity with the calibrated backtest).
# ---------------------------------------------------------------------------

def resolve_thresholds(entry_price: float) -> tuple:
    """Returns (lot1_distance, sl_distance) — pct mode only (Rollout step 5:
    production hardcodes 'pct')."""
    lot1_distance = entry_price * TARGET1_PCT / 100
    sl_distance   = entry_price * SL_PCT / 100 if SL_PCT is not None else None
    return lot1_distance, sl_distance


def resolve_target2(entry_price: float, direction: str) -> tuple:
    """Returns (level, source). Only 'flat'/'flat_pct' are implemented —
    'pivot' mode requires daily-pivot tracking that production doesn't
    build (calibrated TARGET2_MODE='flat_pct' beat pivot on both symbols,
    §6/configs_p2.py docstring) — fail loudly rather than silently
    misbehave if TARGET2_MODE is ever changed to 'pivot' without also
    building that infra."""
    if TARGET2_MODE == 'flat':
        raise NotImplementedError("TARGET2_MODE='flat' not used by the calibrated config; "
                                  "add TARGET2_FLAT_POINTS support if this is intentionally enabled.")
    if TARGET2_MODE == 'flat_pct':
        dist = entry_price * TARGET2_FLAT_PCT / 100
        level = entry_price + dist if direction == 'bullish' else entry_price - dist
        return level, 'flat_pct'
    raise NotImplementedError(f"TARGET2_MODE={TARGET2_MODE!r} requires daily-pivot tracking, "
                              f"not built in production (§ backtest_p2.py pivot logic not ported).")


def target_fill_price(direction: str, level: float, ltp: float) -> float:
    """Profit-taking exit off live LTP — market order fires the instant LTP
    crosses the level; fills at LTP (matches the backtest's "gap fills
    better for the trade" convention off bar-open, live equivalent is
    whatever LTP the market order actually executes at)."""
    return ltp


def stop_fill_price(direction: str, level: float, ltp: float) -> float:
    return ltp


# ---------------------------------------------------------------------------
# §2: Order fill WebSocket (ported from iris_functions.py — already
# strategy-agnostic, no MCX-specific changes needed to the class itself).
# ---------------------------------------------------------------------------

class OrderFillWatcher(SmartWebSocketOrderUpdate if _ORDER_WS_AVAILABLE else object):
    """
    Background daemon for Angel One order-update WebSocket. UNVERIFIED for
    MCX until live-tested (plan §2/Rollout step 2) — same class used
    successfully for NSE/BSE by Iris and Athena, no code changes expected
    to be needed, but this is the one piece of infrastructure this plan
    depends on that hasn't been tested for this exchange segment yet.
    """
    _TERMINAL_STATUSES = ('AB05', 'AB02', 'AB03')

    def __init__(self):
        if _ORDER_WS_AVAILABLE:
            self.wsapp                 = None
            self.last_pong_timestamp   = None
            self.current_retry_attempt = 0
            self.auth_token            = None
            self.api_key               = None
            self.client_code           = None
            self.feed_token            = None
        self._ws_ready   = threading.Event()
        self.live_orders = {}
        self._lock       = threading.Lock()

    def start(self, auth_token, api_key, client_code, feed_token):
        if not _ORDER_WS_AVAILABLE:
            logger.warning('OrderFillWatcher: SmartWebSocketOrderUpdate not available — REST fallback only.')
            return
        self.auth_token, self.api_key = auth_token, api_key
        self.client_code, self.feed_token = client_code, feed_token
        threading.Thread(target=self._run, daemon=True, name='OrderFillWatcher').start()

    def _run(self):
        try:
            self.connect()
        except Exception:
            pass

    def on_open(self, wsapp):
        pass

    def on_message(self, wsapp, message):
        self._handle(message)

    def on_data(self, wsapp, message, data_type, continue_flag):
        self._handle(message)

    def on_pong(self, wsapp, data):
        heartbeat = getattr(self, 'HEARTBEAT_MESSAGE', None)
        if heartbeat and data == heartbeat:
            self.last_pong_timestamp = time.time()
        else:
            self._handle(data)

    def on_error(self, wsapp, error):
        pass

    def on_close(self, wsapp, close_status_code, close_msg):
        self._ws_ready.clear()
        if _ORDER_WS_AVAILABLE:
            self.retry_connect()

    def _handle(self, raw):
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
        except Exception:
            return
        status = parsed.get('order-status')
        if status == 'AB00':
            self._ws_ready.set()
            logger.info('OrderFillWatcher: WS connected and ready.')
            return
        if status in self._TERMINAL_STATUSES:
            od = parsed.get('orderData')
            if od:
                oid = od.get('orderid')
                if oid:
                    with self._lock:
                        self.live_orders[oid] = od


# ---------------------------------------------------------------------------
# §1 (2026-09-04): resilient order placement — ported from Athena's
# _place_order (athena_engine.py:231-303), adapted for Prometheus's single-
# instrument-token calling convention and a per-contract, dynamically-
# sourced freeze_qty (not a hardcoded constant like the other four
# strategies' QTY_FREEZE). Does three things Prometheus's original
# single-try/except place_order didn't:
#   1. Freeze-limit quantity splitting — chunks a request bigger than the
#      broker will accept in one order into several, each placed separately.
#   2. Rejection retry — an actual 'rejected' broker response gets retried
#      up to REJECTION_RETRY_ATTEMPTS times before giving up on that chunk.
#   3. Ghost-order recovery — on DataException/NetworkException specifically
#      (a lost response, not necessarily a lost order), check the order book
#      for a matching order before assuming nothing happened and retrying
#      placement, which could otherwise produce a genuine double-fill.
# ---------------------------------------------------------------------------

_placed_order_ids = set()   # ghost-recovery collision guard: IDs this
                            # process has itself placed, so a ghost-recovery
                            # scan never re-claims an order already ours —
                            # module-level, matching _candle_counter/
                            # _ltp_counter's existing pattern in this file.


def place_order(obj, transaction_type: str, symbol: str, token: str,
                lots: int, dry_run: bool, freeze_qty: int = None) -> list:
    """
    `lots` is an already-resolved lot count — callers compute it from units
    (entry: units*2 combined; a single lot exit: units*1) since entry and
    exit orders need different quantities for the same position.

    Returns a LIST of order IDs, not one. At today's 2-4 lot sizing this
    list always has exactly one element and the freeze-limit split never
    fires — freeze_qty=10000 for CRUDEOILM (lotsize=10) allows up to 1000
    lots in a single order. Callers (get_fill_price_and_qty) must aggregate
    across the whole list regardless, not assume a single ID.
    """
    if dry_run:
        dry_id = f'PAPER_{token}_{transaction_type}_{datetime.now():%H%M%S}'
        logger.info(f'[PAPER] {transaction_type} {lots} lot(s) of {symbol} (token={token}) -> {dry_id}')
        return [dry_id]

    l_limit = max(1, (freeze_qty or lots * LOT_SIZE) // LOT_SIZE)
    order_quantities = []
    rem = lots
    while rem > 0:
        chunk = min(rem, l_limit)
        order_quantities.append(chunk)
        rem -= chunk

    orderid_list = []
    for lot_chunk in order_quantities:
        qty_shares = int(lot_chunk * LOT_SIZE)
        orderparams = {
            'variety': 'NORMAL', 'tradingsymbol': symbol, 'symboltoken': token,
            'transactiontype': transaction_type, 'exchange': FO_EXCHANGE,
            'ordertype': 'MARKET', 'producttype': 'CARRYFORWARD', 'duration': 'DAY',
            'quantity': str(qty_shares), 'price': '0', 'triggerprice': '0',
        }
        rejection_count = 0
        while True:
            try:
                resp = obj.placeOrderFullResponse(orderparams)
                if resp.get('message') == 'SUCCESS':
                    oid = resp['data']['orderid']
                    orderid_list.append(oid)
                    _placed_order_ids.add(oid)
                    logger.info(f'Order placed: {transaction_type} {qty_shares} x {symbol} -> orderid={oid}')
                    break
                rejection_count += 1
                err_msg = resp.get('message', 'Unknown error')
                logger.error(f'Order rejected ({rejection_count}/{REJECTION_RETRY_ATTEMPTS}): '
                             f'{symbol} — {err_msg}')
                if rejection_count >= REJECTION_RETRY_ATTEMPTS:
                    logger.critical(f'Order rejected {REJECTION_RETRY_ATTEMPTS}x for {symbol} '
                                    f'({err_msg}) — giving up on this chunk.')
                    break
                time.sleep(REJECTION_RETRY_COOLDOWN_SEC)
            except (DataException, NetworkException) as e:
                err_msg = str(e).lower()
                if 'access rate' in err_msg or 'exceeding' in err_msg:
                    logger.warning(f'Rate limit hit placing {symbol} order — cooling down '
                                   f'{GHOST_RECOVERY_COOLDOWN_SEC}s.')
                    time.sleep(GHOST_RECOVERY_COOLDOWN_SEC)
                    continue
                logger.warning(f'Connectivity issue ({type(e).__name__}) placing {symbol} order — '
                               f'checking the order book for a ghost order before retrying.')
                time.sleep(GHOST_RECOVERY_COOLDOWN_SEC)
                try:
                    book = obj.orderBook().get('data') or []
                    found = False
                    for o in book:
                        if (o.get('tradingsymbol') == symbol and
                                o.get('transactiontype') == transaction_type and
                                int(o.get('quantity', 0)) == qty_shares and
                                o.get('status') in ('complete', 'open', 'validation pending')):
                            oid = o.get('orderid')
                            if not oid or oid in _placed_order_ids:
                                continue
                            try:
                                ut = datetime.strptime(o['updatetime'], '%d-%b-%Y %H:%M:%S')
                                fresh = (datetime.now() - ut).total_seconds() < GHOST_RECOVERY_LOOKBACK_SEC
                            except Exception:
                                fresh = False
                            if fresh:
                                orderid_list.append(oid)
                                _placed_order_ids.add(oid)
                                logger.info(f'Ghost order recovered for {symbol}: orderid={oid}')
                                found = True
                                break
                    if found:
                        break
                    logger.info(f'No matching ghost order found for {symbol} — retrying placement.')
                except Exception as e_inner:
                    logger.error(f'orderBook check failed while recovering a ghost order for '
                                f'{symbol}: {e_inner} — retrying placement.')
            except Exception as e:
                if 'token' in str(e).lower() or 'invalid' in str(e).lower():
                    logger.critical(f'Session failure placing {symbol} order: {e} — aborting.')
                    raise
                logger.error(f'Order placement failed for {symbol}: {e}')
                time.sleep(REJECTION_RETRY_COOLDOWN_SEC)
    return orderid_list


_ltp_counter = {'count': 0, 'limit': LTP_POLL_LIMIT, 'last_reset': 0.0}


def _check_ltp_limit():
    now = time.time()
    if now - _ltp_counter['last_reset'] > 1.0:
        _ltp_counter['count'] = 0
        _ltp_counter['last_reset'] = now
    if _ltp_counter['count'] >= _ltp_counter['limit']:
        time.sleep(1.1)
        _ltp_counter['count'] = 0
        _ltp_counter['last_reset'] = time.time()
        return
    _ltp_counter['count'] += 1


def fetch_ltp_rest(obj, symbol: str, token: str) -> float:
    try:
        _check_ltp_limit()
        ltp = obj.ltpData(FO_EXCHANGE, symbol, token)['data']['ltp']
        return float(ltp) if ltp is not None else None
    except Exception as e:
        logger.error(f'REST ltpData fallback failed for {symbol}: {e}')
        return None


def get_fill_price_and_qty(obj, order_watcher: OrderFillWatcher, order_ids: list,
                           symbol: str, token: str, requested_lots: int,
                           dry_run: bool, feed) -> tuple:
    """
    Returns (avg_fill_price, filled_lots) or (None, 0) on failure (live
    only — caller should abort). filled_lots < requested_lots signals a
    partial fill — caller decides whether lot2 opens.

    order_ids is a LIST (§1, 2026-09-04) — place_order may have chunked a
    large request into several broker-legal orders (freeze_qty). Aggregates
    fill quantity/value across every ID in the list before returning a
    single blended average price, mirroring Athena's _fetch_order_details.
    At today's 2-4 lot sizing this list always has one element and the
    aggregation degenerates to the original single-order behavior.
    Acknowledged limitation, not solved here: if some chunks fill and others
    time out, this returns a hard failure (None, 0) rather than attempting a
    partial-across-multiple-orders reconciliation — consistent with the
    "never fabricate, never guess" invariant elsewhere in this file, and
    unreachable at current sizing (freeze_qty allows 1000 lots/order, orders
    here never exceed 4).

    Unlike iris_functions.get_fill_price (which subscribes/unsubscribes a
    fresh OPTION token per trade), Prometheus always trades the SAME futures
    contract token for the whole session — it's already subscribed once via
    SharedFeed's startup_tokens (prometheus.py's _setup()) and must stay
    subscribed across every entry/exit. Calling subscribe_options/
    unsubscribe_options here would tear down that permanent subscription
    (both registries map to the same real broker-side subscription) and
    silently drop LTP for the rest of the session — deliberately NOT ported.
    """
    if dry_run:
        deadline = time.time() + 3
        while time.time() < deadline:
            ltp = feed.get_ltp(token)
            if ltp:
                return ltp, requested_lots
            time.sleep(0.1)
        ltp = fetch_ltp_rest(obj, symbol, token)
        if ltp:
            logger.info(f'[PAPER] Entry price via REST ltpData: {ltp}')
            return ltp, requested_lots
        return None, 0

    if not order_ids:
        logger.error(f'get_fill_price_and_qty: empty order_ids for {symbol} — nothing to verify.')
        return None, 0

    if order_watcher._ws_ready.is_set():
        deadline = time.time() + ORDER_TIMEOUT_SEC
        while time.time() < deadline:
            with order_watcher._lock:
                orders = {oid: order_watcher.live_orders.get(str(oid)) for oid in order_ids}
            if all(orders.values()):
                total_qty, total_val = 0, 0.0
                for od in orders.values():
                    qty = int(od.get('filledshares') or 0)
                    total_qty += qty
                    total_val += float(od.get('averageprice') or 0.0) * qty
                if total_qty > 0:
                    filled_lots = total_qty // LOT_SIZE
                    avg = round(total_val / total_qty, 2)
                    logger.info(f'Fill (WS): {symbol} avg={avg} qty={total_qty} '
                               f'({filled_lots} lot(s)) across {len(order_ids)} order(s)')
                    return avg, filled_lots
            time.sleep(0.05)
        logger.warning(f'WS fill timeout for {order_ids} ({symbol}) — falling back to REST.')
    else:
        logger.info('OrderFillWatcher WS not ready — using REST orderBook.')

    deadline = time.time() + ORDER_TIMEOUT_SEC
    while time.time() < deadline:
        try:
            book = obj.orderBook()
            by_id = {str(o.get('orderid')): o for o in (book.get('data') or [])}
            total_qty, total_val = 0, 0.0
            all_resolved, any_rejected = True, False
            for oid in order_ids:
                o = by_id.get(str(oid))
                if not o:
                    all_resolved = False
                    continue
                status = o.get('status')
                if status == 'complete':
                    qty = int(o.get('filledshares', o.get('quantity', 0)))
                    total_qty += qty
                    total_val += float(o.get('averageprice', 0)) * qty
                elif status in ('rejected', 'cancelled'):
                    any_rejected = True
                else:
                    all_resolved = False
            if any_rejected and total_qty == 0:
                logger.error(f'Order(s) {order_ids} rejected/cancelled for {symbol}, nothing filled.')
                return None, 0
            if all_resolved and total_qty > 0:
                filled_lots = total_qty // LOT_SIZE
                avg = round(total_val / total_qty, 2)
                logger.info(f'Fill (REST): {symbol} avg={avg} qty={total_qty} '
                           f'({filled_lots} lot(s)) across {len(order_ids)} order(s)')
                return avg, filled_lots
        except Exception as e:
            logger.warning(f'orderBook poll failed: {e}')
        time.sleep(1)

    logger.error(f'Fill timeout for {order_ids} ({symbol})')
    return None, 0


# ---------------------------------------------------------------------------
# §4: Trade counter + cumulative tracker + per-trade running log
# (Apollo's naming convention: trade_{trade_id:04d}_{entry_dt}.csv)
# ---------------------------------------------------------------------------

def load_trade_counter() -> int:
    if COUNTER_FILE.exists():
        try:
            return int(COUNTER_FILE.read_text().strip())
        except Exception:
            pass
    return 0


def save_trade_counter(counter: int) -> None:
    try:
        COUNTER_FILE.write_text(str(counter))
    except Exception as e:
        logger.warning(f'save_trade_counter: failed to write {COUNTER_FILE}: {e}')


def trade_log_filepath(trade_id: int, entry_ts: datetime) -> Path:
    return TRADE_LOGS_DIR / f"trade_{trade_id:04d}_{entry_ts:%Y-%m-%d_%H%M}.csv"


def append_trade_log_row(trade_id: int, entry_ts: datetime, row: dict) -> None:
    """One row per polling cycle while in-trade (§4) — appended, not
    rewritten, so a crash mid-trade never loses earlier rows."""
    import os
    TRADE_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = trade_log_filepath(trade_id, entry_ts)
    write_header = not os.path.exists(path)
    pd.DataFrame([row]).to_csv(path, mode='a', header=write_header, index=False)


def append_cumulative_trade(row: dict) -> None:
    """§4: prometheus_trades.csv — one row per completed trade, append-only,
    identical columns to trade_summary_p2.csv plus `units` (direct diffability
    against the backtest, per Rollout step 3's parity check)."""
    import os
    from prometheus_configs import DATA_DIR
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not os.path.exists(TRADES_FILE)
    pd.DataFrame([row]).to_csv(TRADES_FILE, mode='a', header=write_header, index=False)


# ---------------------------------------------------------------------------
# §0: Guardian — refuse to start Prometheus if any NSE/BSE strategy is live
# (shared broker account, shared rate-limit budget).
# ---------------------------------------------------------------------------

_INACTIVE = {'idle', 'open', 'closed', 'None', None, ''}


def check_no_active_strategies() -> tuple:
    """Returns (ok, reason). ok=True means it is safe to start Prometheus."""
    checks = [
        ('Apollo',     REPO_ROOT / 'apollo_production/data/apollo_state.csv',    'status'),
        ('Athena',     REPO_ROOT / 'athena_production/data/athena_state.csv',    'status'),
        ('Artemis PE', REPO_ROOT / 'artemis_production/data/pe_trade_params.csv', 'spread_status'),
        ('Artemis CE', REPO_ROOT / 'artemis_production/data/ce_trade_params.csv', 'spread_status'),
        ('Iris',       REPO_ROOT / 'iris_production/data/iris_state.csv',        'status'),
    ]
    for name, path, col in checks:
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if df.empty:
            continue
        val = str(df[col].iloc[0]).strip()
        if val not in _INACTIVE:
            return False, f'{name} has open position (status={val!r})'
    return True, ''
