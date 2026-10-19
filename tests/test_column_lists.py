"""A board of lists: every column is a to-do list of its own."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.trello_todo.const import CONF_COLUMN_LISTS, DOMAIN

from .conftest import API, BOARD_ID
from .test_integration import _calls, _setup

TODO = "todo.chores_to_do"
DOING = "todo.chores_doing"
DONE = "todo.chores_done"


def _as_columns(hass: HomeAssistant, entry) -> None:
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_COLUMN_LISTS: [BOARD_ID]}
    )


async def _items(hass: HomeAssistant, entity_id: str) -> list[dict]:
    out = await hass.services.async_call(
        "todo", "get_items", {"status": ["needs_action", "completed"]},
        target={"entity_id": entity_id}, blocking=True, return_response=True,
    )
    return out[entity_id]["items"]


async def test_one_entity_per_column(hass: HomeAssistant, trello, entry) -> None:
    """Each column is its own list, and a card in a column called Done is not done."""
    entry.add_to_hass(hass)
    _as_columns(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert hass.states.get("todo.chores") is None
    assert [i["uid"] for i in await _items(hass, TODO)] == ["c1"]
    assert [i["uid"] for i in await _items(hass, DOING)] == ["c2"]
    done = await _items(hass, DONE)
    assert [(i["uid"], i["status"]) for i in done] == [("c3", "needs_action")]
    assert hass.states.get(DONE).state == "1"

    board = hass.states.get(DOING).attributes["board"]
    assert board["done"] == board["default"] == "l_doing"
    assert [lst["id"] for lst in board["lists"]] == ["l_doing"]
    assert [c["id"] for c in board["cards"]] == ["c2"]
    assert hass.states.get(DOING).attributes["trello_list_id"] == "l_doing"

    # The board's counts cover every column, by tick rather than by column.
    assert hass.states.get("sensor.chores_open_tasks").state == "3"


async def test_tick_in_place_add_to_its_column(hass: HomeAssistant, trello, entry, board) -> None:
    """Ticking marks the due date complete without moving; new items land in that column."""
    trello.put(f"{API}/cards/c2", json={})
    trello.post(f"{API}/cards", json={"id": "new"})
    entry.add_to_hass(hass)
    _as_columns(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        "todo", "update_item", {"item": "Fix gate", "status": "completed"},
        target={"entity_id": DOING}, blocking=True,
    )
    (call,) = _calls(trello, "PUT", "/cards/c2")
    assert call[1].query["dueComplete"] == "true"
    assert "idList" not in call[1].query
    assert "due" in call[1].query  # something for Trello to show the tick on

    await hass.services.async_call(
        "todo", "add_item", {"item": "Ask about the shed"}, target={"entity_id": DOING}, blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "add_card", {"name": "Friday: Pizza"}, target={"entity_id": DONE}, blocking=True,
    )
    first, second = _calls(trello, "POST", "/cards")
    assert first[1].query["idList"] == "l_doing"
    assert second[1].query["idList"] == "l_done"


async def test_ticked_card_archived_after_rollover(hass: HomeAssistant, trello, entry, board, freezer) -> None:
    """On a board of lists a ticked one-off goes to the archive overnight."""
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to("2026-10-13T05:00:00+01:00")
    card = board["cards"][1]
    card.update(due="2026-10-12T17:00:00.000Z", dueComplete=True,
                dateLastActivity="2026-10-12T17:00:00.000Z")
    trello.put(f"{API}/cards/c2", json={})
    entry.add_to_hass(hass)
    _as_columns(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    (call,) = _calls(trello, "PUT", "/cards/c2")
    assert call[1].query["closed"] == "true"
    assert await _items(hass, DOING) == []


async def test_switching_shape_drops_the_old_entities(hass: HomeAssistant, trello, entry) -> None:
    """From one list to a list per column and back, nothing is left unavailable."""
    await _setup(hass, entry)
    registry = er.async_get(hass)
    assert registry.async_get("todo.chores") is not None

    _as_columns(hass, entry)
    await hass.async_block_till_done()
    assert registry.async_get("todo.chores") is None
    assert hass.states.get(TODO) is not None

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_COLUMN_LISTS: []}
    )
    await hass.async_block_till_done()
    assert registry.async_get(TODO) is None
    assert hass.states.get("todo.chores") is not None


async def test_new_column_appears_without_reload(hass: HomeAssistant, trello, entry, board) -> None:
    """A column added in Trello becomes a list at the next refresh."""
    entry.add_to_hass(hass)
    _as_columns(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("todo.chores_food_notes") is None

    board["lists"].append({"id": "l_food", "name": "Food notes", "pos": 4})
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("todo.chores_food_notes").state == "0"
