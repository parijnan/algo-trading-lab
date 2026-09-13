# Plan: Fyers as an MCX Data Source — Deep Historical Backfill + Live-Feed Investigation

**Status (2026-09-13): blocked on Fyers account verification.** User has completed signup
(all segments requested), pending approval from Fyers's end. No code has been written yet —
this plan exists so work can start the moment an `access_token` is available, without
re-deriving the research that got us here. No production changes are in scope until
explicitly decided (see §6).

---

## 0. Why this exists

Angel One's `getCandleData` has no historical-depth wall per se, but a real data-correctness
bug: querying an MCX contract's token for dates before it was genuinely front-month doesn't
return "no data" — it silently returns a *different* contract's real prices, mislabeled under
the requested token (confirmed across three separate contract-pairs, `data_pipeline/data_downloader_mcx.py`'s
own header comment). There's no way to detect this from the API response alone, so the only
safe policy became "a new contract file never backfills into the past — capture only forward
from today." That's why our current CRUDEOILM/CRUDEOIL dataset only reaches back to
~2026-01-30, not because of any genuine broker-side depth limit.

Researched four alternatives this session (Zerodha, Upstox, Fyers docs via stale forum posts,
GlobalDataFeeds/TrueData) before finding the real answer by reading Fyers's *current* live docs
directly rather than trusting forum snapshots:

- **Zerodha**: same fundamental limitation — expired-contract `instrument_token`s can't be
  retrieved unless already cached while live; `continuous=1` stitching is daily-only.
- **Upstox**: has a genuinely better *mechanism* (a dedicated expired-instrument API down to
  1-minute) but explicitly excludes MCX — confirmed by Upstox engineering staff on their own
  forum, Feb 2026, still an open complaint in April 2026.
- **Fyers (forum posts)**: appeared to have the same "expired data not available" limitation —
  **this turned out to be stale**; the feature had since shipped.
- **Fyers (live docs, `myapi.fyers.in/docsv3`, read directly 2026-09-13)**: has a complete,
  documented 3-step workflow — Get Expiry Dates → Get Expired Contracts → Get Expired F&O
  Data — that resolves each contract by its own correct identity. **MCX is explicitly listed
  alongside NSE as available from 03 Jan 2022**, at 1-minute (and even 5-second) resolution.
  MCX symbol examples (`MCX:CRUDEOILM21SEP26FUT`) appear natively throughout their docs,
  matching our own production naming convention exactly.
- **GlobalDataFeeds / TrueData**: neither's published minute-bar retention beats what we
  already have from Angel One; both require a sales conversation for anything deeper.

Separately, Fyers publishes explicit, generous rate limits (10/sec, 200/min, 100,000/day) —
a documented ceiling, unlike Angel One's undocumented throttling that produces recurring
AB1021 bursts (`project_angelone_ratelimit_investigation`: confirmed widespread, unresolved,
retry/backoff is the only existing mitigation). Worth testing as a possible live-feed fix,
**as a separate question from the historical backfill work** — see §3.

We only need **separate per-contract files**, not a broker-stitched continuous series (explicit
user decision) — this matches our existing storage format already: one file per contract
expiry (`data_pipeline/data/mcx/CRUDEOILM/<expiry>_futures.csv`), with `load_futures_1min()`
doing the front-month stitching itself, in Python, from those separate files. So Fyers slots in
as a new *source* for that same shape of file — no new stitching logic needed, in principle.

---

## 1. Validate Fyers data against Angel One — do this FIRST, before anything else

**Nothing in §2-§5 should start until this passes.** We're about to trust a new data source for
a strategy that trades real money (paper-traded today, but the whole point of a deeper backtest
is informing a real decision later) — confirm it agrees with data we already trust before
building anything on top of it.

### 1.1 Method

1. Pick one **completed, fully-overlapping** contract-month both brokers have real data for —
   easiest choice is CRUDEOILM's most recently expired contract (Angel One's own copy already
   sits in `data_pipeline/data/mcx/CRUDEOILM/<expiry>_futures.csv`), so no separate "which
   contract" decision is needed.
2. Pull the *same* contract, same date range, from Fyers via the three-endpoint expired-contract
   workflow (§2 below covers the actual call sequence — for this validation step, a one-off
   script is enough, not the full downloader).
