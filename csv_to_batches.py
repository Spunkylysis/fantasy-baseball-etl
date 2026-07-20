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
    "Fantrax_Transaction_History_Topps":    "Fantrax_Transaction_History",
    "Fantrax_Transaction_History_Rawlings": "Fantrax_Transaction_History",
    "Fantrax_HOD_Drafts_Topps":            "Fantrax_HOD_Drafts",
    "Fantrax_HOD_Drafts_Rawlings":          "Fantrax_HOD_Drafts",
}

# CSVs that share a table (combined in order listed)
TH_CSVS       = ["Fantrax_Transaction_History_Topps", "Fantrax_Transaction_History_Rawlings"]
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


# Fantrax standings multi-section CSV — the one file downloaded for "Fantrax_Standings"
# contains 25 stacked sub-tables.  We only need the first 5.
STANDINGS_CSV = "Fantrax_Standings"

# Maps the section header text (first cell of section header row) to an internal key.
# Only these 5 are parsed; the remaining 20 per-category re-sort sections are dropped.
STANDINGS_SECTIONS = {
    "Standings":                       "overall",
    "Standings - Points - Hitting":    "pts_hit",
    "Standings - Points - Pitching":   "pts_pit",
    "Standings - Statistics - Hitting": "stats_hit",
    "Standings - Statistics - Pitching": "stats_pit",
}

# Output column headers for the 3 Supabase standings tables.
STANDINGS_HEADERS = ["Rk", "Team", "League", "FPts", "fp_per_g", "GP", "Hit_pts", "Pit_pts", "PBL_pts"]
STANDINGS_HIT_HEADERS = [
    "Team", "League", "Rk", "FPts", "GP",
    "pts_R",  "pts_1B",  "pts_2B",  "pts_3B",  "pts_HR",
    "pts_RBI","pts_BB",  "pts_SO",  "pts_SB",  "pts_GIDP",
    "stat_R", "stat_1B", "stat_2B", "stat_3B", "stat_HR",
    "stat_RBI","stat_BB","stat_SO", "stat_SB", "stat_GIDP",
]
STANDINGS_PIT_HEADERS = [
    "Team", "League", "Rk", "FPts", "GP",
    "pts_IP",  "pts_K",   "pts_L",  "pts_ER", "pts_H",
    "pts_BB",  "pts_SV",  "pts_QS", "pts_CG", "pts_hld_po",
    "stat_IP", "stat_K",  "stat_L", "stat_ER","stat_H",
    "stat_BB", "stat_SV", "stat_QS","stat_CG","stat_hld_po",
]


def _derive_league(team_name: str) -> str:
    """Derive 'Topps' or 'Rawlings' from team name suffix: '(T)' or '(R)'."""
    t = team_name.strip()
    if t.endswith("(T)"):
        return "Topps"
    if t.endswith("(R)"):
        return "Rawlings"
    return ""


def parse_standings_sections(csv_path: Path) -> dict:
    """
    Parse the multi-section standings CSV into a dict of key → (headers, rows).
    Only the 5 sections named in STANDINGS_SECTIONS are returned.
    Section header rows have a single non-empty cell matching a known section name.
    """
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        all_rows = list(reader)

    sections: dict = {}
    i = 0
    while i < len(all_rows):
        row = all_rows[i]
        if not row:
            i += 1
            continue
        first = row[0].strip()
        rest_empty = all(c.strip() == "" for c in row[1:])
        if first in STANDINGS_SECTIONS and rest_empty:
            key = STANDINGS_SECTIONS[first]
            i += 1
            if i >= len(all_rows):
                break
            # Next row is column headers — strip trailing empties
            raw_headers = all_rows[i]
            headers = [h.strip() for h in raw_headers]
            while headers and headers[-1] == "":
                headers.pop()
            i += 1
            data_rows: list[list[str]] = []
            while i < len(all_rows):
                drow = all_rows[i]
                # Blank row → end of section
                if not drow or all(c.strip() == "" for c in drow):
                    break
                # Skip the section's own header row if it somehow re-appears
                if drow[0].strip() in STANDINGS_SECTIONS:
                    break
                data_rows.append([c.strip() for c in drow[:len(headers)]])
                i += 1
            sections[key] = (headers, data_rows)
            log(f"   Section '{first}': {len(headers)} cols, {len(data_rows)} rows")
        else:
            i += 1
    return sections


