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
    "skilsmisser", "fødsler", "ekteskap",
    "brann", "trafikkulykker", "tyveri", "innbrudd",
    "konkurser", "arbeidsledige", "sykefravær", "lønn",
    "snøscooter", "sykkel", "motorsykkel", "traktor", "campingvogn",
    "is", "snø", "regn", "vind", "temperatur", "flom",
]
# ("gravferd" er bevisst fjernet — direkte dødsrelatert søkeord, se
# INNHOLDSFILTER-seksjonen lenger ned.)

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
# INNHOLDSFILTER — vi skal aldri tulle med død, selvmord, vold,
# hatkriminalitet, seksuell orientering/kjønnsidentitet eller annen
# tematikk som med rimelighet oppleves trist/sensitiv å bruke useriøst.
# Dette er lag 1: et deterministisk, gratis nøkkelordfilter som alltid
# kjører og ikke er avhengig av at Claude API er oppe (se lag 2,
# vurder_datagrunnlag(), lenger ned — den gir en mer nyansert vurdering
# på toppen, men denne lista er sikkerhetsgarantien uansett).
# ---------------------------------------------------------------------------

SENSITIVE_STIKKORD = [
    # Død
    "dø", "død", "dødsfall", "dødelighet", "avdød", "omkom", "drept", "bortgang",
    # Selvmord / selvskading
    "selvmord", "sjølvmord", "selvskading",
    # Vold / overgrep
    "vold", "voldtekt", "valdtekt", "overgrep", "mishandling", "incest",
    # Hatkriminalitet / diskriminering
    "hatkriminalitet", "hatprat", "diskriminering",
    # Seksuell orientering / kjønnsidentitet
    "homofil", "lesbisk", "bifil", "transperson", "transkjønn",
    "transseksuell", "skeiv", "kjønnsidentitet", "seksuell legning",
    "seksuell orientering",
    # Alvorlig sykdom
    "kreft", "uhelbredelig", "terminal", "dødssyk",
    # Barn / omsorgssvikt
    "barnemishandling", "omsorgssvikt",
]


def _er_sensitivt(tekst: str) -> bool:
    """Sjekker om en tabelltittel/serienavn inneholder noe fra
    SENSITIVE_STIKKORD. Bevisst delstreng- og overinkluderende — det er
    tryggere å hoppe over en tabell for mye enn én for lite."""
    t = (tekst or "").lower()
    return any(ord in t for ord in SENSITIVE_STIKKORD)


# ---------------------------------------------------------------------------
# 3. TABELLSØK
# ---------------------------------------------------------------------------

def sok_tabeller(sokeord: str, pagesize: int = 5) -> list:
    """Søker etter tabeller for ett søkeord. Returnerer kun tabeller med en
    årlig (eller årlig-lignende) tidsdimensjon — måneds-/kvartalstabeller
    filtreres bort siden vi bygger {år: verdi}-serier — og filtrerer bort
    tabeller som treffer INNHOLDSFILTER-lista (se over)."""
    try:
        svar = _api_get("/tables", {
            "query": sokeord, "lang": "no", "pageSize": pagesize,
        })
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"  Søk feilet for '{sokeord}': {e}")
        return []
    return [
        t for t in svar.get("tables", [])
        if t.get("timeUnit") in ("Annual", "Other") and not _er_sensitivt(t.get("label", ""))
    ]


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
# 5. INNHOLDSVURDERING, LAG 2 — Claude API vurderer datagrunnlaget
#
# Nøkkelordfilteret over (SENSITIVE_STIKKORD) er den harde garantien.
# Dette laget er en mer nyansert vurdering på toppen — Claude ser hele
# ukas kandidatliste under ett og flagger temaer nøkkelordlista ikke
# tenkte på. Feiler kallet, faller vi tilbake til kun nøkkelordfilteret
# (IKKE til å tillate alt) — se try/except i vurder_datagrunnlag().
# ---------------------------------------------------------------------------

DATAGRUNNLAG_SYSTEMPROMPT = (
    "Du vurderer en liste med norske SSB-statistikkserier som skal brukes "
    "til en useriøs humorside som lager tulle-korrelasjoner mellom "
    "tilfeldige tall (à la Tyler Vigens spurious-correlations.com). Siden "
    "skal bare tulle med lette, hverdagslige tema — som smørpriser, vær, "
    "dyr, forbruk, sport og lignende. Den skal ALDRI brukes til å tulle "
    "med: død, dødsfall eller dødelighet; selvmord eller selvskading; "
    "vold, overgrep eller voldtekt; hatkriminalitet eller diskriminering; "
    "seksuell orientering eller kjønnsidentitet (f.eks. homofile, "
    "lesbiske, transpersoner) som tema; alvorlig eller uhelbredelig "
    "sykdom; eller annen tematikk som med rimelighet kan oppleves trist, "
    "sensitiv eller sårende å se brukt i en useriøs sammenheng.\n\n"
    "For hver serie i lista under: vurder om navnet/temaet er trygt å "
    "bruke (passer=true) eller bør utelukkes fordi det faller inn under "
    "kategoriene over (passer=false). Vær på den forsiktige siden ved "
    "tvil. Kopier 'navn'-feltet nøyaktig som gitt i input."
)


