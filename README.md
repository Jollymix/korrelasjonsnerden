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
4. Genererer `index.html` med en pseudo-vitenskapelig norsk tekst og en
   Chart.js-graf.

Kjøres ukentlig (mandager kl. 06:00 UTC) via
[`.github/workflows/ukentlig.yml`](.github/workflows/ukentlig.yml), som
committer den ferske `index.html` rett inn i repoet.

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
