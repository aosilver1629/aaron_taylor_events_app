"""Job 1's Claude calls: the research pass (web search + structured output)
and the ranking pass (used only when validation leaves more than 12
survivors).

The research call can't simply force tool_choice onto the output tool the
way app.anthropic_client does for the reply parser: forcing tool_choice
makes Claude call that tool as its very first action, which would skip web
search entirely. Instead tool_choice is left on "auto" for turn one — the
model runs its web_search calls (server-executed, transparent, multiple
calls fold into this one turn) and is instructed to finish by calling
submit_events. Only on the corrective retry (turn two), with the search
results already sitting in the conversation history, is tool_choice forced
onto submit_events — by then there's nothing left to search for, so forcing
it is safe and guarantees compliant JSON.
"""
from __future__ import annotations

import logging

from anthropic import Anthropic

from app.config import get_settings
from app.utils.retry import with_retry

logger = logging.getLogger("claude_research")

RESEARCH_MODEL = "claude-opus-5"

SOURCES = [
    "Funcheap SF (sffuncheap.com)",
    "DoTheBay (dothebay.com)",
    "SF Station (sfstation.com)",
    "Eventbrite Bay Area",
    "SF Rec & Parks (sfrecpark.org)",
]

SUBMIT_EVENTS_TOOL = {
    "name": "submit_events",
    "description": "Submit the final list of candidate SF events for this week's ballot.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["events"],
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["title", "start_at", "category", "pitch"],
                    "properties": {
                        "title": {"type": "string"},
                        "start_at": {
                            "type": "string",
                            "description": "ISO 8601 datetime with timezone offset.",
                        },
                        "end_at": {"type": ["string", "null"]},
                        "venue": {"type": ["string", "null"]},
                        "neighborhood": {"type": ["string", "null"]},
                        "category": {
                            "type": "string",
                            "enum": ["concert", "festival", "market", "food", "outdoor", "art", "other"],
                        },
                        "price_range": {"type": ["string", "null"]},
                        "url": {"type": ["string", "null"]},
                        "pitch": {
                            "type": "string",
                            "description": "One line on why this might appeal to Aaron and Tay.",
                        },
                        "source": {"type": ["string", "null"]},
                    },
                },
            }
        },
    },
}


def _system_prompt() -> str:
    sources = "\n".join(f"- {s}" for s in SOURCES)
    return f"""You are researching upcoming events in San Francisco / the Bay \
Area for two people, Aaron and Tay, who will vote on which ones to add to \
their shared calendar.

Use the web_search tool to check these sources for events in the next 21 \
days, in addition to anything else relevant you find:
{sources}

You are also given a list of events already found deterministically via \
Ticketmaster and Bandsintown. Do not re-search for those — just fold in \
anything from web search that they missed. Your final submit_events list \
should include BOTH those events (reformatted per the schema) AND the new \
ones you find, but only ONE entry per real-world event — don't create a \
second entry for something already given to you as context.

For each event you decide to include, write a one-line pitch explaining why \
it might appeal to Aaron and Tay specifically, using their voting history \
below if any is given. Only include events with a real, confirmed date and \
a real URL you found (do not invent URLs).

When you are done researching, call submit_events exactly once with the \
full candidate list. Do not call it before you've actually searched the \
sources above."""


def _build_user_message(structured_context: str, preference_summary: str) -> str:
    return f"""Events already found deterministically (do not re-search for \
these, just avoid duplicating them):

{structured_context or "(none found)"}

Preference context from recent voting history:

{preference_summary}

Research the sources you were given, then call submit_events with the full \
candidate list for the next 21 days."""


