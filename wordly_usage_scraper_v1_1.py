# wordly_usage_scraper_v1_1.py
# VERSION: 1.1.3
# CHANGE: Match session restore sequence exactly to working v3.2b script;
#         use btn.count()>0 instead of is_visible(); 10s nav wait.
# PURPOSE: Scrape Wordly portal usage page by service and date, write to BigQuery.
# TARGET:  https://portal.wordly.ai/#/usageAll
# BQ:      support-467322.wordly_session_data.sessions

print("🚀 [HEARTBEAT] SCRIPT IS STARTING NOW...")

import os
import io
import re
import json
import time
from datetime import datetime, timedelta

import pandas as pd
from google.cloud import bigquery
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
SCRIPT_DIR   = os.getcwd()
CREDS_FILE   = os.path.join(SCRIPT_DIR, "wordly_creds.txt")
SLACK_FILE   = os.path.join(SCRIPT_DIR, "slack_webhook.txt")
SESSION_FILE = "/Users/wordly_apps/Documents/Code/Wordly_Usage_to_HS/wordly_session_state.json"

# GCP / BQ
PROJECT_ID   = "support-467322"
BQ_DATASET   = "wordly_session_data"
BQ_TABLE     = "sessions"
BQ_TABLE_REF = f"{PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}"

# Headless — always False now (MFA requires visible browser)
HEADLESS = False

# Date range — if START_DATE not set, auto-detect from BQ last scrape date
# Set END_DATE to yesterday by default (nightly run)
# Override via env vars for backfill: START_DATE=03/01/2026 END_DATE=04/30/2026
_yesterday = (datetime.now() - timedelta(days=1)).strftime("%m/%d/%Y")
START_DATE  = os.environ.get("START_DATE", None)   # None = auto-detect from BQ
END_DATE    = os.environ.get("END_DATE",   _yesterday)

# Validation mode — restrict to one service for testing. Set to None for full run.
VALIDATION_SERVICE = None

# Services to skip
EXCLUSION_KEYWORDS = ["Trial", "Intro", "Demo", "Test", "Free", "Wordly Internal", "All"]

# Timing
RESULTS_LOAD_WAIT  = 10
POST_DOWNLOAD_WAIT = 5
MFA_TIMEOUT_MS     = 300000  # 5 minutes to complete MFA


# --- SESSION PERSISTENCE ---

def load_session_state(context):
    """Load saved Keycloak session state if available (~7 day validity)."""
    if os.path.exists(SESSION_FILE):
        print(f"  🔑 Session state found — loading...")
        try:
            with open(SESSION_FILE, "r") as f:
                state = json.load(f)
            context.add_cookies(state.get("cookies", []))
            print(f"  ✅ Loaded {len(state.get('cookies', []))} cookies")
            return True
        except Exception as e:
            print(f"  ⚠️ Could not load session state: {e}")
    print("  ⚠️ No session state file — will do full login with MFA")
    return False


def save_session_state(context):
    """Save Keycloak session state to disk after successful login."""
    try:
        state = context.storage_state()
        with open(SESSION_FILE, "w") as f:
            json.dump(state, f)
        print(f"  💾 Session state saved ({len(state.get('cookies', []))} cookies)")
    except Exception as e:
        print(f"  ⚠️ Could not save session state: {e}")


# --- UTILITIES ---

def normalize_col(name):
    name = name.strip().lower()
    name = re.sub(r'[^a-z0-9]+', '_', name)
    return name.strip('_')


def load_creds(path):
    with open(path, "r") as f:
        raw = f.read().strip()
    if "," in raw:
        email, password = raw.split(",", 1)
    else:
        lines = raw.splitlines()
        if "=" in lines[0]:
            email    = lines[0].split("=", 1)[1].strip()
            password = lines[1].split("=", 1)[1].strip()
        else:
            email, password = lines[0].strip(), lines[1].strip()
    return email.strip(), password.strip()


def send_slack(message, is_error=True):
    import requests as req
    if not os.path.exists(SLACK_FILE):
        print(f"⚠️ Slack webhook file missing: {SLACK_FILE}")
        return
    with open(SLACK_FILE, "r") as f:
        url = f.read().strip()
    prefix = "🚨 *ERROR:* " if is_error else "✅ *SUCCESS:* "
    try:
        req.post(url, json={"text": f"{prefix}{message}"}, timeout=10)
    except Exception as e:
        print(f"⚠️ Slack post failed: {e}")


