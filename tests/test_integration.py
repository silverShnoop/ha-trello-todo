"""End-to-end tests against a fake Trello API."""

from __future__ import annotations

from http import HTTPStatus

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.trello_todo.const import (
    CONF_BOARDS,
    CONF_DEFAULT_LISTS,
    CONF_DONE_LISTS,
    CONF_USE_WEBHOOK,
    CONF_WEBHOOK_ID,
    DOMAIN,
)

from .conftest import API, BOARD_ID

ENTITY = "todo.chores"


def _calls(mock, method: str, path: str):
    return [c for c in mock.mock_calls if c[0].upper() == method and c[1].path == f"/1{path}"]


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_config_flow(hass: HomeAssistant, trello) -> None:
    """Key → token → boards → columns → entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": "key"})
    assert result["step_id"] == "token"
    assert "key=key" in result["description_placeholders"]["auth_url"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "tok"})
    assert result["step_id"] == "boards"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BOARDS: [BOARD_ID], CONF_USE_WEBHOOK: False}
    )
    assert result["step_id"] == "lists"
    assert result["description_placeholders"]["board"] == "Chores"
    # Defaults guessed: Done column = "Done", new tasks = first column.
    schema = {str(k): k.default() for k in result["data_schema"].schema}
    assert schema == {CONF_DONE_LISTS: "l_done", CONF_DEFAULT_LISTS: "l_todo"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DONE_LISTS: "l_done", CONF_DEFAULT_LISTS: "l_todo"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Trello (James Barker)"
    assert result["options"] == {
        CONF_BOARDS: [BOARD_ID],
        CONF_USE_WEBHOOK: False,
        CONF_DONE_LISTS: {BOARD_ID: "l_done"},
        CONF_DEFAULT_LISTS: {BOARD_ID: "l_todo"},
    }


async def test_config_flow_bad_token(hass: HomeAssistant, aioclient_mock) -> None:
    """A rejected token shows an error rather than crashing."""
    aioclient_mock.get(f"{API}/members/me", status=HTTPStatus.UNAUTHORIZED, text="invalid token")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": "key"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "bad"})
    assert result["errors"] == {"base": "invalid_auth"}


async def test_entities_and_state(hass: HomeAssistant, trello, entry) -> None:
    """One to-do per board; Done column = completed; attributes feed the card."""
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.state == "2"  # two cards not in Done
    assert state.attributes["sync_mode"] == "polling"  # no external URL in tests
    board = state.attributes["board"]
    assert [lst["name"] for lst in board["lists"]] == ["To do", "Doing", "Done"]
    assert board["done"] == "l_done"
    assert board["default"] == "l_todo"
    assert [c["id"] for c in board["cards"]] == ["c1", "c2", "c3"]
    assert board["cards"][0]["members"] == ["m_james"]
    assert board["members"][1]["name"] == "Sam Barker"

    items = await hass.services.async_call(
        "todo", "get_items", {}, target={"entity_id": ENTITY}, blocking=True, return_response=True
    )
    by_uid = {i["uid"]: i for i in items[ENTITY]["items"]}
    assert by_uid["c1"]["status"] == "needs_action"
    assert by_uid["c3"]["status"] == "completed"
    assert by_uid["c2"]["description"] == "Hinge loose"

    assert hass.states.get("sensor.chores_open_tasks") is not None, hass.states.async_entity_ids("sensor")
    assert hass.states.get("sensor.chores_open_tasks").state == "2"
    assert hass.states.get("sensor.chores_overdue_tasks").state == "1"


async def test_tick_moves_to_done(hass: HomeAssistant, trello, entry) -> None:
    """Completing an item moves its card to the Done column; un-ticking moves it back."""
    trello.put(f"{API}/cards/c1", json={})
    trello.put(f"{API}/cards/c3", json={})
    await _setup(hass, entry)

    await hass.services.async_call(
        "todo", "update_item", {"item": "Bins out", "status": "completed"},
        target={"entity_id": ENTITY}, blocking=True,
    )
    (call,) = _calls(trello, "PUT", "/cards/c1")
    assert call[1].query["idList"] == "l_done"
    assert "name" not in call[1].query  # untouched fields are not sent

    await hass.services.async_call(
        "todo", "update_item", {"item": "Hoover", "status": "needs_action"},
        target={"entity_id": ENTITY}, blocking=True,
    )
    (call,) = _calls(trello, "PUT", "/cards/c3")
    assert call[1].query["idList"] == "l_todo"


async def test_add_and_archive(hass: HomeAssistant, trello, entry) -> None:
    """New items land in the default column; removing archives rather than deletes."""
    trello.post(f"{API}/cards", json={"id": "new"})
    trello.put(f"{API}/cards/c2", json={})
    await _setup(hass, entry)

    await hass.services.async_call(
        "todo", "add_item", {"item": "Mow lawn", "due_date": "2026-10-01"},
        target={"entity_id": ENTITY}, blocking=True,
    )
    (call,) = _calls(trello, "POST", "/cards")
    assert call[1].query["idList"] == "l_todo"
    assert call[1].query["name"] == "Mow lawn"
    assert call[1].query["due"].startswith("2026-10-01T12:00:00")

    await hass.services.async_call(
        "todo", "remove_item", {"item": ["Fix gate"]}, target={"entity_id": ENTITY}, blocking=True
    )
    (call,) = _calls(trello, "PUT", "/cards/c2")
    assert call[1].query["closed"] == "true"


async def test_card_services(hass: HomeAssistant, trello, entry) -> None:
    """move_card / set_members / add_card resolve names to ids."""
    trello.put(f"{API}/cards/c1", json={})
    trello.post(f"{API}/cards/c1/idMembers", json=[])
    trello.delete(f"{API}/cards/c1/idMembers/m_james", json=[])
    trello.post(f"{API}/cards", json={"id": "new"})
    await _setup(hass, entry)

    await hass.services.async_call(
        DOMAIN, "move_card", {"card_id": "c1", "list": "doing"},
        target={"entity_id": ENTITY}, blocking=True,
    )
    (call,) = _calls(trello, "PUT", "/cards/c1")
    assert call[1].query["idList"] == "l_doing"

    await hass.services.async_call(
        DOMAIN, "set_members", {"card_id": "c1", "members": ["Sam"]},
        target={"entity_id": ENTITY}, blocking=True,
    )
    (add,) = _calls(trello, "POST", "/cards/c1/idMembers")
    assert add[1].query["value"] == "m_wife"
    assert len(_calls(trello, "DELETE", "/cards/c1/idMembers/m_james")) == 1

    await hass.services.async_call(
        DOMAIN, "add_card", {"name": "Paint fence", "list": "Doing", "members": ["james"]},
        target={"entity_id": ENTITY}, blocking=True,
    )
    (call,) = _calls(trello, "POST", "/cards")
    assert call[1].query["idList"] == "l_doing"
    assert call[1].query["idMembers"] == "m_james"


async def test_unknown_column_is_a_clear_error(hass: HomeAssistant, trello, entry) -> None:
    """Typos in a column name give a helpful message listing the real columns."""
    from homeassistant.exceptions import ServiceValidationError

    await _setup(hass, entry)
    with pytest.raises(ServiceValidationError, match="To do, Doing, Done"):
        await hass.services.async_call(
            DOMAIN, "move_card", {"card_id": "c1", "list": "Later"},
            target={"entity_id": ENTITY}, blocking=True,
        )


async def test_webhook_registration_and_refresh(hass: HomeAssistant, trello, entry, hass_client_no_auth) -> None:
    """With an external URL, a Trello webhook is created and deliveries trigger a refresh."""
    await async_setup_component(hass, "http", {})
    hass.config.external_url = "https://ha.example.co.uk"
    trello.post(f"{API}/webhooks", json={"id": "wh1"})
    await _setup(hass, entry)

    (call,) = _calls(trello, "POST", "/webhooks")
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    assert call[1].query["callbackURL"] == f"https://ha.example.co.uk/api/webhook/{webhook_id}"
    assert call[1].query["idModel"] == BOARD_ID
    assert hass.states.get(ENTITY).attributes["sync_mode"] == "webhook"

    before = len(_calls(trello, "GET", f"/boards/{BOARD_ID}"))
    client = await hass_client_no_auth()
    assert (await client.head(f"/api/webhook/{webhook_id}")).status == 200
    resp = await client.post(f"/api/webhook/{webhook_id}", json={"action": {"type": "updateCard"}})
    assert resp.status == 200
    await hass.async_block_till_done()
    assert len(_calls(trello, "GET", f"/boards/{BOARD_ID}")) == before + 1


async def test_auth_failure_starts_reauth(hass: HomeAssistant, aioclient_mock, entry) -> None:
    """A revoked token puts the entry into reauth instead of failing silently."""
    aioclient_mock.get(f"{API}/boards/{BOARD_ID}", status=HTTPStatus.UNAUTHORIZED, text="unauthorized")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert flows and flows[0]["context"]["source"] == "reauth"
