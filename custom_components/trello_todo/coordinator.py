"""Data coordinator: keeps every selected board in memory and in sync."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TrelloAuthError, TrelloBoard, TrelloClient, TrelloError
from .const import (
    CONF_BOARDS,
    CONF_DEFAULT_LISTS,
    CONF_DONE_LISTS,
    DOMAIN,
    POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class BoardSettings:
    """Per-board choices made in the config flow."""

    done_list_id: str | None
    default_list_id: str | None


class TrelloCoordinator(DataUpdateCoordinator[dict[str, TrelloBoard]]):
    """Fetch all selected boards in parallel."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: TrelloClient) -> None:
        """Initialise."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=POLL_INTERVAL,
        )
        self.client = client
        self.sync_mode = "polling"

    @property
    def board_ids(self) -> list[str]:
        """Boards the user picked."""
        return list(self.config_entry.options.get(CONF_BOARDS, []))

    def settings(self, board_id: str) -> BoardSettings:
        """Return Done / default list choices for a board."""
        opts = self.config_entry.options
        return BoardSettings(
            done_list_id=opts.get(CONF_DONE_LISTS, {}).get(board_id),
            default_list_id=opts.get(CONF_DEFAULT_LISTS, {}).get(board_id),
        )

    def done_list_id(self, board: TrelloBoard) -> str | None:
        """The list that counts as finished; falls back to a list named 'Done'."""
        chosen = self.settings(board.id).done_list_id
        if chosen and board.list_by_id(chosen):
            return chosen
        for lst in board.lists:
            if lst.name.strip().lower() in ("done", "completed", "finished"):
                return lst.id
        return board.lists[-1].id if board.lists else None

    def default_list_id(self, board: TrelloBoard) -> str | None:
        """Where new tasks go; falls back to the first non-Done list."""
        chosen = self.settings(board.id).default_list_id
        if chosen and board.list_by_id(chosen):
            return chosen
        done = self.done_list_id(board)
        return next((lst.id for lst in board.lists if lst.id != done), None)

    def device_info(self, board_id: str) -> DeviceInfo:
        """One device per board, shared by the to-do and its sensors.

        Every entity passes the full info, because platforms set up in
        parallel and whichever registers first names the device.
        """
        board = self.data[board_id]
        return DeviceInfo(
            identifiers={(DOMAIN, board_id)},
            name=board.name,
            manufacturer="Trello",
            model="Board",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=board.url or None,
        )

    async def _async_update_data(self) -> dict[str, TrelloBoard]:
        try:
            boards = await asyncio.gather(
                *(self.client.get_board(board_id) for board_id in self.board_ids)
            )
        except TrelloAuthError as err:
            raise ConfigEntryAuthFailed("Trello rejected the API key or token") from err
        except TrelloError as err:
            raise UpdateFailed(f"Error talking to Trello: {err}") from err
        return {board.id: board for board in boards}
