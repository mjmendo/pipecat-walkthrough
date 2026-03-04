# Architecture Overview

## System: Multi-Persona AI Voice Call Center

A learning project that builds progressively from a simple local voice bot to a full multi-persona WebRTC call center with observability. Each milestone adds one layer.

---

## Component Map (Final State — after all milestones)

```
Browser (WebRTC)
    │  SDP offer/answer (HTTP POST /api/offer)
    │  Media track: OPUS audio (bidirectional)
    ▼
FastAPI Server
    ├── /api/offer     — SmallWebRTC signaling
    └── /api/transfer  — internal call transfer trigger
         │
         ├── PipelineTask: Tech Support Bot
         │     SmallWebRTCTransport.input()
         │       → SileroVADAnalyzer           (VAD: silence detection)
         │       → OpenAISTTService            (gpt-4o-transcribe → TranscriptionFrame)
         │       → LLMUserResponseAggregator   (text accumulation)
         │       → OpenAILLMService            (gpt-4o → LLMTextFrame + function calls)
         │       → OpenAITTSService            (nova voice → TTSAudioRawFrame)
         │     SmallWebRTCTransport.output()
         │       → LLMAssistantResponseAggregator
         │
         └── PipelineTask: Pizza Bot  (activated on transfer)
               SmallWebRTCTransport.input()
                 → SileroVADAnalyzer
                 → OpenAISTTService
                 → LLMUserResponseAggregator
                 → pipecat-flows state machine  (menu navigation)
                 → OpenAILLMService
                 → OpenAITTSService            (shimmer voice)
               SmallWebRTCTransport.output()
                 → LLMAssistantResponseAggregator

Observability (M5):
    ├── TurnTraceObserver  → OTLP → Jaeger (localhost:16686)
    └── PrometheusMetricsObserver → Prometheus (localhost:9090) → Grafana (localhost:3000)
```

---

## Frame Flow (single turn)

```
[mic audio]
     ↓ AudioRawFrame (10ms chunks)
SileroVADAnalyzer
     ↓ VADUserStartedSpeakingFrame / VADUserStoppedSpeakingFrame
OpenAISTTService
     ↓ TranscriptionFrame (text)
LLMUserResponseAggregator
     ↓ LLMMessagesFrame (assembled context)
OpenAILLMService
     ↓ LLMTextFrame (streaming tokens)  +  FunctionCallInProgressFrame (if tool call)
OpenAITTSService
     ↓ TTSAudioRawFrame (PCM chunks)
[speaker output]
```

**System frames** (bypass queue, handled immediately):
- `StartFrame` / `EndFrame` — session lifecycle
- `CancelFrame` — immediate stop
- `InterruptionFrame` — user spoke mid-response; drain output queue

---

## Transport Evolution (per milestone)

| Milestone | Transport | Why |
|-----------|-----------|-----|
| M1 | LocalTransport (PyAudio) | Simplest — no network, proves pipeline works |
| M2 | WebsocketTransport + FastAPI | Browser accessible; introduces FrameSerializer |
| M3 | SmallWebRTCTransport (aiortc) | Production-grade voice; lower latency, OPUS codec |

---

## Multi-Bot Architecture (M4)

Two independent `PipelineTask` instances share the same `SmallWebRTCTransport`. Only one pipeline is active at a time. Transfer:

1. Tech support LLM calls `transfer_to_pizza()` function tool
2. Result callback: cancel tech support `PipelineTask`
3. Create and start new pizza `PipelineTask` on same transport
4. Pizza bot receives context summary as first system message

---

## Directory Structure

```
pipecat-walkthrough/
├── docs/
│   ├── PLAN.md                  # This project's master plan
│   ├── TOUR-PLAN.md             # Original Pipecat tour plan
│   ├── 01-architecture.md       # This file
│   ├── 02-local-transport.md    # Added in M1
│   ├── 03-websocket-transport.md # Added in M2
│   ├── 04-webrtc-transport.md   # Added in M3
│   ├── 05-call-transfer.md      # Added in M4
│   ├── 06-observability.md      # Added in M5
│   ├── 07-rtvi-observers.md     # Added in M6
│   └── learning/
│       ├── M1-core-pipeline.md
│       ├── M2-websocket.md
│       ├── M3-webrtc.md
│       ├── M4-call-transfer.md
│       ├── M5-observability.md
│       ├── M5.5-providers.md
│       └── M6-rtvi-observers.md
├── bots/
│   ├── shared/
│   │   ├── pipeline.py          # Base pipeline builder (added M1)
│   │   ├── observers.py         # Logging + metrics observers (added M5)
│   │   └── context.py           # Context summarization utils (added M4)
│   ├── tech-support/
│   │   ├── local_bot.py         # M1 — terminal bot
│   │   ├── server.py            # M2+ — FastAPI server
│   │   └── persona.py           # System prompt + tools
│   └── pizza/
│       ├── persona.py           # System prompt + flow definition
│       └── flows.py             # pipecat-flows state machine (M4)
├── frontend/
│   ├── index.html               # M2 WebSocket UI
│   └── webrtc.html              # M3+ WebRTC UI
├── infra/
│   ├── docker-compose.yml       # Jaeger + Prometheus + Grafana
│   └── grafana/dashboards/
│       └── pipecat.json         # Pre-built dashboard (M5c)
└── scripts/
    ├── start-infra.sh           # docker compose up
    └── dev.sh                   # Start bot + infra together
```

---

## Key Pipecat Concepts Referenced

| Concept | Where in code | Milestone |
|---------|---------------|-----------|
| `Pipeline` | `bots/shared/pipeline.py` | M1 |
| `PipelineTask` + `PipelineRunner` | `bots/tech-support/local_bot.py` | M1 |
| `PipelineParams(enable_metrics=True)` | All bots | M1 |
| `SileroVADAnalyzer` | All pipelines | M1 |
| `FrameSerializer` | `bots/tech-support/server.py` | M2 |
| `SmallWebRTCTransport` | `bots/tech-support/server.py` | M3 |
| `BaseObserver` subclass | `bots/shared/observers.py` | M5 |
| `TurnTraceObserver` | `bots/tech-support/server.py` | M5b |
| `pipecat-flows` | `bots/pizza/flows.py` | M4 |
| `RTVIProcessor` | All pipelines | M6 |
