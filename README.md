# Voice bots backend

FastAPI + async SQLAlchemy + PostgreSQL, managed with uv. Audio flows through
WebRTC → Sarvam STT → Sarvam LLM → Sarvam TTS → WebRTC using Pipecat 1.12.0.

## Run locally

Requires Python 3.12 or 3.13, uv, Docker (or an existing PostgreSQL server), and a Sarvam API key.

```sh
cp .env.example .env
# Edit .env: set a random JWT_SECRET and your SARVAM_API_KEY.
uv sync --locked
docker compose up -d db
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open http://localhost:8000/docs for the API. Register, then use the returned
`access_token` in Swagger's **Authorize** dialog. Authentication endpoints accept JSON.
Tables are managed by Alembic; starting the server does not create tables.

## Layout

```text
app/
  ai/         Sarvam pipeline and WebRTC session lifecycle
  api/        HTTP routes and authentication dependencies
  business/   Business rules, authorization, and database queries
  core/       Configuration, password hashing, JWT and invitation tokens
  db/         SQLAlchemy models, async sessions, Alembic migrations
  schemas/    Request and response validation
  main.py     Application composition and startup/shutdown
```

`business` is the spelling used for the requested business-logic folder.

## Platform bot model

You own the bot templates and their base system prompts. Customers select a template,
create an organization-specific bot configuration, and provide `company_context`
(company details, services, opening hours, FAQs). Customers cannot replace the
platform prompt. Conversation-only bots send company context as a separate LLM message.
Clinic bots use the reference receptionist role and per-step prompts, with saved context
inserted as factual data. Action tools enforce bot scope, validate inputs and require
caller confirmation independently of the prompt.

Migrations seed General Assistant, Clinic Appointment Assistant, and Sales Assistant.
Clinic templates include connected scheduling tools and persisted appointments. Sales
remains a conversation template; CRM writes and outbound telephone calls require
additional integrations. The transport here is WebRTC.

A template ID selects a platform product. The returned organization **bot ID** identifies
the customer's configured instance and is the ID used by website integrations.
Template changes apply to subsequent calls for all instances using that template.
Disabling a template blocks new calls. Existing calls retain their starting configuration.

Operator-only template management uses database access from the CLI:

```sh
uv run python -m app.core.manage_template path/to/template.json
```

The JSON accepts `slug`, `name`, `description`, `system_prompt`, `is_active`, and optional `service_type` (`conversation` or `clinic`).
Omitting `service_type` preserves an existing template’s service; new templates default
to `conversation`.
It creates or updates by slug. There is no customer-facing template mutation endpoint.

## Accounts and organizations

- Email/password signup hashes passwords with Argon2 and atomically creates the user,
  their default organization, and an owner membership.
- The default organization is named from the email local part: `alex@gmail.com` → `alex`.
  Emails are lowercased; Gmail dots and plus-address aliases are not collapsed.
- Users can create more organizations and belong to any number of organizations.
- Owners and admins manage bots and invite members. Only owners invite admins.
  Members can view bots and start their own voice sessions.
- Every organization operation checks membership. Bot IDs are also checked against
  the organization in the URL. Voice sessions belong to the initiating user and bot.
- Invitations expire after seven days, match the recipient's signed-in email, and
  are single-use. Only a SHA-256 digest of the random token is stored.
- Invitation creation returns a token for the inviter to share privately. There is
  no email delivery integration yet; the API does not send invitation emails.

## API

All routes below are under `/api/v1`; `/health` is a separate liveness endpoint.

| Method | Route | Purpose |
|---|---|---|
| POST | `/auth/register` | Register and create personal organization |
| POST | `/auth/login` | Obtain an expiring bearer JWT |
| GET | `/auth/me` | Current user |
| GET / POST | `/organizations` | List memberships / create organization |
| GET | `/organizations/{org_id}/members` | List members and roles |
| POST | `/organizations/{org_id}/invitations` | Create invitation |
| POST | `/invitations/accept` | Accept with `{"token":"..."}` |
| GET / POST | `/organizations/{org_id}/bots` | List / create bots |
| GET / PUT / DELETE | `/organizations/{org_id}/bots/{bot_id}` | Read / replace / delete bot |
| POST | `/organizations/{org_id}/bots/{bot_id}/voice/offer` | Start voice session |
| DELETE | `/organizations/{org_id}/bots/{bot_id}/voice/{pc_id}` | Stop your voice session |

Bot creation accepts `template_id`, `name`, `company_context`, and `language`
(default `hi-IN`). PUT replaces the name, context, and language; the selected template
is immutable for that instance. Unknown fields such as `system_prompt` are rejected.
Sarvam model names and voice are configured in `.env`; defaults are `saaras:v4`,
`sarvam-105b`, `bulbul:v3`, and `shubh`. Models must be available on your Sarvam account.

## Website integration and public API

Dashboard users continue to authenticate with JWT. Owners/admins create organization
API keys. Keys are shown once, stored only as SHA-256 hashes, expire in 90 days by
default (configurable 1–365 days), and can be revoked. A key can issue call tokens for
any bot in its organization. It cannot access dashboard routes or other organizations.

| Method | Route | Authentication |
|---|---|---|
| GET | `/api/v1/bot-templates` | Dashboard JWT |
| POST / GET | `/api/v1/organizations/{org_id}/api-keys` | Owner/admin JWT |
| DELETE | `/api/v1/organizations/{org_id}/api-keys/{key_id}` | Owner/admin JWT |
| POST | `/api/v1/public/bots/{bot_id}/session-token` | `X-API-Key` |
| POST | `/api/v1/public/bots/{bot_id}/webrtc/offer` | Bearer voice token |
| DELETE | `/api/v1/public/bots/{bot_id}/webrtc/{pc_id}` | Same bearer voice token |

1. Customer selects a template from `/api/v1/bot-templates`.
2. Customer creates an organization bot with their company context and saves its ID.
3. Customer generates an API key and stores it on **their server**, never in browser JS.
4. Their server requests a voice token for that bot and returns it to their browser.
5. Browser sends its SDP offer directly to the public WebRTC endpoint using that token.

The token can start one call within five minutes. A conditional database update prevents
replay. Even failed signaling consumes the token; request a new one to retry. The token
remains usable to stop that call until its returned `expires_at`. Revoking the API key
also invalidates its outstanding voice tokens for subsequent HTTP requests. Already
running media sessions are not forcibly disconnected by key revocation and end through
disconnect or the configured time limit.

Example customer-server request (API key comes from server-side configuration):

```sh
curl -X POST "$PLATFORM_URL/api/v1/public/bots/$BOT_ID/session-token" \
  -H "X-API-Key: $VOICE_PLATFORM_API_KEY"
