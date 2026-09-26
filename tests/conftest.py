"""Fixtures: a fake Trello board served through aioclient_mock."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.trello_todo.const import (
    CONF_API_KEY,
    CONF_BOARDS,
    CONF_DEFAULT_LISTS,
    CONF_DONE_LISTS,
    CONF_TOKEN,
    CONF_USE_WEBHOOK,
    DOMAIN,
)

API = "https://api.trello.com/1"
BOARD_ID = "board1"

BOARD: dict[str, Any] = {
    "id": BOARD_ID,
    "name": "Chores",
    "url": "https://trello.com/b/abc/chores",
    "lists": [
        {"id": "l_todo", "name": "To do", "pos": 1},
        {"id": "l_doing", "name": "Doing", "pos": 2},
        {"id": "l_done", "name": "Done", "pos": 3},
    ],
    "cards": [
        {
            "id": "c1", "name": "Bins out", "desc": "", "due": "2020-01-01T09:00:00.000Z",
            "dueComplete": False, "idList": "l_todo", "idMembers": ["m_james"],
            "idLabels": [], "pos": 100, "shortUrl": "https://trello.com/c/1",
        },
        {
            "id": "c2", "name": "Fix gate", "desc": "Hinge loose", "due": None,
            "dueComplete": False, "idList": "l_doing", "idMembers": [],
            "idLabels": ["lb1"], "pos": 200, "shortUrl": "https://trello.com/c/2",
        },
        {
            "id": "c3", "name": "Hoover", "desc": "", "due": None,
            "dueComplete": False, "idList": "l_done", "idMembers": [],
            "idLabels": [], "pos": 300, "shortUrl": "https://trello.com/c/3",
        },
    ],
    "members": [
        {"id": "m_james", "fullName": "James Barker", "initials": "JB", "username": "james", "avatarUrl": None},
        {"id": "m_wife", "fullName": "Sam Barker", "initials": "SB", "username": "sam", "avatarUrl": None},
    ],
    "labels": [{"id": "lb1", "name": "Garden", "color": "green"}],
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom_components."""
    return


@pytest.fixture
def board() -> dict[str, Any]:
    """Mutable copy of the board."""
    return copy.deepcopy(BOARD)


@pytest.fixture
def trello(aioclient_mock, board):
    """Serve the fake board and accept writes."""
    aioclient_mock.get(f"{API}/members/me", json={"id": "me1", "fullName": "James Barker", "username": "james"})
    aioclient_mock.get(f"{API}/members/me/boards", json=[{"id": BOARD_ID, "name": "Chores", "url": board["url"]}])
    aioclient_mock.get(f"{API}/boards/{BOARD_ID}/lists", json=board["lists"])
    aioclient_mock.get(f"{API}/boards/{BOARD_ID}", json=board)
    aioclient_mock.get(f"{API}/tokens/tok/webhooks", json=[])
    return aioclient_mock


@pytest.fixture
def entry() -> MockConfigEntry:
    """A configured entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="me1",
        title="Trello (James Barker)",
        data={CONF_API_KEY: "key", CONF_TOKEN: "tok"},
        options={
            CONF_BOARDS: [BOARD_ID],
            CONF_DONE_LISTS: {BOARD_ID: "l_done"},
            CONF_DEFAULT_LISTS: {BOARD_ID: "l_todo"},
            CONF_USE_WEBHOOK: True,
        },
    )
