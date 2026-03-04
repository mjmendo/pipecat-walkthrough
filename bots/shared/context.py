"""
Context summarization utilities for M4 call transfer.

When transferring from tech support to pizza bot, we pass a context summary
so the pizza bot knows why the user was transferred.

Learning note (M4):
    Context hand-off is a design choice:
    - Full context: all messages passed to pizza bot → expensive, clutters pizza context
    - Summary: one system message describing the transfer → cheaper, focused

    We use a simple summary here. In production you'd call the LLM to
    generate a summary, then pass that as a system message to the pizza bot.
"""

from typing import List


def build_transfer_summary(tech_support_messages: List[dict]) -> str:
    """Build a brief transfer context summary from tech support conversation.

    Extracts user messages to understand what the user discussed with tech support.
    """
    user_messages = [
        m["content"] for m in tech_support_messages if m.get("role") == "user"
    ]

    if not user_messages:
        return "User was transferred from tech support."

    recent = user_messages[-3:]  # Last 3 user turns for context
    topics = "; ".join(recent[:2])  # Summarize briefly

    return (
        f"This user was just transferred from our tech support team. "
        f"They mentioned: '{topics}'. "
        "They now want to order a pizza. Greet them warmly and take their order."
    )
