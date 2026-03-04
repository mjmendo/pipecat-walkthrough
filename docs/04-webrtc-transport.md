# M3 — SmallWebRTC Transport

## What This Milestone Does

Replace WebSocket with WebRTC. Same pipeline, same bot, dramatically better audio quality and lower latency.

---

## WebSocket vs WebRTC Comparison

| Dimension | M2 WebSocket | M3 WebRTC (SmallWebRTC) |
|-----------|-------------|------------------------|
| **Protocol** | TCP-based, reliable, ordered | UDP-based, unreliable, unordered |
| **Audio codec** | Raw PCM (16-bit) | OPUS (adaptive bitrate) |
| **Framing** | Application (FrameSerializer) | Media track (RTP) |
| **Latency** | Buffered, adds 20-60ms | Hardware-aligned, ~10ms jitter buffer |
| **Audio quality** | Re-encoding artifacts possible | OPUS adaptive to network conditions |
| **NAT traversal** | None (localhost only) | ICE/STUN/TURN |
| **Setup complexity** | Low (just /ws endpoint) | Higher (SDP signaling, ICE) |
| **Browser support** | All browsers | All modern browsers |
| **Interruption** | Software buffer drain (~40ms) | Hardware-level immediate |

**Bottom line:** WebRTC is designed for real-time voice. WebSocket is not.

---

## How SmallWebRTC Works

SmallWebRTC (`aiortc`) is a pure-Python WebRTC implementation. It handles:
- **ICE**: Interactive Connectivity Establishment (finding the network path)
- **DTLS**: Encrypted transport layer
- **SRTP**: Secure Real-time Transport Protocol (audio/video streams)
- **OPUS**: Codec for audio (adaptive bitrate, FEC, PLC)

In `webrtc_server.py`, the sequence per connection:

```
1. Browser calls POST /api/offer with SDP offer
   SDP = Session Description Protocol — describes browser's capabilities:
   { codec: opus, sample_rate: 48000, channels: 2, ice_candidates: [...] }

2. SmallWebRTCConnection.initialize(sdp, type="offer")
   aiortc creates a peer connection, negotiates capabilities,
   generates ICE candidates, creates SDP answer

3. Server returns SDP answer to browser
   Browser sets remote description → ICE exchange begins

4. ICE/DTLS completes (typically <1s on localhost)
   Media track becomes active

5. SmallWebRTCTransport.input() starts receiving OPUS audio
   aiortc decodes OPUS → PCM → InputAudioRawFrame (48kHz, resampled to 16kHz for VAD)

6. Pipeline runs, TTS generates PCM
   SmallWebRTCTransport.output() encodes PCM → OPUS → sends via SRTP
```

---

## Running M3

```bash
python bots/tech-support/webrtc_server.py
# Server at http://localhost:7860

# Open http://localhost:7860 in browser
# Prebuilt UI appears → click Start → bot session begins
```

---

## What to Observe in chrome://webrtc-internals

1. Open `chrome://webrtc-internals` in a tab
2. Open `http://localhost:7860` in another tab, start a call
3. In webrtc-internals, find your connection:
   - **ICE candidates**: local candidates (127.0.0.1) negotiated
   - **DTLS handshake**: `sslinfo` shows TLS version, cipher
   - **Audio tracks** under `RTCInboundRtpStreamStats`:
     - `codec: opus`
     - `bitrate`: ~32kbps adaptive
     - `jitter`: typically <5ms on localhost
     - `packetsLost`: 0 on localhost (non-zero on bad networks)
4. Compare packet timing to M2 DevTools: WebRTC delivers audio at 10ms intervals, WebSocket at 256ms intervals (4096 samples / 16kHz)

---

## Renegotiation (Connection Recovery)

`webrtc_server.py` supports renegotiation via `pc_id`:

```
First connection:  { sdp: "...", type: "offer" }            → { sdp: "...", pc_id: "uuid-1" }
Reconnect:         { sdp: "...", type: "offer", pc_id: "uuid-1" } → reuses existing connection
```

This means:
- Page refresh → new connection (no pc_id)
- Network change (WiFi → cell) → renegotiate (send saved pc_id)
- Same `SmallWebRTCConnection` object → pipeline context preserved across network changes

---

## Architecture Diff (M2 vs M3)

```
M2 WebSocket:
Browser WebSocket ──binary frames──▶ FastAPIWebsocketInputTransport
                                           ↓ RawAudioSerializer.deserialize()
                                           ↓ InputAudioRawFrame (PCM 16kHz)
                                     [pipeline]
                                           ↓ TTSAudioRawFrame
FastAPIWebsocketOutputTransport ◀──WAV──▶ Browser WebSocket
                                     AudioContext.decodeAudioData()

M3 WebRTC:
Browser WebRTC OPUS track ──SRTP──▶ SmallWebRTCInputTransport (aiortc)
                                           ↓ OPUS decode → PCM
                                           ↓ InputAudioRawFrame (48kHz → 16kHz)
                                     [pipeline]
                                           ↓ TTSAudioRawFrame (24kHz PCM)
SmallWebRTCOutputTransport ◀──SRTP──▶ Browser WebRTC OPUS track
                                     Browser decodes OPUS → plays
```
