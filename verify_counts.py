"""
verify_counts.py
Compares row counts between local PostgreSQL and Supabase.
"""

from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

# ── Local PostgreSQL ─────────────────────────────────────────────────────────
local_engine = create_engine(
    "postgresql+psycopg2://postgres:Blurbinpostgres7&@localhost:5432/fantrax"
)

# ── Supabase ─────────────────────────────────────────────────────────────────
sb_password = quote_plus("yHq5gcTHDkNKbFXJ")
supabase_engine = create_engine(
    f"postgresql+psycopg2://postgres.rlwidfirrdwolaywjpca:{sb_password}"
    f"@aws-1-us-east-2.pooler.supabase.com:5432/postgres",
    connect_args={"sslmode": "require", "gssencmode": "disable"}
)

tables = [
    'Fantrax_Transaction_History',
    'Fantrax_HOD_Drafts',
    'Fantrax_Players_Hitters_Rawlings',
    'Fantrax_Players_Hitters_Topps',
    'Fantrax_Players_Pitchers_Rawlings',
    'Fantrax_Players_Pitchers_Topps',
]

print(f"{'Table':<45} {'Local':>8} {'Supabase':>10} {'Match':>7}")
print("-" * 75)

with local_engine.connect() as lconn, supabase_engine.connect() as sconn:
    for t in tables:
        q = text(f'SELECT COUNT(*) FROM fantrax."{t}"')
        local_n    = lconn.execute(q).scalar()
        supabase_n = sconn.execute(q).scalar()
        match = "✓" if local_n == supabase_n else "✗ MISMATCH"
        print(f"  {t:<43} {local_n:>8} {supabase_n:>10} {match:>7}")
