# Sia Backend

## Purpose and architectural approach

Sia connects voice conversations to concrete business workflows: booking a clinic visit, answering a question during a meeting, and carrying useful meeting context forward. This backend owns those workflows, their access rules, and the lifecycle of the external services involved.

The central design decision is to separate conversational interpretation from business authority. Speech models understand and generate language; business components decide what may happen, validate the action, and persist the outcome. The frontend receives information prepared for the current user and workspace.

```text
┌──────────────────────────────┐    ┌──────────────────────────────┐
│ BROWSER VOICE                │    │ MEETING VOICE                │
│ Clinic / conversation        │    │ Google Meet / Attendee       │
└──────────────────────────────┘    └──────────────────────────────┘
                │                                   │
                └─────────────────┬─────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ DOMAIN ORCHESTRATION                                             │
│ Clinic flow / conversation runtime / meeting turns               │
└──────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ BUSINESS COMPONENTS                                              │
│ Scheduling · confirmation · assignment · memory                  │
└──────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ DURABLE OUTCOMES                                                 │
│ Appointments · visit completion · meeting context                │
└──────────────────────────────────────────────────────────────────┘
```

Sarvam supplies STT, LLM, and TTS services. Pipecat orchestrates browser voice sessions; Attendee bridges meeting audio in both directions.

## 1. Workspace access and feature delivery

```text
┌──────────────────────────────────────────────────────────────┐
│ REQUEST SCOPE                                                │
│ Current membership + resource ownership                      │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ POLICY EVALUATION                                            │
│ Action permissions + feature classification                  │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ WORKSPACE PROJECTION                                         │
│ Authorized resources + counts + action capabilities          │
└──────────────────────────────────────────────────────────────┘
```

- Membership and resource ownership are checked on the backend. Knowing an identifier does not grant access to another workspace's bot, patient, meeting, or key.
- The workspace overview assembles feature-specific bot lists, active templates, team information, and management capabilities in one place.
- Removing membership takes effect on subsequent requests. A previously issued sign-in token does not preserve workspace access.
- Platform operator privileges and workspace membership are separate responsibilities. Meeting controls still require access to the relevant workspace.
- Organization selection, feature classification, patient counts, and visit eligibility are prepared server-side. Local search and selected tabs remain presentation concerns.

## 2. Bot configuration and template lifecycle

```text
┌──────────────────────────────┐    ┌──────────────────────────────┐
│ PLATFORM TEMPLATE            │    │ WORKSPACE CONTEXT            │
│ Behavior + service type      │    │ Language + business facts    │
└──────────────────────────────┘    └──────────────────────────────┘
                │                                   │
                └─────────────────┬─────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ RUNTIME PREPARATION                                              │
│ Validate context · synchronize clinic doctors                    │
└──────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ DOMAIN DISPATCH                                                  │
│ Clinic flow                  Conversation pipeline               │
└──────────────────────────────────────────────────────────────────┘
```

- Bot configuration supplies business-specific information without exposing platform system prompts through the workspace overview.
- Clinic context supplies clinic facts, doctors, scheduling details, and timezone information. The backend interprets that context and synchronizes the doctor configuration used by booking.
- Runtime preparation selects the correct domain pipeline. Meetings have their own calendar and participant-audio lifecycle rather than entering through the browser bot dispatcher.

## 3. Clinic receptionist and appointment workflow

```text
┌──────────────────────────────────────────────────────────────┐
│ AUDIO INGESTION                                              │
│ Browser WebRTC → Sarvam STT                                  │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ CLINIC FLOW MANAGER                                          │
│ Caller state + intent + configured clinic facts              │
└──────────────────────────────────────────────────────────────┘
                                │
             ┌──────────────────┼──────────────────┐
             ▼                  ▼                  ▼
  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
  │ BOOK           │  │ RESCHEDULE     │  │ CANCEL         │
  └────────────────┘  └────────────────┘  └────────────────┘
             │                  │                  │
             └──────────────────┼──────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ BUSINESS VALIDATION                                          │
│ Doctor / slot availability or existing appointment           │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ CONFIRMATION GATE                                            │
│ Read back → subsequent affirmative caller turn               │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ PERSISTENCE → SPOKEN RESULT                                  │
│ Validated change → Sarvam TTS → browser playback             │
└──────────────────────────────────────────────────────────────┘
```

