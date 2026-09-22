# MCX liquidity screen — base metals, precious metals, energy

**Status: PAUSED 2026-09-22, pending user's platform/timeframe check on the open discrepancy below.** Triggered by the user looking at other MCX commodities beyond Prometheus's current crude-oil pair, to gauge which ones have enough liquidity to be worth building a strategy on. No script files yet — this was done inline in a chat session and the numbers below are worth preserving before resuming.

## Screen (all local data, no Delos)

Window 2026-06-30 → 2026-09-21 (first clean stretch after the documented Fyers void, `mcx_fyers/`), blended with AngelOne's own `mcx/` files for September. Front month resolved empirically per day (whichever contract had the most volume that day), not from expiry arithmetic. Volume throughout is in lots — the same unit as `STATIC_UNITS`/order size, no conversion needed, and the same convention `prometheus_production/README.md`'s own CRUDEOILM capacity work uses (median boundary volume there: 153 lots over 6.5 months full-history vs. 150 lots reproduced here over the shorter window — good consistency check on the method).

**₹ turnover was deliberately not computed.** `mcx_instrument_master.csv`'s `lotsize` field is not a consistent contract-value multiplier across symbols — validated correct for CRUDEOIL/CRUDEOILM/NATURALGAS/COPPER/NICKEL/SILVER (checked against real known contract values), but off by 100–1000x for GOLD-family and the MT-denominated base metals (ALUMINIUM/ZINC/LEAD/GOLDGUINEA) — those quote price per a different physical unit than what `lotsize` multiplies against. Lots is the decision-relevant unit anyway (it's what an order size and margin both scale with), so this wasn't chased further.

### Energy — all four liquid

| Symbol | ADV (lots) | % tradable min | Median boundary vol | p10 | Max size @ 20% participation |
|---|---:|---:|---:|---:|---:|
| CRUDEOILM | 162,268 | 99.9% | 150 | 40 | 30 |
| NATGASMINI | 176,130 | 99.7% | 131 | 20 | 26 |
| NATURALGAS | 112,709 | 99.6% | 84 | 14 | 17 |
| CRUDEOIL | 62,600 | 99.5% | 48 | 10 | 10 |

### Precious metals — mostly liquid, not just Silver

| Symbol | ADV (lots) | % tradable min | Median boundary vol | p10 | Max size @ 20% |
|---|---:|---:|---:|---:|---:|
| GOLDPETAL (1g) | 221,799 | 97.3% | 181 | 28 | 36 |
| SILVERMIC (1kg) | 110,093 | 96.1% | 98 | 23 | 20 |
| GOLDM (100g) | 43,573 | 98.8% | 37 | 10 | 7 |
| SILVERM (5kg) | 35,897 | 95.4% | 30 | 6 | 6 |
| GOLDTEN (10g) | 24,180 | 94.9% | 20 | 3 | 4 |
| SILVER100 | 23,827 | 89.7% | 15 | 1 | 3 |
| GOLDGUINEA (8g) | 11,486 | 87.5% | 9 | 1 | 2 |
| SILVER (30kg) | 8,556 | 84.5% | 7 | 0 | 1 |
| GOLD (1kg) | 6,890 | 83.4% | 5 | 0 | 1 |

### Base metals — mostly thin, one clear exception

| Symbol | ADV (lots) | % tradable min | Median boundary vol | p10 | Max size @ 20% |
|---|---:|---:|---:|---:|---:|
| COPPER | 8,397 | 88.4% | 6 | 1 | 1 |
| ZINCMINI | 4,940 | 77.8% | 3 | 0 | 1 |
| ALUMINI | 3,480 | 66.7% | 2 | 0 | 0 |
| ZINC | 2,555 | 59.8% | 1 | 0 | 0 |
| ALUMINIUM | 1,856 | 47.9% | 1 | 0 | 0 |
| LEADMINI | 192 | 11.6% | 0 | 0 | 0 |
| LEAD | 154 | 9.7% | 0 | 0 | 0 |
| NICKEL | 147 | 8.6% | 0 | 0 | 0 |
| STEELREBAR | — no data — | — | — | — | — |

