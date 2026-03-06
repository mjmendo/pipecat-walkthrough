# M8 and M9 — AI Agent Memory: Plan

## Context

M8 and M9 extend the M7 guardrails bot (`bots/tech-support/guardrails_server.py`) with two layers of memory:

- **M8 — Working Memory:** in-session context compression and structured fact extraction
- **M9 — Semantic Memory:** cross-session episodic recall via vector similarity search

Both milestones follow the established Pipecat primitive split:
- **FrameProcessors** for anything that transforms or gates the frame path
- **BaseObservers** for anything that only monitors (zero latency, runs in parallel)

No new API accounts. M8 uses the existing OpenAI key. M9 adds `chromadb` (local, no account) and OpenAI `text-embedding-3-small` (same key, ~$0.00002/1K tokens).

---

## M7 Baseline Pipeline (Starting Point)

```python
# guardrails_server.py — the stem M8 and M9 extend

audit = GuardrailAuditObserver(buffer_events=True)
on_event = audit.register_event

pipeline = Pipeline([
    transport.input(),
    stt,
    ContentSafetyGuard(api_key=..., async_mode=True, on_guardrail_event=on_event),  # M7a
    TopicGuard(api_key=..., on_off_topic=on_off_topic_detected,                      # M7b
               on_guardrail_event=on_event),
    PIIRedactGuard(mode="input", on_guardrail_event=on_event),                       # M7c
    user_agg,
    llm,
    PIIRedactGuard(mode="output", on_guardrail_event=on_event),                      # M7c
    tts,
    rtvi,
    transport.output(),
    assistant_agg,
])

task = PipelineTask(
    pipeline,
    params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    observers=[
        LoggingMetricsObserver(),    # M5a
        RTVIObserver(rtvi),           # M6
        DebugFrameObserver(),          # M6
        audit,                         # M7d
    ],
)
```

---

## M8 — Working Memory (Context Compression + Fact Extraction)

### Goal

Give the tech support bot working memory within a session:

1. **Context compression** (`ConversationSummaryProcessor`): when the message list grows beyond a threshold, summarize the oldest messages using `gpt-4o-mini` and replace them with a single compact `system` message. This prevents context-window overflow and reduces per-turn token cost as conversations grow longer.

2. **Fact extraction** (`FactExtractionObserver`): after each assistant turn completes, extract structured facts from the exchange (device, OS, error description, steps already tried) and inject them as a persistent `system` message prefix on every subsequent LLM call. This means the bot "remembers" what was established earlier without relying on the full message history.

### New Files

**`bots/shared/working_memory.py`** — two primitives:

```python
class ConversationSummaryProcessor(FrameProcessor):
    """Intercepts LLMMessagesFrame between user_agg and llm.

    When len(messages) > compress_threshold (default: 10), calls gpt-4o-mini
    to summarize messages[1:-2] (preserving system prompt and the last
    user/assistant exchange), replaces them with a single system message:
    "Conversation so far: <summary>", then forwards the condensed frame.

    Frame path:
        LLMMessagesFrame (from user_agg) → [check count]
            → if over threshold: call gpt-4o-mini, replace old messages
            → push condensed LLMMessagesFrame → llm

    Latency: ~150-300ms when summarization fires (gpt-4o-mini, max_tokens=200).
    Fires at most once per compress_threshold/2 turns after the first trigger.

    Args:
        api_key: OpenAI key (same as main bot).
        compress_threshold: message count that triggers summarization (default: 10).
        summary_model: model for summarization (default: "gpt-4o-mini").
        max_summary_tokens: token limit for the summary (default: 200).
        on_compress: optional async callback(original_count, compressed_count, summary_text).
    """

class FactExtractionObserver(BaseObserver):
    """Out-of-path observer. Extracts structured facts after each assistant turn.

    Watches for LLMFullResponseEndFrame via on_push_frame(). When detected,
    reads the last user+assistant message pair from the shared LLMContext,
    calls gpt-4o-mini with a structured extraction prompt, and updates a
    SessionFacts dict:
        {"device": str, "os": str, "error": str, "steps_tried": list[str]}

    The updated facts are injected as a fresh system message into the LLMContext
    via context.set_system_message_at_index(facts_slot_index, ...) so every
    subsequent LLM call sees the current known facts without touching the frame path.

    Runs entirely in the observer async task — zero latency on the frame path.

    Args:
        context: shared LLMContext object from LLMContextAggregatorPair.
        api_key: OpenAI key.
        facts_slot_index: index in context.messages where facts system message lives.
                          Default: 1 (immediately after the persona system prompt).
        extraction_model: model for extraction (default: "gpt-4o-mini").
    """
```

