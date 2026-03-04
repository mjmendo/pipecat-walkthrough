# M2 — WebSocket Transport + Browser UI

## What This Milestone Does

Same tech support bot as M1, now accessible from a browser via WebSocket. The pipeline is identical — only the transport changes.

**Key difference from M1:**
```
M1: [mic] → LocalAudioTransport → pipeline → LocalAudioTransport → [speaker]
M2: [browser mic] → WebSocket → FastAPIWebsocketTransport → pipeline → FastAPIWebsocketTransport → WebSocket → [browser speaker]
```

---

## How FastAPIWebsocketTransport Works

### Input path (browser → server)

1. Browser captures mic audio (Web Audio API, ScriptProcessor at 16kHz)
2. Browser sends raw 16-bit PCM as binary WebSocket message
3. `FastAPIWebsocketInputTransport._receive_messages()` receives the binary blob
4. Calls `serializer.deserialize(message)` → `InputAudioRawFrame`
5. Calls `push_audio_frame(frame)` → enters the pipeline

**Without a serializer**, step 4 becomes `if not self._params.serializer: continue` — messages are silently dropped. This is the most common M2 debugging mistake.

### Output path (server → browser)

1. `TTSAudioRawFrame` enters `FastAPIWebsocketOutputTransport`
2. With `add_wav_header=True`: wrapped in WAV container before sending
3. Browser receives binary WebSocket message → decodes WAV → plays audio

---

## FrameSerializer: The Missing Link

```
Browser binary bytes
        ↓
  RawAudioSerializer.deserialize()
        ↓
  InputAudioRawFrame(audio=bytes, sample_rate=16000, num_channels=1)
        ↓
  pipeline (VAD → STT → LLM → TTS)
        ↓
  TTSAudioRawFrame
        ↓
  transport wraps as WAV (add_wav_header=True)
        ↓
  binary WebSocket message
        ↓
  Browser plays WAV blob via AudioContext
```

**Serializer types:**
| Serializer | Use case | Wire format |
|-----------|----------|-------------|
| `RawAudioSerializer` (custom, this project) | Browser raw PCM | Raw bytes |
| `ProtobufFrameSerializer` | Telephony (Twilio/Telnyx) | Protobuf binary |
| No serializer | Telephony write-only (e.g., Plivo webhooks) | N/A |

---

## Running M2

```bash
python bots/tech-support/ws_server.py
# Server at http://localhost:8765

# Open http://localhost:8765 in browser
# Click "Connect" → browser requests mic access → WebSocket opens → bot session starts
```

---

## What to Observe in DevTools (Chrome)

1. Open DevTools → Network → WS (filter by WebSocket)
2. Click on the `ws://localhost:8765/ws` connection
3. Messages tab:
   - **Green arrows (↑ outgoing)**: your mic PCM chunks, every ~256ms (4096 samples / 16000 Hz)
   - **Red arrows (↓ incoming)**: WAV audio chunks from TTS, arrive in bursts when bot speaks
4. Frame sizes: outgoing ~8192 bytes (4096 × 2 bytes/sample), incoming varies (~40-120KB per WAV chunk)

**What this confirms:** WebSocket is byte-agnostic. Framing is the application's responsibility. The FrameSerializer defines the protocol.

---

## Latency vs M1

Expected TTFB increase over M1 (local transport):
- STT TTFB: +10-30ms (WebSocket round-trip + audio buffering)
- LLM TTFB: unchanged (still direct OpenAI API call)
- TTS TTFB: +10-30ms (WebSocket delivery + audio decoding in browser)

Total increase: ~20-60ms on localhost. Larger on real networks.

This is why WebRTC (M3) is preferred: it handles audio timing at the OS level.
