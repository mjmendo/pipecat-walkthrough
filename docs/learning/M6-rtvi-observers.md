# M6 Learning Guide — RTVI Protocol + Observers

## What is RTVI?

Every milestone so far used the prebuilt browser UI — but the browser only received raw audio. It had no idea whether the bot was speaking, what words were being said, or when a call transfer happened. It was a black box.

**RTVI (Real-Time Voice Infrastructure)** is the protocol that fixes this. It defines a structured event layer between the Pipecat pipeline and any browser client, so the UI can react to pipeline state in real time — speaking indicators, live transcripts, custom notifications — without polling or guessing.

RTVI has two sides:

**Server side (Python):** Two components are added to the pipeline.
- `RTVIProcessor` — a `FrameProcessor` placed in the frame path (after TTS, before transport output). It handles incoming RTVI messages from the browser (e.g. config updates, action requests) and provides the `send_server_message()` method for pushing custom events to the client.
- `RTVIObserver` — a `BaseObserver` (non-intrusive tap). It watches the frame stream and converts pipeline frames into RTVI events that the browser understands: `BotStartedSpeakingFrame` → speaking indicator on; `TTSTextFrame` → bot transcript word; `TranscriptionFrame` → user transcript word.

**Client side (JavaScript):** The prebuilt UI (`@pipecat-ai/client-js`) already speaks RTVI. Once `RTVIObserver` is active on the server, the browser automatically receives and renders speaking indicators, transcripts, and any custom messages you send.

The key distinction: `RTVIProcessor` is **in the frame path** (adds latency, required for incoming client messages). `RTVIObserver` is **out of the frame path** (zero latency, watches outgoing frames). You need both.

---

## The Stem (M4 + RTVI additions)

```python
rtvi = RTVIProcessor(transport=transport)

pipeline = Pipeline([
    transport.input(), stt, user_agg, llm, tts,
    rtvi,               # new: in-path RTVI processor
    transport.output(), assistant_agg,
])

task = PipelineTask(
    pipeline,
    observers=[
        LoggingMetricsObserver(),    # unchanged from M5a
        RTVIObserver(rtvi),           # new: frame → RTVI event conversion
        DebugFrameObserver(),          # new: frame flow logger
    ],
)
```

Two new observers, one new processor. The pipeline core (stt → llm → tts) is unchanged.

---

## Example 1 — Bot Speaking Indicator

**Do:** Run `rtvi_server.py`. Open `http://localhost:7860`. Say "Hi, what can you help with?"

**Observe in browser:** The prebuilt UI shows a speaking indicator that activates exactly when the bot starts talking and deactivates when it stops. No polling — it reacts instantly.

**Observe in stdout:**
```
HH:MM:SS | DEBUG   | [FRAME] ↓ BotStartedSpeakingFrame              | OpenAITTSService          → RTVIProcessor
HH:MM:SS | DEBUG   | [FRAME] ↓ BotStoppedSpeakingFrame               | OpenAITTSService          → RTVIProcessor
```

**Understand:**
`RTVIObserver.on_push_frame()` watches for `BotStartedSpeakingFrame` and `BotStoppedSpeakingFrame`. When seen, it calls `_rtvi.push_frame(RTVIBotStartedSpeakingMessage(...))`. The RTVI message flows through the processor to `transport.output()`, which serializes it and sends it to the browser via the WebRTC data channel.

The UI doesn't calculate speaking state — it receives explicit events. This is why the indicator is exact, not approximated.

---

## Example 2 — Live Transcript via RTVI

**Do:** After Example 1, watch the prebuilt UI while saying "What are the system requirements for Windows 11?"

**Observe:** Bot response text appears word-by-word as the bot speaks, synchronized with TTS audio.

**Observe in stdout:**
```
HH:MM:SS | DEBUG   | [FRAME] ↓ TTSTextFrame                          | OpenAITTSService          → RTVIProcessor
HH:MM:SS | DEBUG   | [FRAME] ↓ TranscriptionFrame                    | OpenAISTTService          → LLMUserContextAggregator
```

**Understand:**
`RTVIObserver` converts `TTSTextFrame` → `RTVIBotTTSTextMessage` and `TranscriptionFrame` → `RTVIUserTranscriptionMessage`. The browser receives both in real time. User transcription appears as you speak; bot transcription appears as the bot generates text.

This is the same frame stream that's always been there — RTVI just makes it visible to the browser client.

---

## Example 3 — Transfer Notification in UI

