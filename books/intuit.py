"""QuickBooks Online: OAuth 2.0 for many companies, read-only API calls, webhook checks.

Intuit has no read-only scope, so this client is read-only by construction: it only
issues GET requests (queries, reports, company info). There is no code path that writes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx2 as httpx

from books.store import ACTIVE, Store

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SCOPE = "com.intuit.quickbooks.accounting"
API_BASE = {
    "production": "https://quickbooks.api.intuit.com",
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
}
MINOR_VERSION = "75"
REFRESH_MARGIN = timedelta(minutes=5)
MAX_QUERY_RESULTS = 1000

# Reports Claude may pull live. All are read-only GETs.
REPORTS = frozenset(
    {
        "ProfitAndLoss",
        "ProfitAndLossDetail",
        "BalanceSheet",
        "CashFlow",
        "GeneralLedger",
        "TransactionList",
        "TrialBalance",
        "AgedPayables",
        "AgedPayableDetail",
        "AgedReceivables",
        "AgedReceivableDetail",
        "VendorExpenses",
        "CustomerIncome",
        "AccountList",
    }
)
SELECT_RE = re.compile(r"^\s*select\s", re.IGNORECASE)


class QBOError(RuntimeError):
    pass


class ReconnectNeeded(QBOError):
    """The company's refresh token no longer works; someone has to reconnect it."""


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": SCOPE,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )


def verify_webhook(verifier_token: str, body: bytes, signature: str) -> bool:
    """Intuit signs each webhook: base64(HMAC-SHA256(verifier token, raw body))."""
    if not (verifier_token and signature):
        return False
    expected = base64.b64encode(
        hmac.new(verifier_token.encode(), body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


def webhook_realms(payload: object) -> set[str]:
    """Company ids named in a webhook. Handles the legacy eventNotifications format and the
    newer CloudEvents format (a list of events with intuitaccountid)."""
    realms: set[str] = set()
    if isinstance(payload, dict):
        for note in payload.get("eventNotifications") or []:
            if note.get("realmId"):
                realms.add(str(note["realmId"]))
        payload = [payload] if "intuitaccountid" in payload else []
    if isinstance(payload, list):
        for event in payload:
            if isinstance(event, dict) and event.get("intuitaccountid"):
                realms.add(str(event["intuitaccountid"]))
    return realms


class QuickBooks:
    def __init__(
        self,
        store: Store,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        environment: str = "production",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._store = store
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._base = API_BASE[environment]
        self._http = http or httpx.AsyncClient(timeout=60.0)
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    # --- OAuth ---

    async def _token_request(self, data: dict) -> dict:
        resp = await self._http.post(
            TOKEN_URL,
            data=data,
            auth=(self._client_id, self._client_secret),
            headers={"Accept": "application/json"},
        )
        if resp.status_code == 400 and "invalid_grant" in resp.text:
            raise ReconnectNeeded("Intuit rejected the refresh token (invalid_grant)")
        if resp.status_code >= 400:
            raise QBOError(f"Intuit token request failed: {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def _save(self, realm_id: str, tokens: dict, name: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        self._store.save_tokens(
            realm_id,
            name,
            tokens["access_token"],
            now + timedelta(seconds=int(tokens.get("expires_in", 3600))),
            tokens["refresh_token"],
            now + timedelta(seconds=int(tokens.get("x_refresh_token_expires_in", 8_640_000))),
        )

    async def exchange_code(self, code: str, realm_id: str) -> str:
        """Finish the connect flow for one company. Returns the company name."""
        tokens = await self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self._redirect_uri}
        )
        self._save(realm_id, tokens)
        name = await self.company_name(realm_id)
        self._store.set_name(realm_id, name)
        return name

    async def _access_token(self, realm_id: str, force_refresh: bool = False) -> str:
        lock = self._locks.setdefault(realm_id, asyncio.Lock())
        async with lock:
            company = self._store.get_company(realm_id)
            if company is None:
                raise QBOError(f"Company {realm_id} is not connected")
            if company.status != ACTIVE:
                raise ReconnectNeeded(f"{company.name} needs to be reconnected")
            fresh = company.access_expires_at - REFRESH_MARGIN > datetime.now(timezone.utc)
            if fresh and not force_refresh:
                return company.access_token
            try:
                tokens = await self._token_request(
                    {"grant_type": "refresh_token", "refresh_token": company.refresh_token}
                )
            except ReconnectNeeded as exc:
                self._store.mark_needs_reconnect(realm_id, str(exc))
                raise
            # Intuit may rotate the refresh token; always keep the newest one.
            self._save(realm_id, tokens)
            return tokens["access_token"]

    # --- API reads ---

    async def _get(self, realm_id: str, path: str, params: dict | None = None) -> dict:
        url = f"{self._base}/v3/company/{realm_id}/{path}"
        query = {**(params or {}), "minorversion": MINOR_VERSION}
        for attempt in range(2):
            token = await self._access_token(realm_id, force_refresh=attempt > 0)
            resp = await self._http.get(
                url,
                params=query,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            if resp.status_code == 401 and attempt == 0:
                continue  # access token revoked early; refresh once and retry
            if resp.status_code >= 400:
                raise QBOError(f"QuickBooks {path} failed: {resp.status_code} {resp.text[:500]}")
            return resp.json()
        raise QBOError(f"QuickBooks {path} kept returning 401")

    async def company_name(self, realm_id: str) -> str:
        data = await self._get(realm_id, f"companyinfo/{realm_id}")
        info = data.get("CompanyInfo") or {}
        return info.get("CompanyName") or info.get("LegalName") or realm_id

    async def report(self, realm_id: str, name: str, params: dict | None = None) -> dict:
        if name not in REPORTS:
            raise QBOError(f"Report {name} isn't allowed. Choose one of: {', '.join(sorted(REPORTS))}")
        return await self._get(realm_id, f"reports/{name}", params)

    async def query(self, realm_id: str, sql: str) -> dict:
        """Run a QuickBooks query (their SQL-like language). SELECT only."""
        if not SELECT_RE.match(sql) or ";" in sql:
            raise QBOError("Only a single SELECT query is allowed")
        if not re.search(r"\bmaxresults\b", sql, re.IGNORECASE):
            sql = f"{sql.strip()} MAXRESULTS {MAX_QUERY_RESULTS}"
        data = await self._get(realm_id, "query", {"query": sql})
        return data.get("QueryResponse") or {}

    async def aclose(self) -> None:
        await self._http.aclose()
