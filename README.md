# Algo Trading Lab

A personal algorithmic trading laboratory for backtesting, optimising, and automating strategies for Indian Index Options.

## Strategies

### [Artemis](./artemis/) — Sensex Iron Condor
A market-neutral credit spread strategy trading a weekly Sensex Iron Condor, running Monday to Thursday. Sells OTM options on both sides and buys further OTM options as hedges. Deployed when India VIX < 16.

| | |
|---|---|
| Instrument | Sensex weekly options |
| Structure | Iron Condor (PE spread + CE spread) |
| Entry | Monday 10:30 AM |
| Expiry | Thursday |
| Broker | Angel Broking (SmartConnect) |
| Status | Live |

### [Apollo](./apollo_backtest/) — Nifty High-VIX Trend Following
A directional trend-following credit spread strategy deployed when India VIX > 16. Uses dual-timeframe Supertrend (75-min and 15-min) to identify and trade sustained directional moves in Nifty options.

| | |
|---|---|
| Instrument | Nifty weekly options |
| Structure | Credit spread (directional, one side only) |
| Signal | Dual Supertrend — 75-min regime, 15-min entry/exit |
| Deploy condition | India VIX > 16 |
| Broker | TBD |
| Status | In development |

## Infrastructure

| Component | Details |
|-----------|---------|
| VPS | Linode Nanode — hostname `delos` |
| OS | Ubuntu 24.04, 1GB RAM, 1 CPU, 25GB storage |
| Laptop | Garuda Linux (Arch-based) |
| Broker (live) | Angel Broking (SmartConnect API) |
| Broker (data) | ICICI Direct (Breeze) for Nifty, Angel Broking for Sensex and indices |
| Notifications | Slack (`#data-alerts`, `#error-alerts`) |
| Language | Python (Anaconda) |

## VIX Regime

| VIX Level | Strategy |
|-----------|---------|
| < 14 | Artemis — full confidence |
| 14 – 16 | Artemis — reduced size, tighter SLs |
| > 16 | Apollo |

## Data Pipeline

Historical 1-minute OHLCV data for Nifty and Sensex options and indices is maintained by an automated pipeline. See [`data_pipeline/`](./data_pipeline/) for scripts and config.

| Data | Source | Coverage | Schedule |
|------|--------|----------|----------|
| Sensex options | Angel Broking — VPS cron | Mid-2024 onwards | Weekdays 15:45 IST |
| Nifty, Sensex, India VIX index | Angel Broking — VPS cron | Mid-2024 onwards | Weekdays 15:45 IST |
| Nifty options | ICICI Breeze — laptop cron | May 2019 onwards | Tuesdays 23:30 IST |

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
├── data_pipeline/                  # Automated data download pipeline
│   ├── README.md
│   ├── weekly_option_data_sensex.py
│   ├── weekly_option_data_nifty.py
│   ├── rename_legacy_files.py
│   ├── delete_empty_files.py
│   └── config/
│       ├── options_list_sensex.csv
│       └── options_list_nf.csv
├── artemis/                        # Live Sensex Iron Condor strategy (TODO: add)
│   ├── artemis.py
│   ├── iron_condor.py
│   ├── credit_spread.py
│   ├── configs.py
│   └── functions.py
└── apollo_backtest/                # High-VIX Nifty trend following strategy
    ├── README.md
    ├── configs.py
    ├── technical_indicators.py
    ├── precompute.py
    ├── backtest.py                 (TODO)
    └── analysis.py                 (TODO)
```