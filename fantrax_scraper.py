"""
fantrax_scraper.py
Phase 4: Selenium scraper for Fantrax.

Downloads 7 CSVs (4 player tables, Transaction History, 2 Draft results)
to the Sources folder. Credentials are read from .env.

Usage:
    python fantrax_scraper.py

Requirements:
    pip install -r requirements_scraper.txt
"""

import csv as csv_mod
import json
import os
import re
import sys
import time
import shutil
from pathlib import Path
from datetime import datetime
import datetime as dt_mod

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

# ── Config ─────────────────────────────────────────────────────────────────────

load_dotenv()

FANTRAX_EMAIL    = os.environ.get("FANTRAX_EMAIL", "")
FANTRAX_PASSWORD = os.environ.get("FANTRAX_PASSWORD", "")

# FANTRAX_HEADLESS=true  → headless Chrome (used in GitHub Actions)
# FANTRAX_SOURCES_DIR    → override download destination (used in GitHub Actions)
HEADLESS    = os.environ.get("FANTRAX_HEADLESS", "false").lower() == "true"
SOURCES_DIR = Path(
    os.environ.get(
        "FANTRAX_SOURCES_DIR",
        r"C:\Users\James\OneDrive\Documents\Claude-Working-Folder\Fantrax\Sources",
    )
)

LOGIN_URL      = "https://www.fantrax.com/login"
LEAGUE_ID      = "cwp6mey5mk1ubete"
BASE           = f"https://www.fantrax.com/fantasy/league/{LEAGUE_ID}"
DIV_TOPPS      = "2s2tcf9lmk1vc3zd"
DIV_RAWLINGS   = "0xwii66vmk1vc3zd"

PAGE_LOAD_WAIT = 25   # seconds: wait for page elements after navigation
DOWNLOAD_WAIT  = 45   # seconds: wait for a CSV download to complete

# ── Export targets ─────────────────────────────────────────────────────────────
# name       → canonical CSV filename written to SOURCES_DIR
# url        → Fantrax page to navigate to
# wait_for   → CSS selector that confirms the page content has loaded

EXPORTS = [
    {
        "name":      "Fantrax_Players_Hitters_Topps",
        "url":       f"{BASE}/players;statusOrTeamFilter=ALL;pageNumber=1"
                     f";positionOrGroup=BASEBALL_HITTING;miscDisplayType=1"
                     f";divisionId={DIV_TOPPS}",
        "wait_for":  "table, .ag-root, .player-table, [class*='playerTable'], [class*='fantasy-table']",
    },
    {
        "name":      "Fantrax_Players_Hitters_Rawlings",
        "url":       f"{BASE}/players;statusOrTeamFilter=ALL;pageNumber=1"
                     f";positionOrGroup=BASEBALL_HITTING;miscDisplayType=1"
                     f";divisionId={DIV_RAWLINGS}",
        "wait_for":  "table, .ag-root, .player-table, [class*='playerTable'], [class*='fantasy-table']",
    },
    {
        "name":      "Fantrax_Players_Pitchers_Topps",
        "url":       f"{BASE}/players;statusOrTeamFilter=ALL;pageNumber=1"
                     f";positionOrGroup=BASEBALL_PITCHING;miscDisplayType=1"
                     f";divisionId={DIV_TOPPS}",
        "wait_for":  "table, .ag-root, .player-table, [class*='playerTable'], [class*='fantasy-table']",
    },
    {
        "name":      "Fantrax_Players_Pitchers_Rawlings",
        "url":       f"{BASE}/players;statusOrTeamFilter=ALL;pageNumber=1"
                     f";positionOrGroup=BASEBALL_PITCHING;miscDisplayType=1"
                     f";divisionId={DIV_RAWLINGS}",
        "wait_for":  "table, .ag-root, .player-table, [class*='playerTable'], [class*='fantasy-table']",
    },
    {
        # Topps TH: export normally AND capture the XHR API call so we can
        # replay it for Rawlings (which has no accessible TH page for this account).
        "name":        "Fantrax_Transaction_History_Topps",
        "url":         f"{BASE}/transactions/history;team=DIV_{DIV_TOPPS}",
        "wait_for":    "table, .ag-root, [class*='transactions'], [class*='history']",
        "scrape_type": "capture_api",
    },
    {
        # Rawlings TH: replays the captured Fantrax API call with DIV_RAWLINGS.
        # No Fantrax page accessible to the scraper account exposes Rawlings TH
        # (the regular TH page is Topps-scoped; the commissioner/claim-drop page
        # is a roster management tool, not a history view).
        "name":        "Fantrax_Transaction_History_Rawlings",
        "scrape_type": "api_replay",
    },
    {
        "name":      "Fantrax_HOD_Drafts_Topps",
        "url":       f"{BASE}/draft-results;divisionId={DIV_TOPPS}?view=TEAM",
        "wait_for":  "table, .ag-root, [class*='draft'], [class*='pick']",
    },
    {
        "name":      "Fantrax_HOD_Drafts_Rawlings",
        "url":       f"{BASE}/draft-results;divisionId={DIV_RAWLINGS}?view=TEAM",
        "wait_for":  "table, .ag-root, [class*='draft'], [class*='pick']",
    },
    {
        "name":      "Fantrax_Standings",
        "url":       f"{BASE}/standings;view=SEASON_STATS",
        "wait_for":  "table, .ag-root, [class*='standings'], [class*='leagueStandings']",
    },
]

