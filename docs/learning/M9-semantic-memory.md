# M9 Learning Guide — Semantic Memory with Vector Search

## Why Episodic Memory Matters: Users Hate Repeating Themselves

Imagine calling tech support for the second time about the same crashing laptop. The agent has no record of last week's call. You re-explain the device, the OS, the error, the steps you already tried. This is the single most frustrating experience in support interactions.

Voice bots make this worse. Every session starts fresh. The LLM's weights contain semantic knowledge — general facts about Windows, networking, hardware — but nothing about *this user's* specific problem history. Without episodic memory, every call is a first call.

M9 adds cross-session episodic memory to the tech support bot. After each session ends, a summary of the conversation is embedded and stored in a local [ChromaDB](https://www.trychroma.com/) vector database. At the start of each subsequent turn, the user's utterance is embedded and used to query the store. Semantically similar past episodes are injected into the LLM context, enabling responses like:

> "Last time you called in, we resolved a kernel panic on your MacBook by resetting the SMC. Did that fix hold?"

No new accounts. ChromaDB runs locally on disk. Embeddings use `text-embedding-3-small` — the same OpenAI key already in `.env`, at ~$0.00002 per 1K tokens.

---

## Episodic vs Semantic Memory

Two kinds of memory are in play every time the bot responds:

| Type | Where | Example |
|---|---|---|
| **Semantic** | LLM weights | "A kernel panic on macOS is caused by..." |
| **Episodic** | ChromaDB store | "On 2025-03-01, user had kernel panic on MacBook; SMC reset resolved it" |

The LLM already has semantic memory — general knowledge baked into its weights. `EpisodicMemoryStore` adds episodic memory: specific past events, stored and retrieved by similarity.

ChromaDB stores the *what happened* (episode). The LLM contributes the *what it means* (reasoning). `SemanticMemoryRetriever` bridges them by finding relevant episodes and injecting them as context before the LLM reasons.

---

## M9 as Pipecat Primitives

The M6 rule: _transform or gate → processor; monitor → observer_.

| Component | Type | Why |
|---|---|---|
| `EpisodicMemoryStore` | plain class | Storage/retrieval; no frame awareness needed |
| `SemanticMemoryRetriever` | `FrameProcessor` | Must inject memories into context *before* `user_agg` processes the turn |
| `EpisodicMemoryWriter` | `BaseObserver` | Writes on `EndFrame` — no frame mutation, just a side effect |

---

## The Stem (M8 → M9 diff)

```python
# M9 additions over M8 (semantic_memory_server.py)
import uuid
from datetime import datetime

store = EpisodicMemoryStore(persist_path=".chroma", api_key=api_key)
session_id = str(uuid.uuid4())

memory_retriever = SemanticMemoryRetriever(store=store, context=context,
                                           memory_slot_index=2, top_k=3,
                                           threshold=0.75)
memory_writer = EpisodicMemoryWriter(store=store, context=context,
                                     session_id=session_id, api_key=api_key,
                                     session_facts=fact_observer.facts)

pipeline = Pipeline([
    transport.input(), stt,
    ContentSafetyGuard(...),
    TopicGuard(...),
    PIIRedactGuard(mode="input", ...),
    memory_retriever,          # +++ M9a — after PIIRedactGuard(input), before user_agg
    user_agg,
    ConversationSummaryProcessor(...),   # M8a unchanged
    llm,
    PIIRedactGuard(mode="output", ...),
    tts, rtvi, transport.output(), assistant_agg,
])

task = PipelineTask(pipeline, observers=[
    LoggingMetricsObserver(), RTVIObserver(rtvi), DebugFrameObserver(),
    audit_observer,
    fact_observer,             # M8b unchanged
    memory_writer,             # +++ M9b — fires on EndFrame
])
```

Context slot layout after M9:

| Index | Role | Content | Written by | When |
|---|---|---|---|---|
| 0 | system | Persona system prompt | `persona.py` | Once at startup |
| 1 | system | Known facts | `FactExtractionObserver` | After each turn |
| 2 | system | Relevant past sessions | `SemanticMemoryRetriever` | On each `TranscriptionFrame` |
| 3+ | user/assistant | Conversation history | `LLMContextAggregatorPair` | Each turn |

---

## Example 1 — First Session: Cold Start

**Do:** Delete `.chroma/` if it exists (or use a fresh project clone). Start the server and connect.

```bash
rm -rf .chroma/
python bots/tech-support/semantic_memory_server.py
# Open http://localhost:7860
# Say: "My MacBook Pro running macOS Ventura shows a kernel panic on startup"
# Disconnect
```

**Observe in stdout:**
```
HH:MM:SS | DEBUG   | [MEMORY] SemanticMemoryRetriever: no episodes above threshold=0.75 | latency=118ms
HH:MM:SS | INFO    | [MEMORY] FactExtractionObserver updated facts: {device: MacBook Pro, os: macOS Ventura, error: kernel panic}
HH:MM:SS | INFO    | [MEMORY] EpisodicMemoryWriter: stored episode session_id=a3f7c2e1-...
```

**Observe in filesystem:**
```bash
ls .chroma/
# chroma.sqlite3   (ChromaDB's backing store)
```

**Understand:** `SemanticMemoryRetriever` ran on the first `TranscriptionFrame` and called `EpisodicMemoryStore.retrieve()`. The collection was empty — `count() == 0` — so it returned an empty list immediately without an embedding call. No memories were injected.

On `EndFrame`, `EpisodicMemoryWriter` fired:
1. Built conversation text from `context.messages` (user/assistant roles only)
2. Called `gpt-4o-mini` to produce a 3-5 sentence summary
3. Embedded the summary with `text-embedding-3-small`
4. Upserted to ChromaDB with `session_id` as the document ID

The `.chroma/` directory now exists and persists across process restarts.

---

## Example 2 — Second Session: Semantic Recall

**Do:** Without clearing `.chroma/`, restart the server and start a new session.

```bash
python bots/tech-support/semantic_memory_server.py
# Open http://localhost:7860
# Say: "My laptop won't boot, it crashes right before the login screen"
```

**Observe in stdout:**
```
HH:MM:SS | INFO    | [MEMORY] SemanticMemoryRetriever: retrieved 1 episode(s) (top score=0.83) | latency=142ms
```

**Observe in browser:** Bot responds with something like:
> "I see — boot failures before the login screen can be tricky. Last time we had a similar issue with a MacBook Pro on Ventura with a kernel panic on startup. If you have a Mac, resetting the SMC is often the first step. What kind of laptop do you have?"

**Understand:** "My laptop won't boot, crashes before login" and "MacBook Pro kernel panic on startup" have zero keyword overlap. A keyword search would return nothing. But their embeddings are semantically close (score=0.83 > threshold=0.75).

`SemanticMemoryRetriever` injected the past episode at `context.messages[2]`:
```
Relevant past sessions:
- [2025-03-01] User reported kernel panic on MacBook Pro running macOS Ventura
  during startup. Recommended SMC reset and NVRAM clear. Issue appeared resolved.
```

The LLM saw this context and referenced the past session in its response. No session transfer, no explicit user reference — the bot recalled on its own.

---

## Example 3 — Latency: Inline vs Async Retrieval

**Do:** Make 3 consecutive turns in session 2. Watch the `[METRICS] TTFB` log lines compared to M8 (no retriever).

**Observe:**
```
# M8 baseline (memory_server.py):
HH:MM:SS | INFO    | [METRICS] TTFB | processor=OpenAILLMService | value=0.318s

# M9 with SemanticMemoryRetriever:
HH:MM:SS | INFO    | [METRICS] TTFB | processor=OpenAILLMService | value=0.441s
```

A consistent ~120ms increase per turn.

**Understand:** `SemanticMemoryRetriever` awaits the embedding call *inline* — it is a `FrameProcessor` that does not push the `TranscriptionFrame` until retrieval completes:

```python
async def process_frame(self, frame, direction):
    episodes = await self._store.retrieve(frame.text, ...)  # blocks ~100ms
    if episodes:
        self._inject_memories(episodes)
    await self.push_frame(frame, direction)   # forwarded only after retrieval
```

This stalls the frame path for ~100ms every turn. On hardware with fast local STT (e.g. local Whisper model), this stall is visible as added TTFB.

**The async alternative:** push the `TranscriptionFrame` immediately, run retrieval in a background task, inject memories before `user_agg` receives the frame via an asyncio Event as a synchronization point. Faster, but significantly more complex to implement correctly and reason about. The inline design is the right starting point — optimize only if your STT TTFB is reliably below 100ms.

---

## Example 4 — Threshold Tuning

**Do:** Store 3 episodes on different topics (Wi-Fi, kernel panic, printer). Then ask "my connection keeps dropping" with different threshold values.

```python
# In semantic_memory_server.py, change:
memory_retriever = SemanticMemoryRetriever(store=store, context=context,
                                           memory_slot_index=2, top_k=3,
                                           threshold=0.50)  # try: 0.50, 0.75, 0.90
```

**Observe:**

| Threshold | Episodes retrieved | Notes |
|---|---|---|
| `0.50` | 3 (Wi-Fi, printer, kernel panic) | Printer episode is a false positive (score=0.53) |
| `0.75` | 1 (Wi-Fi only) | Correct — only the semantically relevant episode |
| `0.90` | 0 | Too strict — misses genuine paraphrase |

**Understand:** ChromaDB's cosine distance maps to similarity as `1.0 - distance`. Score ranges:

- **0.85–1.00:** Near-identical meaning ("my internet is down" ≈ "Wi-Fi not connecting")
- **0.75–0.85:** Same topic, different phrasing — the useful recall zone
- **0.50–0.75:** Topically related but not the same issue — often false positives
- **0.00–0.50:** Unrelated topics

`threshold=0.75` is the empirically tested default for short voice utterances in tech support. Tune per domain by examining `similarity_score` values in retrieval logs for real user queries over a week of traffic.

---

## Example 5 — Memory Decay and Multi-User Isolation

**Do (decay):** After storing several episodes, call `evict_older_than` at the Python REPL:

```python
import sys
sys.path.insert(0, ".")
from bots.shared.vector_memory import EpisodicMemoryStore

store = EpisodicMemoryStore(persist_path=".chroma", api_key="...")
# Evict everything (days=0 means everything older than now):
deleted = store.evict_older_than(days=0)
print(f"Deleted {deleted} episode(s)")

# Verify store is empty:
from bots.shared.vector_memory import EpisodicMemoryStore
store2 = EpisodicMemoryStore(persist_path=".chroma", api_key="...")
import asyncio
results = asyncio.run(store2.retrieve("kernel panic", threshold=0.0))
print(results)  # []
```

**Observe:** `EpisodicMemoryWriter` on the next session will log the store episode again. The bot cold-starts with no past memory.

**Do (multi-user isolation):** In `EpisodicMemoryWriter._summarize_and_store()`, add `user_id` to metadata:

```python
metadata = {
    "session_id": self._session_id,
    "user_id": "alice",          # add this
    "device": ...,
    "os": ...,
    "resolved": False,
}
```

In `EpisodicMemoryStore.retrieve()`, add a `where` filter to `self._collection.query()`:

```python
results = self._collection.query(
    query_embeddings=[query_embedding],
    n_results=n,
    where={"user_id": user_id},   # add this; pass user_id as a parameter
    include=["documents", "metadatas", "distances"],
)
```

**Understand:**

- **Decay:** ChromaDB has no built-in TTL. `evict_older_than()` queries by `timestamp` metadata and deletes by ID. For production, call this at server startup or on a daily schedule. This is the correct tradeoff for a local deployment — simple, explicit, auditable.

- **Isolation:** Without `user_id` filtering, all sessions share one memory pool. This is intentional for a *shared knowledge base* (any past session about kernel panics is relevant to the current one). For a *per-user assistant* (only Alice's past calls should inform Alice's current call), add `user_id` to metadata and filter. The change is two lines — one in store, one in retrieve.

---

## Key Takeaways

1. **Why vector search, not keyword search.** Users paraphrase. "My internet isn't working" and "Wi-Fi keeps dropping" have zero keyword overlap but near-identical embeddings. Semantic similarity retrieves relevant episodes that keyword search misses — this matters especially for voice, where users speak naturally and rarely use precise technical terms.

2. **EpisodicMemoryWriter on EndFrame is the only correct placement.** The full conversation is only available after the session ends. Writing during a turn would capture a partial conversation and add latency. `EndFrame` is the signal that the session is complete — observers that react to it are the right pattern for post-session persistence and cleanup.

3. **FrameProcessor for retrieval, BaseObserver for writing.** `SemanticMemoryRetriever` must inject memories *before* `user_agg` aggregates the context — so it must be in the frame path as a processor. `EpisodicMemoryWriter` only reads and writes after the session — no frame to mutate, so an observer is correct and adds zero latency to any response.

4. **session_id = str(uuid.uuid4()) per bot startup.** Fresh UUID per `run_tech_support_bot()` call. Each browser connection is a new session with a new ID. The same user reconnecting stores a second episode rather than overwriting the first — building richer history over time. For per-user memory, add a `user_id` field and filter by it (see Example 5).

5. **ChromaDB is local, free, no account.** `.chroma/` is a directory on disk. Data persists across process restarts. No Docker, no cloud, no API key beyond the existing OpenAI key for embeddings. `text-embedding-3-small` costs ~$0.00002/1K tokens — embedding a 3-sentence session summary costs ~$0.000006, negligible even at scale.

6. **Threshold tuning is a product decision.** `threshold=0.75` is the starting point. Too low: unrelated episodes pollute the context and confuse the LLM. Too high: genuine paraphrases are missed. Tune by logging `similarity_score` for real queries in production, not by guessing.