**`bots/tech-support/memory_server.py`** — extends `guardrails_server.py`:

```python
"""
M8 — Working Memory

Extends guardrails_server.py (M7) with:
  M8a: ConversationSummaryProcessor — compresses context when message count > threshold
  M8b: FactExtractionObserver      — extracts structured facts after each turn

Run:
    python bots/tech-support/memory_server.py

Then open http://localhost:7860
"""
```

### Pipeline Diff (M7 → M8)

```
M7 pipeline:
    transport.input() → stt
    → ContentSafetyGuard → TopicGuard → PIIRedactGuard(input)
    → user_agg → llm
    → PIIRedactGuard(output) → tts → rtvi → transport.output() → assistant_agg

M8 additions (marked with +++):
    transport.input() → stt
    → ContentSafetyGuard → TopicGuard → PIIRedactGuard(input)
    → user_agg
    → ConversationSummaryProcessor(compress_threshold=10)   # +++ M8a
    → llm
    → PIIRedactGuard(output) → tts → rtvi → transport.output() → assistant_agg

    observers added:
        FactExtractionObserver(context=context,             # +++ M8b
                               api_key=...,
                               facts_slot_index=1)
```

Exact pipeline construction in `memory_server.py`:

```python
summary_processor = ConversationSummaryProcessor(
    api_key=os.getenv("OPENAI_API_KEY"),
    compress_threshold=10,
    summary_model="gpt-4o-mini",
    max_summary_tokens=200,
)

fact_observer = FactExtractionObserver(
    context=context,                       # shared LLMContext from LLMContextAggregatorPair
    api_key=os.getenv("OPENAI_API_KEY"),
    facts_slot_index=1,
    extraction_model="gpt-4o-mini",
)

pipeline = Pipeline([
    transport.input(),
    stt,
    ContentSafetyGuard(api_key=..., async_mode=True, on_guardrail_event=on_event),
    TopicGuard(api_key=..., on_off_topic=on_off_topic_detected, on_guardrail_event=on_event),
    PIIRedactGuard(mode="input", on_guardrail_event=on_event),
    user_agg,
    summary_processor,           # M8a — between user_agg and llm
    llm,
    PIIRedactGuard(mode="output", on_guardrail_event=on_event),
    tts,
    rtvi,
    transport.output(),
    assistant_agg,
])

task = PipelineTask(
    pipeline,
    params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    observers=[
        LoggingMetricsObserver(),
        RTVIObserver(rtvi),
        DebugFrameObserver(),
        audit,
        fact_observer,           # M8b — out-of-path, zero latency on frame path
    ],
)
```

### What This Teaches

- **Context window economics:** voice bots accumulate tokens faster than chat bots — each TTS turn generates verbose text, users speak in circles, and Silero VAD can fire on partial phrases creating multiple short exchanges. Without compression, a 10-minute tech support call exceeds gpt-4o's optimal context range.
- **Summarization vs truncation:** truncation silently drops facts the user stated. Summarization preserves intent at lower token cost. `ConversationSummaryProcessor` demonstrates that compression is a frame transformation — it lives in the frame path because it mutates the message list the LLM sees.
- **`FactExtractionObserver` vs in-path extraction:** fact extraction could be a FrameProcessor, but it runs after the assistant responds (out-of-path is correct) and it must not block the next turn. The observer pattern hides ~150ms extraction latency completely.
- **`LLMMessagesFrame` anatomy:** the frame that carries the full message list from `user_agg` to `llm`. `ConversationSummaryProcessor` intercepts this frame type specifically — understanding it clarifies how aggregators, memory, and the LLM service cooperate.
- **Shared context object:** `FactExtractionObserver` writes back into the shared `LLMContext` directly. This is the clean way for observers to influence future LLM calls without entering the frame path.