```

Protect the customer's token-issuing endpoint with their authentication/abuse controls.
Public browser routes allow cross-origin bearer requests without cookies; dashboard
CORS stays restricted to `CORS_ORIGINS`. CORS is not an authentication mechanism.
Public Swagger docs are at `/api/v1/public/docs`.

See [examples/embed.js](examples/embed.js) for a browser helper. Call it from a user
click with your bot ID, platform URL, customer-server token endpoint, and an audio
HTML element. It returns a function to stop the call. This example uses full ICE
gathering and requires TURN configuration for production networks.

## Browser WebRTC contract

The backend accepts a complete SDP offer with gathered ICE candidates:

```json
{"type": "offer", "sdp": "v=0\r\n..."}
```

Send it to `/api/v1/public/bots/{bot_id}/webrtc/offer` with `Authorization: Bearer <voice-token>`.
Dashboard users can still use their JWT on the organization-scoped `/voice/offer` route.
The response contains `type`, `sdp`, and `pc_id`; pass `type` and `sdp` to
`RTCPeerConnection.setRemoteDescription`. No raw audio passes through HTTP.

Create the microphone audio track, a receive-audio transceiver, and a data channel
before creating the offer (Pipecat SmallWebRTC uses a data channel for control).
Wait for `iceGatheringState === "complete"` before posting `pc.localDescription`.
Attach remote audio via `pc.ontrack` to an audio element. The user speaks first.
Use localhost or HTTPS for browser microphone access. Configure matching ICE/TURN
servers on the browser and in `ICE_SERVERS` for connections across NAT/firewalls.
This initial API uses full ICE gathering; trickle ICE and SDP renegotiation are not exposed.

Connections time out if the client does not connect within 30 seconds. Sessions
end on disconnect, explicit DELETE, application shutdown, or the configured time
limit. `MAX_VOICE_SESSIONS` bounds active and negotiating sessions per process.
Missing Sarvam credentials return HTTP 503 without opening a peer connection.

## Verification

```sh
uv run pytest -q
uv run ruff check app tests
uv run ruff format --check app tests
# Render PostgreSQL migration SQL without connecting to a database:
uv run alembic upgrade head --sql
```

API tests use an isolated SQLite database for portable checks. Migration tests verify
that existing bot prompts are preserved as company context and check upgrade/downgrade.
Migration `0002` adds `bot_templates`, `api_keys`, and `voice_grants` and changes `bots`
to reference a template. Existing bots use the seeded general-assistant template. PostgreSQL-specific
locking/concurrency behavior still needs verification against a running PostgreSQL
instance. Offline voice tests validate pipeline construction and WebRTC negotiation;
real speech quality and Sarvam connectivity require a key and microphone client.

## Deployment scope

This is a backend foundation. Run a single ASGI worker: sessions live in process
memory. Horizontal scaling needs session-aware routing and distributed limits.
Use TLS, a production PostgreSQL service, and TURN where necessary. The included
Compose password is for local development only.

Email verification/delivery, password recovery, refresh/revocation tokens, account
rate limiting, membership removal/ownership transfer, and persisted call history
are not implemented. Place authentication endpoints behind gateway rate limits
before public exposure; also enforce per-organization quotas and public API/token-issuing
rate limits. Expired voice-grant rows currently need operator-managed periodic cleanup.
Voice media/transcripts are not persisted by this app.

## Integration references

- [Pipecat Sarvam example](https://github.com/pipecat-ai/pipecat/blob/main/examples/voice/voice-sarvam.py)
- [Sarvam Pipecat guide](https://docs.sarvam.ai/api/integration/pipecat-production-guide)
- [Pipecat SmallWebRTC](https://docs.pipecat.ai/client/concepts/choosing-a-transport)


## Clinic data comes from the agent context

The clinic workflow and appointment table structure are shared platform capabilities.
Each agent's `company_context` is the source for its clinic facts, doctors, and calendar.
There are no default clinic names, doctors, contact details, fees, or opening hours.

The frontend agent editor keeps the free-text background and adds clinic fields for
name, address, front desk phone, fees, parking, booking policy, doctors and schedule.
It saves everything in the existing `company_context` field. Free text alone remains
valid for answering questions; actual scheduling requires explicit doctors, timezone,
working days, opening/closing times, and appointment duration. For Markdown contexts, labelled clinic details, doctor tables and dated available-slot
tables are read deterministically. Dates listed for one doctor are never extended to
other doctors or unlisted days. Unrecognised prose cannot become a booking calendar.
Missing facts stay missing; the assistant asks or offers staff help.

API clients can use this context structure (shown as the decoded content of the string):

```json
{
  "background": "Our clinic treats families. Explain our services from this background.",
  "clinic": {
    "name": "Customer Clinic",
    "address": "Your clinic address",
    "phone": "Your front desk phone",
    "fees": "Your consultation fees",
    "timezone": "Asia/Kolkata",
    "working_days": [0, 1, 2, 3, 4, 5],
    "open_time": "10:00",
    "close_time": "18:00",
    "lunch_start": "14:00",
    "lunch_end": "15:00",
    "slot_minutes": 30,
    "doctors": [{"name": "Dr. Customer", "specialty": "Family medicine"}]
  }
}
```

Working days use Monday=0 through Sunday=6. Break times are optional; provide both
or neither. Use local HH:MM times and an IANA timezone. Doctor IDs are returned by
`/clinic/info` and `/clinic/availability`; they are assigned by the server, scoped to
the bot, and cannot be used for another bot's appointments. Saving an agent synchronizes
its doctor records from context. Removed doctors stay only for historical appointment
references and cannot receive new bookings.

Apply `uv run alembic upgrade head` for migration `0004`, which scopes doctor records
to bots. Existing shared seed records remain only for historical foreign keys and are
never exposed as fallback clinic data. Update existing agents with their own clinic
context to enable scheduling. Agent edits apply to subsequent calls; clinic tools read
the current saved context when executed.

Dashboard routes under `/api/v1/organizations/{org_id}/bots/{bot_id}/clinic`:

| Method | Suffix | Input |
|---|---|---|
| GET | `/info` | Saved clinic facts, background, doctors and booking readiness |
| GET | `/availability` | `day=YYYY-MM-DD`, optional `doctor_id` |
| POST | `/appointments` | `doctor_id`, `patient_name`, `patient_phone`, `start_time`, optional `reason` |
| POST | `/appointments/lookup` | `patient_phone` |
| POST | `/appointments/reschedule` | `appointment_id`, `patient_phone`, `new_start_time`, optional `doctor_id` |
| POST | `/appointments/cancel` | `appointment_id`, `patient_phone` |

The receptionist greets in the configured language using saved clinic context, gathers
caller details, looks up appointments, answers FAQs, and confirms actions before using
scheduling tools. Only successful tool results confirm a booking. Emergency reports,
staff callback requests and FAQ gaps are logged. Contact details and callback promises
come from context. Phone lookup follows the reference demo's caller-supplied phone
model; it does not verify identity with an OTP. Confirmation is conversational.

Appointments belong to the bot. A partial unique index prevents double bookings for
the same bot, doctor and time. Members can read; dashboard mutations require an owner
or admin. Both dashboard and public voice calls receive bot-scoped clinic tools.


## Reference Clinic Bot prompts

Clinic-enabled voice calls use the fixed role and per-step prompt templates in
`app/ai/clinic/flow/prompts.py` and `nodes.py`, ported from Clinic Bot. The workflow uses
Pipecat Flows: greet/identify → intent → select doctor and slot → confirm → close.
FAQ, appointment selection, cancellation, rescheduling and goodbye have their own
nodes. Only the current step's actions are exposed. Agent name, clinic facts, doctors,
calendar and language are supplied dynamically; the receptionist workflow is fixed.
`BotTemplate.system_prompt` remains the base prompt for conversation-only products.

The doctor selection step introduces available doctors and specialties. If the caller
provides a symptom first, it narrows the choice to a relevant supported specialty,
rather than repeating every specialist. For a fever, it asks whether the patient is
an adult or child when necessary to select a general physician or pediatrician.

Booking changes and staff requests require an actual new, affirmative caller transcript
after the confirmation question. The model cannot confirm its own question in the same
turn. A staff request is logged only after agreement; no callback or live staff
notification delivery is connected, so the assistant does not claim delivery.

Markdown slot tables can be supplied in the existing company context, for example:

```markdown
## Doctors
| Doctor | Specialty |
|---|---|
| Dr. Customer | General Physician |

