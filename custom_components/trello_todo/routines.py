"""Recurring cards.

A card with a day label (Mon … Sun, Daily, Weekdays, Weekend) is a routine.
It never leaves its column: its due date always points at the next time it
is wanted, ticking it marks that due date complete, and once the day has
rolled over the due date is advanced to the next matching day and the tick
cleared. Everything here is a pure function of a card, the board's labels
and the clock, so it can be tested without Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from .api import TrelloBoard, TrelloCard

# Label name (lower-case, stripped) -> weekdays it stands for (Mon=0 … Sun=6).
DAY_LABELS: dict[str, frozenset[int]] = {
    **{n: frozenset({i}) for i, names in enumerate(
        (
            ("mon", "monday"),
            ("tue", "tues", "tuesday"),
            ("wed", "weds", "wednesday"),
            ("thu", "thur", "thurs", "thursday"),
            ("fri", "friday"),
            ("sat", "saturday"),
            ("sun", "sunday"),
        )
    ) for n in names},
    "daily": frozenset(range(7)),
    "every day": frozenset(range(7)),
    "everyday": frozenset(range(7)),
    "weekdays": frozenset(range(5)),
    "weekday": frozenset(range(5)),
    "weekend": frozenset({5, 6}),
    "weekends": frozenset({5, 6}),
}


DEFAULT_ROLLOVER = time(4, 0)
DEFAULT_ROUTINE_TIME = time(18, 0)


@dataclass(frozen=True, slots=True)
class RoutineSettings:
    """How the house wants routines to roll over."""

    rollover: time = DEFAULT_ROLLOVER
    """The day boundary. A tick made at 23:00 holds until this time next day."""
    default_time: time = DEFAULT_ROUTINE_TIME
    """Deadline for a routine card that has no due date yet."""
    carry_over: bool = True
    """A missed routine stays overdue until ticked; False resets it to its next day."""


def routine_days(card: TrelloCard, board: TrelloBoard) -> frozenset[int] | None:
    """Weekdays this card recurs on, or None if it is an ordinary card."""
    days: set[int] = set()
    names = {lb.id: lb.name for lb in board.labels}
    for label_id in card.label_ids:
        key = names.get(label_id, "").strip().lower()
        if key in DAY_LABELS:
            days |= DAY_LABELS[key]
    return frozenset(days) if days else None


def logical_date(now: datetime, settings: RoutineSettings) -> date:
    """The 'day' we are in, with the boundary at the rollover time rather than midnight."""
    shift = timedelta(hours=settings.rollover.hour, minutes=settings.rollover.minute)
    return (now - shift).date()


def boundary_after(moment: datetime, settings: RoutineSettings) -> datetime:
    """The first rollover instant strictly after `moment`."""
    candidate = datetime.combine(moment.date(), settings.rollover, tzinfo=moment.tzinfo)
    if candidate <= moment:
        candidate += timedelta(days=1)
    return candidate


def next_occurrence(
    days: frozenset[int], at: time, not_before: date, tz: tzinfo
) -> datetime:
    """The first `at` o'clock on a matching weekday on or after `not_before`."""
    day = not_before
    for _ in range(8):
        if day.weekday() in days:
            return datetime.combine(day, at, tzinfo=tz)
        day += timedelta(days=1)
    raise ValueError("no matching day")  # unreachable: days is non-empty


@dataclass(frozen=True, slots=True)
class RoutineUpdate:
    """What a rollover wants changed on a card."""

    card_id: str
    due: datetime | None
    clear_done: bool
    archive: bool = False
    """A ticked one-off whose day is over: it goes to Trello's archive."""


def plan_rollover(
    board: TrelloBoard,
    now: datetime,
    settings: RoutineSettings,
    done_list_id: str | None = None,
) -> list[RoutineUpdate]:
    """Cards whose due date should move on, and where to.

    Idempotent: run as often as you like, it only names cards that are stale.
    """
    tz = now.tzinfo
    today = logical_date(now, settings)
    updates: list[RoutineUpdate] = []
    for card in board.cards:
        days = routine_days(card, board)
        if days is None:
            # A one-off that ticks in place stays visible, ticked, for the
            # rest of the day it was ticked on -- the same rule as a
            # routine -- and then goes to the archive, where Trello keeps
            # it. The list reads as today's record and empties overnight.
            if card.due_complete and ticks_in_place(card, board, done_list_id):
                since = card.last_activity.astimezone(tz) if card.last_activity else (
                    card.due.astimezone(tz) if card.due else None
                )
                if since is None or now >= boundary_after(since, settings):
                    updates.append(RoutineUpdate(card.id, None, False, archive=True))
            continue
        if card.due is None:
            # Newly labelled: give it its first deadline.
            updates.append(
                RoutineUpdate(card.id, next_occurrence(days, settings.default_time, today, tz), False)
            )
            continue
        due = card.due.astimezone(tz)
        at = due.timetz().replace(tzinfo=None)
        if card.due_complete:
            # The tick stays visible until the day boundary after it was
            # made, then the card moves on. Trello does not say when the
            # tick happened, but the card's last activity is no earlier
            # than it, so holding until the boundary after that is right
            # for a tick on the day and for a late tick on a carried-over
            # card alike. (Editing a ticked card merely delays its
            # rollover to the next boundary, which costs nothing.)
            since = max(due, card.last_activity.astimezone(tz)) if card.last_activity else due
            if now < boundary_after(since, settings):
                continue
            earliest = max(due.date() + timedelta(days=1), today)
            nxt = next_occurrence(days, at, earliest, tz)
            if nxt <= now:
                # It would land in the past, so the tick must have come after
                # that time: today's is done too.
                nxt = next_occurrence(days, at, nxt.date() + timedelta(days=1), tz)
            updates.append(RoutineUpdate(card.id, nxt, True))
        elif due.weekday() not in days and due.date() >= today:
            # Labels changed under it: the due date is on a day it no longer runs.
            updates.append(RoutineUpdate(card.id, next_occurrence(days, at, today, tz), False))
        elif not settings.carry_over and now >= boundary_after(due, settings):
            # Missed, and the house would rather it reset than nag.
            earliest = max(due.date() + timedelta(days=1), today)
            updates.append(RoutineUpdate(card.id, next_occurrence(days, at, earliest, tz), False))
    return updates


def ticks_in_place(card: TrelloCard, board: TrelloBoard, done_list_id: str | None) -> bool:
    """Does finishing this card mean ticking it rather than moving it?

    Routines always tick in place. So does every card on a board with no
    separate Done column (one column, or Done and new-tasks the same):
    there is nowhere to move to, and "in the Done column" would make every
    card done the moment it was added.
    """
    if routine_days(card, board) is not None:
        return True
    return done_list_id is None or len(board.lists) < 2


def is_done(card: TrelloCard, board: TrelloBoard, done_list_id: str | None) -> bool:
    """Finished means: ticked, for a routine or on a one-column board; in
    the Done column, for anything else."""
    if ticks_in_place(card, board, done_list_id):
        return card.due_complete
    return card.list_id == done_list_id
