"""
artemis.py — Artemis Production Entry Point
Called by wrapper.py — not run directly.

Changes from original:
  - chdir removed — wrapper sets cwd to artemis_production/ before importing
  - login() removed — wrapper owns market/holiday checks and session
  - set_session(obj, instrument_df) receives authenticated session from wrapper
  - logout() does not terminate the session — wrapper calls terminateSession
"""

from iron_condor import IronCondor
from configs import opening_time, closing_time
from functions import handle_exception


def run(obj, instrument_df):
    """
    Main Artemis execution. Called by wrapper.py with an authenticated
    SmartConnect object and the pre-filtered Sensex instrument DataFrame.
    """
    iron_condor = IronCondor()

    # Receive session from wrapper
    iron_condor.set_session(obj, instrument_df)

    # Trade entry block — executes only if spreads are not yet active
    iron_condor.execute_trade()

    # Trade monitoring loop
    while iron_condor.current_time > opening_time and iron_condor.current_time < closing_time:
        try:
            if not iron_condor.monitor_trade():
                break
            iron_condor.evaluate_adjust_for_elm()
            iron_condor.evaluate_handle_sl()
            iron_condor.continue_monitoring()
        except Exception as e:
            handle_exception(e)
            continue

    # Final update and archive (no session termination)
    iron_condor.logout()