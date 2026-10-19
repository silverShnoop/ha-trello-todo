"""Data coordinator: keeps every selected board in memory and in sync."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import time
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from homeassistant.util import dt as dt_util

from .autotick import AutoTick
from .api import TrelloAuthError, TrelloBoard, TrelloCard, TrelloClient, TrelloError
from .const import (
    CONF_BOARDS,
    CONF_CARRY_OVER,
    CONF_COLUMN_LISTS,
    CONF_DEFAULT_LISTS,
    CONF_DONE_LISTS,
    CONF_ROLLOVER,
    CONF_ROUTINE_DEFAULT_TIME,
    DOMAIN,
    POLL_INTERVAL,
)
from .routines import (
    DEFAULT_ROLLOVER,
    DEFAULT_ROUTINE_TIME,
    RoutineSettings,
    is_done,
    plan_rollover,
    routine_days,
    ticks_in_place,
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
        self.autotick = AutoTick(hass, self)

    @property
    def ticked_by(self) -> dict[str, dict[str, str]]:
        """Card id -> what ticked it and when, for cards a sensor ticked."""
        return self.autotick.ticked_by

    @property
    def routine_settings(self) -> RoutineSettings:
        """How routines roll over, from the options."""
        opts = self.config_entry.options
        return RoutineSettings(
            rollover=_parse_time(opts.get(CONF_ROLLOVER), DEFAULT_ROLLOVER),
            default_time=_parse_time(
                opts.get(CONF_ROUTINE_DEFAULT_TIME), DEFAULT_ROUTINE_TIME
            ),
            carry_over=bool(opts.get(CONF_CARRY_OVER, True)),
        )

    def is_routine(self, card: TrelloCard, board: TrelloBoard) -> bool:
        """Does this card recur?"""
        return routine_days(card, board) is not None

    def is_done(self, card: TrelloCard, board: TrelloBoard) -> bool:
        """Ticked (routine, or any card on a one-column board) or in the Done column."""
        return is_done(card, board, self.done_list_id(board), self.columns_are_lists(board.id))

    def ticks_in_place(self, card: TrelloCard, board: TrelloBoard) -> bool:
        """Is finishing this card a tick rather than a move?"""
        return ticks_in_place(
            card, board, self.done_list_id(board), self.columns_are_lists(board.id)
        )

    def columns_are_lists(self, board_id: str) -> bool:
        """Is every column on this board a to-do list of its own?"""
        return board_id in self.config_entry.options.get(CONF_COLUMN_LISTS, [])

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
        for board in boards:
            await self._async_roll_over(board)
        return {board.id: board for board in boards}

    async def _async_roll_over(self, board: TrelloBoard) -> None:
        """Advance any routine whose day has passed. Idempotent, so safe on every poll."""
        now = dt_util.now()
        for update in plan_rollover(
            board,
            now,
            self.routine_settings,
            self.done_list_id(board),
            self.columns_are_lists(board.id),
        ):
            card = board.card_by_id(update.card_id)
            if card is None:
                continue
            if update.archive:
                try:
                    await self.client.archive_card(card.id)
                except TrelloError as err:
                    _LOGGER.warning("Could not archive '%s': %s", card.name, err)
                    continue
                board.cards.remove(card)
                _LOGGER.debug("Archived finished card '%s'", card.name)
                continue
            fields: dict[str, Any] = {"due": update.due}
            if update.start is not None:
                fields["start"] = update.start
            if update.clear_done:
                fields["dueComplete"] = False
            try:
                await self.client.update_card(card.id, **fields)
            except TrelloError as err:
                _LOGGER.warning("Could not roll over routine '%s': %s", card.name, err)
                continue
            # Reflect it locally rather than fetching the board again.
            card.due = update.due
            if update.start is not None:
                card.start = update.start
            if update.clear_done:
                card.due_complete = False
            _LOGGER.debug("Routine '%s' now due %s", card.name, update.due.isoformat())


def _parse_time(value: Any, default: time) -> time:
    if not value:
        return default
    try:
        parsed = dt_util.parse_time(str(value))
    except (TypeError, ValueError):
        parsed = None
    return parsed or default
