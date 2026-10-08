"""TwiML Bridge — places outbound Twilio calls FROM a verified caller ID and pipes
the audio through to ElevenLabs Conversational AI in real time.

Flow:
  1. /api/candidates/{id}/call (twiml_bridge mode)
  2. Backend creates a Conversation row, then calls Twilio REST API:
        Twilio.calls.create(from=verified_caller_id, to=candidate.phone,
                            url=<APP_PUBLIC_URL>/api/twiml/voice/{conv_id})
  3. Candidate picks up. Twilio fetches our TwiML.
  4. We respond:
        <Response>
          <Connect>
            <Stream url="wss://APP_PUBLIC_URL/api/twiml/stream/{conv_id}/{signed_token}"/>
          </Connect>
        </Response>
  5. Twilio opens a WebSocket to /api/twiml/stream/{conv_id}/{signed_token}
     (token minted by the signature-checked /twiml/voice, valid 5 minutes).
  6. We open a parallel WebSocket to ElevenLabs Conv AI.
  7. We forward audio in both directions (μ-law 8 kHz base64, no resampling).
"""
from __future__ import annotations

import os
import json
import asyncio
import logging
import base64
from typing import Any, Dict, Optional

import requests
import websockets

logger = logging.getLogger(__name__)

ELEVENLABS_API = "https://api.elevenlabs.io"


def get_signed_websocket_url(agent_id: str) -> Optional[str]:
    """For private/auth-required agents, fetch a signed wss URL from ElevenLabs."""
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key or not agent_id:
        return None
    try:
        r = requests.get(
            f"{ELEVENLABS_API}/v1/convai/conversation/get-signed-url",
            headers={"xi-api-key": api_key},
            params={"agent_id": agent_id},
            timeout=15,
        )
        if r.status_code >= 400:
            logger.warning(f"get-signed-url failed: {r.status_code} {r.text[:200]}")
            return None
        return r.json().get("signed_url")
    except Exception as e:
        logger.exception(f"get-signed-url error: {e}")
        return None


async def open_elevenlabs_ws(
    agent_id: str,
    dynamic_variables: Dict[str, Any],
    first_message: Optional[str] = None,
):
    """Open a WebSocket to ElevenLabs Conversational AI and send the init payload.
    Configures conversation for μ-law 8 kHz to match Twilio Media Streams (no resampling)."""
    signed = get_signed_websocket_url(agent_id)
    url = signed or f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={agent_id}"
    ws = await websockets.connect(url, max_size=10 * 1024 * 1024, ping_interval=20)
    init: Dict[str, Any] = {
        "type": "conversation_initiation_client_data",
        "dynamic_variables": dynamic_variables or {},
        # Configure both directions for μ-law 8kHz so we can pass-through Twilio audio.
        # NOTE: this only works if your ElevenLabs agent has overrides enabled for these
        # fields (Agent → Security → Overrides → user_input_audio_format / tts_output_format).
        # If the override is disabled, ElevenLabs ignores these and uses the agent default,
        # which means we'd need to bake the format into the agent permanently at sync time.
        "conversation_config_override": {
            "agent": {
                "user_input_audio_format": "ulaw_8000",
            },
            "tts": {
                "output_format": "ulaw_8000",
            },
        },
    }
    if first_message:
        init["conversation_config_override"]["agent"]["first_message"] = first_message
    await ws.send(json.dumps(init))
    logger.info(f"[bridge] ElevenLabs WS opened agent={agent_id} dyn_keys={list((dynamic_variables or {}).keys())}")
    return ws


