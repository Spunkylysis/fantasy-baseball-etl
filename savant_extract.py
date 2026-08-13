"""
savant_extract.py
-----------------
Pulls Baseball Savant / Statcast data via pybaseball and writes a merged
CSV ready for load_savant_to_supabase.py.

Usage (local):
    python savant_extract.py

Usage (GitHub Actions):
    The YEAR env var overrides the default. If omitted, uses current calendar year.

Output:
    savant_metrics_{YEAR}.csv
"""

import os
import sys
import traceback
from datetime import datetime

import pandas as pd

try:
    from pybaseball import (
        statcast_batter_percentile_ranks,
        statcast_batter_expected_stats,
        statcast_sprint_speed,
        statcast_batter_exitvelo_barrels,
        statcast_pitcher_percentile_ranks,
        statcast_pitcher_expected_stats,
    )
    from pybaseball import cache
    cache.enable()
except ImportError:
    sys.exit("pybaseball not installed. Run: pip install pybaseball")

# ── Config ────────────────────────────────────────────────────────────────────
YEAR     = int(os.environ.get("SAVANT_YEAR") or datetime.now().year)
OUT_FILE = f"savant_metrics_{YEAR}.csv"

print(f"Extracting Baseball Savant data for {YEAR} ...")

# ── Pull each dataset ─────────────────────────────────────────────────────────

def safe_pull(fn, *args, **kwargs):
    """Wrapper so one failed pull doesn't abort the whole run.
    Prints column names on success and full traceback on failure for debugging."""
    try:
        df = fn(*args, **kwargs)
        print(f"  ✓ {fn.__name__}: {len(df)} rows | columns: {list(df.columns)}")
        return df
    except Exception as e:
        print(f"  ✗ {fn.__name__} FAILED: {e}")
        traceback.print_exc()
        return pd.DataFrame()

# Batter percentile ranks: exit_velocity_avg, k_percent, bb_percent, barrel_batted_rate, etc.
bat_pct  = safe_pull(statcast_batter_percentile_ranks, YEAR)

# Batter expected stats: est_ba, est_slg, est_woba, est_woba_minus_woba_diff, pa
bat_exp  = safe_pull(statcast_batter_expected_stats, YEAR)

# Sprint speed: sprint_speed
speed    = safe_pull(statcast_sprint_speed, YEAR)

# Exit velo / barrels: avg_hit_speed, ev95percent, max_hit_speed, brl_percent
bat_ev   = safe_pull(statcast_batter_exitvelo_barrels, YEAR)

# Pitcher percentile ranks
pit_pct  = safe_pull(statcast_pitcher_percentile_ranks, YEAR)

# Pitcher expected stats: est_woba, xera, pa
pit_exp  = safe_pull(statcast_pitcher_expected_stats, YEAR)

# ── Normalize player name columns ─────────────────────────────────────────────
# pybaseball returns names in different formats depending on the endpoint:
#   statcast_batter_percentile_ranks → player_name = "Last, First"
#   statcast_batter_expected_stats   → last_name + first_name columns
#   statcast_sprint_speed            → last_name + first_name columns
#   statcast_batter_exitvelo_barrels → last_name + first_name columns
# All must resolve to "First Last" to match existing savant_metrics rows.

def flip_last_first(name: str) -> str:
    """Convert 'Last, First' → 'First Last'. Leaves 'First Last' unchanged."""
    if isinstance(name, str) and ',' in name:
        last, first = name.split(',', 1)
        return first.strip() + ' ' + last.strip()
    return name

