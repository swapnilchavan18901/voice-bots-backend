"""One rolling string per workspace, with deterministic fourteen-day retention."""

from datetime import date, timedelta

from sqlalchemy import func, select

from app.constants.constants import HEADER
from app.db.models import MeetingCapture, MeetingMemory, Organization, utcnow
from app.db.session import SessionFactory


def prune(context, today=None):
    today = today or utcnow().date()
    cutoff = today - timedelta(days=13)
    sections = []
    keep = False
    for line in context.splitlines():
        if HEADER.fullmatch(line.strip()):
            day = date.fromisoformat(line.strip())
            keep = cutoff <= day <= today
        if keep:
            sections.append(line)
    return "\n".join(sections).strip()


def validate(context, today):
    if not isinstance(context, str) or len(context) > 60000:
        raise ValueError("Invalid memory length")
    if context.strip() and not HEADER.fullmatch(context.strip().splitlines()[0]):
        raise ValueError("Memory must start with a dated heading")
    return prune(context, today)


class MeetingMemoryService:
    def __init__(self, speech):
        self.speech = speech

    async def row(self, db, org_id):
        # Lock a row that always exists, including the first memory update.
        await db.execute(select(Organization.id).where(Organization.id == org_id).with_for_update())
        memory = await db.get(MeetingMemory, org_id, populate_existing=True)
        if memory is None:
            memory = MeetingMemory(organization_id=org_id, context="", updated_at=utcnow())
            db.add(memory)
        return memory

    async def read(self, org_id):
        async with SessionFactory() as db:
            memory = await db.get(MeetingMemory, org_id)
            return prune(memory.context) if memory else ""

    async def merge(self, context, chunk, today):
        updated = validate(await self.speech.memory(context, chunk, today), today)
        if context and not updated:
            raise ValueError("Memory unexpectedly discarded all retained history")
        return updated

    async def update(self, capture_id):
        async with SessionFactory() as db:
            capture = await db.get(MeetingCapture, capture_id)
            if not capture or not capture.transcript:
                return
            memory = await self.row(db, capture.organization_id)
            # Refresh after acquiring the workspace lock: another update may have finished.
            await db.refresh(capture)
            if not capture.transcript:
                return
            pending = list(capture.transcript)
            today = utcnow().date()
            context = prune(memory.context, today)
            # Bound each request without dropping the rest of a long meeting.
            chunk, size = [], 0
            for entry in pending:
                if (
                    entry.get("role") != "user"
                    or not (today - timedelta(days=13)).isoformat()
                    <= entry.get("timestamp", "")[:10]
                    <= today.isoformat()
                ):
                    continue
                entry = {
                    **entry,
                    "speaker": capture.speaker_names.get(
                        entry.get("speaker_id", entry["speaker"]), entry["speaker"]
                    ),
                }
                if chunk and size + len(entry["text"]) > 12000:
                    context = await self.merge(context, chunk, today)
                    chunk, size = [], 0
                chunk.append(entry)
                size += len(entry["text"])
            if chunk:
                context = await self.merge(context, chunk, today)
            # Atomic replacement and removal of ONLY the processed transcript prefix.
            await db.refresh(capture, with_for_update=True)
            if capture.transcript[: len(pending)] != pending:
                raise ValueError("Transcript changed during memory update")
            memory.context, memory.updated_at = context, utcnow()
            capture.transcript = capture.transcript[len(pending) :]
            capture.memory_status = "pending" if capture.transcript else "ready"
            capture.memory_error = None
            await db.commit()

    async def cleanup(self):
        async with SessionFactory() as db:
            ids = list(await db.scalars(select(MeetingMemory.organization_id)))
        for org_id in ids:
            async with SessionFactory() as db:
                memory = await self.row(db, org_id)
                context = prune(memory.context)
                if context != memory.context:
                    memory.context, memory.updated_at = context, utcnow()
                await db.commit()
        # Failed sessions must not retain raw text indefinitely.
        async with SessionFactory() as db:
            captures = (
                await db.scalars(
                    select(MeetingCapture)
                    .where(
                        func.json_array_length(MeetingCapture.transcript) > 0,
                    )
                    .with_for_update()
                )
            ).all()
            cutoff = (utcnow().date() - timedelta(days=13)).isoformat()
            for capture in captures:
                kept = [e for e in capture.transcript if e.get("timestamp", "")[:10] >= cutoff]
                if len(kept) != len(capture.transcript):
                    capture.transcript = kept
                    if not kept:
                        capture.memory_status = "expired"
                        capture.memory_error = None
            await db.commit()
