"""Config and options flow: key → token → boards → which column is Done."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)

from .autotick import describe, links_from
from .api import TrelloAuthError, TrelloClient, TrelloConnectionError, TrelloError, TrelloList
from .const import (
    AUTHORIZE_URL,
    CONF_API_KEY,
    CONF_AUTOTICK,
    CONF_BOARDS,
    CONF_CARRY_OVER,
    CONF_DEFAULT_LISTS,
    CONF_DONE_LISTS,
    CONF_ROLLOVER,
    CONF_ROUTINE_DEFAULT_TIME,
    CONF_TOKEN,
    CONF_USE_WEBHOOK,
    DOMAIN,
    POWERUP_ADMIN_URL,
)

SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _guess_done(lists: list[TrelloList]) -> str | None:
    for lst in lists:
        if lst.name.strip().lower() in ("done", "completed", "finished"):
            return lst.id
    return lists[-1].id if lists else None


def _guess_default(lists: list[TrelloList], done: str | None) -> str | None:
    return next((lst.id for lst in lists if lst.id != done), None)


class _BoardSteps:
    """Board selection and per-board list choices, shared by config and options flows."""

    client: TrelloClient
    _boards: dict[str, str]
    _queue: list[str]
    _choices: dict[str, Any]
    _defaults: dict[str, Any]

    async def _async_load_boards(self) -> None:
        raw = await self.client.get_boards()
        self._boards = {b["id"]: b["name"] for b in sorted(raw, key=lambda b: b["name"].lower())}

    def _boards_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(
                    CONF_BOARDS,
                    default=[b for b in self._defaults.get(CONF_BOARDS, []) if b in self._boards],
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[SelectOptionDict(value=k, label=v) for k, v in self._boards.items()],
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_USE_WEBHOOK, default=self._defaults.get(CONF_USE_WEBHOOK, True)
                ): BooleanSelector(),
            }
        )

    async def async_step_boards(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick boards."""
        errors: dict[str, str] = {}
        if user_input is not None:
            boards = [b for b in user_input[CONF_BOARDS] if b in self._boards]
            if not boards:
                errors["base"] = "no_boards"
            else:
                self._choices = {
                    CONF_BOARDS: boards,
                    CONF_USE_WEBHOOK: user_input[CONF_USE_WEBHOOK],
                    CONF_DONE_LISTS: {},
                    CONF_DEFAULT_LISTS: {},
                }
                self._queue = list(boards)
                return await self.async_step_lists()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="boards", data_schema=self._boards_schema(), errors=errors
        )

    async def async_step_lists(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """For each board: which column is Done, and where new tasks go."""
        if user_input is not None:
            board_id = self._queue.pop(0)
            self._choices[CONF_DONE_LISTS][board_id] = user_input[CONF_DONE_LISTS]
            self._choices[CONF_DEFAULT_LISTS][board_id] = user_input[CONF_DEFAULT_LISTS]
        if not self._queue:
            return await self.async_step_routines()

        board_id = self._queue[0]
        try:
            lists = await self.client.get_board_lists(board_id)
        except TrelloError:
            return self.async_abort(reason="cannot_connect")  # type: ignore[attr-defined]
        if not lists:
            self._queue.pop(0)
            self._choices[CONF_BOARDS].remove(board_id)
            return await self.async_step_lists()

        ids = {lst.id for lst in lists}
        done = self._defaults.get(CONF_DONE_LISTS, {}).get(board_id)
        done = done if done in ids else _guess_done(lists)
        default = self._defaults.get(CONF_DEFAULT_LISTS, {}).get(board_id)
        default = default if default in ids else _guess_default(lists, done)
        options = [SelectOptionDict(value=lst.id, label=lst.name) for lst in lists]
        dropdown = SelectSelector(
            SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="lists",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DONE_LISTS, default=done): dropdown,
                    vol.Required(CONF_DEFAULT_LISTS, default=default): dropdown,
                }
            ),
            description_placeholders={"board": self._boards.get(board_id, board_id)},
        )

    async def async_step_routines(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """How recurring cards (day labels) roll over."""
        if user_input is not None:
            self._choices[CONF_ROLLOVER] = user_input[CONF_ROLLOVER]
            self._choices[CONF_ROUTINE_DEFAULT_TIME] = user_input[CONF_ROUTINE_DEFAULT_TIME]
            self._choices[CONF_CARRY_OVER] = user_input[CONF_CARRY_OVER]
            return await self._async_finish(self._choices)
        d = self._defaults
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="routines",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ROLLOVER, default=d.get(CONF_ROLLOVER, "04:00:00")
                    ): TimeSelector(),
                    vol.Required(
                        CONF_ROUTINE_DEFAULT_TIME,
                        default=d.get(CONF_ROUTINE_DEFAULT_TIME, "18:00:00"),
                    ): TimeSelector(),
                    vol.Required(
                        CONF_CARRY_OVER, default=d.get(CONF_CARRY_OVER, True)
                    ): BooleanSelector(),
                }
            ),
        )

    async def _async_finish(self, options: dict[str, Any]) -> ConfigFlowResult:
        raise NotImplementedError