### Document

`docs/learning/M8-working-memory.md`

### Learning Guide

**Stem (M8 additions over M7):**

```python
# M8 stem additions
summary_processor = ConversationSummaryProcessor(api_key=..., compress_threshold=10)
fact_observer = FactExtractionObserver(context=context, api_key=..., facts_slot_index=1)

pipeline = Pipeline([..., user_agg, summary_processor, llm, ...])   # summary_processor between user_agg and llm
task = PipelineTask(pipeline, observers=[..., fact_observer])
```

| # | Example | Do | Observe | Understand |
|---|---------|-----|---------|------------|
| 1 | Normal turn, facts injected | Say "My MacBook Pro running macOS Ventura shows a kernel panic". Ask an unrelated question next turn. | Second turn: logs show `[MEMORY] FactExtractionObserver updated facts: {device: MacBook Pro, os: macOS Ventura, error: kernel panic}`. LLM context at turn 2 has a system message "Known facts: device=MacBook Pro…" at index 1. | `FactExtractionObserver` runs after the assistant responds (`LLMFullResponseEndFrame`). It injects into the shared `LLMContext` object. The next turn's `LLMMessagesFrame` includes the facts message automatically — no frame was pushed to do this. |
| 2 | Compression triggers | Have 11+ exchanges (say "ok", "I see", etc. to accumulate turns rapidly). Watch logs after the 10th message. | Log: `[MEMORY] ConversationSummaryProcessor compressing 10 messages → summary`. The next `LLMMessagesFrame` seen by the LLM has 3 messages instead of 11: system prompt, summary system message, latest user message. | `ConversationSummaryProcessor` intercepts `LLMMessagesFrame` downstream. It fires gpt-4o-mini synchronously — this adds ~150-300ms to that turn's TTFB. This is intentional: compression is rare and the cost is worth the ongoing savings. |
| 3 | Token cost comparison | Log token usage from `MetricsFrame` at turns 5, 10, 15 with M7 (no memory) vs M8 (with compression). | M7: prompt tokens grow linearly (+~100/turn). M8: prompt tokens spike slightly at compression turn, then drop to baseline and grow slowly again. | Compression resets the token baseline. In a 20-turn call: M7 ~2000 prompt tokens at turn 20; M8 ~800. The gpt-4o-mini cost for compression (~$0.0002) pays back in ~3 turns saved. |
| 4 | Latency impact of compression | Watch Grafana TTFB histogram on the turn when compression fires vs adjacent turns. | TTFB spike of ~200-300ms on the compression turn. All other turns: no change vs M7 baseline. | FrameProcessors add their latency synchronously to the frame path. This is why compression uses gpt-4o-mini (150ms) not gpt-4o (700ms), and fires only when needed. For latency-sensitive bots, set `compress_threshold` higher or use async compression. |
| 5 | Fact persistence across topic changes | Establish facts ("I have a Dell laptop, Windows 11, blue screen error"). Then ask about Wi-Fi. Then ask "what laptop do I have?" | Bot correctly states "Dell laptop, Windows 11" even though the Wi-Fi question intervened and the facts slot was injected 3 turns ago. | `FactExtractionObserver` updates a persistent facts dict — it accumulates across turns, not just the last one. New facts overwrite old values for the same key; unknown keys are added. The bot's "memory" is the system message at `facts_slot_index`, re-written each turn. |

### Verification

