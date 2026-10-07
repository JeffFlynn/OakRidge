import asyncio

from textbot import store as st

from tests.conftest import PARK_LINE, TENANT, inbound, make_settings, safe_decision

JEFF_CELL = "5403334014"


def run(coro):
    return asyncio.run(coro)


def test_shadow_mode_waits_in_inbox_and_sends_nothing(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound()))
    assert pipe.rc.sent == []
    draft = pipe.store.get_draft(1)
    assert draft.status == st.PENDING
    assert "Shadow mode is on" in draft.blockers
    assert draft.thread[-1]["text"] == "my sink is leaking"


def test_auto_send_when_policy_allows(make_pipeline):
    settings = make_settings(shadow_mode=False, auto_send_categories=frozenset({"maintenance"}))
    pipe = make_pipeline(settings)
    run(pipe.handle_inbound(inbound()))
    assert pipe.rc.sent == [(PARK_LINE, TENANT, safe_decision().reply)]
    assert pipe.store.get_draft(1).status == st.AUTO_SENT


def test_settings_page_overrides_env_defaults(make_pipeline):
    pipe = make_pipeline()  # env says shadow mode on, nothing auto
    pipe.store.set_setting("shadow_mode", "0")
    pipe.store.set_setting("auto_send_categories", "maintenance")
    run(pipe.handle_inbound(inbound()))
    assert len(pipe.rc.sent) == 1


def test_duplicate_notification_is_ignored(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound()))
    run(pipe.handle_inbound(inbound()))
    assert pipe.agent.calls == 1


def test_burst_of_texts_only_answers_latest(make_pipeline):
    pipe = make_pipeline()
    # The second text arrives while the first is still waiting out the debounce.
    pipe.store.record_inbound("m2", "c1", TENANT, PARK_LINE, "also the toilet", "2026-10-07T19:00:30+00:00")
    run(pipe.handle_inbound(inbound("m1")))
    assert pipe.agent.calls == 0


def test_newer_text_supersedes_pending_draft(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound("m1")))
    run(pipe.handle_inbound(inbound("m2", "hello?")))
    assert pipe.store.get_draft(1).status == st.SUPERSEDED
    assert pipe.store.get_draft(2).status == st.PENDING
    assert [d.id for d in pipe.store.pending_drafts()] == [2]


def test_approve_sends_once_even_with_double_tap(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound()))
    assert run(pipe.approve(1, None, "jeff")) == "Sent."
    assert run(pipe.approve(1, None, "jeff")).startswith("Already handled")
    assert len(pipe.rc.sent) == 1
    draft = pipe.store.get_draft(1)
    assert draft.status == st.SENT and draft.decided_by == "jeff"


def test_edited_reply_is_what_gets_sent(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound()))
    run(pipe.approve(1, "We'll have someone look today.", "jeff"))
    assert pipe.rc.sent[0][2] == "We'll have someone look today."
    assert pipe.store.get_draft(1).final_text == "We'll have someone look today."


def test_empty_edit_sends_nothing_and_stays_pending(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound()))
    assert run(pipe.approve(1, "   ", "jeff")) == "Nothing to send."
    assert pipe.store.get_draft(1).status == st.PENDING


def test_failed_send_leaves_draft_pending_and_logs(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound()))
    pipe.rc.fail_send = True
    assert run(pipe.approve(1, None, "jeff")).startswith("Sending failed")
    assert pipe.store.get_draft(1).status == st.PENDING
    assert pipe.store.recent_events()[0]["level"] == "error"


def test_failed_auto_send_falls_back_to_inbox(make_pipeline):
    settings = make_settings(shadow_mode=False, auto_send_categories=frozenset({"maintenance"}))
    pipe = make_pipeline(settings)
    pipe.rc.fail_send = True
    run(pipe.handle_inbound(inbound()))
    assert pipe.store.get_draft(1).status == st.PENDING
    assert pipe.store.auto_sends_since(TENANT, pipe._clock().replace(hour=0)) == 0


def test_skip(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound()))
    assert run(pipe.skip(1, "jeff")) == "Skipped."
    assert run(pipe.approve(1, None, "jeff")).startswith("Already handled")
    assert pipe.rc.sent == []


def test_reply_from_ringcentral_retires_pending_draft(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound(conv="c1")))
    run(pipe.handle_outbound("c1"))
    assert pipe.store.get_draft(1).status == st.SUPERSEDED


def test_agent_error_is_logged_and_alerted(make_pipeline):
    settings = make_settings(alert_numbers=frozenset({JEFF_CELL}), alert_from_number=PARK_LINE)
    pipe = make_pipeline(settings)
    pipe.agent.error = RuntimeError("boom")
    run(pipe.handle_inbound(inbound(text="where do I pay")))
    assert "Couldn't process a text" in pipe.store.recent_events()[0]["message"]
    assert pipe.rc.sent[0][1] == JEFF_CELL


def test_staff_numbers_are_ignored(make_pipeline):
    pipe = make_pipeline(make_settings(staff_numbers=frozenset({TENANT})))
    run(pipe.handle_inbound(inbound()))
    assert pipe.agent.calls == 0


def test_alert_texts_jeff_once_per_cooldown(make_pipeline):
    settings = make_settings(alert_numbers=frozenset({JEFF_CELL}), alert_from_number=PARK_LINE)
    pipe = make_pipeline(settings)
    run(pipe.handle_inbound(inbound("m1", conv="c1")))
    run(pipe.handle_inbound(inbound("m2", conv="c2")))
    alerts = [s for s in pipe.rc.sent if s[1] == JEFF_CELL]
    assert len(alerts) == 1
    assert "1 text needs your approval" in alerts[0][2]


def test_flagged_text_alerts_even_during_cooldown(make_pipeline):
    settings = make_settings(alert_numbers=frozenset({JEFF_CELL}), alert_from_number=PARK_LINE)
    pipe = make_pipeline(settings)
    run(pipe.handle_inbound(inbound("m1", conv="c1")))
    pipe.agent.decision = safe_decision(action="flag", reply="")
    run(pipe.handle_inbound(inbound("m2", conv="c2")))
    alerts = [s for s in pipe.rc.sent if s[1] == JEFF_CELL]
    assert len(alerts) == 2 and alerts[1][2].startswith("Text bot: FLAGGED")


def test_pause_by_text_from_jeffs_cell(make_pipeline):
    settings = make_settings(alert_numbers=frozenset({JEFF_CELL}), alert_from_number=PARK_LINE)
    pipe = make_pipeline(settings)
    msg = inbound(text="pause bot")
    msg = msg.__class__(**{**msg.__dict__, "tenant_phone": JEFF_CELL})
    run(pipe.handle_inbound(msg))
    assert pipe.store.is_paused()
    assert pipe.agent.calls == 0
    assert "paused" in pipe.rc.sent[0][2]


def test_no_reply_still_waits_in_inbox(make_pipeline):
    pipe = make_pipeline(decision=safe_decision(action="no_reply", category="closed", reply=""))
    run(pipe.handle_inbound(inbound(text="thank you!")))
    assert pipe.store.get_draft(1).status == st.PENDING
