"""A native to-do entity per Trello board."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

import voluptuous as vol

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import TrelloBoard, TrelloCard, TrelloError, TrelloList, TrelloMember
from .const import (
    ATTR_CARD_ID,
    ATTR_DESCRIPTION,
    ATTR_DUE,
    ATTR_LIST,
    ATTR_MEMBERS,
    ATTR_NAME,
    SERVICE_ADD_CARD,
    SERVICE_MOVE_CARD,
    SERVICE_SET_MEMBERS,
)
from .coordinator import TrelloCoordinator

POS_STEP = 65536.0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add one to-do entity per selected board."""
    coordinator: TrelloCoordinator = entry.runtime_data.coordinator
    async_add_entities(
        TrelloBoardTodo(coordinator, board_id) for board_id in coordinator.data
    )

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_MOVE_CARD,
        {
            vol.Required(ATTR_CARD_ID): cv.string,
            vol.Required(ATTR_LIST): cv.string,
        },
        "async_move_card",
    )
    platform.async_register_entity_service(
        SERVICE_SET_MEMBERS,
        {
            vol.Required(ATTR_CARD_ID): cv.string,
            vol.Required(ATTR_MEMBERS): vol.All(cv.ensure_list, [cv.string]),
        },
        "async_set_members",
    )
    platform.async_register_entity_service(
        SERVICE_ADD_CARD,
        {
            vol.Required(ATTR_NAME): cv.string,
            vol.Optional(ATTR_LIST): cv.string,
            vol.Optional(ATTR_MEMBERS): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional(ATTR_DUE): vol.Any(cv.datetime, cv.date),
            vol.Optional(ATTR_DESCRIPTION): cv.string,
        },
        "async_add_card",
    )


def _due_from_ha(due: date | datetime | None) -> datetime | None:
    """HA may hand us a bare date; Trello needs a timestamp. Use local midday."""
    if due is None:
        return None
    if isinstance(due, datetime):
        return due if due.tzinfo else due.replace(tzinfo=dt_util.get_default_time_zone())
    return datetime.combine(due, time(12, 0), tzinfo=dt_util.get_default_time_zone())


