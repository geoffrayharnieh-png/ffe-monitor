#!/usr/bin/env python3
"""
TEST — Engagement réel sur concours 202656038 (épreuves 1 & 2)
================================================================
Script temporaire pour valider le flow HTTP complet.
À SUPPRIMER après le test.
"""

import json
import os
import re
import sys
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup

# ─── Config ──────────────────────────────────────────────────────────────────
BASE_URL     = "https://ffecompet.ffe.com"
SSO_URL      = "https://sso.ffe.com"
NTFY_TOPIC   = os.environ.get("NTFY_TOPIC", "")
FFE_USERNAME = os.environ.get("FFE_USERNAME", "")
FFE_PASSWORD = os.environ.get("FFE_PASSWORD", "")

CONCOURS_ID    = "202656038"
EPREUVES_TEST  = [1, 2]

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


# ─── Login SSO ───────────────────────────────────────────────────────────────
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

    execution_token = exec_input["value"]
    print(f"  ✓ Token execution récupéré ({len(execution_token)} chars)")

    form_data = {
        "username":  FFE_USERNAME,
        "password":  FFE_PASSWORD,
        "execution": execution_token,
        "_eventId":  "submit",
    }

    resp = session.post(login_page_url, data=form_data, timeout=20, allow_redirects=True)

    if "PHP_FFECOMPET_SESSION" in session.cookies.get_dict():
        sid = session.cookies["PHP_FFECOMPET_SESSION"]
        print(f"  ✅ Connecté ! (session: {sid[:10]}...)")
        return True

    if "ticket=" in (resp.url or ""):
        session.get(resp.url, timeout=20)
        if "PHP_FFECOMPET_SESSION" in session.cookies.get_dict():
            sid = session.cookies["PHP_FFECOMPET_SESSION"]
            print(f"  ✅ Connecté ! (session: {sid[:10]}...)")
            return True

    print("  ❌ Login échoué")
    return False


