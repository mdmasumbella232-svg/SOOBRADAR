import sys
import time
import ssl
import json
import os
import traceback
import urllib.request
import urllib.parse
import threading
from datetime import datetime
try:
    import config
except ImportError:
    import config_example as config

# Force UTF-8 output so emojis work on all terminals
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")

def log(msg, error=False):
    """Write timestamped log to console and bot.log file."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line, file=sys.stderr if error else sys.stdout)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# Create SSL context to bypass issues on restricted or slow networks
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

LOCK_FILE = "lock_state.json"
STATS_FILE = "stats.json"

class ProxyManager:
    """Manages proxy rotation for API requests.
    Uses per-request opener instead of global install_opener to avoid
    interfering with other urllib calls (e.g. proxy list fetch itself)."""
    def __init__(self):
        self.proxies = []
        self.current_proxy = None
        self.proxy_api_url = "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/all-proxies.txt"
        self._lock = threading.Lock()

    def fetch_proxies(self):
        try:
            req = urllib.request.Request(self.proxy_api_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as response:
                text = response.read().decode('utf-8')
                # Filter for HTTP proxies and strip the 'http://' prefix for urllib compat
                self.proxies = [
                    p.strip().replace("http://", "") 
                    for p in text.split('\n') 
                    if p.strip().startswith("http://")
                ]
                log(f"[PROXY] Fetched {len(self.proxies)} HTTP proxies from iplocate GitHub.")
        except Exception as e:
            log(f"[PROXY] Failed to fetch iplocate proxies: {e}", error=True)
            self.proxies = []

    def rotate_proxy(self):
        with self._lock:
            if not self.proxies:
                self.fetch_proxies()
            
            if self.proxies:
                import random
                self.current_proxy = random.choice(self.proxies)
                self.proxies.remove(self.current_proxy)
                log(f"[PROXY] Rotating to new iplocate proxy: {self.current_proxy} ({len(self.proxies)} remaining)")
                return True
            else:
                log("[PROXY] No proxies available. Using direct connection.")
                self.current_proxy = None
                return False

    def get_opener(self):
        """Build a per-request opener with the current proxy and SSL context.
        HTTPSHandler is used to embed the ssl_ctx so opener.open() doesn't need context= kwarg."""
        with self._lock:
            https_handler = urllib.request.HTTPSHandler(context=ssl_ctx)
            if self.current_proxy:
                proxy_handler = urllib.request.ProxyHandler({
                    'http': f"http://{self.current_proxy}",
                    'https': f"http://{self.current_proxy}"
                })
                return urllib.request.build_opener(proxy_handler, https_handler)
            else:
                return urllib.request.build_opener(https_handler)

proxy_manager = ProxyManager()


class InforadarAPIClient:
    """Lightweight client for inforadar.live API using standard library to minimize RAM/CPU usage.
    Includes retry logic with exponential backoff and per-request proxy support."""
    def __init__(self):
        self.base_url = config.BASE_URL.rstrip('/')
        self.api_root = config.API_ROOT.strip('/')
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json'
        }
        self.consecutive_errors = 0
        self.max_retries = 3  # Number of retries per request
        self.retry_backoff_base = 2  # Seconds for first retry delay

    def _request(self, endpoint, params=None, max_retries=None):
        url = f"{self.base_url}/{self.api_root}/{endpoint.lstrip('/')}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        
        req = urllib.request.Request(url, headers=self.headers)
        retries = max_retries if max_retries is not None else self.max_retries
        
        for attempt in range(retries + 1):
            try:
                # Use per-request opener with proxy if available (SSL context is baked into the opener)
                opener = proxy_manager.get_opener()
                with opener.open(req, timeout=config.REQUEST_TIMEOUT_SECONDS) as response:
                    if response.status == 200:
                        self.consecutive_errors = 0
                        return json.loads(response.read().decode('utf-8'))
                    else:
                        log(f"API Error: HTTP Status {response.status} for URL {url}", error=True)
                        return None
            except Exception as e:
                if attempt < retries:
                    delay = self.retry_backoff_base * (2 ** attempt)  # 2, 4, 8 seconds...
                    log(f"Connection Error fetching {url}: {e} (attempt {attempt+1}/{retries+1}, retrying in {delay}s)", error=True)
                    time.sleep(delay)
                    continue
                else:
                    log(f"Connection Error fetching {url}: {e} (all {retries+1} attempts failed)", error=True)
                    # Only count complete failures (not individual retry attempts)
                    self.consecutive_errors += 1
                
                if self.consecutive_errors >= 3:
                    log("[API] 3 consecutive connection errors detected. Triggering automatic IP rotation...")
                    proxy_manager.rotate_proxy()
                    self.consecutive_errors = 0
        return None

    def get_live_games(self, sport_id):
        """Fetch current live games for a specific sport."""
        params = {
            "sport_id": sport_id,
            "page": 1,
            "per_page": 1000
        }
        data = self._request("live_games", params, max_retries=2)  # 2 retries for important list endpoint
        if data and data.get("success") == 1:
            return data.get("results", [])
        return []

    def get_finished_games(self, sport_id):
        """Fetch finished games for a specific sport."""
        params = {
            "sport_id": sport_id,
            "page": 1,
            "per_page": 50
        }
        data = self._request("finished_games/", params, max_retries=2)
        if data and data.get("success") == 1:
            return data.get("results", [])
        return []

    def get_game_view(self, sport_id, event_id):
        """Fetch game details / stats."""
        sport_path = "soccer" if sport_id == config.SPORT_SOCCER else "basketball"
        return self._request(f"{sport_path}/game/view", {"event_id": event_id}, max_retries=1)

    def get_game_odds(self, sport_id, event_id):
        """Fetch game odds history for the standard 6 markets."""
        sport_path = "soccer" if sport_id == config.SPORT_SOCCER else "basketball"
        # Soccer uses 8,5,6,1,2,3 markets, Basketball uses 4,5,6,1,2,3 markets
        markets = "8,5,6,1,2,3" if sport_id == config.SPORT_SOCCER else "4,5,6,1,2,3"
        return self._request(f"{sport_path}/game/odds", {"event_id": event_id, "odds_market": markets}, max_retries=1)  # 1 retry for odds — skip fast


