import sys
import time
from datetime import datetime
import config
from prediction_bot import InforadarAPIClient, PredictionEngine

def parse_final_score(score_str):
    try:
        if "-" in score_str:
            parts = score_str.split("-")
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return None, None

def evaluate_prediction(pred_info, final_home_score, final_away_score):
    prediction = pred_info.get("prediction", "")
    total_goals = final_home_score + final_away_score

    # 1X2 Evaluation — prediction is now "1" or "2"
    if prediction == "1":
        return "WIN" if final_home_score > final_away_score else "LOSS"
    elif prediction == "2":
        return "WIN" if final_away_score > final_home_score else "LOSS"

    # Total Over/Under Evaluation — "Over 2.5" or "Under 2.5"
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

def run_backtest():
    print("=" * 60)
    print("      SOOBRADAR PREDICTION BOT SIMULATION / BACKTESTER      ")
    print("=" * 60)
    
    client = InforadarAPIClient()
    total_predictions = 0
    wins = 0
    losses = 0
    draws = 0
    unknowns = 0
    
    for sport_id in [config.SPORT_SOCCER, config.SPORT_BASKETBALL]:
        sport_name = "Soccer" if sport_id == config.SPORT_SOCCER else "Basketball"
        sport_emoji = "Soccer" if sport_id == config.SPORT_SOCCER else "Basketball"
        print(f"\n[*] Fetching finished {sport_name} games...")
        finished_games = client.get_finished_games(sport_id)[:10]
        
        print(f"[*] Found {len(finished_games)} finished {sport_name} games. Starting simulation...")
        
        for idx, game in enumerate(finished_games):
            event_id = game.get("id")
            home_team = game.get("home", {}).get("name", "Home")
            away_team = game.get("away", {}).get("name", "Away")
            final_scores = game.get("scores", "0-0")
            
            final_home, final_away = parse_final_score(final_scores)
            if final_home is None or final_away is None:
                continue
                
            # Fetch historical odds
            odds_data = client.get_game_odds(sport_id, event_id)
            if not odds_data:
                continue
                
            # Get predictions
            predictions = PredictionEngine.analyze_match(sport_id, game, odds_data)
            
            if predictions:
                print(f"\n  -> Match: {home_team} vs {away_team} | Final Score: {final_scores}")
                for pred in predictions:
                    result = evaluate_prediction(pred, final_home, final_away)
                    total_predictions += 1
                    if result == "WIN":
                        wins += 1
                        status_char = "WIN"
                    elif result == "LOSS":
                        losses += 1
                        status_char = "LOSS"
                    elif result == "DRAW":
                        draws += 1
                        status_char = "DRAW"
                    else:
                        unknowns += 1
                        status_char = "UNKNOWN"
                        
                    pred_label = pred["prediction"]
                    if pred["market"] == "1X2":
                        detail = f"1X2 -> {pred_label} ({pred.get('confidence', 0)}%)"
                    else:
                        detail = f"Total {pred_label} ({pred.get('confidence', 0)}%)"
                        
                    print(f"     Market: {pred['market']:<8} | Prediction: {detail:<30} | Result: {status_char}")
                    print(f"     Reason: {pred.get('reason', '')}")
            
            # Throttling to prevent API ban during fast local run
            time.sleep(1.5)

    print("\n" + "=" * 60)
    print("                    SIMULATION RESULTS                     ")
    print("=" * 60)
    print(f"Total Predictions Generated: {total_predictions}")
    print(f"Wins:                        {wins}")
    print(f"Losses:                      {losses}")
    print(f"Draws:                       {draws}")
    print(f"Unknowns:                    {unknowns}")
    
    resolved = wins + losses
    if resolved > 0:
        win_rate = (wins / resolved) * 100.0
        print(f"Win Rate (Resolved):         {win_rate:.1f}%")
    else:
        print("Win Rate (Resolved):         N/A")
    print("=" * 60)

if __name__ == "__main__":
    run_backtest()
