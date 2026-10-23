"""Persistent batch dispatch with row locks and conservative ambiguous-request handling."""

import asyncio
import hashlib
import hmac
import logging
from datetime import timedelta

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from app.business.outreach import ACTIVE, TERMINAL
from app.clients.exotel import ExotelClient
from app.db.models import OutreachCall, OutreachCampaign, utcnow
from app.db.session import SessionFactory

logger = logging.getLogger(__name__)


class OutreachService:
    def __init__(self, settings, *, sessions=SessionFactory, provider=None):
        self.settings = settings
        self.sessions = sessions
        self.provider = provider or ExotelClient(settings)
        self.tasks = []

    def require_ready(self):
        if not all(
            [
                self.settings.exotel_account_sid,
                self.settings.exotel_api_key.get_secret_value(),
                self.settings.exotel_api_token.get_secret_value(),
                self.settings.exotel_caller_id,
                self.settings.outreach_public_backend_url,
                self.settings.sarvam_api_key.get_secret_value(),
            ]
        ):
            raise HTTPException(
                503,
                "Configure Exotel, Outreach public HTTPS URL and Sarvam before running",
            )

    def token(self, call_id, purpose):
        return hmac.new(
            self.settings.jwt_secret.get_secret_value().encode(),
            f"outreach:{purpose}:{call_id}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def verify(self, call_id, purpose, token):
        if not token.isascii() or not hmac.compare_digest(
            self.token(call_id, purpose), token
        ):
            raise HTTPException(403, "Invalid call token")

    def urls(self, call_id):
        base = self.settings.outreach_public_backend_url.rstrip("/")
        path = f"/api/v1/outreach/exotel/{call_id}"
        return (
            base.replace("https://", "wss://", 1)
            + path
            + f"/media?token={self.token(call_id, 'media')}&sample-rate=16000",
            base + path + f"/status?token={self.token(call_id, 'status')}",
        )

    async def apply_status(self, call_id, sid, status):
        if not isinstance(sid, str) or not sid or len(sid) > 128:
            raise HTTPException(400, "Invalid Exotel call SID")
        if status not in TERMINAL | {"queued", "ringing", "in-progress"}:
            raise HTTPException(400, "Unsupported Exotel call status")
        async with self.sessions() as db:
            call = await db.scalar(
                select(OutreachCall).where(OutreachCall.id == call_id).with_for_update()
            )
            if not call or call.status == "pending" or call.started_at is None:
                raise HTTPException(404, "Dispatched call not found")
            if call.provider_call_sid and call.provider_call_sid != sid:
                raise HTTPException(409, "Call SID does not match")
            if call.status in TERMINAL:
                return  # Duplicate / out-of-order callbacks never reopen a finished call.
            call.provider_call_sid = sid
            rank = {"queued": 0, "ringing": 1, "in-progress": 2}
            if status in TERMINAL or rank.get(status, -1) >= rank.get(call.status, -1):
                call.status = status
                # Provider confirmation resolves an ambiguous dispatch.
                call.error = None
            if status in TERMINAL:
                call.finished_at = utcnow()
            await db.commit()

    async def claim_batch(self, campaign_id):
        async with self.sessions() as db:
            campaign = await db.scalar(
                select(OutreachCampaign)
                .where(OutreachCampaign.id == campaign_id)
                .with_for_update(skip_locked=True)
            )
            if not campaign or campaign.status not in {"running", "stopping"}:
                return []
            active = await db.scalar(
                select(OutreachCall.id)
                .where(
                    OutreachCall.campaign_id == campaign.id,
                    OutreachCall.status.in_(ACTIVE),
                )
                .limit(1)
            )
            if active:
                return []  # A batch must fully finish before another batch starts.
            pending = list(
                await db.scalars(
                    select(OutreachCall)
                    .where(
                        OutreachCall.campaign_id == campaign.id,
                        OutreachCall.status == "pending",
                    )
                    .order_by(OutreachCall.position)
                    .limit(5 if campaign.mode == "parallel" else 1)
                )
            )
            if not pending or campaign.status == "stopping":
                campaign.status = (
                    "cancelled" if campaign.status == "stopping" else "completed"
                )
                campaign.finished_at = utcnow()
                await db.commit()
                return []
            for call in pending:
                call.status = "dialing"
                call.started_at = utcnow()
            await db.commit()  # Persist reservations BEFORE making external requests.
            return [call.id for call in pending]

    async def dispatch(self, call_id):
        async with self.sessions() as db:
            call = await db.get(OutreachCall, call_id)
            number = call.phone_number
        try:
            stream, callback = self.urls(call_id)
            result = await self.provider.dial(number, stream, callback, call_id)
            await self.apply_status(
                call_id, result["Sid"], result.get("Status", "queued")
            )
        except Exception as exc:  # noqa: BLE001 - preserve ambiguous outcomes on any dispatch failure
            # A timeout/5xx/malformed response can still have placed a real phone call.
            # Keep its slot reserved, accept a late callback and NEVER auto-redial it.
            rejected = (
                isinstance(exc, httpx.HTTPStatusError)
                and 400 <= exc.response.status_code < 500
            )
            async with self.sessions() as db:
                call = await db.scalar(
                    select(OutreachCall)
                    .where(OutreachCall.id == call_id)
                    .with_for_update()
                )
                if call.status == "dialing" and not call.provider_call_sid:
                    call.status = "failed" if rejected else "unknown"
                    call.error = (
                        "Exotel rejected the call request"
                        if rejected
                        else "Dispatch outcome unknown; awaiting callback or provider reconciliation"
                    )
                    if rejected:
                        call.finished_at = utcnow()
                    await db.commit()

    async def refresh_statuses(self):
        async with self.sessions() as db:
            active = list(
                await db.scalars(
                    select(OutreachCall).where(OutreachCall.status.in_(ACTIVE))
                )
            )
        for call in active:
            if call.provider_call_sid:
                try:
                    result = await self.provider.details(call.provider_call_sid)
                    await self.apply_status(call.id, result["Sid"], result["Status"])
                except (
                    httpx.HTTPError,
                    ValueError,
                    KeyError,
                    TypeError,
                    HTTPException,
                ):
                    logger.warning(
                        "Outreach call status refresh failed for %s", call.id
                    )
            elif call.status == "dialing":
                # Recover a process that died between reservation and storing the response.
                async with self.sessions() as db:
                    stuck = await db.scalar(
                        select(OutreachCall)
                        .where(
                            OutreachCall.id == call.id,
                            OutreachCall.status == "dialing",
                            OutreachCall.started_at < utcnow() - timedelta(seconds=60),
                        )
                        .with_for_update()
                    )
                    if stuck:
                        stuck.status = "unknown"
                        stuck.error = "Interrupted dispatch; reconcile with Exotel before continuing"
                        await db.commit()

    async def dispatch_pending(self):
        async with self.sessions() as db:
            ids = list(
                await db.scalars(
                    select(OutreachCampaign.id)
                    .where(OutreachCampaign.status.in_({"running", "stopping"}))
                    .order_by(OutreachCampaign.created_at)
                )
            )
        reserved = []
        for campaign_id in ids:
            reserved.extend(await self.claim_batch(campaign_id))
            if len(reserved) >= 10:
                break  # Bound provider dial rate; remaining campaigns get a later tick.
        await asyncio.gather(*(self.dispatch(call_id) for call_id in reserved))

    async def tick(self):
        await self.refresh_statuses()
        await self.dispatch_pending()

    def start(self):
        async def loop(action):
            while True:
                try:
                    self.require_ready()
                    await action()
                except HTTPException:
                    pass
                except Exception:  # noqa: BLE001 - keep the durable queue alive after a failed tick
                    logger.warning(
                        "Outreach worker failed; persisted queue will be retried"
                    )
                await asyncio.sleep(5)

        self.tasks = [
            asyncio.create_task(loop(action))
            for action in (self.refresh_statuses, self.dispatch_pending)
        ]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.provider.close()