class PredictionEngine:
    """Analyze odds shifts and platform rating indicators to generate predictions."""
    @staticmethod
    def calculate_drop_pct(opening, live):
        if not opening or not live or opening <= 0:
            return 0.0
        return ((opening - live) / opening) * 100.0

    @classmethod
    def analyze_match(cls, sport_id, match, odds_markets):
        predictions = []
        if not odds_markets or not isinstance(odds_markets, list):
            return predictions

        home_team = match.get("home", {}).get("name", "Home")
        away_team = match.get("away", {}).get("name", "Away")
        scores = match.get("scores", "0-0")
        
        # Parse scores
        home_score, away_score = 0, 0
        try:
            if "-" in scores:
                parts = scores.split("-")
                home_score = int(parts[0])
                away_score = int(parts[1])
        except Exception:
            pass

        # Pre-parse markets for cross-market strategies
        parsed_markets = {}
        for market in odds_markets:
            m_name = market.get("name", "")
            m_odds = market.get("odds", [])
            m_first = market.get("firstPrematch", {})
            if m_odds and isinstance(m_odds, list):
                parsed_markets[m_name] = {
                    "first": m_first,
                    "latest": m_odds[0]
                }

        # --- MULTI-MARKET / EARLY LIVE STRATEGIES (New Rules) ---
        time_info = match.get("time", {})
        match_minute = 0
        is_early = False
        
        # Determine if early live (0 to 10 minutes)
        if sport_id == config.SPORT_SOCCER:
            tm_str = str(time_info.get("tm", ""))
            try:
                match_minute = int(tm_str)
                if 0 <= match_minute <= 10:
                    is_early = True
            except ValueError:
                pass
        elif sport_id == config.SPORT_BASKETBALL:
            q_str = str(time_info.get("q", ""))
            tm_str = str(time_info.get("tm", ""))
            # Assuming Q1 and time is around 10-12 mins left (varies by league, but we use early Q1)
            # Or we can just check if score is very low (e.g., total < 15)
            if q_str == "1" and (home_score + away_score) < 15:
                is_early = True

        if is_early:
            # Get 1X2
            m_1x2 = parsed_markets.get("1X2", {})
            p_1x2 = m_1x2.get("first", {})
            l_1x2 = m_1x2.get("latest", {})
            
            # Get Total
            m_total = parsed_markets.get("Total", {})
            p_tot = m_total.get("first", {})
            l_tot = m_total.get("latest", {})
            
            # Get Handicap (Asian Handicap / Spread)
            m_ah = parsed_markets.get("Handicap", {}) or parsed_markets.get("Asian Handicap", {})
            p_ah = m_ah.get("first", {})
            l_ah = m_ah.get("latest", {})
            
            # Extract opening 1X2 odds
            open_home = p_1x2.get("row1")
            open_away = p_1x2.get("row3")
            
            if sport_id == config.SPORT_SOCCER:
                if open_home and open_away:
                    # RULE 1: Competitive Under (Soccer) — ACTIVE
                    if 1.60 <= open_home <= 3.50 and 1.60 <= open_away <= 3.50:
                        open_line = p_tot.get("row2")
                        live_line = l_tot.get("row2")
                        # Check: game must have started (score must not be None)
                        # Check: live Under odds must exist and be within acceptable range
                        live_under_odds = l_tot.get("row3")
                        if (open_line in [3.25, 3.50] and live_line is not None
                                and scores is not None and str(scores) not in ["None", ""]
                                and live_under_odds is not None
                                and config.MIN_ODDS <= live_under_odds <= config.MAX_ODDS):
                            if live_line == open_line - 0.25:
                                predictions.append({
                                    "market": "Total", "prediction": f"Under {live_line}", "confidence": 95,
                                    "total_dir": "Under", "total_line": f"{live_line}",
                                    "open_line": f"{open_line}", "now_line": f"{live_line}", "line_diff": "-0.25",
                                    "open_over": f"{p_tot.get('row1', 'N/A')}" if p_tot.get('row1') else "N/A",
                                    "now_over": f"{l_tot.get('row1', 'N/A')}" if l_tot.get('row1') else "N/A",
                                    "open_under": f"{p_tot.get('row3', 'N/A')}" if p_tot.get('row3') else "N/A",
                                    "now_under": f"{live_under_odds:.2f}",
                                    "alg_val": "Comp_Under", "alg_dir": "Under",
                                    "reason": f"Soccer Rule 1: Competitive Under pattern triggered. Line dropped from {open_line} to {live_line}. Live Under odds: {live_under_odds:.2f}"
                                })
                    # RULE 2 (Blowout Over) and RULE 3 (Stale Line Over) REMOVED — poor backtest performance
            
            elif sport_id == config.SPORT_BASKETBALL:
                if open_home and open_away:
                    # RULE 1: Heavy Favorite Lock (Basketball)
                    if open_home <= 1.40 or open_away <= 1.40:
                        fav_pred = "1" if open_home <= 1.40 else "2"
                        fav_odds = open_home if open_home <= 1.40 else open_away
                        # Live odds of favorite must be within acceptable bet range
                        live_fav_odds = l_1x2.get("row1") if fav_pred == "1" else l_1x2.get("row3")
                        if live_fav_odds and config.MIN_ODDS <= live_fav_odds <= config.MAX_ODDS:
                            # Calculate proper drift and prob_shift
                            open_fav = open_home if fav_pred == "1" else open_away
                            open_other = open_away if fav_pred == "1" else open_home
                            live_other = l_1x2.get("row3") if fav_pred == "1" else l_1x2.get("row1")
                            drift_fav = live_fav_odds - open_fav if live_fav_odds and open_fav else None
                            drift_other = live_other - open_other if live_other and open_other else None
                            prob_shift_fav = ((1.0/live_fav_odds) - (1.0/open_fav)) * 100 if live_fav_odds and open_fav else None
                            confidence = min(99, int(85 + (1.40 - fav_odds) * 20))  # Lower odds = higher confidence
                            predictions.append({
                                "market": "1X2", "prediction": fav_pred, "confidence": confidence,
                                "open_1": f"{open_home:.2f}", "open_x": "N/A", "open_2": f"{open_away:.2f}",
                                "now_1": f"{l_1x2.get('row1', 0):.2f}", "now_x": "N/A", "now_2": f"{l_1x2.get('row3', 0):.2f}",
                                "drift_1": f"{drift_fav:+.2f}" if drift_fav is not None else "N/A",
                                "drift_x": "N/A",
                                "drift_2": f"{drift_other:+.2f}" if drift_other is not None else "N/A",
                                "prob_shift": f"{prob_shift_fav:+.1f}" if prob_shift_fav is not None else "N/A",
                                "reason": f"Basketball Rule 1: Heavy Favorite Lock. Backing {fav_pred} at live odds {live_fav_odds}."
                            })
                        
                    # RULE 2: Sharp Favorite Surge (Basketball)
                    # Requires: Handicap movement AND 1X2 odds movement confirmation
                    if 1.50 <= open_home <= 2.50 and 1.50 <= open_away <= 2.50:
                        open_ah = p_ah.get("row2")
                        live_ah = l_ah.get("row2")
                        if open_ah is not None and live_ah is not None:
                            ah_diff = live_ah - open_ah
                            if abs(ah_diff) >= 1.0:
                                fav_pred = "1" if ah_diff <= -1.0 else "2"
                                live_fav_odds = l_1x2.get("row1") if fav_pred == "1" else l_1x2.get("row3")
                                open_fav_odds = open_home if fav_pred == "1" else open_away
                                # Live odds of favorite must be within acceptable bet range
                                if live_fav_odds and config.MIN_ODDS <= live_fav_odds <= config.MAX_ODDS:
                                    # CONFIRMATION: 1X2 odds must have moved (dropped) for the favorite
                                    if open_fav_odds is not None and live_fav_odds < open_fav_odds:
                                        # Calculate proper drift and prob_shift
                                        open_other = open_away if fav_pred == "1" else open_home
                                        live_other = l_1x2.get("row3") if fav_pred == "1" else l_1x2.get("row1")
                                        drift_fav = live_fav_odds - open_fav_odds
                                        drift_other = live_other - open_other if live_other and open_other else None
                                        prob_shift_fav = ((1.0/live_fav_odds) - (1.0/open_fav_odds)) * 100
                                        confidence = min(95, int(75 + abs(ah_diff) * 5))
                                        predictions.append({
                                            "market": "1X2", "prediction": fav_pred, "confidence": confidence,
                                            "open_1": f"{open_home:.2f}", "open_x": "N/A", "open_2": f"{open_away:.2f}",
                                            "now_1": f"{l_1x2.get('row1', 0):.2f}", "now_x": "N/A", "now_2": f"{l_1x2.get('row3', 0):.2f}",
                                            "drift_1": f"{drift_fav:+.2f}" if fav_pred == "1" else (f"{drift_other:+.2f}" if drift_other is not None else "N/A"),
                                            "drift_x": "N/A",
                                            "drift_2": f"{drift_other:+.2f}" if fav_pred == "2" else (f"{drift_fav:+.2f}" if drift_fav is not None else "N/A"),
                                            "prob_shift": f"{prob_shift_fav:+.1f}",
                                            "reason": f"Basketball Rule 2: Sharp Favorite Surge. Spread moved {ah_diff} pts, 1X2 odds confirm. Backing {fav_pred}."
                                        })

        # If a new strategy triggered, return immediately to lock it
        if predictions:
            return predictions

        for market in odds_markets:
            market_name = market.get("name", "")
            odds_list = market.get("odds", [])
            first_prematch = market.get("firstPrematch", {})
            
            if not odds_list or not isinstance(odds_list, list):
                continue
            
            # Latest live odds are the first item in the list
            latest_live = odds_list[0]
            
            # --- STRATEGY 1: 1X2 Odds Drop Strategy ---
            if market_name == "1X2":
                opening_home = first_prematch.get("row1")
                opening_draw = first_prematch.get("row2")
                opening_away = first_prematch.get("row3")
                live_home = latest_live.get("row1")
                live_draw = latest_live.get("row2")
                live_away = latest_live.get("row3")
                
                # We check odds drop if the game is currently tied
                if home_score == away_score:
                    # Check Home Win odds drop
                    if opening_home and live_home and config.MIN_ODDS <= live_home <= config.MAX_ODDS:
                        drop_home = cls.calculate_drop_pct(opening_home, live_home)
                        if drop_home >= config.ODDS_DROP_THRESHOLD_PCT:
                            # Implied probability shift
                            prob_open = (1.0 / opening_home) * 100
                            prob_live = (1.0 / live_home) * 100
                            prob_shift = prob_live - prob_open
                            
                            # Calculate dynamic confidence rating based on drop
                            confidence = min(99, int(70 + (drop_home - config.ODDS_DROP_THRESHOLD_PCT) * 1.5))
                            
                            predictions.append({
                                "market": market_name,
                                "prediction": "1",
                                "confidence": confidence,
                                "open_1": f"{opening_home:.2f}" if opening_home else "N/A",
                                "open_x": f"{opening_draw:.2f}" if opening_draw else "N/A",
                                "open_2": f"{opening_away:.2f}" if opening_away else "N/A",
                                "now_1": f"{live_home:.2f}" if live_home else "N/A",
                                "now_x": f"{live_draw:.2f}" if live_draw else "N/A",
                                "now_2": f"{live_away:.2f}" if live_away else "N/A",
                                "drift_1": f"{live_home - opening_home:+.2f}" if live_home and opening_home else "N/A",
                                "drift_x": f"{live_draw - opening_draw:+.2f}" if live_draw and opening_draw else "N/A",
                                "drift_2": f"{live_away - opening_away:+.2f}" if live_away and opening_away else "N/A",
                                "prob_shift": f"{prob_shift:+.1f}",
                                "reason": f"Home Win odds dropped by {drop_home:.1f}% while tied."
                            })
                            
                    # Check Away Win odds drop
                    if opening_away and live_away and config.MIN_ODDS <= live_away <= config.MAX_ODDS:
                        drop_away = cls.calculate_drop_pct(opening_away, live_away)
                        if drop_away >= config.ODDS_DROP_THRESHOLD_PCT:
                            prob_open = (1.0 / opening_away) * 100
                            prob_live = (1.0 / live_away) * 100
                            prob_shift = prob_live - prob_open
                            
                            confidence = min(99, int(70 + (drop_away - config.ODDS_DROP_THRESHOLD_PCT) * 1.5))
                            
                            predictions.append({
                                "market": market_name,
                                "prediction": "2",
                                "confidence": confidence,
                                "open_1": f"{opening_home:.2f}" if opening_home else "N/A",
                                "open_x": f"{opening_draw:.2f}" if opening_draw else "N/A",
                                "open_2": f"{opening_away:.2f}" if opening_away else "N/A",
                                "now_1": f"{live_home:.2f}" if live_home else "N/A",
                                "now_x": f"{live_draw:.2f}" if live_draw else "N/A",
                                "now_2": f"{live_away:.2f}" if live_away else "N/A",
                                "drift_1": f"{live_home - opening_home:+.2f}" if live_home and opening_home else "N/A",
                                "drift_x": f"{live_draw - opening_draw:+.2f}" if live_draw and opening_draw else "N/A",
                                "drift_2": f"{live_away - opening_away:+.2f}" if live_away and opening_away else "N/A",
                                "prob_shift": f"{prob_shift:+.1f}",
                                "reason": f"Away Win odds dropped by {drop_away:.1f}% while tied."
                            })

            # --- STRATEGY 2: Rating-based (Alg.1) Deviation ---
            # FILTER A: Block Soccer Totals after 75th minute (too late, pace unreliable)
            soccer_late_game = False
            if sport_id == config.SPORT_SOCCER:
                try:
                    match_min = int(match.get("time", {}).get("tm", 0))
                    if match_min >= 75:
                        soccer_late_game = True
                except (ValueError, TypeError):
                    pass

            # FILTER B: Block Basketball Q3+Q4 Total bets (too volatile/late)
            basketball_late = False
            if sport_id == config.SPORT_BASKETBALL:
                try:
                    q_val = int(match.get("time", {}).get("q", 0))
                    if q_val >= 3:
                        basketball_late = True
                except (ValueError, TypeError):
                    pass

            if market_name == "Total" and not soccer_late_game and not basketball_late:
                ratings = latest_live.get("rating", [])
                if ratings and isinstance(ratings, list) and len(ratings) > 0:
                    rating_detail = ratings[0]
                    if isinstance(rating_detail, dict):
                        rating_val = rating_detail.get("rating")
                        direction = rating_detail.get("direction")

                        if rating_val is not None:
                            abs_rating = abs(rating_val)
                            if abs_rating >= config.MIN_ALG1_RATING_THRESHOLD:
                                dir_word = direction if direction else ("Over" if rating_val > 0 else "Under")

                                opening_over = first_prematch.get("row1")
                                opening_line = first_prematch.get("row2")
                                opening_under = first_prematch.get("row3")

                                live_over = latest_live.get("row1")
                                live_line = latest_live.get("row2")
                                live_under = latest_live.get("row3")

                                # Determine the relevant live and opening odds for the predicted direction
                                relevant_live_odds = live_over if dir_word == "Over" else live_under
                                relevant_opening_odds = opening_over if dir_word == "Over" else opening_under
                                
                                # Enforce odds range filter — skip if outside 1.65–2.10
                                if relevant_live_odds is None or not (config.MIN_ODDS <= relevant_live_odds <= config.MAX_ODDS):
                                    continue
                                    
                                # FILTER D: Require Odds Movement — odds must have dropped below their opening price
                                if relevant_opening_odds is not None and relevant_live_odds >= relevant_opening_odds:
                                    continue

                                # FILTER E: Soccer Over — block goal-induced line movements
                                # When goals are scored, the total line mechanically adjusts upward.
                                # This is NOT a predictive signal — the market is just repricing.
                                # Only pick Over when the line movement exceeds what goals explain.
                                if sport_id == config.SPORT_SOCCER and dir_word == "Over":
                                    total_goals = home_score + away_score
                                    if total_goals > 0 and opening_line is not None and live_line is not None:
                                        line_diff_val = live_line - opening_line
                                        if line_diff_val > 0 and line_diff_val <= total_goals + 0.25:
                                            log(f"[SKIP] Soccer Over blocked: goal-induced line movement (line {opening_line}→{live_line}, goals={total_goals})")
                                            continue
                                    # Cap Soccer Over line at 3.75 — lines above this are extremely high
                                    # and almost always result from goal-induced adjustments
                                    if live_line is not None and live_line > 3.75:
                                        log(f"[SKIP] Soccer Over blocked: line too high ({live_line} > 3.75)")
                                        continue

                                # FILTER C: Score Feasibility — current score must be on pace for the bet
                                if live_line is not None and live_line > 0:
                                    try:
                                        current_total = home_score + away_score
                                        pace_ratio = current_total / live_line
                                        if dir_word == "Over":
                                            # Current total must already be at least 60% of line
                                            if sport_id == config.SPORT_BASKETBALL and pace_ratio < 0.60:
                                                continue
                                            # For Soccer: current goal rate must project to at least 90% of line by 90'
                                            # Raised from 80% — Over picks need strong pace confirmation
                                            if sport_id == config.SPORT_SOCCER:
                                                # Use the match minute we already computed earlier
                                                safe_minute = max(1, match_minute)
                                                projected_goals = (current_total / safe_minute) * 90
                                                if projected_goals < live_line * 0.90:
                                                    continue
                                        elif dir_word == "Under":
                                            # For Under: current total must be <= 55% of line (still safely under)
                                            # Stricter threshold (was 70%) — Q3+ games already blocked above
                                            if sport_id == config.SPORT_BASKETBALL and pace_ratio > 0.55:
                                                continue
                                    except (ZeroDivisionError, TypeError, NameError):
                                        pass

                                line_diff = live_line - opening_line if live_line is not None and opening_line is not None else 0.0
                                confidence = min(99, int(70 + (abs_rating - config.MIN_ALG1_RATING_THRESHOLD) * 10))

                                predictions.append({
                                    "market": market_name,
                                    "prediction": f"{dir_word} {live_line}",
                                    "confidence": confidence,
                                    "total_dir": dir_word,
                                    "total_line": f"{live_line}",
                                    "open_line": f"{opening_line}" if opening_line is not None else "N/A",
                                    "now_line": f"{live_line}" if live_line is not None else "N/A",
                                    "line_diff": f"{line_diff:+.2f}" if line_diff != 0 else "0.0",
                                    "open_over": f"{opening_over:.2f}" if opening_over is not None else "N/A",
                                    "now_over": f"{live_over:.2f}" if live_over is not None else "N/A",
                                    "open_under": f"{opening_under:.2f}" if opening_under is not None else "N/A",
                                    "now_under": f"{live_under:.2f}" if live_under is not None else "N/A",
                                    "alg_val": f"{rating_val:.2f}",
                                    "alg_dir": f"{direction if direction else 'None'}",
                                    "reason": f"Alg.1 Rating deviation detected: {rating_val:+.2f}."
                                })

            # --- STRATEGY 3: Abnormal Line Dynamics (Soccer Halftime) ---
            if sport_id == config.SPORT_SOCCER and market_name == "Total" and home_score == 0 and away_score == 0:
                time_info = match.get("time", {})
                tm_str = str(time_info.get("tm", ""))
                is_ht = False
                if tm_str.upper() == "HT":
                    is_ht = True
                else:
                    try:
                        tm_val = int(tm_str)
                        if 40 <= tm_val <= 55:
                            is_ht = True
                    except Exception:
                        pass
                
                if is_ht:
                    opening_line = first_prematch.get("row2")
                    live_line = latest_live.get("row2")
                    if opening_line is not None and live_line is not None and opening_line > 0:
                        expected_ht_line = opening_line / 2.0
                        if live_line >= (expected_ht_line + config.HT_ABNORMAL_LINE_GAP_THRESHOLD):
                            # Ensure live Over odds are within acceptable range
                            live_over = latest_live.get("row1")
                            if live_over is not None and config.MIN_ODDS <= live_over <= config.MAX_ODDS:
                                predictions.append({
                                    "market": market_name,
                                    "prediction": f"Over {live_line}",
                                    "confidence": 85,
                                    "total_dir": "Over",
                                    "total_line": f"{live_line}",
                                    "open_line": f"{opening_line}",
                                    "now_line": f"{live_line}",
                                    "line_diff": f"{live_line - opening_line:+.2f}",
                                    "open_over": f"{first_prematch.get('row1', 0):.2f}" if first_prematch.get('row1') else "N/A",
                                    "now_over": f"{live_over:.2f}",
                                    "open_under": f"{first_prematch.get('row3', 0):.2f}" if first_prematch.get('row3') else "N/A",
                                    "now_under": f"{latest_live.get('row3', 0):.2f}" if latest_live.get('row3') else "N/A",
                                    "alg_val": "Anomaly",
                                    "alg_dir": "Over",
                                    "reason": f"0-0 at HT, but line is abnormally high ({live_line} vs expected {expected_ht_line})."
                                })

        return predictions


