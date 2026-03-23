import os
import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

DIRECTORIES = [
    os.path.join(DATA_DIR, "sensex"),
    os.path.join(DATA_DIR, "nifty", "options"),
]

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
deleted = 0
kept    = 0
errors  = 0

for base_dir in DIRECTORIES:
    print(f"\nScanning {base_dir}...")
    for dirpath, _, filenames in os.walk(base_dir):
        for filename in filenames:
            if not filename.endswith(".csv"):
                continue

            filepath = os.path.join(dirpath, filename)

            try:
                df = pd.read_csv(filepath)
                if df.empty:
                    os.remove(filepath)
                    print(f"  Deleted: {filepath}")
                    deleted += 1
                else:
                    kept += 1
            except Exception as e:
                # Catches completely empty files with no headers (Nifty 1-byte files)
                # pd.read_csv will raise an error on truly empty files
                os.remove(filepath)
                print(f"  Deleted (unreadable): {filepath}")
                deleted += 1

print(f"\nDone. Deleted: {deleted}  |  Kept: {kept}  |  Errors: {errors}")