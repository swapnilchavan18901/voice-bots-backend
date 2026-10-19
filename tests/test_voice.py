import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app.ai.sessions import VoiceSessions
from app.env import Settings
from app.schemas.api import Offer


def settings(**kwargs):
    return Settings(jwt_secret="test-secret-for-voice-at-least-32-characters", **kwargs)


@pytest.mark.parametrize("service_type", ["conversation", "clinic"])
async def test_pipeline_constructs_with_installed_pipecat(service_type):
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

    from app.ai.pipeline import build_pipeline

    connection = SmallWebRTCConnection()
    runner, worker = build_pipeline(
        connection,
        SimpleNamespace(
            id=uuid4(), language="hi-IN", system_prompt="Speak briefly.", service_type=service_type
        ),
        settings(sarvam_api_key=SecretStr("test-key")),
    )
    assert runner is not None and worker is not None
    await connection.disconnect()


async def test_real_sdp_negotiation_and_owned_cleanup(monkeypatch):
    from aiortc import RTCConfiguration, RTCPeerConnection

    import app.ai.pipeline

    ended = asyncio.Event()
    runner = SimpleNamespace(
        add_workers=AsyncMock(), run=ended.wait, cancel=AsyncMock(side_effect=ended.set)
    )
    monkeypatch.setattr(app.ai.pipeline, "build_pipeline", lambda *_: (runner, object()))
    manager = VoiceSessions(settings(sarvam_api_key=SecretStr("test-key")))
    bot = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    user = uuid4()
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    browser.addTransceiver("audio", direction="sendrecv")
    browser.createDataChannel("pipecat")
    try:
        await browser.setLocalDescription(await browser.createOffer())
        answer = await manager.offer(bot, user, Offer(sdp=browser.localDescription.sdp))
        assert answer["type"] == "answer"
        assert "m=audio" in answer["sdp"]
        assert manager.pending == 0
        await asyncio.sleep(0)
        with pytest.raises(HTTPException) as exc:
            await manager.stop(answer["pc_id"], uuid4(), bot.organization_id, bot.id)
        assert exc.value.status_code == 404
        assert answer["pc_id"] in manager.sessions
        await manager.stop(answer["pc_id"], user, bot.organization_id, bot.id)
        assert not manager.sessions
    finally:
        await manager.close()
        await browser.close()


async def test_voice_capacity_and_invalid_offer(monkeypatch):
    import app.ai.pipeline

    monkeypatch.setattr(
        app.ai.pipeline,
        "build_pipeline",
        lambda *_: (SimpleNamespace(add_workers=AsyncMock()), object()),
    )
    manager = VoiceSessions(settings(sarvam_api_key=SecretStr("test-key"), max_voice_sessions=1))
    bot = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    manager.pending = 1
    with pytest.raises(HTTPException) as exc:
        await manager.offer(bot, uuid4(), Offer(sdp="invalid"))
    assert exc.value.status_code == 503
    manager.pending = 0
    with pytest.raises(HTTPException) as exc:
        await manager.offer(bot, uuid4(), Offer(sdp="invalid"))
    assert exc.value.status_code == 400
    assert manager.pending == 0
    assert not manager.sessions


async def test_clinic_flow_exposes_only_current_step_actions(monkeypatch):
    from pipecat.frames.frames import LLMSetToolsFrame
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

    from app.ai.clinic.flow import nodes
    from app.ai.pipeline import build_pipeline

    connection = SmallWebRTCConnection()
    bot = SimpleNamespace(
        id=uuid4(),
        name="Asta",
        language="en-IN",
        service_type="clinic",
        system_prompt="Unused flat clinic prompt",
        company_context="",
        clinic_facts={
            "clinic_name": "Customer Clinic",
            "doctors": [],
            "company_context": "Customer facts",
        },
    )
    runner, worker = build_pipeline(connection, bot, settings(sarvam_api_key=SecretStr("test-key")))
    queued = AsyncMock()
    monkeypatch.setattr(worker, "queue_frame", queued)
    flow = worker.app_resources["clinic_flow"]
    try:
        await flow.initialize(nodes.greet_identify(flow.state))
        tools = next(
            call.args[0].tools
            for call in queued.call_args_list
            if isinstance(call.args[0], LLMSetToolsFrame)
        )
        names = {schema.name for schema in tools.standard_tools}
        assert "save_caller" in names
        assert "confirm_booking" not in names and "select_slot" not in names
        queued.reset_mock()
        flow.state["mode"] = "book"
        await flow.set_node_from_config(nodes.collect_slot(flow.state))
        tools = next(
            call.args[0].tools
            for call in queued.call_args_list
            if isinstance(call.args[0], LLMSetToolsFrame)
        )
        names = {schema.name for schema in tools.standard_tools}
        assert "select_slot" in names and "check_availability" in names
        assert "confirm_booking" not in names
    finally:
        await connection.disconnect()
