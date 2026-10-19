"""Durable bot control; one ASGI process owns live audio sessions."""

import asyncio
import logging
import re
from datetime import timedelta
from urllib.parse import urlparse
from uuid import UUID

import jwt
from fastapi import HTTPException, WebSocketDisconnect
from sqlalchemy import select

from app.ai.meeting.pipeline import MeetingVoice
from app.ai.meeting.speech import MeetingSpeech
from app.business.meeting_memory import MeetingMemoryService
from app.clients import AttendeeClient
from app.db.models import Meeting, MeetingCapture, utcnow
from app.db.session import SessionFactory

STATES = {
    "ready": ("starting", "The bot is preparing to join."),
    "joining": ("joining", "The bot is joining the meeting."),
    "waiting_room": ("waiting", "Admit the bot from the host account."),
    "joined_not_recording": ("connected", "Waiting for recording permission."),
    "joined_recording": ("connected", "Listening. Address Sia by name to ask a question."),
    "leaving": ("stopping", "The bot is leaving."),
    "post_processing": ("stopped", "The bot left; meeting media is processing."),
    "ended": ("stopped", "The meeting session ended."),
    "fatal_error": ("failed", "Attendee could not join or remain in the meeting."),
    "data_deleted": ("stopped", "Meeting media was deleted from Attendee."),
}
TERMINAL = {"ended", "fatal_error", "data_deleted"}


