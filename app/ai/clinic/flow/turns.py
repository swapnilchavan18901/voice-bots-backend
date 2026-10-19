"""Count actual final caller transcripts so mutations cannot follow their own question."""

import re

from pipecat.frames.frames import TranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor


class CallerTurns(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.state = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if self.state is not None and isinstance(frame, TranscriptionFrame) and frame.text.strip():
            self.state["caller_turn"] = self.state.get("caller_turn", 0) + 1
            self.state["last_caller_text"] = frame.text
        await self.push_frame(frame, direction)


def confirmed(state):
    if state.get("caller_turn", 0) <= state.get("confirmation_turn", 0):
        return False
    text = state.get("last_caller_text", "").casefold()
    if re.search(r"\b(no|not|wait|change|but|don't|do not)\b|नहीं|नको|नाही|না|இல்லை|లేదు", text):
        return False
    return bool(
        re.search(
            r"\b(yes|yeah|yep|sure|okay|ok|confirm|confirmed|correct|go ahead|book it|cancel it)\b"
            r"|हाँ|हां|हो|હા|ಹೌದು|അതെ|ହଁ|ਹਾਂ|হ্যাঁ|ஆம்|అవును",
            text,
        )
    )
