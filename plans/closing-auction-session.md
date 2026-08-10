# Plan: Closing Auction Session (CAS) — Adaptation & Investigation

**Status: System adaptation COMPLETE and live (2026-08-04, commits `4422d0e`, `660a7fa`). Artemis kept out of rotation since 2026-08-05 via an active Slack routing override — NOT a code-level disable; if the override lapses without a replacement, Leto auto-routes back to Artemis on any VIX≤16 Monday-Thursday with none of the CAS risks addressed (see §3.1). Overnight gap-through-stop risk (§3) remains unresolved, not on any accepted-risks list. Tracking infrastructure (auction-move log, gap-fade log, live NSE Market Watch poller) shipped 2026-08-05, confirmed working end-to-end in its first live production run 2026-08-06 (commits `2172018`, `b67b445`, `5dbc312`, `97af765`; see §7.3). Backtest/live parity gap (accepted-risk §2.3) closed 2026-08-06 (commit `95a99b2`). **2026-08-06: Iris deployed as the sole live strategy**, force-routed regardless of VIX (static 1-lot first run, scaling planned) — see §9. Sensex's first expiry day under CAS (2026-08-06) produced a materially larger auction move than Nifty's, plus a dramatic options-premium collapse at settlement — see §4.8. **2026-08-07 broke the multi-day overnight gap-up streak for both indices for the first time since CAS launched** (both gapped *down* the morning after the prior auction), while the auction session itself was the quietest yet at both the index and constituent level — see §4.9. External corroboration from three independent sources (@Am_Shai ×3, Zerodha Varsity) supports the thin-liquidity mechanism, including an independent NSE-vs-BSE "separate market regime" conclusion (§5.3) reached from different data than ours; says nothing about deliberate manipulation. Only 5 trading days of evidence — do not overstate any conclusion. **§10 (2026-08-07): comprehensive Artemis risk report — Artemis's real CAS exposure is the Sensex overnight gap, not the Nifty auction print most of this file documents. The Aug 5 incident's `index_sl` breach is reconstructed exactly from code (455 points past the stop at the open); the adjustment leg's thin spot-to-stop distance (~0.255%) is shown, against 421 days of pre-CAS history, to be a pre-existing structural exposure (42% pre-CAS exceedance) that CAS did not create; and the option tape for the actual incident contract confirms CAS's real contribution is a new *mechanism* for overnight gaps (Sensex's own close missing information already visible in Nifty's print, per §4.4-4.6), not a provably larger gap. **2026-08-08: the ClsPric mechanism now replicates cleanly across all 3 days tested** (weekend AngelOne pull, no live-strategy conflict) — 18 of 18 stock-days confirm zero BSE trades after 15:14 (extends §4.4 from 2 days), and NSE's own auction move (the effective shortfall, since BSE contributes nothing) tracks the Nifty-vs-Sensex index divergence at a ratio of 0.75-1.32 across Aug 3/4/5, resolving the earlier bhavcopy-only finding that only worked for Aug 3. **2026-08-10: Vtrender's own long-form writeup (§5.4) adds a real methodological point** — part of §4.8's IV climb is a known 0DTE annualization artifact (fixed event risk compressed into shrinking time), not purely growing fear; doesn't change the settlement outcome, sharpens how the buildup should be read. VIX closed 12.16 on Aug 7 (last trading day), inside Artemis's routing band — only the active override keeps it out.**

---

## 1. What CAS is

NSE/BSE's Closing Auction Session went live 2026-08-03 for all F&O-eligible stocks and their indices (Nifty, Sensex). Mechanics (precise version, from the Zerodha Varsity thread — see §5):

- Continuous trading ends 15:15.
- **Reference price = VWAP of trades between 15:00 and 15:15** (not simply the last tick).
- 15:15–15:20: a **±3% band** is drawn around the reference price.
- 15:20–15:25: limit and market orders accepted but **none matched yet** — both feed a running indicative equilibrium price.
- 15:25 until a random close (15:28–15:30): only limit orders can be entered/modified/cancelled; market orders frozen.
- At close: order book freezes, exchange computes the equilibrium price (max executable volume; ties broken by minimum imbalance), matches all executable orders at that single price, sends confirmations by 15:35. **Unmatched orders are not filled at all.**
- Derivatives (options/futures) keep trading continuously to 15:40, without an auction pause of their own.
- VIX is unaffected — computed from the continuously-trading options order book.
- If the market closes early on a circuit-breaker day, CAS is skipped entirely and the close falls back to VWAP of the last 30 minutes (or LTP).

SEBI's stated rationale for introducing CAS (replacing the old VWAP close), per its own consultation papers: investors couldn't trade *at* the VWAP closing price; passive funds faced tracking differences; large orders went unfilled on rebalance/expiry days; concentrated trading amplified volatility near the close; most major markets already use closing auctions.

---

## 2. System adaptation (shipped 2026-08-04, commits `4422d0e`, `660a7fa`)

