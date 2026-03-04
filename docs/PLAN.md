# Plan: Pipecat Walkthrough — E2E AI Voice System

## Context

The goal is to learn **AI voice processing** using Pipecat as the vehicle. The user will finish the TOUR-PLAN (8 topics done, 3 pending) concurrently with this implementation. Each milestone confirms or deepens concepts from the tour while introducing new ones. The system runs fully locally with only an OpenAI API key.

The final system is a **multi-persona voice call center** with two bots (tech support + pizza ordering) that can transfer calls between each other, exposed via a browser WebRTC UI, with full observability (traces + metrics + dashboards).

---

## Monorepo Structure

```
pipecat-walkthrough/
├── docs/                        # Research docs (existing + new per milestone)
├── bots/
│   ├── shared/                  # Base pipeline builder, context utils, observers
│   ├── tech-support/            # Tech support persona bot
│   └── pizza/                   # Pizza ordering persona bot
├── frontend/                    # HTML/JS using @pipecat-ai/client-js (SmallWebRTC)
├── infra/
│   ├── docker-compose.yml       # Jaeger + Prometheus + Grafana
│   └── grafana/dashboards/      # Pre-built Grafana dashboard JSON
└── scripts/                     # Dev helpers (start all services, etc.)
```

---

## Services & Accounts

| Service | Purpose | Account needed? |
|---------|---------|-----------------|
| OpenAI API | STT (gpt-4o-transcribe), LLM (gpt-4o), TTS (OpenAITTSService) | Already have |
| Docker (local) | Jaeger, Prometheus, Grafana | None — local only |
| SmallWebRTC (aiortc) | WebRTC transport | None — pure Python |
| pipecat-ai/small-webrtc-prebuilt | Prebuilt browser client | None — open source |

---

## Milestones

### M0 — Project Bootstrap
**Goal:** Functioning monorepo, environment, git repo.
- **Save this plan to `docs/PLAN.md`** (permanent project reference)
- Initialize git repo (private, mjmendo)
- Create monorepo directory structure above
- Python virtual env + `pyproject.toml` per module
- `.env` with `OPENAI_API_KEY`
- Verify pipecat installs: `pipecat-ai[openai,silero,webrtc]`
- Document: `docs/01-architecture.md` — system overview + component map
- **Commit**

---

### M1 — Local Transport: Core Pipeline (Terminal)
**Goal:** Working voice bot with mic+speaker, no network, no UI.
- Bot: tech support persona, system prompt as IT help desk agent
- Pipeline: VAD (Silero) → STT (OpenAISTTService, gpt-4o-transcribe) → LLM (OpenAI gpt-4o) → TTS (OpenAITTSService, `nova` voice)
- Transport: `LocalTransport` (PyAudio)
- Enable `PipelineParams(enable_metrics=True)`
- Log `MetricsFrame` to stdout so raw metrics are visible
- **What this confirms:** TOUR-PLAN Topics 1–8 end-to-end in real code. User sees frames, turn management, VAD, interruptions in action.
- Document: `docs/02-local-transport.md` — what each pipeline stage does and which frame types flow through
- **Commit**

---

### M2 — WebSocket Transport + Browser UI
**Goal:** Same bot, now accessible from a browser tab.
- Upgrade to `WebsocketTransport` (FastAPI server, `/ws` endpoint)
- Minimal HTML/JS frontend: mic button, live transcript display, bot response text
- Uses `@pipecat-ai/client-js` with WebSocket transport
- No RTVI yet — raw WebSocket + serializer
- **What this teaches:** How `FrameSerializer` works, how browser audio becomes `AudioRawFrame`, WebSocket vs local transport tradeoffs
- Document: `docs/03-websocket-transport.md`
- **Commit**

---

### M3 — SmallWebRTC Transport
**Goal:** Replace WebSocket with WebRTC; use the prebuilt browser client.
- Upgrade server to `SmallWebRTCTransport` (`aiortc`)
- FastAPI `/api/offer` endpoint (SDP offer/answer signaling)
- Replace custom frontend with `pipecat-ai/small-webrtc-prebuilt` (npm package or clone)
- Same tech support bot underneath
- **What this teaches:** WebRTC vs WebSocket: media tracks, ICE/DTLS, why WebRTC has better voice quality and lower latency. References TOUR-PLAN Topic 5 (SmallWebRTC section).
- Document: `docs/04-webrtc-transport.md` — WebRTC vs WebSocket comparison table, architecture diff
- **Commit**

