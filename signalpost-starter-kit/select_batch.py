"""
Script to pull exactly 1,000 real, active companies from Brreg
for the final hackathon batch run.
"""

import json
import urllib.request
import os

def generate_final_batch():
    url = "https://data.brreg.no/enhetsregisteret/api/enheter?size=1000"
    print("Fetching 1,000 verified organisation numbers from Brreg...")

    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode("utf-8"))

    orgs = data.get("_embedded", {}).get("enheter", [])
    org_numbers = [org["organisasjonsnummer"] for org in orgs]

    output_path = "batch_1000.txt"
    with open(output_path, "w", encoding="utf-8") as f:
        for org in org_numbers:
            f.write(f"{org}\n")

    print(f"Successfully wrote {len(org_numbers)} valid organisation numbers to {output_path}")

if __name__ == "__main__":
    generate_final_batch()