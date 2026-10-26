#!/usr/bin/env python3
"""
Scrape de fiches Google Maps via l'API Searlo (/search/places).

- 3 clés (SEARLO_KEY_1..3), 1 worker par clé, une requête à la fois par clé
- Chaque fiche a au moins 30 s (60 s par défaut) pour répondre avant timeout + nouvel essai
- 1 crédit par requête, tous les résultats (limit=4) sont écrits, sans filtre
- Reprise automatique : les SIREN déjà présents dans agences.csv sont ignorés
- Logs en direct (flush à chaque ligne)
"""
import csv
import os
import queue
import re
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests

# ───────────────────────── Config ─────────────────────────
API_URL = os.getenv("SEARLO_URL", "https://api.searlo.tech/api/v1/search/places")
LOCATION_PARAM = os.getenv("SEARLO_LOCATION_PARAM", "location")
INPUT_FILE = os.getenv("INPUT_FILE", "data/entreprises.csv")
OUTPUT_FILE = os.getenv("OUTPUT_FILE", "agences.csv")
MIN_INTERVAL = float(os.getenv("MIN_INTERVAL", "1"))         # pause mini entre 2 requêtes d'une même clé (anti rate limit)
REQUEST_TIMEOUT = max(30.0, float(os.getenv("REQUEST_TIMEOUT", "60")))  # attente max d'une fiche, jamais < 30 s
MAX_RUNTIME = float(os.getenv("MAX_RUNTIME_MIN", "335")) * 60  # arrêt propre avant la limite GitHub (6 h)
LIMIT = 4
PAGE = 1
GL = "fr"
HL = "fr"

FIELDS = ["Siren", "Nom", "Adresse", "Rating", "Rating count", "Category",
          "Phone number", "Website", "Domain", "Horaires", "PlaceId"]

LOG_LOCK = threading.Lock()
WRITE_LOCK = threading.Lock()
STOP = threading.Event()


def log(msg):
    with LOG_LOCK:
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ───────────────────────── Helpers extraction ─────────────────────────
def norm(k):
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def pick(d, *names):
    """Première valeur non vide parmi plusieurs noms de champs possibles."""
    if not isinstance(d, dict):
        return ""
    idx = {norm(k): v for k, v in d.items()}
    for n in names:
        v = idx.get(norm(n))
        if v not in (None, "", [], {}):
            return v
    return ""


def flat(v):
    if isinstance(v, dict):
        return ", ".join(flat(x) for x in v.values() if x not in (None, ""))
    if isinstance(v, list):
        return ", ".join(flat(x) for x in v if x not in (None, ""))
    return "" if v is None else str(v)


def to_int(v):
    s = re.sub(r"[^\d]", "", str(v))
    return s if s else ""


def domain_of(url):
    if not url:
        return ""
    u = url if "://" in url else "http://" + url
    host = urlparse(u).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


# ───────────────────────── Horaires ─────────────────────────
DAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
DAY_ALIASES = {
    "monday": 0, "mon": 0, "lundi": 0, "lun": 0,
    "tuesday": 1, "tue": 1, "mardi": 1, "mar": 1,
    "wednesday": 2, "wed": 2, "mercredi": 2, "mer": 2,
    "thursday": 3, "thu": 3, "jeudi": 3, "jeu": 3,
    "friday": 4, "fri": 4, "vendredi": 4, "ven": 4,
    "saturday": 5, "sat": 5, "samedi": 5, "sam": 5,
    "sunday": 6, "sun": 6, "dimanche": 6, "dim": 6,
}
TIME_RANGE = re.compile(
    r"(\d{1,2})(?:[:h](\d{2}))?\s*([ap]\.?m\.?)?\s*(?:-|–|—|to|à)\s*"
    r"(\d{1,2})(?:[:h](\d{2}))?\s*([ap]\.?m\.?)?", re.I)


def day_index(s):
    return DAY_ALIASES.get(str(s).strip().lower().rstrip(".:"))


def to_min(h, m, ap):
    h, m = int(h), int(m or 0)
    if ap:
        ap = ap.lower().replace(".", "")
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
    return h * 60 + m


def conv(h, m, ap):
    mins = to_min(h, m, ap)
    hh, mm = divmod(mins, 60)
    return f"{hh}h" if mm == 0 else f"{hh}h{mm:02d}"


