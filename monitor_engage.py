#!/usr/bin/env python3
"""
FFE Compet Monitor + Auto-Engagement — Version Cloud (GitHub Actions)
======================================================================
Vérifie le statut des concours, notifie via ntfy, et engage
automatiquement par requêtes HTTP (sans navigateur ni Selenium).

Flow :
  1. Login SSO CAS → cookie PHP_FFECOMPET_SESSION
  2. Scrape statut de chaque concours surveillé
  3. Si ouverture détectée → notification ntfy
  4. Si engagement configuré pour ce concours → auto-engagement HTTP
  5. Notification du résultat (succès / échec)
"""

import json
import os
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ─── Configuration ────────────────────────────────────────────────────────────
BASE_URL       = "https://ffecompet.ffe.com"
SSO_URL        = "https://sso.ffe.com"
CONCOURS_URL   = f"{BASE_URL}/concours/"
CONCOURS_FILE  = Path(__file__).parent / "concours.json"
ENGAGE_FILE    = Path(__file__).parent / "engagements.json"
STATE_FILE     = Path(os.environ.get("STATE_FILE", str(Path(__file__).parent / "state.json")))

NTFY_TOPIC   = os.environ.get("NTFY_TOPIC", "")
FFE_USERNAME = os.environ.get("FFE_USERNAME", "")
FFE_PASSWORD = os.environ.get("FFE_PASSWORD", "")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
}

XHR_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": BROWSER_UA,
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
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


# ═══════════════════════════════════════════════════════════════════════════════
# LOGIN SSO CAS
# ═══════════════════════════════════════════════════════════════════════════════
def login_sso(session: requests.Session) -> bool:
    """
    Authentification via CAS SSO (sso.ffe.com) → cookie PHP_FFECOMPET_SESSION.

    Flow :
      1. GET sso.ffe.com/login?service=... → page HTML avec token 'execution'
      2. POST username + password + execution  → 302 avec ticket
      3. Redirect vers ffecompet.ffe.com/login?ticket=ST-xxx → cookie posé
    """
    if not FFE_USERNAME or not FFE_PASSWORD:
        print("  ⚠ FFE_USERNAME / FFE_PASSWORD non configurés — mode monitoring seul")
        return False

    print("\n🔐 Connexion SSO CAS...")

    # URL du service FFE Compet (destination après login)
    service_url = (
        f"{BASE_URL}/login?_target_path="
        f"{urllib.parse.quote(BASE_URL + '/', safe='')}"
    )
    login_page_url = (
        f"{SSO_URL}/login?service="
        f"{urllib.parse.quote(service_url, safe='')}"
    )

    # ── Étape 1 : récupérer le token 'execution' ──
    try:
        resp = session.get(login_page_url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"  ❌ Impossible d'accéder à la page de login SSO : {e}")
        return False

    soup = BeautifulSoup(resp.text, "html.parser")
    exec_input = soup.find("input", {"name": "execution"})
    if not exec_input or not exec_input.get("value"):
        print("  ❌ Token 'execution' introuvable (page SSO modifiée ?)")
        return False

    execution_token = exec_input["value"]
    print(f"  ✓ Token execution récupéré ({len(execution_token)} chars)")

    # ── Étape 2 : POST credentials ──
    form_data = {
        "username":  FFE_USERNAME,
        "password":  FFE_PASSWORD,
        "execution": execution_token,
        "_eventId":  "submit",
    }

    try:
        resp = session.post(
            login_page_url,
            data=form_data,
            timeout=20,
            allow_redirects=True,
        )
    except Exception as e:
        print(f"  ❌ Erreur POST login : {e}")
        return False

    # ── Vérification cookie ──
    if "PHP_FFECOMPET_SESSION" in session.cookies.get_dict():
        sid = session.cookies["PHP_FFECOMPET_SESSION"]
        print(f"  ✅ Connecté ! (session: {sid[:10]}...)")
        return True

    # Parfois le redirect ne suit pas automatiquement — vérifier l'URL
    if "ticket=" in (resp.url or ""):
        try:
            resp2 = session.get(resp.url, timeout=20)
        except Exception:
            pass
        if "PHP_FFECOMPET_SESSION" in session.cookies.get_dict():
            sid = session.cookies["PHP_FFECOMPET_SESSION"]
            print(f"  ✅ Connecté ! (session: {sid[:10]}...)")
            return True

    print("  ❌ Login échoué — cookie PHP_FFECOMPET_SESSION absent")
    print(f"     URL finale : {resp.url}")
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# SCRAPING CONCOURS (inchangé)
# ═══════════════════════════════════════════════════════════════════════════════
def detect_status(text: str) -> tuple:
    lower = text.lower()
    for pattern, (code, label) in STATUS_MAP.items():
        if pattern in lower:
            return code, label
    return "INCONNU", "Inconnu"


