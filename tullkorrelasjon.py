#!/usr/bin/env python3
"""
Tullkorrelasjon-generator
=========================
Søker automatisk opp tabeller i SSBs PxWebApi v2 (data.ssb.no) ut fra en
liste med (forhåpentligvis morsomme) norske søkeord, henter "totalserien"
for hver tabell, leter etter par med høy statistisk korrelasjon (Pearson r),
og genererer en norsk HTML-side som presenterer "funnet" på en
pseudo-seriøs måte à la Tyler Vigen sin spurious-correlations.com.

Kjør ukentlig via GitHub Actions (se .github/workflows/ukentlig.yml).

Krav: ingen — kun standardbiblioteket (urllib, json, statistics ...).

---------------------------------------------------------------------------
Om SSBs søke-API (verifisert ved faktiske kall mot data.ssb.no, ikke gjettet):

  GET /tables?query=<søkeord>&lang=no&pageSize=<n>&pageNumber=<n>

  - Parameternavnet er "query" (bekreftet mot faktisk OpenAPI-spec på
    /swagger/v2/swagger.json — de eneste gyldige søkeparametrene er
    lang, query, pastDays, includeDiscontinued, pageNumber, pageSize).
  - Søket er IKKE et rent substring-søk — det ligner mer på et
    relevans-/stikkord-søk som også treffer på variabelverdier inni
    tabellene, ikke bare tabelltittelen. Sammensatte ord ("sykkeltyveri",
    "vinsalg", "vinmonopolet") gir ofte 0 treff, mens enklere grunnord
    ("sykkel", "vin", "tyveri") gir gode treff. Derfor er SOKEORD under
    bevisst enkle substantiv, ikke sammensetninger.
  - Svaret inneholder et "tables"-array (med bl.a. id, label, timeUnit)
    pluss "page" (pageNumber/pageSize/totalElements/totalPages).
  - Tomt søkeresultat gir bare tables: [] — ikke en feil.

  RATE LIMIT (funnet ved å inspisere responsheaderne, står ikke i
  dokumentasjonen): alle endepunkt (søk, metadata og data) deler samme
  kvote, annonsert i responsen som
      X-Ratelimit-Resource: SB_API_1MIN
      X-Ratelimit-Limit: 40
      X-Ratelimit-Policy: 40;w=60s
  altså maks 40 kall per 60 sekunder per IP. Scriptet respekterer dette
  ved å (a) legge inn en liten pause mellom hvert kall, (b) bremse ekstra
  ned når X-Ratelimit-Remaining nærmer seg 0, og (c) lese eventuelt
  Retry-After og prøve på nytt ved 429.
---------------------------------------------------------------------------
"""

import json
import random
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

API_BASIS = "https://data.ssb.no/api/pxwebapi/v2"
USER_AGENT = "tullkorrelasjon/2.0 (+https://github.com/; kontakt: se repo)"

# ---------------------------------------------------------------------------
# 1. SØKEORD — grunnlaget for automatisk tabellsøk. Alle ordene under er
#    testet mot det ekte søke-endepunktet og gir minst noen treff med en
#    tids-dimensjon som er årlig (eller årlig-lignende, f.eks. jaktsesonger
#    à la "1986-1987"). Legg gjerne til flere — men hold dem enkle
#    (grunnord, ikke sammensetninger, se forklaring i modul-docstringen).
# ---------------------------------------------------------------------------

SOKEORD = [
    "smør", "ost", "melk", "egg", "kaffe", "øl", "vin", "brus", "sjokolade",
    "poteter", "korn", "bær", "fisk", "laks", "torsk",
    "elg", "hjort", "rein", "bjørn", "ulv", "gaupe", "jakt", "fiske", "sopp",
    "skilsmisser", "fødsler", "ekteskap", "gravferd",
    "brann", "trafikkulykker", "tyveri", "innbrudd",
    "konkurser", "arbeidsledige", "sykefravær", "lønn",
    "snøscooter", "sykkel", "motorsykkel", "traktor", "campingvogn",
    "is", "snø", "regn", "vind", "temperatur", "flom",
]

# Hvor mange søkeord vi bruker og hvor mange kandidattabeller vi henter data
# for i én kjøring — begrenset for å holde oss godt innenfor SSBs
# rate-limit (40 kall/60 sek) uten at scriptet trenger å bruke evigheter.
MAKS_SOKEORD_PER_KJORING = 15
MAKS_TABELLER_PER_KJORING = 12

