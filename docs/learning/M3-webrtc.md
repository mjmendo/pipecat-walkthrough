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

**Prebuilt UI signaling protocol:** `SmallWebRTCPrebuiltUI` does not POST directly to `/api/offer`. It first POSTs to `/start` expecting a response that tells it where to do the WebRTC negotiation. The server returns `{ "webrtc_request_params": { "endpoint": "/api/offer" } }` which redirects it to the existing `/api/offer` handler. The server also needs a `PATCH /api/offer` endpoint to accept trickle ICE candidates sent after the initial offer/answer exchange.

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

---

## Example 6 — Transcription Quality and Audio Processing

**Observe:** M3 transcription may feel slightly worse than M2, and background noise can cause the STT to output hallucinated text in unexpected languages (Russian is common with Whisper-based models).

**Understand:**
Two separate effects are at play:

**1. WebRTC audio processing pipeline degrades STT input.**
In M2, the browser's `AudioContext({ sampleRate: 16000 })` captures mic audio and resamples it to 16 kHz before sending — clean, unprocessed PCM. In M3, the WebRTC stack applies hardware-level processing to the mic signal before OPUS encoding: echo cancellation (AEC), noise suppression, and automatic gain control (AGC). These are designed for human listeners on the other end, not for transcription models. AGC in particular can cause level fluctuations that confuse STT. On top of that, the server decodes OPUS back to 48 kHz PCM and resamples 48→16 kHz via libav before handing it to the STT service — a longer chain than M2's single browser-side resample.

The fix is to disable the browser's audio processing via `getUserMedia` constraints:

```javascript
getUserMedia({
  audio: {
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: false
  }
})
```

This gives the STT clean audio through the same WebRTC transport with no extra connections or complexity.

**2. Whisper hallucinates on ambiguous audio.**
When the VAD passes through a noise burst or very short sound, Whisper receives audio that doesn't clearly map to any words. Rather than returning silence, it generates plausible-sounding text — and statistically often picks non-English languages. Russian appears frequently because of the phoneme distribution in Whisper's training data. Forcing `language="en"` on `OpenAISTTService` prevents this at the cost of refusing to transcribe other languages.

**The delivery chain also affects output voice quality:**

| Module | TTS → speaker path | Lossy? |
|--------|-------------------|--------|
| M1 | PCM → PyAudio → speaker | No |
| M2 | PCM → WAV → WebSocket → WebAudio → speaker | No |
| M3 | PCM → 48 kHz resample → OPUS ~32 kbps → WebRTC → speaker | Yes |

The TTS voice (nova, gpt-4o-mini-tts) and its 24 kHz output are identical in all three. What differs is the delivery. OPUS at 32 kbps is excellent for voice but is lossy — it tends to slightly soften high-frequency sibilants (s, sh, f sounds), which can make the same voice sound marginally different. This is codec coloring, not a different voice.

**Production pattern:** Rather than a hybrid WebSocket-input/WebRTC-output connection (which introduces two connection lifecycles to synchronize), production systems tap into the decoded PCM server-side. The server forwards the already-decoded audio stream to the STT service in parallel, bypassing the browser's processing entirely. Pipecat's pipeline is already structured this way — `transport.input()` hands decoded frames to STT without the browser knowing.

---

## Appendix — Concepts and Acronyms

### kHz (kilohertz) — Sample Rate

A sound wave is continuous. To store or transmit it digitally, you take snapshots of its amplitude thousands of times per second. **kHz is how many thousands of snapshots per second** — the sample rate.

By the **Nyquist theorem**, a sample rate captures frequencies up to exactly half its value. So:

- **16 kHz** (16,000 samples/sec): captures up to 8 kHz. Used by most STT APIs (Whisper, Google, Deepgram). Enough for clear speech — all consonants, vowels, and prosody live below 8 kHz — but music would sound dull.
- **48 kHz** (48,000 samples/sec): captures up to 24 kHz, just above the ~20 kHz human hearing limit. What OPUS uses over WebRTC. The 48 kHz rate isn't capturing anything beyond human hearing — it's precisely *meeting* that ceiling with a small safety margin.

**Analogy:** Think of a flip-book animation. 16 kHz is like 16,000 pages per second — enough to see a person talking clearly. 48 kHz is 48,000 pages — every subtle facial expression is captured. For a voice call, 16k is fine. For music, you'd notice the difference.

**Why it matters in this pipeline:** The browser captures at 48 kHz via WebRTC/OPUS. The transport downsamples to 16 kHz before handing frames to the STT service. If these rates mismatch silently, the STT either hears chipmunk-speed audio (rate too high) or slow-motion audio (rate too low), producing garbage transcriptions.

---

### OPUS — Audio Codec

A **codec** (coder-decoder) compresses audio for transmission and decompresses it for playback. OPUS is the codec WebRTC uses for voice.

**What each bullet in Example 1 means:**

- **Adaptive bitrate (~32 kbps for voice):** OPUS constantly measures network conditions and adjusts how aggressively it compresses. At 32 kbps it sounds like a clear phone call. Under congestion it can drop to 6 kbps (noticeable but intelligible) — automatically, without any application code. Raw PCM at 16 kHz mono is 256 kbps — 8× more bandwidth, no adaptation.

- **Built-in FEC (Forward Error Correction):** OPUS embeds a low-quality copy of the previous frame inside the current frame. If a UDP packet is lost in transit, the receiver reconstructs the missing audio from the redundant data in the *next* packet. There is a ~1–2% bitrate overhead, but you never hear a dropout from a single lost packet.

