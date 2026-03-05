"""
RawAudioSerializer — a minimal FrameSerializer for M2 (WebSocket transport).

Learning note (M2):
    The browser sends raw 16-bit PCM audio as binary WebSocket messages.
    The server sends WAV-wrapped audio back (add_wav_header=True in transport params).

    This serializer's only job is to wrap incoming binary bytes as an
    InputAudioRawFrame so the pipeline can process them.

    Compare to ProtobufFrameSerializer (used by telephony providers):
    - Protobuf serializer: structured binary envelope with type tags
    - Raw audio serializer: just bytes → InputAudioRawFrame, no framing

    In production, you'd use ProtobufFrameSerializer or the RTVI protocol.
    RawAudioSerializer is intentionally minimal for learning.
"""

from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.transports.base_transport import TransportParams


class RawAudioSerializer(FrameSerializer):
    """Serializes raw binary WebSocket messages as InputAudioRawFrame objects.

    Input (browser → server): raw bytes = 16-bit PCM audio at 16kHz mono
    Output (server → browser): handled by transport (WAV chunks via add_wav_header=True)

    The deserialize method is called by FastAPIWebsocketInputTransport for
    every incoming binary message. The serialize method is never called because
    FastAPIWebsocketOutputTransport handles audio output independently.
    """

    def __init__(self, sample_rate: int = 16000, num_channels: int = 1):
        super().__init__()
        self._sample_rate = sample_rate
        self._num_channels = num_channels

    async def serialize(self, frame: Frame) -> bytes | str | None:
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        return None

    async def deserialize(self, data: bytes | str) -> Frame | None:
        if not isinstance(data, bytes) or len(data) == 0:
            return None

        return InputAudioRawFrame(
            audio=data,
            sample_rate=self._sample_rate,
            num_channels=self._num_channels,
        )
