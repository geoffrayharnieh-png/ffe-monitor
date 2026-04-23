#!/usr/bin/env python3
"""
FFE Compet Monitor — Version Cloud (requests + proxy résidentiel)
==================================================================
Simple et efficace : requests + proxy Webshare résidentiel français.
Pas de navigateur, pas de Selenium, pas de Playwright.
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup

# ─── Configuration ────────────────────────────────────────────────────────────
CONCOURS_URL  = "https://ffecompet.ffe.com/concours/"
CONCOURS_FILE = Path(__file__).parent / "concours.json"
STATE_FILE    = Path(__file__).parent / "state.json"
NTFY_TOPIC    = os.environ.get("NTFY_TOPIC", "")
PROXY_URL     = os.environ.get("PROXY_URL", "")  # http://user:pass@p.webshare.io:80

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://ffecompet.ffe.com/",
}

STATUS_MAP = {
    "ouvert aux engagements": ("OUVERT",     "Ouvert aux engagements"),
    "en cours":               ("EN_COURS",   "En cours"),
    "calendrier":             ("CALENDRIER", "Calendrier"),
    "clôturé":                ("CLOTURE",    "Clôturé"),
    "cloturé":                ("CLOTURE",    "Clôturé"),
    "terminé":                ("TERMINE",    "Terminé"),
    "annulé":                 ("ANNULE",     "Annulé"),
}


# ─── Scraping ─────────────────────────────────────────────────────────────────
def detect_status(text: str) -> tuple:
    lower = text.lower()
    for pattern, (code, label) in STATUS_MAP.items():
        if pattern in lower:
            return code, label
    return "INCONNU", "Inconnu"


def fetch_concours(session: requests.Session, cid: str) -> dict:
    url = f"{CONCOURS_URL}{cid}"
    info = {
        "id": cid, "url": url, "name": "", "status_code": "INCONNU",
        "status": "Inconnu", "ouvert": False, "dates": "", "cloture": "",
        "epreuves": 0, "error": None,
        "checked_at": datetime.now().strftime("%d/%m %H:%M:%S"),
    }

    try:
        resp = session.get(url, timeout=20, headers={
            "Referer": "https://ffecompet.ffe.com/",
        })
        resp.raise_for_status()
    except Exception as e:
        info["error"] = str(e)[:120]
        return info

    html = resp.text

    # Vérifier qu'on n'est pas sur une page Cloudflare
    if len(html) < 1000 or "challenge-platform" in html.lower() or "just a moment" in html.lower():
        info["error"] = f"Cloudflare bloqué ({len(html)} chars)"
        return info

    soup = BeautifulSoup(html, "html.parser")

    # ── Header : nom + statut ──
    header = soup.find("div", class_="card-header")
    if not header:
        for el in soup.find_all(string=re.compile(r"concours\s*N", re.I)):
            header = el.parent
            break

    if header:
        text = header.get_text(separator=" ", strip=True)
        m = re.match(r"^(.+?)\s*[-–—]\s*concours", text, re.I)
        if m:
            info["name"] = m.group(1).strip()

        code, label = detect_status(text)
        info["status_code"] = code
        info["status"] = label
        info["ouvert"] = (code == "OUVERT")

        if code == "INCONNU":
            for child in header.find_all(["span", "div", "small", "strong"]):
                code, label = detect_status(child.get_text(strip=True))
                if code != "INCONNU":
                    info["status_code"] = code
                    info["status"] = label
                    info["ouvert"] = (code == "OUVERT")
                    break

    # ── Body : dates + clôture ──
    body = soup.find("div", class_="card-body") or soup
    text = body.get_text(separator="\n", strip=True)

    m = re.search(r"du\s+(\d{2}/\d{2}/\d{4})\s+au\s+(\d{2}/\d{2}/\d{4})", text)
    if m:
        info["dates"] = f"{m.group(1)} → {m.group(2)}"

    m = re.search(r"[Cc]l[ôo]ture\s+le\s+(\d{2}/\d{2}/\d{4})", text)
    if m:
        info["cloture"] = m.group(1)

    # ── Fallback statut (header uniquement) ──
    if info["status_code"] == "INCONNU" and header:
        code, label = detect_status(header.get_text(separator=" "))
        info["status_code"] = code
        info["status"] = label
        info["ouvert"] = (code == "OUVERT")

    # ── Épreuves ──
    rows = soup.find_all("tr")
    count = sum(1 for r in rows if len(r.find_all("td")) >= 5)
    if count:
        info["epreuves"] = count

    if not info["name"]:
        t = soup.find("title")
        if t:
            info["name"] = t.get_text(strip=True).split("-")[0].strip()

    return info


# ─── Notifications ntfy ──────────────────────────────────────────────────────
def send_ntfy(title: str, message: str, url: str = "", priority: int = 5):
    if not NTFY_TOPIC:
        print("  ⚠ NTFY_TOPIC non configuré")
        return False
    try:
        payload = {
            "topic": NTFY_TOPIC, "title": title, "message": message,
            "priority": priority, "tags": ["horse", "trophy"],
        }
        if url:
            payload["click"] = url
            payload["actions"] = [{"action": "view", "label": "Ouvrir la fiche", "url": url}]
        resp = requests.post("https://ntfy.sh", json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"  ⚠ Erreur ntfy : {e}")
        return False


# ─── Gestion d'état ──────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def load_concours_list() -> list:
    if not CONCOURS_FILE.exists():
        print(f"❌ Fichier {CONCOURS_FILE} introuvable.")
        sys.exit(1)
    with open(CONCOURS_FILE, "r", encoding="utf-8") as f:
        return json.load(f).get("concours", [])


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    print(f"{'='*60}")
    print(f"  FFE Compet Monitor — {now}")
    print(f"{'='*60}")

    concours_list = load_concours_list()
    if not concours_list:
        print("Aucun concours à surveiller.")
        return

    state = load_state()

    # Configurer la session avec proxy + fingerprint Chrome
    session = cffi_requests.Session(impersonate="chrome")

    if PROXY_URL:
        session.proxies = {"http": PROXY_URL, "https": PROXY_URL}
        print(f"  🔀 Proxy résidentiel + fingerprint Chrome configurés")
    else:
        print(f"  ⚠ Pas de proxy configuré (PROXY_URL vide)")

    print(f"\n📋 {len(concours_list)} concours à vérifier")

    jitter = random.uniform(0, 10)
    print(f"   ⏳ Délai initial : {jitter:.0f}s\n")
    time.sleep(jitter)

    changes = False

    for cid in concours_list:
        cid = str(cid).strip()
        prev = state.get(cid, {})
        was_ouvert = prev.get("ouvert", False)
        prev_code = prev.get("status_code", "INCONNU")

        info = fetch_concours(session, cid)
        name = info.get("name") or f"Fiche Concours n°{cid}"

        if info.get("error"):
            print(f"  ⚠ {name} — Erreur : {info['error']}")
        else:
            print(f"  {'🟢' if info['ouvert'] else '⚪'} {name} — {info['status']}")

        if info["ouvert"] and not was_ouvert:
            print(f"\n  🎉🎉🎉 OUVERTURE DÉTECTÉE : {name} !")
            print(f"      Dates : {info.get('dates', '?')}")
            print(f"      Clôture : {info.get('cloture', '?')}")
            print(f"      URL : {info['url']}")
            ok = send_ntfy(
                title="🏇 Engagements OUVERTS !",
                message=f"{name}\nConcours N° {cid}\nDates : {info.get('dates') or '?'}\nClôture : {info.get('cloture') or '?'}",
                url=info["url"], priority=5,
            )
            if ok:
                print(f"  📱 Notification envoyée !")
            print()
        elif info.get("status_code", "INCONNU") != prev_code and prev_code != "INCONNU":
            print(f"      ↻ Changement : {prev.get('status', '?')} → {info.get('status', '?')}")

        if info.get("status_code") != prev_code or info.get("ouvert") != was_ouvert:
            changes = True

        state[cid] = {
            "status_code": info.get("status_code", "INCONNU"),
            "status": info.get("status", "Inconnu"),
            "ouvert": info.get("ouvert", False),
            "name": info.get("name", ""),
            "dates": info.get("dates", ""),
            "cloture": info.get("cloture", ""),
            "last_check": now,
        }

        time.sleep(random.uniform(1, 4))

    save_state(state)
    if changes:
        print(f"\n💾 État mis à jour.")
    print(f"\n✅ Terminé.\n")


if __name__ == "__main__":
    main()
