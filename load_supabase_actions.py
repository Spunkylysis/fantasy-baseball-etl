"""
load_supabase_actions.py
Phase 3: GitHub Actions ETL — loads Fantrax batch SQL files directly into Supabase.
Runs on ubuntu-latest; no local PostgreSQL dependency.

Required GitHub Secret:
    SUPABASE_PASSWORD   — Supabase database password

Run locally (PowerShell):
    $env:SUPABASE_PASSWORD = "yourpassword"
    python load_supabase_actions.py

Batch files expected in ./batches/ relative to this script:
    Fantrax_Players_Hitters_Rawlings_*.sql
    Fantrax_Players_Hitters_Topps_*.sql
    Fantrax_Players_Pitchers_Rawlings_*.sql
    Fantrax_Players_Pitchers_Topps_*.sql
    Fantrax_HOD_Drafts_*.sql
    Fantrax_Transaction_History_000.sql
"""

import datetime
import os
import re
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

# ── Config ─────────────────────────────────────────────────────────────────────

SUPABASE_HOST = "aws-1-us-east-2.pooler.supabase.com"
SUPABASE_USER = "postgres.rlwidfirrdwolaywjpca"
SUPABASE_DB   = "postgres"
SUPABASE_PORT = 5432

BATCH_DIR = Path(__file__).parent / "batches"
LOG_FILE  = Path(__file__).parent / "etl_log.txt"

CHUNK   = 500
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# ── Column definitions ─────────────────────────────────────────────────────────
# Batch files use positional INSERT (no column names).
# Batch position 13 (0-indexed) holds +/- which is absent in Supabase — drop it.

HITTERS_SB_COLS = [
    "ID", "Player", "Team", "Position", "RkOv", "Status", "Age", "Opponent",
    "Salary", "Contract", "FPts", "fp_per_g", "Ros",
    # pos 13 (+/-) dropped
    "AB", "R", "H", "singles", "doubles", "triples", "HR", "RBI", "BB", "SO",
    "SB", "GIDP", "GP",
]  # 26 columns

PITCHERS_SB_COLS = [
    "ID", "Player", "Team", "Position", "RkOv", "Status", "Age", "Opponent",
    "Salary", "Contract", "FPts", "fp_per_g", "Ros",
    # pos 13 (+/-) dropped
    "IP", "ERA", "K", "L", "ER", "H", "BB", "SV", "QS", "CG", "hld_po", "GP",
]  # 25 columns

HOD_DRAFTS_SB_COLS = [
    "player_id", "Round", "Pick", "ov_pick", "Pos", "Player", "Team",
    "fantasy_team", "date_cdt", "League",
]  # 10 columns

# 17-column Supabase Transaction History (full schema)
SB_TH_COLS = [
    "Player", "Team", "Position", "Type", "Owner", "Bid", "date_cdt", "Period",
    "League", "players_id", "Key", "drafted_team", "owner_1",
    "cap_hit_pct", "cap_hit", "date_number", "table_key",
]

# Player tables in load order (salary map is built while parsing these)
PLAYER_TABLE_CONFIG = {
    "Fantrax_Players_Hitters_Rawlings":  (HITTERS_SB_COLS,  {13}),
    "Fantrax_Players_Hitters_Topps":     (HITTERS_SB_COLS,  {13}),
    "Fantrax_Players_Pitchers_Rawlings": (PITCHERS_SB_COLS, {13}),
    "Fantrax_Players_Pitchers_Topps":    (PITCHERS_SB_COLS, {13}),
    "Fantrax_HOD_Drafts":                (HOD_DRAFTS_SB_COLS, set()),
    # Standings: batch files embed column names (same path as HOD_Drafts)
    "Fantrax_Standings":                 ([], set()),
    "Fantrax_Standings_Hit":             ([], set()),
    "Fantrax_Standings_Pit":             ([], set()),
}

# ── Logging ────────────────────────────────────────────────────────────────────

_log_lines: list[str] = []

def log(msg: str = "") -> None:
    print(msg, flush=True)
    _log_lines.append(msg)

def flush_log() -> None:
    LOG_FILE.write_text("\n".join(_log_lines), encoding="utf-8")

# ── SQL row parser ─────────────────────────────────────────────────────────────

