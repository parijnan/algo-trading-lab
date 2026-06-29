"""
Leto integrated backtest simulator.

Iterates every trading day from BACKTEST_START to present and applies
the production routing logic to produce a consolidated trade log.

Era A (before ERA_SPLIT_DATE):
  - Artemis entry day: days in the Nifty contracts.csv entry set (Mondays)
  - Athena entry day:  days in the Athena trade summary entry-date set (Wednesdays,
                       holiday-adjusted)
  - Two checkpoints per week; Iris on any day with VIX > 25

Era B (ERA_SPLIT_DATE onward):
  - Artemis entry day: days in the Sensex contracts.csv entry set (Mondays)
  - Athena entry day:  days in the Athena trade summary entry-date set (Mondays,
                       as Nifty expiry moved to Tuesday)
  - Single Monday checkpoint for both; Iris on any day with VIX > 25

Active trade blocks all new entries until the trade's exit_ts passes 10:30.
Iris is intraday (exits same day); its slot clears at end of day.
"""

import logging
from datetime import time as dtime
import pandas as pd
from configs import (
    BACKTEST_START, ERA_SPLIT_DATE,
    VIX_SNAP_HOUR, VIX_SNAP_MINUTE,
)
from router import route, get_routing_vix

logger = logging.getLogger(__name__)

_SNAP_TIME = dtime(VIX_SNAP_HOUR, VIX_SNAP_MINUTE)
_ERA_SPLIT = pd.Timestamp(ERA_SPLIT_DATE).date()
_START = pd.Timestamp(BACKTEST_START).date()


def _lookup_trade(trades_df: pd.DataFrame, entry_date) -> dict | None:
    """Return the trade dict for entry_date, or None if not found."""
    mask = trades_df['entry_date'] == entry_date
    hits = trades_df[mask]
    if hits.empty:
        return None
    row = hits.iloc[0]
    return row.to_dict()


def _lookup_iris(iris_df: pd.DataFrame, date) -> dict | None:
    """Return the first Iris trade for date (signal already sorted by entry_ts)."""
    mask = iris_df['entry_date'] == date
    hits = iris_df[mask]
    if hits.empty:
        return None
    row = hits.sort_values('entry_ts').iloc[0]
    return row.to_dict()


def _is_slot_free(active_trade: dict | None, today) -> bool:
    """
    True when no active trade blocks a new entry at 10:30 today.
    A trade blocks entry if:
      - its exit_date > today (still running), OR
      - its exit_date == today AND exit_ts.time() >= _SNAP_TIME (exits after routing)
    """
    if active_trade is None:
        return True
    exit_date = active_trade['exit_date']
    if exit_date < today:
        return True
    if exit_date == today:
        exit_time = pd.Timestamp(active_trade['exit_ts']).time()
        return exit_time < _SNAP_TIME
    return False


def _log_row(day, week_start, routing_outcome, strategy=None, instrument=None,
             trade=None, vix=None) -> dict:
    if trade:
        return {
            'week_start':      week_start,
            'entry_date':      trade['entry_date'],
            'entry_ts':        trade['entry_ts'],
            'exit_ts':         trade['exit_ts'],
            'strategy':        trade['strategy'].split('_')[0],  # 'artemis'/'athena'/'iris'
            'instrument':      instrument,
            'vix_at_entry':    vix,
            'pl_rs':           trade['pl_rs'],
            'exit_reason':     trade['exit_reason'],
            'routing_outcome': routing_outcome,
        }
    return {
        'week_start':      week_start,
        'entry_date':      day,
        'entry_ts':        None,
        'exit_ts':         None,
        'strategy':        strategy,
        'instrument':      instrument,
        'vix_at_entry':    vix,
        'pl_rs':           None,
        'exit_reason':     None,
        'routing_outcome': routing_outcome,
    }


def _week_start(date) -> object:
    d = pd.Timestamp(date)
    return (d - pd.Timedelta(days=d.weekday())).date()


def run(
    vix_df: pd.DataFrame,
    artemis_df: pd.DataFrame,
    athena_df: pd.DataFrame,
    iris_df: pd.DataFrame,
    artemis_nifty_entry_dates: set,
    artemis_sensex_entry_dates: set,
    athena_entry_dates: set,
) -> pd.DataFrame:
    """
    Main simulation loop.  Returns a DataFrame of all routing decisions.
    Only 'entered' rows carry P&L; all other outcomes have pl_rs=None.
    """
    all_days = sorted(vix_df['_date'].unique())
    active_trade: dict | None = None
    records = []

    # Sort Iris by entry_ts so _lookup_iris always gets the first signal
    iris_df = iris_df.sort_values('entry_ts').reset_index(drop=True)

    for day in all_days:
        if day < _START:
            continue

        ws = _week_start(day)
        is_era_b = (day >= _ERA_SPLIT)

        # Determine applicable entry-day sets
        artemis_dates = artemis_sensex_entry_dates if is_era_b else artemis_nifty_entry_dates

        # Resolve active trade slot
        if not _is_slot_free(active_trade, day):
            continue   # active trade occupies the slot; no logging needed
        else:
            active_trade = None  # cleared

        # VIX snap at 10:30
        vix = get_routing_vix(day, vix_df)
        if vix is None:
            logger.debug('VIX missing on %s', day)
            records.append(_log_row(day, ws, 'vix_data_missing'))
            continue

        routed = route(vix)

        # ── Artemis checkpoint ───────────────────────────────────────────────
        if day in artemis_dates:
            if routed == 'artemis':
                instrument = 'sensex' if is_era_b else 'nifty'
                trade = _lookup_trade(artemis_df, day)
                if trade:
                    active_trade = trade
                    records.append(_log_row(day, ws, 'entered', instrument=instrument,
                                            trade=trade, vix=vix))
                else:
                    records.append(_log_row(day, ws, 'vix_routed_no_trade',
                                            strategy='artemis', instrument=instrument, vix=vix))
                continue   # Artemis day handled; skip Athena + Iris this iteration

            # VIX not in Artemis range — fall through to Athena check if Era B
            # (in Era B both Artemis and Athena share Monday; in Era A Monday is
            # Artemis-only, so Athena check only fires on Wednesday)

        # ── Athena checkpoint ────────────────────────────────────────────────
        if day in athena_entry_dates:
            if routed == 'athena':
                trade = _lookup_trade(athena_df, day)
                if trade:
                    active_trade = trade
                    records.append(_log_row(day, ws, 'entered', instrument='nifty',
                                            trade=trade, vix=vix))
                else:
                    records.append(_log_row(day, ws, 'vix_routed_no_trade',
                                            strategy='athena', instrument='nifty', vix=vix))
                continue

            # If routed == 'iris' on an Athena day, fall through to Iris block
            if routed == 'artemis':
                # VIX dropped below 16 since the Artemis-day Monday check didn't fire
                # (only possible in Era A where Monday had VIX >= 16 but Wednesday VIX < 16)
                records.append(_log_row(day, ws, 'skipped_no_signal',
                                        strategy='athena', instrument='nifty', vix=vix))
                continue

        # ── Iris check (any day, VIX > 25) ──────────────────────────────────
        if routed == 'iris':
            trade = _lookup_iris(iris_df, day)
            if trade:
                active_trade = trade
                records.append(_log_row(day, ws, 'entered', instrument='nifty',
                                        trade=trade, vix=vix))
            else:
                records.append(_log_row(day, ws, 'skipped_no_signal',
                                        strategy='iris', instrument='nifty', vix=vix))

    return pd.DataFrame(records)
