"""
load_supabase.py
Loads all Fantrax batch SQL files into Supabase.
Run from Spyder: %runfile C:/Users/James/load_supabase.py --wdir
"""

import os
import glob
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

# ── Connection ──────────────────────────────────────────────────────────────
sb_password = quote_plus("Blurbinsupabase7&")
engine = create_engine(
    f"postgresql+psycopg2://postgres.rlwidfirrdwolaywjpca:{sb_password}"
    f"@aws-1-us-east-2.pooler.supabase.com:5432/postgres",
    connect_args={"sslmode": "require", "gssencmode": "disable"}
)

# ── Batch file directory ─────────────────────────────────────────────────────
BATCH_DIR = r"C:\Users\James\OneDrive\Documents\Claude\Projects\Fantasy Baseball (1)\batches"

# Already loaded — skip these
SKIP = {"Fantrax_Transaction_History_000.sql"}

files = sorted(glob.glob(os.path.join(BATCH_DIR, "*.sql")))
to_load = [f for f in files if os.path.basename(f) not in SKIP]

print(f"Files to load: {len(to_load)}")

# Tables where plus_minus (position 14, 1-indexed) was dropped
DROP_COL14 = {
    "Fantrax_Players_Hitters_Rawlings",
    "Fantrax_Players_Hitters_Topps",
    "Fantrax_Players_Pitchers_Rawlings",
    "Fantrax_Players_Pitchers_Topps",
}

import re
import csv
import io

def strip_pos14(sql):
    """Remove the 14th value from every INSERT VALUES (...) line."""
    def remove_col(m):
        inner = m.group(1)
        reader = csv.reader(io.StringIO(inner), skipinitialspace=True)
        vals = next(reader)
        if len(vals) > 13:    # only strip if this is a full-column VALUES row
            del vals[13]
        return "(" + ", ".join(vals) + ")"
    return re.sub(r'\(([^()]+)\)', remove_col, sql)

# ── Load loop ────────────────────────────────────────────────────────────────
errors = []
for fpath in to_load:
    fname = os.path.basename(fpath)
    # Determine table name from filename (strip _NNN.sql suffix)
    table = re.sub(r'_\d+\.sql$', '', fname)
    with open(fpath, "r", encoding="utf-8") as fh:
        sql = fh.read().strip()
    if table in DROP_COL14:
        sql = strip_pos14(sql)
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
        print(f"  ✓  {fname}")
    except Exception as e:
        print(f"  ✗  {fname}  →  {e}")
        errors.append((fname, str(e)))

# ── Row count verification ───────────────────────────────────────────────────
print("\n── Row counts ──────────────────────────────────────")
tables = [
    'fantrax."Fantrax_Transaction_History"',
    'fantrax."Fantrax_HOD_Drafts"',
    'fantrax."Fantrax_Players_Hitters_Rawlings"',
    'fantrax."Fantrax_Players_Hitters_Topps"',
    'fantrax."Fantrax_Players_Pitchers_Rawlings"',
    'fantrax."Fantrax_Players_Pitchers_Topps"',
]
with engine.connect() as conn:
    for t in tables:
        n = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
        print(f"  {t.split('.')[-1].strip('\"'):45s}  {n:>6}")

if errors:
    print(f"\n── {len(errors)} error(s) ──")
    for fname, msg in errors:
        print(f"  {fname}: {msg}")
else:
    print("\nAll files loaded successfully.")
