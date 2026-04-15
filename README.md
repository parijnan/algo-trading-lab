# Algo Trading Lab

A personal algorithmic trading laboratory for backtesting, optimising, and automating strategies for Indian Index Options.

## Strategies

### [Artemis](./artemis_production/) — Sensex Iron Condor
A market-neutral credit spread strategy trading a weekly Sensex Iron Condor, running Monday to Thursday. Sells OTM options on both sides and buys further OTM options as hedges. Deployed when India VIX < 16.

| | |
|---|---|
| Instrument | Sensex weekly options |
| Structure | Iron Condor (PE spread + CE spread) |
| Entry | Monday 10:30 AM |
| Expiry | Thursday |
| Broker | Angel Broking (SmartConnect) |
| Status | Live |

### [Apollo](./apollo_production/) — Nifty High-VIX Trend Following
A directional ITM debit spread strategy deployed when India VIX > 16. Uses dual-timeframe Supertrend (75-min and 15-min) to identify and trade sustained directional moves in Nifty options.

| | |
|---|---|
| Instrument | Nifty weekly options |
| Structure | ITM debit spread (directional, one side only) |
| Signal | Dual Supertrend — 75-min regime, 15-min entry/exit |
| Deploy condition | India VIX > 16 |
| Broker | Angel Broking (SmartConnect) |
| Production config | D-R-D06g |
| Status | Live |

## Session Router

### [Leto](./leto.py) — Strategy Router and Session Manager
Single cron entry point. Logs in to Angel One, checks market hours and holidays, downloads the scrip master, reads VIX, and routes to Apollo or Artemis. Owns the full session lifecycle — `generateSession` and `terminateSession` are called exactly once per day, here.

**Routing logic:**
1. If an active Apollo trade is found in `apollo_state.csv` — route to Apollo regardless of VIX (protects overnight positions when VIX drops below threshold)
2. Otherwise: VIX > 16 → Apollo, VIX ≤ 16 → Artemis
3. If VIX fetch fails — default to Artemis

## Infrastructure

| Component | Details |
|---|---|
| VPS | Linode Nanode — hostname `delos` |
| OS | Ubuntu 24.04 |
| Laptop | Garuda Linux (Arch-based) |
| Broker (live) | Angel Broking (SmartConnect API) |
| Broker (data) | ICICI Direct (Breeze) for Nifty, Angel Broking for Sensex |
| Notifications | Slack |
| Language | Python |

## VIX Regime

| VIX Level | Strategy |
|---|---|
| < 14 | Artemis — full confidence |
| 14 – 16 | Artemis — reduced size, tighter SLs |
| > 16 | Apollo |

## Cron (delos)

Single cron entry replaces all previous per-strategy crons:

```
14 9 * * 1-5 cd /home/parijnan/scripts/algo-trading-lab && /home/parijnan/anaconda3/bin/python leto.py >> logs/leto_$(date +\%Y\%m\%d).log 2>&1
```

## Data Pipeline
Historical 1-minute OHLCV data for Nifty and Sensex options and indices is maintained by an automated pipeline. See `data_pipeline/` for scripts and config.

| Data | Source | Schedule | Coverage |
|---|---|---|---|
| Sensex options + all indices | Angel Broking — VPS cron via `run_sensex_downloader.sh` | Daily at 15:45 | Mid-2024 onwards |
| Nifty options | ICICI Breeze — laptop cron via `run_nifty_downloader.sh` | Tuesdays at 23:30 | May 2019 onwards |

### Pipeline design
- 1-minute OHLCV data, saved as CSV, organised by expiry date
- Sensex and Nifty options: one file per contract (`{strike}{ce|pe}.csv`), one folder per expiry (`YYYY-MM-DD/`)
- Index files: single rolling CSV per index (`sensex.csv`, `nifty.csv`, `india_vix.csv`)
- Incremental saves — each file is written after every 2-day chunk, no data loss on interruption
- Resume on restart — picks up from the last saved timestamp in each file
- Sliding-window rate limiter enforcing broker API limits (AngelOne: 2/sec, 180/min, 5000/hr; Breeze: 100/min, 5000/day)
- Slack notifications on completion (`#data-alerts`) and fatal errors (`#error-alerts`)
- `download_status` flag in config CSVs tracks which expiries are fully downloaded

### Timestamp formats
| File type | Format |
|-----------|--------|
| Index files | `YYYY-MM-DD HH:MM:SS+05:30` |
| Sensex options files | `YYYY-MM-DDTHH:MM:SS+05:30` |
| Nifty options files | As returned by Breeze API |

