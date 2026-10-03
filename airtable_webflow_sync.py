#!/usr/bin/env python3
"""
Airtable -> Webflow sync, standalone (no chat/MCP involved).
 
Dual-target tijdens de fase2-migratie: leest "Opdrachten" records uit
Airtable en zet ze naar ELK target in sync_targets.json dat (nog) niet
gekoppeld is, elk met zijn eigen Webflow-collectie, token en
tracking-velden in Airtable. Zo kan de nieuwe site (nu nog staging)
al gevuld en getest worden terwijl de oude site live blijft draaien.
 
Na livegang van de nieuwe site: verwijder het "oude-site"-blok uit
sync_targets.json (1 stuk JSON weghalen) -- geen losse code-paden nodig.
 
Elk target heeft zijn EIGEN "Webflow Item ID"-achtig veld in Airtable
(zie item_id_field per target hieronder), zodat de twee targets elkaar
niet in de weg zitten en onafhankelijk hun eigen voortgang bijhouden.
 
Only CREATE is handled per target (geen update-detectie op bestaande
Webflow-items): eenmaal aangemaakt wijzigt de inhoud van een opdracht
zelden, en gesloten/verlopen opdrachten bereiken Airtable niet eens
(gefilterd in nixz_airtable_sync.py). De "is-nieuw"-switch (alleen
v2-mapper) is de uitzondering: die wordt per run opnieuw gezet/
teruggedraaid via sync_is_nieuw_switch(), zie onderaan.
 
Opdrachten ZONDER sluitingsdatum worden nooit naar Webflow gesynchroniseerd
(airtable_fetch_unsynced() filtert ze eruit): zonder sluitingsdatum kan
cleanup_expired_items() nooit bepalen of zo'n opdracht verlopen is, dus
zou die voor altijd in Webflow blijven staan. Komt er later alsnog een
sluitingsdatum bij, dan wordt de opdracht vanzelf bij een volgende run
opgepikt.
 
Opruimen (tegen het Webflow CMS-plafond van 10.000 items):
  - Elke normale run: opdrachten waarvan de Sluitingsdatum meer dan
    EXPIRED_GRACE_DAYS geleden is, worden uit Webflow verwijderd
    (cleanup_expired_items()). De Airtable-rij blijft gewoon bestaan,
    alleen de "Sync status"-kolom wordt op "Verwijderd (verlopen)"
    gezet zodat ie nooit opnieuw wordt aangemaakt.
  - --purge-airtable (losse, niet-automatische actie): Airtable-records
    zelf verwijderen als de Sluitingsdatum meer dan AIRTABLE_PURGE_MONTHS
    geleden is. Draait nooit mee met de normale sync.
 
Credentials:
  AIRTABLE_TOKEN (env) of airtable_config.json -> "token"
  Per Webflow-target: env var volgens "token_env", of webflow_config.json
  onder de key uit "token_key". Ontbreekt een token voor een target, dan
  wordt dat ene target overgeslagen (met waarschuwing) -- de rest van de
  run gaat door. Zo kan v2 toegevoegd worden aan sync_targets.json voordat
  er al een token voor is.
Optional overrides: AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME
Publish behaviour: AUTO_PUBLISH=true/false (default true). Pass
--no-publish op de command line om publiceren voor deze ene run uit te
zetten (concept blijft concept) -- handig voor de eerste validatie-runs.
 
CLI-vlaggen:
  --only <naam>      alleen dit ene target uit sync_targets.json verwerken
  --limit <n>        per target hoogstens n records verwerken (nieuw aan te maken)
  --no-publish       forceer concept, ongeacht AUTO_PUBLISH
  --purge-airtable   verwijder Airtable-records >6 maanden na sluiting en stop
                      (draait GEEN gewone sync in dezelfde aanroep)
 
Voor lokaal testen: airtable_config.json / webflow_config.json /
sync_targets.json in dezelfde map (NOT committed to git, behalve
sync_targets.json zelf -- dat bevat geen secrets, alleen IDs/veldnamen).
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
SYNC_TARGETS_PATH = os.path.join(SCRIPT_DIR, "sync_targets.json")
 
IS_NIEUW_REVISIT_WINDOW_DAYS = int(os.environ.get("IS_NIEUW_REVISIT_WINDOW_DAYS", "4"))
IS_NIEUW_DAYS = 2  # een opdracht is 'nieuw' tot 2 dagen na NIXZ createdDate
 
EXPIRED_GRACE_DAYS = 5  # dagen na Sluitingsdatum voordat item uit Webflow verwijderd wordt
AIRTABLE_PURGE_MONTHS = 6  # maanden na Sluitingsdatum voordat record uit Airtable verwijderd wordt (alleen via --purge-airtable)
 
 
# --------------------------------------------------------------------------
# Static mappings -- OUDE SITE (v1), ongewijzigd
# --------------------------------------------------------------------------
 
REGIO_OPTION_IDS_V1 = {
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
 
OPLEIDING_OPTION_IDS_V1 = {
    "MBO": "a3f2c023bfe24184ed60f7baf24c9cf7",
    "HBO": "f5a3dd39d467f7f1b6dc71072737d61d",
    "WO": "8d503a98bf3bf5d61588ae60d03acb7c",
}
 
INHUURTYPE_OPTION_IDS_V1 = {
    "zzp": "deae40cea419e5a576cd58515fff0602",
    "detachering": "05122fc9e975ac64cf00945752436dc9",
}
 
CATEGORY_TO_WERKVELD_ID_V1 = {
    "CONSTRUCTION": "689baf089b62e512800aa8e4",
    "COMMUNICATION": "689baf0afcce13fb4fe70f20",
    "CULTURE": "689baf0aedc79c91d98656b9",
    "SALES": "689baf0a5eda152aa416ac39",
    "FINANCE": "689baf092a4828df6193aea6",
    "HUMAN_RESOURCES": "689baf093de484f797204583",
    "LEGAL": "689baf09b91eeabc5504c838",
    "SPATIAL": "689baf0966aa8543a2d291cf",
    "SOCIAL": "689baf09734d9bed3b4edd75",
    "HEALTHCARE": "689baf0a37b58fcb98bc5ff2",
    "ADMINISTRATION": "689baf0873df3c8c4af47c26",
    "MANAGEMENT": "689baf097d0cd3b95eb43de4",
    "SECURITY": "689baf0919e204759aa5ec34",
    "LOGISTICS": "68a907314dc24cda1386fd04",
    "EDUCATION": "689baf0910962cdde42d9c59",
    "TECHNOLOGY": "689baf08dfacef0f5269428e",
    "FACILITIES": "689baf09923b5069e870a563",
    "ICT": "689baf08dfacef0f5269428e",
    "MARKETING": "689baf0afcce13fb4fe70f20",
}
 
 
# --------------------------------------------------------------------------
# Static mappings -- FASE2/NIEUWE SITE (v2)
# --------------------------------------------------------------------------
 
CATEGORY_TO_CATEGORIE_ID_V2 = {
    "CONSTRUCTION": "6ab2de818611973bebf79859",
    "COMMUNICATION": "6ab2df48436eec35eb7d36ca",
    "CULTURE": "6ab2df507ea9e2c80e6645fd",
    "SALES": "6ab2df5654fded960676e18f",
    "FINANCE": "6ab2df607d5a8e19aced22b1",
    "HUMAN_RESOURCES": "6ab2df6da9502556a33e25eb",
    "LEGAL": "6ab2df752ded87647cd575e5",
    "SPATIAL": "6ab2df7f24723279a3fe8626",
    "SOCIAL": "6ab2df87242393cb1cd117a5",
    "HEALTHCARE": "6ab2df9246ad79f0640b2cee",
    "ADMINISTRATION": "6ab2df9ba9502556a33e36e8",
    "MANAGEMENT": "6ab2dfa2fbb4e08cec7d93bf",
    "SECURITY": "6ab2dfaa6df0627ba3a811a7",
    "LOGISTICS": "6ab2dfb04cc9543b9f9b52c2",
    "EDUCATION": "6ab2dfb8d9f188cd3b42781b",
    "TECHNOLOGY": "6ab2dfbff323c43ea898ce52",
    "FACILITIES": "6ab2dfc75c70691f17bf92fa",
    "ICT": "6ab2dfcd8611973bebf83b21",
    "MARKETING": "6ab2dfd503de660221408233",
}
 
PROVINCIE_OPTION_IDS_V2 = {
    "GRONINGEN": "09e770168d184f3c9c8dd65f02b6ff58",
    "ZEELAND": "84c3e678517180dd3ee48cebf1c1d9d8",
    "ZUID_HOLLAND": "f78f5292689432dac21102324c2812ea",
    "GELDERLAND": "ebcb14b6714b5c0a494d0a18643da96f",
    "NOORD_HOLLAND": "9a53f322d5b371fe6cfde48bcbb9e8ee",
    "DRENTHE": "b46ff2c4c0f739bda5788a805dae3843",
    "OVERIJSSEL": "2ebfd28adcb4bd7b17e1d96d55bf7ba9",
    "UTRECHT": "1292275d0efba5cf4bf00fa2239d1da8",
    "LIMBURG": "ec2b5c000f55aed288e545a1cb870547",
    "NOORD_BRABANT": "ca22ab33580e9f10d4793f9009ab4663",
    "FRIESLAND": "e57e08e9f737bac7b048bea4d83cc7be",
    "FLEVOLAND": "12fa658cfee445a5bdea5a6b7294531f",
}
 
FREELANCER_OPTION_IDS_V2 = {
    "YES": "cc5dfbfc5a21c2bcd9dad70a3a22702e",
    "NO": "22c9b3435bdb28abfaa0e3277ee701b3",
    "UNKNOWN": "e7914b3d902e9696746ff5b82a3662e6",
}
 
 
# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
 
def _load_json_if_exists(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}
 
 
DEFAULT_SYNC_TARGETS = [
    {
        "name": "oude-site",
        "mapper": "v1",
        "collection_id": "689bb58f2ecf8b74698435b6",
        "item_id_field": "Webflow Item ID",
        "sync_status_field": "Sync status",
        "token_env": "WEBFLOW_API_TOKEN",
        "token_key": "token",
    },
    {
        "name": "fase2-staging",
        "mapper": "v2",
        "collection_id": "6a8f5d61df541d4edc682872",
        "item_id_field": "Webflow Item ID (v2)",
        "sync_status_field": "Sync status (v2)",
        "token_env": "WEBFLOW_API_TOKEN_V2",
        "token_key": "token_v2",
    },
]
 
 
def load_sync_targets():
    if os.path.exists(SYNC_TARGETS_PATH):
        with open(SYNC_TARGETS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return DEFAULT_SYNC_TARGETS
 
 
def get_config():
    airtable_local = _load_json_if_exists(os.path.join(SCRIPT_DIR, "airtable_config.json"))
 
    config = {
        "airtable_token": os.environ.get("AIRTABLE_TOKEN") or airtable_local.get("token"),
        "airtable_base_id": os.environ.get("AIRTABLE_BASE_ID") or airtable_local.get("base_id", DEFAULT_AIRTABLE_BASE_ID),
        "airtable_table_name": os.environ.get("AIRTABLE_TABLE_NAME") or airtable_local.get("table_name", DEFAULT_AIRTABLE_TABLE_NAME),
        "auto_publish": os.environ.get("AUTO_PUBLISH", "true").lower() != "false",
    }
    if "--no-publish" in sys.argv:
        config["auto_publish"] = False
 
    if not config["airtable_token"]:
        sys.exit("Ontbrekende AIRTABLE_TOKEN. Zet als env var of in airtable_config.json.")
    return config
 
 
def resolve_webflow_token(target):
    webflow_local = _load_json_if_exists(os.path.join(SCRIPT_DIR, "webflow_config.json"))
    return os.environ.get(target.get("token_env", "")) or webflow_local.get(target.get("token_key", "token"))
 
 
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
 
def airtable_fetch_unsynced(base_url_root, headers, table_name, item_id_field):
    import urllib.parse
    records = []
    offset = None
    while True:
        params = {
            "filterByFormula": (
                f"AND({{{item_id_field}}} = '', {{Sluitingsdatum}} != '', "
                f"NOT(IS_BEFORE({{Sluitingsdatum}}, TODAY())))"
            ),
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
 
 
def airtable_fetch_recent_synced(base_url_root, headers, table_name, item_id_field, sync_status_field, days):
    import urllib.parse
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    formula = (
        f"AND(NOT({{{item_id_field}}} = ''), "
        f"NOT({{{sync_status_field}}} = 'Verwijderd (verlopen)'), "
        f"IS_AFTER({{NIXZ createdDate}}, '{cutoff}'))"
    )
    records = []
    offset = None
    while True:
        params = {"filterByFormula": formula, "pageSize": 100}
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
 
 
def airtable_fetch_expired(base_url_root, headers, table_name, item_id_field, sync_status_field, grace_days):
    import urllib.parse
    formula = (
        f"AND(NOT({{{item_id_field}}} = ''), "
        f"{{Sluitingsdatum}} != '', "
        f"NOT({{{sync_status_field}}} = 'Verwijderd (verlopen)'), "
        f"IS_BEFORE({{Sluitingsdatum}}, DATEADD(TODAY(), -{grace_days}, 'days')))"
    )
    records = []
    offset = None
    while True:
        params = {"filterByFormula": formula, "pageSize": 100}
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
 
 
def airtable_fetch_unpublished(base_url_root, headers, table_name, item_id_field, sync_status_field):
    """Records die in Webflow als concept staan (status 'Nieuw') en nog niet verlopen zijn."""
    import urllib.parse
    formula = (
        f"AND(NOT({{{item_id_field}}} = ''), "
        f"{{{sync_status_field}}} = 'Nieuw', "
        f"{{Sluitingsdatum}} != '', "
        f"NOT(IS_BEFORE({{Sluitingsdatum}}, TODAY())))"
    )
    records = []
    offset = None
    while True:
        params = {"filterByFormula": formula, "pageSize": 100}
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
 
 
def airtable_fetch_for_purge(base_url_root, headers, table_name, months):
    import urllib.parse
    days = months * 30
    formula = (
        f"AND({{Sluitingsdatum}} != '', "
        f"IS_BEFORE({{Sluitingsdatum}}, DATEADD(TODAY(), -{days}, 'days')))"
    )
    records = []
    offset = None
    while True:
        params = {"filterByFormula": formula, "pageSize": 100}
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
    url = f"{base_url_root}/{table_name.replace(' ', '%20')}"
    for i in range(0, len(updates), AIRTABLE_WRITE_BATCH):
        batch = updates[i:i + AIRTABLE_WRITE_BATCH]
        http_json(url, method="PATCH", headers=headers, body={"records": batch, "typecast": True})
        time.sleep(0.25)
 
 
def airtable_delete_records(base_url_root, headers, table_name, record_ids):
    import urllib.parse
    deleted = 0
    for i in range(0, len(record_ids), AIRTABLE_WRITE_BATCH):
        batch = record_ids[i:i + AIRTABLE_WRITE_BATCH]
        params = urllib.parse.urlencode([("records[]", rid) for rid in batch])
        url = f"{base_url_root}/{urllib.parse.quote(table_name)}?{params}"
        result = http_json(url, method="DELETE", headers=headers)
        if result:
            deleted += len(result.get("records", []))
        time.sleep(0.25)
    return deleted
 
 
# --------------------------------------------------------------------------
# Gedeelde helpers
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
        return startdatum
    return startdatum_tekst
 
 
_LI_RE = re.compile(r"<li[^>]*>\s*(.*?)\s*</li>", re.DOTALL | re.IGNORECASE)
_LIST_WRAPPER_RE = re.compile(r"</?(ul|ol)[^>]*>", re.IGNORECASE)
 
 
def fix_lists_for_webflow(html):
    if not html:
        return html
    html = _LI_RE.sub(r"<p>• \1</p>", html)
    html = _LIST_WRAPPER_RE.sub("", html)
    return html
 
 
# --------------------------------------------------------------------------
# Mapper v1 -- oude site
# --------------------------------------------------------------------------
 
def build_field_data_v1(fields):
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
    if provincie in REGIO_OPTION_IDS_V1:
        field_data["regio"] = REGIO_OPTION_IDS_V1[provincie]
 
    opleiding = fields.get("Opleiding")
    if opleiding in OPLEIDING_OPTION_IDS_V1:
        field_data["opleiding"] = OPLEIDING_OPTION_IDS_V1[opleiding]
 
    freelancer = fields.get("Freelancer toegestaan")
    field_data["inhuurtype"] = INHUURTYPE_OPTION_IDS_V1["zzp"] if freelancer == "YES" else INHUURTYPE_OPTION_IDS_V1["detachering"]
 
    categorie = fields.get("Categorie")
    if categorie in CATEGORY_TO_WERKVELD_ID_V1:
        field_data["tags-for-webflow"] = [CATEGORY_TO_WERKVELD_ID_V1[categorie]]
 
    return {k: v for k, v in field_data.items() if v is not None}
 
 
# --------------------------------------------------------------------------
# Mapper v2 -- fase2/nieuwe site
# --------------------------------------------------------------------------
 
def compute_is_nieuw(created_raw):
    from datetime import datetime, timezone, timedelta
    if not created_raw:
        return True
    try:
        dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt) < timedelta(days=IS_NIEUW_DAYS)
 
 
def build_field_data_v2(fields, unmapped_tracker=None):
    nixz_id = fields.get("NIXZ ID")
    titel = fields.get("Titel") or "Opdracht"
 
    def track(key, value):
        if unmapped_tracker is not None and value:
            unmapped_tracker.setdefault(key, set()).add(value)
 
    aantal_professionals = fields.get("Aantal professionals")
 
    field_data = {
        "name": titel,
        "slug": slugify(titel, nixz_id),
        "opdrachtgever": fields.get("Opdrachtgever"),
        "locatie-full": fields.get("Locatie"),
        "stad": fields.get("Stad"),
        "sluiting-inschrijving": fields.get("Sluitingsdatum"),
        "startdatum": fields.get("Startdatum"),
        "einddatum": fields.get("Einddatum"),
        "uren-minimum": fields.get("Uren minimum"),
        "uren-maximum": fields.get("Uren maximum"),
        "salaris-minimum": fields.get("Salaris minimum"),
        "salaris-maximum": fields.get("Salaris maximum"),
        "aanvang": format_aanvang(fields.get("Startdatum"), fields.get("Startdatum tekst")),
        "aantal-professionals": str(aantal_professionals) if aantal_professionals is not None else None,
        "verlengingsoptie": fields.get("Verlengingsoptie"),
        "omschrijving-html": fix_lists_for_webflow(fields.get("Beschrijving (Webflow)")),
        "kandidaatomschrijving-html": fix_lists_for_webflow(fields.get("Kandidaatomschrijving (Webflow)")),
        "external-id": str(nixz_id) if nixz_id is not None else None,
        "opleiding": fields.get("Opleiding"),
        "is-nieuw": compute_is_nieuw(fields.get("NIXZ createdDate")),
    }
 
    logo_url = fields.get("Logo URL")
    if logo_url:
        field_data["logo-url"] = {"url": logo_url}
 
    provincie = fields.get("Provincie")
    if provincie:
        if provincie in PROVINCIE_OPTION_IDS_V2:
            field_data["provincie"] = PROVINCIE_OPTION_IDS_V2[provincie]
        else:
            track("provincie", provincie)
 
    freelancer = fields.get("Freelancer toegestaan")
    if freelancer:
        if freelancer in FREELANCER_OPTION_IDS_V2:
            field_data["freelancer-toegestaan"] = FREELANCER_OPTION_IDS_V2[freelancer]
        else:
            track("freelancer-toegestaan", freelancer)
 
    categorie = fields.get("Categorie")
    if categorie:
        if categorie in CATEGORY_TO_CATEGORIE_ID_V2:
            field_data["categorie"] = CATEGORY_TO_CATEGORIE_ID_V2[categorie]
        else:
            track("categorie", categorie)
 
    return {k: v for k, v in field_data.items() if v is not None}
 
 
MAPPERS = {
    "v1": build_field_data_v1,
    "v2": build_field_data_v2,
}
 
 
# --------------------------------------------------------------------------
# Webflow
# --------------------------------------------------------------------------
 
def webflow_create_item(collection_id, headers, field_data, verbose=False):
    url = f"https://api.webflow.com/v2/collections/{collection_id}/items/bulk"
    body = {"fieldData": field_data, "isDraft": True, "isArchived": False}
    result = http_json(url, method="POST", headers=headers, body=body, verbose=verbose)
    if not result:
        return None
    item_id = result.get("id")
    if item_id:
        return item_id
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
 
 
def webflow_update_items(collection_id, headers, items):
    url = f"https://api.webflow.com/v2/collections/{collection_id}/items"
    updated_ids = []
    for i in range(0, len(items), 100):
        batch = items[i:i + 100]
        result = http_json(url, method="PATCH", headers=headers, body={"items": batch})
        if result:
            updated_ids.extend(item["id"] for item in batch)
        time.sleep(WEBFLOW_PAUSE_SECONDS)
    return updated_ids
 
 
def webflow_delete_items(collection_id, headers, item_ids):
    """Bulk delete. Elk item wordt voor de zekerheid afzonderlijk als
    'geslaagd' beschouwd op basis van een 2xx-statuscode op de hele batch
    (Webflow's bulk-delete endpoint geeft geen per-item resultaat terug)."""
    url = f"https://api.webflow.com/v2/collections/{collection_id}/items"
    deleted_ids = []
    for i in range(0, len(item_ids), 100):
        batch = item_ids[i:i + 100]
        body = {"items": [{"id": iid} for iid in batch]}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="DELETE")
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                if 200 <= resp.status < 300:
                    deleted_ids.extend(batch)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            print(f"    !! HTTP {e.code} bij DELETE {url}: {err_body[:500]}", flush=True)
        except urllib.error.URLError as e:
            print(f"    !! Netwerkfout bij DELETE {url}: {e}", flush=True)
        time.sleep(WEBFLOW_PAUSE_SECONDS)
    return deleted_ids
 
 
