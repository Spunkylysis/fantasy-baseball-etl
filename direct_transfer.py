"""
direct_transfer.py
Transfers all Fantrax tables from local PostgreSQL to Supabase.

Tables handled:
  - Fantrax_Transaction_History  (with cap hit calculation)
  - Fantrax_Players_Hitters_Rawlings
  - Fantrax_Players_Hitters_Topps
  - Fantrax_Players_Pitchers_Rawlings
  - Fantrax_Players_Pitchers_Topps

Run from Anaconda Prompt (avoids Spyder .pyc caching):
  python "C:/Users/James/OneDrive/Documents/Claude/Projects/Fantasy Baseball (1)/direct_transfer.py"
"""

import datetime
import psycopg2
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

# ── Connections ──────────────────────────────────────────────────────────────

local_engine = create_engine(
    "postgresql+psycopg2://postgres:yHq5gcTHDkNKbFXJ@localhost:5432/fantrax"
)

sb_password = quote_plus("yHq5gcTHDkNKbFXJ")
sb_conn = psycopg2.connect(
    host="aws-1-us-east-2.pooler.supabase.com",
    port=5432,
    dbname="postgres",
    user="postgres.rlwidfirrdwolaywjpca",
    password="Blurbinsupabase7&",
    sslmode="require",
    gssencmode="disable"
)
sb_cur = sb_conn.cursor()

sb_engine = create_engine(
    f"postgresql+psycopg2://postgres.rlwidfirrdwolaywjpca:{sb_password}"
    f"@aws-1-us-east-2.pooler.supabase.com:5432/postgres",
    connect_args={"sslmode": "require", "gssencmode": "disable"}
)

CHUNK = 500
DROP_LOCAL_COLS = {"+/-", "plus_minus"}   # local player-table cols to exclude

PLAYER_TABLES = [
    "Fantrax_Players_Hitters_Rawlings",
    "Fantrax_Players_Hitters_Topps",
    "Fantrax_Players_Pitchers_Rawlings",
    "Fantrax_Players_Pitchers_Topps",
]


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — Transaction History with cap hit
# ─────────────────────────────────────────────────────────────────────────────

print("── Transaction History ──────────────────────────────────────────────────")

with local_engine.connect() as src:
    # Discover what columns local TH actually has
    res = src.execute(text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'fantrax'
          AND table_name   = 'Fantrax_Transaction_History'
        ORDER BY ordinal_position
    """))
    local_th_cols = [r[0] for r in res]
    print(f"  Local TH columns ({len(local_th_cols)}): {local_th_cols}")

    # Fetch all TH rows from local
    col_select = ", ".join(f'"{c}"' for c in local_th_cols)
    th_rows = src.execute(
        text(f'SELECT {col_select} FROM fantrax."Fantrax_Transaction_History"')
    ).fetchall()
    print(f"  Local rows: {len(th_rows)}")

    # ── Local row counts diagnostic ──────────────────────────────────────
    print("\n  Local row counts:")
    for t in PLAYER_TABLES + ["Fantrax_HOD_Drafts"]:
        n = src.execute(text(f'SELECT COUNT(*) FROM fantrax."{t}"')).scalar()
        print(f"    {t}: {n}")

    # ── Salary lookup from local player tables ───────────────────────────
    # Salary lives in the player tables (Rawlings/Topps Master), not HOD_Drafts.
    # key = (player_name, league) → (salary, player_id, status/owner)
    salary_map = {}
    for pt in PLAYER_TABLES:
        # Detect exact column names in this table
        col_res = src.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'fantrax' AND table_name = :t
        """), {"t": pt})
        pt_cols = {r[0] for r in col_res}

        # Need at least Player + Salary columns
        if "Player" not in pt_cols or "Salary" not in pt_cols:
            print(f"  Warning: {pt} missing Player/Salary — skipping salary lookup")
            continue

        # Infer league from table name
        league = "Rawlings" if "Rawlings" in pt else "Topps"

        # Player ID column might be "ID" in player tables
        id_col = next((c for c in ("ID", "Player ID", "player_id") if c in pt_cols), None)

        # Status column holds current fantasy owner abbreviation
        status_col = "Status" if "Status" in pt_cols else None

        if id_col:
            sel = f'"Player", "Salary", "{id_col}"'
            if status_col:
                sel += f', "{status_col}"'
            rows_sal = src.execute(text(
                f'SELECT {sel} FROM fantrax."{pt}" WHERE "Salary" IS NOT NULL'
            )).fetchall()
            for row in rows_sal:
                player = row[0]
                salary = row[1]
                pid    = row[2]
                owner  = row[3] if status_col else None
                key = (player, league)
                if key not in salary_map:
                    salary_map[key] = (salary, pid, owner)

    print(f"\n  Salary map built: {len(salary_map)} entries")

