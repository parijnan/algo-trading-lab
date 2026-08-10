# Feeder: Context for a New Monthly Option-Selling Strategy

**Purpose**: this document exists to brief a new design/backtest thread. It describes what Artemis and Athena currently do, and everything the CAS investigation has found that's relevant to designing and risk-managing a new strategy. **It gives no direction on what the new strategy should look like** — that design belongs entirely to the thread that picks this up. Full CAS investigation detail, including all derivations, lives in `plans/closing-auction-session.md` — this document summarizes and points there rather than reproducing it.

---

## 1. Routing status right now — read this first

Neither Artemis nor Athena is currently live. Since 2026-08-06, Leto has been running with **Iris force-routed regardless of VIX**, via an active Slack manual override — not a code change. This override supersedes *both* Artemis's and Athena's routing slots, not just Artemis's.

The underlying auto-routing logic (`leto.py::_route()`), which resumes the moment the override is cleared:
- VIX ≤ 16 → Artemis
- 16 < VIX ≤ 25 → Athena
- VIX > 25 → Iris

If the override lapses on a Monday-Thursday with VIX in the relevant band, Leto will auto-route back to Artemis or Athena with nothing in the routing logic itself guarding against any CAS-era risk described below — none of it was addressed by changing the routing, only by the override.

---

## 2. What Artemis currently does

**Instrument**: Sensex only (weekly options, BFO). **Structure**: iron condor — a PE credit spread and a CE credit spread entered simultaneously.

- **Entry**: both spreads enter together, gated by an entry time window (`entry_window_minutes`, currently 15 min) and a VIX check at entry time (stands down for the week if VIX > `vix_threshold`, currently 16 — this check is skipped when routing is manually forced).
- **Strike placement**: fresh entries are placed at least `minimum_gap` (1000 points, ≈1.27% of spot) from spot. Reopened/adjustment entries (see below) are allowed to sit much closer — re-struck at `minimum_gap_iterator` (400 points, ≈0.51%) if the existing strike isn't already comfortably OTM.
- **Stop-loss, two independent triggers per spread**: `index_sl` (set `sell_strike ∓ index_sl_offset`, currently 200 points inside the strike — a pre-strike buffer meant to exit before the option goes ITM) and `option_sl` (a multiple of entry premium, scaling from 1.33x at 0 DTE to 2.66x at 4 DTE, wider closer to expiry).
- **On an SL breach**: the tested side exits. If the other side is already closed, a fresh spread is opened. If the other side is still active, it's **adjusted** — reinforced with a reopened, closer-to-money entry (the `minimum_gap_iterator` path above). This is the "exit tested side, reinforce the other" transform.
- **ELM handling** (T-1, `elm_time`): if both sides are still active, whichever side has the smaller remaining spread value is closed to manage the 2% Extra Loss Margin. ELM is SEBI-regulatory, not a risk-management lever — not a candidate for SL/sizing optimization.
- **CAS-era addition**: `evaluate_expiry_day_close()` force-closes any still net-open spread at 15:15 on expiry day, ahead of the closing auction — added specifically because CAS removed the continuous-trading window that used to let the position react to an adverse settlement print.
- **Sizing**: 40 lots (20 lot size) as the configured default; a Slack-driven `sizing_override.json` can change this at runtime, currently used to keep Iris at a conservative static size — not applicable to Artemis while it's out of rotation.

## 3. What Athena currently does

**Instrument**: Nifty only (NFO). **Structure**: double calendar condor — short the *second*-nearest weekly expiry, long the nearest monthly expiry (rolled if DTE < `BUY_LEG_MIN_DTE`, currently 16 days) on both CE and PE.

- **Entry**: 10:30 AM, gated by a VIX band check (`VIX_FILTER_LOW`-`VIX_FILTER_HIGH`, currently 16-25) — if VIX is out of range at entry, Athena hands control back to Leto for re-routing rather than standing down silently. Strikes are selected by target delta (0.30 sold, `TARGET_DELTA_SOLD`).
- **Dynamic hedges, not stop-losses** — this is a materially different design philosophy from Artemis's per-leg SL/exit approach:
  - **Emergency CE parachute**: if spot rises to `CE sell strike + 150 points` (`EMERGENCY_TRIGGER_OFFSET`), buys a 0.35-delta monthly CE as upside protection; exits that hedge if spot falls back to the CE sell strike (reversal). Capped at 1 attempt per trade.
  - **Reactive PE wing**: if spot falls 1.75% below entry spot (`REACTIVE_WING_PCT`), buys a 0.05-delta monthly PE; exits it if spot recovers above entry spot. (Static safety wings at entry, `ENABLE_SAFETY_WINGS`, are currently disabled in favor of this reactive approach.)
  - Neither hedge closes the underlying position — they add protective legs and can be entered/exited multiple times as spot moves.
