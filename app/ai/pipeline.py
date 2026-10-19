"""Dispatch WebRTC sessions to independent domain pipelines."""


def build_pipeline(connection, bot, settings):
    if getattr(bot, "service_type", "conversation") == "clinic":
        from app.ai.clinic.pipeline import build_pipeline as build
    else:
        from app.ai.conversation.pipeline import build_pipeline as build
    return build(connection, bot, settings)