# ── Selectors tried (in order) to find the Export/Download button ───────────────
# Fantrax uses Angular Material icons. The debug scan of a live Fantrax page
# confirmed the download button uses the 'get_app' mat-icon text.
EXPORT_BUTTON_SELECTORS = [
    # PRIMARY — confirmed via debug scan of live Fantrax page
    (By.XPATH,        "//mat-icon[normalize-space()='get_app']//ancestor::button[1]"),
    # Other common Material icon names for download
    (By.XPATH,        "//mat-icon[normalize-space()='save_alt']//ancestor::button[1]"),
    (By.XPATH,        "//mat-icon[normalize-space()='file_download']//ancestor::button[1]"),
    (By.XPATH,        "//mat-icon[normalize-space()='cloud_download']//ancestor::button[1]"),
    (By.XPATH,        "//mat-icon[normalize-space()='download']//ancestor::button[1]"),
    # aria-label / title
    (By.XPATH,        "//button[contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'export')]"),
    (By.XPATH,        "//button[contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'download')]"),
    (By.XPATH,        "//button[contains(translate(@title,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'export')]"),
    # Visible button text
    (By.XPATH,        "//button[contains(translate(normalize-space(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'export')]"),
    # CSS class-based
    (By.CSS_SELECTOR, "button.export-btn, button[data-testid='export'], .export-icon button"),
    # Link-based export
    (By.XPATH,        "//a[contains(translate(normalize-space(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'export')]"),
]


# CSV column headers that csv_to_batches.py / load_supabase_actions.py expect
# for Transaction History data.  Rawlings JSON must be converted to this layout.
TH_CSV_HEADERS = ["Player", "Team", "Position", "Type", "Owner", "Bid", "Time (CDT)", "Period"]


