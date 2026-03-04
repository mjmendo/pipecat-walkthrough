# Pipecat Walkthrough

A hands-on learning project building a **multi-persona AI voice call center** with [Pipecat](https://github.com/pipecat-ai/pipecat). Two bots — tech support and pizza ordering — run on a shared WebRTC connection and can transfer calls between each other, with full observability (traces, metrics, dashboards).

## What's inside

| Milestone | What it builds | Key concepts |
|-----------|---------------|--------------|
| M1 — Local Pipeline | Voice bot via mic/speaker | Frame types, VAD, STT→LLM→TTS, MetricsFrame |
| M2 — WebSocket | Same bot from a browser tab | FrameSerializer, WebSocket transport |
| M3 — WebRTC | WebRTC audio via aiortc | SmallWebRTCTransport, SDP signaling, OPUS codec |
| M4 — Call Transfer | Two personas, one connection | Function calling, pipeline restart, pipecat-flows state machine |
| M5 — Observability | Logs + traces + Grafana | BaseObserver, OpenTelemetry/Jaeger, Prometheus, custom metrics |
| M5.5 — Providers | Swap STT/LLM/TTS, measure impact | Deepgram, Ollama, Cartesia, Whisper.cpp, Coqui |
| M6 — RTVI | Structured browser events | RTVIProcessor, RTVIObserver, custom server messages |

## Project structure

```
pipecat-walkthrough/
├── bots/
│   ├── shared/          # Observers, context utils, debug tools
│   ├── tech-support/    # Alex persona — local, WebSocket, WebRTC, RTVI servers
│   └── pizza/           # Marco persona — pipecat-flows ordering state machine
├── docs/
│   ├── learning/        # Per-milestone Do/Observe/Understand guides
│   └── *.md             # Architecture and component reference docs
├── frontend/            # Browser clients (WebSocket + WebRTC)
├── infra/               # Docker Compose: Jaeger + Prometheus + Grafana
└── scripts/             # start-infra.sh
```

## Prerequisites

- Python 3.11+
- OpenAI API key
- Docker (for M5 observability stack)
- `portaudio` (for M1 local audio): `brew install portaudio`

```bash
cp .env.example .env
# add OPENAI_API_KEY to .env

pip install -e ".[observability]"
```

> This project installs Pipecat from a local source checkout at `../pipecat`. Clone [pipecat-ai/pipecat](https://github.com/pipecat-ai/pipecat) as a sibling directory first.

## Running each milestone

```bash
# M1 — terminal voice bot
python bots/tech-support/local_bot.py

# M2 — WebSocket browser client
uvicorn bots.tech-support.ws_server:app --port 8765
open frontend/websocket.html

# M3 — WebRTC (prebuilt browser UI)
python bots/tech-support/webrtc_server.py
# open http://localhost:7860

# M4 — multi-bot call transfer
python bots/tech-support/transfer_server.py
# open http://localhost:7860

# M5 — full observability stack
./scripts/start-infra.sh
python bots/tech-support/observability_server.py
# Jaeger:    http://localhost:16686
# Grafana:   http://localhost:3000  (admin/admin)
# Prometheus: http://localhost:9090

# M6 — RTVI protocol + observers
python bots/tech-support/rtvi_server.py
# open http://localhost:7860
```

## Learning guides

Each milestone has a guide in `docs/learning/` following a **Do / Observe / Understand** format — run the bot, observe the output, then read the explanation of what Pipecat did internally.

- `M1-core-pipeline.md` — frame flow, interruptions, metrics, graceful shutdown
- `M2-websocket.md` — serializer, latency comparison vs local
- `M3-webrtc.md` — OPUS codec, ICE/DTLS, audio quality vs WebSocket
- `M4-call-transfer.md` — function calling, context hand-off, pipecat-flows
- `M5-observability.md` — MetricsFrame, OTel spans, Prometheus counters, Grafana
- `M5.5-providers.md` — provider swap guide with TTFB measurement context
- `M6-rtvi-observers.md` — RTVI events, observer vs processor latency tradeoff

## Architecture docs

- `docs/01-architecture.md` — system overview and component map
- `docs/02-local-transport.md` through `docs/07-rtvi-observers.md` — per-milestone reference
