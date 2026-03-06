"""
M6 — RTVI Protocol + Observers

Extends M4 (multi-bot call transfer) with:
1. RTVIProcessor — handles incoming RTVI client messages
2. RTVIObserver  — converts pipeline frames to RTVI events for the browser
3. Custom server message on call transfer ("Transferring to pizza bot…")
4. DebugFrameObserver — logs every frame type + direction (non-intrusive tap)

These additions enable the browser client to show:
- Speaking indicator (bot/user) — driven by RTVI events, not polling
- Live turn transcript via RTVI text events
- "Transferring to pizza bot…" notification at handoff

Learning notes (M6):
    RTVIProcessor is a FrameProcessor — it's in the frame path.
    RTVIObserver is a BaseObserver — it's a non-intrusive tap (zero latency).

    Compare: RTVIObserver.on_push_frame() runs in its own async task.
             RTVIProcessor.process_frame() runs inline, adding latency.

    For monitoring: use observers. For transforming/gating: use processors.

Run:
    python bots/tech-support/rtvi_server.py

Then open http://localhost:7860
"""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import Dict, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import RedirectResponse
from loguru import logger
from pipecat_ai_small_webrtc_prebuilt.frontend import SmallWebRTCPrebuiltUI

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
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
from pipecat.processors.frameworks.rtvi import RTVIObserver, RTVIProcessor
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat_flows import FlowManager

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from bots.shared.context import build_transfer_summary
from bots.shared.debug_observer import DebugFrameObserver
from bots.shared.observers import LoggingMetricsObserver

import importlib.util as _ilu


def _load_module(name, path):
    spec = _ilu.spec_from_file_location(name, path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_here = os.path.dirname(os.path.abspath(__file__))
_pizza_dir = os.path.join(_here, "..", "pizza")

_ts_persona = _load_module("ts_persona", os.path.join(_here, "persona.py"))
_pizza_persona = _load_module("pizza_persona", os.path.join(_pizza_dir, "persona.py"))
_pizza_flows = _load_module("pizza_flows", os.path.join(_pizza_dir, "flows.py"))

load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG", format="{time:HH:mm:ss} | {level:<7} | {message}")

pcs_map: Dict[str, SmallWebRTCConnection] = {}
ICE_SERVERS = []


async def run_pizza_bot(
    transport: SmallWebRTCTransport,
    tech_support_messages: list,
    rtvi: Optional[RTVIProcessor] = None,
) -> None:
    """Run the pizza ordering bot on an existing transport.

    Learning note: The RTVI processor from tech support is passed in so
    we can send a "transfer complete" server message from the pizza bot.
    A new RTVIObserver is created for the new pipeline — it taps the same
    transport output but for the new frame stream.
    """
    logger.info("[TRANSFER] Starting pizza bot pipeline")

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
        voice="shimmer",
        model="gpt-4o-mini-tts",
    )

    transfer_summary = build_transfer_summary(tech_support_messages)
    logger.info(f"[TRANSFER] Context summary: {transfer_summary[:100]}...")

    messages = [
        {"role": "system", "content": _pizza_persona.SYSTEM_PROMPT},
        {"role": "system", "content": transfer_summary},
    ]

    context = LLMContext(messages)
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    # Pizza bot: RTVIProcessor reuses the transport-level message path.
    # RTVIObserver taps the new pipeline's frame stream.
    pizza_rtvi = RTVIProcessor(transport=transport)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            pizza_rtvi,          # RTVI processor in the frame path
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=[
            LoggingMetricsObserver(),
            RTVIObserver(pizza_rtvi),    # converts frames to RTVI events
            DebugFrameObserver(),         # logs all frame types for M6 learning
        ],
    )

    order = {}
    flow_manager = FlowManager(
        task=task,
        llm=llm,
        context_aggregator=context_aggregator,
    )

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("[PIZZA] Client disconnected")
        await task.cancel()

    initial_node = _pizza_flows.create_select_size_node(order)
    await flow_manager.initialize(initial_node)

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
    logger.info("[PIZZA] Pizza bot session ended")


