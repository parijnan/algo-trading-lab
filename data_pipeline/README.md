# Data Pipeline

Automated pipeline to download and maintain historical 1-minute OHLCV data for Sensex and Nifty options contracts, and index data for Sensex, Nifty, and India VIX.

For full details on design decisions, API behaviour, deployment, and file formats see the [root README](../README.md).

## Scripts

| Script | Description | Runs on | Schedule |
|--------|-------------|---------|----------|
| `weekly_option_data_sensex.py` | Downloads Sensex options and all index data via AngelOne | VPS (`delos`) | Weekdays 15:45 IST |
| `weekly_option_data_nifty.py` | Downloads Nifty options via ICICI Direct/Breeze | Laptop | Wednesdays 23:30 IST |
| `run_sensex_downloader.sh` | Wrapper: git pull → run Sensex downloader → git push if config changed | VPS (`delos`) | Weekdays 15:45 IST |
| `run_nifty_downloader.sh` | Wrapper: git pull → run Nifty downloader → git push if config changed | Laptop | Wednesdays 23:30 IST |
| `rename_legacy_files.py` | One-time utility to rename legacy Sensex option files | Laptop | Manual |
| `delete_empty_files.py` | One-time utility to delete empty option CSV files | Laptop | Manual |

## Directory Structure

```
data_pipeline/
├── weekly_option_data_sensex.py
├── weekly_option_data_nifty.py
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
    │   ├── sensex.csv
    │   ├── nifty.csv
    │   └── india_vix.csv
    ├── sensex/                     # Sensex options — one folder per expiry
    │   └── YYYY-MM-DD/
    └── nifty/
        └── options/                # Nifty options — one folder per expiry
            └── YYYY-MM-DD/
```

## Crontab Entries

**VPS:**
```
45 15 * * 1-5 /home/parijnan/scripts/algo-trading-lab/data_pipeline/run_sensex_downloader.sh
```

**Laptop:**
```
30 23 * * 3 /home/parijnan/scripts/algo-trading-lab/data_pipeline/run_nifty_downloader.sh
```

Note: Nifty downloads run on **Wednesday** nights (not Tuesday) — ICICI Direct does not update their servers immediately after expiry.