class TrelloBoardTodo(CoordinatorEntity[TrelloCoordinator], TodoListEntity):
    """All open cards on a board. Ticked = card sits in the board's Done list."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_translation_key = "board"
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.MOVE_TODO_ITEM
        | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
        | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )
    # The card data is large and changes often; keep it out of the recorder.
    _unrecorded_attributes = frozenset({"board", "board_url"})

    def __init__(self, coordinator: TrelloCoordinator, board_id: str) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self._board_id = board_id
        self._attr_unique_id = board_id
        self._attr_device_info = coordinator.device_info(board_id)
        self._update_items()

    # ---- helpers ---------------------------------------------------------

    @property
    def board(self) -> TrelloBoard:
        """Current board snapshot."""
        return self.coordinator.data[self._board_id]

    @property
    def available(self) -> bool:
        """Unavailable if the board has gone (deleted / access removed)."""
        return super().available and self._board_id in self.coordinator.data

    def _card(self, card_id: str) -> TrelloCard:
        card = self.board.card_by_id(card_id)
        if card is None:
            raise ServiceValidationError(f"No card {card_id} on board {self.board.name}")
        return card

    def _resolve_list(self, value: str) -> TrelloList:
        """Accept a list id or a (case-insensitive) list name."""
        board = self.board
        by_id = board.list_by_id(value)
        if by_id:
            return by_id
        wanted = value.strip().lower()
        for lst in board.lists:
            if lst.name.strip().lower() == wanted:
                return lst
        raise ServiceValidationError(
            f"No list '{value}' on {board.name}. Lists: {', '.join(x.name for x in board.lists)}"
        )

    def _resolve_member(self, value: str) -> TrelloMember:
        """Accept a member id, username, full name or first name."""
        wanted = value.strip().lower().lstrip("@")
        for m in self.board.members:
            if wanted in (
                m.id.lower(),
                m.username.lower(),
                m.full_name.lower(),
                m.full_name.split(" ")[0].lower(),
            ):
                return m
        raise ServiceValidationError(f"No member '{value}' on {self.board.name}")

    def _sorted_cards(self) -> list[TrelloCard]:
        board = self.board
        order = {lst.id: i for i, lst in enumerate(board.lists)}
        done = self.coordinator.done_list_id(board)
        return sorted(
            (c for c in board.cards if c.list_id in order),
            key=lambda c: (c.list_id == done, order[c.list_id], c.pos),
        )

    def _new_pos_after(self, list_id: str, previous: TrelloCard | None, exclude: str) -> float | str:
        """A Trello pos that lands just after `previous` (or at the top)."""
        siblings = sorted(
            (c for c in self.board.cards if c.list_id == list_id and c.id != exclude),
            key=lambda c: c.pos,
        )
        if previous is None:
            return "top"
        after = [c for c in siblings if c.pos > previous.pos]
        if not after:
            return previous.pos + POS_STEP
        return (previous.pos + after[0].pos) / 2

    async def _call(self, coro: Any) -> Any:
        try:
            result = await coro
        except TrelloError as err:
            raise HomeAssistantError(f"Trello request failed: {err}") from err
        await self.coordinator.async_request_refresh()
        return result

    # ---- state -----------------------------------------------------------

    @callback
    def _update_items(self) -> None:
        if self._board_id not in self.coordinator.data:
            self._attr_todo_items = []
            return
        board = self.board
        done = self.coordinator.done_list_id(board)
        tz = dt_util.get_default_time_zone()
        self._attr_todo_items = [
            TodoItem(
                uid=c.id,
                summary=c.name,
                status=TodoItemStatus.COMPLETED if c.list_id == done else TodoItemStatus.NEEDS_ACTION,
                due=c.due.astimezone(tz) if c.due else None,
                description=c.desc or None,
            )
            for c in self._sorted_cards()
        ]

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_items()
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Everything the dashboard card needs to draw columns, people and labels."""
        if self._board_id not in self.coordinator.data:
            return {}
        board = self.board
        done = self.coordinator.done_list_id(board)
        return {
            "trello_board_id": board.id,
            "board_url": board.url,
            "sync_mode": self.coordinator.sync_mode,
            # One object so a dashboard card can take it in a single read:
            # {entity: todo.chores, attribute: board}
            "board": self._board_attribute(board, done),
        }

    def _board_attribute(self, board: TrelloBoard, done: str | None) -> dict[str, Any]:
        return {
            "done": done,
            "default": self.coordinator.default_list_id(board),
            "lists": [{"id": lst.id, "name": lst.name} for lst in board.lists],
            "members": [
                {
                    "id": m.id,
                    "name": m.full_name,
                    "initials": m.initials,
                    "username": m.username,
                    "avatar": m.avatar_url,
                }
                for m in board.members
            ],
            "labels": [
                {"id": lb.id, "name": lb.name, "color": lb.color} for lb in board.labels
            ],
            "cards": [
                {
                    "id": c.id,
                    "name": c.name,
                    "list": c.list_id,
                    "members": c.member_ids,
                    "labels": c.label_ids,
                    "due": c.due.isoformat() if c.due else None,
                    "desc": c.desc,
                    "url": c.url,
                }
                for c in self._sorted_cards()
            ],
        }

    # ---- standard to-do operations --------------------------------------

    async def async_create_todo_item(self, item: TodoItem) -> None:
        """Add a card to the default list."""
        list_id = self.coordinator.default_list_id(self.board)
        if list_id is None:
            raise HomeAssistantError(f"{self.board.name} has no lists to add to")
        if item.status == TodoItemStatus.COMPLETED:
            list_id = self.coordinator.done_list_id(self.board) or list_id
        await self._call(
            self.coordinator.client.create_card(
                list_id,
                item.summary or "",
                desc=item.description,
                due=_due_from_ha(item.due),
            )
        )

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Rename / re-describe / re-date a card, and tick → move to Done."""
        board = self.board
        card = self._card(item.uid)  # type: ignore[arg-type]
        done = self.coordinator.done_list_id(board)
        fields: dict[str, Any] = {}

        if item.summary is not None and item.summary != card.name:
            fields["name"] = item.summary
        desc = item.description or ""
        if desc != card.desc:
            fields["desc"] = desc
        new_due = _due_from_ha(item.due)
        if new_due != card.due:
            fields["due"] = new_due if new_due else "null"

        if item.status == TodoItemStatus.COMPLETED and card.list_id != done and done:
            fields["idList"] = done
            fields["pos"] = "top"
        elif item.status == TodoItemStatus.NEEDS_ACTION and card.list_id == done:
            back = self.coordinator.default_list_id(board)
            if back:
                fields["idList"] = back
                fields["pos"] = "bottom"

        if fields:
            await self._call(self.coordinator.client.update_card(card.id, **fields))

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """Archive cards (they can still be restored from Trello)."""
        for uid in uids:
            await self.coordinator.client.archive_card(uid)
        await self.coordinator.async_request_refresh()

    async def async_move_todo_item(self, uid: str, previous_uid: str | None = None) -> None:
        """Drag-to-reorder from the HA to-do UI."""
        card = self._card(uid)
        previous = self._card(previous_uid) if previous_uid else None
        list_id = previous.list_id if previous else card.list_id
        fields: dict[str, Any] = {"pos": self._new_pos_after(list_id, previous, uid)}
        if list_id != card.list_id:
            fields["idList"] = list_id
        await self._call(self.coordinator.client.update_card(uid, **fields))

    # ---- extra services used by the dashboard card ----------------------

    async def async_move_card(self, card_id: str, list: str) -> None:  # noqa: A002
        """Move a card to another column (by list name or id)."""
        card = self._card(card_id)
        target = self._resolve_list(list)
        if target.id == card.list_id:
            return
        await self._call(
            self.coordinator.client.update_card(card.id, idList=target.id, pos="bottom")
        )

    async def async_set_members(self, card_id: str, members: list[str]) -> None:
        """Replace a card's assignees (empty list = unassigned)."""
        card = self._card(card_id)
        wanted = {self._resolve_member(m).id for m in members}
        current = set(card.member_ids)
        client = self.coordinator.client
        try:
            for member_id in wanted - current:
                await client.add_member(card.id, member_id)
            for member_id in current - wanted:
                await client.remove_member(card.id, member_id)
        except TrelloError as err:
            raise HomeAssistantError(f"Trello request failed: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_add_card(
        self,
        name: str,
        list: str | None = None,  # noqa: A002
        members: list[str] | None = None,
        due: date | datetime | None = None,
        description: str | None = None,
    ) -> None:
        """Add a card to a specific column with assignees."""
        list_id = (
            self._resolve_list(list).id if list else self.coordinator.default_list_id(self.board)
        )
        if list_id is None:
            raise HomeAssistantError(f"{self.board.name} has no lists to add to")
        member_ids = [self._resolve_member(m).id for m in members or []]
        await self._call(
            self.coordinator.client.create_card(
                list_id, name, desc=description, due=_due_from_ha(due), member_ids=member_ids
            )
        )
