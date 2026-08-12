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

Krav: ingen harde avhengigheter — kun standardbiblioteket (urllib, json,
statistics ...). Ett unntak: «Nerden spekulerer»-seksjonen bruker Claude
API (pakken `anthropic`, se requirements.txt) til å skrive selve
formuleringen. Mangler pakken eller ANTHROPIC_API_KEY, faller scriptet
automatisk tilbake til statisk tekst — det krasjer aldri på grunn av
dette.

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

import html
import json
import pathlib
import posixpath
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
#
# Overskrift og ingress trekkes tilfeldig fra flere maler i stedet for én
# fast setning, slik at ukentlige innlegg ikke ser ut som ren copy-paste
# av hverandre — à la den varierte (men gjenkjennelige) tonen på
# spurious-correlations.com.
# ---------------------------------------------------------------------------

OVERSKRIFT_MALER = [
    "Ny analyse: {a} {retning} {b_liten}",
    "SSB-tall avslører: {a} henger tett sammen med {b_liten}",
    "Uka i tall: {styrke} sammenheng mellom {a} og {b_liten}",
    "{a} og {b_liten} følger hverandre påfallende tett, viser fersk statistikk",
    "Ingen har spurt om dette, men tallene sier det likevel: {a} vs {b_liten}",
]

INGRESS_MALER = [
    (
        "En gjennomgang av offisielle tall fra SSB for perioden "
        "{fra}–{til} avdekker en {styrke} korrelasjonskoeffisient på "
        "r = {r:.3f} mellom {a_liten} og {b_liten}. Forskere er ikke "
        "kontaktet, og årsakssammenheng er verken undersøkt eller "
        "sannsynlig."
    ),
    (
        "Tall hentet rett fra Statistisk sentralbyrå viser en {styrke} "
        "statistisk sammenheng (r = {r:.3f}) mellom {a_liten} og "
        "{b_liten} i årene {fra}–{til}. Om det betyr noe som helst, er "
        "en helt annen sak."
    ),
    (
        "Mellom {fra} og {til} har {a_liten} og {b_liten} beveget seg "
        "i {retning_kort} med en {styrke} korrelasjon på r = {r:.3f}. "
        "Kausalitet er verken hevdet eller undersøkt."
    ),
    (
        "En rask krysskjøring av SSBs offentlige tabeller gir en "
        "{styrke} korrelasjon (r = {r:.3f}) mellom {a_liten} og "
        "{b_liten} for perioden {fra}–{til} — akkurat den typen "
        "sammenheng du ikke bør legge for mye i."
    ),
    (
        "Det er ingen kjent grunn til at {a_liten} og {b_liten} skulle "
        "ha noe med hverandre å gjøre. Likevel viser SSB-tall for "
        "{fra}–{til} en {styrke} korrelasjon på r = {r:.3f}."
    ),
]

KOMMENTAR_BANK = [
    "Vi tar ingen forbehold. Grafen taler for seg selv.",
    "Ingen årsakssammenheng er antydet, foreslått, eller ønsket.",
    "SSB har ikke blitt bedt om en kommentar, og ville uansett neppe gitt en.",
    "Dette er ikke vitenskap. Dette er to linjer som tilfeldigvis ligner på hverandre.",
    "Korrelasjonen er ekte. Konklusjonen er det ikke.",
]

# "Nerden spekulerer" — selve formuleringen skrives av Claude API (se
# generer_spekulasjon()) i stedet for enda en statisk malbank, slik at
# den blir ny og treffsikker for akkurat ukens tall. Banken under er kun
# en nødløsning hvis API-kallet av en eller annen grunn ikke lykkes
# (mangler pakke/nøkkel, nettverksfeil, avvist svar) — den ukentlige
# jobben skal aldri stoppe opp på grunn av dette.
FALLBACK_SPEKULASJON = [
    "Tallene antyder en sammenheng vi ikke har grunnlag for å avvise. "
    "Vi anbefaler at ingen handler basert på denne analysen.",
    "Basert på grafen alene fremstår årsakssammenhengen som statistisk "
    "uomtvistelig. Eventuelle innvendinger fra fagfolk er ikke hensyntatt.",
    "Dersom trenden fortsetter, bør noen kanskje se nærmere på dette. "
    "Vi kommer ikke til å være de som gjør det.",
    "Det finnes ingen åpenbar mekanisme her, noe vi velger å se på som "
    "en styrke ved analysen snarere enn en svakhet.",
]