class AudioGrantLogFilter(logging.Filter):
    """WebSocket callback URLs carry scoped grants; redact them from server logs."""

    def filter(self, record):
        def clean(value):
            if isinstance(value, str) and "/meeting-audio/" in value:
                return re.sub(r"([?&]token=)[^\s\"']+", r"\1[redacted]", value)
            return value

        record.msg = clean(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(clean(value) for value in record.args)
        return True


class MeetingVoiceService:
    def __init__(self, settings):
        for name in ("uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            if not any(isinstance(item, AudioGrantLogFilter) for item in logger.filters):
                logger.addFilter(AudioGrantLogFilter())
        self.settings = settings
        self.attendee = AttendeeClient(settings)
        self.speech = MeetingSpeech(settings)
        self.memory = MeetingMemoryService(self.speech)
        self.tasks = []
        self.live: dict[UUID, MeetingVoice] = {}
        self.closing = False

    @property
    def ready(self):
        url = urlparse(self.settings.meeting_public_backend_url)
        return bool(
            self.settings.attendee_api_key.get_secret_value()
            and self.settings.sarvam_api_key.get_secret_value()
            and url.scheme == "https"
            and url.hostname
            and not url.query
            and not url.fragment
            and not url.username
            and not url.password
        )

    def output(self, capture, meeting_id):
        runtime = self.live.get(capture.id) if capture else None
        return {
            "meeting_id": meeting_id,
            "status": capture.status if capture else "idle",
            "detail": capture.detail if capture else "The bot has not joined this meeting.",
            "updated_at": capture.updated_at if capture else None,
            "voice_state": runtime.phase if runtime else "disconnected",
            "voice_error": runtime.error if runtime else None,
            "memory_status": capture.memory_status if capture else "pending",
            "memory_error": capture.memory_error if capture else None,
            "can_join": not runtime
            and (
                not capture
                or capture.provider_status in TERMINAL
                or (capture.status == "failed" and not capture.provider_bot_id)
            ),
        }

    async def capture(self, db, meeting_id, lock=False):
        statement = select(MeetingCapture).where(MeetingCapture.meeting_id == meeting_id)
        if lock:
            statement = statement.with_for_update()
        return await db.scalar(statement)

    async def start(self, db, meeting, scheduled=False):
        if not self.ready:
            raise HTTPException(
                503,
                "Configure ATTENDEE_API_KEY, SARVAM_API_KEY "
                "and MEETING_PUBLIC_BACKEND_URL (HTTPS).",
            )
        if self.closing:
            raise HTTPException(503, "The meeting voice service is shutting down.")
        await db.execute(select(Meeting.id).where(Meeting.id == meeting.id).with_for_update())
        await db.refresh(meeting)
        capture = await self.capture(db, meeting.id)
        if meeting.status == "cancelled":
            raise HTTPException(409, "Cancelled meetings cannot be joined")
        if scheduled and (
            capture
            or not meeting.auto_join
            or meeting.status != "scheduled"
            or meeting.starts_at > utcnow()
            or meeting.ends_at <= utcnow()
        ):
            return self.output(capture, meeting.id)
        if capture and capture.status == "failed" and not capture.provider_bot_id:
            await db.delete(capture)
            await db.flush()
            capture = None
        if capture and capture.provider_status in TERMINAL and capture.id not in self.live:
            capture.provider_bot_id = None
            capture.provider_status = None
            capture.status = "starting"
            capture.detail = "Reconnecting the meeting voice agent."
        elif capture:
            # One bot per assigned meeting. Never blindly retry a potentially accepted POST.
            return self.output(capture, meeting.id)
        if capture is None:
            capture = MeetingCapture(meeting_id=meeting.id, organization_id=meeting.organization_id)
            db.add(capture)
        await db.flush()
        await db.commit()  # Durable deduplication identity before external side effects.
        token = jwt.encode(
            {
                "sub": str(capture.id),
                "aud": "attendee-audio",
                "exp": utcnow() + timedelta(seconds=self.settings.meeting_voice_seconds),
            },
            self.settings.jwt_secret.get_secret_value(),
            algorithm="HS256",
        )
        public_url = self.settings.meeting_public_backend_url.rstrip("/")
        audio_url = public_url.replace("https://", "wss://", 1)
        try:
            bot = await self.attendee.request(
                "POST",
                "bots",
                json={
                    "meeting_url": meeting.meet_url,
                    "bot_name": self.settings.meeting_voice_name,
                    "deduplication_key": str(capture.id),
                    "automatic_leave_settings": {
                        "max_uptime_seconds": self.settings.meeting_voice_seconds,
                        "only_participant_in_meeting_timeout_seconds": (
                            self.settings.meeting_empty_leave_seconds
                        ),
                    },
                    "websocket_settings": {
                        "per_participant_audio": {
                            "url": f"{audio_url}/api/v1/meeting-audio/{capture.id}?token={token}",
                            "sample_rate": 16000,
                        }
                    },
                },
            )
            capture.provider_bot_id = bot["id"]
            self.apply_state(capture, meeting, bot.get("state", "joining"))
        except HTTPException as error:
            capture.status = "unknown" if "did not respond" in error.detail else "failed"
            capture.detail = error.detail
            await db.commit()
            raise
        except Exception:
            capture.status = "unknown"
            capture.detail = (
                "Bot creation is unconfirmed. Check Attendee before retrying; "
                "no duplicate will be sent."
            )
            await db.commit()
            raise HTTPException(502, capture.detail) from None
        await db.commit()
        return self.output(capture, meeting.id)

    def apply_state(self, capture, meeting, provider_state):
        capture.provider_status = provider_state
        capture.status, capture.detail = STATES.get(
            provider_state, ("joining", "Checking bot status.")
        )
        capture.detail = capture.detail.replace("Sia", self.settings.meeting_voice_name)
        capture.updated_at = capture.last_synced_at = utcnow()
        if capture.status == "connected":
            meeting.status = "in_progress"
        elif provider_state == "ended":
            meeting.status = "completed"
        elif provider_state == "fatal_error":
            meeting.status = "failed"

    async def status(self, db, meeting):
        capture = await self.capture(db, meeting.id)
        if capture and capture.provider_bot_id and capture.provider_status not in TERMINAL:
            synced = capture.last_synced_at
            if synced and synced.tzinfo is None:
                synced = synced.replace(tzinfo=utcnow().tzinfo)
            if not synced or utcnow() - synced > timedelta(seconds=15):
                bot = await self.attendee.request("GET", f"bots/{capture.provider_bot_id}")
                self.apply_state(capture, meeting, bot.get("state", "joining"))
                try:
                    events = await self.attendee.request(
                        "GET", f"bots/{capture.provider_bot_id}/participant_events"
                    )
                    if isinstance(events, dict):
                        events = events.get("results", [])
                    names = dict(capture.speaker_names)
                    for event in events:
                        if event.get("participant_uuid") and event.get("participant_name"):
                            names[event["participant_uuid"]] = event["participant_name"]
                    capture.speaker_names = names
                except HTTPException:
                    pass  # Names are optional; stream identities still separate speakers.
                await db.commit()
        if capture and capture.provider_status in TERMINAL:
            runtime = self.live.get(capture.id)
            if runtime:
                await runtime.websocket.close(code=1000)
        return self.output(capture, meeting.id)

    async def stop(self, db, meeting):
        capture = await self.capture(db, meeting.id, lock=True)
        if capture and capture.provider_bot_id and capture.provider_status not in TERMINAL:
            await self.attendee.request("POST", f"bots/{capture.provider_bot_id}/leave")
            capture.status, capture.detail = "stopping", "The bot is leaving the meeting."
            capture.updated_at = utcnow()
            await db.commit()
            runtime = self.live.get(capture.id)
            if runtime:
                runtime.interrupt()
                await runtime.websocket.close(code=1000)
        return self.output(capture, meeting.id)

    async def save_turn(self, capture_id, speaker, text, role, addressed):
        entry = {
            "speaker": speaker,
            "text": text,
            "role": role,
            "addressed": addressed,
            "timestamp": utcnow().isoformat(),
        }
        async with SessionFactory() as db:
            capture = await db.scalar(
                select(MeetingCapture).where(MeetingCapture.id == capture_id).with_for_update()
            )
            entry["speaker_id"] = speaker
            entry["speaker"] = capture.speaker_names.get(speaker, speaker)
            capture.transcript = [*capture.transcript, entry]
            capture.memory_status = "pending"
            capture.memory_error = None
            capture.updated_at = utcnow()
            await db.commit()
        return entry

    async def audio(self, websocket, capture_id, token):
        try:
            claim = jwt.decode(
                token,
                self.settings.jwt_secret.get_secret_value(),
                algorithms=["HS256"],
                audience="attendee-audio",
            )
            if claim["sub"] != str(capture_id) or not self.ready:
                raise ValueError("Invalid audio grant")
        except (jwt.PyJWTError, KeyError, ValueError):
            await websocket.close(code=1008)
            return
        async with SessionFactory() as db:
            capture = await db.get(MeetingCapture, capture_id)
            if (
                not capture
                or not capture.provider_bot_id
                or capture.provider_status in TERMINAL
                or capture.status == "stopping"
            ):
                await websocket.close(code=1008)
                return
            meeting = await db.get(Meeting, capture.meeting_id)
            if meeting.status == "cancelled":
                await websocket.close(code=1008)
                return
        if capture_id in self.live or self.closing:
            await websocket.close(code=1013)
            return

        async def save(speaker, text, role, addressed):
            return await self.save_turn(capture_id, speaker, text, role, addressed)

        runtime = MeetingVoice(
            websocket,
            capture,
            meeting,
            self.speech,
            save,
            list(capture.transcript[-60:]),
            self.settings,
            get_memory=lambda: self.memory.read(meeting.organization_id),
        )
        self.live[capture_id] = runtime  # Reserve before accepting the socket.
        consumer = None
        flusher = None
        try:
            await websocket.accept()
            consumer = asyncio.create_task(runtime.consume())
            flusher = asyncio.create_task(runtime.flush_idle())
            async with asyncio.timeout(self.settings.meeting_voice_seconds):
                while True:
                    raw = await websocket.receive_text()
                    if len(raw) > 100_000:
                        raise ValueError("Oversized audio message")
                    import json

                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ValueError("Expected an audio object")
                    await runtime.receive_audio(message)
        except WebSocketDisconnect:
            pass
        except (ValueError, TimeoutError, asyncio.QueueFull):
            await websocket.close(code=1008)
        finally:
            runtime.interrupt()
            if flusher:
                flusher.cancel()
                await asyncio.gather(flusher, return_exceptions=True)
            # Finish already-received human speech before the memory worker can run.
            if consumer and not consumer.done():
                try:
                    async with asyncio.timeout(30):
                        for participant, speaker in runtime.speakers.items():
                            if speaker.audio or speaker.parts:
                                await runtime.queue.put(
                                    (participant, bytes(speaker.audio), True, -1)
                                )
                                speaker.audio.clear()
                        await runtime.queue.join()
                except TimeoutError:
                    pass
            for task in (consumer, flusher):
                if task:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (consumer, flusher) if task), return_exceptions=True
            )
            await runtime.close()
            self.live.pop(capture_id, None)

    async def close(self):
        self.closing = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for runtime in list(self.live.values()):
            runtime.interrupt()
            await runtime.websocket.close(code=1001)
        await self.speech.close()
        await self.attendee.close()