async def run_tech_support_bot(
    webrtc_connection: SmallWebRTCConnection,
) -> None:
    """Run the tech support bot with RTVI protocol enabled.

    Key additions over transfer_server.py:
    - RTVIProcessor in the pipeline: handles client→server RTVI messages
    - RTVIObserver as pipeline observer: converts frames to RTVI events
    - DebugFrameObserver: logs all frame types + directions (M6 learning)
    - send_server_message on transfer: browser shows "Transferring…" toast

    Learning note: Compare observers list in transfer_server.py (M4) vs here:
        M4: observers=[LoggingMetricsObserver()]
        M6: observers=[LoggingMetricsObserver(), RTVIObserver(rtvi), DebugFrameObserver()]
    All three are BaseObserver subclasses. They're independent, non-blocking,
    and run in the same PipelineTask's observer fan-out.
    """
    logger.info("[TECH-SUPPORT] Starting tech support bot (RTVI enabled)")

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

    transfer_schema = FunctionSchema(
        name="transfer_to_pizza",
        description=(
            "Transfer this call to our pizza ordering service. "
            "Use this when the user asks about food, ordering, or pizza."
        ),
        properties={},
        required=[],
    )
    tools = ToolsSchema(standard_tools=[transfer_schema])

    messages = [{"role": "system", "content": _ts_persona.SYSTEM_PROMPT}]
    context = LLMContext(messages, tools)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    # RTVIProcessor must be in the pipeline (it's a FrameProcessor).
    # Position: after TTS, before transport output — it appends RTVI
    # metadata frames to the stream going out to the browser.
    rtvi = RTVIProcessor(transport=transport)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            rtvi,               # RTVI in-path processor
            transport.output(),
            assistant_aggregator,
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=[
            LoggingMetricsObserver(),    # M5a — stdout metric logs
            RTVIObserver(rtvi),           # M6 — frame → RTVI event conversion
            DebugFrameObserver(),          # M6 — frame flow logger (observer mode)
        ],
    )

    transfer_requested = False

    async def handle_transfer_to_pizza(params: FunctionCallParams):
        nonlocal transfer_requested
        logger.info("[TRANSFER] transfer_to_pizza called — sending RTVI notification")
        transfer_requested = True

        # Send custom RTVI server message BEFORE cancelling the pipeline.
        # The browser client receives this as an RTVIEvent.Custom and shows
        # a "Transferring to pizza bot…" notification.
        await rtvi.send_server_message({
            "type": "transfer",
            "destination": "pizza",
            "message": "Transferring you to our pizza ordering service…",
        })

        await params.result_callback({
            "status": "transferring",
            "message": "Transferring you to our pizza service now.",
        })

        transport._input._client._leave_counter += 1
        await task.cancel()

    llm.register_function("transfer_to_pizza", handle_transfer_to_pizza)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("[TECH-SUPPORT] Client connected — queuing greeting")
        messages.append({
            "role": "system",
            "content": f"Greet the user: {_ts_persona.INITIAL_GREETING}"
        })
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("[TECH-SUPPORT] Client disconnected")
        if not transfer_requested:
            await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)

    if transfer_requested:
        logger.info("[TRANSFER] Tech support ended — starting pizza bot")
        transport._input._initialized = False
        transport._output._initialized = False
        transport._input._cancelling = False
        transport._output._cancelling = False
        try:
            await run_pizza_bot(transport, messages, rtvi)
        except Exception as e:
            logger.exception(f"[TRANSFER] Pizza bot failed: {e}")
        finally:
            await transport._input._client.disconnect()
    else:
        logger.info("[TECH-SUPPORT] Session ended normally")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    coros = [pc.disconnect() for pc in pcs_map.values()]
    await asyncio.gather(*coros)
    pcs_map.clear()


app = FastAPI(lifespan=lifespan)
app.mount("/client", SmallWebRTCPrebuiltUI)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/client/")


@app.post("/start")
async def start():
    return {"webrtc_request_params": {"endpoint": "/api/offer"}}


@app.patch("/api/offer")
async def offer_ice(request: dict):
    pc_id = request.get("pc_id")
    candidates = request.get("candidates", [])
    if not pc_id or pc_id not in pcs_map:
        return {"status": "unknown_peer"}
    connection = pcs_map[pc_id]
    for c in candidates:
        await connection.add_ice_candidate(c)
    return {"status": "ok"}


@app.post("/api/offer")
async def offer(request: dict, background_tasks: BackgroundTasks):
    pc_id = request.get("pc_id")

    if pc_id and pc_id in pcs_map:
        connection = pcs_map[pc_id]
        await connection.renegotiate(
            sdp=request["sdp"],
            type=request["type"],
            restart_pc=request.get("restart_pc", False),
        )
    else:
        connection = SmallWebRTCConnection(ICE_SERVERS)
        await connection.initialize(sdp=request["sdp"], type=request["type"])

        @connection.event_handler("closed")
        async def on_closed(conn: SmallWebRTCConnection):
            logger.info(f"Connection closed: pc_id={conn.pc_id}")
            pcs_map.pop(conn.pc_id, None)

        background_tasks.add_task(run_tech_support_bot, connection)

    answer = connection.get_answer()
    pcs_map[answer["pc_id"]] = connection
    return answer


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Pipecat M6 — RTVI + Multi-Bot")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)