# --------------------------------------------------------------------------
# is-nieuw revisit-pas
# --------------------------------------------------------------------------
 
def sync_is_nieuw_switch(airtable_base_url_root, airtable_headers, table_name, target, webflow_headers, auto_publish):
    from datetime import datetime, timezone, timedelta
    item_id_field = target["item_id_field"]
    switch_field = target["revisit_switch_field"]
 
    print(f"  [{target['name']}] Recente opdrachten checken voor '{switch_field}'-status...", flush=True)
    records = airtable_fetch_recent_synced(
        airtable_base_url_root, airtable_headers, table_name, item_id_field,
        target["sync_status_field"], IS_NIEUW_REVISIT_WINDOW_DAYS
    )
    if not records:
        print(f"    Geen recente gesynchroniseerde opdrachten gevonden.", flush=True)
        return
 
    now = datetime.now(timezone.utc)
    items = []
    for r in records:
        created_raw = r["fields"].get("NIXZ createdDate")
        item_id = r["fields"].get(item_id_field)
        if not created_raw or not item_id:
            continue
        try:
            created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        is_nieuw = (now - created_dt) < timedelta(days=IS_NIEUW_DAYS)
        items.append({"id": item_id, "fieldData": {"is-nieuw": is_nieuw}})
 
    updated_ids = webflow_update_items(target["collection_id"], webflow_headers, items)
    print(f"    {len(updated_ids)}/{len(items)} item(s) bijgewerkt.", flush=True)
    if auto_publish and updated_ids:
        webflow_publish_items(target["collection_id"], webflow_headers, updated_ids)
        print(f"    {len(updated_ids)} item(s) opnieuw gepubliceerd.", flush=True)
 
 
