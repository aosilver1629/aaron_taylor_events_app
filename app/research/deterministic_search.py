"""Job 1's research call — a fixed, cheap pull from curated SF sources
instead of letting Opus freely orchestrate its own search loop (see
app.research.claude_research for that original approach, kept in the repo
unused in case this needs to be reverted).

Per source, in order:

1. Plain HTTP fetch, tried first for every source. Several of these sites
   (The Chapel, 1015 Folsom, GAMH, Funcheap, the two food outlets) server-
   render their listings, so a bare `httpx.get` + tag-strip gets the real
   content for free — no Claude call at all for the fetch itself.
2. A cheap, forced, single-shot web_search fallback for sources where the
   plain fetch comes back empty (checked heuristically — a JS-only shell or
   a bot-block page is short and says so). Three of the five music venues
   here (The Fillmore, Cobb's, Punch Line) are Live Nation sites that only
   render listings client-side; The Independent's calendar page sits behind
   Cloudflare's bot check. tool_choice is forced onto web_search so this
   can't spiral into an agentic loop. Known weaker link: search results can
   be stale (an already-past show date still indexed), so these four
   sources are less reliable than the three below.
3a. For The Chapel / GAMH / 1015 Folsom specifically: a regex parser (see
    SOURCES' "parser" key) runs first on the fetched HTML — all three
    happen to use a regular, repetitive ticketing-widget template that LLM
    extraction handled unreliably (under-extraction, occasional garbage
    fields) no matter how the prompt was tuned, and a parser is both free
    and exhaustive where the LLM wasn't. One small forced Claude call still
    runs per source afterward just to write pitches for the parsed titles.
3b. Every other source (or a parser source if the parser finds nothing —
    e.g. the page's template changed) — one forced submit_events call per
    source (Sonnet, not Opus). Each source's category (venue / comedy /
    fairs / food) carries different extraction rules, since what counts as
    a real candidate differs per source — a comedy club needs one entry per
    performer, not one per night of a run; Funcheap needs only annual
    one-off fairs, not recurring weekly markets (this rule's strictness has
    shown real run-to-run variance in testing — sometimes finds several,
    sometimes none); the food outlets need dated pop-up events, not
    reviews or evergreen articles.

Known limitation: The Independent's fallback URL is the same Cloudflare-
blocked page that broke the initial scrape, so an event sourced from it
will almost always fail validate_candidate's URL-liveness check.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime
from datetime import time as dtime

import httpx
from anthropic import Anthropic

from app.config import get_settings
from app.research.claude_research import SUBMIT_EVENTS_TOOL, _find_tool_use
from app.utils.retry import with_retry
from app.utils.time import PACIFIC, now_utc, to_pacific

logger = logging.getLogger("deterministic_search")

SEARCH_MODEL = "claude-haiku-4-5-20251001"
EXTRACT_MODEL = "claude-sonnet-5"

MAX_SOURCE_CHARS = 6000
_MIN_REAL_CONTENT_CHARS = 900
_BLOCKED_MARKERS = ("loading...", "cloudflare", "attention required", "enable javascript")

# Kept as plain data (not baked into a prompt paragraph) so a future,
# smarter source list — e.g. built from preference history, or with venues
# added/removed — can replace this without touching the fetch/extract code.
# "parser" is optional: a regex-based parser to try before falling back to
# LLM extraction (see _parse_chapel / _parse_gamh / _parse_1015_folsom below)
# — used for the three venues whose calendar pages turned out to be a
# regular, repetitive ticketing-widget template. LLM extraction on those
# specific pages proved unreliable (under-extraction, occasional garbage
# fields) no matter how the prompt was tuned; a page with this much
# structure is a better fit for a parser than an LLM call anyway. The two
# Live Nation venues (Fillmore, Cobb's, Punch Line) and The Independent
# have no plain-HTTP-fetchable listing at all (JS-only or Cloudflare-
# blocked), so they have no parser and always go through the search
# fallback + LLM extraction path.
SOURCES = [
    {"label": "The Fillmore", "url": "https://thefillmore.com/shows/", "kind": "venue"},
    {"label": "The Independent", "url": "https://www.theindependentsf.com/calendar", "kind": "venue"},
    {
        "label": "The Chapel",
        "url": "https://thechapelsf.com/calendar/",
        "kind": "venue",
        "parser": "chapel",
    },
    {"label": "1015 Folsom", "url": "https://1015.com/", "kind": "venue", "parser": "1015folsom"},
    {
        "label": "Great American Music Hall",
        "url": "https://gamh.com/calendar/",
        "kind": "venue",
        "parser": "gamh",
    },
    {"label": "Cobb's Comedy Club", "url": "https://www.cobbscomedy.com/shows", "kind": "comedy"},
    {"label": "Punch Line SF", "url": "https://www.punchlinecomedyclub.com/shows", "kind": "comedy"},
    {"label": "Funcheap SF Events", "url": "https://sf.funcheap.com/events/", "kind": "fairs"},
    {"label": "SF Standard Food & Drink", "url": "https://sfstandard.com/food-drink/", "kind": "food"},
    {"label": "SFGate Food", "url": "https://www.sfgate.com/food/", "kind": "food"},
]

_KIND_INSTRUCTIONS = {
    "venue": (
        "This is {label}'s show listing. List every real upcoming show you "
        "find with its date and price if shown. For EVERY event from this "
        "source: set venue to \"{label}\" unless the listing clearly states "
        "a different specific venue, and set url to that show's own link "
        "if the listing gives one, otherwise set url to this exact page: "
        "{url}. Never leave venue or url blank for an event from this "
        "source — one of these two URLs always applies."
    ),
    "comedy": (
        "This is {label}'s show listing. For each distinct performer/act, "
        "list only ONE entry — if that performer has multiple nights or "
        "showtimes in the listing, do NOT list each night separately; use "
        "their first/soonest date and note in your summary that it runs "
        "across multiple nights (e.g. 'runs Thu-Sat'). Include price if "
        "shown. For EVERY event from this source: set venue to \"{label}\", "
        "and set url to that show's own link if given, otherwise to this "
        "exact page: {url}. Never leave venue or url blank for an event "
        "from this source."
    ),
    "fairs": (
        "This is Funcheap's SF events listing. Only include fairs, street "
        "festivals, and markets that are ANNUAL, ONCE-A-YEAR special "
        "events — a named street fair or festival that happens one time "
        "per year. Do NOT include recurring weekly/monthly things like a "
        "regular farmers market, a weekly night market, or an ongoing "
        "weekly series. Include the date, price if shown, and venue if "
        "the listing names one. For EVERY event from this source: set url "
        "to that event's own page if the listing links to one, otherwise "
        "set url to this exact page: {url}. Never leave url blank for an "
        "event from this source."
    ),
    "food": (
        "This is from {label}, a news outlet. Only include specific, "
        "DATED food/dining EVENTS mentioned in its articles — pop-up "
        "dinners, food festivals, launch events, limited-run "
        "collaborations with an actual date. Do NOT include restaurant "
        "reviews, 'best of' lists, or evergreen articles with no specific "
        "event date. For EVERY event from this source: set url to that "
        "article's own link if given, otherwise to this exact page: {url}. "
        "Never leave url blank for an event from this source."
    ),
}


def _clean_html(raw_html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _looks_blocked_or_empty(text: str) -> bool:
    if len(text) < _MIN_REAL_CONTENT_CHARS:
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in _BLOCKED_MARKERS)


# ---- regex parsers for structured venue-calendar pages ------------------
#
# Verified by hand against live fetches of all three sites before wiring
# in: Chapel 10/10 and GAMH 11/11 events with correct titles/dates/prices,
# 1015 Folsom 16/16. See the module docstring for why these exist at all.

_MONTHS_FULL = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_MONTH_NUM = {name: i for i, name in enumerate(_MONTHS_FULL, start=1)}
_MONTH_NUM.update({name[:3]: i for i, name in enumerate(_MONTHS_FULL, start=1)})

_WEEKDAYS_ABBR = "Sun|Mon|Tue|Wed|Thu|Fri|Sat"
_MONTHS_ABBR = "|".join(name[:3] for name in _MONTHS_FULL)
_MONTHS_FULL_ALT = "|".join(_MONTHS_FULL)

_DEFAULT_SHOW_TIME = dtime(21, 0)


def _infer_upcoming_date(month_name: str, day: int) -> date | None:
    """Venue pages routinely give a date with no year ("Fri Sep 4",
    "September 4th") — infer the nearest upcoming occurrence relative to
    today, never a date in the past."""
    month = _MONTH_NUM.get(month_name)
    if month is None:
        return None
    today = to_pacific(now_utc()).date()
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        return None
    if candidate < today:
        try:
            candidate = date(today.year + 1, month, day)
        except ValueError:
            return None
    return candidate


def _parse_clock_time(time_str: str | None) -> dtime:
    if time_str:
        m = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)", time_str.strip(), re.IGNORECASE)
        if m:
            hour = int(m.group(1)) % 12
            if m.group(3).upper() == "PM":
                hour += 12
            return dtime(hour, int(m.group(2)))
    return _DEFAULT_SHOW_TIME


def _combine_iso(d: date | None, t: dtime) -> str | None:
    if d is None:
        return None
    return datetime.combine(d, t, tzinfo=PACIFIC).isoformat()


def _clean_venue_title(raw_title: str) -> str:
    """Venue calendar widgets commonly render as "{Promoter} Presents…
    {Title} {Title} with {Support}" — strip the promoter prefix and
    collapse the title's self-repeat, which is where the real title
    lives. Handles both a straight repeat ("Pallbearer Pallbearer") and
    one with a tour-subtitle before it ("Foundations of Burden Tour
    Pallbearer Pallbearer")."""
    t = re.sub(r"^.*?\bpresents?\b[.…]*\s+", "", raw_title, flags=re.IGNORECASE)
    words = t.split()
    for start in range(len(words)):
        for n in range(min(8, len(words) - start), 0, -1):
            a = words[start:start + n]
            b = words[start + n:start + 2 * n]
            if a and [w.lower() for w in a] == [w.lower() for w in b]:
                return " ".join(a)
    # No repeat found — the title is genuinely just stated once (e.g. "An
    # Evening With Drink The Sea"). Don't guess-truncate at "with"; a
    # legitimate title can contain that word.
    return t.strip()


def _parse_ticketing_widget(text: str, doors_label: str, show_label: str) -> list[dict]:
    """Shared parser for the "Doors at X / Show at Y ... Share Event
    {status}" template used by both The Chapel and GAMH's calendar pages —
    same widget, different door/show label text."""
    date_token_re = re.compile(rf"\b(?:{_WEEKDAYS_ABBR}) ({_MONTHS_ABBR}) (\d{{1,2}})\b")
    date_matches = list(date_token_re.finditer(text))
    block_re = re.compile(
        rf"{re.escape(doors_label)}\s*(\d{{1,2}}:\d{{2}}\s*[AP]M)\s*/\s*{re.escape(show_label)}\s*"
        rf"(\d{{1,2}}:\d{{2}}\s*[AP]M)\s*at\s+([^,$]+?)\s*(?:,\s*)?\$?([\d.]+)(?:-\$?[\d.]+)?\s+"
        rf"([A-Za-z/ ]+?)\s*Share Event\s*(Buy Tickets|Get Tickets|Sold Out|More Info)"
    )
    events: list[dict] = []
    title_start = 0
    for i, dm in enumerate(date_matches):
        block_search_end = date_matches[i + 1].start() if i + 1 < len(date_matches) else len(text)
        block_text = text[dm.end():block_search_end]
        raw_title = text[max(title_start, dm.start() - 200):dm.start()].strip()
        title = _clean_venue_title(raw_title)
        bm = block_re.search(block_text)
        if bm and title:
            start_date = _infer_upcoming_date(dm.group(1), int(dm.group(2)))
            start_at = _combine_iso(start_date, _parse_clock_time(bm.group(2)))
            if start_at is None:
                title_start = dm.end() + bm.end()
                continue
            events.append(
                {
                    "title": title[:120],
                    "start_at": start_at,
                    "price_range": f"${bm.group(4)}",
                    "category": "concert",
                }
            )
            title_start = dm.end() + bm.end()
        # else: leave title_start where it was — a block that fails to
        # match (e.g. a "PRIVATE EVENT" with no price) shouldn't eat the
        # next real event's title text; the 200-char lookback cap already
        # bounds how far back a title search can reach.
    return events


def _parse_chapel(text: str) -> list[dict]:
    return _parse_ticketing_widget(text, "Doors at", "Show at")


def _parse_gamh(text: str) -> list[dict]:
    return _parse_ticketing_widget(text, "Event Doortime:", "Event Showtime:")


def _parse_1015_folsom(text: str) -> list[dict]:
    pattern = re.compile(
        r"\b(Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday) "
        rf"({_MONTHS_FULL_ALT}) (\d{{1,2}})(?:st|nd|rd|th)\s+(.{{1,60}}?)\s+"
        r"(?:BUY TICKETS|SIGN UP) Bottle Service"
    )
    events: list[dict] = []
    for _weekday, month_name, day, title in pattern.findall(text):
        start_date = _infer_upcoming_date(month_name, int(day))
        start_at = _combine_iso(start_date, _DEFAULT_SHOW_TIME)
        if start_at is None or not title.strip():
            continue
        events.append(
            {
                "title": title.strip()[:120],
                "start_at": start_at,
                "category": "concert",
            }
        )
    return events


_PARSERS = {
    "chapel": _parse_chapel,
    "gamh": _parse_gamh,
    "1015folsom": _parse_1015_folsom,
}


def _generate_pitches(events: list[dict], source_label: str, preference_summary: str) -> None:
    """Fills in event["pitch"] for a batch of regex-parsed events with one
    small forced call — regex can get title/date/price/venue for free, but
    can't write "why Aaron and Tay might like this," so this is the one
    LLM call these events still need. Matches by title, since titles
    within one source's batch are effectively unique."""
    if not events:
        return
    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)

    pitch_tool = {
        "name": "assign_pitches",
        "description": "Assign a one-line pitch to each event title.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["pitches"],
            "properties": {
                "pitches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["title", "pitch"],
                        "properties": {
                            "title": {"type": "string"},
                            "pitch": {"type": "string"},
                        },
                    },
                }
            },
        },
    }
    titles = "\n".join(f"- {e['title']}" for e in events)
    system = (
        f"These are upcoming shows at {source_label}, a San Francisco venue, "
        "for two people, Aaron and Tay, who vote on which to add to their "
        "shared calendar. Write a one-line pitch for each title explaining "
        "why it might appeal to them, using their voting history if given."
    )
    user = f"Titles:\n{titles}\n\nPreference context:\n{preference_summary}\n\nCall assign_pitches."

    def _call():
        return client.messages.create(
            model=EXTRACT_MODEL,
            max_tokens=2000,
            system=system,
            tools=[pitch_tool],
            tool_choice={"type": "tool", "name": "assign_pitches"},
            messages=[{"role": "user", "content": user}],
        )

    try:
        response = with_retry(_call, what=f"deterministic_pitch:{source_label}", reraise=True)
    except Exception:
        logger.exception("deterministic_pitch_failed", extra={"job_fields": {"source": source_label}})
        for event in events:
            event["pitch"] = f"Live show at {source_label}."
        return

    submit = _find_tool_use(response, "assign_pitches")
    pitch_by_title = {}
    if submit is not None:
        for row in submit.input.get("pitches", []):
            if isinstance(row, dict) and row.get("title"):
                pitch_by_title[row["title"]] = row.get("pitch", "")

    for event in events:
        event["pitch"] = pitch_by_title.get(event["title"]) or f"Live show at {source_label}."


