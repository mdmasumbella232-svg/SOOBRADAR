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
    interfering with other urllib calls (e.g. proxy list fetch itself).
    Supports forced direct connection mode for fallback when proxies fail."""
    def __init__(self):
        self.proxies = []
        self.current_proxy = None
        self.proxy_api_url = "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/all-proxies.txt"
        self._lock = threading.Lock()
        self.force_direct = False  # When True, skip proxy and use direct connection
        self.direct_fail_count = 0  # Track consecutive direct connection failures
        self.proxy_fail_count = 0   # Track consecutive proxy failures

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
                self.proxy_fail_count = 0
                self.force_direct = False
                log(f"[PROXY] Rotating to new iplocate proxy: {self.current_proxy} ({len(self.proxies)} remaining)")
                return True
            else:
                log("[PROXY] No proxies available. Using direct connection.")
                self.current_proxy = None
                return False

    def toggle_direct_mode(self, enable):
        """Switch between proxy and direct connection mode.
        When proxies fail, we fall back to direct; when direct fails, we go back to proxy."""
        with self._lock:
            self.force_direct = enable
            if enable:
                self.current_proxy = None
                log("[PROXY] Switched to DIRECT connection mode (no proxy)")
            else:
                log("[PROXY] Switched back to PROXY mode")

    def get_opener(self, use_direct=False):
        """Build a per-request opener with the current proxy and SSL context.
        HTTPSHandler is used to embed the ssl_ctx so opener.open() doesn't need context= kwarg.
        use_direct=True forces direct connection regardless of proxy state."""
        with self._lock:
            https_handler = urllib.request.HTTPSHandler(context=ssl_ctx)
            if self.current_proxy and not self.force_direct and not use_direct:
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
        self.consecutive_errors = {}  # Track errors per sport_id to avoid cross-sport reset
        self.max_retries = 3  # Number of retries per request
        self.retry_backoff_base = 2  # Seconds for first retry delay

    def _request(self, endpoint, params=None, max_retries=None, sport_id=None):
        url = f"{self.base_url}/{self.api_root}/{endpoint.lstrip('/')}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        
        req = urllib.request.Request(url, headers=self.headers)
        retries = max_retries if max_retries is not None else self.max_retries
        error_key = sport_id if sport_id is not None else "default"
        
        # SMART RETRY: Try DIRECT first (1 attempt, fast timeout).
        # If DIRECT fails, switch to PROXY immediately — don't waste time retrying DIRECT.
        # Previous logic tried DIRECT twice (50s wasted) before falling back to PROXY.
        # Also: if DIRECT failed recently (direct_fail_count > 0), start with PROXY.
        direct_failed_recently = proxy_manager.direct_fail_count > 0
        
        for attempt in range(retries + 1):
            try:
                # Decide mode: 
                # - If DIRECT failed recently, start with PROXY
                # - Otherwise, try DIRECT first (attempt 0), then PROXY for all retries
                if direct_failed_recently:
                    use_direct = False  # Skip DIRECT, go straight to PROXY
                else:
                    use_direct = (attempt == 0)  # Only 1 DIRECT attempt, then PROXY
                
                opener = proxy_manager.get_opener(use_direct=use_direct)
                # Shorter timeout for DIRECT (fail fast), longer for PROXY
                timeout = config.REQUEST_TIMEOUT_SECONDS
                if use_direct:
                    timeout = min(timeout, 12)  # 12s max for DIRECT — fail fast
                else:
                    if endpoint == "live_games":
                        timeout = max(timeout, 20)  # 20s for live_games via PROXY
                    elif "game/odds" in endpoint:
                        timeout = max(timeout, 18)  # 18s for game odds via PROXY
                with opener.open(req, timeout=timeout) as response:
                    if response.status == 200:
                        self.consecutive_errors[error_key] = 0
                        data = json.loads(response.read().decode('utf-8'))
                        # If this connection mode worked, remember that
                        if use_direct:
                            proxy_manager.direct_fail_count = 0
                        else:
                            proxy_manager.proxy_fail_count = 0
                        return data
                    else:
                        log(f"API Error: HTTP Status {response.status} for URL {url}", error=True)
                        return None
            except Exception as e:
                if use_direct:
                    proxy_manager.direct_fail_count += 1
                    if proxy_manager.direct_fail_count >= 3:
                        log(f"[CONNECT] DIRECT failed {proxy_manager.direct_fail_count}x — switching to PROXY-first mode", error=True)
                else:
                    proxy_manager.rotate_proxy()
                
                if attempt < retries:
                    delay = min(self.retry_backoff_base * (2 ** attempt), 4)  # 2, 4, 4s max
                    mode = "DIRECT" if use_direct else "PROXY"
                    log(f"Connection Error fetching {url}: {e} (attempt {attempt+1}/{retries+1}, mode={mode}, retrying in {delay}s)", error=True)
                    time.sleep(delay)
                    continue
                else:
                    mode = "DIRECT" if use_direct else "PROXY"
                    log(f"Connection Error fetching {url}: {e} (all {retries+1} attempts failed, last mode={mode})", error=True)
                    self.consecutive_errors[error_key] = self.consecutive_errors.get(error_key, 0) + 1
                
                if self.consecutive_errors.get(error_key, 0) >= 3:
                    sport_name = f"Sport {sport_id}" if sport_id is not None else "API"
                    log(f"[API] 3 consecutive {sport_name} errors detected. Triggering automatic IP rotation...")
                    proxy_manager.rotate_proxy()
                    self.consecutive_errors[error_key] = 0
        return None

    def get_live_games(self, sport_id):
        """Fetch current live games for a specific sport.
        Uses smaller per_page (100) to reduce response size and avoid timeouts.
        Fetches up to 3 pages to cover all live games."""
        all_games = []
        pages_fetched = 0
        for page in range(1, 4):  # Fetch up to 3 pages (max 300 games)
            params = {
                "sport_id": sport_id,
                "page": page,
                "per_page": 100  # Reduced from 1000 — soccer has many games, huge response causes timeouts
            }
            data = self._request("live_games", params, max_retries=3, sport_id=sport_id)
            if data and data.get("success") == 1:
                results = data.get("results", [])
                all_games.extend(results)
                pages_fetched = page
                # If we got fewer than per_page results, this is the last page
                if len(results) < 100:
                    break
            else:
                # If a page fails, stop pagination and return what we have
                break
        if pages_fetched > 1:
            log(f"[API] Fetched {len(all_games)} live games across {pages_fetched} page(s) for sport_id={sport_id}")
        return all_games

    def get_finished_games(self, sport_id):
        """Fetch finished games for a specific sport."""
        params = {
            "sport_id": sport_id,
            "page": 1,
            "per_page": 50
        }
        data = self._request("finished_games/", params, max_retries=2, sport_id=sport_id)
        if data and data.get("success") == 1:
            return data.get("results", [])
        return []

    def get_game_view(self, sport_id, event_id):
        """Fetch game details / stats."""
        sport_path = "soccer" if sport_id == config.SPORT_SOCCER else "basketball"
        return self._request(f"{sport_path}/game/view", {"event_id": event_id}, max_retries=1, sport_id=sport_id)

    def get_game_odds(self, sport_id, event_id):
        """Fetch game odds history for the standard 6 markets."""
        sport_path = "soccer" if sport_id == config.SPORT_SOCCER else "basketball"
        # Soccer uses 8,5,6,1,2,3 markets, Basketball uses 4,5,6,1,2,3 markets
        markets = "8,5,6,1,2,3" if sport_id == config.SPORT_SOCCER else "4,5,6,1,2,3"
        return self._request(f"{sport_path}/game/odds", {"event_id": event_id, "odds_market": markets}, max_retries=2, sport_id=sport_id)  # 2 retries — more chances to fetch odds before skipping


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
        no_signal_reasons = []  # Track why no signal was generated
        if not odds_markets or not isinstance(odds_markets, list):
            return predictions, ["no odds data"]

        home_team = match.get("home", {}).get("name", "Home")
        away_team = match.get("away", {}).get("name", "Away")
        scores = match.get("scores", "0-0")
        
        # Parse scores — prefer odds data over game list for accuracy
        # The game list is fetched before the odds data, so a goal can be scored
        # between the two fetches. Using stale game list score causes false signals.
        home_score, away_score = 0, 0
        try:
            if "-" in scores:
                parts = scores.split("-")
                home_score = int(parts[0])
                away_score = int(parts[1])
        except Exception:
            pass

        # Track the most up-to-date score from odds data (updated during market parsing)
        # This is used by Strategy 3 (HT Anomaly) and Strategy 2 (FILTER E/C) to avoid
        # false signals from stale game list data.
        latest_odds_home, latest_odds_away = home_score, away_score

        # Pre-parse markets for cross-market strategies
        parsed_markets = {}
        available_markets = []
        for market in odds_markets:
            m_name = market.get("name", "")
            m_odds = market.get("odds", [])
            m_first = market.get("firstPrematch", {})
            available_markets.append(m_name)
            # Use earliest odds list entry as opening (same bookmaker as live odds)
            # Fall back to firstPrematch if odds list is too short
            m_earliest = m_odds[-1] if m_odds and isinstance(m_odds, list) and len(m_odds) > 1 else m_first
            if m_odds and isinstance(m_odds, list):
                parsed_markets[m_name] = {
                    "first": m_earliest,
                    "latest": m_odds[0]
                }
        if "Total" not in parsed_markets:
            no_signal_reasons.append("no Total market")

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
            # Get Total
            m_total = parsed_markets.get("Total", {})
            p_tot = m_total.get("first", {})
            l_tot = m_total.get("latest", {})
            
            # Get 1X2 (used only as condition for Soccer Rule 1 — Competitive Under)
            m_1x2 = parsed_markets.get("1X2", {})
            p_1x2 = m_1x2.get("first", {})
            open_home = p_1x2.get("row1")
            open_away = p_1x2.get("row3")
            
            if sport_id == config.SPORT_SOCCER:
                if not open_home or not open_away:
                    no_signal_reasons.append("early: no 1X2 odds")
                elif not (1.50 <= open_home <= 4.00 and 1.50 <= open_away <= 4.00):
                    no_signal_reasons.append(f"early: 1X2 odds out of range (H={open_home}, A={open_away})")
                else:
                    # RULE 1: Competitive Under (Soccer) — ACTIVE (widened for aggressive mode)
                    # Widened: accept 1X2 odds 1.50-4.00 (was 1.60-3.50), opening lines 3.00-3.75 (was 3.25-3.50),
                    # line drops of 0.25 or 0.50 (was only 0.25)
                    # IMPORTANT: game must have ACTUALLY started (score must be a valid number like "0-0"),
                    # not just "None" or empty. Pre-match line drops are NOT live signals.
                    open_line = p_tot.get("row2")
                    live_line = l_tot.get("row2")
                    # Check: game must have started (score must be a valid number like "0-0")
                    # "None" or "" means the game hasn't started — skip pre-match line drops
                    score_valid = scores is not None and str(scores) not in ["None", ""] and "-" in str(scores)
                    # Check: live Under odds must exist and be within acceptable range
                    live_under_odds = l_tot.get("row3")
                    if open_line not in [3.00, 3.25, 3.50, 3.75]:
                        no_signal_reasons.append(f"early: open line {open_line} not in [3.00-3.75]")
                    elif live_line is None:
                        no_signal_reasons.append("early: no live line")
                    elif not score_valid:
                        no_signal_reasons.append("early: score not valid (game not started?)")
                    elif live_under_odds is None:
                        no_signal_reasons.append("early: no live Under odds")
                    elif not (config.MIN_ODDS <= live_under_odds <= config.MAX_ODDS):
                        no_signal_reasons.append(f"early: Under odds {live_under_odds:.2f} outside [{config.MIN_ODDS}-{config.MAX_ODDS}]")
                    else:
                        line_drop = open_line - live_line if live_line is not None else 0
                        if line_drop >= 0.25:
                            predictions.append({
                                "market": "Total", "prediction": f"Under {live_line}", "confidence": 95,
                                "total_dir": "Under", "total_line": f"{live_line}",
                                "open_line": f"{open_line}", "now_line": f"{live_line}", "line_diff": f"{-line_drop:.2f}",
                                "open_over": f"{p_tot.get('row1', 'N/A')}" if p_tot.get('row1') else "N/A",
                                "now_over": f"{l_tot.get('row1', 'N/A')}" if l_tot.get('row1') else "N/A",
                                "open_under": f"{p_tot.get('row3', 'N/A')}" if p_tot.get('row3') else "N/A",
                                "now_under": f"{live_under_odds:.2f}",
                                "alg_val": "Comp_Under", "alg_dir": "Under",
                                "reason": f"Soccer Rule 1: Competitive Under pattern triggered. Line dropped from {open_line} to {live_line} (-{line_drop:.2f}). Live Under odds: {live_under_odds:.2f}"
                            })
                        else:
                            no_signal_reasons.append(f"early: line drop {line_drop:.2f} < 0.25")
                    # RULE 2 (Blowout Over) and RULE 3 (Stale Line Over) REMOVED — poor backtest performance
            elif sport_id == config.SPORT_BASKETBALL:
                no_signal_reasons.append("early: no basketball early rule")
        else:
            # Not early game — strategies 2-6 will be checked in the main loop below
            if sport_id == config.SPORT_SOCCER and match_minute > 0:
                no_signal_reasons.append(f"not early ({match_minute}')")

        # If a new strategy triggered, return immediately to lock it
        if predictions:
            return predictions, []

        for market in odds_markets:
            market_name = market.get("name", "")
            odds_list = market.get("odds", [])
            first_prematch = market.get("firstPrematch", {})
            
            if not odds_list or not isinstance(odds_list, list):
                continue
            
            # Skip 1X2 market — only Total Over/Under predictions
            if market_name == "1X2":
                continue
            
            # Latest live odds are the first item in the list
            latest_live = odds_list[0]
            
            # --- SCORE VERIFICATION FIX ---
            # Extract the live score from the latest odds entry.
            # This is more current than the game list score (which was fetched earlier).
            # A goal can be scored between the game list fetch and the odds fetch,
            # causing false signals if we use the stale game list score.
            odds_score_str = str(latest_live.get("scores", "")).strip()
            if odds_score_str and odds_score_str not in ["None", "-", ""]:
                try:
                    sp = odds_score_str.split("-")
                    odds_h = int(sp[0])
                    odds_a = int(sp[1])
                    # Update the latest score (only overwrite if we got valid data)
                    latest_odds_home, latest_odds_away = odds_h, odds_a
                except Exception:
                    pass
            
            # --- BOOKMAKER COMPARISON FIX ---
            # firstPrematch may come from a different bookmaker than the live odds list.
            # This inflates the apparent drop when bookmakers disagree on the opening price.
            # FIX: Use the earliest entry in the odds list (odds_list[-1]) as the opening,
            # since it's from the same data source as the live odds.
            # Fall back to firstPrematch only if the odds list is too short.
            earliest_live = odds_list[-1] if len(odds_list) > 1 else first_prematch

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

            if market_name != "Total":
                # Skip non-Total markets silently — only Total matters for our strategies
                continue
            elif soccer_late_game:
                no_signal_reasons.append("S2: soccer late game (75'+)")
            elif basketball_late:
                no_signal_reasons.append("S2: basketball late (Q3+)")
            else:
                ratings = latest_live.get("rating", [])
                if not ratings or not isinstance(ratings, list) or len(ratings) == 0:
                    no_signal_reasons.append("S2: no rating data")
                else:
                    rating_detail = ratings[0]
                    # Handle different rating formats from API:
                    # Format 1: {"rating": 0.06, "direction": "Over"} (dict)
                    # Format 2: 0.06 (just a number — use sign as direction)
                    # Format 3: "0.06" (string — parse as float)
                    rating_val = None
                    direction = None
                    if isinstance(rating_detail, dict):
                        rating_val = rating_detail.get("rating")
                        direction = rating_detail.get("direction")
                    elif isinstance(rating_detail, (int, float)):
                        rating_val = float(rating_detail)
                    elif isinstance(rating_detail, str):
                        try:
                            rating_val = float(rating_detail)
                        except (ValueError, TypeError):
                            no_signal_reasons.append(f"S2: rating is unparseable string '{rating_detail}'")
                            continue

                    if rating_val is None:
                        no_signal_reasons.append("S2: rating value is None")
                    else:
                        abs_rating = abs(rating_val)
                        if abs_rating < config.MIN_ALG1_RATING_THRESHOLD:
                            no_signal_reasons.append(f"S2: rating {rating_val:+.2f} < {config.MIN_ALG1_RATING_THRESHOLD}")
                        else:
                            dir_word = direction if direction else ("Over" if rating_val > 0 else "Under")

                            opening_over = earliest_live.get("row1") or first_prematch.get("row1")
                            opening_line = earliest_live.get("row2") or first_prematch.get("row2")
                            opening_under = earliest_live.get("row3") or first_prematch.get("row3")

                            live_over = latest_live.get("row1")
                            live_line = latest_live.get("row2")
                            live_under = latest_live.get("row3")

                            # Determine the relevant live and opening odds for the predicted direction
                            relevant_live_odds = live_over if dir_word == "Over" else live_under
                            relevant_opening_odds = opening_over if dir_word == "Over" else opening_under
                            
                            # Enforce odds range filter — skip if outside 1.65–2.10
                            if relevant_live_odds is None or not (config.MIN_ODDS <= relevant_live_odds <= config.MAX_ODDS):
                                if relevant_live_odds is None:
                                    no_signal_reasons.append(f"S2: {dir_word} odds is None")
                                else:
                                    no_signal_reasons.append(f"S2: {dir_word} odds {relevant_live_odds:.2f} outside [{config.MIN_ODDS}-{config.MAX_ODDS}]")
                                continue
                                    
                                # FILTER D: REMOVED — too strict, was blocking legitimate picks
                                # The Alg.1 rating is already a strong signal; requiring odds movement
                                # on top of that was too restrictive and caused the bot to find no picks
                                # from 70+ games. The odds range filter (1.55-2.20) still protects quality.

                                # FILTER E: Soccer Over — block goal-induced line movements
                                if sport_id == config.SPORT_SOCCER and dir_word == "Over":
                                    total_goals = latest_odds_home + latest_odds_away
                                    if total_goals > 0 and opening_line is not None and live_line is not None:
                                        line_diff_val = live_line - opening_line
                                        if line_diff_val >= total_goals * 0.5 and line_diff_val <= total_goals + 0.25:
                                            log(f"[SKIP] Soccer Over blocked: goal-induced line movement (line {opening_line}→{live_line}, goals={total_goals})")
                                            continue
                                        if live_line > 0 and (total_goals / live_line) >= 0.5:
                                            log(f"[SKIP] Soccer Over blocked: goals-to-line ratio too high (goals={total_goals}, line={live_line}, ratio={total_goals/live_line:.2f})")
                                            continue
                                    if live_line is not None and live_line > 4.50:
                                        log(f"[SKIP] Soccer Over blocked: line too high ({live_line} > 4.50)")
                                        continue

                                    # FILTER F: Soccer Over — block weak halftime Over picks
                                    if total_goals <= 1 and match_minute >= 40 and match_minute <= 55:
                                        log(f"[SKIP] Soccer Over blocked: halftime with ≤1 goal (goals={total_goals}, minute={match_minute}, need 2+ more goals in 2nd half)")
                                        continue

                                # FILTER C: Score Feasibility — current score must be on pace for the bet
                                if live_line is not None and live_line > 0:
                                    try:
                                        current_total = latest_odds_home + latest_odds_away
                                        pace_ratio = current_total / live_line
                                        if dir_word == "Over":
                                            if sport_id == config.SPORT_BASKETBALL and pace_ratio < 0.60:
                                                no_signal_reasons.append(f"S2: pace too low ({pace_ratio:.0%} < 60%)")
                                                continue
                                            if sport_id == config.SPORT_SOCCER:
                                                safe_minute = max(1, match_minute)
                                                projected_goals = (current_total / safe_minute) * 90
                                                if projected_goals < live_line * 0.75:
                                                    no_signal_reasons.append(f"S2: projected {projected_goals:.1f} < {live_line*0.75:.1f} (75% of line)")
                                                    continue
                                        elif dir_word == "Under":
                                            if sport_id == config.SPORT_BASKETBALL and pace_ratio > 0.55:
                                                no_signal_reasons.append(f"S2: pace too high for Under ({pace_ratio:.0%} > 55%)")
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
                                    "alg_dir": f"{dir_word}",
                                    "reason": f"Alg.1 Rating deviation detected: {rating_val:+.2f}."
                                })

            # --- STRATEGY 3: Abnormal Line Dynamics (Soccer Halftime) ---
            # IMPORTANT: Use latest_odds_home/latest_odds_away (from the latest odds entry),
            # not home_score/away_score from the game list.
            # The game list is fetched before the odds data, so a goal can be scored
            # between the two fetches. Using the stale game list score causes false
            # anomalies (e.g., game list says 0-0 but odds data shows 1-0 after a goal).

            if sport_id == config.SPORT_SOCCER and market_name == "Total" and latest_odds_home == 0 and latest_odds_away == 0:
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
                    opening_line = earliest_live.get("row2") or first_prematch.get("row2")
                    live_line = latest_live.get("row2")
                    if opening_line is not None and live_line is not None and opening_line > 0:
                        expected_ht_line = opening_line / 2.0
                        if live_line >= (expected_ht_line + config.HT_ABNORMAL_LINE_GAP_THRESHOLD):
                            # Ensure live Over odds are within acceptable range
                            live_over = latest_live.get("row1")
                            if live_over is not None and config.MIN_ODDS <= live_over <= config.MAX_ODDS:
                                # Log score verification for debugging
                                if latest_odds_home != home_score or latest_odds_away != away_score:
                                    log(f"[STRAT3] Score verified from odds data: {latest_odds_home}-{latest_odds_away} (game list had {home_score}-{away_score})")
                                predictions.append({
                                    "market": market_name,
                                    "prediction": f"Over {live_line}",
                                    "confidence": 85,
                                    "total_dir": "Over",
                                    "total_line": f"{live_line}",
                                    "open_line": f"{opening_line}",
                                    "now_line": f"{live_line}",
                                    "line_diff": f"{live_line - opening_line:+.2f}",
                                    "open_over": f"{earliest_live.get('row1', 0):.2f}" if earliest_live.get('row1') else "N/A",
                                    "now_over": f"{live_over:.2f}",
                                    "open_under": f"{earliest_live.get('row3', 0):.2f}" if earliest_live.get('row3') else "N/A",
                                    "now_under": f"{latest_live.get('row3', 0):.2f}" if latest_live.get('row3') else "N/A",
                                    "alg_val": "Anomaly",
                                    "alg_dir": "Over",
                                    "reason": f"0-0 at HT, but line is abnormally high ({live_line} vs expected {expected_ht_line})."
                                })

            # --- STRATEGY 4: Sharp Under (Line is BELOW expected time-adjusted line) ---
            # When the live Total line is significantly below the expected line at the current minute,
            # the market is signaling fewer goals than the time-adjusted expectation.
            # KEY FIX: Compare live line to EXPECTED line (accounting for time decay), not opening line.
            # Normal time decay: line at 58' with 0-0 should be ~1.07 from opening 3.00.
            # A "sharp" signal is when the live line is 0.5+ BELOW the expected line.
            # Expected line = current_goals + (opening_line * (90 - minute) / 90)
            if sport_id == config.SPORT_SOCCER and market_name == "Total" and not soccer_late_game:
                opening_line_s4 = earliest_live.get("row2") or first_prematch.get("row2")
                live_line_s4 = latest_live.get("row2")
                live_under_s4 = latest_live.get("row3")
                live_over_s4 = latest_live.get("row1")
                if (opening_line_s4 is not None and live_line_s4 is not None
                        and live_under_s4 is not None
                        and config.MIN_ODDS <= live_under_s4 <= config.MAX_ODDS
                        and opening_line_s4 > 0):
                    total_goals_s4 = latest_odds_home + latest_odds_away
                    # Calculate expected line at current minute (time-adjusted)
                    # Expected remaining goals = opening_line * (90 - minute) / 90
                    # Expected live line = current_goals + expected_remaining_goals
                    try:
                        s4_minute = int(match.get("time", {}).get("tm", 0))
                    except (ValueError, TypeError):
                        s4_minute = 0
                    if 10 <= s4_minute <= 74:  # Only in meaningful game time (not too early, not too late)
                        expected_remaining = opening_line_s4 * (90 - s4_minute) / 90.0
                        expected_line = total_goals_s4 + expected_remaining
                        # The "sharp" signal: live line is 0.5+ below expected
                        line_gap = expected_line - live_line_s4
                        if line_gap >= 0.5:
                            # Also check: not goal-induced — goals must be less than the gap
                            if total_goals_s4 < line_gap:
                                # Check we haven't already picked this from Strategy 2
                                already_picked = any(p.get("total_dir") == "Under" and p.get("total_line") == f"{live_line_s4}" for p in predictions)
                                if not already_picked:
                                    predictions.append({
                                        "market": market_name,
                                        "prediction": f"Under {live_line_s4}",
                                        "confidence": 88,
                                        "total_dir": "Under",
                                        "total_line": f"{live_line_s4}",
                                        "open_line": f"{opening_line_s4}",
                                        "now_line": f"{live_line_s4}",
                                        "line_diff": f"{live_line_s4 - opening_line_s4:+.2f}",
                                        "open_over": f"{earliest_live.get('row1', 0):.2f}" if earliest_live.get('row1') else "N/A",
                                        "now_over": f"{live_over_s4:.2f}" if live_over_s4 else "N/A",
                                        "open_under": f"{earliest_live.get('row3', 0):.2f}" if earliest_live.get('row3') else "N/A",
                                        "now_under": f"{live_under_s4:.2f}",
                                        "alg_val": "Sharp_Under",
                                        "alg_dir": "Under",
                                        "reason": f"Line {live_line_s4} is {line_gap:.2f} below expected {expected_line:.2f} at {s4_minute}' (opening {opening_line_s4}, goals {total_goals_s4}). Market expects fewer goals."
                                    })

            # --- STRATEGY 5: Momentum Over (Line rises significantly early in game) ---
            # When the Total line rises 0.5+ during early live play and the score is still 0-0,
            # the market is signaling MORE goals than expected. This is a strong Over signal.
            # Key: only trigger when no goals have been scored yet (not goal-induced).
            if sport_id == config.SPORT_SOCCER and market_name == "Total" and not soccer_late_game:
                opening_line_s5 = earliest_live.get("row2") or first_prematch.get("row2")
                live_line_s5 = latest_live.get("row2")
                live_over_s5 = latest_live.get("row1")
                live_under_s5 = latest_live.get("row3")
                if (opening_line_s5 is not None and live_line_s5 is not None
                        and live_over_s5 is not None
                        and config.MIN_ODDS <= live_over_s5 <= config.MAX_ODDS):
                    line_rise_s5 = live_line_s5 - opening_line_s5
                    if line_rise_s5 >= 0.5:
                        # Check: not goal-induced — no goals scored yet
                        total_goals_s5 = latest_odds_home + latest_odds_away
                        if total_goals_s5 == 0:
                            # Only trigger in first half (0-45 min)
                            try:
                                s5_min = int(match.get("time", {}).get("tm", 0))
                            except (ValueError, TypeError):
                                s5_min = 0
                            if 0 < s5_min <= 45:
                                # Check we haven't already picked this from Strategy 2
                                already_picked = any(p.get("total_dir") == "Over" and p.get("total_line") == f"{live_line_s5}" for p in predictions)
                                if not already_picked:
                                    predictions.append({
                                        "market": market_name,
                                        "prediction": f"Over {live_line_s5}",
                                        "confidence": 87,
                                        "total_dir": "Over",
                                        "total_line": f"{live_line_s5}",
                                        "open_line": f"{opening_line_s5}",
                                        "now_line": f"{live_line_s5}",
                                        "line_diff": f"{line_rise_s5:+.2f}",
                                        "open_over": f"{earliest_live.get('row1', 0):.2f}" if earliest_live.get('row1') else "N/A",
                                        "now_over": f"{live_over_s5:.2f}",
                                        "open_under": f"{earliest_live.get('row3', 0):.2f}" if earliest_live.get('row3') else "N/A",
                                        "now_under": f"{live_under_s5:.2f}" if live_under_s5 else "N/A",
                                        "alg_val": "Momentum_Over",
                                        "alg_dir": "Over",
                                        "reason": f"Line rose sharply from {opening_line_s5} to {live_line_s5} (+{line_rise_s5:.2f}) with 0 goals scored. Market expects more goals."
                                    })

            # --- STRATEGY 6: Slow Pace Under (0-0 at 30-70' with line >= 2.0) ---
            # When the game is 0-0 deep into the match and the total line is still 2.0+,
            # the market is pricing in goals that haven't materialized. The Under is a strong
            # value play — the game has shown no scoring ability for 30+ minutes.
            # Key: only trigger when NO goals have been scored (0-0 score).
            if sport_id == config.SPORT_SOCCER and market_name == "Total" and not soccer_late_game:
                opening_line_s6 = earliest_live.get("row2") or first_prematch.get("row2")
                live_line_s6 = latest_live.get("row2")
                live_under_s6 = latest_live.get("row3")
                live_over_s6 = latest_live.get("row1")
                # Only trigger when score is 0-0
                if latest_odds_home == 0 and latest_odds_away == 0:
                    try:
                        s6_minute = int(match.get("time", {}).get("tm", 0))
                    except (ValueError, TypeError):
                        s6_minute = 0
                    if 30 <= s6_minute <= 70:
                        if (live_line_s6 is not None and live_line_s6 >= 2.0
                                and live_under_s6 is not None
                                and config.MIN_ODDS <= live_under_s6 <= config.MAX_ODDS):
                            # Check we haven't already picked this from Strategy 2 or 4
                            already_picked = any(p.get("total_dir") == "Under" and p.get("total_line") == f"{live_line_s6}" for p in predictions)
                            if not already_picked:
                                predictions.append({
                                    "market": market_name,
                                    "prediction": f"Under {live_line_s6}",
                                    "confidence": 86,
                                    "total_dir": "Under",
                                    "total_line": f"{live_line_s6}",
                                    "open_line": f"{opening_line_s6}" if opening_line_s6 is not None else "N/A",
                                    "now_line": f"{live_line_s6}",
                                    "line_diff": f"{live_line_s6 - opening_line_s6:+.2f}" if opening_line_s6 is not None else "N/A",
                                    "open_over": f"{earliest_live.get('row1', 0):.2f}" if earliest_live.get('row1') else "N/A",
                                    "now_over": f"{live_over_s6:.2f}" if live_over_s6 else "N/A",
                                    "open_under": f"{earliest_live.get('row3', 0):.2f}" if earliest_live.get('row3') else "N/A",
                                    "now_under": f"{live_under_s6:.2f}",
                                    "alg_val": "Slow_Pace",
                                    "alg_dir": "Under",
                                    "reason": f"0-0 at {s6_minute}' with line {live_line_s6}. No scoring for 30+ minutes — market overpricing goals."
                                })

            # --- STRATEGY 7: Line Movement (No rating needed — pure market signal) ---
            # When the Total line moves significantly during live play, the market is
            # signaling a shift in expectations. This is a strong signal that doesn't
            # depend on the Alg.1 rating data.
            # Over signal: line rises 0.5+ (market expects more goals) — only if no goals scored yet
            # Under signal: line drops 0.25+ (market expects fewer goals) — only if no goals scored yet
            # This catches games where the rating is weak or missing but the line movement is clear.
            if sport_id == config.SPORT_SOCCER and market_name == "Total" and not soccer_late_game:
                s7_opening_line = earliest_live.get("row2") or first_prematch.get("row2")
                s7_live_line = latest_live.get("row2")
                s7_live_over = latest_live.get("row1")
                s7_live_under = latest_live.get("row3")
                if s7_opening_line is not None and s7_live_line is not None and s7_opening_line > 0:
                    s7_line_diff = s7_live_line - s7_opening_line
                    s7_total_goals = latest_odds_home + latest_odds_away
                    try:
                        s7_minute = int(match.get("time", {}).get("tm", 0))
                    except (ValueError, TypeError):
                        s7_minute = 0
                    if 5 <= s7_minute <= 74:  # Not too early, not too late
                        # Under signal: line dropped 0.25+ and no goals scored yet
                        # Market is pricing in fewer goals — the Under is a value play
                        if s7_line_diff <= -0.25 and s7_total_goals == 0:
                            if s7_live_under is not None and config.MIN_ODDS <= s7_live_under <= config.MAX_ODDS:
                                already_picked = any(p.get("total_dir") == "Under" and p.get("total_line") == f"{s7_live_line}" for p in predictions)
                                if not already_picked:
                                    predictions.append({
                                        "market": market_name,
                                        "prediction": f"Under {s7_live_line}",
                                        "confidence": 82,
                                        "total_dir": "Under",
                                        "total_line": f"{s7_live_line}",
                                        "open_line": f"{s7_opening_line}",
                                        "now_line": f"{s7_live_line}",
                                        "line_diff": f"{s7_line_diff:+.2f}",
                                        "open_over": f"{earliest_live.get('row1', 0):.2f}" if earliest_live.get('row1') else "N/A",
                                        "now_over": f"{s7_live_over:.2f}" if s7_live_over else "N/A",
                                        "open_under": f"{earliest_live.get('row3', 0):.2f}" if earliest_live.get('row3') else "N/A",
                                        "now_under": f"{s7_live_under:.2f}",
                                        "alg_val": "Line_Move",
                                        "alg_dir": "Under",
                                        "reason": f"Line dropped {s7_opening_line}→{s7_live_line} ({s7_line_diff:+.2f}) with 0 goals at {s7_minute}'. Market expects fewer goals."
                                    })
                        # Over signal: line rose 0.5+ and no goals scored yet (not goal-induced)
                        # Market is pricing in more goals — the Over is a value play
                        elif s7_line_diff >= 0.5 and s7_total_goals == 0:
                            if s7_live_over is not None and config.MIN_ODDS <= s7_live_over <= config.MAX_ODDS:
                                already_picked = any(p.get("total_dir") == "Over" and p.get("total_line") == f"{s7_live_line}" for p in predictions)
                                if not already_picked:
                                    predictions.append({
                                        "market": market_name,
                                        "prediction": f"Over {s7_live_line}",
                                        "confidence": 82,
                                        "total_dir": "Over",
                                        "total_line": f"{s7_live_line}",
                                        "open_line": f"{s7_opening_line}",
                                        "now_line": f"{s7_live_line}",
                                        "line_diff": f"{s7_line_diff:+.2f}",
                                        "open_over": f"{s7_live_over:.2f}",
                                        "now_over": f"{s7_live_over:.2f}",
                                        "open_under": f"{earliest_live.get('row3', 0):.2f}" if earliest_live.get('row3') else "N/A",
                                        "now_under": f"{s7_live_under:.2f}" if s7_live_under else "N/A",
                                        "alg_val": "Line_Move",
                                        "alg_dir": "Over",
                                        "reason": f"Line rose {s7_opening_line}→{s7_live_line} (+{s7_line_diff:.2f}) with 0 goals at {s7_minute}'. Market expects more goals."
                                    })

        if not no_signal_reasons:
            no_signal_reasons.append("no strategy triggered")
        return predictions, no_signal_reasons


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
    
    # 1X2 evaluation removed — bot only picks Total Over/Under
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

    # Market details — Total Over/Under only
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

    # Total Over/Under settlement
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
    
    # Consecutive failure counter — auto-restart if API is unreachable for too long
    # When consecutive_failures >= threshold, bot exits and Render restarts it with a fresh IP
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 5  # 5 cycles × 60s = ~5 minutes of total failure → restart

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
                        # API says Cancelled/Abandoned — but the game might have actually finished.
                        # The API is UNRELIABLE for lower leagues (Slovenia, Venezuela, etc.) and marks
                        # many finished games as "Cancelled". We can't trust the API's timeStatus alone.
                        # FIX: Use game time + score data to determine if the game actually finished.
                        # Logic: If the game has a valid score AND the game time shows it was past halftime,
                        # it almost certainly finished — the API is just wrong.
                        # A genuinely cancelled game would be cancelled early (before halftime) or pre-match.
                        game_actually_finished = False

                        # Check 1: Game time from game_view data
                        # If the game was past 80', it's almost certainly finished
                        if game_view:
                            gv_time = game_view.get("time", {})
                            try:
                                gv_min = int(gv_time.get("tm", 0))
                            except (ValueError, TypeError):
                                gv_min = 0
                            # If game time shows 80'+, game definitely finished
                            if gv_min >= 80:
                                game_actually_finished = True
                                log(f"[VOID-CHECK] API says {void_reason} but game time={gv_min}'. Game clearly finished. Overriding void.")

                        # Check 2: Score data — if the game has goals, it was likely played
                        # A genuinely cancelled game rarely has goals scored
                        # Only override if we haven't already determined it finished from Check 1
                        if not game_actually_finished and scores:
                            try:
                                if "-" in str(scores):
                                    sp = str(scores).split("-")
                                    h = int(sp[0])
                                    a = int(sp[1])
                                    total = h + a
                                    # If 2+ total goals scored, the game was likely played to completion
                                    # A game abandoned at 60' with 1-0 would only have 1 goal
                                    # But a game with 1-3 or 1-1 (2+ goals) almost certainly finished
                                    if total >= 2:
                                        game_actually_finished = True
                                        log(f"[VOID-CHECK] API says {void_reason} but score={scores} ({total} goals). Game likely finished. Overriding void.")
                            except Exception:
                                pass

                        # Check 3: Also check finished_games list (API might correct itself later)
                        if not game_actually_finished and not lock_state.get("void_checked_finished"):
                            log(f"[VOID-CHECK] API says {void_reason} for {home} vs {away}. Checking finished_games list...")
                            finished = client.get_finished_games(sport_id)
                            for fin_game in finished:
                                if str(fin_game.get("id")) == str(event_id):
                                    fin_status = str(fin_game.get("timeStatus", ""))
                                    fin_scores = fin_game.get("scores", "0-0")
                                    log(f"[VOID-CHECK] Found event {event_id} in finished_games. timeStatus={fin_status}, score={fin_scores}")
                                    if fin_status == "2":  # Finished!
                                        game_actually_finished = True
                                        scores = fin_scores
                                        log(f"[VOID-CHECK] Game was actually FINISHED in finished_games! Score: {fin_scores}.")
                                    break
                            lock_state["void_checked_finished"] = True
                            save_lock_state(lock_state)

                        if game_actually_finished:
                            # Override: game actually finished, evaluate the result
                            is_void = False
                            log(f"[VOID-OVERRIDE] Game {home} vs {away} was actually FINISHED. Score: {scores}. Evaluating result instead of voiding.")
                            # Fall through to normal settlement below

                        if is_void:
                            # Confirmed void — game was genuinely cancelled/abandoned early
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

                    if not is_void:
                        # Normal settlement — game finished (either directly or after void-override)
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
                any_games_found = False
                for sport_id in [config.SPORT_SOCCER, config.SPORT_BASKETBALL]:
                    sport_name = "Soccer" if sport_id == config.SPORT_SOCCER else "Basketball"
                    live_games = client.get_live_games(sport_id)
                    
                    # Track scan stats for this sport
                    scan_total = len(live_games)
                    scan_prefiltered = 0
                    scan_cached = 0
                    scan_no_odds = 0
                    scan_no_signal = 0
                    scan_blocked = 0
                    scan_picks = 0
                    scan_no_signal_reasons = {}  # Track WHY no signal was generated
                    
                    if live_games:
                        any_games_found = True
                    
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
                        
                        # Pre-filter: skip games that can't trigger any strategy
                        # This saves time when there are 100+ live games per sport
                        time_info = game.get("time", {})
                        scores = game.get("scores", "0-0")
                        
                        if sport_id == config.SPORT_SOCCER:
                            try:
                                tm_val = int(time_info.get("tm", 0))
                            except (ValueError, TypeError):
                                tm_val = 0
                            # Skip games past 75' — FILTER A blocks all soccer totals after 75'
                            # No strategy triggers after 75' (Strategy 4 caps at 74', Strategy 6 caps at 70')
                            if tm_val >= 75:
                                scan_prefiltered += 1
                                continue
                            # Skip games before 2' — not enough data for any strategy
                            # Soccer Rule 1 needs 0-10' but the game needs at least a couple minutes
                            # to have a valid score and line movement
                            if tm_val < 2 and str(time_info.get("tm", "")) != "HT":
                                scan_prefiltered += 1
                                continue
                            # Skip games with 4+ total goals — almost no strategy triggers
                            # and the Over line would be too high (filtered by line cap)
                            try:
                                if "-" in str(scores):
                                    sp = str(scores).split("-")
                                    total_goals = int(sp[0]) + int(sp[1])
                                    if total_goals >= 4:
                                        scan_prefiltered += 1
                                        continue
                            except Exception:
                                pass
                        elif sport_id == config.SPORT_BASKETBALL:
                            # Prevent betting on games in the absolute final moments (API ghost lines)
                            q_str = str(time_info.get("q", ""))
                            tm_str = str(time_info.get("tm", ""))
                            if str(time_info.get("tt", "")) == "":
                                scan_prefiltered += 1
                                continue
                            if q_str == "4" and tm_str in ["0", "1"]:
                                scan_prefiltered += 1
                                continue
                        
                        state_key = f"{scores}_{json.dumps(time_info)}"
                        
                        if match_cache.get(event_id) == state_key:
                            scan_cached += 1
                            continue
                        
                        match_cache[event_id] = state_key
                        
                        odds_data = client.get_game_odds(sport_id, event_id)
                        time.sleep(0.3)  # Throttle to prevent IP ban (reduced from 0.8s — direct-first means faster connections)
                        if not odds_data:
                            scan_no_odds += 1
                            continue
                        
                        predictions, no_signal_reasons = PredictionEngine.analyze_match(sport_id, game, odds_data)
                        if not predictions:
                            scan_no_signal += 1
                            # Log the top reason for no signal (helps debug why bot isn't picking)
                            home_name = game.get("home", {}).get("name", "?")
                            away_name = game.get("away", {}).get("name", "?")
                            top_reasons = no_signal_reasons[:3] if no_signal_reasons else ["unknown"]
                            log(f"[NOSIG] {home_name} vs {away_name}: {' | '.join(top_reasons)}")
                            # Track reasons for scan summary
                            for r in no_signal_reasons:
                                # Group by prefix (S2, early, S3, etc.)
                                prefix = r.split(":")[0] if ":" in r else r
                                scan_no_signal_reasons[prefix] = scan_no_signal_reasons.get(prefix, 0) + 1
                            continue
                        
                        # Count how many were blocked by filters vs made it through
                        scan_blocked += len(predictions) - 1 if len(predictions) > 1 else 0
                        scan_picks += 1
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
                                    "opening_val": pred.get("open_line"),
                                    "live_val": pred.get("now_line"),
                                    "reason": pred["reason"]
                                }
                                save_lock_state(new_lock)
                                log(f"Bot is successfully locked on event {event_id}.")
                                break
                                
                            time.sleep(1.0)

                    # Scan summary for this sport
                    analyzed = scan_total - scan_prefiltered - scan_cached
                    if scan_total > 0:
                        reasons_str = ""
                        if scan_no_signal > 0 and scan_no_signal_reasons:
                            # Show top 3 reasons sorted by frequency
                            top_reasons = sorted(scan_no_signal_reasons.items(), key=lambda x: -x[1])[:3]
                            reasons_str = " | top reasons: " + ", ".join(f"{k}({v})" for k, v in top_reasons)
                        log(f"[SCAN] {sport_name}: {scan_total} total | {scan_prefiltered} filtered | {scan_cached} cached | {analyzed} analyzed | {scan_no_odds} no odds | {scan_no_signal} no signal{reasons_str} | {scan_picks} picks")
                    else:
                        log(f"[SCAN] {sport_name}: 0 live games")

            # Prevent cache growing too large
            if len(match_cache) > config.MAX_CACHE_SIZE:
                match_cache.clear()
            
            # --- AUTO-RESTART LOGIC ---
            # If we found games or are locked, reset the failure counter
            lock_state = load_lock_state()
            if lock_state.get("locked") or any_games_found:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                log(f"[CONNECT] No games found. Consecutive failures: {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log(f"[RESTART] {consecutive_failures} consecutive failures. Exiting to trigger Render auto-restart with fresh IP...", error=True)
                    time.sleep(5)  # Give logs time to flush
                    sys.exit(1)  # Render will auto-restart the process
                
        except Exception as e:
            log(f"Error in main loop: {e}\n{traceback.format_exc()}", error=True)
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log(f"[RESTART] {consecutive_failures} consecutive failures (including errors). Exiting for auto-restart...", error=True)
                sys.exit(1)
            
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

