"""Start dates, and chores that tick themselves from a sensor."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.trello_todo.const import CONF_AUTOTICK, DOMAIN

from .conftest import API, BOARD_ID

ENTITY = "todo.chores"
SENSOR = "sensor.dishwasher"
LINK = {"board": BOARD_ID, "card": "dw", "entity": SENSOR, "states": ["running"]}


def _calls(mock, method: str, path: str):
    return [c for c in mock.mock_calls if c[0].upper() == method and c[1].path == f"/1{path}"]


def _dishwasher(board, *, due="2026-10-14T21:30:00.000Z", start="2026-10-14T19:00:00.000Z", done=False):
    board["labels"].append({"id": "lb_daily", "name": "Daily", "color": "green"})
    board["cards"].append({
        "id": "dw", "name": "Run the dishwasher", "desc": "", "due": due, "start": start,
        "dueComplete": done, "idList": "l_todo", "idMembers": [], "idLabels": ["lb_daily"],
        "pos": 10, "shortUrl": "https://trello.com/c/dw",
    })


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, links=None) -> None:
    entry.add_to_hass(hass)
    if links is not None:
        hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_AUTOTICK: links})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _card(hass: HomeAssistant, cid: str = "dw") -> dict:
    return next(c for c in hass.states.get(ENTITY).attributes["board"]["cards"] if c["id"] == cid)


@pytest.fixture(autouse=True)
async def _london(hass: HomeAssistant, freezer) -> None:
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to("2026-10-14T18:00:00+01:00")  # Wednesday, before the window


async def test_start_is_on_the_board_attribute(hass: HomeAssistant, trello, entry, board) -> None:
    _dishwasher(board)
    await _setup(hass, entry)
    c = _card(hass)
    assert c["start"].startswith("2026-10-14T19:00:00+00:00")
    assert c["ticked_by"] is None


async def test_add_card_with_a_start_time(hass: HomeAssistant, trello, entry, board) -> None:
    trello.post(f"{API}/labels", json={"id": "lb_new", "name": "Monday", "color": "green"})
    trello.post(f"{API}/cards", json={"id": "new"})
    await _setup(hass, entry)
    await hass.services.async_call(
        DOMAIN, "add_card",
        {"name": "Run the dishwasher", "days": ["daily"], "time": "22:30", "start": "20:00"},
        target={"entity_id": ENTITY}, blocking=True,
    )
    (card,) = _calls(trello, "POST", "/cards")
    assert card[1].query["due"].startswith("2026-10-14T22:30:00+01:00")
    assert card[1].query["start"].startswith("2026-10-14T20:00:00+01:00")

    # Without one, no start goes to Trello: the card asks from the start of its day.
    await hass.services.async_call(
        DOMAIN, "add_card", {"name": "Feed the cat", "days": ["daily"], "time": "19:00"},
        target={"entity_id": ENTITY}, blocking=True,
    )
    (_, card) = _calls(trello, "POST", "/cards")
    assert "start" not in card[1].query


async def test_a_start_after_the_deadline_is_refused(hass: HomeAssistant, trello, entry) -> None:
    from homeassistant.exceptions import ServiceValidationError

    await _setup(hass, entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "add_card",
            {"name": "Odd", "due": "2026-10-15T10:00:00", "start": "2026-10-15T12:00:00"},
            target={"entity_id": ENTITY}, blocking=True,
        )


async def test_moving_the_deadline_moves_the_start(hass: HomeAssistant, trello, entry, board) -> None:
    _dishwasher(board)
    trello.put(f"{API}/cards/dw", json={})
    await _setup(hass, entry)
    await hass.services.async_call(
        "todo", "update_item", {"item": "dw", "due_datetime": "2026-10-15 22:30:00"},
        target={"entity_id": ENTITY}, blocking=True,
    )
    (call,) = _calls(trello, "PUT", "/cards/dw")
    assert call[1].query["start"].startswith("2026-10-15T20:00:00+01:00")


async def test_running_ticks_it_and_says_what_did(hass: HomeAssistant, trello, entry, board) -> None:
    _dishwasher(board)
    trello.put(f"{API}/cards/dw", json={})
    hass.states.async_set(SENSOR, "idle", {"friendly_name": "Dishwasher"})
    await _setup(hass, entry, [LINK])

    hass.states.async_set(SENSOR, "running", {"friendly_name": "Dishwasher"})
    await hass.async_block_till_done()
    (call,) = _calls(trello, "PUT", "/cards/dw")
    assert call[1].query["dueComplete"] == "true"

    board["cards"][-1]["dueComplete"] = True
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    c = _card(hass)
    assert c["done"] is True
    assert c["ticked_by"]["by"] == "Dishwasher"

    # Still running, nothing more happens.
    hass.states.async_set(SENSOR, "running", {"friendly_name": "Dishwasher", "x": 1})
    await hass.async_block_till_done()
    assert len(_calls(trello, "PUT", "/cards/dw")) == 1


async def test_a_run_before_the_window_still_counts(hass: HomeAssistant, trello, entry, board, freezer) -> None:
    freezer.move_to("2026-10-14T14:00:00+01:00")
    _dishwasher(board)
    trello.put(f"{API}/cards/dw", json={})
    hass.states.async_set(SENSOR, "idle")
    await _setup(hass, entry, [LINK])
    hass.states.async_set(SENSOR, "running")
    await hass.async_block_till_done()
    assert len(_calls(trello, "PUT", "/cards/dw")) == 1


async def test_not_ticked_when_not_due_until_a_later_day(hass: HomeAssistant, trello, entry, board) -> None:
    _dishwasher(board, due="2026-10-15T21:30:00.000Z", start="2026-10-15T19:00:00.000Z")
    trello.put(f"{API}/cards/dw", json={})
    hass.states.async_set(SENSOR, "idle")
    await _setup(hass, entry, [LINK])
    hass.states.async_set(SENSOR, "running")
    await hass.async_block_till_done()
    assert not _calls(trello, "PUT", "/cards/dw")


async def test_unticked_by_hand_waits_for_the_next_run(hass: HomeAssistant, trello, entry, board) -> None:
    _dishwasher(board, done=True)
    trello.put(f"{API}/cards/dw", json={})
    hass.states.async_set(SENSOR, "running")
    await _setup(hass, entry, [LINK])

    await hass.services.async_call(
        "todo", "update_item", {"item": "dw", "status": "needs_action"},
        target={"entity_id": ENTITY}, blocking=True,
    )
    board["cards"][-1]["dueComplete"] = False
    await entry.runtime_data.coordinator.async_refresh()
    puts = len(_calls(trello, "PUT", "/cards/dw"))

    # Restart mid-run: the run already in progress does not re-tick it.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(_calls(trello, "PUT", "/cards/dw")) == puts

    # A fresh run does.
    hass.states.async_set(SENSOR, "idle")
    hass.states.async_set(SENSOR, "running")
    await hass.async_block_till_done()
    assert len(_calls(trello, "PUT", "/cards/dw")) == puts + 1


async def test_already_running_at_start_counts(hass: HomeAssistant, trello, entry, board) -> None:
    _dishwasher(board)
    trello.put(f"{API}/cards/dw", json={})
    hass.states.async_set(SENSOR, "running")
    await _setup(hass, entry, [LINK])
    await hass.async_block_till_done()
    assert len(_calls(trello, "PUT", "/cards/dw")) == 1


async def test_options_add_and_remove_a_link(hass: HomeAssistant, trello, entry, board) -> None:
    _dishwasher(board)
    hass.states.async_set(SENSOR, "idle", {"friendly_name": "Dishwasher"})
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "autotick"}
    )
    assert result["step_id"] == "autotick"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"card": f"{BOARD_ID}|dw", "entity": SENSOR, "states": "running"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_AUTOTICK] == [LINK]
    assert entry.options["boards"] == [BOARD_ID]  # the rest is kept

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "autotick"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"keep": []})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_AUTOTICK] == []


async def test_settings_path_keeps_the_links(hass: HomeAssistant, trello, entry, board) -> None:
    await _setup(hass, entry, [LINK])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["step_id"] == "boards"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"boards": [BOARD_ID], "use_webhook": True}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"done_lists": "l_done", "default_lists": "l_todo"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"rollover": "04:00:00", "routine_default_time": "18:00:00", "carry_over": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_AUTOTICK] == [LINK]
