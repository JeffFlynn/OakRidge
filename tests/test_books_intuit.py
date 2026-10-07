import asyncio
import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import httpx2 as httpx
import pytest

from books.intuit import QBOError, QuickBooks, ReconnectNeeded, verify_webhook, webhook_realms
from books.store import NEEDS_RECONNECT, Store

from tests.books_helpers import REALM, connect


def run(coro):
    return asyncio.run(coro)


def sign(body: bytes, token: str = "verifier") -> str:
    return base64.b64encode(hmac.new(token.encode(), body, hashlib.sha256).digest()).decode()


def test_verify_webhook():
    body = b'{"eventNotifications": []}'
    assert verify_webhook("verifier", body, sign(body))
    assert not verify_webhook("verifier", body, sign(body, "other"))
    assert not verify_webhook("", body, sign(body, ""))
    assert not verify_webhook("verifier", body, "")


def test_webhook_realms_handles_both_formats():
    legacy = {"eventNotifications": [{"realmId": "1", "dataChangeEvent": {"entities": []}}, {"realmId": "2"}]}
    cloudevents = [
        {"specversion": "1.0", "type": "qbo.purchase.created.v1", "intuitaccountid": "3"},
        {"specversion": "1.0", "type": "qbo.bill.updated.v1", "intuitaccountid": "3"},
    ]
    assert webhook_realms(legacy) == {"1", "2"}
    assert webhook_realms(cloudevents) == {"3"}
    assert webhook_realms({"intuitaccountid": "4"}) == {"4"}
    assert webhook_realms("junk") == set()


class Intuit:
    """A fake Intuit API behind httpx's MockTransport."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.valid_access = "acc"
        self.refresh_ok = True
        self.refreshes = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/tokens/bearer"):
            if not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.refreshes += 1
            self.valid_access = f"acc-{self.refreshes}"
            return httpx.Response(
                200,
                json={
                    "access_token": self.valid_access,
                    "expires_in": 3600,
                    "refresh_token": f"ref-{self.refreshes}",
                    "x_refresh_token_expires_in": 8640000,
                },
            )
        if request.headers["Authorization"] != f"Bearer {self.valid_access}":
            return httpx.Response(401, json={"fault": "auth"})
        assert request.method == "GET"
        return httpx.Response(200, json={"QueryResponse": {"Account": []}, "CompanyInfo": {"CompanyName": "Pine"}})


def make_client(**connect_kw):
    store = Store(":memory:")
    connect(store, **connect_kw)
    intuit = Intuit()
    http = httpx.AsyncClient(transport=httpx.MockTransport(intuit.handler))
    return QuickBooks(store, "id", "secret", "https://x/books/callback", "production", http=http), store, intuit


def test_uses_cached_token_when_fresh():
    qbo, _, intuit = make_client(access_exp=datetime.now(timezone.utc) + timedelta(hours=1))
    run(qbo.query(REALM, "SELECT * FROM Account"))
    assert intuit.refreshes == 0
    assert "MAXRESULTS 1000" in intuit.requests[0].url.params["query"]
    assert intuit.requests[0].url.params["minorversion"] == "75"


def test_refreshes_expired_token_and_keeps_rotated_refresh_token():
    qbo, store, intuit = make_client(access_exp=datetime.now(timezone.utc) - timedelta(minutes=1))
    run(qbo.query(REALM, "SELECT * FROM Account"))
    assert intuit.refreshes == 1
    company = store.get_company(REALM)
    assert company.access_token == "acc-1" and company.refresh_token == "ref-1"


def test_401_triggers_one_refresh_and_retry():
    qbo, _, intuit = make_client(access_exp=datetime.now(timezone.utc) + timedelta(hours=1))
    intuit.valid_access = "something-else"  # token revoked early
    run(qbo.company_name(REALM))
    assert intuit.refreshes == 1


def test_invalid_grant_marks_company_for_reconnect():
    qbo, store, intuit = make_client(access_exp=datetime.now(timezone.utc) - timedelta(minutes=1))
    intuit.refresh_ok = False
    with pytest.raises(ReconnectNeeded):
        run(qbo.query(REALM, "SELECT * FROM Account"))
    assert store.get_company(REALM).status == NEEDS_RECONNECT
    with pytest.raises(ReconnectNeeded):  # and stops calling Intuit after that
        run(qbo.query(REALM, "SELECT * FROM Account"))
    assert len(intuit.requests) == 1


@pytest.mark.parametrize("sql", ["DELETE FROM Bill", "select * from Bill; select * from Vendor", "update x"])
def test_query_rejects_anything_but_one_select(sql):
    qbo, _, intuit = make_client()
    with pytest.raises(QBOError):
        run(qbo.query(REALM, sql))
    assert intuit.requests == []


def test_report_allowlist():
    qbo, _, _ = make_client()
    with pytest.raises(QBOError):
        run(qbo.report(REALM, "Invoice"))


def test_tokens_are_encrypted_at_rest():
    from cryptography.fernet import Fernet

    store = Store(":memory:", Fernet.generate_key().decode())
    connect(store, refresh="super-secret-refresh")
    raw = store._db.execute("SELECT refresh_token FROM companies").fetchone()[0]
    assert "super-secret" not in raw
    assert store.get_company(REALM).refresh_token == "super-secret-refresh"
