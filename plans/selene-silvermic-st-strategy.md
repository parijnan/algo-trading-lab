# Plan: Selene — SILVERMIC ST-Based Strategy Discovery

**Status: DRAFT, Phase 0 partially researched (§1.4/§2.4's liquidity-crossover question resolved same day; §1.3/§2.1's tender-margin length still open).** No code written yet. Written 2026-09-22 at the user's request, following the same design trajectory Prometheus used for CRUDEOILM: rollover mechanics research first, then a raw signal sweep, then exit calibration, then position sizing/liquidity, then risk of ruin, then production. Nothing here is committed until each phase's own decision is made, the same way Prometheus's own phases were.

This document mixes two things deliberately: the **plan** (what to do, in what order) and a **research log** of what's already been checked while writing it, since several of the user's own numbered questions turned out to have real, non-obvious answers worth recording now rather than re-deriving later.

---

## 0. Why SILVERMIC

From `research/mcx_liquidity_screen/README.md` (2026-09-22, same session): SILVERMIC is one of the clearly liquid precious-metal contracts — ADV 110,093 lots, 96.1% of session minutes see a trade, median 15-min-boundary volume 98 lots, comparable in character to CRUDEOILM (150 lots) over the same window. It was one of the two contracts the user's own eyeballing had already flagged as liquid (alongside Natural Gas), and the closest analog to CRUDEOILM in tradability among the precious metals.

---

## 1. Structural differences from CRUDEOILM — read this before reusing any Prometheus code wholesale