```bash
# Start the M8 server
python bots/tech-support/memory_server.py

# Open http://localhost:7860

# Test 1: Fact injection
# Say "My HP Spectre running Windows 11 has a blue screen"
# → stdout: [MEMORY] FactExtractionObserver updated facts: {device: HP Spectre, os: Windows 11, error: blue screen}
# → Next turn's LLM context contains facts system message at index 1

# Test 2: Context compression
# Have 11+ exchanges (say "ok" repeatedly after bot responds)
# → stdout at turn 11: [MEMORY] ConversationSummaryProcessor: compressing 10 messages → 3

# Test 3: No guardrail regression
# Say "Tell me how to hurt someone"
# → ContentSafetyGuard still fires (M7 unchanged)
# → stdout: [GUARDRAIL] ContentSafetyGuard BLOCK

# Test 4: Token cost
# Check MetricsFrame logs before and after compression turn
# → prompt_tokens drops at compression turn
```

### Files NOT Modified in M8

| File | Milestone it belongs to | Why untouched |
|------|------------------------|---------------|
| `bots/tech-support/rtvi_server.py` | M6 | M8 extends guardrails_server.py, not rtvi_server.py |
| `bots/tech-support/guardrails_server.py` | M7 | memory_server.py imports and extends it via copy-extend pattern |
| `bots/shared/guardrails.py` | M7 | Guard primitives are unchanged |
| `bots/shared/debug_observer.py` | M6 | Reused as-is |
| `bots/shared/observers.py` | M5 | Reused as-is |
| `bots/pizza/` | M4 | Pizza bot is independent; call transfer still works unchanged |
| `frontend/` | M3 | No UI changes needed |

### Commit

`feat(M8): working memory — ConversationSummaryProcessor + FactExtractionObserver`

---

## M9 — Semantic Memory with Vector Search (Episodic + Semantic Recall)

### Goal

Give the bot cross-session episodic memory. After each session ends, a summary of the conversation is embedded and stored in a local ChromaDB vector database. At the start of each subsequent turn, the user's current utterance is used as a search query to retrieve semantically similar past episodes. Relevant episodes are injected into the LLM context as a `system` message, enabling responses like:

> "Last time you called in, we resolved a kernel panic on your MacBook by resetting the SMC. Did that fix hold?"

ChromaDB runs locally with `PersistentClient` — no Docker, no account, no network. Embeddings use OpenAI `text-embedding-3-small` (same API key, negligible cost).

### New Files

**`bots/shared/vector_memory.py`** — three classes:

```python
class EpisodicMemoryStore:
    """Wraps ChromaDB PersistentClient. Stores and retrieves conversation episodes.

    Storage path: .chroma/ (relative to project root, gitignored).
    Collection: "tech_support_episodes"

    Each episode is stored as:
        document: str           — plain-text summary of the conversation
        embedding: list[float]  — from OpenAI text-embedding-3-small
        metadata: dict          — {session_id, timestamp, device, os, resolved: bool}
        id: str                 — session_id (UUID)

    Methods:
        async store(session_id, summary, metadata) → None
            Embeds summary and upserts to ChromaDB.

        async retrieve(query_text, top_k=3, threshold=0.75) → list[EpisodeResult]
            Embeds query_text, queries ChromaDB, filters by cosine distance threshold.
            Returns list of EpisodeResult(summary, metadata, similarity_score).

        evict_older_than(days=30) → int
            Deletes episodes with timestamp < now - days. Returns count deleted.
            Call manually or on a schedule; not automatic.
    """

class SemanticMemoryRetriever(FrameProcessor):
    """Retrieves relevant past episodes and injects them into LLM context.

    Position: between PIIRedactGuard(input) and user_agg.

    On TranscriptionFrame downstream:
        1. Calls EpisodicMemoryStore.retrieve(frame.text, top_k, threshold) — async.
        2. If episodes found: formats them as:
               "Relevant past sessions:\n- [date] {summary}\n- ..."
           Injects this as context.messages[memory_slot_index] (a system message).
        3. Always forwards the TranscriptionFrame unchanged.

    Embedding call (~100ms) runs concurrently with the frame push — it is
    awaited before the frame is forwarded, but the 100ms is hidden inside the
    STT TTFB budget (STT typically takes 200-400ms after this processor).

    Actually: the retriever awaits the embedding inline (blocks the frame path
    for ~100ms). This is the design tradeoff — see M9 Learning Guide example 3
    for the async alternative and why inline is simpler to reason about.

    Args:
        store: EpisodicMemoryStore instance (shared across sessions).
        context: shared LLMContext object.
        memory_slot_index: index in context.messages for the retrieved memories
                           system message. Default: 2 (after persona + facts).
        top_k: max episodes to retrieve (default: 3).
        threshold: minimum cosine similarity to include (default: 0.75, range 0-1).
        on_retrieve: optional async callback(episodes) for logging/metrics.
    """

class EpisodicMemoryWriter(BaseObserver):
    """Out-of-path observer. Saves a session summary to EpisodicMemoryStore on session end.

    Watches for EndFrame via on_push_frame(). When detected:
        1. Reads context.messages to build the full conversation text.
        2. Calls gpt-4o-mini to produce a 3-5 sentence session summary.
        3. Extracts metadata from SessionFacts (if FactExtractionObserver ran).
        4. Calls EpisodicMemoryStore.store(session_id, summary, metadata).

    This runs in the observer async task — EndFrame signals session close,
    so the ~300ms storage operation does not block any user-facing response.

    Args:
        store: EpisodicMemoryStore instance.
        context: shared LLMContext.
        session_id: UUID string for this session (generated at bot startup).
        api_key: OpenAI key (for summarization + embedding).
        session_facts: optional dict from FactExtractionObserver for metadata.
    """
```

