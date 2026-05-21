"""
artemis.py — Artemis Production Entry Point
Called by leto.py — not run directly.

Changes from original:
  - chdir removed — Leto sets cwd to artemis_production/ before importing
  - login() removed — Leto owns market/holiday checks and session
  - set_session(obj, instrument_df) receives authenticated session from Leto
  - logout() does not terminate the session — Leto calls terminateSession
"""

from iron_condor import IronCondor
from configs import opening_time, closing_time
from functions import handle_exception


def run(obj, auth_token, instrument_df):
    """
    Main Artemis execution. Called by leto.py with an authenticated
    SmartConnect object, JWT auth token, and the pre-filtered Sensex
    instrument DataFrame.
    """
    iron_condor = IronCondor()

    # Receive session from Leto
    iron_condor.set_session(obj, auth_token, instrument_df)

    # Trade entry block — executes only if spreads are not yet active
    iron_condor.execute_trade()

    # If both spreads are still 'open' after execute_trade() returns, the entry
    # window or VIX check failed — stand down cleanly without monitoring.
    if (iron_condor.pe_spread.spread_status == 'open' and
            iron_condor.ce_spread.spread_status == 'open'):
        iron_condor.logout()
        return True, None  # Hand back to Leto for re-routing

    # Trade monitoring loop
    while iron_condor.current_time > opening_time and iron_condor.current_time < closing_time:
        try:
            iron_condor._check_slack_commands()
            if not iron_condor.monitor_trade():
                break
            iron_condor.evaluate_adjust_for_elm()
            iron_condor.evaluate_handle_sl()
            iron_condor.continue_monitoring()
        except Exception as e:
            if "Session terminated" in str(e): raise
            handle_exception(e)
            continue

    # Build summary before logout so spread statuses are still live
    summary = iron_condor.get_session_summary()
    iron_condor.logout()
    return False, summary