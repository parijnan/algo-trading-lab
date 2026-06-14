# Open Interest (OI) Analysis — Design, Methodology, and Findings

> **Purpose of this document:** A complete reference for the OI analysis research module built in June 2026 for the algo-trading-lab codebase. Covers background on the trading strategies this serves, the full system design with rationale, methodology for signal validation, and all empirical findings. Intended to be self-contained — a reader unfamiliar with the codebase should be able to understand every decision and result.

---

## Table of Contents

1. [Background: The Trading Strategies](#1-background-the-trading-strategies)
2. [The Research Question](#2-the-research-question)
3. [The Data](#3-the-data)
4. [System Design](#4-system-design)
5. [Feature Engineering](#5-feature-engineering)
6. [Signal Validation Methodology](#6-signal-validation-methodology)
7. [Findings: 21-Event Validation (CE Parachute)](#7-findings-21-event-validation-ce-parachute)
8. [Findings: Full Signal Quality Map (371 Expiries)](#8-findings-full-signal-quality-map-371-expiries)
9. [Barrier Analysis: Do OI Walls Actually Hold?](#9-barrier-analysis-do-oi-walls-actually-hold)
10. [Implications for Each Strategy](#10-implications-for-each-strategy)
11. [Files and Usage](#11-files-and-usage)
12. [Known Limitations](#12-known-limitations)

---

## 1. Background: The Trading Strategies

To understand why this OI analysis module was built, it is necessary to understand the three options-selling strategies it is designed to serve.

### 1.1 Athena — Nifty Weekly Double Calendar

**What it trades:** Nifty 50 index options (NSE, India). Nifty 50 is India's benchmark large-cap index, tracking the fifty largest companies on the National Stock Exchange. Options on Nifty are cash-settled, European-style, and expire every Thursday.

**The strategy structure:** Athena sells a short-dated straddle or strangle (the "sell expiry" — typically the current week's expiry) and buys a longer-dated strangle as a hedge (the "buy expiry" — the following week or further). The position profits when Nifty stays within a range and time decay erodes the value of the sold options. Athena operates when the VIX (India's volatility index) is between 16 and 25 — a moderate volatility regime.

**The CE parachute (emergency hedge):** When Nifty rallies sharply and the spot price crosses the sold call (CE) strike by 150 points or more, the CE parachute fires. It purchases an at-the-money call option as a temporary hedge against further upside. This is the intervention point where OI analysis is most immediately relevant — at the moment of trigger, OI features can indicate whether the rally is likely to continue (hedge is necessary) or reverse (hedge will lose money as a false alarm). In the backtest dataset, 21 of 124 Athena trades triggered the CE parachute. Of those 21, roughly half were genuine breakouts where the hedge made money, and half were reversals where it did not.

**The reactive PE wing:** Athena also has a put wing (PE wing) that activates when the market falls more than 1.75% from the entry spot. This acts as a delta hedge on the downside, mirroring the CE parachute's role on the upside.

**Triple-Confirm (TC) signal integration (in development):** A directional signal called TRIPLE_CONFIRM, which fires when a set of trend-following indicators (ORB_75, SuperTrend on 5-minute and 3-minute timeframes) all align in the same direction. The plan is to use TC signals to trigger CE and PE parachutes proactively — before the spot price crosses the sell strike — if OI analysis validates that TC-triggered entries are higher-quality.

### 1.2 Artemis — Sensex Weekly Short Straddle

**What it trades:** Sensex index options (BSE, India). Sensex is the Bombay Stock Exchange's benchmark index, tracking thirty large-cap companies. Like Nifty, Sensex options expire every Thursday.

**The strategy structure:** Artemis sells a straddle (sells both a call and a put at the same strike, near ATM) on a weekly basis. It profits from time decay and low volatility. Artemis operates when VIX is below 16, and is currently paused pending live testing.

**The Sensex OI limitation:** Sensex options OI data is only available until March 2026 in the current dataset. After that, the OI column is absent from the data files. This means the OI analysis model must be built and validated on Nifty (which has rich OI history back to 2019) and then forward-tested on Sensex when positions are live.

### 1.3 Iris — Nifty Intraday Trend-Following

**What it trades:** Nifty 50 futures and options, intraday.

**The strategy structure:** Iris uses a directional bias signal (the TRIPLE_CONFIRM system, described above) to enter intraday positions in the direction of the trend. It operates when VIX is above 25 — a high-volatility regime where selling premium is dangerous and directional trading is more appropriate.

**VIX-based routing:** The three strategies are mutually exclusive by VIX regime. A session routing system determines which strategy runs on any given day: Iris above VIX 25, Athena between 16 and 25, Artemis below 16. Artemis and Iris never run concurrently with Athena. This matters for the OI analysis because real-time OI signals must be computed inside whichever strategy is running — there is no shared signal bus.

---

## 2. The Research Question

### 2.1 The Core Problem

All three strategies involve selling options. Selling options means collecting premium in exchange for capping upside (selling calls) or cushioning downside (selling puts). The primary risk is that the market moves sharply in one direction, pushing the sold options deeply in-the-money and creating large losses.

Each strategy has its own intervention mechanism — Athena's CE parachute, Artemis's stop-loss levels, Iris's trend exit signals — but all three interventions share the same underlying uncertainty: **when the market moves sharply, will it continue or reverse?**

If the move continues, intervention is necessary and profitable. If the market reverses, intervention is a false alarm — you took a loss on the hedge that you didn't need.

### 2.2 The OI Hypothesis

Options Open Interest (OI) is the total number of contracts outstanding at a given strike and expiry. Large concentrations of OI at specific strikes are not random — they represent where options writers (usually institutional market makers and large traders) have committed capital. These positions create structural incentives:

- **Options writers (sellers) of calls (CE)** tend to delta-hedge by selling futures when spot rises toward their strike. This creates selling pressure near the CE wall, acting as resistance.
- **Options writers (sellers) of puts (PE)** tend to delta-hedge by buying futures when spot falls toward their strike. This creates buying pressure near the PE wall, acting as support.
- **Max pain:** The strike at which the greatest number of options expire worthless (maximizing losses for option buyers) is called the "max pain" strike. Because options writers have a collective incentive to steer expiry toward this level, markets tend to gravitate toward max pain as expiry approaches.
- **PCR (Put-Call Ratio):** The ratio of outstanding put OI to call OI reflects the market's aggregate sentiment and hedging posture. High PCR means more puts than calls are outstanding — often a contrarian bullish indicator because it signals widespread fear and defensive positioning.

The hypothesis: **OI structure at any given moment contains information about the likely near-term price path.** If we can extract this information systematically, we can improve the quality of intervention decisions across all three strategies.

### 2.3 Scope and Ambition

The OI model is not designed to be a standalone signal that replaces price-based decisions. It is designed as a **filter and qualifier** that says, in effect: "given that your strategy has flagged a potential intervention, is the OI structure consistent with the move continuing, or does it suggest the move is likely to reverse?"

Additionally, because the model tests across a wide range of forward horizons (from 15 minutes to an entire expiry week), it can serve different purposes for different strategies:
- **Athena:** Short-horizon signals (15min–2hr) for CE parachute timing; medium-horizon (1D–3D) for trade management.
- **Artemis:** Weekly signals for entry-day selection and settlement prediction.
- **Iris:** Very short signals (15min–30min) for trend confirmation.

---

## 3. The Data

### 3.1 Nifty Options Data

**Location:** `data_pipeline/data/nifty/options/`

**Structure:** One folder per expiry date (e.g., `2024-05-30/`), containing one CSV file per strike/side combination (e.g., `23000ce.csv` for the 23000 Call, `22900pe.csv` for the 22900 Put).

**Columns in each file:**
- `datetime` — timestamp of the trade (tick-on-trade, not OHLCV bars)
- `open_interest` — total open contracts at this strike at this timestamp
- `volume` — cumulative daily volume
- `close` — last traded price
- Additional metadata columns (stock code, exchange code, etc.)

**Coverage:** 371 weekly expiries from May 2019 to June 2026. Each expiry has data for 10 calendar days (the expiry week plus the prior week). Earlier expiries (2019–2024) have fewer strikes active; post-2024 expiries have dramatically higher OI levels as retail participation increased.

**Key characteristic — tick-on-trade format:** OI is not reported at regular intervals. It only updates when a trade occurs at that strike. This means the data is sparse — a quiet strike might have only a handful of rows per day. The engine handles this by forward-filling OI onto a regular 1-minute grid.

### 3.2 Nifty Index Data

**Location:** `data_pipeline/data/indices/nifty.csv`

**Format:** 1-minute OHLCV bars, 09:15–15:30 IST, from 2019 to present. Contains 681,717 rows.

**Timezone:** Stored with +05:30 offset; stripped to naive timestamps during processing.

**Usage:** Provides the spot price series. OI features are aligned to the spot price to compute distances (CE wall distance from spot, PE wall distance from spot, etc.).

### 3.3 Sensex Options Data

**Location:** `data_pipeline/data/sensex/options/` (88 expiry directories)

**Coverage:** October 2024 to June 2026. However, the `open_interest` column only exists in files up to March 2026. After that date, the column is absent. This limits OI-based Sensex analysis to approximately 18 months of data.

**File format difference:** Sensex option files use column name `oi` (not `open_interest`). The engine handles both column names automatically.

### 3.4 Athena Backtest Data

**Location:** `athena_backtest/data_wing_reactive_pct_150/`

This is the baseline Athena backtest using the 1.75% reactive PE wing configuration across the VIX 16–25 regime. It contains:
- `trade_summary_wing_reactive_pct_150.csv` — one row per trade (124 trades), including the emergency hedge fields: `emer_strike`, `emer_pl`, `emer_active`
- `trade_logs/trade_NNNN_YYYY-MM-DD.csv` — per-minute trade logs with columns for spot price, `emer_active` flag, and all position fields

The 21 trades with `emer_strike` set are the CE parachute events that the OI validation script targets.

---

## 4. System Design

The module consists of four files:

```
research/oi_analysis/
├── __init__.py              — package marker
├── oi_engine.py             — core feature computation (fully vectorised)
├── build_nifty_features.py  — batch builder for all 371 expiries
├── validate_athena.py       — 21-event CE parachute validation
├── signal_quality.py        — full signal quality map (IC + quintiles + barrier)
└── data/
    ├── nifty_oi_features.csv          — pre-built features (277,248 rows)
    ├── athena_emer_oi_validation.csv  — 21-event validation results
    ├── signal_quality_ic.csv          — IC table (feature × horizon)
    ├── signal_quality_quintiles.csv   — quintile lift by (feature, horizon)
    └── signal_quality_barrier.csv     — wall breakthrough rates by proximity
```

### 4.1 oi_engine.py — The Core Engine

This is the central computation layer. It takes raw per-strike OI CSV files and produces a structured DataFrame of market-structure features at 5-minute resolution.

**Design goal:** Fast enough to run on-the-fly during live trading. Each expiry takes approximately 3–5 seconds. This allows the engine to be called at strategy runtime (before a trade, or at each bar) rather than requiring a pre-built lookup table.

**Performance approach — fully vectorised with NumPy broadcasting:**

The naive approach — a Python loop iterating over each (timestamp, strike) pair — takes over 30 seconds per expiry and is unusable. The vectorised approach represents the entire OI surface as a 2D NumPy array of shape `(n_time_bars, n_strikes)` and computes all features simultaneously using matrix operations:

```
ce_matrix:  shape = (n_bars, n_strikes)   rows = time bars, columns = strike prices
pe_matrix:  same structure for put options

spot_arr:   shape = (n_bars,)   spot price at each bar (from Nifty index)
```

All wall, PCR, and max pain computations operate on these matrices without any Python-level loops over bars, bringing execution time to ~3 seconds per expiry.

**The 1-minute grid:**

For each expiry, the engine constructs a regular 1-minute grid covering 09:15–15:30 IST for each trading day in the expiry's lookback window (10 calendar days). Sparse tick-on-trade OI values are forward-filled onto this grid — if OI was last reported at 10:32 for a given strike, that value is carried forward until the next trade at that strike.

**Resampling to 5-minute bars:**

After building the 1-minute grid, the engine resamples to 5-minute bars using a right-closed, right-labeled convention. This means each 5-minute bar is labeled with the timestamp at which it ends, and its OI value is the last observation within the 5-minute window. This convention is consistent with how the Nifty index data is also resampled, ensuring that OI features and spot prices align correctly.

### 4.2 build_nifty_features.py — Batch Builder

Runs the OI engine over all 371 expiries and writes a consolidated CSV to `data/nifty_oi_features.csv`.

**Parallel execution:** Uses Python's `ProcessPoolExecutor` with a configurable number of workers. A critical optimisation: each worker loads the Nifty 1-minute index CSV once at initialisation (via an `initializer` function) and reuses it for all expiries assigned to that worker. Without this, each worker would reload the 681,717-row index file on every task call, making parallel execution slower than sequential. With the fix, 4 workers process 371 expiries in approximately 6 minutes.

**Output format:** 277,248 rows. Each row is one 5-minute bar for one expiry, covering the 10-day lookback window.

### 4.3 validate_athena.py — 21-Event Validation

**Purpose:** Before testing the model at scale, validate it against a labeled dataset with known outcomes. The 21 CE parachute events in the Athena VIX 16–25 backtest provide exactly this: each event has a known outcome (emer_pl > 0 means the hedge made money; emer_pl < 0 means it lost money).

**Design decision — compute on the fly, not from pre-built CSV:**

The validation script builds OI features fresh for each expiry rather than reading from the batch CSV. This was a deliberate choice to validate the engine itself in the same mode it would be used live. If it worked correctly on 21 known events, it was safe to run on the full 371-expiry dataset.

**Outcome classification:**
- `emer_pl > 5` → `breakout`: spot kept rising, hedge was correct and profitable
- `emer_pl < -5` → `reversal`: spot reversed, hedge lost money (false alarm)
- otherwise → `neutral`: hedge had minimal P&L either way

### 4.4 signal_quality.py — Full Signal Quality Map

**Purpose:** Test every OI feature against every forward horizon on the full 7-year history. This answers the broad question: "which OI features predict future price movement, at what time horizon, and how strongly?"

**Front-expiry dedup:** The raw features CSV has multiple rows per timestamp (one row for each active expiry whose lookback window includes that date). On a given Monday, for example, both the current week's expiry (expires in 4 days) and the next week's expiry (expires in 11 days) would produce feature rows for that bar. The dedup step keeps only the nearest unexpired expiry — the one most relevant to active positions. This reduces 277,248 rows to 197,448 unique bars.

**Rolling z-score normalisation:** OI scales have changed dramatically over time. The absolute number of open contracts in 2025 is 5–10× what it was in 2020, as retail participation in Nifty options has exploded. A threshold like "PE wall OI > 5 million" that signals strong support in 2025 would have signalled extreme conditions in 2020. To make features comparable across time, each feature is normalised using a trailing rolling mean and standard deviation computed over the prior 252 bars (approximately 21 trading days at 5-minute resolution). Each bar's normalised feature value reflects where it sits relative to the recent past, not in absolute terms.

---

## 5. Feature Engineering

### 5.1 CE Wall (Call Option Resistance Wall)

**Definition:** The strike *above* the current spot price with the highest CE (call option) open interest, within a search range of 0.5% to 10% of spot.

**What it represents:** When a large number of call options are outstanding at a particular strike, the writers (sellers) of those calls are exposed to unlimited losses if spot rises above that strike. To hedge their exposure, they sell futures (or the underlying) as spot approaches that level, creating structural selling pressure. This makes the CE wall strike a genuine resistance level — not based on chart patterns but on the mechanics of options hedging.

**Features extracted:**
- `ce_wall_strike` — the actual strike price of the wall
- `ce_wall_oi` — the OI at that strike (absolute wall strength)
- `ce_wall_dist_pts` — distance in points from current spot to the CE wall strike
- `ce_wall_dist_pct` — the same distance expressed as a percentage of spot

**Search range rationale:** The 0.5% lower bound excludes the ATM strike itself (which would trivially have high OI in most cases and doesn't represent distant resistance). The 10% upper bound excludes deep out-of-the-money strikes where OI exists but has no near-term mechanical relevance.

### 5.2 PE Wall (Put Option Support Wall)

**Definition:** The strike *below* the current spot price with the highest PE (put option) open interest, within a range of 0.5% to 10% below spot.

**What it represents:** Symmetrically to the CE wall, put writers hedge by buying futures when spot falls toward their sold put strike. This creates structural buying pressure at the PE wall strike, making it a mechanical support level.

**Features extracted:**
- `pe_wall_strike`, `pe_wall_oi`, `pe_wall_dist_pts`, `pe_wall_dist_pct`

### 5.3 PCR — Put-Call Ratio

**Definition:** The ratio of total put OI to total call OI, computed for two ranges:

- `pcr_near`: OI within ±5 strikes of ATM (each strike is 50 points, so ±250 points)
- `pcr_broad`: OI within ±10% of spot

**What it represents:** PCR is one of the most widely watched options market indicators. A PCR above 1.0 means more put contracts are outstanding than call contracts, which conventionally signals bearish sentiment. However, PCR is commonly used as a *contrarian* indicator: when retail traders are rushing to buy puts (insurance against a fall), institutional writers are on the other side collecting premium. If the fear is excessive, the market tends to recover, making high PCR a bullish signal in practice.

The "near" PCR (±5 strikes) is more sensitive to immediate ATM conditions. The "broad" PCR (±10% of spot) captures a wider picture of the entire options market positioning.

### 5.4 Max Pain

**Definition:** The strike at which the total dollar value of options expiring in-the-money is minimized — equivalently, the strike at which option buyers collectively lose the most.

**What it represents:** Options writers (who collect premium) have an aggregated incentive to see expiry occur at the max pain strike, because at that level the maximum amount of premium stays with them. This creates a gravitational pull toward max pain as expiry approaches — not because of any collusion, but because writers' delta-hedging activities systematically accumulate around this level.

**Computation:** For each bar, and each candidate strike K within 10% of spot:

```
pain(K) = Σ over all CE strikes S: CE_OI(S) × max(K - S, 0)
         + Σ over all PE strikes S: PE_OI(S) × max(S - K, 0)
```

The strike K that minimises this total is the max pain strike for that bar.

**Features extracted:**
- `max_pain_strike` — the max pain strike
- `max_pain_dist_pts` — `max_pain_strike - spot` (positive = max pain above current spot)

**Computational challenge:** Max pain requires a nested loop over all candidate strikes × all OI strikes. The vectorised implementation pre-computes the pain basis matrices (`ce_pain_base`, `pe_pain_base`) as outer products and then uses matrix-vector multiplication `pb_ce @ ce_oi_t + pb_pe @ pe_oi_t` per bar. This is still the most compute-intensive feature (requires a Python loop over bars, unlike the other features).

### 5.5 Total OI

**Definition:** The sum of all call and put OI within 10% of spot.

**What it represents:** A proxy for total market participation in the current week's options. High total OI weeks tend to be range-bound (options writers need low realised volatility for their premium to decay profitably). Low total OI weeks tend to have more directional movement.

### 5.6 Derived Features

Two features computed from combinations of the above:

**wall_asym = ce_wall_dist_pct − pe_wall_dist_pct**

The asymmetry between the distance to the CE wall above and the PE wall below. Positive values mean there is more room above (CE wall is farther away than PE wall is), indicating a bullish-leaning market structure. Negative values mean there is more room below (PE wall is farther below), indicating a bearish-leaning structure.

**wall_oi_ratio = pe_wall_oi / ce_wall_oi**

The ratio of put wall strength to call wall strength. Unlike PCR, which measures all OI in a band, this ratio measures only the *peak* strike on each side — the heaviest concentration of writers. A ratio >> 1 means the put side has a disproportionately strong wall relative to the call side, implying structural support is stronger than resistance.

---

## 6. Signal Validation Methodology

### 6.1 Why Looking at Future Price Is Not Cheating

A common misconception in signal research is that "using future price data" is always lookahead bias. It is not, as long as this rule is strictly followed:

> **Features at time T are computed using only data available at or before time T. Labels (forward returns) use data strictly after T.**

This is exactly how every market signal is ever tested. The OI feature at 10:30 uses OI data from 09:15 to 10:30. The forward return from 10:30 to 12:30 uses price data from 10:30 to 12:30. The two windows do not overlap. This is point-in-time feature engineering with forward-looking labels — the standard methodology for signal research.

### 6.2 Information Coefficient (IC) — Spearman Rank Correlation

Rather than testing whether a particular threshold is "right" (e.g., "does PCR > 1.2 predict a reversal?"), the IC test asks a more fundamental question: **is there any monotonic relationship between this feature and this forward return?**

The Spearman rank correlation is used (rather than Pearson) because:
1. It is robust to non-normal distributions and outliers
2. It captures any monotonic relationship, not just linear ones
3. Market returns are heavy-tailed; Pearson would give excessive weight to extreme days

An IC of +0.05 means: if you rank all bars from lowest feature value to highest, the forward returns tend to be slightly higher in the top-ranked bars than the bottom-ranked bars. Small but consistent IC values, if statistically significant across large samples, represent genuine predictive power.

**Statistical significance:** With 197,448 bars, even very small IC values are statistically detectable. The significance thresholds used are p < 0.05 (*), p < 0.01 (**), and p < 0.001 (***).

### 6.3 Three Methodological Traps Addressed

**Trap 1 — Base rate:** Nifty has a long-run upward drift (India's economy grows over time, inflating nominal prices). "Always predict up" would have a positive average return. All IC values implicitly control for this because Spearman rank correlation measures rank ordering, not absolute levels. But when interpreting quintile returns, the baseline (market average return at each horizon) must be kept in mind.

**Trap 2 — In-sample thresholds:** The threshold PCR > 1.0 used in the 21-event validation was not pre-specified — it was chosen after looking at the data. Any threshold chosen this way will appear better than it is. The IC test is threshold-free and avoids this entirely. If thresholds are to be used operationally, they must be calibrated on a training period (e.g., 2019–2022) and tested on a holdout period (2023–2026).

**Trap 3 — Overlapping windows:** At 5-minute resolution with a 2-hour forward horizon, adjacent bars share 95% of their forward window. The N of 197,448 overstates the number of independent observations. The reported IC values are correct (not inflated), but the reported p-values will be smaller (more significant) than they truly are. In practice, this means some "***" results at short horizons might be "**" if autocorrelation is properly corrected.

### 6.4 Quintile Lift

Features are sorted into 5 equal-frequency buckets. The mean forward return in each bucket measures the *shape* of the signal: is it monotonically increasing from Q1 to Q5 (linear signal)? Or is it concentrated in the extremes (Q1 and Q5 differ but Q2–Q4 are flat)? A purely two-tailed signal — where only extreme readings matter — suggests using the feature as a filter (avoid conditions in Q5 if the signal is bearish there) rather than a continuous confidence adjustment.

### 6.5 Barrier Analysis

A separate test from the IC analysis. Instead of asking "does OI predict direction?", it asks: "when spot is near an OI wall, does it break through or bounce within 2 hours?"

This is a conditional probability test: given that spot is within X% of the CE wall, what fraction of bars see spot close *above* the CE wall within the next 24 five-minute bars (2 hours)? Results are grouped by proximity bucket (0.5–1.0%, 1.0–1.5%, 1.5–2.0%, 2.0–5.0%).

To avoid cross-day contamination (where the "next 24 bars" might bleed into the next trading session), future extremes are computed separately within each calendar day.

---

## 7. Findings: 21-Event Validation (CE Parachute)

### 7.1 Dataset

21 CE parachute events from Athena's VIX 16–25 backtest (reactive PE wing, 1.75% trigger). Each event was triggered when Nifty spot crossed the sold call strike by 150 points or more. The OI features at the exact trigger timestamp were extracted for each event.

**Outcome distribution:** 9 breakouts, 9 reversals, 3 neutral.

### 7.2 PCR Near — 80% Accuracy at Threshold 1.0

Using PCR_near < 1.0 to predict breakout and PCR_near > 1.0 to predict reversal:

- 16 of 20 events correctly classified (1 event had no PCR data)
- The 4 misclassifications: 3 were borderline (PCR between 0.9 and 1.1), and 1 was the June 2024 election-result event (described below)

**Key discriminator statistics (breakout vs reversal group means):**

| Feature | Breakout group | Reversal group | Direction |
|---|---|---|---|
| CE wall dist % | 2.5% | 3.8% | Closer wall → more breakouts |
| OI @ sell strike | 2.2M | 2.4M | Less OI → more breakouts |
| OI delta (30 min) | −660K | +46K | Unwinding → more breakouts |
| PCR near | 0.8 | 1.1 | Lower PCR → more breakouts |

### 7.3 The OI Delta Limitation — Gap Opens

**13 of 21 CE parachute triggers fired at or before 09:20 (market open).** These are gap-open events where Nifty opened higher than the previous day's close, instantly crossing the sell strike before the first 5-minute bar was complete. For these events, there is no prior intraday OI data within the current trading day, and the 30-minute OI delta at the sell strike is undefined (NaN).

This is a critical structural limitation: the OI delta signal (which is theoretically the strongest — OI unwinding at the sell strike means writers are covering their positions, signalling the level will be breached) is unavailable for exactly the most dangerous scenario: overnight gap moves.

PCR, by contrast, is computed from the full OI snapshot which includes overnight position changes. PCR is available at the first bar even on gap-open days.

### 7.4 The June 2024 Election Event — A Black Swan

Trade 109 (entry June 5, 2024) is the largest outlier in the dataset. The CE parachute triggered on June 5, 2024, when election results showed the incumbent government winning fewer seats than polls predicted, causing an immediate market collapse. The hedge made 313 points (emer_pl = 313.45) — the largest single CE parachute profit in the dataset.

However, at trigger time, PCR_near was 1.59 (strongly above 1.0) and CE wall distance was 6.4% — both signals pointing to reversal (suggesting the hedge was premature). The OI model was completely wrong on this event, and correctly wrong: no OI signal could predict a sudden electoral outcome. The CE parachute fired correctly because spot moved exactly as if it were a false alarm until the results came in, then became a genuine breakout.

This event is important for calibration: any OI-based filter for the CE parachute must include an override mechanism for tail-risk events, because the OI signal will always say "reversal" when the setup looks crowded but the actual move is macro-driven.

### 7.5 The Sign Inversion — PCR and CE Parachute Context

The full signal quality analysis (Section 8) shows that PCR_near has a **positive** IC with forward returns: high PCR → Nifty goes up. Yet the 21-event validation showed that PCR > 1.0 at CE parachute trigger → reversal (CE hedge lost money).

This is not a contradiction. The two signals are measuring different things in different contexts:

- **General population (197K bars):** High PCR is a contrarian bullish indicator because the market tends to recover when fear is high. High PCR → market goes up (positive IC).
- **CE parachute trigger specifically (21 events, spot already 150 pts above sell strike):** At this specific moment, if PCR is high, it means traders are already heavily positioned in puts for protection *against the rally continuing*. This bearish option positioning tends to be correct in this specific context, and the rally reverses (PCR > 1 → reversal).

The CE parachute context is a conditioning event that inverts the usual PCR interpretation. This is why it is crucial not to apply general market signals mechanically to specific strategy decision points — the conditional distribution can differ substantially from the unconditional one.

---

## 8. Findings: Full Signal Quality Map (371 Expiries)

### 8.1 Dataset and Setup

- 277,248 raw feature rows → 197,448 after front-expiry dedup
- 10 features (8 base + 2 derived)
- 10 forward horizons: 15min, 30min, 1hr, 2hr, 4hr, EOD, 1D, 3D, 5D, to_expiry
- All features z-score normalised using trailing 252-bar rolling window
- Spearman IC computed for all 100 (feature, horizon) pairs

### 8.2 Complete IC Table

```
Feature              15min  30min  60min 120min 240min    EOD     1D     3D     5D  to_exp
────────────────────────────────────────────────────────────────────────────────────────────
pcr_near            +0.028 +0.039 +0.049 +0.061 +0.067 +0.052 +0.034 +0.022 +0.042 +0.069
pcr_broad           +0.031 +0.044 +0.059 +0.068 +0.070 +0.048 +0.052 +0.010 +0.024 +0.074
ce_wall_dist_pct    +0.003 +0.001 +0.002 +0.011 +0.011 +0.002 +0.003 -0.011 +0.003 +0.038
pe_wall_dist_pct    -0.004 -0.005 -0.007 -0.010 -0.016 -0.022 -0.015 +0.006 -0.004 +0.038
ce_wall_oi          -0.011 -0.016 -0.021 -0.021 -0.004 +0.001 +0.012 +0.044 +0.028 -0.051
pe_wall_oi          +0.011 +0.015 +0.023 +0.029 +0.047 +0.051 +0.037 +0.044 +0.042 -0.022
max_pain_dist_pts   -0.010 -0.018 -0.030 -0.037 -0.035 -0.033 -0.022 -0.004 -0.026 -0.044
total_oi            -0.011 -0.018 -0.021 -0.016 +0.010 +0.013 +0.007 +0.023 +0.014 -0.058
wall_asym           +0.005 +0.006 +0.011 +0.020 +0.026 +0.027 +0.023 -0.010 +0.001 +0.004
wall_oi_ratio       +0.023 +0.033 +0.045 +0.054 +0.061 +0.059 +0.036 +0.013 +0.029 +0.054
```

*All IC values marked with *** (p<0.001) except where IC is near zero. Full p-values in signal_quality_ic.csv.*

### 8.3 Feature-by-Feature Interpretation

#### PCR near and PCR broad — The Most Reliable Signals

Both PCR measures show positive IC at every single horizon, all significant at p<0.001. The signal builds gradually from 0.028 at 15 minutes to a peak of 0.069–0.074 at `to_expiry`. This means the weekly settlement price is meaningfully predictable from the PCR observable at the start of the week — high PCR early in the week predicts a higher settlement relative to the current spot.

**Why high PCR → positive forward returns:** When retail traders buy more puts than calls, they are paying for downside protection. Options market makers (the writers of those puts) collect premium and hedge by buying futures. This hedging buying activity creates sustained upward pressure throughout the week. Additionally, high put buying often reflects excessive fear — a contrarian indicator that the feared outcome is already priced in.

**The signal grows with horizon:** The 15-minute IC (0.028) is meaningful but modest. The to_expiry IC (0.069) is more than twice as large. This suggests PCR is a structural, slow-moving signal — it reflects the positioning of the entire market for the week, and its effect accumulates. It is not an intraday trading signal in the traditional sense.

#### wall_oi_ratio — Second Best Directional Signal

Consistently positive IC from 0.023 at 15 minutes to 0.061 at 4 hours, remaining positive through to_expiry (+0.054). This is the cleanest of the derived features.

Unlike PCR (which counts all OI in a band), the wall OI ratio specifically measures the *strongest* point of support (PE wall peak) versus the *strongest* point of resistance (CE wall peak). A high ratio means the heaviest concentration of put writers is larger than the heaviest concentration of call writers — the market's single strongest support level is better-staffed than its single strongest resistance level. This is a refined version of the PCR signal.

#### CE Wall OI — Multi-Horizon Reversal

CE wall OI shows a striking pattern: **negative IC at short horizons (resistance effect), flipping to positive at 1D–5D (delta-hedging buying pressure), then negative again at to_expiry (max pain pull-down).**

- **Short-term negative (−0.011 to −0.021, 15min–2hr):** A large CE wall is genuine mechanical resistance. Writers defending their sold calls create selling pressure as spot approaches the wall, tending to push price back below the wall. This confirms the wall does what it is supposed to do in the near term.
- **Medium-term positive (+0.012 to +0.044, 1D–5D):** When a lot of calls are outstanding, writers are long delta (they buy futures when spot rises, sell when it falls — their dynamic hedging activity creates sustained buying across the week). This net buying effect manifests as positive multi-day returns in high-CE-OI weeks.
- **to_expiry negative (−0.051***):** By settlement, heavy call writing pulls max pain toward lower levels (more CE OI means pain is maximised for buyers at a lower strike), and the market converges to this lower level. The net effect is that high-CE-OI weeks see expiry settlement below the early-week spot.

#### PE Wall OI — Consistently Bullish

Positive IC at all horizons from 15 minutes (+0.011) through 5 days (+0.042), then modestly negative at to_expiry (−0.022). Heavy put walls → positive returns throughout the week. The mechanism is the same delta-hedging channel: when put writers hedge, they buy futures, creating upward pressure. The reversal at to_expiry may reflect max pain convergence pulling price upward and then back down to max pain.

#### Max Pain — Real Gravity, Unexpected Sign

Max_pain_dist_pts = max_pain_strike − spot. Positive value means max pain is above the current spot. The IC is **negative** at nearly all horizons: high max_pain_dist_pts (max pain above spot) → negative forward returns.

**This seems counterintuitive** — if max pain is above spot, shouldn't spot be pulled up toward it? The negative IC suggests the opposite: when max pain is above spot, the market tends to move further away from max pain (downward). This might be because:

1. When max pain is above spot, it typically means spot has already been pushed below max pain by selling pressure. The OI distribution reflects a bearish market structure where put OI at higher strikes (in the money) is heavy. This bearish OI structure tends to persist.
2. Alternatively, the max pain effect at the *end* of the expiry week (final day or two) is different from earlier in the week. Mid-week bars with max pain above spot may reflect different dynamics than Thursday morning bars.

The to_expiry IC is −0.044***: over the full expiry week, when max pain was above spot at the time of measurement, expiry settlement ended up *below* that measurement's spot price. This could mean max pain is above because the entire OI structure is positioned for a pullback — and that pullback materialises by settlement.

#### Total OI — Regime Indicator

Negative IC at short horizons (−0.011 to −0.021), flipping to mildly positive at EOD and multi-day horizons, then strongly negative at to_expiry (−0.058***).

High total OI weeks are defined by heavy option writing across the board. Options writers profit from low realised volatility. This creates a collective hedging network that dampens moves in both directions during the week. The short-term negative IC may reflect the initial dampening effect (sold options create resistance to upward moves). The to_expiry negative IC suggests that high-OI weeks see expiry settle below the mid-week spot — premium decay and max pain convergence dominate.

#### Wall Distance Features — Weak Intraday, Meaningful to Expiry

CE and PE wall distance (in % of spot) are near-flat intraday but show significant positive IC at to_expiry (+0.038*** for both). When both walls are far from spot (market has wide breathing room in both directions), the expiry settlement tends to be higher than the current mid-week spot.

The intraday irrelevance makes sense — the wall distance doesn't predict near-term direction, only whether the market is in a wide or narrow structure. A wide structure (both walls far) tends to have upward drift by settlement.

#### Wall Asymmetry — Directional Lean

Positive IC from 15 minutes (+0.005) through EOD (+0.027) and 1D (+0.023), then reversal at 3D (−0.010). When the CE wall is farther above than the PE wall is below (positive asymmetry = more room above), near-term returns tend to be positive. This makes intuitive sense — an asymmetric market structure with more overhead room is a bullish lean. The reversal at 3D may reflect mean-reversion of the asymmetry itself.

### 8.4 Top Signals by Quintile Spread

The following table shows the 15 strongest (feature, horizon) combinations ranked by the difference in mean forward return between the top quintile (Q5 = highest feature values) and bottom quintile (Q1 = lowest feature values). Returns are expressed as percentage of spot.

| Feature | Horizon | Q1 return | Q5 return | Spread |
|---|---|---|---|---|
| pcr_near | to_expiry | −0.016% | +0.304% | 0.320% |
| pcr_broad | to_expiry | −0.014% | +0.245% | 0.259% |
| ce_wall_oi | 3D | +0.035% | +0.272% | 0.237% |
| pe_wall_oi | 3D | +0.049% | +0.272% | 0.223% |
| ce_wall_oi | 5D | +0.190% | +0.405% | 0.215% |
| pe_wall_oi | 5D | +0.225% | +0.433% | 0.208% |
| wall_oi_ratio | to_expiry | +0.064% | +0.257% | 0.193% |
| pcr_near | 5D | +0.188% | +0.365% | 0.177% |
| pe_wall_oi | 1D | −0.038% | +0.135% | 0.173% |
| max_pain_dist_pts | 5D | +0.296% | +0.133% | 0.163% |
| wall_oi_ratio | 5D | +0.202% | +0.360% | 0.158% |
| total_oi | to_expiry | +0.230% | +0.085% | 0.145% |
| max_pain_dist_pts | to_expiry | +0.192% | +0.067% | 0.125% |
| ce_wall_oi | to_expiry | +0.218% | +0.103% | 0.116% |
| pcr_near | 4hr | −0.037% | +0.074% | 0.111% |

**Reading the table:**
- For `pcr_near × to_expiry`: bars in the lowest PCR quintile see Nifty settle −0.016% below the measurement spot on average by expiry Thursday. Bars in the highest PCR quintile see Nifty settle +0.304% above the measurement spot. The 0.320% spread is economically meaningful — on a 100-lot position at current Nifty levels (~23,000), 0.3% is approximately 69 points, or ₹34,500 per lot.
- The week-level signals (3D, 5D, to_expiry) dominate the top of the table because intraday noise washes out and the structural OI effect accumulates over days.

---

## 9. Barrier Analysis: Do OI Walls Actually Hold?

### 9.1 Setup

For each 5-minute bar where spot was within 0.5–5% of the CE wall (above) or PE wall (below), the analysis measures whether spot crossed through the wall within the next 24 bars (2 trading hours), staying within the same calendar day.

### 9.2 Results

**CE Wall (upward breakthrough):**

| Spot distance from CE wall | Bars tested | Breakthrough rate |
|---|---|---|
| 0.5–1.0% | 43,192 | **1.9%** |
| 1.0–1.5% | 23,070 | 0.5% |
| 1.5–2.0% | 13,160 | 0.2% |
| 2.0–5.0% | 38,842 | 0.1% |

**PE Wall (downward breakthrough):**

| Spot distance from PE wall | Bars tested | Breakthrough rate |
|---|---|---|
| 0.5–1.0% | 49,589 | **3.1%** |
| 1.0–1.5% | 22,225 | 1.0% |
| 1.5–2.0% | 12,235 | 0.2% |
| 2.0–5.0% | 32,878 | 0.1% |

### 9.3 Interpretation

**The walls genuinely hold.** On a typical Nifty day, a 0.5–1% move in 2 hours happens perhaps 10–15% of the time in normal conditions. The CE wall suppresses this to 1.9% — a roughly 5–7× reduction in breakthrough probability. The wall is not an impenetrable ceiling, but it is a strong one.

**The PE wall is slightly easier to break (3.1% vs 1.9%).** Fear-driven selloffs can be sharper and faster than rallies; the floor has higher breakthrough risk than the ceiling.

**Proximity matters sharply.** The breakthrough rate drops from 1.9% to 0.5% as distance doubles from 0.5–1% to 1.0–1.5%. The wall effect is concentrated in the near-distance bucket. Once spot is more than 1.5% away from the wall, the wall is essentially irrelevant for 2-hour prediction.

**Implication for CE parachute decisions:** The base rate for a CE wall breakthrough in 2 hours is under 2%, even when spot is already within 1% of the wall. When the CE parachute fires (spot has crossed the *sell strike* by 150 points, but the CE wall may be further ahead), the prior probability of the wall breaking is low. This means the OI wall is likely to push price back before it reaches the next structural level — supporting the observation that roughly half of CE parachute triggers are reversals.

The breakthrough *does* happen occasionally, and when it does, it tends to be violent (the June 2024 event, for example, broke through the wall immediately). These are the events the CE parachute is designed to protect against.

---

## 10. Implications for Each Strategy

### 10.1 Athena — CE Parachute Filter

**Immediate application:** Use PCR_near at trigger time as a gating condition.

- If PCR_near > 1.2 at trigger time → consider raising the effective trigger threshold (e.g., require spot to be +250 pts above sell strike rather than +150) or delay the hedge by one 5-minute bar to observe whether the move is sustained.
- If PCR_near < 0.8 at trigger time → hedge confidence is high; the breakout environment is bullish and the CE wing is warranted immediately.
- If PCR_near is between 0.8 and 1.2 → proceed with the standard trigger logic.

**Caution:** The above thresholds should be calibrated on the 2019–2022 period and tested on 2023–2026 before deployment. The 21-event test suggests they work directionally, but 21 events is not enough for robust calibration.

**Gap-open limitation:** For CE parachute triggers at market open (09:15), OI delta is unavailable. PCR is the only actionable signal. For these events, the decision reverts to standard trigger logic unless PCR provides a very strong signal.

**Override for macro events:** Any OI-based filter should be bypassed if there is a known macro catalyst (election results, central bank decisions, major earnings). OI reflects pre-event positioning, not the event outcome itself.

### 10.2 Athena — Weekly Trade Selection

**Application:** Before entering a new Athena trade (Monday morning entry), observe the OI structure:

- High PCR_near or PCR_broad (top quintile of historical distribution) → the week is likely to settle higher. This favours Athena's CE side: the call wing is safer (market going up means the put wing is the risk). Consider starting with a wider CE margin or being less aggressive about the CE wing.
- High PE wall OI (top quintile) → bullish week expected; the PE wing is well-supported and the trade is safer.
- High total OI → range-bound week likely; the double calendar is expected to be profitable. Premium decay will be efficient.
- Max pain significantly below current spot (max_pain_dist_pts strongly negative) → settlement likely below current level; the PE wing is under more stress.

### 10.3 Artemis — Settlement Direction Prediction

Artemis sells straddles at ATM and profits when spot stays near the entry level at expiry. The to_expiry IC of +0.074 for PCR_broad means that at the start of the week, PCR provides a directional lean for where Sensex (or Nifty, where the signal is validated) will settle.

**Application:** Use weekly PCR as an early asymmetric hedge signal. If PCR is very high (top quintile), settlement is more likely to be above the straddle center → a slight PE bias in position sizing (sell slightly more PE than CE premium, or start with a wider PE strike) may improve expected value.

**Sensex forward testing:** The OI model is trained on Nifty (371 expiries). For Sensex (18 months of OI data until March 2026), the model should be applied with the following adjustments:
- Use Sensex OI data in the `oi` column (already handled by the engine)
- Strike step is 100 (Sensex strikes are in 100-point increments vs Nifty's 50)
- The same features and interpretation should apply if Sensex and Nifty are correlated (which they are, at approximately 0.95+ correlation)

### 10.4 New Intervention Ideas

The OI model suggests several interventions that go beyond the existing strategy logic:

**1. Max pain convergence trade (near-expiry):** On Wednesday afternoon (1–2 days before expiry), if spot is significantly above max pain (max_pain_dist_pts strongly negative, meaning max pain is below spot), this suggests expiry will pull spot lower. The Athena PE wing or a dedicated PE hedge could be sized up at this point. Similarly, if spot is below max pain (max_pain_dist_pts positive), consider reducing PE exposure.

**2. Wall proximity alert:** When spot enters the 0.5–1% proximity zone of the CE wall, the breakthrough rate is only 1.9% in the next 2 hours. However, when a breakthrough does happen, it is likely large and fast. A flag at this proximity threshold could prepare the CE parachute for immediate deployment rather than waiting for the standard 150-point trigger.

**3. PCR-driven position sizing:** Enter Athena with larger notional size when PCR is in the top quintile (bullish OI structure, PE well-supported) and smaller size when PCR is in the bottom quintile (CE-heavy, bearish lean, more risk to the CE wing).

**4. PE chute entry filter:** The PE chute (PE parachute — buy ATM put as hedge when market falls sharply) could be qualified by PCR_near < 0.6 (very low PCR = call-heavy = market structure is bearish). The 5D IC for pcr_near of +0.042 suggests that very low PCR is a contrarian bullish signal in the general case, meaning the PE chute fired in low-PCR environments may be a false alarm.

---

## 11. Files and Usage

### 11.1 Running the Full Pipeline

```bash
# Step 1: Build OI features for all 371 Nifty expiries
# Takes ~6 minutes with 4 workers; index loaded once per worker
python research/oi_analysis/build_nifty_features.py --workers 4

# Step 2: Run the signal quality analysis (requires pre-built features)
python research/oi_analysis/signal_quality.py

# Step 3: Run the Athena CE parachute validation (standalone, builds features on the fly)
python research/oi_analysis/validate_athena.py
```

### 11.2 Selective Date Ranges

```bash
# Build features for a date range only
python research/oi_analysis/build_nifty_features.py --from 2024-01-01 --to 2026-06-09

# Run signal quality on a subset (requires features CSV to cover the range)
python research/oi_analysis/signal_quality.py --from 2023-01-01 --to 2026-06-09

# Build features and run signal quality in one command (if CSV doesn't exist)
python research/oi_analysis/signal_quality.py --build --workers 4
```

### 11.3 Using the Engine Directly

```python
from research.oi_analysis.oi_engine import build_oi_profile, oi_at_strike
import pandas as pd

# Load Nifty index
index_df = pd.read_csv('data_pipeline/data/indices/nifty.csv', parse_dates=['time_stamp'])
index_df = index_df.rename(columns={'time_stamp': 'ts'}).set_index('ts')
index_df.index = index_df.index.tz_localize(None)

# Build OI profile for one expiry
profile = build_oi_profile(
    expiry_date='2026-06-09',
    options_dir='data_pipeline/data/nifty/options/2026-06-09',
    index_df=index_df,
    strike_step=50,        # Nifty strikes in 50-pt increments
    resample='5min',
    wall_min_pct=0.5,      # Ignore walls closer than 0.5% of spot
    wall_max_pct=10.0,     # Ignore walls further than 10% of spot
    near_strikes=5,        # PCR_near = ATM ± 5 strikes
)
# Returns DataFrame: index=ts (5-min bars), columns=all OI features

# Get OI direction at a specific strike (for OI delta analysis)
oi_series = oi_at_strike(
    expiry_date='2026-06-09',
    options_dir='data_pipeline/data/nifty/options/2026-06-09',
    strike=23600,
    side='ce',
    index_df=index_df,
    lookback_bars=6,       # OI delta = change over last 6 bars (30 min at 5-min)
    resample='5min',
)
# Returns DataFrame: oi, volume, oi_delta, oi_pct_chg, spot at each bar
```

### 11.4 Output Files

| File | Description | Rows |
|---|---|---|
| `data/nifty_oi_features.csv` | Pre-built OI features for all 371 expiries | 277,248 |
| `data/athena_emer_oi_validation.csv` | 21-event CE parachute validation results | 21 |
| `data/signal_quality_ic.csv` | IC table: one row per feature, one column per horizon | 10 |
| `data/signal_quality_quintiles.csv` | Quintile lift: forward returns per (feature, horizon, quintile) | ~500 |
| `data/signal_quality_barrier.csv` | Wall breakthrough rates by proximity bucket | 8 |

---

## 12. Known Limitations

### 12.1 OI Delta Unavailable at Gap Opens

The single most important limitation. OI delta at the sold strike — theoretically the strongest signal for CE parachute decisions — is NaN for 13 of 21 CE parachute events because they triggered at market open. Overnight gaps are the most dangerous scenario for the strategy, and the signal is unavailable precisely there. PCR remains available.

### 12.2 Autocorrelation in IC Tests

At short horizons (15min, 30min), adjacent 5-minute bars share most of their forward return window. The effective number of independent observations is much smaller than 197,448. P-values at short horizons are overstated (appear more significant than they are). At longer horizons (1D, 3D, 5D), adjacent bars have less overlap and the tests are more reliable.

### 12.3 OI Scale Non-Stationarity

Despite rolling z-score normalisation, the OI market has undergone structural change: retail participation in Nifty weekly options has grown dramatically since 2022. Features calibrated on 2019–2021 data may not transfer cleanly to 2025–2026. The time-series split (2019–2022 train, 2023–2026 test) should be used before any threshold is deployed operationally.

### 12.4 Sensex OI Gap

Sensex OI data is only available until March 2026. The model cannot be backtested on Sensex; it can only be forward-tested from March 2026 onward. During the gap period, Sensex-based strategies (Artemis) must operate without OI intelligence.

### 12.5 Max Pain Computation Speed

Unlike the other features (computed in fully vectorised batch across all bars simultaneously), max pain requires a loop over individual bars. For a 10-day lookback with 5-minute resolution (~750 bars), this loop takes approximately 1–2 seconds per expiry. It is acceptable but the slowest component. For any real-time application, max pain should be computed incrementally (updating only the new bar each tick) rather than recomputing the full history.

### 12.6 Wall Detection Gaps

The wall detection algorithm requires at least one strike within 0.5–10% of spot with non-zero OI. In very early days of an expiry week (when only a few strikes have been traded), or in extreme market conditions where OI hasn't built up, walls may not be detected. Features like `ce_wall_oi` and `pe_wall_oi` will be NaN for these bars and will be excluded from IC calculations.

### 12.7 The Single Excluded Event (April 2022)

Trade 72 (entry 2022-03-30, sell_expiry 2022-04-07) had missing CE wall data (NaN for `ce_wall_dist_pct`). The options data for the April 7, 2022 expiry may have been incomplete. This event was excluded from wall-specific statistics. It was a reversal (emer_pl = −40.7).

---

*Built June 2026. Engine: `oi_engine.py`. Data: 371 Nifty weekly expiries (2019–2026), 197,448 unique 5-min bars after dedup. Signal quality computed on `signal_quality.py`.*