# Map any local column name variant to the Supabase canonical name
LOCAL_TO_SB = {
    "Player":         "Player",
    "Team":           "Team",
    "Position":       "Position",
    "Type":           "Type",
    "Owner":          "Owner",
    "Bid":            "Bid",
    "Date (CDT)":     "date_cdt",
    "date_cdt":       "date_cdt",
    "Period":         "Period",
    "League":         "League",
    "Players.ID":     "players_id",
    "players_id":     "players_id",
    "Key":            "Key",
    "key":            "Key",
    "Drafted Team":   "drafted_team",
    "drafted_team":   "drafted_team",
    "Owner.1":        "owner_1",
    "owner_1":        "owner_1",
    "Cap Hit %":      "cap_hit_pct",
    "cap_hit_pct":    "cap_hit_pct",
    "Cap Hit":        "cap_hit",
    "cap_hit":        "cap_hit",
    "DateNumber":     "date_number",
    "date_number":    "date_number",
    "TableKey":       "table_key",
    "table_key":      "table_key",
}

# Supabase TH column order (17 cols after ALTER TABLE in this session)
SB_TH_COLS = [
    "Player", "Team", "Position", "Type", "Owner", "Bid", "date_cdt", "Period",
    "League", "players_id", "Key", "drafted_team", "owner_1",
    "cap_hit_pct", "cap_hit", "date_number", "table_key",
]


def excel_date_number(dt):
    """Convert Python datetime → Excel serial date string (integer days since 1899-12-30)."""
    if dt is None:
        return None
    base = datetime.date(1899, 12, 30)
    d = dt.date() if hasattr(dt, "date") else dt
    return str((d - base).days)


def build_th_row(local_row):
    """Return a 17-value tuple aligned to SB_TH_COLS, with cap hit computed."""
    # Name → value from local columns
    vals = dict(zip(local_th_cols, local_row))

    # Translate to Supabase names
    norm = {}
    for lk, lv in vals.items():
        sb_key = LOCAL_TO_SB.get(lk)
        if sb_key:
            norm[sb_key] = lv

    player_name = norm.get("Player")
    league      = norm.get("League")

    # Derive players_id / drafted_team from HOD_Drafts if not already present
    players_id   = norm.get("players_id")
    drafted_team = norm.get("drafted_team")
    salary_val   = None

    if player_name and league:
        lookup = salary_map.get((player_name, league))
        if lookup:
            sal, pid, owner = lookup
            salary_val = sal
            if not players_id:
                players_id = pid
            # drafted_team stays None if not in local TH cols —
            # we don't overwrite it with the current owner abbreviation

    norm["players_id"]   = players_id
    norm["drafted_team"] = drafted_team

    # Key = players_id + League (e.g. "*05wru*Topps")
    if not norm.get("Key") and players_id and league:
        norm["Key"] = f"{players_id}{league}"

    # DateNumber = Excel serial date from date_cdt
    date_cdt = norm.get("date_cdt")
    dn = norm.get("date_number")
    if not dn and date_cdt:
        dn = excel_date_number(date_cdt)
    norm["date_number"] = dn

    # Cap hit calculation
    cap_hit_pct = norm.get("cap_hit_pct")
    cap_hit     = norm.get("cap_hit")
    if cap_hit is None or cap_hit_pct is None:
        cap_hit_pct = -0.5
        if salary_val:
            cap_hit = int(round(salary_val * cap_hit_pct))
        norm["cap_hit_pct"] = cap_hit_pct
        norm["cap_hit"]     = cap_hit

    # TableKey = Key + DateNumber (e.g. "*05wru*Topps46049")
    if not norm.get("table_key"):
        k  = norm.get("Key")
        dn = norm.get("date_number")
        if k and dn:
            norm["table_key"] = f"{k}{dn}"

    return tuple(norm.get(c) for c in SB_TH_COLS)


