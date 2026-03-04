# M4 — Multi-Bot Call Transfer

## Transfer Sequence Diagram

```
User: "I'd like to order a pizza"

Browser ──────── WebRTC audio ─────────▶ SmallWebRTCTransport (shared)
                                                    │
                                          Tech Support Pipeline
                                          (PipelineTask A: nova voice)
                                                    │
                                         LLM detects pizza intent
                                                    │
                                     calls transfer_to_pizza() tool
                                                    │
                                         handle_transfer_to_pizza()
                                          ├── result_callback({transferring})
                                          │   LLM says "Transferring you now"
                                          └── await task.cancel()
                                                    │
                                         PipelineTask A ends
                                         (EndFrame through processors)
                                                    │
                                         run_pizza_bot(transport, messages)
                                                    │
                                          Pizza Pipeline starts
                                          (PipelineTask B: shimmer voice)
                                          ├── context: SYSTEM_PROMPT + transfer_summary
                                          ├── FlowManager initialized
                                          └── TTSSpeakFrame: "Hi, I'm Marco..."
                                                    │
Browser ◀─────── WebRTC audio ──────────  ShimmerVoice: "What size pizza?"
```

---

## Key Design Decisions

### 1. Transport reuse across pipelines

The `SmallWebRTCTransport` is created once and shared. When tech support cancels, the WebRTC connection stays alive. Pizza bot calls `transport.input()` and `transport.output()` on the same transport object.

This means:
- No ICE renegotiation needed
- No audio gap for the user (< 200ms between pipelines)
- Same browser connection throughout the call

### 2. Context hand-off via summary

Full context hand-off (all messages from tech support conversation) would:
- Pass potentially 50+ messages to pizza bot
- Confuse the pizza LLM with IT discussion
- Cost more tokens

Instead: `build_transfer_summary()` extracts the last 2-3 user messages as a brief summary injected as a system message to pizza bot.

### 3. Function calling as application trigger

`transfer_to_pizza()` is registered as an LLM tool. The LLM decides when to call it — not the user clicking a button. This is function calling used for application logic, not just data retrieval.

The handler uses `FunctionCallResultProperties(run_llm=False)` (via `result_callback`) to prevent the LLM from generating more text after the function. The TTS says "Transferring..." then the pipeline ends.

### 4. pipecat-flows for pizza state machine

Without flows, pizza ordering requires manually:
- Injecting new system messages per step
- Calling `LLMRunFrame` after each state change
- Tracking what step the user is on

With `FlowManager`:
- Each state is a `NodeConfig` with its own prompt and functions
- Returning `(result, next_node_config)` triggers automatic transition
- Context strategy handles message history across nodes

---

## Running M4

```bash
python bots/tech-support/transfer_server.py
# Open http://localhost:7860
```

### Test scenarios:

1. **Normal tech support**: "My laptop won't boot" → gets tech support response
2. **Transfer trigger**: "I'd like to order a pizza" → voice changes to shimmer
3. **Pizza order flow**: "Large" → "pepperoni and mushrooms" → confirmation
4. **Mid-transfer interruption**: Say "transfer" then immediately say "wait" — transfer still completes (function call result is in-flight)

---

## Transfer vs Full-Restart Comparison

| Approach | Pros | Cons |
|----------|------|------|
| **Pipeline transfer (M4)** | No WebRTC renegotiation, seamless audio | State in memory, single process |
| Full disconnect + reconnect | Completely isolated, can be separate services | Audio gap, ICE renegotiation, loses context |
| Parallel pipelines | Both bots ready instantly | Higher resource usage |

M4 uses the simplest approach: sequential pipelines on shared transport.
