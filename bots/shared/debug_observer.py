"""
DebugFrameObserver — M6 learning tool.

Logs every frame that flows through the pipeline with:
- Frame type
- Source processor → destination processor
- Direction (downstream / upstream)

Usage:
    task = PipelineTask(pipeline, observers=[DebugFrameObserver()])

Learning note (M6):
    Compare this observer to a FrameProcessor that does the same logging.

    Observer: non-intrusive, runs in separate async task, zero pipeline latency.
    FrameProcessor: in the frame path, adds latency on every frame.

    For debugging, always use observers. Only add a processor when you need to
    transform or gate frames.
"""

from loguru import logger

from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection


# Frame types to skip (too noisy for most debugging)
_SKIP_TYPES = frozenset({
    "InputAudioRawFrame",
    "OutputAudioRawFrame",
    "TTSAudioRawFrame",
})


class DebugFrameObserver(BaseObserver):
    """Logs all frames flowing through the pipeline.

    Useful for understanding frame flow without modifying any processor.
    Set verbose=True to also include audio frames (very noisy).
    """

    def __init__(self, verbose: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._verbose = verbose

    async def on_push_frame(self, data: FramePushed) -> None:
        frame_type = type(data.frame).__name__

        if not self._verbose and frame_type in _SKIP_TYPES:
            return

        direction = "↓" if data.direction == FrameDirection.DOWNSTREAM else "↑"
        src = type(data.source).__name__ if data.source else "?"
        dst = type(data.destination).__name__ if data.destination else "?"

        logger.debug(
            f"[FRAME] {direction} {frame_type:40s} | {src:30s} → {dst}"
        )
