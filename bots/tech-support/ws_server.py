"""
M2 — WebSocket Transport Server

Same tech support bot as M1, now accessible from a browser via WebSocket.

Transport: FastAPIWebsocketTransport
Serializer: RawAudioSerializer (browser sends raw PCM, receives WAV chunks)

Run:
    uvicorn bots.tech-support.ws_server:app --port 8765

Then open frontend/websocket.html in a browser.

Learning:
    - The pipeline is identical to M1. Only the transport changes.
    - Each browser connection spawns a new PipelineTask and PipelineRunner.
    - The serializer converts binary WebSocket messages to InputAudioRawFrame.
    - Without a serializer, input messages are silently dropped (see fastapi.py:305).
    - add_wav_header=True on the transport: audio is wrapped in WAV for browser playback.

    Key observation: open Chrome DevTools → Network → WS tab.
    You'll see binary frames flowing both directions during a turn.
"""

import asyncio
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from loguru import logger

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
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from bots.shared.observers import LoggingMetricsObserver
from bots.shared.raw_audio_serializer import RawAudioSerializer

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

app = FastAPI()


@app.get("/")
async def index():
    """Serve the WebSocket frontend."""
    frontend_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "frontend", "websocket.html"
    )
    return FileResponse(frontend_path)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """One WebSocket connection = one bot session.

    Learning note: Each call to this endpoint creates a fresh pipeline.
    The browser is responsible for opening the connection when the user
    clicks the mic button, and closing it when done.
    """
    logger.info("New WebSocket connection")
    await websocket.accept()

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=True,      # Browser can play WAV without extra decoding
            serializer=RawAudioSerializer(sample_rate=16000, num_channels=1),
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

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected — queuing greeting")
        messages.append({"role": "system", "content": f"Greet the user: {INITIAL_GREETING}"})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected — cancelling pipeline")
        await task.cancel()

    # handle_sigint=False because the FastAPI server handles signals
    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
    logger.info("WebSocket session ended")


if __name__ == "__main__":
    uvicorn.run(app, host="localhost", port=8765)