---

### M4 — Multi-Bot Call Transfer
**Goal:** Two personas; tech support bot can hand off to pizza bot mid-conversation.
- Add `bots/pizza/` — pizza ordering persona with function tools (add_item, remove_item, confirm_order)
- Tech support bot gets a `transfer_to_pizza()` LLM function tool
- Transfer mechanism: on function call, server starts new `PipelineTask` for pizza bot, closes tech support task, preserves WebRTC connection
- Use `pipecat-flows` for conversation state in pizza bot (menu navigation)
- Context summary passed to pizza bot ("user transferred from tech support")
- Use different TTS voice per persona: `nova` for tech support, `shimmer` for pizza
- **What this teaches:** Function calling E2E, pipeline lifecycle (start/stop), context hand-off via `LLMContextFrame`, pipecat-flows state machine. Confirms TOUR-PLAN Topic 11 (Advanced — function calling, custom processors).
- Document: `docs/05-call-transfer.md` — sequence diagram of transfer flow
- **Commit**

---

### M5 — Observability: Standard → Custom Metrics
**Goal:** Full observability stack, progressively from built-in to custom.

#### M5a — Pipecat Native Metrics (standard)
- Enable `MetricsFrame` logging with a `LoggingMetricsObserver`
- Display per-turn: TTFB (STT, LLM, TTS), token usage, TTS character count, E2E latency
- **Learning:** What MetricsData subclasses exist and when each is emitted

#### M5b — OpenTelemetry Traces
- Add `TurnTraceObserver` to `PipelineTask`
- Run **Jaeger** locally via Docker (`docker-compose up jaeger`)
- Configure OTLP exporter → Jaeger
- View per-turn spans: conversation → turn → STT/LLM/TTS sub-spans with TTFB attributes
- **Learning:** How observers tap into the pipeline, what `on_push_frame()` sees, span hierarchy

#### M5c — Custom Prometheus Metrics + Grafana Dashboard
- Write a `PrometheusMetricsObserver(BaseObserver)` — custom FrameProcessor that consumes `MetricsFrame` and exports:
  - `pipecat_ttfb_seconds{service="stt|llm|tts"}` (histogram)
  - `pipecat_llm_tokens_total{type="prompt|completion"}` (counter)
  - `pipecat_interruptions_total` (counter, custom — intercepts `InterruptionFrame`)
  - `pipecat_call_transfers_total` (counter, custom — intercepts function call result)
  - `pipecat_turn_duration_seconds` (histogram)
- Run **Prometheus + Grafana** via Docker Compose
- Build Grafana dashboard: TTFB p50/p95, tokens/min, interruption rate, transfer rate
- **Learning:** How to write a custom observer, Prometheus metric types, what's "standard" (built-in) vs "custom" (you instrument it)
- Document: `docs/06-observability.md` — metrics taxonomy (native vs custom), how to add new custom metrics
- **Commit**

---

### M5.5 — Provider Exploration (Swap & Measure)
**Goal:** Swap individual AI services (STT, LLM, TTS) one at a time while M5's Grafana dashboard is running, so every change has a measurable effect you can see.

**Why here:** You now have a working bot + metrics. Swapping a provider is a 3-line change, and the dashboard immediately shows you the latency impact. This is the most instructive context to compare providers — not as a setup exercise, but as a measurement exercise.

**Sequence:** Swap one service at a time. Keep the other two on OpenAI so you isolate the variable.

#### STT options (swap `OpenAISTTService`)
| Provider | Type | Cost | Notes |
|----------|------|------|-------|
| OpenAI Whisper / gpt-4o-transcribe | Commercial | Pay-per-use | Baseline |
| **Whisper.cpp** | Local / free | Free | Run via `pipecat[whisper]`. Higher TTFB but zero cost. |
| **Deepgram Nova-3** | Commercial | Free tier (12k min/yr) | Lowest TTFB of all STT options (~200ms). Pipecat has `DeepgramSTTService`. |
| AssemblyAI | Commercial | Pay-per-use | Good accuracy, slightly higher latency than Deepgram |

