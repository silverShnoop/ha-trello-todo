"""Minimal async Trello REST client."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://api.trello.com/1"
TIMEOUT = aiohttp.ClientTimeout(total=20)

CARD_FIELDS = "name,desc,due,dueComplete,idList,idMembers,idLabels,pos,shortUrl"
LIST_FIELDS = "name,pos"
MEMBER_FIELDS = "fullName,initials,username,avatarUrl"
LABEL_FIELDS = "name,color"


class TrelloError(Exception):
    """Generic Trello API error."""


class TrelloAuthError(TrelloError):
    """Key or token rejected."""


class TrelloConnectionError(TrelloError):
    """Trello could not be reached."""


@dataclass(slots=True)
class TrelloList:
    """A column on a board."""

    id: str
    name: str
    pos: float


@dataclass(slots=True)
class TrelloMember:
    """A board member."""

    id: str
    full_name: str
    initials: str
    username: str
    avatar_url: str | None


@dataclass(slots=True)
class TrelloLabel:
    """A board label."""

    id: str
    name: str
    color: str | None


@dataclass(slots=True)
class TrelloCard:
    """A card (task)."""

    id: str
    name: str
    desc: str
    due: datetime | None
    due_complete: bool
    list_id: str
    member_ids: list[str]
    label_ids: list[str]
    pos: float
    url: str


@dataclass(slots=True)
class TrelloBoard:
    """A board with everything needed to render it."""

    id: str
    name: str
    url: str
    lists: list[TrelloList] = field(default_factory=list)
    cards: list[TrelloCard] = field(default_factory=list)
    members: list[TrelloMember] = field(default_factory=list)
    labels: list[TrelloLabel] = field(default_factory=list)

    def list_by_id(self, list_id: str) -> TrelloList | None:
        """Return a list by id."""
        return next((lst for lst in self.lists if lst.id == list_id), None)

    def card_by_id(self, card_id: str) -> TrelloCard | None:
        """Return a card by id."""
        return next((c for c in self.cards if c.id == card_id), None)


def _parse_due(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_board(raw: dict[str, Any]) -> TrelloBoard:
    """Build a TrelloBoard from the nested /boards/{id} response."""
    return TrelloBoard(
        id=raw["id"],
        name=raw.get("name", ""),
        url=raw.get("url", ""),
        lists=sorted(
            (
                TrelloList(id=lst["id"], name=lst["name"], pos=float(lst["pos"]))
                for lst in raw.get("lists", [])
            ),
            key=lambda lst: lst.pos,
        ),
        cards=[
            TrelloCard(
                id=c["id"],
                name=c.get("name", ""),
                desc=c.get("desc", "") or "",
                due=_parse_due(c.get("due")),
                due_complete=bool(c.get("dueComplete")),
                list_id=c["idList"],
                member_ids=list(c.get("idMembers") or []),
                label_ids=list(c.get("idLabels") or []),
                pos=float(c.get("pos", 0)),
                url=c.get("shortUrl", ""),
            )
            for c in raw.get("cards", [])
        ],
        members=[
            TrelloMember(
                id=m["id"],
                full_name=m.get("fullName") or m.get("username", ""),
                initials=m.get("initials") or "",
                username=m.get("username", ""),
                avatar_url=(
                    f"{m['avatarUrl']}/50.png" if m.get("avatarUrl") else None
                ),
            )
            for m in raw.get("members", [])
        ],
        labels=[
            TrelloLabel(id=lb["id"], name=lb.get("name", ""), color=lb.get("color"))
            for lb in raw.get("labels", [])
        ],
    )


class TrelloClient:
    """Thin wrapper over the parts of the Trello REST API we use."""

    def __init__(self, session: aiohttp.ClientSession, api_key: str, token: str) -> None:
        """Initialise the client."""
        self._session = session
        self._key = api_key
        self._token = token

    @property
    def token(self) -> str:
        """Return the user token."""
        return self._token

    async def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        query: dict[str, Any] = {"key": self._key, "token": self._token}
        for k, v in (params or {}).items():
            if v is None:
                continue
            if isinstance(v, bool):
                v = "true" if v else "false"
            elif isinstance(v, (list, tuple)):
                v = ",".join(v)
            query[k] = v
        try:
            async with self._session.request(
                method, f"{API_BASE}{path}", params=query, timeout=TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    raise TrelloAuthError(await resp.text())
                if resp.status >= 400:
                    raise TrelloError(f"{method} {path}: {resp.status} {await resp.text()}")
                body = await resp.text()
                try:
                    return json.loads(body) if body else None
                except ValueError:
                    return body
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise TrelloConnectionError(str(err)) from err

    async def get_me(self) -> dict[str, Any]:
        """Return the member who owns the token."""
        return await self._request("GET", "/members/me", {"fields": "fullName,username"})

    async def get_boards(self) -> list[dict[str, Any]]:
        """Return the open boards the token can see."""
        return await self._request(
            "GET", "/members/me/boards", {"filter": "open", "fields": "name,url"}
        )

    async def get_board_lists(self, board_id: str) -> list[TrelloList]:
        """Return a board's open lists in order."""
        raw = await self._request(
            "GET", f"/boards/{board_id}/lists", {"filter": "open", "fields": LIST_FIELDS}
        )
        return sorted(
            (TrelloList(id=r["id"], name=r["name"], pos=float(r["pos"])) for r in raw),
            key=lambda lst: lst.pos,
        )

    async def get_board(self, board_id: str) -> TrelloBoard:
        """Return a board with lists, open cards, members and labels in one call."""
        raw = await self._request(
            "GET",
            f"/boards/{board_id}",
            {
                "fields": "name,url",
                "lists": "open",
                "list_fields": LIST_FIELDS,
                "cards": "open",
                "card_fields": CARD_FIELDS,
                "members": "all",
                "member_fields": MEMBER_FIELDS,
                "labels": "all",
                "label_fields": LABEL_FIELDS,
            },
        )
        return parse_board(raw)

    async def create_card(
        self,
        list_id: str,
        name: str,
        *,
        desc: str | None = None,
        due: datetime | None = None,
        member_ids: list[str] | None = None,
        pos: str | float = "bottom",
    ) -> dict[str, Any]:
        """Create a card."""
        return await self._request(
            "POST",
            "/cards",
            {
                "idList": list_id,
                "name": name,
                "desc": desc,
                "due": due.isoformat() if due else None,
                "idMembers": member_ids or None,
                "pos": pos,
            },
        )

    async def update_card(self, card_id: str, **fields: Any) -> dict[str, Any]:
        """Update a card. Pass Trello field names (idList, name, desc, due, pos…).

        Pass due="null" to remove the due date.
        """
        params = {}
        for k, v in fields.items():
            if isinstance(v, datetime):
                v = v.isoformat()
            params[k] = v
        return await self._request("PUT", f"/cards/{card_id}", params)

    async def add_member(self, card_id: str, member_id: str) -> None:
        """Assign a member to a card."""
        await self._request("POST", f"/cards/{card_id}/idMembers", {"value": member_id})

    async def remove_member(self, card_id: str, member_id: str) -> None:
        """Unassign a member from a card."""
        await self._request("DELETE", f"/cards/{card_id}/idMembers/{member_id}")

    async def archive_card(self, card_id: str) -> None:
        """Archive (close) a card. Safer than deleting; it can be restored in Trello."""
        await self._request("PUT", f"/cards/{card_id}", {"closed": True})

    async def get_webhooks(self) -> list[dict[str, Any]]:
        """List webhooks registered by this token."""
        return await self._request("GET", f"/tokens/{self._token}/webhooks")

    async def create_webhook(self, model_id: str, callback_url: str, description: str) -> dict[str, Any]:
        """Register a webhook. Trello sends a HEAD to the callback first."""
        return await self._request(
            "POST",
            "/webhooks",
            {"idModel": model_id, "callbackURL": callback_url, "description": description},
        )

    async def delete_webhook(self, webhook_id: str) -> None:
        """Remove a webhook."""
        await self._request("DELETE", f"/webhooks/{webhook_id}")