SPEKULASJON_SYSTEMPROMPT = (
    "Du skriver en kort seksjon kalt «Nerden spekulerer» til en norsk "
    "tulle-korrelasjon-side (à la Tyler Vigens spurious-correlations.com). "
    "Du får to variabler fra SSB-statistikk, korrelasjonen mellom dem, og "
    "hvilken av dem som (for moro skyld) skal fremstilles som årsaken. "
    "Skriv 1–3 setninger på norsk (bokmål) som:\n"
    "- later som korrelasjonen viser en reell årsakssammenheng\n"
    "- trekker en absurd, men logisk formulert konklusjon eller "
    "anbefaling basert på tallene\n"
    "- er saklig og underdreven i tonen, med overdreven statistisk "
    "selvsikkerhet — som om dette var en seriøs analyse\n"
    "Ikke forklar vitsen eller nevn at dette er humor/ironi. Ikke gjenta "
    "«Nerden spekulerer» i selve teksten. Varier setningsoppbygningen fra "
    "gang til gang — ikke bruk samme faste struktur hver gang. Svar KUN "
    "med selve teksten, uten anførselstegn eller forklaring."
)

SPEKULASJON_BRUKERMAL = (
    "Årsak (later som): {arsak}\n"
    "Virkning (later som): {virkning}\n"
    "Retning: {arsak} {retning} {virkning_liten}\n"
    "Korrelasjon: r = {r:.3f} ({styrke})\n"
    "Periode: {fra}–{til}"
)


def generer_spekulasjon(felter: dict) -> str:
    """Ber Claude API skrive «Nerden spekulerer»-teksten for ukens tall.
    Feiler aldri utad — enhver feil (manglende pakke/nøkkel, nettverk,
    avvist svar) fanges bredt med vilje, siden dette er en ukentlig
    cron-jobb som skal degradere til statisk fallback-tekst, ikke stoppe."""
    try:
        import anthropic

        client = anthropic.Anthropic()  # leser ANTHROPIC_API_KEY fra miljøet
        respons = client.messages.create(
            model="claude-opus-5",
            max_tokens=300,
            output_config={"effort": "low"},  # enkel kreativ tekstoppgave
            system=SPEKULASJON_SYSTEMPROMPT,
            messages=[{"role": "user", "content": SPEKULASJON_BRUKERMAL.format(**felter)}],
        )
        if respons.stop_reason == "refusal":
            raise RuntimeError("Claude avviste forespørselen")
        tekst = "".join(b.text for b in respons.content if b.type == "text").strip()
        if not tekst:
            raise RuntimeError("Tomt svar fra Claude")
        return tekst
    except Exception as e:
        print(f"  Klarte ikke generere 'Nerden spekulerer' via Claude API ({e}) — bruker fallback-tekst.")
        return random.choice(FALLBACK_SPEKULASJON)


def korrelasjonsstyrke(r: float) -> str:
    """Gir en styrkefrase basert på |r|, så selv setningen henger sammen
    med hvor ekstrem korrelasjonen faktisk er."""
    absr = abs(r)
    if absr > 0.95:
        return "påfallende sterk"
    if absr > 0.90:
        return "svært sterk"
    return "sterk"


def lag_tekst(par: dict) -> dict:
    """Trekker overskrift og ingress fra malbankene over, og ber Claude
    API skrive «Nerden spekulerer». Kalles ÉN gang per kjøring — samme
    ukes forside- og arkivkopi skal ha identisk tekst, kun menylenkene
    skal variere mellom dem."""
    retning = "øker i takt med" if par["r"] > 0 else "synker når"
    retning_kort = "samme retning" if par["r"] > 0 else "motsatt retning"
    styrke = korrelasjonsstyrke(par["r"])
    # Tilfeldig hvem som later som "årsak" — gir variasjon fra uke til
    # uke i hvem som "får skylden", uavhengig av hvilken som er a/b i data.
    arsak, virkning = (par["a"], par["b"]) if random.random() < 0.5 else (par["b"], par["a"])
    felter = {
        "a": par["a"], "b": par["b"],
        "a_liten": par["a"].lower(), "b_liten": par["b"].lower(),
        "arsak": arsak, "virkning": virkning, "virkning_liten": virkning.lower(),
        "retning": retning, "retning_kort": retning_kort,
        "styrke": styrke, "r": par["r"],
        "fra": par["aar"][0], "til": par["aar"][-1],
    }
    overskrift = random.choice(OVERSKRIFT_MALER).format(**felter)
    ingress = random.choice(INGRESS_MALER).format(**felter)
    kommentar = random.choice(KOMMENTAR_BANK)
    spekulasjon = generer_spekulasjon(felter)
    return {
        "overskrift": overskrift, "ingress": ingress,
        "kommentar": kommentar, "spekulasjon": spekulasjon,
    }


