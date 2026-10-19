"""Unit tests for the TTS text normalizer — 30 tricky strings.

Run:  uv run pytest -v
"""

import asyncio

import pytest

from app.ai.common.text_normalizer import SpeechNormalizer, normalize_for_speech

# (input, expected spoken text)
CASES = [
    # --- dates & times the booking flow produces ---
    ("Booked for 2026-10-12T10:00.", "Booked for Monday 12 October at 10 AM."),
    ("Moved to 2026-10-12T14:30", "Moved to Monday 12 October at 2:30 PM"),
    ("Your slot is 2026-10-12 09:00:00.", "Your slot is Monday 12 October at 9 AM."),
    ("Free on 2026-10-13.", "Free on Tuesday 13 October."),
    ("Report dated 12/10/2026.", "Report dated 12 October 2026."),
    ("Come at 13:30 please.", "Come at 1:30 PM please."),
    ("Doors open at 09:00.", "Doors open at 9 AM."),
    ("See you at 10:00 am.", "See you at 10 AM."),
    ("Is 10:30 p.m. okay?", "Is 10:30 PM okay?"),
    ("The lunch break starts at 12:00.", "The lunch break starts at 12 noon."),
    ("Open 9am-5pm", "Open 9 AM to 5 PM"),
    ("We're open Mon–Sat, 9 AM–5 PM.", "We're open Monday to Saturday, 9 AM to 5 PM."),
    ("Closed on Sun.", "Closed on Sunday."),
    # --- phone numbers & codes ---
    ("Your number is 9175541890.", "Your number is 9 1 7 5 5, 4 1 8 9 0."),
    ("Call +91 98765 43210 now.", "Call plus 9 1, 9 8 7 6 5, 4 3 2 1 0 now."),
    ("Front desk: 02194 123456.", "Front desk: 0 2 1 9 4, 1 2 3 4 5 6."),
    ("Pincode 402109.", "Pincode 4 0 2 1 0 9."),
    # --- money ---
    ("The fee is ₹500.", "The fee is 500 rupees."),
    ("Total Rs. 1,500/- only", "Total 1500 rupees only"),
    ("That's INR 300.", "That's 300 rupees."),
    ("Package costs ₹1,50,000.", "Package costs 150000 rupees."),
    # --- abbreviations & units ---
    ("Dr. Mehta will see you.", "Doctor Mehta will see you."),
    ("Each slot is 30 mins.", "Each slot is 30 minutes."),
    (
        "Wait approx. 2 hrs, e.g. after lunch.",
        "Wait approximately 2 hours, for example after lunch.",
    ),
    ("Your appt is No. 4.", "Your appointment is number 4."),
    # --- symbols, markdown, emoji ---
    ("**Booked!** ✅ See you soon 😊", "Booked! See you soon"),
    ("- Fees: ₹500\n- Parking: free", "Fees: 500 rupees\nParking: free"),
    ("Cash & UPI accepted, 100% refund.", "Cash and UPI accepted, 100 percent refund."),
    ("Email help@sunrise.clinic.in or visit.", "Email help at sunrise dot clinic dot in or visit."),
    ("Great!!! Morning/evening both work...", "Great! Morning or evening both work."),
]


@pytest.mark.parametrize("text, expected", CASES, ids=[c[0][:30] for c in CASES])
def test_english(text, expected):
    assert normalize_for_speech(text, "en-IN") == expected


def test_there_are_30_cases():
    assert len(CASES) == 30


# --- behaviour outside the 30 strings ---


def test_plain_text_unchanged():
    s = "Thank you Swapnil, would you like to book, reschedule or cancel?"
    assert normalize_for_speech(s) == s


def test_at_before_datetime_becomes_on():
    assert normalize_for_speech("with Dr. Anjali Mehta at 2026-10-12T11:00.") == (
        "with Doctor Anjali Mehta on Monday 12 October at 11 AM."
    )


def test_number_word_compounds_become_words():
    # Bulbul spells "10-digit" as "one zero D I G I T"
    assert normalize_for_speech("Please share your full 10-digit number.") == (
        "Please share your full ten digit number."
    )
    assert normalize_for_speech("It's a 30-minute slot.") == "It's a thirty minute slot."
    assert normalize_for_speech("Open 9am-5pm") == "Open 9 AM to 5 PM"  # ranges untouched


def test_small_numbers_not_spelled():
    assert normalize_for_speech("Room 204, slot 3 of 14.") == "Room 204, slot 3 of 14."


def test_hindi_only_safe_rules():
    # markdown, emoji and phone digits are cleaned; English words are NOT inserted
    out = normalize_for_speech("**बुक हो गया** ✅ नंबर 9175541890, शुल्क ₹500", "hi-IN")
    assert out == "बुक हो गया नंबर 9 1 7 5 5, 4 1 8 9 0, शुल्क ₹500"


def test_filter_class_is_async_and_uses_language():
    f = SpeechNormalizer("en-IN")
    assert asyncio.run(f.filter("Dr. Mehta at 13:30")) == "Doctor Mehta at 1:30 PM"