# ─── Découverte epreuve_id ───────────────────────────────────────────────────
def discover_epreuve_ids(session, concours_id):
    """Essaie de trouver les IDs internes des épreuves."""
    print(f"\n🔍 Découverte des IDs internes pour {concours_id}...")
    mapping = {}

    # Méthode 1 : parser la page d'engagement
    try:
        url = f"{BASE_URL}/engagement/{concours_id}"
        resp = session.get(url, timeout=20)
        print(f"  Page /engagement/{concours_id} : HTTP {resp.status_code}")

        if resp.status_code == 200:
            text = resp.text

            # Afficher un extrait pour debug
            print(f"  Taille page : {len(text)} chars")

            # Chercher des patterns d'ID
            for pattern_name, pattern in [
                ("composition/check", r'/composition/check/(\d{6,})/'),
                ("requestEnter", r'/requestEnter/(\d{6,})/'),
                ("epreuveId JS", r'epreuveId["\s:=]+["\']?(\d{6,})'),
                ("data-id", r'data-id["\s=:]+["\']?(\d{6,})'),
                ("id dans JSON", r'"id"\s*:\s*(\d{6,})'),
            ]:
                matches = re.findall(pattern, text)
                if matches:
                    print(f"  ✓ Pattern '{pattern_name}' : {matches}")

    except Exception as e:
        print(f"  ⚠ Erreur page engagement : {e}")

    # Méthode 2 : charger chaque épreuve individuellement
    print(f"\n  Chargement individuel des épreuves...")
    for num in EPREUVES_TEST:
        try:
            url = f"{BASE_URL}/engagement/{concours_id}/{num}"
            resp = session.get(url, timeout=15)
            print(f"\n  Épreuve #{num} : HTTP {resp.status_code}, {len(resp.text)} chars")

            if resp.status_code == 200:
                # Chercher l'ID interne
                for pattern_name, pattern in [
                    ("composition/check", r'/composition/check/(\d{6,})/'),
                    ("requestEnter", r'/requestEnter/(\d{6,})/'),
                    ("epreuveId", r'epreuveId["\s:=]+["\']?(\d{6,})'),
                    ("epreuve_id", r'epreuve_id["\s:=]+["\']?(\d{6,})'),
                    ("idEpreuve", r'idEpreuve["\s:=]+["\']?(\d{6,})'),
                    ("data-epreuve", r'data-epreuve[_-]?id["\s=:]+["\']?(\d{6,})'),
                    ("id 9 chiffres", r'["\'](\d{9})["\']'),
                ]:
                    matches = re.findall(pattern, resp.text)
                    if matches:
                        print(f"    ✓ Pattern '{pattern_name}' : {matches}")
                        if not mapping.get(num):
                            mapping[num] = matches[0]

            time.sleep(0.5)
        except Exception as e:
            print(f"    ⚠ Erreur : {e}")

    # Méthode 3 : endpoint selecteurs/test
    print(f"\n  Essai endpoint selecteurs/test...")
    for num in EPREUVES_TEST:
        try:
            headers = {
                **XHR_HEADERS,
                "Referer": f"{BASE_URL}/engagement/{concours_id}/{num}",
            }
            # D'abord visiter la page pour avoir le contexte
            session.get(f"{BASE_URL}/engagement/{concours_id}/{num}", timeout=15)
            time.sleep(0.5)

            # Appeler test
            test_url = f"{BASE_URL}/concours/selecteurs/test"
            resp = session.post(test_url, headers=headers, timeout=15)
            print(f"  selecteurs/test (ref épr #{num}) : HTTP {resp.status_code}, {len(resp.text)} chars")
            if resp.status_code == 200 and resp.text:
                # Afficher un extrait
                preview = resp.text[:500]
                print(f"    Réponse : {preview}")

                # Chercher des IDs
                try:
                    data = resp.json()
                    if isinstance(data, dict):
                        for key, val in data.items():
                            print(f"    Clé '{key}' : {str(val)[:200]}")
                except:
                    pass

            # Appeler contest
            contest_url = f"{BASE_URL}/concours/selecteurs/contest"
            resp = session.post(contest_url, headers=headers, timeout=15)
            print(f"  selecteurs/contest (ref épr #{num}) : HTTP {resp.status_code}, {len(resp.text)} chars")
            if resp.status_code == 200 and resp.text:
                preview = resp.text[:500]
                print(f"    Réponse : {preview}")

            time.sleep(0.5)
        except Exception as e:
            print(f"    ⚠ Erreur selecteurs : {e}")

    # Méthode 4 : endpoint division
    print(f"\n  Essai endpoint division...")
    try:
        headers = {
            **XHR_HEADERS,
            "Referer": f"{BASE_URL}/engagement/{concours_id}/1",
        }
        div_url = f"{BASE_URL}/concours/selecteurs/division"
        resp = session.get(div_url, headers=headers, timeout=15)
        print(f"  division : HTTP {resp.status_code}, {len(resp.text)} chars")
        if resp.status_code == 200 and resp.text:
            preview = resp.text[:500]
            print(f"    Réponse : {preview}")
    except Exception as e:
        print(f"    ⚠ Erreur division : {e}")

    print(f"\n  📊 Mapping final : {mapping}")
    return mapping


# ─── Engagement ──────────────────────────────────────────────────────────────
def do_engagement(session, epreuve_id, concours_id, epreuve_num):
    """Exécute l'engagement complet (check + requestEnter)."""
    cavaliers_list = [
        {"idCompo": CAVALIER["idCompo"], "idLic": str(CAVALIER["idLic"])},
        {"idCompo": COACH["idCompo"],    "idLic": str(COACH["idLic"])},
    ]
    chevaux_list = [
        {"idCompo": CHEVAL["idCompo"], "idHorse": str(CHEVAL["idHorse"])},
    ]
    params_obj = {
        "action": "enter",
        "justControls": 0,
        "checkPenalty": 0,
        "checkAttestation": 0,
    }

    cavaliers_json = json.dumps(cavaliers_list, separators=(",", ":"))
    chevaux_json   = json.dumps(chevaux_list,   separators=(",", ":"))
    params_json    = json.dumps(params_obj,      separators=(",", ":"))

    referer = f"{BASE_URL}/engagement/{concours_id}/{epreuve_num}"
    headers = {**XHR_HEADERS, "Referer": referer}

    # ── Étape 1 : check ──
    print(f"\n    → Étape 1/2 : composition/check ...")
    check_url = (
        f"{BASE_URL}/composition/check/{epreuve_id}"
        f"/{urllib.parse.quote(cavaliers_json, safe='')}"
        f"/{urllib.parse.quote(chevaux_json, safe='')}"
        f"/1/%7B%7D"
        f"?enterId=0"
        f"&params={urllib.parse.quote(params_json, safe='')}"
        f"&"
    )
    print(f"      URL : {check_url[:150]}...")

    resp = session.get(check_url, headers=headers, timeout=20)
    print(f"      HTTP {resp.status_code}")
    print(f"      Réponse : {resp.text[:500]}")

    if resp.status_code != 200:
        return False, f"Check HTTP {resp.status_code}"

    # Vérifier erreurs bloquantes
    try:
        check_data = resp.json()
        check_str = json.dumps(check_data).lower()
        if "error" in check_str and "true" in check_str:
            return False, f"Check bloqué : {resp.text[:300]}"
    except:
        pass

    time.sleep(1)

    # ── Étape 2 : requestEnter ──
    print(f"\n    → Étape 2/2 : engagement/requestEnter ...")
    enter_url = (
        f"{BASE_URL}/engagement/requestEnter/{epreuve_id}"
        f"/{urllib.parse.quote(cavaliers_json, safe='')}"
        f"/{urllib.parse.quote(chevaux_json, safe='')}"
        f"/{urllib.parse.quote(params_json, safe='')}"
        f"/0?checkMore=1"
    )
    print(f"      URL : {enter_url[:150]}...")

    resp = session.get(enter_url, headers=headers, timeout=20)
    print(f"      HTTP {resp.status_code}")
    print(f"      Réponse : {resp.text[:500]}")

    if resp.status_code != 200:
        return False, f"RequestEnter HTTP {resp.status_code}"

    try:
        enter_data = resp.json()
        enter_str = json.dumps(enter_data).lower()
        if "error" in enter_str and "true" in enter_str:
            return False, f"Engagement refusé : {resp.text[:300]}"
    except:
        pass

    return True, f"Engagement envoyé !"