# ---------------------------------------------------------------------------
# 7. ARKIV — hver ukes side lagres permanent under arkiv/<år>/uke-<nn>.html,
#    og alle sider (forsiden og hver arkivside) får en innebygd, kollapsbar
#    meny som lenker til alle tidligere uker gruppert per år. Menyen bygges
#    fra selve filtreet under arkiv/ (ingen egen manifest-fil å holde synk)
#    — de committede HTML-filene ER fasiten.
# ---------------------------------------------------------------------------

ARKIV_ROT = pathlib.Path("arkiv")
TITTEL_MONSTER = re.compile(r"<title>(.*?)</title>", re.DOTALL)
MENY_MONSTER = re.compile(
    r"<!--ARKIV-MENY-START-->.*?<!--ARKIV-MENY-END-->", re.DOTALL
)


def arkivsti(aar: int, uke: int) -> str:
    return f"arkiv/{aar}/uke-{uke:02d}.html"


def finn_alle_innslag() -> list:
    """Skanner arkiv/<år>/uke-<nn>.html og bygger lista menyen trenger.
    Overskriften hentes fra hver fils <title> — filtreet er eneste
    kilde til sannhet, det finnes ingen separat manifest-fil."""
    innslag = []
    if ARKIV_ROT.is_dir():
        for fil in ARKIV_ROT.glob("*/uke-*.html"):
            m = re.match(r"uke-(\d+)$", fil.stem)
            if not m or not fil.parent.name.isdigit():
                continue
            treff = TITTEL_MONSTER.search(fil.read_text(encoding="utf-8"))
            # <title> i filen er allerede HTML-escaped ved skriving — hent
            # ut ren tekst her, så vi ikke escaper to ganger når menyen
            # bygges (se bygg_meny_html).
            tittel = html.unescape(treff.group(1)) if treff else fil.stem
            innslag.append({
                "aar": int(fil.parent.name),
                "uke": int(m.group(1)),
                "sti": fil.as_posix(),
                "tittel": tittel,
            })
    return innslag


def relativ_lenke(til_sti: str, fra_fil: str) -> str:
    """Regner ut riktig relativ lenke fra fra_fil til til_sti, uansett
    hvor dypt i arkiv/-treet fra_fil ligger."""
    fra_katalog = posixpath.dirname(fra_fil) or "."
    return posixpath.relpath(til_sti, fra_katalog)


def bygg_meny_html(alle_innslag: list, denne_sti: str) -> str:
    """Genererer en kollapsbar <details>-meny gruppert per år (nyeste
    år/uke øverst). Gjeldende side vises som ren tekst, ikke lenke."""
    if not alle_innslag:
        return ""

    per_aar = {}
    for e in sorted(alle_innslag, key=lambda e: (e["aar"], e["uke"]), reverse=True):
        per_aar.setdefault(e["aar"], []).append(e)

    nyeste_aar = max(per_aar)
    gjeldende_aar = next((e["aar"] for e in alle_innslag if e["sti"] == denne_sti), None)
    deler = ['<nav class="meny">']
    for aar in sorted(per_aar, reverse=True):
        apen = " open" if aar in (nyeste_aar, gjeldende_aar) else ""
        deler.append(f"<details{apen}><summary>{aar}</summary><ul>")
        for e in per_aar[aar]:
            tittel = html.escape(e["tittel"])
            if e["sti"] == denne_sti:
                deler.append(f'<li class="na">Uke {e["uke"]}: {tittel}</li>')
            else:
                lenke = relativ_lenke(e["sti"], denne_sti)
                deler.append(f'<li><a href="{lenke}">Uke {e["uke"]}: {tittel}</a></li>')
        deler.append("</ul></details>")
    deler.append("</nav>")
    return "".join(deler)


def oppdater_meny_i_eldre_filer(alle_innslag: list, unnta: set) -> None:
    """Bytter ut menyblokken i hver eksisterende side (unntatt de som
    skrives fullt ut denne kjøringen) slik at gamle sider også lenker til
    ukens nye side — ellers ville menyen deres fryse på generasjonstidspunktet."""
    for e in alle_innslag:
        if e["sti"] in unnta:
            continue
        fil = pathlib.Path(e["sti"])
        innhold = fil.read_text(encoding="utf-8")
        ny_meny = (
            "<!--ARKIV-MENY-START-->"
            + bygg_meny_html(alle_innslag, e["sti"])
            + "<!--ARKIV-MENY-END-->"
        )
        oppdatert = MENY_MONSTER.sub(lambda _: ny_meny, innhold, count=1)
        if oppdatert != innhold:
            fil.write_text(oppdatert, encoding="utf-8")