**1.1 Quarterly contracts, not monthly (user's point #1, confirmed).** SILVERMIC's real expiry cadence (from Fyers' full contract archive) is irregular but roughly bi-monthly-to-quarterly: Nov→Feb (3mo)→Apr(2mo)→Jun(2mo)→Aug(2mo)→Nov(3mo), repeating. AngelOne's live scrip master today (2026-09-22) lists three SILVERMIC contracts simultaneously: `30NOV2026`, `26FEB2027`, `30APR2027` — so unlike crude, several quarters are open at once, and rollover happens roughly 5-6 times a year instead of monthly. This directly lowers how often any rollover-mechanics bug can bite in production, but also means we'll have far fewer *real* rollover events in the backtest window to calibrate against and validate live.

**1.2 Silver is compulsory-delivery, not cash/margin-only — this changes what "getting the rollover window wrong" actually costs (user's point #2, researched).** Crude's `TENDER_ROLL_TRADING_DAYS=5` avoids an elevated **margin** requirement near expiry — a cost, not an obligation, per `prometheus_configs.py`'s own comments. MCX Silver is different: settlement is **physical and delivery is compulsory** — "all open interest positions of the Members at expiry... result into deliverable obligations" ([Angel One](https://www.angelone.in/knowledge-center/commodities-trading/what-is-tender-period-in-mcx)). Holding a SILVERMIC position into/through expiry isn't a margin cost, it's a delivery obligation. This makes the early-roll discipline **more** important here than for crude, not less, even though it fires less often.

**1.3 Tender period length — conflicting figures found, needs MCX's own circular before picking a number.** Two sources disagree: one describes SILVERMIC's own tender period as **3 trading days** before and including expiry (shorter than the general figure); a second, describing gold/silver generically, says the tender period **begins 5 trading days** before expiry ([Groww](https://groww.in/blog/what-is-tender-period-in-mcx), [Angel One](https://www.angelone.in/knowledge-center/commodities-trading/what-is-tender-period-in-mcx)). These may both be true (the mini/micro variant's own tender period genuinely differs from the standard contract's), but this needs settling from MCX's own contract specification or circular before it goes anywhere near a production config — not from aggregator blog posts. **This is Phase 0's first concrete task**, not something to guess past.

**1.4 Far-month liquidity during the rollover window — resolved, via Fyers' expired-contract API (user's point #3, corrected after initial local-data read was wrong).** First pass (local AngelOne + already-staged Fyers files) looked like a data gap — the Nov26 contract showed zero volume before 2026-09-02. That read was **wrong, not just incomplete**: it was a downloader-scope artifact (neither `data_downloader_mcx.py` nor the existing Fyers staging tree had ever fetched a contract before it became the tracked front/next month), not evidence about the market. The user pushed back — Fyers' own **expired-contract history API** (`data_downloader_fyers_mcx.py`'s `get_expired_historical_data()`, "Expired F&O Data") can pull any already-expired contract's *full* life directly from Fyers, sidestepping local staging entirely. Re-authenticated (manual browser OAuth via Claude-in-Chrome, same pattern as the original 2026-09-15 setup — the TOTP-headless path in `fyers_auth.py` is currently broken, `-1025 invalid request` at step 1, unrelated to this check) and queried it directly:

- **The Aug26-expiry contract had real volume from 2026-01-01** — 8 months before its own expiry — refuting the "no far-month data" read outright. Monthly volume: Jan ₹175k lots-equiv / Feb 59k / Mar 101k (thin but real), then a **hard zero in April–May** that lines up exactly with the already-documented Fyers systemic void (`project_fyers_mcx_integration`: "2026-03-13→2026-06-29") — not a SILVERMIC-specific gap, the same known hole affecting other instruments. June shows a single day (the 29th/30th) before the void's own end, then **July/August jump to ~2.15–2.19M/month, a ~15–20x surge** as the contract nears and becomes front-month.
- **Two clean, void-free historical rollover pairs both show the same sharp crossover shape.** Feb26→Apr26 (pre-void) and Nov25→Feb26 (pre-void): the far contract's volume *share* sits around 25–50% for weeks/months beforehand, then swings decisively to >95% in the **final ~4-5 trading days** before expiry (Feb26 pair: 48.5% five days out → 99.8% on expiry day; Nov25 pair: 51.0% five days out → 99.9% on expiry day). Both independent rollovers land on essentially the same shape and the same rough day-count.
- **This is a real, actionable finding for §2.2**: a ~5-trading-day early-roll threshold — the same magnitude as crude's own `TENDER_ROLL_TRADING_DAYS=5` — looks empirically well-supported by SILVERMIC's own liquidity-crossover data, not just carried over from Prometheus by convention. It doesn't by itself resolve §1.3's tender-*margin* question (a different mechanism than the liquidity crossover, even though the numbers rhyme), but it does mean an early roll at that threshold lands in a genuinely liquid successor contract, not a thin one.
- **Reproducible**: `data_downloader_fyers_mcx.py`'s `resolve_anchor_symbol()` / `get_expiry_dates()` / `get_expired_contract_symbol()` / `get_expired_historical_data()` called directly (wider custom date ranges than `backfill_instrument()`'s own hardcoded 60-day-before-expiry assumption, which is tuned for crude's ~1-month real listing window and undersized for silver's multi-month one) — worth turning into a standing research script rather than redone ad hoc next time.

**1.5 Margin formula — don't assume crude's `/3` divisor carries over.** `MARGIN_CONTRACT_VALUE_DIVISOR=3` in `_calculate_margin_per_unit()` was checked by the user against CRUDEOILM's and CRUDEOIL's own real margin requirements specifically (`prometheus_production/README.md` §26) — it is not a universal constant. Silver's real MCX margin percentage needs its own check before reusing the formula shape (`entry_price × LOT_SIZE / X × Y`) for this strategy.

**1.6 Units are simpler here.** `mcx_instrument_master.csv`: SILVERMIC `lotsize=1` (1 lot = 1 kg, matching the real contract spec), `tick_size=100` (paise-scaled master convention → real tick ₹1 = 1 index point, confirmed against observed data in the liquidity screen), `freeze_qty=600` (lots, i.e. 600 kg — smaller than CRUDEOILM's 1,000-lot ceiling in absolute terms, worth keeping in mind once position sizing gets to real numbers). Price is already quoted per kg matching `lotsize`'s own unit, so — unlike GOLD/GOLDGUINEA/ALUMINIUM etc. — the naive `LTP × LOT_SIZE` contract-value formula should hold without a hidden unit-conversion factor; still worth a one-line sanity check against a real SILVERMIC margin figure before trusting it (§1.5).

---

## 2. Phase 0 — rollover mechanics research (blocking; do this before any backtest work)

Mirrors how Prometheus's own contract-rollover logic (`TENDER_ROLL_TRADING_DAYS`, the per-date rule, the eve-lookahead) was researched and only then encoded — not guessed and fixed later. Getting this wrong here risks an actual delivery obligation, not just a bad backtest number (§1.2).

1. **Confirm SILVERMIC's real tender *margin* period length** directly from MCX's own contract specification / circular (mcxindia.com), not aggregator sites. Resolve the 3-day vs. 5-day conflict found in §1.3 — this is the compulsory-delivery trigger, a distinct question from §1.4's liquidity-crossover finding even though both point to a similar day-count.
2. **Pick the early-roll threshold.** §1.4's own data gives real empirical grounding: liquidity crosses over to the far contract in the final ~4-5 trading days before expiry, consistent across two independent historical rollovers. Recommend ~5 trading days (same as crude's `TENDER_ROLL_TRADING_DAYS`) pending #1's confirmation that this also clears the real tender-margin trigger with room to spare — don't finalize until both checks agree.
3. **Confirm the live listing cadence** empirically via the AngelOne scrip master (done once already, §1.1 — re-check periodically, don't assume it's static) rather than assuming a fixed N-month pattern.
4. **Backfill the full history via Fyers' expired-contract API**, now validated (§1.4) — pull every historical SILVERMIC contract's full life (not just the crude-tuned 60-day-before-expiry window `backfill_instrument()` currently assumes) into the staging tree, ahead of Phase 1's data assembly. This replaces the originally-planned "start monitoring now and wait for the next real rollover" fallback — we don't need to wait, the history already exists and is fetchable today.
5. **Validate the margin-per-unit divisor for silver** (§1.5) against a real MCX margin figure before any sizing work assumes crude's ratio carries over.

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

- Tender *margin* period length unresolved (§1.3/§2.1) — blocking for any production rollover logic, not for backtest work. (Distinct from §1.4's liquidity-crossover finding, which is resolved.)
- Margin divisor unvalidated for silver (§1.5/§2.5).
- Regime-break risk in the raw sweep untested until Phase 2 actually runs (§4).
- The April–May 2026 Fyers systemic void (§1.4) will need the same naive-fallback/AngelOne-blend handling Prometheus's own `data_loader_p3.py` used for CRUDEOILM's equivalent gap — don't assume it's SILVERMIC-specific or already handled.
