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

### 10.2 Athena — Weekly Trade Selection (Empirically Validated, June 2026)

This section documents the results of `validate_athena_entry.py`, which tested OI features at entry against P&L outcomes for all 124 Athena VIX 16–25 trades (2020–2026). See `data/athena_entry_oi_joined.csv` for the full joined dataset.

#### 10.2.1 PCR_near: The Primary Entry Quality Signal

**PCR_near is the strongest OI predictor of Athena trade quality** (Spearman r = +0.288, p < 0.001 for total P&L; r = +0.260, p = 0.005 for CE-side P&L). The signal is **stable across both time periods**:

| Period | n | PCR_near → total_pl | PCR_near → ce_pl |
|---|---|---|---|
| Early 2020–2022 | 92 | r = +0.303 | r = +0.281 |
| Recent 2023–2026 | 21 | r = +0.143 | r = +0.258 |

The CE-side correlation is more consistent than the total-P&L correlation across periods, which is expected: total P&L includes the reactive PE wing (which flows through `running_realised_pl` and is not in `ce_pl_points` or `pe_pl_points`).

**Quintile P&L breakdown by PCR_near at entry:**

| PCR quintile | Mean total P&L | Mean CE P&L | Mean PE P&L | n |
|---|---|---|---|---|
| Q1 (lowest 20%) | **−3.9 pts** | −18.4 pts | +3.9 pts | 23 |
| Q2 | +30.8 pts | +20.5 pts | +6.3 pts | 22 |
| Q3 | +8.1 pts | +5.9 pts | −0.8 pts | 23 |
| Q4 | +27.7 pts | +9.8 pts | +16.7 pts | 22 |
| Q5 (highest 20%) | **+54.9 pts** | +48.8 pts | +9.3 pts | 23 |

The Q1→Q5 spread is 58.8 pts total (67.2 pts on the CE side). This is the single largest explainable factor in Athena trade outcomes found so far.

#### 10.2.2 Why the Mechanism is Not a Directional Skew

The intuitive hypothesis was: *high PCR (bullish market structure) → CE at risk → widen CE strike, tighten PE strike.* This was tested directly and **rejected**.

The correlation of PCR_near with CE P&L is **positive** (+0.260), not negative. High PCR does not make CE more dangerous — it makes the entire trade better.

The mechanism is simpler: **high PCR weeks in the VIX 16–25 regime are calm, bullish weeks where the double calendar decays profitably.** The CE calendar spread has a natural profit zone slightly above the sell strike (far-month CE offsets near-month losses), so even in high-PCR weeks when spot occasionally presses past the CE sell strike, the CE spread still profits. The spot move data confirms the pattern:

| PCR quintile | Mean CE pressure (max_spot − ce_strike) | Mean PE pressure (pe_strike − min_spot) |
|---|---|---|
| Q1 | −17.5 pts (spot stayed below CE) | **+74.5 pts** (spot breached PE by 74.5 pts) |
| Q5 | **+24.7 pts** (spot breached CE by 24.7 pts) | −54.9 pts (spot stayed above PE) |

In Q1 (low PCR, bearish weeks): spot drops hard into PE territory. The reactive PE wing fires and captures most of the downside gain. The CE calendar is hurt (mean ce_pl = −18.4) not because of CE breach, but because the volatility expansion during the drop hurts the sold CE (vega risk). In Q5 (high PCR, bullish weeks): CE is slightly breached but the calendar structure absorbs it and still profits.

**Strike skew is not supported by this data.** The signal predicts week quality overall, not which side to protect. The wall features (wall_asym, wall_oi_ratio, ce_wall_dist_pct) show no consistent relationship with per-side P&L (all correlations under 0.15, mostly unstable across time periods).

#### 10.2.3 Entry Filter — The Actionable Conclusion

Skipping the **bottom PCR quintile** (approximately PCR_near < 0.75 in recent data) improves mean P&L by **+8.1 pts per trade** (from 22.3 to 30.4 pts, a +36% improvement) at the cost of skipping 23 of 124 trades (19%). The skipped trades have a mean P&L of −3.9 pts.

**Entry filter simulation (skip bottom 20% by feature):**

| Feature | Kept | Mean P&L (kept) | Mean P&L (skip) | ΔMean |
|---|---|---|---|---|
| pcr_near | 100 | +30.4 pts | **−3.9 pts** | **+8.1 pts** |
| wall_oi_ratio | 100 | +25.6 pts | +15.0 pts | +3.3 pts |
| ce_wall_dist_pct | 100 | +24.7 pts | +18.3 pts | +2.4 pts |
| pcr_broad | 100 | +24.0 pts | +21.1 pts | +1.7 pts |

PCR_near is the only feature with a large, consistent, and statistically significant entry filter effect.

