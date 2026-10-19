"""Meeting scope policy and speech-only filler lines."""

from app.constants.constants import SCOPE_REFUSALS


def scope_refusal(language):
    return SCOPE_REFUSALS.get(language, SCOPE_REFUSALS["en-IN"])


