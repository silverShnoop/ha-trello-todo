"""Constants for the Trello To-do integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "trello_todo"
VERSION: Final = "0.1.0"

CONF_API_KEY: Final = "api_key"
CONF_TOKEN: Final = "token"
CONF_BOARDS: Final = "boards"
CONF_DONE_LISTS: Final = "done_lists"
CONF_DEFAULT_LISTS: Final = "default_lists"
CONF_USE_WEBHOOK: Final = "use_webhook"
CONF_WEBHOOK_ID: Final = "webhook_id"

# Refresh cadence. With a working Trello webhook we still poll occasionally as a
# safety net in case a delivery is missed.
POLL_INTERVAL: Final = timedelta(seconds=60)
POLL_INTERVAL_WITH_WEBHOOK: Final = timedelta(minutes=15)

WEBHOOK_DESCRIPTION: Final = "Home Assistant trello_todo"

AUTHORIZE_URL: Final = (
    "https://trello.com/1/authorize?expiration=never&scope=read,write"
    "&response_type=token&name=Home%20Assistant&key={key}"
)
POWERUP_ADMIN_URL: Final = "https://trello.com/power-ups/admin"

SERVICE_MOVE_CARD: Final = "move_card"
SERVICE_SET_MEMBERS: Final = "set_members"
SERVICE_ADD_CARD: Final = "add_card"

ATTR_CARD_ID: Final = "card_id"
ATTR_LIST: Final = "list"
ATTR_MEMBERS: Final = "members"
ATTR_NAME: Final = "name"
ATTR_DUE: Final = "due"
ATTR_DESCRIPTION: Final = "description"
