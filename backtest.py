"""
SlamRadar Backtest v2 — Full Historical Odds Scanner
Scans EVERY historical odds entry for each finished game to find
the peak Alg.1 signal and any 1X2 odds drops that would have triggered,
then validates against the final score.
"""
import sys
import os
import time
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import config
except ImportError:
    import config_example as config
from prediction_bot import InforadarAPIClient

# ─── Configurable thresholds to test ───────────────────────────────────────
ALG1_THRESHOLD     = config.MIN_ALG1_RATING_THRESHOLD
ODDS_DROP_PCT      = config.ODDS_DROP_THRESHOLD_PCT
MIN_ODDS           = config.MIN_ODDS
MAX_ODDS           = config.MAX_ODDS
SOCCER_LATE_CUTOFF = 75    # Ignore Soccer Total triggers after this minute
# ────────────────────────────────────────────────────────────────────────────

def parse_score(ss):
    """Parse score — handles both string '1-2' and list [1,2] formats from API."""
    try:
        if isinstance(ss, list) and len(ss) >= 2:
            return int(ss[0]), int(ss[1])
        if ss and "-" in str(ss):
            parts = str(ss).split("-")
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return None, None

def parse_quarter_and_seconds(game_time):
    """
    Parse quarter and remaining seconds from game_time string.
    Formats seen: '4 - 03:14', '1 - 00:00', '37' (soccer minute)
    Returns (quarter, seconds_remaining) or (None, None)
    """
    try:
        s = str(game_time).strip()
        if " - " in s:
            parts = s.split(" - ")
            q = int(parts[0].strip())
            time_part = parts[1].strip()
            if ":" in time_part:
                m, sec = time_part.split(":")
                try:
                    total_secs = int(m) * 60 + int(sec)
                except ValueError:
                    total_secs = 0
            else:
                total_secs = 0
            return q, total_secs
        else:
            # Soccer minute
            return None, int(s)
    except Exception:
        pass
    return None, None

def drop_pct(opening, live):
    if not opening or not live or opening <= 0:
        return 0.0
    return ((opening - live) / opening) * 100.0

def evaluate(prediction, final_home, final_away, sport_id):
    total = final_home + final_away
    if prediction.startswith("1X2_1"):
        return "WIN" if final_home > final_away else "LOSS"
    if prediction.startswith("1X2_2"):
        return "WIN" if final_away > final_home else "LOSS"
    if prediction.startswith("Over"):
        line = float(prediction.split("Over ")[1])
        if total > line: return "WIN"
        if total == line: return "PUSH"
        return "LOSS"
    if prediction.startswith("Under"):
        line = float(prediction.split("Under ")[1])
        if total < line: return "WIN"
        if total == line: return "PUSH"
        return "LOSS"
    return "UNKNOWN"

def is_feasible(dir_word, live_line, current_total, sport_id, quarter, soccer_minute):
    """
    Returns True if the bet is realistically achievable given current score and time.
    Filters out mathematically unlikely bets.
    """
    if live_line is None or live_line <= 0:
        return True  # can't determine, allow through

    pace_ratio = current_total / live_line

    if sport_id == config.SPORT_BASKETBALL:
        # Block Q4 entirely for Totals (too late, too volatile)
        if quarter is not None and quarter >= 4:
            return False
        if dir_word == "Over":
            # Must already be at 60%+ of the line to be on track
            if pace_ratio < 0.60:
                return False
        elif dir_word == "Under":
            # Must not already be burning through the line too fast (>70%)
            if pace_ratio > 0.70:
                return False

    elif sport_id == config.SPORT_SOCCER:
        # Block after 75th minute
        if soccer_minute is not None and soccer_minute >= 75:
            return False
        if dir_word == "Over" and soccer_minute is not None and soccer_minute > 0:
            # Projected total at 90' must be >= 80% of line
            projected = (current_total / soccer_minute) * 90
            if projected < live_line * 0.80:
                return False

    return True

