"""Claude reads the week's numbers and writes the scorecard Jeff gets in Slack."""

from __future__ import annotations

import json
import logging

import anthropic

log = logging.getLogger(__name__)

SYSTEM = """\
You are the portfolio analyst for Jeff, who owns a group of mobile home park LLCs. Each
LLC is its own QuickBooks company. Every week you get the numbers for every entity and
write the scorecard he reads in Slack.

What Jeff wants from it: which entities need his attention and what he should do about
each, in priority order. Lead with the decisions. Be specific: name the entity, the
account, the dollar amount and the comparison. Recommend a concrete next step (who to
ask, what to look at, what to change), and say when something is probably a bookkeeping
issue rather than a business one; uncategorized money or a stale sync makes the P&L
untrustworthy, so call that out before drawing conclusions from it.

Use only the numbers provided. Don't invent causes; when the data can't explain a
change, say what would. NOI is income minus cost of goods sold minus operating expenses;
debt service and depreciation usually sit below it.

Format for Slack mrkdwn: *bold* section labels, short bullets, no tables, no headings
with #. Keep it under 400 words. Sections: *Headline* (one or two sentences on the
portfolio), *Needs attention*, *Watch*, *Data issues* (omit any section with nothing in it).
"""


class Analyst:
    def __init__(self, client: anthropic.AsyncAnthropic, model: str, effort: str) -> None:
        self._client = client
        self._model = model
        self._effort = effort

    async def write_scorecard(self, data: dict) -> str:
        response = await self._client.beta.messages.create(
            model=self._model,
            max_tokens=16000,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[
                {
                    "role": "user",
                    "content": "<data>\n" + json.dumps(data, default=str) + "\n</data>\n\n"
                    "Write this week's scorecard.",
                }
            ],
            output_config={"effort": self._effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            return "_Claude declined to write this week's summary; the numbers are below._"
        if response.stop_reason == "max_tokens":
            log.warning("scorecard narrative was cut off")
        return next((b.text for b in response.content if b.type == "text"), "").strip()
