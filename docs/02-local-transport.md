# M1 — Local Transport: Core Pipeline

## What This Milestone Does

Runs a complete voice AI pipeline entirely on your machine. No browser, no network, no server — just a Python process reading your mic and writing to your speaker.

**Pipeline:**
```
[mic] → LocalAudioTransport.input()
      → OpenAISTTService (gpt-4o-transcribe)
      → LLMUserResponseAggregator
      → OpenAILLMService (gpt-4o)
      → OpenAITTSService (nova)
      → LocalAudioTransport.output()
      → [speaker]
      → LLMAssistantResponseAggregator
```

---

## Each Stage Explained

### LocalAudioTransport.input()
**In:** hardware audio device (mic)
**Out:** `InputAudioRawFrame` — 10ms chunks of raw PCM audio at 16kHz

The transport reads from PyAudio in a background thread and converts chunks into frames. Audio is 16-bit mono by default. Runs at 16kHz to match Silero VAD's expected sample rate.

### SileroVADAnalyzer (inside LLMUserAggregatorParams)
**In:** `InputAudioRawFrame` (continuous mic stream)
**Out:** `VADUserStartedSpeakingFrame` / `VADUserStoppedSpeakingFrame`

VAD (Voice Activity Detection) runs the Silero ONNX model on each chunk to detect speech boundaries. The VAD is wired into the user aggregator, not placed directly in the pipeline — it tells the aggregator "user started/stopped speaking" so the aggregator knows when to flush accumulated audio to STT.

The Silero model (~1MB ONNX file) downloads automatically on first run.

### OpenAISTTService (gpt-4o-transcribe)
**In:** Accumulated audio from VAD speech segment
**Out:** `TranscriptionFrame` (text)

Sends the audio to OpenAI's transcription API. Returns a `TranscriptionFrame` with the recognized text once the API responds. TTFB here = time from sending audio to receiving the first transcript word.

### LLMUserResponseAggregator
**In:** `TranscriptionFrame` + `VADUserStoppedSpeakingFrame`
**Out:** `LLMMessagesFrame` (assembled conversation context)

Accumulates transcribed text and, when the user stops speaking, creates an LLM context message (`role: user`) appended to the full conversation history. This is what gives the LLM memory of prior turns.

### OpenAILLMService (gpt-4o)
**In:** `LLMMessagesFrame` (full context)
**Out:** `LLMTextFrame` (streaming text tokens)

Sends the context to GPT-4o and streams tokens back as `LLMTextFrame` objects. TTFB here = time from sending request to receiving the first token.

Function calls (if enabled) appear as `FunctionCallInProgressFrame` here.

### OpenAITTSService (nova)
**In:** `LLMTextFrame` tokens (accumulated into sentences)
**Out:** `TTSAudioRawFrame` — PCM audio chunks

Converts LLM text into speech. Waits for punctuation boundaries (sentence-end) before sending to the API, then streams audio chunks back. TTFB here = time to first audio chunk.

### LocalAudioTransport.output()
**In:** `TTSAudioRawFrame`
**Out:** hardware audio device (speaker)

Writes PCM chunks to PyAudio output stream. Resamples from TTS sample rate (24kHz) to output device rate if needed.

### LLMAssistantResponseAggregator
**In:** `LLMTextFrame` tokens (same ones that went to TTS)
**Out:** updates `LLMContext` with `role: assistant` message

Accumulates the bot's response text and writes it back into the context after the turn completes. This is what gives the LLM memory of what it said.

---

## Frame Types in This Pipeline

| Frame | Direction | From → To | Purpose |
|-------|-----------|-----------|---------|
| `InputAudioRawFrame` | downstream | transport.input → STT | Raw mic PCM |
| `VADUserStartedSpeakingFrame` | downstream | VAD → aggregator | Speech begin signal |
| `VADUserStoppedSpeakingFrame` | downstream | VAD → aggregator | Speech end signal |
| `TranscriptionFrame` | downstream | STT → user_agg | Recognized text |
| `LLMMessagesFrame` | downstream | user_agg → LLM | Full context for LLM |
| `LLMTextFrame` | downstream | LLM → TTS | Streaming text tokens |
| `TTSAudioRawFrame` | downstream | TTS → transport.output | PCM audio |
| `MetricsFrame` | downstream | STT/LLM/TTS → observers | TTFB + usage data |
| `InterruptionFrame` | upstream | transport.input → pipeline | User spoke mid-response |
| `StartFrame` | downstream | task → all | Session start |
| `EndFrame` | downstream | task/runner → all | Session end |

---

## Metrics Logged (via LoggingMetricsObserver)

After each turn, look for `[METRICS]` lines in stdout:

```
HH:MM:SS | INFO    | [METRICS] TTFB | processor=OpenAISTTService model=gpt-4o-transcribe value=0.42s
HH:MM:SS | INFO    | [METRICS] TTFB | processor=OpenAILLMService model=gpt-4o value=0.89s
HH:MM:SS | INFO    | [METRICS] TTFB | processor=OpenAITTSService model=gpt-4o-mini-tts value=0.31s
HH:MM:SS | INFO    | [METRICS] LLM tokens | processor=OpenAILLMService prompt=142 completion=67 total=209
HH:MM:SS | INFO    | [METRICS] TTS chars | processor=OpenAITTSService characters=284
```

---

## Interruption Behavior

When you speak while the bot is responding:

1. LocalAudioTransport detects audio → sends `VADUserStartedSpeakingFrame`
2. Pipeline converts this to `InterruptionFrame` (a `SystemFrame` — bypasses queue)
3. TTS stops generating audio immediately
4. Transport output drains its buffer (~40ms audio may still play)
5. The user's new utterance is processed as a fresh turn

---

## Provider Alternatives (for M5.5)

These can replace the OpenAI services one-at-a-time in M5.5. The pipeline structure stays identical.

| Slot | Current | Local free alternative | Commercial alternative |
|------|---------|----------------------|----------------------|
| STT | `OpenAISTTService` | `WhisperSTTService` (Whisper.cpp) | `DeepgramSTTService` (~180ms TTFB) |
| LLM | `OpenAILLMService` | `OllamaLLMService` (Llama 3.2) | `AnthropicLLMService` (Haiku) |
| TTS | `OpenAITTSService` | `CoquiTTSService` (XTTS-v2) | `CartesiaTTSService` (~80ms TTFB) |

---

## Running

```bash
# From project root
source .venv/bin/activate

# Copy env if not done
cp .env.example .env
# Edit .env and add OPENAI_API_KEY=sk-...

python bots/tech-support/local_bot.py
```

On first run, Silero VAD downloads `silero_vad.onnx` (~1MB). Subsequent runs use the cached model.
