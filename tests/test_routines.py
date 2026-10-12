"""The rollover planner, as pure functions of a board and the clock."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from custom_components.trello_todo.api import TrelloBoard, TrelloCard, TrelloLabel
from custom_components.trello_todo.routines import (
    RoutineSettings,
    is_done,
    plan_rollover,
    routine_days,
)

TZ = ZoneInfo("Europe/London")
LABELS = [
    TrelloLabel("lb_mon", "Mon", "blue"),
    TrelloLabel("lb_wed", "Wednesday", "blue"),
    TrelloLabel("lb_daily", "Daily", "green"),
    TrelloLabel("lb_weekend", "Weekend", "green"),
    TrelloLabel("lb_garden", "Garden", "lime"),
]
SETTINGS = RoutineSettings(rollover=time(4, 0), default_time=time(18, 0), carry_over=True)


def card(cid: str, labels: list[str], due: datetime | None, done: bool = False) -> TrelloCard:
    return TrelloCard(
        id=cid, name=cid, desc="", due=due, due_complete=done, list_id="l_todo",
        member_ids=[], label_ids=labels, pos=1, url="",
    )


def board(*cards: TrelloCard) -> TrelloBoard:
    return TrelloBoard(id="b", name="Chores", url="", lists=[], cards=list(cards), labels=LABELS)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """October 2026: the 12th is a Monday."""
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def test_day_labels_make_a_routine() -> None:
    b = board(
        card("daily", ["lb_daily"], None),
        card("weekend", ["lb_weekend", "lb_garden"], None),
        card("plain", ["lb_garden"], None),
    )
    assert routine_days(b.cards[0], b) == frozenset(range(7))
    assert routine_days(b.cards[1], b) == frozenset({5, 6})
    assert routine_days(b.cards[2], b) is None


def test_done_means_ticked_for_a_routine_and_column_for_the_rest() -> None:
    b = board(card("r", ["lb_daily"], at(12, 19), done=True), card("p", [], None))
    assert is_done(b.cards[0], b, "l_done") is True
    assert is_done(b.cards[1], b, "l_done") is False
    b.cards[1].list_id = "l_done"
    assert is_done(b.cards[1], b, "l_done") is True


def test_new_routine_gets_its_first_deadline() -> None:
    b = board(card("bins", ["lb_mon"], None))
    (u,) = plan_rollover(b, at(12, 10), SETTINGS)  # Monday 10:00
    assert u.due == at(12, 18) and u.clear_done is False  # today, at the default time

    b = board(card("bins", ["lb_mon"], None))
    (u,) = plan_rollover(b, at(13, 10), SETTINGS)  # Tuesday
    assert u.due == at(19, 18)  # next Monday


def test_tick_holds_until_rollover_then_moves_on() -> None:
    b = board(card("cat", ["lb_daily"], at(12, 19), done=True))
    assert plan_rollover(b, at(12, 19, 30), SETTINGS) == []  # ticked at 19:30, stays ticked
    assert plan_rollover(b, at(13, 3, 59), SETTINGS) == []  # still before the boundary
    (u,) = plan_rollover(b, at(13, 4), SETTINGS)
    assert u.due == at(13, 19) and u.clear_done is True  # Tuesday 19:00, untick


def test_weekly_routine_skips_to_its_day() -> None:
    b = board(card("plants", ["lb_wed"], at(14, 9), done=True))  # Wednesday, done
    (u,) = plan_rollover(b, at(15, 4), SETTINGS)
    assert u.due == at(21, 9)  # next Wednesday, same time


def test_missed_routine_carries_over_by_default() -> None:
    b = board(card("bins", ["lb_mon"], at(12, 19)))  # not done
    assert plan_rollover(b, at(14, 10), SETTINGS) == []  # Wednesday: still overdue, left alone


def test_missed_routine_resets_when_asked() -> None:
    b = board(card("cat", ["lb_daily"], at(12, 19)))
    reset = RoutineSettings(carry_over=False)
    assert plan_rollover(b, at(12, 23), reset) == []  # overdue but the day hasn't rolled
    (u,) = plan_rollover(b, at(13, 4), reset)
    assert u.due == at(13, 19) and u.clear_done is False


def test_late_tick_counts_as_done_today() -> None:
    b = board(card("cat", ["lb_daily"], at(12, 19), done=True))  # Monday's, ticked Wednesday
    (u,) = plan_rollover(b, at(14, 10), SETTINGS)
    assert u.due == at(15, 19) and u.clear_done is True  # on to Thursday, not today again


def test_tick_a_day_late_after_the_deadline_skips_today() -> None:
    b = board(card("cat", ["lb_daily"], at(12, 19), done=True))  # Monday's, ticked Tuesday 20:00
    (u,) = plan_rollover(b, at(13, 20), SETTINGS)
    assert u.due == at(14, 19)  # Tuesday 19:00 is already past, so Wednesday


def test_relabelled_card_moves_to_a_matching_day() -> None:
    b = board(card("plants", ["lb_wed"], at(12, 9)))  # due Monday but labelled Wednesday
    (u,) = plan_rollover(b, at(12, 8), SETTINGS)
    assert u.due == at(14, 9) and u.clear_done is False


def test_idempotent_and_ignores_ordinary_cards() -> None:
    b = board(card("cat", ["lb_daily"], at(12, 19)), card("gate", ["lb_garden"], at(1, 9)))
    assert plan_rollover(b, at(12, 10), SETTINGS) == []