def _fetch_via_http(url: str) -> str:
    try:
        with httpx.Client(timeout=10, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return _clean_html(resp.text)
    except Exception:
        logger.warning("deterministic_http_fetch_failed", extra={"job_fields": {"url": url}})
        return ""


def _fetch_via_search_fallback(source: dict) -> str:
    """Forced, single-shot web_search — used only when the plain HTTP fetch
    for a source comes back empty or blocked. Same bounded, non-agentic
    pattern as the rest of this module: one search, no branching, no
    code_execution (the basic web_search_20250305 variant doesn't bundle
    it)."""
    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)
    web_search_tool = {"type": "web_search_20250305", "name": "web_search"}
    query = f"{source['label']} San Francisco upcoming shows tickets"

    system = (
        "You are researching upcoming San Francisco events. After the "
        "search results come back, write a plain-text summary of every "
        "real, dated show you can find, with venue, price if mentioned, "
        "and URL. Do not invent details that aren't in the results."
    )

    def _call():
        return client.messages.create(
            model=SEARCH_MODEL,
            max_tokens=2048,
            system=system,
            tools=[web_search_tool],
            tool_choice={"type": "tool", "name": "web_search"},
            messages=[{"role": "user", "content": query}],
        )

    try:
        response = with_retry(_call, what=f"deterministic_search_fallback:{source['label']}", reraise=True)
    except Exception:
        logger.exception(
            "deterministic_search_fallback_failed",
            extra={"job_fields": {"source": source["label"]}},
        )
        return ""

    return "\n".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()


