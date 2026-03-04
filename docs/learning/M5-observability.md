# M5 Learning Guide — Observability (Standard → Custom)

## The Stem (unchanged pipeline + observers added)

```python
task = PipelineTask(
    pipeline,
    params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    observers=[
        LoggingMetricsObserver(),        # M5a: stdout
        PrometheusMetricsObserver(),     # M5c: Prometheus
        turn_tracker,                    # M5b: dependency
        latency_tracker,                 # M5b: dependency
        TurnTraceObserver(turn_tracker, latency_tracker),  # M5b: Jaeger
    ],
)
```

Observers are the only addition. The pipeline is unchanged from M4.

---

## Example 1 — Native MetricsFrame Log

**Do:** Run `observability_server.py`. Speak a short question (< 10 words).

**Observe in stdout:**
```
HH:MM:SS | INFO    | [METRICS] TTFB | processor=OpenAISTTService model=gpt-4o-transcribe value=0.41s
HH:MM:SS | INFO    | [METRICS] TTFB | processor=OpenAILLMService model=gpt-4o value=0.73s
HH:MM:SS | INFO    | [METRICS] TTFB | processor=OpenAITTSService model=gpt-4o-mini-tts value=0.28s
HH:MM:SS | INFO    | [METRICS] LLM tokens | prompt=142 completion=57 total=199
HH:MM:SS | INFO    | [METRICS] TTS chars | characters=231
```

**Understand:**
Pipecat emits `MetricsFrame` as a `SystemFrame` — it bypasses the data queue and goes directly to observers. `LoggingMetricsObserver.on_push_frame()` checks `isinstance(data.frame, MetricsFrame)` and logs each metric.

This is not polling or aggregation — it's event-driven. The log appears within milliseconds of the service emitting the data.

Key insight: LLM TTFB is almost always the highest. It grows with prompt complexity and output length.

---

## Example 2 — OpenTelemetry Traces in Jaeger

**Do:**
1. `docker compose -f infra/docker-compose.yml up jaeger -d`
2. `python bots/tech-support/observability_server.py`
3. Open `http://localhost:16686` (Jaeger UI)
4. Do 3 conversation turns (including one interruption)
5. In Jaeger: search for service `pipecat-walkthrough`, look at traces

**Observe:**
- Each turn is a span with child spans for STT, LLM, TTS
- The interrupted turn has `turn.was_interrupted=true` attribute
- Turn duration attribute shows total turn time including STT wait
- `conversation.id` links all turns from one session

**Understand:**
`TurnTraceObserver` listens to `TurnTrackingObserver` events:
- `on_turn_started(turn_number)` → creates a new OTel span
- `on_turn_ended(turn_number, duration, was_interrupted)` → ends the span

Service spans (STT/LLM/TTS) are created by pipecat's internal tracing decorators (`@traced_stt`, `@traced_llm`). They use the tracing context set by `TurnTraceObserver` to become child spans of the turn span.

This parent-child hierarchy is what makes traces useful: you can see exactly which service added latency within a turn.

---

## Example 3 — Compare TTFB Across Turns

**Do:** Run 5 turns with different question lengths:
1. "Hi" (short)
2. "What can you help me with?" (medium)
3. "Can you explain the difference between WiFi 5 and WiFi 6, and when I should upgrade?" (long)
4. "OK thanks" (short)
5. "What are the most common causes of slow internet at home?" (medium)

**Observe in Jaeger:** Compare LLM span durations across turns.

**Observe from TTFB logs:**
```
Turn 1: STT=0.38s  LLM=0.52s  TTS=0.27s
Turn 2: STT=0.41s  LLM=0.68s  TTS=0.29s
Turn 3: STT=0.44s  LLM=1.23s  TTS=0.32s  ← longer prompt, longer TTFB
Turn 4: STT=0.35s  LLM=0.48s  TTS=0.26s
Turn 5: STT=0.42s  LLM=0.81s  TTS=0.31s
```

**Understand:**
- **STT TTFB**: stable (~0.4s). Bounded by audio duration + model inference. Question length doesn't matter much.
- **LLM TTFB**: variable (0.5s – 2s+). Grows with prompt complexity. The model thinks before generating the first token.
- **TTS TTFB**: stable (~0.3s). Bounded by sentence assembly + API round-trip.

LLM TTFB is the primary lever for improving perceived latency. Smaller models (Haiku, Gemini Flash) have lower TTFB. This is what you'll measure in M5.5 when swapping providers.

---

## Example 4 — Custom Prometheus Counter (Interruptions)

**Do:**
1. `docker compose -f infra/docker-compose.yml up prometheus grafana -d`
2. Start `observability_server.py`
3. Open Grafana at `http://localhost:3000` → Dashboards → "Pipecat Voice Bot"
4. Trigger 3-4 interruptions (speak while bot is responding)
5. Watch the "Interruptions" stat panel

**Observe:** Counter increments in Grafana every time you interrupt.

**Understand:**
`PrometheusMetricsObserver.on_push_frame()` watches for `InterruptionFrame`:

```python
elif isinstance(frame, InterruptionFrame):
    INTERRUPTIONS_COUNTER.inc()
```

`InterruptionFrame` is a `SystemFrame` — it passes through ALL observers, including ours.

This is a **custom metric**: Pipecat doesn't export interruption counts by default. We instrument it by watching for the specific frame type in our observer.

Compare to TTFB: TTFB data is in `MetricsFrame` (standard). Interruption count is not — we add it.

---

## Example 5 — Full Grafana Dashboard

**Do:** Have a 5-minute conversation including:
- 3 normal turns
- 2 interruptions
- 1 call transfer to pizza

**Observe in Grafana (`http://localhost:3000`):**
- TTFB p50/p95 panels: per-service latency history
- LLM Tokens/min panel: token rate with prompt/completion breakdown
- Interruptions counter: total = 2
- Call Transfers counter: total = 1
- Turns counter: total = 4 (3 tech support + pizza greeting)

**Understand:**
The observability progression:
1. **LoggingMetricsObserver (M5a)**: text log, ephemeral, one run at a time
2. **TurnTraceObserver (M5b)**: structured spans, request-level detail, query by conversation ID
3. **PrometheusMetricsObserver (M5c)**: time-series data, aggregate rates, visual trends

Each layer serves a different operational need. Logs for debugging, traces for attribution, metrics for trends. In production, you'd have all three.

Now that you have the dashboard running, you're ready for **M5.5**: swap providers one at a time and watch the TTFB histograms change in real time.
