# M2 Learning Guide — WebSocket Transport

## The Stem (unchanged from M1)

```python
pipeline = Pipeline([
    transport.input(),    # NOW: FastAPIWebsocketInputTransport
    stt,
    user_aggregator,
    llm,
    tts,
    transport.output(),   # NOW: FastAPIWebsocketOutputTransport
    assistant_aggregator,
])
```

**The pipeline didn't change. The transport did.**

---

## Example 1 — Same "Hi" via Browser

**Do:** Start `ws_server.py`, open browser at `http://localhost:8765`, click Connect, say "Hi, what can you do?"

**Observe:** Same response quality as M1. Browser shows bot text reply. Audio plays.

**Understand:**
The pipeline is transport-agnostic. `transport.input()` produces `InputAudioRawFrame` regardless of whether it came from PyAudio (M1) or a WebSocket (M2). The STT service doesn't know or care how the audio arrived.

This is the core design principle: **frames decouple the transport from the processing logic.**

---

## Example 2 — Inspect WebSocket Frames in DevTools

**Do:** Open Chrome DevTools → Network → filter to "WS" → click the `ws://localhost:8765/ws` entry → Messages tab.

**Observe:**
- ↑ (outgoing) green arrows: binary frames every ~256ms (your mic PCM)
- ↓ (incoming) red arrows: binary frames in bursts when bot speaks (WAV chunks)
- Outgoing frame size: ~8KB (4096 samples × 2 bytes)
- Incoming frame size: varies, ~40-120KB per chunk

**Understand:**
`FrameSerializer` is the bridge between raw bytes and typed Pipecat frames:

```
Browser binary bytes
        ↓ RawAudioSerializer.deserialize(bytes)
InputAudioRawFrame(audio=bytes, sample_rate=16000, num_channels=1)
        ↓ pipeline processes...
TTSAudioRawFrame(audio=pcm_bytes)
        ↓ FastAPIWebsocketOutputTransport wraps WAV header (add_wav_header=True)
binary WebSocket message → browser
```

Without `serializer=RawAudioSerializer(...)` in `FastAPIWebsocketParams`, the input receives bytes but immediately does `continue` — they're silently dropped and the bot never hears you.

---

## Example 3 — Latency Comparison (M1 vs M2)

**Do:** Note the perceived delay in M1 (local), then compare to M2 (WebSocket on localhost).

**Observe:** Slight increase in STT TTFB — typically +10-30ms. LLM and TTS TTFB unchanged.

**Understand:**
M1 `LocalAudioTransport` reads from PyAudio at ~10ms intervals. M2 browser sends 4096-sample chunks at 256ms intervals. The STT service receives audio later than in M1, adding to TTFB.

This is inherent to WebSocket audio transport:
1. Browser buffers samples (ScriptProcessor chunk size = 4096 = 256ms)
2. TCP adds ~1ms on localhost, more on real networks
3. WebSocket has no clock synchronization — intervals aren't guaranteed

WebRTC (M3) fixes this with hardware-synchronized 10ms intervals.

---

## Example 4 — Interruption over WebSocket

**Do:** Trigger a long response, interrupt mid-sentence.

**Observe:** Bot stops, but you may hear 100-300ms of audio after speaking. More "tail" than M1.

**Understand:**
Two buffers between pipeline and your ears (vs one in M1):

1. **Pipeline output buffer**: ~40ms (Pipecat default audio chunk). `InterruptionFrame` drains this immediately.
2. **Browser audio queue**: `playbackQueue` in `websocket.html`. Already-received WAV blobs continue playing until the current blob ends.

The browser queue isn't aware of the interruption until the server stops sending new audio. The audio tail is the last WAV chunk that was already in-flight when the interrupt occurred.

In M3 (WebRTC), the jitter buffer + OPUS stream resets much faster.

---

## Example 5 — Disconnect and Reconnect

**Do:** Click Disconnect, then Connect again (same page, no reload).

**Observe:** Server logs show `EndFrame` → session ends. New Connect → new pipeline task starts fresh. Conversation history is gone.

**Understand:**
Each WebSocket connection is one pipeline session. The `on_client_disconnected` handler calls `await task.cancel()`, which sends `CancelFrame` downstream. This tears down the pipeline: STT disconnects from OpenAI, LLM session ends, TTS buffer discarded.

On reconnect, `@app.websocket("/ws")` creates a new `FastAPIWebsocketTransport` and a fresh `LLMContext`. Context doesn't persist between sessions in M2/M3.

(In M4, context summarization will carry context across bot transfers — but sessions still restart fresh on reconnect.)