- `data_pipeline/data_downloader_angelone.py`: `fill_missing_candles` flat-fills the mid-day auction gap in `nifty.csv`/`sensex.csv`, preserving the terminal print's real OHLC rather than overwriting it (the old open-correction logic would have produced an invalid open-outside-high/low candle). New `extend_to_day_close` extends each day to 15:39 via flat carry-forward at the terminal print's close. Options fetch window widened 15:30→15:40.
- `nifty.csv`/`sensex.csv` backfilled for Aug 3-4 with this logic; verified against `nifty_daily.csv` and external sources.
- Artemis/Athena `closing_time`/`MARKET_CLOSE` bumped 15:30→15:40 to match the extended derivatives session.
- New Artemis-only rule: `evaluate_expiry_day_close()` (`iron_condor.py`) force-closes any still net-open spread at 15:15 on expiry day — distinct from the existing T-1 ELM mechanism. First live test was meant to be the 2026-08-06 Sensex expiry but Artemis was disabled first (see §3), so that test never happened.
- `websocket_feed.py`: stale-tick watchdog suppressed for Nifty/Sensex spot tokens only (not VIX, not options) during 15:15-15:40 — this was spamming `#error-alerts` every day post-CAS otherwise.
- Iris investigated and found already CAS-safe by design (`MAX_ENTRY_TIME=15:00`, wall-clock `EXIT_BY_TIME=15:15` trading the option leg's own LTP, never touches index data that late) — no code change needed.

**Risks surfaced and explicitly accepted 2026-08-04** (deliberate calls, not oversights):
1. **Index-referenced triggers go blind 15:15-15:40** — Artemis's `index_sl` and Athena's parachute/PE-wing triggers compare against a frozen ~15:14 index value in that window; `option_sl` still works normally. Decision: leave silent, bank on `option_sl`.
2. Apollo untouched (retired from routing — open-position-resume only, safely ignored).
3. Backtest/live parity broken on Artemis's expiry-day exit (backtests still assume ≤15:30 close). Decision: accepted for now; backtests to be retroactively updated eventually. **DONE 2026-08-06 (commit `95a99b2`)** — `artemis_backtest/backtest.py`/`configs.py` now mirror production's `evaluate_expiry_day_close()`, force-closing any still-open spread at 15:15 on expiry day for CAS-era weeks (`CAS_EFFECTIVE_DATE` gate), while pre-CAS weeks keep the original ≤15:30 exit. Landed via a separate chat session in the same repo; verified by reading the diff before stash/pull/commit/push.
4. Synthetic terminal-print jump in the index CSVs will show as a spurious single-bar ~0.6-0.8% move to any per-minute-return research — flagged, no consumer audit done.
5. `sensex_daily.csv` update lag after CAS — root-caused as a BSE EOD-publish delay through AngelOne, not a bug; self-heals via the existing incremental-fetch design.

---

## 3. Live incident and Artemis's disabled status

**2026-08-05: user closed a CE position manually (net loss) and disabled Artemis** — no fixed re-enable date ("probably keep it disabled next week as well till we figure out what's happening").

**Reconstructed technical detail of the incident**: short the 78800 CE at ~145 entry. Both `index_sl` and `option_sl` were breached **at market open**, and the breach held through the code's existing 9:16 AM confirmation gate (`credit_spread.py::monitor_spread()`, lines ~948-995 — any SL breach detected before 9:16 is deferred, LTP re-fetched at 9:16, only acted on if still breached; built for the pre-existing NSE/BSE opening call-auction noise, not CAS-specific, but incidentally filters some CAS-catchup noise too). Code-verified: for a CE spread, `index_sl` is checked and returned **before** `option_sl` is ever evaluated (short-circuit) — so `index_sl` was the actual trigger, not `option_sl`, despite both being breached.

**Important distinction from accepted-risk #1 above**: that risk is specific to the 15:15-15:40 freeze window, when the index stops ticking. This incident happened **at the open**, when the index is fully live — `index_sl` was not blind; it correctly detected a real, valid move (the overnight catch-up of the prior day's CAS-distorted close — see §4). The SL infrastructure worked exactly as designed.

**The actual new failure mode**: not SL blindness, but the classic gap-through-stop problem — a stop-loss protects by detecting a breach, but doesn't cap the loss size when price gaps straight past the threshold overnight rather than grinding toward it intraday. CAS's daily close-freeze-then-catchup pattern turns this from a rare tail event into something structurally likely on any morning following a day where the auction distorted the prior close. **This is the first live confirmation of a risk raised theoretically the same session, before the incident was reported.**

The strategy's automatic PE-side reinforcement (iron condor transform logic: exit tested side, reinforce the other) fired as designed — not a malfunction. User closed it manually on a bearish intuition, avoiding unwanted net-bullish exposure.

**Status: this gap-through-stop risk is explicitly NOT YET added to any accepted-risks list.** The user has said so directly — still thinking through mitigations. Do not mark it accepted without being told to. It is a second, distinct blocking concern for Artemis's eventual re-enable, separate from the manipulation-theory investigation below — even if that investigation resolves favorably, this risk needs its own answer (wider SL buffers sized for a known structural overnight gap, reduced overnight sizing, or a rule against carrying Sensex positions through a CAS-affected close).

### 3.1 Correction, 2026-08-06: "disabled" is not a code-level state

User corrected the framing used throughout this document and earlier chat turns: Artemis being out of rotation is **not** an explicit disable anywhere in the code. Verified by reading `leto.py::_route()` directly — with no manual override active, auto-routing (Mon-Thu) falls through unconditionally to `if vix <= VIX_ARTEMIS_MAX: ... Routing to Artemis` (~lines 424-426). The *only* thing keeping Artemis out right now is the active Slack manual-override (`routing_state.json` on delos, currently forcing Iris — see §9). **If that override is ever cleared without a replacement, and it's a Monday-Thursday with VIX ≤ 16, Leto will auto-route straight back to Artemis** — with every risk in §2 (accepted) and §3 (unresolved) fully intact, since nothing was added to the routing logic itself to guard against it. Treat "Artemis is disabled" as shorthand for "kept out via an override the user is actively maintaining," not a structural safeguard.

---

## 4. Measured evidence: what's actually happening

### 4.1 Index-level auction moves (own data, `cas_auction_tracking.csv`)

| Date | Index | Move | Notes |
|---|---|---|---|
| Aug 3 | Nifty | +0.82% | Non-expiry day |
| Aug 3 | Sensex | −0.05% | Flat |
| Aug 4 | Nifty | +0.62% | Nifty's own weekly expiry day |
| Aug 4 | Sensex | +0.13% | Flat |
| Aug 5 | Nifty | +0.22% | Third straight positive day, shrinking magnitude |
| Aug 5 | Sensex | +0.10% | First clearly positive Sensex reading |
| Aug 6 | Nifty | +0.03% | Fourth straight positive day, smallest yet — non-expiry day for Nifty |
| Aug 6 | Sensex | +0.21% | **Sensex's own first expiry day under CAS — largest Sensex move of the 4 days, breaking its own flat pattern** |
| Aug 7 | Nifty | +0.06% | Streak of shrinking magnitude broke — ticked up from Aug 6, though still small |
| Aug 7 | Sensex | +0.01% | Smallest Sensex move of all 5 days, including day one |

Nifty's shrinking-magnitude pattern held through 4 days (0.82% → 0.62% → 0.22% → 0.03%) but **broke on day 5** — Aug 7 ticked back up to +0.06%, still tiny in absolute terms but no longer monotonically decaying. @Am_Shai's 2026-08-05 follow-up post (§5.1) independently reported nearly identical numbers through day 4 and offered a mechanism: growing market awareness causing shorts to close earlier (from ~14:30) and funds to pre-position ahead of the close, diluting each day's surprise. Self-reported that he called this on day one, not independently verified, but the mechanism is plausible — the day-5 uptick doesn't necessarily contradict it (still an order of magnitude below the day-1/2 moves), but it does mean "shrinking every day" was too strong a claim.

**Sensex broke the opposite way on its own expiry day, then reverted to its quietest reading yet.** Aug 6's auction move (+0.21%) was Sensex's largest of the period — about 6.6x Nifty's same-day move (§4.8, foreshadowed by the entire morning's rolling-ATM IV climb, not a surprise at the close). Aug 7 (a non-expiry day) reverted sharply, to its smallest move of the whole 5-day sample (+0.01%) — consistent with the elevated Aug 6 reading being expiry-specific rather than a new baseline.

### 4.2 Next-day gap-fade pattern (`cas_gap_fade_tracking.csv`)

Through Aug 5, both indices consistently opened near the day's high (reflecting the prior day's auction print) and drifted lower the rest of the session:

| | Aug 4 open→low | Aug 5 open→low | Aug 4 open→close | Aug 5 open→close |
|---|---|---|---|---|
| Nifty | −1.12% | −0.69% | −0.36% | −0.18% |
| Sensex | −1.16% | −0.97% | −0.89% | −0.60% |

**This pattern doesn't apply to Aug 6→7 the same way, because Aug 7 didn't open near a high — see §4.9.** The open→low/open→close framing assumed a gap-up open; Aug 7 opened *down* instead, so these two columns aren't directly comparable to the Aug 4/5 rows. Full detail in §4.9.

### 4.3 Futures vs. cash auction print (own data, ad-hoc AngelOne pull)

