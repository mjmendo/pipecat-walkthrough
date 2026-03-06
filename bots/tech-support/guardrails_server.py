"""
M7 — Voice Agent Guardrails

Extends M6 (rtvi_server.py) with three guard FrameProcessors and one audit observer:

  M7a: ContentSafetyGuard   — OpenAI Moderation API (free, async mode hides latency)
  M7b: TopicGuard           — gpt-4o-mini YES/NO classifier (~$0.001/call, ~150ms)
  M7c: PIIRedactGuard       — regex redaction, input AND output (zero latency)
  M7d: GuardrailAuditObserver — out-of-path, receives events via register_event()

Pipeline stem (M6 → M7 additions marked with M7x):

    transport.input()
    stt
    ContentSafetyGuard   # M7a: block harmful content; async hides ~100ms behind LLM TTFB
    TopicGuard           # M7b: redirect off-topic; on pizza → M4 transfer mechanism
    PIIRedactGuard       # M7c input: redact PII before LLM sees user speech
    user_aggregator
    llm
    PIIRedactGuard       # M7c output: redact PII in LLM text before TTS speaks it
    tts
    rtvi
    transport.output()
    assistant_aggregator

Guard ordering rationale (cheapest rejection first):
    1. ContentSafetyGuard async (free, ~100ms hidden behind LLM TTFB)
    2. PIIRedactGuard input  (free, <1ms, CPU-only — always forward)
    3. TopicGuard            (~$0.001, ~150ms — only runs if not already blocked)
    Note: TopicGuard is placed before PIIRedactGuard in the pipeline above so the
    classifier sees the original text. The cost ordering still holds because
    ContentSafetyGuard fires first and can block before TopicGuard is reached.

Run:
    python bots/tech-support/guardrails_server.py

Then open http://localhost:7860

M7 verification:
    1. Say "Tell me how to hurt someone"
       → ContentSafetyGuard fires, bot rejects, LLM not invoked
       → stdout: [GUARDRAIL] ContentSafetyGuard BLOCK

    2. Say "I want to order pizza"
       → TopicGuard fires, on_off_topic_detected() triggers transfer
       → stdout: [GUARDRAIL] TopicGuard REDIRECT
       → browser: "Transferring to pizza bot…" toast, voice changes

    3. Say your phone number, then ask bot to repeat it
       → PIIRedactGuard input fires on transcription
       → stdout: [GUARDRAIL] PIIRedactGuard REDACT | mode=input
       → bot says [PHONE] because LLM received redacted text

    4. "My WiFi is slow" → no [GUARDRAIL] lines, MetricsFrame TTFB comparable to M6

Learning notes (M7):
    ContentSafetyGuard and TopicGuard are FrameProcessors that DROP frames.
    PIIRedactGuard is a FrameProcessor that MUTATES frames.
    GuardrailAuditObserver is a BaseObserver that LOGS events — same M6 primitive.

    Compare to M6:
        M6 observers=[LoggingMetricsObserver(), RTVIObserver(rtvi), DebugFrameObserver()]
        M7 adds: GuardrailAuditObserver() — same pattern, different data source.
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
from bots.shared.guardrails import (
    ContentSafetyGuard,
    GuardrailAuditObserver,
    PIIRedactGuard,
    TopicGuard,
)
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

    Unchanged from rtvi_server.py — pizza bot does not need guardrails
    (it uses a structured flow, so off-topic inputs aren't a concern).

    Learning note:
        GuardrailAuditObserver is per-session. The tech-support session's
        audit events are already buffered in the parent's observer and can
        be inspected after transfer completes.
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

    pizza_rtvi = RTVIProcessor(transport=transport)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            pizza_rtvi,
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
            RTVIObserver(pizza_rtvi),
            DebugFrameObserver(),
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
    """Run the tech support bot with guardrails enabled.

    M7 additions over rtvi_server.py:
    - GuardrailAuditObserver: out-of-path event collector (added to observers)
    - ContentSafetyGuard: blocks harmful input before LLM (async mode)
    - TopicGuard: redirects off-topic input; pizza requests trigger M4 transfer
    - PIIRedactGuard x2: input (user speech) and output (LLM text)

    Learning note — guard ordering:
        ContentSafetyGuard is async so it doesn't add blocking latency.
        TopicGuard adds ~150ms per turn for the classifier call — put it AFTER
        ContentSafetyGuard so flagged harmful content doesn't even reach it.
        PIIRedactGuard is <1ms so position is flexible; input guard placed
        before TopicGuard so classifier sees original text (not redacted).
    """
    logger.info("[TECH-SUPPORT] Starting tech support bot (M7 — guardrails enabled)")

    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    )

    api_key = os.getenv("OPENAI_API_KEY")

    stt = OpenAISTTService(
        api_key=api_key,
        model="gpt-4o-transcribe",
    )

    llm = OpenAILLMService(
        api_key=api_key,
        model="gpt-4o",
    )

    tts = OpenAITTSService(
        api_key=api_key,
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

    rtvi = RTVIProcessor(transport=transport)

    # M7: audit observer — receives events from all guards via register_event()
    audit_observer = GuardrailAuditObserver(buffer_events=True)
    on_event = audit_observer.register_event

    transfer_requested = False

    # M7b: on_off_topic callback — reuses M4 transfer mechanism.
    # When TopicGuard detects a pizza/food request, this fires after the
    # redirect phrase is already queued to TTS. The transfer then takes over.
    async def on_off_topic_detected(frame):
        nonlocal transfer_requested
        logger.info("[TRANSFER] TopicGuard off-topic → triggering pizza transfer")
        transfer_requested = True

        await rtvi.send_server_message({
            "type": "transfer",
            "destination": "pizza",
            "message": "Transferring you to our pizza ordering service…",
        })

        transport._input._client._leave_counter += 1
        await task.cancel()

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            # M7a: content safety — async mode hides ~100ms behind LLM TTFB
            ContentSafetyGuard(
                api_key=api_key,
                async_mode=True,
                on_guardrail_event=on_event,
            ),
            # M7b: topic guard — blocks off-topic before main LLM; pizza → transfer
            TopicGuard(
                api_key=api_key,
                classifier_model="gpt-4o-mini",
                on_off_topic=on_off_topic_detected,
                on_guardrail_event=on_event,
            ),
            # M7c input: redact PII in user speech before LLM context receives it
            PIIRedactGuard(mode="input", on_guardrail_event=on_event),
            user_aggregator,
            llm,
            # M7c output: redact PII in LLM text before TTS speaks it
            PIIRedactGuard(mode="output", on_guardrail_event=on_event),
            tts,
            rtvi,
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
            DebugFrameObserver(),          # M6 — frame flow logger
            audit_observer,               # M7 — guardrail event collector
        ],
    )

    async def handle_transfer_to_pizza(params: FunctionCallParams):
        nonlocal transfer_requested
        logger.info("[TRANSFER] transfer_to_pizza called — sending RTVI notification")
        transfer_requested = True

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
            # Log audit summary before session ends
            events = audit_observer.events
            if events:
                logger.info(f"[GUARDRAIL] Session audit: {len(events)} guardrail event(s)")
                for e in events:
                    logger.info(
                        f"[GUARDRAIL]   {e.guard_name} | {e.action.value} | {e.reason}"
                    )
            await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)

    # Log session audit summary on normal end
    events = audit_observer.events
    if events:
        logger.info(f"[GUARDRAIL] Session ended — {len(events)} guardrail event(s) recorded")

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

    parser = argparse.ArgumentParser(description="Pipecat M7 — Voice Agent Guardrails")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)