class TrelloTodoConfigFlow(_BoardSteps, ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise."""
        self._api_key = ""
        self._token = ""
        self._title = "Trello"
        self._boards = {}
        self._queue = []
        self._choices = {}
        self._defaults = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options flow."""
        return TrelloTodoOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: the Power-Up API key."""
        if user_input is not None:
            self._api_key = user_input[CONF_API_KEY].strip()
            return await self.async_step_token()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}),
            description_placeholders={"admin_url": POWERUP_ADMIN_URL},
        )

    async def async_step_token(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2: authorise and paste the token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._token = user_input[CONF_TOKEN].strip()
            self.client = TrelloClient(
                async_get_clientsession(self.hass), self._api_key, self._token
            )
            try:
                me = await self.client.get_me()
                await self._async_load_boards()
            except TrelloAuthError:
                errors["base"] = "invalid_auth"
            except TrelloConnectionError:
                errors["base"] = "cannot_connect"
            except TrelloError:
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(me["id"])
                if self.source == "reauth":
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(),
                        data_updates={CONF_API_KEY: self._api_key, CONF_TOKEN: self._token},
                    )
                self._abort_if_unique_id_configured()
                self._title = f"Trello ({me.get('fullName') or me.get('username')})"
                return await self.async_step_boards()
        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): SECRET}),
            errors=errors,
            description_placeholders={"auth_url": AUTHORIZE_URL.format(key=self._api_key)},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Token revoked or expired: ask for a fresh one."""
        self._api_key = entry_data[CONF_API_KEY]
        return await self.async_step_token()

    async def _async_finish(self, options: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self._title,
            data={CONF_API_KEY: self._api_key, CONF_TOKEN: self._token},
            options=options,
        )


class TrelloTodoOptionsFlow(_BoardSteps, OptionsFlow):
    """Change boards / Done column later, and which chores tick themselves."""

    def __init__(self) -> None:
        """Initialise."""
        self._boards = {}
        self._queue = []
        self._choices = {}
        self._defaults = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Boards and routines, or the chores that tick themselves."""
        return self.async_show_menu(step_id="init", menu_options=["settings", "autotick"])

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Start at board selection with current values filled in."""
        self._defaults = dict(self.config_entry.options)
        self.client = TrelloClient(
            async_get_clientsession(self.hass),
            self.config_entry.data[CONF_API_KEY],
            self.config_entry.data[CONF_TOKEN],
        )
        try:
            await self._async_load_boards()
        except TrelloError:
            return self.async_abort(reason="cannot_connect")
        return await self.async_step_boards()

    async def async_step_autotick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Link a chore to the state that proves it done; keep or drop the links there are."""
        runtime = getattr(self.config_entry, "runtime_data", None)
        coordinator = getattr(runtime, "coordinator", None)
        if coordinator is None or not coordinator.data:
            return self.async_abort(reason="not_loaded")
        cards: dict[str, str] = {}
        for board in coordinator.data.values():
            for card in sorted(board.cards, key=lambda c: c.name.lower()):
                cards[f"{board.id}|{card.id}"] = (
                    card.name if len(coordinator.data) == 1 else f"{board.name}: {card.name}"
                )
        links = links_from(self.config_entry.options)

        def label(link: dict[str, Any]) -> str:
            state = self.hass.states.get(link["entity"])
            entity = state.name if state else link["entity"]
            card = cards.get(f"{link['board']}|{link['card']}", "a card that is gone")
            return describe(link, card, entity)

        keys = [f"{link['board']}|{link['card']}|{link['entity']}" for link in links]
        errors: dict[str, str] = {}
        if user_input is not None:
            kept = [link for key, link in zip(keys, links) if key in user_input.get("keep", [])]
            chosen = user_input.get("card")
            entity = user_input.get("entity")
            states = [x.strip() for x in str(user_input.get("states") or "").split(",") if x.strip()]
            if chosen or entity:
                if not (chosen and entity and states):
                    errors["base"] = "incomplete_link"
                elif chosen not in cards:
                    errors["card"] = "unknown_card"
                else:
                    board_id, card_id = chosen.split("|", 1)
                    kept = [k for k in kept if not (k["card"] == card_id and k["entity"] == entity)]
                    kept.append({"board": board_id, "card": card_id, "entity": entity, "states": states})
            if not errors:
                return self.async_create_entry(
                    data={**self.config_entry.options, CONF_AUTOTICK: kept}
                )

        schema: dict[Any, Any] = {}
        if links:
            schema[vol.Optional("keep", default=keys)] = SelectSelector(
                SelectSelectorConfig(
                    options=[SelectOptionDict(value=k, label=label(link)) for k, link in zip(keys, links)],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        schema[vol.Optional("card")] = SelectSelector(
            SelectSelectorConfig(
                options=[SelectOptionDict(value=k, label=v) for k, v in cards.items()],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        schema[vol.Optional("entity")] = EntitySelector(EntitySelectorConfig())
        schema[vol.Optional("states", default="on")] = TextSelector()
        return self.async_show_form(step_id="autotick", data_schema=vol.Schema(schema), errors=errors)

    async def _async_finish(self, options: dict[str, Any]) -> ConfigFlowResult:
        # Keep what this path does not ask about, such as the auto-tick links.
        return self.async_create_entry(data={**self.config_entry.options, **options})
