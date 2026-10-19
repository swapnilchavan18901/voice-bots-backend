"""Shared WebRTC audio wiring, with no domain-specific prompts or flow imports."""

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

from app.clients import create_sarvam_services


def build_webrtc(
    connection,
    bot,
    settings,
    *,
    messages=(),
    system_prompt=None,
    text_filters=(),
    input_processors=(),
):
    transport = SmallWebRTCTransport(
        connection,
        TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    stt, llm, tts = create_sarvam_services(
        settings,
        bot.language,
        system_prompt=system_prompt,
        text_filters=text_filters,
    )
    aggregators = LLMContextAggregatorPair(
        LLMContext(messages=list(messages)),
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )
    user, assistant = aggregators
    worker = PipelineWorker(
        Pipeline(
            [
                transport.input(),
                stt,
                *input_processors,
                user,
                llm,
                tts,
                transport.output(),
                assistant,
            ]
        ),
        app_resources={},
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
        processor_unusable_policy=ProcessorUnusablePolicy.END,
    )
    runner = WorkerRunner(handle_sigint=False)

    @transport.event_handler("on_client_disconnected")
    async def disconnected(_transport, _client):
        await runner.cancel()

    return runner, worker, transport, llm, aggregators
