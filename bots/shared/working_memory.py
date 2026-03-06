"""
M8 — Working Memory

Reusable FrameProcessor and BaseObserver primitives for in-session memory:

- ConversationSummaryProcessor: compresses LLM context when message count exceeds threshold
- FactExtractionObserver:       extracts structured facts after each assistant turn

Design principle:
    ConversationSummaryProcessor is a FrameProcessor — it sits in the frame path
    between user_agg and llm because it must mutate the LLMContextFrame before
    the LLM service sees it.

    FactExtractionObserver is a BaseObserver — it runs after the LLM responds
    (on LLMFullResponseEndFrame) and injects facts back into the shared LLMContext.
    Zero latency on the frame path.

Context slot layout expected by callers:
    messages[0] — persona system prompt (set at bot startup)
    messages[1] — facts slot (written by FactExtractionObserver)
    messages[2] — memories slot (written by SemanticMemoryRetriever, M9)
    messages[3+] — conversation history (managed by LLMContextAggregatorPair)

Usage:
    from bots.shared.working_memory import ConversationSummaryProcessor, FactExtractionObserver

    summary_processor = ConversationSummaryProcessor(api_key=..., compress_threshold=10)
    fact_observer = FactExtractionObserver(context=context, api_key=..., facts_slot_index=1)

    pipeline = Pipeline([..., user_agg, summary_processor, llm, ...])
    task = PipelineTask(pipeline, observers=[..., fact_observer])
"""

import asyncio
import json
import time
from typing import Callable, Dict, List, Optional

from loguru import logger
from openai import AsyncOpenAI

from pipecat.frames.frames import LLMContextFrame, LLMFullResponseEndFrame
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


# Type alias for the structured facts dict
SessionFacts = Dict[str, object]

_FACTS_EXTRACT_SYSTEM = """\
Extract structured facts from the following tech support conversation exchange.
Return a JSON object with exactly these fields (use empty string or empty list if unknown):
{"device": str, "os": str, "error": str, "steps_tried": list[str]}
Only include information explicitly stated by the user. Return only valid JSON, no explanation."""

_SUMMARY_SYSTEM = """\
Summarize the following tech support conversation in 2-4 sentences.
Preserve all technical details: device names, OS versions, error codes, and troubleshooting steps tried.
Be concise — this summary replaces the original messages to save context space."""


# ── ConversationSummaryProcessor ─────────────────────────────────────────────


