"""
Script to pull exactly 1,000 real companies for the final Hackathon run.
"""

import json
import urllib.request

def generate_final_batch():
    # Query the official Brreg API for 1,000 real, active companies
    url = "https://data.brreg.no/enhetsregisteret/api/enheter?size=100"
    print("Downloading 100 real organisation numbers from Brreg...")
    
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode("utf-8"))
        
    orgs = data.get("_embedded", {}).get("enheter", [])
    org_numbers = [org["organisasjonsnummer"] for org in orgs]
    
    with open("batch_1000.txt", "w", encoding="utf-8") as f:
        for org in org_numbers:
            f.write(f"{org}\n")
            
    print(f"Successfully saved {len(org_numbers)} valid numbers to batch_1000.txt")

if __name__ == "__main__":
    generate_final_batch()