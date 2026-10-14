"""The rollover planner, as pure functions of a board and the clock."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from custom_components.trello_todo.api import TrelloBoard, TrelloCard, TrelloLabel, TrelloList
from custom_components.trello_todo.routines import (
    RoutineSettings,
    is_done,
    plan_rollover,
    routine_days,
    start_for,
    window_start,
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


def card(
    cid: str, labels: list[str], due: datetime | None, done: bool = False,
    touched: datetime | None = None,
) -> TrelloCard:
    return TrelloCard(
        id=cid, name=cid, desc="", due=due, due_complete=done, list_id="l_todo",
        member_ids=[], label_ids=labels, pos=1, url="", last_activity=touched,
    )


LISTS = [TrelloList("l_todo", "To do", 1), TrelloList("l_done", "Done", 2)]


def board(*cards: TrelloCard, lists: list[TrelloList] = LISTS) -> TrelloBoard:
    return TrelloBoard(id="b", name="Chores", url="", lists=lists, cards=list(cards), labels=LABELS)


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
    (u,) = plan_rollover(b, at(12, 20), SETTINGS)  # Monday 20:00: today's 18:00 has gone
    assert u.due == at(19, 18)  # so next Monday, not born overdue

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


def test_late_tick_stays_visible_for_the_day_it_was_made() -> None:
    # Monday's card, carried over, ticked Wednesday 10:00.
    b = board(card("cat", ["lb_daily"], at(12, 19), done=True, touched=at(14, 10)))
    assert plan_rollover(b, at(14, 12), SETTINGS) == []  # still ticked on Wednesday
    assert plan_rollover(b, at(15, 3), SETTINGS) == []
    (u,) = plan_rollover(b, at(15, 4), SETTINGS)
    assert u.due == at(15, 19) and u.clear_done is True  # Thursday's, after the boundary


def test_late_tick_with_no_activity_time_moves_on_at_once() -> None:
    # Without a last-activity time (older API data) there is nothing to hold on.
    b = board(card("cat", ["lb_daily"], at(12, 19), done=True))
    (u,) = plan_rollover(b, at(14, 10), SETTINGS)
    assert u.due == at(14, 19)


def test_tick_a_day_late_after_the_deadline_skips_today() -> None:
    # Monday's card ticked Tuesday 20:00: Tuesday 19:00 is already past.
    b = board(card("cat", ["lb_daily"], at(12, 19), done=True, touched=at(13, 20)))
    assert plan_rollover(b, at(13, 21), SETTINGS) == []
    (u,) = plan_rollover(b, at(14, 4), SETTINGS)
    assert u.due == at(14, 19)


def test_editing_a_ticked_card_does_not_untick_it_early() -> None:
    b = board(card("cat", ["lb_daily"], at(12, 19), done=True, touched=at(12, 19, 30)))
    assert plan_rollover(b, at(12, 23), SETTINGS) == []
    (u,) = plan_rollover(b, at(13, 4), SETTINGS)
    assert u.due == at(13, 19)


def test_relabelled_card_moves_to_a_matching_day() -> None:
    b = board(card("plants", ["lb_wed"], at(12, 9)))  # due Monday but labelled Wednesday
    (u,) = plan_rollover(b, at(12, 8), SETTINGS)
    assert u.due == at(14, 9) and u.clear_done is False


def test_idempotent_and_ignores_ordinary_cards() -> None:
    b = board(card("cat", ["lb_daily"], at(12, 19)), card("gate", ["lb_garden"], at(1, 9)))
    assert plan_rollover(b, at(12, 10), SETTINGS) == []


def test_one_column_board_ticks_everything_in_place() -> None:
    one = [TrelloList("l_todo", "Todo", 1)]
    b = board(card("gate", [], None), card("paid", [], at(12, 10), done=True), lists=one)
    assert is_done(b.cards[0], b, "l_todo") is False  # being in the only column is not done
    assert is_done(b.cards[1], b, "l_todo") is True


def test_ticked_one_off_is_archived_after_its_day() -> None:
    one = [TrelloList("l_todo", "Todo", 1)]
    b = board(card("paid", [], at(12, 10), done=True, touched=at(12, 10)), lists=one)
    assert plan_rollover(b, at(12, 23), SETTINGS, "l_todo") == []  # still today's record
    (u,) = plan_rollover(b, at(13, 4), SETTINGS, "l_todo")
    assert u.archive is True and u.card_id == "paid"
    # On a board with a Done column, one-offs move there and are left alone.
    b2 = board(card("paid", [], at(12, 10), done=True, touched=at(12, 10)))
    assert plan_rollover(b2, at(13, 4), SETTINGS, "l_done") == []


def test_rollover_carries_the_start_with_the_due_date() -> None:
    # The dishwasher: asks from 20:00, due 22:30, ticked on Monday.
    c = card("dishes", ["lb_daily"], at(12, 22, 30), done=True, touched=at(12, 21, 14))
    c.start = at(12, 20)
    (u,) = plan_rollover(board(c), at(13, 4), SETTINGS)
    assert u.due == at(13, 22, 30)
    assert u.start == at(13, 20)


def test_rollover_leaves_a_card_without_a_start_alone() -> None:
    (u,) = plan_rollover(board(card("cat", ["lb_daily"], at(12, 19), done=True)), at(13, 4), SETTINGS)
    assert u.start is None


def test_start_for_keeps_the_window_shape_and_ignores_nonsense() -> None:
    assert start_for(at(12, 20), at(12, 22, 30), at(19, 22, 30)) == at(19, 20)
    assert start_for(at(12, 23), at(12, 22, 30), at(13, 22, 30)) is None  # after the deadline
    assert start_for(None, at(12, 22), at(13, 22)) is None
    # No old deadline to measure from: keep the time of day, before the deadline.
    assert start_for(at(1, 20), None, at(14, 22, 30)) == at(14, 20)
    assert start_for(at(1, 23), None, at(14, 7)) == at(13, 23)


def test_window_start_opens_before_the_deadline() -> None:
    assert window_start(time(20, 0), at(14, 22, 30)) == at(14, 20)
    assert window_start(time(23, 0), at(14, 1)) == at(13, 23)  # overnight window
    assert window_start(None, at(14, 22)) is None
