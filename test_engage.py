#!/usr/bin/env python3
"""
TEST v4 — Fix : ajout du segment {} manquant dans requestEnter
"""

import json
import os
import sys
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup

BASE_URL     = "https://ffecompet.ffe.com"
SSO_URL      = "https://sso.ffe.com"
NTFY_TOPIC   = os.environ.get("NTFY_TOPIC", "")
FFE_USERNAME = os.environ.get("FFE_USERNAME", "")
FFE_PASSWORD = os.environ.get("FFE_PASSWORD", "")

CONCOURS_ID = "202656038"
EPREUVES = {
    1: "300255502",
    2: "300255503",
}

CAVALIER = {"idCompo": "25_1_3_1_1",  "idLic": "228951"}
COACH    = {"idCompo": "25_1_30_1_0", "idLic": ""}
CHEVAL   = {"idCompo": "25_1_1_1_1",  "idHorse": "1948585"}

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

XHR_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": BROWSER_UA,
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
}


def ffe_quote(s):
    """URL-encode comme le navigateur : encode []{}\" mais garde :, intacts."""
    return urllib.parse.quote(s, safe=":,")


def login_sso(session):
    print("\n🔐 Connexion SSO CAS...")
    service_url = f"{BASE_URL}/login?_target_path={urllib.parse.quote(BASE_URL + '/', safe='')}"
    login_page_url = f"{SSO_URL}/login?service={urllib.parse.quote(service_url, safe='')}"

    resp = session.get(login_page_url, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    exec_input = soup.find("input", {"name": "execution"})
    if not exec_input or not exec_input.get("value"):
        print("  ❌ Token 'execution' introuvable")
        return False

    print(f"  ✓ Token execution récupéré ({len(exec_input['value'])} chars)")

    resp = session.post(login_page_url, data={
        "username": FFE_USERNAME,
        "password": FFE_PASSWORD,
        "execution": exec_input["value"],
        "_eventId": "submit",
    }, timeout=20, allow_redirects=True)

    if "PHP_FFECOMPET_SESSION" in session.cookies.get_dict():
        print(f"  ✅ Connecté ! (session: {session.cookies['PHP_FFECOMPET_SESSION'][:10]}...)")
        return True

    if "ticket=" in (resp.url or ""):
        session.get(resp.url, timeout=20)
        if "PHP_FFECOMPET_SESSION" in session.cookies.get_dict():
            print(f"  ✅ Connecté !")
            return True

    print("  ❌ Login échoué")
    return False


def do_engagement(session, epreuve_id, epreuve_num):
    referer = f"{BASE_URL}/engagement/{CONCOURS_ID}/{epreuve_num}"
    headers = {**XHR_HEADERS, "Referer": referer}

    cavaliers_json = json.dumps([
        {"idCompo": CAVALIER["idCompo"], "idLic": str(CAVALIER["idLic"])},
        {"idCompo": COACH["idCompo"],    "idLic": str(COACH["idLic"])},
    ], separators=(",", ":"))

    chevaux_json = json.dumps([
        {"idCompo": CHEVAL["idCompo"], "idHorse": str(CHEVAL["idHorse"])},
    ], separators=(",", ":"))

    params_json = json.dumps({
        "action": "enter",
        "justControls": 0,
        "checkPenalty": 0,
        "checkAttestation": 0,
    }, separators=(",", ":"))

    # ── Étape 0 : Visiter la page ──
    print(f"\n    → Étape 0 : Visite page /engagement/{CONCOURS_ID}/{epreuve_num} ...")
    resp = session.get(f"{BASE_URL}/engagement/{CONCOURS_ID}/{epreuve_num}", timeout=20)
    print(f"      HTTP {resp.status_code}")
    time.sleep(0.5)

    # ── Étape 1 : selecteurs/test ──
    print(f"    → Étape 1 : POST selecteurs/test ...")
    resp = session.post(f"{BASE_URL}/concours/selecteurs/test", headers=headers, timeout=20)
    print(f"      HTTP {resp.status_code}")
    time.sleep(0.5)

    # ── Étape 1b : selecteurs/contest ──
    print(f"    → Étape 1b : POST selecteurs/contest ...")
    resp = session.post(f"{BASE_URL}/concours/selecteurs/contest", headers=headers, timeout=20)
    print(f"      HTTP {resp.status_code}")
    time.sleep(0.5)

    # ── Étape 2 : translate ──
    print(f"    → Étape 2 : GET composition/translate ...")
    translate_payload = json.dumps({
        "licensees": json.dumps([
            {"idCompo": CAVALIER["idCompo"], "idLic": str(CAVALIER["idLic"])},
            {"idCompo": COACH["idCompo"],    "idLic": str(COACH["idLic"])},
        ], separators=(",", ":")),
        "horses": json.dumps([
            {"idCompo": CHEVAL["idCompo"], "idHorse": str(CHEVAL["idHorse"])},
        ], separators=(",", ":")),
    }, separators=(",", ":"))
    resp = session.get(
        f"{BASE_URL}/composition/translate/{ffe_quote(translate_payload)}",
        headers=headers, timeout=20,
    )
    print(f"      HTTP {resp.status_code}")
    if resp.text:
        print(f"      Réponse : {resp.text[:200]}")
    time.sleep(0.5)

    # ── Étape 3 : check ──
    print(f"    → Étape 3 : GET composition/check ...")
    check_url = (
        f"{BASE_URL}/composition/check/{epreuve_id}"
        f"/{ffe_quote(cavaliers_json)}"
        f"/{ffe_quote(chevaux_json)}"
        f"/1/%7B%7D"
        f"?enterId=0"
        f"&params={ffe_quote(params_json)}"
        f"&"
    )
    print(f"      URL check : ...check/{epreuve_id}/[cavaliers]/[chevaux]/1/{{}}?...")

    resp = session.get(check_url, headers=headers, timeout=20)
    print(f"      HTTP {resp.status_code}")
    print(f"      Réponse : {resp.text[:300]}")

    if resp.status_code != 200:
        return False, f"Check HTTP {resp.status_code}"

    time.sleep(1)

    # ── Étape 4 : requestEnter ──
    # URL format exact du navigateur :
    # /engagement/requestEnter/{id}/{cavaliers}/{chevaux}/{}/{params}/0?checkMore=1
    #                                                    ^^
    #                                              objet vide {}
    print(f"    → Étape 4 : GET engagement/requestEnter ...")
    enter_url = (
        f"{BASE_URL}/engagement/requestEnter/{epreuve_id}"
        f"/{ffe_quote(cavaliers_json)}"
        f"/{ffe_quote(chevaux_json)}"
        f"/%7B%7D"
        f"/{ffe_quote(params_json)}"
        f"/0?checkMore=1"
    )
    print(f"      URL enter : ...requestEnter/{epreuve_id}/[cavaliers]/[chevaux]/{{}}/{{params}}/0?checkMore=1")

    resp = session.get(enter_url, headers=headers, timeout=20)
    print(f"      HTTP {resp.status_code}")
    print(f"      Réponse : {resp.text[:500]}")

    if resp.status_code != 200:
        return False, f"RequestEnter HTTP {resp.status_code} : {resp.text[:200]}"

    try:
        data = resp.json()
        if isinstance(data, dict) and (data.get("error") or data.get("erreur")):
            return False, f"Refusé : {resp.text[:300]}"
    except:
        pass

    return True, "Engagement envoyé !"


def send_ntfy(title, message, priority=5):
    if not NTFY_TOPIC:
        return
    try:
        requests.post("https://ntfy.sh", json={
            "topic": NTFY_TOPIC, "title": title, "message": message,
            "priority": priority, "tags": ["horse", "test_tube"],
        }, timeout=10)
    except:
        pass


def main():
    print("=" * 60)
    print(f"  TEST v4 — Concours {CONCOURS_ID}")
    print(f"  Fix : ajout segment {{}} + encoding :, safe")
    print(f"  Épreuve 1 → {EPREUVES[1]}")
    print(f"  Épreuve 2 → {EPREUVES[2]}")
    print("=" * 60)

    session = requests.Session()
    session.headers.update({
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9",
    })

    if not login_sso(session):
        send_ntfy("❌ TEST v4 : Login échoué", "Impossible de se connecter")
        sys.exit(1)

    resultats = []
    for num, eid in EPREUVES.items():
        print(f"\n{'='*50}")
        print(f"  🏇 Épreuve #{num} (id={eid})")
        print(f"{'='*50}")
        success, message = do_engagement(session, eid, num)
        print(f"\n  {'✅' if success else '❌'} {message}")
        resultats.append((num, success, message))
        time.sleep(2)

    print(f"\n{'='*60}")
    print(f"  📊 RÉSUMÉ")
    print(f"{'='*60}")
    for num, success, msg in resultats:
        print(f"  {'✅' if success else '❌'} Épreuve #{num} : {msg}")

    resume = "\n".join(f"{'✅' if s else '❌'} Épr. #{n} : {m}" for n, s, m in resultats)
    send_ntfy("🧪 TEST v4 terminé", f"Concours {CONCOURS_ID}\n{resume}")
    print(f"\n✅ Test terminé.\n")


if __name__ == "__main__":
    main()