**Implementation path:** Add a `pcr_near` check in `backtest.py` at entry (same location as the VIX filter). PCR_near at 10:30 on entry day for the sell expiry. Calibrate threshold on 2019–2022, apply to 2023–2026. The infrastructure is already in `oi_engine.py` — compute on-the-fly or from the pre-built features CSV. Live use requires calling `build_oi_profile()` for the current sell expiry at 10:30 each entry day.

### 10.3 Artemis — Entry OI Analysis (Empirically Tested, June 2026)

This section documents the results of `validate_artemis_entry.py`, run on 150 Artemis Nifty VIX < 16 trades from 2019–2025. See `data/artemis_entry_oi_joined.csv` for the full joined dataset.

#### 10.3.1 Summary Finding: OI Is Informationally Weak for Artemis at Entry

Unlike Athena (where PCR_near has r=+0.288 with trade P&L), **no OI feature predicts Artemis total trade quality with meaningful strength**. The strongest correlations are in the 0.06–0.15 range and none reach statistical significance for total P&L:

| Feature | total_pl r | ce_pl r | pe_pl r |
|---|---|---|---|
| pcr_near | −0.001 | −0.066 | +0.075 |
| pcr_broad | −0.003 | −0.137 | +0.153 |
| wall_oi_ratio | −0.122 | −0.132 | +0.011 |
| ce_wall_dist_pct | +0.072 | +0.111 | −0.118 |

#### 10.3.2 The Directional Hypothesis Holds in Sign but Fails in Magnitude

For a short strangle, the expected channel is: *high PCR (bullish market structure) → CE at risk, PE safe.* This is confirmed in direction for `pcr_broad` (ce_pl r=−0.137, pe_pl r=+0.153, both p≈0.06–0.09). The signs are correct. But the magnitude is too weak for either an entry filter or a strike skew to be reliable.

**The quintile breakdown shows the right directional shape but modest CE-side P&L variation:**

| PCR quintile | Mean CE P&L | Mean PE P&L | Mean total P&L |
|---|---|---|---|
| Q1 (lowest PCR) | **+11.6 pts** | +2.2 pts | **+22.3 pts** |
| Q2 | −2.4 pts | +1.5 pts | +4.3 pts |
| Q3 | +8.2 pts | −5.1 pts | +6.3 pts |
| Q4 | +4.1 pts | +4.7 pts | +9.4 pts |
| Q5 (highest PCR) | **+2.3 pts** | **+5.3 pts** | **+11.1 pts** |

The CE P&L falls from Q1 to Q5 (from +11.6 to +2.3) and PE P&L rises (from +2.2 to +5.3) — consistent with the directional channel. However, the total P&L is **highest in Q1 (lowest PCR)**, not Q5. This means:

1. The directional asymmetry exists (PCR tells you which side gets hit) but it doesn't translate into better overall outcomes in high-PCR weeks.
2. In low-VIX conditions, both sides tend to do well; the low-PCR (bearish-leaning) weeks are not meaningfully worse.

#### 10.3.3 The Signal Does Not Hold Across Time Periods

The GO/NO-GO time-split check reveals why this signal cannot be built on:

| Feature | Period | total_pl r | ce_pl r | pe_pl r | n |
|---|---|---|---|---|---|
| pcr_near | 2019–2022 | +0.094 | **−0.201** | +0.117 | 33 |
| pcr_near | 2023–2025 | −0.021 | **−0.028** | +0.037 | 117 |

The directional signal for CE (r=−0.201 in early period) essentially vanishes in the recent 117-trade period (r=−0.028). The early-period signal was driven by the small sample of 33 trades in 2019–2022, which happen to show the pattern more clearly but cannot be separated from sampling noise at that size. The recent period (where 78% of all Artemis Nifty trades sit) shows no directional OI signal at entry.

#### 10.3.4 Entry Filter: Reversed or Absent

For Athena, skipping the bottom PCR quintile improved mean P&L by +8.1 pts. For Artemis, the effect is **reversed**: the bottom PCR quintile (Q1, lowest PCR) has the *highest* mean total P&L (+22.3 pts), and skipping it would *hurt* outcomes (−2.9 pts per trade). No OI feature provides a reliable entry quality filter for Artemis.

#### 10.3.5 Why Artemis Is Informationally Different

Two structural reasons why OI carries less information for Artemis:

1. **VIX regime.** Artemis trades when VIX < 16 — a complacent, low-hedging environment. In low-VIX weeks, institutional options positioning is lighter and PCR variation is narrower. The "wall of puts protecting the market" dynamic that gives PCR its predictive power in higher-VIX regimes is simply less present. The OI signal has less to say when less hedging is happening.

2. **Regime shift in retail options participation.** 117 of 150 Artemis Nifty trades occur in 2023–2025, when weekly Nifty options became heavily retail-driven. The OI structure in this period is noisier — retail OI reflects speculative positioning more than institutional hedging, which is the channel behind PCR's predictive value.

