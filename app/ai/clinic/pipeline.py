"""Clinic conversation flow and its session state."""

from pipecat.flows import FlowManager

from app.ai.clinic.flow import nodes
from app.ai.clinic.flow.turns import CallerTurns
from app.ai.common.text_normalizer import SpeechNormalizer
from app.ai.common.webrtc import build_webrtc
from app.db.session import SessionFactory


def build_pipeline(connection, bot, settings):
    turns = CallerTurns()
    runner, worker, transport, llm, aggregators = build_webrtc(
        connection,
        bot,
        settings,
        text_filters=[SpeechNormalizer(bot.language)],
        input_processors=[turns],
    )
    flow = FlowManager(
        llm=llm,
        context_aggregator=aggregators,
        worker=worker,
        transport=transport,
        global_functions=nodes.global_functions(),
    )
    facts = getattr(bot, "clinic_facts", None) or {
        "company_context": getattr(bot, "company_context", ""),
        "doctors": [],
    }
    flow.state.update(bot=bot, facts=facts, session_factory=SessionFactory, caller_turn=0)
    turns.state = flow.state
    worker.app_resources["clinic_flow"] = flow
    started = False

    @transport.event_handler("on_client_connected")
    async def start_clinic_flow(_transport, _client):
        nonlocal started
        if started:
            return
        started = True
        await flow.initialize(nodes.greet_identify(flow.state))

    return runner, worker
