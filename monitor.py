#!/usr/bin/env python3
"""
FFE Compet Monitor — Version Cloud (GitHub Actions)
=====================================================
Vérifie le statut des concours et envoie une notification
push via ntfy.sh quand un concours s'ouvre aux engagements.

Utilise curl_cffi pour imiter le fingerprint TLS de Chrome
et contourner la détection anti-bot de la FFE.
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests  # pour ntfy uniquement
from curl_cffi import requests as cffi_requests  # pour FFE (anti-403)
from bs4 import BeautifulSoup

# ─── Configuration ────────────────────────────────────────────────────────────
CONCOURS_URL = "https://ffecompet.ffe.com/concours/"
CONCOURS_FILE = Path(__file__).parent / "concours.json"
STATE_FILE    = Path(__file__).parent / "state.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

STATUS_MAP = {
    "ouvert aux engagements": ("OUVERT",     "Ouvert aux engagements"),
    "en cours":               ("EN_COURS",   "En cours"),
    "calendrier":             ("CALENDRIER", "Calendrier"),
    "clôturé":                ("CLOTURE",    "Clôturé"),
    "cloturé":                ("CLOTURE",    "Clôturé"),
    "terminé":                ("TERMINE",    "Terminé"),
    "annulé":                 ("ANNULE",     "Annulé"),
}


# ─── Scraping (avec curl_cffi pour contourner le 403) ─────────────────────────
def detect_status(text: str) -> tuple:
    lower = text.lower()
    for pattern, (code, label) in STATUS_MAP.items():
        if pattern in lower:
            return code, label
    return "INCONNU", "Inconnu"


def fetch_concours(session, cid: str) -> dict:
    url = f"{CONCOURS_URL}{cid}"
    info = {
        "id": cid, "url": url, "name": "", "status_code": "INCONNU",
        "status": "Inconnu", "ouvert": False, "dates": "", "cloture": "",
        "lieu": "", "organisateur": "", "epreuves": 0, "error": None,
        "checked_at": datetime.now().strftime("%d/%m %H:%M:%S"),
    }

    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        info["error"] = str(e)[:120]
        return info

    soup = BeautifulSoup(resp.text, "html.parser")

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

    # ── Fallback statut (header seulement, pas toute la page) ──
    if info["status_code"] == "INCONNU" and header:
        # Ne chercher que dans le header pour éviter les faux positifs
        code, label = detect_status(header.get_text(separator=" "))
        info["status_code"] = code
        info["status"] = label
        info["ouvert"] = (code == "OUVERT")

    # ── Table : compter épreuves ──
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
        print("  ⚠ NTFY_TOPIC non configuré — notification ignorée")
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

    # Créer une session curl_cffi qui imite Chrome
    session = cffi_requests.Session(impersonate="chrome")

    print(f"\n📋 {len(concours_list)} concours à vérifier")

    jitter = random.uniform(0, 15)
    print(f"   ⏳ Délai initial : {jitter:.0f}s\n")
    time.sleep(jitter)

    changes = False

    for cid in concours_list:
        cid = str(cid).strip()
        prev = state.get(cid, {})
        was_ouvert = prev.get("ouvert", False)
        prev_code = prev.get("status_code", "INCONNU")

        info = fetch_concours(session, cid)
        name = info["name"] or f"Fiche Concours n°{cid}"

        if info.get("error"):
            print(f"  ⚠ {name} — Erreur : {info['error']}")
        else:
            print(f"  {'🟢' if info['ouvert'] else '⚪'} {name} — {info['status']}")

        if info["ouvert"] and not was_ouvert:
            print(f"\n  🎉🎉🎉 OUVERTURE DÉTECTÉE : {name} !")
            print(f"      Dates : {info['dates']}")
            print(f"      Clôture : {info['cloture']}")
            print(f"      URL : {info['url']}")
            ok = send_ntfy(
                title="🏇 Engagements OUVERTS !",
                message=f"{name}\nConcours N° {cid}\nDates : {info.get('dates') or '?'}\nClôture : {info.get('cloture') or '?'}",
                url=info["url"], priority=5,
            )
            if ok:
                print(f"  📱 Notification envoyée !")
            print()
        elif info["status_code"] != prev_code and prev_code != "INCONNU":
            print(f"      ↻ Changement : {prev.get('status', '?')} → {info['status']}")

        if info["status_code"] != prev_code or info["ouvert"] != was_ouvert:
            changes = True

        state[cid] = {
            "status_code": info["status_code"], "status": info["status"],
            "ouvert": info["ouvert"], "name": info["name"],
            "dates": info["dates"], "cloture": info["cloture"],
            "last_check": now,
        }

        time.sleep(random.uniform(1, 4))

    save_state(state)
    if changes:
        print(f"\n💾 État mis à jour.")
    print(f"\n✅ Terminé.\n")


if __name__ == "__main__":
    main()
