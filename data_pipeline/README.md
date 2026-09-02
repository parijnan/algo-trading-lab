# Data Pipeline

Automated pipeline to download and maintain historical 1-minute OHLCV data for Sensex and Nifty options contracts, index data for Sensex, Nifty, and India VIX, daily OHLC for Nifty and VIX, and front-month MCX commodity futures.

For full details on design decisions, API behaviour, deployment, and file formats see the [root README](../README.md).

## Scripts

| Script | Description | Runs on | Schedule |
|--------|-------------|---------|----------|
| `data_downloader_angelone.py` | Downloads Sensex options, all 1-min indices, and daily Nifty + VIX OHLC via AngelOne | VPS (`delos`) | Weekdays 15:45 IST |
| `data_downloader_mcx.py` | Downloads/updates 1-min OHLCV for the current front-month and next-month futures contract on every enabled MCX underlying (base metals, energy, precious metals — see `config/mcx_underlyings.csv`), via AngelOne (SmartAPI) | VPS (`delos`) | Weekdays 23:56 IST |
| `mcx_live_downloader.py` | Live 1-min CRUDEOILM polling (boundary-aligned, zero-buffer, resilient retry/backoff) plus a parallel WebSocket SNAP_QUOTE feed, from NSE close through MCX close. Doubles as an AB1021 rate-limit diagnostic probe. | Manual (evening, ad hoc) | Manual |
| `nse_cas_market_watch.py` | Polls NSE's live Closing Auction Session Market Watch feed (indicative equilibrium price, imbalance, final auction print) every 15s through the 15:14-15:36 window, one row per poll per symbol | VPS (`delos`) | Weekdays 15:12 IST |
| `data_downloader_icicidirect.py` | Downloads Nifty options via ICICI Direct/Breeze | Laptop | Wednesdays 23:30 IST |
| `run_angelone_downloader.sh` | Wrapper: git pull → run AngelOne downloader → git push if config changed | VPS (`delos`) | Weekdays 15:45 IST |
| `run_mcx_downloader.sh` | Wrapper: git pull → run MCX downloader (no config push — `data/` isn't tracked) | VPS (`delos`) | Weekdays 23:56 IST |
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
`data_downloader_mcx.py` downloads 1-min OHLCV for the current **front-month** and **next-month** futures contract (nearest and second-nearest un-expired expiry) on every underlying enabled in `config/mcx_underlyings.csv`, via AngelOne SmartAPI (`exchange="MCX"`). ICICI Direct/Breeze does not support MCX at all — confirmed against its own docs and an open, unanswered GitHub issue on the Breeze SDK repo asking for it (`Idirect-Tech/Breeze-Python-SDK#122`) — so this pipeline exists only on the AngelOne side.

Two things make this different from the Sensex/Nifty options downloader:
- **No expired-contract history, and no backfill past a contract's own front-month tenure.** SmartAPI does not serve historical data for expired F&O contracts (confirmed via the SmartAPI forum). Worse — confirmed 2026-09-01/02 via a real corrupted-data incident (`prometheus_backtest/README.md`, `plans/prometheus-phase3-production.md` §6) — querying `getCandleData` for a contract's token over a date range *before that contract was genuinely front-month* doesn't come back empty either: it comes back with whichever other contract actually was front-month at that time, silently mislabeled under the requested token. Confirmed as a broker-side artifact across three separate contract-pairs (zero-spread clone across each pair's entire pre-front-month history), not something this pipeline's own code causes. So this pipeline is strictly forward-collection: a brand-new contract file (no prior tracking history) starts capturing from the day it's first seen, never backward — the old `LOOKBACK_DAYS=200` deep-backfill-on-first-sight approach was removed 2026-09-02 for exactly this reason. There is no analogue of `config/options_list_sensex.csv`'s pre-known expiry list, because expired contracts can't be fetched anyway.
- **Automatic roll detection.** `data/mcx_contract_tracking.csv` records which contract (symbol/token) was front-month for each underlying on the last run. When a contract expires and the exchange's own front-month rolls to the next one, the script picks that up automatically (it always re-derives front-month from the live scrip master) and posts a `#data-alerts` Slack notification listing every underlying that rolled. The old contract's CSV file is left as-is — once a contract stops being selected as front-month it's simply never queried again by this script (`select_front_month_contracts` filters to unexpired contracts only), so there's no separate "freeze" step needed; a new file starts under the new expiry date.
- **"Already up-to-date" skip — removed 2026-09-02.** `_already_up_to_date()` (added 2026-08-30, `plans/prometheus-phase2-production.md` §1) used to skip an underlying if a live process (`mcx_live_downloader.py`, or Prometheus's own poller) had already kept its file current. Retired as part of the same redesign that moved this cron to 23:56 (see "Cron schedule" below) — the design intent is that Prometheus stops writing into these files at all, making this downloader the sole writer for every contract. **Sequencing note:** that Prometheus-side change is a plan, not yet implemented (`plans/prometheus-phase3-production.md`'s Prometheus-write-removal section) — until it lands, Prometheus's live poller still writes into CRUDEOIL/CRUDEOILM's files during the day, and this downloader will redundantly re-fetch that day's session for whichever one is actively traded. Harmless (dedup-safe merge, same as every other re-fetch here) but worth knowing while both halves of this redesign aren't yet live together.
- **Next-month tracking, universal (added 2026-09-02, simplified from an initial CRUDEOIL/CRUDEOILM-only design to every enabled underlying).** Prometheus's Phase 3 design (`plans/prometheus-phase3-production.md` §2-§6) can hold a position across a contract roll, which means the new front-month contract needs real ST_15 seed history *at the moment it becomes front-month* — but a front-month-only downloader would have exactly zero history on it right then, since no file ever backfills into the past (previous bullet). `select_next_month_contracts()` picks the second-nearest unexpired expiry for every enabled underlying and downloads it through the same `download_futures_contract()` path (fully generic on token/expiry — nothing about it assumes "front-month"), so history quietly accumulates in every underlying's next-month file, before the day it rolls into front-month. No new roll-alerting needed for this — the moment a next-month contract becomes front-month is exactly the transition the existing roll-detection bullet above already posts to Slack.

