# wordly_session_scraper_v1_0_CLOUD.py
# VERSION: 1.0.2-CLOUD
# CHANGE: Add HEADLESS env var for local testing; parse Account field into
#         account_name + wordly_account_id + account_raw.
# PURPOSE: Scrape Wordly portal usage page by service and date, write to BigQuery.
# TARGET:  https://portal.wordly.ai/#/usageAll
# BQ:      support-467322.wordly_session_data.sessions

print("🚀 [HEARTBEAT] SCRIPT IS STARTING NOW...")

import os
import io
import re
import time
from datetime import datetime, timedelta

import pandas as pd
from google.cloud import bigquery
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
SCRIPT_DIR  = os.getcwd()
CREDS_FILE  = os.path.join(SCRIPT_DIR, "wordly_creds.txt")
SLACK_FILE  = os.path.join(SCRIPT_DIR, "slack_webhook.txt")

# GCP / BQ
PROJECT_ID  = "support-467322"
BQ_DATASET  = "wordly_session_data"
BQ_TABLE    = "sessions"
BQ_TABLE_REF = f"{PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}"

# Headless — False for local testing, True for Cloud Run
# Cloud Run should set env var HEADLESS=true
HEADLESS    = os.environ.get("HEADLESS", "false").lower() == "true"

# Date range — default is yesterday (nightly run)
# Override via env vars for backfill: START_DATE=03/01/2026 END_DATE=04/30/2026
_yesterday  = (datetime.utcnow() - timedelta(days=1)).strftime("%m/%d/%Y")
START_DATE  = os.environ.get("START_DATE", "03/01/2026")
END_DATE    = os.environ.get("END_DATE",   "03/31/2026")


# Validation mode — restrict to one service for testing. Set to None for full run.
VALIDATION_SERVICE = None

# Services to skip
EXCLUSION_KEYWORDS = ["Trial", "Intro", "Demo", "Test", "Free", "Wordly Internal", "All"]

# Timing
RESULTS_LOAD_WAIT  = 10
POST_DOWNLOAD_WAIT = 5


# --- UTILITIES ---

def normalize_col(name):
    """Lowercase, replace non-alphanumeric runs with underscore, strip edges."""
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


def login(page, email, password):
    print("🔑 Logging in...")
    page.goto("https://portal.wordly.ai", wait_until="networkidle")
    nuke_alert(page, "login page")
    try:
        page.wait_for_selector("#portal-login-btn-signin-wordly", timeout=5000)
        page.click("#portal-login-btn-signin-wordly", force=True)
    except:
        pass
    page.wait_for_selector("#username", timeout=10000)
    page.fill("#username", email)
    page.fill("#password", password)
    page.click("#kc-login")
    page.wait_for_load_state("networkidle")
    time.sleep(3)
    print("  ✅ Logged in.")


def get_service_list(email, password):
    """Throwaway browser session just to read the service dropdown."""
    print("📋 Reading service list...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page    = context.new_page()
        login(page, email, password)

        page.goto("https://portal.wordly.ai/#/usageAll", wait_until="networkidle")
        page.wait_for_timeout(5000)
        nuke_alert(page, "usage page")
        page.wait_for_selector("#serviceFilter", timeout=20000)
        time.sleep(2)

        page.locator("#serviceFilter").click()
        page.wait_for_timeout(2000)

        options = page.locator(".p-dropdown-item")
        targets = []
        skipped = []

        for i in range(options.count()):
            name = options.nth(i).inner_text().strip()
            if any(kw.lower() in name.lower() for kw in EXCLUSION_KEYWORDS):
                skipped.append(name)
            else:
                targets.append(name)

        page.keyboard.press("Escape")
        browser.close()

    if VALIDATION_SERVICE:
        targets = [t for t in targets if t == VALIDATION_SERVICE]
        print(f"  🔬 VALIDATION MODE — restricted to: {targets}")
    else:
        print(f"  ✅ {len(targets)} services. Skipping {len(skipped)}: {skipped}")

    return targets


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


def scrape_one(email, password, service, date_str):
    """
    Fresh browser + login per iteration — only reliable way to get clean
    date fields. Portal holds field state in session.
    Returns pandas DataFrame or None.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()

        try:
            login(page, email, password)

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

            # Read into memory — no disk I/O needed
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
                browser.close()
            except:
                pass


def prepare_df(df, service, date_str):
    """
    Normalize columns, parse Account field, add metadata.
    Column names are always normalized by name not position.
    New columns from new services/custom fields flow through automatically.
    """
# Normalize all column names
    df.columns = [normalize_col(c) for c in df.columns]

    # Deduplicate column names — Wordly occasionally emits duplicate headers
    # Keep first occurrence, drop subsequent duplicates
    df = df.loc[:, ~df.columns.duplicated(keep='first')]

    # Convert all columns to string before BQ write — prevents type conflicts
    # when a column is null/empty on first write and has values on subsequent writes
    df = df.astype(str).replace('nan', None)

    # Parse account field — split "Name (id)" into separate columns
    # Defensive: if pattern doesn't match, name = raw value, id = None
    if "account" in df.columns:
        parsed = df["account"].str.extract(r'^(.*?)\s*\(([^)]+)\)\s*$')
        df["account_name"]      = parsed[0].where(parsed[0].notna(), df["account"])
        df["wordly_account_id"] = parsed[1]   # NaN if no match
        df.rename(columns={"account": "account_raw"}, inplace=True)

    # Metadata columns
    df["_service"]     = service
    df["_scrape_date"] = date_str
    df["_inserted_at"] = datetime.utcnow().isoformat()

    return df


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
    """
    Append rows to BQ. Schema auto-detected, new columns added automatically.
    """
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


def date_range(start_str, end_str):
    fmt = "%m/%d/%Y"
    current = datetime.strptime(start_str, fmt)
    end     = datetime.strptime(end_str,   fmt)
    while current <= end:
        yield current.strftime(fmt)
        current += timedelta(days=1)


# --- MAIN ---

def run():
    print(f"\n🚀 wordly_session_scraper v1.0.2-CLOUD — "
          f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"   Date range  : {START_DATE} → {END_DATE}")
    print(f"   BQ table    : {BQ_TABLE_REF}")
    print(f"   Headless    : {HEADLESS}")
    print(f"   Validation  : {VALIDATION_SERVICE or 'OFF — all services'}\n")

    email, password = load_creds(CREDS_FILE)

    ensure_bq_dataset()

    services = get_service_list(email, password)

    total_rows  = 0
    total_skips = 0
    fail_log    = []

    for service in services:
        print(f"\n{'='*60}")
        print(f"SERVICE: {service}")
        print(f"{'='*60}")

        for date_str in date_range(START_DATE, END_DATE):
            label = f"{service} / {date_str}"
            print(f"\n  📅 {label}")

            try:
                df = scrape_one(email, password, service, date_str)

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

    # --- SUMMARY ---
    print(f"\n{'='*60}")
    print(f"🏁 DONE — {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
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
            f"Check logs. {START_DATE}→{END_DATE}",
            is_error=True
        )
    else:
        send_slack(
            f"✅ Success: Daily Session Info captured. "
            f"{total_rows} rows | {total_skips} zero-skips | {START_DATE}→{END_DATE}",
            is_error=False
        )


if __name__ == "__main__":
    run()