"""Schedules said in words."""

from __future__ import annotations

from datetime import time

import pytest

from custom_components.trello_todo.schedule import parse_schedule

ALL = frozenset(range(7))


@pytest.mark.parametrize(
    ("text", "days", "at", "rest"),
    [
        ("every day, deadline 7 PM", ALL, time(19, 0), ""),
        ("Every day deadline 7pm", ALL, time(19, 0), ""),
        ("daily by 19:00", ALL, time(19, 0), ""),
        ("each day at 7:30am", ALL, time(7, 30), ""),
        ("Mondays and Thursdays by 8", frozenset({0, 3}), time(8, 0), ""),
        ("Mondays and Thursdays by 8pm", frozenset({0, 3}), time(20, 0), ""),
        ("every day by 6", ALL, time(18, 0), ""),
        ("mon, wed & fri at 07:15", frozenset({0, 2, 4}), time(7, 15), ""),
        ("on Tuesday", frozenset({1}), None, ""),
        ("weekdays before 8am", frozenset(range(5)), time(8, 0), ""),
        ("at the weekend by midday", frozenset({5, 6}), time(12, 0), ""),
        ("every weekday and saturday at 6pm", frozenset({0, 1, 2, 3, 4, 5}), time(18, 0), ""),
        ("Hinge is loose, needs a new bolt", None, None, "Hinge is loose, needs a new bolt"),
        ("2 bottles", None, None, "2 bottles"),
        ("every day, deadline 7 PM. Use the blue bin", ALL, time(19, 0), "Use the blue bin"),
        ("by 5pm", None, time(17, 0), ""),
        ("", None, None, ""),
        (None, None, None, ""),
    ],
)
def test_parse(text, days, at, rest) -> None:
    got = parse_schedule(text)
    assert (got.days, got.at, got.rest) == (days, at, rest)