# --------------------------------------------------------------------------
# Opruimen van verlopen opdrachten (tegen het 10.000-item plafond)
# --------------------------------------------------------------------------
 
def cleanup_expired_items(target, airtable_base_url_root, airtable_headers, table_name, webflow_headers):
    name = target["name"]
    item_id_field = target["item_id_field"]
    sync_status_field = target["sync_status_field"]
 
    print(f"  [{name}] Verlopen opdrachten checken (>{EXPIRED_GRACE_DAYS}d na Sluitingsdatum)...", flush=True)
    records = airtable_fetch_expired(
        airtable_base_url_root, airtable_headers, table_name, item_id_field, sync_status_field, EXPIRED_GRACE_DAYS
    )
    if not records:
        print(f"    Geen verlopen opdrachten om te verwijderen.", flush=True)
        return
 
    by_item_id = {r["fields"][item_id_field]: r for r in records if r["fields"].get(item_id_field)}
    item_ids = list(by_item_id.keys())
    deleted_ids = webflow_delete_items(target["collection_id"], webflow_headers, item_ids)
    print(f"    {len(deleted_ids)}/{len(item_ids)} item(s) verwijderd uit Webflow.", flush=True)
 
    status_updates = [
        {"id": by_item_id[iid]["id"], "fields": {sync_status_field: "Verwijderd (verlopen)"}}
        for iid in deleted_ids
    ]
    if status_updates:
        airtable_update_records(airtable_base_url_root, airtable_headers, table_name, status_updates)
        print(f"    Airtable-status bijgewerkt voor {len(status_updates)} record(en).", flush=True)
 
 
