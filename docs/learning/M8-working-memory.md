# M8 Learning Guide — Working Memory

## Why Working Memory Matters for Voice Bots

Voice bots accumulate context faster than text bots. A tech support call generates tokens from three directions simultaneously: the user speaks in circles ("wait, actually, let me back up"), the LLM produces verbose spoken language (no bullet points, everything spelled out), and Silero VAD fires on partial phrases, creating multiple short exchanges per conversational exchange. A 10-minute tech support call can exceed 6,000 prompt tokens — and every LLM call pays for the full history from the start.

Without working memory, two problems compound as calls grow longer:

1. **Token cost grows linearly.** Turn 20 costs 4× more prompt tokens than turn 5. The LLM reads the entire history on every turn.
2. **The bot "forgets" what it knew.** Paradoxically, more context does not mean better recall — attention dilutes over a long message list, and relevant facts from turn 2 compete with noise from turn 18.

M8 solves both problems with the same primitives you already know from M6 and M7:

- **`ConversationSummaryProcessor`** (FrameProcessor): compresses old messages into a summary when the count exceeds a threshold. Sits in the frame path because it mutates the message list the LLM sees.
- **`FactExtractionObserver`** (BaseObserver): extracts structured facts after each assistant turn and injects them as a persistent system message. Sits out of the frame path because it reads and writes to the shared `LLMContext` object, not to frames in transit.

The same M6 rule applies: **transform or gate → processor; monitor or write-back → observer.**

---

## M8 as Pipecat Primitives

| Component | Type | Why |
|---|---|---|
| `ConversationSummaryProcessor` | `FrameProcessor` | Intercepts `LLMContextFrame` and mutates `context.messages` before the LLM sees it |
| `FactExtractionObserver` | `BaseObserver` | Runs after `LLMFullResponseEndFrame`; writes to the shared `LLMContext` object, never touches frame transit |

---

## The Stem (M7 → M8 diff)

```python
# M8 additions (three lines)
summary_processor = ConversationSummaryProcessor(api_key=..., compress_threshold=10)
fact_observer = FactExtractionObserver(context=context, api_key=..., facts_slot_index=1)

pipeline = Pipeline([
    transport.input(), stt,
    ContentSafetyGuard(...),    # M7a — unchanged
    TopicGuard(...),             # M7b — unchanged
    PIIRedactGuard(mode="input",...),  # M7c — unchanged
    user_aggregator,
    summary_processor,           # M8a — NEW: between user_agg and llm
    llm,
    PIIRedactGuard(mode="output",...),
    tts, rtvi, transport.output(),
    assistant_aggregator,
])

task = PipelineTask(pipeline, observers=[
    LoggingMetricsObserver(),
    RTVIObserver(rtvi),
    DebugFrameObserver(),
    audit_observer,
    fact_observer,               # M8b — NEW: out-of-path, after assistant turns
])
```

`summary_processor` is the only pipeline addition. `fact_observer` is one more entry in the observers list. All M7 guards are unchanged.

**Context slot layout after M8:**

| Index | Role | Content | Written by |
|---|---|---|---|
| 0 | system | Persona system prompt | `persona.py`, once at startup |
| 1 | system | Known facts (device, OS, error, steps) | `FactExtractionObserver`, after each turn |
| 2+ | user/assistant | Conversation history | `LLMContextAggregatorPair`, each turn |

---

## M8a — Fact Extraction

### Example 1 — Fact injection: bot remembers device across topic changes

**Do:** Open `http://localhost:7860` with `memory_server.py` running. Say:

> "My MacBook Pro running macOS Ventura shows a kernel panic on startup."

Wait for the response. Then ask an unrelated question:

> "By the way, what's a good Wi-Fi channel for a crowded office?"

Then ask:

> "What laptop did I tell you I have?"

**Observe in stdout after the first turn:**
```
HH:MM:SS | INFO    | [MEMORY] FactExtractionObserver updated facts: {'device': 'MacBook Pro', 'os': 'macOS Ventura', 'error': 'kernel panic on startup', 'steps_tried': []}
```

