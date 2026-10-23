# Sia Outreach

Reusable AI sales agents and outbound phone campaigns, scoped to an organization.
Exotel connects the phone call to Sarvam speech recognition, language generation and
speech synthesis through Pipecat.

## Setup

```sh
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Set these deployment values in `.env` (see `.env.example`):

- `SARVAM_API_KEY`
- `EXOTEL_ACCOUNT_SID`, `EXOTEL_API_KEY`, `EXOTEL_API_TOKEN`
- `EXOTEL_CALLER_ID`: the account's ExoPhone / virtual number
- `EXOTEL_API_HOST`: `api.in.exotel.com` (India) or `api.exotel.com` (Singapore)
- `OUTREACH_PUBLIC_BACKEND_URL`: public HTTPS backend base URL, with WebSocket upgrades enabled

The Exotel account needs Connect Voice AI / AgentStream access and outbound calling
capacity for the selected concurrency. The backend uses the
[Connect Voice AI API](https://developer.exotel.com/docs/agentstream/connect-voice-ai)
and [AgentStream WebSocket protocol](https://developer.exotel.com/docs/agentstream/websocket-protocol).
No Exotel flow ID is required for this direct phone-to-bot integration. Without the
required configuration, drafts can be created but Run returns HTTP 503.

Use one ASGI worker as the existing WebRTC implementation requires. PostgreSQL row
locks also prevent competing Outreach workers from reserving the same campaign batch.
The Exotel client spaces requests to at most approximately 150 per minute per process,
including status reads. Share account capacity with other Exotel integrations accordingly.

## Workflow

All dashboard paths below start with `/api/v1/organizations/{org_id}/outreach` and
require the existing bearer authentication. Organization owners/admins can create,
edit, run, stop and reconcile; members can view. Interactive contracts are in `/docs`.

1. `POST /agents` to create a reusable agent:

   ```json
   {
     "name": "Growth Advisor",
     "language": "en-IN",
     "sales_context": "We are ExampleCo. Sell our Standard subscription at INR 500/month. It includes email support and five users. The goal is to arrange a demo. No discounts are available."
   }
   ```

   List with `GET /agents`, inspect with `GET /agents/{agent_id}`, update with
   `PUT /agents/{agent_id}`. Multiple agents are supported in every organization.
   The platform-owned prompt lives in `app/ai/outreach.py`; tenant sales context and
   recipient facts are supplied as a separate reference message. Context controls
   the offer, benefits, pricing, FAQs and desired next step. Platform rules cover AI
   disclosure, supported claims, refusal, sensitive data and ending the conversation.
   The model has an `end_call` tool to speak a goodbye and end its voice session.

2. `POST /campaigns` to prepare a draft:

   ```json
   {
     "name": "October demos",
     "agent_id": "<agent UUID>",
     "mode": "parallel",
     "contacts": [
       "+919876543210",
       {"phone_number": "+919876543211", "name": "Asha", "context": "Interested in team subscriptions"}
     ]
   }
   ```

   A single number uses a one-element contacts list. Both plain numbers and contact
   objects are accepted. Numbers must include country codes (E.164); spaces, dots,
   parentheses and hyphens are normalized. Duplicates after normalization reject the
   entire campaign. Each campaign supports up to 10,000 contacts.

   Alternatively, send `multipart/form-data` to `POST /campaigns/csv`, with fields
   `name`, `agent_id`, optional `mode`, and the CSV in `file`. Download a sample using
   `GET /contacts-template.csv`.

   ```csv
   phone_number,name,context
   +919876543210,Asha,"Interested in plans, wants a demo"
   +919876543211,Ravi,Asked about pricing
   ```

   `phone_number` is required; `name` and `context` are optional columns. No other
   columns are accepted. Use UTF-8 (a BOM is accepted), up to 2 MB. Missing fields,
   malformed rows and invalid numbers reject the whole import before any dialing.

3. Review `GET /campaigns/{campaign_id}` and `GET /campaigns/{campaign_id}/calls`.
   Creation makes no calls. The campaign stores and returns a snapshot of the agent
   configuration at creation; subsequent agent edits affect newly created campaigns.

4. `POST /campaigns/{campaign_id}/run` explicitly starts dialing. Running the same
   campaign again returns HTTP 409, including after completion. Create a new draft
   for another run.

   - `sequential`: one call at a time in CSV/list order, waiting for its terminal status.
   - `parallel`: batches of up to five active calls. The whole batch must end before
     the next batch starts. The final batch can contain fewer than five calls.
   - Separate campaigns, including campaigns using different agents in one
     organization, can run at the same time. The five-call limit is per campaign.
   - Calls have a 30-minute limit. Recording is disabled.

5. Read paginated progress with `GET /campaigns` (`offset`, `limit` up to 100) and
   `GET /campaigns/{campaign_id}/calls` (`offset`, `limit` up to 500).
   Campaign states: `draft`, `running`, `stopping`, `completed`, `cancelled`.
   Call states: `pending`, `dialing`, `unknown`, `queued`, `ringing`, `in-progress`,
   `completed`, `failed`, `busy`, `no-answer`, `cancelled`.
   A completed campaign means its queue finished; inspect individual calls for outcomes.

6. `POST /campaigns/{campaign_id}/stop` cancels pending contacts. Already reserved or
   active calls finish, then the campaign becomes `cancelled`. Stop is idempotent.

## Call status and recovery

Exotel receives per-call, purpose-specific HMAC-authenticated media and callback URLs.
Callbacks accept JSON, URL-encoded and multipart forms; call SID binding and monotonic
state transitions reject mismatches and tolerate duplicate/out-of-order delivery.
Media streams verify the account, call SID and audio format and allow one voice session
per call. Keep the deployment signing key stable, and redact callback URL query strings
in proxy/access logs because the tokens authorize those endpoints.

The queue and reservations are persisted before dialing. Terminal callbacks advance
the queue, with Call Details polling as a fallback. A lost media connection alone does
not release a call slot. Restarting the backend preserves queued campaigns; an already
connected voice session cannot resume and needs provider termination before progressing.

A timeout, server error or interrupted dispatch can leave the result uncertain. Such a
call becomes `unknown`, reserves its batch slot, and is never automatically redialed.
A late authenticated callback or media start can resolve it. If necessary, an org
owner/admin can inspect the Exotel dashboard and use:

`POST /campaigns/{campaign_id}/calls/{call_id}/reconcile`

```json
{"provider_call_sid": "<verified Exotel call SID>"}
```

The backend fetches the call and checks that its destination matches the contact before
attaching the SID. If Exotel definitely did not place the call, explicitly submit:

```json
{"confirmed_not_placed": true}
```

This marks the attempt failed and allows the next contacts to proceed. It does not
redial the contact. Failed, busy and unanswered calls are also not retried automatically.

## Verification

```sh
uv sync --dev
uv run python -m pytest tests/test_outreach.py -q
```

The default suite uses temporary SQLite databases and a simulated provider. To exercise
real PostgreSQL row locking, set `OUTREACH_TEST_POSTGRES_URL` to a disposable
`postgresql+asyncpg://...` database. Tests create and remove isolated random schemas.
The suite never calls Exotel or Sarvam. It covers API access, input validation, draft/run
behavior, batching, simultaneous campaigns, callback authentication, stream binding,
restart recovery, reconciliation, migration rollback and voice pipeline construction.
Live phone/audio verification requires configured provider accounts and a public deployment.
