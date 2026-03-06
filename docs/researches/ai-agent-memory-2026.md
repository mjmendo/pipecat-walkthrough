# AI Agent Memory in 2026: A Comprehensive Research Guide

> **Scope**: This document covers the taxonomy, use cases, solutions, trade-offs, and Pipecat integration patterns for AI agent memory as of March 2026. Primary focus is voice agents, with reference to this project's architecture.

---

## TL;DR

Memory is the #1 unsolved UX problem for voice agents in 2026. Users hate repeating themselves across calls. The good news: the solution stack has matured dramatically since 2024. Here is what to use:

| Scenario | Recommended Solution | Why | Est. Cost | Latency Impact |
|---|---|---|---|---|
| **Single-session prototype** | LLM context window + rolling summary | Zero infrastructure, zero latency | LLM tokens only | 0ms inline |
| **Multi-session personalization (< 10K users)** | **Mem0 cloud** (native Pipecat service) | Fastest path to production, handles extraction + dedup, SOC2/HIPAA options | ~$19–$249/mo | 0ms if pre-fetched at session start |
| **Multi-session personalization (self-hosted)** | pgvector on Postgres + Redis hot cache | Existing stack, no new infra, full control, 11x faster than Pinecone at scale | Postgres cost only | <10ms inline if co-located |
| **Multi-agent orchestration** | **Zep cloud with Graphiti** or Graphiti + Neo4j self-hosted | Graph structure captures entity relationships across agents and time | $1.25/1K msgs or infra cost | 0ms if pre-fetched |
| **Enterprise compliance (HIPAA/SOC2)** | Mem0 Enterprise (private cloud) or pgvector on own infra | HIPAA BAA, SOC2, BYOK, Kubernetes/air-gapped support | Enterprise pricing | 0ms if pre-fetched |
| **Local/private (no cloud, edge)** | FAISS or ChromaDB + SQLite | Fully local, no external calls, works offline | Near zero | 1–5ms inline |
| **High-scale SaaS (1M+ users)** | pgvector + Redis hybrid (async write pipeline) | Proven at scale, cost-efficient, Redis for hot state, Postgres for long-term | Infra cost only | Sub-ms (Redis) / <10ms (pgvector) |
| **Relationship-heavy / long-term users** | Zep open-source + Graphiti on Neo4j | Temporal graph captures evolving entity relationships across years of calls | Neo4j Community free | 50–150ms self-hosted |

**The latency rule:** Any memory retrieval that runs *inline* (before the LLM call) must complete in **<50ms**. Everything else must be async or pre-fetched before the pipeline starts. This eliminates naive real-time cloud vector search from most voice pipelines.

**The recommended production pattern:**
1. **Session start (outside pipeline):** Fetch user memories from Mem0/Zep/pgvector → inject into `system_prompt`
2. **Per turn (in-pipeline):** Use Redis-cached context (sub-ms); no blocking retrieval
3. **Post-turn (async observer):** Write new facts to memory store non-blocking via `BaseObserver`
4. **Session end:** Run LLM summarization → update rolling summary in Redis for next session

---

## Table of Contents

