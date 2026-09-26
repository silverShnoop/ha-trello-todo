"""Trello boards as native Home Assistant to-do lists."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from aiohttp import web

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError

from .api import TrelloClient, TrelloError
from .const import (
    CONF_API_KEY,
    CONF_TOKEN,
    CONF_USE_WEBHOOK,
    CONF_WEBHOOK_ID,
    DOMAIN,
    POLL_INTERVAL,
    POLL_INTERVAL_WITH_WEBHOOK,
    WEBHOOK_DESCRIPTION,
)
from .coordinator import TrelloCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.TODO, Platform.SENSOR]


@dataclass(slots=True)
class RuntimeData:
    """Objects held for the life of a config entry."""

    coordinator: TrelloCoordinator
    webhook_registered: bool = False


type TrelloConfigEntry = ConfigEntry[RuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: TrelloConfigEntry) -> bool:
    """Set up from a config entry."""
    client = TrelloClient(
        async_get_clientsession(hass), entry.data[CONF_API_KEY], entry.data[CONF_TOKEN]
    )
    coordinator = TrelloCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = RuntimeData(coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if entry.options.get(CONF_USE_WEBHOOK, True):
        await _async_setup_webhook(hass, entry)
    else:
        # Switched off in options: stop Trello calling a URL nobody answers.
        try:
            await _async_sync_trello_webhooks(client, [], "")
        except TrelloError as err:
            _LOGGER.debug("Could not remove Trello webhooks: %s", err)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: TrelloConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: TrelloConfigEntry) -> bool:
    """Unload."""
    if entry.runtime_data.webhook_registered:
        webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID])
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: TrelloConfigEntry) -> None:
    """Tidy up Trello webhooks and any Nabu Casa cloudhook when the entry is deleted."""
    client = TrelloClient(
        async_get_clientsession(hass), entry.data[CONF_API_KEY], entry.data[CONF_TOKEN]
    )
    try:
        for hook in await client.get_webhooks():
            if hook.get("description") == WEBHOOK_DESCRIPTION:
                await client.delete_webhook(hook["id"])
    except TrelloError as err:
        _LOGGER.warning("Could not remove Trello webhooks: %s", err)
    if (webhook_id := entry.data.get(CONF_WEBHOOK_ID)) and "cloud" in hass.config.components:
        from homeassistant.components import cloud  # noqa: PLC0415

        try:
            await cloud.async_delete_cloudhook(hass, webhook_id)
        except Exception:  # noqa: BLE001
            pass


# ---- webhooks -------------------------------------------------------------


async def _async_callback_url(hass: HomeAssistant, webhook_id: str) -> str | None:
    """Prefer a Nabu Casa cloudhook; otherwise the external URL; else nothing."""
    if "cloud" in hass.config.components:
        from homeassistant.components import cloud  # noqa: PLC0415

        if cloud.async_active_subscription(hass):
            try:
                return await cloud.async_get_or_create_cloudhook(hass, webhook_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Cloudhook unavailable: %s", err)
    try:
        return webhook.async_generate_url(
            hass, webhook_id, allow_internal=False, prefer_external=True
        )
    except NoURLAvailableError:
        return None


async def _async_setup_webhook(hass: HomeAssistant, entry: TrelloConfigEntry) -> None:
    coordinator = entry.runtime_data.coordinator

    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if not webhook_id:
        webhook_id = webhook.async_generate_id()
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_WEBHOOK_ID: webhook_id}
        )

    async def handle(hass: HomeAssistant, _id: str, request: web.Request) -> web.Response:
        # The payload is only used as a "something changed" signal; we re-read
        # the board from the API, so a forged call can at most cause a refresh.
        await coordinator.async_request_refresh()
        return web.Response(status=200)

    webhook.async_register(
        hass, DOMAIN, "Trello To-do", webhook_id, handle, local_only=False
    )
    entry.runtime_data.webhook_registered = True

    url = await _async_callback_url(hass, webhook_id)
    if url is None:
        _LOGGER.info(
            "No external URL for Home Assistant; Trello changes will be picked up by polling every %s",
            POLL_INTERVAL,
        )
        return

    try:
        await _async_sync_trello_webhooks(coordinator.client, coordinator.board_ids, url)
    except TrelloError as err:
        _LOGGER.warning(
            "Trello could not reach %s, so falling back to polling every %s: %s",
            url,
            POLL_INTERVAL,
            err,
        )
        return

    coordinator.sync_mode = "webhook"
    coordinator.update_interval = POLL_INTERVAL_WITH_WEBHOOK
    coordinator.async_update_listeners()


async def _async_sync_trello_webhooks(
    client: TrelloClient, board_ids: list[str], url: str
) -> None:
    """Make Trello's webhooks for this token match the selected boards exactly."""
    existing = [
        h for h in await client.get_webhooks() if h.get("description") == WEBHOOK_DESCRIPTION
    ]
    have: set[str] = set()
    for hook in existing:
        if hook.get("callbackURL") == url and hook.get("idModel") in board_ids:
            have.add(hook["idModel"])
        else:
            await client.delete_webhook(hook["id"])
    for board_id in board_ids:
        if board_id not in have:
            await client.create_webhook(board_id, url, WEBHOOK_DESCRIPTION)