def vurder_datagrunnlag(serier: list) -> list:
    """Ber Claude API vurdere hele lista med kandidatserier for uka samlet
    (ett kall) mot sidens "trygt useriøst"-kriterier. Feiler kallet på noen
    som helst måte (nettverk, kreditter, avvist svar, uventet svarformat)
    fanges det bredt med vilje, og funksjonen returnerer serier UENDRET —
    nøkkelordfilteret (SENSITIVE_STIKKORD) er allerede kjørt på hver serie
    og er den reelle sikkerhetsgarantien, uavhengig av om dette mer
    nyanserte laget lykkes denne uka."""
    if not serier:
        return serier
    try:
        import anthropic

        client = anthropic.Anthropic()
        liste_tekst = "\n".join(f"- {s['navn']}" for s in serier)
        respons = client.messages.create(
            model="claude-opus-5",
            max_tokens=2000,
            output_config={
                "effort": "medium",  # reelle konsekvenser hvis vurderingen bommer
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "vurderinger": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "navn": {"type": "string"},
                                        "passer": {"type": "boolean"},
                                    },
                                    "required": ["navn", "passer"],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        "required": ["vurderinger"],
                        "additionalProperties": False,
                    },
                },
            },
            system=DATAGRUNNLAG_SYSTEMPROMPT,
            messages=[{"role": "user", "content": liste_tekst}],
        )
        if respons.stop_reason == "refusal":
            raise RuntimeError("Claude avviste forespørselen")
        tekst = next(b.text for b in respons.content if b.type == "text")
        vurderinger = {v["navn"]: v["passer"] for v in json.loads(tekst)["vurderinger"]}

        # Manglende navn i svaret (bør ikke skje, men) tolkes som "passer" —
        # nøkkelordfilteret har uansett allerede sett på denne serien.
        beholdt = [s for s in serier if vurderinger.get(s["navn"], True)]
        forkastet = [s["navn"] for s in serier if not vurderinger.get(s["navn"], True)]
        if forkastet:
            print(f"  Claude-vurdering filtrerte bort: {', '.join(forkastet)}")
        return beholdt
    except Exception as e:
        print(f"  Klarte ikke vurdere datagrunnlaget via Claude API ({e}) — "
              f"stoler kun på nøkkelordfilteret denne uka.")
        return serier


# ---------------------------------------------------------------------------
# 6. KORRELASJON
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
            # Forsvar-i-dybden: samme innholdsfilter som sok_tabeller(),
            # i tilfelle en tabells auto-valgte totalkategori-navn
            # avslører noe selve tabelltittelen ikke gjorde.
            if _er_sensitivt(a["navn"]) or _er_sensitivt(b["navn"]):
                continue
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
# 7. TEKSTGENERERING (pseudo-vitenskapelig, norsk)
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
# (Banken over er fallback hvis generer_overskrift() under feiler — den
# er den primære kilden til overskrifter, og gir kortere, mer
# "nyhetsaktige" titler enn de fulle setningene her.)

OVERSKRIFT_SYSTEMPROMPT = (
    "Du skriver en kort, nyhetsaktig overskrift til en norsk "
    "tulle-korrelasjon-side (à la Tyler Vigens spurious-correlations.com). "
    "Du får to variabler fra SSB-statistikk og korrelasjonen mellom dem.\n\n"
    "Skriv ÉN kort overskrift (maks 8–10 ord) med tørr, absurd statistisk "
    "selvsikkerhet — som en avisoverskrift, ikke en hel setning. La "
    "korrelasjonens retning (positiv eller negativ) skinne naturlig "
    "gjennom i formuleringen der det passer.\n\n"
    "Eksempler på ønsket stil og lengde (ikke kopier disse, bruk dem kun "
    "som forbilde for tone):\n"
    "- Flere etternavn, større tettsteder\n"
    "- Etternavn og tettstedsareal følger hverandre mistenkelig tett\n"
    "- Jo flere etternavn, desto mer tettsted\n"
    "- Nye tall: Etternavn kan forklare størrelsen på norske tettsteder\n"
    "- SSB-tall avslører: Etternavn og tettsteder vokser hånd i hånd\n\n"
    "Ikke forklar vitsen. Ikke bruk anførselstegn. Svar KUN med selve "
    "overskriften, uten avsluttende punktum."
)