**`bots/tech-support/semantic_memory_server.py`** — extends `memory_server.py` (M8):

```python
"""
M9 — Semantic Memory with Vector Search

Extends memory_server.py (M8) with:
  M9a: SemanticMemoryRetriever  — retrieves relevant past episodes on each turn
  M9b: EpisodicMemoryWriter     — saves session summary to ChromaDB on EndFrame
  M9c: EpisodicMemoryStore      — local ChromaDB persistent store (no account)

Run:
    python bots/tech-support/semantic_memory_server.py

Then open http://localhost:7860

First session: no memories retrieved (store is empty).
Second session onward: relevant episodes from past calls appear as context.

Storage: .chroma/ directory at project root (persistent across runs, gitignored).
"""
```

### Pipeline Diff (M8 → M9)

```
M8 pipeline:
    transport.input() → stt
    → ContentSafetyGuard → TopicGuard → PIIRedactGuard(input)
    → user_agg → ConversationSummaryProcessor → llm
    → PIIRedactGuard(output) → tts → rtvi → transport.output() → assistant_agg

    observers: [LoggingMetricsObserver, RTVIObserver, DebugFrameObserver,
                GuardrailAuditObserver, FactExtractionObserver]

M9 additions (marked with +++):
    transport.input() → stt
    → ContentSafetyGuard → TopicGuard → PIIRedactGuard(input)
    → SemanticMemoryRetriever(store, context, memory_slot_index=2)  # +++ M9a
    → user_agg → ConversationSummaryProcessor → llm
    → PIIRedactGuard(output) → tts → rtvi → transport.output() → assistant_agg

    observers: [..., EpisodicMemoryWriter(store, context, session_id)]  # +++ M9b
```

Exact pipeline construction in `semantic_memory_server.py`:

```python
import uuid

store = EpisodicMemoryStore(
    persist_path=".chroma",
    collection_name="tech_support_episodes",
    api_key=os.getenv("OPENAI_API_KEY"),
    embedding_model="text-embedding-3-small",
)

session_id = str(uuid.uuid4())

memory_retriever = SemanticMemoryRetriever(
    store=store,
    context=context,           # shared LLMContext from LLMContextAggregatorPair
    memory_slot_index=2,       # after system prompt (0) and facts (1)
    top_k=3,
    threshold=0.75,
)

memory_writer = EpisodicMemoryWriter(
    store=store,
    context=context,
    session_id=session_id,
    api_key=os.getenv("OPENAI_API_KEY"),
    session_facts=fact_observer.facts,   # dict from FactExtractionObserver (M8b)
)

pipeline = Pipeline([
    transport.input(),
    stt,
    ContentSafetyGuard(api_key=..., async_mode=True, on_guardrail_event=on_event),
    TopicGuard(api_key=..., on_off_topic=on_off_topic_detected, on_guardrail_event=on_event),
    PIIRedactGuard(mode="input", on_guardrail_event=on_event),
    memory_retriever,            # M9a — retrieves past episodes on each turn
    user_agg,
    summary_processor,           # M8a — compresses context when too long
    llm,
    PIIRedactGuard(mode="output", on_guardrail_event=on_event),
    tts,
    rtvi,
    transport.output(),
    assistant_agg,
])

task = PipelineTask(
    pipeline,
    params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    observers=[
        LoggingMetricsObserver(),
        RTVIObserver(rtvi),
        DebugFrameObserver(),
        audit,
        fact_observer,           # M8b — fact extraction after each turn
        memory_writer,           # M9b — saves session summary on EndFrame
    ],
)
```

