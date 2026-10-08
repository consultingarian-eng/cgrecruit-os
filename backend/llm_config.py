"""Which Claude models the app calls, from one place.

    ANTHROPIC_MODEL       every Claude call (CV parsing, call summaries,
                          insights, the text/email/web-chat replies).
                          Default: claude-sonnet-5-5
    ANTHROPIC_FAST_MODEL  only the candidate-facing chat paths (SMS replies and
                          the web retry chat) that already fall back to a
                          second, faster model when the first is rate-limited
                          or slow. Default: claude-haiku-4-5

The key is ANTHROPIC_API_KEY (EMERGENT_LLM_KEY is still read as a fallback).
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterable

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_FAST_MODEL = "claude-haiku-4-5"


def primary_model() -> str:
    return (os.getenv("ANTHROPIC_MODEL") or "").strip() or DEFAULT_MODEL


def fast_model() -> str:
    return (os.getenv("ANTHROPIC_FAST_MODEL") or "").strip() or DEFAULT_FAST_MODEL


def chat_models() -> tuple:
    """(primary, fallback) for the latency-sensitive candidate chat paths,
    without a duplicate when both settings name the same model."""
    p, f = primary_model(), fast_model()
    return (p,) if p == f else (p, f)


def api_key() -> str:
    return (os.getenv("ANTHROPIC_API_KEY") or os.getenv("EMERGENT_LLM_KEY") or "").strip()


def response_text(resp: Any) -> str:
    """All text from a Messages API response, SDK object or raw JSON dict.

    Current models can return a thinking block before the text, so reading
    content[0] alone can come back empty (or fail) — always join the text
    blocks."""
    if resp is None:
        return ""
    content: Iterable[Any] = (
        resp.get("content") if isinstance(resp, dict) else getattr(resp, "content", None)
    ) or []
    parts = []
    for block in content:
        btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if btype == "text":
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", "")
            if text:
                parts.append(text)
    return "".join(parts)


def low_latency_params(model: str) -> Dict[str, Any]:
    """Extra request fields for a short, latency-sensitive reply: low effort on
    models that accept the effort setting (Sonnet/Opus 4.6 and newer). Sent via
    extra_body so any SDK version passes it through; Haiku and older models get
    nothing, since they reject the field."""
    m = (model or "").lower()
    supports_effort = (
        m.startswith(("claude-sonnet-5", "claude-opus-5", "claude-fable-"))
        or m.startswith(("claude-sonnet-4-6", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8"))
    )
    return {"extra_body": {"output_config": {"effort": "low"}}} if supports_effort else {}
