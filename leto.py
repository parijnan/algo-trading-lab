"""
leto.py — Algo Trading Lab Session Manager and Strategy Router
Single cron entry point. Owns the full session lifecycle.

Responsibilities:
  - Login to Angel One (one session, one API key)
  - Market hours and holiday check — exit before any strategy is initialised
  - Scrip master download and filtering for Nifty (NFO) and Sensex (BFO)
  - VIX-based routing: Artemis (VIX <= 16), Athena (16 < VIX <= 25), Apollo (VIX > 25)
  - Re-routing loop: Supports strategy hand-back if VIX breaches at entry time
  - Session teardown (terminateSession) after strategy returns

Cron on delos:
    15 9 * * 1-5 cd /home/parijnan/scripts/algo-trading-lab && \
    /home/parijnan/anaconda3/bin/python leto.py >> logs/leto_$(date +%%Y%%m%%d).log 2>&1

Strategy interfaces:
  Artemis : artemis.run(obj, auth_token, instrument_df_sensex) — returns True for hand-back
  Athena  : athena_engine.Athena(obj, auth_token, instrument_df)  — returns True for hand-back
  Apollo  : apollo.Apollo(obj, auth_token, instrument_df)  — returns False (market close)
"""

import os
import sys
import logging
import pandas as pd
from io import StringIO
from datetime import datetime, time
from traceback import format_exc
from urllib.request import urlopen
from pyotp import TOTP
from time import sleep
from requests import post

from SmartApi import SmartConnect

# ---------------------------------------------------------------------------
# Repo root — all strategy directories derived from here
# ---------------------------------------------------------------------------
REPO_ROOT      = os.path.dirname(os.path.abspath(__file__))
APOLLO_DIR     = os.path.join(REPO_ROOT, "apollo_production")
ARTEMIS_DIR    = os.path.join(REPO_ROOT, "artemis_production")
ATHENA_DIR     = os.path.join(REPO_ROOT, "athena_production")
SHARED_DIR     = os.path.join(REPO_ROOT, "shared")
DATA_DIR       = os.path.join(REPO_ROOT, "data")
LOGS_DIR       = os.path.join(REPO_ROOT, "logs")

# ---------------------------------------------------------------------------
# Logging — Leto has its own logger, separate from strategy loggers
# ---------------------------------------------------------------------------
os.makedirs(LOGS_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger('leto')

# ---------------------------------------------------------------------------
# Credentials — loaded once from shared data/user_credentials.csv
# ---------------------------------------------------------------------------
_CREDS_FILE = os.path.join(DATA_DIR, 'user_credentials.csv')
_creds      = pd.read_csv(_CREDS_FILE).iloc[0]
api_key     = _creds['api_key']
user_name   = _creds['user_name']
password    = str(_creds['password'])
qr_code     = _creds['qr_code']
slack_token = _creds['slack_token']

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MARKET_OPEN  = time(9, 15)
MARKET_CLOSE = time(15, 30)

# Angel One scrip master
_SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# Index tokens for VIX routing check
_NIFTY_INDEX_TOKEN = "99926000"
_VIX_TOKEN         = "99926017"

# Slack channel for Leto-level messages
_SLACK_CHANNEL = "#tradebot-updates"


# ---------------------------------------------------------------------------
# Slack helper — Leto-level only, does not depend on strategy functions.py
# ---------------------------------------------------------------------------

def _slack(msg):
    """Send a Slack message. Fails silently — never crashes Leto."""
    try:
        post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {slack_token}",
                "Content-Type":  "application/json",
            },
            json={"channel": _SLACK_CHANNEL, "text": msg},
            timeout=5,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Error log helper
# ---------------------------------------------------------------------------

def _write_error_log(msg):
    log_path = os.path.join(DATA_DIR, 'leto_error_log.txt')
    try:
        with open(log_path, 'a') as f:
            f.write(msg + '\n')
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Circuit Breaker Check
# ---------------------------------------------------------------------------

def _check_circuit_breaker():
    """
    Check for the presence of a persistent Slack command flag.
    If EXIT, KILL, or DISABLE is active, abort Leto immediately.
    """
    flag_path = os.path.join(DATA_DIR, 'SLACK_COMMAND.flag')
    if os.path.exists(flag_path):
        try:
            with open(flag_path, 'r') as f:
                command = f.read().strip()
            
            if command in ["EXIT", "KILL", "DISABLE"]:
                msg = f"⛔ *Leto*: Circuit Breaker active (Flag: _{command}_). Standing down. Send `Clear Flag` to resume."
                logger.info(msg.replace('*', ''))
                _slack(msg)
                sys.exit(0)
        except Exception as e:
            logger.error(f"Error reading circuit breaker flag: {e}")

# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def _login():
    """
    Authenticate with Angel One. Returns (SmartConnect obj, auth_token, login_data).
    Retries on failure — same pattern as strategies.
    """
    logger.info(f"Logging in as {user_name}.")
    obj = SmartConnect(api_key=api_key)
    while True:
        try:
            totp = TOTP(qr_code).now()
            data = obj.generateSession(user_name, password, totp)
            break
        except Exception as e:
            msg = f"Login failed: {e}\n{format_exc()}"
            logger.error(msg)
            _write_error_log(msg)
            sleep(1)

    auth_token = data['data']['jwtToken']
    logger.info(f"Logged in successfully at {datetime.now():%Y-%m-%d %H:%M:%S}.")
    _slack(f"*Leto*: Logged in at {datetime.now():%Y-%m-%d %H:%M:%S}.")
    return obj, auth_token


# ---------------------------------------------------------------------------
# Market hours and holiday check
# ---------------------------------------------------------------------------

def _check_market(obj):
    """
    Exit the process if market is closed or today is a holiday.
    Called immediately after login — before any strategy is loaded.
    """
    now = datetime.now()

    if now.time() < MARKET_OPEN or now.time() > MARKET_CLOSE:
        msg = f"Market is closed. Exiting at {now:%Y-%m-%d %H:%M:%S}."
        logger.info(msg)
        _slack(f"*Leto*: {msg}")
        obj.terminateSession(user_name)
        sys.exit(0)

    holidays_file = os.path.join(DATA_DIR, 'holidays.csv')
    if os.path.exists(holidays_file):
        holidays_df = pd.read_csv(holidays_file, parse_dates=['date'])
        holidays    = set(pd.to_datetime(holidays_df['date']).dt.date)
        if now.date() in holidays:
            holiday_name = holidays_df.loc[
                holidays_df['date'].dt.date == now.date(), 'holiday'
            ].iloc[0]
            msg = f"Market holiday today ({holiday_name}). Exiting."
            logger.info(msg)
            _slack(f"*Leto*: {msg}")
            obj.terminateSession(user_name)
            sys.exit(0)
    else:
        logger.warning("holidays.csv not found. No holiday check applied.")


# ---------------------------------------------------------------------------
# Scrip master
# ---------------------------------------------------------------------------

def _download_scrip_master():
    """Download Angel One scrip master and return filtered DataFrames."""
    logger.info("Downloading scrip master...")
    scrip_df = pd.read_json(StringIO(urlopen(_SCRIP_MASTER_URL).read().decode()))
    logger.info(f"Scrip master downloaded: {len(scrip_df):,} rows.")

    instrument_df_nifty = scrip_df[
        (scrip_df['exch_seg'] == 'NFO') &
        (scrip_df['name'] == 'NIFTY')
    ].copy()

    instrument_df_sensex = scrip_df[
        (scrip_df['exch_seg'] == 'BFO') &
        (scrip_df['name'] == 'SENSEX')
    ].copy()

    logger.info(
        f"Nifty NFO rows: {len(instrument_df_nifty)}. "
        f"Sensex BFO rows: {len(instrument_df_sensex)}.")

    return instrument_df_nifty, instrument_df_sensex


# ---------------------------------------------------------------------------
# VIX routing
# ---------------------------------------------------------------------------

def _get_vix(obj):
    """
    Fetch current India VIX via REST ltpData.
    Returns float, or None on failure.
    """
    try:
        ltp = obj.ltpData("NSE", "India VIX", _VIX_TOKEN)['data']['ltp']
        return float(ltp)
    except Exception as e:
        logger.error(f"VIX fetch failed: {e}")
        return None


def _apollo_trade_open():
    """
    Return True if apollo_state.csv records an active or exiting trade.
    Used to force Apollo routing when VIX has dropped below threshold overnight
    but an open Apollo position still needs to be managed.
    """
    state_file = os.path.join(APOLLO_DIR, 'data', 'apollo_state.csv')
    if not os.path.exists(state_file):
        return False
    try:
        df = pd.read_csv(state_file)
        if df.empty:
            return False
        return str(df.iloc[0].get('status', 'idle')) in ('in_trade', 'exiting')
    except Exception as e:
        logger.error(f"Could not read Apollo state file: {e}")
        return False