def fmt_slots(text):
    t = re.sub(r"[\u202f\u2009\u00a0]", " ", str(text)).strip()
    if re.search(r"closed|ferm", t, re.I):
        return "fermé"
    if re.search(r"24\s*(hours|h)|open 24|24/24|ouvert 24", t, re.I):
        return "24h/24"
    out = []
    for m in TIME_RANGE.finditer(t):
        h1, m1, a1, h2, m2, a2 = m.groups()
        if a2 and not a1:
            a1 = a2
            if to_min(h1, m1, a1) > to_min(h2, m2, a2):
                a1 = "am" if a2.lower().startswith("p") else "pm"
        out.append(f"{conv(h1, m1, a1)}-{conv(h2, m2, a2)}")
    return " ".join(out) if out else t


def collect_hours(v, days):
    if isinstance(v, dict):
        for k, val in v.items():
            i = day_index(k)
            if i is not None:
                days[i] = fmt_slots(flat(val))
        dk = pick(v, "day", "dayOfWeek", "weekday")
        hk = pick(v, "hours", "time", "times", "openingHours", "slots")
        if dk and hk != "":
            i = day_index(dk)
            if i is not None:
                days[i] = fmt_slots(flat(hk))
        for k, val in v.items():
            if day_index(k) is None and isinstance(val, (list, dict)):
                collect_hours(val, days)
    elif isinstance(v, list):
        for x in v:
            collect_hours(x, days)
    elif isinstance(v, str):
        for part in re.split(r"[;\n|]", v):
            m = re.match(r"\s*([A-Za-zéèûôÉ\.]+)\s*[:\-]?\s*(.*)", part)
            if m and day_index(m.group(1)) is not None:
                days[day_index(m.group(1))] = fmt_slots(m.group(2))


def normalize_hours(v):
    if v in (None, "", [], {}):
        return ""
    days = {}
    collect_hours(v, days)
    if not days:
        return flat(v)
    return ", ".join(f"{DAYS_FR[i]} {days[i]}" for i in sorted(days))


# ───────────────────────── Parsing réponse ─────────────────────────
def find_list(data):
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for k in ("places", "local", "results", "organic", "items", "data", "maps"):
        v = data.get(k)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
        if isinstance(v, dict):
            sub = find_list(v)
            if sub:
                return sub
    for v in data.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    return []


def build_row(task, p):
    website = flat(pick(p, "website", "url", "site", "link"))
    cat = pick(p, "category", "categories", "type", "types", "mainCategory")
    if isinstance(cat, list):
        cat = ", ".join(flat(c) if not isinstance(c, dict) else flat(pick(c, "name", "title")) for c in cat)
    return {
        "Siren": task["siren"],
        "Nom": flat(pick(p, "title", "name")),
        "Adresse": flat(pick(p, "address", "formattedAddress", "fullAddress", "vicinity")),
        "Rating": flat(pick(p, "rating", "stars", "score")),
        "Rating count": to_int(pick(p, "ratingCount", "reviews", "reviewsCount", "reviewCount",
                                    "userRatingsTotal", "ratingsTotal", "numberOfReviews")),
        "Category": flat(cat),
        "Phone number": flat(pick(p, "phone", "phoneNumber", "telephone", "tel", "internationalPhoneNumber")),
        "Website": website,
        "Domain": domain_of(website),
        "Horaires": normalize_hours(pick(p, "hours", "openingHours", "workingHours", "workHours",
                                         "schedule", "operatingHours", "weekdayText")),
        "PlaceId": flat(pick(p, "placeId", "place_id", "id", "cid", "fid", "dataId", "ludocid")),
    }


def empty_row(task):
    r = {f: "" for f in FIELDS}
    r["Siren"], r["Nom"] = task["siren"], task["nom"]
    return r


# ───────────────────────── I/O CSV ─────────────────────────
def load_input():
    with open(INPUT_FILE, encoding="utf-8-sig", newline="") as f:
        head = f.readline()
        f.seek(0)
        delim = ";" if head.count(";") > head.count(",") else ","
        rd = csv.DictReader(f, delimiter=delim)
        rows = []
        for r in rd:
            low = {norm(k): (v or "").strip() for k, v in r.items() if k}
            siren = low.get("siren", "")
            nom = low.get("nom") or low.get("name") or low.get("denomination") or ""
            loc = low.get("location") or low.get("adresse") or low.get("ville") or low.get("localisation") or ""
            if not nom:
                continue
            rows.append({"siren": siren, "nom": nom, "location": loc,
                         "key": siren or norm(nom + loc)})
    seen, out = set(), []
    for t in rows:
        if t["key"] not in seen:
            seen.add(t["key"])
            out.append(t)
    return out


