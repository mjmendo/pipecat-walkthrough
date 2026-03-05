# Pipecat Walkthrough

A hands-on learning project building a **multi-persona AI voice call center** with [Pipecat](https://github.com/pipecat-ai/pipecat). Two bots — tech support and pizza ordering — run on a shared WebRTC connection and can transfer calls between each other, with full observability (traces, metrics, dashboards).

---

## Start here

### 1. Clone Pipecat as a sibling directory

This project runs Pipecat from source (required — the pip package is behind).

```bash
cd ~/dev   # or wherever this repo lives
git clone https://github.com/pipecat-ai/pipecat.git
# Result: ~/dev/pipecat  ← sibling of ~/dev/pipecat-walkthrough
```

### 2. Install system dependencies

```bash
brew install portaudio   # required for M1 mic/speaker audio
```

### 3. Create a virtual environment and install

```bash
cd pipecat-walkthrough
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[observability]"
```

### 4. Add your API key

```bash
cp .env.example .env
# Edit .env and set OPENAI_API_KEY=sk-...
```

### 5. Run M1 — your first voice bot

```bash
python bots/tech-support/local_bot.py
# Speak into your mic. The bot responds as Alex, a tech support agent.
# Press Ctrl+C to quit.
```

Then open `docs/learning/M1-core-pipeline.md` and work through its 5 examples to understand what just happened inside the pipeline.

---

## Milestones — run in order

Each milestone adds one layer. The bot core (STT → LLM → TTS) never changes — only the transport and observability wrappers grow.

| # | Run this | Then read |
|---|----------|-----------|
| M1 | `python bots/tech-support/local_bot.py` | `docs/learning/M1-core-pipeline.md` |
| M2 | `python bots/tech-support/ws_server.py` → open `frontend/websocket.html` | `docs/learning/M2-websocket.md` |
| M3 | `python bots/tech-support/webrtc_server.py` → open `http://localhost:7860` | `docs/learning/M3-webrtc.md` |
| M4 | `python bots/tech-support/transfer_server.py` → open `http://localhost:7860` | `docs/learning/M4-call-transfer.md` |
| M5 | `./scripts/start-infra.sh` then `python bots/tech-support/observability_server.py` | `docs/learning/M5-observability.md` |
| M5.5 | (modify `observability_server.py` per guide, keep Grafana open) | `docs/learning/M5.5-providers.md` |
| M6 | `python bots/tech-support/rtvi_server.py` → open `http://localhost:7860` | `docs/learning/M6-rtvi-observers.md` |

**M5 requires Docker** for Jaeger + Prometheus + Grafana. Run `./scripts/start-infra.sh` once — it starts all three. Dashboards:

- Jaeger (traces): http://localhost:16686
- Grafana (metrics): http://localhost:3000 — login: `admin / admin`
- Prometheus: http://localhost:9090

---

## What each milestone teaches

| Milestone | What it builds | Key concepts |
|-----------|----------------|--------------|
| M1 — Local Pipeline | Voice bot via mic/speaker | Frame types, VAD, STT→LLM→TTS, MetricsFrame |
| M2 — WebSocket | Same bot from a browser tab | FrameSerializer, WebSocket transport |
| M3 — WebRTC | WebRTC audio via aiortc | SmallWebRTCTransport, SDP signaling, OPUS codec |
| M4 — Call Transfer | Two personas, one connection | Function calling, pipeline restart, pipecat-flows |
| M5 — Observability | Logs + traces + Grafana | BaseObserver, OpenTelemetry/Jaeger, Prometheus |
| M5.5 — Providers | Swap STT/LLM/TTS, measure TTFB | Deepgram, Ollama, Cartesia, Whisper.cpp, Coqui |
| M6 — RTVI | Structured browser events | RTVIProcessor, RTVIObserver, custom server messages |

---

## Project structure

```
pipecat-walkthrough/
├── bots/
│   ├── shared/          # Observers, context utils, debug tools
│   ├── tech-support/    # Alex persona — one server per milestone
│   └── pizza/           # Marco persona — pipecat-flows ordering state machine
├── docs/
│   ├── learning/        # Per-milestone Do/Observe/Understand guides
│   └── *.md             # Architecture and component reference
├── frontend/            # Browser clients (WebSocket + WebRTC)
├── infra/               # Docker Compose: Jaeger + Prometheus + Grafana
└── scripts/             # start-infra.sh
```

## Reference docs

- `docs/01-architecture.md` — full system overview and component map
- `docs/02-local-transport.md` through `docs/07-rtvi-observers.md` — per-milestone deep-dives
