# Recurring meeting definitions

The backend stores one `meetings` row for each Google standalone event or recurring
series. It no longer stores future occurrences in `meeting_calendar_events` or
`meeting_series`; migration 0012 removes those tables after consolidating their data.

## Tables and identity

- `meeting_google_connection`: the existing deployment-level Google authorization.
- `meeting_calendars`: the existing calendars that are synchronized.
- `meetings`: one definition, identified by `(calendar_id, provider_event_id)`. The
  provider ID is the Google master event ID for recurring series. `is_recurring` and
  `recurrence` describe the series, while `next_occurrence_id`, `next_starts_at`,
  `next_ends_at`, `next_meet_url`, and `next_title` describe only its next available
  occurrence. `organization_id=NULL` means the definition is in the organizer's
  private inbox. Assignment, agenda and auto-join preference apply to the whole series.
- `meeting_captures`: one actual join attempt per `(meeting_id, provider_occurrence_id)`.
  Each capture snapshots its scheduled start/end, title, Meet URL and agenda. Its bot
  status and transcript belong to this occurrence even after the definition advances.
- `meeting_memories`: the existing single rolling text memory per organization.

For a daily standup: one definition M1, then capture C1 for Monday, C2 for Tuesday,
C3 for Wednesday. There are no pre-created captures or meeting rows for future days.
The Meet URL is not an identity key: different meetings can reuse a room, and an
individual recurrence exception can use another room.

## Calendar synchronization

The 60-second calendar loop discovers master definitions with `singleEvents=false`.
For each recurring master, Google's
[instances API](https://developers.google.com/workspace/calendar/api/v3/reference/events/instances)
resolves its occurrences and exceptions. Pages are read in memory, cancelled/expired/
untimed/non-Meet instances and previously attempted occurrences are excluded, and
only the earliest remaining instance is saved. Google's timestamps preserve timezone
and DST behavior. All pages must succeed before the transaction commits.

The lookahead remains 30 days. A recurring series with no occurrence inside that
window keeps its definition and settings, with null next-occurrence fields. It is
checked again on subsequent syncs, so a monthly/yearly meeting is not treated as
permanently ended. This window limits fetching, not the number of persisted rows.

Known definitions missing from discovery are fetched directly to distinguish a move
outside the window from cancellation/deletion. Recurrence exceptions do not create
extra definition rows. Calendar transfers to a different organizer disable future
joins rather than silently moving conversations between workspaces; operator review
is needed for such transfers.

## Joining and history

The 10-second dispatch loop uses the next-occurrence pointer and auto-join preference.
Only assigned definitions can be joined. Scheduled joins start at the scheduled time
(with the existing ten-minute catch-up window). Authorized manual joins can start up
to ten minutes early. The capture is committed before contacting Attendee, and a row
lock plus the unique occurrence constraint prevents duplicate provider requests.

A later sync advances past the reserved occurrence regardless of summary success.
An active capture continues using its own immutable snapshot. Provider status refresh
operates by capture ID, so late completion/failure for Monday cannot change Tuesday's
schedule. Connection/stop controls prefer an active capture even if the definition
already points to tomorrow. Only one active capture runs for a definition at a time.

A completed or failed occurrence is not automatically joined again. Unknown provider
outcomes remain visible rather than blindly creating duplicate bots. Restarting does
not repeat an occurrence already represented by a capture. Missed occurrences age out
of the next pointer on calendar sync without generating fake session-history rows.

Memory behavior is unchanged: completed transcripts are summarized into organization
memory, then processed transcript entries are cleared atomically. The 14-day retention
window remains in effect. This refactor does not add permanent transcript storage.

## API compatibility

Existing organization meeting, join, leave, connection, schedule and memory routes
remain. `/operator/meeting-events` now returns private unassigned definitions and its
assignment endpoint accepts a definition ID. Recurring assignment always applies to
the entire definition; `assign_series` is accepted as a deprecated, ignored input for
older clients. The dashboard no longer displays the occurrence-only assignment option.

Meeting responses expose `is_recurring`, `next_occurrence_id`, `next_starts_at`,
`next_ends_at` and `next_meet_url`. Compatibility fields `starts_at` and `ends_at`
alias the next occurrence and are nullable when none is scheduled in the window.
Definition status is `scheduled`, `cancelled`, or `completed` (for finished single
events). Live session status is returned by the connection endpoint and capture history.

`GET /api/v1/organizations/{org_id}/meetings/{meeting_id}/captures` returns paginated
session history, including original occurrence ID/time, title, URL, session status and
memory status. `offset` defaults to 0; `limit` defaults to 50 and is capped at 100.
Workspace membership and meeting ownership apply to this endpoint as well.

## Migrating existing data

1. Stop the backend so calendar sync/join requests cannot run during consolidation.
2. Back up the database.
3. Run `uv sync`, then `uv run alembic upgrade head`.
4. Restart the backend and allow its next successful calendar sync to refresh pointers.
5. Refresh open dashboards; a consolidated series retains one of its original meeting IDs.

The migration preserves capture IDs, provider bot IDs, speaker mappings, raw transcripts,
original occurrence times and meeting memory. It attaches historical captures to a
single canonical definition. Existing series preferences determine agenda and auto-join;
if absent, the oldest linked meeting's settings are used. Review these settings for
series whose occurrences previously had different preferences. Calendar-only inbox rows
become unassigned definitions, and standalone legacy meetings without a calendar link
are preserved. No Google or Attendee requests run during migration.

A series assigned to multiple organizations, ambiguous cross-calendar ownership, or
duplicate historical captures for one occurrence blocks the migration instead of
merging or dropping tenant history. Resolve those conflicts before retrying. PostgreSQL
runs the migration transactionally. Consolidation is intentionally not automatically
reversible; restore the backup if a downgrade is required.

## Verification

```sh
uv run python -m pytest tests/test_meeting_definitions.py tests/test_outreach.py -q
```

Set `MEETING_TEST_POSTGRES_URL` to a disposable PostgreSQL database to include real
concurrency and full historical-data migration tests. It must allow temporary schema
and database creation. Those tests create and remove their own isolated schemas and
databases. External calendar, voice and AI calls are simulated. They do not place
real calls, join meetings, or change Google Calendar.
