> Meeting voice now uses Attendee. See [meeting-attendee-voice.md](meeting-attendee-voice.md)
> for active joining and speech configuration. Calendar OAuth and sync setup below
> remains in use.

# Connect the operator's Google account

This step adds operator CLI commands, not customer-facing OAuth routes. Only someone
with deployment/database access can connect the shared account or run calendar sync.
No credentials are accepted from tenants. No calendar write, invitation, Gmail send,
or invitation-acceptance operation exists in this integration.

## One-time setup

1. In Google Cloud, enable **Google Calendar API** for your project.
2. Configure the OAuth consent screen. For an external app in Testing, add your bot
   email as a test user. Google testing-mode grants using Calendar permissions can
   expire after seven days; reconnect or configure the app appropriately for ongoing use.
3. Create an OAuth client of type **Desktop app**. This operator command uses PKCE and
   a temporary `http://127.0.0.1:<port>/callback` listener. It is intentionally not a
   website callback. Run connection setup on the computer where the browser opens,
   with access to the deployment database (use a secure tunnel if needed).
4. In the backend `.env`, set these values locally; do not send secrets in chat:

   ```dotenv
   GOOGLE_CLIENT_ID=<desktop OAuth client ID>
   GOOGLE_CLIENT_SECRET=<desktop OAuth client secret>
   MEETING_BOT_EMAIL=<the one account you supply>
   MEETING_TOKEN_KEY=<Fernet key generated below>
   MEETING_SYNC_DAYS=30
   ```

5. From the backend directory, install dependencies and apply database migrations:

   ```sh
   uv sync
   uv run python -m alembic upgrade head
   uv run python -m app.core.meeting_google keygen
   ```

   Put the generated key into `MEETING_TOKEN_KEY`. Keep it stable and backed up in a
   secret store separate from the database. Changing it makes saved tokens unreadable.
   The `keygen` command prints a secret intentionally; do not copy its output into logs.
6. Connect the account:

   ```sh
   uv run python -m app.core.meeting_google connect
   ```

   The browser opens Google consent. Sign in as `MEETING_BOT_EMAIL`. The callback is
   bound to a random, single-use process state with a ten-minute timeout. Only the
   verified configured email is accepted; changing an existing connection to a
   different Google identity is rejected. Access and refresh tokens are encrypted
   before persistence. `--no-browser` prints the URL without opening it automatically.
7. Inspect and sync:

   ```sh
   uv run python -m app.core.meeting_google status
   uv run python -m app.core.meeting_google sync
   ```

   Status prints only connection state and email. Sync prints calendar/event counts.
   Tokens, authorization codes, and provider response bodies are not printed.

The application requests `openid`, `email`, and `calendar.events.readonly`. The first
two identify your account; the third reads existing events. No write permission is
requested. A refresh token maintains access; cron cannot grant initial consent.

## Calendar sync

The running API syncs the connected calendar every minute and refreshes expired tokens
on demand. No separate cron is required. For an immediate operator sync:

```sh
uv run python -m app.core.meeting_google sync
```

## What sync does

- Syncs the primary calendar of the supplied account; it does not discover other users'
  calendars. Events need to appear in this calendar under Google's invitation settings.
- Fetches a bounded full snapshot from yesterday through `MEETING_SYNC_DAYS` ahead,
  expanding recurring instances and traversing all pages. This moving horizon deliberately
  does not use incremental sync tokens (Google prohibits mixing those with time bounds).
- Stores event IDs, schedules, recurrence IDs, and Meet links in the operator-only inbox.
  Re-running sync updates the same records. All-day and non-Meet events remain ineligible
  for joining. Read-only sync does not accept invitations.
- For previously seen timed events missing from the window, reads the individual event
  to distinguish cancellation from a move outside the window. 404/410 marks it cancelled.
- Updates already-linked, still-scheduled meetings on changes; cancels
  them when an event is cancelled, becomes all-day, or loses its Meet link. It does not
  revive cancelled meetings automatically or interrupt a running call.
- Commits calendar changes only after the entire sync succeeds. A failed page leaves
  the previous snapshot intact. The primary calendar is the only auto-created calendar;
  this version does not resolve conflicting copies across additional calendars.

## Meeting lifecycle

See [meeting-attendee-voice.md](meeting-attendee-voice.md) for recurring assignments,
auto-join, voice interaction and rolling workspace memory.

## References

- [Google installed-app OAuth and loopback redirects](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Google token expiration](https://developers.google.com/identity/protocols/oauth2#expiration)
- [Calendar event listing and time/sync-token constraints](https://developers.google.com/workspace/calendar/api/v3/reference/events/list)

## Dashboard access and assignment

The frontend now has `/meetings` for workspace members and `/calendar-inbox` for the
platform operator. Set `MEETING_OPERATOR_USER_IDS` to a JSON list of existing dashboard
user UUIDs and restart the backend. For example:

```dotenv
MEETING_OPERATOR_USER_IDS=["<existing-dashboard-user-uuid>"]
```

Keep this setting in the backend environment only. An organization owner/admin does
not automatically receive operator access. Email addresses are not used as an automatic
authorization rule because dashboard registration does not verify email ownership.
The user's `/api/v1/auth/me` response contains their UUID after login.

The inbox exposes only calendar status and upcoming, timed, unassigned event metadata.
The operator selects a workspace explicitly and can add an agenda. Assignment is
idempotent, serialized with calendar sync, and refuses to move an assigned event to
another workspace. Repeated copies of a meeting occurrence cannot be assigned to
different workspaces. Assignment may enable auto-join and routing future series occurrences.

Workspace members may view their workspace's meeting schedules, agendas and shared
fourteen-day memory. The dashboard shows live connection status and operator join/stop
controls. Refresh view reloads stored data; calendar sync runs every minute independently.
