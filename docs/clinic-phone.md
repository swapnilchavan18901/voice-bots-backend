# Clinic voice: browser, inbound phone, and outbound phone

The same clinic Pipecat Flow and appointment tools serve all three transports.
WebRTC endpoints remain available. Phone sessions use Exotel AgentStream PCM audio
and retain caller identification, verified availability, explicit booking/reschedule/
cancellation confirmation, emergency guidance, and staff-request logging.
Staff requests still do not place a live transfer or deliver a callback.

## Configuration

1. Run `uv sync`, then `uv run alembic upgrade head`.
2. Configure the existing `SARVAM_API_KEY` and `EXOTEL_ACCOUNT_SID`,
   `EXOTEL_API_KEY`, `EXOTEL_API_TOKEN`, and regional `EXOTEL_API_HOST`.
3. Set `CLINIC_PUBLIC_BACKEND_URL` to the backend's public HTTPS origin. Its proxy
   must forward WebSocket upgrades. An HTTPS tunnel works for local development.
4. Set `EXOTEL_CALLER_ID` to your ExoPhone for outbound calls. Inbound numbers are
   allocated separately per clinic. The same provider account/client and request
   throttle are shared with sales campaigns.
5. Add trusted platform operator dashboard UUIDs to `CLINIC_OPERATOR_USER_IDS`.
   These operators must also be owners/admins in a destination workspace to
   allocate its inbound number. Verify ownership of the number in Exotel before
   allocation: this setting is trusted provisioning, not a claim-your-own-number API.
6. Run one ASGI worker, as for existing browser voice sessions. Phone calls have
   a 30-minute runtime limit and at most 20 pending/active clinic phone sessions
   per process; browser limits are tracked separately.

The Exotel account must have AgentStream / Voicebot and outbound calling enabled.
Configuring backend variables alone does not connect a telephone number.

## Inbound: a patient calls the clinic

An authorized operator calls:

`PUT /api/v1/organizations/{org_id}/bots/{bot_id}/phone/route`

```json
{"phone_number":"+918012345678","enabled":true}
```

The response contains `inbound_stream_url`, a signed WSS URL for this clinic.
In Exotel App Bazaar, create a flow containing a **bidirectional Voicebot** applet,
paste that URL as the stream URL, and assign the clinic ExoPhone to that flow.
Use raw PCM, mono, signed 16-bit audio, preferably 16 kHz (8 and 24 kHz also work).
Follow the Voicebot applet with Hangup so the phone call ends when the AI closes
its media connection. A normal personal SIM number needs forwarding to the
ExoPhone before calls to it can reach the AI.

A patient calling that number now follows:

`Phone → Exotel Voicebot → clinic flow → availability / confirmation / booking → spoken result`

The backend verifies the signed route, Exotel account, provider call details,
called number, caller metadata, and PCM format. Each provider call SID may create
only one clinic voice session. The AI asks the patient for their name and phone;
telephony caller ID alone does not authorize access to appointment records.
A booking is saved only after the patient confirms it on a subsequent turn.

Owners/admins can retrieve the setup URL using `GET .../phone/route`. Treat the URL
as a secret. Every successful PUT rotates it (including disabling a route), so
update Exotel after changing configuration. Disabling a route blocks new inbound
sessions; already connected calls can finish. One number can belong to one clinic.

## Outbound: the clinic AI calls a patient

A workspace owner/admin calls:

`POST /api/v1/organizations/{org_id}/bots/{bot_id}/phone/calls`

```json
{
  "phone_number":"+919876543210",
  "request_id":"f6839467-7c77-4834-a963-0288b268f442"
}
```

This action places a real call immediately. The agent identifies itself as an AI
clinic receptionist, asks whether it is a convenient time, and then uses the same
clinic flow. It can end the call when the recipient declines. Recordings are
disabled, as in the existing Exotel adapter. API phone numbers require E.164;
provider India metadata in national format is normalized internally.

Keep `request_id` stable when retrying an HTTP request: a persisted unique
bot/request pair prevents duplicate dialing. Reusing it with another destination
returns 409. Use a new UUID only when intentionally requesting a new call.

`GET .../phone/calls?offset=0&limit=50` lists inbound and outbound attempts.
Terminal callbacks and provider polling update statuses; a media disconnect alone
does not imply the telephone call has finished. Provider callbacks are scoped to
one call and purpose; terminal states cannot be reopened by late callbacks.

Ambiguous dispatches become `unknown` and are never automatically redialed.
After verifying the attempt in Exotel, owners/admins can call:

`POST .../phone/calls/{call_id}/reconcile`

with either `{"provider_call_sid":"verified-provider-sid"}` or
`{"confirmed_not_placed":true}`. Reconciliation never places another call.
A provider SID is checked against the destination before binding it.

## Deployment and verification boundaries

The durable call log survives restarts. An interrupted dispatch is marked unknown;
connected audio sessions cannot resume after a backend restart. Provider polling
still updates known call statuses. Do not retry an uncertain dial with a new UUID.
Redact callback URL query strings at your proxy; application Uvicorn logs redact
the scoped tokens. Keep `JWT_SECRET` stable because it signs these URLs.

Run `uv run pytest tests/test_clinic_phone.py tests/test_outreach.py -q` for tests
with temporary databases and simulated providers. Tests do not place real calls.
Real audio/call delivery requires configured provider accounts, a public backend,
and the Exotel inbound flow; these are separate from local test verification.

Provider references:
- https://developer.exotel.com/docs/agentstream/what-to-use-when
- https://developer.exotel.com/docs/agentstream/websocket-protocol
- https://developer.exotel.com/docs/agentstream/connect-voice-ai
