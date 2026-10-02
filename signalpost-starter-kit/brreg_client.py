"""
Client for the Norwegian Brønnøysund Register Centre (Brreg) Enhetsregisteret API.
Docs: https://data.brreg.no/enhetsregisteret/api/docs/index.html
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class Forretningsadresse(BaseModel):
    land: str | None = None
    landkode: str | None = None
    postnummer: str | None = None
    poststed: str | None = None
    adresse: list[str] | None = None
    kommune: str | None = None
    kommunenummer: str | None = None

class Organisasjonsform(BaseModel):
    kode: str
    beskrivelse: str

class Naeringskode(BaseModel):
    kode: str
    beskrivelse: str

class Enhet(BaseModel):
    organisasjonsnummer: str
    navn: str
    organisasjonsform: Organisasjonsform
    naeringskode1: Naeringskode | None = None
    forretningsadresse: Forretningsadresse | None = None


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

BASE_URL = "https://data.brreg.no/enhetsregisteret/api/enheter"


async def lookup_enhet(orgnr: str) -> Enhet | None:
    """Fetch a Norwegian organisation from the Enhetsregisteret API."""
    url = f"{BASE_URL}/{orgnr}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url, headers={"Accept": "application/json"})

        if response.status_code == 404:
            return None

        response.raise_for_status()
        return Enhet.model_validate(response.json())


# ---------------------------------------------------------------------------
# Quick smoke-test (run with:  python brreg_client.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio

    async def _demo() -> None:
        # Equinor ASA — a well-known Norwegian company
        result = await lookup_enhet("923609016")
        if result:
            print(f"Name  : {result.navn}")
            print(f"OrgNr : {result.organisasjonsnummer}")
            print(f"Type  : {result.organisasjonsform.kode} - {result.organisasjonsform.beskrivelse}")
            if result.naeringskode1:
                print(f"NACE  : {result.naeringskode1.kode} - {result.naeringskode1.beskrivelse}")
            addr = result.forretningsadresse
            if addr:
                street = ", ".join(addr.adresse or [])
                print(f"Addr  : {street}, {addr.postnummer} {addr.poststed}, {addr.land}")
        else:
            print("Organisation not found.")

        # Test 404 handling
        missing = await lookup_enhet("000000000")
        print(f"\n404 test -> returned: {missing}")  # should print None

    async def _main():
        await _demo()

    asyncio.run(_main())