### System Context Slot Layout

After M9, the `LLMContext.messages` list is structured as:

| Index | Role | Content | Written by | When updated |
|-------|------|---------|-----------|--------------|
| 0 | system | Persona system prompt | `persona.py` | Once at bot start |
| 1 | system | Known facts (device, OS, error, steps) | `FactExtractionObserver` | After each turn |
| 2 | system | Retrieved past episodes | `SemanticMemoryRetriever` | On each `TranscriptionFrame` |
| 3+ | user/assistant | Conversation history | `LLMContextAggregatorPair` | Each turn; compressed by `ConversationSummaryProcessor` |

### What This Teaches

- **Episodic vs semantic memory:** episodic = specific past events ("last Tuesday the user had a kernel panic"). Semantic = general knowledge (already in the LLM weights). ChromaDB stores episodic memory; the LLM contributes semantic memory. The retriever bridges them.
- **Why keyword search fails for voice:** users paraphrase. "My internet isn't working" and "Wi-Fi keeps dropping" have zero keyword overlap but near-identical embeddings. Vector search retrieves relevant episodes that keyword search misses.
- **Embedding pipeline latency:** generating an embedding via OpenAI's API takes ~80-150ms. `SemanticMemoryRetriever` awaits this inline — the frame path stalls for ~100ms on every turn. This is acceptable because it is hidden inside STT TTFB (~200-400ms after the audio arrives). See example 3 for the async alternative and its tradeoffs.
- **Relevance threshold tuning:** `threshold=0.75` (cosine similarity) is a starting point. Too low (e.g. 0.5): unrelated episodes pollute the context and confuse the LLM. Too high (e.g. 0.95): misses genuine paraphrases. Tune by examining `similarity_score` in retrieval logs for real queries.
- **Multi-session isolation:** the `session_id` metadata field on each stored episode allows per-user filtering if a `user_id` is added. Without it, all sessions share one memory pool — intentional for a shared knowledge base, but wrong for a per-user assistant. The metadata filter is a one-line change to `EpisodicMemoryStore.retrieve()`.
- **Memory decay and eviction:** `EpisodicMemoryStore.evict_older_than(days=30)` deletes stale episodes by filtering on the `timestamp` metadata field. ChromaDB does not have TTL natively — eviction is manual. This is the correct tradeoff for local deployments.
- **`EpisodicMemoryWriter` as EndFrame observer:** the session summary is written *after* the session ends (`EndFrame`), not during. This is the only correct place — the full conversation is only available then, and writing during a turn would add latency. Observers that react to `EndFrame` are the right pattern for post-session cleanup and persistence.

### Document

`docs/learning/M9-semantic-memory.md`

### Learning Guide

**Stem (M9 additions over M8):**

```python
# M9 stem additions (over M8)
store = EpisodicMemoryStore(persist_path=".chroma", api_key=...)
session_id = str(uuid.uuid4())
memory_retriever = SemanticMemoryRetriever(store=store, context=context, top_k=3, threshold=0.75)
memory_writer = EpisodicMemoryWriter(store=store, context=context, session_id=session_id, api_key=...)

pipeline = Pipeline([..., PIIRedactGuard(mode="input",...), memory_retriever, user_agg, ...])
task = PipelineTask(pipeline, observers=[..., memory_writer])
```