async def bridge_streams(
    twilio_ws,
    eleven_ws,
    on_transcript_turn: Optional[callable] = None,
    on_metadata: Optional[callable] = None,
):
    """Forward audio between a Twilio Media-Streams socket and an ElevenLabs Conv AI socket.
    Stops when either side closes. `on_transcript_turn(role, text)` is called as we receive
    user_transcript / agent_response events so the candidate's transcript stays live.
    `on_metadata(conversation_id)` fires once when ElevenLabs sends the init metadata frame
    so we can persist the conversation_id for later audio playback."""
    stream_sid: Dict[str, Optional[str]] = {"value": None}
    transcript_buf = []
    stopping = asyncio.Event()
    # Buffer ElevenLabs audio frames that arrive BEFORE Twilio sends `start`.
    # Without this, Aria's "Hi this is Aria…" first message gets dropped because
    # we have no streamSid to address Twilio's media frames at, which can also
    # confuse the candidate's phone (silent ring → call drops at ~2 seconds).
    pre_start_audio_queue: list = []
    twilio_started = asyncio.Event()

    async def keepalive_pinger():
        """Application-level heartbeat — fires a Twilio `mark` event every 5
        seconds. This is a SECONDARY safety net; the primary keepalive is
        uvicorn's WebSocket-protocol PING (every 15s, set via
        `--ws-ping-interval` in supervisord.conf). The K8s ingress's 60s
        proxy_read_timeout is reset by EITHER mechanism — we belt-and-braces
        both because Twilio reports error 31921 ("WebSocket Close") if
        EITHER side stops sending frames for 60s.

        Marks are echoed back by Twilio (giving us inbound traffic too) and
        don't interfere with the audio playback (unlike silent-media frames
        which Twilio still mixes into the output audio)."""
        try:
            n = 0
            logger.info("[bridge] keepalive task started — waiting for Twilio start")
            try:
                await asyncio.wait_for(twilio_started.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("[bridge] keepalive: Twilio start never arrived in 10s")
                return
            logger.info(f"[bridge] keepalive: Twilio start detected, beginning 5s mark heartbeat (streamSid={stream_sid['value']})")
            while not stopping.is_set():
                if stream_sid["value"]:
                    try:
                        n += 1
                        await twilio_ws.send_text(json.dumps({
                            "event": "mark",
                            "streamSid": stream_sid["value"],
                            "mark": {"name": f"keepalive-{n}"},
                        }))
                        if n <= 3 or n % 6 == 0:
                            logger.info(f"[bridge] keepalive #{n} mark sent")
                    except Exception as e:
                        logger.warning(f"[bridge] keepalive send failed at #{n}: {e}")
                        return
                else:
                    logger.warning(f"[bridge] keepalive: stream_sid is None — can't send mark (n={n})")
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=5.0)
                    return  # stopping set during sleep — exit
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            logger.info("[bridge] keepalive cancelled")
            return
        except Exception as e:
            logger.exception(f"[bridge] keepalive crashed: {e}")

    async def twilio_to_eleven():
        try:
            async for raw in twilio_ws.iter_text():
                if stopping.is_set():
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                event = msg.get("event")
                if event == "connected":
                    logger.info("[bridge] Twilio: connected")
                elif event == "start":
                    stream_sid["value"] = msg.get("start", {}).get("streamSid")
                    twilio_started.set()
                    logger.info(f"[bridge] Twilio: start streamSid={stream_sid['value']}")
                    # Drain any audio that ElevenLabs sent before Twilio was ready.
                    if pre_start_audio_queue:
                        logger.info(f"[bridge] flushing {len(pre_start_audio_queue)} pre-start audio frames")
                        for audio_b64 in pre_start_audio_queue:
                            try:
                                await twilio_ws.send_text(json.dumps({
                                    "event": "media",
                                    "streamSid": stream_sid["value"],
                                    "media": {"payload": audio_b64},
                                }))
                            except Exception as e:
                                logger.warning(f"[bridge] flush send failed: {e}")
                                break
                        pre_start_audio_queue.clear()
                elif event == "media":
                    payload = (msg.get("media") or {}).get("payload")
                    if payload:
                        await eleven_ws.send(json.dumps({
                            "user_audio_chunk": payload,
                        }))
                elif event == "stop":
                    logger.info("[bridge] Twilio: stop")
                    stopping.set()
                    break
                elif event == "mark":
                    pass  # we don't need marks
        except Exception as e:
            logger.warning(f"[bridge] twilio→eleven loop error: {e}")
        finally:
            stopping.set()

    async def eleven_to_twilio():
        try:
            async for raw in eleven_ws:
                if stopping.is_set():
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                t = msg.get("type")
                if t == "conversation_initiation_metadata":
                    meta = msg.get("conversation_initiation_metadata_event") or {}
                    conv_id = meta.get("conversation_id") or meta.get("conversationId")
                    if conv_id and on_metadata:
                        try: await on_metadata(conv_id)
                        except Exception as e: logger.warning(f"[bridge] on_metadata failed: {e}")
                    logger.info(f"[bridge] ElevenLabs init metadata, conversation_id={conv_id}")
                elif t == "audio":
                    audio_b64 = (msg.get("audio_event") or {}).get("audio_base_64")
                    if audio_b64:
                        if stream_sid["value"]:
                            await twilio_ws.send_text(json.dumps({
                                "event": "media",
                                "streamSid": stream_sid["value"],
                                "media": {"payload": audio_b64},
                            }))
                        else:
                            # Twilio's `start` hasn't landed yet — buffer instead
                            # of dropping. Keeps Aria's first message from being
                            # silently swallowed, which made the candidate hear
                            # 1-2s of dead air and hang up.
                            pre_start_audio_queue.append(audio_b64)
                elif t == "interruption":
                    if stream_sid["value"]:
                        await twilio_ws.send_text(json.dumps({
                            "event": "clear",
                            "streamSid": stream_sid["value"],
                        }))
                elif t == "user_transcript":
                    text = (msg.get("user_transcription_event") or {}).get("user_transcript")
                    if text:
                        transcript_buf.append({"role": "user", "text": text})
                        if on_transcript_turn:
                            try: await on_transcript_turn("user", text)
                            except Exception: pass
                elif t == "agent_response":
                    text = (msg.get("agent_response_event") or {}).get("agent_response")
                    if text:
                        transcript_buf.append({"role": "agent", "text": text})
                        if on_transcript_turn:
                            try: await on_transcript_turn("agent", text)
                            except Exception: pass
                elif t == "ping":
                    pong = msg.get("ping_event") or {}
                    await eleven_ws.send(json.dumps({
                        "type": "pong",
                        "event_id": pong.get("event_id"),
                    }))
        except Exception as e:
            logger.warning(f"[bridge] eleven→twilio loop error: {e}")
        finally:
            stopping.set()

    await asyncio.gather(twilio_to_eleven(), eleven_to_twilio(), keepalive_pinger(), return_exceptions=True)
    return transcript_buf


def place_outbound_call_via_twiml(
    from_number: str,
    to_number: str,
    conversation_id: str,
    public_base_url: str,
) -> Dict[str, Any]:
    """Place a Twilio outbound call FROM `from_number` (any verified caller ID or owned
    number on the Twilio account). The call is bridged to ElevenLabs via TwiML."""
    from voice_service import _twilio_client
    try:
        client = _twilio_client()
        twiml_url = f"{public_base_url.rstrip('/')}/api/twiml/voice/{conversation_id}"
        status_url = f"{public_base_url.rstrip('/')}/api/twiml/status/{conversation_id}"
        call = client.calls.create(
            from_=from_number,
            to=to_number,
            url=twiml_url,
            method="POST",
            status_callback=status_url,
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
        )
        return {"status": "initiated", "call_sid": call.sid, "conversation_id": conversation_id}
    except Exception as e:
        logger.exception(f"place_outbound_call_via_twiml failed: {e}")
        return {"status": "failed", "error": str(e)}
