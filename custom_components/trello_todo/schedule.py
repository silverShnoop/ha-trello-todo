"""Reading a schedule out of plain words.

A task added from Home Assistant -- by voice, or typed -- arrives as a name
and a description, and people say the schedule in the description: "every
day, deadline 7 PM", "Mondays and Thursdays by 8", "weekdays at 07:30".
This turns that into the days and the time the routine machinery wants,
and hands back whatever words were left over as the real description.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
import re

DAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
DAY_WORD = r"(?:mon|tues?|wed(?:ne)?s?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?s?"

# A list of days: "mondays and thursdays", "mon, wed & fri", "on tuesday".
_DAY_LIST = re.compile(
    rf"\b(?:(?:on|every|each)\s+)?({DAY_WORD}(?:\s*(?:,|and|&|\+)\s*{DAY_WORD})*)\b",
    re.I,
)
_ALL = re.compile(r"\b(?:every\s*day|everyday|daily|each\s+day|all\s+week)\b", re.I)
_WEEKDAYS = re.compile(r"\b(?:on\s+)?(?:weekdays?|every\s+weekday|work\s*days?|school\s*days?)\b", re.I)
_WEEKEND = re.compile(r"\b(?:on\s+|at\s+|every\s+)?(?:the\s+)?weekends?\b", re.I)
# "by 7pm", "deadline 7 PM", "at 19:30", "before 8", "due 6.45pm", "by midday".
_TIME = re.compile(
    r"\b(?:by|at|before|deadline(?:\s+(?:is|of|at|by))?|due(?:\s+(?:at|by))?|until)?\s*[:\-]?\s*"
    r"(?:(?P<noon>noon|midday|mid-day)|(?P<midnight>midnight)"
    r"|(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?)?"
    r"(?:\s*(?:o'?clock|hrs?|hours?))?)\b",
    re.I,
)
_TIME_CUE = re.compile(r"\b(?:by|at|before|deadline|due|until|o'?clock|am|pm|noon|midday|midnight)\b", re.I)


@dataclass(frozen=True, slots=True)
class Schedule:
    """What the words said."""

    days: frozenset[int] | None
    at: time | None
    rest: str
    """The description with the schedule taken out of it ("" if nothing else)."""


def _days_from_list(text: str) -> frozenset[int]:
    found: set[int] = set()
    for word in re.findall(DAY_WORD, text, re.I):
        key = word.lower().rstrip("s") if word.lower().endswith("days") else word.lower()
        key = key[:-1] if key.endswith("s") and key[:-1] in DAYS else key
        if key in DAYS:
            found.add(DAYS[key])
        elif key.rstrip("s") in DAYS:
            found.add(DAYS[key.rstrip("s")])
    return frozenset(found)


def _time_from(m: re.Match[str]) -> time | None:
    if m.group("noon"):
        return time(12, 0)
    if m.group("midnight"):
        return time(0, 0)
    h = int(m.group("h"))
    minute = int(m.group("m") or 0)
    ap = (m.group("ap") or "").replace(".", "").lower()
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    elif not ap and not m.group("m") and h <= 6:
        # "by 7" with no am/pm in a house of evening deadlines: a bare small
        # number is far more often 19:00 than 07:00. Explicit minutes
        # ("7:30") or am/pm say what they mean and are left alone.
        h += 12
    if not 0 <= h <= 23 or not 0 <= minute <= 59:
        return None
    return time(h, minute)


def parse_schedule(text: str | None) -> Schedule:
    """Pull the days and the deadline out of a description."""
    if not text:
        return Schedule(None, None, "")
    rest = text
    days: set[int] = set()
    is_routine = False

    for pattern, picked in ((_ALL, range(7)), (_WEEKDAYS, range(5)), (_WEEKEND, (5, 6))):
        if pattern.search(rest):
            days |= set(picked)
            is_routine = True
            rest = pattern.sub(" ", rest)

    m = _DAY_LIST.search(rest)
    if m:
        listed = _days_from_list(m.group(1))
        if listed:
            days |= listed
            is_routine = True
            rest = rest[: m.start()] + " " + rest[m.end():]

    at: time | None = None
    for m in _TIME.finditer(rest):
        # A bare number with nothing around it is not a time ("2 bottles").
        if not m.group("noon") and not m.group("midnight") and not m.group("ap") and not m.group("m"):
            window = rest[max(0, m.start() - 12): m.end() + 10]
            if not _TIME_CUE.search(window):
                continue
        at = _time_from(m)
        if at is not None:
            rest = rest[: m.start()] + " " + rest[m.end():]
            break

    # Joining words left stranded once the schedule is taken out of
    # "every weekday and saturday".
    rest = re.sub(r"^(?:\s*(?:and|&|\+|on|every|each)\b)+|(?:\b(?:and|&|\+|on|every|each)\s*)+$", "", rest.strip(), flags=re.I)
    rest = re.sub(r"\s*[,;.\-–]\s*(?=[,;.\-–]|$)", "", rest)
    rest = re.sub(r"^\s*[,;.\-–]+\s*|\s*[,;.\-–]+\s*$", "", rest)
    rest = re.sub(r"\s{2,}", " ", rest).strip()
    return Schedule(frozenset(days) if is_routine else None, at, rest)