- **Caller state:** phone digits can accumulate across turns; appointment lookup remains scoped to the clinic and caller.
- **Find availability:** use configured doctors, schedules, occupied slots, and clinic-local time. The model receives verified availability instead of inventing appointment times.
- **Confirm:** booking, rescheduling, and cancellation pass through explicit confirmation nodes. Handlers check the conversation state and a subsequent confirming caller turn before applying the change.
- **Commit:** the booking component validates the requested operation and saves the result. A spoken promise alone is not a completed booking.
- **Answer questions:** clinic FAQs use configured facts. Missing information leads to a front-desk contact rather than an invented answer.
- **Handle exceptions:** an emergency branch ends the normal booking flow with emergency guidance. A human-help branch can record a confirmed staff request.

**Current boundary:** recording a staff request does not deliver a notification, place a callback, or transfer a live call. The conversation explicitly reflects that limitation.

## 4. Daily patients and visit completion

```text
┌──────────────────────────────────────────────────────────────┐
│ APPOINTMENT REGISTER                                         │
│ Clinic + appointment day in clinic-local time                │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ SERVER-PREPARED VIEW                                         │
│ Patient entries · counts · visit eligibility                 │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ VISIT UPDATE                                                 │
│ Role + booking status + date validation                      │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ PERSIST AND REFRESH                                          │
│ Visit flag changes; booked slot remains occupied             │
└──────────────────────────────────────────────────────────────┘
```

- The register is grouped by the appointment day, not by the day a person first called or created the booking.
- Owners and admins can update visit completion; members can view the register.
- Future appointments and cancelled appointments cannot be marked visited.
- Visit completion is separate from booking status, so marking a patient visited does not release their booked slot. A visited appointment must be unmarked before rescheduling or cancellation.
- Counts and checkbox eligibility come from the backend and are refreshed after a change.

## 5. Calendar ingestion and private assignment

```text
┌──────────────────────────────────────────────────────────────┐
│ CALENDAR INGESTION                                           │
│ Shared Google connection → reusable meeting definition          │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ ORGANIZER RESOLUTION                                         │
│ Verified calendar identity + current memberships             │
└──────────────────────────────────────────────────────────────┘
                                │
             ┌──────────────────┼──────────────────┐
             ▼                  ▼                  ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ NO MEMBERSHIP    │  │ ONE WORKSPACE    │  │ MULTIPLE         │
│ Unassigned       │  │ Auto-assign      │  │ Private choice   │
└──────────────────┘  └──────────────────┘  └──────────────────┘
                                │                  │
                                └─────────┬────────┘
                                          ▼
┌──────────────────────────────────────────────────────────────┐
│ ASSIGNED WORKSPACE MEETING                                   │
│ One definition + next occurrence + session history      │
└──────────────────────────────────────────────────────────────┘
```

- Assignment follows the event's organizer email, matched to an operator-verified calendar identity. A self-declared signup email is insufficient.
- The Google connection is shared and operator-managed; this is not a separate Google OAuth connection for every dashboard user.
- A user with multiple organizations receives only their own membership choices. Their pending inbox and those choices are private to them, including from other workspace owners.
- Once assigned, the meeting belongs to the selected workspace and becomes visible through that workspace's meeting access rules.
- One `meetings` row represents a standalone event or an entire recurring series. Unassigned definitions appear only in the organizer's private inbox; assigning a recurring definition assigns the series.
- Google resolves recurrence and exceptions inside a rolling 30-day window. Only the next unattempted occurrence is persisted on the definition; future occurrences do not become database rows.
- Actual join attempts create `meeting_captures` rows with immutable occurrence timing, title, link, and agenda. Calendar advancement is independent of capture completion and memory generation.
- A unique meeting/occurrence pair prevents duplicate join attempts. Cancelled, expired, untimed, or non-Meet occurrences are not eligible for assignment or joining.
- See [recurring meeting storage, migration and APIs](docs/meeting-definitions.md) for the schema and upgrade procedure.

