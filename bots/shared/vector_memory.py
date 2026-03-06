"""
M9 — Semantic Memory with Vector Search

Reusable primitives for cross-session episodic memory via ChromaDB:

- EpisodicMemoryStore:    ChromaDB PersistentClient wrapper (store + retrieve + evict)
- SemanticMemoryRetriever: FrameProcessor that retrieves past episodes on each turn
- EpisodicMemoryWriter:   BaseObserver that saves session summary on EndFrame

Design principle:
    SemanticMemoryRetriever is a FrameProcessor — it sits between PIIRedactGuard(input)
    and user_agg and must inject retrieved memories into the LLMContext before the
    aggregator builds the LLMContextFrame. It awaits the embedding call inline
    (~100ms), hidden inside the STT TTFB budget (~200-400ms after audio arrives).

    EpisodicMemoryWriter is a BaseObserver — it fires on EndFrame, after the session
    ends. Writing to ChromaDB (~300ms) does not block any user-facing response.

Requires:
    pip install "chromadb>=0.5.0"

    chromadb is imported at module level with a clear ImportError message if missing.

Usage:
    from bots.shared.vector_memory import (
        EpisodicMemoryStore, SemanticMemoryRetriever, EpisodicMemoryWriter
    )

    store = EpisodicMemoryStore(persist_path=".chroma", api_key=...)
    session_id = str(uuid.uuid4())
    retriever = SemanticMemoryRetriever(store=store, context=context, memory_slot_index=2)
    writer = EpisodicMemoryWriter(store=store, context=context, session_id=session_id, api_key=...)

    pipeline = Pipeline([..., PIIRedactGuard(mode="input",...), retriever, user_agg, ...])
    task = PipelineTask(pipeline, observers=[..., writer])
"""

try:
    import chromadb
except ImportError as exc:
    raise ImportError(
        "chromadb is required for M9 semantic memory. "
        "Install it with: pip install 'chromadb>=0.5.0'\n"
        "Original error: " + str(exc)
    ) from exc

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from loguru import logger
from openai import AsyncOpenAI

from pipecat.frames.frames import EndFrame, TranscriptionFrame
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


_SESSION_SUMMARY_SYSTEM = """\
Summarize the following tech support conversation in 3-5 sentences.
Include: device type, OS, error description, troubleshooting steps taken, and whether the issue was resolved.
Be specific and concise. This summary will be used to help future sessions recall this conversation."""


# ── EpisodeResult ─────────────────────────────────────────────────────────────


@dataclass
class EpisodeResult:
    """A single retrieved episode from the vector store."""

    summary: str
    metadata: dict
    similarity_score: float


# ── EpisodicMemoryStore ───────────────────────────────────────────────────────


