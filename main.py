"""
Over 0.5 HT Scanner + Telegram Notification
"""

import os
import time
import json
import requests
from datetime import date

API_KEY = os.environ["THESTATSAPI_KEY"]
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE = "https://api.thestatsapi.com/api"
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}


# ──────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────
def send_telegram(text: str):
    """Envía mensaje HTML a Telegram."""
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️  Telegram no configurado (faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID)")
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            print("✅ Mensaje enviado a Telegram")
        else:
            print(f"❌ Telegram error {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"❌ Telegram error: {e}")


# ──────────────────────────────────────────────
# API
# ──────────────────────────────────────────────
def get(endpoint: str, **params) -> dict:
    for attempt in range(3):
        try:
            resp = requests.get(f"{BASE}{endpoint}", headers=HEADERS, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json().get("data", {})
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                wait = int(e.response.headers.get("Retry-After", 5))
                time.sleep(wait)
            elif attempt < 2:
                time.sleep(2 ** attempt)
            else:
                raise
    return {}


def implied_prob(odds: float) -> float:
    return 1 / odds if odds and odds > 1 else 0.0


# ──────────────────────────────────────────────
# SCORING
# ──────────────────────────────────────────────
def score_match(match: dict, stats: dict, odds: dict) -> dict:
    h_xg = float(stats.get("home", {}).get("xg", 0) or 0)
    a_xg = float(stats.get("away", {}).get("xg", 0) or 0)
    total_xg = h_xg + a_xg

    h_poss = float(stats.get("home", {}).get("possession", 50) or 50)
    poss_diff = abs(h_poss - (100 - h_poss))

    h_sot = int(stats.get("home", {}).get("shots_on_target", 0) or 0)
    a_sot = int(stats.get("away", {}).get("shots_on_target", 0) or 0)
    total_sot = h_sot + a_sot

    ou_odds = None
    for bm in odds.get("bookmakers", []):
        tg = bm.get("markets", {}).get("total_goals", {})
        if tg.get("over"):
            ou_odds = float(tg["over"].get("last_seen") or tg["over"].get("opening") or 0)
            break

    xg_score = min(total_xg / 2.5, 1.0) * 40
    poss_score = min(poss_diff / 25, 1.0) * 15
    sot_score = min(total_sot / 12, 1.0) * 15
    odds_score = max(0, (implied_prob(ou_odds) - 0.45) / 0.30) * 30 if ou_odds else 0

    score = round(xg_score + poss_score + sot_score + odds_score, 1)

    if score >= 65:
        verdict = "🟢"
    elif score >= 45:
        verdict = "🟡"
    else:
        verdict = "🔴"

    return {
        "total_xg": round(total_xg, 2),
        "possession": f"{int(h_poss)}-{int(100 - h_poss)}",
        "total_sot": total_sot,
        "score": score,
        "verdict": verdict,
    }


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    today = date.today().isoformat()
    print(f"📅 Escaneando partidos del {today}...")

    # 1) Obtener todos los match_ids del día
    all_matches = []
    page = 1
    while True:
        data = get("/football/matches", date_from=today, date_to=today, per_page=100, page=page)
        if isinstance(data, dict):
            data = data.get("matches", [])
        if not data:
            break
        all_matches.extend(data)
        meta = get("/football/matches", date_from=today, date_to=today, per_page=100, page=page)
        total_pages = 1  # simplificado
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.5)

    # Filtrar: solo con xG + odds
    valid = [m for m in all_matches if m.get("xg_available") and m.get("odds_available")]
    print(f"Total: {len(all_matches)} | Con datos: {len(valid)}")

    # 2) Procesar
    results = []
    for m in valid:
        mid = m["id"]
        name = f"{m['home_team']['name']} vs {m['away_team']['name']}"

        try:
            stats = get(f"/football/matches/{mid}/stats")
            odds = get(f"/football/matches/{mid}/odds")
            r = score_match(m, stats, odds)

            # HT real si terminó
            ht = ""
            if m.get("status") == "finished":
                ht_h = m.get("score", {}).get("half_time_home", 0) or 0
                ht_a = m.get("score", {}).get("half_time_away", 0) or 0
                hit = "✅" if (ht_h + ht_a) >= 1 else "❌"
                ht = f" HT {ht_h}-{ht_a} {hit}"

            r["name"] = name
            r["ht"] = ht
            results.append(r)
            print(f"{r['verdict']} {name:<45} xG={r['total_xg']:.2f} SOT={r['total_sot']} Score={r['score']:>5}{ht}")
        except Exception as e:
            print(f"⚠️ {name}: {e}")

        time.sleep(0.3)

    # 3) Enviar a Telegram
    if results:
        high = [r for r in results if r["verdict"] == "🟢"]
        medium = [r for r in results if r["verdict"] == "🟡"]
        low = [r for r in results if r["verdict"] == "🔴"]

        # Construir mensaje HTML
        lines = [f"⚽ <b>OVER 0.5 HT — {today}</b>", ""]

        if high:
            lines.append(f"🟢 <b>ALTA ({len(high)})</b>")
            for r in high:
                ht_part = f" → {r['ht'].strip()}" if r["ht"] else ""
                lines.append(f"  {r['name']} | xG {r['total_xg']} | SOT {r['total_sot']} | <b>{r['score']}</b>{ht_part}")
            lines.append("")

        if medium:
            lines.append(f"🟡 <b>MEDIA ({len(medium)})</b>")
            for r in medium:
                ht_part = f" → {r['ht'].strip()}" if r["ht"] else ""
                lines.append(f"  {r['name']} | xG {r['total_xg']} | SOT {r['total_sot']} | {r['score']}{ht_part}")
            lines.append("")

        if low:
            lines.append(f"🔴 <b>BAJA ({len(low)})</b>")
            for r in low[:5]:  # máx 5 para no saturar
                lines.append(f"  {r['name']} | xG {r['total_xg']} | {r['score']}")
            if len(low) > 5:
                lines.append(f"  ... y {len(low)-5} más")

        lines.append("")
        lines.append(f"Total: {len(results)} partidos analizados")

        message = "\n".join(lines)
        send_telegram(message)

        # También guardar JSON (para artifact)
        with open("scan_results.json", "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()   