- **Built-in PLC (Packet Loss Concealment):** When a packet is lost and FEC can't recover it (e.g. two consecutive losses), the decoder synthesizes a plausible continuation — extrapolating the waveform forward. The result is a very brief blur rather than a hard silence or click. PLC is the last line of defense.

- **Hardware-aligned 10ms frames:** OPUS always encodes in exactly 10ms chunks (or multiples: 20ms, 40ms, 60ms). These align with the OS audio clock, so delivery is metronomically regular. Compare to M2's ScriptProcessor, which fires every 256ms based on a JavaScript timer that can be delayed by tab throttling or garbage collection.

---

### Jitter — Arrival Irregularity

**What it is:** Jitter is the variation in packet arrival time. If packets are supposed to arrive every 10ms but actually arrive at 10ms, 14ms, 8ms, 11ms, 9ms — that variation is jitter.

**Why it matters:** Audio playback is continuous. If packets arrive unevenly, the player either stutters (runs out of audio) or plays unevenly (wrong timing). Jitter is the primary reason real-time audio over the internet can sound choppy even when no packets are lost.

**How it is measured:** The WebRTC stack tracks the difference between expected and actual arrival time for each packet, using the RTP timestamp to know when a packet *should* have arrived. The reported `jitter` stat (visible in `chrome://webrtc-internals`) is the running mean deviation in seconds. Values below 5ms are excellent; 20–50ms is noticeable; above 100ms means the call is in trouble.

**What influences it and by how much:**

| Factor | Typical jitter contribution |
|--------|---------------------------|
| Localhost (loopback) | < 1ms — effectively zero |
| Same WiFi network | 1–5ms — negligible |
| Home broadband (wired) | 2–15ms — fine for voice |
| Home WiFi (congested) | 10–50ms — audible |
| Mobile 4G | 20–80ms — variable |
| Intercontinental path | 30–150ms — significant |
| Packet queue at overloaded router | up to 500ms+ |

**How WebRTC handles it:** The browser maintains a **jitter buffer** — a small queue that delays playback slightly (typically 20–60ms) to smooth out arrival irregularities. Packets are held until the buffer can guarantee smooth delivery. This is why WebRTC audio sounds stable even on variable-quality networks. M2's manual `playbackQueue` in JavaScript is a coarse approximation of this.

---

### RTP — Real-time Transport Protocol

RTP is the packet format used to carry real-time audio and video over UDP. Each RTP packet contains:

- A **sequence number** — so the receiver can detect lost or reordered packets
- A **timestamp** — the time of the first audio sample in this packet (used for jitter calculation and synchronization)
- A **SSRC** (Synchronization Source) — identifies which media stream this packet belongs to
- The **payload** — the compressed audio (OPUS bytes)

RTP itself provides no security or reliability. It relies on companion protocols for those.

---

### SRTP — Secure RTP

SRTP is RTP with encryption and authentication added. Every RTP packet is encrypted with AES and includes an authentication tag to detect tampering. WebRTC mandates SRTP — unencrypted media is not allowed.

---

### DTLS — Datagram Transport Layer Security

DTLS is TLS adapted for UDP (which, unlike TCP, has no ordering guarantees). WebRTC uses DTLS to:

1. Perform a key exchange handshake between browser and server
2. Derive the encryption keys used by SRTP

The `a=fingerprint:sha-256 ...` line in the SDP is the server's DTLS certificate hash, which the browser verifies to prevent man-in-the-middle attacks.

---

### ICE — Interactive Connectivity Establishment

ICE is the process WebRTC uses to find a network path between two peers. It works by:

1. Each side collects **ICE candidates** — possible addresses: local IP, router's public IP (via STUN), or a relay address (via TURN)
2. Both sides exchange candidate lists (via SDP or trickle ICE PATCH requests)
3. ICE probes all candidate pairs and picks the best working one

On localhost, the winner is always `127.0.0.1` — trivially. Over the internet, ICE is what allows WebRTC to punch through NATs and firewalls.

- **STUN** (Session Traversal Utilities for NAT): a lightweight server that tells you your public IP/port as seen from the internet. Used during ICE gathering.
- **TURN** (Traversal Using Relays around NAT): a relay server that forwards media when direct connection is impossible (e.g. symmetric NAT). Adds latency but ensures connectivity.

---

### SDP — Session Description Protocol

SDP is the text format used to describe a media session — not a transport protocol, just a structured document. It describes:

- Which codecs are offered/accepted, in preference order
- The sample rate and channel count for each codec
- ICE credentials (username fragment + password)
- DTLS fingerprint

The **offer/answer** exchange: the browser creates an SDP offer ("here's what I can do"), the server responds with an SDP answer ("here's what I'll accept"), and both sides configure themselves accordingly. This is the entire purpose of the `POST /api/offer` endpoint.

---

### FEC and PLC — Loss Recovery Stack

These two work together as a two-tier defense against packet loss:

| Tier | Mechanism | Handles | Cost |
|------|-----------|---------|------|
| FEC | Redundant data in next packet | Single isolated loss | ~2% more bandwidth |
| PLC | Synthesized audio continuation | Loss FEC can't recover | CPU only, no bandwidth |

FEC recovers from loss you don't even notice. PLC handles the cases FEC misses. Together they make OPUS extremely resilient to the 0.5–2% packet loss typical of the internet.
