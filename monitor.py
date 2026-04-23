#!/usr/bin/env python3
"""
FFE Compet Monitor — Playwright + Proxy Résidentiel
=====================================================
Chrome headless sur GitHub Actions, trafic routé via proxy
résidentiel français → Cloudflare voit un vrai navigateur
depuis une IP résidentielle française.
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
PROXY_URL     = os.environ.get("PROXY_URL", "")  # http://user:pass@p.webshare.io:80

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


# ─── Parse proxy URL ─────────────────────────────────────────────────────────
def parse_proxy_url(proxy_url: str) -> dict:
    """Parse http://user:pass@host:port into Playwright proxy config."""
    m = re.match(r"https?://([^:]+):([^@]+)@([^:]+):(\d+)", proxy_url)
    if m:
        return {
            "server": f"http://{m.group(3)}:{m.group(4)}",
            "username": m.group(1),
            "password": m.group(2),
        }
    return {}


# ─── Playwright + Proxy ──────────────────────────────────────────────────────
def fetch_all_concours(concours_list: list) -> dict:
    from playwright.sync_api import sync_playwright

    results = {}
    proxy_config = parse_proxy_url(PROXY_URL) if PROXY_URL else None

    with sync_playwright() as p:
        # Lancer Chrome avec le proxy résidentiel
        launch_args = {
            "headless": False,
            "args": ["--no-sandbox", "--disable-dev-shm-usage",
                     "--disable-blink-features=AutomationControlled",
                     "--disable-infobars", "--window-size=1920,1080"],
        }
        if proxy_config:
            launch_args["proxy"] = proxy_config
            print(f"  🔀 Proxy : {proxy_config['server']} (user: {proxy_config['username'][:8]}...)")

        browser = p.chromium.launch(**launch_args)

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

        # Appliquer playwright-stealth si disponible
        try:
            from playwright_stealth import stealth_sync
            page = context.new_page()
            stealth_sync(page)
            print("  🥷 Stealth mode activé (playwright-stealth)")
        except ImportError:
            # Fallback : stealth manuelle
            context.add_init_script("""
                // Masquer webdriver
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                delete navigator.__proto__.webdriver;

                // Plugins réalistes
                Object.defineProperty(navigator, 'plugins', {
                    get: () => {
                        const plugins = [
                            {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
                            {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
                            {name: 'Native Client', filename: 'internal-nacl-plugin'}
                        ];
                        plugins.length = 3;
                        return plugins;
                    }
                });

                // Languages
                Object.defineProperty(navigator, 'languages', {get: () => ['fr-FR', 'fr', 'en-US', 'en']});

                // Chrome runtime
                window.chrome = {
                    runtime: {
                        onMessage: {addListener: () => {}, removeListener: () => {}},
                        sendMessage: () => {},
                        connect: () => ({onMessage: {addListener: () => {}}, postMessage: () => {}})
                    },
                    loadTimes: () => ({})
                };

                // Permission
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) =>
                    parameters.name === 'notifications'
                        ? Promise.resolve({state: Notification.permission})
                        : originalQuery(parameters);

                // WebGL vendor
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) return 'Intel Inc.';
                    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                    return getParameter.apply(this, arguments);
                };
            """)
            page = context.new_page()
            print("  🥷 Stealth mode activé (manuelle)")

        # ── Passer Cloudflare sur la page d'accueil ──
        print("  🌐 Résolution Cloudflare (via proxy résidentiel)...")
        cf_passed = False
        try:
            page.goto("https://ffecompet.ffe.com/", timeout=60000)
            for i in range(30):
                content = page.content().lower()
                if ("recherche concours" in content or
                    "recherche cheval" in content or
                    "class=\"navbar" in content):
                    print(f"  ✅ Cloudflare résolu en {(i+1)*2}s")
                    cf_passed = True

                    # Debug cookies
                    cookies = context.cookies()
                    cookie_names = [c["name"] for c in cookies]
                    print(f"  🔑 Cookies : {cookie_names}")
                    cf_cookies = [c for c in cookies if "cf_" in c["name"].lower()]
                    if cf_cookies:
                        for c in cf_cookies:
                            print(f"     {c['name']} = {c['value'][:30]}...")
                    break
                time.sleep(2)

            if not cf_passed:
                raw = re.sub(r'<[^>]+>', ' ', page.content()[:600]).strip()
                print(f"  ❌ Cloudflare non résolu après 60s")
                print(f"     Titre: {page.title()}")
                print(f"     Contenu: {raw[:300]}")
                browser.close()
                return results

        except Exception as e:
            print(f"  ❌ Erreur : {e}")
            browser.close()
            return results

        time.sleep(random.uniform(1, 3))

        # ── Visiter chaque concours ──
        for cid in concours_list:
            cid = str(cid).strip()
            url = f"{CONCOURS_URL}{cid}"
            print(f"\n  📄 Concours {cid}...")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # Attendre résolution Cloudflare sur cette page
                resolved = False
                for attempt in range(30):  # 60 seconds max
                    html = page.content()
                    html_lower = html.lower()

                    if "card-header" in html_lower or "concours n" in html_lower:
                        resolved = True
                        break
                    if ("challenge" not in html_lower and
                        "cloudflare" not in html_lower and
                        "just a moment" not in html_lower and
                        "un instant" not in html_lower and
                        len(html) > 2000):
                        resolved = True
                        break

                    # Toutes les 4 secondes, tenter de cliquer le Turnstile
                    if attempt % 2 == 1:
                        try:
                            # Chercher l'iframe Turnstile
                            for frame in page.frames:
                                frame_url = frame.url or ""
                                if "challenges.cloudflare.com" in frame_url or "turnstile" in frame_url:
                                    # Cliquer au centre de l'iframe (la checkbox)
                                    try:
                                        checkbox = frame.query_selector("input[type='checkbox']")
                                        if checkbox:
                                            checkbox.click()
                                            print(f"      🖱 Clic checkbox Turnstile (attempt {attempt})")
                                    except Exception:
                                        pass
                                    # Ou cliquer sur n'importe quel élément cliquable
                                    try:
                                        body = frame.query_selector("body")
                                        if body:
                                            body.click()
                                            print(f"      🖱 Clic body iframe Turnstile (attempt {attempt})")
                                    except Exception:
                                        pass
                                    break

                            # Aussi essayer de cliquer sur l'iframe elle-même
                            iframes = page.query_selector_all("iframe")
                            for iframe in iframes:
                                src = iframe.get_attribute("src") or ""
                                if "challenges" in src or "turnstile" in src:
                                    iframe.click()
                                    print(f"      🖱 Clic iframe element (attempt {attempt})")
                                    break
                        except Exception:
                            pass

                    time.sleep(2)

                if resolved:
                    html = page.content()
                    results[cid] = parse_concours_html(html, cid)
                else:
                    raw = re.sub(r'<[^>]+>', ' ', page.content()[:500]).strip()
                    print(f"      ⚠ Cloudflare bloqué ({len(page.content())} chars)")
                    print(f"      Extrait: {raw[:200]}")
                    results[cid] = {
                        "id": cid, "url": url, "name": "", "status_code": "INCONNU",
                        "status": "Inconnu", "ouvert": False, "dates": "", "cloture": "",
                        "epreuves": 0, "error": "Cloudflare bloqué",
                        "checked_at": datetime.now().strftime("%d/%m %H:%M:%S"),
                    }

            except Exception as e:
                print(f"      ❌ {e}")
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
