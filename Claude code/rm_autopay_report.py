"""Download Rent Manager's "Tenant AutoPay Configuration" report to the same folder as this script.

Runs on YOUR computer (not in the cloud), opening real Chrome with Playwright.

Setup (once):
    pip install playwright
    playwright install chrome        # skip if Chrome is already installed

Set your details as environment variables (never hardcode the password):
    macOS/Linux:  export RM_URL="https://yourcompany.rentmanager.com"
                  export RM_USER="you"   RM_PASS="secret"
    Windows:      setx RM_URL "https://yourcompany.rentmanager.com"  (and RM_USER, RM_PASS)

Run:
    python rm_autopay_report.py

NOTE: Written without access to your Rent Manager screens, so the button and
menu names below are best guesses. If a step fails, the script saves a
screenshot next to the downloaded file and tells you which step broke. Tweak the
LABELS dict at the top to match what you see on screen.
"""

import os
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

REPORT_NAME = "Tenant AutoPay Configuration"
DESKTOP = Path(__file__).resolve().parent  # same folder as this script
TIMEOUT_MS = 60_000

# Adjust these if your Rent Manager uses different wording.
LABELS = {
    "username": r"user\s*name|user id|login",
    "password": r"password",
    "login_button": r"log\s*in|sign\s*in",
    "reports_menu": r"reports?",
    "all_properties": r"all properties",
    "run_button": r"run|view|generate|preview",
    "export_button": r"export|download|save",
    "export_format": r"excel|xlsx|csv",
}


def need(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing environment variable {name}. See the setup notes at the top of this file.")
    return value


def main() -> None:
    url, user, password = need("RM_URL"), need("RM_USER"), need("RM_PASS")
    DESKTOP.mkdir(parents=True, exist_ok=True)
    step = "starting"

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(TIMEOUT_MS)

        try:
            step = "opening Rent Manager"
            page.goto(url)

            step = "logging in"
            page.get_by_label(re.compile(LABELS["username"], re.I)).first.fill(user)
            page.get_by_label(re.compile(LABELS["password"], re.I)).first.fill(password)
            page.get_by_role("button", name=re.compile(LABELS["login_button"], re.I)).first.click()
            # If you have 2-factor or a company picker, finish it by hand now.
            page.wait_for_load_state("networkidle")

            step = f"opening the {REPORT_NAME} report"
            page.get_by_role("link", name=re.compile(LABELS["reports_menu"], re.I)).first.click()
            search = page.get_by_placeholder(re.compile("search", re.I))
            if search.count():
                search.first.fill(REPORT_NAME)
            page.get_by_text(REPORT_NAME, exact=False).first.click()

            step = "choosing all properties"
            all_props = page.get_by_text(re.compile(LABELS["all_properties"], re.I))
            if all_props.count():
                all_props.first.click()

            step = "generating the report"
            page.get_by_role("button", name=re.compile(LABELS["run_button"], re.I)).first.click()
            page.wait_for_load_state("networkidle")

            step = "downloading the file"
            fmt = page.get_by_text(re.compile(LABELS["export_format"], re.I))
            if fmt.count():
                fmt.first.click()
            with page.expect_download(timeout=TIMEOUT_MS * 2) as dl:
                page.get_by_role("button", name=re.compile(LABELS["export_button"], re.I)).first.click()
            download = dl.value

            stamp = datetime.now().strftime("%Y-%m-%d")
            suffix = Path(download.suggested_filename).suffix or ".xlsx"
            target = DESKTOP / f"Tenant AutoPay Configuration {stamp}{suffix}"
            download.save_as(target)
            print(f"Saved: {target}")

        except PWTimeout:
            shot = DESKTOP / "rm_autopay_error.png"
            page.screenshot(path=str(shot))
            print(f"Failed while {step}. Screenshot: {shot}")
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