## Available appointment slots
### Wednesday, 14 October 2026
| Doctor | Available slots |
|---|---|
| Dr. Customer | 10:00 AM, 11:20 AM, 5:00 PM |
```

Include a labelled appointment length and timezone in clinic details. India-local time
is the platform's legacy convention when a Markdown schedule omits its timezone.
Structured doctor entries also support `available_slots` as local ISO datetime strings.
Slot tables are the source allowlist; the database removes slots as appointments are booked.
Examples of patients and test utterances in context are not used as the caller's identity.

## Restore default templates after clearing data

To restore missing General Assistant, Clinic Appointment Assistant and Sales Assistant
records, run `uv run python -m app.core.seed_templates`. This restores the default IDs
and service types and preserves existing template customizations. It is safe to repeat.
Migrations do not rerun seed inserts once their revisions have already been applied.
Keep `bot_templates` and `alembic_version` out of a reset intended only for customer data.

## Meeting facilitator: shared Google calendar

The meeting feature uses one operator-owned Google account for read-only calendar
sync. Operators assign events to workspaces and control joining through Attendee.
The backend runs calendar sync, automatic joining, provider status refresh, and rolling
workspace memory updates. See [operator setup](docs/meeting-google-setup.md) for OAuth
credentials and connection commands, [meeting voice](docs/meeting-attendee-voice.md)
for joining and spoken questions, and [database design](docs/meeting-facilitator-database.md)
for workspace ownership and retention.


## Pipeline and client organization

- `app/ai/clinic/`: clinic Pipecat flow, prompts, handlers and pipeline setup.
- `app/ai/meeting/`: meeting audio turns, prompts and speech orchestration.
- `app/ai/conversation/`: generic WebRTC conversations.
- `app/ai/common/`: shared speech normalization and WebRTC wiring.
- `app/ai/pipeline.py`: WebRTC dispatch only; domain pipelines never import each other.
- `app/clients/`: exported Attendee, Google HTTP, Sarvam HTTP/Pipecat and database
  factories. Stateful Pipecat processors are created per call. HTTP clients belong
  to their service or context manager and must be closed by that owner.
- `app/env.py`: the only settings definition and environment-file loader. Import
  `get_settings()` to reuse the cached validated settings. The `.env` path resolves
  from the backend directory, independent of the shell's working directory.

Configuration validates numeric limits, database driver, service URLs, CORS origins,
ICE URLs, nonblank model/voice names, JWT secret length and optional Fernet keys.
Optional provider credentials may be empty to disable those integrations; voice
entry points still check required credentials before starting sessions.

Clinic information comes only from the bot's saved `company_context`. Markdown
clinic facts and doctor/slot tables are supported, as is existing JSON context.
Doctor database rows are derived references for appointment history, not a second
editable clinic profile. Removing a doctor from context removes them from available
booking choices. The backend does not require a separate clinic-details form.
# voice-bots-backend

### Daily clinic patients

The Clinic Bot section includes setup, a dedicated voice test ground, and a daily
patient register. The register uses the appointment date in the clinic's timezone
and reads confirmed bookings already saved by HTTP or voice. Cancelled bookings
remain visible. Owners and admins can mark or undo a doctor visit; members can
view the list. A visit mark does not release the booked slot.

Apply migration `0009` before using this version: `uv run alembic upgrade head`.
Existing appointments start with `visited = false`.

- `GET /api/v1/organizations/{org_id}/bots/{bot_id}/clinic/appointments?day=YYYY-MM-DD`
- `PATCH /api/v1/organizations/{org_id}/bots/{bot_id}/clinic/appointments/{appointment_id}/visit`
  with `{ "visited": true }` (or `false` to undo).