**Do:** Say "I'd like to order a pizza."

**Observe in browser:** A notification appears ("Transferring you to our pizza ordering service…") and the voice changes to shimmer within seconds.

**Observe in stdout:**
```
HH:MM:SS | INFO    | [TRANSFER] transfer_to_pizza called — sending RTVI notification
HH:MM:SS | DEBUG   | [FRAME] ↓ LLMFunctionCallFrame                  | OpenAILLMService          → LLMAssistantContextAggregator
```

**Understand:**
The transfer handler calls `await rtvi.send_server_message({...})` before cancelling the pipeline. `send_server_message` wraps the dict in an `RTVIServerMessage` and pushes it to the client immediately. The browser's `RTVIEvent.ServerMessage` listener catches it and shows the toast.

This is the **custom RTVI message** pattern: `send_server_message()` lets you send any application event to the browser client without defining a formal RTVI action.

---

## Example 4 — Observer vs Processor Comparison

**Do:** Look at `bots/shared/debug_observer.py` vs what a processor version would look like.

**Observer version (current):**
```python
# In task = PipelineTask(..., observers=[DebugFrameObserver()])
async def on_push_frame(self, data: FramePushed) -> None:
    logger.debug(f"[FRAME] {direction} {frame_type} | {src} → {dst}")
    # No push_frame call needed — observer never touches the frame
```

**Processor version (hypothetical — do NOT add to the pipeline):**
```python
class DebugProcessor(FrameProcessor):
    async def process_frame(self, frame, direction):
        logger.debug(f"[FRAME] {type(frame).__name__}")
        await self.push_frame(frame, direction)  # MUST re-push or pipeline stalls
```

**Observe:** Add `time.perf_counter()` measurements around the log call in both versions. Observer: ~0μs pipeline impact. Processor: ~50–200μs per frame (async overhead).

**Understand:**
Observers run in their own async task via a fan-out proxy. The pipeline's `push_frame()` call returns immediately — it doesn't wait for observers to finish. This is why observers have zero pipeline latency.

Processors are inline — `push_frame()` in the previous processor awaits `process_frame()` in the next one. Every frame must pass through synchronously.

**Rule:** Use observers for monitoring, logging, and metrics. Use processors only when you need to transform or gate frames (change content, add delay, drop frames by type).

---

## Example 5 — Reading the DebugFrameObserver Output

**Do:** Run `rtvi_server.py` with `DebugFrameObserver()` (already included). Do one complete turn: speak a question, wait for bot to finish.

**Observe in stdout (filtered to DEBUG level):**
```
↓ VADUserStartedSpeakingFrame          | SileroVADAnalyzer         → LLMUserContextAggregator
↓ InputAudioRawFrame                   | (filtered — too noisy)
↓ TranscriptionFrame                   | OpenAISTTService          → LLMUserContextAggregator
↓ LLMMessagesAppendFrame               | LLMUserContextAggregator  → OpenAILLMService
↓ LLMTextFrame                         | OpenAILLMService          → OpenAITTSService
↓ BotStartedSpeakingFrame              | OpenAITTSService          → RTVIProcessor
↓ TTSAudioRawFrame                     | (filtered — too noisy)
↓ BotStoppedSpeakingFrame              | OpenAITTSService          → RTVIProcessor
↑ MetricsFrame                         | OpenAILLMService          → PipelineTask
```

**Understand:**
- `↓` = DOWNSTREAM (left to right in the pipeline: input → output)
- `↑` = UPSTREAM (right to left: output → input — used for MetricsFrame, SystemFrames)
- `InputAudioRawFrame` and `TTSAudioRawFrame` are filtered by `_SKIP_TYPES` (too noisy)
- `MetricsFrame` travels upstream because `LLMService` pushes it upstream to observers

This output answers: "what happened inside the pipeline during that turn?" without modifying any processor. This is the observer pattern's value: non-intrusive, after-the-fact visibility.

---

## Key Takeaways

1. **RTVI = structured event layer** between pipeline and browser. Without it, the browser only gets audio bytes.
2. **RTVIObserver** (out-of-path) converts frames to events. **RTVIProcessor** (in-path) handles incoming client messages.
3. **Custom server messages** via `rtvi.send_server_message({...})` let you push any application event to the browser.
4. **Observers run in their own task** — zero pipeline latency. This is why they're preferred for monitoring.
5. **DebugFrameObserver** is the correct tool for debugging frame flow. Never add a logging processor to the frame path just to see what's happening.
