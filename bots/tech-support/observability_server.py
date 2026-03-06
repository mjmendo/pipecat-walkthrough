"""
M5 — Observability Server

Extends M4's transfer_server with full observability:
- M5a: LoggingMetricsObserver (stdout — already in M1/M4)
- M5b: TurnTraceObserver (OpenTelemetry → Jaeger at localhost:16686)
- M5c: PrometheusMetricsObserver (Prometheus at localhost:9090, Grafana at localhost:3000)

Pre-requisites:
    docker compose -f infra/docker-compose.yml up -d

Run:
    python bots/tech-support/observability_server.py

Then:
    - Bot: http://localhost:7860
    - Jaeger: http://localhost:16686
    - Prometheus: http://localhost:9090
    - Grafana: http://localhost:3000 (admin/admin)

Learning (M5):
    Three progressive observability layers:
    1. Native metrics (stdout): LoggingMetricsObserver intercepts MetricsFrame
    2. OpenTelemetry traces: TurnTraceObserver creates spans → Jaeger
    3. Prometheus counters: PrometheusMetricsObserver exports to Prometheus → Grafana

    All three use the same BaseObserver.on_push_frame() hook.
    None of them modify the pipeline — pure observation.

    Key question answered: "Why do we need all three?"
    - Logs: human-readable, per-run debugging
    - Traces: distributed request tracing, span hierarchy, latency attribution
    - Metrics: time-series data, alerting, dashboards, rate calculations
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
from pipecat.observers.turn_tracking_observer import TurnTrackingObserver
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
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
from pipecat.utils.tracing.setup import setup_tracing
from pipecat.utils.tracing.turn_trace_observer import TurnTraceObserver
from pipecat_flows import FlowManager

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from bots.shared.context import build_transfer_summary
from bots.shared.observers import LoggingMetricsObserver
from bots.shared.prometheus_observer import PrometheusMetricsObserver, start_metrics_server

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

# ── M5b: OpenTelemetry setup ─────────────────────────────────────────────────
def setup_otel():
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        ok = setup_tracing(service_name="pipecat-walkthrough", exporter=exporter)
        if ok:
            logger.info(f"[M5b] OpenTelemetry → Jaeger at {endpoint}")
        else:
            logger.warning("[M5b] OpenTelemetry setup failed — traces disabled")
    except Exception as e:
        logger.warning(f"[M5b] OpenTelemetry not available: {e}")


# ── M5c: Prometheus setup ─────────────────────────────────────────────────────
def setup_prometheus():
    try:
        start_metrics_server(port=8000)
        logger.info("[M5c] Prometheus metrics at http://localhost:8000/metrics")
    except Exception as e:
        logger.warning(f"[M5c] Prometheus server error: {e}")


pcs_map: Dict[str, SmallWebRTCConnection] = {}
ICE_SERVERS = []


def _make_observers():
    """Create the full observer stack for one pipeline session.

    Returns list of observers to pass to PipelineTask.

    M5a: LoggingMetricsObserver (stdout)
    M5b: TurnTraceObserver (OTel → Jaeger)
    M5c: PrometheusMetricsObserver (Prometheus)
    """
    observers = [LoggingMetricsObserver(), PrometheusMetricsObserver()]

    try:
        from pipecat.utils.tracing.setup import is_tracing_available
        if is_tracing_available():
            turn_tracker = TurnTrackingObserver()
            latency_tracker = UserBotLatencyObserver()
            trace_observer = TurnTraceObserver(
                turn_tracker=turn_tracker,
                latency_tracker=latency_tracker,
            )
            observers.extend([turn_tracker, latency_tracker, trace_observer])
            logger.info("[M5b] TurnTraceObserver active")
    except Exception as e:
        logger.warning(f"[M5b] TurnTraceObserver skipped: {e}")

    return observers


async def run_pizza_bot(transport, tech_support_messages: list) -> None:
    logger.info("[TRANSFER] Starting pizza bot pipeline (with observability)")

    stt = OpenAISTTService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o-transcribe")
    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o")
    tts = OpenAITTSService(api_key=os.getenv("OPENAI_API_KEY"), voice="shimmer", model="gpt-4o-mini-tts")

    transfer_summary = build_transfer_summary(tech_support_messages)
    messages = [
        {"role": "system", "content": _pizza_persona.SYSTEM_PROMPT},
        {"role": "system", "content": transfer_summary},
    ]
    context = LLMContext(messages)
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline([transport.input(), stt, context_aggregator.user(), llm, tts, transport.output(), context_aggregator.assistant()])
    task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True, enable_usage_metrics=True), observers=_make_observers())

    order = {}
    flow_manager = FlowManager(task=task, llm=llm, context_aggregator=context_aggregator)

    @transport.event_handler("on_client_disconnected")
    async def on_disc(transport, client):
        await task.cancel()

    initial_node = _pizza_flows.create_select_size_node(order)
    await flow_manager.initialize(initial_node)

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)


async def run_tech_support_bot(webrtc_connection: SmallWebRTCConnection) -> None:
    logger.info("[TECH-SUPPORT] Starting with full observability stack")

    transport = SmallWebRTCTransport(webrtc_connection=webrtc_connection, params=TransportParams(audio_in_enabled=True, audio_out_enabled=True))

    stt = OpenAISTTService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o-transcribe")
    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o")
    tts = OpenAITTSService(api_key=os.getenv("OPENAI_API_KEY"), voice="nova", model="gpt-4o-mini-tts")

    transfer_schema = FunctionSchema(
        name="transfer_to_pizza",
        description="Transfer this call to pizza ordering when user asks about food or pizza.",
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

    pipeline = Pipeline([transport.input(), stt, user_aggregator, llm, tts, transport.output(), assistant_aggregator])
    task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True, enable_usage_metrics=True), observers=_make_observers())

    transfer_requested = False

    async def handle_transfer_to_pizza(params: FunctionCallParams):
        nonlocal transfer_requested
        logger.info("[TRANSFER] transfer_to_pizza called")
        transfer_requested = True
        # Record transfer in Prometheus
        PrometheusMetricsObserver.record_transfer()
        await params.result_callback({"status": "transferring", "message": "Transferring you now."})
        transport._input._client._leave_counter += 1
        await task.cancel()

    llm.register_function("transfer_to_pizza", handle_transfer_to_pizza)

    @transport.event_handler("on_client_connected")
    async def on_connected(transport, client):
        messages.append({"role": "system", "content": f"Greet: {_ts_persona.INITIAL_GREETING}"})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport, client):
        if not transfer_requested:
            await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)

    if transfer_requested:
        transport._input._initialized = False
        transport._output._initialized = False
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
    setup_otel()
    setup_prometheus()
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
        await connection.renegotiate(sdp=request["sdp"], type=request["type"], restart_pc=request.get("restart_pc", False))
    else:
        connection = SmallWebRTCConnection(ICE_SERVERS)
        await connection.initialize(sdp=request["sdp"], type=request["type"])

        @connection.event_handler("closed")
        async def on_closed(conn):
            pcs_map.pop(conn.pc_id, None)

        background_tasks.add_task(run_tech_support_bot, connection)

    answer = connection.get_answer()
    pcs_map[answer["pc_id"]] = connection
    return answer


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
