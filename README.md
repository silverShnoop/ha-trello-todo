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

## Routines: cards that repeat

Give a card a **day label** and it becomes a routine. Labels are matched by
name, case-insensitive: `Mon` … `Sun` (or the full names), `Daily`,
`Weekdays`, `Weekend`. Several labels add up (`Mon` + `Thu`). The card's
**due date is its deadline**: only the time of day matters, and the date
always points at the next time it is wanted. Members are who it's for.

A routine never leaves its column, so the board needs no extra lists:

- **Ticking it** (dashboard, HA to-do view, voice, or the due-date tick in
  the Trello app) marks the due date complete. Trello shows the green badge.
- At the **rollover time** (default 04:00) HA advances the due date to the
  next matching day at the same time, and clears the tick. Ticks made in the
  evening therefore stay visible until the next morning.
- A card with a day label but **no due date** gets one: its next matching
  day at the default deadline time (default 18:00).
- A **missed** routine stays overdue until someone ticks it. The tick then
  stays visible for the rest of that day (HA goes by the card's last
  activity time), and at the next rollover it moves on. Turn off *carry
  over* in the options and a missed routine resets to its next day instead.

**Adding a routine from Home Assistant** is `trello_todo.add_card` with
`days` and `time`: the days become the labels (added to the board if it
lacks them) and the due date lands on the next matching day still ahead,
so a routine is never born overdue. A dashboard mic that turns speech
into structured items can call this directly.

Ordinary cards (no day label) still complete by moving to the Done column.
On a board with **one column** there is nowhere to move to, so they tick in
place too: the tick marks the due date complete (a card without one is
given the moment of ticking), it stays visible for the rest of that day,
and at the next rollover HA archives it. Trello keeps the archive, so
nothing is lost, and the column empties itself overnight.
`sensor.<board>_overdue_tasks` and `_due_today` count routines by their
deadline, which is what a "needs attention" badge wants. In the `board`
attribute each card carries `routine` (its weekday numbers, Mon = 0, or
null) and `done`.

## When a chore starts asking

A card's **start date** (Trello's own, beside the due date) is when it starts
asking: the dishwasher from 20:00, due 22:30. Before the start it is not due
yet; without one it asks from the start of the day it is due. The rollover
moves the start with the due date, keeping the window's shape, and so does
changing the due date from Home Assistant. `add_card` takes `start` as a time
(`"20:00"`, the window opening that day) or a full date and time. The `board`
attribute carries each card's `start`.

## Chores that tick themselves

**Configure → Chores that tick themselves** links a card to an entity and
the state that means it is done: *Run the dishwasher* ← `sensor.dishwasher`
is `running`. When the entity goes into that state the card is ticked,
exactly as a person ticking it would, and the card's `ticked_by` in the
`board` attribute says what and when (`{"by": "Dishwasher", "at": …}`).

- Only a card that is asking counts: due today or carried over. One already
  ticked, or not due until a later day, is left alone.
- A run before the start time still counts: the job is done either way.
- Found already in the state when Home Assistant starts, or when the link is
  made, counts too.
- Unticking by hand wins: the same run will not tick it again, through a
  restart as well. Only the entity going into the state afresh does.

## Services

All target a `todo.*` entity from this integration. Columns and people can
be given by name.

```yaml
action: trello_todo.move_card
target: {entity_id: todo.chores}
data: {card_id: 64f0…, list: Doing}

action: trello_todo.set_members          # replaces the assignees; [] to clear
target: {entity_id: todo.chores}
data: {card_id: 64f0…, members: [Morgan]}

action: trello_todo.add_card
target: {entity_id: todo.chores}
data: {name: Paint the fence, list: Doing, members: [Morgan, Casey], due: "2026-10-04"}

action: trello_todo.add_card           # a routine: days become labels
target: {entity_id: todo.chores}
data: {name: Bins out, days: [mon], time: "19:00", members: [Casey]}

action: trello_todo.add_card           # asks from 20:00, due 22:30, every day
target: {entity_id: todo.chores}
data: {name: Run the dishwasher, days: [daily], start: "20:00", time: "22:30"}
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
