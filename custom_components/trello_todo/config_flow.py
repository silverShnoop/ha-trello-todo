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
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import TrelloAuthError, TrelloClient, TrelloConnectionError, TrelloError, TrelloList
from .const import (
    AUTHORIZE_URL,
    CONF_API_KEY,
    CONF_BOARDS,
    CONF_DEFAULT_LISTS,
    CONF_DONE_LISTS,
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
            return await self._async_finish(self._choices)

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
    """Change boards / Done column later."""

    def __init__(self) -> None:
        """Initialise."""
        self._boards = {}
        self._queue = []
        self._choices = {}
        self._defaults = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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

    async def _async_finish(self, options: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(data=options)