# Truncate then reload
sb_cur.execute('TRUNCATE TABLE fantrax."Fantrax_Transaction_History"')
sb_conn.commit()

th_clean = [build_th_row(r) for r in th_rows]

col_insert  = ", ".join(f'"{c}"' for c in SB_TH_COLS)
insert_sql  = f'INSERT INTO fantrax."Fantrax_Transaction_History" ({col_insert}) VALUES %s'

inserted = 0
for i in range(0, len(th_clean), CHUNK):
    execute_values(sb_cur, insert_sql, th_clean[i:i + CHUNK])
    sb_conn.commit()
    inserted += len(th_clean[i:i + CHUNK])

# Quick sanity: how many rows got a cap_hit value?
cap_hit_filled = sum(1 for r in th_clean if r[SB_TH_COLS.index("cap_hit")] is not None)
print(f"  ✓  Fantrax_Transaction_History  {inserted:>5} rows  "
      f"({cap_hit_filled} with cap_hit, {inserted - cap_hit_filled} NULL)")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — Player tables (positional drop of +/-)
# ─────────────────────────────────────────────────────────────────────────────

print("\n── Player Tables ────────────────────────────────────────────────────────")

with local_engine.connect() as src:
    for table in PLAYER_TABLES:
        fq = f'fantrax."{table}"'

        # Local column names
        res = src.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'fantrax' AND table_name = :t
            ORDER BY ordinal_position
        """), {"t": table})
        local_cols = [r[0] for r in res]

        # Supabase column names
        sb_cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'fantrax' AND table_name = %s
            ORDER BY ordinal_position
        """, (table,))
        sb_cols = [r[0] for r in sb_cur.fetchall()]

        keep_idx = [i for i, c in enumerate(local_cols) if c not in DROP_LOCAL_COLS]

        if len(keep_idx) != len(sb_cols):
            print(f"  ✗  {table}: column mismatch after drop "
                  f"(local_kept={len(keep_idx)}, supabase={len(sb_cols)})")
            for j in range(min(len(keep_idx), len(sb_cols))):
                lc = local_cols[keep_idx[j]] if j < len(keep_idx) else "—"
                sc = sb_cols[j]             if j < len(sb_cols)  else "—"
                flag = "" if lc == sc or True else " ← mismatch"
                print(f"      [{j:02d}] local={lc:<25} sb={sc}{flag}")
            continue

        # Fetch all rows
        col_select = ", ".join(f'"{c}"' for c in local_cols)
        rows = src.execute(text(f"SELECT {col_select} FROM {fq}")).fetchall()
        total = len(rows)

        # Strip dropped columns
        rows_clean = [tuple(r[i] for i in keep_idx) for r in rows]

        # Truncate then reload
        sb_cur.execute(f'TRUNCATE TABLE {fq}')
        sb_conn.commit()

        col_insert = ", ".join(f'"{c}"' for c in sb_cols)
        insert_sql = f'INSERT INTO {fq} ({col_insert}) VALUES %s'

        inserted = 0
        for i in range(0, total, CHUNK):
            execute_values(sb_cur, insert_sql, rows_clean[i:i + CHUNK])
            sb_conn.commit()
            inserted += len(rows_clean[i:i + CHUNK])

        print(f"  ✓  {table:<45}  {inserted:>5} / {total} rows")

sb_cur.close()
sb_conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — Verify row counts
# ─────────────────────────────────────────────────────────────────────────────

print("\n── Row counts ──────────────────────────────────────────────────────────")
print(f"  {'Table':<45} {'Local':>8} {'Supabase':>10} {'Match':>9}")
print("  " + "-" * 77)

ALL_TABLES = [
    "Fantrax_Transaction_History",
    "Fantrax_HOD_Drafts",
    "Fantrax_Players_Hitters_Rawlings",
    "Fantrax_Players_Hitters_Topps",
    "Fantrax_Players_Pitchers_Rawlings",
    "Fantrax_Players_Pitchers_Topps",
]

with local_engine.connect() as lc, sb_engine.connect() as sc:
    for t in ALL_TABLES:
        q = text(f'SELECT COUNT(*) FROM fantrax."{t}"')
        ln = lc.execute(q).scalar()
        sn = sc.execute(q).scalar()
        match = "✓" if ln == sn else "✗ MISMATCH"
        print(f"  {t:<45} {ln:>8} {sn:>10} {match:>9}")

print("\nDone.")