3. Bar-by-bar comparison, both instruments (CRUDEOILM and CRUDEOIL):
   - OHLC values — should match to the tick; any discrepancy needs an explanation, not a shrug
     (both brokers redistribute the same MCX exchange prints, so real disagreement is a red flag,
     not "brokers just differ sometimes").
   - Volume — check units convention matches (contracts/lots, not underlying barrels or
     something else) the same way we verified this for Angel One originally
     (`prometheus_backtest/README.md`'s "Units check, done first" precedent).
   - Row count / gap pattern — same missing-bar shape, or does one source have gaps the other
     doesn't?
   - Timestamp alignment — bar-open vs bar-close convention, IST offset, first/last bar of each
     session.
4. **Specific, already-known cross-check**: the CRUDEOILM 09:00 opening-bar price-discovery
   artifact (`_CRUDEOILM_OPENING_BAR_CORRECTIONS` in `prometheus_backtest/data_loader.py`,
   confirmed real via a CRUDEOILM-vs-CRUDEOIL true-range ratio check). Does Fyers's feed show
   the same wild opening print at 09:00, or is it already clean? This tells us two things at
   once: (a) whether the artifact is a genuine MCX/exchange-wide phenomenon (both brokers would
   show it) vs. an Angel-One-specific data/feed bug (only Angel One shows it), and (b) whether
   the existing correction table needs to apply to Fyers-sourced bars too, or only to Angel-One
   ones if we ever mix sources.
5. Same check for the known Sunday MCX Union Budget special-session artifact
   (`load_futures_1min`'s weekend-bar-drop fix, 2026-02-01) — does Fyers even return a row for
   that Sunday, and if so does it look like the same thin/disconnected session Angel One showed?

### 1.2 Pass/fail criteria (decide explicitly, don't wing it in the moment)

- OHLC: exact match expected. Any systematic offset (even sub-tick) needs root-causing before
  proceeding — could be a timestamp-alignment bug in the comparison itself, not a real data
  problem, but must be resolved either way.
- Volume: confirm same units; some vendor-to-vendor variance in exact volume figures is more
  plausible than for price (different aggregation windows, etc.) — but should be close, and any
  large disagreement needs investigating before trusting Fyers's volume figures for the
  liquidity/participation analysis in `prometheus_backtest/README.md`.
- Gaps: any gap in one source not present in the other needs explaining before that region of
  Fyers data is trusted.

**If this step fails or turns up unexplained disagreement, stop — don't build the downloader on
unvalidated data.** Come back to this plan once resolved.

---

## 2. Historical data downloader

Only start this once §1 passes.

### 2.1 Shape

Mirror `data_pipeline/data_downloader_mcx.py`'s architecture and **output schema exactly**
(`time_stamp,open,high,low,close,volume` headers, same timestamp format/timezone convention,
one file per contract expiry) so `load_futures_1min()` needs zero — or minimal — changes to
consume Fyers-sourced files. Confirm the exact column/dtype/timestamp match as part of §1, not
assumed here.

### 2.2 Workflow

- **Auth**: OAuth login flow (`generate-authcode` → `validate-authcode` → `access_token`).
  Refresh tokens are being discontinued April 1 (per their own docs) — the downloader needs
  either a daily re-auth step (manual or scripted around whatever replaces refresh-token renewal
  by then) or to be run in sessions short enough that a single day's token suffices. Worth
  checking Fyers's docs again closer to April for what the replacement flow looks like.
- **For each historical contract-month wanted**: Get Expiry Dates (resolve what expiries exist
  for the underlying, if not already known from MCX's own contract calendar) → Get Expired
  Contracts (resolve the exact `expired_instrument_key` for that expiry) → Get Expired F&O Data
  (pull 1-minute candles for that contract's own real tradeable window, chunked at 100 days per
  request — though a single MCX contract's own listing window is only a few months, so this is
  probably 1-2 chunks per contract, not a large chunking problem).
- **For the current, still-active front-month contract**: use the regular History API instead
  (not the expired-contract one) — plain per-contract query, `cont_flag` **not** set (per the
  explicit decision that we want separate contract data, not broker-side continuous stitching).
- **Both instruments**: CRUDEOILM and CRUDEOIL, same treatment, matching the existing
  two-instrument convention everywhere else in this project.
- **Rate limiting**: Fyers's limits (10/sec, 200/min) are generous and documented — simpler to
  respect than Angel One's AB1021 dance, but still worth basic pacing/backoff for safety, not
  assuming zero risk just because the limit is higher.

### 2.3 Storage — open decision, depends on §1's outcome

Two options, don't default to one without deciding:
- **Extend the existing per-contract files** in `data_pipeline/data/mcx/CRUDEOILM/` /
  `CRUDEOIL/` directly with Fyers-sourced history for contracts before our current earliest
  data — single unified dataset, cleanest for `load_futures_1min()`, but only safe once §1
  confirms the two sources genuinely agree in their overlap window.
- **Store separately** (e.g. a parallel `data_pipeline/data/mcx_fyers/` tree or a filename
  suffix) until confidence is higher, merge later once more validated.

Recommend leaning toward the first option *if and only if* §1 passes cleanly — a split dataset
just defers a decision that'll need making eventually and complicates `load_futures_1min()` in
the meantime.

### 2.4 Credentials

Mirror the existing pattern: add Fyers `app_id` / `app_secret` / `access_token` fields to
`data/user_credentials.csv` (gitignored, never committed — same as every other broker
credential in this repo).

---

## 3. Live data fetch test script — AB1021 investigation

**Explicitly separate from §2.** Even if this succeeds, replacing Angel One as Prometheus's
*live* production feed is a much bigger, separate decision — not something this plan commits
to, just investigates. Don't let a good result here quietly turn into a live-feed migration
without a dedicated go-ahead.

### 3.1 What "testing" actually means here

AB1021 doesn't show up on a single call — it shows up under sustained realistic polling load
(Prometheus's main loop hits the candle endpoint roughly every tick throughout a live session,
retrying on 15m/1h boundaries). A meaningful test means:

- A standalone script (not integrated into production) that authenticates against Fyers and
  polls the regular History API for the current CRUDEOILM front-month contract at our real
  production cadence, for a sustained stretch of an actual live trading session.
- Log every call's timing and response the same way `data_pipeline/mcx_live_downloader.py`'s
  existing AB1021 diagnostic probe already does, so the comparison is apples-to-apples against
  the Angel One baseline we already have data on.
- Watch specifically for: any 429/rate-limit response, any silent-failure pattern analogous to
  AB1021's own "JSON parse failure" symptom, and general latency/reliability under load.

### 3.2 Explicitly out of scope for this step

- No changes to `prometheus_production/prometheus_functions.py` or the live polling loop.
- No decision about migrating the live feed — that's a follow-on question (§6), contingent on
  this test's result and a lot more discussion (redundancy, failover, dual-broker session
  management).

---

## 4. Backtest pipeline changes

Contingent on §1 and §2 both landing cleanly.

- If Fyers-sourced files match the existing schema exactly (§2.1), `load_futures_1min()` and the
  front-month de-duplication logic should work **unchanged** — both are already written against
  the per-contract-file storage format, not against anything Angel-One-specific. Confirm this
  rather than assume it: run the existing loader against a Fyers-sourced file and check it parses
  cleanly, front-month dedup behaves, no exceptions.
- `_CRUDEOILM_OPENING_BAR_CORRECTIONS`: whether this applies to Fyers-sourced bars too depends on
  §1.4's finding. If the artifact is genuine and exchange-wide, the same correction table applies
  regardless of source. If it turns out to be an Angel-One-specific feed quirk, the correction
  needs to be scoped to only apply to Angel-One-originated rows once the two sources are mixed in
  one file.
- Weekend-bar-drop logic: same "does this need to change" check as above, informed by §1.5.
- **Once a deeper, validated dataset exists, re-running the full Phase 3 pipeline
  (`refresh_pipeline.py`) means calibration decisions were made on a much shorter window than
  will now be available.** This is a real thing to flag to the user explicitly once it happens,
  not something to silently absorb into a routine refresh — the mult-2.0 decision, the T1=2.2%
  fine-grid pick, etc. were all fit on ~7 months of data; a multi-year window could plausibly
  shift the picture. Whether/when to re-open those calibration questions is the user's call, not
  something to decide as a side effect of a data refresh.

---

## 5. `data_pipeline/` changes

- New downloader script (§2), following the existing naming convention
  (`data_downloader_fyers_mcx.py` or similar — match whatever pattern reads clearest next to
  `data_downloader_mcx.py`/`data_downloader_angelone.py`).
- Credentials file additions (§2.4).
- **Decide whether this becomes a recurring job or a one-time backfill.** Historical backfill for
  contracts that already expired is inherently a one-time (or occasional catch-up) operation, not
  a nightly cron job — Angel One's existing pipeline already handles ongoing forward collection.
  Unless §3's live-feed investigation changes the picture, Fyers's role here is "fill in the deep
  past once," not "replace the nightly MCX downloader."
- `CLAUDE.md` update: add the new script to the "Repository structure" / "Running things"
  sections once it exists, per the repo's own convention of keeping that file in sync with
  what's actually in `data_pipeline/`.

---

## 6. Open questions / follow-on decisions — not resolved by this plan, flagged for later

- **Does this ever become the live production feed**, replacing or supplementing Angel One?
  Contingent entirely on §3's result, and a separate, bigger decision even if that test succeeds
  (dual-broker session management, redundancy/failover design, whether to keep both accounts
  long-term).
- **Unified vs. parallel historical datasets** (§2.3) — needs deciding once §1's validation
  result is in hand, not before.
- **Re-opening backtest calibration** (§4's last point) once deeper history exists — the user's
  call, flagged not decided here.
- **CRUDEOIL (full-size contract)** gets the identical treatment to CRUDEOILM throughout this
  plan — no separate open question, just noting it's not an afterthought.