# ---------------------------------------------------------------------------
# 8. HTML-GENERERING
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
<script src="{chartjs_sti}"></script>
<style>
  body {{ font-family: Georgia, serif; max-width: 700px; margin: 40px auto; padding: 0 20px; color: #222; }}
  h1 {{ font-size: 1.6em; line-height: 1.3; }}
  .ingress {{ font-size: 1.1em; color: #444; }}
  .r-verdi {{ font-family: monospace; background: #f0f0f0; padding: 2px 6px; }}
  .disclaimer {{ margin-top: 40px; font-size: 0.85em; color: #888; border-top: 1px solid #ddd; padding-top: 12px; }}
  canvas {{ margin-top: 30px; }}
  .meny {{ margin: 14px 0 26px; font-size: 0.9em; }}
  .meny summary {{ cursor: pointer; color: #2563eb; }}
  .meny ul {{ list-style: none; padding-left: 16px; margin: 6px 0 10px; }}
  .meny li {{ margin: 3px 0; }}
  .meny .na {{ color: #888; font-style: italic; }}
  .spekulasjon {{ margin-top: 26px; padding: 14px 16px; background: #f7f5ef; border-left: 3px solid #999; }}
  .spekulasjon-tittel {{ font-weight: bold; margin: 0 0 6px; }}
</style>
</head>
<body>
  <p style="color:#888; font-size:0.85em;">Publisert {dato}</p>
  <!--ARKIV-MENY-START-->{meny}<!--ARKIV-MENY-END-->
  <h1>{tittel}</h1>
  <p class="ingress">{ingress}</p>
  <canvas id="chart" height="280"></canvas>
  <div class="spekulasjon">
    <p class="spekulasjon-tittel">Nerden spekulerer:</p>
    <p>{spekulasjon}</p>
  </div>
  <p class="disclaimer">
    {kommentar} Denne siden genereres automatisk fra offentlige SSB-tall og
    er ment som underholdning. Korrelasjon er ikke kausalitet — det er
    faktisk hele poenget med siden.
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


def lag_html(par: dict, tekst: dict, denne_sti: str, meny_html: str) -> str:
    return HTML_MAL.format(
        tittel=html.escape(tekst["overskrift"]),
        ingress=html.escape(tekst["ingress"]),
        kommentar=html.escape(tekst["kommentar"]),
        spekulasjon=html.escape(tekst["spekulasjon"]),
        meny=meny_html,
        dato=datetime.now().strftime("%d.%m.%Y"),
        chartjs_sti=relativ_lenke("chart.umd.min.js", denne_sti),
        aar=json.dumps(par["aar"]),
        navn_a=par["a"],
        navn_b=par["b"],
        x=json.dumps(par["x"]),
        y=json.dumps(par["y"]),
    )


# ---------------------------------------------------------------------------
# 9. HOVEDPROGRAM
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

    tekst = lag_tekst(par)

    iso = datetime.now().isocalendar()
    aar, uke = iso.year, iso.week
    ny_sti = arkivsti(aar, uke)

    # Ekskluder en ev. eksisterende oppføring for akkurat denne uken (f.eks.
    # ved en manuell re-kjøring samme uke) — den skrives uansett over under,
    # og skal ikke stå igjen som en duplikat i menyen.
    eksisterende = [e for e in finn_alle_innslag() if e["sti"] != ny_sti]
    alle = eksisterende + [{"aar": aar, "uke": uke, "sti": ny_sti, "tittel": tekst["overskrift"]}]

    pathlib.Path(ny_sti).parent.mkdir(parents=True, exist_ok=True)
    for sti in (ny_sti, "index.html"):
        meny = bygg_meny_html(alle, denne_sti=sti)
        innhold = lag_html(par, tekst, sti, meny)
        with open(sti, "w", encoding="utf-8") as f:
            f.write(innhold)

    oppdater_meny_i_eldre_filer(alle, unnta={ny_sti, "index.html"})

    print(f"Ferdig: {par['a']} vs {par['b']} (r = {par['r']:.3f})")
    print(f"Skrev index.html og {ny_sti} (arkiv har nå {len(alle)} innslag)")


if __name__ == "__main__":
    main()
