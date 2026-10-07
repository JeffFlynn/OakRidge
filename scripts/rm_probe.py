"""Check the Rent Manager API connection and show what the bot would see.

    python -m scripts.rm_probe 3345550101

Needs RM_USERNAME and RM_PASSWORD (and RM_BASE_URL / RM_LOCATION_ID if not the
defaults). Read-only. If a call fails, compare the request in textbot/rentmanager.py
with the API's Help/TestClient page and adjust the constants at the top of that file.
"""

from __future__ import annotations

import asyncio
import json
import sys

from textbot.config import load_settings
from textbot.rentmanager import RentManagerClient


async def main(phone: str) -> None:
    s = load_settings()
    rm = RentManagerClient(s.rm_base_url, s.rm_username, s.rm_password, s.rm_location_id)
    if not rm.configured:
        sys.exit("Set RM_USERNAME and RM_PASSWORD first.")
    try:
        print(f"Logging in to {s.rm_base_url} ...")
        await rm._login()
        print("Login OK.\n")
        matches = await rm.find_tenants_by_phone(phone)
        print(f"Tenants with phone {phone}:")
        print(json.dumps(matches, indent=2, default=str))
        if matches and matches[0].get("tenant_id"):
            account = await rm.get_account(int(matches[0]["tenant_id"]))
            print("\nAccount for the first match:")
            print(json.dumps(account, indent=2, default=str))
    finally:
        await rm.aclose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