def _parse_row_values(row_str: str) -> list:
    """
    Parse the content between outer parens of one VALUES row into Python values.
    Handles: NULL, 'quoted strings' ('' escaping), integers, floats.
    Special case: '100%' / '0.94%' → float (Ros column quirk in batch files).
    """
    vals = []
    i, n = 0, len(row_str)

    while i < n:
        while i < n and row_str[i] in " \t\n\r":
            i += 1
        if i >= n:
            break

        ch = row_str[i]

        if ch == ",":
            i += 1
            continue

        # NULL
        peek4 = row_str[i:i+4].upper()
        if peek4 == "NULL" and (i + 4 >= n or row_str[i + 4] in ", \t\n\r)"):
            vals.append(None)
            i += 4
            continue

        # Quoted string
        if ch == "'":
            j = i + 1
            chars: list[str] = []
            while j < n:
                if row_str[j] == "'" and j + 1 < n and row_str[j + 1] == "'":
                    chars.append("'")
                    j += 2
                elif row_str[j] == "'":
                    j += 1
                    break
                else:
                    chars.append(row_str[j])
                    j += 1
            raw = "".join(chars)
            # '100%' or '0.94%' → numeric (Ros column in Topps/Pitchers batch files)
            if re.match(r"^\d+(?:\.\d+)?%$", raw):
                vals.append(float(raw[:-1]))
            else:
                vals.append(raw)
            i = j
            continue

        # Number (int or float, possibly negative)
        if ch in "-0123456789.":
            j = i
            while j < n and row_str[j] in "-0123456789.eE+":
                j += 1
            num_str = row_str[i:j]
            try:
                vals.append(
                    int(num_str)
                    if "." not in num_str and "e" not in num_str.lower()
                    else float(num_str)
                )
            except ValueError:
                vals.append(num_str)
            i = j
            continue

        # Fallback: read until comma or closing paren
        j = i
        while j < n and row_str[j] not in ",)":
            j += 1
        vals.append(row_str[i:j].strip())
        i = j

    return vals


def parse_batch_file(filepath: Path) -> list[list]:
    """
    Read a batch SQL file and return a list of row value lists.
    Expects one INSERT ... VALUES (...); per line (standard batch format).
    For each line containing VALUES, extracts the content between the
    outermost parentheses using a greedy regex on the trimmed line.
    """
    rows: list[list] = []
    for raw_line in filepath.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = re.search(r"VALUES\s*\((.+)\)\s*;?\s*$", line, re.IGNORECASE)
        if m:
            rows.append(_parse_row_values(m.group(1)))
    return rows


def parse_batch_file_with_cols(filepath: Path) -> tuple[list[str], list[list]]:
    """
    Read a batch SQL file, extracting both column names and row values.
    Column names are parsed from the first INSERT statement's column list:
        INSERT INTO schema."table" ("col1", "col2", ...) VALUES (...);
    Old positional-INSERT files (no column list) return cols=[].
    """
    cols: list[str] = []
    rows: list[list] = []
    for raw_line in filepath.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Capture column list from first INSERT that has one
        if not cols:
            col_m = re.search(
                r'INSERT\s+INTO\s+\S+\s*\(([^)]+)\)\s+VALUES',
                line, re.IGNORECASE
            )
            if col_m:
                cols = [c.strip().strip('"') for c in col_m.group(1).split(',')]
        m = re.search(r"VALUES\s*\((.+)\)\s*;?\s*$", line, re.IGNORECASE)
        if m:
            rows.append(_parse_row_values(m.group(1)))
    return cols, rows

# ── Date helpers ───────────────────────────────────────────────────────────────

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
)

def _parse_date(val) -> datetime.datetime | None:
    if val is None:
        return None
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val if isinstance(val, datetime.datetime) else datetime.datetime.combine(val, datetime.time())
    s = str(val).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# Period 1 = 2026-03-25 (inferred from transactions: Period 38 = 2026-05-01, daily cadence).
# Update SEASON_START each new season.
SEASON_START = datetime.datetime(2026, 3, 25)

def _period_to_date(period) -> datetime.datetime | None:
    """Estimate a period's start date from its number (daily cadence)."""
    try:
        return SEASON_START + datetime.timedelta(days=int(period) - 1)
    except (TypeError, ValueError):
        return None


def _excel_serial(dt: datetime.datetime | None) -> str | None:
    """Convert a datetime to an Excel serial date string (days since 1899-12-30)."""
    if dt is None:
        return None
    base = datetime.date(1899, 12, 30)
    return str((dt.date() - base).days)

# ── Batch file discovery ───────────────────────────────────────────────────────