#### LLM options (swap `OpenAILLMService`)
| Provider | Type | Cost | Notes |
|----------|------|------|-------|
| OpenAI GPT-4o | Commercial | Pay-per-use | Baseline |
| **Ollama (Llama 3.2, Mistral)** | Local / free | Free | Run `ollama serve`. Pipecat has `OllamaLLMService`. High TTFB on CPU, fast on Apple Silicon. |
| **Anthropic Claude Haiku 4.5** | Commercial | Pay-per-use | Very low cost, fast TTFB. Pipecat has `AnthropicLLMService`. |
| Google Gemini Flash | Commercial | Free tier | Fast, multimodal. Pipecat has `GoogleLLMService`. |

#### TTS options (swap `OpenAITTSService`)
| Provider | Type | Cost | Notes |
|----------|------|------|-------|
| OpenAI TTS | Commercial | Pay-per-use | Baseline |
| **Coqui TTS (XTTS-v2)** | Local / free | Free | Run locally. Pipecat has `CoquiTTSService`. TTFB higher but offline. |
| **Cartesia Sonic** | Commercial | Free trial | Industry-lowest TTS TTFB (~80ms). Pipecat has `CartesiaTTSService`. |
| ElevenLabs | Commercial | Free tier (10k chars/mo) | High voice quality. Pipecat has `ElevenLabsTTSService`. |

**Commit after each working swap.**

---

### M6 — RTVI Protocol + Observers
**Goal:** Structured client-server events; UI knows when bot is speaking, transferring, etc.
- Add `RTVIProcessor` to both bot pipelines
- Frontend: upgrade to `@pipecat-ai/client-js` RTVI mode
- UI enhancements driven by RTVI events:
  - Speaking indicator (bot/user)
  - "Transferring to pizza bot…" notification
  - Live turn transcript via RTVI text events
- Implement a custom `BaseObserver` subclass that logs all frame transitions for debugging
- **What this covers:** TOUR-PLAN Topic 9 (Observers & RTVI) — final uncovered topic
- Document: `docs/07-rtvi-observers.md`
- **Commit**

---

## Task Tracking

| # | Milestone | Status |
|---|-----------|--------|
| M0 | Project Bootstrap | ✅ done |
| M1 | Local Transport Core Pipeline | ✅ done |
| M2 | WebSocket + Browser UI | ✅ done |
| M3 | SmallWebRTC Transport | ✅ done |
| M4 | Multi-Bot Call Transfer | ✅ done |
| M5a | Native Metrics Logging | ✅ done |
| M5b | OpenTelemetry + Jaeger | ✅ done |
| M5c | Prometheus + Grafana Custom Metrics | ✅ done |
| M5.5 | Provider Exploration (Swap & Measure) | ✅ done (guide) |
| M6 | RTVI + Observers | ✅ done |

---

## TOUR-PLAN Coverage Map

| Tour Topic | Status | Covered by Milestone |
|------------|--------|----------------------|
| 1–8 (core pipeline) | ✅ done | Confirmed & deepened by M1–M3 |
| 9 — Observers & RTVI | ⬜ | M6 |
| 10 — Metrics | ⬜ | M5a (native) + M5c (custom) |
| 11 — Advanced Topics | ⬜ | M4 (function calling, pipeline restart, pipecat-flows) |

---

## Key References

| Resource | URL / Path |
|----------|------------|
| Pipecat source | `/Users/marcelo.mendoza/dev/pipecat/` |
| SmallWebRTC transport | `src/pipecat/transports/smallwebrtc/transport.py` |
| SmallWebRTC prebuilt client | https://github.com/pipecat-ai/small-webrtc-prebuilt |
| OpenTelemetry example | https://github.com/pipecat-ai/pipecat-examples/tree/main/open-telemetry |
| pipecat-flows | https://github.com/pipecat-ai/pipecat-flows |
| Call transfer PR | https://github.com/pipecat-ai/pipecat/pull/1348 |
| RTVI client JS | https://github.com/pipecat-ai/pipecat-client-web |
| TurnTraceObserver | `src/pipecat/utils/tracing/turn_trace_observer.py` |
| MetricsData | `src/pipecat/metrics/metrics.py` |
| Voice switch example | `examples/foundational/15-switch-voices.py` |

