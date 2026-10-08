#!/usr/bin/env python3
"""Check that a text message uses only GSM-7 characters.

One character outside GSM-7 (an emoji, a curly quote, a long dash, "...") turns
the whole message into UCS-2: 70 characters per segment instead of 160, so
every text costs several times more. Paste or pipe the SMS text in:

    python3 .claude/skills/brand/gsm7_check.py < message.txt
    echo "Hi [First Name], thanks for applying" | python3 .claude/skills/brand/gsm7_check.py

Same character set as backend/tests/test_screening_shortened.py.
"""
import math
import sys

GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
    "^{}\\[~]|€"
)
EXTENDED = set("^{}\\[~]|€")  # count as two characters each

text = sys.stdin.read().rstrip("\n")
bad = sorted({c for c in text if c not in GSM7})
if bad:
    print("NOT GSM-7. Replace these characters: " + " ".join(repr(c) for c in bad))
    sys.exit(1)
length = len(text) + sum(1 for c in text if c in EXTENDED)
segments = 1 if length <= 160 else math.ceil(length / 153)
print(f"GSM-7 OK: about {length} characters before placeholders are filled in, {segments} segment(s).")
