import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import (
    EndFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from app.env import Settings
from app.utils.filler import ResponseFiller, with_filler


async def test_fast_work_never_emits_filler():
    speak = AsyncMock()
    assert await with_filler(asyncio.sleep(0, result="answer"), 700, speak) == "answer"
    speak.assert_not_awaited()


async def test_slow_work_continues_during_single_filler():
    release = asyncio.Event()

    async def work():
        await release.wait()
        return "answer"

    speak = AsyncMock(side_effect=release.set)
    assert await with_filler(work(), 1, speak) == "answer"
    speak.assert_awaited_once()


async def test_interruption_cancels_owned_work():
    started, stopped = asyncio.Event(), asyncio.Event()

    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    speak = AsyncMock()
    reply = asyncio.create_task(with_filler(work(), 700, speak))
    await started.wait()
    reply.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reply
    assert stopped.is_set()
    speak.assert_not_awaited()


async def test_browser_slow_response_filler_excluded_from_history():
    processor = ResponseFiller("hi-IN", 1)
    emitted = asyncio.Event()
    frames = []

    async def push(frame, *args):
        frames.append(frame)
        if isinstance(frame, TTSSpeakFrame):
            emitted.set()

    processor.push_frame = push
    try:
        await processor.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(emitted.wait(), 1)
        await processor.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
        fillers = [f for f in frames if isinstance(f, TTSSpeakFrame)]
        assert len(fillers) == 1
        assert fillers[0].append_to_context is False
        assert "एक पल" in fillers[0].text
    finally:
        await processor.cancel_pending()


@pytest.mark.parametrize("frame", [LLMTextFrame("Answer"), LLMFullResponseEndFrame(), EndFrame()])
async def test_browser_ready_or_ended_response_cancels_filler(frame):
    processor = ResponseFiller("en-IN", 700)
    processor.push_frame = AsyncMock()
    await processor.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await processor.process_frame(frame, FrameDirection.DOWNSTREAM)
    assert processor.pending is None
    assert not any(
        isinstance(call.args[0], TTSSpeakFrame) for call in processor.push_frame.await_args_list
    )


def test_shared_threshold_defaults_to_seven_hundred_ms():
    from app.constants import constants

    assert constants.FILLER_DELAY_MS == 700


async def test_browser_domains_both_receive_normalizer_and_filler(monkeypatch):
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

    from app.ai.common import webrtc
    from app.ai.pipeline import build_pipeline
    from app.utils.text_normalizer import SpeechNormalizer

    original = webrtc.create_sarvam_services
    filters = []

    def capture(*args, **kwargs):
        filters.extend(kwargs["text_filters"])
        return original(*args, **kwargs)

    monkeypatch.setattr(webrtc, "create_sarvam_services", capture)
    settings = Settings(_env_file=None, sarvam_api_key="test-key")
    for domain in ("clinic", "conversation"):
        filters.clear()
        connection = SmallWebRTCConnection()
        try:
            _, worker = build_pipeline(
                connection,
                SimpleNamespace(language="en-IN", service_type=domain, system_prompt="Brief"),
                settings,
            )
            assert sum(isinstance(f, SpeechNormalizer) for f in filters) == 1
            assert worker is not None
        finally:
            await connection.disconnect()


async def test_zero_disables_filler():
    speak = AsyncMock()
    assert await with_filler(asyncio.sleep(0.001, result="answer"), 0, speak) == "answer"
    speak.assert_not_awaited()
    processor = ResponseFiller("en-IN", 0)
    processor.push_frame = AsyncMock()
    await processor.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    assert processor.pending is None


@pytest.mark.parametrize("slow_stage", ["memory", "scope", "answer", "tts"])
async def test_meeting_timer_covers_entire_reply_and_keeps_filler_out_of_history(
    slow_stage, monkeypatch
):
    from app.constants import constants

    monkeypatch.setattr(constants, "FILLER_DELAY_MS", 1)
    from app.ai.meeting.pipeline import MeetingVoice

    ready = asyncio.Event()

    async def stage(name, value):
        if slow_stage == name:
            await ready.wait()
        return value

    speech = SimpleNamespace(
        is_in_scope=AsyncMock(),
        answer=AsyncMock(),
        synthesize=AsyncMock(),
        filler_audio=AsyncMock(return_value=b"filler"),
    )

    async def scope(*a):
        return await stage("scope", True)

    async def answer(*a):
        return await stage("answer", "Confirmed")

    async def synthesize(*a):
        return await stage("tts", b"answer")

    speech.is_in_scope.side_effect = scope
    speech.answer.side_effect = answer
    speech.synthesize.side_effect = synthesize
    save = AsyncMock(return_value={"role": "bot", "text": "Confirmed"})
    voice = MeetingVoice(
        None,
        None,
        SimpleNamespace(),
        speech,
        save,
        [],
        Settings(_env_file=None),
        get_memory=lambda: stage("memory", "context"),
    )
    played = []

    async def send(audio, generation):
        played.append(audio)
        if audio == b"filler":
            ready.set()

    voice.send_audio = send
    await asyncio.wait_for(voice.reply("What was decided?", 0), 1)
    assert voice.error is None
    assert played == [b"filler", b"answer"]
    save.assert_awaited_once_with("Sia", "Confirmed", "bot", False)
    assert voice.history == [{"role": "bot", "text": "Confirmed"}]