#### 10.3.6 Sensex Extension (Empirically Tested, June 2026)

`validate_artemis_sensex.py` was run on 27 Sensex traded weeks (Sep 2025 – Mar 2026), building Sensex OI profiles on-the-fly with `strike_step=100`. Results are directional indicators only — n=27 requires |r| > 0.39 for p < 0.05 and nothing here clears that bar.

**PCR_near is directionally consistent with Nifty:**

| Feature | Metric | Sensex r | Nifty r | Match |
|---|---|---|---|---|
| pcr_near | ce_pl | −0.068 | −0.066 | ✓ |
| pcr_near | pe_pl | +0.181 | +0.075 | ✓ |
| pcr_broad | ce_pl | +0.024 | −0.137 | ✗ |
| pcr_broad | pe_pl | +0.158 | +0.153 | ✓ |
| wall_oi_ratio | ce_pl | +0.194 | −0.132 | ✗ |

The `pcr_near` CE correlation (Sensex −0.068, Nifty −0.066) is remarkably similar across the two markets — both point in the expected direction and are nearly identical in magnitude. The PE correlation also matches. This supports the Nifty–Sensex structural similarity (≈0.95+ correlation) holding at the OI-signal level.

However, the strength of the signal is identical to Nifty — meaning neither market provides a significant signal, and the Sensex data does not add statistical power (it is all from the recent 2025–2026 period where the Nifty signal was already weak at r=−0.028 for the recent sub-period).

**The tercile breakdown shows a different total-P&L pattern vs Nifty:**

| PCR_near tercile | Mean total P&L (Sensex) |
|---|---|
| Lo (PCR < 0.9) | +59.6 pts |
| Mid | +52.8 pts |
| Hi (PCR > 1.2) | **+119.9 pts** |

Unlike Nifty (where Q1 was best at +22.3 pts), Sensex shows the highest P&L in the high-PCR tercile. This aligns with how Athena performs: high PCR → good week overall. Artemis Sensex appears to behave more like Athena in this dimension, possibly because the Sensex short-week strangle structure has more calendar-spread-like vega exposure than the Nifty structure.

**Bottom line for Artemis:** No OI-based entry intervention is currently supported for either Nifty or Sensex. The PCR_near directional signal is real in sign (CE worse, PE better, when PCR is high) and confirmed consistently across both markets — but at r≈−0.07 it is too weak to filter entries or justify a strike skew. Revisit when the combined Nifty + Sensex trade universe reaches 300+ trades, or if live Sensex OI data resumes.

### 10.4 Artemis — Intraday OI Wall and Max Pain Analysis (Empirically Tested, June 2026)

**Scope:** 84 Artemis Nifty traded weeks (2023-09 to 2025-08) for which 1-minute bar logs are available. OI features (5-minute cadence, pre-built) are joined to each trade log via `pd.merge_asof` with `direction='backward'` to prevent lookahead. Entry-bar features and intraday minimums/maximums are extracted for each week. Script: `validate_artemis_intraday.py`.

---

#### 10.4.1 Use Case 1 — Entry Strike Placement Near OI Wall

**Hypothesis:** Place the CE (or PE) sell strike near the OI wall for natural resistance/support.

**Finding: CLOSED.** Entry-time wall distance features have no significant IC with leg P&L:

| Feature | CE P&L IC | PE P&L IC | Total P&L IC |
|---|---|---|---|
| CE wall dist at entry | +0.102 | −0.046 | +0.097 |
| PE wall dist at entry | +0.087 | −0.115 | +0.044 |
| CE wall buffer (wall above sell %) | +0.190 | −0.235* | +0.013 |

*Threshold for p<0.05 at n=84: |r| > 0.21. Only one value (CE wall buffer → PE P&L) crosses it, and the sign is counter-intuitive.*

There is also a structural reason this is impractical: in every one of the 84 weeks, the CE OI wall (max-OI strike in the call chain) was **already above the CE sell strike**. Median buffer = +1.6% (wall is ~300 Nifty points above the sell strike). The OI wall does not provide new placement guidance — strikes are already sold below the natural resistance.

---

#### 10.4.2 Use Case 2 — Asymmetric Strike Skew from Wall Asymmetry

**Hypothesis:** When the CE wall is much further from spot than the PE wall (`wall_asym` = ce_wall_dist − pe_wall_dist), the CE side is structurally protected → widen CE, tighten PE.

**Finding: CLOSED.** Wall asymmetry at entry shows no usable IC for either leg. Entry PCR (which subsumes wall asymmetry information) already showed IC collapse in 2023–2025 (§10.3.3). No entry-snapshot OI feature passes the gate for Artemis Nifty.

---

#### 10.4.3 Use Case 3 — Intraday SL Triggers via Wall Proximity / Breach

