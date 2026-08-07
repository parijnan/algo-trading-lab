# Data Pipeline

Automated pipeline to download and maintain historical 1-minute OHLCV data for Sensex and Nifty options contracts, index data for Sensex, Nifty, and India VIX, and daily OHLC for Nifty and VIX.

For full details on design decisions, API behaviour, deployment, and file formats see the [root README](../README.md).

## Scripts

| Script | Description | Runs on | Schedule |
|--------|-------------|---------|----------|
| `data_downloader_angelone.py` | Downloads Sensex options, all 1-min indices, and daily Nifty + VIX OHLC via AngelOne | VPS (`delos`) | Weekdays 15:45 IST |
| `nse_cas_market_watch.py` | Polls NSE's live Closing Auction Session Market Watch feed (indicative equilibrium price, imbalance, final auction print) every 15s through the 15:14-15:36 window, one row per poll per symbol | VPS (`delos`) | Weekdays 15:12 IST |
| `data_downloader_icicidirect.py` | Downloads Nifty options via ICICI Direct/Breeze | Laptop | Wednesdays 23:30 IST |
| `run_angelone_downloader.sh` | Wrapper: git pull → run AngelOne downloader → git push if config changed | VPS (`delos`) | Weekdays 15:45 IST |
| `run_icicidirect_downloader.sh` | Wrapper: git pull → run ICICI Direct downloader → git push if config changed | Laptop | Wednesdays 23:30 IST |
| `angel_nifty_backtest_data.py` | Downloads Nifty options for specific expiries from Angel One into `data/nifty/temp/` for realtime backtesting of open/recent trades not yet in ICICI | Laptop | Manual |
| `nifty_daily_index.py` | Backup: official daily Nifty OHLC via ICICI Breeze (not scheduled — manual use only) | Laptop | Manual |
| `rename_legacy_files.py` | One-time utility to rename legacy Sensex option files | Laptop | Manual |
| `delete_empty_files.py` | One-time utility to delete empty option CSV files | Laptop | Manual |

### Data Integrity Guards
To ensure high-fidelity historical data, `data_downloader_angelone.py` implements a **Data Integrity Warning**. Each trading day should have roughly 375 minutes of data; the script validates that at least 370 new rows are added for each index (Sensex, Nifty, VIX) — loosened from the old flat 375 to absorb the row-count variance described below. If the count is lower, an alert is sent to the `#error-alerts` Slack channel.

### Closing Auction Session (CAS) Handling
Since 3 Aug 2026, NSE/BSE run a Closing Auction Session for Nifty and Sensex: continuous trading in the underlying index stops at 15:15, a ~15-minute call auction follows with no 1-minute candles, then a single terminal print (the auction-clearing price) lands somewhere in 15:16–15:35. Derivatives keep trading on to 15:40. Two mechanisms in `data_downloader_angelone.py` handle this for Nifty and Sensex (not VIX, which ticks continuously and is unaffected):

- **`fill_missing_candles`** — flat-fills the mid-day auction gap with the pre-auction close, then reconstructs the terminal auction-print candle itself to match what the exchange's chart actually shows: open = the carried-forward pre-auction close, close = the terminal print (unchanged), high/low = the direction-aware max/min of the two. (An earlier version left the terminal candle's OHLC untouched, reasoning that forcing its open to the pre-auction close would produce an invalid candle — that didn't hold up against the chart's own display, which does exactly that and is a normal, valid candle.)
- **`extend_to_day_close`** — since the index has no ticks after the terminal print but derivatives (and backtests) care about price up to 15:40, each day is extended forward with flat carry-forward candles (at the terminal print's close) through 15:39.

Together these make every CAS-era Nifty/Sensex trading day exactly 385 rows (09:15–15:39 inclusive), gapless. The options fetch window (`MARKET_CLOSE`) was widened from 15:30 to 15:40 to match the extended derivatives session.

### CAS Auction-Move Tracking
`fill_missing_candles` also returns the day's auction-window move (pre-auction close → terminal print, both timestamps) whenever it detects a CAS gap. `log_cas_auction_moves()` appends one row per date/index to `data/cas_auction_tracking.csv`, gated to Nifty/Sensex only — no extra API calls, it's derived from data the pipeline already fetches. This exists to track a suspected closing-auction price-impact pattern (large, consistent one-directional moves on Nifty's auction print) under investigation as of Aug 2026; deliberately logs to CSV only, no Slack notification.

### NSE CAS Market Watch Tracking
`nse_cas_market_watch.py` polls NSE's own live auction feed directly — a different, richer data source than the mid-day gap-fill above, which only reconstructs the auction's *start* and *end* points from 1-min index candles. NSE's Market Watch page (`nseindia.com/market-data/closing-auction-session`) calls a public JSON endpoint (`/api/NextApi/apiClient/casApi?functionName=getCASData`, found by reading the page's own JS bundle — no official API docs exist for it) that returns, per F&O stock: the VWAP(15:00-15:15) reference price, the ±3% band, the live indicative equilibrium price (IEP) and imbalance quantities as the auction develops, and the final matched price/quantity once it closes. No auth required — cookie handling reuses the Akamai warm-up technique proven in `../../swing-trading-lab/data_pipeline/bhavcopy/nse_session.py` (a homepage hit 403s but still sets cookies; a follow-up hit to a real page completes the set), duplicated here in miniature rather than cross-imported since delos only deploys this repo.

The script polls every 15s from 15:14 to 15:36 and appends one row per (poll, symbol) to `data/cas_market_watch/YYYY-MM-DD.csv` — a full reconstruction of how the auction evolved per stock, not just its endpoints. Skips non-trading days via the root `data/holidays.csv`. Research-only; doesn't feed any live strategy or post to Slack.

**BSE has no equivalent found.** Its Market Watch page is a heavily obfuscated Angular SPA with lazy-loaded route chunks that couldn't be mapped via static analysis (tried: URL guessing, grepping all discoverable JS bundles including the ones with genuine `api.bseindia.com` calls, search-engine site search) — unlike NSE's, which yielded a clean endpoint on the first bundle checked. Live browser network inspection would likely resolve this quickly but is blocked by Claude's own safety restrictions for both `nseindia.com` and `bseindia.com`. Worth revisiting by checking the Network tab manually during a live 15:15-15:30 window if this is wanted later.

### CAS Gap-Fade Tracking
`log_gap_fade_tracking()` (called from `update_index`, same Nifty/Sensex-only gate) pairs each new CAS-era day's own open/low/high/close against the *prior* trading day's auction move logged in `cas_auction_tracking.csv`, appending one row per date/index to `data/cas_gap_fade_tracking.csv`. This is a research log testing whether a large one-directional auction move tends to fade the next session (open near the prior close, drift lower) — it does not drive any trading decision. Also derived entirely from data already fetched; CSV only, no Slack notification.

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
    ├── cas_auction_tracking.csv    # Daily Nifty/Sensex auction-window move (pre-auction close → terminal print)
    ├── cas_gap_fade_tracking.csv   # Research log: next-day open/low/close vs. prior day's auction move
    ├── cas_market_watch/           # NSE live auction feed polls, one file per day
    │   └── YYYY-MM-DD.csv
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
