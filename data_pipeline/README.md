# Data Pipeline

Automated pipeline to download and maintain historical 1-minute OHLCV data for Sensex and Nifty options contracts, index data for Sensex, Nifty, and India VIX, daily OHLC for Nifty and VIX, and front-month MCX commodity futures.

For full details on design decisions, API behaviour, deployment, and file formats see the [root README](../README.md).

## Scripts

| Script | Description | Runs on | Schedule |
|--------|-------------|---------|----------|
| `data_downloader_angelone.py` | Downloads Sensex options, all 1-min indices, and daily Nifty + VIX OHLC via AngelOne | VPS (`delos`) | Weekdays 15:45 IST |
| `data_downloader_mcx.py` | Downloads/updates 1-min OHLCV for the current front-month futures contract on every enabled MCX underlying, via AngelOne (SmartAPI) | Manual (not yet scheduled) | Manual |
| `mcx_live_downloader.py` | Live 1-min CRUDEOILM polling (boundary-aligned, zero-buffer, resilient retry/backoff) plus a parallel WebSocket SNAP_QUOTE feed, from NSE close through MCX close. Doubles as an AB1021 rate-limit diagnostic probe. | Manual (evening, ad hoc) | Manual |
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

### MCX Futures Downloader
`data_downloader_mcx.py` downloads 1-min OHLCV for the current **front-month** futures contract (nearest un-expired expiry) on every underlying enabled in `config/mcx_underlyings.csv`, via AngelOne SmartAPI (`exchange="MCX"`). ICICI Direct/Breeze does not support MCX at all — confirmed against its own docs and an open, unanswered GitHub issue on the Breeze SDK repo asking for it (`Idirect-Tech/Breeze-Python-SDK#122`) — so this pipeline exists only on the AngelOne side.

Two things make this different from the Sensex/Nifty options downloader:
- **No expired-contract history.** SmartAPI does not serve historical data for expired F&O contracts (confirmed via the SmartAPI forum), so there is no way to backfill past MCX contracts. This pipeline is a forward-collection tool: each run refreshes the scrip master, picks whichever contract is currently front-month per underlying, and backfills/updates *that* contract's data from its own listing date (up to `LOOKBACK_DAYS`, currently 200) forward. There is no analogue of `config/options_list_sensex.csv`'s pre-known expiry list, because expired contracts can't be fetched anyway.
- **Automatic roll detection.** `data/mcx_contract_tracking.csv` records which contract (symbol/token) was front-month for each underlying on the last run. When a contract expires and the exchange's own front-month rolls to the next one, the script picks that up automatically (it always re-derives front-month from the live scrip master) and posts a `#data-alerts` Slack notification listing every underlying that rolled. The old contract's CSV file is left as-is; a new file starts under the new expiry date.
- **"Already up-to-date" skip (added 2026-08-30, `plans/prometheus-phase2-production.md` §1).** `_already_up_to_date()` skips an underlying entirely if its file's last candle is already within `LIVE_FEED_FRESHNESS_MIN` (60 min) of now — meaning a live process (`mcx_live_downloader.py`, or Prometheus's own production poller) is already keeping it current, so there's nothing for this post-market run to backfill. Instrument-agnostic by construction: no underlying name is hardcoded, so it naturally skips whichever instrument is currently live-fed (CRUDEOILM today) and picks up the rest, and automatically follows a Prometheus instrument switch (§6) without a config change here.

A first backfill (28 underlyings × up to 200 days) took about 20 minutes end-to-end under the shared AngelOne rate limits and pulled ~1.8M rows. Two underlyings (`COTTONOIL`, `STEELREBAR`) returned zero data across the full lookback — both are known thin/illiquid MCX contracts (small lot sizes, coarse tick sizes), not a script fault; their folders exist but stay empty until the exchange sees a trade.

Not yet wired into cron — MCX's non-agri session runs to 23:30/23:55 IST, well past the equities downloader's 15:45 slot, so it needs its own schedule (something after MCX close, e.g. ~23:59, or the next morning before market open) rather than reusing `run_angelone_downloader.sh`'s timing.

