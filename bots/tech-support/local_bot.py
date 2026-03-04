"""
M1 — Local Transport Core Pipeline

Tech support voice bot using:
- LocalAudioTransport (PyAudio mic + speaker)
- SileroVADAnalyzer (voice activity detection)
- OpenAISTTService (gpt-4o-transcribe)
- OpenAILLMService (gpt-4o)
- OpenAITTSService (nova voice)

Run:
    python bots/tech-support/local_bot.py

Requires:
    - .env with OPENAI_API_KEY
    - portaudio installed (brew install portaudio)

Learning:
    This is the M1 stem — the simplest complete voice pipeline.
    Watch stdout for frame types flowing through and MetricsFrame logs.

    Key observations to make while running:
    1. Startup: SileroVADAnalyzer downloads its ONNX model on first run
    2. After speaking: watch TranscriptionFrame appear in logs
    3. After LLM responds: watch LLMTextFrame tokens stream
    4. After turn completes: watch [METRICS] lines for TTFB per service
    5. While bot is speaking: interrupt it — watch InterruptionFrame appear
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import TTSSpeakFrame
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
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

# Add project root to path so we can import from bots/shared
_PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, _PROJECT_ROOT)

from bots.shared.observers import LoggingMetricsObserver

# bots/tech-support has a hyphen so we load persona.py via importlib
import importlib.util as _ilu

_persona_spec = _ilu.spec_from_file_location(
    "persona", os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona.py")
)
_persona = _ilu.module_from_spec(_persona_spec)
_persona_spec.loader.exec_module(_persona)
INITIAL_GREETING = _persona.INITIAL_GREETING
SYSTEM_PROMPT = _persona.SYSTEM_PROMPT

load_dotenv(override=True)

# Show INFO+ logs with clean format; set to DEBUG to see all frame types
logger.remove(0)
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")


async def main():
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            # SileroVAD needs 16kHz input
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
        )
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

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    context = LLMContext(messages)

    # LLMContextAggregatorPair creates both the user and assistant aggregators.
    # The user aggregator is where VAD is wired in — it decides when the user
    # has finished speaking before sending the accumulated text to the LLM.
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),      # Mic → InputAudioRawFrame
            stt,                    # InputAudioRawFrame → TranscriptionFrame
            user_aggregator,        # TranscriptionFrame → LLMMessagesFrame (with context)
            llm,                    # LLMMessagesFrame → LLMTextFrame (streaming tokens)
            tts,                    # LLMTextFrame → TTSAudioRawFrame (audio chunks)
            transport.output(),     # TTSAudioRawFrame → speaker
            assistant_aggregator,   # Accumulates bot text back into context
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

    # Queue the opening greeting as a TTS frame before the pipeline starts
    # waiting for user input. TTSSpeakFrame bypasses the LLM entirely.
    await task.queue_frames([TTSSpeakFrame(INITIAL_GREETING)])

    runner = PipelineRunner()
    await runner.run(task)


if __name__ == "__main__":
    asyncio.run(main())