def _artemis_trade_open():
    """
    Return True if either pe_trade_params.csv or ce_trade_params.csv records
    an active Artemis position (spread_status not 'open' or 'closed').
    Used to force Artemis routing when VIX has risen above threshold overnight
    but an open Artemis position still needs to be managed.
    """
    active_statuses = {
        'active', 'active_additional', 'adjusted', 'adjusted_additional',
        'adjusted_elm', 'adjusted_additional_elm', 'active_additional_elm',
        'active_elm',
    }
    for filename in ('pe_trade_params.csv', 'ce_trade_params.csv'):
        filepath = os.path.join(ARTEMIS_DIR, 'data', filename)
        if not os.path.exists(filepath):
            continue
        try:
            df = pd.read_csv(filepath)
            if df.empty:
                continue
            if str(df.iloc[0].get('spread_status', 'closed')) in active_statuses:
                return True
        except Exception as e:
            logger.error(f"Could not read Artemis state file {filename}: {e}")
    return False


def _athena_trade_open():
    """
    Return True if athena_state.csv records an active trade.
    """
    state_file = os.path.join(ATHENA_DIR, 'data', 'athena_state.csv')
    if not os.path.exists(state_file):
        return False
    try:
        df = pd.read_csv(state_file)
        if df.empty:
            return False
        return str(df.iloc[0].get('status', 'idle')) in ('in_trade', 'exiting')
    except Exception as e:
        logger.error(f"Could not read Athena state file: {e}")
        return False


# ---------------------------------------------------------------------------
# VIX Thresholds for Routing
# ---------------------------------------------------------------------------
VIX_ARTEMIS_MAX = 16.0
VIX_ATHENA_MAX  = 25.0


def _route(obj, auth_token, instrument_df_nifty, instrument_df_sensex):
    """
    Decide which strategy to run, then run it.
    Returns (should_reroute: bool, summary: dict | None).
    """
    is_friday = datetime.now().weekday() == 4

    # Priority 1: resume open positions unconditionally
    if _apollo_trade_open():
        logger.info("Open Apollo trade detected. Routing to Apollo.")
        _slack("*Leto*: Open Apollo trade detected. Routing to Apollo.")
        _, summary = _run_apollo(obj, auth_token, instrument_df_nifty)
        return False, summary  # No re-routing if position open

    if _athena_trade_open():
        logger.info("Open Athena trade detected. Routing to Athena.")
        _slack("*Leto*: Open Athena trade detected. Routing to Athena.")
        handoff, summary = _run_athena(obj, auth_token, instrument_df_nifty)
        return handoff, summary

    if _artemis_trade_open():
        logger.info(f"Open Artemis trade detected. Routing to Artemis {'(Friday)' if is_friday else ''}.")
        _slack(f"*Leto*: Open Artemis trade detected. Routing to Artemis {'(Friday)' if is_friday else ''}.")
        handoff, summary = _run_artemis(obj, auth_token, instrument_df_sensex)
        return handoff, summary

    # Priority 2: no open positions — route on current VIX
    vix = _get_vix(obj)
    if vix is None:
        if is_friday:
            logger.info("Friday and no open positions — standing down.")
            _slack("*Leto*: Friday, no open positions. Standing down.")
            return False, None
        logger.warning("Could not fetch VIX. Defaulting to Artemis.")
        _slack("*Leto* ALERT: Could not fetch VIX. Defaulting to Artemis.")
        vix = 0.0

    if is_friday:
        # Check if Athena has FORCE_ENTRY enabled for dry run
        force_athena = False
        try:
            if ATHENA_DIR not in sys.path: sys.path.insert(0, ATHENA_DIR)
            from configs_live import FORCE_ENTRY as ATHENA_FORCE # type: ignore
            force_athena = ATHENA_FORCE
        except Exception:
            pass

        # Friday: Artemis/Athena do not enter fresh unless forced.
        if vix > VIX_ATHENA_MAX:
            logger.info(f"Friday. VIX {vix:.2f} > {VIX_ATHENA_MAX}. Routing to Apollo.")
            _slack(f"*Leto*: Friday. VIX {vix:.2f} > {VIX_ATHENA_MAX}. Routing to Apollo.")
            _, summary = _run_apollo(obj, auth_token, instrument_df_nifty)
            return False, summary
        elif force_athena and vix > VIX_ARTEMIS_MAX:
            logger.info(f"Friday (FORCED). VIX {vix:.2f} in (16, 25]. Routing to Athena.")
            _slack(f"*Leto*: Friday (FORCED). VIX {vix:.2f}. Routing to *Athena*.")
            _, summary = _run_athena(obj, auth_token, instrument_df_nifty)
            return False, summary
        else:
            logger.info(f"Friday. VIX {vix:.2f} <= {VIX_ATHENA_MAX}. Standing down.")
            _slack(f"*Leto*: Friday. VIX {vix:.2f}. No fresh entries today.")
            return False, None

    # Priority 3: Mon–Thu, no open positions — 3-way route
    if vix <= VIX_ARTEMIS_MAX:
        logger.info(f"VIX {vix:.2f} <= {VIX_ARTEMIS_MAX}. Routing to Artemis.")
        _slack(f"*Leto*: VIX {vix:.2f}. Routing to *Artemis*.")
        handoff, summary = _run_artemis(obj, auth_token, instrument_df_sensex)
        return handoff, summary
    elif vix <= VIX_ATHENA_MAX:
        logger.info(f"VIX {vix:.2f} in (16, 25]. Routing to Athena.")
        _slack(f"*Leto*: VIX {vix:.2f}. Routing to *Athena*.")
        handoff, summary = _run_athena(obj, auth_token, instrument_df_nifty)
        return handoff, summary
    else:
        logger.info(f"VIX {vix:.2f} > {VIX_ATHENA_MAX}. Routing to Apollo.")
        _slack(f"*Leto*: VIX {vix:.2f}. Routing to *Apollo*.")
        _, summary = _run_apollo(obj, auth_token, instrument_df_nifty)
        return False, summary