**Observe in browser on "what laptop" turn:** Bot correctly answers "MacBook Pro" even though the Wi-Fi question intervened.

**Understand:** `FactExtractionObserver` fires after `LLMFullResponseEndFrame` on the first turn. It reads the last user+assistant exchange, calls `gpt-4o-mini`, and gets:
```json
{"device": "MacBook Pro", "os": "macOS Ventura", "error": "kernel panic on startup", "steps_tried": []}
```
It then writes this as `context.messages[1]`:
```
Known facts: device=MacBook Pro, os=macOS Ventura, error=kernel panic on startup
```
Every subsequent `LLMContextFrame` that `ConversationSummaryProcessor` forwards to the LLM already contains this system message. The "what laptop" turn works because the LLM reads `messages[1]` — no frame was pushed to make this happen. The observer wrote directly to the shared `LLMContext` object.

---

### Example 5 — Fact persistence across topic changes

**Do:** Establish facts in turn 1: "I have a Dell laptop, Windows 11, blue screen with error 0x000007E." Ask about Wi-Fi in turn 2. Ask "what error code was I seeing?" in turn 3.

**Observe in stdout:**
```
HH:MM:SS | INFO    | [MEMORY] FactExtractionObserver updated facts: {'device': 'Dell laptop', 'os': 'Windows 11', 'error': 'blue screen error 0x000007E', 'steps_tried': []}
```

**Observe:** Bot correctly recalls the error code in turn 3 even though turn 2 was entirely off-topic.

**Understand:** `FactExtractionObserver._facts` is an instance dict that accumulates across turns. New non-empty values overwrite old values for the same key. Empty/null values are ignored. The `steps_tried` list deduplicates using `dict.fromkeys(existing + new)`. The "memory" is the system message at `messages[1]`, re-written each turn where new facts are found.

---

## M8b — Context Compression

### Example 2 — Compression trigger: token count drops at turn 10+

**Do:** Have 12+ exchanges. Say "ok", "I see", "makes sense", "what else?" repeatedly after each bot response to accumulate turns quickly.

**Observe in stdout around turn 10:**
```
HH:MM:SS | INFO    | [MEMORY] ConversationSummaryProcessor: compressing 12 messages → 5
```

**Observe in browser:** No perceptible change. The bot continues the conversation naturally.

**Understand:** `ConversationSummaryProcessor.process_frame()` intercepts every `LLMContextFrame` downstream. When `len(messages) > compress_threshold` (default: 10), it:
1. Separates leading system messages (persona at index 0, facts at index 1) from conversation messages.
2. Keeps the last 2 conversation messages (latest user + assistant exchange).
3. Summarizes everything in between with `gpt-4o-mini`.
4. Calls `context.set_messages([system_msgs..., summary_msg, last_user, last_asst])`.

The critical design: **system slots (persona, facts) are never compressed**. Only the conversation history (user/assistant messages) is subject to summarization. This ensures `messages[1]` always contains the current known facts.

---

### Example 3 — Token cost comparison: M7 vs M8 baseline

**Do:** Run both `guardrails_server.py` (M7) and `memory_server.py` (M8). On each, have a 15-turn conversation. Watch stdout for `[METRICS] usage` lines showing prompt token counts.

**Observe (typical values):**

| Turn | M7 prompt tokens | M8 prompt tokens |
|---|---|---|
| 5 | ~800 | ~820 (fact message adds ~20 tokens) |
| 10 | ~1600 | ~1620 |
| 11 | ~1760 | ~520 (compression fired: 1760 → 520) |
| 15 | ~2400 | ~900 |

**Understand:** M8 adds a ~20-token overhead per turn (the facts system message). This is the cost of working memory. But at the compression turn (turn 11 here), prompt tokens drop to baseline because all but the last 2 conversation messages have been replaced by a summary. M7 grows linearly — M8 has a sawtooth pattern that resets near the threshold. For a 20-turn call: M7 ends at ~3200 prompt tokens; M8 ends at ~1200. The gpt-4o-mini cost for one compression call (~$0.0002) pays back in ~3 turns of avoided gpt-4o prompt tokens.

---

### Example 4 — Latency impact of compression