def fetch_concours(session: requests.Session, cid: str) -> dict:
    url = f"{CONCOURS_URL}{cid}"
    info = {
        "id": cid, "url": url, "name": "",
        "status_code": "INCONNU", "status": "Inconnu", "ouvert": False,
        "dates": "", "cloture": "", "error": None,
    }

    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        info["error"] = str(e)[:120]
        return info

    soup = BeautifulSoup(resp.text, "html.parser")

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

    if info["status_code"] == "INCONNU":
        code, label = detect_status(soup.get_text(separator=" "))
        info["status_code"] = code
        info["status"] = label
        info["ouvert"] = (code == "OUVERT")

    if not info["name"]:
        t = soup.find("title")
        if t:
            info["name"] = t.get_text(strip=True).split("-")[0].strip()

    return info


# ═══════════════════════════════════════════════════════════════════════════════
# DISCOVERY ÉPREUVE ID
# ═══════════════════════════════════════════════════════════════════════════════
def discover_epreuve_ids(session: requests.Session, concours_id: str) -> dict:
    """
    Récupère le mapping {epreuve_num: epreuve_id_interne} pour un concours.

    Essaie plusieurs méthodes :
      1. Fetcher la page d'engagement et parser le HTML/JS
      2. Appeler les endpoints selecteurs (test/contest)
    
    Retourne un dict {1: "300254985", 2: "300254986", ...}
    """
    mapping = {}

    # ── Méthode 1 : Parser la page d'engagement ──
    # La page /engagement/{concours_id} contient souvent la liste des épreuves
    try:
        url = f"{BASE_URL}/engagement/{concours_id}"
        resp = session.get(url, timeout=20)
        if resp.status_code == 200:
            # Chercher les liens /engagement/{concours}/{num} et les IDs associés
            # Souvent dans du JS inline ou des data-attributes
            text = resp.text

            # Pattern 1 : liens vers les épreuves avec IDs dans le JS
            # ex: epreuves = [{id: 300254985, num: 1, ...}, ...]
            for m in re.finditer(
                r'"id"\s*:\s*(\d{6,}).*?"num(?:ero)?"\s*:\s*(\d+)', text
            ):
                eid, num = m.group(1), int(m.group(2))
                mapping[num] = eid

            if mapping:
                return mapping

            # Pattern 2 : chercher dans les URLs de la page
            for m in re.finditer(
                r'/composition/check/(\d{6,})/', text
            ):
                # On ne sait pas le numéro, mais on a au moins un ID
                pass

            # Pattern 3 : chercher data-id dans les éléments
            soup = BeautifulSoup(text, "html.parser")
            for elem in soup.select("[data-id]"):
                eid = elem.get("data-id", "")
                if eid.isdigit() and len(eid) >= 6:
                    # Essayer de trouver le numéro associé
                    num_text = elem.get_text(strip=True)
                    m = re.match(r"(\d+)", num_text)
                    if m:
                        mapping[int(m.group(1))] = eid

            if mapping:
                return mapping

    except Exception as e:
        print(f"    ⚠ Méthode 1 (page engagement) échouée : {e}")

    # ── Méthode 2 : Essayer chaque épreuve individuellement ──
    # Fetch /engagement/{concours}/{num} et chercher l'ID dans la page
    for num in range(1, 20):  # Max 20 épreuves
        try:
            url = f"{BASE_URL}/engagement/{concours_id}/{num}"
            resp = session.get(url, timeout=10, allow_redirects=False)
            if resp.status_code != 200:
                break  # Plus d'épreuves

            # Chercher l'ID interne dans le HTML/JS de cette page
            for pattern in [
                r'/composition/check/(\d{6,})/',
                r'/requestEnter/(\d{6,})/',
                r'epreuveId["\s:=]+["\']?(\d{6,})',
                r'"epreuve_id"\s*:\s*(\d{6,})',
                r"'epreuve_id'\s*:\s*(\d{6,})",
                r'data-epreuve[_-]?id["\s=:]+["\']?(\d{6,})',
            ]:
                m = re.search(pattern, resp.text)
                if m:
                    mapping[num] = m.group(1)
                    break

            time.sleep(0.5)

        except Exception:
            break

    # ── Méthode 3 : Endpoint selecteurs/test ──
    if not mapping:
        try:
            test_url = f"{BASE_URL}/concours/selecteurs/test"
            headers = {
                **XHR_HEADERS,
                "Referer": f"{BASE_URL}/engagement/{concours_id}/1",
            }
            resp = session.post(test_url, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                # Essayer de parser la réponse pour trouver les epreuve IDs
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "id" in item:
                            num = item.get("numero", item.get("num", item.get("position")))
                            if num is not None:
                                mapping[int(num)] = str(item["id"])
                elif isinstance(data, dict):
                    for key, val in data.items():
                        if isinstance(val, list):
                            for item in val:
                                if isinstance(item, dict) and "id" in item:
                                    num = item.get("numero", item.get("num"))
                                    if num is not None:
                                        mapping[int(num)] = str(item["id"])
        except Exception as e:
            print(f"    ⚠ Méthode 3 (selecteurs) échouée : {e}")

    return mapping


# ═══════════════════════════════════════════════════════════════════════════════
# AUTO-ENGAGEMENT HTTP
# ═══════════════════════════════════════════════════════════════════════════════
def url_encode_json(obj) -> str:
    """Encode un objet en JSON compact puis URL-encode pour les paths."""
    raw = json.dumps(obj, separators=(",", ":"))
    return urllib.parse.quote(raw, safe="")


def do_engagement(
    session: requests.Session,
    epreuve_id: str,
    concours_id: str,
    epreuve_num: int,
    config: dict,
) -> tuple:
    """
    Exécute un engagement via requêtes HTTP.

    Séquence capturée du navigateur :
      1. GET /composition/check/{epreuve_id}/[cavaliers]/[chevaux]/1/{}
             ?enterId=0&params={action:enter,...}
      2. GET /engagement/requestEnter/{epreuve_id}/[cavaliers]/[chevaux]
             /{params}/0?checkMore=1

    Retourne (succès: bool, message: str)
    """
    cavalier = config["cavalier"]
    coach    = config.get("coach", {"idCompo": "25_1_30_1_0", "idLic": ""})
    cheval   = config["cheval"]

    cavaliers_list = [
        {"idCompo": cavalier["idCompo"], "idLic": str(cavalier["idLic"])},
        {"idCompo": coach["idCompo"],    "idLic": str(coach.get("idLic", ""))},
    ]
    chevaux_list = [
        {"idCompo": cheval["idCompo"], "idHorse": str(cheval["idHorse"])},
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

    # ── Étape 1 : /composition/check — pré-validation ──
    print(f"    → Étape 1/2 : composition/check ...")
    check_path = (
        f"/composition/check/{epreuve_id}"
        f"/{urllib.parse.quote(cavaliers_json, safe='')}"
        f"/{urllib.parse.quote(chevaux_json, safe='')}"
        f"/1/%7B%7D"
    )
    check_url = (
        f"{BASE_URL}{check_path}"
        f"?enterId=0"
        f"&params={urllib.parse.quote(params_json, safe='')}"
        f"&"
    )

    try:
        resp = session.get(check_url, headers=headers, timeout=20)
    except Exception as e:
        return False, f"Erreur réseau check : {e}"

    if resp.status_code != 200:
        return False, f"Check HTTP {resp.status_code}"

    try:
        check_data = resp.json()
    except Exception:
        check_data = {"raw": resp.text[:300]}

    print(f"      Réponse check : {json.dumps(check_data, ensure_ascii=False)[:200]}")

    # Vérifier s'il y a des erreurs bloquantes dans la réponse
    check_str = json.dumps(check_data).lower()
    if "error" in check_str and "true" in check_str:
        return False, f"Check bloqué : {json.dumps(check_data, ensure_ascii=False)[:200]}"

    time.sleep(random.uniform(0.5, 1.5))

    # ── Étape 2 : /engagement/requestEnter — engagement réel ──
    print(f"    → Étape 2/2 : engagement/requestEnter ...")
    enter_path = (
        f"/engagement/requestEnter/{epreuve_id}"
        f"/{urllib.parse.quote(cavaliers_json, safe='')}"
        f"/{urllib.parse.quote(chevaux_json, safe='')}"
        f"/{urllib.parse.quote(params_json, safe='')}"
        f"/0"
    )
    enter_url = f"{BASE_URL}{enter_path}?checkMore=1"

    try:
        resp = session.get(enter_url, headers=headers, timeout=20)
    except Exception as e:
        return False, f"Erreur réseau requestEnter : {e}"

    if resp.status_code != 200:
        return False, f"RequestEnter HTTP {resp.status_code}"

    try:
        enter_data = resp.json()
    except Exception:
        enter_data = {"raw": resp.text[:300]}

    print(f"      Réponse enter : {json.dumps(enter_data, ensure_ascii=False)[:200]}")

    # Analyser la réponse pour déterminer le succès
    enter_str = json.dumps(enter_data).lower()
    if "error" in enter_str and "true" in enter_str:
        return False, f"Engagement refusé : {json.dumps(enter_data, ensure_ascii=False)[:200]}"

    return True, f"Engagement envoyé (epreuve_id={epreuve_id})"


# ═══════════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS NTFY
# ═══════════════════════════════════════════════════════════════════════════════
def send_ntfy(title: str, message: str, url: str = "", priority: int = 5, tags=None):
    if not NTFY_TOPIC:
        print("  ⚠ NTFY_TOPIC non configuré — notification ignorée")
        return False

    try:
        payload = {
            "topic": NTFY_TOPIC,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": tags or ["horse", "trophy"],
        }
        if url:
            payload["click"] = url
            payload["actions"] = [
                {"action": "view", "label": "Ouvrir la fiche", "url": url}
            ]
        resp = requests.post("https://ntfy.sh", json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"  ⚠ Erreur ntfy : {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# GESTION D'ÉTAT
# ═══════════════════════════════════════════════════════════════════════════════
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
        data = json.load(f)
    return data.get("concours", [])


def load_engagements() -> dict:
    """Charge la config d'engagements auto."""
    if not ENGAGE_FILE.exists():
        return {}
    try:
        with open(ENGAGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠ Erreur lecture {ENGAGE_FILE} : {e}")
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATION ENGAGEMENTS
# ═══════════════════════════════════════════════════════════════════════════════
def process_engagements(session: requests.Session, concours_id: str, state: dict):
    """
    Si des engagements sont configurés pour ce concours, les exécuter.
    Gère la découverte d'epreuve_id, les anti-doublons, et les limites.
    """
    engage_config = load_engagements()
    if not engage_config:
        print("    ℹ Pas de fichier engagements.json — engagement auto désactivé")
        return

    max_per_run = engage_config.get("max_engagements_par_run", 5)
    cavalier    = engage_config.get("cavalier")
    coach       = engage_config.get("coach", {"idCompo": "25_1_30_1_0", "idLic": ""})
    cheval      = engage_config.get("cheval")

    if not cavalier or not cheval:
        print("    ❌ Cavalier ou cheval non configuré dans engagements.json")
        return

    # Trouver les engagements pour ce concours
    engagements = engage_config.get("engagements", [])
    target = None
    for eng in engagements:
        if str(eng.get("concours")) == str(concours_id):
            target = eng
            break

    if not target:
        print(f"    ℹ Pas d'engagement configuré pour le concours {concours_id}")
        return

    epreuves_voulues = target.get("epreuves", [])
    if not epreuves_voulues:
        print(f"    ℹ Pas d'épreuves configurées pour le concours {concours_id}")
        return

    # Anti-doublon : vérifier ce qui a déjà été engagé
    engaged_key = f"engaged_{concours_id}"
    already_engaged = state.get(engaged_key, [])

    epreuves_restantes = [e for e in epreuves_voulues if e not in already_engaged]
    if not epreuves_restantes:
        print(f"    ✓ Toutes les épreuves déjà engagées pour {concours_id}")
        return

    # Limite par run
    epreuves_restantes = epreuves_restantes[:max_per_run]

    print(f"\n  🎯 AUTO-ENGAGEMENT : {len(epreuves_restantes)} épreuve(s) à engager")
    print(f"     Concours : {concours_id}")
    print(f"     Épreuves : {epreuves_restantes}")

    # ── Découverte des epreuve_id internes ──
    print(f"\n    🔍 Découverte des IDs internes...")
    id_mapping = discover_epreuve_ids(session, concours_id)

    if id_mapping:
        print(f"    ✓ Mapping trouvé : {id_mapping}")
    else:
        print(f"    ⚠ Impossible de découvrir les IDs internes automatiquement")
        # Vérifier s'il y a des IDs manuels dans la config
        manual_ids = target.get("epreuve_ids", {})
        if manual_ids:
            id_mapping = {int(k): str(v) for k, v in manual_ids.items()}
            print(f"    ✓ IDs manuels utilisés : {id_mapping}")
        else:
            send_ntfy(
                title="⚠️ Engagement impossible",
                message=(
                    f"Concours {concours_id} ouvert mais impossible\n"
                    f"de trouver les IDs internes des épreuves.\n"
                    f"Ajoutez 'epreuve_ids' dans engagements.json\n"
                    f"ou engagez manuellement."
                ),
                priority=4,
                tags=["warning", "horse"],
            )
            return

    # ── Engagement pour chaque épreuve ──
    engagement_count = 0
    config = {"cavalier": cavalier, "coach": coach, "cheval": cheval}

    for epreuve_num in epreuves_restantes:
        epreuve_id = id_mapping.get(epreuve_num)
        if not epreuve_id:
            print(f"\n    ⚠ Épreuve #{epreuve_num} : ID interne inconnu — ignorée")
            send_ntfy(
                title=f"⚠️ ID inconnu épreuve #{epreuve_num}",
                message=f"Concours {concours_id}, épreuve #{epreuve_num}\nID interne non trouvé.",
                priority=3,
                tags=["warning"],
            )
            continue

        print(f"\n    🏇 Engagement épreuve #{epreuve_num} (id={epreuve_id})...")

        success, message = do_engagement(
            session, epreuve_id, concours_id, epreuve_num, config
        )

        if success:
            print(f"    ✅ {message}")
            engagement_count += 1

            # Anti-doublon : enregistrer
            already_engaged.append(epreuve_num)
            state[engaged_key] = already_engaged

            send_ntfy(
                title=f"✅ Engagement réussi !",
                message=(
                    f"Concours {concours_id} — Épreuve #{epreuve_num}\n"
                    f"Cavalier : {cavalier.get('idLic')}\n"
                    f"Cheval : {cheval.get('idHorse')}\n"
                    f"{message}"
                ),
                url=f"{BASE_URL}/engagement/{concours_id}/{epreuve_num}",
                priority=5,
                tags=["white_check_mark", "horse"],
            )
        else:
            print(f"    ❌ {message}")
            send_ntfy(
                title=f"❌ Engagement échoué",
                message=(
                    f"Concours {concours_id} — Épreuve #{epreuve_num}\n"
                    f"{message}"
                ),
                priority=5,
                tags=["x", "horse"],
            )

        # Pause entre engagements
        if epreuve_num != epreuves_restantes[-1]:
            time.sleep(random.uniform(1, 3))

    print(f"\n    📊 Résultat : {engagement_count}/{len(epreuves_restantes)} engagement(s) réussi(s)")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    print(f"{'='*60}")
    print(f"  FFE Compet Monitor + Auto-Engagement — {now}")
    print(f"{'='*60}")

    # ── Charger config ──
    concours_list = load_concours_list()
    if not concours_list:
        print("Aucun concours à surveiller. Modifiez concours.json.")
        return

    state = load_state()
    session = requests.Session()
    session.headers.update(HEADERS)

    # ── Login SSO (nécessaire pour l'engagement auto) ──
    logged_in = login_sso(session)

    print(f"\n📋 {len(concours_list)} concours à vérifier")

    # Jitter initial
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
        name = info["name"] or cid

        if info.get("error"):
            print(f"  ⚠ {name} — Erreur : {info['error']}")
        else:
            print(f"  {'🟢' if info['ouvert'] else '⚪'} {name} — {info['status']}")

        # ══ Détection d'ouverture ══
        if info["ouvert"] and not was_ouvert:
            print(f"\n  🎉🎉🎉 OUVERTURE DÉTECTÉE : {name} !")
            print(f"      Dates : {info['dates']}")
            print(f"      Clôture : {info['cloture']}")
            print(f"      URL : {info['url']}")

            # Notification d'ouverture
            ok = send_ntfy(
                title="🏇 Engagements OUVERTS !",
                message=(
                    f"{name}\n"
                    f"Concours N° {cid}\n"
                    f"Dates : {info.get('dates') or '?'}\n"
                    f"Clôture : {info.get('cloture') or '?'}"
                ),
                url=info["url"],
                priority=5,
            )
            if ok:
                print(f"  📱 Notification ouverture envoyée !")

            # ══ Auto-engagement ══
            if logged_in:
                process_engagements(session, cid, state)
            else:
                print("  ⚠ Non connecté — engagement auto impossible")
                send_ntfy(
                    title="⚠️ Engagement auto impossible",
                    message=(
                        f"{name} est OUVERT mais le login a échoué.\n"
                        f"Engagez-vous manuellement !\n"
                        f"Concours N° {cid}"
                    ),
                    url=info["url"],
                    priority=5,
                    tags=["warning", "horse"],
                )

            print()

        elif info["status_code"] != prev_code and prev_code != "INCONNU":
            print(f"      ↻ Changement : {prev.get('status', '?')} → {info['status']}")

        # ── Mise à jour état ──
        if info["status_code"] != prev_code or info["ouvert"] != was_ouvert:
            changes = True

        state[cid] = {
            "status_code": info["status_code"],
            "status": info["status"],
            "ouvert": info["ouvert"],
            "name": info["name"],
            "dates": info["dates"],
            "cloture": info["cloture"],
            "last_check": now,
        }

        time.sleep(random.uniform(1, 4))

    # ── Sauvegarder ──
    save_state(state)
    if changes:
        print(f"\n💾 État mis à jour.")

    print(f"\n✅ Terminé.\n")


if __name__ == "__main__":
    main()
