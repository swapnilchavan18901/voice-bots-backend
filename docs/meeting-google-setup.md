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
- Stores event IDs, schedules, recurrence IDs, and Meet links in the organizer-private inbox.
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

The frontend has `/meetings` for workspace members and `/calendar-inbox` for each
meeting organizer. Apply migration `0010` before running this version.

Google's event `organizer.email` identifies whose inbox an event belongs to. Signup
emails are not proof of ownership. After independently verifying both the person's
calendar email and dashboard account, a deployment operator links that identity:

```sh
uv run python -m app.core.meeting_google link-organizer --user-id <dashboard-user-uuid> --email <verified-email>
```

The email must match the dashboard account. This action is available only through
trusted deployment tooling; users and workspace owners cannot link or claim identities
through the web API. To revoke a link:

```sh
uv run python -m app.core.meeting_google unlink-organizer --user-id <dashboard-user-uuid>
```

On the next calendar sync, a linked organizer with exactly one current membership
gets automatic assignment to that workspace. With multiple memberships, only that
person sees their pending events and chooses from their own workspaces. Their other
workspaces and pending choices are never exposed to coworkers, workspace owners, or
platform operators through the inbox API. Missing, unknown, or unlinked organizer
emails stay private and unassigned. Legacy rows acquire organizer metadata on the
next successful sync; legacy series preferences require explicit organizer confirmation.

Assignments recheck current membership on submission. Repeated copies of an occurrence
cannot be assigned to different workspaces. Choosing a series stores a preference for
that organizer only, and future sync rechecks membership before using it. Once assigned,
the meeting's title, schedule, link, and agenda become visible to the selected workspace's
members. Assignment preferences and the organizer's other memberships remain private.

Manual bot join/stop and scheduling controls still require `MEETING_OPERATOR_USER_IDS`,
a JSON list of trusted dashboard user UUIDs in the backend environment. Organization
owners/admins do not automatically receive those bot-control permissions. Calendar
sync runs every minute; Refresh view only reloads the saved inbox.
