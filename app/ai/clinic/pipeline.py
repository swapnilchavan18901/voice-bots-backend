"""Clinic conversation flow and its session state."""

from pipecat.flows import FlowManager

from app.ai.clinic.flow import nodes
from app.ai.clinic.flow.turns import CallerTurns
from app.ai.common.webrtc import build_voice, build_webrtc
from app.db.session import SessionFactory


def build_pipeline(
    connection,
    bot,
    settings,
    *,
    transport=None,
    output_sample_rate=24000,
    session_factory=SessionFactory,
    direction="inbound",
):
    turns = CallerTurns()
    if transport is None:
        components = build_webrtc(connection, bot, settings, input_processors=[turns])
    else:
        components = build_voice(
            transport,
            bot,
            settings,
            input_processors=[turns],
            output_sample_rate=output_sample_rate,
        )
    runner, worker, transport, llm, aggregators = components
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
    flow.state.update(
        bot=bot, facts=facts, session_factory=session_factory, caller_turn=0
    )
    turns.state = flow.state
    worker.app_resources["clinic_flow"] = flow
    started = False

    @transport.event_handler("on_client_connected")
    async def start_clinic_flow(_transport, _client):
        nonlocal started
        if started:
            return
        started = True
        node = nodes.greet_identify(flow.state)
        if direction == "outbound":
            node["task_messages"][0]["content"] = (
                "You are making an outbound clinic phone call. Introduce yourself as an AI "
                "receptionist and identify the clinic using configured facts. Ask if this is "
                "a convenient time and wait for their reply before collecting identity. "
                "If they decline or ask to stop, use end_call. Otherwise: "
                + node["task_messages"][0]["content"]
            )
        await flow.initialize(node)

    return runner, worker