# Koder/labels vi tolker som "totalkategori" for en ikke-tid-dimensjon.
TOTAL_KODER = {"0", "00", "000"}
TOTAL_LABELS = {
    "i alt", "totalt", "total", "begge kjønn", "begge kjonn", "hele landet",
}


# ---------------------------------------------------------------------------
# 2. LAVNIVÅ API-KLIENT (med rate-limit-håndtering)
# ---------------------------------------------------------------------------

def _api_get(sti: str, params: dict | None = None, forsok: int = 0) -> dict:
    """Kaller et endepunkt under SSBs PxWebApi v2 og returnerer parset JSON.

    Håndterer SSBs delte rate-limit (se modul-docstring) ved å bremse ned
    når kvoten er i ferd med å gå tom, og ved å prøve på nytt (med
    Retry-After) hvis vi likevel treffer en 429."""
    url = f"{API_BASIS}{sti}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            gjenstaende = resp.headers.get("X-Ratelimit-Remaining")
            if gjenstaende is not None and gjenstaende.isdigit() and int(gjenstaende) <= 2:
                # Kvoten er nesten brukt opp — gi den tid til å fylles på
                # igjen (vinduet er 60 sekunder) før neste kall.
                time.sleep(15)
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429 and forsok < 4:
            vent = int(e.headers.get("Retry-After", 20))
            print(f"    Rate-limited (429) av SSB, venter {vent}s ...")
            time.sleep(vent)
            return _api_get(sti, params, forsok + 1)
        raise


# ---------------------------------------------------------------------------
# 3. TABELLSØK
# ---------------------------------------------------------------------------

def sok_tabeller(sokeord: str, pagesize: int = 5) -> list:
    """Søker etter tabeller for ett søkeord. Returnerer kun tabeller med en
    årlig (eller årlig-lignende) tidsdimensjon — måneds-/kvartalstabeller
    filtreres bort siden vi bygger {år: verdi}-serier."""
    try:
        svar = _api_get("/tables", {
            "query": sokeord, "lang": "no", "pageSize": pagesize,
        })
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"  Søk feilet for '{sokeord}': {e}")
        return []
    return [t for t in svar.get("tables", []) if t.get("timeUnit") in ("Annual", "Other")]


def bygg_kandidatliste() -> list:
    """Trekker et tilfeldig utvalg søkeord, samler unike tabell-ID-er fra
    treffene, og returnerer et tilfeldig utvalg av dem."""
    utvalg = random.sample(SOKEORD, min(MAKS_SOKEORD_PER_KJORING, len(SOKEORD)))
    funnet = {}
    for ord in utvalg:
        for t in sok_tabeller(ord):
            funnet.setdefault(t["id"], t.get("label", t["id"]))
        time.sleep(1.5)

    ider = list(funnet.keys())
    random.shuffle(ider)
    valgte = ider[:MAKS_TABELLER_PER_KJORING]
    print(f"Søkte på {len(utvalg)} ord, fant {len(ider)} unike tabeller, "
          f"henter data for {len(valgte)} av dem.")
    return valgte


# ---------------------------------------------------------------------------
# 4. AUTO-HENT-TOTAL — metadata inn, ren {år: verdi}-serie ut
# ---------------------------------------------------------------------------

def _er_totalkode(kode: str, label: str) -> bool:
    if kode in TOTAL_KODER:
        return True
    l = (label or "").strip().lower()
    return l in TOTAL_LABELS or "i alt" in l


def _finn_tidsnokkel(meta: dict) -> str:
    tid = meta.get("role", {}).get("time") or []
    if tid:
        return tid[0]
    treff = [k for k in meta["dimension"] if "tid" in k.lower()]
    return treff[0] if treff else "Tid"


def velg_totalkoder(meta: dict) -> tuple:
    """For hver ikke-tid-dimensjon (kjønn, region, alder, statistikkvariabel
    ...): velg koden som representerer "totalkategorien" — kode '0'/'00'
    eller en label som "I alt", "Begge kjønn", "Hele landet" e.l. Faller
    tilbake til første tilgjengelige kode hvis ingen åpenbar total finnes.
    Returnerer (valgte_koder, tidsnøkkel)."""
    dims = meta["dimension"]
    tid_key = _finn_tidsnokkel(meta)

    valgt = {}
    for key in meta["id"]:
        if key == tid_key:
            continue
        kategori = dims[key]["category"]
        koder = list(kategori["index"].keys())
        labels = kategori.get("label", {})
        total = next((k for k in koder if _er_totalkode(k, labels.get(k, ""))), None)
        valgt[key] = total if total is not None else koder[0]
    return valgt, tid_key


