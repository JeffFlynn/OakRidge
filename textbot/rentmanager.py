"""Rent Manager 12 Web API: read-only tenant lookups.

VERIFY BEFORE GOING LIVE: the endpoint paths, filter syntax and field names
below follow Rent Manager's REST conventions (/Tenants with filters= and
embeds=), but haven't been checked against the ndrellc API yet. Run
`python scripts/rm_probe.py <phone>` once credentials are set, compare the
output with the API's Help/TestClient page, and adjust the constants here.
Everything Rent Manager-specific lives in this file.
"""

from __future__ import annotations

from typing import Any

import httpx2 as httpx

from textbot.phone import normalize_phone, phone_variants

AUTH_PATH = "/Authentication/AuthorizeUser"
TOKEN_HEADER = "X-RM12Api-ApiToken"
TENANTS_PATH = "/Tenants"
PHONE_FILTER_FIELD = "Contacts.PhoneNumbers.PhoneNumber"
TENANT_EMBEDS = "Contacts.PhoneNumbers,Property,Leases.Unit,Balance"
TRANSACTIONS_PATH = "/Tenants/{tenant_id}/Transactions"
MAX_TRANSACTIONS = 25


class RentManagerError(RuntimeError):
    pass


class RentManagerClient:
    def __init__(self, base_url: str, username: str, password: str, location_id: int) -> None:
        self._username = username
        self._password = password
        self._location_id = location_id
        self._token = ""
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30.0)

    @property
    def configured(self) -> bool:
        return bool(self._username and self._password)

    async def _login(self) -> None:
        resp = await self._http.post(
            AUTH_PATH,
            json={
                "Username": self._username,
                "Password": self._password,
                "LocationID": self._location_id,
            },
        )
        if resp.status_code >= 400:
            raise RentManagerError(f"Rent Manager login failed ({resp.status_code})")
        token = resp.json()
        self._token = token if isinstance(token, str) else str(token.get("Token", ""))

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self._token:
            await self._login()
        for attempt in range(2):
            resp = await self._http.get(path, params=params, headers={TOKEN_HEADER: self._token})
            if resp.status_code == 401 and attempt == 0:
                await self._login()
                continue
            if resp.status_code >= 400:
                raise RentManagerError(f"Rent Manager GET {path} failed ({resp.status_code})")
            return resp.json()
        raise RentManagerError("Rent Manager authorization failed")

    async def find_tenants_by_phone(self, phone: str) -> list[dict]:
        target = normalize_phone(phone)
        for variant in phone_variants(phone):
            rows = await self._get(
                TENANTS_PATH,
                {"filters": f"{PHONE_FILTER_FIELD},eq,{variant}", "embeds": TENANT_EMBEDS},
            )
            matches = [summarize_tenant(r) for r in rows or []]
            matches = [m for m in matches if target in m["phones"]] or matches
            if matches:
                return matches
        return []

    async def get_account(self, tenant_id: int) -> dict:
        rows = await self._get(
            TENANTS_PATH, {"filters": f"TenantID,eq,{tenant_id}", "embeds": TENANT_EMBEDS}
        )
        if not rows:
            raise RentManagerError(f"Tenant {tenant_id} not found")
        account = summarize_tenant(rows[0])
        transactions = await self._get(
            TRANSACTIONS_PATH.format(tenant_id=tenant_id),
            {"orderby": "TransactionDate desc", "pagesize": MAX_TRANSACTIONS},
        )
        account["recent_transactions"] = [summarize_transaction(t) for t in transactions or []]
        return account

    async def aclose(self) -> None:
        await self._http.aclose()


def summarize_tenant(row: dict) -> dict:
    phones = sorted(
        {
            normalize_phone(p.get("PhoneNumber"))
            for c in row.get("Contacts") or []
            for p in c.get("PhoneNumbers") or []
        }
        - {""}
    )
    leases = row.get("Leases") or []
    unit = ((leases[0].get("Unit") or {}).get("Name")) if leases else None
    balance = row.get("Balance")
    if isinstance(balance, dict):
        balance = balance.get("Balance", balance.get("TotalBalance"))
    return {
        "tenant_id": row.get("TenantID"),
        "name": row.get("Name") or " ".join(filter(None, [row.get("FirstName"), row.get("LastName")])),
        "status": row.get("Status"),
        "property": (row.get("Property") or {}).get("Name"),
        "unit": unit,
        "balance": balance,
        "phones": phones,
    }


def summarize_transaction(row: dict) -> dict:
    return {
        "date": row.get("TransactionDate"),
        "type": row.get("TransactionType"),
        "description": row.get("Comment") or row.get("Description") or row.get("ChargeTypeName"),
        "amount": row.get("Amount"),
    }