**`download_futures_contract()`'s incremental resume was bugged until 2026-08-28**: it computed `fetch_from = last_ts + 1 day` unconditionally, which is only correct when the prior day was already fully downloaded. Any run that left a day partially downloaded (ad-hoc manual pull mid-session, an interrupted run) caused the *next* run to jump straight past the rest of that day and never revisit it — silently and permanently, since nothing flags a mid-day cutoff as abnormal. Fixed to re-request from the start of `last_ts`'s own day instead; the redundant re-fetch of already-saved rows is harmless (deduped on `time_stamp` before saving).

Two known gap days from before the fix, confirmed **permanently unrecoverable** (AngelOne only retains an expired contract's historical data for ~1-2 weeks post-expiry, and both windows have long since lapsed):
- **CRUDEOILM, 2026-02-01, ~17:00 onward** — 1 trade in `prometheus_backtest/`'s (superseded) v1 baseline was force-closed early by this gap being misread as that day's actual session end; the current recommended Phase 2 config has zero trades touching this day.
- **CRUDEOILM, 2026-08-18, ~16:48 onward** — no trade in either backtest touches this day; zero impact on any reported result.

Neither is fixable at this point — noted here so a future gap-check doesn't waste time trying to backfill them, and so anyone reading old commentary that called 2026-02-01 "a half day" (an early, incorrect guess) knows it was actually this data gap.

### MCX Live Downloader (Post-NSE-Close)
`mcx_live_downloader.py` is a separate, ad hoc script (not the overnight `data_downloader_mcx.py`) that polls CRUDEOILM live from NSE close (15:30) through MCX close, for two purposes:
1. **Genuinely useful live data.** Writes into the same front-month contract file `data_downloader_mcx.py` maintains (`data/mcx/CRUDEOILM/<expiry>_futures.csv`), via the same dedup-on-timestamp merge — the "maintained running CSV" idea from `plans/prometheus-phase2-production.md` §1, kept current live rather than only overnight.
2. **A diagnostic probe for the ongoing AngelOne AB1021 false-positive rate-limit investigation.** Logs every single `getCandleData` attempt (not just each cycle's final outcome) to `data/ab1021_probe_log.csv`, to test whether the AB1021 hit rate differs between NSE-open hours and MCX-only hours.

Resilience is ported directly from Iris's hardened candle-fetch pattern (`plans/iris-signal-pipeline-hardening.md`): an inner 3-attempt/1s burst per fetch, and a non-blocking outer pending-recovery queue that retries missed windows every subsequent cycle rather than blocking the poll loop.

**Boundary-aligned, zero-buffer polling.** The REST fetch fires as close to each 1-minute candle's close as possible (e.g. the 16:01 candle is fetched right at 16:02), re-deriving the sleep target from wall-clock time every cycle so there's no accumulated drift across a multi-hour run. `CANDLE_CLOSE_BUFFER_SEC = 0` deliberately — no artificial safety margin, matching how Iris runs live: fire at the boundary and let retry/backoff absorb whatever doesn't succeed on the first try, rather than backing off in advance of a problem that may not occur.

**Parallel WebSocket SNAP_QUOTE feed.** A second, independent resilience test runs alongside the REST loop: a background-thread WebSocket subscription in SNAP_QUOTE mode (best-5 bid/ask, not the SDK's separate 20-level DEPTH mode), logging every tick — LTP, average traded price, buy/sell totals, and all five bid/ask levels — to `data/mcx_snapquote_log.csv` for liquidity/slippage analysis. Reconnect-with-backoff is ported from `websocket_feed.py`'s `SharedFeed` (`WS_RECONNECT_BACKOFF_SEC = [5, 10, 20, 40, 60]`, dedicated reconnect-worker thread, never blocking the on_error/on_close callback itself). One thing worth flagging for future readers: the installed SDK's own SNAP_QUOTE parser (`smartWebSocketV2.py`'s `_parse_data`) *looks* buggy on a read — it appears to assign the inner best-5 parser's "sell" list to the outer `best_5_buy_data` key and vice versa. Live-verified before trusting it rather than "fixing" a suspected bug blind: the delivered `best_5_buy_data`'s top price is genuinely below `best_5_sell_data`'s (correct bid<ask ordering) — the labels are correct as delivered despite how the source reads, so don't re-swap them.

**Logging gotcha found and fixed (2026-08-28):** this script's own `logging.basicConfig(...)` call was a silent no-op — `data_downloader_mcx` (imported above it) calls its own `logging.basicConfig()` first, which claims the root logger, and stdlib `basicConfig()` does nothing once handlers already exist. The dedicated `mcx_live_downloader_YYYYMMDD.log` file (written directly under `data_pipeline/`, not `data/`) stayed empty all run despite console output looking normal. Fixed with `force=True` on the later call, which replaces the existing handlers with this script's own StreamHandler + FileHandler pair.

Usage: `python data_pipeline/mcx_live_downloader.py [--max-cycles N]` (the flag is a smoke-test override, not for normal use). Not cron-scheduled — started manually in the evening after confirming no live strategy is trading (shared AngelOne session/rate-limit budget).

## Directory Structure

```
data_pipeline/
├── data_downloader_angelone.py     # AngelOne: Sensex options + all indices (1-min + daily)
├── data_downloader_icicidirect.py  # ICICI Direct: Nifty options (1-min)
├── data_downloader_mcx.py          # AngelOne: front-month MCX futures (1-min), overnight backfill/update
├── mcx_live_downloader.py          # AngelOne: live 1-min CRUDEOILM (NSE close -> MCX close) + parallel WS
│                                   #   SNAP_QUOTE feed; also an AB1021 rate-limit diagnostic probe
├── mcx_live_downloader_YYYYMMDD.log # Dated log, one per run day (gitignored)
├── run_angelone_downloader.sh      # VPS cron wrapper for AngelOne downloader
├── run_icicidirect_downloader.sh   # Laptop cron wrapper for ICICI Direct downloader
├── nifty_daily_index.py            # Backup: daily Nifty OHLC via ICICI Breeze
├── rename_legacy_files.py
├── delete_empty_files.py
├── README.md
├── config/
│   ├── options_list_sensex.csv     # Sensex expiry list and download status
│   ├── options_list_nf.csv         # Nifty expiry list and download status
│   └── mcx_underlyings.csv         # MCX underlyings to track (name, enabled)
└── data/                           # Not tracked by git — lives on each machine
    ├── user_credentials_angel.csv
    ├── user_credentials_icici.csv
    ├── instrument_master.csv       # Auto-refreshed daily from AngelOne (BFO/Sensex)
    ├── mcx_instrument_master.csv   # Auto-refreshed from AngelOne (MCX FUTCOM)
    ├── mcx_contract_tracking.csv   # Last-seen front-month contract per underlying (roll detection)
    ├── ab1021_probe_log.csv        # mcx_live_downloader.py: per-getCandleData-call diagnostic log
    ├── mcx_snapquote_log.csv       # mcx_live_downloader.py: WS SNAP_QUOTE ticks (best-5 bid/ask + LTP)
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
    ├── nifty/
    │   └── options/                # Nifty options — one folder per expiry
    │       └── YYYY-MM-DD/
    └── mcx/                        # MCX futures — one folder per underlying
        └── <NAME>/
            └── YYYY-MM-DD_futures.csv   # front-month contract, named by its expiry
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

## Local sync utility (`datasync`, outside this repo)

`~/.local/bin/datasync` (personal script, not version-controlled, run manually on the local machine) rsyncs `data_pipeline/data/{indices,sensex,cas_auction_tracking.csv,cas_gap_fade_tracking.csv,cas_market_watch}/` from the VPS to local, then `rclone sync`s the local `data_pipeline/data/` tree to a Google Drive remote (`Work:Data`) as an off-machine backup.

**2026-08-10: `Work` remote's rclone `client_id` reconfigured.** It was previously running on rclone's shared/default Google Drive `client_id`, which rclone's own tooling warned is being retired sometime in 2026. Created a dedicated Google Cloud project (`quant-grow.com` Workspace account), enabled the Drive API, configured an OAuth consent screen (External, with the account added as a test user), and generated a Desktop-app OAuth client. Re-ran `rclone config` on the `Work` remote with the new `client_id`/`client_secret`, replaced the old cached token, completed the browser OAuth flow, kept it as a non-Shared-Drive (`My Drive`) remote — matching the prior config. Confirmed fixed: `rclone config show Work` now shows the dedicated `client_id`, and `datasync` no longer prints the deprecation notice. No repo changes involved (config lives in `~/.config/rclone/rclone.conf`, outside `algo-trading-lab`) — this note exists here purely so the fix is discoverable if the sync ever breaks again.