---

## Learning Guide Structure

Each milestone produces a `docs/learning/MX-<name>.md` guide. Format mirrors the tour-docs: one canonical **stem** code snippet used throughout, then 3–5 progressively complex examples using that stem. Each example has a **Do / Observe / Understand** block.

The stem for all milestone guides is the same voice bot pipeline, with additions:

```python
# Stem (grows with each milestone — same bot, more layers)
pipeline = Pipeline([transport.input(), stt, user_agg, llm, tts, transport.output(), assistant_agg])
task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True))
runner = PipelineRunner()
await runner.run(task)
```

---

### M1 Learning Guide — Core Pipeline (Local Transport)

**File:** `docs/learning/M1-core-pipeline.md`

> **Provider note (informational — not changing anything yet):**
> Every service in this milestone uses OpenAI. The guide will include a sidebar listing what Pipecat-supported alternatives exist for each slot (STT, LLM, TTS), so you know the landscape before you see the code. You will try them in M5.5.

| # | Example | Do | Observe | Understand |
|---|---------|-----|---------|------------|
| 1 | Normal turn | Say "Hi, what can you do?" | Stdout logs showing `TranscriptionFrame` → `LLMTextFrame` tokens → `TTSAudioRawFrame` chunks | The pipeline is a typed frame stream. Each stage transforms one frame type into another. |
| 2 | Metric logging | After turn completes, look at logs | `MetricsFrame` with `TTFBMetricsData` for STT, LLM, and TTS. Notice LLM TTFB is longest. | TTFB per service is the key latency signal. STT TTFB = time until first word. LLM TTFB = time until first token. TTS TTFB = time until first audio chunk. |
| 3 | Interruption | Start speaking while bot is mid-response | Bot stops immediately. Log shows `InterruptionFrame` then `VADUserStartedSpeakingFrame`. | Dual-queue architecture: SystemFrames jump the priority queue. The bot doesn't wait to finish — queues are drained in ~1ms. |
| 4 | Fatal vs non-fatal error | Temporarily use a bad API key | `ErrorFrame` logged upstream, pipeline stays alive | `push_error(fatal=False)` lets the app decide. `fatal=True` would shut down the pipeline via `CancelFrame`. |
| 5 | Graceful shutdown | Say "goodbye", app detects end_conversation function call | `EndFrame` flows through every processor; each closes its service connection in order | One `StartFrame`/`EndFrame` pair = one session. Processors clean up in pipeline order. |

---

### M2 Learning Guide — WebSocket Transport

**File:** `docs/learning/M2-websocket.md`

Same 5 scenarios from M1, now run from the browser. **Compare** each result to M1.

| # | Example | Do | Observe | Understand |
|---|---------|-----|---------|------------|
| 1 | Same "Hi" via browser | Open browser, click mic, say "Hi" | Turn completes. Browser shows transcript. | WebSocket transport wraps audio bytes in a serializer frame before sending. Same pipeline, different transport boundary. |
| 2 | Inspect serializer | Open DevTools → Network → WS tab | Binary frames sent as Base64 or protobuf blobs; text frames as JSON | `FrameSerializer` converts `AudioRawFrame` → wire format. Required because WebSocket is byte-agnostic. |
| 3 | Latency comparison | Note perceived delay vs M1 local | Slight increase in TTFB (STT side) | Network round-trip added even on localhost. WebSocket buffer adds ~10–20ms. This is why WebRTC is preferred for voice. |
| 4 | Interruption over WebSocket | Same as M1 example 3 | Interruption still works, but you may hear a slight audio tail before silence | Transport output buffers ~40ms audio chunks (Pipecat default). InterruptionFrame drains the processor queue but not the OS audio buffer. |
| 5 | Disconnect and reconnect | Close browser tab and reopen | Server logs `EndFrame` on disconnect; `StartFrame` on next connection | Each browser session = one pipeline session. PipelineRunner handles reconnection by starting a fresh task. |