class EpisodicMemoryStore:
    """Wraps ChromaDB PersistentClient. Stores and retrieves conversation episodes.

    Storage path: .chroma/ (relative to project root, gitignored).
    Collection: "tech_support_episodes" (or custom collection_name).

    Each episode is stored as:
        document: str           — plain-text summary of the conversation
        embedding: list[float]  — from OpenAI text-embedding-3-small
        metadata: dict          — {session_id, timestamp, device, os, resolved: bool}
        id: str                 — session_id (UUID)

    Embeddings use the OpenAI API (same key as main bot) rather than ChromaDB's
    default ONNX embedding — avoids the heavy ONNX dependency and reuses the
    existing key.

    Args:
        persist_path: directory for ChromaDB storage (default: ".chroma").
        collection_name: ChromaDB collection name (default: "tech_support_episodes").
        api_key: OpenAI API key for embeddings.
        embedding_model: OpenAI embedding model (default: "text-embedding-3-small").
    """

    def __init__(
        self,
        persist_path: str = ".chroma",
        collection_name: str = "tech_support_episodes",
        api_key: Optional[str] = None,
        embedding_model: str = "text-embedding-3-small",
    ):
        self._client = chromadb.PersistentClient(path=persist_path)
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        self._openai = AsyncOpenAI(api_key=api_key)
        self._model = embedding_model

    async def _embed(self, text: str) -> List[float]:
        """Embed text using OpenAI text-embedding-3-small. ~80-150ms."""
        response = await self._openai.embeddings.create(
            model=self._model,
            input=text,
        )
        return response.data[0].embedding

    async def store(
        self,
        session_id: str,
        summary: str,
        metadata: Optional[Dict] = None,
    ) -> None:
        """Embed summary and upsert into ChromaDB.

        Args:
            session_id: UUID string — used as the ChromaDB document ID.
            summary: plain-text session summary to embed and store.
            metadata: optional dict with fields like device, os, resolved.
                      Values must be str/int/float/bool (ChromaDB requirement).
        """
        embedding = await self._embed(summary)
        meta: Dict = {"timestamp": datetime.utcnow().isoformat(), **(metadata or {})}

        # ChromaDB metadata values must be str/int/float/bool
        for k, v in list(meta.items()):
            if not isinstance(v, (str, int, float, bool)):
                meta[k] = str(v)

        self._collection.upsert(
            ids=[session_id],
            documents=[summary],
            embeddings=[embedding],
            metadatas=[meta],
        )

    async def retrieve(
        self,
        query_text: str,
        top_k: int = 3,
        threshold: float = 0.75,
    ) -> List[EpisodeResult]:
        """Embed query_text and retrieve semantically similar past episodes.

        Args:
            query_text: the user's current utterance (TranscriptionFrame.text).
            top_k: maximum number of episodes to consider (before threshold filter).
            threshold: minimum cosine similarity to include (0-1, default 0.75).

        Returns:
            List of EpisodeResult sorted by similarity descending.
            Empty list if store is empty or no episode exceeds threshold.
        """
        count = self._collection.count()
        if count == 0:
            return []

        n = min(top_k, count)
        query_embedding = await self._embed(query_text)

        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=n,
            include=["documents", "metadatas", "distances"],
        )

        episodes = []
        for doc, meta, distance in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            # ChromaDB cosine distance: 0=identical → similarity = 1 - distance
            similarity = round(1.0 - distance, 4)
            if similarity >= threshold:
                episodes.append(
                    EpisodeResult(
                        summary=doc,
                        metadata=meta,
                        similarity_score=similarity,
                    )
                )

        return episodes

    def evict_older_than(self, days: int = 30) -> int:
        """Delete episodes older than `days` days. Returns count deleted.

        ChromaDB has no TTL — eviction is manual. Call on a schedule or
        at bot startup. Uses the `timestamp` metadata field (ISO-8601 UTC).

        Args:
            days: episodes with timestamp < now - days are deleted.

        Returns:
            Number of episodes deleted.
        """
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        all_items = self._collection.get(include=["metadatas"])
        ids_to_delete = [
            item_id
            for item_id, meta in zip(all_items["ids"], all_items["metadatas"])
            if meta.get("timestamp", "") < cutoff
        ]
        if ids_to_delete:
            self._collection.delete(ids=ids_to_delete)
        return len(ids_to_delete)


# ── SemanticMemoryRetriever ───────────────────────────────────────────────────


