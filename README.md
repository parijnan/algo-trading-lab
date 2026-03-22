# Algo Trading Lab

A personal algorithmic trading laboratory for backtesting, optimising, and automating strategies for Indian Index Options.

## Strategies

### [Artemis](./artemis/) — Sensex Iron Condor
A market-neutral credit spread strategy trading a weekly Sensex Iron Condor, running Monday to Thursday. Sells OTM options on both sides and buys further OTM options as hedges. Deployed when India VIX < 16.

| | |
|---|---|
| Instrument | Sensex weekly options |
| Structure | Iron Condor (PE spread + CE spread) |
| Entry | Monday 10:30 AM |
| Expiry | Thursday |
| Broker | Angel Broking (SmartConnect) |
| Status | Live |

### [Apollo](./apollo_backtest/) — Nifty High-VIX Trend Following
A directional trend-following credit spread strategy deployed when India VIX > 16. Uses dual-timeframe Supertrend (75-min and 15-min) to identify and trade sustained directional moves in Nifty options.

| | |
|---|---|
| Instrument | Nifty weekly options |
| Structure | Credit spread (directional, one side only) |
| Signal | Dual Supertrend — 75-min regime, 15-min entry/exit |
| Deploy condition | India VIX > 16 |
| Broker | TBD |
| Status | In development |

## Infrastructure

| Component | Details |
|---|---|
| VPS | Linode Nanode — hostname `delos` |
| OS | Ubuntu 24.04 |
| Laptop | Garuda Linux (Arch-based) |
| Broker (live) | Angel Broking (SmartConnect API) |
| Broker (data) | ICICI Direct (Breeze) for Nifty, Angel Broking for Sensex |
| Notifications | Slack |
| Language | Python |

## Data Pipeline

Historical 1-minute OHLCV data for Nifty and Sensex options is maintained by an automated pipeline running on the VPS and laptop. See the pipeline notes for full details.

| Data | Source | Coverage |
|---|---|---|
| Sensex options | Angel Broking — VPS cron daily at 15:45 | Mid-2024 onwards |
| Nifty options | ICICI Breeze — laptop cron Tuesdays at 23:30 | May 2019 onwards |
| Nifty, Sensex, India VIX index | Angel Broking — VPS cron daily at 15:45 | Mid-2024 onwards |

## VIX Regime

| VIX Level | Strategy |
|---|---|
| < 14 | Artemis — full confidence |
| 14 – 16 | Artemis — reduced size, tighter SLs |
| > 16 | Apollo |

## Repository Structure

```
algo-trading-lab/
├── README.md
├── .gitignore
├── artemis/                    # Live iron condor strategy (TODO: add)
│   ├── artemis.py
│   ├── iron_condor.py
│   ├── credit_spread.py
│   ├── configs.py
│   └── functions.py
└── apollo_backtest/            # High-VIX trend following strategy
    ├── README.md
    ├── configs.py
    ├── technical_indicators.py
    ├── precompute.py
    ├── backtest.py             (TODO)
    └── analysis.py             (TODO)
```