"""Per-session Pipecat services: never share stateful processors between calls."""

from app.constants import constants


def create_sarvam_services(settings, language_code, *, system_prompt=None, text_filters=()):
    from pipecat.services.sarvam.llm import SarvamLLMService
    from pipecat.services.sarvam.stt import SarvamSTTService
    from pipecat.services.sarvam.tts import SarvamTTSService
    from pipecat.services.tts_service import TextAggregationMode
    from pipecat.transcriptions.language import Language

    key = settings.sarvam_api_key.get_secret_value()
    language = Language.OR_IN if language_code == "od-IN" else Language(language_code)
    llm_settings = {"model": constants.SARVAM_LLM_MODEL}
    if system_prompt is not None:
        llm_settings["system_instruction"] = system_prompt
    stt = SarvamSTTService(
        api_key=key,
        settings=SarvamSTTService.Settings(model=constants.SARVAM_STT_MODEL, language=language),
    )
    llm = SarvamLLMService(
        api_key=key,
        # The /v2 endpoint requires beta access for sarvam-105b.
        base_url="https://api.sarvam.ai/v1",
        settings=SarvamLLMService.Settings(**llm_settings),
    )
    tts = SarvamTTSService(
        api_key=key,
        text_aggregation_mode=TextAggregationMode.SENTENCE,
        text_filters=list(text_filters),
        settings=SarvamTTSService.Settings(
            model=constants.SARVAM_TTS_MODEL,
            voice=constants.SARVAM_VOICE,
            language=language,
        ),
    )
    return stt, llm, tts