Over the exact 15:14→15:29 window on Aug 3 (non-expiry day, cleanest reading — the Aug 4 number is confounded by ~35 pts/day of cost-of-carry noise in the monthly future used): the Nifty **future** moved +10 points while the **cash auction print** moved +200.95 points. Same clock, same underlying, one side continuously traded and one side frozen except for the single print. Sensex showed no equivalent divergence either day (future essentially flat, matching Sensex's own flat/noisy auction prints) — the effect looks Nifty-specific.

### 4.4 Constituent-level NSE vs. BSE (own data, 6 heavyweight stocks: HDFCBANK, RELIANCE, ICICIBANK, INFY, TCS, ITC)

- **NSE**: 11 of 12 stock-days (both Aug 3 and Aug 4) printed a single higher price at the post-gap tick (15:28/15:29), one flat. Range +0.19% to +1.01%, no negatives, across six unrelated sectors.
- **BSE: zero trades recorded in any of the 6 stocks after 15:14, both days, no exceptions** — not thinner, literally no printed volume for the rest of the session, immediately after trading at normal volume (500-5,700 shares/min) right up to 15:14.
- This reframes the Nifty-vs-Sensex divergence: likely not "two competing fair-value views, one honest" so much as "BSE's own closing auction isn't producing real price discovery for its heaviest constituents at all, while NSE's auction is actually trading and consistently clears higher."
- **Extended to a 3rd day, 2026-08-08**: re-pulled the same 6 stocks' intraday ticks for Aug 5 (§10.4). Same result — BSE's last print all 6 stocks was at 15:14 again, **18 of 18 stock-days now with zero post-15:14 BSE trades**, no exceptions across any day tested. All 6 BSE listings are marked `is_cas_enabled: True` in the scrip master — this is CAS being enabled and clearing zero volume, not CAS being unavailable for these names.

### 4.5 Next-session convergence (own data)

Checked both transitions (Aug 3→4, Aug 4→5). **Convergence is instant, not gradual** — BSE's opening auction the next session re-prices immediately using overnight information; it does not visibly chase NSE's price up over the morning. Practical conclusion: the CAS-window divergence is fully contained within the auction itself and leaves no observable next-day dislocation to trade (forecloses any "buy the BSE laggard overnight" idea).

### 4.6 Bhavcopy cross-check (own data, official NSE/BSE EOD files)

BSE's own bhavcopy carries two closing reference prices per stock: `ClsPric` (matches the stale, no-trade price already measured — confirms it wasn't a feed artifact) and `SttlmPric` (nearly identical to NSE's close, every time — likely a pre-existing SEBI uniform-closing-price rule for derivative-eligible stocks, not something new to CAS; not fully verified). What IS solidly supported regardless: since Sensex's own index move stayed flat both days, the index calculation must use `ClsPric`, not `SttlmPric` — explains why Sensex looks flat despite its own heaviest constituents clearly moving.

### 4.7 Causality caveat (repeated deliberately throughout)

All of the above measures a **structural/mechanical** pattern — thin sell-side auction liquidity producing a one-directional clearing price on NSE, and no clearing price at all on BSE for these names. It does **not** distinguish "the auction mechanism just works this way" from "someone is doing this deliberately." Keep these two claims separate. Only ~3 days of evidence exist; the market was already on a multi-day up-run before CAS started, so "auction-structural bias" vs. "ordinary bullish momentum" isn't yet cleanly separable statistically. (Applies equally to §4.8 below.)

### 4.8 Sensex's own expiry day, 2026-08-06 — options IV and settlement behaviour

Own data throughout, via live AngelOne pulls (rolling-ATM strike recomputed at each checkpoint, not held fixed — a fixed strike drifting out of moneyness was caught and corrected mid-session) plus the newly-landed local Sensex options for the 2026-08-06 expiry and the Nifty options for the 2026-08-04 expiry (ICICI Breeze, landed 2026-08-06).

**IV climbed through the whole session, well ahead of the close** — a materially different shape than Nifty's own expiry day:

| Time | Nifty Aug 4 (own expiry) avg IV | Sensex Aug 6 (own expiry) avg IV |
|---|---|---|
| ~09:15-11:30 | flat, 15.75%-19.61% (matches pre-CAS baseline) | — |
| 09:45 | — | 39.25% |
| ~12:00 | 24.07% | — |
| 10:08 | — | 44.58% |
| ~14:00 | 35.58% | — |
| 11:31 | — | 52.75% |
| ~15:00 (pre-auction) | **89.97%** | building further into the close |

Pre-CAS baseline (last 3 pre-CAS Thursday expiries, same time-of-day) was ~17.5%-20.5%, cross-checked against India VIX being flat-to-lower than baseline on both days — ruling out generic market-wide vol as the explanation.

**Nifty's shape: flat for ~3 hours, then a late, sharp ramp** (34.5%→90% from 14:00 to 15:00 alone). **Sensex's shape: rising from the first reading, no flat stretch at all.** Plausible read: the market learned from Nifty's Aug 4 event (the first live demonstration of CAS expiry-settlement violence) and priced Sensex's own first exposure in earlier and more continuously, rather than waiting for the final hour.

**Settlement outcome confirms the risk was real, not just anticipated.** The Sensex 78800 ATM straddle was priced at 505.95 (CE 300.90 + PE 205.05) at 15:15; by the time it settled (stable from 15:30 onward) it had collapsed to ~154.65 — CE settling almost exactly at its own intrinsic value (78954.76 − 78800 = 154.76) and PE collapsing to near-zero (spot finished well above strike). All the extrinsic/time value built up all morning was paid out and vanished within ~15 minutes, exactly as option theory predicts once a settlement price becomes known. Mechanically distinct from Nifty's Aug 4 case (24500 CE ~doubled because spot crossed decisively from OTM to ITM) — here the dominant effect was extrinsic-value collapse, not a moneyness flip, even though the call did finish ITM.

**A separate, smaller finding: the option's own price was unstable inside the 15:16-15:29 window, independent of the eventual settlement.** The 78800 CE spiked to a high of 500 at 15:23 — nearly double the 15:15 reference — before crashing back down by 15:27-15:29. This is the option tape gyrating during the running-equilibrium and limit-only phases (§1 mechanics), a distinct risk dimension from the index-freeze problem already tracked — it would affect anyone trying to mark or exit using option LTP during that window, independent of overnight gap risk.

**Contrast at the constituent level (via `cas_market_watch/2026-08-06.csv`, §7.3's tracker, first live production data)**: checked HDFCBANK, RELIANCE, ICICIBANK's own IEP (indicative equilibrium price) trajectories through their NSE auctions today. All three were remarkably **stable** — IEP barely moved from first read to settlement (e.g. HDFCBANK 735.0→734.3→734.3, final 734.3), even though the underlying imbalance quantity (`iiqAtEP`) swung wildly. This is a real contrast to the Sensex option's own gyrations — suggests the intra-window instability is concentrated in the derivatives layer (leverage, thin option-specific order flow), not present in how NSE resolves the underlying stock auctions themselves, at least on a day when Nifty's own move was small (+0.03%).

**Net read**: Sensex's first CAS expiry day showed the same qualitative risk Nifty's Aug 4 expiry did — options pricing in real uncertainty ahead of a settlement, then collapsing/resolving sharply — but with earlier IV buildup, a different mechanical signature at settlement (vega collapse vs. directional flip), and no corresponding chaos in the underlying constituent auctions. Two clean expiry-day data points now exist (one per index), not yet enough to call this a stable pattern, but both point the same direction.

**Methodological refinement, 2026-08-10 (§5.4)**: part of the raw IV climb above (39%→90%+) is a known mechanical artifact of 0DTE annualization, not purely growing fear — with a roughly fixed amount of absolute event risk (the settlement gap) and shrinking time-to-expiry, annualized IV rises simply because the same risk is compressed into less time, independent of whether the market's actual expectation of the settlement move is changing at all. This doesn't invalidate the finding (the settlement outcome above confirms real, not just anticipated, risk) but means the raw IV numbers overstate how much the market's *expectation* grew relative to how much is just calendar mechanics. The cleanest control for this — comparing the expiring series' IV against the *next* expiry's IV at the same clock time, rather than against a pre-CAS historical baseline — wasn't used here; worth adopting for the next Sensex expiry (2026-08-13) if IV is checked again.

### 4.9 2026-08-07 — first overnight gap reversal since CAS launched

Own data (`cas_auction_tracking.csv`, `cas_gap_fade_tracking.csv`, `cas_market_watch/2026-08-07.csv`, `india_vix_daily.csv`), all local files already synced — no ad-hoc AngelOne calls (Iris was live all day; see §8's standing operating rule).

**The overnight-gap-up pattern broke for the first time since Aug 3.** Computing each day's actual gap (that day's regular 9:15 open vs. the *prior* day's CAS terminal close, not the prior day's own open):

| Prior day's auction close → next open | Gap |
|---|---|
| Aug 3 Nifty close → Aug 4 open | −0.28% |
| Aug 4 Nifty close → Aug 5 open | +0.22% |
| Aug 5 Nifty close → Aug 6 open | +0.07% |
| **Aug 6 Nifty close → Aug 7 open** | **−0.39%** |
| Aug 3 Sensex close → Aug 4 open | +0.63% |
| Aug 4 Sensex close → Aug 5 open | +0.80% |
| Aug 5 Sensex close → Aug 6 open | +0.26% |
| **Aug 6 Sensex close → Aug 7 open** | **−0.56%** |

Sensex had run three straight overnight gap-ups (+0.63%, +0.80%, +0.26%); Aug 7 reversed that into its sharpest overnight gap of the whole period, in the opposite direction. Nifty's gap also flipped negative, its largest gap-down since the very first CAS day.

**No VIX signal accompanies it.** `india_vix_daily.csv`: 11.76 → 12.19 → 12.06 → 12.16 → 12.16 (Aug 3-7), completely flat through Aug 7, open equals close on the day itself. Rules out a fear-driven move as the explanation — reads as an ordinary price pullback, not a risk event.

**Both indices partially recovered intraday rather than extending the drop**: Nifty open→close +0.13%, Sensex open→close −0.02% (near flat) — the day didn't continue falling after the gap-down open, it stabilized.

**The auction session itself, and the underlying constituent auctions, were unusually quiet** (`cas_market_watch/2026-08-07.csv`, first full day this file was captured via the newly-fixed `datasync` — see §7.3 correction below): 83 polls, all 208 symbols present throughout, clean. Settlement `perChange` (vs. each stock's own 15:15 reference price) across all 208 names: mean +0.07%, std 0.34%, range −1.01% (FORTIS) to +1.10% (PETRONET) — an ordinary, contained spread. Worst intra-auction IEP swing was IDFCFIRSTB at 2.5% and ASIANPAINT at 2.0%, well short of the kind of instability seen in the Sensex 78800 straddle on its own expiry day (§4.8) — expected, since Aug 7 wasn't an index-options expiry day for either underlying.

**Net read**: Aug 7 is the cleanest evidence yet that the "consistent one-directional lift" framing in the top status line needs a caveat — it held for the first 4 days, but day 5 broke it on both the auction-move axis (§4.1) and the overnight-gap axis, on a day where the auction itself was unremarkable at every level checked. Doesn't resolve the manipulation-vs-structural question either way; if anything, a quiet auction session producing a large *reversal* gap the next morning suggests the overnight gap and the auction-session mechanics may be more independent of each other than the first 4 days made them look. Needs more days before drawing a real conclusion — still only 5 trading days total.

---

## 5. External corroboration

### 5.1 @Am_Shai (Shai Coelho, verified market-structure/orderflow trader, ~38K followers), gathered 2026-08-05 via Chrome

Independently reached the same mechanism, unprompted: naked short-selling ban in India's cash segment → thin sell-side in the auction → "CAS can float up." Distinguished expiry vs. non-expiry days — on a non-expiry day, derivatives correctly *ignored* a 200-point CAS move; on an expiry day, options had to follow (concrete example: a 24,400 CE moved ₹97→₹217 as Nifty's CAS print pushed it toward ITM). Suggests acute risk concentrates on expiry days specifically. Also framed CAS as a tradeable opportunity (see §6) — weight this part less; he sells a market-data product, so the mechanism claim is corroboration but the edge claim isn't independent.

A concrete regulatory fix is publicly proposed (Shijumon Antony, tagging SEBI/NSE/BSE): settle expiry options at the pre-auction 15:15 reference price instead of the post-auction close. If adopted, this would sever the link between the auction's distortion and expiry settlement — worth checking periodically as a signal for when it might be safe to reconsider Artemis.

### 5.2 Zerodha Varsity, 10-tweet thread (`x.com/ZerodhaVarsity/status/2084934800918348118`), gathered 2026-08-05

Far more authoritative and detailed. Key points beyond the mechanics already folded into §1:

- **Independent volume evidence, NSE side** (their own Kite Connect data): Aug 4 CAS clearing volume for dozens of Nifty 50 stocks — including all 6 we tested ourselves — at just **0.1x-0.7x of what those same stocks traded in the same 15:15-15:30 window the week before CAS**. Their own conclusion: *"Price discovery on a thinner order book likely explains much of the wide moves in F&O stocks and, by extension, in the Nifty and Sensex."*
- **Closes the expiry-day-options question independently**: concrete numbers from the actual Aug 4 Nifty expiry — the 24,500 call went from ₹54 (OTM, pre-auction) to ₹115 (ITM, post-auction, +113%); the 24,550 put fell from ₹88 (ITM) to ₹0.
- **Confirms BSE independently and flags a date**: *"BSE runs its own CAS too. It has been quieter than NSE's so far, but tomorrow (Thursday) Sensex expiry will be its first expiry-day test."* **2026-08-06 was Sensex's own first expiry-day CAS test** — check whether this produced the same acute risk Nifty's expiry did.
- **Forward-looking, worth tracking**: *"If sharp closing moves continue, option sellers may demand higher premiums and spreads could widen near the close."* Rising near-close premiums/IV would be a leading indicator that the options market is pricing in this risk — relevant to eventually judging when it's safer to sell premium into this regime again.
- Their live "CAS Market Watch" page prompted the tracker built in §7.3 below.

### 5.3 @Am_Shai, "THE FIRST FOUR SESSIONS" recap thread (`x.com/Am_Shai/status/2085341064593129747`, 13 tweets, posted 2026-08-06 17:53), gathered 2026-08-07 via Chrome

A consolidated retrospective covering his Aug 3-6 observations (mostly restating what's already in §5.1 above, quote-tweeting his own earlier posts) — posted before Aug 7, so it says nothing about today's gap reversal (§4.9). Two points are new synthesis, not just recap:

- **Independently reaches the same NSE-vs-BSE conclusion we did in §4.4, from different data.** His tweet 12 ("DAY 4 · EXCHANGE LIQUIDITY MATTERS"): *"On 6 August, Nifty was broadly flat through CAS while Sensex moved sharply higher. That divergence matters. It suggests the result is not always a broad repricing of the Indian market. Auction depth, constituent weights and where the closing basket is routed can produce different outcomes across NSE and BSE. CAS must therefore be studied as a separate market regime [per exchange]."* We reached this from 6-stock NSE-vs-BSE tick data (Aug 3-4, §4.4); he reached it from the Aug 6 Nifty/Sensex index-level divergence alone (§4.1, §4.8). Two independent routes to the same structural read — real corroboration, not just repetition.
- **His forward "what to watch" checklist (tweet 13) matches our own tracking almost item-for-item**, and flags one thing we hadn't been watching: *"Index at 3:15 PM versus final CAS close; constituent breadth and weighted contribution; matched auction value; buy/sell imbalance; market-order imbalance; NSE versus BSE closing differences; futures and synthetic-futures response; **reversal at the next opening**; **month-end, quarter-end and index-rebalance sessions**."* The "reversal at the next opening" item is exactly what §4.9 measured today, independently corroborating that as the right thing to track — he named it as a watch item on Aug 6, before we had the data point. **"Month-end/quarter-end/index-rebalance sessions" is new** — we have no tracking or awareness of upcoming Nifty/Sensex index-rebalance dates, and haven't considered whether CAS behaves differently on those sessions (plausibly *more* genuine two-sided closing demand, which could look different from the ordinary-session pattern measured so far). Added as a new open item (§9).
- **Minor mechanical clarification (tweet 6), consistent with but crisper than what we had**: the settlement reference for expiring derivatives is the cash-index close, fixed once constituent auction prices are final — continued F&O trading to 15:40 can change who holds the position and at what price, but cannot change the already-fixed cash-index close itself.
- **One new piece of "why CAS exists" color** (tweet 9, quoting his own reply to a follower): beyond SEBI's stated rationale (§1), he also attributes CAS's introduction to fixing "the expiry close management done by HFT" — i.e., a pre-existing concern about high-frequency trading activity distorting the close specifically on expiry days, not just the general VWAP-tracking-error rationale already documented.
- Closing line worth keeping as a fair summary of the whole investigation's open question, in his words rather than ours: *"CAS is useful. The open question is whether it consistently has enough opposing liquidity to produce a closing price that represents the wider market — not only the most urgent closing order."*

### 5.4 Vtrender long-form article, "Closing Auction Session (CAS): the cash close is only part of the change" (`vtrender.com/posts/...`), gathered 2026-08-10

Same author/company as §5.1 and §5.3 (Shai / Vtrender) — **not independent corroboration**, a more structured, longer-form version of the same voice, published on his own platform rather than X. Treat as elaboration, not a fourth independent source. One genuinely new point, one useful independent-ish data cross-check, and confirmation of the Aug 7 finding from a different constituent set.

- **New methodological point, folded into §4.8 above**: explicitly separates three effects that this file had been measuring together — (1) the cash-close displacement, (2) the expiry settlement mechanism, and (3) a *pre-auction* 0DTE IV effect. On his numbers for Sensex's Aug 6 expiry, front-expiry ATM IV moved from the low 40s toward ~60 by 1PM and near 96 by 3PM, while the *next week's* series stayed flat in the mid-teens throughout — his point being that **on 0DTE, annualized IV can rise simply because a roughly fixed amount of event risk gets compressed into ever-less remaining time**, independent of whether the market's actual expectation of the move is growing. This means our own §4.8 IV numbers (39%→90%+) likely overstate how much market *expectation* grew, versus how much is just the annualization convention doing its usual thing under a shrinking clock. Doesn't change the settlement outcome (real, not just anticipated — the straddle collapse happened), but sharpens how the buildup should be read. His suggested control — expiring series IV vs. next-expiry series IV at the same clock time, not vs. a historical baseline — is a better tool than what we used and worth adopting going forward.
- **Independent-ish constituent numbers for Aug 5**, a different 6-stock set than ours (HDFC Bank +0.24%, Bharti Airtel +0.99%, Infosys +0.56%, ICICI Bank +0.49%, Reliance +0.53%, Bajaj Finance +0.76%) — broad, same-direction, consistent with our own Aug 3-4 6-stock finding (§4.4) and our Aug 3/4/5 intraday-verified shortfall result (§10.4). Not independently re-verified against our own data for this specific day, but directionally consistent.
- **Confirms our Aug 7 finding from a third constituent set**: his Aug 7 numbers are genuinely mixed both directions (Reliance +0.27%, Bharti Airtel +0.21%, Kotak Bank +0.41%, L&T +0.40% vs. HDFC Bank −0.10%, Axis Bank −0.55%, Bajaj Finance −0.18%, SBI −0.15%), concluding "Nifty consequently showed little displacement at the close" — matches our own §4.1 measurement of Nifty's smallest/most balanced Aug 7 auction move, from different underlying stocks than any we tested ourselves.
- **Reframes the "operational asymmetry" already in §5.1** slightly more generally: not just the naked short-selling ban specifically, but "additional buy liquidity requires capital; additional sell liquidity requires deliverable stock or an available borrowing arrangement" — a broader statement of the same mechanism, not new information.
- Explicitly states expiry settlement uses the cash-index close, not the option's last traded premium or the futures price — consistent with tweet 6 in §5.3, cited here as "NSE's settlement methodology states this directly" (methodology document itself not sourced or read independently).
- Own caveat, stated in the article and worth keeping: "The first week is too small a sample to establish a permanent directional CAS bias." Consistent with the discipline already applied throughout this file.

---

## 6. Tradeable-opportunity discussion (2026-08-05)

Split into testable-now vs. not-yet, per the standard applied throughout: enumerate, don't just assert.

- **Fade-the-gap** (§4.2) — the only idea testable from data already flowing; implemented as `cas_gap_fade_tracking.csv` (§7.2).
- **Long calls/short puts into the expiry-day close** (mirroring @Am_Shai's example) and **straddle into the auction** both require options data through 15:15-15:40 on a post-CAS expiry, which wasn't available at the time this was raised.
- **Fade-the-gap and "buy into the close" are opposite sides of the same distortion** — one says the print reverts, one says ride it, they can't both be the edge. The futures/constituent findings in §4.3-4.4 (auction print disconnected from continuously-traded markets) lean toward fade over ride.
- Any of these would be long-premium/directional — a different risk profile than Artemis's short-premium book, not a "make the loss back" repair.
- No personalized trade recommendation was given or should be inferred from this section — informational/measurement framing only.

---

## 7. Tracking infrastructure shipped

### 7.1 CAS auction-move tracker (commit `2172018`)

`data_downloader_angelone.py`'s `fill_missing_candles` returns a 4th value, `cas_auction_moves`. `log_cas_auction_moves()` appends one row per date/index to `data/cas_auction_tracking.csv` — zero new API calls (derived from data already fetched), gated to Nifty/Sensex, wired into the existing daily `update_index` cron. CSV-only, no Slack.

### 7.2 CAS gap-fade tracker (commit `b67b445`)

`log_gap_fade_tracking()`, same file, same cron. Pairs each new CAS-era day's own open/low/high/close against the *prior* day's auction move (read back from `cas_auction_tracking.csv`), appends to `data/cas_gap_fade_tracking.csv`. Feeds the §4.2 measurement. Zero new API calls, CSV-only, no Slack.

**Bug found and fixed same day (commit `5dbc312`)**: both tracker functions wrote freshly-computed dates as raw `datetime.date` objects into new rows, while dates re-read from the existing CSV came back as plain strings — concatenating and sorting a mixed-type object column doesn't error, it silently produces a wrong row order (today's row landing first instead of last). Fixed by normalizing to `str()` at construction in both functions. This also explains why `cas_gap_fade_tracking.csv` never appeared on delos at all on its first run there: with no prior-day row available in delos's freshly-created `cas_auction_tracking.csv` (no historical backfill had been pushed there), the function's `prior.empty` check skipped the only candidate day and returned before ever writing the file — self-heals from the next cron run onward.

### 7.3 Live NSE CAS Market Watch tracker (commit `97af765`)

`data_pipeline/nse_cas_market_watch.py` — a fuller reconstruction of the auction's evolution than 7.1/7.2, which only capture start/end points from 1-min index candles. Polls NSE's actual live auction feed:

- **Endpoint**: `GET https://www.nseindia.com/api/NextApi/apiClient/casApi?functionName=getCASData` — found by reading the Market Watch page's own JS bundle (no official API docs exist). No auth, no query params, returns all 208 F&O stocks per call: `refrencePrice` (VWAP 15:00-15:15), `prevClose`, `upperBand`/`lowerBand` (±3%), live `IEP` (indicative equilibrium price), `finalPrice`/`finalQuantity` once matched, `iiqAtEP`/`iiqAtMO` (imbalance quantities), full OHLC, `status`, `lastUpdateTime`.
- **Session handling**: reuses the Akamai cookie warm-up technique already proven in the sibling `swing-trading-lab` repo's `data_pipeline/bhavcopy/nse_session.py` (homepage hit 403s but sets cookies; follow-up hit to `/option-chain` completes the set — the same technique the `nsepython` package uses). Duplicated in miniature into this repo rather than cross-imported, since delos only deploys `algo-trading-lab`.
- **Design**: polls every 15s from 15:14 to 15:36, appends one row per (poll, symbol) to `data_pipeline/data/cas_market_watch/YYYY-MM-DD.csv`. Skips non-trading days via root `data/holidays.csv`. Research-only, no Slack, no live-strategy dependency.
- Smoke-tested end-to-end (holiday check, session warm-up, live fetch, CSV append) before shipping; the full 22-minute polling loop was not tested live in production before deployment since the market was closed when it was built.
- **BSE: no equivalent found**, despite a genuine multi-angle attempt (URL guessing, grepping all 7 discoverable JS bundles including ones with confirmed calls to `api.bseindia.com`, search-engine site search). BSE's Market Watch page is a heavily obfuscated Angular SPA with lazy-loaded route chunks that couldn't be mapped via static analysis. Browser-based network inspection would likely resolve this quickly but is blocked by Claude's own safety restrictions for both `nseindia.com` and `bseindia.com`. Open item — revisit via manual DevTools inspection during a live 15:15-15:30 window if wanted.

**Deployment**: pushed to `origin/main`; Claude has no SSH access to delos (no `delos` host alias, raw VPS IP rejects publickey/password auth) so could only hand off the crontab line. User confirmed 2026-08-05 they pulled the commit and added the cron entry themselves:
```
12 15 * * 1-5 cd /home/parijnan/scripts/algo-trading-lab/data_pipeline && /home/parijnan/anaconda3/bin/python nse_cas_market_watch.py >> ../logs/nse_cas_market_watch_$(date +\%Y\%m\%d).log 2>&1
```
**First live run confirmed clean, 2026-08-06** — also Sensex's own first expiry-day CAS test (§5.2). 83 polls (15:15:15 to 15:35:50), all 208 symbols present on every single poll, no drops. Status transitions from `Open` to `Closed` per-symbol as each stock's own auction resolved, matching expected mechanics. See §4.8 for what it captured. **Second clean run, 2026-08-07** — same shape (83 polls, 208/208 symbols), see §4.9.

**`datasync` gap found and fixed, 2026-08-07**: `~/.local/bin/datasync` (outside this repo, not version-controlled) syncs `indices/`, `sensex/`, and the two CAS tracker CSVs from delos, but never had a line for `cas_market_watch/` — the Aug 6 file only reached the local machine because the user copied it over by hand. Fixed by adding an `rsync` line for the directory, same pattern as the existing entries. Going forward this should sync automatically; no repo commit involved since the script lives outside `algo-trading-lab`.

### 7.4 Rate-limiting note

AngelOne's historical-candle endpoint hit a sustained, undocumented throttle during this investigation — ~4 heavy pulls in a few hours triggered 20+ minutes of "exceeding access rate" errors even with 15-20s pacing, well under the documented 3/sec-180/min-5000/hour limits. Suggests a stricter limit specific to bulk historical requests, or a cumulative daily budget. Space out ad-hoc historical pulls across sessions if repeating this kind of investigation.

---

## 8. Strategic response: Iris deployed as sole live strategy (2026-08-06)

With Artemis's re-enable blocked on two open questions (§3, §3.1) and Athena carrying a related-but-lesser version of accepted-risk §2.1, the user re-enabled Leto with **Iris force-routed regardless of VIX** — not just its original VIX>25 slot, a deliberate expansion. This supersedes Athena's 16-25 slot too while the override is active, not just Artemis's.

**Reasoning:**
1. **Structural CAS-immunity, verified by reading the code, not assumed.** Iris's expiry-selection logic — `MIN_DTE=2` in the backtest (`run_options_sim.py`), and an equivalent ELM-date check in production (`select_expiry()`, `iris_production/functions.py`) — means it **never trades a contract within 2 days of expiry**. Combined with its existing `EXIT_BY_TIME=15:15` intraday-only design, Iris is architecturally insulated from the same-day-settlement dynamics in §4.8, not merely untested against them.
2. **Backtest evidence checked directly** (`iris_backtest_summary.csv`, 1,208 trades, 2019-04-26 to 2026-07-30, validated in a separate chat session in the same repo — file existence/date-range/DTE-distribution independently confirmed by reading it): VIX≤16 is robust across both all-time (n=641, 59.4% WR, ₹239/trade) and 2023+ only (n=468, 60.0% WR, ₹244/trade) — the strongest, most consistent band. **The 16-25 band is notably weaker in 2023+ than the all-time average suggests** (₹86 vs ₹212/trade). **VIX>25 — the band Iris was originally placed in for being "strongest"** — is actually net-negative in the 2023+ subset (n=5, -₹422/trade, 20% WR; too small a sample to be conclusive alone, but the opposite of reassuring for the live slot Iris has occupied since June).
3. **User's own market-regime view**: expects VIX to trade predominantly ≤16 going forward given recent regulatory measures, with occasional event-driven spikes reverting. If correct, most exposure lands in Iris's best-supported band regardless of the weaker 16-25/>25 numbers — a forecast underpinning the decision, not something the data itself confirms.

**A related argument was raised and then dropped**: the elevated options IV observed around CAS closing auctions this week (§4.8) was initially proposed as a tailwind for Iris ("premium decay vanishing works in its favor" — since it's a directional option buyer). Dropped once the `MIN_DTE=2`/ELM-date check confirmed Iris never trades close enough to expiry to be exposed to that specific dynamic. The decision rests on points 1-3 above, not this.

**Sizing discipline**: first live run at a **static 1 lot**, not dynamic (`LOT_CALC=False`, set via `iris_production/data/sizing_override.json` — the Slack "Manage Sizing" modal mechanism, same pattern as Artemis/Athena). Explicit user framing: treat "capital under risk" as "capital available," not "capital freed up" — Iris requiring less capital than Artemis/Athena is not itself a reason to size up. Plan is to scale toward Artemis/Athena-comparable size gradually. Note: `sizing_override.json` and `routing_state.json` are both gitignored, delos-only files — not visible from the local machine, so the live values (vs. just the override *mechanism*) couldn't be independently confirmed from here.

**No slippage modeled in the Iris backtest** — confirmed by reading `run_strategy_backtest.py` (produces `iris_backtest_summary.csv`): fills at raw historical `open` price via `_get_price_near()`, no haircut applied. A separate standalone script (`options_slippage.py`) measures realistic bar-to-bar gap empirically but isn't wired into the simulation. User's call: acceptable as-is, not worth backtesting a haircut — Nifty options are liquid enough that they haven't observed significant slippage even on gamma moves, and Iris's websocket-driven trigger plus execution infrastructure should hold up without it.

**Standing operational rule as of this decision**: no ad-hoc AngelOne API calls (live spot/option pulls, IV checks, etc.) from Claude until Leto terminates for the day, now that Iris is live and sharing AngelOne's rate-limit budget — this session's own research pulls repeatedly hit sustained throttling well under documented limits (§7.4). See `feedback_no_angelone_during_live.md` in memory.

---

## 9. Open items / follow-up checklist

When resuming this investigation, check in this order:

1. ~~Does `cas_market_watch/2026-08-06.csv` exist with a full run's worth of polls?~~ **DONE 2026-08-06**, confirmed again 2026-08-07 (§7.3) — both clean. Note: `datasync` didn't pull this directory until fixed 2026-08-07 (§7.3) — if a gap ever reappears, check whether the fix survived or the sync script changed again.
2. ~~How many days has `cas_auction_tracking.csv` / `cas_gap_fade_tracking.csv` accumulated since 2026-08-06?~~ **Checked through 2026-08-07 (§4.1, §4.9)** — Nifty's shrinking-magnitude streak broke on day 5 (ticked up from 0.03% to 0.06%); Sensex reverted from its expiry-day spike to its quietest reading yet (+0.01%); and the overnight gap-up streak broke on both indices for the first time (§4.9). Keep watching: does Nifty's move stay small/noisy from here, or was Aug 7 a blip? Does Sensex's flat/quiet baseline hold until its next expiry (2026-08-13)?
3. ~~Did the Aug-4-expiry Nifty options and Aug-6-expiry Sensex options land?~~ **DONE 2026-08-06** — both landed and analyzed in depth (§4.8). Next: does the next Nifty expiry (2026-08-11) and next Sensex expiry (2026-08-13) show the same shape (early IV climb for Sensex, late sharp ramp for Nifty), or was this week idiosyncratic?
4. Has any SEBI/exchange response emerged regarding the reference-price settlement fix proposed in §5.1?
5. Is the overnight gap-through-stop risk (§3) still unresolved / not on any accepted-risks list? Don't assume a decision was made without being told.
6. ~~Is Artemis still disabled?~~ **Corrected 2026-08-06 (§3.1)**: it's not a code-level disable, it's contingent on the active Slack override (currently forcing Iris, §9). Check whether that override is still active before assuming Artemis is out of rotation — if it lapsed, Leto may have auto-routed back to Artemis on a qualifying VIX≤16 day.
7. **New**: how is Iris actually performing live (§9)? Still at static 1 lot, or has sizing been scaled up? Any gap-through or expiry-adjacent incidents that would test the CAS-immunity argument in practice, not just in backtest/code-review?
8. **New**: does the "market learned from Nifty's Aug 4 event and priced Sensex's Aug 6 expiry in earlier" read (§4.8) hold up on Sensex's *next* expiry (2026-08-13) — i.e. does IV now climb early for Sensex expiries generally, or was Aug 6 specifically anticipatory because it was the first one?
9. **New (§5.3)**: check whether any Nifty/Sensex index-rebalance date falls in the near term, and whether month-end (Aug 31) shows different CAS liquidity behaviour than the ordinary sessions measured so far (@Am_Shai flagged this as a distinct watch category — plausibly more genuine two-sided closing demand on those days, which could look different from the pattern in §4.1/§4.9). No tracking exists for this yet — would need the NSE/BSE index-rebalance calendar, which hasn't been sourced.
10. **New (§10)**: comprehensive Artemis risk report added 2026-08-07 — re-read before any Artemis re-enable decision, and before treating the overnight-gap risk (§3, item 5 above) as resolved either way.
11. ~~The `ClsPric` shortfall mechanism was only quantified for Aug 3; Aug 4/5 need each exchange's ~15:14 intraday level, blocked while Iris is live.~~ **DONE 2026-08-08** (weekend, Leto not running): pulled AngelOne intraday for Aug 3-5, confirmed 18/18 stock-days of zero BSE trades after 15:14, and the NSE-auction-move-as-shortfall replicates across all 3 days (ratios 0.75/1.32/1.20 vs index divergence) — see §10.4. Next, if picked up again: extend the same intraday pull to more CAS-era days as they accumulate, and to more than 6 stocks if a fuller (weighted) reconstruction is ever wanted.

---

## 10. Artemis risk report — negative and positive implications (2026-08-07)

Requested deep-dive, consolidating everything measured so far (§1-9) specifically against Artemis's actual exposure, not the investigation in general. Trading strategy is risk management at its core, so this treats "is Artemis safe to re-enable" as the central question and works through both directions of evidence. **No recommendation is made — the user has said this is still being worked through, and this report's job is to lay out the evidence, not decide.**

### 10.0 Framing correction: Artemis's exposure is Sensex, not Nifty

Most of the alarming evidence gathered so far (§4.3's futures/cash divergence, the "manipulation theory" in §3, the bulk of external corroboration in §5) is about **Nifty's** auction print. Artemis trades **Sensex only** (`artemis_configs.py`: `instrument = SENSEX`). None of the Nifty-specific auction-print distortion is Artemis's exposure, and it would be a mistake to import that alarm wholesale into a Sensex risk assessment.

Flip to the axis that *does* apply to Artemis — the overnight gap (§4.9) — and Sensex is not the calmer index, it's the more volatile one: three of Sensex's four overnight gaps exceed 0.6% in magnitude, against Nifty's largest at 0.39%. **Artemis's real CAS exposure is the overnight gap on Sensex, not the closing-auction print itself.** Everything below is organized around that.

### 10.1 Live-exposure status — this is not academic

As of today (2026-08-07), VIX closed at 12.16 — inside Artemis's routing band (`VIX ≤ 16`). Per §3.1, **the only thing keeping Artemis out of rotation is the active Slack manual override**, currently forcing Iris. If that override lapses on any Monday-Thursday with VIX ≤ 16, Leto auto-routes straight back to Artemis with the `index_sl_offset` unchanged at its current value and every risk below fully live. This report describes a configuration one lapsed override away from being active, not a hypothetical.

### 10.2 Negative implications — the risks

**10.2.1 Quantified against the actual spot-to-stop distance, and checked against 421 days of pre-CAS history — not the 200-point offset in isolation.**

`index_sl` is strike-relative, not close-relative, so the number that matters is the full distance a gap must travel from spot to reach it, which depends on how the strike was placed:

- **Fresh entry**: strike ≥ `minimum_gap` (1000 pts) from spot at entry, `index_sl` a further 200 pts inside → **~800 pts, ≈1.02%** of spot-to-stop distance.
- **Adjustment/reopened entry** (10.2.3 below): re-struck at `minimum_gap_iterator` (400 pts) from spot, `index_sl` 200 pts inside that → **~200 pts, ≈0.255%**.

Checked against 421 pre-CAS Tue/Wed/Thu adjacent-trading-day Sensex overnight gaps (`sensex_daily.csv`, 2023-05-23 to 2026-08-03 — the window Artemis actually holds through, excluding weekend/holiday-spanning opens):

| Spot-to-stop distance | Entry type | Pre-CAS days exceeding it | Fraction |
|---|---|---|---|
| ≈1.02% | Fresh entry | 13 of 421 | 3.1% |
| ≈0.255% | Adjustment/reopened entry | 177 of 421 | 42.0% |

**This is not a CAS-era finding — it's a pre-existing structural property of the adjustment/reinforcement mechanism, measured over more than three years of pre-CAS data.** Fresh entries have always had a comfortable buffer (a gap large enough to close it happened ~3% of Tue-Thu sessions). Adjustment legs have never had that margin — a gap large enough to close their spot-to-stop distance has happened on roughly 2 of every 5 pre-CAS Tue-Thu sessions. CAS didn't create this gap; it's a longstanding property of how the reinforcement transform places its re-struck strike. What CAS changes is covered in §10.4.

**10.2.2 Existence proof, verified against `credit_spread.py` and the actual trade record — not a reconstruction from memory.**

Read `credit_spread.py::_set_sl()` directly: for a **CE** spread, `index_sl = sell_strike − index_sl_offset` (a pre-strike buffer on the danger side, meant to exit before the option goes ITM). `ce_trade_params.csv` records the actual Aug 5 incident spread: `sell_strike = 78800`, so `index_sl = 78800 − 200 = 78600`. `cas_gap_fade_tracking.csv`'s Aug 5 row: Sensex `day_open = 79055.38`.

**79055.38 is 455.38 points past the 78600 stop at the opening print alone** — more than double the offset itself. This is a harder number than the 55-point estimate first floated for this incident; the gap didn't graze the stop, it blew through it by over 2x the buffer's own width, before the market had traded a single tick that day.

**10.2.3 Sharper causal finding: adjustment/reinforcement legs use a smaller minimum gap than fresh entries, by design — code-verified, not inferred from the trade record.**

`ce_trade_params.csv`'s `entry` timestamp (2026-08-04 13:32:38.774693) is within one second of the `pe_trade_params.csv` exit timestamp (13:32:38.030227) — this CE leg was the iron condor's own "exit tested side, reinforce the other" transform, not an original entry. (Note: this section originally cited the CSV's `index_entry` field as evidence the leg was struck only ~82 points OTM — that figure was withdrawn on review. `index_entry` was identical, 78718.24, on both the Aug 3 PE row and the Aug 4 CE row despite a 27-hour gap between them, which means the local CSV can't be trusted as a per-leg live value here; per `feedback_local_vs_delos_state`, local trade-state files aren't guaranteed to mirror delos, and this field wasn't independently cross-checked the way `index_sl` was in 10.2.2.)

The claim that survives is structural, straight from `credit_spread.py::initialize_spread()`: a **fresh** entry (spread_status `!= 'closed'`) uses `minimum_gap = 1000` points (≈1.27%) when placing the strike. A **reopened/adjustment** entry (spread_status `== 'closed'`, lines ~543-562) instead checks whether the *existing* strike is still `> minimum_gap` from spot — and if not, re-strikes using `minimum_gap_iterator = 400` points (≈0.51%), less than half the fresh-entry buffer. **Adjustment legs are permitted to sit structurally closer to the money than fresh entries, by design** (that's how the transform re-engages the active side) — which means whatever `index_sl` buffer they get (10.2.1) starts from a smaller cushion than a fresh entry's, before any further drift. This doesn't by itself prove the Aug 5 CE leg was struck at exactly 400 points — that would need delos's own record — but it establishes the mechanism is real and code-level, not a one-incident coincidence.

**10.2.4 The stop firing doesn't just cap losses at a bad level — it locks in the worst level of the day, and the entire triggering move fully reversed.**

Same Aug 5 row: `day_open = 79055.38` (where the stop breach happened, 10.2.2), `day_low = 78285.74`, `day_close = 78581.00`. Two things stand out: the index closed **474 points below the opening print that triggered the exit**, after touching a low 770 points below it; and `day_close = 78581.00` finished **below the 78600 stop itself** — meaning the entire overnight move that forced the exit had fully reversed by end of day. For a market-neutral short-premium book, a gap that partially or fully mean-reverts intraday is not a tail risk, it's close to the modal shape measured so far (§4.2's fade table: Sensex opens near the day's high and drifts lower on both days measured, −0.97% to −1.16% open→low). **CAS converts an ordinary gap-and-fade into a forced loss**, because the stop is mandatory and unwinds at the open, while the potential recovery later in the day is not available to a position that's already been closed.

**10.2.5 `option_sl` is comparatively better-insulated, but not immune — exact breach margin depends on a recompute-timing detail not verifiable from local files.**

`option_sl` is set as a *multiple* of entry premium (`sl_0_dte = 1.33` through `sl_4_dte = 2.66`, from `trade_settings.csv`), not a fixed point offset. Elevated CAS-era IV (§4.8: Sensex IV climbed from ~39% to ~55%+ intraday on Sensex's own expiry day) inflates option premiums, which widens `option_sl` in absolute points automatically — a self-scaling mitigant `index_sl` doesn't have. Two bounds, not one number: the CSV's recorded `option_sl` for this spread is 288.94 (2x the 144.47 entry, the 2-DTE multiplier from the original Aug 4 13:32 entry) — against that, intrinsic value alone at the open (79055.38 − 78800 = 255.38) falls *short* of the stop, so a breach would have needed real extrinsic value too, not intrinsic alone. But `credit_spread.py` recomputes `option_sl` on some restarts (line ~436, conditional on `spread_status`), and *if* that happened on the morning of Aug 5 (1 business day to expiry, `sl_1_dte = 1.66` → `option_sl = 144.47 × 1.66 = 239.82`), intrinsic alone would clear that lower bar. Which of the two applied that morning isn't determinable from local files — either way, the memory record that both `index_sl` and `option_sl` were breached that morning stands; only the exact margin is uncertain.

**10.2.6 Distributional caveat, applies to all of the above.**

10.2.1's 42%/3.1% figures rest on 421 pre-CAS days, which is solid. The CAS-era sample is n=4, and on that sample **CAS-era gap magnitude cannot be distinguished from the pre-CAS distribution** — two of the four CAS-era gaps exceed the pre-CAS 90th percentile (0.79%), two don't. What's established at this sample size is not "CAS made gaps bigger" — that claim isn't supported — it's a new *mechanism* for producing a gap, detailed in §10.4, which happens to fall within a magnitude range the pre-CAS distribution already had a real tail for. Keep the structural-thinness-vs-deliberate-manipulation distinction (§4.7) separate from this section entirely: this is a liquidity/volatility-regime risk, not a claim about anyone's intent, and it would exist even if the manipulation-theory investigation resolves in the "purely structural" direction.

### 10.3 Positive implications — what's been de-risked or never was a Sensex problem

1. **`evaluate_expiry_day_close()` removes the §4.8 exposure entirely.** The Sensex 78800 straddle collapsed 505.95 → 154.65 inside the closing-auction window on its own expiry day — Artemis is force-flat by 15:15 on expiry day, before that collapse happens. Shipped and code-verified (§2), and the backtest/live parity gap on this exact mechanism closed 2026-08-06 (`95a99b2`), so any future SL-tuning work won't be contaminated by a stale backtest assumption.
2. **Sensex's auction-move magnitude, while noisy, doesn't show a runaway trend.** Aug 6's expiry-day spike (+0.21%) reverted to the smallest reading of the whole sample on Aug 7 (+0.01%) — see §4.1, §4.9. If the market-awareness mechanism @Am_Shai describes (§5.1, §5.3) is real, the *auction-print* distortion may be self-limiting even if the *overnight-gap* risk (§10.2) is not.
3. **No multi-day dislocation to fight or get caught by.** §4.5: convergence is instant, not gradual — a bad print doesn't compound over multiple sessions.
4. **Sensex's own index is comparatively inert to its constituents' real auction prices.** §4.4/§4.6: the Sensex index calculation appears to rest on `ClsPric` (stale/no-trade reference), not the live-clearing `SttlmPric` — the same finding that makes Sensex's *auction print* look muted also means the print Artemis's `index_sl` reads intraday is less reactive to real constituent-level moves than Nifty's. Cuts both ways: less noise, but also less signal if a real move is happening.
5. **`option_sl` self-scales with entry premium** (10.2.5) — CAS-era IV elevation widens the absolute-point buffer automatically, unlike `index_sl`'s fixed 200-point offset.

### 10.4 The user's diagnosis, checked against data (2026-08-07)

User's own read of the problem, in three parts. Each checked directly rather than taken on priors.

**Point 1 — "historically the frequency of huge gaps on Tue/Wed/Thu has been relatively small and manageable; that's the basic premise Artemis is built on."** Checked against the same 421-day pre-CAS sample as 10.2.1: median |gap| 0.21%, 90th percentile 0.59% — small, as described, for the central tendency. But there's a real tail: the single largest pre-CAS Tue/Wed/Thu gap was 4.48% (2026-02-03), and four separate days exceeded 1.9%. **Both things are true** — the premise holds for the typical session, and it always carried real, if infrequent, tail exposure. CAS doesn't change this history; whether it changes the forward-looking frequency of tail events is the open question the rest of this section addresses.

**Point 2 — "index_sl is blind because the index freezes at 15:15." Corrected framing, conceding the user's pushback: this was written as if the issue were a missing monitoring capability. It isn't — no market is open overnight, so there's nothing to evaluate against between one session and the next; that was never fixable and pre-CAS never needed fixing.** The actual problem is a **reference-price problem, not a monitoring problem**: the *last evaluable price before the gap* is structurally wrong (the index frozen at 15:15 at a level that — per §10.4's mechanism finding below — is itself a damped, partial version of the real move; the option premium anchored to that damped index and never repricing, 10.4 point 3's tape evidence), and the *first evaluable price after the gap* already contains the full move, with nothing in between. Pre-CAS this two-point architecture (last close, next open) was adequate because the close was information-efficient — it actually reflected the day's real move. CAS breaks that specific assumption for Sensex, not the architecture itself.

**Point 3 — "option_sl is an illusion; the real repricing happens the next morning at the open, reflecting what Nifty already showed the previous evening."** Checked directly against the actual option tape for the Aug 5 incident's contract (`data_pipeline/data/sensex/2026-08-06/78800ce.csv`, which covers both days):

| Time | 78800 CE price |
|---|---|
| Aug 4, 15:15 | 174.45 (open of that bar) |
| Aug 4, 15:20 | 170.65 |
| Aug 4, 15:25 | 165.00 |
| Aug 4, 15:29 (last CAS-window bar) | 164.80 |
| **Aug 5, 09:15 (first bar of the day)** | **300.05 open, ranging 266.10–376.45** |

**The premium sat flat-to-drifting-down through the entire 15:15-15:29 CAS window on Aug 4 — no anticipation whatsoever — then opened Aug 5 already at 300.05, past the 288.94 `option_sl` at the very first print of the day.** This confirms the user's diagnosis exactly as stated: the option market itself had not repriced by Aug 4's close. `option_sl` wasn't stale because it failed to see something the market already knew — the market hadn't priced it in yet either. The real repricing happened entirely at the next session's open, in a single discontinuous jump, with no window in between for either SL to act.

**Why this happens, tying together evidence already in this file**: §4.4 found zero trades in NSE-vs-BSE testing for 6 heavyweight stocks on BSE after 15:14, both days tested. §4.6 found the Sensex index appears to compute off `ClsPric` (the stale reference), not the live-clearing `SttlmPric`. Together, these mean the same shared heavyweight constituents that get a real, matched closing price on NSE (feeding Nifty's print, per Zerodha's independent volume evidence in §5.2) get **no *new* price discovery in Sensex's own closing auction** — the information exists by ~15:30 (visible in Nifty's print), but structurally cannot reach Sensex's own close. It reaches Sensex the next morning instead, when BSE's continuous session reopens and real trading resumes. **This is the §4.4/§4.5/§4.6 mechanism, already established at the individual-stock level in this file, now shown operating at the index and single-option level for the actual incident trade.**

**Quantified and confirmed to replicate, 2026-08-08 (answering the user's direct question — "could it be to do with what values are actually being used to calculate Sensex"): yes, checked against both official bhavcopy and, once the weekend removed the no-AngelOne-while-Iris-is-live constraint, real per-exchange intraday ticks for all 3 days.** Bhavcopy first (Aug 3-7): `SttlmPric` (BSE's own file) matches NSE's close almost exactly every day checked — e.g. Aug 7: diffs of −0.003% to +0.006% across the 6 heavyweight stocks, i.e. noise. **This control, from the same BSE file, is what rules out a cross-exchange data artifact** for what follows.

With Leto not running this weekend, pulled 1-minute candles directly from AngelOne for the same 6 stocks on both NSE and BSE, 15:05-15:35, for Aug 3, 4, and 5 — the exact window §4.3/§4.4 originally used but only for Aug 3-4. **Result: BSE's last print for all 6 stocks was at 15:14 on all 3 days — 18 of 18 stock-days with zero trades after 15:14, no exceptions** (extends §4.4's original 2-day/12-stock-day finding to 3 days/18 stock-days, same result both times — notably, the scrip master marks all 6 BSE listings `is_cas_enabled: True`, so this isn't CAS being unavailable for these names, it's CAS being enabled and still clearing zero volume). Because BSE has no post-15:14 print, its auction-window contribution is definitionally zero — there's no second price to compare against, not a measured "0% move" that happens to match anything. NSE, by contrast, clears a real terminal print at 15:28/15:29 every day. That makes the "shortfall" for each stock simply NSE's own 15:14-to-terminal auction move:

| Date | Mean per-stock NSE auction move (= shortfall) | Nifty−Sensex index divergence | Ratio |
|---|---|---|---|
| Aug 3 | +0.650pp | +0.866pp | 0.75 |
| Aug 4 | +0.639pp | +0.486pp | 1.32 |
| Aug 5 | +0.145pp | +0.121pp | 1.20 |

**All three days: same sign, same order of magnitude, ratio clustering near 1** (0.75-1.32) on an *unweighted* 6-stock sample against two differently-weighted indices (Nifty 50 vs Sensex 30) — this is the replication that the bhavcopy close-to-close method (below) failed to find. **This resolves the earlier open item**: the Aug 4/5 bhavcopy failure (mean shortfalls of −0.283pp and −0.340pp, wrong sign) was exactly the methodology limit flagged at the time — bhavcopy only gives whole-day close-to-close, which bundles the real auction-window effect together with ordinary continuous-session cross-exchange noise, large enough on quieter days to flip the sign. Isolating the actual 15:14-to-terminal window with intraday ticks removes that noise and the mechanism holds cleanly across all 3 days tested, not just Aug 3.

**Mechanism, not magnitude — the distinction that matters for what comes next**: per 10.2.6, the *size* of CAS-era gaps isn't distinguishable from the pre-CAS distribution at n=4. What's new is a **specific, non-random source of overnight gap** — pre-CAS, a gap this size required genuine exogenous news; post-CAS, it can happen on an ordinary day purely because BSE's own auction failed to price in information that already existed and was visible in Nifty's print hours earlier. That's a different risk character even if it doesn't (yet, provably) change the odds. It also means part of the information is observable the evening before, which §10.6 uses.

### 10.5 Net picture

Two things are true at once, and neither cancels the other: the acute expiry-day risk (§4.8) that motivated pulling Artemis was real and is now structurally mitigated (10.3.1); a *different*, still-unaddressed risk — the overnight gap hitting a thin `index_sl`/`option_sl` buffer, worst on adjustment legs (10.2.1, 10.2.3) — caused the actual Aug 5 loss, is a pre-existing property of the adjustment mechanism rather than a CAS invention (10.2.1), and remains exactly as present today as it was then. **These are not the same risk, and closing the first one doesn't touch the second.** The open question this file has carried since §3 narrows to something more specific after 10.4: not "is CAS producing bigger gaps" (unproven at n=4) but "is the adjustment leg's spot-to-stop distance — already thin enough to fail 42% of pre-CAS Tue-Thu sessions — an acceptable risk now that there's a demonstrated, non-random mechanism that can produce exactly that kind of gap on an ordinary day."

### 10.6 Mitigation options — enumerated, not recommended

Per standing instruction, ELM is not on this list — it's SEBI-regulatory, not a risk-management lever. These are the levers that are: `index_sl`, `option_sl`, and the strike-placement parameters that set their effective distance.

- **Raise `minimum_gap_iterator`** (currently 400 pts, ≈0.255% spot-to-stop once `index_sl`'s 200-pt buffer is netted out) **toward `minimum_gap`'s 1000-pt fresh-entry standard.** Quantified from 10.2.1: doubling it to 800 pts (≈0.765% spot-to-stop) would have dropped pre-CAS exceedance from 42.0% to roughly 5.5% (23 of 421 days) — close to the fresh-entry rate. This is the most directly quantified lever on this list, and the one the user's own point 3 diagnosis points to (the exposure is concentrated on the re-struck leg, not fresh entries). Tradeoff: a further-OTM adjustment re-strike collects less premium, which weakens the transform's whole purpose — reinforcing the active side to keep the book balanced and paid.
- **Widen `index_sl_offset` (currently 200 pts) directly.** Tradeoff, stated plainly: this does not reduce gap *exposure* — the strike placement is unchanged, so the same fraction of gaps will still reach it — it only moves *where in the loss curve the position exits* once a gap has already happened. A wider offset accepts a larger loss before triggering; it's a loss-size knob, not a risk-reduction one, unlike `minimum_gap_iterator` above.
- **Act on the NSE print during the 15:28-15:40 window, before Sensex derivatives close.** Per §10.4, part of the next-morning move is visible in Nifty's own auction print, which resolves at 15:28-15:30 (confirmations by 15:35) — while Sensex derivatives keep trading to 15:40. `nse_cas_market_watch.py` (§7.3) already polls this feed live to 15:36. A same-day reduce/hedge/flatten decision using that signal is executable inside a real, if narrow, window. Tradeoff: a ~5-12 minute execution window on what may be a thin, volatile tape (§4.8 showed the Sensex option tape itself gyrating in this exact window on an expiry day); this also requires new logic that doesn't exist today, not a parameter change.
- **Reduce overnight position sizing generally** on Sensex during the CAS era, independent of any SL or strike-placement change — caps the loss-per-gap without touching the mechanism itself.
- **A rule against carrying Sensex positions through a CAS-affected close** (i.e., force-flatten before every close, not just expiry day) — removes the overnight-gap exposure entirely, but is a structural change to Artemis's identity as an overnight-carry theta strategy (10.4, point 1), not a parameter tweak; loses the multi-day decay capture that's the strategy's founding premise.

No verdict given; this is the menu the data supports, not a choice among it.