OVERSKRIFT_BRUKERMAL = (
    "Variabel A: {a}\n"
    "Variabel B: {b}\n"
    "Retning: A {retning} B\n"
    "Korrelasjon: r = {r:.3f} ({styrke})"
)


def generer_overskrift(felter: dict) -> str:
    """Ber Claude API skrive en kort, nyhetsaktig overskrift for ukens
    tall. Feiler aldri utad — enhver feil (manglende pakke/nøkkel,
    nettverk, avvist svar, uventet langt/tomt svar) fanges bredt med
    vilje, og faller tilbake til OVERSKRIFT_MALER-banken over."""
    try:
        import anthropic

        client = anthropic.Anthropic()
        respons = client.messages.create(
            model="claude-opus-5",
            max_tokens=100,
            output_config={"effort": "low"},  # enkel kreativ tekstoppgave
            system=OVERSKRIFT_SYSTEMPROMPT,
            messages=[{"role": "user", "content": OVERSKRIFT_BRUKERMAL.format(**felter)}],
        )
        if respons.stop_reason == "refusal":
            raise RuntimeError("Claude avviste forespørselen")
        tekst = "".join(b.text for b in respons.content if b.type == "text").strip()
        tekst = tekst.strip("\"'“”")
        # Sikkerhetsnett hvis Claude ignorerer korthets-instruksen eller
        # svarer med flere linjer — da er svaret ikke det vi ba om.
        if not tekst or len(tekst) > 100 or "\n" in tekst:
            raise RuntimeError(f"Uventet svar ({len(tekst)} tegn)")
        return tekst
    except Exception as e:
        print(f"  Klarte ikke generere overskrift via Claude API ({e}) — bruker fallback-mal.")
        return random.choice(OVERSKRIFT_MALER).format(**felter)

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
    overskrift = generer_overskrift(felter)
    ingress = random.choice(INGRESS_MALER).format(**felter)
    kommentar = random.choice(KOMMENTAR_BANK)
    spekulasjon = generer_spekulasjon(felter)
    # Bildeteksten under grafen: den ukentlig varierte "kommentar"-frasen
    # (KOMMENTAR_BANK) + en fast periode-opplysning. Selve disclaimer-
    # boksen lenger ned bruker derimot alltid samme faste ordlyd (se
    # HTML_MAL) — det er en av tekstene som skal stå uendret på siden.
    chart_note = f"{kommentar} Årlige tall, {felter['fra']}–{felter['til']}."
    return {
        "overskrift": overskrift, "ingress": ingress,
        "kommentar": kommentar, "chart_note": chart_note,
        "spekulasjon": spekulasjon,
    }


# ---------------------------------------------------------------------------
# 8. ARKIV — hver ukes side lagres permanent under arkiv/<år>/uke-<nn>.html,
#    og alle sider (forsiden og hver arkivside) får en innebygd, kollapsbar
#    meny som lenker til alle tidligere uker gruppert per år. Menyen bygges
#    fra selve filtreet under arkiv/ (ingen egen manifest-fil å holde synk)
#    — de committede HTML-filene ER fasiten.
# ---------------------------------------------------------------------------