---

### M3 Learning Guide — SmallWebRTC Transport

**File:** `docs/learning/M3-webrtc.md`

Same stem, same conversation scenarios. Now with WebRTC. Compare to M2.

| # | Example | Do | Observe | Understand |
|---|---------|-----|---------|------------|
| 1 | Same "Hi" via SmallWebRTC | Open SmallWebRTC prebuilt UI, say "Hi" | Same transcript. Audio quality noticeably crisper than M2. | WebRTC uses OPUS codec at native sample rate. WebSocket had resampling overhead. |
| 2 | WebRTC internals | Open `chrome://webrtc-internals` during call | ICE candidates negotiated, DTLS handshake, OPUS audio track stats (bitrate, jitter, packets lost) | WebRTC transports audio as media tracks (not data channel bytes). ICE/DTLS happen before any voice frame enters Pipecat's pipeline. |
| 3 | Latency comparison | Note TTFB from M1/M2 logs vs M3 | STT TTFB lower or equal; audio perceived as more natural | WebRTC adaptive jitter buffer smooths audio. No base64 encoding overhead. Media track timing is hardware-aligned at 10ms chunks. |
| 4 | Signaling flow | Add logging to `/api/offer` endpoint | SDP offer from browser → server answers → ICE exchange → stream starts | SDP is the "contract" for media capabilities. SmallWebRTC uses HTTP POST for signaling (no external STUN/TURN on localhost). |
| 5 | Audio quality degradation | Add artificial packet loss via browser throttling | Voice becomes choppy; VAD may miss speech events | WebRTC has built-in FEC/PLC, but at high loss rates VAD fails. This is why production systems use TURN servers for NAT traversal and reliability. |

---

### M4 Learning Guide — Multi-Bot Call Transfer

**File:** `docs/learning/M4-call-transfer.md`

| # | Example | Do | Observe | Understand |
|---|---------|-----|---------|------------|
| 1 | Tech support normal turn | Ask "My laptop won't boot" | Standard turn. Note `LLMContext.messages` in logs. | Baseline: single-persona pipeline with function tools registered. |
| 2 | Trigger call transfer | Say "I'd like to order a pizza" | Logs show `FunctionCallInProgressFrame`, then `transfer_to_pizza()` result. New pipeline task starts. TTS voice changes (`nova` → `shimmer`). | Function calling is how the LLM drives application logic. The result callback starts a new `PipelineTask` and ends the current one. Context summary is injected as the first system message of the pizza bot. |
| 3 | Pizza bot multi-turn with state | Order "a margherita, add pepperoni, remove mushrooms, confirm" | `pipecat-flows` state machine transitions: `select_pizza` → `customize` → `confirm`. Each state change logs as a frame event. | `pipecat-flows` manages structured conversation state. Each state is a node with its own LLM prompt and exit conditions. |
| 4 | Interrupt during transfer handoff | Say "transfer me to pizza" then immediately say "wait, never mind" | Interruption fires. `FunctionCallResultFrame` is `UninterruptibleFrame` — it survives. Pizza bot still starts. | `UninterruptibleFrame` keeps the context consistent even through interruptions. |
| 5 | Inspect context at handoff | Log `LLMContext.messages` at transfer point | Tech support context serialized to JSON, pizza bot receives it as first `system` message. | Context hand-off is explicit. The receiving bot gets a summary, not the full history. |

---

### M5 Learning Guide — Observability (Standard → Custom)

**File:** `docs/learning/M5-observability.md`

