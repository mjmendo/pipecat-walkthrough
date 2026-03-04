"""
M3 — SmallWebRTC Transport Server

Same tech support bot as M1/M2, now with WebRTC (aiortc).
Uses the prebuilt browser UI from pipecat-ai-small-webrtc-prebuilt.

Signaling: HTTP POST /api/offer (SDP offer/answer)
Media: WebRTC OPUS audio track (bidirectional)
UI: SmallWebRTCPrebuiltUI mounted at /

Run:
    python bots/tech-support/webrtc_server.py

Then open http://localhost:7860 in a browser.

Learning:
    - Same pipeline as M1/M2. Transport is the only change.
    - SmallWebRTCConnection handles ICE/DTLS negotiation (aiortc)
    - OPUS codec: adaptive bitrate, built-in jitter buffer, FEC
    - /api/offer receives SDP offer → returns SDP answer
    - pcs_map keeps connections alive across renegotiations

    Key observation: open chrome://webrtc-internals during a call.
    You'll see ICE candidates, DTLS handshake, and OPUS track stats
    (bitrate ~32kbps, jitter, packets lost).

    Compare latency to M2:
    - M2 WebSocket: audio encoded as PCM → base64 or binary → decoded on server
    - M3 WebRTC: audio encoded as OPUS by the browser → decoded on server as PCM
    WebRTC has lower E2E latency because OPUS handles timing at the hardware level.
"""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import Dict

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import RedirectResponse
from loguru import logger
from pipecat_ai_small_webrtc_prebuilt.frontend import SmallWebRTCPrebuiltUI

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from bots.shared.observers import LoggingMetricsObserver

import importlib.util as _ilu

_persona_spec = _ilu.spec_from_file_location(
    "persona", os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona.py")
)
_persona = _ilu.module_from_spec(_persona_spec)
_persona_spec.loader.exec_module(_persona)
INITIAL_GREETING = _persona.INITIAL_GREETING
SYSTEM_PROMPT = _persona.SYSTEM_PROMPT

load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")

# Store active connections for renegotiation and cleanup
pcs_map: Dict[str, SmallWebRTCConnection] = {}

# Optional: add STUN/TURN for non-localhost use
# ICE_SERVERS = [IceServer(urls="stun:stun.l.google.com:19302")]
ICE_SERVERS = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Cleanup all connections on shutdown
    coros = [pc.disconnect() for pc in pcs_map.values()]
    await asyncio.gather(*coros)
    pcs_map.clear()


app = FastAPI(lifespan=lifespan)

# Mount prebuilt UI at /client — serves index.html + static assets
app.mount("/client", SmallWebRTCPrebuiltUI)


async def run_bot(webrtc_connection: SmallWebRTCConnection):
    """Start a bot pipeline for this WebRTC connection.

    Called in a background task — one call per browser connection.
    """
    logger.info("Starting tech support bot")

    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    )

    stt = OpenAISTTService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4o-transcribe",
    )

    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4o",
    )

    tts = OpenAITTSService(
        api_key=os.getenv("OPENAI_API_KEY"),
        voice="nova",
        model="gpt-4o-mini-tts",
    )

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    context = LLMContext(messages)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),      # WebRTC media track → InputAudioRawFrame (OPUS decoded to PCM)
            stt,                    # InputAudioRawFrame → TranscriptionFrame
            user_aggregator,        # → LLMMessagesFrame
            llm,                    # → LLMTextFrame
            tts,                    # → TTSAudioRawFrame
            transport.output(),     # TTSAudioRawFrame → WebRTC media track (PCM → OPUS)
            assistant_aggregator,
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=[LoggingMetricsObserver()],
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected via WebRTC — queuing greeting")
        messages.append({"role": "system", "content": f"Greet the user: {INITIAL_GREETING}"})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected — cancelling pipeline")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
    logger.info("Bot session ended")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/client/")


@app.post("/api/offer")
async def offer(request: dict, background_tasks: BackgroundTasks):
    """SDP signaling endpoint.

    Learning note (M3):
        1. Browser sends: { sdp: "...", type: "offer", pc_id: null }
        2. Server creates SmallWebRTCConnection, initializes with SDP offer
        3. Server returns: { sdp: "...", type: "answer", pc_id: "uuid" }
        4. Browser sets remote description with the answer → ICE exchange begins
        5. Once ICE/DTLS completes → media track active → pipeline starts

    On renegotiation (reconnect without page reload):
        Browser sends pc_id from previous answer → server reuses the connection
        This preserves the pipeline and context across network changes.
    """
    pc_id = request.get("pc_id")

    if pc_id and pc_id in pcs_map:
        # Renegotiation: reuse existing connection
        connection = pcs_map[pc_id]
        logger.info(f"Renegotiating connection pc_id={pc_id}")
        await connection.renegotiate(
            sdp=request["sdp"],
            type=request["type"],
            restart_pc=request.get("restart_pc", False),
        )
    else:
        # New connection
        connection = SmallWebRTCConnection(ICE_SERVERS)
        await connection.initialize(sdp=request["sdp"], type=request["type"])
        logger.info(f"New WebRTC connection initialized")

        @connection.event_handler("closed")
        async def on_closed(conn: SmallWebRTCConnection):
            logger.info(f"Connection closed: pc_id={conn.pc_id}")
            pcs_map.pop(conn.pc_id, None)

        # Run bot in background — don't block the HTTP response
        background_tasks.add_task(run_bot, connection)

    answer = connection.get_answer()
    pcs_map[answer["pc_id"]] = connection
    logger.info(f"Sending SDP answer: pc_id={answer['pc_id']}")
    return answer


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Pipecat Tech Support — SmallWebRTC")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)