### AngelOne API
- `getCandleData()` — max 1000 records per call; 2 trading days per chunk (375 min × 2 = 750 records)
- Exchange codes: `BFO` (Sensex options), `BSE` (Sensex index), `NSE` (Nifty / India VIX)
- Options identified by token from `instrument_master.csv` (auto-refreshed daily)
- Strike prices stored as strike × 100 in instrument master (e.g. `8700000` = 87000)
- Expiry dates stored as `DDMMMYYYY` in instrument master (e.g. `24SEP2026`)
- Broker returns random dates when no data exists — window guard discards out-of-range rows
- Data retained on broker servers for ~1-2 weeks post-expiry — daily cron ensures same-day capture

### ICICI Breeze API
- `get_historical_data()` — no per-call record limit; full date range in a single call
- Contracts identified by strike price, right (call/put), and expiry date — no token lookup
- Data retained for a rolling 3-year window
- Session authentication requires Selenium (headless Chrome) — runs on laptop only

### Data storage
Data files are not tracked by git. On each machine, a `data/` directory sits alongside the pipeline scripts:

**VPS** (`/home/parijnan/scripts/algo-trading-lab/data_pipeline/data/`):
```
data/
├── user_credentials_angel.csv    # not in git
├── instrument_master.csv         # not in git — auto-refreshed daily
├── indices/
│   ├── sensex.csv
│   ├── nifty.csv
│   └── india_vix.csv
└── sensex/
    └── YYYY-MM-DD/
        ├── 78000ce.csv
        └── 78000pe.csv
```

**Laptop** (`/home/parijnan/scripts/algo-trading-lab/data_pipeline/data/`):
```
data/
├── user_credentials_icici.csv    # not in git
├── indices/                      # synced from VPS via sync_data.sh
├── sensex/                       # synced from VPS via sync_data.sh
└── nifty/
    └── options/
        └── YYYY-MM-DD/
            ├── 23000ce.csv
            └── 23000pe.csv
```

---

## Repository Structure

```
algo-trading-lab/
├── README.md
├── .gitignore
├── leto.py                         # Session router and strategy entry point
├── data/                           # Shared runtime data (credentials, holidays)
│   ├── user_credentials.csv        # not in git
│   └── holidays.csv
├── logs/                           # Leto session logs — gitignored, created at runtime
├── artemis_production/             # Live Sensex iron condor strategy
│   ├── README.md
│   ├── artemis.py
│   ├── iron_condor.py
│   ├── credit_spread.py
│   ├── configs.py
│   ├── functions.py
│   └── data/
│       ├── contracts.csv
│       ├── trade_settings.csv
│       ├── user_credentials.csv    # symlink → ../data/user_credentials.csv
│       ├── holidays.csv            # symlink → ../data/holidays.csv
│       └── archived/
├── artemis_backtest/               # Artemis historical backtesting and optimisation
│   ├── README.md
│   ├── configs.py
│   ├── generate_contracts.py
│   ├── contracts.csv               (generated by generate_contracts.py)
│   ├── backtest.py
│   ├── data_loader.py
│   └── data/
│       ├── trade_summary.csv       (generated by backtest.py)
│       └── trade_logs/             (generated by backtest.py)
├── apollo_production/              # Live Nifty debit spread strategy
│   ├── README.md
│   ├── configs_live.py
│   ├── apollo.py
│   ├── supertrend.py
│   ├── state.py
│   ├── websocket_feed.py
│   ├── functions.py
│   ├── logger_setup.py
│   ├── technical_indicators.py
│   ├── tests/
│   │   └── ws_test.py
│   ├── data/
│   │   ├── user_credentials.csv    # symlink → ../data/user_credentials.csv
│   │   ├── holidays.csv            # symlink → ../data/holidays.csv
│   │   └── .gitkeep               # runtime data gitignored
│   └── logs/                       # gitignored, created at runtime
├── apollo_backtest/                # Apollo backtesting and optimisation
│   ├── README.md
│   ├── configs.py
│   ├── technical_indicators.py
│   ├── precompute.py
│   ├── backtest.py
│   └── data/
│       ├── nifty_15min.csv         (generated — gitignored)
│       ├── nifty_75min.csv         (generated — gitignored)
│       ├── vix_daily.csv           (generated — gitignored)
│       └── trade_logs/             (generated — gitignored)
└── data_pipeline/                  # Automated historical data download
    ├── README.md
    ├── weekly_option_data_sensex.py
    ├── weekly_option_data_nifty.py
    ├── run_sensex_downloader.sh
    ├── run_nifty_downloader.sh
    ├── rename_legacy_files.py
    ├── delete_empty_files.py
    ├── config/
    │   ├── options_list_sensex.csv
    │   └── options_list_nf.csv
    └── data/                       (excluded from git — raw market data)
        ├── indices/
        ├── sensex/
        └── nifty/
            └── options/
```