STEELREBAR: empty on AngelOne (`data_pipeline/data/mcx/STEELREBAR/` has no contract files), and Fyers' only file is from 2024 — looks effectively delisted/dormant, not assessed.

### Bottom line (as of the screen)

User's starting read was "other than Natural Gas and Silver, none of the other contracts have enough liquidity." The screen only partly confirms that:
- **Confirmed thin/dead:** LEAD, LEADMINI, NICKEL (essentially untradeable — under 12% of minutes see a trade, zero median boundary volume). ZINC, ALUMINIUM thin (under 60% tradable, 1-lot median boundary).
- **Confirmed liquid beyond just "Silver":** CRUDEOILM/CRUDEOIL (already live), NATGASMINI, GOLDPETAL, GOLDM, GOLDTEN, SILVERM, SILVERMIC, SILVER100, and COPPER all looked clearly tradeable in the aggregate screen — the mini/micro precious-metal variants in particular, not just the named "Silver" contract.

**Known methodology gap, not yet fixed:** a diagnostic built to check rollover cleanliness (front-month's share of that day's total volume across all listed contracts) came out biased low (50–65%) across the board — traced to Fyers and AngelOne both holding the same physical contract on overlapping days, so the "total volume that day" denominator double-counts the same real trades. Doesn't affect the ADV/tradability/boundary-volume numbers above (those only ever use one contract-day at a time), but the front-share diagnostic itself was dropped rather than reported wrong.

## Open discrepancy — GOLDPETAL/GOLDTEN vs SILVERMIC, user's own eyeballing

User's own read from looking at charts: "For GOLDPETAL/GOLDTEN, there are so many minute bars where the volume traded was 0. I don't see that in SILVERMIC or NATGASMINI." This does not match either the full-window screen above or a tighter recheck:

**Full window (2026-06-30→09-21, blended Fyers+AngelOne, front month by day):**
- GOLDPETAL 2.1% zero-volume minutes, GOLDTEN 4.5%, SILVERMIC 3.9%, NATGASMINI 0.3%.
- i.e. GOLDPETAL *better* than SILVERMIC, GOLDTEN roughly the same as SILVERMIC — not the "so many zero bars" gap described.

**Tighter recheck, current front-month contract only, most recent 2 weeks (2026-09-08→09-21), AngelOne only (freshest source), front month reconfirmed empirically per day rather than assumed from the filename:**
- GOLDPETAL (Sept contract, 09-30 expiry): 0.1% zero-volume minutes (11 of 8,221).
- GOLDTEN (Sept contract, 09-30 expiry): 0.9% zero-volume minutes (78 of 8,221).
- SILVERMIC (**Nov contract**, 11-30 expiry — confirmed this, not Aug, is current front month by volume): 0.0% zero-volume minutes (1 of 8,220).
- NATGASMINI (Sept contract, 09-25 expiry): 0.4% zero-volume minutes (37 of 8,223).
- Daily volume for GOLDPETAL's Sept contract this window: 165K–387K lots/day. SILVERMIC's Nov contract: 74K–202K lots/day. Comparable character, not a different tier.

**Still unreconciled.** Local downloaded data (both sources, current front-month contracts, recent window) shows GOLDPETAL/GOLDTEN just as populated as SILVERMIC, not worse. Possible explanations not yet checked:
1. User's platform/feed may not be AngelOne (e.g. Zerodha/Kite, TradingView, a different vendor) — a display or feed-completeness issue independent of real MCX liquidity.
2. A different timeframe (e.g. 5-min candles) could show zero-volume bars differently than the 1-min reindex used here.
3. A specific date/time the user was looking at, not represented in the 2-week window above.
4. AngelOne's files drop zero-volume minutes entirely from the raw CSV (confirmed separately, e.g. `COPPER`: minimum non-empty `volume` value present is 1) — the "zero" counts above are reconstructed by reindexing each day onto its own observed 1-min grid and treating a missing row as 0, not read literally off the file. If the user is looking at a raw AngelOne CSV directly rather than a chart, they would see zero *rows entirely absent*, not printed as `0` — worth confirming this isn't just a difference in what "seeing a 0" means between the two of us.

**Next step:** waiting on user to specify platform, timeframe/candle size, and the approximate date/time they were looking at, so this can be checked against the same exact slice.
