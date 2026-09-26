"""Count sensors per board, for 'needs attention' badges on dashboards."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import TrelloBoard
from .coordinator import TrelloCoordinator


@dataclass(frozen=True, kw_only=True)
class TrelloSensorDescription(SensorEntityDescription):
    """Describes a board count sensor."""

    value_fn: Callable[[TrelloBoard, str | None], int]


def _open(board: TrelloBoard, done: str | None) -> int:
    return sum(1 for c in board.cards if c.list_id != done)


def _overdue(board: TrelloBoard, done: str | None) -> int:
    now = dt_util.utcnow()
    return sum(1 for c in board.cards if c.list_id != done and c.due and c.due < now)


def _due_today(board: TrelloBoard, done: str | None) -> int:
    today = dt_util.now().date()
    tz = dt_util.get_default_time_zone()
    return sum(
        1
        for c in board.cards
        if c.list_id != done and c.due and c.due.astimezone(tz).date() == today
    )


SENSORS: tuple[TrelloSensorDescription, ...] = (
    TrelloSensorDescription(
        key="open",
        translation_key="open",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_open,
    ),
    TrelloSensorDescription(
        key="overdue",
        translation_key="overdue",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_overdue,
    ),
    TrelloSensorDescription(
        key="due_today",
        translation_key="due_today",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_due_today,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add sensors for each board."""
    coordinator: TrelloCoordinator = entry.runtime_data.coordinator
    async_add_entities(
        TrelloCountSensor(coordinator, board_id, desc)
        for board_id in coordinator.data
        for desc in SENSORS
    )


class TrelloCountSensor(CoordinatorEntity[TrelloCoordinator], SensorEntity):
    """A count of cards on a board."""

    _attr_has_entity_name = True
    entity_description: TrelloSensorDescription

    def __init__(
        self, coordinator: TrelloCoordinator, board_id: str, description: TrelloSensorDescription
    ) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self.entity_description = description
        self._board_id = board_id
        self._attr_unique_id = f"{board_id}_{description.key}"
        self._attr_device_info = coordinator.device_info(board_id)

    @property
    def available(self) -> bool:
        """Unavailable if the board has gone."""
        return super().available and self._board_id in self.coordinator.data

    @property
    def native_value(self) -> int | None:
        """Current count."""
        board = self.coordinator.data.get(self._board_id)
        if board is None:
            return None
        return self.entity_description.value_fn(board, self.coordinator.done_list_id(board))
