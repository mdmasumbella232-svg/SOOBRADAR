# Configuration settings for SOOBRADAR Telegram Bot

import os

# Telegram Settings
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

# API Settings
BASE_URL = "https://inforadar.live"
API_ROOT = "/api/v1/"

# Sport IDs
SPORT_SOCCER = 1
SPORT_BASKETBALL = 18

# Prediction Logic Tuning Parameters
# === STRATEGY & RISK PARAMETERS ===
MIN_ODDS = 1.65             # Minimum acceptable odds for any bet
MAX_ODDS = 2.10             # Maximum acceptable odds for any bet

# Strategy 1 (1X2 Drop) — DISABLED: bot only picks Total Over/Under
# ODDS_DROP_THRESHOLD_PCT = 20.0  # No longer used

# Strategy 2 (Alg.1 Totals) Config
MIN_ALG1_RATING_THRESHOLD = 1.0 # (Loosened) Minimum Alg.1 rating required to trigger

# Strategy 3 (Abnormal Halftime Line) Config
HT_ABNORMAL_LINE_GAP_THRESHOLD = 1.0 # Line must be at least this much higher than expected HT line

# General monitoring settings
POLL_INTERVAL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 15  # Increased from 8s — API can be slow; retry logic handles timeouts
MAX_CACHE_SIZE = 1000
