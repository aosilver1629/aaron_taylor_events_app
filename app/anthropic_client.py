"""Shared helper for Claude calls that must return strict structured JSON.

Both the reply parser (Job 2) and the research ranker/schema call (Job 1)
need the same discipline: force a tool call so the model can't wrap its
answer in prose, validate the result against a caller-supplied validator,
and on a malformed response retry the call exactly once with a corrective
follow-up message — never attempt to regex-repair broken JSON.
"""
from __future__ import annotations

import logging
from typing import Callable

from anthropic import Anthropic

from app.config import get_settings

logger = logging.getLogger("anthropic_client")


class MalformedToolOutput(Exception):
    pass


def get_client() -> Anthropic:
    settings = get_settings()
    return Anthropic(api_key=settings.anthropic_api_key)


def call_tool_with_validated_json(
    *,
    model: str,
    system: str,
    user_content,
    tool: dict,
    validate: Callable[[dict], dict],
    max_tokens: int = 1024,
    extra_tools: list[dict] | None = None,
    tool_choice_name: str | None = None,
) -> dict:
    """Call `tool` (forced tool_choice) and return its validated input.

    On a malformed/invalid response, retries once with a message telling
    the model exactly what was wrong. Raises MalformedToolOutput if the
    retry also fails validation — the caller decides what "log and move
    on" means for that job.
    """
    client = get_client()
    tools = [tool] + (extra_tools or [])
    forced_name = tool_choice_name or tool["name"]

    messages = [{"role": "user", "content": user_content}]

    for attempt in range(2):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            tool_choice={"type": "tool", "name": forced_name},
            messages=messages,
        )
        tool_use = next(
            (b for b in response.content if getattr(b, "type", None) == "tool_use"),
            None,
        )
        if tool_use is None:
            reason = "no tool_use block in response"
        else:
            try:
                return validate(tool_use.input)
            except Exception as exc:  # noqa: BLE001 - validator defines the contract
                reason = str(exc)

        logger.warning(
            "malformed_tool_output",
            extra={"job_fields": {"attempt": attempt, "reason": reason}},
        )
        if attempt == 0:
            messages.append({"role": "assistant", "content": response.content})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"That response was invalid: {reason}. "
                        f"Call {forced_name} again with corrected arguments "
                        "matching the schema exactly."
                    ),
                }
            )

    raise MalformedToolOutput(f"tool {forced_name} did not return valid output after retry")
