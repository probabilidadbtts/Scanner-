"""
Over 0.5 HT Scanner + Telegram Notification

Importante:
- No se usa llms.txt como fuente de IDs. Ese archivo es documentación, no un feed de datos.
- La fuente real de datos es TheStats API: /football/matches + /football/matches/{id}/stats + /football/matches/{id}/odds.
- Se filtran partidos con mercado de primera parte "Over 0.5" real, no rellenos sintéticos.
"""

import os
import re
import time
import json
from datetime import date

import requests

API_KEY = os.environ.get("THESTATSAPI_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE = "https://api.thestatsapi.com/api"
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"} if API_KEY else {"Accept": "application/json"}


# ──────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────
def send_telegram(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram no configurado (faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID)")
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
    except Exception as exc:
        print(f"❌ Telegram error: {exc}")


# ──────────────────────────────────────────────
# API
# ──────────────────────────────────────────────
def get(endpoint: str, **params) -> dict:
    """GET a TheStats API. Devuelve body.get('data', body). No usa llms.txt como feed."""
    for attempt in range(3):
        try:
            resp = requests.get(f"{BASE}{endpoint}", headers=HEADERS, params=params, timeout=30)
            resp.raise_for_status()
            body = resp.json()
            return body.get("data", body)
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                wait = int(exc.response.headers.get("Retry-After", 5))
                time.sleep(wait)
            elif attempt < 2:
                time.sleep(2 ** attempt)
            else:
                raise
    return {}


# Mantenemos compatibilidad con backtest.py

def implied_prob(odds: float) -> float:
    return 1 / odds if odds and odds > 1 else 0.0


def remove_vig(odds_a: float, odds_b: float):
    try:
        p_a = 1 / odds_a if odds_a and odds_a > 0 else 0
        p_b = 1 / odds_b if odds_b and odds_b > 0 else 0
        s = p_a + p_b
        if s <= 0 or s == 1:
            return odds_a, odds_b
        return 1 / (p_a / s), 1 / (p_b / s)
    except Exception:
        return odds_a, odds_b


# ──────────────────────────────────────────────
# ODDS PARSING: mercado real de first-half goals / 0.5
# ──────────────────────────────────────────────
def _iter_dicts(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _iter_dicts(v)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dicts(item)


def extract_ht_over_05_odds(odds: dict) -> float:
    """Busca el precio de Over 0.5 en primera parte en cualquier estructura real de odds.
    No debe depender de llms.txt ni de IDs sintéticos.
    """
    if not isinstance(odds, dict):
        return None

    best = None

    for obj in _iter_dicts(odds):
        if not isinstance(obj, dict):
            continue

        market_name = str(obj.get("name") or obj.get("key") or obj.get("market") or obj.get("type") or "").lower()
        market_period = str(obj.get("period") or obj.get("half") or obj.get("segment") or "").lower()
        combined = f"{market_name} {market_period}"

        # Pistas para detectar mercado de primera parte / total goals / 0.5
        is_ht_market = any(token in combined for token in ["first half", "first_half", "half time", "ht", "1h", "halftime"])
        is_goals_market = any(token in combined for token in ["goals", "goal", "total", "totals"])
        has_half_line = any(token in combined for token in ["0.5", "0.50", "over 0.5", "over0.5"])

        if not (is_ht_market and is_goals_market) and not has_half_line:
            continue

        # Extraer precio verdadero desde over / odds / price / last_seen
        for key in ("over", "Over", "OVER"):
            cand = obj.get(key)
            if isinstance(cand, dict):
                price = cand.get("last_seen") or cand.get("opening") or cand.get("price") or cand.get("odds")
            elif isinstance(cand, (int, float)):
                price = cand
            else:
                price = None

            if price is not None:
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    price = None

            if price and (best is None or price > best):
                best = price

        # A veces el precio está en 'outcomes' / 'selection' / 'lines'
        for nested_key in ("outcomes", "lines", "selections", "markets"):
            nested = obj.get(nested_key)
            if isinstance(nested, list):
                for item in nested:
                    if not isinstance(item, dict):
                        continue
                    label = str(item.get("label") or item.get("name") or item.get("selection") or "").lower()
                    line = str(item.get("line") or item.get("value") or "")
                    if "over" in label or "0.5" in label or "0.5" in line:
                        price = item.get("price") or item.get("odds") or item.get("last_seen") or item.get("opening")
                        try:
                            price = float(price)
                        except (TypeError, ValueError):
                            price = None
                        if price and (best is None or price > best):
                            best = price

    return best


# Compatibilidad con backtest.py

def score_over_05_ht(match: dict, stats: dict, odds: dict) -> dict:
    ht_odds = extract_ht_over_05_odds(odds) if odds else None
    return score_match(match, stats, odds, ht_odds)


# ──────────────────────────────────────────────
# SCORING
# ──────────────────────────────────────────────
def score_match(match: dict, stats: dict, odds: dict, ht_over_odds: float = None) -> dict:
    stats_home = stats.get("home", {}) if isinstance(stats, dict) else {}
    stats_away = stats.get("away", {}) if isinstance(stats, dict) else {}

    h_xg = float(stats_home.get("xg", 0) or 0)
    a_xg = float(stats_away.get("xg", 0) or 0)
    total_xg = h_xg + a_xg

    h_poss = float(stats_home.get("possession", 50) or 50)
    poss_diff = abs(h_poss - (100 - h_poss))

    h_sot = int(stats_home.get("shots_on_target", 0) or 0)
    a_sot = int(stats_away.get("shots_on_target", 0) or 0)
    total_sot = h_sot + a_sot

    xg_score = min(total_xg / 2.5, 1.0) * 40
    poss_score = min(poss_diff / 25, 1.0) * 15
    sot_score = min(total_sot / 12, 1.0) * 15

    odds_score = 0
    if ht_over_odds:
        odds_score = max(0, (implied_prob(ht_over_odds) - 0.45) / 0.30) * 30

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

    all_matches = []
    page = 1
    while True:
        data = get("/football/matches", date_from=today, date_to=today, per_page=100, page=page)
        if isinstance(data, dict):
            matches = data.get("matches") or []
        elif isinstance(data, list):
            matches = data
        else:
            matches = []

        if not matches:
            break

        all_matches.extend(matches)
        page += 1
        time.sleep(0.4)

    print(f"Total partidos por fecha: {len(all_matches)}")

    results = []
    for m in all_matches:
        mid = m.get("id")
        if not mid:
            continue

        name = f"{m.get('home_team', {}).get('name', '?')} vs {m.get('away_team', {}).get('name', '?')}"

        try:
            stats = get(f"/football/matches/{mid}/stats") or {}
            odds = get(f"/football/matches/{mid}/odds") or {}

            ht_odds = extract_ht_over_05_odds(odds)
            if not ht_odds:
                # no hay mercado real HT Over 0.5 -> no lo enviamos
                continue

            score = score_match(m, stats, odds, ht_odds)

            ht = ""
            if m.get("status") == "finished":
                ht_h = m.get("score", {}).get("half_time_home", 0) or 0
                ht_a = m.get("score", {}).get("half_time_away", 0) or 0
                hit = "✅" if (ht_h + ht_a) >= 1 else "❌"
                ht = f" HT {ht_h}-{ht_a} {hit}"

            score["name"] = name
            score["ht"] = ht
            score["ht_over_odds"] = ht_odds
            results.append(score)
            print(f"{score['verdict']} {name:<45} xG={score['total_xg']:.2f} SOT={score['total_sot']} Score={score['score']:>5} HTodds={ht_odds}{ht}")
        except Exception as exc:
            print(f"⚠️ {name}: {exc}")

        time.sleep(0.25)

    if not results:
        print("ℹ️ No se encontraron partidos con mercado real de Over 0.5 HT.")
        return

    high = [r for r in results if r["verdict"] == "🟢"]
    medium = [r for r in results if r["verdict"] == "🟡"]
    low = [r for r in results if r["verdict"] == "🔴"]

    lines = [f"⚽ <b>OVER 0.5 HT — {today}</b>", ""]

    def fmt(r):
        ht_part = f" → {r['ht'].strip()}" if r.get("ht") else ""
        return f"  {r['name']} | xG {r['total_xg']} | SOT {r['total_sot']} | <b>{r['score']}</b> | HT odds {r.get('ht_over_odds')}{ht_part}"

    if high:
        lines.append(f"🟢 <b>ALTA ({len(high)})</b>")
        for r in high:
            lines.append(fmt(r))
        lines.append("")

    if medium:
        lines.append(f"🟡 <b>MEDIA ({len(medium)})</b>")
        for r in medium:
            lines.append(fmt(r))
        lines.append("")

    if low:
        lines.append(f"🔴 <b>BAJA ({len(low)})</b>")
        for r in low[:5]:
            lines.append(f"  {r['name']} | xG {r['total_xg']} | SOT {r['total_sot']} | {r['score']}")
        if len(low) > 5:
            lines.append(f"  ... y {len(low)-5} más")

    lines.append("")
    lines.append(f"Total: {len(results)} partidos analizados")

    message = "\n".join(lines)
    send_telegram(message)

    with open("scan_results.json", "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