1. [Taxonomy of Agent Memory Types](#1-taxonomy-of-agent-memory-types)
2. [Use Cases Where Voice Agents Need Memory](#2-use-cases-where-voice-agents-need-memory)
3. [Full Spectrum of Solutions](#3-full-spectrum-of-solutions)
4. [Trade-offs Comparison Table](#4-trade-offs-comparison-table)
5. [Voice-Specific Analysis: The 300ms TTFB Constraint](#5-voice-specific-analysis-the-300ms-ttfb-constraint)
6. [Recommendations for pipecat-walkthrough](#6-recommendations-for-pipecat-walkthrough)
7. [What's New in 2026](#7-whats-new-in-2026)

---

## 1. Taxonomy of Agent Memory Types

There are four canonical memory types for AI agents, drawn from cognitive science and operationalized in LLM systems. Each maps differently to voice agent requirements.

### 1.1 Working / Short-Term Memory (In-Context Window)

**Definition**: The live contents of the LLM's context window during a single inference call — the system prompt, conversation turns, tool call results, and injected documents. It is ephemeral: it vanishes when the session ends unless explicitly persisted.

**Capacity in 2026**: Context windows of 128K–1M tokens are now common (GPT-4o at 128K, Gemini 1.5 Pro at 1M, Claude 3.5 Sonnet at 200K). Despite this, sending entire conversation histories on every turn is expensive and slow.

**Voice agent use cases**:
- The current turn's transcription and LLM response
- Tool call context (e.g., a CRM lookup result injected mid-conversation)
- Active booking / appointment data being collected
- The current pipeline's system prompt with persona and guardrails

### 1.2 Episodic / Conversation History Memory

**Definition**: A record of past conversation turns — what was said, in what order, and in what session. This is the "what happened before" layer. Stored externally (database, file) and selectively re-injected into context.

**Voice agent use cases**:
- "Resume" conversations: "As I mentioned last time, I need to reschedule…"
- Multi-turn booking flows that span a hang-up and callback
- Agent-side log of prior calls for support ticket context
- Compliance audit trail of everything said

### 1.3 Semantic / Long-Term Knowledge Memory

**Definition**: Distilled facts extracted from interactions and stored persistently — user preferences, biographical data, domain knowledge, product catalog facts. Retrieved by semantic similarity rather than chronological order.

**Voice agent use cases**:
- "Always call me Mike, not Michael" — name preference stored once, recalled forever
- User's preferred language, accent, or response length
- Product FAQ / knowledge base retrieval (RAG over documentation)
- Multi-user household identification (voice print → user profile → their preferences)

### 1.4 Procedural / Skill Memory

**Definition**: Encoded instructions for how to accomplish tasks — prompts, workflow templates, tool schemas, and agent behavior rules. The agent doesn't need to re-learn these from experience; they are engineered and stored as structured configurations.

**Voice agent use cases**:
- Call routing logic: "If caller asks about billing, transfer to billing queue"
- Persona scripts and tone guidelines
- Tool/function call schemas stored in the system prompt or a prompt registry
- Guardrail rule sets (see M7 in this project) — the "always do / never do" policy layer

---

## 2. Use Cases Where Voice Agents Need Memory

### 2.1 User Name and Preference Recall

The most basic personalization case. A caller's name, preferred language, and communication style are extracted once and persisted as semantic memory. On subsequent calls, these facts are injected into the system prompt before the first LLM call, making the agent feel naturally familiar.

**Technical note**: This is a **synchronous read** that must complete before the pipeline's first LLM call. It must be fast (<50ms) to stay within TTFB budget. See [Section 5](#5-voice-specific-analysis-the-300ms-ttfb-constraint).

### 2.2 Cross-Session Conversation Continuity

"We talked last week about upgrading your plan — would you like to continue where we left off?" This requires episodic memory with session linkage. The agent must associate a caller (via phone number, account ID, or voice biometrics) to their prior conversation history, retrieve a summary or key events, and inject them.

**Technical note**: Full conversation replay is impractical for voice. The standard pattern is to store a **rolling summary** that is regenerated at conversation end, then pre-pended to the next session's system prompt.

### 2.3 Knowledge Base Retrieval (Product Docs, FAQs)

RAG (Retrieval Augmented Generation) over a corpus of documents — support articles, product manuals, pricing tables. The user's utterance is embedded, a semantic search retrieves relevant chunks, and they are injected into context. This is the most common enterprise voice agent deployment pattern.

**Technical note**: Vector search latency is the key bottleneck here (50–200ms). Pre-fetching or semantic caching can bring this under 10ms on cache hits. See [VoiceAgentRAG dual-agent pattern](#52-dual-agent-rag-pattern-for-latency-mitigation).

### 2.4 Learned User Behavior

Observations accumulated over many sessions: "This user always asks in Spanish", "This user interrupts frequently and prefers short answers", "This user typically calls on Mondays about invoices". These behavioral patterns are extracted by the memory layer and influence the agent's next-session strategy.

**Technical note**: This requires a background **write-back** process — the memory system processes conversation transcripts asynchronously after the call ends and extracts behavioral patterns. This is always **out-of-path** and uses an observer/hook pattern.

### 2.5 Multi-User Household Identification

Smart home or shared-device scenarios: multiple people share one phone number or one smart speaker. The agent must identify the caller (voice biometrics, PIN, or explicit identification) and load the correct user's memory profile. Zep and Mem0 both support multi-user memory via `user_id` namespacing.

### 2.6 Compliance / Audit Trail Requirements

Regulated industries (healthcare, finance, insurance) require a complete, tamper-evident log of all interactions. Every conversation turn must be persisted, timestamped, and attributable. This is purely episodic memory with strict write guarantees, not retrieval-optimized. AWS Bedrock AgentCore Memory and Azure AI Foundry both position themselves here.

---

## 3. Full Spectrum of Solutions

### 3.1 In-Process Patterns (No External Service)

These patterns run entirely inside the Python process managing the pipeline. Zero network latency, but limited by what fits in memory and context.

#### 3.1.1 Sliding Window Truncation

Keep only the last N turns in context. The simplest approach — discard the oldest messages when the buffer exceeds a token budget. Works for single-session interactions with short memory requirements.

**Implementation**: LangChain's `ConversationBufferWindowMemory(k=N)`. In Pipecat, the `OpenAILLMContext` object automatically manages the messages list; you add a custom truncation step before the LLM processor.

**Limitations**: Information older than N turns is irrecoverably lost. Poor for any cross-session need.

#### 3.1.2 LLM Summarization of Older Turns

When the context window fills, an LLM call summarizes the oldest segment into a compact paragraph, which replaces those turns. Newer turns are kept verbatim. The summary floats at the top of the history section.

**Implementation**: LangChain's `ConversationSummaryMemory` and `ConversationSummaryBufferMemory`. The latter keeps recent turns verbatim and summarizes older ones.

**Trade-off**: The summarization LLM call adds latency (500ms–2s) and cost. Best triggered at session end as a background task, not mid-conversation.

#### 3.1.3 Rolling Summary Injection

A persistent, human-readable paragraph ("The user's name is Ana. She is interested in the Premium plan. She mentioned she travels frequently.") is stored externally and injected at the top of every session's system prompt. The paragraph is updated at session end by an LLM summarizer. This is the most practical pattern for voice agents that need cross-session personalization without a full memory service.

**Pipecat pattern**: Store the rolling summary in a Redis key or database row keyed on `user_id`. Read it in the pipeline's startup hook, inject it into `system_prompt`. Update it via a `BaseObserver` triggered on `EndFrame`.

---

### 3.2 LangChain Memory Primitives

LangChain provides a family of memory classes, now supplemented by the newer **LangMem SDK** for long-term memory.

#### ConversationBufferMemory
Stores the full conversation history verbatim. Simple, but grows unbounded. Use for single-session agents with short conversations.

#### ConversationSummaryMemory
Summarizes the entire history into a running paragraph using an LLM. Compact, but lossy. Every new summary call erases granularity.

#### VectorStoreRetrieverMemory
Stores each conversation turn as an embedding in a vector store (Chroma, Pinecone, etc.) and retrieves the most semantically similar past turns for each new query. Enables true episodic recall, not just recency.

#### LangMem SDK (2026)
LangChain's dedicated long-term memory SDK, released in late 2025 and actively developed in 2026. Provides:
- `create_manage_memory_tool` and `create_search_memory_tool` — LLM-callable tools for agents to explicitly manage their own memories
- Namespace-based memory isolation per user, per team, or globally
- Integration with LangGraph's built-in persistence layer
- Backend-agnostic: uses any store (Postgres, Redis, in-memory)

**Limitation**: LangMem's search has high latency in benchmarks (p50: ~18s, p95: ~60s), making it unsuitable for real-time voice agent retrieval. It is best suited for background extraction and pre-call injection, not in-call retrieval.

---

### 3.3 LlamaIndex Memory

LlamaIndex provides:
- **ChatMemoryBuffer**: Keeps a rolling window of chat turns, truncated by token count. The default for LlamaIndex chat agents.
- **VectorMemory**: Embeds and stores messages in a vector index (backed by any LlamaIndex vector store), retrieving semantically similar past messages on each call.
- **SimpleComposableMemory**: Composes multiple memory types (e.g., buffer + vector) into one interface.

LlamaIndex's memory abstraction plugs directly into its `AgentRunner`, making it natural for RAG-based voice agents built on LlamaIndex. However, LlamaIndex is primarily a document indexing and retrieval framework; for pure conversational memory, Mem0 or Zep offer more purpose-built features.

---

### 3.4 Mem0

**Website**: mem0.ai | **GitHub**: github.com/mem0ai/mem0

#### What it does
Mem0 is a dedicated memory layer for AI applications. It sits between your agent and the LLM, providing a managed store of extracted facts and preferences. When you call `memory.add(messages, user_id=...)`, Mem0 uses an internal LLM to extract key facts from the conversation and stores them as compressed memory entries. When you call `memory.search(query, user_id=...)`, it returns the most semantically relevant memories via vector search.

The memory pipeline: conversation turns → LLM extraction → vector + graph store → semantic retrieval.

#### Architecture
- **Storage backends**: Vector store (for semantic search) + optional graph layer (for entity relationships)
- **Extraction model**: Configurable (OpenAI, Anthropic, local models)
- **LOCOMO benchmark score**: +26% accuracy over OpenAI Memory
- **Token efficiency**: Claims 90% lower token usage vs. full-context, 91% faster responses

#### 2026 Status
- 41K+ GitHub stars
- 13M+ Python package downloads
- 80K+ cloud service signups
- Raised $24M Series A (October 2025) from YC, Peak XV, Basis Set
- Exclusive memory provider for AWS's new Agent SDK
- SOC 2 & HIPAA compliant, BYOK available

#### Pricing (as of March 2026)
| Tier | Price | Memories | Retrieval Calls |
|------|-------|----------|-----------------|
| Free | $0 | 10K/month | 1K/month |
| Starter | ~$19/month | 50K/month | included |
| Pro | ~$249/month | Unlimited | Unlimited |
| Enterprise | Custom | Unlimited | Committed SLAs |

#### Pipecat Integration
Pipecat ships a first-class `Mem0MemoryService` in `pipecat.services.mem0`. It is a `FrameProcessor` that:
1. Intercepts `UserMessage` frames
2. Searches Mem0 for relevant memories matching the user's utterance
3. Injects retrieved memories into the LLM context (as system message or prepended context)
4. Writes new conversation turns to Mem0 (configurable: async or sync)

Key configuration parameters:
- `search_limit`: Max memories to retrieve per query (default: 5)
- `search_threshold`: Minimum similarity score (0.0–1.0)
- `add_as_system_message`: Whether to prepend memories as a system message
- `user_id`: Namespaces memories per user

**Known issue**: As of mid-2025, `self._store_messages` blocks the conversation flow (GitHub issue #1741). The write-back to Mem0 should be done asynchronously. Check the issue for current status.

---

### 3.5 Zep

**Website**: getzep.com | **GitHub**: github.com/getzep/zep and github.com/getzep/graphiti

#### What it does
Zep is an agent memory platform built on a temporal knowledge graph engine called **Graphiti**. Unlike Mem0's vector-only approach, Zep models entities (people, organizations, products) and their relationships, tracking how facts change over time.

#### Core Technology: Graphiti
Graphiti is open-source (Apache license) and available separately from Zep Cloud. Key architectural properties:

- **Bi-temporal model**: Every edge in the graph has two time dimensions — when the event *occurred* and when it was *ingested*. This allows accurate point-in-time queries: "What did we know about this user on January 15th?"
- **Temporal invalidation**: When a new fact contradicts an old one (user changed their address), Graphiti invalidates the old edge with an end timestamp rather than deleting it. History is preserved.
- **Dynamic synthesis**: Handles both unstructured conversational data and structured business data (CRM records, product catalogs).
- **Graph backends**: Neo4j, AWS Neptune, FalkorDB

#### Benchmark performance
Zep outperforms MemGPT on the Deep Memory Retrieval (DMR) benchmark and achieves the highest F1 and J scores in open-domain settings, edging out Mem0 methods by a narrow margin.

#### 2026 Pricing
Zep uses metered billing: you pay for ingestion/processing, not for storage.

| Metric | Rate |
|--------|------|
| Messages ingested | $1.25 per 1,000 messages |
| Business data ingested | $2.50 per MB |
| Free tier | Available (rate-limited) |
| Enterprise | Custom, BYOC available (deploy in your own AWS/GCP/Azure VPC) |
| Compliance | SOC 2 Type II, HIPAA BAA on Enterprise |
| Latency (cloud) | <200ms retrieval |

#### Pipecat Integration
Zep does not have a first-party Pipecat service as of this writing. Integration requires a custom `FrameProcessor` that:
1. Calls `zep_client.memory.add_session_messages()` for episodic writes (async, out-of-path)
2. Calls `zep_client.memory.search()` for retrieval before LLM calls (sync read, in-path)
3. Or uses Zep's MCP server to expose graph memory to any MCP-compatible agent

**Best for**: Enterprise applications needing entity-relationship tracking, temporal fact invalidation, or structured business data integration alongside conversational memory.

---

### 3.6 Redis Agent Memory Server (formerly Motorhead)

**Repository**: github.com/redis/agent-memory-server

**Motorhead** was the original Redis-backed summarization memory service from metal.ai. In 2025–2026, Redis absorbed and expanded this concept into the official **Redis Agent Memory Server**, repositioning Redis as a first-class AI memory backend.

#### What it does
- Stores conversation history, user preferences, and extracted facts across sessions
- Uses Redis Vector Search for semantic retrieval
- Automatically extracts and organizes memories from interactions
- Exposes both REST API and MCP interface (compatible with OpenAI, Anthropic, 100+ LLM providers)
- Runs summarization as a background worker on message ingestion

#### 2026 Status
Actively maintained by Redis Inc. Available via Docker for both dev and production. Redis positions it as a free, self-hosted alternative to Mem0/Zep for teams that already run Redis infrastructure.

#### Pipecat Integration
Custom `FrameProcessor` calling the REST API, or direct Redis client integration. Low-latency given Redis's in-memory architecture (sub-millisecond for cache reads, ~10ms for vector searches locally).

**Best for**: Teams already running Redis who want a unified memory + caching layer with zero additional managed-service cost.

---

### 3.7 Vector Stores as Memory Backends

Vector stores are the storage layer underneath most memory services. They can also be used directly as memory backends without a higher-level service.

#### Chroma (Local / Cloud)
- **Self-hosted**: Runs embedded in-process (`chromadb` Python package, zero config) or as a standalone HTTP server
- **Chroma Cloud**: Managed serverless, deployed on AWS/GCP/Azure, $5 free credits, ~30-second setup
- **Use case**: Rapid prototyping and learning projects. The default for LangChain tutorials.
- **Limitation**: In-memory local mode doesn't survive restarts. The unified Search API is cloud-only. For production, Chroma Cloud or a self-hosted persistent server is required.
- **Latency**: Sub-millisecond (local in-process), 20–80ms (local HTTP server), 50–200ms (Chroma Cloud)

#### Qdrant (Self-Hosted / Cloud)
- **Self-hosted**: Docker image, also available via Qdrant Cloud's Hybrid Cloud (deploy in your VPC)
- **Qdrant Cloud**: Usage-based pricing (CPU + RAM + disk), calculator at qdrant.tech/pricing
- **Key features**: HNSW indexing, named vectors, payload filtering, sparse vector support, quantization for memory reduction
- **Latency**: <10ms (self-hosted, local), 20–100ms (Qdrant Cloud depending on region and index size)
- **Best for**: Production self-hosted deployments requiring GDPR/data-residency compliance

#### Pinecone (Cloud-Only)
- **Architecture**: Serverless (consumption-based) — pay for read units, write units, and storage
- **2026 pricing model**: Consumption-based; storage at $0.33/GB/month; Starter free tier; Standard and Enterprise with monthly minimums
- **Key features**: Hybrid search (dense + sparse BM25), integrated reranking, real-time indexing, dedicated read nodes for billion-vector scale
- **Latency**: 20–100ms (varies by index size and tier)
- **Best for**: Teams wanting fully managed, zero-ops vector infrastructure with enterprise support

#### Weaviate (Self-Hosted / Cloud)
- **Weaviate Cloud**: Serverless (Flex plan) — $25 per 1M vector dimensions/month; Enterprise Cloud and BYOC with custom pricing
- **Key features**: Pluggable vectorizers, hybrid BM25 + dense search, multi-modal support, built-in AI agents layer
- **Latency**: 20–150ms (cloud, depending on configuration)
- **Best for**: Multi-modal data (text + images + audio) or complex hybrid search requirements

#### pgvector (PostgreSQL Extension)
- **What it is**: A PostgreSQL extension adding vector similarity search via HNSW and IVFFlat indexes
- **2026 status**: pgvector 0.8.0 released with improved filter performance and HNSW build speed
- **pgvectorscale**: Companion extension from Timescale (Rust/PGRX), adds StreamingDiskANN index. Benchmarked at 28× lower p95 latency and 16× higher throughput than Pinecone's storage-optimized index at 99% recall (471 QPS on 50M vectors)
- **Cost**: Zero additional licensing — runs inside your existing Postgres. Available managed on Supabase, Neon, AWS RDS, and all major cloud Postgres services.
- **Best for**: Teams with existing Postgres infrastructure who want to avoid a separate vector database service

---

### 3.8 Graph Memory

#### Graphiti (Open Source)
As described in [Section 3.5](#35-zep), Graphiti is the open-source core of Zep's memory system. It is independently usable with Neo4j, AWS Neptune, or FalkorDB as the graph backend. It is Apache-licensed and actively maintained by the Zep team.

**When to use Graphiti directly vs. Zep Cloud**: Use Graphiti directly when you need full data control, can manage Neo4j infrastructure, and want no managed-service cost. Use Zep Cloud when you need a managed API with SLAs.

#### Neo4j + LLM
The general pattern: Neo4j stores entities and relationships; an LLM layer extracts entities from conversations and generates Cypher queries. Tools like LangChain's `Neo4jGraph` chain and LlamaIndex's `KnowledgeGraphIndex` implement this pattern.

**Limitation**: Without Graphiti's bi-temporal model, Neo4j graphs require custom logic to handle fact invalidation over time. Graphiti solves this.

#### Zep's Temporal Knowledge Graph (Managed)
The production-ready, managed version of Graphiti. See [Section 3.5](#35-zep).

---

### 3.9 Letta (formerly MemGPT)

**Website**: letta.com | **GitHub**: github.com/letta-ai/letta

#### What it does
Letta is an agent development platform built on the MemGPT research paradigm: agents that manage their own memory as a first-class reasoning task. Rather than passive memory retrieval, Letta agents actively decide what to remember and what to forget by calling memory management tools as part of their reasoning loop.

The core insight: give the LLM explicit tools to read and write to different memory tiers (core memory, archival memory, recall memory), and it will learn to manage context limitations itself.

#### 2026 Architecture
- **Core memory**: Small, always-in-context block (human profile, agent persona) — the "working desk"
- **Archival memory**: Infinite external storage (vector search), summoned by the agent on demand
- **Recall memory**: Conversation history (episodic), also searchable
- **Letta Cloud**: Fully hosted agent service with REST API, model-agnostic, supports OpenAI/Anthropic/local
- **Agent Development Environment (ADE)**: Web UI to view and edit agent memory and prompts in real time ("white-box memory")

#### 2026 Milestones
- January 2026: Conversations API — agents maintain shared memory across parallel user experiences
- February 2026: Context Repositories — programmatic context management with git-based versioning (Letta Code)
- March 2026: Remote environments — message an agent working on your laptop from your phone

#### Pipecat Integration
Letta is not designed as a drop-in memory service; it is an agent runtime. Integration with Pipecat would require using Letta as the LLM layer (via its REST API), replacing Pipecat's LLM processor entirely. This is a deeper architectural change than using Mem0 or Zep.

**Best for**: Agents requiring sophisticated, autonomous memory management — long-lived personal assistants, research agents, coding assistants. Less suited for real-time voice agents where the agent loop latency is critical.

---

### 3.10 OpenAI Built-in Memory

#### ChatGPT Memory (Consumer)
Available in ChatGPT Plus/Pro — the model automatically remembers facts about the user across conversations. As of 2026, this is a consumer product feature, not directly accessible via the API.

#### Responses API (Stateful, API-level)
The Responses API (launched early 2025, replacing the Assistants API by August 2026) supports basic statefulness:
- `store: true` parameter causes OpenAI to store the conversation server-side
- `previous_response_id` chaining lets you continue a conversation without re-sending history
- OpenAI Agents SDK builds on top of this with `session.run()` for managed multi-turn state

**Limitation**: This is session continuity, not persistent long-term memory. OpenAI manages the history, but there is no user-level preference extraction, no semantic search over past conversations, and no cross-user memory. The Responses API deprecates the Assistants API; the latter is scheduled for shutdown August 26, 2026.

#### OpenAI Agents SDK Context
The `RunContextWrapper` allows developers to define structured state objects (Python dataclasses) that persist across agent `run()` calls within a session. This is in-process, not persistent — it doesn't survive a process restart.

**Bottom line on OpenAI memory**: OpenAI provides session continuity, not a memory system. For persistent, cross-session, semantic memory with OpenAI LLMs, use Mem0, Zep, or a vector store.

---

### 3.11 Cloud Provider Memory Services

#### AWS Bedrock AgentCore Memory
Amazon's fully managed memory service for Bedrock agents (announced 2025). Provides:
- **Short-term working memory**: Captures conversation context within a session (session state, slot values)
- **Long-term intelligent memory**: Stores persistent insights, preferences, and facts across sessions
- **Integration**: Native to AWS Bedrock's agent framework; also integrates with Mem0 (Mem0 is the exclusive memory provider for AWS's new Agent SDK)
- **Use case**: Enterprise AWS shops building regulated, compliant agent pipelines

#### Azure AI Foundry + Azure AI Search
Azure's approach integrates Azure AI Search (vector + keyword hybrid) as the memory backend:
- **Azure OpenAI Responses API**: Stateful multi-turn API similar to OpenAI's (Azure-hosted version)
- **Long-term memory pattern**: Mem0 + Azure AI Foundry + Azure AI Search (documented reference architecture by Microsoft)
- **Azure AI Gateway**: Centralizes model logging, rate limiting, and compliance for agents

**Both platforms** provide enterprise-grade compliance (SOC 2, HIPAA, FedRAMP) at the cost of vendor lock-in and higher per-operation cost compared to open-source alternatives.

---

## 4. Trade-offs Comparison Table

| Solution | Type | Latency p50 read | Cost | Self-hosted | Persistence | Data stays local | Pipecat integration difficulty | Best for |
|---|---|---|---|---|---|---|---|---|
| In-context sliding window | Working | 0ms (in-process) | Token cost only | Yes | No | Yes | Trivial | Short single-session calls |
| LLM summarization | Episodic | 500ms–2s (write) | LLM call cost | Yes | With DB | Yes | Low | Cross-session summary injection |
| LangChain ConversationBufferMemory | Episodic | 0ms | Free | Yes | With DB | Yes | Low | LangChain-based agents |
| LangChain VectorStoreRetrieverMemory | Episodic + Semantic | 50–200ms | Vector store cost | Yes | Yes | Yes | Medium | LangChain + Chroma/Qdrant stacks |
| LangMem SDK | Semantic | ~18s (p50!) | Free + infra | Yes | Yes | Yes | Medium | Background extraction, not in-call |
| LlamaIndex ChatMemoryBuffer | Working + Episodic | 0ms | Free | Yes | With DB | Yes | Low | LlamaIndex-based agents |
| **Mem0 Cloud** | Semantic + Episodic | ~50–100ms | $0–$249+/mo | No (or OSS) | Yes | No (cloud) | **Very Low** (native Pipecat service) | Most voice agents wanting quick personalization |
| Mem0 OSS | Semantic + Episodic | ~10–50ms | Infra cost | Yes | Yes | Yes | Low | Self-hosted Mem0 |
| **Zep Cloud** | Semantic + Episodic + Graph | <200ms | $1.25/1K msgs | No (or BYOC) | Yes | No (cloud/BYOC) | Medium | Enterprise, entity-relationship tracking |
| Graphiti (OSS) | Graph | 50–200ms | Infra + Neo4j | Yes | Yes | Yes | High | Full control over graph memory |
| Redis Agent Memory Server | Episodic + Semantic | <10ms (Redis) | Free (self-hosted) | Yes | Yes | Yes | Medium | Existing Redis shops |
| Chroma (local) | Semantic | <1ms (in-process) | Free | Yes | Opt-in | Yes | Low | Learning, prototyping |
| Chroma Cloud | Semantic | 50–200ms | Usage-based | No | Yes | No | Low | Small production |
| Qdrant (self-hosted) | Semantic | <10ms | Infra cost | Yes | Yes | Yes | Medium | Data-residency-sensitive production |
| Pinecone | Semantic | 20–100ms | $0.33/GB+usage | No | Yes | No | Medium | Zero-ops cloud-only |
| Weaviate (cloud) | Semantic + Multi-modal | 20–150ms | $25/1M dims/mo | No (or BYOC) | Yes | BYOC option | Medium | Multi-modal RAG |
| pgvector | Semantic | <10ms (local Postgres) | Postgres cost | Yes | Yes | Yes | Medium | Existing Postgres shops |
| Letta Cloud | All four types | 200–500ms (agent loop) | Usage-based | No (or OSS) | Yes | No | High (replaces LLM layer) | Long-lived personal agent |
| OpenAI Responses API | Working + Session | 0ms (server-side) | Token cost | No | Session only | No | Low | OpenAI-native apps, no cross-session need |
| AWS Bedrock AgentCore | Episodic + Semantic | 50–200ms | AWS pricing | No | Yes | AWS region | High | AWS-native enterprise |
| Azure AI Foundry | Episodic + Semantic | 50–200ms | Azure pricing | No | Yes | Azure region | High | Azure-native enterprise |

---

## 5. Voice-Specific Analysis: The 300ms TTFB Constraint

### 5.1 The Constraint

Human conversation operates on a **200–300ms response window** — the natural pause between one person finishing speaking and another beginning to respond. Voice AI that exceeds this threshold feels broken. Research confirms this is hardwired across cultures.

In a Pipecat pipeline, TTFB (time to first byte of TTS audio) is the sum of:
```
ASR finalization (~100ms)
+ Memory retrieval (variable)
+ LLM first-token latency (100–500ms depending on model and prompt length)
+ TTS first-audio-packet latency (~100ms)
```

The **memory retrieval budget is roughly 0–50ms** if you want to hit 300ms TTFB at p50. This is not much. A naive synchronous vector search to a remote cloud service (50–200ms) will reliably bust this budget.

### 5.2 Dual-Agent RAG Pattern for Latency Mitigation

The **VoiceAgentRAG** paper (arXiv 2603.02206, 2026) presents an open-source dual-agent memory router:
- **Fast Talker**: Checks a semantic cache first (sub-millisecond lookup with correct semantic matching)
- **Deep Talker**: If cache miss, performs full vector retrieval and responds, then warms the cache

Results: 75% overall cache hit rate, 316× retrieval speedup on cache hits (110ms → 0.35ms). This is the state-of-the-art pattern for voice RAG in 2026.

### 5.3 Memory Classification by Latency Profile

#### Class (a): In-Path Safe — Sync Read <50ms

These can be `FrameProcessor` nodes in the pipeline that **block** until memory retrieval completes before passing frames downstream. The latency is acceptable within the TTFB budget.

| Solution | Typical latency | Notes |
|---|---|---|
| Redis Agent Memory Server (local) | <5ms | Requires local Redis instance |
| pgvector (local Postgres) | <10ms | Requires local/co-located Postgres |
| Chroma (in-process, local) | <1ms | In-memory, no persistence |
| Qdrant (local Docker) | <10ms | Requires Docker sidecar |
| Rolling summary injection (pre-loaded) | 0ms (loaded at startup) | No per-turn retrieval needed |
| Pre-loaded user profile at session start | 0ms (loaded before first turn) | Any DB, loaded once |

**Pipecat pattern for class (a)**:
```
UserContextAggregator
  → MemoryRetrievalProcessor (sync, reads from fast local store)
  → LLMProcessor
  → TTSProcessor
```
The `MemoryRetrievalProcessor` is a custom `FrameProcessor` subclass that awaits the retrieval result before calling `push_frame()` downstream.

#### Class (b): Out-of-Path Only — Async Write-Back

These should **never** block the pipeline. Memory writes happen asynchronously after the conversation turn or after session end.

| Solution | Why out-of-path | Integration point |
|---|---|---|
| Mem0 cloud (write) | Network call to Mem0 API, 100–500ms | `BaseObserver.on_push_frame()` or post-session hook |
| Zep cloud (write) | Network call to Zep API, variable | `BaseObserver` or background task |
| LLM summarization | 500ms–2s LLM call | `EndFrame` observer, background task |
| LangMem extraction | ~18s p50 | Background job, definitely not in-path |
| Redis Agent Memory (write) | Fire-and-forget | Non-blocking async write |
| Behavioral learning / analytics | High latency, batch | Post-call processing |

**Pipecat pattern for class (b)**:
```python
class MemoryWriteObserver(BaseObserver):
    async def on_push_frame(self, src, dst, frame, direction, timestamp):
        if isinstance(frame, BotStoppedSpeakingFrame):
            asyncio.create_task(self._write_to_memory(context))
```
The observer creates a non-blocking async task for the write. Pipeline flow is unaffected.

#### Class (c): Hybrid — Pre-load Sync + Write-Back Async

The most practical pattern for production voice agents. Memory is loaded **once at session start** (acceptable blocking operation outside the real-time turn loop), then updated **asynchronously** after each turn or at session end.

| Solution | Read pattern | Write pattern |
|---|---|---|
| Mem0 (native Pipecat service) | Sync search per turn (50–100ms) | Async write-back (known issue: currently blocking, see #1741) |
| Zep Cloud | Pre-load session summary at start (sync), per-turn graph lookup (async) | Async message ingestion |
| Qdrant (cloud) | Pre-load relevant chunks at session start | Async indexing of new content |
| Rolling summary + Redis | Load summary at startup (0ms in-turn) | Async regeneration at `EndFrame` |

**Pipecat pattern for class (c) (recommended)**:
```
[Session Start]
  → load_user_profile() → load_rolling_summary() → inject into system_prompt
[Per Turn — in-path]
  → UserContextAggregator
  → LLMProcessor (with pre-injected context)
  → TTSProcessor
[Per Turn — out-of-path, async]
  → MemoryWriteObserver → async write to memory store
[Session End]
  → SummarizationTask → update rolling summary in store
```

### 5.4 Mem0's Native Pipecat Integration: Latency Nuance

Mem0's `Mem0MemoryService` in Pipecat performs a **synchronous search per user turn** (adds ~50–100ms) plus a **write-back** that, as of late 2025, could block the pipeline (GitHub issue #1741 — verify current status). For strict latency requirements:
1. Increase `search_threshold` to skip retrieval when confidence is low
2. Reduce `search_limit` to 3–5 memories
3. Track the fix for the blocking write-back issue

For most production voice agents targeting <500ms TTFB (not <300ms), Mem0's native integration is acceptable as-is.

---

## 6. Recommendations for pipecat-walkthrough

### 6.1 Learning / Personal Scale

**Goal**: Understand memory concepts, experiment without infrastructure overhead.

**Recommended stack**:
- **In-process sliding window**: Already provided by Pipecat's `OpenAILLMContext` — zero config
- **Chroma (local, in-process)**: Add `chromadb` for semantic recall experiments — no Docker needed
- **Rolling summary with JSON file**: Persist a JSON file per `user_id` containing a rolling summary paragraph — zero infrastructure, illustrates the cross-session pattern clearly

**Why not Mem0 yet**: The free tier (10K memories, 1K retrievals/month) is adequate, but learning the pattern with local tools first builds better intuition for what the managed services abstract away.

**Next step**: Add a `M8-memory.md` learning module demonstrating rolling summary + Chroma local memory in the Pipecat pipeline used in modules 1–4.

---

### 6.2 Small Production Scale (< 1,000 Users)

**Goal**: Real users, real sessions, sub-500ms TTFB acceptable, $50–$200/month budget.

**Recommended stack**:
- **Mem0 Starter or Pro tier** (~$19–$249/month): Use Pipecat's native `Mem0MemoryService` for personalization memory. Fast integration, good SDK, proven in production.
- **pgvector on Supabase or Neon** (free-to-$25/month): For knowledge base / FAQ retrieval (RAG). Keeps vector search inside your existing Postgres, avoids a separate service.
- **Redis (self-hosted or Upstash)**: For rolling session summaries — store a short paragraph per user_id, load at session start. Sub-millisecond reads.
- **Architecture**: Class (c) hybrid — pre-load user profile + summary at session start, Mem0 per-turn search, async write-back via observer.

**Pattern**:
```
[Session Start]: Load user profile (DB) + rolling summary (Redis) → inject into system_prompt
[Per Turn]: Mem0 search (50–100ms, in-path) → LLM → TTS
[Async]: Mem0 write-back (observer, non-blocking)
[Session End]: LLM summarize → update Redis summary
```

---

### 6.3 Enterprise Scale

**Goal**: 1,000+ users, compliance requirements (HIPAA/SOC2), data residency, complex entity tracking.

**Recommended stack**:
- **Zep Cloud (Enterprise/BYOC)** or **Graphiti + Neo4j (self-hosted)**: For temporal knowledge graph memory. Handles entity relationships, fact invalidation over time, and complex user profiles.
- **Qdrant (self-hosted, Hybrid Cloud)**: For knowledge base RAG. Full data residency, high-performance HNSW, no per-query cloud pricing.
- **AWS Bedrock AgentCore Memory** or **Azure AI Foundry** (if already AWS/Azure-native): For compliance audit trail and cloud-native integration.
- **Redis Enterprise**: For session caching and rolling summary storage.
- **Architecture**: Class (c) hybrid with Zep for semantic/graph + Qdrant for document RAG, all writes async.

**Compliance note**: Zep BYOC (deploy in your own VPC) + Qdrant self-hosted keeps all data on your infrastructure. This is critical for HIPAA-regulated healthcare voice agents.

---

## 7. What's New in 2026

### 7.1 OpenAI Memory

- **ChatGPT Memory** (consumer): Available in Plus/Pro/Team/Enterprise tiers in ChatGPT. Not an API feature.
- **Responses API** (developer): Stateful session continuity with `store: true` and `previous_response_id`. Replaces Assistants API (deprecated August 26, 2026). This is session state, not persistent semantic memory.
- **OpenAI Agents SDK**: `RunContextWrapper` for in-process state, `session.run()` for managed turns. No persistent semantic memory in the SDK itself; recommends Mem0 or custom vector store for long-term memory.
- **No standalone Memory API pricing as of March 2026**: OpenAI has not released a publicly priced, general-purpose memory API. Memory in ChatGPT remains a consumer product feature.

### 7.2 Mem0 v2 / Recent Features

- **Graph memory layer**: Optional graph layer added alongside vector storage for entity relationship modeling, competing with Zep's core differentiator
- **$24M Series A** (October 2025): Significant investment enabling expanded infrastructure and enterprise features
- **AWS Agent SDK integration**: Exclusive memory provider for AWS's new Agent SDK — major distribution channel
- **41K GitHub stars, 13M package downloads**: Dominant mindshare in the developer community
- **MCP server**: Mem0 now exposes an MCP server, enabling any MCP-compatible agent to use Mem0 memory
- **LOCOMO benchmark +26%** over OpenAI Memory: Published performance advantage

### 7.3 Letta Cloud

- Active cloud offering with full REST API
- Agent Development Environment (ADE) — white-box memory inspection and editing
- **Conversations API** (January 2026): Agents maintain shared memory across parallel user experiences — important for multi-session household voice agents
- **Context Repositories** (February 2026): Git-versioned context management
- **Remote environments** (March 2026): Agent running on one machine accessible from another
- Still positioned for sophisticated, long-lived agents rather than real-time voice

### 7.4 Major New Entrants and Trends

- **VoiceAgentRAG** (arXiv 2603.02206, 2026): Open-source dual-agent architecture solving the RAG latency problem for voice. 75% cache hit rate, 316× speedup on hits. Likely to influence Pipecat's built-in RAG patterns.
- **MemoClaw**: New entrant appearing in 2026 comparison articles alongside Mem0, Zep, LangMem. Privacy-first, local-first positioning.
- **Redis repositioning**: Redis Inc. now explicitly markets Redis as an AI memory infrastructure layer, not just a cache. The Agent Memory Server formalizes this.
- **pgvectorscale** (Timescale, 2026): Makes Postgres+pgvector competitive with or faster than Pinecone at scale, accelerating the "use Postgres for everything" trend.
- **Zep Graphiti MCP Server**: Zep now exposes its knowledge graph as an MCP server, making graph memory available to any MCP-compatible agent framework.
- **Context window vs. memory tradeoff**: As models hit 1M+ token contexts, the case for vector search memory weakens for small-to-medium conversation lengths. The remaining cases for external memory are: (1) truly long-lived agents (years of history), (2) cost optimization (avoiding re-sending large histories), (3) cross-session persistent facts, and (4) multi-user memory isolation.

---

## Sources

- [Mem0 Pricing](https://mem0.ai/pricing)
- [Mem0 GitHub](https://github.com/mem0ai/mem0)
- [Mem0 Series A Announcement](https://techcrunch.com/2025/10/28/mem0-raises-24m-from-yc-peak-xv-and-basis-set-to-build-the-memory-layer-for-ai-apps/)
- [Mem0 Graph Memory for AI Agents (January 2026)](https://mem0.ai/blog/graph-memory-solutions-ai-agents)
- [Mem0 vs Zep vs LangMem vs MemoClaw Comparison 2026](https://dev.to/anajuliabit/mem0-vs-zep-vs-langmem-vs-memoclaw-ai-agent-memory-comparison-2026-1l1k)
- [Mem0 AI Memory Benchmark](https://mem0.ai/blog/benchmarked-openai-memory-vs-langmem-vs-memgpt-vs-mem0-for-long-term-memory-here-s-how-they-stacked-up)
- [Zep: A Temporal Knowledge Graph Architecture for Agent Memory (arXiv 2501.13956)](https://arxiv.org/abs/2501.13956)
- [Zep Pricing](https://www.getzep.com/pricing/)
- [Zep Metered Billing Announcement](https://blog.getzep.com/introducing-metered-billing-and-byoc-deployments/)
- [Graphiti GitHub](https://github.com/getzep/graphiti)
- [Graphiti on Neo4j](https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/)
- [Letta Overview](https://docs.letta.com/overview)
- [Letta GitHub](https://github.com/letta-ai/letta)
- [Letta MemGPT Transition Blog](https://www.letta.com/blog/memgpt-and-letta)
- [LangMem SDK Launch](https://blog.langchain.com/langmem-sdk-launch/)
- [LangMem GitHub](https://github.com/langchain-ai/langmem)
- [LangMem Conceptual Guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)
- [Pipecat Mem0 Service Docs](https://docs.pipecat.ai/server/services/memory/mem0)
- [Pipecat Mem0 Service Reference](https://reference-server.pipecat.ai/en/stable/api/pipecat.services.mem0.memory.html)
- [Pipecat Mem0 PR #1388](https://github.com/pipecat-ai/pipecat/pull/1388)
- [Pipecat Mem0 Blocking Issue #1741](https://github.com/pipecat-ai/pipecat/issues/1741)
- [OpenAI Responses API Migration Guide](https://platform.openai.com/docs/guides/migrate-to-responses)
- [OpenAI Backend Memory API Discussion](https://community.openai.com/t/backend-memory-api-availability/1327871)
- [OpenAI Assistants API Deprecation August 2026](https://learn.microsoft.com/en-us/answers/questions/5571874/openai-assistants-api-will-be-deprecated-in-august)
- [AWS Bedrock AgentCore Memory](https://aws.amazon.com/blogs/machine-learning/amazon-bedrock-agentcore-memory-building-context-aware-agents/)
- [AWS Bedrock AgentCore Memory Cheat Sheet](https://tutorialsdojo.com/amazon-bedrock-agentcore-memory-cheat-sheet/)
- [Azure AI Foundry + Mem0 Long-Term Memory](https://medium.com/microsoftazure/implement-long-term-memory-in-your-ai-agents-with-mem0-azure-ai-foundry-and-ai-search-56efd8683c03)
- [Qdrant Pricing](https://qdrant.tech/pricing/)
- [Qdrant Cloud](https://qdrant.tech/cloud/)
- [Pinecone Pricing](https://www.pinecone.io/pricing/)
- [Pinecone Understanding Cost](https://docs.pinecone.io/guides/manage-cost/understanding-cost)
- [Weaviate Pricing](https://weaviate.io/pricing)
- [Weaviate Cloud Pricing Update](https://weaviate.io/blog/weaviate-cloud-pricing-update)
- [Chroma GitHub](https://github.com/chroma-core/chroma)
- [pgvector GitHub](https://github.com/pgvector/pgvector)
- [pgvector 0.8.0 Release](https://www.postgresql.org/about/news/pgvector-080-released-2952/)
- [pgvector 2026 Guide](https://www.instaclustr.com/education/vector-database/pgvector-key-features-tutorial-and-pros-and-cons-2026-guide/)
- [Redis Agent Memory Server](https://redis.github.io/agent-memory-server/)
- [Redis Agent Memory GitHub](https://github.com/redis/agent-memory-server)
- [The 300ms Rule — AssemblyAI](https://www.assemblyai.com/blog/low-latency-voice-ai)
- [Mem0 Voice Agent Memory Guide](https://mem0.ai/blog/ai-memory-for-voice-agents)
- [VoiceAgentRAG: Dual-Agent RAG Architecture (arXiv 2603.02206)](https://arxiv.org/html/2603.02206.pdf)
- [Sub-Second Voice Agent Latency Guide](https://dev.to/tigranbs/sub-second-voice-agent-latency-a-practical-architecture-guide-4cg1)
- [Top 10 AI Memory Products 2026](https://medium.com/@bumurzaqov2/top-10-ai-memory-products-2026-09d7900b5ab1)
- [ODEI vs Mem0 vs Zep: Choosing Agent Memory Architecture 2026](https://dev.to/zer0h1ro/odei-vs-mem0-vs-zep-choosing-agent-memory-architecture-in-2026-15c0)
