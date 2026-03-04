# M5 — Observability: Standard → Custom Metrics

## Three Layers of Observability

| Layer | Tool | What you see | When to use |
|-------|------|-------------|-------------|
| M5a — Native metrics | `LoggingMetricsObserver` (stdout) | TTFB, tokens per turn | Debugging a single run |
| M5b — OTel traces | `TurnTraceObserver` → Jaeger | Span hierarchy, turn duration, interruption flag | Diagnosing latency, tracing requests |
| M5c — Prometheus | `PrometheusMetricsObserver` → Grafana | Time-series, rates, dashboards | Production monitoring, alerting |

---

## M5a: Native MetricsFrame Logging

Already done in M1. `LoggingMetricsObserver` in `bots/shared/observers.py`.

Every `MetricsFrame` that flows through the pipeline is intercepted and logged:
```
[METRICS] TTFB | processor=OpenAISTTService model=gpt-4o-transcribe value=0.42s
[METRICS] TTFB | processor=OpenAILLMService model=gpt-4o value=0.87s
[METRICS] TTFB | processor=OpenAITTSService model=gpt-4o-mini-tts value=0.31s
[METRICS] LLM tokens | prompt=142 completion=67 total=209
[METRICS] TTS chars | characters=284
```

**What are MetricsFrame?**
`MetricsFrame` is a `SystemFrame` subtype. It bypasses the data queue and goes directly to observers. Services emit them at specific points:
- `TTFBMetricsData`: emitted when the first byte of response arrives (STT/LLM/TTS)
- `LLMUsageMetricsData`: emitted after LLM response completes (token counts)
- `TTSUsageMetricsData`: emitted after TTS response (character count)

---

## M5b: OpenTelemetry Traces → Jaeger

### Setup
```bash
./scripts/start-infra.sh
# or: docker compose -f infra/docker-compose.yml up jaeger -d

python bots/tech-support/observability_server.py
# Open http://localhost:16686 → Jaeger UI
```

### Span Hierarchy
```
conversation (span)
└── turn-1 (span)
    ├── stt: transcribe (span) — TTFB attribute
    ├── llm: gpt-4o (span) — TTFB + token attributes
    └── tts: nova (span) — TTFB attribute
└── turn-2 (span, was_interrupted=true)
    ├── stt: transcribe (span)
    └── llm: gpt-4o (span) ← interrupted before TTS
```

### How TurnTraceObserver Works

`TurnTraceObserver` depends on two other observers:
- `TurnTrackingObserver`: detects turn start/end via frame types
- `UserBotLatencyObserver`: measures user speech → bot response time

These three work as a composition chain. Each is a `BaseObserver` registered with the `PipelineTask`.

**All three must be in `task.observers=[]` for spans to appear in Jaeger.**

### OTLP → Jaeger Configuration

Jaeger's `all-in-one` image accepts OTLP gRPC on port 4317:
```python
# In observability_server.py
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
exporter = OTLPSpanExporter(endpoint="http://localhost:4317", insecure=True)
setup_tracing("pipecat-walkthrough", exporter=exporter)
```

---

## M5c: Custom Prometheus Metrics → Grafana

### Metrics Taxonomy

| Metric | Type | Standard? | Source |
|--------|------|-----------|--------|
| `pipecat_ttfb_seconds` | Histogram | Built-in data | `TTFBMetricsData` from `MetricsFrame` |
| `pipecat_llm_tokens_total` | Counter | Built-in data | `LLMUsageMetricsData` |
| `pipecat_tts_characters_total` | Counter | Built-in data | `TTSUsageMetricsData` |
| `pipecat_interruptions_total` | Counter | **Custom** | `InterruptionFrame` detected by observer |
| `pipecat_call_transfers_total` | Counter | **Custom** | Explicitly called in transfer handler |
| `pipecat_turns_total` | Counter | Semi-custom | Inferred from `LLMUsageMetricsData` |

**Standard metrics** come from data already in `MetricsFrame` — we just re-emit them to Prometheus.
**Custom metrics** require us to watch for specific frames (`InterruptionFrame`) or call `record_transfer()` explicitly.

### Setup
```bash
./scripts/start-infra.sh

python bots/tech-support/observability_server.py
# Metrics at http://localhost:8000/metrics
# Grafana at http://localhost:3000 (admin/admin)
```

Grafana dashboard `Pipecat Voice Bot` auto-loads from `infra/grafana/dashboards/pipecat.json`.

### Adding a New Custom Metric

1. Define the metric in `bots/shared/prometheus_observer.py`:
   ```python
   MY_NEW_COUNTER = Counter("pipecat_my_event_total", "Description")
   ```

2. Add detection in `on_push_frame()`:
   ```python
   elif isinstance(frame, MySpecificFrame):
       MY_NEW_COUNTER.inc()
   ```

3. Add a Grafana panel referencing `pipecat_my_event_total`.

That's the full pattern for any new custom metric.

---

## How to Run All Layers Together

```bash
# Terminal 1: start infra
./scripts/start-infra.sh

# Terminal 2: run bot with full observability
python bots/tech-support/observability_server.py

# Dashboards:
# - Stdout: see [METRICS] lines immediately
# - Jaeger: http://localhost:16686 (spans appear after each turn)
# - Grafana: http://localhost:3000 (metrics update every 5s)
```