- **Full-position exit** — verified by reading `athena_engine.py` directly, not assumed: **only two automatic paths exist in the live code.** `_execute_exit()` is called from exactly two places: a manual Slack command (`slack_exit`), and a timed exit (`pre_expiry`) at `exit_timestamp`, which is set at entry to `ELM_EXIT_TIME` (10:25) on the last trading day before the *sell* leg's expiry. **There is no separate PnL-target or plain stop-loss full-exit path in the current code** — CLAUDE.md's strategy summary lists "PnL target, SL" among Athena's exits, but that doesn't correspond to anything callable in `athena_engine.py` today; flagging the discrepancy rather than silently resolving it. As currently coded, Athena holds through to the ELM-timed exit (or manual intervention), leaning entirely on the two dynamic hedges above to manage adverse moves in between.
- **Sizing**: 40 lots (65 lot size) default, same Slack override mechanism as Artemis.

Note: Athena already runs a **monthly buy leg** as part of its calendar structure — monthly-expiry mechanics aren't entirely unexplored in this codebase, even though no CAS-specific monthly-expiry data exists yet (§5 below).

---

## 4. CAS mechanics, briefly (full detail: `plans/closing-auction-session.md` §1)

Since 2026-08-03, continuous trading in F&O-eligible stocks and the Nifty/Sensex indices stops at 15:15. A closing auction follows (reference price = VWAP 15:00-15:15; a ±3% band 15:15-15:20; a running equilibrium 15:20-15:25; limit-only 15:25 to a random close between 15:28-15:30) and produces a single equilibrium price — unmatched orders don't fill. Derivatives (options/futures) keep trading to 15:40. **For expiring index derivatives, the settlement price is the auction's cash-index close — not the option's last traded premium or the futures price.** VIX is unaffected (computed from continuously-trading options).

---

## 5. Findings relevant to designing and risk-managing a new strategy

Three structurally distinct risks emerged from the investigation. A monthly design inherits each of them differently depending on what it trades and how it carries positions — stated here as findings, not as design guidance.

### 5.1 Overnight-gap-through-stop (Sensex-specific, any ordinary trading day, applies regardless of expiry cadence)

**Root mechanism, code/data-verified across 3 separate days (18 of 18 stock-days), not inferred**: BSE's own closing auction produces **zero trades** for major Nifty/Sensex-shared heavyweight constituents (HDFCBANK, RELIANCE, ICICIBANK, INFY, TCS, ITC tested) after 15:14 — despite all being marked `is_cas_enabled: True`. NSE's own auction for the same names clears real trades. Sensex's index calculation is consistent with using BSE's own stale last-continuous-trade price (`ClsPric`), not the SEBI-uniform settlement price (`SttlmPric`, which matches NSE's close almost exactly) — so Sensex's close misses same-day information that Nifty's close (fed by NSE's real auction) captures. That information isn't lost; it shows up entirely at **Sensex's next trading day's open**, as a single discontinuous jump with no intermediate window to react.