# ---------------------------------------------------------------------------
# Strategy runners
# ---------------------------------------------------------------------------

def _run_apollo(obj, auth_token, instrument_df_nifty):
    """Run Apollo. Returns (handback: bool, summary: dict)."""
    logger.info("Starting Apollo.")
    if APOLLO_DIR not in sys.path:
        sys.path.insert(0, APOLLO_DIR)
    from apollo import Apollo  # type: ignore
    apollo = Apollo(obj, auth_token, instrument_df_nifty)
    handoff, summary = apollo.run()
    logger.info(f"Apollo returned. Handoff signal: {handoff}")
    return bool(handoff), summary


def _run_athena(obj, auth_token, instrument_df_nifty):
    """Run Athena. Returns (handback: bool, summary: dict)."""
    logger.info("Starting Athena.")
    if ATHENA_DIR not in sys.path:
        sys.path.insert(0, ATHENA_DIR)
    import athena_engine  # type: ignore
    engine = athena_engine.Athena(obj, auth_token, instrument_df_nifty)
    handoff, summary = engine.run()
    logger.info(f"Athena returned. Handoff signal: {handoff}")
    return bool(handoff), summary


def _run_artemis(obj, auth_token, instrument_df_sensex):
    """Run Artemis. Returns (handback: bool, summary: dict | None)."""
    logger.info("Starting Artemis.")
    os.chdir(ARTEMIS_DIR)
    if ARTEMIS_DIR not in sys.path:
        sys.path.insert(0, ARTEMIS_DIR)
    import artemis  # type: ignore
    handoff, summary = artemis.run(obj, auth_token, instrument_df_sensex)
    os.chdir(REPO_ROOT)
    logger.info(f"Artemis returned. Handoff signal: {handoff}")
    return bool(handoff), summary


# ---------------------------------------------------------------------------
# Session report
# ---------------------------------------------------------------------------

_STRATEGY_SUBTITLE = {
    'Apollo':  'Nifty Debit Spread',
    'Athena':  'Nifty Double Calendar',
    'Artemis': 'Sensex Iron Condor',
}