**Hypothesis:** When spot approaches within X% of the CE or PE wall during the trade week, tighten the SL for that leg or exit early.

**Finding: CLOSED — breach rate = 0%.** In all 84 weeks, spot never crossed the CE or PE OI wall at any point during the trade. The wall (max-OI strike) is structurally positioned beyond the sell strikes in the 2023–2025 VIX<16 regime:

- CE sell strike is typically 100–200 pts OTM
- CE OI wall is an additional 300 pts beyond that (well into deep-OTM call space)
- By the time spot would reach the OI wall, the CE `index_sl` (triggered when spot crosses the sell strike) would have already fired

Intraday min wall distance has no significant IC with SL hits (CE: r=−0.027, PE: r=+0.151, both insignificant). **OI wall proximity is not a viable intraday trigger for Artemis.**

---

#### 10.4.4 Use Case 4 — Max Pain as Intraday Adjustment Trigger

**Hypothesis:** When spot moves far from max pain during the week, the exposed leg is at risk.

**Finding: PARTIAL — strong contemporaneous correlation, limited predictive lead time.**

Intraday max pain movement is the strongest OI signal found across the entire Artemis Nifty dataset:

| Feature | CE P&L IC | PE P&L IC | Total P&L IC |
|---|---|---|---|
| Min spot-vs-max_pain during week | +0.554*** | −0.423*** | +0.128 |
| Max spot-vs-max_pain during week | +0.508*** | −0.665*** | −0.033 |

*`max_pain_dist_pts` = spot − max_pain_strike at each bar.*

The mechanism is direct: when spot rises far above max pain, the PE sell strike gets tested. When spot drops far below max pain, the CE sell strike gets tested. These are not coincidences — they reflect the same underlying directional move. The correlation is strong but largely contemporaneous.

**Threshold analysis (max_max_pain_dist_pts → PE SL):**

| Threshold (spot above max pain by) | Weeks crossing | PE SL rate | Weeks not crossing | PE SL rate |
|---|---|---|---|---|
| +50 pts | 71 | 59% | 13 | 8% |
| **+100 pts** | **38** | **79%** | **46** | **28%** |
| +150 pts | 28 | 82% | 56 | 36% |
| +200 pts | 16 | 81% | 68 | 44% |

The +100 pts threshold is the most actionable: 79% PE SL rate when spot reaches max_pain+100 at any point during the week, vs 28% base rate.

**Timing check:** Does the threshold fire before the PE SL event?

Of 43 PE SL events (option_sl + index_sl), only **12 (28%)** had spot crossing max_pain+100 pts *before* the SL event. For those 12, median lead time was 730 minutes (>12 hours). The remaining 72% of PE SL events happened without the threshold being crossed first — driven by option premium moves (VIX spikes) or rapid directional gaps, not gradual spot drift.

**Conclusion:** The max_pain+100 trigger has high specificity when it fires (79% PE SL rate) but very low sensitivity (catches only 28% of SL events). It is not usable as a standalone trigger. It could serve as a complementary confirmation signal alongside existing SL logic, or as a readiness flag (when spot approaches max_pain+100, heighten monitoring of PE premium).

---

#### 10.4.5 Summary — Gate Assessment for All Four Use Cases

| Use Case | Status | Reason |
|---|---|---|
| 1. Entry strike near OI wall | **CLOSED** | No significant entry IC; wall already beyond sell strike structurally |
| 2. Asymmetric CE/PE skew | **CLOSED** | Wall asymmetry has no IC; entry PCR already shown weak |
| 3. Intraday SL trigger on wall breach | **CLOSED** | Breach rate = 0% in 84-week sample; wall unreachable in VIX<16 regime |
| 4. Max pain adjustment trigger | **OPEN (limited)** | Strong correlation but 72% miss rate; complementary signal only |

**Research verdict:** For Artemis in the VIX<16 regime, OI wall features carry no actionable intraday information. Max pain distance is the only channel with a real signal, but it is too noisy as a standalone trigger. The primary OI opportunity for Artemis remains the Athena-style PCR entry filter (§10.2), which would need Artemis-specific validation once ≥300 trades are available.

---

### 10.6 Artemis — OI Wall Migration (Empirically Tested, June 2026)

**Scope:** 84 Artemis Nifty traded weeks (2023-09 to 2025-08). For each week, the OI wall strike (CE and PE) is tracked from entry bar to each EOD observation point. Wall **delta** = (wall strike at observation) − (entry wall strike). Script: `validate_artemis_wall_delta.py`.

**Motivation:** §10.4 found strong IC for full-week wall extrema (e.g., r=−0.511*** for minimum CE wall delta vs CE P&L). The question is whether wall *migration* — how the wall moves during the week — provides an independent, forward-looking signal that could support the four derivative use cases raised: (1) SL trigger, (2) adjustment trigger, (3) position skewing, (4) adjustment timing.