# ── Utilities ──────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def make_driver() -> webdriver.Chrome:
    """Launch Chrome with the Sources folder as the download directory."""
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)

    prefs = {
        "download.default_directory":        str(SOURCES_DIR),
        "download.prompt_for_download":      False,
        "download.directory_upgrade":        True,
        "safebrowsing.enabled":              True,
        # Allow downloads in headless mode
        "profile.default_content_setting_values.automatic_downloads": 1,
    }
    opts = Options()
    opts.add_experimental_option("prefs", prefs)
    opts.add_argument("--disable-blink-features=AutomationControlled")

    if HEADLESS:
        # CI / GitHub Actions mode — no visible window needed
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument(
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
        log("  Running in headless mode.")
    else:
        # Local mode — keep browser open so failures are inspectable
        opts.add_experimental_option("detach", True)
        opts.add_argument("--start-maximized")

    # Enable performance logging so we can capture XHR/fetch requests made by
    # Angular pages.  Used by scrape_th_via_api() to find the Fantrax API URL.
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


def wait_for_download(timeout: int = DOWNLOAD_WAIT) -> Path | None:
    """
    Block until a new non-partial CSV file appears in SOURCES_DIR.
    Snapshot the directory before calling this function.
    Returns the Path of the newest CSV, or None on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        csvs = [
            f for f in SOURCES_DIR.iterdir()
            if f.suffix.lower() == ".csv" and not f.name.endswith(".crdownload")
        ]
        if csvs:
            # Return the most recently modified CSV
            newest = max(csvs, key=lambda f: f.stat().st_mtime)
            time.sleep(1)   # let the file finish flushing
            return newest
        time.sleep(0.5)
    return None


def snapshot_csvs() -> set[Path]:
    """Return the set of CSV paths currently in SOURCES_DIR."""
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    return {f for f in SOURCES_DIR.iterdir() if f.suffix.lower() == ".csv"}


# ── Login ──────────────────────────────────────────────────────────────────────

def is_logged_in(driver: webdriver.Chrome) -> bool:
    """
    Return True if the browser already has an active Fantrax session.
    Navigates to the league home page and checks for the logged-in nav.
    """
    try:
        driver.get(f"{BASE}/home")
        # The leagues nav item only renders when authenticated
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "[anchorid='DESKTOP_NAV.leagues']")
            )
        )
        return True
    except TimeoutException:
        return False


def login(driver: webdriver.Chrome) -> None:
    if not HEADLESS:
        # Local mode: Fantrax session cookies may already be present —
        # skip the login form if we're already in.
        log("Checking authentication status …")
        if is_logged_in(driver):
            log("  ✓  Already logged in — skipping credentials.")
            return

    # Headless / CI: fresh Chrome has no cookies. is_logged_in() false-positives
    # because Fantrax renders the nav shell even for unauthenticated visitors.
    # Always do the full login flow in headless mode.
    log("Navigating to login page …")
    driver.get(LOGIN_URL)
    # Save the login page immediately so we can inspect it if anything goes wrong
    _save_debug(driver, "login_page_initial")
    time.sleep(3)   # let Angular finish the initial render
    wait = WebDriverWait(driver, PAGE_LOAD_WAIT)

    # ── Email ──
    # Fantrax uses Angular Material: the email input has formcontrolname="email"
    # but NO type="email", name="email", or id="email" — check that first.
    email_selectors = [
        "input[formcontrolname='email']",   # Angular Material — confirmed selector
        "input[type='email']",
        "input[name='email']",
        "#email",
        "input[placeholder*='email' i]",
        "input[autocomplete='email']",
    ]
    email_field = None
    # First selector is the confirmed match — give it the full wait.
    # Fallback selectors get a short timeout so failures don't pile up.
    for idx, sel in enumerate(email_selectors):
        timeout = PAGE_LOAD_WAIT if idx == 0 else 5
        try:
            email_field = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel))
            )
            break
        except TimeoutException:
            continue

    if email_field is None:
        _save_debug(driver, "login_page_no_form")
        raise RuntimeError(
            "Could not find email field on login page. "
            "Check debug_login_page_no_form.html — Fantrax may use a modal "
            "or a different login URL."
        )

    email_field.clear()
    email_field.send_keys(FANTRAX_EMAIL)

    # ── Password ──
    pw_field = None
    for sel in ["input[type='password']", "input[name='password']", "#password"]:
        try:
            pw_field = driver.find_element(By.CSS_SELECTOR, sel)
            break
        except NoSuchElementException:
            continue

    if pw_field is None:
        raise RuntimeError("Could not find password field on login page.")

    pw_field.clear()
    pw_field.send_keys(FANTRAX_PASSWORD)

    # ── Submit ──
    try:
        btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        btn.click()
    except NoSuchElementException:
        pw_field.send_keys(Keys.RETURN)

    # ── Wait for redirect away from /login ──
    log("Waiting for login redirect …")
    try:
        WebDriverWait(driver, PAGE_LOAD_WAIT).until(
            lambda d: "/login" not in d.current_url.lower()
        )
        log("  ✓  Logged in.")
    except TimeoutException:
        _save_debug(driver, "login_failed")
        raise RuntimeError(
            "Login timed out — still on login page after submit. "
            "Verify FANTRAX_EMAIL / FANTRAX_PASSWORD in your .env, "
            "or check debug_login_failed.html to see if a CAPTCHA appeared."
        )


# ── Per-page export ────────────────────────────────────────────────────────────

def export_page(driver: webdriver.Chrome, export: dict) -> bool:
    """Navigate to one Fantrax export URL, click the export button, save the CSV."""
    name     = export["name"]
    url      = export["url"]
    wait_sel = export["wait_for"]

    log(f"\n── {name} {'─' * max(1, 54 - len(name))}")
    log(f"   → {url}")

    driver.get(url)

    # ── Wait for page content ──
    log("   Waiting for page content …")
    try:
        WebDriverWait(driver, PAGE_LOAD_WAIT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, wait_sel))
        )
        # Extra pause for Angular to finish rendering rows
        time.sleep(3)
    except TimeoutException:
        log(f"   ⚠  Table not detected — proceeding anyway (page may still be loading).")
        time.sleep(5)

    # ── Snapshot before download ──
    before = snapshot_csvs()

    # ── Find export button ──
    export_btn = None
    for by, sel in EXPORT_BUTTON_SELECTORS:
        try:
            export_btn = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((by, sel))
            )
            log(f"   Found export button: {sel}")
            break
        except TimeoutException:
            continue

    if export_btn is None:
        _save_debug(driver, name)
        log(
            f"   ✗  Export button not found.\n"
            f"      Page source saved → debug_{name}.html\n"
            f"      Open that file, search for 'export' or 'download' to find the correct selector,\n"
            f"      then update EXPORT_BUTTON_SELECTORS in fantrax_scraper.py."
        )
        return False

    # ── Click ──
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", export_btn)
    time.sleep(0.4)
    try:
        export_btn.click()
    except Exception:
        driver.execute_script("arguments[0].click();", export_btn)
    log("   Clicked export, waiting for download …")

    # ── Wait for new CSV ──
    deadline = time.time() + DOWNLOAD_WAIT
    downloaded = None
    while time.time() < deadline:
        after  = snapshot_csvs()
        new    = after - before
        # filter out partial downloads
        ready  = [f for f in new if not f.name.endswith(".crdownload")]
        if ready:
            downloaded = max(ready, key=lambda f: f.stat().st_mtime)
            time.sleep(1)
            break
        time.sleep(0.5)

    if downloaded is None:
        log(f"   ✗  Download timed out for {name}.")
        return False

    # ── Rename to canonical filename ──
    dest = SOURCES_DIR / f"{name}.csv"
    if dest.exists():
        dest.unlink()
    shutil.move(str(downloaded), str(dest))
    size_kb = dest.stat().st_size // 1024
    log(f"   ✓  Saved → {dest.name}  ({size_kb} KB)")
    return True


# ── Rawlings TH via API intercept ──────────────────────────────────────────────
# The regular TH page (transactions/history) is division-scoped to the scraper
# account's Topps membership — team=DIV_{DIV_RAWLINGS} returns nothing.
# The commissioner/claim-drop page is a roster management tool, NOT transaction
# history.  No Fantrax page accessible to this account has a Rawlings TH export.
#
# Solution: intercept the XHR/fetch Fantrax makes when loading the Topps TH page,
# extract the API URL + headers, then replay it with DIV_RAWLINGS.  The Angular
# app calls the same backend endpoint for both divisions — only the divisionId
# parameter differs.  We use requests + Selenium session cookies so auth is
# handled automatically.

try:
    import requests as _requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False


def _drain_perf_log(driver: webdriver.Chrome) -> list[dict]:
    """Return all accumulated performance log entries and clear the buffer."""
    try:
        return [json.loads(e["message"])["message"] for e in driver.get_log("performance")]
    except Exception:
        return []


def _parse_fantrax_th_json(data, log_fn) -> list | None:
    """
    Convert Fantrax /fxpa/req JSON response into TH CSV rows.

    Returns list of 8-element rows matching TH_CSV_HEADERS, or None if the
    JSON structure is unrecognizable.

    Logs diagnostic info so structure can be inspected from the ETL log if
    parsing fails or field mapping is wrong.
    """
    # ── Log top-level structure ───────────────────────────────────────────────
    if isinstance(data, dict):
        log_fn(f"   JSON top-level keys: {list(data.keys())}")
    elif isinstance(data, list):
        log_fn(f"   JSON is list[{len(data)}]")

    # ── Locate the transaction array ──────────────────────────────────────────
    TX_KEYS = {"transactionType", "type", "action", "player", "playerName",
               "addPlayerInfo", "dropPlayerInfo", "fantasyTeam"}

    def _find_tx(obj, depth=0):
        if depth > 6:
            return None
        if isinstance(obj, list):
            if len(obj) > 0 and isinstance(obj[0], dict):
                first = obj[0]
                # Confident match: has known transaction field names
                if TX_KEYS & set(first.keys()):
                    return obj
                # Probable match: large list of dicts
                if len(obj) > 2:
                    return obj
            # Not a tx list — recurse into elements to find one
            candidates = [c for v in obj for c in [_find_tx(v, depth + 1)] if c]
            if candidates:
                return max(candidates, key=len)
            return None
        if isinstance(obj, dict):
            # Try well-known key names first
            for key in ("transactions", "transactionList", "items", "rows",
                        "history", "claims", "drops", "adds"):
                if key in obj and isinstance(obj[key], list) and len(obj[key]) > 0:
                    return obj[key]
            # Recurse into all values, pick the largest result
            candidates = [c for v in obj.values() for c in [_find_tx(v, depth + 1)] if c]
            if candidates:
                return max(candidates, key=len)
        return None

    tx_list = _find_tx(data)
    if not tx_list:
        log_fn("   ✗  No transaction list located in JSON")
        log_fn(f"   JSON (first 1000 chars): {json.dumps(data)[:1000]}")
        return None

    log_fn(f"   Found {len(tx_list)} transaction object(s)")
    log_fn(f"   Sample keys : {list(tx_list[0].keys())[:20]}")
    log_fn(f"   Sample value: {json.dumps(tx_list[0])[:400]}")

    # ── Field extraction helpers ──────────────────────────────────────────────

    def _norm_pos(pos):
        if not pos:
            return ""
        if isinstance(pos, list):
            return ",".join(str(p) for p in pos)
        return str(pos)

    def _norm_type(raw):
        if not raw:
            return ""
        s = str(raw).upper()
        if "DROP" in s:
            return "Drop"
        if "ADD" in s or "CLAIM" in s or "WAIVER" in s:
            return "Claim"
        return str(raw)

    def _norm_date(val):
        if val is None:
            return ""
        if isinstance(val, (int, float)) and val > 1e10:
            # millisecond epoch
            try:
                return dt_mod.datetime.utcfromtimestamp(val / 1000).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return str(val)
        if isinstance(val, (int, float)) and val > 1e6:
            # second epoch
            try:
                return dt_mod.datetime.utcfromtimestamp(val).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return str(val)
        return str(val)

    def _get_player(tx):
        """Return (name, mlb_team, positions) from a transaction dict."""
        # Flat fields
        name = tx.get("playerName") or tx.get("player") or tx.get("name")
        team = tx.get("mlbTeam") or tx.get("team") or tx.get("mlbTeamId")
        pos  = tx.get("position") or tx.get("positions") or tx.get("pos")
        if name:
            return str(name), str(team or ""), _norm_pos(pos)

        # Nested: addPlayerInfo.player / dropPlayerInfo.player
        for info_key in ("addPlayerInfo", "dropPlayerInfo", "playerInfo"):
            info = tx.get(info_key)
            if not isinstance(info, dict):
                continue
            p = info.get("player", info)
            if isinstance(p, dict):
                name = p.get("name") or p.get("displayName") or p.get("playerName")
                team = p.get("mlbTeamId") or p.get("mlbTeam") or p.get("team")
                pos  = p.get("positions") or p.get("position") or p.get("eligiblePositions")
                if name:
                    return str(name), str(team or ""), _norm_pos(pos)
        return "", "", ""

    def _get_owner(tx):
        """Return fantasy team name string."""
        for key in ("fantasyTeamName", "owner", "teamName"):
            v = tx.get(key)
            if v and isinstance(v, str):
                return v
        for key in ("fantasyTeam", "toTeam", "team"):
            v = tx.get(key)
            if isinstance(v, dict):
                n = v.get("name") or v.get("displayName")
                if n:
                    return str(n)
        return ""

    # ── Build rows ────────────────────────────────────────────────────────────
    rows = []
    for tx in tx_list:
        player, team, pos = _get_player(tx)
        tx_type = _norm_type(
            tx.get("transactionType") or tx.get("type") or tx.get("action")
        )
        owner   = _get_owner(tx)
        bid_val = (tx.get("faabBid") if tx.get("faabBid") is not None
                   else tx.get("bid") if tx.get("bid") is not None
                   else tx.get("amount") if tx.get("amount") is not None
                   else tx.get("faab"))
        bid     = "" if bid_val is None else str(bid_val)
        date_val = (tx.get("processedDate") or tx.get("processedDateMs")
                    or tx.get("transactionDate") or tx.get("date")
                    or tx.get("timestamp"))
        date_str = _norm_date(date_val)
        period   = (tx.get("period") or tx.get("scoringPeriod")
                    or tx.get("scoringPeriodId") or tx.get("periodId") or "")
        rows.append([player, team, pos, tx_type, owner, bid, date_str,
                     str(period) if period else ""])

    return rows


def scrape_th_via_api(driver: webdriver.Chrome, export: dict,
                      topps_api_info: dict) -> bool:
    """
    Replay the Fantrax TH API call captured while loading the Topps TH page,
    substituting DIV_RAWLINGS for DIV_TOPPS in the request URL/body.

    topps_api_info is populated by export_page() when it processes the Topps TH
    entry (scrape_type='capture_api').  It contains:
        url      – full API endpoint URL
        method   – 'GET' or 'POST'
        headers  – dict of request headers (including auth/session tokens)
        body     – raw POST body string (or None for GET)
        cookies  – dict of session cookies from Selenium
    """
    name = export["name"]
    log(f"\n── {name} {'─' * max(1, 54 - len(name))}")

    if not topps_api_info:
        log("   ✗  No Topps API info captured — cannot replay for Rawlings.")
        log("      Check that Fantrax_Transaction_History_Topps ran first "
            "with scrape_type='capture_api'.")
        return False

    if not _REQUESTS_OK:
        log("   ✗  'requests' library not installed. Run: pip install requests")
        return False

    # Substitute Rawlings division ID everywhere the Topps ID appears
    api_url  = topps_api_info["url"].replace(DIV_TOPPS, DIV_RAWLINGS)
    method   = topps_api_info["method"]
    headers  = topps_api_info["headers"]
    body     = topps_api_info.get("body") or ""
    if body:
        body = body.replace(DIV_TOPPS, DIV_RAWLINGS)
    cookies  = topps_api_info["cookies"]

    log(f"   Replaying {method} {api_url[:120]}")

    sess = _requests.Session()
    for c in cookies:
        sess.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

    try:
        if method == "POST":
            resp = sess.post(api_url, data=body, headers=headers, timeout=30)
        else:
            resp = sess.get(api_url, headers=headers, timeout=30)
    except Exception as e:
        log(f"   ✗  API request failed: {e}")
        return False

    log(f"   Response: HTTP {resp.status_code}, {len(resp.content):,} bytes, "
        f"Content-Type: {resp.headers.get('Content-Type','?')[:60]}")

    if resp.status_code != 200:
        log(f"   ✗  Non-200 response. Body: {resp.text[:300]}")
        return False

    content_type = resp.headers.get("Content-Type", "")
    dest = SOURCES_DIR / f"{name}.csv"

    # ── Detect JSON vs CSV response ───────────────────────────────────────────
    is_json = "application/json" in content_type or resp.content[:1] in (b"{", b"[")
    if is_json:
        log("   Response is JSON — parsing into TH CSV format …")
        # Log POST body so we can understand what method was called
        log(f"   POST body (first 300): {(topps_api_info.get('body') or '')[:300]}")
        try:
            data = resp.json()
        except Exception as e:
            log(f"   ✗  JSON parse error: {e}")
            debug = SOURCES_DIR / f"{name}_debug.json"
            debug.write_bytes(resp.content)
            log(f"   Raw response saved → {debug.name} for inspection")
            return False

        rows = _parse_fantrax_th_json(data, log)
        if rows is None:
            log("   ✗  JSON structure not recognized — saving raw JSON for inspection")
            debug = SOURCES_DIR / f"{name}_debug.json"
            debug.write_text(resp.text, encoding="utf-8")
            log(f"   Raw JSON saved → {debug.name}")
            return False

        with open(dest, "w", newline="", encoding="utf-8") as f:
            writer = csv_mod.writer(f)
            writer.writerow(TH_CSV_HEADERS)
            writer.writerows(rows)
        log(f"   ✓  Parsed {len(rows)} rows → {dest.name}")
    else:
        # Already CSV (same format as Topps TH export) — save directly
        dest.write_bytes(resp.content)
        size_kb = len(resp.content) // 1024
        log(f"   ✓  Saved CSV → {dest.name}  ({size_kb} KB)")

    return True


def export_page_capture_api(driver: webdriver.Chrome, export: dict,
                             topps_api_info: dict) -> bool:
    """
    Wrapper around export_page() that additionally captures the XHR/fetch call
    Fantrax makes to populate the TH table.  Populates topps_api_info in-place.
    """
    # Drain any stale perf log entries before navigating
    _drain_perf_log(driver)

    ok = export_page(driver, export)

    # Scan performance log for the Fantrax data API call
    entries = _drain_perf_log(driver)
    for entry in entries:
        method = entry.get("method", "")
        params = entry.get("params", {})

        if method == "Network.requestWillBeSent":
            req = params.get("request", {})
            url = req.get("url", "")
            # Look for a Fantrax XHR that contains the Topps division ID in its
            # URL or POST body — this is the data API call we want to replay.
            body = req.get("postData", "") or ""
            if DIV_TOPPS in url or DIV_TOPPS in body:
                rtype = params.get("type", "")
                if rtype in ("XHR", "Fetch", "fetch", "xhr"):
                    log(f"   [API capture] {req.get('method','GET')} {url[:100]}")
                    topps_api_info["url"]    = url
                    topps_api_info["method"] = req.get("method", "GET")
                    topps_api_info["headers"] = req.get("headers", {})
                    topps_api_info["body"]   = body
                    # Grab cookies from Selenium session
                    topps_api_info["cookies"] = driver.get_cookies()
                    break   # first match is the data call

    if not topps_api_info:
        log("   ⚠  No Fantrax API call captured for Topps TH — "
            "Rawlings replay will be skipped.")

    return ok


# ── Debug helper ───────────────────────────────────────────────────────────────

def _save_debug(driver: webdriver.Chrome, label: str) -> None:
    out = Path(f"debug_{label}.html")
    out.write_text(driver.page_source, encoding="utf-8")
    log(f"   Debug page source saved → {out.resolve()}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    if not FANTRAX_EMAIL or not FANTRAX_PASSWORD:
        print("ERROR: FANTRAX_EMAIL and FANTRAX_PASSWORD must be set in .env")
        print("       Copy .env.example to .env and fill in your credentials.")
        return 1

    log("=" * 60)
    log("  Fantrax Scraper — Phase 4")
    log(f"  Target folder: {SOURCES_DIR}")
    log("=" * 60)

    driver = make_driver()
    results = []   # list of (name: str, ok: bool)

    try:
        login(driver)

        topps_api_info: dict = {}   # populated by capture_api, consumed by api_replay

        for export in EXPORTS:
            stype = export.get("scrape_type")
            if stype == "capture_api":
                ok = export_page_capture_api(driver, export, topps_api_info)
            elif stype == "api_replay":
                ok = scrape_th_via_api(driver, export, topps_api_info)
            else:
                ok = export_page(driver, export)
            results.append((export["name"], ok))

    except RuntimeError as e:
        log(f"\nFATAL: {e}")
        return 1
    finally:
        log("\n" + "=" * 60)
        log("  Summary")
        log("=" * 60)
        for name, ok in results:
            status = "\u2713" if ok else "\u2717"
            log(f"  {status}  {name}")

        failed = [n for n, ok in results if not ok]
        if failed:
            log(f"\n  {len(failed)} export(s) failed.")
            log("  Browser left open — inspect failed pages manually.")
            log("  Then update EXPORT_BUTTON_SELECTORS in fantrax_scraper.py.")
        else:
            log(f"\n  All {len(results)} exports complete.")
            log(f"  CSVs in: {SOURCES_DIR}")
            if not HEADLESS:
                log("  Browser left open — close it when done.")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
