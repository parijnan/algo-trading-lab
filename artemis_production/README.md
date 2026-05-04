# Artemis Production — Sensex Dynamic Iron Condor (Live Execution)

Live execution module for the Artemis strategy.
Part of the **Algo Trading Lab** project.

Deployed when India VIX < 16. Sells a weekly Sensex **Iron Condor** on Monday at 10:30 AM.
If the market trends and a side is tested, the strategy dynamically transforms into a **reinforced directional credit spread** by exiting the losing side and rolling/reinforcing the winning side. It manages the position through to Thursday expiry.

## Module structure

| File | Purpose |
|---|---|
| `artemis.py` | Entry point — `run(obj, instrument_df)` called by Leto |
| `iron_condor.py` | IronCondor class — trade lifecycle, monitoring, adjustment, archival |
| `credit_spread.py` | CreditSpread class — individual PE/CE spread execution and SL logic |
| `configs.py` | All parameters — loaded from `data/` files at import time |
| `functions.py` | Slack messaging, Telegram fallback, exception handling |

## Setup on delos

```bash
cd /home/parijnan/scripts/algo-trading-lab/artemis_production
# Ensure data/ symlinks are in place:
#   data/user_credentials.csv -> ../../data/user_credentials.csv
#   data/holidays.csv         -> ../../data/holidays.csv
# Verify data/contracts.csv and data/trade_settings.csv are current
# Artemis is launched via Leto — not run directly
```

## Session model

Artemis does not manage its own Angel One session. Leto owns login, market/holiday
checks, scrip master download, and session teardown. Artemis receives:
- `obj` — authenticated `SmartConnect` instance
- `instrument_df` — Sensex BFO rows filtered from the scrip master

`iron_condor.set_session(obj, instrument_df)` propagates both to the PE and CE
spread objects and handles lot sizing if `lot_calc = true` in `trade_settings.csv`.

## Trade state

State is split across three files in `data/`:

| File | Purpose |
|---|---|
| `pe_trade_params.csv` | Full PE spread state — one row, overwritten on every change |
| `ce_trade_params.csv` | Full CE spread state — one row, overwritten on every change |
| `trade_book.csv` | Append-only leg-level log (entry, adjustment, exit rows) |
| `trade_log.csv` | Periodic monitoring snapshots (index LTP, spread LTPs, P&L) |

`spread_status` values in the trade params files:

| Value | Meaning |
|---|---|
| `open` | Spread initialised, waiting for entry time |
| `active` | Spread live, original lot count only |
| `active_additional` | Spread live with additional lots |
| `adjusted` | Sell leg rolled, no additional lots |
| `adjusted_additional` | Sell leg rolled with additional lots |
| `adjusted_elm` | Post-ELM hedge adjustment, original lots |
| `adjusted_additional_elm` | Post-ELM hedge adjustment with additional lots |
| `active_additional_elm` | Additional lots exited for ELM |
| `closed` | Spread fully exited |

At week end, `_archive_trade()` renames all state files into `data/archived/`
prefixed with the expiry date, leaving `data/` clean for the next week.

## Key config parameters (`data/trade_settings.csv`)

| Parameter | Description |
|---|---|
| `lot_size` | Sensex lot size (currently 20) |
| `lot_count` | Fixed lot count when `lot_calc = false` |
| `lot_calc` | If true, size from available margin at session start |
| `lot_capital` | Capital per lot for auto-sizing (Rs) |
| `expected_premium` | Target sell premium for strike selection |
| `hedge_points` | Distance of buy leg from sell leg |
| `sl_0_dte` to `sl_4_dte` | Option SL multipliers by days to expiry |
| `adj_dist` | Strike adjustment distance on SL hit |
| `index_sl_offset` | Index SL offset from sell strike |
| `monitor_frequency` | Monitoring loop sleep interval (seconds) |

## Status

- [x] Iron condor execution — live and profitable
- [x] SL handling and spread adjustment — validated
- [x] ELM adjustment — validated
- [x] Trade archival — validated
- [x] Leto integration — session management moved to Leto