"""
M7 — Voice Agent Guardrails

Reusable FrameProcessor and BaseObserver primitives for voice bot safety:

- ContentSafetyGuard: OpenAI Moderation API (free, omni-moderation-latest)
- TopicGuard:         gpt-4o-mini binary YES/NO classifier (~$0.001/call)
- PIIRedactGuard:     regex-based PII redaction (zero latency, CPU-only)
- GuardrailAuditObserver: out-of-path audit log collector

Design principle:
    Guards are FrameProcessors — they sit in the frame path and can block,
    redirect, or mutate frames. Audit is a BaseObserver — it sits out of the
    frame path and receives events via register_event() called by the guards.

    This follows the M6 pattern: use processors to transform/gate, use
    observers to monitor. Guards need to gate frames, so they're processors.
    Audit only logs, so it's an observer.

Defense-in-depth pipeline ordering (cheapest rejection first):
    stt → ContentSafetyGuard (async, free) → TopicGuard (LLM, ~150ms)
        → PIIRedactGuard (regex, <1ms) → user_agg → llm
        → PIIRedactGuard (output, <1ms) → tts → ...

All guards use the same OpenAI API key already in .env. No new accounts needed.

Usage:
    from bots.shared.guardrails import (
        ContentSafetyGuard, TopicGuard, PIIRedactGuard,
        GuardrailAuditObserver, GuardrailEvent, GuardrailAction,
    )

    audit = GuardrailAuditObserver(buffer_events=True)

    pipeline = Pipeline([
        transport.input(), stt,
        ContentSafetyGuard(api_key=..., async_mode=True, on_guardrail_event=audit.register_event),
        TopicGuard(api_key=..., on_guardrail_event=audit.register_event),
        PIIRedactGuard(mode="input", on_guardrail_event=audit.register_event),
        user_agg, llm,
        PIIRedactGuard(mode="output", on_guardrail_event=audit.register_event),
        tts, transport.output(), assistant_agg,
    ])

    task = PipelineTask(pipeline, observers=[..., audit])
"""

import asyncio
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional

from loguru import logger
from openai import AsyncOpenAI

from pipecat.frames.frames import LLMTextFrame, TranscriptionFrame, TTSSpeakFrame
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


# ── Supporting types ───────────────────────────────────────────────────────


class GuardrailAction(Enum):
    BLOCK_REDIRECT = "BLOCK_REDIRECT"   # blocked + sent redirect phrase to TTS
    REDACT = "REDACT"                   # PII removed, frame forwarded mutated
    FLAG_ONLY = "FLAG_ONLY"             # logged but not blocked (future use)


@dataclass
class GuardrailEvent:
    guard_name: str
    action: GuardrailAction
    reason: str
    original_text: str          # raw PII / flagged content — kept in memory only, never logged
    redacted_text: Optional[str]
    latency_ms: float


# ── PII regex patterns ─────────────────────────────────────────────────────
# Order matters: more specific patterns first to avoid partial replacements.

