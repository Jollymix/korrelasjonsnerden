# Korrelasjonsnerden

En generator for "tulle-korrelasjoner" à la [spurious-correlations.com](https://www.tylervigen.com/spurious-correlations)
(Tyler Vigen), men basert på ekte norske tidsserier fra
[SSBs PxWebApi v2](https://data.ssb.no/api/pxwebapi/v2).

Hver uke:
1. Søker automatisk opp SSB-tabeller ut fra en liste med søkeord (`SOKEORD`
   i `tullkorrelasjon.py`).
2. Henter "totalserien" for hver tabell (auto-hent-total: velger
   totalkategori for kjønn/region/alder/osv., beholder tidsdimensjonen).
3. Regner Pearson-korrelasjon mellom alle par av serier og plukker et
   tilfeldig par med |r| > 0,85.
4. Genererer `index.html` med en pseudo-vitenskapelig norsk tekst (trukket
   tilfeldig fra flere maler, se `OVERSKRIFT_MALER`/`INGRESS_MALER`), en
   "Nerden spekulerer"-seksjon skrevet av Claude API (se under), og en
   Chart.js-graf.

Kjøres ukentlig (mandager kl. 06:00 UTC) via
[`.github/workflows/ukentlig.yml`](.github/workflows/ukentlig.yml), som
committer den ferske `index.html` (og arkivet, se under) rett inn i repoet.

## Arkiv

Hver ukes side lagres permanent under `arkiv/<år>/uke-<nn>.html` (ISO-uke),
i tillegg til at `index.html` alltid viser den nyeste. Alle sider har en
innebygd, kollapsbar meny (år → uke) som lenker til samtlige tidligere
funn. Menyen har ingen egen datafil å holde synkronisert — den bygges hver
kjøring direkte fra filtreet under `arkiv/` (`finn_alle_innslag()` i
`tullkorrelasjon.py`), og gamle sider får menyen sin oppdatert i etterkant
slik at de også lenker til nyere uker.

## «Nerden spekulerer»

Hver side har en boks der Claude API (`claude-opus-5`) skriver 1–3
tørre, underdrevne setninger som later som ukens korrelasjon er en reell
årsakssammenheng, og trekker en absurd men logisk-klingende konklusjon
eller anbefaling. Krever en `ANTHROPIC_API_KEY` — sett den som repo-secret
under **Settings → Secrets and variables → Actions** for at GitHub
Actions-kjøringen skal bruke ekte Claude-generert tekst. Mangler
nøkkelen (eller `anthropic`-pakken, eller nettet svikter), faller
scriptet automatisk tilbake til en liten bank med statisk tekst —
kjøringen feiler aldri på grunn av dette.

## Innholdsfilter

Siden skal aldri tulle med død, selvmord/selvskading, vold/overgrep,
hatkriminalitet, seksuell orientering/kjønnsidentitet, alvorlig sykdom
eller annen tematikk som med rimelighet oppleves trist eller sensitiv.
Dette håndheves i to lag:

1. **Nøkkelordfilter** (`SENSITIVE_STIKKORD`/`_er_sensitivt()`) — kjører
   alltid, uavhengig av Claude API, og filtrerer bort tabeller/serier/par
   som treffer eksplisitte ord i disse kategoriene. Dette er den harde
   garantien.
2. **Claude API-vurdering** (`vurder_datagrunnlag()`) — ett samlet kall
   som lar Claude vurdere hele ukas kandidatliste mer nyansert, og fanger
   opp temaer nøkkelordlista ikke tenkte på (f.eks. "personer bosatt på
   sykehjem" eller "mistet en pårørende i ulykke" — ingen sensitive
   enkeltord, men åpenbart ikke noe å tulle useriøst med).

Feiler Claude-kallet (nettverk, kreditter, avvist svar), faller scriptet
tilbake til kun nøkkelordfilteret — **aldri** til å tillate alt.

## Kjøre lokalt

```bash
pip install -r requirements.txt
python tullkorrelasjon.py
```

Kjernescriptet bruker kun standardbiblioteket. Eneste avhengighet
(`anthropic`, i `requirements.txt`) brukes til "Nerden spekulerer" — se
over. Uten `ANTHROPIC_API_KEY` satt i miljøet kjører scriptet fint, bare
med fallback-tekst i den seksjonen.

## Om SSBs søke-API

Se docstringen øverst i `tullkorrelasjon.py` for detaljer verifisert mot
det ekte API-et — bl.a. at søkeparameteret heter `query`, at søket ligner
mer på et relevanssøk enn et rent substring-søk (så enkle grunnord som
"elg" fungerer bedre enn sammensetninger som "elgjakt-statistikk"), og at
alle endepunkt deler samme rate-limit på 40 kall/60 sekunder.

## Publisere via GitHub Pages

Slå på GitHub Pages for repoet (Settings → Pages → Deploy from branch →
`main` / root), så blir `index.html` tilgjengelig på
`https://<bruker>.github.io/korrelasjonsnerden/` og oppdateres automatisk
hver mandag.