def rens_tittel(label: str) -> str:
    """Kosmetisk opprydding av SSBs tabelltitler: fjerner "12345: "-prefiks
    og halen med årstall/perioder og ettbokstavs tabelltype-koder, f.eks.
    "03432: Felte elg, etter alder og kjønn (K) (1986-1987)-(2025-2026)"
    -> "Felte elg, etter alder og kjønn"."""
    tittel = re.sub(r"^\d+:\s*", "", label)
    tittel = re.sub(r"\s*(\([A-ZÆØÅ]{1,3}\)\s*)?\(?\d{4}[\d\sMK()\-–]*$", "", tittel)
    tittel = tittel.strip(" ,.-")
    return tittel or label


def hent_serie_for_tabell(tabell_id: str) -> dict | None:
    """Auto-hent-total: henter metadata for tabellen, velger totalkategori
    for alle ikke-tid-dimensjoner, henter dataene for akkurat den
    kombinasjonen, og returnerer {"navn": ..., "data": {år: verdi}}."""
    meta = _api_get(f"/tables/{tabell_id}/metadata", {"lang": "no"})
    valgt, tid_key = velg_totalkoder(meta)

    params = {"lang": "no", "outputFormat": "json-stat2"}
    for key, kode in valgt.items():
        params[f"valueCodes[{key}]"] = kode
    params[f"valueCodes[{tid_key}]"] = "*"

    data = _api_get(f"/tables/{tabell_id}/data", params)

    dims = data["dimension"]
    tid_koder = list(dims[tid_key]["category"]["index"].keys())
    verdier = data["value"]

    punkter = {}
    for i, kode in enumerate(tid_koder):
        if i < len(verdier) and verdier[i] is not None:
            aar_tekst = "".join(c for c in kode if c.isdigit())[:4]
            if aar_tekst:
                punkter[int(aar_tekst)] = verdier[i]

    if len(punkter) < 4:
        return None
    return {"navn": rens_tittel(meta.get("label", tabell_id)), "data": punkter}


def hent_alle_serier() -> list:
    ider = bygg_kandidatliste()
    ok = []
    for tabell_id in ider:
        try:
            serie = hent_serie_for_tabell(tabell_id)
            if serie:
                ok.append(serie)
                print(f"  OK: {serie['navn']} ({len(serie['data'])} datapunkter)")
            else:
                print(f"  Hoppet over {tabell_id} — for få datapunkter")
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError) as e:
            print(f"  Feil ved henting av tabell {tabell_id}: {e}")
        time.sleep(1.5)
    return ok


# ---------------------------------------------------------------------------
# 5. KORRELASJON
# ---------------------------------------------------------------------------

def pearson(x: list, y: list) -> float:
    if len(x) < 3:
        return 0.0
    return statistics.correlation(x, y)  # Python 3.10+


def finn_beste_par(serier: list, min_overlapp: int = 5) -> dict | None:
    """Regner korrelasjon for alle par og returnerer et tilfeldig par
    blant de med |r| > 0.85 (jo flere kandidater, jo mer 'tull')."""
    kandidater = []
    for i in range(len(serier)):
        for j in range(i + 1, len(serier)):
            a, b = serier[i], serier[j]
            felles_aar = sorted(set(a["data"]) & set(b["data"]))
            if len(felles_aar) < min_overlapp:
                continue
            x = [a["data"][aar] for aar in felles_aar]
            y = [b["data"][aar] for aar in felles_aar]
            try:
                r = pearson(x, y)
            except statistics.StatisticsError:
                continue
            if abs(r) > 0.85:
                kandidater.append({
                    "a": a["navn"], "b": b["navn"], "r": r,
                    "aar": felles_aar, "x": x, "y": y,
                })

    if not kandidater:
        return None
    return random.choice(kandidater)


# ---------------------------------------------------------------------------
# 6. TEKSTGENERERING (pseudo-vitenskapelig, norsk)
# ---------------------------------------------------------------------------

def lag_overskrift(par: dict) -> str:
    retning = "øker i takt med" if par["r"] > 0 else "synker når"
    return f"Ny analyse: {par['a']} {retning} {par['b'].lower()}"

