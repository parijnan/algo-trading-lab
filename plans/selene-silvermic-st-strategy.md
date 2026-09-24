# Plan: Selene — SILVERMIC ST-Based Strategy Discovery

**Status (2026-09-24): Phase 0 and Phase 1 DONE (§2.4 backfill, §3 data assembly, §10 below); Phase 2 sweep code written, not yet run.** Earlier status: DRAFT, Phase 0 partially researched (§1.4/§2.4's liquidity-crossover question resolved 2026-09-22; §1.3/§2.1's tender-margin length and §1.5/§2.5's margin formula both resolved by the user 2026-09-24).** No code written yet. Written 2026-09-22 at the user's request, following the same design trajectory Prometheus used for CRUDEOILM: rollover mechanics research first, then a raw signal sweep, then exit calibration, then position sizing/liquidity, then risk of ruin, then production. Nothing here is committed until each phase's own decision is made, the same way Prometheus's own phases were.

This document mixes two things deliberately: the **plan** (what to do, in what order) and a **research log** of what's already been checked while writing it, since several of the user's own numbered questions turned out to have real, non-obvious answers worth recording now rather than re-deriving later.

---

## 0. Why SILVERMIC

From `research/mcx_liquidity_screen/README.md` (2026-09-22, same session): SILVERMIC is one of the clearly liquid precious-metal contracts — ADV 110,093 lots, 96.1% of session minutes see a trade, median 15-min-boundary volume 98 lots, comparable in character to CRUDEOILM (150 lots) over the same window. It was one of the two contracts the user's own eyeballing had already flagged as liquid (alongside Natural Gas), and the closest analog to CRUDEOILM in tradability among the precious metals.

---

## 1. Structural differences from CRUDEOILM — read this before reusing any Prometheus code wholesale

**1.1 Quarterly contracts, not monthly (user's point #1, confirmed).** SILVERMIC's real expiry cadence (from Fyers' full contract archive) is irregular but roughly bi-monthly-to-quarterly: Nov→Feb (3mo)→Apr(2mo)→Jun(2mo)→Aug(2mo)→Nov(3mo), repeating. AngelOne's live scrip master today (2026-09-22) lists three SILVERMIC contracts simultaneously: `30NOV2026`, `26FEB2027`, `30APR2027` — so unlike crude, several quarters are open at once, and rollover happens roughly 5-6 times a year instead of monthly. This directly lowers how often any rollover-mechanics bug can bite in production, but also means we'll have far fewer *real* rollover events in the backtest window to calibrate against and validate live.

**1.2 Silver is compulsory-delivery, not cash/margin-only — this changes what "getting the rollover window wrong" actually costs (user's point #2, researched).** Crude's `TENDER_ROLL_TRADING_DAYS=5` avoids an elevated **margin** requirement near expiry — a cost, not an obligation, per `prometheus_configs.py`'s own comments. MCX Silver is different: settlement is **physical and delivery is compulsory** — "all open interest positions of the Members at expiry... result into deliverable obligations" ([Angel One](https://www.angelone.in/knowledge-center/commodities-trading/what-is-tender-period-in-mcx)). Holding a SILVERMIC position into/through expiry isn't a margin cost, it's a delivery obligation. This makes the early-roll discipline **more** important here than for crude, not less, even though it fires less often.

**1.3 Tender period length — RESOLVED 2026-09-24 (user-confirmed): the tender margin applies from 5 working days prior to the contract's final expiry date.** So `TENDER_ROLL_TRADING_DAYS=5` carries over from Prometheus as the hard deadline, and §1.4's liquidity crossover (~4-5 trading days) lands on the same number, so the two mechanisms agree rather than conflict. The 3-day figure below is superseded. The original conflicting-sources note is kept for the record: two sources disagreed: one describes SILVERMIC's own tender period as **3 trading days** before and including expiry (shorter than the general figure); a second, describing gold/silver generically, says the tender period **begins 5 trading days** before expiry ([Groww](https://groww.in/blog/what-is-tender-period-in-mcx), [Angel One](https://www.angelone.in/knowledge-center/commodities-trading/what-is-tender-period-in-mcx)). These may both be true (the mini/micro variant's own tender period genuinely differs from the standard contract's), but this needs settling from MCX's own contract specification or circular before it goes anywhere near a production config — not from aggregator blog posts. **This is Phase 0's first concrete task**, not something to guess past.

**1.4 Far-month liquidity during the rollover window — resolved, via Fyers' expired-contract API (user's point #3, corrected after initial local-data read was wrong).** First pass (local AngelOne + already-staged Fyers files) looked like a data gap — the Nov26 contract showed zero volume before 2026-09-02. That read was **wrong, not just incomplete**: it was a downloader-scope artifact (neither `data_downloader_mcx.py` nor the existing Fyers staging tree had ever fetched a contract before it became the tracked front/next month), not evidence about the market. The user pushed back — Fyers' own **expired-contract history API** (`data_downloader_fyers_mcx.py`'s `get_expired_historical_data()`, "Expired F&O Data") can pull any already-expired contract's *full* life directly from Fyers, sidestepping local staging entirely. Re-authenticated (manual browser OAuth via Claude-in-Chrome, same pattern as the original 2026-09-15 setup — Fyers' daily login is manual by design under SEBI's algo framework — `fyers_auth.py`'s headless TOTP path is rejected with `-1025`, not a bug to fix: the user logs in to Fyers in the browser, then the `generate-authcode` redirect's `auth_code` is exchanged for that day's token (expires midnight IST), so a data pull always starts with the user's browser login) and queried it directly:

- **The Aug26-expiry contract had real volume from 2026-01-01** — 8 months before its own expiry — refuting the "no far-month data" read outright. Monthly volume: Jan ₹175k lots-equiv / Feb 59k / Mar 101k (thin but real), then a **hard zero in April–May** that lines up exactly with the already-documented Fyers systemic void (`project_fyers_mcx_integration`: "2026-03-13→2026-06-29") — not a SILVERMIC-specific gap, the same known hole affecting other instruments. June shows a single day (the 29th/30th) before the void's own end, then **July/August jump to ~2.15–2.19M/month, a ~15–20x surge** as the contract nears and becomes front-month.
- **Two clean, void-free historical rollover pairs both show the same sharp crossover shape.** Feb26→Apr26 (pre-void) and Nov25→Feb26 (pre-void): the far contract's volume *share* sits around 25–50% for weeks/months beforehand, then swings decisively to >95% in the **final ~4-5 trading days** before expiry (Feb26 pair: 48.5% five days out → 99.8% on expiry day; Nov25 pair: 51.0% five days out → 99.9% on expiry day). Both independent rollovers land on essentially the same shape and the same rough day-count.
- **This is a real, actionable finding for §2.2**: a ~5-trading-day early-roll threshold — the same magnitude as crude's own `TENDER_ROLL_TRADING_DAYS=5` — looks empirically well-supported by SILVERMIC's own liquidity-crossover data, not just carried over from Prometheus by convention. It doesn't by itself resolve §1.3's tender-*margin* question (a different mechanism than the liquidity crossover, even though the numbers rhyme), but it does mean an early roll at that threshold lands in a genuinely liquid successor contract, not a thin one.
- **Reproducible**: `data_downloader_fyers_mcx.py`'s `resolve_anchor_symbol()` / `get_expiry_dates()` / `get_expired_contract_symbol()` / `get_expired_historical_data()` called directly (wider custom date ranges than `backfill_instrument()`'s own hardcoded 60-day-before-expiry assumption, which is tuned for crude's ~1-month real listing window and undersized for silver's multi-month one) — worth turning into a standing research script rather than redone ad hoc next time.

**1.5 Margin formula — RESOLVED 2026-09-24 (user-confirmed): required capital per unit = `LTP × LOT_SIZE / 8 × 4`** (crude's is `/3 × 4`; the divisor is instrument-specific, `MARGIN_CONTRACT_VALUE_DIVISOR=3` in `_calculate_margin_per_unit()` was never a universal constant, `prometheus_production/README.md` §26). The user reports one SILVERMIC contract's margin is almost the same as CRUDEOILM's, which is a plausibility cross-check on the divisor: silver's higher price × lot size (1 kg) is offset by the larger divisor. In config terms `MARGIN_CONTRACT_VALUE_DIVISOR=8`, `MARGIN_SIZING_MULTIPLIER=4`, and it stays a per-trade calculation off that trade's own entry price like Prometheus's, not a frozen constant.

**1.7 Metrics convention — all percentage returns and derived metrics are computed off this capital-per-unit figure (user's instruction, 2026-09-24).** Capital base for one unit = `entry_price × LOT_SIZE / 8 × 4` (§1.5), so a trade's return % = trade P&L (Rs per unit) ÷ that capital, and every downstream metric (win-rate-weighted return, Calmar, drawdown %, risk-of-ruin drawdown thresholds, dynamic-sizing equity curve) is expressed against it. This differs from Prometheus's raw-signal sweep, which reports in points/percent of price: in Selene the sweep and calibration tables should carry a capital-based return column from the start so multipliers/exits are compared on the same footing the live sizing will use. Because the base moves with each trade's entry price, drawdown % on a fixed-units run is not comparable to a dynamic-sizing run's; keep the two clearly labeled like Prometheus's own no-slippage/slippage split.

**1.6 Units are simpler here.** `mcx_instrument_master.csv`: SILVERMIC `lotsize=1` (1 lot = 1 kg, matching the real contract spec), `tick_size=100` (paise-scaled master convention → real tick ₹1 = 1 index point, confirmed against observed data in the liquidity screen), `freeze_qty=600` (lots, i.e. 600 kg — smaller than CRUDEOILM's 1,000-lot ceiling in absolute terms, worth keeping in mind once position sizing gets to real numbers). Price is already quoted per kg matching `lotsize`'s own unit, so — unlike GOLD/GOLDGUINEA/ALUMINIUM etc. — the naive `LTP × LOT_SIZE` contract-value formula should hold without a hidden unit-conversion factor; still worth a one-line sanity check against a real SILVERMIC margin figure before trusting it (§1.5).

---

## 2. Phase 0 — rollover mechanics research (blocking; do this before any backtest work)

Mirrors how Prometheus's own contract-rollover logic (`TENDER_ROLL_TRADING_DAYS`, the per-date rule, the eve-lookahead) was researched and only then encoded — not guessed and fixed later. Getting this wrong here risks an actual delivery obligation, not just a bad backtest number (§1.2).

1. ~~Confirm SILVERMIC's real tender margin period length~~ — **DONE 2026-09-24**: user confirmed 5 working days prior to final expiry (§1.3). Still worth a passing check that "working days" here means MCX trading days (holiday-aware, per `mcx_holidays.csv`) the same way crude's `TENDER_ROLL_TRADING_DAYS` is counted, so the same counting helper can be reused.
2. **Set the early-roll threshold to 5 trading days** (`TENDER_ROLL_TRADING_DAYS=5`, same as crude). This is now backed by both the confirmed tender-margin start (#1) and §1.4's empirical liquidity crossover, so nothing further blocks it. Roll on or before that day; the far contract is already the liquid one by then.
3. **Confirm the live listing cadence** empirically via the AngelOne scrip master (done once already, §1.1 — re-check periodically, don't assume it's static) rather than assuming a fixed N-month pattern.
4. **DONE 2026-09-24 — see §10.** Backfill the full history via Fyers' expired-contract API, now validated (§1.4) — pull every historical SILVERMIC contract's full life (not just the crude-tuned 60-day-before-expiry window `backfill_instrument()` currently assumes) into the staging tree, ahead of Phase 1's data assembly. This replaces the originally-planned "start monitoring now and wait for the next real rollover" fallback — we don't need to wait, the history already exists and is fetchable today.
5. ~~Validate the margin-per-unit divisor for silver~~ — **DONE 2026-09-24**: `LTP × LOT_SIZE / 8 × 4` (§1.5), with all return/metric calculations based on it (§1.7).

## 3. Phase 1 — data assembly

Same blended-source architecture as `prometheus_backtest/data_loader_p3.py` (user's point #4): Fyers for history, AngelOne for the currently-running contract, front-month resolved empirically per day (whichever contract carried the most volume that day — the same method the MCX liquidity screen already used, and the right one given §1.4's finding that neither source's per-contract file boundary reliably tracks the real listing window). Given the quarterly cadence, expect far fewer distinct contract-file boundaries to stitch across than CRUDEOILM's near-monthly rolls — likely a plus for backtest cleanliness, but re-check for the same rollover-week splicing artifacts Prometheus's own backtest explicitly accepted as a known limitation (`plans/prometheus-phase2-production.md` §1) rather than assuming silver is exempt.

## 4. Phase 2 — raw signal-quality sweep (mirrors Prometheus's own Phase 1/raw sweep)

`ST_PERIOD` × `ST_MULTIPLIER` grid, no SL/target/EOD, across the full available history first (user's point #5) — same shape as `sweep_p3.py`. **Explicitly watch for a regime break the way CRUDEOILM's own Fyers-track work found** (`project_fyers_mcx_integration`: "Prometheus edge is regime-dependent, near-breakeven pre-2026-03") — if the raw sweep's Calmar or win-rate shifts sharply at some date (a rate-cycle turn, a volatility regime change, or simply the boundary where our own usable data starts being trustworthy per §1.4), split the calibration window there rather than calibrating across a mixed regime and hoping it averages out. Don't pre-commit to a split before the data says so.

## 5. Phase 3 — exit calibration

Bespoke 2-lot scale-out, same SL/T1/T2 grid-search methodology as `exit_calib_p3.py`/`bespoke_2lot_p3.py`, once Phase 2 has picked a multiplier (or a small shortlist) worth carrying forward. Re-derive from scratch for silver's own price/volatility character — do not assume CRUDEOILM's calibrated 2.2%/2.2%/5.0% has any reason to transfer.

## 6. Phase 4 — position sizing / liquidity, and Phase 5 — dynamic sizing / risk of ruin

Reuse the CRUDEOILM 2026-09-07 methodology (boundary-minute participation table, tick-cost framing, no fitted slippage coefficient) — a first pass already exists from this session's liquidity screen (median boundary volume 98 lots, 96.1% tradable minutes) and can be refreshed/extended once a real order-size candidate exists to size against. Dynamic-sizing simulation and risk-of-ruin Monte Carlo follow the same per-trade-drawdown methodology as `dynamic_sizing_sim.py`/`risk_of_ruin_p3.py`, once an exit config is decided.

## 7. Phase 6 — production build

Not scoped in detail here — deliberately deferred until the backtest phases above produce a decided config, the same order Prometheus followed (`prometheus-phase2-production.md` → `prometheus-phase3-production.md` only after Phase 2/3's own backtest decisions landed).

## 8. Naming — decided: Selene

Repo convention is Greek mythology, matched to the instrument (`CLAUDE.md`: "Prometheus... the fire-bringer, fitting for a crude oil / energy strategy"). **Selene** — the Titan goddess of the moon — was chosen 2026-09-22: Moon↔Silver is the classical alchemical/planetary metal pairing (Sun↔Gold, Moon↔Silver), the same kind of one-step thematic link Prometheus has to fire/energy. Artemis (also moon-associated) is already taken by the Sensex iron condor.

## 9. Open items / risks carried forward

- Sweep-phase data caveats are in §10 (Fyers void gap-filled from AngelOne without the early roll, early-2021 illiquid stretch excluded, data starts 2021-04-01 — 1,202,324 1-min bars).
- ~~Tender margin period length~~ resolved 2026-09-24: 5 working days (§1.3). ~~Margin divisor~~ resolved 2026-09-24: `/8 × 4` (§1.5). Remaining minor check: confirm the 5 working days is counted on MCX trading days, holiday-aware (§2.1).
- Regime-break risk in the raw sweep untested until Phase 2 actually runs (§4).
- The April–May 2026 Fyers systemic void (§1.4) will need the same naive-fallback/AngelOne-blend handling Prometheus's own `data_loader_p3.py` used for CRUDEOILM's equivalent gap — don't assume it's SILVERMIC-specific or already handled.

## 10. Phase 0 task 4 + Phase 1 — data assembly, DONE 2026-09-24

**What was already there vs. missing.** Fyers history was already staged for SILVERMIC (`data_pipeline/data/mcx_fyers/SILVERMIC/`, 2021-10 → 2026-08), so no full re-backfill was needed. An audit of which source covered each session's *effective* (early-rolled) contract found the real holes were structural, not "no data": the downloader's 60-day-before-expiry window (tuned for crude) left roughly a month a year uncovered (all of September and December, since Nov→Feb and Aug→Nov are 3-month gaps), plus the final ~5 sessions of every contract, where the early roll needs the *next* contract's data. About 200 sessions were affected.

**Fix.** `data_downloader_fyers_mcx.py` gained an opt-in `--extend-history-days N` (existing staging files are extended backward and merged, never re-fetched or overwritten from scratch; default behaviour unchanged). Run once: `--instrument SILVERMIC --months-back 66 --extend-history-days 150`. Contracts now start 2021-01-31 (June-2021 contract) with the Feb/Apr-2021 contracts returning nothing, and an odd short-lived March-2024 contract (640 rows, Dec-2023 only) was deleted because a stray expiry would corrupt the early-roll calendar. Effective-contract coverage went from 882 to 1,193 sessions from Fyers; the September/December holes and the roll-week holes are gone.

**Loader.** `selene_backtest/selene_data_loader.py` reuses `prometheus_backtest/data_loader_p3.py`'s rollover functions (imported, not copied; that module is crude-guarded and untouched) and adds a five-tier per-session source preference (effective contract Fyers → AngelOne → un-rolled contract Fyers → AngelOne → any AngelOne file holding the date). Result: 1,239,424 1-minute bars, 1,452 sessions, 2021-02-01 → 2026-09-23, no gap longer than 4 calendar days; 94% Fyers.

**Fyers void gap fill (user's instruction 3).** Fyers has nothing for SILVERMIC from 2026-04-01 to 2026-06-29. Those ~58 sessions come from AngelOne's local `2026-08-31` file, which reaches back to 2026-01-30 because AngelOne returns the then-front-month contract's real prices under a not-yet-front token. Checked directly against Fyers: its March prices match Fyers' April contract, its July-August prices match Fyers' August contract (85% of 1-min closes identical). Limitation: inside the gap the series stays on the then-front contract; the early roll can't be reproduced there. Every row carries `data_source`.

**Caveats carried into the sweep.**
- Holiday calendar (resolved same day): the user-supplied `data/mcx_holidays_2022_2026.csv` (2021's weekday holidays appended from truedata.in's 2021 MCX list, verified against the 1-min data: every morning-closed day has no bars before 17:00, no unlisted 2021 weekday is missing data) is unioned with production's 2026 `mcx_holidays.csv` for the early-roll trading-day count (`selene_configs.HOLIDAYS_FILE`). Cross-checked against the data: of 24 listed full closures only two have any data (the Diwali muhurat sessions 2022-10-24 and 2024-11-01, irrelevant to any roll window), and the only unlisted weekday without data is 2026-09-01 (tracker starts 09-02). All roll dates came out identical with and without the calendar, including 2021's.
- Feb–Mar 2021 has only the far-dated June-2021 contract, median daily volume ~6,500 lots against 100,000+ afterwards: illiquid, so `selene_configs.DATA_START = 2021-04-01`.
- Rollover-week ST splicing artifacts, and Fyers' zero-volume placeholder bars, are accepted as in Prometheus Phase 3.

**Phase 2 code (written, not yet run).** `selene_backtest/`: `selene_configs.py` (source of truth: multiplier grid 1.0–6.0 in 0.5 steps, `ST_PERIOD=10` held, margin `/8 × 4` recorded for later, `SAVE_TRADE_LOGS=False`), `backtest_selene.py` (verbatim port of Prometheus's raw state machine), `trade_paths_selene.py`, `sweep_selene.py` (writes a per-multiplier summary and a per-year breakdown so a regime break is visible immediately). No return-% calculation, per the user. Module names are prefixed because the Prometheus loader chain does a bare `import configs`.

## 11. Phase 2 — raw signal sweep result, run 2026-09-24

`python selene_backtest/sweep_selene.py` (~3.5 min), ST_PERIOD 10, multipliers 1.0–6.0, 2021-04-01 → 2026-09-23, raw signal only (no SL/target/costs), 1 lot (1 kg). Outputs in `selene_backtest/data_sweep/` (gitignored): `sweep_summary.csv`, `sweep_by_year.csv`, per-multiplier `trade_summary.csv`. P&L is in points = Rs per lot; no return-% by design.

| Mult | Trades | Win % | P&L pts | Avg pts/trade | Avg hold (h) |
|---|---|---|---|---|---|
| 1.0 | 8,063 | 36.3 | 349,306 | 43 | 5.9 |
| 1.5 | 4,945 | 36.7 | 486,190 | 98 | 9.7 |
| 2.0 | 3,405 | 36.9 | 634,388 | 186 | 14.1 |
| 2.5 | 2,471 | 38.9 | 583,253 | 236 | 19.3 |
| 3.0 | 1,950 | 39.7 | 634,381 | 325 | 24.5 |
| 3.5 | 1,628 | 39.7 | 454,943 | 279 | 29.4 |
| 4.0 | 1,346 | 39.7 | 389,255 | 289 | 35.4 |
| 4.5 | 1,129 | 39.8 | 427,442 | 379 | 42.2 |
| 5.0 | 983 | 40.7 | 484,086 | 492 | 48.6 |
| 5.5 | 846 | 42.3 | 459,415 | 543 | 56.3 |
| 6.0 | 755 | 42.8 | 303,565 | 402 | 63.1 |

**Reading.** Every multiplier is net positive over the full window, and every multiplier is positive in 2022–2026; only 2021 (a partial, weakest year) has small losses at 1.0/4.0/4.5. Total P&L is a broad plateau at **2.0–3.0** (2.0 and 3.0 tie at ~634k), falling off on both sides, and win rate rises steadily with the multiplier (36% → 43%) as trades get longer and fewer. Points are not comparable across years because the price rose ~4x (≈65k → ≈260k per kg), so `sweep_by_year` was also read price-normalised (sum of per-trade % move): ex-2026, **2.5 leads (191)**, then 3.0 (173), with 1.5/2.0/6.0 around 155 and 4.0 lowest (102). **2026 dominates every multiplier** (e.g. 2.0: 174 of a 328 total) because silver trended violently, so raw totals overstate what a normal year earns; this is the regime question §4 asked to watch. It is a trend-intensity step-up in 2026 (win rates jump there for mults 2.0–3.5) more than a sharp break like crude's, but it means the full-window ranking is 2026-led.

**Not yet weighed:** costs (1.0–1.5 trade 5,000–8,000 times, the high multipliers under 1,000), drawdown and Calmar (deferred with the return-% work), and the fixed early-roll/fill artifacts noted in §10. Next per §5: exit calibration on a shortlist (2.0, 2.5, 3.0 look like the candidates), and a walk-forward/ex-2026 split first if the ranking's 2026 dependence matters to the decision.

## 12. Phase 3 — exit calibration, and the decided signal + exit config (2026-09-24)

**Decided (user): ST_PERIOD 10, ST_MULTIPLIER 2.5, trend-flip as the only profit exit, plus a wide protective stop-loss of 3.0% of entry price. No profit targets.** Chosen from the shortlist 2.0 / 2.5 / 3.0 (2.0 was kept as the full-window co-leader). Code: `selene_backtest/exit_calib_selene.py` (staged SL → T1 → T2, Prometheus's method) and `exit_structures_selene.py` (stop-loss-only vs. the full 2-lot T1×T2 grid). The exit simulator is a numpy port of `prometheus_backtest/phase3/exit_calib_p3.py`'s per-bar loop, verified identical on 3,750 sampled trade/parameter combinations.

**Findings (2 lots, 2021-04-01 → 2026-09-23, P&L in Rs = points × 1 kg lot):**

| Mult | Raw (no SL, no targets) P&L / Calmar | Staged-calibration winner | Best stop-loss only | Best 2-lot grid |
|---|---|---|---|---|
| 2.0 | 1,268,776 / 8.95 | SL 3.5, T1 1.25, T2 8.0: 895,665 / 9.7 | SL 8: = raw | SL 8, T1 6, T2 8: 1,201,098 / 10.4 |
| **2.5** | **1,166,506 / 13.59** | SL 3.5, T1 3.0, T2 8.0: 1,085,359 / 17.5 | **SL 3.0: 1,189,942 / 14.05** | SL 3, T1 3, T2 12: 1,073,799 / 17.3 |
| 3.0 | 1,268,762 / 8.29 | SL 0.6, T1 3.0, T2 8.0: 969,400 / 23.5 | SL 0.6: 992,429 / 23.9 | SL 0.6, T1 8, T2 12: 1,103,727 / 26.6 |

- The staged calibration landed on its own grid edges (T1 3.0, T2 8.0) and every staged winner earned *less* than the raw signal (9–30% less). An earlier statement that the calibrated exits "help outside 2026" compared a 1-lot raw sweep to 2-lot exit runs and was wrong; on a like-for-like basis the trend-flip exit alone is essentially optimal for P&L. Extra exit rules only trade P&L for drawdown.
- Total P&L rises monotonically as targets move out and as the stop widens; stops under ~2% cost 20–40% of P&L. A stop of 3%+ leaves P&L at raw level.
- Calmar differences are driven by a handful of Jan–Feb 2026 gap trades (stops fill at the gap open, e.g. a short entered 2026-02-20 lost 14.5% of price into a real +5.9% Monday gap despite a 3.5% stop). That risk belongs to position sizing (Phases 4/5), not to exit tuning.
- Multiplier 2.5 is the most consistent: raw Calmar 11.5 before 2026 and 9.3 in 2026, and stable across every exit variant. 2.0 is weak before 2026 (Calmar 4.3); 3.0's best result rests on a fragile 0.6% stop.
- With targets off, lot 1 and lot 2 exit together, so the 2-lot scale-out adds nothing over one lot with the wide stop; treat a "unit" as one lot per unit unless a later phase finds a reason to split.

**What has and hasn't been simulated so far (user asked, 2026-09-24).** Entries and the raw ST signal come from a bar-by-bar state machine on 15-minute bars (`backtest_selene.py`, next-bar-open fills, entry buffer, single position). Exits are overlaid per trade on each trade's 1-minute path, vectorised with numpy. For this design that overlay is exact, because after any exit the system waits for the next flip, and the next flip's entry does not depend on how the previous trade ended. It is **not** a live-mirroring, order-level simulation. Not modelled: contract-roll execution (the series is a spliced continuous one: silver's curve is in steady contango, consecutive contracts 1–3% apart, mean 1.95%; only 25 of 2,471 multiplier-2.5 trades span a splice with both contracts in Fyers, netting −1.5% of raw P&L, gross ±8% by direction, so small but not zero), costs and slippage, position sizing/margin and dynamic sizing, freeze quantity, circuit limits, and the early roll inside the AngelOne-filled 2026-04-01 → 06-29 stretch. The percent-of-capital metrics (§1.7) are also still to do.

**Next per the plan:** Phase 4/5 (liquidity/slippage and dynamic sizing off the `/8 × 4` capital, risk of ruin), which is where the gap tail risk gets handled, then a production-parity backtest with roll-under-open-position mechanics before any production build.

## 13. Production-parity backtest, run 2026-09-24

`python selene_backtest/parity_backtest_selene.py` (~4 min). `parity_backtest_selene.py` is an event-driven simulator of how `prometheus_production/` actually handles contracts (plans/prometheus-phase3-production.md §3–§9, §18), applied to the decided config (multiplier 2.5, 3.0% stop, trend-flip exit, 1 lot, no targets). It trades one real contract at a time, computes each session's ST from that contract's own trailing 18 calendar days (`SEED_DAYS`), and handles rolls the way production does: on the eve of a roll a flat system switches to the new contract at once; an open position tracks both contracts' ST all day, and if the old contract's flip closes it while the new contract flips the same way on the same 15-minute bar it re-enters on the new contract, otherwise it closes and switches; a position still open at the rollover time gets the ST-disagreement veto, then close-old/reopen-new with the stop recalibrated off the historical basis. Fill conventions are the same as every other phase (next-bar open; stop at its level or the gap open; first 1-minute bar of a session exempt; 15-minute entry buffer). Fyers per-contract files only (AngelOne's per-contract files mislabel pre-front-month history).

**Window: 2021-04-01 → 2026-03-31.** Fyers has nothing 2026-04-01 → 06-29 and no history for the unexpired Nov-2026 contract, so per-contract dual tracking is impossible after that. The last open trade at the window's end is excluded.

**Result (same window, same config, 1 lot):**

| | Trades | Win % | P&L (pts) | Max DD (pts) | Calmar | Calmar (% of price) |
|---|---|---|---|---|---|---|
| Production-parity | 2,267 | 38.3 | 469,552 | −42,686 | 11.00 | 14.18 |
| Spliced series (Phases 2/3) | 2,265 | 38.1 | 441,666 | −42,337 | 10.43 | 11.10 |

24 rolls: 17 coincident-flip re-entries, 5 close-and-switch without a coincident flip, 2 carried to the rollover-time fallback (both GO, 2 two-leg trades), 0 vetoed. All 24 were in-trade rolls (the strategy is almost always in the market). Exit legs: 2,253 trend-flip, 14 stop-loss, 2 rollover.

**The spliced backtest was slightly pessimistic, not optimistic (+6.3% P&L in parity).** 2,208 of the 2,235 trades common to both are identical to the point, so per-contract ST reproduces the spliced signal almost exactly (the 18-day seeding window and the removal of the splice jumps change nothing off roll days). Every difference sits within ~3 days of a roll eve. Silver's curve is in contango (consecutive contracts 1–3% apart), so the splice credited bullish trades crossing it a phantom gain (e.g. the 2026-02-19 long: +11,256 spliced vs +2,955 real) and charged bearish trades a phantom loss, and the jump also produced spurious ST flips: 30 spliced-only trades, −19,344 pts in total, against 32 parity-only trades netting −81. Net effect of correct roll handling: +27.9k pts. Rolling itself costs almost nothing here because most rolls coincide with an ordinary flip, so no extra round trip is added; only the two fallback rolls carry an extra leg.

**Validation.** The independent event-driven implementation matches the earlier vectorised pipeline on 99% of trades, which cross-checks both. Not exercised by this data, so untested by it: the flat-switch and stop-loss-on-a-roll-eve branches (no such day occurred), and the NO-GO veto and missing-data paths (0 events). They should get unit tests before any production build.

**Not modelled / caveats.** Missed-rollover recovery (process assumed alive), the shelved 1h entry filter, costs and slippage, sizing. Signals fill at the next bar's open, whereas production acts as soon as the bar closes; this is the convention every Prometheus phase used, so the comparison is like-for-like. Trades from 2026-04 onward are covered by the extension below, with degraded roll handling.

**Extended to 2026-09-23 with AngelOne where Fyers has nothing (user, 2026-09-24).** `python selene_backtest/parity_backtest_selene.py` now defaults to the end of the data. Per-contract frames are Fyers first, AngelOne only for dates Fyers lacks: AngelOne's per-contract files hold their own contract only from 2026-09-02 (Nov-2026/Feb-2027), and before that their rows are the then-front-month contract's real prices, so they are relabelled to that day's front-month contract (April through 04-30, June from 05-01) and used only where Fyers has no row for that contract and date. Where production's contract has no data the simulator falls back to the front month (14 such days), and where a held position's contract is not that day's contract and no production roll handled it, a **forced roll** closes and reopens at real prices (same instant when both contracts trade, else old last close → new first open; direction and trade id kept; stop re-based on the new fill because no historical basis exists). Three forced rolls occurred: 2026-05-01 (Apr→Jun), 2026-06-30 (Jun→Aug), 2026-09-02 (Aug→Nov). What remains impossible: the Apr→Jun and Jun→Aug production rolls (the eve-of-roll dual tracking) inside the void, and Aug→Nov's, because the new contract's prices for those days don't exist in any source; those three rolls happen at expiry, not five trading days early.

| 1 lot, 2021-04-01 → 2026-09-23 | Trades | Win % | P&L (pts) | Max DD (pts) | Calmar | Calmar (% of price) |
|---|---|---|---|---|---|---|
| Parity, all | 2,471 | 39.0 | 620,494 | −42,686 | 14.54 | 17.11 |
| Spliced, all | 2,471 | 38.8 | 594,971 | −42,337 | 14.05 | 13.60 |
| Parity, entries before 2026-04 (Fyers-complete) | 2,268 | 38.3 | 464,914 | −42,686 | 10.89 | 14.08 |
| Parity, entries from 2026-04 (AngelOne-filled) | 203 | 46.3 | 155,580 | −22,767 | 6.83 | 7.40 |
| Spliced, entries from 2026-04 | 206 | 46.1 | 153,305 | −22,767 | 6.73 | 7.26 |

Parity beats the spliced backtest by 4.3% overall (620,494 vs 594,971 pts, equal to 1,240,988 vs 1,189,942 for 2 lots), almost all of it from the Fyers-complete years where roll handling is real; in the AngelOne-filled stretch the two agree to within 1.5%, as expected since neither can model the roll there. Exit legs across the whole window: 2,456 trend-flip, 15 stop-loss, 3 forced-roll, 2 rollover fallback; 5 multi-leg trades. The trade still open at 2026-09-23 is excluded. The decided config (multiplier 2.5, 3.0% stop) stands.

**Per-trade minute logs (2026-09-24).** `parity_trade_logs_selene.py` (also called at the end of `parity_backtest_selene.py`) writes one CSV per trade, 2,471 files (~127 MB), to `selene_backtest/data_sweep/parity_trade_logs/trade_<id>_<entry date>_<HHMM>_<B|S>.csv`: one row per 1-minute bar of the contract actually held, entry to exit, with `unrealised_pts` (realised legs plus the current leg marked to close), running MAE/MFE on total P&L, the current stop level, and an `event` marker on each leg's first and last row. A rolled trade is one file with the hand-over visible in the `contract`/`leg_no` columns. `parity_trades.csv` also gained `final_mae`, `final_mfe` and `hold_hours`. Checked on every trade: final MFE ≥ P&L and final MAE ≥ −P&L, with no exceptions.
