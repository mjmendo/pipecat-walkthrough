# M3 Learning Guide — SmallWebRTC Transport

## The Stem (unchanged from M1/M2)

```python
pipeline = Pipeline([
    transport.input(),    # NOW: SmallWebRTCInputTransport (aiortc)
    stt,
    user_aggregator,
    llm,
    tts,
    transport.output(),   # NOW: SmallWebRTCOutputTransport (aiortc)
    assistant_aggregator,
])
```

**Still the same pipeline. Transport is the only change.**

---

## Example 1 — Same "Hi" via SmallWebRTC

**Do:** Start `webrtc_server.py`, open `http://localhost:7860`, click Start in the prebuilt UI, say "Hi, what can you do?"

**Observe:** Response sounds noticeably crisper and more natural than M2. Lower perceived latency. UI shows connected state and speaking indicators.

**Understand:**
WebRTC carries audio as an **OPUS media track** (not WebSocket binary blobs). OPUS:
- Adaptive bitrate: ~32kbps for voice (vs ~256kbps for raw PCM in M2)
- Built-in FEC (Forward Error Correction): recovers from packet loss
- Built-in PLC (Packet Loss Concealment): smooths audio on loss
- Hardware-aligned delivery: 10ms frames, not 256ms software chunks

The audio quality difference is immediate and obvious.

---

## Example 2 — WebRTC Internals

**Do:** Open `chrome://webrtc-internals` in one tab. Start a call in another. Find your connection in the internals page.

**Observe:**
- `ICECandidatePairStats`: shows the chosen candidate pair (127.0.0.1 on localhost)
- `RTCInboundRtpStreamStats` (for audio you receive from bot):
  - `codec`: `opus/48000/2`
  - `jitter`: < 5ms on localhost
  - `packetsLost`: 0 on localhost
  - `bytesReceived`: growing during bot speech
- `RTCOutboundRtpStreamStats` (your mic stream):
  - `bytesSent`: growing continuously (including silence — OPUS encodes silence efficiently)

**Understand:**
WebRTC audio travels as **RTP packets** (Real-time Transport Protocol) over UDP, wrapped in SRTP (encrypted). Each RTP packet contains ~10ms of OPUS-encoded audio.

The browser and server never see raw bytes over the network — they see SRTP/DTLS encrypted RTP. `SmallWebRTCConnection` (aiortc) handles all of this transparently.

Contrast with M2: WebSocket you could see raw bytes in DevTools. WebRTC internals is the equivalent inspector — but it shows decoded metadata, not raw bytes.

---

## Example 3 — Latency Comparison (M2 vs M3)

**Do:** Compare perceived latency from M2 vs M3. Note the TTFB values in server logs.

**Observe:**
- STT TTFB: similar or slightly lower in M3 (audio delivered to pipeline faster)
- LLM TTFB: identical (same API call)
- TTS TTFB: similar (same API call; delivery slightly faster in M3)
- **Perceived** latency: noticeably lower in M3 even if measured TTFB is similar

**Understand:**
The perceived improvement comes from audio timing, not API latency:

| Factor | M2 WebSocket | M3 WebRTC |
|--------|-------------|----------|
| Audio chunk interval | 256ms (ScriptProcessor) | 10ms (RTP) |
| First audio delivery | After full WAV chunk | Within first RTP packet |
| Jitter buffer | Software (variable) | Hardware (stable) |
| Audio start | After WAV decode | Immediate on RTP decode |

Even if TTFB is identical, M3 feels faster because the first sound arrives 10ms after TTS sends its first audio frame, vs M2 where you wait for a full WAV chunk to arrive and decode.

---

## Example 4 — Signaling Flow

**Do:** Add `logger.debug(f"SDP offer received: {request['sdp'][:200]}")` to the `/api/offer` handler (temporarily). Restart server, start a call.

**Observe:** The SDP offer logged shows:
```
v=0
o=- 8921 1 IN IP4 0.0.0.0
...
m=audio 9 UDP/TLS/RTP/SAVPF 111 ...
a=rtpmap:111 opus/48000/2
a=fmtp:111 minptime=10;useinbandfec=1
a=ice-ufrag:...
a=ice-pwd:...
a=fingerprint:sha-256 ...
```

**Understand:**
SDP (Session Description Protocol) is the "menu" both sides use to negotiate:
- Which codecs are supported (OPUS with FEC enabled)
- What sample rates/channels (48kHz stereo, downmixed to mono)
- ICE credentials (ufrag + pwd) for finding network path
- DTLS fingerprint for encryption verification

The `SmallWebRTCConnection.initialize(sdp, type="offer")` call parses this SDP, generates candidates, selects capabilities, and creates the SDP answer. All handled by aiortc.

---

## Example 5 — Connection Resilience

**Do:** Start a call. Open browser DevTools → Application → Throttle to "Slow 3G". Observe the call quality.

**Observe:** OPUS adapts bitrate down. Audio may become slightly compressed (codec adapts). Call continues.

**Understand:**
WebRTC has three layers of resilience M2 lacks:
1. **FEC** (Forward Error Correction): sends redundant data so lost packets can be reconstructed
2. **PLC** (Packet Loss Concealment): synthesizes missing audio frames to prevent dropouts
3. **Adaptive bitrate**: reduces OPUS bitrate when network is constrained (32kbps → 8kbps)

None of this is in Pipecat code — it's in aiortc and the browser's WebRTC stack. The pipeline just sees clean `InputAudioRawFrame` regardless of network conditions.

This is why WebRTC is used for production voice AI and WebSocket is for simple demos.
