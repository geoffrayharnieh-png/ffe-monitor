#!/usr/bin/env python3
"""
FFE Compet Monitor — Version Cloud (GitHub Actions + Playwright)
=================================================================
Utilise un vrai navigateur Chrome headless (Playwright) pour
contourner la protection Cloudflare de la FFE.
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
from bs4 import BeautifulSoup

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


# ─── Parsing HTML ─────────────────────────────────────────────────────────────
def detect_status(text: str) -> tuple:
    lower = text.lower()
    for pattern, (code, label) in STATUS_MAP.items():
        if pattern in lower:
            return code, label
    return "INCONNU", "Inconnu"


def parse_concours_html(html: str, cid: str, url: str) -> dict:
    info = {
        "id": cid, "url": url, "name": "", "status_code": "INCONNU",
        "status": "Inconnu", "ouvert": False, "dates": "", "cloture": "",
        "epreuves": 0, "error": None,
        "checked_at": datetime.now().strftime("%d/%m %H:%M:%S"),
    }

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


# ─── Playwright : navigateur headless ─────────────────────────────────────────
def fetch_all_concours(concours_list: list) -> dict:
    """Lance Chrome headless, passe Cloudflare, scrape chaque concours."""
    from playwright.sync_api import sync_playwright

    results = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,  # Mode headed avec Xvfb — Cloudflare ne détecte pas
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled",
                  "--disable-infobars",
                  "--window-size=1920,1080"],
        )

        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )

        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['fr-FR', 'fr', 'en']});
            window.chrome = { runtime: {} };
        """)

        page = context.new_page()

        # ── Passer Cloudflare via la page d'accueil ──
        print("  🌐 Résolution du challenge Cloudflare...")
        try:
            page.goto("https://ffecompet.ffe.com/", timeout=60000)
            # Attendre jusqu'à 30s que Cloudflare se résolve
            for i in range(15):
                content = page.content().lower()
                if "recherche concours" in content or "ffecompet" in content:
                    if "challenge" not in content and "cloudflare" not in content:
                        print(f"  ✅ Cloudflare résolu en {(i+1)*2}s")
                        break
                time.sleep(2)
            else:
                print("  ⚠ Cloudflare possiblement non résolu — on essaie quand même")
        except Exception as e:
            print(f"  ⚠ Erreur page d'accueil : {e}")

        time.sleep(random.uniform(1, 3))

        # ── Visiter chaque concours ──
        for cid in concours_list:
            cid = str(cid).strip()
            url = f"{CONCOURS_URL}{cid}"

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # Attendre que Cloudflare se résolve sur cette page aussi
                resolved = False
                for attempt in range(25):  # max 50 secondes
                    html = page.content()
                    html_lower = html.lower()

                    # Vérifier si on a le vrai contenu (card-header = page concours)
                    if "card-header" in html_lower or "concours n" in html_lower:
                        resolved = True
                        break

                    # Si page normale sans challenge
                    if "challenge" not in html_lower and "cloudflare" not in html_lower and len(html) > 1000:
                        resolved = True
                        break

                    # Tenter de cliquer sur le widget Turnstile (checkbox Cloudflare)
                    if attempt in (3, 6, 10):
                        try:
                            # Le Turnstile est souvent dans une iframe
                            for frame in page.frames:
                                checkbox = frame.query_selector("input[type='checkbox']") or \
                                           frame.query_selector(".cf-turnstile") or \
                                           frame.query_selector("#challenge-stage")
                                if checkbox:
                                    checkbox.click()
                                    print(f"      🖱 Clic Turnstile tenté (attempt {attempt})")
                                    break
                        except Exception:
                            pass

                    time.sleep(2)

                if not resolved:
                    print(f"  ⚠ {cid} — Cloudflare non résolu (HTML: {len(html)} chars)")
                    # Afficher un extrait pour debug
                    clean = re.sub(r'<[^>]+>', ' ', html[:500]).strip()
                    print(f"      Extrait : {clean[:200]}")
                    results[cid] = {
                        "id": cid, "url": url, "name": "", "status_code": "INCONNU",
                        "status": "Inconnu", "ouvert": False, "dates": "", "cloture": "",
                        "epreuves": 0, "error": "Cloudflare bloqué",
                        "checked_at": datetime.now().strftime("%d/%m %H:%M:%S"),
                    }
                else:
                    html = page.content()
                    results[cid] = parse_concours_html(html, cid, url)

            except Exception as e:
                results[cid] = {
                    "id": cid, "url": url, "name": "", "status_code": "INCONNU",
                    "status": "Inconnu", "ouvert": False, "dates": "", "cloture": "",
                    "epreuves": 0, "error": str(e)[:120],
                    "checked_at": datetime.now().strftime("%d/%m %H:%M:%S"),
                }

            time.sleep(random.uniform(2, 5))

        browser.close()

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

    # Lancer le navigateur et vérifier tous les concours
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
            print(f"      Dates : {info.get('dates', '?')}")
            print(f"      Clôture : {info.get('cloture', '?')}")
            print(f"      URL : {info.get('url', '?')}")
            ok = send_ntfy(
                title="🏇 Engagements OUVERTS !",
                message=f"{name}\nConcours N° {cid}\nDates : {info.get('dates') or '?'}\nClôture : {info.get('cloture') or '?'}",
                url=info.get("url", ""), priority=5,
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

    save_state(state)
    if changes:
        print(f"\n💾 État mis à jour.")
    print(f"\n✅ Terminé.\n")


if __name__ == "__main__":
    main()
