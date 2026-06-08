"""
csv_to_batches.py
Converts Fantrax CSV exports into batch SQL INSERT files consumed by
load_supabase_actions.py.

Reads CSVs from FANTRAX_SOURCES_DIR (or local Sources folder).
Writes batch SQL files to ./batches/ (500 rows per file).

The two HOD Drafts CSVs (Topps + Rawlings) are merged into one table.

Usage:
    python csv_to_batches.py
"""

import os
import re
import csv
import sys
from pathlib import Path
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────────────────────

SOURCES_DIR = Path(
    os.environ.get(
        "FANTRAX_SOURCES_DIR",
        r"C:\Users\James\OneDrive\Documents\Claude-Working-Folder\Fantrax\Sources",
    )
)
BATCHES_DIR = Path(os.environ.get("FANTRAX_BATCHES_DIR", "batches"))
SCHEMA      = "fantrax"
ROWS_PER_FILE = 500

# CSV filename stem → SQL table name
# HOD Drafts: two CSVs merge into one table
CSV_TABLE_MAP = {
    "Fantrax_Players_Hitters_Topps":    "Fantrax_Players_Hitters_Topps",
    "Fantrax_Players_Hitters_Rawlings":  "Fantrax_Players_Hitters_Rawlings",
    "Fantrax_Players_Pitchers_Topps":   "Fantrax_Players_Pitchers_Topps",
    "Fantrax_Players_Pitchers_Rawlings": "Fantrax_Players_Pitchers_Rawlings",
    "Fantrax_Transaction_History":       "Fantrax_Transaction_History",
    "Fantrax_HOD_Drafts_Topps":         "Fantrax_HOD_Drafts",
    "Fantrax_HOD_Drafts_Rawlings":       "Fantrax_HOD_Drafts",
}

# CSVs that share a table (combined in order listed)
HOD_DRAFTS_CSVS = ["Fantrax_HOD_Drafts_Topps", "Fantrax_HOD_Drafts_Rawlings"]

