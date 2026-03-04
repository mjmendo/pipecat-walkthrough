# M1 Learning Guide — Core Pipeline (Local Transport)

## The Stem

Every guide in this series uses the same bot pipeline, adding layers progressively. The M1 stem is the simplest complete voice pipeline:

```python
pipeline = Pipeline([
    transport.input(),    # mic → InputAudioRawFrame
    stt,                  # → TranscriptionFrame
    user_aggregator,      # → LLMMessagesFrame (context)
    llm,                  # → LLMTextFrame (tokens)
    tts,                  # → TTSAudioRawFrame (audio)
    transport.output(),   # → speaker
    assistant_aggregator, # → updates context
])
task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True))
runner = PipelineRunner()
await runner.run(task)
```

**Services used in M1:**
- Transport: `LocalAudioTransport` (PyAudio)
- VAD: `SileroVADAnalyzer` (inside `LLMUserAggregatorParams`)
- STT: `OpenAISTTService` (`gpt-4o-transcribe`)
- LLM: `OpenAILLMService` (`gpt-4o`)
- TTS: `OpenAITTSService` (`nova` voice)

> **Provider landscape (reference for M5.5):**
> - STT alternatives: Deepgram Nova-3 (~180ms TTFB), Whisper.cpp (local/free)
> - LLM alternatives: Ollama/Llama 3.2 (local/free), Anthropic Haiku (fast+cheap)
> - TTS alternatives: Cartesia Sonic (~80ms TTFB), Coqui XTTS (local/free)

---

## Example 1 — Normal Turn

**Do:** Start the bot, say "Hi, what can you do?"

**Observe in stdout:**
```
HH:MM:SS | INFO    | Transcription: Hi, what can you do?
HH:MM:SS | INFO    | LLM response streaming...
HH:MM:SS | INFO    | [METRICS] TTFB | processor=OpenAISTTService value=0.42s
HH:MM:SS | INFO    | [METRICS] TTFB | processor=OpenAILLMService value=0.87s
HH:MM:SS | INFO    | [METRICS] TTFB | processor=OpenAITTSService value=0.31s
```

**Understand:**
The pipeline is a typed frame stream. Each stage transforms one frame type into another:
- `InputAudioRawFrame` (mic) → `TranscriptionFrame` (STT) → `LLMMessagesFrame` (aggregator) → `LLMTextFrame` (LLM) → `TTSAudioRawFrame` (TTS)

The pipeline doesn't "know" about voice — it just passes frames. Swapping STT from OpenAI to Deepgram only changes which class produces the `TranscriptionFrame`. The rest of the pipeline is unchanged.

---

## Example 2 — Reading MetricsFrame Logs

**Do:** Complete a turn, look at the `[METRICS]` lines in stdout.

**Observe:**
```
[METRICS] TTFB | processor=OpenAISTTService  model=gpt-4o-transcribe  value=0.42s
[METRICS] TTFB | processor=OpenAILLMService  model=gpt-4o             value=0.87s
[METRICS] TTFB | processor=OpenAITTSService  model=gpt-4o-mini-tts    value=0.31s
[METRICS] LLM tokens | prompt=142  completion=67  total=209
[METRICS] TTS chars  | characters=284
```

**Understand:**
Pipecat emits `MetricsFrame` objects as the pipeline runs. These are `SystemFrame` subtypes — they bypass processor queues and arrive at observers immediately.

`LoggingMetricsObserver` (in `bots/shared/observers.py`) implements `BaseObserver.on_push_frame()`. It receives every frame pushed between any two processors. It only logs `MetricsFrame` instances and ignores everything else.

Key insight: **LLM TTFB is almost always the highest.** STT and TTS are bounded by model inference per token; LLM varies with prompt complexity and output length.

---

## Example 3 — Interruption

**Do:** Start a question that triggers a long response (e.g., "Explain everything you know about networking"). Wait 2-3 seconds until the bot is mid-sentence, then speak over it.

**Observe:**
- Bot stops speaking immediately (< 100ms)
- Logs show `InterruptionFrame` followed by a new turn starting

**Understand:**
Pipecat uses a dual-queue architecture:

1. **Data queue** — normal frames (audio, text, context)
2. **System queue** — priority frames (start, cancel, interruption)

When `VADUserStartedSpeakingFrame` arrives while TTS is playing, the pipeline converts it to `InterruptionFrame`. This is a `SystemFrame`, so it **bypasses** the data queue entirely and is handled in ~1ms.

The TTS service receives the interrupt, cancels the current API call, and the transport output drains. The user's new speech is processed as a fresh turn with the existing context.

---

## Example 4 — Error Handling (Fatal vs Non-Fatal)

**Do:** Temporarily edit `.env` to use an invalid API key (`OPENAI_API_KEY=sk-invalid`). Run the bot and speak.

**Observe:**
- STT call fails
- An `ErrorFrame` is logged
- The pipeline **stays alive** (doesn't crash)

**Restore:** Fix the API key and restart.

**Understand:**
Pipecat errors come in two kinds:

- `push_error(fatal=False)` — logs the error, pipeline continues waiting for next input. Used for transient API failures.
- `push_error(fatal=True)` — sends a `CancelFrame` downstream, which shuts down all processors in order via `EndFrame`.

The default for API call failures is `fatal=False` — the pipeline survives a single bad request. This is intentional: a retry or the user speaking again will recover the session.

---

## Example 5 — Graceful Shutdown

**Do:** Press `Ctrl+C` while the bot is idle (not mid-turn).

**Observe in logs:**
```
Received signal 2, shutting down...
EndFrame flowing through pipeline...
[processors close in order]
```

**Understand:**
`PipelineRunner` catches `SIGINT` and queues an `EndFrame` at the head of the pipeline. The `EndFrame` propagates downstream through every processor, giving each one a chance to clean up its resources (close API connections, stop audio streams, flush buffers).

Order matters: `EndFrame` arrives at `LocalAudioInputTransport` first, then `STT`, `LLM`, `TTS`, `LocalAudioOutputTransport` last. Output transport is last so it can finish playing any buffered audio before closing.

One `StartFrame` + one `EndFrame` = one pipeline session.

---

## What You've Confirmed

After completing M1, you've seen **TOUR-PLAN Topics 1–8 in real code**:

| Tour Topic | What you saw in M1 |
|------------|-------------------|
| 1. Frame types | `InputAudioRawFrame`, `TranscriptionFrame`, `LLMTextFrame`, `TTSAudioRawFrame` |
| 2. Pipeline | Linear stage chain, each stage transforms frame type |
| 3. PipelineTask + Runner | `task = PipelineTask(pipeline, ...)`, `await runner.run(task)` |
| 4. Turn management | User aggregator accumulates text; assistant aggregator writes context back |
| 5. VAD | Silero ONNX model detecting speech boundaries inside user aggregator |
| 6. Interruption | Dual-queue; `InterruptionFrame` bypasses data queue |
| 7. Metrics | `MetricsFrame` with `TTFBMetricsData`, `LLMUsageMetricsData` |
| 8. Error handling | `ErrorFrame`, fatal vs non-fatal |
