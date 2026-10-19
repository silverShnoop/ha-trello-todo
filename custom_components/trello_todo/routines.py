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


def first_occurrence(
    days: frozenset[int], at: time, now: datetime, settings: RoutineSettings
) -> datetime:
    """A brand-new routine's first deadline: the next matching time still
    ahead of us. Today's, if today matches and the hour has not gone;
    otherwise the next day it runs. A card born already overdue is a nag
    for something nobody was asked to do."""
    nxt = next_occurrence(days, at, logical_date(now, settings), now.tzinfo)
    if nxt <= now:
        nxt = next_occurrence(days, at, nxt.date() + timedelta(days=1), now.tzinfo)
    return nxt


@dataclass(frozen=True, slots=True)
class RoutineUpdate:
    """What a rollover wants changed on a card."""

    card_id: str
    due: datetime | None
    clear_done: bool
    archive: bool = False
    """A ticked one-off whose day is over: it goes to Trello's archive."""
    start: datetime | None = None
    """The start date to set beside the new due date, when the card has one."""


def start_for(start: datetime | None, old_due: datetime | None, new_due: datetime) -> datetime | None:
    """Where a card's start goes when its due date moves to `new_due`.

    The window keeps its shape: a card that starts asking at 20:00 for a
    22:30 deadline starts at 20:00 on the new day too. Without an old due
    date to measure from, the start keeps its time of day, on the due day,
    or the day before when that time is after the deadline. A start after
    the due date, or more than a week before it, is not a window anybody
    meant, and is left where it is.
    """
    if start is None:
        return None
    tz = new_due.tzinfo
    start = start.astimezone(tz)
    if old_due is not None:
        lead = old_due.astimezone(tz) - start
        if timedelta(0) <= lead <= timedelta(days=7):
            return new_due - lead
        return None
    candidate = datetime.combine(new_due.date(), start.timetz().replace(tzinfo=None), tzinfo=tz)
    return candidate if candidate <= new_due else candidate - timedelta(days=1)


def window_start(start_at: time | None, due: datetime) -> datetime | None:
    """The start date for a new card whose window opens at `start_at`."""
    if start_at is None:
        return None
    candidate = datetime.combine(due.date(), start_at, tzinfo=due.tzinfo)
    return candidate if candidate <= due else candidate - timedelta(days=1)


def plan_rollover(
    board: TrelloBoard,
    now: datetime,
    settings: RoutineSettings,
    done_list_id: str | None = None,
    columns: bool = False,
) -> list[RoutineUpdate]:
    """Cards whose due date should move on, and where to.

    Idempotent: run as often as you like, it only names cards that are stale.
    `columns` is a board whose every column is a list of its own.
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
            if card.due_complete and ticks_in_place(card, board, done_list_id, columns):
                since = card.last_activity.astimezone(tz) if card.last_activity else (
                    card.due.astimezone(tz) if card.due else None
                )
                if since is None or now >= boundary_after(since, settings):
                    updates.append(RoutineUpdate(card.id, None, False, archive=True))
            continue
        if card.due is None:
            # Newly labelled: give it its first deadline.
            due = first_occurrence(days, settings.default_time, now, settings)
            updates.append(
                RoutineUpdate(card.id, due, False, start=start_for(card.start, None, due))
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
            updates.append(RoutineUpdate(card.id, nxt, True, start=start_for(card.start, due, nxt)))
        elif due.weekday() not in days and due.date() >= today:
            # Labels changed under it: the due date is on a day it no longer runs.
            nxt = next_occurrence(days, at, today, tz)
            updates.append(RoutineUpdate(card.id, nxt, False, start=start_for(card.start, due, nxt)))
        elif not settings.carry_over and now >= boundary_after(due, settings):
            # Missed, and the house would rather it reset than nag.
            earliest = max(due.date() + timedelta(days=1), today)
            nxt = next_occurrence(days, at, earliest, tz)
            updates.append(RoutineUpdate(card.id, nxt, False, start=start_for(card.start, due, nxt)))
    return updates


def ticks_in_place(
    card: TrelloCard, board: TrelloBoard, done_list_id: str | None, columns: bool = False
) -> bool:
    """Does finishing this card mean ticking it rather than moving it?

    Routines always tick in place. So does every card on a board with no
    separate Done column (one column, or Done and new-tasks the same):
    there is nowhere to move to, and "in the Done column" would make every
    card done the moment it was added. And so does every card on a board
    whose columns are lists of their own (`columns`): each column is a
    one-column board as far as its cards are concerned, and moving a card
    to another column would put it on another list, not finish it.
    """
    if columns or routine_days(card, board) is not None:
        return True
    return done_list_id is None or len(board.lists) < 2


def is_done(
    card: TrelloCard, board: TrelloBoard, done_list_id: str | None, columns: bool = False
) -> bool:
    """Finished means: ticked, for a routine, on a one-column board or on a
    board of column lists; in the Done column, for anything else."""
    if ticks_in_place(card, board, done_list_id, columns):
        return card.due_complete
    return card.list_id == done_list_id