class TelegramBot:
    """Lightweight Telegram client using urllib to send styled messages."""
    def __init__(self):
        self.token = config.TELEGRAM_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID

    def send_message(self, text, parse_mode="HTML"):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode
        }).encode('utf-8')
        
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as response:
                res = json.loads(response.read().decode('utf-8'))
                return res.get("ok", False)
        except Exception as e:
            log(f"Failed to send Telegram message: {e}", error=True)
            return False


def load_lock_state():
    """Load bot locking state from file."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            log(f"Error reading lock file: {e}", error=True)
    return {"locked": False}


def save_lock_state(state):
    """Save bot locking state to file."""
    try:
        with open(LOCK_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"Error writing lock file: {e}", error=True)


def load_stats():
    """Load performance stats from file."""
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            log(f"Error reading stats file: {e}", error=True)
    return {
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "voids": 0,
        "total_bets": 0,
        "current_streak_type": "win",
        "current_streak_val": 0,
        "longest_w": 0,
        "longest_l": 0
    }


def save_stats(stats):
    """Save performance stats to file."""
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        log(f"Error writing stats file: {e}", error=True)


def update_stats(result):
    stats = load_stats()
    
    if result == "VOID":
        # Void bets don't count at all — no total_bets, no streak, no W/L
        stats["voids"] = stats.get("voids", 0) + 1
        log(f"[VOID] Bet voided. Not counted in W/L record.")
        save_stats(stats)
        return stats
    
    stats["total_bets"] += 1
    
    if result == "WIN":
        stats["wins"] += 1
        if stats["current_streak_type"] == "win":
            stats["current_streak_val"] += 1
        else:
            stats["current_streak_type"] = "win"
            stats["current_streak_val"] = 1
        stats["longest_w"] = max(stats["longest_w"], stats["current_streak_val"])
    elif result == "LOSS":
        stats["losses"] += 1
        if stats["current_streak_type"] == "loss":
            stats["current_streak_val"] += 1
        else:
            stats["current_streak_type"] = "loss"
            stats["current_streak_val"] = 1
        stats["longest_l"] = max(stats["longest_l"], stats["current_streak_val"])
    else:
        stats["draws"] += 1
        
    save_stats(stats)
    return stats


def evaluate_prediction(pred_info, final_home_score, final_away_score):
    prediction = pred_info.get("prediction", "")
    total_goals = final_home_score + final_away_score
    
    if "Win" in prediction or prediction in ["1", "2"]:
        if prediction == "1":
            if final_home_score > final_away_score:
                return "WIN"
            else:
                return "LOSS"
        elif prediction == "2":
            if final_away_score > final_home_score:
                return "WIN"
            else:
                return "LOSS"
                
    if "Over" in prediction:
        try:
            line_val = float(prediction.split("Over ")[1])
            if total_goals > line_val:
                return "WIN"
            elif total_goals == line_val:
                return "DRAW"
            else:
                return "LOSS"
        except Exception:
            pass
    elif "Under" in prediction:
        try:
            line_val = float(prediction.split("Under ")[1])
            if total_goals < line_val:
                return "WIN"
            elif total_goals == line_val:
                return "DRAW"
            else:
                return "LOSS"
        except Exception:
            pass

    return "UNKNOWN"


def format_stats_line(stats):
    total = stats.get("total_bets", 0)
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    draws = stats.get("draws", 0)
    
    hit_rate = 0
    resolved = wins + losses
    if resolved > 0:
        hit_rate = int((wins / resolved) * 100)
        
    streak_type = stats.get("current_streak_type", "win")
    streak_val = stats.get("current_streak_val", 0)
    streak_emoji = "🔥" if streak_type == "win" else "❄️"
    
    voids = stats.get("voids", 0)
    return f"📊 Record: W{wins} / L{losses} / D{draws} / V{voids} | Total Bets: {total} | Hit Rate: {hit_rate}% | Current streak: {streak_emoji}{streak_val} | Longest W: {stats.get('longest_w', 0)} | Longest L: {stats.get('longest_l', 0)}"


def format_telegram_alert(sport_id, match, prediction, stats):
    sport_emoji = "⚽" if sport_id == config.SPORT_SOCCER else "🏀"
    sport_path = "soccer" if sport_id == config.SPORT_SOCCER else "basketball"
    home_team = match.get("home", {}).get("name", "Home")
    away_team = match.get("away", {}).get("name", "Away")
    score = match.get("scores", "0-0")
    league = match.get("league", {}).get("name", "Unknown League")
    event_id = match.get("id")
    
    match_url = f"https://inforadar.live/#/dashboard/{sport_path}/game/{event_id}"
    
    # Calculate game time
    game_time = "Live"
    time_info = match.get("time", {})
    if isinstance(time_info, dict):
        if sport_id == config.SPORT_SOCCER:
            game_time = f"{time_info.get('tm', '')}'"
        else:
            game_time = f"Q{time_info.get('q', '')} - {time_info.get('tm', '')}"

    now_str = datetime.now().strftime("%H:%M %d/%m")
    stats_line = format_stats_line(stats)

    # Header Match Info
    msg = (
        f"🔮 <b>TOP PICK — {now_str}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{sport_emoji} {home_team} vs {away_team}\n"
        f"📍 {league} | {game_time} | Score: {score}\n"
        f"🔗 <a href='{match_url}'>Open Match</a>\n\n"
    )

    # Market details
    if prediction["market"] == "1X2":
        msg += (
            f"🎯 1X2 → {prediction['prediction']} 🟢 STRONG ({prediction['confidence']}%)\n"
            f"  Open: 1={prediction['open_1']} X={prediction['open_x']} 2={prediction['open_2']}\n"
            f"  Now:  1={prediction['now_1']} X={prediction['now_x']} 2={prediction['now_2']}\n"
            f"  Drift: 1={prediction['drift_1']} X={prediction['drift_x']} 2={prediction['drift_2']}\n"
            f"  📈 Prob shift: {prediction['prob_shift']}%\n"
        )
    else:
        msg += (
            f"🎯 Total {prediction['total_dir']}: {prediction['total_line']} 🟢 STRONG ({prediction['confidence']}%)\n"
            f"  Line: {prediction['open_line']} → {prediction['now_line']} ({prediction['line_diff']})\n"
            f"  Over: {prediction['open_over']} → {prediction['now_over']}\n"
            f"  Under: {prediction['open_under']} → {prediction['now_under']}\n"
            f"  📊 Alg: {prediction['alg_val']} ({prediction['alg_dir']})\n"
        )

    msg += (
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔒 <b>BOT IS NOW LOCKED</b>\n"
        f"⏳ No new picks until this bet settles.\n"
        f"{stats_line}"
    )
    return msg


def format_settlement_alert(sport_id, lock_state, final_score, result, stats):
    sport_emoji = "⚽" if sport_id == config.SPORT_SOCCER else "🏀"
    sport_path = "soccer" if sport_id == config.SPORT_SOCCER else "basketball"
    home_team = lock_state.get("home", "Home")
    away_team = lock_state.get("away", "Away")
    event_id = lock_state.get("event_id")
    prediction_val = lock_state.get("prediction", "")
    market = lock_state.get("market", "")
    
    match_url = f"https://inforadar.live/#/dashboard/{sport_path}/game/{event_id}"
    result_emoji = "✅" if result == "WIN" else "❌" if result == "LOSS" else "💤"
    
    stats_line = format_stats_line(stats)

    msg = (
        f"{result_emoji} <b>BET SETTLED — {result}</b>\n\n"
        f"{sport_emoji} {home_team} vs {away_team}\n"
        f"Final: {final_score}\n"
    )

    if market == "1X2":
        msg += f"Pick: 1X2 → {prediction_val}\n"
    else:
        msg += f"Pick: Total {prediction_val}\n"
        # Extract total goals
        total_goals = 0
        try:
            if "-" in final_score:
                parts = final_score.split("-")
                total_goals = int(parts[0]) + int(parts[1])
        except Exception:
            pass
        msg += f"Total goals: {total_goals}\n"

    msg += (
        f"🔗 <a href='{match_url}'>Open Match</a>\n\n"
        f"{stats_line}\n"
        f"🔓 <i>Bot is now UNLOCKED — scanning for next pick</i>"
    )
    return msg


def main():
    log("Starting SOOBRADAR Live Prediction Bot...")
    
    # --- ONE-TIME STATS CORRECTION ---
    # Fix the incorrect LOSS from the postponed Livingstone vs Tropical Royals game
    # (game was postponed but bot counted it as LOSS — should be VOID)
    stats = load_stats()
    if stats.get("total_bets") == 3 and stats.get("wins") == 1 and stats.get("losses") == 2 and stats.get("voids", 0) == 0:
        log("[CORRECTION] Fixing incorrect LOSS from postponed Livingstone vs Tropical Royals game")
        stats["losses"] = 1
        stats["voids"] = 1
        stats["total_bets"] = 2
        stats["current_streak_type"] = "loss"
        stats["current_streak_val"] = 1
        stats["longest_l"] = 1
        save_stats(stats)
        log("[CORRECTION] Stats corrected: W1/L1/V1 | Total Bets: 2")
    
    client = InforadarAPIClient()
    bot = TelegramBot()
    
    # Cache to store match states to avoid redundant odds fetches
    match_cache = {}

    while True:
        try:
            # Check if bot is locked
            lock_state = load_lock_state()
            if lock_state.get("locked"):
                sport_id = lock_state.get("sport_id")
                event_id = lock_state.get("event_id")
                home = lock_state.get("home")
                away = lock_state.get("away")
                prediction = lock_state.get("prediction")

                # --- MAX LOCK DURATION: Auto-unlock if stuck too long ---
                MAX_LOCK_SECONDS = 10800 if sport_id == config.SPORT_SOCCER else 7200  # 3h Soccer, 2h Basketball
                locked_at_str = lock_state.get("locked_at")
                if locked_at_str:
                    try:
                        locked_at_dt = datetime.fromisoformat(locked_at_str)
                        elapsed = (datetime.now() - locked_at_dt).total_seconds()
                        if elapsed > MAX_LOCK_SECONDS:
                            log(f"[TIMEOUT] Bot has been locked for {elapsed/3600:.1f}h on event {event_id}. Force-unlocking.", error=True)
                            stats = update_stats("UNKNOWN")
                            timeout_msg = (
                                f"⏰ <b>BET TIMEOUT — UNKNOWN</b>\n\n"
                                f"Event: {home} vs {away}\n"
                                f"Pick: {prediction}\n"
                                f"Reason: API unreachable for {elapsed/3600:.1f} hours. Could not confirm result.\n\n"
                                f"🔓 <i>Bot is now UNLOCKED — scanning for next pick</i>"
                            )
                            bot.send_message(timeout_msg)
                            save_lock_state({"locked": False})
                            log(f"[UNLOCK] Force-unlocked after timeout. Scanning for next pick...")
                            time.sleep(config.POLL_INTERVAL_SECONDS)
                            continue
                    except Exception as te:
                        log(f"[TIMEOUT] Could not parse locked_at: {te}", error=True)

                log(f"[LOCKED] Monitoring {home} vs {away} | Event: {event_id} | Pick: {prediction}")

                settled = False
                scores = None
                time_status = None

                # --- PRIMARY: Try game_view endpoint ---
                # timeStatus meanings: "2"=Finished, "3"=Cancelled, "4"=Postponed, "10"=Abandoned, "99"=Unknown
                VOID_STATUSES = {"3", "4", "10"}   # Cancelled, Postponed, Abandoned → VOID
                FINISH_STATUSES = {"2"}             # Finished → evaluate score
                UNKNOWN_STATUSES = {"99"}           # Unknown → treat as VOID (can't confirm result)
                ALL_SETTLE_STATUSES = FINISH_STATUSES | VOID_STATUSES | UNKNOWN_STATUSES

                game_view = client.get_game_view(sport_id, event_id)
                is_void = False
                void_reason = ""
                if game_view:
                    time_status = str(game_view.get("timeStatus", ""))
                    scores = game_view.get("scores", "0-0")
                    if time_status in ALL_SETTLE_STATUSES:
                        settled = True
                        if time_status in VOID_STATUSES:
                            is_void = True
                            void_reason = {"3": "Cancelled", "4": "Postponed", "10": "Abandoned"}.get(time_status, "Unknown")
                        elif time_status in UNKNOWN_STATUSES:
                            is_void = True
                            void_reason = "Unknown status"
                        log(f"[SETTLE] game_view timeStatus={time_status}. Score: {scores}. Void={is_void} ({void_reason})")

                # --- FALLBACK: API was down or game_view returned nothing ---
                # Search the finished_games list for the event_id
                if not settled and not game_view:
                    log(f"[FALLBACK] game_view unavailable, scanning finished_games list for event {event_id}...")
                    finished = client.get_finished_games(sport_id)
                    for fin_game in finished:
                        if str(fin_game.get("id")) == str(event_id):
                            fin_status = str(fin_game.get("timeStatus", ""))
                            fin_scores = fin_game.get("scores", "0-0")
                            if fin_status in ALL_SETTLE_STATUSES:
                                settled = True
                                scores = fin_scores
                                time_status = fin_status
                                if fin_status in VOID_STATUSES:
                                    is_void = True
                                    void_reason = {"3": "Cancelled", "4": "Postponed", "10": "Abandoned"}.get(fin_status, "Unknown")
                                elif fin_status in UNKNOWN_STATUSES:
                                    is_void = True
                                    void_reason = "Unknown status"
                                log(f"[FALLBACK] Found event {event_id} in finished_games. Score: {scores}. Void={is_void} ({void_reason})")
                            break

                # --- Periodic heartbeat: log every 30 cycles so user knows bot is alive ---
                lock_cycle = lock_state.get("cycle_count", 0) + 1
                lock_state["cycle_count"] = lock_cycle
                save_lock_state(lock_state)
                if lock_cycle % 30 == 0:
                    log(f"[HEARTBEAT] Still locked on {home} vs {away} | Waiting for settlement...")

                if settled and scores is not None:
                    if is_void:
                        # Game was cancelled/postponed/abandoned — VOID the bet
                        log(f"[VOID] Match {home} vs {away} was {void_reason}. Score: {scores}. Voiding bet.")
                        result = "VOID"
                        stats = update_stats(result)

                        sport_emoji = "⚽" if sport_id == config.SPORT_SOCCER else "🏀"
                        void_msg = (
                            f"💤 <b>BET VOIDED — {void_reason.upper()}</b>\n\n"
                            f"{sport_emoji} {home} vs {away}\n"
                            f"Final: {scores}\n"
                            f"Pick: {prediction}\n"
                            f"Reason: Game was {void_reason}\n\n"
                            f"🔓 <i>Bot is now UNLOCKED — scanning for next pick</i>\n\n"
                            f"{format_stats_line(stats)}"
                        )
                        bot.send_message(void_msg)

                        save_lock_state({"locked": False})
                        log(f"[UNLOCK] Bot is now UNLOCKED. Bet voided ({void_reason}). Scanning for next pick...")
                    else:
                        # Normal settlement — game finished
                        log(f"[SETTLE] Match {home} vs {away} confirmed finished. Score: {scores}. Resolving...")

                        home_score, away_score = 0, 0
                        try:
                            if "-" in scores:
                                parts = scores.split("-")
                                home_score = int(parts[0])
                                away_score = int(parts[1])
                        except Exception:
                            pass

                        result = evaluate_prediction(lock_state, home_score, away_score)
                        stats = update_stats(result)

                        msg = format_settlement_alert(sport_id, lock_state, scores, result, stats)
                        bot.send_message(msg)

                        save_lock_state({"locked": False})
                        log(f"[UNLOCK] Bot is now UNLOCKED. Result: {result}. Scanning for next pick...")
                elif not game_view and not settled:
                    log("[API-DOWN] Could not reach API. Will retry next cycle...", error=True)

                
            else:
                # Normal live scanning
                for sport_id in [config.SPORT_SOCCER, config.SPORT_BASKETBALL]:
                    sport_name = "Soccer" if sport_id == config.SPORT_SOCCER else "Basketball"
                    live_games = client.get_live_games(sport_id)
                    
                    log(f"[Status: UNLOCKED] Scanned {len(live_games)} live {sport_name} games.")
                    
                    for game in live_games:
                        # Re-verify lock state inside loop in case it got locked in this iteration
                        if load_lock_state().get("locked"):
                            break
                            
                        event_id = game.get("id")
                        if not event_id:
                            continue
                            
                        # Do not bet on games that are finished, cancelled, or in final transition
                        time_status = str(game.get("timeStatus", ""))
                        if time_status in ["3", "4", "99", "10"]:
                            continue
                        
                        
                        # Prevent betting on games in the absolute final moments (API ghost lines)
                        time_info = game.get("time", {})
                        if sport_id == config.SPORT_BASKETBALL:
                            q_str = str(time_info.get("q", ""))
                            tm_str = str(time_info.get("tm", ""))
                            if str(time_info.get("tt", "")) == "":
                                continue
                            if q_str == "4" and tm_str in ["0", "1"]:
                                continue
                        elif sport_id == config.SPORT_SOCCER:
                            try:
                                tm_val = int(time_info.get("tm", 0))
                                if tm_val >= 88:
                                    continue
                            except (ValueError, TypeError):
                                pass
                        scores = game.get("scores", "0-0")
                        time_info = game.get("time", {})
                        state_key = f"{scores}_{json.dumps(time_info)}"
                        
                        if match_cache.get(event_id) == state_key:
                            continue
                        
                        match_cache[event_id] = state_key
                        
                        odds_data = client.get_game_odds(sport_id, event_id)
                        time.sleep(0.8)  # Throttle to prevent IP ban (reduced from 1.5s)
                        if not odds_data:
                            continue
                        
                        predictions = PredictionEngine.analyze_match(sport_id, game, odds_data)
                        if predictions:
                            # We pick the first matching prediction and lock on it
                            pred = predictions[0]
                            stats = load_stats()
                            
                            msg = format_telegram_alert(sport_id, game, pred, stats)
                            
                            log(f"Found prediction: {pred['prediction']}. Sending alert and locking bot...")
                            
                            success = bot.send_message(msg)
                            if success:
                                # Lock the bot
                                new_lock = {
                                    "locked": True,
                                    "locked_at": datetime.now().isoformat(),
                                    "sport_id": sport_id,
                                    "event_id": event_id,
                                    "home": game.get("home", {}).get("name", "Home"),
                                    "away": game.get("away", {}).get("name", "Away"),
                                    "market": pred["market"],
                                    "prediction": pred["prediction"],
                                    "opening_val": pred.get("open_1") if pred["market"] == "1X2" else pred.get("open_line"),
                                    "live_val": pred.get("now_1") if pred["market"] == "1X2" else pred.get("now_line"),
                                    "reason": pred["reason"]
                                }
                                save_lock_state(new_lock)
                                log(f"Bot is successfully locked on event {event_id}.")
                                break
                                
                            time.sleep(1.0)

            # Prevent cache growing too large
            if len(match_cache) > config.MAX_CACHE_SIZE:
                match_cache.clear()
                
        except Exception as e:
            log(f"Error in main loop: {e}\n{traceback.format_exc()}", error=True)
            
        time.sleep(config.POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    log("=" * 50)
    log("SlamRadar Bot started — 24/7 mode active.")
    log(f"Log file: {LOG_FILE}")
    log("=" * 50)
    try:
        main()
    except KeyboardInterrupt:
        log("Bot stopped manually by user (KeyboardInterrupt). Exiting cleanly...")
    except Exception as e:
        log(f"Fatal error: {e}", error=True)