def nuke_alert(page, location=""):
    try:
        print(f"  ☢️  nuke_alert @ {location}")
        page.evaluate("""() => {
            const selectors = ['#DN70', '.p-dialog-mask', '.p-component-overlay', 'p-dialog'];
            selectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => {
                    const container = el.closest('.p-dialog-mask')
                                   || el.closest('.p-component-overlay')
                                   || el;
                    container.remove();
                });
            });
        }""")
        page.keyboard.press("Escape")
        time.sleep(1)
    except:
        pass


def get_bq_last_date():
    """
    Query BQ for the most recent _scrape_date in the sessions table.
    Returns the day AFTER that date as MM/DD/YYYY string (i.e. where to start next).
    Falls back to 30 days ago if no data found.
    """
    try:
        client = bigquery.Client(project=PROJECT_ID)
        query = f"""
            SELECT MAX(date_parsed) as last_date
            FROM `{PROJECT_ID}.{BQ_DATASET}.sessions_clean`
            WHERE _scrape_date NOT IN ('historical', 'historical_2025_daily')
        """
        result = list(client.query(query).result())
        if result and result[0].last_date:
            last = result[0].last_date
            next_day = last + timedelta(days=1)
            print(f"  📅 BQ last scrape date: {last} → starting from {next_day.strftime('%m/%d/%Y')}")
            return next_day.strftime("%m/%d/%Y")
    except Exception as e:
        print(f"  ⚠️ Could not query BQ for last date: {e}")

    fallback = (datetime.now() - timedelta(days=30)).strftime("%m/%d/%Y")
    print(f"  ⚠️ Falling back to 30 days ago: {fallback}")
    return fallback


# --- LOGIN + SERVICE LIST ---

def login_and_get_services(context, email, password):
    """
    Combined login and service list scan.
    Reuses the same page after login to navigate to usageAll —
    avoids cold Angular bootstrap on a new tab for the service scan.
    Returns list of target services, or empty list on failure.
    """
    page = context.new_page()
    session_loaded = load_session_state(context)

    if session_loaded:
        print("  🔄 Testing saved session...")
        page.goto("https://portal.wordly.ai", wait_until="networkidle")
        page.wait_for_timeout(3000)
        # Click through landing page if present — triggers Keycloak redirect
        try:
            btn = page.locator("#portal-login-btn-signin-wordly")
            if btn.count() > 0:
                btn.click(force=True)
                page.wait_for_timeout(3000)
        except:
            pass
        try:
            page.wait_for_selector("app-root", timeout=8000)
            print("  ✅ Session valid — skipping login")
        except:
            print("  ⚠️ Session expired — deleting stale JSON, falling back to full login")
            if os.path.exists(SESSION_FILE):
                os.remove(SESSION_FILE)
            session_loaded = False

    if not session_loaded:
        page.goto("https://portal.wordly.ai")
        nuke_alert(page, "login page")
        try:
            page.wait_for_selector("#portal-login-btn-signin-wordly", timeout=10000)
            page.click("#portal-login-btn-signin-wordly", force=True)
            page.wait_for_timeout(3000)
        except:
            pass

        page.wait_for_selector("#username", timeout=10000)
        page.fill("#username", email)
        page.fill("#password", password)
        page.click("#kc-login")

        print(f"  ⏳ Waiting for MFA — complete on your phone (up to 5 mins)...")
        try:
            page.wait_for_selector("app-root", timeout=MFA_TIMEOUT_MS)
            print("  ✅ Login complete")
            save_session_state(context)
        except Exception as e:
            print(f"  ❌ Login failed or timed out: {e}")
            page.close()
            return []

    # Navigate to usage page on the same page object — no cold new tab
    print("📋 Reading service list...")
    page.goto("https://portal.wordly.ai/#/usageAll", wait_until="networkidle")
    page.wait_for_timeout(10000)
    nuke_alert(page, "usage page")

    try:
        page.wait_for_selector("#serviceFilter", timeout=45000)
    except Exception as e:
        print(f"  ❌ Service filter not found: {e}")
        page.close()
        return []

    time.sleep(2)
    page.locator("#serviceFilter").click()
    page.wait_for_timeout(2000)

    options  = page.locator(".p-dropdown-item")
    targets  = []
    skipped  = []

    for i in range(options.count()):
        name = options.nth(i).inner_text().strip()
        if any(kw.lower() in name.lower() for kw in EXCLUSION_KEYWORDS):
            skipped.append(name)
        else:
            targets.append(name)

    page.keyboard.press("Escape")
    page.close()

    if VALIDATION_SERVICE:
        targets = [t for t in targets if t == VALIDATION_SERVICE]
        print(f"  🔬 VALIDATION MODE — restricted to: {targets}")
    else:
        print(f"  ✅ {len(targets)} services. Skipping {len(skipped)}: {skipped}")

    return targets