def lag_ingress(par: dict) -> str:
    return (
        f"En gjennomgang av offisielle tall fra SSB for perioden "
        f"{par['aar'][0]}–{par['aar'][-1]} avdekker en korrelasjonskoeffisient "
        f"på r = {par['r']:.3f} mellom {par['a'].lower()} og {par['b'].lower()}. "
        f"Forskere er ikke kontaktet, og årsakssammenheng er verken "
        f"undersøkt eller sannsynlig."
    )


# ---------------------------------------------------------------------------
# 7. HTML-GENERERING
#
# Chart.js lastes fra en lokal fil (chart.umd.min.js, ligger ved siden av
# index.html i repoet) i stedet for en CDN. Det er ikke bare for å unngå
# et eksternt avhengighetspunkt på GitHub Pages — under testing viste det
# seg at cdnjs.cloudflare.com rett og slett ikke var DNS-oppløselig i
# nettleseren som skulle vise en lokalt åpnet index.html (net::ERR_NAME_NOT_RESOLVED),
# noe som gjorde grafen usynlig. Se scripts/hent_chartjs.py for hvordan
# filen ble hentet ned.
# ---------------------------------------------------------------------------

HTML_MAL = """<!DOCTYPE html>
<html lang="no">
<head>
<meta charset="UTF-8">
<title>{tittel}</title>
<script src="chart.umd.min.js"></script>
<style>
  body {{ font-family: Georgia, serif; max-width: 700px; margin: 40px auto; padding: 0 20px; color: #222; }}
  h1 {{ font-size: 1.6em; line-height: 1.3; }}
  .ingress {{ font-size: 1.1em; color: #444; }}
  .r-verdi {{ font-family: monospace; background: #f0f0f0; padding: 2px 6px; }}
  .disclaimer {{ margin-top: 40px; font-size: 0.85em; color: #888; border-top: 1px solid #ddd; padding-top: 12px; }}
  canvas {{ margin-top: 30px; }}
</style>
</head>
<body>
  <p style="color:#888; font-size:0.85em;">Publisert {dato}</p>
  <h1>{tittel}</h1>
  <p class="ingress">{ingress}</p>
  <canvas id="chart" height="280"></canvas>
  <p class="disclaimer">
    Denne siden genereres automatisk fra offentlige SSB-tall og er ment som
    underholdning. Korrelasjon er ikke kausalitet — det er faktisk hele
    poenget med siden.
  </p>
<script>
new Chart(document.getElementById('chart'), {{
  type: 'line',
  data: {{
    labels: {aar},
    datasets: [
      {{
        label: '{navn_a}',
        data: {x},
        borderColor: '#2563eb',
        yAxisID: 'y',
        tension: 0.3,
      }},
      {{
        label: '{navn_b}',
        data: {y},
        borderColor: '#dc2626',
        yAxisID: 'y1',
        tension: 0.3,
      }}
    ]
  }},
  options: {{
    scales: {{
      y: {{ type: 'linear', position: 'left' }},
      y1: {{ type: 'linear', position: 'right', grid: {{ drawOnChartArea: false }} }}
    }}
  }}
}});
</script>
</body>
</html>
"""


def lag_html(par: dict) -> str:
    return HTML_MAL.format(
        tittel=lag_overskrift(par),
        ingress=lag_ingress(par),
        dato=datetime.now().strftime("%d.%m.%Y"),
        aar=json.dumps(par["aar"]),
        navn_a=par["a"],
        navn_b=par["b"],
        x=json.dumps(par["x"]),
        y=json.dumps(par["y"]),
    )


# ---------------------------------------------------------------------------
# 8. HOVEDPROGRAM
# ---------------------------------------------------------------------------

def main():
    print("Søker etter tabeller hos SSB ...")
    serier = hent_alle_serier()
    print(f"Fikk {len(serier)} brukbare serier.")

    par = finn_beste_par(serier)
    if par is None:
        print("Fant ingen par med |r| > 0.85 denne uken. "
              "Legg til flere søkeord i SOKEORD-listen, øk "
              "MAKS_TABELLER_PER_KJORING, eller senk terskelen.")
        return

    html = lag_html(par)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Ferdig: {par['a']} vs {par['b']} (r = {par['r']:.3f})")
    print("Skrev index.html")


if __name__ == "__main__":
    main()