def build_standings_batch_rows(sections: dict) -> tuple:
    """
    Merge parsed sections into the 3 output datasets.
    Returns: (standings_rows, hit_rows, pit_rows)
    Each is a list of lists matching the corresponding HEADERS above.

    Source column layout:
      overall:   [Rk, Team, FPts, +/-, FP/G, GP, Hit, Pit, PBL]   → indices 0-8
      pts_hit:   [Rk, Team, FPts, +/-, R, 1B, 2B, 3B, HR, RBI, BB, SO, SB, GIDP]
      pts_pit:   [Rk, Team, FPts, +/-, IP, K, L, ER, H, BB, SV, QS, CG, HLD+PO]
      stats_hit: [Rk, Team, FPts, GP,  R, 1B, 2B, 3B, HR, RBI, BB, SO, SB, GIDP]
      stats_pit: [Rk, Team, FPts, GP,  IP, K, L, ER, H, BB, SV, QS, CG, HLD+PO]
    Note: stats GP (index 3) is blank in the Fantrax export — stored as empty → NULL.
    """
    def _get(row: list, idx: int) -> str:
        return row[idx] if idx < len(row) else ""

    # ── Fantrax_Standings ──────────────────────────────────────────────────────
    _, overall_rows = sections.get("overall", ([], []))
    standings_rows = []
    for r in overall_rows:
        team = _get(r, 1)
        standings_rows.append([
            _get(r, 0),              # Rk
            team,                    # Team
            _derive_league(team),    # League  (derived from (T)/(R) suffix)
            _get(r, 2),              # FPts
            _get(r, 4),              # fp_per_g  (FP/G at idx 4; +/- skipped at 3)
            _get(r, 5),              # GP
            _get(r, 6),              # Hit_pts
            _get(r, 7),              # Pit_pts
            _get(r, 8),              # PBL_pts
        ])

    # ── Fantrax_Standings_Hit ─────────────────────────────────────────────────
    _, pts_hit_rows   = sections.get("pts_hit",   ([], []))
    _, stats_hit_rows = sections.get("stats_hit", ([], []))
    stats_hit_by_team = {r[1]: r for r in stats_hit_rows if len(r) > 1}

    hit_rows = []
    for pr in pts_hit_rows:
        if len(pr) < 2:
            continue
        team = _get(pr, 1)
        sr   = stats_hit_by_team.get(team, [])
        hit_rows.append([
            team,                    # Team
            _derive_league(team),    # League
            _get(pr, 0),             # Rk
            _get(pr, 2),             # FPts (from pts section)
            _get(sr, 3),             # GP   (from stats section; blank → NULL)
            # points cols (idx 4-13; +/- at 3 skipped)
            _get(pr, 4),  _get(pr, 5),  _get(pr, 6),  _get(pr, 7),  _get(pr, 8),
            _get(pr, 9),  _get(pr, 10), _get(pr, 11), _get(pr, 12), _get(pr, 13),
            # stat cols (idx 4-13; same positional order)
            _get(sr, 4),  _get(sr, 5),  _get(sr, 6),  _get(sr, 7),  _get(sr, 8),
            _get(sr, 9),  _get(sr, 10), _get(sr, 11), _get(sr, 12), _get(sr, 13),
        ])

    # ── Fantrax_Standings_Pit ─────────────────────────────────────────────────
    _, pts_pit_rows   = sections.get("pts_pit",   ([], []))
    _, stats_pit_rows = sections.get("stats_pit", ([], []))
    stats_pit_by_team = {r[1]: r for r in stats_pit_rows if len(r) > 1}

    pit_rows = []
    for pr in pts_pit_rows:
        if len(pr) < 2:
            continue
        team = _get(pr, 1)
        sr   = stats_pit_by_team.get(team, [])
        pit_rows.append([
            team,                    # Team
            _derive_league(team),    # League
            _get(pr, 0),             # Rk
            _get(pr, 2),             # FPts (from pts section)
            _get(sr, 3),             # GP   (blank → NULL)
            # points cols (idx 4-13)
            _get(pr, 4),  _get(pr, 5),  _get(pr, 6),  _get(pr, 7),  _get(pr, 8),
            _get(pr, 9),  _get(pr, 10), _get(pr, 11), _get(pr, 12), _get(pr, 13),
            # stat cols (idx 4-13)
            _get(sr, 4),  _get(sr, 5),  _get(sr, 6),  _get(sr, 7),  _get(sr, 8),
            _get(sr, 9),  _get(sr, 10), _get(sr, 11), _get(sr, 12), _get(sr, 13),
        ])

    return standings_rows, hit_rows, pit_rows


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
    # Comma-formatted decimal (e.g. Fantrax standings '3,356.167' → 3356.167)
    try:
        float(no_commas)
        return no_commas
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
    single_csvs = [k for k in CSV_TABLE_MAP if k not in HOD_DRAFTS_CSVS and k not in TH_CSVS]
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

    # ── Process Transaction History (merge Topps + Rawlings) ─────────────────
    log(f"\n── Fantrax_Transaction_History (Topps + Rawlings combined) {'─' * 3}")
    th_rows: list[list[str]] = []
    th_headers: list[str] = []
    th_ok = True
    for stem in TH_CSVS:
        csv_path = SOURCES_DIR / f"{stem}.csv"
        if not csv_path.exists():
            log(f"  ✗  Missing: {csv_path.name}")
            th_ok = False
            continue
        headers, rows = read_csv_rows(csv_path)
        if not th_headers:
            th_headers = headers
        th_rows.extend(rows)
        log(f"   {stem}: {len(rows)} rows")

    if th_ok and th_rows:
        written = write_batch_files("Fantrax_Transaction_History", th_headers, th_rows)
        log(f"   ✓  {written} combined rows → {BATCHES_DIR.name}/Fantrax_Transaction_History_*.sql")
        results.append(("Fantrax_Transaction_History", True, written))
    else:
        results.append(("Fantrax_Transaction_History", False, 0))

    # ── Process Standings (multi-section CSV → 3 tables) ──────────────────────
    log(f"\n── Fantrax_Standings (multi-section → 3 tables) {'─' * 5}")
    standings_csv = SOURCES_DIR / f"{STANDINGS_CSV}.csv"
    if not standings_csv.exists():
        log(f"  ✗  Missing: {standings_csv.name}")
        results.append(("Fantrax_Standings*", False, 0))
    else:
        sections = parse_standings_sections(standings_csv)
        missing = [k for k in STANDINGS_SECTIONS.values() if k not in sections]
        if missing:
            log(f"  ✗  Missing sections in CSV: {missing}")
            results.append(("Fantrax_Standings*", False, 0))
        else:
            s_rows, h_rows, p_rows = build_standings_batch_rows(sections)

            for tbl, hdrs, rows in [
                ("Fantrax_Standings",     STANDINGS_HEADERS,     s_rows),
                ("Fantrax_Standings_Hit", STANDINGS_HIT_HEADERS, h_rows),
                ("Fantrax_Standings_Pit", STANDINGS_PIT_HEADERS, p_rows),
            ]:
                written = write_batch_files(tbl, hdrs, rows)
                log(f"   ✓  {written} rows → {BATCHES_DIR.name}/{tbl}_*.sql")
                results.append((tbl, True, written))

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
