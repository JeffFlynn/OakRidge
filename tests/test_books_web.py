import asyncio
import base64
import hashlib
import hmac
import json

from fastapi.testclient import TestClient
from mcp import Client

from books.main import create_app
from books.tools import build_mcp

from tests.books_helpers import (
    ACCOUNTS_RESPONSE,
    MONTHS,
    REALM,
    REALM2,
    connect,
    make_books,
    make_settings,
    pl_report,
    steady_accounts,
)


def run(coro):
    return asyncio.run(coro)


def synced_books():
    books, qbo, slack, analyst = make_books()
    for realm, name, rent in ((REALM, "Pine Ridge Estates LLC", 20000.0), (REALM2, "Meadow Ridge MHP LLC", 9000.0)):
        connect(books.store, realm, name)
        qbo.pl[realm] = pl_report(steady_accounts({("Income", "Lot Rent"): {m: rent for m in MONTHS}}), MONTHS)
        qbo.accounts[realm] = ACCOUNTS_RESPONSE
        qbo.names[realm] = name
        run(books.sync(realm))
    return books, qbo


def client_for(books, **settings):
    books.qbo.configured = False  # keep the background scheduler out of web tests
    return TestClient(create_app(make_settings(**settings), books))


def sign(body: bytes) -> str:
    return base64.b64encode(hmac.new(b"verifier", body, hashlib.sha256).digest()).decode()


def test_intuit_webhook_requires_signature_and_marks_company_dirty():
    books, _ = synced_books()
    client = client_for(books)
    body = json.dumps({"eventNotifications": [{"realmId": REALM}]}).encode()
    assert client.post("/webhooks/intuit", content=body, headers={"intuit-signature": "bad"}).status_code == 401
    assert books.store.get_company(REALM).dirty_since is None
    resp = client.post("/webhooks/intuit", content=body, headers={"intuit-signature": sign(body)})
    assert resp.status_code == 200
    assert books.store.get_company(REALM).dirty_since is not None


def test_connect_needs_the_key_and_full_setup():
    books, _ = synced_books()
    client = client_for(books)
    assert client.get("/books/connect").status_code == 403
    assert client.get("/books/connect?key=wrong").status_code == 403
    assert client.get("/books/connect?key=k3y").status_code == 500  # no Intuit app configured


def test_callback_rejects_unknown_state():
    books, _ = synced_books()
    client = client_for(books)
    resp = client.get(f"/books/callback?code=abc&state=forged&realmId={REALM}")
    assert resp.status_code == 400


def test_mcp_endpoint_requires_key():
    books, _ = synced_books()
    with client_for(books) as client:
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
        }
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        assert client.post("/mcp", json=init, headers=headers).status_code == 401
        assert client.post("/mcp?key=nope", json=init, headers=headers).status_code == 401
        ok = client.post("/mcp", json=init, headers={**headers, "Authorization": "Bearer k3y"})
        assert ok.status_code == 200, ok.text
        assert client.post("/mcp?key=k3y", json=init, headers=headers).status_code == 200
        assert client.get("/health").json()["companies"] == 2


async def call(books, tool, **args):
    async with Client(build_mcp(books)) as client:
        result = await client.call_tool(tool, args)
        return result


def text_of(result) -> str:
    return result.content[0].text


def test_mcp_tools_list():
    books, _ = synced_books()

    async def names():
        async with Client(build_mcp(books)) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert run(names()) == {
        "list_entities", "portfolio_scorecard", "entity_monthly", "entity_pl_detail", "compare_account",
        "cash_balances", "open_alerts", "qbo_query", "qbo_report", "refresh_entity",
    }


def test_mcp_scorecard_ranks_entities_by_noi():
    books, _ = synced_books()
    data = json.loads(text_of(run(call(books, "portfolio_scorecard"))))
    assert [e["entity"] for e in data["entities"]] == ["Pine Ridge Estates LLC", "Meadow Ridge MHP LLC"]
    assert data["portfolio"]["income"] == 29000


def test_mcp_compare_account_across_entities():
    books, _ = synced_books()
    data = json.loads(text_of(run(call(books, "compare_account", account_contains="water"))))
    assert [e["total"] for e in data["entities"]] == [15000, 15000]  # May-Sept in the sample
    meadow = next(e for e in data["entities"] if e["entity"].startswith("Meadow"))
    assert meadow["pct_of_income"] == round(15000 / 45000, 4)


def test_mcp_entity_detail_and_live_query():
    books, qbo = synced_books()
    detail = json.loads(text_of(run(call(books, "entity_pl_detail", entity="meadow", start_month="2026-09", end_month="2026-09"))))
    assert {"section": "Income", "account": "Lot Rent", "2026-09": 9000.0} in detail["accounts"]
    run(call(books, "qbo_query", entity="pine", query="SELECT * FROM Purchase"))
    assert qbo.calls[-1] == ("query", REALM, "SELECT * FROM Purchase")


def test_mcp_unknown_entity_is_a_tool_error():
    books, _ = synced_books()
    result = run(call(books, "entity_monthly", entity="ridge"))
    assert result.is_error
    assert "matches several entities" in text_of(result)


def test_mcp_bad_report_params_are_explained():
    books, _ = synced_books()
    result = run(call(books, "qbo_report", entity="pine", report="ProfitAndLoss", params_json="[1]"))
    assert result.is_error and "JSON object" in text_of(result)