class ConversationSummaryProcessor(FrameProcessor):
    """Compresses LLM context when message count exceeds a threshold.

    Position: between user_agg and llm in the pipeline.

    On each LLMContextFrame:
        - If total messages <= compress_threshold: forward unchanged.
        - If total messages > compress_threshold and there are enough conversation
          messages to summarize: calls gpt-4o-mini to summarize the older
          conversation messages (preserving initial system slots and the last
          user/assistant pair), replaces them with a single system message
          "Conversation so far: <summary>", then forwards the condensed frame.

    The initial system messages (role="system" at the start of the list before
    any user/assistant messages) are always preserved verbatim — this protects
    the persona prompt, facts slot, and memories slot.

    Latency: ~150-300ms when summarization fires (gpt-4o-mini, max_tokens=200).
    Fires at most once per ~compress_threshold/2 additional turns after first trigger.

    Args:
        api_key: OpenAI key (same as main bot).
        compress_threshold: total message count that triggers summarization (default: 10).
        summary_model: model for summarization (default: "gpt-4o-mini").
        max_summary_tokens: token limit for the summary (default: 200).
        on_compress: optional async callback(original_count, compressed_count, summary_text).
    """

    def __init__(
        self,
        api_key: str,
        *,
        compress_threshold: int = 10,
        summary_model: str = "gpt-4o-mini",
        max_summary_tokens: int = 200,
        on_compress: Optional[Callable] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._client = AsyncOpenAI(api_key=api_key)
        self._compress_threshold = compress_threshold
        self._summary_model = summary_model
        self._max_summary_tokens = max_summary_tokens
        self._on_compress = on_compress

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if not isinstance(frame, LLMContextFrame) or direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        context = frame.context
        msgs = list(context.messages)

        if len(msgs) <= self._compress_threshold:
            await self.push_frame(frame, direction)
            return

        # Separate leading system messages (slots 0, 1, 2, ...) from conversation
        system_msgs = []
        conv_msgs = []
        past_system = False
        for m in msgs:
            if m.get("role") == "system" and not past_system:
                system_msgs.append(m)
            else:
                past_system = True
                conv_msgs.append(m)

        # Need at least 4 conversation messages to compress meaningfully
        # (2 to summarize + 2 to keep as latest pair)
        if len(conv_msgs) < 4:
            await self.push_frame(frame, direction)
            return

        to_summarize = conv_msgs[:-2]
        to_keep = conv_msgs[-2:]
        original_count = len(msgs)

        try:
            summary = await self._summarize(to_summarize)
            new_msgs = (
                system_msgs
                + [{"role": "system", "content": f"Conversation so far: {summary}"}]
                + to_keep
            )
            context.set_messages(new_msgs)

            logger.info(
                f"[MEMORY] ConversationSummaryProcessor: compressing "
                f"{original_count} messages → {len(new_msgs)}"
            )

            if self._on_compress:
                await self._on_compress(original_count, len(new_msgs), summary)

        except Exception as e:
            logger.warning(f"[MEMORY] ConversationSummaryProcessor: summarization failed: {e}")

        await self.push_frame(frame, direction)

    async def _summarize(self, messages: list) -> str:
        """Summarize a list of conversation messages via gpt-4o-mini."""
        text = "\n".join(
            f"{m.get('role', 'unknown').capitalize()}: {m.get('content', '')}"
            for m in messages
            if isinstance(m.get("content"), str)
        )
        response = await self._client.chat.completions.create(
            model=self._summary_model,
            messages=[
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": text},
            ],
            max_tokens=self._max_summary_tokens,
            temperature=0,
        )
        return response.choices[0].message.content.strip()


# ── FactExtractionObserver ────────────────────────────────────────────────────


class FactExtractionObserver(BaseObserver):
    """Extracts structured facts after each assistant turn and injects them into context.

    Out-of-path observer — zero latency on the frame path.

    Watches for LLMFullResponseEndFrame via on_push_frame(). When detected:
        1. Reads the last user+assistant message pair from the shared LLMContext.
        2. Calls gpt-4o-mini with a structured extraction prompt.
        3. Merges new facts into the accumulated SessionFacts dict:
               {"device": str, "os": str, "error": str, "steps_tried": list[str]}
        4. Writes the updated facts as a system message at facts_slot_index in context.

    Facts are accumulated across turns — new values overwrite old for the same key;
    unknown keys are ignored (not reset to empty). The bot "remembers" everything
    stated explicitly by the user throughout the session.

    Args:
        context: shared LLMContext object from LLMContextAggregatorPair.
        api_key: OpenAI key.
        facts_slot_index: index in context.messages for the facts system message.
                          Default: 1 (immediately after the persona system prompt).
        extraction_model: model for extraction (default: "gpt-4o-mini").
    """

    def __init__(
        self,
        context: LLMContext,
        api_key: str,
        *,
        facts_slot_index: int = 1,
        extraction_model: str = "gpt-4o-mini",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._context = context
        self._client = AsyncOpenAI(api_key=api_key)
        self._slot = facts_slot_index
        self._model = extraction_model
        self._facts: SessionFacts = {}

    @property
    def facts(self) -> SessionFacts:
        """Live reference to the accumulated facts dict.

        Returns the actual dict (not a copy) so callers holding this reference
        see updates as facts accumulate across turns.
        """
        return self._facts

    async def on_push_frame(self, data: FramePushed) -> None:
        if not isinstance(data.frame, LLMFullResponseEndFrame):
            return
        asyncio.create_task(self._extract_and_inject())

    async def _extract_and_inject(self) -> None:
        msgs = self._context.messages

        # Find the last user+assistant pair
        user_msg = None
        asst_msg = None
        for m in reversed(msgs):
            role = m.get("role", "")
            content = m.get("content", "")
            if not isinstance(content, str):
                continue
            if role == "assistant" and asst_msg is None:
                asst_msg = content
            elif role == "user" and user_msg is None:
                user_msg = content
            if user_msg is not None and asst_msg is not None:
                break

        if not user_msg:
            return

        try:
            exchange = f"User: {user_msg}\nAssistant: {asst_msg or ''}"
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _FACTS_EXTRACT_SYSTEM},
                    {"role": "user", "content": exchange},
                ],
                max_tokens=150,
                temperature=0,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content.strip()
            extracted = json.loads(raw)

            # Merge into accumulated facts (non-empty values override)
            changed = False
            for key in ("device", "os", "error"):
                v = extracted.get(key, "")
                if isinstance(v, str) and v.strip():
                    self._facts[key] = v.strip()
                    changed = True

            steps = extracted.get("steps_tried", [])
            if isinstance(steps, list) and steps:
                existing = self._facts.get("steps_tried", [])
                if isinstance(existing, list):
                    combined = list(dict.fromkeys(existing + steps))
                    self._facts["steps_tried"] = combined
                else:
                    self._facts["steps_tried"] = steps
                changed = True

            if changed:
                self._inject_facts()
                logger.info(f"[MEMORY] FactExtractionObserver updated facts: {self._facts}")

        except Exception as e:
            logger.warning(f"[MEMORY] FactExtractionObserver: extraction failed: {e}")

    def _inject_facts(self) -> None:
        """Write current facts as a system message at the configured slot index."""
        parts = []
        if self._facts.get("device"):
            parts.append(f"device={self._facts['device']}")
        if self._facts.get("os"):
            parts.append(f"os={self._facts['os']}")
        if self._facts.get("error"):
            parts.append(f"error={self._facts['error']}")
        steps = self._facts.get("steps_tried", [])
        if steps:
            parts.append(f"steps_tried=[{', '.join(steps)}]")

        if not parts:
            return

        facts_text = "Known facts: " + ", ".join(parts)
        msgs = list(self._context.messages)
        if len(msgs) > self._slot:
            msgs[self._slot] = {"role": "system", "content": facts_text}
            self._context.set_messages(msgs)