def scan_game(sport_id, game, odds_markets):
    """
    Scan all historical odds entries in a finished game.
    Returns a list of triggered predictions with their result context.
    """
    triggers = []
    final_score  = game.get("scores", "0-0")
    final_home, final_away = parse_score(final_score)
    sport_name = "Soccer" if sport_id == config.SPORT_SOCCER else "Basketball"

    for market in odds_markets:
        name      = market.get("name", "")
        odds_list = market.get("odds", [])
        fp        = market.get("firstPrematch", {})
        if not odds_list or not fp:
            continue
            
    # --- MULTI-MARKET / EARLY LIVE STRATEGIES (New Rules) ---
    parsed_markets = {}
    for market in odds_markets:
        m_name = market.get("name", "")
        m_odds = market.get("odds", [])
        m_first = market.get("firstPrematch", {})
        if m_odds and isinstance(m_odds, list):
            # For backtesting, we need to find an entry in the first 10 minutes
            early_live = None
            for entry in reversed(m_odds):  # Iterate from start of match
                gm = entry.get("game_time", 0)
                if sport_id == config.SPORT_SOCCER:
                    try:
                        tm_val = int(str(gm))
                        if 0 <= tm_val <= 10:
                            early_live = entry
                            break
                    except:
                        pass
                elif sport_id == config.SPORT_BASKETBALL:
                    q, tm = parse_quarter_and_seconds(gm)
                    if q == 1:
                        early_live = entry
                        break
            if not early_live:
                early_live = m_odds[-1]  # fallback to earliest available
                
            parsed_markets[m_name] = {
                "first": m_first,
                "early_live": early_live
            }

    m_1x2 = parsed_markets.get("1X2", {})
    p_1x2 = m_1x2.get("first", {})
    l_1x2 = m_1x2.get("early_live", {})
    
    m_total = parsed_markets.get("Total", {})
    p_tot = m_total.get("first", {})
    l_tot = m_total.get("early_live", {})
    
    m_ah = parsed_markets.get("Handicap", {}) or parsed_markets.get("Asian Handicap", {})
    p_ah = m_ah.get("first", {})
    l_ah = m_ah.get("early_live", {})

    open_home = p_1x2.get("row1")
    open_away = p_1x2.get("row3")

    if open_home and open_away:
        if sport_id == config.SPORT_SOCCER:
            if 1.60 <= open_home <= 3.50 and 1.60 <= open_away <= 3.50:
                open_line = p_tot.get("row2")
                live_line = l_tot.get("row2")
                if open_line in [3.25, 3.50] and live_line is not None:
                    if live_line == open_line - 0.25:
                        triggers.append({
                            "sport": sport_name, "market": "Total", "prediction": f"Under {live_line}",
                            "label": f"Soccer Rule 1 (Competitive Under): Line dropped to {live_line}",
                            "minute": "Early", "live_score": "0-0",
                            "final_score": final_score, "final_home": final_home, "final_away": final_away,
                        })
            # Soccer Rule 2 (Blowout Over) and Rule 3 (Stale Line Over) REMOVED — poor backtest performance
        
        elif sport_id == config.SPORT_BASKETBALL:
            if open_home <= 1.40 or open_away <= 1.40:
                fav_pred = "1X2_1" if open_home <= 1.40 else "1X2_2"
                triggers.append({
                    "sport": sport_name, "market": "1X2", "prediction": fav_pred,
                    "label": f"Basketball Rule 1 (Heavy Favorite Lock): Odds <= 1.40",
                    "minute": "Early", "live_score": "0-0",
                    "final_score": final_score, "final_home": final_home, "final_away": final_away,
                })
            
            if 1.50 <= open_home <= 2.50 and 1.50 <= open_away <= 2.50:
                open_ah = p_ah.get("row2")
                live_ah = l_ah.get("row2")
                if open_ah is not None and live_ah is not None:
                    ah_diff = live_ah - open_ah
                    if abs(ah_diff) >= 1.0:
                        fav_pred = "1X2_1" if ah_diff <= -1.0 else "1X2_2"
                        triggers.append({
                            "sport": sport_name, "market": "1X2", "prediction": fav_pred,
                            "label": f"Basketball Rule 2 (Sharp Favorite Surge): Spread moved {ah_diff}",
                            "minute": "Early", "live_score": "0-0",
                            "final_score": final_score, "final_home": final_home, "final_away": final_away,
                        })

    for market in odds_markets:
        name      = market.get("name", "")
        odds_list = market.get("odds", [])
        fp        = market.get("firstPrematch", {})
        if not odds_list or not fp:
            continue

        open_r1 = fp.get("row1")
        open_r3 = fp.get("row3")

        # ── 1X2 strategy ────────────────────────────────────────────────
        if name == "1X2":
            for entry in odds_list:
                ss = entry.get("ss", "")
                h, a = parse_score(ss)
                if h is None or h != a:
                    continue  # only fire while tied

                gm = entry.get("game_time", 0) or 0
                live_1 = entry.get("row1")
                live_2 = entry.get("row3")

                # Home win drop
                if open_r1 and live_1 and MIN_ODDS <= live_1 <= MAX_ODDS:
                    d = drop_pct(open_r1, live_1)
                    if d >= ODDS_DROP_PCT:
                        triggers.append({
                            "sport": sport_name, "market": "1X2",
                            "prediction": "1X2_1",
                            "label": f"Home Win (odds {open_r1:.2f}→{live_1:.2f}, -{d:.1f}%)",
                            "minute": gm, "live_score": ss,
                            "final_score": final_score,
                            "final_home": final_home, "final_away": final_away,
                        })
                        break

                # Away win drop
                if open_r3 and live_2 and MIN_ODDS <= live_2 <= MAX_ODDS:
                    d = drop_pct(open_r3, live_2)
                    if d >= ODDS_DROP_PCT:
                        triggers.append({
                            "sport": sport_name, "market": "1X2",
                            "prediction": "1X2_2",
                            "label": f"Away Win (odds {open_r3:.2f}→{live_2:.2f}, -{d:.1f}%)",
                            "minute": gm, "live_score": ss,
                            "final_score": final_score,
                            "final_home": final_home, "final_away": final_away,
                        })
                        break

        # ── Total strategy: find peak Alg.1 rating ───────────────────────
        if name == "Total":
            peak_rating = 0.0
            peak_entry  = None
            for entry in odds_list:
                ratings = entry.get("rating", [])
                if not ratings or not isinstance(ratings, list):
                    continue
                for r in ratings:
                    if isinstance(r, dict) and r.get("rating") is not None:
                        val = abs(r["rating"])
                        if val > peak_rating:
                            peak_rating = val
                            peak_entry = dict(entry)
                            peak_entry["_alg_val"] = r["rating"]
                            peak_entry["_dir"]     = r.get("direction")

            if peak_rating >= ALG1_THRESHOLD and peak_entry is not None:
                gm       = peak_entry.get("game_time", 0)
                ss       = peak_entry.get("ss", "")
                alg_val  = peak_entry["_alg_val"]
                direction = peak_entry["_dir"]
                live_line = peak_entry.get("row2")
                live_over = peak_entry.get("row1")
                live_under = peak_entry.get("row3")

                dir_word = direction if direction else ("Over" if alg_val > 0 else "Under")
                    
                live_odds = live_over if dir_word == "Over" else live_under
                opening_odds = fp.get("row1") if dir_word == "Over" else fp.get("row3")

                if not (live_odds and MIN_ODDS <= live_odds <= MAX_ODDS):
                    continue
                    
                # FILTER D: Require Odds Movement
                if opening_odds is not None and live_odds >= opening_odds:
                    continue

                # Parse time info
                quarter, time_val = parse_quarter_and_seconds(gm)
                soccer_minute = time_val if sport_id == config.SPORT_SOCCER else None

                # Parse current score at trigger moment
                h_now, a_now = parse_score(ss)
                current_total = (h_now + a_now) if h_now is not None and a_now is not None else 0

                # Apply feasibility filter
                if not is_feasible(dir_word, live_line, current_total, sport_id, quarter, soccer_minute):
                    continue

                triggers.append({
                    "sport": sport_name, "market": "Total",
                    "prediction": f"{dir_word} {live_line}",
                    "label": f"Alg.1={alg_val:+.3f} at {gm} (line {fp.get('row2')}→{live_line})",
                    "minute": gm, "live_score": ss,
                    "final_score": final_score,
                    "final_home": final_home, "final_away": final_away,
                })

        # ── Strategy 3: Abnormal Line Dynamics (Soccer Halftime) ───────────────────────
        if sport_id == config.SPORT_SOCCER and name == "Total":
            for entry in odds_list:
                gm = entry.get("game_time", 0) or 0
                ss = entry.get("ss", "")
                
                # Check score is 0-0
                h_now, a_now = parse_score(ss)
                if h_now != 0 or a_now != 0:
                    continue
                
                # Parse time
                quarter, time_val = parse_quarter_and_seconds(gm)
                is_ht = False
                if str(gm).upper() == "HT":
                    is_ht = True
                elif time_val is not None and 40 <= time_val <= 55:
                    is_ht = True
                
                if is_ht:
                    opening_line = fp.get("row2")
                    live_line = entry.get("row2")
                    if opening_line is not None and live_line is not None and opening_line > 0:
                        expected_ht_line = opening_line / 2.0
                        if live_line >= (expected_ht_line + config.HT_ABNORMAL_LINE_GAP_THRESHOLD):
                            live_over = entry.get("row1")
                            if live_over is not None and MIN_ODDS <= live_over <= MAX_ODDS:
                                triggers.append({
                                    "sport": sport_name, "market": "Total",
                                    "prediction": f"Over {live_line}",
                                    "label": f"Anomaly at HT (line {live_line} vs expected {expected_ht_line})",
                                    "minute": gm, "live_score": ss,
                                    "final_score": final_score,
                                    "final_home": final_home, "final_away": final_away,
                                })
                                break

    return triggers

