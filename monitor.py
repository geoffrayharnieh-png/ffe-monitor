#!/usr/bin/env python3
"""
FFE Compet Monitor — Version Cloud (Playwright + JS fetch)
============================================================
Passe Cloudflare une seule fois sur la page d'accueil, puis
utilise fetch() JavaScript depuis le navigateur pour charger
les fiches concours sans déclencher de nouveau challenge.
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


# ─── Playwright ───────────────────────────────────────────────────────────────
def fetch_all_concours(concours_list: list) -> dict:
    from playwright.sync_api import sync_playwright

    results = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled",
                  "--disable-infobars", "--window-size=1920,1080"],
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

        # ── Étape 1 : Passer Cloudflare sur la page d'accueil ──
        print("  🌐 Résolution du challenge Cloudflare...")
        cf_passed = False
        try:
            page.goto("https://ffecompet.ffe.com/", timeout=60000)
            for i in range(30):  # max 60 secondes
                content = page.content()
                content_lower = content.lower()

                # Vrai test : chercher un élément du site FFE
                if ("recherche concours" in content_lower or
                    "recherche cheval" in content_lower or
                    "recherche cavalier" in content_lower or
                    "class=\"navbar" in content_lower):
                    print(f"  ✅ Cloudflare résolu en {(i+1)*2}s")
                    cf_passed = True

                    # Debug : afficher le titre et les cookies
                    title = page.title()
                    cookies = context.cookies()
                    cookie_names = [c["name"] for c in cookies]
                    print(f"      Titre page : {title}")
                    print(f"      Cookies ({len(cookies)}) : {cookie_names}")
                    cf_cookie = [c for c in cookies if "cf_" in c["name"]]
                    if cf_cookie:
                        for c in cf_cookie:
                            print(f"      🔑 {c['name']} = {c['value'][:20]}... (domain={c.get('domain','?')})")
                    break

                # Tenter de cliquer Turnstile si présent
                if i in (5, 10, 15):
                    try:
                        for frame in page.frames:
                            cb = frame.query_selector("input[type='checkbox']")
                            if cb:
                                cb.click()
                                print(f"      🖱 Clic Turnstile (attempt {i})")
                    except Exception:
                        pass

                time.sleep(2)

            if not cf_passed:
                # Log debug
                raw = re.sub(r'<[^>]+>', ' ', page.content()[:800]).strip()
                print(f"  ❌ Cloudflare non résolu après 60s")
                print(f"     Contenu : {raw[:300]}")
                browser.close()
                return results

        except Exception as e:
            print(f"  ❌ Erreur page d'accueil : {e}")
            browser.close()
            return results

        time.sleep(random.uniform(1, 3))

        # ── Étape 2 : Utiliser fetch() JS pour charger chaque concours ──
        # Le navigateur a les cookies Cloudflare → fetch() les envoie automatiquement
        # Pas de navigation = pas de nouveau challenge
        print(f"\n  📡 Chargement des fiches via fetch() JavaScript...")

        for cid in concours_list:
            cid = str(cid).strip()
            url = f"{CONCOURS_URL}{cid}"

            try:
                # Exécuter fetch() depuis le navigateur (inclut les cookies CF)
                html = page.evaluate("""async (url) => {
                    try {
                        const resp = await fetch(url, {
                            credentials: 'include',
                            headers: {
                                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                            }
                        });
                        if (!resp.ok) return {error: 'HTTP ' + resp.status, html: ''};
                        const text = await resp.text();
                        return {error: null, html: text};
                    } catch(e) {
                        return {error: e.message, html: ''};
                    }
                }""", url)

                if html.get("error"):
                    print(f"  ⚠ {cid} — fetch error: {html['error']}")
                    # Debug: essayer aussi via page.goto pour comparer
                    print(f"      Debug: tentative page.goto()...")
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=15000)
                        time.sleep(5)
                        goto_html = page.content()
                        has_card = "card-header" in goto_html.lower()
                        has_cf = "cloudflare" in goto_html.lower()
                        print(f"      goto result: {len(goto_html)} chars, card-header={has_card}, cloudflare={has_cf}")
                        if has_card:
                            # Ça marche via goto ! Parser cette page
                            results[cid] = parse_concours_html(goto_html, cid)
                            time.sleep(random.uniform(1, 3))
                            continue
                        # Revenir à l'accueil pour le prochain
                        page.goto("https://ffecompet.ffe.com/", wait_until="domcontentloaded", timeout=15000)
                        time.sleep(3)
                    except Exception as goto_e:
                        print(f"      goto aussi échoué: {goto_e}")
                    results[cid] = {
                        "id": cid, "url": url, "name": "", "status_code": "INCONNU",
                        "status": "Inconnu", "ouvert": False, "dates": "", "cloture": "",
                        "epreuves": 0, "error": html["error"],
                        "checked_at": datetime.now().strftime("%d/%m %H:%M:%S"),
                    }
                elif len(html.get("html", "")) < 500:
                    print(f"  ⚠ {cid} — Réponse trop courte ({len(html.get('html', ''))} chars)")
                    results[cid] = {
                        "id": cid, "url": url, "name": "", "status_code": "INCONNU",
                        "status": "Inconnu", "ouvert": False, "dates": "", "cloture": "",
                        "epreuves": 0, "error": "Réponse vide",
                        "checked_at": datetime.now().strftime("%d/%m %H:%M:%S"),
                    }
                else:
                    results[cid] = parse_concours_html(html["html"], cid)

            except Exception as e:
                print(f"  ⚠ {cid} — Exception: {e}")
                results[cid] = {
                    "id": cid, "url": url, "name": "", "status_code": "INCONNU",
                    "status": "Inconnu", "ouvert": False, "dates": "", "cloture": "",
                    "epreuves": 0, "error": str(e)[:120],
                    "checked_at": datetime.now().strftime("%d/%m %H:%M:%S"),
                }

            time.sleep(random.uniform(1, 3))

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
