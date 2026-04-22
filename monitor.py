#!/usr/bin/env python3
"""
FFE Compet Monitor — undetected-chromedriver + Selenium
========================================================
Utilise undetected-chromedriver pour contourner Cloudflare.
Ce driver patche Chrome en profondeur (supprime cdc_, modifie
le binaire) pour être indétectable.
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
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ─── Configuration ────────────────────────────────────────────────────────────
CONCOURS_URL  = "https://ffecompet.ffe.com/concours/"
CONCOURS_FILE = Path(__file__).parent / "concours.json"
STATE_FILE    = Path(__file__).parent / "state.json"
NTFY_TOPIC    = os.environ.get("NTFY_TOPIC", "")

STATUS_MAP = {
    "ouvert aux engagements": ("OUVERT",     "Ouvert aux engagements"),
    "en cours":               ("EN_COURS",   "En cours"),
    "calendrier":             ("CALENDRIER", "Calendrier"),
    "clôturé":                ("CLOTURE",    "Clôturé"),
    "cloturé":                ("CLOTURE",    "Clôturé"),
    "terminé":                ("TERMINE",    "Terminé"),
    "annulé":                 ("ANNULE",     "Annulé"),
}


# ─── Parsing ──────────────────────────────────────────────────────────────────
def detect_status(text: str) -> tuple:
    lower = text.lower()
    for pattern, (code, label) in STATUS_MAP.items():
        if pattern in lower:
            return code, label
    return "INCONNU", "Inconnu"


def parse_concours_html(html: str, cid: str) -> dict:
    url = f"{CONCOURS_URL}{cid}"
    info = {
        "id": cid, "url": url, "name": "", "status_code": "INCONNU",
        "status": "Inconnu", "ouvert": False, "dates": "", "cloture": "",
        "epreuves": 0, "error": None,
        "checked_at": datetime.now().strftime("%d/%m %H:%M:%S"),
    }

    soup = BeautifulSoup(html, "html.parser")

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

    body = soup.find("div", class_="card-body") or soup
    text = body.get_text(separator="\n", strip=True)

    m = re.search(r"du\s+(\d{2}/\d{2}/\d{4})\s+au\s+(\d{2}/\d{2}/\d{4})", text)
    if m:
        info["dates"] = f"{m.group(1)} → {m.group(2)}"

    m = re.search(r"[Cc]l[ôo]ture\s+le\s+(\d{2}/\d{2}/\d{4})", text)
    if m:
        info["cloture"] = m.group(1)

    if info["status_code"] == "INCONNU" and header:
        code, label = detect_status(header.get_text(separator=" "))
        info["status_code"] = code
        info["status"] = label
        info["ouvert"] = (code == "OUVERT")

    rows = soup.find_all("tr")
    count = sum(1 for r in rows if len(r.find_all("td")) >= 5)
    if count:
        info["epreuves"] = count

    if not info["name"]:
        t = soup.find("title")
        if t:
            info["name"] = t.get_text(strip=True).split("-")[0].strip()

    return info


# ─── Selenium + undetected-chromedriver ───────────────────────────────────────
def wait_for_cloudflare(driver, timeout=60):
    """Attend que le challenge Cloudflare se résolve."""
    print(f"      Attente résolution Cloudflare (max {timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            source = driver.page_source.lower()
            title = driver.title.lower()

            # La page FFE est chargée si on voit ces éléments
            if ("card-header" in source or
                "recherche concours" in source or
                "recherche cheval" in source or
                "ffecompet" in title):
                if "challenge" not in source or "card-header" in source:
                    elapsed = time.time() - start
                    print(f"      ✅ Page chargée en {elapsed:.0f}s")
                    return True

            # Toujours sur la page Cloudflare "Un instant..."
            if "un instant" in source or "challenge-platform" in source:
                time.sleep(2)
                continue

            # Page chargée mais pas de marqueur FFE connu
            if len(source) > 5000 and "cloudflare" not in source:
                elapsed = time.time() - start
                print(f"      ✅ Page chargée en {elapsed:.0f}s (contenu inconnu)")
                return True

        except Exception:
            pass

        time.sleep(2)

    print(f"      ❌ Timeout après {timeout}s")
    return False


def fetch_all_concours(concours_list: list) -> dict:
    """Lance Chrome non détecté, passe Cloudflare, scrape chaque concours."""
    results = {}

    # ── Configurer Chrome avec undetected-chromedriver ──
    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-infobars")
    options.add_argument("--lang=fr-FR")

    print("  🌐 Lancement de Chrome (undetected)...")

    try:
        driver = uc.Chrome(options=options, headless=False)
    except Exception as e:
        print(f"  ❌ Impossible de lancer Chrome : {e}")
        # Essayer en headless si headed échoue
        try:
            driver = uc.Chrome(options=options, headless=True)
            print("  ⚠ Fallback en headless")
        except Exception as e2:
            print(f"  ❌ Chrome headless aussi échoué : {e2}")
            return results

    try:
        # ── Étape 1 : Passer Cloudflare sur la page d'accueil ──
        print("  🌐 Navigation vers ffecompet.ffe.com...")
        driver.get("https://ffecompet.ffe.com/")

        if not wait_for_cloudflare(driver, timeout=45):
            # Debug
            source = driver.page_source
            clean = re.sub(r'<[^>]+>', ' ', source[:600]).strip()
            print(f"  ❌ Cloudflare non résolu sur l'accueil")
            print(f"     Titre: {driver.title}")
            print(f"     Contenu: {clean[:300]}")
            driver.quit()
            return results

        # Debug : cookies
        cookies = driver.get_cookies()
        cookie_names = [c["name"] for c in cookies]
        print(f"  🔑 Cookies : {cookie_names}")
        cf_cookies = [c for c in cookies if "cf_" in c["name"].lower()]
        for c in cf_cookies:
            print(f"     {c['name']} = {c['value'][:30]}...")

        time.sleep(random.uniform(2, 4))

        # ── Étape 2 : Visiter chaque concours ──
        for cid in concours_list:
            cid = str(cid).strip()
            url = f"{CONCOURS_URL}{cid}"

            print(f"\n  📄 Chargement concours {cid}...")
            try:
                driver.get(url)

                if wait_for_cloudflare(driver, timeout=45):
                    html = driver.page_source

                    if "card-header" in html.lower() or len(html) > 10000:
                        results[cid] = parse_concours_html(html, cid)
                    else:
                        print(f"      ⚠ Page chargée mais pas de card-header")
                        clean = re.sub(r'<[^>]+>', ' ', html[:500]).strip()
                        print(f"      Contenu: {clean[:200]}")
                        results[cid] = {
                            "id": cid, "url": url, "name": "", "status_code": "INCONNU",
                            "status": "Inconnu", "ouvert": False, "dates": "", "cloture": "",
                            "epreuves": 0, "error": "Contenu inattendu",
                            "checked_at": datetime.now().strftime("%d/%m %H:%M:%S"),
                        }
                else:
                    source = driver.page_source
                    clean = re.sub(r'<[^>]+>', ' ', source[:500]).strip()
                    print(f"      Contenu: {clean[:200]}")
                    results[cid] = {
                        "id": cid, "url": url, "name": "", "status_code": "INCONNU",
                        "status": "Inconnu", "ouvert": False, "dates": "", "cloture": "",
                        "epreuves": 0, "error": "Cloudflare bloqué",
                        "checked_at": datetime.now().strftime("%d/%m %H:%M:%S"),
                    }

            except Exception as e:
                print(f"      ❌ Exception: {e}")
                results[cid] = {
                    "id": cid, "url": url, "name": "", "status_code": "INCONNU",
                    "status": "Inconnu", "ouvert": False, "dates": "", "cloture": "",
                    "epreuves": 0, "error": str(e)[:120],
                    "checked_at": datetime.now().strftime("%d/%m %H:%M:%S"),
                }

            time.sleep(random.uniform(2, 5))

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return results


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

    print(f"\n📋 {len(concours_list)} concours à vérifier")

    jitter = random.uniform(0, 10)
    print(f"   ⏳ Délai initial : {jitter:.0f}s\n")
    time.sleep(jitter)

    all_results = fetch_all_concours(concours_list)

    changes = False

    for cid in concours_list:
        cid = str(cid).strip()
        prev = state.get(cid, {})
        was_ouvert = prev.get("ouvert", False)
        prev_code = prev.get("status_code", "INCONNU")

        info = all_results.get(cid, {})
        name = info.get("name") or f"Fiche Concours n°{cid}"

        if info.get("error"):
            print(f"  ⚠ {name} — Erreur : {info['error']}")
        else:
            print(f"  {'🟢' if info.get('ouvert') else '⚪'} {name} — {info.get('status', '?')}")

        if info.get("ouvert") and not was_ouvert:
            print(f"\n  🎉🎉🎉 OUVERTURE DÉTECTÉE : {name} !")
            ok = send_ntfy(
                title="🏇 Engagements OUVERTS !",
                message=f"{name}\nConcours N° {cid}\nDates : {info.get('dates') or '?'}\nClôture : {info.get('cloture') or '?'}",
                url=info.get("url", ""), priority=5,
            )
            if ok:
                print(f"  📱 Notification envoyée !")
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

    save_state(state)
    if changes:
        print(f"\n💾 État mis à jour.")
    print(f"\n✅ Terminé.\n")


if __name__ == "__main__":
    main()
