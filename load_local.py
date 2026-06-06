"""
load_local.py
Loads all Fantrax batch SQL files into LOCAL PostgreSQL (pgAdmin 4 / fantrax DB).

Run from Anaconda Prompt:
  python "C:/Users/James/OneDrive/Documents/Claude/Projects/Fantasy Baseball (1)/load_local.py"

What it does:
  1. TRUNCATEs the 4 player tables + HOD_Drafts in local Postgres
  2. Fixes Ros column: strips '%' from quoted percent strings ('100%' → 100)
  3. Loads all batch files in order, reporting success/failure per file
  4. Prints final row counts for all 6 tables
"""

import glob
import os
import re
import psycopg2

# ── Local PostgreSQL connection ───────────────────────────────────────────────
conn = psycopg2.connect(
    host="localhost",
    port=5432,
    dbname="fantrax",
    user="postgres",
    password="Blurbinpostgres7&"
)
cur = conn.cursor()

# ── Config ────────────────────────────────────────────────────────────────────
BATCH_DIR = r"C:\Users\James\OneDrive\Documents\Claude\Projects\Fantasy Baseball (1)\batches"

# Tables to TRUNCATE then reload (Transaction History already managed separately)
RELOAD_TABLES = [
    "Fantrax_HOD_Drafts",
    "Fantrax_Players_Hitters_Rawlings",
    "Fantrax_Players_Hitters_Topps",
    "Fantrax_Players_Pitchers_Rawlings",
    "Fantrax_Players_Pitchers_Topps",
]

# Batch files to skip (Transaction History is loaded via MCP / direct_transfer)
SKIP = {"Fantrax_Transaction_History_000.sql"}

# ── Step 1: TRUNCATE ──────────────────────────────────────────────────────────
print("── Truncating tables ────────────────────────────────────────────────────")
for t in RELOAD_TABLES:
    cur.execute(f'TRUNCATE TABLE fantrax."{t}"')
    print(f"  ✓  {t}")
conn.commit()

# ── Step 2: Collect and sort batch files ─────────────────────────────────────
all_files = sorted(glob.glob(os.path.join(BATCH_DIR, "*.sql")))
to_load   = [f for f in all_files if os.path.basename(f) not in SKIP]
print(f"\n── Loading {len(to_load)} batch files ───────────────────────────────────────")


def clean_sql(sql):
    """
    Fix data type issues before loading into local Postgres:
      - Strip '%' from quoted percentage strings so numeric Ros column accepts them.
        '100%' → 100,  '99%' → 99,  '0.94%' → 0.94
        Works for any XX% or XX.XX% pattern.
    """
    return re.sub(r"'(\d+(?:\.\d+)?)%'", r"\1", sql)


# ── Step 3: Load loop ─────────────────────────────────────────────────────────
errors = []
for fpath in to_load:
    fname = os.path.basename(fpath)
    with open(fpath, "r", encoding="utf-8") as fh:
        sql = clean_sql(fh.read().strip())
    try:
        cur.execute(sql)
        conn.commit()
        print(f"  ✓  {fname}")
    except Exception as e:
        conn.rollback()
        print(f"  ✗  {fname}  →  {e}")
        errors.append((fname, str(e)))

# ── Step 4: Row count verification ───────────────────────────────────────────
print("\n── Row counts ───────────────────────────────────────────────────────────")
all_tables = [
    "Fantrax_Transaction_History",
    "Fantrax_HOD_Drafts",
    "Fantrax_Players_Hitters_Rawlings",
    "Fantrax_Players_Hitters_Topps",
    "Fantrax_Players_Pitchers_Rawlings",
    "Fantrax_Players_Pitchers_Topps",
]
expected = {
    "Fantrax_Transaction_History":       327,
    "Fantrax_HOD_Drafts":               1540,
    "Fantrax_Players_Hitters_Rawlings": 3119,
    "Fantrax_Players_Hitters_Topps":    3119,
    "Fantrax_Players_Pitchers_Rawlings":3566,
    "Fantrax_Players_Pitchers_Topps":   3566,
}
print(f"  {'Table':<45} {'Rows':>8} {'Expected':>10} {'Match':>7}")
print("  " + "-" * 75)
for t in all_tables:
    cur.execute(f'SELECT COUNT(*) FROM fantrax."{t}"')
    n   = cur.fetchone()[0]
    exp = expected.get(t, "?")
    match = "✓" if n == exp else "✗"
    print(f"  {t:<45} {n:>8} {str(exp):>10} {match:>7}")

# ── Step 5: Error summary ─────────────────────────────────────────────────────
if errors:
    print(f"\n── {len(errors)} error(s) ───────────────────────────────────────────────────")
    for fname, msg in errors:
        print(f"  {fname}: {msg}")
else:
    print("\nAll files loaded successfully.")

cur.close()
conn.close()
