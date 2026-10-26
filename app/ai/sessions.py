"""Process-local WebRTC sessions. Run one ASGI worker (or use sticky routing)."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from app.constants import constants

logger = logging.getLogger(__name__)


def _prepare_remote_sdp(description):
    # Browser mDNS hostnames are only resolvable on its local network. When
    # public/TURN candidates exist, avoid involving the cloud server's mDNS
    # transport in negotiation and teardown. Preserve local-only offers.
    for media in description.media:
        if any(candidate.type in {"srflx", "relay"} for candidate in media.ice_candidates):
            media.ice_candidates = [
                candidate
                for candidate in media.ice_candidates
                if not (
                    candidate.type == "host"
                    and candidate.ip.lower().rstrip(".").endswith(".local")
                )
            ]
    return str(description)


@dataclass
class Session:
    user_id: UUID
    organization_id: UUID
    bot_id: UUID
    connection: Any
    runner: Any
    task: asyncio.Task | None = None


class VoiceSessions:
    def __init__(self, settings, turn=None):
        self.settings = settings
        self.turn = turn
        self.sessions: dict[str, Session] = {}
        self.pending = 0
        self.closing = False

    async def offer(self, bot, user_id, offer):
        if not self.settings.sarvam_api_key.get_secret_value():
            raise HTTPException(503, "Voice is unavailable: SARVAM_API_KEY is not configured")
        if self.closing or len(self.sessions) + self.pending >= constants.MAX_VOICE_SESSIONS:
            raise HTTPException(503, "Voice session capacity reached")
        # Reserve before the first await, so simultaneous offers cannot exceed capacity.
        self.pending += 1
        connection = None
        try:
            from aiortc.sdp import SessionDescription
            from pipecat.transports.smallwebrtc.connection import (
                IceServer,
                SmallWebRTCConnection,
            )

            from app.ai.pipeline import build_pipeline

            try:
                description = SessionDescription.parse(offer.sdp)
                kinds = {media.kind for media in description.media}
                if not offer.sdp.startswith("v=0") or not {"audio", "application"} <= kinds:
                    raise ValueError("Audio and a control data channel are required")
            except (ValueError, AssertionError, IndexError) as exc:
                raise HTTPException(400, "Offer must contain audio and a data channel") from exc
            config = (
                await self.turn.configuration()
                if self.turn
                else {"ice_servers": [{"urls": constants.STUN_URL}]}
            )
            connection = SmallWebRTCConnection(
                ice_servers=[IceServer(**server) for server in config["ice_servers"]],
                connection_timeout_secs=30,
            )
            try:
                async with asyncio.timeout(30):
                    await connection.initialize(
                        sdp=_prepare_remote_sdp(description), type=offer.type
                    )
            except (ValueError, AssertionError) as exc:
                raise HTTPException(400, "Invalid WebRTC offer") from exc
            except TimeoutError as exc:
                raise HTTPException(504, "WebRTC negotiation timed out") from exc
            answer = connection.get_answer()
            if answer is None:
                raise HTTPException(502, "Could not negotiate WebRTC")
            if self.closing:
                raise HTTPException(503, "Server is shutting down")
            runner, worker = build_pipeline(connection, bot, self.settings)
            await runner.add_workers(worker)
            session = Session(user_id, bot.organization_id, bot.id, connection, runner)
            self.sessions[connection.pc_id] = session
            session.task = asyncio.create_task(self._run(session))
            return answer
        except BaseException:
            if connection is not None:
                await connection.disconnect()
            raise
        finally:
            self.pending -= 1

    async def _run(self, session):
        try:
            async with asyncio.timeout(constants.VOICE_SESSION_SECONDS):
                await session.runner.run()
        except TimeoutError:
            logger.info("Voice session reached its time limit")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Voice pipeline failed")
        finally:
            try:
                await session.connection.disconnect()
            finally:
                self.sessions.pop(session.connection.pc_id, None)

    async def _stop(self, session):
        await session.runner.cancel()
        if session.task:
            try:
                await asyncio.wait_for(asyncio.shield(session.task), timeout=10)
            except TimeoutError:
                session.task.cancel()
                await asyncio.gather(session.task, return_exceptions=True)

    async def stop(self, pc_id, user_id, org_id, bot_id):
        session = self.sessions.get(pc_id)
        if not session or (session.user_id, session.organization_id, session.bot_id) != (
            user_id,
            org_id,
            bot_id,
        ):
            raise HTTPException(404, "Voice session not found")
        await self._stop(session)

    async def close(self):
        self.closing = True
        await asyncio.gather(
            *(self._stop(s) for s in list(self.sessions.values())), return_exceptions=True
        )