# Map raw Fantrax CSV column names → Supabase schema column names for HOD_Drafts.
# "League" is not in the Fantrax export — it is derived from which CSV the row
# came from (Topps or Rawlings) and injected by the ETL.
HOD_DRAFTS_COL_MAP = {
    "Player ID":    "player_id",
    "Round":        "Round",
    "Pick":         "Pick",
    "Ov Pick":      "ov_pick",
    "Pos":          "Pos",
    "Player":       "Player",
    "Team":         "Team",
    "Fantasy Team": "fantasy_team",
    "Time (CDT)":   "date_cdt",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def sql_val(raw: str) -> str:
    """
    Convert a raw CSV cell to a SQL literal.
    - Empty string or 'NULL' (case-insensitive) → NULL
    - Pure integer string                        → unquoted integer
    - Pure float string                          → unquoted float
    - Anything else                              → single-quoted string,
                                                   single-quotes doubled
    """
    stripped = raw.strip()
    if stripped == "" or stripped.upper() == "NULL":
        return "NULL"
    # Integer — strip commas first to handle Fantrax salary formatting
    # e.g. "20,878,000" → 20878000 (bigint), "Smith, J." stays a string
    no_commas = stripped.replace(",", "")
    if re.match(r'^-?\d+$', no_commas):
        return no_commas
    # Float (handles '12.5', '-0.5', '100%' stripped elsewhere, etc.)
    try:
        float(stripped)
        return stripped
    except ValueError:
        pass
    # String — escape internal single quotes
    return "'" + stripped.replace("'", "''") + "'"


def read_csv_rows(csv_path: Path) -> tuple[list[str], list[list[str]]]:
    """Return (headers, rows) from a CSV file."""
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        headers = next(reader)
        rows = list(reader)
    return headers, rows


def write_batch_files(table_name: str, headers: list[str], rows: list[list[str]]) -> int:
    """
    Write rows as batch SQL INSERT files to BATCHES_DIR.
    Overwrites any existing batch files for this table.
    Returns the total number of rows written.
    """
    BATCHES_DIR.mkdir(parents=True, exist_ok=True)

    # Remove any existing batch files for this table
    for old in BATCHES_DIR.glob(f"{table_name}_*.sql"):
        old.unlink()

    n_cols = len(headers)
    col_list = ", ".join(f'"{h}"' for h in headers)
    insert_prefix = f'INSERT INTO {SCHEMA}."{table_name}" ({col_list}) VALUES'

    total = 0
    file_idx = 0
    for chunk_start in range(0, len(rows), ROWS_PER_FILE):
        chunk = rows[chunk_start : chunk_start + ROWS_PER_FILE]
        batch_path = BATCHES_DIR / f"{table_name}_{file_idx:03d}.sql"
        with open(batch_path, "w", encoding="utf-8") as f:
            for row in chunk:
                # Ensure row length matches headers — pad with empty or truncate
                if len(row) != n_cols:
                    row = (list(row) + [''] * n_cols)[:n_cols]
                vals = ", ".join(sql_val(cell) for cell in row)
                f.write(f"{insert_prefix} ({vals});\n")
        total += len(chunk)
        file_idx += 1

    return total


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    log("=" * 60)
    log("  CSV → Batch SQL Converter")
    log(f"  Sources : {SOURCES_DIR}")
    log(f"  Batches : {BATCHES_DIR.resolve()}")
    log("=" * 60)

    results = []
    hod_rows: list[list[str]] = []
    hod_headers: list[str] = []

    # ── Process single-CSV tables ──────────────────────────────────────────────
    single_csvs = [k for k in CSV_TABLE_MAP if k not in HOD_DRAFTS_CSVS]
    for stem in single_csvs:
        csv_path = SOURCES_DIR / f"{stem}.csv"
        if not csv_path.exists():
            log(f"  ✗  Missing: {csv_path.name}")
            results.append((stem, False, 0))
            continue

        table = CSV_TABLE_MAP[stem]
        log(f"\n── {table} {'─' * max(1, 52 - len(table))}")
        headers, rows = read_csv_rows(csv_path)
        log(f"   {len(rows)} rows, {len(headers)} columns")
        written = write_batch_files(table, headers, rows)
        log(f"   ✓  {written} rows → {BATCHES_DIR.name}/{table}_*.sql")
        results.append((table, True, written))

    # ── Process HOD Drafts (merge Topps + Rawlings) ────────────────────────────
    log(f"\n── Fantrax_HOD_Drafts (Topps + Rawlings combined) {'─' * 5}")
    hod_ok = True
    for stem in HOD_DRAFTS_CSVS:
        csv_path = SOURCES_DIR / f"{stem}.csv"
        if not csv_path.exists():
            log(f"  ✗  Missing: {csv_path.name}")
            hod_ok = False
            continue
        # Derive league tag from CSV stem name
        league_val = "Topps" if "Topps" in stem else "Rawlings"
        headers, rows = read_csv_rows(csv_path)
        # Rename headers to Supabase schema names; append derived League column
        mapped_headers = [HOD_DRAFTS_COL_MAP.get(h, h) for h in headers] + ["League"]
        if not hod_headers:
            hod_headers = mapped_headers
        n_hdr = len(hod_headers)
        # Append league value to every row
        mapped_rows = [list(row) + [league_val] for row in rows]
        bad = [(i, len(r)) for i, r in enumerate(mapped_rows) if len(r) != n_hdr]
        hod_rows.extend(mapped_rows)
        log(f"   {stem}: {len(rows)} rows, {len(headers)} columns → mapped to {len(mapped_headers)} cols with League='{league_val}'")
        log(f"   Raw headers  : {headers}")
        log(f"   Mapped headers: {mapped_headers}")
        if bad:
            log(f"   ⚠  {len(bad)} rows width mismatch (expected {n_hdr}). "
                f"First: row {bad[0][0]}, len={bad[0][1]}")

    if hod_ok and hod_rows:
        written = write_batch_files("Fantrax_HOD_Drafts", hod_headers, hod_rows)
        log(f"   ✓  {written} combined rows → {BATCHES_DIR.name}/Fantrax_HOD_Drafts_*.sql")
        results.append(("Fantrax_HOD_Drafts", True, written))
    else:
        results.append(("Fantrax_HOD_Drafts", False, 0))

    # ── Summary ──────────────────────────────────────────────────────────────────────────
    log("\n" + "=" * 60)
    log("  Summary")
    log("=" * 60)
    total_rows = 0
    failed = []
    for name, ok, n in results:
        status = "✓" if ok else "✗"
        log(f"  {status}  {name:45s}  {n:>6,} rows")
        if ok:
            total_rows += n
        else:
            failed.append(name)

    log(f"\n  Total rows written: {total_rows:,}")
    if failed:
        log(f"  FAILED: {failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
