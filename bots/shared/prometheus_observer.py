"""
PrometheusMetricsObserver — M5c custom metrics observer.

Exports Pipecat pipeline metrics as Prometheus metrics:
- pipecat_ttfb_seconds{service}         HISTOGRAM — per-service TTFB
- pipecat_llm_tokens_total{type}        COUNTER   — LLM token usage
- pipecat_tts_characters_total          COUNTER   — TTS characters synthesized
- pipecat_interruptions_total           COUNTER   — user interrupted the bot
- pipecat_call_transfers_total          COUNTER   — call transfers completed
- pipecat_turns_total                   COUNTER   — total conversation turns

Usage:
    from bots.shared.prometheus_observer import PrometheusMetricsObserver, start_metrics_server

    # Start HTTP server for Prometheus to scrape (default port 8000)
    start_metrics_server(port=8000)

    # Add observer to PipelineTask
    task = PipelineTask(pipeline, observers=[PrometheusMetricsObserver()])

Learning note (M5c):
    This is a BaseObserver subclass that intercepts MetricsFrame (for TTFB/tokens)
    and InterruptionFrame (for interruption count).

    Key insight: custom metrics require CUSTOM INSTRUMENTATION — we write code to
    emit them. They don't appear automatically. "Standard" metrics (TTFB, tokens)
    are built into Pipecat; "custom" metrics (interruptions, transfers) need this class.

    The observer runs in its own async task (non-blocking). All prometheus_client
    operations are thread-safe.
"""

from prometheus_client import Counter, Histogram, start_http_server

from pipecat.frames.frames import InterruptionFrame, MetricsFrame
from pipecat.metrics.metrics import LLMUsageMetricsData, TTFBMetricsData, TTSUsageMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed

# ── Metric definitions ─────────────────────────────────────────────────────

TTFB_HISTOGRAM = Histogram(
    "pipecat_ttfb_seconds",
    "Time to first byte per service",
    labelnames=["service"],
    buckets=[0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0],
)

LLM_TOKENS_COUNTER = Counter(
    "pipecat_llm_tokens_total",
    "LLM token usage",
    labelnames=["type"],  # prompt | completion | total
)

TTS_CHARS_COUNTER = Counter(
    "pipecat_tts_characters_total",
    "TTS characters synthesized",
)

INTERRUPTIONS_COUNTER = Counter(
    "pipecat_interruptions_total",
    "Number of times user interrupted the bot mid-response",
)

CALL_TRANSFERS_COUNTER = Counter(
    "pipecat_call_transfers_total",
    "Number of call transfers (tech support → pizza)",
)

TURNS_COUNTER = Counter(
    "pipecat_turns_total",
    "Total conversation turns completed",
)


def start_metrics_server(port: int = 8000) -> None:
    """Start the Prometheus HTTP server on the given port.

    Call once at server startup. Prometheus scrapes /metrics at this port.
    """
    start_http_server(port)


class PrometheusMetricsObserver(BaseObserver):
    """Observer that exports pipeline metrics to Prometheus.

    Watches for:
    - MetricsFrame → TTFB, token usage, TTS character count
    - InterruptionFrame → interruption counter
    - (call transfer counter incremented externally via record_transfer())

    Thread safety: prometheus_client metrics are thread-safe.
    Async safety: on_push_frame runs in observer's own task (non-blocking).
    """

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame

        if isinstance(frame, MetricsFrame):
            for metric in frame.data:
                if isinstance(metric, TTFBMetricsData):
                    # Normalize processor name to service label
                    service = self._processor_to_service(metric.processor)
                    TTFB_HISTOGRAM.labels(service=service).observe(metric.value)

                elif isinstance(metric, LLMUsageMetricsData):
                    usage = metric.value
                    LLM_TOKENS_COUNTER.labels(type="prompt").inc(usage.prompt_tokens)
                    LLM_TOKENS_COUNTER.labels(type="completion").inc(usage.completion_tokens)
                    LLM_TOKENS_COUNTER.labels(type="total").inc(usage.total_tokens)
                    TURNS_COUNTER.inc()

                elif isinstance(metric, TTSUsageMetricsData):
                    TTS_CHARS_COUNTER.inc(metric.value)

        elif isinstance(frame, InterruptionFrame):
            INTERRUPTIONS_COUNTER.inc()

    @staticmethod
    def record_transfer() -> None:
        """Call this when a call transfer completes.

        Not tied to a specific frame — called explicitly in the transfer handler.

        Usage:
            PrometheusMetricsObserver.record_transfer()
        """
        CALL_TRANSFERS_COUNTER.inc()

    @staticmethod
    def _processor_to_service(processor_name: str) -> str:
        """Map processor class name to a short service label."""
        name_lower = processor_name.lower()
        if "stt" in name_lower or "transcrib" in name_lower or "whisper" in name_lower:
            return "stt"
        elif "tts" in name_lower:
            return "tts"
        elif "llm" in name_lower:
            return "llm"
        else:
            return processor_name