| # | Example | Do | Observe | Understand |
|---|---------|-----|---------|------------|
| 1 | First session — no memories yet | Start the server fresh (`.chroma/` does not exist). Say "My MacBook has a kernel panic on startup". Complete the session (disconnect). | No retrieved memories appear in logs (store empty). On `EndFrame`: `[MEMORY] EpisodicMemoryWriter storing episode session_id=<uuid>`. `.chroma/` directory created. | First session cold-starts with empty store. `EpisodicMemoryWriter` captures the full conversation, summarizes it with gpt-4o-mini, embeds the summary, and persists it to ChromaDB. Subsequent sessions will be able to retrieve this episode. |
| 2 | Second session — semantic recall | Start a new session (same server process or restart). Say "My laptop won't boot, it crashes before the login screen". | Logs show: `[MEMORY] SemanticMemoryRetriever: retrieved 1 episode (score=0.83)`. LLM context at `messages[2]` now contains: "Relevant past sessions: [date] User had kernel panic on MacBook startup; SMC reset resolved it." Bot response references the past episode. | "laptop crashes before login" and "MacBook kernel panic on startup" share no keywords but are semantically close. The similarity score 0.83 > threshold 0.75, so the episode is injected. The bot can say "last time we resolved a similar issue by resetting the SMC — would you like to try that first?" |
| 3 | Latency: inline vs async retrieval | On turn 2 of session 2, watch the STT TTFB in MetricsFrame logs. Compare to M8 baseline (no memory retriever). | M8 STT TTFB: ~320ms. M9 with inline retriever: ~420ms (adds ~100ms). The retrieval latency adds to the user-perceived TTFB. | `SemanticMemoryRetriever` awaits the embedding call inline — it stalls the frame path. The ~100ms is usually hidden inside STT TTFB (if STT > 100ms), but on fast hardware with local Whisper, STT TTFB can be shorter than the embedding call, making the stall visible. The async alternative: push the `TranscriptionFrame` immediately, retrieve in background, inject before `user_agg` receives it via a synchronization point. Tradeoff: async is faster but adds implementation complexity. |
| 4 | Threshold tuning | Store 5 episodes across 5 sessions on different topics (Wi-Fi, kernel panic, printer, Bluetooth, slow laptop). Then ask "my connection keeps dropping". Try `threshold=0.5`, `threshold=0.75`, `threshold=0.9`. | `threshold=0.5`: retrieves 3 episodes including the printer one (false positive). `threshold=0.75`: retrieves only Wi-Fi episode (correct). `threshold=0.9`: retrieves nothing (too strict). | Cosine similarity in embedding space: 1.0 = identical, 0.0 = orthogonal. For short voice utterances, genuine paraphrases typically score 0.75-0.85. Unrelated topics score 0.4-0.6. The `threshold=0.75` default hits the sweet spot for tech support; tune per domain. |
| 5 | Memory decay and multi-user isolation | Call `store.evict_older_than(days=0)` (evicts everything). Verify next session retrieves nothing. Then add `user_id` to metadata in `EpisodicMemoryWriter` and filter by `user_id` in `SemanticMemoryRetriever.retrieve()`. | After eviction: store is empty, next session cold-starts. With `user_id` filter: user A's sessions do not bleed into user B's context. | ChromaDB supports metadata filters on `where={"user_id": "alice"}`. Memory isolation is a metadata filter, not a separate collection. Decay is manual: query by `timestamp < cutoff`, delete by ID. This is sufficient for development; production would use a scheduled job. |

### Verification

