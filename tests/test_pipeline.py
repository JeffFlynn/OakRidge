import asyncio

from textbot import store as st

from tests.conftest import PARK_LINE, TENANT, inbound, make_settings, safe_decision


def run(coro):
    return asyncio.run(coro)


def test_shadow_mode_posts_card_and_sends_nothing(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound()))
    assert pipe.rc.sent == []
    assert len(pipe.slack.posts) == 1
    draft = pipe.store.get_draft(1)
    assert draft.status == st.PENDING
    assert "Shadow mode is on" in draft.blockers
    assert draft.slack_ts == "ts-1"


def test_auto_send_when_policy_allows(make_pipeline):
    settings = make_settings(shadow_mode=False, auto_send_categories=frozenset({"maintenance"}))
    pipe = make_pipeline(settings)
    run(pipe.handle_inbound(inbound()))
    assert pipe.rc.sent == [(PARK_LINE, TENANT, safe_decision().reply)]
    assert pipe.store.get_draft(1).status == st.AUTO_SENT
    assert pipe.slack.posts[0][0].startswith("Auto-sent")


def test_duplicate_notification_is_ignored(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound()))
    run(pipe.handle_inbound(inbound()))
    assert pipe.agent.calls == 1


def test_burst_of_texts_only_answers_latest(make_pipeline):
    pipe = make_pipeline()
    # Simulate the second text arriving while the first is still waiting out the debounce.
    pipe.store.record_inbound("m2", "c1", TENANT, PARK_LINE, "also the toilet", "2026-10-07T19:00:30+00:00")
    run(pipe.handle_inbound(inbound("m1")))
    assert pipe.agent.calls == 0


def test_newer_text_supersedes_pending_draft(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound("m1")))
    run(pipe.handle_inbound(inbound("m2", "hello?")))
    assert pipe.store.get_draft(1).status == st.SUPERSEDED
    assert pipe.store.get_draft(2).status == st.PENDING
    assert pipe.slack.updates[0][1] == "Superseded by a newer text"


def test_approve_sends_once_even_with_double_tap(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound()))
    assert run(pipe.approve(1, None, "U1")) == "Sent."
    assert run(pipe.approve(1, None, "U1")).startswith("Already handled")
    assert len(pipe.rc.sent) == 1
    draft = pipe.store.get_draft(1)
    assert draft.status == st.SENT and draft.decided_by == "U1"


def test_edited_reply_is_what_gets_sent(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound()))
    run(pipe.approve(1, "We'll have someone look today.", "U1"))
    assert pipe.rc.sent[0][2] == "We'll have someone look today."
    assert pipe.store.get_draft(1).final_text == "We'll have someone look today."
    assert "(edited)" in pipe.slack.updates[-1][1]


def test_failed_send_leaves_draft_pending(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound()))
    pipe.rc.fail_send = True
    assert run(pipe.approve(1, None, "U1")).startswith("Sending failed")
    assert pipe.store.get_draft(1).status == st.PENDING


def test_skip(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound()))
    assert run(pipe.skip(1, "U1")) == "Skipped."
    assert run(pipe.approve(1, None, "U1")).startswith("Already handled")
    assert pipe.rc.sent == []


def test_agent_error_alerts_slack_with_the_text(make_pipeline):
    pipe = make_pipeline()
    pipe.agent.error = RuntimeError("boom")
    run(pipe.handle_inbound(inbound(text="where do I pay")))
    assert "Text bot error" in pipe.slack.posts[0][0]
    assert "where do I pay" in pipe.slack.posts[0][0]


def test_staff_numbers_are_ignored(make_pipeline):
    pipe = make_pipeline(make_settings(staff_numbers=frozenset({TENANT})))
    run(pipe.handle_inbound(inbound()))
    assert pipe.agent.calls == 0 and pipe.slack.posts == []


def test_no_reply_still_gets_a_card(make_pipeline):
    pipe = make_pipeline(decision=safe_decision(action="no_reply", category="closed", reply=""))
    run(pipe.handle_inbound(inbound(text="thank you!")))
    assert pipe.slack.posts[0][0].startswith("No reply needed")
    assert pipe.store.get_draft(1).status == st.PENDING


def test_failed_auto_send_falls_back_to_approval_card(make_pipeline):
    settings = make_settings(shadow_mode=False, auto_send_categories=frozenset({"maintenance"}))
    pipe = make_pipeline(settings)
    pipe.rc.fail_send = True
    run(pipe.handle_inbound(inbound()))
    assert pipe.store.get_draft(1).status == st.PENDING
    assert pipe.slack.posts[-1][0].startswith("Auto-send failed")
    assert pipe.store.auto_sends_since(TENANT, pipe._clock().replace(hour=0)) == 0


def test_reply_from_ringcentral_retires_pending_card(make_pipeline):
    pipe = make_pipeline()
    run(pipe.handle_inbound(inbound(conv="c1")))
    run(pipe.handle_outbound("c1"))
    assert pipe.store.get_draft(1).status == st.SUPERSEDED
    assert pipe.slack.updates[-1][1] == "Answered directly in RingCentral"
