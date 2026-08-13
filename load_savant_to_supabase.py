"""
load_savant_to_supabase.py
--------------------------
Reads the CSV produced by savant_extract.py and upserts rows into
public.savant_metrics on Supabase via the REST API.

Required env vars:
    SUPABASE_ANON_KEY   — anon key from Supabase Settings → API
                          (also accepts SUPABASE_SERVICE_KEY for service role)

Optional env vars:
    SUPABASE_URL        — override project URL (default: HoD project)
    SAVANT_YEAR         — year suffix for input CSV (default: current year)

Usage:
    SUPABASE_ANON_KEY=eyJ... python load_savant_to_supabase.py
"""

import os
import sys
import json
import time
from datetime import datetime

import pandas as pd
import requests

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    "https://rlwidfirrdwolaywjpca.supabase.co"
)
ANON_KEY = os.environ.get("SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")

if not ANON_KEY:
    sys.exit(
        "ERROR: Set SUPABASE_ANON_KEY env var.\n"
        "  Local:   set SUPABASE_ANON_KEY=eyJ... (Windows) or export SUPABASE_ANON_KEY=eyJ... (bash)\n"
        "  Actions: add SUPABASE_ANON_KEY to repo Secrets"
    )

YEAR     = int(os.environ.get("SAVANT_YEAR") or datetime.now().year)
IN_FILE  = f"savant_metrics_{YEAR}.csv"
ENDPOINT = f"{SUPABASE_URL}/rest/v1/savant_metrics"
CHUNK    = 50

HEADERS = {
    "apikey":        ANON_KEY,
    "Authorization": f"Bearer {ANON_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates",  # upsert
}

# ── Column map: CSV column → DB column ───────────────────────────────────────
# Only columns confirmed to exist in public.savant_metrics are included.
# Unmapped CSV columns are silently dropped.
COL_MAP = {
    "player_name":                     "player_name",
    "player_type":                     "player_type",   # NOT NULL in schema: "batter" | "pitcher"
    # Percentile ranks (batter & pitcher share same columns, keyed by player_name)
    "savant_exit_velocity":            "savant_ev_pct",
    "savant_k_percent":                "savant_k_pct",
    "savant_bb_percent":               "savant_bb_pct",
    # Expected stats
    "savant_pa":                       "savant_pa",
    "savant_est_ba":                   "savant_xba",
    "savant_est_slg":                  "savant_xslg",
    "savant_est_woba":                 "savant_xwoba",
    "savant_est_woba_minus_woba_diff": "savant_xwoba_diff",
    # Speed & contact
    "savant_sprint_speed":             "savant_sprint_speed",
    "savant_avg_hit_speed":            "savant_avg_ev",
    "savant_ev95percent":              "savant_ev95_pct",
    "savant_max_hit_speed":            "savant_max_ev",
    "savant_brl_percent":              "savant_brl_pct",
    # Pitcher expected ERA (pitchers only, null for batters)
    "savant_xera":                     "savant_xera",
}

# ── Load CSV ──────────────────────────────────────────────────────────────────
if not os.path.exists(IN_FILE):
    sys.exit(f"ERROR: {IN_FILE} not found. Run savant_extract.py first.")

df = pd.read_csv(IN_FILE)
print(f"Loaded {len(df)} rows from {IN_FILE}")

# Keep only columns that are in the map
keep = {c: COL_MAP[c] for c in df.columns if c in COL_MAP}
df   = df[list(keep.keys())].rename(columns=keep)

# Safety dedup — protects against duplicate PKs from traded players appearing
# multiple times in pybaseball sources. The extract script also deduplicates,
# but this is a second line of defense before hitting Supabase.
if "player_name" in df.columns and "player_type" in df.columns:
    before = len(df)
    df = df.drop_duplicates(subset=["player_name", "player_type"], keep="last")
    if len(df) < before:
        print(f"Dropped {before - len(df)} duplicate rows before upload")

# Use pandas JSON serialiser — converts NaN → null correctly.
# Plain json.dumps() serialises NaN as bare NaN which is invalid JSON
# and causes Supabase PGRST102 errors.
rows = json.loads(df.to_json(orient="records"))
print(f"Uploading {len(rows)} rows in chunks of {CHUNK} ...")

# ── Upload in chunks ──────────────────────────────────────────────────────────
errors   = 0
uploaded = 0

for i in range(0, len(rows), CHUNK):
    chunk   = rows[i : i + CHUNK]
    payload = json.dumps(chunk)

    for attempt in range(3):
        try:
            resp = requests.post(ENDPOINT, headers=HEADERS, data=payload, timeout=30)
            if resp.status_code in (200, 201):
                uploaded += len(chunk)
                print(f"  chunk {i//CHUNK + 1:>3}: {len(chunk)} rows OK")
                break
            else:
                print(f"  chunk {i//CHUNK + 1:>3}: HTTP {resp.status_code} — {resp.text[:200]}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
                else:
                    errors += len(chunk)
        except requests.exceptions.RequestException as e:
            print(f"  chunk {i//CHUNK + 1:>3}: request error — {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                errors += len(chunk)

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*40}")
print(f"Uploaded : {uploaded}")
print(f"Errors   : {errors}")

if errors:
    sys.exit(f"Completed with {errors} failed rows.")
else:
    print("All rows upserted successfully.")