def get_batch_files(table_name: str) -> list[Path]:
    """Return sorted list of batch .sql files matching the table name exactly.

    Uses a 3-digit index glob (e.g. Fantrax_Standings_000.sql) so that tables
    whose names share a prefix — e.g. Fantrax_Standings vs Fantrax_Standings_Hit
    — do not accidentally pick up each other's files.
    """
    files = sorted(BATCH_DIR.glob(f"{table_name}_[0-9][0-9][0-9].sql"))
    return files

# ── Main ETL ───────────────────────────────────────────────────────────────────

def main() -> int:
    password = os.environ.get("SUPABASE_PASSWORD", "")
    if not password:
        log("ERROR: SUPABASE_PASSWORD environment variable is not set.")
        flush_log()
        return 1

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
    log(f"{'='*70}")
    log(f"  Fantrax ETL  —  {ts}{'  [DRY RUN]' if DRY_RUN else ''}")
    log(f"{'='*70}")

    conn = psycopg2.connect(
        host=SUPABASE_HOST,
        port=SUPABASE_PORT,
        dbname=SUPABASE_DB,
        user=SUPABASE_USER,
        password=password,
        sslmode="require",
        gssencmode="disable",
    )
    cur = conn.cursor()

    # salary_map: player_name → (salary, player_id, league)
    # Built while parsing player batch files; used when processing TH.
    salary_map: dict[str, tuple] = {}

    # ── Player tables + HOD_Drafts ─────────────────────────────────────────────
    for table_name, (sb_cols, drop_idx) in PLAYER_TABLE_CONFIG.items():
        log(f"\n── {table_name} {'─'*(55 - len(table_name))}")

        files = get_batch_files(table_name)
        if not files:
            log(f"  ✗  No batch files found for {table_name} — skipping")
            continue

        # ── HOD_Drafts + Standings: read column names from the batch file itself ──
        # These tables' batch files embed the column list in the INSERT statement
        # (written by csv_to_batches.py), so we parse it dynamically.
        if table_name in ("Fantrax_HOD_Drafts",
                          "Fantrax_Standings",
                          "Fantrax_Standings_Hit",
                          "Fantrax_Standings_Pit"):
            batch_cols, first_rows = parse_batch_file_with_cols(files[0])
            log(f"  Parsed  {files[0].name}  →  {len(first_rows)} rows")
            all_rows: list[list] = first_rows
            for fpath in files[1:]:
                extra = parse_batch_file(fpath)
                all_rows.extend(extra)
                log(f"  Parsed  {fpath.name}  →  {len(extra)} rows")

            if not all_rows:
                log(f"  ✗  No rows parsed — skipping")
                continue

            # Use cols from batch file if present, else fall back to hardcoded
            effective_cols = batch_cols if batch_cols else sb_cols
            log(f"  Columns ({len(effective_cols)}): {effective_cols}")

            if DRY_RUN:
                log(f"  DRY RUN — would TRUNCATE and INSERT {len(all_rows)} rows")
                continue

            cur.execute(f'TRUNCATE TABLE fantrax."{table_name}"')
            conn.commit()

            col_list   = ", ".join(f'"{c}"' for c in effective_cols)
            insert_sql = f'INSERT INTO fantrax."{table_name}" ({col_list}) VALUES %s'

            inserted = 0
            for chunk_start in range(0, len(all_rows), CHUNK):
                chunk = [tuple(r) for r in all_rows[chunk_start:chunk_start + CHUNK]]
                execute_values(cur, insert_sql, chunk)
                conn.commit()
                inserted += len(chunk)

            log(f"  ✓  {inserted} rows inserted")
            continue

        # ── Player tables (Hitters / Pitchers) ───────────────────────────────
        all_rows = []
        for fpath in files:
            rows = parse_batch_file(fpath)
            all_rows.extend(rows)
            log(f"  Parsed  {fpath.name}  →  {len(rows)} rows")

        if not all_rows:
            log(f"  ✗  No rows parsed — skipping")
            continue

        # Validate column count
        expected_raw = len(sb_cols) + len(drop_idx)
        bad_rows = [i for i, r in enumerate(all_rows) if len(r) != expected_raw]
        if bad_rows:
            log(f"  WARNING: {len(bad_rows)} rows have unexpected column count "
                f"(expected {expected_raw}). First offender: row {bad_rows[0]}, "
                f"len={len(all_rows[bad_rows[0]])}")

        # Drop +/- column (and any other drop_idx positions)
        if drop_idx:
            clean_rows = [
                [v for i, v in enumerate(row) if i not in drop_idx]
                for row in all_rows
            ]
        else:
            clean_rows = all_rows

        # Accumulate salary map from Hitter/Pitcher tables.
        # Keyed by (player_name, league) so Topps and Rawlings salaries are
        # stored independently — a player's salary can differ across leagues.
        league = "Rawlings" if "Rawlings" in table_name else "Topps"
        pid_idx    = sb_cols.index("ID")
        player_idx = sb_cols.index("Player")
        salary_idx = sb_cols.index("Salary")
        status_idx = sb_cols.index("Status")
        for row in clean_rows:
            if len(row) > salary_idx:
                pname  = row[player_idx]
                sal    = row[salary_idx]
                pid    = row[pid_idx]
                status = row[status_idx] if len(row) > status_idx else None
                if pname and sal is not None:
                    # Always overwrite so the most-recently-loaded file wins
                    # (both leagues are loaded once per ETL run)
                    salary_map[(pname, league)] = (sal, pid, status)

        if DRY_RUN:
            log(f"  DRY RUN — would TRUNCATE and INSERT {len(clean_rows)} rows")
            continue

        cur.execute(f'TRUNCATE TABLE fantrax."{table_name}"')
        conn.commit()

        col_list   = ", ".join(f'"{c}"' for c in sb_cols)
        insert_sql = f'INSERT INTO fantrax."{table_name}" ({col_list}) VALUES %s'

        inserted = 0
        for chunk_start in range(0, len(clean_rows), CHUNK):
            chunk = [tuple(r) for r in clean_rows[chunk_start:chunk_start + CHUNK]]
            execute_values(cur, insert_sql, chunk)
            conn.commit()
            inserted += len(chunk)

        log(f"  ✓  {inserted} rows inserted")

    log(f"\n  Salary map: {len(salary_map)} players available for cap hit lookup")

    # ── Transaction History ────────────────────────────────────────────────────
    log(f"\n── Fantrax_Transaction_History {'─'*39}")

    th_files = [BATCH_DIR / "Fantrax_Transaction_History_000.sql"]
    th_files = [f for f in th_files if f.exists()]

    if not th_files:
        log("  ✗  Fantrax_Transaction_History_000.sql not found — skipping TH")
    else:
        raw_th: list[list] = []
        for fpath in th_files:
            rows = parse_batch_file(fpath)
            raw_th.extend(rows)
            log(f"  Parsed  {fpath.name}  →  {len(rows)} rows")

        def _build_th_row(raw: list) -> tuple:
            """
            Convert an 8-value TH batch row into a full 17-column Supabase row.
            raw = [Player, Team, Position, Type, Owner, Bid, date_cdt_str, Period]
            """
            # Pad with None if truncated
            while len(raw) < 8:
                raw.append(None)

            player, team, position, type_, owner, bid, date_raw, period = raw[:8]

            # Parse date; fall back to period-derived date if Fantrax omitted it
            date_cdt = _parse_date(date_raw)
            if date_cdt is None:
                date_cdt = _period_to_date(period)

            # Derive league from owner name: "(T)" anywhere in name → Topps, else Rawlings.
            # Using "in" rather than endswith() handles names like 'Riddle me This" (T)"'
            # where Fantrax embeds stray quotes that corrupt the suffix.
            owner_str    = str(owner).strip() if owner else ""
            owner_league = "Topps" if "(T)" in owner_str else "Rawlings"

            # Salary / player ID / roster-status lookup (league-aware)
            info = salary_map.get((player, owner_league)) if player else None
            salary, pid, roster_status = info if info else (None, None, None)
            league = owner_league

            # Cap hit: Minor-league-rostered players carry 0% penalty (they are
            # development slots, not active roster commitments).  All others: -50%.
            if roster_status == "Minor":
                cap_hit_pct = 0.0
                cap_hit     = 0
            else:
                cap_hit_pct = -0.5
                cap_hit     = int(round(salary * cap_hit_pct)) if salary else None
            key         = f"{pid}{league}" if pid and league else None
            date_number = _excel_serial(date_cdt)
            table_key   = f"{key}{date_number}" if key and date_number else None

            # Return in SB_TH_COLS order
            return (
                player,       # Player
                team,         # Team
                position,     # Position
                type_,        # Type
                owner,        # Owner
                bid,          # Bid
                date_cdt,     # date_cdt
                period,       # Period
                league,       # League
                pid,          # players_id
                key,          # Key
                None,         # drafted_team  (not in batch file)
                None,         # owner_1       (not in batch file)
                cap_hit_pct,  # cap_hit_pct
                cap_hit,      # cap_hit
                date_number,  # date_number
                table_key,    # table_key
            )

        # Only load Drop transactions — Adds/Trades not needed for cap-hit analysis
        TH_TYPE_IDX = 3  # raw row: [Player, Team, Position, Type, Owner, Bid, date_cdt, Period]
        drops = [r for r in raw_th if len(r) > TH_TYPE_IDX and r[TH_TYPE_IDX] == 'Drop']
        log(f"  Filtered {len(raw_th)} rows → {len(drops)} Drop transactions")

        th_clean = [_build_th_row(r) for r in drops]

        cap_filled = sum(1 for r in th_clean if r[SB_TH_COLS.index("cap_hit")] is not None)
        log(f"  Built {len(th_clean)} TH rows  ({cap_filled} with cap_hit, "
            f"{len(th_clean) - cap_filled} NULL)")

        if not DRY_RUN:
            cur.execute('TRUNCATE TABLE fantrax."Fantrax_Transaction_History"')
            conn.commit()

            col_list   = ", ".join(f'"{c}"' for c in SB_TH_COLS)
            insert_sql = f'INSERT INTO fantrax."Fantrax_Transaction_History" ({col_list}) VALUES %s'

            inserted = 0
            for chunk_start in range(0, len(th_clean), CHUNK):
                chunk = th_clean[chunk_start:chunk_start + CHUNK]
                execute_values(cur, insert_sql, chunk)
                conn.commit()
                inserted += len(chunk)

            log(f"  ✓  {inserted} rows inserted")
        else:
            log(f"  DRY RUN — would TRUNCATE and INSERT {len(th_clean)} rows")

    # ── Row count verification ────────────────────────────────────────────
    log(f"\n── Row count verification ────────────────────────────────────────────")

    ALL_TABLES = [
        "Fantrax_Transaction_History",
        "Fantrax_HOD_Drafts",
        "Fantrax_Players_Hitters_Rawlings",
        "Fantrax_Players_Hitters_Topps",
        "Fantrax_Players_Pitchers_Rawlings",
        "Fantrax_Players_Pitchers_Topps",
        "Fantrax_Standings",
        "Fantrax_Standings_Hit",
        "Fantrax_Standings_Pit",
    ]
    # Exact expected counts for tables that are stable mid-season.
    EXPECTED = {
        "Fantrax_HOD_Drafts":                1540,
        "Fantrax_Players_Hitters_Rawlings":  3275,
        "Fantrax_Players_Hitters_Topps":     3275,
        "Fantrax_Players_Pitchers_Rawlings": 3667,
        "Fantrax_Players_Pitchers_Topps":    3667,
        "Fantrax_Standings":                 28,
        "Fantrax_Standings_Hit":             28,
        "Fantrax_Standings_Pit":             28,
    }
    # Minimum expected counts for tables that grow over time (e.g. transaction log).
    # TH only stores Drops, so count will be well below total transactions scraped.
    EXPECTED_MIN = {
        "Fantrax_Transaction_History": 50,
    }

    log(f"  {'Table':<45} {'Rows':>8}  {'Expected':>8}  {'Status':>7}")
    log("  " + "---------------------------------------------------------------------------")

    all_ok = True
    for t in ALL_TABLES:
        cur.execute(f'SELECT COUNT(*) FROM fantrax."{t}"')
        n = cur.fetchone()[0]
        if t in EXPECTED:
            exp_str = str(EXPECTED[t])
            ok = n == EXPECTED[t]
            status = '✓' if ok else '✗ MISMATCH'
        elif t in EXPECTED_MIN:
            exp_str = f'≥{EXPECTED_MIN[t]}'
            ok = n >= EXPECTED_MIN[t]
            status = '✓' if ok else '✗ TOO FEW'
        else:
            exp_str = '?'
            ok = True
            status = '?'
        if not ok:
            all_ok = False
        log(f"  {t:<45} {n:>8}  {exp_str:>8}  {status:>7}")

    cur.close()
    conn.close()

    log(f"\n======================================================================")
    if all_ok:
        log("  ETL completed successfully — all row counts match.")
    else:
        log("  ETL completed with ROW COUNT MISMATCHES — review output above.")
    log(f"======================================================================")

    flush_log()


if __name__ == "__main__":
    sys.exit(main())
