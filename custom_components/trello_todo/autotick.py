"""Chores that tick themselves: a card linked to the thing that proves it done.

"Run the dishwasher" is done when the dishwasher starts. A link names a card,
an entity and the state that means done (the dishwasher's `running`); when the
entity goes into that state the card is ticked in Trello, exactly as a person
ticking it would, and the board says what ticked it and when.

Only a card that is asking counts: one due today or carried over, not one
already ticked or not due until a later day. A run before the card's start
time still counts -- the job is done, whenever it was done.

A person has the last word. Unticking by hand clears what the sensor said,
and the same run will not tick it again: only the entity going into the state
afresh does. The record of both lives in a small store, so a restart neither
forgets what ticked a card nor re-ticks one somebody unticked.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import TrelloCard, TrelloError
from .const import CONF_AUTOTICK, DOMAIN
from .routines import logical_date

if TYPE_CHECKING:
    from .coordinator import TrelloCoordinator

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1


def links_from(options: dict[str, Any]) -> list[dict[str, Any]]:
    """The well-formed links in the options: board, card, entity, states."""
    out = []
    for link in options.get(CONF_AUTOTICK) or []:
        if not isinstance(link, dict):
            continue
        states = link.get("states") or []
        if isinstance(states, str):
            states = [states]
        states = [str(s).strip() for s in states if str(s).strip()]
        if link.get("board") and link.get("card") and link.get("entity") and states:
            out.append({**link, "states": states})
    return out


class AutoTick:
    """Watches the linked entities and ticks their cards."""

    def __init__(self, hass: HomeAssistant, coordinator: TrelloCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{coordinator.config_entry.entry_id}.autotick"
        )
        self.ticked_by: dict[str, dict[str, str]] = {}
        """Card id -> {"by": what ticked it, "at": when}."""
        self.unticked: dict[str, str] = {}
        """Card id -> when a person last unticked it, so a run in progress does not re-tick it."""
        self._unsubs: list[Callable[[], None]] = []
        self._key = f"autotick_{coordinator.config_entry.entry_id}"

    @property
    def links(self) -> list[dict[str, Any]]:
        return links_from(self.coordinator.config_entry.options)

    async def async_start(self) -> None:
        # A reload (new options, say) is quicker than the delayed save, so the
        # last instance's record is handed over in memory; the store is for
        # restarts.
        kept = self.hass.data.setdefault(DOMAIN, {}).get(self._key)
        data = kept or await self._store.async_load() or {}
        self.ticked_by = dict(data.get("ticked_by") or {})
        self.unticked = dict(data.get("unticked") or {})
        entities = sorted({link["entity"] for link in self.links})
        if entities:
            self._unsubs.append(
                async_track_state_change_event(self.hass, entities, self._changed)
            )

    @callback
    def async_catch_up(self) -> None:
        """Once the boards are loaded: anything already in its state counts.

        Set up mid-run, or restarted during one, the run is still a run --
        as long as nobody has unticked the card by hand since.
        """
        self._unsubs.append(async_at_started(self.hass, self._catch_up))

    @callback
    def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    # -- what people do -------------------------------------------------------

    @callback
    def note_untick(self, card_id: str) -> None:
        """A person unticked it: forget the sensor, and do not re-tick this run."""
        self.ticked_by.pop(card_id, None)
        self.unticked[card_id] = dt_util.now().isoformat()
        self._save()

    @callback
    def prune(self) -> None:
        """Drop records for cards that are gone or no longer ticked."""
        live: dict[str, bool] = {}
        for board in (self.coordinator.data or {}).values():
            for card in board.cards:
                live[card.id] = self.coordinator.is_done(card, board)
        stale = [cid for cid in self.ticked_by if not live.get(cid)]
        for cid in stale:
            self.ticked_by.pop(cid)
        settings = self.coordinator.routine_settings
        today = logical_date(dt_util.now(), settings)
        old = [
            cid for cid, at in self.unticked.items()
            if cid not in live or _day_of(at, settings) != today
        ]
        for cid in old:
            self.unticked.pop(cid)
        if stale or old:
            self._save()

    # -- what entities do -----------------------------------------------------

    @callback
    def _changed(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        old = event.data["old_state"]
        if new is None:
            return
        for link in self.links:
            if link["entity"] != new.entity_id or new.state not in link["states"]:
                continue
            if old is not None and old.state in link["states"]:
                continue  # still in it, not freshly into it
            self.hass.async_create_task(self._tick(link, new, fresh=True))

    @callback
    def _catch_up(self, _hass: HomeAssistant) -> None:
        for link in self.links:
            state = self.hass.states.get(link["entity"])
            if state is not None and state.state in link["states"]:
                self.hass.async_create_task(self._tick(link, state, fresh=False))

    async def _tick(self, link: dict[str, Any], state: State, *, fresh: bool) -> None:
        board = (self.coordinator.data or {}).get(link["board"])
        card = board.card_by_id(link["card"]) if board else None
        if board is None or card is None or self.coordinator.is_done(card, board):
            return
        if not self._asking(card):
            return
        if not fresh and card.id in self.unticked:
            # Found already running, but a person unticked it during this
            # run. Wait for the next one.
            return
        fields: dict[str, Any] = {}
        if self.coordinator.ticks_in_place(card, board):
            fields["dueComplete"] = True
            if card.due is None:
                fields["due"] = dt_util.now()
        else:
            done = self.coordinator.done_list_id(board)
            if not done:
                return
            fields["idList"] = done
            fields["pos"] = "top"
        try:
            await self.coordinator.client.update_card(card.id, **fields)
        except TrelloError as err:
            _LOGGER.warning("Could not tick '%s' from %s: %s", card.name, state.entity_id, err)
            return
        self.ticked_by[card.id] = {
            "by": state.attributes.get(ATTR_FRIENDLY_NAME) or state.entity_id,
            "at": dt_util.now().isoformat(),
        }
        self.unticked.pop(card.id, None)
        self._save()
        _LOGGER.debug("'%s' ticked by %s", card.name, state.entity_id)
        # Show it at once, from what Trello accepted. Fetching the board now
        # could return a copy from before the tick, and the board's webhook
        # brings the real one moments later anyway.
        if "dueComplete" in fields:
            card.due_complete = True
            if card.due is None:
                card.due = fields["due"]
        else:
            card.list_id = fields["idList"]
        self.coordinator.async_set_updated_data(self.coordinator.data)

    def _asking(self, card: TrelloCard) -> bool:
        """Due today or carried over. A card not due until a later day is not."""
        if card.due is None:
            return True
        settings = self.coordinator.routine_settings
        now = dt_util.now()
        return logical_date(card.due.astimezone(now.tzinfo), settings) <= logical_date(now, settings)

    def _save(self) -> None:
        self.hass.data.setdefault(DOMAIN, {})[self._key] = {
            "ticked_by": dict(self.ticked_by), "unticked": dict(self.unticked),
        }
        self._store.async_delay_save(
            lambda: {"ticked_by": self.ticked_by, "unticked": self.unticked}, 1
        )


def _day_of(iso: str, settings: Any) -> Any:
    moment = dt_util.parse_datetime(iso)
    if moment is None:
        return None
    return logical_date(dt_util.as_local(moment), settings)


def describe(link: dict[str, Any], card_name: str, entity_name: str) -> str:
    """One line for the options page: Run the dishwasher ← Dishwasher is running."""
    return f"{card_name} ← {entity_name} is {' or '.join(link['states'])}"
