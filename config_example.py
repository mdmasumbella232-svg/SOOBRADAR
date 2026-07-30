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
# 1X2 market parameters
MIN_ODDS = 1.65
MAX_ODDS = 2.10
ODDS_DROP_THRESHOLD_PCT = 18.0  # Alert if odds drop by 18% or more compared to opening/prematch

# Alg.1 Rating Parameters
MIN_ALG1_RATING_THRESHOLD = 0.8  # Alert if absolute value of Alg.1 exceeds this

# General monitoring settings
POLL_INTERVAL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 10
MAX_CACHE_SIZE = 1000