ARKIV_ROT = pathlib.Path("arkiv")
TITTEL_MONSTER = re.compile(r"<title>(.*?)</title>", re.DOTALL)
# To separate menyblokker (mobil/desktop, se bygg_arkiv_blokker) — hver med
# sitt eget markørpar, siden de ligger på hvert sitt sted i HTML_MAL og
# derfor må kunne byttes ut hver for seg i oppdater_meny_i_eldre_filer().
MENY_MOBIL_MONSTER = re.compile(
    r"<!--ARKIV-MENY-MOBIL-START-->.*?<!--ARKIV-MENY-MOBIL-END-->", re.DOTALL
)
MENY_DESKTOP_MONSTER = re.compile(
    r"<!--ARKIV-MENY-DESKTOP-START-->.*?<!--ARKIV-MENY-DESKTOP-END-->", re.DOTALL
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
            # bygges (se bygg_arkiv_blokker/_bygg_arkiv_liste).
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


def _bygg_arkiv_liste(alle_innslag: list, denne_sti: str) -> str:
    """Bygger selve år→uke-lista (uten ytre wrapper) som gjenbrukes både i
    mobil- og desktop-arkivblokka, gruppert per år (nyeste år/uke øverst).
    Gjeldende side vises som ren tekst, ikke lenke. Årene er kollapsbare
    <details>-grupper (nyeste år åpent) — praktisk når arkivet vokser seg
    langt utover ett år med ukentlige innlegg."""
    if not alle_innslag:
        return ""

    per_aar = {}
    for e in sorted(alle_innslag, key=lambda e: (e["aar"], e["uke"]), reverse=True):
        per_aar.setdefault(e["aar"], []).append(e)

    nyeste_aar = max(per_aar)
    gjeldende_aar = next((e["aar"] for e in alle_innslag if e["sti"] == denne_sti), None)
    deler = []
    for aar in sorted(per_aar, reverse=True):
        apen = " open" if aar in (nyeste_aar, gjeldende_aar) else ""
        deler.append(f'<details class="archive-year-group"{apen}>'
                      f'<summary class="archive-year">{aar}</summary><ul>')
        for e in per_aar[aar]:
            tittel = html.escape(e["tittel"])
            if e["sti"] == denne_sti:
                deler.append(
                    f'<li class="archive-link na" aria-current="page">'
                    f'<i aria-hidden="true"></i><span><small>Uke {e["uke"]}</small>'
                    f"{tittel}</span></li>"
                )
            else:
                lenke = relativ_lenke(e["sti"], denne_sti)
                deler.append(
                    f'<li><a class="archive-link" href="{lenke}">'
                    f'<i aria-hidden="true"></i><span><small>Uke {e["uke"]}</small>'
                    f"{tittel}</span></a></li>"
                )
        deler.append("</ul></details>")
    return "".join(deler)


def bygg_arkiv_blokker(alle_innslag: list, denne_sti: str) -> tuple:
    """Returnerer (mobil_html, desktop_html) — to selvstendige menyblokker
    med samme innhold (samme lenkeliste), men ulik ytre struktur:
    - mobil: ett sammenleggbart <details>-element rett under toppfeltet
      (skjules over 800px bredde, se styles.css/CSS-en i HTML_MAL).
    - desktop: en sticky <aside> i venstre kolonne (skjules under 800px).
    Begge er pakket inn i sine egne HTML-kommentarmarkører slik at
    oppdater_meny_i_eldre_filer() kan bytte dem ut hver for seg i
    allerede skrevne arkivsider."""
    innhold = _bygg_arkiv_liste(alle_innslag, denne_sti)
    mobil = (
        "<!--ARKIV-MENY-MOBIL-START-->"
        '<details class="mobile-archive"><summary>Tidligere korrelasjoner</summary>'
        f"{innhold}</details>"
        "<!--ARKIV-MENY-MOBIL-END-->"
    )
    desktop = (
        "<!--ARKIV-MENY-DESKTOP-START-->"
        '<aside class="archive-card" id="arkiv" aria-labelledby="archive-title">'
        '<h2 id="archive-title">Tidligere korrelasjoner</h2>'
        f"{innhold}</aside>"
        "<!--ARKIV-MENY-DESKTOP-END-->"
    )
    return mobil, desktop


def oppdater_meny_i_eldre_filer(alle_innslag: list, unnta: set) -> None:
    """Bytter ut menyblokkene i hver eksisterende side (unntatt de som
    skrives fullt ut denne kjøringen) slik at gamle sider også lenker til
    ukens nye side — ellers ville menyen deres fryse på generasjonstidspunktet."""
    for e in alle_innslag:
        if e["sti"] in unnta:
            continue
        fil = pathlib.Path(e["sti"])
        innhold = fil.read_text(encoding="utf-8")
        ny_mobil, ny_desktop = bygg_arkiv_blokker(alle_innslag, e["sti"])
        oppdatert = MENY_MOBIL_MONSTER.sub(lambda _: ny_mobil, innhold, count=1)
        oppdatert = MENY_DESKTOP_MONSTER.sub(lambda _: ny_desktop, oppdatert, count=1)
        if oppdatert != innhold:
            fil.write_text(oppdatert, encoding="utf-8")


# ---------------------------------------------------------------------------
# 9. HTML-GENERERING
#
# Design: skandinavisk nettmagasin-uttrykk (varm papirbakgrunn, mørk
# marineblå toppfelt, hvite kort, Georgia-serif for overskrifter/brødtekst,
# system-sans for grensesnittekst) — se designreferansen
# korrelasjonsnerden-redesign/ (index.html + styles.css) som dette bygger
# på. Alt er fortsatt ett selvstendig, avhengighetsfritt HTML-dokument
# (samme prinsipp som før: ingen eksterne fonter/CDN-er), kun CSS/HTML-
# strukturen og Chart.js-oppsettet er nytt.
#
# Chart.js lastes fra en lokal fil (chart.umd.min.js, ligger ved siden av
# index.html i repoet) i stedet for en CDN. Det er ikke bare for å unngå
# et eksternt avhengighetspunkt på GitHub Pages — under testing viste det
# seg at cdnjs.cloudflare.com rett og slett ikke var DNS-oppløselig i
# nettleseren som skulle vise en lokalt åpnet index.html (net::ERR_NAME_NOT_RESOLVED),
# noe som gjorde grafen usynlig. Se scripts/hent_chartjs.py for hvordan
# filen ble hentet ned.
#
# nerden-spekulerer.png (assets/) er et vendoret, transparent PNG-ikon —
# samme "ikke last eksterne ressurser"-prinsipp som Chart.js-filen.
# ---------------------------------------------------------------------------

HTML_MAL = """<!DOCTYPE html>
<html lang="no">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Påfallende sammenhenger i offentlig SSB-statistikk, presentert med urimelig stor selvtillit.">
<title>{tittel}</title>
<script src="{chartjs_sti}"></script>
<style>
  :root {{
    --ink: #15243a; --deep: #0b1c30; --paper: #f7f4ee; --card: #fffefb;
    --line: #dedbd3; --muted: #6d7480;
    --blue: #2d65c8; --red: #d84a43;
    --yellow: #f3e7b3; --yellow-line: #dbc976;
  }}
  * {{ box-sizing: border-box; }}
  html {{ scroll-behavior: smooth; scroll-padding-top: 90px; }}
  body {{
    margin: 0; background: var(--paper); color: var(--ink);
    font: 16px/1.6 Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  a {{ color: inherit; }}
  a:focus-visible, summary:focus-visible, .chart-scroll:focus-visible {{
    outline: 3px solid rgba(45, 101, 200, 0.45); outline-offset: 3px;
  }}

  .site-header {{
    position: sticky; top: 0; z-index: 20;
    display: flex; align-items: center; justify-content: space-between;
    min-height: 70px; padding: 0 32px; color: #fff;
    background: rgba(11, 28, 48, 0.98); border-bottom: 1px solid rgba(255,255,255,0.12);
  }}
  .wordmark {{
    color: #fff; font: 700 clamp(1.15rem, 2vw, 1.45rem)/1 Georgia, "Times New Roman", serif;
    letter-spacing: 0.025em; text-decoration: none;
  }}
  .wordmark span {{ color: #f1d769; }}
  .site-header nav {{ display: flex; gap: 32px; font-size: 0.94rem; }}
  .site-header nav a {{ color: rgba(255,255,255,0.76); text-decoration: none; }}
  .site-header nav a:hover, .site-header nav a:focus-visible {{ color: #fff; }}

  .page-shell {{
    display: grid; grid-template-columns: minmax(220px, 292px) minmax(0, 1fr);
    gap: clamp(30px, 4vw, 68px); width: min(1480px, calc(100% - 48px));
    margin: auto; padding: 42px 0 76px;
  }}
  .archive-card {{
    position: sticky; top: 112px; align-self: start; min-height: 0;
    padding: 25px 24px; background: rgba(255,254,251,0.82);
    border: 1px solid var(--line); border-radius: 14px;
    box-shadow: 0 8px 30px rgba(21,36,58,0.04);
  }}
  .archive-card h2 {{ margin: 0 0 14px; font: 700 1.05rem/1.25 Georgia, "Times New Roman", serif; }}
  .mobile-archive summary {{ margin: 0; font: 700 1.05rem/1.25 Georgia, "Times New Roman", serif; }}
  .archive-year-group {{ margin: 0 0 6px; }}
  .archive-year-group summary {{
    padding: 2px 0 9px; color: #174eae; font-size: 0.92rem; font-weight: 700;
    list-style: none; cursor: pointer;
  }}
  .archive-year-group summary::-webkit-details-marker {{ display: none; }}
  .archive-year-group summary::before {{ content: "▾ "; }}
  .archive-year-group[open] summary::before {{ content: "▴ "; }}
  .archive-year-group ul {{ list-style: none; margin: 0; padding: 0; }}
  .archive-link {{
    display: grid; grid-template-columns: 8px 1fr; align-items: start; gap: 10px;
    padding: 9px 9px 10px 5px; border-radius: 9px;
    font: 400 0.91rem/1.4 Georgia, "Times New Roman", serif; text-decoration: none;
  }}
  a.archive-link:hover, a.archive-link:focus-visible {{ background: #eef2f8; outline: none; }}
  .archive-link.na {{ color: var(--muted); font-style: italic; }}
  .archive-link i {{ width: 6px; height: 6px; margin-top: 8px; background: var(--blue); border-radius: 50%; }}
  .archive-link small {{
    display: block; margin-bottom: 2px; color: #174eae;
    font: 800 0.68rem/1.4 Inter, sans-serif; letter-spacing: 0.12em; text-transform: uppercase;
  }}
  .archive-link.na small {{ color: var(--muted); }}
  .mobile-archive {{ display: none; }}
  article {{ min-width: 0; }}

  .eyebrow, .kicker, .stat small {{
    margin: 0; color: var(--muted); font-size: 0.7rem; font-weight: 800;
    letter-spacing: 0.145em; text-transform: uppercase;
  }}
  .article-header {{ max-width: 1050px; }}
  h1 {{
    max-width: 980px; margin: 9px 0 14px; color: var(--deep);
    font: 700 clamp(2.2rem, 4.3vw, 3.6rem)/1.08 Georgia, "Times New Roman", serif;
    letter-spacing: -0.03em;
  }}
  .lead {{
    max-width: 1050px; margin: 0; color: #374356;
    font: 400 clamp(1.1rem, 1.7vw, 1.35rem)/1.5 Georgia, "Times New Roman", serif;
  }}

  .stat-grid {{
    display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px;
    max-width: 720px; margin: 28px 0 18px;
  }}
  .stat {{
    padding: 15px 18px 16px; background: var(--card); border: 1px solid var(--line);
    border-radius: 12px; box-shadow: 0 5px 14px rgba(21,36,58,0.05);
  }}
  .stat small {{ display: block; }}
  .stat strong {{
    display: block; margin-top: 2px; color: var(--deep);
    font: 700 1.12rem/1.25 Georgia, "Times New Roman", serif;
  }}

  .chart-card {{
    padding: clamp(20px, 2.8vw, 34px); background: var(--card);
    border: 1px solid var(--line); border-radius: 14px;
    box-shadow: 0 18px 44px rgba(21,36,58,0.08);
  }}
  .chart-header {{ display: flex; align-items: end; justify-content: space-between; gap: 24px; margin-bottom: 22px; }}
  .chart-header h2 {{ margin: 3px 0 0; color: var(--deep); font: 700 clamp(1.2rem, 2.1vw, 1.55rem)/1.18 Georgia, "Times New Roman", serif; }}
  .legend {{ display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 10px 18px; color: #4d5868; font-size: 0.76rem; white-space: nowrap; }}
  .legend span {{ display: inline-flex; align-items: center; gap: 7px; }}
  .legend i {{ width: 26px; height: 3px; border-radius: 3px; }}
  .legend .blue {{ background: var(--blue); }}
  .legend .red {{ background: var(--red); }}
  .chart-scroll {{ width: 100%; overflow-x: auto; overscroll-behavior-inline: contain; scrollbar-width: thin; }}
  .chart-inner {{ position: relative; min-width: 680px; height: 380px; }}
  .chart-inner canvas {{ width: 100% !important; height: 100% !important; }}
  .chart-note {{ margin: 10px 0 0; color: var(--muted); font-size: 0.75rem; }}

  .nerd-callout {{
    display: grid; grid-template-columns: 96px minmax(0, 1fr); align-items: center;
    gap: clamp(20px, 3vw, 34px); margin-top: 18px; padding: clamp(22px, 3vw, 34px);
    background: var(--yellow); border: 1px solid var(--yellow-line); border-radius: 14px;
    box-shadow: 0 12px 30px rgba(98,79,10,0.08);
  }}
  .nerd-image {{
    display: grid; place-items: center; width: 96px; height: 96px; padding: 8px;
    background: rgba(255,255,255,0.3); border: 2px solid var(--ink); border-radius: 50%;
  }}
  .nerd-image img {{ display: block; width: 100%; height: 100%; object-fit: contain; }}
  .nerd-callout h2 {{ margin: 3px 0 0; color: var(--deep); font: 700 clamp(1.2rem, 2.1vw, 1.5rem)/1.18 Georgia, "Times New Roman", serif; }}
  .nerd-callout div > p:last-child {{
    margin: 8px 0 0; color: #253247;
    font: 400 clamp(1rem, 1.35vw, 1.12rem)/1.55 Georgia, "Times New Roman", serif;
  }}

  .disclaimer {{
    display: grid; grid-template-columns: 34px minmax(0, 1fr); gap: 16px;
    margin-top: 18px; padding: 24px 12px 25px; color: var(--muted);
    border-top: 1px solid var(--line); border-bottom: 1px solid var(--line);
  }}
  .disclaimer > span {{
    display: grid; place-items: center; width: 30px; height: 30px;
    border: 1.5px solid #a6abb2; border-radius: 50%;
    font: 700 1.05rem Georgia, "Times New Roman", serif;
  }}
  .disclaimer p {{ max-width: 960px; margin: 0; font: 400 0.98rem/1.55 Georgia, "Times New Roman", serif; }}
  footer {{
    display: flex; flex-wrap: wrap; gap: 8px 13px; padding: 21px 2px 0;
    color: var(--muted); font: 400 0.88rem Georgia, "Times New Roman", serif;
  }}
  footer b {{ font-weight: 400; }}

  @media (max-width: 1040px) {{
    .page-shell {{ grid-template-columns: 220px minmax(0, 1fr); gap: 28px; }}
    .chart-header {{ align-items: start; flex-direction: column; }}
    .legend {{ justify-content: flex-start; }}
  }}
  @media (max-width: 800px) {{
    .site-header {{ min-height: 62px; padding: 0 20px; }}
    .site-header nav {{ gap: 18px; font-size: 0.82rem; }}
    .site-header nav a:nth-child(2) {{ display: none; }}
    .mobile-archive {{
      display: block; margin: 18px 18px 0; padding: 0 16px 12px;
      background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    }}
    .mobile-archive summary {{ margin: 0 -16px; padding: 14px 16px; cursor: pointer; }}
    .mobile-archive summary::-webkit-details-marker {{ display: none; }}
    .page-shell {{ display: block; width: min(100% - 36px, 760px); padding-top: 30px; }}
    .archive-card {{ display: none; }}
    h1 {{ font-size: clamp(2.1rem, 11vw, 3rem); }}
    .lead {{ font-size: 1.08rem; }}
  }}
  @media (max-width: 600px) {{
    .site-header nav a:last-child {{ display: none; }}
    .stat {{ padding: 12px 10px; }}
    .stat small {{ font-size: 0.58rem; }}
    .stat strong {{ font-size: 0.9rem; }}
    .chart-card {{ margin-inline: -8px; }}
    .nerd-callout {{ display: block; }}
    .nerd-callout::after {{ display: block; clear: both; content: ""; }}
    .nerd-image {{ float: left; width: 72px; height: 72px; margin: 0 16px 8px 0; padding: 6px; }}
    footer {{ display: grid; gap: 2px; }}
    footer b {{ display: none; }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    html {{ scroll-behavior: auto; }}
  }}
</style>
</head>
<body>
<header class="site-header">
  <a class="wordmark" href="{forside_lenke}" aria-label="Korrelasjonsnerden – forsiden">Korrelasjonsnerden<span>.</span></a>
  <nav aria-label="Hovedmeny">
    <a href="{forside_lenke}">Forsiden</a>
    <a href="#arkiv">Arkiv</a>
    <a href="#om">Om prosjektet</a>
  </nav>
</header>

{meny_mobil}

<main class="page-shell" id="top">
{meny_desktop}

  <article id="artikkel">
    <header class="article-header">
      <p class="eyebrow">Publisert {dato}</p>
      <h1>{tittel}</h1>
      <p class="lead">{ingress}</p>
    </header>

    <section class="stat-grid" aria-label="Nøkkeltall">
      <div class="stat"><small>Korrelasjon</small><strong>r = {r_streng}</strong></div>
      <div class="stat"><small>Periode</small><strong>{fra}–{til}</strong></div>
      <div class="stat"><small>Datakilde</small><strong>SSB</strong></div>
    </section>

    <section class="chart-card" aria-labelledby="chart-heading">
      <header class="chart-header">
        <div>
          <p class="kicker">Utvikling over tid</p>
          <h2 id="chart-heading">To kurver. Én påfallende sammenheng.</h2>
        </div>
        <div class="legend" role="group" aria-label="Tegnforklaring">
          <span><i class="blue"></i>{navn_a}</span><span><i class="red"></i>{navn_b}</span>
        </div>
      </header>
      <div class="chart-scroll" tabindex="0" aria-label="Rull sidelengs for å se hele grafen på små skjermer">
        <div class="chart-inner">
          <canvas id="chart" role="img" aria-label="{chart_aria}"></canvas>
        </div>
      </div>
      <p class="chart-note">{chart_note}</p>
    </section>

    <aside class="nerd-callout" aria-labelledby="nerd-heading">
      <div class="nerd-image">
        <img src="{bilde_sti}" width="96" height="96" alt="Illustrasjon av en statistikknerd med lupe og et stolpediagram">
      </div>
      <div>
        <p class="kicker">Analyse*</p>
        <h2 id="nerd-heading">Nerden spekulerer</h2>
        <p>{spekulasjon}</p>
      </div>
    </aside>

    <section class="disclaimer" id="om" aria-label="Om prosjektet">
      <span aria-hidden="true">i</span>
      <p>
        Ingen årsakssammenheng er antydet, foreslått, eller ønsket. Denne
        siden genereres automatisk fra offentlige SSB-tall og er ment som
        underholdning. Korrelasjon er ikke kausalitet — det er faktisk hele
        poenget med siden.
      </p>
    </section>
    <footer>
      <span>Data fra Statistisk sentralbyrå</span><b aria-hidden="true">·</b><span>Laget med overdreven statistisk selvtillit</span>
    </footer>
  </article>
</main>
<script>
new Chart(document.getElementById('chart'), {{
  type: 'line',
  data: {{
    labels: {aar},
    datasets: [
      {{
        label: {navn_a_json},
        data: {x},
        borderColor: '#2d65c8',
        backgroundColor: '#2d65c8',
        pointBackgroundColor: '#2d65c8',
        pointBorderColor: '#fffefb',
        pointBorderWidth: 1.5,
        pointRadius: 3,
        borderWidth: 3,
        yAxisID: 'y',
        tension: 0.3,
      }},
      {{
        label: {navn_b_json},
        data: {y},
        borderColor: '#d84a43',
        backgroundColor: '#d84a43',
        pointBackgroundColor: '#d84a43',
        pointBorderColor: '#fffefb',
        pointBorderWidth: 1.5,
        pointRadius: 3,
        borderWidth: 3,
        yAxisID: 'y1',
        tension: 0.3,
      }}
    ]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    interaction: {{ intersect: false, mode: 'index' }},
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{
        grid: {{ color: '#e4e2dc' }},
        ticks: {{ color: '#737b87', font: {{ family: 'Inter, sans-serif', size: 11 }} }},
      }},
      y: {{
        type: 'linear', position: 'left',
        title: {{ display: true, text: {navn_a_json}, color: '#174eae', font: {{ family: 'Inter, sans-serif', size: 11, weight: '700' }} }},
        grid: {{ color: '#e4e2dc' }},
        ticks: {{ color: '#737b87', font: {{ family: 'Inter, sans-serif', size: 11 }} }},
      }},
      y1: {{
        type: 'linear', position: 'right',
        title: {{ display: true, text: {navn_b_json}, color: '#a8423e', font: {{ family: 'Inter, sans-serif', size: 11, weight: '700' }} }},
        grid: {{ drawOnChartArea: false }},
        ticks: {{ color: '#a8423e', font: {{ family: 'Inter, sans-serif', size: 11 }} }},
      }}
    }}
  }}
}});
</script>
</body>
</html>
"""


def lag_html(par: dict, tekst: dict, denne_sti: str, meny_mobil: str, meny_desktop: str) -> str:
    fra, til = par["aar"][0], par["aar"][-1]
    r_streng = f"{par['r']:.3f}"
    return HTML_MAL.format(
        tittel=html.escape(tekst["overskrift"]),
        ingress=html.escape(tekst["ingress"]),
        chart_note=html.escape(tekst["chart_note"]),
        spekulasjon=html.escape(tekst["spekulasjon"]),
        meny_mobil=meny_mobil,
        meny_desktop=meny_desktop,
        forside_lenke=relativ_lenke("index.html", denne_sti),
        dato=datetime.now().strftime("%d.%m.%Y"),
        chartjs_sti=relativ_lenke("chart.umd.min.js", denne_sti),
        bilde_sti=relativ_lenke("assets/nerden-spekulerer.png", denne_sti),
        r_streng=html.escape(r_streng),
        fra=fra, til=til,
        navn_a=html.escape(par["a"]),
        navn_b=html.escape(par["b"]),
        chart_aria=html.escape(
            f"Graf over {par['a']} og {par['b']} fra {fra} til {til}, "
            f"r = {r_streng}"
        ),
        aar=json.dumps(par["aar"]),
        navn_a_json=json.dumps(par["a"]),
        navn_b_json=json.dumps(par["b"]),
        x=json.dumps(par["x"]),
        y=json.dumps(par["y"]),
    )


# ---------------------------------------------------------------------------
# 10. HOVEDPROGRAM
# ---------------------------------------------------------------------------

def main():
    print("Søker etter tabeller hos SSB ...")
    serier = hent_alle_serier()
    print(f"Fikk {len(serier)} brukbare serier.")

    serier = vurder_datagrunnlag(serier)
    print(f"{len(serier)} serier igjen etter innholdsvurdering.")

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
        meny_mobil, meny_desktop = bygg_arkiv_blokker(alle, denne_sti=sti)
        innhold = lag_html(par, tekst, sti, meny_mobil, meny_desktop)
        with open(sti, "w", encoding="utf-8") as f:
            f.write(innhold)

    oppdater_meny_i_eldre_filer(alle, unnta={ny_sti, "index.html"})

    print(f"Ferdig: {par['a']} vs {par['b']} (r = {par['r']:.3f})")
    print(f"Skrev index.html og {ny_sti} (arkiv har nå {len(alle)} innslag)")


if __name__ == "__main__":
    main()
