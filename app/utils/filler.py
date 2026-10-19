"""Shared, context-free latency feedback for voice pipelines."""

import asyncio

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.constants import constants
from app.constants.constants import FILLERS


def filler_text(language):
    return FILLERS.get(language, FILLERS["en-IN"])


async def with_filler(operation, delay_ms, speak):
    """Keep work running during one filler; cancel owned work on interruption."""
    task = asyncio.create_task(operation)
    try:
        if delay_ms == 0:
            return await task
        done, _ = await asyncio.wait({task}, timeout=delay_ms / 1000)
        if not done:
            await speak()
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class ResponseFiller(FrameProcessor):
    """One filler while waiting for the first LLM text, never added to context."""

    def __init__(self, language, delay_ms=None):
        super().__init__()
        self.language = language
        self.delay_ms = constants.FILLER_DELAY_MS if delay_ms is None else delay_ms
        self.pending = None

    async def cancel_pending(self):
        if self.pending:
            self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)
            self.pending = None

    async def wait_and_speak(self):
        await asyncio.sleep(self.delay_ms / 1000)
        await self.push_frame(
            TTSSpeakFrame(text=filler_text(self.language), append_to_context=False)
        )

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, (InterruptionFrame, CancelFrame, EndFrame)):
            await self.cancel_pending()
        elif direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                await self.cancel_pending()
                if self.delay_ms > 0:
                    self.pending = asyncio.create_task(self.wait_and_speak())
            elif isinstance(frame, LLMFullResponseEndFrame) or (
                isinstance(frame, LLMTextFrame) and frame.text.strip()
            ):
                await self.cancel_pending()
        await self.push_frame(frame, direction)

    async def cleanup(self):
        await self.cancel_pending()
        await super().cleanup()
