"""
diagnose_cols.py
Compares column names between local PostgreSQL and Supabase for the Hitters table.
"""
import psycopg2
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

local_engine = create_engine(
    "postgresql+psycopg2://postgres:Blurbinpostgres7&@localhost:5432/fantrax"
)

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

table = "Fantrax_Players_Hitters_Rawlings"

# Local columns
with local_engine.connect() as conn:
    res = conn.execute(text("""
        SELECT column_name, ordinal_position
        FROM information_schema.columns
        WHERE table_schema = 'fantrax' AND table_name = :t
        ORDER BY ordinal_position
    """), {"t": table})
    local_cols = [(r[1], r[0]) for r in res]

# Supabase columns
sb_cur.execute("""
    SELECT column_name, ordinal_position
    FROM information_schema.columns
    WHERE table_schema = 'fantrax' AND table_name = %s
    ORDER BY ordinal_position
""", (table,))
sb_cols = [(r[1], r[0]) for r in sb_cur.fetchall()]

print(f"{'Pos':<5} {'Local':<35} {'Supabase':<35} {'Match'}")
print("-" * 85)
for i in range(max(len(local_cols), len(sb_cols))):
    lpos, lcol = local_cols[i] if i < len(local_cols) else ("", "-- missing --")
    spos, scol = sb_cols[i]    if i < len(sb_cols)    else ("", "-- missing --")
    match = "✓" if lcol == scol else "✗"
    print(f"{lpos:<5} {lcol:<35} {scol:<35} {match}")

sb_cur.close()
sb_conn.close()
