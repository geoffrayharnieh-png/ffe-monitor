#!/usr/bin/env python3
"""
FFE Compet Monitor + Auto-Engagement — Version Cloud (GitHub Actions)
======================================================================
Vérifie le statut des concours, notifie via ntfy, et engage
automatiquement par requêtes HTTP (sans navigateur ni Selenium).

Supporte cavalier/cheval différent par épreuve.

Flow engagement validé :
  0. GET /engagement/{concours}/{num}          → contexte serveur
  1. POST /concours/selecteurs/test            → chargement sélecteurs
  1b.POST /concours/selecteurs/contest         → chargement concours
  2. GET /composition/translate/{json}          → résolution noms
  3. GET /composition/check/{id}/...            → pré-validation
  4. GET /engagement/requestEnter/{id}/...      → engagement réel
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


# ─── Helpers ──────────────────────────────────────────────────────────────────
def ffe_quote(s):
    """URL-encode comme le navigateur : garde :, non-encodés."""
    return urllib.parse.quote(s, safe=":,")


# ═══════════════════════════════════════════════════════════════════════════════
# LOGIN SSO CAS
# ═══════════════════════════════════════════════════════════════════════════════
def login_sso(session: requests.Session) -> bool:
    if not FFE_USERNAME or not FFE_PASSWORD:
        print("  ⚠ FFE_USERNAME / FFE_PASSWORD non configurés — mode monitoring seul")
        return False

    print("\n🔐 Connexion SSO CAS...")

    service_url = (
        f"{BASE_URL}/login?_target_path="
        f"{urllib.parse.quote(BASE_URL + '/', safe='')}"
    )
    login_page_url = (
        f"{SSO_URL}/login?service="
        f"{urllib.parse.quote(service_url, safe='')}"
    )

    try:
        resp = session.get(login_page_url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"  ❌ Accès page SSO impossible : {e}")
        return False

    soup = BeautifulSoup(resp.text, "html.parser")
    exec_input = soup.find("input", {"name": "execution"})
    if not exec_input or not exec_input.get("value"):
        print("  ❌ Token 'execution' introuvable")
        return False

    print(f"  ✓ Token execution récupéré ({len(exec_input['value'])} chars)")

    try:
        resp = session.post(login_page_url, data={
            "username":  FFE_USERNAME,
            "password":  FFE_PASSWORD,
            "execution": exec_input["value"],
            "_eventId":  "submit",
        }, timeout=20, allow_redirects=True)
    except Exception as e:
        print(f"  ❌ POST login échoué : {e}")
        return False

    if "PHP_FFECOMPET_SESSION" in session.cookies.get_dict():
        print(f"  ✅ Connecté ! (session: {session.cookies['PHP_FFECOMPET_SESSION'][:10]}...)")
        return True

    if "ticket=" in (resp.url or ""):
        try:
            session.get(resp.url, timeout=20)
        except Exception:
            pass
        if "PHP_FFECOMPET_SESSION" in session.cookies.get_dict():
            print(f"  ✅ Connecté !")
            return True

    print("  ❌ Login échoué — cookie absent")
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# SCRAPING CONCOURS
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
# DÉCOUVERTE EPREUVE ID
# ═══════════════════════════════════════════════════════════════════════════════
def discover_epreuve_id(session: requests.Session, concours_id: str, epreuve_num: int) -> str:
    """
    Découvre l'ID interne d'une épreuve en visitant sa page d'engagement.
    Retourne l'ID ou "" si introuvable.
    """
    url = f"{BASE_URL}/engagement/{concours_id}/{epreuve_num}"
    try:
        resp = session.get(url, timeout=20)
        if resp.status_code != 200:
            return ""
    except Exception:
        return ""

    text = resp.text
    candidates = set()

    # Pattern 1 : dans les URLs composition/check ou requestEnter
    for m in re.finditer(r'/(?:composition/check|requestEnter)/(\d{9,})', text):
        eid = m.group(1)
        if not eid.startswith("20"):
            candidates.add(eid)

    if candidates:
        return candidates.pop()

    # Pattern 2 : variables JS
    for pattern in [
        r'epreuveId\s*[=:]\s*["\']?(\d{9,})',
        r'epreuve_id\s*[=:]\s*["\']?(\d{9,})',
        r'idEpreuve\s*[=:]\s*["\']?(\d{9,})',
        r'"id"\s*:\s*(\d{9,})',
    ]:
        for m in re.finditer(pattern, text):
            eid = m.group(1)
            if not eid.startswith("20"):
                candidates.add(eid)

    if candidates:
        return candidates.pop()

    # Pattern 3 : data-attributes
    soup = BeautifulSoup(text, "html.parser")
    for attr in ["data-id", "data-epreuve-id", "data-epreuve_id"]:
        for elem in soup.select(f"[{attr}]"):
            val = elem.get(attr, "")
            if val.isdigit() and len(val) >= 9 and not val.startswith("20"):
                return val

    # Pattern 4 : tout nombre de 9+ chiffres commençant par 3
    for m in re.finditer(r'\b(3\d{8,})\b', text):
        eid = m.group(1)
        if eid != concours_id:
            return eid

    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# ENGAGEMENT HTTP (flow validé)
# ═══════════════════════════════════════════════════════════════════════════════
def do_engagement(
    session: requests.Session,
    epreuve_id: str,
    concours_id: str,
    epreuve_num: int,
    cavalier: dict,
    cheval: dict,
    coach: dict,
) -> tuple:
    """
    Exécute un engagement en simulant le flow exact du navigateur.
    Retourne (succès: bool, message: str).
    """
    referer = f"{BASE_URL}/engagement/{concours_id}/{epreuve_num}"
    headers = {**XHR_HEADERS, "Referer": referer}

    cavaliers_json = json.dumps([
        {"idCompo": cavalier["idCompo"], "idLic": str(cavalier["idLic"])},
        {"idCompo": coach["idCompo"],    "idLic": str(coach.get("idLic", ""))},
    ], separators=(",", ":"))

    chevaux_json = json.dumps([
        {"idCompo": cheval["idCompo"], "idHorse": str(cheval["idHorse"])},
    ], separators=(",", ":"))

    params_json = json.dumps({
        "action": "enter",
        "justControls": 0,
        "checkPenalty": 0,
        "checkAttestation": 0,
    }, separators=(",", ":"))

    translate_payload = json.dumps({
        "licensees": cavaliers_json,
        "horses": chevaux_json,
    }, separators=(",", ":"))

    try:
        # ── Étape 0 : Visiter la page (contexte serveur) ──
        resp = session.get(
            f"{BASE_URL}/engagement/{concours_id}/{epreuve_num}",
            timeout=20,
        )
        if resp.status_code != 200:
            return False, f"Page engagement HTTP {resp.status_code}"
        time.sleep(0.5)

        # ── Étape 1 : selecteurs/test ──
        session.post(f"{BASE_URL}/concours/selecteurs/test", headers=headers, timeout=20)
        time.sleep(0.3)

        # ── Étape 1b : selecteurs/contest ──
        session.post(f"{BASE_URL}/concours/selecteurs/contest", headers=headers, timeout=20)
        time.sleep(0.3)

        # ── Étape 2 : translate ──
        session.get(
            f"{BASE_URL}/composition/translate/{ffe_quote(translate_payload)}",
            headers=headers, timeout=20,
        )
        time.sleep(0.3)

        # ── Étape 3 : check (pré-validation) ──
        check_url = (
            f"{BASE_URL}/composition/check/{epreuve_id}"
            f"/{ffe_quote(cavaliers_json)}"
            f"/{ffe_quote(chevaux_json)}"
            f"/1/%7B%7D"
            f"?enterId=0"
            f"&params={ffe_quote(params_json)}"
            f"&"
        )
        resp = session.get(check_url, headers=headers, timeout=20)
        if resp.status_code != 200:
            return False, f"Check HTTP {resp.status_code}"

        time.sleep(0.5)

        # ── Étape 4 : requestEnter (engagement réel!) ──
        enter_url = (
            f"{BASE_URL}/engagement/requestEnter/{epreuve_id}"
            f"/{ffe_quote(cavaliers_json)}"
            f"/{ffe_quote(chevaux_json)}"
            f"/%7B%7D"
            f"/{ffe_quote(params_json)}"
            f"/0?checkMore=1"
        )
        resp = session.get(enter_url, headers=headers, timeout=20)

        if resp.status_code != 200:
            return False, f"RequestEnter HTTP {resp.status_code} : {resp.text[:200]}"

        try:
            data = resp.json()
            if isinstance(data, dict):
                if data.get("error") or data.get("erreur"):
                    return False, f"Refusé : {json.dumps(data, ensure_ascii=False)[:200]}"
                if data.get("redirectTo"):
                    return True, f"Engagement validé → {data['redirectTo']}"
        except Exception:
            pass

        return True, f"Engagement envoyé (HTTP 200)"

    except Exception as e:
        return False, f"Erreur : {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS NTFY
# ═══════════════════════════════════════════════════════════════════════════════
def send_ntfy(title: str, message: str, url: str = "", priority: int = 5, tags=None):
    if not NTFY_TOPIC:
        print("  ⚠ NTFY_TOPIC non configuré")
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
            payload["actions"] = [{"action": "view", "label": "Ouvrir", "url": url}]
        resp = requests.post("https://ntfy.sh", json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"  ⚠ Erreur ntfy : {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# GESTION D'ÉTAT + CONFIG
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
        return json.load(f).get("concours", [])


def load_engagements() -> dict:
    if not ENGAGE_FILE.exists():
        return {}
    try:
        with open(ENGAGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠ Erreur {ENGAGE_FILE} : {e}")
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATION ENGAGEMENTS
# ═══════════════════════════════════════════════════════════════════════════════
def process_engagements(session: requests.Session, concours_id: str, state: dict):
    engage_config = load_engagements()
    if not engage_config:
        print("    ℹ Pas de fichier engagements.json")
        return

    max_per_run     = engage_config.get("max_engagements_par_run", 5)
    cavalier_defaut = engage_config.get("cavalier_defaut")
    coach_defaut    = engage_config.get("coach_defaut", {"idCompo": "25_1_30_1_0", "idLic": ""})
    cheval_defaut   = engage_config.get("cheval_defaut")

    if not cavalier_defaut or not cheval_defaut:
        print("    ❌ cavalier_defaut ou cheval_defaut manquant dans engagements.json")
        return

    # Trouver la config pour ce concours
    target = None
    for eng in engage_config.get("engagements", []):
        if str(eng.get("concours")) == str(concours_id):
            target = eng
            break

    if not target:
        print(f"    ℹ Pas d'engagement configuré pour {concours_id}")
        return

    epreuves_config = target.get("epreuves", [])
    if not epreuves_config:
        return

    # Anti-doublon
    engaged_key = f"engaged_{concours_id}"
    already_engaged = state.get(engaged_key, [])

    # Filtrer les épreuves déjà engagées
    epreuves_todo = []
    for ep in epreuves_config:
        # Support ancien format (juste un numéro) et nouveau format (dict)
        if isinstance(ep, int):
            ep = {"num": ep}
        num = ep.get("num")
        if num and num not in already_engaged:
            epreuves_todo.append(ep)

    if not epreuves_todo:
        print(f"    ✓ Toutes les épreuves déjà engagées pour {concours_id}")
        return

    epreuves_todo = epreuves_todo[:max_per_run]
    manual_ids = target.get("epreuve_ids", {})

    print(f"\n  🎯 AUTO-ENGAGEMENT : {len(epreuves_todo)} épreuve(s)")
    print(f"     Concours : {concours_id}")

    engagement_count = 0

    for ep in epreuves_todo:
        epreuve_num = ep["num"]

        # ── Résoudre cavalier / cheval / coach (défaut ou surcharge) ──
        cavalier = ep.get("cavalier", cavalier_defaut)
        cheval   = ep.get("cheval",   cheval_defaut)
        coach    = ep.get("coach",    coach_defaut)

        # ── Trouver l'ID interne ──
        epreuve_id = manual_ids.get(str(epreuve_num), "")

        if not epreuve_id:
            print(f"\n    🔍 Découverte ID épreuve #{epreuve_num}...")
            epreuve_id = discover_epreuve_id(session, concours_id, epreuve_num)

        if not epreuve_id:
            print(f"    ⚠ Épreuve #{epreuve_num} : ID interne introuvable")
            send_ntfy(
                title=f"⚠️ ID inconnu épreuve #{epreuve_num}",
                message=f"Concours {concours_id}\nAjoutez epreuve_ids manuellement.",
                priority=4, tags=["warning", "horse"],
            )
            continue

        cav_label = cavalier.get("idLic", "?")
        che_label = cheval.get("idHorse", "?")
        print(f"\n    🏇 Épreuve #{epreuve_num} (id={epreuve_id})")
        print(f"       Cavalier: {cav_label} | Cheval: {che_label}")

        # ── Engagement ──
        success, message = do_engagement(
            session, epreuve_id, concours_id, epreuve_num,
            cavalier, cheval, coach,
        )

        if success:
            print(f"    ✅ {message}")
            engagement_count += 1
            already_engaged.append(epreuve_num)
            state[engaged_key] = already_engaged

            send_ntfy(
                title=f"✅ Engagement réussi !",
                message=(
                    f"Concours {concours_id} — Épreuve #{epreuve_num}\n"
                    f"Cavalier : {cav_label}\n"
                    f"Cheval : {che_label}\n"
                    f"{message}"
                ),
                url=f"{BASE_URL}/engagement/{concours_id}/{epreuve_num}",
                tags=["white_check_mark", "horse"],
            )
        else:
            print(f"    ❌ {message}")
            send_ntfy(
                title=f"❌ Engagement échoué",
                message=f"Concours {concours_id} — Épreuve #{epreuve_num}\n{message}",
                tags=["x", "horse"],
            )

        if ep != epreuves_todo[-1]:
            time.sleep(random.uniform(1, 3))

    print(f"\n    📊 {engagement_count}/{len(epreuves_todo)} engagement(s) réussi(s)")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    print(f"{'='*60}")
    print(f"  FFE Compet Monitor + Auto-Engagement — {now}")
    print(f"{'='*60}")

    concours_list = load_concours_list()
    if not concours_list:
        print("Aucun concours à surveiller.")
        return

    state = load_state()
    session = requests.Session()
    session.headers.update(HEADERS)

    logged_in = login_sso(session)

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
        name = info["name"] or cid

        if info.get("error"):
            print(f"  ⚠ {name} — Erreur : {info['error']}")
        else:
            print(f"  {'🟢' if info['ouvert'] else '⚪'} {name} — {info['status']}")

        # ══ Ouverture détectée ══
        if info["ouvert"] and not was_ouvert:
            print(f"\n  🎉🎉🎉 OUVERTURE DÉTECTÉE : {name} !")

            send_ntfy(
                title="🏇 Engagements OUVERTS !",
                message=(
                    f"{name}\n"
                    f"Concours N° {cid}\n"
                    f"Dates : {info.get('dates') or '?'}\n"
                    f"Clôture : {info.get('cloture') or '?'}"
                ),
                url=info["url"],
            )
            print(f"  📱 Notification ouverture envoyée !")

            if logged_in:
                process_engagements(session, cid, state)
            else:
                print("  ⚠ Non connecté — engagement auto impossible")
                send_ntfy(
                    title="⚠️ Engagement auto impossible",
                    message=f"{name} est OUVERT mais login échoué.\nEngagez-vous manuellement !",
                    url=info["url"], tags=["warning", "horse"],
                )

            print()

        elif info["status_code"] != prev_code and prev_code != "INCONNU":
            print(f"      ↻ {prev.get('status', '?')} → {info['status']}")

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

    save_state(state)
    if changes:
        print(f"\n💾 État mis à jour.")

    print(f"\n✅ Terminé.\n")


if __name__ == "__main__":
    main()