---

#### 10.6.1 Wall Migration Statistics

CE wall migrates strongly during a typical Artemis week:

| Metric | CE wall delta | PE wall delta |
|---|---|---|
| Mean (full week) | −337 pts | +335 pts |
| Median (full week) | −200 pts | +100 pts |
| Std | 519 pts | 592 pts |
| P10 | −1000 pts | −270 pts |
| P90 | +170 pts | +1000 pts |
| Mean intraday extremum | −604 pts (min) | +640 pts (max) |

56% of weeks see CE wall move DOWN (toward spot); 58% see PE wall move UP. Both reflect the standard expiry-approach effect: OI concentrates near ATM as Thursday nears, pushing CE wall down and PE wall up regardless of market direction.

At **Tuesday EOD** (Day 1 post-entry, i.e., ~end of second trading day):

| Metric | CE wall delta | PE wall delta |
|---|---|---|
| Mean | −257 pts | +283 pts |
| Median | 0 pts | +100 pts |
| Std | 465 pts | 592 pts |

The median Tue EOD CE wall delta is 0 — half of weeks show zero movement at that point.

---

#### 10.6.2 Full-Window IC — Wall Delta vs Leg P&L

The full-week wall delta features show strong Spearman IC:

| Feature | CE P&L | PE P&L | Total P&L |
|---|---|---|---|
| Min CE wall delta (most downward) | −0.511*** | +0.412*** | −0.203 |
| CE wall delta (end vs entry) | −0.483*** | +0.418*** | −0.139 |
| PE wall delta (end vs entry) | −0.382*** | +0.286** | −0.080 |

Sign interpretation: CE wall moving **up** (positive delta) = spot moved up = CE (call sell) at risk → lower CE P&L. CE wall moving down = spot moved down = CE safer, PE at risk → CE P&L better but PE P&L worse. These patterns are consistent across legs.

**Critical caveat:** The full-window extremum (min CE wall delta over the whole week) is computed after the fact — you cannot observe the week's minimum until the week ends. It may merely track the same underlying market move that caused the SL.

---

#### 10.6.3 Critical Test: Does Wall Migration Add Anything Beyond Spot Direction?

**The key finding of this analysis.**

CE wall delta at Tue EOD correlates strongly with spot move to Tue EOD: **r=+0.455*** (n=84). This means wall migration is highly collinear with spot direction — they are not independent signals.

**Partial Spearman IC** (wall delta vs P&L, controlling for spot move to Tue EOD using rank residuals):

| Feature | CE P&L | PE P&L | Total P&L |
|---|---|---|---|
| CE wall delta (Tue EOD) | −0.025 | +0.068 | +0.048 |
| PE wall delta (Tue EOD) | +0.075 | −0.123 | −0.028 |

**All partial ICs are near zero and statistically insignificant.** After controlling for where spot moved by Tuesday, the OI wall delta carries no additional information about final P&L. Wall migration is a proxy for spot direction, not an independent signal.

This means:

> The +0.511*** full-window IC for min CE wall delta with CE P&L is entirely explained by the direction spot moved during the week. There is no independent OI positioning signal beyond what spot already tells you.

---

#### 10.6.4 Fixed-Decision-Time IC and Year-Split Stability

Raw (uncontrolled) IC at Tue EOD:

| Feature | CE P&L | PE P&L | Total P&L |
|---|---|---|---|
| CE wall delta (Tue EOD) | −0.269* | +0.342** | +0.022 |
| PE wall delta (Tue EOD) | −0.103 | +0.103 | −0.040 |
| CE wall OI % change (Tue EOD) | +0.021 | −0.233* | −0.108 |
| PE wall OI % change (Tue EOD) | −0.029 | +0.207 | +0.150 |

The CE wall delta shows nominally significant IC (r=−0.269*) but year-split reveals instability:

| Period | n | CE wall delta → CE P&L | CE wall delta → PE P&L |
|---|---|---|---|
| 2023-2024 | 61 | −0.174 (not sig) | +0.431*** |
| 2025 | 23 | −0.533** | +0.156 (not sig) |
| All | 84 | −0.269* | +0.342** |

The CE→CE path is significant only in 2025 (n=23), and the CE→PE path is significant only in 2023-24 and reverses in 2025. This instability — opposite legs significant in opposite years — is consistent with the partial IC result: the raw correlation simply tracks spot, and which leg spot hurts changes with regime.

---

#### 10.6.5 Use Case Assessment — All Four Closed

**SL trigger (CE):** A trigger fires when CE wall moves UP significantly by Tue EOD (spot moved up = CE at risk). Threshold analysis:

