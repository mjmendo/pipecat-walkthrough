"""
M4 — Multi-Bot Call Transfer Server

Two personas on the same WebRTC connection:
1. Tech Support Bot (Alex, nova voice) — starts the call
2. Pizza Bot (Marco, shimmer voice) — activated on transfer

Transfer flow:
1. Tech support LLM calls transfer_to_pizza() function tool
2. Handler captures current context, cancels tech support pipeline task
3. New pizza pipeline task starts on the SAME WebRTC connection
4. Pizza bot receives context summary as first system message
5. pipecat-flows manages pizza ordering state machine

Run:
    python bots/tech-support/transfer_server.py

Then open http://localhost:7860

Learning:
    - Function calling drives application logic (not just conversation)
    - Pipeline lifecycle: cancel() + new PipelineTask on same transport
    - Context hand-off: summary injected as system message to pizza bot
    - pipecat-flows state machine manages multi-turn ordering
    - Two different TTS voices (nova → shimmer) signal persona change to user

    Key observation: Watch server logs during transfer.
    You'll see: FunctionCallInProgressFrame → pipeline cancel → new task start
"""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import Dict, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
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
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")

pcs_map: Dict[str, SmallWebRTCConnection] = {}
ICE_SERVERS = []


async def run_pizza_bot(
    transport: SmallWebRTCTransport,
    tech_support_messages: list,
) -> None:
    """Run the pizza ordering bot on an existing transport.

    Called after tech support task is cancelled. The WebRTC connection
    stays alive — only the pipeline changes.

    Learning note: This is the "pipeline restart" pattern for call transfer.
    The transport is reused; everything else is fresh.
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

    # shimmer voice — user hears the persona change immediately
    tts = OpenAITTSService(
        api_key=os.getenv("OPENAI_API_KEY"),
        voice="shimmer",
        model="gpt-4o-mini-tts",
    )

    # Build context summary from tech support conversation
    transfer_summary = build_transfer_summary(tech_support_messages)
    logger.info(f"[TRANSFER] Context summary: {transfer_summary[:100]}...")

    messages = [
        {"role": "system", "content": _pizza_persona.SYSTEM_PROMPT},
        {"role": "system", "content": transfer_summary},
    ]

    context = LLMContext(messages)
    # Keep the pair object (not unpacked) — FlowManager calls .user() and
    # .assistant() methods on it to access each aggregator.
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
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
        observers=[LoggingMetricsObserver()],
    )

    # Set up pipecat-flows for pizza ordering state machine
    order = {}  # Shared state across flow nodes
    flow_manager = FlowManager(
        task=task,
        llm=llm,
        context_aggregator=context_aggregator,
    )

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("[PIZZA] Client disconnected")
        await task.cancel()

    # Initialize flow with first node — this queues LLMRunFrame so the
    # pizza bot speaks its greeting immediately when the pipeline starts.
    initial_node = _pizza_flows.create_select_size_node(order)
    await flow_manager.initialize(initial_node)

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
    logger.info("[PIZZA] Pizza bot session ended")


async def run_tech_support_bot(
    webrtc_connection: SmallWebRTCConnection,
) -> None:
    """Run the tech support bot. On transfer, starts pizza bot on same transport.

    Learning note: transfer_to_pizza is a function tool registered with the LLM.
    When the LLM decides to call it (user asks for pizza), the handler:
    1. Cancels this pipeline task (sends CancelFrame)
    2. Starts a new pizza pipeline on the same transport
    The WebRTC connection is never interrupted — the user doesn't hear a pause.
    """
    logger.info("[TECH-SUPPORT] Starting tech support bot")

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

    # nova voice for tech support
    tts = OpenAITTSService(
        api_key=os.getenv("OPENAI_API_KEY"),
        voice="nova",
        model="gpt-4o-mini-tts",
    )

    # Register transfer_to_pizza as a function tool
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

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
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
        observers=[LoggingMetricsObserver()],
    )

    transfer_requested = False

    async def handle_transfer_to_pizza(params: FunctionCallParams):
        nonlocal transfer_requested
        logger.info("[TRANSFER] transfer_to_pizza called — initiating handoff")
        transfer_requested = True

        # Acknowledge the function call to the LLM (brief message before hang-up)
        await params.result_callback({
            "status": "transferring",
            "message": "Transferring you to our pizza service now.",
        })

        # Keep the WebRTC connection alive through the pipeline transition.
        # task.cancel() triggers client.disconnect() twice (input + output),
        # decrementing the leave counter 2→1→0 which closes the connection.
        # Adding 1 here makes it bottom out at 1 instead, keeping the peer alive.
        transport._input._client._leave_counter += 1

        # Cancel current pipeline — CancelFrame flows through all processors
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

    # After tech support pipeline ends, check if transfer was requested
    if transfer_requested:
        logger.info("[TRANSFER] Tech support ended — starting pizza bot")
        # Reset initialization flags so the pizza pipeline can restart the
        # audio receive task on the still-open WebRTC connection.
        transport._input._initialized = False
        transport._output._initialized = False
        # Reset cancellation flag: task.cancel() sets _cancelling=True on all
        # processors, and queue_frame() silently drops frames when _cancelling
        # is True. Without this reset the reused transport drops every frame.
        transport._input._cancelling = False
        transport._output._cancelling = False
        try:
            await run_pizza_bot(transport, messages)
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
async def offer_ice(request: Request):
    data = await request.json()
    pc_id = data.get("pc_id")
    candidates = data.get("candidates", [])
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

    parser = argparse.ArgumentParser(description="Pipecat Multi-Bot — Tech Support + Pizza")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)