def load_done():
    if not os.path.exists(OUTPUT_FILE) or os.path.getsize(OUTPUT_FILE) == 0:
        return set()
    with open(OUTPUT_FILE, encoding="utf-8-sig", newline="") as f:
        return {r.get("Siren", "") for r in csv.DictReader(f) if r.get("Siren")}


def init_output():
    if not os.path.exists(OUTPUT_FILE) or os.path.getsize(OUTPUT_FILE) == 0:
        with open(OUTPUT_FILE, "w", encoding="utf-8-sig", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()


def save_rows(rows):
    with WRITE_LOCK:
        with open(OUTPUT_FILE, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writerows(rows)
            f.flush()
            os.fsync(f.fileno())


# ───────────────────────── Appel API ─────────────────────────
RAW_LOGGED = threading.Event()
HEADERS_LOGGED = threading.Event()
LOC_IN_QUERY = threading.Event()  # activé si l'API refuse le paramètre location (400)


def build_params(task):
    if task["location"] and LOC_IN_QUERY.is_set():
        q = f"{task['nom']} {task['location']}"
        params = {"q": q}
    else:
        params = {"q": task["nom"]}
        if task["location"]:
            params[LOCATION_PARAM] = task["location"]
    params.update({"limit": LIMIT, "page": PAGE, "gl": GL, "hl": HL})
    return params


def fetch(tag, key, task):
    for attempt in range(1, 6):
        params = build_params(task)
        if STOP.is_set():
            return "retry", None
        try:
            r = requests.get(API_URL, params=params, headers={"x-api-key": key}, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            log(f"{tag} ⚠ réseau ({e.__class__.__name__}), essai {attempt}/5")
            STOP.wait(10 * attempt)
            continue
        sc = r.status_code
        if not HEADERS_LOGGED.is_set():
            rl = {k: v for k, v in r.headers.items() if k.lower().startswith(("x-ratelimit", "retry-after"))}
            if rl:
                HEADERS_LOGGED.set()
                log(f"{tag} 📊 en-têtes rate limit : {rl}")
        if sc == 200:
            try:
                return "ok", r.json()
            except ValueError:
                log(f"{tag} ⚠ réponse non JSON : {r.text[:200]}")
                STOP.wait(5)
                continue
        if sc == 429:
            wait = int(r.headers.get("Retry-After", "60") or 60)
            log(f"{tag} ⏳ 429 rate limit, pause {wait}s (essai {attempt}/5)")
            STOP.wait(wait)
            continue
        if sc in (401, 402, 403):
            return "dead", f"HTTP {sc} {r.text[:200]}"
        if sc == 400 and "location" in params and not LOC_IN_QUERY.is_set():
            LOC_IN_QUERY.set()
            log(f"{tag} ⚠ 400 avec le paramètre {LOCATION_PARAM} ({r.text[:150]}) → "
                f"bascule : la localisation est ajoutée à la requête (q = nom + adresse)")
            continue
        if sc in (400, 404, 422):
            return "bad", f"HTTP {sc} {r.text[:200]}"
        log(f"{tag} ⚠ HTTP {sc}, essai {attempt}/5")
        STOP.wait(10 * attempt)
    return "retry", None


# ───────────────────────── Worker ─────────────────────────
class State:
    def __init__(self, total):
        self.total = total
        self.done = 0
        self.alive = 0
        self.lock = threading.Lock()
        self.bad_streak = 0
        self.start = time.monotonic()
        self.deadline = self.start + MAX_RUNTIME


def worker(idx, key, q, st, n_keys):
    tag = f"K{idx}"
    last = 0.0
    # décalage des workers pour lisser les requêtes
    if STOP.wait((idx - 1) * MIN_INTERVAL / n_keys):
        return
    while not STOP.is_set():
        if time.monotonic() > st.deadline:
            log(f"{tag} ⏱ temps max atteint, arrêt propre")
            break
        try:
            task = q.get_nowait()
        except queue.Empty:
            break

        wait = MIN_INTERVAL - (time.monotonic() - last)
        if wait > 0 and STOP.wait(wait):
            q.put(task)
            break
        if time.monotonic() > st.deadline:
            q.put(task)
            log(f"{tag} ⏱ temps max atteint, arrêt propre")
            break

        last = time.monotonic()
        t0 = time.monotonic()
        status, data = fetch(tag, key, task)
        dt = time.monotonic() - t0

        if status == "dead":
            log(f"{tag} ✗ clé inutilisable ({data}) → worker arrêté, tâche remise en file")
            q.put(task)
            with st.lock:
                st.alive -= 1
            return
        if status == "retry":
            log(f"{tag} ✗ échec temporaire {task['siren']} {task['nom']} (sera repris au prochain run)")
            continue

        if status == "bad":
            with st.lock:
                st.bad_streak += 1
                streak = st.bad_streak
            log(f"{tag} ✗ requête refusée {task['siren']} {task['nom']} : {data} (non enregistrée, reprise au prochain run)")
            if streak >= 5:
                log("⛔ 5 requêtes refusées d'affilée : paramètres probablement invalides, arrêt du run")
                STOP.set()
                return
            continue
        else:
            with st.lock:
                st.bad_streak = 0
            if not RAW_LOGGED.is_set():
                RAW_LOGGED.set()
                log(f"{tag} 🔎 1ʳᵉ réponse brute : {str(data)[:1500]}")
            places = find_list(data)[:LIMIT]
            rows = [build_row(task, p) for p in places] or [empty_row(task)]
            n_res = len(places)
            credits = f" | coût={data.get('credits')} crédit(s)" if isinstance(data, dict) and data.get("credits") is not None else ""

        save_rows(rows)
        with st.lock:
            st.done += 1
            done, alive = st.done, max(st.alive, 1)
        remaining = st.total - done
        per_item = (time.monotonic() - st.start) / done
        eta = timedelta(seconds=int(remaining * per_item))
        log(f"{tag} ✓ {task['siren']} | {task['nom']} | {task['location']} | "
            f"{n_res} résultat(s) | {dt:.1f}s{credits} | {done}/{st.total} | ETA ~{eta}")


# ───────────────────────── Main ─────────────────────────
def set_output(name, value):
    path = os.getenv("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


def main():
    keys = [os.getenv(f"SEARLO_KEY_{i}", "").strip() for i in (1, 2, 3)]
    keys = [k for k in keys if k]
    if not keys:
        log("✗ Aucune clé : définis SEARLO_KEY_1, SEARLO_KEY_2, SEARLO_KEY_3")
        sys.exit(1)

    tasks = load_input()
    init_output()
    done = load_done()
    todo = [t for t in tasks if t["key"] not in done]
    if any(not t["siren"] for t in tasks):
        log("⚠ certaines lignes n'ont pas de SIREN : la reprise ne fonctionnera pas pour elles")
    log(f"▶ {len(tasks)} entreprises en entrée, {len(done)} déjà faites, {len(todo)} à traiter")
    log(f"▶ {len(keys)} clé(s), pause {MIN_INTERVAL:.0f}s entre 2 requêtes d'une clé, "
        f"timeout par fiche {REQUEST_TIMEOUT:.0f}s")
    if not todo:
        set_output("remaining", 0)
        set_output("alive", len(keys))
        set_output("done_this_run", 0)
        log("✔ Rien à faire")
        return

    q = queue.Queue()
    for t in todo:
        q.put(t)

    st = State(len(todo))
    st.alive = len(keys)

    def stop_handler(signum, frame):
        log("⛔ signal d'arrêt reçu, arrêt propre…")
        STOP.set()

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    threads = [threading.Thread(target=worker, args=(i + 1, k, q, st, len(keys)), daemon=True)
               for i, k in enumerate(keys)]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        time.sleep(1)

    remaining = st.total - st.done
    log(f"■ Fin du run : {st.done} traitées, {remaining} restantes, {st.alive} clé(s) encore utilisables")
    set_output("remaining", remaining)
    set_output("alive", st.alive)
    set_output("done_this_run", st.done)


if __name__ == "__main__":
    main()
