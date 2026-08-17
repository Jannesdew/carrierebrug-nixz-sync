#!/usr/bin/env python3
"""
Airtable -> Webflow sync, standalone (no chat/MCP involved).

Reads "Opdrachten" records from Airtable that don't have a Webflow Item ID
yet, creates the corresponding CMS item in the Webflow "Opdracht listings"
collection, and writes the new Webflow item id (+ sync status) back to
Airtable.

This is a deliberately temporary bridge: it "pours" NIXZ data into the
existing (older) Webflow CMS shape. A dedicated CMS/collection built for the
new NIXZ-based data model is planned for later.

Only CREATE is handled (no update-detection): once an opdracht is posted its
content essentially never changes, and closed/expired opdrachten never reach
Airtable in the first place (filtered upstream in nixz_airtable_sync.py), so
a create-once pattern is sufficient here.

Credentials come from environment variables (GitHub Actions secrets):
  AIRTABLE_TOKEN, WEBFLOW_API_TOKEN
Optional overrides: AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, WEBFLOW_COLLECTION_ID
Publish behaviour: AUTO_PUBLISH=true/false (default true). Pass
--no-publish on the command line to force draft-only for a single run
(used for the very first run, per Jannes' request to review before going live).

For local testing, these can instead be read from airtable_config.json /
webflow_config.json in the same folder (NOT committed to git).
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
import urllib.error

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REQUEST_TIMEOUT = 30
AIRTABLE_WRITE_BATCH = 10
WEBFLOW_PUBLISH_BATCH = 50
WEBFLOW_PAUSE_SECONDS = 0.3

DEFAULT_AIRTABLE_BASE_ID = "appgoJ97eVpLTyQq6"
DEFAULT_AIRTABLE_TABLE_NAME = "Opdrachten"
DEFAULT_WEBFLOW_COLLECTION_ID = "689bb58f2ecf8b74698435b6"

# --------------------------------------------------------------------------
# Static mappings (Webflow option/reference IDs -- from get_collection_details
# on the "Opdracht listings" collection and the "Werkvelden" collection,
# fetched 2026-07-17. If Jannes renames/reorders these options in Webflow,
# these IDs stay valid (Webflow option IDs don't change on rename) but if
# options are deleted and recreated, this mapping needs updating.)
# --------------------------------------------------------------------------

REGIO_OPTION_IDS = {
    "DRENTHE": "d29390360f9f07f7262817b44adf5458",
    "GELDERLAND": "b757b571fe078815d92075abef9f1c8c",
    "GRONINGEN": "2c5acf3b0796e03bf8454f97e09b5e1a",
    "FRIESLAND": "bf96426d059b1e2da9fe244a5064bb13",
    "FLEVOLAND": "3feb64f539ad5f6dcb6fd56b71627cbc",
    "LIMBURG": "ed3fa9702e70a2554035d53abbb1880f",
    "NOORD_BRABANT": "edb07ffaadbb2535ec44981fc6576a6d",
    "NOORD_HOLLAND": "7eccacf94743a70e432aa6307f0fa428",
    "OVERIJSSEL": "f6221b790854c176cde78f6ad325652f",
    "UTRECHT": "2629eb5f021c08bd86d6f81a4fe48d0d",
    "ZEELAND": "936ead898aee41bfb597e5c60d52d0d8",
    "ZUID_HOLLAND": "765721787ed3ee1c7da33a0327b1f107",
}

OPLEIDING_OPTION_IDS = {
    "MBO": "a3f2c023bfe24184ed60f7baf24c9cf7",
    "HBO": "f5a3dd39d467f7f1b6dc71072737d61d",
    "WO": "8d503a98bf3bf5d61588ae60d03acb7c",
}

INHUURTYPE_OPTION_IDS = {
    "zzp": "deae40cea419e5a576cd58515fff0602",
    "detachering": "05122fc9e975ac64cf00945752436dc9",
}

# NIXZ category enum -> Werkveld (Tags for webflow) collection item id.
# MANAGEMENT/LOGISTICS/MARKETING are best-guess mappings flagged for review.
CATEGORY_TO_WERKVELD_ID = {
    "CONSTRUCTION": "689baf089b62e512800aa8e4",       # Bouwkunde/Civiele Techniek
    "COMMUNICATION": "689baf0afcce13fb4fe70f20",       # Voorlichting/Communicatie
    "CULTURE": "689baf0aedc79c91d98656b9",             # Sport/Recreatie/Wetenschap/Cultuur
    "SALES": "689baf0a5eda152aa416ac39",               # Verkoop/Inkoop
    "FINANCE": "689baf092a4828df6193aea6",              # Financieel/Economisch
    "HUMAN_RESOURCES": "689baf093de484f797204583",     # P&O/HR
    "LEGAL": "689baf09b91eeabc5504c838",                # Juridisch
    "SPATIAL": "689baf0966aa8543a2d291cf",              # Ruimtelijke ordening/Milieu
    "SOCIAL": "689baf09734d9bed3b4edd75",               # Sociaal domein
    "HEALTHCARE": "689baf0a37b58fcb98bc5ff2",           # Welzijn/Zorg/Jeugd
    "ADMINISTRATION": "689baf0873df3c8c4af47c26",       # Administratief/Secretarieel
    "MANAGEMENT": "689baf097d0cd3b95eb43de4",           # Projectmanagement (best guess)
    "SECURITY": "689baf0919e204759aa5ec34",             # Openbare orde en veiligheid
    "LOGISTICS": "68a907314dc24cda1386fd04",            # Mobiliteit (best guess)
    "EDUCATION": "689baf0910962cdde42d9c59",            # Onderwijs
    "TECHNOLOGY": "689baf08dfacef0f5269428e",           # Automatisering/ICT
    "FACILITIES": "689baf09923b5069e870a563",           # Dienstverlening/Facilitair
    "ICT": "689baf08dfacef0f5269428e",                  # Automatisering/ICT
    "MARKETING": "689baf0afcce13fb4fe70f20",            # Voorlichting/Communicatie (best guess)
}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def _load_json_if_exists(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def get_config():
    airtable_local = _load_json_if_exists(os.path.join(SCRIPT_DIR, "airtable_config.json"))
    webflow_local = _load_json_if_exists(os.path.join(SCRIPT_DIR, "webflow_config.json"))

    config = {
        "airtable_token": os.environ.get("AIRTABLE_TOKEN") or airtable_local.get("token"),
        "airtable_base_id": os.environ.get("AIRTABLE_BASE_ID") or airtable_local.get("base_id", DEFAULT_AIRTABLE_BASE_ID),
        "airtable_table_name": os.environ.get("AIRTABLE_TABLE_NAME") or airtable_local.get("table_name", DEFAULT_AIRTABLE_TABLE_NAME),
        "webflow_token": os.environ.get("WEBFLOW_API_TOKEN") or webflow_local.get("token"),
        "webflow_collection_id": os.environ.get("WEBFLOW_COLLECTION_ID") or webflow_local.get("collection_id", DEFAULT_WEBFLOW_COLLECTION_ID),
        "auto_publish": os.environ.get("AUTO_PUBLISH", "true").lower() != "false",
    }
    if "--no-publish" in sys.argv:
        config["auto_publish"] = False

    missing = [k for k in ("airtable_token", "webflow_token") if not config[k]]
    if missing:
        sys.exit(f"Ontbrekende credentials: {missing}. Zet ze als env vars of in "
                  f"airtable_config.json / webflow_config.json.")
    return config


# --------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------

def http_json(url, method="GET", headers=None, body=None, verbose=False):
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
            if verbose:
                print(f"    -> HTTP {resp.status} raw response: {raw[:2000]!r}", flush=True)
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                print(f"    !! Kon respons niet als JSON lezen ({url}): {raw[:500]!r}", flush=True)
                return None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"    !! HTTP {e.code} bij {method} {url}: {err_body[:1000]}", flush=True)
        return None
    except urllib.error.URLError as e:
        print(f"    !! Netwerkfout bij {url}: {e}", flush=True)
        return None


# --------------------------------------------------------------------------
# Airtable
# --------------------------------------------------------------------------

def airtable_fetch_unsynced(base_url_root, headers, table_name):
    """Records where 'Webflow Item ID' is still empty."""
    import urllib.parse
    records = []
    offset = None
    while True:
        params = {
            "filterByFormula": "{Webflow Item ID} = ''",
            "pageSize": 100,
        }
        if offset:
            params["offset"] = offset
        url = f"{base_url_root}/{urllib.parse.quote(table_name)}?{urllib.parse.urlencode(params)}"
        result = http_json(url, headers=headers)
        if not result:
            break
        records.extend(result.get("records", []))
        offset = result.get("offset")
        if not offset:
            break
    return records


def airtable_update_records(base_url_root, headers, table_name, updates):
    """updates: list of {"id": recId, "fields": {...}}"""
    url = f"{base_url_root}/{table_name.replace(' ', '%20')}"
    for i in range(0, len(updates), AIRTABLE_WRITE_BATCH):
        batch = updates[i:i + AIRTABLE_WRITE_BATCH]
        http_json(url, method="PATCH", headers=headers, body={"records": batch})
        time.sleep(0.25)


# --------------------------------------------------------------------------
# Webflow
# --------------------------------------------------------------------------

def slugify(text, suffix):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = text[:200] if text else "opdracht"
    return f"{text}-{suffix}"


def format_uren(minimum, maximum):
    if minimum and maximum and minimum != maximum:
        return f"{minimum:g}-{maximum:g} uur per week"
    if minimum:
        return f"{minimum:g} uur per week"
    if maximum:
        return f"{maximum:g} uur per week"
    return None


def format_aanvang(startdatum, startdatum_tekst):
    if startdatum:
        return startdatum  # YYYY-MM-DD, laten we as-is; front-end kan formatteren
    return startdatum_tekst


_LI_RE = re.compile(r"<li[^>]*>\s*(.*?)\s*</li>", re.DOTALL | re.IGNORECASE)
_LIST_WRAPPER_RE = re.compile(r"</?(ul|ol)[^>]*>", re.IGNORECASE)


def fix_lists_for_webflow(html):
    """Webflow's RichText API is known to strip/blank out <ul>/<li> list markup
    when creating items via the API (confirmed platform limitation, not fixed
    on Webflow's end as of 2026-07). Work around it by flattening lists into
    plain bullet paragraphs before sending, so the content still shows up."""
    if not html:
        return html
    html = _LI_RE.sub(r"<p>• \1</p>", html)
    html = _LIST_WRAPPER_RE.sub("", html)
    return html


def build_field_data(fields):
    nixz_id = fields.get("NIXZ ID")
    titel = fields.get("Titel") or "Opdracht"

    field_data = {
        "name": titel,
        "slug": slugify(titel, nixz_id),
        "opdrachtgever": fields.get("Opdrachtgever"),
        "sluiting-inschrijving": fields.get("Sluitingsdatum"),
        "urenperweek": format_uren(fields.get("Uren minimum"), fields.get("Uren maximum")),
        "aanvang": format_aanvang(fields.get("Startdatum"), fields.get("Startdatum tekst")),
        "duur": fields.get("Duur"),
        "aantal-professionals": fields.get("Aantal professionals"),
        "verlengingsoptie": fields.get("Verlengingsoptie"),
        "omschrijving-html": fix_lists_for_webflow(fields.get("Beschrijving (Webflow)")),
        "kandidaatomschrijving-html": fix_lists_for_webflow(fields.get("Kandidaatomschrijving (Webflow)")),
        "is-actief": True,
        "external-id": str(nixz_id) if nixz_id is not None else None,
    }

    provincie = fields.get("Provincie")
    if provincie in REGIO_OPTION_IDS:
        field_data["regio"] = REGIO_OPTION_IDS[provincie]

    opleiding = fields.get("Opleiding")
    if opleiding in OPLEIDING_OPTION_IDS:
        field_data["opleiding"] = OPLEIDING_OPTION_IDS[opleiding]

    freelancer = fields.get("Freelancer toegestaan")
    field_data["inhuurtype"] = INHUURTYPE_OPTION_IDS["zzp"] if freelancer == "YES" else INHUURTYPE_OPTION_IDS["detachering"]

    categorie = fields.get("Categorie")
    if categorie in CATEGORY_TO_WERKVELD_ID:
        field_data["tags-for-webflow"] = [CATEGORY_TO_WERKVELD_ID[categorie]]

    # Drop nulls
    return {k: v for k, v in field_data.items() if v is not None}


def webflow_create_item(collection_id, headers, field_data, verbose=False):
    url = f"https://api.webflow.com/v2/collections/{collection_id}/items/bulk"
    body = {"fieldData": field_data, "isDraft": True, "isArchived": False}
    result = http_json(url, method="POST", headers=headers, body=body, verbose=verbose)
    if not result:
        return None
    item_id = result.get("id")
    if item_id:
        return item_id
    # Fallback: some response shapes nest the created item(s) under "items".
    items = result.get("items")
    if items and isinstance(items, list) and items[0].get("id"):
        return items[0]["id"]
    print(f"    !! Onverwachte respons zonder 'id': {json.dumps(result)[:1000]}", flush=True)
    return None


def webflow_publish_items(collection_id, headers, item_ids):
    url = f"https://api.webflow.com/v2/collections/{collection_id}/items/publish"
    for i in range(0, len(item_ids), WEBFLOW_PUBLISH_BATCH):
        batch = item_ids[i:i + WEBFLOW_PUBLISH_BATCH]
        http_json(url, method="POST", headers=headers, body={"itemIds": batch})
        time.sleep(WEBFLOW_PAUSE_SECONDS)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    config = get_config()

    airtable_headers = {"Authorization": f"Bearer {config['airtable_token']}", "Content-Type": "application/json"}
    airtable_base_url_root = f"https://api.airtable.com/v0/{config['airtable_base_id']}"
    webflow_headers = {"Authorization": f"Bearer {config['webflow_token']}", "Content-Type": "application/json"}

    print(f"Publiceren staat: {'AAN' if config['auto_publish'] else 'UIT (concept blijft concept)'}", flush=True)

    print("Airtable-records zonder Webflow Item ID ophalen...", flush=True)
    records = airtable_fetch_unsynced(airtable_base_url_root, airtable_headers, config["airtable_table_name"])
    print(f"Gevonden: {len(records)} nog niet naar Webflow gesynchroniseerd.", flush=True)

    limit = None
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        limit = int(sys.argv[idx + 1])
        records = records[:limit]
        print(f"--limit actief: slechts {len(records)} record(en) deze run.", flush=True)

    if not records:
        print("Niets te doen, klaar.")
        return

    created = []  # (airtable_record_id, webflow_item_id)
    failed = 0
    for i, record in enumerate(records, 1):
        field_data = build_field_data(record["fields"])
        verbose = limit is not None  # volledige respons tonen tijdens gericht testen
        item_id = webflow_create_item(config["webflow_collection_id"], webflow_headers, field_data, verbose=verbose)
        if item_id:
            created.append((record["id"], item_id))
            print(f"  [{i}/{len(records)}] aangemaakt: {field_data.get('name')} -> {item_id}", flush=True)
        else:
            failed += 1
            print(f"  [{i}/{len(records)}] MISLUKT: {field_data.get('name')}", flush=True)
        time.sleep(WEBFLOW_PAUSE_SECONDS)

    if failed:
        print(f"Let op: {failed} item(s) niet aangemaakt (zie foutmeldingen hierboven).", flush=True)

    if not created:
        print("Niets aangemaakt, klaar.")
        return

    if config["auto_publish"]:
        print(f"Publiceren van {len(created)} item(s)...", flush=True)
        webflow_publish_items(config["webflow_collection_id"], webflow_headers, [wid for _, wid in created])
        sync_status = "Gepubliceerd"
    else:
        print("Publiceren overgeslagen (--no-publish of AUTO_PUBLISH=false) — items staan als concept in Webflow.", flush=True)
        sync_status = "Nieuw"

    updates = [
        {"id": rec_id, "fields": {"Webflow Item ID": wid, "Sync status": sync_status}}
        for rec_id, wid in created
    ]
    airtable_update_records(airtable_base_url_root, airtable_headers, config["airtable_table_name"], updates)

    print(f"Klaar. {len(created)} opdrachten naar Webflow gesynchroniseerd "
          f"({sync_status}).")


if __name__ == "__main__":
    main()