# --------------------------------------------------------------------------
# Eén target verwerken
# --------------------------------------------------------------------------
 
def run_target(target, airtable_base_url_root, airtable_headers, table_name, auto_publish, limit, verbose):
    name = target["name"]
    mapper = MAPPERS[target["mapper"]]
    item_id_field = target["item_id_field"]
    sync_status_field = target["sync_status_field"]
 
    webflow_token = resolve_webflow_token(target)
    if not webflow_token:
        print(f"[{name}] Overgeslagen: geen Webflow-token gevonden "
              f"(env {target.get('token_env')} of webflow_config.json key "
              f"'{target.get('token_key')}').", flush=True)
        return
    webflow_headers = {"Authorization": f"Bearer {webflow_token}", "Content-Type": "application/json"}
 
    print(f"[{name}] Airtable-records zonder '{item_id_field}' ophalen (met sluitingsdatum)...", flush=True)
    records = airtable_fetch_unsynced(airtable_base_url_root, airtable_headers, table_name, item_id_field)
    print(f"[{name}] Gevonden: {len(records)} nog niet gesynchroniseerd.", flush=True)
 
    if limit is not None:
        records = records[:limit]
        print(f"[{name}] --limit actief: slechts {len(records)} record(en) deze run.", flush=True)
 
    unmapped = {}
    created = []
    pending_writeback = []
    failed = 0
 
    def flush_writeback():
        if pending_writeback:
            airtable_update_records(airtable_base_url_root, airtable_headers, table_name, list(pending_writeback))
            pending_writeback.clear()
 
    if records:
        for i, record in enumerate(records, 1):
            if target["mapper"] == "v2":
                field_data = mapper(record["fields"], unmapped_tracker=unmapped)
            else:
                field_data = mapper(record["fields"])
            item_id = webflow_create_item(target["collection_id"], webflow_headers, field_data, verbose=verbose)
            if item_id:
                created.append((record["id"], item_id))
                pending_writeback.append({"id": record["id"], "fields": {item_id_field: item_id, sync_status_field: "Nieuw"}})
                print(f"  [{name}] [{i}/{len(records)}] aangemaakt: {field_data.get('name')} -> {item_id}", flush=True)
            else:
                failed += 1
                print(f"  [{name}] [{i}/{len(records)}] MISLUKT: {field_data.get('name')}", flush=True)
 
            if len(pending_writeback) >= AIRTABLE_WRITE_BATCH:
                flush_writeback()
            time.sleep(WEBFLOW_PAUSE_SECONDS)
 
        flush_writeback()
 
        if failed:
            print(f"[{name}] Let op: {failed} item(s) niet aangemaakt.", flush=True)
 
        if created:
            if auto_publish:
                print(f"[{name}] Publiceren van {len(created)} item(s)...", flush=True)
                webflow_publish_items(target["collection_id"], webflow_headers, [wid for _, wid in created])
                status_updates = [{"id": rec_id, "fields": {sync_status_field: "Gepubliceerd"}} for rec_id, _ in created]
                airtable_update_records(airtable_base_url_root, airtable_headers, table_name, status_updates)
            else:
                print(f"[{name}] Publiceren overgeslagen -- items staan als concept.", flush=True)
 
        for field_name, values in unmapped.items():
            print(f"[{name}] Let op: {len(values)} niet-gematchte waarde(n) voor '{field_name}': "
                  f"{sorted(values)}", flush=True)
    else:
        print(f"[{name}] Niets nieuws aan te maken.", flush=True)
 
    if auto_publish:
        pending = airtable_fetch_unpublished(airtable_base_url_root, airtable_headers, table_name,
                                             item_id_field, sync_status_field)
        if pending:
            print(f"[{name}] {len(pending)} item(s) staan nog als concept -- publiceren...", flush=True)
            pending_ids = [r["fields"][item_id_field] for r in pending if r["fields"].get(item_id_field)]
            webflow_publish_items(target["collection_id"], webflow_headers, pending_ids)
            airtable_update_records(airtable_base_url_root, airtable_headers, table_name,
                                    [{"id": r["id"], "fields": {sync_status_field: "Gepubliceerd"}} for r in pending])
            print(f"[{name}] {len(pending_ids)} item(s) gepubliceerd.", flush=True)
 
    if target.get("revisit_switch_field"):
        sync_is_nieuw_switch(airtable_base_url_root, airtable_headers, table_name, target, webflow_headers, auto_publish)
 
    if target.get("auto_cleanup_expired", True):
        cleanup_expired_items(target, airtable_base_url_root, airtable_headers, table_name, webflow_headers)
 
    print(f"[{name}] Klaar. {len(created)} nieuw aangemaakt.", flush=True)
 
 
# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
 
def run_purge_airtable(airtable_base_url_root, airtable_headers, table_name):
    print(f"Airtable-records zoeken die >{AIRTABLE_PURGE_MONTHS} maanden na Sluitingsdatum zijn...", flush=True)
    records = airtable_fetch_for_purge(airtable_base_url_root, airtable_headers, table_name, AIRTABLE_PURGE_MONTHS)
    print(f"Gevonden: {len(records)} record(en) om te verwijderen uit Airtable.", flush=True)
    if not records:
        return
    if "--yes" not in sys.argv:
        print("Dit is een PERMANENTE verwijdering uit Airtable (niet alleen Webflow).")
        print("Voeg --yes toe aan het commando om dit ook echt uit te voeren.")
        print("Voorbeeld-titels van de gevonden records:")
        for r in records[:10]:
            print(f"  - {r['fields'].get('Titel')} (sluiting: {r['fields'].get('Sluitingsdatum')})")
        return
    record_ids = [r["id"] for r in records]
    deleted = airtable_delete_records(airtable_base_url_root, airtable_headers, table_name, record_ids)
    print(f"{deleted} record(en) definitief verwijderd uit Airtable.", flush=True)
 
 
def main():
    config = get_config()
 
    airtable_headers = {"Authorization": f"Bearer {config['airtable_token']}", "Content-Type": "application/json"}
    airtable_base_url_root = f"https://api.airtable.com/v0/{config['airtable_base_id']}"
 
    if "--purge-airtable" in sys.argv:
        run_purge_airtable(airtable_base_url_root, airtable_headers, config["airtable_table_name"])
        return
 
    targets = load_sync_targets()
 
    only = None
    if "--only" in sys.argv:
        idx = sys.argv.index("--only")
        only = sys.argv[idx + 1]
        targets = [t for t in targets if t["name"] == only]
        if not targets:
            sys.exit(f"Geen target met naam '{only}' gevonden in sync_targets.json.")
 
    limit = None
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        limit = int(sys.argv[idx + 1])
 
    verbose = limit is not None or only is not None
 
    print(f"Publiceren staat: {'AAN' if config['auto_publish'] else 'UIT (concept blijft concept)'}", flush=True)
    print(f"Targets deze run: {[t['name'] for t in targets]}", flush=True)
 
    for t in targets:
        if t["mapper"] == "v2" and "revisit_switch_field" not in t:
            t["revisit_switch_field"] = "is-nieuw"
 
    for target in targets:
        run_target(target, airtable_base_url_root, airtable_headers, config["airtable_table_name"],
                   config["auto_publish"], limit, verbose)
 
 
if __name__ == "__main__":
    main()