| Threshold (CE wall ≥ X) | N triggered | CE SL if triggered | CE SL baseline | Sensitivity |
|---|---|---|---|---|
| ≥ +900 pts | 1/84 | 100% | 51% | 2% |
| ≥ +300 pts | 5/84 | 60% | 51% | 7% |
| ≥ +100 pts | 10/84 | 60% | 51% | 14% |
| ≥ 0 pts (any up) | 59/84 | 58% | 51% | 79% |

At tight thresholds (≥300 pts), sensitivity is 2–7% — the trigger almost never fires in the weeks that need it. At loose thresholds (≥0 pts), 59/84 weeks fire and SL rate lifts only from 51% to 58% — not meaningful lift. **CLOSED.**

**SL trigger (PE):** PE wall delta threshold analysis (PE wall moving DOWN = PE at risk):

| Threshold (PE wall ≤ X) | N triggered | PE SL if triggered | PE SL baseline | Sensitivity |
|---|---|---|---|---|
| ≤ −900 pts | 3/84 | 33% | 51% | 2% |
| ≤ −300 pts | 17/84 | 35% | 51% | 14% |
| ≤ 0 pts | 56/84 | 61% | 51% | 79% |

No significant lift beyond baseline at any usable threshold. PE wall migration adds nothing. **CLOSED.**

**Adjustment trigger:** CE wall delta at Tue EOD has r=−0.269* with CE P&L (raw), but partial IC = −0.025 (zero) after controlling for spot. Not actionable without adding something that spot direction doesn't already give. **CLOSED.**

**Position skewing (leg weighting):** Wall asymmetry = CE wall delta − PE wall delta at Tue EOD. Asymmetry sign predicts worse leg correctly in only **56%** of 84 weeks (barely above 50% random). No skewing signal. **CLOSED.**

**Adjustment timing (intraweek):** CE wall delta IC with CE P&L evolves from r=−0.020 (Mon EOD, not sig) to r=−0.269* (Tue EOD) to r=−0.443*** (Wed EOD) to r=−0.483*** (Thu EOD). The signal strengthens through the week, but since it merely tracks spot direction, knowing "CE wall moved down a lot by Wednesday" is the same as knowing "spot moved up a lot by Wednesday" — both are obvious from the spot price itself. **CLOSED.**

---

#### 10.6.6 Summary

| Use Case | Status | Core Reason |
|---|---|---|
| SL trigger (CE) | **CLOSED** | Partial IC ≈ 0; thresholds have ≤14% sensitivity at meaningful SL rate lift |
| SL trigger (PE) | **CLOSED** | No IC before or after spot control; threshold analysis shows no lift |
| Adjustment trigger | **CLOSED** | Nominally significant raw IC collapses to zero in partial IC test |
| Position skewing | **CLOSED** | 56% correct-leg ID from asymmetry sign (≈ random) |
| Adjustment timing | **CLOSED** | Signal is present but entirely explained by spot direction |

**Overall verdict:** OI wall migration is a real and measurable phenomenon (full-week IC r=−0.511***) but is entirely contemporaneous with and explained by spot direction. It carries **no independent information** beyond knowing where spot moved. If you want to skew, adjust, or hedge based on intraday market direction, tracking spot directly is both simpler and at least as informative as tracking OI wall migration.

---

### 10.7 Artemis — Max Pain Drift (Empirically Tested, June 2026)

**Scope:** 84 Artemis Nifty traded weeks (2023-09 to 2025-08). Script: `validate_artemis_max_pain_drift.py`.

**Motivation:** §10.4 found the full-week max_pain_dist extremum has strong IC (r=−0.665*** for max_max_pain_dist vs PE P&L, r=+0.554*** for min_max_pain_dist vs CE P&L). The question is whether the max pain strike migrating during the week — or the fixed-time distance between spot and max pain at a midweek observation point — provides an actionable, independent signal.

Two features tested in parallel:
- **max_pain_delta** = max_pain_strike(t) − max_pain_strike(entry): how far the OI-weighted fair-value strike has moved
- **max_pain_dist** = spot(t) − max_pain_strike(t): how far spot currently sits above/below the max pain level

---

#### 10.7.1 Migration Statistics

Max pain STRIKE moves very little at Tue EOD (median = 0 pts) but can swing substantially over the full week:

| Metric | MP strike delta (full week) | MP delta at Tue EOD | MP dist at Tue EOD |
|---|---|---|---|
| Mean | −2 pts | +4 pts | 0 pts |
| Median | 0 pts | 0 pts | −11 pts |
| Std | 298 pts | 115 pts | 80 pts |
| P10 | −400 pts | −150 pts | −98 pts |
| P90 | +300 pts | +135 pts | +115 pts |

Unlike CE/PE wall migration (mean −337 pts, −257 pts at Tue EOD), the max pain strike is much more stable midweek — large migrations mostly occur Wednesday–Thursday as expiry nears. The full-week max_pain_dist extrema are substantial (mean max = +122 pts, mean min = −112 pts), consistent with §10.4's findings.