## 6. Meeting participation and spoken assistance

```text
┌──────────────────────────────────────────────────────────────┐
│ JOIN ORCHESTRATION                                           │
│ Assigned meeting → scheduler / authorized manual join        │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ ATTENDEE / GOOGLE MEET                                       │
│ Participant audio → authenticated audio connection           │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ TURN PROCESSING                                              │
│ Per-speaker VAD → Sarvam STT → captured human turn           │
└──────────────────────────────────────────────────────────────┘
                                │
                   ┌────────────┴────────────┐
                   ▼                         ▼
┌──────────────────────────────┐    ┌──────────────────────────────┐
│ ORDINARY DISCUSSION          │    │ DIRECT ADDRESS               │
│ Retain context; listen       │    │ Enter response pipeline      │
└──────────────────────────────┘    └──────────────────────────────┘
                                                    │
                                ┌───────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ GROUNDED RESPONSE                                            │
│ Scope check → agenda + human evidence + memory → LLM         │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ SPEECH OUTPUT                                                │
│ Normalize → Sarvam TTS → paced audio frames                  │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ ATTENDEE / GOOGLE MEET                                       │
│ Synthesized reply played to participants                     │
└──────────────────────────────────────────────────────────────┘
  Human speech during a reply ──► cancel pending generation/output
```

- **Attendee is the transport bridge:** it joins the meeting, supplies participant audio, and plays the synthesized reply. It is present on both sides of the STT–LLM–TTS loop.
- **Speaker handling:** each participant has separate voice-activity state. Completed speech turns are transcribed and retained for meeting context.
- **Response gating:** ordinary discussion contributes context. A direct address triggers the answer path, including a short follow-up window when the user initially says only the bot's name.
- **Grounding:** the answer uses the agenda, participant discussion, and retained workspace memory. Scope checks limit the assistant to meeting-related questions.
- **Evidence discipline:** questions and previous bot answers are not treated as proof that a task was assigned or completed. The assistant distinguishes proposals, commitments, and confirmed outcomes.
- **Interruption:** new human speech invalidates pending replies and cancels ongoing generation/output work. Audio already delivered to the provider may still play briefly.
- **Playback:** replies are normalized for speech and streamed as paced audio frames. Optional filler speech covers a slow answer without becoming part of the factual transcript.

**Connected components:** calendar assignment, scheduling, Attendee lifecycle management, Sarvam speech services, transcript capture, and workspace memory. The assistant can explain meeting context; it does not execute the tasks discussed in the meeting.

## 7. Meeting memory and retention

```text
┌──────────────────────────────────────────────────────────────┐
│ MEMORY INPUT                                                 │
│ Completed capture + human turns + prior workspace memory     │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ BOUNDED PROCESSING                                           │
│ Chunk transcript → generate dated context → validate         │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ ATOMIC UPDATE                                                │
│ Serialize workspace updates; save + remove processed turns   │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ RETENTION AND REUSE                                          │
│ 14 UTC calendar days → future meeting answer context         │
└──────────────────────────────────────────────────────────────┘
```

- Memory belongs to the workspace, so it can connect discussions across meetings assigned there.
- Updates summarize human turns; bot replies do not become independent evidence.
- Large transcripts are processed in bounded chunks. Workspace updates are serialized, and processed text is removed only with the successful memory update.
- Failed updates remain eligible for retry. Retention cleanup also removes aged raw text so failures do not create indefinite transcript storage.
- Memory is limited to fourteen UTC calendar days. Missing historical information remains unknown.

## 8. General conversation and website voice access

```text
┌──────────────────────────────────────────────────────────────┐
│ SERVER-SIDE INTEGRATION                                      │
│ Workspace credential → short-lived bot-bound grant           │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ SESSION ADMISSION                                            │
│ Validate grant → atomically consume once → WebRTC session    │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ CONVERSATION RUNTIME                                         │
│ STT → template + company context → LLM → TTS                 │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ SESSION LIFECYCLE                                            │
│ Playback · duration limits · disconnect cleanup              │
└──────────────────────────────────────────────────────────────┘
```