# ─── Notification ────────────────────────────────────────────────────────────
def send_ntfy(title, message, priority=5):
    if not NTFY_TOPIC:
        return
    try:
        requests.post("https://ntfy.sh", json={
            "topic": NTFY_TOPIC,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": ["horse", "test_tube"],
        }, timeout=10)
    except:
        pass


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"  TEST ENGAGEMENT — Concours {CONCOURS_ID}")
    print(f"  Épreuves : {EPREUVES_TEST}")
    print(f"  Cavalier : Geoffrey HARNIEH (228951)")
    print(f"  Cheval : LOVER D'OZ H (1948585)")
    print("=" * 60)

    if not FFE_USERNAME or not FFE_PASSWORD:
        print("❌ FFE_USERNAME / FFE_PASSWORD non configurés")
        sys.exit(1)

    session = requests.Session()
    session.headers.update({
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9",
    })

    # 1. Login
    if not login_sso(session):
        print("❌ Login échoué — abandon")
        send_ntfy("❌ TEST : Login échoué", "Impossible de se connecter à FFE Compet")
        sys.exit(1)

    # 2. Découverte IDs
    id_mapping = discover_epreuve_ids(session, CONCOURS_ID)

    if not id_mapping:
        print("\n❌ Aucun ID interne trouvé automatiquement.")
        print("   Le script va quand même essayer les endpoints pour debug.")
        send_ntfy(
            "⚠️ TEST : IDs non trouvés",
            f"Concours {CONCOURS_ID}\nImpossible de trouver les IDs internes.\nVérifiez les logs.",
        )
        sys.exit(1)

    # 3. Engagement
    print(f"\n{'='*60}")
    print(f"  🎯 LANCEMENT DES ENGAGEMENTS")
    print(f"{'='*60}")

    resultats = []
    for num in EPREUVES_TEST:
        epreuve_id = id_mapping.get(num)
        if not epreuve_id:
            print(f"\n  ⚠ Épreuve #{num} : ID inconnu — ignorée")
            resultats.append((num, False, "ID inconnu"))
            continue

        print(f"\n  🏇 Épreuve #{num} (id={epreuve_id})")
        success, message = do_engagement(session, epreuve_id, CONCOURS_ID, num)

        if success:
            print(f"  ✅ {message}")
        else:
            print(f"  ❌ {message}")

        resultats.append((num, success, message))
        time.sleep(1)

    # 4. Résumé
    print(f"\n{'='*60}")
    print(f"  📊 RÉSUMÉ")
    print(f"{'='*60}")
    for num, success, msg in resultats:
        emoji = "✅" if success else "❌"
        print(f"  {emoji} Épreuve #{num} : {msg}")

    # Notification résumé
    resume = "\n".join(
        f"{'✅' if s else '❌'} Épr. #{n} : {m}" for n, s, m in resultats
    )
    send_ntfy(
        "🧪 TEST Engagement terminé",
        f"Concours {CONCOURS_ID}\n{resume}",
    )

    print(f"\n✅ Test terminé.\n")


if __name__ == "__main__":
    main()
