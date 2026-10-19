# Meeting data model

The shared Google Calendar connection belongs to the platform operator. Calendar inbox
entries stay private until explicitly assigned to a workspace. Credentials are encrypted.

| Table | Purpose |
| --- | --- |
| meeting_google_connection | One operator account and encrypted OAuth tokens |
| meeting_calendars | Enabled calendars and sync timestamps |
| meeting_calendar_events | Discovery inbox, recurring identity and cancellations |
| meeting_series | Explicit workspace assignment and auto-join preference for a series |
| meetings | One occurrence, including original occurrence identity and auto-join setting |
| meeting_captures | Provider session state and temporary transcript awaiting memory merge |
| meeting_memories | One rolling context TEXT string per workspace |

Migration 0007 replaces per-meeting notes with the single workspace context and removes
unused meeting_jobs. Recent existing notes are preserved in the context. Operational
fields remain separate from the context string. There are no separate task/event-memory
records. Temporary transcript entries are removed atomically after successful merging.

See [the current lifecycle documentation](meeting-attendee-voice.md) for scheduling,
retention, concurrency and deployment behavior.