# --- DATE FIELD ---

def fill_date_field(page, field_id, date_str):
    field = page.locator(f"#{field_id}")
    field.click()
    time.sleep(0.5)
    field.click(click_count=3)
    time.sleep(0.2)
    field.type(date_str, delay=80)
    time.sleep(0.3)
    page.keyboard.press("Escape")
    time.sleep(0.4)
    actual = field.input_value()
    print(f"  📅 {field_id} = '{actual}' (expected '{date_str}')")
    return actual == date_str


def fire_filter(page):
    reload_div = page.locator("div.reload-usage-summary")
    reload_div.wait_for(state="visible", timeout=10000)
    reload_div.click()
    print(f"  🔄 Filter fired. Waiting {RESULTS_LOAD_WAIT}s...")
    time.sleep(RESULTS_LOAD_WAIT)


def get_session_count(page):
    try:
        text = page.locator("span.overview-session").inner_text()
        return int(''.join(filter(str.isdigit, text)))
    except:
        return None


# --- SCRAPE ONE ITERATION ---

def scrape_one(context, service, date_str):
    """
    Open a NEW TAB in the existing browser context for each iteration.
    New tab = fresh Session Storage = clean date fields.
    Auth persists via Local Storage across tabs — no re-login needed.
    Returns pandas DataFrame or None.
    """
    page = context.new_page()

    try:
        page.goto("https://portal.wordly.ai/#/usageAll", wait_until="networkidle")
        page.wait_for_timeout(5000)
        nuke_alert(page, "usage page")
        page.wait_for_selector("#serviceFilter", timeout=20000)
        time.sleep(2)

        # Select service
        page.locator("#serviceFilter").click()
        page.wait_for_timeout(1500)
        option = page.locator(f'.p-dropdown-item:has-text("{service}")')
        if option.count() == 0:
            print(f"  ⚠️  Service '{service}' not found in dropdown.")
            return None
        option.first.click()
        page.wait_for_timeout(2000)

        # Fill dates
        from_ok = fill_date_field(page, "fromDate", date_str)
        to_ok   = fill_date_field(page, "toDate",   date_str)
        if not from_ok or not to_ok:
            print(f"  ❌ Date fields rejected.")
            return None

        fire_filter(page)

        # Zero-session check
        count = get_session_count(page)
        if count is not None:
            print(f"  📊 Sessions found: {count}")
            if count == 0:
                print(f"  ⏭️  Zero sessions — skipping.")
                return None

        # Open Options menu
        options_btn = page.locator("span.option-text").first
        options_btn.click(force=True)
        page.wait_for_timeout(1500)

        download_link = page.locator(
            "span.p-menuitem-text:has-text('Download Activity Report')"
        )
        if download_link.count() == 0:
            print(f"  ⚠️  Download option not found.")
            return None

        print(f"  💾 Downloading...")
        with page.expect_download(timeout=120000) as dl:
            download_link.click(force=True)

        tmp_path = dl.value.path()
        time.sleep(POST_DOWNLOAD_WAIT)

        df = pd.read_csv(tmp_path)
        print(f"  ✅ {len(df)} rows downloaded.")
        return df

    except Exception as e:
        print(f"  ❌ scrape_one failed: {e}")
        return None

    finally:
        try:
            page.close()
        except:
            pass


# --- DATA PREP ---

