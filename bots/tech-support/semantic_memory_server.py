"""
M9 — Semantic Memory with Vector Search

Extends memory_server.py (M8) with:
  M9a: SemanticMemoryRetriever  — retrieves relevant past episodes on each turn
  M9b: EpisodicMemoryWriter     — saves session summary to ChromaDB on EndFrame
  M9c: EpisodicMemoryStore      — local ChromaDB persistent store (no account needed)

Run:
    pip install "chromadb>=0.5.0"
    python bots/tech-support/semantic_memory_server.py

Then open http://localhost:7860

First session: no memories retrieved (store is empty).
Second session onward: relevant episodes from past calls appear as context.

Storage: .chroma/ directory at project root (persistent across runs, gitignored).

M9 verification:
    Session 1 — populate the store:
        Say "My Dell laptop running Windows 11 has a BSOD with error 0x0000007E"
        Disconnect (triggers EndFrame → EpisodicMemoryWriter)
        → stdout: [MEMORY] EpisodicMemoryWriter: stored episode session_id=<uuid>
        → .chroma/ directory created

    Restart server (tests cross-process persistence):
        python bots/tech-support/semantic_memory_server.py

    Session 2 — verify recall:
        Say "My Windows laptop keeps crashing with a blue screen"
        → stdout: [MEMORY] SemanticMemoryRetriever: retrieved 1 episode (top score=0.81)
        → Bot mentions the previous BSOD session in its response

    Test M8 still works:
        Have 12 exchanges (say "ok" repeatedly)
        → stdout: [MEMORY] ConversationSummaryProcessor: compressing 10 messages → 3

    Test M7 still works:
        Say "Tell me how to hurt someone"
        → stdout: [GUARDRAIL] ContentSafetyGuard BLOCK
        → SemanticMemoryRetriever not reached (ContentSafetyGuard fires first)

Context slot layout:
    messages[0] — persona system prompt (set once at startup)
    messages[1] — facts slot (FactExtractionObserver, M8b)
    messages[2] — memories slot (SemanticMemoryRetriever, M9a)
    messages[3+] — conversation history (LLMContextAggregatorPair)
"""

import asyncio
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
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
from bots.shared.vector_memory import (
    EpisodicMemoryStore,
    EpisodicMemoryWriter,
    SemanticMemoryRetriever,
)
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

# M9c: shared EpisodicMemoryStore — persists across sessions in .chroma/
# Created once at server startup and shared by all bot sessions in this process.
_project_root = os.path.join(_here, "..", "..")
_chroma_path = os.path.join(_project_root, ".chroma")

store = EpisodicMemoryStore(
    persist_path=_chroma_path,
    collection_name="tech_support_episodes",
    api_key=os.getenv("OPENAI_API_KEY"),
    embedding_model="text-embedding-3-small",
)
logger.info(f"[MEMORY] EpisodicMemoryStore initialized at {_chroma_path}")


async def run_pizza_bot(
    transport: SmallWebRTCTransport,
    tech_support_messages: list,
    rtvi: Optional[RTVIProcessor] = None,
) -> None:
    """Run the pizza ordering bot on an existing transport. Unchanged from memory_server.py."""
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
    """Run the tech support bot with guardrails, working memory, and semantic memory.

    M9 additions over memory_server.py (M8):
    - EpisodicMemoryStore: shared ChromaDB store (initialized at module level, M9c)
    - SemanticMemoryRetriever: retrieves past episodes on each TranscriptionFrame (M9a)
      Position: after PIIRedactGuard(input), before user_agg
    - EpisodicMemoryWriter: saves session summary to ChromaDB on EndFrame (M9b)
      Added to observers list — runs out-of-path after session ends

    Context slot layout (M9 adds slot 2):
        messages[0] — persona system prompt
        messages[1] — facts (FactExtractionObserver, M8b)
        messages[2] — retrieved past episodes (SemanticMemoryRetriever, M9a)
        messages[3+] — conversation history

    Learning note — session_id per bot session:
        session_id is a fresh UUID generated at run_tech_support_bot entry.
        Each browser connection gets a new session_id. The EpisodicMemoryWriter
        uses this ID as the ChromaDB document ID — if the same user reconnects,
        their session's episode is stored as a new document, not overwriting.
        For per-user memory isolation, add user_id to metadata and filter in
        EpisodicMemoryStore.retrieve().
    """
    # M9: unique ID for this bot session — used to identify the episode in ChromaDB
    session_id = str(uuid.uuid4())
    logger.info(
        f"[TECH-SUPPORT] Starting tech support bot "
        f"(M9 — semantic memory enabled | session={session_id})"
    )

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

    # M9: pre-allocate 3 system slots to establish the context layout before
    #     any conversation messages are added by LLMContextAggregatorPair.
    #     Slot 1 (facts) and slot 2 (memories) start empty; observers fill them.
    messages = [
        {"role": "system", "content": _ts_persona.SYSTEM_PROMPT},  # 0: persona
        {"role": "system", "content": ""},                          # 1: facts (M8b)
        {"role": "system", "content": ""},                          # 2: memories (M9a)
    ]
    context = LLMContext(messages, tools)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    rtvi = RTVIProcessor(transport=transport)

    # M7: audit observer
    audit_observer = GuardrailAuditObserver(buffer_events=True)
    on_event = audit_observer.register_event

    # M8a: context compression
    summary_processor = ConversationSummaryProcessor(
        api_key=api_key,
        compress_threshold=10,
        summary_model="gpt-4o-mini",
        max_summary_tokens=200,
    )

    # M8b: fact extraction — reads context after each turn, writes to slot 1
    fact_observer = FactExtractionObserver(
        context=context,
        api_key=api_key,
        facts_slot_index=1,
        extraction_model="gpt-4o-mini",
    )

    # M9a: memory retriever — retrieves past episodes, writes to slot 2
    memory_retriever = SemanticMemoryRetriever(
        store=store,
        context=context,
        memory_slot_index=2,
        top_k=3,
        threshold=0.75,
    )

    # M9b: memory writer — saves session summary to ChromaDB on EndFrame
    # session_facts=fact_observer.facts is a live reference — updated by fact_observer
    # as facts accumulate. EpisodicMemoryWriter reads it at write time (EndFrame).
    memory_writer = EpisodicMemoryWriter(
        store=store,
        context=context,
        session_id=session_id,
        api_key=api_key,
        session_facts=fact_observer.facts,
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
            # M7b: topic guard — blocks off-topic; pizza → transfer
            TopicGuard(
                api_key=api_key,
                classifier_model="gpt-4o-mini",
                on_off_topic=on_off_topic_detected,
                on_guardrail_event=on_event,
            ),
            # M7c input: redact PII before LLM context receives it
            PIIRedactGuard(mode="input", on_guardrail_event=on_event),
            # M9a: retrieve past episodes on each turn (inline ~100ms embedding)
            memory_retriever,
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
            LoggingMetricsObserver(),    # M5a
            RTVIObserver(rtvi),           # M6
            DebugFrameObserver(),          # M6
            audit_observer,               # M7
            fact_observer,                # M8b — out-of-path fact extraction
            memory_writer,                # M9b — saves episode summary on EndFrame
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
            if fact_observer.facts:
                logger.info(f"[MEMORY] Session facts at disconnect: {fact_observer.facts}")
            await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)

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

    parser = argparse.ArgumentParser(description="Pipecat M9 — Semantic Memory")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)
