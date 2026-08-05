# Plan: Closing Auction Session (CAS) — Adaptation & Investigation

**Status: System adaptation COMPLETE and live (2026-08-04, commits `4422d0e`, `660a7fa`). Artemis DISABLED indefinitely since 2026-08-05 pending the manipulation investigation below — no fixed re-enable date. Live incident 2026-08-05 (short 78800 CE, `index_sl` fired at the open on a genuine overnight catch-up gap) surfaced a second, still-unresolved blocking concern (overnight gap-through-stop risk) — explicitly NOT yet added to accepted-risks, still being thought through. Tracking infrastructure (auction-move log, gap-fade log, live NSE Market Watch poller) shipped and running on delos as of 2026-08-05 (commits `2172018`, `b67b445`, `5dbc312`, `97af765`). External corroboration gathered from two independent sources (@Am_Shai, Zerodha Varsity) supports the thin-liquidity mechanism; says nothing about deliberate manipulation. Only ~3 days of evidence — do not overstate any conclusion.**

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
3. Backtest/live parity broken on Artemis's expiry-day exit (backtests still assume ≤15:30 close). Decision: accepted for now; backtests to be retroactively updated eventually.
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

Three straight positive Nifty days, shrinking in magnitude (0.82% → 0.62% → 0.22%) — not enough points to call the shrink a real trend vs. noise, but worth watching rather than assuming the effect is static in size.

### 4.2 Next-day gap-fade pattern (`cas_gap_fade_tracking.csv`)

Both indices consistently open near the day's high (reflecting the prior day's auction print) and drift lower the rest of the session:

| | Aug 4 open→low | Aug 5 open→low | Aug 4 open→close | Aug 5 open→close |
|---|---|---|---|---|
| Nifty | −1.12% | −0.69% | −0.36% | −0.18% |
| Sensex | −1.16% | −0.97% | −0.89% | −0.60% |

### 4.3 Futures vs. cash auction print (own data, ad-hoc AngelOne pull)

Over the exact 15:14→15:29 window on Aug 3 (non-expiry day, cleanest reading — the Aug 4 number is confounded by ~35 pts/day of cost-of-carry noise in the monthly future used): the Nifty **future** moved +10 points while the **cash auction print** moved +200.95 points. Same clock, same underlying, one side continuously traded and one side frozen except for the single print. Sensex showed no equivalent divergence either day (future essentially flat, matching Sensex's own flat/noisy auction prints) — the effect looks Nifty-specific.

### 4.4 Constituent-level NSE vs. BSE (own data, 6 heavyweight stocks: HDFCBANK, RELIANCE, ICICIBANK, INFY, TCS, ITC)

- **NSE**: 11 of 12 stock-days (both Aug 3 and Aug 4) printed a single higher price at the post-gap tick (15:28/15:29), one flat. Range +0.19% to +1.01%, no negatives, across six unrelated sectors.
- **BSE: zero trades recorded in any of the 6 stocks after 15:14, both days, no exceptions** — not thinner, literally no printed volume for the rest of the session, immediately after trading at normal volume (500-5,700 shares/min) right up to 15:14.
- This reframes the Nifty-vs-Sensex divergence: likely not "two competing fair-value views, one honest" so much as "BSE's own closing auction isn't producing real price discovery for its heaviest constituents at all, while NSE's auction is actually trading and consistently clears higher."

### 4.5 Next-session convergence (own data)

Checked both transitions (Aug 3→4, Aug 4→5). **Convergence is instant, not gradual** — BSE's opening auction the next session re-prices immediately using overnight information; it does not visibly chase NSE's price up over the morning. Practical conclusion: the CAS-window divergence is fully contained within the auction itself and leaves no observable next-day dislocation to trade (forecloses any "buy the BSE laggard overnight" idea).

### 4.6 Bhavcopy cross-check (own data, official NSE/BSE EOD files)

BSE's own bhavcopy carries two closing reference prices per stock: `ClsPric` (matches the stale, no-trade price already measured — confirms it wasn't a feed artifact) and `SttlmPric` (nearly identical to NSE's close, every time — likely a pre-existing SEBI uniform-closing-price rule for derivative-eligible stocks, not something new to CAS; not fully verified). What IS solidly supported regardless: since Sensex's own index move stayed flat both days, the index calculation must use `ClsPric`, not `SttlmPric` — explains why Sensex looks flat despite its own heaviest constituents clearly moving.

### 4.7 Causality caveat (repeated deliberately throughout)

All of the above measures a **structural/mechanical** pattern — thin sell-side auction liquidity producing a one-directional clearing price on NSE, and no clearing price at all on BSE for these names. It does **not** distinguish "the auction mechanism just works this way" from "someone is doing this deliberately." Keep these two claims separate. Only ~3 days of evidence exist; the market was already on a multi-day up-run before CAS started, so "auction-structural bias" vs. "ordinary bullish momentum" isn't yet cleanly separable statistically.

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
First live run: 2026-08-06 — also Sensex's own first expiry-day CAS test (§5.2). **Check `data_pipeline/data/cas_market_watch/2026-08-06.csv` exists and has a full run's worth of polls (~88 polls × 208 symbols expected) before assuming the cron fired correctly — this was its first live production run, untested end-to-end before deployment.**

### 7.4 Rate-limiting note

AngelOne's historical-candle endpoint hit a sustained, undocumented throttle during this investigation — ~4 heavy pulls in a few hours triggered 20+ minutes of "exceeding access rate" errors even with 15-20s pacing, well under the documented 3/sec-180/min-5000/hour limits. Suggests a stricter limit specific to bulk historical requests, or a cumulative daily budget. Space out ad-hoc historical pulls across sessions if repeating this kind of investigation.

---

## 8. Open items / follow-up checklist

When resuming this investigation, check in this order:

1. Does `data_pipeline/data/cas_market_watch/2026-08-06.csv` exist (on delos or synced locally) with a full run's worth of polls? First live-production confirmation of the new tracker.
2. How many days has `cas_auction_tracking.csv` / `cas_gap_fade_tracking.csv` accumulated since 2026-08-05? Is the shrinking-magnitude pattern in §4.1 continuing, reversing, or noise?
3. Did the Aug-4-expiry Nifty options data (via the ICICI Wednesday cron) and the Aug-6-expiry Sensex data (via delos, post-expiry) land? Useful for sanity-checking §5.2's Zerodha numbers against our own data, and for the still-untested Sensex expiry-day mechanism.
4. Has any SEBI/exchange response emerged regarding the reference-price settlement fix proposed in §5.1?
5. Is the overnight gap-through-stop risk (§3) still unresolved / not on any accepted-risks list? Don't assume a decision was made without being told.
6. Is Artemis still disabled? Don't assume either way without checking current state first.