class SemanticMemoryRetriever(FrameProcessor):
    """Retrieves relevant past episodes and injects them into LLM context.

    Position: between PIIRedactGuard(input) and user_agg in the pipeline.

    On TranscriptionFrame downstream:
        1. Calls EpisodicMemoryStore.retrieve(frame.text, top_k, threshold) — async.
        2. If episodes found: formats them as:
               "Relevant past sessions:\n- [date] {summary}\n- ..."
           and writes to context.messages[memory_slot_index] (a system message).
        3. Always forwards the TranscriptionFrame unchanged.

    The embedding call (~100ms) is awaited inline — the frame path stalls for
    ~100ms on every turn. This is acceptable because it is absorbed inside the
    STT TTFB budget (~200-400ms). See M9 learning guide example 3 for the
    async alternative and its tradeoffs.

    Args:
        store: EpisodicMemoryStore instance (shared across the session).
        context: shared LLMContext object.
        memory_slot_index: index in context.messages for retrieved memories.
                           Default: 2 (after persona at 0 and facts at 1).
        top_k: max episodes to retrieve (default: 3).
        threshold: minimum cosine similarity to include (default: 0.75).
        on_retrieve: optional async callback(episodes: list[EpisodeResult]).
    """

    def __init__(
        self,
        store: EpisodicMemoryStore,
        context: LLMContext,
        *,
        memory_slot_index: int = 2,
        top_k: int = 3,
        threshold: float = 0.75,
        on_retrieve: Optional[Callable] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._store = store
        self._context = context
        self._slot = memory_slot_index
        self._top_k = top_k
        self._threshold = threshold
        self._on_retrieve = on_retrieve

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if not isinstance(frame, TranscriptionFrame) or direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        try:
            t0 = time.perf_counter()
            episodes = await self._store.retrieve(
                frame.text,
                top_k=self._top_k,
                threshold=self._threshold,
            )
            latency_ms = (time.perf_counter() - t0) * 1000

            if episodes:
                logger.info(
                    f"[MEMORY] SemanticMemoryRetriever: retrieved {len(episodes)} episode(s) "
                    f"(top score={episodes[0].similarity_score:.2f}) | latency={latency_ms:.0f}ms"
                )
                self._inject_memories(episodes)
                if self._on_retrieve:
                    await self._on_retrieve(episodes)
            else:
                logger.debug(
                    f"[MEMORY] SemanticMemoryRetriever: no episodes above "
                    f"threshold={self._threshold} | latency={latency_ms:.0f}ms"
                )

        except Exception as e:
            logger.warning(f"[MEMORY] SemanticMemoryRetriever: retrieval failed: {e}")

        await self.push_frame(frame, direction)

    def _inject_memories(self, episodes: List[EpisodeResult]) -> None:
        """Write retrieved episodes as a system message at the configured slot index."""
        lines = []
        for ep in episodes:
            ts = ep.metadata.get("timestamp", "")
            date_str = ts[:10] if ts else "past session"
            lines.append(f"- [{date_str}] {ep.summary}")

        memory_text = "Relevant past sessions:\n" + "\n".join(lines)
        msgs = list(self._context.messages)
        if len(msgs) > self._slot:
            msgs[self._slot] = {"role": "system", "content": memory_text}
            self._context.set_messages(msgs)


# ── EpisodicMemoryWriter ──────────────────────────────────────────────────────


class EpisodicMemoryWriter(BaseObserver):
    """Saves a session summary to EpisodicMemoryStore on session end.

    Out-of-path observer — zero latency on the frame path.

    Watches for EndFrame via on_push_frame(). When detected:
        1. Reads context.messages to build the full conversation text.
        2. Calls gpt-4o-mini to produce a 3-5 sentence session summary.
        3. Extracts metadata from session_facts (if FactExtractionObserver ran).
        4. Calls EpisodicMemoryStore.store(session_id, summary, metadata).

    Runs after session close — the ~300ms storage operation does not block any
    user-facing response. Only fires once per session (guarded by self._written).

    Args:
        store: EpisodicMemoryStore instance.
        context: shared LLMContext.
        session_id: UUID string for this session (generated at bot startup).
        api_key: OpenAI key (for summarization).
        session_facts: live dict from FactExtractionObserver.facts — read at
                       write time so it contains the final accumulated facts.
        summary_model: model for summarization (default: "gpt-4o-mini").
    """

    def __init__(
        self,
        store: EpisodicMemoryStore,
        context: LLMContext,
        session_id: str,
        api_key: str,
        *,
        session_facts: Optional[Dict] = None,
        summary_model: str = "gpt-4o-mini",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._store = store
        self._context = context
        self._session_id = session_id
        self._openai = AsyncOpenAI(api_key=api_key)
        self._session_facts = session_facts if session_facts is not None else {}
        self._model = summary_model
        self._written = False

    async def on_push_frame(self, data: FramePushed) -> None:
        if not isinstance(data.frame, EndFrame) or self._written:
            return
        self._written = True
        asyncio.create_task(self._summarize_and_store())

    async def _summarize_and_store(self) -> None:
        msgs = self._context.messages

        # Build conversation text from user/assistant messages only
        conv_lines = []
        for m in msgs:
            role = m.get("role", "")
            content = m.get("content", "")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                conv_lines.append(f"{role.capitalize()}: {content}")

        if not conv_lines:
            logger.debug(
                f"[MEMORY] EpisodicMemoryWriter: no conversation to store "
                f"for session {self._session_id}"
            )
            return

        conversation = "\n".join(conv_lines)

        try:
            response = await self._openai.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SESSION_SUMMARY_SYSTEM},
                    {"role": "user", "content": conversation},
                ],
                max_tokens=250,
                temperature=0,
            )
            summary = response.choices[0].message.content.strip()

            metadata = {
                "session_id": self._session_id,
                "device": str(self._session_facts.get("device", "")),
                "os": str(self._session_facts.get("os", "")),
                "resolved": False,
            }

            await self._store.store(self._session_id, summary, metadata)
            logger.info(
                f"[MEMORY] EpisodicMemoryWriter: stored episode "
                f"session_id={self._session_id}"
            )

        except Exception as e:
            logger.warning(f"[MEMORY] EpisodicMemoryWriter: failed to store episode: {e}")