def _get_source_text(source: dict) -> tuple[str, str]:
    """Returns (text, method) — method is 'http' or 'search_fallback', for
    logging/inspection. Full, untruncated text — a regex parser needs to
    see the whole page (GAMH's listing alone runs past MAX_SOURCE_CHARS);
    truncation for the LLM path happens in _extract_from_source instead."""
    text = _fetch_via_http(source["url"])
    if not _looks_blocked_or_empty(text):
        return text, "http"

    logger.info(
        "deterministic_http_fetch_thin_falling_back_to_search",
        extra={"job_fields": {"source": source["label"], "url": source["url"]}},
    )
    return _fetch_via_search_fallback(source), "search_fallback"


_EXTRACT_SYSTEM_PROMPT_HEADER = """You are given raw text pulled from one \
San Francisco Bay Area source, for two people, Aaron and Tay, who will \
vote on which events to add to their shared calendar.

Today's date is {today} (America/Los_Angeles). Many venue listings give a \
date like "Friday August 28th" with no year — when a listing omits the \
year, infer it as the nearest UPCOMING occurrence of that month/day \
relative to today, never a date in the past. Every start_at you return \
must be today or later.

This page commonly lists MANY events (a venue calendar can have a dozen or \
more shows). You must extract EVERY SINGLE real, dated event you find — \
not just the first one, not a representative sample. If the text lists 15 \
shows, return 15 entries. Under-extracting is a real failure here: read \
the entire text below carefully before calling submit_events, and count \
how many distinct dated events you found before you respond.

For each event, write a one-line pitch explaining why it might appeal to \
Aaron and Tay specifically, using their voting history below if any is \
given. Every event needs a real title — never submit an entry with a \
blank or missing title. Only include events with a real date — do not \
invent one. Do not set the "source" field yourself; leave it out (it gets \
filled in afterward). Do not include anything from the "already found" \
list again — those are already in the database. The raw text may contain \
nav menus, footers, and unrelated boilerplate — ignore anything that \
isn't a real dated event. If nothing here is a real dated event, call \
submit_events with an empty list rather than forcing a match.

Source-specific instructions, which take priority over anything above \
they narrow or extend:
{instructions}

When done, call submit_events exactly once with the full candidate list \
from this source."""


