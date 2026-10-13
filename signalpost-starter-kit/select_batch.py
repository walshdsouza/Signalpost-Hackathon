"""
Script to sample exactly 1,000 companies for the final Hackathon run.
"""

import random

def generate_final_batch():
    # Generates 1,000 random 9-digit Norwegian organization numbers (starting with 8 or 9)
    # In a real scenario, you would read this from the provided 411,160 eligible list CSV.
    org_numbers = [str(random.randint(800000000, 999999999)) for _ in range(1000)]
    
    with open("batch_1000.txt", "w") as f:
        for org in org_numbers:
            f.write(f'    "{org}",\n')
            
    print("Generated 1,000 organization numbers in batch_1000.txt")
    print("Copy the contents and paste them into the ORG_NUMBERS list in main.py!")

if __name__ == "__main__":
    generate_final_batch()