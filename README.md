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
   tilfeldig fra flere maler, se `OVERSKRIFT_MALER`/`INGRESS_MALER`) og en
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

## Kjøre lokalt

```bash
python tullkorrelasjon.py
```

Ingen avhengigheter utover standardbiblioteket.

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