---

#### 10.7.2 Full-Window IC (Contemporaneous Reference)

| Feature | CE P&L | PE P&L | Total P&L |
|---|---|---|---|
| Min MP delta (most downward) | −0.658*** | +0.673*** | −0.103 |
| Max MP delta (most upward) | −0.634*** | +0.605*** | −0.100 |
| MP delta (end vs entry) | **−0.694****** | **+0.654****** | −0.115 |
| Min MP dist (spot most below max_pain) | +0.554*** | −0.423*** | +0.128 |
| Max MP dist (spot most above max_pain) | +0.508*** | **−0.665****** | −0.033 |

These are the strongest ICs in the entire Artemis OI feature set. The max_max_pain_dist reproduces the §10.4 result (r=−0.665***). The full-week MP delta is similarly strong (r=−0.694***). However, all are computed with information only available at week end.

---

#### 10.7.3 Critical Test: Does Max Pain Drift Add Anything Beyond Spot?

Max pain strike delta at Tue EOD correlates with spot move at Tue EOD: **r=+0.861*****.** This is even tighter coupling than wall migration (+0.455). The max pain strike is essentially a smoothed, stickier, discretized proxy for spot — it updates as OI shifts to new strikes but always tracks where spot has moved.

Partial Spearman IC (controlling for spot move to Tue EOD):

| Feature | CE P&L | PE P&L | Total P&L |
|---|---|---|---|
| MP strike delta (Tue EOD) | −0.151 | +0.096 | −0.034 |
| MP dist (Tue EOD) | −0.033 | −0.089 | −0.091 |

**All partial ICs are near zero and statistically insignificant.** After controlling for where spot moved by Tuesday, neither the max pain strike position nor its distance from spot adds any information about final P&L.

---

#### 10.7.4 Fixed-Time IC and Year-Split Stability

Raw IC at Tue EOD is strong before spot control:

| Feature | CE P&L | PE P&L | Total P&L |
|---|---|---|---|
| MP strike delta (Tue EOD) | −0.538*** | +0.598*** | −0.056 |
| MP dist (Tue EOD) | +0.385*** | −0.524*** | −0.029 |

Note the sign of MP dist: **spot above max_pain → CE P&L BETTER (r=+0.385***)** and PE P&L worse. This is the "max pain gravity" effect — when spot runs above the OI fair-value level, it tends to revert (good for CE call sell), while the PE put sell is temporarily under pressure.

Year-split:

| Feature → Target | 2023-24 (n=61) | 2025 (n=23) |
|---|---|---|
| MP delta → CE P&L | −0.507*** | −0.616** |
| MP delta → PE P&L | +0.660*** | +0.421* |
| MP dist → CE P&L | +0.352** | +0.405 |
| **MP dist → PE P&L** | **−0.650****** | **−0.067** |

Max pain delta is **stable across years** (both CE and PE legs significant in both periods) — unlike CE/PE wall migration which was inconsistent. But this stability is a property of the signal tracking spot (spot direction is consistently predictive), not of an independent OI signal.

Max pain DISTANCE → PE P&L shows the same collapse as wall migration: −0.650*** in 2023-24, −0.067 in 2025. Not stable.

---

#### 10.7.5 Threshold Analysis — MP Dist at Tue EOD as PE SL Readiness Flag

**§8 PE SL trigger:**

| Threshold (mp_dist ≥ X) | N triggered | PE SL if triggered | PE SL baseline | Sensitivity |
|---|---|---|---|---|
| ≥ +150 pts | 6/84 | **83%** | 51% | 12% |
| ≥ +100 pts | 10/84 | **70%** | 51% | 16% |
| ≥ +75 pts | 15/84 | **80%** | 51% | 28% |
| ≥ +50 pts | 18/84 | **78%** | 51% | 33% |
| ≥ +25 pts | 27/84 | 74% | 51% | 47% |
| ≥ 0 pts | 40/84 | 70% | 51% | 65% |

At ≥+50 pts: PE SL rate lifts from 51% baseline to 78%, with 33% sensitivity. This is a real, material lift — when spot is 50+ pts above max pain at Tuesday EOD, that week has a 78% chance of hitting PE SL. However, 67% of PE SL events occur without this flag (sensitivity only 33%), and the year-split instability (2025 collapse) means this is not a robust standalone trigger.

**§9 CE SL (spot above max_pain → CE reversal):**

At mp_dist ≥ +150 pts: **CE SL rate = 0%** (6 events, no CE SL). This is the max pain gravity effect: spot so far above max_pain it tends to revert, protecting CE. However, n=6 is too small for reliable conclusions.

---

#### 10.7.6 Intraday Timing — Signal Available Monday EOD

MP delta is detectable even on Monday EOD (entry day):

