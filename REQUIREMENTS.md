# Algo Trading Lab: Requirements & Dependencies

This document lists the third-party libraries and system-level dependencies required to run the laboratory on both local (Backtesting) and VPS (Production) environments.

## Python Dependencies

### Core Strategy & Execution
- **SmartApi (v1.3.3+):** Official Python SDK for Angel One (Angel Broking) SmartConnect API.
- **breeze-connect:** Official Python SDK for ICICI Direct Breeze API (primarily for Nifty historical data).
- **pandas:** Data manipulation and CSV-based state persistence.
- **numpy:** Mathematical operations and indicator calculations.
- **pyotp:** Generation of TOTP tokens for automated broker login.
- **requests:** HTTP client for REST API calls and Slack notifications.
- **websocket-client:** Used for streaming real-time LTP/Orderbook data (Apollo).

### Quantitative Research
- **mibian:** Option pricing model for calculating Greeks (Delta, Theta, Vega).
- **technical_indicators (custom/local):** Implementation of Supertrend, EMA, and RSI.

### Slack Interaction & Monitoring
- **slack-bolt:** Framework for building interactive Slack apps (Circuit Breakers/Modals).
- **slack-sdk:** Underlying SDK for sending alerts and updates.

### Data Pipeline
- **selenium:** Web automation for scraping/downloading scrip masters from exchange sites.
- **webdriver-manager:** Automated management of ChromeDriver/GeckoDriver for Selenium.

## System Dependencies

### Production (Ubuntu 24.04 VPS)
- **Python 3.10+** (Anaconda distribution recommended).
- **systemd:** Used to manage the `slack_listener.service` daemon.
- **git:** For source control and VPS-Local synchronization.
- **cron:** Orchestrates the daily 09:15 AM launch via `leto.py`.

### Development (Garuda/Linux)
- **Google Chrome / Chromium:** Required for Selenium-based data downloaders.

## Installation

```bash
# Core execution & strategy
pip install SmartApi breeze-connect pandas numpy pyotp requests websocket-client mibian

# Slack Listener
pip install slack-bolt

# Data Pipeline
pip install selenium webdriver-manager
```
