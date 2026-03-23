# Data Pipeline

Automated pipeline to download and maintain historical 1-minute OHLCV data for Sensex and Nifty options contracts, and index data for Sensex, Nifty, and India VIX.

For full details on design decisions, API behaviour, deployment, and file formats see the [root README](../README.md).

## Scripts

| Script | Description | Runs on | Schedule |
|--------|-------------|---------|----------|
| `weekly_option_data_sensex.py` | Downloads Sensex options and all index data via AngelOne | VPS (`delos`) | Weekdays 15:45 IST |
| `weekly_option_data_nifty.py` | Downloads Nifty options via ICICI Direct/Breeze | Laptop | Tuesdays 23:30 IST |
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
45 15 * * 1-5 /home/parijnan/anaconda3/bin/python /home/parijnan/scripts/algo-trading-lab/data_pipeline/weekly_option_data_sensex.py >> /home/parijnan/scripts/algo-trading-lab/data_pipeline/cron.log 2>&1
```

**Laptop:**
```
30 23 * * 2 /home/parijnan/anaconda3/bin/python "/home/parijnan/scripts/algo-trading-lab/data_pipeline/weekly_option_data_nifty.py" >> "/home/parijnan/scripts/algo-trading-lab/data_pipeline/cron.log" 2>&1
```