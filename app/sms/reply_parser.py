"""Turns a raw inbound SMS body into {"yes": [...], "no": [...]} against the
numbered list that batch sent to that person. A small Claude call (Haiku) —
the model has to do real work here (typos, ranges, hedges, name references,
garbage) so it isn't worth hand-rolling a regex parser, but it's cheap and
fast since the whole task is "classify these numbers."
"""
from __future__ import annotations

import logging

from app.anthropic_client import MalformedToolOutput, call_tool_with_validated_json
from app.models import ReplyParseResult

logger = logging.getLogger("reply_parser")

MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """You are parsing a text message reply to a numbered list of \
local events. The person was asked to reply with the numbers they're in for.

Rules:
- Map explicit numbers, ranges ("1-3" means 1, 2, 3), and comma lists directly.
- "all" / "all of them" means every number in the list is a yes.
- "none" / "nah" / "no thanks" means every number is a no.
- Hedges like "1, 3 and maybe 5" count the hedged number as a yes — a maybe \
still means they want it on the list; err toward including it.
- Typos like "yess 2" or "ya 1 3" should still resolve to the numbers given.
- A reference by name instead of number (e.g. "the market one") should \
resolve to the matching item's number using the numbered list you were given.
- If you cannot confidently map something in the message to a specific \
number, do not guess — leave it out of both yes and no entirely. Never put \
a number in "yes" unless the message clearly expresses interest in it.
- Only include numbers that actually appear in the numbered list you were \
given. Every number you return in "yes" or "no" must come from that list.
- Do NOT try to classify every number in the list unless the message \
clearly means the whole list (see the "all"/"none" rules above). Most \
replies only mention a few specific numbers — the rest must be left out \
of BOTH arrays, not defaulted into "no". Example: if the list has events \
1-5 and the reply is "1, 3", the correct output is yes=[1, 3], no=[] — NOT \
no=[2, 4, 5]. Declining or ignoring one specific event also does not make \
the others a "yes": if the reply is "skip the symphony (event 3)" with no \
other events mentioned, the correct output is yes=[], no=[3] — NOT \
yes=[1, 2, 4, 5]. It is normal and expected for yes and no to together \
cover only a small fraction of the numbered list, EXCEPT when the message \
is a blanket reply like "none"/"nah"/"no thanks"/"skip this week", which \
still means every number in the list goes in "no"."""

TOOL = {
    "name": "record_reply",
    "description": "Record which numbered events the person is voting yes or no on.",
    "input_schema": {
        "type": "object",
        "properties": {
            "yes": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Numbers the person is clearly in for.",
            },
            "no": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Numbers the person clearly declined.",
            },
        },
        "required": ["yes", "no"],
    },
}


def _validate(raw: dict, valid_numbers: set[int]) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("tool input is not an object")
    yes = raw.get("yes")
    no = raw.get("no")
    if not isinstance(yes, list) or not isinstance(no, list):
        raise ValueError("yes/no must be arrays")
    if not all(isinstance(n, int) for n in yes + no):
        raise ValueError("yes/no must contain only integers")
    bad = [n for n in yes + no if n not in valid_numbers]
    if bad:
        raise ValueError(f"numbers not in the sent list: {bad}")
    overlap = set(yes) & set(no)
    if overlap:
        raise ValueError(f"numbers in both yes and no: {sorted(overlap)}")
    return {"yes": yes, "no": no}


def build_user_content(raw_body: str, numbered_events: list[tuple[int, str]]) -> str:
    listing = "\n".join(f"{n}. {title}" for n, title in numbered_events)
    return f'Numbered list sent to this person:\n{listing}\n\nTheir reply: "{raw_body}"'


def parse_reply(raw_body: str, numbered_events: list[tuple[int, str]]) -> ReplyParseResult:
    """numbered_events: [(list_number, title), ...] for the person's most
    recent batch. Every number not returned as yes resolves to no by the
    caller — this function only reports what it could confidently map.
    """
    valid_numbers = {n for n, _ in numbered_events}

    try:
        result = call_tool_with_validated_json(
            model=MODEL,
            system=SYSTEM_PROMPT,
            user_content=build_user_content(raw_body, numbered_events),
            tool=TOOL,
            validate=lambda raw: _validate(raw, valid_numbers),
            max_tokens=300,
            temperature=0,
        )
    except MalformedToolOutput:
        logger.error(
            "reply_parse_failed",
            extra={"job_fields": {"raw_body": raw_body, "note": "flagged, defaulting all to no"}},
        )
        return ReplyParseResult(yes=[], no=[], unmapped_raw=raw_body)

    yes = sorted(set(result["yes"]))
    no = sorted(set(result["no"]))
    unmapped = sorted(valid_numbers - set(yes) - set(no))
    if unmapped:
        logger.warning(
            "reply_parse_unmapped_numbers",
            extra={
                "job_fields": {
                    "raw_body": raw_body,
                    "unmapped": unmapped,
                    "note": "defaulting to no, never guessing yes",
                }
            },
        )
    return ReplyParseResult(yes=yes, no=no, unmapped_raw=raw_body if unmapped else None)
