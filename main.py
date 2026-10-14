"""
Over 0.5 HT Scanner + Telegram Notification
Mejoras:
- Intenta leer https://api.thestatsapi.com/llms.txt (lista de partidos en vivo) y extraer match_ids.
- Extrae correctamente odds de PRIMERA PARTE (HT Over 0.5) buscando en los mercados de los bookmakers.
- Filtra y envía solo partidos donde exista mercado HT Over 0.5 y calcula score usando xG, posesión y SOT.
- Añade funciones auxiliares `score_over_05_ht` y `remove_vig` para compatibilidad con backtest.py
"""

import os
import time
import json
import re
import requests
from datetime import date

API_KEY = os.environ.get("THESTATSAPI_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE = "https://api.thestatsapi.com/api"
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"} if API_KEY else {"Accept": "application/json"}


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
    """Llamada GET al API (retorna body.get('data', body)). Conservadora para compatibilidad.
    endpoint debe empezar por '/'.
    """
    for attempt in range(3):
        try:
            resp = requests.get(f"{BASE}{endpoint}", headers=HEADERS, params=params, timeout=30)
            resp.raise_for_status()
            body = resp.json()
            return body.get("data", body)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                wait = int(e.response.headers.get("Retry-After", 5))
                time.sleep(wait)
            elif attempt < 2:
                time.sleep(2 ** attempt)
            else:
                raise
    return {}


def fetch_llms_txt(url: str = "https://api.thestatsapi.com/llms.txt") -> list:
    """Intenta descargar llms.txt y extraer posibles match_ids (heurística)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            # Try without headers
            resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            print(f"⚠️ llms.txt no disponible ({resp.status_code})")
            return []

        lines = resp.text.splitlines()
        ids = set()
        for L in lines:
            # Extraer números largos (4+ dígitos) como candidate ids
            for m in re.findall(r"\b\d{4,}\b", L):
                ids.add(int(m))
        return sorted(ids)
    except Exception as e:
        print(f"⚠️ Error descargando llms.txt: {e}")
        return []


def implied_prob(odds: float) -> float:
    return 1 / odds if odds and odds > 1 else 0.0


def remove_vig(odds_a: float, odds_b: float) -> tuple[float, float]:
    """Intento simple de remover vigorish en mercado binario: normaliza probabilidades y retorna odds ajustadas.
    Si no es aplicable devuelve los mismos odds.
    """
    try:
        p_a = 1 / odds_a if odds_a and odds_a > 0 else 0
        p_b = 1 / odds_b if odds_b and odds_b > 0 else 0
        s = p_a + p_b
        if s <= 0 or s == 1:
            return odds_a, odds_b
        na = (p_a / s)
        nb = (p_b / s)
        return (1 / na if na > 0 else odds_a), (1 / nb if nb > 0 else odds_b)
    except Exception:
        return odds_a, odds_b


# ──────────────────────────────────────────────
# ODDS PARSING
# ──────────────────────────────────────────────

def extract_ht_over_05_odds(odds: dict) -> float:
    """Busca en la estructura de odds el mercado 'First Half Over 0.5' y devuelve la mejor cuota 'over'.
    Devuelve None si no se encuentra.

    La función es robusta: maneja formatos donde `bookmakers` es una lista y `markets` puede ser dict o lista.
    Busca keywords comunes como 'first', 'ht', 'half' y '1h' y una linea 0.5 (o 0).
    """
    best = None

    for bm in odds.get("bookmakers", []) if isinstance(odds, dict) else []:
        markets = bm.get("markets") or bm.get("market") or []

        # markets puede ser dict (clave -> mercado) o lista
        if isinstance(markets, dict):
            items = markets.items()
        elif isinstance(markets, list):
            items = [(i, mk) for i, mk in enumerate(markets)]
        else:
            items = []

        for _, mk in items:
            # mk puede tener varios campos: 'key','name','period', 'outcomes' o 'lines'
            name = (mk.get("name") or "").lower() if isinstance(mk, dict) else ""
            key = (mk.get("key") or "").lower() if isinstance(mk, dict) else ""
            period = (mk.get("period") or "").lower() if isinstance(mk, dict) else ""

            if any(k in (name + " " + key + " " + period) for k in ["first", "first half", "half", "ht", "1h"]):
                # Buscar outcomes/lines
                outcomes = mk.get("outcomes") or mk.get("lines") or mk.get("prices") or []
                # Si es dict con submarkets
                if isinstance(outcomes, dict):
                    outcomes = outcomes.get("values") or list(outcomes.values())

                if isinstance(outcomes, list):
                    for out in outcomes:
                        # out puede tener 'label'/'name' y 'price'/'odds'/'last_seen' y 'line'
                        label = (out.get("label") or out.get("name") or "").lower() if isinstance(out, dict) else ""
                        line = out.get("line") if isinstance(out, dict) else None
                        price = out.get("price") or out.get("odds") or out.get("last_seen") if isinstance(out, dict) else None
                        try:
                            price = float(price) if price is not None else None
                        except Exception:
                            price = None

                        # Aceptar 'over' con line 0.5 o 0
                        if ("over" in label or "o" == label.strip().lower()) and (line in (0.5, 0, "0.5", "0") or line is None and "0.5" in label):
                            if price and (best is None or price > best):
                                best = price
                else:
                    # mk podría tener keys 'over' / 'under'
                    for side in ("over", "Over", "OVER"):
                        if isinstance(mk, dict) and side in mk:
                            candidate = mk[side]
                            price = None
                            if isinstance(candidate, dict):
                                price = candidate.get("last_seen") or candidate.get("price") or candidate.get("odds")
                            try:
                                price = float(price) if price is not None else None
                            except Exception:
                                price = None
                            if price and (best is None or price > best):
                                best = price

    return best


# ──────────────────────────────────────────────
# SCORING
# ──────────────────────────────────────────────
def score_match(match: dict, stats: dict, odds: dict, ht_over_odds: float = None) -> dict:
    """Calcula un score combinando xG, posesión, SOT y la cuota HT over 0.5 (si existe).
    Devuelve dict con total_xg, possession, total_sot, score y verdict.
    """
    h_xg = float((stats.get("home") or {}).get("xg", 0) or 0)
    a_xg = float((stats.get("away") or {}).get("xg", 0) or 0)
    total_xg = h_xg + a_xg

    h_poss = float((stats.get("home") or {}).get("possession", 50) or 50)
    poss_diff = abs(h_poss - (100 - h_poss))

    h_sot = int((stats.get("home") or {}).get("shots_on_target", 0) or 0)
    a_sot = int((stats.get("away") or {}).get("shots_on_target", 0) or 0)
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


# Compatibilidad con backtest.py (nombre esperado)
def score_over_05_ht(match: dict, stats: dict, odds: dict) -> dict:
    ht_odds = extract_ht_over_05_odds(odds) if odds else None
    return score_match(match, stats, odds, ht_over_odds)


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    today = date.today().isoformat()
    print(f"📅 Escaneando partidos del {today}...")

    # 0) Intentar llms.txt para obtener match ids rápidos
    ids = fetch_llms_txt()
    all_matches = []

    if ids:
        print(f"🔎 llms.txt: encontrados {len(ids)} ids (usando feed rápido)")
        for mid in ids:
            try:
                m = get(f"/football/matches/{mid}")
                # get devuelve body.get('data',body) — si devuelve dict directamente, usarlo
                if isinstance(m, dict) and m.get("id"):
                    all_matches.append(m)
            except Exception as e:
                # ignorar errores por id
                print(f"⚠️ Error fetching match {mid}: {e}")
            time.sleep(0.2)
    else:
        # 1) Obtener todos los match_ids del día (fallback)
        page = 1
        while True:
            data = get("/football/matches", date_from=today, date_to=today, per_page=100, page=page)
            # compatibilidad: si data es dict con 'matches'
            if isinstance(data, dict):
                items = data.get("matches") or []
            elif isinstance(data, list):
                items = data
            else:
                items = []

            if not items:
                break
            all_matches.extend(items)
            # simplificado: no usamos meta, asumimos una sola página en muchos casos
            break

    print(f"Total matches encontrados: {len(all_matches)}")

    # Filtrar: solo con xG + odds de HT over 0.5 disponibles
    results = []
    for m in all_matches:
        mid = m.get("id") if isinstance(m, dict) else None
        if not mid:
            continue

        name = f"{m.get('home_team',{}).get('name','?')} vs {m.get('away_team',{}).get('name','?')}"

        try:
            stats = get(f"/football/matches/{mid}/stats") or {}
            odds = get(f"/football/matches/{mid}/odds") or {}

            ht_over = extract_ht_over_05_odds(odds)
            if not ht_over:
                # no existe mercado HT Over 0.5 -> skip
                print(f"⏭️ {name}: no HT Over 0.5 market")
                continue

            r = score_match(m, stats, odds, ht_over)

            # HT real si terminó
            ht = ""
            if m.get("status") == "finished":
                ht_h = m.get("score", {}).get("half_time_home", 0) or 0
                ht_a = m.get("score", {}).get("half_time_away", 0) or 0
                hit = "✅" if (ht_h + ht_a) >= 1 else "❌"
                ht = f" HT {ht_h}-{ht_a} {hit}"

            r["name"] = name
            r["ht"] = ht
            r["ht_over_odds"] = ht_over
            results.append(r)
            print(f"{r['verdict']} {name:<45} xG={r['total_xg']:.2f} SOT={r['total_sot']} Score={r['score']:>5} HTodds={ht_over}{ht}")
        except Exception as e:
            print(f"⚠️ {name}: {e}")

        time.sleep(0.2)

    # 3) Enviar a Telegram solo si hay resultados
    if results:
        high = [r for r in results if r["verdict"] == "🟢"]
        medium = [r for r in results if r["verdict"] == "🟡"]
        low = [r for r in results if r["verdict"] == "🔴"]

        # Construir mensaje HTML
        lines = [f"⚽ <b>OVER 0.5 HT — {today}</b>", ""]

        def fmt(r, show_odds=True):
            odds_part = f" | HTodds {r.get('ht_over_odds')}" if show_odds else ""
            ht_part = f" → {r['ht'].strip()}" if r["ht"] else ""
            return f"  {r['name']} | xG {r['total_xg']} | SOT {r['total_sot']} | <b>{r['score']}</b>{odds_part}{ht_part}"

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
            for r in low[:5]:  # máx 5 para no saturar
                lines.append(fmt(r, show_odds=False))
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