def _send_session_report(summaries, session_date):
    """Format and post the end-of-day session summary to Slack."""
    day_str = session_date.strftime('%a %d %b %Y')
    lines   = [f"📊 *Algo Trading Lab — Session Report*  |  {day_str}", ""]

    total_rs  = 0.0
    any_trade = False

    for s in summaries:
        strategy = s.get('strategy', '?')
        subtitle = _STRATEGY_SUBTITLE.get(strategy, '')
        header   = f"*{strategy}*" + (f"  ·  _{subtitle}_" if subtitle else "")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(header)

        if not s.get('traded'):
            reason = s.get('no_trade_reason', 'No signal')
            lines.append(f"  ↳ No trade today — {reason}")
            lines.append("")
            continue

        any_trade = True
        lots       = s.get('lots', '?')
        entry_time = s.get('entry_time') or '?'
        exit_time  = s.get('exit_time')  or '—'
        exit_raw   = s.get('exit_reason', '')
        exit_str   = exit_raw.replace('_', ' ').title() if exit_raw else '?'
        pnl_pts    = s.get('pnl_pts')
        pnl_rs     = s.get('pnl_rs', 0) or 0
        peak       = s.get('peak_pnl_pts')
        total_rs  += pnl_rs

        if strategy == 'Apollo':
            direction = s.get('direction', '?').capitalize()
            lines.append(f"  ↳ Direction  : {direction}  |  Lots: {lots}")
        elif strategy == 'Athena':
            sp_in  = s.get('spot_entry')
            sp_out = s.get('spot_exit')
            if sp_in and sp_out:
                delta  = sp_out - sp_in
                lines.append(
                    f"  ↳ Spot move  : {sp_in:,.2f} → {sp_out:,.2f}  ({delta:+.0f} pts)"
                    f"  |  Lots: {lots}")
            else:
                lines.append(f"  ↳ Lots: {lots}")
        elif strategy == 'Artemis':
            outcome = s.get('outcome', 'Neutral')
            lines.append(f"  ↳ Outcome    : {outcome}  |  Lots: {lots}")

        lines.append(f"  ↳ Entry: {entry_time}   Exit: {exit_time}  ·  {exit_str}")

        if pnl_pts is not None:
            lines.append(f"  ↳ P&L        : *{pnl_pts:+.1f} pts  ({pnl_rs:+,.0f} Rs)*")
        else:
            lines.append(f"  ↳ P&L        : *{pnl_rs:+,.0f} Rs*")

        if peak is not None:
            lines.append(f"  ↳ Peak       : {peak:+.1f} pts")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if any_trade or len(summaries) > 1:
        lines.append(f"*Session Total  :  {total_rs:+,.0f} Rs*")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    _slack('\n'.join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    logger.info("=== Leto starting ===")
    _check_circuit_breaker()
    obj           = None
    auth_token    = None
    all_summaries = []

    try:
        obj, auth_token = _login()
        _check_market(obj)

        instrument_df_nifty, instrument_df_sensex = _download_scrip_master()

        # Re-routing loop: allows strategies to hand back control if VIX breaches at entry
        while True:
            should_reroute, summary = _route(
                obj, auth_token, instrument_df_nifty, instrument_df_sensex)
            if summary:
                all_summaries.append(summary)
            if not should_reroute:
                break

            logger.info("Strategy handed back control. Re-evaluating routing...")
            _slack("*Leto*: Strategy returned control. Re-evaluating routing based on new VIX...")
            sleep(5) # breather before re-fetch

    except SystemExit:
        # sys.exit() from _check_market — session already terminated there
        raise

    except Exception as e:
        msg = (
            f"Leto unhandled exception at {datetime.now():%Y-%m-%d %H:%M:%S}: "
            f"{e}\n{format_exc()}"
        )
        logger.error(msg)
        _slack(f"*Leto* ERROR: {e} — check logs.")
        _write_error_log(msg)

    finally:
        # Always terminate session if obj exists and we didn't already exit
        if obj is not None:
            try:
                obj.terminateSession(user_name)
                logger.info(
                    f"Session terminated at {datetime.now():%Y-%m-%d %H:%M:%S}.")
                _slack(
                    f"*Leto*: Session terminated at "
                    f"{datetime.now():%Y-%m-%d %H:%M:%S}.")
            except Exception as e:
                logger.error(f"terminateSession failed: {e}")

        if all_summaries:
            try:
                _send_session_report(all_summaries, datetime.now().date())
            except Exception as e:
                logger.error(f"Session report failed: {e}")

    logger.info("=== Leto complete ===")