- General conversation shares the browser audio infrastructure with the clinic feature but has its own conversational configuration.
- External integrations keep the long-lived credential on the customer server. The browser receives a limited voice grant.
- A grant can start at most one call; token consumption is atomic. Expiry, bot ownership, and credential revocation are checked when resolving it.
- Session management handles connection ownership, duration limits, disconnects, and cleanup. Dashboard test grounds use authenticated workspace access to the same domain pipelines.

## 9. Team collaboration and operational resilience

- **Team invitations:** a workspace owner or admin creates an invitation for a specific email and role. The recipient accepts the token while signed in. Owner-only rules govern invitations granting admin access; delivery of the token is a separate user action.
- **Credential lifecycle:** authorized workspace managers create and revoke integration keys. The raw key is shown at creation, while subsequent access uses its stored digest.
- **Independent background work:** calendar sync, scheduled joins, provider status refresh, memory updates, and retention run as separate periodic tasks. A failed cycle is logged and retried without ending the other loops.
- **External side effects:** meeting join records establish a durable deduplication identity before contacting Attendee. An ambiguous provider response is treated as unknown instead of blindly creating another bot.
- **Visible degradation:** provider failures surface as connection or processing states. A failed speech turn can leave the meeting assistant listening for the next turn.

## Shared speech behavior

- Browser clinic and conversation agents apply the same sentence-level text normalizer before TTS. Meeting speech uses the same normalization rules before synthesis.
- `FILLER_DELAY_MS` in `app/constants/constants.py` defaults to 700 ms. Browser pipelines trigger one context-free filler when an LLM response has not produced text by that threshold; clinic business calls use the same threshold while their work continues.
- Meeting replies apply the threshold across memory lookup, scope checking, answer generation, and final speech synthesis. All pipelines use that same default; zero disables filler.
- Filler is excluded from factual conversation history. Interruptions and teardown cancel pending filler work. The threshold starts synthesis; actual audible onset depends on TTS and transport latency.

## Engineering rationale

- Keep policy close to the data and validate it at every business boundary.
- Let language models interpret conversation while explicit workflows control consequential actions.
- Keep transport concerns separate from domain behavior so browser calls and meeting audio can evolve independently.
- Treat confirmation, identity, timezones, duplicate requests, interruptions, and provider uncertainty as part of the main workflow.
- Present the frontend with decisions it can render directly, keeping access logic and business rules consistent across clients.

## Code organization

```text
app/
├── constants/constants.py   Fixed defaults, policy text, catalogs and mappings
├── types.py                 Validated data contracts for every feature
├── env.py                   Credentials and deployment configuration
├── utils/                   Filler, speech normalization, security and CORS
├── business/                Clinic, calendar, workspace and meeting rules
├── ai/                      Domain voice pipelines and conversation flows
├── clients/                 Provider and transport clients
└── core/                    Operator command-line entry points
```

Change model names, speech defaults, filler timing, session limits, and calendar sync duration in the constants file. Environment configuration holds values that differ between deployments: credentials, database location, OAuth account identity, operator IDs, public callback origin, CORS origins, and ICE servers. Domain workflows retain their own orchestration; shared helpers are imported from `utils`.

## Sia Outreach: outbound AI sales campaigns

Organizations can create reusable sales agents with their own context, prepare campaigns
from phone numbers or CSV, and explicitly start calls through Exotel. Execution supports
one number at a time or batches of five concurrent calls, with independent campaigns
running simultaneously. See [Outreach setup, API workflow and recovery](docs/outreach.md).

## Clinic inbound and outbound phone calls

Clinic agents support the existing browser WebRTC interface and Exotel phone
calls using the same conversation flow and appointment tools. Inbound ExoPhone
routing, authorized outbound calls, deduplication, status callbacks and recovery
are described in [Clinic phone setup and APIs](docs/clinic-phone.md).
