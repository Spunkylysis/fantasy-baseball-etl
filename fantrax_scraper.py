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
import os
import sys
import time
import shutil
from pathlib import Path
from datetime import datetime

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
        "name":      "Fantrax_Transaction_History_Topps",
        "url":       f"{BASE}/transactions/history;team=DIV_{DIV_TOPPS}",
        "wait_for":  "table, .ag-root, [class*='transactions'], [class*='history']",
    },
    {
        # Rawlings TH: regular TH page is division-scoped to the scraper's Topps
        # account and returns nothing for Rawlings. The commissioner/claim-drop page
        # has Rawlings data but no export button — scrape the ag-grid rows directly.
        "name":        "Fantrax_Transaction_History_Rawlings",
        "url":         f"{BASE}/commissioner/claim-drop;divId={DIV_RAWLINGS}",
        "scrape_type": "commissioner_html",
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


# ── Commissioner claim-drop HTML extractor ─────────────────────────────────────
# The commissioner/claim-drop page has no export button.  We extract ag-grid rows
# directly via JavaScript and write a CSV matching the standard TH export format.

# Maps commissioner page ag-grid header text → standard TH CSV column name.
# Logged on every run so the mapping can be verified and corrected if Fantrax
# changes column names.
COMMISSIONER_COL_MAP = {
    "Player":        "Player",
    "MLB Team":      "Team",
    "Team":          "Team",
    "Pos":           "Position",
    "Position":      "Position",
    "Type":          "Type",
    "Fantasy Team":  "Owner",
    "Owner":         "Owner",
    "Bid":           "Bid",
    "FAAB Bid":      "Bid",
    "FAAB":          "Bid",
    "Time (CDT)":    "Time (CDT)",
    "Date":          "Time (CDT)",
    "Time":          "Time (CDT)",
    "Period":        "Period",
    "Wk":            "Period",
    "Week":          "Period",
}

# Output column order — must match the Topps TH export so csv_to_batches.py
# can merge both files into a single Fantrax_Transaction_History table.
TH_CSV_HEADERS = ["Player", "Team", "Position", "Type", "Owner", "Bid",
                   "Time (CDT)", "Period"]


def scrape_commissioner_claim_drop(driver: webdriver.Chrome, export: dict) -> bool:
    """
    Scrape Fantrax commissioner/claim-drop by extracting ag-grid rows via
    JavaScript.  Saves a CSV matching the standard TH export column layout.
    """
    name = export["name"]
    url  = export["url"]

    log(f"\n── {name} {'─' * max(1, 54 - len(name))}")
    log(f"   → {url}  [commissioner ag-grid extraction]")

    driver.get(url)

    # Wait for at least one ag-grid row to appear
    log("   Waiting for ag-grid rows …")
    try:
        WebDriverWait(driver, PAGE_LOAD_WAIT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".ag-row"))
        )
        time.sleep(5)   # allow Angular to finish rendering all rows
    except TimeoutException:
        _save_debug(driver, name)
        log(f"   ✗  No ag-grid rows found after {PAGE_LOAD_WAIT}s. "
            f"Debug saved → debug_{name}.html")
        return False

    # Scroll to bottom then back to top to materialise virtualised rows
    try:
        driver.execute_script(
            "const vp = document.querySelector('.ag-body-viewport');"
            "if (vp) vp.scrollTop = vp.scrollHeight;"
        )
        time.sleep(2)
        driver.execute_script(
            "const vp = document.querySelector('.ag-body-viewport');"
            "if (vp) vp.scrollTop = 0;"
        )
        time.sleep(1)
    except Exception:
        pass

    # Extract column headers from the rendered ag-grid header row
    raw_headers: list = driver.execute_script("""
        return Array.from(
            document.querySelectorAll('.ag-header-cell .ag-header-cell-text')
        ).map(el => el.textContent.trim()).filter(h => h.length > 0);
    """) or []
    log(f"   Headers found on page: {raw_headers}")

    # Try ag-grid internal API first — returns ALL rows including virtualised ones
    raw_rows = driver.execute_script("""
        const root = document.querySelector('.ag-root-wrapper');
        if (!root) return null;
        for (const key of Object.keys(root)) {
            try {
                const comp = root[key];
                const api  = comp && (comp.gridOptions?.api || comp.api || comp.gridApi);
                if (api && typeof api.forEachNode === 'function') {
                    const rows = [];
                    api.forEachNode(node => { if (node.data) rows.push(Object.values(node.data)); });
                    return rows;
                }
            } catch (e) {}
        }
        return null;
    """)

    if not raw_rows:
        # Fallback: extract from visible DOM cells, sorted by left-offset
        log("   ag-grid API inaccessible — falling back to DOM cell extraction")
        raw_rows = driver.execute_script("""
            const rows = [];
            document.querySelectorAll('.ag-row').forEach(rowEl => {
                const cells = Array.from(rowEl.querySelectorAll('.ag-cell'));
                cells.sort((a, b) =>
                    parseInt(a.style.left || 0) - parseInt(b.style.left || 0)
                );
                rows.push(cells.map(c => c.textContent.trim()));
            });
            return rows;
        """) or []

    if not raw_rows:
        _save_debug(driver, name)
        log(f"   ✗  Extracted 0 rows. Debug saved → debug_{name}.html")
        return False

    log(f"   Extracted {len(raw_rows)} rows")

    # Build header → index map using COMMISSIONER_COL_MAP
    col_index: dict[str, int] = {}
    for i, h in enumerate(raw_headers):
        mapped = COMMISSIONER_COL_MAP.get(h)
        if mapped and mapped not in col_index:
            col_index[mapped] = i
    log(f"   Column mapping applied: {col_index}")
    missing = [c for c in TH_CSV_HEADERS if c not in col_index]
    if missing:
        log(f"   ⚠  Columns not found on page (will be blank): {missing}")

    # Build output rows in standard TH column order
    out_rows = []
    for raw in raw_rows:
        out_row = [
            (raw[col_index[col]] if col in col_index and col_index[col] < len(raw) else "")
            for col in TH_CSV_HEADERS
        ]
        out_rows.append(out_row)

    # Write CSV
    dest = SOURCES_DIR / f"{name}.csv"
    with open(dest, "w", newline="", encoding="utf-8") as f:
        writer = csv_mod.writer(f)
        writer.writerow(TH_CSV_HEADERS)
        writer.writerows(out_rows)

    size_kb = dest.stat().st_size // 1024
    log(f"   ✓  Saved → {dest.name}  ({size_kb} KB, {len(out_rows)} rows)")
    return True


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

        for export in EXPORTS:
            if export.get("scrape_type") == "commissioner_html":
                ok = scrape_commissioner_claim_drop(driver, export)
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
