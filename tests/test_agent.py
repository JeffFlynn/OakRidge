import asyncio
import json
from types import SimpleNamespace

from textbot.agent import ReplyAgent, parse_decision
from textbot.models import ThreadMessage

from tests.conftest import NOON_CT, TENANT


class FakeLookup:
    configured = True

    def __init__(self):
        self.calls = []

    async def find_tenants_by_phone(self, phone):
        self.calls.append(("find", phone))
        return [{"tenant_id": 7, "name": "Pat Doe", "property": "Oak", "unit": "12", "balance": 0}]

    async def get_account(self, tenant_id):
        self.calls.append(("account", tenant_id))
        return {"tenant_id": tenant_id, "balance": 0, "recent_transactions": []}


class FakeMessages:
    """Returns a tool call first, then a final JSON decision."""

    def __init__(self, final: dict, stop_reason: str = "end_turn"):
        self.final = final
        self.final_stop = stop_reason
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot
        if len(self.requests) == 1:
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[SimpleNamespace(type="tool_use", id="tu1", name="find_tenant_by_phone", input={"phone": TENANT})],
            )
        return SimpleNamespace(
            stop_reason=self.final_stop,
            content=[SimpleNamespace(type="text", text=json.dumps(self.final))],
        )


DECISION = {
    "category": "billing",
    "action": "send",
    "reply": "We can see the $425 came through. Thank you!",
    "reason": "Payment posted on the ledger.",
    "tenant_verified": True,
    "all_facts_verified": True,
    "creates_new_commitment": False,
    "sensitive_topic": False,
    "lookups_done": ["Rent Manager tenant lookup"],
    "needs_from_jeff": "",
}


def make_agent(messages, lookup=None):
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return ReplyAgent(client, lookup or FakeLookup(), "claude-opus-5-5", "medium", "America/Chicago")


def thread():
    return [ThreadMessage("m1", "Inbound", "I paid my rent this morning", NOON_CT)]


def test_agent_runs_tool_then_returns_decision():
    messages = FakeMessages(DECISION)
    lookup = FakeLookup()
    decision = asyncio.run(make_agent(messages, lookup).decide(thread(), TENANT, NOON_CT))
    assert decision.action == "send" and decision.category == "billing"
    assert lookup.calls == [("find", TENANT)]
    # The tool result goes back in the next request, after the assistant's tool call.
    second = messages.requests[1]["messages"]
    assert second[-1]["content"][0]["type"] == "tool_result"
    assert second[-1]["content"][0]["tool_use_id"] == "tu1"
    # Every request carries the cached rules and the JSON schema.
    assert messages.requests[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert messages.requests[0]["output_config"]["format"]["type"] == "json_schema"


def test_refusal_becomes_hold():
    decision = asyncio.run(make_agent(FakeMessages(DECISION, "refusal")).decide(thread(), TENANT, NOON_CT))
    assert decision.action == "hold"


def test_unconfigured_rent_manager_reports_error_to_claude():
    lookup = FakeLookup()
    lookup.configured = False
    messages = FakeMessages(DECISION)
    asyncio.run(make_agent(messages, lookup).decide(thread(), TENANT, NOON_CT))
    result = messages.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and lookup.calls == []


def test_parse_decision_rejects_garbage():
    assert parse_decision("not json").action == "hold"
    assert parse_decision(json.dumps({**DECISION, "action": "explode"})).action == "hold"