**Commodity scope (added 2026-09-02):** `config/mcx_underlyings.csv` enables only base metals, energy, and precious metals — `ALUMINI(UM)`, `COPPER`, `LEAD(MINI)`, `NICKEL`, `ZINC(MINI)`, `STEELREBAR`; `CRUDEOIL(M)`, `NATURALGAS`/`NATGASMINI`; `GOLD` and its variants, `SILVER` and its variants. Agri/soft commodities (`CARDAMOM`, `COTTON`, `COTTONOIL`, `KAPAS`, `MENTHAOIL`) and `ELECDMBL` (electricity, not a classic oil/gas energy contract) are disabled, not deleted — flip `enabled` back to `True` per row to re-include one.

**Known limitation, not yet cleaned up:** the very first backfill (2026-08-30, 28 underlyings × up to 200 days, ~1.8M rows) ran under the old `LOOKBACK_DAYS=200` logic, before the pre-front-month corruption above was diagnosed — so every underlying's on-disk history from that run carries the same corruption pattern found in CRUDEOILM's September-contract file, not just CRUDEOIL/CRUDEOILM. `prometheus_backtest/data_loader.py`'s front-month de-duplication step works around this for Prometheus's own reads, but the raw files under `data/mcx/*/` themselves are not corrected. Auditing/re-basing the existing files is a separate job from standing up this cron — flagged here so a future reader doesn't assume the on-disk history is clean just because the downloader that produced it has since been fixed. (Two underlyings, `COTTONOIL` and `STEELREBAR`, returned zero data across that lookback regardless — both known thin/illiquid MCX contracts, not a script fault; their folders exist but stay empty until the exchange sees a trade.)

**Cron schedule:** Weekdays 23:56 IST (`56 23 * * 1-5`) — a same-calendar-day slot, chosen with a ~1-minute buffer past MCX's latest possible close (23:30-23:55 IST, DST-dependent). A same-day slot is deliberate, not just a rounding choice: for a brand-new contract file (first day this script sees it as front- or next-month), `fetch_from`/`fetch_to` both resolve to *today*, and at 23:56 that day's real session has already happened — so day-one history is captured immediately, rather than the empty first attempt a next-morning slot would get (the new day's session hasn't opened yet at, say, 06:00 or 00:00 the following day; that request just comes back empty and self-heals the night after — harmless, but 23:56 avoids the one-day delay entirely).