_PII_PATTERNS = [
    ("SSN",         re.compile(r'\b\d{3}[-\s]\d{2}[-\s]\d{4}\b')),
    ("CREDIT_CARD", re.compile(r'\b(?:\d[ \-]?){13,16}\b')),
    ("PHONE",       re.compile(r'\b(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b')),
    ("EMAIL",       re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')),
    ("IP_ADDRESS",  re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')),
]


# ── ContentSafetyGuard ────────────────────────────────────────────────────


class ContentSafetyGuard(FrameProcessor):
    """Checks TranscriptionFrame content against OpenAI Moderation API.

    Two modes:
        async_mode=False (blocking): waits for moderation result before
            forwarding the frame. Adds ~100ms latency to every turn.

        async_mode=True (background): forwards the frame immediately so the
            pipeline can start the LLM call. Moderation runs in a background
            task. If flagged, pushes a TTSSpeakFrame to interrupt the ongoing
            LLM response. Latency is hidden behind LLM TTFB (~700ms).

    On trigger:
        - Drops the original TranscriptionFrame (LLM is never invoked).
        - Pushes TTSSpeakFrame(rejection_phrase) downstream.
        - Calls on_guardrail_event(GuardrailEvent) for audit.

    The OpenAI Moderation API (omni-moderation-latest) is free.

    Learning note:
        async_mode=True exploits the latency budget: guard TTFB (~100ms) is
        always less than LLM TTFB (~700ms). So the moderation result arrives
        before the LLM produces its first token — the guard can interrupt
        before any harmful response reaches TTS.
    """

    def __init__(
        self,
        api_key: str,
        *,
        async_mode: bool = True,
        rejection_phrase: str = (
            "I'm not able to help with that. "
            "Is there something else I can assist you with?"
        ),
        on_guardrail_event: Optional[Callable] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._client = AsyncOpenAI(api_key=api_key)
        self._async_mode = async_mode
        self._rejection_phrase = rejection_phrase
        self._on_event = on_guardrail_event

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if not isinstance(frame, TranscriptionFrame) or direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        if self._async_mode:
            # Forward immediately; moderation runs concurrently.
            # If flagged, TTSSpeakFrame will interrupt the in-flight LLM response.
            await self.push_frame(frame, direction)
            asyncio.create_task(self._check_async(frame.text))
        else:
            flagged = await self._check_blocking(frame.text)
            if not flagged:
                await self.push_frame(frame, direction)
            # Flagged: frame is dropped, TTSSpeakFrame was pushed inside _check_blocking.

    async def _check_blocking(self, text: str) -> bool:
        """Inline check. Blocks pipeline until moderation returns. Returns True if flagged."""
        t0 = time.perf_counter()
        flagged, reason = await self._moderate(text)
        latency_ms = (time.perf_counter() - t0) * 1000

        if flagged:
            await self._handle_flagged(text, reason, latency_ms)

        return flagged

    async def _check_async(self, text: str) -> None:
        """Background check. Pushes TTSSpeakFrame interrupt if flagged."""
        t0 = time.perf_counter()
        flagged, reason = await self._moderate(text)
        latency_ms = (time.perf_counter() - t0) * 1000

        if flagged:
            await self._handle_flagged(text, reason, latency_ms)

    async def _moderate(self, text: str):
        """Call OpenAI Moderation API. Returns (flagged: bool, reason: str)."""
        try:
            response = await self._client.moderations.create(
                model="omni-moderation-latest",
                input=text,
            )
            result = response.results[0]
            if not result.flagged:
                return False, ""

            # Collect all triggered category names for audit
            cats = result.categories.model_dump()
            triggered = [k for k, v in cats.items() if v]
            return True, ", ".join(triggered)

        except Exception as e:
            logger.warning(f"[GUARDRAIL] ContentSafetyGuard API error: {e}")
            return False, ""   # fail open on API errors

    async def _handle_flagged(self, text: str, reason: str, latency_ms: float):
        logger.warning(
            f"[GUARDRAIL] ContentSafetyGuard BLOCK | reason={reason} | latency={latency_ms:.0f}ms"
        )
        await self.push_frame(TTSSpeakFrame(self._rejection_phrase), FrameDirection.DOWNSTREAM)

        if self._on_event:
            event = GuardrailEvent(
                guard_name="ContentSafetyGuard",
                action=GuardrailAction.BLOCK_REDIRECT,
                reason=reason,
                original_text=text,
                redacted_text=None,
                latency_ms=latency_ms,
            )
            await self._on_event(event)


# ── TopicGuard ────────────────────────────────────────────────────────────

_TOPIC_SYSTEM_PROMPT = """\
You are a strict topic classifier for a tech support voice agent.

The ONLY allowed topics are: computer hardware, software, networking, internet \
connectivity, printers, phones, tablets, and related technical support.

Respond with exactly one word: YES if the user's message is on-topic, NO if off-topic.
Do not explain. Do not add punctuation. One word only."""


class TopicGuard(FrameProcessor):
    """Classifies TranscriptionFrame content as on-topic or off-topic.

    Uses a gpt-4o-mini chat completion with a minimal YES/NO prompt (~150ms, ~$0.001/call).
    On off-topic detection:
        1. Pushes TTSSpeakFrame(redirect_phrase) — bot redirects without invoking main LLM.
        2. Drops the original TranscriptionFrame.
        3. Calls on_guardrail_event(GuardrailEvent) for audit.
        4. Calls on_off_topic(frame) if provided — used for M4-style call transfer.

    Learning note:
        gpt-4o-mini at max_tokens=1 returns YES or NO in ~150ms. This is slower
        than async ContentSafetyGuard (~100ms hidden) but still fast enough to
        reject before the main LLM would respond. For binary classifiers, always
        prefer the smallest model and lowest max_tokens.
    """

    def __init__(
        self,
        api_key: str,
        *,
        classifier_model: str = "gpt-4o-mini",
        redirect_phrase: str = (
            "I can only help with tech support questions. "
            "What device or connection issue can I help you with?"
        ),
        on_off_topic: Optional[Callable] = None,
        on_guardrail_event: Optional[Callable] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = classifier_model
        self._redirect_phrase = redirect_phrase
        self._on_off_topic = on_off_topic
        self._on_event = on_guardrail_event

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if not isinstance(frame, TranscriptionFrame) or direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        t0 = time.perf_counter()
        on_topic = await self._classify(frame.text)
        latency_ms = (time.perf_counter() - t0) * 1000

        if on_topic:
            await self.push_frame(frame, direction)
            return

        logger.warning(
            f"[GUARDRAIL] TopicGuard REDIRECT | latency={latency_ms:.0f}ms "
            f"| text={frame.text[:60]!r}"
        )
        await self.push_frame(TTSSpeakFrame(self._redirect_phrase), FrameDirection.DOWNSTREAM)

        if self._on_event:
            event = GuardrailEvent(
                guard_name="TopicGuard",
                action=GuardrailAction.BLOCK_REDIRECT,
                reason="off-topic",
                original_text=frame.text,
                redacted_text=None,
                latency_ms=latency_ms,
            )
            await self._on_event(event)

        if self._on_off_topic:
            await self._on_off_topic(frame)

    async def _classify(self, text: str) -> bool:
        """Returns True if on-topic (safe to pass to the main LLM)."""
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _TOPIC_SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                max_tokens=1,
                temperature=0,
            )
            answer = response.choices[0].message.content.strip().upper()
            return answer.startswith("Y")   # "YES" → True, "NO" → False

        except Exception as e:
            logger.warning(f"[GUARDRAIL] TopicGuard classification error: {e}")
            return True  # fail open: pass to LLM on API errors


# ── PIIRedactGuard ────────────────────────────────────────────────────────


class PIIRedactGuard(FrameProcessor):
    """Regex-based PII detection and redaction. Zero latency (CPU-only).

    mode="input":  watches TranscriptionFrame downstream.
                   Redacts before the LLM sees user speech.
    mode="output": watches LLMTextFrame downstream.
                   Redacts before TTS speaks LLM output.

    Mutates frame.text in-place and always forwards the frame.
    Use both modes together for double-layer protection.

    Patterns covered: US phone, email, SSN, credit card, IP address.
    Replaced with: [PHONE], [EMAIL], [SSN], [CREDIT_CARD], [IP_ADDRESS].

    Learning note:
        Regex is the highest-ROI guard — zero latency, zero cost, compliance-
        critical. False positive risk (e.g. "error code 555-1234" flagged as
        phone) is real; Microsoft Presidio with NER is a drop-in alternative
        with lower false positive rate at 5-20ms.
    """

    def __init__(
        self,
        *,
        mode: str = "input",
        on_guardrail_event: Optional[Callable] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        if mode not in ("input", "output"):
            raise ValueError(f"PIIRedactGuard mode must be 'input' or 'output', got {mode!r}")
        self._mode = mode
        self._on_event = on_guardrail_event

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        if self._mode == "input" and isinstance(frame, TranscriptionFrame):
            await self._redact(frame)
        elif self._mode == "output" and isinstance(frame, LLMTextFrame):
            await self._redact(frame)
        else:
            await self.push_frame(frame, direction)

    async def _redact(self, frame) -> None:
        t0 = time.perf_counter()
        original = frame.text
        redacted = original
        hits = []

        for label, pattern in _PII_PATTERNS:
            new_text = pattern.sub(f"[{label}]", redacted)
            if new_text != redacted:
                hits.append(label)
                redacted = new_text

        latency_ms = (time.perf_counter() - t0) * 1000

        if hits:
            frame.text = redacted
            logger.warning(
                f"[GUARDRAIL] PIIRedactGuard REDACT | mode={self._mode} "
                f"| types={hits} | latency={latency_ms:.2f}ms"
            )
            if self._on_event:
                event = GuardrailEvent(
                    guard_name="PIIRedactGuard",
                    action=GuardrailAction.REDACT,
                    reason=f"PII types: {', '.join(hits)}",
                    original_text=original,
                    redacted_text=redacted,
                    latency_ms=latency_ms,
                )
                await self._on_event(event)

        await self.push_frame(frame, FrameDirection.DOWNSTREAM)


# ── GuardrailAuditObserver ────────────────────────────────────────────────


class GuardrailAuditObserver(BaseObserver):
    """Out-of-path audit log for all guardrail events in a session.

    Unlike other observers, this one is NOT driven by on_push_frame().
    Events arrive via register_event() called directly by guard processors.
    This means audit has zero pipeline latency regardless of frame volume.

    Logs to WARNING level without original_text to avoid PII in log files.
    Buffers events in self._events for post-session inspection or testing.
    Optional sink callback for external systems (database, alerting webhook).

    Usage:
        audit = GuardrailAuditObserver(buffer_events=True)

        # Register with each guard:
        ContentSafetyGuard(..., on_guardrail_event=audit.register_event)
        TopicGuard(..., on_guardrail_event=audit.register_event)
        PIIRedactGuard(..., on_guardrail_event=audit.register_event)

        # Add to pipeline task:
        task = PipelineTask(pipeline, observers=[..., audit])

        # After session ends:
        for event in audit.events:
            print(event.guard_name, event.action, event.reason)

    Learning note:
        PrometheusMetricsObserver (M5c) uses a similar "externally-called"
        pattern: PrometheusMetricsObserver.record_transfer() is called by the
        transfer handler, not by on_push_frame. Same idea here — some events
        don't correspond to specific frame pushes, so direct method calls are
        the right interface.
    """

    def __init__(
        self,
        *,
        buffer_events: bool = True,
        sink: Optional[Callable] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._buffer = buffer_events
        self._sink = sink
        self._events: List[GuardrailEvent] = []

    @property
    def events(self) -> List[GuardrailEvent]:
        """All buffered GuardrailEvent objects from this session (copy)."""
        return list(self._events)

    async def register_event(self, event: GuardrailEvent) -> None:
        """Receive a guardrail event from a guard processor.

        Called directly by ContentSafetyGuard, TopicGuard, or PIIRedactGuard.
        Logs at WARNING level without original_text (PII hygiene).
        """
        logger.warning(
            f"[GUARDRAIL] {event.guard_name} | action={event.action.value} "
            f"| reason={event.reason} | latency={event.latency_ms:.1f}ms"
        )

        if self._buffer:
            self._events.append(event)

        if self._sink:
            try:
                await self._sink(event)
            except Exception as e:
                logger.error(f"[GUARDRAIL] Audit sink error: {e}")

    async def on_push_frame(self, data: FramePushed) -> None:
        """Not used — events arrive via register_event(), not the frame stream."""
        pass
