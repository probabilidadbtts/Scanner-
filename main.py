"""
Over 0.5 HT Scanner
Escanea partidos del día, obtiene xG, posesión y cuotas,
y estima la probabilidad de Over 0.5 goles en primera parte.
"""

import os
import sys
import time
import requests
from datetime import date, datetime, timedelta

API_KEY = os.environ["THESTATSAPI_KEY"]
BASE = "https://api.thestatsapi.com/api"
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}


def get(endpoint: str, **params) -> dict:
    """GET con retry."""
    for attempt in range(3):
        try:
            resp = requests.get(f"{BASE}{endpoint}", headers=HEADERS, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json().get("data", {})
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                wait = int(e.response.headers.get("Retry-After", 5))
                print(f"  ⏳ Rate limit, esperando {wait}s...")
                time.sleep(wait)
            elif attempt < 2:
                time.sleep(2 ** attempt)
            else:
                raise
    return {}


def implied_prob(decimal_odds: float) -> float:
    return 1 / decimal_odds if decimal_odds and decimal_odds > 1 else 0.0


def remove_vig(probs: list[float]) -> list[float]:
    """Elimina el margen del bookmaker."""
    total = sum(probs)
    if total == 0:
        return probs
    return [p / total for p in probs]


def fetch_match_detail(match_id: str) -> dict:
    """Detalle del partido (incluye score HT)."""
    return get(f"/football/matches/{match_id}")


def fetch_match_stats(match_id: str) -> dict:
    """xG, posesión, tiros."""
    return get(f"/football/matches/{match_id}/stats")


def fetch_match_odds(match_id: str) -> dict:
    """Cuotas pre-match (Bet365, Pinnacle, Betfair, Kambi)."""
    return get(f"/football/matches/{match_id}/odds")


def score_over_05_ht(match: dict, stats: dict, odds: dict) -> dict:
    """
    Heurística combinada para estimar P(Over 0.5 HT).
    
    Componentes:
    - xG total (peso 40%)
    - Posesión dominante (peso 15%)
    - Tiros al arco (peso 15%)
    - Cuota Over 2.5 FT como proxy (peso 30%)
    """
    # --- Stats ---
    home_xg = float(stats.get("home", {}).get("xg", 0) or 0)
    away_xg = float(stats.get("away", {}).get("xg", 0) or 0)
    total_xg = home_xg + away_xg

    home_poss = float(stats.get("home", {}).get("possession", 50) or 50)
    away_poss = float(stats.get("away", {}).get("possession", 50) or 50)

    home_sot = int(stats.get("home", {}).get("shots_on_target", 0) or 0)
    away_sot = int(stats.get("away", {}).get("shots_on_target", 0) or 0)
    total_sot = home_sot + away_sot

    # --- Odds: Over 2.5 FT como proxy ---
    ou_over_odds = None
    ou_under_odds = None
    for bm in odds.get("bookmakers", []):
        tg = bm.get("markets", {}).get("total_goals", {})
        if tg.get("over") and tg.get("under"):
            ou_over_odds = float(tg["over"].get("last_seen") or tg["over"].get("opening") or 0)
            ou_under_odds = float(tg["under"].get("last_seen") or tg["under"].get("opening") or 0)
            break

    ou_over_implied = implied_prob(ou_over_odds) if ou_over_odds else None
    ou_under_implied = implied_prob(ou_under_odds) if ou_under_odds else None

    # No-vig fair probability para Over
    ou_fair_over = None
    if ou_over_implied and ou_under_implied:
        fair = remove_vig([ou_over_implied, ou_under_implied])
        ou_fair_over = fair[0]

    # --- Score compuesto (0-100) ---
    # 1) xG: >2.0 total → muy probable gol en HT
    xg_score = min(total_xg / 2.5, 1.0) * 40

    # 2) Posesión: diferencia >10% sugiere dominio ofensivo
    poss_diff = abs(home_poss - away_poss)
    poss_score = min(poss_diff / 25, 1.0) * 15

    # 3) Tiros al arco: >8 SOT totales → alto volumen ofensivo
    sot_score = min(total_sot / 12, 1.0) * 15

    # 4) Cuota Over 2.5: si fair prob > 55%, HT también es probable
    odds_score = 0
    if ou_fair_over:
        odds_score = max(0, (ou_fair_over - 0.45) / 0.30) * 30

    total_score = xg_score + poss_score + sot_score + odds_score

    # --- Veredicto ---
    if total_score >= 65:
        verdict = "🟢 ALTA"
    elif total_score >= 45:
        verdict = "🟡 MEDIA"
    else:
        verdict = "🔴 BAJA"

    return {
        "total_xg": round(total_xg, 2),
        "possession": f"{int(home_poss)}-{int(away_poss)}",
        "total_sot": total_sot,
        "ou25_fair": f"{ou_fair_over:.1%}" if ou_fair_over else "N/A",
        "score": round(total_score, 1),
        "verdict": verdict,
    }


def scan_date(target_date: str, competition_id: str = None):
    """Escanea todos los partidos de una fecha."""
    params = {"date_from": target_date, "date_to": target_date}
    if competition_id:
        params["competition_id"] = competition_id

    matches = get("/football/matches", **params)
    if isinstance(matches, dict):
        matches = matches.get("matches", [])

    if not matches:
        print(f"No se encontraron partidos para {target_date}")
        return []

    print(f"\n{'='*100}")
    print(f"  OVER 0.5 HT SCANNER — {target_date} — {len(matches)} partidos")
    print(f"{'='*100}\n")
    print(f"{'Partido':<42} {'xG':>5} {'Pos':>9} {'SOT':>4} {'O/U2.5':>8} {'Score':>6}  Verdict")
    print(f"{'-'*100}")

    results = []
    for i, m in enumerate(matches):
        mid = m.get("id")
        if not mid:
            continue

        name = f"{m.get('home_team',{}).get('name','?')} vs {m.get('away_team',{}).get('name','?')}"

        # Solo partidos con xG disponible
        if not m.get("xg_available", False):
            print(f"{name:<42} {'—':>5} {'—':>9} {'—':>4} {'—':>8} {'—':>6}  ⚠️ sin xG")
            continue

        try:
            stats = fetch_match_stats(mid)
            odds = fetch_match_odds(mid)
            result = score_over_05_ht(m, stats, odds)

            # Si el partido ya terminó, mostrar resultado HT real
            ht_home = m.get("score", {}).get("half_time_home", 0) or 0
            ht_away = m.get("score", {}).get("half_time_away", 0) or 0
            ht_total = ht_home + ht_away
            ht_actual = f"HT {ht_home}-{ht_away}" if m.get("status") == "finished" else ""

            print(f"{name:<42} {result['total_xg']:>5} {result['possession']:>9} "
                  f"{result['total_sot']:>4} {result['ou25_fair']:>8} "
                  f"{result['score']:>6}  {result['verdict']}  {ht_actual}")

            result["match_id"] = mid
            result["name"] = name
            result["ht_actual"] = ht_total
            result["status"] = m.get("status", "unknown")
            results.append(result)

        except Exception as e:
            print(f"{name:<42} ERROR: {e}")

        time.sleep(0.3)  # rate limit

    return results


def main():
    today = date.today().isoformat()
    comp = os.environ.get("COMPETITION_ID", "")  # opcional: comp_3039, comp_3033, etc.

    results = scan_date(today, comp or None)

    # Resumen
    if results:
        high = [r for r in results if "ALTA" in r["verdict"]]
        medium = [r for r in results if "MEDIA" in r["verdict"]]
        low = [r for r in results if "BAJA" in r["verdict"]]

        print(f"\n{'='*100}")
        print(f"  RESUMEN: 🟢 {len(high)} | 🟡 {len(medium)} | 🔴 {len(low)}")
        print(f"{'='*100}")

        if high:
            print("\n  🟢 ALTA PROBABILIDAD:")
            for r in high:
                print(f"    • {r['name']} (score: {r['score']}, xG: {r['total_xg']})")


if __name__ == "__main__":
    main()   