Running this close to the wire is safe to do — it wasn't before this pipeline's 2026-09-02 redesign — because the *design intent* is that **Prometheus stops writing into these files at all.** It used to (`_merge_1m` writing every live poll straight into the same contract CSV, `plans/prometheus-phase2-production.md` §1), which is why this downloader previously needed `_already_up_to_date()` to detect and skip a file a live process was already keeping fresh, and needed to run inside that freshness window specifically to avoid a stale-file false trigger. Once Prometheus keeps its own in-memory intraday series instead (`plans/prometheus-phase3-production.md`'s Prometheus-write-removal section — planned, not yet implemented as of 2026-09-02), this downloader becomes the sole writer for every contract, every run, with no freshness race to schedule around at all — so `_already_up_to_date`/`LIVE_FEED_FRESHNESS_MIN` were removed now, ahead of that change landing. Until it does, see the "Already up-to-date skip" sequencing note above. The only other historical writer, `mcx_live_downloader.py`, is manual/ad hoc; losing the skip just costs one redundant, dedup-safe re-fetch on the rare evening someone runs it. Not run on weekends, matching every other cron in this repo (`leto.py`, `run_angelone_downloader.sh`) — MCX's rare Saturday special sessions (e.g. the 2026-02-01 Union Budget session) are an accepted gap, already handled at the backtest-analysis layer (weekend bars dropped in `prometheus_backtest/data_loader.py`) rather than by data collection.

**API volume note:** next-month tracking is now universal (every enabled underlying, not only CRUDEOIL/CRUDEOILM — see below), so a nightly run now covers 44 contracts (22 × front+next month) instead of 22. The very first backfill under the old front-month-only, all-`LIVEFEED`-eligible design took ~20 minutes for 28 underlyings; expect roughly double that under the shared AngelOne rate limits (2/sec, 180/min, 5000/hr) the first time this runs with next-month enabled for everything.

`STEELREBAR` stays enabled despite returning zero rows in the first backfill — deliberate, not inherited from the old all-`True` default: it's a genuine base metal, and leaving it enabled means the pipeline picks up data automatically the day the exchange sees a trade, rather than needing a manual re-enable later.

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
│   └── mcx_underlyings.csv         # MCX underlyings to track (name, enabled, track_next_month)
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
56 23 * * 1-5 /home/parijnan/scripts/algo-trading-lab/data_pipeline/run_mcx_downloader.sh
```

**Laptop:**
```
30 23 * * 3 /home/parijnan/scripts/algo-trading-lab/data_pipeline/run_icicidirect_downloader.sh
```

Note: Nifty downloads run on **Wednesday** nights (not Tuesday) — ICICI Direct does not update their servers immediately after expiry.

## Local sync utility (`datasync`, outside this repo)

`~/.local/bin/datasync` (personal script, not version-controlled, run manually on the local machine) rsyncs `data_pipeline/data/{indices,sensex,cas_auction_tracking.csv,cas_gap_fade_tracking.csv,cas_market_watch}/` from the VPS to local, then `rclone sync`s the local `data_pipeline/data/` tree to a Google Drive remote (`Work:Data`) as an off-machine backup.

**2026-08-10: `Work` remote's rclone `client_id` reconfigured.** It was previously running on rclone's shared/default Google Drive `client_id`, which rclone's own tooling warned is being retired sometime in 2026. Created a dedicated Google Cloud project (`quant-grow.com` Workspace account), enabled the Drive API, configured an OAuth consent screen (External, with the account added as a test user), and generated a Desktop-app OAuth client. Re-ran `rclone config` on the `Work` remote with the new `client_id`/`client_secret`, replaced the old cached token, completed the browser OAuth flow, kept it as a non-Shared-Drive (`My Drive`) remote — matching the prior config. Confirmed fixed: `rclone config show Work` now shows the dedicated `client_id`, and `datasync` no longer prints the deprecation notice. No repo changes involved (config lives in `~/.config/rclone/rclone.conf`, outside `algo-trading-lab`) — this note exists here purely so the fix is discoverable if the sync ever breaks again.
