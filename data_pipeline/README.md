# Data Pipeline

Automated pipeline to download and maintain historical 1-minute OHLCV data for Sensex and Nifty options contracts, index data for Sensex, Nifty, and India VIX, and daily OHLC for Nifty and VIX.

For full details on design decisions, API behaviour, deployment, and file formats see the [root README](../README.md).

## Scripts

| Script | Description | Runs on | Schedule |
|--------|-------------|---------|----------|
| `data_downloader_angelone.py` | Downloads Sensex options, all 1-min indices, and daily Nifty + VIX OHLC via AngelOne | VPS (`delos`) | Weekdays 15:45 IST |
| `data_downloader_icicidirect.py` | Downloads Nifty options via ICICI Direct/Breeze | Laptop | Wednesdays 23:30 IST |
| `run_angelone_downloader.sh` | Wrapper: git pull → run AngelOne downloader → git push if config changed | VPS (`delos`) | Weekdays 15:45 IST |
| `run_icicidirect_downloader.sh` | Wrapper: git pull → run ICICI Direct downloader → git push if config changed | Laptop | Wednesdays 23:30 IST |
| `angel_nifty_backtest_data.py` | Downloads Nifty options for specific expiries from Angel One into `data/nifty/temp/` for realtime backtesting of open/recent trades not yet in ICICI | Laptop | Manual |
| `nifty_daily_index.py` | Backup: official daily Nifty OHLC via ICICI Breeze (not scheduled — manual use only) | Laptop | Manual |
| `rename_legacy_files.py` | One-time utility to rename legacy Sensex option files | Laptop | Manual |
| `delete_empty_files.py` | One-time utility to delete empty option CSV files | Laptop | Manual |

### Data Integrity Guards
To ensure high-fidelity historical data, `data_downloader_angelone.py` implements a **Data Integrity Warning**. Since each trading day consists of 375 minutes, the script validates that at least 375 new rows are added for each index (Sensex, Nifty, VIX). If the count is lower, an alert is sent to the `#error-alerts` Slack channel.

## Directory Structure

```
data_pipeline/
├── data_downloader_angelone.py     # AngelOne: Sensex options + all indices (1-min + daily)
├── data_downloader_icicidirect.py  # ICICI Direct: Nifty options (1-min)
├── run_angelone_downloader.sh      # VPS cron wrapper for AngelOne downloader
├── run_icicidirect_downloader.sh   # Laptop cron wrapper for ICICI Direct downloader
├── nifty_daily_index.py            # Backup: daily Nifty OHLC via ICICI Breeze
├── rename_legacy_files.py
├── delete_empty_files.py
├── README.md
├── config/
│   ├── options_list_sensex.csv     # Sensex expiry list and download status
│   └── options_list_nf.csv         # Nifty expiry list and download status
└── data/                           # Not tracked by git — lives on each machine
    ├── user_credentials_angel.csv
    ├── user_credentials_icici.csv
    ├── instrument_master.csv       # Auto-refreshed daily from AngelOne
    ├── indices/
    │   ├── sensex.csv              # 1-min Sensex index
    │   ├── nifty.csv               # 1-min Nifty index (last traded price)
    │   ├── india_vix.csv           # 1-min India VIX
    │   ├── nifty_daily.csv         # Official daily Nifty OHLC (AngelOne, same-day)
    │   ├── sensex_daily.csv        # Official daily Sensex OHLC (AngelOne, same-day)
    │   └── india_vix_daily.csv     # Official daily VIX OHLC (AngelOne, same-day)
    ├── sensex/                     # Sensex options — one folder per expiry
    │   └── YYYY-MM-DD/
    └── nifty/
        └── options/                # Nifty options — one folder per expiry
            └── YYYY-MM-DD/
```

## Crontab Entries

**VPS:**
```
45 15 * * 1-5 /home/parijnan/scripts/algo-trading-lab/data_pipeline/run_angelone_downloader.sh
```

**Laptop:**
```
30 23 * * 3 /home/parijnan/scripts/algo-trading-lab/data_pipeline/run_icicidirect_downloader.sh
```

Note: Nifty downloads run on **Wednesday** nights (not Tuesday) — ICICI Direct does not update their servers immediately after expiry.
