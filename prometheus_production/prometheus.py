"""
Prometheus — MCX CRUDEOILM/CRUDEOIL intraday trend-following. Phase 3
(plans/prometheus-phase3-production.md): ST_15 single-timeframe, 2-lot
scale-out, positions can now span multiple sessions (no EOD flatten) and,
once §4-§9 are built, a contract rollover. Standalone cron entry, not
Leto-routed (different exchange, different underlying, no VIX coupling).

Lifecycle:
  Start  -> resolve effective contract -> seed ST_15 (private intraday
            cache + live gap-fetch, §15) -> arm (status=watching)
  Signal -> enter 2 lots (status=in_trade)
  Exit   -> lot1 target / lot2 target / stop_loss / trend_flip
  Stop   -> graceful teardown; an open position is LEFT OPEN as the normal
            case (§2, no EOD flatten) — a restart resumes monitoring it.

DRY_RUN is ON by default (DRY_RUN=True in prometheus_configs.py).
Set DRY_RUN=False only after Rollout steps 2-4 (plan) are complete:
  1. order-update WS verified for MCX
  2. backtest/live parity check
  3. DRY_RUN paper mode under real market conditions
"""
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from prometheus_configs import (
    DRY_RUN, DATA_DIR, FLAG_PATH, COMMAND_FLAG_PATH, PID_FILE, STATE_FILE, CREDS_FILE,
    REPO_ROOT, SYMBOL, LOT_SIZE, LOTS_PER_LEG, MCX_FO_WS_EXCHANGE_TYPE,
    MIN_ENTRY_BUFFER_MIN, SESSION_START_TIME,
    SESSION_END_TIME, ST_PERIOD, ST_MULTIPLIER,
    DYNAMIC_SIZING, STATIC_UNITS, MARGIN_PER_UNIT, TRADE_UPDATE_SEC, TRADES_FILE,
    TODAY_1M_CACHE_FILE, DEFERRED_BAR_CUTOFF_MIN,
    SEED_RETRY_ATTEMPTS, SEED_RETRY_INTERVAL_SEC,
    ROLLOVER_TIME, ROLLOVER_PREFETCH_TIME, PENDING_FLIP_REALERT_DEBOUNCE_SEC,
    ENTRY_FILTER_1H_ALIGN_ENABLED, ST_1H_PERIOD, ST_1H_MULTIPLIER,
    PROVISIONAL_BOUNDARY_ENABLED, PROVISIONAL_MARGIN_PCT,
    NO_EXIT_BEFORE_BUFFER_MIN,
)
from prometheus_state import PrometheusState, save_state, load_state
from prometheus_logger_setup import get_logger
from prometheus_functions import (
    compute_st, resolve_effective_contract, seed_st15, persist_15m_series,
    fetch_one_minute_window, _merge_and_save, clear_today_cache, read_today_cache, _safe_concat,
    patch_opening_bar_if_artifact, fetch_crudeoil_opening_bar,
    resolve_thresholds, resolve_target2, next_trading_day,
    compute_st_for_contract, historical_basis_price,
    place_order, get_fill_price_and_qty, OrderFillWatcher, fetch_ltp_rest,
    load_trade_counter, save_trade_counter, append_trade_log_row, append_cumulative_trade,
    check_no_active_strategies, mcx_fully_closed_today,
)

sys.path.insert(0, str(REPO_ROOT))
from websocket_feed import SharedFeed  # noqa: E402
try:
    from leto_config import (SLACK_TRADEBOT_CHANNEL, SLACK_TRADE_ALERTS,
                              SLACK_TRADE_UPDATES, SLACK_ERRORS_CHANNEL)
except ImportError:
    SLACK_TRADEBOT_CHANNEL = SLACK_TRADE_ALERTS = SLACK_TRADE_UPDATES = SLACK_ERRORS_CHANNEL = None

logger = get_logger('prometheus')


def _load_slack_token() -> str:
    try:
        creds = pd.read_csv(REPO_ROOT / 'data/user_credentials.csv')
        return str(creds.iloc[0]['slack_token'])
    except Exception:
        return ''


_SLACK_TOKEN = _load_slack_token()


def _tag(symbol_root: str) -> str:
    return f'*Prometheus [{symbol_root}]*'


def _slack(msg: str, channel=None) -> None:
    if channel is None:
        channel = SLACK_TRADE_ALERTS
    if not channel or not _SLACK_TOKEN:
        return
    try:
        from slack_sdk import WebClient
        import threading
        def _send():
            try:
                WebClient(token=_SLACK_TOKEN).chat_postMessage(channel=channel, text=msg)
            except Exception:
                pass
        threading.Thread(target=_send, daemon=True).start()
    except ImportError:
        pass


