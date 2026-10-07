from datetime import datetime, timezone

from textbot.phone import normalize_phone
from textbot.policy import PolicyContext, evaluate

from tests.conftest import NOON_CT, TENANT, make_settings, safe_decision

LIVE = dict(shadow_mode=False, auto_send_categories=frozenset({"maintenance", "billing"}))


def ctx(**overrides) -> PolicyContext:
    base = dict(tenant_phone=TENANT, now=NOON_CT, auto_sends_today=0, paused=False)
    base.update(overrides)
    return PolicyContext(**base)


def test_safe_reply_auto_sends_when_live_and_category_allowed():
    result = evaluate(safe_decision(), ctx(), make_settings(**LIVE))
    assert result.auto_send, result.blockers


def test_shadow_mode_blocks_everything():
    result = evaluate(safe_decision(), ctx(), make_settings(auto_send_categories=frozenset({"maintenance"})))
    assert not result.auto_send
    assert "Shadow mode is on" in result.blockers


def test_test_number_bypasses_shadow_and_category_but_not_safety_checks():
    settings = make_settings(test_numbers=frozenset({TENANT}))
    assert evaluate(safe_decision(), ctx(), settings).auto_send
    risky = safe_decision(sensitive_topic=True)
    assert not evaluate(risky, ctx(), settings).auto_send


def test_category_not_on_allowlist_is_held():
    result = evaluate(safe_decision(category="lease"), ctx(), make_settings(**LIVE))
    assert not result.auto_send


def test_each_safety_flag_blocks():
    settings = make_settings(**LIVE)
    for change in (
        dict(action="hold"),
        dict(all_facts_verified=False),
        dict(creates_new_commitment=True),
        dict(sensitive_topic=True),
        dict(category="billing", tenant_verified=False),
        dict(reply=""),
        dict(reply="x" * 1000),
        dict(reply="Pay here: https://evil.example.com/pay"),
    ):
        assert not evaluate(safe_decision(**change), ctx(), settings).auto_send, change


def test_always_human_categories_even_if_allowlisted():
    settings = make_settings(shadow_mode=False, auto_send_categories=frozenset({"ada", "tenant_drama"}))
    for category in ("ada", "tenant_drama"):
        assert not evaluate(safe_decision(category=category), ctx(), settings).auto_send


def test_quiet_hours_cap_pause_and_staff():
    settings = make_settings(**LIVE)
    late_night = datetime(2026, 10, 8, 4, 0, tzinfo=timezone.utc)  # 11pm Central
    assert not evaluate(safe_decision(), ctx(now=late_night), settings).auto_send
    assert not evaluate(safe_decision(), ctx(auto_sends_today=3), settings).auto_send
    assert not evaluate(safe_decision(), ctx(paused=True), settings).auto_send
    staff = make_settings(**LIVE, staff_numbers=frozenset({TENANT}))
    assert not evaluate(safe_decision(), ctx(), staff).auto_send


def test_normalize_phone():
    assert normalize_phone("+1 (334) 555-0101") == "3345550101"
    assert normalize_phone("334.555.0101") == "3345550101"
    assert normalize_phone("555-0101") == ""
