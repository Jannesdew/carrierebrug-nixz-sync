#!/usr/bin/env python3
"""
NIXZ -> Airtable sync, standalone (no chat/MCP involved).

Designed to run unattended (e.g. via GitHub Actions on a schedule):
  1. Asks Airtable for the highest "NIXZ ID" already stored (so it's
     self-resuming across runs without needing to persist a cursor file).
  2. Authenticates with NIXZ and fetches every job newer than that ID.
  3. Scrubs known leverancier/platform names out of the free-text
     description fields (case-sensitive whole-word match on the brand
     name, so words like "Select"/"Hero" that are also ordinary words
     don't get mangled).
  4. Writes/updates the records in Airtable directly via the REST API,
     upserting on "NIXZ ID" (Airtable's built-in performUpsert), in
     batches of 10 with a short pause to respect the API rate limit.

Credentials come from environment variables (GitHub Actions secrets):
  NIXZ_USERNAME, NIXZ_PASSWORD, AIRTABLE_TOKEN
Optional overrides: NIXZ_BASE_URL, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME

For local testing, these can instead be read from nixz_config.json /
airtable_config.json in the same folder (NOT committed to git — see
.gitignore). Environment variables always win if set.

Known platform/leverancier values are accumulated in known_platforms.json
(committed to the repo) so scrub coverage only grows over time, even when
NIXZ adds a platform that isn't in their own API documentation (this
happened during testing: FLINTER, CIRCLE_8 and HAERT appear in live data
but are missing from the NIXZ swagger).
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KNOWN_PLATFORMS_PATH = os.path.join(SCRIPT_DIR, "known_platforms.json")

NIXZ_PAGE_SIZE = 100
NIXZ_MAX_PAGES = 1000  # safety cap: 100k records, way above realistic volume
REQUEST_TIMEOUT = 30
AIRTABLE_BATCH_SIZE = 10  # Airtable REST API hard limit per write request
AIRTABLE_PAUSE_SECONDS = 0.25  # keeps us under the 5 req/sec rate limit

PLATFORM_TERM_OVERRIDES = {
    "NEGOMETRIX_3": "Negometrix",
    "CTM_SOLUTION": "CTM Solution",
    "POOLZ_ID": "Poolz",
}
REPLACEMENT = "onze broker"


# --------------------------------------------------------------------------
# Config / credentials
# --------------------------------------------------------------------------

def _load_json_if_exists(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def get_config():
    nixz_local = _load_json_if_exists(os.path.join(SCRIPT_DIR, "nixz_config.json"))
    airtable_local = _load_json_if_exists(os.path.join(SCRIPT_DIR, "airtable_config.json"))

    config = {
        "nixz_username": os.environ.get("NIXZ_USERNAME") or nixz_local.get("username"),
        "nixz_password": os.environ.get("NIXZ_PASSWORD") or nixz_local.get("password"),
        "nixz_base_url": os.environ.get("NIXZ_BASE_URL") or nixz_local.get("base_url", "https://app.nixz.io/api"),
        "airtable_token": os.environ.get("AIRTABLE_TOKEN") or airtable_local.get("token"),
        "airtable_base_id": os.environ.get("AIRTABLE_BASE_ID") or airtable_local.get("base_id", "appgoJ97eVpLTyQq6"),
        "airtable_table_name": os.environ.get("AIRTABLE_TABLE_NAME") or airtable_local.get("table_name", "Opdrachten"),
    }
    missing = [k for k in ("nixz_username", "nixz_password", "airtable_token") if not config[k]]
    if missing:
        sys.exit(f"Ontbrekende credentials: {missing}. Zet ze als env vars of in "
                  f"nixz_config.json / airtable_config.json.")
    return config


# --------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------

def http_json(url, method="GET", headers=None, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        sys.exit(f"HTTP {e.code} calling {url}: {err_body}")
    except urllib.error.URLError as e:
        sys.exit(f"Netwerkfout bij {url}: {e}")


# --------------------------------------------------------------------------
# NIXZ
# --------------------------------------------------------------------------

def nixz_authenticate(base_url, username, password):
    result = http_json(
        f"{base_url}/authenticate",
        method="POST",
        body={"username": username, "password": password},
    )
    token = result.get("id_token") if result else None
    if not token:
        sys.exit("NIXZ authenticatie mislukt: geen id_token in respons")
    return token


def nixz_fetch_jobs(base_url, token, min_created_date):
    """Fetch jobs created after min_created_date (proven to work; unlike
    id.greaterThan, which real testing showed the API appears to ignore —
    it returned 33,000+ historical records instead of only newer ones)."""
    headers = {"Authorization": f"Bearer {token}"}
    jobs = []
    page = 0
    previous_first_id = None
    while True:
        params = f"sort=createdDate,asc&size={NIXZ_PAGE_SIZE}&page={page}"
        if min_created_date is not None:
            params += f"&createdDate.greaterThan={min_created_date}"
        url = f"{base_url}/feed/jobs?{params}"

        t0 = time.time()
        batch = http_json(url, headers=headers)
        elapsed = time.time() - t0

        if not batch:
            print(f"  Pagina {page}: 0 records — klaar.", flush=True)
            break

        first_id = batch[0].get("id")
        print(f"  Pagina {page}: {len(batch)} records in {elapsed:.1f}s "
              f"(totaal: {len(jobs) + len(batch)})", flush=True)

        if first_id is not None and first_id == previous_first_id:
            print("  Waarschuwing: paginering lijkt vast te lopen, gestopt.", flush=True)
            break
        previous_first_id = first_id

        jobs.extend(batch)
        if len(batch) < NIXZ_PAGE_SIZE:
            break
        page += 1
        if page >= NIXZ_MAX_PAGES:
            print(f"  Waarschuwing: veiligheidslimiet van {NIXZ_MAX_PAGES} pagina's bereikt.", flush=True)
            break
    return jobs


# --------------------------------------------------------------------------
# Scrub
# --------------------------------------------------------------------------

def derive_term(raw_value):
    if not raw_value or raw_value == "NONE":
        return None
    if raw_value in PLATFORM_TERM_OVERRIDES:
        return PLATFORM_TERM_OVERRIDES[raw_value]
    words = raw_value.split("_")
    return " ".join(w if w.isdigit() else w.capitalize() for w in words)


def build_patterns(terms):
    scrub_patterns = [re.compile(r"\b" + re.escape(t) + r"s?\b") for t in terms]
    check_patterns = [re.compile(r"\b" + re.escape(t) + r"s?\b", re.IGNORECASE) for t in terms]
    return scrub_patterns, check_patterns


def scrub_text(text, scrub_patterns, check_patterns):
    if not text:
        return text, False
    cleaned = text
    for pattern in scrub_patterns:
        cleaned = pattern.sub(REPLACEMENT, cleaned)
    needs_review = any(p.search(cleaned) for p in check_patterns)
    return cleaned, needs_review


def load_known_platforms():
    data = _load_json_if_exists(KNOWN_PLATFORMS_PATH)
    return set(data.get("platforms", []))


def save_known_platforms(platforms):
    with open(KNOWN_PLATFORMS_PATH, "w", encoding="utf-8") as f:
        json.dump({"platforms": sorted(platforms)}, f, indent=2)


# --------------------------------------------------------------------------
# Record shaping
# --------------------------------------------------------------------------

def to_airtable_fields(job, scrub_patterns, check_patterns):
    description_clean, review_1 = scrub_text(job.get("description"), scrub_patterns, check_patterns)
    candidate_clean, review_2 = scrub_text(job.get("candidateDescription"), scrub_patterns, check_patterns)
    needs_review = review_1 or review_2

    def date_only(value):
        return value[:10] if value else None

    fields = {
        "Titel": job.get("title"),
        "NIXZ ID": job.get("id"),
        "Opdrachtgever": job.get("employer"),
        "Locatie": job.get("location"),
        "Stad": job.get("city"),
        "Provincie": job.get("province"),
        "Categorie": job.get("category"),
        "Status": job.get("status"),
        "Platform (intern)": job.get("platform"),
        "Source (intern)": job.get("source"),
        "Beschrijving (raw)": job.get("description"),
        "Beschrijving (Webflow)": description_clean,
        "Kandidaatomschrijving (raw)": job.get("candidateDescription"),
        "Kandidaatomschrijving (Webflow)": candidate_clean,
        "Nog te controleren": needs_review,
        "Startdatum": date_only(job.get("startDate")),
        "Einddatum": date_only(job.get("endDate")),
        "Sluitingsdatum": date_only(job.get("closingDate")),
        "Duur": job.get("duration"),
        "Uren minimum": job.get("hoursMinimum"),
        "Uren maximum": job.get("hoursMaximum"),
        "Salaris minimum": job.get("salaryMinimum"),
        "Salaris maximum": job.get("salaryMaximum"),
        "Salarisschaal": job.get("salaryScale"),
        "Freelancer toegestaan": job.get("freelancerAllowed"),
        "URL (intern)": job.get("url"),
        "NIXZ createdDate": job.get("createdDate"),
        "Opleiding": (job.get("educations") or [{}])[0].get("level"),
        "Aantal professionals": job.get("maximumCandidates"),
        "Verlengingsoptie": job.get("extensionOption"),
        "Startdatum tekst": job.get("startDateText"),
    }
    # Drop nulls -- Airtable is happier not receiving explicit nulls for
    # fields like singleSelect/date, and it keeps payloads smaller.
    return {k: v for k, v in fields.items() if v is not None}


def is_already_closed(job):
    """Extra vangnet: sla opdrachten over waarvan de sluitingsdatum al voorbij is.
    Jannes wil gesloten opdrachten nooit in Airtable/Webflow (die worden er
    later automatisch uitgehaald na >5 dagen, maar hoeven er niet eens in te komen)."""
    closing = job.get("closingDate")
    if not closing:
        return False
    try:
        closing_dt = datetime.fromisoformat(closing.replace("Z", "+00:00"))
    except ValueError:
        return False
    return closing_dt < datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Airtable
# --------------------------------------------------------------------------

def airtable_get_max_created_date(base_url_root, headers, table_name):
    import urllib.parse
    params = urllib.parse.urlencode({
        "maxRecords": 1,
        "sort[0][field]": "NIXZ createdDate",
        "sort[0][direction]": "desc",
    })
    url = f"{base_url_root}/{urllib.parse.quote(table_name)}?{params}"
    result = http_json(url, headers=headers)
    records = (result or {}).get("records", [])
    if not records:
        return None
    return records[0]["fields"].get("NIXZ createdDate")


def airtable_upsert_batch(base_url_root, headers, table_name, records):
    url = f"{base_url_root}/{table_name.replace(' ', '%20')}"
    body = {
        "performUpsert": {"fieldsToMergeOn": ["NIXZ ID"]},
        "records": records,
        # NIXZ voegt af en toe platformwaarden toe die niet in onze
        # vooraf-ingestelde keuzelijst-opties staan (zie known_platforms.json).
        # typecast laat Airtable die automatisch als nieuwe optie aanmaken
        # i.p.v. de hele batch te weigeren.
        "typecast": True,
    }
    return http_json(url, method="PATCH", headers=headers, body=body)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_since_days_arg():
    if "--since-days" not in sys.argv:
        return None
    idx = sys.argv.index("--since-days")
    try:
        days = int(sys.argv[idx + 1])
    except (IndexError, ValueError):
        sys.exit("Gebruik: --since-days <getal>, bv. --since-days 1")
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def main():
    config = get_config()

    airtable_headers = {
        "Authorization": f"Bearer {config['airtable_token']}",
        "Content-Type": "application/json",
    }
    airtable_base_url_root = f"https://api.airtable.com/v0/{config['airtable_base_id']}"

    since_days_override = parse_since_days_arg()
    if since_days_override:
        max_created_date = since_days_override
        print(f"Testmodus: alleen opdrachten aangemaakt na {max_created_date} "
              f"(negeert de Airtable-cursor).", flush=True)
    else:
        print("Hoogste bekende NIXZ createdDate in Airtable opvragen...", flush=True)
        max_created_date = airtable_get_max_created_date(airtable_base_url_root, airtable_headers, config["airtable_table_name"])
        if max_created_date is None:
            print("Geen bestaande records gevonden — dit haalt ALLES op vanaf het begin. "
                  "Overweeg eerst te testen met --since-days 1 als je niet zeker weet "
                  "hoe groot de historische dataset is.", flush=True)
        else:
            print(f"Nieuwste NIXZ createdDate in Airtable: {max_created_date}. Ophalen wat nieuwer is.", flush=True)

    print("Authenticeren bij NIXZ...", flush=True)
    token = nixz_authenticate(config["nixz_base_url"], config["nixz_username"], config["nixz_password"])
    print("Authenticatie gelukt. Opdrachten ophalen...", flush=True)

    jobs = nixz_fetch_jobs(config["nixz_base_url"], token, max_created_date)
    print(f"Opgehaald: {len(jobs)} nieuwe/gewijzigde opdrachten van NIXZ.", flush=True)

    before_filter = len(jobs)
    jobs = [job for job in jobs if not is_already_closed(job)]
    skipped_closed = before_filter - len(jobs)
    if skipped_closed:
        print(f"Overgeslagen (sluitingsdatum al voorbij): {skipped_closed}", flush=True)

    if not jobs:
        print("Niets nieuws (of alles al gesloten), klaar.")
        return

    known_platforms = load_known_platforms()
    seen_this_run = {job.get("platform") for job in jobs if job.get("platform")}
    new_platforms = seen_this_run - known_platforms
    if new_platforms:
        print(f"Nieuwe platformwaarden geleerd: {sorted(new_platforms)}", flush=True)
    all_platforms = known_platforms | seen_this_run
    save_known_platforms(all_platforms)

    terms = sorted({t for t in (derive_term(p) for p in all_platforms) if t})
    scrub_patterns, check_patterns = build_patterns(terms)
    print(f"Scrub-lijst ({len(terms)} termen): {terms}", flush=True)

    records = [{"fields": to_airtable_fields(job, scrub_patterns, check_patterns)} for job in jobs]
    flagged = sum(1 for r in records if r["fields"].get("Nog te controleren"))
    if flagged:
        print(f"Let op: {flagged} record(en) gemarkeerd als 'Nog te controleren'.", flush=True)

    total_synced = 0
    for i in range(0, len(records), AIRTABLE_BATCH_SIZE):
        batch = records[i:i + AIRTABLE_BATCH_SIZE]
        airtable_upsert_batch(airtable_base_url_root, airtable_headers, config["airtable_table_name"], batch)
        total_synced += len(batch)
        print(f"  Airtable: {total_synced}/{len(records)} weggeschreven", flush=True)
        time.sleep(AIRTABLE_PAUSE_SECONDS)

    print(f"Klaar. {total_synced} opdrachten gesynchroniseerd naar Airtable.")


if __name__ == "__main__":
    main()
