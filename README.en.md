# Jupiter Perps Wallet Tracker

🇩🇪 [Deutsche Version](README.md)

A monitoring system for a Solana wallet used for leverage trading on Jupiter Perps (ETH, SOL, BTC).

The idea: warnings **before** a loss happens, not after. On top of that, a daily report with charts, a backtest of a simple trading method and paper trading without real money.

The system is read-only. It has no private key and cannot execute trades.

> **All rules and numbers in this project are examples.** When alerts are sent, at which profit or loss, what the rule recommends and how often messages arrive can all be changed. See [Adjusting the rules](#adjusting-the-rules).

## What it does

**1. Telegram alerts (checked every 30 seconds)**

All numbers here are example values.

- profit or loss of a position in steps (every +5 % profit, every −10 % loss)
- distance to liquidation, in steps down to 0.25 %
- fast price moves in both directions
- fixed rules against typical leverage trading mistakes („👉 REGEL: …“), e.g. close half at +10 %, close at −50 %, profit melting away
- daily stop, pause after a losing streak, overtrading, drawdown
- deposits, withdrawals and swaps in the wallet
- `/status` in the chat shows the current state, after 6 quiet hours the bot sends a heartbeat

The most important information is bold at the top, so it is visible in the phone notification preview.

**2. Daily report (at night)**
- equity curve and 30-day overview as charts
- metrics and rule violations are calculated in Python
- a local language model (Ollama) only writes the text

**3. Trade analysis and backtest**
- reconstruction of the wallet's past perps trades directly from the blockchain
- backtest of the method „go long after a noticeable dip, hold briefly“ on minute prices, with a training and a test period and fees
- result: small dips with a small target and no trend filter were negative after fees on all markets. Only a few settings were positive in both periods.

**4. Paper trading**
- tests the best backtest setting live, without real money, and reports every buy and sell

## Architecture

```
Solana RPC ──► positions.py ──► alerts.py ──┐
Binance prices ──► papertrade.py ───────────┼──► service.py (FastAPI) ◄── n8n (every 30 s) ──► Telegram
Telegram /status ──► status.py ─────────────┘
wallet_export.py ──► report.py ──► Ollama (text) ──► Telegram (nightly)
```

- **Python** does all calculations: positions, fees, liquidation, rules
- **n8n** (Docker) calls the service regularly and sends the messages to Telegram
- **Ollama** (local, gemma4:26b) only writes the text of the daily report

The code, comments and Telegram messages are in German.

## Files

| File | Purpose |
|---|---|
| `positions.py` | reads open positions from Solana, calculates value like Jupiter („Inc. Fees“), borrow fee, liquidation |
| `alerts.py` | checks the rules and builds the alerts |
| `status.py` | `/status` command and heartbeat |
| `report.py` | daily report with charts and a local language model |
| `wallet_export.py` | full transaction history of the wallet as CSV |
| `trade_analyse.py` | reconstructs past perps trades |
| `backtest.py` | backtest of the method |
| `papertrade.py` | paper trading without real money |
| `service.py` | small local service called by n8n |
| `wallet_waechter_workflow.json` | n8n workflow for the alerts |
| `wallet_waechter_bericht_workflow.json` | n8n workflow for the daily report |
| `deploy/wallet-waechter.service` | systemd service so everything starts automatically |

## Adjusting the rules

All values are settings at the top of the files (section „1. Regeln“ or „1. Einstellungen“). The numbers below are only example values. Restart the service after a change.

**When you get informed** (`alerts.py`)

| Setting | Example | Meaning |
|---|---|---|
| `PNL_SCHRITT_GEWINN` | 5 | message at every +5 % profit on the collateral |
| `PNL_SCHRITT_VERLUST` | 10 | message at every −10 % loss |
| `PNL_PUFFER_GEWINN` / `PNL_PUFFER_VERLUST` | 2 / 3 | buffer against duplicate messages when the value swings around a step |
| `LIQ_STUFEN` | 5, 3, 2 … 0.25 | warning at this % distance to liquidation |
| `PREIS_FENSTER_MIN` / `PREIS_BEWEGUNG` | 10 / 0.25 | message when the price moves 0.25 % within 10 minutes |
| `PREIS_PAUSE_MIN` | 10 | same price message again after 10 minutes at the earliest |
| `STOP_LOSS_ERINNERUNG` | False | reminder when no stop-loss is set (True = on) |

**What the rule recommends** („👉 REGEL: …“, recommendation only, nothing is executed)

| Setting | Example | Recommendation |
|---|---|---|
| `GEWINN_TEIL_SICHERN` | 10 | from +10 %: close half |
| `GEWINN_GANZ_SICHERN` | 25 | from +25 %: close the rest |
| `GEWINN_SCHMILZT` | 0.5 | profit shrank to half: close |
| `VERLUST_SCHLIESSEN` | −50 | from −50 %: close |
| `LIQ_SCHLIESSEN` | 1.0 | liquidation 1 % away: close, do not add collateral |
| `MAX_HEBEL` / `KRITISCHER_HEBEL` | 10 / 25 | warning from this leverage |
| `RISIKO_PRO_TRADE` | 0.02 | at most 2 % of equity at risk per trade |

**Daily rules** (`alerts.py`)

| Setting | Example | Meaning |
|---|---|---|
| `TAGESVERLUST_MAX` | 0.06 | daily stop at −6 % |
| `MAX_TRADES_TAG` | 5 | more trades count as overtrading |
| `VERLUSTSERIE` / `PAUSE_NACH_SERIE_MIN` | 2 / 30 | 30 minutes pause after two losses in a row |
| `MAX_DRAWDOWN` | 0.20 | 20 % below the high: pause |
| `GROSSER_TAGESGEWINN` | 0.50 | +50 % in one day: secure profit, pause |

**Other places**

- check interval: in the n8n workflow „Wallet Wächter – Warnungen“, Schedule node (example: every 30 seconds)
- heartbeat: `LEBENSZEICHEN_STUNDEN` in `status.py` (example: 6)
- paper trading rule: `RUECKGANG`, `ZIEL`, `NOTAUSSTIEG` in `papertrade.py`
- backtest: tested values in `RUECKGANG`, `ZIEL`, `NOTAUSSTIEG`, fees with `--gebuehr` in `backtest.py`
- daily report: language model via `OLLAMA_MODEL` in `.env`; the `REGELN` text in `report.py` should match your own rules
- message wording: can be changed directly in `alerts.py`

## Setup (short)

1. `pip install -r requirements.txt`
2. copy `.env.example` to `.env` and fill it in
3. start the service: `uvicorn service:app --host 172.17.0.1 --port 8765` (or as a systemd service)
4. import both workflows into n8n, create Header Auth (`x-api-key`) and Telegram credentials, set *Parse Mode* to **HTML** in the Telegram node
5. send `/status` in Telegram

## Limitations

- Values are calculated from public account data. The opening fee is estimated, small differences to Jupiter are possible.
- The public Solana RPC is sometimes slow or rejects requests.
- The laptop has to be running for alerts to arrive. The system warns, it does not protect.
- The rules are example rules of thumb and not financial advice. Everyone should adapt them to themselves.
- Backtest results only apply to the tested period and say nothing certain about the future.

## Data sources

- [Solana JSON-RPC](https://solana.com/docs/rpc)
- [Jupiter Perps documentation](https://developers.jup.ag/docs/perps)
- [Jupiter Perps IDL and examples](https://github.com/julianfssen/jupiter-perps-anchor-idl-parsing)
- [Binance public market data](https://data-api.binance.vision)
- [Telegram Bot API](https://core.telegram.org/bots/api)

## AI use

The code was written with AI support. How I work with AI is described in [local-ai-workflow](https://github.com/Amirhouschang/local-ai-workflow).

---

© 2026 Amirhoushang Rahmannejad. All rights reserved.