def _build_extract_user_message(text: str, structured_context: str, preference_summary: str) -> str:
    return f"""Source text:

{text}

Events already found deterministically (do not re-add these):

{structured_context or "(none found)"}

Preference context from recent voting history:

{preference_summary}

Extract the candidate list from this source and call submit_events."""


def _extract_from_source(source: dict, text: str, structured_context: str, preference_summary: str) -> list[dict]:
    if not text:
        return []
    text = text[:MAX_SOURCE_CHARS]

    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)
    instructions = _KIND_INSTRUCTIONS[source["kind"]].format(label=source["label"], url=source["url"])
    today = to_pacific(now_utc()).strftime("%A, %B %-d, %Y")
    system = _EXTRACT_SYSTEM_PROMPT_HEADER.format(instructions=instructions, today=today)

    messages = [
        {
            "role": "user",
            "content": _build_extract_user_message(text, structured_context, preference_summary),
        }
    ]

    def _turn(tool_choice, tools):
        return client.messages.create(
            model=EXTRACT_MODEL,
            max_tokens=4000,
            system=system,
            tools=tools,
            tool_choice=tool_choice,
            messages=messages,
        )

    try:
        # tool_choice forced onto submit_events reliably skips thinking on
        # this model (observed 0 thinking_tokens forced vs. ~2700 on auto)
        # and under-extracts long listings as a result — auto first, so it
        # actually reasons through the text, then a forced corrective retry
        # only if it didn't call submit_events at all. Same two-turn shape
        # app.research.claude_research uses for the same reason.
        response = with_retry(
            lambda: _turn({"type": "auto"}, [SUBMIT_EVENTS_TOOL]),
            what=f"deterministic_extract_turn1:{source['label']}",
            reraise=True,
        )
        submit = _find_tool_use(response, "submit_events")
        if submit is None:
            messages.append({"role": "assistant", "content": response.content})
            messages.append(
                {
                    "role": "user",
                    "content": "You didn't call submit_events. Call it now with the full candidate list.",
                }
            )
            response = with_retry(
                lambda: _turn({"type": "tool", "name": "submit_events"}, [SUBMIT_EVENTS_TOOL]),
                what=f"deterministic_extract_turn2:{source['label']}",
                reraise=True,
            )
            submit = _find_tool_use(response, "submit_events")
    except Exception:
        logger.exception("deterministic_extract_failed", extra={"job_fields": {"source": source["label"]}})
        return []

    events = submit.input.get("events", []) if submit is not None else []
    for event in events:
        if not isinstance(event, dict):
            continue
        # Set deterministically rather than trusting the model — it has no
        # reason to guess "source", and earlier versions of this prompt saw
        # it invent a literal "placeholder" string, and leave venue/url
        # null despite explicit fallback instructions.
        event["source"] = source["label"]
        if not event.get("url"):
            event["url"] = source["url"]
        if source["kind"] in ("venue", "comedy") and not event.get("venue"):
            event["venue"] = source["label"]
    return events


