# NIXZ → Airtable → Webflow sync — GitHub Actions opzetten

## Wat dit is
- `nixz_airtable_sync.py`: haalt periodiek nieuwe (nog open) opdrachten op
  uit de NIXZ API, scrubt leveranciersnamen uit de tekst, en zet ze in de
  Airtable-base "Carrièrebrug - NIXZ Sync" (tabel "Opdrachten").
- `airtable_webflow_sync.py`: neemt Airtable-records die nog geen Webflow
  Item ID hebben en zet ze in de bestaande Webflow-collectie
  "Opdracht listings" (tijdelijke brug naar het oude CMS-formaat, in
  afwachting van een nieuwe CMS-opzet specifiek voor de NIXZ-data).
- `.github/workflows/nixz-sync.yml` laat beide scripts automatisch draaien
  op een schema, in de cloud, zonder dat jouw computer aan hoeft te staan.

## Eenmalig opzetten

1. **Maak een nieuwe GitHub-repository** (kan private zijn — bevat geen
   geheimen zolang je stap 3 volgt).
2. Zet in de root van die repo:
   - `nixz_airtable_sync.py`
   - `airtable_webflow_sync.py`
   - `known_platforms.json` (alvast gevuld met de platforms die tijdens het
     testen zijn gevonden)
   - `.github/workflows/nixz-sync.yml` (let op: moet in die exacte map staan)
   - `.gitignore` (hernoem `nixz-sync-gitignore.txt` naar `.gitignore`)
3. **Voeg de secrets toe** — GitHub repo → Settings → Secrets and variables →
   Actions → "New repository secret":
   - `NIXZ_USERNAME`
   - `NIXZ_PASSWORD`
   - `AIRTABLE_TOKEN`
   - `WEBFLOW_API_TOKEN`

   (Deze staan dus NIET in de code zelf, alleen als versleutelde GitHub-secrets.)
4. Commit en push alles naar GitHub.
5. Ga naar het tabblad "Actions" in de repo → workflow "NIXZ naar Airtable
   naar Webflow sync" → **Run workflow**. Zet bij de eerste run "publish" op
   **false** zodat de eerste batch als concept in Webflow komt en je 'm kan
   nalopen voordat 'ie live gaat. Daarna kan "publish" op true (= standaard).
6. Check eerst Airtable, dan Webflow (als concept-items, tabblad "Staging"
   in de Opdracht listings-collectie) — publiceer handmatig via Webflow als
   het er goed uitziet, of draai de workflow nogmaals met publish=true.

## Schema aanpassen
Standaard draait het elke 2 uur (`cron: "0 */2 * * *"` in de workflow-yaml).
Wil je het vaker/minder vaak? Pas die regel aan volgens cron-syntax.

## Hoe het incrementeel werkt
- NIXZ → Airtable: het script vraagt Airtable zelf naar de nieuwste
  "NIXZ createdDate" die al is opgeslagen, en haalt bij NIXZ alleen wat
  daarna komt op (plus: opdrachten waarvan de sluitingsdatum al voorbij is
  worden altijd overgeslagen). Airtable is dus de bron van waarheid voor
  wat al gesynchroniseerd is — geen apart "vorige-run"-bestand dat kan
  verdwalen.
- Airtable → Webflow: alleen records zonder "Webflow Item ID" worden
  opgepakt (create-once — opdrachten veranderen na plaatsing zelden van
  inhoud, en gesloten opdrachten bereiken Airtable sowieso nooit).

`known_platforms.json` is wel een lokaal/gecommit bestand: dit onthoudt
welke leverancier-platformnamen ooit zijn gezien, zodat de scrub-lijst blijft
groeien als NIXZ een nieuw platform toevoegt (zoals FLINTER/CIRCLE_8/HAERT,
die niet in de officiële NIXZ-documentatie stonden maar wel in de praktijk
voorkomen). De workflow committeert dit bestand automatisch terug naar de
repo als er iets bijkomt.

## Categorie-mapping (let op)
NIXZ heeft 19 categorieën, Webflow's "Werkvelden" heeft er 25 (fijnmaziger).
De mapping in `airtable_webflow_sync.py` (`CATEGORY_TO_WERKVELD_ID`) is voor
de meeste categorieën eenduidig, maar MANAGEMENT → Projectmanagement,
LOGISTICS → Mobiliteit en MARKETING → Voorlichting/Communicatie zijn een
inschatting. Pas de dict aan als dat niet klopt.

## Lokaal testen (optioneel)
Zet deze bestanden in dezelfde map — ze staan in `.gitignore` en komen dus
nooit in de repo terecht:

```json
// nixz_config.json
{ "username": "...", "password": "...", "base_url": "https://app.nixz.io/api" }

// airtable_config.json
{ "token": "pat...", "base_id": "appgoJ97eVpLTyQq6", "table_name": "Opdrachten" }

// webflow_config.json
{ "token": "...", "collection_id": "689bb58f2ecf8b74698435b6" }
```

Dan: `python3 nixz_airtable_sync.py` en/of `python3 airtable_webflow_sync.py --no-publish`
(`--no-publish` forceert concept, ongeacht de `AUTO_PUBLISH`-instelling).