def normalize_name(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "player_name" in df.columns:
        df["player_name"] = df["player_name"].apply(flip_last_first)
    elif "last_name" in df.columns and "first_name" in df.columns:
        df["player_name"] = df["first_name"].str.strip() + " " + df["last_name"].str.strip()
    elif "name" in df.columns:
        df = df.rename(columns={"name": "player_name"})
        df["player_name"] = df["player_name"].apply(flip_last_first)
    return df

bat_pct  = normalize_name(bat_pct)
bat_exp  = normalize_name(bat_exp)
speed    = normalize_name(speed)
bat_ev   = normalize_name(bat_ev)
pit_pct  = normalize_name(pit_pct)
pit_exp  = normalize_name(pit_exp)

# ── Select & rename columns for each source ───────────────────────────────────
# Maps: pybaseball column → CSV/DB column used downstream.
# Each map lists BOTH the current known column name AND common aliases in case
# Baseball Savant renames columns between seasons. slim() takes the first match.

BAT_PCT_COLS = {
    "player_name":          "player_name",
    # EV percentile — alias added for possible 2026+ rename
    "exit_velocity_avg":    "savant_exit_velocity",
    "avg_exit_velocity":    "savant_exit_velocity",   # alias
    # K% percentile — higher = fewer strikeouts for batters
    "k_percent":            "savant_k_percent",
    # BB% percentile — higher = more walks
    "bb_percent":           "savant_bb_percent",
    # Barrel % percentile
    "barrel_batted_rate":   "savant_barrel_pct_rank",
    "brl_percent_ba":       "savant_barrel_pct_rank", # alias
}

BAT_EXP_COLS = {
    "player_name":                    "player_name",
    "pa":                             "savant_pa",
    "est_ba":                         "savant_est_ba",
    "est_slg":                        "savant_est_slg",
    "est_woba":                       "savant_est_woba",
    "est_woba_minus_woba_diff":       "savant_est_woba_minus_woba_diff",
    # aliases for possible column renames
    "xba":                            "savant_est_ba",
    "xslg":                           "savant_est_slg",
    "xwoba":                          "savant_est_woba",
}

SPEED_COLS = {
    "player_name":  "player_name",
    "sprint_speed": "savant_sprint_speed",
    "hp_to_1b":     "savant_sprint_speed",  # alias: some pybaseball versions use this
}

BAT_EV_COLS = {
    "player_name":    "player_name",
    "avg_hit_speed":  "savant_avg_hit_speed",
    "avg_exit_velocity": "savant_avg_hit_speed",   # alias
    "ev95percent":    "savant_ev95percent",
    "ev95_percent":   "savant_ev95percent",         # alias
    "max_hit_speed":  "savant_max_hit_speed",
    "max_exit_velocity": "savant_max_hit_speed",   # alias
    "brl_percent":    "savant_brl_percent",
    "barrel_pct":     "savant_brl_percent",         # alias
}

# CRITICAL FIX: pitcher percentile columns MUST use the SAME output names as
# batter percentile columns. Since pitchers and batters are in separate DataFrames
# (different player_type), there is no naming collision. Using different names
# (savant_pit_k_percent etc.) previously caused pitcher percentile data to be
# silently dropped by load_savant_to_supabase.py's COL_MAP.
PIT_PCT_COLS = {
    "player_name":          "player_name",
    "k_percent":            "savant_k_percent",      # same as batter — keyed by player_type in DB
    "bb_percent":           "savant_bb_percent",     # same as batter
    "exit_velocity_avg":    "savant_exit_velocity",  # same as batter
    "avg_exit_velocity":    "savant_exit_velocity",  # alias
}

# CRITICAL FIX: pitcher expected stats must use the same intermediate column names
# as the batter expected stats so they pass through COL_MAP in the load script.
# Previous names (savant_pit_pa, savant_pit_xwoba, savant_pit_xba) had no COL_MAP
# entries and were silently dropped.
PIT_EXP_COLS = {
    "player_name":  "player_name",
    "pa":           "savant_pa",        # same as batter PA — keyed by player_type
    "xera":         "savant_xera",      # pitcher-specific, maps directly to savant_xera in DB
    "est_woba":     "savant_est_woba",  # maps to savant_xwoba in COL_MAP
    "est_ba":       "savant_est_ba",    # maps to savant_xba in COL_MAP
    # aliases
    "xwoba":        "savant_est_woba",
    "xba":          "savant_est_ba",
}


def slim(df: pd.DataFrame, col_map: dict) -> pd.DataFrame:
    """Keep only mapped columns that actually exist in df, rename them.
    If multiple source columns map to the same target (aliases), takes first match."""
    selected = {}
    seen_targets = set()
    for src, tgt in col_map.items():
        if src in df.columns and tgt not in seen_targets:
            selected[src] = tgt
            seen_targets.add(tgt)
    if "player_name" not in selected:
        return pd.DataFrame()
    return df[list(selected.keys())].rename(columns=selected)


bat_pct  = slim(bat_pct,  BAT_PCT_COLS)
bat_exp  = slim(bat_exp,  BAT_EXP_COLS)
speed    = slim(speed,    SPEED_COLS)
bat_ev   = slim(bat_ev,   BAT_EV_COLS)
pit_pct  = slim(pit_pct,  PIT_PCT_COLS)
pit_exp  = slim(pit_exp,  PIT_EXP_COLS)

print("\nAfter slim():")
for name, df in [("bat_pct",bat_pct),("bat_exp",bat_exp),("speed",speed),
                 ("bat_ev",bat_ev),("pit_pct",pit_pct),("pit_exp",pit_exp)]:
    print(f"  {name}: {len(df)} rows, cols: {list(df.columns)}")

# ── Merge batter tables ───────────────────────────────────────────────────────
batter_frames = [df for df in [bat_pct, bat_exp, speed, bat_ev] if not df.empty]

if batter_frames:
    batters = batter_frames[0]
    for frame in batter_frames[1:]:
        batters = batters.merge(frame, on="player_name", how="outer")
else:
    batters = pd.DataFrame(columns=["player_name"])

batters["player_type"] = "H"   # matches player_type values in savant_metrics schema

# ── Merge pitcher tables ──────────────────────────────────────────────────────
pitcher_frames = [df for df in [pit_pct, pit_exp] if not df.empty]

if pitcher_frames:
    pitchers = pitcher_frames[0]
    for frame in pitcher_frames[1:]:
        pitchers = pitchers.merge(frame, on="player_name", how="outer")
else:
    pitchers = pd.DataFrame(columns=["player_name"])

pitchers["player_type"] = "P"   # matches player_type values in savant_metrics schema

# ── Combine and write ─────────────────────────────────────────────────────────
combined = pd.concat([batters, pitchers], ignore_index=True, sort=False)
combined["season"] = YEAR

# Drop rows with no player name
combined = combined.dropna(subset=["player_name"])
combined = combined[combined["player_name"].str.strip() != ""]

# Deduplicate on (player_name, player_type) — traded players can appear once per
# team in some pybaseball endpoints, causing duplicate PK violations on upsert.
# Keep last occurrence (typically has the fuller stat line for the full season).
before_dedup = len(combined)
combined = combined.drop_duplicates(subset=["player_name", "player_type"], keep="last")
dupes_dropped = before_dedup - len(combined)
if dupes_dropped:
    print(f"  ⚠ Dropped {dupes_dropped} duplicate (player_name, player_type) rows")

combined.to_csv(OUT_FILE, index=False)
print(f"\nWrote {len(combined)} rows → {OUT_FILE}")
print(f"Columns: {list(combined.columns)}")
print(f"Batters: {len(batters.dropna(subset=['player_name']))} | Pitchers: {len(pitchers.dropna(subset=['player_name']))}")