def run_research_call(structured_context: str, preference_summary: str) -> list[dict]:
    """Returns the raw event dicts from submit_events.input["events"]. Caller
    (research_job) is responsible for per-row validation — this only
    guarantees the tool call itself is well-formed JSON.
    """
    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)

    web_search_tool = {
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 15,
    }
    tools = [web_search_tool, SUBMIT_EVENTS_TOOL]

    messages = [
        {
            "role": "user",
            "content": _build_user_message(structured_context, preference_summary),
        }
    ]

    def _turn(tool_choice, tools_for_turn, max_tokens=8000):
        return client.messages.create(
            model=RESEARCH_MODEL,
            max_tokens=max_tokens,
            system=_system_prompt(),
            tools=tools_for_turn,
            tool_choice=tool_choice,
            messages=messages,
        )

    response = with_retry(
        lambda: _turn({"type": "auto"}, tools, max_tokens=24000),
        what="research_call_turn1",
        reraise=True,
    )

    submit = _find_tool_use(response, "submit_events")
    if submit is not None:
        return submit.input.get("events", [])

    logger.warning(
        "research_call_no_submit",
        extra={"job_fields": {"stop_reason": response.stop_reason}},
    )

    # Corrective retry: search context is already in history, so it's safe
    # to force the output tool now. Do not attempt to regex-repair the
    # first response — just ask again with tool_choice forced. If turn one
    # hit max_tokens mid tool-call, trim the dangling unresolved tool-use
    # block first — replaying it as-is gets the whole request rejected.
    messages.append({"role": "assistant", "content": _trim_unresolved_tail(response.content)})
    messages.append(
        {
            "role": "user",
            "content": (
                "You didn't call submit_events. Using what you've already found, "
                "call submit_events now with the full candidate list."
            ),
        }
    )
    retry_response = with_retry(
        lambda: _turn({"type": "tool", "name": "submit_events"}, [SUBMIT_EVENTS_TOOL]),
        what="research_call_turn2",
        reraise=True,
    )
    submit = _find_tool_use(retry_response, "submit_events")
    if submit is None:
        logger.error("research_call_failed_after_retry")
        return []
    return submit.input.get("events", [])


def _find_tool_use(response, name: str):
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == name:
            return block
    return None


_TOOL_USE_TYPES = {"tool_use", "server_tool_use"}


def _trim_unresolved_tail(content: list) -> list:
    """Drop trailing tool-use blocks left dangling by a max_tokens cutoff.

    Opus can hit max_tokens mid-turn while it's still using its bundled
    code_execution tool to orchestrate searches — the response then ends on
    an unresolved server_tool_use with no matching *_tool_result, which the
    API rejects outright if replayed as-is into the next turn's history.
    """
    content = list(content)
    while content and getattr(content[-1], "type", None) in _TOOL_USE_TYPES:
        content.pop()
    return content


RANK_MODEL = "claude-opus-5"

RANK_TOOL = {
    "name": "select_best_events",
    "description": "Select the best events to include in this week's ballot.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["event_keys"],
        "properties": {
            "event_keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The event_key values of the chosen events, best first.",
            }
        },
    },
}


def rank_and_select(candidates: list[dict], preference_summary: str, cap: int) -> list[str]:
    """candidates: [{"event_key": ..., "title": ..., "category": ..., ...}].
    Returns the chosen event_keys, length <= cap.
    """
    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)

    listing = "\n".join(
        f"- {c['event_key']}: {c['title']} ({c.get('category')}, {c.get('venue')})"
        for c in candidates
    )
    system = (
        f"You must pick the best {cap} of the following {len(candidates)} candidate "
        "SF events for a couple to vote on, given their preference history. Prefer "
        "variety and events matching liked categories/venues; avoid disliked ones."
    )
    user = f"Candidates:\n{listing}\n\nPreference context:\n{preference_summary}"

    def _call():
        return client.messages.create(
            model=RANK_MODEL,
            max_tokens=1024,
            system=system,
            tools=[RANK_TOOL],
            tool_choice={"type": "tool", "name": "select_best_events"},
            messages=[{"role": "user", "content": user}],
        )

    response = with_retry(_call, what="research_rank", reraise=True)
    submit = _find_tool_use(response, "select_best_events")
    if submit is None:
        # Fall back to the first `cap` candidates rather than failing the
        # whole job over a ranking-call hiccup.
        logger.error("rank_call_failed_falling_back_to_first_n")
        return [c["event_key"] for c in candidates[:cap]]

    valid_keys = {c["event_key"] for c in candidates}
    chosen = [k for k in submit.input.get("event_keys", []) if k in valid_keys]
    return chosen[:cap]
