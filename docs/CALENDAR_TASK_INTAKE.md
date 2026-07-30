# Google Calendar task intake

HealthMes treats Google Calendar as an opt-in task input surface. Only events
whose summary begins with `[HM]` are candidates.

## v1 contract

- The event must be organized by the connected user, have no attendees, be
  non-recurring, and use the default event type.
- `[HM]` is removed from the task title.
- A timed event is a user-preferred work block. Its duration becomes
  `est_minutes`; HealthMes must not create a duplicate Calendar block when the
  existing placement is acceptable.
- An all-day event is an unplaced task. Its Calendar date becomes the task
  deadline hint; the planner may propose a concrete time block and must wait
  for confirmation before writing it.
- Missing energy demand defaults to `med`.
- Repeated polls, restarts, and full resyncs update the linked task instead of
  creating another task.
- Removing `[HM]` or deleting the source event does not delete the HealthMes
  task automatically.
- HealthMes never moves, deletes, or retitles the source input event.

## Confirmation behavior

Planner proposals remain `proposed` until a live user reply accepts or declines
the exact proposal using `적용 <handle>` or `그대로 <handle>`. Hermes attaches
a short-lived proof only for that exact inbound Telegram message; HealthMes
requires both the proof and one-time handle. Acceptance queues the block for the
next Calendar sync.
The sync creates one HealthMes-owned event, marks the proposal `pushed`, and
marks the linked task `scheduled`. Replaying the same approval or provider
revision must not create another task, proposal, or Calendar event.

## Dogfood examples

- `[HM] 백오피스 작업`, 18:00–18:45: keep the preferred block or explain why a
  re-plan is needed; a no-op is valid.
- `[HM] 투자자 목록 정리`, all day: propose a concrete block before the deadline,
  ask for confirmation, then write exactly one owned block.
