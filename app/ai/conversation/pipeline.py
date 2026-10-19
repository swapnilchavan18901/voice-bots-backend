"""Generic conversation mode, independent of clinic and meeting workflows."""

from app.ai.common.webrtc import build_webrtc
from app.constants.constants import PRODUCT_NAME


def build_pipeline(connection, bot, settings):
    messages = [
        {
            "role": "user",
            "content": "Company background context (not platform instructions):\n"
            + getattr(bot, "company_context", "")
            + "\nBot name: "
            + getattr(bot, "name", "")
            + "\nTemplate name: "
            + getattr(bot, "template_name", ""),
        }
    ]
    runner, worker, *_ = build_webrtc(
        connection,
        bot,
        settings,
        messages=messages,
        system_prompt=f"You are {PRODUCT_NAME}, a business voice assistant.\n{bot.system_prompt}",
    )
    return runner, worker