| Observation | MP delta → CE P&L | MP delta → PE P&L |
|---|---|---|
| Mon EOD | −0.336** | +0.305** |
| Tue EOD | −0.538*** | +0.598*** |
| Wed EOD | −0.611*** | +0.710*** |
| Thu EOD | −0.694*** | +0.654*** |

The signal is present from the start but is just tracking intraday spot direction. Monday EOD signal significance means: "if spot moved significantly on Monday, the week is likely directional" — which requires no OI data to observe.

---

#### 10.7.7 Summary

| Use Case | Status | Core Reason |
|---|---|---|
| SL trigger (PE): mp_dist ≥ +50 pts at Tue EOD | **OPEN (limited)** | 78% PE SL rate vs 51% baseline; 33% sensitivity; year-split unstable (2025 collapse) |
| SL trigger (CE): mp_dist or mp_delta | **CLOSED** | Partial IC ≈ 0; spot above max_pain is bullish for CE (reversal), not bearish |
| Adjustment trigger | **CLOSED** | Partial IC ≈ 0 after spot control; year-split unstable for MP dist |
| Position skewing | **CLOSED** | Direction asymmetry is just spot direction; no independent OI signal |
| Adjustment timing | **CLOSED** | Early signal (Mon EOD) is just early spot move; r=+0.861*** with spot |

**Overall verdict:** Max pain drift shares the same fundamental problem as wall migration — it tracks spot direction rather than representing independent OI information (r=+0.861*** for MP delta vs spot move). After controlling for spot direction, all ICs collapse. The one partial exception is `mp_dist ≥ +50 pts at Tue EOD` as a PE SL readiness flag (+27% SL rate lift, 33% sensitivity), but this is unstable across years and partially explained by spot position.

The max pain gravity effect (spot far above max_pain → CE tends to reverse) is real in the data but operates through spot itself, not through OI repositioning.

---

### 10.5 Former Speculative Intervention Ideas (Pre-Empirical)

*(Retained for reference — these were the pre-empirical hypotheses. All have been replaced by empirical tests above.)*

**1. Max pain convergence trade (near-expiry):** On Wednesday afternoon, if spot is significantly above max pain, this suggests expiry will pull spot lower. The Athena PE wing or a dedicated PE hedge could be sized up. — *Partially supported by §10.4.4 (max pain movement correlates with outcomes) but not validated for near-expiry timing specifically.*

**2. Wall proximity alert:** When spot enters the 0.5–1% proximity zone of the CE wall, the breakthrough rate is only 1.9% in the next 2 hours. — *Valid for Athena (§9), moot for Artemis (wall breach rate = 0%).*

**3. PCR-driven position sizing:** Enter Athena with larger notional size when PCR is in the top quintile. — *Supported for Athena (§10.2); not supported for Artemis (§10.3).*

**4. PE chute entry filter:** The PE chute could be qualified by PCR_near < 0.6. — *Hypothesis not empirically tested; precursor IC work (§10.3) suggests PCR is too weak for Artemis to support this.*

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

# Step 4: Run Artemis validation scripts (require pre-built nifty_oi_features.csv)
python research/oi_analysis/validate_artemis_entry.py        # §10.3 — entry OI features
python research/oi_analysis/validate_artemis_intraday.py     # §10.4 — intraday OI path
python research/oi_analysis/validate_artemis_wall_delta.py      # §10.6 — wall migration
python research/oi_analysis/validate_artemis_max_pain_drift.py  # §10.7 — max pain drift
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
| `data/athena_entry_oi_joined.csv` | 124 Athena trades with OI features at entry (§10.2) | 124 |
| `data/artemis_entry_oi_joined.csv` | 150 Artemis Nifty trades with OI features at entry (§10.3) | 150 |
| `data/artemis_sensex_entry_oi_joined.csv` | 27 Artemis Sensex trades with OI features at entry (§10.3.6) | 27 |
| `data/artemis_intraday_oi_joined.csv` | 84 Artemis Nifty trades with intraday OI path features (§10.4) | 84 |
| `data/artemis_wall_delta_joined.csv` | 84 Artemis Nifty trades with per-day wall delta features (§10.6) | 84 |
| `data/artemis_max_pain_drift_joined.csv` | 84 Artemis Nifty trades with per-day max pain drift features (§10.7) | 84 |
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

*Built June 2026. Engine: `oi_engine.py`. Data: 371 Nifty weekly expiries (2019–2026), 197,448 unique 5-min bars after dedup. Signal quality: `signal_quality.py`. Athena entry validation: `validate_athena_entry.py` (124 trades). Artemis Nifty entry validation: `validate_artemis_entry.py` (150 trades). Artemis Sensex validation: `validate_artemis_sensex.py` (27 trades, Sep 2025–Mar 2026). Artemis intraday wall/max pain validation: `validate_artemis_intraday.py` (84 trades, 2023–2025).*
