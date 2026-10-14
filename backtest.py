"""
Backtest: descarga partidos históricos con xG y odds,
y mide la precisión del score predictivo vs. resultado HT real.
"""

import os
import sys
import time
import json
import requests
from datetime import date, timedelta
from collections import defaultdict

API_KEY = os.environ["THESTATSAPI_KEY"]
BASE = "https://api.thestatsapi.com/api"
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}

# Importar funciones de main
from main import get, implied_prob, remove_vig, score_over_05_ht


def fetch_page(endpoint: str, **params) -> tuple[list, int]:
    """GET paginado, devuelve (data, total_pages)."""
    resp = requests.get(f"{BASE}{endpoint}", headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    return body.get("data", []), body.get("meta", {}).get("total_pages", 1)


def backtest_range(date_from: str, date_to: str, competition_id: str = None, max_matches: int = 200):
    """
    Backtest sobre un rango de fechas.
    Descarga partidos terminados, obtiene stats + odds,
    y compara el score con el resultado HT real.
    """
    print(f"\n{'='*80}")
    print(f"  BACKTEST: {date_from} → {date_to}")
    if competition_id:
        print(f"  Competición: {competition_id}")
    print(f"  Máx. partidos: {max_matches}")
    print(f"{'='*80}\n")

    # 1) Obtener lista de partidos
    all_matches = []
    page = 1
    total_pages = 1

    while page <= total_pages and len(all_matches) < max_matches:
        params = {"date_from": date_from, "date_to": date_to, "status": "finished",
                  "per_page": 50, "page": page}
        if competition_id:
            params["competition_id"] = competition_id

        try:
            data, total_pages = fetch_page("/football/matches", **params)
        except Exception as e:
            print(f"  ⚠️ Error paginación: {e}")
            break

        if isinstance(data, dict):
            data = data.get("matches", [])

        all_matches.extend(data)
        print(f"  Página {page}/{total_pages} — acumulado: {len(all_matches)}")
        page += 1
        time.sleep(0.5)

    all_matches = [m for m in all_matches if m.get("xg_available")]
    print(f"\n  Partidos con xG: {len(all_matches)}\n")

    # 2) Procesar cada partido
    results = []
    for i, m in enumerate(all_matches):
        mid = m.get("id")
        if not mid:
            continue

        name = f"{m.get('home_team',{}).get('name','?')} vs {m.get('away_team',{}).get('name','?')}"
        ht_home = m.get("score", {}).get("half_time_home", 0) or 0
        ht_away = m.get("score", {}).get("half_time_away", 0) or 0
        ht_actual = ht_home + ht_away  # 0, 1, 2, 3...

        try:
            stats = get(f"/football/matches/{mid}/stats")
            odds = get(f"/football/matches/{mid}/odds")
            score = score_over_05_ht(m, stats, odds)

            results.append({
                "match_id": mid,
                "name": name,
                "ht_goals": ht_actual,
                "over_05_ht": ht_actual >= 1,
                "predicted_score": score["score"],
                "verdict": score["verdict"],
                "total_xg": score["total_xg"],
            })

            if (i + 1) % 20 == 0:
                print(f"  Progreso: {i+1}/{len(all_matches)}")

        except Exception as e:
            pass

        time.sleep(0.3)

    if not results:
        print("  No se obtuvieron resultados.")
        return

    # 3) Métricas
    total = len(results)
    actual_over = sum(1 for r in results if r["over_05_ht"])
    predicted_high = [r for r in results if "ALTA" in r["verdict"]]
    predicted_medium = [r for r in results if "MEDIA" in r["verdict"]]
    predicted_low = [r for r in results if "BAJA" in r["verdict"]]

    high_hits = sum(1 for r in predicted_high if r["over_05_ht"])
    med_hits = sum(1 for r in predicted_medium if r["over_05_ht"])
    low_hits = sum(1 for r in predicted_low if r["over_05_ht"])

    print(f"\n{'='*80}")
    print(f"  RESULTADOS DEL BACKTEST ({total} partidos)")
    print(f"{'='*80}")
    print(f"\n  Over 0.5 HT real: {actual_over}/{total} ({actual_over/total:.1%})")
    print(f"\n  {'Categoría':<12} {'Partidos':>9} {'Hits':>6} {'Precisión':>10}")
    print(f"  {'-'*40}")
    print(f"  {'🟢 ALTA':<12} {len(predicted_high):>9} {high_hits:>6} "
          f"{(high_hits/len(predicted_high) if predicted_high else 0):>9.1%}")
    print(f"  {'🟡 MEDIA':<12} {len(predicted_medium):>9} {med_hits:>6} "
          f"{(med_hits/len(predicted_medium) if predicted_medium else 0):>9.1%}")
    print(f"  {'🔴 BAJA':<12} {len(predicted_low):>9} {low_hits:>6} "
          f"{(low_hits/len(predicted_low) if predicted_low else 0):>9.1%}")

    # Precision / Recall para ALTA
    if predicted_high:
        precision = high_hits / len(predicted_high)
        recall = high_hits / actual_over if actual_over else 0
        print(f"\n  Precision (ALTA): {precision:.1%}")
        print(f"  Recall (ALTA):    {recall:.1%}")

    # Guardar resultados
    output_file = "backtest_results.json"
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  📁 Resultados guardados en: {output_file}")


def main():
    # Rango por defecto: últimos 30 días
    date_to = date.today().isoformat()
    date_from = (date.today() - timedelta(days=30)).isoformat()

    # Overridable por CLI
    if len(sys.argv) >= 3:
        date_from, date_to = sys.argv[1], sys.argv[2]

    comp = os.environ.get("COMPETITION_ID", "")
    max_matches = int(os.environ.get("MAX_MATCHES", "200"))

    backtest_range(date_from, date_to, comp or None, max_matches)


if __name__ == "__main__":
    main()   
