#!/usr/bin/env python3
"""Laster ned en fast versjon av Chart.js (UMD-bundle) og lagrer den som
chart.umd.min.js i repo-roten, ved siden av index.html.

Kjøres manuelt når du vil oppdatere Chart.js-versjonen — IKKE av
ukentlig.yml. Grunnen til at Chart.js er vendored i repoet i stedet for
lastet fra en CDN i selve HTML-malen (se tullkorrelasjon.py): ved testing
klarte ikke nettleseren å DNS-oppløse cdnjs.cloudflare.com i det hele
tatt når index.html ble åpnet som lokal fil, så grafen ble usynlig uten
noen synlig feilmelding på selve siden (bare i devtools-konsollen).
Vendoring fjerner det eksterne avhengighetspunktet helt.
"""

import pathlib
import urllib.request

VERSJON = "4.4.0"
URL = f"https://cdnjs.cloudflare.com/ajax/libs/Chart.js/{VERSJON}/chart.umd.min.js"
MAL_FIL = pathlib.Path(__file__).resolve().parent.parent / "chart.umd.min.js"


def main():
    req = urllib.request.Request(URL, headers={"User-Agent": "tullkorrelasjon/2.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        innhold = resp.read()
    MAL_FIL.write_bytes(innhold)
    print(f"Skrev {MAL_FIL} ({len(innhold)} bytes, Chart.js {VERSJON})")


if __name__ == "__main__":
    main()
