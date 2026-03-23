import os
import pandas as pd

# ---------------------------------------------------------------------------
# Config — adjust if needed
# ---------------------------------------------------------------------------
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(BASE_DIR, "data")
SENSEX_DIR  = os.path.join(DATA_DIR, "sensex")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
renamed   = 0
skipped   = 0
errors    = 0

for dirpath, _, filenames in os.walk(SENSEX_DIR):
    for filename in filenames:
        if not filename.startswith("SENSEX_"):
            skipped += 1
            continue

        filepath = os.path.join(dirpath, filename)

        # Parse: SENSEX_68400_CE_12_MAR_26.csv
        try:
            parts       = filename.replace(".csv", "").split("_")
            # parts: ['SENSEX', '68400', 'CE', '12', 'MAR', '26']
            strike      = parts[1]
            option_type = parts[2].lower()   # 'ce' or 'pe'
            new_name    = f"{strike}{option_type}.csv"
            new_path    = os.path.join(dirpath, new_name)
        except Exception as e:
            print(f"  ERROR parsing filename {filename}: {e}")
            errors += 1
            continue

        # Fix timestamp column header inside the file
        try:
            df = pd.read_csv(filepath)
            if "timestamp" in df.columns:
                df.rename(columns={"timestamp": "time_stamp"}, inplace=True)
            df.to_csv(new_path, index=False)
        except Exception as e:
            print(f"  ERROR processing {filename}: {e}")
            errors += 1
            continue

        # Remove old file only after new one is successfully written
        os.remove(filepath)
        print(f"  {filename} → {new_name}")
        renamed += 1

print(f"\nDone. Renamed: {renamed}  |  Skipped: {skipped}  |  Errors: {errors}")