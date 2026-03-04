"""
Shared observers for the pipecat-walkthrough project.

Provides:
- LoggingMetricsObserver: logs MetricsFrame data to stdout in a readable format.
"""

from loguru import logger

from pipecat.frames.frames import MetricsFrame
from pipecat.metrics.metrics import (
    LLMUsageMetricsData,
    TTFBMetricsData,
    TTSUsageMetricsData,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed


class LoggingMetricsObserver(BaseObserver):
    """Logs MetricsFrame events to stdout.

    Prints a readable summary of each metric as it passes through the pipeline.
    Does not modify or block any frames — pure observation.

    Learning note:
        This is one of the simplest possible BaseObserver implementations.
        on_push_frame() is called for every frame transfer in the pipeline.
        We filter for MetricsFrame and ignore everything else.
    """

    async def on_push_frame(self, data: FramePushed):
        if not isinstance(data.frame, MetricsFrame):
            return

        for metric in data.frame.data:
            if isinstance(metric, TTFBMetricsData):
                logger.info(
                    f"[METRICS] TTFB | processor={metric.processor} "
                    f"model={metric.model} value={metric.value:.3f}s"
                )
            elif isinstance(metric, LLMUsageMetricsData):
                usage = metric.value
                logger.info(
                    f"[METRICS] LLM tokens | processor={metric.processor} "
                    f"prompt={usage.prompt_tokens} "
                    f"completion={usage.completion_tokens} "
                    f"total={usage.total_tokens}"
                )
            elif isinstance(metric, TTSUsageMetricsData):
                logger.info(
                    f"[METRICS] TTS chars | processor={metric.processor} "
                    f"characters={metric.value}"
                )
            else:
                logger.info(f"[METRICS] {type(metric).__name__} | {metric}")
