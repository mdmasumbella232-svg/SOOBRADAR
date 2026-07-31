# SlamRadar — Live Prediction Telegram Bot

A lightweight, 3G-safe, low-resource live prediction bot for soccer and basketball using [inforadar.live](https://inforadar.live).

## Features

- 🔍 **Live Scanning** — Monitors all live soccer & basketball games every 60 seconds
- 🎯 **Smart Picks** — Predicts Full Time **1X2** or **Total Over/Under** using:
  - Opening odds vs in-play odds drift (1X2)
  - Platform Alg.1 rating deviation (Total)
- 🔒 **One Pick at a Time** — Bot locks after each pick, waits for settlement, then unlocks
- 📊 **Record Tracking** — Persistent Win/Loss/Draw stats, streaks, and hit rate
- 🔄 **API-Down Resilient** — Falls back to finished_games list if game_view is unavailable
- 🔁 **Auto-Restart** — Recovers from crashes automatically via `start_bot.bat`
- 📱 **Telegram Alerts** — Premium formatted messages with full odds details

## Odds Filter

Only picks where the relevant live odds are between **1.65 and 2.10**.

## Setup

### 1. Clone the repo
```bash
git clone https://github.com/YOUR_USERNAME/SOOBRADAR.git
cd SOOBRADAR
```

### 2. Create your config
```bash
cp config_example.py config.py
# Edit config.py and fill in your Telegram token and chat ID
```

### 3. Run the bot
```bash
# Windows
double-click start_bot.bat

# Linux / VPS
nohup python3 prediction_bot.py &
```

### 4. Backtest
```bash
python backtest.py
```

## Telegram Alert Format

```
🔮 TOP PICK — 07:13 30/07

━━━━━━━━━━━━━━━━━━━━━━
⚽ Canada U20 vs Honduras U20
📍 CONCACAF U20 Championship | 13' | Score: 0-0
🔗 Open Match

🎯 1X2 → 1 🟢 STRONG (81%)
  Open: 1=2.25 X=3.50 2=2.60
  Now:  1=1.73 X=3.75 2=4.00
  Drift: 1=-0.52 X=+0.25 2=+1.40
  📈 Prob shift: +13.5%
━━━━━━━━━━━━━━━━━━━━━━

🔒 BOT IS NOW LOCKED
⏳ No new picks until this bet settles.
📊 Record: W1 / L0 / D0 | Total Bets: 1 | Hit Rate: 100%
```

## Files

| File | Purpose |
|---|---|
| `prediction_bot.py` | Main bot logic |
| `config_example.py` | Configuration template |
| `backtest.py` | Simulation on finished games |
| `start_bot.bat` | Auto-restart launcher (Windows) |
| `register_startup.bat` | Register bot to run on Windows boot |

## Data Source

[inforadar.live](https://inforadar.live) — Live soccer & basketball odds API

## Requirements

- Python 3.8+
- No external packages — uses standard library only
