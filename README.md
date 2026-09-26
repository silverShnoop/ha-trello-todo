# Trello To-do

Trello boards as native Home Assistant to-do lists. Tick a task on the
dashboard and its card moves to your Done column in Trello; move a card in
Trello and the dashboard follows within a second.

Each board you pick becomes:

| Entity | What it is |
| --- | --- |
| `todo.<board>` | Every open card on the board. State is the number not in Done. Works with the built-in to-do card, voice, Assist and the `todo.*` services. |
| `sensor.<board>_open_tasks` | Cards not in Done. |
| `sensor.<board>_overdue_tasks` | Open cards whose due date has passed. For "needs attention" badges. |
| `sensor.<board>_due_today` | Open cards due today. |

## How it maps

- **Ticked = in the Done column.** You choose which column counts as Done
  when you set up (it guesses a column called "Done"). Ticking moves the
  card there; unticking moves it back to your new-tasks column.
- **New tasks** go to the column you choose for them (usually "To do").
- **Removing** a task **archives** the card rather than deleting it, so it
  can be restored from Trello.
- **Due dates** go both ways. A date without a time is stored at midday.
- **Descriptions** go both ways.
- **Reordering** in the HA to-do view reorders cards in Trello.

The to-do entity also carries a `board` attribute with the columns, members
and each card's column and assignees, for dashboard cards that want to show
more than done/not done. [Spectra Cards](https://github.com/silverShnoop/ha-spectra-cards)
reads it:

```yaml
type: custom:spectra-card
icon: mdi:trello
title: Chores
meta: {entity: todo.chores, suffix: " to do"}
body:
  type: todo
  list: todo.chores
  items: {todo: todo.chores, status: needs_action}
  board: {entity: todo.chores, attribute: board}
  detail: below
```

## Services

All target a `todo.*` entity from this integration. Columns and people can
be given by name.

```yaml
action: trello_todo.move_card
target: {entity_id: todo.chores}
data: {card_id: 64f0…, list: Doing}

action: trello_todo.set_members          # replaces the assignees; [] to clear
target: {entity_id: todo.chores}
data: {card_id: 64f0…, members: [James]}

action: trello_todo.add_card
target: {entity_id: todo.chores}
data: {name: Paint the fence, list: Doing, members: [James, Sam], due: "2026-10-04"}
```

`card_id` is the to-do item's `uid`.

## Updates: webhook or polling

With **Nabu Casa** or an **external URL** set in HA, the integration
registers a Trello webhook per board, so changes made in Trello show up
straight away. It still polls every 15 minutes in case one is missed.
Without either, it polls every minute. The `sync_mode` attribute on the
to-do entity says which is in use.

The webhook's payload is not trusted: it only triggers a re-read of the
board from the API, so a forged call can at most cause a refresh.

## Install

1. HACS → ⋮ → Custom repositories → this repository, category
   **Integration** → Download → restart HA.
2. Settings → Devices & services → Add integration → **Trello To-do**.
3. **API key.** Trello issues keys to Power-Ups. Open the
   [Power-Up admin portal](https://trello.com/power-ups/admin), choose
   **New**, fill in the form (any workspace; leave the Iframe connector
   URL blank), then open **API key** → **Generate a new API key**.
4. **Token.** The next step shows a link that asks Trello to authorise
   Home Assistant for read and write. Approve it and paste the token back.
5. Pick your boards, then for each one which column is Done and where new
   tasks go.

Change boards or columns later from the integration's **Configure** button.
If the token is revoked, HA asks for a new one.

## Removing

Deleting the integration also removes the Trello webhooks it created.

## Development

```
pip install pytest-homeassistant-custom-component
pytest
```

The tests run against a fake Trello API.
