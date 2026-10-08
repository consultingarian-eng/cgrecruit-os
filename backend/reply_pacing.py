"""How long the AI should appear to think before a reply lands.

A screening answer that arrives one second after the candidate hits send reads
as automation — the candidate concludes nobody considered what they wrote,
which is exactly the feeling the text-first screening is trying to avoid. So
replies are paced like a person typing them: a couple of seconds for a short
acknowledgement, ten-to-fifteen for a long paragraph, with a little credit for
how much the candidate wrote (their message takes time to read).

Pure maths, shared intent with the web chat (Retry.jsx mirrors the same
formula client-side). This module governs HOW LONG a reply waits, never
whether it sends — opt-out confirmations and compliance messages must not
pass through here.
"""

# Seconds. The floor keeps even "Perfect!" from arriving instantly; the cap
# keeps a long booking recap from feeling like the agent wandered off.
_FLOOR = 2.0
_CAP = 15.0
_PER_REPLY_CHAR = 0.03   # ~typing speed: 400 chars ≈ 12s + base
_PER_INBOUND_CHAR = 0.02  # reading time for what the candidate wrote
_INBOUND_CREDIT_CAP = 3.0


def considered_delay_seconds(reply_text: str, inbound_text: str = "") -> float:
    """Target total delay between the candidate's message and our reply."""
    reply_len = len((reply_text or "").strip())
    inbound_len = len((inbound_text or "").strip())
    delay = _FLOOR + _PER_REPLY_CHAR * reply_len + min(
        _INBOUND_CREDIT_CAP, _PER_INBOUND_CHAR * inbound_len
    )
    return max(_FLOOR, min(_CAP, delay))


def remaining_delay_seconds(reply_text: str, inbound_text: str, elapsed_seconds: float) -> float:
    """How much longer to wait, given work already done.

    The LLM call itself takes seconds — that time already *was* the thinking,
    so it counts toward the target rather than stacking on top of it.
    """
    remaining = considered_delay_seconds(reply_text, inbound_text) - max(0.0, elapsed_seconds)
    return max(0.0, remaining)