def prepare_df(df, service, date_str):
    df.columns = [normalize_col(c) for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated(keep='first')]
    df = df.astype(str).replace('nan', None)

    if "account" in df.columns:
        parsed = df["account"].str.extract(r'^(.*?)\s*\(([^)]+)\)\s*$')
        df["account_name"]      = parsed[0].where(parsed[0].notna(), df["account"])
        df["wordly_account_id"] = parsed[1]
        df.rename(columns={"account": "account_raw"}, inplace=True)

    df["_service"]     = service
    df["_scrape_date"] = date_str
    df["_inserted_at"] = datetime.now().isoformat()

    return df


# --- BQ ---

def ensure_bq_dataset():
    client = bigquery.Client(project=PROJECT_ID)
    dataset_ref = bigquery.Dataset(f"{PROJECT_ID}.{BQ_DATASET}")
    dataset_ref.location = "US"
    try:
        client.get_dataset(dataset_ref)
        print(f"  ✅ BQ dataset '{BQ_DATASET}' exists.")
    except Exception:
        client.create_dataset(dataset_ref, exists_ok=True)
        print(f"  ✅ BQ dataset '{BQ_DATASET}' created.")


def write_to_bq(df):
    client = bigquery.Client(project=PROJECT_ID)
    job_config = bigquery.LoadJobConfig(
        write_disposition     = bigquery.WriteDisposition.WRITE_APPEND,
        autodetect            = True,
        schema_update_options = [
            bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION,
            bigquery.SchemaUpdateOption.ALLOW_FIELD_RELAXATION,
        ],
    )
    job = client.load_table_from_dataframe(df, BQ_TABLE_REF, job_config=job_config)
    job.result()
    print(f"  ✅ BQ: {len(df)} rows written to {BQ_TABLE_REF}")
    return len(df)


# --- DATE RANGE ---

def date_range(start_str, end_str):
    fmt = "%m/%d/%Y"
    current = datetime.strptime(start_str, fmt)
    end     = datetime.strptime(end_str,   fmt)
    while current <= end:
        yield current.strftime(fmt)
        current += timedelta(days=1)


# --- MAIN ---

def run():
    # Resolve start date
    start = START_DATE if START_DATE else get_bq_last_date()

    print(f"\n🚀 wordly_session_scraper v1.1.3 — "
          f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   Date range  : {start} → {END_DATE}")
    print(f"   BQ table    : {BQ_TABLE_REF}")
    print(f"   Validation  : {VALIDATION_SERVICE or 'OFF — all services'}\n")

    email, password = load_creds(CREDS_FILE)
    ensure_bq_dataset()

    total_rows  = 0
    total_skips = 0
    fail_log    = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        # One persistent context for the entire run
        context = browser.new_context(accept_downloads=True)

        # Login + get service list in one flow
        services = login_and_get_services(context, email, password)
        if not services:
            print("❌ Could not log in or read services. Aborting.")
            browser.close()
            return

        for service in services:
            print(f"\n{'='*60}")
            print(f"SERVICE: {service}")
            print(f"{'='*60}")

            for date_str in date_range(start, END_DATE):
                label = f"{service} / {date_str}"
                print(f"\n  📅 {label}")

                try:
                    df = scrape_one(context, service, date_str)

                    if df is None:
                        total_skips += 1
                        continue

                    df = prepare_df(df, service, date_str)
                    rows = write_to_bq(df)
                    total_rows += rows

                except Exception as e:
                    msg = f"{label} — {e}"
                    print(f"  ❌ FAILED: {msg}")
                    fail_log.append(msg)

        browser.close()

    # --- SUMMARY ---
    print(f"\n{'='*60}")
    print(f"🏁 DONE — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   ✅ Rows to BQ  : {total_rows}")
    print(f"   ⏭️  Skipped     : {total_skips}")
    print(f"   ❌ Failed      : {len(fail_log)}")
    if fail_log:
        print("\n   Failures:")
        for f in fail_log:
            print(f"     • {f}")
    print(f"{'='*60}\n")

    if fail_log:
        send_slack(
            f"session_scraper: {total_rows} rows, {len(fail_log)} failures. "
            f"Check logs. {start}→{END_DATE}",
            is_error=True
        )
    else:
        send_slack(
            f"✅ Success: Daily Session Info captured. "
            f"{total_rows} rows | {total_skips} zero-skips | {start}→{END_DATE}",
            is_error=False
        )


if __name__ == "__main__":
    run()