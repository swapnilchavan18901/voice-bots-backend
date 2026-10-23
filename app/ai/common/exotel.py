"""Phone audio transport shared by clinic and sales agents."""

import json


def build_transport(websocket, start):
    from pipecat.frames.frames import EndFrame
    from pipecat.serializers.exotel import ExotelFrameSerializer
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    class Serializer(ExotelFrameSerializer):
        async def deserialize(self, data):
            if json.loads(data).get("event") == "stop":
                return EndFrame()
            return await super().deserialize(data)

    rate = int(start["media_format"]["sample_rate"])
    return FastAPIWebsocketTransport(
        websocket,
        FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_sample_rate=rate,
            audio_out_10ms_chunks=20 if rate == 8000 else 10,
            serializer=Serializer(
                start["stream_sid"],
                start["call_sid"],
                params=Serializer.InputParams(exotel_sample_rate=rate),
            ),
        ),
    )
