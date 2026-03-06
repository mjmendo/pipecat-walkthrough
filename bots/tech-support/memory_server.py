"""
M8 — Working Memory

Extends guardrails_server.py (M7) with two working memory primitives:

  M8a: ConversationSummaryProcessor — compresses context when message count
       exceeds compress_threshold. Fires gpt-4o-mini to summarize old messages,
       replacing them with a single "Conversation so far: ..." system message.
       Sits between user_agg and llm in the pipeline. Adds ~0ms on normal turns,
       ~200-300ms on the turn where compression fires (rare, once per N/2 turns
       after threshold).

  M8b: FactExtractionObserver — out-of-path observer. After each assistant turn
       (LLMFullResponseEndFrame), reads the last user+assistant exchange, calls
       gpt-4o-mini to extract {device, os, error, steps_tried}, and injects
       these as a system message at context.messages[1]. Zero latency on the
       frame path. Facts persist and accumulate for the entire session.

Pipeline stem (M7 → M8 additions marked with M8x):

    transport.input()
    stt
    ContentSafetyGuard       # M7a
    TopicGuard               # M7b
    PIIRedactGuard(input)    # M7c
    user_aggregator
    ConversationSummaryProcessor   # M8a: compresses context over threshold
    llm
    PIIRedactGuard(output)   # M7c
    tts
    rtvi
    transport.output()
    assistant_aggregator

    observers=[
        LoggingMetricsObserver(),
        RTVIObserver(rtvi),
        DebugFrameObserver(),
        audit_observer,
        fact_observer,         # M8b: extracts facts after each assistant turn
    ]

Run:
    python bots/tech-support/memory_server.py

Then open http://localhost:7860

M8 verification:
    1. Say "My MacBook Pro running macOS Ventura shows a kernel panic"
       → stdout: [MEMORY] FactExtractionObserver updated facts: {device: MacBook Pro, ...}

    2. Have 11+ exchanges (say "ok" repeatedly after each bot response)
       → stdout: [MEMORY] ConversationSummaryProcessor: compressing 10 messages → 3

    3. Ask about WiFi mid-session; ask "what laptop do I have?" later
       → Bot correctly recalls MacBook Pro from the injected facts system message

    4. Say "Tell me how to hurt someone" — ContentSafetyGuard still fires (M7 unchanged)
       → stdout: [GUARDRAIL] ContentSafetyGuard BLOCK

Learning notes (M8):
    ConversationSummaryProcessor is a FrameProcessor — it must mutate the
    LLMContextFrame before the LLM sees it. Only processors can gate/mutate.

    FactExtractionObserver is a BaseObserver — it runs after the assistant
    responds, doesn't need to modify any frame in transit, and must not block
    the next user turn. Observers are the right primitive for post-turn side effects.

    Compare to M7:
        M7 observers=[LoggingMetricsObserver(), RTVIObserver(rtvi),
                       DebugFrameObserver(), audit_observer]
        M8 adds: fact_observer — same BaseObserver pattern, different trigger.

        M7 pipeline: ... PIIRedactGuard(input) → user_agg → llm ...
        M8 adds:     ... PIIRedactGuard(input) → user_agg → ConversationSummaryProcessor → llm ...
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
from bots.shared.working_memory import ConversationSummaryProcessor, FactExtractionObserver

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

    Unchanged from guardrails_server.py — pizza bot does not need working memory
    (it uses a structured flow with fixed menu options, not freeform conversation).
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
    """Run the tech support bot with guardrails and working memory enabled.

    M8 additions over guardrails_server.py (M7):
    - ConversationSummaryProcessor: compresses context when message count > 10
      (between user_agg and llm in the pipeline)
    - FactExtractionObserver: extracts device/OS/error facts after each turn
      (added to observers list; runs out-of-path, zero latency)

    Learning note — why ConversationSummaryProcessor is in the pipeline but
    FactExtractionObserver is in observers:
        Summary must intercept the LLMContextFrame and mutate it BEFORE the LLM
        sees it — that requires being in the frame path as a FrameProcessor.
        Fact extraction happens AFTER the assistant responds; it reads from the
        shared context object directly and writes back to it. No frame needs to
        be modified in transit, so an observer is correct and avoids any latency
        on the frame path.
    """
    logger.info("[TECH-SUPPORT] Starting tech support bot (M8 — working memory enabled)")

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

    # M8a: context compression — fires gpt-4o-mini when message count > 10
    summary_processor = ConversationSummaryProcessor(
        api_key=api_key,
        compress_threshold=10,
        summary_model="gpt-4o-mini",
        max_summary_tokens=200,
    )

    # M8b: fact extraction — reads context after each turn, injects at index 1
    fact_observer = FactExtractionObserver(
        context=context,
        api_key=api_key,
        facts_slot_index=1,
        extraction_model="gpt-4o-mini",
    )

    transfer_requested = False

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
            # M8a: compress context when message count exceeds threshold
            summary_processor,
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
            fact_observer,                # M8b — fact extractor (out-of-path)
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
            events = audit_observer.events
            if events:
                logger.info(f"[GUARDRAIL] Session audit: {len(events)} guardrail event(s)")
                for e in events:
                    logger.info(
                        f"[GUARDRAIL]   {e.guard_name} | {e.action.value} | {e.reason}"
                    )
            logger.info(f"[MEMORY] Session facts at disconnect: {fact_observer.facts}")
            await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)

    # Log session summary on normal end
    events = audit_observer.events
    if events:
        logger.info(f"[GUARDRAIL] Session ended — {len(events)} guardrail event(s) recorded")

    logger.info(f"[MEMORY] Final session facts: {fact_observer.facts}")

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

    parser = argparse.ArgumentParser(description="Pipecat M8 — Working Memory")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)
