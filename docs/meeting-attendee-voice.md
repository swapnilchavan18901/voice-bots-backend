# Sia meetings

The frontend calls the main backend on port 8000. Attendee joins Google Meet and
connects per-participant audio to the backend. The retired browser worker is removed.

## Configuration

```dotenv
ATTENDEE_API_KEY=your-key
ATTENDEE_API_URL=https://app.attendee.dev
MEETING_PUBLIC_BACKEND_URL=https://your-public-backend-or-tunnel
MEETING_VOICE_NAME=Sia
MEETING_VOICE_LANGUAGE=en-IN
MEETING_VOICE_SECONDS=14400
MEETING_EMPTY_LEAVE_SECONDS=30
MEETING_VOICE_MAX_PARTICIPANTS=30
MEETING_FILLER_DELAY_MS=700
```

Reuse the configured Sarvam API key/models/voice. The public backend must support
HTTPS and WebSockets. Credentials stay on the backend. Run migrations before starting:

```sh
uv run alembic upgrade head
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Use one ASGI process: it owns live voice sessions and runs the small background loops.
The audio callback has a signed, expiring grant. Workspace routes require membership;
calendar assignments and bot controls require operator access.

## Calendar and joining

Calendar sync runs every minute and expands recurring events into distinct occurrences.
Assign an event to a workspace, optionally enabling auto-join and assignment of future
occurrences of its series. Series identity is scoped to the source calendar; occurrences
use the original recurrence start, so rescheduling does not create another meeting.
Cancelled/rescheduled upcoming events are reconciled by calendar sync.

The dispatcher checks every ten seconds. It joins at the scheduled start, not when the
first human enters. A ten-minute catch-up window tolerates short restarts without joining
old meetings. Existing assignments retain manual joining until auto-join is enabled in
the dashboard. New assignments default to auto-join. Host admission still applies.

Capture rows and database locks prevent concurrent/manual/scheduled duplicate starts.
Uncertain provider creation is not blindly retried; operators must reconcile an unknown
creation with Attendee. Failed auto-join attempts can be retried manually. Status refresh
runs independently of the dashboard every fifteen seconds. Closing the dashboard does
not stop a bot. Stop sends an explicit leave request. Attendee is configured to leave
when alone for thirty seconds and after the maximum voice session duration.

## Voice and memory

Say “Sia, what's today's agenda?” or “Hey Sia, what progress did we make?” Names must
start the turn. Saying only the name arms the same person's next question for ten seconds.
Sia otherwise listens. The scope guard allows current and historical meeting questions
but rejects unrelated trivia. Answers are concise and grounded in participant evidence.
Speech interruption cancels generation/output. Sarvam PCM is paced in 100 ms chunks with
200 ms scheduling lead; provider buffering can produce a short interruption tail.
The clinic-style filler is spoken only for slow answers and never saved as evidence.
The shared text normalizer prepares text for speech without changing the source text.

`meeting_memories` contains exactly ONE `context` TEXT string per workspace, plus its
workspace key and update timestamp. It covers all meetings in the current UTC date and
preceding thirteen dates. Sections use YYYY-MM-DD headings. There are no separate task,
summary, decision or long-term transcript tables.

After a session reaches a terminal provider state and its audio processing finishes,
the memory loop sends existing memory, current participant transcript and current UTC
date to the LLM. It merges important outcomes, assignments, progress, blockers and
completion. Original observation dates are preserved. The backend also removes expired
date sections deterministically, including on every read and an hourly cleanup.

Updates hold a workspace row lock so concurrent meetings cannot overwrite each other.
Long transcripts are processed in bounded chunks. Only after every chunk succeeds does
one transaction replace the context and erase the processed temporary transcript.
Failed generation preserves the previous memory and pending transcript for automatic
retry. Temporary transcript turns expire at the fourteen-day boundary even after failure.
Existing recent per-meeting notes are migrated into the workspace string before their
columns are removed. The dashboard exposes the shared memory, not raw transcripts.

Every addressed voice question fetches current workspace memory and combines it with
recent live turns. Old information is unknown once it expires; a later observation must
not invent an old assignment date. Different workspaces never share a memory string.

This retention applies to this application's database. Attendee manages its own hosted
recordings/transcripts and account retention separately. Calendar titles/agendas and
operational session metadata are not part of the rolling meeting-memory retention.

## Limitations

The memory model is a lossy summary, not a verbatim audit trail. LLM facts and scope
classification remain probabilistic. Inputs have a 60000-character memory bound; an
invalid or truncated generation fails rather than replacing the prior memory.
Automatic joining needs this backend running with a valid Calendar connection and
Attendee configuration. No outbound invitations are created. Production multi-process
voice routing and uncertain-create reconciliation are not implemented.
