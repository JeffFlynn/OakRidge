"""Log into WaterScope (waterscope.us) and confirm the dashboard loads.

Meant to run nightly at 2 am from cron on your own computer.

Credentials come from environment variables only; nothing is stored in this repo:
    WATERSCOPE_USER      login email (ndremhp@gmail.com)
    WATERSCOPE_PASSWORD  password

Setup:
    pip install playwright && playwright install chromium

Cron (crontab -e), 2 am every night:
    0 2 * * * WATERSCOPE_USER=... WATERSCOPE_PASSWORD=... /usr/bin/python3 /path/to/scripts/waterscope_login.py >> ~/waterscope_login.log 2>&1
Or keep the two variables in a chmod 600 file and `source` it in the cron line.

Exit code 0 = logged in, 1 = failed. On failure a screenshot is saved next to the log.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

URL = "https://waterscope.us/Home/Main"
# The left nav shows these once logged in (per the water-check skill).
LOGGED_IN_MARKER = "ACCOUNT LOOKUP"


def main() -> int:
    user = os.environ.get("WATERSCOPE_USER")
    password = os.environ.get("WATERSCOPE_PASSWORD")
    if not user or not password:
        print(f"{datetime.now():%F %T} ERROR set WATERSCOPE_USER and WATERSCOPE_PASSWORD")
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
            pw_field = page.locator("input[type=password]").first
            if pw_field.count():
                # Selectors are generic; adjust if the login form differs.
                page.locator(
                    "input[type=email], input[type=text], input[name*=user i], input[id*=user i]"
                ).first.fill(user)
                pw_field.fill(password)
                page.keyboard.press("Enter")
            page.get_by_text(LOGGED_IN_MARKER, exact=False).first.wait_for(timeout=60_000)
            print(f"{datetime.now():%F %T} OK logged in")
            return 0
        except PWTimeout:
            shot = Path.home() / f"waterscope_fail_{datetime.now():%Y%m%d_%H%M}.png"
            page.screenshot(path=str(shot))
            print(f"{datetime.now():%F %T} FAIL dashboard not reached, screenshot {shot}")
            return 1
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
