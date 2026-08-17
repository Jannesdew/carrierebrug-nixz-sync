#!/usr/bin/env python3
"""
Eenmalige reparatie: haalt voor alle Airtable-records met een "Webflow Item ID"
de ruwe "Beschrijving (Webflow)" en "Kandidaatomschrijving (Webflow)" op, past
fix_lists_for_webflow() toe (zelfde functie als in airtable_webflow_sync.py) en
update de bestaande Webflow-items in place via de bulk PATCH-endpoint.

Waarom dit nodig is: Webflow's RichText-editor/renderer laat de tekst van
<li>-items leeg zien wanneer <ul>/<ol>/<li> via de API wordt gezet (bevestigd
door Jannes met screenshots op 2026-07-17). De ruwe data via GET bevat de
<li>-tekst nog gewoon, maar de weergave in Webflow zelf niet. Oplossing:
lijsten platslaan naar "<p>• tekst</p>" i.p.v. <ul><li>.

Dit script VERWIJDERT of HERMAAKT niets - het update alleen de twee
rich-text-velden van bestaande items (id blijft hetzelfde, dus geen
Airtable-koppeling hoeft gereset te worden).

Gebruik:
    python3 fix_webflow_lists.py            # voert de fix door
    python3 fix_webflow_lists.py --dry-run  # laat alleen zien wat er zou gebeuren
    python3 fix_webflow_lists.py --limit 5  # test op de eerste 5 records
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

AIRTABLE_BATCH = 100
WEBFLOW_BATCH = 100

_LI_RE = re.compile(r"<li[^>]*>\s*(.*?)\s*</li>", re.DOTALL | re.IGNORECASE)
_LIST_WRAPPER_RE = re.compile(r"</?(ul|ol)[^>]*>", re.IGNORECASE)


def fix_lists_for_webflow(html):
    """Flatten <ul>/<ol><li> lists into bullet paragraphs.
    Webflow's RichText field is known to render <li> text as empty when set
    via the API - confirmed 2026-07-17 via screenshots showing empty list
    item containers in the Designer/CMS editor. Plain <p> paragraphs render
    fine, so we sidestep the bug entirely."""
    if not html:
        return html
    html = _LI_RE.sub(r"<p>• \1</p>", html)
    html = _LIST_WRAPPER_RE.sub("", html)
    return html


def load_config(filename, env_keys):
    """Load config from a local JSON file, falling back to env vars."""
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    cfg = {}
    for json_key, env_key in env_keys.items():
        val = os.environ.get(env_key)
        if val:
            cfg[json_key] = val
    return cfg


def http_json(url, method="GET", headers=None, body=None, verbose=False):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"    !! HTTP {e.code} bij {method} {url}: {err_body[:800]}", flush=True)
        if verbose:
            print(f"    body was: {json.dumps(body)[:1000]}", flush=True)
        return None


def airtable_fetch_records_with_webflow_id(base_url_root, headers, table_name, limit=None):
    """Fetch all Airtable records that have a non-empty 'Webflow Item ID'."""
    records = []
    offset = None
    formula = "NOT({Webflow Item ID} = '')"
    while True:
        params = f"?pageSize={AIRTABLE_BATCH}&filterByFormula={urllib.parse.quote(formula)}"
        params += "&fields%5B%5D=Webflow+Item+ID&fields%5B%5D=Beschrijving+(Webflow)&fields%5B%5D=Kandidaatomschrijving+(Webflow)"
        if offset:
            params += f"&offset={offset}"
        url = f"{base_url_root}/{table_name}{params}"
        result = http_json(url, headers=headers)
        if result is None:
            break
        batch = result.get("records", [])
        records.extend(batch)
        print(f"    Opgehaald: {len(records)} records tot nu toe...", flush=True)
        if limit and len(records) >= limit:
            records = records[:limit]
            break
        offset = result.get("offset")
        if not offset:
            break
    return records


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    airtable_cfg = load_config(
        "airtable_config.json",
        {"token": "AIRTABLE_TOKEN", "base_id": "AIRTABLE_BASE_ID", "table_name": "AIRTABLE_TABLE_NAME"},
    )
    webflow_cfg = load_config(
        "webflow_config.json",
        {"token": "WEBFLOW_API_TOKEN", "collection_id": "WEBFLOW_COLLECTION_ID"},
    )

    airtable_token = airtable_cfg.get("token")
    base_id = airtable_cfg.get("base_id")
    table_name = airtable_cfg.get("table_name", "Opdrachten")
    webflow_token = webflow_cfg.get("token")
    collection_id = webflow_cfg.get("collection_id")

    if not all([airtable_token, base_id, webflow_token, collection_id]):
        print("Ontbrekende config. Zorg voor airtable_config.json en webflow_config.json "
              "(zie README-nixz-sync.md), of zet de env vars.", flush=True)
        sys.exit(1)

    airtable_base_url = f"https://api.airtable.com/v0/{base_id}"
    airtable_headers = {"Authorization": f"Bearer {airtable_token}"}
    webflow_headers = {"Authorization": f"Bearer {webflow_token}"}

    print("Ophalen van Airtable-records met een Webflow Item ID...", flush=True)
    records = airtable_fetch_records_with_webflow_id(airtable_base_url, airtable_headers, table_name, limit=args.limit)
    print(f"Totaal gevonden: {len(records)} records.", flush=True)

    items_to_update = []
    skipped_no_change = 0
    for rec in records:
        fields = rec.get("fields", {})
        webflow_id = fields.get("Webflow Item ID")
        if not webflow_id:
            continue
        raw_omschrijving = fields.get("Beschrijving (Webflow)") or ""
        raw_kandidaat = fields.get("Kandidaatomschrijving (Webflow)") or ""
        fixed_omschrijving = fix_lists_for_webflow(raw_omschrijving)
        fixed_kandidaat = fix_lists_for_webflow(raw_kandidaat)

        if fixed_omschrijving == raw_omschrijving and fixed_kandidaat == raw_kandidaat:
            skipped_no_change += 1
            continue

        items_to_update.append({
            "id": webflow_id,
            "isDraft": True,
            "fieldData": {
                "omschrijving-html": fixed_omschrijving,
                "kandidaatomschrijving-html": fixed_kandidaat,
            },
        })

    print(f"Te updaten: {len(items_to_update)} items (overgeslagen zonder wijziging: {skipped_no_change}).", flush=True)

    if args.dry_run:
        print("Dry-run: er wordt niets naar Webflow gestuurd.", flush=True)
        for item in items_to_update[:3]:
            print(json.dumps(item, ensure_ascii=False, indent=2)[:1500], flush=True)
        return

    url = f"https://api.webflow.com/v2/collections/{collection_id}/items"

    def patch_batch(batch, failed_ids):
        """PATCH a batch. Webflow fails the WHOLE batch if even one item id
        no longer exists (e.g. an Airtable 'Webflow Item ID' left over from
        a deleted duplicate). On failure, bisect the batch to isolate and
        skip the bad id(s) instead of losing the whole batch."""
        if not batch:
            return 0
        result = http_json(url, method="PATCH", headers=webflow_headers, body={"items": batch})
        if result is not None:
            return len(result.get("items", []))
        if len(batch) == 1:
            failed_ids.append(batch[0]["id"])
            print(f"    !! Item {batch[0]['id']} overgeslagen (bestaat niet meer in Webflow).", flush=True)
            return 0
        mid = len(batch) // 2
        return patch_batch(batch[:mid], failed_ids) + patch_batch(batch[mid:], failed_ids)

    updated_count = 0
    failed_ids = []
    for i in range(0, len(items_to_update), WEBFLOW_BATCH):
        batch = items_to_update[i:i + WEBFLOW_BATCH]
        print(f"  Batch {i // WEBFLOW_BATCH + 1}: {len(batch)} items PATCHen...", flush=True)
        updated_count += patch_batch(batch, failed_ids)
        time.sleep(1)  # klein beetje ruimte tussen batches i.v.m. rate limits

    print(f"Klaar. Totaal succesvol geupdatet: {updated_count} / {len(items_to_update)}.", flush=True)
    if failed_ids:
        print(f"Overgeslagen (bestaan niet meer in Webflow, {len(failed_ids)}x): {', '.join(failed_ids)}", flush=True)
        print("Tip: dit zijn waarschijnlijk verweesde 'Webflow Item ID'-verwijzingen in Airtable "
              "(bv. van eerder verwijderde concept-duplicaten) - leeg dat veld voor deze records "
              "zodat een volgende sync-run ze opnieuw aanmaakt.", flush=True)


if __name__ == "__main__":
    main()