| # | Example | Do | Observe | Understand |
|---|---------|-----|---------|------------|
| 1 | Native MetricsFrame log | Enable `enable_metrics=True`, run a turn, check stdout | Log line: `MetricsFrame[TTFBMetricsData(processor=stt, model=gpt-4o-transcribe, value=0.42s)]` | Pipecat emits metrics as frames — they flow downstream like data. |
| 2 | OpenTelemetry traces in Jaeger | `docker compose up jaeger`, configure OTLP exporter, run 3 turns | Jaeger UI shows: conversation span → turn spans → STT/LLM/TTS child spans. | `TurnTraceObserver` is a `BaseObserver`. It watches `on_push_frame()` events non-blocking. |
| 3 | Compare TTFB across turns | Run 5 turns with different question complexity | STT TTFB stable. LLM TTFB varies (0.5–2s). TTS TTFB stable. | LLM is the variable. STT/TTS are latency-bound by model inference, roughly constant per token. |
| 4 | Custom Prometheus counter | Add `pipecat_interruptions_total` counter | Prometheus shows counter incrementing on each interruption. | Custom metrics require writing a `BaseObserver` that intercepts specific frame types. |
| 5 | Full Grafana dashboard | Add panels: TTFB histogram, token rate, interruption rate, transfer count | Dashboard shows all 4 metrics in real time. | The progression: native → OTel traces → Prometheus counters → Grafana visualization. |

---

### M5.5 Learning Guide — Provider Exploration

**File:** `docs/learning/M5.5-providers.md`

| # | Example | Do | Observe | Understand |
|---|---------|-----|---------|------------|
| 1 | Swap STT: Deepgram Nova-3 | Replace `OpenAISTTService` with `DeepgramSTTService` | STT TTFB drops from ~420ms to ~180ms on Grafana histogram. | TTFB is provider-specific. Deepgram is streaming-native; OpenAI Whisper is batch-then-respond. |
| 2 | Swap STT: local Whisper.cpp | Replace with `WhisperSTTService` (local) | STT TTFB jumps to 800ms–2s. Zero API cost. | Local inference trades latency for cost. |
| 3 | Swap LLM: Ollama (Llama 3.2) | `ollama pull llama3.2`, replace with `OllamaLLMService` | LLM TTFB increases vs GPT-4o. Token rate visible in Grafana. | Local LLM = no data leaves your machine. TTFB depends on hardware. |
| 4 | Swap TTS: Cartesia Sonic | Replace `OpenAITTSService` with `CartesiaTTSService` | TTS TTFB drops to ~80ms. | Cartesia streams at the phoneme level. |
| 5 | Full local stack | Whisper.cpp + Ollama + Coqui XTTS | E2E latency visible on dashboard. | A fully local voice bot is possible but 3–5x higher latency on typical hardware. |

---

### M6 Learning Guide — RTVI + Observers

**File:** `docs/learning/M6-rtvi-observers.md`

| # | Example | Do | Observe | Understand |
|---|---------|-----|---------|------------|
| 1 | Bot speaking indicator | Add `RTVIProcessor`, open browser, watch UI | Speaking indicator activates exactly when bot audio starts. | `RTVIProcessor` watches `BotStartedSpeakingFrame` / `BotStoppedSpeakingFrame` via `on_push_frame()`. |
| 2 | Transfer notification in UI | Trigger call transfer | Browser shows "Transferring to pizza bot…" notification. | Custom RTVI message sent at transfer point. Client's `on(RTVIEvent.Custom)` handler receives it. |
| 3 | Write a debug observer | Implement `BaseObserver.on_push_frame()` that logs every frame type | Stdout shows every frame, processor name, and direction. | Observers are non-intrusive pipeline taps. They can't block frames. |
| 4 | Compare observer vs processor | Move debug logger into a FrameProcessor | Logger adds latency. Observer version: zero latency. | Observers run in their own async task. Only add logic to the frame path when you need to transform frames. |

---

## Verification (per milestone)

- **M1:** `python bots/tech-support/local_bot.py` → speak into mic → bot responds, interruption works, MetricsFrame logs in stdout
- **M2:** `uvicorn bots/tech-support/server:app` + open `frontend/index.html` → voice works in browser
- **M3:** Same but `SmallWebRTCTransport`, verify audio quality vs M2
- **M4:** Say "transfer me to order pizza" → bot switches persona mid-call, context summary visible in logs
- **M5a/b:** Turn logs show TTFB values; Jaeger UI shows span tree at `localhost:16686`
- **M5c:** `localhost:9090` Prometheus scrapes metrics; Grafana at `localhost:3000` shows dashboard
- **M6:** Browser UI shows speaking indicator, transfer notification — driven by RTVI events, not polling