Quantified: the "shortfall" this produces (essentially NSE's own 15:14-to-terminal auction move, since BSE contributes nothing) tracks the Nifty-vs-Sensex index-level percentage divergence at a ratio of 0.75-1.32 across the 3 days tested — a consistent, non-trivial fraction of the observed divergence, not a marginal effect.

**One concrete incident** (2026-08-05, Artemis): a short Sensex 78800 CE's `index_sl` (78,600, 200 points inside the strike) was breached by **455 points at the opening print alone**; `option_sl` was also breached. The option's own tape sat flat-to-drifting-down through the entire prior evening's CAS window (~165-178, no anticipation) before opening the next day already past both stops — confirming the reference price used by both stop mechanisms was stale, not merely that the market moved fast.

**This is a live-hold risk, not an expiry-specific one** — it applies to any strategy carrying a Sensex position overnight, weekly or monthly.

**Important context, not evidence CAS created a bigger risk than before**: checked against 421 days of pre-CAS Sensex Tue/Wed/Thu overnight gaps, the specific buffer that failed on Aug 5 (an adjustment/reopened leg's thinner strike distance, ~0.255%) was *already* exceeded 42% of the time pre-CAS — a pre-existing structural property of placing a stop close to spot, not something CAS invented. Fresh, far-OTM entries had a comfortable buffer (~1.02%), only exceeded 3.1% of the time pre-CAS. What CAS adds is a new, non-random **mechanism** for producing this kind of gap (described above) — not a provably larger gap on average; CAS-era gap magnitude (n=3-4) isn't statistically distinguishable from the pre-CAS distribution yet.

### 5.2 Expiry-day settlement risk (both indices, own-expiry day only — a monthly strategy touches this ~12×/year instead of ~52×)

Two mechanically distinct events observed, one per index's first CAS-era expiry:
- **Nifty, 2026-08-04 — a moneyness-flip/gamma event.** The 24,500 CE roughly doubled (₹97→₹217, or ₹54→₹115 per an independent source) as spot crossed decisively from OTM to ITM at the settlement print.
- **Sensex, 2026-08-06 — a vega/extrinsic-value-collapse event.** The 78,800 ATM straddle collapsed from 505.95 combined to ~154.65 as settlement-uncertainty resolved, even though the call finished ITM — the extrinsic value built up all morning was paid out and vanished within ~15 minutes.

IV climbed ahead of both expiries (Sensex's climb starting earlier and more continuously than Nifty's late, sharp ramp), reaching extreme levels (~90%+) near the close in both cases. **Caveat on the raw IV numbers**: part of that climb is a known mechanical artifact of 0DTE annualization — a roughly fixed amount of absolute event risk gets compressed into shrinking time-to-expiry, inflating the annualized number independent of whether the market's actual expectation is growing. The settlement outcomes above confirm the risk was real, not just an annualization artifact, but the buildup numbers overstate how much expectation grew relative to how much is just calendar mechanics. A cleaner control (not yet used systematically): compare the expiring series' IV against the *next* expiry's IV at the same clock time, rather than a historical baseline.

Artemis's `evaluate_expiry_day_close()` (force-close at 15:15 on expiry day) removes this specific exposure for Artemis by not carrying a position into the auction at all — at the cost of whatever decay would have been captured in the final minutes.

### 5.3 Intra-auction-window option instability (derivatives layer only, 15:16-15:29, distinct from both risks above)

Independent of the eventual settlement, the option's own price can be unstable *during* the running-equilibrium and limit-only phases. The Sensex 78,800 CE spiked to ~500 (nearly 2x its 15:15 reference) at 15:23 before crashing back by 15:27-15:29. This matters specifically to anyone marking or exiting using live option LTP inside that window — it's not present in how NSE resolves the underlying stock auctions themselves (HDFCBANK/RELIANCE/ICICIBANK's own indicative equilibrium prices were stable through their auctions on the day checked), so it looks concentrated in thin, leveraged derivatives-layer liquidity rather than the cash auction.

### 5.4 What has not been tested — gaps, not findings

- **No CAS-era data exists for monthly expiries at all.** Every finding above is from weekly Nifty (Tuesday) and Sensex (Thursday) expiries. Whether the same mechanisms (overnight gap, settlement event, IV buildup) look the same on a monthly expiry is untested.
- **No CAS-era data exists for month-end, quarter-end, or index-rebalance sessions.** These days plausibly carry more genuine two-sided closing demand than an ordinary session, which could change the BSE-illiquidity mechanism's behavior — flagged as an open question, never investigated.
- **Sensex options only download after expiry** (`options_list_sensex.csv`, one-shot per contract) — monthly-expiry option data, if pulled through the existing pipeline, would arrive on a monthly cadence, not accumulate intraday until the contract has already expired. Relevant to backtest data availability, not a live-trading constraint.
- **Causality remains unresolved.** Everything above is a structural/mechanical finding (thin-to-absent BSE closing-auction liquidity for major names). It does not distinguish "the auction mechanism just works this way" from "someone is deliberately exploiting it." Both readings stay open; the total evidence base is about one week.

---

## 6. CAS-specific data that will continue to be provided

These feeds are already live in production and will keep accumulating without any new work:

- **`data_pipeline/data/cas_auction_tracking.csv`** — one row per trading day per index (Nifty, Sensex): pre-auction close (~15:15), terminal auction close, move in points and percent. Updated automatically by the existing daily production cron, zero extra API calls.
- **`data_pipeline/data/cas_gap_fade_tracking.csv`** — one row per trading day per index: that day's own open/low/high/close paired against the *prior* day's auction move. Same cron.
- **`data_pipeline/data/cas_market_watch/YYYY-MM-DD.csv`** — a full reconstruction of the auction's evolution: 15-second polls from 15:14 to 15:36, one row per (poll, symbol), covering **all 208 NSE F&O-eligible stocks** — reference price, indicative equilibrium price, imbalance quantities, status transitions, full OHLC. Separate cron (`nse_cas_market_watch.py`), synced to local via `datasync`. **NSE-only — no BSE equivalent exists** (searched for one; BSE's own Market Watch page is an obfuscated SPA that couldn't be mapped via static analysis). This is the one gap in the ongoing feed most relevant to a Sensex-facing strategy: there's no equivalent constituent-level auction reconstruction for BSE's own side.
- **Official NSE/BSE bhavcopy** (`nsearchives.nseindia.com`, `bseindia.com`) — public, no-auth, fetchable on demand for any historical date; not automatically archived, but this is how the `ClsPric`/`SttlmPric` mechanism (§5.1) was cross-checked and can be re-checked for any future date.
- **`plans/closing-auction-session.md`** itself — the full investigation, updated as new CAS-era days accumulate and new findings land. This feeder document is a snapshot; that one keeps moving.

---

*This document reflects the codebase and investigation state as of 2026-08-10. No recommendation on strategy design, instrument choice, or risk parameters is made or implied.*