class Prometheus:
    def __init__(self, obj, auth_token: str, api_key: str, client_code: str):
        self.obj = obj
        self.auth_token = auth_token
        self._api_key = api_key
        self._client_code = client_code

        self.state = load_state()
        self.feed = None
        self.order_watcher = OrderFillWatcher()
        self._shutdown = False
        # Set by the KILL command handler so _teardown() knows NOT to
        # auto-exit an open position -- KILL's entire premise ("Control
        # dropped. Position remains OPEN.", slack_listener.py's own message)
        # contradicts _teardown()'s default behavior of auto-flattening
        # anything still open, since teardown otherwise has no way to tell
        # a deliberate hand-off apart from a normal end-of-session flatten.
        self._kill_no_exit = False

        self._contract = None      # resolved once at setup — §1's single authoritative source
        self._df_15m = None        # full seeded + accumulated 15-min ST series (in-memory)
        self._df_1m_today = pd.DataFrame(columns=['time_stamp', 'open', 'high', 'low', 'close', 'volume'])

        self._pending_recovery = []   # [(from_dt, to_dt)] — §3 outer non-blocking retry queue
        self._pending_15m_boundary = None   # §12: 15m boundary awaiting a complete 1-min window
        self._pending_15m_deadline = None   # §12: cutoff before building it from what's on hand
        self._opening_bar_checked = False   # §11: run patch_opening_bar_if_artifact once per session
        self._trade_counter = load_trade_counter()
        self._pending_trade_row = {}   # built in _execute_entry; reconstructed in _setup() on crash resume

        self._trade_count = 0
        self._total_pnl_rs = 0.0

        # §4-§9 (2026-09-04): rollover state — all in-memory only, same
        # precedent as _pending_recovery/_pending_15m_boundary above. A
        # crash mid-rollover loses this and falls back to §5's missed-
        # rollover recovery the next morning, never to an unsafe state.
        self._rollover_new_contract = None   # set once §4's evening lookahead confirms a roll tonight
        self._rollover_basis = None          # §6 step 1: precomputed {basis_price, sl_price, lot1_target, lot2_target, lot2_target_source, lot2_only}
        self._rollover_prefetch_done = False # §6 step 2 latch
        self._df_1m_today_new = pd.DataFrame(columns=['time_stamp', 'open', 'high', 'low', 'close', 'volume'])
        self._rollover_executed_today = False  # latch so §6 steps 4-7 only ever fire once per evening
        self._rollover_new_ws_subscribed = False   # §6 step 2 latch, separate from the historical-fetch latch
        self._rollover_go_decision = None    # §6 step 4: decided ONCE, cached — retries only retry
                                              # execution (exit confirm / reopen), never re-litigate go/no-go

        self._pending_flip = None   # §7: Rule 7's combined-order retry-until-resolved marker

        # Provisional boundary computation (2026-09-04, user-directed) —
        # all in-memory only, same precedent as _pending_recovery/
        # _pending_15m_boundary: a crash loses this and falls back to a
        # safe default (no provisional bookkeeping, real series untouched).
        self._tick_ohlc_accum = {'open': None, 'high': None, 'low': None, 'close': None}
        self._provisional_pending = None   # set only while a provisional action awaits
                                            # reconciliation against the real bar for the SAME boundary
        self._provisional_disabled_this_session = False   # latched True on any real disagreement —
                                                            # a restart re-arms it

        self._summary = {'strategy': 'Prometheus', 'symbol': SYMBOL, 'traded': False}

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info('Shutdown signal received.')
        self._shutdown = True

    # -----------------------------------------------------------------------
    # Sizing (§6) — Artemis's formula, not Apollo/Athena's dual-constraint one
    # -----------------------------------------------------------------------

    def _calculate_units(self) -> int:
        if not DYNAMIC_SIZING:
            logger.debug(f'Sizing: fixed STATIC_UNITS={STATIC_UNITS}')
            return STATIC_UNITS
        try:
            margin = float(self.obj.rmsLimit()['data']['availablecash'])
            units = max(1, int(margin // MARGIN_PER_UNIT))
            logger.info(f'Sizing: Available margin={margin:,.0f}  MARGIN_PER_UNIT={MARGIN_PER_UNIT:,}  '
                        f'Units={units}')
            return units
        except Exception as e:
            logger.warning(f'rmsLimit() failed ({e}) — falling back to STATIC_UNITS={STATIC_UNITS}')
            return STATIC_UNITS

    def _check_margin_sufficient(self, units: int) -> bool:
        """§6: general pre-entry margin check regardless of sizing mode — the
        primary defense against the tender-margin window is the early roll
        (§1), this is defense-in-depth against any other margin shift."""
        try:
            margin = float(self.obj.rmsLimit()['data']['availablecash'])
            required = units * MARGIN_PER_UNIT
            if margin < required:
                logger.error(f'Insufficient margin: available={margin:,.0f} required={required:,.0f} '
                            f'for {units} unit(s) — skipping entry.')
                _slack(f'⚠️ {_tag(self._contract["symbol_root"])}: insufficient margin '
                      f'(available Rs.{margin:,.0f}, need Rs.{required:,.0f}) — entry skipped.',
                      SLACK_ERRORS_CHANNEL)
                return False
            return True
        except Exception as e:
            logger.warning(f'Margin check failed ({e}) — proceeding without it.')
            return True

    # -----------------------------------------------------------------------
    # Circuit breaker (§5) — Prometheus's own dedicated flag, not the shared
    # SLACK_COMMAND.flag (an operator on NSE/BSE shouldn't accidentally kill MCX).
    # -----------------------------------------------------------------------

    def _check_command_flag(self) -> None:
        if not COMMAND_FLAG_PATH.exists():
            return
        try:
            command = COMMAND_FLAG_PATH.read_text().strip()
            tag = _tag(self._contract['symbol_root']) if self._contract else '*Prometheus*'

            if command == 'EXIT':
                msg = f'⚠️ {tag}: Slack `Exit Trade` detected. Liquidating...'
                logger.critical(msg.replace('*', ''))
                _slack(msg, SLACK_TRADE_ALERTS)
                if self._pending_flip is not None:
                    # §7: a Rule 7 flip mid-transition (old side already
                    # closed, new side not yet opened) would otherwise be
                    # silently abandoned once the loop exits below -- the
                    # user asked to stop, not to finish opening a new
                    # position, so drop it explicitly rather than never.
                    logger.critical('Rule 7 pending flip abandoned due to !exit command '
                                    '(re-entry not completed, by design).')
                    self._pending_flip = None
                if self.state.status == 'in_trade':
                    self._execute_exit_all('slack_exit')
                raise RuntimeError('Session terminated by Slack !exit command.')

            elif command == 'KILL':
                msg = f'\U0001f6a8 {tag}: Slack `Kill Switch` detected. Dropping control immediately.'
                logger.critical(msg.replace('*', ''))
                _slack(msg, SLACK_TRADE_ALERTS)
                self._kill_no_exit = True   # tell _teardown() not to auto-exit an open position
                raise RuntimeError('Session terminated by Slack !kill command.')

            # DISABLE: startup gate only, handled in main() before instantiation.

        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f'Error reading command flag: {e}')

    # -----------------------------------------------------------------------
    # Contract rollover (§4-§9, 2026-09-04)
    # -----------------------------------------------------------------------

    def _check_rollover_tonight(self) -> None:
        """§4: checked once, at setup — well before ROLLOVER_TIME, so §6
        step 1's basis precompute has plenty of lead time. Checks whether
        tomorrow's *trading* day (not a naive today+1) would resolve to a
        different contract than today's self._contract; if so, a roll is
        needed. §18 (2026-09-05): the two cases now diverge sharply —

        - Flat (no position to protect): switch to the new contract RIGHT
          NOW instead of waiting for the scheduled evening mechanism. There
          is nothing §6's dual-poll/veto machinery is protecting here, and
          waiting until ROLLOVER_TIME just means any fresh entry today would
          open on the OLD contract only to be immediately rolled the same
          evening — pure churn. See _switch_to_new_contract_now.
        - In-trade: unchanged for now (§6's evening-triggered sequence,
          driven by _check_rollover_timing every 1-min cycle) — §18's
          same-day coincident-flip transition for this case is Phase 2/3,
          not yet built.
        """
        tomorrow = next_trading_day(datetime.now().date())
        tomorrow_contract = resolve_effective_contract(SYMBOL, today=tomorrow)
        if tomorrow_contract['token'] == self._contract['token']:
            return

        if self.state.status != 'in_trade':
            self._switch_to_new_contract_now(tomorrow_contract)
            return

        self._rollover_new_contract = tomorrow_contract
        tag = _tag(self._contract['symbol_root'])
        logger.info(f"Rollover tonight: {self._contract['symbol']} -> "
                   f"{tomorrow_contract['symbol']} at {ROLLOVER_TIME}.")
        _slack(f'\U0001f514 {tag}: rolling to {tomorrow_contract["symbol"]} tonight at '
              f'{ROLLOVER_TIME}.', SLACK_TRADEBOT_CHANNEL)
        self._precompute_rollover_basis()

    def _switch_to_new_contract_now(self, new_contract: dict) -> None:
        """§18 flat-at-open case: nothing open to protect, so switch
        immediately rather than waiting for §6's scheduled evening
        mechanism. Deliberately does NOT reuse seed_st15/read_today_cache —
        those are coupled to a single shared "today" cache file implicitly
        tied to whichever contract has been trading all day (self._contract);
        blindly re-reading that cache for a DIFFERENT contract mid-day would
        silently mix the old contract's price rows into what's supposed to
        be the new contract's own series (a real, different price level —
        confirmed ~150-250pt CRUDEOILM calendar spread, not a rounding
        difference). Instead, fetches the new contract's own "today" 1-min
        window directly (same live-fetch primitive §6 step 2's
        _do_rollover_prefetch already uses) and computes ST via
        compute_st_for_contract — built for exactly this "ST for a contract
        that isn't self._contract" shape, already exercised by §6/§8's
        veto and §17's 1h filter.

        On any failure, stays on the OLD contract for the rest of today —
        safe by construction: tomorrow's fresh _setup() calls
        resolve_effective_contract() with no override, which independently
        re-derives the correct contract for that day regardless of whether
        today's early switch succeeded.
        """
        old_contract = self._contract
        tag = _tag(new_contract['symbol_root'])
        now = datetime.now()
        session_start = pd.Timestamp(f'{now.date()} {SESSION_START_TIME}')

        new_today_1m = fetch_one_minute_window(self.obj, new_contract['token'], session_start, now)
        if new_today_1m is None:
            new_today_1m = pd.DataFrame(columns=['time_stamp', 'open', 'high', 'low', 'close', 'volume'])
        else:
            new_today_1m = new_today_1m.copy()
            if new_today_1m['time_stamp'].dt.tz is not None:
                new_today_1m['time_stamp'] = new_today_1m['time_stamp'].dt.tz_localize(None)

        df_15m = compute_st_for_contract(new_contract, new_today_1m, now)
        if df_15m.empty:
            logger.error(f"§18 flat-switch: could not build {new_contract['symbol']}'s ST_15 "
                        f"(gap or fetch failure) — staying on {old_contract['symbol']} for today; "
                        f"tomorrow's fresh setup will resolve the contract correctly regardless.")
            _slack(f'⚠️ {tag}: could not switch to {new_contract["symbol"]} today (ST build failed) — '
                  f'staying on {old_contract["symbol"]} for the rest of today.', SLACK_ERRORS_CHANNEL)
            return

        self.feed.subscribe_options([new_contract['token']], exchange_type=MCX_FO_WS_EXCHANGE_TYPE)
        self.feed.unsubscribe_options([old_contract['token']], exchange_type=MCX_FO_WS_EXCHANGE_TYPE)

        self._contract = new_contract
        self._df_15m = df_15m
        self._df_1m_today = new_today_1m
        self._rollover_executed_today = True   # nothing left for §6's evening path to do today

        last = self._df_15m.iloc[-1]
        trend_str = ('bullish' if bool(last['trend']) else 'bearish') if not pd.isna(last['trend']) else 'warmup'
        logger.info(f"§18: flat -- switched {old_contract['symbol']} -> {new_contract['symbol']} "
                   f"now, not waiting for {ROLLOVER_TIME} ({len(self._df_15m)} bars, trend={trend_str}).")
        _slack(f'{"[PAPER] " if DRY_RUN else ""}\U0001f504 {tag}: switched to {new_contract["symbol"]} '
              f'at open (flat, no position to protect). ST_15 built ({len(self._df_15m)} bars, '
              f'trend={trend_str}).', SLACK_TRADEBOT_CHANNEL)

    def _precompute_rollover_basis(self) -> None:
        """§6 step 1 / §8 points 1-2: as soon as tonight's roll is
        confirmed, precompute the recalibrated basis price — pure
        historical lookup + arithmetic against already-accumulated data,
        no live fetch needed, no reason to wait until near ROLLOVER_TIME.
        No-op if nothing is open (a flat rollover has nothing to
        recalibrate, no go/no-go stakes)."""
        if self.state.status != 'in_trade':
            return
        nc = self._rollover_new_contract
        entry_ts = datetime.fromisoformat(self.state.entry_ts)
        basis_price = historical_basis_price(nc, entry_ts)
        if basis_price is None:
            logger.error(f"Rollover basis precompute failed for {nc['symbol']} — historical lookup "
                        f"came back empty. The ST-disagreement veto at {ROLLOVER_TIME} still runs; "
                        f"if it says go, the reopen is skipped anyway since no basis is available "
                        f"(same outcome as a no-go — flatten only).")
            self._rollover_basis = None
            return

        lot1_open = self.state.lot1_status == 'open'
        lot2_open = self.state.lot2_status == 'open'
        lots_to_reopen = ((self.state.lot1_lots or 0) if lot1_open else 0) + \
                          ((self.state.lot2_lots or 0) if lot2_open else 0)
        self._rollover_basis = {
            'direction': self.state.direction, 'basis_price': basis_price,
            'signal_ts': self.state.signal_ts, 'signal_close': self.state.signal_close,
            'units': self.state.units, 'lots_to_reopen': lots_to_reopen,
            'lot2_only': lot2_open and not lot1_open,
        }
        logger.info(f"Rollover basis precomputed: {nc['symbol']} basis={basis_price:.2f} "
                   f"(from {self.state.symbol}'s entry at {entry_ts}), "
                   f"lots_to_reopen={lots_to_reopen}, lot2_only={self._rollover_basis['lot2_only']}.")

    def _rollover_entry_suppressed(self, now: datetime) -> bool:
        """§4's entry-ordering rule: once a rollover is confirmed for
        tonight, suppress fresh/Rule-7 entries starting at ROLLOVER_TIME —
        otherwise a fresh position could open seconds before being
        flattened straight into the roll."""
        if self._rollover_new_contract is None:
            return False
        return now >= pd.Timestamp(f'{now.date()} {ROLLOVER_TIME}')

    def _check_1h_alignment(self, direction: str, contract: dict = None,
                            today_1m: pd.DataFrame = None) -> bool:
        """
        §17 (2026-09-04): only take a 15m flip if the 1-hour Supertrend
        already agrees with its direction. Gated off by
        ENTRY_FILTER_1H_ALIGN_ENABLED (default False, values unset until
        Phase 4's own backtest calibrates them) — when off, always returns
        True, matching every entry path's behavior before this existed.
        Applied uniformly at every entry/reopen path — DECIDED, no per-site
        exceptions: fresh entry, Rule 7 re-entry, and BOTH rollover reopen
        triggers (§6's evening `_execute_rollover_decision` and §5's
        `_recover_missed_rollover` — the latter was a real gap, missed on
        the initial 2026-09-04 wiring pass and fixed the same day once
        found).

        Defaults to self._contract/self._df_1m_today (fresh entry and Rule 7
        both trade the currently-effective contract; missed-rollover
        recovery too, since by that point in _setup() self._contract has
        already swapped to the new contract). §6's evening rollover reopen
        is the one exception that passes the NEW contract explicitly, since
        self._contract hasn't swapped to it yet at the point its own veto
        runs (`_execute_rollover_decision`).
        """
        if not ENTRY_FILTER_1H_ALIGN_ENABLED:
            return True
        contract = contract if contract is not None else self._contract
        today_1m = today_1m if today_1m is not None else self._df_1m_today
        st_1h = compute_st_for_contract(contract, today_1m, datetime.now(),
                                        minutes=60, st_period=ST_1H_PERIOD,
                                        st_multiplier=ST_1H_MULTIPLIER)
        if st_1h.empty or pd.isna(st_1h.iloc[-1]['trend']):
            logger.warning('1h-alignment filter: ST_1H could not be computed — '
                           'treating as disagree (no entry).')
            return False
        st_1h_direction = 'bullish' if bool(st_1h.iloc[-1]['trend']) else 'bearish'
        agree = st_1h_direction == direction
        if not agree:
            logger.info(f"1h-alignment filter: {direction} blocked — 1h ST is "
                       f"{st_1h_direction} on {contract['symbol']}.")
        return agree

    # -----------------------------------------------------------------------
    # Provisional boundary computation (2026-09-04, user-directed) — see
    # prometheus_configs.py's PROVISIONAL_BOUNDARY_ENABLED docstring for the
    # full design rationale.
    # -----------------------------------------------------------------------

    def _harvest_tick_ohlc(self) -> None:
        """
        Drains SharedFeed's own tick-aggregated OHLC window for the current
        contract (feed.get_ohlc — genuine WS ticks, resets on each call;
        Apollo already reads this same mechanism for Nifty/VIX) every 1-min
        cycle and merges it into an accumulator covering the CURRENT
        in-progress 15m window. Reset in run() right after that window's
        real bar is finalized. Entirely independent of the REST poller — an
        AB1021 stretch on the candle endpoint never touches the WS feed, so
        this keeps accumulating right through one.
        """
        if self.feed is None:
            return
        chunk = self.feed.get_ohlc(self._contract['token'])
        if chunk is None:
            return
        acc = self._tick_ohlc_accum
        acc['open'] = chunk['open'] if acc['open'] is None else acc['open']
        acc['high'] = chunk['high'] if acc['high'] is None else max(acc['high'], chunk['high'])
        acc['low'] = chunk['low'] if acc['low'] is None else min(acc['low'], chunk['low'])
        acc['close'] = chunk['close']

    def _evaluate_provisional_boundary(self, boundary: datetime, window_start: datetime) -> None:
        """
        Runs exactly ONCE per 15m-aligned boundary, at the instant the
        boundary tick arrives (T+0), and only when the caller has already
        confirmed the REST 1-min window is incomplete right then. Builds a
        provisional candle from the tick-OHLC accumulator, computes a
        provisional ST verdict against a COPY of self._df_15m (never
        mutates or persists the real series), and:
          - ALWAYS shadow-logs the verdict, toggle or margin regardless —
            this is how PROVISIONAL_MARGIN_PCT eventually gets calibrated
            on real agreement data instead of a guess.
          - Only ACTS (mirroring _handle_new_15m_bar's own real branching)
            if PROVISIONAL_BOUNDARY_ENABLED and the provisional close
            clears the band by PROVISIONAL_MARGIN_PCT. Otherwise this is a
            no-op on real state — the existing §12 wait/cutoff path is
            completely unaffected either way.
        Suppressed entirely during a scheduled/in-progress rollover or an
        already-stuck Rule 7 transition — deliberately not layered on top
        of those already-complex sequences.
        """
        tag = _tag(self._contract['symbol_root'])
        acc = self._tick_ohlc_accum
        if acc['open'] is None or acc['close'] is None:
            logger.debug('Provisional boundary check: no tick OHLC accumulated this window — skipping.')
            return
        if self._rollover_new_contract is not None or self._pending_flip is not None:
            logger.info('Provisional boundary check: skipped (rollover pending or Rule 7 mid-transition).')
            return

        provisional_bar = pd.DataFrame([{
            'time_stamp': window_start, 'open': acc['open'], 'high': acc['high'],
            'low': acc['low'], 'close': acc['close'], 'volume': 0,
        }])
        combined = _safe_concat([self._df_15m, provisional_bar], ignore_index=True)
        combined = combined.drop_duplicates(subset=['time_stamp'], keep='last').sort_values('time_stamp')
        provisional_series = compute_st(combined.reset_index(drop=True), ST_PERIOD, ST_MULTIPLIER)
        row = provisional_series[provisional_series['time_stamp'] == window_start]
        if row.empty or pd.isna(row.iloc[-1]['trend']):
            logger.warning('Provisional boundary check: provisional ST could not be computed — skipping.')
            return
        bar = row.iloc[-1]
        direction_prov = 'bullish' if bool(bar['trend']) else 'bearish'
        flip_prov = bool(bar['trend_flip'])
        band_distance_pct = (abs(bar['close'] - bar['supertrend']) / bar['close'] * 100
                             if bar['close'] else 0.0)
        clears_margin = band_distance_pct > PROVISIONAL_MARGIN_PCT

        logger.info(f'Provisional boundary {window_start:%H:%M}: close={bar["close"]:.2f} '
                   f'ST={bar["supertrend"]:.2f} direction={direction_prov} flip={flip_prov} '
                   f'band_dist_pct={band_distance_pct:.3f} (margin={PROVISIONAL_MARGIN_PCT}) '
                   f'clears_margin={clears_margin} '
                   f'gating={"ON" if PROVISIONAL_BOUNDARY_ENABLED else "OFF"} — SHADOW LOG, '
                   f'reconciled against the real bar once it computes.')

        if not PROVISIONAL_BOUNDARY_ENABLED or not clears_margin:
            return
        if self._provisional_disabled_this_session:
            logger.warning('Provisional boundary check: disabled for the rest of this session '
                           '(a prior disagreement fired) — skipping action.')
            return
        if not flip_prov:
            return   # nothing to act on -- no provisional flip at this boundary

        pre_status, pre_direction = self.state.status, self.state.direction
        acted = False

        if self.state.status == 'in_trade':
            if direction_prov != self.state.direction:
                _slack(f'⚠️ {tag}: PROVISIONAL flip -> {direction_prov} at '
                      f'{window_start:%H:%M} (REST data incomplete, acting on a '
                      f'tick-reconstructed candle; band_dist={band_distance_pct:.3f}%). '
                      f'Will reconcile against the real bar once REST recovers.',
                      SLACK_TRADEBOT_CHANNEL)
                self._execute_rule7_flip(direction_prov, window_start, bar['close'])
                acted = True
        elif self.state.status == 'watching':
            if (self._past_min_entry_guard(datetime.now()) and not self._rollover_entry_suppressed(datetime.now())
                    and self._check_1h_alignment(direction_prov)):
                _slack(f'⚠️ {tag}: PROVISIONAL entry {direction_prov.upper()} at '
                      f'{window_start:%H:%M} (REST data incomplete, acting on a '
                      f'tick-reconstructed candle; band_dist={band_distance_pct:.3f}%). '
                      f'Will reconcile against the real bar once REST recovers.',
                      SLACK_TRADEBOT_CHANNEL)
                self._execute_entry(direction_prov, window_start, bar['close'])
                acted = True

        if not acted:
            return

        self._provisional_pending = {
            'boundary': boundary, 'window_start': window_start,
            'provisional_direction': direction_prov,
            'pre_action_status': pre_status, 'pre_action_direction': pre_direction,
        }
        logger.warning(f'Provisional action taken for boundary {window_start:%H:%M} -> '
                       f'{direction_prov}. Awaiting reconciliation against the real bar.')

    def _reconcile_provisional(self, real_direction: str, real_bar) -> None:
        """
        Called from _handle_new_15m_bar once the REAL bar for a boundary
        that already had a provisional action taken finally computes.
        Compares the real, state-independent verdict (what does an
        accurate ST_15 say the trend is for this closed bar) against the
        provisional one. Agreement clears silently. Disagreement is
        CRITICAL + Slack + provisional gating disabled for the rest of
        this session (a restart re-arms it) — deliberately NO automated
        reversal (see PROVISIONAL_BOUNDARY_ENABLED's docstring for why:
        the margin guard is meant to make this dead code, and if it fires,
        that means the margin is mis-sized, not something to paper over
        with another automated order).
        """
        pending = self._provisional_pending
        tag = _tag(self._contract['symbol_root'])
        agree = real_direction == pending['provisional_direction']

        if agree:
            logger.info(f"Provisional reconciliation AGREES: real bar confirms {real_direction} "
                       f"for {pending['window_start']:%H:%M} (ST={real_bar['supertrend']:.2f} "
                       f"close={real_bar['close']:.2f}).")
            _slack(f'✅ {tag}: provisional {pending["provisional_direction"]} flip at '
                  f'{pending["window_start"]:%H:%M} CONFIRMED by the real bar.', SLACK_TRADEBOT_CHANNEL)
        else:
            logger.critical(f"Provisional reconciliation DISAGREES: acted on "
                           f"{pending['provisional_direction']} but the real bar says "
                           f"{real_direction} for {pending['window_start']:%H:%M} "
                           f"(ST={real_bar['supertrend']:.2f} close={real_bar['close']:.2f}). "
                           f"No automated reversal — provisional gating disabled for the rest of "
                           f"this session. Manual review required.")
            _slack(f'\U0001f6a8 {tag}: provisional {pending["provisional_direction"]} flip at '
                  f'{pending["window_start"]:%H:%M} DISAGREES with the real bar ({real_direction}). '
                  f'Current position may be WRONG — REVIEW MANUALLY. Provisional gating disabled '
                  f'for the rest of this session.', SLACK_ERRORS_CHANNEL)
            self._provisional_disabled_this_session = True
            if self._pending_flip is not None:
                logger.critical('Abandoning still-pending Rule 7 flip due to provisional '
                               'disagreement — broker-side position may need manual reconciliation.')
                _slack(f'\U0001f6a8 {tag}: abandoning an in-progress Rule 7 order due to the '
                      f'provisional disagreement above — CHECK THE BROKER TERMINAL MANUALLY NOW.',
                      SLACK_ERRORS_CHANNEL)
                self._pending_flip = None
        self._provisional_pending = None

    def _check_rollover_timing(self, now: datetime) -> None:
        """§6 steps 2-4, called every 1-min cycle from the main loop
        (same cadence as _recover_pending_windows). No-op once today's
        rollover (if any) has already executed."""
        if self._rollover_new_contract is None or self._rollover_executed_today:
            return
        nc = self._rollover_new_contract
        prefetch_time = pd.Timestamp(f'{now.date()} {ROLLOVER_PREFETCH_TIME}')
        rollover_time = pd.Timestamp(f'{now.date()} {ROLLOVER_TIME}')

        if not self._rollover_prefetch_done and now >= prefetch_time:
            self._do_rollover_prefetch(nc, now)
        elif self._rollover_prefetch_done and now < rollover_time:
            self._do_rollover_topup_poll(nc, now)

        if now >= rollover_time:
            self._execute_rollover_decision(now)

    def _do_rollover_prefetch(self, nc: dict, now: datetime) -> None:
        """§6 step 2: bulk-fetch the new contract's *today* series and
        subscribe its WS feed — reuses fetch_one_minute_window (the same
        resilient poller every other 1-min fetch in this file uses), just
        pointed at the new contract's token. The WS subscribe runs
        regardless of whether the historical fetch succeeds, so the feed
        is warm even if the history side is still retrying."""
        session_start = pd.Timestamp(f'{now.date()} {SESSION_START_TIME}')
        df = fetch_one_minute_window(self.obj, nc['token'], session_start, now)
        if df is not None and not df.empty:
            working = df.copy()
            if working['time_stamp'].dt.tz is not None:
                working['time_stamp'] = working['time_stamp'].dt.tz_localize(None)
            self._df_1m_today_new = working.sort_values('time_stamp').reset_index(drop=True)
            self._rollover_prefetch_done = True
            logger.info(f"Rollover prefetch: {len(self._df_1m_today_new)} 1-min row(s) for "
                       f"{nc['symbol']}.")
        else:
            logger.warning(f"Rollover prefetch failed for {nc['symbol']} — will retry next cycle.")

        if not self._rollover_new_ws_subscribed:
            self.feed.subscribe_options([nc['token']], exchange_type=MCX_FO_WS_EXCHANGE_TYPE)
            self._rollover_new_ws_subscribed = True
            logger.info(f"Rollover: subscribed {nc['symbol']}'s WS feed ahead of {ROLLOVER_TIME}.")

    def _do_rollover_topup_poll(self, nc: dict, now: datetime) -> None:
        """§6 step 3: per-minute top-up for the new contract, same 5-min
        lookback shape as the old contract's own poll in run() — catches
        the prefetched series up to the moment ROLLOVER_TIME arrives."""
        win_to = now
        win_from = win_to - timedelta(minutes=5)
        df = fetch_one_minute_window(self.obj, nc['token'], win_from, win_to)
        if df is None:
            logger.warning(f"Rollover top-up poll failed for {nc['symbol']} — will retry next cycle.")
            return
        working = df.copy()
        if working['time_stamp'].dt.tz is not None:
            working['time_stamp'] = working['time_stamp'].dt.tz_localize(None)
        today = now.date()
        new_today = working[working['time_stamp'].dt.date == today]
        if new_today.empty:
            return
        combined = _safe_concat([self._df_1m_today_new, new_today], ignore_index=True)
        combined = combined.drop_duplicates(subset=['time_stamp'], keep='last')
        self._df_1m_today_new = combined.sort_values('time_stamp').reset_index(drop=True)

    def _execute_rollover_decision(self, now: datetime) -> None:
        """§6 steps 4-7. Safely re-callable every tick past ROLLOVER_TIME
        until fully resolved — an unconfirmed exit leaves state as
        in_trade and this returns early, retried on the next call exactly
        like any other unconfirmed exit in this file."""
        nc = self._rollover_new_contract
        tag = _tag(self._contract['symbol_root'])

        if self._rollover_go_decision is None:
            if self.state.status != 'in_trade':
                self._rollover_go_decision = True   # flat rollover -- pure housekeeping, nothing to veto
                self._rollover_basis = None
            else:
                # Refresh the basis precompute right before use, not just
                # once at _setup() time (§6 step 1's early call) — the
                # position open right NOW might not be the one that was
                # open then, if it closed/reopened/flipped during the day.
                # Cheap (pure historical lookup, no broker call), safe to
                # redo, and this is the only way to guarantee it never goes
                # stale.
                self._precompute_rollover_basis()
                new_st = compute_st_for_contract(nc, self._df_1m_today_new, now)
                if new_st.empty or pd.isna(new_st.iloc[-1]['trend']):
                    logger.error(f"Rollover veto: {nc['symbol']}'s ST could not be computed "
                                f"(incomplete data at {ROLLOVER_TIME}) — DECIDED default: no-go.")
                    _slack(f'⚠️ {tag}: rollover veto — {nc["symbol"]}\'s ST unavailable at '
                          f'{ROLLOVER_TIME}, defaulting to no-go (flatten only).', SLACK_ERRORS_CHANNEL)
                    self._rollover_go_decision = False
                else:
                    new_direction = 'bullish' if bool(new_st.iloc[-1]['trend']) else 'bearish'
                    st_agree = new_direction == self.state.direction
                    # §17: gated the same way as every other entry path, on
                    # top of §8's own 15m ST-disagreement veto above — either
                    # one failing lands on the same flatten-and-wait outcome.
                    align_agree = self._check_1h_alignment(new_direction, contract=nc,
                                                            today_1m=self._df_1m_today_new)
                    agree = st_agree and align_agree
                    self._rollover_go_decision = agree
                    logger.info(f'Rollover veto check: old={self.state.direction} new={new_direction} '
                               f'st_agree={st_agree} align_agree={align_agree} '
                               f'-> {"GO" if agree else "NO-GO"}.')
                    if not st_agree:
                        _slack(f'\U0001f504 {tag}: rollover veto — {nc["symbol"]}\'s ST '
                              f'({new_direction}) disagrees with the carried position '
                              f'({self.state.direction}) — flatten only, no reopen.', SLACK_TRADEBOT_CHANNEL)
                    elif not align_agree:
                        _slack(f'\U0001f504 {tag}: rollover veto — {nc["symbol"]}\'s 1h ST '
                              f'disagrees with the {new_direction} reopen — flatten only, '
                              f'no reopen.', SLACK_TRADEBOT_CHANNEL)

        # Step 5: flatten the old position unconditionally, regardless of go/no-go
        if self.state.status == 'in_trade':
            closed = self._execute_exit_all('rollover')
            if not closed:
                logger.critical('Rollover exit did not confirm closed — will retry next tick.')
                return
            self.feed.unsubscribe_options([self._contract['token']], exchange_type=MCX_FO_WS_EXCHANGE_TYPE)

        # Old position (if any) is now confirmed flat -- safe to swap contracts
        # before attempting a reopen, which needs to target the NEW contract.
        self._contract = nc

        # Step 6: reopen only if go
        if self._rollover_go_decision and self._rollover_basis is not None:
            self._execute_rollover_reopen(nc)

        # Step 7: clear rollover state, persist
        self._rollover_new_contract = None
        self._rollover_basis = None
        self._rollover_prefetch_done = False
        self._rollover_new_ws_subscribed = False
        self._rollover_go_decision = None
        self._df_1m_today = self._df_1m_today_new
        self._df_1m_today_new = pd.DataFrame(columns=['time_stamp', 'open', 'high', 'low', 'close', 'volume'])
        self._rollover_executed_today = True
        save_state(self.state)
        logger.info(f"Rollover complete: now trading {nc['symbol']}.")
        _slack(f'{"[PAPER] " if DRY_RUN else ""}✅ {tag}: rolled to {nc["symbol"]}.', SLACK_TRADEBOT_CHANNEL)

    def _execute_rollover_reopen(self, new_contract: dict) -> None:
        """§6 step 6 / §8: reopen on the new contract using the
        precomputed basis price, sized to however many lots actually
        survived to the roll — not blindly both (§8 point 1)."""
        basis = self._rollover_basis
        old_trade_id = self._trade_counter   # the just-closed old leg's trade_id (§9 parent_trade_id)
        lots_to_reopen = basis['lots_to_reopen']
        tag = _tag(new_contract['symbol_root'])

        order_ids = place_order(self.obj, 'BUY' if basis['direction'] == 'bullish' else 'SELL',
                                new_contract['symbol'], new_contract['token'], lots_to_reopen,
                                DRY_RUN, new_contract['freeze_qty'])
        if not order_ids:
            logger.critical('Rollover reopen order FAILED to place — no position opened on the new contract.')
            _slack(f'\U0001f6a8 {tag}: rollover reopen order FAILED to place — no position opened '
                  f'on {new_contract["symbol"]}. Check manually.', SLACK_ERRORS_CHANNEL)
            return

        fill_price, filled_lots = get_fill_price_and_qty(
            self.obj, self.order_watcher, order_ids, new_contract['symbol'], new_contract['token'],
            lots_to_reopen, DRY_RUN, self.feed)
        if fill_price is None or filled_lots == 0:
            logger.critical('Rollover reopen order placed but fill unconfirmed — no position opened.')
            _slack(f'\U0001f6a8 {tag}: rollover reopen fill unconfirmed on {new_contract["symbol"]}. '
                  f'Check manually.', SLACK_ERRORS_CHANNEL)
            return

        self._finalize_new_position(
            basis['direction'], basis['signal_ts'], basis['signal_close'],
            fill_price, filled_lots, lots_to_reopen, basis['units'],
            basis_price=basis['basis_price'], parent_trade_id=old_trade_id,
            lot2_only=basis['lot2_only'])

    def _recover_missed_rollover(self) -> None:
        """§5 (DECIDED option a): if a rollover was missed overnight (the
        process wasn't alive at ROLLOVER_TIME, an MCX holiday, KILL,
        DISABLE), roll immediately at today's open instead of walking into
        §3's problem with an open position on a contract
        resolve_effective_contract() no longer returns.

        Simpler than the evening-triggered path (§6): by this point in
        _setup(), self._contract is ALREADY the new contract (freshly
        resolved) and self._df_15m is ALREADY its seeded ST — no separate
        prefetch/dual-poll window needed, _setup()'s normal flow already
        did the equivalent work. Must run AFTER self.feed is started and
        subscribed to state.token (§3's invariant) — the exit below reads
        LTP off state.token, not self._contract.
        """
        if self.state.status != 'in_trade' or not self.state.token:
            return
        if self.state.token == self._contract['token']:
            return   # normal case -- no missed roll

        old_symbol = self.state.symbol
        old_token = self.state.token
        tag = _tag(self._contract['symbol_root'])
        logger.critical(f'Missed rollover detected: open position is on {old_symbol} '
                        f'(token {old_token}), but the effective contract is now '
                        f'{self._contract["symbol"]} (token {self._contract["token"]}). '
                        f"Rolling immediately, per §5's decided safety net.")
        _slack(f'\U0001f6a8 {tag}: MISSED ROLLOVER detected — {old_symbol} -> '
              f'{self._contract["symbol"]}. Rolling immediately at today\'s open.', SLACK_ERRORS_CHANNEL)

        entry_ts = datetime.fromisoformat(self.state.entry_ts)
        basis_price = historical_basis_price(self._contract, entry_ts)
        lot1_open = self.state.lot1_status == 'open'
        lot2_open = self.state.lot2_status == 'open'
        lots_to_reopen = ((self.state.lot1_lots or 0) if lot1_open else 0) + \
                          ((self.state.lot2_lots or 0) if lot2_open else 0)
        basis = None
        if basis_price is not None:
            basis = {
                'direction': self.state.direction, 'basis_price': basis_price,
                'signal_ts': self.state.signal_ts, 'signal_close': self.state.signal_close,
                'units': self.state.units, 'lots_to_reopen': lots_to_reopen,
                'lot2_only': lot2_open and not lot1_open,
            }
        else:
            logger.error('Missed-rollover basis precompute failed — reopen (if go) will be skipped.')

        go = False
        last = self._df_15m.iloc[-1]
        if pd.isna(last['trend']):
            logger.error("Missed-rollover veto: new contract's ST is still warming up (NaN trend) — no-go.")
        else:
            new_direction = 'bullish' if bool(last['trend']) else 'bearish'
            st_agree = new_direction == self.state.direction
            # §17: this is also a rollover reopen (just via the missed-roll
            # trigger rather than §6's evening one) -- gated the same way,
            # on top of the 15m ST-disagreement veto above. self._contract
            # and self._df_1m_today are already the NEW contract's by this
            # point in _setup() (see this method's own docstring).
            align_agree = self._check_1h_alignment(new_direction, contract=self._contract,
                                                    today_1m=self._df_1m_today)
            go = st_agree and align_agree
            logger.info(f'Missed-rollover veto check: old={self.state.direction} new={new_direction} '
                       f'st_agree={st_agree} align_agree={align_agree} -> {"GO" if go else "NO-GO"}.')

        closed = self._execute_exit_all('rollover')
        if not closed:
            logger.critical('Missed-rollover exit did not confirm closed — leaving state as '
                            'in_trade; will retry on the next restart (same fill-confirmation '
                            'invariant as always).')
            return
        self.feed.unsubscribe_options([old_token], exchange_type=MCX_FO_WS_EXCHANGE_TYPE)

        if go and basis is not None:
            self._rollover_basis = basis
            self._execute_rollover_reopen(self._contract)
            self._rollover_basis = None

        save_state(self.state)

    # -----------------------------------------------------------------------
    # Setup / teardown
    # -----------------------------------------------------------------------

    def _setup(self) -> bool:
        logger.info(f'Prometheus starting [DRY_RUN={DRY_RUN}] SYMBOL={SYMBOL}')

        self._contract = resolve_effective_contract(SYMBOL)
        tag = _tag(self._contract['symbol_root'])
        roll_note = ' (rolled early, tender-margin window)' if self._contract['rolled_early'] else ''
        logger.info(f'Effective contract: {self._contract["symbol"]} '
                    f'(token {self._contract["token"]}, expiry {self._contract["expiry_date"]:%Y-%m-%d})'
                    f'{roll_note}')
        _slack(f'{"[PAPER] " if DRY_RUN else ""}⚡ {tag} starting — '
              f'trading {self._contract["symbol"]}{roll_note}', SLACK_TRADEBOT_CHANNEL)

        # §15: seed_st15 is single-shot by default (no retry) -- a broker
        # hiccup during its live gap-fetch would otherwise block the whole
        # day's trading. _setup() runs before the main loop starts, before
        # any position exists and before _check_exit_conditions_ltp has
        # anything to monitor, so a blocking retry here is safe -- unlike
        # every other retry loop in this codebase, there's no concurrent
        # exit-check loop to starve.
        for attempt in range(1, SEED_RETRY_ATTEMPTS + 1):
            self._df_15m = seed_st15(self.obj, self._contract, datetime.now())
            if self._df_15m is not None and not self._df_15m.empty:
                break
            if attempt == 1:
                _slack(f'⚠️ {tag}: ST_15 seed failed (attempt {attempt}/{SEED_RETRY_ATTEMPTS}) — '
                      f'retrying every {SEED_RETRY_INTERVAL_SEC // 60} min.', SLACK_ERRORS_CHANNEL)
            logger.warning(f'ST_15 seed failed (attempt {attempt}/{SEED_RETRY_ATTEMPTS}).')
            if attempt < SEED_RETRY_ATTEMPTS:
                time.sleep(SEED_RETRY_INTERVAL_SEC)

        if self._df_15m is None or self._df_15m.empty:
            logger.critical(f'ST_15 seed failed after {SEED_RETRY_ATTEMPTS} attempts — cannot start.')
            _slack(f'\U0001f6a8 {tag}: seed failed after {SEED_RETRY_ATTEMPTS} attempts — cannot start.',
                  SLACK_ERRORS_CHANNEL)
            return False

        last = self._df_15m.iloc[-1]
        trend_str = ('bullish' if bool(last['trend']) else 'bearish') if not pd.isna(last['trend']) else 'warmup'
        _slack(f'{"[PAPER] " if DRY_RUN else ""}✅ {tag}: ST_15 seeded '
              f'({len(self._df_15m)} bars). Trend: {trend_str}.', SLACK_TRADEBOT_CHANNEL)

        # §15: seed_st15 already assembled + cached today's 1-min data
        # internally (private cache + live gap-fetch) — re-read that same
        # cache rather than the shared pipeline file, which never has
        # today's rows under this design.
        self._df_1m_today = read_today_cache(datetime.now())
        self._maybe_check_opening_bar()   # §11: covers both a fresh 09:00 start
                                           # (bar not there yet, no-op) and a
                                           # mid-day restart (bar already
                                           # present in the cache, check now
                                           # rather than never)

        feed_token = self.obj.getfeedToken()
        self.feed = SharedFeed()
        self.feed.start(
            auth_token=self.auth_token, api_key=self._api_key, client_code=self._client_code,
            feed_token=feed_token,
            alert_callback=lambda m: (_slack(f'⚠️ {tag}: {m}', SLACK_ERRORS_CHANNEL),
                                      logger.warning(m)),
        )
        # Deliberately NOT passed via startup_tokens: SharedFeed's
        # _subscribed_index bucket (what startup_tokens populates) drops
        # exchange_type on reconnect — resubscribe_all() hardcodes
        # EXCHANGE_NSE_CM for everything in it (websocket_feed.py:327-331),
        # which would silently resubscribe this MCX token under the wrong
        # exchange after a WS reconnect. subscribe_options's _subscribed_options
        # bucket tracks {token: exchange_type} per-token and resubscribes
        # correctly — used here for the one-time, permanent-for-the-session
        # subscription instead. Never unsubscribed except by the rollover
        # sequence (§6), once its own exit is confirmed.
        self.feed.subscribe_options([self._contract['token']], exchange_type=MCX_FO_WS_EXCHANGE_TYPE)
        # §3's hard prerequisite: if resuming in_trade on a token that
        # differs from the freshly-resolved self._contract (a missed
        # rollover, §5), ALSO subscribe state.token's feed — every price
        # read while in_trade keys off state.token, not self._contract, and
        # that only works if it's actually subscribed.
        if self.state.status == 'in_trade' and self.state.token and self.state.token != self._contract['token']:
            self.feed.subscribe_options([self.state.token], exchange_type=MCX_FO_WS_EXCHANGE_TYPE)

        if not DRY_RUN:
            self.order_watcher.start(auth_token=self.auth_token, api_key=self._api_key,
                                     client_code=self._client_code, feed_token=feed_token)

        if self.state.status == 'in_trade' and self.state.token:
            logger.info('Resuming in-trade state.')
            # No feed.subscribe_options() needed — the contract token is
            # already permanently subscribed via startup_tokens above (same
            # token every session, unlike Iris's per-trade option tokens).
            # Reconstruct the cumulative-tracker row from persisted state so a
            # crash mid-trade doesn't lose trade_id/entry fields when the
            # trade eventually closes (lot1/lot2 exit fields, if any were
            # already booked before the crash, are also carried forward).
            self._pending_trade_row = {
                'trade_id': self._trade_counter, 'contract_expiry': self.state.contract_expiry,
                'direction': self.state.direction, 'units': self.state.units,
                'entry_ts': self.state.entry_ts, 'entry_price': self.state.entry_price,
                'signal_ts': self.state.signal_ts, 'signal_close': self.state.signal_close,
                'entry_slippage_points': None,
                'sl_price': self.state.sl_price,
                'lot1_target': self.state.lot1_target, 'lot2_target': self.state.lot2_target,
                'lot2_target_source': self.state.lot2_target_source,
            }
            if self.state.lot1_status == 'booked':
                self._pending_trade_row.update({
                    'lot1_exit_ts': self.state.lot1_exit_ts, 'lot1_exit_price': self.state.lot1_exit_price,
                    'lot1_exit_reason': self.state.lot1_exit_reason,
                })
            if self.state.lot2_status == 'booked':
                self._pending_trade_row.update({
                    'lot2_exit_ts': self.state.lot2_exit_ts, 'lot2_exit_price': self.state.lot2_exit_price,
                    'lot2_exit_reason': self.state.lot2_exit_reason,
                })
            # §4: reconciliation gap flagged in the plan (crash between order
            # placement and fill confirmation) — not built; a resumed in-trade
            # state is trusted as-is, same as Iris/Athena today. Flag loudly.
            _slack(f'ℹ️ {tag}: resumed with an open position from a prior session '
                  f'({self.state.direction}, entry {self.state.entry_price}). State was NOT '
                  f'reconciled against the broker\'s order book — verify manually if in doubt.',
                  SLACK_TRADEBOT_CHANNEL)
        else:
            self.state.status = 'watching'
        save_state(self.state)

        # §5: fix up today's contract first (if a rollover was missed
        # overnight), THEN check whether ANOTHER roll is needed tonight —
        # in that order, so §4's check runs against the now-correct contract.
        self._recover_missed_rollover()
        self._check_rollover_tonight()

        logger.info('Setup complete — watchdog armed.')
        return True

    def _teardown(self) -> None:
        tag = _tag(self._contract['symbol_root']) if self._contract else '*Prometheus*'

        if self._pending_flip is not None:
            # Rare -- session ended exactly mid-Rule-7-transition. Safe by
            # construction either way (nothing here was ever fabricated),
            # but worth a visible note rather than a silently unexplained
            # 'watching' status the next morning.
            logger.warning(f"Teardown with a Rule 7 pending flip still unresolved "
                           f"(direction={self._pending_flip['direction']}, "
                           f"opened_lots={self._pending_flip['opened_lots']}/"
                           f"{self._pending_flip['new_trade_lots_target']}) — abandoned, not retried "
                           f"further. State reflects whatever was actually confirmed.")
            _slack(f'ℹ️ {tag}: session ended mid-Rule-7-flip — '
                  f'{self._pending_flip["opened_lots"]}/{self._pending_flip["new_trade_lots_target"]} '
                  f'of the new {self._pending_flip["direction"]} position opened. Not fabricated, '
                  f'not retried further this session.', SLACK_TRADEBOT_CHANNEL)

        if self._kill_no_exit and self.state.status == 'in_trade':
            # KILL's entire premise (slack_listener.py's own message: "Control
            # dropped. Position remains OPEN.") is to hand the position off
            # untouched for manual management -- do not place any exit order,
            # do not change state. Only the feed/process itself shuts down.
            if self.feed:
                try:
                    self.feed.stop()
                except Exception:
                    pass
            logger.warning('Teardown after KILL — leaving open position untouched, per Kill Switch.')
            _slack(f'⏹ {tag}: stopped via Kill Switch. Position left OPEN and untouched, '
                  f'as promised — manage it manually, or restart Prometheus to resume monitoring it.',
                  SLACK_TRADEBOT_CHANNEL)
            self._send_session_report()
            return

        # §2 (Phase 3, 2026-09-04): no EOD flatten — a position is *expected*
        # to still be open at session end most days (§3: positions can span
        # a contract roll), so teardown's normal path is "leave it open,
        # stop the feed, save state, exit cleanly," not "force flat and go
        # idle" the way Phase 2 always did. The rare exits that DO need to
        # happen before the process exits (SL/target/trend_flip firing right
        # as the loop ends) are handled by their own existing paths
        # (_check_exit_conditions_ltp, _handle_new_15m_bar) during run()
        # itself — teardown never reaches in and flattens on its own
        # initiative any more.
        if self.feed:
            try:
                self.feed.stop()
            except Exception:
                pass

        clear_today_cache()   # tomorrow re-fetches fresh regardless of position state (§15)

        if self.state.status == 'in_trade':
            save_state(self.state)
            logger.info('Teardown with an open position — left OPEN, as designed (Phase 3, no EOD flatten).')
            _slack(f'{"[PAPER] " if DRY_RUN else ""}⏹ {tag} stopped. Position left OPEN — expected, '
                  f'not an error. A restart resumes monitoring it.', SLACK_TRADEBOT_CHANNEL)
            self._send_session_report()
            return

        self.state.status = 'idle'
        save_state(self.state)
        logger.info('Prometheus stopped.')
        _slack(f'{"[PAPER] " if DRY_RUN else ""}⏹ {tag} stopped.', SLACK_TRADEBOT_CHANNEL)
        self._send_session_report()

    def _send_session_report(self) -> None:
        """
        End-of-session Slack report — mirrors leto.py's _send_session_report
        style (divider lines, per-trade blocks, bold session total,
        #tradebot-updates) but reads prometheus_trades.csv (the cumulative
        tracker) for today's date rather than a single in-memory summary
        dict: unlike Leto's once-daily routed strategies, Prometheus can
        have several trades in one session (confirmed 2026-08-31: 2 trades
        in a single day), so a Leto-style "one outcome per strategy" shape
        doesn't fit -- this lists every trade plus a session total, and
        reports a still-open position (§2, Phase 3: no EOD flatten, so this
        is the NORMAL end-of-day shape on most days, not a defensive
        fallback for a rare unclosed position the way it was in Phase 2).
        Wrapped so a reporting failure never masks the actual teardown.
        """
        try:
            tag_root = self._contract['symbol_root'] if self._contract else SYMBOL
            day_str = datetime.now().strftime('%a %d %b %Y')
            lines = [f'\U0001f4ca *Prometheus [{tag_root}] — Session Report*  |  {day_str}', '']
            lines.append('━' * 37)

            today = datetime.now().date()
            trades_today = pd.DataFrame()
            if TRADES_FILE.exists():
                all_trades = pd.read_csv(TRADES_FILE, parse_dates=['entry_ts'])
                if not all_trades.empty:
                    trades_today = all_trades[all_trades['entry_ts'].dt.date == today]

            total_rs = 0.0
            traded = not trades_today.empty or self.state.status == 'in_trade'

            if not traded:
                lines.append('  ↳ No trade today')
                lines.append('')
            else:
                for _, t in trades_today.iterrows():
                    direction = str(t['direction']).capitalize()
                    entry_ts_str = pd.Timestamp(t['entry_ts']).strftime('%H:%M')
                    entry_price = t['entry_price']
                    exit_candidates = [pd.Timestamp(v) for v in
                                       (t.get('lot1_exit_ts'), t.get('lot2_exit_ts')) if pd.notna(v)]
                    exit_ts_str = max(exit_candidates).strftime('%H:%M') if exit_candidates else '—'
                    exit_reason = t.get('lot2_exit_reason') or t.get('lot1_exit_reason') or '?'
                    exit_str = str(exit_reason).replace('_', ' ').title()
                    pnl_pts = t.get('total_pnl_points') or 0
                    pnl_rs = t.get('total_pnl_rs') or 0
                    total_rs += pnl_rs

                    lines.append(f"*Trade #{int(t['trade_id'])}*  ·  {direction}  |  Units: {int(t['units'])}")
                    lines.append(f"  ↳ Entry: {entry_ts_str} @ {entry_price:.2f}   "
                                 f"Exit: {exit_ts_str}  ·  {exit_str}")
                    lines.append(f"  ↳ P&L        : *{pnl_pts:+.1f} pts  ({pnl_rs:+,.0f} Rs)*")
                    lines.append('')

                if self.state.status == 'in_trade':
                    direction = (self.state.direction or '?').capitalize()
                    entry_ts_str = (datetime.fromisoformat(self.state.entry_ts).strftime('%H:%M')
                                    if self.state.entry_ts else '?')
                    entry = self.state.entry_price or 0
                    ltp = self.state.last_known_ltp or entry
                    # §13 (2026-09-04): realised + unrealised, not unrealised
                    # alone — a still-open trade routinely has one lot
                    # already booked under Phase 3 (positions run days to
                    # weeks, §3), and that locked-in P&L was previously
                    # invisible here, silently understating the session total.
                    pnl = self._compute_trade_pnl(ltp)
                    total_rs += pnl['total_rs']

                    lines.append(f"*Open Position*  ·  {direction}  |  Units: {self.state.units}")
                    lines.append(f"  ↳ Entry: {entry_ts_str} @ {entry:.2f}   Still open at session end")
                    lines.append(f"  ↳ Realised   : {pnl['realised_pts']:+.1f} pts  ({pnl['realised_rs']:+,.0f} Rs)")
                    lines.append(f"  ↳ Unrealised : {pnl['unrealised_pts']:+.1f} pts  ({pnl['unrealised_rs']:+,.0f} Rs)")
                    lines.append(f"  ↳ P&L        : *{pnl['total_rs']:+,.0f} Rs*")
                    lines.append('')

            lines.append('━' * 37)
            lines.append(f'*Session Total  :  {total_rs:+,.0f} Rs*')
            lines.append('━' * 37)

            _slack('\n'.join(lines), SLACK_TRADEBOT_CHANNEL)
        except Exception as e:
            logger.error(f'_send_session_report failed: {e}')

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    def run(self):
        if not self._setup():
            self._summary['no_trade_reason'] = 'Setup failed'
            return self._summary

        tag = _tag(self._contract['symbol_root'])
        session_end = datetime.now().replace(
            hour=int(SESSION_END_TIME.split(':')[0]), minute=int(SESSION_END_TIME.split(':')[1]),
            second=0, microsecond=0)
        last_update_ts = time.time()

        # Non-blocking 1-min boundary tracker — deliberately NOT a blocking
        # sleep-until-boundary (that pattern is right for a pure poller like
        # mcx_live_downloader.py, wrong here: it would starve the LTP-driven
        # exit checks below for up to 60s at a time, plan §3's "candle- vs
        # LTP-dependent logic stay two explicit blocks" requirement).
        next_boundary = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=1)

        try:
            while not self._shutdown and FLAG_PATH.exists() and datetime.now() < session_end:
                self._check_command_flag()
                now = datetime.now()

                # ── §7: retry a stuck Rule 7 combined order every tick,
                #    regardless of status -- it can straddle in_trade
                #    (old lots still closing) and watching (old side
                #    closed, new side not yet fully open) ─────────────
                if self._pending_flip is not None:
                    self._retry_pending_flip()

                # ── In-trade: tight LTP-driven exit loop, every tick ────
                if self.state.status == 'in_trade':
                    self._check_exit_conditions_ltp(now)
                    if time.time() - last_update_ts >= TRADE_UPDATE_SEC:
                        self._send_trade_update()
                        last_update_ts = time.time()

                # ── 1-min boundary reached: poll, merge, and on a 15-min
                #    mark, recompute ST and check entry/trend-flip (§3) ──
                if now >= next_boundary:
                    boundary = next_boundary
                    self._recover_pending_windows()
                    self._harvest_tick_ohlc()   # provisional-boundary feature, every 1-min cycle
                    self._check_rollover_timing(now)   # §6 steps 2-4, same 1-min cadence
                    win_to = datetime.now()
                    win_from = win_to - timedelta(minutes=5)
                    df = fetch_one_minute_window(self.obj, self._contract['token'], win_from, win_to)
                    if df is not None:
                        self._merge_1m(df)
                    else:
                        self._pending_recovery.append((win_from, win_to))

                    # §12 (2026-09-04): don't compute a 15m bar the instant
                    # its boundary tick arrives if the 1-min poller hasn't
                    # actually delivered all 15 minutes yet (an AB1021
                    # stretch, confirmed live 2026-09-03 to span ~60s) —
                    # wait, re-checking every 1-min cycle, up to
                    # DEFERRED_BAR_CUTOFF_MIN before building it from
                    # whatever's on hand with a loud warning. Minimizes
                    # flip-detection lag over fallback precision (user's
                    # call) — SL/target monitoring above is completely
                    # unaffected by the wait either way.
                    if boundary.minute % 15 == 0 and self._pending_15m_boundary is None:
                        self._pending_15m_boundary = boundary
                        self._pending_15m_deadline = boundary + timedelta(minutes=DEFERRED_BAR_CUTOFF_MIN)

                        # Provisional boundary check (2026-09-04) — exactly
                        # once per boundary, right here, before the
                        # deferred-wait loop below gets its first chance to
                        # run. Predicate is "window incomplete AT the
                        # boundary tick," checked directly — NOT "the
                        # boundary poll's own retry burst exhausted," which
                        # is a different (and less precise) condition.
                        pb_window_start = boundary - timedelta(minutes=15)
                        pb_window = self._df_1m_today[
                            (self._df_1m_today['time_stamp'] >= pb_window_start) &
                            (self._df_1m_today['time_stamp'] < boundary)]
                        if len(pb_window) < 15:
                            self._evaluate_provisional_boundary(boundary, pb_window_start)

                    if self._pending_15m_boundary is not None:
                        pb = self._pending_15m_boundary
                        window_start = pb - timedelta(minutes=15)
                        window = self._df_1m_today[(self._df_1m_today['time_stamp'] >= window_start) &
                                                   (self._df_1m_today['time_stamp'] < pb)]
                        complete = len(window) >= 15
                        past_cutoff = datetime.now() >= self._pending_15m_deadline
                        if complete or past_cutoff:
                            if not complete:
                                logger.warning(f'15m bar {window_start:%H:%M}-{pb:%H:%M} still '
                                               f'incomplete ({len(window)}/15) after '
                                               f'{DEFERRED_BAR_CUTOFF_MIN}min cutoff — building from '
                                               f'what is on hand.')
                                _slack(f'⚠️ {tag}: 15m bar {window_start:%H:%M}-{pb:%H:%M} still '
                                      f'incomplete ({len(window)}/15) after '
                                      f'{DEFERRED_BAR_CUTOFF_MIN}min — building anyway.',
                                      SLACK_ERRORS_CHANNEL)
                            self._handle_new_15m_bar(pb)
                            self._pending_15m_boundary = None
                            self._pending_15m_deadline = None
                            # Provisional-boundary feature: this window is
                            # resolved (real bar computed either way) —
                            # reset the tick-OHLC accumulator to start
                            # tracking the NEXT window fresh.
                            self._tick_ohlc_accum = {'open': None, 'high': None, 'low': None, 'close': None}

                    # §4: "one row per polling cycle" — the 1-min boundary,
                    # not the 0.5s exit-check tick (~24,000 rows/trade at
                    # tick rate vs. ~200 at poll rate for the average hold).
                    if self.state.status == 'in_trade':
                        ltp = self.state.last_known_ltp
                        if ltp:
                            self._append_running_row(ltp)

                    next_boundary += timedelta(minutes=1)

                time.sleep(0.5 if self.state.status == 'in_trade' else 1.0)

            if not FLAG_PATH.exists():
                logger.info('Circuit breaker flag removed externally — stopping.')
            elif datetime.now() >= session_end:
                logger.info(f'Session end ({SESSION_END_TIME}) reached — stopping.')

        except RuntimeError as e:
            if 'Session terminated' not in str(e):
                logger.error(f'Unhandled RuntimeError in Prometheus run loop: {e}')
                _slack(f'⚠️ {tag} EXCEPTION: {e}', SLACK_ERRORS_CHANNEL)
        except Exception as e:
            logger.error(f'Unhandled exception in Prometheus run loop: {e}')
            _slack(f'⚠️ {tag} EXCEPTION: {e}', SLACK_ERRORS_CHANNEL)
        finally:
            self._teardown()
        return self._summary

    def _recover_pending_windows(self) -> None:
        if not self._pending_recovery:
            return
        still_pending = []
        for (win_from, win_to) in self._pending_recovery:
            recovered = fetch_one_minute_window(self.obj, self._contract['token'], win_from, win_to)
            if recovered is not None:
                self._merge_1m(recovered)
                logger.info(f'Recovered pending window [{win_from} -> {win_to}]')
            else:
                still_pending.append((win_from, win_to))
        self._pending_recovery = still_pending

    def _merge_1m(self, new_df: pd.DataFrame) -> None:
        """§15 (2026-09-04): writes into Prometheus's own private intraday
        cache (TODAY_1M_CACHE_FILE) — NOT the shared file data_downloader_mcx.py
        maintains, which Prometheus never writes to any more — AND updates
        the in-memory today-accumulator used to build 15-min bars."""
        _merge_and_save(TODAY_1M_CACHE_FILE, new_df)

        working = new_df.copy()
        if working['time_stamp'].dt.tz is not None:
            working['time_stamp'] = working['time_stamp'].dt.tz_localize(None)
        today = datetime.now().date()
        new_today = working[working['time_stamp'].dt.date == today]
        if new_today.empty:
            return
        combined = _safe_concat([self._df_1m_today, new_today], ignore_index=True)
        combined = combined.drop_duplicates(subset=['time_stamp'], keep='last')
        self._df_1m_today = combined.sort_values('time_stamp').reset_index(drop=True)
        self._maybe_check_opening_bar()

    def _maybe_check_opening_bar(self) -> None:
        """§11 (2026-09-04): once per session, the first time today's 09:00
        1-min row is available in self._df_1m_today, check it against
        CRUDEOIL's own 09:00 print for the thin-liquidity price-discovery
        artifact and patch it in place if OPENING_BAR_CORRECTION_ENABLED —
        in memory only, at 1-min-ingestion time, before it's ever read by
        any resample (compute_st's ATR window included). When the toggle
        is off, this still runs and logs what it WOULD have done, so the
        user can validate ST accuracy against the raw, uncorrected chart.
        """
        if self._opening_bar_checked or self._df_1m_today.empty:
            return
        session_start = pd.Timestamp(f'{datetime.now().date()} {SESSION_START_TIME}')
        mask = self._df_1m_today['time_stamp'] == session_start
        if not mask.any():
            return
        self._opening_bar_checked = True   # only ever attempt this once per session,
                                            # success or failure — never retried mid-session
        row = self._df_1m_today.loc[mask].iloc[0]
        m_bar = {'open': row['open'], 'high': row['high'], 'low': row['low'], 'close': row['close']}
        o_bar = fetch_crudeoil_opening_bar(self.obj, session_start)
        if o_bar is None:
            logger.warning('Opening-bar artifact check: CRUDEOIL reference unavailable — skipping.')
            return
        patched = patch_opening_bar_if_artifact(m_bar, o_bar)
        if patched is not m_bar:
            idx = self._df_1m_today.index[mask][0]
            for col in ('open', 'high', 'low', 'close'):
                self._df_1m_today.at[idx, col] = patched[col]

    # -----------------------------------------------------------------------
    # 15-min bar handling — flip detection, fresh entry, trend-flip exit+reentry
    # -----------------------------------------------------------------------

    def _handle_new_15m_bar(self, boundary: datetime) -> None:
        tag = _tag(self._contract['symbol_root'])
        window_start = boundary - timedelta(minutes=15)
        window = self._df_1m_today[(self._df_1m_today['time_stamp'] >= window_start) &
                                   (self._df_1m_today['time_stamp'] < boundary)]
        # A missing bar isn't a hypothetical: the 1-min poller's own pending-
        # recovery queue can still be catching up when a 15-min boundary
        # fires. Unlike seed_st15 (gap-checked, refuses to seed on any hole),
        # there's no way to "refuse" a live boundary — the bar either gets
        # built from what's on hand or the ST series silently gets a gap
        # compute_st then computes across as if contiguous. Alert loudly
        # either way rather than let it pass unnoticed (matches this
        # codebase's "no silent staleness" convention throughout §1/§3).
        if window.empty:
            logger.error(f'No 1-min bars for the {window_start:%H:%M}-{boundary:%H:%M} window — '
                        f'15m bar SKIPPED, leaving a gap in the ST series.')
            _slack(f'\U0001f6a8 {tag}: no 1-min data for {window_start:%H:%M}-{boundary:%H:%M} — '
                  f'15m bar skipped, ST series has a gap.', SLACK_ERRORS_CHANNEL)
            return
        if len(window) < 8:   # < ~half the expected 15 rows
            logger.warning(f'Only {len(window)}/15 1-min bars for {window_start:%H:%M}-{boundary:%H:%M} — '
                           f'building the 15m bar from incomplete data.')
            _slack(f'⚠️ {tag}: only {len(window)}/15 1-min bars for {window_start:%H:%M}-'
                  f'{boundary:%H:%M} — 15m bar built from incomplete data.', SLACK_ERRORS_CHANNEL)

        new_bar = pd.DataFrame([{
            'time_stamp': window_start, 'open': window['open'].iloc[0],
            'high': window['high'].max(), 'low': window['low'].min(),
            'close': window['close'].iloc[-1], 'volume': window['volume'].sum(),
        }])
        combined = _safe_concat([self._df_15m, new_bar], ignore_index=True)
        combined = combined.drop_duplicates(subset=['time_stamp'], keep='last').sort_values('time_stamp')
        self._df_15m = compute_st(combined.reset_index(drop=True), ST_PERIOD, ST_MULTIPLIER)
        persist_15m_series(self._df_15m)

        row = self._df_15m[self._df_15m['time_stamp'] == window_start]
        if row.empty:
            return
        bar = row.iloc[-1]
        if pd.isna(bar['trend']):
            return

        flip = bool(bar['trend_flip'])
        direction_now = 'bullish' if bool(bar['trend']) else 'bearish'
        tag = _tag(self._contract['symbol_root'])

        # Provisional boundary computation (2026-09-04): this boundary
        # already had a provisional action taken on it — this is the
        # reconciliation moment, not a fresh decision. Never a second live
        # action for the same boundary.
        if self._provisional_pending is not None and self._provisional_pending['boundary'] == boundary:
            self._reconcile_provisional(direction_now, bar)
            return

        if flip:
            logger.info(f'15m bar {window_start:%H:%M} — ST={bar["supertrend"]:.2f} '
                       f'close={bar["close"]:.2f}  FLIP -> {direction_now}')
            _slack(f'\U0001f504 {tag}: ST_15 flip -> *{direction_now}* at {window_start:%H:%M} '
                  f'(close={bar["close"]:.2f}, ST={bar["supertrend"]:.2f})', SLACK_TRADEBOT_CHANNEL)
        else:
            logger.info(f'15m bar {window_start:%H:%M} — ST={bar["supertrend"]:.2f} '
                       f'close={bar["close"]:.2f}  no flip')

        if self.state.status == 'in_trade':
            if flip and direction_now != self.state.direction:
                # Rule 7 (§7, 2026-09-04): one combined net order for the
                # exit and re-entry together, not the old two/three-order
                # sequence — see _execute_rule7_flip.
                self._execute_rule7_flip(direction_now, window_start, bar['close'])
            return

        # status == 'watching' — fresh entry detection. §2 (Phase 3): no
        # LAST_ENTRY_TIME cutoff before close any more — configs_p3.py never
        # gated entries near close (positional, nothing to "hold until").
        # §4: suppressed once a confirmed rollover reaches ROLLOVER_TIME —
        # otherwise a fresh position could open seconds before the roll
        # flattens it straight back out. §17: additionally gated on 1h
        # alignment (inert unless ENTRY_FILTER_1H_ALIGN_ENABLED).
        if (flip and self._past_min_entry_guard(datetime.now()) and not self._rollover_entry_suppressed(datetime.now())
                and self._check_1h_alignment(direction_now)):
            self._execute_entry(direction_now, window_start, bar['close'])

    # -----------------------------------------------------------------------
    # Rule 7 — combine the flip's exit and re-entry into one net order (§7)
    # -----------------------------------------------------------------------

    def _execute_rule7_flip(self, direction_now: str, signal_ts, signal_close: float) -> None:
        """
        §7 (2026-09-04): a flip's exit and re-entry are always the same
        instrument, just opposite net direction — one order for the net
        quantity (`old_open_lots + new_trade_lots`), not two/three separate
        ones. Degenerates cleanly to a plain entry (both lots already
        flat — nothing to combine) or a plain exit (`new_trade_lots=0`
        when re-entry isn't allowed — still within MIN_ENTRY_BUFFER_MIN of
        the session open, a suppressed rollover evening, or §17's 1h
        filter disagreeing).
        """
        old_open_lots = ((self.state.lot1_lots or 0) if self.state.lot1_status == 'open' else 0) + \
                         ((self.state.lot2_lots or 0) if self.state.lot2_status == 'open' else 0)
        # §4: suppressed once a confirmed rollover reaches ROLLOVER_TIME.
        # §17: additionally gated on 1h alignment (inert unless
        # ENTRY_FILTER_1H_ALIGN_ENABLED) — the exit half above still always
        # happens regardless; only the re-entry half is affected.
        reentry_allowed = (self._past_min_entry_guard(datetime.now()) and not self._rollover_entry_suppressed(datetime.now())
                           and self._check_1h_alignment(direction_now))

        if old_open_lots == 0:
            if reentry_allowed:
                self._execute_entry(direction_now, signal_ts, signal_close)
            return

        units = self._calculate_units()
        new_trade_lots = units * LOTS_PER_LEG * 2 if reentry_allowed else 0
        if new_trade_lots > 0 and not self._check_margin_sufficient(units):
            new_trade_lots = 0   # can't afford the new leg -- still close the old one below

        self._pending_flip = {
            'direction': direction_now, 'signal_ts': signal_ts, 'signal_close': signal_close,
            'units': units, 'new_trade_lots_target': new_trade_lots,
            'opened_lots': 0, 'opened_price_weighted_sum': 0.0,
            'first_alert_ts': None, 'last_realert_ts': None,
        }
        self._retry_pending_flip()

    def _retry_pending_flip(self) -> None:
        """
        Retried every tick of the main loop until fully resolved (§7) —
        deliberately NOT gated on a fresh 15-min trend_flip transition.
        Confirmed against the code before this was built: `trend_flip` is
        a one-shot flag, true only on the bar the flip actually happens —
        a stuck partial fill on a LATER boundary would never re-trigger
        _handle_new_15m_bar's old exit-then-entry path, and
        _check_exit_conditions_ltp doesn't cover it either (it only checks
        SL/target against the CURRENT state.direction). This is what closes
        that gap.
        """
        if self._pending_flip is None:
            return
        pf = self._pending_flip

        old_open_lots = ((self.state.lot1_lots or 0) if self.state.lot1_status == 'open' else 0) + \
                         ((self.state.lot2_lots or 0) if self.state.lot2_status == 'open' else 0)
        new_remaining = max(0, pf['new_trade_lots_target'] - pf['opened_lots'])
        requested = old_open_lots + new_remaining

        if requested == 0:
            self._pending_flip = None   # defensive -- resolution below should already have cleared this
            return

        order_ids = place_order(self.obj, 'BUY' if pf['direction'] == 'bullish' else 'SELL',
                                self._contract['symbol'], self._contract['token'],
                                requested, DRY_RUN, self._contract['freeze_qty'])
        if not order_ids:
            self._alert_pending_flip_stuck(f'combined order FAILED to place (requested {requested} lots).')
            return

        fill_price, filled = get_fill_price_and_qty(
            self.obj, self.order_watcher, order_ids, self._contract['symbol'],
            self._contract['token'], requested, DRY_RUN, self.feed)
        if fill_price is None or filled == 0:
            self._alert_pending_flip_stuck(f'combined order placed (orderid(s)={order_ids}) but fill unconfirmed.')
            return

        # Reconcile: close old lots first (lot2-before-lot1 — DECIDED tie-break,
        # §7: keeps lot1's nearer target alive, exits sooner on a favorable
        # reversal), the remainder opens the new position.
        closed_qty = min(filled, old_open_lots)
        opened_qty = filled - closed_qty

        remaining_close = closed_qty
        if remaining_close > 0 and self.state.lot2_status == 'open':
            take = min(remaining_close, self.state.lot2_lots)
            self._apply_confirmed_lot_exit(2, fill_price, take, 'trend_flip')
            remaining_close -= take
        if remaining_close > 0 and self.state.lot1_status == 'open':
            take = min(remaining_close, self.state.lot1_lots)
            self._apply_confirmed_lot_exit(1, fill_price, take, 'trend_flip')
            remaining_close -= take

        if opened_qty > 0:
            pf['opened_price_weighted_sum'] += fill_price * opened_qty
            pf['opened_lots'] += opened_qty

        still_old_open = ((self.state.lot1_lots or 0) if self.state.lot1_status == 'open' else 0) + \
                          ((self.state.lot2_lots or 0) if self.state.lot2_status == 'open' else 0)
        still_new_remaining = pf['new_trade_lots_target'] - pf['opened_lots']

        if still_old_open == 0 and still_new_remaining <= 0:
            if pf['opened_lots'] > 0:
                avg_open_price = pf['opened_price_weighted_sum'] / pf['opened_lots']
                self._finalize_new_position(pf['direction'], pf['signal_ts'], pf['signal_close'],
                                            avg_open_price, pf['opened_lots'],
                                            pf['new_trade_lots_target'], pf['units'])
            logger.info('Rule 7 pending flip fully resolved.')
            self._pending_flip = None
        else:
            logger.warning(f'Rule 7 pending flip: {still_old_open} old lot(s) still open, '
                           f'{max(0, still_new_remaining)} new lot(s) still to open — retrying next tick.')

    def _alert_pending_flip_stuck(self, detail: str) -> None:
        """§7: immediate CRITICAL alert the first time a pending flip gets
        stuck, then a debounced re-alert (PENDING_FLIP_REALERT_DEBOUNCE_SEC,
        matching the stale-tick-watchdog's existing 5-min convention) while
        it stays stuck — visible without spamming Slack every tick."""
        pf = self._pending_flip
        tag = _tag(self._contract['symbol_root'])
        now = time.time()
        if pf['first_alert_ts'] is None:
            pf['first_alert_ts'] = pf['last_realert_ts'] = now
            logger.critical(f'Rule 7 pending flip stuck: {detail}')
            _slack(f'\U0001f6a8 {tag}: Rule 7 combined order stuck — {detail} Retrying every tick.',
                  SLACK_ERRORS_CHANNEL)
        elif now - pf['last_realert_ts'] >= PENDING_FLIP_REALERT_DEBOUNCE_SEC:
            pf['last_realert_ts'] = now
            logger.critical(f'Rule 7 pending flip STILL stuck: {detail}')
            _slack(f'\U0001f6a8 {tag}: Rule 7 combined order STILL stuck — {detail}', SLACK_ERRORS_CHANNEL)
        else:
            logger.warning(f'Rule 7 pending flip stuck (debounced): {detail}')

    # -----------------------------------------------------------------------
    # Entry
    # -----------------------------------------------------------------------

    def _execute_entry(self, direction: str, signal_ts: datetime, signal_close: float) -> None:
        tag = _tag(self._contract['symbol_root'])
        ltp = self.feed.get_ltp(self._contract['token'])
        if not ltp:
            logger.error('LTP unavailable — cannot enter.')
            return

        units = self._calculate_units()
        if not self._check_margin_sufficient(units):
            return

        requested_lots = units * LOTS_PER_LEG * 2
        order_ids = place_order(self.obj, 'BUY' if direction == 'bullish' else 'SELL',
                                self._contract['symbol'], self._contract['token'],
                                requested_lots, DRY_RUN, self._contract['freeze_qty'])
        if not order_ids:
            logger.error('Entry order failed.')
            return

        fill_price, filled_lots = get_fill_price_and_qty(
            self.obj, self.order_watcher, order_ids, self._contract['symbol'],
            self._contract['token'], requested_lots, DRY_RUN, self.feed)
        if fill_price is None or filled_lots == 0:
            logger.error('Entry fill verification failed — no position opened.')
            _slack(f'\U0001f6a8 {tag}: entry order failed to fill — no position opened.', SLACK_ERRORS_CHANNEL)
            return

        self._finalize_new_position(direction, signal_ts, signal_close, fill_price,
                                    filled_lots, requested_lots, units)

    def _finalize_new_position(self, direction: str, signal_ts, signal_close: float,
                               entry_price: float, filled_lots: int, requested_lots: int,
                               units: int, basis_price: float = None,
                               parent_trade_id: int = None, lot2_only: bool = False) -> None:
        """
        Shared position-construction logic (2026-09-04) — the fill has
        already happened and been confirmed by the caller; this only builds
        state/log/Slack from it. Used by a fresh entry (_execute_entry),
        Rule 7's combined-order reconciliation (§7), and a rollover reopen
        (§8), which is why it takes more than a plain entry needs:

        `basis_price`: rollover reopen only (§8). SL/target LEVELS are
        computed off this instead of `entry_price` (the historical-basis
        method) — `entry_price` itself, and all P&L everywhere else, still
        use the REAL fill price. None means "use entry_price," i.e. every
        non-rollover caller.
        `parent_trade_id`: rollover continuation only (§9) — when set, the
        trade-log row's `direction` gets the `-rollover` suffix
        (`state.direction` itself never does — it stays the plain binary
        everywhere it drives real logic).
        `lot2_only`: rollover reopen with only one lot surviving to the roll
        (§8 point 1) — sizes/targets the position as a lone lot2 (the
        farther target), not a fresh lot1+lot2 split.
        """
        tag = _tag(self._contract['symbol_root'])
        threshold_price = basis_price if basis_price is not None else entry_price

        if lot2_only:
            lot1_lots, lot2_lots = 0, filled_lots
            lot1_target = None
            _, sl_distance = resolve_thresholds(threshold_price)
            sl_price = (threshold_price - sl_distance if direction == 'bullish'
                       else threshold_price + sl_distance) if sl_distance is not None else None
            lot2_target, lot2_source = resolve_target2(threshold_price, direction)
        else:
            lot1_lots = min(filled_lots, units * LOTS_PER_LEG)
            lot2_lots = max(0, filled_lots - lot1_lots)
            lot1_distance, sl_distance = resolve_thresholds(threshold_price)
            lot1_target = (threshold_price + lot1_distance if direction == 'bullish'
                          else threshold_price - lot1_distance)
            sl_price = (threshold_price - sl_distance if direction == 'bullish'
                       else threshold_price + sl_distance) if sl_distance is not None else None
            lot2_target, lot2_source = resolve_target2(threshold_price, direction)

        if filled_lots < requested_lots:
            logger.warning(f'Partial fill: requested {requested_lots} lots, filled {filled_lots} '
                          f'(lot1={lot1_lots}, lot2={lot2_lots}).')
            _slack(f'⚠️ {tag}: partial fill — requested {requested_lots} lots, got '
                  f'{filled_lots}.', SLACK_ERRORS_CHANNEL)

        now = datetime.now()
        self._trade_counter += 1
        save_trade_counter(self._trade_counter)

        signal_ts_iso = signal_ts.isoformat() if hasattr(signal_ts, 'isoformat') else signal_ts

        self.state = PrometheusState(
            status='in_trade', direction=direction, units=units,
            entry_price=entry_price, entry_ts=now.isoformat(),
            recalibration_basis_price=basis_price,
            signal_ts=signal_ts_iso, signal_close=signal_close,
            contract_expiry=self._contract['expiry_date'].strftime('%Y-%m-%d'),
            symbol=self._contract['symbol'], token=self._contract['token'],
            sl_price=sl_price,
            lot1_target=lot1_target, lot1_lots=lot1_lots,
            lot1_status='open' if lot1_lots > 0 else 'never_opened',
            lot2_target=lot2_target, lot2_target_source=lot2_source, lot2_lots=lot2_lots,
            lot2_status='open' if lot2_lots > 0 else 'never_opened',
            last_known_ltp=entry_price,
        )
        save_state(self.state)

        self._trade_count += 1
        self._summary.update({'traded': True, 'direction': direction, 'units': units,
                              'entry_time': now.strftime('%H:%M')})

        entry_slippage = (entry_price - signal_close) if direction == 'bullish' else (signal_close - entry_price)
        log_direction = f'{direction}-rollover' if parent_trade_id is not None else direction
        self._pending_trade_row = {
            'trade_id': self._trade_counter, 'contract_expiry': self.state.contract_expiry,
            'direction': log_direction, 'units': units, 'entry_ts': now.isoformat(), 'entry_price': entry_price,
            'signal_ts': signal_ts_iso, 'signal_close': signal_close,
            'entry_slippage_points': round(entry_slippage, 2),
            'sl_price': round(sl_price, 2) if sl_price is not None else None,
            'lot1_target': round(lot1_target, 2) if lot1_target is not None else None,
            'lot2_target': round(lot2_target, 2), 'lot2_target_source': lot2_source,
            'parent_trade_id': parent_trade_id,
        }

        sl_str = f'{sl_price:.2f}' if sl_price is not None else 'n/a'
        lot1_str = f'{lot1_target:.2f}' if lot1_target is not None else 'n/a (lot2-only, §8)'
        basis_note = f' (recalibration basis {basis_price:.2f})' if basis_price is not None else ''
        msg = (f'{"[PAPER] " if DRY_RUN else ""}⚡ {tag}: Entered {direction.upper()}{" (rollover)" if parent_trade_id is not None else ""}\n'
              f'{self.state.symbol} | Units: {units} ({filled_lots} lots)\n'
              f'Entry: {entry_price:.2f}{basis_note} | SL: {sl_str}\n'
              f'Lot1 target: {lot1_str} | Lot2 target: {lot2_target:.2f} ({lot2_source})')
        logger.info(msg.replace('\n', '  '))
        _slack(msg)

    # -----------------------------------------------------------------------
    # Exit — priority: stop_loss -> lot1 target -> lot2 target -> trend_flip
    # (SL wins any same-tick tie against a target — backtest_p2.py convention).
    # §2 (Phase 3): no EOD-squareoff tier any more — a position is expected
    # to carry overnight/across days; matches configs_p3.py, which never
    # had an EOD exit calibrated into it.
    # -----------------------------------------------------------------------

    def _get_ltp(self) -> float:
        # §3 (2026-09-04): state.token, not self._contract['token'] — while
        # in_trade, every price read keys off the position's own token, not
        # whichever contract is currently resolved as "effective" (these can
        # diverge across a rollover, not yet built — this fix lands ahead of
        # that so the invariant is already in place when it does).
        if self.feed is not None and self.feed.is_connected():
            ltp = self.feed.get_ltp(self.state.token)
            if ltp is not None:
                return ltp
        return fetch_ltp_rest(self.obj, self.state.symbol, self.state.token)

    def _minutes_since_session_open(self, now: datetime):
        """
        Minutes elapsed since the ACTUAL first 1-min bar of today's
        session (self._df_1m_today's own earliest row), never a hardcoded
        clock time — most days that's SESSION_START_TIME (09:00), but on
        the evening-only special sessions the real open is 17:00, and a
        fixed clock-time threshold would already be hours in the past by
        then, guarding nothing. Same anchor-must-be-dynamic principle as
        the 15m/1h resample's day anchor (data_loader.py's
        origin=day.index[0], not a hardcoded 09:00). Returns None if no
        bar has arrived yet at all. Shared by both the first-minute exit
        guard (§10) and the min-entry guard below — both are the same
        "how long has today's session actually been open" question
        against a different threshold, not two separate mechanisms.
        """
        if self._df_1m_today.empty:
            return None
        first_bar_ts = self._df_1m_today['time_stamp'].min()
        return (now - first_bar_ts).total_seconds() / 60.0

    def _past_first_minute_guard(self, now: datetime) -> bool:
        """
        §10 (built 2026-09-04): True once today's session has genuinely
        been open for at least NO_EXIT_BEFORE_BUFFER_MIN minutes — gates
        _check_exit_conditions_ltp against a first-minute price-discovery
        print (the 2026-09-02 incident: a 447-point single-minute range
        that would have been indistinguishable from a real SL/target hit
        to the continuous LTP check). False (guard still active) if no
        bar has arrived yet — the very first tick IS the one this guard
        exists to hold off on.
        """
        elapsed = self._minutes_since_session_open(now)
        return elapsed is not None and elapsed >= NO_EXIT_BEFORE_BUFFER_MIN

    def _past_min_entry_guard(self, now: datetime) -> bool:
        """
        Fixed 2026-09-04 (same bug class as §10's guard, found while
        building it): replaces the old module-level now_after_min_entry(),
        which compared against a hardcoded MIN_ENTRY_TIME='09:15' clock
        time — silently zero minutes of thin-opening-liquidity protection
        on the evening-only sessions (real open 17:00, already past 09:15
        on the clock). Gates fresh entries and Rule 7 re-entries. False
        (still gated) if no bar has arrived yet.
        """
        elapsed = self._minutes_since_session_open(now)
        return elapsed is not None and elapsed >= MIN_ENTRY_BUFFER_MIN

    def _check_exit_conditions_ltp(self, now: datetime) -> None:
        if not self._past_first_minute_guard(now):
            return
        ltp = self._get_ltp()
        if ltp:
            self.state.last_known_ltp = ltp
            save_state(self.state)

        if not ltp:
            return

        direction = self.state.direction
        sl = self.state.sl_price
        if sl is not None:
            hit = (ltp <= sl) if direction == 'bullish' else (ltp >= sl)
            if hit:
                self._execute_exit_all('stop_loss')
                return

        if self.state.lot1_status == 'open':
            hit = (ltp >= self.state.lot1_target) if direction == 'bullish' else (ltp <= self.state.lot1_target)
            if hit:
                self._execute_exit_lot(1, 'target1', ltp)

        if self.state.status == 'in_trade' and self.state.lot2_status == 'open':
            hit = (ltp >= self.state.lot2_target) if direction == 'bullish' else (ltp <= self.state.lot2_target)
            if hit:
                self._execute_exit_lot(2, f'target2_{self.state.lot2_target_source}', ltp)

        if self.state.status == 'in_trade' and self.state.lot1_status != 'open' and self.state.lot2_status != 'open':
            self._finalize_trade()

    def _execute_exit_lot(self, lot_num: int, reason: str, ltp: float) -> bool:
        """
        Returns True only if the exit is genuinely confirmed (real orderid,
        real fill, filled > 0). On any failure, lot{n}_status is left
        untouched (still 'open') and NOTHING about this lot is marked
        closed -- no fabricated fill price, no P&L, no Slack success
        message. This is a deliberate, hard invariant, added after a real
        incident (2026-08-31): the previous code placed an order that
        failed (orderid=None), then silently used the current LTP as a fake
        "fill price" when fill resolution predictably timed out, marking
        both lots closed and returning to 'watching' -- while the real
        position stayed fully open at the broker, completely unmonitored,
        for ~28 minutes until caught manually. Returning False here instead
        leaves lot_status=='open', so the NEXT tick's exit-condition check
        (every 0.5-1s) retries automatically -- no separate retry loop
        needed, and no lie enters the state file or trade log.
        """
        lots = self.state.lot1_lots if lot_num == 1 else self.state.lot2_lots
        if not lots:
            return True   # nothing to exit -- vacuously done, not a failure
        tag = _tag(self._contract['symbol_root'])
        order_ids = place_order(self.obj, 'SELL' if self.state.direction == 'bullish' else 'BUY',
                                self.state.symbol, self.state.token, lots, DRY_RUN,
                                self._contract['freeze_qty'])
        if not order_ids:
            logger.critical(f'Lot{lot_num} exit order FAILED to place ({reason}) — '
                            f'position may still be OPEN at the broker.')
            _slack(f'\U0001f6a8 {tag}: Lot{lot_num} exit order FAILED to place ({reason}). '
                  f'Position may still be OPEN at the broker — will keep retrying automatically, '
                  f'but check the broker terminal manually now.', SLACK_ERRORS_CHANNEL)
            return False

        fill_price, filled = get_fill_price_and_qty(
            self.obj, self.order_watcher, order_ids, self.state.symbol, self.state.token,
            lots, DRY_RUN, self.feed)
        if fill_price is None or filled == 0:
            logger.critical(f'Lot{lot_num} exit order placed (orderid(s)={order_ids}, {reason}) but fill '
                            f'could NOT be confirmed (WS and REST both exhausted) — position status '
                            f'unknown, may still be open.')
            _slack(f'\U0001f6a8 {tag}: Lot{lot_num} exit order placed (orderid(s)={order_ids}) but fill '
                  f'unconfirmed — position status UNKNOWN. Check the broker terminal manually now; '
                  f'will keep retrying automatically.', SLACK_ERRORS_CHANNEL)
            return False

        self._apply_confirmed_lot_exit(lot_num, fill_price, filled, reason)
        return True

    def _apply_confirmed_lot_exit(self, lot_num: int, fill_price: float, filled_qty: int,
                                  reason: str) -> None:
        """
        Shared state-mutation for a CONFIRMED lot exit (2026-09-04) — used
        by both the normal single-lot exit path above and Rule 7's
        combined-order reconciliation (§7, _retry_pending_flip). Caller is
        responsible for confirming the fill FIRST; this never fabricates
        one and is never called speculatively.
        """
        tag = _tag(self._contract['symbol_root'])
        entry = self.state.entry_price
        pnl_pts = (fill_price - entry) if self.state.direction == 'bullish' else (entry - fill_price)
        pnl_rs = round(pnl_pts * filled_qty * LOT_SIZE, 2)
        self._total_pnl_rs += pnl_rs
        now = datetime.now()

        if lot_num == 1:
            self.state.lot1_status, self.state.lot1_exit_price = 'booked', round(fill_price, 2)
            self.state.lot1_exit_ts, self.state.lot1_exit_reason = now.isoformat(), reason
            self._pending_trade_row.update({
                'lot1_exit_ts': now.isoformat(), 'lot1_exit_price': round(fill_price, 2),
                'lot1_exit_reason': reason, 'lot1_pnl_points': round(pnl_pts, 2), 'lot1_pnl_rs': pnl_rs,
            })
        else:
            self.state.lot2_status, self.state.lot2_exit_price = 'booked', round(fill_price, 2)
            self.state.lot2_exit_ts, self.state.lot2_exit_reason = now.isoformat(), reason
            self._pending_trade_row.update({
                'lot2_exit_ts': now.isoformat(), 'lot2_exit_price': round(fill_price, 2),
                'lot2_exit_reason': reason, 'lot2_pnl_points': round(pnl_pts, 2), 'lot2_pnl_rs': pnl_rs,
            })
        save_state(self.state)
        # Final logged row for this lot's close, exit_reason stamped —
        # written here (status is still 'in_trade' at this point) rather
        # than relying on the next 1-min boundary, which for a trend-flip
        # close+reentry (rule 7) can land after _finalize_trade has already
        # reset self.state to 'watching', losing this trade's last row.
        self._append_running_row(fill_price, exit_reason=f'lot{lot_num}_{reason}')

        msg = (f'{"[PAPER] " if DRY_RUN else ""}✅ {tag}: Lot{lot_num} exit — {reason}\n'
              f'Entry {entry:.2f} -> Exit {fill_price:.2f} | P&L: {pnl_pts:+.2f} pts  Rs.{pnl_rs:+,.0f}')
        logger.info(msg.replace('\n', '  '))
        _slack(msg)

        if self.state.lot1_status != 'open' and self.state.lot2_status != 'open':
            self._finalize_trade()

    def _execute_exit_all(self, reason: str) -> bool:
        """
        stop_loss / trend_flip / slack_exit — closes whichever lot(s)
        remain open, at the SAME reason. §2 (Phase 3): no more eod_squareoff
        or teardown-triggered shutdown reasons — a position left open at
        session end is the normal case now, not something teardown exits.
        Returns True only if the position ended up fully closed (status
        back to 'watching').
        Callers that chain an action after this — specifically rule 7's
        same-tick trend-flip re-entry in _handle_new_15m_bar — MUST check
        this before proceeding: firing a fresh entry in the opposite
        direction while the exit itself failed to confirm would mean
        attempting to hold both directions at once against a position we
        already don't have a confirmed read on.
        """
        if self.state.status != 'in_trade':
            return True   # already flat -- vacuously true, not a failure
        ltp = self._get_ltp() or self.state.last_known_ltp or self.state.entry_price
        if self.state.lot1_status == 'open':
            self._execute_exit_lot(1, reason, ltp)
        if self.state.status == 'in_trade' and self.state.lot2_status == 'open':
            self._execute_exit_lot(2, reason, ltp)
        # No separate finalize call here -- _execute_exit_lot already
        # finalizes internally with the correct guard (both lot statuses
        # genuinely non-open). A second, looser "if still in_trade, finalize
        # anyway" check here was the exact same class of bug this whole fix
        # addresses: it force-closed the trade whenever status was still
        # 'in_trade' regardless of WHY (including both lots having just
        # failed to exit), the same blind trust in appearances that caused
        # the 2026-08-31 incident.
        return self.state.status != 'in_trade'

    def _finalize_trade(self) -> None:
        tag = _tag(self._contract['symbol_root'])
        lot1_pts = self._pending_trade_row.get('lot1_pnl_points', 0) or 0
        lot2_pts = self._pending_trade_row.get('lot2_pnl_points', 0) or 0
        lot1_rs = self._pending_trade_row.get('lot1_pnl_rs', 0) or 0
        lot2_rs = self._pending_trade_row.get('lot2_pnl_rs', 0) or 0
        self._pending_trade_row['total_pnl_points'] = round(lot1_pts + lot2_pts, 2)
        self._pending_trade_row['total_pnl_rs'] = round(lot1_rs + lot2_rs, 2)
        append_cumulative_trade(self._pending_trade_row)

        msg = (f'{"[PAPER] " if DRY_RUN else ""}\U0001f4ca {tag}: Trade #{self._trade_counter} closed. '
              f'Total P&L: {self._pending_trade_row["total_pnl_points"]:+.2f} pts  '
              f'Rs.{self._pending_trade_row["total_pnl_rs"]:+,.0f}')
        logger.info(msg)
        _slack(msg)

        # No feed.unsubscribe_options() here — the contract token stays
        # subscribed for the whole session regardless of trade state (see
        # get_fill_price_and_qty's docstring); unsubscribing it would have
        # silently killed LTP for rule 7's same-tick re-entry below.

        # Clear trade fields on state (matches iris.py's _execute_exit
        # cleanup) — a fresh entry always builds a new PrometheusState from
        # scratch regardless, so this is about keeping the CSV's watching-mode
        # rows from showing stale prior-trade data, not correctness.
        self.state = PrometheusState(status='watching')
        save_state(self.state)
        self._pending_trade_row = {}

    # -----------------------------------------------------------------------
    # Running trade log (§4) + periodic Slack update
    # -----------------------------------------------------------------------

    def _append_running_row(self, ltp: float, exit_reason: str = None) -> None:
        if self.state.status != 'in_trade' or not self.state.entry_ts:
            return
        entry_ts = datetime.fromisoformat(self.state.entry_ts)
        entry = self.state.entry_price
        direction = self.state.direction

        def _lot_pnl(target, status, exit_price, lots):
            if not lots:
                return 0.0, 0.0
            if status == 'booked' and exit_price is not None:
                pts = (exit_price - entry) if direction == 'bullish' else (entry - exit_price)
            else:
                pts = (ltp - entry) if direction == 'bullish' else (entry - ltp)
            return round(pts, 2), round(pts * lots * LOT_SIZE, 2)

        lot1_pts, lot1_rs = _lot_pnl(self.state.lot1_target, self.state.lot1_status,
                                     self.state.lot1_exit_price, self.state.lot1_lots)
        lot2_pts, lot2_rs = _lot_pnl(self.state.lot2_target, self.state.lot2_status,
                                     self.state.lot2_exit_price, self.state.lot2_lots)

        row = {
            'ts': datetime.now().isoformat(),
            'bars_since_entry': int((datetime.now() - entry_ts).total_seconds() // 60),
            'ltp': ltp, 'sl_price': self.state.sl_price,
            'lot1_target': self.state.lot1_target, 'lot2_target': self.state.lot2_target,
            'lot1_pnl_points': lot1_pts, 'lot1_pnl_rs': lot1_rs,
            'lot2_pnl_points': lot2_pts, 'lot2_pnl_rs': lot2_rs,
            'total_pnl_points': round(lot1_pts + lot2_pts, 2), 'total_pnl_rs': round(lot1_rs + lot2_rs, 2),
            'exit_reason': exit_reason,
        }
        append_trade_log_row(self._trade_counter, entry_ts, row)

    def _compute_trade_pnl(self, ltp: float) -> dict:
        """§13 (2026-09-04): realised (booked lots) + unrealised (still-open
        lots) + total — this is exactly what _finalize_trade/
        _append_running_row already compute per lot, just needed here
        un-conditioned on both lots being closed. Shared by _send_trade_update
        and _send_session_report's "still open" fallback so both report the
        same three numbers the same way.

        Real, currently-existing gap this fixes: once lot1 books, its P&L
        previously vanished from every subsequent update (both call sites
        computed P&L only from still-open lots, labeled "unrealised") — a
        trade mid-way through its scale-out silently understated its true
        total by exactly the already-booked amount. In Phase 2 this was a
        brief transitional window (position open at most one session); in
        Phase 3 a position can run for days to weeks, so it's the normal
        shape of a trade for most of its life, not an edge case.
        """
        entry = self.state.entry_price or 0
        direction = self.state.direction

        def _lot_realised(exit_price, status, lots):
            if status != 'booked' or exit_price is None or not lots:
                return 0.0, 0.0
            pts = (exit_price - entry) if direction == 'bullish' else (entry - exit_price)
            return pts, pts * lots * LOT_SIZE

        def _lot_unrealised(status, lots):
            if status != 'open' or not lots or not ltp:
                return 0.0, 0.0
            pts = (ltp - entry) if direction == 'bullish' else (entry - ltp)
            return pts, pts * lots * LOT_SIZE

        r1_pts, r1_rs = _lot_realised(self.state.lot1_exit_price, self.state.lot1_status, self.state.lot1_lots)
        r2_pts, r2_rs = _lot_realised(self.state.lot2_exit_price, self.state.lot2_status, self.state.lot2_lots)
        u1_pts, u1_rs = _lot_unrealised(self.state.lot1_status, self.state.lot1_lots)
        u2_pts, u2_rs = _lot_unrealised(self.state.lot2_status, self.state.lot2_lots)

        realised_rs, unrealised_rs = r1_rs + r2_rs, u1_rs + u2_rs
        return {
            'realised_pts': round(r1_pts + r2_pts, 2), 'realised_rs': round(realised_rs, 2),
            'unrealised_pts': round(u1_pts + u2_pts, 2), 'unrealised_rs': round(unrealised_rs, 2),
            'total_rs': round(realised_rs + unrealised_rs, 2),
        }

    def _send_trade_update(self) -> None:
        if self.state.status != 'in_trade':
            return
        ltp = self.state.last_known_ltp or 0
        entry = self.state.entry_price or 0
        direction = self.state.direction
        pnl = self._compute_trade_pnl(ltp)
        tag = _tag(self._contract['symbol_root'])
        prefix = '[PAPER] ' if DRY_RUN else ''
        msg = (f'{prefix}\U0001f4ca {tag} update: {direction.upper()}  {self.state.symbol}  '
              f'Entry: {entry:.2f}  LTP: {ltp:.2f}\n'
              f'Realised: {pnl["realised_pts"]:+.2f} pts (Rs.{pnl["realised_rs"]:+,.0f})  '
              f'Unrealised: {pnl["unrealised_pts"]:+.2f} pts (Rs.{pnl["unrealised_rs"]:+,.0f})  '
              f'Total: Rs.{pnl["total_rs"]:+,.0f}')
        logger.info(msg.replace(prefix, '').replace('\n', '  '))
        _slack(msg, SLACK_TRADE_UPDATES)


# ---------------------------------------------------------------------------
# Standalone login and entry point
# ---------------------------------------------------------------------------

def _login() -> tuple:
    import pyotp
    from SmartApi import SmartConnect

    creds = pd.read_csv(CREDS_FILE)
    row = creds.iloc[0]
    api_key = str(row['api_key'])
    client_code = str(row['user_name'])

    obj = SmartConnect(api_key=api_key)
    totp = pyotp.TOTP(str(row['qr_code'])).now()
    resp = obj.generateSession(client_code, str(row['password']), totp)
    if not resp.get('status'):
        raise RuntimeError(f'Angel One login failed: {resp}')

    auth_token = resp['data']['jwtToken']
    logger.info(f'Logged in as {client_code}')
    return obj, auth_token, api_key, client_code


def main():
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / 'trade_logs').mkdir(exist_ok=True)

    # §0: Prometheus's own daily gate against mcx_holidays.csv — distinct
    # from Leto's NSE/BSE check, and checked before login (a local-file
    # check needs no broker session) so a holiday costs nothing beyond
    # this one read.
    closed, reason = mcx_fully_closed_today()
    if closed:
        logger.info(f'MCX closed today ({reason}) — not starting.')
        print(f'MCX is closed today ({reason}). Not starting Prometheus.')
        sys.exit(0)

    if COMMAND_FLAG_PATH.exists() and COMMAND_FLAG_PATH.read_text().strip() == 'DISABLE':
        logger.info('Prometheus DISABLE flag set — not starting.')
        print('Prometheus is disabled via Slack. Clear the flag to resume.')
        sys.exit(0)

    ok, reason = check_no_active_strategies()
    if not ok:
        print(f'ERROR: Cannot start Prometheus — {reason}')
        print('Prometheus requires an exclusive Angel One session.')
        sys.exit(1)

    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            os.kill(old_pid, signal.SIGTERM)
            logger.info(f'Sent SIGTERM to existing Prometheus process (PID {old_pid}); waiting...')
            time.sleep(3)
        except (ProcessLookupError, ValueError):
            pass
        PID_FILE.unlink(missing_ok=True)

    PID_FILE.write_text(str(os.getpid()))
    FLAG_PATH.touch()
    logger.info('prometheus_active.flag created.')

    obj = None
    try:
        obj, auth_token, api_key, client_code = _login()
        prometheus = Prometheus(obj, auth_token, api_key, client_code)
        prometheus.run()
    except KeyboardInterrupt:
        logger.info('KeyboardInterrupt.')
    except Exception as e:
        logger.exception(f'Unhandled exception: {e}')
        _slack(f'\U0001f6a8 *Prometheus* crashed: {e}', SLACK_ERRORS_CHANNEL)
    finally:
        FLAG_PATH.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)
        if obj is not None:
            try:
                obj.terminateSession(str(pd.read_csv(CREDS_FILE).iloc[0]['user_name']))
            except Exception:
                pass
        logger.info('Session terminated.')


if __name__ == '__main__':
    main()