def run():
    print("=" * 65)
    print("   SlamRadar Backtest v2 — Full Historical Odds Scanner")
    print(f"   Alg.1 threshold: {ALG1_THRESHOLD} | 1X2 drop: {ODDS_DROP_PCT}% | Late cutoff: {SOCCER_LATE_CUTOFF}'")
    print("=" * 65)

    client = InforadarAPIClient()
    all_triggers = []

    for sport_id in [config.SPORT_SOCCER, config.SPORT_BASKETBALL]:
        sport_name = "Soccer" if sport_id == config.SPORT_SOCCER else "Basketball"
        print(f"\n[*] Fetching finished {sport_name} games...")
        games = client.get_finished_games(sport_id)[:20]
        print(f"[*] Scanning {len(games)} finished {sport_name} games...")

        for game in games:
            event_id  = game.get("id")
            home_name = game.get("home", {}).get("name", "?")
            away_name = game.get("away", {}).get("name", "?")

            odds_data = client.get_game_odds(sport_id, event_id)
            if not odds_data:
                time.sleep(0.5)
                continue

            markets = odds_data if isinstance(odds_data, list) else odds_data.get("results", [])
            triggers = scan_game(sport_id, game, markets)

            if triggers:
                for t in triggers:
                    t["match"] = f"{home_name} vs {away_name}"
                    all_triggers.append(t)

            time.sleep(1.0)

    # ─── Results ────────────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("   TRIGGERED PREDICTIONS")
    print("=" * 65)

    wins = losses = pushes = unknowns = 0

    for t in all_triggers:
        if t["final_home"] is None or t["final_away"] is None:
            result = "UNKNOWN"
        else:
            result = evaluate(t["prediction"], t["final_home"], t["final_away"], t["sport"])

        icon = {"WIN": "✅", "LOSS": "❌", "PUSH": "➡️", "UNKNOWN": "❓"}.get(result, "❓")
        if result == "WIN":    wins += 1
        elif result == "LOSS": losses += 1
        elif result == "PUSH": pushes += 1
        else:                  unknowns += 1

        print(f"\n  {icon} [{t['sport']}] {t['match']}")
        print(f"     Market    : {t['market']} — {t['prediction']}")
        print(f"     Signal    : {t['label']}  @ {t['minute']}' (score {t['live_score']})")
        print(f"     Final     : {t['final_score']}  → {result}")

    resolved = wins + losses
    hit_rate = (wins / resolved * 100) if resolved > 0 else 0

    print("\n" + "=" * 65)
    print("   SUMMARY")
    print("=" * 65)
    print(f"  Total triggers  : {len(all_triggers)}")
    print(f"  Wins            : {wins}")
    print(f"  Losses          : {losses}")
    print(f"  Pushes          : {pushes}")
    print(f"  Unknowns        : {unknowns}")
    print(f"  Hit Rate        : {hit_rate:.1f}%  ({wins}W / {losses}L)")
    print("=" * 65)

if __name__ == "__main__":
    run()