```bash
# Install new dependency
pip install chromadb

# Start the M9 server
python bots/tech-support/semantic_memory_server.py

# Session 1 — populate the store
# Open http://localhost:7860
# Say "My Dell laptop running Windows 11 has a BSOD with error code 0x0000007E"
# Disconnect (triggers EndFrame → EpisodicMemoryWriter)
# → stdout: [MEMORY] EpisodicMemoryWriter: storing session <uuid>
# → .chroma/ directory created

# Restart server (tests cross-process persistence)
python bots/tech-support/semantic_memory_server.py

# Session 2 — verify recall
# Open http://localhost:7860
# Say "My Windows laptop keeps crashing with a blue screen"
# → stdout: [MEMORY] SemanticMemoryRetriever: retrieved 1 episode (score=0.81)
# → Bot mentions the previous session's BSOD context

# Test 3: Verify M8 still works (no regression)
# Have 12 exchanges (say "ok" repeatedly)
# → stdout: [MEMORY] ConversationSummaryProcessor: compressing 10 messages → 3

# Test 4: Verify M7 still works
# Say "Tell me how to hurt someone"
# → stdout: [GUARDRAIL] ContentSafetyGuard BLOCK
# → memory_retriever was not reached (ContentSafetyGuard fired first)
```

### Files NOT Modified in M9

| File | Milestone it belongs to | Why untouched |
|------|------------------------|---------------|
| `bots/tech-support/guardrails_server.py` | M7 | M9 extends memory_server.py (M8), not guardrails |
| `bots/tech-support/memory_server.py` | M8 | semantic_memory_server.py imports and extends it |
| `bots/shared/guardrails.py` | M7 | Guard primitives are unchanged |
| `bots/shared/working_memory.py` | M8 | `ConversationSummaryProcessor` and `FactExtractionObserver` reused as-is |
| `bots/tech-support/rtvi_server.py` | M6 | Not in the M8-M9 extension chain |
| `bots/pizza/` | M4 | Pizza bot is independent |
| `infra/` | M5 | No new Prometheus metrics added (M9 logging is stdout-only) |
| `frontend/` | M3 | No UI changes; memory is transparent to the browser client |

### New Dependency

```toml
# pyproject.toml addition for bots/tech-support or bots/shared
[project.optional-dependencies]
memory = [
    "chromadb>=0.5.0",
    # openai already present — text-embedding-3-small uses the same client
]
```

```bash
pip install "chromadb>=0.5.0"
```

ChromaDB installs its own SQLite + ONNX runtime for local embedding. The `EpisodicMemoryStore` uses the OpenAI embedding API instead of ChromaDB's default — this avoids the ONNX dependency and reuses the existing key.

### Gitignore Addition

```
# .gitignore addition
.chroma/
```

### Commit

`feat(M9): semantic memory — ChromaDB episodic store + SemanticMemoryRetriever`

---

## Summary Table

| | M8 — Working Memory | M9 — Semantic Memory |
|---|---|---|
| **New shared file** | `bots/shared/working_memory.py` | `bots/shared/vector_memory.py` |
| **New server file** | `bots/tech-support/memory_server.py` | `bots/tech-support/semantic_memory_server.py` |
| **New primitives** | `ConversationSummaryProcessor` (FrameProcessor), `FactExtractionObserver` (BaseObserver) | `EpisodicMemoryStore`, `SemanticMemoryRetriever` (FrameProcessor), `EpisodicMemoryWriter` (BaseObserver) |
| **New dependency** | None | `chromadb>=0.5.0` |
| **New API calls** | `gpt-4o-mini` (summarization, extraction) — same key | `text-embedding-3-small` — same key |
| **Approximate cost/call** | ~$0.0002 (compression, rare) + ~$0.001 (fact extraction, every turn) | ~$0.00002 (embedding, every turn) |
| **Approximate added latency** | +0ms normally; +200ms on compression turn | +~100ms per turn (embedding, inline) |
| **Persistence** | In-memory per session | `.chroma/` on disk, cross-session |
| **Extends** | `guardrails_server.py` (M7) | `memory_server.py` (M8) |
| **Learning guide** | `docs/learning/M8-working-memory.md` | `docs/learning/M9-semantic-memory.md` |

---

## Task Tracking Additions

| # | Milestone | Status |
|---|-----------|--------|
| M8 | Working Memory (Compression + Fact Extraction) | ⬜ planned |
| M9 | Semantic Memory (ChromaDB + Episodic Recall) | ⬜ planned |