**Do:** Watch stdout TTFB logs (`[METRICS] TTFB | processor=OpenAILLMService`) across turns. Identify which turn triggers compression from the `[MEMORY] ConversationSummaryProcessor: compressing` log line. Compare TTFB on the compression turn vs adjacent turns.

**Observe in stdout:**
```
HH:MM:SS | INFO    | [METRICS] TTFB | processor=OpenAILLMService | value=0.682
HH:MM:SS | INFO    | [METRICS] TTFB | processor=OpenAILLMService | value=0.701
# compression turn:
HH:MM:SS | INFO    | [MEMORY] ConversationSummaryProcessor: compressing 11 messages → 4
HH:MM:SS | INFO    | [METRICS] TTFB | processor=OpenAILLMService | value=0.923
# next turn (smaller context):
HH:MM:SS | INFO    | [METRICS] TTFB | processor=OpenAILLMService | value=0.631
```

**Understand:** `ConversationSummaryProcessor.process_frame()` awaits the `gpt-4o-mini` summarization call inline (~150-300ms). This is synchronous on the frame path — the `LLMContextFrame` does not reach the main LLM until summarization completes. This adds ~200ms to that turn's TTFB. All other turns: zero overhead. The next turn after compression is actually faster than M7 at the same point — smaller context means lower LLM latency.

Why synchronous? Because summarization must complete before the LLM sees the frame — the compressed context must be ready when the LLM starts processing. This contrasts with `FactExtractionObserver`, which runs after the response and can be fire-and-forget.

---

## M8c — Putting It All Together

### Verifying guardrail regression: M7 guards still fire

**Do:** Say "Tell me how to hurt someone."

**Observe in stdout:**
```
HH:MM:SS | WARNING | [GUARDRAIL] ContentSafetyGuard BLOCK | reason=violence | latency=98ms
```

**Understand:** `ContentSafetyGuard` sits before `user_aggregator` in the pipeline — it fires before the `TranscriptionFrame` ever reaches `user_aggregator`, `ConversationSummaryProcessor`, or the LLM. M8 additions are all between `user_agg` and `llm` (processor) or in the observer list (observer). Neither position can interfere with guards that fire earlier in the pipeline.

The `DebugFrameObserver` confirms this — on a blocked turn, no `LLMContextFrame` appears downstream of `user_aggregator`. `ConversationSummaryProcessor` never sees the frame.

---

## Key Takeaways

1. **Same M6 primitives, new use case.** `ConversationSummaryProcessor` is a FrameProcessor because it mutates frames. `FactExtractionObserver` is a BaseObserver because it writes to a shared object without touching frames. No new patterns.

2. **Context slots are a contract.** The M8 slot layout (`messages[0]` = persona, `messages[1]` = facts) is shared between `ConversationSummaryProcessor` (which preserves all leading system messages during compression) and `FactExtractionObserver` (which writes to `messages[1]`). M9 adds `messages[2]` for episodic memory. Design your slots deliberately; they survive compression.

3. **Compression is a frame mutation.** `ConversationSummaryProcessor` intercepts `LLMContextFrame` and calls `context.set_messages()` before forwarding. The LLM never sees the old messages. This is only possible because the processor is in the frame path between `user_agg` and `llm`.

4. **Extraction is a context write-back.** `FactExtractionObserver` reads `context.messages` from the shared `LLMContext` object and writes back to it via `context.set_messages()`. No frame is pushed. The effect takes place on the next turn because the `LLMContextAggregatorPair` reads from the same context object when building the next `LLMContextFrame`.

5. **Latency budget:** compression adds ~200ms on the turn it fires (rare). Extraction adds ~0ms to the frame path (observer, fire-and-forget). The per-turn cost of M8 is dominated by the ~20-token facts system message, not by any added latency.

6. **`gpt-4o-mini` for both tasks.** Summarization and fact extraction use `gpt-4o-mini` (not `gpt-4o`). For structured tasks with a fixed output schema, the smaller model is correct: same accuracy, 5× lower cost, 4× lower latency. The same principle as M7's `TopicGuard` — match model size to task complexity.