def _run_regex_parser(source: dict, text: str, preference_summary: str) -> list[dict]:
    parser = _PARSERS[source["parser"]]
    events = parser(text)
    for event in events:
        event["source"] = source["label"]
        event["url"] = source["url"]
        event["venue"] = source["label"]
    _generate_pitches(events, source["label"], preference_summary)
    return events


def run_deterministic_research_call(structured_context: str, preference_summary: str) -> list[dict]:
    """Same return shape as claude_research.run_research_call — raw event
    dicts from submit_events.input["events"].

    Per source: a regex parser (see SOURCES' "parser" key) is tried first
    when the fetch was plain HTTP — free, and proved far more reliable than
    LLM extraction on these three specific pages (see module docstring).
    If there's no parser, or the parser finds nothing (page format
    changed, or genuinely no near-term shows), falls back to one LLM
    extraction call per source — never one giant combined call, which
    proved unreliable at that scale in testing. Cross-source dedup still
    happens downstream in validate_candidate's seen_keys tracking, so
    splitting extraction doesn't lose that."""
    all_candidates: list[dict] = []
    for source in SOURCES:
        text, method = _get_source_text(source)
        logger.info(
            "deterministic_source_fetched",
            extra={"job_fields": {"source": source["label"], "method": method, "chars": len(text)}},
        )

        candidates: list[dict] = []
        used = "none"
        if source.get("parser") and method == "http":
            candidates = _run_regex_parser(source, text, preference_summary)
            used = "regex"
        if not candidates:
            candidates = _extract_from_source(source, text, structured_context, preference_summary)
            used = "llm"

        logger.info(
            "deterministic_source_extracted",
            extra={"job_fields": {"source": source["label"], "method": used, "candidates": len(candidates)}},
        )
        all_candidates.extend(candidates)
    return all_candidates
